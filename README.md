# OpenClaw on AWS Bedrock AgentCore

[![License: MIT-0](https://img.shields.io/badge/License-MIT--0-blue.svg)](LICENSE)
[![Status: Experimental](https://img.shields.io/badge/Status-Experimental-orange.svg)]()
[![AWS CDK](https://img.shields.io/badge/AWS%20CDK-v2-yellow.svg)]()

> **Experimental** — This project is provided for experimentation and learning purposes only. It is **not intended for production use**. APIs, architecture, and configuration may change without notice.

Deploy an AI-powered multi-channel messaging bot (Telegram, Slack) on AWS Bedrock AgentCore Runtime using CDK.

## Table of Contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Project Structure](#project-structure)
- [Configuration](#configuration)
- [Channel Setup](#channel-setup)
- [How It Works](#how-it-works)
- [Operations](#operations)
- [Troubleshooting](#troubleshooting)
- [Known Limitations](#known-limitations)
- [Gotchas](#gotchas)
- [Cleanup](#cleanup)
- [Security](#security)
- [License](#license)

OpenClaw runs as **per-user serverless containers** on AgentCore Runtime. A Router Lambda handles webhook ingestion from Telegram and Slack, resolves user identity via DynamoDB, and invokes per-user AgentCore sessions. Each user gets their own microVM with workspace persistence (`.openclaw/` directory synced to S3). The agent has built-in tools (web, filesystem, runtime, sessions, automation), custom skills for file storage and cron scheduling, and **EventBridge-based cron scheduling** for recurring tasks.

Users can send **text and images** — photos sent via Telegram or Slack are downloaded by the Router Lambda, stored in S3, and passed to Claude as multimodal content via Bedrock's ConverseStream API. Supported formats: JPEG, PNG, GIF, WebP (max 3.75 MB).

## Architecture

```mermaid
flowchart LR
    subgraph Channels
        TG[Telegram]
        SL[Slack]
    end

    subgraph AWS[AWS Cloud]
        APIGW[API Gateway<br/>HTTP API]
        ROUTER[Router Lambda]
        DDB[(DynamoDB<br/>Identity + Access)]
        AGENT[AgentCore Runtime<br/>Per-User Container]
        BEDROCK[Amazon Bedrock<br/>Claude]
        CRON[EventBridge<br/>Scheduler]
        CRONLAMBDA[Cron Lambda]
    end

    TG & SL <-->|webhooks| APIGW
    APIGW <--> ROUTER
    ROUTER <-->|users, sessions| DDB
    ROUTER <--> AGENT
    AGENT <--> BEDROCK
    CRON --> CRONLAMBDA
    CRONLAMBDA <--> AGENT
    CRONLAMBDA -->|Bot API| TG & SL
```

**How it works:** Messages from Telegram/Slack hit the Router Lambda, which resolves user identity and routes to a per-user AgentCore container. Each user gets isolated compute, persistent workspace, and access to Claude via Bedrock.

See [docs/architecture-detailed.md](docs/architecture-detailed.md) for technical details (sequence diagrams, container internals, data flows).

### Why S3 Workspace Sync?

AgentCore microVMs are ephemeral — they're destroyed when idle. OpenClaw stores conversation history, user profiles, and agent configuration in the `.openclaw/` directory. **S3-backed workspace sync** restores this directory on session start, saves it periodically (every 5 min), and performs a final save on shutdown. Each user's workspace is isolated under a unique S3 prefix derived from their channel identity.

This lets the system behave like a persistent server (continuous conversation history) while benefiting from serverless economics (no idle compute costs).

### Security

This solution applies **defense-in-depth** across network, application, identity, and data layers. Key controls include:

- **Network isolation**: Private VPC subnets with VPC endpoints; no direct internet exposure for containers
- **Webhook authentication**: Cryptographic validation (Telegram secret token, Slack HMAC-SHA256 with replay protection)
- **Per-user isolation**: Each user runs in their own AgentCore microVM with dedicated S3 namespace
- **Encryption**: Data encrypted at rest (KMS) and in transit (TLS); secrets in Secrets Manager
- **Least-privilege IAM**: Tightly scoped permissions per component
- **Automated compliance**: cdk-nag AwsSolutions checks on every `cdk synth`

See [SECURITY.md](SECURITY.md) for the complete security architecture.

## Prerequisites

- **AWS Account** with Bedrock access
- **AWS CLI** v2 configured with credentials (`aws sts get-caller-identity` should succeed)
- **Node.js** >= 18 (for CDK CLI)
- **Python** >= 3.11 (for CDK app)
- **Docker** (for building the bridge container image; ARM64 support via Docker Desktop or buildx)
- **AWS CDK** v2 (`npm install -g aws-cdk`)
- **Telegram Bot Token** from [@BotFather](https://t.me/BotFather)

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/aws-samples/sample-host-openclaw-on-amazon-bedrock-agentcore.git
cd sample-host-openclaw-on-amazon-bedrock-agentcore

# Set your AWS account and region
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=us-west-2  # change to your preferred region
```

Or edit `cdk.json` directly:
```json
{
  "context": {
    "account": "123456789012",
    "region": "us-west-2"
  }
}
```

### 2. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Bootstrap CDK (first time only)

```bash
cdk bootstrap aws://$CDK_DEFAULT_ACCOUNT/$CDK_DEFAULT_REGION
```

### 4. Deploy all stacks

```bash
cdk synth          # validate (runs cdk-nag security checks)
cdk deploy --all --require-approval never
```

This deploys 7 stacks in order:
1. **OpenClawVpc** — VPC, subnets, NAT gateway, VPC endpoints
2. **OpenClawSecurity** — KMS, Secrets Manager, Cognito, CloudTrail
3. **OpenClawAgentCore** — Runtime, WorkloadIdentity, ECR, S3, IAM
4. **OpenClawRouter** — Lambda + API Gateway HTTP API, DynamoDB identity table
5. **OpenClawObservability** — Dashboards, alarms, Bedrock logging
6. **OpenClawTokenMonitoring** — DynamoDB, Lambda processor, token analytics
7. **OpenClawCron** — EventBridge Scheduler group, Cron executor Lambda, Scheduler IAM role

The CDK AgentCore stack creates the ECR repository. The container image does not need to exist at deploy time — AgentCore only pulls the image when spinning up a microVM for a user session.

### 5. Build and push the bridge container image

After the CDK deploy creates the ECR repository, build and push the bridge container image.

```bash
# Authenticate Docker to ECR
aws ecr get-login-password --region $CDK_DEFAULT_REGION | \
  docker login --username AWS --password-stdin \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com

# Read version from cdk.json for versioned image tags
VERSION=$(python3 -c "import json; print(json.load(open('cdk.json'))['context']['image_version'])")

# Build ARM64 image (required by AgentCore Runtime)
docker build --platform linux/arm64 -t openclaw-bridge:v${VERSION} bridge/

# Tag and push
docker tag openclaw-bridge:v${VERSION} \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:v${VERSION}
docker push \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:v${VERSION}
```

### 6. Store your Telegram bot token

```bash
aws secretsmanager update-secret \
  --secret-id openclaw/channels/telegram \
  --secret-string 'YOUR_TELEGRAM_BOT_TOKEN' \
  --region $CDK_DEFAULT_REGION
```

### 7. Set up Telegram webhook and add yourself to the allowlist

The setup script registers the webhook and adds you to the bot's allowlist in one step:

```bash
./scripts/setup-telegram.sh
```

The script will:
1. Register the Telegram webhook with API Gateway (with secret token for request validation)
2. Prompt you for your Telegram user ID (find it via [@userinfobot](https://t.me/userinfobot) on Telegram)
3. Add you to the DynamoDB allowlist so you can use the bot immediately

<details>
<summary>Manual setup (if you prefer individual commands)</summary>

```bash
# Get Router API URL
API_URL=$(aws cloudformation describe-stacks \
  --stack-name OpenClawRouter \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text --region $CDK_DEFAULT_REGION)

# Get the webhook secret (used for request validation)
WEBHOOK_SECRET=$(aws secretsmanager get-secret-value \
  --secret-id openclaw/webhook-secret \
  --region $CDK_DEFAULT_REGION --query SecretString --output text)

# Point Telegram to the webhook with secret_token for validation
TELEGRAM_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id openclaw/channels/telegram \
  --region $CDK_DEFAULT_REGION --query SecretString --output text)
curl "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook?url=${API_URL}webhook/telegram&secret_token=${WEBHOOK_SECRET}"

# Add yourself to the allowlist (find your ID via @userinfobot on Telegram)
./scripts/manage-allowlist.sh add telegram:YOUR_TELEGRAM_USER_ID
```

</details>

### 9. Verify

Send a message to your Telegram bot. The first message triggers a cold start — the lightweight agent responds in ~10-15 seconds (with file storage and scheduling support) while OpenClaw initializes in the background (~2-4 minutes). After OpenClaw is ready, the full feature set is available. Subsequent messages in the same session are fast.

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
    agentcore-contract.js         # AgentCore HTTP contract with hybrid routing (shim + OpenClaw)
    lightweight-agent.js          # Warm-up agent shim (s3-user-files + eventbridge-cron tools)
    lightweight-agent.test.js     # Lightweight agent unit tests (node:test, 70 tests)
    agentcore-proxy.js            # OpenAI -> Bedrock ConverseStream adapter + Identity + multimodal images
    image-support.test.js         # Image support unit tests (node:test)
    workspace-sync.js             # .openclaw/ directory S3 sync (restore/save/periodic)
    force-ipv4.js                 # DNS patch for Node.js 22 IPv6 issue
    CLAUDE.md                     # Project instructions (for Claude Code IDE)
    skills/
      s3-user-files/              # Custom per-user file storage skill (S3-backed)
      eventbridge-cron/           # Cron scheduling skill (EventBridge Scheduler)
  lambda/
    token_metrics/index.py        # Bedrock log -> DynamoDB + CloudWatch metrics
    router/index.py               # Webhook router (Telegram + Slack, image uploads)
    router/test_image_upload.py   # Image upload unit tests (pytest)
    cron/index.py                 # Cron executor (warmup, invoke, deliver)
  scripts/
    setup-telegram.sh             # Telegram webhook + admin allowlist (one-step)
    setup-slack.sh                # Slack Event Subscriptions + admin allowlist
    manage-allowlist.sh           # Add/remove/list users in the allowlist
  tests/
    e2e/                          # E2E tests (simulated Telegram webhooks + CloudWatch logs)
      config.py                   # AWS config auto-discovery (CF outputs, Secrets Manager)
      webhook.py                  # Build + POST Telegram webhook payloads
      session.py                  # DynamoDB session/user reset + AgentCore session stop
      log_tailer.py               # CloudWatch log tailing with pattern matching
      bot_test.py                 # CLI entrypoint + pytest test classes (11 tests)
      conftest.py                 # pytest fixtures, conversation scenarios
  docs/
    architecture.md               # Detailed architecture diagram
```

## CDK Stacks

| Stack | Resources | Dependencies |
|---|---|---|
| **OpenClawVpc** | VPC (2 AZ), private/public subnets, NAT, 7 VPC endpoints, flow logs | None |
| **OpenClawSecurity** | KMS CMK, Secrets Manager (7 secrets incl. webhook validation), Cognito User Pool, CloudTrail | None |
| **OpenClawAgentCore** | CfnRuntime, CfnRuntimeEndpoint, CfnWorkloadIdentity, ECR, S3 bucket, SG, IAM | Vpc, Security |
| **OpenClawRouter** | Lambda, API Gateway HTTP API (explicit routes, throttling), DynamoDB identity table | AgentCore, Security |
| **OpenClawObservability** | Operations dashboard, alarms (errors, latency, throttles), SNS, Bedrock logging | None |
| **OpenClawTokenMonitoring** | DynamoDB (single-table, 4 GSIs), Lambda processor, analytics dashboard | Observability |
| **OpenClawCron** | EventBridge Scheduler group, Cron executor Lambda, Scheduler IAM role | AgentCore, Router, Security |

## Configuration

All tunable parameters are in `cdk.json`:

| Parameter | Default | Description |
|---|---|---|
| `account` | (empty) | AWS account ID. Falls back to `CDK_DEFAULT_ACCOUNT` env var |
| `region` | `us-west-2` | AWS region. Falls back to `CDK_DEFAULT_REGION` env var |
| `default_model_id` | `global.anthropic.claude-opus-4-6-v1` | Bedrock model ID. The `global.` prefix routes to any available region automatically |
| `subagent_model_id` | (empty) | Bedrock model ID for sub-agents. Empty = use `default_model_id`. Set to e.g. `global.anthropic.claude-sonnet-4-6-v1` for faster/cheaper sub-agents |
| `cloudwatch_log_retention_days` | `30` | Log retention in days |
| `daily_token_budget` | `1000000` | Daily token budget alarm threshold |
| `daily_cost_budget_usd` | `5` | Daily cost budget alarm threshold (USD) |
| `session_idle_timeout` | `1800` | Per-user session idle timeout (seconds) |
| `session_max_lifetime` | `28800` | Per-user session max lifetime (seconds) |
| `workspace_sync_interval_seconds` | `300` | .openclaw/ S3 sync interval |
| `router_lambda_timeout_seconds` | `300` | Router Lambda timeout |
| `router_lambda_memory_mb` | `256` | Router Lambda memory |
| `registration_open` | `false` | If `true`, anyone can message the bot. If `false`, only allowlisted users can register |
| `token_ttl_days` | `90` | DynamoDB token usage record TTL |
| `image_version` | `1` | Bridge container version tag. Bump to force container redeploy |
| `user_files_ttl_days` | `365` | S3 per-user file expiration |
| `cron_lambda_timeout_seconds` | `600` | Cron executor Lambda timeout (must exceed warmup time) |
| `cron_lambda_memory_mb` | `256` | Cron executor Lambda memory |
| `cron_lead_time_minutes` | `5` | Minutes before schedule time to start warmup |

## Channel Setup

### Telegram

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Create a new bot with `/newbot`
3. Copy the bot token
4. Store it in Secrets Manager:
   ```bash
   aws secretsmanager update-secret \
     --secret-id openclaw/channels/telegram \
     --secret-string 'YOUR_BOT_TOKEN' \
     --region $CDK_DEFAULT_REGION
   ```
5. Set up the webhook (see Quick Start step 8)

### Slack

OpenClaw uses **Slack Events API** with the Router Lambda as the webhook endpoint. Incoming requests are validated using Slack's HMAC signing secret.

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App** > **From scratch**
2. Give it a name (e.g., "OpenClaw") and select your workspace
3. If **Settings** > **Socket Mode** is enabled, turn it **off** (Socket Mode hides the Event Subscriptions URL field)

**Add OAuth Scopes:**

4. Go to **Features** > **OAuth & Permissions** > **Scopes** > **Bot Token Scopes** and add:
   - `chat:write` — send messages
   - `files:read` — download image attachments (required for image upload support)
   - `app_mentions:read` — detect @mentions (optional)
   - `im:history` — read DM history
   - `im:read` — access DMs
   - `im:write` — send DMs
5. Click **Install to Workspace** and authorize

**Enable direct messages:**

6. Go to **Features** > **App Home**
7. Under **Show Tabs**, enable **Messages Tab**
8. Check **Allow users to send Slash commands and messages from the messages tab**

**Configure Event Subscriptions:**

9. Get your API Gateway URL (you'll need this for the Request URL):
    ```bash
    aws cloudformation describe-stacks \
      --stack-name OpenClawRouter \
      --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
      --output text --region $CDK_DEFAULT_REGION
    ```
10. Go to **Features** > **Event Subscriptions** and toggle **Enable Events** on
11. Set the **Request URL** to your API URL followed by `webhook/slack`, e.g.:
    ```
    https://<your-api-id>.execute-api.us-west-2.amazonaws.com/webhook/slack
    ```
    Slack sends a verification challenge — you should see a green checkmark confirming the URL is valid.
12. Under **Subscribe to bot events**, add:
    - `message.im` — receive direct messages
    - `message.channels` — messages in channels the bot is in (optional)
13. Click **Save Changes**

**Store credentials in Secrets Manager:**

14. From **Settings** > **Basic Information** > **App Credentials**, copy the **Signing Secret** (a hex string like `a1b2c3d4...` — this is NOT the app-level token that starts with `xapp-`)
15. From **Features** > **OAuth & Permissions**, copy the **Bot User OAuth Token** (starts with `xoxb-`)
16. Store both values:
    ```bash
    aws secretsmanager update-secret \
      --secret-id openclaw/channels/slack \
      --secret-string '{"botToken":"xoxb-YOUR-BOT-TOKEN","signingSecret":"YOUR-SIGNING-SECRET"}' \
      --region $CDK_DEFAULT_REGION
    ```

The signing secret is used by the Router Lambda to validate `X-Slack-Signature` HMAC on every incoming webhook request (with 5-minute replay attack prevention).

**Add yourself to the allowlist:**

17. Find your Slack member ID: click your profile picture → **Profile** → **⋯** (more) → **Copy member ID**
18. Run the setup script (handles steps 9–11 and the allowlist in one go):
    ```bash
    ./scripts/setup-slack.sh
    ```
    Or add yourself manually:
    ```bash
    ./scripts/manage-allowlist.sh add slack:YOUR_MEMBER_ID
    ```

## How It Works

### Per-User Sessions

Each user gets their own AgentCore microVM. When a user sends a message:

1. **Router Lambda** receives the webhook, resolves user identity in DynamoDB, and calls `InvokeAgentRuntime` with a per-user session ID
2. **Contract server** (port 8080) handles the invocation — on first message, it runs parallel initialization:
   - Starts the Bedrock proxy with `USER_ID`/`CHANNEL` env vars
   - Starts OpenClaw gateway in headless mode (background)
   - Restores `.openclaw/` workspace from S3 (background)
   - Waits for proxy only (~5s), then the **lightweight agent** handles the message immediately
3. **Lightweight agent** (warm-up phase, ~5s to ~2-4min) runs an agentic loop with 10 tools: `web_fetch`, `web_search`, S3 file storage (read/write/list/delete), and EventBridge cron scheduling (create/list/update/delete). Web tools include SSRF prevention (IP blocklists, DNS rebinding mitigation). All responses include a deterministic warm-up footer
4. **WebSocket bridge** (after OpenClaw ready, ~2-4min) takes over — messages route to OpenClaw which provides full tool profile, 5 ClawHub skills, and sub-agent support. Responses no longer have the warm-up footer
5. **Router Lambda** sends the response back to the channel (Telegram/Slack API)

When the session idles (default 30 min), AgentCore terminates the microVM. Before shutdown, the SIGTERM handler saves `.openclaw/` to S3. The next message creates a fresh microVM and restores the workspace.

### Image Uploads

Users can send photos alongside text messages. The system supports JPEG, PNG, GIF, and WebP images up to 3.75 MB (the Bedrock Converse API limit).

**How it works:**

1. **Router Lambda** detects an image in the incoming webhook (Telegram `photo` array or `document` with image MIME type; Slack `files` with image MIME type)
2. **Router Lambda** downloads the image from the channel API (Telegram `getFile` endpoint; Slack `url_private_download` with Bearer auth) and uploads it to S3 under `{namespace}/_uploads/img_{timestamp}_{hex}.{ext}`
3. The message payload sent to AgentCore becomes a structured object: `{"text": "caption text", "images": [{"s3Key": "...", "contentType": "image/jpeg"}]}`
4. **Contract server** converts this to a string with an appended marker: `caption text\n\n[OPENCLAW_IMAGES:[...]]`
5. **Proxy** extracts the marker, fetches the image bytes from S3 (validating the S3 key belongs to the user's namespace), and builds Bedrock multimodal content blocks
6. **Bedrock ConverseStream** receives both text and image content, enabling Claude to reason about the image

**Telegram**: Photos use the `caption` field for text (not `text`). The Router Lambda checks both. The largest photo size in the `photo` array is used.

**Slack**: The bot requires the `files:read` OAuth scope to download file attachments. Without it, images are silently ignored and only text is processed.

### Cross-Channel Account Linking

By default, each channel creates a separate user identity. If you use both Telegram and Slack, you'll have two separate sessions with separate conversation histories. To unify them into a single identity and shared session:

1. **On your first channel** (e.g., Telegram), send: `link`
   - The bot responds with a 6-character code (e.g., `A1B2C3`) valid for 10 minutes
2. **On your second channel** (e.g., Slack), send: `link A1B2C3`
   - The bot confirms the accounts are linked

After linking, both channels route to the same user, the same AgentCore session, and the same conversation history. The bind code is stored in DynamoDB with a 10-minute TTL and deleted after use.

You can link multiple channels to the same identity by repeating the process.

### Access Control (User Allowlist)

By default, the bot is **private** (`registration_open: false` in `cdk.json`). Only users on the allowlist can register. Existing users (already registered) are always allowed through.

When an unauthorized user messages the bot, they receive a rejection message that includes their channel ID:

> *Sorry, this bot is private and requires an invitation.*
> *Your ID: `telegram:123456`*
> *Send this ID to the bot admin to request access.*

**Adding users:**

```bash
# Add a user to the allowlist
./scripts/manage-allowlist.sh add telegram:123456

# Remove a user
./scripts/manage-allowlist.sh remove telegram:123456

# List all allowed users
./scripts/manage-allowlist.sh list
```

Only the first channel identity needs to be allowlisted. When a user binds a second channel (e.g. Slack) via `link`, the new channel maps to their existing approved user — no separate allowlist entry needed.

To make the bot open to everyone, set `registration_open: true` in `cdk.json` and redeploy.

### Scheduled Tasks (Cron Jobs)

The agent can create, manage, and execute **recurring scheduled tasks** using Amazon EventBridge Scheduler. Schedules persist across sessions and fire even when the user is not chatting — the response is delivered to the user's Telegram or Slack channel automatically.

**Just ask the bot in natural language.** Examples:

| What you say | What the bot does |
|---|---|
| "Remind me every day at 7am to check my email" | Creates a daily schedule at 7:00 AM in your timezone |
| "Every weekday at 5pm remind me to log my hours" | Creates a MON-FRI schedule at 17:00 |
| "Send me a weather update every morning at 8" | Creates a daily schedule at 8:00 AM |
| "What schedules do I have?" | Lists all your active schedules |
| "Change my morning reminder to 8:30am" | Updates the schedule expression |
| "Pause my daily reminder" | Disables the schedule (keeps it for later) |
| "Resume my daily reminder" | Re-enables a paused schedule |
| "Delete all my reminders" | Removes all schedules |

The bot will ask for your **timezone** (e.g., `Australia/Sydney`, `America/New_York`, `Asia/Tokyo`) if it doesn't know it yet.

**How it works under the hood:**

1. The bot uses the `eventbridge-cron` skill to create an EventBridge Scheduler rule in the `openclaw-cron` schedule group
2. At the scheduled time, EventBridge invokes the Cron executor Lambda (`openclaw-cron-executor`)
3. The Lambda warms up the user's AgentCore session (or waits for it to initialize if cold)
4. The Lambda sends the scheduled message to the agent via AgentCore
5. The agent processes the message and the Lambda delivers the response to the user's chat channel

Each user's schedules are isolated — no cross-user access. Schedule metadata is stored in the DynamoDB identity table alongside user profiles and session data.

### Container Startup Sequence

1. **entrypoint.sh**: Configure Node.js IPv4 DNS patch, start contract server
2. **agentcore-contract.js** (port 8080): Responds to `/ping` with `Healthy` immediately
3. **At boot** (background): Pre-fetch secrets from Secrets Manager (~2s)
4. **On first `/invocations` with `action: chat`, `action: warmup`, or `action: cron`** (parallel init):
   - Start `agentcore-proxy.js` (port 18790) with `USER_ID`/`CHANNEL` env vars
   - Start OpenClaw gateway (port 18789) in background
   - Restore `.openclaw/` from S3 via `workspace-sync.js` in background
   - Wait for proxy only (~5s)
5. **Warm-up phase** (t=~10s to ~2-4min): `lightweight-agent.js` handles messages via proxy -> Bedrock (supports s3-user-files and eventbridge-cron tools — users can manage files and schedules immediately)
6. **Handoff** (~2-4min): OpenClaw becomes ready, all subsequent messages route via WebSocket bridge
7. **After handoff**: Full OpenClaw features — built-in web tools (`web_search`, `web_fetch`), 5 ClawHub skills (jina-reader, deep-research-pro, telegram-compose, transcript, task-decomposer), sub-agent support, session management
8. **SIGTERM**: Save `.openclaw/` to S3, kill child processes, exit

### Message Flow

1. User sends message (text/photo) → Telegram/Slack webhook → API Gateway → Router Lambda
2. Lambda returns 200 immediately, self-invokes async for processing
3. Lambda resolves user identity in DynamoDB, uploads photos to S3 if present
4. Lambda calls `InvokeAgentRuntime` with per-user session ID
5. Contract server triggers lazy init (first message) or bridges to OpenClaw directly
6. Proxy converts to Bedrock ConverseStream API call (multimodal if images present)
7. Response streams back → Lambda sends to channel API

### Tools & Skills

The agent runs with OpenClaw's **full tool profile** enabled, giving it access to built-in tool groups (web, filesystem, runtime, sessions, automation). Two custom skills are included:

| Skill | Purpose |
|---|---|
| `eventbridge-cron` | Cron scheduling via EventBridge Scheduler — create, update, and delete recurring tasks |
| `s3-user-files` | Per-user file storage (S3-backed) — read, write, list, and delete files |

Five ClawHub community skills are pre-installed at Docker build time:

| ClawHub Skill | Purpose |
|---|---|
| `jina-reader` | Extract web content as clean markdown |
| `deep-research-pro` | In-depth multi-step research (spawns sub-agents) |
| `telegram-compose` | Rich HTML formatting for Telegram messages |
| `transcript` | YouTube video transcript extraction |
| `task-decomposer` | Break complex requests into subtasks (spawns sub-agents) |

During the warm-up phase (~first 2-4 min on cold start), the **lightweight agent shim** handles messages with built-in `web_fetch` and `web_search` tools, plus `s3-user-files` and `eventbridge-cron` skills. ClawHub skills become available after OpenClaw fully starts.

### Webhook Security

The Router Lambda validates all incoming webhook requests:

- **Telegram**: Validates the `X-Telegram-Bot-Api-Secret-Token` header against the `openclaw/webhook-secret` stored in Secrets Manager. The secret is registered with Telegram via the `secret_token` parameter on `setWebhook`.
- **Slack**: Validates the `X-Slack-Signature` HMAC-SHA256 header using the Slack app's signing secret. Includes 5-minute timestamp check to prevent replay attacks.
- **API Gateway**: Only explicit routes are exposed (`POST /webhook/telegram`, `POST /webhook/slack`, `GET /health`). All other paths return 404 from API Gateway without invoking the Lambda. Rate limiting is applied (burst: 50, sustained: 100 req/s).

Requests that fail validation receive a 401 response and are logged with the source IP.

### Token Usage Tracking

Bedrock invocation logs flow to CloudWatch, where a Lambda processor extracts token counts, estimates costs, and writes to DynamoDB (single-table design with 4 GSIs for different query patterns). Custom CloudWatch metrics power the analytics dashboard and budget alarms.

## Operations

### Check runtime status

```bash
RUNTIME_ID=$(aws cloudformation describe-stacks \
  --stack-name OpenClawAgentCore \
  --query "Stacks[0].Outputs[?OutputKey=='RuntimeId'].OutputValue" \
  --output text --region $CDK_DEFAULT_REGION)

aws bedrock-agentcore get-runtime \
  --agent-runtime-id $RUNTIME_ID \
  --region $CDK_DEFAULT_REGION
```

### Check DynamoDB identity table

```bash
aws dynamodb scan --table-name openclaw-identity --region $CDK_DEFAULT_REGION
```

### Deploy new bridge version

```bash
# 1. Bump image_version in cdk.json (or use -c image_version=N on the CLI)
#    This forces AgentCore to pull the new container image.
# 2. Build + push image
VERSION=$(python3 -c "import json; print(json.load(open('cdk.json'))['context']['image_version'])")
docker build --platform linux/arm64 -t openclaw-bridge:v${VERSION} bridge/
docker tag openclaw-bridge:v${VERSION} \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:v${VERSION}
aws ecr get-login-password --region $CDK_DEFAULT_REGION | \
  docker login --username AWS --password-stdin \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com
docker push \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:v${VERSION}
# 3. CDK deploy
cdk deploy OpenClawAgentCore --require-approval never
# 4. New sessions will use the new image automatically (per-user idle termination)
```

### Run tests

```bash
cd bridge && node --test proxy-identity.test.js       # identity + workspace tests
cd bridge && node --test image-support.test.js         # image upload + multimodal tests
cd bridge && node --test lightweight-agent.test.js     # lightweight agent tools + buildToolArgs tests
cd bridge/skills/s3-user-files && AWS_REGION=$CDK_DEFAULT_REGION node --test common.test.js  # S3 skill tests
cd lambda/router && python -m pytest test_image_upload.py -v   # image upload unit tests

# E2E tests (requires deployed stack + E2E_TELEGRAM_CHAT_ID/E2E_TELEGRAM_USER_ID env vars)
pytest tests/e2e/bot_test.py -v -k smoke               # connectivity + webhook auth
pytest tests/e2e/bot_test.py -v -k lifecycle            # full message lifecycle
pytest tests/e2e/bot_test.py -v -k cold_start           # new session creation
pytest tests/e2e/bot_test.py -v -k warmup               # warm-up shim verification
pytest tests/e2e/bot_test.py -v -k full_startup          # full OpenClaw startup + timing (~5min)
pytest tests/e2e/bot_test.py -v -k conversation          # multi-turn + rapid-fire
pytest tests/e2e/bot_test.py -v                          # all E2E tests
```

### Security validation

```bash
cdk synth   # Runs cdk-nag AwsSolutions checks — should produce no errors
```

## Troubleshooting

### Container fails health check (RuntimeClientError: health check timed out)

The AgentCore contract server on port 8080 must start within seconds. If `entrypoint.sh` does slow operations (like Secrets Manager calls) before starting the contract server, the health check will time out. The contract server is started as step 1 to avoid this.

### First message is slow (~4 minutes for full OpenClaw)

This is expected for full OpenClaw initialization. However, the **lightweight agent shim** responds to the first message in ~10-15 seconds with support for file storage and cron scheduling tools. OpenClaw initializes in the background (~2-4 minutes) and takes over once ready. The Router Lambda sends a "typing" indicator to Telegram while waiting. Subsequent messages in the same session are fast.

### Slack bot not responding

- **Socket Mode conflict**: If Event Subscriptions doesn't show a Request URL field, disable **Settings** > **Socket Mode**. Socket Mode uses WebSocket connections instead of webhooks.
- **Signing secret mismatch**: The Lambda validates `X-Slack-Signature` using the signing secret stored in Secrets Manager. Verify it matches:
  ```bash
  aws secretsmanager get-secret-value \
    --secret-id openclaw/channels/slack \
    --region $CDK_DEFAULT_REGION \
    --query SecretString --output text
  ```
- **Bot not in DMs**: Go to **Features** > **App Home** and enable **Messages Tab** + **Allow users to send messages**.
- **Separate session from Telegram**: By default, Slack and Telegram create separate user identities. Use the cross-channel linking feature (see above) to unify them into a single session.

### Telegram bot not responding

- **Token invalid**: Check that the Telegram token in Secrets Manager is correct:
  ```bash
  aws secretsmanager get-secret-value \
    --secret-id openclaw/channels/telegram \
    --region $CDK_DEFAULT_REGION \
    --query SecretString --output text
  ```
- **Webhook not set**: Verify the webhook is configured:
  ```bash
  curl "https://api.telegram.org/bot${TELEGRAM_TOKEN}/getWebhookInfo"
  ```
- **Router Lambda errors**: Check Lambda logs in CloudWatch

### 502 / Bedrock authorization errors

- **Model access not enabled**: Enable model access in the Bedrock console for your region.
- **Cross-region inference**: The default model ID `global.anthropic.claude-opus-4-6-v1` uses a global cross-region inference profile that routes to any available region. The IAM policy uses `arn:aws:bedrock:*::foundation-model/*` and `arn:aws:bedrock:{region}:{account}:inference-profile/*` to allow all regions.

### Node.js ETIMEDOUT / ENETUNREACH in VPC

Node.js 22's Happy Eyeballs (`autoSelectFamily`) tries both IPv4 and IPv6. In VPCs without IPv6, this causes connection failures. The `force-ipv4.js` script patches `dns.lookup()` to force IPv4 only, loaded via `NODE_OPTIONS`.

## Known Limitations

| Limitation | Details |
|---|---|
| **Cold start time** | Lightweight agent responds in ~5-15s; full OpenClaw ready in ~2-4 min (plugin registration) |
| **Image size** | Max 3.75 MB per image (Bedrock Converse API limit) |
| **Session timeout** | Sessions terminate after 30 min idle (configurable via `session_idle_timeout`) |
| **ClawHub skills** | 5 pre-installed; available only after full OpenClaw startup (~2-4 min). During warm-up, built-in web_fetch/web_search tools are available |
| **Single region** | AgentCore Runtime deployed in one region; no multi-region failover |
| **No voice/video** | Only text and images supported; no audio or video messages |

## Gotchas

- **ARM64 required**: AgentCore Runtime runs ARM64 containers. Build with `--platform linux/arm64`.
- **Push image after CDK deploy**: The CDK AgentCore stack creates the ECR repository. Do **not** manually create it beforehand (causes a `Resource already exists` error). Deploy CDK first, then push the image. AgentCore only pulls the image when a user session starts, not at deploy time.
- **AgentCore resource names**: Must match `^[a-zA-Z][a-zA-Z0-9_]{0,47}$` — use underscores, not hyphens.
- **Per-user sessions**: Contract returns `Healthy` (not `HealthyBusy`) — allows natural idle termination after `session_idle_timeout`.
- **VPC endpoints**: The `bedrock-agentcore-runtime` VPC endpoint is not available in all regions. Omit it if your region doesn't support it.
- **CDK RetentionDays**: `logs.RetentionDays` is an enum, not constructable from int. Use the helper in `stacks/__init__.py`.
- **Cognito passwords**: HMAC-derived (`HMAC-SHA256(secret, actorId)`) — deterministic, never stored. Enables `AdminInitiateAuth` without per-user password storage.
- **`skills.allowBundled` is an array**: OpenClaw expects `["*"]` (not `true`) — boolean causes config validation failure.
- **ClawHub skills**: 5 community skills are pre-installed at Docker build time (jina-reader, deep-research-pro, telegram-compose, transcript, task-decomposer). Custom skills (s3-user-files, eventbridge-cron) are in `/skills/` loaded via `extraDirs`. ClawHub installs to the managed skills path, scanned automatically by OpenClaw.
- **ClawHub `--force` flag**: Some skills are flagged by VirusTotal for external API calls. Use `--no-input --force` for non-interactive Docker builds.
- **`default-user` fallback**: If identity resolution fails, requests fall back to `actorId = "default-user"` — meaning all such users share one S3 namespace. The `USER_ID` env var path (set by contract server) should prevent this in per-user mode.
- **actorId vs namespace format**: The actorId uses colon format (`telegram:123456789`) while skill scripts expect namespace/underscore format (`telegram_123456789`). The lightweight agent's `chat()` function converts via `userId.replace(/:/g, "_")` before passing to tool scripts. The proxy and workspace sync also use namespace format for S3 keys.
- **Image version bumps are required**: After pushing a new bridge container image, you must bump `image_version` in `cdk.json` and redeploy `OpenClawAgentCore`. AgentCore caches images by digest and only re-pulls when the runtime endpoint configuration changes. Without the bump, existing sessions continue using the old image.
- **Image upload size limit**: Bedrock Converse API limits images to 3.75 MB. The Router Lambda checks this before uploading to S3.
- **OpenClaw 2026.2.23 controlUi breaking change**: OpenClaw versions from 2026.2.23 onward require `gateway.controlUi.dangerouslyAllowHostHeaderOriginFallback: true` (or explicit `allowedOrigins`) when binding to non-loopback addresses (`--bind lan`). Without this, the gateway fails to start with: `Error: non-loopback Control UI requires gateway.controlUi.allowedOrigins`. The contract server's `writeOpenClawConfig()` includes this setting.

## Cleanup

```bash
cdk destroy --all
```

Note: KMS keys and the Cognito User Pool have `RETAIN` removal policies and will not be deleted automatically. Remove them manually if needed.

## Security

See [SECURITY.md](SECURITY.md) for the detailed security architecture, and [CONTRIBUTING.md](CONTRIBUTING.md#security-issue-notifications) for reporting security issues.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
