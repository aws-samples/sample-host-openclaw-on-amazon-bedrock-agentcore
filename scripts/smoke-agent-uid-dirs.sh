#!/usr/bin/env bash
# Smoke test for a built bridge image: run `openclaw doctor` the way the
# contract server does, as the unprivileged agent uid (10001) with HOME=/root,
# and check that the agent uid gets its own writable cache/temp directories
# while /root stays traverse-only (0711) and unwritable for it.
#
# Usage: scripts/smoke-agent-uid-dirs.sh <image>
# Needs Docker and an image whose platform the host can run (for a local
# check, build bridge/Dockerfile for the host platform). No network or AWS
# access is used (--network none).
set -euo pipefail
IMAGE="${1:?usage: $0 <image>}"

docker run --rm --network none --entrypoint bash "$IMAGE" -c '
set -uo pipefail
fail() { echo "FAIL: $*"; exit 1; }
cd /app
# Supervisor side (root): config the doctor reads, state dir handed over, agent dirs.
node -e "
const fs = require(\"fs\");
const { renderConfig } = require(\"/app/gateway-config.js\");
fs.mkdirSync(\"/root/.openclaw/workspace\", { recursive: true });
fs.writeFileSync(\"/root/.openclaw/openclaw.json\", JSON.stringify(renderConfig({ gatewayToken: \"smoke\" })));
" || fail "could not write the smoke config"
chown -R 10001:10001 /root/.openclaw
node -e "process.exit(require(\"/app/runtime-privsep.js\").ensureAgentDirs({ log: console }) ? 0 : 1)" \
  || fail "ensureAgentDirs failed"
mapfile -t AGENT_ENV < <(node -e "
for (const [k, v] of Object.entries(require(\"/app/runtime-privsep.js\").agentDirEnv())) console.log(k + \"=\" + v);
")
[ "${#AGENT_ENV[@]}" -gt 0 ] || fail "agentDirEnv is empty (privilege separation off?)"

[ "$(stat -c %a /root)" = 711 ] || fail "/root mode is $(stat -c %a /root), want 711"
as_agent() {
  setpriv --reuid=10001 --regid=10001 --clear-groups env -i \
    PATH=/usr/local/bin:/usr/bin:/bin HOME=/root NODE_PATH=/app/node_modules \
    NODE_COMPILE_CACHE=/app/.compile-cache AWS_EC2_METADATA_DISABLED=true OPENCLAW_SKIP_CRON=1 \
    OPENCLAW_STATE_DIR=/root/.openclaw OPENCLAW_CONFIG_PATH=/root/.openclaw/openclaw.json \
    OPENCLAW_WORKSPACE_DIR=/root/.openclaw/workspace "${AGENT_ENV[@]}" "$@"
}
as_agent bash -c "touch /root/agent-write-probe" 2>/dev/null && fail "the agent uid can write to /root"
as_agent bash -c "ls /root" >/dev/null 2>&1 && fail "the agent uid can list /root"

out="$(as_agent timeout 120 openclaw doctor --fix --non-interactive 2>&1)"; rc=$?
printf "%s\n" "$out" | tail -3
[ "$rc" = 0 ] || fail "openclaw doctor exited $rc"
printf "%s\n" "$out" | grep -q -E "EACCES|Unable to create fallback" && fail "doctor reported EACCES"
cache="$(printf "%s\n" "${AGENT_ENV[@]}" | sed -n "s/^XDG_CACHE_HOME=//p")"
[ "$(stat -c %u "$cache/openclaw" 2>/dev/null)" = 10001 ] || fail "$cache/openclaw was not created by the agent uid"
as_agent bash -c "mktemp >/dev/null && mktemp -d >/dev/null" || fail "the agent uid cannot create temp files"
[ -e /root/.cache ] && fail "/root/.cache exists"
echo "PASS: agent uid caches and temp files live in its own directory; /root stays 0711"
'
