"use strict";

const { describe, it, beforeEach, afterEach, mock } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  buildSessionPolicy,
  createScopedCredentials,
  writeCredentialFiles,
  buildOpenClawEnv,
} = require("./scoped-credentials");

const ACCOUNT = "123456789012";
const CMK_ARN = `arn:aws:kms:eu-west-1:${ACCOUNT}:key/abc-123`;
const ROLE_ARN = `arn:aws:iam::${ACCOUNT}:role/personal-operator-workspace-session`;
const MOCK_CREDS = {
  AccessKeyId: "ASIAIOSFODNN7EXAMPLE",
  SecretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
  SessionToken: "FwoGZXIvYXdzEBYaDH...",
  Expiration: new Date(Date.now() + 30 * 60 * 1000),
};

function parsedPolicy(overrides = {}) {
  return JSON.parse(
    buildSessionPolicy({
      bucket: "personal-operator-workspace",
      namespace: "user_A",
      ...overrides,
    }),
  );
}

function allActions(policy) {
  return policy.Statement.flatMap((statement) =>
    Array.isArray(statement.Action) ? statement.Action : [statement.Action],
  );
}

function allResources(policy) {
  return policy.Statement.flatMap((statement) =>
    Array.isArray(statement.Resource) ? statement.Resource : [statement.Resource],
  );
}

describe("exact workspace session policy", () => {
  it("grants only exact namespace S3 object and prefix-list access", () => {
    const policy = parsedPolicy();
    assert.deepEqual(policy, {
      Version: "2012-10-17",
      Statement: [
        {
          Effect: "Allow",
          Action: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
          Resource:
            "arn:aws:s3:::personal-operator-workspace/user_A/*",
        },
        {
          Effect: "Allow",
          Action: "s3:ListBucket",
          Resource: "arn:aws:s3:::personal-operator-workspace",
          Condition: { StringLike: { "s3:prefix": "user_A/*" } },
        },
      ],
    });
  });

  it("adds only exact-key KMS encryption operations with exact conditions", () => {
    const policy = parsedPolicy({ cmkArn: CMK_ARN, callerAccount: ACCOUNT });
    assert.deepEqual(policy.Statement[2], {
      Effect: "Allow",
      Action: ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"],
      Resource: CMK_ARN,
      Condition: {
        StringEquals: {
          "kms:ViaService": "s3.eu-west-1.amazonaws.com",
          "kms:CallerAccount": ACCOUNT,
        },
      },
    });
  });

  it("contains no wildcard resource/action or non-S3/KMS authority", () => {
    const policy = parsedPolicy({ cmkArn: CMK_ARN, callerAccount: ACCOUNT });
    const actions = allActions(policy);
    assert.deepEqual(new Set(actions), new Set([
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:GenerateDataKey",
    ]));
    assert.equal(actions.some((action) => action.includes("*")), false);
    assert.equal(allResources(policy).includes("*"), false);
    assert.doesNotMatch(
      JSON.stringify(policy),
      /scheduler|events|eventbridge|dynamodb|secretsmanager|iam:|sts:|PassRole/i,
    );
  });

  it("produces disjoint object resources and list prefixes for two users", () => {
    const first = parsedPolicy({ namespace: "user_A" });
    const second = parsedPolicy({ namespace: "user_B" });
    assert.notEqual(first.Statement[0].Resource, second.Statement[0].Resource);
    assert.notEqual(
      first.Statement[1].Condition.StringLike["s3:prefix"],
      second.Statement[1].Condition.StringLike["s3:prefix"],
    );
    assert.doesNotMatch(first.Statement[0].Resource, /user_B/);
    assert.doesNotMatch(second.Statement[0].Resource, /user_A/);
  });

  it("rejects invalid buckets, namespaces, regions, and CMK accounts", () => {
    for (const bucket of ["", "Bad_Bucket", "bucket/*", "bucket/name"] ) {
      assert.throws(
        () => buildSessionPolicy({ bucket, namespace: "user_A" }),
        /bucket/i,
      );
    }
    for (const namespace of ["", "a", "telegram:123", "../user_A"] ) {
      assert.throws(
        () => buildSessionPolicy({ bucket: "valid-bucket", namespace }),
        /identity|namespace/i,
      );
    }
    assert.throws(
      () =>
        parsedPolicy({
          cmkArn: `arn:aws:kms:us-west-2:${ACCOUNT}:key/abc`,
          callerAccount: ACCOUNT,
        }),
      /CMK|region/i,
    );
    assert.throws(
      () => parsedPolicy({ cmkArn: CMK_ARN, callerAccount: "999999999999" }),
      /CMK|account/i,
    );
  });
});

describe("fatal scoped credential minting", () => {
  let env;
  let stsClient;

  beforeEach(() => {
    env = {
      AWS_REGION: "eu-west-1",
      AWS_DEFAULT_REGION: "eu-west-1",
      S3_USER_FILES_BUCKET: "personal-operator-workspace",
      WORKSPACE_SESSION_ROLE_ARN: ROLE_ARN,
      CMK_ARN,
    };
    stsClient = {
      send: mock.fn(async () => ({ Credentials: { ...MOCK_CREDS } })),
    };
  });

  it("assumes only the workspace session role with a bounded workspace name", async () => {
    const result = await createScopedCredentials("user_A", { env, stsClient });
    assert.equal(result.accessKeyId, MOCK_CREDS.AccessKeyId);
    assert.equal(stsClient.send.mock.calls.length, 1);
    const input = stsClient.send.mock.calls[0].arguments[0].input;
    assert.equal(input.RoleArn, ROLE_ARN);
    assert.match(input.RoleSessionName, /^workspace-[A-Za-z0-9_-]+$/);
    assert.ok(input.RoleSessionName.length <= 64);
    assert.equal(input.DurationSeconds, 3600);
    assert.deepEqual(
      new Set(allActions(JSON.parse(input.Policy))),
      new Set([
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:GenerateDataKey",
      ]),
    );
  });

  it("derives deterministic and namespace-distinct role session names", async () => {
    await createScopedCredentials("user_A", { env, stsClient });
    await createScopedCredentials("user_A", { env, stsClient });
    await createScopedCredentials("user_B", { env, stsClient });
    const names = stsClient.send.mock.calls.map(
      (call) => call.arguments[0].input.RoleSessionName,
    );

    assert.equal(names[0], names[1]);
    assert.notEqual(names[0], names[2]);
    assert.ok(names.every((name) => name.length <= 64));
  });

  it("does not accept EXECUTION_ROLE_ARN as a fallback", async () => {
    delete env.WORKSPACE_SESSION_ROLE_ARN;
    env.EXECUTION_ROLE_ARN =
      `arn:aws:iam::${ACCOUNT}:role/personal-operator-execution`;
    await assert.rejects(
      () => createScopedCredentials("user_A", { env, stsClient }),
      /WORKSPACE_SESSION_ROLE_ARN/,
    );
    assert.equal(stsClient.send.mock.calls.length, 0);
  });

  it("rejects a workspace role and CMK from different accounts before STS", async () => {
    const poisoned = {
      ...env,
      WORKSPACE_SESSION_ROLE_ARN:
        "arn:aws:iam::999999999999:role/personal-operator-workspace-session",
    };

    await assert.rejects(
      () => createScopedCredentials("user_A", { env: poisoned, stsClient }),
      /CMK account must equal the caller account/i,
    );
    assert.equal(stsClient.send.mock.calls.length, 0);
  });

  it("rejects malformed role and non-key KMS ARNs before STS", async () => {
    for (const poisoned of [
      { ...env, WORKSPACE_SESSION_ROLE_ARN: "arn:aws:iam::123:role/bad" },
      {
        ...env,
        CMK_ARN: `arn:aws:kms:eu-west-1:${ACCOUNT}:alias/workspace`,
      },
    ]) {
      await assert.rejects(
        () => createScopedCredentials("user_A", { env: poisoned, stsClient }),
        /role|CMK|key|ARN/i,
      );
    }
    assert.equal(stsClient.send.mock.calls.length, 0);
  });

  it("fails before STS for missing configuration or an explicit wrong region", async () => {
    for (const key of [
      "S3_USER_FILES_BUCKET",
      "WORKSPACE_SESSION_ROLE_ARN",
      "CMK_ARN",
    ]) {
      const poisoned = { ...env };
      delete poisoned[key];
      await assert.rejects(
        () => createScopedCredentials("user_A", { env: poisoned, stsClient }),
        new RegExp(key),
      );
    }
    for (const poisoned of [
      { ...env, AWS_REGION: "us-west-2" },
      { ...env, AWS_DEFAULT_REGION: "us-west-2" },
    ]) {
      await assert.rejects(
        () => createScopedCredentials("user_A", { env: poisoned, stsClient }),
        /eu-west-1|region/i,
      );
    }
    assert.equal(stsClient.send.mock.calls.length, 0);
  });

  it("fails on malformed, incomplete, or expired STS credentials", async () => {
    for (const credentials of [
      undefined,
      {},
      { ...MOCK_CREDS, AccessKeyId: "" },
      { ...MOCK_CREDS, SecretAccessKey: "" },
      { ...MOCK_CREDS, SessionToken: "" },
      { ...MOCK_CREDS, Expiration: new Date(Date.now() - 1_000) },
    ]) {
      const client = { send: async () => ({ Credentials: credentials }) };
      await assert.rejects(
        () => createScopedCredentials("user_A", { env, stsClient: client }),
        /credentials|expired/i,
      );
    }
  });
});

describe("credential files and OpenClaw child environment", () => {
  let tmpDir;
  const credentials = {
    accessKeyId: MOCK_CREDS.AccessKeyId,
    secretAccessKey: MOCK_CREDS.SecretAccessKey,
    sessionToken: MOCK_CREDS.SessionToken,
    expiration: MOCK_CREDS.Expiration,
  };

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "workspace-creds-"));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it("writes complete credentials and exact-region config atomically at 0600", () => {
    writeCredentialFiles(credentials, tmpDir, {
      AWS_REGION: "eu-west-1",
      AWS_DEFAULT_REGION: "eu-west-1",
    });
    const credsPath = path.join(tmpDir, "scoped-creds.json");
    const configPath = path.join(tmpDir, "scoped-aws-config");
    assert.equal(fs.statSync(credsPath).mode & 0o777, 0o600);
    assert.equal(fs.statSync(configPath).mode & 0o777, 0o600);
    assert.deepEqual(
      JSON.parse(fs.readFileSync(credsPath, "utf8")),
      {
        Version: 1,
        AccessKeyId: credentials.accessKeyId,
        SecretAccessKey: credentials.secretAccessKey,
        SessionToken: credentials.sessionToken,
        Expiration: credentials.expiration.toISOString(),
      },
    );
    assert.match(fs.readFileSync(configPath, "utf8"), /region = eu-west-1/);
    assert.equal(fs.readdirSync(tmpDir).some((file) => file.endsWith(".tmp")), false);
  });

  it("forces the credential directory to 0700", () => {
    fs.chmodSync(tmpDir, 0o755);
    writeCredentialFiles(credentials, tmpDir);

    assert.equal(fs.statSync(tmpDir).mode & 0o777, 0o700);
  });

  it("atomically replaces credentials during refresh", () => {
    writeCredentialFiles(credentials, tmpDir);
    const refreshed = {
      ...credentials,
      accessKeyId: "ASIAREFRESHED",
      expiration: new Date(Date.now() + 45 * 60 * 1000),
    };
    writeCredentialFiles(refreshed, tmpDir);
    const document = JSON.parse(
      fs.readFileSync(path.join(tmpDir, "scoped-creds.json"), "utf8"),
    );

    assert.equal(document.AccessKeyId, "ASIAREFRESHED");
    assert.equal(fs.readdirSync(tmpDir).some((file) => file.endsWith(".tmp")), false);
  });

  it("fails closed on incomplete credentials, missing directory, or wrong region", () => {
    assert.throws(
      () => writeCredentialFiles({ ...credentials, sessionToken: "" }, tmpDir),
      /credentials/i,
    );
    assert.throws(
      () =>
        writeCredentialFiles(credentials, "", {
          AWS_REGION: "eu-west-1",
        }),
      /directory/i,
    );
    assert.throws(
      () =>
        writeCredentialFiles(credentials, tmpDir, {
          AWS_REGION: "us-west-2",
        }),
      /eu-west-1|region/i,
    );
  });

  it("builds an exact scoped environment with ambient providers disabled", () => {
    const result = buildOpenClawEnv({
      credDir: "/tmp/scoped",
      baseEnv: {
        PATH: "/tmp/attacker-bin",
        HOME: "/tmp/attacker-home",
        NODE_PATH: "/tmp/attacker-modules",
        NODE_OPTIONS: "--require /tmp/evil.js",
        AWS_REGION: "eu-west-1",
        AWS_DEFAULT_REGION: "eu-west-1",
        S3_USER_FILES_BUCKET: "personal-operator-workspace",
        AWS_ACCESS_KEY_ID: "full-role-key",
        AWS_SECRET_ACCESS_KEY: "full-role-secret",
        AWS_SESSION_TOKEN: "full-role-token",
        AWS_CONTAINER_CREDENTIALS_RELATIVE_URI: "/v2/credentials/full-role",
        AWS_CONTAINER_AUTHORIZATION_TOKEN: "container-token",
        AWS_PROFILE: "admin",
        USER_ID: "telegram:123",
        INTERNAL_USER_ID: "victim",
        EVENTBRIDGE_ROLE_ARN: "scheduler-role",
        IDENTITY_TABLE_NAME: "identity-table",
      },
    });

    assert.deepEqual(result, {
      PATH: "/usr/local/bin:/usr/bin:/bin",
      HOME: "/root",
      NODE_PATH: "/app/node_modules",
      NODE_OPTIONS:
        "--dns-result-order=ipv4first --no-network-family-autoselection -r /app/force-ipv4.js",
      AWS_REGION: "eu-west-1",
      AWS_DEFAULT_REGION: "eu-west-1",
      S3_USER_FILES_BUCKET: "personal-operator-workspace",
      AWS_CONFIG_FILE: "/tmp/scoped/scoped-aws-config",
      AWS_SDK_LOAD_CONFIG: "1",
      AWS_EC2_METADATA_DISABLED: "true",
      AWS_SHARED_CREDENTIALS_FILE: "/dev/null",
      PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE:
        "/tmp/scoped/scoped-creds.json",
      OPENCLAW_SKIP_CRON: "1",
    });
  });

  it("rejects a missing credential directory or explicit region poison", () => {
    assert.throws(
      () => buildOpenClawEnv({ credDir: "", baseEnv: {} }),
      /credential directory/i,
    );
    assert.throws(
      () =>
        buildOpenClawEnv({
          credDir: "/tmp/scoped",
          baseEnv: { AWS_REGION: "us-west-2" },
        }),
      /eu-west-1|region/i,
    );
  });

  it("rejects an invalid workspace bucket before building child env", () => {
    assert.throws(
      () =>
        buildOpenClawEnv({
          credDir: "/tmp/scoped",
          baseEnv: {
            AWS_REGION: "eu-west-1",
            S3_USER_FILES_BUCKET: "victim/*",
          },
        }),
      /bucket/i,
    );
  });
});
