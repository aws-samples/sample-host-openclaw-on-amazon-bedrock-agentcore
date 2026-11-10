const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createGatewayActivation } = require("./gateway-activation");

function fixture(extraEnv = {}) {
  const binding = Object.freeze({ actorId: "slack:test-user", userId: "test-user" });
  const env = { USER_ID: binding.actorId, INTERNAL_USER_ID: binding.userId, ...extraEnv };
  const abort = new AbortController();
  const scope = { generation: "test-generation", signal: abort.signal,
    checkpoint: () => abort.signal.throwIfAborted() };
  const current = { state: "BINDING", generation: scope.generation, binding, namespace: "slack_test-user",
    resources: ["credentials"] };
  const lifecycle = { snapshot: () => current, assertBinding: (value) => assert.equal(value, current.binding) };
  const calls = [];
  const child = { pid: 1234 };
  const activation = createGatewayActivation({
    env,
    createCold: () => ({
      child,
      async prepare() { calls.push("cold"); },
      async activate(input) { calls.push("cold-activate"); return input; },
      async stop() { calls.push("cold-stop"); },
    }),
  });
  const input = { lifecycle, scope, namespace: "slack_test-user" };
  return { activation, calls, input, current, abort, env, child };
}

test("nothing starts before the snapshot; binding spawns, activation follows", async () => {
  const f = fixture();
  assert.deepEqual(await f.activation.prepare(), { status: "deferred" });
  assert.equal(f.activation.child, undefined);
  assert.deepEqual(f.calls, []);
  const phases = [];
  f.input.onPhase = (name, details) => phases.push(details ? `${name}:${JSON.stringify(details)}` : name);
  assert.deepEqual(await f.activation.restoreForBinding(f.input), { status: "cold" });
  assert.deepEqual(phases, ["cold_prepare_start", "cold_prepare_ack"]);
  assert.equal(f.activation.child, f.child);
  // A repeated call reuses the spawned gateway instead of spawning another.
  assert.deepEqual(await f.activation.restoreForBinding(f.input), { status: "cold" });
  assert.deepEqual(f.calls, ["cold"]);
  const request = { generation: f.input.scope.generation };
  assert.equal(await f.activation.activate(request), request);
  assert.deepEqual(f.calls, ["cold", "cold-activate"]);
  await f.activation.stop();
  assert.deepEqual(f.calls, ["cold", "cold-activate", "cold-stop"]);
  await assert.rejects(f.activation.activate(request), /stopped/);
});

for (const value of ["1", "", "0", "true"]) {
  test(`the removed OPENCLAW_UNSAFE_SHARED_SEED=${JSON.stringify(value)} starts nothing before the snapshot`, async () => {
    const f = fixture({ OPENCLAW_UNSAFE_SHARED_SEED: value });
    assert.equal((await f.activation.prepare()).status, "deferred");
    assert.deepEqual(f.calls, []);
    assert.equal((await f.activation.restoreForBinding(f.input)).status, "cold");
    assert.deepEqual(f.calls, ["cold"]);
  });
}

test("activation and binding are refused out of order", async () => {
  const f = fixture();
  await assert.rejects(f.activation.restoreForBinding(f.input), /not prepared/);
  await f.activation.prepare();
  await assert.rejects(f.activation.prepare(), /already attempted/);
  await assert.rejects(f.activation.activate({}), /must be prepared/);
  assert.deepEqual(f.calls, []);
});

test("a binding change, stop or abort around the spawn cannot activate", async () => {
  const f = fixture();
  await f.activation.prepare();
  f.current.binding = { actorId: "slack:other", userId: "other" };
  await assert.rejects(f.activation.restoreForBinding(f.input), /binding mismatch|changed/);
  assert.deepEqual(f.calls, []);

  const env = fixture();
  await env.activation.prepare();
  env.env.USER_ID = "slack:other";
  await assert.rejects(env.activation.restoreForBinding(env.input), /binding changed/);
  assert.deepEqual(env.calls, []);

  const g = fixture();
  await g.activation.prepare();
  await g.activation.stop();
  await assert.rejects(g.activation.restoreForBinding(g.input), /stopped/);
  await assert.rejects(g.activation.activate({}), /stopped/);
  assert.deepEqual(g.calls, []);

  const h = fixture();
  await h.activation.prepare();
  h.input.onPhase = (name) => { if (name === "cold_prepare_ack") h.abort.abort(new Error("aborted")); };
  await assert.rejects(h.activation.restoreForBinding(h.input), /aborted/);
  assert.deepEqual(h.calls, ["cold", "cold-stop"]);
  await assert.rejects(h.activation.activate({}), /stopped/);
});

test("a stop during the spawn is terminal", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const calls = [];
  const binding = { actorId: "a", userId: "u" };
  const scope = { generation: "g", checkpoint() {} };
  const lifecycle = {
    snapshot: () => ({ state: "BINDING", generation: "g", binding, namespace: "n", resources: ["credentials"] }),
    assertBinding() {},
  };
  const activation = createGatewayActivation({
    env: { USER_ID: "a", INTERNAL_USER_ID: "u" },
    createCold: () => ({
      async prepare() { calls.push("cold"); await gate; },
      async activate() { calls.push("cold-activate"); },
      async stop() { calls.push("cold-stop"); },
    }),
  });
  await activation.prepare();
  const binding_ = activation.restoreForBinding({ lifecycle, scope, namespace: "n" });
  await new Promise((resolve) => setImmediate(resolve));
  await activation.stop();
  release();
  await assert.rejects(binding_, /stopped/);
  await assert.rejects(activation.activate({}), /stopped/);
  assert.deepEqual(calls.filter((call) => call !== "cold-stop"), ["cold"]);
  assert.ok(calls.includes("cold-stop"));
});

test("phase telemetry failures do not change the path", async () => {
  const f = fixture();
  await f.activation.prepare();
  f.input.onPhase = () => { throw new Error("telemetry down"); };
  assert.equal((await f.activation.restoreForBinding(f.input)).status, "cold");
  assert.deepEqual(f.calls, ["cold"]);
});
