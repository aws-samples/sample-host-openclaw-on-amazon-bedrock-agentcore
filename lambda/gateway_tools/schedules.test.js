/**
 * schedules target: minimum interval and schedule names for long namespaces.
 * Run: node --test lambda/gateway_tools/
 */
"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const h = require("./test-helpers");
const { createVerifier, RESERVED_ARG } = require("./lib/identity");
const cron = require("./eventbridge_cron/index");

const verifier = createVerifier({ issuer: h.ISSUER, clientId: h.CLIENT_ID, fetchJwks: h.fakeFetchJwks });
// Two namespaces that share their first 46 characters.
const LONG_A = `telegram:${"a".repeat(37)}owner`;
const LONG_B = `telegram:${"a".repeat(37)}other`;
const NS_A = LONG_A.replace(/:/g, "_");
const NS_B = LONG_B.replace(/:/g, "_");

function notFound() {
  const err = new Error("not found");
  err.name = "ResourceNotFoundException";
  return Promise.reject(err);
}

function handlerFor(actorId, { schedules = {} } = {}) {
  const profile = { Item: { PK: `CHANNEL#${actorId}`, SK: "PROFILE", userId: "user_abc" } };
  const owned = { Item: { PK: "USER#user_abc", SK: "CRON#deadbeef", scheduleId: "deadbeef", enabled: true } };
  const scheduler = h.fakeClient((cmd) => {
    if (cmd.kind === "GetScheduleCommand" || cmd.kind === "DeleteScheduleCommand") {
      const found = schedules[cmd.input.Name];
      if (!found) return notFound();
      return cmd.kind === "GetScheduleCommand" ? found : {};
    }
    return {};
  });
  const ddb = h.fakeClient((cmd) => (cmd.kind === "GetCommand" ? (cmd.input.Key.SK === "PROFILE" ? profile : owned) : {}));
  const handler = cron.createHandler({
    log: () => {},
    verifier,
    scheduler,
    schedulerCommands: h.fakeCommands("CreateScheduleCommand", "GetScheduleCommand", "UpdateScheduleCommand", "DeleteScheduleCommand"),
    ddb,
    ddbCommands: h.fakeCommands("GetCommand", "PutCommand", "QueryCommand", "UpdateCommand", "DeleteCommand"),
    cronLambdaArn: "arn:aws:lambda:us-west-2:123456789012:function:openclaw-cron-executor",
    schedulerRoleArn: "arn:aws:iam::123456789012:role/openclaw-cron-scheduler-role-us-west-2",
    tableName: "openclaw-identity",
    newId: () => "deadbeef",
  });
  return { scheduler, handler, token: h.idToken(actorId) };
}

function schedule(actorId) {
  return {
    ScheduleExpression: "rate(1 hour)", ScheduleExpressionTimezone: "UTC", Description: "d",
    Target: { Input: JSON.stringify({ actorId, message: "old" }) },
  };
}

describe("schedules minimum interval", () => {
  it("rejects every cron() minutes field that runs more often than every 5 minutes", async () => {
    const { scheduler, handler, token } = handlerFor("telegram:123456");
    const ctx = h.gatewayContext("schedules", "create_schedule");
    for (const minutes of ["*", "*/1", "*/2", "*/4", "0-59", "0/1", "0-59/1", "0-59/4", "0,1,2", "0,30,32", "0,58", "1-3"]) {
      const out = await handler(
        { expression: `cron(${minutes} * * * ? *)`, timezone: "UTC", message: "m", [RESERVED_ARG]: token }, ctx,
      );
      assert.match(out.message || "", /Every-minute/, minutes);
    }
    for (const minutes of ["a", "60", "5-1", "0/0", "L", "0-60/5"]) {
      const out = await handler(
        { expression: `cron(${minutes} * * * ? *)`, timezone: "UTC", message: "m", [RESERVED_ARG]: token }, ctx,
      );
      assert.match(out.message || "", /Unsupported cron\(\) minutes field/, minutes);
    }
    assert.equal(scheduler.sent.length, 0);
  });

  it("still accepts schedules 5 or more minutes apart", async () => {
    for (const minutes of ["0", "30", "*/5", "0/5", "0-59/5", "0,30", "0,15,30,45", "10-20/5", "5,10"]) {
      const { scheduler, handler, token } = handlerFor("telegram:123456");
      const out = await handler(
        { expression: `cron(${minutes} 9 * * ? *)`, timezone: "UTC", message: "m", [RESERVED_ARG]: token },
        h.gatewayContext("schedules", "create_schedule"),
      );
      assert.equal(out.schedule_id, "deadbeef", minutes);
      assert.ok(scheduler.sent.some((c) => c.kind === "CreateScheduleCommand"), minutes);
    }
  });

  it("measures the gap between minutes, including across the hour", () => {
    assert.equal(cron.cronMinuteGap("0"), 60);
    assert.equal(cron.cronMinuteGap("*/15"), 15);
    assert.equal(cron.cronMinuteGap("0,58"), 2);
    assert.equal(cron.cronMinuteGap("10-20/5"), 5);
    assert.equal(cron.cronMinuteGap("0-59"), 1);
    assert.equal(cron.cronMinuteGap("1,2,x"), null);
  });
});

describe("schedule names", () => {
  it("keeps names of namespaces up to 46 characters unchanged", () => {
    assert.equal(cron.buildScheduleName("telegram_123456", "deadbeef"), "openclaw-telegram_123456-deadbeef");
    const ns46 = `feishu_${"x".repeat(39)}`;
    assert.equal(cron.buildScheduleName(ns46, "deadbeef"), `openclaw-${ns46}-deadbeef`);
    assert.equal(cron.legacyScheduleName(ns46, "deadbeef"), null);
  });

  it("gives two long namespaces with the same first 46 characters different names of at most 64 characters", () => {
    assert.equal(NS_A.slice(0, 46), NS_B.slice(0, 46));
    const a = cron.buildScheduleName(NS_A, "deadbeef");
    const b = cron.buildScheduleName(NS_B, "deadbeef");
    assert.notEqual(a, b);
    assert.ok(a.length <= 64 && b.length <= 64);
    assert.equal(cron.legacyScheduleName(NS_A, "deadbeef"), cron.legacyScheduleName(NS_B, "deadbeef"));
  });

  it("creates long-namespace schedules under the new name", async () => {
    const { scheduler, handler, token } = handlerFor(LONG_A);
    await handler(
      { expression: "rate(1 hour)", timezone: "UTC", message: "m", [RESERVED_ARG]: token },
      h.gatewayContext("schedules", "create_schedule"),
    );
    const create = scheduler.sent.find((c) => c.kind === "CreateScheduleCommand").input;
    assert.equal(create.Name, cron.buildScheduleName(NS_A, "deadbeef"));
  });

  it("updates and deletes an existing schedule under its old truncated name when it is the caller's", async () => {
    const legacy = cron.legacyScheduleName(NS_A, "deadbeef");
    const { scheduler, handler, token } = handlerFor(LONG_A, { schedules: { [legacy]: schedule(LONG_A) } });
    let out = await handler(
      { schedule_id: "deadbeef", message: "new", [RESERVED_ARG]: token }, h.gatewayContext("schedules", "update_schedule"),
    );
    assert.deepEqual(out.updated, ["message"]);
    assert.equal(scheduler.sent.find((c) => c.kind === "UpdateScheduleCommand").input.Name, legacy);
    out = await handler({ schedule_id: "deadbeef", [RESERVED_ARG]: token }, h.gatewayContext("schedules", "delete_schedule"));
    assert.deepEqual(out, { deleted: "deadbeef" });
    assert.ok(scheduler.sent.some((c) => c.kind === "DeleteScheduleCommand" && c.input.Name === legacy));
  });

  it("never updates or deletes a schedule under the shared old name that another namespace created", async () => {
    const legacy = cron.legacyScheduleName(NS_A, "deadbeef");
    const { scheduler, handler, token } = handlerFor(LONG_A, { schedules: { [legacy]: schedule(LONG_B) } });
    const out = await handler(
      { schedule_id: "deadbeef", message: "new", [RESERVED_ARG]: token }, h.gatewayContext("schedules", "update_schedule"),
    );
    assert.equal(out.error, "error");
    await handler({ schedule_id: "deadbeef", [RESERVED_ARG]: token }, h.gatewayContext("schedules", "delete_schedule"));
    const touched = scheduler.sent.filter((c) => c.kind !== "GetScheduleCommand" && c.input.Name === legacy);
    assert.deepEqual(touched, []);
  });

  it("prefers the new name when both exist", async () => {
    const name = cron.buildScheduleName(NS_A, "deadbeef");
    const legacy = cron.legacyScheduleName(NS_A, "deadbeef");
    const { scheduler, handler, token } = handlerFor(LONG_A, {
      schedules: { [name]: schedule(LONG_A), [legacy]: schedule(LONG_A) },
    });
    await handler({ schedule_id: "deadbeef", [RESERVED_ARG]: token }, h.gatewayContext("schedules", "delete_schedule"));
    const deletes = scheduler.sent.filter((c) => c.kind === "DeleteScheduleCommand").map((c) => c.input.Name);
    assert.deepEqual(deletes, [name]);
  });
});

describe("schedules in group chats", () => {
  it("refuses to create a schedule for a Telegram group identity", async () => {
    const { scheduler, handler, token } = handlerFor("telegram_group:-1001234567890");
    const out = await handler(
      { expression: "rate(1 hour)", timezone: "UTC", message: "m", [RESERVED_ARG]: token },
      h.gatewayContext("schedules", "create_schedule"),
    );
    assert.match(out.message || "", /not available in group chats/);
    assert.equal(scheduler.sent.length, 0);
  });
});
