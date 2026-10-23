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
 *   - read another process's /proc/<pid>/environ (the proxy child carries
 *     COGNITO_PASSWORD_SECRET in its environment), or
 *   - curl the MMDS address and lift the full, unscoped execution role,
 * bypassing the per-user scoped STS credentials (PR #206).
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

/**
 * A structured statement of the protections in force, for the startup log and
 * the fix39 report. `networkBlock` and `procHidden` are set by the entrypoint
 * (it owns the iptables/hidepid attempts); this module reports the uid layer,
 * which it owns, and composes the full picture.
 */
function describeProtections({ proc = process, env = process.env, networkBlock = "unknown", procHidden = "unknown" } = {}) {
  const dropped = canDropPrivileges(proc, env);
  return {
    uidSplit: dropped ? "active" : "INACTIVE",
    uid: dropped ? openClawUid(env) : (isRoot(proc) ? null : (proc.getuid?.() ?? null)),
    gid: dropped ? openClawGid(env) : null,
    // The uid split alone closes the cross-process /proc/<pid>/environ read.
    procEnvironIsolated: dropped ? "active" : "INACTIVE",
    procHidden,
    networkBlock,
  };
}

function formatStartupLines(state) {
  const lines = [];
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
  describeProtections,
  formatStartupLines,
};
