#!/usr/bin/env node
/**
 * AWS Secrets Manager API key management.
 * Usage: node secret.js <user_id> <action> [key_name] [key_value]
 */
const { REGION, validateUserId, validateKeyName } = require("./common");

const SECRET_PREFIX = "openclaw/user/";
const MAX_SECRETS_PER_USER = 10;

function buildSecretName(namespace, keyName) {
  return `${SECRET_PREFIX}${namespace}/${keyName}`;
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

  const {
    SecretsManagerClient,
    ListSecretsCommand,
    GetSecretValueCommand,
    PutSecretValueCommand,
    CreateSecretCommand,
    DeleteSecretCommand,
  } = require("@aws-sdk/client-secrets-manager");

  const client = new SecretsManagerClient({ region: REGION });

  if (action === "list") {
    const prefix = `${SECRET_PREFIX}${userId}/`;
    const resp = await client.send(new ListSecretsCommand({
      Filters: [{ Key: "name", Values: [prefix] }],
      MaxResults: 100,
    }));
    const secrets = (resp.SecretList || []).map((s) => s.Name.slice(prefix.length));
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
      console.log(`Secret '${keyName}' updated (Secrets Manager, KMS-encrypted).`);
    } catch (err) {
      if (err.name === "ResourceNotFoundException") {
        // Check limit
        const prefix = `${SECRET_PREFIX}${userId}/`;
        const listResp = await client.send(new ListSecretsCommand({
          Filters: [{ Key: "name", Values: [prefix] }],
          MaxResults: 100,
        }));
        if ((listResp.SecretList || []).length >= MAX_SECRETS_PER_USER) {
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
        console.log(`Secret '${keyName}' saved (Secrets Manager, KMS-encrypted, auditable via CloudTrail).`);
      } else {
        console.error(`Error updating secret: ${err.message}`);
        process.exit(1);
      }
    }
  } else if (action === "get") {
    try {
      const resp = await client.send(new GetSecretValueCommand({ SecretId: secretName }));
      console.log(resp.SecretString);
    } catch (err) {
      if (err.name === "ResourceNotFoundException") {
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
      console.log(`Secret '${keyName}' scheduled for deletion (7-day recovery window).`);
    } catch (err) {
      if (err.name === "ResourceNotFoundException") {
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
