#!/usr/bin/env bash
# Smoke test for a built bridge image: run `openclaw doctor` the way the
# contract server does, as the unprivileged agent uid (10001) with HOME=/root,
# and check that the agent uid gets its own writable cache/temp directories
# while /root stays traverse-only (0711) and unwritable for it.
#
# Three runs, each in a fresh container:
#   1. root      - doctor on the synthetic state as root. Proves the state
#                  layout itself is one doctor accepts, so a failure in run 2
#                  is about the uid split, not about the harness.
#   2. agent     - the real check, as uid 10001.
#   3. negative  - as run 2, but /home/openclaw is handed back to root after
#                  the supervisor prepared it. This run MUST fail; if it
#                  passes, the gate cannot detect the bug it exists for.
#
# The synthetic state is shaped like a 2.0 state OpenClaw has already used:
# OpenClaw creates its state database (`openclaw models list`, no network) and
# the shared auth store is marked as living in that database, which is the row
# OpenClaw writes itself the first time a fresh store is used. A brand-new state dir without that row
# reads as a pre-2.0 "legacy" store, so doctor runs the legacy shared-auth
# migration, which takes the state lock with a 250 ms budget and stops doctor
# ("Failed migrating legacy shared auth store ... owns this state directory")
# whenever it cannot get it. A used state carries the row and skips it.
#
# Host preflight: OpenClaw takes its state lock with openat2(2), which Linux
# has from 5.6. Where the container cannot use it (an older host kernel, such as
# the 4.14 kernel of CodeBuild's arm64 build hosts, or a seccomp profile without
# openat2), every OpenClaw command that takes the lock fails, as root too (on
# such a kernel with "Function not implemented (os error 38)"), so the host
# cannot test the uid split at all. The preflight asks the kernel for openat2 from inside the image;
# if it is missing, or the check cannot tell and the kernel is older than 5.6,
# the script prints SKIP with the reason and exits 0 without running the three
# runs. Every other host runs them.
#
# Usage: scripts/smoke-agent-uid-dirs.sh <image>
# Needs Docker and an image whose platform the host can run (for a local
# check, build bridge/Dockerfile for the host platform). No network or AWS
# access is used (--network none). Exit 0 when all three runs behave, or with
# SKIP on a host that cannot run OpenClaw (see above); non-zero otherwise.
# SMOKE_TIMEOUT (seconds, default 120) bounds each OpenClaw command; raise it on
# slow or emulated hosts.
set -euo pipefail
IMAGE="${1:?usage: $0 <image>}"
SMOKE_TIMEOUT="${SMOKE_TIMEOUT:-120}"
[[ "$SMOKE_TIMEOUT" =~ ^[0-9]+$ ]] || { echo "SMOKE_TIMEOUT must be a number of seconds" >&2; exit 2; }

inner() {
  cat <<'INNER'
set -uo pipefail
MODE="$1"
T="$2"
fail() { echo "FAIL[$MODE]: $*"; exit 1; }
cd /app
OC_ENV=(AWS_EC2_METADATA_DISABLED=true OPENCLAW_SKIP_CRON=1
  OPENCLAW_STATE_DIR=/root/.openclaw OPENCLAW_CONFIG_PATH=/root/.openclaw/openclaw.json
  OPENCLAW_WORKSPACE_DIR=/root/.openclaw/workspace)

# Supervisor side (root): the config doctor reads.
node -e "
const fs = require(\"fs\");
const { renderConfig } = require(\"/app/gateway-config.js\");
fs.mkdirSync(\"/root/.openclaw/workspace\", { recursive: true });
fs.writeFileSync(\"/root/.openclaw/openclaw.json\", JSON.stringify(renderConfig({ gatewayToken: \"smoke\" })));
" || fail "could not write the smoke config"

AGENT_ENV=()
if [ "$MODE" = root ]; then
  run() { env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/root NODE_PATH=/app/node_modules \
    NODE_COMPILE_CACHE=/app/.compile-cache "${OC_ENV[@]}" "$@"; }
else
  # State dir handed over, agent dirs prepared, exactly as the contract server does.
  chown -R 10001:10001 /root/.openclaw
  node -e "process.exit(require(\"/app/runtime-privsep.js\").ensureAgentDirs({ log: console }) ? 0 : 1)" \
    || fail "ensureAgentDirs failed"
  mapfile -t AGENT_ENV < <(node -e "
for (const [k, v] of Object.entries(require(\"/app/runtime-privsep.js\").agentDirEnv())) console.log(k + \"=\" + v);
")
  [ "${#AGENT_ENV[@]}" -gt 0 ] || fail "agentDirEnv is empty (privilege separation off?)"
  [ "$(stat -c %a /root)" = 711 ] || fail "/root mode is $(stat -c %a /root), want 711"
  run() {
    setpriv --reuid=10001 --regid=10001 --clear-groups env -i \
      PATH=/usr/local/bin:/usr/bin:/bin HOME=/root NODE_PATH=/app/node_modules \
      NODE_COMPILE_CACHE=/app/.compile-cache "${OC_ENV[@]}" "${AGENT_ENV[@]}" "$@"
  }
  run bash -c "touch /root/agent-write-probe" 2>/dev/null && fail "the agent uid can write to /root"
  run bash -c "ls /root" >/dev/null 2>&1 && fail "the agent uid can list /root"
  # OpenClaw watches \$HOME/.agents/skills; it must exist and be watchable, not writable.
  run node -e "require(\"fs\").watch(\"/root/.agents/skills\").close()" \
    || fail "the agent uid cannot watch /root/.agents/skills (OpenClaw would fall back to watching /root)"
  run bash -c "touch /root/.agents/skills/probe" 2>/dev/null && fail "the agent uid can write to /root/.agents/skills"
  # Negative control: take the agent's own directories away again.
  [ "$MODE" = negative ] && chown -R 0:0 /home/openclaw
fi

# Synthetic 2.0 state, created by the uid under test (see the header).
run timeout "$T" openclaw models list >/dev/null 2>&1 || fail "openclaw could not create its state database"
run node -e "
const { DatabaseSync } = require(\"node:sqlite\");
const db = new DatabaseSync(\"/root/.openclaw/state/openclaw.sqlite\");
db.prepare(\"INSERT INTO config_machine_state (state_key, value_json, updated_at_ms) VALUES (?, ?, ?) ON CONFLICT (state_key) DO NOTHING\")
  .run(\"auth.sharedStore\", JSON.stringify({ location: \"state-db\" }), Date.now());
const row = db.prepare(\"SELECT value_json FROM config_machine_state WHERE state_key = ?\").get(\"auth.sharedStore\");
db.close();
process.exit(row && JSON.parse(row.value_json).location === \"state-db\" ? 0 : 1);
" || fail "could not mark the shared auth store as state-db (OpenClaw state schema changed?)"

# On a doctor failure, say why: take the state lock the way doctor's
# migrations do and print the error chain, plus the lock directories' owners
# and filesystems (doctor itself only prints "owns this state directory").
diagnose() {
  run node --input-type=module -e "
import fs from \"node:fs\";
import path from \"node:path\";
const dist = \"/usr/local/lib/node_modules/openclaw/dist\";
let acquire;
for (const f of fs.readdirSync(dist).filter((n) => /^gateway-lock-.*[.]mjs\$/.test(n))) {
  acquire = Object.values(await import(path.join(dist, f))).find((v) => typeof v === \"function\" && v.name === \"acquireGatewayLock\");
  if (acquire) break;
}
if (!acquire) { console.log(\"diag: acquireGatewayLock not found in this OpenClaw build\"); process.exit(0); }
try {
  const lock = await acquire({ allowInTests: true, env: process.env, pollIntervalMs: 25, role: \"sqlite-maintenance\", timeoutMs: 250 });
  console.log(\"diag: state lock acquired now\");
  await lock?.release();
} catch (error) {
  for (let e = error, d = 0; e && d < 6; e = e.cause, d += 1) console.log(\"diag: \" + \"  \".repeat(d) + e.name + \": \" + e.message);
}
" 2>&1 | head -10
  for d in /tmp /tmp/openclaw-state-locks-* /root/.openclaw /root/.openclaw/tmp /root/.openclaw/tmp/openclaw-*; do
    [ -e "$d" ] && echo "diag: $(stat -c "%a %u:%g" "$d") $(stat -f -c %T "$d") $d"
  done
}

out="$(run timeout "$T" openclaw doctor --fix --non-interactive 2>&1)"; rc=$?
printf "%s\n" "$out" | grep -E "Failed migrating|Doctor stopped|EACCES|Unable to create fallback|Doctor complete" | head -5
if [ "$rc" != 0 ] || printf "%s\n" "$out" | grep -q -E "Doctor stopped|Failed migrating"; then diagnose; fi
[ "$rc" = 0 ] || fail "openclaw doctor exited $rc"
printf "%s\n" "$out" | grep -q -E "Doctor stopped|Failed migrating" && fail "doctor refused a state migration"
printf "%s\n" "$out" | grep -q -E "EACCES|Unable to create fallback" && fail "doctor reported EACCES"
if [ "$MODE" = root ]; then
  echo "PASS[root]: doctor accepts the synthetic state"
  exit 0
fi
cache="$(printf "%s\n" "${AGENT_ENV[@]}" | sed -n "s/^XDG_CACHE_HOME=//p")"
[ "$(stat -c %u "$cache/openclaw" 2>/dev/null)" = 10001 ] || fail "$cache/openclaw was not created by the agent uid"
run bash -c "mktemp >/dev/null && mktemp -d >/dev/null" || fail "the agent uid cannot create temp files"
[ -e /root/.cache ] && fail "/root/.cache exists"
echo "PASS[$MODE]: agent uid caches and temp files live in its own directory; /root stays 0711"
INNER
}

smoke() {
  docker run --rm -i --network none --entrypoint bash "$IMAGE" -s -- "$1" "$SMOKE_TIMEOUT" < <(inner)
}

# Runs inside the image: the host kernel version and whether openat2 exists.
preflight_probe() {
  cat <<'PREFLIGHT'
echo "kernel=$(uname -r)"
if command -v python3 >/dev/null 2>&1; then
  # openat2(AT_FDCWD, "/", NULL, 0) fails with EINVAL where the call exists,
  # before any file is touched. ENOSYS means the kernel does not have it; ENOSYS
  # or EPERM can also come from a seccomp profile that does not allow it.
  python3 -c '
import ctypes, errno
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
rc = libc.syscall(ctypes.c_long(437), ctypes.c_int(-100), ctypes.c_char_p(b"/"), ctypes.c_void_p(0), ctypes.c_size_t(0))
blocked = rc < 0 and ctypes.get_errno() in (errno.ENOSYS, errno.EPERM)
print("openat2=" + ("missing" if blocked else "present"))
' 2>/dev/null || echo "openat2=unknown"
else
  echo "openat2=unknown"
fi
PREFLIGHT
}

older_than_5_6() {  # $1: uname -r, e.g. 4.14.355-287.753.amzn2.aarch64
  local version="${1%%-*}" major minor
  major="${version%%.*}"
  minor="${version#*.}"
  minor="${minor%%.*}"
  [[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ ]] || return 1
  (( major < 5 || (major == 5 && minor < 6) ))
}

host_preflight() {
  local out kernel openat2 reason=""
  out="$(docker run --rm -i --network none --entrypoint bash "$IMAGE" -s -- preflight < <(preflight_probe))"
  kernel="$(printf "%s\n" "$out" | sed -n "s/^kernel=//p")"
  openat2="$(printf "%s\n" "$out" | sed -n "s/^openat2=//p")"
  case "$openat2" in
    present) ;;
    missing) reason="openat2(2) is not available in the container (host kernel ${kernel:-unknown})" ;;
    *) if older_than_5_6 "$kernel"; then reason="host kernel $kernel is older than Linux 5.6, which added openat2(2)"; fi ;;
  esac
  if [ -n "$reason" ]; then
    echo "SKIP: $reason. OpenClaw takes its state lock with openat2, so every OpenClaw command fails on this host, as root too, and the uid split cannot be tested here. Run this check on a host with Linux 5.6 or newer whose container runtime allows openat2."
    exit 0
  fi
  echo "preflight: host kernel ${kernel:-unknown}, openat2 ${openat2:-unknown}"
}

host_preflight
smoke root
smoke agent
if smoke negative; then
  echo "FAIL: the negative control passed (agent uid without its own dirs); this check cannot detect the bug"
  exit 1
fi
echo "PASS: negative control failed as expected"
echo "PASS: all smoke runs behaved"
