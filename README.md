# OpenClaw on AWS Bedrock AgentCore

[![License: MIT-0](https://img.shields.io/badge/License-MIT--0-blue.svg)](LICENSE)
[![Status: Experimental](https://img.shields.io/badge/Status-Experimental-orange.svg)]()
[![AWS CDK](https://img.shields.io/badge/AWS%20CDK-v2-yellow.svg)]()

> **Experimental** — This project is provided for experimentation and learning purposes only. It is **not intended for production use**. APIs, architecture, and configuration may change without notice.

Deploy an AI-powered multi-channel messaging bot (Telegram, Slack, Feishu) on AWS Bedrock AgentCore Runtime using CDK.

## Table of Contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Project Structure](#project-structure)
- [Configuration](#configuration)
- [Channel Setup](#channel-setup)
- [How It Works](#how-it-works)
- [Operations](#operations)
- [Upgrading from OpenClaw 2026.3.x](#upgrading-from-openclaw-20263x)
- [Troubleshooting](#troubleshooting)
- [Known Limitations](#known-limitations)
- [Gotchas](#gotchas)
- [Cleanup](#cleanup)
- [Security](#security)
- [Security Testing](#security-testing)
- [License](#license)

OpenClaw runs as **per-user serverless containers** on AgentCore Runtime. A Router Lambda handles webhook ingestion from Telegram, Slack and Feishu, resolves user identity via DynamoDB, and invokes per-user AgentCore sessions. Each user gets their own microVM with workspace persistence: the OpenClaw state dir (`~/.openclaw/`, SQLite session state plus the agent workspace) lives on the container's local disk, is mirrored to AgentCore session storage while the session runs, and is backed up to S3. The agent has built-in tools (web, filesystem, runtime, sessions, automation), custom skills for file storage and cron scheduling, and **EventBridge-based cron scheduling** for recurring tasks.

Users can send **text and images** — photos sent via Telegram, Slack or Feishu are downloaded by the Router Lambda, stored in S3, and passed to Claude as multimodal content via Bedrock's ConverseStream API. Supported formats: JPEG, PNG, GIF, WebP (max 3.75 MB).

### Features

- Per-user Firecracker microVM isolation (AgentCore Runtime)
- Multi-channel support (Telegram, Slack, Feishu) with cross-channel account linking
- Multimodal: text + image messages via Bedrock ConverseStream
- STS session-scoped credentials (per-user S3, DynamoDB, Secrets Manager isolation)
- Custom skills: S3 file storage, EventBridge cron scheduling, API key management, ClawHub skill installer
- Headless browser (optional, AgentCore Browser API)
- AWS Bedrock Guardrails — content filtering, PII redaction, topic denial, word filters, prompt attack detection
- LLM red team testing — 62 test cases across 12 attack categories via promptfoo
- App-level security E2E tests (TestGuardrailSecurity — 6 tests through the full Telegram webhook pipeline)

## Architecture

![Architecture: users message Telegram, Slack or Feishu; webhooks reach API Gateway and the Router Lambda, which resolves the user in DynamoDB and invokes OpenClaw 2.0 on a per-user Bedrock AgentCore Runtime microVM; the runtime calls Amazon Bedrock (Claude with Guardrails), keeps state in S3, reads Secrets Manager and Cognito/STS scoped credentials, drives an AgentCore Browser in the VPC, and schedules tasks through EventBridge Scheduler and a Cron Lambda; Bedrock invocation logs feed CloudWatch token monitoring](docs/images/architecture.svg)

The diagram shows the high-level request path. Container internals (contract server, lightweight agent, Bedrock proxy, session storage), KMS and networking are described in the component table below and in [docs/architecture-detailed.md](docs/architecture-detailed.md).

Messages from a channel reach API Gateway and the Router Lambda, which validates the webhook, resolves the user in DynamoDB and calls `InvokeAgentRuntime` with a per-user session id. Inside the user's microVM the contract server answers immediately through the lightweight agent while the OpenClaw gateway boots, then bridges every later message to OpenClaw over WebSocket. Both paths call Bedrock through the local proxy. OpenClaw state lives on local disk, is mirrored to session storage and snapshotted to S3. Scheduled tasks and token monitoring run on their own Lambdas. The AgentCore Browser (`enable_browser`, on by default) is a `CfnBrowserCustom` in the VPC private subnets: the contract server starts a per-user browser session and the `agentcore-browser` skill drives it over CDP to navigate, screenshot and interact with pages; see [Browser Support](#browser-support-optional). The dashed *Optional: enable_gateway* group is the opt-in `enable_gateway` flag: OpenClaw also calls an AgentCore Gateway over MCP (per-user Cognito access token) whose Lambda targets serve the user-files and schedule tools against the same S3 bucket and EventBridge Scheduler; see [AgentCore Gateway MCP tools](#agentcore-gateway-mcp-tools-prototype).

| Component | What it is | Code |
|---|---|---|
| API Gateway HTTP API | Routes `POST /webhook/{telegram,slack,feishu}` and `GET /health`; throttled (burst 50, rate 100) | `stacks/router_stack.py` |
| Router Lambda | Webhook validation, identity resolution, image upload, `InvokeAgentRuntime`, reply delivery, typing/progress notices | `lambda/router/index.py` |
| DynamoDB `openclaw-identity` | Users, channel bindings, sessions, allowlist, link codes, `CRON#` records | `stacks/router_stack.py` |
| Contract server | AgentCore HTTP contract on port 8080 (`/ping`, `/invocations`); per-user init, state layout, gateway spawn, WebSocket bridge, SIGTERM snapshot | `bridge/agentcore-contract.js` |
| Lightweight agent | Warm-up agent with 17 tools (web, S3 files, schedules, ClawHub, API keys) used until the gateway is ready | `bridge/lightweight-agent.js` |
| OpenClaw gateway | `openclaw@2026.9.5` on `node:24-slim`, gateway protocol v4, `gateway-client`/`backend` identity, 5 pinned ClawHub skills + 4 custom skills from `/skills` | `bridge/Dockerfile`, `bridge/skills/` |
| Bedrock proxy | OpenAI-compatible endpoint on port 18790 → Bedrock `ConverseStream`; multimodal images, sub-agent model routing, per-user Cognito JWT | `bridge/agentcore-proxy.js` |
| Local disk `~/.openclaw` | Authoritative OpenClaw state (SQLite sessions + workspace); the NFS mount cannot hold SQLite locks or hard links | `bridge/state-storage.js` |
| Session storage `/mnt/workspace` | AgentCore managed session storage; mirror of the state dir, restored before the gateway spawns | `bridge/state-storage.js`, `scripts/deploy.sh` |
| S3 user-files bucket | Per-user files, image uploads, screenshots, and `~/.openclaw` snapshots (SQLite via online backup) | `stacks/agentcore_stack.py`, `bridge/workspace-sync.js` |
| STS scoped credentials | Execution role re-assumed with a session policy limiting S3, DynamoDB, Secrets Manager and Scheduler to the user's namespace | `bridge/scoped-credentials.js` |
| Cognito User Pool | Per-user Cognito user with an HMAC-derived password; the proxy acquires and caches an ID token per user. With `enable_gateway: true` the contract server mints the same user's **access** token as the bearer for the Gateway MCP server | `stacks/security_stack.py`, `bridge/agentcore-proxy.js`, `bridge/cognito-token.js` |
| Secrets Manager | `openclaw/gateway-token`, `openclaw/channels/*`, `openclaw/webhook-secret`, `openclaw/cognito-password-secret`, per-user `openclaw/user/{ns}/*` | `stacks/security_stack.py`, `bridge/skills/api-keys/` |
| KMS CMK | Encrypts S3, DynamoDB, SNS and Secrets Manager | `stacks/security_stack.py` |
| EventBridge Scheduler + Cron Lambda | Schedule group `openclaw-cron`; `openclaw-cron-executor` warms the session, sends the `cron` action and posts the reply to Telegram/Slack | `stacks/cron_stack.py`, `lambda/cron/index.py`, `bridge/skills/eventbridge-cron/` |
| Token monitoring | Bedrock invocation logs → CloudWatch subscription → `token_metrics` Lambda → DynamoDB (3 GSIs) + custom metrics, dashboards, budget alarms, SNS | `stacks/observability_stack.py`, `stacks/token_monitoring_stack.py`, `lambda/token_metrics/index.py` |
| Bedrock Guardrails (optional) | `CfnGuardrail` + version; see [Security](#security) | `stacks/guardrails_stack.py` |
| AgentCore Gateway (optional, prototype) | `enable_gateway: false` by default. When on: MCP Gateway `openclaw-tools` with a Cognito JWT authorizer, a REQUEST interceptor Lambda and two Lambda targets (`user-files`, `schedules`) that serve the file and schedule tools as typed MCP tools; see [AgentCore Gateway MCP tools](#agentcore-gateway-mcp-tools-prototype) | `stacks/gateway_stack.py`, `lambda/gateway_tools/`, `bridge/gateway-mcp.js` |
| AgentCore Browser (optional) | `CfnBrowserCustom` in the VPC, used by the `agentcore-browser` skill | `stacks/agentcore_stack.py`, `bridge/skills/agentcore-browser/` |

The AgentCore Runtime, its endpoint and the ECR repository are created by the **AgentCore Starter Toolkit** (`agentcore deploy`) in Phase 2 of `scripts/deploy.sh`, not by CDK. `OpenClawAgentCore` provides the execution role, security group and bucket, and the Phase 3 stacks read `runtime_id`/`runtime_endpoint_id` from `cdk.json` context.

**Container startup on the first message of a session:**

```mermaid
sequenceDiagram
    participant R as Router Lambda
    participant C as Contract server
    participant L as Lightweight agent
    participant P as Bedrock proxy
    participant G as OpenClaw gateway

    R->>C: POST /invocations (chat)
    C->>C: scoped STS creds, state layout, restore mirror / S3
    C->>P: spawn :18790
    C->>G: spawn :18789 after restore (bounded wait)
    C->>L: handle message once proxy is up
    L->>P: chat completion
    P-->>R: reply with warm-up footer
    G-->>C: ready
    Note over C,G: later messages bridged over WebSocket v4
    C->>G: forward chat
    G->>P: chat completion
    P-->>R: reply
```

See [docs/architecture-detailed.md](docs/architecture-detailed.md) for sequence diagrams, container internals and data flows, and [docs/architecture.md](docs/architecture.md) for the solution overview.

### Why S3 Workspace Sync?

AgentCore microVMs are ephemeral — they're destroyed when idle. OpenClaw stores conversation history (per-agent SQLite databases since 2.0), user profiles, and agent configuration in the `~/.openclaw/` directory. **S3-backed workspace sync** restores this directory on session start, saves it periodically (every 5 min, or 30 min when session storage is the primary store), and performs a final save on shutdown. Each `*.sqlite` file is uploaded as a consistent point-in-time snapshot (node:sqlite online backup), never as raw WAL-mode bytes. Each user's workspace is isolated under a unique S3 prefix derived from their channel identity.

This lets the system behave like a persistent server (continuous conversation history) while benefiting from serverless economics (no idle compute costs).

### Session Storage (Persistent Filesystem)

When [Managed Session Storage](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-persistent-filesystems.html) is available, `~/.openclaw/` persists across stop/resume cycles via `/mnt/workspace`, with S3 as a cold backup. Configured automatically by `deploy.sh`.

The state dir is **not** placed directly on the mount. Session storage is a loopback NFS export with `local_lock=none` and no hard-link support; OpenClaw 2.0 needs both (SQLite locks for its session store, `link(2)` to publish workspace files such as `AGENTS.md`). `bridge/state-storage.js` therefore keeps `~/.openclaw/` on local disk and mirrors it onto `/mnt/workspace/.openclaw/` — the workspace within seconds of a change (debounced `fs.watch`), everything else (with each `*.sqlite` as a consistent snapshot) every 5 minutes and on `SIGTERM` — then restores the mirror to local disk before the gateway spawns on the next start. See [docs/session-storage.md](docs/session-storage.md) and [docs/openclaw-2.0-upgrade.md](docs/openclaw-2.0-upgrade.md).

Operators can run one-off shell commands inside a live session (health checks, workspace inspection, CI assertions) with `scripts/agentcore-exec.py`, a boto3 wrapper around `InvokeAgentRuntimeCommand`. It runs with the **full execution-role credentials**, so it is operator-only and has no chat path. See [docs/execute-command.md](docs/execute-command.md).

### Security

This solution applies **defense-in-depth** across network, application, identity, and data layers. Key controls include:

- **Network isolation**: Private VPC subnets with VPC endpoints; no direct internet exposure for containers
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

With the opt-in `enable_gateway: true` in `cdk.json`, Phase 1 also deploys `OpenClawGateway` and Phase 2 reads its `GatewayUrl` output (exact `OutputKey`; the deploy fails if it is empty) and passes it to the runtime as `AGENTCORE_GATEWAY_URL`. With the default `false` the stack is not in the app and the variable is not set. See [AgentCore Gateway MCP tools](#agentcore-gateway-mcp-tools-prototype).

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

## Project Structure

```
openclaw-on-agentcore/
  app.py                          # CDK app entry point (8 stacks + opt-in OpenClawGateway)
  cdk.json                        # Configuration (model, budgets, sessions, cron, guardrails)
  requirements.txt                # Python deps (aws-cdk-lib, cdk-nag)
  stacks/
    __init__.py                   # Shared helper (RetentionDays converter)
    vpc_stack.py                  # VPC, subnets, NAT, 7 interface + 1 S3 gateway VPC endpoints, flow logs
    security_stack.py             # KMS CMK, Secrets Manager, Cognito, optional CloudTrail
    agentcore_stack.py            # Execution role, SG, S3 user-files bucket, optional AgentCore Browser (Runtime/ECR are toolkit-managed)
    router_stack.py               # Router Lambda + API Gateway HTTP API (telegram/slack/feishu routes) + DynamoDB identity
    observability_stack.py        # Operations dashboard, alarms, SNS topic, Bedrock invocation logging
    token_monitoring_stack.py     # Lambda processor, DynamoDB (3 GSIs), token analytics dashboard
    guardrails_stack.py           # Bedrock Guardrails (content filters, PII, topic denial)
    cron_stack.py                 # EventBridge Scheduler, Cron executor Lambda, IAM
    gateway_stack.py              # Prototype: AgentCore Gateway (MCP) + interceptor + tool Lambdas; only when enable_gateway=true
  bridge/
    Dockerfile                    # Container image (node:24-slim, ARM64, pinned openclaw@2026.9.5 + clawhub@0.23.3, 5 owner-pinned ClawHub skills)
    entrypoint.sh                 # Startup: configure IPv4, start contract server
    agentcore-contract.js         # AgentCore HTTP contract with hybrid routing (shim + OpenClaw)
    gateway-mcp.js                # mcp.servers.agentcore config + bearer refresh (only when AGENTCORE_GATEWAY_URL is set)
    gateway-mcp.test.js           # Gateway MCP config/refresh + Cognito token provider tests (node:test, 12 tests)
    cognito-token.js              # Per-user Cognito ID/access token provider shared by proxy and contract server
    lightweight-agent.js          # Warm-up agent shim (17 tools: web, s3-user-files, eventbridge-cron, clawhub-manage, api-keys)
    lightweight-agent.test.js     # Lightweight agent unit tests (node:test, 111 tests)
    agentcore-proxy.js            # OpenAI -> Bedrock ConverseStream adapter + Identity + multimodal images
    image-support.test.js         # Image support unit tests (node:test)
    proxy-identity.test.js        # Proxy identity resolution tests (node:test)
    agentcore-browser.test.js     # Browser skill unit tests (node:test)
    browser-lifecycle.test.js     # Browser session lifecycle tests (node:test)
    content-extraction.test.js    # Content block extraction tests (node:test)
    subagent-routing.test.js      # Subagent model routing + detection tests (node:test)
    workspace-sync.js             # ~/.openclaw/ S3 sync (restore/save/periodic, SQLite snapshots)
    workspace-sync.test.js        # Workspace sync tests (node:test, 48 tests)
    state-storage.js              # Local state dir + session-storage mirror/restore (SQLite + workspace)
    state-storage.test.js         # State storage layout tests (node:test, 34 tests)
    scoped-credentials.js         # Per-user STS session-scoped S3 credentials
    scoped-credentials.test.js    # Scoped credentials unit tests (node:test, 43 tests)
    force-ipv4.js                 # DNS patch for Node.js Happy Eyeballs IPv6 issue
    cloudwatch-logger.js          # Ships container stdout/stderr to CloudWatch Logs
    CLAUDE.md                     # Project instructions (for Claude Code IDE)
    skills/
      s3-user-files/              # Custom per-user file storage skill (S3-backed)
      eventbridge-cron/           # Cron scheduling skill (EventBridge Scheduler)
      clawhub-manage/             # ClawHub skill installer (install/uninstall/list)
      api-keys/                   # Dual-mode API key management (native file + Secrets Manager)
      agentcore-browser/          # Optional headless browser skill (navigate/screenshot/interact)
  lambda/
    token_metrics/index.py        # Bedrock log -> DynamoDB + CloudWatch metrics
    router/index.py                    # Webhook router (Telegram + Slack + Feishu, image uploads)
    router/test_image_upload.py        # Image upload unit tests (pytest)
    router/test_content_extraction.py  # Content block extraction tests (pytest)
    router/test_markdown_html.py       # Markdown-to-HTML conversion tests (pytest)
    router/test_slack.py               # Slack handler tests (pytest)
    router/test_feishu.py              # Feishu handler tests (pytest)
    router/test_screenshot_handling.py # Screenshot marker delivery tests (pytest)
    router/test_formatting_integration.py # Formatting integration tests (pytest)
    cron/index.py                      # Cron executor (warmup, invoke, deliver)
    gateway_tools/                     # Prototype Gateway MCP targets (Node 22 Lambdas)
      tool-schemas.json                # MCP tool schemas consumed by CDK and the tests
      interceptor/index.js             # REQUEST interceptor: copies the bearer JWT into __caller_token
      s3_user_files/index.js           # user-files target: list/read/write/delete in the caller's S3 prefix
      eventbridge_cron/index.js        # schedules target: create/list/update/delete the caller's schedules
      lib/identity.js                  # JWT verification against the pool JWKS; namespace derivation
      lib/mcp.js                       # Lambda-target event/context helpers
      identity.test.js, tools.test.js  # Unit tests (node:test, 31 tests)
  scripts/
    setup-telegram.sh             # Telegram webhook + admin allowlist (one-step)
    setup-slack.sh                # Slack Event Subscriptions + admin allowlist
    setup-feishu.sh               # Feishu app credentials + event subscription + admin allowlist
    deploy.sh                     # Three-phase deploy (CDK -> Starter Toolkit -> CDK)
    e2e-deploy-and-test.sh        # Deploy then run the E2E suite
    manage-allowlist.sh           # Add/remove/list users in the allowlist
    agentcore-exec.py             # Operator CLI: run a shell command in a live session (InvokeAgentRuntimeCommand)
  tests/
    test_agentcore_exec.py        # Unit tests for scripts/agentcore-exec.py (mocked boto3, no AWS)
    test_gateway_stack_synth.py   # OpenClawGateway synth tests: flag off = unchanged templates, flag on = stack + IAM + cdk-nag (10 tests, no AWS)
    e2e/                          # E2E tests (simulated Telegram webhooks + CloudWatch logs)
      config.py                   # AWS config auto-discovery (CF outputs, Secrets Manager)
      webhook.py                  # Build + POST Telegram webhook payloads
      session.py                  # DynamoDB session/user reset + AgentCore session stop
      log_tailer.py               # CloudWatch log tailing with pattern matching
      bot_test.py                 # CLI entrypoint + pytest test classes (46 tests, 15 classes)
      conftest.py                 # pytest fixtures, conversation scenarios
      container_logs.py           # Container/Lambda log helpers for the Gateway E2E tests
      test_gateway_tools.py       # Gateway MCP tools E2E (4 tests, `-m gateway`; skipped when the stack is not deployed)
  redteam/                        # LLM red team testing (promptfoo, 62 test cases)
  docs/
    images/architecture.svg       # README architecture diagram (AWS icons)
    architecture.md               # Solution architecture (ASCII diagrams)
    architecture-detailed.md      # Sequence diagrams, container internals, data flows
    openclaw-2.0-upgrade.md       # 2026.3.8 -> 2026.9.5 upgrade notes, risks, validation
    design-feishu-channel.md      # Feishu channel design
    security.md                   # Complete security architecture
    guardrails.md                 # Bedrock Guardrails operational runbook
    session-storage.md            # Persistent /mnt/workspace (Managed Session Storage)
    execute-command.md            # Operator CLI for InvokeAgentRuntimeCommand + security boundary
    gateway-mcp-tools.md          # Prototype: AgentCore Gateway MCP tools (design, identity flow, IAM, tests, live findings)
```

## CDK Stacks

| Stack | Resources | Dependencies |
|---|---|---|
| **OpenClawVpc** | VPC (2 AZ), private/public subnets, NAT, 7 interface endpoints (Bedrock Runtime, SSM, ECR API/Docker, Secrets Manager, CloudWatch Logs/Monitoring) + S3 gateway endpoint, flow logs | None |
| **OpenClawSecurity** | KMS CMK, Secrets Manager (8 secrets: gateway token, 5 channel tokens, webhook secret, Cognito password secret), Cognito User Pool + client, optional CloudTrail | None |
| **OpenClawGuardrails** | CfnGuardrail (content filters, topic denial, PII, word filters, regex), CfnGuardrailVersion | Security |
| **OpenClawAgentCore** | Execution role, security group, S3 user-files bucket, optional `CfnBrowserCustom`. The Runtime, its endpoint and the ECR repository are created by the Starter Toolkit in Phase 2; this stack only reads `runtime_id`/`runtime_endpoint_id` from `cdk.json` context | Vpc, Security, Guardrails |
| **OpenClawRouter** | Lambda, API Gateway HTTP API (`/webhook/telegram`, `/webhook/slack`, `/webhook/feishu`, `/health`; throttling), DynamoDB `openclaw-identity` table | AgentCore, Security |
| **OpenClawObservability** | Operations dashboard, alarms (errors, latency, throttles), SNS topic, Bedrock invocation logging | Security |
| **OpenClawTokenMonitoring** | DynamoDB (single-table, 3 GSIs), Lambda processor, analytics dashboard | Observability, Security |
| **OpenClawCron** | EventBridge Scheduler group `openclaw-cron`, Cron executor Lambda `openclaw-cron-executor`, Scheduler IAM role | AgentCore, Security (identity table referenced by name) |
| **OpenClawGateway** (opt-in, `enable_gateway`) | Prototype: AgentCore Gateway (MCP, Cognito JWT authorizer), REQUEST interceptor Lambda, two Lambda MCP targets for per-user files and schedules. Not instantiated with the default `enable_gateway: false`; see [docs/gateway-mcp-tools.md](docs/gateway-mcp-tools.md) | Security |

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
| `router_lambda_timeout_seconds` | `600` | Router Lambda timeout |
| `router_lambda_memory_mb` | `256` | Router Lambda memory |
| `registration_open` | `false` | If `true`, anyone can message the bot. If `false`, only allowlisted users can register |
| `token_ttl_days` | `90` | DynamoDB token usage record TTL |
| `image_version` | (see `cdk.json`) | Bridge container version tag. Bump to force container redeploy |
| `user_files_ttl_days` | `365` | S3 per-user file expiration |
| `cron_lambda_timeout_seconds` | `900` | Cron executor Lambda timeout (must exceed warmup time) |
| `cron_lambda_memory_mb` | `256` | Cron executor Lambda memory |
| `enable_cloudtrail` | `false` | Deploy a dedicated CloudTrail trail. Off by default — most accounts already have one. Enabling creates an S3 bucket + trail (additional cost) |
| `cron_lead_time_minutes` | `5` | Minutes before schedule time to start warmup |
| `enable_guardrails` | `true` | Deploy Bedrock Guardrails for content filtering. Set `false` to disable (reduces safety but saves cost) |
| `guardrails_content_filter_level` | `HIGH` | Content filter strength for all categories: `LOW`, `MEDIUM`, or `HIGH` |
| `guardrails_pii_action` | `ANONYMIZE` | PII handling: `ANONYMIZE` (redact) or `BLOCK` (reject). Credit cards always BLOCK regardless |
| `enable_browser` | `true` | Deploy an AgentCore Browser (`CfnBrowserCustom`) and pass its id to the runtime as `BROWSER_IDENTIFIER`. Only deployed in regions listed in `BROWSER_SUPPORTED_REGIONS` (`stacks/agentcore_stack.py`) |
| `enable_gateway` | `false` | Prototype: deploy `OpenClawGateway` (AgentCore Gateway serving the per-user file and schedule tools as MCP tools) and pass `AGENTCORE_GATEWAY_URL` to the runtime. See [docs/gateway-mcp-tools.md](docs/gateway-mcp-tools.md) |
| `anomaly_band_width` | `2` | Standard-deviation band for the token-usage anomaly detector alarm |
| `runtime_id` / `runtime_endpoint_id` | (written by `deploy.sh`) | AgentCore Runtime id and endpoint id from the Starter Toolkit. Phase 3 stacks read them from here; do not edit by hand |

> **Guardrails cost**: Bedrock Guardrails are enabled by default and add ~$0.75 per 1,000 text units on top of model inference costs. To disable, set `"enable_guardrails": false` in `cdk.json`. See [AWS Bedrock Guardrails Pricing](https://aws.amazon.com/bedrock/pricing/#Guardrails). Disabling removes content-level protections but other security layers (STS scoping, tool deny list, SSRF protection) remain active.

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

### Feishu

Feishu (飞书 / Lark) uses the Events API with the Router Lambda as the webhook endpoint (`POST /webhook/feishu`). Requests are validated with the `X-Lark-Signature` SHA-256 check (fail-closed if `encryptKey` is not set). The Lambda calls `open.feishu.cn` by default (`FEISHU_API_DOMAIN`), downloads image messages via `im/v1/images`, and caches the tenant access token.

1. Create an app with **Bot** capability at [open.feishu.cn/app](https://open.feishu.cn/app)
2. Run `./scripts/setup-feishu.sh` — it prints the Request URL for **Event Subscriptions**, stores the app credentials, and adds you to the allowlist
3. Or store the credentials manually (all four fields are read by the Lambda):
   ```bash
   aws secretsmanager update-secret \
     --secret-id openclaw/channels/feishu \
     --secret-string '{"appId":"cli_xxx","appSecret":"...","verificationToken":"...","encryptKey":"..."}' \
     --region $CDK_DEFAULT_REGION
   ./scripts/manage-allowlist.sh add feishu:YOUR_OPEN_ID
   ```

Scheduled-task delivery (see [Scheduled Tasks](#scheduled-tasks-cron-jobs)) currently posts to Telegram and Slack only; `lambda/cron/index.py` has a Feishu sender but `deliver_response` does not route to it. Design notes: [docs/design-feishu-channel.md](docs/design-feishu-channel.md).

## How It Works

### Per-User Sessions

Each user gets their own AgentCore microVM. When a user sends a message:

1. **Router Lambda** receives the webhook, resolves user identity in DynamoDB, and calls `InvokeAgentRuntime` with a per-user session ID
2. **Contract server** (port 8080) handles the invocation — on first message, it runs parallel initialization:
   - Creates STS scoped credentials restricting S3 to the user's namespace prefix
   - Starts the Bedrock proxy with `USER_ID`/`CHANNEL` env vars
   - Starts OpenClaw gateway with scoped credentials (container credentials stripped)
   - Restores `~/.openclaw/` from session storage, or from S3 when the mount is empty (awaited, bounded)
   - Starts credential refresh timer (45 min interval)
   - Waits for proxy only (~5s), then the **lightweight agent** handles the message immediately
3. **Lightweight agent** (warm-up phase; on us-west-2 staging the proxy was ready 165 ms after spawn and the first warm-up reply reached the E2E harness 23 s after the webhook) runs an agentic loop with 17 tools: `web_fetch`, `web_search`, S3 file storage (read/write/list/delete), EventBridge cron scheduling (create/list/update/delete), ClawHub skill management (install/uninstall/list), and API key management (native CRUD, Secrets Manager CRUD, unified retrieval, migration). Web tools include SSRF prevention (IP blocklists, DNS rebinding mitigation). All responses include a deterministic warm-up footer
4. **WebSocket bridge** (after OpenClaw ready; the 2.0 gateway logged `ready` 2.6 s after spawn on us-west-2 staging, but spawn itself waits for the S3 workspace restore, bounded by `WORKSPACE_RESTORE_WAIT_MS`, so the E2E harness measured 70 s from webhook to the first full-OpenClaw reply) takes over — messages route to OpenClaw which provides full tool profile, 5 ClawHub skills, and sub-agent support. Responses no longer have the warm-up footer
5. **Router Lambda** sends the response back to the channel API (Telegram, Slack or Feishu). While waiting, it sends typing indicators (Telegram) and a one-time progress message after 30s (Telegram and Slack) for long-running requests

When the session idles (default 30 min), AgentCore terminates the microVM. Before shutdown, the SIGTERM handler stops the gateway, snapshots `~/.openclaw/` (SQLite included) onto session storage and saves it to S3. The next message creates a fresh microVM and restores the state dir from session storage (or S3 when the mount is empty).

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

The agent can create, manage, and execute **recurring scheduled tasks** using Amazon EventBridge Scheduler. Schedules persist across sessions and fire even when the user is not chatting — the response is delivered to the user's Telegram or Slack channel automatically (Feishu delivery is not wired in the cron executor yet).

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

**Enable it:** Set `enable_browser` to `true` in `cdk.json` and ensure `BROWSER_IDENTIFIER` is configured in the AgentCore environment. The contract server creates a browser session on init, and the `agentcore-browser` skill scripts communicate with it via a session file.

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
   - Set up the state layout (local `~/.openclaw` incl. `workspace/`, mirror restored from session storage), clean stale lock files
   - Start `agentcore-proxy.js` (port 18790) with `USER_ID`/`CHANNEL` env vars
   - Restore `.openclaw/` from S3 via `workspace-sync.js` (awaited, bounded by `WORKSPACE_RESTORE_WAIT_MS`, default 45s)
   - Write `openclaw.json` + `AGENTS.md`; if a pre-2.0 `sessions.json` is present, run `openclaw doctor --fix` to import it into SQLite (see [docs/openclaw-2.0-upgrade.md](docs/openclaw-2.0-upgrade.md))
   - Start the workspace change watcher, then the OpenClaw gateway (port 18789, `OPENCLAW_STATE_DIR=~/.openclaw`) with scoped credentials (no container credentials)
   - Start credential refresh timer (45 min interval)
   - Wait for proxy only (165 ms measured on us-west-2 staging)
5. **Warm-up phase** (until the gateway is ready): `lightweight-agent.js` handles messages via proxy -> Bedrock (supports s3-user-files, eventbridge-cron, and clawhub-manage tools — users can manage files, schedules, and install skills immediately)
6. **Handoff**: OpenClaw becomes ready (2.6 s after spawn on us-west-2 staging; ~70 s after the first webhook once the bounded S3 restore wait is included), all subsequent messages route via WebSocket bridge
7. **After handoff**: Full OpenClaw features — built-in web tools (`web_search`, `web_fetch`), 5 ClawHub skills (jina-reader, deep-research-pro, telegram-compose, transcript, task-decomposer), sub-agent support, session management
8. **SIGTERM**: Stop the gateway, snapshot `~/.openclaw/` onto session storage, save it to S3, kill child processes, exit

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
| `clawhub-manage` | ClawHub skill installer — install, uninstall, and list community skills. Runtime installs are recorded per user (`~/.openclaw/runtime-skills.json`) and reinstalled in the background after a cold start |
| `api-keys` | Secure API key management — dual-mode storage with native file-based or AWS Secrets Manager backend (see [API Key Management](#api-key-management)) |
| `agentcore-browser` | Headless Chromium browser — navigate, screenshot, interact with web pages (optional, see [Browser Support](#browser-support-optional)) |

Five ClawHub community skills are pre-installed at Docker build time with `clawhub@0.23.3`. ClawHub now hosts duplicate slugs for three of them, and clawhub >= 0.23 refuses an ambiguous bare slug, so those are installed as owner-qualified, version-pinned specs (the owners and versions previously shipped in this image). The Dockerfile fails the build if any of the five is missing after install:

| ClawHub Skill | Install spec | Purpose |
|---|---|---|
| `jina-reader` | `jina-reader` | Extract web content as clean markdown |
| `deep-research-pro` | `@parags/deep-research-pro --version 1.0.2` | In-depth multi-step research (spawns sub-agents) |
| `telegram-compose` | `telegram-compose` | Rich HTML formatting for Telegram messages |
| `transcript` | `@therohitdas/transcript --version 1.4.1` | YouTube video transcript extraction |
| `task-decomposer` | `@10e9928a/task-decomposer --version 1.0.0` | Break complex requests into subtasks (spawns sub-agents) |

clawhub installs a qualified spec into `/skills/@owner/<slug>`; the Dockerfile moves it to the flat `/skills/<slug>` path that `skills.load.extraDirs`, the `clawhub-manage` skill and the system prompt all use.

Prototype: with `enable_gateway: true` the `s3-user-files` and `eventbridge-cron` capabilities are additionally served as typed MCP tools by an AgentCore Gateway; see [AgentCore Gateway MCP tools](#agentcore-gateway-mcp-tools-prototype) below.

During the warm-up phase (~first 1-2 min on cold start), the **lightweight agent shim** handles messages with built-in `web_fetch` and `web_search` tools, plus `s3-user-files`, `eventbridge-cron`, `clawhub-manage`, and `api-keys` skills. Users can manage files, schedules, skills, and API keys even during warm-up. ClawHub skills become available after OpenClaw fully starts.

### AgentCore Gateway MCP tools (prototype)

Off by default: `enable_gateway` is `false` in `cdk.json`, the `OpenClawGateway` stack is then not in the CDK app, the eight existing templates synthesize unchanged and `openclaw.json` gets no `mcp` block (asserted by `tests/test_gateway_stack_synth.py` and `bridge/gateway-mcp.test.js`). With the flag on, the `s3-user-files` and `eventbridge-cron` capabilities are additionally served as **typed MCP tools by an Amazon Bedrock AgentCore Gateway**, so the model calls `list_files` or `create_schedule` instead of composing a shell command, and the tool runs in a Lambda with its own least-privilege role instead of inside the user's microVM. The exec skills stay installed; both surfaces operate on the same S3 prefixes and the same `openclaw-cron` schedules. `api-keys` is not ported. Design, identity flow, IAM and live findings: [docs/gateway-mcp-tools.md](docs/gateway-mcp-tools.md).

| Piece | What it does | Code |
|---|---|---|
| `OpenClawGateway` stack | `AWS::BedrockAgentCore::Gateway` `openclaw-tools` (protocol MCP, `CUSTOM_JWT` authorizer against the existing Cognito user pool and its `openclaw-proxy` client), REQUEST interceptor Lambda `openclaw-gateway-interceptor`, and two Lambda MCP targets: `user-files` (`list_files`, `read_file`, `write_file`, `delete_file`) and `schedules` (`create_schedule`, `list_schedules`, `update_schedule`, `delete_schedule`). Outputs `GatewayUrl`, `GatewayId`, `GatewayServiceRoleArn` | `stacks/gateway_stack.py`, `lambda/gateway_tools/` |
| Bridge | When `AGENTCORE_GATEWAY_URL` is set, the contract server mints the per-user Cognito **access** token and writes `mcp.servers.agentcore` (`streamable-http`, `Authorization: Bearer ...`) into `openclaw.json`. A timer re-mints the token 5 min before expiry (Cognito tokens last 1 h), rewrites only the header (tmp file + rename) and retries a failed refresh every 60 s. If Cognito is not configured or the mint fails, the block is omitted with a warning and the exec skills keep working | `bridge/gateway-mcp.js`, `bridge/cognito-token.js`, `bridge/agentcore-contract.js` |
| Deploy | Phase 1 also deploys `OpenClawGateway`; Phase 2 reads the `GatewayUrl` output and passes `AGENTCORE_GATEWAY_URL` to the runtime, both only when the flag is on | `scripts/deploy.sh` |

**Auth and identity.** The Gateway accepts only the Cognito **access** token: the same user's ID token is refused with `403 insufficient_scope` (measured on us-west-2 staging, which is why the bridge uses `getAccessToken()`). Caller identity comes only from the verified JWT. The interceptor copies the bearer into the reserved tool argument `__caller_token`, overwriting anything the model sent, and refuses calls with no bearer; each tool Lambda re-verifies the signature against the pool JWKS (issuer, client id, expiry) and derives the S3 prefix / schedule owner from `cognito:username`. The tool schemas offer no `user_id` or `namespace` argument, and a model-supplied user or folder name is ignored (`lambda/gateway_tools/tools.test.js`, E2E `test_other_users_folder_is_never_reached`). Each Lambda role is scoped to the user-files bucket or to the `openclaw-cron` schedule group plus the identity table; the Gateway service role may invoke only these three functions.

**Enable / disable**

```bash
# enable: set "enable_gateway": true in cdk.json, then deploy
./scripts/deploy.sh                 # Phase 1 adds OpenClawGateway; Phase 2 passes AGENTCORE_GATEWAY_URL to the runtime
aws cloudformation describe-stacks --stack-name OpenClawGateway \
  --query "Stacks[0].Outputs[?OutputKey=='GatewayUrl'].OutputValue" --output text
# inside a user's live session, list the tools the Gateway serves and prove the bearer is accepted
# (operator tool, see docs/execute-command.md; the session id is the user's SESSION record):
python3 scripts/agentcore-exec.py --session-id <ses_...> --command 'openclaw mcp doctor agentcore --probe'

# disable: set "enable_gateway": false in cdk.json, then
./scripts/deploy.sh --runtime-only  # redeploys the runtime without AGENTCORE_GATEWAY_URL (rebuilds the image)
cdk destroy OpenClawGateway         # removes Gateway, targets, interceptor, tool Lambdas, log groups; files stay in S3, schedules in openclaw-cron
```

Container log lines to expect with the flag on: `[contract] Gateway MCP bearer acquired for telegram:<id> (expires ...)`, `[contract] OpenClaw headless config written (mcp.servers.agentcore enabled)`, and later `[contract] [gateway-mcp] bearer refreshed; next refresh before ...`. Tool calls appear in the container log as `tool=agentcore__user-files___list_files` (not `exec`) and in `/openclaw/lambda/gateway-userfiles` / `gateway-schedules` as `gateway_tool_call` audit lines carrying the caller's namespace and `tokenUse=access`.

**Staging measurements** (us-west-2, 2026-09-24, 10 cold-start iterations per phase, same prompts; exec skills vs Gateway tools): every chat and startup p50 moved by less than a second (cold start to first reply 4.61 s vs 5.41 s; boot to OpenClaw ready 10.25 s vs 10.59 s; plain chat 3.42 s vs 3.69 s), within the noise of a 10-sample set. The tool hop itself was faster through the Gateway on a cold call (300 ms vs 483 ms p50) and slightly slower on a warm repeat (267 ms vs 223 ms). Each model call carried 51 tool definitions instead of 38, a median of 29.0k input tokens vs 27.2k (+6.6 %). Correct replies 10/10 in both phases, no timeouts or errors. The full table is in [docs/gateway-mcp-tools.md](docs/gateway-mcp-tools.md#performance-us-west-2-staging-2026-09-24).

### Webhook Security

The Router Lambda validates all incoming webhook requests:

- **Telegram**: Validates the `X-Telegram-Bot-Api-Secret-Token` header against the `openclaw/webhook-secret` stored in Secrets Manager. The secret is registered with Telegram via the `secret_token` parameter on `setWebhook`.
- **Slack**: Validates the `X-Slack-Signature` HMAC-SHA256 header using the Slack app's signing secret. Includes 5-minute timestamp check to prevent replay attacks.
- **Feishu**: Validates the `X-Lark-Signature` header (`SHA256(timestamp + nonce + encryptKey + body)`). Rejects every request when `encryptKey` is not configured.
- **API Gateway**: Only explicit routes are exposed (`POST /webhook/telegram`, `POST /webhook/slack`, `POST /webhook/feishu`, `GET /health`). All other paths return 404 from API Gateway without invoking the Lambda. Rate limiting is applied (burst: 50, sustained: 100 req/s).

Requests that fail validation receive a 401 response and are logged with the source IP.

### Token Usage Tracking

Bedrock invocation logs flow to CloudWatch, where a Lambda processor extracts token counts, estimates costs, and writes to DynamoDB (single-table design with 4 GSIs for different query patterns). Custom CloudWatch metrics power the analytics dashboard and budget alarms.

## Operations

### Check runtime status

```bash
# The Runtime is created by the Starter Toolkit, not CDK; deploy.sh writes its id
# into cdk.json context (runtime_id) and .bedrock_agentcore.yaml.
RUNTIME_ID=$(python3 -c "import json; print(json.load(open('cdk.json'))['context']['runtime_id'])")

aws bedrock-agentcore-control get-agent-runtime \
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
cd bridge && node --test *.test.js                     # all bridge unit tests (412 tests, Node 24)
cd bridge && node --test proxy-identity.test.js       # identity + workspace tests
cd bridge && node --test image-support.test.js         # image upload + multimodal tests
cd bridge && node --test lightweight-agent.test.js     # lightweight agent tools + buildToolArgs tests
cd bridge && node --test subagent-routing.test.js      # subagent model routing + detection tests
cd bridge && node --test content-extraction.test.js    # recursive content block extraction tests
cd bridge && node --test scoped-credentials.test.js    # per-user STS credential scoping tests
cd bridge && node --test workspace-sync.test.js        # workspace sync + SQLite snapshot tests
cd bridge && node --test state-storage.test.js         # local state dir / session-storage mirror + restore tests
cd bridge && node --test gateway-mcp.test.js           # Gateway MCP config, bearer refresh, Cognito token provider (12 tests)
cd bridge && node --test runtime-skills.test.js        # runtime skill manifest + cold-start reinstall (38 tests)
node --test lambda/gateway_tools/*.test.js             # Gateway tool Lambdas: JWT verification, namespace scoping, interceptor (31 tests, Node 24)
cd bridge/skills/s3-user-files && AWS_REGION=$CDK_DEFAULT_REGION node --test common.test.js  # S3 skill tests
cd lambda/router && python -m pytest test_image_upload.py -v        # image upload unit tests
cd lambda/router && python -m pytest test_content_extraction.py -v  # content block extraction tests
cd lambda/router && python -m pytest test_markdown_html.py -v       # markdown-to-HTML conversion tests
cd lambda/router && python -m pytest test_slack.py test_feishu.py -v # Slack + Feishu handler tests
python -m pytest tests/test_agentcore_exec.py -v                     # operator CLI tests (mocked boto3)
python -m pytest tests/test_gateway_stack_synth.py -v                # OpenClawGateway synth: flag off leaves the 8 templates unchanged, flag on adds the stack (10 tests, no AWS)

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
pytest tests/e2e/bot_test.py -v -k GuardrailSecurity     # guardrail content filtering (requires BEDROCK_GUARDRAIL_ID env var, see below)
pytest tests/e2e/test_guardrail_wiring.py -v             # guardrail WIRING: passes only on a logged guardrail intervention
pytest tests/e2e/test_gateway_tools.py -v -m gateway     # Gateway MCP tools (4 tests; needs enable_gateway=true, skipped when OpenClawGateway is not deployed)
pytest tests/e2e/bot_test.py -v                          # all E2E tests
```

### Security validation

```bash
cdk synth   # Runs cdk-nag AwsSolutions checks — should produce no errors
```

## Upgrading from OpenClaw 2026.3.x

This branch moves the image from OpenClaw 2026.3.8 / Node 22 to **OpenClaw 2026.9.5 ("2.0") / Node 24 / clawhub 0.23.3**. Existing deployments upgrade by pushing the new image and bumping `image_version`; per-user state carries over:

- **Legacy sessions**: 2.0 stores sessions in per-agent SQLite and refuses readiness while a pre-2.0 `agents/<id>/sessions/sessions.json` is present. The contract runs `openclaw doctor --fix --non-interactive` once before the gateway spawns (bounded by `OPENCLAW_MIGRATION_TIMEOUT_MS`) to import it; an unreadable index is moved aside so the gateway still starts.
- **Behaviour-preserving config knobs** written into `openclaw.json` so users see no change: `session.reset: { mode: "daily", atHour: 4 }` (2.0 stopped resetting daily); the new `tools.profile: "full"` tools that need a Control UI or a human answer (`terminal`, `process`, `plugins`, `ask_user`, `secrets`, `screen`, `progress_card`, `nodes`, `heartbeat_respond`, media generation) added to `tools.deny`; `skills.workshop.autonomous.mode: "off"` (autonomous Skill Workshop); Active Memory cross-conversation recall and grounded dreaming disabled (`memory.search.rememberAcrossConversations: false`, `plugins.entries["active-memory"].enabled: false`, `plugins.entries["memory-core"].config.dreaming.enabled: false`).
- **WebSocket protocol 4** with `client.id: "gateway-client"`, `client.mode: "backend"` and no `Origin` header (see Gotchas).
- **State on local disk**, mirrored to session storage, because the mount has neither SQLite locks nor hard links (see [Session Storage](#session-storage-persistent-filesystem)).

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

## Known Limitations

| Limitation | Details |
|---|---|
| **Cold start time** | Lightweight agent answers first (23 s webhook-to-reply measured on us-west-2 staging, most of it Lambda + Bedrock); the 2.0 gateway is ready 2.6 s after spawn, but spawn waits up to `WORKSPACE_RESTORE_WAIT_MS` (45 s) for the S3 restore, so the first full-OpenClaw reply arrived ~70 s after the webhook |
| **Image size** | Max 3.75 MB per image (Bedrock Converse API limit) |
| **Session timeout** | Sessions terminate after 30 min idle (configurable via `session_idle_timeout`) |
| **ClawHub skills** | 5 pre-installed; available only after full OpenClaw startup. During warm-up, built-in web_fetch/web_search tools are available |
| **Single region** | AgentCore Runtime deployed in one region; no multi-region failover |
| **No voice/video** | Only text and images supported; no audio or video messages |

## Gotchas

- **ARM64 required**: AgentCore Runtime runs ARM64 containers. Build with `--platform linux/arm64`.
- **Push image after CDK deploy**: The CDK AgentCore stack creates the ECR repository. Do **not** manually create it beforehand (causes a `Resource already exists` error). Deploy CDK first, then push the image. AgentCore only pulls the image when a user session starts, not at deploy time.
- **AgentCore resource names**: Must match `^[a-zA-Z][a-zA-Z0-9_]{0,47}$` — use underscores, not hyphens.
- **Per-user sessions**: Contract returns `Healthy` (not `HealthyBusy`) — allows natural idle termination after `session_idle_timeout`.
- **VPC endpoints**: The `bedrock-agentcore-runtime` VPC endpoint is not created by `stacks/vpc_stack.py` (the service is not available in every region). Runtime API calls from the Lambdas go out via NAT; the Bedrock Runtime endpoint is created with private DNS disabled so `global.*` inference profiles can route cross-region.
- **CDK RetentionDays**: `logs.RetentionDays` is an enum, not constructable from int. Use the helper in `stacks/__init__.py`.
- **Cognito passwords**: HMAC-derived (`HMAC-SHA256(secret, actorId)`) — deterministic, never stored. Enables `AdminInitiateAuth` without per-user password storage.
- **`skills.allowBundled` is an array**: OpenClaw expects an array (the bridge writes `[]` and loads everything from `skills.load.extraDirs`); a boolean causes config validation failure.
- **ClawHub skills**: 5 community skills are pre-installed at Docker build time (jina-reader, `@parags/deep-research-pro`, telegram-compose, `@therohitdas/transcript`, `@10e9928a/task-decomposer`), flattened to `/skills/<slug>` next to the custom skills (s3-user-files, eventbridge-cron, clawhub-manage, api-keys) and loaded via `skills.load.extraDirs: ["/skills"]`. The build fails if any of the five is missing. Bare slugs that ClawHub now hosts under several owners are refused by clawhub >= 0.23 (`ambiguous`), so use `@owner/slug` and pin `--version`. Users can install/uninstall skills via the `clawhub-manage` skill. `/skills` is part of the image, not of the per-user state, so a runtime install is recorded (slug + pinned version) in `~/.openclaw/runtime-skills.json` — which is mirrored to session storage and backed up to S3 with the rest of the state dir — and `agentcore-contract.js` reinstalls every recorded skill in the background once the gateway is ready after a cold start (never on the `/ping` or first-reply path; a failed reinstall only logs and is retried at the next cold start). Runtime installs pass `--no-input` but never `--force`, so a skill ClawHub has flagged for security review is refused with an explanation.
- **ClawHub `--no-input --force` at image build**: still required with clawhub 0.23.3 for non-interactive Docker builds of the five pinned, reviewed skills (`--no-input` disables prompts; `--force` overrides the VirusTotal flag some skills carry for calling external APIs). Verified in the us-west-2 staging CodeBuild log: all five skills print `Installed <slug> v<version>`.
- **`default-user` fallback**: If identity resolution fails, requests fall back to `actorId = "default-user"` — meaning all such users share one S3 namespace. The `USER_ID` env var path (set by contract server) should prevent this in per-user mode.
- **actorId vs namespace format**: The actorId uses colon format (`telegram:123456789`) while skill scripts expect namespace/underscore format (`telegram_123456789`). The lightweight agent's `chat()` function converts via `userId.replace(/:/g, "_")` before passing to tool scripts. The proxy and workspace sync also use namespace format for S3 keys.
- **Image version bumps are required**: After pushing a new bridge container image, you must bump `image_version` in `cdk.json` and redeploy `OpenClawAgentCore`. AgentCore caches images by digest and only re-pulls when the runtime endpoint configuration changes. Without the bump, existing sessions continue using the old image.
- **Image upload size limit**: Bedrock Converse API limits images to 3.75 MB. The Router Lambda checks this before uploading to S3.
- **agentcore CLI urllib3 warnings**: The `agentcore` CLI may emit a `RequestsDependencyWarning` to stdout before its JSON output. This is benign — `deploy.sh` handles mixed output gracefully.
- **OpenClaw 2.0 WebSocket identity (protocol v4)**: The bridge connects with `minProtocol: 4, maxProtocol: 4`, `client.id: "gateway-client"`, `client.mode: "backend"` and **no `Origin` header**. On OpenClaw 2.0 (2026.8.1+) any browser-style `Origin` header or a Control-UI client id requires a signed device identity; `gateway-client`/`backend` on loopback token auth is the only device-less path that keeps `operator.*` scopes. The old `allowInsecureAuth`/`dangerouslyDisableDeviceAuth` config keys are retired. Details: [docs/openclaw-2.0-upgrade.md](docs/openclaw-2.0-upgrade.md). (Pre-2.0 note, kept for history: OpenClaw 2026.3.2 enforced origin checks on connections carrying an `Origin` header; the `ws` library needed the `origin` option and `allowedOrigins: ["*"]`. Without both the client `origin` option and config `allowedOrigins`, connections fail with: `Auth failed: origin not allowed`.)

## Cleanup

```bash
cdk destroy --all
```

Note: KMS keys and the Cognito User Pool have `RETAIN` removal policies and will not be deleted automatically. Remove them manually if needed. `OpenClawGateway` is only part of the app (and of `--all`) while `enable_gateway` is `true` in `cdk.json`.

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

### Guardrail wiring

The guardrail is only applied if two things are true at once, and `scripts/deploy.sh` sets both:

1. **IAM** — `app.py` passes the `OpenClawGuardrails` outputs into `AgentCoreStack`, which grants the runtime execution role `bedrock:ApplyGuardrail`. `tests/test_guardrail_wiring_synth.py` asserts this at synth time so a rebase cannot drop it again (it did once, in #30 — see #100).
2. **Runtime env** — Phase 2 of `scripts/deploy.sh` reads the `GuardrailId` / `GuardrailVersion` outputs of `OpenClawGuardrails` (exact `OutputKey` match) and passes them to the runtime as `BEDROCK_GUARDRAIL_ID` / `BEDROCK_GUARDRAIL_VERSION`. `bridge/agentcore-proxy.js` only injects `guardrailConfig` into Bedrock calls when `BEDROCK_GUARDRAIL_ID` is set. With `enable_guardrails: true` (the default) the deploy **fails** if either output resolves empty rather than silently shipping a runtime without guardrails; with `enable_guardrails: false` the variables are simply not set.

Exporting `BEDROCK_GUARDRAIL_ID` in your own shell does **not** configure the runtime — that only happens through `scripts/deploy.sh` (or `--runtime-only`). A runtime that has it set logs `[proxy] Bedrock Guardrails enabled: <id> v<version>` on startup.

### Guardrail E2E Tests

`tests/e2e/test_guardrail_wiring.py` verifies the wiring end to end. `test_guardrail_blocks_harmful_content` passes only when the runtime log (`/aws/bedrock-agentcore/runtimes/<runtime_id>-<endpoint>`) contains the proxy's `[guardrail] intervention ...` line, i.e. Bedrock returned `stopReason: guardrail_intervened`. A refusal written by the model itself does not count, so this test fails on a deployment where the guardrail is deployed but not wired.

```bash
pytest tests/e2e/test_guardrail_wiring.py -v
```

The `TestGuardrailSecurity` test class in `bot_test.py` (6 tests) exercises guardrail behaviour through the full Telegram webhook pipeline. It is gated on `BEDROCK_GUARDRAIL_ID` being set **in the test runner's shell** (this only selects the tests; the runtime gets its value from the deploy):

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
