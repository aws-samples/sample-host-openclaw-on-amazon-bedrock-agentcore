# OpenClaw on AgentCore

A serverless multi-channel AI assistant running on AWS. Connects Telegram, Discord, Slack, and a web browser to Claude via [OpenClaw](https://github.com/nichochar/openclaw) on ECS Fargate, with optional [AWS Bedrock AgentCore](https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore.html) Runtime for managed agent execution and memory.

## What it does

- Routes messages from Telegram, Discord, Slack, and a web UI to Claude Sonnet 4.6 on Amazon Bedrock
- Automatically provisions per-user identities via Cognito (e.g. `telegram:6087229962`)
- Tracks token usage per user, per channel, per model in DynamoDB with CloudWatch dashboards
- Supports two AI paths: direct Bedrock ConverseStream (default) or AgentCore Runtime with semantic memory
- Deploys entirely via CDK with cdk-nag security checks

## Architecture

```
Users (Telegram / Discord / Slack / Web)
  |
CloudFront + WAF (TLS, rate limiting, token auth)
  |
Public ALB (restricted to CloudFront IPs)
  |
ECS Fargate (private subnet)
  +-- OpenClaw Gateway (port 18789) -- channels, WebSocket, Web UI
  +-- agentcore-proxy.js (port 18790) -- Bedrock translation, identity, streaming
        |
        +-- [bedrock-direct] --> Bedrock ConverseStream API --> Claude Sonnet 4.6
        +-- [agentcore]      --> AgentCore Runtime --> Strands Agent --> Bedrock + Memory
```

See [CLAUDE.md](CLAUDE.md) for the full architecture diagram and component details.

## Prerequisites

- AWS account with Bedrock model access enabled for Claude Sonnet 4.6
- [AWS CDK v2](https://docs.aws.amazon.com/cdk/v2/guide/getting-started.html) installed (`npm install -g aws-cdk`)
- Python 3.9+
- Docker
- AWS CLI configured with appropriate credentials

## Quick Start

### 1. Configure

Edit `cdk.json` and update these fields for your environment:

```json
{
  "context": {
    "account": "YOUR_AWS_ACCOUNT_ID",
    "region": "YOUR_REGION"
  }
}
```

Other tunable settings in `cdk.json`:

| Setting | Default | Description |
|---------|---------|-------------|
| `default_model_id` | `au.anthropic.claude-sonnet-4-6` | Bedrock model ID (cross-region inference profile) |
| `proxy_mode` | `bedrock-direct` | `bedrock-direct` or `agentcore` |
| `fargate_cpu` | `256` | Fargate task CPU units |
| `fargate_memory_mib` | `1024` | Fargate task memory (MiB) |
| `waf_rate_limit` | `100` | WAF rate limit (requests per 5 min per IP) |
| `daily_token_budget` | `1000000` | Token budget alarm threshold |
| `daily_cost_budget_usd` | `5` | Cost budget alarm threshold (USD) |
| `token_ttl_days` | `90` | DynamoDB token usage record TTL |

### 2. Deploy

```bash
# Create and activate a Python virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Full deployment (CDK stacks + Docker images + ECS restart)
./scripts/deploy.sh
```

The deploy script will:
1. Install CDK dependencies
2. Synthesize and validate CloudFormation templates (cdk-nag)
3. Deploy foundation stacks (VPC, Security, AgentCore, Fargate) -- creates ECR repos
4. Build and push Docker images (bridge + agent) to ECR
5. Force new ECS deployment
6. Deploy remaining stacks (Edge, Observability, Token Monitoring)
7. Print the Web UI URL and gateway token

Use `--skip-images` to skip Docker build/push if images haven't changed:

```bash
./scripts/deploy.sh --skip-images
```

### 3. Connect a channel

Store a bot token in Secrets Manager and redeploy:

```bash
# Telegram (get token from @BotFather)
aws secretsmanager update-secret \
  --secret-id openclaw/channels/telegram \
  --secret-string 'YOUR_BOT_TOKEN' \
  --region YOUR_REGION

# Force new deployment to pick up the token
aws ecs update-service --cluster CLUSTER_NAME --service SERVICE_NAME \
  --force-new-deployment --region YOUR_REGION
```

Discord and Slack follow the same pattern with `openclaw/channels/discord` and `openclaw/channels/slack`.

### 4. Access the Web UI

Open the CloudFront URL printed by the deploy script. The gateway token is passed as a query parameter:

```
https://DISTRIBUTION_ID.cloudfront.net?token=GATEWAY_TOKEN
```

## Project Structure

```
openclaw-on-agentcore/
  app.py                    # CDK app entry point
  cdk.json                  # CDK context config
  requirements.txt          # Python deps (aws-cdk-lib, cdk-nag)
  stacks/
    vpc_stack.py             # VPC, subnets, NAT, VPC endpoints, flow logs
    security_stack.py        # KMS, Secrets Manager, Cognito, CloudTrail
    agentcore_stack.py       # AgentCore Runtime, Endpoint, Memory, WorkloadIdentity
    fargate_stack.py         # ECS cluster, Fargate service, ALB
    edge_stack.py            # CloudFront, WAF, CF Function
    observability_stack.py   # Dashboards, alarms, invocation log group
    token_monitoring_stack.py # Lambda processor, DynamoDB, analytics dashboard
  agent/
    my_agent.py              # Strands Agent (Python) for AgentCore Runtime
    Dockerfile               # Agent container image
  bridge/
    agentcore-proxy.js       # OpenAI-to-Bedrock proxy + identity extraction + Cognito
    entrypoint.sh            # Container startup (fetch secrets, write config)
    force-ipv4.js            # DNS patch for Node.js 22 IPv6 issue in VPC
    Dockerfile               # Bridge container image (node:22-slim + OpenClaw)
  lambda/
    token_metrics/index.py   # Bedrock invocation log processor
  scripts/
    deploy.sh                # Full deployment script
    rotate-token.sh          # Gateway token rotation
    test-e2e.js              # WebSocket end-to-end streaming test
```

## CDK Stacks

7 stacks deployed in dependency order:

| Stack | Resources |
|-------|-----------|
| **OpenClawVpc** | VPC (2 AZ), NAT Gateway, 7 VPC endpoints, flow logs |
| **OpenClawSecurity** | KMS CMK, Secrets Manager, Cognito User Pool, CloudTrail |
| **OpenClawAgentCore** | AgentCore Runtime + Endpoint + Memory, WorkloadIdentity, IAM |
| **OpenClawFargate** | ECS cluster, Fargate service, ALB, ECR repo, task definition |
| **OpenClawEdge** | CloudFront distribution, WAF WebACL, CF Function (token auth) |
| **OpenClawObservability** | Operations dashboard, CloudWatch alarms, SNS topic |
| **OpenClawTokenMonitoring** | Lambda processor, DynamoDB (single-table), analytics dashboard |

## Proxy Modes

The proxy supports two AI paths, controlled by `proxy_mode` in `cdk.json`:

**`bedrock-direct` (default)** -- Calls Bedrock ConverseStream API directly. No memory persistence.

**`agentcore`** -- Routes through AgentCore Runtime, which runs a Strands Agent with semantic memory, user preferences, and conversation summaries.

Switch modes:
```bash
cdk deploy OpenClawFargate -c proxy_mode=agentcore    # enable AgentCore
cdk deploy OpenClawFargate -c proxy_mode=bedrock-direct  # rollback
```

## Per-User Identity

The proxy extracts per-user identity from the OpenClaw system prompt (which includes the channel's `chat_id` field). Each user gets:

- A unique Cognito identity (e.g. `telegram:6087229962`)
- HMAC-derived passwords (deterministic, never stored)
- Cached JWT tokens (auto-refreshed)
- Isolated token usage tracking in DynamoDB

## Observability

Two CloudWatch dashboards are created automatically:

- **OpenClaw-Operations** -- Fargate CPU/memory, ALB request count, error rates, Bedrock invocation latency
- **OpenClaw-Token-Analytics** -- Token usage per user/channel/model, estimated cost, daily trends

Budget alarms notify via SNS when daily token or cost thresholds are exceeded.

## Security

- All secrets stored in Secrets Manager with KMS CMK encryption
- VPC endpoints for ECR, Secrets Manager, Bedrock, CloudWatch, S3
- WAF rate limiting on CloudFront
- CloudFront Function validates gateway token by exact value
- ALB restricted to CloudFront origin-facing IPs via managed prefix list
- cdk-nag AwsSolutions checks enforced at synth time
- CloudTrail audit logging enabled

## Useful Commands

```bash
# Synthesize (validates cdk-nag)
source .venv/bin/activate && cdk synth

# Deploy a single stack
cdk deploy OpenClawFargate

# Preview changes
cdk diff

# Run end-to-end test
GATEWAY_TOKEN=$(aws secretsmanager get-secret-value --secret-id openclaw/gateway-token --query SecretString --output text)
node scripts/test-e2e.js

# Rotate gateway token
./scripts/rotate-token.sh

# Check auto-provisioned Cognito users
aws cognito-idp list-users --user-pool-id POOL_ID

# Tear down
cdk destroy --all
```

## Known Issues

- OpenClaw takes ~4 minutes from container start to gateway listening (plugin initialization phase)
- Node.js 22 in VPC without IPv6 requires the `force-ipv4.js` DNS patch (included and auto-loaded)
- `bedrock-agentcore-runtime` VPC endpoint is not available in `ap-southeast-2` -- AgentCore invocations traverse NAT Gateway
- WhatsApp requires interactive QR code auth and cannot be configured via a secret token

## License

See [LICENSE](LICENSE) for details.
