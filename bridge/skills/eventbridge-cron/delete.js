#!/usr/bin/env node
/**
 * delete_schedule — Delete an EventBridge cron schedule.
 * Usage: node delete.js <user_id> <schedule_id>
 */
const { SchedulerClient, DeleteScheduleCommand } = require("@aws-sdk/client-scheduler");
const {
  REGION,
  SCHEDULE_GROUP,
  validateUserId,
  validateEnv,
  buildScheduleName,
  getCronRecord,
  deleteCronRecord,
  extractChannelInfo,
} = require("./common");

async function main() {
  const userId = process.argv[2];
  const scheduleId = process.argv[3];

  validateUserId(userId);
  validateEnv();

  if (!scheduleId) {
    console.error("Error: schedule_id argument is required.");
    process.exit(1);
  }

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

  // Verify the schedule exists for this user
  const record = await getCronRecord(lookupUserId, scheduleId);
  if (!record) {
    console.error(`Error: Schedule ${scheduleId} not found for this user.`);
    process.exit(1);
  }

  const ebScheduleName = buildScheduleName(userId, scheduleId);
  const schedulerClient = new SchedulerClient({ region: REGION });

  // Delete from EventBridge
  try {
    await schedulerClient.send(new DeleteScheduleCommand({
      Name: ebScheduleName,
      GroupName: SCHEDULE_GROUP,
    }));
  } catch (err) {
    if (err.name === "ResourceNotFoundException") {
      // Schedule already deleted from EventBridge — clean up DynamoDB anyway
      console.log("Note: Schedule was already removed from EventBridge.");
    } else {
      console.error(`Error deleting schedule from EventBridge: ${err.message}`);
      process.exit(1);
    }
  }

  // Delete from DynamoDB
  try {
    await deleteCronRecord(lookupUserId, scheduleId);
  } catch (err) {
    console.error(`Warning: EventBridge deleted but DynamoDB cleanup failed: ${err.message}`);
  }

  console.log(`Schedule ${scheduleId} deleted successfully.`);
  if (record.scheduleName) console.log(`  Name: ${record.scheduleName}`);
  if (record.expression) console.log(`  Was: ${record.expression} (${record.timezone || "UTC"})`);
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
