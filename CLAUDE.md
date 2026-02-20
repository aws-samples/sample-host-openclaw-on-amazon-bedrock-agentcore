# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OpenClaw on AgentCore Runtime — a multi-channel AI messaging bot (Telegram, Discord, Slack) running as a serverless container on AWS Bedrock AgentCore Runtime. The container runs OpenClaw (Node.js), a Bedrock proxy adapter, and an AgentCore contract server. A keepalive Lambda prevents idle session termination.

## Tech Stack

- **Infrastructure**: CDK v2 (Python), 6 stacks
- **Runtime**: Bedrock AgentCore Runtime (serverless ARM64 container, VPC mode)
- **Messaging**: OpenClaw (Node.js) — Telegram, Discord, Slack channel providers
- **Tools & Skills**: Built-in tool groups (full profile) + 9 ClawHub skills (web search, research, memory, etc.)
- **AI Model**: Claude Sonnet 4.6 via Bedrock ConverseStream (`us.anthropic.claude-sonnet-4-6`)
- **Identity**: Cognito User Pool (HMAC-derived passwords, auto-provisioned users)
- **Memory**: AgentCore Memory (semantic, user preferences, summary strategies)
- **Observability**: CloudWatch dashboards + alarms, Bedrock invocation logging
- **Token Monitoring**: Lambda + DynamoDB (single-table) + CloudWatch custom metrics
- **Security**: VPC endpoints, KMS CMK, Secrets Manager, cdk-nag

## Architecture

```
     Telegram / Discord / Slack
                |
    +-----------v-----------+
    |   AgentCore Runtime   |  <-- Serverless container (ARM64, VPC mode)
    |                       |
    |  agentcore-contract.js (port 8080) -- /ping, /invocations
    |  OpenClaw Gateway     (port 18789) -- channel providers
    |  agentcore-proxy.js   (port 18790) -- OpenAI -> Bedrock
    +-----------+-----------+
                |
    +-----------v-----------+      +-----------------------+
    |   Amazon Bedrock      |      | EventBridge (5 min)   |
    |   ConverseStream API  |      | + Keepalive Lambda    |
    |   Claude Sonnet 4.6   |      +-----------------------+
    +-----------------------+

    Supporting: VPC, KMS, Secrets Manager, Cognito, AgentCore Memory,
               CloudWatch, DynamoDB, CloudTrail
```

## Project Structure

```
openclaw-on-agentcore/
  app.py                          # CDK app entry point (6 stacks)
  cdk.json                        # Configuration (model, budgets, region)
  requirements.txt                # Python deps (aws-cdk-lib, cdk-nag)
  stacks/
    __init__.py                   # Shared helper (RetentionDays converter)
    vpc_stack.py                  # VPC, subnets, NAT, 7 VPC endpoints, flow logs
    security_stack.py             # KMS CMK, Secrets Manager, Cognito, CloudTrail
    agentcore_stack.py            # Runtime, Memory, WorkloadIdentity, ECR, IAM
    keepalive_stack.py            # Lambda + EventBridge (5-min keepalive)
    observability_stack.py        # Dashboards, alarms, Bedrock logging
    token_monitoring_stack.py     # Lambda processor, DynamoDB, token analytics
  bridge/
    Dockerfile                    # Container image (node:22-slim, ARM64, clawhub skills)
    entrypoint.sh                 # Startup orchestration (5 steps)
    agentcore-contract.js         # AgentCore HTTP contract (/ping, /invocations)
    agentcore-proxy.js            # OpenAI -> Bedrock ConverseStream adapter
    force-ipv4.js                 # DNS patch for Node.js 22 IPv6 issue
  lambda/
    token_metrics/index.py        # Bedrock log -> DynamoDB + CloudWatch metrics
    keepalive/index.py            # Runtime keepalive invoker
  docs/
    architecture.md               # Detailed architecture diagrams
```

## CDK Stacks (6 stacks)

| Stack | Key Resources | Dependencies |
|---|---|---|
| **OpenClawVpc** | VPC (2 AZ), subnets, NAT, 7 VPC endpoints, flow logs | None |
| **OpenClawSecurity** | KMS CMK, Secrets Manager (5 secrets), Cognito User Pool, CloudTrail | None |
| **OpenClawAgentCore** | CfnRuntime, CfnRuntimeEndpoint, CfnMemory, CfnWorkloadIdentity, ECR, SG, IAM | Vpc, Security |
| **OpenClawKeepalive** | Lambda, EventBridge rule (every 5 min) | AgentCore |
| **OpenClawObservability** | Operations dashboard, alarms, SNS, Bedrock invocation logging | None |
| **OpenClawTokenMonitoring** | DynamoDB (single-table, 4 GSIs), Lambda processor, analytics dashboard | Observability |

## Expected Commands

### CDK
```bash
source .venv/bin/activate
cdk synth                                    # synthesize + cdk-nag checks
cdk deploy --all --require-approval never    # deploy all stacks
cdk deploy OpenClawAgentCore                 # deploy single stack
cdk diff                                     # preview changes
cdk destroy --all                            # tear down
```

### Build & Push Bridge Image
```bash
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=us-west-2

docker build --platform linux/arm64 -t openclaw-bridge bridge/
aws ecr get-login-password --region $CDK_DEFAULT_REGION | \
  docker login --username AWS --password-stdin \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com
docker tag openclaw-bridge:latest \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:latest
docker push \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:latest
```

### Channel Setup
```bash
# Store Telegram bot token
aws secretsmanager update-secret \
  --secret-id openclaw/channels/telegram \
  --secret-string 'BOT_TOKEN' \
  --region $CDK_DEFAULT_REGION

# Store Discord bot token
aws secretsmanager update-secret \
  --secret-id openclaw/channels/discord \
  --secret-string 'BOT_TOKEN' \
  --region $CDK_DEFAULT_REGION

# Store Slack tokens (JSON — both botToken and appToken required for Socket Mode)
aws secretsmanager update-secret \
  --secret-id openclaw/channels/slack \
  --secret-string '{"botToken":"xoxb-YOUR-BOT-TOKEN","appToken":"xapp-YOUR-APP-TOKEN"}' \
  --region $CDK_DEFAULT_REGION
```

### Runtime Operations
```bash
# Get runtime ID
RUNTIME_ID=$(aws cloudformation describe-stacks \
  --stack-name OpenClawAgentCore \
  --query "Stacks[0].Outputs[?OutputKey=='RuntimeId'].OutputValue" \
  --output text --region $CDK_DEFAULT_REGION)

# Check runtime status
aws bedrock-agentcore get-runtime \
  --agent-runtime-id $RUNTIME_ID \
  --region $CDK_DEFAULT_REGION

# Invoke runtime (status check)
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-id $RUNTIME_ID \
  --qualifier openclaw_agent_live \
  --session-id openclaw-telegram-session-primary-keepalive-001 \
  --payload '{"action":"status"}' \
  --region $CDK_DEFAULT_REGION \
  /tmp/status.json
```

## Key Configuration (cdk.json)

| Parameter | Default | Description |
|---|---|---|
| `account` | (empty) | AWS account ID. Falls back to `CDK_DEFAULT_ACCOUNT` |
| `region` | `us-west-2` | AWS region. Falls back to `CDK_DEFAULT_REGION` |
| `default_model_id` | `us.anthropic.claude-sonnet-4-6` | Bedrock model ID |
| `cloudwatch_log_retention_days` | `30` | Log retention |
| `daily_token_budget` | `1000000` | Token budget alarm threshold |
| `daily_cost_budget_usd` | `5` | Cost budget alarm threshold |
| `token_ttl_days` | `90` | DynamoDB TTL |

## Container Startup Sequence (entrypoint.sh)

1. **agentcore-contract.js** (port 8080) — MUST start first for health check
2. **Fetch secrets** — gateway token, Cognito secret, channel bot tokens
3. **agentcore-proxy.js** (port 18790) — OpenAI-to-Bedrock adapter
4. **Write OpenClaw config** — `openclaw.json` with enabled channels, tools (full profile), and skills
5. **OpenClaw gateway** (port 18789) — foreground process

## Gotchas

### AgentCore Runtime
- **ARM64 required**: Build with `--platform linux/arm64`
- **Image must exist before deploy**: Push to ECR before `cdk deploy`
- **Resource names**: Must match `^[a-zA-Z][a-zA-Z0-9_]{0,47}$` — underscores, not hyphens
- **Health check timing**: Contract server on port 8080 must start within seconds or health check times out
- **Keepalive**: Lambda invokes runtime every 5 min; contract server returns `HealthyBusy` to prevent idle termination
- **Memory event expiry**: `event_expiry_duration` is in days (max 365), not seconds
- **VPC endpoints**: `bedrock-agentcore-runtime` endpoint not available in all regions — omit if unsupported

### IAM / Bedrock
- **Cross-region inference**: Model `us.anthropic.claude-sonnet-4-6` routes to any US region — IAM uses `arn:aws:bedrock:*::foundation-model/*`
- **Inference profile ARN**: Separate from foundation model — `arn:aws:bedrock:{region}:{account}:inference-profile/*`
- **Memory execution role**: Must trust both `bedrock.amazonaws.com` and `bedrock-agentcore.amazonaws.com`

### Node.js 22 + VPC
- **IPv6 issue**: Node.js 22 Happy Eyeballs fails in VPCs without IPv6 — `force-ipv4.js` patches `dns.lookup()` to force IPv4
- **NODE_OPTIONS**: `--dns-result-order=ipv4first --no-network-family-autoselection -r /app/force-ipv4.js`

### CDK
- `logs.RetentionDays` is an enum — use helper in `stacks/__init__.py`
- Cross-stack cyclic deps: use string ARN params + `add_to_policy()` instead of `grant_*()`
- Empty `cdk.json` account: falls back to `CDK_DEFAULT_ACCOUNT` env var via `app.py`

### OpenClaw
- Startup takes ~4 minutes (plugin registration, channel connection)
- Correct start command: `openclaw gateway run --port 18789 --bind lan --verbose`
- Telegram `dmPolicy: "open"` requires `allowFrom: ["*"]`
- Channel token validation: `entrypoint.sh` skips channels with placeholder/short tokens
- **Slack Socket Mode requires two tokens**: The `openclaw/channels/slack` secret must be JSON `{"botToken":"xoxb-...","appToken":"xapp-..."}`. The entrypoint parses JSON secrets and passes both tokens to OpenClaw. Plain string secrets (backward compatible) only pass `botToken` and will fail to connect via Socket Mode
- **`skills.allowBundled`**: Must be an array (e.g., `["*"]`), not a boolean — OpenClaw schema validation rejects `true`
- **ClawHub skill paths**: `clawhub install` installs to `/skills/<name>`, not `~/.openclaw/skills` — use `/skills` as `extraDirs`
- **ClawHub VirusTotal flags**: Some skills (jina-reader, etc.) are flagged for external API calls — use `--force` in non-interactive mode
- **Image updates require session restart**: Pushing a new ECR image and redeploying via CDK updates the runtime config, but the existing keepalive session continues running the old image. Stop it with `stop-runtime-session` or bump `IMAGE_VERSION` env var in the stack and redeploy

### Cognito Identity
- Self-signup disabled — users auto-provisioned by proxy via `AdminCreateUser`
- Passwords: `HMAC-SHA256(secret, actorId).slice(0, 32)` — deterministic, never stored
- Usernames are channel-prefixed: `telegram:6087229962`
- JWT tokens cached per user with 60s early refresh
