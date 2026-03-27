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

  // Use INTERNAL_USER_ID env var (set by agentcore-contract.js at container init).
  // This is the container's own authorized userId — scoped credentials allow DynamoDB
  // writes to USER#{INTERNAL_USER_ID}. Avoids cross-user contamination when the same
  // Telegram/Slack account has multiple internal userIds (session rotation history).
  const realUserId = process.env.INTERNAL_USER_ID || null;

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
    // Rollback: delete the EventBridge schedule to avoid orphaned schedules that
    // will fire forever but always fail ownership check (403).
    console.error(`CRON# record save failed — rolling back EventBridge schedule: ${err.message}`);
    try {
      const { DeleteScheduleCommand } = require("@aws-sdk/client-scheduler");
      await schedulerClient.send(new DeleteScheduleCommand({
        Name: ebScheduleName,
        GroupName: SCHEDULE_GROUP,
      }));
      console.error(`Rollback: deleted EventBridge schedule ${ebScheduleName}`);
    } catch (rollbackErr) {
      console.error(`Rollback failed (orphaned schedule): ${rollbackErr.message}`);
    }
    console.error(`Error creating schedule: ${err.message}`);
    process.exit(1);
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
