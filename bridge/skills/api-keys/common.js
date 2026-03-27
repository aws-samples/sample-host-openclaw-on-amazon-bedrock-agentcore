#!/usr/bin/env node
/**
 * Shared utilities for api-keys skill.
 */
const REGION = process.env.AWS_REGION;
if (!REGION) {
  console.error("Error: AWS_REGION environment variable is not set.");
  process.exit(1);
}

const VALID_KEY_NAME = /^[a-zA-Z][a-zA-Z0-9_-]{0,63}$/;
const VALID_NAMESPACE = /^(telegram|slack|discord|whatsapp)_[a-zA-Z0-9_-]{1,64}$/;

function validateUserId(userId) {
  if (!userId) {
    console.error("Error: user_id argument is required.");
    process.exit(1);
  }
  if (userId === "default-user" || userId === "default_user") {
    console.error("Error: Cannot operate for default-user. User identity was not resolved.");
    process.exit(1);
  }
  if (!VALID_NAMESPACE.test(userId)) {
    console.error(`Error: Invalid user_id "${userId}". Must match channel_identifier format (e.g., telegram_123456).`);
    process.exit(1);
  }
}

function validateKeyName(keyName) {
  if (!keyName || !VALID_KEY_NAME.test(keyName)) {
    console.error("Error: key_name is required and must be alphanumeric (a-z, 0-9, _, -), starting with a letter, max 64 chars.");
    process.exit(1);
  }
}

// Request timeout for Secrets Manager calls — prevents indefinite hangs
// on VPC endpoint issues. 30s per attempt × 3 retries = ~90s worst case.
const SM_REQUEST_TIMEOUT_MS = 30_000;

module.exports = {
  REGION,
  VALID_KEY_NAME,
  validateUserId,
  validateKeyName,
  SM_REQUEST_TIMEOUT_MS,
};
