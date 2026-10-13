/**
 * Reserve a neutral Git worker for the live gateway's first session baseline.
 *
 * Private owner callbacks authorize preparation and later resume for the
 * same process, seed and binding generation. The bootstrap loads modules
 * with no task dispatched; resume leaves the reservation waiting for the
 * ordinary checkout.baseline worker factory to claim it exactly once.
 *
 * Server close and lease release dispose of an unused reservation. After
 * claim, the ordinary worker pool owns its lifetime, so reservation cleanup
 * no longer terminates it.
 */
import fs from "node:fs";
import { Worker, MessageChannel, isMainThread } from "node:worker_threads";
import { randomUUID } from "node:crypto";
import { isDeepStrictEqual } from "node:util";

export const SESSION_DIFF_WORKER_FLAG = "OPENCLAW_AGENTCORE_SESSION_DIFF_WORKER_PREPARE";
const workerUrl = "file:///usr/local/lib/node_modules/openclaw/dist/infra/git-operation.worker.js";
const bootstrapUrl = new URL("./session-diff-worker-bootstrap.mjs", import.meta.url);
const protocol = "openclaw.session-diff-worker-control.v1";
const immutableKeys = ["PATH", "LANG", "TZ", "CI", "DO_NOT_TRACK", "NO_COLOR", "HOME",
  "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH", "AWS_EC2_METADATA_DISABLED", "NODE_DISABLE_COMPILE_CACHE"];
const uuid = value => typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
const check = (value, reason) => { if (!value) throw new Error(reason); };
let reservation;

export function readSessionDiffWorkerFlag(value) {
  check(value === undefined || value === "0" || value === "1", "session_diff_worker_flag");
  return value === "1";
}

function processStart() {
  const text = fs.readFileSync("/proc/self/stat", "utf8");
  return text.slice(text.lastIndexOf(")") + 2).split(" ")[19];
}
function neutralEnvironment() {
  for (const key of Object.keys(process.env)) {
    check(!/^(?:USER_ID|INTERNAL_USER_ID|CHANNEL|NODE_OPTIONS|VITEST(?:_.*)?|OPENCLAW_AGENTCORE_PREINIT|AWS_(?:ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN|ACCESS_KEY|SECRET_KEY|SECURITY_TOKEN|PROFILE|DEFAULT_PROFILE|CONFIG_FILE|SHARED_CREDENTIALS_FILE|SDK_LOAD_CONFIG|WEB_IDENTITY_TOKEN_FILE|ROLE_ARN|ROLE_SESSION_NAME|CONTAINER_.*)|OPENAI_API_KEY|ANTHROPIC_API_KEY|GIT_.*)$/.test(key),
      "session_diff_worker_non_neutral");
  }
  check(process.env.AWS_EC2_METADATA_DISABLED === "true", "session_diff_worker_metadata_enabled");
  for (const key of ["HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH"])
    check(typeof process.env[key] === "string" && process.env[key].startsWith("/") &&
      !process.env[key].includes("\0"), "session_diff_worker_neutral_paths");
  check(process.env.OPENCLAW_STATE_DIR === process.env.HOME + "/.openclaw" &&
    process.env.OPENCLAW_CONFIG_PATH === process.env.OPENCLAW_STATE_DIR + "/openclaw.json",
  "session_diff_worker_neutral_paths");
  return Object.fromEntries(immutableKeys.filter(key => process.env[key] !== undefined)
    .map(key => [key, process.env[key]]));
}
function ownerValid(owner) {
  return owner && Object.keys(owner).sort().join(",") === "pid,processStart,seedId" &&
    uuid(owner.seedId) && owner.pid === process.pid &&
    typeof owner.processStart === "string" && /^\d+$/.test(owner.processStart);
}

// The ordinary owner's private closures are the only authority for preparation and adoption.
export function createSessionDiffWorkerPreparation(assertPreparingUnbound, getBoundLineage) {
  check(isMainThread && typeof assertPreparingUnbound === "function" &&
    typeof getBoundLineage === "function", "session_diff_worker_owner_guard");
  let state = "new", identity, environment, worker, port, gate, pending, latch, retirement;
  let failure, exited = false, exitCode = null, workerThreadId = null, generation = null;
  const facts = () => ({ seed_id: identity?.seedId ?? null, gateway_pid: process.pid,
    gateway_process_start: identity?.gatewayStart ?? null, preparation_id: identity?.preparationId ?? null,
    worker_thread_id: workerThreadId, generation });
  function stableEnvironment() {
    check(environment && readSessionDiffWorkerFlag(process.env[SESSION_DIFF_WORKER_FLAG]) &&
      immutableKeys.every(key => process.env[key] === environment[key]),
      "session_diff_worker_environment_changed");
  }
  function currentOwner(expectedGeneration) {
    const current = getBoundLineage();
    check(current && current.seedId === identity.seedId && current.pid === process.pid &&
      current.processStart === identity.gatewayStart && processStart() === identity.gatewayStart &&
      uuid(current.generation) && current.generation === expectedGeneration,
    "session_diff_worker_bound_identity");
    stableEnvironment();
    return current;
  }
  function settlePending(error, value) {
    const wait = pending;
    if (!wait) return;
    pending = null; clearTimeout(wait.timer);
    if (error) wait.reject(error); else wait.resolve(value);
  }
  function awaitReply(event, requestId) {
    check(!pending, "session_diff_worker_request_overlap");
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        failure ??= new Error("session_diff_worker_handshake_timeout");
        settlePending(failure);
      }, 10000);
      pending = { event, requestId, resolve, reject, timer };
    });
  }
  function lost(error) {
    failure ??= error;
    settlePending(failure);
    if (state !== "claimed" && state !== "retiring" && state !== "retired")
      void dispose().catch(() => {});
  }
  const unexpectedTaskOutput = () => lost(new Error("session_diff_worker_output_before_claim"));
  async function dispose() {
    if (state === "new") { state = "retired"; return { status: "retired", cleanup_confirmed: true }; }
    if (state === "claimed") return { status: "pool_owned", cleanup_confirmed: false };
    if (retirement) return retirement;
    state = "retiring";
    settlePending(failure ?? new Error("session_diff_worker_disposed"));
    retirement = (async () => {
      if (worker && !exited) await worker.terminate();
      check(!worker || exited && worker.threadId === -1, "session_diff_worker_cleanup_unconfirmed");
      port?.close(); state = "retired";
      if (reservation === api) reservation = undefined;
      return { status: "retired", cleanup_confirmed: true, exit_code: exitCode };
    })();
    return retirement;
  }
  const api = Object.freeze({
    attachLifetime(server, lock) {
      check(state === "new", "session_diff_worker_lifetime");
      for (const [owner, method] of [[server, "close"], [lock, "release"]]) {
        const original = owner?.[method];
        check(typeof original === "function", "session_diff_worker_lifetime");
        owner[method] = async function (...args) {
          await api.disposeUnused();
          return original.apply(this, args);
        };
      }
    },
    prepare({ enabled, seedId }) {
      check(typeof enabled === "boolean", "session_diff_worker_flag");
      if (!enabled) return Promise.resolve(Object.freeze({ status: "disabled" }));
      check(readSessionDiffWorkerFlag(process.env[SESSION_DIFF_WORKER_FLAG]), "session_diff_worker_flag");
      if (latch) {
        const owner = assertPreparingUnbound();
        check(ownerValid(owner) && owner.seedId === seedId && owner.processStart === identity?.gatewayStart,
          "session_diff_worker_owner_changed");
        return latch;
      }
      latch = (async () => {
        try {
          check(process.platform === "linux" && process.arch === "arm64", "session_diff_worker_platform");
          check(state === "new", "session_diff_worker_already_reserved");
          const owner = assertPreparingUnbound();
          check(ownerValid(owner) && owner.seedId === seedId && owner.processStart === processStart(),
            "session_diff_worker_owner_identity");
          check(!reservation, "session_diff_worker_already_reserved");
          environment = Object.freeze(neutralEnvironment());
          identity = Object.freeze({ seedId, gatewayPid: process.pid, gatewayStart: owner.processStart,
            preparationId: randomUUID() });
          reservation = api; state = "preparing";
          gate = new Int32Array(new SharedArrayBuffer(Int32Array.BYTES_PER_ELEMENT));
          const channel = new MessageChannel();
          port = channel.port1;
          port.on("message", message => {
            try {
              check(message && pending && message.schema === protocol && message.event === pending.event &&
                message.requestId === pending.requestId && message.status === "ok" &&
                Object.keys(identity).every(key => message[key] === identity[key]) &&
                Number.isSafeInteger(message.threadId) && message.threadId > 0 &&
                message.threadId === worker.threadId &&
                (workerThreadId === null || workerThreadId === message.threadId) &&
                message.generation === generation, "session_diff_worker_reply_identity");
              workerThreadId = message.threadId;
              settlePending(null, message);
            } catch (error) { lost(error); }
          });
          port.on("messageerror", () => lost(new Error("session_diff_worker_message_error")));
          worker = new Worker(bootstrapUrl, { execArgv: [], env: { ...environment },
            workerData: { schema: protocol, ...identity, gate: gate.buffer, control: channel.port2 },
            transferList: [channel.port2] });
          worker.on("message", unexpectedTaskOutput);
          worker.on("error", () => lost(new Error("session_diff_worker_error")));
          worker.on("messageerror", () => lost(new Error("session_diff_worker_message_error")));
          worker.once("exit", code => {
            exited = true; exitCode = code;
            if (state !== "retiring" && state !== "retired" && state !== "claimed")
              lost(new Error("session_diff_worker_lost"));
          });
          const reply = await awaitReply("module_ready", identity.preparationId);
          check(reply.task_listener_count === 1 && reply.task_dispatch_count === 0 &&
            reply.gate_state === 0, "session_diff_worker_module_ack");
          check(isDeepStrictEqual(assertPreparingUnbound(), owner), "session_diff_worker_owner_changed");
          stableEnvironment();
          check(!failure && !exited && Atomics.load(gate, 0) === 0, "session_diff_worker_lost");
          state = "prepared"; worker.unref(); port.unref();
          return Object.freeze({ status: "prepared", ...facts(), tasks_dispatched: 0 });
        } catch (error) {
          try { await dispose(); } catch (cleanup) {
            throw new AggregateError([error, cleanup], "session_diff_worker_prepare_cleanup_failed");
          }
          throw error;
        }
      })();
      return latch;
    },
    async resume({ generation: nextGeneration }) {
      try {
        check(state === "prepared" && !failure && !exited, "session_diff_worker_not_prepared");
        currentOwner(nextGeneration); generation = nextGeneration; state = "resuming";
        worker.ref(); port.ref();
        const requestId = randomUUID(), wait = awaitReply("resume_ready", requestId);
        port.postMessage({ schema: protocol, event: "resume", ...identity, generation, requestId });
        const reply = await wait;
        check(reply.gate_state === 1 && reply.task_dispatch_count === 0 &&
          Atomics.load(gate, 0) === 1 && !failure && !exited, "session_diff_worker_resume_ack");
        currentOwner(generation);
        state = "bound"; worker.unref(); port.unref();
        return Object.freeze({ status: "bound", ...facts(), tasks_dispatched: 0 });
      } catch (error) {
        try { await dispose(); } catch (cleanup) {
          throw new AggregateError([error, cleanup], "session_diff_worker_resume_cleanup_failed");
        }
        throw error;
      }
    },
    take(url, options) {
      if (state === "claimed") return null;
      if (state === "retired") {
        check(!worker || exited && worker.threadId === -1, "session_diff_worker_cleanup_unconfirmed");
        return null;
      }
      check(state === "bound" && !failure && !exited && worker.threadId === workerThreadId,
        "session_diff_worker_not_bound");
      currentOwner(generation);
      check(url?.href === workerUrl && Object.keys(options).length === 1 &&
        Array.isArray(options.execArgv) && options.execArgv.length === 0,
      "session_diff_worker_factory_options");
      check(Atomics.compareExchange(gate, 0, 1, 2) === 1, "session_diff_worker_claim_consumed");
      state = "claimed"; worker.ref(); port.close(); worker.removeListener("message", unexpectedTaskOutput);
      return worker;
    },
    dispose,
    disposeUnused: () => state === "claimed" ? undefined : dispose(),
    snapshot: () => Object.freeze({ state, ...facts(), exited, exit_code: exitCode }),
  });
  return api;
}

export function sessionDiffWorkerFactory(type) {
  if (type !== "checkout.baseline" || !readSessionDiffWorkerFlag(process.env[SESSION_DIFF_WORKER_FLAG]) ||
    !reservation) return undefined;
  const current = reservation;
  return Object.assign((url, options) => current.take(url, options),
    { dispose: current.dispose, disposeUnused: current.disposeUnused });
}
