// SQLite receiver preparation (sqlite-receiver-control.mjs, sqlite-receiver-reservation.mjs):
// the empty receiver is prepared before binding and adopted only by a bound first acquire.
const { test, before } = require("node:test");
const assert = require("node:assert/strict");
let receiver, ReceiverReservation;
before(async () => {
  receiver = await import("./sqlite-receiver-control.mjs");
  ({ ReceiverReservation } = await import("./sqlite-receiver-reservation.mjs"));
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
