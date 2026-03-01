# Experiment: Cost Analyzer Agent using Claude Agent SDK

**Branch**: `add/cost-analyzer-claude-agent-sdk`
**Date**: 2026-02-28 → 2026-03-01
**Status**: Working prototype (E2E verified via Telegram)
**Image versions**: v40–v46 (iterative development)

---

## What We Built

A dedicated **Claude Agent SDK agent** that runs inside the AgentCore container alongside OpenClaw. When a user asks about costs, the bridge intercepts the message, spawns the agent as a child process, and returns the report directly — bypassing OpenClaw entirely.

The agent autonomously queries three data sources via MCP servers, cross-references the results, and produces a comprehensive cost report:

1. **AWS Cost Explorer** (Python MCP server, stdio) — infrastructure costs by service/day
2. **CloudWatch Bedrock Logs** (same Python server) — per-model invocation stats
3. **DynamoDB Token Usage** (in-process MCP server, Node.js) — per-user token breakdown

### Architecture

```
User: "analyze my AWS costs"
  → Router Lambda receives Telegram webhook
  → Invokes AgentCore session (per-user microVM)
  → agentcore-contract.js detects cost request via regex
  → Spawns: node /skills/cost-analyzer/run.js <user_id> [days]
  → run.js creates Claude Agent SDK query():
      - CLAUDE_CODE_USE_BEDROCK=1 (routes SDK through Bedrock)
      - systemPrompt: cost analysis specialist
      - mcpServers:
          "aws-cost": Python stdio server (Cost Explorer + CloudWatch)
          "token-usage": createSdkMcpServer() in-process (DynamoDB)
      - allowedTools: [5 MCP tools]
      - maxTurns: 20, permissionMode: bypassPermissions
  → Agent runs autonomously (5-8 tool calls, ~90 seconds)
  → stdout captured by bridge → sent back to user via Telegram
```

### E2E Result

14,852-character cost report delivered to Telegram in ~108 seconds. Included:
- Daily infrastructure cost breakdown (AgentCore Runtime vCPU/memory, NAT, S3, DynamoDB, Lambda)
- Bedrock model usage stats (invocations, input/output tokens per model)
- Per-user token spend with channel and model breakdown
- Cross-referenced infrastructure vs AI model costs
- Trend analysis and actionable recommendations

---

## Key Files

| File | Purpose |
|---|---|
| `bridge/skills/cost-analyzer/run.js` | Agent runner — `query()` from `@anthropic-ai/claude-agent-sdk` |
| `bridge/skills/cost-analyzer/token-usage-server.js` | In-process MCP server for DynamoDB token queries |
| `bridge/skills/cost-analyzer/aws-cost-server/server.py` | Python MCP server for Cost Explorer + CloudWatch |
| `bridge/skills/cost-analyzer/system-prompt.md` | Specialized system prompt for cost analysis |
| `bridge/skills/cost-analyzer/SKILL.md` | OpenClaw skill manifest |
| `bridge/agentcore-contract.js` | Modified to intercept cost requests and run agent directly |
| `lambda/router/index.py` | Increased botocore read_timeout to 570s |
| `stacks/agentcore_stack.py` | IAM for Cost Explorer, CloudWatch Logs, DynamoDB |

---

## What Worked Well

### 1. Claude Agent SDK + MCP Pattern

The `query()` function from `@anthropic-ai/claude-agent-sdk` provides a clean way to run autonomous multi-step agents. Combined with MCP servers, it creates a powerful pattern:

- **`createSdkMcpServer()`** for in-process tools — no subprocess overhead, direct function calls
- **Stdio MCP servers** for language-agnostic tools — the Python Cost Explorer server communicates via JSON-RPC over stdin/stdout
- **`allowedTools`** for security — only specified MCP tools are available, no filesystem or shell access

### 2. Autonomous Multi-Step Reasoning

The cost analysis task genuinely benefits from an autonomous agent loop. A single LLM call can't:
- Query 3 different data sources
- Cross-reference infrastructure costs with token usage
- Identify anomalies (e.g., "AgentCore memory cost 2x vCPU cost suggests idle sessions")
- Generate contextual recommendations

The agent typically makes 5-8 tool calls across a 90-second session — exactly the kind of work that justifies the agent pattern.

### 3. In-Process MCP Server

`createSdkMcpServer()` + `tool()` is genuinely useful for wrapping existing Node.js functions as MCP tools without spawning a subprocess:

```javascript
const server = createSdkMcpServer({
  name: "token-usage",
  version: "1.0.0",
  tools: [
    tool("query_user_usage", "...", { user_id: z.string(), days: z.number() }, handler),
    // ...
  ],
});
```

The tools run in the same process, sharing the AWS SDK client and connection pool.

---

## What Didn't Work / Gotchas

### 1. OpenClaw WebSocket Bridge Only Streams the Final Turn

**This was the single biggest problem and required a fundamental architectural change.**

OpenClaw's WebSocket protocol only streams the **final assistant turn** to connected clients. For multi-turn tool-use agents, the actual output (the cost report) is in an **intermediate** assistant turn. The final turn is just `"NO_REPLY"` — the agent's way of saying "I've finished using tools."

This means if you run the cost analyzer as a normal OpenClaw skill (which invokes it via Bash), OpenClaw correctly captures the tool output internally, but the WebSocket bridge only sends the final `"NO_REPLY"` to the external client.

**Solution**: Bypass OpenClaw entirely. The bridge detects cost analysis requests via regex and spawns the agent as a direct child process, capturing stdout. OpenClaw never sees the message.

**Implication**: This bypass pattern works but defeats the purpose of running inside OpenClaw. The agent is essentially a sidecar that happens to share the same container.

### 2. CLAUDECODE Environment Variable Detection

The Agent SDK spawns `claude` as a subprocess. If the `CLAUDECODE` environment variable is set (which it is inside an OpenClaw container), the SDK detects it as a "nested session" and refuses to start.

**Solution**: Delete the env var before spawning:
```javascript
const agentEnv = { ...process.env, CLAUDE_CODE_USE_BEDROCK: "1" };
delete agentEnv.CLAUDECODE;
```

### 3. Root User Permission Mode

Inside AgentCore containers, processes run as root. The Agent SDK's `bypassPermissions` mode uses `--dangerously-skip-permissions`, which is blocked for root. But `dontAsk` mode combined with `allowedTools` auto-approves specified tools and silently denies everything else.

```javascript
const isRoot = process.getuid && process.getuid() === 0;
const permissionMode = isRoot ? "dontAsk" : "bypassPermissions";
```

### 4. createSdkMcpServer Requires Async Generator Input

When using `createSdkMcpServer()` (in-process MCP), the `prompt` parameter must be an **async generator**, not a plain string:

```javascript
// WRONG — fails silently
query({ prompt: "Analyze costs...", options: { mcpServers: { "token-usage": tokenUsageServer } } })

// CORRECT — async generator
async function* generateMessages() {
  yield { type: "user", message: { role: "user", content: "Analyze costs..." } };
}
query({ prompt: generateMessages(), options: { ... } })
```

This is not documented. String prompts work fine for stdio-only MCP servers.

### 5. Unpredictable Result Format

The `message.result` from `query()` can be:
- A plain string
- A JSON string of a content block: `'{"type":"text","text":"..."}'`
- A JSON string of a content block array: `'[{"type":"text","text":"..."},...]'`
- An object: `{type: "text", text: "..."}`
- An array of content blocks

We had to write an `extractText()` function that handles all 5 formats.

### 6. Botocore Read Timeout

The default botocore read timeout is 60 seconds. AgentCore cold start + container init + cost analysis can take 2-5 minutes. The Router Lambda's `invoke_agent_runtime` call would timeout and return an error to the user.

**Solution**: Increase to 570 seconds with zero retries (retries would cause duplicate processing):
```python
agentcore_client = boto3.client(
    "bedrock-agentcore",
    config=Config(read_timeout=570, retries={"max_attempts": 0}),
)
```

### 7. Skill Directory Scanner Interference

OpenClaw scans `/skills/` directories for SKILL.md files and tries to parse them. Having the system prompt, Python server, and other supporting files alongside SKILL.md confused the scanner.

**Solution**: Only SKILL.md + code files (run.js, token-usage-server.js) live in `/skills/cost-analyzer/`. Supporting files (system-prompt.md, aws-cost-server/) are copied to `/app/cost-analyzer-deps/` in the Dockerfile:
```dockerfile
COPY skills/cost-analyzer/system-prompt.md /app/cost-analyzer-deps/
COPY skills/cost-analyzer/aws-cost-server /app/cost-analyzer-deps/aws-cost-server
```

### 8. Agent SDK Cost

Each cost analysis invocation runs a full agent loop (system prompt + 5-8 tool calls + reasoning). With Claude Opus 4.6, a single report costs roughly $0.15-0.30 in tokens. This adds up for frequent or scheduled reports. A cheaper model (Sonnet) could be configured via the `subagent_model_id` CDK parameter.

---

## Honest Assessment: When to Use This Pattern

### Use a Claude Agent SDK sub-agent when:

1. **Multi-step autonomous reasoning** — the task requires querying multiple sources, cross-referencing results, and making decisions about what to query next
2. **Specialized system prompt** — the agent needs deep domain knowledge (cost interpretation, anomaly detection) that would dilute the main agent's general-purpose prompt
3. **MCP server integration** — you want to leverage existing MCP servers (the ecosystem has hundreds of ready-made tools)
4. **Isolation** — the sub-agent shouldn't have access to the main agent's tools (filesystem, channels, etc.)

### Don't use it when:

1. **OpenClaw can do it directly** — if the task is a single tool call or simple query, OpenClaw with CLI tools (e.g., `aws ce get-cost-and-usage`) is faster, cheaper, and simpler
2. **You need real-time streaming** — the Agent SDK collects the full result before returning it. Users see nothing during the 90-second execution. OpenClaw's streaming would be better UX if the WebSocket bridge supported intermediate turns
3. **Cost sensitivity** — each agent invocation costs real money. A cron job running daily cost reports at $0.20/report = $6/month per user
4. **You end up bypassing the host anyway** — if the host (OpenClaw) can't relay the sub-agent's output to the user, the sub-agent becomes a sidecar, and you've added complexity without gaining the benefits of host integration

### The fundamental tension:

The Claude Agent SDK is designed for **programmatic orchestration** — your code calls `query()`, gets a result, does something with it. OpenClaw is designed for **conversational AI** — messages flow through a chat protocol with streaming deltas. These two models don't compose well:

- Agent SDK wants to run autonomously and return a result
- OpenClaw wants to stream every token to the user in real-time
- When the SDK agent runs inside OpenClaw, the autonomous execution works fine, but the result gets trapped in an intermediate turn that never reaches the user

The bypass pattern (intercepting in the bridge) works but means the agent is effectively a separate service that shares a container for convenience, not a true skill within the OpenClaw ecosystem.

---

## If You Want to Reproduce This

### Prerequisites
- AWS account with Bedrock access (Claude model enabled)
- CDK deployed (all 7 stacks)
- Bridge container running on AgentCore

### Quick Test (Local)
```bash
cd bridge/skills/cost-analyzer
AWS_REGION=us-west-2 \
TOKEN_USAGE_TABLE_NAME=openclaw-token-usage \
CLAUDE_CODE_USE_BEDROCK=1 \
node run.js telegram_6087229962 7
```

### Deploy
```bash
# 1. Build + push container (image_version=46 in cdk.json)
docker build --platform linux/arm64 -t openclaw-bridge:v46 bridge/
# ... tag + push to ECR ...

# 2. Deploy AgentCore stack (IAM + env vars) + Router stack (read_timeout)
cdk deploy OpenClawAgentCore OpenClawRouter --require-approval never

# 3. Send "show me my cost report" via Telegram
```

---

## Iteration History

| Version | What Changed | Outcome |
|---|---|---|
| v40 | Initial Agent SDK integration as OpenClaw skill | Agent ran, but report never reached user (NO_REPLY problem) |
| v41 | Tried wrapping agent output as OpenClaw tool result | Still NO_REPLY — WebSocket only streams final turn |
| v42 | Added file-based output (write to /tmp, read back) | Partially worked but fragile |
| v43 | Switched to `--print` flag for single-shot mode | Better reliability, same NO_REPLY problem |
| v44 | Reverted to `query()` API, added extractText() | Clean API but still trapped in intermediate turns |
| v45 | Added in-process MCP server (createSdkMcpServer) | MCP tools working, report quality improved |
| v46 | **Bridge bypass** — intercept cost requests, run directly | E2E working. 14,852-char report delivered in 108s |

The core lesson: **6 iterations** to discover that the host's streaming architecture fundamentally can't relay multi-turn agent output. The solution was to bypass the host, which raises the question of whether the integration was worth the complexity.

---

## Conclusion

This experiment demonstrates that the Claude Agent SDK can run autonomous, MCP-equipped agents inside AgentCore containers. The pattern works and produces genuinely useful results (the cost report is better than any single CLI command could produce).

However, the integration with OpenClaw required a bypass that undermines the value of running inside OpenClaw in the first place. A standalone Agent SDK service (e.g., a Lambda function or separate container) would be simpler and achieve the same result.

**Best use case for this pattern**: When you genuinely need the host's other capabilities (workspace persistence, user identity, channel routing, other skills) AND the sub-agent's task can be expressed as a single tool call from the host's perspective (invoke agent, get result, present to user).

**Not ideal when**: The sub-agent produces large outputs that need to be streamed to the user, or when the host can't relay the result through its normal message flow.
