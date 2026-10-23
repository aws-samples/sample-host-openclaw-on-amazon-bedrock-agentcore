/**
 * eventbridge-cron skill: minimum interval and schedule names for long userIds.
 * Run: cd bridge/skills/eventbridge-cron && node --test common.test.js
 * (no AWS calls: the scheduler client is a fake)
 */
"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

process.env.AWS_REGION = process.env.AWS_REGION || "us-west-2";
const common = require("./common");

const LONG_A = `telegram_${"a".repeat(37)}owner`;
const LONG_B = `telegram_${"a".repeat(37)}other`;

function exitsWith(fn) {
  const errors = [];
  const { exit } = process;
  const { error } = console;
  process.exit = (code) => { throw Object.assign(new Error("exit"), { code }); };
  console.error = (msg) => errors.push(String(msg));
  try {
    fn();
    return null;
  } catch (err) {
    if (err.message !== "exit") throw err;
    return errors.join("\n");
  } finally {
    process.exit = exit;
    console.error = error;
  }
}

function fakeScheduler(schedules) {
  const sent = [];
  return {
    sent,
    async send(cmd) {
      sent.push(cmd.input.Name);
      if (schedules[cmd.input.Name]) return schedules[cmd.input.Name];
      throw Object.assign(new Error("not found"), { name: "ResourceNotFoundException" });
    },
  };
}

const scheduleFor = (userId) => ({ Target: { Input: JSON.stringify({ actorId: userId.replace("_", ":") }) } });

describe("validateExpression minimum interval", () => {
  it("rejects every cron() minutes field that runs more often than every 5 minutes", () => {
    for (const minutes of ["*", "*/1", "*/2", "0-59", "0/1", "0-59/4", "0,1,2", "0,58"]) {
      assert.match(exitsWith(() => common.validateExpression(`cron(${minutes} * * * ? *)`)) || "", /Every-minute/, minutes);
    }
    for (const minutes of ["a", "60", "5-1", "0/0"]) {
      assert.match(exitsWith(() => common.validateExpression(`cron(${minutes} * * * ? *)`)) || "",
        /Unsupported cron\(\) minutes field/, minutes);
    }
  });

  it("still accepts schedules 5 or more minutes apart", () => {
    for (const minutes of ["0", "*/5", "0/5", "0-59/5", "0,30", "10-20/5"]) {
      assert.equal(exitsWith(() => common.validateExpression(`cron(${minutes} 9 * * ? *)`)), null, minutes);
    }
  });
});

describe("schedule names", () => {
  it("keeps names of userIds up to 46 characters unchanged", () => {
    assert.equal(common.buildScheduleName("telegram_123456", "deadbeef"), "openclaw-telegram_123456-deadbeef");
    assert.equal(common.legacyScheduleName("telegram_123456", "deadbeef"), null);
  });

  it("gives two long userIds with the same first 46 characters different names", () => {
    const a = common.buildScheduleName(LONG_A, "deadbeef");
    const b = common.buildScheduleName(LONG_B, "deadbeef");
    assert.notEqual(a, b);
    assert.ok(a.length <= 64 && b.length <= 64);
  });

  it("finds the caller's existing schedule under its old truncated name", async () => {
    const legacy = common.legacyScheduleName(LONG_A, "deadbeef");
    const client = fakeScheduler({ [legacy]: scheduleFor(LONG_A) });
    assert.equal(await common.resolveScheduleName(client, LONG_A, "deadbeef"), legacy);
  });

  it("does not use the shared old name when another userId created that schedule", async () => {
    const legacy = common.legacyScheduleName(LONG_A, "deadbeef");
    const client = fakeScheduler({ [legacy]: scheduleFor(LONG_B) });
    assert.equal(await common.resolveScheduleName(client, LONG_A, "deadbeef"),
      common.buildScheduleName(LONG_A, "deadbeef"));
  });

  it("makes no scheduler call for a userId of up to 46 characters", async () => {
    const client = fakeScheduler({});
    assert.equal(await common.resolveScheduleName(client, "telegram_123456", "deadbeef"), "openclaw-telegram_123456-deadbeef");
    assert.deepEqual(client.sent, []);
  });
});
