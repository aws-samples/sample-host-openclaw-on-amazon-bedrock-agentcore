/**
 * Runtime privilege separation for the OpenClaw gateway and its tool shells.
 *
 * Why this exists
 * ---------------
 * On AgentCore Runtime the container starts as root (the contract server runs
 * as PID 1 with no USER directive). Execution-role credentials are delivered by
 * the microVM metadata service (MMDS), the IMDS-like link-local endpoint, to
 * *any* process in the VM — see
 * https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html
 * ("Understand credential exposure within the VM"). OpenClaw exposes an `exec`
 * tool, so a shell the model runs could:
 *   - read another process's /proc/<pid>/environ and recover the execution
 *     role's credentials from the supervisor or proxy, or
 *   - query the MMDS / container credential endpoint and lift the full,
 *     unscoped execution role,
 * bypassing the per-user scoped STS credentials (PR #206). The execution role
 * can read every user's openclaw/cognito-user/* secret (and so mint any user's
 * Cognito tokens), so the agent must not be able to recover it.
 *
 * The contract server (and the Bedrock proxy it owns) are the trusted
 * supervisor: they legitimately hold the execution role to fetch secrets,
 * AssumeRole for scoped per-user credentials and call Bedrock. They stay root.
 * OpenClaw — the untrusted agent — and everything it spawns (tool shells,
 * `clawhub`, `openclaw doctor`) run as a dedicated unprivileged uid instead.
 *
 * What this module gives the caller
 * ----------------------------------
 *   - childPrivilegeOptions(): spawn() options ({ uid, gid } or {}) for every
 *     OpenClaw-family child. Only populated when we are root and can therefore
 *     actually drop privileges; otherwise empty, and the caller is told.
 *   - chownToOpenClaw(path): hand a file/dir the gateway must read or write to
 *     the unprivileged uid. Best-effort and only meaningful when we are root.
 *   - describeProtections()/formatStartupLines(): a plain statement of which
 *     protections are active, for the startup log (the network block and the
 *     /proc hardening are best-effort; this says so out loud).
 *
 * Every privileged step fails safe: if we cannot drop privileges, spawning with
 * { uid } simply fails and the gateway activation fails closed rather than
 * silently running the agent as root. The network and /proc layers are
 * defence-in-depth on top of the uid split, which alone already closes the
 * cross-process /proc/<pid>/environ read (the kernel only lets the owning uid
 * read another process's environ).
 */

"use strict";

const realFs = require("node:fs");

// Fixed, unprivileged uid/gid the OpenClaw gateway and its shells run as. A
// numeric id (not a name) so the container never trips the AgentCore >53-layer
// "424 / Failed to mount overlay" issue with non-numeric USER values. Overridable
// only for tests/local runs; production uses the Dockerfile-provisioned account.
const DEFAULT_UID = 10001;
const DEFAULT_GID = 10001;

function resolveId(raw, fallback) {
  if (raw === undefined || raw === null || raw === "") return fallback;
  const value = Number(raw);
  return Number.isInteger(value) && value > 0 ? value : fallback;
}

function openClawUid(env = process.env) {
  return resolveId(env.OPENCLAW_RUNTIME_UID, DEFAULT_UID);
}

function openClawGid(env = process.env) {
  return resolveId(env.OPENCLAW_RUNTIME_GID, DEFAULT_GID);
}

/**
 * True when this process is root and so can setuid/setgid its children. Only
 * root can hand a child a lower uid; a non-root parent cannot, so there is
 * nothing to configure (and the caller must be told the agent is NOT isolated).
 */
function isRoot(proc = process) {
  return typeof proc.getuid === "function" && proc.getuid() === 0;
}

function canDropPrivileges(proc = process, env = process.env) {
  const uid = openClawUid(env);
  const gid = openClawGid(env);
  return isRoot(proc) && uid > 0 && gid > 0 && uid !== 0 && gid !== 0;
}

/**
 * spawn() options that run a child as the unprivileged OpenClaw uid/gid.
 * Returns {} when we cannot drop privileges, so the caller spawns normally;
 * in that case the agent is NOT privilege-separated and describeProtections()
 * reports it.
 */
function childPrivilegeOptions(proc = process, env = process.env) {
  if (!canDropPrivileges(proc, env)) return {};
  return { uid: openClawUid(env), gid: openClawGid(env) };
}

/**
 * Give `target` to the OpenClaw uid/gid so a non-root gateway (and the
 * credential_process `cat` it runs) can read/write it. Best-effort: a no-op
 * when we are not root or the path is absent. Never throws — a failure to chown
 * a transient /tmp artifact must not fail a user's turn; it is logged instead.
 *
 * @returns {boolean} true when at least the target existed and was chowned.
 */
function chownToOpenClaw(target, { recursive = false, proc = process, env = process.env, fs = realFs, log } = {}) {
  if (!canDropPrivileges(proc, env) || typeof target !== "string" || !target) return false;
  const uid = openClawUid(env);
  const gid = openClawGid(env);
  try {
    if (recursive) {
      return chownRecursiveSync(target, uid, gid, fs);
    }
    fs.chownSync(target, uid, gid);
    return true;
  } catch (error) {
    if (error && error.code === "ENOENT") return false;
    log?.warn?.(`[privsep] could not chown ${target} to ${uid}:${gid}: ${error.message}`);
    return false;
  }
}

function chownRecursiveSync(dir, uid, gid, fs) {
  let stat;
  try {
    stat = fs.lstatSync(dir);
  } catch (error) {
    if (error && error.code === "ENOENT") return false;
    throw error;
  }
  (fs.lchownSync || fs.chownSync)(dir, uid, gid);
  if (stat.isDirectory() && !stat.isSymbolicLink()) {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      entries = [];
    }
    for (const entry of entries) {
      chownRecursiveSync(`${dir}/${entry.name}`, uid, gid, fs);
    }
  }
  return true;
}

// --- Writable locations for the agent uid ---------------------------------
//
// HOME stays /root for every OpenClaw-family child: the state dir, the config
// path and the live-seed layout checks are all derived from it, and /root is
// 0711 (traverse only), so the agent uid cannot create anything directly under
// it. OpenClaw, clawhub and the tools they run still write per-user caches and
// temp files relative to HOME or the XDG base directories, e.g. OpenClaw's
// SQLite snapshot staging root ($XDG_CACHE_HOME, else $HOME/.cache) and npm's
// cache ($HOME/.npm). Left alone those resolve under /root and fail with
// EACCES (gateway activation and `openclaw doctor` both stop on it). Point them
// at a directory the agent uid owns instead. The Dockerfile creates it; the
// supervisor re-creates it at startup if it is missing.
const DEFAULT_AGENT_DIR = "/home/openclaw";

function agentDir(env = process.env) {
  const raw = env.OPENCLAW_RUNTIME_AGENT_DIR;
  if (typeof raw === "string" && raw.length > 1 && raw.startsWith("/") &&
      !raw.split("/").includes("..") && !raw.includes("\0")) {
    return raw.replace(/\/+$/, "");
  }
  return DEFAULT_AGENT_DIR;
}

/** Directories (parent first) the agent uid needs to own, and the env var for each. */
function agentDirLayout(env = process.env) {
  const root = agentDir(env);
  return [
    { path: root },
    { path: `${root}/.cache`, env: "XDG_CACHE_HOME" },
    { path: `${root}/.config`, env: "XDG_CONFIG_HOME" },
    { path: `${root}/.local` },
    { path: `${root}/.local/share`, env: "XDG_DATA_HOME" },
    { path: `${root}/.local/state`, env: "XDG_STATE_HOME" },
    { path: `${root}/.npm`, env: "npm_config_cache" },
    { path: `${root}/tmp`, env: "TMPDIR" },
  ];
}

/**
 * Environment entries that send the agent uid's caches, config and temp files
 * to its own directory. Merge into the env of every child spawned with
 * childPrivilegeOptions(). Empty when privilege separation is off: the child
 * then runs with the supervisor's uid and its usual HOME-relative locations.
 */
function agentDirEnv(proc = process, env = process.env) {
  if (!canDropPrivileges(proc, env)) return {};
  const out = {};
  for (const entry of agentDirLayout(env)) {
    if (entry.env) out[entry.env] = entry.path;
  }
  return out;
}

/**
 * Create the agent uid's directories (mode 0700, owned by the agent uid) when
 * they are missing. Call once at startup, before any agent-uid child exists, so
 * nothing the agent controls can be in the way. An existing entry that is not a
 * plain directory (e.g. a symlink) is left untouched and reported: chown and
 * chmod here never follow a link. Never throws.
 *
 * @returns {boolean} true when every directory is in place and owned by the agent uid.
 */
function ensureAgentDirs({ proc = process, env = process.env, fs = realFs, log } = {}) {
  if (!canDropPrivileges(proc, env)) return false;
  const uid = openClawUid(env);
  const gid = openClawGid(env);
  for (const { path: dir } of agentDirLayout(env)) {
    try {
      try {
        fs.mkdirSync(dir, { mode: 0o700 });
      } catch (error) {
        if (!error || error.code !== "EEXIST") throw error;
      }
      const stat = fs.lstatSync(dir);
      if (!stat.isDirectory() || stat.isSymbolicLink()) {
        log?.warn?.(`[privsep] ${dir} is not a directory; agent uid caches and temp files may fail`);
        return false;
      }
      (fs.lchownSync || fs.chownSync)(dir, uid, gid);
      fs.chmodSync(dir, 0o700);
    } catch (error) {
      log?.warn?.(`[privsep] could not prepare ${dir} for ${uid}:${gid}: ${error.message}`);
      return false;
    }
  }
  return true;
}

// Normalise a status reported by the entrypoint to "active" / "INACTIVE" /
// "unknown", so an inactive layer always reads the same way in the log.
function normaliseStatus(value) {
  const v = String(value ?? "").trim().toLowerCase();
  if (v === "active") return "active";
  if (v === "inactive") return "INACTIVE";
  return "unknown";
}

// Hosts the mmds-firewall.sh rules cover (keep in sync with that script).
const FIREWALLED_HOSTS = new Set(["169.254.169.254", "169.254.170.2", "fd00:ec2::254"]);

/**
 * Describe the container credential endpoint the runtime handed us, without
 * printing any credential: whether a relative URI is set (always served on the
 * firewalled 169.254.170.2), whether a full URI is set and whether its host is
 * one the firewall covers, and whether an authorization token file exists and
 * is readable by other uids. Never throws.
 */
function credentialEndpointShape(env = process.env, fs = realFs) {
  const shape = { relativeUri: Boolean(env.AWS_CONTAINER_CREDENTIALS_RELATIVE_URI), fullUri: "none", tokenFile: "none" };
  const fullUri = env.AWS_CONTAINER_CREDENTIALS_FULL_URI;
  if (fullUri) {
    let host = null;
    try {
      host = new URL(fullUri).hostname.replace(/^\[|\]$/g, "").toLowerCase();
    } catch {
      host = null;
    }
    if (host === null) shape.fullUri = "unparseable (NOT covered by the firewall)";
    else if (FIREWALLED_HOSTS.has(host)) shape.fullUri = "covered by the firewall";
    else shape.fullUri = `NOT covered by the firewall (host ${host})`;
  }
  const tokenFile = env.AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE;
  if (tokenFile) {
    try {
      const mode = fs.statSync(tokenFile).mode;
      shape.tokenFile = (mode & 0o044) ? "readable by other uids" : "owner-only";
    } catch {
      shape.tokenFile = "set but not readable";
    }
  }
  return shape;
}

/**
 * A structured statement of the protections in force, for the startup log.
 * `networkBlock` and `procHidden` are set by the entrypoint (it owns the
 * iptables/hidepid attempts); this module reports the uid layer, which it
 * owns, and composes the full picture.
 */
function describeProtections({ proc = process, env = process.env, networkBlock = "unknown", procHidden = "unknown", fs = realFs } = {}) {
  const dropped = canDropPrivileges(proc, env);
  return {
    uidSplit: dropped ? "active" : "INACTIVE",
    uid: dropped ? openClawUid(env) : (isRoot(proc) ? null : (proc.getuid?.() ?? null)),
    gid: dropped ? openClawGid(env) : null,
    // The uid split alone closes the cross-process /proc/<pid>/environ read.
    procEnvironIsolated: dropped ? "active" : "INACTIVE",
    procHidden: normaliseStatus(procHidden),
    networkBlock: normaliseStatus(networkBlock),
    credentialEndpoint: credentialEndpointShape(env, fs),
  };
}

function formatStartupLines(state) {
  const lines = [];
  lines.push(`[privsep] protections: uid_split=${state.uidSplit} proc_environ_isolation=${state.procEnvironIsolated} ` +
    `proc_hidepid=${state.procHidden} metadata_block=${state.networkBlock}`);
  if (state.uidSplit === "active") {
    lines.push(`[privsep] OpenClaw gateway + tool shells run as uid=${state.uid} gid=${state.gid} (supervisor stays root)`);
    lines.push("[privsep] /proc/<pid>/environ of the supervisor and proxy is unreadable to the agent uid (kernel-enforced)");
  } else {
    lines.push("[privsep] WARNING: privilege separation INACTIVE — OpenClaw runs with the supervisor's privileges. " +
      "The container must start as root for the uid split to apply.");
  }
  lines.push(`[privsep] /proc other-process hardening (hidepid): ${state.procHidden}`);
  lines.push(`[privsep] MMDS / metadata network block for the agent uid: ${state.networkBlock}`);
  if (state.networkBlock !== "active") {
    lines.push("[privsep] NOTE: with the network block inactive, a tool shell can still reach the metadata endpoint " +
      "and read the full execution role. Scope the execution role tightly (least privilege) as the remaining control.");
  }
  const endpoint = state.credentialEndpoint;
  if (endpoint) {
    lines.push(`[privsep] container credential endpoint: relative_uri=${endpoint.relativeUri ? "set" : "unset"} ` +
      `full_uri=${endpoint.fullUri} token_file=${endpoint.tokenFile}`);
    if (/^NOT covered|^unparseable/.test(endpoint.fullUri) || endpoint.tokenFile === "readable by other uids") {
      lines.push("[privsep] NOTE: the agent uid may be able to reach the container credential endpoint; " +
        "the metadata firewall does not cover this endpoint shape.");
    }
  }
  return lines;
}

module.exports = {
  DEFAULT_UID,
  DEFAULT_GID,
  openClawUid,
  openClawGid,
  isRoot,
  canDropPrivileges,
  childPrivilegeOptions,
  chownToOpenClaw,
  DEFAULT_AGENT_DIR,
  agentDir,
  agentDirLayout,
  agentDirEnv,
  ensureAgentDirs,
  credentialEndpointShape,
  describeProtections,
  formatStartupLines,
};
