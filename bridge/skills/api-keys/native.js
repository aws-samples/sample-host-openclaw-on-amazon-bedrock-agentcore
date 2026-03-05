#!/usr/bin/env node
/**
 * Native file-based API key management.
 * Usage: node native.js <user_id> <action> [key_name] [key_value]
 */
const fs = require("fs");
const path = require("path");
const { validateUserId, validateKeyName } = require("./common");

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

const [userId, action, keyName, ...rest] = process.argv.slice(2);
const keyValue = rest.join(" ");

validateUserId(userId);

if (!action || !["set", "get", "list", "delete"].includes(action)) {
  console.error("Error: action must be 'set', 'get', 'list', or 'delete'.");
  process.exit(1);
}

if (action === "list") {
  const keys = readApiKeys();
  const names = Object.keys(keys);
  if (names.length === 0) {
    console.log("No API keys stored (native mode).");
  } else {
    console.log("Stored API keys (native mode):\n" + names.map((n) => `- ${n}`).join("\n"));
  }
  process.exit(0);
}

validateKeyName(keyName);

if (action === "set") {
  if (!keyValue) {
    console.error("Error: key_value is required for 'set' action.");
    process.exit(1);
  }
  const keys = readApiKeys();
  const isNew = !(keyName in keys);
  keys[keyName] = keyValue;
  writeApiKeys(keys);
  console.log(
    isNew
      ? `API key '${keyName}' saved (native mode). Stored in workspace, synced to S3 (KMS-encrypted).`
      : `API key '${keyName}' updated (native mode).`,
  );
} else if (action === "get") {
  const keys = readApiKeys();
  if (!(keyName in keys)) {
    console.error(`Error: No API key found with name '${keyName}'.`);
    process.exit(1);
  }
  console.log(keys[keyName]);
} else if (action === "delete") {
  const keys = readApiKeys();
  if (!(keyName in keys)) {
    console.error(`Error: No API key found with name '${keyName}'.`);
    process.exit(1);
  }
  delete keys[keyName];
  writeApiKeys(keys);
  console.log(`API key '${keyName}' deleted (native mode).`);
}
