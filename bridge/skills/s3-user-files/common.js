/**
 * Shared utilities for s3-user-files skill.
 */
const path = require("node:path");
const fs = require("node:fs");

const BUCKET = process.env.S3_USER_FILES_BUCKET;
const REGION = process.env.AWS_REGION;
if (!REGION) {
  console.error("Error: AWS_REGION environment variable is not set.");
  process.exit(1);
}

/**
 * Sanitize a string for safe use as an S3 key component.
 * Removes path traversal, restricts to safe characters, limits length.
 */
function sanitize(str) {
  // Iteratively remove ".." until stable to prevent "...."->"..".
  let result = str;
  while (result.includes("..")) {
    result = result.replace(/\.\./g, "");
  }
  result = result.replace(/[^a-zA-Z0-9_\-.]/g, "_").slice(0, 256);
  // Reject leading/trailing dots (hidden files, path traversal)
  if (result.startsWith(".") || result.endsWith(".")) {
    throw new Error(
      `Invalid filename: "${result}" — leading/trailing dots not allowed`,
    );
  }
  return result;
}

// Files the Router Lambda stores for the user (Telegram .md/.zip uploads,
// images) live one level down in {user_id}/_uploads/. Accepting exactly this
// one folder lets the agent read the names the router gives it and the names
// list.js prints; any further "/" or ".." is still flattened by sanitize().
const UPLOADS_PREFIX = "_uploads/";

// The proxy sources the system-prompt files (AGENTS.md, SOUL.md, USER.md,
// IDENTITY.md, TOOLS.md, MEMORY.md) from "_system/<namespace>/" at the bucket
// root (bridge/workspace-files-s3.js), outside every user's prefix. Refusing
// a "_system/" filename here keeps user files from looking like those
// sources, and refusing the reserved names stops the agent from saving data
// under a name it would expect to see in its system prompt.
const SYSTEM_PREFIX = "_system/";

// Filenames the Bedrock proxy concatenates verbatim into the Converse `system`
// text block (bridge/agentcore-proxy.js WORKSPACE_FILES). Any writer surface
// that lets a user-authored string reach these keys becomes a persistent
// prompt-injection channel that the Bedrock guardrail never sees, so these
// names are refused by both skill surfaces. Keep in sync with WORKSPACE_FILES
// in bridge/agentcore-proxy.js and sanitizeFilename in
// lambda/gateway_tools/lib/mcp.js.
const RESERVED_SYSTEM_FILENAMES = new Set([
  "AGENTS.md",
  "SOUL.md",
  "USER.md",
  "IDENTITY.md",
  "TOOLS.md",
  "MEMORY.md",
]);

function isReservedSystemFilename(name) {
  return RESERVED_SYSTEM_FILENAMES.has(name);
}

/**
 * Build the S3 key from userId and optional filename.
 * Returns "sanitized_userId/", "sanitized_userId/sanitized_filename", or
 * "sanitized_userId/_uploads/sanitized_name" for router-stored uploads.
 * Refuses the reserved _system/ sub-prefix and the six reserved
 * system-prompt filenames so the user-files skill cannot overwrite the
 * sources the proxy injects into the Converse `system` block.
 */
function buildKey(userId, filename) {
  const prefix = sanitize(userId);
  if (!filename) return `${prefix}/`;
  if (filename.startsWith(SYSTEM_PREFIX) || filename === "_system") {
    throw new Error(
      `Invalid filename: "${filename}" — "${SYSTEM_PREFIX}" is reserved for system-prompt sources and is not user-writable.`,
    );
  }
  if (filename.startsWith(UPLOADS_PREFIX)) {
    const name = sanitize(filename.slice(UPLOADS_PREFIX.length));
    if (!name) throw new Error("Invalid filename: empty name after _uploads/");
    return `${prefix}/${UPLOADS_PREFIX}${name}`;
  }
  const sanitized = sanitize(filename);
  if (isReservedSystemFilename(sanitized)) {
    throw new Error(
      `Invalid filename: "${sanitized}" is a reserved system-prompt source ` +
        `and is not writable through the user-files skill. The proxy sources ` +
        `it from "${SYSTEM_PREFIX}<namespace>/${sanitized}", which is not user-writable.`,
    );
  }
  return `${prefix}/${sanitized}`;
}

// download_user_file refuses objects above this size (Telegram uploads are
// at most 20 MB).
const MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024;
const DEFAULT_DOWNLOAD_ROOT = "/tmp/openclaw-uploads";

/**
 * Local path download_user_file writes an object to: the key's base name
 * inside outDir (absolute), or /tmp/openclaw-uploads/<user_id>/ by default.
 */
function downloadPath(userId, key, outDir) {
  const dir = outDir || path.join(DEFAULT_DOWNLOAD_ROOT, sanitize(userId));
  if (!path.isAbsolute(dir)) {
    throw new Error("Output directory must be an absolute path");
  }
  return path.join(dir, path.posix.basename(key));
}

// The contract server writes the bound user's identity here and sets USER_ID
// ("channel:id") before OpenClaw runs a skill.
const IDENTITY_FILE = "/tmp/current-identity.json";

/**
 * Namespaces ("channel_id") of the user this AgentCore Runtime session is
 * bound to, from the identity file and USER_ID. Empty when neither is set.
 */
function boundNamespaces() {
  const namespaces = [];
  try {
    const identity = JSON.parse(fs.readFileSync(IDENTITY_FILE, "utf8"));
    if (typeof identity?.actorId === "string" && identity.actorId) {
      namespaces.push(identity.actorId.replace(/:/g, "_"));
    }
  } catch {
    // No readable identity file: fall back to USER_ID alone.
  }
  if (process.env.USER_ID) namespaces.push(process.env.USER_ID.replace(/:/g, "_"));
  return namespaces;
}

/**
 * Exit unless userId is the namespace of the user this session is bound to.
 * The scoped credentials only reach that user's resources, so any other
 * namespace would fail at AWS anyway; this gives a clear error first.
 */
function assertOwnNamespace(userId) {
  const bound = boundNamespaces();
  if (bound.length === 0) {
    console.error("Error: This session's user identity is not available.");
    process.exit(1);
  }
  if (bound.some((namespace) => namespace !== userId)) {
    console.error("Error: user_id does not match the user this session belongs to. Use the user_id from your system prompt.");
    process.exit(1);
  }
}

/**
 * Validate that userId is present, not the default-user fallback, matches the
 * expected channel_identifier namespace pattern, AND is the namespace of the
 * user this session is bound to. The last check is what prevents an LLM /
 * prompt-injection attacker from choosing another tenant's namespace as the
 * argv argument. Exits the process with an error message if validation fails.
 */
function validateUserId(userId) {
  if (!userId) {
    console.error("Error: user_id argument is required.");
    process.exit(1);
  }
  if (userId === "default-user" || userId === "default_user") {
    console.error(
      "Error: Cannot operate on files for default-user. User identity was not resolved.",
    );
    process.exit(1);
  }
  // Namespace must match channel_identifier pattern (e.g., telegram_123456789, slack_U0XXXXXXXX1).
  // This is a format gate only; assertOwnNamespace below proves ownership.
  const VALID_NAMESPACE =
    /^(telegram|slack|discord|whatsapp|line)_[a-zA-Z0-9_-]{1,64}$/;
  if (!VALID_NAMESPACE.test(userId)) {
    console.error(
      `Error: Invalid user_id "${userId}". Must match channel_identifier format (e.g., telegram_123456, slack_username).`,
    );
    process.exit(1);
  }
  assertOwnNamespace(userId);
}

/**
 * Validate that the S3_USER_FILES_BUCKET env var is set.
 * Exits the process with an error message if missing.
 */
function validateBucket() {
  if (!BUCKET) {
    console.error("Error: S3_USER_FILES_BUCKET environment variable not set.");
    process.exit(1);
  }
}

/**
 * Convert an S3 response body stream to a UTF-8 string.
 */
async function streamToString(stream) {
  const chunks = [];
  for await (const chunk of stream) {
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf-8");
}

module.exports = {
  BUCKET,
  REGION,
  sanitize,
  buildKey,
  downloadPath,
  MAX_DOWNLOAD_BYTES,
  validateUserId,
  assertOwnNamespace,
  validateBucket,
  streamToString,
  SYSTEM_PREFIX,
  RESERVED_SYSTEM_FILENAMES,
  isReservedSystemFilename,
};
