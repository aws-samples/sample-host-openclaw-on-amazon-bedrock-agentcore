---
name: cost-analyzer
description: ALWAYS use this skill for any cost, spending, billing, or usage questions. Runs a dedicated AI agent that cross-references AWS Cost Explorer, CloudWatch Bedrock logs, and DynamoDB token usage to produce a comprehensive report with per-user breakdown and recommendations. Do NOT use raw aws cli for cost queries — this skill provides richer cross-referenced data.
allowed-tools: Bash(node:*)
---

# Cost Analyzer Agent

**Use this skill whenever the user asks about costs, spending, usage, or billing.**
Do NOT run `aws ce` or other CLI commands directly — this agent cross-references
multiple data sources and produces a structured report that raw CLI cannot.

## Usage

### run_cost_analysis

```bash
node {baseDir}/run.js <user_id> [days]
```

- `user_id` (required): The user's namespace (e.g., `telegram_12345`)
- `days` (optional): Number of days to analyze (default: 7)

## From Agent Chat

- "How much have I spent this week?" -> run.js with days=7
- "Analyze my AWS costs for the last month" -> run.js with days=30
- "Show me a cost report" -> run.js with days=7
- "Why are my costs so high?" -> run.js with days=14

## CRITICAL: Presenting Results

After running the command, you MUST include the COMPLETE output from the command
in your response message. The user can ONLY see your assistant messages — tool
outputs are NOT visible to them. Do NOT say "see the report above" or assume
the tool output was displayed. Copy the full report text into your response.

If the output is very long, include at minimum:
1. The summary section (total spend, daily trend)
2. The per-user breakdown
3. The recommendations

## Security Notes

- Cost data is read-only — no modifications to billing or resources
- Token usage queries are scoped to the DynamoDB token-usage table
- User identity is validated (rejects default-user)
- Never use `default_user` as user_id — the script rejects it with an error
