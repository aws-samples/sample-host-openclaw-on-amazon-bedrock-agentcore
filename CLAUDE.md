# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OpenClaw on AgentCore Runtime — a multi-channel AI messaging bot (Telegram, Slack) running as per-user serverless containers on AWS Bedrock AgentCore Runtime. Each user gets their own microVM with workspace persistence. A Router Lambda handles webhook ingestion from Telegram and Slack (text and images), resolves user identity via DynamoDB, and invokes per-user AgentCore sessions. Image uploads are stored in S3 and passed to Bedrock as multimodal content.

## Tech Stack

- **Infrastructure**: CDK v2 (Python), 7 stacks
- **Runtime**: Bedrock AgentCore Runtime (serverless ARM64 container, VPC mode, per-user sessions)
- **Channel Ingestion**: Router Lambda behind API Gateway HTTP API (Telegram webhook, Slack Events API, image uploads)
- **Multimodal**: Image upload support — photos downloaded by Router Lambda, stored in S3, fetched by proxy, sent to Bedrock as multimodal content
- **Messaging**: OpenClaw (Node.js) — headless mode, messages bridged via WebSocket
- **Tools & Skills**: Built-in tool groups (full profile) + 9 ClawHub skills + 2 custom skills (S3 user files, EventBridge cron)
- **Scheduling**: EventBridge Scheduler for recurring tasks — cron executor Lambda warms sessions and delivers responses to channels
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

  +------------------------------------------+
  | S3 Image Uploads                         |
  | {namespace}/_uploads/img_*.{jpeg,png,...} |
  | Router Lambda uploads, proxy fetches     |
  | for Bedrock multimodal ConverseStream    |
  +------------------------------------------+

  +------------------------------------------------------+
  | EventBridge Scheduler (Cron Jobs)                    |
  |                                                      |
  | openclaw-cron schedule group                         |
  |   -> Cron Lambda (openclaw-cron-executor)            |
  |     1. Warm up user's AgentCore session              |
  |     2. Send cron message via AgentCore               |
  |     3. Deliver response to Telegram/Slack            |
  +------------------------------------------------------+

  Supporting: VPC, KMS, Secrets Manager, Cognito,
             CloudWatch, DynamoDB, CloudTrail
```

## Project Structure

```
openclaw-on-agentcore/
  app.py                          # CDK app entry point (7 stacks)
  cdk.json                        # Configuration (model, budgets, sessions, cron)
  requirements.txt                # Python deps (aws-cdk-lib, cdk-nag)
  stacks/
    __init__.py                   # Shared helper (RetentionDays converter)
    vpc_stack.py                  # VPC, subnets, NAT, 7 VPC endpoints, flow logs
    security_stack.py             # KMS CMK, Secrets Manager, Cognito, CloudTrail
    agentcore_stack.py            # Runtime, WorkloadIdentity, ECR, S3, IAM
    router_stack.py               # Router Lambda + API Gateway HTTP API + DynamoDB identity
    observability_stack.py        # Dashboards, alarms, Bedrock logging
    token_monitoring_stack.py     # Lambda processor, DynamoDB, token analytics
    cron_stack.py                 # EventBridge Scheduler, Cron executor Lambda, IAM
  bridge/
    Dockerfile                    # Container image (node:22-slim, ARM64, clawhub skills)
    entrypoint.sh                 # Startup: configure IPv4, start contract server
    agentcore-contract.js         # AgentCore HTTP contract with lazy init + WebSocket bridge
    agentcore-proxy.js            # OpenAI -> Bedrock ConverseStream adapter + Identity + multimodal images
    image-support.test.js         # Image support unit tests (node:test)
    workspace-sync.js             # .openclaw/ directory S3 sync (restore/save/periodic)
    force-ipv4.js                 # DNS patch for Node.js 22 IPv6 issue
    skills/
      s3-user-files/              # Custom per-user file storage skill (S3-backed)
        SKILL.md                  # OpenClaw skill manifest
        common.js                 # Shared utilities (sanitize, buildKey, validation)
        read.js / write.js        # Read/write files in user's S3 namespace
        list.js / delete.js       # List/delete files in user's S3 namespace
      eventbridge-cron/           # Cron scheduling skill (EventBridge Scheduler)
        SKILL.md                  # OpenClaw skill manifest
        common.js                 # Shared utilities (schedule group, DynamoDB helpers)
        create.js / update.js     # Create/update EventBridge schedules
        list.js / delete.js       # List/delete schedules
  lambda/
    token_metrics/index.py        # Bedrock log -> DynamoDB + CloudWatch metrics
    router/index.py               # Webhook router (Telegram + Slack, image uploads)
    router/test_image_upload.py   # Image upload unit tests (pytest)
    cron/index.py                 # Cron executor (warmup, invoke, deliver to channel)
  tests/
    e2e/                          # E2E tests (simulated Telegram webhooks + CloudWatch logs)
  docs/
    architecture.md               # Detailed architecture diagrams
```

## CDK Stacks (7 stacks)

| Stack | Key Resources | Dependencies |
|---|---|---|
| **OpenClawVpc** | VPC (2 AZ), subnets, NAT, 7 VPC endpoints, flow logs | None |
| **OpenClawSecurity** | KMS CMK, Secrets Manager (7 secrets incl. webhook validation), Cognito User Pool, CloudTrail | None |
| **OpenClawAgentCore** | CfnRuntime, CfnRuntimeEndpoint, CfnWorkloadIdentity, ECR, S3 bucket, SG, IAM | Vpc, Security |
| **OpenClawRouter** | Lambda, API Gateway HTTP API (explicit routes, throttling), DynamoDB identity table | AgentCore, Security |
| **OpenClawObservability** | Operations dashboard, alarms, SNS, Bedrock invocation logging | None |
| **OpenClawTokenMonitoring** | DynamoDB (single-table, 4 GSIs), Lambda processor, analytics dashboard | Observability |
| **OpenClawCron** | EventBridge Scheduler group, Cron executor Lambda, Scheduler IAM role | AgentCore, Router, Security |

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
cd bridge && node --test image-support.test.js         # image upload + multimodal tests
cd bridge/skills/s3-user-files && AWS_REGION=$CDK_DEFAULT_REGION node --test common.test.js  # S3 skill tests
```

### Router Lambda Tests
```bash
cd lambda/router && python -m pytest test_image_upload.py -v   # image upload unit tests
```

### E2E Tests
```bash
cd tests/e2e && python -m pytest bot_test.py -v   # simulated Telegram webhook tests (requires deployed stack)
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
| `cron_lambda_timeout_seconds` | `600` | Cron executor Lambda timeout (must exceed warmup time) |
| `cron_lambda_memory_mb` | `256` | Cron executor Lambda memory |
| `cron_lead_time_minutes` | `5` | Minutes before schedule time to start warmup |

## Container Startup Sequence

1. **entrypoint.sh**: Configure Node.js IPv4 DNS patch, start contract server
2. **agentcore-contract.js** (port 8080): Responds to `/ping` with `Healthy` immediately
3. **On first `/invocations` with `action: chat` or `action: warmup`** (lazy init):
   - Fetch secrets from Secrets Manager (gateway token, Cognito secret)
   - Restore `.openclaw/` from S3 via `workspace-sync.js`
   - Start `agentcore-proxy.js` (port 18790) with `USER_ID`/`CHANNEL` env vars
   - Write headless OpenClaw config (no channels)
   - Start OpenClaw gateway (port 18789) — ~4 min startup
   - Start periodic workspace saves (every 5 min)
4. **`action: warmup`**: Triggers lazy init only; returns `{ready: true}` when OpenClaw is ready (used by cron Lambda to pre-warm sessions)
5. **`action: cron`**: Sends a cron message via the WebSocket bridge (same as chat but intended for scheduled tasks)
6. **`action: status`**: Returns current init state (`{openclawReady, proxyReady, uptime}`) without triggering init
7. **Subsequent `/invocations` with `action: chat`**: Bridge message via WebSocket to OpenClaw
8. **SIGTERM**: Save `.openclaw/` to S3, kill child processes, exit

## DynamoDB Identity Table Schema

**Table: `openclaw-identity`** (PAY_PER_REQUEST, TTL on `ttl` attribute)

| PK | SK | Purpose |
|---|---|---|
| `CHANNEL#telegram:6087229962` | `PROFILE` | Channel→user lookup |
| `USER#user_abc123` | `PROFILE` | User profile |
| `USER#user_abc123` | `CHANNEL#telegram:6087229962` | User's bound channels |
| `USER#user_abc123` | `SESSION` | Current session |
| `BIND#ABC123` | `BIND` | Cross-channel bind code (10 min TTL) |
| `USER#user_abc123` | `CRON#schedule-name` | User's cron schedule metadata (expression, message, timezone, channel) |

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
- **OpenClaw 2026.2.23 breaking change**: Non-loopback `controlUi` requires `dangerouslyAllowHostHeaderOriginFallback: true` or explicit `allowedOrigins`. Without this, `openclaw gateway run --bind lan` fails with `Error: non-loopback Control UI requires gateway.controlUi.allowedOrigins`

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
- **Image uploads**: Telegram photos and Slack file attachments (JPEG, PNG, GIF, WebP, max 3.75 MB) are downloaded by the Router Lambda, uploaded to S3 under `{namespace}/_uploads/`, and passed to AgentCore as a structured message `{text, images[{s3Key, contentType}]}`
- **Telegram captions**: `message.get("text", "") or message.get("caption", "")` — photos use `caption`, not `text`

### Image Upload Flow
- **Router Lambda** downloads image from channel API (Telegram `getFile` / Slack `url_private_download`), uploads to S3 `{namespace}/_uploads/img_{ts}_{hex}.{ext}`
- **Contract server** converts structured message to bridge text with `[OPENCLAW_IMAGES:[...]]` marker appended
- **Proxy** extracts marker via regex, fetches image bytes from S3 (with namespace validation to prevent cross-user reads), builds Bedrock multimodal content blocks (`{image: {format, source: {bytes}}}`)
- **Supported types**: `image/jpeg`, `image/png`, `image/gif`, `image/webp` (max 3.75 MB per Bedrock limit)
- **Security**: S3 key validated against user's namespace prefix + path traversal (`..`) rejection. Format validated against `VALID_BEDROCK_FORMATS` set
- **Slack prerequisite**: Bot needs `files:read` OAuth scope to download image files

### Workspace Sync
- **S3 prefix**: `{namespace}/.openclaw/` where `namespace = actorId.replace(/:/g, "_")`
- **Periodic saves**: Every 5 min (configurable via `WORKSPACE_SYNC_INTERVAL_MS`)
- **SIGTERM grace**: 10s max for final save before exit (AgentCore gives 15s total)
- **Skip patterns**: `node_modules/`, `.cache/`, `*.log`, files > 10MB
- **Same S3 bucket**: Uses `S3_USER_FILES_BUCKET` (shared with s3-user-files skill)

### EventBridge Cron Scheduling
- **Schedule group**: All schedules created under `openclaw-cron` group in EventBridge Scheduler
- **Schedule naming**: `openclaw-{namespace}-{shortId}` (e.g., `openclaw-telegram_6087229962-87a86927`)
- **DynamoDB storage**: Schedule metadata stored as `CRON#` SK under the user's PK in the identity table
- **Cron executor Lambda**: Warms up the user's AgentCore session (sends `action: warmup`), then sends the cron message (sends `action: cron`), then delivers the response to the user's chat channel
- **Lead time**: Cron Lambda invoked with `cron_lead_time_minutes` (default 5 min) to allow session warmup before the scheduled time
- **Environment variables**: Container receives `EVENTBRIDGE_SCHEDULE_GROUP`, `CRON_LAMBDA_ARN`, `EVENTBRIDGE_ROLE_ARN`, `IDENTITY_TABLE_NAME`, `CRON_LEAD_TIME_MINUTES` for the eventbridge-cron skill

### Per-User Identity Resolution
- **Priority order**: (0) `USER_ID` env var (set by contract server) → (1) `x-openclaw-actor-id` header → (2) OpenAI `user` field → (3) message envelope parsing → (4) message `name` field → (5) fallback `default-user`
- **Per-user sessions**: Contract server sets `USER_ID` env var when starting proxy, so identity is always resolved from environment in per-user mode
- **S3-backed isolation**: User files in `s3://openclaw-user-files-{account}-{region}/{namespace}/`
- **Namespace immutability**: System-determined from channel identity, cannot be changed by user request
