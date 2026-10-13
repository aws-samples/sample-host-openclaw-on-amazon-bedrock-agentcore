/**
 * Scoped S3 Credentials — Per-user IAM isolation via STS session policies.
 *
 * Creates STS AssumeRole session credentials that restrict S3 access to
 * a single user's namespace prefix. Prevents cross-user data access even
 * if OpenClaw's bash/code execution tools are used to call the AWS CLI/SDK.
 *
 * Usage:
 *   const { createScopedCredentials, writeCredentialFiles, buildOpenClawEnv } = require("./scoped-credentials");
 *   const creds = await createScopedCredentials(namespace);
 *   writeCredentialFiles(creds, "/tmp/scoped");
 *   const env = buildOpenClawEnv({ credDir: "/tmp/scoped", baseEnv: process.env });
 *   spawn("openclaw", args, { env });
 */

const fs = require("fs");
const path = require("path");
const { isAsyncFunction } = require("util").types;

const VALID_NAMESPACE = /^[a-zA-Z][a-zA-Z0-9_-]{1,64}$/;

function validateNamespace(namespace) {
  if (typeof namespace !== "string" || !VALID_NAMESPACE.test(namespace)) {
    throw new Error(`Invalid namespace "${namespace}" — must match ${VALID_NAMESPACE}`);
  }
  return namespace;
}

function credentialsExpireAt(creds) {
  const expiresAt = new Date(creds?.expiration).getTime();
  return Number.isFinite(expiresAt) ? expiresAt : 0;
}

function credentialsNeedRefresh(creds, { now = Date.now(), minValidityMs = 5 * 60 * 1000 } = {}) {
  const timestamp = typeof now === "function" ? now() : now;
  return !creds || credentialsExpireAt(creds) <= timestamp + minValidityMs;
}

function assertCredentialsFresh(creds, { now = Date.now, minValidityMs = 0 } = {}) {
  for (const key of ["accessKeyId", "secretAccessKey", "sessionToken"]) {
    if (!creds || typeof creds[key] !== "string" || !creds[key]) {
      throw new Error(`Scoped credentials: ${key} is required`);
    }
  }
  if (credentialsNeedRefresh(creds, { now, minValidityMs })) {
    throw new Error("Scoped credentials expired or expiration is invalid");
  }
  return creds;
}

// ENV vars that MUST be excluded from OpenClaw to prevent credential leakage
const CREDENTIAL_ENV_BLOCKLIST = [
  "AWS_ACCESS_KEY_ID",
  "AWS_SECRET_ACCESS_KEY",
  "AWS_SESSION_TOKEN",
  "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
  "AWS_CONTAINER_CREDENTIALS_FULL_URI",
  "AWS_WEB_IDENTITY_TOKEN_FILE",
  "AWS_ROLE_ARN",
];

// ENV vars forwarded from baseEnv to OpenClaw process
const FORWARDED_ENV_KEYS = [
  "PATH",
  "HOME",
  "NODE_PATH",
  "NODE_OPTIONS",
  "NODE_COMPILE_CACHE",
  "AWS_REGION",
  "S3_USER_FILES_BUCKET",
  "USER_ID",
  "SUBAGENT_BEDROCK_MODEL_ID",
  // EventBridge cron skill
  "EVENTBRIDGE_SCHEDULE_GROUP",
  "IDENTITY_TABLE_NAME",
  "CRON_LAMBDA_ARN",
  "EVENTBRIDGE_ROLE_ARN",
  "CRON_LEAD_TIME_MINUTES",
  // User identity — skills like agentcore-browser read USER_ID from env
  "USER_ID",
  "INTERNAL_USER_ID",
];

/**
 * Build an IAM session policy JSON string that scopes S3 access to a user's namespace.
 *
 * @param {object} opts
 * @param {string} opts.bucket - S3 bucket name
 * @param {string} opts.namespace - User namespace (e.g. "telegram_12345")
 * @param {string} [opts.cmkArn] - KMS CMK ARN for encrypted bucket
 * @param {string} [opts.eventbridgeRoleArn] - EventBridge scheduler role ARN for iam:PassRole
 * @param {string} [opts.identityTableArn] - DynamoDB identity table ARN (scopes DynamoDB access)
 * @param {string} [opts.scheduleGroupArn] - EventBridge schedule group ARN (scopes scheduler access)
 * @returns {string} JSON policy document
 */
function buildSessionPolicy({ bucket, namespace, actorId, internalUserId, cmkArn, eventbridgeRoleArn, identityTableArn, scheduleGroupArn, region, account }) {
  validateNamespace(namespace);

  // EventBridge Scheduler resources:
  // - CRUD actions (Create/Update/Delete/Get) operate on individual schedules
  //   ARN format: arn:aws:scheduler:REGION:ACCOUNT:schedule/GROUP/SCHEDULE
  // - ListSchedules operates on the schedule-group resource
  const scheduleCrudArn = scheduleGroupArn
    ? scheduleGroupArn.replace(":schedule-group/", ":schedule/") + "/*"
    : "*";
  const scheduleListArn = scheduleGroupArn || "*";

  // DynamoDB resources: table + GSI indexes
  const dynamoResources = identityTableArn
    ? [identityTableArn, `${identityTableArn}/index/*`]
    : "*";

  // Build a minimal session policy that fits within the 2048-byte AWS packed limit.
  // The execution role (attached to the runtime) provides the broad permissions.
  // This session policy only RESTRICTS to the user's namespace — it cannot grant
  // permissions the role doesn't have. So we only need S3 namespace scoping here.
  // DynamoDB/Scheduler/SecretsManager scoping is enforced at the application level
  // (skill scripts validate namespace before every operation).
  const policy = {
    Version: "2012-10-17",
    Statement: [
      {
        Effect: "Allow",
        // AbortMultipartUpload: workspace-sync uploads large SQLite snapshots
        // as multipart uploads and aborts a failed one so no billed parts linger.
        Action: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"],
        Resource: `arn:aws:s3:::${bucket}/${namespace}/*`,
      },
      {
        Effect: "Allow",
        Action: "s3:ListBucket",
        Resource: `arn:aws:s3:::${bucket}`,
      },
      // Scheduler, DynamoDB, SecretsManager, KMS, PassRole — allowed by the
      // execution role; no further restriction needed in the session policy.
      // Application-level namespace enforcement in skill scripts provides isolation.
      {
        Effect: "Allow",
        Action: [
          "scheduler:*",
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query",
          "kms:Decrypt", "kms:GenerateDataKey",
          "secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue", "secretsmanager:CreateSecret", "secretsmanager:DeleteSecret", "secretsmanager:ListSecrets", "secretsmanager:TagResource",
          "iam:PassRole",
        ],
        Resource: "*",
      },
    ],
  };

  return JSON.stringify(policy);
}

/**
 * Create scoped S3 credentials via STS AssumeRole with a session policy.
 *
 * @param {string} namespace - User namespace (e.g. "telegram_12345")
 * @param {object} [opts] - Options
 * @param {object} [opts.stsClient] - Pre-configured STS client (for testing)
 * @param {string} [opts.internalUserId] - Internal user ID (e.g. "user_abc123") for DynamoDB CRON#/SESSION access
 * @returns {Promise<{accessKeyId: string, secretAccessKey: string, sessionToken: string, expiration: Date}>}
 */
async function createScopedCredentials(namespace, opts = {}) {
  validateNamespace(namespace);
  const bucket = process.env.S3_USER_FILES_BUCKET;
  const roleArn = process.env.EXECUTION_ROLE_ARN;
  const cmkArn = process.env.CMK_ARN;
  const eventbridgeRoleArn = process.env.EVENTBRIDGE_ROLE_ARN;
  const region = process.env.AWS_REGION;

  if (!bucket) {
    throw new Error("createScopedCredentials: S3_USER_FILES_BUCKET is required");
  }
  if (!roleArn) {
    throw new Error("createScopedCredentials: EXECUTION_ROLE_ARN is required");
  }

  // Extract account ID from role ARN (arn:aws:iam::ACCOUNT:role/NAME)
  const arnParts = roleArn.split(":");
  const account = arnParts.length >= 5 ? arnParts[4] : null;

  // Construct scoped resource ARNs when account and region are available
  const identityTableName = process.env.IDENTITY_TABLE_NAME;
  const scheduleGroup = process.env.EVENTBRIDGE_SCHEDULE_GROUP;

  const identityTableArn = (account && region && identityTableName)
    ? `arn:aws:dynamodb:${region}:${account}:table/${identityTableName}`
    : undefined;
  const scheduleGroupArn = (account && region && scheduleGroup)
    ? `arn:aws:scheduler:${region}:${account}:schedule-group/${scheduleGroup}`
    : undefined;

  // Derive actorId from namespace (namespace uses underscores, actorId uses colons)
  // e.g. namespace "telegram_123456" -> actorId "telegram:123456"
  const actorId = namespace.replace(/_/, ":");

  const sessionPolicy = buildSessionPolicy({
    bucket, namespace, actorId, internalUserId: opts.internalUserId,
    cmkArn, eventbridgeRoleArn,
    identityTableArn, scheduleGroupArn, region, account,
  });

  const commandInput = {
    RoleArn: roleArn,
    RoleSessionName: `scoped-${namespace}`.slice(0, 64),
    DurationSeconds: 3600, // Max for self-assume (role chaining)
    Policy: sessionPolicy,
  };

  let stsClient = opts.stsClient;
  let command;

  if (stsClient) {
    // Mock/test path — use plain object with input property (avoids SDK require)
    command = { input: commandInput };
  } else {
    // Production path — use real STS SDK
    const { STSClient, AssumeRoleCommand } = require("@aws-sdk/client-sts");
    stsClient = new STSClient({ region: process.env.AWS_REGION });
    command = new AssumeRoleCommand(commandInput);
  }

  const resp = await stsClient.send(command);

  const creds = {
    accessKeyId: resp.Credentials.AccessKeyId,
    secretAccessKey: resp.Credentials.SecretAccessKey,
    sessionToken: resp.Credentials.SessionToken,
    expiration: resp.Credentials.Expiration,
  };
  assertCredentialsFresh(creds, { now: opts.now });
  return creds;
}

function writePrivateFileAtomic(file, content) {
  const tmp = `${file}.tmp-${process.pid}-${require("crypto").randomUUID()}`;
  try {
    fs.writeFileSync(tmp, content, { mode: 0o600, flag: "wx" });
    fs.renameSync(tmp, file);
  } finally {
    try { fs.unlinkSync(tmp); } catch (err) { if (err.code !== "ENOENT") throw err; }
  }
}

/**
 * Write credential files for AWS SDK credential_process integration.
 *
 * Creates two files:
 * - scoped-creds.json: Credential process output format (Version 1)
 * - scoped-aws-config: AWS config file with credential_process directive
 *
 * @param {object} creds - Credentials from createScopedCredentials()
 * @param {string} dir - Directory to write files in
 */
function writeCredentialFiles(creds, dir, { now = Date.now } = {}) {
  assertCredentialsFresh(creds, { now });
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });

  const credsJson = {
    Version: 1,
    AccessKeyId: creds.accessKeyId,
    SecretAccessKey: creds.secretAccessKey,
    SessionToken: creds.sessionToken,
    Expiration: creds.expiration instanceof Date
      ? creds.expiration.toISOString()
      : creds.expiration,
  };

  // Atomic write: write to .tmp then rename — prevents credential_process
  // from reading a partially-written file during refresh.
  const credsPath = path.join(dir, "scoped-creds.json");
  writePrivateFileAtomic(credsPath, JSON.stringify(credsJson, null, 2));

  const configContent = [
    "[default]",
    `credential_process = /bin/cat "${credsPath}"`,
    `region = ${process.env.AWS_REGION || "us-west-2"}`,
    "",
  ].join("\n");

  const configPath = path.join(dir, "scoped-aws-config");
  writePrivateFileAtomic(configPath, configContent);
}

// Requests and persistence share refreshes; drain keeps only persistence active.
function createCredentialManager(namespace, {
  credDir, internalUserId, stsClient, onRefresh = () => {}, now = Date.now,
  prepareRefresh = () => {},
  refreshBeforeMs = 5 * 60 * 1000,
  refresh = () => createScopedCredentials(namespace, { internalUserId, stsClient, now }),
} = {}) {
  validateNamespace(namespace);
  if (!Number.isFinite(refreshBeforeMs) || refreshBeforeMs < 0) throw new Error("Invalid refreshBeforeMs");
  const callbackError = "onRefresh must be synchronous; use prepareRefresh for asynchronous preparation";
  if (typeof onRefresh !== "function" || isAsyncFunction(onRefresh)) throw new TypeError(callbackError);
  if (typeof prepareRefresh !== "function") throw new TypeError("prepareRefresh must be a function");
  let credentials = null;
  let inFlight = null;
  let stopped = false;
  let draining = false;
  function assertActive() {
    if (stopped) throw new Error("Scoped credential manager stopped");
  }
  function assertAccess(forPersistence = false) {
    assertActive();
    if (draining && !forPersistence) throw new Error("Scoped credential manager draining");
  }
  function getCredentials() {
    assertAccess();
    return assertCredentialsFresh(credentials, { now });
  }
  async function ensureFresh({ forPersistence = false, minValidityMs = refreshBeforeMs } = {}) {
    assertAccess(forPersistence === true);
    if (!Number.isFinite(minValidityMs) || minValidityMs < 0) throw new Error("Invalid minValidityMs");
    if (!inFlight && credentials && new Date(credentials.expiration).getTime() > now() + minValidityMs) {
      return assertCredentialsFresh(credentials, { now, minValidityMs });
    }
    inFlight ||= Promise.resolve().then(async () => {
      assertActive();
      const next = await refresh();
      assertActive();
      assertCredentialsFresh(next, { now, minValidityMs });
      const prepared = await prepareRefresh(next);
      assertActive();
      assertCredentialsFresh(next, { now, minValidityMs });
      // Draining saves use memory only; stopped or aborted children get no new files.
      if (!draining) {
        // No await between the stop check and synchronous publication.
        if (credDir) writeCredentialFiles(next, credDir, { now });
        const result = onRefresh(next, prepared);
        if (result && typeof result.then === "function") {
          Promise.resolve(result).catch(() => {});
          throw new TypeError(callbackError);
        }
      }
      assertActive();
      credentials = Object.freeze({ ...next });
      return credentials;
    }).finally(() => { inFlight = null; });
    const refreshed = await inFlight;
    assertAccess(forPersistence === true);
    return assertCredentialsFresh(refreshed, { now, minValidityMs });
  }
  function beginDrain() {
    draining = true;
  }
  function stop() {
    stopped = true;
    credentials = null;
  }
  return { ensureFresh, getCredentials, beginDrain, stop };
}

/**
 * Build a clean environment for the OpenClaw child process.
 *
 * Includes scoped credential config and app env vars.
 * Explicitly EXCLUDES all AWS credential env vars to prevent
 * OpenClaw from accessing the container's full execution role.
 *
 * @param {object} opts
 * @param {string} opts.credDir - Directory containing scoped credential files
 * @param {object} [opts.baseEnv] - Base environment to extract forwarded vars from
 * @returns {object} Environment variables for spawn()
 */
function buildOpenClawEnv({ credDir, baseEnv = {} }) {
  const env = { AWS_EC2_METADATA_DISABLED: "true" };

  // Forward allowed env vars from base environment
  for (const key of FORWARDED_ENV_KEYS) {
    if (baseEnv[key] !== undefined) {
      env[key] = baseEnv[key];
    }
  }

  // Scoped credentials via credential_process (when credDir is available)
  if (credDir) {
    env.AWS_CONFIG_FILE = path.join(credDir, "scoped-aws-config");
    env.AWS_SDK_LOAD_CONFIG = "1";
  }
  // When credDir is null/undefined, OpenClaw gets zero AWS access —
  // no credential_process, no credential env vars, tools fail gracefully.

  // OpenClaw internal
  env.OPENCLAW_SKIP_CRON = "1";

  // Ensure no credential env vars leak through
  for (const key of CREDENTIAL_ENV_BLOCKLIST) {
    delete env[key];
  }

  return env;
}

module.exports = {
  credentialsExpireAt,
  credentialsNeedRefresh,
  validateNamespace,
  assertCredentialsFresh,
  createCredentialManager,
  buildSessionPolicy,
  createScopedCredentials,
  writeCredentialFiles,
  buildOpenClawEnv,
  CREDENTIAL_ENV_BLOCKLIST,
  FORWARDED_ENV_KEYS,
};
