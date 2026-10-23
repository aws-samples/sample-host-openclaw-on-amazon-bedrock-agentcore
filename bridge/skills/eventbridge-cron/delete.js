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
  resolveScheduleName,
  getCronRecord,
  deleteCronRecord,
  ownerUserId,
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

  const lookupUserId = ownerUserId(userId);

  // Verify the schedule exists for this user
  const record = await getCronRecord(lookupUserId, scheduleId);
  if (!record) {
    console.error(`Error: Schedule ${scheduleId} not found for this user.`);
    process.exit(1);
  }

  const schedulerClient = new SchedulerClient({ region: REGION });
  const ebScheduleName = await resolveScheduleName(schedulerClient, userId, scheduleId);

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
