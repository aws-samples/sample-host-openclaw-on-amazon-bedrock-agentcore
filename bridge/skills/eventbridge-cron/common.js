/**
 * Shared utilities for eventbridge-cron skill.
 */
const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const { DynamoDBDocumentClient, PutCommand, GetCommand, DeleteCommand, QueryCommand, UpdateCommand } = require("@aws-sdk/lib-dynamodb");
const fs = require("fs");

const REGION = process.env.AWS_REGION;
if (!REGION) {
  console.error("Error: AWS_REGION environment variable is not set.");
  process.exit(1);
}

const SCHEDULE_GROUP = process.env.EVENTBRIDGE_SCHEDULE_GROUP || "openclaw-cron";
const CRON_LAMBDA_ARN = process.env.CRON_LAMBDA_ARN;
const EVENTBRIDGE_ROLE_ARN = process.env.EVENTBRIDGE_ROLE_ARN;
const IDENTITY_TABLE_NAME = process.env.IDENTITY_TABLE_NAME;

// The contract server writes the bound user's identity here and sets USER_ID
// ("channel:id") before OpenClaw runs a skill.
const IDENTITY_FILE = "/tmp/current-identity.json";

/**
 * Namespaces ("channel_id") of the user this AgentCore Runtime session is
 * bound to, from the identity file and USER_ID. Empty when neither is set.
 */
function boundNamespaces() {
  const namespaces = [];
  try {
    const identity = JSON.parse(fs.readFileSync(IDENTITY_FILE, "utf8"));
    if (typeof identity?.actorId === "string" && identity.actorId) {
      namespaces.push(identity.actorId.replace(/:/g, "_"));
    }
  } catch {
    // No readable identity file: fall back to USER_ID alone.
  }
  if (process.env.USER_ID) namespaces.push(process.env.USER_ID.replace(/:/g, "_"));
  return namespaces;
}

/**
 * Exit unless userId is the namespace of the user this session is bound to.
 * The scoped credentials only reach that user's resources, so any other
 * namespace would fail at AWS anyway; this gives a clear error first.
 */
function assertOwnNamespace(userId) {
  const bound = boundNamespaces();
  if (bound.length === 0) {
    console.error("Error: This session's user identity is not available.");
    process.exit(1);
  }
  if (bound.some((namespace) => namespace !== userId)) {
    console.error("Error: user_id does not match the user this session belongs to. Use the user_id from your system prompt.");
    process.exit(1);
  }
}

/**
 * Internal user ID whose USER#<id> partition holds this user's CRON# records:
 * the ID the contract server bound this session to (create.js stores records
 * there too), or the namespace on deployments that do not set it.
 */
function ownerUserId(userId) {
  return process.env.INTERNAL_USER_ID || userId;
}

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
    /^(telegram|slack|discord|whatsapp|feishu|line)_[a-zA-Z0-9_-]{1,64}$/;
  if (!VALID_NAMESPACE.test(userId)) {
    console.error(
      `Error: Invalid user_id "${userId}". Must match channel_identifier format (e.g., telegram_123456, slack_username).`,
    );
    process.exit(1);
  }
  assertOwnNamespace(userId);
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

const MIN_INTERVAL_MINUTES = 5;

/** Sorted minutes (0-59) a cron() minutes field fires on, or null for syntax it does not accept. */
function cronMinutes(field) {
  const minutes = new Set();
  for (const part of field.split(",")) {
    const m = part.match(/^(?:\*|(\d{1,2})(?:-(\d{1,2}))?)(?:\/(\d{1,2}))?$/);
    if (!m) return null;
    const step = m[3] === undefined ? 1 : Number(m[3]);
    let start = 0;
    let end = 59;
    if (m[1] !== undefined) {
      start = Number(m[1]);
      // "5/10" starts at 5 and repeats to the end of the hour; a bare "5" is one minute.
      end = m[2] !== undefined ? Number(m[2]) : m[3] !== undefined ? 59 : start;
    }
    if (step < 1 || start > end || end > 59) return null;
    for (let minute = start; minute <= end; minute += step) minutes.add(minute);
  }
  return [...minutes].sort((a, b) => a - b);
}

/** Shortest time in minutes between two runs of a cron() minutes field, or null if the field is not accepted. */
function cronMinuteGap(field) {
  const minutes = cronMinutes(field);
  if (!minutes) return null;
  // From the hour's last run to the next hour's first run.
  let gap = 60 - minutes[minutes.length - 1] + minutes[0];
  for (let i = 1; i < minutes.length; i++) gap = Math.min(gap, minutes[i] - minutes[i - 1]);
  return gap;
}

/**
 * Validate a schedule expression: cron(...), rate(...), or at(...).
 * - cron() must have exactly 6 space-separated fields (Minutes Hours Day Month DayOfWeek Year)
 * - rate() must match rate(N unit) format with minimum 5-minute interval
 * - at() must match at(YYYY-MM-DDTHH:MM:SS) format
 */
function validateExpression(expression) {
  const cronRegex = /^cron\((.+)\)$/;
  const rateRegex = /^rate\(\d+\s+(minute|minutes|hour|hours|day|days)\)$/;
  const atRegex = /^at\((\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\)$/;

  if (atRegex.test(expression)) {
    // at() expression — validate the datetime is parseable
    const dateStr = expression.match(atRegex)[1];
    const parsed = new Date(dateStr);
    if (isNaN(parsed.getTime())) {
      console.error(
        `Error: Invalid at() datetime "${dateStr}". ` +
        `Must be a valid ISO 8601 datetime: at(YYYY-MM-DDTHH:MM:SS)`,
      );
      process.exit(1);
    }
    return;
  }

  if (rateRegex.test(expression)) {
    // Enforce minimum 5-minute interval for rate expressions
    const rateMatch = expression.match(/^rate\((\d+)\s+(minute|minutes)\)$/);
    if (rateMatch && parseInt(rateMatch[1], 10) < 5) {
      console.error(
        "Error: Minimum rate interval is 5 minutes. " +
        "Use rate(5 minutes) or higher to prevent cost abuse.",
      );
      process.exit(1);
    }
    return;
  }

  const cronMatch = expression.match(cronRegex);
  if (cronMatch) {
    // Validate cron has exactly 6 fields
    const fields = cronMatch[1].trim().split(/\s+/);
    if (fields.length !== 6) {
      console.error(
        `Error: cron() expression must have exactly 6 fields, got ${fields.length}.\n` +
        `Format: cron(Minutes Hours Day-of-month Month Day-of-week Year)\n` +
        `Example: cron(0 9 * * ? *)`,
      );
      process.exit(1);
    }

    // Reject cron expressions that run more often than every 5 minutes
    // (*, */2, 0-59, 0/1, 0,1,2, ...): measure the minutes field's real gap.
    const minutesField = fields[0];
    const gap = cronMinuteGap(minutesField);
    if (gap === null) {
      console.error(
        'Error: Unsupported cron() minutes field "%s". Use numbers 0-59 with , - * or /.',
        minutesField,
      );
      process.exit(1);
    }
    if (gap < MIN_INTERVAL_MINUTES) {
      console.error(
        "Error: Every-minute cron expressions are not allowed. " +
        "Minimum interval is 5 minutes. Use */5 or higher.",
      );
      process.exit(1);
    }
    return;
  }

  // No recognized format
  console.error(
    `Error: Invalid expression "${expression}". ` +
    `Must be cron(...), rate(...), or at(...) format.\n` +
    `Examples: cron(0 9 * * ? *), rate(1 hour), at(2025-12-31T23:59:00)`,
  );
  process.exit(1);
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

// EventBridge Scheduler names are at most 64 characters. "openclaw-" and
// "-<8-hex id>" leave 46 for the userId namespace.
const MAX_NAME_NAMESPACE = 64 - "openclaw-".length - 9;

/**
 * Build the EventBridge schedule name from userId and scheduleId.
 * Format: openclaw-{userId}-{scheduleId}. A userId longer than 46 characters
 * keeps its first characters plus a digest of the whole userId, so two userIds
 * that share their first 46 characters get different names.
 * Same rule as lambda/gateway_tools/eventbridge_cron/index.js.
 */
function buildScheduleName(userId, scheduleId) {
  if (userId.length <= MAX_NAME_NAMESPACE) return `openclaw-${userId}-${scheduleId}`;
  const { createHash } = require("crypto");
  const digest = createHash("sha256").update(userId).digest("hex").slice(0, 12);
  return `openclaw-${userId.slice(0, MAX_NAME_NAMESPACE - 13)}-${digest}-${scheduleId}`;
}

/** The name a schedule of a userId longer than 46 characters had before the digest; null otherwise. */
function legacyScheduleName(userId, scheduleId) {
  return userId.length > MAX_NAME_NAMESPACE
    ? `openclaw-${userId.slice(0, MAX_NAME_NAMESPACE)}-${scheduleId}`
    : null;
}

/**
 * Name of this user's schedule. Schedules created before long userIds got a
 * digest keep their old name; that name is used only when the schedule there
 * was created for this userId (its target input's actorId).
 */
async function resolveScheduleName(schedulerClient, userId, scheduleId) {
  const name = buildScheduleName(userId, scheduleId);
  const legacy = legacyScheduleName(userId, scheduleId);
  if (!legacy) return name;
  const { GetScheduleCommand } = require("@aws-sdk/client-scheduler");
  const get = (Name) => schedulerClient.send(new GetScheduleCommand({ Name, GroupName: SCHEDULE_GROUP }));
  try {
    await get(name);
    return name;
  } catch (err) {
    if (!err || err.name !== "ResourceNotFoundException") throw err;
  }
  let schedule;
  try {
    schedule = await get(legacy);
  } catch (err) {
    if (!err || err.name !== "ResourceNotFoundException") throw err;
    return name;
  }
  let actorId = null;
  try {
    actorId = JSON.parse(schedule.Target?.Input || "{}").actorId;
  } catch {
    // Unreadable input: not provably this user's schedule.
  }
  return typeof actorId === "string" && actorId.replace(/:/g, "_") === userId ? legacy : name;
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
 * Read the current turn's delivery destination without changing its storage owner.
 */
function resolveScheduleDelivery(userId, { channel, channelTarget } = {}) {
  const identity = JSON.parse(fs.readFileSync(IDENTITY_FILE, "utf8"));
  if (typeof identity.actorId !== "string" || identity.actorId.replace(/:/g, "_") !== userId) {
    throw new Error("Schedule namespace does not match the AgentCore Runtime owner");
  }
  if (typeof identity.channel !== "string" || !identity.channel ||
      typeof identity.channelActorId !== "string" ||
      !identity.channelActorId.startsWith(`${identity.channel}:`) ||
      !identity.channelActorId.slice(identity.channel.length + 1)) {
    throw new Error("Current channel delivery identity is unavailable");
  }
  if (channel && channel !== identity.channel) {
    throw new Error("Schedule channel must match the current conversation");
  }
  const target = channelTarget === "" ? identity.channelTarget : (channelTarget ?? identity.channelTarget);
  if (typeof target !== "string" || !target) {
    throw new Error("Schedule delivery target is unavailable");
  }
  return {
    actorId: identity.actorId,
    channelActorId: identity.channelActorId,
    channel: identity.channel,
    channelTarget: target,
  };
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
  assertOwnNamespace,
  ownerUserId,
  validateEnv,
  validateExpression,
  validateTimezone,
  cronMinuteGap,
  generateScheduleId,
  buildScheduleName,
  legacyScheduleName,
  resolveScheduleName,
  extractChannelInfo,
  resolveScheduleDelivery,
  getDynamoClient,
  saveCronRecord,
  getCronRecord,
  deleteCronRecord,
  listCronRecords,
  updateCronRecord,
};
