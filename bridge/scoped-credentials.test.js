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
    assert.ok(parsed.Statement.length >= 2, "should have at least S3 + KMS statements");
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

    // Resource should be scoped to namespace prefix only
    const resources = Array.isArray(objectStmt.Resource)
      ? objectStmt.Resource
      : [objectStmt.Resource];
    assert.ok(
      resources.every((r) => r.includes("slack_U0ABC")),
      `all resources should contain namespace, got: ${resources}`,
    );
    // Must NOT grant access to the whole bucket
    assert.ok(
      !resources.some((r) => r.endsWith("/*") && !r.includes("slack_U0ABC")),
      "should not grant bucket-wide access",
    );
  });

  it("ListBucket restricted by s3:prefix condition", () => {
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
    assert.ok(listStmt.Condition, "ListBucket should have Condition");
    assert.ok(
      listStmt.Condition.StringLike || listStmt.Condition.StringEquals,
      "should have StringLike or StringEquals condition",
    );

    // The condition should reference the namespace
    const conditionValues = JSON.stringify(listStmt.Condition);
    assert.ok(
      conditionValues.includes("telegram_99999"),
      `ListBucket condition should reference namespace, got: ${conditionValues}`,
    );
  });

  it("includes KMS decrypt permission when cmkArn provided", () => {
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
    assert.ok(kmsStmt, "should have KMS statement");
    // KMS should be scoped to the specific CMK, not wildcard
    const resource = Array.isArray(kmsStmt.Resource)
      ? kmsStmt.Resource[0]
      : kmsStmt.Resource;
    assert.equal(resource, "arn:aws:kms:us-west-2:123:key/abc-123");
  });

  it("omits KMS statement when cmkArn not provided", () => {
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
    assert.equal(kmsStmt, undefined, "should not have KMS statement without cmkArn");
  });

  it("scopes iam:PassRole to eventbridgeRoleArn when provided", () => {
    const policy = buildSessionPolicy({
      bucket: "my-bucket",
      namespace: "telegram_12345",
      cmkArn: "arn:aws:kms:us-west-2:123:key/abc-123",
      eventbridgeRoleArn: "arn:aws:iam::123:role/openclaw-cron-scheduler-role",
    });

    const parsed = JSON.parse(policy);
    const passRoleStmt = parsed.Statement.find(
      (s) => s.Action === "iam:PassRole",
    );
    assert.ok(passRoleStmt, "should have PassRole statement");
    assert.equal(
      passRoleStmt.Resource,
      "arn:aws:iam::123:role/openclaw-cron-scheduler-role",
    );
  });

  it("omits iam:PassRole when eventbridgeRoleArn not provided", () => {
    const policy = buildSessionPolicy({
      bucket: "my-bucket",
      namespace: "telegram_12345",
      cmkArn: "arn:aws:kms:us-west-2:123:key/abc-123",
    });

    const parsed = JSON.parse(policy);
    const passRoleStmt = parsed.Statement.find(
      (s) => s.Action === "iam:PassRole",
    );
    assert.equal(passRoleStmt, undefined, "should not have PassRole without eventbridgeRoleArn");
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

  it("scopes DynamoDB to identity table ARN when provided", () => {
    const policy = buildSessionPolicy({
      bucket: "my-bucket",
      namespace: "telegram_12345",
      identityTableArn: "arn:aws:dynamodb:us-west-2:123456789012:table/openclaw-identity",
    });

    const parsed = JSON.parse(policy);
    const dynamoStmt = parsed.Statement.find((s) => s.Sid === "DynamoDBIdentity");
    assert.ok(dynamoStmt, "should have DynamoDB statement");
    const resources = Array.isArray(dynamoStmt.Resource) ? dynamoStmt.Resource : [dynamoStmt.Resource];
    assert.ok(
      resources.includes("arn:aws:dynamodb:us-west-2:123456789012:table/openclaw-identity"),
      "should include table ARN",
    );
    assert.ok(
      resources.includes("arn:aws:dynamodb:us-west-2:123456789012:table/openclaw-identity/index/*"),
      "should include table index wildcard",
    );
    assert.equal(resources.length, 2, "should have exactly table + index resources");
  });

  it("falls back to wildcard DynamoDB when identityTableArn not provided", () => {
    const policy = buildSessionPolicy({
      bucket: "my-bucket",
      namespace: "telegram_12345",
    });

    const parsed = JSON.parse(policy);
    const dynamoStmt = parsed.Statement.find((s) => s.Sid === "DynamoDBIdentity");
    assert.ok(dynamoStmt, "should have DynamoDB statement");
    assert.equal(dynamoStmt.Resource, "*", "should fall back to wildcard");
  });

  it("scopes EventBridge CRUD to schedule group schedules when provided", () => {
    const policy = buildSessionPolicy({
      bucket: "my-bucket",
      namespace: "telegram_12345",
      scheduleGroupArn: "arn:aws:scheduler:us-west-2:123456789012:schedule-group/openclaw-cron",
    });

    const parsed = JSON.parse(policy);
    const crudStmt = parsed.Statement.find((s) => s.Sid === "EventBridgeSchedulerCRUD");
    assert.ok(crudStmt, "should have EventBridge CRUD statement");
    assert.equal(
      crudStmt.Resource,
      "arn:aws:scheduler:us-west-2:123456789012:schedule/openclaw-cron/*",
      "CRUD resource should target schedules within the group",
    );
  });

  it("scopes EventBridge List to schedule group when provided", () => {
    const policy = buildSessionPolicy({
      bucket: "my-bucket",
      namespace: "telegram_12345",
      scheduleGroupArn: "arn:aws:scheduler:us-west-2:123456789012:schedule-group/openclaw-cron",
    });

    const parsed = JSON.parse(policy);
    const listStmt = parsed.Statement.find((s) => s.Sid === "EventBridgeSchedulerList");
    assert.ok(listStmt, "should have EventBridge List statement");
    assert.equal(
      listStmt.Resource,
      "arn:aws:scheduler:us-west-2:123456789012:schedule-group/openclaw-cron",
      "List resource should target the schedule group",
    );
  });

  it("falls back to wildcard EventBridge when scheduleGroupArn not provided", () => {
    const policy = buildSessionPolicy({
      bucket: "my-bucket",
      namespace: "telegram_12345",
    });

    const parsed = JSON.parse(policy);
    const crudStmt = parsed.Statement.find((s) => s.Sid === "EventBridgeSchedulerCRUD");
    const listStmt = parsed.Statement.find((s) => s.Sid === "EventBridgeSchedulerList");
    assert.equal(crudStmt.Resource, "*", "CRUD should fall back to wildcard");
    assert.equal(listStmt.Resource, "*", "List should fall back to wildcard");
  });
});

// --- createScopedCredentials ---

describe("createScopedCredentials", () => {
  let createScopedCredentials, _mockStsClient;

  const MOCK_CREDS = {
    AccessKeyId: "AKIAIOSFODNN7EXAMPLE",
    SecretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    SessionToken: "FwoGZXIvYXdzEBYaDH...",
    Expiration: new Date("2026-03-02T02:00:00Z"),
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

  it("constructs scoped DynamoDB and EventBridge ARNs from env vars", async () => {
    process.env.IDENTITY_TABLE_NAME = "openclaw-identity";
    process.env.EVENTBRIDGE_SCHEDULE_GROUP = "openclaw-cron";

    await createScopedCredentials("telegram_12345", {
      stsClient: _mockStsClient,
    });

    const input = _mockStsClient.send.mock.calls[0].arguments[0].input;
    const policy = JSON.parse(input.Policy);

    // DynamoDB should be scoped to identity table
    const dynamoStmt = policy.Statement.find((s) => s.Sid === "DynamoDBIdentity");
    assert.ok(Array.isArray(dynamoStmt.Resource), "DynamoDB should have array of resources");
    assert.ok(
      dynamoStmt.Resource[0].includes("openclaw-identity"),
      "DynamoDB should reference identity table",
    );

    // EventBridge CRUD should be scoped to schedule group
    const crudStmt = policy.Statement.find((s) => s.Sid === "EventBridgeSchedulerCRUD");
    assert.ok(
      crudStmt.Resource.includes("openclaw-cron"),
      "EventBridge CRUD should reference schedule group",
    );

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
    expiration: new Date("2026-03-02T02:00:00Z"),
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
      expiration: new Date("2026-03-02T03:00:00Z"),
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

  it("includes standard env vars (PATH, HOME, NODE_OPTIONS, AWS_REGION)", () => {
    const env = buildOpenClawEnv({
      credDir: "/tmp/scoped",
      baseEnv: {
        PATH: "/usr/bin",
        HOME: "/root",
        NODE_OPTIONS: "--dns-result-order=ipv4first",
        AWS_REGION: "us-west-2",
      },
    });
    assert.equal(env.PATH, "/usr/bin");
    assert.equal(env.HOME, "/root");
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

  it("includes SecretsManagerUserSecrets statement when region and account provided", () => {
    const policy = buildSessionPolicy({
      bucket: "openclaw-user-files-123-us-west-2",
      namespace: "telegram_12345",
      cmkArn: "arn:aws:kms:us-west-2:123:key/abc-123",
      region: "us-west-2",
      account: "123456789012",
    });

    const parsed = JSON.parse(policy);
    const smStmt = parsed.Statement.find((s) => s.Sid === "SecretsManagerUserSecrets");
    assert.ok(smStmt, "SecretsManagerUserSecrets statement should exist");
    assert.equal(
      smStmt.Resource,
      "arn:aws:secretsmanager:us-west-2:123456789012:secret:openclaw/user/telegram_12345/*",
    );
    assert.ok(smStmt.Action.includes("secretsmanager:GetSecretValue"));
    assert.ok(smStmt.Action.includes("secretsmanager:CreateSecret"));
    assert.ok(smStmt.Action.includes("secretsmanager:DeleteSecret"));
  });

  it("includes SecretsManagerListUserSecrets statement", () => {
    const policy = buildSessionPolicy({
      bucket: "openclaw-user-files-123-us-west-2",
      namespace: "telegram_12345",
      region: "us-west-2",
      account: "123456789012",
    });

    const parsed = JSON.parse(policy);
    const listStmt = parsed.Statement.find((s) => s.Sid === "SecretsManagerListUserSecrets");
    assert.ok(listStmt, "SecretsManagerListUserSecrets statement should exist");
    assert.equal(listStmt.Action, "secretsmanager:ListSecrets");
    assert.equal(listStmt.Resource, "*");
  });

  it("omits Secrets Manager statements when region/account not provided", () => {
    const policy = buildSessionPolicy({
      bucket: "openclaw-user-files-123-us-west-2",
      namespace: "telegram_12345",
    });

    const parsed = JSON.parse(policy);
    const smStmt = parsed.Statement.find((s) => s.Sid === "SecretsManagerUserSecrets");
    assert.equal(smStmt, undefined, "SecretsManagerUserSecrets should not exist without region/account");
  });

  it("scopes secret resource to correct namespace", () => {
    const policy = buildSessionPolicy({
      bucket: "test-bucket",
      namespace: "slack_abc-def",
      region: "ap-southeast-2",
      account: "999888777666",
    });

    const parsed = JSON.parse(policy);
    const smStmt = parsed.Statement.find((s) => s.Sid === "SecretsManagerUserSecrets");
    assert.ok(smStmt.Resource.includes("slack_abc-def"));
    assert.ok(smStmt.Resource.includes("ap-southeast-2"));
    assert.ok(smStmt.Resource.includes("999888777666"));
  });
});
