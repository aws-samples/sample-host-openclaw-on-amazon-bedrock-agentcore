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

  const REGION = "us-west-2";
  const ACCOUNT = "123456789012";
  const TABLE_ARN = `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/openclaw-identity`;
  const GROUP_ARN = `arn:aws:scheduler:${REGION}:${ACCOUNT}:schedule-group/openclaw-cron`;
  const SCHEDULER_ROLE_ARN = `arn:aws:iam::${ACCOUNT}:role/openclaw-cron-scheduler-role-${REGION}`;

  function fullPolicy(overrides = {}) {
    return JSON.parse(buildSessionPolicy({
      bucket: "openclaw-user-files-123-us-west-2",
      namespace: "telegram_12345",
      internalUserId: "user_0123456789abcdef",
      identityTableArn: TABLE_ARN,
      scheduleGroupArn: GROUP_ARN,
      eventbridgeRoleArn: SCHEDULER_ROLE_ARN,
      region: REGION,
      account: ACCOUNT,
      ...overrides,
    }));
  }

  function statementFor(policy, action) {
    return policy.Statement.find((s) => [].concat(s.Action).includes(action));
  }

  beforeEach(() => {
    ({ buildSessionPolicy } = require("./scoped-credentials"));
  });

  it("returns valid JSON with only Allow statements", () => {
    const parsed = fullPolicy();
    assert.equal(parsed.Version, "2012-10-17");
    assert.ok(Array.isArray(parsed.Statement));
    for (const s of parsed.Statement) assert.equal(s.Effect, "Allow");
  });

  it("scopes S3 object actions to the namespace prefix", () => {
    const stmt = statementFor(fullPolicy(), "s3:GetObject");
    assert.equal(stmt.Resource, "arn:aws:s3:::openclaw-user-files-123-us-west-2/telegram_12345/*");
    assert.deepEqual(stmt.Action, ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]);
  });

  it("limits ListBucket to the namespace prefix", () => {
    const stmt = statementFor(fullPolicy(), "s3:ListBucket");
    assert.equal(stmt.Resource, "arn:aws:s3:::openclaw-user-files-123-us-west-2");
    assert.deepEqual(stmt.Condition, { StringLike: { "s3:prefix": "telegram_12345/*" } });
  });

  it("scopes Secrets Manager item actions to the user's secret path", () => {
    const stmt = statementFor(fullPolicy(), "secretsmanager:GetSecretValue");
    assert.equal(stmt.Resource,
      `arn:aws:secretsmanager:${REGION}:${ACCOUNT}:secret:openclaw/user/telegram_12345/*`);
    for (const action of ["secretsmanager:PutSecretValue", "secretsmanager:CreateSecret",
      "secretsmanager:DeleteSecret", "secretsmanager:TagResource"]) {
      assert.ok(stmt.Action.includes(action), action);
    }
    assert.ok(!stmt.Action.includes("secretsmanager:ListSecrets"));
  });

  it("keeps ListSecrets alone in its own statement (no resource-level support)", () => {
    const stmt = statementFor(fullPolicy(), "secretsmanager:ListSecrets");
    assert.equal(stmt.Action, "secretsmanager:ListSecrets");
    assert.equal(stmt.Resource, "*");
  });

  it("limits DynamoDB to the identity table and the user's USER# partition", () => {
    const stmt = statementFor(fullPolicy(), "dynamodb:PutItem");
    assert.equal(stmt.Resource, TABLE_ARN);
    assert.deepEqual(stmt.Condition, {
      "ForAllValues:StringEquals": { "dynamodb:LeadingKeys": ["USER#user_0123456789abcdef"] },
    });
    assert.ok(!stmt.Action.includes("dynamodb:Scan"));
  });

  it("does not let the session write CHANNEL#, ALLOW# or another user's partition", () => {
    const keys = statementFor(fullPolicy(), "dynamodb:PutItem")
      .Condition["ForAllValues:StringEquals"]["dynamodb:LeadingKeys"];
    assert.ok(!keys.some((k) => /^(CHANNEL|ALLOW|BIND|ACTOR)#/.test(k)));
    assert.ok(!keys.some((k) => k.includes("*") || k.includes("?")));
  });

  it("limits scheduler actions to schedules named for this namespace", () => {
    const stmt = statementFor(fullPolicy(), "scheduler:CreateSchedule");
    assert.equal(stmt.Resource,
      `arn:aws:scheduler:${REGION}:${ACCOUNT}:schedule/openclaw-cron/openclaw-telegram_12345-*`);
    assert.deepEqual(stmt.Action.slice().sort(), [
      "scheduler:CreateSchedule", "scheduler:DeleteSchedule", "scheduler:GetSchedule", "scheduler:UpdateSchedule",
    ]);
  });

  it("matches the schedule-name truncation used by the eventbridge-cron skill", () => {
    // The skill module exits at load without AWS_REGION; it makes no AWS call.
    const savedRegion = process.env.AWS_REGION;
    process.env.AWS_REGION = "us-west-2";
    let buildScheduleName;
    try {
      ({ buildScheduleName } = require("./skills/eventbridge-cron/common"));
    } finally {
      if (savedRegion === undefined) delete process.env.AWS_REGION;
      else process.env.AWS_REGION = savedRegion;
    }
    for (const namespace of ["telegram_12345", `feishu_ou_${"a".repeat(32)}`, `slack_${"B".repeat(59)}`]) {
      const stmt = statementFor(fullPolicy({ namespace }), "scheduler:GetSchedule");
      const pattern = stmt.Resource.split("/").pop();
      assert.ok(pattern.endsWith("-*"));
      const name = buildScheduleName(namespace, "0a1b2c3d");
      assert.ok(name.startsWith(pattern.slice(0, -1)), `${name} should match ${pattern}`);
    }
  });

  it("gives two long namespaces that share their first 46 characters separate schedule patterns", () => {
    const savedRegion = process.env.AWS_REGION;
    process.env.AWS_REGION = "us-west-2";
    let buildScheduleName, legacyScheduleName;
    try {
      ({ buildScheduleName, legacyScheduleName } = require("./skills/eventbridge-cron/common"));
    } finally {
      if (savedRegion === undefined) delete process.env.AWS_REGION;
      else process.env.AWS_REGION = savedRegion;
    }
    const owner = `telegram_${"a".repeat(37)}owner`;
    const other = `telegram_${"a".repeat(37)}other`;
    const pattern = statementFor(fullPolicy({ namespace: owner }), "scheduler:GetSchedule")
      .Resource.split("/").pop().slice(0, -1);
    assert.ok(buildScheduleName(owner, "0a1b2c3d").startsWith(pattern));
    assert.ok(!buildScheduleName(other, "0a1b2c3d").startsWith(pattern));
    // The pre-digest name is shared by both namespaces, so it is not granted.
    assert.equal(legacyScheduleName(owner, "0a1b2c3d"), legacyScheduleName(other, "0a1b2c3d"));
    assert.ok(!legacyScheduleName(owner, "0a1b2c3d").startsWith(pattern));
  });

  it("scopes iam:PassRole to the scheduler role and service", () => {
    const stmt = statementFor(fullPolicy(), "iam:PassRole");
    assert.equal(stmt.Resource, SCHEDULER_ROLE_ARN);
    assert.deepEqual(stmt.Condition, { StringEquals: { "iam:PassedToService": "scheduler.amazonaws.com" } });
  });

  it("allows KMS only via S3, DynamoDB and Secrets Manager", () => {
    const stmt = statementFor(fullPolicy(), "kms:Decrypt");
    assert.deepEqual(stmt.Action, ["kms:Decrypt", "kms:GenerateDataKey"]);
    assert.deepEqual(stmt.Condition, { StringLike: { "kms:ViaService": [
      "s3.us-west-2.amazonaws.com", "dynamodb.us-west-2.amazonaws.com", "secretsmanager.us-west-2.amazonaws.com",
    ] } });
  });

  it("grants no wildcard action and Resource * only to ListSecrets and via-service KMS", () => {
    const parsed = fullPolicy();
    for (const s of parsed.Statement) {
      for (const action of [].concat(s.Action)) assert.ok(!action.includes("*"), action);
      if (s.Resource === "*") {
        assert.ok(s.Action === "secretsmanager:ListSecrets" || s.Condition?.StringLike?.["kms:ViaService"],
          JSON.stringify(s));
      }
    }
  });

  it("leaves out statements whose resource is unknown instead of widening them", () => {
    const parsed = JSON.parse(buildSessionPolicy({ bucket: "my-bucket", namespace: "telegram_12345" }));
    for (const action of ["dynamodb:GetItem", "scheduler:CreateSchedule", "iam:PassRole",
      "secretsmanager:GetSecretValue"]) {
      assert.equal(statementFor(parsed, action), undefined, action);
    }
    // S3 and the via-service KMS statement stay so workspace sync keeps working.
    assert.ok(statementFor(parsed, "s3:GetObject"));
    assert.ok(statementFor(parsed, "kms:Decrypt"));
  });

  it("leaves out DynamoDB when the internal user ID is unknown", () => {
    assert.equal(statementFor(fullPolicy({ internalUserId: undefined }), "dynamodb:GetItem"), undefined);
  });

  it("rejects an internal user ID that could widen the LeadingKeys match", () => {
    for (const internalUserId of ["user_*", "user_?", 'user_"x', "", 42]) {
      assert.throws(() => fullPolicy({ internalUserId }), /internalUserId/);
    }
  });

  it("does not let one namespace's secret or schedule pattern cover a longer one", () => {
    const parsed = fullPolicy({ namespace: "telegram_1" });
    assert.ok(statementFor(parsed, "secretsmanager:GetSecretValue").Resource.endsWith(":secret:openclaw/user/telegram_1/*"));
    assert.ok(statementFor(parsed, "scheduler:GetSchedule").Resource.endsWith("/openclaw-telegram_1-*"));
    assert.equal(statementFor(parsed, "s3:ListBucket").Condition.StringLike["s3:prefix"], "telegram_1/*");
  });

  it("stays under the 2048-character STS limit at the longest allowed names", () => {
    const suffix = "-" + "x".repeat(12); // longest environment suffix
    const region = "ap-southeast-2";
    const policy = buildSessionPolicy({
      bucket: `openclaw-user-files-${ACCOUNT}-${region}${suffix}`.slice(0, 63),
      namespace: "a" + "b".repeat(64), // longest namespace validateNamespace accepts
      internalUserId: "user_0123456789abcdef",
      identityTableArn: `arn:aws:dynamodb:${region}:${ACCOUNT}:table/openclaw-identity${suffix}`,
      scheduleGroupArn: `arn:aws:scheduler:${region}:${ACCOUNT}:schedule-group/openclaw-cron${suffix}`,
      eventbridgeRoleArn: `arn:aws:iam::${ACCOUNT}:role/openclaw-cron-scheduler-role-${region}${suffix}`,
      region,
      account: ACCOUNT,
    });
    assert.ok(policy.length <= 2048, `policy is ${policy.length} characters`);
  });

  it("refuses to build a policy over the STS limit", () => {
    assert.throws(() => fullPolicy({ bucket: "b".repeat(1500) }), /limit is 2048/);
  });

  it("rejects namespace with path traversal", () => {
    assert.throws(
      () => buildSessionPolicy({ bucket: "my-bucket", namespace: "../other-user" }),
      /invalid namespace/i,
    );
  });

  it("rejects empty namespace", () => {
    assert.throws(
      () => buildSessionPolicy({ bucket: "my-bucket", namespace: "" }),
      /invalid namespace/i,
    );
  });

  it("rejects a namespace carrying an IAM wildcard", () => {
    for (const namespace of ["telegram_*", "telegram_1?"]) {
      assert.throws(() => buildSessionPolicy({ bucket: "my-bucket", namespace }), /invalid namespace/i);
    }
  });

  it("grants the identity table only, not its indexes", () => {
    const stmt = statementFor(fullPolicy(), "dynamodb:GetItem");
    for (const resource of [].concat(stmt.Resource)) {
      assert.ok(!resource.includes("/index/"), resource);
    }
  });

  it("does not grant ListSchedules (it would list other users' schedule names)", () => {
    assert.equal(statementFor(fullPolicy(), "scheduler:ListSchedules"), undefined);
  });

  it("stays under the 2048-character STS limit for a 46-character Feishu namespace", () => {
    const namespace = "feishu_ou_" + "0123456789abcdef".repeat(2) + "abcd";
    assert.equal(namespace.length, 46);
    const region = "ap-southeast-2";
    const policy = buildSessionPolicy({
      bucket: `openclaw-user-files-${ACCOUNT}-${region}-production`,
      namespace,
      internalUserId: "user_0123456789abcdef",
      eventbridgeRoleArn: `arn:aws:iam::${ACCOUNT}:role/openclaw-cron-scheduler-role-${region}-production`,
      identityTableArn: `arn:aws:dynamodb:${region}:${ACCOUNT}:table/openclaw-identity-production`,
      scheduleGroupArn: `arn:aws:scheduler:${region}:${ACCOUNT}:schedule-group/openclaw-cron-production`,
      region,
      account: ACCOUNT,
    });
    assert.ok(policy.length <= 2048, `policy is ${policy.length} characters`);
  });

  it("never grants the per-user Cognito password secrets (openclaw/cognito-user/*)", () => {
    const { defaultSecretId } = require("./cognito-token");
    const name = defaultSecretId("telegram:12345", "us-west-2_POOL");
    const arn = `arn:aws:secretsmanager:${REGION}:${ACCOUNT}:secret:${name}-AbCdEf`;
    const matches = (pattern) => new RegExp("^" + pattern.split("*").map(
      (part) => part.replace(/[.+?^${}()|[\]\\]/g, "\\$&")).join(".*") + "$").test(arn);
    for (const stmt of fullPolicy().Statement) {
      const actions = [].concat(stmt.Action);
      if (!actions.some((a) => a.startsWith("secretsmanager:") && a !== "secretsmanager:ListSecrets")) continue;
      assert.ok(![].concat(stmt.Resource).some(matches), "session policy must not reach " + name);
    }
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

  it("builds the scoped ARNs from the runtime environment and the internal user ID", async () => {
    process.env.IDENTITY_TABLE_NAME = "openclaw-identity";
    process.env.EVENTBRIDGE_SCHEDULE_GROUP = "openclaw-cron";
    process.env.EVENTBRIDGE_ROLE_ARN = "arn:aws:iam::123456789012:role/openclaw-cron-scheduler-role-us-west-2";
    try {
      await createScopedCredentials("telegram_12345", {
        stsClient: _mockStsClient, internalUserId: "user_0123456789abcdef",
      });
      const policy = JSON.parse(_mockStsClient.send.mock.calls[0].arguments[0].input.Policy);
      const find = (action) => policy.Statement.find((s) => [].concat(s.Action).includes(action));
      assert.equal(find("dynamodb:GetItem").Resource,
        "arn:aws:dynamodb:us-west-2:123456789012:table/openclaw-identity");
      assert.deepEqual(find("dynamodb:GetItem").Condition["ForAllValues:StringEquals"]["dynamodb:LeadingKeys"],
        ["USER#user_0123456789abcdef"]);
      assert.equal(find("scheduler:UpdateSchedule").Resource,
        "arn:aws:scheduler:us-west-2:123456789012:schedule/openclaw-cron/openclaw-telegram_12345-*");
      assert.equal(find("iam:PassRole").Resource, process.env.EVENTBRIDGE_ROLE_ARN);
      assert.equal(find("secretsmanager:GetSecretValue").Resource,
        "arn:aws:secretsmanager:us-west-2:123456789012:secret:openclaw/user/telegram_12345/*");
      assert.ok(!policy.Statement.some((s) => [].concat(s.Action).some((a) => a.includes("*"))));
    } finally {
      delete process.env.IDENTITY_TABLE_NAME;
      delete process.env.EVENTBRIDGE_SCHEDULE_GROUP;
      delete process.env.EVENTBRIDGE_ROLE_ARN;
    }
  });

  it("logs the packed policy size STS reports", async () => {
    _mockStsClient.send = mock.fn(async () => ({ Credentials: { ...MOCK_CREDS }, PackedPolicySize: 41 }));
    const log = mock.method(console, "log", () => {});
    try {
      await createScopedCredentials("telegram_12345", { stsClient: _mockStsClient });
      assert.ok(log.mock.calls.some((c) => c.arguments[0].includes("packed size") && c.arguments[1] === 41));
    } finally {
      log.mock.restore();
    }
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

  it("hands the credential directory to the chown hook when one is given", () => {
    const chowned = [];
    writeCredentialFiles(CREDS, tmpDir, { chown: (p) => chowned.push(p) });
    // The hook receives the directory (recursive chown covers both files).
    assert.deepEqual(chowned, [tmpDir]);
  });

  it("does not require a chown hook", () => {
    assert.doesNotThrow(() => writeCredentialFiles(CREDS, tmpDir));
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
      baseEnv: { SUBAGENT_BEDROCK_MODEL_ID: "global.anthropic.claude-sonnet-5-5" },
    });
    assert.equal(env.SUBAGENT_BEDROCK_MODEL_ID, "global.anthropic.claude-sonnet-5-5");
  });
});

// --- Secrets Manager session policy ---

describe("buildSessionPolicy Secrets Manager", () => {
  let buildSessionPolicy;

  beforeEach(() => {
    ({ buildSessionPolicy } = require("./scoped-credentials"));
  });

  const smStatement = (policy) => JSON.parse(policy).Statement.find(
    (s) => Array.isArray(s.Action) && s.Action.includes("secretsmanager:GetSecretValue"));

  it("includes the api-keys skill's item actions, TagResource included", () => {
    const stmt = smStatement(buildSessionPolicy({
      bucket: "openclaw-user-files-123-us-west-2", namespace: "telegram_12345",
      region: "us-west-2", account: "123456789012",
    }));
    for (const action of ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue",
      "secretsmanager:CreateSecret", "secretsmanager:DeleteSecret", "secretsmanager:TagResource"]) {
      assert.ok(stmt.Action.includes(action), action);
    }
  });

  it("uses the region, account and namespace it is given", () => {
    const stmt = smStatement(buildSessionPolicy({
      bucket: "test-bucket", namespace: "slack_abc-def", region: "ap-southeast-2", account: "999888777666",
    }));
    assert.equal(stmt.Resource, "arn:aws:secretsmanager:ap-southeast-2:999888777666:secret:openclaw/user/slack_abc-def/*");
  });

  it("grants no secret item actions when region or account is unknown", () => {
    assert.equal(smStatement(buildSessionPolicy({ bucket: "b", namespace: "telegram_12345" })), undefined);
    assert.equal(smStatement(buildSessionPolicy({ bucket: "b", namespace: "telegram_12345", region: "us-west-2" })),
      undefined);
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

  it("passes chownCredentials through to writeCredentialFiles on each refresh", async () => {
    const chowned = [];
    create({ chownCredentials: (p) => chowned.push(p) });
    await manager.ensureFresh();
    now += 3600001;
    await manager.ensureFresh();
    // One chown per credential write (activation + post-jump refresh).
    assert.deepEqual(chowned, [dir, dir]);
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
