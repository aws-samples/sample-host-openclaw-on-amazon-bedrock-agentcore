---
name: aws-mcp
description: "On-demand activation of AWS MCP servers (API, IaC, AgentCore). Enables servers only when needed to minimize context window usage."
user-invocable: true
---

# AWS MCP Servers (On-Demand)

Three AWS MCP servers are configured in `.mcp.json` but **disabled by default** to avoid filling the context window with tool descriptions. This skill activates them on demand.

## Available Servers

| Server ID | Package | Tools Provided | Use When |
|-----------|---------|---------------|----------|
| `aws-api` | `awslabs.aws-api-mcp-server` | `call_aws`, `suggest_aws_commands`, `get_execution_plan` | Running AWS CLI commands, querying AWS resources, managing infrastructure |
| `aws-iac` | `awslabs.aws-iac-mcp-server` | `validate_cloudformation_template`, `check_cloudformation_template_compliance`, `troubleshoot_cloudformation_deployment`, `search_cloudformation_documentation`, `search_cdk_documentation`, `search_cdk_samples_and_constructs`, `cdk_best_practices` | CDK/CloudFormation validation, compliance checks, troubleshooting deployments, searching CDK docs |
| `bedrock-agentcore` | `awslabs.amazon-bedrock-agentcore-mcp-server` | `search_agentcore_docs`, `fetch_agentcore_doc`, `manage_agentcore_runtime`, `manage_agentcore_memory`, `manage_agentcore_gateway` | Managing AgentCore runtimes, searching AgentCore documentation, deploying MCP gateways |

## Activation Procedure

### Step 1: Determine which server(s) are needed

Match the user's task to the table above. Multiple servers can be enabled simultaneously if the task spans concerns (e.g., CDK validation + AWS API calls).

### Step 2: Enable the server(s)

Edit `.mcp.json` in the project root. Change `"disabled": true` to `"disabled": false` for each needed server. Example for enabling `aws-api`:

```json
"aws-api": {
  ...
  "disabled": false
}
```

**IMPORTANT**: Only change the `disabled` field. Do not modify `command`, `args`, or `env`.

### Step 3: Wait for connection

After editing `.mcp.json`, Claude Code will automatically detect the change and start the MCP server(s). The new tools will become available within a few seconds. Inform the user which server(s) were enabled.

### Step 4: Use the tools

Once connected, use the MCP tools directly:
- **aws-api**: `mcp__aws-api__call_aws`, `mcp__aws-api__suggest_aws_commands`
- **aws-iac**: `mcp__aws-iac__validate_cloudformation_template`, `mcp__aws-iac__search_cdk_documentation`, etc.
- **bedrock-agentcore**: `mcp__bedrock-agentcore__search_agentcore_docs`, `mcp__bedrock-agentcore__manage_agentcore_runtime`, etc.

### Step 5: Deactivation (after task completion)

When the AWS task is complete, edit `.mcp.json` to set `"disabled": true` for all servers that were enabled. This frees the context window for subsequent work.

## Auto-Selection Guide

If the user doesn't specify which server, use these heuristics:

| User intent | Server(s) to enable |
|-------------|-------------------|
| "deploy", "check stack", "run aws command" | `aws-api` |
| "validate template", "CDK best practices", "troubleshoot deployment" | `aws-iac` |
| "agentcore runtime", "agentcore docs", "manage agent" | `bedrock-agentcore` |
| "deploy and validate" | `aws-api` + `aws-iac` |
| "full infrastructure work" | all three |
| CDK synth/deploy for this project | `aws-api` + `aws-iac` |
| AgentCore runtime management for this project | `aws-api` + `bedrock-agentcore` |

## Configuration Reference

The `.mcp.json` file is at the project root. All servers use:
- **Runtime**: `uvx` at `/home/ec2-user/.local/bin/uvx`
- **Region**: `ap-southeast-2` (for aws-api and aws-iac)
- **Log level**: `ERROR` (minimal noise)
- **AWS credentials**: Inherited from environment/profile

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Server fails to start | Run `uvx awslabs.<package>@latest --help` in Bash to check |
| Tools not appearing | Verify `.mcp.json` edit saved correctly, check `disabled` is `false` |
| Permission denied | Ensure AWS credentials are configured (`aws sts get-caller-identity`) |
| Slow startup | Packages are pre-cached; first run after cache expiry may download |
