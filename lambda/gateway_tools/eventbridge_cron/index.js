/**
 * Gateway Lambda MCP target: schedules
 * (create_schedule / list_schedules / update_schedule / delete_schedule).
 *
 * Mirrors bridge/skills/eventbridge-cron: schedules live in the
 * `openclaw-cron` EventBridge Scheduler group, named
 * `openclaw-<namespace>-<id>`, target the openclaw-cron-executor Lambda, and
 * are mirrored as `USER#<internalUserId>` / `CRON#<id>` records in the
 * openclaw-identity table. The cron executor verifies ownership against that
 * record, so the same internalUserId resolution the skill uses
 * (`CHANNEL#<actorId>` PROFILE -> userId) is applied here.
 *
 * Identity (actorId, namespace) comes ONLY from the verified caller token.
 *
 * Runtime: nodejs22.x (bundled AWS SDK v3).
 */
"use strict";

const crypto = require("node:crypto");
const { createVerifier, resolveCaller } = require("../lib/identity");
const { toolNameFromContext, withErrorEnvelope } = require("../lib/mcp");

// --- Validation (same rules as bridge/skills/eventbridge-cron/common.js) ---

function validateExpression(expression) {
  if (typeof expression !== "string") throw new Error("expression is required");
  const atMatch = expression.match(/^at\((\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\)$/);
  if (atMatch) {
    if (Number.isNaN(new Date(atMatch[1]).getTime())) {
      throw new Error(`Invalid at() datetime "${atMatch[1]}"`);
    }
    return;
  }
  if (/^rate\(\d+\s+(minute|minutes|hour|hours|day|days)\)$/.test(expression)) {
    const m = expression.match(/^rate\((\d+)\s+(minute|minutes)\)$/);
    if (m && parseInt(m[1], 10) < 5) throw new Error("Minimum rate interval is 5 minutes");
    return;
  }
  const cronMatch = expression.match(/^cron\((.+)\)$/);
  if (cronMatch) {
    const fields = cronMatch[1].trim().split(/\s+/);
    if (fields.length !== 6) {
      throw new Error(`cron() expression must have exactly 6 fields, got ${fields.length}`);
    }
    if (fields[0] === "*" || fields[0] === "*/1") {
      throw new Error("Every-minute cron expressions are not allowed (minimum 5 minutes)");
    }
    return;
  }
  throw new Error(`Invalid expression "${expression}". Must be cron(...), rate(...), or at(...)`);
}

function validateTimezone(timezone) {
  try {
    Intl.DateTimeFormat(undefined, { timeZone: timezone });
  } catch {
    throw new Error(`Invalid timezone "${timezone}". Must be a valid IANA timezone`);
  }
}

function validateScheduleId(id) {
  if (typeof id !== "string" || !/^[a-f0-9]{8}$/.test(id)) {
    throw new Error("schedule_id must be the 8-character id from list_schedules");
  }
  return id;
}

function buildScheduleName(namespace, scheduleId) {
  const prefix = "openclaw-";
  const suffix = `-${scheduleId}`;
  const max = 64 - prefix.length - suffix.length;
  return `${prefix}${namespace.length > max ? namespace.slice(0, max) : namespace}${suffix}`;
}

function createHandler(deps = {}) {
  const env = {
    scheduleGroup: deps.scheduleGroup || process.env.EVENTBRIDGE_SCHEDULE_GROUP || "openclaw-cron",
    cronLambdaArn: deps.cronLambdaArn || process.env.CRON_LAMBDA_ARN,
    schedulerRoleArn: deps.schedulerRoleArn || process.env.EVENTBRIDGE_ROLE_ARN,
    tableName: deps.tableName || process.env.IDENTITY_TABLE_NAME,
  };
  for (const [k, v] of Object.entries(env)) {
    if (!v) throw new Error(`missing configuration: ${k}`);
  }
  const verifier = deps.verifier || createVerifier();
  const newId = deps.newId || (() => crypto.randomBytes(4).toString("hex"));

  let scheduler = deps.scheduler;
  let schedulerCmds = deps.schedulerCommands;
  let ddb = deps.ddb;
  let ddbCmds = deps.ddbCommands;
  function clients() {
    if (!scheduler) {
      const sdk = require("@aws-sdk/client-scheduler");
      scheduler = new sdk.SchedulerClient({});
      schedulerCmds = sdk;
    }
    if (!ddb) {
      const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
      const lib = require("@aws-sdk/lib-dynamodb");
      ddb = lib.DynamoDBDocumentClient.from(new DynamoDBClient({}));
      ddbCmds = lib;
    }
    return { scheduler, schedulerCmds, ddb, ddbCmds };
  }

  /** CHANNEL#<actorId> PROFILE -> internal userId (same lookup as the skill and cron executor). */
  async function resolveInternalUserId(actorId) {
    const { ddb, ddbCmds } = clients();
    const resp = await ddb.send(
      new ddbCmds.GetCommand({
        TableName: env.tableName,
        Key: { PK: `CHANNEL#${actorId}`, SK: "PROFILE" },
      }),
    );
    if (!resp.Item || !resp.Item.userId) {
      throw new Error("no OpenClaw user profile for this identity");
    }
    return resp.Item.userId;
  }

  function channelInfo(actorId) {
    const idx = actorId.indexOf(":");
    return idx === -1
      ? { channel: "unknown", channelTarget: actorId }
      : { channel: actorId.slice(0, idx), channelTarget: actorId.slice(idx + 1) };
  }

  const tools = {
    async create_schedule(identity, args) {
      validateExpression(args.expression);
      validateTimezone(args.timezone);
      if (typeof args.message !== "string" || !args.message) throw new Error("message is required");
      const scheduleName =
        typeof args.schedule_name === "string" && args.schedule_name ? args.schedule_name : "";
      const { scheduler, schedulerCmds, ddb, ddbCmds } = clients();
      const userId = await resolveInternalUserId(identity.actorId);
      const { channel, channelTarget } = channelInfo(identity.actorId);
      const scheduleId = newId();
      const ebName = buildScheduleName(identity.namespace, scheduleId);
      const displayName = scheduleName || `Schedule ${scheduleId}`;

      await scheduler.send(
        new schedulerCmds.CreateScheduleCommand({
          Name: ebName,
          GroupName: env.scheduleGroup,
          ScheduleExpression: args.expression,
          ScheduleExpressionTimezone: args.timezone,
          FlexibleTimeWindow: { Mode: "OFF" },
          State: "ENABLED",
          Target: {
            Arn: env.cronLambdaArn,
            RoleArn: env.schedulerRoleArn,
            Input: JSON.stringify({
              userId,
              actorId: identity.actorId,
              channel,
              channelTarget,
              message: args.message,
              scheduleId,
              scheduleName: displayName,
            }),
          },
          Description: `OpenClaw cron: ${scheduleName || args.message.slice(0, 100)}`,
        }),
      );

      const now = new Date().toISOString();
      try {
        await ddb.send(
          new ddbCmds.PutCommand({
            TableName: env.tableName,
            Item: {
              PK: `USER#${userId}`,
              SK: `CRON#${scheduleId}`,
              scheduleId,
              scheduleName: displayName,
              expression: args.expression,
              timezone: args.timezone,
              message: args.message,
              channel,
              channelTarget,
              actorId: identity.actorId,
              enabled: true,
              createdAt: now,
              updatedAt: now,
              source: "gateway-mcp",
            },
          }),
        );
      } catch (err) {
        // Roll back so no orphaned schedule fires and fails ownership forever.
        await scheduler
          .send(new schedulerCmds.DeleteScheduleCommand({ Name: ebName, GroupName: env.scheduleGroup }))
          .catch(() => {});
        throw err;
      }
      return {
        schedule_id: scheduleId,
        name: displayName,
        expression: args.expression,
        timezone: args.timezone,
        message: args.message,
      };
    },

    async list_schedules(identity) {
      const { ddb, ddbCmds } = clients();
      const userId = await resolveInternalUserId(identity.actorId);
      const resp = await ddb.send(
        new ddbCmds.QueryCommand({
          TableName: env.tableName,
          KeyConditionExpression: "PK = :pk AND begins_with(SK, :sk)",
          ExpressionAttributeValues: { ":pk": `USER#${userId}`, ":sk": "CRON#" },
        }),
      );
      return {
        schedules: (resp.Items || []).map((r) => ({
          schedule_id: r.scheduleId,
          name: r.scheduleName,
          expression: r.expression,
          timezone: r.timezone,
          message: r.message,
          enabled: r.enabled !== false,
          created_at: r.createdAt,
        })),
      };
    },

    async update_schedule(identity, args) {
      const scheduleId = validateScheduleId(args.schedule_id);
      const updates = {};
      if (args.expression !== undefined) {
        validateExpression(args.expression);
        updates.expression = args.expression;
      }
      if (args.timezone !== undefined) {
        validateTimezone(args.timezone);
        updates.timezone = args.timezone;
      }
      if (args.message !== undefined) {
        if (typeof args.message !== "string" || !args.message) throw new Error("message must be non-empty");
        updates.message = args.message;
      }
      if (args.schedule_name !== undefined) updates.scheduleName = String(args.schedule_name);
      if (args.enabled !== undefined) updates.enabled = Boolean(args.enabled);
      if (Object.keys(updates).length === 0) throw new Error("no updates specified");

      const { scheduler, schedulerCmds, ddb, ddbCmds } = clients();
      const userId = await resolveInternalUserId(identity.actorId);
      const rec = await ddb.send(
        new ddbCmds.GetCommand({
          TableName: env.tableName,
          Key: { PK: `USER#${userId}`, SK: `CRON#${scheduleId}` },
        }),
      );
      if (!rec.Item) return { error: "not_found", message: `Schedule ${scheduleId} not found for this user` };

      const ebName = buildScheduleName(identity.namespace, scheduleId);
      const current = await scheduler.send(
        new schedulerCmds.GetScheduleCommand({ Name: ebName, GroupName: env.scheduleGroup }),
      );
      const input = JSON.parse((current.Target && current.Target.Input) || "{}");
      if (updates.message) input.message = updates.message;
      if (updates.scheduleName) input.scheduleName = updates.scheduleName;
      const enabled = updates.enabled !== undefined ? updates.enabled : rec.Item.enabled !== false;

      await scheduler.send(
        new schedulerCmds.UpdateScheduleCommand({
          Name: ebName,
          GroupName: env.scheduleGroup,
          ScheduleExpression: updates.expression || current.ScheduleExpression,
          ScheduleExpressionTimezone: updates.timezone || current.ScheduleExpressionTimezone,
          FlexibleTimeWindow: { Mode: "OFF" },
          State: enabled ? "ENABLED" : "DISABLED",
          Target: {
            Arn: env.cronLambdaArn,
            RoleArn: env.schedulerRoleArn,
            Input: JSON.stringify(input),
          },
          Description: current.Description,
        }),
      );

      const names = {};
      const values = {};
      const sets = [];
      for (const [k, v] of Object.entries({ ...updates, updatedAt: new Date().toISOString() })) {
        names[`#${k}`] = k;
        values[`:${k}`] = v;
        sets.push(`#${k} = :${k}`);
      }
      await ddb.send(
        new ddbCmds.UpdateCommand({
          TableName: env.tableName,
          Key: { PK: `USER#${userId}`, SK: `CRON#${scheduleId}` },
          UpdateExpression: `SET ${sets.join(", ")}`,
          ExpressionAttributeNames: names,
          ExpressionAttributeValues: values,
        }),
      );
      return { schedule_id: scheduleId, updated: Object.keys(updates) };
    },

    async delete_schedule(identity, args) {
      const scheduleId = validateScheduleId(args.schedule_id);
      const { scheduler, schedulerCmds, ddb, ddbCmds } = clients();
      const userId = await resolveInternalUserId(identity.actorId);
      const rec = await ddb.send(
        new ddbCmds.GetCommand({
          TableName: env.tableName,
          Key: { PK: `USER#${userId}`, SK: `CRON#${scheduleId}` },
        }),
      );
      if (!rec.Item) return { error: "not_found", message: `Schedule ${scheduleId} not found for this user` };
      const ebName = buildScheduleName(identity.namespace, scheduleId);
      try {
        await scheduler.send(
          new schedulerCmds.DeleteScheduleCommand({ Name: ebName, GroupName: env.scheduleGroup }),
        );
      } catch (err) {
        if (!err || err.name !== "ResourceNotFoundException") throw err;
      }
      await ddb.send(
        new ddbCmds.DeleteCommand({
          TableName: env.tableName,
          Key: { PK: `USER#${userId}`, SK: `CRON#${scheduleId}` },
        }),
      );
      return { deleted: scheduleId };
    },
  };

  return withErrorEnvelope(async (event, context) => {
    const { identity, args } = await resolveCaller(event, verifier);
    const tool = toolNameFromContext(context);
    const fn = tools[tool];
    if (!fn) return { error: "unknown_tool", message: `Unknown tool "${tool}"` };
    return fn(identity, args);
  });
}

let _handler = null;
async function handler(event, context) {
  if (!_handler) _handler = createHandler();
  return _handler(event, context);
}

module.exports = { handler, createHandler, validateExpression, validateTimezone, buildScheduleName };
