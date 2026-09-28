# OpenClaw on AWS Bedrock AgentCore

[![License: MIT-0](https://img.shields.io/badge/License-MIT--0-blue.svg)](LICENSE)
[![Status: Experimental](https://img.shields.io/badge/Status-Experimental-orange.svg)]()
[![AWS CDK](https://img.shields.io/badge/AWS%20CDK-v2-yellow.svg)]()

> **Experimental** — This project is provided for experimentation and learning purposes only. It is **not intended for production use**. APIs, architecture, and configuration may change without notice.

Deploy an AI-powered multi-channel messaging bot (Telegram, Slack, Feishu) on AWS Bedrock AgentCore Runtime using CDK. OpenClaw runs as **per-user serverless containers** on AgentCore Runtime. A Router Lambda handles webhook ingestion from Telegram, Slack and Feishu, resolves user identity via DynamoDB, and invokes per-user AgentCore sessions. Each user gets their own microVM with workspace persistence: the OpenClaw state dir (`~/.openclaw/`, SQLite session state plus the agent workspace) lives on the container's local disk, is mirrored to AgentCore session storage while the session runs, and is backed up to S3. The agent has built-in tools (web, filesystem, runtime, sessions, automation), custom skills for file storage and cron scheduling, and **EventBridge-based cron scheduling** for recurring tasks.

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
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
- Multi-channel support (Telegram, Slack, Feishu) with cross-channel account linking
- Multimodal: text + image messages via Bedrock ConverseStream
- STS session-scoped credentials (per-user S3, DynamoDB, Secrets Manager isolation)
- Custom skills: S3 file storage, EventBridge cron scheduling, API key management, ClawHub skill installer
- Headless browser (optional, AgentCore Browser API)
- AWS Bedrock Guardrails — content filtering, PII redaction, topic denial, word filters, prompt attack detection
- LLM red team testing — 62 test cases across 12 attack categories via promptfoo
- App-level security E2E tests (TestGuardrailSecurity — 6 tests through the full Telegram webhook pipeline)

Users can send **text and images** — photos sent via Telegram, Slack or Feishu are downloaded by the Router Lambda, stored in S3, and passed to Claude as multimodal content via Bedrock's ConverseStream API. Supported formats: JPEG, PNG, GIF, WebP (max 3.75 MB).

## Architecture

![Architecture: users message Telegram, Slack or Feishu; webhooks reach API Gateway and the Router Lambda, which resolves the user in DynamoDB and invokes OpenClaw 2.0 on a per-user Bedrock AgentCore Runtime microVM; the runtime calls Amazon Bedrock (Claude with Guardrails), keeps state in S3, reads Secrets Manager and Cognito/STS scoped credentials, drives an AgentCore Browser in the VPC, and schedules tasks through EventBridge Scheduler and a Cron Lambda; Bedrock invocation logs feed CloudWatch token monitoring](docs/images/architecture.svg)

The diagram shows the high-level request path. Container internals (contract server, lightweight agent, Bedrock proxy, session storage), KMS and networking are described in the [component table](docs/how-it-works.md#components) and in [docs/architecture-detailed.md](docs/architecture-detailed.md).

Messages from a channel reach API Gateway and the Router Lambda, which validates the webhook, resolves the user in DynamoDB and calls `InvokeAgentRuntime` with a per-user session id. Inside the user's microVM the contract server answers immediately through the lightweight agent while the OpenClaw gateway boots, then bridges every later message to OpenClaw over WebSocket. Both paths call Bedrock through the local proxy. OpenClaw state lives on local disk, is mirrored to session storage and snapshotted to S3. Scheduled tasks and token monitoring run on their own Lambdas. The AgentCore Browser (`enable_browser`, on by default) is a `CfnBrowserCustom` in the VPC private subnets: the contract server starts a per-user browser session and the `agentcore-browser` skill drives it over CDP to navigate, screenshot and interact with pages; see [Browser Support](docs/how-it-works.md#browser-support-optional). The dashed *Optional: enable_gateway* group is the opt-in `enable_gateway` flag: OpenClaw also calls an AgentCore Gateway over MCP (per-user Cognito access token) whose Lambda targets serve the user-files and schedule tools against the same S3 bucket and EventBridge Scheduler; see [AgentCore Gateway MCP tools](docs/how-it-works.md#agentcore-gateway-mcp-tools-prototype).

The AgentCore Runtime, its endpoint and the ECR repository are created by the **AgentCore Starter Toolkit** (`agentcore deploy`) in Phase 2 of `scripts/deploy.sh`, not by CDK. `OpenClawAgentCore` provides the execution role, security group and bucket, and the Phase 3 stacks read `runtime_id`/`runtime_endpoint_id` from `cdk.json` context.

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

## Prerequisites

- **AWS Account** with Bedrock access
- **AWS CLI** v2 configured with credentials (`aws sts get-caller-identity` should succeed)
- **Node.js** >= 18 (for CDK CLI)
- **Python** >= 3.11 (for CDK app)
- **Docker** (for building the bridge container image; ARM64 support via Docker Desktop or buildx). Not required if using `BUILD_MODE=codebuild`
- **AWS CDK** v2 (`npm install -g aws-cdk`)
- **AgentCore Starter Toolkit** (`pip install bedrock-agentcore-starter-toolkit`)
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

### 4. Install the AgentCore Starter Toolkit

The project uses a **hybrid deployment model**: CDK manages infrastructure (VPC, Lambda, DynamoDB, S3, etc.) while the AgentCore Starter Toolkit manages the Runtime (container image, ECR, lifecycle config).

```bash
pip install bedrock-agentcore-starter-toolkit
```

> After installing, ensure `agentcore` is in your PATH (`which agentcore` should succeed). On some systems, pip installs to `~/.local/bin` which may not be in PATH — add it with `export PATH="$HOME/.local/bin:$PATH"`.

### 5. Deploy

```bash
cdk synth          # validate (runs cdk-nag security checks)
./scripts/deploy.sh
```

The deploy script runs three phases automatically:
1. **Phase 1 (CDK)** — VPC, Security, AgentCore base, Observability stacks
2. **Phase 2 (Starter Toolkit)** — Reads CDK outputs, auto-generates `.bedrock_agentcore.yaml`, builds ARM64 container image, deploys AgentCore Runtime
3. **Phase 3 (CDK)** — Router, Cron, TokenMonitoring stacks (depend on Runtime ID from Phase 2)

The script runs pre-flight checks (AWS credentials, CDK CLI, Docker, agentcore CLI) before starting.

With the opt-in `enable_gateway: true` in `cdk.json`, Phase 1 also deploys `OpenClawGateway` and Phase 2 reads its `GatewayUrl` output (exact `OutputKey`; the deploy fails if it is empty) and passes it to the runtime as `AGENTCORE_GATEWAY_URL`. With the default `false` the stack is not in the app and the variable is not set. See [AgentCore Gateway MCP tools](docs/how-it-works.md#agentcore-gateway-mcp-tools-prototype).

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
4. Redeploy: `cdk destroy OpenClawVpc --force && ./scripts/deploy.sh`

#### Build modes

By default, the container image is built **locally** with Docker (`--local-build`). If you don't have Docker or prefer cloud builds, set `BUILD_MODE=codebuild`:

| Mode | Command | Requires | Notes |
|------|---------|----------|-------|
| **local-build** (default) | `./scripts/deploy.sh` | Docker | Builds ARM64 image locally. On x86 hosts, uses QEMU emulation via Docker buildx |
| **codebuild** | `BUILD_MODE=codebuild ./scripts/deploy.sh` | — | Builds in AWS CodeBuild (no Docker needed, adds ~2 min + CodeBuild cost) |

#### Running individual phases

```bash
./scripts/deploy.sh --phase1         # CDK foundation only
./scripts/deploy.sh --runtime-only   # Starter Toolkit only (Phase 2)
./scripts/deploy.sh --phase3         # CDK dependent stacks only
./scripts/deploy.sh --cdk-only       # CDK stacks only (skip toolkit)
```

> **Note:** `.bedrock_agentcore.yaml` is auto-generated by `deploy.sh` from CDK CloudFormation outputs. It contains account-specific values and is gitignored — do not commit it.

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

Send a message to your Telegram bot. The first message triggers a cold start — the lightweight agent responds first (23 s webhook-to-reply measured on us-west-2 staging, with file storage and scheduling support) while OpenClaw initializes in the background (first full-OpenClaw reply ~70 s after the webhook in the same measurement). After OpenClaw is ready, the full feature set is available. Subsequent messages in the same session are fast.

## Configuration

All tunable parameters are in `cdk.json`:

| Parameter | Default | Description |
|---|---|---|
| `account` | (empty) | AWS account ID. Falls back to `CDK_DEFAULT_ACCOUNT` env var |
| `region` | (empty) | AWS region. Falls back to `CDK_DEFAULT_REGION` env var |
| `availability_zones` | `[]` | Optional list of AZ names to use for VPC. Set this only if AgentCore Runtime has AZ restrictions in your region. See deployment notes above |
| `default_model_id` | `global.anthropic.claude-sonnet-4-6` | Bedrock model ID. The `global.` prefix routes to any available region automatically |
| `subagent_model_id` | (empty) | Bedrock model ID for sub-agents. Empty = use `default_model_id`. Set to e.g. `global.anthropic.claude-sonnet-4-6-v1` for faster/cheaper sub-agents |
| `cloudwatch_log_retention_days` | `30` | Log retention in days |
| `daily_token_budget` | `1000000` | Daily token budget alarm threshold |
| `daily_cost_budget_usd` | `5` | Daily cost budget alarm threshold (USD) |
| `session_idle_timeout` | `1800` | Per-user session idle timeout (seconds) |
| `session_max_lifetime` | `28800` | Per-user session max lifetime (seconds) |
| `workspace_sync_interval_seconds` | `300` | .openclaw/ S3 sync interval |
| `workspace_restore_wait_seconds` | `180` | How long the bridge waits for the S3 `.openclaw/` restore before starting the OpenClaw gateway (passed as `WORKSPACE_RESTORE_WAIT_MS`). The wait ends as soon as the restore finishes; a large state (~1,200 files / 220 MB) restores in ~100-115 s, so keep this above that |
| `router_lambda_timeout_seconds` | `600` | Router Lambda timeout |
| `router_lambda_memory_mb` | `256` | Router Lambda memory |
| `registration_open` | `false` | If `true`, anyone can message the bot. If `false`, only allowlisted users can register |
| `token_ttl_days` | `90` | DynamoDB token usage record TTL |
| `image_version` | (see `cdk.json`) | Bridge container version tag. Bump to force container redeploy |
| `user_files_ttl_days` | `365` | S3 per-user file expiration |
| `user_files_noncurrent_days` | `30` | Days after which noncurrent S3 object versions expire (CDK context key, not set in `cdk.json`; add it there or pass `-c`) |
| `user_files_noncurrent_keep` | `3` | Newest noncurrent versions always kept per key (CDK context key, not set in `cdk.json`; add it there or pass `-c`) |
| `cron_lambda_timeout_seconds` | `900` | Cron executor Lambda timeout (must exceed warmup time) |
| `cron_lambda_memory_mb` | `256` | Cron executor Lambda memory |
| `enable_cloudtrail` | `false` | Deploy a dedicated CloudTrail trail. Off by default — most accounts already have one. Enabling creates an S3 bucket + trail (additional cost) |
| `cron_lead_time_minutes` | `5` | Minutes before schedule time to start warmup |
| `enable_guardrails` | `true` | Deploy Bedrock Guardrails for content filtering. Set `false` to disable (reduces safety but saves cost) |
| `guardrails_content_filter_level` | `HIGH` | Not read by the stack: filter strengths are set per category in `stacks/guardrails_stack.py` (`PROMPT_ATTACK` input `HIGH`, `INSULTS` input `MEDIUM`, the rest `HIGH`) |
| `guardrails_pii_action` | `ANONYMIZE` | PII handling: `ANONYMIZE` (redact) or `BLOCK` (reject). Credit cards always BLOCK regardless |
| `enable_browser` | `true` | Deploy an AgentCore Browser (`CfnBrowserCustom`) and pass its id to the runtime as `BROWSER_IDENTIFIER`. Only deployed in regions listed in `BROWSER_SUPPORTED_REGIONS` (`stacks/agentcore_stack.py`) |
| `enable_gateway` | `false` | Prototype: deploy `OpenClawGateway` (AgentCore Gateway serving the per-user file and schedule tools as MCP tools) and pass `AGENTCORE_GATEWAY_URL` to the runtime. See [docs/gateway-mcp-tools.md](docs/gateway-mcp-tools.md) |
| `anomaly_band_width` | `2` | Standard-deviation band for the token-usage anomaly detector alarm |
| `runtime_id` / `runtime_endpoint_id` | (written by `deploy.sh`) | AgentCore Runtime id and endpoint id from the Starter Toolkit. Phase 3 stacks read them from here; do not edit by hand |

> **Guardrails cost**: Bedrock Guardrails are enabled by default and add ~$0.75 per 1,000 text units on top of model inference costs. To disable, set `"enable_guardrails": false` in `cdk.json`. See [AWS Bedrock Guardrails Pricing](https://aws.amazon.com/bedrock/pricing/#Guardrails). Disabling removes content-level protections but other security layers (STS scoping, tool deny list, SSRF protection) remain active.

## Channel Setup

Telegram is the quick-start channel. Slack and Feishu setup is in [docs/channels.md](docs/channels.md).

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

## Operations

Operator commands are in [docs/operations.md](docs/operations.md):

- [Check runtime status](docs/operations.md#check-runtime-status)
- [Check DynamoDB identity table](docs/operations.md#check-dynamodb-identity-table)
- [Deploy new bridge version](docs/operations.md#deploy-new-bridge-version)
- [Run tests](docs/operations.md#run-tests)
- [Security validation](docs/operations.md#security-validation)
- Run one-off shell commands inside a live session with `scripts/agentcore-exec.py` (operator-only, full execution-role credentials): [docs/execute-command.md](docs/execute-command.md)

## Upgrading from OpenClaw 2026.3.x

This branch moves the image from OpenClaw 2026.3.8 / Node 22 to **OpenClaw 2026.9.5 ("2.0") / Node 24 / clawhub 0.23.3**. Existing deployments upgrade by pushing the new image and bumping `image_version`; per-user state carries over:

- **Legacy sessions**: 2.0 stores sessions in per-agent SQLite and refuses readiness while a pre-2.0 `agents/<id>/sessions/sessions.json` is present. The contract runs `openclaw doctor --fix --non-interactive` once before the gateway spawns (bounded by `OPENCLAW_MIGRATION_TIMEOUT_MS`) to import it and records a receipt so later cold starts (which restore the 1.x index from S3 again) skip the import; an unreadable index is moved aside so the gateway still starts. The import runs after the S3 restore, so the first 2.0 boot of a large 1.x user waits for both: on staging a state dir of 1,000+ files took 80-115 s to restore and the import of ~1,000 sessions took ~50 s. `deploy.sh` sets the restore wait from `workspace_restore_wait_seconds` (180 s default); if you set `WORKSPACE_RESTORE_WAIT_MS` yourself, or run the image without `deploy.sh` (bridge fallback 45 s), raise it to about 180 s for such users, otherwise the gateway starts on a partly restored state dir. The imported store can be hundreds of MB; SQLite databases over 10 MB are backed up as streamed gzip multipart objects, at most every 10 min (`WORKSPACE_SYNC_LARGE_SQLITE_MIN_INTERVAL_MS`), up to `WORKSPACE_SYNC_MAX_SQLITE_BYTES` (1 GiB) — see [docs/session-storage.md](docs/session-storage.md).
- **Behaviour-preserving config knobs** written into `openclaw.json` so users see no change: `session.reset: { mode: "daily", atHour: 4 }` (2.0 stopped resetting daily); the new `tools.profile: "full"` tools that need a Control UI or a human answer (`terminal`, `process`, `plugins`, `ask_user`, `secrets`, `screen`, `progress_card`, `nodes`, `heartbeat_respond`, media generation) added to `tools.deny`; `skills.workshop.autonomous.mode: "off"` (autonomous Skill Workshop); Active Memory cross-conversation recall and grounded dreaming disabled (`memory.search.rememberAcrossConversations: false`, `plugins.entries["active-memory"].enabled: false`, `plugins.entries["memory-core"].config.dreaming.enabled: false`).
- **WebSocket protocol 4** with `client.id: "gateway-client"`, `client.mode: "backend"` and no `Origin` header (see [Implementation Notes](docs/how-it-works.md#implementation-notes)).
- **State on local disk**, mirrored to session storage, because the mount has neither SQLite locks nor hard links (see [Session Storage](docs/how-it-works.md#session-storage-persistent-filesystem)).

Full details, risks and the validation checklist: [docs/openclaw-2.0-upgrade.md](docs/openclaw-2.0-upgrade.md).

## Troubleshooting

### Container fails health check (RuntimeClientError: health check timed out)

The AgentCore contract server on port 8080 must start within seconds. If `entrypoint.sh` does slow operations (like Secrets Manager calls) before starting the contract server, the health check will time out. The contract server is started as step 1 to avoid this.

### First message is slow (~4 minutes for full OpenClaw)

This is expected for full OpenClaw initialization. However, the **lightweight agent shim** responds to the first message first (23 s webhook-to-reply measured on us-west-2 staging) with support for file storage and cron scheduling tools. OpenClaw initializes in the background (~70 s to the first full-OpenClaw reply in the same measurement, most of it the bounded S3 restore wait) and takes over once ready. The Router Lambda sends a typing indicator to Telegram while waiting, and after 30 seconds sends a progress message ("Working on your request...") to both Telegram and Slack so users know the bot is still working. Subsequent messages in the same session are fast.

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
- **Cross-region inference**: The default model ID `global.anthropic.claude-sonnet-4-6` uses a global cross-region inference profile that routes to any available region. The IAM policy uses `arn:aws:bedrock:*::foundation-model/*` and `arn:aws:bedrock:{region}:{account}:inference-profile/*` to allow all regions.

### Node.js ETIMEDOUT / ENETUNREACH in VPC

Node.js's Happy Eyeballs (`autoSelectFamily`, Node 20+) tries both IPv4 and IPv6. In VPCs without IPv6, this causes connection failures. The `force-ipv4.js` script patches `dns.lookup()` to force IPv4 only, loaded via `NODE_OPTIONS`.

### Deployment gotchas

- **ARM64 required**: AgentCore Runtime runs ARM64 containers. Build with `--platform linux/arm64`.
- **Push image after CDK deploy**: The CDK AgentCore stack creates the ECR repository. Do **not** manually create it beforehand (causes a `Resource already exists` error). Deploy CDK first, then push the image. AgentCore only pulls the image when a user session starts, not at deploy time.
- **AgentCore resource names**: Must match `^[a-zA-Z][a-zA-Z0-9_]{0,47}$` — use underscores, not hyphens.
- **VPC endpoints**: The `bedrock-agentcore-runtime` VPC endpoint is not created by `stacks/vpc_stack.py` (the service is not available in every region). Runtime API calls from the Lambdas go out via NAT; the Bedrock Runtime endpoint is created with private DNS disabled so `global.*` inference profiles can route cross-region.
- **CDK RetentionDays**: `logs.RetentionDays` is an enum, not constructable from int. Use the helper in `stacks/__init__.py`.
- **ClawHub `--no-input --force` at image build**: still required with clawhub 0.23.3 for non-interactive Docker builds of the five pinned, reviewed skills (`--no-input` disables prompts; `--force` overrides the VirusTotal flag some skills carry for calling external APIs). Verified in the us-west-2 staging CodeBuild log: all five skills print `Installed <slug> v<version>`.
- **Image version bumps are required**: After pushing a new bridge container image, you must bump `image_version` in `cdk.json` and redeploy `OpenClawAgentCore`. AgentCore caches images by digest and only re-pulls when the runtime endpoint configuration changes. Without the bump, existing sessions continue using the old image.
- **agentcore CLI urllib3 warnings**: The `agentcore` CLI may emit a `RequestsDependencyWarning` to stdout before its JSON output. This is benign — `deploy.sh` handles mixed output gracefully.

## Known Limitations

| Limitation | Details |
|---|---|
| **Cold start time** | Lightweight agent answers first (23 s webhook-to-reply measured on us-west-2 staging, most of it Lambda + Bedrock); the 2.0 gateway is ready 2.6 s after spawn, but spawn waits up to `WORKSPACE_RESTORE_WAIT_MS` (180 s via `deploy.sh`; 45 s bridge fallback) for the S3 restore, so the first full-OpenClaw reply arrived ~70 s after the webhook |
| **Image size** | Max 3.75 MB per image (Bedrock Converse API limit). The Router Lambda checks this before uploading to S3 |
| **Session timeout** | Sessions terminate after 30 min idle (configurable via `session_idle_timeout`) |
| **ClawHub skills** | 5 pre-installed; available only after full OpenClaw startup. During warm-up, built-in web_fetch/web_search tools are available |
| **Single region** | AgentCore Runtime deployed in one region; no multi-region failover |
| **No voice/video** | Only text and images supported; no audio or video messages |

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

The guardrail is applied only when the runtime role has `bedrock:ApplyGuardrail` and `scripts/deploy.sh` has set `BEDROCK_GUARDRAIL_ID`/`BEDROCK_GUARDRAIL_VERSION` on the runtime; `tests/e2e/test_guardrail_wiring.py` passes only on a logged guardrail intervention. Details and test commands: [docs/guardrails.md](docs/guardrails.md#wiring-and-end-to-end-tests).

#### Guardrail prompt eval

`scripts/guardrail-eval.py` sends the prompts in `tests/fixtures/guardrail_prompts.json` to a guardrail version through `ApplyGuardrail` (input side, no model call) and prints which policy fired for each one. It exits 1 when a verdict differs from the fixture's `expected` value, so you can compare two guardrail versions or check a new scheduled-task prompt before it reaches production. See [docs/guardrails.md](docs/guardrails.md#evaluating-prompts-against-a-guardrail-version).

### Guardrails known limitations

- **Scheduled-task prompts can be blocked.** `PROMPT_ATTACK` runs at input strength `HIGH`. At that strength ordinary scheduled-brief prompts, such as an instruction to send the brief to the user on a named channel or persona-style rules ("you are my assistant, always ..."), can be classified as prompt attacks and the task gets the guardrail's blocked message instead of a reply. A digest about cryptocurrency prices can also match the `CryptoScams` denied topic. Before enabling guardrails for existing users, run `scripts/guardrail-eval.py` against a copy of your own cron prompts (with the `[Scheduled task: <name>]` prefix the Cron Lambda adds) and adjust the prompts or `stacks/guardrails_stack.py`.
- **The runtime is pinned to a numbered version.** `deploy.sh` passes the `GuardrailVersion` output of `OpenClawGuardrails` (the `CfnGuardrailVersion` resource) to the runtime as `BEDROCK_GUARDRAIL_VERSION`. Editing the policies in `stacks/guardrails_stack.py` and deploying updates only the guardrail's working draft; CloudFormation publishes a new version only when the `CfnGuardrailVersion` resource itself changes (for example its `description`). The runtime keeps the old version until a new version is published and the runtime is updated with `./scripts/deploy.sh --runtime-only`. See [docs/guardrails.md](docs/guardrails.md#updating-guardrail-policies).

## Cleanup

```bash
cdk destroy --all
```

Note: KMS keys and the Cognito User Pool have `RETAIN` removal policies and will not be deleted automatically. Remove them manually if needed. `OpenClawGateway` is only part of the app (and of `--all`) while `enable_gateway` is `true` in `cdk.json`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to report issues and send pull requests, and [docs/operations.md](docs/operations.md#run-tests) for the unit and E2E test commands.

### Project structure

Full file tree: [docs/project-structure.md](docs/project-structure.md).

| Directory | Contents |
|---|---|
| `stacks/` | CDK stacks (VPC, security, guardrails, AgentCore, router, observability, token monitoring, cron, opt-in gateway); entry point `app.py`, settings `cdk.json` |
| `bridge/` | Container image: contract server, lightweight agent, Bedrock proxy, workspace sync, custom skills, unit tests |
| `lambda/` | Router, cron executor, token metrics and Gateway tool Lambdas, with their unit tests |
| `scripts/` | `deploy.sh`, channel setup, allowlist management, operator exec CLI, guardrail prompt eval, cron record repair |
| `tests/` | Synth tests, script tests and the E2E suites under `tests/e2e/` |
| `redteam/` | promptfoo LLM red-team harness |
| `docs/` | Architecture, security, guardrails, session storage, upgrade notes and the reference pages moved out of this README |
| `specs/` | Implementation specs for the guardrails, proxy and red-team work |

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
