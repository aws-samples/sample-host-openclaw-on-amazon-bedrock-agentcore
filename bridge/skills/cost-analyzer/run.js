#!/usr/bin/env node
/**
 * Cost Analysis Agent Runner — Claude Agent SDK
 *
 * Uses the Claude Agent SDK's query() function to run a dedicated cost analysis
 * agent with two MCP servers:
 *   - In-process token-usage server (DynamoDB queries via createSdkMcpServer)
 *   - Stdio aws-cost server (Python, Cost Explorer + CloudWatch logs)
 *
 * The agent autonomously queries multiple data sources, cross-references
 * the results, and produces a comprehensive cost report.
 *
 * Usage: node run.js <user_id> [days]
 */

const { query, tool, createSdkMcpServer } = require("@anthropic-ai/claude-agent-sdk");
const { z } = require("zod");
const { readFileSync, existsSync } = require("fs");
const path = require("path");

// Import DynamoDB query functions from token-usage-server
const {
  queryUserUsage,
  queryDailyTotals,
  queryTopUsers,
} = require("./token-usage-server");

// Supporting files live in /app/cost-analyzer-deps/ in the container (outside /skills/
// to avoid interfering with OpenClaw's skill scanner). Fall back to __dirname for local dev.
const DEPS_DIR = existsSync("/app/cost-analyzer-deps")
  ? "/app/cost-analyzer-deps"
  : __dirname;

// Read the specialized system prompt
const SYSTEM_PROMPT = readFileSync(
  path.join(DEPS_DIR, "system-prompt.md"),
  "utf8",
);

async function main() {
  const userId = process.argv[2];
  const days = parseInt(process.argv[3] || "7", 10);

  if (!userId) {
    console.error("Usage: node run.js <user_id> [days]");
    console.error("  user_id: User namespace (e.g., telegram_12345)");
    console.error("  days:    Number of days to analyze (default: 7)");
    process.exit(1);
  }

  // Validate user_id is not the default fallback
  if (userId === "default-user" || userId === "default_user") {
    console.error(
      "Error: Cannot analyze costs for default-user. User identity was not resolved.",
    );
    process.exit(1);
  }

  const region = process.env.AWS_REGION || "us-west-2";
  const logGroupName =
    process.env.BEDROCK_LOG_GROUP_NAME || "/aws/bedrock/invocation-logs";

  // --- In-process MCP server for DynamoDB token usage queries ---
  // Uses createSdkMcpServer() + tool() from the Claude Agent SDK to define
  // tools that run in the same process (no subprocess, no stdio).
  const tokenUsageServer = createSdkMcpServer({
    name: "token-usage",
    version: "1.0.0",
    tools: [
      tool(
        "query_user_usage",
        "Query a specific user's token usage from DynamoDB, aggregated by day. " +
          "Returns per-day breakdown of input/output tokens, cost, invocations, channels, and models.",
        {
          user_id: z
            .string()
            .describe(
              "User namespace (e.g., telegram_12345). Converted to actor ID format internally.",
            ),
          days: z
            .number()
            .optional()
            .describe("Number of days to query (default: 7)"),
        },
        async (args) => {
          const result = await queryUserUsage(args.user_id, args.days || 7);
          return {
            content: [
              { type: "text", text: JSON.stringify(result, null, 2) },
            ],
          };
        },
      ),
      tool(
        "query_daily_totals",
        "Query system-wide daily token usage totals. Returns per-day totals across all users " +
          "including total cost, tokens, invocations, and unique user count.",
        {
          days: z
            .number()
            .optional()
            .describe("Number of days to query (default: 7)"),
        },
        async (args) => {
          const result = await queryDailyTotals(args.days || 7);
          return {
            content: [
              { type: "text", text: JSON.stringify(result, null, 2) },
            ],
          };
        },
      ),
      tool(
        "query_top_users",
        "Query the top users by estimated cost for a specific date. " +
          "Returns up to 10 users ranked by their token spend.",
        {
          date: z
            .string()
            .describe("Date in yyyy-mm-dd format (e.g., 2026-02-28)"),
        },
        async (args) => {
          const result = await queryTopUsers(args.date);
          return {
            content: [
              { type: "text", text: JSON.stringify(result, null, 2) },
            ],
          };
        },
      ),
    ],
  });

  // --- Build the user prompt ---
  const prompt = [
    `Analyze AWS costs and token usage for the OpenClaw platform over the last ${days} days.`,
    `Focus on user: ${userId}`,
    "",
    "Instructions:",
    "1. Query Cost Explorer for a detailed daily breakdown of ALL AWS service costs",
    "2. Query Bedrock daily usage stats from CloudWatch logs",
    "3. Query the token usage table for this user's per-day breakdown",
    "4. Query system-wide daily totals for comparison",
    "5. Query top users by cost to see where this user ranks",
    "6. Cross-reference infrastructure costs with AI model costs",
    "7. Generate a comprehensive report with actionable recommendations",
  ].join("\n");

  // --- Streaming input mode (required for in-process MCP servers) ---
  async function* generateMessages() {
    yield {
      type: "user",
      message: { role: "user", content: prompt },
    };
  }

  // --- Determine model (use SUBAGENT_MODEL if set, otherwise SDK default) ---
  const model = process.env.SUBAGENT_MODEL || undefined;

  // --- Build clean env (unset CLAUDECODE to avoid nested-session detection) ---
  const agentEnv = { ...process.env, CLAUDE_CODE_USE_BEDROCK: "1" };
  delete agentEnv.CLAUDECODE;

  // --- Permission mode: bypassPermissions when not root, dontAsk when root ---
  // Root user (AgentCore container) can't use --dangerously-skip-permissions.
  // dontAsk + allowedTools auto-approves MCP tools, denies everything else silently.
  const isRoot = process.getuid && process.getuid() === 0;
  const permissionMode = isRoot ? "dontAsk" : "bypassPermissions";

  // --- Run the cost analysis agent ---
  try {
    let finalResult = "";

    for await (const message of query({
      prompt: generateMessages(),
      options: {
        systemPrompt: SYSTEM_PROMPT,
        model,
        mcpServers: {
          // Python stdio server for AWS Cost Explorer + CloudWatch Logs
          "aws-cost": {
            type: "stdio",
            command: "python3",
            args: [path.join(DEPS_DIR, "aws-cost-server", "server.py")],
            env: {
              AWS_REGION: region,
              BEDROCK_LOG_GROUP_NAME: logGroupName,
            },
          },
          // In-process server for DynamoDB token usage queries
          "token-usage": tokenUsageServer,
        },
        allowedTools: [
          "mcp__aws-cost__get_detailed_breakdown_by_day",
          "mcp__aws-cost__get_bedrock_daily_usage_stats",
          "mcp__token-usage__query_user_usage",
          "mcp__token-usage__query_daily_totals",
          "mcp__token-usage__query_top_users",
        ],
        maxTurns: 20,
        permissionMode,
        allowDangerouslySkipPermissions: !isRoot,
        persistSession: false,
        tools: [],
        settingSources: [],
        env: agentEnv,
        stderr: (data) => process.stderr.write(data),
      },
    })) {
      if (message.type === "result") {
        if (message.subtype === "success") {
          finalResult = message.result;
        } else {
          // Error subtypes: error_max_turns, error_during_execution, error_max_budget_usd
          const errors = message.errors || [];
          console.error(
            `Cost analysis agent ended with ${message.subtype}: ${errors.join(", ") || "unknown error"}`,
          );
          // Still output any partial result if available
          if (message.result) {
            console.log(message.result);
          }
          process.exit(1);
        }
      }
    }

    if (finalResult) {
      const reportText = extractText(finalResult);
      console.log(reportText);
      // Also write to file as backup (bridge reads this if stdout capture fails)
      try {
        require("fs").writeFileSync("/tmp/cost-report-latest.txt", reportText);
      } catch {}
    } else {
      console.error("Cost analysis agent produced no result.");
      process.exit(1);
    }
  } catch (err) {
    console.error(`Cost analysis agent error: ${err.message}`);
    process.exit(1);
  }
}

/**
 * Extract plain text from the SDK result.
 * The result may be:
 *   - Plain text string
 *   - JSON string of a content block: '{"type":"text","text":"..."}'
 *   - JSON string of content block array: '[{"type":"text","text":"..."},...]'
 *   - An object: {type: "text", text: "..."}
 *   - An array of content blocks: [{type: "text", text: "..."}, ...]
 */
function extractText(result) {
  if (result == null) return "";

  // Handle arrays directly (e.g., SDK returns array of content blocks)
  if (Array.isArray(result)) {
    const texts = result
      .filter((b) => b && b.type === "text" && typeof b.text === "string")
      .map((b) => b.text);
    return texts.length > 0 ? texts.join("\n\n") : JSON.stringify(result, null, 2);
  }

  // Handle objects directly (e.g., single content block)
  if (typeof result === "object") {
    if (result.type === "text" && typeof result.text === "string") {
      return result.text;
    }
    return JSON.stringify(result, null, 2);
  }

  if (typeof result !== "string") return String(result);

  // Try to parse string as JSON content block(s)
  try {
    const parsed = JSON.parse(result);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed) && parsed.type === "text" && typeof parsed.text === "string") {
      return parsed.text;
    }
    if (Array.isArray(parsed)) {
      const texts = parsed
        .filter((b) => b && b.type === "text" && typeof b.text === "string")
        .map((b) => b.text);
      return texts.length > 0 ? texts.join("\n\n") : result;
    }
  } catch {
    // Not JSON — return as-is (plain text)
  }
  return result;
}

main();
