/**
 * Unit tests for the user_files and schedules Lambda tool targets.
 * Run: node --test lambda/gateway_tools/
 */
"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const h = require("./test-helpers");
const { createVerifier, RESERVED_ARG } = require("./lib/identity");
const files = require("./s3_user_files/index");
const cron = require("./eventbridge_cron/index");

const schemas = JSON.parse(fs.readFileSync(path.join(__dirname, "tool-schemas.json"), "utf8"));
const verifier = createVerifier({ issuer: h.ISSUER, clientId: h.CLIENT_ID, fetchJwks: h.fakeFetchJwks });
const TOKEN = h.idToken("telegram:123456");

// ---------------------------------------------------------------- user_files

function filesHandler(answer) {
  const s3 = h.fakeClient(answer);
  const commands = h.fakeCommands("ListObjectsV2Command", "GetObjectCommand", "PutObjectCommand", "DeleteObjectCommand");
  return { s3, handler: files.createHandler({ bucket: "test-bucket", verifier, s3, commands }) };
}

describe("user_files target", () => {
  it("every tool declared in tool-schemas.json is handled", async () => {
    for (const t of schemas["user-files"].tools) {
      const { handler } = filesHandler({ Contents: [] });
      const out = await handler({ filename: "a.txt", content: "x", [RESERVED_ARG]: TOKEN }, h.gatewayContext("user-files", t.name));
      assert.notEqual(out.error, "unknown_tool", `${t.name} not handled`);
    }
    for (const t of schemas["user-files"].tools) {
      assert.ok(t.inputSchema.properties[RESERVED_ARG], `${t.name} schema lacks ${RESERVED_ARG}`);
    }
  });

  it("list_files lists only the caller's prefix, derived from the token", async () => {
    const { s3, handler } = filesHandler({
      Contents: [
        { Key: "telegram_123456/", Size: 0, LastModified: new Date(0) },
        { Key: "telegram_123456/notes.md", Size: 12, LastModified: new Date("2026-01-02T00:00:00Z") },
      ],
    });
    const out = await handler({ [RESERVED_ARG]: TOKEN }, h.gatewayContext("user-files", "list_files"));
    assert.equal(s3.sent[0].input.Prefix, "telegram_123456/");
    assert.deepEqual(out, { files: [{ name: "notes.md", size: 12, modified: "2026-01-02T00:00:00.000Z" }], truncated: false });
  });

  it("a spoofed user_id/namespace argument does not change the prefix", async () => {
    const { s3, handler } = filesHandler({ Contents: [] });
    await handler(
      { user_id: "telegram_victim", namespace: "telegram_victim", [RESERVED_ARG]: TOKEN },
      h.gatewayContext("user-files", "list_files"),
    );
    assert.equal(s3.sent[0].input.Prefix, "telegram_123456/");
  });

  it("read/write/delete address <namespace>/<sanitized filename>", async () => {
    const { s3, handler } = filesHandler({ Body: { transformToString: async () => "hello" } });
    const ctx = (t) => h.gatewayContext("user-files", t);
    let out = await handler({ filename: "../../etc/passwd", [RESERVED_ARG]: TOKEN }, ctx("read_file"));
    assert.equal(s3.sent[0].input.Key, "telegram_123456/__etc_passwd");
    assert.equal(out.content, "hello");

    out = await handler({ filename: "a b.txt", content: "data", [RESERVED_ARG]: TOKEN }, ctx("write_file"));
    assert.equal(s3.sent[1].input.Key, "telegram_123456/a_b.txt");
    assert.equal(s3.sent[1].input.Body, "data");
    assert.deepEqual(out, { written: "a_b.txt", bytes: 4 });

    out = await handler({ filename: "a_b.txt", [RESERVED_ARG]: TOKEN }, ctx("delete_file"));
    assert.equal(s3.sent[2].kind, "DeleteObjectCommand");
    assert.deepEqual(out, { deleted: "a_b.txt" });
  });

  it("rejects hidden files, oversize content and missing token", async () => {
    const { s3, handler } = filesHandler({});
    let out = await handler({ filename: ".ssh", [RESERVED_ARG]: TOKEN }, h.gatewayContext("user-files", "read_file"));
    assert.equal(out.error, "error");
    assert.match(out.message, /leading\/trailing dots/);

    out = await handler(
      { filename: "big", content: "x".repeat(files.MAX_CONTENT_BYTES + 1), [RESERVED_ARG]: TOKEN },
      h.gatewayContext("user-files", "write_file"),
    );
    assert.match(out.message, /maximum allowed size/);

    out = await handler({ filename: "a" }, h.gatewayContext("user-files", "read_file"));
    assert.equal(out.error, "unauthorized");
    assert.equal(s3.sent.length, 0, "no S3 call without a verified caller");
  });

  it("read_file reports not_found for NoSuchKey", async () => {
    const { handler } = filesHandler(() => Promise.reject(Object.assign(new Error("nope"), { name: "NoSuchKey" })));
    const out = await handler({ filename: "missing", [RESERVED_ARG]: TOKEN }, h.gatewayContext("user-files", "read_file"));
    assert.equal(out.error, "not_found");
  });
});

// ----------------------------------------------------------------- schedules

const PROFILE = { Item: { PK: "CHANNEL#telegram:123456", SK: "PROFILE", userId: "user_abc" } };

function cronHandler({ ddbAnswer, schedAnswer, newId = () => "deadbeef" } = {}) {
  const scheduler = h.fakeClient(schedAnswer || {});
  const ddb = h.fakeClient(ddbAnswer || ((cmd) => (cmd.kind === "GetCommand" && cmd.input.Key.SK === "PROFILE" ? PROFILE : {})));
  const handler = cron.createHandler({
    verifier,
    scheduler,
    schedulerCommands: h.fakeCommands("CreateScheduleCommand", "GetScheduleCommand", "UpdateScheduleCommand", "DeleteScheduleCommand"),
    ddb,
    ddbCommands: h.fakeCommands("GetCommand", "PutCommand", "QueryCommand", "UpdateCommand", "DeleteCommand"),
    cronLambdaArn: "arn:aws:lambda:us-west-2:123456789012:function:openclaw-cron-executor",
    schedulerRoleArn: "arn:aws:iam::123456789012:role/openclaw-cron-scheduler-role-us-west-2",
    tableName: "openclaw-identity",
    newId,
  });
  return { scheduler, ddb, handler };
}

describe("schedules target", () => {
  it("every tool declared in tool-schemas.json is handled", async () => {
    for (const t of schemas.schedules.tools) {
      const { handler } = cronHandler();
      const out = await handler(
        { expression: "rate(1 hour)", timezone: "UTC", message: "m", schedule_id: "deadbeef", [RESERVED_ARG]: TOKEN },
        h.gatewayContext("schedules", t.name),
      );
      assert.notEqual(out.error, "unknown_tool", `${t.name} not handled`);
    }
  });

  it("create_schedule names the schedule after the token namespace and writes USER#<internalUserId>", async () => {
    const { scheduler, ddb, handler } = cronHandler();
    const out = await handler(
      { expression: "cron(0 9 * * ? *)", timezone: "Asia/Tokyo", message: "stand up", user_id: "telegram_victim", [RESERVED_ARG]: TOKEN },
      h.gatewayContext("schedules", "create_schedule"),
    );
    assert.equal(out.schedule_id, "deadbeef");
    const create = scheduler.sent.find((c) => c.kind === "CreateScheduleCommand").input;
    assert.equal(create.Name, "openclaw-telegram_123456-deadbeef");
    assert.equal(create.GroupName, "openclaw-cron");
    const input = JSON.parse(create.Target.Input);
    assert.equal(input.userId, "user_abc");
    assert.equal(input.actorId, "telegram:123456");
    assert.equal(input.channel, "telegram");
    assert.equal(input.channelTarget, "123456");
    const put = ddb.sent.find((c) => c.kind === "PutCommand").input;
    assert.equal(put.Item.PK, "USER#user_abc");
    assert.equal(put.Item.SK, "CRON#deadbeef");
    assert.equal(put.Item.actorId, "telegram:123456");
  });

  it("create_schedule rolls back the EventBridge schedule if the DynamoDB write fails", async () => {
    const { scheduler, handler } = cronHandler({
      ddbAnswer: (cmd) => {
        if (cmd.kind === "GetCommand") return PROFILE;
        if (cmd.kind === "PutCommand") return Promise.reject(new Error("ddb down"));
        return {};
      },
    });
    const out = await handler(
      { expression: "rate(1 hour)", timezone: "UTC", message: "m", [RESERVED_ARG]: TOKEN },
      h.gatewayContext("schedules", "create_schedule"),
    );
    assert.equal(out.error, "error");
    assert.ok(scheduler.sent.some((c) => c.kind === "DeleteScheduleCommand"), "rollback delete issued");
  });

  it("validates expressions and timezones like the exec skill", async () => {
    const { scheduler, handler } = cronHandler();
    const ctx = h.gatewayContext("schedules", "create_schedule");
    for (const [expression, re] of [
      ["rate(1 minute)", /Minimum rate interval/],
      ["cron(* * * * ? *)", /Every-minute/],
      ["cron(0 9 * * ?)", /exactly 6 fields/],
      ["every day", /Invalid expression/],
    ]) {
      const out = await handler({ expression, timezone: "UTC", message: "m", [RESERVED_ARG]: TOKEN }, ctx);
      assert.match(out.message, re, expression);
    }
    const out = await handler({ expression: "rate(1 hour)", timezone: "Mars/Olympus", message: "m", [RESERVED_ARG]: TOKEN }, ctx);
    assert.match(out.message, /Invalid timezone/);
    assert.equal(scheduler.sent.length, 0);
  });

  it("list_schedules queries only USER#<internalUserId> CRON# records", async () => {
    const { ddb, handler } = cronHandler({
      ddbAnswer: (cmd) => {
        if (cmd.kind === "GetCommand") return PROFILE;
        if (cmd.kind === "QueryCommand") {
          return { Items: [{ scheduleId: "deadbeef", scheduleName: "n", expression: "rate(1 hour)", timezone: "UTC", message: "m", enabled: true, createdAt: "t" }] };
        }
        return {};
      },
    });
    const out = await handler({ [RESERVED_ARG]: TOKEN }, h.gatewayContext("schedules", "list_schedules"));
    const q = ddb.sent.find((c) => c.kind === "QueryCommand").input;
    assert.equal(q.ExpressionAttributeValues[":pk"], "USER#user_abc");
    assert.equal(out.schedules.length, 1);
    assert.equal(out.schedules[0].schedule_id, "deadbeef");
  });

  it("update/delete refuse a schedule the caller does not own (no CRON# record)", async () => {
    const { scheduler, handler } = cronHandler();
    let out = await handler({ schedule_id: "cafebabe", message: "x", [RESERVED_ARG]: TOKEN }, h.gatewayContext("schedules", "update_schedule"));
    assert.equal(out.error, "not_found");
    out = await handler({ schedule_id: "cafebabe", [RESERVED_ARG]: TOKEN }, h.gatewayContext("schedules", "delete_schedule"));
    assert.equal(out.error, "not_found");
    assert.equal(scheduler.sent.length, 0, "no Scheduler call for a foreign schedule");
  });

  it("update_schedule merges fields and delete_schedule removes both halves", async () => {
    const owned = { Item: { PK: "USER#user_abc", SK: "CRON#deadbeef", scheduleId: "deadbeef", enabled: true } };
    const { scheduler, ddb, handler } = cronHandler({
      ddbAnswer: (cmd) => (cmd.kind === "GetCommand" ? (cmd.input.Key.SK === "PROFILE" ? PROFILE : owned) : {}),
      schedAnswer: (cmd) =>
        cmd.kind === "GetScheduleCommand"
          ? { ScheduleExpression: "rate(1 hour)", ScheduleExpressionTimezone: "UTC", Description: "d", Target: { Input: JSON.stringify({ message: "old" }) } }
          : {},
    });
    let out = await handler(
      { schedule_id: "deadbeef", message: "new", enabled: false, [RESERVED_ARG]: TOKEN },
      h.gatewayContext("schedules", "update_schedule"),
    );
    const upd = scheduler.sent.find((c) => c.kind === "UpdateScheduleCommand").input;
    assert.equal(upd.Name, "openclaw-telegram_123456-deadbeef");
    assert.equal(upd.State, "DISABLED");
    assert.equal(JSON.parse(upd.Target.Input).message, "new");
    assert.deepEqual(out.updated.sort(), ["enabled", "message"]);

    out = await handler({ schedule_id: "deadbeef", [RESERVED_ARG]: TOKEN }, h.gatewayContext("schedules", "delete_schedule"));
    assert.deepEqual(out, { deleted: "deadbeef" });
    assert.ok(scheduler.sent.some((c) => c.kind === "DeleteScheduleCommand"));
    assert.ok(ddb.sent.some((c) => c.kind === "DeleteCommand" && c.input.Key.SK === "CRON#deadbeef"));
  });

  it("fails closed without a verified caller", async () => {
    const { scheduler, ddb, handler } = cronHandler();
    const out = await handler({ expression: "rate(1 hour)", timezone: "UTC", message: "m" }, h.gatewayContext("schedules", "create_schedule"));
    assert.equal(out.error, "unauthorized");
    assert.equal(scheduler.sent.length + ddb.sent.length, 0);
  });
});
