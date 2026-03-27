#!/usr/bin/env node
/**
 * list_schedules — List all cron schedules for a user.
 * Usage: node list.js <user_id>
 */
const {
  validateUserId,
  validateEnv,
  listCronRecords,
  extractChannelInfo,
} = require("./common");

const DAY_NAMES = { "1": "Sunday", "2": "Monday", "3": "Tuesday", "4": "Wednesday", "5": "Thursday", "6": "Friday", "7": "Saturday",
  SUN: "Sunday", MON: "Monday", TUE: "Tuesday", WED: "Wednesday", THU: "Thursday", FRI: "Friday", SAT: "Saturday" };

/**
 * Convert a schedule expression + timezone into a human-readable description.
 */
function describeSchedule(expression, timezone) {
  const tz = timezone || "UTC";

  // rate(N unit)
  const rateMatch = expression.match(/^rate\((\d+)\s+(\w+)\)$/);
  if (rateMatch) {
    return `every ${rateMatch[1]} ${rateMatch[2]}`;
  }

  // at(datetime)
  const atMatch = expression.match(/^at\((.+)\)$/);
  if (atMatch) {
    return `once at ${atMatch[1]} ${tz}`;
  }

  // cron(min hour dom month dow year)
  const cronMatch = expression.match(/^cron\((.+)\)$/);
  if (cronMatch) {
    const fields = cronMatch[1].trim().split(/\s+/);
    if (fields.length !== 6) return `${expression} ${tz}`;

    const [minutes, hours, dom, month, dow] = fields;
    const time = `${hours.padStart(2, "0")}:${minutes.padStart(2, "0")}`;

    // Weekday range (e.g. MON-FRI)
    if (dom === "?" && /^[A-Z]{3}-[A-Z]{3}$/.test(dow)) {
      const [start, end] = dow.split("-");
      return `${DAY_NAMES[start] || start} to ${DAY_NAMES[end] || end} at ${time} ${tz}`;
    }

    // Specific day of week (e.g. MON, 2)
    if (dom === "?" && dow !== "*") {
      const dayName = DAY_NAMES[dow.toUpperCase()] || DAY_NAMES[dow] || dow;
      return `every ${dayName} at ${time} ${tz}`;
    }

    // Daily (dom=? or *, dow=*)
    if ((dom === "?" || dom === "*") && (dow === "*" || dow === "?")) {
      return `daily at ${time} ${tz}`;
    }

    // Monthly on specific day (dom=N, month=*)
    if (/^\d+$/.test(dom) && month === "*") {
      const suffix = dom === "1" || dom === "21" || dom === "31" ? "st"
        : dom === "2" || dom === "22" ? "nd"
        : dom === "3" || dom === "23" ? "rd" : "th";
      return `monthly on the ${dom}${suffix} at ${time} ${tz}`;
    }

    // Fallback: show simplified cron with timezone
    return `${expression} (${tz})`;
  }

  return `${expression} (${tz})`;
}

async function main() {
  const userId = process.argv[2];
  validateUserId(userId);
  validateEnv();

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
    const schedule = describeSchedule(record.expression, record.timezone);
    console.log(`  ID: ${record.scheduleId}`);
    console.log(`  Name: ${record.scheduleName || "—"}`);
    console.log(`  Schedule: ${schedule}`);
    console.log(`  Raw: ${record.expression}`);
    console.log(`  Message: ${record.message}`);
    console.log(`  Status: ${status}`);
    console.log(`  Created: ${record.createdAt || "—"}`);
    console.log("");
  }
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
