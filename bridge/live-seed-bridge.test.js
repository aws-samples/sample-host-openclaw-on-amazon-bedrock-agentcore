const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createLiveSeedBridge, readGatewayToken } = require("./live-seed-bridge");
const { renderConfig, renderAgents } = require("./gateway-config");
const stateStorage = require("./state-storage");

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function fixture(t, changes = {}) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "live-seed-bridge-"));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  const paths = stateStorage.resolvePaths({ homeDir: home });
  const binding = Object.freeze({ actorId: "slack:test-user", userId: "test-user" });
  const env = { HOME: home, USER_ID: binding.actorId, INTERNAL_USER_ID: binding.userId };
  const abort = new AbortController();
  const scope = { generation: "test-generation", signal: abort.signal,
    checkpoint: () => abort.signal.throwIfAborted() };
  const current = { state: "BINDING", generation: scope.generation, binding, resources: ["credentials"] };
  const lifecycle = { snapshot: () => current, assertBinding: (value) => assert.equal(value, current.binding) };
  const calls = [];
  let wasBound = false;
  let restoreReady = false;
  const child = { pid: 1234, exitCode: null, signalCode: null };
  const seedId = "test-seed";
  const owner = {
    child,
    layout: { home, stateDir: paths.stateDir, configPath: path.join(paths.stateDir, "openclaw.json"),
      workspaceDir: paths.workspaceDir, cwd: home, port: 18789,
      controlDir: "/tmp/live-gateway-seed-control", pid: child.pid, seedId },
    async prepare({ renderedConfig, agentsText }) {
      calls.push("prepare");
      fs.mkdirSync(paths.workspaceDir, { recursive: true });
      fs.writeFileSync(this.layout.configPath, JSON.stringify(renderedConfig));
      fs.writeFileSync(path.join(paths.stateDir, "AGENTS.md"), agentsText);
      return { status: "prepared", prepared: true, pid: child.pid, seedId };
    },
    async bind(input) {
      calls.push("bind");
      assert.deepEqual(Object.keys(input).sort(), [
        "absenceProof", "actorId", "boundEnv", "channel", "gatewayToken",
        "generation", "renderedConfig", "userId",
      ]);
      assert.equal(input.absenceProof.restoreGateVerified, true);
      wasBound = true;
      return { status: "bound", bound: true, generation: scope.generation,
        userReady: false, pid: child.pid, seedId };
    },
    async stop() {
      calls.push("stop");
      return { status: "stopped", cleanupConfirmed: true, seedInventoryRemoved: !wasBound };
    },
  };
  const workspaceSync = {
    async restoreAbsentSeed(_namespace, { bindSeed }) {
      calls.push("restore");
      await bindSeed({ restoreGateVerified: true });
      restoreReady = true;
      return { status: "adopted" };
    },
    awaitRestore: (promise) => promise,
    assertRestoreReady() { assert.equal(restoreReady, true); },
  };
  const options = {
    createOwner: async () => owner,
    createCold: () => ({
      async prepare() { calls.push("cold"); },
      async activate() { calls.push("cold-activate"); },
      async stop() { calls.push("cold-stop"); },
    }),
    render: (token) => ({
      config: renderConfig({ gatewayToken: token }), agentsText: renderAgents({ browserEnabled: true }),
    }),
    readToken: async () => "test-gateway-token",
    stateStorage, workspaceSync, neutralEnv: {}, env, cwd: home,
    onChild: () => calls.push("child"),
    inspectLocalEligibility: () => true,
    ...changes,
  };
  const bridge = createLiveSeedBridge(options);
  const input = { lifecycle, scope, namespace: "slack_test-user", channel: "slack",
    token: "test-gateway-token", boundEnv: { ...env, AWS_CONFIG_FILE: "/tmp/test-config",
      AWS_SDK_LOAD_CONFIG: "1" }, waitMs: 1000 };
  return { bridge, owner, calls, input, current, abort, paths, workspaceSync };
}

test("adoption retains full config/AGENTS and the same child through activation", async (t) => {
  const f = fixture(t);
  await f.bridge.prepare();
  const before = ["openclaw.json", "AGENTS.md"].map((name) =>
    fs.readFileSync(path.join(f.paths.stateDir, name), "utf8"));
  await f.bridge.restoreForBinding(f.input);
  await f.bridge.activate({ generation: f.input.scope.generation,
    binding: f.input.lifecycle.snapshot().binding, env: f.input.boundEnv, signal: f.abort.signal });
  assert.equal(f.bridge.child, f.owner.child);
  assert.deepEqual(f.calls, ["prepare", "child", "restore", "bind"]);
  assert.deepEqual(["openclaw.json", "AGENTS.md"].map((name) =>
    fs.readFileSync(path.join(f.paths.stateDir, name), "utf8")), before);
  assert.equal((await f.bridge.stop()).seedInventoryRemoved, false);
});

test("a stop during token read prevents owner creation and cold transition", async (t) => {
  const token = deferred();
  let creates = 0;
  const f = fixture(t, { readToken: () => token.promise, createOwner: () => { creates++; } });
  const preparing = assert.rejects(f.bridge.prepare(), /stopped/);
  await f.bridge.stop();
  token.resolve("test-gateway-token");
  await preparing;
  assert.equal(creates, 0);
  assert.deepEqual(f.calls, []);
});

test("a late-created owner is stopped without preparing or publishing its child", async (t) => {
  const owner = deferred();
  const entered = deferred();
  const f = fixture(t, { createOwner: () => { entered.resolve(); return owner.promise; } });
  const preparing = assert.rejects(f.bridge.prepare(), /stopped/);
  await entered.promise;
  await f.bridge.stop();
  owner.resolve(f.owner);
  await preparing;
  assert.deepEqual(f.calls, ["stop"]);
});

for (const mismatch of ["token", "agents", "local", "namespace"]) {
  test(`${mismatch} mismatch stops an unused seed before the cold path`, async (t) => {
    const f = fixture(t, mismatch === "local" ? { inspectLocalEligibility: () => false } : {});
    await f.bridge.prepare();
    if (mismatch === "token") f.input.token = "changed";
    if (mismatch === "agents") fs.appendFileSync(path.join(f.paths.stateDir, "AGENTS.md"), "\nchanged");
    if (mismatch === "namespace") {
      f.workspaceSync.restoreAbsentSeed = async () => ({ status: "ineligible", reason: "namespace_content" });
    }
    assert.equal((await f.bridge.restoreForBinding(f.input)).status, "cold");
    assert.ok(!f.calls.includes("bind"));
    assert.ok(f.calls.indexOf("stop") < f.calls.indexOf("cold"));
  });
}

test("an uncertain bind cannot restore over a possibly adopted seed", async (t) => {
  const f = fixture(t);
  await f.bridge.prepare();
  f.owner.bind = async () => ({ status: "declined", adoptionStatus: "unknown" });
  f.owner.stop = async () => {
    f.calls.push("stop");
    return { status: "stopped", cleanupConfirmed: true, seedInventoryRemoved: false };
  };
  f.workspaceSync.restoreAbsentSeed = async (_namespace, { bindSeed }) => {
    await bindSeed({ restoreGateVerified: true });
    return { status: "ineligible", reason: "owner_declined" };
  };
  await assert.rejects(f.bridge.restoreForBinding(f.input), /Unused live seed state remains/);
  assert.ok(!f.calls.includes("cold"));
});

test("actor/generation change or abort after bind cannot activate or discard bound state", async (t) => {
  for (const change of ["actor", "generation", "abort"]) {
    const f = fixture(t);
    await f.bridge.prepare();
    const original = f.owner.bind.bind(f.owner);
    f.owner.bind = async (input) => {
      const reply = await original(input);
      if (change === "actor") f.current.binding = { actorId: "slack:other", userId: "other" };
      if (change === "generation") f.current.generation = "different";
      if (change === "abort") f.abort.abort(new Error("aborted"));
      return reply;
    };
    await assert.rejects(f.bridge.restoreForBinding(f.input), /changed|aborted/);
    assert.equal((await f.bridge.stop()).seedInventoryRemoved, false);
    assert.ok(!f.calls.includes("cold"));
    await assert.rejects(f.bridge.activate({}), /stopped/);
  }
});

test("common token lookup closes its short-lived client on success and failure", async () => {
  for (const fail of [false, true]) {
    let destroyed = 0;
    class Client {
      constructor(input) { assert.equal(input.maxAttempts, 1); }
      async send() {
        if (fail) throw new Error("lookup failed");
        return { SecretString: "test-token" };
      }
      destroy() { destroyed++; }
    }
    const request = readGatewayToken({ SecretsManagerClient: Client,
      GetSecretValueCommand: class {}, env: { GATEWAY_TOKEN_SECRET_ID: "test-secret" } });
    if (fail) await assert.rejects(request, /lookup failed/);
    else assert.equal(await request, "test-token");
    assert.equal(destroyed, 1);
  }
});
