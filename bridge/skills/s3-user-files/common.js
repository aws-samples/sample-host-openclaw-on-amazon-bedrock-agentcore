/**
 * Shared utilities for s3-user-files skill.
 */
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
  return result.replace(/[^a-zA-Z0-9_\-.]/g, "_").slice(0, 256);
}

/**
 * Build the S3 key from userId and optional filename.
 * Returns "sanitized_userId/" or "sanitized_userId/sanitized_filename".
 */
function buildKey(userId, filename) {
  const prefix = sanitize(userId);
  if (!filename) return `${prefix}/`;
  return `${prefix}/${sanitize(filename)}`;
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
  // Namespace must match channel_identifier pattern (e.g., telegram_123456789, slack_U0AGD41CBGS).
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
  validateUserId,
  validateBucket,
  streamToString,
};
