/**
 * eventbridge-cron skill: minimum interval, schedule names for long userIds,
 * and the owner checks in common.js, list.js, update.js and delete.js.
 * Run: cd bridge/skills/eventbridge-cron && node --test common.test.js
 *
 * No real AWS call is made. The interval and name tests use a fake scheduler
 * client. The owner-check tests run each script in a child process with a `-r`
 * preload that replaces the AWS SDK clients with a recorder and, when
 * IDENTITY_STUB is set, serves that JSON as /tmp/current-identity.json.
 */
"use strict";

const { describe, it, beforeEach, afterEach } = require("node:test");
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
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const LIST = path.join(__dirname, "list.js");
const UPDATE = path.join(__dirname, "update.js");
const DELETE = path.join(__dirname, "delete.js");
const NAMESPACE = "telegram_123456";
const ACTOR_ID = "telegram:123456";
const INTERNAL_USER_ID = "user_0123456789abcdef";

const STUB_SOURCE = `
const Module = require("module");
const fs = require("fs");
const log = (o) => fs.appendFileSync(process.env.AWS_STUB_LOG, JSON.stringify(o) + "\\n");
const readFileSync = fs.readFileSync;
fs.readFileSync = function (file, ...rest) {
  if (file === "/tmp/current-identity.json") {
    if (!process.env.IDENTITY_STUB) {
      throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
    }
    return process.env.IDENTITY_STUB;
  }
  return readFileSync.call(this, file, ...rest);
};
const mk = (name) => class { constructor(input) { this.name = name; this.input = input; } };
const orig = Module._load;
Module._load = function (req, ...rest) {
  if (req === "@aws-sdk/client-dynamodb") return { DynamoDBClient: class {} };
  if (req === "@aws-sdk/lib-dynamodb") {
    return {
      DynamoDBDocumentClient: { from: () => ({ async send(cmd) {
        log({ cmd: cmd.name, input: cmd.input });
        if (cmd.name === "GetCommand") {
          return { Item: { scheduleId: "0a1b2c3d", scheduleName: "Brief", expression: "rate(1 day)" } };
        }
        if (cmd.name === "QueryCommand") return { Items: [] };
        return {};
      } }) },
      PutCommand: mk("PutCommand"), GetCommand: mk("GetCommand"), DeleteCommand: mk("DeleteCommand"),
      QueryCommand: mk("QueryCommand"), UpdateCommand: mk("UpdateCommand"),
    };
  }
  if (req === "@aws-sdk/client-scheduler") {
    return {
      SchedulerClient: class { async send(cmd) {
        log({ cmd: cmd.name, input: cmd.input });
        if (cmd.name === "GetScheduleCommand") {
          return { Target: { Input: "{}" }, ScheduleExpression: "rate(1 day)", ScheduleExpressionTimezone: "UTC" };
        }
        return {};
      } },
      GetScheduleCommand: mk("GetScheduleCommand"), UpdateScheduleCommand: mk("UpdateScheduleCommand"),
      DeleteScheduleCommand: mk("DeleteScheduleCommand"), CreateScheduleCommand: mk("CreateScheduleCommand"),
    };
  }
  return orig.call(this, req, ...rest);
};
`;

let tmp;
let stubPath;
let logPath;

function run(script, args, extraEnv = {}) {
  const env = {
    PATH: process.env.PATH,
    HOME: tmp,
    AWS_REGION: "us-west-2",
    AWS_STUB_LOG: logPath,
    CRON_LAMBDA_ARN: "arn:aws:lambda:us-west-2:123456789012:function:openclaw-cron",
    EVENTBRIDGE_ROLE_ARN: "arn:aws:iam::123456789012:role/openclaw-cron-scheduler-role-us-west-2",
    IDENTITY_TABLE_NAME: "openclaw-identity",
    USER_ID: ACTOR_ID,
    INTERNAL_USER_ID,
    ...extraEnv,
  };
  for (const [key, value] of Object.entries(env)) if (value === undefined) delete env[key];
  return spawnSync(process.execPath, ["-r", stubPath, script, ...args], {
    env, encoding: "utf-8", timeout: 20_000,
  });
}

function awsCalls() {
  if (!fs.existsSync(logPath)) return [];
  return fs.readFileSync(logPath, "utf-8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l));
}

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "eventbridge-cron-test-"));
  stubPath = path.join(tmp, "aws-stub.js");
  logPath = path.join(tmp, "aws-calls.log");
  fs.writeFileSync(stubPath, STUB_SOURCE);
});

afterEach(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
});

describe("eventbridge-cron owner checks", () => {
  for (const [name, script, args] of [
    ["list.js", LIST, ["telegram_654321"]],
    ["update.js", UPDATE, ["telegram_654321", "0a1b2c3d", "--message", "hi"]],
    ["delete.js", DELETE, ["telegram_654321", "0a1b2c3d"]],
  ]) {
    it(`${name} refuses another user's namespace before any AWS call`, () => {
      const r = run(script, args);
      assert.equal(r.status, 1);
      assert.match(r.stderr, /does not match the user this session belongs to/);
      assert.deepEqual(awsCalls(), []);
    });
  }

  it("refuses a namespace the identity file does not name, even if USER_ID agrees", () => {
    const r = run(LIST, [NAMESPACE], { IDENTITY_STUB: JSON.stringify({ actorId: "telegram:654321" }) });
    assert.equal(r.status, 1);
    assert.deepEqual(awsCalls(), []);
  });

  it("accepts the namespace from the identity file alone", () => {
    const r = run(LIST, [NAMESPACE], { USER_ID: undefined, IDENTITY_STUB: JSON.stringify({ actorId: ACTOR_ID }) });
    assert.equal(r.status, 0, r.stderr);
  });

  it("refuses every user_id when the session's user is unknown", () => {
    const r = run(LIST, [NAMESPACE], { USER_ID: undefined });
    assert.equal(r.status, 1);
    assert.match(r.stderr, /user identity is not available/);
    assert.deepEqual(awsCalls(), []);
  });

  it("list.js reads CRON# records from the session's own USER# partition only", () => {
    const r = run(LIST, [NAMESPACE]);
    assert.equal(r.status, 0, r.stderr);
    const calls = awsCalls();
    assert.deepEqual(calls.map((c) => c.cmd), ["QueryCommand"]);
    assert.equal(calls[0].input.ExpressionAttributeValues[":pk"], `USER#${INTERNAL_USER_ID}`);
  });

  it("update.js and delete.js use the session's internal user ID, not a CHANNEL# lookup", () => {
    assert.equal(run(UPDATE, [NAMESPACE, "0a1b2c3d", "--message", "hi"]).status, 0);
    assert.equal(run(DELETE, [NAMESPACE, "0a1b2c3d"]).status, 0);
    const dynamo = awsCalls().filter((c) => c.input.TableName);
    assert.ok(dynamo.length >= 4);
    for (const call of dynamo) assert.equal(call.input.Key.PK, `USER#${INTERNAL_USER_ID}`);
    const scheduler = awsCalls().filter((c) => c.input.GroupName);
    for (const call of scheduler) assert.equal(call.input.Name, `openclaw-${NAMESPACE}-0a1b2c3d`);
  });

  it("falls back to the namespace partition when INTERNAL_USER_ID is not set", () => {
    const r = run(LIST, [NAMESPACE], { INTERNAL_USER_ID: undefined });
    assert.equal(r.status, 0, r.stderr);
    assert.equal(awsCalls()[0].input.ExpressionAttributeValues[":pk"], `USER#${NAMESPACE}`);
  });
});
