"use strict";

const fs = require("node:fs");
const path = require("node:path");

const RUNTIME_REGION = "eu-west-1";
const VALID_BUCKET = /^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/;
const BROKER_FUNCTION_NAME = "personal-operator-workspace-credential-broker";
const MAX_CAPABILITY_BYTES = 2_048;
const MAX_BROKER_RESPONSE_BYTES = 16_384;
const MAX_SCOPED_CREDENTIAL_LIFETIME_MS = 15.5 * 60 * 1000;

const FORWARDED_ENV_KEYS = Object.freeze(["S3_USER_FILES_BUCKET"]);
const FIXED_CHILD_ENV = Object.freeze({
  PATH: "/usr/local/bin:/usr/bin:/bin",
  HOME: "/run/personal-operator/home",
  NODE_PATH: "/app/node_modules",
  NODE_OPTIONS:
    "--dns-result-order=ipv4first --no-network-family-autoselection -r /app/force-ipv4.js",
});

const CREDENTIAL_ENV_BLOCKLIST = Object.freeze([
  "AWS_ACCESS_KEY_ID",
  "AWS_SECRET_ACCESS_KEY",
  "AWS_SESSION_TOKEN",
  "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
  "AWS_CONTAINER_CREDENTIALS_FULL_URI",
  "AWS_CONTAINER_AUTHORIZATION_TOKEN",
  "AWS_WEB_IDENTITY_TOKEN_FILE",
  "AWS_ROLE_ARN",
  "AWS_PROFILE",
  "AWS_CREDENTIAL_PROFILES_FILE",
]);

function requireExactRegion(env = {}) {
  for (const key of ["AWS_REGION", "AWS_DEFAULT_REGION"]) {
    if (env[key] !== undefined && env[key] !== "" && env[key] !== RUNTIME_REGION) {
      throw new Error(`${key} must be exactly ${RUNTIME_REGION}`);
    }
  }
  return RUNTIME_REGION;
}

function validateBucket(bucket) {
  if (
    typeof bucket !== "string" ||
    !VALID_BUCKET.test(bucket) ||
    bucket.includes("..") ||
    bucket.includes(".-") ||
    bucket.includes("-.")
  ) {
    throw new Error("Invalid S3 workspace bucket name");
  }
  return bucket;
}

function validateTemporaryCredentials(credentials, now = Date.now()) {
  if (
    !credentials ||
    typeof credentials.AccessKeyId !== "string" ||
    credentials.AccessKeyId.length === 0 ||
    typeof credentials.SecretAccessKey !== "string" ||
    credentials.SecretAccessKey.length === 0 ||
    typeof credentials.SessionToken !== "string" ||
    credentials.SessionToken.length === 0
  ) {
    throw new Error("STS returned incomplete scoped credentials");
  }
  const expiration = new Date(credentials.Expiration);
  if (!Number.isFinite(expiration.getTime()) || expiration.getTime() <= now) {
    throw new Error("STS returned expired scoped credentials");
  }
  if (expiration.getTime() > now + MAX_SCOPED_CREDENTIAL_LIFETIME_MS) {
    throw new Error("Scoped credentials exceeded their fixed maximum lifetime");
  }
  return {
    accessKeyId: credentials.AccessKeyId,
    secretAccessKey: credentials.SecretAccessKey,
    sessionToken: credentials.SessionToken,
    expiration,
  };
}

async function createScopedCredentials(capability, options = {}) {
  const env = options.env || process.env;
  const region = requireExactRegion(env);
  if (
    typeof capability !== "string" ||
    !capability ||
    !capability.isWellFormed() ||
    !/^[\x20-\x7e]+$/.test(capability) ||
    Buffer.byteLength(capability, "ascii") > MAX_CAPABILITY_BYTES
  ) {
    throw new Error("A bounded ASCII workspace capability is required");
  }
  const functionName = env.WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME;
  if (functionName !== BROKER_FUNCTION_NAME) {
    throw new Error(
      `WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME must be exactly ${BROKER_FUNCTION_NAME}`,
    );
  }
  const commandInput = {
    FunctionName: functionName,
    InvocationType: "RequestResponse",
    Payload: Buffer.from(JSON.stringify({ capability })),
  };

  let lambdaClient = options.lambdaClient;
  let command;
  if (lambdaClient) {
    command = { input: commandInput };
  } else {
    const { LambdaClient, InvokeCommand } = require("@aws-sdk/client-lambda");
    lambdaClient = new LambdaClient({ region, maxAttempts: 1 });
    command = new InvokeCommand(commandInput);
  }
  const response = await lambdaClient.send(command);
  if (
    response?.StatusCode !== 200 ||
    response?.FunctionError ||
    !response?.Payload
  ) {
    throw new Error("Workspace credential broker invocation failed");
  }
  const raw = Buffer.from(response.Payload);
  if (!raw.length || raw.length > MAX_BROKER_RESPONSE_BYTES) {
    throw new Error("Workspace credential broker response is invalid");
  }
  let credentials;
  try {
    credentials = JSON.parse(raw.toString("utf8"));
  } catch {
    throw new Error("Workspace credential broker response is invalid");
  }
  const exactKeys = [
    "AccessKeyId",
    "Expiration",
    "SecretAccessKey",
    "SessionToken",
    "Version",
  ];
  if (
    !credentials ||
    typeof credentials !== "object" ||
    Array.isArray(credentials) ||
    credentials.Version !== 1 ||
    !exactKeys.every((key) => Object.hasOwn(credentials, key)) ||
    Object.keys(credentials).length !== exactKeys.length
  ) {
    throw new Error("Workspace credential broker returned malformed credentials");
  }
  return validateTemporaryCredentials(credentials);
}

function normalizeCredentialFileInput(credentials) {
  const normalized = validateTemporaryCredentials({
    AccessKeyId: credentials?.accessKeyId,
    SecretAccessKey: credentials?.secretAccessKey,
    SessionToken: credentials?.sessionToken,
    Expiration: credentials?.expiration,
  });
  return {
    Version: 1,
    AccessKeyId: normalized.accessKeyId,
    SecretAccessKey: normalized.secretAccessKey,
    SessionToken: normalized.sessionToken,
    Expiration: normalized.expiration.toISOString(),
  };
}

function atomicWrite(filePath, content) {
  const temporaryPath = `${filePath}.tmp`;
  try {
    fs.writeFileSync(temporaryPath, content, { mode: 0o600 });
    fs.renameSync(temporaryPath, filePath);
    fs.chmodSync(filePath, 0o600);
  } catch (error) {
    try {
      fs.rmSync(temporaryPath, { force: true });
    } catch {}
    throw error;
  }
}

function writeCredentialFiles(credentials, dir, env = process.env) {
  const region = requireExactRegion(env);
  if (typeof dir !== "string" || dir.length === 0) {
    throw new Error("A scoped credential directory is required");
  }
  const credentialDocument = normalizeCredentialFileInput(credentials);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  fs.chmodSync(dir, 0o700);

  const credentialsPath = path.join(dir, "scoped-creds.json");
  atomicWrite(credentialsPath, JSON.stringify(credentialDocument, null, 2));

  const configPath = path.join(dir, "scoped-aws-config");
  const config = [
    "[default]",
    `credential_process = /bin/cat ${JSON.stringify(credentialsPath)}`,
    `region = ${region}`,
    "",
  ].join("\n");
  atomicWrite(configPath, config);
  return Object.freeze({ credentialsPath, configPath });
}

function buildOpenClawEnv({ credDir, baseEnv = {} } = {}) {
  if (typeof credDir !== "string" || credDir.length === 0) {
    throw new Error("A scoped credential directory is required");
  }
  const region = requireExactRegion(baseEnv);
  if (!baseEnv.S3_USER_FILES_BUCKET) {
    throw new Error("S3_USER_FILES_BUCKET is required for OpenClaw");
  }
  validateBucket(baseEnv.S3_USER_FILES_BUCKET);

  const env = {};
  Object.assign(env, FIXED_CHILD_ENV);
  for (const key of FORWARDED_ENV_KEYS) {
    if (baseEnv[key] !== undefined && baseEnv[key] !== "") {
      env[key] = baseEnv[key];
    }
  }
  env.AWS_REGION = region;
  env.AWS_DEFAULT_REGION = region;
  env.AWS_CONFIG_FILE = path.join(credDir, "scoped-aws-config");
  env.AWS_SDK_LOAD_CONFIG = "1";
  env.AWS_EC2_METADATA_DISABLED = "true";
  env.AWS_SHARED_CREDENTIALS_FILE = "/dev/null";
  env.PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE = path.join(
    credDir,
    "scoped-creds.json",
  );
  env.OPENCLAW_SKIP_CRON = "1";

  for (const key of CREDENTIAL_ENV_BLOCKLIST) delete env[key];
  return env;
}

module.exports = {
  RUNTIME_REGION,
  CREDENTIAL_ENV_BLOCKLIST,
  FORWARDED_ENV_KEYS,
  FIXED_CHILD_ENV,
  requireExactRegion,
  createScopedCredentials,
  writeCredentialFiles,
  buildOpenClawEnv,
  validateTemporaryCredentials,
};
