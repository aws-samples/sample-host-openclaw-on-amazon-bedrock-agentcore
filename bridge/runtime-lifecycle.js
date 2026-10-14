/**
 * Bootstrap, user binding, and turn admission for the bridge in AgentCore Runtime.
 *
 * Bootstrap health is separate from user readiness: activation fixes one user,
 * actor, routing generation, and session binding, then waits for every required
 * resource. Turns run serially against that binding and check their deadlines
 * before and after activation. Recovery suspends readiness without rebinding.
 *
 * Activation or recovery failure closes admission and starts bounded cleanup;
 * work that settles late triggers cleanup again for resources acquired later.
 * UUIDs read OS entropy per call rather than reusing random state from a snapshot.
 */
const fs = require("node:fs");

function osRandomUUID() {
  // Read per call: snapshots can preserve OpenSSL DRBG state and UUID caches.
  const bytes = Buffer.alloc(16);
  const fd = fs.openSync("/dev/urandom", "r");
  try {
    for (let offset = 0; offset < bytes.length;) {
      const count = fs.readSync(fd, bytes, offset, bytes.length - offset, null);
      if (!Number.isInteger(count) || count <= 0 || count > bytes.length - offset) {
        throw new Error("OS entropy read was incomplete");
      }
      offset += count;
    }
  } finally {
    fs.closeSync(fd);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = bytes.toString("hex");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

const REQUIRED_RESOURCES = Object.freeze([
  "restore", "credentials", "proxy", "gateway", "browser", "skills", "persistence",
]);
const BINDING_KEYS = ["userId", "actorId", "routingGeneration", "runtimeSessionId"];

function createRuntimeLifecycle({
  bootstrap = async () => {},
  activate,
  cleanup = async () => {},
  activationTimeoutMs = 300000,
  cleanupTimeoutMs = 10000,
  now = Date.now,
} = {}) {
  let state = "BOOTSTRAPPING";
  let binding = null;
  let bootstrapPromise;
  let activationPromise;
  let recoveryPromise;
  let controller;
  let generation;
  let failure;
  let cleanupPromise;
  let tail = Promise.resolve();
  const ready = new Set();

  function checkpoint() {
    if (controller?.signal.aborted) throw controller.signal.reason;
    if (state === "FAILED" || state === "DRAINING") throw failure || new Error("AgentCore Runtime is draining");
  }

  function fail(error) {
    failure ||= error;
    state = "FAILED";
    controller?.abort(failure);
    cleanupPromise ||= (async () => {
      let timer;
      try {
        await Promise.race([
          Promise.resolve().then(() => cleanup()),
          new Promise((resolve) => { timer = setTimeout(resolve, cleanupTimeoutMs); }),
        ]);
      } catch {
        // Admission remains closed even when cleanup is incomplete.
      } finally {
        clearTimeout(timer);
      }
    })();
    return cleanupPromise;
  }

  function start() {
    bootstrapPromise ||= Promise.resolve().then(bootstrap).then(() => {
      checkpoint();
      state = "BOOTSTRAP_READY";
    }).catch(async (error) => {
      await fail(error);
      throw error;
    });
    return bootstrapPromise;
  }

  function assertBinding(context) {
    if (typeof context.userId !== "string" || !context.userId ||
        typeof context.actorId !== "string" || !context.actorId) {
      throw new Error("Missing user binding");
    }
    if (binding && BINDING_KEYS.some((key) => (binding[key] ?? null) !== (context[key] ?? null))) {
      throw new Error("Request does not match the active user binding");
    }
  }

  async function ensureReady(context) {
    assertBinding(context);
    await start();
    checkpoint();
    assertBinding(context);
    if (state === "USER_READY") return;
    if (state === "RECOVERING") return recoveryPromise;
    if (activationPromise) return activationPromise;
    binding = Object.freeze(Object.fromEntries(BINDING_KEYS.map((key) => [key, context[key] ?? null])));
    generation = osRandomUUID(); // Session-scoped; correlate with runtimeSessionId.
    controller = new AbortController();
    state = "BINDING";
    const scope = Object.freeze({
      generation,
      signal: controller.signal,
      checkpoint,
      mark(name) {
        checkpoint();
        if (!REQUIRED_RESOURCES.includes(name)) throw new Error(`Unknown resource: ${name}`);
        ready.add(name);
      },
    });
    let timer;
    let workSettled = false;
    const work = Promise.resolve().then(() => activate(Object.freeze({ ...context }), scope))
      .finally(() => { workSettled = true; });
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => {
        const error = new Error("User activation deadline exceeded");
        controller.abort(error);
        reject(error);
      }, activationTimeoutMs);
    });
    activationPromise = Promise.race([work, timeout]).then(() => {
      checkpoint();
      const missing = REQUIRED_RESOURCES.filter((name) => !ready.has(name));
      if (missing.length) throw new Error(`Missing required resources: ${missing.join(", ")}`);
      state = "USER_READY";
    }).catch(async (error) => {
      const pendingAtFailure = !workSettled;
      await fail(error);
      // Late SDK completion may have acquired a resource after the first cleanup.
      if (pendingAtFailure) work.finally(() => cleanup()).catch(() => {});
      throw error;
    }).finally(() => clearTimeout(timer));
    return activationPromise;
  }

  function run(context, execute) {
    const turn = Object.freeze({ ...context, engine: "openclaw" });
    let expired = false;
    let timer;
    const work = tail.then(async () => {
      clearTimeout(timer);
      // Expired while queued: the caller already has its error, so never dispatch.
      if (expired) throw new Error("Turn deadline exceeded in queue");
      assertBinding(turn);
      checkpoint();
      if (now() >= turn.deadline) throw new Error("Turn deadline exceeded in queue");
      await ensureReady(turn);
      checkpoint();
      if (now() >= turn.deadline) throw new Error("Turn deadline exceeded during activation");
      return execute(turn);
    });
    // Later turns still wait for the earlier turn, not for this one's expiry.
    tail = work.catch(() => {});
    if (!Number.isFinite(turn.deadline)) return work;
    // Reject at the deadline while queued, so the caller gets its reply before it stops
    // listening. Once the turn starts, the timer is cleared and the turn's own bounds apply.
    const expiry = new Promise((_, reject) => {
      timer = setTimeout(() => {
        expired = true;
        reject(new Error("Turn deadline exceeded in queue"));
      }, Math.max(0, turn.deadline - now()));
    });
    return Promise.race([work, expiry]);
  }

  function health(activeTasks = 0) {
    const unavailable = ["BOOTSTRAPPING", "FAILED", "DRAINING"].includes(state);
    const busy = activeTasks > 0 || ["BINDING", "RECOVERING"].includes(state);
    return {
      statusCode: unavailable ? 503 : 200,
      body: { status: unavailable ? "Unhealthy" : busy ? "HealthyBusy" : "Healthy" },
    };
  }

  function recover(execute, timeoutMs = activationTimeoutMs) {
    checkpoint();
    if (recoveryPromise) return recoveryPromise;
    state = "RECOVERING";
    let settled = false;
    let timer;
    const work = Promise.resolve().then(execute).finally(() => { settled = true; });
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("Resource recovery deadline exceeded")), Math.max(1, timeoutMs));
    });
    recoveryPromise = Promise.race([work, timeout]).then(() => {
      checkpoint();
      state = "USER_READY";
    }).catch(async (error) => {
      const pendingAtFailure = !settled;
      await fail(error);
      if (pendingAtFailure) work.finally(() => cleanup()).catch(() => {});
      throw error;
    }).finally(() => {
      clearTimeout(timer);
      recoveryPromise = null;
    });
    return recoveryPromise;
  }

  return {
    start, ensureReady, run, recover, fail, health, assertBinding, checkpoint,
    snapshot: () => ({ state, binding, generation, resources: [...ready] }),
    drain() {
      state = "DRAINING";
      controller?.abort(new Error("AgentCore Runtime is draining"));
    },
  };
}

module.exports = { createRuntimeLifecycle, REQUIRED_RESOURCES, osRandomUUID };
