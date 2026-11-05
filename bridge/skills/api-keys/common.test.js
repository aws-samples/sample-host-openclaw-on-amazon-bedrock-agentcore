/**
 * Tests for api-keys/common.js validateUserId cross-tenant check.
 * Shells out to a harness because validateUserId calls process.exit(1).
 * Run: node --test skills/api-keys/common.test.js
 */
const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const COMMON = path.join(__dirname, "common.js");

const HARNESS = [
  // TEST_IDENTITY_FILE stands in for the contract's /tmp/current-identity.json.
  'const fs = require("node:fs");',
  'const readFileSync = fs.readFileSync;',
  'fs.readFileSync = (file, ...rest) => readFileSync(',
  '  file === "/tmp/current-identity.json" && process.env.TEST_IDENTITY_FILE ? process.env.TEST_IDENTITY_FILE : file,',
  '  ...rest);',
  'const { validateUserId } = require(' + JSON.stringify(COMMON) + ');',
  'validateUserId(process.argv[2]);',
  'console.log("ok");',
].join("\n");

let tmp;
let harnessPath;

function run(args, extraEnv) {
  const env = Object.assign({
    PATH: process.env.PATH,
    AWS_REGION: "us-west-2",
  }, extraEnv || {});
  return spawnSync(process.execPath, [harnessPath].concat(args), {
    env: env,
    encoding: "utf-8",
    timeout: 10000,
  });
}

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "api-keys-common-"));
  harnessPath = path.join(tmp, "harness.js");
  fs.writeFileSync(harnessPath, HARNESS);
});

afterEach(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
});

describe("validateUserId cross-tenant check", () => {
  it("accepts argv userId matching USER_ID", () => {
    const r = run(["telegram_123456"], { USER_ID: "telegram:123456" });
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.stdout, /ok/);
  });

  it("accepts a feishu namespace matching USER_ID", () => {
    const r = run(["feishu_ou_abc123"], { USER_ID: "feishu:ou_abc123" });
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.stdout, /ok/);
  });

  it("rejects every call when no bound identity is available", () => {
    const r = run(["telegram_123456"], {});
    assert.notEqual(r.status, 0);
    assert.match(r.stderr, /identity is not available/);
  });

  it("rejects argv userId targeting a different tenant namespace", () => {
    const r = run(["telegram_999999"], { USER_ID: "telegram:123456" });
    assert.notEqual(r.status, 0);
    assert.match(r.stderr, /does not match the user this session belongs to/);
  });

  it("rejects argv userId with a different channel than the bound one", () => {
    const r = run(["slack_U0ABC"], { USER_ID: "telegram:123456" });
    assert.notEqual(r.status, 0);
    assert.match(r.stderr, /does not match the user this session belongs to/);
  });

  it("still rejects default-user even when env is unset", () => {
    const r = run(["default-user"], {});
    assert.notEqual(r.status, 0);
    assert.match(r.stderr, /default-user/);
  });

  it("still rejects malformed namespaces", () => {
    const r = run(["not_a_valid_channel"], { USER_ID: "telegram:123456" });
    assert.notEqual(r.status, 0);
    assert.match(r.stderr, /Invalid user_id/);
  });
});

describe("validateUserId for Slack channel and Feishu group identities", () => {
  it("accepts a Slack channel namespace matching USER_ID", () => {
    const r = run(["slack_channel_C0123CHAN"], { USER_ID: "slack_channel:C0123CHAN" });
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.stdout, /ok/);
  });

  it("accepts a Feishu group namespace matching USER_ID", () => {
    const r = run(["feishu_group_oc_0123abcdef"], { USER_ID: "feishu_group:oc_0123abcdef" });
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.stdout, /ok/);
  });

  it("rejects a member's own namespace in a channel's session", () => {
    const r = run(["slack_U0123USER"], { USER_ID: "slack_channel:C0123CHAN" });
    assert.notEqual(r.status, 0);
    assert.match(r.stderr, /does not match the user this session belongs to/);
  });
});

describe("validateUserId with a stored namespace in the identity file", () => {
  const OPAQUE = "ns_" + "g".repeat(26);

  function withIdentity(identity, args, extraEnv) {
    const file = path.join(tmp, "identity.json");
    fs.writeFileSync(file, JSON.stringify(identity));
    return run(args, Object.assign({ TEST_IDENTITY_FILE: file }, extraEnv || {}));
  }

  it("accepts the namespace the identity file names for the bound actor", () => {
    const r = withIdentity({ actorId: "telegram:123456", namespace: OPAQUE }, [OPAQUE], { USER_ID: "telegram:123456" });
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.stdout, /ok/);
  });

  it("refuses the old legacy namespace, as taken from an older transcript, with the system-prompt hint", () => {
    const r = withIdentity({ actorId: "telegram:123456", namespace: OPAQUE }, ["telegram_123456"],
      { USER_ID: "telegram:123456" });
    assert.notEqual(r.status, 0);
    assert.match(r.stderr, /Use the user_id from your system prompt/);
  });

  it("refuses another stored namespace and a USER_ID that names another actor", () => {
    let r = withIdentity({ actorId: "telegram:123456", namespace: OPAQUE }, ["ns_" + "h".repeat(26)],
      { USER_ID: "telegram:123456" });
    assert.notEqual(r.status, 0);
    assert.match(r.stderr, /does not match the user this session belongs to/);
    r = withIdentity({ actorId: "telegram:123456", namespace: OPAQUE }, [OPAQUE], { USER_ID: "telegram:999999" });
    assert.notEqual(r.status, 0);
    assert.match(r.stderr, /does not match the user this session belongs to/);
  });

  it("uses the legacy namespace when the identity file names none", () => {
    const r = withIdentity({ actorId: "telegram:123456" }, ["telegram_123456"], { USER_ID: "telegram:123456" });
    assert.equal(r.status, 0, r.stderr);
  });
});

describe("validateUserId for a Feishu group with a stored namespace", () => {
  it("accepts the stored namespace the identity file names for the group", () => {
    const file = path.join(tmp, "identity.json");
    const opaque = "ns_" + "r".repeat(26);
    fs.writeFileSync(file, JSON.stringify({ actorId: "feishu_group:oc_0123abcdef", namespace: opaque }));
    const env = { TEST_IDENTITY_FILE: file, USER_ID: "feishu_group:oc_0123abcdef" };
    const ok = run([opaque], env);
    assert.equal(ok.status, 0, ok.stderr);
    const legacy = run(["feishu_group_oc_0123abcdef"], env);
    assert.notEqual(legacy.status, 0);
    assert.match(legacy.stderr, /Use the user_id from your system prompt/);
  });
});

