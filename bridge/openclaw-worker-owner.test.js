const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { createPreparedGateway } = require("./openclaw-worker-owner");

const seed = {
  PATH: "/usr/bin", HOME: "/root", OPENCLAW_STATE_DIR: "/root/.openclaw",
  OPENCLAW_CONFIG_PATH: "/root/.openclaw/openclaw.json",
  OPENCLAW_WORKSPACE_DIR: "/root/.openclaw/workspace",
  AWS_CONFIG_FILE: "/tmp/scoped-creds/scoped-aws-config",
  AWS_EC2_METADATA_DISABLED: "true",
};
const binding = {
  userId: "user-123", actorId: "telegram:123",
  runtimeSessionId: "session-123", routingGeneration: 3,
};

function fixture(t, options = {}) {
  const child = new EventEmitter();
  Object.assign(child, {
    connected: true, exitCode: null, signalCode: null,
    messages: [], kills: [],
    kill(signal) { this.kills.push(signal); },
    send(message, callback) { this.messages.push(message); callback?.(); },
  });
  const spawns = [];
  const failures = [];
  const owner = createPreparedGateway({
    env: seed, onFailure: (error) => failures.push(error),
    spawn(...args) { spawns.push(args); return child; },
    ...options,
  });
  t.after(() => owner.stop());
  return { owner, child, spawns, failures };
}

async function prepared(f) {
  const promise = f.owner.prepare();
  f.child.emit("message", { type: "openclaw:prepared", version: "2026.9.5" });
  await promise;
}

function activation(overrides = {}) {
  return {
    env: { ...seed, USER_ID: binding.actorId, INTERNAL_USER_ID: binding.userId },
    binding, generation: "generation-123", deadline: Date.now() + 1000,
    ...overrides,
  };
}

test("childOptions (uid/gid) are spread into the gateway spawn", async (t) => {
  const f = fixture(t, { childOptions: { uid: 10001, gid: 10001 } });
  f.owner.prepare().catch(() => {});
  await Promise.resolve();
  const [, , spawnOptions] = f.spawns[0];
  assert.equal(spawnOptions.uid, 10001);
  assert.equal(spawnOptions.gid, 10001);
  // The credential-free seed env is still present alongside the uid/gid.
  assert.equal(spawnOptions.env.OPENCLAW_NO_RESPAWN, "1");
});

test("no uid/gid is set when childOptions is omitted", async (t) => {
  const f = fixture(t);
  f.owner.prepare().catch(() => {});
  await Promise.resolve();
  const [, , spawnOptions] = f.spawns[0];
  assert.equal(spawnOptions.uid, undefined);
  assert.equal(spawnOptions.gid, undefined);
});

test("preparation waits for the child and preserves that child for activation", async (t) => {
  const f = fixture(t);
  let ready = false;
  const promise = f.owner.prepare();
  assert.strictEqual(f.owner.prepare(), promise);
  promise.then(() => { ready = true; });
  await Promise.resolve();
  assert.equal(ready, false);
  assert.equal(f.spawns.length, 1);
  const [command, args, spawnOptions] = f.spawns[0];
  assert.equal(command, process.execPath);
  assert.deepEqual(args, [
    "--import", `${__dirname}/openclaw-preinit.mjs`,
    "/usr/local/lib/node_modules/openclaw/openclaw.mjs",
    "gateway", "run", "--port", "18789", "--verbose",
  ]);
  assert.equal(spawnOptions.env.USER_ID, undefined);
  assert.equal(spawnOptions.env.AWS_ACCESS_KEY_ID, undefined);
  assert.equal(spawnOptions.env.OPENCLAW_NO_RESPAWN, "1");
  assert.equal(spawnOptions.env.NODE_DISABLE_COMPILE_CACHE, "1");
  assert.equal(spawnOptions.stdio[3], "ipc");
  f.child.emit("message", { type: "openclaw:prepared", version: "2026.9.5" });
  await promise;
  const activating = f.owner.activate(activation());
  assert.equal(f.child.messages[0].env.USER_ID, binding.actorId);
  assert.deepEqual(f.child.messages[0].binding, binding);
  f.child.emit("message", { type: "openclaw:activated", generation: "generation-123" });
  await activating;
  assert.equal(f.spawns.length, 1);
  assert.strictEqual(f.owner.child, f.child);
  assert.equal(f.failures.length, 0);
});

test("preparation rejects identity and execution role credentials", () => {
  for (const key of ["USER_ID", "INTERNAL_USER_ID", "AWS_ACCESS_KEY_ID",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_WEB_IDENTITY_TOKEN_FILE"]) {
    assert.throws(() => createPreparedGateway({ env: { ...seed, [key]: "unbound-value" } }),
      /identity or credentials/);
  }
});

test("activation cannot be queued before preparation finishes", async (t) => {
  const f = fixture(t);
  const preparing = f.owner.prepare();
  const failedPreparation = assert.rejects(preparing, /prepared before activation/);
  await assert.rejects(f.owner.activate(activation()), /prepared before activation/);
  await failedPreparation;
  assert.equal(f.child.messages.length, 0);
  assert.deepEqual(f.child.kills, ["SIGTERM"]);
});

test("activation rejects path changes, credential sources, and mismatched identity", async (t) => {
  for (const change of [
    { HOME: "/different-user" },
    { OPENCLAW_STATE_DIR: "/mnt/workspace/other" },
    { AWS_CONFIG_FILE: "/root/.aws/config" },
    { AWS_CONTAINER_CREDENTIALS_FULL_URI: "http://credentials.invalid" },
    { USER_ID: "telegram:other" },
  ]) {
    const f = fixture(t);
    await prepared(f);
    const input = activation();
    Object.assign(input.env, change);
    await assert.rejects(f.owner.activate(input), /environment changed|identity does not match/);
    assert.equal(f.child.messages.length, 0);
    assert.equal(f.failures.length, 1);
  }
});

test("acknowledgements must match the pinned version and activation generation", async (t) => {
  const first = fixture(t);
  const preparing = first.owner.prepare();
  first.child.emit("message", { type: "openclaw:prepared", version: "other" });
  await assert.rejects(preparing, /Unexpected/);
  const second = fixture(t);
  await prepared(second);
  const activating = second.owner.activate(activation());
  second.child.emit("message", { type: "openclaw:activated", generation: "other-generation" });
  await assert.rejects(activating, /Unexpected/);
});

test("one prepared process cannot be activated twice", async (t) => {
  const f = fixture(t);
  await prepared(f);
  const activating = f.owner.activate(activation());
  f.child.emit("message", { type: "openclaw:activated", generation: "generation-123" });
  await activating;
  await assert.rejects(f.owner.activate(activation()), /prepared before activation/);
  assert.equal(f.child.messages.length, 1);
});

test("preparation timeout terminates the child and reports failure", async (t) => {
  const f = fixture(t, { prepareTimeoutMs: 5 });
  await assert.rejects(f.owner.prepare(), /timed out/);
  assert.deepEqual(f.child.kills, ["SIGTERM"]);
  assert.equal(f.failures.length, 1);
});

test("activation deadline begins after preparation and can expire independently", async (t) => {
  t.mock.timers.enable({ apis: ["Date", "setTimeout"] });
  const f = fixture(t);
  await prepared(f);
  const expired = assert.rejects(f.owner.activate(activation({ deadline: Date.now() + 5 })), /timed out/);
  t.mock.timers.tick(5);
  await expired;
  assert.equal(f.child.messages.length, 1);
  assert.equal(f.failures.length, 1);
});

test("aborting activation rejects the wait and ignores a late acknowledgement", async (t) => {
  const f = fixture(t);
  await prepared(f);
  const controller = new AbortController();
  const activating = f.owner.activate(activation({ signal: controller.signal }));
  controller.abort();
  await assert.rejects(activating, /aborted/);
  f.child.emit("message", { type: "openclaw:activated", generation: "generation-123" });
  assert.equal(f.failures.length, 1);
  assert.deepEqual(f.child.kills, ["SIGTERM"]);
});

test("child exit or IPC loss fails pending preparation", async (t) => {
  for (const event of ["exit", "disconnect", "error"]) {
    const f = fixture(t);
    const preparing = f.owner.prepare();
    f.child.emit(event, event === "error" ? new Error("spawn failed") : 1);
    await assert.rejects(preparing, /Prepared gateway/);
    assert.equal(f.failures.length, 1);
  }
});

test("stop rejects pending work without reporting an unexpected failure", async (t) => {
  const f = fixture(t);
  const preparing = f.owner.prepare();
  f.owner.stop();
  f.owner.stop();
  await assert.rejects(preparing, /stopped/);
  assert.deepEqual(f.child.kills, ["SIGTERM"]);
  assert.equal(f.failures.length, 0);
});

test("failed activation delivery cannot leave a prepared process serving", async (t) => {
  const f = fixture(t);
  await prepared(f);
  f.child.send = (_message, callback) => callback(new Error("closed"));
  await assert.rejects(f.owner.activate(activation()), /could not be delivered/);
  assert.deepEqual(f.child.kills, ["SIGTERM"]);
});

test("a stopped or lost prepared child cannot reuse its successful preparation", async (t) => {
  for (const event of ["stop", "exit", "disconnect"]) {
    const f = fixture(t);
    await prepared(f);
    if (event === "stop") f.owner.stop();
    else f.child.emit(event);
    await assert.rejects(f.owner.prepare(), /unavailable/);
    assert.equal(f.spawns.length, 1);
  }
});
