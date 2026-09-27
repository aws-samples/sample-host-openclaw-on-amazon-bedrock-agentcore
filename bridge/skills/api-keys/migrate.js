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

// A missing file means "no keys yet". Any other read or parse failure means
// the file holds data we cannot see, so we stop rather than overwrite it.
function readApiKeys() {
  const filePath = getApiKeysPath();
  let raw;
  try {
    raw = fs.readFileSync(filePath, "utf-8");
  } catch (err) {
    if (err.code === "ENOENT") return {};
    console.error(`Error: native key file is unreadable (${err.code || err.name}); not modified.`);
    process.exit(1);
  }
  let keys;
  try {
    keys = JSON.parse(raw);
  } catch {
    keys = undefined;
  }
  if (!keys || typeof keys !== "object" || Array.isArray(keys)) {
    console.error("Error: native key file is not a valid JSON object; not modified.");
    process.exit(1);
  }
  return keys;
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

    // A binary-only or empty secret has no string value to store natively;
    // writing it would drop the key and the delete below would then lose it.
    if (typeof value !== "string" || value === "") {
      console.error(`Error: secret '${keyName}' has no string value; nothing migrated.`);
      process.exit(1);
    }

    // Write to native (exits without writing if the existing file is unreadable)
    const keys = readApiKeys();
    keys[keyName] = value;
    writeApiKeys(keys);

    // Delete from Secrets Manager only after the native write succeeded.
    // Keep a recovery window so the key survives if the native copy is lost
    // before it is backed up.
    await client.send(new DeleteSecretCommand({
      SecretId: secretName,
      RecoveryWindowInDays: 7,
    }));

    console.log(`Key '${keyName}' migrated from Secrets Manager to native.`);
  }
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
