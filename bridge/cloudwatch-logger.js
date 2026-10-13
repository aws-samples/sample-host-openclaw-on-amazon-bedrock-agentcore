/**
 * CloudWatch Logger — ships container stdout/stderr to CloudWatch Logs.
 *
 * Hooks console.log/warn/error to buffer log events and flush them
 * to CloudWatch periodically (every 5s) and on process exit.
 *
 * Usage: require('./cloudwatch-logger').init('stream-name');
 */
const {
  CloudWatchLogsClient,
  CreateLogGroupCommand,
  CreateLogStreamCommand,
  PutLogEventsCommand,
} = require("@aws-sdk/client-cloudwatch-logs");

const LOG_GROUP = "/openclaw/container";
const FLUSH_INTERVAL_MS = 5000;
const MAX_BATCH_SIZE = 100;

let client = null;
let logStreamName = null;
let buffer = [];
let flushTimer = null;
let flushing = false;
let scopedOwner = null;

/**
 * Initialize the CloudWatch logger. Hooks console.log/warn/error.
 * @param {string} streamName - Log stream name (e.g., user ID or session ID)
 * @returns {Promise<boolean>} Whether scoped log collection is ready.
 */
async function init(streamName) {
  if (scopedOwner) throw new Error("Scoped logger initialization modes cannot be mixed");
  const region = process.env.AWS_REGION;
  if (!region) return false;

  client = new CloudWatchLogsClient({ region });
  logStreamName = streamName || `container-${Date.now()}`;

  // Create log group (ignore if already exists)
  try {
    await client.send(new CreateLogGroupCommand({ logGroupName: LOG_GROUP }));
  } catch (err) {
    if (err.name !== "ResourceAlreadyExistsException") {
      // Non-fatal — group may already exist from a previous session
    }
  }

  // Create log stream (ignore if already exists)
  try {
    await client.send(
      new CreateLogStreamCommand({
        logGroupName: LOG_GROUP,
        logStreamName,
      })
    );
  } catch (err) {
    if (err.name !== "ResourceAlreadyExistsException") {
      client = null;
      return false;
    }
  }

  // Hook console methods
  const origLog = console.log;
  const origWarn = console.warn;
  const origError = console.error;

  console.log = (...args) => {
    origLog.apply(console, args);
    enqueue("INFO", args);
  };
  console.warn = (...args) => {
    origWarn.apply(console, args);
    enqueue("WARN", args);
  };
  console.error = (...args) => {
    origError.apply(console, args);
    enqueue("ERROR", args);
  };

  // Periodic flush
  flushTimer = setInterval(() => flush().catch(() => {}), FLUSH_INTERVAL_MS);
  flushTimer.unref(); // Don't prevent process exit
  return true;
}

function enqueue(level, args) {
  if (!client) return;
  const message = `[${level}] ${args.map((a) => (typeof a === "string" ? a : JSON.stringify(a))).join(" ")}`;
  buffer.push({ timestamp: Date.now(), message });
  if (buffer.length >= MAX_BATCH_SIZE) {
    flush().catch(() => {});
  }
}

async function flush() {
  if (!client || buffer.length === 0 || flushing) return;
  flushing = true;

  const events = buffer.splice(0, MAX_BATCH_SIZE);
  // CloudWatch requires events sorted by timestamp
  events.sort((a, b) => a.timestamp - b.timestamp);

  try {
    await client.send(
      new PutLogEventsCommand({
        logGroupName: LOG_GROUP,
        logStreamName,
        logEvents: events,
      })
    );
  } catch (err) {
    // Put events back on failure (best-effort)
    buffer.unshift(...events);
  } finally {
    flushing = false;
  }
}

/**
 * Final flush — call before process exit.
 */
async function shutdown() {
  if (flushTimer) clearInterval(flushTimer);
  // Flush up to 3 times to drain buffer
  for (let i = 0; i < 3 && buffer.length > 0; i++) {
    await flush();
  }
}

// Capture belongs to the bound session while remote log initialization overlaps activation.
function startOwned(streamName, { binding, generation, signal } = {}) {
  if (!binding || typeof binding.userId !== "string" || !binding.userId ||
      typeof binding.actorId !== "string" || !binding.actorId ||
      typeof generation !== "string" || !/^[a-zA-Z0-9_-]{1,80}$/.test(generation) ||
      typeof streamName !== "string" || !streamName ||
      (binding.routingGeneration != null &&
        (!Number.isSafeInteger(binding.routingGeneration) || binding.routingGeneration < 0)) ||
      (binding.runtimeSessionId != null && typeof binding.runtimeSessionId !== "string") ||
      !signal || typeof signal.addEventListener !== "function" ||
      typeof signal.removeEventListener !== "function") {
    throw new Error("Missing scoped logger binding or generation");
  }
  const identity = Object.freeze({
    userId: binding.userId, actorId: binding.actorId,
    routingGeneration: binding.routingGeneration ?? null,
    runtimeSessionId: binding.runtimeSessionId ?? null, generation,
  });
  if (scopedOwner) {
    if (Object.keys(identity).some((key) => identity[key] !== scopedOwner.identity[key]) ||
        streamName !== scopedOwner.streamName || signal !== scopedOwner.signal) {
      throw new Error("Scoped logger does not match the active binding");
    }
    return scopedOwner;
  }
  if (client || logStreamName || flushTimer) {
    throw new Error("Scoped logger initialization modes cannot be mixed");
  }
  if (signal?.aborted) throw signal.reason || new Error("Scoped logger binding was aborted");

  const region = process.env.AWS_REGION;
  const ownerClient = region ? new CloudWatchLogsClient({ region }) : null;
  const ownerStream = streamName || `container-${Date.now()}`;
  const queue = [];
  const original = { log: console.log, warn: console.warn, error: console.error };
  const hooks = {};
  const initController = new AbortController();
  let captureAttached = false;
  let remoteReady = false;
  let retired = false;
  let closed = false;
  let destroyed = false;
  let timer = null;
  let inFlight = null;
  let sendController = null;
  let closePromise = null;
  function destroy() {
    if (destroyed) return;
    destroyed = true;
    try { ownerClient?.destroy(); } catch {}
  }
  function retire() {
    if (retired) return;
    retired = true;
    for (const method of Object.keys(original)) {
      if (console[method] === hooks[method]) console[method] = original[method];
    }
    captureAttached = false;
    if (timer) clearInterval(timer);
    timer = null;
    signal?.removeEventListener("abort", retire);
    initController.abort();
    sendController?.abort();
  }
  function flushOwned(drain = false) {
    if (closed || (retired && !drain) || !remoteReady || !ownerClient) return Promise.resolve();
    if (inFlight) return inFlight;
    if (queue.length === 0) return Promise.resolve();
    const events = queue.splice(0, MAX_BATCH_SIZE);
    events.sort((a, b) => a.timestamp - b.timestamp);
    const controller = new AbortController();
    sendController = controller;
    const work = Promise.resolve().then(() => {
      if (closed || (retired && !drain)) {
        queue.unshift(...events);
        return;
      }
      return ownerClient.send(new PutLogEventsCommand({
        logGroupName: LOG_GROUP, logStreamName: ownerStream, logEvents: events,
      }), { abortSignal: controller.signal });
    }).catch(() => {
      // A late failure requeues only into the same retired or live owner.
      queue.unshift(...events);
    }).finally(() => {
      if (inFlight === work) {
        inFlight = null;
        sendController = null;
      }
    });
    inFlight = work;
    return work;
  }
  function enqueueOwned(level, args) {
    if (retired || !captureAttached) return;
    const message = `[${level}] ${args.map((a) => (typeof a === "string" ? a : JSON.stringify(a))).join(" ")}`;
    // MAX_BATCH_SIZE is a send batch, not a queue cap. Native output remains live.
    queue.push({ timestamp: Date.now(), message });
    if (queue.length >= MAX_BATCH_SIZE) void flushOwned();
  }
  function shutdownOwned() {
    if (closePromise) return closePromise;
    retire(); // Retire synchronously, before any cleanup await or late SDK completion.
    if (!remoteReady || !ownerClient) {
      closed = true;
      destroy();
      closePromise = Promise.resolve();
      return closePromise;
    }
    closePromise = (async () => {
      let deadline;
      const work = (async () => {
        await inFlight;
        for (let i = 0; i < 3 && queue.length && !closed; i++) await flushOwned(true);
      })();
      try {
        await Promise.race([work, new Promise((resolve) => {
          deadline = setTimeout(resolve, FLUSH_INTERVAL_MS);
        })]);
      } finally {
        closed = true;
        sendController?.abort();
        clearTimeout(deadline);
        destroy();
      }
    })();
    return closePromise;
  }
  if (ownerClient) {
    for (const [method, level] of [["log", "INFO"], ["warn", "WARN"], ["error", "ERROR"]]) {
      hooks[method] = (...args) => {
        original[method].apply(console, args);
        enqueueOwned(level, args);
      };
      console[method] = hooks[method];
    }
    captureAttached = true;
  }
  signal?.addEventListener("abort", retire, { once: true });
  if (signal?.aborted) retire();
  const ready = (async () => {
    if (!ownerClient || retired) return false;
    try {
      await ownerClient.send(new CreateLogGroupCommand({ logGroupName: LOG_GROUP }),
        { abortSignal: initController.signal });
    } catch (err) {
      if (err.name !== "ResourceAlreadyExistsException") {
        // Preserve the original non-fatal group-error behavior.
      }
    }
    if (retired) return false;
    try {
      await ownerClient.send(new CreateLogStreamCommand({
        logGroupName: LOG_GROUP, logStreamName: ownerStream,
      }), { abortSignal: initController.signal });
    } catch (err) {
      if (err.name !== "ResourceAlreadyExistsException") return false;
    }
    if (retired) return false;
    remoteReady = true;
    timer = setInterval(() => { void flushOwned(); }, FLUSH_INTERVAL_MS);
    timer.unref();
    if (queue.length >= MAX_BATCH_SIZE) void flushOwned();
    return true;
  })().then((result) => {
    if (result !== true) {
      retire();
      destroy();
    }
    return result;
  }, (error) => {
    retire();
    destroy();
    throw error;
  });
  ready.catch(() => {}); // Keep the rejection observable without an unhandled late branch.
  scopedOwner = Object.freeze({
    identity, streamName, signal, ready, retire, shutdown: shutdownOwned,
    flush: () => flushOwned(), get captureAttached() { return captureAttached; },
  });
  return scopedOwner;
}

module.exports = {
  init, startOwned,
  flush: () => scopedOwner ? scopedOwner.flush() : flush(),
  shutdown: () => scopedOwner ? scopedOwner.shutdown() : shutdown(),
};
