# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OpenClaw on AgentCore Runtime — a multi-channel AI messaging bot (Telegram, Discord, Slack) running as a serverless container on AWS Bedrock AgentCore Runtime. The container runs OpenClaw (Node.js), a Bedrock proxy adapter, and an AgentCore contract server. A keepalive Lambda prevents idle session termination.

## Tech Stack

- **Infrastructure**: CDK v2 (Python), 6 stacks
- **Runtime**: Bedrock AgentCore Runtime (serverless ARM64 container, VPC mode)
- **Messaging**: OpenClaw (Node.js) — Telegram, Discord, Slack channel providers
- **Tools & Skills**: Built-in tool groups (full profile) + 9 ClawHub skills + 1 custom S3 user files skill
- **Per-User File Storage**: S3-backed per-user file isolation via custom `s3-user-files` skill
- **AI Model**: Claude Sonnet 4.6 via Bedrock ConverseStream (`au.anthropic.claude-sonnet-4-6`)
- **Identity**: Cognito User Pool (HMAC-derived passwords, auto-provisioned users)
- **Memory**: AgentCore Memory (semantic, user_preference, summary strategies) — integrated into proxy for per-user persistent context
- **Observability**: CloudWatch dashboards + alarms, Bedrock invocation logging
- **Token Monitoring**: Lambda + DynamoDB (single-table) + CloudWatch custom metrics
- **Security**: VPC endpoints, KMS CMK, Secrets Manager, cdk-nag

## Architecture

```
     Telegram / Discord / Slack
                |
    +-----------v-----------+
    |   AgentCore Runtime   |  <-- Serverless container (ARM64, VPC mode)
    |                       |
    |  agentcore-contract.js (port 8080) -- /ping, /invocations
    |  OpenClaw Gateway     (port 18789) -- channel providers
    |  agentcore-proxy.js   (port 18790) -- OpenAI -> Bedrock + Memory
    +-----------+-----------+
                |
    +-----------v-----------+      +-----------------------+
    |   Amazon Bedrock      |      | EventBridge (5 min)   |
    |   ConverseStream API  |      | + Keepalive Lambda    |
    |   Claude Sonnet 4.6   |      +-----------------------+
    +-----------+-----------+
                |
    +-----------v-----------+
    |  AgentCore Memory     |  <-- Per-user persistent memory
    |  (semantic, prefs,    |      Namespaced by actorId
    |   summary strategies) |      Extraction every 10 min
    +-----------------------+

    +-----------------------+
    |  S3 User Files        |  <-- Per-user file storage
    |  s3://bucket/         |      Namespaced by actorId
    |  {namespace}/file.md  |      Via s3-user-files skill
    +-----------------------+

    Supporting: VPC, KMS, Secrets Manager, Cognito,
               CloudWatch, DynamoDB, CloudTrail
```

## Project Structure

```
openclaw-on-agentcore/
  app.py                          # CDK app entry point (6 stacks)
  cdk.json                        # Configuration (model, budgets, region)
  requirements.txt                # Python deps (aws-cdk-lib, cdk-nag)
  stacks/
    __init__.py                   # Shared helper (RetentionDays converter)
    vpc_stack.py                  # VPC, subnets, NAT, 7 VPC endpoints, flow logs
    security_stack.py             # KMS CMK, Secrets Manager, Cognito, CloudTrail
    agentcore_stack.py            # Runtime, Memory, WorkloadIdentity, ECR, IAM
    keepalive_stack.py            # Lambda + EventBridge (5-min keepalive)
    observability_stack.py        # Dashboards, alarms, Bedrock logging
    token_monitoring_stack.py     # Lambda processor, DynamoDB, token analytics
  bridge/
    Dockerfile                    # Container image (node:22-slim, ARM64, clawhub skills)
    entrypoint.sh                 # Startup orchestration (5 steps)
    agentcore-contract.js         # AgentCore HTTP contract (/ping, /invocations)
    agentcore-proxy.js            # OpenAI -> Bedrock ConverseStream adapter + Memory + Identity
    force-ipv4.js                 # DNS patch for Node.js 22 IPv6 issue
    skills/
      s3-user-files/              # Custom per-user file storage skill (S3-backed)
        SKILL.md                  # OpenClaw skill manifest
        common.js                 # Shared utilities (sanitize, buildKey, validation)
        read.js / write.js        # Read/write files in user's S3 namespace
        list.js / delete.js       # List/delete files in user's S3 namespace
  lambda/
    token_metrics/index.py        # Bedrock log -> DynamoDB + CloudWatch metrics
    keepalive/index.py            # Runtime keepalive invoker
  docs/
    architecture.md               # Detailed architecture diagrams
```

## CDK Stacks (6 stacks)

| Stack | Key Resources | Dependencies |
|---|---|---|
| **OpenClawVpc** | VPC (2 AZ), subnets, NAT, 7 VPC endpoints, flow logs | None |
| **OpenClawSecurity** | KMS CMK, Secrets Manager (5 secrets), Cognito User Pool, CloudTrail | None |
| **OpenClawAgentCore** | CfnRuntime, CfnRuntimeEndpoint, CfnMemory, CfnWorkloadIdentity, ECR, SG, IAM | Vpc, Security |
| **OpenClawKeepalive** | Lambda, EventBridge rule (every 5 min) | AgentCore |
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

### Build & Push Bridge Image
```bash
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=ap-southeast-2

docker build --platform linux/arm64 -t openclaw-bridge bridge/
aws ecr get-login-password --region $CDK_DEFAULT_REGION | \
  docker login --username AWS --password-stdin \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com
docker tag openclaw-bridge:latest \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:latest
docker push \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:latest
```

### Channel Setup
```bash
# Store Telegram bot token
aws secretsmanager update-secret \
  --secret-id openclaw/channels/telegram \
  --secret-string 'BOT_TOKEN' \
  --region $CDK_DEFAULT_REGION

# Store Discord bot token
aws secretsmanager update-secret \
  --secret-id openclaw/channels/discord \
  --secret-string 'BOT_TOKEN' \
  --region $CDK_DEFAULT_REGION

# Store Slack tokens (JSON — both botToken and appToken required for Socket Mode)
aws secretsmanager update-secret \
  --secret-id openclaw/channels/slack \
  --secret-string '{"botToken":"xoxb-YOUR-BOT-TOKEN","appToken":"xapp-YOUR-APP-TOKEN"}' \
  --region $CDK_DEFAULT_REGION
```

### Deploy New Proxy Version
```bash
# 1. Bump IMAGE_VERSION in stacks/agentcore_stack.py
# 2. Build + push
docker build --platform linux/arm64 -t openclaw-bridge bridge/
docker tag openclaw-bridge:latest 657117630614.dkr.ecr.ap-southeast-2.amazonaws.com/openclaw-bridge:latest
docker push 657117630614.dkr.ecr.ap-southeast-2.amazonaws.com/openclaw-bridge:latest
# 3. CDK deploy
source .venv/bin/activate && cdk deploy OpenClawAgentCore --require-approval never
# 4. Stop old session (REQUIRED — existing session keeps old image)
aws bedrock-agentcore stop-runtime-session \
  --runtime-session-id "openclaw-telegram-session-primary-keepalive-001" \
  --agent-runtime-arn "arn:aws:bedrock-agentcore:ap-southeast-2:657117630614:runtime/openclaw_agent-4AglMQ9ED4" \
  --qualifier "openclaw_agent_live" --region ap-southeast-2
# 5. Invoke keepalive to start new session, wait ~4 min for OpenClaw startup
aws lambda invoke --function-name openclaw-keepalive --payload '{}' --region ap-southeast-2 /tmp/status.json
```

### Bridge Tests
```bash
cd bridge && node --test proxy-identity.test.js       # 31 identity + workspace tests
cd bridge/skills/s3-user-files && AWS_REGION=ap-southeast-2 node --test common.test.js  # 22 S3 skill tests
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

# Invoke runtime (status check)
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-id $RUNTIME_ID \
  --qualifier openclaw_agent_live \
  --session-id openclaw-telegram-session-primary-keepalive-001 \
  --payload '{"action":"status"}' \
  --region $CDK_DEFAULT_REGION \
  /tmp/status.json
```

## Key Configuration (cdk.json)

| Parameter | Default | Description |
|---|---|---|
| `account` | (empty) | AWS account ID. Falls back to `CDK_DEFAULT_ACCOUNT` |
| `region` | `ap-southeast-2` | AWS region. Falls back to `CDK_DEFAULT_REGION` |
| `default_model_id` | `au.anthropic.claude-sonnet-4-6` | Bedrock model ID (cross-region inference profile) |
| `cloudwatch_log_retention_days` | `30` | Log retention |
| `daily_token_budget` | `1000000` | Token budget alarm threshold |
| `daily_cost_budget_usd` | `5` | Cost budget alarm threshold |
| `token_ttl_days` | `90` | DynamoDB TTL |
| `user_files_ttl_days` | `365` | S3 per-user file expiration |

## Container Startup Sequence (entrypoint.sh)

1. **agentcore-contract.js** (port 8080) — MUST start first for health check
2. **Fetch secrets** — gateway token, Cognito secret, channel bot tokens
3. **agentcore-proxy.js** (port 18790) — OpenAI-to-Bedrock adapter
4. **Write OpenClaw config** — `openclaw.json` with enabled channels, tools (full profile), and skills
5. **OpenClaw gateway** (port 18789) — foreground process

## Gotchas

### AgentCore Runtime
- **ARM64 required**: Build with `--platform linux/arm64`
- **Image must exist before deploy**: Push to ECR before `cdk deploy`
- **Resource names**: Must match `^[a-zA-Z][a-zA-Z0-9_]{0,47}$` — underscores, not hyphens
- **Health check timing**: Contract server on port 8080 must start within seconds or health check times out
- **Keepalive**: Lambda invokes runtime every 5 min; contract server returns `HealthyBusy` to prevent idle termination
- **Memory event expiry**: `event_expiry_duration` is in days (max 365), not seconds
- **VPC endpoints**: `bedrock-agentcore-runtime` endpoint not available in all regions — omit if unsupported
- **Endpoint version drift**: `CfnRuntimeEndpoint` must set `agent_runtime_version=self.runtime.attr_agent_runtime_version` to stay in sync with runtime version on each deploy. Without this, the endpoint stays on an old version after runtime updates, causing misleading "execution role cannot be assumed" errors. Fix: `aws bedrock-agentcore-control update-agent-runtime-endpoint --agent-runtime-version <N>`

### IAM / Bedrock
- **Cross-region inference**: Model `au.anthropic.claude-sonnet-4-6` routes to any AU/APAC region — IAM uses `arn:aws:bedrock:*::foundation-model/*`
- **Inference profile ARN**: Separate from foundation model — `arn:aws:bedrock:{region}:{account}:inference-profile/*`
- **Memory execution role**: Must trust both `bedrock.amazonaws.com` and `bedrock-agentcore.amazonaws.com`
- **Memory IAM actions use `bedrock-agentcore:` prefix**: NOT `bedrock:`. Actions: `CreateEvent`, `GetEvent`, `ListEvents`, `DeleteEvent`, `RetrieveMemoryRecords`, `ListMemoryRecords`, `StartMemoryExtractionJob`, `ListMemoryExtractionJobs`

### Node.js 22 + VPC
- **IPv6 issue**: Node.js 22 Happy Eyeballs fails in VPCs without IPv6 — `force-ipv4.js` patches `dns.lookup()` to force IPv4
- **NODE_OPTIONS**: `--dns-result-order=ipv4first --no-network-family-autoselection -r /app/force-ipv4.js`

### CDK
- `logs.RetentionDays` is an enum — use helper in `stacks/__init__.py`
- Cross-stack cyclic deps: use string ARN params + `add_to_policy()` instead of `grant_*()`
- Empty `cdk.json` account: falls back to `CDK_DEFAULT_ACCOUNT` env var via `app.py`

### OpenClaw
- Startup takes ~4 minutes (plugin registration, channel connection)
- Correct start command: `openclaw gateway run --port 18789 --bind lan --verbose`
- Telegram `dmPolicy: "open"` requires `allowFrom: ["*"]`
- Channel token validation: `entrypoint.sh` skips channels with placeholder/short tokens
- **Slack Socket Mode requires two tokens**: The `openclaw/channels/slack` secret must be JSON `{"botToken":"xoxb-...","appToken":"xapp-..."}`. The entrypoint parses JSON secrets and passes both tokens to OpenClaw. Plain string secrets (backward compatible) only pass `botToken` and will fail to connect via Socket Mode
- **`skills.allowBundled`**: Must be an array (e.g., `["*"]`), not a boolean — OpenClaw schema validation rejects `true`
- **ClawHub skill paths**: `clawhub install` installs to `/skills/<name>`, not `~/.openclaw/skills` — use `/skills` as `extraDirs`
- **ClawHub VirusTotal flags**: Some skills (jina-reader, etc.) are flagged for external API calls — use `--force` in non-interactive mode
- **Image updates require session restart**: Pushing a new ECR image and redeploying via CDK updates the runtime config, but the existing keepalive session continues running the old image. Stop it with `stop-runtime-session` or bump `IMAGE_VERSION` env var in the stack and redeploy

### Cognito Identity
- Self-signup disabled — users auto-provisioned by proxy via `AdminCreateUser`
- Passwords: `HMAC-SHA256(secret, actorId).slice(0, 32)` — deterministic, never stored
- Usernames are channel-prefixed: `telegram:6087229962`
- JWT tokens cached per user with 60s early refresh

### AgentCore Memory Integration
- **Per-user isolation**: Each user's memories are namespaced by `actorId` (colons replaced with underscores, e.g., `telegram_6087229962`)
- **Identity resolution**: `actorId` is extracted in priority order: (1) `x-openclaw-actor-id` header, (2) OpenAI `user` field, (3) OpenClaw message envelope parsing (3 formats, checked in reverse message order): **Format C** (metadata JSON with `sender` field — highest priority, contains platform user IDs) > **Format A** (`System: [TIMESTAMP] Channel TYPE from SenderName:` — display-name fallback) > **Format B** (`[Channel ... id:ID]` — legacy). Format C auto-detects channel from sender ID pattern: `/^[UW][A-Z0-9]{8,}$/i` → Slack, `/^\d{15,}$/` → Discord, `/^\d{5,14}$/` → Telegram. (4) message `name` field, (5) fallback `"default-user"`. All extracted IDs validated against `VALID_ACTOR_ID` regex
- **Request flow**: Before each Bedrock call, the proxy retrieves up to 5 relevant memory records via `RetrieveMemoryRecords` (hardcoded `MEMORY_RETRIEVAL_LIMIT = 5` in `agentcore-proxy.js`) using the user's latest message as a semantic search query. Records are filtered (`r.content && r.content.text`) and appended to the system prompt under a `## Relevant memories about this user` heading with instructions not to mention memory unless asked. After the response, the user/assistant exchange (both `USER` and `ASSISTANT` roles) is stored as a memory event via `CreateEvent` (fire-and-forget)
- **Memory extraction**: A timer triggers `StartMemoryExtractionJob` every 10 minutes (+ 30s after startup). This is a **global operation** — it processes accumulated events across all user namespaces, not per-user. The 3 configured strategies (semantic, user_preference, summary) run server-side using a dedicated `MemoryExecutionRole` with `bedrock:InvokeModel` permissions
- **Event expiry**: Raw conversation events expire after 90 days (`event_expiry_duration=90` in `agentcore_stack.py`). Extracted memory records (the output of strategies) persist independently
- **Graceful degradation**: All memory operations (retrieval, storage, extraction) are wrapped in try/catch — they log warnings on failure but never block the chat flow. If `AGENTCORE_MEMORY_ID` is empty, every memory function short-circuits immediately
- **Session ID generation**: Session IDs are generated per `actorId:channel` pair as `ses-{timestamp}-{random}-{md5hash}` and cached in-memory (`sessionMap`). AgentCore requires minimum 33 characters. Session IDs are lost on container restart but this only affects session continuity metadata, not memory records
- **SDK**: Uses `@aws-sdk/client-bedrock-agentcore` (`BedrockAgentCoreClient`)
- **Namespace character restrictions**: `actorId` contains colons (e.g., `telegram:6087229962`) which may be rejected by the namespace field — proxy replaces `:` with `_`
- **Added latency**: Memory retrieval adds ~50-200ms, workspace file pre-loading adds ~100ms (parallel S3 reads) per request
- **Container restart**: Memories persist across container restarts since they are stored in AgentCore Memory (server-side), not in-memory. The in-memory `sessionMap` is lost, generating new session IDs, but this has no effect on memory retrieval

### Per-User File Isolation
- **S3-backed isolation**: User files stored in `s3://openclaw-user-files-{account}-{region}/{namespace}/{filename}` where `namespace = actorId.replace(/:/g, "_")`
- **System prompt injection**: `buildUserIdentityContext()` in `agentcore-proxy.js` ALWAYS injects `actorId`, `namespace`, and isolation rules into the system prompt (not conditional on memory)
- **S3 skill**: Custom `s3-user-files` skill provides `read.js`, `write.js`, `list.js`, `delete.js` — all namespaced by user_id argument
- **NODE_PATH**: Set to `/app/node_modules` in Dockerfile so skill scripts can resolve `@aws-sdk/client-s3`
- **openclaw-mem removed**: The shared SQLite-based `openclaw-mem` ClawHub skill was replaced by AgentCore Memory (per-user) + S3 skill (per-user files)
- **Content as CLI argument**: `write.js` receives content via `process.argv.slice(4).join(" ")` — works for typical .md files but may truncate very large content passed as shell arguments
- **default-user rejection**: `write.js` and other S3 scripts reject `default_user`/`default-user` to prevent accidental shared-namespace writes
- **Workspace file pre-loading**: Proxy reads 5 workspace files (`AGENTS.md`, `SOUL.md`, `USER.md`, `IDENTITY.md`, `TOOLS.md`) from S3 in parallel at request time and injects content into system prompt — prevents LLM from reading wrong namespace via tool calls. ~100ms added latency (parallel S3 reads)
- **System prompt sanitization**: Workspace file content is truncated to 4096 chars per file (20,000 total) and triple-backticks replaced with `~~~` to prevent code fence escape / prompt injection
- **Channel validation**: Channel value validated against allowlist (`telegram`, `slack`, `discord`, `whatsapp`, `unknown`) before system prompt injection
- **Namespace immutability**: System prompt includes "Namespace Protection (IMMUTABLE)" section — namespace is system-determined, users can change display name but not actorId/namespace
- **S3 bucket encryption**: Uses project CMK (not AWS-managed key). When switching encryption keys, existing objects must be re-encrypted in-place via `aws s3 cp --sse aws:kms --sse-kms-key-id CMK_ARN`
- **No PII in diagnostics**: `/health` endpoint only exposes `actorId`, `channel`, `idSource`, `msgCount`, `toolCount`, `timestamp` — no user message content
- **AWS_REGION required**: Proxy, S3 skill scripts, and entrypoint.sh all fail fast if `AWS_REGION` is not set — no silent fallback to a wrong region
