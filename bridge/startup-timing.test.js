const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { execFileSync } = require("node:child_process");
const { createStartupTiming, MAX_PENDING_REQUESTS } = require("./startup-timing");

const entrypointTime = 1710000000007;
const env = Object.freeze({
  OPENCLAW_ENTRYPOINT_WALL_MS: String(entrypointTime),
  OPENCLAW_ENTRYPOINT_EXECUTION_ID: `entrypoint-${entrypointTime}-123-456`,
});
const attempt = (n = 1) => `startup-${n.toString(16).padStart(32, "0")}`;
const source = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf8");

function timing(options = {}) {
  const records = [];
  let wall = entrypointTime + 100;
  const telemetry = createStartupTiming({
    env, wallNow: () => wall, monotonicNow: () => 123456789n,
    emit: (record) => records.push(record), ...options,
  });
  return {
    ...telemetry, records,
    at(value) { wall = value; },
    capture: () => telemetry.captureHttpHandler("POST", "/invocations"),
  };
}

test("replay retains producer clocks and is enabled only once", () => {
  const app = timing();
  const arrival = app.capture();
  app.at(entrypointTime + 30 * 24 * 3600 * 1000);
  app.recordHttpHandler(arrival, attempt());
  assert.equal(app.records.length, 2);
  assert.equal(app.records[0].wall_unix_ms, entrypointTime + 100);
  app.enableScopedLogging();
  assert.deepEqual(app.records[2], {
    schema_version: 1, event: "ENTRYPOINT_EXEC", wall_unix_ms: entrypointTime,
    entrypoint_execution_id: env.OPENCLAW_ENTRYPOINT_EXECUTION_ID, delivery: "replay",
  });
  assert.deepEqual(app.records[3], { ...app.records[1], delivery: "replay" });
  assert.equal(app.records[3].monotonic_ns, "123456789");
  assert.equal(app.records[3].request_id, app.records[0].request_id);
  assert.equal(app.records.filter((record) => record.event === "PRE_ENTRYPOINT").length, 1);
  app.enableScopedLogging();
  assert.equal(app.records.length, 4);
  app.recordHttpHandler(app.capture(), attempt(2));
  assert.equal(app.records.at(-1).delivery, "producer");
  app.enableScopedLogging();
  assert.equal(app.records.length, 6);
});

test("capture emits distinct request IDs within a process with identical clocks and lineage", () => {
  const apps = [timing(), timing()];
  const ids = new Set();
  for (let i = 0; i < 64; i++) {
    const app = apps[i % apps.length];
    const arrival = app.capture();
    assert.ok(Object.isFrozen(arrival));
    assert.match(arrival.request_id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    assert.deepEqual(app.records.at(-1), {
      schema_version: 1, event: "PRE_ENTRYPOINT", marker: "[PRE-ENTRYPOINT]",
      wall_unix_ms: entrypointTime + 100, monotonic_ns: "123456789",
      request_id: arrival.request_id, route_kind: "invocations",
      entrypoint_execution_id: env.OPENCLAW_ENTRYPOINT_EXECUTION_ID, delivery: "producer",
    });
    ids.add(arrival.request_id);
  }
  assert.equal(ids.size, 64);
});

test("request IDs delegate to OS entropy only when a handler captures an invocation", () => {
  const calls = [];
  const records = [];
  const context = vm.createContext({
    module: { exports: {} },
    require(name) {
      assert.equal(name, "./runtime-lifecycle");
      return { osRandomUUID(...args) { calls.push(args); return `test-request-${calls.length}`; } };
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "startup-timing.js"), "utf8"), context);
  assert.equal(calls.length, 0, "import must not consume entropy before snapshot restore");
  const app = context.module.exports.createStartupTiming({
    env: {}, wallNow: () => entrypointTime, monotonicNow: () => 1n, emit: (record) => records.push(record),
  });
  assert.equal(calls.length, 0, "creating telemetry must not pre-generate request IDs");
  assert.equal(app.captureHttpHandler("GET", "/ping"), null);
  assert.equal(app.captureHttpHandler("POST", "/other"), null);
  assert.equal(calls.length, 0, "unmatched routes must not consume entropy");
  for (let i = 1; i <= 2; i++) {
    const arrival = app.captureHttpHandler("POST", "/invocations");
    assert.equal(calls.length, i, "each request must call the OS helper once");
    assert.equal(arrival.request_id, `test-request-${i}`);
    assert.equal(records.at(-1).event, "PRE_ENTRYPOINT");
    assert.equal(records.at(-1).request_id, arrival.request_id);
    app.recordHttpHandler(arrival, attempt(i));
  }
  app.enableScopedLogging();
  assert.deepEqual(calls, [[], []], "correlation and replay must reuse the captured IDs without generating more");
});

test("pending replay keeps entrypoint and only the most recent bounded requests", () => {
  const app = timing();
  const count = MAX_PENDING_REQUESTS * 4;
  for (let i = 0; i < count; i++) app.recordHttpHandler(app.capture(), attempt(i));
  assert.equal(app.records.length, count * 2, "each matching request emits a marker and correlation");
  app.enableScopedLogging();
  const replay = app.records.filter((record) => record.delivery === "replay");
  assert.equal(replay.length, MAX_PENDING_REQUESTS + 1);
  assert.equal(replay[0].event, "ENTRYPOINT_EXEC");
  assert.deepEqual(replay.slice(1).map((record) => record.measurement_attempt_id),
    Array.from({ length: MAX_PENDING_REQUESTS }, (_, i) => attempt(count - MAX_PENDING_REQUESTS + i)));
});

test("strict synthetic IDs cannot leak arbitrary values or coerce objects", () => {
  const app = timing({ env: {} });
  const invalid = [
    undefined, null, 1, {}, [], { toString() { throw new Error("must not coerce"); } },
    "", "startup-short", attempt().toUpperCase(), `${attempt()}\n`,
    `${attempt()}-token`, ` ${attempt()}`, "startup-" + "a".repeat(100000),
    "startup-" + "g".repeat(32), "startup-" + "０".repeat(32),
  ];
  for (const value of invalid) app.recordHttpHandler(app.capture(), value);
  app.enableScopedLogging();
  assert.equal(app.records.length, invalid.length);
  assert.ok(app.records.every((record) => record.event === "PRE_ENTRYPOINT" &&
    record.delivery === "producer" && !("measurement_attempt_id" in record)));
  app.recordHttpHandler(app.capture(), attempt());
  assert.deepEqual(Object.keys(app.records.at(-1)).sort(), [
    "delivery", "entrypoint_execution_id", "event", "measurement_attempt_id",
    "monotonic_ns", "request_id", "route_kind", "schema_version", "wall_unix_ms",
  ]);
  assert.equal(app.records[0].entrypoint_execution_id, null);
});

test("missing or malformed handoff never replays environment contents", () => {
  const invalid = [
    {}, { ...env, OPENCLAW_ENTRYPOINT_WALL_MS: "secret" },
    { ...env, OPENCLAW_ENTRYPOINT_WALL_MS: String(entrypointTime + 1) },
    { ...env, OPENCLAW_ENTRYPOINT_EXECUTION_ID: "secret-token" },
    { ...env, OPENCLAW_ENTRYPOINT_EXECUTION_ID: `${env.OPENCLAW_ENTRYPOINT_EXECUTION_ID}\n` },
  ];
  for (const seed of invalid) {
    const app = timing({ env: { ...seed, AWS_SECRET_ACCESS_KEY: "never-log" } });
    app.enableScopedLogging();
    assert.deepEqual(app.records, []);
    app.recordHttpHandler(app.capture(), attempt());
    assert.equal(app.records[0].entrypoint_execution_id, null);
    assert.doesNotMatch(JSON.stringify(app.records), /secret|never-log/);
  }
});

test("copied snapshot handoff retains lineage without claiming a new VM identity", () => {
  const first = timing({ env: { ...env } });
  const second = timing({ env: { ...env } });
  first.enableScopedLogging();
  second.enableScopedLogging();
  assert.deepEqual(first.records, second.records);
  first.recordHttpHandler(first.capture(), attempt(1));
  second.recordHttpHandler(second.capture(), attempt(2));
  assert.notEqual(first.records[2].measurement_attempt_id, second.records[2].measurement_attempt_id);
});

test("clock or logging failures do not escape telemetry", () => {
  for (const clocks of [
    { wallNow() { throw new Error("clock"); } },
    { wallNow: () => NaN },
    { monotonicNow() { throw new Error("clock"); } },
    { monotonicNow: () => -1n },
  ]) {
    const app = timing(clocks);
    assert.equal(app.capture(), null);
    app.recordHttpHandler(app.capture(), attempt());
    assert.deepEqual(app.records, []);
  }
  const app = timing({ emit() { throw new Error("output unavailable"); } });
  assert.doesNotThrow(() => {
    app.recordHttpHandler(app.capture(), attempt());
    app.enableScopedLogging();
  });
});

function contractHandler(telemetry) {
  let handler;
  let releaseBody;
  const calls = [];
  const forwarded = [];
  const body = new Promise((resolve) => { releaseBody = resolve; });
  const context = vm.createContext({
    startupTiming: telemetry,
    http: { createServer(callback) { handler = callback; return {}; } },
    readBody(req, limit) {
      calls.push("readBody");
      assert.equal(limit, 1048576);
      telemetry.at(entrypointTime + 5000);
      return body;
    },
    trackCallerDisconnect() {
      assert.equal(telemetry.records.at(-1).event, "PRE_ENTRYPOINT");
      calls.push("disconnect");
      telemetry.at(entrypointTime + 2000);
      return () => false;
    },
    async handleV2Invocation(payload, req, res) {
      calls.push("dispatch");
      forwarded.push({ payload, req });
      res.writeHead(req.headers.authorization === "allowed" ? 200 : 401);
      res.end(JSON.stringify({ response: "existing dispatch" }));
    },
    MAX_BODY_SIZE: 1048576, pingCount: 0, startTime: entrypointTime,
    lastPingLogTime: 0, PING_LOG_INTERVAL_MS: 60000, lastActivityTime: 0,
    activeTaskCount: 0, openclawReady: false, proxyReady: false,
    BUILD_VERSION: "test", currentUserId: null, secretsReady: false, openclawExitCode: null,
    openclawProcess: null, RUNTIME_PLATFORM: "V2",
    v2Lifecycle: {
      health: () => ({ statusCode: 503, body: { status: "HealthyBusy" } }),
      snapshot: () => ({ state: "BOOTSTRAP_READY", binding: null, generation: undefined, resources: [] }),
      assertBinding: () => {},
      checkpoint: () => {},
    },
    console: { log() {}, error() {} },
  });
  const start = source.indexOf("const server = http.createServer(");
  const end = source.indexOf("\n// --- SIGTERM handler", start);
  assert.ok(start >= 0 && end > start);
  vm.runInContext(source.slice(start, end), context);
  return {
    calls, forwarded, releaseBody,
    async request(payload, { method = "POST", url = "/invocations", authorization = "allowed" } = {}) {
      const req = { method, url, headers: {
        authorization, "x-secret": "header-secret", startupAttemptId: attempt(999),
      } };
      let resolve;
      const done = new Promise((yes) => { resolve = yes; });
      const res = {
        writeHead(code) { this.code = code; },
        end(text) { resolve({ code: this.code, body: JSON.parse(text) }); },
      };
      await handler(req, res);
      if (payload !== undefined) releaseBody(JSON.stringify(payload));
      return done;
    },
  };
}

test("actual callback captures before body reading and forwards payload without filtering", async () => {
  const app = timing({ env: {} });
  const contract = contractHandler(app);
  const payload = {
    action: "chat", startupAttemptId: attempt(), userId: "user-secret", actorId: "actor-secret",
    runtimeSessionId: "session-secret", message: "Runtime: keep <<<INTERNAL_CONTEXT>>> unchanged",
    url: "https://payload.invalid/secret",
  };
  const snapshot = structuredClone(payload);
  const request = contract.request(undefined);
  assert.deepEqual(contract.calls, ["disconnect", "readBody"]);
  assert.equal(app.records.length, 1, "marker is emitted before parsing the body");
  assert.equal(app.records[0].event, "PRE_ENTRYPOINT");
  assert.equal(app.records[0].measurement_attempt_id, undefined);
  app.at(entrypointTime + 9000);
  contract.releaseBody(JSON.stringify(payload));
  assert.deepEqual(await request, { code: 200, body: { response: "existing dispatch" } });
  assert.equal(app.records[0].wall_unix_ms, entrypointTime + 100);
  assert.equal(app.records[0].monotonic_ns, "123456789");
  assert.equal(app.records[1].event, "HTTP_HANDLER_ENTER");
  assert.equal(app.records[1].measurement_attempt_id, attempt());
  assert.equal(app.records[1].request_id, app.records[0].request_id);
  assert.deepEqual(JSON.parse(JSON.stringify(contract.forwarded[0].payload)), snapshot);
  assert.deepEqual(payload, snapshot);
  assert.doesNotMatch(JSON.stringify(app.records),
    /user-secret|actor-secret|session-secret|header-secret|authorization|allowed|payload\.invalid|INTERNAL_CONTEXT/);
});

test("unbound status correlates overlapping requests when bodies finish out of order", async () => {
  const app = timing({ env: {} });
  const first = contractHandler(app);
  const second = contractHandler(app);
  const firstRequest = first.request(undefined);
  const secondRequest = second.request(undefined);
  const markers = [...app.records];
  assert.equal(markers.length, 2);
  assert.notEqual(markers[0].request_id, markers[1].request_id);
  second.releaseBody(JSON.stringify({ action: "status", startupAttemptId: attempt(2) }));
  const secondResponse = await secondRequest;
  first.releaseBody(JSON.stringify({ action: "status", startupAttemptId: attempt(1) }));
  const firstResponse = await firstRequest;
  for (const response of [firstResponse, secondResponse]) {
    assert.equal(response.code, 200);
    const diag = JSON.parse(response.body.response);
    assert.equal("currentUserId" in diag, false);
    assert.equal("openclawLogs" in diag, false);
    assert.equal(diag.openclawReady, false);
    assert.equal(diag.activeTaskCount, 0);
    assert.equal(diag.lifecycle.state, "BOOTSTRAP_READY");
    assert.equal(diag.lifecycle.bound, false);
  }
  for (const contract of [first, second]) {
    assert.deepEqual(contract.calls, ["disconnect", "readBody"]);
    assert.deepEqual(contract.forwarded, []);
  }
  for (const [index, marker] of markers.entries()) {
    const record = app.records.find((item) => item.measurement_attempt_id === attempt(index + 1));
    assert.equal(record.request_id, marker.request_id);
    assert.equal(record.wall_unix_ms, marker.wall_unix_ms);
    assert.equal(record.monotonic_ns, marker.monotonic_ns);
  }
});

test("malformed bodies still emit only the immediate marker", async () => {
  const app = timing({ env: {} });
  const contract = contractHandler(app);
  const request = contract.request(undefined);
  contract.releaseBody("{");
  assert.equal((await request).code, 200);
  assert.equal(app.records.length, 1);
  assert.equal(app.records[0].event, "PRE_ENTRYPOINT");
  assert.deepEqual(contract.forwarded, []);
});

test("ping does not consume measurement and normal requests retain dispatch and rejection", async () => {
  const app = timing({ env: {} });
  const contract = contractHandler(app);
  assert.equal((await contract.request(undefined, { method: "GET", url: "/ping" })).code, 503);
  assert.deepEqual(contract.calls, []);
  assert.deepEqual(app.records, []);
  for (const [i, authorization] of ["allowed", "denied"].entries()) {
    const next = contractHandler(app);
    const payload = { action: "chat", message: "Runtime: user text", startupAttemptId: attempt(i) };
    assert.equal((await next.request(payload, { authorization })).code, i === 0 ? 200 : 401);
  }
  assert.equal(app.records.length, 4, "both attempts emit a marker and correlation after ping");
  const before = app.records.filter((record) => record.event === "HTTP_HANDLER_ENTER");
  const ordinary = contractHandler(app);
  assert.equal((await ordinary.request({ action: "chat", message: "unchanged" })).code, 200);
  const invalid = contractHandler(app);
  assert.equal((await invalid.request({ action: "chat", startupAttemptId: "private", message: "unchanged" },
    { authorization: "denied" })).code, 401);
  assert.deepEqual(app.records.filter((record) => record.event === "HTTP_HANDLER_ENTER"),
    before, "headers do not supply a measurement attempt");
  assert.equal(app.records.length, 6, "ordinary and invalid attempts only emit markers");
  assert.equal((await contract.request(undefined, {
    method: "POST", url: `/invocations?startupAttemptId=${attempt(99)}`,
  })).code, 404);
  assert.equal(app.records.length, 6);
});

function loggerFixture({ region = "test-region", failStream = false } = {}) {
  const records = [];
  const batches = [];
  const now = entrypointTime + 30 * 24 * 3600 * 1000;
  const output = { log(line) { records.push(JSON.parse(line)); }, warn() {}, error() {} };
  class Command { constructor(input) { this.input = input; } }
  class CreateLogGroupCommand extends Command {}
  class CreateLogStreamCommand extends Command {}
  class PutLogEventsCommand extends Command {}
  class CloudWatchLogsClient {
    async send(command) {
      if (command instanceof CreateLogStreamCommand && failStream) throw new Error("unavailable");
      if (command instanceof PutLogEventsCommand) batches.push(command.input.logEvents);
      return {};
    }
  }
  const context = vm.createContext({
    module: { exports: {} }, process: { env: { AWS_REGION: region } }, console: output,
    Date: { now: () => now }, setInterval: () => ({ unref() {} }), clearInterval() {},
    AbortController, setTimeout, clearTimeout,
    require(name) {
      assert.equal(name, "@aws-sdk/client-cloudwatch-logs");
      return { CloudWatchLogsClient, CreateLogGroupCommand, CreateLogStreamCommand, PutLogEventsCommand };
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "cloudwatch-logger.js"), "utf8"), context);
  const cwLogger = context.module.exports;
  const telemetry = timing({ emit: (record) => output.log(JSON.stringify(record)) });
  Object.assign(context, {
    cwLogger, startupTiming: telemetry, checkpoint() {}, userId: "test-user", actorId: "test:actor",
    v2Lifecycle: { snapshot: () => ({ binding: { userId: "test-user", actorId: "test:actor" } }) },
    activationScope: { generation: "test-generation", signal: new AbortController().signal },
  });
  const start = source.indexOf('    const namespace = actorId.replace(/:/g, "_");');
  const end = source.indexOf("\n    // Expose USER_ID", start);
  assert.ok(start >= 0 && end > start);
  const initialize = vm.compileFunction(
    `return (async () => {${source.slice(start, end)}\nreturn scopedLogger.ready;})();`, [],
    { parsingContext: context });
  return { telemetry, initialize, cwLogger, records, batches, now };
}

test("actual scoped logger queues old producer times with current CloudWatch timestamps", async () => {
  const logger = loggerFixture();
  logger.telemetry.recordHttpHandler(logger.telemetry.capture(), attempt());
  await logger.cwLogger.flush();
  assert.deepEqual(logger.batches, []);
  assert.equal(await logger.initialize(), true);
  await logger.cwLogger.flush();
  assert.equal(logger.batches.length, 1);
  const events = logger.batches[0];
  assert.equal(events.length, 2);
  assert.ok(events.every((event) => event.timestamp === logger.now));
  const replay = events.map((event) => JSON.parse(event.message.slice("[INFO] ".length)));
  assert.equal(replay[0].wall_unix_ms, entrypointTime);
  assert.equal(replay[1].wall_unix_ms, entrypointTime + 100);
  assert.ok(replay.every((record) => record.delivery === "replay"));
  assert.equal(logger.records.length, 4);
});

test("no-op logger skips replay and failed remote initialization publishes no batch", async () => {
  for (const options of [{ region: "" }, { failStream: true }]) {
    const logger = loggerFixture(options);
    logger.telemetry.recordHttpHandler(logger.telemetry.capture(), attempt());
    assert.equal(await logger.initialize(), false);
    await logger.cwLogger.flush();
    assert.deepEqual(logger.batches, []);
    assert.equal(logger.records.length, options.failStream ? 4 : 2);
    assert.equal(logger.records[0].delivery, "producer");
    if (options.failStream) assert.ok(logger.records.slice(2).every((r) => r.delivery === "replay"));
  }
});

test("entrypoint metadata prefix uses only builtins and passes matching validated metadata", () => {
  const shell = fs.readFileSync(path.join(__dirname, "entrypoint.sh"), "utf8");
  const end = shell.indexOf("# Start the contract server;");
  assert.ok(end > 0);
  const prefix = shell.slice(0, end);
  assert.doesNotMatch(prefix, /\$\((?!\()|\bexec\b|\bnode\b/);
  const deterministic = prefix.replace("entrypoint_epoch_realtime=${EPOCHREALTIME:-}",
    "entrypoint_epoch_realtime=1710000000.007999");
  const result = execFileSync("/bin/bash", ["--noprofile", "--norc", "-c",
    `${deterministic}\nprintf '%s\\n%s\\n' "$OPENCLAW_ENTRYPOINT_WALL_MS" "$OPENCLAW_ENTRYPOINT_EXECUTION_ID"`],
  { encoding: "utf8", env: {} }).trim().split("\n");
  const produced = JSON.parse(result[0]);
  assert.equal(produced.wall_unix_ms, entrypointTime);
  const app = timing({ env: {
    OPENCLAW_ENTRYPOINT_WALL_MS: result[1], OPENCLAW_ENTRYPOINT_EXECUTION_ID: result[2],
  } });
  app.enableScopedLogging();
  assert.deepEqual(app.records, [{ ...produced, delivery: "replay" }]);
  const missing = prefix.replace("entrypoint_epoch_realtime=${EPOCHREALTIME:-}", "entrypoint_epoch_realtime=");
  assert.equal(execFileSync("/bin/bash", ["--noprofile", "--norc", "-c",
    `${missing}\nprintf '%s%s' "\${OPENCLAW_ENTRYPOINT_WALL_MS-}" "\${OPENCLAW_ENTRYPOINT_EXECUTION_ID-}"`],
  { encoding: "utf8", env: { ...env } }), "");
});

test("the source Dockerfile includes the helper and verifies the Bash clock", () => {
  const dockerfile = fs.readFileSync(path.join(__dirname, "Dockerfile"), "utf8");
  assert.match(dockerfile, /^COPY startup-timing\.js \/app\/startup-timing\.js$/m);
  assert.match(dockerfile, /^RUN \/bin\/bash -c .*EPOCHREALTIME/m);
});
