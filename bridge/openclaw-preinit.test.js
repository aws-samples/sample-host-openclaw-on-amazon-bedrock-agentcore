/**
 * Offline unit tests: all OpenClaw imports and child IPC are injected stubs.
 * Run: node --test openclaw-preinit.test.js
 */
const { before, describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { setImmediate: nextTurn } = require("node:timers/promises");

let createGatewayPreparer, LAUNCHER_URL, PACKAGE_ROOT, RUNTIME_FILES;
before(async () => {
  ({ createGatewayPreparer, LAUNCHER_URL, PACKAGE_ROOT, RUNTIME_FILES } =
    await import("./openclaw-preinit.mjs"));
});

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness(options = {}) {
  const child = new EventEmitter();
  child.connected = true;
  child.argv = ["node", `${PACKAGE_ROOT}/openclaw.mjs`, "gateway", "run", "--port", "18789", "--verbose"];
  child.env = {
    HOME: "/root", PATH: "/usr/local/bin:/usr/bin:/bin",
    OPENCLAW_STATE_DIR: "/root/.openclaw", OPENCLAW_CONFIG_PATH: "/root/.openclaw/openclaw.json",
    OPENCLAW_WORKSPACE_DIR: "/root/.openclaw/workspace",
    AWS_CONFIG_FILE: "/tmp/scoped-creds/scoped-aws-config", AWS_SDK_LOAD_CONFIG: "1",
    AWS_EC2_METADATA_DISABLED: "true",
    OPENCLAW_AGENTCORE_PREINIT: "1", OPENCLAW_NO_RESPAWN: "1", NODE_DISABLE_COMPILE_CACHE: "1",
  };
  const seed = structuredClone(child.env);
  const calls = [];
  const sent = [];
  const exits = [];
  const prepared = deferred();
  const activated = deferred();
  child.exit = (code) => { exits.push(code); child.emit("exit", code); };
  child.send = (message, callback) => {
    calls.push(message.type);
    sent.push(structuredClone(message));
    if (message.type === "openclaw:prepared") prepared.resolve();
    if (message.type === "openclaw:activated") activated.resolve();
    if (options.send) return options.send(message, callback, child);
    queueMicrotask(() => callback(null));
    return true;
  };
  const prepare = createGatewayPreparer({
    child,
    createInvariant: options.createInvariant,
    readFile(file) {
      assert.equal(file, `${PACKAGE_ROOT}/package.json`, "No user config or credential file may be read.");
      return options.manifest ?? JSON.stringify({ name: "openclaw", version: "2026.9.5" });
    },
    realpath: options.realpath ?? ((file) => file),
    importModule: async (url) => {
      calls.push(url);
      if (options.importModule) return options.importModule(url, child);
      if (url.endsWith(RUNTIME_FILES.server)) return {};
      if (url.endsWith(RUNTIME_FILES.reply)) return {
        prewarmConfigDrivenReplyRuntime: async () => {
          calls.push("prewarm");
          await options.prewarm?.(child);
        },
      };
      if (url.endsWith(RUNTIME_FILES.presence)) return {
        resetAgentCoreSnapshotPresence: async () => {
          calls.push("reset");
          assert.equal(child.env.USER_ID, "actor-one");
          assert.equal(child.env.INTERNAL_USER_ID, "user-one");
          assert.equal(child.env.OPENCLAW_AGENTCORE_PREINIT, "1");
          await options.reset?.(child);
        },
      };
      assert.fail(`Unexpected import: ${url}`);
    },
  });
  const message = () => ({
    type: "openclaw:activate", generation: "generation-one",
    binding: { actorId: "actor-one", userId: "user-one", routingGeneration: 0, runtimeSessionId: null },
    env: { ...seed, USER_ID: "actor-one", INTERNAL_USER_ID: "user-one" },
  });
  const disconnect = () => { child.connected = false; child.emit("disconnect"); };
  return { child, seed, calls, sent, exits, prepared, activated, prepare, message, disconnect };
}

function cleanFailure(h) {
  for (const event of ["message", "disconnect", "error", "exit"]) {
    assert.equal(h.child.listenerCount(event), 0, event);
  }
  assert.equal(h.sent.some((message) => message.type === "openclaw:activated"), false);
}

describe("same-process preparation and activation", () => {
  it("awaits every import and actual reply prewarm before announcing prepared", async () => {
    const server = deferred(), reply = deferred(), prewarm = deferred(), presence = deferred();
    const calls = [];
    const h = harness({
      importModule: async (url, child) => {
        if (url.endsWith(RUNTIME_FILES.server)) { calls.push("server"); return server.promise; }
        if (url.endsWith(RUNTIME_FILES.reply)) { calls.push("reply"); return reply.promise; }
        if (url.endsWith(RUNTIME_FILES.presence)) {
          calls.push("presence");
          await presence.promise;
          return { resetAgentCoreSnapshotPresence() {
            assert.equal(child.env.USER_ID, "actor-one");
            calls.push("reset");
          } };
        }
        assert.fail("Unexpected import");
      },
    });
    let completed = false;
    const done = h.prepare(LAUNCHER_URL).then(() => { completed = true; });
    await nextTurn();
    assert.deepEqual(calls, ["server"]);
    assert.deepEqual(h.sent, []);
    server.resolve({});
    await nextTurn();
    assert.deepEqual(calls, ["server", "reply"]);
    reply.resolve({ prewarmConfigDrivenReplyRuntime() { calls.push("prewarm"); return prewarm.promise; } });
    await nextTurn();
    assert.deepEqual(calls, ["server", "reply", "prewarm"]);
    assert.deepEqual(h.sent, []);
    prewarm.resolve();
    await nextTurn();
    assert.deepEqual(calls, ["server", "reply", "prewarm", "presence"]);
    assert.deepEqual(h.sent, []);
    presence.resolve();
    await h.prepared.promise;
    await nextTurn();
    assert.equal(completed, false);
    assert.deepEqual(h.child.env, h.seed);
    assert.deepEqual(h.sent, [{ type: "openclaw:prepared", version: "2026.9.5" }]);
    const activation = h.message();
    h.child.emit("message", activation);
    // IPC input must be copied before any further asynchronous work.
    activation.generation = "mutated";
    activation.env.USER_ID = "other";
    await done;
    assert.equal(completed, true);
    const expected = { ...h.seed, USER_ID: "actor-one", INTERNAL_USER_ID: "user-one" };
    delete expected.OPENCLAW_AGENTCORE_PREINIT;
    assert.deepEqual(h.child.env, expected);
    assert.deepEqual(calls, ["server", "reply", "prewarm", "presence", "reset"]);
    assert.deepEqual(h.sent[1], { type: "openclaw:activated", generation: "generation-one" });
    assert.equal(h.child.listenerCount("disconnect"), 0);
    assert.equal(h.child.listenerCount("error"), 0);
    assert.equal(h.child.listenerCount("message"), 1, "Only the process-lifetime replay guard remains.");
    h.child.emit("exit", 0);
    assert.equal(h.child.listenerCount("message"), 0);
    assert.equal(h.child.listenerCount("exit"), 0);
  });

  it("has no child activation timer and accepts IPC backpressure", async (t) => {
    const h = harness({ send(message, callback) { queueMicrotask(() => callback(null)); return false; } });
    const timer = t.mock.method(global, "setTimeout", () => assert.fail("Child must not own a deadline."));
    const done = h.prepare(LAUNCHER_URL);
    await h.prepared.promise;
    await nextTurn();
    h.child.emit("message", h.message());
    await done;
    assert.equal(timer.mock.callCount(), 0);
    timer.mock.restore();
    h.child.emit("exit", 0);
  });

  it("consumes only the hook opt-in after reset and final ACK completion", async () => {
    let finishAck;
    const h = harness({
      send(message, callback, child) {
        if (message.type === "openclaw:activated") {
          assert.equal(child.env.OPENCLAW_AGENTCORE_PREINIT, "1");
          finishAck = callback;
        } else queueMicrotask(() => callback(null));
      },
    });
    const done = h.prepare(LAUNCHER_URL);
    await h.prepared.promise;
    h.child.emit("message", h.message());
    await h.activated.promise;
    assert.equal(h.calls.includes("reset"), true);
    assert.equal(h.child.env.OPENCLAW_AGENTCORE_PREINIT, "1");
    finishAck(null);
    await done;
    assert.equal(Object.hasOwn(h.child.env, "OPENCLAW_AGENTCORE_PREINIT"), false);
    assert.equal(h.child.env.OPENCLAW_NO_RESPAWN, "1");
    assert.equal(h.child.env.NODE_DISABLE_COMPILE_CACHE, "1");
    for (const [key, value] of Object.entries(h.seed)) {
      if (key !== "OPENCLAW_AGENTCORE_PREINIT") assert.equal(h.child.env[key], value, key);
    }
    // Consuming the opt-in does not retire the running process's replay guard.
    h.child.emit("message", h.message());
    assert.deepEqual(h.exits, [1]);
  });

  it("supports explicit runtime session IDs and nullable routing generations", async () => {
    for (const [routingGeneration, runtimeSessionId] of [[0, "runtime-one"], [null, null], [2, "runtime-two"]]) {
      const h = harness();
      const done = h.prepare(LAUNCHER_URL);
      await h.prepared.promise;
      const message = h.message();
      Object.assign(message.binding, { routingGeneration, runtimeSessionId });
      h.child.emit("message", message);
      await done;
      h.child.emit("exit", 0);
    }
  });

  it("accepts the scoped helper and owner seed with explicit final selectors using stub IPC", async (t) => {
    const { buildOpenClawEnv } = require("./scoped-credentials");
    const { gatewayStateEnv } = require("./state-storage");
    const { createPreparedGateway } = require("./openclaw-worker-owner");
    const baseEnv = {
      HOME: "/root", PATH: "/usr/local/bin:/usr/bin:/bin",
      AWS_EC2_METADATA_DISABLED: "false",
      AWS_CONTAINER_CREDENTIALS_RELATIVE_URI: "/not-forwarded",
      Z_AI_API_KEY: "not-forwarded",
    };
    const environment = () => ({
      ...buildOpenClawEnv({ credDir: "/tmp/scoped-creds", baseEnv }),
      ...gatewayStateEnv("/root/.openclaw"),
      HOME: "/root",
      OPENCLAW_CONFIG_PATH: "/root/.openclaw/openclaw.json",
      OPENCLAW_WORKSPACE_DIR: "/root/.openclaw/workspace",
    });
    const seed = environment();
    assert.equal(seed.AWS_EC2_METADATA_DISABLED, "true");
    assert.equal(Object.hasOwn(seed, "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"), false);
    assert.equal(Object.hasOwn(seed, "Z_AI_API_KEY"), false);
    const receiverCalls = [];
    const h = harness({ createInvariant: () => ({
      async prepare(seed) {
        assert.equal(seed.OPENCLAW_AGENTCORE_SQLITE_RECEIVER_PREPARE, "1");
        assert.equal(seed.OPENCLAW_AGENTCORE_SQLITE_BACKEND_PREPARE, "1");
        assert.equal(seed.USER_ID, undefined);
        receiverCalls.push("prepare");
      },
      proof() {},
      async bind(generation, environment) {
        assert.equal(environment.USER_ID, "actor-one");
        assert.equal(environment.INTERNAL_USER_ID, "user-one");
        receiverCalls.push("bind");
      },
      async close() {},
    }) });
    const parentChild = new EventEmitter();
    Object.assign(parentChild, { connected: true, exitCode: null, signalCode: null });
    const kills = [], failures = [];
    parentChild.kill = (signal) => { kills.push(signal); };
    parentChild.send = (message, callback) => {
      queueMicrotask(() => { h.child.emit("message", structuredClone(message)); callback(null); });
      return true;
    };
    h.child.send = (message, callback) => {
      queueMicrotask(() => { parentChild.emit("message", structuredClone(message)); callback(null); });
      return true;
    };
    const owner = createPreparedGateway({
      env: seed,
      prepareTimeoutMs: 1000,
      onFailure: (error) => { failures.push(error); },
      spawn(command, argv, options) {
        assert.equal(command, process.execPath);
        assert.deepEqual(argv.slice(0, 3), [
          "--import", `${__dirname}/openclaw-preinit.mjs`, `${PACKAGE_ROOT}/openclaw.mjs`,
        ]);
        assert.deepEqual(options.stdio, ["ignore", "pipe", "pipe", "ipc"]);
        h.child.argv = [command, ...argv.slice(2)];
        h.child.env = { ...options.env };
        return parentChild;
      },
    });
    t.after(() => { owner.stop(); h.child.emit("exit", 0); });
    const parentPrepared = owner.prepare();
    const childDone = h.prepare(LAUNCHER_URL);
    await parentPrepared;
    assert.equal(h.child.env.USER_ID, undefined);
    const finalEnv = { ...environment(), USER_ID: "actor-one", INTERNAL_USER_ID: "user-one" };
    await Promise.all([
      childDone,
      owner.activate({
        env: finalEnv, binding: h.message().binding, generation: "generation-one",
        deadline: Date.now() + 1000,
      }),
    ]);
    assert.equal(h.child.env.AWS_EC2_METADATA_DISABLED, "true");
    assert.equal(h.child.env.USER_ID, "actor-one");
    assert.equal(h.child.env.INTERNAL_USER_ID, "user-one");
    assert.equal(h.child.env.OPENCLAW_NO_RESPAWN, "1");
    assert.equal(Object.hasOwn(h.child.env, "OPENCLAW_AGENTCORE_PREINIT"), false);
    assert.deepEqual(failures, []);
    assert.deepEqual(kills, []);
    assert.deepEqual(receiverCalls, ["prepare", "bind"]);
  });

  it("never accepts an activation received before prepared, even when imports later finish", async () => {
    const server = deferred();
    const h = harness({ importModule: () => server.promise });
    const done = h.prepare(LAUNCHER_URL);
    const rejected = assert.rejects(done, /preparation or activation failed/);
    await nextTurn();
    h.child.emit("message", h.message());
    await rejected;
    server.resolve({});
    await nextTurn();
    assert.equal(h.calls.length, 1);
    assert.deepEqual(h.sent, []);
    assert.deepEqual(h.child.env, h.seed);
    cleanFailure(h);
  });

  it("rejects activation immediately after invocation instead of buffering it", async () => {
    const h = harness();
    const done = h.prepare(LAUNCHER_URL);
    h.child.emit("message", h.message());
    await assert.rejects(done);
    assert.deepEqual(h.calls, []);
    cleanFailure(h);
  });

  it("rejects repeats during activation and never performs a second preparation", async () => {
    const h = harness();
    const done = h.prepare(LAUNCHER_URL);
    await h.prepared.promise;
    h.child.emit("message", h.message());
    h.child.emit("message", h.message());
    await assert.rejects(done);
    await assert.rejects(h.prepare(LAUNCHER_URL), /one-shot/);
    assert.equal(h.calls.includes("reset"), false);
    cleanFailure(h);
  });

  it("terminates on a replay after success and releases its process-lifetime guard", async () => {
    const h = harness();
    const done = h.prepare(LAUNCHER_URL);
    await h.prepared.promise;
    h.child.emit("message", h.message());
    await done;
    h.child.emit("message", h.message());
    assert.deepEqual(h.exits, [1]);
    assert.equal(h.child.listenerCount("message"), 0);
    assert.equal(h.child.listenerCount("exit"), 0);
    assert.equal(h.calls.filter((call) => call === "reset").length, 1);
    await assert.rejects(h.prepare(LAUNCHER_URL), /one-shot/);
  });

  it("rejects repeat activation while presence reset is still pending", async () => {
    const gate = deferred(), entered = deferred();
    const h = harness({ reset() { entered.resolve(); return gate.promise; } });
    const done = h.prepare(LAUNCHER_URL);
    const rejected = assert.rejects(done);
    await h.prepared.promise;
    h.child.emit("message", h.message());
    await entered.promise;
    h.child.emit("message", h.message());
    await rejected;
    gate.resolve();
    await nextTurn();
    cleanFailure(h);
  });
});

describe("IPC failures and parent lifetime", () => {
  it("requires connected process.send before importing anything", async () => {
    for (const mutate of [(h) => { h.child.send = undefined; }, (h) => { h.child.connected = false; }]) {
      const h = harness();
      mutate(h);
      await assert.rejects(h.prepare(LAUNCHER_URL), /connected parent IPC/);
      assert.deepEqual(h.calls, []);
      cleanFailure(h);
    }
  });

  for (const phase of ["import", "prewarm", "waiting", "prepared-send", "reset", "activated-send"]) {
    it(`rejects parent disconnect during ${phase} and never returns to the CLI`, async () => {
      const gate = deferred(), entered = deferred();
      const options = {};
      if (phase === "import") options.importModule = () => { entered.resolve(); return gate.promise; };
      if (phase === "prewarm") options.prewarm = () => { entered.resolve(); return gate.promise; };
      if (phase === "reset") options.reset = () => { entered.resolve(); return gate.promise; };
      if (phase.endsWith("-send")) options.send = (message, callback) => {
        if (message.type === `openclaw:${phase.split("-")[0]}`) entered.resolve();
        else queueMicrotask(() => callback(null));
      };
      const h = harness(options);
      const done = h.prepare(LAUNCHER_URL);
      const rejected = assert.rejects(done, /preparation or activation failed/);
      if (["reset", "activated-send"].includes(phase)) {
        await h.prepared.promise;
        h.child.emit("message", h.message());
      }
      if (phase === "waiting") await h.prepared.promise;
      else await entered.promise;
      h.disconnect();
      await rejected;
      gate.resolve({});
      await nextTurn();
      for (const event of ["message", "disconnect", "error", "exit"]) {
        assert.equal(h.child.listenerCount(event), 0);
      }
      if (phase !== "activated-send") cleanFailure(h);
      if (!["reset", "activated-send"].includes(phase)) assert.deepEqual(h.child.env, h.seed);
    });
  }

  it("checks connected state even without a disconnect event", async () => {
    const h = harness({ prewarm(child) { child.connected = false; } });
    await assert.rejects(h.prepare(LAUNCHER_URL));
    cleanFailure(h);
  });

  for (const phase of ["server", "reply", "presence", "prewarm", "reset", "send-throw", "send-error", "ipc-error"]) {
    it(`fails closed on ${phase} failure without exposing error contents`, async () => {
      const secretError = new Error("DO_NOT_ECHO_PRIVATE_ERROR");
      const options = {};
      if (["server", "reply", "presence"].includes(phase)) {
        options.importModule = async (url) => {
          if (url.endsWith(RUNTIME_FILES[phase])) throw secretError;
          return { prewarmConfigDrivenReplyRuntime() {} };
        };
      }
      if (phase === "prewarm" || phase === "reset") options[phase] = () => { throw secretError; };
      if (phase === "send-throw") options.send = () => { throw secretError; };
      if (phase === "send-error") options.send = (_, callback) => callback(secretError);
      const h = harness(options);
      const done = h.prepare(LAUNCHER_URL);
      const rejected = assert.rejects(done, { message: "OpenClaw preinit: preparation or activation failed." });
      if (phase === "reset" || phase === "ipc-error") {
        await h.prepared.promise;
        if (phase === "reset") h.child.emit("message", h.message());
        else h.child.emit("error", secretError);
      }
      await rejected;
      cleanFailure(h);
    });
  }

  it("rejects missing prewarm and missing pinned reset exports", async () => {
    for (const missing of ["reply", "presence"]) {
      const h = harness({ importModule: async (url) =>
        url.endsWith(RUNTIME_FILES[missing]) ? {} : { prewarmConfigDrivenReplyRuntime() {} } });
      await assert.rejects(h.prepare(LAUNCHER_URL));
      cleanFailure(h);
    }
  });

  it("does not remove unrelated process listeners when cleaning up", async () => {
    const h = harness();
    const listener = () => {};
    h.child.on("message", listener);
    h.child.on("disconnect", listener);
    const done = h.prepare(LAUNCHER_URL);
    h.disconnect();
    await assert.rejects(done);
    assert.deepEqual(h.child.listeners("message"), [listener]);
    assert.deepEqual(h.child.listeners("disconnect"), [listener]);
  });
});

describe("fixed package, command and seed", () => {
  it("rejects wrong roots, query-qualified URLs, symlinks, versions and metadata before imports", async () => {
    const cases = [
      { url: LAUNCHER_URL + "?other=1" },
      { url: "file:///tmp/other/openclaw.mjs" },
      { options: { realpath: () => "/tmp/replaced" } },
      { options: { manifest: JSON.stringify({ name: "openclaw", version: "2026.9.6" }) } },
      { options: { manifest: JSON.stringify({ name: "other", version: "2026.9.5" }) } },
      { options: { manifest: "{invalid" } },
      { options: { manifest: "null" } },
    ];
    for (const entry of cases) {
      const h = harness(entry.options);
      await assert.rejects(h.prepare(entry.url ?? LAUNCHER_URL));
      assert.deepEqual(h.calls, []);
      cleanFailure(h);
    }
  });

  it("accepts the resolved launcher symlink used by the original openclaw command", async () => {
    const h = harness({ realpath: (file) => file === "/usr/local/bin/openclaw"
      ? `${PACKAGE_ROOT}/openclaw.mjs` : file });
    h.child.argv[1] = "/usr/local/bin/openclaw";
    const done = h.prepare(LAUNCHER_URL);
    await h.prepared.promise;
    h.child.emit("message", h.message());
    await done;
    h.child.emit("exit", 0);
  });

  it("rejects commands and selector flags outside the narrow gateway run invocation", async () => {
    const commands = [
      [], ["gateway"], ["gateway", "status"], ["--profile", "dev", "gateway", "run"],
      ["gateway", "run", "--dev"], ["gateway", "run", "--profile=other"],
      ["gateway", "run", "--config", "/tmp/other"], ["gateway", "run", "--container", "other"],
      ["gateway", "run", "--help"], ["gateway", "run", "--port", "0"],
      ["gateway", "run", "--port", "65536"], ["gateway", "run", "--port"],
      ["gateway", "run", "--port=18789"], ["gateway", "run", "--", "--verbose"],
      ["gateway", "run", "--verbose", "--verbose"], ["gateway", "run", "--bind", "lan"],
    ];
    for (const command of commands) {
      const h = harness();
      h.child.argv = h.child.argv.slice(0, 2).concat(command);
      await assert.rejects(h.prepare(LAUNCHER_URL));
      assert.deepEqual(h.calls, []);
      cleanFailure(h);
    }
  });

  it("rejects unfixed paths, controls, selectors, credentials and prebound identities", async () => {
    const overrides = [
      ["NODE_DISABLE_COMPILE_CACHE", "0"], ["OPENCLAW_NO_RESPAWN", "0"], ["OPENCLAW_AGENTCORE_PREINIT", "0"],
      ["HOME", "relative"], ["OPENCLAW_CONFIG_PATH", "/root/../other.json"], ["OPENCLAW_STATE_DIR", undefined],
      ["OPENCLAW_WORKSPACE_DIR", ""], ["PATH", "/usr/bin:."], ["OPENCLAW_PROFILE", "dev"],
      ["OPENCLAW_CONTAINER", "other"], ["OPENCLAW_PACKAGE_DIR", "/tmp/other"], ["OPENCLAW_HOME", "/tmp"],
      ["AWS_ACCESS_KEY_ID", "private"], ["AWS_CONTAINER_CREDENTIALS_FULL_URI", "http://credential-endpoint"],
      ["AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", "/tmp/token"], ["AWS_WEB_IDENTITY_TOKEN_FILE", "/tmp/token"],
      ["AWS_CONFIG_FILE", "relative"], ["USER_ID", "actor-one"], ["INTERNAL_USER_ID", "user-one"],
    ];
    for (const [key, value] of overrides) {
      const h = harness();
      if (value === undefined) delete h.child.env[key];
      else h.child.env[key] = value;
      await assert.rejects(h.prepare(LAUNCHER_URL));
      assert.deepEqual(h.calls, []);
      cleanFailure(h);
    }
  });

  it("requires literal AWS_EC2_METADATA_DISABLED=true before importing any runtime", async () => {
    for (const value of [undefined, "", "false", "TRUE", "1", true]) {
      const h = harness();
      if (value === undefined) delete h.child.env.AWS_EC2_METADATA_DISABLED;
      else h.child.env.AWS_EC2_METADATA_DISABLED = value;
      await assert.rejects(h.prepare(LAUNCHER_URL), /instance metadata credentials must be disabled/);
      assert.deepEqual(h.calls, []);
      cleanFailure(h);
    }
  });
});

describe("activation envelope and immutable environment", () => {
  const mutations = {
    "wrong message type": (m) => { m.type = "other"; },
    "unknown envelope field": (m) => { m.extra = "other"; },
    "empty generation": (m) => { m.generation = ""; },
    "blank generation": (m) => { m.generation = " "; },
    "non-string generation": (m) => { m.generation = 1; },
    "empty user": (m) => { m.binding.userId = ""; },
    "empty actor": (m) => { m.binding.actorId = ""; },
    "unknown binding field": (m) => { m.binding.extra = 1; },
    "missing binding field": (m) => { delete m.binding.runtimeSessionId; },
    "negative routing generation": (m) => { m.binding.routingGeneration = -1; },
    "fractional routing generation": (m) => { m.binding.routingGeneration = 0.1; },
    "string routing generation": (m) => { m.binding.routingGeneration = "0"; },
    "numeric runtime session": (m) => { m.binding.runtimeSessionId = 1; },
    "empty runtime session": (m) => { m.binding.runtimeSessionId = ""; },
    "wrong actor environment": (m) => { m.env.USER_ID = "other"; },
    "wrong user environment": (m) => { m.env.INTERNAL_USER_ID = "other"; },
    "missing identity": (m) => { delete m.env.INTERNAL_USER_ID; },
    "missing stable selector": (m) => { delete m.env.HOME; },
    "changed HOME": (m) => { m.env.HOME = "/other"; },
    "changed state directory": (m) => { m.env.OPENCLAW_STATE_DIR = "/other"; },
    "changed config path": (m) => { m.env.OPENCLAW_CONFIG_PATH = "/other"; },
    "changed PATH": (m) => { m.env.PATH = "/other"; },
    "changed credential config": (m) => { m.env.AWS_CONFIG_FILE = "/other"; },
    "enabled instance metadata": (m) => { m.env.AWS_EC2_METADATA_DISABLED = "false"; },
    "omitted instance metadata control": (m) => { delete m.env.AWS_EC2_METADATA_DISABLED; },
    "added profile": (m) => { m.env.OPENCLAW_PROFILE = "dev"; },
    "added credential endpoint": (m) => { m.env.AWS_CONTAINER_CREDENTIALS_FULL_URI = "http://endpoint"; },
    "added credential value": (m) => { m.env.AWS_SECRET_ACCESS_KEY = "private"; },
    "added arbitrary field": (m) => { m.env.EXTRA = "other"; },
    "omitted preinit flag": (m) => { delete m.env.OPENCLAW_AGENTCORE_PREINIT; },
    "changed preinit flag": (m) => { m.env.OPENCLAW_NO_RESPAWN = "0"; },
    "non-string env value": (m) => { m.env.USER_ID = 7; },
    "null env": (m) => { m.env = null; },
    "array env": (m) => { m.env = []; },
    "accessor env": (m) => { Object.defineProperty(m.env, "USER_ID", { get() { assert.fail("Must not read accessor"); } }); },
    "prototype env": (m) => { Object.setPrototypeOf(m.env, { hidden: "value" }); },
  };
  for (const [label, mutate] of Object.entries(mutations)) {
    it(`rejects ${label} without applying identity`, async () => {
      const h = harness();
      const done = h.prepare(LAUNCHER_URL);
      await h.prepared.promise;
      const message = h.message();
      mutate(message);
      h.child.emit("message", message);
      await assert.rejects(done, { message: "OpenClaw preinit: preparation or activation failed." });
      assert.deepEqual(h.child.env, h.seed);
      cleanFailure(h);
    });
  }

  it("rejects null and non-object messages", async () => {
    for (const message of [null, "openclaw:activate", [], 0]) {
      const h = harness();
      const done = h.prepare(LAUNCHER_URL);
      await h.prepared.promise;
      h.child.emit("message", message);
      await assert.rejects(done);
      cleanFailure(h);
    }
  });

  it("rejects local environment or argv mutations during preparation and waiting", async () => {
    for (const phase of ["prewarm", "waiting-env", "waiting-argv", "reset"]) {
      const options = {};
      if (phase === "prewarm" || phase === "reset") options[phase] = (child) => { child.env.PATH = "/changed"; };
      const h = harness(options);
      const done = h.prepare(LAUNCHER_URL);
      const rejected = assert.rejects(done);
      if (phase !== "prewarm") {
        await h.prepared.promise;
        if (phase === "waiting-env") h.child.env.HOME = "/changed";
        if (phase === "waiting-argv") h.child.argv.push("--dev");
        h.child.emit("message", h.message());
      }
      await rejected;
      cleanFailure(h);
    }
  });

  it("cannot return to the CLI if the environment changes during activated ACK delivery", async () => {
    for (const key of ["PATH", "USER_ID", "INTERNAL_USER_ID"]) {
      const h = harness({ send(message, callback, child) {
        if (message.type === "openclaw:activated") child.env[key] = "changed";
        queueMicrotask(() => callback(null));
      } });
      const done = h.prepare(LAUNCHER_URL);
      const rejected = assert.rejects(done);
      await h.prepared.promise;
      h.child.emit("message", h.message());
      await rejected;
      assert.equal(h.child.listenerCount("message"), 0);
      assert.equal(h.child.listenerCount("disconnect"), 0);
    }
  });
});
