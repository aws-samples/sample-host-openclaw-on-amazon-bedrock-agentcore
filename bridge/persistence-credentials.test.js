const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const { DatabaseSync } = require("node:sqlite");
const { createRuntimeLifecycle, REQUIRED_RESOURCES } = require("./runtime-lifecycle");

function persistence(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "persistence-creds-"));
  const epoch = Date.now();
  let now = epoch;
  let refreshCalls = 0;
  let refreshError;
  let releaseRefresh;
  let publicationGuard = () => {};
  const warnings = [];
  const uploads = [];
  const intervals = [];
  const options = [];
  const publication = [];
  class Clock extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  function load(name, mocks = {}) {
    const box = {
      module: { exports: {} }, require: (key) => mocks[key] || require(key),
      Buffer, Date: Clock, AbortController, AbortSignal,
      process: { pid: process.pid, env: { HOME: root, AWS_REGION: "us-west-2", S3_USER_FILES_BUCKET: "bucket" } },
      console: { log() {}, warn(message) { warnings.push(message); } },
      setTimeout: () => ({ unref() {} }), clearTimeout() {},
      setInterval(callback) { intervals.push(callback); return { unref() {} }; }, clearInterval() {},
    };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, name), "utf8"), box, { filename: name });
    return box.module.exports;
  }
  const scoped = load("scoped-credentials.js");
  class List { constructor(input) { this.input = input; } }
  class Put { constructor(input) { this.input = input; } }
  const sync = load("workspace-sync.js", {
    "./scoped-credentials": scoped,
    fs: { ...fs, watch: () => Object.assign(new EventEmitter(), { close() {} }) },
    "@aws-sdk/client-s3": {
      ListObjectsV2Command: List, PutObjectCommand: Put,
      S3Client: class {
        constructor(config) { this.config = config; options.push(config); }
        async send(command) {
          assert.equal(typeof this.config.credentials, "function", "parent credentials must never be used");
          const credentials = await this.config.credentials();
          scoped.assertCredentialsFresh(credentials, { now: () => now });
          if (command instanceof List) return { Contents: [], IsTruncated: false };
          assert.ok(command instanceof Put);
          assert.ok(command.input.Key.startsWith("telegram_1/.openclaw/"));
          uploads.push({ at: now - epoch, key: credentials.accessKeyId, body: Buffer.from(command.input.Body) });
          return {};
        }
      },
    },
  });
  const manager = scoped.createCredentialManager("telegram_1", {
    credDir: path.join(root, "credentials"), now: () => now, refreshBeforeMs: 680000,
    onRefresh(credentials) {
      publicationGuard();
      publication.push(credentials.accessKeyId);
      sync.configureCredentials(credentials);
    },
    refresh: async () => {
      refreshCalls++;
      if (releaseRefresh) await new Promise((resolve) => { releaseRefresh.resolve = resolve; });
      if (refreshError) throw refreshError;
      return {
        accessKeyId: `key-${refreshCalls}`, secretAccessKey: "secret", sessionToken: "token",
        expiration: new Clock(now + 3600000),
      };
    },
  });
  const provider = () => manager.ensureFresh({ forPersistence: true, minValidityMs: 60000 });
  sync.configureCredentialProvider?.(provider);
  t.after(() => {
    manager.stop();
    fs.rmSync(root, { recursive: true, force: true });
  });
  return {
    root, manager, sync, uploads, publication, warnings, intervals, options,
    at(value) { now = epoch + value; },
    refreshCount: () => refreshCalls,
    failRefresh(error) { refreshError = error; },
    guardPublication(checkpoint) { publicationGuard = checkpoint; },
    holdRefresh() { releaseRefresh = {}; return releaseRefresh; },
    async start() {
      await manager.ensureFresh();
      await sync.restoreWorkspace("telegram_1");
      sync.startChangeBackup("telegram_1");
    },
    write(text) { fs.writeFileSync(path.join(root, ".openclaw", "notes.txt"), text); },
  };
}

test("completed-turn SQLite backup refreshes credentials when its deferred flush reaches expiry", async (t) => {
  const p = persistence(t);
  await p.start();
  p.at(2919000);
  await p.manager.ensureFresh();
  assert.equal(p.refreshCount(), 1);
  const db = new DatabaseSync(path.join(p.root, ".openclaw", "history.sqlite"));
  t.after(() => { try { db.close(); } catch {} });
  db.exec("CREATE TABLE history (value TEXT); INSERT INTO history VALUES ('during-turn')");
  p.at(3300000);
  await p.sync.saveWorkspace("telegram_1");
  assert.equal(p.refreshCount(), 1);
  p.at(3530000);
  db.exec("INSERT INTO history VALUES ('completed-turn')");
  db.close();
  p.sync.markChanged("history.sqlite");
  const held = await p.sync.flushPendingSaves("after-turn");
  assert.equal(held.held, 1);
  assert.equal(held.holdMs, 70000);
  p.at(3600000);
  const flushed = await p.sync.flushPendingSaves("scheduled-flush");
  assert.equal(flushed.failed, 0, p.warnings.join("\n"));
  assert.equal(flushed.uploaded, 1);
  assert.equal(p.refreshCount(), 2);
  assert.deepEqual(p.uploads.map(({ at, key }) => ({ at, key })), [
    { at: 3300000, key: "key-1" }, { at: 3600000, key: "key-2" },
  ]);
  const snapshot = path.join(p.root, "uploaded.sqlite");
  fs.writeFileSync(snapshot, p.uploads[1].body);
  const saved = new DatabaseSync(snapshot);
  assert.deepEqual(saved.prepare("SELECT value FROM history").all().map((row) => row.value), [
    "during-turn", "completed-turn",
  ]);
  saved.close();
});

function failureRuntime(t, p) {
  const timers = [];
  const handlers = {};
  const dispatchCalls = [];
  const child = Object.assign(new EventEmitter(), { exitCode: null, signalCode: null });
  child.kill = () => {
    if (child.exitCode !== null) return;
    p.write("gateway shutdown change");
    p.sync.markChanged("notes.txt");
    child.exitCode = 0;
    child.emit("exit", 0);
  };
  const box = vm.createContext({
    credentialManager: p.manager, workspaceSync: p.sync, currentNamespace: "telegram_1",
    shuttingDown: false, openclawReady: true, proxyReady: true, lateAnnounces: null,
    proxySdkOwner: null, proxyAuthControl: null, scopedLogger: null,
    retireBrowserStartOwner: async () => {},
    browserHeaderRefreshTimer: null,
    runtimeSkillManifestWatcher: null,
    openclawProcess: child, proxyProcess: null, migrationProcess: null, runtimeSkillChildren: new Set(),
    preparedGateway: { async stop() { child.kill(); } },
    stateStorage: { stopPeriodicMirror() {}, stopWorkspaceWatcher() {} },
    sessionStorageActive: false, GATEWAY_STOP_WAIT_MS: 5,
    cwLogger: { shutdown: async () => {}, flush: async () => {} },
    stopBrowserSessions: async () => {},
    stopGateway: async () => { if (child.exitCode === null) child.kill(); return true; },
    setTimeout(callback, delay) {
      const timer = setTimeout(callback, delay);
      timers.push({ callback, delay, timer });
      return timer;
    },
    clearTimeout, clearInterval,
    console: { log() {}, warn(message) { p.warnings.push(message); }, error() {} },
    process: { on(name, callback) { handlers[name] = callback; }, exit() {} },
    TURN_TIMEOUT_MS: 620000, RUNTIME_PLATFORM: "V2",
    proxyModelOutcomeUnknown: false,
    UNKNOWN_OUTCOME_RESPONSE: "The request outcome is unknown. It has not been automatically retried.",
    checkProxyHealth: async () => {
      dispatchCalls.push("proxy:health");
      return { model_outcome_unknown: false };
    },
    verifyV2Resources: async () => {}, updateIdentityFile() {}, TELEGRAM_BOT_TOKEN: null,
    OPENCLAW_PORT: 18789, GATEWAY_TOKEN: "test", buildBridgeText: (message) => message,
    extractTextFromContent: (text) => text,
    gatewayTurn: async () => {
      dispatchCalls.push("chat.send");
      return { phase: "unknown_outcome", response: "unfinished" };
    },
    activeTaskCount: 0, lastActivityTime: 0,
  });
  const source = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf8");
  const cleanupStart = source.indexOf("async function cleanupV2Resources()");
  vm.runInContext(source.slice(cleanupStart, source.indexOf("\nfunction runRuntimeSkillInstall(", cleanupStart)), box);
  const lifecycle = createRuntimeLifecycle({
    activate: async (_context, scope) => {
      box.activationScope = scope;
      REQUIRED_RESOURCES.forEach((name) => scope.mark(name));
    },
    cleanup: () => box.cleanupV2Resources(),
  });
  box.v2Lifecycle = lifecycle;
  const handlerStart = source.indexOf("async function handleV2Invocation(");
  vm.runInContext(source.slice(handlerStart, source.indexOf("\n/**\n * AgentCore contract HTTP server.", handlerStart)), box);
  const shutdownStart = source.indexOf('process.on("SIGTERM", async () => {');
  vm.runInContext(source.slice(shutdownStart, source.indexOf("\nserver.listen(", shutdownStart)), box);
  t.after(() => { for (const { timer } of timers) clearTimeout(timer); });
  const owner = { userId: "u1", actorId: "telegram:1", runtimeSessionId: "session-1", routingGeneration: 1 };
  return {
    lifecycle, owner,
    async start() {
      await lifecycle.ensureReady(owner);
      p.guardPublication(lifecycle.checkpoint);
    },
    async unknown() {
      let response;
      await box.handleV2Invocation({ ...owner, action: "chat", message: "do the task" }, { headers: {} }, {
        writeHead() {}, end(body) { response = JSON.parse(body); },
      }, () => false);
      assert.deepEqual(dispatchCalls, ["proxy:health", "chat.send", "proxy:health"],
        "the unknown Gateway outcome must follow one dispatch between successful health probes");
      return response;
    },
    shutdown: () => handlers.SIGTERM(),
    failureDeadline: () => timers.find((entry) => entry.delay === 10000).callback(),
  };
}

for (const failure of ["readiness failure", "unknown outcome"]) {
  for (const expired of [false, true]) {
    test(`${failure} flushes dirty state and gateway shutdown changes with ${expired ? "expired" : "valid"} credentials`, async (t) => {
      const p = persistence(t);
      await p.start();
      const runtime = failureRuntime(t, p);
      await runtime.start();
      p.write("pending turn change");
      p.sync.markChanged("notes.txt");
      if (expired) p.at(3600001);
      if (failure === "unknown outcome") {
        const result = await runtime.unknown();
        assert.equal(result.status, "unknown_outcome");
        assert.equal(result.execution.phase, "unknown_outcome");
      } else await runtime.lifecycle.fail(new Error("Readiness lost"));
      assert.ok(p.uploads.length > 0, p.warnings.join("\n"));
      assert.equal(p.uploads.at(-1).body.toString(), "gateway shutdown change");
      assert.equal(p.uploads.at(-1).key, expired ? "key-2" : "key-1");
      assert.equal(p.refreshCount(), expired ? 2 : 1);
      assert.deepEqual(p.publication, ["key-1"]);
      assert.equal(runtime.lifecycle.health().statusCode, 503);
      await assert.rejects(runtime.lifecycle.run({ ...runtime.owner, deadline: Date.now() + 1000 },
        () => assert.fail("failed runtime cannot dispatch")), /Readiness lost|outcome is unknown/);
      await assert.rejects(p.manager.ensureFresh({ forPersistence: true }), /stopped/);
      const warnings = p.warnings.length;
      await runtime.shutdown();
      assert.equal(p.warnings.length, warnings, "later SIGTERM must not need a stopped provider for already saved files");
    });
  }
}

test("SIGTERM takes over persistence while failure cleanup is refreshing credentials", async (t) => {
  const p = persistence(t);
  await p.start();
  const runtime = failureRuntime(t, p);
  await runtime.start();
  p.at(3600001);
  p.write("pending change");
  p.sync.markChanged("notes.txt");
  const waiting = p.holdRefresh();
  const failure = runtime.lifecycle.fail(new Error("Readiness lost"));
  for (let i = 0; i < 10 && !waiting.resolve; i++) await new Promise(setImmediate);
  assert.equal(typeof waiting.resolve, "function");
  const shutdown = runtime.shutdown();
  runtime.failureDeadline();
  waiting.resolve();
  await Promise.all([failure, shutdown]);
  assert.equal(p.uploads.at(-1).body.toString(), "gateway shutdown change");
  assert.deepEqual(p.publication, ["key-1"]);
  await assert.rejects(p.manager.ensureFresh({ forPersistence: true }), /stopped/);
});

test("failure cleanup deadline fences late refresh without publishing or saving", async (t) => {
  const p = persistence(t);
  await p.start();
  const runtime = failureRuntime(t, p);
  await runtime.start();
  p.at(3600001);
  p.write("pending change");
  p.sync.markChanged("notes.txt");
  const waiting = p.holdRefresh();
  const failure = runtime.lifecycle.fail(new Error("Readiness lost"));
  for (let i = 0; i < 10 && !waiting.resolve; i++) await new Promise(setImmediate);
  assert.equal(typeof waiting.resolve, "function");
  runtime.failureDeadline();
  await assert.rejects(p.manager.ensureFresh({ forPersistence: true }), /stopped/);
  waiting.resolve();
  await failure;
  assert.equal(p.uploads.length, 0);
  assert.deepEqual(p.publication, ["key-1"]);
  assert.ok(p.warnings.some((line) => line.includes("manager stopped")));
  assert.equal(runtime.lifecycle.health().statusCode, 503);
});

test("idle periodic backup obtains fresh scoped credentials without a user request", async (t) => {
  const p = persistence(t);
  await p.start();
  p.sync.startPeriodicSave("telegram_1", 300000);
  p.at(3600001);
  p.write("idle change");
  p.intervals.at(-1)();
  for (let i = 0; i < 10 && p.uploads.length === 0; i++) await new Promise(setImmediate);
  assert.equal(p.uploads.length, 1, p.warnings.join("\n"));
  assert.equal(p.uploads[0].key, "key-2");
  assert.equal(p.refreshCount(), 2);
});

test("failed persistence refresh retains dirty data and never uses parent credentials", async (t) => {
  const p = persistence(t);
  await p.start();
  p.at(3600001);
  p.write("keep until refresh succeeds");
  p.sync.markChanged("notes.txt");
  p.failRefresh(new Error("STS unavailable"));
  const failed = await p.sync.flushPendingSaves("idle");
  assert.equal(failed.failed, 1);
  assert.ok(p.warnings.some((line) => line.includes("STS unavailable")));
  assert.equal(p.uploads.length, 0);
  p.failRefresh(null);
  const retried = await p.sync.flushPendingSaves("idle-retry");
  assert.equal(retried.uploaded, 1);
  assert.equal(p.uploads[0].body.toString(), "keep until refresh succeeds");
  await assert.rejects(p.sync.saveWorkspace("telegram_2"), /owner|namespace/i);
});

test("draining allows final persistence refresh without publishing child credentials", async (t) => {
  const p = persistence(t);
  await p.start();
  const credentialPath = path.join(p.root, "credentials", "scoped-creds.json");
  const original = fs.readFileSync(credentialPath, "utf8");
  p.manager.beginDrain();
  p.at(3600001);
  p.write("final change");
  await p.sync.cleanup("telegram_1");
  assert.equal(p.uploads.length, 1, p.warnings.join("\n"));
  assert.equal(p.uploads[0].key, "key-2");
  assert.deepEqual(p.publication, ["key-1"]);
  assert.equal(fs.readFileSync(credentialPath, "utf8"), original);
  await assert.rejects(p.manager.ensureFresh(), /draining/i);
  p.manager.stop();
  await assert.rejects(p.options.at(-1).credentials(), /stopped/i);
});

test("stopping during persistence refresh prevents late S3 sends and publication", async (t) => {
  const p = persistence(t);
  await p.start();
  p.at(3600001);
  p.write("pending change");
  p.sync.markChanged("notes.txt");
  const waiting = p.holdRefresh();
  const pending = p.sync.flushPendingSaves("idle");
  for (let i = 0; i < 10 && !waiting.resolve; i++) await new Promise(setImmediate);
  assert.equal(typeof waiting.resolve, "function");
  p.manager.stop();
  waiting.resolve();
  const result = await pending;
  assert.equal(result.failed, 1);
  assert.equal(p.uploads.length, 0);
  assert.deepEqual(p.publication, ["key-1"]);
});
