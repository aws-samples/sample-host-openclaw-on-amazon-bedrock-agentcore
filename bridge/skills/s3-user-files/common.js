/**
 * Shared utilities for s3-user-files skill.
 */
const path = require("node:path");

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

/**
 * Build the S3 key from userId and optional filename.
 * Returns "sanitized_userId/", "sanitized_userId/sanitized_filename", or
 * "sanitized_userId/_uploads/sanitized_name" for router-stored uploads.
 */
function buildKey(userId, filename) {
  const prefix = sanitize(userId);
  if (!filename) return `${prefix}/`;
  if (filename.startsWith(UPLOADS_PREFIX)) {
    const name = sanitize(filename.slice(UPLOADS_PREFIX.length));
    if (!name) throw new Error("Invalid filename: empty name after _uploads/");
    return `${prefix}/${UPLOADS_PREFIX}${name}`;
  }
  return `${prefix}/${sanitize(filename)}`;
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

/**
 * Validate that userId is present, not the default-user fallback,
 * and matches the expected channel_identifier namespace pattern.
 * Exits the process with an error message if validation fails.
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
  // This prevents prompt injection attacks where a user tricks the AI into using
  // an arbitrary namespace to access another user's files.
  const VALID_NAMESPACE =
    /^(telegram|slack|discord|whatsapp)_[a-zA-Z0-9_-]{1,64}$/;
  if (!VALID_NAMESPACE.test(userId)) {
    console.error(
      `Error: Invalid user_id "${userId}". Must match channel_identifier format (e.g., telegram_123456, slack_username).`,
    );
    process.exit(1);
  }
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
  validateBucket,
  streamToString,
};
