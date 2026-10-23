/**
 * Scoped Credentials — Per-user IAM scoping via STS session policies.
 *
 * Creates STS AssumeRole session credentials for the OpenClaw process whose
 * AWS access (S3, Secrets Manager, DynamoDB, EventBridge Scheduler) is limited
 * to a single user's resources: the user's S3 prefix, the user's
 * openclaw/user/<namespace>/ secrets, the user's USER#<id> identity-table
 * partition and the user's openclaw-<namespace>- schedules.
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

// EventBridge schedule names are "openclaw-<namespace>-<8 hex id>", capped at
// 64 characters. A namespace longer than 46 characters is named
// "openclaw-<first 33>-<12-hex sha256 of the namespace>-<id>", the same rule as
// buildScheduleName in skills/eventbridge-cron/common.js. The session policy
// matches that prefix only. The pre-digest name of a long namespace
// ("openclaw-<first 46>-<id>") is not granted: two namespaces that share their
// first 46 characters share it.
const SCHEDULE_NAME_PREFIX = "openclaw-";
const SCHEDULE_NAMESPACE_MAX = 64 - SCHEDULE_NAME_PREFIX.length - "-00000000".length;

function scheduleNamePrefix(namespace) {
  if (namespace.length <= SCHEDULE_NAMESPACE_MAX) return `${SCHEDULE_NAME_PREFIX}${namespace}-`;
  const digest = require("crypto").createHash("sha256").update(namespace).digest("hex").slice(0, 12);
  return `${SCHEDULE_NAME_PREFIX}${namespace.slice(0, SCHEDULE_NAMESPACE_MAX - 13)}-${digest}-`;
}

// Internal user IDs ("user_<hex>") are placed in an IAM condition value, so
// they must not carry IAM wildcards or JSON metacharacters.
const VALID_INTERNAL_USER_ID = /^[a-zA-Z0-9_-]{1,128}$/;

// AWS rejects a session policy whose plain-text JSON exceeds 2048 characters.
const MAX_SESSION_POLICY_LENGTH = 2048;

/**
 * Build the IAM session policy for one user's scoped credentials.
 *
 * The effective permissions are the intersection of this policy and the
 * execution role, so every statement names only the resources that belong to
 * this user. A statement whose resource cannot be named (missing table, group
 * or role) is left out rather than widened.
 *
 * @param {object} opts
 * @param {string} opts.bucket - S3 bucket name
 * @param {string} opts.namespace - User namespace (e.g. "telegram_12345")
 * @param {string} [opts.internalUserId] - Internal user ID ("user_<hex>"); scopes DynamoDB to USER#<id>
 * @param {string} [opts.eventbridgeRoleArn] - EventBridge scheduler role ARN for iam:PassRole
 * @param {string} [opts.identityTableArn] - DynamoDB identity table ARN
 * @param {string} [opts.scheduleGroupArn] - EventBridge schedule group ARN
 * @param {string} [opts.region] - AWS region
 * @param {string} [opts.account] - AWS account ID
 * @returns {string} JSON policy document
 */
function buildSessionPolicy({ bucket, namespace, internalUserId, eventbridgeRoleArn, identityTableArn, scheduleGroupArn, region, account }) {
  validateNamespace(namespace);
  if (internalUserId !== undefined && internalUserId !== null &&
      (typeof internalUserId !== "string" || !VALID_INTERNAL_USER_ID.test(internalUserId))) {
    throw new Error("Invalid internalUserId for session policy");
  }

  const statements = [
    {
      Effect: "Allow",
      // AbortMultipartUpload: workspace-sync uploads large SQLite snapshots
      // as multipart uploads and aborts a failed one so no billed parts linger.
      Action: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"],
      Resource: `arn:aws:s3:::${bucket}/${namespace}/*`,
    },
    {
      // Listing is limited to the user's own prefix (skills and workspace-sync
      // always list "<namespace>/..."). A listing without a prefix is refused.
      Effect: "Allow",
      Action: "s3:ListBucket",
      Resource: `arn:aws:s3:::${bucket}`,
      Condition: { StringLike: { "s3:prefix": `${namespace}/*` } },
    },
  ];

  if (region && account) {
    // api-keys skill: secrets live at openclaw/user/<namespace>/<key>. The
    // trailing "/" keeps telegram_1 from matching telegram_12's secrets.
    statements.push({
      Effect: "Allow",
      Action: [
        "secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue", "secretsmanager:CreateSecret",
        "secretsmanager:DeleteSecret", "secretsmanager:TagResource",
      ],
      Resource: `arn:aws:secretsmanager:${region}:${account}:secret:openclaw/user/${namespace}/*`,
    });
  }
  // ListSecrets has no resource-level permissions. The api-keys skill uses it
  // (prefix-filtered) for `list` and the per-user secret limit; it returns
  // secret names and metadata, never values.
  statements.push({ Effect: "Allow", Action: "secretsmanager:ListSecrets", Resource: "*" });

  if (identityTableArn && internalUserId) {
    // eventbridge-cron skill: CRON# items under the user's own partition only.
    // Every granted action names a partition key, so LeadingKeys is always set.
    statements.push({
      Effect: "Allow",
      Action: ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query"],
      Resource: identityTableArn,
      Condition: { "ForAllValues:StringEquals": { "dynamodb:LeadingKeys": [`USER#${internalUserId}`] } },
    });
  }

  if (scheduleGroupArn) {
    // eventbridge-cron skill: only schedules named for this user.
    statements.push({
      Effect: "Allow",
      Action: ["scheduler:CreateSchedule", "scheduler:GetSchedule", "scheduler:UpdateSchedule", "scheduler:DeleteSchedule"],
      Resource: `${scheduleGroupArn.replace(":schedule-group/", ":schedule/")}/${scheduleNamePrefix(namespace)}*`,
    });
  }

  if (eventbridgeRoleArn) {
    statements.push({
      Effect: "Allow",
      Action: "iam:PassRole",
      Resource: eventbridgeRoleArn,
      Condition: { StringEquals: { "iam:PassedToService": "scheduler.amazonaws.com" } },
    });
  }

  // KMS only on behalf of the services above (S3 objects, the identity table
  // and Secrets Manager); no direct KMS API calls.
  statements.push({
    Effect: "Allow",
    Action: ["kms:Decrypt", "kms:GenerateDataKey"],
    Resource: "*",
    Condition: {
      StringLike: {
        "kms:ViaService": ["s3", "dynamodb", "secretsmanager"].map((s) => `${s}.${region || "*"}.amazonaws.com`),
      },
    },
  });

  const policy = JSON.stringify({ Version: "2012-10-17", Statement: statements });
  if (policy.length > MAX_SESSION_POLICY_LENGTH) {
    throw new Error(`Session policy is ${policy.length} characters; the limit is ${MAX_SESSION_POLICY_LENGTH}`);
  }
  return policy;
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

  const sessionPolicy = buildSessionPolicy({
    bucket, namespace, internalUserId: opts.internalUserId, eventbridgeRoleArn,
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
  // How close the session policy is to the STS packed-size limit (percent).
  if (Number.isFinite(resp.PackedPolicySize)) {
    console.log("[scoped-creds] session policy packed size %d%%", resp.PackedPolicySize);
  }

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
