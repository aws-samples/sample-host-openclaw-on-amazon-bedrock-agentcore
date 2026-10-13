const { test, before } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createHash, randomUUID } = require("node:crypto");
const { renderConfig, renderAgents } = require("./gateway-config");
let common, probe, receiver, ReceiverReservation, sessionDiff;
before(async () => {
  common = await import("./live-gateway-seed-common.mjs");
  probe = await import("./live-gateway-seed-child.mjs");
  receiver = await import("./sqlite-receiver-control.mjs");
  ({ ReceiverReservation } = await import("./sqlite-receiver-reservation.mjs"));
  sessionDiff = await import("./session-diff-worker-prewarm.mjs");
});

test("unused Git preparation retires before its original server closes or lock releases", async () => {
  for (const method of ["close", "release"]) {
    const prepared = sessionDiff.createSessionDiffWorkerPreparation(() => {}, () => assert.fail("Closed owner was consulted"));
    const server = { async close(value) {
      assert.equal(this, server);
      assert.equal(prepared.snapshot().state, "retired");
      return value;
    } };
    const lock = { async release(value) {
      assert.equal(this, lock);
      assert.equal(prepared.snapshot().state, "retired");
      return value;
    } };
    prepared.attachLifetime(server, lock);
    assert.equal(await (method === "close" ? server.close("closed") : lock.release("closed")), "closed");
    assert.equal(prepared.take(), null);
  }
});

function chatHarness(invalidCapture = false) {
  const globals = {}, key = Symbol.for("openclaw.liveSeedReplyRuntimeWitness");
  const promises = [Promise.resolve(), Promise.resolve(), Promise.resolve()];
  const modules = {
    "gateway-request-scope-CVb8Vezy.mjs": { c: () => undefined },
    "plugin-instance-invocation-DhuKro1-.mjs": { t: () => undefined },
    "get-reply-from-config.runtime-uSvr2ey5.mjs": {
      async prewarmConfigDrivenReplyRuntime() {
        globals[key](invalidCapture ? [promises[0], promises[0], promises[0]] : promises);
      },
    },
    "agents/agent-bundle-mcp-runtime.js": { createSessionMcpRuntime() {}, createBundleMcpJsonSchemaValidator() {} },
    "chat-BuK_U07L.mjs": { chatHandlers: { "chat.send"() {} } },
  };
  const file = fs.readFileSync(path.join(__dirname, "live-gateway-seed-child.mjs"), "utf8");
  const source = file.split("export function createLiveSeedChatPreparation")[1]
    .split("export const PROBE_HELPER_SELECTOR")[0].replaceAll("await import(", "await importModule(");
  const dependencies = { ...common, createHash, globalThis: globals, replyWitnessKey: key,
    isMainThread: true, CHAT_PREPARE_MODULES: [], emptyReplyRegistry: () => undefined,
    process: { pid: 101, env: { AWS_EC2_METADATA_DISABLED: "true" } },
    fs: { readFileSync: () => JSON.stringify({ version: "2026.9.5" }) },
    importModule: async url => modules[url.slice(common.vendor.length)] };
  const create = vm.compileFunction(`let chatPreparationClaim; return function createLiveSeedChatPreparation${source}`,
    Object.keys(dependencies))(...Object.values(dependencies));
  const seedId = randomUUID();
  return { globals, run: create(() => ({ seedId, pid: 101, processStart: "123" })), seedId };
}
test("reply Promise capture is preparation-only and removed after success", async () => {
  const h = chatHarness();
  const result = await h.run({ enabled: true, seedId: h.seedId });
  assert.equal(result.privateReplyPromisesCaptured, 3);
  assert.equal(result.definitionsRetained, true);
  assert.deepEqual(Object.getOwnPropertySymbols(h.globals), []);
  assert.equal(await h.run({ enabled: true, seedId: h.seedId }), result);
  assert.equal(Object.hasOwn(result, "actualReplyLoaderUse"), false);
});
test("invalid reply Promise capture fails closed and removes its observer", async () => {
  const h = chatHarness(true);
  await assert.rejects(h.run({ enabled: true, seedId: h.seedId }), /chat_reply_witness_invalid/);
  assert.deepEqual(Object.getOwnPropertySymbols(h.globals), []);
});

function binding() {
  const generation = randomUUID(), gatewayToken = "private-neutral-gateway-token";
  return { generation, actorId: "actor", userId: "owner", channel: "web",
    gatewayToken, renderedConfig: renderConfig({ gatewayToken }),
    boundEnv: { USER_ID: "actor", INTERNAL_USER_ID: "owner", CHANNEL: "web",
      AWS_CONFIG_FILE: "/tmp/scoped-creds/scoped-aws-config", AWS_SDK_LOAD_CONFIG: "1" },
    absenceProof: { generation, actorId: "actor", userId: "owner", verified: true,
      restoreGateVerified: true, mcpDisabled: true } };
}
test("live preparation accepts only the shared render and neutral environment", () => {
  const bound = binding();
  common.configValid(bound.renderedConfig, bound.gatewayToken, 18789);
  for (const browserEnabled of [true, false]) common.agentsValid(renderAgents({ browserEnabled }));
  assert.throws(() => common.agentsValid("custom user workspace"), /agents_template_changed/);
  assert.throws(() => common.configValid({ ...bound.renderedConfig, mcp: {} }, bound.gatewayToken, 18789));
  const env = common.prepareEnv({ PATH: "/usr/local/bin:/usr/bin:/bin" }, "/root");
  assert.equal(env.OPENCLAW_AGENTCORE_LIVE_EAGER_COMPILE, "1");
  assert.equal(env.OPENCLAW_AGENTCORE_SESSION_DIFF_WORKER_PREPARE, "1");
  assert.equal(env.AWS_EC2_METADATA_DISABLED, "true");
  assert.equal(env.USER_ID, undefined);
  for (const key of ["USER_ID", "AWS_ACCESS_KEY_ID", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "NODE_OPTIONS", "OPENCLAW_DIAGNOSTICS_LOADER_CODE_PRIME"]) {
    assert.throws(() => common.prepareEnv({ [key]: "secret" }, "/root"), /neutral_env_key/);
  }
  assert.throws(() => common.prepareEnv({ OPENCLAW_AGENTCORE_LIVE_CHAT_PREPARE: "0" }, "/root"));
  common.assertLiveEagerCompileExecArgv(undefined, ["--no-lazy"]);
  assert.throws(() => common.assertLiveEagerCompileExecArgv(undefined, []));
  assert.throws(() => common.assertLiveEagerCompileExecArgv(undefined, ["--no-lazy", "--lazy"]));
});
test("bind requires exact identity, credential selector and verified empty restore", () => {
  const valid = binding();
  common.validateBind(valid, valid);
  for (const mutate of [
    b => { b.absenceProof.verified = false; },
    b => { b.absenceProof.restoreGateVerified = false; },
    b => { b.absenceProof.mcpDisabled = false; },
    b => { b.absenceProof.generation = randomUUID(); },
    b => { b.boundEnv.AWS_CONFIG_FILE = "/tmp/other"; },
    b => { b.boundEnv.AWS_SESSION_TOKEN = "secret"; },
    b => { b.boundEnv.INTERNAL_USER_ID = "other"; },
    b => { b.gatewayToken = "different"; },
    b => { b.renderedConfig.channels = { custom: {} }; },
    b => { b.extra = true; },
  ]) {
    const altered = structuredClone(valid); mutate(altered);
    assert.throws(() => common.validateBind(altered, valid));
  }
});
test("native coordinator proof distinguishes exclusion, acquisition and failed release", () => {
  assert.equal(probe.acquireNativeProbeOutcome(() => { throw { family: "gateway-lifecycle" }; }, "/db").outcome, "excluded");
  assert.equal(probe.acquireNativeProbeOutcome(() => { throw { code: "EPERM" }; }, "/db").outcome, "error");
  let releases = 0;
  assert.equal(probe.acquireNativeProbeOutcome(() => ({ release() { releases++; }, closed: true }), "/db").result_code, 3);
  assert.equal(releases, 1);
  assert.equal(probe.acquireNativeProbeOutcome(() => ({ release() {}, closed: false }), "/db").error_code, "RELEASE_FAILED");
});
test("native helper ACK must match the exact process and request lineage", () => {
  const request = { type: "probe", version: "openclaw.live-seed-native-probe.v1",
    seedId: randomUUID(), gatewayPid: 10, gatewayStart: "12", helperPid: 11, helperStart: "13",
    group: 10, requestId: randomUUID(), phase: "bind-before-resume", generation: randomUUID() };
  const reply = { ...request, type: "result", outcome: "excluded", result_code: 0, error_code: null,
    start_ns: "1", end_ns: "2", duration_ns: "1" };
  probe.validateNativeProbeReply(reply, request);
  for (const key of ["requestId", "generation", "seedId", "gatewayStart", "helperStart", "group", "phase"]) {
    assert.throws(() => probe.validateNativeProbeReply({ ...reply, [key]: "changed" }, request));
  }
  assert.throws(() => probe.validateNativeProbeReply({ ...reply, result_code: 3 }, request));
  assert.throws(() => probe.validateNativeProbeReply({ ...reply, extra: true }, request));
});

function reservationHarness() {
  const seed = { HOME: "/root", AWS_EC2_METADATA_DISABLED: "true" };
  const bound = { ...seed, USER_ID: "actor", INTERNAL_USER_ID: "owner" };
  const slot = { actors: new Map(), current: null, queue: [], pendingOpens: 0,
    worker: { threadId: 7, ref() {}, unref() {}, postMessage(message) {
      queueMicrotask(() => reservation.receive({ control: receiver.CONTROL, type: "bound",
        threadId: 7, controlId: message.controlId, generation: message.generation,
        environmentHash: receiver.environmentHash(message.environment), empty: empty() }));
    } } };
  const broker = { async retire() { slot.exited = true; }, fail(_slot, error) { throw error; } };
  const reservation = new ReceiverReservation(broker, seed);
  broker.agentCoreReceiver = reservation; reservation.attach(slot);
  return { reservation, slot, seed, bound, broker };
}
function empty() {
  return Object.fromEntries(["actors", "paths", "contexts", "fences", "pendingInput", "pendingResult",
    "sourceLoader", "operationAdmission", "lifecycle", "maintenanceFence"].map(key => [key, 0]));
}
async function preparedReservation() {
  const h = reservationHarness(), ready = h.reservation.ready(100);
  h.reservation.receive({ control: receiver.CONTROL, type: "ready", threadId: 7,
    environmentHash: receiver.environmentHash(h.seed), empty: empty() });
  await ready;
  return h;
}
test("SQLite reservation stays anonymous until a bound first acquire", async () => {
  const h = await preparedReservation();
  assert.equal(h.reservation.proof().phase, "ready");
  await h.reservation.bind("generation", h.bound);
  assert.equal(h.reservation.proof().bound, false);
  assert.equal(h.slot.pendingOpens, 0);
  const adopted = await h.reservation.claim(h.bound);
  assert.equal(adopted, h.slot);
  assert.equal(h.slot.pendingOpens, 1);
  assert.equal(h.broker.agentCoreReceiver, undefined);
  await assert.rejects(h.reservation.retire(), /adopted receiver/);
});
test("SQLite reservation rejects changed owner and proves retirement before fallback", async () => {
  const h = await preparedReservation();
  await h.reservation.bind("generation", h.bound);
  await assert.rejects(h.reservation.claim({ ...h.bound, USER_ID: "other" }), /identity changed/);
  assert.equal(h.reservation.phase, "retired");
  assert.equal(h.slot.exited, true);
  assert.equal(h.slot.pendingOpens, 0);
});
test("SQLite reservation rejects a ready ACK with any nonempty inventory", async () => {
  const h = reservationHarness(), ready = h.reservation.ready(100);
  assert.throws(() => h.reservation.receive({ control: receiver.CONTROL, type: "ready", threadId: 7,
    environmentHash: receiver.environmentHash(h.seed), empty: { ...empty(), actors: 1 } }), /acknowledgement mismatch/);
  await assert.rejects(ready, /acknowledgement mismatch/);
});
test("SQLite receiver environment rejects credentials and prebound identities", () => {
  assert.throws(() => receiver.validateEnvironment({ AWS_ACCESS_KEY_ID: "secret" }, false));
  assert.throws(() => receiver.validateEnvironment({ USER_ID: "actor" }, false));
  assert.throws(() => receiver.validateEnvironment({ USER_ID: "actor" }, true));
  assert.equal(receiver.environmentHash({ A: "a", B: "b" }), receiver.environmentHash({ B: "b", A: "a" }));
});
