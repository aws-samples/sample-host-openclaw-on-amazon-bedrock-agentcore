const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createLiveSeedBridge } = require("./live-seed-bridge");
const { renderConfig, renderAgents } = require("./gateway-config");
const stateStorage = require("./state-storage");

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

// Same prepare/stop contract as openclaw-worker-owner: prepare() is cached, and rejects
// once the gateway has failed or stopped.
function coldGateway(calls, name, { ack = Promise.resolve(), onPrepare } = {}) {
  let state = "NEW", promise, failAck;
  const child = { pid: name.length + 4000, name };
  return {
    name, child,
    prepare() {
      if (state === "FAILED" || state === "STOPPED") return Promise.reject(new Error("Prepared gateway is unavailable"));
      if (promise) return promise;
      state = "PREPARING";
      calls.push(`${name}:spawn`);
      onPrepare?.();
      promise = new Promise((resolve, reject) => {
        failAck = reject;
        ack.then(() => {
          if (state !== "PREPARING") return;
          state = "PREPARED";
          calls.push(`${name}:ack`);
          resolve();
        }, (error) => { state = "FAILED"; reject(error); });
      });
      return promise;
    },
    fail() { state = "FAILED"; calls.push(`${name}:failed`); failAck?.(new Error("Prepared gateway exited before activation")); },
    async activate() { calls.push(`${name}:activate`); },
    stop() {
      if (state === "STOPPED") return;
      state = "STOPPED";
      calls.push(`${name}:stop`);
      failAck?.(new Error("Prepared gateway stopped"));
    },
    get state() { return state; },
  };
}

function fixture(t, { standbyAck, standbyOnPrepare, freshAck, restore, coldStandby = true } = {}) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "live-seed-standby-"));
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
  const colds = [];
  const children = [];
  let wasBound = false, restoreReady = false;
  const seedChild = { pid: 1234, exitCode: null, signalCode: null };
  const owner = {
    child: seedChild,
    layout: { home, stateDir: paths.stateDir, configPath: path.join(paths.stateDir, "openclaw.json"),
      workspaceDir: paths.workspaceDir, cwd: home, port: 18789,
      controlDir: "/tmp/live-gateway-seed-control", pid: seedChild.pid, seedId: "test-seed" },
    async prepare({ renderedConfig, agentsText }) {
      calls.push("seed:prepare");
      // The real owner refuses a state dir with anything but an empty workspace in it.
      if (fs.existsSync(paths.stateDir)) {
        const names = fs.readdirSync(paths.stateDir);
        if (names.some((name) => name !== "workspace")) {
          return { status: "fallback", prepared: false, reason: "initial_state_not_empty" };
        }
      }
      fs.mkdirSync(paths.workspaceDir, { recursive: true });
      fs.writeFileSync(this.layout.configPath, JSON.stringify(renderedConfig));
      fs.writeFileSync(path.join(paths.stateDir, "AGENTS.md"), agentsText);
      return { status: "prepared", prepared: true, pid: seedChild.pid, seedId: "test-seed" };
    },
    async bind() {
      calls.push("seed:bind");
      wasBound = true;
      return { status: "bound", bound: true, generation: scope.generation, userReady: false,
        pid: seedChild.pid, seedId: "test-seed" };
    },
    async stop() {
      calls.push("seed:stop");
      if (!wasBound) {
        fs.rmSync(path.join(paths.stateDir, "openclaw.json"), { force: true });
        fs.rmSync(path.join(paths.stateDir, "AGENTS.md"), { force: true });
      }
      return { status: "stopped", cleanupConfirmed: true, seedInventoryRemoved: !wasBound };
    },
  };
  const workspaceSync = {
    async restoreAbsentSeed(_namespace, { bindSeed }) {
      calls.push("seed:restore");
      if (restore) return restore();
      await bindSeed({ restoreGateVerified: true });
      restoreReady = true;
      return { status: "adopted" };
    },
    awaitRestore: (promise) => promise,
    assertRestoreReady() { assert.equal(restoreReady, true); },
  };
  const bridge = createLiveSeedBridge({
    createOwner: async () => owner,
    createCold: (options) => {
      const standby = options?.standby === true;
      const gateway = coldGateway(calls, standby ? "standby" : `fresh${colds.length}`,
        standby ? { ack: standbyAck, onPrepare: standbyOnPrepare } : { ack: freshAck });
      colds.push(gateway);
      return gateway;
    },
    render: (token) => ({
      config: renderConfig({ gatewayToken: token }), agentsText: renderAgents({ browserEnabled: true }),
    }),
    readToken: async () => "test-gateway-token",
    stateStorage, workspaceSync, neutralEnv: {}, env, cwd: home,
    onChild: (child) => { children.push(child); calls.push(`observe:${child.name || "seed"}`); },
    inspectLocalEligibility: () => true, coldStandby, log: (line) => calls.push(`log:${line.split(" ").slice(0, 4).join(" ")}`),
  });
  const phases = [];
  const input = { lifecycle, scope, namespace: "slack_test-user", channel: "slack",
    token: "test-gateway-token", boundEnv: { ...env, AWS_CONFIG_FILE: "/tmp/test-config" }, waitMs: 1000,
    onPhase: (name, details) => phases.push(details?.source ? `${name}:${details.source}` : name) };
  const savedState = () => { workspaceSync.restoreAbsentSeed = async () => ({ status: "ineligible", reason: "namespace_content" }); };
  return { bridge, calls, colds, children, input, phases, paths, abort, savedState, workspaceSync };
}

const activation = (f) => ({ generation: f.input.scope.generation, binding: f.input.lifecycle.snapshot().binding,
  env: f.input.boundEnv, signal: f.abort.signal });

test("bootstrap prepares the standby against an empty state dir before the seed writes", async (t) => {
  const ack = deferred();
  const f = fixture(t, { standbyAck: ack.promise });
  const preparing = f.bridge.prepare();
  await new Promise(setImmediate);
  assert.deepEqual(f.calls, ["standby:spawn"], "the seed must wait for the standby acknowledgement");
  assert.equal(fs.existsSync(path.join(f.paths.stateDir, "openclaw.json")), false);
  ack.resolve();
  assert.equal((await preparing).status, "prepared");
  assert.deepEqual(f.calls.filter((call) => !call.startsWith("log:")),
    ["standby:spawn", "standby:ack", "seed:prepare", "observe:seed"]);
  assert.ok(f.calls.includes("log:[live-seed] cold standby prepared"));
  assert.equal(f.bridge.standbyPrepared, true);
});

test("a saved-state user gets the standby, promoted only after the unused seed is gone", async (t) => {
  const f = fixture(t);
  await f.bridge.prepare();
  f.savedState();
  assert.equal((await f.bridge.restoreForBinding(f.input)).status, "cold");
  const order = f.calls.filter((call) => !call.startsWith("log:"));
  assert.ok(order.indexOf("seed:stop") < order.indexOf("observe:standby"));
  assert.equal(f.colds.length, 1, "no fresh gateway is spawned");
  assert.deepEqual(f.phases.slice(-2), ["cold_prepare_start", "cold_prepare_ack:standby"]);
  assert.equal(f.children.at(-1), f.colds[0].child);
  assert.equal(f.bridge.child, f.colds[0].child);
  assert.equal(f.bridge.standbyPrepared, false);
  // The seed's files are gone before anything restores user state.
  assert.equal(fs.existsSync(path.join(f.paths.stateDir, "openclaw.json")), false);
  await f.bridge.activate(activation(f));
  assert.ok(f.calls.includes("standby:activate"));
  await f.bridge.stop();
  assert.ok(f.calls.includes("standby:stop"));
});

test("an adopted seed stops the unused standby and never activates it", async (t) => {
  const f = fixture(t);
  await f.bridge.prepare();
  assert.equal((await f.bridge.restoreForBinding(f.input)).status, "adopted");
  assert.ok(f.calls.indexOf("seed:bind") < f.calls.indexOf("standby:stop"));
  assert.equal(f.bridge.standbyPrepared, false);
  await f.bridge.activate(activation(f));
  assert.ok(!f.calls.includes("standby:activate"));
  assert.ok(!f.calls.includes("observe:standby"));
});

test("a standby that fails during the seed's restore check is replaced by a fresh gateway", async (t) => {
  const check = deferred();
  const f = fixture(t, { restore: () => check.promise });
  await f.bridge.prepare();
  const restoring = f.bridge.restoreForBinding(f.input);
  await new Promise(setImmediate);
  f.colds[0].fail();
  check.resolve({ status: "ineligible", reason: "namespace_content" });
  assert.equal((await restoring).status, "cold");
  assert.equal(f.colds.length, 2);
  assert.deepEqual(f.phases.slice(-1), ["cold_prepare_ack:fresh_after_standby"]);
  assert.equal(f.bridge.child, f.colds[1].child);
  assert.ok(!f.calls.includes("observe:standby"));
  await f.bridge.activate(activation(f));
  assert.ok(f.calls.includes("fresh1:activate"));
  assert.ok(!f.calls.includes("standby:activate"));
});

test("a fresh prepare failure after a failed standby rejects the binding", async (t) => {
  const f = fixture(t, { freshAck: Promise.reject(new Error("fresh prepare failed")) });
  await f.bridge.prepare();
  f.colds[0].fail();
  f.savedState();
  await assert.rejects(f.bridge.restoreForBinding(f.input), /fresh prepare failed/);
  await assert.rejects(f.bridge.activate(activation(f)), /stopped/);
});

test("a standby that cannot prepare leaves the seed path and later uses a fresh gateway", async (t) => {
  const f = fixture(t, { standbyAck: Promise.reject(new Error("no ack")) });
  assert.equal((await f.bridge.prepare()).status, "prepared");
  assert.ok(f.calls.includes("log:[live-seed] cold standby unavailable;"));
  assert.equal(f.bridge.standbyPrepared, false);
  f.savedState();
  assert.equal((await f.bridge.restoreForBinding(f.input)).status, "cold");
  assert.deepEqual(f.phases.slice(-1), ["cold_prepare_ack:fresh"]);
});

test("a standby that writes into the state dir fails bootstrap before the seed exists", async (t) => {
  const g = fixture(t, { standbyOnPrepare: () => {
    fs.mkdirSync(g.paths.stateDir, { recursive: true });
    fs.writeFileSync(path.join(g.paths.stateDir, "openclaw.json"), "{}");
  } });
  await assert.rejects(g.bridge.prepare(), /Cold standby gateway wrote state/);
  assert.ok(g.calls.includes("standby:stop"));
  assert.ok(!g.calls.includes("seed:prepare"));
  assert.equal(g.bridge.standbyPrepared, false);
});

test("stop while the standby prepares stops it and creates no seed", async (t) => {
  const ack = deferred();
  const f = fixture(t, { standbyAck: ack.promise });
  const preparing = assert.rejects(f.bridge.prepare(), /stopped/);
  await new Promise(setImmediate);
  await f.bridge.stop();
  ack.resolve();
  await preparing;
  assert.ok(f.calls.includes("standby:stop"));
  assert.ok(!f.calls.includes("seed:prepare"));
});

test("an abort after promotion stops the standby and nothing activates", async (t) => {
  const f = fixture(t);
  await f.bridge.prepare();
  f.savedState();
  const original = f.input.onPhase;
  f.input.onPhase = (name, details) => {
    original(name, details);
    if (name === "cold_prepare_ack") f.abort.abort(new Error("aborted"));
  };
  await assert.rejects(f.bridge.restoreForBinding(f.input), /aborted/);
  assert.ok(f.calls.includes("standby:stop"));
  await assert.rejects(f.bridge.activate(activation(f)), /stopped/);
});

test("without coldStandby the cold path still spawns its gateway after the seed stops", async (t) => {
  const f = fixture(t, { coldStandby: false });
  await f.bridge.prepare();
  assert.ok(!f.calls.includes("standby:spawn"));
  f.savedState();
  assert.equal((await f.bridge.restoreForBinding(f.input)).status, "cold");
  assert.ok(f.calls.indexOf("seed:stop") < f.calls.indexOf("fresh0:spawn"));
  assert.deepEqual(f.phases.slice(-1), ["cold_prepare_ack:fresh"]);
});
