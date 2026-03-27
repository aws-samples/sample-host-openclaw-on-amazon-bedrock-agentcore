#!/usr/bin/env node
/**
 * Migrate an API key between native file storage and AWS Secrets Manager.
 * Usage: node migrate.js <user_id> <key_name> <direction>
 *   direction: "native-to-secure" or "secure-to-native"
 */
const fs = require("fs");
const path = require("path");
const { REGION, validateUserId, validateKeyName, SM_REQUEST_TIMEOUT_MS } = require("./common");

const SECRET_PREFIX = "openclaw/user/";
const API_KEYS_FILENAME = "user-api-keys.json";

function getApiKeysPath() {
  return path.join(process.env.HOME || "/root", ".openclaw", API_KEYS_FILENAME);
}

function readApiKeys() {
  try {
    return JSON.parse(fs.readFileSync(getApiKeysPath(), "utf-8"));
  } catch {
    return {};
  }
}

function writeApiKeys(keys) {
  const filePath = getApiKeysPath();
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const tmp = filePath + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(keys, null, 2), { mode: 0o600 });
  fs.renameSync(tmp, filePath);
}

async function main() {
  const [userId, keyName, direction] = process.argv.slice(2);

  validateUserId(userId);
  validateKeyName(keyName);

  if (!direction || !["native-to-secure", "secure-to-native"].includes(direction)) {
    console.error("Error: direction must be 'native-to-secure' or 'secure-to-native'.");
    process.exit(1);
  }

  const {
    SecretsManagerClient,
    GetSecretValueCommand,
    PutSecretValueCommand,
    CreateSecretCommand,
    DeleteSecretCommand,
  } = require("@aws-sdk/client-secrets-manager");

  const client = new SecretsManagerClient({
    region: REGION,
    requestHandler: { requestTimeout: SM_REQUEST_TIMEOUT_MS },
  });
  const secretName = `${SECRET_PREFIX}${userId}/${keyName}`;

  if (direction === "native-to-secure") {
    // Read from native
    const keys = readApiKeys();
    if (!(keyName in keys)) {
      console.error(`Error: No native key found with name '${keyName}'.`);
      process.exit(1);
    }
    const value = keys[keyName];

    // Write to Secrets Manager
    try {
      await client.send(new PutSecretValueCommand({
        SecretId: secretName,
        SecretString: value,
      }));
    } catch (err) {
      if (err.name === "ResourceNotFoundException") {
        await client.send(new CreateSecretCommand({
          Name: secretName,
          SecretString: value,
          Tags: [
            { Key: "openclaw:user", Value: userId },
            { Key: "openclaw:managed", Value: "true" },
          ],
        }));
      } else {
        console.error(`Error creating secret: ${err.message}`);
        process.exit(1);
      }
    }

    // Remove from native
    delete keys[keyName];
    writeApiKeys(keys);

    console.log(`Key '${keyName}' migrated from native to Secrets Manager.`);
  } else {
    // secure-to-native: Read from Secrets Manager
    let value;
    try {
      const resp = await client.send(new GetSecretValueCommand({ SecretId: secretName }));
      value = resp.SecretString;
    } catch (err) {
      if (err.name === "ResourceNotFoundException") {
        console.error(`Error: No secret found with name '${keyName}' in Secrets Manager.`);
      } else {
        console.error(`Error reading secret: ${err.message}`);
      }
      process.exit(1);
    }

    // Write to native
    const keys = readApiKeys();
    keys[keyName] = value;
    writeApiKeys(keys);

    // Delete from Secrets Manager
    await client.send(new DeleteSecretCommand({
      SecretId: secretName,
      ForceDeleteWithoutRecovery: true,
    }));

    console.log(`Key '${keyName}' migrated from Secrets Manager to native.`);
  }
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
