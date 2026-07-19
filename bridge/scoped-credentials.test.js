"use strict";

const { describe, it, beforeEach, afterEach, mock } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  createScopedCredentials,
  writeCredentialFiles,
  buildOpenClawEnv,
} = require("./scoped-credentials");

const MOCK_CREDS = {
  AccessKeyId: "ASIAIOSFODNN7EXAMPLE",
  SecretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
  SessionToken: "FwoGZXIvYXdzEBYaDH...",
  Expiration: new Date(Date.now() + 10 * 60 * 1000),
};

describe("trusted broker credential issuance", () => {
  let env;
  let lambdaClient;

  beforeEach(() => {
    env = {
      AWS_REGION: "eu-west-1",
      AWS_DEFAULT_REGION: "eu-west-1",
      WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME:
        "personal-operator-workspace-credential-broker",
    };
    lambdaClient = {
      send: mock.fn(async () => ({
        StatusCode: 200,
        Payload: Buffer.from(
          JSON.stringify({
            Version: 1,
            AccessKeyId: MOCK_CREDS.AccessKeyId,
            SecretAccessKey: MOCK_CREDS.SecretAccessKey,
            SessionToken: MOCK_CREDS.SessionToken,
            Expiration: MOCK_CREDS.Expiration.toISOString(),
          }),
        ),
      })),
    };
  });

  it("sends only the opaque capability to the exact trusted broker", async () => {
    const result = await createScopedCredentials("signed.capability", {
      env,
      lambdaClient,
    });
    assert.equal(result.accessKeyId, MOCK_CREDS.AccessKeyId);
    assert.equal(lambdaClient.send.mock.calls.length, 1);
    const input = lambdaClient.send.mock.calls[0].arguments[0].input;
    assert.deepEqual(input, {
      FunctionName: "personal-operator-workspace-credential-broker",
      InvocationType: "RequestResponse",
      Payload: Buffer.from(JSON.stringify({ capability: "signed.capability" })),
    });
    assert.doesNotMatch(JSON.stringify(input), /RoleArn|Policy|namespace|user_A/);
  });

  it("requires exact broker configuration and rejects ambient legacy role inputs", async () => {
    delete env.WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME;
    env.WORKSPACE_SESSION_ROLE_ARN = "arn:aws:iam::123456789012:role/legacy";
    env.CMK_ARN = "arn:aws:kms:eu-west-1:123456789012:key/legacy";
    await assert.rejects(
      () => createScopedCredentials("signed.capability", { env, lambdaClient }),
      /WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME/,
    );
    assert.equal(lambdaClient.send.mock.calls.length, 0);
  });

  it("fails before broker invocation for malformed capability or wrong region", async () => {
    for (const capability of ["", "x".repeat(2049), "not ascii 💥"]) {
      await assert.rejects(
        () => createScopedCredentials(capability, { env, lambdaClient }),
        /capability/i,
      );
    }
    for (const poisoned of [
      { ...env, AWS_REGION: "us-west-2" },
      { ...env, AWS_DEFAULT_REGION: "us-west-2" },
    ]) {
      await assert.rejects(
        () => createScopedCredentials("signed.capability", {
          env: poisoned,
          lambdaClient,
        }),
        /eu-west-1|region/i,
      );
    }
    assert.equal(lambdaClient.send.mock.calls.length, 0);
  });

  it("fails closed on broker function errors and malformed credentials", async () => {
    for (const response of [
      { StatusCode: 500, Payload: Buffer.from("{}") },
      { StatusCode: 200, FunctionError: "Unhandled", Payload: Buffer.from("{}") },
      { StatusCode: 200, Payload: Buffer.from("not-json") },
      { StatusCode: 200, Payload: Buffer.from("{}") },
      {
        StatusCode: 200,
        Payload: Buffer.from(JSON.stringify({
          Version: 1,
          AccessKeyId: MOCK_CREDS.AccessKeyId,
          SecretAccessKey: MOCK_CREDS.SecretAccessKey,
          SessionToken: MOCK_CREDS.SessionToken,
          Expiration: new Date(Date.now() + 16 * 60 * 1000).toISOString(),
        })),
      },
    ]) {
      const client = { send: async () => response };
      await assert.rejects(
        () => createScopedCredentials("signed.capability", { env, lambdaClient: client }),
        /broker|credentials|response/i,
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
      expiration: new Date(Date.now() + 8 * 60 * 1000),
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
      HOME: "/run/personal-operator/home",
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
