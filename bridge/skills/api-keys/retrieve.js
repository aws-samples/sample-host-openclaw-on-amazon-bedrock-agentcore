#!/usr/bin/env node
/**
 * Unified API key retrieval — checks Secrets Manager first, falls back to native file.
 * Usage: node retrieve.js <user_id> <key_name>
 */
const fs = require("fs");
const path = require("path");
const { REGION, validateUserId, validateKeyName, SM_REQUEST_TIMEOUT_MS } = require("./common");

const SECRET_PREFIX = "openclaw/user/";
const API_KEYS_FILENAME = "user-api-keys.json";

function getApiKeysPath() {
  return path.join(process.env.HOME || "/root", ".openclaw", API_KEYS_FILENAME);
}

async function main() {
  const [userId, keyName] = process.argv.slice(2);

  validateUserId(userId);
  validateKeyName(keyName);

  // Try Secrets Manager first
  const {
    SecretsManagerClient,
    GetSecretValueCommand,
  } = require("@aws-sdk/client-secrets-manager");

  const client = new SecretsManagerClient({
    region: REGION,
    requestHandler: { requestTimeout: SM_REQUEST_TIMEOUT_MS },
  });
  const secretName = `${SECRET_PREFIX}${userId}/${keyName}`;

  try {
    const resp = await client.send(new GetSecretValueCommand({ SecretId: secretName }));
    console.log(`Backend: Secrets Manager\n${resp.SecretString}`);
    return;
  } catch (err) {
    if (err.name !== "ResourceNotFoundException") {
      console.error(`Error checking Secrets Manager: ${err.message}`);
      // Fall through to native
    }
  }

  // Fall back to native file
  try {
    const keys = JSON.parse(fs.readFileSync(getApiKeysPath(), "utf-8"));
    if (keyName in keys) {
      console.log(`Backend: native (file-based)\n${keys[keyName]}`);
      return;
    }
  } catch {
    // File doesn't exist or is invalid — key not found
  }

  console.error(`Error: No key found with name '${keyName}' in either backend.`);
  process.exit(1);
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
