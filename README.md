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
- [Security Testing](#security-testing)
- [License](#license)

OpenClaw runs as **per-user serverless containers** on AgentCore Runtime. A Router Lambda handles webhook ingestion from Telegram and Slack, resolves user identity via DynamoDB, and invokes per-user AgentCore sessions. Each user gets their own microVM with workspace persistence (`.openclaw/` directory synced to S3). The agent has built-in tools (web, filesystem, runtime, sessions, automation), custom skills for file storage and cron scheduling, and **EventBridge-based cron scheduling** for recurring tasks.

Users can send **text and images** — photos sent via Telegram or Slack are downloaded by the Router Lambda, stored in S3, and passed to the configured Bedrock model as multimodal content via Bedrock's ConverseStream API. Supported formats: JPEG, PNG, GIF, WebP (max 3.75 MB).

### Features

- Per-user Firecracker microVM isolation (AgentCore Runtime)
- Multi-channel support (Telegram, Slack) with cross-channel account linking
- Multimodal: text + image messages via Bedrock ConverseStream
- STS session-scoped credentials (per-user S3, DynamoDB, Secrets Manager isolation)
- Custom skills: S3 file storage, EventBridge cron scheduling, API key management, ClawHub skill installer
- Headless browser (optional, AgentCore Browser API)
- AWS Bedrock Guardrails — content filtering, PII redaction, topic denial, word filters, prompt attack detection
- LLM red team testing — 62 test cases across 12 attack categories via promptfoo
- App-level security E2E tests (TestGuardrailSecurity — 6 tests through the full Telegram webhook pipeline)

## Architecture

### Shared logical flow

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
        BEDROCK[Amazon Bedrock<br/>Kimi]
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

**How it works:** Messages from Telegram/Slack hit the Router Lambda, which resolves user identity and routes to a per-user AgentCore container. Each user gets isolated compute, persistent workspace, and access to the configured Bedrock model.

### `environment_suffix == "dev"` architecture (`PUBLIC` network mode)

```mermaid
flowchart LR
    TG[Telegram / Slack] --> APIGW[API Gateway]
    APIGW --> ROUTER[Router Lambda]
    ROUTER --> DDB[(DynamoDB)]
    ROUTER --> AGENT[AgentCore Runtime<br/>Public network mode]
    AGENT --> BEDROCK[Bedrock]
    AGENT --> S3[S3 user files]
    AGENT --> SM[Secrets Manager]
    AGENT --> CW[CloudWatch]
```

### `environment_suffix != "dev"` architecture (`VPC` network mode)

```mermaid
flowchart LR
    TG[Telegram / Slack] --> APIGW[API Gateway]
    APIGW --> ROUTER[Router Lambda]
    ROUTER --> DDB[(DynamoDB)]

    subgraph VPC[VPC]
        subgraph Private[Private subnets]
            AGENT[AgentCore Runtime<br/>VPC mode]
        end
        VPCE[VPC endpoints<br/>S3, Secrets Manager,<br/>ECR, CloudWatch, Bedrock]
    end

    ROUTER --> AGENT
    AGENT --> VPCE
    VPCE --> AWS[AWS services]
```

See [docs/architecture-detailed.md](docs/architecture-detailed.md) for technical details (sequence diagrams, container internals, data flows).

### Why S3 Workspace Sync?

AgentCore microVMs are ephemeral — they're destroyed when idle. OpenClaw stores conversation history, user profiles, and agent configuration in the `.openclaw/` directory. **S3-backed workspace sync** restores this directory on session start, saves it periodically (every 5 min), and performs a final save on shutdown. Each user's workspace is isolated under a unique S3 prefix derived from their channel identity.

This lets the system behave like a persistent server (continuous conversation history) while benefiting from serverless economics (no idle compute costs).

### Security

This solution applies **defense-in-depth** across network, application, identity, and data layers. Key controls include:

- **Network isolation**: The code treats `environment_suffix == "dev"` as the public-network case. Any other suffix value, including an empty suffix, uses VPC mode for the AgentCore runtime
- **Webhook authentication**: Cryptographic validation (Telegram secret token, Slack HMAC-SHA256 with replay protection)
- **Per-user isolation**: Each user runs in their own AgentCore microVM with dedicated S3 namespace
- **STS session-scoped credentials**: Container assumes its own role with a session policy restricting S3 and DynamoDB to the user's namespace/records — prevents cross-user data access even through shell tools
- **Encryption**: All data encrypted at rest with customer-managed KMS key (S3, DynamoDB, SNS, Secrets Manager) and in transit (TLS)
- **CloudTrail**: Optional dedicated trail (`enable_cloudtrail` in cdk.json). Off by default — most AWS accounts already have an organization or account-level trail. Enabling adds a dedicated S3 bucket + trail for this project's audit logs
- **Least-privilege IAM**: Tightly scoped permissions per component
- **Bedrock Guardrails**: Content filtering on every Bedrock API call — content filters (hate, violence, prompt attacks), topic denial (6 categories), PII redaction, word filters, and custom regex for credential patterns. Opt-out via `enable_guardrails: false` in `cdk.json`
- **Tool hardening**: OpenClaw `read` tool denied to prevent credential access via `/proc` and local file reads; `exec` allowed for skill management (scoped STS credentials limit blast radius); proxy bound to loopback only; security group egress restricted to HTTPS
- **Automated compliance**: cdk-nag AwsSolutions checks on every `cdk synth`

See [docs/security.md](docs/security.md) for the complete security architecture.

**Suffix-based network mode note:** this behavior is currently keyed to the **literal suffix value**. When `environment_suffix == "dev"`, the AgentCore runtime uses public network mode and is **not** attached to the project VPC. The `dev` VPC stack also uses **public subnets only with `nat_gateways = 0`**, so there is **no NAT gateway** in that case. For any other suffix value, including `prod`, `staging`, or an empty suffix, the AgentCore runtime uses **VPC mode**. The `dev` case is still protected by IAM-authenticated runtime access, the Router Lambda entry path, webhook validation, per-user session isolation, scoped AWS credentials, and TLS, but it does **not** have the extra private-subnet and VPC-endpoint isolation used by the VPC-mode case.

| Aspect | `environment_suffix == "dev"` | `environment_suffix != "dev"` |
| --- | --- | --- |
| AgentCore runtime network mode | Public network | VPC mode |
| Attached to project VPC | No | Yes |
| Private subnets | No | Yes |
| NAT gateway | No | Yes |
| VPC endpoints | No | Yes |
| IAM-authenticated runtime access | Yes | Yes |
| Router Lambda + webhook validation | Yes | Yes |
| Per-user isolation + scoped credentials | Yes | Yes |
| TLS to AWS services | Yes | Yes |
| Network isolation strength | Lower | Higher |

## Prerequisites

- **AWS Account** with Bedrock access
- **AWS CLI** v2 configured with credentials (`aws sts get-caller-identity` should succeed)
- **Node.js** >= 18 (for CDK CLI)
- **Python** >= 3.11 (for CDK app)
- **Docker** (for building the bridge container image; ARM64 support via Docker Desktop or buildx). Not required if using `BUILD_MODE=codebuild`
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

### 4. Create your local deployment env file

Copy the template and fill in the values you want to drive deployment with:

```bash
cp .env.template .env
```

At minimum, set your environment suffix in `.env`:

```bash
OPENCLAW_ENV_SUFFIX=dev
CDK_DEFAULT_REGION=us-east-1
```

If you keep multiple env files, the scripts load **one file only**:

- default: `.env`
- override: `OPENCLAW_ENV_FILE=/path/to/file`

Examples:

```bash
OPENCLAW_ENV_FILE=.env.dev ./scripts/deploy.sh
OPENCLAW_ENV_FILE=.env.prod ./scripts/deploy.sh
OPENCLAW_ENV_FILE=.env.prod ./scripts/undeploy.sh --all
```

The scripts do **not** merge `.env` with `.env.prod` or `.env.dev`. The selected file is sourced as-is, and its values become the deployment settings for that run.

If you also set these Telegram values, deployment will bootstrap the bot automatically:

```bash
TELEGRAM_BOT_TOKEN=123456:your-bot-token
TELEGRAM_ADMIN_USER_ID=123456789
```

That makes `./scripts/deploy.sh` do all of the following without a separate setup step:
1. Store the Telegram bot token in Secrets Manager
2. Register the Telegram webhook against the deployed Router URL
3. Add your Telegram account to the DynamoDB allowlist

### 5. Deploy

```bash
cdk synth          # validate (runs cdk-nag security checks)
./scripts/deploy.sh
```

By default, this repository uses **unsuffixed** stack names unless you set `OPENCLAW_ENV_SUFFIX`. If you follow `.env.template` as written, you will deploy the **`dev`** environment and get stack names like `OpenClawVpc-dev`, `OpenClawAgentCore-dev`, and `OpenClawRouter-dev`. You can either set `OPENCLAW_ENV_SUFFIX=prod` inside the selected env file, or override it per run with `OPENCLAW_ENV_SUFFIX=prod ./scripts/deploy.sh`.

The deploy script runs three phases automatically:
1. **Phase 1 (CDK)** — VPC, Security, Guardrails, Observability stacks
2. **Phase 2 (CDK)** — AgentCore runtime stack (container asset, runtime, endpoint, browser, session storage)
3. **Phase 3 (CDK)** — Router, Cron, TokenMonitoring stacks

The script runs pre-flight checks (AWS credentials, CDK CLI, Python venv bootstrap, and Docker when needed) before starting.

**Bedrock invocation logging is shared per AWS account + region.** It is not isolated by `OPENCLAW_ENV_SUFFIX`. Because of that, exactly one environment in a given account+region should own the Bedrock invocation logging configuration and the CloudWatch Logs subscription filter. Configure that explicitly with:

```bash
MANAGE_BEDROCK_INVOCATION_LOGGING=true
```

Set it in one environment only (typically prod) and leave it `false` elsewhere.

Example:

```bash
# .env.prod
MANAGE_BEDROCK_INVOCATION_LOGGING=true

# .env.dev
MANAGE_BEDROCK_INVOCATION_LOGGING=false
```

**Note on Availability Zones:** Bedrock AgentCore Runtime may not be available in all AZs in a region, and AZ names like `us-east-1a` are account-specific aliases. The better fix is to supply the stable **AZ IDs** that AgentCore reports and let the deploy script resolve them to the correct AZ names for your account before creating the VPC.

Add this to your selected env file:

```bash
AGENTCORE_SUPPORTED_AZ_IDS=use1-az4,use1-az1,use1-az2
```

Or set the same data in `cdk.json`:

```json
{
  "context": {
    "agentcore_supported_availability_zone_ids": ["use1-az4", "use1-az1", "use1-az2"]
  }
}
```

At deploy time, `./scripts/deploy.sh` resolves those stable AZ IDs to this account's AZ names (for example `us-east-1b`, `us-east-1c`) and passes the resolved names into CDK. That means the **VPC is created to match AgentCore's supported subnets**, instead of relying on CDK's default AZ ordering.

If you need a manual override, you can still set `availability_zones` directly in `cdk.json`:

```json
{
  "context": {
    "availability_zones": ["us-east-1b", "us-east-1c"]
  }
}
```

To recover from an AZ mismatch:
1. Check the AgentCore error message for the supported AZ IDs (for example `use1-az4,use1-az1,use1-az2`)
2. Put those IDs into `AGENTCORE_SUPPORTED_AZ_IDS` in your selected env file
3. Redeploy with `./scripts/deploy.sh`
4. If the VPC was already created in unsupported AZs, destroy the VPC stack first, then deploy again

#### Build modes

By default, the deploy script chooses the best supported build path automatically. If Docker buildx can build `linux/arm64`, it uses a local Docker asset build. Otherwise it falls back to CDK's CodeBuild-backed asset publishing.

| Mode | Command | Requires | Notes |
|------|---------|----------|-------|
| **auto** (default) | `./scripts/deploy.sh` | Docker *or* CodeBuild bootstrap | Uses local Docker buildx for `linux/arm64` when available; otherwise uses CodeBuild |
| **local-build** | `BUILD_MODE=local-build ./scripts/deploy.sh` | Docker buildx with `linux/arm64` support | Builds the AgentCore image locally and publishes it via CDK assets |
| **codebuild** | `BUILD_MODE=codebuild ./scripts/deploy.sh` | CDK bootstrap with CodeBuild asset publishing support | Builds and publishes the image through AWS CodeBuild |

#### Running individual phases

```bash
./scripts/deploy.sh --phase1         # CDK foundation only
./scripts/deploy.sh --runtime-only   # AgentCore runtime stack only (Phase 2)
./scripts/deploy.sh --phase3         # CDK dependent stacks only
./scripts/deploy.sh --cdk-only       # all CDK phases only
```

### 6. Optional manual Telegram setup

If you prefer not to put Telegram bootstrap values in `.env`, you can still do the old manual flow after deployment. The setup script now also reads `.env` if present, so you can mix both approaches.

```bash
./scripts/setup-telegram.sh
```

The script will:
1. Register the Telegram webhook with API Gateway (with secret token for request validation)
2. Prompt you for your Telegram user ID unless `TELEGRAM_ADMIN_USER_ID` is already set
3. Add you to the DynamoDB allowlist so you can use the bot immediately

<details>
<summary>Manual setup (if you prefer individual commands)</summary>

```bash
# Get Router API URL
API_URL=$(aws cloudformation describe-stacks \
  --stack-name OpenClawRouter-dev \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text --region $CDK_DEFAULT_REGION)

# Get the webhook secret (used for request validation)
WEBHOOK_SECRET=$(aws secretsmanager get-secret-value \
  --secret-id openclaw/webhook-secret-dev \
  --region $CDK_DEFAULT_REGION --query SecretString --output text)

# Point Telegram to the webhook with secret_token for validation
TELEGRAM_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id openclaw/channels/telegram-dev \
  --region $CDK_DEFAULT_REGION --query SecretString --output text)
curl "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook?url=${API_URL}webhook/telegram&secret_token=${WEBHOOK_SECRET}"

# Add yourself to the allowlist (find your ID via @userinfobot on Telegram)
./scripts/manage-allowlist.sh add telegram:YOUR_TELEGRAM_USER_ID
```

</details>

### 8. Verify

Send a message to your Telegram bot. The first message triggers a cold start — the lightweight agent responds in ~10-15 seconds (with file storage and scheduling support) while OpenClaw initializes in the background (~1-2 minutes). After OpenClaw is ready, the full feature set is available. Subsequent messages in the same session are fast.

## Project Structure

```
openclaw-on-agentcore/
  app.py                          # CDK app entry point (8 stacks)
  cdk.json                        # Configuration (model, budgets, sessions, cron, guardrails)
  requirements.txt                # Python deps (aws-cdk-lib, cdk-nag)
  stacks/
    __init__.py                   # Shared helper (RetentionDays converter)
    vpc_stack.py                  # VPC foundation; `dev` = public-only/no endpoints, other suffixes = private subnets + NAT + VPC endpoints
    security_stack.py             # KMS CMK, Secrets Manager, Cognito, optional CloudTrail
    agentcore_stack.py            # Runtime, WorkloadIdentity, ECR, S3, IAM
    router_stack.py               # Router Lambda + API Gateway HTTP API + DynamoDB identity
    observability_stack.py        # Dashboards, alarms, Bedrock logging
    token_monitoring_stack.py     # Lambda processor, DynamoDB, token analytics
    guardrails_stack.py           # Bedrock Guardrails (content filters, PII, topic denial)
    cron_stack.py                 # EventBridge Scheduler, Cron executor Lambda, IAM
  bridge/
    Dockerfile                    # Container image (node:22-slim, ARM64, clawhub skills)
    entrypoint.sh                 # Startup: configure IPv4, start contract server
    agentcore-contract.js         # AgentCore HTTP contract with hybrid routing (shim + OpenClaw)
    lightweight-agent.js          # Warm-up agent shim (s3-user-files + eventbridge-cron + clawhub-manage tools)
    lightweight-agent.test.js     # Lightweight agent unit tests (node:test, 73 tests)
    agentcore-proxy.js            # OpenAI -> Bedrock ConverseStream adapter + Identity + multimodal images
    image-support.test.js         # Image support unit tests (node:test)
    content-extraction.test.js    # Content block extraction tests (node:test)
    subagent-routing.test.js      # Subagent model routing + detection tests (node:test)
    workspace-sync.js             # .openclaw/ directory S3 sync (restore/save/periodic)
    workspace-sync.test.js        # Workspace sync credential tests (node:test, 7 tests)
    scoped-credentials.js         # Per-user STS session-scoped S3 credentials
    scoped-credentials.test.js    # Scoped credentials unit tests (node:test, 38 tests)
    force-ipv4.js                 # DNS patch for Node.js 22 IPv6 issue
    CLAUDE.md                     # Project instructions (for Claude Code IDE)
    skills/
      s3-user-files/              # Custom per-user file storage skill (S3-backed)
      eventbridge-cron/           # Cron scheduling skill (EventBridge Scheduler)
      clawhub-manage/             # ClawHub skill installer (install/uninstall/list)
      api-keys/                   # Dual-mode API key management (native file + Secrets Manager)
  lambda/
    token_metrics/index.py        # Bedrock log -> DynamoDB + CloudWatch metrics
    router/index.py                    # Webhook router (Telegram + Slack, image uploads)
    router/test_image_upload.py        # Image upload unit tests (pytest)
    router/test_content_extraction.py  # Content block extraction tests (pytest)
    router/test_markdown_html.py       # Markdown-to-HTML conversion tests (pytest)
    cron/index.py                      # Cron executor (warmup, invoke, deliver)
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
      bot_test.py                 # CLI entrypoint + pytest test classes (17 tests)
      conftest.py                 # pytest fixtures, conversation scenarios
  redteam/                        # LLM red team testing (promptfoo, 62 test cases)
  docs/
    architecture.md               # Detailed architecture diagram
    security.md                   # Complete security architecture
    guardrails.md                 # Bedrock Guardrails operational runbook
```

## CDK Stacks

| Stack | Resources | Dependencies |
|---|---|---|
| **OpenClawVpc** | VPC foundation. `environment_suffix == "dev"`: public subnets only, no NAT, no VPC endpoints. Other suffixes: public + private subnets, NAT, VPC endpoints, flow logs | None |
| **OpenClawSecurity** | KMS CMK, Secrets Manager (7 secrets incl. webhook validation), Cognito User Pool, optional CloudTrail | None |
| **OpenClawGuardrails** | CfnGuardrail (content filters, topic denial, PII, word filters, regex), CfnGuardrailVersion | Security |
| **OpenClawAgentCore** | CfnRuntime, CfnRuntimeEndpoint, CfnWorkloadIdentity, ECR, S3 bucket, SG, IAM. Runtime network mode is `PUBLIC` only when `environment_suffix == "dev"`; otherwise it uses VPC mode | Vpc, Security, Guardrails |
| **OpenClawRouter** | Lambda, API Gateway HTTP API (explicit routes, throttling), DynamoDB identity table | AgentCore, Security |
| **OpenClawObservability** | Operations dashboard, alarms (errors, latency, throttles), SNS, Bedrock logging | None |
| **OpenClawTokenMonitoring** | DynamoDB (single-table, 4 GSIs), Lambda processor, analytics dashboard | Observability |
| **OpenClawCron** | EventBridge Scheduler group, Cron executor Lambda, Scheduler IAM role | AgentCore, Router, Security |

Physical stack names are suffixed by `environment_suffix`. For example, with `OPENCLAW_ENV_SUFFIX=dev` they become `OpenClawVpc-dev`, `OpenClawAgentCore-dev`, `OpenClawRouter-dev`, and so on.

## Configuration

All tunable parameters are in `cdk.json`:

| Parameter | Default | Description |
|---|---|---|
| `environment_suffix` | `""` | Environment suffix appended to stack names and fixed physical names so multiple deployments can coexist in one account/region. Set it in `.env` or `OPENCLAW_ENV_SUFFIX` when you want suffixed environments like `dev` or `prod` |
| `reuse_existing_user_files_bucket` | `false` | Reuse an existing user-files S3 bucket instead of creating it. If left unset, the AgentCore stack now uses boto3 `head_bucket()` during synth to auto-import a retained bucket with the expected name when it already exists |
| `manage_bedrock_invocation_logging` | `false` | Whether this environment owns the shared Bedrock model invocation logging configuration and CloudWatch Logs subscription. Set this to `true` in exactly one environment per AWS account+region |
| `account` | (empty) | AWS account ID. Falls back to `CDK_DEFAULT_ACCOUNT` env var |
| `region` | `""` | AWS region. Falls back to `CDK_DEFAULT_REGION` env var |
| `availability_zones` | `["us-east-1b", "us-east-1c"]` | Optional list of AZ names to use for VPC. Set this only if AgentCore Runtime has AZ restrictions in your region. See deployment notes above |
| `default_model_id` | `moonshotai.kimi-k2.5` | Bedrock model ID used by the main agent runtime |
| `subagent_model_id` | (empty) | Bedrock model ID for sub-agents. Empty = use `default_model_id`. Set it explicitly only if you want sub-agents on a different model |
| `cloudwatch_log_retention_days` | `30` | Log retention in days |
| `daily_token_budget` | `1000000` | Daily token budget alarm threshold |
| `daily_cost_budget_usd` | `5` | Daily cost budget alarm threshold (USD) |
| `session_idle_timeout` | `1800` | Per-user session idle timeout (seconds) |
| `session_max_lifetime` | `28800` | Per-user session max lifetime (seconds) |
| `workspace_sync_interval_seconds` | `300` | .openclaw/ S3 sync interval |
| `router_lambda_timeout_seconds` | `600` | Router Lambda timeout |
| `router_lambda_memory_mb` | `256` | Router Lambda memory |
| `registration_open` | `false` | If `true`, anyone can message the bot. If `false`, only allowlisted users can register |
| `token_ttl_days` | `90` | DynamoDB token usage record TTL |
| `image_version` | `"70"` | Bridge container version tag. Bump to force container redeploy |
| `user_files_ttl_days` | `365` | S3 per-user file expiration |
| `cron_lambda_timeout_seconds` | `600` | Cron executor Lambda timeout (must exceed warmup time) |
| `cron_lambda_memory_mb` | `256` | Cron executor Lambda memory |
| `enable_cloudtrail` | `false` | Deploy a dedicated CloudTrail trail. Off by default — most accounts already have one. Enabling creates an S3 bucket + trail (additional cost) |
| `cron_lead_time_minutes` | `5` | Minutes before schedule time to start warmup |
| `enable_guardrails` | `true` | Deploy Bedrock Guardrails for content filtering. Set `false` to disable (reduces safety but saves cost) |
| `guardrails_content_filter_level` | `HIGH` | Content filter strength for all categories: `LOW`, `MEDIUM`, or `HIGH` |
| `guardrails_pii_action` | `ANONYMIZE` | PII handling: `ANONYMIZE` (redact) or `BLOCK` (reject). Credit cards always BLOCK regardless |
| `enable_browser` | `false` | Enable headless Chromium browser inside the container. CDK creates the browser resource and wires `BROWSER_IDENTIFIER` automatically |

Set `environment_suffix` in `cdk.json` or override it per run with `OPENCLAW_ENV_SUFFIX`, for example `OPENCLAW_ENV_SUFFIX=prod ./scripts/deploy.sh`. The deploy, undeploy, setup, and E2E helper scripts derive the same suffixed stack names, secret IDs, and DynamoDB table names automatically.

Network mode is currently keyed to the literal suffix in code: **`environment_suffix == "dev"` uses AgentCore public network mode**. Any other suffix value, including an empty suffix, uses **VPC mode** for the AgentCore runtime.

### Selecting the env file

All helper scripts (`deploy.sh`, `undeploy.sh`, `setup-telegram.sh`, `setup-slack.sh`, `setup-feishu.sh`, `manage-allowlist.sh`, and `e2e-deploy-and-test.sh`) use the same env-file loader:

- If `OPENCLAW_ENV_FILE` is set, that file is loaded
- Otherwise, the script loads `.env` from the repository root

Examples:

```bash
./scripts/deploy.sh                             # loads ./.env
OPENCLAW_ENV_FILE=.env.dev ./scripts/deploy.sh
OPENCLAW_ENV_FILE=.env.prod ./scripts/deploy.sh
OPENCLAW_ENV_FILE=.env.prod ./scripts/undeploy.sh --all
```

Recommended pattern:

- `.env.dev` for development
- `.env.prod` for production-like deployment
- `.env` only if you want one default local environment

The scripts do **not** layer multiple env files together. Pick exactly one file per run.

> **Guardrails cost**: Bedrock Guardrails are enabled by default and add ~$0.75 per 1,000 text units on top of model inference costs. To disable, set `"enable_guardrails": false` in `cdk.json`. See [AWS Bedrock Guardrails Pricing](https://aws.amazon.com/bedrock/pricing/#Guardrails). Disabling removes content-level protections but other security layers (STS scoping, tool deny list, SSRF protection) remain active.

## Channel Setup

### Telegram

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Create a new bot with `/newbot`
3. Copy the bot token
4. Store it in Secrets Manager:
   ```bash
   aws secretsmanager update-secret \
     --secret-id openclaw/channels/telegram-dev \
     --secret-string 'YOUR_BOT_TOKEN' \
     --region $CDK_DEFAULT_REGION
   ```
   With the default `dev` environment, the secret ID is `openclaw/channels/telegram-dev`.
5. Set up the webhook (see Quick Start step 7)

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
      --stack-name OpenClawRouter-dev \
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
      --secret-id openclaw/channels/slack-dev \
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
   - Creates STS scoped credentials restricting S3 to the user's namespace prefix
   - Starts the Bedrock proxy with `USER_ID`/`CHANNEL` env vars
   - Starts OpenClaw gateway with scoped credentials (container credentials stripped)
   - Restores `.openclaw/` workspace from S3 (background)
   - Starts credential refresh timer (45 min interval)
   - Waits for proxy only (~5s), then the **lightweight agent** handles the message immediately
3. **Lightweight agent** (warm-up phase, ~5s to ~1-2min) runs an agentic loop with 17 tools: `web_fetch`, `web_search`, S3 file storage (read/write/list/delete), EventBridge cron scheduling (create/list/update/delete), ClawHub skill management (install/uninstall/list), and API key management (native CRUD, Secrets Manager CRUD, unified retrieval, migration). Web tools include SSRF prevention (IP blocklists, DNS rebinding mitigation). All responses include a deterministic warm-up footer
4. **WebSocket bridge** (after OpenClaw ready, ~1-2min) takes over — messages route to OpenClaw which provides full tool profile, 5 ClawHub skills, and sub-agent support. Responses no longer have the warm-up footer
5. **Router Lambda** sends the response back to the channel (Telegram/Slack API). While waiting, it sends typing indicators (Telegram) and a one-time progress message after 30s (both channels) for long-running requests

When the session idles (default 30 min), AgentCore terminates the microVM. Before shutdown, the SIGTERM handler saves `.openclaw/` to S3. The next message creates a fresh microVM and restores the workspace.

### Image Uploads

Users can send photos alongside text messages. The system supports JPEG, PNG, GIF, and WebP images up to 3.75 MB (the Bedrock Converse API limit).

**How it works:**

1. **Router Lambda** detects an image in the incoming webhook (Telegram `photo` array or `document` with image MIME type; Slack `files` with image MIME type)
2. **Router Lambda** downloads the image from the channel API (Telegram `getFile` endpoint; Slack `url_private_download` with Bearer auth) and uploads it to S3 under `{namespace}/_uploads/img_{timestamp}_{hex}.{ext}`
3. The message payload sent to AgentCore becomes a structured object: `{"text": "caption text", "images": [{"s3Key": "...", "contentType": "image/jpeg"}]}`
4. **Contract server** converts this to a string with an appended marker: `caption text\n\n[OPENCLAW_IMAGES:[...]]`
5. **Proxy** extracts the marker, fetches the image bytes from S3 (validating the S3 key belongs to the user's namespace), and builds Bedrock multimodal content blocks
6. **Bedrock ConverseStream** receives both text and image content, enabling the configured model to reason about the image

**Telegram**: Photos use the `caption` field for text (not `text`). The Router Lambda checks both. The largest photo size in the `photo` array is used.

**Slack**: The bot requires the `files:read` OAuth scope to download file attachments. Without it, images are silently ignored and only text is processed.

### Cross-Channel Account Linking

By default, each channel creates a separate user identity. If you use both Telegram and Slack, you'll have two separate sessions with separate conversation histories. To unify them into a single identity and shared session:

1. **On your first channel** (e.g., Telegram), send: `link`
   - The bot responds with an 8-character code (e.g., `A1B2C3D4`) valid for 10 minutes
2. **On your second channel** (e.g., Slack), send: `link A1B2C3D4`
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

1. The bot uses the `eventbridge-cron` skill to create an EventBridge Scheduler rule in the environment-specific schedule group (default: `openclaw-cron-dev`)
2. At the scheduled time, EventBridge invokes the Cron executor Lambda (default: `openclaw-cron-executor-dev`)
3. The Lambda warms up the user's AgentCore session (or waits for it to initialize if cold)
4. The Lambda sends the scheduled message to the agent via AgentCore
5. The agent processes the message and the Lambda delivers the response to the user's chat channel

Each user's schedules are isolated — no cross-user access. Schedule metadata is stored in the DynamoDB identity table alongside user profiles and session data.

### API Key Management

The agent includes a built-in `api-keys` skill for securely storing and retrieving API keys (e.g., OpenAI, Jina, YouTube). This replaces the common but **insecure** practice of storing secrets in plaintext `.env` files or pasting them into chat messages.

> **Why not `.env` files?** Plaintext `.env` files on disk are readable by any process, visible in shell history, easily committed to git, and have no audit trail. The `api-keys` skill stores secrets in **AWS Secrets Manager** — KMS-encrypted, per-user isolated, and auditable via CloudTrail.

**Two storage backends:**

| Backend | Storage | Encryption | Audit Trail | Best For |
|---|---|---|---|---|
| **Secrets Manager** (recommended) | `openclaw/user/{namespace}/{key_name}` | KMS CMK | CloudTrail | Production API keys, tokens with compliance requirements |
| **Native file** | `.openclaw/user-api-keys.json` (S3-synced) | S3 SSE-KMS | S3 access logs | Quick prototyping, less sensitive keys |

**Just ask the bot in natural language:**

| What you say | What happens |
|---|---|
| "Store my OpenAI key: sk-abc123" | Saves to Secrets Manager (default) |
| "What API keys do I have?" | Lists keys from both backends |
| "Get my YouTube API key" | Retrieves from SM first, falls back to native |
| "Move my key to Secrets Manager" | Migrates from native → SM |
| "Delete my old API key" | Removes from the appropriate backend |

The agent also **proactively detects API keys** — if you paste something that looks like a key (e.g., `sk-...`, `ghp_...`, `AKIA...`), it offers to store it securely without you having to ask.

**Security controls:**
- Per-user isolation via STS session-scoped credentials (each user can only access `openclaw/user/{their_namespace}/*`)
- Max 10 secrets per user in Secrets Manager
- Key names validated (alphanumeric, max 64 chars)
- Available immediately during warm-up phase — no need to wait for full OpenClaw startup

### Browser Support (Optional)

The agent can browse the web using a headless Chromium browser running inside the AgentCore container. This is **opt-in** — disabled by default.

**Enable it:** Set `enable_browser` to `true` in `cdk.json`. CDK creates the browser resource and injects `BROWSER_IDENTIFIER` into the runtime environment automatically. The contract server creates a browser session on init, and the `agentcore-browser` skill scripts communicate with it via a session file.

**What you can do:**

| What you say | What happens |
|---|---|
| "Open https://example.com" | Navigates to the URL and returns page content |
| "Take a screenshot of this page" | Captures a PNG screenshot, delivered as a photo in chat |
| "Click the Sign In button" | Interacts with page elements (click, type, scroll) |

**Three skill tools:**

| Tool | Purpose |
|---|---|
| `browser_navigate` | Navigate to a URL, return page title and text content |
| `browser_screenshot` | Capture a PNG screenshot, uploaded to S3 with `[SCREENSHOT:]` marker for channel delivery |
| `browser_interact` | Click, type, scroll, or wait on page elements by CSS selector |

Screenshots are uploaded to `{namespace}/_screenshots/` in S3 and delivered as photos to Telegram/Slack via the router's screenshot marker detection.

> **Note:** Browser support requires full OpenClaw startup — it is not available during the warm-up phase. The browser session has a 1-hour timeout and is recreated automatically if needed.

### Container Startup Sequence

1. **entrypoint.sh**: Configure Node.js IPv4 DNS patch, start contract server
2. **agentcore-contract.js** (port 8080): Responds to `/ping` with `Healthy` immediately
3. **At boot** (background): Pre-fetch secrets from Secrets Manager (~2s)
4. **On first `/invocations` with `action: chat`, `action: warmup`, or `action: cron`** (parallel init):
   - Create STS scoped credentials restricting S3 to user's namespace prefix
   - Start `agentcore-proxy.js` (port 18790) with `USER_ID`/`CHANNEL` env vars
   - Start OpenClaw gateway (port 18789) with scoped credentials (no container credentials)
   - Restore `.openclaw/` from S3 via `workspace-sync.js` in background
   - Start credential refresh timer (45 min interval)
   - Wait for proxy only (~5s)
5. **Warm-up phase** (t=~10s to ~1-2min): `lightweight-agent.js` handles messages via proxy -> Bedrock (supports s3-user-files, eventbridge-cron, and clawhub-manage tools — users can manage files, schedules, and install skills immediately)
6. **Handoff** (~1-2min): OpenClaw becomes ready, all subsequent messages route via WebSocket bridge
7. **After handoff**: Full OpenClaw features — built-in web tools (`web_search`, `web_fetch`), 5 ClawHub skills (jina-reader, deep-research-pro, telegram-compose, transcript, task-decomposer), sub-agent support, session management
8. **SIGTERM**: Save `.openclaw/` to S3, kill child processes, exit

### Message Flow

1. User sends message (text/photo) → Telegram/Slack webhook → API Gateway → Router Lambda
2. Lambda returns 200 immediately, self-invokes async for processing
3. Lambda resolves user identity in DynamoDB, uploads photos to S3 if present
4. Lambda calls `InvokeAgentRuntime` with per-user session ID
5. Contract server triggers lazy init (first message) or bridges to OpenClaw directly
6. Proxy converts to Bedrock ConverseStream API call (multimodal if images present)
7. Response streams back → Lambda recursively unwraps nested content blocks (from subagent responses), converts markdown to Telegram HTML, sends to channel API

### Tools & Skills

The agent runs with OpenClaw's **full tool profile** enabled, giving it access to built-in tool groups (web, filesystem, runtime, sessions, automation). Three custom skills are included:

| Skill | Purpose |
|---|---|
| `eventbridge-cron` | Cron scheduling via EventBridge Scheduler — create, update, and delete recurring tasks |
| `s3-user-files` | Per-user file storage (S3-backed) — read, write, list, and delete files |
| `clawhub-manage` | ClawHub skill installer — install, uninstall, and list community skills |
| `api-keys` | Secure API key management — dual-mode storage with native file-based or AWS Secrets Manager backend (see [API Key Management](#api-key-management)) |
| `agentcore-browser` | Headless Chromium browser — navigate, screenshot, interact with web pages (optional, see [Browser Support](#browser-support-optional)) |

Five ClawHub community skills are pre-installed at Docker build time:

| ClawHub Skill | Purpose |
|---|---|
| `jina-reader` | Extract web content as clean markdown |
| `deep-research-pro` | In-depth multi-step research (spawns sub-agents) |
| `telegram-compose` | Rich HTML formatting for Telegram messages |
| `transcript` | YouTube video transcript extraction |
| `task-decomposer` | Break complex requests into subtasks (spawns sub-agents) |

During the warm-up phase (~first 1-2 min on cold start), the **lightweight agent shim** handles messages with built-in `web_fetch` and `web_search` tools, plus `s3-user-files`, `eventbridge-cron`, `clawhub-manage`, and `api-keys` skills. Users can manage files, schedules, skills, and API keys even during warm-up. ClawHub skills become available after OpenClaw fully starts.

### Webhook Security

The Router Lambda validates all incoming webhook requests:

- **Telegram**: Validates the `X-Telegram-Bot-Api-Secret-Token` header against the environment-specific webhook secret stored in Secrets Manager (default: `openclaw/webhook-secret-dev`). The secret is registered with Telegram via the `secret_token` parameter on `setWebhook`.
- **Slack**: Validates the `X-Slack-Signature` HMAC-SHA256 header using the Slack app's signing secret. Includes 5-minute timestamp check to prevent replay attacks.
- **API Gateway**: Only explicit routes are exposed (`POST /webhook/telegram`, `POST /webhook/slack`, `GET /health`). All other paths return 404 from API Gateway without invoking the Lambda. Rate limiting is applied (burst: 50, sustained: 100 req/s).

Requests that fail validation receive a 401 response and are logged with the source IP.

### Token Usage Tracking

Bedrock invocation logs flow to CloudWatch, where a Lambda processor extracts token counts, estimates costs, and writes to DynamoDB (single-table design with 4 GSIs for different query patterns). Custom CloudWatch metrics power the analytics dashboard and budget alarms.

## Operations

### Check runtime status

```bash
RUNTIME_ID=$(aws cloudformation describe-stacks \
  --stack-name OpenClawAgentCore-dev \
  --query "Stacks[0].Outputs[?OutputKey=='RuntimeId'].OutputValue" \
  --output text --region $CDK_DEFAULT_REGION)

aws bedrock-agentcore get-runtime \
  --agent-runtime-id $RUNTIME_ID \
  --region $CDK_DEFAULT_REGION
```

### Check DynamoDB identity table

```bash
aws dynamodb scan --table-name openclaw-identity-dev --region $CDK_DEFAULT_REGION
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
cdk deploy OpenClawAgentCore-dev --require-approval never
# 4. New sessions will use the new image automatically (per-user idle termination)
```

### Run tests

```bash
cd bridge && node --test proxy-identity.test.js       # identity + workspace tests
cd bridge && node --test image-support.test.js         # image upload + multimodal tests
cd bridge && node --test lightweight-agent.test.js     # lightweight agent tools + buildToolArgs tests
cd bridge && node --test subagent-routing.test.js      # subagent model routing + detection tests
cd bridge && node --test content-extraction.test.js    # recursive content block extraction tests
cd bridge && node --test scoped-credentials.test.js    # per-user STS credential scoping tests
cd bridge && node --test workspace-sync.test.js        # workspace sync credential tests
cd bridge/skills/s3-user-files && AWS_REGION=$CDK_DEFAULT_REGION node --test common.test.js  # S3 skill tests
cd lambda/router && python -m pytest test_image_upload.py -v        # image upload unit tests
cd lambda/router && python -m pytest test_content_extraction.py -v  # content block extraction tests
cd lambda/router && python -m pytest test_markdown_html.py -v       # markdown-to-HTML conversion tests

# E2E tests (requires deployed stack + E2E_TELEGRAM_CHAT_ID/E2E_TELEGRAM_USER_ID env vars)
pytest tests/e2e/bot_test.py -v -k smoke               # connectivity + webhook auth
pytest tests/e2e/bot_test.py -v -k lifecycle            # full message lifecycle
pytest tests/e2e/bot_test.py -v -k cold_start           # new session creation
pytest tests/e2e/bot_test.py -v -k warmup               # warm-up shim verification
pytest tests/e2e/bot_test.py -v -k full_startup          # full OpenClaw startup + timing (~5min)
pytest tests/e2e/bot_test.py -v -k ScopedCredentials     # S3 file write/read/delete via scoped creds
pytest tests/e2e/bot_test.py -v -k conversation          # multi-turn + rapid-fire
pytest tests/e2e/bot_test.py -v -k SkillManagement       # clawhub skill install/uninstall/list
pytest tests/e2e/bot_test.py -v -k ApiKeyManagement      # API key storage (native + Secrets Manager)
pytest tests/e2e/bot_test.py -v -k CronSchedule          # cron lifecycle + CRON# DynamoDB record check
pytest tests/e2e/bot_test.py -v -k GuardrailSecurity     # guardrail content filtering (requires BEDROCK_GUARDRAIL_ID env var)
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

This is expected for full OpenClaw initialization. However, the **lightweight agent shim** responds to the first message in ~10-15 seconds with support for file storage and cron scheduling tools. OpenClaw initializes in the background (~1-2 minutes) and takes over once ready. The Router Lambda sends a typing indicator to Telegram while waiting, and after 30 seconds sends a progress message ("Working on your request...") to both Telegram and Slack so users know the bot is still working. Subsequent messages in the same session are fast.

### Slack bot not responding

- **Socket Mode conflict**: If Event Subscriptions doesn't show a Request URL field, disable **Settings** > **Socket Mode**. Socket Mode uses WebSocket connections instead of webhooks.
- **Signing secret mismatch**: The Lambda validates `X-Slack-Signature` using the signing secret stored in Secrets Manager. Verify it matches:
  ```bash
  aws secretsmanager get-secret-value \
   --secret-id openclaw/channels/slack-dev \
    --region $CDK_DEFAULT_REGION \
    --query SecretString --output text
  ```
- **Bot not in DMs**: Go to **Features** > **App Home** and enable **Messages Tab** + **Allow users to send messages**.
- **Separate session from Telegram**: By default, Slack and Telegram create separate user identities. Use the cross-channel linking feature (see above) to unify them into a single session.

### Telegram bot not responding

- **Token invalid**: Check that the Telegram token in Secrets Manager is correct:
  ```bash
  aws secretsmanager get-secret-value \
   --secret-id openclaw/channels/telegram-dev \
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
- **Model permissions**: The IAM policy uses `arn:aws:bedrock:*::foundation-model/*` and `arn:aws:bedrock:{region}:{account}:inference-profile/*` so you can swap Bedrock models in `cdk.json` without changing the role policy.

### Node.js ETIMEDOUT / ENETUNREACH in VPC

Node.js 22's Happy Eyeballs (`autoSelectFamily`) tries both IPv4 and IPv6. In VPCs without IPv6, this causes connection failures. The `force-ipv4.js` script patches `dns.lookup()` to force IPv4 only, loaded via `NODE_OPTIONS`.

## Known Limitations

| Limitation | Details |
|---|---|
| **Cold start time** | Lightweight agent responds in ~5-15s; full OpenClaw ready in ~1-2 min (plugin registration) |
| **Image size** | Max 3.75 MB per image (Bedrock Converse API limit) |
| **Session timeout** | Sessions terminate after 30 min idle (configurable via `session_idle_timeout`) |
| **ClawHub skills** | 5 pre-installed; available only after full OpenClaw startup (~1-2 min). During warm-up, built-in web_fetch/web_search tools are available |
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
- **ClawHub skills**: 5 community skills are pre-installed at Docker build time (jina-reader, deep-research-pro, telegram-compose, transcript, task-decomposer). Custom skills (s3-user-files, eventbridge-cron, clawhub-manage) are in `/skills/` loaded via `extraDirs`. ClawHub installs to the managed skills path, scanned automatically by OpenClaw. Users can install/uninstall skills via the `clawhub-manage` skill — changes take effect on the next session start.
- **ClawHub `--force` flag**: Some skills are flagged by VirusTotal for external API calls. Use `--no-input --force` for non-interactive Docker builds.
- **`default-user` fallback**: If identity resolution fails, requests fall back to `actorId = "default-user"` — meaning all such users share one S3 namespace. The `USER_ID` env var path (set by contract server) should prevent this in per-user mode.
- **actorId vs namespace format**: The actorId uses colon format (`telegram:123456789`) while skill scripts expect namespace/underscore format (`telegram_123456789`). The lightweight agent's `chat()` function converts via `userId.replace(/:/g, "_")` before passing to tool scripts. The proxy and workspace sync also use namespace format for S3 keys.
- **Image version bumps are required**: After pushing a new bridge container image, you must bump `image_version` in `cdk.json` and redeploy the matching AgentCore stack (default: `OpenClawAgentCore-dev`). AgentCore caches images by digest and only re-pulls when the runtime endpoint configuration changes. Without the bump, existing sessions continue using the old image.
- **Image upload size limit**: Bedrock Converse API limits images to 3.75 MB. The Router Lambda checks this before uploading to S3.
- **OpenClaw gateway protocol version**: Current OpenClaw gateway clients connect with protocol `4`. The contract bridge now defaults to protocol `4` and retries once with the server-advertised `expectedProtocol` on `PROTOCOL_MISMATCH`, which avoids intermittent `Auth failed: protocol mismatch` errors during mixed-version rollouts or stale sessions.
- **OpenClaw 2026.5.19 WebSocket origin enforcement**: OpenClaw enforces origin checks on all WebSocket connections carrying an `Origin` header. The `ws` Node.js library must use the `origin` **option** (not `headers.Origin`) to correctly set the header on the HTTP upgrade request. The `controlUi` config requires `allowedOrigins: ["*"]` to accept the origin. Without both the client `origin` option and config `allowedOrigins`, connections fail with: `Auth failed: origin not allowed`.

## Cleanup

```bash
./scripts/undeploy.sh
```

`undeploy.sh` no longer scans DynamoDB for session IDs. It now uses only runtime-scoped AgentCore session discovery when the installed AWS tooling exposes that API; otherwise, stop any active sessions manually or wait for them to idle out before VPC teardown.

There are **two separate cleanup layers**:

1. `./scripts/undeploy.sh --all` controls **which stacks** CloudFormation is asked to destroy.
2. Each resource's `RemovalPolicy` controls whether that resource is **actually deleted or retained** when its stack is destroyed.

That means **`--all` does not override `RemovalPolicy.RETAIN`**. A stack can be deleted successfully while some of its resources are intentionally left behind.

Common cleanup variants:

```bash
./scripts/undeploy.sh                                   # destroy deployable stacks, keep OpenClawSecurity
./scripts/undeploy.sh --delete-user-files-bucket        # also delete the retained S3 user-files bucket
./scripts/undeploy.sh --all                             # also destroy OpenClawSecurity
./scripts/undeploy.sh --all --delete-user-files-bucket  # destroy all included stacks and also delete the user-files bucket
```

The undeploy script destroys the matching AgentCore stack separately (default: `OpenClawAgentCore-dev`), waits for AgentCore-managed `agentic_ai` ENIs to leave the VPC before deleting the matching VPC stack, and falls back to retaining the AgentCore runtime security group if CloudFormation gets stuck on security-group cleanup.

### What `--all` really means

`--all` means: **also include `OpenClawSecurity-*` in the destroy operation**.

It does **not** mean: **force-delete every resource created by every stack**.

### What can still remain after `./scripts/undeploy.sh --all`

These resources are currently retained by CDK policy or by undeploy fallback logic:

| Resource | Why it can remain after `--all` |
| --- | --- |
| DynamoDB identity table | `RemovalPolicy.RETAIN` |
| API Gateway access log group | `RemovalPolicy.RETAIN` |
| S3 user-files bucket | `RemovalPolicy.RETAIN`; only removed when `--delete-user-files-bucket` is passed |
| DynamoDB token-monitoring table | `RemovalPolicy.RETAIN` |
| VPC flow log group | `RemovalPolicy.RETAIN` |
| KMS key in `OpenClawSecurity-*` | `RemovalPolicy.RETAIN` |
| Cognito user pool in `OpenClawSecurity-*` | `RemovalPolicy.RETAIN` |
| CloudTrail bucket (if enabled) | `RemovalPolicy.RETAIN` |
| AgentCore runtime security group | undeploy fallback may call `delete-stack --retain-resources` if CloudFormation is stuck on SG cleanup |

So the correct mental model is:

- **stack destroyed** != **every resource deleted**
- **`--all`** = destroy more stacks
- **`RETAIN`** = keep specific resources even if their stack is destroyed

## Security

See [docs/security.md](docs/security.md) for the complete security architecture (threat model, defense-in-depth layers, operations runbook), [SECURITY.md](SECURITY.md) for reporting vulnerabilities, and [CONTRIBUTING.md](CONTRIBUTING.md#security-issue-notifications) for contribution guidelines.

## Security Testing

### LLM Red Team Testing

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

### Guardrail E2E Tests

The `TestGuardrailSecurity` test class (6 tests) validates guardrail behavior through the full Telegram webhook pipeline:

```bash
# Requires deployed stack + guardrail ID
export BEDROCK_GUARDRAIL_ID=$(aws cloudformation describe-stacks \
  --stack-name OpenClawGuardrails \
  --query "Stacks[0].Outputs[?OutputKey=='GuardrailId'].OutputValue" \
  --output text --region ap-southeast-2)
pytest tests/e2e/bot_test.py -v -k GuardrailSecurity
```

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
