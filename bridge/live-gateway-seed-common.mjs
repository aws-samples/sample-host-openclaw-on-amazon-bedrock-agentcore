/**
 * Shared protocol and eligibility checks for the live gateway seed.
 *
 * Preparation accepts the common rendered config and a restricted neutral
 * environment. Binding must preserve that config and token, supply an exact
 * absence proof, and add only the allowed identity and credential selectors.
 * Linux process, listener and filesystem witnesses let both ends check that
 * the prepared gateway still owns the resources being adopted.
 */
import fs from "node:fs";
import path from "node:path";
import { isDeepStrictEqual } from "node:util";
import { createRequire } from "node:module";
const { renderConfig, renderAgents } = createRequire(import.meta.url)("./gateway-config.js");
function commonConfig(token, port, proxyPort) {
  const config = renderConfig({ gatewayToken: token });
  check(config.gateway.port === port && config.models.providers.agentcore.baseUrl ===
    `http://127.0.0.1:${proxyPort}/v1`, "common_config_ports");
  return config;
}
const commonAgentsText = browserEnabled => renderAgents({ browserEnabled });

export const version = "openclaw.live-gateway-seed.v4";
export const symbol = Symbol.for("openclaw.liveGatewaySeed");
export const vendor = "/usr/local/lib/node_modules/openclaw/dist/";
export const PROBE_PREPARED_FLAG = "OPENCLAW_AGENTCORE_LIVE_PROBE_PREPARED";
export const LIVE_EAGER_COMPILE_FLAG = "OPENCLAW_AGENTCORE_LIVE_EAGER_COMPILE";
export function readLiveEagerCompileFlag(value) {
  check(value === undefined || value === "1", "live_eager_compile_flag");
  return true;
}
export function liveGatewayExecArgv(value) {
  // Disable lazy bytecode compilation; this does not force optimizing JIT tiers.
  return [...(readLiveEagerCompileFlag(value) ? ["--no-lazy"] : []),
    "--import", new URL("./live-gateway-seed-child.mjs", import.meta.url).pathname];
}
export function assertLiveEagerCompileExecArgv(value, argv = process.execArgv) {
  const enabled = readLiveEagerCompileFlag(value);
  check(Array.isArray(argv) && argv.every(arg => typeof arg === "string"), "live_eager_compile_execution");
  // Permit existing audit preloads, but reject conflicting, duplicate or alternate lazy options.
  const lazy = argv.filter(arg => /^--(?:no-?)?(?:lazy|max-lazy)(?:-|=|$)/.test(arg.replaceAll("_", "-")));
  check(equal(lazy, enabled ? ["--no-lazy"] : []), "live_eager_compile_execution");
  return enabled;
}
export const BIND_OBSERVER_ASYNC_FLAG = "OPENCLAW_AGENTCORE_BIND_OBSERVER_ASYNC";
export function readBindObserverAsyncFlag(value) {
  check(value === undefined || value === "1", "bind_observer_async_flag");
  return true;
}
export const SESSION_DIFF_WORKER_FLAG = "OPENCLAW_AGENTCORE_SESSION_DIFF_WORKER_PREPARE";
export function readSessionDiffWorkerFlag(value) {
  check(value === undefined || value === "1", "session_diff_worker_flag");
  return true;
}

export const CHAT_PREPARE_FLAG = "OPENCLAW_AGENTCORE_LIVE_CHAT_PREPARE";
export const CHAT_PREPARE_REASONS = Object.freeze([
  "chat_admission_not_idle", "chat_environment_changed", "chat_export_mismatch", "chat_identity_or_selector",
  "chat_mcp_manager_present", "chat_metadata_enabled", "chat_owner_changed", "chat_owner_guard",
  "chat_owner_identity", "chat_owner_not_preparing", "chat_pin_mismatch", "chat_prepare_already_started",
  "chat_prepare_flag", "chat_registry_changed", "chat_registry_not_empty", "chat_registry_shape",
  "chat_reply_witness_missing", "chat_request_scope", "chat_version_mismatch", "chat_witness_changed",
  "chat_witness_present",
]);
export function chatFailureReason(error) {
  try {
    const message = Object.getOwnPropertyDescriptor(error, "message")?.value;
    return CHAT_PREPARE_REASONS.includes(message) ? message : "native_failure";
  } catch { return "native_failure"; }
}
export function readChatPrepareFlag(value) {
  check(value === undefined || value === "1", "chat_prepare_flag");
  return true;
}
export function assertChatNeutralEnv(env) {
  for (const key of ["USER_ID", "INTERNAL_USER_ID", "CHANNEL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "AWS_ACCESS_KEY", "AWS_SECRET_KEY", "AWS_SECURITY_TOKEN",
    "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_SDK_LOAD_CONFIG", "AWS_PROFILE", "AWS_DEFAULT_PROFILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "AWS_ROLE_SESSION_NAME",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "VITEST", "VITEST_WORKER_ID",
    "OPENCLAW_AGENTCORE_PREINIT", "NODE_OPTIONS"])
    check(env[key] === undefined, "chat_identity_or_selector");
  check(env.AWS_EC2_METADATA_DISABLED === "true", "chat_metadata_enabled");
}
export const HEARTBEAT_PREPARED_FLAG = "OPENCLAW_AGENTCORE_LIVE_HEARTBEAT_PREPARED";
export function readHeartbeatPreparedFlag(value) {
  check(value === undefined || value === "1", "heartbeat_flag");
  return true;
}
export function readProbePreparedFlag(value) {
  check(value === undefined || value === "1", "probe_flag");
  return true;
}
export function probeClock() {
  try {
    const value = process.hrtime.bigint();
    return typeof value === "bigint" && value >= 0n ? value : null;
  } catch { return null; }
}
export function probeTiming(start, end = probeClock()) {
  return { start_ns: start?.toString() ?? null, end_ns: end?.toString() ?? null,
    duration_ns: start !== null && end !== null && end >= start ? (end - start).toString() : null };
}

export const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
export const equal = isDeepStrictEqual;
export const safeReason = error => /^[a-z_]{1,64}$/.test(error?.message) ? error.message : "native_failure";
export const uuid = value => typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
export function check(value, reason) {
  if (!value) throw new Error(reason);
}
export function keys(value, names) {
  check(value && Object.getPrototypeOf(value) === Object.prototype &&
    equal(Object.keys(value).sort(), [...names].sort()), "protocol_shape");
}
export function processWitness(pid) {
  const text = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
  const fields = text.slice(text.lastIndexOf(")") + 2).split(" ");
  return { pid, start: fields[19], group: Number(fields[2]), state: fields[0] };
}
export function listeners() {
  return ["tcp", "tcp6"].flatMap(family =>
    fs.readFileSync(`/proc/net/${family}`, "utf8").trim().split("\n").slice(1).flatMap(line => {
      const fields = line.trim().split(/\s+/);
      if (fields[3] !== "0A") return [];
      const [address, port] = fields[1].split(":");
      return [{ port: parseInt(port, 16), inode: fields[9],
        loopback: ["0100007F", "00000000000000000000000001000000"].includes(address) }];
    }));
}
export function privateWitness(file, directory = false) {
  const stat = fs.lstatSync(file);
  check((directory ? stat.isDirectory() : stat.isFile() && stat.nlink === 1) &&
    stat.uid === process.getuid() && (stat.mode & 0o077) === 0, "private_identity");
  return { dev: stat.dev, ino: stat.ino };
}
export function workspaceWitness(file) {
  const stat = fs.lstatSync(file);
  check(stat.isDirectory() && stat.uid === process.getuid(), "workspace_identity");
  return { dev: stat.dev, ino: stat.ino };
}
export function configValid(config, token, port) {
  check(typeof token === "string" && token.length >= 16 && token.length <= 4096, "token_shape");
  check(config && config.gateway?.mode === "local" && config.gateway.port === port &&
    config.gateway.auth?.mode === "token" && config.gateway.auth.token === token &&
    ["off", undefined].includes(config.gateway.tailscale?.mode) &&
    ["loopback", undefined].includes(config.gateway.bind) &&
    config.gateway.controlUi?.enabled === false &&
    config.channels && Object.keys(config.channels).length === 0 && !config.mcp,
  "config_ineligible");
  check(config.tools?.profile === "full" && config.tools.exec?.host === "gateway" &&
    config.tools.exec?.mode === "full" && config.tools.deny?.includes("browser") &&
    !config.tools.deny.includes("exec") && config.agents?.defaults?.subagents?.maxConcurrent === 2,
  "original_features_required");
  const proxy = /^http:\/\/127\.0\.0\.1:(\d+)\/v1$/.exec(config.models?.providers?.agentcore?.baseUrl);
  check(proxy && Number(proxy[1]) > 1024 && Number(proxy[1]) < 65536 &&
    equal(config, commonConfig(token, port, Number(proxy[1]))), "common_config_mismatch");
}
export function agentsValid(text) {
  check(typeof text === "string" && (text === commonAgentsText(false) || text === commonAgentsText(true)),
    "agents_template_changed");
}
const neutral = new Set(["PATH", "NODE_PATH", "LANG", "TZ", "AWS_REGION", "AWS_DEFAULT_REGION",
  "AWS_EC2_METADATA_DISABLED", "CI", "DO_NOT_TRACK", "NO_COLOR", "S3_USER_FILES_BUCKET",
  "SUBAGENT_BEDROCK_MODEL_ID", "EVENTBRIDGE_SCHEDULE_GROUP", "IDENTITY_TABLE_NAME",
  "CRON_LAMBDA_ARN", "EVENTBRIDGE_ROLE_ARN", "CRON_LEAD_TIME_MINUTES", PROBE_PREPARED_FLAG, HEARTBEAT_PREPARED_FLAG, CHAT_PREPARE_FLAG, SESSION_DIFF_WORKER_FLAG, BIND_OBSERVER_ASYNC_FLAG, LIVE_EAGER_COMPILE_FLAG]);
const late = new Set(["USER_ID", "INTERNAL_USER_ID", "CHANNEL", "AWS_CONFIG_FILE", "AWS_SDK_LOAD_CONFIG"]);
export function prepareEnv(input, home) {
  check(input && Object.getPrototypeOf(input) === Object.prototype, "neutral_env_shape");
  for (const [key, value] of Object.entries(input)) {
    check(neutral.has(key) && typeof value === "string" && !value.includes("\0"), "neutral_env_key");
  }
  readProbePreparedFlag(input[PROBE_PREPARED_FLAG]);
  readBindObserverAsyncFlag(input[BIND_OBSERVER_ASYNC_FLAG]);
  readHeartbeatPreparedFlag(input[HEARTBEAT_PREPARED_FLAG]);
  const chat = readChatPrepareFlag(input[CHAT_PREPARE_FLAG]);
  readLiveEagerCompileFlag(input[LIVE_EAGER_COMPILE_FLAG]);
  const sessionDiff = readSessionDiffWorkerFlag(input[SESSION_DIFF_WORKER_FLAG]);
  // Match the pinned Linux CLI's existing prepend/dedup step before its exact environment guard.
  // The ordinary CLI still performs its own bootstrap; unexpected directory additions still fail.
  const launch = (chat || sessionDiff) && input.PATH !== undefined ? { ...input, PATH: [...new Set([
    path.dirname(process.execPath), "/usr/bin", "/bin", ...input.PATH.split(path.delimiter),
  ].map(part => part.trim()).filter(Boolean))].join(path.delimiter) } : input;
  return {
    ...launch,
    OPENCLAW_AGENTCORE_LIVE_PROBE_PREPARED: "1",
    OPENCLAW_AGENTCORE_LIVE_EAGER_COMPILE: "1",
    OPENCLAW_AGENTCORE_BIND_OBSERVER_ASYNC: "1",
    OPENCLAW_AGENTCORE_SESSION_DIFF_WORKER_PREPARE: "1",
    OPENCLAW_AGENTCORE_LIVE_CHAT_PREPARE: "1",
    OPENCLAW_AGENTCORE_LIVE_HEARTBEAT_PREPARED: "1",
    HOME: home,
    OPENCLAW_STATE_DIR: path.join(home, ".openclaw"),
    OPENCLAW_CONFIG_PATH: path.join(home, ".openclaw", "openclaw.json"),
    OPENCLAW_NO_RESPAWN: "1",
    NODE_DISABLE_COMPILE_CACHE: "1", OPENCLAW_SKIP_CRON: "1", AWS_EC2_METADATA_DISABLED: "true",
  };
}
export function validateBind(input, prepared) {
  keys(input, ["generation", "actorId", "userId", "channel", "renderedConfig",
    "gatewayToken", "boundEnv", "absenceProof"]);
  check(uuid(input.generation), "bind_generation");
  for (const value of [input.actorId, input.userId, input.channel])
    check(typeof value === "string" && /^[A-Za-z0-9_.:@/-]{1,256}$/.test(value), "bind_identity");
  check(equal(input.renderedConfig, prepared.renderedConfig) &&
    input.gatewayToken === prepared.gatewayToken, "config_token_changed");
  keys(input.absenceProof, ["generation", "actorId", "userId", "verified", "restoreGateVerified", "mcpDisabled"]);
  check(equal(input.absenceProof, { generation: input.generation, actorId: input.actorId,
    userId: input.userId, verified: true, restoreGateVerified: true, mcpDisabled: true }), "absence_unproven");
  check(input.boundEnv && Object.getPrototypeOf(input.boundEnv) === Object.prototype, "bound_env_shape");
  for (const [key, value] of Object.entries(input.boundEnv))
    check(late.has(key) && typeof value === "string" && !value.includes("\0"), "bound_env_key");
  // The pinned Bridge publishes only this credential_process config, no shared INI selector.
  check(input.boundEnv.AWS_CONFIG_FILE === "/tmp/scoped-creds/scoped-aws-config" &&
    input.boundEnv.AWS_SDK_LOAD_CONFIG === "1", "credential_selector");
  check(input.boundEnv.USER_ID === input.actorId && input.boundEnv.INTERNAL_USER_ID === input.userId &&
    input.boundEnv.CHANNEL === input.channel, "bound_env_identity");
}
