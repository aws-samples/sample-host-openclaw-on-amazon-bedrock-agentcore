#!/usr/bin/env node
/**
 * Token Usage MCP Server — DynamoDB token usage queries via MCP protocol.
 *
 * Implements the Model Context Protocol (JSON-RPC 2.0 over stdio) to expose
 * three tools for querying the openclaw-token-usage DynamoDB table:
 *   - query_user_usage:  Per-user token usage aggregated by day
 *   - query_daily_totals: System-wide daily cost totals
 *   - query_top_users:   Top users by cost for a specific date
 *
 * Runs as a stdio subprocess spawned by the Claude Code SDK.
 */

const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const {
  DynamoDBDocumentClient,
  QueryCommand,
} = require("@aws-sdk/lib-dynamodb");
const readline = require("readline");

const REGION = process.env.AWS_REGION || "us-west-2";
const TABLE_NAME =
  process.env.TOKEN_USAGE_TABLE_NAME || "openclaw-token-usage";

const ddbClient = new DynamoDBClient({ region: REGION });
const docClient = DynamoDBDocumentClient.from(ddbClient);

// --- Utilities ---

/**
 * Convert namespace (telegram_12345) to actor ID (telegram:12345).
 */
function namespaceToActorId(namespace) {
  const idx = namespace.indexOf("_");
  if (idx === -1) return namespace;
  return namespace.substring(0, idx) + ":" + namespace.substring(idx + 1);
}

/**
 * Get date strings for the last N days in yyyy-mm-dd format.
 */
function getDateRange(days) {
  const dates = [];
  const now = new Date();
  for (let i = 0; i < days; i++) {
    const d = new Date(now);
    d.setDate(d.getDate() - i);
    dates.push(d.toISOString().slice(0, 10));
  }
  return dates;
}

// --- DynamoDB Query Handlers ---

/**
 * Query a specific user's token usage, aggregated by day.
 */
async function queryUserUsage(userId, days) {
  const actorId = namespaceToActorId(userId);
  const dates = getDateRange(days);
  const oldestDate = dates[dates.length - 1];

  const resp = await docClient.send(
    new QueryCommand({
      TableName: TABLE_NAME,
      KeyConditionExpression: "PK = :pk AND SK >= :sk",
      ExpressionAttributeValues: {
        ":pk": `USER#${actorId}`,
        ":sk": `DATE#${oldestDate}`,
      },
    }),
  );

  // Aggregate by date
  const byDate = {};
  for (const item of resp.Items || []) {
    const skParts = item.SK.split("#");
    const date = skParts[1]; // DATE#yyyy-mm-dd#CHANNEL#...
    if (!byDate[date]) {
      byDate[date] = {
        date,
        inputTokens: 0,
        outputTokens: 0,
        totalTokens: 0,
        estimatedCostUSD: 0,
        invocationCount: 0,
        channels: new Set(),
        models: new Set(),
      };
    }
    byDate[date].inputTokens += item.inputTokens || 0;
    byDate[date].outputTokens += item.outputTokens || 0;
    byDate[date].totalTokens += item.totalTokens || 0;
    byDate[date].estimatedCostUSD += parseFloat(item.estimatedCostUSD || "0");
    byDate[date].invocationCount += item.invocationCount || 0;
    if (item.channel) byDate[date].channels.add(item.channel);
    if (item.modelId) byDate[date].models.add(item.modelId);
  }

  // Convert Sets to arrays for JSON serialization
  const records = Object.values(byDate)
    .map((d) => ({
      ...d,
      channels: Array.from(d.channels),
      models: Array.from(d.models),
      estimatedCostUSD: d.estimatedCostUSD.toFixed(6),
    }))
    .sort((a, b) => b.date.localeCompare(a.date));

  return {
    userId,
    actorId,
    days,
    records,
    totalCost: records
      .reduce((sum, d) => sum + parseFloat(d.estimatedCostUSD), 0)
      .toFixed(6),
    totalTokens: records.reduce((sum, d) => sum + d.totalTokens, 0),
    totalInvocations: records.reduce((sum, d) => sum + d.invocationCount, 0),
  };
}

/**
 * Query system-wide daily totals from GSI3.
 */
async function queryDailyTotals(days) {
  const dates = getDateRange(days);
  const results = [];

  for (const date of dates) {
    const resp = await docClient.send(
      new QueryCommand({
        TableName: TABLE_NAME,
        IndexName: "GSI3",
        KeyConditionExpression: "GSI3PK = :pk",
        ExpressionAttributeValues: {
          ":pk": `DATE#${date}`,
        },
      }),
    );

    let totalCost = 0;
    let totalTokens = 0;
    let totalInvocations = 0;
    const users = new Set();

    for (const item of resp.Items || []) {
      totalCost += parseFloat(item.estimatedCostUSD || "0");
      totalTokens += item.totalTokens || 0;
      totalInvocations += item.invocationCount || 0;
      if (item.actorId) users.add(item.actorId);
    }

    results.push({
      date,
      totalCost: totalCost.toFixed(6),
      totalTokens,
      totalInvocations,
      uniqueUsers: users.size,
    });
  }

  return {
    days,
    dailyTotals: results.sort((a, b) => b.date.localeCompare(a.date)),
    grandTotalCost: results
      .reduce((sum, d) => sum + parseFloat(d.totalCost), 0)
      .toFixed(6),
    grandTotalTokens: results.reduce((sum, d) => sum + d.totalTokens, 0),
  };
}

/**
 * Query top users by estimated cost for a specific date from GSI3.
 */
async function queryTopUsers(date) {
  const resp = await docClient.send(
    new QueryCommand({
      TableName: TABLE_NAME,
      IndexName: "GSI3",
      KeyConditionExpression: "GSI3PK = :pk",
      ExpressionAttributeValues: {
        ":pk": `DATE#${date}`,
      },
      ScanIndexForward: false, // Descending by cost (GSI3SK = COST#...)
    }),
  );

  // Aggregate by user (multiple records per user per day)
  const byUser = {};
  for (const item of resp.Items || []) {
    const actorId = item.actorId || "unknown";
    if (!byUser[actorId]) {
      byUser[actorId] = {
        actorId,
        totalCost: 0,
        totalTokens: 0,
        invocationCount: 0,
      };
    }
    byUser[actorId].totalCost += parseFloat(item.estimatedCostUSD || "0");
    byUser[actorId].totalTokens += item.totalTokens || 0;
    byUser[actorId].invocationCount += item.invocationCount || 0;
  }

  const ranked = Object.values(byUser)
    .sort((a, b) => b.totalCost - a.totalCost)
    .slice(0, 10)
    .map((u, i) => ({
      rank: i + 1,
      ...u,
      totalCost: u.totalCost.toFixed(6),
    }));

  return { date, topUsers: ranked };
}

// --- MCP Protocol (JSON-RPC 2.0 over stdio, newline-delimited) ---

const TOOLS = [
  {
    name: "query_user_usage",
    description:
      "Query a specific user's token usage from DynamoDB, aggregated by day. " +
      "Returns per-day breakdown of input/output tokens, cost, invocations, channels, and models.",
    inputSchema: {
      type: "object",
      properties: {
        user_id: {
          type: "string",
          description:
            "User namespace (e.g., telegram_12345). Converted to actor ID format internally.",
        },
        days: {
          type: "number",
          description: "Number of days to query (default: 7)",
        },
      },
      required: ["user_id"],
    },
  },
  {
    name: "query_daily_totals",
    description:
      "Query system-wide daily token usage totals. Returns per-day totals across all users " +
      "including total cost, tokens, invocations, and unique user count.",
    inputSchema: {
      type: "object",
      properties: {
        days: {
          type: "number",
          description: "Number of days to query (default: 7)",
        },
      },
    },
  },
  {
    name: "query_top_users",
    description:
      "Query the top users by estimated cost for a specific date. " +
      "Returns up to 10 users ranked by their token spend.",
    inputSchema: {
      type: "object",
      properties: {
        date: {
          type: "string",
          description: "Date in yyyy-mm-dd format (e.g., 2026-02-28)",
        },
      },
      required: ["date"],
    },
  },
];

function sendResponse(response) {
  process.stdout.write(JSON.stringify(response) + "\n");
}

async function handleMessage(msg) {
  const { id, method, params } = msg;

  // Notifications (no id) — acknowledge silently
  if (id === undefined || id === null) return null;

  switch (method) {
    case "initialize":
      return {
        jsonrpc: "2.0",
        id,
        result: {
          protocolVersion: "2024-11-05",
          capabilities: { tools: {} },
          serverInfo: { name: "token-usage", version: "1.0.0" },
        },
      };

    case "tools/list":
      return {
        jsonrpc: "2.0",
        id,
        result: { tools: TOOLS },
      };

    case "tools/call": {
      const toolName = params?.name;
      const args = params?.arguments || {};
      let result;

      try {
        switch (toolName) {
          case "query_user_usage":
            result = await queryUserUsage(args.user_id, args.days || 7);
            break;
          case "query_daily_totals":
            result = await queryDailyTotals(args.days || 7);
            break;
          case "query_top_users":
            result = await queryTopUsers(args.date);
            break;
          default:
            return {
              jsonrpc: "2.0",
              id,
              error: { code: -32601, message: `Unknown tool: ${toolName}` },
            };
        }
        return {
          jsonrpc: "2.0",
          id,
          result: {
            content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
          },
        };
      } catch (err) {
        return {
          jsonrpc: "2.0",
          id,
          result: {
            content: [{ type: "text", text: `Error: ${err.message}` }],
            isError: true,
          },
        };
      }
    }

    default:
      return {
        jsonrpc: "2.0",
        id,
        error: { code: -32601, message: `Method not found: ${method}` },
      };
  }
}

// --- Export query functions for programmatic use (Claude Agent SDK in-process MCP) ---
module.exports = {
  queryUserUsage,
  queryDailyTotals,
  queryTopUsers,
  namespaceToActorId,
  getDateRange,
};

// --- Stdio MCP server (only when run directly, not when imported) ---
if (require.main === module) {
  const rl = readline.createInterface({ input: process.stdin, terminal: false });

  rl.on("line", async (line) => {
    if (!line.trim()) return;
    try {
      const msg = JSON.parse(line);
      const response = await handleMessage(msg);
      if (response) {
        sendResponse(response);
      }
    } catch (err) {
      sendResponse({
        jsonrpc: "2.0",
        id: null,
        error: { code: -32700, message: `Parse error: ${err.message}` },
      });
    }
  });

  // Keep process alive waiting for stdin
  process.stdin.resume();
}
