#!/usr/bin/env node
/**
 * AWS Secrets Manager API key management.
 * Usage: node secret.js <user_id> <action> [key_name] [key_value]
 *
 * `list` and the per-user secret limit read a per-user name index kept in the
 * user's own S3 prefix (see secrets-index.js) instead of calling
 * secretsmanager:ListSecrets, which cannot be scoped to one tenant.
 */
const { REGION, validateUserId, validateKeyName, SM_REQUEST_TIMEOUT_MS } = require("./common");
const secretsIndex = require("./secrets-index");

const SECRET_PREFIX = "openclaw/user/";
const MAX_SECRETS_PER_USER = 10;
const BUCKET = process.env.S3_USER_FILES_BUCKET;

function buildSecretName(namespace, keyName) {
  return `${SECRET_PREFIX}${namespace}/${keyName}`;
}

function requireBucket() {
  if (!BUCKET) {
    console.error("Error: S3_USER_FILES_BUCKET environment variable is not set.");
    process.exit(1);
  }
}

// Index maintenance is secondary to the Secrets Manager write that just
// succeeded: never fail the user's operation because the name index could not
// be updated. A stale index self-heals on the next list/get of that name.
async function tryIndex(op, keyName) {
  try {
    await op();
  } catch (err) {
    console.error(`Warning: API-key index not updated for '${keyName}': ${err.message}`);
  }
}

async function main() {
  const [userId, action, keyName, ...rest] = process.argv.slice(2);
  const keyValue = rest.join(" ");

  validateUserId(userId);

  if (!action || !["set", "get", "list", "delete"].includes(action)) {
    console.error("Error: action must be 'set', 'get', 'list', or 'delete'.");
    process.exit(1);
  }
  if (action !== "list") {
    validateKeyName(keyName);
    if (action === "set" && !keyValue) {
      console.error("Error: key_value is required for 'set' action.");
      process.exit(1);
    }
  }

  requireBucket();
  const s3 = secretsIndex.makeClient(REGION);

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

  if (action === "list") {
    const secrets = await secretsIndex.listKeys(s3, BUCKET, userId);
    if (secrets.length === 0) {
      console.log("No secrets stored (Secrets Manager).");
    } else {
      console.log("Stored secrets (Secrets Manager):\n" + secrets.map((n) => `- ${n}`).join("\n"));
    }
    return;
  }

  const secretName = buildSecretName(userId, keyName);

  if (action === "set") {
    try {
      await client.send(new PutSecretValueCommand({
        SecretId: secretName,
        SecretString: keyValue,
      }));
      // Existing secret updated — make sure it is in the index (self-heals a
      // key created before the index existed).
      await tryIndex(() => secretsIndex.addKey(s3, BUCKET, userId, keyName), keyName);
      console.log(`Secret '${keyName}' updated (Secrets Manager, KMS-encrypted).`);
    } catch (err) {
      if (err.name === "ResourceNotFoundException") {
        // Check the per-user limit from the index.
        const names = await secretsIndex.listKeys(s3, BUCKET, userId);
        if (names.length >= MAX_SECRETS_PER_USER) {
          console.error(`Error: Maximum ${MAX_SECRETS_PER_USER} secrets per user reached.`);
          process.exit(1);
        }
        await client.send(new CreateSecretCommand({
          Name: secretName,
          SecretString: keyValue,
          Tags: [
            { Key: "openclaw:user", Value: userId },
            { Key: "openclaw:managed", Value: "true" },
          ],
        }));
        await tryIndex(() => secretsIndex.addKey(s3, BUCKET, userId, keyName), keyName);
        console.log(`Secret '${keyName}' saved (Secrets Manager, KMS-encrypted, auditable via CloudTrail).`);
      } else {
        console.error(`Error updating secret: ${err.message}`);
        process.exit(1);
      }
    }
  } else if (action === "get") {
    try {
      const resp = await client.send(new GetSecretValueCommand({ SecretId: secretName }));
      // Self-heal: a readable key belongs in the index.
      await tryIndex(() => secretsIndex.addKey(s3, BUCKET, userId, keyName), keyName);
      console.log(resp.SecretString);
    } catch (err) {
      if (err.name === "ResourceNotFoundException") {
        // The key is gone — drop any stale index entry.
        await tryIndex(() => secretsIndex.removeKey(s3, BUCKET, userId, keyName), keyName);
        console.error(`Error: No secret found with name '${keyName}'.`);
      } else {
        console.error(`Error retrieving secret: ${err.message}`);
      }
      process.exit(1);
    }
  } else if (action === "delete") {
    try {
      await client.send(new DeleteSecretCommand({
        SecretId: secretName,
        RecoveryWindowInDays: 7,
      }));
      await tryIndex(() => secretsIndex.removeKey(s3, BUCKET, userId, keyName), keyName);
      console.log(`Secret '${keyName}' scheduled for deletion (7-day recovery window).`);
    } catch (err) {
      if (err.name === "ResourceNotFoundException") {
        await tryIndex(() => secretsIndex.removeKey(s3, BUCKET, userId, keyName), keyName);
        console.error(`Error: No secret found with name '${keyName}'.`);
      } else {
        console.error(`Error deleting secret: ${err.message}`);
      }
      process.exit(1);
    }
  }
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
