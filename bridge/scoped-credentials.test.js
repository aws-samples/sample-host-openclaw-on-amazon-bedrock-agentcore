/**
 * Tests for scoped-credentials.js — STS session-scoped S3 credentials.
 *
 * Covers: buildSessionPolicy, createScopedCredentials, writeCredentialFiles,
 *         buildOpenClawEnv, credential refresh lifecycle.
 * Run: cd bridge && node --test scoped-credentials.test.js
 */
const { describe, it, beforeEach, afterEach, mock } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const os = require("os");

// --- buildSessionPolicy ---

describe("buildSessionPolicy", () => {
  let buildSessionPolicy;

  beforeEach(() => {
    ({ buildSessionPolicy } = require("./scoped-credentials"));
  });

  it("returns valid JSON policy with S3 scoped to namespace", () => {
    const policy = buildSessionPolicy({
      bucket: "openclaw-user-files-123-us-west-2",
      namespace: "telegram_12345",
      cmkArn: "arn:aws:kms:us-west-2:123:key/abc-123",
    });

    const parsed = JSON.parse(policy);
    assert.equal(parsed.Version, "2012-10-17");
    assert.ok(Array.isArray(parsed.Statement));
    assert.equal(parsed.Statement.length, 3, "should have 3 statements (S3 object, S3 list, services)");
  });

  it("S3 object actions scoped to namespace/* only", () => {
    const policy = buildSessionPolicy({
      bucket: "my-bucket",
      namespace: "slack_U0ABC",
      cmkArn: "arn:aws:kms:us-west-2:123:key/abc-123",
    });

    const parsed = JSON.parse(policy);
    const objectStmt = parsed.Statement.find(
      (s) => s.Action && (Array.isArray(s.Action) ? s.Action.includes("s3:GetObject") : s.Action === "s3:GetObject*")
    );
    assert.ok(objectStmt, "should have S3 object statement");

    // Resource should include namespace-scoped object ARN
    const resources = Array.isArray(objectStmt.Resource)
      ? objectStmt.Resource
      : [objectStmt.Resource];
    assert.ok(
      resources.some((r) => r.includes("slack_U0ABC")),
      `at least one resource should contain namespace, got: ${resources}`,
    );
    // Must NOT grant access to the whole bucket with /*
    assert.ok(
      !resources.some((r) => r.endsWith("/*") && !r.includes("slack_U0ABC")),
      "should not grant bucket-wide access",
    );
  });

  it("ListBucket included in S3 statement without Condition block", () => {
    const policy = buildSessionPolicy({
      bucket: "my-bucket",
      namespace: "telegram_99999",
      cmkArn: "arn:aws:kms:us-west-2:123:key/abc-123",
    });

    const parsed = JSON.parse(policy);
    const listStmt = parsed.Statement.find(
      (s) => s.Action && (Array.isArray(s.Action) ? s.Action.includes("s3:ListBucket") : s.Action === "s3:ListBucket")
    );
    assert.ok(listStmt, "should have ListBucket statement");
    assert.equal(listStmt.Condition, undefined, "ListBucket should have no Condition block");
  });

  it("includes KMS decrypt permission always in statement 1 with Resource *", () => {
    const policy = buildSessionPolicy({
      bucket: "my-bucket",
      namespace: "telegram_12345",
      cmkArn: "arn:aws:kms:us-west-2:123:key/abc-123",
    });

    const parsed = JSON.parse(policy);
    const kmsStmt = parsed.Statement.find(
      (s) =>
        s.Action &&
        (Array.isArray(s.Action)
          ? s.Action.some((a) => a.startsWith("kms:"))
          : s.Action.startsWith("kms:")),
    );
    assert.ok(kmsStmt, "should have KMS actions");
    assert.equal(kmsStmt.Resource, "*", "KMS resource should be wildcard");
  });

  it("includes KMS even when cmkArn not provided", () => {
    const policy = buildSessionPolicy({
      bucket: "my-bucket",
      namespace: "telegram_12345",
    });

    const parsed = JSON.parse(policy);
    const kmsStmt = parsed.Statement.find(
      (s) =>
        s.Action &&
        (Array.isArray(s.Action)
          ? s.Action.some((a) => a.startsWith("kms:"))
          : s.Action.startsWith("kms:")),
    );
    assert.ok(kmsStmt, "KMS actions should be present even without cmkArn");
    assert.equal(kmsStmt.Resource, "*", "KMS resource should be wildcard");
  });

  it("includes iam:PassRole always in statement 1 with Resource *", () => {
    const policy = buildSessionPolicy({
      bucket: "my-bucket",
      namespace: "telegram_12345",
      cmkArn: "arn:aws:kms:us-west-2:123:key/abc-123",
      eventbridgeRoleArn: "arn:aws:iam::123:role/openclaw-cron-scheduler-role",
    });

    const parsed = JSON.parse(policy);
    const passRoleStmt = parsed.Statement.find(
      (s) => Array.isArray(s.Action) && s.Action.includes("iam:PassRole"),
    );
    assert.ok(passRoleStmt, "should have PassRole action");
    assert.equal(passRoleStmt.Resource, "*", "PassRole resource should be wildcard");
  });

  it("includes iam:PassRole even when eventbridgeRoleArn not provided", () => {
    const policy = buildSessionPolicy({
      bucket: "my-bucket",
      namespace: "telegram_12345",
      cmkArn: "arn:aws:kms:us-west-2:123:key/abc-123",
    });

    const parsed = JSON.parse(policy);
    const passRoleStmt = parsed.Statement.find(
      (s) => Array.isArray(s.Action) && s.Action.includes("iam:PassRole"),
    );
    assert.ok(passRoleStmt, "PassRole should be present even without eventbridgeRoleArn");
    assert.equal(passRoleStmt.Resource, "*", "PassRole resource should be wildcard");
  });

  it("rejects namespace with path traversal", () => {
    assert.throws(
      () =>
        buildSessionPolicy({
          bucket: "my-bucket",
          namespace: "../other-user",
          cmkArn: "arn:aws:kms:us-west-2:123:key/abc-123",
        }),
      /invalid namespace/i,
    );
  });

  it("rejects empty namespace", () => {
    assert.throws(
      () =>
        buildSessionPolicy({
          bucket: "my-bucket",
          namespace: "",
          cmkArn: "arn:aws:kms:us-west-2:123:key/abc-123",
        }),
      /invalid namespace/i,
    );
  });

  it("includes DynamoDB actions in statement 1 with Resource *", () => {
    const policy = buildSessionPolicy({
      bucket: "my-bucket",
      namespace: "telegram_12345",
      identityTableArn: "arn:aws:dynamodb:us-west-2:123456789012:table/openclaw-identity",
    });

    const parsed = JSON.parse(policy);
    const dynamoStmt = parsed.Statement.find((s) => Array.isArray(s.Action) && s.Action.includes("dynamodb:GetItem"));
    assert.ok(dynamoStmt, "should have DynamoDB actions");
    assert.equal(dynamoStmt.Resource, "*", "DynamoDB resource should be wildcard");
  });

  it("includes DynamoDB actions even when identityTableArn not provided", () => {
    const policy = buildSessionPolicy({
      bucket: "my-bucket",
      namespace: "telegram_12345",
    });

    const parsed = JSON.parse(policy);
    const dynamoStmt = parsed.Statement.find((s) => Array.isArray(s.Action) && s.Action.includes("dynamodb:GetItem"));
    assert.ok(dynamoStmt, "should have DynamoDB actions");
    assert.equal(dynamoStmt.Resource, "*", "DynamoDB resource should be wildcard");
  });

  it("DynamoDB has no Condition block regardless of parameters", () => {
    const policy = buildSessionPolicy({
      bucket: "my-bucket",
      namespace: "telegram_12345",
      actorId: "telegram:12345",
      internalUserId: "user_abc123",
      identityTableArn: "arn:aws:dynamodb:us-west-2:123456789012:table/openclaw-identity",
    });

    const parsed = JSON.parse(policy);
    const dynamoStmt = parsed.Statement.find((s) => Array.isArray(s.Action) && s.Action.includes("dynamodb:GetItem"));
    assert.ok(dynamoStmt, "should have DynamoDB actions");
    assert.equal(dynamoStmt.Condition, undefined, "DynamoDB should have no Condition block");
  });

  it("no Condition block even when actorId equals internalUserId", () => {
    const policy = buildSessionPolicy({
      bucket: "my-bucket",
      namespace: "telegram_12345",
      actorId: "telegram:12345",
      internalUserId: "telegram:12345",
    });

    const parsed = JSON.parse(policy);
    const dynamoStmt = parsed.Statement.find((s) => Array.isArray(s.Action) && s.Action.includes("dynamodb:GetItem"));
    assert.equal(dynamoStmt.Condition, undefined, "DynamoDB should have no Condition block");
  });

  it("includes scheduler:* in statement 1 with Resource *", () => {
    const policy = buildSessionPolicy({
      bucket: "my-bucket",
      namespace: "telegram_12345",
      scheduleGroupArn: "arn:aws:scheduler:us-west-2:123456789012:schedule-group/openclaw-cron",
    });

    const parsed = JSON.parse(policy);
    const ebStmt = parsed.Statement.find((s) => Array.isArray(s.Action) && s.Action.includes("scheduler:*"));
    assert.ok(ebStmt, "should have scheduler:* action");
    assert.equal(ebStmt.Resource, "*", "scheduler resource should be wildcard");
  });

  it("scheduler:* always present regardless of scheduleGroupArn", () => {
    const policy = buildSessionPolicy({
      bucket: "my-bucket",
      namespace: "telegram_12345",
    });

    const parsed = JSON.parse(policy);
    const ebStmt = parsed.Statement.find((s) => Array.isArray(s.Action) && s.Action.includes("scheduler:*"));
    assert.ok(ebStmt, "should have scheduler:* action even without scheduleGroupArn");
    assert.equal(ebStmt.Resource, "*", "scheduler resource should be wildcard");
  });
});

// --- createScopedCredentials ---

describe("createScopedCredentials", () => {
  let createScopedCredentials, _mockStsClient;

  const MOCK_CREDS = {
    AccessKeyId: "AKIAIOSFODNN7EXAMPLE",
    SecretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    SessionToken: "FwoGZXIvYXdzEBYaDH...",
    Expiration: new Date(Date.now() + 3600000),
  };

  beforeEach(() => {
    // Re-require to get fresh module with mock injection
    delete require.cache[require.resolve("./scoped-credentials")];

    // Mock STS client
    _mockStsClient = {
      send: mock.fn(async () => ({
        Credentials: { ...MOCK_CREDS },
      })),
    };

    // Set required env vars
    process.env.S3_USER_FILES_BUCKET = "test-bucket";
    process.env.AWS_REGION = "us-west-2";
    process.env.EXECUTION_ROLE_ARN =
      "arn:aws:iam::123456789012:role/openclaw-agentcore-execution-role";

    ({ createScopedCredentials } = require("./scoped-credentials"));
  });

  afterEach(() => {
    delete process.env.S3_USER_FILES_BUCKET;
    delete process.env.EXECUTION_ROLE_ARN;
  });

  it("calls STS AssumeRole with session policy", async () => {
    const creds = await createScopedCredentials("telegram_12345", {
      stsClient: _mockStsClient,
    });

    assert.equal(_mockStsClient.send.mock.calls.length, 1);
    const input = _mockStsClient.send.mock.calls[0].arguments[0].input;
    assert.ok(input.RoleArn, "should include RoleArn");
    assert.ok(input.RoleSessionName, "should include RoleSessionName");
    assert.ok(input.Policy, "should include session Policy");

    // Verify session policy is namespace-scoped
    const policy = JSON.parse(input.Policy);
    const resourceStr = JSON.stringify(policy);
    assert.ok(
      resourceStr.includes("telegram_12345"),
      "policy should scope to namespace",
    );
  });

  it("returns credentials object with required fields", async () => {
    const creds = await createScopedCredentials("telegram_12345", {
      stsClient: _mockStsClient,
    });

    assert.ok(creds.accessKeyId, "should have accessKeyId");
    assert.ok(creds.secretAccessKey, "should have secretAccessKey");
    assert.ok(creds.sessionToken, "should have sessionToken");
    assert.ok(creds.expiration, "should have expiration");
  });

  it("sets RoleSessionName with namespace", async () => {
    await createScopedCredentials("slack_U0ABC", {
      stsClient: _mockStsClient,
    });

    const input = _mockStsClient.send.mock.calls[0].arguments[0].input;
    assert.ok(
      input.RoleSessionName.includes("slack_U0ABC"),
      `RoleSessionName should contain namespace, got: ${input.RoleSessionName}`,
    );
  });

  it("sets DurationSeconds to 3600 (max for self-assume)", async () => {
    await createScopedCredentials("telegram_12345", {
      stsClient: _mockStsClient,
    });

    const input = _mockStsClient.send.mock.calls[0].arguments[0].input;
    assert.equal(input.DurationSeconds, 3600);
  });

  it("includes DynamoDB and scheduler actions with Resource * regardless of env vars", async () => {
    process.env.IDENTITY_TABLE_NAME = "openclaw-identity";
    process.env.EVENTBRIDGE_SCHEDULE_GROUP = "openclaw-cron";

    await createScopedCredentials("telegram_12345", {
      stsClient: _mockStsClient,
    });

    const input = _mockStsClient.send.mock.calls[0].arguments[0].input;
    const policy = JSON.parse(input.Policy);

    // DynamoDB should be in statement 1 with Resource "*"
    const dynamoStmt = policy.Statement.find((s) => Array.isArray(s.Action) && s.Action.includes("dynamodb:GetItem"));
    assert.ok(dynamoStmt, "should have DynamoDB actions");
    assert.equal(dynamoStmt.Resource, "*", "DynamoDB resource should be wildcard");

    // Scheduler should be in statement 1 with Resource "*"
    const ebStmt = policy.Statement.find((s) => Array.isArray(s.Action) && s.Action.includes("scheduler:*"));
    assert.ok(ebStmt, "should have scheduler actions");
    assert.equal(ebStmt.Resource, "*", "scheduler resource should be wildcard");

    delete process.env.IDENTITY_TABLE_NAME;
    delete process.env.EVENTBRIDGE_SCHEDULE_GROUP;
  });

  it("throws when S3_USER_FILES_BUCKET is missing", async () => {
    delete process.env.S3_USER_FILES_BUCKET;
    await assert.rejects(
      () => createScopedCredentials("telegram_12345", { stsClient: _mockStsClient }),
      /S3_USER_FILES_BUCKET/,
    );
  });

  it("throws when EXECUTION_ROLE_ARN is missing", async () => {
    delete process.env.EXECUTION_ROLE_ARN;
    await assert.rejects(
      () => createScopedCredentials("telegram_12345", { stsClient: _mockStsClient }),
      /EXECUTION_ROLE_ARN/,
    );
  });
});

// --- writeCredentialFiles ---

describe("writeCredentialFiles", () => {
  let writeCredentialFiles;
  let tmpDir;

  const CREDS = {
    accessKeyId: "AKIAIOSFODNN7EXAMPLE",
    secretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    sessionToken: "FwoGZXIvYXdzEBYaDH...",
    expiration: new Date(Date.now() + 3600000),
  };

  beforeEach(() => {
    delete require.cache[require.resolve("./scoped-credentials")];
    ({ writeCredentialFiles } = require("./scoped-credentials"));
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scoped-creds-test-"));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it("writes scoped-creds.json in credential_process format", () => {
    writeCredentialFiles(CREDS, tmpDir);

    const credsPath = path.join(tmpDir, "scoped-creds.json");
    assert.ok(fs.existsSync(credsPath), "scoped-creds.json should exist");

    const content = JSON.parse(fs.readFileSync(credsPath, "utf8"));
    assert.equal(content.Version, 1);
    assert.equal(content.AccessKeyId, CREDS.accessKeyId);
    assert.equal(content.SecretAccessKey, CREDS.secretAccessKey);
    assert.equal(content.SessionToken, CREDS.sessionToken);
    assert.ok(content.Expiration, "should have Expiration");
  });

  it("writes scoped-aws-config with credential_process", () => {
    writeCredentialFiles(CREDS, tmpDir);

    const configPath = path.join(tmpDir, "scoped-aws-config");
    assert.ok(fs.existsSync(configPath), "scoped-aws-config should exist");

    const content = fs.readFileSync(configPath, "utf8");
    assert.ok(content.includes("[default]"), "should have [default] profile");
    assert.ok(
      content.includes("credential_process"),
      "should have credential_process",
    );
    assert.ok(
      content.includes("scoped-creds.json"),
      "should reference scoped-creds.json",
    );
  });

  it("overwrites existing files on refresh", () => {
    writeCredentialFiles(CREDS, tmpDir);

    const updatedCreds = {
      ...CREDS,
      accessKeyId: "AKIAI_REFRESHED",
      expiration: new Date(Date.now() + 7200000),
    };
    writeCredentialFiles(updatedCreds, tmpDir);

    const credsPath = path.join(tmpDir, "scoped-creds.json");
    const content = JSON.parse(fs.readFileSync(credsPath, "utf8"));
    assert.equal(content.AccessKeyId, "AKIAI_REFRESHED");
  });

  it("sets restrictive file permissions on credentials file", () => {
    writeCredentialFiles(CREDS, tmpDir);

    const credsPath = path.join(tmpDir, "scoped-creds.json");
    const stat = fs.statSync(credsPath);
    // Owner read+write only (0o600)
    const mode = stat.mode & 0o777;
    assert.equal(mode, 0o600, `expected 0600, got 0${mode.toString(8)}`);
  });

  it("uses atomic writes (no .tmp files left behind)", () => {
    writeCredentialFiles(CREDS, tmpDir);

    // After a successful write, no .tmp files should remain
    const files = fs.readdirSync(tmpDir);
    const tmpFiles = files.filter((f) => f.endsWith(".tmp"));
    assert.equal(tmpFiles.length, 0, `no .tmp files should remain, got: ${tmpFiles}`);

    // Both final files should exist
    assert.ok(files.includes("scoped-creds.json"), "scoped-creds.json should exist");
    assert.ok(files.includes("scoped-aws-config"), "scoped-aws-config should exist");
  });
});

// --- buildOpenClawEnv ---

describe("buildOpenClawEnv", () => {
  let buildOpenClawEnv;

  beforeEach(() => {
    delete require.cache[require.resolve("./scoped-credentials")];
    ({ buildOpenClawEnv } = require("./scoped-credentials"));
  });

  it("includes AWS_CONFIG_FILE pointing to scoped config", () => {
    const env = buildOpenClawEnv({ credDir: "/tmp/scoped" });
    assert.equal(env.AWS_CONFIG_FILE, "/tmp/scoped/scoped-aws-config");
  });

  it("includes AWS_SDK_LOAD_CONFIG=1", () => {
    const env = buildOpenClawEnv({ credDir: "/tmp/scoped" });
    assert.equal(env.AWS_SDK_LOAD_CONFIG, "1");
  });

  it("does NOT include AWS_ACCESS_KEY_ID", () => {
    const env = buildOpenClawEnv({ credDir: "/tmp/scoped" });
    assert.equal(env.AWS_ACCESS_KEY_ID, undefined);
  });

  it("does NOT include AWS_SECRET_ACCESS_KEY", () => {
    const env = buildOpenClawEnv({ credDir: "/tmp/scoped" });
    assert.equal(env.AWS_SECRET_ACCESS_KEY, undefined);
  });

  it("does NOT include AWS_SESSION_TOKEN", () => {
    const env = buildOpenClawEnv({ credDir: "/tmp/scoped" });
    assert.equal(env.AWS_SESSION_TOKEN, undefined);
  });

  it("does NOT include AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", () => {
    const env = buildOpenClawEnv({ credDir: "/tmp/scoped" });
    assert.equal(env.AWS_CONTAINER_CREDENTIALS_RELATIVE_URI, undefined);
  });

  it("does NOT include AWS_CONTAINER_CREDENTIALS_FULL_URI", () => {
    const env = buildOpenClawEnv({ credDir: "/tmp/scoped" });
    assert.equal(env.AWS_CONTAINER_CREDENTIALS_FULL_URI, undefined);
  });

  it("includes standard env vars and the image compile cache", () => {
    const env = buildOpenClawEnv({
      credDir: "/tmp/scoped",
      baseEnv: {
        PATH: "/usr/bin",
        HOME: "/root",
        NODE_OPTIONS: "--dns-result-order=ipv4first",
        NODE_COMPILE_CACHE: "/app/.compile-cache",
        AWS_REGION: "us-west-2",
      },
    });
    assert.equal(env.PATH, "/usr/bin");
    assert.equal(env.HOME, "/root");
    assert.equal(env.NODE_COMPILE_CACHE, "/app/.compile-cache");
    assert.equal(env.AWS_EC2_METADATA_DISABLED, "true");
    assert.equal(env.AWS_REGION, "us-west-2");
  });

  it("includes S3_USER_FILES_BUCKET from baseEnv", () => {
    const env = buildOpenClawEnv({
      credDir: "/tmp/scoped",
      baseEnv: { S3_USER_FILES_BUCKET: "my-bucket" },
    });
    assert.equal(env.S3_USER_FILES_BUCKET, "my-bucket");
  });

  it("includes EventBridge cron env vars from baseEnv", () => {
    const env = buildOpenClawEnv({
      credDir: "/tmp/scoped",
      baseEnv: {
        EVENTBRIDGE_SCHEDULE_GROUP: "openclaw-cron",
        IDENTITY_TABLE_NAME: "openclaw-identity",
        CRON_LAMBDA_ARN: "arn:aws:lambda:us-west-2:123:function:cron",
        EVENTBRIDGE_ROLE_ARN: "arn:aws:iam::123:role/scheduler",
        CRON_LEAD_TIME_MINUTES: "5",
      },
    });
    assert.equal(env.EVENTBRIDGE_SCHEDULE_GROUP, "openclaw-cron");
    assert.equal(env.IDENTITY_TABLE_NAME, "openclaw-identity");
    assert.equal(env.CRON_LAMBDA_ARN, "arn:aws:lambda:us-west-2:123:function:cron");
    assert.equal(env.EVENTBRIDGE_ROLE_ARN, "arn:aws:iam::123:role/scheduler");
    assert.equal(env.CRON_LEAD_TIME_MINUTES, "5");
  });

  it("includes OPENCLAW_SKIP_CRON=1", () => {
    const env = buildOpenClawEnv({ credDir: "/tmp/scoped" });
    assert.equal(env.OPENCLAW_SKIP_CRON, "1");
  });

  it("includes SUBAGENT_BEDROCK_MODEL_ID from baseEnv", () => {
    const env = buildOpenClawEnv({
      credDir: "/tmp/scoped",
      baseEnv: { SUBAGENT_BEDROCK_MODEL_ID: "global.anthropic.claude-sonnet-4-6-v1" },
    });
    assert.equal(env.SUBAGENT_BEDROCK_MODEL_ID, "global.anthropic.claude-sonnet-4-6-v1");
  });
});

// --- Secrets Manager session policy ---

describe("buildSessionPolicy Secrets Manager", () => {
  let buildSessionPolicy;

  beforeEach(() => {
    ({ buildSessionPolicy } = require("./scoped-credentials"));
  });

  it("includes Secrets Manager actions always in statement 1 with Resource *", () => {
    const policy = buildSessionPolicy({
      bucket: "openclaw-user-files-123-us-west-2",
      namespace: "telegram_12345",
      cmkArn: "arn:aws:kms:us-west-2:123:key/abc-123",
      region: "us-west-2",
      account: "123456789012",
    });

    const parsed = JSON.parse(policy);
    const smStmt = parsed.Statement.find((s) => Array.isArray(s.Action) && s.Action.includes("secretsmanager:GetSecretValue"));
    assert.ok(smStmt, "Secrets Manager actions should exist");
    assert.equal(smStmt.Resource, "*", "Secrets Manager resource should be wildcard");
    assert.ok(smStmt.Action.includes("secretsmanager:GetSecretValue"));
    assert.ok(smStmt.Action.includes("secretsmanager:CreateSecret"));
    assert.ok(smStmt.Action.includes("secretsmanager:DeleteSecret"));
    assert.ok(smStmt.Action.includes("secretsmanager:TagResource"), "TagResource needed for api-keys skill");
  });

  it("includes ListSecrets in the Secrets Manager statement with wildcard resource", () => {
    const policy = buildSessionPolicy({
      bucket: "openclaw-user-files-123-us-west-2",
      namespace: "telegram_12345",
      region: "us-west-2",
      account: "123456789012",
    });

    const parsed = JSON.parse(policy);
    const smStmt = parsed.Statement.find((s) => Array.isArray(s.Action) && s.Action.includes("secretsmanager:ListSecrets"));
    assert.ok(smStmt, "Secrets Manager statement with ListSecrets should exist");
    const resources = Array.isArray(smStmt.Resource) ? smStmt.Resource : [smStmt.Resource];
    assert.ok(resources.includes("*"), "Resource array should include wildcard for ListSecrets");
  });

  it("includes Secrets Manager actions even when region/account not provided", () => {
    const policy = buildSessionPolicy({
      bucket: "openclaw-user-files-123-us-west-2",
      namespace: "telegram_12345",
    });

    const parsed = JSON.parse(policy);
    const smStmt = parsed.Statement.find((s) => Array.isArray(s.Action) && s.Action.includes("secretsmanager:GetSecretValue"));
    assert.ok(smStmt, "Secrets Manager actions should exist even without region/account");
    assert.equal(smStmt.Resource, "*", "Secrets Manager resource should be wildcard");
  });

  it("Secrets Manager resource is always wildcard regardless of namespace", () => {
    const policy = buildSessionPolicy({
      bucket: "test-bucket",
      namespace: "slack_abc-def",
      region: "ap-southeast-2",
      account: "999888777666",
    });

    const parsed = JSON.parse(policy);
    const smStmt = parsed.Statement.find((s) => Array.isArray(s.Action) && s.Action.includes("secretsmanager:GetSecretValue"));
    assert.ok(smStmt, "Secrets Manager actions should exist");
    assert.equal(smStmt.Resource, "*", "Secrets Manager resource should be wildcard");
  });
});

describe("expiry-aware scoped credentials", () => {
  let dir, now, calls, refresh, published, manager, scoped, savedModule;
  const modulePath = require.resolve("./scoped-credentials");
  const creds = (key = "new", lifetime = 3600000) => ({
    accessKeyId: key, secretAccessKey: "secret", sessionToken: "token", expiration: new Date(now + lifetime),
  });
  beforeEach(() => {
    savedModule = require.cache[modulePath];
    delete require.cache[modulePath];
    scoped = require("./scoped-credentials");
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "creds-v2-"));
    now = Date.now();
    calls = 0;
    published = [];
    refresh = async () => creds(`key-${++calls}`);
  });
  afterEach(() => {
    try {
      manager?.stop();
    } finally {
      manager = null;
      mock.restoreAll();
      if (savedModule) require.cache[modulePath] = savedModule; else delete require.cache[modulePath];
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
  const create = (options = {}) => {
    manager = scoped.createCredentialManager("telegram_1", {
      credDir: dir, now: () => now, refresh: () => refresh(), onRefresh: (c) => published.push(c.accessKeyId),
      ...options,
    });
    return manager;
  };

  it("exposes the runtime expiry helpers with an explicit safety margin", () => {
    assert.equal(scoped.credentialsExpireAt(creds()), now + 3600000);
    assert.equal(scoped.credentialsExpireAt({ expiration: "invalid" }), 0);
    assert.equal(scoped.credentialsNeedRefresh(creds("key", 1000), { now, minValidityMs: 2000 }), true);
    assert.equal(scoped.credentialsNeedRefresh(creds(), { now, minValidityMs: 2000 }), false);
  });

  it("does no credential IO before activation and refreshes after a wall-clock jump", async () => {
    create();
    assert.equal(calls, 0);
    assert.deepEqual(fs.readdirSync(dir), []);
    await manager.ensureFresh();
    now += 3600001;
    assert.throws(() => manager.getCredentials(), /expired/i);
    await manager.ensureFresh();
    assert.equal(manager.getCredentials().accessKeyId, "key-2");
    assert.deepEqual(published, ["key-1", "key-2"]);
    assert.equal(JSON.parse(fs.readFileSync(path.join(dir, "scoped-creds.json"))).AccessKeyId, "key-2");
  });

  it("coalesces concurrent activation and refresh calls", async () => {
    create();
    let resolve;
    refresh = () => { calls++; return new Promise((r) => { resolve = r; }); };
    const first = manager.ensureFresh();
    const second = manager.ensureFresh();
    await Promise.resolve();
    assert.equal(calls, 1);
    resolve(creds());
    await Promise.all([first, second]);
    assert.equal(published.length, 1);
  });

  it("rejects failed refresh with expired credentials and can retry", async () => {
    create();
    await manager.ensureFresh();
    now += 3600001;
    refresh = async () => { throw new Error("STS unavailable"); };
    await assert.rejects(manager.ensureFresh(), /STS unavailable/);
    assert.throws(() => manager.getCredentials(), /expired/i);
    refresh = async () => creds("recovered");
    await manager.ensureFresh();
    assert.equal(manager.getCredentials().accessKeyId, "recovered");
  });

  it("stopping retires pending refresh before files or callbacks can publish", async () => {
    create();
    let resolve;
    refresh = () => new Promise((r) => { resolve = r; });
    const pending = manager.ensureFresh();
    await Promise.resolve();
    manager.stop();
    resolve(creds());
    await assert.rejects(pending, /stopped/i);
    assert.deepEqual(fs.readdirSync(dir), []);
    assert.deepEqual(published, []);
    await assert.rejects(manager.ensureFresh(), /stopped/i);
  });

  it("does not start a queued refresh after stop", async () => {
    create();
    const pending = manager.ensureFresh();
    manager.stop();
    await assert.rejects(pending, /stopped/i);
    assert.equal(calls, 0);
    assert.deepEqual(published, []);
    assert.deepEqual(fs.readdirSync(dir), []);
  });

  it("does not publish after stop while asynchronous preparation is pending", async () => {
    let started, finish;
    const preparationStarted = new Promise((resolve) => { started = resolve; });
    const prepared = new Promise((resolve) => { finish = resolve; });
    create({
      prepareRefresh: async () => { started(); return prepared; },
      onRefresh: (next, value) => published.push([next.accessKeyId, value]),
    });
    const pending = manager.ensureFresh();
    await Promise.race([preparationStarted, pending]);
    assert.deepEqual(published, []);
    assert.deepEqual(fs.readdirSync(dir), []);
    manager.stop();
    finish("prepared");
    await assert.rejects(pending, /stopped/i);
    assert.deepEqual(published, []);
    assert.deepEqual(fs.readdirSync(dir), []);
  });

  it("drain fences child publication while request and persistence share pending preparation", async () => {
    let started, finish;
    const preparationStarted = new Promise((resolve) => { started = resolve; });
    const prepared = new Promise((resolve) => { finish = resolve; });
    create({ prepareRefresh: async () => { started(); await prepared; } });
    const request = manager.ensureFresh();
    const rejected = assert.rejects(request, /draining/i);
    await preparationStarted;
    const save = manager.ensureFresh({ forPersistence: true, minValidityMs: 60000 });
    manager.beginDrain();
    finish();
    await rejected;
    assert.equal((await save).accessKeyId, "key-1");
    assert.equal(calls, 1);
    assert.deepEqual(published, []);
    assert.deepEqual(fs.readdirSync(dir), []);
    assert.throws(() => manager.getCredentials(), /draining/i);
  });

  for (const acquisitionFails of [false, true]) {
    it(`persistence joins pending acquisition across drain${acquisitionFails ? " and retries a failed acquisition" : ""}`, async () => {
      let finish, reject;
      create();
      refresh = () => {
        calls++;
        return new Promise((resolve, fail) => { finish = resolve; reject = fail; });
      };
      const request = manager.ensureFresh();
      const rejected = assert.rejects(request, acquisitionFails ? /STS unavailable/ : /draining/);
      await Promise.resolve();
      manager.beginDrain();
      const save = manager.ensureFresh({ forPersistence: true, minValidityMs: 60000 });
      if (acquisitionFails) {
        const failedSave = assert.rejects(save, /STS unavailable/);
        reject(new Error("STS unavailable"));
        await Promise.all([rejected, failedSave]);
        refresh = async () => { calls++; return creds("recovered"); };
        assert.equal((await manager.ensureFresh({ forPersistence: true })).accessKeyId, "recovered");
        assert.equal(calls, 2);
      } else {
        finish(creds("draining"));
        assert.equal((await save).accessKeyId, "draining");
        await rejected;
        assert.equal(calls, 1);
      }
      assert.deepEqual(published, []);
      assert.deepEqual(fs.readdirSync(dir), []);
    });
  }

  it("stop fences a draining refresh while asynchronous preparation is pending", async () => {
    let started, finish;
    const preparationStarted = new Promise((resolve) => { started = resolve; });
    const prepared = new Promise((resolve) => { finish = resolve; });
    create({ prepareRefresh: async () => { started(); await prepared; } });
    manager.beginDrain();
    const save = manager.ensureFresh({ forPersistence: true, minValidityMs: 60000 });
    await preparationStarted;
    manager.stop();
    finish();
    await assert.rejects(save, /stopped/i);
    assert.deepEqual(published, []);
    assert.deepEqual(fs.readdirSync(dir), []);
  });

  it("checks each concurrent caller's validity margin after a shared refresh", async () => {
    let finish;
    create();
    refresh = () => {
      calls++;
      return new Promise((resolve) => { finish = resolve; });
    };
    const save = manager.ensureFresh({ forPersistence: true, minValidityMs: 60000 });
    const request = manager.ensureFresh({ minValidityMs: 680000 });
    const rejected = assert.rejects(request, /expired/i);
    await Promise.resolve();
    finish(creds("short", 120000));
    assert.equal((await save).accessKeyId, "short");
    await rejected;
    assert.equal(calls, 1);
  });

  it("publishes prepared credentials synchronously after preparation succeeds", async () => {
    const prepared = { ready: true };
    create({
      prepareRefresh: async (next) => {
        assert.deepEqual(fs.readdirSync(dir), []);
        assert.equal(next.accessKeyId, "key-1");
        return prepared;
      },
      onRefresh: (next, value) => {
        assert.equal(value, prepared);
        published.push(next.accessKeyId);
      },
    });
    await manager.ensureFresh();
    assert.deepEqual(published, ["key-1"]);
    assert.equal(manager.getCredentials().accessKeyId, "key-1");
    assert.equal(JSON.parse(fs.readFileSync(path.join(dir, "scoped-creds.json"))).AccessKeyId, "key-1");
  });

  it("rejects asynchronous publication callbacks before they can start", async () => {
    let finish;
    const deferred = new Promise((resolve) => { finish = resolve; });
    const onRefresh = async (next) => {
      await deferred;
      published.push(next.accessKeyId);
    };
    assert.throws(() => create({ onRefresh }), /onRefresh.*synchronous.*prepareRefresh/i);
    finish();
    await Promise.resolve();
    assert.equal(calls, 0);
    assert.deepEqual(published, []);
    assert.deepEqual(fs.readdirSync(dir), []);
  });

  it("rejects preparation failures and credentials that expire during preparation", async () => {
    let fail = true;
    create({
      prepareRefresh: async () => {
        if (fail) throw new Error("preparation failed");
        now += 3600001;
      },
    });
    await assert.rejects(manager.ensureFresh(), /preparation failed/);
    fail = false;
    await assert.rejects(manager.ensureFresh(), /expired/i);
    assert.deepEqual(published, []);
    assert.deepEqual(fs.readdirSync(dir), []);
  });

  it("rejects invalid or expired credentials before replacing existing files", () => {
    scoped.writeCredentialFiles(creds("good"), dir, { now: () => now });
    const original = fs.readFileSync(path.join(dir, "scoped-creds.json"), "utf8");
    assert.throws(() => scoped.writeCredentialFiles(creds("bad", -1), dir, { now: () => now }), /expired/i);
    assert.throws(() => scoped.writeCredentialFiles(creds("bad", -1), dir), /expired/i);
    assert.throws(() => scoped.writeCredentialFiles({ ...creds(), sessionToken: null }, dir, { now: () => now }), /sessionToken/i);
    assert.equal(fs.readFileSync(path.join(dir, "scoped-creds.json"), "utf8"), original);
  });

  it("uses private atomic files and removes its staging file after a failed rename", () => {
    scoped.writeCredentialFiles(creds("good"), dir);
    const file = path.join(dir, "scoped-creds.json");
    const original = fs.readFileSync(file, "utf8");
    const rename = fs.renameSync;
    mock.method(fs, "renameSync", (src, dst) => {
      if (dst === file) {
        assert.equal(fs.readFileSync(file, "utf8"), original);
        assert.equal(JSON.parse(fs.readFileSync(src)).AccessKeyId, "next");
        throw new Error("rename failed");
      }
      return rename(src, dst);
    });
    assert.throws(() => scoped.writeCredentialFiles(creds("next"), dir), /rename failed/);
    assert.equal(fs.readFileSync(file, "utf8"), original);
    assert.equal(fs.statSync(file).mode & 0o777, 0o600);
    assert.deepEqual(fs.readdirSync(dir).sort(), ["scoped-aws-config", "scoped-creds.json"]);
  });
});
