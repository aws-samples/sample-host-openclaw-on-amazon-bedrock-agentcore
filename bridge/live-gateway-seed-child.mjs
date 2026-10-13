/**
 * CLI preload and native lock probe helper for a live gateway seed.
 *
 * The preload waits for parent IPC before allowing ordinary gateway startup,
 * then attaches to that gateway's server and lease. It prepares the pinned
 * chat module graph and session-diff worker while identity is absent, and
 * quiesces the lease heartbeat after startup has settled. Process, filesystem,
 * listener and admission checks accompany independent native lock probes
 * before and after both heartbeat quiescence and resumption.
 *
 * A validated bind installs the late environment and resumes the same lease
 * and reserved worker for one generation. Its acknowledgement keeps
 * userReady false so the Bridge still owns user admission. Protocol failures
 * and parent disconnects stop the child after reserved-worker cleanup.
 */
import { createSessionDiffWorkerPreparation } from "./session-diff-worker-prewarm.mjs";
import fs from "node:fs";
import { spawn, spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { isMainThread } from "node:worker_threads";
import { fileURLToPath } from "node:url";
import { randomUUID, createHash } from "node:crypto";
import { check, keys, equal, version, uuid, processWitness, listeners, privateWitness,
  configValid, prepareEnv, validateBind, sleep, symbol, vendor, safeReason, workspaceWitness, agentsValid,
  PROBE_PREPARED_FLAG, readProbePreparedFlag, probeClock, probeTiming,
  BIND_OBSERVER_ASYNC_FLAG, readBindObserverAsyncFlag,
  CHAT_PREPARE_FLAG, readChatPrepareFlag, assertChatNeutralEnv,
  SESSION_DIFF_WORKER_FLAG, readSessionDiffWorkerFlag,
  LIVE_EAGER_COMPILE_FLAG,
  readLiveEagerCompileFlag, assertLiveEagerCompileExecArgv } from "./live-gateway-seed-common.mjs";

export const CHAT_PREPARE_MODULES = Object.freeze([
  ["get-reply-from-config.runtime-uSvr2ey5.mjs", "a58da16a2a15c2457e90853fdeeb3fe6c52764a25a89d3e71a67d80d98a84b62"],
  ["get-reply-DN4OcavF.mjs", "5f631b689395c3f6bbc4c2c15f195afddf965c88271409cfec1b017d9a8a2976"],
  ["lazy-promise-DGqyc4Y4.mjs", "322c44814888bf48c38deb8498f0d78bc7fe5e794b71f961b5f2adfdd7bd295a"],
  ["embedded-agent.runtime-DO9-VwPR.mjs", "de8ade36e781a93632fc59874d2af44314e9d1027ec794441380436b7cb61b63"],
  ["embedded-agent-BEeEP6_K.mjs", "e1a39e81429d64de2800475fba80bf297d76acbd8885cdfc418d16faf68ff374"],
  ["agent-runner.runtime.js", "630b191db3e015c487805ff06c062ebd983b9cf1a553c6c7ddd8806272db6544"],
  ["agent-runner.runtime-DRp6jmuv.mjs", "f3f4732fc5eec0ded876b6e8db31abd06bf30b5a30c7cbd1094cd9c4e7f5c190"],
  ["session-updates.runtime.js", "c2b4e710ad386dc9128f87f2970eae1ab53c99157d2d68325202608e683524fd"],
  ["session-updates.runtime-C_WOcqlK.mjs", "351cd9c37815e866280ffafac91e987f7d5dc45af82c98ceb3038a3d828854bb"],
  ["session-updates-B-tZzfPq.mjs", "4cecae95510895327d1bd0de7a3d49bbacf0bd4d298b24198a4057edc1b592d7"],
  ["agents/agent-bundle-mcp-runtime.js", "c964813a1a83984f1f843bef38e2b223e335e2f9c0f58af8229337f958e3f238"],
  ["agent-bundle-mcp-manager-api-DswOxDdW.mjs", "1595df5752712b958619686c4f74828e6e72b39b2849a8db1bb67c2813eb5689"],
  ["chat-BuK_U07L.mjs", "b258c751dd539ac1a1f92bd1054ceef7872236ed269f218673bd3971144781d3"],
  ["chat-BQ5bS4bL.mjs", "9c57fcfbae8e69791d238e270b4182b03e2241af27f1090707491ce5350dc54f"],
  ["server-methods-C72cO74x.mjs", "248973d955c9fb03bcec2f9899c473b8ef35687f70979b13de7b7bd0f6860537"],
  ["gateway-request-scope-CVb8Vezy.mjs", "fef4d4538f0f873a20210983918266fabe412dc199eabbc7d9592e2d200630d8"],
  ["reply-run-registry.state-DJqQB_Mq.mjs", "cc34905af4370e17a01c10ce023951fb7f408752a5456f52b7f0f862e5bb706c"],
  ["workspace-default-B86Ou7E_.mjs", "4127150ca282a862b4e2d2cc58c47a6a257cedbf1228eed14e46429a51425646"],
  ["workspace-default-path-vJpCl3D3.mjs", "2811c61d2edca890c4f0504989453d9a2ca004ac34048238b225fcbf6314f75b"],
  ["plugin-instance-invocation-DhuKro1-.mjs", "78a5a719d029b8e3e1ffac4b1f9a1843222fb4c689e4fa0e7e8666ba4bcfa477"],
  ["agent-bundle-mcp-runtime-shared-BFBNCZj0.mjs", "546133eb5fd3f57a2e8abf9711f8e2951c638d7ef4a34fd56b7d2a50d6db5283"],
].map(Object.freeze));
const replyWitnessKey = Symbol.for("openclaw.liveSeedReplyRuntimeWitness");
let chatPreparationClaim;
function emptyReplyRegistry() {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, Symbol.for("openclaw.replyRunRegistry"));
  if (!descriptor) return undefined;
  check(Object.hasOwn(descriptor, "value"), "chat_registry_shape");
  const registry = descriptor.value;
  for (const name of ["activeRunsByKey", "activeSessionIdsByKey", "activeKeysBySessionId", "waitKeysBySessionId",
    "waitersByKey", "followupAdmissionBarriersByKey", "successorAdmissionBarriersByKey", "sourceTurnByKey"]) {
    const entry = Object.getOwnPropertyDescriptor(registry, name);
    check(entry && Object.hasOwn(entry, "value") && entry.value instanceof Map && entry.value.size === 0,
      "chat_registry_not_empty");
  }
  return registry;
}

// Called only from the ordinary owner's unbound startup closure. No graph import at definition time.
export function createLiveSeedChatPreparation(assertPreparingUnbound) {
  check(typeof assertPreparingUnbound === "function", "chat_owner_guard");
  let latch, ownerWitness, assertNoRequest;
  const claim = Object.freeze({});
  let replyPromises, capturing = false;
  const replyObserver = value => {
    check(capturing && !replyPromises && Array.isArray(value) && value.length === 3 &&
      value.every(p => p instanceof Promise) && new Set(value).size === 3, "chat_reply_witness_invalid");
    replyPromises = value;
  };
  const validateCaller = seedId => {
    check(isMainThread && uuid(seedId), "chat_owner_identity");
    const current = assertPreparingUnbound();
    keys(current, ["seedId", "pid", "processStart"]);
    check(current.seedId === seedId && current.pid === process.pid &&
      typeof current.processStart === "string" && /^\d+$/.test(current.processStart), "chat_owner_identity");
    if (ownerWitness) check(equal(current, ownerWitness), "chat_owner_changed");
    assertChatNeutralEnv(process.env);
    assertNoRequest?.();
    return Object.freeze({ ...current });
  };
  return function prepare({ enabled, seedId }) {
    check(typeof enabled === "boolean", "chat_prepare_flag");
    if (!enabled) return Promise.resolve(Object.freeze({ status: "disabled" }));
    if (latch) {
      try { validateCaller(seedId); } catch (error) { return Promise.reject(error); }
      return latch;
    }
    latch = Promise.resolve().then(async () => {
      try {
        ownerWitness = validateCaller(seedId);
        check(chatPreparationClaim === undefined || chatPreparationClaim === claim, "chat_prepare_already_started");
        chatPreparationClaim = claim;
        assertChatNeutralEnv(process.env);
        check(JSON.parse(fs.readFileSync(vendor + "../package.json", "utf8")).version === "2026.9.5",
          "chat_version_mismatch");
        for (const [file, digest] of CHAT_PREPARE_MODULES)
          check(createHash("sha256").update(fs.readFileSync(vendor + file)).digest("hex") === digest,
            "chat_pin_mismatch");
        const moduleSetSha256 = createHash("sha256").update(JSON.stringify(CHAT_PREPARE_MODULES)).digest("hex");
        const scope = await import(vendor + "gateway-request-scope-CVb8Vezy.mjs");
        const invocation = await import(vendor + "plugin-instance-invocation-DhuKro1-.mjs");
        check(typeof scope.c === "function" && typeof invocation.t === "function", "chat_export_mismatch");
        assertNoRequest = () => {
          check(scope.c() === undefined && invocation.t() === undefined, "chat_request_scope");
          check(!Object.hasOwn(globalThis, Symbol.for("openclaw.sessionMcpRuntimeManager")), "chat_mcp_manager_present");
        };
        assertNoRequest();
        const registry = emptyReplyRegistry();
        check(!Object.hasOwn(globalThis, replyWitnessKey), "chat_witness_present");
        Object.defineProperty(globalThis, replyWitnessKey, { value: replyObserver, configurable: true });
        const replyRuntime = await import(vendor + "get-reply-from-config.runtime-uSvr2ey5.mjs");
        check(typeof replyRuntime.prewarmConfigDrivenReplyRuntime === "function" &&
          replyRuntime.prewarmConfigDrivenReplyRuntime.length === 0, "chat_export_mismatch");
        capturing = true;
        try { await replyRuntime.prewarmConfigDrivenReplyRuntime(); } finally { capturing = false; }
        check(replyPromises, "chat_reply_witness_missing");
        const mcp = await import(vendor + "agents/agent-bundle-mcp-runtime.js");
        check(typeof mcp.createSessionMcpRuntime === "function" &&
          typeof mcp.createBundleMcpJsonSchemaValidator === "function", "chat_export_mismatch");
        const chat = await import(vendor + "chat-BuK_U07L.mjs");
        check(typeof chat.chatHandlers?.["chat.send"] === "function", "chat_export_mismatch");
        validateCaller(seedId); assertChatNeutralEnv(process.env); assertNoRequest();
        const afterRegistry = emptyReplyRegistry();
        check(registry === undefined || registry === afterRegistry, "chat_registry_changed");
        check(Object.getOwnPropertyDescriptor(globalThis, replyWitnessKey)?.value === replyObserver, "chat_witness_changed");
        return Object.freeze({ status: "prepared", moduleSetSha256, definitionsRetained: true,
          privateReplyPromisesCaptured: replyPromises.length });
      } finally {
        capturing = false; replyPromises = undefined;
        if (Object.getOwnPropertyDescriptor(globalThis, replyWitnessKey)?.value === replyObserver)
          delete globalThis[replyWitnessKey];
      }
    });
    return latch;
  };
}

export const PROBE_HELPER_SELECTOR = "--live-seed-native-probe-helper-v1";
export const PROOF_PHASES = Object.freeze(["prepare-before-quiesce", "prepare-after-quiesce",
  "bind-before-resume", "bind-after-resume"]);
const probeVersion = "openclaw.live-seed-native-probe.v1";
const identityKeys = ["seedId", "gatewayPid", "gatewayStart", "helperPid", "helperStart", "group"];
const requestKeys = ["type", "version", ...identityKeys, "requestId", "phase", "generation"];
const replyKeys = [...requestKeys, "outcome", "result_code", "error_code", "start_ns", "end_ns", "duration_ns"];
const errorCodes = new Set(["ETIMEDOUT", "ENOENT", "EACCES", "EPERM", "ENOMEM", "EAGAIN", "EMFILE", "ENFILE",
  "ERR_IPC_CHANNEL_CLOSED", "ERR_IPC_DISCONNECTED", "SQLITE_BUSY", "SQLITE_LOCKED",
  "PROTOCOL", "IDENTITY", "DISCONNECTED", "EXITED", "SEND_FAILED", "RELEASE_FAILED", "OTHER"]);
const signals = new Set(["SIGTERM", "SIGKILL", "SIGABRT", "SIGSEGV", "SIGBUS", "SIGILL", "SIGPIPE",
  "SIGINT", "SIGHUP", "SIGQUIT", "SIGSTOP", "SIGCONT", "SIGUSR1", "SIGUSR2", "SIGALRM", "SIGXCPU", "SIGXFSZ"]);
const errorCode = error => { try { return errorCodes.has(error?.code) ? error.code : "OTHER"; } catch { return "OTHER"; } };
const signalCode = value => value == null ? null : signals.has(value) ? value : "OTHER";
const ns = value => value === null || (typeof value === "string" && /^\d{1,30}$/.test(value));
const identityOf = value => Object.fromEntries(identityKeys.map(key => [key, value[key]]));

export function validateNativeProbeReply(reply, request) {
  keys(reply, replyKeys);
  check(reply.type === "result" && reply.version === probeVersion &&
    equal(identityOf(reply), identityOf(request)) && reply.requestId === request.requestId &&
    reply.phase === request.phase && reply.generation === request.generation, "native_exclusivity_unproven");
  check(["start_ns", "end_ns", "duration_ns"].every(key => ns(reply[key])) &&
    (reply.error_code === null || errorCodes.has(reply.error_code)), "native_exclusivity_unproven");
  const result = reply.outcome === "excluded" ? 0 : reply.outcome === "acquired" ? 3 :
    reply.outcome === "error" ? 4 : null;
  const outcomes = request.type === "init" ? ["ready", "error"] : request.type === "stop" ?
    ["stopped"] : ["excluded", "acquired", "error"];
  check(outcomes.includes(reply.outcome) && reply.result_code === result &&
    (reply.outcome === "error" || reply.error_code === null), "native_exclusivity_unproven");
}

export function acquireNativeProbeOutcome(acquire, databasePath) {
  let lock, outcome, result_code, error_code = null;
  try {
    lock = acquire({ databasePath, busyTimeoutMs: 0 });
    outcome = "acquired"; result_code = 3;
  } catch (error) {
    outcome = error.family === "gateway-lifecycle" ? "excluded" : "error";
    result_code = outcome === "excluded" ? 0 : 4;
    error_code = outcome === "excluded" ? null : errorCode(error);
  }
  if (lock) try {
    lock.release();
    check(lock.closed, "native_exclusivity_unproven");
  } catch { outcome = "error"; result_code = 4; error_code = "RELEASE_FAILED"; }
  return { outcome, result_code, error_code };
}

export function synchronousNativeProbe(databasePath, cwd) {
  const start = probeClock();
  // Flag zero retains the original independent process, arguments and native operation.
  const result = spawnSync(process.execPath, ["--input-type=module", "-e", `
      import { r as acquire } from ${JSON.stringify(vendor + "state-database-coordinator-BshfOa2T.mjs")};
      try {
        const lock = acquire({databasePath: process.argv[1], busyTimeoutMs: 0});
        lock.release(); process.exitCode = 3;
      } catch (error) {
        process.exitCode = error.family === "gateway-lifecycle" ? 0 : 4;
      }
    `, databasePath], { env: process.env, cwd, timeout: 10000, stdio: "ignore" });
  const end = probeClock();
  return { pid: Number.isSafeInteger(result.pid) && result.pid > 0 ? result.pid : null,
    result_code: result.status, spawn_status: result.status, spawn_signal: signalCode(result.signal),
    error_code: result.error ? errorCode(result.error) : null,
    outcome: result.error?.code === "ETIMEDOUT" ? "timeout" : result.status === 0 ? "excluded" :
      result.status === 3 ? "acquired" : "error",
    duration_ns: probeTiming(start, end).duration_ns, helper_request_ordinal: null, helper_import_prepared: false };
}

export function createNativeProbeClient({ seedId, databasePath, cwd, onFailure }) {
  check(uuid(seedId), "native_exclusivity_unproven");
  const gateway = processWitness(process.pid);
  let child, helper, pending, spawning, failure, stopping = false, prepared = false, ordinal = 0;
  let exitObserved = false, exitStatus = null, exitSignal = null, exitWait;
  const metadata = () => ({ pid: child?.pid ?? null, result_code: null, spawn_status: exitStatus,
    spawn_signal: signalCode(exitSignal), error_code: null, outcome: "error", duration_ns: null,
    helper_request_ordinal: ordinal || null, helper_import_prepared: prepared });
  function reject(code, extra = {}) {
    if (failure) return failure;
    failure = Object.assign(new Error("native_exclusivity_unproven"), {
      nativeProbe: { ...metadata(), error_code: code, outcome: code === "ETIMEDOUT" ? "timeout" : "error", ...extra },
    });
    const handled = !!pending || !!spawning || stopping;
    if (pending) { clearTimeout(pending.timer); pending.reject(failure); pending = null; }
    if (spawning) { clearTimeout(spawning.timer); spawning.reject(failure); spawning = null; }
    if (exitWait) { clearTimeout(exitWait.timer); exitWait.reject(failure); exitWait = null; }
    try { if (child && !exitObserved) child.kill("SIGTERM"); } catch {}
    if (!handled) try { onFailure?.(failure); } catch {}
    return failure;
  }
  function assertIdentity() {
    const parent = processWitness(process.pid), current = processWitness(child.pid);
    check(parent.start === gateway.start && parent.group === gateway.group &&
      current.start === helper.start && current.group === gateway.group && current.state !== "Z",
    "native_exclusivity_unproven");
  }
  function request(type, phase, generation, extra = {}) {
    if (failure) return Promise.reject(failure);
    try { check(!pending, "native_exclusivity_unproven"); assertIdentity(); }
    catch { return Promise.reject(reject("IDENTITY")); }
    const message = { type, version: probeVersion, seedId, requestId: randomUUID(),
      gatewayPid: process.pid, gatewayStart: gateway.start, helperPid: child.pid,
      helperStart: helper.start, group: gateway.group, phase, generation, ...extra };
    return new Promise((resolve, fail) => {
      pending = { request: message, resolve, reject: fail, timer: setTimeout(() => reject("ETIMEDOUT"), 10000) };
      try { child.send(message, error => { if (error) reject("SEND_FAILED"); }); }
      catch { reject("SEND_FAILED"); }
    });
  }
  return {
    get pid() { return child?.pid ?? null; },
    get exitEvidence() {
      return { helper_exit_observed: exitObserved, helper_exit_status: exitStatus,
        helper_exit_signal: signalCode(exitSignal) };
    },
    async start() {
      check(!child, "native_exclusivity_unproven");
      const spawned = new Promise((resolve, fail) => {
        spawning = { resolve, reject: fail, timer: setTimeout(() => reject("ETIMEDOUT"), 10000) };
      });
      // No parent/user environment or Node preloads; same existing owned process group.
      try {
        child = spawn(process.execPath, [fileURLToPath(import.meta.url), PROBE_HELPER_SELECTOR], {
          cwd, detached: false, env: { NODE_DISABLE_COMPILE_CACHE: "1", AWS_EC2_METADATA_DISABLED: "true" },
          stdio: ["ignore", "ignore", "ignore", "ipc"],
        });
        child.once("spawn", () => {
          try {
            helper = processWitness(child.pid); assertIdentity();
            if (!spawning) return;
            const item = spawning; spawning = null; clearTimeout(item.timer); item.resolve();
          } catch { reject("IDENTITY"); }
        });
        child.on("error", error => reject(errorCode(error)));
        child.on("disconnect", () => { if (!stopping) reject("DISCONNECTED"); });
        child.on("exit", (code, signal) => {
          exitObserved = true; exitStatus = code; exitSignal = signal;
          if (!stopping || code !== 0 || signal !== null) reject("EXITED");
          if (exitWait) { const item = exitWait; exitWait = null; clearTimeout(item.timer); item.resolve(); }
        });
        child.on("message", reply => {
          try {
            check(pending, "native_exclusivity_unproven");
            validateNativeProbeReply(reply, pending.request);
            // A clean stop ACK can arrive after its sender has already exited.
            if (pending.request.type !== "stop") assertIdentity();
            const item = pending; pending = null; clearTimeout(item.timer); item.resolve(reply);
          } catch { reject("PROTOCOL"); }
        });
      } catch (error) { reject(errorCode(error)); }
      await spawned;
      const reply = await request("init", "initialize", null, { databasePath });
      if (reply.outcome !== "ready") throw reject(reply.error_code ?? "OTHER");
      prepared = true;
    },
    async probe(phase, generation) {
      check(prepared && phase === PROOF_PHASES[ordinal], "native_exclusivity_unproven");
      ordinal++;
      const reply = await request("probe", phase, generation);
      const result = { ...metadata(), result_code: reply.result_code, error_code: reply.error_code,
        outcome: reply.outcome, helper_start_ns: reply.start_ns, helper_end_ns: reply.end_ns,
        helper_duration_ns: reply.duration_ns };
      if (reply.result_code !== 0) throw reject(reply.error_code ?? "OTHER", result);
      return result;
    },
    async stop(generation) {
      stopping = true;
      await request("stop", "stop", generation);
      if (!exitObserved) await new Promise((resolve, fail) => {
        exitWait = { resolve, reject: fail, timer: setTimeout(() => reject("ETIMEDOUT"), 10000) };
      });
      if (failure) throw failure;
      check(exitStatus === 0 && exitSignal === null, "native_exclusivity_unproven");
      return { helper_exit_observed: true, helper_exit_status: exitStatus, helper_exit_signal: exitSignal };
    },
    abort() { if (!exitObserved) { stopping = true; reject("DISCONNECTED"); } },
  };
}

async function runNativeProbeHelper() {
  check(isMainThread && process.argv.length === 3 && process.argv[2] === PROBE_HELPER_SELECTOR &&
    process.execArgv.length === 0 && typeof process.send === "function", "native_exclusivity_unproven");
  check(Object.keys(process.env).every(key => ["NODE_DISABLE_COMPILE_CACHE", "AWS_EC2_METADATA_DISABLED"].includes(key)) &&
    process.env.NODE_DISABLE_COMPILE_CACHE === "1" && process.env.AWS_EC2_METADATA_DISABLED === "true",
  "native_exclusivity_unproven");
  process.umask(0o077);
  const self = processWitness(process.pid), parent = processWitness(process.ppid), consumed = new Set();
  let identity, databasePath, acquire, busy = false, ordinal = 0, generation = null;
  const die = () => process.exit(2);
  const current = message => {
    const now = processWitness(process.pid), owner = processWitness(process.ppid);
    check(now.start === self.start && now.group === self.group && owner.pid === parent.pid &&
      owner.start === parent.start && owner.group === self.group && owner.state !== "Z" &&
      message.gatewayPid === parent.pid && message.gatewayStart === parent.start &&
      message.helperPid === self.pid && message.helperStart === self.start && message.group === self.group,
    "native_exclusivity_unproven");
  };
  process.on("disconnect", die);
  process.on("error", die);
  process.on("message", message => {
    void (async () => {
      keys(message, message.type === "init" ? [...requestKeys, "databasePath"] : requestKeys);
      check(!busy && consumed.size < 6 && message.version === probeVersion && uuid(message.seedId) &&
        uuid(message.requestId) && !consumed.has(message.requestId), "native_exclusivity_unproven");
      current(message);
      if (identity) check(equal(identityOf(message), identity), "native_exclusivity_unproven");
      busy = true; consumed.add(message.requestId);
      const start = probeClock();
      let outcome, result_code = null, error_code = null;
      if (message.type === "init") {
        check(!identity && message.phase === "initialize" && message.generation === null &&
          typeof message.databasePath === "string" && message.databasePath.startsWith("/") &&
          !message.databasePath.includes("\0"), "native_exclusivity_unproven");
        identity = identityOf(message); databasePath = message.databasePath;
        privateWitness(databasePath);
        try { ({ r: acquire } = await import(vendor + "state-database-coordinator-BshfOa2T.mjs")); outcome = "ready"; }
        catch (error) { outcome = "error"; result_code = 4; error_code = errorCode(error); }
      } else if (message.type === "probe") {
        check(acquire && message.phase === PROOF_PHASES[ordinal] &&
          (ordinal < 2 ? message.generation === null : uuid(message.generation) &&
            (generation === null || generation === message.generation)), "native_exclusivity_unproven");
        generation = message.generation; ordinal++;
        ({ outcome, result_code, error_code } = acquireNativeProbeOutcome(acquire, databasePath));
      } else {
        check(message.type === "stop" && identity && message.phase === "stop" &&
          message.generation === generation, "native_exclusivity_unproven");
        outcome = "stopped";
      }
      current(message);
      const timing = probeTiming(start);
      // Native work and release have finished before making another request admissible.
      if (outcome !== "stopped") busy = false;
      process.send({ type: "result", version: probeVersion, ...identity,
        requestId: message.requestId, phase: message.phase, generation: message.generation,
        outcome, result_code, error_code, ...timing }, error => {
        if (error) die();
        else if (outcome === "stopped") process.exit(0);
      });
    })().catch(die);
  });
}

// This preload gates only the ordinary CLI's main thread, never its native workers.
if (isMainThread && process.argv[1] === fileURLToPath(import.meta.url)) {
  await runNativeProbeHelper();
} else if (isMainThread && typeof process.send === "function") {
  process.umask(0o077);
  const require = createRequire(import.meta.url);
  const { waitForGateway } = require("./gateway-lifecycle.js");
  let state = "new", prepared, seedId, preparation, handle, generation = null, originalDb;
  const usePreparedProbe = readProbePreparedFlag(process.env[PROBE_PREPARED_FLAG]);
  const useAsyncBindObserver = readBindObserverAsyncFlag(process.env[BIND_OBSERVER_ASYNC_FLAG]);
  const liveEagerCompile = readLiveEagerCompileFlag(process.env[LIVE_EAGER_COMPILE_FLAG]);
  let probeHelper, prepareChat, sessionDiffWorker;
  let bindAdmission;
  const prepareSessionDiffWorker = readSessionDiffWorkerFlag(process.env[SESSION_DIFF_WORKER_FLAG]);
  const consumed = new Set();
  const original = processWitness(process.pid);
  const configPath = process.env.OPENCLAW_CONFIG_PATH;
  const startup = new Promise(resolve => { preparation = resolve; });
  const observedLayout = () => ({ home: process.env.HOME, stateDir: process.env.OPENCLAW_STATE_DIR,
    configPath, workspaceDir: process.env.OPENCLAW_STATE_DIR + "/workspace", cwd: process.cwd(),
    controlDir: prepared.seedRoot, port: handle.port, pid: process.pid, seedId });
  function liveEagerCompileProof() {
    check(liveEagerCompile === readLiveEagerCompileFlag(prepared.neutralEnv[LIVE_EAGER_COMPILE_FLAG]) &&
      liveEagerCompile === readLiveEagerCompileFlag(process.env[LIVE_EAGER_COMPILE_FLAG]) &&
      liveEagerCompile === assertLiveEagerCompileExecArgv(prepared.neutralEnv[LIVE_EAGER_COMPILE_FLAG]),
    "live_eager_compile_execution");
    return liveEagerCompile ? { liveEagerCompileExecArgv: true } : {};
  }
  function reply(message, status, reason = null, proof = {}) {
    if (process.connected) process.send({ type: "result", version, seedId,
      requestId: message.requestId, status, pid: process.pid, generation, reason, proof });
  }
  function fail(error) {
    if (state === "stopping") return;
    if (handle?.request) reply(handle.request, "fallback", safeReason(error));
    state = "stopping";
    stopAfterWorkerCleanup();
  }
  function stopAfterWorkerCleanup() {
    const stop = () => process.kill(process.pid, "SIGTERM");
    if (sessionDiffWorker) void sessionDiffWorker.dispose().then(stop, stop);
    else stop();
  }
  async function ready(settle = true) {
    await waitForGateway({ port: prepared.port, token: prepared.gatewayToken,
      deadline: Date.now() + 10000, intervalMs: 25 });
    if (settle) await sleep(50);
  }
  function assertExclusive() {
    check(state !== "stopping" && processWitness(process.pid).start === original.start &&
      process.cwd() === prepared.cwd, "process_identity");
    check(equal(privateWitness(prepared.seedRoot, true), prepared.rootWitness), "seed_root_changed");
    check(equal(privateWitness(prepared.home, true), prepared.homeWitness) &&
      process.env.HOME === prepared.home &&
      process.env.OPENCLAW_STATE_DIR === prepared.home + "/.openclaw" &&
      process.env.OPENCLAW_CONFIG_PATH === prepared.home + "/.openclaw/openclaw.json", "home_changed");
    check(equal(privateWitness(process.env.OPENCLAW_STATE_DIR, true), prepared.stateWitness), "state_root_changed");
    if (prepared.workspaceWitness) check(equal(workspaceWitness(prepared.home + "/.openclaw/workspace"),
      prepared.workspaceWitness), "workspace_changed");
    check(equal(JSON.parse(fs.readFileSync(configPath, "utf8")), prepared.renderedConfig), "config_changed");
    check(fs.readFileSync(prepared.home + "/.openclaw/AGENTS.md", "utf8") === prepared.agentsText, "agents_changed");
    handle.lock.seedOwnership.assertExclusive();
    if (originalDb) check(equal(privateWitness(handle.databasePath), originalDb), "database_changed");
  }
  async function waitNativeAdmissionIdle(activeWork) {
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline) {
      check(state !== "stopping", "process_identity");
      if (activeWork().idle) return deadline;
      await sleep(100);
    }
    check(false, "active_work");
  }
  function bindTransitionIdle(message, phase) {
    const beforeEnvironment = phase === "bind-before-environment";
    check(beforeEnvironment || phase === "bind-before-ack", "bind_identity");
    check(state === (beforeEnvironment ? "binding" : "bound") && process.connected &&
      handle.request === message && seedId === message.seedId && consumed.has(message.requestId) &&
      generation === (beforeEnvironment ? null : message.payload.generation) &&
      bindAdmission?.generation === message.payload.generation &&
      bindAdmission.phase === (beforeEnvironment ? "bind-before-resume" : "bind-after-resume"),
    "bind_identity");
    assertExclusive();
    // These are fresh observations of existing inventories, not a fence against future work.
    const current = bindAdmission.activeWork();
    const admission = handle.server.seedAdmission?.();
    check(current && typeof current.idle === "boolean", "active_work");
    check(admission && Number.isSafeInteger(admission.pending) && admission.pending >= 0 &&
      Number.isSafeInteger(admission.work) && admission.work >= 0 &&
      admission.stopped === false && admission.adopted === true && admission.activated === true,
    "admission_not_idle");
    if (!current.idle || admission.pending !== 0 || admission.work !== 0) return false;
    return true;
  }
  function bindAdmissionRetryDelay() {
    // Reuse the preceding native admission's ten-second budget; do not reset it per retry.
    const remaining = bindAdmission.deadline - Date.now();
    check(remaining > 0, "active_work");
    return Math.min(100, remaining);
  }
  async function nativeProof(phase, proofGeneration = null) {
    const concurrentAudit = useAsyncBindObserver &&
      (phase === "bind-before-resume" || phase === "bind-after-resume");
    let probe = null;
    assertExclusive();
    const { t: activeWork } = await import(vendor + "gateway-active-work-OAr0e9ce.mjs");
    if (concurrentAudit) {
      bindAdmission = Object.freeze({ activeWork, phase, generation: proofGeneration,
        deadline: await waitNativeAdmissionIdle(activeWork) });
    } else {
      let stable = 0;
      const deadline = Date.now() + 10000;
      while (stable < 5 && Date.now() < deadline) {
        stable = activeWork().idle ? stable + 1 : 0;
        await sleep(100);
      }
      check(stable === 5 && activeWork().idle, "active_work");
    }
    const admission = handle.server.seedAdmission?.();
    check(admission && admission.pending === 0 && admission.work === 0 &&
      !admission.stopped && admission.adopted && admission.activated, "admission_not_idle");
    const { l: refusals } = await import(vendor + "agent-database-admission-BULBDQcT.mjs");
    check(refusals({ env: process.env }).length === 0, "admission_refusal");
    const { o: readLease } = await import(vendor + "gateway-process-argv-DLvsergx.mjs");
    const lease = readLease({ env: process.env, port: prepared.port, current: true });
    check(lease?.pid === process.pid && lease.state === "live" &&
      lease.owner === handle.lock.seedOwnership.owner, "lease_identity");
    const { o: readLock } = await import(vendor + "gateway-lock-tftI_mQR.mjs");
    const lock = await readLock({ env: process.env, requireInspection: true });
    check(lock?.pid === process.pid && lock.ownerId === lease.owner && lock.port === prepared.port, "lock_identity");
    const sockets = new Set(fs.readdirSync("/proc/self/fd").flatMap(fd => {
      try { return [fs.readlinkSync(`/proc/self/fd/${fd}`).match(/^socket:\[(\d+)\]$/)?.[1]]; } catch { return []; }
    }));
    const groupSockets = new Set(sockets);
    for (const pid of fs.readdirSync("/proc").filter(name => /^\d+$/.test(name))) {
      try {
        if (processWitness(Number(pid)).group !== original.group) continue;
        for (const fd of fs.readdirSync(`/proc/${pid}/fd`)) {
          try { groupSockets.add(fs.readlinkSync(`/proc/${pid}/fd/${fd}`).match(/^socket:\[(\d+)\]$/)?.[1]); } catch {}
        }
      } catch {}
    }
    const listening = listeners();
    check(listening.some(row => row.port === prepared.port && sockets.has(row.inode)) &&
      listening.filter(row => groupSockets.has(row.inode)).every(row => row.loopback), "listener_identity");
    // Independent process must be excluded by the actual native coordinator lock.
    const probeStart = probeClock();
    try {
      if (usePreparedProbe) {
        if (!probeHelper) {
          probeHelper = createNativeProbeClient({ seedId, databasePath: handle.databasePath,
            cwd: prepared.cwd, onFailure: fail });
          await probeHelper.start();
        }
        probe = await probeHelper.probe(phase, proofGeneration);
      } else probe = synchronousNativeProbe(handle.databasePath, prepared.cwd);
    } catch (error) {
      probe = error.nativeProbe ?? { pid: probeHelper?.pid ?? null, result_code: null,
        spawn_status: null, spawn_signal: null, error_code: "OTHER", outcome: "error",
        helper_request_ordinal: null, helper_import_prepared: false };
      throw error;
    } finally {
      if (probe) probe.duration_ns = probeTiming(probeStart).duration_ns;
    }
    check(probe.result_code === 0, "native_exclusivity_unproven");
    assertExclusive();
  }
  async function stopProbeHelper() {
    if (probeHelper) await probeHelper.stop(generation);
  }
  process.once("exit", () => { probeHelper?.abort(); });
  globalThis[symbol] = {
    fail,
    heartbeatContext() {
      check(["starting", "prepared", "binding"].includes(state) && uuid(seedId), "unbound_identity");
      return Object.freeze({ seedId, pid: process.pid, processStart: Number(original.start), generation });
    },
    async attach(value) {
      check(state === "starting" && value.lock?.seedOwnership, "ordinary_owner_missing");
      handle = { ...handle, ...value };
      await value.server.startupSettled;
      const { o: resolveDb } = await import(vendor + "openclaw-state-db.paths-D2XwLmRO.mjs");
      handle.databasePath = resolveDb(process.env);
      originalDb = privateWitness(handle.databasePath);
      prepared.workspaceWitness = workspaceWitness(prepared.home + "/.openclaw/workspace");
      await ready();
      if (readChatPrepareFlag(process.env[CHAT_PREPARE_FLAG])) {
        prepareChat ??= createLiveSeedChatPreparation(() => {
          check(state === "starting" && generation === null && handle.request?.type === "prepare" &&
            handle.request.seedId === seedId && process.connected, "chat_owner_not_preparing");
          assertExclusive();
          const admission = handle.server.seedAdmission?.();
          check(admission && admission.pending === 0 && admission.work === 0 &&
            !admission.stopped && admission.adopted && admission.activated, "chat_admission_not_idle");
          const expectedEnv = prepareEnv(prepared.neutralEnv, prepared.home);
          for (const [key, expected] of Object.entries(expectedEnv))
            check(process.env[key] === expected, "chat_environment_changed");
          return { seedId, pid: process.pid, processStart: original.start };
        });
        await prepareChat({ enabled: true, seedId });
      }
      if (prepareSessionDiffWorker) {
        sessionDiffWorker ??= createSessionDiffWorkerPreparation(() => {
          check(state === "starting" && generation === null && handle.request?.type === "prepare" &&
            handle.request.seedId === seedId && process.connected, "session_diff_worker_owner_not_preparing");
          assertExclusive();
          assertChatNeutralEnv(process.env);
          const admission = handle.server.seedAdmission?.();
          check(admission && admission.pending === 0 && admission.work === 0 &&
            !admission.stopped && admission.adopted && admission.activated, "session_diff_worker_admission");
          const expectedEnv = prepareEnv(prepared.neutralEnv, prepared.home);
          for (const [key, expected] of Object.entries(expectedEnv))
            check(process.env[key] === expected, "session_diff_worker_environment_changed");
          return { seedId, pid: process.pid, processStart: original.start };
        }, () => {
          if (state !== "bound") return null;
          assertExclusive();
          return { seedId, pid: process.pid, processStart: original.start, generation };
        });
        sessionDiffWorker.attachLifetime(value.server, value.lock);
        await sessionDiffWorker.prepare({ enabled: true, seedId });
      }
      await nativeProof("prepare-before-quiesce");
      await value.lock.seedOwnership.quiesce({ assertExclusive, seedId, generation: null });
      await nativeProof("prepare-after-quiesce");
      state = "prepared";
      reply(handle.request, "prepared", null, { heartbeatQuiescent: true, layout: observedLayout(),
        ...liveEagerCompileProof() });
      handle.request = null;
    },
  };
  process.on("disconnect", () => {
    state = "stopping";
    stopAfterWorkerCleanup();
  });
  process.on("message", async message => {
    try {
      keys(message, ["type", "version", "seedId", "requestId", "payload"]);
      check(message.version === version && uuid(message.seedId) && uuid(message.requestId) &&
        !consumed.has(message.requestId), "request_identity");
      check(["prepare", "bind", "stop"].includes(message.type), "request_type");
      if (seedId) check(message.seedId === seedId, "seed_identity");
      consumed.add(message.requestId);
      if (message.type === "prepare") {
        check(state === "new", "prepare_consumed");
        state = "starting"; seedId = message.seedId;
        keys(message.payload, ["renderedConfig", "neutralEnv", "gatewayToken", "agentsText", "seedRoot", "home", "cwd", "port"]);
        prepared = message.payload;
        configValid(prepared.renderedConfig, prepared.gatewayToken, prepared.port);
        agentsValid(prepared.agentsText);
        const env = prepareEnv(prepared.neutralEnv, prepared.home);
        for (const [key, value] of Object.entries(env)) check(process.env[key] === value, "neutral_env_changed");
        for (const key of ["USER_ID", "INTERNAL_USER_ID", "CHANNEL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
          "AWS_SESSION_TOKEN", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE"])
          check(!process.env[key], "unbound_identity");
        prepared.rootWitness = privateWitness(prepared.seedRoot, true);
        prepared.homeWitness = privateWitness(prepared.home, true);
        prepared.stateWitness = privateWitness(process.env.OPENCLAW_STATE_DIR, true);
        handle = { request: message };
        liveEagerCompileProof();
        preparation();
      } else if (message.type === "bind") {
        check(state === "prepared", "bind_consumed");
        state = "binding"; handle.request = message;
        validateBind(message.payload, prepared);
        await nativeProof("bind-before-resume", message.payload.generation);
        if (useAsyncBindObserver) {
          while (!bindTransitionIdle(message, "bind-before-environment"))
            await sleep(bindAdmissionRetryDelay());
        }
        for (const [key, value] of Object.entries(message.payload.boundEnv)) process.env[key] = value;
        generation = message.payload.generation;
        await handle.lock.seedOwnership.resume({ assertExclusive, seedId, generation });
        await ready(!useAsyncBindObserver);
        await nativeProof("bind-after-resume", generation);
        await stopProbeHelper();
        state = "bound";
        if (sessionDiffWorker) await sessionDiffWorker.resume({ generation });
        if (useAsyncBindObserver) {
          // The successful observation and the ACK below have no intervening await.
          while (!bindTransitionIdle(message, "bind-before-ack"))
            await sleep(bindAdmissionRetryDelay());
        }
        // Ordinary later CLI restarts own their new server and lease lifecycle.
        delete globalThis[symbol];
        reply(message, "bound", null, { heartbeatResponsive: true, userReady: false, layout: observedLayout() });
        handle.request = null;
      } else {
        keys(message.payload, ["reason"]);
        check(["discard", "abort", "shutdown", "invalid_bind", "protocol_failure", "native_failure"]
          .includes(message.payload.reason), "stop_reason");
        state = "stopping";
        reply(message, "stopping");
        stopAfterWorkerCleanup();
      }
    } catch (error) { fail(error); }
  });
  await startup;
}
