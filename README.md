# OpenClaw on AWS Bedrock AgentCore

[![License: MIT-0](https://img.shields.io/badge/License-MIT--0-blue.svg)](LICENSE)
[![Status: Experimental](https://img.shields.io/badge/Status-Experimental-orange.svg)]()
[![AWS CDK](https://img.shields.io/badge/AWS%20CDK-v2-yellow.svg)]()

> **Experimental** — This project is provided for experimentation and learning purposes only. It is **not intended for production use**. APIs, architecture, and configuration may change without notice.

Deploy an AI-powered multi-channel messaging bot (Telegram, Slack, Feishu, WhatsApp) on AWS Bedrock AgentCore Runtime V2 using CDK and the deployment SDK. OpenClaw runs as **per-user serverless containers** on AgentCore Runtime. A Router Lambda handles webhook ingestion from Telegram, Slack, Feishu and WhatsApp, resolves user identity via DynamoDB, and invokes per-user AgentCore sessions. Each user gets their own microVM with workspace persistence: the OpenClaw state dir (`~/.openclaw/`, SQLite session state plus the agent workspace) lives on the container's local disk, is mirrored to AgentCore session storage while the session runs, and is backed up to S3. The agent has built-in tools (web, filesystem, runtime, sessions, automation), custom skills for file storage and cron scheduling, and **EventBridge-based cron scheduling** for recurring tasks.

AgentCore Runtime V2 is available in `us-east-1`, `us-east-2`, `us-west-2`, `eu-west-1` and `ap-northeast-1`. For other Regions, and for existing V1 deployments, use the [`release/v1` branch](docs/release-v1.md); see [Choosing a branch](#choosing-a-branch-v2-or-v1).

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Choosing a branch (V2 or V1)](#choosing-a-branch-v2-or-v1)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Per-user model switching](#per-user-model-switching)
- [Channel Setup](#channel-setup)
- [Operations](#operations)
- [Upgrading from OpenClaw 2026.3.x](#upgrading-from-openclaw-20263x)
- [Troubleshooting](#troubleshooting)
- [Known Limitations](#known-limitations)
- [Security](#security)
- [Cleanup](#cleanup)
- [Contributing](#contributing)
- [License](#license)

## Features

- Per-user Firecracker microVM isolation (AgentCore Runtime)
- Multi-channel support (Telegram, Slack, Feishu, WhatsApp) with cross-channel account linking
- Multimodal: text + image messages via Bedrock ConverseStream
- Per-user model switching with a `/model` chat command, limited to an operator allowlist
- STS session-scoped credentials (per-user S3, DynamoDB, Secrets Manager isolation)
- Custom skills: S3 file storage, EventBridge cron scheduling, API key management, ClawHub skill installer
- Scheduled briefs that deliver only the final answer, send a fixed notice instead of raw errors, and can attach an MP3 on Telegram ([brief audio](docs/how-it-works.md#brief-audio))
- Headless browser (optional, AgentCore Browser API)
- AWS Bedrock Guardrails — content filtering, PII redaction, topic denial, word filters, prompt attack detection
- LLM red team testing — 62 test cases across 12 attack categories via promptfoo
- App-level security E2E tests (TestGuardrailSecurity — 6 tests through the full Telegram webhook pipeline)

Users can send **text and images** — photos sent via Telegram, Slack or Feishu are downloaded by the Router Lambda, stored in S3, and passed to Claude as multimodal content via Bedrock's ConverseStream API. Supported formats: JPEG, PNG, GIF, WebP (max 3.75 MB).

## Architecture

![Architecture: users message Telegram, Slack or Feishu; webhooks reach API Gateway and the Router Lambda, which resolves the user in DynamoDB and invokes OpenClaw 2.0 on a per-user Bedrock AgentCore Runtime microVM; the AgentCore Runtime calls Amazon Bedrock (Claude with Guardrails), keeps state in S3, reads Secrets Manager and Cognito/STS scoped credentials, drives an AgentCore Browser in the VPC, and schedules tasks through EventBridge Scheduler and a Cron Lambda; Bedrock invocation logs feed CloudWatch token monitoring](docs/images/architecture.svg)

The diagram shows the high-level request path. Container internals (contract server, Bedrock proxy, session storage), KMS and networking are described in the [component table](docs/how-it-works.md#components) and in [docs/architecture-detailed.md](docs/architecture-detailed.md).

Messages from a channel reach API Gateway and the Router Lambda, which validates the webhook, resolves the user in DynamoDB and calls `InvokeAgentRuntime` with a per-user session id. Inside the user's microVM, the contract server restores user state and waits for full OpenClaw startup before forwarding messages over WebSocket. OpenClaw calls Bedrock through the local proxy. OpenClaw state lives on local disk, is mirrored to session storage and snapshotted to S3. Scheduled tasks and token monitoring run on their own Lambdas. The AgentCore Browser (`enable_browser`, on by default) is a `CfnBrowserCustom` in the VPC private subnets: the contract server starts a per-user browser session and the `agentcore-browser` skill drives it over CDP to navigate, screenshot and interact with pages; see [Browser Support](docs/how-it-works.md#browser-support-optional). The dashed *Optional: enable_gateway* group is the opt-in `enable_gateway` flag: OpenClaw also calls an AgentCore Gateway over MCP (per-user Cognito access token) whose Lambda targets serve the user-files and schedule tools against the same S3 bucket and EventBridge Scheduler; see [AgentCore Gateway MCP tools](docs/how-it-works.md#agentcore-gateway-mcp-tools-prototype).

The pinned SDK controller manages AgentCore Runtime and its release endpoints in Phase 2 of `scripts/deploy.sh`. `OpenClawAgentCore` provides the execution role, security group and bucket; Phase 3 stacks read the verified manifest. See the [AgentCore Runtime V2 runbook](docs/runtime-v2.md).

How state is saved and restored: [Why S3 Workspace Sync?](docs/how-it-works.md#why-s3-workspace-sync) and [Session Storage](docs/how-it-works.md#session-storage-persistent-filesystem).

### How it works

Component-by-component reference, moved to [docs/how-it-works.md](docs/how-it-works.md):

- [Components](docs/how-it-works.md#components): the component table
- [CDK Stacks](docs/how-it-works.md#cdk-stacks): resources and dependencies per stack
- [First-Message Startup Diagram](docs/how-it-works.md#first-message-startup-diagram): what happens on the first message of a session
- [Per-User Sessions](docs/how-it-works.md#per-user-sessions)
- [Image Uploads](docs/how-it-works.md#image-uploads)
- [Cross-Channel Account Linking](docs/how-it-works.md#cross-channel-account-linking)
- [Access Control (User Allowlist)](docs/how-it-works.md#access-control-user-allowlist)
- [Scheduled Tasks (Cron Jobs)](docs/how-it-works.md#scheduled-tasks-cron-jobs)
- [API Key Management](docs/how-it-works.md#api-key-management)
- [Browser Support (Optional)](docs/how-it-works.md#browser-support-optional)
- [Container Startup Sequence](docs/how-it-works.md#container-startup-sequence)
- [Message Flow](docs/how-it-works.md#message-flow)
- [Tools & Skills](docs/how-it-works.md#tools--skills)
- [AgentCore Gateway MCP tools (prototype)](docs/how-it-works.md#agentcore-gateway-mcp-tools-prototype)
- [Webhook Security](docs/how-it-works.md#webhook-security)
- [Token Usage Tracking](docs/how-it-works.md#token-usage-tracking)
- [Implementation Notes](docs/how-it-works.md#implementation-notes): behaviour that is easy to break when changing the code

See [docs/architecture-detailed.md](docs/architecture-detailed.md) for sequence diagrams, container internals and data flows, and [docs/architecture.md](docs/architecture.md) for the solution overview.

## Choosing a branch (V2 or V1)

| Branch | AgentCore Runtime | Regions | Deployed with |
|---|---|---|---|
| `main` (this README) | V2: snapshot restore, immutable `release_N` endpoints, no warm-up agent | `us-east-1`, `us-east-2`, `us-west-2`, `eu-west-1`, `ap-northeast-1` | `scripts/deploy.sh` with the pinned SDK controller |
| [`release/v1`](docs/release-v1.md) | V1, with the lightweight warm-up agent | Any Region with AgentCore Runtime | `scripts/deploy.sh` with the AgentCore Starter Toolkit (see that branch's README) |

`scripts/runtime_deploy.py` refuses Regions outside the V2 list (`V2_REGIONS`). `release/v1` gets bug-fix backports, not new features. There is no in-place migration of a V1 deployment or its users to V2, and both branches use the same stack names, so don't deploy `main` over a V1 deployment. Details, the backported fixes and a V1/V2 comparison: [docs/release-v1.md](docs/release-v1.md).

## Prerequisites

- **AWS Account** with Bedrock access, in one of the V2 Regions above
- **AWS CLI** v2 configured with credentials (`aws sts get-caller-identity` should succeed)
- **Node.js** 24.x, 24.16 or later (for the CDK CLI and the bridge unit tests; the container image uses Node 24)
- **Python** >= 3.11 (for CDK app)
- **uv** ([installation](https://docs.astral.sh/uv/getting-started/installation/))
- **Docker buildx** with ARM64 support, or a prebuilt ARM64 ECR image supplied with `--image-uri`
- **AWS CDK** v2 (`npm install -g aws-cdk`)
- **Deployment SDK environment** from `requirements-deploy.txt` (`boto3==1.43.95`, `botocore==1.43.95`)
- **Telegram Bot Token** from [@BotFather](https://t.me/BotFather)

For local builds, the Docker builder must resolve `registry.npmjs.org` and `clawhub.ai` and reach both over HTTPS.

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/aws-samples/sample-host-openclaw-on-amazon-bedrock-agentcore.git
cd sample-host-openclaw-on-amazon-bedrock-agentcore

# Set your AWS account and region
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=us-west-2  # must be a V2 Region, see "Choosing a branch"
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
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
source .venv/bin/activate
```

### 3. Bootstrap CDK (first time only)

```bash
cdk bootstrap aws://$CDK_DEFAULT_ACCOUNT/$CDK_DEFAULT_REGION
```

### 4. Install the deployment SDK

The project uses a **hybrid deployment model**: CDK manages infrastructure (VPC, Lambda, DynamoDB, S3, etc.) while the pinned SDK controller deploys AgentCore Runtime V2. See the [AgentCore Runtime V2 runbook](docs/runtime-v2.md) for the platform-version requirement.

```bash
uv venv .venv-deploy
uv pip sync --python .venv-deploy/bin/python requirements-deploy.txt
uv run --no-project --python .venv-deploy/bin/python python scripts/runtime_deploy.py preflight --region "$CDK_DEFAULT_REGION"
```

Preflight checks installed SDK versions and service-model fields without AWS calls.

### 5. Deploy

```bash
cdk synth          # validate (runs cdk-nag security checks)
./scripts/deploy.sh
```

The deploy script runs three phases automatically:
1. **Phase 1 (CDK)** — VPC, Security, AgentCore base, Guardrails and Observability stacks; no manifest required
2. **Phase 2 (SDK)** — Reads CDK outputs, builds an ARM64 container image (or uses `--image-uri`), deploys V2, verifies READY/readback and performs a billed full-OpenClaw chat warmup
3. **Phase 3 (CDK)** — Router, Cron, TokenMonitoring stacks (require the verified manifest from Phase 2)

Failed verification or chat warmup blocks Phase 3.

With the opt-in `enable_gateway: true` in `cdk.json`, Phase 1 also deploys `OpenClawGateway` and Phase 2 reads its `GatewayUrl` output (exact `OutputKey`; the deploy fails if it is empty) and passes it to the AgentCore Runtime as `AGENTCORE_GATEWAY_URL`. With the default `false` the stack is not in the app and the variable is not set. See [AgentCore Gateway MCP tools](docs/how-it-works.md#agentcore-gateway-mcp-tools-prototype).

**Note on Availability Zones:** Bedrock AgentCore Runtime may not be available in all AZs in a region. If deployment fails with an "unsupported availability zones" error, specify supported AZs in `cdk.json`:

```json
{
  "context": {
    "availability_zones": ["us-east-1b", "us-east-1c"]
  }
}
```

To find supported AZs for your region:
1. Check the error message from the failed deployment (it lists supported AZ IDs like `use1-az1`, `use1-az2`)
2. Map AZ IDs to AZ names in your account: `aws ec2 describe-availability-zones --region us-east-1`
3. Update `availability_zones` in `cdk.json` with the AZ names that match the supported AZ IDs
4. Review the VPC change with `cdk diff OpenClawVpc` before redeploying the foundation. Keep the existing network and state configuration when preparing a V2 release.

#### Build modes

By default, the container image is built **locally** with Docker buildx and pushed to ECR. To use a prebuilt ARM64 ECR image, pass `--image-uri`:

| Mode | Command | Requires | Notes |
|------|---------|----------|-------|
| **local-build** (default) | `./scripts/deploy.sh` | Docker buildx | Builds ARM64 image locally |
| **prebuilt** | `./scripts/deploy.sh --image-uri "$IMAGE_URI"` | Existing ECR image | Resolves and records the immutable digest |

#### Running individual phases

```bash
./scripts/deploy.sh --phase1          # Foundation without a manifest
./scripts/deploy.sh --runtime-only    # V2 + real-chat warmup
./scripts/deploy.sh --phase3          # Verify manifest, deploy dependent stacks
./scripts/deploy.sh --cdk-only        # Foundation + verified dependent stacks
```

A new AgentCore Runtime uses immutable endpoint `release_1`. For image or configuration changes, use an unused name, for example `./scripts/deploy.sh --runtime-only --endpoint-name release_2`, then `./scripts/deploy.sh --phase3`. Keep prior release manifests and endpoints for existing users.

> **Note:** Preserve the generated `.runtime/runtime-v2.json`; the wrapper passes it to CDK as `runtime_manifest`. It is gitignored — do not commit it.

### 6. Store your Telegram bot token

> **Timing:** The secret is created (empty) by CDK in Phase 1. Store your bot token any time after Phase 1 completes, before testing the bot. It does not need to be stored before running `./scripts/deploy.sh`.

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

### 8. Verify

Send a message to your Telegram bot after Phase 3 completes. The first message waits for state restoration and full OpenClaw startup. After OpenClaw is ready, the full feature set is available.

## Configuration

All tunable parameters are in `cdk.json`:

| Parameter | Default | Description |
|---|---|---|
| `account` | (empty) | AWS account ID. Falls back to `CDK_DEFAULT_ACCOUNT` env var |
| `region` | (empty) | AWS region. Falls back to `CDK_DEFAULT_REGION` env var |
| `availability_zones` | `[]` | Optional list of AZ names to use for VPC. Set this only if AgentCore Runtime has AZ restrictions in your region. See deployment notes above |
| `default_model_id` | `global.anthropic.claude-opus-5-5` | Primary Bedrock model for the main agent. Opus 5.5 uses its default adaptive thinking and omits temperature; see the [AgentCore Runtime V2 model contract](docs/runtime-v2.md#model-and-api-contract) |
| `fallback_model_id` | `global.anthropic.claude-sonnet-5-5` | Fallback for eligible primary API failures before content starts; never replays partial output or a Guardrail intervention |
| `subagent_model_id` | `global.anthropic.claude-sonnet-5-5` | Bedrock model ID for sub-agents. Empty = the bridge default, also Sonnet 5.5. Sub-agents never use the fallback model |
| `user_selectable_models` | `haiku`, `sonnet`, `opus` (Claude 5.5 global profiles) | Aliases users can pick with `/model <alias>`, mapped to Bedrock model IDs. Set to `{}` to disable switching. See [Per-user model switching](#per-user-model-switching) |
| `cloudwatch_log_retention_days` | `30` | Log retention in days |
| `daily_token_budget` | `1000000` | Threshold of the `openclaw-daily-token-budget` alarm. Despite the name, the alarm compares the token sum over a **1-hour** period |
| `daily_cost_budget_usd` | `5` | Threshold (USD) of the `openclaw-daily-cost-budget` alarm, compared with the estimated cost summed over a **1-hour** period. `5` is low for an Opus-class model; see [Token Usage Tracking](docs/how-it-works.md#token-usage-tracking) |
| `session_idle_timeout` | `1800` | Per-user session idle timeout (seconds) |
| `session_max_lifetime` | `28800` | Per-user session max lifetime (seconds) |
| `workspace_sync_interval_seconds` | `300` | .openclaw/ S3 sync interval |
| `workspace_restore_wait_seconds` | `180` | How long the bridge waits for the S3 `.openclaw/` restore before starting the OpenClaw gateway (passed as `WORKSPACE_RESTORE_WAIT_MS`). The wait ends as soon as the restore finishes; failure or timeout blocks activation |
| `router_lambda_timeout_seconds` | `600` | Router Lambda timeout |
| `router_lambda_memory_mb` | `256` | Router Lambda memory |
| `registration_open` | `false` | If `true`, anyone can message the bot. If `false`, only allowlisted users can register |
| `token_ttl_days` | `90` | DynamoDB token usage record TTL |
| `image_version` | (see `cdk.json`) | Local build image tag. The SDK controller resolves the ECR digest and updates AgentCore Runtime configuration |
| `user_files_ttl_days` | `365` | S3 per-user file expiration |
| `user_files_noncurrent_days` | `30` | Days after which noncurrent S3 object versions expire (CDK context key, not set in `cdk.json`; add it there or pass `-c`) |
| `user_files_noncurrent_keep` | `3` | Newest noncurrent versions always kept per key (CDK context key, not set in `cdk.json`; add it there or pass `-c`) |
| `cron_lambda_timeout_seconds` | `900` | Cron executor Lambda timeout (must exceed warmup time) |
| `cron_lambda_memory_mb` | `256` | Cron executor Lambda memory |
| `enable_cloudtrail` | `false` | Deploy a dedicated CloudTrail trail. Off by default — most accounts already have one. Enabling creates an S3 bucket + trail (additional cost) |
| `cron_lead_time_minutes` | `5` | Minutes before schedule time to start warmup |
| `enable_guardrails` | `true` | Deploy Bedrock Guardrails for content filtering. See the [disable procedure](docs/guardrails.md#disable) before changing shared resources |
| `guardrails_content_filter_level` | `HIGH` | Not read by the stack: filter strengths are set per category in `stacks/guardrails_stack.py` (`PROMPT_ATTACK` input `HIGH`, `INSULTS` input `MEDIUM`, the rest `HIGH`) |
| `guardrails_pii_action` | `ANONYMIZE` | PII handling: `ANONYMIZE` (redact) or `BLOCK` (reject). Credit cards always BLOCK regardless |
| `enable_browser` | `true` | Deploy an AgentCore Browser (`CfnBrowserCustom`) and pass its id to the AgentCore Runtime as `BROWSER_IDENTIFIER`. Only deployed in regions listed in `BROWSER_SUPPORTED_REGIONS` (`stacks/agentcore_stack.py`) |
| `enable_gateway` | `false` | Prototype: deploy `OpenClawGateway` (AgentCore Gateway serving the per-user file and schedule tools as MCP tools) and pass `AGENTCORE_GATEWAY_URL` to the AgentCore Runtime. See [docs/gateway-mcp-tools.md](docs/gateway-mcp-tools.md) |
| `anomaly_band_width` | `2` | Standard-deviation band for the token-usage anomaly detector alarm |
| `runtime_manifest` | (empty for foundation) | Verified V2 manifest required for dependent stacks; wrapper default `.runtime/runtime-v2.json` |
| `table_tags` | `{}` | Extra tags put on every DynamoDB table (identity and token-usage). CloudFormation does not copy its own stack tags onto DynamoDB tables, so if your account runs an automated cleanup job that deletes untagged tables, set the opt-out tag it honours, for example `{"auto-delete": "no"}` (or `-c 'table_tags={"auto-delete":"no"}'`). Deletion protection is on for both tables regardless of this setting |

> **Guardrails cost**: Bedrock Guardrails are enabled by default and add ~$0.75 per 1,000 text units on top of model inference costs. Before disabling, follow the [dependency check](docs/guardrails.md#disable). See [AWS Bedrock Guardrails Pricing](https://aws.amazon.com/bedrock/pricing/#Guardrails). Disabling removes content-level protections but other security layers (STS scoping, tool deny list) remain active.

## Per-user model switching

Users can change the model that answers their own messages from any channel:

| Command | Effect |
|---|---|
| `/model` | Show the current model and the available aliases |
| `/model <alias>` | Use that model for this user's messages, e.g. `/model opus` |
| `/model default` | Go back to the deployment's `default_model_id` |

The Router Lambda handles the command before anything reaches the agent and replies directly; the command is not part of the conversation. Only a message that starts with `/model` is a command (`/model@YourBot opus` also works in Telegram groups). In Slack, type a space before the slash (` /model opus`), otherwise the Slack client treats it as an unknown slash command and does not send it. In Discord, put the command in the `/ask` prompt (`/ask prompt: /model opus`).

The choice is stored as `modelOverride` (the model ID, not the alias) on the user's `USER#<userId>` / `PROFILE` item in the identity table, so it applies on every linked channel and survives session idle timeouts and container recycling. The Router sends it with every chat invocation.

The operator controls which models are offered with `user_selectable_models` in `cdk.json`:

```json
"user_selectable_models": {
  "haiku": "global.anthropic.claude-haiku-5-5",
  "sonnet": "global.anthropic.claude-sonnet-5-5",
  "opus": "global.anthropic.claude-opus-5-5"
}
```

- Aliases are 1-32 lowercase letters, digits, `-` or `_`; `default` is reserved. Values must be Bedrock model or inference profile IDs. CDK synth and `scripts/deploy.sh` reject an invalid map.
- To disable the feature, set `"user_selectable_models": {}` and redeploy both the runtime configuration and the Router (see [Deploy and verify V2](docs/runtime-v2.md#deploy-and-verify-v2)). `/model` then replies that switching is not enabled, and stored choices are ignored.
- Removing a model from the map takes effect at the next deployment: users who picked it go back to `default_model_id` without being told, and `/model` shows them the default.
- Rules that still apply with an override: sub-agents keep `subagent_model_id`; if the chosen model fails before any output, the bridge falls back once to `fallback_model_id` (no fallback when they are the same model); scheduled (Cron) tasks use `default_model_id`. Request settings follow the model actually sent (Haiku 5.5 runs with thinking disabled; Haiku 5.5, Sonnet 5.5 and Opus 5.5 omit temperature).
- Cost: larger models cost more per token. Token monitoring prices Haiku, Sonnet and Opus 5.5, and the daily budget alarms cover all users together.
- The runtime execution role already allows every model through `inference-profile/*` and `foundation-model/*`. If you narrow that policy, keep every allowlisted model's inference profile and its foundation models.

## Channel Setup

Telegram is the quick-start channel. Slack, Feishu and WhatsApp setup is in [docs/channels.md](docs/channels.md).

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
5. Set up the webhook (see Quick Start step 7)

### Slack

Create a Slack app that uses the Events API, point its Request URL at `<ApiUrl>webhook/slack`, store the bot token and signing secret in `openclaw/channels/slack`, and add yourself with `./scripts/setup-slack.sh`. Step-by-step instructions (scopes, App Home, event subscriptions): [docs/channels.md#slack](docs/channels.md#slack).

### Feishu

Create a Feishu app with **Bot** capability and run `./scripts/setup-feishu.sh`. Manual setup and scheduled-task delivery: [docs/channels.md#feishu](docs/channels.md#feishu).

### WhatsApp

Off by default. Create a Meta app with the WhatsApp product (Cloud API), store its token, App Secret, verify token and phone number ID in `openclaw/channels/whatsapp`, and point the webhook at `webhook/whatsapp`: [docs/channels.md#whatsapp](docs/channels.md#whatsapp).

## Operations

Operator commands are in [docs/operations.md](docs/operations.md):

- [Check AgentCore Runtime status](docs/operations.md#check-agentcore-runtime-status)
- [Check DynamoDB identity table](docs/operations.md#inspect-sessions)
- [Deploy new bridge version](docs/operations.md#deploy-a-new-bridge-version)
- [Run tests](docs/operations.md#run-tests)
- [Security validation](docs/operations.md#security-validation)
- Run one-off shell commands inside a live session with `scripts/agentcore-exec.py` (operator-only, full execution-role credentials): [docs/execute-command.md](docs/execute-command.md)
- Scheduled tasks: [delivery, deadlines and failures](docs/how-it-works.md#delivery-deadlines-and-failures) and the [brief audio marker](docs/how-it-works.md#brief-audio) for prompt authors
- Token usage, cost estimates and budget alarms: [Token Usage Tracking](docs/how-it-works.md#token-usage-tracking)
- DynamoDB tables have deletion protection and `RETAIN`; for accounts with automated cleanup of untagged resources, set `table_tags` (see [Configuration](#configuration))

## Upgrading from OpenClaw 2026.3.x

This branch moves the image from OpenClaw 2026.3.8 / Node 22 to **OpenClaw 2026.9.5 ("2.0") / Node 24 / clawhub 0.23.3**. The compatibility behavior below concerns OpenClaw data; see the [AgentCore Runtime V2 migration limits](docs/runtime-v2.md#existing-user-migration-and-cutover).

- **Legacy sessions**: 2.0 stores sessions in per-agent SQLite and refuses readiness while a pre-2.0 `agents/<id>/sessions/sessions.json` is present. The contract runs `openclaw doctor --fix --non-interactive` once before the gateway spawns (bounded by `OPENCLAW_MIGRATION_TIMEOUT_MS`) to import it and records a receipt so later cold starts (which restore the 1.x index from S3 again) skip the import; an unreadable index is moved aside so the gateway still starts. The import runs after the S3 restore, so the first 2.0 boot of a large 1.x user waits for both: in earlier staging tests a state dir of 1,000+ files took 80-115 s to restore and the import of ~1,000 sessions took ~50 s. `deploy.sh` sets the restore wait from `workspace_restore_wait_seconds` (180 s default); if you set `WORKSPACE_RESTORE_WAIT_MS` yourself, or run the image without `deploy.sh` (bridge fallback 45 s), raise it to about 180 s for such users. Activation stops if restoration fails or exceeds the deadline. The imported store can be hundreds of MB; SQLite databases over 10 MB are backed up as streamed gzip multipart objects, at most every 10 min (`WORKSPACE_SYNC_LARGE_SQLITE_MIN_INTERVAL_MS`), up to `WORKSPACE_SYNC_MAX_SQLITE_BYTES` (1 GiB) — see [docs/session-storage.md](docs/session-storage.md).
- **Behaviour-preserving config knobs** written into `openclaw.json` so users see no change: `session.reset: { mode: "daily", atHour: 4 }` (2.0 stopped resetting daily); the new `tools.profile: "full"` tools that need a Control UI or a human answer (`terminal`, `process`, `plugins`, `ask_user`, `secrets`, `screen`, `progress_card`, `nodes`, `heartbeat_respond`, media generation) added to `tools.deny`; `skills.workshop.autonomous.mode: "off"` (autonomous Skill Workshop); Active Memory cross-conversation recall and grounded dreaming disabled (`memory.search.rememberAcrossConversations: false`, `plugins.entries["active-memory"].enabled: false`, `plugins.entries["memory-core"].config.dreaming.enabled: false`).
- **WebSocket protocol 4** with `client.id: "gateway-client"`, `client.mode: "backend"` and no `Origin` header (see [Implementation Notes](docs/how-it-works.md#implementation-notes)).
- **State on local disk**, mirrored to session storage, because the mount has neither SQLite locks nor hard links (see [Session Storage](docs/how-it-works.md#session-storage-persistent-filesystem)).

Full details, risks and the validation checklist: [docs/openclaw-2.0-upgrade.md](docs/openclaw-2.0-upgrade.md).

## Troubleshooting

### Container fails health check (RuntimeClientError: health check timed out)

The contract server starts on port 8080 before user activation and reports healthy after OpenClaw modules are prepared. See the [AgentCore Runtime V2 lifecycle](docs/runtime-v2.md#lifecycle-and-compatibility) for bootstrap failures.

### First message is slow

The first message waits for state restoration and full OpenClaw initialization. Large workspaces can increase this delay; see the [runbook](docs/runtime-v2.md#startup-measurement-points) for timing diagnostics. The Router Lambda sends a typing indicator to Telegram while waiting, and after 30 seconds sends a progress message ("Working on your request...") to both Telegram and Slack so users know the bot is still working. Subsequent messages reuse the initialized session.

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
- **Separate session from Telegram**: By default, Slack and Telegram create separate user identities. Use [cross-channel linking](docs/how-it-works.md#cross-channel-account-linking) to add an unregistered channel to an existing user; independently registered accounts cannot be merged.

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
- **Cross-region inference**: The default primary model `global.anthropic.claude-opus-5-5` and the sub-agent/fallback model `global.anthropic.claude-sonnet-5-5` use global cross-region inference profiles that route to any available region. The IAM policy uses `arn:aws:bedrock:*::foundation-model/*` and `arn:aws:bedrock:{region}:{account}:inference-profile/*` to allow all regions. See the [model contract](docs/runtime-v2.md#model-and-api-contract).
- **Region not supported**: `scripts/runtime_deploy.py` stops with `V2 region is outside the reviewed supported region set` outside the V2 Regions. Use the [`release/v1` branch](docs/release-v1.md) there.

### Scheduled task says it "did not return a result this time"

The Cron Lambda sends this fixed notice when the AgentCore invocation failed, returned an error, or its outcome is unknown (for example the turn ran past the caller's deadline while another brief for the same user was running). It never forwards the error text. Check the `openclaw-cron-executor` logs for the exception type, and stagger schedules that fire close together for one user. See [Delivery, deadlines and failures](docs/how-it-works.md#delivery-deadlines-and-failures).

### Brief audio not sent

The text is delivered and the Lambda logs `Brief audio not sent: <exception type>` or `Brief audio skipped: ...`. Check that the reply's marker line is `BRIEF_AUDIO: <namespace>/<file>.mp3`, the key is in the user's own namespace, and the file is an `.mp3` of at most 50 MB. Audio is sent on Telegram only. See [Brief audio](docs/how-it-works.md#brief-audio).

### Cost alarm fires often, or token usage shows `default-user`

The `openclaw-daily-cost-budget` alarm compares cost per **1-hour** period with `daily_cost_budget_usd` (default `5`); raise it for Opus-class models. Usage is attributed to `default-user` / `unknown` because Bedrock requests carry no request metadata; see [Token Usage Tracking](docs/how-it-works.md#token-usage-tracking).

### Node.js ETIMEDOUT / ENETUNREACH in VPC

Node.js's Happy Eyeballs (`autoSelectFamily`, Node 20+) tries both IPv4 and IPv6. In VPCs without IPv6, this causes connection failures. The `force-ipv4.js` script patches `dns.lookup()` to force IPv4 only, loaded via `NODE_OPTIONS`.

### Deployment gotchas

- **ARM64 required**: AgentCore Runtime runs ARM64 containers. Build with `--platform linux/arm64`.
- **ECR provisioning**: Phase 2 resolves or creates `ECR_REPOSITORY` (default `openclaw-bridge`) before building the image.
- **AgentCore resource names**: Must match `^[a-zA-Z][a-zA-Z0-9_]{0,47}$` — use underscores, not hyphens.
- **VPC endpoints**: The `bedrock-agentcore-runtime` VPC endpoint is not created by `stacks/vpc_stack.py` (the service is not available in every region). AgentCore Runtime API calls from the Lambdas go out via NAT; the Bedrock Runtime endpoint is created with private DNS disabled so `global.*` inference profiles can route cross-region.
- **CDK RetentionDays**: `logs.RetentionDays` is an enum, not constructable from int. Use the helper in `stacks/__init__.py`.
- **ClawHub `--no-input --force` at image build**: still required with clawhub 0.23.3 for non-interactive Docker builds of the five pinned, reviewed skills (`--no-input` disables prompts; `--force` overrides the VirusTotal flag some skills carry for calling external APIs). Verified in the us-west-2 staging CodeBuild log: all five skills print `Installed <slug> v<version>`.
- **AgentCore Runtime changes use the SDK controller**: A CDK-only deploy does not update the AgentCore Runtime image. Deploy a new immutable release for image changes; existing users retain their previous release.
- **Pinned deployment SDK required**: Use the `.venv-deploy` environment from Step 4; preflight rejects different boto3/botocore versions.

## Known Limitations

| Limitation | Details |
|---|---|
| **Cold start time** | The first message waits for state restoration and full OpenClaw startup; large workspaces take longer to restore |
| **AgentCore Runtime platform** | AgentCore Runtime V2 only; existing-user migration and automatic rollback are not implemented |
| **Image size** | Max 3.75 MB per image (Bedrock Converse API limit). The Router Lambda checks this before uploading to S3 |
| **Session timeout** | Sessions terminate after 30 min idle (configurable via `session_idle_timeout`) |
| **ClawHub skills** | 5 pre-installed; available only after full OpenClaw startup |
| **Single region** | AgentCore Runtime deployed in one region; no multi-region failover |
| **V2 Regions only** | `main` deploys only in the AgentCore Runtime V2 Regions; use [`release/v1`](docs/release-v1.md) elsewhere. V1 deployments cannot be migrated in place |
| **No voice/video input** | Users can send text and images only; no audio or video messages. Scheduled briefs can send an MP3 on Telegram ([brief audio](docs/how-it-works.md#brief-audio)) |
| **Token usage attribution** | Bedrock requests carry no request metadata, so token usage is recorded under `default-user` / `unknown` rather than per user and channel |

## Security

This solution applies **defense-in-depth** across network, application, identity, and data layers. Key controls include:

- **Network isolation**: Private VPC subnets with VPC endpoints; no direct internet exposure for containers
- **Webhook authentication**: Cryptographic validation (Telegram secret token, Slack HMAC-SHA256 with replay protection)
- **Per-user isolation**: Each user runs in their own AgentCore microVM with dedicated S3 namespace
- **STS session-scoped credentials**: Container assumes its own role with a session policy restricting S3 and DynamoDB to the user's namespace/records — prevents cross-user data access even through shell tools
- **Encryption**: All data encrypted at rest with customer-managed KMS key (S3, DynamoDB, SNS, Secrets Manager) and in transit (TLS)
- **CloudTrail**: Optional dedicated trail (`enable_cloudtrail` in cdk.json). Off by default — most AWS accounts already have an organization or account-level trail. Enabling adds a dedicated S3 bucket + trail for this project's audit logs
- **Least-privilege IAM**: Tightly scoped permissions per component
- **Bedrock Guardrails**: Content filtering on every Bedrock API call — content filters (hate, violence, prompt attacks), topic denial (6 categories), PII redaction, word filters, and custom regex for credential patterns. Opt-out via `enable_guardrails: false` in `cdk.json`. Known limitations (ordinary scheduled-task prompts blocked as prompt attacks, version pinning): see [Guardrails known limitations](#guardrails-known-limitations)
- **Tool hardening**: OpenClaw `read` tool denied to prevent credential access via `/proc` and local file reads; OpenClaw's channel-delivery tools (`message`, `conversations_send`, `conversations_turn`) denied because OpenClaw has no bot tokens and replies are delivered by the Router/Cron Lambda; `exec` allowed for skill management (scoped STS credentials limit blast radius); proxy bound to loopback only; security group egress restricted to HTTPS
- **Automated compliance**: cdk-nag AwsSolutions checks on every `cdk synth`

See [docs/security.md](docs/security.md) for the complete security architecture (threat model, defense-in-depth layers, operations runbook), [docs/guardrails.md](docs/guardrails.md) for the guardrail runbook, [SECURITY.md](SECURITY.md) for reporting vulnerabilities, and [CONTRIBUTING.md](CONTRIBUTING.md#security-issue-notifications) for contribution guidelines.

### Security testing

#### LLM Red Team Testing

The `redteam/` directory contains a developer-only adversarial testing harness using [promptfoo](https://promptfoo.dev/). It runs 62 test cases across 12 attack categories against the Bedrock model, comparing results with and without Bedrock Guardrails.

**Attack categories tested:** jailbreaks, prompt injection, harmful content, PII fishing, topic denial, credential extraction, tool abuse (SSRF, namespace traversal), channel secret extraction, content filter bypasses (HATE/SEXUAL/INSULTS), encoding bypasses (base64, ROT13, multilingual, Unicode), and session/context manipulation.

```bash
# Run the full red team evaluation
cd redteam && npm install
AWS_REGION=ap-southeast-2 npx promptfoo@latest eval --config evalconfig.yaml

# View interactive report
npx promptfoo@latest view
```

**Results with guardrails enabled:** ~93% pass rate (up from ~77% baseline without guardrails). See [redteam/README.md](redteam/README.md) for details.

#### Guardrail wiring and E2E tests

The guardrail is applied only when the AgentCore Runtime role has `bedrock:ApplyGuardrail` and `scripts/deploy.sh` has set `BEDROCK_GUARDRAIL_ID`/`BEDROCK_GUARDRAIL_VERSION` on the AgentCore Runtime; `tests/e2e/test_guardrail_wiring.py` passes only on a logged guardrail intervention. Details and test commands: [docs/guardrails.md](docs/guardrails.md#wiring-and-end-to-end-tests).

#### Guardrail prompt eval

`scripts/guardrail-eval.py` sends the prompts in `tests/fixtures/guardrail_prompts.json` to a guardrail version through `ApplyGuardrail` (input side, no model call) and prints which policy fired for each one. It exits 1 when a verdict differs from the fixture's `expected` value, so you can compare two guardrail versions or check a new scheduled-task prompt before it reaches production. See [docs/guardrails.md](docs/guardrails.md#evaluating-prompts-against-a-guardrail-version).

### Guardrails known limitations

- **Scheduled-task prompts can be blocked.** `PROMPT_ATTACK` runs at input strength `HIGH`. At that strength ordinary scheduled-brief prompts, such as an instruction to send the brief to the user on a named channel or persona-style rules ("you are my assistant, always ..."), can be classified as prompt attacks and the task gets the guardrail's blocked message instead of a reply. A digest about cryptocurrency prices can also match the `CryptoScams` denied topic. Before enabling guardrails for existing users, run `scripts/guardrail-eval.py` against a copy of your own cron prompts (with the `[Scheduled task: <name>]` prefix the Cron Lambda adds) and adjust the prompts or `stacks/guardrails_stack.py`.
- **Sub-agent tasks can be blocked too.** The guardrail assesses the task text the main agent writes for a sub-agent, which can be classified as a prompt attack in the same way; the sub-agent then returns the blocked message. See [docs/guardrails.md](docs/guardrails.md#known-limitation-sub-agent-requests).
- **The AgentCore Runtime is pinned to a numbered version.** `deploy.sh` passes the `GuardrailVersion` output of `OpenClawGuardrails` (the `CfnGuardrailVersion` resource) to the AgentCore Runtime as `BEDROCK_GUARDRAIL_VERSION`. Editing the policies in `stacks/guardrails_stack.py` and deploying updates only the guardrail's working draft; CloudFormation publishes a new version only when the `CfnGuardrailVersion` resource itself changes (for example its `description`). Update V2 with `./scripts/deploy.sh --runtime-only --endpoint-name release_2`, using a new unused endpoint name for each changed release. See [docs/guardrails.md](docs/guardrails.md#updating-guardrail-policies).

## Cleanup

```bash
cdk destroy --all
```

Note: The SDK-managed AgentCore Runtime and its release endpoints are outside CDK teardown; retain manifests and reconcile active sessions before retiring them. KMS keys, the Cognito User Pool and the DynamoDB identity and token-usage tables have `RETAIN` removal policies and will not be deleted automatically. Remove them manually if needed; the DynamoDB tables also have deletion protection, which you must turn off before deleting them. `OpenClawGateway` is only part of the app (and of `--all`) while `enable_gateway` is `true` in `cdk.json`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to report issues and send pull requests, and [docs/operations.md](docs/operations.md#run-tests) for the unit and E2E test commands.

### Project structure

Full file tree: [docs/project-structure.md](docs/project-structure.md).

| Directory | Contents |
|---|---|
| `stacks/` | CDK stacks (VPC, security, guardrails, AgentCore, router, observability, token monitoring, cron, opt-in gateway); entry point `app.py`, settings `cdk.json` |
| `bridge/` | Container image: contract server, Bedrock proxy, workspace sync, custom skills, unit tests |
| `lambda/` | Router, cron executor, token metrics and Gateway tool Lambdas, with their unit tests |
| `scripts/` | `deploy.sh`, channel setup, allowlist management, operator exec CLI, guardrail prompt eval, cron record repair |
| `tests/` | Synth tests, script tests and the E2E suites under `tests/e2e/` |
| `redteam/` | promptfoo LLM red-team harness |
| `docs/` | Architecture, security, guardrails, session storage, upgrade notes and the reference pages moved out of this README |
| `specs/` | Implementation specs for the guardrails, proxy and red-team work |

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
