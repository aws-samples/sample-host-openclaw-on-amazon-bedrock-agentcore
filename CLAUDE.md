# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OpenClaw on AgentCore Runtime — a multi-channel AI messaging bot (Telegram, Slack) running as per-user serverless containers on AWS Bedrock AgentCore Runtime. Each user gets their own microVM with workspace persistence. A Router Lambda handles webhook ingestion from Telegram and Slack, resolves user identity via DynamoDB, and invokes per-user AgentCore sessions.

## Tech Stack

- **Infrastructure**: CDK v2 (Python), 6 stacks
- **Runtime**: Bedrock AgentCore Runtime (serverless ARM64 container, VPC mode, per-user sessions)
- **Channel Ingestion**: Router Lambda behind API Gateway HTTP API (Telegram webhook, Slack Events API)
- **Messaging**: OpenClaw (Node.js) — headless mode, messages bridged via WebSocket
- **Tools & Skills**: Built-in tool groups (full profile) + 9 ClawHub skills + 1 custom S3 user files skill
- **Per-User File Storage**: S3-backed per-user file isolation via custom `s3-user-files` skill
- **Workspace Persistence**: .openclaw/ directory synced to/from S3 per user
- **AI Model**: Claude Opus 4.6 via Bedrock ConverseStream (configurable via `default_model_id` in `cdk.json`, default `global.anthropic.claude-opus-4-6-v1`)
- **Identity**: DynamoDB identity table (channel→user mapping, cross-channel binding) + Cognito User Pool
- **Observability**: CloudWatch dashboards + alarms, Bedrock invocation logging
- **Token Monitoring**: Lambda + DynamoDB (single-table) + CloudWatch custom metrics
- **Security**: VPC endpoints, KMS CMK, Secrets Manager, cdk-nag

## Architecture

```
  Telegram webhook / Slack Events API
              |
  +-----------v-----------+
  |   Router Lambda       |  <-- API Gateway HTTP API, async self-invoke
  |   - User resolution   |      DynamoDB identity table
  |   - Session mgmt      |      Cross-channel binding
  |   - Channel dispatch   |
  +-----------+-----------+
              |
  +-----------v-----------+
  | InvokeAgentRuntime    |  <-- Per-user session (runtimeSessionId)
  | (session per user)    |
  +-----------+-----------+
              |
  +-----------v-----------+
  | AgentCore Runtime     |  <-- Per-user microVM (ARM64, VPC mode)
  |                       |
  | agentcore-contract.js (8080) -- /ping (Healthy), /invocations
  |   -> lazy init:
  |     1. Restore .openclaw/ from S3
  |     2. Start proxy (18790) with USER_ID env
  |     3. Start OpenClaw headless (18789)
  |     4. Bridge messages via WebSocket
  |   -> SIGTERM: save .openclaw/ to S3
  |                       |
  | agentcore-proxy.js    (18790) -- OpenAI -> Bedrock ConverseStream
  | OpenClaw Gateway      (18789) -- headless, no channels
  +-----------+-----------+
              |
  +-----------v-----------+
  |   Amazon Bedrock      |
  |   ConverseStream API  |
  |   Claude Opus 4.6   |
  +-----------------------+

  +-----------------------+        +------------------------+
  | S3 User Files         |        | S3 Workspace Sync      |
  | {namespace}/file.md   |        | {namespace}/.openclaw/  |
  | Via s3-user-files      |        | Restored on init,      |
  | skill                 |        | saved periodically     |
  +-----------------------+        +------------------------+

  Supporting: VPC, KMS, Secrets Manager, Cognito,
             CloudWatch, DynamoDB, CloudTrail
```

## Project Structure

```
openclaw-on-agentcore/
  app.py                          # CDK app entry point (6 stacks)
  cdk.json                        # Configuration (model, budgets, sessions)
  requirements.txt                # Python deps (aws-cdk-lib, cdk-nag)
  stacks/
    __init__.py                   # Shared helper (RetentionDays converter)
    vpc_stack.py                  # VPC, subnets, NAT, 7 VPC endpoints, flow logs
    security_stack.py             # KMS CMK, Secrets Manager, Cognito, CloudTrail
    agentcore_stack.py            # Runtime, WorkloadIdentity, ECR, S3, IAM
    router_stack.py               # Router Lambda + API Gateway HTTP API + DynamoDB identity
    observability_stack.py        # Dashboards, alarms, Bedrock logging
    token_monitoring_stack.py     # Lambda processor, DynamoDB, token analytics
  bridge/
    Dockerfile                    # Container image (node:22-slim, ARM64, clawhub skills)
    entrypoint.sh                 # Startup: configure IPv4, start contract server
    agentcore-contract.js         # AgentCore HTTP contract with lazy init + WebSocket bridge
    agentcore-proxy.js            # OpenAI -> Bedrock ConverseStream adapter + Identity
    workspace-sync.js             # .openclaw/ directory S3 sync (restore/save/periodic)
    force-ipv4.js                 # DNS patch for Node.js 22 IPv6 issue
    skills/
      s3-user-files/              # Custom per-user file storage skill (S3-backed)
        SKILL.md                  # OpenClaw skill manifest
        common.js                 # Shared utilities (sanitize, buildKey, validation)
        read.js / write.js        # Read/write files in user's S3 namespace
        list.js / delete.js       # List/delete files in user's S3 namespace
  lambda/
    token_metrics/index.py        # Bedrock log -> DynamoDB + CloudWatch metrics
    router/index.py               # Webhook router (Telegram + Slack)
  docs/
    architecture.md               # Detailed architecture diagrams
```

## CDK Stacks (6 stacks)

| Stack | Key Resources | Dependencies |
|---|---|---|
| **OpenClawVpc** | VPC (2 AZ), subnets, NAT, 7 VPC endpoints, flow logs | None |
| **OpenClawSecurity** | KMS CMK, Secrets Manager (7 secrets incl. webhook validation), Cognito User Pool, CloudTrail | None |
| **OpenClawAgentCore** | CfnRuntime, CfnRuntimeEndpoint, CfnWorkloadIdentity, ECR, S3 bucket, SG, IAM | Vpc, Security |
| **OpenClawRouter** | Lambda, API Gateway HTTP API (explicit routes, throttling), DynamoDB identity table | AgentCore, Security |
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

### Build & Push Bridge Image (after CDK deploy creates ECR repo)
```bash
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=us-west-2  # change to your preferred region

aws ecr get-login-password --region $CDK_DEFAULT_REGION | \
  docker login --username AWS --password-stdin \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com
docker build --platform linux/arm64 -t openclaw-bridge bridge/
docker tag openclaw-bridge:latest \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:latest
docker push \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:latest
```

### Webhook Setup
```bash
# Get Router API Gateway URL
API_URL=$(aws cloudformation describe-stacks \
  --stack-name OpenClawRouter \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text --region $CDK_DEFAULT_REGION)

# Get webhook secret (for Telegram request validation)
WEBHOOK_SECRET=$(aws secretsmanager get-secret-value \
  --secret-id openclaw/webhook-secret \
  --region $CDK_DEFAULT_REGION --query SecretString --output text)

# Set up Telegram webhook with secret_token for validation
TELEGRAM_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id openclaw/channels/telegram \
  --region $CDK_DEFAULT_REGION --query SecretString --output text)
curl "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook?url=${API_URL}webhook/telegram&secret_token=${WEBHOOK_SECRET}"
```

### Channel Setup
```bash
# Store Telegram bot token
aws secretsmanager update-secret \
  --secret-id openclaw/channels/telegram \
  --secret-string 'BOT_TOKEN' \
  --region $CDK_DEFAULT_REGION

# Store Slack credentials (JSON: bot token + signing secret for HMAC validation)
aws secretsmanager update-secret \
  --secret-id openclaw/channels/slack \
  --secret-string '{"botToken":"xoxb-YOUR-BOT-TOKEN","signingSecret":"YOUR-SIGNING-SECRET"}' \
  --region $CDK_DEFAULT_REGION
```

### Deploy New Bridge Version
```bash
# 1. Bump image_version in cdk.json (or use -c image_version=N on the CLI)
#    This forces AgentCore to pull the new container image.
# 2. Build + push image (see above)
# 3. CDK deploy
source .venv/bin/activate && cdk deploy OpenClawAgentCore --require-approval never
# 4. New sessions will use the new image automatically (per-user idle termination)
```

### Bridge Tests
```bash
cd bridge && node --test proxy-identity.test.js       # identity + workspace tests
cd bridge && node --test message-queue.test.js         # message queue serialization tests
cd bridge/skills/s3-user-files && AWS_REGION=$CDK_DEFAULT_REGION node --test common.test.js  # S3 skill tests
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

# Check DynamoDB identity table
aws dynamodb scan --table-name openclaw-identity --region $CDK_DEFAULT_REGION
```

## Key Configuration (cdk.json)

| Parameter | Default | Description |
|---|---|---|
| `account` | (empty) | AWS account ID. Falls back to `CDK_DEFAULT_ACCOUNT` |
| `region` | `us-west-2` | AWS region. Falls back to `CDK_DEFAULT_REGION` |
| `default_model_id` | `global.anthropic.claude-opus-4-6-v1` | Bedrock model ID. The `global.` prefix routes to any available region |
| `image_version` | `1` | Bridge container version tag. Bump to force container redeploy |
| `cloudwatch_log_retention_days` | `30` | Log retention |
| `daily_token_budget` | `1000000` | Token budget alarm threshold |
| `daily_cost_budget_usd` | `5` | Cost budget alarm threshold |
| `token_ttl_days` | `90` | DynamoDB TTL |
| `user_files_ttl_days` | `365` | S3 per-user file expiration |
| `session_idle_timeout` | `1800` | Per-user session idle timeout (seconds) |
| `session_max_lifetime` | `28800` | Per-user session max lifetime (seconds) |
| `workspace_sync_interval_seconds` | `300` | .openclaw/ S3 sync interval |
| `router_lambda_timeout_seconds` | `300` | Router Lambda timeout |
| `router_lambda_memory_mb` | `256` | Router Lambda memory |

## Container Startup Sequence

1. **entrypoint.sh**: Configure Node.js IPv4 DNS patch, start contract server
2. **agentcore-contract.js** (port 8080): Responds to `/ping` with `Healthy` immediately
3. **On first `/invocations` with `action: chat`** (lazy init):
   - Fetch secrets from Secrets Manager (gateway token, Cognito secret)
   - Restore `.openclaw/` from S3 via `workspace-sync.js`
   - Start `agentcore-proxy.js` (port 18790) with `USER_ID`/`CHANNEL` env vars
   - Write headless OpenClaw config (no channels)
   - Start OpenClaw gateway (port 18789) — ~4 min startup
   - Start periodic workspace saves (every 5 min)
4. **Subsequent `/invocations`**: Enqueue message for serialized processing via `enqueueMessage()` → `processQueue()` → `bridgeMessage()` (WebSocket to OpenClaw)
5. **SIGTERM**: Drain message queue, save `.openclaw/` to S3, kill child processes, exit

## DynamoDB Identity Table Schema

**Table: `openclaw-identity`** (PAY_PER_REQUEST, TTL on `ttl` attribute)

| PK | SK | Purpose |
|---|---|---|
| `CHANNEL#telegram:6087229962` | `PROFILE` | Channel→user lookup |
| `USER#user_abc123` | `PROFILE` | User profile |
| `USER#user_abc123` | `CHANNEL#telegram:6087229962` | User's bound channels |
| `USER#user_abc123` | `SESSION` | Current session |
| `BIND#ABC123` | `BIND` | Cross-channel bind code (10 min TTL) |

**Cross-channel binding**: User says "link accounts" on Telegram → gets 6-char code → enters code on Slack → both channels route to same user/session.

## Gotchas

### AgentCore Runtime
- **ARM64 required**: Build with `--platform linux/arm64`
- **Push image after CDK deploy**: CDK creates the ECR repo — do not manually create it (causes `Resource already exists` error). Push the image after `cdk deploy`. AgentCore pulls the image at session start, not deploy time
- **Resource names**: Must match `^[a-zA-Z][a-zA-Z0-9_]{0,47}$` — underscores, not hyphens
- **Health check timing**: Contract server on port 8080 must start within seconds
- **Per-user sessions**: Contract returns `Healthy` (not `HealthyBusy`) — allows natural idle termination
- **Session recreation**: InvokeAgentRuntime with terminated session creates new microVM; workspace restored on init
- **VPC endpoints**: `bedrock-agentcore-runtime` endpoint not available in all regions
- **Endpoint version drift**: `CfnRuntimeEndpoint` must set `agent_runtime_version=self.runtime.attr_agent_runtime_version`

### IAM / Bedrock
- **Cross-region inference**: Model `global.anthropic.claude-opus-4-6-v1` uses a global cross-region inference profile that routes to any available region — IAM uses `arn:aws:bedrock:*::foundation-model/*` and inference-profile wildcards
- **Inference profile ARN**: Separate from foundation model — `arn:aws:bedrock:{region}:{account}:inference-profile/*`

### Node.js 22 + VPC
- **IPv6 issue**: Node.js 22 Happy Eyeballs fails in VPCs without IPv6 — `force-ipv4.js` patches `dns.lookup()` to force IPv4
- **NODE_OPTIONS**: `--dns-result-order=ipv4first --no-network-family-autoselection -r /app/force-ipv4.js`

### CDK
- `logs.RetentionDays` is an enum — use helper in `stacks/__init__.py`
- Cross-stack cyclic deps: use string ARN params + `add_to_policy()` instead of `grant_*()`
- Empty `cdk.json` account: falls back to `CDK_DEFAULT_ACCOUNT` env var via `app.py`

### OpenClaw
- Startup takes ~4 minutes (plugin registration)
- Correct start command: `openclaw gateway run --port 18789 --bind lan --verbose`
- **`skills.allowBundled`**: Must be an array (e.g., `["*"]`), not a boolean
- **ClawHub skill paths**: `clawhub install` installs to `/skills/<name>` — use `/skills` as `extraDirs`
- **ClawHub VirusTotal flags**: Some skills flagged for external API calls — use `--force`
- **Image updates**: New sessions use new image automatically (no keepalive restart needed)
- **WebSocket bridge protocol**: Connect → auth (type:req, method:connect, protocol:3, auth:{token}) → agent.chat → streaming deltas → final
- **Message queue serialization**: Concurrent `/invocations` requests are serialized via `enqueueMessage()`/`processQueue()` in the contract server. Messages arriving while one is processing are batched into a single `bridgeMessage()` call with `[Message N/M]` format. First entry gets the full response; remaining entries get empty string (suppressed by the Router Lambda). Queue depth limit: 10, wait timeout: 90s
- **Batched response suppression**: Router Lambda skips `send_telegram_message`/`send_slack_message` when response is empty — signals the message was batched with another

### Cognito Identity
- Self-signup disabled — users auto-provisioned by proxy via `AdminCreateUser`
- Passwords: `HMAC-SHA256(secret, actorId).slice(0, 32)` — deterministic, never stored
- Usernames are channel-prefixed: `telegram:6087229962`
- JWT tokens cached per user with 60s early refresh

### Router Lambda
- **API Gateway HTTP API**: Only explicit routes exposed (`POST /webhook/telegram`, `POST /webhook/slack`, `GET /health`). Rate limiting: burst 50, sustained 100 req/s
- **Webhook validation**: Telegram uses `X-Telegram-Bot-Api-Secret-Token` header (set via `secret_token` on `setWebhook`). Slack uses `X-Slack-Signature` HMAC-SHA256 with 5-minute replay window
- **Async dispatch**: Self-invokes with `InvocationType=Event` for actual processing; returns 200 immediately to webhook
- **Slack**: Handles `url_verification` challenge synchronously; ignores retries via `x-slack-retry-num` header
- **Cold start latency**: First message to a new user triggers microVM creation + OpenClaw startup (~4 min)
- **Telegram typing indicator**: Sent while waiting for AgentCore response
- **Cross-channel binding**: "link accounts" generates 6-char code in DynamoDB with 10-min TTL

### Workspace Sync
- **S3 prefix**: `{namespace}/.openclaw/` where `namespace = actorId.replace(/:/g, "_")`
- **Periodic saves**: Every 5 min (configurable via `WORKSPACE_SYNC_INTERVAL_MS`)
- **SIGTERM grace**: 10s max for final save before exit (AgentCore gives 15s total)
- **Skip patterns**: `node_modules/`, `.cache/`, `*.log`, files > 10MB
- **Same S3 bucket**: Uses `S3_USER_FILES_BUCKET` (shared with s3-user-files skill)

### Per-User Identity Resolution
- **Priority order**: (0) `USER_ID` env var (set by contract server) → (1) `x-openclaw-actor-id` header → (2) OpenAI `user` field → (3) message envelope parsing → (4) message `name` field → (5) fallback `default-user`
- **Per-user sessions**: Contract server sets `USER_ID` env var when starting proxy, so identity is always resolved from environment in per-user mode
- **S3-backed isolation**: User files in `s3://openclaw-user-files-{account}-{region}/{namespace}/`
- **Namespace immutability**: System-determined from channel identity, cannot be changed by user request
