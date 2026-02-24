#!/usr/bin/env node
/**
 * update_schedule — Update an existing EventBridge cron schedule.
 * Usage: node update.js <user_id> <schedule_id> [--expression "cron(...)"] [--timezone "Asia/Tokyo"]
 *        [--message "new message"] [--enable] [--disable] [--name "new name"]
 */
const { SchedulerClient, GetScheduleCommand, UpdateScheduleCommand } = require("@aws-sdk/client-scheduler");
const {
  REGION,
  SCHEDULE_GROUP,
  CRON_LAMBDA_ARN,
  EVENTBRIDGE_ROLE_ARN,
  validateUserId,
  validateEnv,
  validateExpression,
  validateTimezone,
  buildScheduleName,
  getCronRecord,
  updateCronRecord,
  extractChannelInfo,
} = require("./common");

function parseArgs(args) {
  const result = {};
  let i = 0;
  while (i < args.length) {
    const arg = args[i];
    if (arg === "--expression" && i + 1 < args.length) {
      result.expression = args[++i];
    } else if (arg === "--timezone" && i + 1 < args.length) {
      result.timezone = args[++i];
    } else if (arg === "--message" && i + 1 < args.length) {
      result.message = args[++i];
    } else if (arg === "--name" && i + 1 < args.length) {
      result.scheduleName = args[++i];
    } else if (arg === "--enable") {
      result.enabled = true;
    } else if (arg === "--disable") {
      result.enabled = false;
    }
    i++;
  }
  return result;
}

async function main() {
  const userId = process.argv[2];
  const scheduleId = process.argv[3];
  const updates = parseArgs(process.argv.slice(4));

  validateUserId(userId);
  validateEnv();

  if (!scheduleId) {
    console.error("Error: schedule_id argument is required.");
    process.exit(1);
  }

  if (Object.keys(updates).length === 0) {
    console.error("Error: No updates specified. Use --expression, --timezone, --message, --enable, --disable, or --name.");
    process.exit(1);
  }

  // Validate new values if provided
  if (updates.expression) validateExpression(updates.expression);
  if (updates.timezone) validateTimezone(updates.timezone);

  // Look up real userId from DynamoDB
  const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
  const { DynamoDBDocumentClient, GetCommand } = require("@aws-sdk/lib-dynamodb");
  const IDENTITY_TABLE_NAME = process.env.IDENTITY_TABLE_NAME;

  let realUserId = null;
  try {
    const { channel, channelTarget } = extractChannelInfo(userId);
    const docClient = DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }));
    const channelKey = `${channel}:${channelTarget}`;
    const resp = await docClient.send(new GetCommand({
      TableName: IDENTITY_TABLE_NAME,
      Key: { PK: `CHANNEL#${channelKey}`, SK: "PROFILE" },
    }));
    if (resp.Item) {
      realUserId = resp.Item.userId;
    }
  } catch (err) {
    // Fall through
  }

  const lookupUserId = realUserId || userId;

  // Get existing record
  const record = await getCronRecord(lookupUserId, scheduleId);
  if (!record) {
    console.error(`Error: Schedule ${scheduleId} not found for this user.`);
    process.exit(1);
  }

  const ebScheduleName = buildScheduleName(userId, scheduleId);
  const schedulerClient = new SchedulerClient({ region: REGION });

  // Get current schedule from EventBridge
  let currentSchedule;
  try {
    currentSchedule = await schedulerClient.send(new GetScheduleCommand({
      Name: ebScheduleName,
      GroupName: SCHEDULE_GROUP,
    }));
  } catch (err) {
    console.error(`Error: Schedule not found in EventBridge: ${err.message}`);
    process.exit(1);
  }

  // Build updated target input
  const currentInput = JSON.parse(currentSchedule.Target.Input || "{}");
  if (updates.message) currentInput.message = updates.message;
  if (updates.scheduleName) currentInput.scheduleName = updates.scheduleName;

  // Update EventBridge schedule
  try {
    await schedulerClient.send(new UpdateScheduleCommand({
      Name: ebScheduleName,
      GroupName: SCHEDULE_GROUP,
      ScheduleExpression: updates.expression || currentSchedule.ScheduleExpression,
      ScheduleExpressionTimezone: updates.timezone || currentSchedule.ScheduleExpressionTimezone,
      FlexibleTimeWindow: { Mode: "OFF" },
      State: updates.enabled === false ? "DISABLED" : "ENABLED",
      Target: {
        Arn: CRON_LAMBDA_ARN,
        RoleArn: EVENTBRIDGE_ROLE_ARN,
        Input: JSON.stringify(currentInput),
      },
      Description: currentSchedule.Description,
    }));
  } catch (err) {
    console.error(`Error updating schedule: ${err.message}`);
    process.exit(1);
  }

  // Update DynamoDB record
  const dbUpdates = { updatedAt: new Date().toISOString() };
  if (updates.expression) dbUpdates.expression = updates.expression;
  if (updates.timezone) dbUpdates.timezone = updates.timezone;
  if (updates.message) dbUpdates.message = updates.message;
  if (updates.scheduleName) dbUpdates.scheduleName = updates.scheduleName;
  if (updates.enabled !== undefined) dbUpdates.enabled = updates.enabled;

  try {
    await updateCronRecord(lookupUserId, scheduleId, dbUpdates);
  } catch (err) {
    console.error(`Warning: EventBridge updated but DynamoDB update failed: ${err.message}`);
  }

  console.log(`Schedule ${scheduleId} updated successfully!`);
  if (updates.expression) console.log(`  Expression: ${updates.expression}`);
  if (updates.timezone) console.log(`  Timezone: ${updates.timezone}`);
  if (updates.message) console.log(`  Message: ${updates.message}`);
  if (updates.scheduleName) console.log(`  Name: ${updates.scheduleName}`);
  if (updates.enabled === true) console.log(`  Status: ENABLED`);
  if (updates.enabled === false) console.log(`  Status: DISABLED`);
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
