/**
 * Prepare gateway modules before the original CLI opens user state.
 *
 * This one-shot preload imports the pinned server and reply runtimes in the
 * serving process, prepares SQLite invariants, then waits for parent IPC.
 * Paths, CLI arguments and the seed environment stay fixed across activation;
 * only the actor and user identities may be added from the validated binding.
 *
 * Activation resets snapshot presence, arms the prepared receiver, and
 * acknowledges the generation before releasing the CLI. The parent owns
 * deadlines so no bootstrap-relative timeout survives the snapshot.
 * Subsequent activation messages remain fatal after the CLI starts.
 */
import { readFileSync, realpathSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { createInvariantPrewarmer } from "./gateway-invariant-prewarm.mjs";

export const OPENCLAW_VERSION = "2026.9.5";
export const PACKAGE_ROOT = "/usr/local/lib/node_modules/openclaw";
export const LAUNCHER_URL = pathToFileURL(`${PACKAGE_ROOT}/openclaw.mjs`).href;
export const RUNTIME_FILES = Object.freeze({
  server: "dist/server-start-gE7ggn-j.mjs",
  reply: "dist/get-reply-from-config.runtime-uSvr2ey5.mjs",
  presence: "dist/system-presence-B9mMmTN5.mjs",
});
const IDENTITIES = new Set(["USER_ID", "INTERNAL_USER_ID"]);
const CREDENTIAL_SOURCES = [
  "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
  "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
  "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
  "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "AWS_ROLE_SESSION_NAME",
  "AWS_SHARED_CREDENTIALS_FILE", "AWS_CREDENTIAL_FILE", "AWS_BEARER_TOKEN_BEDROCK",
];
const STABLE_PATHS = [
  "HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH", "OPENCLAW_WORKSPACE_DIR",
];

function invalid(message) {
  return new Error(`OpenClaw preinit: ${message}.`);
}

function identifier(value) {
  return typeof value === "string" && value.trim().length > 0 &&
    value === value.trim() && !/[\u0000-\u001f\u007f]/u.test(value);
}

function record(value) {
  if (!value || typeof value !== "object") return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

function ownDataKeys(value) {
  if (!record(value)) throw invalid("invalid IPC object");
  const keys = Reflect.ownKeys(value);
  if (keys.some((key) => typeof key !== "string" ||
      !Object.getOwnPropertyDescriptor(value, key)?.enumerable ||
      !Object.hasOwn(Object.getOwnPropertyDescriptor(value, key), "value"))) {
    throw invalid("invalid IPC fields");
  }
  return keys;
}

function exactKeys(value, expected) {
  const keys = ownDataKeys(value);
  if (keys.length !== expected.length || keys.some((key) => !expected.includes(key))) {
    throw invalid("invalid IPC fields");
  }
}

function snapshotEnvironment(env) {
  const snapshot = Object.create(null);
  for (const key of Object.keys(env)) {
    const value = env[key];
    if (!key || key.includes("=") || key.includes("\0") ||
        typeof value !== "string" || value.includes("\0")) {
      throw invalid("invalid environment");
    }
    snapshot[key] = value;
  }
  return Object.freeze(snapshot);
}

function sameEnvironment(actual, seed, allowIdentities = false) {
  const actualKeys = Object.keys(actual);
  const expectedKeys = Object.keys(seed);
  const relevant = (key) => !allowIdentities || !IDENTITIES.has(key);
  if (actualKeys.filter(relevant).length !== expectedKeys.filter(relevant).length ||
      actualKeys.some((key) => relevant(key) &&
        (!Object.hasOwn(seed, key) || actual[key] !== seed[key]))) {
    throw invalid("immutable environment changed");
  }
}

function validateSeed(env) {
  if (env.OPENCLAW_AGENTCORE_PREINIT !== "1" || env.OPENCLAW_NO_RESPAWN !== "1" ||
      env.NODE_DISABLE_COMPILE_CACHE !== "1") {
    throw invalid("same-process preinit controls are required");
  }
  if (env.USER_ID || env.INTERNAL_USER_ID) throw invalid("seed is already bound");
  if (env.AWS_EC2_METADATA_DISABLED !== "true") {
    throw invalid("implicit instance metadata credentials must be disabled");
  }
  if (CREDENTIAL_SOURCES.some((key) => Object.hasOwn(env, key))) {
    throw invalid("credential sources are forbidden in the seed");
  }
  for (const key of STABLE_PATHS) {
    if (!identifier(env[key]) || !path.posix.isAbsolute(env[key]) ||
        path.posix.normalize(env[key]) !== env[key]) {
      throw invalid("stable absolute paths are required");
    }
  }
  if (!identifier(env.PATH) || env.PATH.split(":").some((entry) => !path.posix.isAbsolute(entry))) {
    throw invalid("stable absolute PATH is required");
  }
  for (const key of ["OPENCLAW_PROFILE", "OPENCLAW_CONTAINER"]) {
    if (Object.hasOwn(env, key)) throw invalid("profile and container selectors are forbidden");
  }
  if (env.OPENCLAW_PACKAGE_DIR !== undefined && env.OPENCLAW_PACKAGE_DIR !== PACKAGE_ROOT) {
    throw invalid("package selector does not match the pinned root");
  }
  if (env.OPENCLAW_HOME !== undefined && env.OPENCLAW_HOME !== env.HOME) {
    throw invalid("home selector does not match HOME");
  }
  if (env.AWS_CONFIG_FILE !== undefined &&
      (!path.posix.isAbsolute(env.AWS_CONFIG_FILE) ||
        path.posix.normalize(env.AWS_CONFIG_FILE) !== env.AWS_CONFIG_FILE)) {
    throw invalid("credential configuration path must be stable and absolute");
  }
}

function validateArgv(argv) {
  if (!Array.isArray(argv) || argv[2] !== "gateway" || argv[3] !== "run") {
    throw invalid("only gateway run is supported");
  }
  const seen = new Set();
  for (let index = 4; index < argv.length; index += 1) {
    const option = argv[index];
    if (seen.has(option)) throw invalid("duplicate gateway option");
    seen.add(option);
    if (option === "--verbose") continue;
    if (option === "--port") {
      const port = argv[++index];
      if (typeof port === "string" && /^[1-9]\d{0,4}$/u.test(port) && Number(port) <= 65535) continue;
    } else if (option === "--bind" && argv[++index] === "loopback") continue;
    throw invalid("unsupported gateway option or selector");
  }
}

function validateActivation(message, seed) {
  exactKeys(message, ["type", "generation", "binding", "env"]);
  if (message.type !== "openclaw:activate" || !identifier(message.generation)) {
    throw invalid("invalid activation envelope");
  }
  exactKeys(message.binding, ["userId", "actorId", "routingGeneration", "runtimeSessionId"]);
  const binding = message.binding;
  if (!identifier(binding.userId) || !identifier(binding.actorId) ||
      !(binding.routingGeneration === null ||
        (Number.isSafeInteger(binding.routingGeneration) && binding.routingGeneration >= 0)) ||
      !(binding.runtimeSessionId === null || identifier(binding.runtimeSessionId))) {
    throw invalid("invalid activation binding");
  }
  exactKeys(message.env, [...new Set([...Object.keys(seed), ...IDENTITIES])]);
  const env = snapshotEnvironment(message.env);
  sameEnvironment(env, seed, true);
  if (env.USER_ID !== binding.actorId || env.INTERNAL_USER_ID !== binding.userId) {
    throw invalid("activation identities do not match binding");
  }
  // Keep no caller-owned objects across asynchronous IPC sends.
  return Object.freeze({
    generation: message.generation, actorId: binding.actorId, userId: binding.userId,
  });
}

// A rejected attempt also consumes the one-shot preparation.
export function createGatewayPreparer({
  child = process,
  readFile = readFileSync,
  realpath = realpathSync,
  importModule = (url) => import(url),
  createInvariant = createInvariantPrewarmer,
} = {}) {
  let attempted = false;
  return async function prepareGateway(launcherUrl) {
    if (attempted) throw invalid("preparation is one-shot");
    attempted = true;
    if (typeof child.send !== "function" || child.connected !== true) {
      throw invalid("connected parent IPC is required");
    }
    validateSeed(child.env);
    const seed = snapshotEnvironment(child.env);
    const argv = Object.freeze([...child.argv]);
    validateArgv(argv);
    if (launcherUrl !== LAUNCHER_URL ||
        realpath(PACKAGE_ROOT) !== PACKAGE_ROOT ||
        realpath(`${PACKAGE_ROOT}/openclaw.mjs`) !== `${PACKAGE_ROOT}/openclaw.mjs` ||
        realpath(argv[1]) !== `${PACKAGE_ROOT}/openclaw.mjs`) {
      throw invalid("launcher must use the fixed package root");
    }
    let manifest;
    try {
      manifest = JSON.parse(readFile(`${PACKAGE_ROOT}/package.json`, "utf8"));
    } catch {
      throw invalid("invalid package metadata");
    }
    if (manifest?.name !== "openclaw" || manifest.version !== OPENCLAW_VERSION) {
      throw invalid("unsupported package version");
    }

    let state = "preparing";
    const invariant = createInvariant();
    let failure;
    let rejectFailure;
    let resolveActivation;
    const failed = new Promise((_, reject) => { rejectFailure = reject; });
    // Disconnect may race a queued import before guarded() installs its race.
    failed.catch(() => {});
    const activation = new Promise((resolve) => { resolveActivation = resolve; });
    const fail = (error) => {
      failure ||= error;
      rejectFailure(failure);
    };
    const assertLive = () => {
      if (failure) throw failure;
      if (child.connected !== true) throw invalid("parent disconnected");
      if (child.argv.length !== argv.length || child.argv.some((value, index) => value !== argv[index])) {
        throw invalid("gateway arguments changed");
      }
    };
    const guarded = async (work) => {
      const result = await Promise.race([
        Promise.resolve().then(() => { assertLive(); return work(); }), failed,
      ]);
      assertLive();
      return result;
    };
    const onDisconnect = () => fail(invalid("parent disconnected"));
    const onError = () => fail(invalid("parent IPC failed"));
    const onMessage = (message) => {
      if (state !== "waiting") return fail(invalid("unexpected or repeated activation"));
      try {
        sameEnvironment(child.env, seed);
        const validated = validateActivation(message, seed);
        state = "activating";
        resolveActivation(validated);
      } catch {
        fail(invalid("activation rejected"));
      }
    };
    const send = (message) => guarded(() => new Promise((resolve, reject) => {
      try {
        // A false return is backpressure, not failure; completion is callback-owned.
        if (message.type === "openclaw:prepared") state = "waiting";
        child.send(message, (error) => error ? reject(invalid("parent IPC send failed")) : resolve());
      } catch {
        reject(invalid("parent IPC send failed"));
      }
    }));
    const cleanup = () => {
      child.removeListener("message", onMessage);
      child.removeListener("disconnect", onDisconnect);
      child.removeListener("error", onError);
    };
    child.on("message", onMessage);
    child.on("disconnect", onDisconnect);
    child.on("error", onError);
    try {
      await guarded(() => importModule(new URL(RUNTIME_FILES.server, launcherUrl).href));
      const reply = await guarded(() => importModule(new URL(RUNTIME_FILES.reply, launcherUrl).href));
      if (typeof reply.prewarmConfigDrivenReplyRuntime !== "function") {
        throw invalid("pinned reply import prewarm is missing");
      }
      await guarded(() => reply.prewarmConfigDrivenReplyRuntime());
      await invariant.prepare(seed, launcherUrl, guarded, importModule);
      const presence = await guarded(() => importModule(new URL(RUNTIME_FILES.presence, launcherUrl).href));
      if (typeof presence.resetAgentCoreSnapshotPresence !== "function") {
        throw invalid("pinned presence reset patch is missing");
      }
      sameEnvironment(child.env, seed);
      invariant.proof();
      await send({ type: "openclaw:prepared", version: OPENCLAW_VERSION });
      // No bootstrap-relative deadline survives the snapshot. The parent owns it.
      const bound = await guarded(() => activation);
      sameEnvironment(child.env, seed);
      child.env.USER_ID = bound.actorId;
      child.env.INTERNAL_USER_ID = bound.userId;
      await guarded(() => presence.resetAgentCoreSnapshotPresence());
      await invariant.bind(bound.generation, child.env, guarded);
      sameEnvironment(child.env, seed, true);
      if (child.env.USER_ID !== bound.actorId || child.env.INTERNAL_USER_ID !== bound.userId) {
        throw invalid("bound identities changed");
      }
      await send({ type: "openclaw:activated", generation: bound.generation });
      assertLive();
      sameEnvironment(child.env, seed, true);
      if (child.env.USER_ID !== bound.actorId || child.env.INTERNAL_USER_ID !== bound.userId) {
        throw invalid("bound identities changed");
      }
      // Descendant CLI commands have no activation channel.
      delete child.env.OPENCLAW_AGENTCORE_PREINIT;
      cleanup();
      // Reject activation replay even after the CLI starts.
      const releaseReplayGuard = () => {
        child.removeListener("message", rejectReplay);
        child.removeListener("exit", releaseReplayGuard);
      };
      const rejectReplay = () => {
        releaseReplayGuard();
        child.exitCode = 1;
        child.exit(1);
      };
      child.on("message", rejectReplay);
      child.once("exit", releaseReplayGuard);
      state = "activated";
    } catch {
      state = "failed";
      cleanup();
      await invariant.close();
      throw invalid("preparation or activation failed");
    }
  };
}

export const prepareGateway = createGatewayPreparer();

if (process.env.OPENCLAW_AGENTCORE_PREINIT === "1") {
  await prepareGateway(LAUNCHER_URL);
}
