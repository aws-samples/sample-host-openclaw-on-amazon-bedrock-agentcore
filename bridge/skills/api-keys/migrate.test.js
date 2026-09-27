/**
 * Tests for the api-keys skill scripts (migrate.js, native.js).
 * Run: node --test skills/api-keys/migrate.test.js
 *
 * Each script runs in a child process with HOME pointed at a temp dir and a
 * `-r` preload that replaces @aws-sdk/client-secrets-manager with a recorder,
 * so no real AWS call is made. AWS_REGION is set only in the child env.
 */
const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const MIGRATE = path.join(__dirname, "migrate.js");
const NATIVE = path.join(__dirname, "native.js");
const USER_ID = "telegram_123456";
const KEY_NAME = "demo_key";
const SECRET_VALUE = "sk-test-value";

// Preload stub: records every SecretsManager command to SM_STUB_LOG.
// SM_STUB selects the GetSecretValue response: "string" (default), "binary", "empty".
const STUB_SOURCE = `
const Module = require("module");
const fs = require("fs");
const orig = Module._load;
const log = (o) => fs.appendFileSync(process.env.SM_STUB_LOG, JSON.stringify(o) + "\\n");
Module._load = function (req, ...rest) {
  if (req === "@aws-sdk/client-secrets-manager") {
    const mk = (name) => class { constructor(input) { this.name = name; this.input = input; } };
    return {
      SecretsManagerClient: class {
        async send(cmd) {
          log({ cmd: cmd.name, input: cmd.input });
          if (cmd.name === "GetSecretValueCommand") {
            const mode = process.env.SM_STUB || "string";
            if (mode === "binary") return { SecretBinary: Buffer.from("x") };
            if (mode === "empty") return { SecretString: "" };
            return { SecretString: ${JSON.stringify(SECRET_VALUE)} };
          }
          return {};
        }
      },
      GetSecretValueCommand: mk("GetSecretValueCommand"),
      PutSecretValueCommand: mk("PutSecretValueCommand"),
      CreateSecretCommand: mk("CreateSecretCommand"),
      DeleteSecretCommand: mk("DeleteSecretCommand"),
    };
  }
  return orig.call(this, req, ...rest);
};
`;

let tmp;
let stubPath;
let logPath;
let keysPath;

function run(script, args, extraEnv = {}) {
  const env = {
    PATH: process.env.PATH,
    HOME: tmp,
    AWS_REGION: "us-west-2",
    SM_STUB_LOG: logPath,
    ...extraEnv,
  };
  return spawnSync(process.execPath, ["-r", stubPath, script, ...args], {
    env,
    encoding: "utf-8",
    timeout: 20_000,
  });
}

function smCalls() {
  if (!fs.existsSync(logPath)) return [];
  return fs.readFileSync(logPath, "utf-8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l));
}

function writeNative(content) {
  fs.mkdirSync(path.dirname(keysPath), { recursive: true });
  fs.writeFileSync(keysPath, content);
}

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "api-keys-test-"));
  stubPath = path.join(tmp, "sm-stub.js");
  logPath = path.join(tmp, "sm-calls.log");
  keysPath = path.join(tmp, ".openclaw", "user-api-keys.json");
  fs.writeFileSync(stubPath, STUB_SOURCE);
});

afterEach(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
});

describe("migrate.js secure-to-native", () => {
  it("schedules deletion with a recovery window instead of force-deleting", () => {
    writeNative(JSON.stringify({ other_key: "keep-me" }));
    const r = run(MIGRATE, [USER_ID, KEY_NAME, "secure-to-native"]);
    assert.equal(r.status, 0, r.stderr);

    const keys = JSON.parse(fs.readFileSync(keysPath, "utf-8"));
    assert.deepEqual(keys, { other_key: "keep-me", demo_key: SECRET_VALUE });

    const del = smCalls().filter((c) => c.cmd === "DeleteSecretCommand");
    assert.equal(del.length, 1);
    assert.equal(del[0].input.SecretId, `openclaw/user/${USER_ID}/${KEY_NAME}`);
    assert.equal(del[0].input.RecoveryWindowInDays, 7);
    assert.equal("ForceDeleteWithoutRecovery" in del[0].input, false);
  });

  it("creates the native file when none exists", () => {
    const r = run(MIGRATE, [USER_ID, KEY_NAME, "secure-to-native"]);
    assert.equal(r.status, 0, r.stderr);
    assert.deepEqual(JSON.parse(fs.readFileSync(keysPath, "utf-8")), { demo_key: SECRET_VALUE });
    assert.equal(smCalls().filter((c) => c.cmd === "DeleteSecretCommand").length, 1);
  });

  for (const mode of ["binary", "empty"]) {
    it(`refuses a secret without a usable SecretString (${mode}): no write, no delete`, () => {
      const before = JSON.stringify({ other_key: "keep-me" });
      writeNative(before);
      const r = run(MIGRATE, [USER_ID, KEY_NAME, "secure-to-native"], { SM_STUB: mode });
      assert.notEqual(r.status, 0, "expected non-zero exit");
      assert.equal(fs.readFileSync(keysPath, "utf-8"), before);
      assert.equal(smCalls().filter((c) => c.cmd === "DeleteSecretCommand").length, 0);
    });
  }

  it("refuses to overwrite an unreadable native file: bytes unchanged, no delete", () => {
    const corrupt = '{"other_key":"keep-me"';
    writeNative(corrupt);
    const r = run(MIGRATE, [USER_ID, KEY_NAME, "secure-to-native"]);
    assert.notEqual(r.status, 0, "expected non-zero exit");
    assert.equal(fs.readFileSync(keysPath, "utf-8"), corrupt);
    assert.equal(smCalls().filter((c) => c.cmd === "DeleteSecretCommand").length, 0);
  });
});

describe("migrate.js native-to-secure", () => {
  it("moves the key and keeps the other native keys", () => {
    writeNative(JSON.stringify({ other_key: "keep-me", demo_key: SECRET_VALUE }));
    const r = run(MIGRATE, [USER_ID, KEY_NAME, "native-to-secure"]);
    assert.equal(r.status, 0, r.stderr);
    const put = smCalls().filter((c) => c.cmd === "PutSecretValueCommand");
    assert.equal(put.length, 1);
    assert.equal(put[0].input.SecretString, SECRET_VALUE);
    assert.deepEqual(JSON.parse(fs.readFileSync(keysPath, "utf-8")), { other_key: "keep-me" });
  });
});

describe("native.js set", () => {
  it("refuses to overwrite an unreadable native file", () => {
    const corrupt = '{"other_key":"keep-me"';
    writeNative(corrupt);
    const r = run(NATIVE, [USER_ID, "set", KEY_NAME, SECRET_VALUE]);
    assert.notEqual(r.status, 0, "expected non-zero exit");
    assert.equal(fs.readFileSync(keysPath, "utf-8"), corrupt);
  });

  it("adds a key alongside existing ones", () => {
    writeNative(JSON.stringify({ other_key: "keep-me" }));
    const r = run(NATIVE, [USER_ID, "set", KEY_NAME, SECRET_VALUE]);
    assert.equal(r.status, 0, r.stderr);
    assert.deepEqual(JSON.parse(fs.readFileSync(keysPath, "utf-8")), { other_key: "keep-me", demo_key: SECRET_VALUE });
  });

  it("creates the native file when none exists", () => {
    const r = run(NATIVE, [USER_ID, "set", KEY_NAME, SECRET_VALUE]);
    assert.equal(r.status, 0, r.stderr);
    assert.deepEqual(JSON.parse(fs.readFileSync(keysPath, "utf-8")), { demo_key: SECRET_VALUE });
  });
});
