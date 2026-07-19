/**
 * Closed runtime logger.
 *
 * Runtime code and child processes may handle user content, credentials, file
 * paths, provider diagnostics, and exception traces. None of those values are
 * log data. This module replaces every console method with a sink that ignores
 * its arguments and emits a small allowlisted record instead. Callers that need
 * an operational signal must use emit() with one of the fixed event codes.
 */

const LOG_GROUP = "/openclaw/container";
const LOG_STREAM = "runtime";
const FLUSH_INTERVAL_MS = 5_000;
const MAX_BATCH_SIZE = 100;
const MAX_BUFFER_SIZE = 1_000;
const MAX_COUNT = 1_000_000_000;

const EVENT_CODES = new Set([
  "LEGACY_CONSOLE_CALL",
  "LOG_EVENT_REJECTED",
  "RUNTIME_STATE",
]);
const LEVELS = new Set(["DEBUG", "INFO", "WARN", "ERROR"]);
const STATUSES = new Set([
  "DENIED",
  "FAILED",
  "INITIALIZING",
  "OK",
  "QUARANTINED",
  "READY",
  "RETRYABLE",
  "RUNNING",
  "STOPPED",
  "UNCERTAIN",
]);
const CHILD_OUTPUT_CHANNELS = new Set([
  "OPENCLAW_STDERR",
  "OPENCLAW_STDOUT",
  "PROXY_STDERR",
  "PROXY_STDOUT",
]);
const CONSOLE_HOOK = Symbol("personalOperatorSafeConsoleHook");
const PROCESS_FAILURE_HOOK = Symbol("personalOperatorSafeProcessFailureHook");

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function rejectedRecord() {
  return Object.freeze({
    version: 1,
    event: "LOG_EVENT_REJECTED",
    level: "WARN",
  });
}

function sanitizeRecord(eventCode, metadata = {}) {
  if (!EVENT_CODES.has(eventCode) || !isPlainObject(metadata)) {
    return rejectedRecord();
  }
  const keys = Object.keys(metadata);
  if (keys.some((key) => !["level", "status", "count"].includes(key))) {
    return rejectedRecord();
  }

  const level = metadata.level === undefined ? "INFO" : metadata.level;
  if (!LEVELS.has(level)) return rejectedRecord();
  if (metadata.status !== undefined && !STATUSES.has(metadata.status)) {
    return rejectedRecord();
  }
  if (
    metadata.count !== undefined &&
    (!Number.isSafeInteger(metadata.count) ||
      metadata.count < 0 ||
      metadata.count > MAX_COUNT)
  ) {
    return rejectedRecord();
  }

  return Object.freeze({
    version: 1,
    event: eventCode,
    level,
    ...(metadata.status === undefined ? {} : { status: metadata.status }),
    ...(metadata.count === undefined ? {} : { count: metadata.count }),
  });
}

function defaultPlatformWriter(stream) {
  return (line) => {
    try {
      stream.write(`${line}\n`);
    } catch {
      // Logging is never allowed to affect runtime behavior.
    }
  };
}

function createStructuredLogger({
  platformStdout = defaultPlatformWriter(process.stdout),
  platformStderr = defaultPlatformWriter(process.stderr),
  clientFactory,
  clock = () => Date.now(),
  scheduleFlush = (callback) => setInterval(callback, FLUSH_INTERVAL_MS),
  cancelFlush = (timer) => clearInterval(timer),
} = {}) {
  if (
    typeof platformStdout !== "function" ||
    typeof platformStderr !== "function" ||
    (clientFactory !== undefined && typeof clientFactory !== "function") ||
    typeof clock !== "function" ||
    typeof scheduleFlush !== "function" ||
    typeof cancelFlush !== "function"
  ) {
    throw new TypeError("Structured logger dependencies must be functions");
  }

  let client = null;
  let flushTimer = null;
  let flushing = false;
  const buffer = [];
  const discardedChildBytes = Object.create(null);
  for (const channel of CHILD_OUTPUT_CHANNELS) {
    discardedChildBytes[channel] = 0;
  }

  function writeRecord(record) {
    const line = JSON.stringify(record);
    if (record.level === "WARN" || record.level === "ERROR") {
      platformStderr(line);
    } else {
      platformStdout(line);
    }
    if (buffer.length >= MAX_BUFFER_SIZE) buffer.shift();
    buffer.push({ timestamp: clock(), message: line });
    if (client && buffer.length >= MAX_BATCH_SIZE) {
      void flush();
    }
  }

  function emit(eventCode, metadata = {}) {
    const record = sanitizeRecord(eventCode, metadata);
    writeRecord(record);
    return record;
  }

  function installConsoleHooks(targetConsole = console) {
    if (!targetConsole || typeof targetConsole !== "object") {
      throw new TypeError("Console hook target must be an object");
    }
    if (targetConsole[CONSOLE_HOOK]) return;
    const methodLevels = Object.freeze({
      log: "INFO",
      info: "INFO",
      warn: "WARN",
      error: "ERROR",
      debug: "DEBUG",
    });
    for (const [method, level] of Object.entries(methodLevels)) {
      targetConsole[method] = (..._discardedArguments) => {
        emit("LEGACY_CONSOLE_CALL", { level });
      };
    }
    Object.defineProperty(targetConsole, CONSOLE_HOOK, {
      configurable: false,
      enumerable: false,
      value: true,
      writable: false,
    });
  }

  function installProcessFailureHooks(
    targetProcess = process,
    terminate = (code) => targetProcess.exit(code),
  ) {
    if (
      !targetProcess ||
      typeof targetProcess.once !== "function" ||
      typeof terminate !== "function"
    ) {
      throw new TypeError("Process failure hooks require exact dependencies");
    }
    if (targetProcess[PROCESS_FAILURE_HOOK]) return;
    let failing = false;
    const failClosed = () => {
      if (failing) return;
      failing = true;
      emit("RUNTIME_STATE", { level: "ERROR", status: "FAILED" });
      void shutdown().finally(() => terminate(1));
    };
    targetProcess.once("uncaughtException", failClosed);
    targetProcess.once("unhandledRejection", failClosed);
    Object.defineProperty(targetProcess, PROCESS_FAILURE_HOOK, {
      configurable: false,
      enumerable: false,
      value: true,
      writable: false,
    });
  }

  function drainChildOutput(stream, channel) {
    if (
      !stream ||
      typeof stream.on !== "function" ||
      !CHILD_OUTPUT_CHANNELS.has(channel)
    ) {
      throw new TypeError("Child output requires an allowlisted channel");
    }
    stream.on("data", (chunk) => {
      const byteLength = Buffer.isBuffer(chunk)
        ? chunk.length
        : Buffer.byteLength(String(chunk));
      discardedChildBytes[channel] = Math.min(
        Number.MAX_SAFE_INTEGER,
        discardedChildBytes[channel] + byteLength,
      );
    });
  }

  function childOutputCounts() {
    return Object.freeze(
      Object.fromEntries(
        Object.entries(discardedChildBytes).filter(([, count]) => count > 0),
      ),
    );
  }

  async function init({ env = process.env } = {}) {
    if (client) return;
    if (!env || env.AWS_REGION !== "eu-west-1") return;

    let commands;
    try {
      commands = require("@aws-sdk/client-cloudwatch-logs");
      client = clientFactory
        ? clientFactory(env.AWS_REGION)
        : new commands.CloudWatchLogsClient({ region: env.AWS_REGION });
    } catch {
      client = null;
      return;
    }

    try {
      await client.send(
        new commands.CreateLogGroupCommand({ logGroupName: LOG_GROUP }),
      );
    } catch {
      // The group normally already exists. Error text is deliberately ignored.
    }

    try {
      await client.send(
        new commands.CreateLogStreamCommand({
          logGroupName: LOG_GROUP,
          logStreamName: LOG_STREAM,
        }),
      );
    } catch (error) {
      if (error?.name !== "ResourceAlreadyExistsException") {
        client = null;
        return;
      }
    }

    flushTimer = scheduleFlush(() => {
      void flush();
    });
    flushTimer?.unref?.();
  }

  async function flush() {
    if (!client || buffer.length === 0 || flushing) return;
    flushing = true;
    const events = buffer.splice(0, MAX_BATCH_SIZE);
    events.sort((left, right) => left.timestamp - right.timestamp);
    try {
      const { PutLogEventsCommand } = require("@aws-sdk/client-cloudwatch-logs");
      await client.send(
        new PutLogEventsCommand({
          logGroupName: LOG_GROUP,
          logStreamName: LOG_STREAM,
          logEvents: events,
        }),
      );
    } catch {
      buffer.unshift(...events);
      if (buffer.length > MAX_BUFFER_SIZE) {
        buffer.splice(MAX_BUFFER_SIZE);
      }
    } finally {
      flushing = false;
    }
  }

  async function shutdown() {
    if (flushTimer) cancelFlush(flushTimer);
    flushTimer = null;
    for (let attempt = 0; attempt < 3 && buffer.length > 0; attempt += 1) {
      await flush();
    }
  }

  return Object.freeze({
    childOutputCounts,
    drainChildOutput,
    emit,
    flush,
    init,
    installConsoleHooks,
    installProcessFailureHooks,
    shutdown,
  });
}

const defaultLogger = createStructuredLogger();

module.exports = {
  createStructuredLogger,
  childOutputCounts: defaultLogger.childOutputCounts,
  drainChildOutput: defaultLogger.drainChildOutput,
  emit: defaultLogger.emit,
  flush: defaultLogger.flush,
  init: defaultLogger.init,
  installConsoleHooks: defaultLogger.installConsoleHooks,
  installProcessFailureHooks: defaultLogger.installProcessFailureHooks,
  shutdown: defaultLogger.shutdown,
};
