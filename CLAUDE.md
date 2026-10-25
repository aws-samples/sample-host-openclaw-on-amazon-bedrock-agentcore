# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OpenClaw on AgentCore Runtime — a multi-channel AI messaging bot (Telegram, Slack) running as per-user serverless containers on AWS Bedrock AgentCore Runtime. Each user gets their own microVM with workspace persistence. A Router Lambda handles webhook ingestion from Telegram and Slack (text and images), resolves user identity via DynamoDB, and invokes per-user AgentCore sessions. Image uploads are stored in S3 and passed to Bedrock as multimodal content.

## Tech Stack

- **Infrastructure**: CDK v2 (Python), 7 stacks
- **AgentCore Runtime**: Bedrock AgentCore Runtime (serverless ARM64 container, VPC mode, per-user sessions)
- **Channel Ingestion**: Router Lambda behind API Gateway HTTP API (Telegram webhook, Slack Events API, image uploads)
- **Multimodal**: Image upload support — photos downloaded by Router Lambda, stored in S3, fetched by proxy, sent to Bedrock as multimodal content
- **Messaging**: OpenClaw (Node.js) — headless mode, messages bridged via WebSocket
- **Tools & Skills**: Built-in tool groups (full profile) + 5 ClawHub skills + 5 custom skills (S3 user files, EventBridge cron, ClawHub manage, API keys, agentcore-browser)
- **Scheduling**: EventBridge Scheduler for recurring tasks — cron executor Lambda warms sessions and delivers responses to channels
- **Per-User File Storage**: S3-backed per-user file isolation via custom `s3-user-files` skill
- **Workspace Persistence**: Local SQLite state, mirrored to AgentCore session storage (`/mnt/workspace`) with S3 backup. Strict restore completes before gateway activation; failure blocks readiness and uploads
- **AI Model**: Bedrock ConverseStream; see [README Configuration](README.md#configuration) for primary, fallback and subagent model defaults
- **Identity**: DynamoDB identity table (channel→user mapping, cross-channel binding) + Cognito User Pool
- **Observability**: CloudWatch dashboards + alarms, Bedrock invocation logging
- **Token Monitoring**: Lambda + DynamoDB (single-table) + CloudWatch custom metrics
- **API Key Management**: Dual-mode storage — native file-based (S3-synced) or AWS Secrets Manager (KMS-encrypted, CloudTrail-auditable) via `api-keys` skill
- **Security**: VPC endpoints, KMS CMK, Secrets Manager, cdk-nag. `SECURITY.md` is a thin policy pointer; `docs/security.md` is the single source of truth for the full security architecture

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
  |   -> bootstrap: prepare user-neutral gateway for snapshot
  |   -> first bound request: credentials, strict restore, config + skills
  |   -> bind eligible gateway, or start for restored/custom state
  |   -> USER_READY: full OpenClaw handles chat and cron
  |   -> SIGTERM: drain credentials and flush state before cleanup
  |                       |
  | agentcore-proxy.js    (18790) -- OpenAI -> Bedrock ConverseStream
  | OpenClaw Gateway      (18789) -- headless, no channels
  +-----------+-----------+
              |
  +-----------v-----------+
  |   Amazon Bedrock      |
  |   ConverseStream API  |
  |   MiniMax M2.1      |
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
    security_stack.py             # KMS CMK, Secrets Manager, Cognito, optional CloudTrail
    agentcore_stack.py            # Foundation: S3, IAM, security group, Browser (AgentCore Runtime via SDK)
    router_stack.py               # Router Lambda + API Gateway HTTP API + DynamoDB identity
    observability_stack.py        # Dashboards, alarms, Bedrock logging
    token_monitoring_stack.py     # Lambda processor, DynamoDB, token analytics
    cron_stack.py                 # EventBridge Scheduler, Cron executor Lambda, IAM
  bridge/
    Dockerfile                    # Container image (node:24-slim, ARM64, OpenClaw 2026.9.5, clawhub skills)
    entrypoint.sh                 # Startup: configure IPv4, start contract server
    agentcore-contract.js         # AgentCore HTTP contract with V2 readiness and full OpenClaw
    agentcore-proxy.js            # OpenAI -> Bedrock ConverseStream adapter + Identity + multimodal images
    image-support.test.js         # Image support unit tests (node:test)
    content-extraction.test.js    # Content block extraction tests (node:test)
    workspace-sync.js             # .openclaw/ directory S3 sync (restore/save/periodic)
    scoped-credentials.js         # Per-user STS session-scoped credentials (S3, Secrets Manager, DynamoDB)
    scoped-credentials.test.js    # Scoped credentials unit tests (node:test)
    workspace-sync.test.js        # Workspace sync credential tests (node:test)
    force-ipv4.js                 # DNS patch for Node.js Happy Eyeballs IPv6 issue
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
      clawhub-manage/             # ClawHub skill installer (install/uninstall/list)
        SKILL.md                  # OpenClaw skill manifest
        common.js                 # Skill name validation
        install.js / uninstall.js # Install/uninstall ClawHub skills
        list.js                   # List installed skills
      api-keys/                   # Dual-mode API key management (native + Secrets Manager)
        SKILL.md                  # OpenClaw skill manifest
        common.js                 # Shared validation (userId, keyName)
        native.js / secret.js    # Native file CRUD / Secrets Manager CRUD
        retrieve.js              # Unified lookup (SM first, native fallback)
        migrate.js               # Move keys between backends
      agentcore-browser/          # Headless browser skill (optional, enable_browser=true)
        SKILL.md                  # OpenClaw skill manifest
        common.js                 # Session file reader, S3 upload helper
        navigate.js               # Navigate to URL, return title + content
        screenshot.js             # Capture PNG screenshot, upload to S3
        interact.js               # Click, type, scroll, wait on elements
  lambda/
    token_metrics/index.py        # Bedrock log -> DynamoDB + CloudWatch metrics
    router/index.py               # Webhook router (Telegram + Slack, image uploads)
    router/test_image_upload.py        # Image upload unit tests (pytest)
    router/test_content_extraction.py  # Content block extraction tests (pytest)
    router/test_markdown_html.py       # Markdown-to-HTML conversion tests (pytest)
    cron/index.py                      # Cron executor (warmup, invoke, deliver to channel)
  scripts/
    setup-telegram.sh             # Telegram webhook + admin allowlist (one-step)
    setup-slack.sh                # Slack Event Subscriptions + admin allowlist
    manage-allowlist.sh           # Add/remove/list users in the allowlist
  tests/
    e2e/                          # E2E tests (simulated Telegram webhooks + CloudWatch logs)
  docs/
    architecture.md               # Detailed architecture diagrams
    architecture-detailed.md      # Technical deep-dive (sequence diagrams, container internals, data flows)
    security.md                   # Complete security architecture (single source of truth — threat model, 10 defense layers, operations runbook)
```

## CDK Stacks (7 stacks)

| Stack | Key Resources | Dependencies |
|---|---|---|
| **OpenClawVpc** | VPC (2 AZ), subnets, NAT, 7 VPC endpoints, flow logs | None |
| **OpenClawSecurity** | KMS CMK, Secrets Manager (8 secrets incl. webhook validation + feishu), Cognito User Pool, optional CloudTrail | None |
| **OpenClawAgentCore** | Execution Role, Security Group, S3 bucket (AgentCore Runtime/Endpoint managed by the pinned SDK controller) | Vpc, Security |
| **OpenClawRouter** | Lambda, API Gateway HTTP API (explicit routes, throttling), DynamoDB identity table | AgentCore, Security |
| **OpenClawObservability** | Operations dashboard, alarms, SNS, Bedrock invocation logging | None |
| **OpenClawTokenMonitoring** | DynamoDB (single-table, 4 GSIs), Lambda processor, analytics dashboard | Observability |
| **OpenClawCron** | EventBridge Scheduler group, Cron executor Lambda, Scheduler IAM role | AgentCore, Router, Security |

## Expected Commands

### V2 deployment (CDK + pinned SDK)

CDK handles foundation/dependent stacks; Phase 2 sets `platformVersion="V2"` through boto3/botocore 1.43.95. See [Quick Start](README.md#quick-start) and the [AgentCore Runtime V2 runbook](docs/runtime-v2.md) for setup and the CloudFormation/CDK limitation.

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
uv venv .venv-deploy
uv pip sync --python .venv-deploy/bin/python requirements-deploy.txt
./scripts/deploy.sh                  # Fresh V2 release_1 and all phases
./scripts/deploy.sh --phase1          # Foundation without a manifest
./scripts/deploy.sh --runtime-only    # SDK deployment + real-chat warmup
./scripts/deploy.sh --phase3          # Verify manifest before dependent stacks
```

CDK receives `.runtime/runtime-v2.json` as `runtime_manifest`; Phase 3 requires successful warmup and readback. Platform selectors, V1 fallback and enrollment opt-in flags are unsupported. Retain persisted SESSION records and existing users' release bindings.

### Other CDK commands

```bash
cdk synth -c runtime_manifest=.runtime/runtime-v2.json
cdk diff -c runtime_manifest=.runtime/runtime-v2.json
```

CDK teardown excludes SDK-managed AgentCore Runtime/endpoints. Reconcile active routes and retain recovery manifests before separately retiring AgentCore Runtime resources.

### Webhook Setup (Telegram)

The setup script registers the webhook and adds you to the allowlist in one step:
```bash
./scripts/setup-telegram.sh
```

Or manually:
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

# Add yourself to the allowlist (find your ID via @userinfobot on Telegram)
./scripts/manage-allowlist.sh add telegram:YOUR_TELEGRAM_USER_ID
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

### Slack Setup (Event Subscriptions + Allowlist)
```bash
./scripts/setup-slack.sh
```
This displays the webhook URL for Slack Event Subscriptions, prompts for your Slack member ID, and adds you to the allowlist.

### Feishu Setup (Event Subscriptions + Allowlist)
```bash
./scripts/setup-feishu.sh
```
This displays the webhook URL, guides you through Feishu developer console setup (app creation, permissions, events, publishing), stores credentials in Secrets Manager, and adds you to the allowlist. Store credentials format: `{"appId":"...","appSecret":"...","verificationToken":"...","encryptKey":"..."}`

### Deploy a new bridge version

Archive the prior release manifest, select an unused immutable endpoint name and use the controller:

```bash
./scripts/deploy.sh --runtime-only --endpoint-name release_2
./scripts/deploy.sh --phase3
```

Supply `--image-uri "$IMAGE_URI"` for a reviewed prebuilt ARM64 image. Existing sessions retain their saved AgentCore Runtime/endpoint/version; stopping one does not upgrade its route. Use the selected user's SESSION route for any session-specific diagnostics or stop request. Never substitute a legacy CDK AgentCore Runtime default or delete the SESSION to trigger registration.

### Bridge Tests
```bash
cd bridge && node --test proxy-identity.test.js       # identity + workspace tests
cd bridge && node --test image-support.test.js         # image upload + multimodal tests
cd bridge && node --test subagent-routing.test.js      # subagent model routing + detection tests
cd bridge && node --test content-extraction.test.js    # recursive content block extraction tests
cd bridge && node --test scoped-credentials.test.js    # per-user STS credential scoping tests
cd bridge && node --test workspace-sync.test.js        # workspace sync credential tests
cd bridge && node --test agentcore-browser.test.js     # browser skill unit tests
cd bridge && node --test browser-lifecycle.test.js     # browser session lifecycle tests
cd bridge/skills/s3-user-files && AWS_REGION=$CDK_DEFAULT_REGION node --test common.test.js  # S3 skill tests
```

### Router Lambda Tests

Install the [pinned SDK and pytest](docs/runtime-v2.md#offline-checks), then run each command independently from the repository root.

```bash
PYTHONPATH=lambda/shared/python:lambda/router uv run --no-project --python .venv/bin/python python -m pytest lambda/router/test_image_upload.py -v        # image upload unit tests
PYTHONPATH=lambda/shared/python:lambda/router uv run --no-project --python .venv/bin/python python -m pytest lambda/router/test_content_extraction.py -v  # content block extraction tests
PYTHONPATH=lambda/shared/python:lambda/router uv run --no-project --python .venv/bin/python python -m pytest lambda/router/test_markdown_html.py -v       # markdown-to-HTML conversion tests
PYTHONPATH=lambda/shared/python:lambda/router uv run --no-project --python .venv/bin/python python -m pytest lambda/router/test_screenshot_handling.py -v # screenshot marker detection + delivery tests
PYTHONPATH=lambda/shared/python:lambda/router uv run --no-project --python .venv/bin/python python -m pytest lambda/router/test_model_switching.py -v   # /model per-user model switching tests
```

### E2E Tests
```bash
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v                    # simulated Telegram webhook tests (requires deployed stack)
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k TestBrowserFeature  # browser E2E tests (requires enable_browser=true)
```

### AgentCore Runtime Operations

```bash
uv run --no-project --python .venv-deploy/bin/python python scripts/runtime_deploy.py verify \
  --region "$CDK_DEFAULT_REGION" --manifest .runtime/runtime-v2.json --require-enrollment-ready
aws logs tail /openclaw/lambda/router --region "$CDK_DEFAULT_REGION" --since 10m
aws logs tail /openclaw/container --region "$CDK_DEFAULT_REGION" --since 10m
```

For user status, `tests/e2e/session.py:get_agent_status` reads the saved V2 route and checks binding metadata. `--stop-session` stops that session and retains identity records; it proves neither a checkpoint nor a cold-start reset. Live E2E uses real AWS resources, model/tool calls and channel messages, so use a dedicated V2 test identity and configuration.

### Docker Build & Push (local)
```bash
# Build ARM64 image from current branch
cd bridge && sudo docker build --platform linux/arm64 -t openclaw-bridge:latest .

# Test locally before pushing
sudo docker run --rm -d --name test -p 8080:8080 -e AWS_REGION=$CDK_DEFAULT_REGION openclaw-bridge:latest
curl http://localhost:8080/ping   # should return {"status":"Healthy",...}
sudo docker stop test

# Login + push to ECR
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
aws ecr get-login-password --region $CDK_DEFAULT_REGION | sudo docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com
sudo docker tag openclaw-bridge:latest $ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:local-build
sudo docker push $ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:local-build
```

## Key Configuration (cdk.json)

| Parameter | Default | Description |
|---|---|---|
| `account` | (empty) | AWS account ID. Falls back to `CDK_DEFAULT_ACCOUNT` |
| `region` | `us-west-2` | AWS region. Falls back to `CDK_DEFAULT_REGION` |
| `default_model_id`, `fallback_model_id` | [README Configuration](README.md#configuration) | Primary and fallback Bedrock model IDs |
| `runtime_manifest` | (empty for foundation) | Verified V2 manifest; wrapper default `.runtime/runtime-v2.json`, required for dependent stacks |
| `image_version` | `1` | Local build image tag; changed releases require a new immutable endpoint through the SDK controller |
| `cloudwatch_log_retention_days` | `30` | Log retention |
| `hourly_token_alarm` | `5000000` | `openclaw-hourly-token-spike` threshold (TotalTokens Sum over 1 h) |
| `daily_token_budget` | `18000000` | `openclaw-daily-token-budget` threshold (TotalTokens Sum over 1 day). Was compared per hour before the alarm split |
| `hourly_cost_alarm_usd` | `30` | `openclaw-hourly-cost-spike` threshold (EstimatedCostUSD Sum over 1 h) |
| `daily_cost_budget_usd` | `100` | `openclaw-daily-cost-budget` threshold (EstimatedCostUSD Sum over 1 day). Was compared per hour before the alarm split; move an hourly value to `hourly_cost_alarm_usd` |
| `alarm_email` | `""` | Optional email subscription on `openclaw-alarms`; unset, alarms notify nobody |
| `token_ttl_days` | `90` | DynamoDB TTL |
| `user_files_ttl_days` | `365` | S3 per-user file expiration |
| `session_idle_timeout` | `1800` | Per-user session idle timeout (seconds) |
| `session_max_lifetime` | `28800` | Per-user session max lifetime (seconds) |
| `workspace_sync_interval_seconds` | `300` | .openclaw/ S3 sync interval |
| `router_lambda_timeout_seconds` | `300` | Router Lambda timeout |
| `router_lambda_memory_mb` | `256` | Router Lambda memory |
| `router_stage_rate_limit` | `100` | API Gateway stage-wide steady-state throttle (requests/second) shared by every webhook route |
| `router_stage_burst_limit` | `50` | API Gateway stage-wide burst throttle |
| `router_get_route_rate_limit` | `10` | Per-route throttle (requests/second) for `GET /health` and `GET /webhook/whatsapp` (Meta's subscription check), the two routes that answer without a channel signature. Signed `POST` webhooks keep the stage-wide limits. Over the limit, API Gateway returns `429` |
| `router_get_route_burst_limit` | `20` | Per-route burst throttle for the same two `GET` routes |
| `router_reserved_concurrency` | `0` (unset) | Opt-in Lambda reserved concurrency for the Router. Caps the Router and guarantees it that many executions; also removes them from the account's unreserved pool, and Lambda rejects a reservation that would leave fewer than 100 unreserved, so the deploy fails in low-limit accounts. Each in-flight chat turn holds one Router execution for up to `router_lambda_timeout_seconds` |
| `registration_open` | `false` | If true, any user can register. If false, only allowlisted users |
| `cron_lambda_timeout_seconds` | `600` | Cron executor Lambda timeout (must exceed warmup time) |
| `cron_lambda_memory_mb` | `256` | Cron executor Lambda memory |
| `cron_reserved_concurrency` | `0` (unset) | Opt-in Lambda reserved concurrency for the Cron executor (same trade-off as `router_reserved_concurrency`) |
| `token_metrics_reserved_concurrency` | `0` (unset) | Opt-in Lambda reserved concurrency for the token-metrics Lambda (same trade-off) |
| `enable_cloudtrail` | `false` | Deploy a dedicated multi-region CloudTrail (S3 bucket + trail) that also records S3 data events on the user-files bucket. Off by default — most accounts already have one |
| `cognito_advanced_security_mode` | `OFF` | Cognito threat protection for the user pool: `OFF`, `AUDIT` or `ENFORCED`. Off by default. `AUDIT` and `ENFORCED` also switch the pool to the Plus feature plan, which is billed per monthly active user |
| `cron_lead_time_minutes` | `5` | Minutes before schedule time to start warmup |
| `subagent_model_id` | `global.anthropic.claude-sonnet-5-5` | Bedrock model for sub-agents. Empty = bridge default (also Sonnet 5.5) |
| `enable_browser` | `false` | Enable headless Chromium browser. Sets `BROWSER_IDENTIFIER` env var on container |

## Container Startup Sequence

See the [AgentCore Runtime V2 lifecycle](docs/runtime-v2.md#lifecycle-and-compatibility) for gateway preparation, strict restore and `USER_READY`. `action: warmup` uses the same readiness barrier; `action: status` reports lifecycle without activating a user. Historical status latency does not measure current-image chat or `USER_READY`.

## DynamoDB Identity Table Schema

**Table: `openclaw-identity`** (PAY_PER_REQUEST, TTL on `ttl` attribute)

| PK | SK | Purpose |
|---|---|---|
| `CHANNEL#telegram:123456789` | `PROFILE` | Channel→user lookup |
| `USER#user_abc123` | `PROFILE` | User profile |
| `USER#user_abc123` | `CHANNEL#telegram:123456789` | User's bound channels |
| `USER#user_abc123` | `SESSION` | Current session |
| `BIND#ABC123` | `BIND` | Cross-channel bind code (10 min TTL) |
| `ALLOW#telegram:123456789` | `ALLOW` | User allowlist entry |
| `USER#user_abc123` | `CRON#schedule-name` | User's cron schedule metadata (expression, message, timezone, channel) |

**Cross-channel binding**: User says "link accounts" on Telegram → gets 6-char code → enters code on Slack → both channels route to same user/session.

### User Allowlist

When `registration_open` is `false` (default), only users with an `ALLOW#` record in DynamoDB can register. Existing users (already have a `CHANNEL#` record) are always allowed. Cross-channel binding bypasses the allowlist since it links to an already-approved user.

Unauthorized users who message the bot receive a rejection message that includes their channel ID (e.g. `telegram:123456`), so they can share it with the admin for onboarding.

#### First-User Bootstrap

After initial deployment, no users exist. The easiest path is the setup script, which registers the webhook and adds you to the allowlist in one step:

```bash
./scripts/setup-telegram.sh
```

Alternatively, if you don't know your Telegram user ID:

1. Message the bot from Telegram
2. The bot replies with a rejection message showing your ID, e.g. `telegram:123456`
3. Add yourself to the allowlist:
   ```bash
   ./scripts/manage-allowlist.sh add telegram:123456
   ```
4. Message the bot again — you are now registered

#### Adding New Users

When someone wants access to the bot:

1. They message the bot and receive: *"Your ID: `telegram:789012`. Send this ID to the bot admin to request access."*
2. The admin adds them:
   ```bash
   ./scripts/manage-allowlist.sh add telegram:789012
   ```
3. The user messages the bot again — they are now registered

#### Managing the Allowlist

```bash
# Add a user to the allowlist
./scripts/manage-allowlist.sh add telegram:123456

# Remove a user
./scripts/manage-allowlist.sh remove telegram:123456

# List all allowed users
./scripts/manage-allowlist.sh list
```

Only the **first channel identity** needs to be allowlisted. When a user binds a second channel (e.g. Slack) via `link`, the new channel maps to their existing approved user — no separate allowlist entry needed.

## Gotchas

### AgentCore Runtime
- **V2 deploy**: the pinned SDK controller manages AgentCore Runtime/endpoints and the build helper handles ECR/image inputs. CDK manages foundation/dependent stacks.
- **ARM64 required**: Build with `--platform linux/arm64`. Use Docker buildx or supply a reviewed ARM64 image with `--image-uri`
- **Docker Hub rate limit**: Dockerfile uses `public.ecr.aws/docker/library/node:24-slim` (ECR Public Gallery) instead of Docker Hub to avoid anonymous pull rate limits in CodeBuild
- **IAM role names are region-suffixed**: `openclaw-agentcore-execution-role-{region}` and `openclaw-cron-scheduler-role-{region}` to avoid cross-region conflicts (IAM roles are global)
- **Trust policy self-assume**: Uses `AccountRootPrincipal()` + `ArnEquals` condition (not `ArnPrincipal`) to avoid chicken-and-egg during role creation
- **AgentCore Runtime configuration**: use the pinned controller to preserve Get fields, apply reviewed overrides, verify readback and create an immutable release endpoint.
- **Resource names**: Must match `^[a-zA-Z][a-zA-Z0-9_]{0,47}$` — underscores, not hyphens
- **Health check timing**: Contract server on port 8080 must start within seconds
- **Per-user sessions**: Contract returns `Healthy` (not `HealthyBusy`) — allows natural idle termination
- **Session ownership**: retain the saved V2 route; missing or incomplete records require reconciliation
- **VPC endpoints**: `bedrock-agentcore-runtime` endpoint not available in all regions
- **Build context**: `bridge/` contains the Dockerfile and COPY inputs; the wrapper builds ARM64 or accepts a prebuilt image.

### IAM / Bedrock
- **Cross-region inference**: Model `minimax.minimax-m2.1` uses a global cross-region inference profile that routes to any available region — IAM uses `arn:aws:bedrock:*::foundation-model/*` and inference-profile wildcards
- **Inference profile ARN**: Separate from foundation model — `arn:aws:bedrock:{region}:{account}:inference-profile/*`

### Node.js + VPC
- **IPv6 issue**: Node.js Happy Eyeballs (20+) fails in VPCs without IPv6 — `force-ipv4.js` patches `dns.lookup()` to force IPv4
- **NODE_OPTIONS**: `--dns-result-order=ipv4first --no-network-family-autoselection -r /app/force-ipv4.js`

### CDK
- `logs.RetentionDays` is an enum — use helper in `stacks/__init__.py`
- Cross-stack cyclic deps: use string ARN params + `add_to_policy()` instead of `grant_*()`
- Empty `cdk.json` account: falls back to `CDK_DEFAULT_ACCOUNT` env var via `app.py`

### OpenClaw
- Startup follows the [AgentCore Runtime V2 lifecycle contract](docs/runtime-v2.md#lifecycle-and-compatibility); chat waits for `USER_READY`.
- Correct start command: `openclaw gateway run --port 18789 --verbose` (no `--bind lan` — localhost binding sufficient since both processes run in the same container)
- **Tool profile**: Uses `"full"` profile with a deny list. Do NOT use `"basic"` (undocumented, may disable web tools). Documented profiles: `minimal`, `coding`, `messaging`, `full`
- **Deny list**: `["write", "edit", "apply_patch", "read", "browser", "canvas", "cron", "gateway"]` — local writes use S3 skill, `read` blocked to prevent credential reads, no browser/UI in container, EventBridge replaces built-in cron. `exec` is NOT denied — skills like `clawhub-manage` need it; scoped STS credentials limit blast radius
- **Sub-agent sandbox**: Must be `"off"` — no Docker inside AgentCore microVMs. MicroVMs already provide per-user isolation
- **Sub-agent model**: Configurable via `SUBAGENT_BEDROCK_MODEL_ID` env var (from `subagent_model_id` in cdk.json). Default and empty = `global.anthropic.claude-sonnet-5-5`; the main agent defaults to `global.anthropic.claude-opus-5-5`. Subagents use a distinct model name (`bedrock-agentcore-subagent`) so the proxy can detect and count them separately
- **`skipBootstrap` removed**: No longer a valid config key — OpenClaw rejects unknown keys and exits with code 1
- **`skills.allowBundled`**: Must be an array (e.g., `[]` for none, `["*"]` for all), not a boolean. Set to `[]` for fast startup
- **ClawHub skill paths**: `clawhub install` installs to managed skills path — OpenClaw scans this automatically. Custom skills in `/skills/` loaded via `extraDirs`
- **ClawHub VirusTotal flags**: Some skills flagged for external API calls — use `--no-input --force` for non-interactive Docker builds
- **5 ClawHub skills installed**: jina-reader, deep-research-pro, telegram-compose, transcript, task-decomposer (reduced from 8 — duckduckgo-search, hackernews, news-feed removed to optimize cold start)
- **Image updates**: existing saved routes keep their endpoint/version; a new deployment does not automatically migrate them
- **WebSocket bridge protocol**: Connect → auth (type:req, method:connect, minProtocol/maxProtocol:4, client:{id:"gateway-client", mode:"backend"}, auth:{token}, NO Origin header) → chat.send → streaming deltas → final
- **OpenClaw 2.0 WebSocket identity**: The bridge must connect WITHOUT an `Origin` header and identify as `client.id: "gateway-client"`, `client.mode: "backend"` on gateway protocol v4. Any `Origin` header or a Control-UI client id makes 2.0 demand a signed device identity (`reject-control-ui-insecure-auth`, close 1008), and other device-less token clients get their `operator.*` scopes cleared (`chat.send` then fails with a missing-scope error). `controlUi.allowInsecureAuth` (dead key) and `dangerouslyDisableDeviceAuth` (retired) must NOT be emitted — the startup doctor would rewrite `openclaw.json` + `.bak` every boot. Config still sets `controlUi: { enabled: false, dangerouslyAllowHostHeaderOriginFallback: true, allowedOrigins: ["*"] }`. See `docs/openclaw-2.0-upgrade.md`
- **Workspace sync overwrites config**: The `.openclaw/` S3 sync can overwrite `openclaw.json` with stale configs. `openclaw.json` is excluded from sync via SKIP_PATTERNS — config is always programmatically generated by `writeOpenClawConfig()`

### Cognito Identity
- Self-signup disabled — users auto-provisioned by proxy via `AdminCreateUser`
- Passwords: `HMAC-SHA256(secret, actorId).slice(0, 32)` — deterministic, never stored
- Usernames are channel-prefixed: `telegram:123456789`
- JWT tokens cached per user with 60s early refresh

### Router Lambda
- **API Gateway HTTP API**: Only explicit routes exposed (`POST /webhook/telegram`, `POST /webhook/slack`, `GET /health`). Rate limiting: burst 50, sustained 100 req/s
- **Webhook validation**: Telegram uses `X-Telegram-Bot-Api-Secret-Token` header (set via `secret_token` on `setWebhook`). Slack uses `X-Slack-Signature` HMAC-SHA256 with 5-minute replay window
- **Async dispatch**: Self-invokes with `InvocationType=Event` for actual processing; returns 200 immediately to webhook
- **Slack**: Handles `url_verification` challenge synchronously; ignores retries via `x-slack-retry-num` header
- **Cold start latency**: measure user activation and chat separately from historical unbound status timing.
- **Typing indicator + progress message**: Telegram typing indicator sent every 4s while waiting; after 30s of waiting, a one-time progress message ("Working on your request...") is sent to both Telegram and Slack so users know the bot is still working during long subagent tasks
- **Content block extraction**: `_extract_text_from_content_blocks()` recursively unwraps nested `[{"type":"text","text":"..."}]` JSON — subagent responses (deep-research-pro, task-decomposer) can wrap content multiple levels deep
- **Markdown-to-HTML conversion**: `_markdown_to_telegram_html()` converts markdown to Telegram-compatible HTML before sending. Handles bold, italic, strikethrough, code blocks, inline code, headers, links, blockquotes, horizontal rules, and markdown tables (rendered as monospace `<pre>` blocks with aligned columns). Uses `parse_mode: "HTML"` (not `"Markdown"` v1 which is too strict for AI-generated content)
- **Cross-channel binding**: "link accounts" generates 6-char code in DynamoDB with 10-min TTL
- **Image uploads**: Telegram photos and Slack file attachments (JPEG, PNG, GIF, WebP, max 3.75 MB) are downloaded by the Router Lambda, uploaded to S3 under `{namespace}/_uploads/`, and passed to AgentCore as a structured message `{text, images[{s3Key, contentType}]}`
- **Telegram captions**: `message.get("text", "") or message.get("caption", "")` — photos use `caption`, not `text`
- **Secret cache TTL**: Secrets Manager values cached for 15 minutes (was indefinite). Rotated secrets reflected within 15 min without container restart

### Image Upload Flow
- **Router Lambda** downloads image from channel API (Telegram `getFile` / Slack `url_private_download`), uploads to S3 `{namespace}/_uploads/img_{ts}_{hex}.{ext}`
- **Contract server** converts structured message to bridge text with `[OPENCLAW_IMAGES:[...]]` marker appended
- **Proxy** extracts marker via regex, fetches image bytes from S3 (with namespace validation to prevent cross-user reads), builds Bedrock multimodal content blocks (`{image: {format, source: {bytes}}}`)
- **Supported types**: `image/jpeg`, `image/png`, `image/gif`, `image/webp` (max 3.75 MB per Bedrock limit)
- **Security**: S3 key validated against user's namespace prefix + path traversal (`..`) rejection. Format validated against `VALID_BEDROCK_FORMATS` set
- **Slack prerequisite**: Bot needs `files:read` OAuth scope to download image files

### Workspace Persistence (Session Storage + S3 Backup)
- **Primary**: AgentCore Session Storage — service-managed persistent filesystem mounted at `/mnt/workspace`. Data survives session stop/resume automatically. Configured via `filesystemConfigurations` on the AgentCore Runtime
- **Local state**: `~/.openclaw` holds SQLite and workspace files on local disk; `/mnt/workspace/.openclaw` holds the session-storage mirror
- **S3 backup**: `workspace-sync.js` continues to run at 5 min interval (unchanged). Backs up to `{namespace}/.openclaw/` in the user files S3 bucket
- **Restore logic**: User activation restores the mount's mirror to local disk, then awaits S3 restore. A non-empty mount still restores missing files from S3 without overwriting restored files; failure or timeout blocks activation
- **Fallback**: If session storage mount not available → full S3 sync mode (5 min interval, existing behavior)
- **Data lifecycle**: Session storage cleared on 14-day inactivity. Verify S3 backups before relying on recovery
- **Version updates**: Verify recoverable backups and retain existing routes and dependencies; see [existing-user migration limits](docs/runtime-v2.md#existing-user-migration-and-cutover)
- **VPC permissions**: S3 Gateway Endpoint defaults to allow-all — no policy change needed. Session storage is managed by AgentCore platform (not the execution role), so no IAM changes required
- **SIGTERM grace**: effectively none on AgentCore (staging: process gone < 5 s after SIGTERM; idle stops send none). Changed state is uploaded to S3 ~5 s after it changes (`workspaceSync.startChangeBackup()`; SQLite-only changes at most every 5 min, since the gateway heartbeats into `state/openclaw.sqlite` every 30 s); the SIGTERM handler flushes what is still dirty first, then best-effort mirror + full save (hard exit after 10 s + `GATEWAY_STOP_WAIT_MS`). No upload happens before the S3 restore has completed; a failed restore disables uploads for that container. A non-empty session-storage mount no longer skips the S3 restore — missing files are filled in from S3
- **Skip patterns**: `node_modules/`, `.cache/`, `*.log`, files > 10MB (S3 backup only)
- **Same S3 bucket**: Uses `S3_USER_FILES_BUCKET` (shared with s3-user-files skill)

### EventBridge Cron Scheduling
- **Schedule group**: All schedules created under `openclaw-cron` group in EventBridge Scheduler
- **Schedule naming**: `openclaw-{namespace}-{shortId}` (e.g., `openclaw-telegram_123456789-87a86927`)
- **DynamoDB storage**: Schedule metadata stored as `CRON#` SK under the user's PK in the identity table
- **Cron executor Lambda**: Warms up the user's AgentCore session (sends `action: warmup`), then sends the cron message (sends `action: cron`), then delivers the response to the user's chat channel
- **Lead time**: Cron Lambda invoked with `cron_lead_time_minutes` (default 5 min) to allow session warmup before the scheduled time
- **Environment variables**: Container receives `EVENTBRIDGE_SCHEDULE_GROUP`, `CRON_LAMBDA_ARN`, `EVENTBRIDGE_ROLE_ARN`, `IDENTITY_TABLE_NAME`, `CRON_LEAD_TIME_MINUTES` for the eventbridge-cron skill

### AgentCore Browser (Optional)
- **Opt-in**: Only active when `enable_browser=true` in `cdk.json` and `BROWSER_IDENTIFIER` is set as container env var
- **Session file**: `/tmp/agentcore-browser-session.json` — written by contract server on init, read by skill scripts
- **Session timeout**: 1 hour (`BROWSER_SESSION_TIMEOUT_SECONDS = 3600`). Session recreated automatically on expiry
- **Screenshot delivery**: Screenshots uploaded to `{namespace}/_screenshots/` in S3, embedded in response as `[SCREENSHOT:key]` marker. Router Lambda detects markers and delivers as photos to Telegram/Slack
- **Browser readiness**: a configured Browser session must be verified READY before USER_READY and before every turn
- **Lifecycle**: `startBrowserSession()` called during init (parallel with proxy/OpenClaw start), `stopBrowserSession()` called on SIGTERM
- **Skill scripts**: `navigate.js` (CDP Page.navigate), `screenshot.js` (CDP Page.captureScreenshot → S3 upload), `interact.js` (click/type/scroll/wait via CDP)

### Per-User Identity Resolution
- **Bound identity**: the contract fixes canonical USER_ID, state namespace and scoped credentials. A linked channel caller cannot replace the canonical actor.
- **Per-user sessions**: Contract server sets `USER_ID` env var when starting proxy, so identity is always resolved from environment in per-user mode
- **S3-backed isolation**: User files in `s3://openclaw-user-files-{account}-{region}/{namespace}/`
- **Namespace immutability**: System-determined from channel identity, cannot be changed by user request
- **actorId vs namespace**: actorId uses colon format (`telegram:123456789`), namespace uses underscore format (`telegram_123456789`). Skill scripts (s3-user-files, eventbridge-cron) expect namespace format. The proxy and workspace sync also use namespace format for S3 keys

### Per-User Credential Isolation
- **STS session-scoped credentials**: On init, the contract server calls `STS:AssumeRole` on the execution role with a session policy that limits OpenClaw to the user's own resources: S3 `{namespace}/*` (list with an `s3:prefix` condition), Secrets Manager `openclaw/user/{namespace}/*`, DynamoDB `USER#{internalUserId}` via `dynamodb:LeadingKeys`, schedules named `openclaw-{namespace}-*`, PassRole on the scheduler role only, and KMS only via S3/DynamoDB/Secrets Manager. `ListSecrets` stays on `*` (no resource-level support)
- **Session policy size limit**: AWS STS `AssumeRole` session policies are limited to 2048 plain-text characters (and a separate packed-size limit reported as `PackedPolicySize`). The current policy is ~1.75 KB for typical names and ~2.03 KB at the longest allowed namespace, bucket and environment suffix; `bridge/scoped-credentials.test.js` pins that worst case. Keep new statements short (no `Sid`, no redundant actions)
- **Credential files**: Scoped credentials written to `/tmp/scoped-creds/` in `credential_process` format. OpenClaw uses `AWS_CONFIG_FILE` + `AWS_SDK_LOAD_CONFIG=1` to pick them up
- **OpenClaw env isolation**: OpenClaw spawned with explicit env that excludes `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`, and `AWS_CONTAINER_CREDENTIALS_FULL_URI`
- **Credential refresh**: 45-minute interval timer re-assumes the role and updates credential files (STS self-assume max duration is 1 hour)
- **Trust policy condition**: STS self-assume trust requires sts:RoleSessionName matching "scoped-*" prefix, preventing unconditioned re-assumption
- **Credential failure**: V2 fails activation/admission if required scoped credentials cannot be acquired or refreshed.
- **Proxy keeps full credentials**: The proxy process is trusted code and retains full execution role credentials for Bedrock, Cognito, and S3 image access (with application-level namespace enforcement)

## Workflow Conventions

### Branch Awareness
Always confirm which git branch you are on BEFORE making any code changes or deployments. If the user specifies a branch, switch to it first and verify with `git branch --show-current`. Never assume the current branch is correct.

### Deployment Target
Default deployment region is `us-west-2`. Use `./scripts/deploy.sh` for CDK foundation, pinned SDK V2 deployment and verified dependent stacks. Existing routes stay on their saved immutable endpoint; a new deployment does not replace their binding.

### Git Operations
Never push to any remote (GitHub, GitLab, or otherwise) without explicit user confirmation. Always ask before pushing.

### Planning vs Implementation
When asked to create a plan, produce it concisely in ONE iteration. Do not endlessly revise or research unless asked. If the user says 'implement', move directly to code changes — do not re-plan. If a plan is approved, begin implementation immediately.

## Adding a New Channel (Checklist)

To add a new messaging channel (e.g., WhatsApp, Discord, LINE), follow the Feishu implementation as a reference:

### 1. Secrets Manager (CDK: `security_stack.py`)
- Add a new secret for the channel bot token/credentials
- Export the secret name for cross-stack reference

### 2. Router Lambda (`lambda/router/index.py`)
- Add credential fetching function (e.g., `_get_feishu_credentials()`)
- Add webhook validation function (e.g., `validate_feishu_webhook()`)
- Add message sending function (e.g., `send_feishu_message()`)
- Add progress notification function (e.g., `_feishu_progress_notify()`)
- Add main handler function (e.g., `handle_feishu()`)
- Wire into the Lambda handler: sync path (webhook validation + async dispatch) and async path (message processing)
- Handle channel-specific features: event decryption (Feishu AES-256-CBC), signature verification, bot mention stripping (group chat), image download, etc.

### 3. API Gateway Route (CDK: `router_stack.py`)
- Add `POST /webhook/<channel>` route
- Pass the new secret name as Lambda environment variable

### 4. Cron Lambda (`lambda/cron/index.py`)
- Add response delivery function for the new channel (e.g., `send_feishu_message()`)

### 5. Setup Script (`scripts/setup-<channel>.sh`)
- Interactive script: display webhook URL, prompt for credentials, store in Secrets Manager, add user to allowlist

### 6. Tests (`lambda/router/test_<channel>.py`)
- Webhook validation, event parsing, message sending, edge cases

### Key design decisions:
- **Webhook validation**: Each channel has its own signature/token verification. Fail-closed (reject if validation fails)
- **Async dispatch**: Return 200 immediately to the webhook, self-invoke Lambda asynchronously for processing (prevents webhook timeouts)
- **User ID format**: `<channel>:<platform_user_id>` (e.g., `feishu:ou_xxxx`, `telegram:123456`)
- **Event encryption**: Some platforms (Feishu) encrypt webhook events. Decrypt in the handler using platform-provided keys. Use system libcrypto (ctypes) for AES to avoid native dependency issues across Lambda architectures

## Deployment Gotchas (Learned the Hard Way)

### SDK + CDK
- Preserve VPC/session-storage settings through the pinned controller; incomplete readback or warmup blocks Phase 3.
- Keep existing immutable endpoints for saved routes. Stop-session is not an upgrade mechanism.
- Match ECR pull permissions to the reviewed repository; the wrapper default is `openclaw-bridge`.

### VPC + Bedrock
- **Cross-region inference profiles work through VPC endpoints**: `global.anthropic.claude-opus-4-6-v1` works fine through `bedrock-runtime` VPC endpoint (despite initial suspicion otherwise)
- **Security group egress**: TCP 443 only is sufficient — DNS uses VPC internal resolver (not affected by SG)

### Session Management
- The saved SESSION route is authoritative. Never delete it to force another registration or replace its actor with a linked caller.
- Reconcile unknown outcomes and retained admissions before migration. Cleanup and a stop response alone do not prove a complete state checkpoint.

## Git Worktree Guide

This project uses git worktrees for parallel branch development:

```bash
# Current worktrees
git worktree list

# The deploy branch is checked out at ~/g-repo/openclaw-deploy (worktree)
# The main repo is at ~/g-repo/sample-host-openclaw-on-amazon-bedrock-agentcore

# When done with the deploy branch, merge to main and clean up:
cd ~/g-repo/sample-host-openclaw-on-amazon-bedrock-agentcore
git checkout main
git merge deploy/starter-toolkit-hybrid
git worktree remove ~/g-repo/openclaw-deploy   # removes worktree directory
git branch -d deploy/starter-toolkit-hybrid     # delete branch if fully merged

# Or keep the worktree for continued work — no cleanup needed
```

## Project Context
This is a Python/Node.js project (OpenClaw on AWS Bedrock AgentCore). Key components: Telegram bot, Slack Socket Mode, CDK infrastructure, Docker/ECR deployments, S3 workspace, per-user memory isolation. Subagents are OpenClaw-native running on the same AgentCore runtime — they are NOT separate Bedrock agents.
