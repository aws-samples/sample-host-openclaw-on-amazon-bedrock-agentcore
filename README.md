# OpenClaw on AgentCore Runtime

Deploy an AI-powered multi-channel messaging bot (Telegram, Discord, Slack) on AWS Bedrock AgentCore Runtime using CDK.

OpenClaw runs as a serverless container on AgentCore Runtime, with a local proxy that translates OpenAI-format chat requests to Bedrock ConverseStream API calls. Each user gets **persistent, isolated conversation memory** via AgentCore Memory — the proxy retrieves relevant context before each request and stores exchanges after each response, so the bot remembers users across sessions and container restarts. The agent has built-in tools (web, filesystem, runtime, memory, sessions, automation) and 10 pre-installed ClawHub skills for web search, content extraction, research, and more. A keepalive Lambda prevents idle session termination.

## Architecture

```
     Telegram / Discord / Slack
                |
                | (bot APIs over internet)
                |
    +-----------v-----------+
    |   AgentCore Runtime   |  <-- Serverless container (ARM64, VPC mode)
    |   (managed by AWS)    |
    |                       |
    |  +------------------+ |
    |  | agentcore-       | |     +-----------------------+
    |  | contract.js      | |     | EventBridge (5 min)   |
    |  | (port 8080)      |<------| + Keepalive Lambda    |
    |  | /ping, /invoke   | |     +-----------------------+
    |  +------------------+ |
    |                       |
    |  +------------------+ |
    |  | OpenClaw Gateway | |
    |  | (port 18789)     | |
    |  | - Channels       | |
    |  | - Message routing| |
    |  +--------+---------+ |
    |           |           |
    |  +--------v---------+ |
    |  | agentcore-       | |
    |  | proxy.js         | |
    |  | (port 18790)     | |
    |  | OpenAI -> Bedrock| |
    |  | + Memory         | |
    |  +--------+---------+ |
    +-----------+-----------+
                |
    +-----------v-----------+      +-----------------------+
    |   Amazon Bedrock      |      |   AgentCore Memory    |
    |   ConverseStream API  |      |   Per-user namespaces |
    |   Claude Sonnet 4.6   |      |   3 strategies:       |
    +-----------------------+      |   semantic, prefs,    |
                                   |   summary             |
                                   +-----------------------+

    +--------------------------------------------------+
    |              Supporting Services                  |
    |                                                  |
    |  VPC (2 AZ, private subnets, NAT, 7 endpoints)  |
    |  KMS CMK (encryption at rest)                    |
    |  Secrets Manager (bot tokens, gateway token)     |
    |  Cognito User Pool (identity auto-provisioning)  |
    |  CloudWatch (dashboards, alarms, logs)           |
    |  DynamoDB (token usage tracking)                 |
    |  CloudTrail (audit logging)                      |
    +--------------------------------------------------+
```

See [docs/architecture.md](docs/architecture.md) for the detailed architecture diagram.

## Prerequisites

- **AWS Account** with Bedrock model access enabled for Claude Sonnet 4.6
- **AWS CLI** v2 configured with credentials (`aws sts get-caller-identity` should succeed)
- **Node.js** >= 18 (for CDK CLI)
- **Python** >= 3.11 (for CDK app)
- **Docker** (for building the bridge container image; ARM64 support via Docker Desktop or buildx)
- **AWS CDK** v2 (`npm install -g aws-cdk`)
- **Telegram Bot Token** from [@BotFather](https://t.me/BotFather)

### Enable Bedrock Model Access

Before deploying, enable model access in the AWS console:

1. Go to **Amazon Bedrock** > **Model access** in your target region
2. Request access to **Anthropic Claude Sonnet 4.6** (or the model specified in `cdk.json`)
3. If using cross-region inference profiles (e.g., `us.anthropic.claude-sonnet-4-6`), enable access in all regions the profile may route to

## Quick Start

### 1. Clone and configure

```bash
git clone <repo-url>
cd openclaw-on-agentcore

# Set your AWS account and region
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=ap-southeast-2
```

Or edit `cdk.json` directly:
```json
{
  "context": {
    "account": "123456789012",
    "region": "ap-southeast-2"
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

### 4. Build and push the bridge container image

The bridge image must exist in ECR before the AgentCore stack deploys. You need to create the ECR repository first, then build and push.

```bash
# Create ECR repository (will be managed by CDK after first deploy)
aws ecr create-repository --repository-name openclaw-bridge --region $CDK_DEFAULT_REGION 2>/dev/null || true

# Authenticate Docker to ECR
aws ecr get-login-password --region $CDK_DEFAULT_REGION | \
  docker login --username AWS --password-stdin \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com

# Build ARM64 image (required by AgentCore Runtime)
docker build --platform linux/arm64 -t openclaw-bridge bridge/

# Tag and push
docker tag openclaw-bridge:latest \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:latest
docker push \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:latest
```

### 5. Deploy all stacks

```bash
cdk synth          # validate (runs cdk-nag security checks)
cdk deploy --all --require-approval never
```

This deploys 6 stacks in order:
1. **OpenClawVpc** — VPC, subnets, NAT gateway, VPC endpoints
2. **OpenClawSecurity** — KMS, Secrets Manager, Cognito, CloudTrail
3. **OpenClawAgentCore** — Runtime, Memory, WorkloadIdentity, ECR, IAM
4. **OpenClawKeepalive** — Lambda + EventBridge (5-min keepalive)
5. **OpenClawObservability** — Dashboards, alarms, Bedrock logging
6. **OpenClawTokenMonitoring** — DynamoDB, Lambda processor, token analytics

### 6. Store your Telegram bot token

```bash
aws secretsmanager update-secret \
  --secret-id openclaw/channels/telegram \
  --secret-string 'YOUR_TELEGRAM_BOT_TOKEN' \
  --region $CDK_DEFAULT_REGION
```

### 7. Trigger a new container deployment

After storing the token, the running container needs to restart to pick up the new secret:

```bash
# Get the runtime ID from stack outputs
RUNTIME_ID=$(aws cloudformation describe-stacks \
  --stack-name OpenClawAgentCore \
  --query "Stacks[0].Outputs[?OutputKey=='RuntimeId'].OutputValue" \
  --output text --region $CDK_DEFAULT_REGION)

# Invoke the runtime to start a fresh session with the new token
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-id $RUNTIME_ID \
  --qualifier openclaw_agent_live \
  --session-id openclaw-telegram-session-primary-keepalive-001 \
  --payload '{"action":"status"}' \
  --region $CDK_DEFAULT_REGION \
  /dev/null
```

OpenClaw takes ~4 minutes to fully initialize (plugin registration, channel connection). After that, your Telegram bot should respond to messages.

### 8. Verify

Send a message to your Telegram bot. It should respond using Claude Sonnet 4.6 via Bedrock.

## Project Structure

```
openclaw-on-agentcore/
  app.py                          # CDK app entry point
  cdk.json                        # Configuration (model, budgets, region)
  requirements.txt                # Python deps (aws-cdk-lib, cdk-nag)
  stacks/
    __init__.py                   # Shared helper (RetentionDays converter)
    vpc_stack.py                  # VPC, subnets, NAT, VPC endpoints, flow logs
    security_stack.py             # KMS CMK, Secrets Manager, Cognito, CloudTrail
    agentcore_stack.py            # AgentCore Runtime, Memory, WorkloadIdentity, IAM
    keepalive_stack.py            # Lambda + EventBridge keepalive (every 5 min)
    observability_stack.py        # CloudWatch dashboards, alarms, Bedrock logging
    token_monitoring_stack.py     # Lambda log processor, DynamoDB, token analytics
  bridge/
    Dockerfile                    # Bridge container (node:22-slim, ARM64, clawhub skills)
    entrypoint.sh                 # Startup: contract server -> secrets -> proxy -> OpenClaw
    agentcore-contract.js         # AgentCore HTTP contract (/ping, /invocations)
    agentcore-proxy.js            # OpenAI -> Bedrock ConverseStream adapter + Memory integration
    force-ipv4.js                 # DNS patch for Node.js 22 IPv6 issue in VPC
  lambda/
    token_metrics/index.py        # Bedrock log -> DynamoDB + CloudWatch metrics
    keepalive/index.py            # Runtime keepalive invoker
  docs/
    architecture.md               # Detailed architecture diagram
```

## CDK Stacks

| Stack | Resources | Dependencies |
|---|---|---|
| **OpenClawVpc** | VPC (2 AZ), private/public subnets, NAT, 7 VPC endpoints, flow logs | None |
| **OpenClawSecurity** | KMS CMK, Secrets Manager (5 secrets), Cognito User Pool, CloudTrail | None |
| **OpenClawAgentCore** | CfnRuntime, CfnRuntimeEndpoint, CfnMemory, CfnWorkloadIdentity, ECR, SG, IAM | Vpc, Security |
| **OpenClawKeepalive** | Lambda, EventBridge rule (every 5 min) | AgentCore |
| **OpenClawObservability** | Operations dashboard, alarms (errors, latency, throttles), SNS, Bedrock logging | None |
| **OpenClawTokenMonitoring** | DynamoDB (single-table, 4 GSIs), Lambda processor, analytics dashboard | Observability |

## Configuration

All tunable parameters are in `cdk.json`:

| Parameter | Default | Description |
|---|---|---|
| `account` | (empty) | AWS account ID. Falls back to `CDK_DEFAULT_ACCOUNT` env var |
| `region` | `ap-southeast-2` | AWS region. Falls back to `CDK_DEFAULT_REGION` env var |
| `default_model_id` | `au.anthropic.claude-sonnet-4-6` | Bedrock model ID (cross-region inference profile) |
| `cloudwatch_log_retention_days` | `30` | Log retention in days |
| `daily_token_budget` | `1000000` | Daily token budget alarm threshold |
| `daily_cost_budget_usd` | `5` | Daily cost budget alarm threshold (USD) |
| `anomaly_band_width` | `2` | CloudWatch anomaly detection band width |
| `token_ttl_days` | `90` | DynamoDB token usage record TTL |

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
5. Restart the runtime session (see [Update the container image](#update-the-container-image) for the `stop-runtime-session` command)

### Discord

1. Create an application at [discord.com/developers](https://discord.com/developers/applications)
2. Create a bot and copy the token
3. Enable the **Message Content** intent
4. Store the token:
   ```bash
   aws secretsmanager update-secret \
     --secret-id openclaw/channels/discord \
     --secret-string 'YOUR_BOT_TOKEN' \
     --region $CDK_DEFAULT_REGION
   ```
5. Restart the runtime session (see [Update the container image](#update-the-container-image) for the `stop-runtime-session` command)

### Slack

OpenClaw connects to Slack via **Socket Mode** (WebSocket), which requires both a Bot Token (`xoxb-`) and an App-Level Token (`xapp-`). No public URL or request URL is needed.

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App** > **From scratch**
2. Give it a name (e.g., "OpenClaw") and select your workspace

**Enable Socket Mode:**

3. Go to **Settings** > **Socket Mode** and toggle it **on**
4. Create an app-level token with the `connections:write` scope — name it something like `openclaw-socket`. Copy this token (starts with `xapp-`)

**Configure Event Subscriptions:**

5. Go to **Features** > **Event Subscriptions** and toggle **Enable Events** on
6. Under **Subscribe to bot events**, add:
   - `message.im` — receive direct messages
   - `app_mention` — respond when @mentioned in channels

**Add OAuth Scopes:**

7. Go to **Features** > **OAuth & Permissions** > **Scopes** > **Bot Token Scopes** and add:
   - `chat:write` — send messages
   - `app_mentions:read` — detect @mentions
   - `im:history` — read DM history
   - `im:read` — access DMs
   - `im:write` — send DMs
   - `channels:history` — read channel messages (for @mention context)

**Install and configure:**

8. Go to **Settings** > **Install App** and click **Install to Workspace**, then authorize
9. Copy the **Bot User OAuth Token** (starts with `xoxb-`)
10. Go to **Features** > **App Home** and enable **Allow users to send Slash commands and messages from the messages tab** (under "Show Tabs" > "Messages Tab")
11. Store both tokens as JSON in Secrets Manager:
    ```bash
    aws secretsmanager update-secret \
      --secret-id openclaw/channels/slack \
      --secret-string '{"botToken":"xoxb-YOUR-BOT-TOKEN","appToken":"xapp-YOUR-APP-TOKEN"}' \
      --region $CDK_DEFAULT_REGION
    ```
12. Restart the runtime session (see [Update the container image](#update-the-container-image) for the `stop-runtime-session` command) — the entrypoint fetches secrets at startup, so a running session won't pick up the new token automatically

## How It Works

### Container Startup Sequence

The bridge container runs on AgentCore Runtime and executes 5 steps in order:

1. **AgentCore contract server** (port 8080) — starts immediately for health check
2. **Fetch secrets** — gateway token, Cognito secret, channel bot tokens from Secrets Manager
3. **Bedrock proxy** (port 18790) — OpenAI-to-Bedrock adapter with Cognito auto-provisioning
4. **Write OpenClaw config** — generates `openclaw.json` with enabled channels, tools (full profile), and skills (`/skills` directory)
5. **OpenClaw gateway** (port 18789) — main process, handles channel messages

### Keepalive Mechanism

AgentCore Runtime has an 8-hour maximum session lifetime and will terminate idle sessions. The keepalive Lambda runs every 5 minutes via EventBridge, sending a lightweight `invoke_agent_runtime` call to keep the session active. The contract server responds to `/ping` with `HealthyBusy` status to signal the container should not be terminated.

### Message Flow

```
User sends Telegram message
  -> OpenClaw Gateway (port 18789) receives it
  -> Routes to agentcore-proxy.js (port 18790) as OpenAI chat completion
  -> Proxy extracts actorId (e.g., telegram:6087229962) from request
  -> Proxy retrieves relevant memories for this user from AgentCore Memory
  -> Memory context appended to system prompt
  -> Proxy converts to Bedrock ConverseStream API call
  -> Claude Sonnet 4.6 generates response (with user-specific context)
  -> Proxy converts response back to OpenAI SSE format
  -> Proxy stores user/assistant exchange as memory event (fire-and-forget)
  -> OpenClaw streams response to Telegram
```

### Tools & Skills

The agent runs with OpenClaw's **full tool profile** enabled, giving it access to built-in tool groups (web, filesystem, runtime, memory, sessions, automation). Additionally, 10 community skills are pre-installed from ClawHub at Docker build time:

| Skill | Purpose |
|---|---|
| `duckduckgo-search` | Web search (no API key required) |
| `jina-reader` | Web content extraction as markdown |
| `openclaw-mem` | Persistent memory (SQLite + FTS5) |
| `telegram-compose` | Rich HTML formatting for Telegram |
| `transcript` | YouTube transcript extraction |
| `deep-research-pro` | Multi-step research agent |
| `hackernews` | Browse/search Hacker News |
| `news-feed` | RSS-based news aggregation |
| `task-decomposer` | Break complex requests into subtasks |
| `cron-mastery` | Cron scheduling and management |

Skills are installed into `/skills/` by `clawhub` during the Docker build. The `openclaw.json` config references this directory via `skills.load.extraDirs`.

### Per-User Memory

Each user gets isolated, persistent conversation memory powered by AgentCore Memory. The proxy handles all memory operations transparently — OpenClaw is unaware of the memory layer.

**How it works:**

1. **Identity extraction** — The proxy extracts `actorId` from each request using three sources in priority order: (1) `x-openclaw-actor-id` header, (2) OpenAI `user` field in the request body, (3) fallback to `"default-user"`. Channel-prefixed IDs like `telegram:6087229962` are namespaced by replacing colons with underscores (e.g., `telegram_6087229962`). Users sharing the `default-user` fallback would share the same memory namespace.
2. **Memory retrieval** — Before each Bedrock call, the proxy queries `RetrieveMemoryRecords` with the user's latest message as a semantic search query, scoped to that user's namespace. Up to 5 relevant records (hardcoded `MEMORY_RETRIEVAL_LIMIT`) are filtered and appended to the system prompt under a `## Relevant memories about this user` heading. The model is instructed to use them for personalization without mentioning memory unless asked.
3. **Event storage** — After each response, the user/assistant exchange (both roles in a single event) is stored via `CreateEvent` (fire-and-forget, non-blocking). Events include the session ID for grouping.
4. **Memory extraction** — Every 10 minutes (plus 30 seconds after startup), the proxy triggers `StartMemoryExtractionJob`. This is a **global operation** that processes accumulated events across all user namespaces through 3 configured strategies:
   - **Semantic** (`openclaw_semantic`) — extracts factual information and topics
   - **User preference** (`openclaw_user_prefs`) — captures user preferences and patterns
   - **Summary** (`openclaw_summary`) — produces conversation summaries

   Extraction runs server-side using a dedicated `MemoryExecutionRole` with `bedrock:InvokeModel` permissions. New events are not searchable until the next extraction job runs.
5. **Persistence** — Memories survive container restarts because they are stored server-side in AgentCore Memory, not in the container's process memory. Raw conversation events expire after **90 days** (`event_expiry_duration`). The in-memory `sessionMap` (which generates stable session IDs per `actorId:channel` pair) is lost on restart, but this only affects session ID continuity — not memory records.

**Graceful degradation** — All memory operations (retrieval, storage, extraction) are wrapped in try/catch — they log warnings on failure but never block the chat flow. If `AGENTCORE_MEMORY_ID` is not set, every memory function short-circuits immediately and memory is completely disabled.

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

### View logs

Container stdout/stderr is available via the AgentCore Runtime console or by invoking the runtime with a status action:

```bash
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-id $RUNTIME_ID \
  --qualifier openclaw_agent_live \
  --session-id openclaw-telegram-session-primary-keepalive-001 \
  --payload '{"action":"status"}' \
  --region $CDK_DEFAULT_REGION \
  /tmp/status.json

cat /tmp/status.json
```

### Update the container image

After making changes to files in `bridge/`:

```bash
# Build, tag, push
docker build --platform linux/arm64 -t openclaw-bridge bridge/
docker tag openclaw-bridge:latest \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:latest
aws ecr get-login-password --region $CDK_DEFAULT_REGION | \
  docker login --username AWS --password-stdin \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com
docker push \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:latest

# Bump IMAGE_VERSION in stacks/agentcore_stack.py to force a runtime update,
# then redeploy the AgentCore stack
cdk deploy OpenClawAgentCore --require-approval never

# Stop the running keepalive session so it restarts with the new image
RUNTIME_ARN=$(aws cloudformation describe-stacks \
  --stack-name OpenClawAgentCore \
  --query "Stacks[0].Outputs[?OutputKey=='RuntimeArn'].OutputValue" \
  --output text --region $CDK_DEFAULT_REGION)

aws bedrock-agentcore stop-runtime-session \
  --runtime-session-id openclaw-telegram-session-primary-keepalive-001 \
  --agent-runtime-arn $RUNTIME_ARN \
  --qualifier openclaw_agent_live \
  --region $CDK_DEFAULT_REGION

# The keepalive Lambda will start a new session within 5 minutes,
# or trigger it manually:
aws lambda invoke --function-name openclaw-keepalive \
  --region $CDK_DEFAULT_REGION /tmp/keepalive.json
```

### Security validation

```bash
cdk synth   # Runs cdk-nag AwsSolutions checks — should produce no errors
```

## Troubleshooting

### Container fails health check (RuntimeClientError: health check timed out)

The AgentCore contract server on port 8080 must start within seconds. If `entrypoint.sh` does slow operations (like Secrets Manager calls) before starting the contract server, the health check will time out. The contract server is started as step 1 to avoid this.

### Telegram bot not responding

- **Startup delay**: OpenClaw takes ~4 minutes to initialize. Wait and retry.
- **Token invalid**: Check that the Telegram token in Secrets Manager is correct:
  ```bash
  aws secretsmanager get-secret-value \
    --secret-id openclaw/channels/telegram \
    --region $CDK_DEFAULT_REGION \
    --query SecretString --output text
  ```
- **Container not running**: Check runtime status (see Operations section above).

### 502 / Bedrock authorization errors

- **Model access not enabled**: Enable model access in the Bedrock console for your region.
- **Cross-region inference**: The model ID `au.anthropic.claude-sonnet-4-6` routes requests to AU/APAC regions. The IAM policy uses `arn:aws:bedrock:*::foundation-model/*` to allow all regions. Use the region-appropriate prefix (`us.` for US, `eu.` for EU, `au.` for APAC).
- **Inference profile ARN**: The IAM policy also includes `arn:aws:bedrock:{region}:{account}:inference-profile/*`.

### Node.js ETIMEDOUT / ENETUNREACH in VPC

Node.js 22's Happy Eyeballs (`autoSelectFamily`) tries both IPv4 and IPv6. In VPCs without IPv6, this causes connection failures. The `force-ipv4.js` script patches `dns.lookup()` to force IPv4 only, loaded via `NODE_OPTIONS`.

## Gotchas

- **ARM64 required**: AgentCore Runtime runs ARM64 containers. Build with `--platform linux/arm64`.
- **Image must exist before deploy**: Push the bridge image to ECR before running `cdk deploy` — otherwise CfnRuntime creation fails.
- **AgentCore resource names**: Must match `^[a-zA-Z][a-zA-Z0-9_]{0,47}$` — use underscores, not hyphens.
- **Memory event expiry**: `event_expiry_duration` is in days (max 365), not seconds.
- **VPC endpoints**: The `bedrock-agentcore-runtime` VPC endpoint is not available in all regions. Omit it if your region doesn't support it.
- **CDK RetentionDays**: `logs.RetentionDays` is an enum, not constructable from int. Use the helper in `stacks/__init__.py`.
- **Cognito passwords**: HMAC-derived (`HMAC-SHA256(secret, actorId)`) — deterministic, never stored. Enables `AdminInitiateAuth` without per-user password storage.
- **Channel token validation**: `entrypoint.sh` skips channels with placeholder tokens (< 20 chars or "changeme") to prevent retry loops.
- **Slack requires two tokens**: Socket Mode needs both `botToken` (`xoxb-`) and `appToken` (`xapp-`). The `openclaw/channels/slack` secret must be JSON: `{"botToken":"xoxb-...","appToken":"xapp-..."}`. A plain `xoxb-` string will fail to connect.
- **`skills.allowBundled` is an array**: OpenClaw expects `["*"]` (not `true`) — boolean causes config validation failure.
- **ClawHub installs to `/skills/`**: Not `~/.openclaw/skills`. The `extraDirs` config must point to `/skills`.
- **ClawHub `--force` flag**: Some skills are flagged by VirusTotal for external API calls. Use `--no-input --force` for non-interactive Docker builds.
- **Image updates need session restart**: Pushing a new image to ECR and redeploying via CDK updates the runtime config, but the existing keepalive session keeps running the old image. Stop it with `stop-runtime-session` (see Operations section).
- **Memory IAM prefix**: AgentCore Memory APIs use the `bedrock-agentcore:` IAM action prefix, NOT `bedrock:`. Using the wrong prefix causes `AccessDeniedException`.
- **Memory namespace characters**: `actorId` values contain colons (e.g., `telegram:6087229962`) which may be rejected by namespace fields. The proxy replaces `:` with `_`.
- **Memory extraction latency**: New conversation events are not immediately searchable. The extraction job must run first (triggered every 10 minutes as a global operation across all namespaces) to process events into retrievable records via the 3 configured strategies.
- **`default-user` fallback**: If OpenClaw doesn't populate the `user` field or custom headers, all requests fall back to `actorId = "default-user"` — meaning all such users share one memory namespace. Ensure your channel providers populate user identity.

## Cleanup

```bash
cdk destroy --all
```

Note: KMS keys and the Cognito User Pool have `RETAIN` removal policies and will not be deleted automatically. Remove them manually if needed.

## License

See [LICENSE](LICENSE) for details.
