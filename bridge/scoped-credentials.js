"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { canonicalNamespace } = require("./session-binding");

const RUNTIME_REGION = "eu-west-1";
const VALID_BUCKET = /^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/;
const ROLE_ARN_PATTERN = /^arn:aws:iam::(\d{12}):role\/[A-Za-z0-9_+=,.@\/-]{1,512}$/;
const CMK_ARN_PATTERN = /^arn:aws:kms:([^:]+):(\d{12}):key\/([A-Za-z0-9-]+)$/;

const FORWARDED_ENV_KEYS = Object.freeze(["S3_USER_FILES_BUCKET"]);
const FIXED_CHILD_ENV = Object.freeze({
  PATH: "/usr/local/bin:/usr/bin:/bin",
  HOME: "/root",
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

function parseCmkArn(cmkArn, callerAccount) {
  const match = typeof cmkArn === "string" ? cmkArn.match(CMK_ARN_PATTERN) : null;
  if (!match || match[1] !== RUNTIME_REGION) {
    throw new Error(`CMK ARN must name an exact key in ${RUNTIME_REGION}`);
  }
  const account = match[2];
  if (callerAccount !== undefined && callerAccount !== account) {
    throw new Error("CMK account must equal the caller account");
  }
  return { account };
}

function buildSessionPolicy({
  bucket,
  namespace,
  cmkArn,
  callerAccount,
  region,
} = {}) {
  const validatedBucket = validateBucket(bucket);
  const canonical = canonicalNamespace(namespace);
  if (region !== undefined && region !== RUNTIME_REGION) {
    throw new Error(`Session policy region must be ${RUNTIME_REGION}`);
  }

  const statements = [
    {
      Effect: "Allow",
      Action: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      Resource: `arn:aws:s3:::${validatedBucket}/${canonical}/*`,
    },
    {
      Effect: "Allow",
      Action: "s3:ListBucket",
      Resource: `arn:aws:s3:::${validatedBucket}`,
      Condition: { StringLike: { "s3:prefix": `${canonical}/*` } },
    },
  ];

  if (cmkArn !== undefined) {
    const { account } = parseCmkArn(cmkArn, callerAccount);
    statements.push({
      Effect: "Allow",
      Action: ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"],
      Resource: cmkArn,
      Condition: {
        StringEquals: {
          "kms:ViaService": `s3.${RUNTIME_REGION}.amazonaws.com`,
          "kms:CallerAccount": account,
        },
      },
    });
  }

  return JSON.stringify({ Version: "2012-10-17", Statement: statements });
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
  return {
    accessKeyId: credentials.AccessKeyId,
    secretAccessKey: credentials.SecretAccessKey,
    sessionToken: credentials.SessionToken,
    expiration,
  };
}

async function createScopedCredentials(namespace, options = {}) {
  const env = options.env || process.env;
  const region = requireExactRegion(env);
  const canonical = canonicalNamespace(namespace);
  const bucket = env.S3_USER_FILES_BUCKET;
  const roleArn = env.WORKSPACE_SESSION_ROLE_ARN;
  const cmkArn = env.CMK_ARN;

  if (!bucket) throw new Error("S3_USER_FILES_BUCKET is required");
  if (!roleArn) throw new Error("WORKSPACE_SESSION_ROLE_ARN is required");
  if (!cmkArn) throw new Error("CMK_ARN is required");
  validateBucket(bucket);

  const roleMatch = roleArn.match(ROLE_ARN_PATTERN);
  if (!roleMatch) throw new Error("WORKSPACE_SESSION_ROLE_ARN is invalid");
  const callerAccount = roleMatch[1];
  parseCmkArn(cmkArn, callerAccount);

  const hashSuffix = createHash("sha256")
    .update(canonical)
    .digest("hex")
    .slice(0, 12);
  const readable = canonical.slice(0, 41);
  const commandInput = {
    RoleArn: roleArn,
    RoleSessionName: `workspace-${readable}-${hashSuffix}`,
    DurationSeconds: 3600,
    Policy: buildSessionPolicy({
      bucket,
      namespace: canonical,
      cmkArn,
      callerAccount,
      region,
    }),
  };

  let stsClient = options.stsClient;
  let command;
  if (stsClient) {
    command = { input: commandInput };
  } else {
    const { STSClient, AssumeRoleCommand } = require("@aws-sdk/client-sts");
    stsClient = new STSClient({ region });
    command = new AssumeRoleCommand(commandInput);
  }
  const response = await stsClient.send(command);
  return validateTemporaryCredentials(response?.Credentials);
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
  buildSessionPolicy,
  createScopedCredentials,
  writeCredentialFiles,
  buildOpenClawEnv,
  validateTemporaryCredentials,
};
