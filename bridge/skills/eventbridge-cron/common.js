/**
 * Shared utilities for eventbridge-cron skill.
 */
const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const { DynamoDBDocumentClient, PutCommand, GetCommand, DeleteCommand, QueryCommand, UpdateCommand } = require("@aws-sdk/lib-dynamodb");

const REGION = process.env.AWS_REGION;
if (!REGION) {
  console.error("Error: AWS_REGION environment variable is not set.");
  process.exit(1);
}

const SCHEDULE_GROUP = process.env.EVENTBRIDGE_SCHEDULE_GROUP || "openclaw-cron";
const CRON_LAMBDA_ARN = process.env.CRON_LAMBDA_ARN;
const EVENTBRIDGE_ROLE_ARN = process.env.EVENTBRIDGE_ROLE_ARN;
const IDENTITY_TABLE_NAME = process.env.IDENTITY_TABLE_NAME;

/**
 * Validate that userId is present, not the default-user fallback,
 * and matches the expected channel_identifier namespace pattern.
 */
function validateUserId(userId) {
  if (!userId) {
    console.error("Error: user_id argument is required.");
    process.exit(1);
  }
  if (userId === "default-user" || userId === "default_user") {
    console.error(
      "Error: Cannot operate on schedules for default-user. User identity was not resolved.",
    );
    process.exit(1);
  }
  const VALID_NAMESPACE =
    /^(telegram|slack|discord|whatsapp)_[a-zA-Z0-9_-]{1,64}$/;
  if (!VALID_NAMESPACE.test(userId)) {
    console.error(
      `Error: Invalid user_id "${userId}". Must match channel_identifier format (e.g., telegram_123456, slack_username).`,
    );
    process.exit(1);
  }
}

/**
 * Validate required environment variables for schedule operations.
 */
function validateEnv() {
  if (!CRON_LAMBDA_ARN) {
    console.error("Error: CRON_LAMBDA_ARN environment variable not set.");
    process.exit(1);
  }
  if (!EVENTBRIDGE_ROLE_ARN) {
    console.error("Error: EVENTBRIDGE_ROLE_ARN environment variable not set.");
    process.exit(1);
  }
  if (!IDENTITY_TABLE_NAME) {
    console.error("Error: IDENTITY_TABLE_NAME environment variable not set.");
    process.exit(1);
  }
}

/**
 * Validate a cron or rate expression.
 * Must match cron(...) or rate(...) format.
 */
function validateExpression(expression) {
  const cronRegex = /^cron\(.+\)$/;
  const rateRegex = /^rate\(\d+\s+(minute|minutes|hour|hours|day|days)\)$/;

  if (!cronRegex.test(expression) && !rateRegex.test(expression)) {
    console.error(
      `Error: Invalid expression "${expression}". Must be cron(...) or rate(...) format.\n` +
      `Examples: cron(0 9 * * ? *), rate(1 hour), rate(5 minutes)`,
    );
    process.exit(1);
  }
}

/**
 * Validate an IANA timezone string using Intl.DateTimeFormat.
 */
function validateTimezone(timezone) {
  try {
    Intl.DateTimeFormat(undefined, { timeZone: timezone });
  } catch {
    console.error(
      `Error: Invalid timezone "${timezone}". Must be a valid IANA timezone.\n` +
      `Examples: Asia/Tokyo, America/New_York, Europe/London, UTC`,
    );
    process.exit(1);
  }
}

/**
 * Generate an 8-character hex schedule ID.
 */
function generateScheduleId() {
  const { randomBytes } = require("crypto");
  return randomBytes(4).toString("hex");
}

/**
 * Build the EventBridge schedule name from userId and scheduleId.
 * Format: openclaw-{userId}-{scheduleId}
 */
function buildScheduleName(userId, scheduleId) {
  return `openclaw-${userId}-${scheduleId}`;
}

/**
 * Extract channel and channelTarget from userId.
 * userId format: telegram_12345 -> channel=telegram, channelTarget=12345
 */
function extractChannelInfo(userId) {
  const underscoreIdx = userId.indexOf("_");
  if (underscoreIdx === -1) {
    return { channel: "unknown", channelTarget: userId };
  }
  return {
    channel: userId.substring(0, underscoreIdx),
    channelTarget: userId.substring(underscoreIdx + 1),
  };
}

/**
 * Build the actorId from channel info.
 * channel=telegram, channelTarget=12345 -> actorId=telegram:12345
 */
function buildActorId(channel, channelTarget) {
  return `${channel}:${channelTarget}`;
}

/**
 * Get a DynamoDB Document Client.
 */
function getDynamoClient() {
  const client = new DynamoDBClient({ region: REGION });
  return DynamoDBDocumentClient.from(client);
}

/**
 * Save a cron record to DynamoDB.
 */
async function saveCronRecord(userId, record) {
  const docClient = getDynamoClient();
  await docClient.send(new PutCommand({
    TableName: IDENTITY_TABLE_NAME,
    Item: {
      PK: `USER#${userId}`,
      SK: `CRON#${record.scheduleId}`,
      ...record,
    },
  }));
}

/**
 * Get a cron record from DynamoDB.
 */
async function getCronRecord(userId, scheduleId) {
  const docClient = getDynamoClient();
  const resp = await docClient.send(new GetCommand({
    TableName: IDENTITY_TABLE_NAME,
    Key: {
      PK: `USER#${userId}`,
      SK: `CRON#${scheduleId}`,
    },
  }));
  return resp.Item || null;
}

/**
 * Delete a cron record from DynamoDB.
 */
async function deleteCronRecord(userId, scheduleId) {
  const docClient = getDynamoClient();
  await docClient.send(new DeleteCommand({
    TableName: IDENTITY_TABLE_NAME,
    Key: {
      PK: `USER#${userId}`,
      SK: `CRON#${scheduleId}`,
    },
  }));
}

/**
 * Query all cron records for a user.
 */
async function listCronRecords(userId) {
  const docClient = getDynamoClient();
  const resp = await docClient.send(new QueryCommand({
    TableName: IDENTITY_TABLE_NAME,
    KeyConditionExpression: "PK = :pk AND begins_with(SK, :sk)",
    ExpressionAttributeValues: {
      ":pk": `USER#${userId}`,
      ":sk": "CRON#",
    },
  }));
  return resp.Items || [];
}

/**
 * Update specific fields of a cron record in DynamoDB.
 */
async function updateCronRecord(userId, scheduleId, updates) {
  const docClient = getDynamoClient();
  const expressions = [];
  const attrValues = {};
  const attrNames = {};

  for (const [key, value] of Object.entries(updates)) {
    const placeholder = `:${key}`;
    const nameRef = `#${key}`;
    expressions.push(`${nameRef} = ${placeholder}`);
    attrValues[placeholder] = value;
    attrNames[nameRef] = key;
  }

  if (expressions.length === 0) return;

  await docClient.send(new UpdateCommand({
    TableName: IDENTITY_TABLE_NAME,
    Key: {
      PK: `USER#${userId}`,
      SK: `CRON#${scheduleId}`,
    },
    UpdateExpression: `SET ${expressions.join(", ")}`,
    ExpressionAttributeValues: attrValues,
    ExpressionAttributeNames: attrNames,
  }));
}

module.exports = {
  REGION,
  SCHEDULE_GROUP,
  CRON_LAMBDA_ARN,
  EVENTBRIDGE_ROLE_ARN,
  IDENTITY_TABLE_NAME,
  validateUserId,
  validateEnv,
  validateExpression,
  validateTimezone,
  generateScheduleId,
  buildScheduleName,
  extractChannelInfo,
  buildActorId,
  getDynamoClient,
  saveCronRecord,
  getCronRecord,
  deleteCronRecord,
  listCronRecords,
  updateCronRecord,
};
