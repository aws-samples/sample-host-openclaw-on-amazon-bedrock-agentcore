/**
 * Telegram groups on release/v1: a group is its own identity
 * (telegram_group:<chat_id>), so the proxy must keep that actor instead of
 * falling back to the shared default-user namespace, tell the model the chat
 * is shared, and the cron skill must refuse to schedule tasks for a group.
 * All IDs are placeholders.
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const PROXY_SOURCE = fs.readFileSync(path.join(__dirname, "agentcore-proxy.js"), "utf8");
const GROUP_ACTOR = "telegram_group:-1001234567890";
const GROUP_NAMESPACE = "telegram_group_-1001234567890";

function proxyActorRegex() {
  const match = PROXY_SOURCE.match(/const VALID_ACTOR_ID =\s*(\/.+\/);/);
  assert.ok(match, "VALID_ACTOR_ID not found in agentcore-proxy.js");
  return eval(match[1]); // eslint-disable-line no-eval -- a regex literal from our own source
}

describe("Telegram group identity in the proxy", () => {
  it("accepts the group actor, so it never falls back to default-user", () => {
    const re = proxyActorRegex();
    assert.ok(re.test(GROUP_ACTOR));
    assert.ok(re.test("telegram:123456789"));
    for (const bad of ["telegram_group:-100 1", "telegram_group:", "telegramgroup:-100", "group:-100"]) {
      assert.ok(!re.test(bad), bad);
    }
  });

  it("names the group channel and adds the shared-group rules only for groups", () => {
    const channels = PROXY_SOURCE.match(/const VALID_CHANNELS = new Set\(\[([\s\S]*?)\]\);/);
    assert.ok(channels && /"telegram_group"/.test(channels[1]));
    assert.match(PROXY_SOURCE, /const GROUP_CHANNELS = new Set\(\["telegram_group"\]\);/);
    assert.match(PROXY_SOURCE, /groupContext\(safeChannel\) \+/);
    assert.match(PROXY_SOURCE, /Scheduled tasks \(cron\) are not available in group chats/);
  });
});

describe("eventbridge-cron create.js in a Telegram group", () => {
  // Runs common.js and create.js with stubbed AWS clients that record every call.
  async function create(args, extraEnv = {}) {
    const calls = [];
    const errors = [];
    class Command { constructor(input) { this.input = input; calls.push(input); } }
    const env = {
      AWS_REGION: "us-west-2", CRON_LAMBDA_ARN: "arn:aws:lambda:us-west-2:123456789012:function:cron",
      EVENTBRIDGE_ROLE_ARN: "arn:aws:iam::123456789012:role/scheduler", IDENTITY_TABLE_NAME: "identity",
      ...extraEnv,
    };
    let exitCode = null;
    const processStub = {
      env, argv: ["node", "create.js", ...args],
      exit(code) { exitCode = code; throw new Error(`exit ${code}`); },
    };
    const requireStub = (name) => {
      if (name === "@aws-sdk/client-dynamodb") return { DynamoDBClient: class {} };
      if (name === "@aws-sdk/lib-dynamodb") {
        return {
          DynamoDBDocumentClient: { from: () => ({ async send(command) { calls.push(command.input); } }) },
          PutCommand: Command, GetCommand: Command, DeleteCommand: Command, QueryCommand: Command, UpdateCommand: Command,
        };
      }
      if (name === "@aws-sdk/client-scheduler") {
        return { SchedulerClient: class { async send() { return {}; } }, CreateScheduleCommand: Command };
      }
      return require(name);
    };
    const base = { process: processStub, console: { log() {}, error(message) { errors.push(message); } } };
    const common = { ...base, module: { exports: {} }, require: requireStub };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, "skills/eventbridge-cron/common.js"), "utf8"), common);
    try {
      await vm.runInNewContext(fs.readFileSync(path.join(__dirname, "skills/eventbridge-cron/create.js"), "utf8"), {
        ...base, require: (name) => (name === "./common" ? common.module.exports : requireStub(name)),
      });
      await new Promise((resolve) => setImmediate(resolve));
    } catch (err) {
      if (!/^exit /.test(err.message)) throw err;
    }
    return { exitCode, errors, calls };
  }

  for (const [name, args, extraEnv] of [
    ["the group namespace", [GROUP_NAMESPACE, "rate(1 hour)", "UTC", "remind us"], {}],
    ["a group session's CHANNEL", ["telegram_123456789", "rate(1 hour)", "UTC", "hi"], { CHANNEL: "telegram_group" }],
    ["an explicit group channel", ["telegram_123456789", "rate(1 hour)", "UTC", "hi", "telegram_group", "-100"], {}],
  ]) {
    it(`refuses ${name} before any AWS call`, async () => {
      const { exitCode, errors, calls } = await create(args, extraEnv);
      assert.equal(exitCode, 1);
      assert.match(errors.join(" "), /not available in group chats/);
      assert.deepEqual(calls, []);
    });
  }

  it("still creates a schedule for a private chat", async () => {
    const { exitCode, calls } = await create(["telegram_123456789", "rate(1 hour)", "UTC", "hi"], { CHANNEL: "telegram" });
    assert.equal(exitCode, null);
    assert.ok(calls.some((input) => input.Target && input.ScheduleExpression === "rate(1 hour)"));
  });
});
