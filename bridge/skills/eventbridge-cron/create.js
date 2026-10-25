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
  MAX_SCHEDULES_PER_USER,
  validateUserId,
  validateEnv,
  validateExpression,
  validateTimezone,
  generateScheduleId,
  buildScheduleName,
  resolveScheduleDelivery,
  saveCronRecord,
  listCronRecords,
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

  // Keep the AgentCore Runtime owner separate from the current conversation's destination.
  const { actorId, channelActorId, channel, channelTarget } = resolveScheduleDelivery(userId, {
    channel: channelArg, channelTarget: channelTargetArg,
  });

  // Generate schedule ID and name
  const scheduleId = generateScheduleId();
  const ebScheduleName = buildScheduleName(userId, scheduleId);

  // Use INTERNAL_USER_ID env var (set by agentcore-contract.js at container init).
  // This is the container's own authorized userId — scoped credentials allow DynamoDB
  // writes to USER#{INTERNAL_USER_ID}. Avoids cross-user contamination when the same
  // Telegram/Slack account has multiple internal userIds (session rotation history).
  const realUserId = process.env.INTERNAL_USER_ID || null;

  // Per-caller schedule quota. CWE-770: cap how many CRON# records this user
  // may own at once so a single actor cannot allocate an unbounded number of
  // every-minute agent turns. Mirrors the gateway MCP target.
  try {
    const existing = await listCronRecords(realUserId || userId);
    if (Array.isArray(existing) && existing.length >= MAX_SCHEDULES_PER_USER) {
      console.error(
        `Error: Schedule quota exceeded: this user already owns ${existing.length} schedules ` +
        `(max ${MAX_SCHEDULES_PER_USER}). Delete an existing schedule before creating another.`,
      );
      process.exit(1);
    }
  } catch (err) {
    console.error(`Error checking existing schedules: ${err.message}`);
    process.exit(1);
  }

  // Build target input with real userId if found
  const lambdaInput = JSON.stringify({
    userId: realUserId || userId,
    actorId,
    channelActorId,
    channel,
    channelTarget,
    message,
    scheduleId,
    scheduleName: scheduleName || `Schedule ${scheduleId}`,
    // EventBridge Scheduler fills this in at each fire; the cron Lambda names the
    // run <scheduleId>-<fire time> so a repeated fire cannot run twice.
    scheduledTime: "<aws.scheduler.scheduled-time>",
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
      // Persist the exact EventBridge name created here so update/delete
      // bind to this object instead of recomputing a name from the user
      // namespace at call time (preventing name-collision cross-tenant
      // addressing if the derivation ever changes).
      ebName: ebScheduleName,
      expression,
      timezone,
      message,
      channel,
      channelTarget,
      actorId,
      channelActorId,
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
