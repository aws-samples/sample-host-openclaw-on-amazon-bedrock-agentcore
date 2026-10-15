const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createRuntimeLifecycle, REQUIRED_RESOURCES } = require("./runtime-lifecycle");

const owner = { userId: "u1", actorId: "telegram:1", channel: "telegram", routingGeneration: 2 };
const deferred = () => {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
};
const markReady = (scope) => REQUIRED_RESOURCES.forEach((name) => scope.mark(name));

test("bootstrap health never activates a user and stays unavailable until complete", async () => {
  const gate = deferred();
  let activations = 0;
  const runtime = createRuntimeLifecycle({
    bootstrap: () => gate.promise,
    activate: async () => { activations++; },
  });
  const boot = runtime.start();
  assert.equal(runtime.health(0).statusCode, 503);
  assert.equal(runtime.snapshot().binding, null);
  gate.resolve();
  await boot;
  assert.equal(runtime.health(0).body.status, "Healthy");
  assert.equal(activations, 0);
});

test("restore, gateway and all required resources form one single-flight barrier", async () => {
  const resources = deferred();
  let activations = 0;
  const runtime = createRuntimeLifecycle({
    activate: async (_context, scope) => {
      activations++;
      scope.mark("restore");
      scope.mark("gateway");
      await resources.promise;
      markReady(scope);
    },
  });
  await runtime.start();
  const first = runtime.ensureReady(owner);
  const second = runtime.ensureReady({ ...owner });
  await new Promise(setImmediate);
  assert.equal(runtime.snapshot().state, "BINDING");
  assert.equal(runtime.health(0).body.status, "HealthyBusy");
  await assert.rejects(runtime.ensureReady({ ...owner, userId: "u2" }), /binding/);
  await assert.rejects(runtime.ensureReady({ ...owner, actorId: "slack:1" }), /binding/);
  await assert.rejects(runtime.ensureReady({ ...owner, routingGeneration: 3 }), /binding/);
  resources.resolve();
  await Promise.all([first, second]);
  assert.equal(activations, 1);
  assert.equal(runtime.snapshot().state, "USER_READY");
});

test("missing resource and strict restore errors fail closed without another activation", async () => {
  for (const failure of ["missing", "restore"]) {
    let starts = 0;
    let cleanups = 0;
    const runtime = createRuntimeLifecycle({
      activate: async (_context, scope) => {
        starts++;
        if (failure === "restore") throw new Error("restore denied");
        scope.mark("gateway");
      },
      cleanup: async () => { cleanups++; },
    });
    await runtime.start();
    await assert.rejects(runtime.ensureReady(owner), /restore|required/);
    await assert.rejects(runtime.ensureReady(owner));
    assert.equal(starts, 1);
    assert.equal(cleanups, 1);
    assert.equal(runtime.health(0).statusCode, 503);
  }
});

test("activation deadline aborts ownership and late completion cannot become ready", async () => {
  const late = deferred();
  let stale;
  const runtime = createRuntimeLifecycle({
    activationTimeoutMs: 15,
    activate: async (_context, scope) => {
      stale = scope;
      await late.promise;
      markReady(scope);
    },
  });
  await runtime.start();
  await assert.rejects(runtime.ensureReady(owner), /deadline/);
  late.resolve();
  await new Promise(setImmediate);
  assert.equal(stale.signal.aborted, true);
  assert.throws(() => stale.mark("gateway"), /deadline/);
  assert.equal(runtime.snapshot().state, "FAILED");
});

test("serial turns use frozen binding and do not execute expired queued work", async () => {
  const hold = deferred();
  const calls = [];
  const runtime = createRuntimeLifecycle({ activate: async (_c, scope) => markReady(scope) });
  await runtime.start();
  const context = { ...owner, deadline: Date.now() + 5000 };
  const first = runtime.run(context, async (turn) => {
    assert.ok(Object.isFrozen(turn));
    calls.push(turn.actorId);
    await hold.promise;
  });
  await new Promise(setImmediate);
  // The expired turn rejects at its deadline, before the first turn releases the queue.
  const second = assert.rejects(runtime.run({ ...owner, deadline: Date.now() + 10 }, async () => calls.push("expired")), /deadline/);
  context.actorId = "attacker";
  await new Promise((r) => setTimeout(r, 20));
  hold.resolve();
  await first;
  await second;
  assert.deepEqual(calls, ["telegram:1"]);
});

test("resource recovery is single-flight and failure closes admission", async () => {
  const wait = deferred();
  let refreshes = 0;
  const runtime = createRuntimeLifecycle({ activate: async (_c, scope) => markReady(scope) });
  await runtime.ensureReady(owner);
  const first = runtime.recover(async () => { refreshes++; await wait.promise; });
  const second = runtime.recover(() => { refreshes++; });
  assert.equal(runtime.snapshot().state, "RECOVERING");
  wait.resolve();
  await Promise.all([first, second]);
  assert.equal(refreshes, 1);
  await assert.rejects(runtime.recover(async () => { throw new Error("credentials expired"); }), /expired/);
  await assert.rejects(runtime.ensureReady(owner), /expired/);
  assert.equal(runtime.snapshot().state, "FAILED");
});

for (const phase of ["activation", "recovery"]) {
  test(`${phase} cleans resources acquired while the first cleanup waits`, async () => {
    const cleanupStarted = deferred();
    const releaseCleanup = deferred();
    const releaseWork = deferred();
    const workEnded = deferred();
    const resources = [];
    let cleanupCalls = 0;
    let scope;
    const lateWork = async () => {
      await releaseWork.promise;
      resources.push({ closed: false });
      try { scope.checkpoint(); } finally { workEnded.resolve(); }
    };
    const runtime = createRuntimeLifecycle({
      activationTimeoutMs: 15,
      activate: async (_context, ownerScope) => {
        scope = ownerScope;
        if (phase === "activation") await lateWork();
        else markReady(scope);
      },
      cleanup: async () => {
        const ownedAtStart = [...resources];
        if (++cleanupCalls === 1) {
          cleanupStarted.resolve();
          await releaseCleanup.promise;
        }
        for (const resource of ownedAtStart) resource.closed = true;
      },
    });
    if (phase === "recovery") await runtime.ensureReady(owner);
    const pending = phase === "activation" ? runtime.ensureReady(owner) : runtime.recover(lateWork, 15);
    const rejected = assert.rejects(pending, /deadline/);
    await cleanupStarted.promise;
    releaseWork.resolve();
    await workEnded.promise;
    await new Promise(setImmediate);
    assert.equal(resources[0].closed, false);
    releaseCleanup.resolve();
    await rejected;
    await new Promise(setImmediate);
    assert.equal(cleanupCalls, 2);
    assert.equal(resources[0].closed, true);
    assert.equal(runtime.snapshot().state, "FAILED");
  });
}

test("a queued turn is rejected at its own deadline while the earlier turn still runs", async () => {
  const hold = deferred();
  const calls = [];
  const runtime = createRuntimeLifecycle({ activate: async (_c, scope) => markReady(scope) });
  await runtime.start();
  const first = runtime.run({ ...owner, deadline: Date.now() + 60000 }, async () => {
    calls.push("first");
    await hold.promise;
    return "first done";
  });
  await new Promise(setImmediate);
  const queuedAt = Date.now();
  let settledAt = null;
  const second = runtime.run({ ...owner, deadline: queuedAt + 50 }, async () => calls.push("expired"));
  const outcome = second.then(() => "resolved", (error) => error.message).finally(() => { settledAt = Date.now(); });
  // A turn queued after the expired one still waits for the earlier turn.
  const third = runtime.run({ ...owner, deadline: Date.now() + 60000 }, async () => calls.push("third"));
  await new Promise((r) => setTimeout(r, 300));
  assert.notEqual(settledAt, null, "the queued turn was not rejected while the earlier turn held the queue");
  assert.equal(await outcome, "Turn deadline exceeded in queue");
  assert.ok(settledAt - queuedAt < 250, `rejected ${settledAt - queuedAt} ms after queueing`);
  assert.deepEqual(calls, ["first"], "nothing ran ahead of the earlier turn");
  hold.resolve();
  assert.equal(await first, "first done");
  await third;
  await new Promise((r) => setTimeout(r, 20));
  assert.deepEqual(calls, ["first", "third"], "the expired turn was dispatched after the earlier turn released");
  assert.equal(runtime.snapshot().state, "USER_READY");
});

test("a turn that starts before its deadline is not cut off by the queue timer", async () => {
  const runtime = createRuntimeLifecycle({ activate: async (_c, scope) => markReady(scope) });
  await runtime.start();
  const result = await runtime.run({ ...owner, deadline: Date.now() + 30 }, async () => {
    await new Promise((r) => setTimeout(r, 80));
    return "ran past the queue deadline";
  });
  assert.equal(result, "ran past the queue deadline");
});
