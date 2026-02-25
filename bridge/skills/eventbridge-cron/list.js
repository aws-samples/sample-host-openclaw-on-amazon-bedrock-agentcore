#!/usr/bin/env node
/**
 * list_schedules — List all cron schedules for a user.
 * Usage: node list.js <user_id>
 */
const {
  validateUserId,
  listCronRecords,
  extractChannelInfo,
} = require("./common");

async function main() {
  const userId = process.argv[2];
  validateUserId(userId);

  // Look up real userId from DynamoDB using channel info
  const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
  const { DynamoDBDocumentClient, GetCommand } = require("@aws-sdk/lib-dynamodb");
  const REGION = process.env.AWS_REGION;
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
    // Fall through to using userId as-is
  }

  const records = await listCronRecords(realUserId || userId);

  if (records.length === 0) {
    console.log("No scheduled tasks found.");
    return;
  }

  console.log(`Scheduled tasks (${records.length}):\n`);
  for (const record of records) {
    const status = record.enabled !== false ? "ENABLED" : "DISABLED";
    console.log(`  ID: ${record.scheduleId}`);
    console.log(`  Name: ${record.scheduleName || "—"}`);
    console.log(`  Expression: ${record.expression}`);
    console.log(`  Timezone: ${record.timezone}`);
    console.log(`  Message: ${record.message}`);
    console.log(`  Status: ${status}`);
    console.log(`  Created: ${record.createdAt || "—"}`);
    console.log(`  Updated: ${record.updatedAt || "—"}`);
    console.log("");
  }
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
