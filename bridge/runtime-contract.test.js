const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const os = require("os");
const path = require("path");
const vm = require("vm");
const { EventEmitter } = require("events");
const { spawnSync } = require("child_process");

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const tick = () => new Promise(setImmediate);

function loadContract(t, {
  platform, restore = Promise.resolve(), skills = Promise.resolve({}),
  browser = Promise.resolve(), outcome = { phase: "completed", response: "reply" },
  poisoned = false, failCredentialsAfter = Infinity, gatewayMcpEnabled = false,
  signBrowser, cleanup = async () => {}, activationTimeoutMs = 1000,
  managerFactory, persistenceSave, gatewayTurnImpl, privsepMock,
  prepare = Promise.resolve(), gatewayActivation = Promise.resolve(), migration,
  migrationExitCode = 0, remainingLegacy = false,
  home = "/test-home", beforeFsOperation, proxyHealth, runtimeEnv = {},
} = {}) {
  let handler;
  let restored = false;
  const calls = [];
  const errors = [];
  const logs = [];
  const files = new Map();
  const children = [];
  const preparations = [];
  const activations = [];
  const timers = [];
  const intervals = [];
  const timeouts = [];
  const processHandlers = {};
  let persistenceProvider;
  const remoteBrowserStatus = new Map();
  let browserStarts = 0;
  let credentialChecks = 0;
  const env = {
    ...(platform !== undefined ? { AGENTCORE_RUNTIME_PLATFORM: platform } : {}),
    HOME: home, PATH: "/usr/local/bin:/usr/bin:/bin",
    EXECUTION_ROLE_ARN: "arn:aws:iam::123456789012:role/runtime",
    GATEWAY_TOKEN_SECRET_ID: "gateway", S3_USER_FILES_BUCKET: "bucket",
    BROWSER_IDENTIFIER: "browser", USER_ACTIVATION_TIMEOUT_MS: String(activationTimeoutMs),
    NODE_COMPILE_CACHE: "/app/.compile-cache",
    AWS_ACCESS_KEY_ID: "test-only-key", AWS_CONTAINER_CREDENTIALS_FULL_URI: "http://credentials.invalid",
    ...runtimeEnv,
  };
  if (gatewayMcpEnabled) env.AGENTCORE_GATEWAY_URL = "https://gateway.invalid/mcp";
  const stateDir = `${home || "/root"}/.openclaw`;
  const fakeFs = {
    ...fs,
    existsSync: (filename) => poisoned && filename === stateDir || files.has(filename),
    readdirSync: () => poisoned ? ["state.sqlite"] : [],
    readFileSync: (filename) => {
      if (filename.endsWith("/openclaw/package.json")) return '{"version":"2026.9.5"}';
      if (filename.startsWith("/skills/")) return "# Skill";
      if (files.has(filename)) return files.get(filename);
      return fs.readFileSync(filename, "utf8");
    },
    mkdirSync: () => {},
    writeFileSync: (filename, content) => {
      beforeFsOperation?.("write", filename);
      if (filename.includes("/.openclaw/")) assert.ok(restored, "state write before strict restore");
      calls.push(`write:${filename}`);
      files.set(filename, content);
    },
    renameSync: (source, destination) => {
      beforeFsOperation?.("rename", destination);
      assert.ok(files.has(source), "rename source must exist");
      calls.push(`rename:${destination}`);
      files.set(destination, files.get(source));
      files.delete(source);
    },
    unlinkSync: (filename) => { files.delete(filename); },
    promises: { access: async () => { throw new Error("missing"); } },
  };
  const ready = () => { if (!restored) throw new Error("Restore barrier closed"); };
  class Command { constructor(input) { this.input = input; } }
  class StartBrowser extends Command {}
  class GetBrowser extends Command {}
  const mocks = {
    fs: fakeFs,
    "./runtime-privsep": privsepMock || require("./runtime-privsep"),
    http: {
      createServer(callback) {
        handler = callback;
        return { listen(_port, _host, ready) { ready(); } };
      },
      get(_url, callback) {
        const request = new EventEmitter();
        request.setTimeout = () => request;
        request.destroy = () => {};
        queueMicrotask(() => {
          const health = proxyHealth && _url.endsWith("/health")
            ? proxyHealth(calls) : { status: "ok", model_outcome_unknown: false };
          if (health instanceof Error) { request.emit("error", health); return; }
          const response = new EventEmitter();
          response.statusCode = 200;
          response.resume = () => {};
          callback(response);
          response.emit("data", typeof health === "string" ? health : JSON.stringify(health));
          response.emit("end");
        });
        return request;
      },
    },
    child_process: {
      spawn(command, args, options) {
        const preparing = options.env.OPENCLAW_AGENTCORE_PREINIT === "1";
        if (!preparing) ready();
        const kind = preparing ? "prepared-openclaw" : args[0] === "doctor" ? "doctor" : command;
        calls.push(`spawn:${kind}`);
        const child = new EventEmitter();
        Object.assign(child, {
          stdout: new EventEmitter(), stderr: new EventEmitter(), exitCode: null, signalCode: null,
          kind, command, args, env: { ...options.env }, uid: options.uid, gid: options.gid,
        });
        child.kill = () => { child.exitCode = 0; child.emit("exit", 0); };
        children.push(child);
        if (kind === "doctor") migration.then(() => {
          calls.push("migration:done");
          child.exitCode = migrationExitCode;
          child.emit("exit", migrationExitCode);
        });
        return child;
      },
    },
    "./openclaw-worker-owner": {
      createPreparedGateway(options) {
        assert.ok(platform === undefined || platform === "V2");
        preparations.push(options);
        let child;
        let stopped = false;
        const closed = deferred();
        return {
          get child() { return child; },
          async prepare() {
            child = mocks.child_process.spawn("openclaw",
              ["gateway", "run", "--port", String(options.port), "--verbose"],
              { env: { ...options.env, OPENCLAW_AGENTCORE_PREINIT: "1" } });
            options.onChild(child);
            try {
              await Promise.race([prepare, closed.promise]);
              if (stopped) throw new Error("Prepared gateway stopped");
              calls.push("prepare:ack");
            } catch (error) {
              options.onFailure(error);
              throw error;
            }
          },
          async activate(input) {
            ready();
            assert.ok(calls.includes("skills:done"), "gateway activation before skill restore");
            assert.ok(files.has(`${stateDir}/openclaw.json`), "gateway activation before config");
            assert.ok(!migration || calls.includes("migration:done"), "gateway activation before migration");
            const stableEnv = { ...input.env };
            delete stableEnv.USER_ID;
            delete stableEnv.INTERNAL_USER_ID;
            assert.deepEqual(stableEnv, options.env);
            activations.push(input);
            calls.push("activate:openclaw");
            await Promise.race([gatewayActivation, closed.promise]);
            if (stopped || input.signal.aborted) throw new Error("Prepared gateway activation aborted");
            calls.push("activate:ack");
          },
          stop() {
            if (stopped) return;
            stopped = true;
            calls.push("prepared:stop");
            closed.reject(new Error("Prepared gateway stopped"));
            if (child && child.exitCode === null) child.kill();
          },
        };
      },
    },
    "./live-seed-bridge": {
      createLiveSeedBridge({ createCold }) {
        const cold = createCold();
        return {
          get child() { return cold.child; },
          get prepared() { return false; },
          prepare: () => cold.prepare(),
          activate: (input) => cold.activate(input),
          restoreForBinding: async (input) => {
            input.onPhase?.("cold_prepare_start");
            input.onPhase?.("seed_ineligible", { reason: "bad value\n[contract] forged" });
            return { adopted: false };
          },
          stop: async () => cold.stop(),
        };
      },
    },
    "./proxy-sdk-snapshot": {
      createProxySdkSnapshotOwner({ onChild }) {
        let child;
        return {
          get child() { return child; },
          async prepare() {
            child = new EventEmitter();
            Object.assign(child, {
              stdout: new EventEmitter(), stderr: new EventEmitter(), exitCode: null, signalCode: null,
              kind: "node", env: null,
            });
            child.kill = () => { child.exitCode = 0; child.emit("exit", 0); };
            onChild(child);
          },
          async activate(input) {
            input.assertCurrent();
            ready();
            calls.push("spawn:node");
            child.env = { ...input.env };
            children.push(child);
          },
          async attest() { ready(); calls.push("proxy:attested"); },
          async stop() { if (child && child.exitCode === null) child.kill(); },
        };
      },
    },
    "./proxy-auth-prepare": {
      createProxyAuthParent: () => ({ prepare: () => {}, stop() {} }),
    },
    "@aws-sdk/client-secrets-manager": {
      GetSecretValueCommand: Command,
      SecretsManagerClient: class { async send() { calls.push("secrets"); return { SecretString: "secret" }; } },
    },
    "@aws-sdk/client-s3": {
      PutObjectCommand: Command,
      S3Client: class { async send() { ready(); calls.push("publish"); } },
    },
    "@aws-sdk/client-bedrock-agentcore": {
      StartBrowserSessionCommand: StartBrowser, StopBrowserSessionCommand: Command,
      GetBrowserSessionCommand: GetBrowser,
      BedrockAgentCoreClient: class {
        async send(command) {
          if (command instanceof StartBrowser) {
            calls.push("browser:start");
            await browser;
            const sessionId = `b${++browserStarts}`;
            remoteBrowserStatus.set(sessionId, "READY");
            return { sessionId, streams: { automationStream: { streamEndpoint: `wss://browser.invalid/${sessionId}` } } };
          }
          if (command instanceof GetBrowser) {
            calls.push(`browser:get:${command.input.sessionId}`);
            const status = remoteBrowserStatus.get(command.input.sessionId);
            if (status instanceof Error) throw status;
            return { browserIdentifier: command.input.browserIdentifier, sessionId: command.input.sessionId, status };
          }
          calls.push("browser:stop");
          remoteBrowserStatus.set(command.input.sessionId, "TERMINATED");
        }
      },
    },
    "@smithy/signature-v4": { SignatureV4: class {
      async sign(request) {
        return signBrowser ? signBrowser(request) : { headers: { Authorization: `signed:${request.path}` } };
      }
    } },
    "@aws-crypto/sha256-js": { Sha256: class {} },
    "@aws-sdk/credential-provider-node": { defaultProvider: () => ({}) },
    "./cloudwatch-logger": {
      startOwned: () => { calls.push("logger"); return { ready: Promise.resolve(true), retire() {} }; },
      shutdown: async () => {}, flush: async () => {},
    },
    "./cognito-token": {
      createCognitoTokenProvider: () => ({
        isConfigured: () => true,
        getAccessToken: async (_actor, options) => {
          calls.push(options?.force ? "bearer:refresh" : "bearer:initial");
          return { token: options?.force ? "fresh-bearer" : "initial-bearer", expiresAt: Date.now() + 3600000 };
        },
      }),
    },
    "./scoped-credentials": {
      createCredentialManager: (optionsNamespace, options) => managerFactory
        ? managerFactory(optionsNamespace, options)
        : ({
        ensureFresh: async () => {
          calls.push("credentials");
          if (++credentialChecks > failCredentialsAfter) throw new Error("Credential refresh denied");
        },
        getCredentials: () => ({}), stop: () => calls.push("credentials:stop"),
        beginDrain: () => calls.push("credentials:drain"),
      }),
      buildOpenClawEnv: require("./scoped-credentials").buildOpenClawEnv,
    },
    "./workspace-sync": {
      restoreWorkspace(_namespace, options) {
        calls.push("restore");
        return restore.then(() => { restored = true; });
      },
      awaitRestore: async (promise, _timeout, options) => {
        await promise;
      },
      assertRestoreReady: ready,
      configureCredentials: () => {},
      configureCredentialProvider: (provider) => { persistenceProvider = provider; },
      getS3Client: () => ({ send: async () => { ready(); calls.push("publish"); } }),
      startPeriodicSave: ready,
      startChangeBackup: () => { ready(); calls.push("backup"); return { mode: "watch" }; },
      flushPendingSaves: async () => { calls.push("save:urgent"); await persistenceSave?.("urgent", persistenceProvider); },
      stopChangeBackup: async () => { calls.push("save:changed"); await persistenceSave?.("changed", persistenceProvider); },
      cleanup: async () => { calls.push("cleanup"); await cleanup(); await persistenceSave?.("full", persistenceProvider); },
    },
    "./state-storage": {
      resolvePaths: () => ({ stateDir, mountStateDir: "/test-mount/.openclaw" }),
      gatewayStateEnv: (dir) => ({ OPENCLAW_STATE_DIR: dir }),
      setupSessionStorage: (options) => {
        calls.push("mount");
        return { available: false };
      },
      stopPeriodicMirror: () => {}, stopWorkspaceWatcher: () => {},
    },
    "./legacy-session-import": {
      pruneRetiredFiles: () => ({ pruned: [], changed: [] }),
      planLegacyImport: () => ({ alreadyImported: [], toImport: migration ? ["legacy-index"] : [] }),
      findLegacySessionStores: () => remainingLegacy ? ["legacy-index"] : [],
      fingerprintIndexes: () => [],
      fingerprintStateDir: () => new Map(),
      writeReceipts: () => [],
      diffRetiredFiles: () => [],
      writeRetiredManifest: () => null,
    },
    "./runtime-skills": {
      manifestPath: (dir) => `${dir}/runtime-skills.json`,
      reinstallFromManifest: async () => {
        ready();
        calls.push("skills");
        const result = await skills;
        calls.push("skills:done");
        return result;
      },
      watchManifest: () => ({ close() {} }),
    },
    "./gateway-lifecycle": {
      waitForGateway: async (options) => {
        ready();
        assert.equal(options.configProbe, true);
        calls.push("gateway:verified");
      },
      dismissStaleSuspendedDeliveries: async () => {
        calls.push("gateway:sweep");
        return { phase: "done", listed: 0, candidates: [], dismissed: [], notDismissed: [] };
      },
      watchChatEvents: () => {
        calls.push("gateway:watch");
        return { ready: Promise.resolve(), stopped: Promise.resolve(), readRunAnswer: async () => null };
      },
      gatewayTurn: async (args) => {
        ready();
        calls.push("chat.send");
        calls.push(args.message);
        return gatewayTurnImpl ? gatewayTurnImpl(args) : outcome;
      },
    },
  };
  const context = vm.createContext({
    require: (name) => Object.hasOwn(mocks, name) ? mocks[name] : require(name),
    __dirname, Buffer, URL, queueMicrotask, AbortController, AbortSignal,
    setTimeout(callback, delay) {
      const timer = setTimeout(callback, delay);
      timeouts.push({ callback, delay, timer });
      return timer;
    },
    clearTimeout, clearInterval,
    setInterval(callback, delay) {
      const timer = setInterval(callback, delay);
      timer.unref();
      timers.push(timer);
      intervals.push({ callback, delay });
      return timer;
    },
    process: {
      env, on(name, callback) { processHandlers[name] = callback; },
      exit() { calls.push("exit"); },
    },
    console: { log(...args) { logs.push(args.join(" ")); }, warn() {}, error(...args) { errors.push(args.join(" ")); } },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf8"), context);
  t.after(() => {
    for (const timer of timers) clearInterval(timer);
    for (const { timer } of timeouts) clearTimeout(timer);
  });
  async function request(payload, method = "POST", url = "/invocations", disconnected = false) {
    const req = new EventEmitter();
    Object.assign(req, { method, url, headers: {} });
    const res = new EventEmitter();
    const result = deferred();
    const frames = [];
    res.writeHead = (code, headers) => { res.statusCode = code; res.headers = headers; res.headersSent = true; };
    res.flushHeaders = () => {};
    res.write = (frame) => { frames.push(frame); return true; };
    res.end = (body) => {
      res.writableEnded = res.writableFinished = true;
      result.resolve({
        code: res.statusCode, headers: res.headers,
        ...(frames.length ? { events: frames.map((frame) => ({
          event: /^event: (.+)$/m.exec(frame)[1], data: JSON.parse(/^data: (.+)$/m.exec(frame)[1]),
        })) } : { body: JSON.parse(body) }),
      });
    };
    handler(req, res);
    if (disconnected) res.emit("close");
    if (method === "POST") {
      req.emit("data", Buffer.from(JSON.stringify(payload)));
      req.emit("end");
    }
    return result.promise;
  }
  return {
    request, calls, files, children, errors, logs, preparations, activations, remoteBrowserStatus,
    gatewayEnv: () => vm.runInContext('openclawEnvFor("u1")', context),
    shutdown: () => processHandlers.SIGTERM(),
    shutdownDeadline: () => timeouts.findLast((entry) => entry.delay === 12000).callback(),
    failLifecycle: () => vm.runInContext('v2Lifecycle.fail(new Error("Interrupted turn"))', context),
    stubTelegramApi(deliveries) {
      context.deliveries = deliveries;
      vm.runInContext(`TELEGRAM_BOT_TOKEN = "test";
        telegramApiCall = async (method, payload) => {
          deliveries.push({ method, text: payload.text, chatId: payload.chat_id });
          return { ok: true, result: { message_id: 123 } };
        };`, context);
    },
    expireResources() {
      vm.runInContext("gatewayBearer.expiresAt = Date.now(); browserExpiresAt = Date.now();", context);
    },
    expireBrowser() {
      vm.runInContext("browserExpiresAt = Date.now();", context);
    },
    browserRefresh: () => intervals.findLast((entry) => entry.delay === 4 * 60 * 1000).callback,
  };
}

const owner = { userId: "u1", actorId: "telegram:1", channel: "test", routingGeneration: 1, runtimeSessionId: "session-1" };

test("ordinary chat opts into SSE only after complete readiness, with one original Gateway turn", async (t) => {
  t.mock.method(console, "log", () => {});
  const app = loadContract(t, { gatewayTurnImpl: async (args) => {
    args.onDelta("Hello");
    args.onDelta("Hello world");
    return { phase: "completed", response: "Hello world", runId: "gateway-run" };
  } });
  const result = await app.request({ ...owner, action: "chat", stream: true, message: "hello" });
  assert.equal(result.code, 200, app.errors.join("\n"));
  assert.equal(result.headers["Content-Type"], "text/event-stream; charset=utf-8");
  assert.deepEqual(result.events.map((entry) => entry.event), ["ready", "delta", "delta", "done"]);
  const ready = result.events[0].data, done = result.events.at(-1).data;
  assert.equal(ready.source_phase, "USER_READY");
  assert.equal(ready.runtime_session_id, owner.runtimeSessionId);
  assert.equal(ready.resources.length, 7);
  assert.equal(done.request_id, ready.request_id);
  assert.equal(done.generation, ready.generation);
  assert.equal(done.execution.run_id, "gateway-run");
  assert.equal(done.response, "Hello world");
  assert.equal(done.streamed_text_matches_final, true);
  assert.equal(app.calls.filter((entry) => entry === "chat.send").length, 1);
  assert.ok(app.calls.indexOf("proxy:attested") < app.calls.indexOf("chat.send"));
});

test("a yielded successor completes the original invocation without failing readiness or replaying", async (t) => {
  t.mock.method(console, "log", () => {});
  const app = loadContract(t, { gatewayTurnImpl: async (args) => {
    args.onDelta("before yield");
    args.onDelta("successor");
    args.onDelta("successor answer");
    assert.equal(args.signal.aborted, false);
    return { phase: "completed", response: "successor answer", runId: "successor-run" };
  } });
  const result = await app.request({ ...owner, action: "chat", stream: true, message: "hello" });
  assert.deepEqual(result.events.map((entry) => entry.event), ["ready", "delta", "done"]);
  assert.equal(result.events[1].data.text, "before yield");
  const done = result.events.at(-1).data;
  assert.equal(done.phase, "USER_READY");
  assert.equal(done.execution.phase, "completed");
  assert.equal(done.execution.run_id, "successor-run");
  assert.equal(done.response, "successor answer");
  assert.equal(done.streamed_text_matches_final, false);
  assert.equal(app.calls.filter((entry) => entry === "chat.send").length, 1);
  assert.deepEqual(app.errors, []);
});

test("JSON chat and cron responses remain JSON without an explicit chat stream opt-in", async (t) => {
  const app = loadContract(t);
  for (const [action, stream] of [["chat", undefined], ["chat", false], ["cron", true]]) {
    const result = await app.request({ ...owner, action, stream, message: "hello" });
    assert.equal(result.headers["Content-Type"], "application/json");
    assert.equal(result.body.response, "reply", app.errors.join("\n"));
    assert.equal(result.body.execution.phase, "completed");
    assert.equal(result.events, undefined);
  }
  const unknown = await app.request({ ...owner, action: "exec", stream: true, message: "hello" });
  assert.equal(unknown.headers["Content-Type"], "application/json");
  assert.equal(unknown.body.response, "Unknown action");
});

test("SSE failure before readiness cannot emit ready or send a turn", async (t) => {
  const app = loadContract(t, { failCredentialsAfter: 0 });
  const result = await app.request({ ...owner, action: "chat", stream: true, message: "hello" });
  assert.deepEqual(result.events.map((entry) => entry.event), ["error"]);
  assert.equal(result.events[0].data.code, "INVOCATION_FAILED");
  assert.equal(app.calls.includes("chat.send"), false);
});

test("V2 bootstrap waits for preparation acknowledgement without creating user resources", async (t) => {
  const prepare = deferred();
  const app = loadContract(t, { prepare: prepare.promise });
  assert.equal((await app.request(null, "GET", "/ping")).code, 503);
  await tick();
  assert.equal((await app.request(null, "GET", "/ping")).code, 503);
  assert.deepEqual(app.calls, ["spawn:prepared-openclaw"]);
  assert.equal(app.files.size, 0);
  assert.equal(app.activations.length, 0);
  const seed = app.preparations[0].env;
  assert.equal(seed.HOME, "/test-home");
  assert.equal(seed.OPENCLAW_STATE_DIR, "/test-home/.openclaw");
  assert.equal(seed.OPENCLAW_CONFIG_PATH, "/test-home/.openclaw/openclaw.json");
  assert.equal(seed.OPENCLAW_WORKSPACE_DIR, "/test-home/.openclaw/workspace");
  assert.equal(seed.AWS_CONFIG_FILE, "/tmp/scoped-creds/scoped-aws-config");
  assert.equal(seed.NODE_COMPILE_CACHE, "/app/.compile-cache");
  for (const key of ["USER_ID", "INTERNAL_USER_ID", "AWS_ACCESS_KEY_ID",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_SHARED_CREDENTIALS_FILE"]) {
    assert.equal(Object.hasOwn(seed, key), false, key);
  }
  assert.equal(app.files.has(seed.AWS_CONFIG_FILE), false);
  prepare.resolve();
  await tick();
  assert.equal((await app.request(null, "GET", "/ping")).body.status, "Healthy");
  await app.request({ action: "status" });
  assert.deepEqual(app.calls, ["spawn:prepared-openclaw", "prepare:ack"]);
  assert.equal(app.files.size, 0);
  assert.ok(app.logs.some((line) => /^\[contract\] preparation elapsedMs=\d+$/.test(line)));
});

for (const home of ["/test-home", ""]) {
  test(`contract seed and activation pass the child validator with ${home ? "inherited HOME" : "default HOME"}`, async (t) => {
    const { createGatewayPreparer, LAUNCHER_URL, PACKAGE_ROOT, RUNTIME_FILES } =
      await import("./openclaw-preinit.mjs");
    const app = loadContract(t, { home });
    await tick();
    const seed = Object.freeze(structuredClone(app.preparations[0].env));
    assert.equal(seed.HOME, home || "/root");
    assert.equal(seed.OPENCLAW_CONFIG_PATH, `${seed.OPENCLAW_STATE_DIR}/openclaw.json`);
    assert.equal(seed.OPENCLAW_WORKSPACE_DIR, `${seed.OPENCLAW_STATE_DIR}/workspace`);
    const controls = {
      OPENCLAW_AGENTCORE_PREINIT: "1", OPENCLAW_NO_RESPAWN: "1", NODE_DISABLE_COMPILE_CACHE: "1",
    };
    const child = new EventEmitter();
    child.connected = true;
    child.env = { ...seed, ...controls };
    child.argv = ["node", `${PACKAGE_ROOT}/openclaw.mjs`, "gateway", "run", "--port", "18789", "--verbose"];
    const prepared = deferred();
    const acknowledgements = [];
    child.send = (message, callback) => {
      acknowledgements.push(message);
      queueMicrotask(() => {
        callback(null);
        if (message.type === "openclaw:prepared") prepared.resolve();
      });
      return true;
    };
    child.exit = () => assert.fail("The validated child must not exit.");
    t.after(() => { child.connected = false; child.emit("disconnect"); child.emit("exit", 0); });
    const prepare = createGatewayPreparer({
      child,
      realpath: (file) => file,
      readFile: (file) => {
        assert.equal(file, `${PACKAGE_ROOT}/package.json`, "Preparation must not read user state.");
        return '{"name":"openclaw","version":"2026.9.5"}';
      },
      importModule: async (url) => {
        if (url.endsWith(RUNTIME_FILES.server)) return {};
        if (url.endsWith(RUNTIME_FILES.reply)) return { prewarmConfigDrivenReplyRuntime() {} };
        if (url.endsWith(RUNTIME_FILES.presence)) return { resetAgentCoreSnapshotPresence() {} };
        assert.fail("Unexpected runtime import.");
      },
    });
    const done = prepare(LAUNCHER_URL);
    await Promise.race([
      prepared.promise,
      done.then(() => assert.fail("Preparation must wait for activation.")),
    ]);
    assert.deepEqual(child.env, { ...seed, ...controls });
    assert.equal(app.files.size, 0);
    const response = await app.request({ ...owner, action: "chat", message: "hello" });
    assert.equal(response.body.phase, "USER_READY", app.errors.join("\n"));
    const activation = app.activations[0];
    const finalEnv = Object.freeze({ ...structuredClone(activation.env), ...controls });
    for (const key of ["HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH", "OPENCLAW_WORKSPACE_DIR"]) {
      assert.equal(finalEnv[key], seed[key], key);
    }
    assert.equal(Object.hasOwn(finalEnv, "AWS_SHARED_CREDENTIALS_FILE"), false);
    child.emit("message", {
      type: "openclaw:activate", env: finalEnv, generation: activation.generation,
      binding: structuredClone(activation.binding),
    });
    await done;
    const activatedEnv = { ...finalEnv };
    delete activatedEnv.OPENCLAW_AGENTCORE_PREINIT;
    assert.deepEqual(child.env, activatedEnv);
    assert.equal(Object.hasOwn(child.env, "OPENCLAW_AGENTCORE_PREINIT"), false);
    assert.equal(child.env.OPENCLAW_NO_RESPAWN, "1");
    assert.equal(child.env.NODE_DISABLE_COMPILE_CACHE, "1");
    assert.equal(finalEnv.OPENCLAW_AGENTCORE_PREINIT, "1");
    assert.deepEqual(app.preparations[0].env, seed);
    assert.deepEqual(acknowledgements.map((message) => message.type), ["openclaw:prepared", "openclaw:activated"]);
  });
}

test("explicit V2 uses the prepared environment and activation path", async (t) => {
  for (const home of ["/test-home", ""]) {
    const app = loadContract(t, { platform: "V2", home });
    await tick();
    const env = app.gatewayEnv();
    assert.equal(env.HOME, home || "/root");
    assert.equal(env.OPENCLAW_STATE_DIR, `${home || "/root"}/.openclaw`);
    assert.equal(env.OPENCLAW_CONFIG_PATH, `${env.OPENCLAW_STATE_DIR}/openclaw.json`);
    assert.equal(env.OPENCLAW_WORKSPACE_DIR, `${env.OPENCLAW_STATE_DIR}/workspace`);
    assert.equal(app.preparations.length, 1);
    const reply = await app.request({ ...owner, action: "chat", message: "hello" });
    assert.equal(reply.body.runtimePlatform, "V2", app.errors.join("\n"));
    assert.equal(app.activations.length, 1);
  }
});

for (const reason of ["Preparation failed", "Prepared gateway acknowledgement timed out"]) {
  test(`preparation failure closes admission and stops its child: ${reason}`, async (t) => {
    const prepare = deferred();
    const app = loadContract(t, { prepare: prepare.promise });
    await tick();
    prepare.reject(new Error(reason));
    await tick();
    assert.equal((await app.request(null, "GET", "/ping")).code, 503);
    assert.equal(app.children[0].exitCode, 0);
    assert.ok(app.calls.includes("prepared:stop"));
    assert.equal(app.activations.length, 0);
    assert.ok(!app.calls.includes("secrets"));
    assert.equal(app.files.size, 0);
  });
}

test("bootstrap rechecks user resources after preparation and stops a contaminated child", async (t) => {
  const prepare = deferred();
  const app = loadContract(t, { prepare: prepare.promise });
  await tick();
  app.files.set("/tmp/scoped-creds/scoped-aws-config", "unexpected");
  prepare.resolve();
  await tick();
  assert.equal((await app.request(null, "GET", "/ping")).code, 503);
  assert.ok(app.errors.some((line) => line.includes("User resource present before bootstrap")));
  assert.equal(app.children[0].exitCode, 0);
  assert.equal(app.activations.length, 0);
});

test("a prepared child exit before binding closes admission", async (t) => {
  const app = loadContract(t);
  await tick();
  app.children[0].kill();
  await tick();
  assert.equal((await app.request(null, "GET", "/ping")).code, 503);
  assert.equal(app.activations.length, 0);
  assert.ok(!app.calls.includes("restore"));
});

test("shutdown cancels pending preparation and a late acknowledgement cannot restore health", async (t) => {
  const prepare = deferred();
  const app = loadContract(t, { prepare: prepare.promise });
  await tick();
  await app.shutdown();
  prepare.resolve();
  await tick();
  assert.equal(app.children[0].exitCode, 0);
  assert.ok(app.calls.includes("prepared:stop"));
  assert.ok(!app.calls.includes("prepare:ack"));
  assert.equal((await app.request(null, "GET", "/ping")).code, 503);
  assert.equal(app.activations.length, 0);
});

test("poisoned bootstrap stays unhealthy without user activation", async (t) => {
  const app = loadContract(t, { poisoned: true });
  await tick();
  assert.equal((await app.request(null, "GET", "/ping")).code, 503);
  assert.ok(!app.calls.includes("secrets"));
});

test("runtime skills that could not be restored do not block USER_READY", async (t) => {
  const app = loadContract(t, {
    skills: Promise.resolve({ total: 3, reinstalled: 0, present: 0, failed: 2, deferred: 1, corrupt: true, invalid: 1 }),
  });
  await tick();
  const result = await app.request({ ...owner, action: "chat", message: { text: "hi" } });
  assert.equal(result.body.phase, "USER_READY", app.errors.join("\n"));
  assert.equal(result.body.response, "reply");
  assert.equal(app.activations.length, 1);
});

test("HTTP chat activates the same child after restore, config, migration and skills, then waits for readiness", async (t) => {
  const restore = deferred();
  const skills = deferred();
  const browser = deferred();
  const migration = deferred();
  const gatewayActivation = deferred();
  const app = loadContract(t, {
    restore: restore.promise, skills: skills.promise, browser: browser.promise,
    migration: migration.promise, gatewayActivation: gatewayActivation.promise,
  });
  await tick();
  const preparedChild = app.children[0];
  const chat = app.request({ ...owner, action: "chat", message: { text: "look", images: [{ s3Key: "telegram_1/i.png" }] } });
  await tick();
  assert.ok(app.calls.includes("restore"));
  assert.deepEqual(app.calls.filter((call) => call.startsWith("spawn:")), ["spawn:prepared-openclaw"]);
  assert.equal(app.activations.length, 0);
  assert.equal((await app.request(null, "GET", "/ping")).body.status, "HealthyBusy");
  restore.resolve();
  await tick();
  assert.ok(app.calls.includes("spawn:doctor"), app.errors.join("\n"));
  assert.equal(app.activations.length, 0);
  assert.ok(!app.calls.includes("skills"));
  migration.resolve();
  await tick();
  assert.ok(app.calls.includes("skills"), app.errors.join("\n"));
  assert.ok(!app.calls.includes("spawn:openclaw"));
  skills.resolve({});
  await tick();
  assert.equal(app.activations.length, 1, app.errors.join("\n"));
  assert.equal(app.children[0], preparedChild);
  assert.equal(preparedChild.exitCode, null);
  assert.ok(!app.calls.includes("spawn:openclaw"));
  assert.ok(!app.calls.includes("gateway:verified"));
  const activation = app.activations[0];
  assert.deepEqual({ ...activation.binding }, {
    userId: owner.userId, actorId: owner.actorId,
    routingGeneration: owner.routingGeneration, runtimeSessionId: owner.runtimeSessionId,
  });
  assert.equal(activation.env.USER_ID, owner.actorId);
  assert.equal(activation.env.INTERNAL_USER_ID, owner.userId);
  for (const key of ["HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH", "OPENCLAW_WORKSPACE_DIR"]) {
    assert.equal(activation.env[key], app.preparations[0].env[key], key);
  }
  assert.equal(activation.signal.aborted, false);
  assert.ok(activation.deadline > Date.now());
  gatewayActivation.resolve();
  await tick();
  assert.ok(app.calls.includes("gateway:verified"));
  assert.ok(app.calls.includes("browser:start"));
  assert.ok(!app.calls.includes("chat.send"));
  const warmup = await app.request({ ...owner, action: "warmup" });
  assert.equal(warmup.body.status, "initializing");
  browser.resolve();
  const result = await chat;
  assert.equal(result.body.response, "reply");
  assert.equal(result.body.runtimePlatform, "V2");
  assert.equal(result.body.phase, "USER_READY");
  assert.equal(result.body.execution.engine, "openclaw");
  assert.equal(result.body.execution.phase, "completed");
  assert.equal(result.body.runtimeSessionId, owner.runtimeSessionId);
  assert.equal(result.body.actorId, owner.actorId);
  assert.equal(result.body.routingGeneration, owner.routingGeneration);
  const statusText = (await app.request({ ...owner, action: "status" })).body.response;
  const status = JSON.parse(statusText);
  assert.equal(status.lifecycle.generation, result.body.activationGeneration);
  assert.equal(activation.generation, result.body.activationGeneration);
  assert.equal(status.lifecycle.state, "USER_READY");
  // Status reports whether a binding exists, never the binding itself.
  assert.equal(status.lifecycle.bound, true);
  assert.equal(Object.hasOwn(status.lifecycle, "binding"), false);
  assert.equal(Object.hasOwn(status, "currentUserId"), false);
  assert.equal(Object.hasOwn(status, "openclawLogs"), false);
  for (const value of [owner.userId, owner.actorId, owner.runtimeSessionId]) {
    assert.equal(statusText.includes(value), false, value);
  }
  // Status for a different binding, or with no binding at all, is refused.
  for (const probe of [{ action: "status" }, { ...owner, actorId: "telegram:999", action: "status" },
    { ...owner, routingGeneration: owner.routingGeneration + 1, action: "status" }]) {
    const refused = (await app.request(probe)).body;
    assert.equal(refused.status, "error");
    assert.equal(refused.response, "This AgentCore Runtime cannot accept the requested user binding.");
  }
  // An action-less payload no longer falls into the status branch.
  assert.match((await app.request({ ...owner })).body.response, /Unknown action/);
  assert.ok(app.calls.some((call) => call.includes("[OPENCLAW_IMAGES:")));
  // The stale suspended-delivery sweep runs once, after readiness, before the first turn.
  assert.equal(app.calls.filter((call) => call === "gateway:sweep").length, 1);
  assert.ok(app.calls.indexOf("gateway:verified") < app.calls.indexOf("gateway:sweep"));
  assert.ok(app.calls.indexOf("gateway:sweep") < app.calls.indexOf("chat.send"));
  assert.equal((await app.request({ ...owner, action: "warmup" })).body.status, "ready");
  assert.equal(app.children.find((child) => child.kind === "node").env.NODE_COMPILE_CACHE, "/app/.compile-cache");
  const phases = app.logs.map((line) => line.match(
    /^\[contract\] (credentials_ready|restore_ready|config_skills_ready|gateway_ready|browser_ready|user_ready) elapsedMs=(\d+) generation=(\S+)$/,
  )).filter(Boolean);
  assert.deepEqual(phases.map((phase) => phase[1]), [
    "credentials_ready", "restore_ready", "config_skills_ready", "gateway_ready", "browser_ready", "user_ready",
  ]);
  phases.forEach((phase, index) => {
    assert.equal(phase[3], activation.generation);
    if (index) assert.ok(Number(phase[2]) >= Number(phases[index - 1][2]));
  });
});

test("cold activation logs every phase in order with monotonic elapsed times", async (t) => {
  const app = loadContract(t);
  await tick();
  const result = await app.request({ ...owner, action: "chat", message: "hello" });
  assert.equal(result.body.phase, "USER_READY", app.errors.join("\n"));
  const phases = app.logs.map((line) => line.match(
    /^\[contract\] ([a-z_]+) elapsedMs=(\d+) generation=(\S+)((?: [a-z_]+=[\w.:-]+)*)$/,
  )).filter(Boolean);
  assert.deepEqual(phases.map((phase) => phase[1] + phase[4]), [
    "credentials_ready", "cold_prepare_start", "seed_ineligible reason=bad_value__contract__forged",
    "gateway_prepared path=cold", "session_storage_ready available=false", "restore_start", "restore_ready",
    "config_skills_ready", "gateway_activate_start", "gateway_activated", "proxy_ready", "gateway_ready",
    "browser_ready", "user_ready",
  ]);
  assert.ok(!app.logs.some((line) => /\n/.test(line)));
  phases.forEach((phase, index) => {
    if (index) assert.ok(Number(phase[2]) >= Number(phases[index - 1][2]));
  });
});

for (const options of [{ migrationExitCode: 1 }, { remainingLegacy: true }]) {
  test(`incomplete legacy import fails activation and preserves history: ${JSON.stringify(options)}`, async (t) => {
    const app = loadContract(t, { migration: Promise.resolve(), ...options });
    await tick();
    const reply = await app.request({ ...owner, action: "chat", message: "hello" });
    assert.equal(reply.body.status, "error");
    assert.equal((await app.request(null, "GET", "/ping")).code, 503);
    assert.equal(app.activations.length, 0);
    assert.ok(!app.calls.includes("chat.send"));
    assert.ok(!app.calls.some((call) => call.startsWith("rename:") && call.includes("/.openclaw/")));
    assert.ok(app.errors.some((error) => error.includes("Legacy session import did not complete")));
  });
}

test("activation timeout stops the prepared child and rejects a late activation acknowledgement", async (t) => {
  const gatewayActivation = deferred();
  const app = loadContract(t, { gatewayActivation: gatewayActivation.promise, activationTimeoutMs: 25 });
  await tick();
  const result = await app.request({ ...owner, action: "chat", message: "hello" });
  assert.equal(result.body.status, "error");
  assert.equal(app.activations.length, 1);
  assert.equal(app.activations[0].signal.aborted, true);
  assert.ok(app.children.every((child) => child.exitCode === 0));
  gatewayActivation.resolve();
  await tick();
  assert.ok(!app.calls.includes("activate:ack"));
  assert.ok(!app.calls.includes("gateway:verified"));
  assert.ok(!app.calls.includes("chat.send"));
  assert.equal((await app.request(null, "GET", "/ping")).code, 503);
});

test("headless config preserves the system cache boundary for main and subagent models", async (t) => {
  const app = loadContract(t);
  await tick();
  const message = "Explain this user-supplied text: Runtime: agent=main";
  const result = await app.request({ ...owner, action: "chat", message });
  assert.equal(result.body.response, "reply", app.errors.join("\n"));
  const config = JSON.parse(app.files.get("/test-home/.openclaw/openclaw.json"));
  const provider = config.models.providers.agentcore;
  assert.equal(provider.api, "openai-completions");
  assert.equal(provider.models.length, 2);
  for (const alias of [config.agents.defaults.model.primary, config.agents.defaults.subagents.model]) {
    const model = provider.models.find((entry) => `agentcore/${entry.id}` === alias);
    assert.ok(model, `Missing model definition for ${alias}`);
    assert.deepEqual(model.compat, {
      cacheControlFormat: "anthropic",
      supportsDeveloperRole: false,
      requiresStringContent: false,
    });
    assert.equal(config.agents.defaults.models[alias].params.cacheRetention, "short");
  }
  assert.ok(app.calls.includes(message), "user message must reach the gateway unchanged");
});

test("restore failure blocks every engine and another owner's request cannot overwrite identity", async (t) => {
  const restore = deferred();
  const app = loadContract(t, { restore: restore.promise });
  await tick();
  const chat = app.request({ ...owner, action: "chat", message: "hello" });
  await tick();
  const identity = app.files.get("/tmp/current-identity.json");
  const rejected = await app.request({ ...owner, actorId: "telegram:2", action: "chat", message: "other" });
  assert.equal(rejected.body.status, "error");
  assert.equal(app.files.get("/tmp/current-identity.json"), identity);
  restore.reject(new Error("S3 denied"));
  assert.equal((await chat).body.status, "error");
  assert.deepEqual(app.calls.filter((call) => call.startsWith("spawn:")), ["spawn:prepared-openclaw"]);
  assert.equal(app.activations.length, 0);
  assert.equal(app.children[0].exitCode, 0);
  assert.ok(!app.calls.includes("chat.send"));
  assert.equal((await app.request(null, "GET", "/ping")).code, 503);
});

for (const action of ["chat", "cron"]) {
  test(`${action} unknown outcome is returned once and closes admission`, async (t) => {
    const app = loadContract(t, { outcome: { phase: "unknown_outcome", response: "" } });
    await tick();
    const result = await app.request({ ...owner, action, message: "hello" });
    assert.equal(result.body.status, "unknown_outcome", app.errors.join("\n"));
    assert.equal(result.body.execution.phase, "unknown_outcome");
    assert.equal(result.body.phase, "FAILED");
    assert.equal(app.calls.filter((call) => call === "chat.send").length, 1);
    assert.equal((await app.request(null, "GET", "/ping")).code, 503);
  });
}

for (const stream of [false, true]) {
  for (const phase of ["completed", "error"]) {
    test(`proxy unknown latch overrides Gateway ${phase} with stream=${stream} without replay`, async (t) => {
      t.mock.method(console, "log", () => {});
      let healthReads = 0;
      const app = loadContract(t, {
        proxyHealth: (calls) => {
          healthReads++;
          return { status: "ok", model_outcome_unknown: calls.includes("chat.send") };
        },
        gatewayTurnImpl: async (args) => {
          args.onDelta("Partial claim");
          return { phase, response: "Partial claim", runId: "original-run" };
        },
      });
      const result = await app.request({ ...owner, action: "chat", message: "do it", stream });
      assert.equal(healthReads, 4, "activation, readiness, pre-turn and post-turn probes");
      if (stream) {
        assert.deepEqual(result.events.map((event) => event.event), ["ready", "delta", "error"]);
        assert.equal(result.events[1].data.text, "Partial claim");
        assert.equal(result.events[2].data.code, "UNKNOWN_OUTCOME");
      } else {
        assert.equal(result.body.status, "unknown_outcome");
        assert.equal(result.body.execution.phase, "unknown_outcome");
        assert.equal(result.body.execution.run_id, "original-run");
        assert.ok(!result.body.response.includes("Partial claim"));
      }
      const next = await app.request({ ...owner, action: "chat", message: "next", stream });
      assert.equal(next.body.status, "unknown_outcome");
      assert.equal(next.body.execution.phase, "unknown_outcome");
      assert.equal(healthReads, 4);
      assert.equal(app.calls.filter((call) => call === "chat.send").length, 1);
      assert.equal((await app.request(null, "GET", "/ping")).code, 503);
    });
  }
}

for (const disconnected of [false, true]) {
  test(`proxy unknown latch preserves Telegram delivery ownership disconnected=${disconnected}`, async (t) => {
    const delivered = [];
    const app = loadContract(t, {
      proxyHealth: (calls) => ({ status: "ok", model_outcome_unknown: calls.includes("chat.send") }),
      gatewayTurnImpl: async (args) => {
        args.onDelta("Unconfirmed tool result");
        return { phase: "completed", response: "Unconfirmed tool result" };
      },
    });
    app.stubTelegramApi(delivered);
    const result = await app.request({ ...owner, channel: "telegram", channelActorId: "telegram:1",
      channelTarget: "1", action: "chat", message: "do it" }, "POST", "/invocations", disconnected);
    assert.equal(result.body.status, "unknown_outcome");
    assert.equal(result.body.streamed, disconnected ? true : undefined);
    assert.equal(delivered.length, disconnected ? 1 : 0);
    if (disconnected) assert.equal(delivered[0].text, result.body.response);
    assert.ok(!result.body.response.includes("Unconfirmed tool result"));
    assert.equal(app.calls.filter((call) => call === "chat.send").length, 1);
  });
}

test("an already latched proxy rejects before Gateway dispatch", async (t) => {
  let probes = 0;
  const app = loadContract(t, {
    proxyHealth: () => ({ status: "ok", model_outcome_unknown: ++probes >= 3 }),
  });
  const result = await app.request({ ...owner, action: "chat", message: "do it" });
  assert.equal(result.body.status, "unknown_outcome");
  assert.equal(result.body.execution.phase, "unknown_outcome");
  assert.equal(result.body.execution.run_id, null);
  assert.equal(probes, 3);
  assert.ok(!app.calls.includes("chat.send"));
  assert.equal((await app.request({ ...owner, action: "warmup" })).body.status, "unknown_outcome");
});

for (const health of [null, new Error("unreachable"), { status: "ok" },
  { status: "ok", model_outcome_unknown: "false" }]) {
  test(`unverified pre-turn health after readiness never dispatches: ${JSON.stringify(health)}`, async (t) => {
    let probes = 0;
    const app = loadContract(t, {
      proxyHealth: () => ++probes < 3 ? { status: "ok", model_outcome_unknown: false } : health,
    });
    const result = await app.request({ ...owner, action: "chat", message: "do it" });
    assert.equal(result.body.status, "unknown_outcome");
    assert.equal(result.body.execution.phase, "unknown_outcome");
    assert.equal(probes, 3);
    assert.ok(!app.calls.includes("chat.send"));
    assert.equal((await app.request({ ...owner, action: "chat", message: "next" })).body.status, "unknown_outcome");
    assert.equal(probes, 3);
    assert.ok(!app.calls.includes("chat.send"));
  });
}

for (const health of [null, new Error("health disconnected"), "{invalid", { status: "ok" },
  { status: "ok", model_outcome_unknown: "false" }]) {
  test(`unverified post-turn proxy health remains unknown: ${JSON.stringify(health)}`, async (t) => {
    const app = loadContract(t, {
      proxyHealth: (calls) => calls.includes("chat.send") ? health : { status: "ok", model_outcome_unknown: false },
    });
    const result = await app.request({ ...owner, action: "chat", message: "do it" });
    assert.equal(result.body.status, "unknown_outcome");
    assert.equal(result.body.execution.phase, "unknown_outcome");
    await app.request({ ...owner, action: "chat", message: "next" });
    assert.equal(app.calls.filter((call) => call === "chat.send").length, 1);
  });
}

test("missing pre-turn proxy outcome status remains unknown without dispatch or Telegram delivery", async (t) => {
  const delivered = [];
  const app = loadContract(t, { proxyHealth: () => ({ status: "ok" }) });
  app.stubTelegramApi(delivered);
  const result = await app.request({ ...owner, channel: "telegram", channelActorId: "telegram:1",
    channelTarget: "1", action: "chat", message: "do it" });
  assert.equal(result.body.status, "unknown_outcome");
  assert.equal(result.body.execution.phase, "unknown_outcome");
  assert.match(result.body.response, /outcome is unknown/);
  assert.ok(!app.calls.includes("chat.send"));
  assert.equal(delivered.length, 0);
  assert.equal((await app.request(null, "GET", "/ping")).code, 503);
});

test("a queued request inherits the unknown outcome without another dispatch", async (t) => {
  const pending = deferred();
  const entered = deferred();
  const app = loadContract(t, {
    proxyHealth: (calls) => ({ status: "ok", model_outcome_unknown: calls.includes("chat.send") }),
    gatewayTurnImpl: () => { entered.resolve(); return pending.promise; },
  });
  const first = app.request({ ...owner, action: "chat", message: "first" });
  await entered.promise;
  const second = app.request({ ...owner, action: "chat", message: "second" });
  await tick();
  pending.resolve({ phase: "error", response: "" });
  assert.equal((await first).body.status, "unknown_outcome");
  assert.equal((await second).body.status, "unknown_outcome");
  assert.equal(app.calls.filter((call) => call === "chat.send").length, 1);
});

test("a thrown Gateway call remains unknown even when proxy health is clear", async (t) => {
  const app = loadContract(t, { gatewayTurnImpl: async () => { throw new Error("lost completion"); } });
  const result = await app.request({ ...owner, action: "chat", message: "do it" });
  assert.equal(result.body.status, "unknown_outcome");
  assert.equal(result.body.execution.phase, "unknown_outcome");
  assert.ok(!result.body.response.includes("No turn was dispatched"));
  assert.equal((await app.request({ ...owner, action: "chat", message: "next" })).body.status, "unknown_outcome");
  assert.equal(app.calls.filter((call) => call === "chat.send").length, 1);
});

test("a known Gateway error with clear proxy health is not retried or mislabeled unknown", async (t) => {
  const app = loadContract(t, { outcome: { phase: "error", response: "" } });
  const result = await app.request({ ...owner, action: "chat", message: "do it" });
  assert.equal(result.body.status, "error");
  assert.equal(result.body.execution.phase, "error");
  assert.equal(app.calls.filter((call) => call === "chat.send").length, 1);
  assert.equal((await app.request(null, "GET", "/ping")).code, 200);
});

test("proxy outcome health probes have an absolute deadline and bounded response size", async () => {
  const source = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf8");
  const begin = source.indexOf("function checkProxyHealth()");
  const end = source.indexOf("\n/**", begin);
  for (const mode of ["deadline", "oversized", "aborted"]) {
    let respond, deadline, destroyed = 0, cleared = 0;
    const request = new EventEmitter();
    request.setTimeout = (ms) => { assert.equal(ms, 2000); };
    request.destroy = () => { destroyed++; };
    const box = vm.createContext({
      Buffer, PROXY_PORT: 18790,
      http: { get(_url, callback) { respond = callback; return request; } },
      setTimeout(callback, ms) { assert.equal(ms, 2000); deadline = callback; return 1; },
      clearTimeout() { cleared++; },
    });
    vm.runInContext(source.slice(begin, end), box);
    const result = box.checkProxyHealth();
    const response = new EventEmitter();
    response.statusCode = 200;
    respond(response);
    response.emit("data", "{");
    if (mode === "deadline") deadline();
    else if (mode === "oversized") response.emit("data", "x".repeat(65536));
    else response.emit("aborted");
    assert.equal(await result, null);
    assert.equal(cleared, 1);
    assert.equal(destroyed, mode === "aborted" ? 0 : 1);
  }
});

test("privilege separation: gateway + doctor spawn unprivileged, state dir handed over, status logged", async (t) => {
  const privsepMock = {
    childPrivilegeOptions: () => ({ uid: 10001, gid: 10001 }),
    chownToOpenClaw: (p) => { privsepMock._chowns.push(p); return true; },
    agentDirEnv: () => ({ XDG_CACHE_HOME: "/home/openclaw/.cache", TMPDIR: "/home/openclaw/tmp" }),
    ensureAgentDirs: () => { privsepMock._ensured += 1; return true; },
    _ensured: 0,
    describeProtections: (x) => ({ uidSplit: "active", uid: 10001, networkBlock: x.networkBlock, procHidden: x.procHidden }),
    formatStartupLines: (st) => [`[privsep] uid-split=${st.uidSplit} mmds=${st.networkBlock} hidepid=${st.procHidden}`],
    _chowns: [],
  };
  const app = loadContract(t, {
    privsepMock, migration: Promise.resolve(),
    runtimeEnv: { OPENCLAW_MMDS_BLOCK: "active", OPENCLAW_PROC_HIDDEN: "inactive" },
  });
  await tick();
  const reply = await app.request({ ...owner, action: "chat", message: "hello" });
  assert.equal(reply.body.execution.phase, "completed", app.errors.join("\n"));
  assert.equal(reply.body.response, "reply");
  // The prepared gateway was told to run as the unprivileged uid/gid.
  assert.deepEqual(app.preparations[0].childOptions, { uid: 10001, gid: 10001 });
  // openclaw doctor ran with the same uid/gid.
  const doctor = app.children.find((child) => child.kind === "doctor");
  assert.ok(doctor, "doctor was spawned");
  assert.equal(doctor.uid, 10001);
  assert.equal(doctor.gid, 10001);
  // The state dir was handed to the OpenClaw uid (before doctor and before activate).
  assert.ok(app.preparations.length);
  assert.ok(privsepMock._chowns.filter((p) => p === "/test-home/.openclaw").length >= 2,
    `state dir chowned, got: ${JSON.stringify(privsepMock._chowns)}`);
  // The agent uid gets writable cache/temp dirs (HOME=/root is not writable for
  // it): created once at bootstrap, and in the env of the prepared gateway, the
  // activation and doctor. HOME itself is unchanged.
  assert.equal(privsepMock._ensured, 1);
  for (const [name, env] of [["prepared", app.preparations[0].env], ["activation", app.activations[0].env],
    ["doctor", doctor.env]]) {
    assert.equal(env.XDG_CACHE_HOME, "/home/openclaw/.cache", name);
    assert.equal(env.TMPDIR, "/home/openclaw/tmp", name);
    assert.equal(env.HOME, "/test-home", name);
  }
  // The startup log states which protections are active.
  assert.ok(app.logs.includes("[privsep] uid-split=active mmds=active hidepid=inactive"),
    "privsep status logged at bootstrap");
});

for (const platform of ["V1", "", "v2", " V2 ", "V3"]) {
  test(`invalid platform ${JSON.stringify(platform)} fails before loading dependencies or shell preflight`, () => {
    let imports = 0;
    const source = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf8");
    assert.throws(() => vm.runInNewContext(source, {
      process: { env: { AGENTCORE_RUNTIME_PLATFORM: platform } },
      require() { imports++; throw new Error("Unexpected dependency import"); },
    }), /AGENTCORE_RUNTIME_PLATFORM must be V2 when set/);
    assert.equal(imports, 0);
    const result = spawnSync("/bin/bash", [path.join(__dirname, "entrypoint.sh")], {
      env: { AGENTCORE_RUNTIME_PLATFORM: platform, PATH: "/nonexistent" }, encoding: "utf8",
    });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /AGENTCORE_RUNTIME_PLATFORM must be V2 when set/);
    assert.equal(result.stdout, "");
  });
}

test("required Browser failure prevents chat and readiness", async (t) => {
  const browser = deferred();
  const app = loadContract(t, { browser: browser.promise });
  await tick();
  const chat = app.request({ ...owner, action: "chat", message: "hello" });
  await tick();
  browser.reject(new Error("Browser unavailable"));
  assert.equal((await chat).body.status, "error");
  assert.ok(!app.calls.includes("chat.send"));
  assert.equal((await app.request(null, "GET", "/ping")).code, 503);
});

test("credential failure on dispatch cannot use prior gateway readiness", async (t) => {
  const app = loadContract(t, { failCredentialsAfter: 2 });
  await tick();
  const chat = await app.request({ ...owner, action: "chat", message: "hello" });
  assert.equal(chat.body.status, "error");
  assert.ok(app.calls.includes("gateway:verified"), "initial gateway activation completed");
  assert.ok(!app.calls.includes("chat.send"));
  assert.equal((await app.request(null, "GET", "/ping")).code, 503);
});

test("expired bearer restarts the gateway and expired Browser is replaced before another turn", async (t) => {
  const app = loadContract(t, { gatewayMcpEnabled: true });
  await tick();
  const first = await app.request({ ...owner, action: "chat", message: "first" });
  assert.equal(first.body.execution.phase, "completed", app.errors.join("\n"));
  app.expireResources();
  const second = await app.request({ ...owner, action: "chat", message: "second", runtimePlatform: "V1", phase: "FAILED" });
  assert.equal(second.body.phase, "USER_READY", app.errors.join("\n"));
  assert.equal(second.body.runtimePlatform, "V2");
  assert.equal(second.body.activationGeneration, first.body.activationGeneration);
  assert.equal(app.calls.filter((call) => call === "spawn:prepared-openclaw").length, 1);
  assert.equal(app.calls.filter((call) => call === "spawn:openclaw").length, 1);
  assert.equal(app.activations.length, 1);
  const restart = app.children.find((child) => child.kind === "openclaw");
  for (const key of ["OPENCLAW_AGENTCORE_PREINIT", "OPENCLAW_NO_RESPAWN", "NODE_DISABLE_COMPILE_CACHE"]) {
    assert.equal(Object.hasOwn(restart.env, key), false, key);
  }
  assert.equal(restart.env.USER_ID, owner.actorId);
  assert.equal(restart.env.INTERNAL_USER_ID, owner.userId);
  assert.equal(app.calls.filter((call) => call === "browser:start").length, 2);
  assert.equal(app.calls.filter((call) => call === "browser:stop").length, 1);
  assert.equal(app.calls.filter((call) => call === "bearer:refresh").length, 1);
  assert.equal(app.calls.filter((call) => call === "chat.send").length, 2);
});

test("Browser acquired after abort stops itself while the first cleanup is still waiting", async (t) => {
  const browser = deferred();
  const cleanupStarted = deferred();
  const releaseCleanup = deferred();
  const app = loadContract(t, {
    browser: browser.promise, activationTimeoutMs: 25,
    cleanup: async () => { cleanupStarted.resolve(); await releaseCleanup.promise; },
  });
  t.after(() => releaseCleanup.resolve());
  await tick();
  const chat = app.request({ ...owner, action: "chat", message: "hello" });
  await cleanupStarted.promise;
  browser.resolve();
  await tick();
  assert.equal(app.calls.filter((call) => call === "browser:stop").length, 1);
  assert.ok(!app.files.has("/tmp/agentcore-browser-session.json"));
  releaseCleanup.resolve();
  assert.equal((await chat).body.status, "error");
  assert.ok(!app.calls.includes("chat.send"));
});

for (const failure of [false, true]) {
  test(`old Browser signing ${failure ? "failure" : "result"} cannot change a replacement session`, async (t) => {
    const signingStarted = deferred();
    const releaseSign = deferred();
    let delayOldSign = false;
    const app = loadContract(t, {
      signBrowser: async (request) => {
        if (delayOldSign && request.path === "/b1") {
          delayOldSign = false;
          signingStarted.resolve();
          await releaseSign.promise;
          if (failure) throw new Error("Old signing failed");
          return { headers: { Authorization: "stale-signature" } };
        }
        return { headers: { Authorization: `signed:${request.path}` } };
      },
    });
    await tick();
    await app.request({ ...owner, action: "chat", message: "first" });
    const oldRefresh = app.browserRefresh();
    delayOldSign = true;
    const signing = oldRefresh();
    await signingStarted.promise;
    app.expireBrowser();
    const replacement = await app.request({ ...owner, action: "chat", message: "second" });
    assert.equal(replacement.body.phase, "USER_READY");
    const before = app.files.get("/tmp/agentcore-browser-session.json");
    assert.equal(JSON.parse(before).sessionId, "b2");
    releaseSign.resolve();
    await signing;
    assert.equal(app.files.get("/tmp/agentcore-browser-session.json"), before);
    assert.equal((await app.request(null, "GET", "/ping")).code, 200);
    assert.ok(!app.errors.some((error) => error.includes("Old signing failed")));
  });
}

test("future local expiry cannot mask a remotely terminated Browser session", async (t) => {
  const app = loadContract(t);
  await tick();
  await app.request({ ...owner, action: "chat", message: "first" });
  app.remoteBrowserStatus.set("b1", "TERMINATED");
  const result = await app.request({ ...owner, action: "chat", message: "second" });
  assert.equal(result.body.phase, "USER_READY", app.errors.join("\n"));
  assert.equal(JSON.parse(app.files.get("/tmp/agentcore-browser-session.json")).sessionId, "b2");
  assert.ok(app.calls.includes("browser:get:b1"));
  assert.ok(app.calls.includes("browser:get:b2"));
});

for (const status of ["UNKNOWN", new Error("Browser status denied")]) {
  test(`unknown Browser status fails closed: ${String(status)}`, async (t) => {
    const app = loadContract(t);
    await tick();
    await app.request({ ...owner, action: "chat", message: "first" });
    app.remoteBrowserStatus.set("b1", status);
    const result = await app.request({ ...owner, action: "chat", message: "second" });
    assert.equal(result.body.status, "error");
    assert.equal(app.calls.filter((call) => call === "browser:start").length, 1);
    assert.equal(app.calls.filter((call) => call === "chat.send").length, 1);
    assert.equal((await app.request(null, "GET", "/ping")).code, 503);
  });
}

test("installed AWS SDK serializes GetBrowserSession as a read-only GET with READY/TERMINATED states", async () => {
  const { BedrockAgentCoreClient, GetBrowserSessionCommand, BrowserSessionStatus } =
    require("@aws-sdk/client-bedrock-agentcore");
  assert.deepEqual(BrowserSessionStatus, { READY: "READY", TERMINATED: "TERMINATED" });
  const client = new BedrockAgentCoreClient({
    region: "us-west-2", credentials: { accessKeyId: "test", secretAccessKey: "test" },
    requestHandler: {
      async handle(request) {
        assert.equal(request.method, "GET");
        assert.equal(request.path, "/browsers/browser/sessions/get");
        assert.equal(request.query.sessionId, "session");
        return { response: { statusCode: 200, headers: {}, body: Buffer.from(JSON.stringify({
          browserIdentifier: "browser", sessionId: "session", status: "READY", createdAt: "2026-10-07T00:00:00Z",
        })) } };
      },
    },
  });
  const response = await client.send(new GetBrowserSessionCommand({ browserIdentifier: "browser", sessionId: "session" }));
  assert.equal(response.status, "READY");
  client.destroy();
});

for (const [phase, disconnected] of ["unknown_outcome", "error", "aborted", "completed"]
  .flatMap((phase) => [[phase, false], [phase, true]])) {
  test(`partial ${phase} text preserves final delivery with the caller ${disconnected ? "disconnected" : "connected"}`, async (t) => {
    const sent = [];
    const delivered = [];
    const partial = "I have submitted your request.";
    class Socket extends EventEmitter {
      constructor() {
        super();
        queueMicrotask(() => this.frame({ type: "event", event: "connect.challenge" }));
      }
      frame(frame) { this.emit("message", Buffer.from(JSON.stringify(frame))); }
      send(raw, callback) {
        const frame = JSON.parse(raw);
        sent.push(frame.method);
        queueMicrotask(() => {
          const reply = (payload) => this.frame({ type: "res", id: frame.id, ok: true, payload });
          if (frame.method === "connect") reply({
            type: "hello-ok", protocol: 4, features: { methods: ["chat.send", "last-heartbeat"] },
          });
          else if (frame.method === "last-heartbeat") reply(null);
          else {
            reply({ runId: frame.id, status: "accepted" });
            const payload = { runId: frame.id, sessionKey: "agent:main:main", text: partial };
            this.frame({ type: "event", event: "chat", payload: { ...payload, state: "delta" } });
            if (phase === "unknown_outcome") this.emit("close");
            else this.frame({ type: "event", event: "chat", payload: {
              ...payload, state: phase === "completed" ? "final" : phase,
            } });
          }
          callback?.();
        });
      }
      terminate() {}
    }
    const box = {
      module: { exports: {} }, require: (name) => name === "ws" ? Socket : require(name),
      setTimeout, clearTimeout,
    };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, "gateway-lifecycle.js"), "utf8"), box);
    const app = loadContract(t, { gatewayTurnImpl: box.module.exports.gatewayTurn });
    app.stubTelegramApi(delivered);
    const result = await app.request(
      { ...owner, channel: "telegram", channelActorId: "telegram:1", channelTarget: "1",
        action: "chat", message: "Do the task" },
      "POST", "/invocations", disconnected,
    );
    assert.equal(result.body.execution.phase, phase);
    assert.equal(result.body.streamed, disconnected ? true : undefined);
    assert.equal(delivered.length, disconnected ? 1 : 0);
    if (disconnected) {
      assert.equal(delivered[0].method, "sendMessage");
      assert.equal(delivered[0].text, result.body.response);
    }
    assert.equal(sent.filter((method) => method === "chat.send").length, 1);
    if (phase === "completed") {
      assert.equal(result.body.response, partial);
      assert.equal(result.body.status, undefined);
    } else {
      assert.notEqual(result.body.response, partial);
      assert.ok(!result.body.response.includes(partial), "unfinished claims must not be delivered as a result");
      assert.equal(result.body.status, phase === "unknown_outcome" ? "unknown_outcome" : "error");
      if (phase === "unknown_outcome") {
        assert.match(result.body.response, /outcome is unknown/i);
        assert.match(result.body.response, /not been (automatically )?retried/i);
        assert.equal(result.body.phase, "FAILED");
        await app.request({ ...owner, action: "chat", message: "another" });
        assert.equal(sent.filter((method) => method === "chat.send").length, 1);
      } else assert.match(result.body.response, /could not complete|aborted|failed/i);
    }
  });
}

for (const deadline of [false, true]) {
  test(`shutdown ${deadline ? "deadline fences a pending refresh" : "flushes after drain before stopping credentials"}`, async (t) => {
    const scoped = require("./scoped-credentials");
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "shutdown-credentials-"));
    let manager;
    let now = Date.now();
    let refreshes = 0;
    let publications = 0;
    const waiting = deferred();
    const started = deferred();
    const saved = [];
    t.after(() => { manager?.stop(); fs.rmSync(dir, { recursive: true, force: true }); });
    const app = loadContract(t, {
      managerFactory(namespace, options) {
        assert.equal(namespace, "telegram_1");
        assert.equal(options.internalUserId, "u1");
        manager = scoped.createCredentialManager(namespace, {
          ...options, credDir: dir, now: () => now,
          refresh: async () => {
            refreshes++;
            if (refreshes > 1) {
              started.resolve();
              if (deadline) await waiting.promise;
            }
            return {
              accessKeyId: `key-${refreshes}`, secretAccessKey: "secret", sessionToken: "token",
              expiration: new Date(now + 3600000),
            };
          },
          onRefresh(next, prepared) { publications++; options.onRefresh(next, prepared); },
        });
        return manager;
      },
      async persistenceSave(stage, provider) {
        assert.equal(typeof provider, "function", "runtime must bind a persistence provider");
        try {
          saved.push({ stage, key: (await provider()).accessKeyId });
        } catch (error) {
          if (!deadline) throw error;
          assert.match(error.message, /stopped/);
        }
      },
    });
    const result = await app.request({ ...owner, action: "chat", message: "hello" });
    assert.equal(result.body.execution.phase, "completed");
    const original = fs.readFileSync(path.join(dir, "scoped-creds.json"), "utf8");
    now += 3600001;
    const shutdown = app.shutdown();
    if (deadline) {
      await Promise.race([started.promise, shutdown]);
      app.shutdownDeadline();
      waiting.resolve();
    } else {
      await app.failLifecycle();
    }
    await shutdown;
    assert.equal(publications, 1);
    assert.equal(fs.readFileSync(path.join(dir, "scoped-creds.json"), "utf8"), original);
    if (deadline) assert.deepEqual(saved, []);
    else assert.deepEqual(saved, [
      { stage: "urgent", key: "key-2" }, { stage: "changed", key: "key-2" }, { stage: "full", key: "key-2" },
    ]);
    await assert.rejects(manager.ensureFresh({ forPersistence: true }), /stopped/);
    assert.equal((await app.request(null, "GET", "/ping")).code, 503);
  });
}

async function createCronForIdentity(identity, args = [], namespace = "telegram_123") {
  const schedules = [];
  const records = [];
  const errors = [];
  class Command { constructor(input) { this.input = input; } }
  class QueryCommand extends Command {}
  const env = {
    AWS_REGION: "us-west-2", EVENTBRIDGE_SCHEDULE_GROUP: "group", CRON_LAMBDA_ARN: "cron",
    EVENTBRIDGE_ROLE_ARN: "scheduler-role", IDENTITY_TABLE_NAME: "identity", INTERNAL_USER_ID: "u1",
  };
  const processStub = {
    env, argv: ["node", "create.js", namespace, "rate(1 hour)", "UTC", "remind me", ...args],
    exit(code) { throw new Error(`Skill exited with code ${code}: ${errors.join(" ")}`); },
  };
  const requireStub = (name) => {
    if (name === "fs") return { readFileSync(file) {
      assert.equal(file, "/tmp/current-identity.json");
      return JSON.stringify(identity);
    } };
    if (name === "@aws-sdk/client-dynamodb") return { DynamoDBClient: class {} };
    if (name === "@aws-sdk/lib-dynamodb") return {
      DynamoDBDocumentClient: { from: () => ({ async send(command) {
        // create.js counts the caller's CRON# records before creating one.
        if (command instanceof QueryCommand) return { Items: records.slice(), Count: records.length };
        records.push(command.input.Item);
        return {};
      } }) },
      PutCommand: Command, GetCommand: Command, DeleteCommand: Command, UpdateCommand: Command,
      QueryCommand,
    };
    if (name === "@aws-sdk/client-scheduler") return {
      SchedulerClient: class { async send(command) { schedules.push(command.input); return {}; } },
      CreateScheduleCommand: Command,
    };
    if (name === "crypto") return require(name);
    throw new Error(`Unexpected skill dependency: ${name}`);
  };
  const base = { process: processStub, console: { log() {}, error(message) { errors.push(message); } } };
  const common = { ...base, module: { exports: {} }, require: requireStub };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "skills/eventbridge-cron/common.js"), "utf8"), common);
  await vm.runInNewContext(fs.readFileSync(path.join(__dirname, "skills/eventbridge-cron/create.js"), "utf8"), {
    ...base, require: (name) => name === "./common" ? common.module.exports : requireStub(name),
  });
  return { schedules, records };
}

test("a linked Slack turn and its Cron retain delivery metadata separately from the canonical owner", async (t) => {
  const channelTarget = "C789";
  const deliveries = [];
  const app = loadContract(t);
  app.stubTelegramApi(deliveries);
  const canonical = { ...owner, actorId: "telegram:123" };
  const first = await app.request({
    ...canonical, channel: "telegram", channelActorId: "telegram:123", channelTarget: "123",
    action: "chat", message: "first",
  });
  const reply = await app.request({
    ...canonical, channel: "slack", channelActorId: "slack:U456", channelTarget,
    action: "chat", message: "schedule a reminder",
  }, "POST", "/invocations", true);
  assert.equal(reply.body.execution.phase, "completed", app.errors.join("\n"));
  assert.equal(reply.body.actorId, "telegram:123");
  assert.equal(reply.body.activationGeneration, first.body.activationGeneration);
  assert.equal(reply.body.channel, "slack");
  assert.equal(reply.body.channelActorId, "slack:U456");
  assert.deepEqual(reply.body.channelTarget, channelTarget);
  assert.equal(reply.body.streamed, undefined, "Slack delivery remains with the router");
  assert.deepEqual(deliveries, [], "a Slack turn must never send to Telegram");
  assert.equal(app.activations.length, 1);
  assert.equal(app.activations[0].env.USER_ID, "telegram:123");
  assert.equal(app.activations[0].env.INTERNAL_USER_ID, "u1");
  const identity = JSON.parse(app.files.get("/tmp/current-identity.json"));
  assert.deepEqual(identity, { actorId: "telegram:123", channel: "slack", channelActorId: "slack:U456", channelTarget,
    trigger: "chat", sessionId: "session-1" });
  const { schedules, records } = await createCronForIdentity(identity);
  assert.equal(schedules.length, 1);
  assert.match(schedules[0].Name, /^openclaw-telegram_123-/);
  assert.equal(records[0].PK, "USER#u1");
  for (const metadata of [JSON.parse(schedules[0].Target.Input), records[0]]) {
    assert.equal(metadata.actorId, "telegram:123");
    assert.equal(metadata.channelActorId, "slack:U456");
    assert.equal(metadata.channel, "slack");
    assert.equal(metadata.channelTarget, channelTarget);
  }
  const rejected = await app.request({
    ...canonical, actorId: "slack:U456", channel: "slack", channelActorId: "slack:U456", channelTarget,
    action: "chat", message: "replace owner",
  });
  assert.equal(rejected.body.status, "error");
  assert.deepEqual(JSON.parse(app.files.get("/tmp/current-identity.json")), identity);
});

for (const failedOperation of ["write", "rename"]) {
  test(`identity ${failedOperation} failure blocks a linked Slack turn and Cron creation with stale Telegram metadata`, async (t) => {
    const identityFile = "/tmp/current-identity.json";
    const schedules = [];
    const records = [];
    let failPublication = false;
    let failedAttempts = 0;
    let previousIdentity;
    const app = loadContract(t, {
      beforeFsOperation(operation, filename) {
        if (!failPublication || operation !== failedOperation || !filename.startsWith(identityFile)) return;
        failedAttempts++;
        assert.equal(app.files.get(identityFile), previousIdentity);
        if (operation === "write") app.files.set(filename, '{"channel":"slack"');
        throw Object.assign(new Error(`EACCES: identity ${operation} denied`), { code: "EACCES" });
      },
      async gatewayTurnImpl({ message }) {
        if (message === "schedule a reminder") {
          const created = await createCronForIdentity(JSON.parse(app.files.get(identityFile)));
          schedules.push(...created.schedules);
          records.push(...created.records);
        }
        return { phase: "completed", response: "reply" };
      },
    });
    const canonical = { ...owner, actorId: "telegram:123" };
    const first = await app.request({
      ...canonical, channel: "telegram", channelActorId: "telegram:123", channelTarget: "123",
      action: "chat", message: "first",
    });
    assert.equal(first.body.execution.phase, "completed");
    previousIdentity = app.files.get(identityFile);
    assert.equal(JSON.parse(previousIdentity).channel, "telegram");
    failPublication = true;
    const slackTurn = {
      ...canonical, channel: "slack", channelActorId: "slack:U456", channelTarget: "C789",
      action: "chat", message: "schedule a reminder",
    };
    const rejected = await app.request(slackTurn);
    assert.equal(rejected.body.status, "error");
    assert.equal(failedAttempts, 1);
    assert.equal(app.calls.filter((call) => call === "chat.send").length, 1);
    assert.deepEqual(schedules, []);
    assert.deepEqual(records, []);
    assert.equal(app.files.get(identityFile), previousIdentity);
    assert.deepEqual([...app.files.keys()].filter((name) => name.startsWith(`${identityFile}.`)), []);
    assert.ok(app.errors.some((error) => error.includes("EACCES")));

    failPublication = false;
    const retried = await app.request(slackTurn);
    assert.equal(retried.body.execution.phase, "completed");
    assert.equal(schedules.length, 1);
    assert.equal(records.length, 1);
    for (const metadata of [JSON.parse(schedules[0].Target.Input), records[0]]) {
      assert.equal(metadata.actorId, "telegram:123");
      assert.equal(metadata.channelActorId, "slack:U456");
      assert.equal(metadata.channel, "slack");
      assert.equal(metadata.channelTarget, "C789");
    }
  });
}

test("a Telegram group turn is not streamed by the contract, so the router can thread the reply", async (t) => {
  const deliveries = [];
  const app = loadContract(t);
  app.stubTelegramApi(deliveries);
  const reply = await app.request({
    ...owner, channel: "telegram_group", channelActorId: "telegram_group:-100777",
    channelTarget: "-100777", action: "chat", message: "hello",
  }, "POST", "/invocations", true);
  assert.equal(reply.body.actorId, owner.actorId);
  assert.equal(typeof reply.body.response, "string");
  assert.notEqual(reply.body.streamed, true);
  assert.deepEqual(deliveries, []);
});

test("Telegram fallback delivery uses the channel target instead of the canonical or sender identity", async (t) => {
  const deliveries = [];
  const app = loadContract(t);
  app.stubTelegramApi(deliveries);
  const reply = await app.request({
    ...owner, actorId: "slack:U456", channel: "telegram", channelActorId: "telegram:123",
    channelTarget: "-100777", action: "chat", message: "hello",
  }, "POST", "/invocations", true);
  assert.equal(reply.body.actorId, "slack:U456");
  assert.equal(reply.body.streamed, true);
  assert.deepEqual(deliveries.map((entry) => entry.chatId), ["-100777"]);
});

test("Cron creation rejects missing delivery metadata and cannot infer Telegram from its namespace", async () => {
  await assert.rejects(createCronForIdentity({ actorId: "telegram:123", channel: "slack" }), /delivery identity is unavailable/);
  await assert.rejects(createCronForIdentity({
    actorId: "telegram:123", channel: "slack", channelActorId: "slack:U456", channelTarget: "C789",
  }, ["telegram", "123"]), /channel must match the current conversation/);
});

test("Cron creation is refused in a Telegram group, before any schedule is written", async () => {
  const group = "telegram_group:-1001234567890";
  for (const args of [[], ["", ""], ["telegram", "-1001234567890"]]) {
    await assert.rejects(createCronForIdentity({
      actorId: group, channel: "telegram_group", channelActorId: group, channelTarget: "-1001234567890",
    }, args, "telegram_group_-1001234567890"), /not available in group chats/);
  }
});

for (const args of [["", ""], ["slack", ""], ["", "C789"], ["", "", "named reminder"], [null, null]]) {
  test(`Cron optional delivery arguments use the current conversation: ${JSON.stringify(args)}`, async () => {
    const identity = { actorId: "telegram:123", channel: "slack", channelActorId: "slack:U456", channelTarget: "C789" };
    const { schedules, records } = await createCronForIdentity(identity, args);
    assert.equal(schedules.length, 1);
    assert.equal(records.length, 1);
    assert.equal(records[0].PK, "USER#u1");
    for (const metadata of [JSON.parse(schedules[0].Target.Input), records[0]]) {
      for (const [key, value] of Object.entries(identity)) assert.equal(metadata[key], value);
    }
  });
}

test("Cron optional empty arguments preserve owner and channel validation", async () => {
  const identity = { actorId: "telegram:123", channel: "slack", channelActorId: "slack:U456", channelTarget: "C789" };
  await assert.rejects(createCronForIdentity(identity, ["telegram", ""]), /channel must match the current conversation/);
  await assert.rejects(createCronForIdentity(identity, [" ", ""]), /channel must match the current conversation/);
  // validateUserId refuses another owner before create.js resolves delivery.
  await assert.rejects(createCronForIdentity({ ...identity, actorId: "telegram:999" }, ["", ""]),
    /does not match the user this session belongs to/);
  await assert.rejects(createCronForIdentity({ ...identity, channelActorId: "telegram:123" }, ["", ""]), /delivery identity is unavailable/);
});

test("Cron optional empty target requires a valid conversation target", async () => {
  const identity = { actorId: "telegram:123", channel: "slack", channelActorId: "slack:U456" };
  for (const channelTarget of [undefined, null, "", false, 0, 123]) {
    await assert.rejects(createCronForIdentity({ ...identity, channelTarget }, ["", ""]), /delivery target is unavailable/);
  }
  for (const channelTarget of [false, 0]) {
    await assert.rejects(createCronForIdentity({ ...identity, channelTarget: "C789" }, ["slack", channelTarget]), /delivery target is unavailable/);
  }
});

for (const channelTarget of [{ channelId: "C789" }, true, 123, ""]) {
  test(`rejects a non-string or empty delivery target before activation: ${JSON.stringify(channelTarget)}`, async (t) => {
    const app = loadContract(t);
    const reply = await app.request({
      ...owner, channel: "slack", channelActorId: "slack:U456", channelTarget,
      action: "chat", message: "hello",
    });
    assert.equal(reply.code, 400);
    assert.equal(app.activations.length, 0);
    assert.ok(!app.calls.includes("secrets"));
    assert.ok(!app.calls.includes("chat.send"));
  });
}


// --- Per-user /model override (issue #18) ---
const OVERRIDE_IDENTITY_FILE = "/tmp/current-identity.json";
const OPUS_OVERRIDE = "global.anthropic.claude-opus-5-5";

test("a payload model override reaches the proxy identity file for that turn only", async (t) => {
  const allowlist = "global.anthropic.claude-haiku-5-5,global.anthropic.claude-sonnet-5-5," + OPUS_OVERRIDE;
  const app = loadContract(t, { runtimeEnv: { BEDROCK_USER_SELECTABLE_MODEL_IDS: allowlist } });
  const chosen = await app.request({ ...owner, action: "chat", message: "think hard", modelOverride: OPUS_OVERRIDE });
  assert.equal(chosen.body.execution.phase, "completed", app.errors.join("\n"));
  assert.equal(JSON.parse(app.files.get(OVERRIDE_IDENTITY_FILE)).modelOverride, OPUS_OVERRIDE);
  const proxyChild = app.children.find((child) => child.kind === "node");
  assert.equal(proxyChild.env.BEDROCK_USER_SELECTABLE_MODEL_IDS, allowlist);

  const plain = await app.request({ ...owner, action: "chat", message: "next" });
  assert.equal(plain.body.execution.phase, "completed");
  assert.deepEqual(JSON.parse(app.files.get(OVERRIDE_IDENTITY_FILE)),
    { actorId: owner.actorId, channel: owner.channel, channelActorId: null, channelTarget: null,
      trigger: "chat", sessionId: "session-1" });

  const cron = await app.request({ ...owner, action: "cron", message: "scheduled" });
  assert.equal(cron.body.execution.phase, "completed");
  assert.equal(Object.hasOwn(JSON.parse(app.files.get(OVERRIDE_IDENTITY_FILE)), "modelOverride"), false);
  // The proxy attributes the cron turn's Bedrock calls from this (request-metadata.js).
  assert.equal(JSON.parse(app.files.get(OVERRIDE_IDENTITY_FILE)).trigger, "cron");

  const next = await app.request({ ...owner, action: "chat", message: "after cron" });
  assert.equal(next.body.execution.phase, "completed");
  assert.equal(JSON.parse(app.files.get(OVERRIDE_IDENTITY_FILE)).trigger, "chat");
});

test("a recycled container applies the override on its first turn", async (t) => {
  const app = loadContract(t);
  assert.equal(app.files.has(OVERRIDE_IDENTITY_FILE), false);
  const first = await app.request({ ...owner, action: "chat", message: "hello again", modelOverride: OPUS_OVERRIDE });
  assert.equal(first.body.execution.phase, "completed", app.errors.join("\n"));
  assert.equal(JSON.parse(app.files.get(OVERRIDE_IDENTITY_FILE)).modelOverride, OPUS_OVERRIDE);
  assert.equal(app.children.find((child) => child.kind === "node").env.BEDROCK_USER_SELECTABLE_MODEL_IDS, "",
    "an unset allowlist is forwarded empty");
});

test("a malformed payload model override is ignored without failing the turn", async (t) => {
  const app = loadContract(t);
  for (const modelOverride of [42, "", "../../etc/passwd", "Global.Anthropic", { id: OPUS_OVERRIDE }, "x".repeat(200)]) {
    const reply = await app.request({ ...owner, action: "chat", message: "hi", modelOverride });
    assert.equal(reply.body.execution.phase, "completed", JSON.stringify(modelOverride));
    assert.equal(Object.hasOwn(JSON.parse(app.files.get(OVERRIDE_IDENTITY_FILE)), "modelOverride"), false);
  }
});

// --- Cron timing: the EN and CN briefs share one session, so CN queues behind EN. ---

async function until(predicate, label, limitMs = 2000) {
  const stop = Date.now() + limitMs;
  while (!predicate()) {
    if (Date.now() > stop) throw new Error(`Timed out waiting for ${label}`);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

function blockingTurns(t) {
  const turns = [];
  // Release anything still blocked so a failing assertion cannot hang the suite.
  t.after(() => { for (const turn of turns) turn.done.resolve({ phase: "completed", response: "released" }); });
  return {
    turns,
    gatewayTurnImpl: async (args) => {
      const turn = { args, sentAt: Date.now(), done: deferred() };
      turns.push(turn);
      return turn.done.promise;
    },
  };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test("a warmup while a cron turn runs reports ready without queueing behind the turn", async (t) => {
  t.mock.method(console, "log", () => {});
  const gateway = blockingTurns(t);
  const app = loadContract(t, { gatewayTurnImpl: gateway.gatewayTurnImpl });
  const en = app.request({ ...owner, action: "cron", message: "EN brief", callerBudgetMs: 800000 });
  await until(() => gateway.turns.length === 1, "the EN turn");
  const warmup = app.request({ ...owner, action: "warmup" });
  const raced = await Promise.race([warmup, sleep(300).then(() => "queued")]);
  assert.notEqual(raced, "queued", "warmup waited for the running cron turn");
  assert.equal(raced.body.status, "ready");
  gateway.turns[0].done.resolve({ phase: "completed", response: "EN done" });
  assert.equal((await en).body.response, "EN done");
  assert.equal(app.calls.filter((entry) => entry === "chat.send").length, 1);
});

test("a queued cron turn's deadline runs from dispatch, capped by the caller's wait budget", async (t) => {
  t.mock.method(console, "log", () => {});
  const gateway = blockingTurns(t);
  const app = loadContract(t, { gatewayTurnImpl: gateway.gatewayTurnImpl });
  const en = app.request({ ...owner, action: "cron", message: "EN brief", callerBudgetMs: 900000 });
  await until(() => gateway.turns.length === 1, "the EN turn");
  const cnArrival = Date.now();
  const cn = app.request({ ...owner, action: "cron", message: "CN brief", callerBudgetMs: 900000 });
  await sleep(400); // CN waits in the queue behind EN.
  gateway.turns[0].done.resolve({ phase: "completed", response: "EN done" });
  await until(() => gateway.turns.length === 2, "the CN turn");
  const cnTurn = gateway.turns[1];
  // A full 620 s turn from when CN is actually sent; the queue wait is not charged to it.
  assert.ok(cnTurn.args.deadline >= cnTurn.sentAt + 620000 - 50,
    `CN lost ${cnTurn.sentAt + 620000 - cnTurn.args.deadline} ms of its turn to the queue`);
  // Still ends before the caller stops listening, with room to reply.
  assert.ok(cnTurn.args.deadline <= cnArrival + 900000 - 20000 + 50);
  cnTurn.done.resolve({ phase: "completed", response: "CN done" });
  assert.equal((await en).body.response, "EN done");
  assert.equal((await cn).body.response, "CN done");
});

test("a cron turn never outlives the caller's wait budget", async (t) => {
  t.mock.method(console, "log", () => {});
  const gateway = blockingTurns(t);
  const app = loadContract(t, { gatewayTurnImpl: gateway.gatewayTurnImpl });
  const arrival = Date.now();
  const cron = app.request({ ...owner, action: "cron", message: "brief", callerBudgetMs: 300000 });
  await until(() => gateway.turns.length === 1, "the cron turn");
  const deadline = gateway.turns[0].args.deadline;
  assert.ok(deadline <= arrival + 300000 - 20000 + 50, `deadline ${deadline - arrival} ms after arrival`);
  assert.ok(deadline >= arrival + 300000 - 20000 - 1000);
  gateway.turns[0].done.resolve({ phase: "completed", response: "done" });
  assert.equal((await cron).body.response, "done");
});

test("a cron turn runs past 620 s up to the caller's wait budget", async (t) => {
  // us-west-2 daily-market-desk: the cron Lambda waited ~815 s, but every run was cut at
  // 620 s with unknown_outcome while the model was still working.
  t.mock.method(console, "log", () => {});
  const gateway = blockingTurns(t);
  const app = loadContract(t, { gatewayTurnImpl: gateway.gatewayTurnImpl });
  const arrival = Date.now();
  const cron = app.request({ ...owner, action: "cron", message: "long brief", callerBudgetMs: 815000 });
  await until(() => gateway.turns.length === 1, "the cron turn");
  const deadline = gateway.turns[0].args.deadline;
  assert.ok(deadline >= arrival + 815000 - 20000 - 1000,
    `the turn was cut ${arrival + 815000 - 20000 - deadline} ms before the caller stops listening`);
  assert.ok(deadline <= Date.now() + 815000 - 20000);
  gateway.turns[0].done.resolve({ phase: "completed", response: "long brief done" });
  assert.equal((await cron).body.response, "long brief done");
});

test("resources are refreshed to outlast a cron turn longer than 620 s", async (t) => {
  t.mock.method(console, "log", () => {});
  const validity = [];
  const app = loadContract(t, {
    managerFactory: () => ({
      ensureFresh: async (options = {}) => { validity.push(options.minValidityMs); },
      getCredentials: () => ({}), stop() {}, beginDrain() {},
    }),
  });
  const result = await app.request({ ...owner, action: "cron", message: "long brief", callerBudgetMs: 815000 });
  assert.equal(result.body.response, "reply");
  const perTurn = validity.filter((ms) => ms !== undefined);
  assert.ok(perTurn.length > 0, "the turn did not ask for credentials covering it");
  assert.ok(Math.max(...perTurn) >= 815000 - 20000 - 1000 + 60000,
    `credentials only cover ${Math.max(...perTurn)} ms`);
});

test("a cron turn whose caller budget ran out in the queue is not dispatched", async (t) => {
  t.mock.method(console, "log", () => {});
  const gateway = blockingTurns(t);
  const app = loadContract(t, { gatewayTurnImpl: gateway.gatewayTurnImpl });
  const en = app.request({ ...owner, action: "cron", message: "EN brief", callerBudgetMs: 900000 });
  await until(() => gateway.turns.length === 1, "the EN turn");
  // 20 s reply margin + 100 ms: the CN caller's budget is gone 100 ms after arrival.
  let cnResult;
  const cn = app.request({ ...owner, action: "cron", message: "CN brief", callerBudgetMs: 20100 })
    .then((result) => { cnResult = result; });
  await sleep(300);
  gateway.turns[0].done.resolve({ phase: "completed", response: "EN done" });
  await until(() => cnResult || gateway.turns.length === 2, "CN to settle");
  assert.equal(gateway.turns.length, 1, "CN must not reach the gateway after its caller gave up");
  await cn;
  assert.equal(cnResult.body.status, "error");
  assert.equal((await en).body.response, "EN done");
  assert.equal((await app.request(null, "GET", "/ping")).code, 200, "a skipped turn does not fail the runtime");
});

test("chat without a caller budget keeps the 620 s limit from arrival", async (t) => {
  t.mock.method(console, "log", () => {});
  const gateway = blockingTurns(t);
  const app = loadContract(t, { gatewayTurnImpl: gateway.gatewayTurnImpl });
  const arrival = Date.now();
  const chat = app.request({ ...owner, action: "chat", message: "hi" });
  await until(() => gateway.turns.length === 1, "the chat turn");
  const deadline = gateway.turns[0].args.deadline;
  assert.ok(deadline >= arrival + 620000 - 1000 && deadline <= Date.now() + 620000);
  gateway.turns[0].done.resolve({ phase: "completed", response: "hello" });
  assert.equal((await chat).body.response, "hello");
});

for (const callerBudgetMs of [0, -1, 1.5, "600000", 900001, null]) {
  test(`rejects an invalid caller wait budget before dispatch: ${JSON.stringify(callerBudgetMs)}`, async (t) => {
    const app = loadContract(t);
    const result = await app.request({ ...owner, action: "cron", message: "brief", callerBudgetMs });
    assert.equal(result.code, 400);
    assert.ok(!app.calls.includes("chat.send"));
  });
}

test("a queued cron turn replies status:error at its caller deadline, while the earlier turn still runs", async (t) => {
  t.mock.method(console, "log", () => {});
  t.mock.method(console, "error", () => {});
  const gateway = blockingTurns(t);
  const app = loadContract(t, { gatewayTurnImpl: gateway.gatewayTurnImpl });
  const en = app.request({ ...owner, action: "cron", message: "EN brief", callerBudgetMs: 900000 });
  await until(() => gateway.turns.length === 1, "the EN turn");
  // 20 s reply margin + 150 ms: the CN caller's queue deadline is 150 ms after arrival.
  const cnArrival = Date.now();
  const cn = app.request({ ...owner, action: "cron", message: "CN brief", callerBudgetMs: 20150 });
  const raced = await Promise.race([cn, sleep(1500).then(() => "still queued")]);
  assert.notEqual(raced, "still queued", "CN got no reply until the EN turn released the queue");
  assert.equal(raced.code, 200);
  assert.equal(raced.body.status, "error");
  assert.notEqual(raced.body.status, "unknown_outcome", "the caller must be able to release its admission");
  assert.ok(Date.now() - cnArrival < 1500);
  const ping = await app.request(null, "GET", "/ping");
  assert.equal(ping.code, 200);
  assert.equal(ping.body.active_tasks, 1, "only the EN turn is still active");
  // EN is unaffected, and CN never reaches the gateway once EN releases the queue.
  gateway.turns[0].done.resolve({ phase: "completed", response: "EN done" });
  assert.equal((await en).body.response, "EN done");
  await sleep(100);
  assert.equal(gateway.turns.length, 1, "CN was dispatched after its caller had its reply");
  assert.equal(app.calls.filter((entry) => entry === "chat.send").length, 1);
  assert.equal((await app.request(null, "GET", "/ping")).body.status, "Healthy");
});
