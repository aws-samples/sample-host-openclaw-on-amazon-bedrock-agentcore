#!/usr/bin/env node
/**
 * Shared utilities for api-keys skill.
 */
const fs = require("node:fs");
const REGION = process.env.AWS_REGION;
if (!REGION) {
  console.error("Error: AWS_REGION environment variable is not set.");
  process.exit(1);
}

const VALID_KEY_NAME = /^[a-zA-Z][a-zA-Z0-9_-]{0,63}$/;
const VALID_NAMESPACE = /^(telegram|slack|discord|whatsapp|line)_[a-zA-Z0-9_-]{1,64}$/;

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
  assertOwnNamespace(userId);
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
  assertOwnNamespace,
  validateKeyName,
  SM_REQUEST_TIMEOUT_MS,
};
