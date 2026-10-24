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
