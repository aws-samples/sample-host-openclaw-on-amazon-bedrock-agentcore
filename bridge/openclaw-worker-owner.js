/**
 * Keep the V2 gateway's prepared module graph in its serving process.
 * The parent owns deadlines and permits activation only after user restore.
 */
const { spawn: spawnChild } = require("node:child_process");

const CONTROL_ENV = Object.freeze({
  OPENCLAW_AGENTCORE_PREINIT: "1",
  OPENCLAW_NO_RESPAWN: "1",
  NODE_DISABLE_COMPILE_CACHE: "1",
  OPENCLAW_AGENTCORE_SQLITE_RECEIVER_PREPARE: "1",
  OPENCLAW_AGENTCORE_SQLITE_BACKEND_PREPARE: "1",
});
const IDENTITY_ENV = ["USER_ID", "INTERNAL_USER_ID"];
const FORBIDDEN_ENV = [
  "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
  "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
  "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
  "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "AWS_ROLE_SESSION_NAME",
  "AWS_SHARED_CREDENTIALS_FILE", "AWS_PROFILE",
];
const BINDING_KEYS = ["userId", "actorId", "routingGeneration", "runtimeSessionId"];

function createPreparedGateway({
  env, port = 18789, onChild = () => {}, onFailure = () => {},
  prepareTimeoutMs = 120000, spawn = spawnChild,
}) {
  if (!env || !Number.isFinite(prepareTimeoutMs) || prepareTimeoutMs <= 0 ||
      !Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error("Invalid prepared gateway configuration");
  }
  const seed = Object.freeze({ ...env, ...CONTROL_ENV });
  if (IDENTITY_ENV.concat(FORBIDDEN_ENV).some((key) => Object.hasOwn(seed, key))) {
    throw new Error("Prepared gateway environment contains identity or credentials");
  }
  if (Object.values(seed).some((value) => typeof value !== "string")) {
    throw new Error("Prepared gateway environment must contain strings");
  }

  let child = null;
  let state = "NEW";
  let preparePromise;
  let pending;
  let activationGeneration;

  function clearPending(error) {
    if (!pending) return;
    const current = pending;
    pending = null;
    clearTimeout(current.timer);
    current.signal?.removeEventListener("abort", current.abort);
    if (error) current.reject(error);
    else current.resolve();
  }

  function detach() {
    child?.removeListener("message", onMessage);
    child?.removeListener("error", onError);
    child?.removeListener("exit", onExit);
    child?.removeListener("disconnect", onDisconnect);
  }

  function terminate() {
    if (child && child.exitCode === null && child.signalCode === null) {
      try { child.kill("SIGTERM"); } catch {}
    }
  }

  function fail(error) {
    if (state === "FAILED" || state === "STOPPED") return;
    state = "FAILED";
    detach();
    clearPending(error);
    terminate();
    onFailure(error);
  }

  function onError() { fail(new Error("Prepared gateway process failed")); }
  function onExit() { fail(new Error("Prepared gateway exited before activation")); }
  function onDisconnect() { fail(new Error("Prepared gateway IPC disconnected")); }

  function onMessage(message) {
    if (state === "PREPARING" && message?.type === "openclaw:prepared" &&
        message.version === "2026.9.5") {
      state = "PREPARED";
      clearPending();
    } else if (state === "ACTIVATING" && message?.type === "openclaw:activated" &&
        message.generation === activationGeneration) {
      state = "ACTIVE";
      clearPending();
      // The contract's normal child observer owns the running gateway.
      detach();
    } else {
      fail(new Error("Unexpected prepared gateway acknowledgement"));
    }
  }

  function waitForAck(timeoutMs, signal) {
    return new Promise((resolve, reject) => {
      const abort = () => fail(new Error("Prepared gateway activation aborted"));
      pending = {
        resolve, reject, signal, abort,
        timer: setTimeout(() => fail(new Error("Prepared gateway acknowledgement timed out")), timeoutMs),
      };
      if (signal) {
        signal.addEventListener("abort", abort, { once: true });
        if (signal.aborted) abort();
      }
    });
  }

  function prepare() {
    if (state === "FAILED" || state === "STOPPED") {
      return Promise.reject(new Error("Prepared gateway is unavailable"));
    }
    if (preparePromise) return preparePromise;
    if (state !== "NEW") return Promise.reject(new Error("Prepared gateway is unavailable"));
    state = "PREPARING";
    preparePromise = waitForAck(prepareTimeoutMs);
    try {
      child = spawn(process.execPath, [
        "--import", `${__dirname}/openclaw-preinit.mjs`,
        "/usr/local/lib/node_modules/openclaw/openclaw.mjs",
        "gateway", "run", "--port", String(port), "--verbose",
      ], {
        env: seed, stdio: ["ignore", "pipe", "pipe", "ipc"],
      });
      child.on("message", onMessage);
      child.on("error", onError);
      child.on("exit", onExit);
      child.on("disconnect", onDisconnect);
      onChild(child);
    } catch (error) {
      fail(error);
    }
    return preparePromise;
  }

  async function activate({ env: finalEnv, binding, generation, signal, deadline }) {
    try {
      if (state !== "PREPARED" || !child?.connected) {
        throw new Error("Gateway must be prepared before activation");
      }
      if (!binding || typeof binding.userId !== "string" || !binding.userId ||
          typeof binding.actorId !== "string" || !binding.actorId ||
          typeof generation !== "string" || !generation) {
        throw new Error("Prepared gateway requires a user binding and generation");
      }
      const activationEnv = { ...finalEnv, ...CONTROL_ENV };
      const keys = new Set([...Object.keys(seed), ...Object.keys(activationEnv)]);
      for (const key of keys) {
        if (!IDENTITY_ENV.includes(key) && seed[key] !== activationEnv[key]) {
          throw new Error(`Prepared gateway environment changed: ${key}`);
        }
      }
      if (activationEnv.USER_ID !== binding.actorId ||
          activationEnv.INTERNAL_USER_ID !== binding.userId) {
        throw new Error("Prepared gateway identity does not match its binding");
      }
      const remaining = deadline - Date.now();
      if (!Number.isFinite(remaining) || remaining <= 0 || signal?.aborted) {
        throw new Error("Prepared gateway activation deadline elapsed or aborted");
      }
      activationGeneration = generation;
      state = "ACTIVATING";
      const acknowledgement = waitForAck(remaining, signal);
      if (state === "ACTIVATING") {
        try {
          child.send({
            type: "openclaw:activate", generation,
            binding: Object.fromEntries(BINDING_KEYS.map((key) => [key, binding[key] ?? null])),
            env: activationEnv,
          }, (error) => {
            if (error) fail(new Error("Prepared gateway activation could not be delivered"));
          });
        } catch {
          fail(new Error("Prepared gateway activation could not be delivered"));
        }
      }
      await acknowledgement;
    } catch (error) {
      fail(error);
      throw error;
    }
  }

  function stop() {
    if (state === "STOPPED") return;
    state = "STOPPED";
    detach();
    clearPending(new Error("Prepared gateway stopped"));
    terminate();
  }

  return { prepare, activate, stop, get child() { return child; } };
}

module.exports = { createPreparedGateway };
