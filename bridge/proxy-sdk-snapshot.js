/**
 * Preload the pinned Bedrock SDK in a user-neutral proxy child for snapshots
 * in AgentCore Runtime, then bind that same child before loading the proxy.
 *
 * The parent and child check the launch environment, process ancestry and
 * start ticks, SDK source hash, and retained SDK exports. Binding installs an
 * allowlisted environment once; subsequent attestations check the same
 * generation, user binding, and process lineage through inherited IPC.
 * Nonces and increasing request ordinals correlate control acknowledgments.
 *
 * Protocol failures, lost ownership, and phase deadlines terminate the child.
 * The body-loaded acknowledgment confirms proxy module loading; readiness
 * checks for the serving endpoint remain the caller's responsibility.
 */
"use strict";

const fs = require("node:fs");
const { spawn } = require("node:child_process");
const { createHash, randomUUID } = require("node:crypto");
const { createRequire } = require("node:module");

const TYPE = "openclaw.proxy-sdk-snapshot-control.v1";
const SCHEMA = "openclaw.proxy-sdk-snapshot.v1";
const ENTRY = "/app/proxy-sdk-snapshot.js";
const PROXY = "/app/agentcore-proxy.js";
const SELECTOR = "--openclaw-proxy-sdk-snapshot-v1";
const SDK = "@aws-sdk/client-bedrock-runtime";
const SDK_PATH = "/app/node_modules/@aws-sdk/client-bedrock-runtime/dist-cjs/index.js";
const SDK_SHA = "2d0c2e259d5ea7b483ee5b9dc301ac85f85f9a612074f1c5a5d1ee9be8887682";
const NODE_OPTIONS = "--dns-result-order=ipv4first --no-network-family-autoselection -r /app/force-ipv4.js";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const BINDING_KEYS = ["userId", "actorId", "runtimeSessionId", "routingGeneration"];
const BASE_KEYS = ["type", "event", "nonce", "generation", "parent_pid", "parent_start_ticks",
  "proxy_pid", "proxy_start_ticks"];
const ENV_KEYS = ["PATH", "HOME", "NODE_PATH", "NODE_OPTIONS", "AWS_REGION",
  "BEDROCK_MODEL_ID", "BEDROCK_FALLBACK_MODEL_ID", "COGNITO_USER_POOL_ID", "COGNITO_CLIENT_ID", "COGNITO_PASSWORD_SECRET",
  "S3_USER_FILES_BUCKET", "SUBAGENT_MODEL_NAME", "SUBAGENT_BEDROCK_MODEL_ID",
  "BEDROCK_GUARDRAIL_ID", "BEDROCK_GUARDRAIL_VERSION", "BEDROCK_TEMPERATURE",
  "BEDROCK_USER_SELECTABLE_MODEL_IDS",
  "USER_ID", "INTERNAL_USER_ID", "CHANNEL", "OPENCLAW_SKIP_CRON"];
const OPTIONAL_ENV = ["NODE_COMPILE_CACHE"];
const REASONS = new Set(["selector", "lineage", "protocol", "environment", "binding", "sdk",
  "prepare_timeout", "activate_timeout", "attest_timeout", "ipc", "child_exit",
  "parent_lost", "aborted", "closed", "body_load", "cleanup"]);
const capabilities = new WeakMap();
let serving = null;

function error(reason) {
  const e = new Error("Proxy SDK snapshot: " + (REASONS.has(reason) ? reason : "closed"));
  e.code = REASONS.has(reason) ? reason : "closed";
  return e;
}
function check(condition, reason) { if (!condition) throw error(reason); }
function exact(value, keys) {
  return value && typeof value === "object" && !Array.isArray(value) &&
    Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}
function uuid(value) { return typeof value === "string" && UUID.test(value); }
function equal(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
function entries(env) { return Object.keys(env).sort().map((key) => [key, env[key]]); }
function digest(value) { return createHash("sha256").update(JSON.stringify(value)).digest("hex"); }
function clock() { return process.hrtime.bigint().toString(); }
function processInfo(pid, allowZombie = false) {
  check(Number.isSafeInteger(pid) && pid > 0, "lineage");
  const text = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
  const f = text.slice(text.lastIndexOf(")") + 2).trim().split(/\s+/);
  check(f[0] !== "X" && (allowZombie || f[0] !== "Z") && /^[0-9]+$/.test(f[19] || ""), "lineage");
  return { pid, ppid: Number(f[1]), start_ticks: f[19], state: f[0] };
}
function staticEnv(env) {
  const value = {
    PATH: env.PATH, HOME: env.HOME || "/root", NODE_PATH: env.NODE_PATH || "/app/node_modules",
    NODE_OPTIONS: env.NODE_OPTIONS || "", AWS_REGION: env.AWS_REGION || "us-west-2",
  };
  if (env.NODE_COMPILE_CACHE !== undefined) value.NODE_COMPILE_CACHE = env.NODE_COMPILE_CACHE;
  const allowedPath = new Set(["/opt/venv/bin", "/usr/local/sbin", "/usr/local/bin",
    "/usr/sbin", "/usr/bin", "/sbin", "/bin"]);
  check(typeof value.PATH === "string" && value.PATH.split(":").every((p) => allowedPath.has(p)) &&
    value.HOME === "/root" && value.NODE_PATH === "/app/node_modules" &&
    ["", NODE_OPTIONS].includes(value.NODE_OPTIONS) &&
    (value.NODE_COMPILE_CACHE === undefined || value.NODE_COMPILE_CACHE === "/app/.compile-cache") &&
    typeof value.AWS_REGION === "string" && /^[a-z]{2}(?:-[a-z]+)+-[0-9]+$/.test(value.AWS_REGION), "selector");
  return value;
}
function launchIdentity(env) {
  check(process.platform === "linux" && process.arch === "arm64" && process.version === "v24.21.0" &&
    process.cwd() === "/app" && process.getuid() === 0 && process.getgid() === 0, "selector");
  let executable = null;
  for (const path of env.PATH.split(":")) {
    try { fs.accessSync(path + "/node", fs.constants.X_OK); executable = fs.realpathSync(path + "/node"); break; }
    catch (e) { if (!["ENOENT", "EACCES"].includes(e.code)) throw error("selector"); }
  }
  check(executable === "/usr/local/bin/node" && fs.realpathSync(process.execPath) === executable, "selector");
  return { env: entries(env), executable, cwd: process.cwd(), uid: process.getuid(), gid: process.getgid(),
    node: process.version, exec_argv: [] };
}
function bindingCopy(value) {
  check(exact(value, BINDING_KEYS), "binding");
  const result = Object.fromEntries(BINDING_KEYS.map((key) => [key, value[key]]));
  check(["userId", "actorId"].every((key) => typeof result[key] === "string" &&
    result[key].length > 0 && result[key].length <= 1024 && !result[key].includes("\0")), "binding");
  check(result.runtimeSessionId === null || (typeof result.runtimeSessionId === "string" &&
    result.runtimeSessionId.length <= 256), "binding");
  check(result.routingGeneration === null || (typeof result.routingGeneration === "string" &&
    result.routingGeneration.length <= 256) || Number.isSafeInteger(result.routingGeneration), "binding");
  return Object.freeze(result);
}
function boundEnv(input, neutral, binding) {
  check(input && typeof input === "object" && !Array.isArray(input), "environment");
  const env = Object.fromEntries(Object.entries(input).filter(([, v]) => v !== undefined));
  check(ENV_KEYS.every((key) => Object.hasOwn(env, key)) &&
    Object.keys(env).every((key) => ENV_KEYS.includes(key) || OPTIONAL_ENV.includes(key)) &&
    Object.values(env).every((v) => typeof v === "string" && !v.includes("\0")) &&
    JSON.stringify(env).length <= 65536 && env.OPENCLAW_SKIP_CRON === "1" &&
    env.USER_ID === binding.actorId && env.INTERNAL_USER_ID === binding.userId &&
    equal(entries(staticEnv(env)), entries(neutral)), "environment");
  return Object.freeze(env);
}
function envelope(lineage, event, generation = null) { return { type: TYPE, event, ...lineage, generation }; }
function validEnvelope(message, lineage, event, generation, extra) {
  return exact(message, [...BASE_KEYS, ...extra]) && message.type === TYPE && message.event === event &&
    message.generation === generation && Object.keys(lineage).every((key) => message[key] === lineage[key]);
}
function emit(event, lineage, generation, fields) {
  try {
    const monotonic_ns = clock();
    fs.writeSync(2, JSON.stringify({ schema: SCHEMA, event, ...lineage, generation,
      producer: process.pid === lineage.proxy_pid ? "proxy" : "bridge", producer_pid: process.pid,
      clock: "process.hrtime.bigint", monotonic_ns, ...fields }) + "\n");
  } catch {}
}
function currentLineage(lineage, allowChildZombie = false) {
  const parent = processInfo(lineage.parent_pid), child = processInfo(lineage.proxy_pid, allowChildZombie);
  check(parent.start_ticks === lineage.parent_start_ticks && child.start_ticks === lineage.proxy_start_ticks &&
    child.ppid === parent.pid, "lineage");
  return child.state;
}
function bindingHash(env, binding, generation, lineage) {
  return digest({ env: entries(env), binding, generation, lineage });
}

function createProxySdkSnapshotOwner({ env, onChild = () => {}, onFailure = () => {} }) {
  check(env.USER_ID === undefined && env.INTERNAL_USER_ID === undefined, "selector");
  const neutral = Object.freeze(staticEnv(env));
  const launch = launchIdentity(neutral), launchHash = digest(launch);
  const parent = processInfo(process.pid), nonce = randomUUID();
  const capability = Object.freeze({});
  let child = null, lineage = null, pending = null, phase = "new", failure = null;
  let preparePromise = null, stopping = null, bound = null, abortSignal = null, assertCurrent = null;
  let attempted = false, lastAck = null;
  const closed = () => ["closed", "stopping"].includes(phase);
  const alive = () => child && child.exitCode === null && child.signalCode === null && child.connected;
  const rejectPending = (e) => {
    if (!pending) return;
    clearTimeout(pending.timer);
    const work = pending; pending = null; work.reject(e);
  };
  const stop = () => {
    if (stopping) return stopping;
    phase = "stopping";
    rejectPending(failure || error("closed"));
    abortSignal?.removeEventListener("abort", abort);
    stopping = new Promise((resolve, reject) => {
      if (!child || child.exitCode !== null || child.signalCode !== null) { phase = "closed"; resolve(); return; }
      let timer, settled = false;
      const settle = (e) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        child.removeListener("exit", finish);
        child.removeListener("close", finish);
        if (e) reject(e);
        else { phase = "closed"; resolve(); }
      };
      const finish = () => {
        if (child.exitCode !== null || child.signalCode !== null) settle();
      };
      child.once("exit", finish);
      child.once("close", finish);
      const kill = (signal) => {
        if (lineage) {
          check(child.pid === lineage.proxy_pid, "lineage");
          // Only cleanup accepts the same zombie; live admission and attestation remain strict.
          if (currentLineage(lineage, true) === "Z") return false;
        }
        check(child.kill(signal), "cleanup");
        return true;
      };
      try {
        const signalled = kill("SIGTERM");
        if (settled) return;
        timer = setTimeout(() => {
          try {
            if (signalled) kill("SIGKILL");
            if (!settled) timer = setTimeout(() => settle(error("cleanup")), 2000);
          } catch { settle(error("cleanup")); }
        }, 2000);
      } catch { settle(error("cleanup")); }
    });
    return stopping;
  };
  const fail = (reason) => {
    if (closed()) return;
    failure ||= error(reason);
    rejectPending(failure);
    if (lineage) emit("failed", lineage, bound?.generation ?? null, { phase, status: "failed", reason: failure.code });
    void stop().catch(() => {});
    try { onFailure(failure); } catch {}
  };
  const abort = () => fail("aborted");
  function current() {
    check(!closed() && alive() && lineage && child.pid === lineage.proxy_pid &&
      process.pid === lineage.parent_pid, "closed");
    currentLineage(lineage);
    check(equal(launchIdentity(staticEnv(env)), launch), "environment");
    if (bound) { check(!abortSignal?.aborted, "aborted"); assertCurrent(); }
  }
  function wait(event, extra, timeout, reason, send) {
    check(!pending, "protocol");
    return new Promise((resolve, reject) => {
      pending = { event, extra, resolve, reject, timer: setTimeout(() => fail(reason), timeout) };
      if (send) transmit(send);
    });
  }
  function transmit(message) {
    try {
      check(alive(), "closed");
      child.send(message, (e) => { if (e) fail("ipc"); });
    } catch { fail("ipc"); }
  }
  function receive(message) {
    if (closed()) return;
    try {
      current();
      if (phase === "bound" && validAuthStatus(message, bound.generation)) return;
      check(pending && validEnvelope(message, lineage, pending.event, bound?.generation ?? null,
        pending.extra), "protocol");
      if (pending.event === "prepared") {
        check(message.launch_hash === launchHash && exact(message.prepared, ["start_ns", "end_ns", "duration_ns"]) &&
          Object.values(message.prepared).every((v) => typeof v === "string" && /^[0-9]+$/.test(v)) &&
          BigInt(message.prepared.end_ns) - BigInt(message.prepared.start_ns) === BigInt(message.prepared.duration_ns), "protocol");
        phase = "prepared";
      } else {
        check(message.request_id === bound.requestId && message.request_ordinal === bound.ordinal &&
          message.env_hash === bound.hash &&
          equal(bindingCopy(message.binding), bound.binding), "binding");
        lastAck = Object.freeze({ ...message });
        phase = "bound";
      }
      clearTimeout(pending.timer);
      const work = pending; pending = null; work.resolve(message);
    } catch (e) { fail(REASONS.has(e.code) ? e.code : "protocol"); }
  }
  const prepare = () => {
    if (closed() || phase === "bound" || phase === "binding") return Promise.reject(error("closed"));
    if (preparePromise) return preparePromise;
    check(phase === "new", "closed");
    phase = "preparing";
    preparePromise = wait("prepared", ["launch_hash", "prepared"], 30000, "prepare_timeout");
    try {
      child = spawn("node", [ENTRY, SELECTOR], {
        cwd: launch.cwd, env: { ...neutral }, stdio: ["inherit", "pipe", "pipe", "ipc"],
        detached: false,
      });
      child.on("message", receive);
      child.once("spawn", () => {
        if (closed()) { void stop().catch(() => {}); return; }
        try {
          const info = processInfo(child.pid);
          check(info.ppid === parent.pid, "lineage");
          lineage = Object.freeze({ nonce, parent_pid: parent.pid, parent_start_ticks: parent.start_ticks,
            proxy_pid: info.pid, proxy_start_ticks: info.start_ticks });
          current();
          transmit({ ...envelope(lineage, "prepare"), launch_hash: launchHash });
        } catch { fail("lineage"); }
      });
      child.once("error", () => fail("child_exit"));
      child.once("exit", () => fail("child_exit"));
      child.once("disconnect", () => fail("parent_lost"));
      onChild(child);
    } catch { fail("child_exit"); }
    return preparePromise;
  };
  const activate = async (options) => {
    try {
      check(phase === "prepared" && !attempted && !pending, "binding");
      attempted = true;
      current();
      check(uuid(options.generation) && options.signal && typeof options.assertCurrent === "function", "binding");
      const binding = bindingCopy(options.binding);
      const fullEnv = boundEnv(options.env, neutral, binding);
      assertCurrent = options.assertCurrent;
      abortSignal = options.signal;
      check(!abortSignal.aborted, "aborted");
      assertCurrent();
      const requestId = randomUUID();
      bound = { binding, env: fullEnv, generation: options.generation, requestId, ordinal: 1,
        hash: bindingHash(fullEnv, binding, options.generation, lineage) };
      abortSignal.addEventListener("abort", abort, { once: true });
      phase = "binding";
      await wait("body-loaded", ["request_id", "request_ordinal", "env_hash", "binding"], 30000, "activate_timeout",
        { ...envelope(lineage, "bind", bound.generation), request_id: requestId, request_ordinal: bound.ordinal,
          env: fullEnv, binding, env_hash: bound.hash });
      current();
      return child;
    } catch (e) { fail(REASONS.has(e.code) ? e.code : "binding"); throw failure || error("binding"); }
  };
  const attest = async (context, state, launchProof) => {
    try {
      current();
      check(phase === "bound" && !pending && lastAck && state.generation === bound.generation &&
        state.owner.proxy_pid === child.pid && launchProof.proxy_pid === child.pid &&
        launchProof.proxy_start_ticks === lineage.proxy_start_ticks &&
        equal(bindingCopy(state.binding), bound.binding) &&
        BINDING_KEYS.every((key) => (context[key] ?? null) === bound.binding[key]), "binding");
      bound.requestId = randomUUID();
      bound.ordinal++;
      await wait("attested", ["request_id", "request_ordinal", "env_hash", "binding"], 5000, "attest_timeout",
        { ...envelope(lineage, "attest", bound.generation), request_id: bound.requestId,
          request_ordinal: bound.ordinal });
      current();
      return Object.freeze({ ...lineage, generation: bound.generation });
    } catch (e) { fail(REASONS.has(e.code) ? e.code : "binding"); throw failure || error("binding"); }
  };
  capabilities.set(capability, attest);
  return Object.freeze({ prepare, activate, stop, capability,
    attest: async (context, signal) => {
      signal?.throwIfAborted();
      const proof = await attest(context, {
        generation: bound?.generation, binding: bindingCopy(context), owner: { proxy_pid: child?.pid },
      }, { proxy_pid: child?.pid, proxy_start_ticks: lineage?.proxy_start_ticks });
      signal?.throwIfAborted();
      current();
      return proof;
    },
    get child() { return child; },
  });
}

function validAuthStatus(message, generation) {
  return exact(message, ["type", "generation", "status"]) &&
    message.type === "openclaw.proxy-auth-status.v1" && message.generation === generation &&
    ["started", "warmed", "unavailable", "failed", "joined", "stopped"].includes(message.status);
}
function attestProxySdkSnapshot(capability, context, state, launch) {
  const attest = capabilities.get(capability);
  check(typeof attest === "function", "binding");
  return attest(context, state, launch);
}
function sdkIdentity() {
  try {
    const fromProxy = createRequire(PROXY);
    const resolved = fromProxy.resolve(SDK);
    check(resolved === SDK_PATH && require.resolve(SDK) === resolved &&
      createHash("sha256").update(fs.readFileSync(resolved)).digest("hex") === SDK_SHA, "sdk");
    return { fromProxy, resolved };
  } catch { throw error("sdk"); }
}
function assertProxySdkSnapshotActive() {
  check(serving && ["activating", "bound"].includes(serving.phase), "binding");
}
function preparedSdk() {
  if (!serving) return null;
  assertProxySdkSnapshotActive();
  check(serving.bindingMatches() && require.cache[serving.resolved]?.exports === serving.sdk &&
    Object.keys(serving.classes).every((key) => serving.sdk[key] === serving.classes[key]), "sdk");
  return serving.sdk;
}

function runChild() {
  let phase = "new", lineage = null, generation = null, binding = null, expectedHash = null;
  let neutral, launchHash, prepared, sdk, classes, resolved, previousRequest = null, ordinal = 0;
  const stop = (reason, code = 1) => {
    if (phase === "closed") return;
    const failedPhase = phase; phase = "closed";
    if (lineage && code) emit("failed", lineage, generation, { phase: failedPhase, status: "failed", reason });
    process.exit(code);
  };
  function current() {
    check(process.connected && lineage && process.pid === lineage.proxy_pid &&
      process.ppid === lineage.parent_pid, "parent_lost");
    currentLineage(lineage);
    check(equal(launchIdentity(staticEnv(process.env)), JSON.parse(launchHash.input)), "environment");
  }
  function actualBinding() {
    current();
    const env = boundEnv(process.env, neutral, binding);
    check(bindingHash(env, binding, generation, lineage) === expectedHash &&
      require.cache[resolved]?.exports === sdk &&
      Object.keys(classes).every((key) => sdk[key] === classes[key]), "environment");
    return bindingCopy({ ...binding, actorId: process.env.USER_ID, userId: process.env.INTERNAL_USER_ID });
  }
  function send(message) {
    check(process.connected, "parent_lost");
    process.send(message, (e) => { if (e) stop("ipc"); });
  }
  function receive(message) {
    if (phase === "closed") return;
    try {
      if (phase === "new") {
        check(exact(message, [...BASE_KEYS, "launch_hash"]) && message.type === TYPE &&
          message.event === "prepare" && message.generation === null && uuid(message.nonce) &&
          message.proxy_pid === process.pid && message.parent_pid === process.ppid &&
          ["parent_start_ticks", "proxy_start_ticks"].every((key) =>
            typeof message[key] === "string" && /^[1-9][0-9]{0,31}$/.test(message[key])), "protocol");
        lineage = Object.freeze(Object.fromEntries(BASE_KEYS.filter((k) =>
          !["type", "event", "generation"].includes(k)).map((key) => [key, message[key]])));
        current();
        check(message.launch_hash === launchHash.digest, "environment");
        phase = "preparing";
        const start_ns = clock();
        const identity = sdkIdentity();
        resolved = identity.resolved;
        check(!require.cache[resolved], "sdk");
        try { sdk = identity.fromProxy(SDK); } catch { throw error("sdk"); }
        check(["BedrockRuntimeClient", "ConverseCommand", "ConverseStreamCommand"]
          .every((key) => typeof sdk[key] === "function"), "sdk");
        classes = Object.freeze(Object.fromEntries(["BedrockRuntimeClient", "ConverseCommand", "ConverseStreamCommand"]
          .map((key) => [key, sdk[key]])));
        check(!require.cache[PROXY] && equal(entries(process.env), entries({ ...neutral })), "environment");
        const end_ns = clock();
        prepared = Object.freeze({ start_ns, end_ns, duration_ns: (BigInt(end_ns) - BigInt(start_ns)).toString() });
        phase = "prepared";
        send({ ...envelope(lineage, "prepared"), launch_hash: launchHash.digest, prepared });
        return;
      }
      current();
      if (phase === "bound" && exact(message, ["type", "generation"]) &&
          ["openclaw.proxy-auth-prepare.v1", "openclaw.proxy-auth-stop.v1"].includes(message.type)) {
        check(message.generation === generation, "binding");
        actualBinding();
        return; // The already-installed original AUTH receiver handles this message.
      }
      if (phase === "prepared") {
        check(validEnvelope(message, lineage, "bind", message.generation,
          ["request_id", "request_ordinal", "env", "binding", "env_hash"]) &&
          uuid(message.generation) && uuid(message.request_id) && message.request_ordinal === 1, "protocol");
        binding = bindingCopy(message.binding);
        const fullEnv = boundEnv(message.env, neutral, binding);
        generation = message.generation;
        expectedHash = bindingHash(fullEnv, binding, generation, lineage);
        check(message.env_hash === expectedHash && !require.cache[PROXY], "binding");
        check(equal(entries(process.env), entries({ ...neutral })), "environment");
        Object.assign(process.env, fullEnv);
        previousRequest = message.request_id;
        ordinal = message.request_ordinal;
        actualBinding();
        phase = "activating";
        serving = { phase, lineage, generation, sdk, classes, resolved, ordinal: 0,
          bindingMatches: () => process.pid === lineage.proxy_pid && process.ppid === lineage.parent_pid &&
            bindingHash(boundEnv(process.env, neutral, binding), binding, generation, lineage) === expectedHash };
        emit("prepared", lineage, null, { ...prepared, status: "prepared",
          replayed_after_binding: true, sdk_exports_verified: true });
        const start_ns = clock();
        try { require(PROXY); } catch { throw error("body_load"); }
        process.channel.ref(); // AUTH may unref it; this owner must retain disconnect detection.
        const actual = actualBinding();
        phase = serving.phase = "bound";
        const end_ns = clock();
        emit("bound", lineage, generation, { start_ns, end_ns,
          duration_ns: (BigInt(end_ns) - BigInt(start_ns)).toString(), status: "body_loaded" });
        send({ ...envelope(lineage, "body-loaded", generation), request_id: message.request_id,
          request_ordinal: ordinal, env_hash: expectedHash, binding: actual });
      } else {
        check(phase === "bound" && validEnvelope(message, lineage, "attest", generation,
          ["request_id", "request_ordinal"]) && uuid(message.request_id) && message.request_id !== previousRequest &&
          Number.isSafeInteger(message.request_ordinal) && message.request_ordinal === ordinal + 1, "protocol");
        previousRequest = message.request_id;
        ordinal = message.request_ordinal;
        send({ ...envelope(lineage, "attested", generation), request_id: message.request_id,
          request_ordinal: ordinal, env_hash: expectedHash, binding: actualBinding() });
      }
    } catch (e) { stop(REASONS.has(e.code) ? e.code : "protocol"); }
  }
  try {
    check(process.argv.length === 3 && process.argv[1] === ENTRY && process.argv[2] === SELECTOR &&
      process.execArgv.length === 0 && process.connected && typeof process.send === "function" &&
      typeof process.channel?.ref === "function", "selector");
    neutral = Object.freeze(staticEnv(process.env));
    check(equal(entries(process.env), entries({ ...neutral })), "environment");
    const identity = launchIdentity(neutral);
    launchHash = { input: JSON.stringify(identity), digest: digest(identity) };
    process.on("message", receive);
    process.once("disconnect", () => stop("parent_lost"));
    process.once("SIGTERM", () => stop("closed", 0));
    process.channel.ref();
  } catch (e) { stop(REASONS.has(e.code) ? e.code : "selector"); }
}

module.exports = { createProxySdkSnapshotOwner, attestProxySdkSnapshot, preparedSdk };
if (require.main === module) runChild();
