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

/**
 * Initialize the CloudWatch logger. Hooks console.log/warn/error.
 * @param {string} streamName - Log stream name (e.g., user ID or session ID)
 */
async function init(streamName) {
  const region = process.env.AWS_REGION;
  if (!region) return;

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
      return;
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

module.exports = { init, flush, shutdown };
