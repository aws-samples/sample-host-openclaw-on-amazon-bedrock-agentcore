// SQLite receiver preparation (sqlite-receiver-control.mjs, sqlite-receiver-reservation.mjs,
// sqlite-backend-prepare.mjs): the empty receiver is prepared before binding and adopted only by
// a bound first acquire for the same carrier module.
const { test, before } = require("node:test");
const assert = require("node:assert/strict");
const { Worker } = require("node:worker_threads");
const { pathToFileURL } = require("node:url");
let receiver, ReceiverReservation, brokerEmpty, EMPTY_KEYS;
before(async () => {
  receiver = await import("./sqlite-receiver-control.mjs");
  ({ ReceiverReservation, brokerEmpty, EMPTY_KEYS } = await import("./sqlite-receiver-reservation.mjs"));
});

const CARRIER = "file:///usr/local/lib/node_modules/openclaw/dist/infra/sqlite-store.worker.js";
function reservationHarness() {
  const seed = { HOME: "/root", AWS_EC2_METADATA_DISABLED: "true" };
  const bound = { ...seed, USER_ID: "actor", INTERNAL_USER_ID: "owner" };
  const sent = [];
  const slot = { actors: new Set(), current: undefined, queue: [], pendingOpens: 0,
    worker: { threadId: 7, ref() {}, unref() {}, postMessage(message) {
      sent.push(message);
      queueMicrotask(() => reservation.receive({ control: receiver.CONTROL, type: "bound",
        threadId: 7, controlId: message.controlId, generation: message.generation,
        environmentHash: receiver.environmentHash(message.environment), empty: empty() }));
    } } };
  const broker = { lifecycle: { async retire() { slot.exited = true; } }, fail(_slot, error) { throw error; } };
  const reservation = new ReceiverReservation(broker, seed, CARRIER);
  broker.agentCoreReceiver = reservation; reservation.attach(slot);
  return { reservation, slot, seed, bound, broker, sent };
}
function empty() {
  return Object.fromEntries(EMPTY_KEYS.map(key => [key, 0]));
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
  await assert.rejects(h.reservation.acquire(h.bound, CARRIER), /binding must precede/);
  await h.reservation.bind("generation", h.bound);
  assert.equal(h.reservation.proof().bound, false);
  assert.equal(h.slot.pendingOpens, 0);
  assert.equal(h.sent.length, 0, "arming sends nothing to the worker");
  const adopted = await h.reservation.acquire(h.bound, CARRIER);
  assert.equal(adopted, h.slot);
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0].environment.USER_ID, "actor");
  assert.equal(h.slot.pendingOpens, 1);
  assert.equal(h.broker.agentCoreReceiver, undefined);
  assert.equal(await h.reservation.acquire(h.bound, CARRIER), null, "consumed reservation is not reused");
  await assert.rejects(h.reservation.retire(), /adopted receiver/);
});
test("SQLite reservation rejects changed owner and proves retirement before fallback", async () => {
  const h = await preparedReservation();
  await h.reservation.bind("generation", h.bound);
  await assert.rejects(h.reservation.acquire({ ...h.bound, USER_ID: "other" }, CARRIER), /identity changed/);
  assert.equal(h.reservation.phase, "retired");
  assert.equal(h.slot.exited, true);
  assert.equal(h.slot.pendingOpens, 0);
});
test("SQLite reservation retires instead of serving another carrier module", async () => {
  const h = await preparedReservation();
  await h.reservation.bind("generation", h.bound);
  const other = "file:///usr/local/lib/node_modules/openclaw/dist/generation-2/infra/sqlite-store.worker.js";
  assert.equal(await h.reservation.acquire(h.bound, other), null);
  assert.equal(h.reservation.phase, "retired");
  assert.equal(h.slot.exited, true);
  assert.equal(h.sent.length, 0, "the anonymous worker never received the binding");
  assert.equal(h.broker.agentCoreReceiver, undefined);
});
test("SQLite reservation lets concurrent opens wait for the claim, then use ordinary slots", async () => {
  const h = await preparedReservation();
  await h.reservation.bind("generation", h.bound);
  const first = h.reservation.acquire(h.bound, CARRIER);
  const second = h.reservation.acquire(h.bound, CARRIER);
  assert.equal(h.reservation.phase, "binding");
  assert.equal(await first, h.slot);
  assert.equal(await second, null);
  assert.equal(h.slot.pendingOpens, 1, "only the first open owns the adopted slot");
});
test("SQLite reservation rejects a ready ACK with any nonempty inventory", async () => {
  const h = reservationHarness(), ready = h.reservation.ready(100);
  assert.throws(() => h.reservation.receive({ control: receiver.CONTROL, type: "ready", threadId: 7,
    environmentHash: receiver.environmentHash(h.seed), empty: { ...empty(), actors: 1 } }), /acknowledgement mismatch/);
  await assert.rejects(ready, /acknowledgement mismatch/);
  const missing = reservationHarness(), pending = missing.reservation.ready(100);
  const { nativeAdmission, ...partial } = empty();
  assert.throws(() => missing.reservation.receive({ control: receiver.CONTROL, type: "ready", threadId: 7,
    environmentHash: receiver.environmentHash(missing.seed), empty: partial }), /acknowledgement mismatch/);
  await assert.rejects(pending, /acknowledgement mismatch/);
});
test("SQLite reservation requires its carrier module URL", () => {
  assert.throws(() => new ReceiverReservation({}, { HOME: "/root" }), /carrier module URL/);
  assert.throws(() => new ReceiverReservation({}, { HOME: "/root" }, "https://example.com/x.js"), /carrier module URL/);
});
test("SQLite broker counts as empty only with no clients, queues or admitted input", () => {
  const input = () => ({ bytes: 0, inputPreparations: new Set(), openQueue: new Map(), opens: new Set() });
  const broker = () => ({ actors: new Map(), clients: new Set(), stores: new Map(), operations: new Set(),
    waiters: new Map(), bytes: 0, inputAdmission: input() });
  assert.equal(brokerEmpty(broker()), true);
  for (const change of [
    (b) => b.actors.set("k", {}), (b) => b.clients.add({}), (b) => b.stores.set({}, {}), (b) => b.operations.add({}),
    (b) => b.waiters.set({}, new Set()), (b) => { b.bytes = 1; }, (b) => { b.inputAdmission.bytes = 1; },
    (b) => b.inputAdmission.inputPreparations.add({}), (b) => b.inputAdmission.openQueue.set("p", []),
    (b) => b.inputAdmission.opens.add({}), (b) => { delete b.inputAdmission; },
  ]) {
    const value = broker();
    change(value);
    assert.equal(brokerEmpty(value), false);
  }
});
test("SQLite receiver environment rejects credentials and prebound identities", () => {
  assert.throws(() => receiver.validateEnvironment({ AWS_ACCESS_KEY_ID: "secret" }, false));
  assert.throws(() => receiver.validateEnvironment({ USER_ID: "actor" }, false));
  assert.throws(() => receiver.validateEnvironment({ USER_ID: "actor" }, true));
  assert.equal(receiver.environmentHash({ A: "a", B: "b" }), receiver.environmentHash({ B: "b", A: "a" }));
});

// The patched store worker imports sqlite-backend-prepare.mjs first, so only the receiver worker
// is already preparing when its other static imports (which reach tslog) evaluate.
function preparationPhaseIn(env) {
  const url = pathToFileURL(require.resolve("./sqlite-backend-prepare.mjs")).href;
  const code = `import(${JSON.stringify(url)}).then((m) => require("node:worker_threads").parentPort.postMessage(
    { preparing: m.backendDefinitionsPreparing(), phase: m.backendDefinitionPreparationProof().phase }),
    (e) => require("node:worker_threads").parentPort.postMessage({ error: String(e.message) }));`;
  return new Promise((resolve, reject) => {
    const worker = new Worker(code, { eval: true, env: { PATH: process.env.PATH, ...env } });
    worker.once("message", (message) => { worker.terminate(); resolve(message); });
    worker.once("error", reject);
  });
}
test("only the empty receiver begins backend preparation when the module loads", async () => {
  const flags = { OPENCLAW_AGENTCORE_SQLITE_RECEIVER_PREPARE: "1", OPENCLAW_AGENTCORE_SQLITE_BACKEND_PREPARE: "1" };
  assert.deepEqual(await preparationPhaseIn({ ...flags, OPENCLAW_AGENTCORE_EMPTY_SQLITE_RECEIVER: "1" }),
    { preparing: true, phase: "preparing" });
  assert.deepEqual(await preparationPhaseIn(flags), { preparing: false, phase: "inactive" });
  assert.deepEqual(await preparationPhaseIn({ OPENCLAW_AGENTCORE_EMPTY_SQLITE_RECEIVER: "1" }),
    { preparing: false, phase: "inactive" });
  assert.match((await preparationPhaseIn({ OPENCLAW_AGENTCORE_EMPTY_SQLITE_RECEIVER: "1",
    OPENCLAW_AGENTCORE_SQLITE_BACKEND_PREPARE: "1" })).error, /requires the prepared receiver/);
});
