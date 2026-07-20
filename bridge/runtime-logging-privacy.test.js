const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const loggerModule = require("./cloudwatch-logger");

const SENTINELS = Object.freeze([
  "user-privacy-sentinel",
  "workspace-privacy-sentinel",
  "provider-privacy-sentinel",
  "token-privacy-sentinel",
  "/private/privacy-sentinel/path",
  "stdout-privacy-sentinel",
  "stderr-privacy-sentinel",
  "exception-privacy-sentinel",
  "stack-privacy-sentinel",
]);

function assertNoSentinels(value) {
  const serialized = typeof value === "string" ? value : JSON.stringify(value);
  for (const sentinel of SENTINELS) {
    assert.equal(
      serialized.includes(sentinel),
      false,
      `private sentinel escaped the logging boundary: ${sentinel}`,
    );
  }
}

function createFakeConsole() {
  return {
    log() {},
    info() {},
    warn() {},
    error() {},
    debug() {},
  };
}

test("console hooks and CloudWatch retain only closed structured metadata", async () => {
  const platformStdout = [];
  const platformStderr = [];
  const commands = [];
  const logger = loggerModule.createStructuredLogger({
    platformStdout: (line) => platformStdout.push(line),
    platformStderr: (line) => platformStderr.push(line),
    clientFactory: () => ({
      async send(command) {
        commands.push(command);
      },
    }),
    clock: () => 1_726_000_000_000,
    scheduleFlush: () => ({ unref() {} }),
    cancelFlush: () => {},
  });
  const targetConsole = createFakeConsole();
  logger.installConsoleHooks(targetConsole);

  const exception = new Error(SENTINELS[7]);
  exception.stack = `Error: ${SENTINELS[7]}\n at ${SENTINELS[8]}`;
  const rawPayload = {
    user: SENTINELS[0],
    workspace: SENTINELS[1],
    provider: SENTINELS[2],
    token: SENTINELS[3],
    path: SENTINELS[4],
    stdout: SENTINELS[5],
    stderr: SENTINELS[6],
  };
  for (const method of ["log", "info", "warn", "error", "debug"]) {
    targetConsole[method](rawPayload, exception, exception.message, exception.stack);
  }

  logger.emit("RUNTIME_STATE", {
    level: "INFO",
    status: "READY",
    count: 3,
  });
  logger.emit(SENTINELS[2], rawPayload);
  await logger.init({
    env: { AWS_REGION: "eu-west-1" },
    legacyStreamName: `${SENTINELS[0]}-${SENTINELS[1]}`,
  });
  await logger.flush();

  assert.ok(platformStdout.length > 0);
  assert.ok(platformStderr.length > 0);
  assertNoSentinels(platformStdout);
  assertNoSentinels(platformStderr);
  assertNoSentinels(commands.map((command) => command.input));

  const createStream = commands.find(
    (command) => command.constructor.name === "CreateLogStreamCommand",
  );
  assert.equal(createStream.input.logStreamName, "runtime");

  const put = commands.find(
    (command) => command.constructor.name === "PutLogEventsCommand",
  );
  const records = put.input.logEvents.map(({ message }) => JSON.parse(message));
  assert.ok(
    records.some(
      (record) =>
        record.event === "RUNTIME_STATE" &&
        record.level === "INFO" &&
        record.status === "READY" &&
        record.count === 3,
    ),
  );
  assert.ok(records.some((record) => record.event === "LOG_EVENT_REJECTED"));
  for (const record of records) {
    assert.deepEqual(
      Object.keys(record).sort(),
      Object.keys(record).filter((key) =>
        ["version", "event", "level", "status", "count"].includes(key),
      ).sort(),
    );
  }
});

test("accessor-backed metadata is rejected without invoking stateful getters", async () => {
  const platformStdout = [];
  const platformStderr = [];
  const commands = [];
  const logger = loggerModule.createStructuredLogger({
    platformStdout: (line) => platformStdout.push(line),
    platformStderr: (line) => platformStderr.push(line),
    clientFactory: () => ({
      async send(command) {
        commands.push(command);
      },
    }),
    clock: () => 1_726_000_000_000,
    scheduleFlush: () => ({ unref() {} }),
    cancelFlush: () => {},
  });
  const reads = { level: 0, status: 0, count: 0 };
  const cases = [
    {
      field: "level",
      metadata: { status: "READY", count: 1 },
      value(readCount) {
        return readCount < 100 ? "INFO" : SENTINELS[2];
      },
    },
    {
      field: "status",
      metadata: { level: "INFO", count: 1 },
      value(readCount) {
        return readCount < 4 ? "READY" : SENTINELS[1];
      },
    },
    {
      field: "count",
      metadata: { level: "ERROR", status: "FAILED" },
      value(readCount) {
        return readCount < 6 ? 1 : SENTINELS[0];
      },
    },
  ];

  const records = cases.map(({ field, metadata, value }) => {
    Object.defineProperty(metadata, field, {
      enumerable: true,
      get() {
        reads[field] += 1;
        return value(reads[field]);
      },
    });
    return logger.emit("RUNTIME_STATE", metadata);
  });

  await logger.init({ env: { AWS_REGION: "eu-west-1" } });
  await logger.flush();

  assertNoSentinels(platformStdout);
  assertNoSentinels(platformStderr);
  assertNoSentinels(commands.map((command) => command.input));
  assert.deepEqual(reads, { level: 0, status: 0, count: 0 });
  assert.deepEqual(
    records.map(({ event }) => event),
    ["LOG_EVENT_REJECTED", "LOG_EVENT_REJECTED", "LOG_EVENT_REJECTED"],
  );
});

test("child output is drained as bounded counts and never retained or emitted", () => {
  const platform = [];
  const logger = loggerModule.createStructuredLogger({
    platformStdout: (line) => platform.push(line),
    platformStderr: (line) => platform.push(line),
  });
  const stdout = new EventEmitter();
  const stderr = new EventEmitter();

  logger.drainChildOutput(stdout, "PROXY_STDOUT");
  logger.drainChildOutput(stderr, "OPENCLAW_STDERR");
  stdout.emit("data", Buffer.from(`${SENTINELS[5]} ${SENTINELS[3]}`));
  stderr.emit("data", Buffer.from(`${SENTINELS[6]} ${SENTINELS[7]}`));

  assertNoSentinels(platform);
  assert.deepEqual(logger.childOutputCounts(), {
    PROXY_STDOUT: Buffer.byteLength(`${SENTINELS[5]} ${SENTINELS[3]}`),
    OPENCLAW_STDERR: Buffer.byteLength(`${SENTINELS[6]} ${SENTINELS[7]}`),
  });
});

test("uncaught exceptions and rejections terminate without printing their values", async () => {
  const platform = [];
  const targetProcess = new EventEmitter();
  const exits = [];
  const logger = loggerModule.createStructuredLogger({
    platformStdout: (line) => platform.push(line),
    platformStderr: (line) => platform.push(line),
  });
  logger.installProcessFailureHooks(targetProcess, (code) => exits.push(code));

  const exception = new Error(SENTINELS[7]);
  exception.stack = `Error: ${SENTINELS[7]}\n at ${SENTINELS[8]}`;
  targetProcess.emit("uncaughtException", exception);
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(exits, [1]);
  assertNoSentinels(platform);
  assert.ok(
    platform.some((line) => {
      const record = JSON.parse(line);
      return record.event === "RUNTIME_STATE" && record.status === "FAILED";
    }),
  );
});

test("runtime entrypoints install privacy hooks before active logs", () => {
  const contract = fs.readFileSync(
    path.join(__dirname, "agentcore-contract.js"),
    "utf8",
  );
  const proxy = fs.readFileSync(
    path.join(__dirname, "agentcore-proxy.js"),
    "utf8",
  );
  const entrypoint = fs.readFileSync(
    path.join(__dirname, "entrypoint.sh"),
    "utf8",
  );

  const contractHook = contract.indexOf("installConsoleHooks");
  const contractFailureHook = contract.indexOf("installProcessFailureHooks");
  const contractActiveImport = contract.indexOf('require("./workspace-sync")');
  assert.ok(contractHook >= 0 && contractHook < contractActiveImport);
  assert.ok(
    contractFailureHook >= 0 && contractFailureHook < contractActiveImport,
  );

  const proxyHook = proxy.indexOf("installConsoleHooks");
  const proxyFailureHook = proxy.indexOf("installProcessFailureHooks");
  const proxyActiveImport = proxy.indexOf('require("./session-binding")');
  assert.ok(proxyHook >= 0 && proxyHook < proxyActiveImport);
  assert.ok(proxyFailureHook >= 0 && proxyFailureHook < proxyActiveImport);

  assert.doesNotMatch(entrypoint, /echo\s+.*(?:\$\{|\$\()/u);
  assert.doesNotMatch(entrypoint, /echo\s+.*AWS_REGION/u);
});

test("provider failures and guardrail traces are never reflected into durable text", () => {
  const proxy = fs.readFileSync(
    path.join(__dirname, "agentcore-proxy.js"),
    "utf8",
  );

  assert.doesNotMatch(proxy, /lastIdentityDiag/u);
  assert.doesNotMatch(proxy, /JSON\.stringify\([^\n]*trace\.guardrail/u);
  assert.doesNotMatch(proxy, /(?:Invocation|streaming) failed:[^\n]*err(?:or)?\.message/u);
  assert.match(proxy, /message: "Provider invocation failed"/u);
});

test("contract never retains or re-emits raw child output", () => {
  const contract = fs.readFileSync(
    path.join(__dirname, "agentcore-contract.js"),
    "utf8",
  );

  assert.doesNotMatch(contract, /openclawLogs/u);
  assert.doesNotMatch(contract, /\[proxy:(?:out|err)\]/u);
  assert.doesNotMatch(contract, /\[openclaw:\$\{label\}\]/u);
  assert.match(contract, /drainChildOutput\(proxyProcess\.stdout, "PROXY_STDOUT"\)/u);
  assert.match(contract, /drainChildOutput\(proxyProcess\.stderr, "PROXY_STDERR"\)/u);
  assert.match(contract, /drainChildOutput\(openclawProcess\.stdout, "OPENCLAW_STDOUT"\)/u);
  assert.match(contract, /drainChildOutput\(openclawProcess\.stderr, "OPENCLAW_STDERR"\)/u);
});
