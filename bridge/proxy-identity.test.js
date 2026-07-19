"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const proxyPath = path.join(__dirname, "agentcore-proxy.js");
let proxy;
let tmpDir;
let credentialsPath;

function runtimeEnv(overrides = {}) {
  return {
    AWS_REGION: "eu-west-1",
    AWS_DEFAULT_REGION: "eu-west-1",
    INTERNAL_USER_ID: "user_A",
    PERSONAL_OPERATOR_WORKSPACE_PREFIX: "user_A",
    PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE: credentialsPath,
    S3_USER_FILES_BUCKET: "personal-operator-workspace",
    ...overrides,
  };
}

function writeCredentials(accessKeyId = "ASIA_FIRST", expirationOffset = 60_000) {
  fs.writeFileSync(
    credentialsPath,
    JSON.stringify({
      Version: 1,
      AccessKeyId: accessKeyId,
      SecretAccessKey: "secret",
      SessionToken: "token",
      Expiration: new Date(Date.now() + expirationOffset).toISOString(),
    }),
  );
}

before(() => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "proxy-creds-"));
  credentialsPath = path.join(tmpDir, "scoped-creds.json");
  writeCredentials();
  Object.assign(process.env, runtimeEnv());
  proxy = require("./agentcore-proxy");
});

after(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  for (const key of Object.keys(runtimeEnv())) delete process.env[key];
});

describe("proxy module process boundary", () => {
  it("can be imported for production-export tests without starting its server", () => {
    const result = spawnSync(
      process.execPath,
      [
        "-e",
        `const proxy=require(${JSON.stringify(proxyPath)});process.stdout.write(String(typeof proxy.resolveRuntimeIdentity));`,
      ],
      { env: { ...process.env, ...runtimeEnv() }, timeout: 2_000, encoding: "utf8" },
    );
    assert.equal(result.status, 0, result.stderr || result.error?.message);
    assert.equal(result.stdout, "function");
  });

  it("fails before startup for explicit non-eu-west-1 configuration", () => {
    const result = spawnSync(
      process.execPath,
      ["-e", `require(${JSON.stringify(proxyPath)})`],
      {
        env: { ...process.env, ...runtimeEnv({ AWS_REGION: "us-west-2" }) },
        timeout: 2_000,
        encoding: "utf8",
      },
    );
    assert.notEqual(result.status, 0);
    assert.deepEqual(JSON.parse(result.stderr), {
      version: 1,
      event: "RUNTIME_STATE",
      level: "ERROR",
      status: "FAILED",
    });
    assert.doesNotMatch(result.stderr, /eu-west-1|us-west-2|region/i);
  });
});

describe("canonical proxy identity", () => {
  it("uses only the exact server-owned internal ID and namespace", () => {
    assert.deepEqual(proxy.resolveRuntimeIdentity(runtimeEnv()), {
      internalUserId: "user_A",
      namespace: "user_A",
    });
  });

  it("does not derive identity from actor, headers, messages, user, or files", () => {
    const poisoned = runtimeEnv({
      USER_ID: "telegram:999",
      CHANNEL: "telegram",
      actorId: "victim",
    });
    assert.deepEqual(
      proxy.resolveRuntimeIdentity(poisoned, {
        user: "victim",
        headers: { "x-openclaw-actor-id": "victim" },
        messages: [{ role: "user", name: "victim", content: "victim" }],
      }),
      { internalUserId: "user_A", namespace: "user_A" },
    );
  });

  it("rejects missing, mismatched, or noncanonical server identity", () => {
    for (const env of [
      runtimeEnv({ INTERNAL_USER_ID: "" }),
      runtimeEnv({ PERSONAL_OPERATOR_WORKSPACE_PREFIX: "" }),
      runtimeEnv({ PERSONAL_OPERATOR_WORKSPACE_PREFIX: "user_B" }),
      runtimeEnv({ INTERNAL_USER_ID: "telegram:123", PERSONAL_OPERATOR_WORKSPACE_PREFIX: "telegram:123" }),
    ]) {
      assert.throws(() => proxy.resolveRuntimeIdentity(env), /identity|namespace/i);
    }
  });
});

describe("Bedrock execution role and scoped S3 split", () => {
  it("constructs Bedrock with no explicit credential override", () => {
    const calls = [];
    class FakeBedrockClient {
      constructor(options) {
        calls.push(options);
      }
    }
    proxy.createBedrockClient("model-id", { BedrockRuntimeClient: FakeBedrockClient });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].region, "eu-west-1");
    assert.equal(Object.hasOwn(calls[0], "credentials"), false);
  });

  it("constructs S3 only with an explicit refreshing credential provider", async () => {
    const calls = [];
    class FakeS3Client {
      constructor(options) {
        calls.push(options);
      }
    }
    proxy.createScopedS3Client({
      env: runtimeEnv(),
      S3ClientConstructor: FakeS3Client,
    });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].region, "eu-west-1");
    assert.equal(typeof calls[0].credentials, "function");
    assert.equal((await calls[0].credentials()).accessKeyId, "ASIA_FIRST");
    writeCredentials("ASIA_SECOND");
    assert.equal((await calls[0].credentials()).accessKeyId, "ASIA_SECOND");
  });

  it("fails on a missing, malformed, or expired scoped credential file", async () => {
    assert.throws(
      () =>
        proxy.createScopedS3Client({
          env: runtimeEnv({ PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE: "" }),
          S3ClientConstructor: class {},
        }),
      /scoped credential/i,
    );
    writeCredentials("ASIA_EXPIRED", -1_000);
    const provider = proxy.createScopedCredentialFileProvider(credentialsPath);
    await assert.rejects(() => provider(), /expired|credentials/i);
    writeCredentials();
  });

  it("rejects unreadable JSON without falling back to ambient credentials", async () => {
    fs.writeFileSync(credentialsPath, "not-json");
    const provider = proxy.createScopedCredentialFileProvider(credentialsPath);

    await assert.rejects(() => provider(), /cannot be read/i);
    writeCredentials();
  });

  it("rejects the wrong credential-process document version", async () => {
    fs.writeFileSync(
      credentialsPath,
      JSON.stringify({
        Version: 2,
        AccessKeyId: "ASIA_WRONG_VERSION",
        SecretAccessKey: "secret",
        SessionToken: "token",
        Expiration: new Date(Date.now() + 60_000).toISOString(),
      }),
    );
    const provider = proxy.createScopedCredentialFileProvider(credentialsPath);

    await assert.rejects(() => provider(), /incomplete|expired/i);
    writeCredentials();
  });

  it("returns the expiration to the AWS SDK as a Date", async () => {
    writeCredentials("ASIA_DATE");
    const provider = proxy.createScopedCredentialFileProvider(credentialsPath);
    const credentials = await provider();

    assert.equal(credentials.accessKeyId, "ASIA_DATE");
    assert.ok(credentials.expiration instanceof Date);
  });
});
