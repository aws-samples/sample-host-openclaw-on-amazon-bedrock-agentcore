#!/usr/bin/env node
/**
 * create_schedule — Create a new EventBridge cron schedule.
 * Usage: node create.js <user_id> <cron_expression> <timezone> <message> [channel] [channel_target] [schedule_name]
 */
const { SchedulerClient, CreateScheduleCommand } = require("@aws-sdk/client-scheduler");
const {
  REGION,
  SCHEDULE_GROUP,
  CRON_LAMBDA_ARN,
  EVENTBRIDGE_ROLE_ARN,
  validateUserId,
  validateEnv,
  validateExpression,
  validateTimezone,
  generateScheduleId,
  buildScheduleName,
  extractChannelInfo,
  buildActorId,
  saveCronRecord,
} = require("./common");

async function main() {
  const userId = process.argv[2];
  const expression = process.argv[3];
  const timezone = process.argv[4];
  const message = process.argv[5];
  const channelArg = process.argv[6];
  const channelTargetArg = process.argv[7];
  const scheduleName = process.argv.slice(8).join(" ") || "";

  validateUserId(userId);
  validateEnv();

  if (!expression) {
    console.error("Error: cron_expression argument is required.");
    process.exit(1);
  }
  if (!timezone) {
    console.error("Error: timezone argument is required.");
    process.exit(1);
  }
  if (!message) {
    console.error("Error: message argument is required.");
    process.exit(1);
  }

  validateExpression(expression);
  validateTimezone(timezone);

  // Determine channel info
  const extracted = extractChannelInfo(userId);
  const channel = channelArg || extracted.channel;
  const channelTarget = channelTargetArg || extracted.channelTarget;
  const actorId = buildActorId(channel, channelTarget);

  // Generate schedule ID and name
  const scheduleId = generateScheduleId();
  const ebScheduleName = buildScheduleName(userId, scheduleId);

  // Build the target input payload for the cron Lambda
  const targetInput = JSON.stringify({
    userId: userId.replace(/_/g, ":").replace(/^(telegram|slack|discord|whatsapp):/, (match) => {
      // Reconstruct the proper userId format (user_xxx from DynamoDB)
      // The Lambda will look up the real userId from the actorId
      return match;
    }),
    actorId,
    channel,
    channelTarget,
    message,
    scheduleId,
    scheduleName: scheduleName || `Schedule ${scheduleId}`,
  });

  // We need the real userId from DynamoDB. The cron Lambda uses actorId to
  // resolve the user. But we also need the DynamoDB userId for the session
  // lookup. We pass the actorId and let the Lambda resolve it, but we also
  // need to store a reference. Let's look up the userId from the USER_ID env.
  // Actually, the skill runs inside the container where USER_ID is set as the
  // actorId (e.g. telegram:12345). The userId in DynamoDB is different
  // (user_xxx). We need to query DynamoDB for the real userId.
  const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
  const { DynamoDBDocumentClient, GetCommand } = require("@aws-sdk/lib-dynamodb");
  const IDENTITY_TABLE_NAME = process.env.IDENTITY_TABLE_NAME;

  let realUserId = null;
  try {
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
    console.error(`Warning: Could not look up DynamoDB userId: ${err.message}`);
  }

  // Build target input with real userId if found
  const lambdaInput = JSON.stringify({
    userId: realUserId || userId,
    actorId,
    channel,
    channelTarget,
    message,
    scheduleId,
    scheduleName: scheduleName || `Schedule ${scheduleId}`,
  });

  // Create the EventBridge schedule
  const schedulerClient = new SchedulerClient({ region: REGION });
  try {
    await schedulerClient.send(new CreateScheduleCommand({
      Name: ebScheduleName,
      GroupName: SCHEDULE_GROUP,
      ScheduleExpression: expression,
      ScheduleExpressionTimezone: timezone,
      FlexibleTimeWindow: { Mode: "OFF" },
      State: "ENABLED",
      Target: {
        Arn: CRON_LAMBDA_ARN,
        RoleArn: EVENTBRIDGE_ROLE_ARN,
        Input: lambdaInput,
      },
      Description: `OpenClaw cron: ${scheduleName || message.slice(0, 100)}`,
    }));
  } catch (err) {
    console.error(`Error creating schedule: ${err.message}`);
    process.exit(1);
  }

  // Save metadata to DynamoDB
  const now = new Date().toISOString();
  try {
    await saveCronRecord(realUserId || userId, {
      scheduleId,
      scheduleName: scheduleName || `Schedule ${scheduleId}`,
      expression,
      timezone,
      message,
      channel,
      channelTarget,
      actorId,
      enabled: true,
      createdAt: now,
      updatedAt: now,
    });
  } catch (err) {
    console.error(`Warning: Schedule created in EventBridge but DynamoDB save failed: ${err.message}`);
  }

  console.log(`Schedule created successfully!`);
  console.log(`  ID: ${scheduleId}`);
  console.log(`  Name: ${scheduleName || `Schedule ${scheduleId}`}`);
  console.log(`  Expression: ${expression}`);
  console.log(`  Timezone: ${timezone}`);
  console.log(`  Message: ${message}`);
  console.log(`  Channel: ${channel}:${channelTarget}`);
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
