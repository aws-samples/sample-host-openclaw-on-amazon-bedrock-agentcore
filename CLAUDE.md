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
- **Tools & Skills**: Built-in tool groups (full profile) + 5 ClawHub skills + 5 custom skills (S3 user files, EventBridge cron, ClawHub manage, API keys, agentcore-browser) + 2 built-in shim tools (web_fetch, web_search)
- **Scheduling**: EventBridge Scheduler for recurring tasks — cron executor Lambda warms sessions and delivers responses to channels
- **Per-User File Storage**: S3-backed per-user file isolation via custom `s3-user-files` skill
- **Workspace Persistence**: AgentCore Session Storage (primary, `/mnt/workspace`) + S3 backup (5 min). `.openclaw/` is restored through the reviewed persistence boundary for a new immutable release session; no direct Runtime update path is supported.
- **AI Model**: Claude Opus 4.6 via Bedrock ConverseStream (configurable via `default_model_id` in `cdk.json`, default `global.anthropic.claude-opus-4-6-v1`)
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
  |   -> boot: pre-fetch secrets from Secrets Manager
  |   -> first /invocations (parallel):
  |     1. Start proxy (18790) + OpenClaw (18789) + restore .openclaw/
  |     2. Wait for proxy only (~5s)
  |     3. Lightweight agent handles messages immediately
  |   -> background: OpenClaw starts (~1-2 min)
  |   -> handoff: once OpenClaw ready, route via WebSocket bridge
  |   -> SIGTERM: save .openclaw/ to S3
  |                       |
  | lightweight-agent.js  -- warm-up shim (proxy -> Bedrock, 17 tools: s3-user-files, eventbridge-cron, clawhub-manage, api-keys, web_fetch, web_search)
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
    agentcore_stack.py            # Runtime, WorkloadIdentity, ECR, S3, IAM
    router_stack.py               # Router Lambda + API Gateway HTTP API + DynamoDB identity
    observability_stack.py        # Dashboards, alarms, Bedrock logging
    token_monitoring_stack.py     # Lambda processor, DynamoDB, token analytics
    cron_stack.py                 # EventBridge Scheduler, Cron executor Lambda, IAM
  bridge/
    Dockerfile                    # Container image (node:22-slim, ARM64, clawhub skills)
    entrypoint.sh                 # Startup: configure IPv4, start contract server
    agentcore-contract.js         # AgentCore HTTP contract with hybrid routing (shim + OpenClaw)
    lightweight-agent.js          # Warm-up agent shim (s3-user-files + eventbridge-cron + clawhub-manage + api-keys tools)
    lightweight-agent.test.js     # Lightweight agent unit tests (node:test, 110 tests)
    agentcore-proxy.js            # OpenAI -> Bedrock ConverseStream adapter + Identity + multimodal images
    image-support.test.js         # Image support unit tests (node:test)
    content-extraction.test.js    # Content block extraction tests (node:test)
    workspace-sync.js             # .openclaw/ directory S3 sync (restore/save/periodic)
    scoped-credentials.js         # Per-user STS session-scoped credentials (S3, Secrets Manager, DynamoDB)
    scoped-credentials.test.js    # Scoped credentials unit tests (node:test)
    workspace-sync.test.js        # Workspace sync credential tests (node:test)
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
| **OpenClawAgentCore** | Execution Role, Security Group, retained ECR/signing foundation, S3 bucket, and optional digest-bound release Runtime/Endpoint L1s | Vpc, Security |
| **OpenClawRouter** | Lambda, API Gateway HTTP API (explicit routes, throttling), DynamoDB identity table | AgentCore, Security |
| **OpenClawObservability** | Operations dashboard, alarms, SNS, Bedrock invocation logging | None |
| **OpenClawTokenMonitoring** | DynamoDB (single-table, 4 GSIs), Lambda processor, analytics dashboard | Observability |
| **OpenClawCron** | EventBridge Scheduler group, Cron executor Lambda, Scheduler IAM role | AgentCore, Router, Security |

## Authoritative v1 commands

The former hybrid Starter Toolkit deployment, mutable image tags, direct
Runtime updates, and `us-west-2` instructions are retired. They are not a
supported fallback. Do not run `agentcore deploy`, retarget an existing
endpoint, push a mutable tag, invoke raw `update-agent-runtime`, or deploy with
`--require-approval never`.

Use the reviewed v1 design and plan as the source of truth:

- `docs/superpowers/specs/2026-07-18-personal-operator-v1-design.md`
- `docs/superpowers/plans/2026-07-18-personal-operator-v1.md`
- `docs/OPERATIONS.md`
- `docs/RELEASE-EVIDENCE.md`

Safe local checks are credential-free:

```bash
./scripts/test-local.sh
./.venv/bin/python scripts/staging-release.py --help
./.venv/bin/python scripts/staging-release.py \
  --status .release/<exact-commit>/staging-transaction.json
```

A release preflight is also local and creates only a canonical journal for the
exact clean Git commit/tree, explicit non-synthetic account, and
`eu-west-1`. It does not discover credentials or mutate AWS:

```bash
./.venv/bin/python scripts/staging-release.py \
  --preflight \
  --journal .release/<exact-commit>/staging-transaction.json \
  --account <12-digit-account> \
  --region eu-west-1 \
  --commit <exact-40-character-HEAD>
```

Every later phase is an explicit write-ahead transaction boundary. Follow
`docs/OPERATIONS.md` only after the candidate, exact operation digest,
confirmation string, change sets, immutable image evidence, and rollback
reference receive separate human review. Unknown provider outcomes remain
`UNCERTAIN`; never guess `persisted` or `absent`, never replay a mutation
without authoritative reconciliation, and never use a mutable `DEFAULT`
endpoint.

All external gates are currently open: Docker-backed artifact reproduction,
AWS identity and IAM, ECR push/sign/scan/SBOM/provenance, AgentCore readiness,
change-set inspection, consumers, provider credentials, and pilot evidence.
Local implementation work must not claim or perform any of them.

### Per-User Credential Isolation
- **STS session-scoped credentials**: On init, the contract server calls `STS:AssumeRole` on the execution role with a minimal session policy that restricts S3 access to `{namespace}/*`. Other services (DynamoDB, Scheduler, SecretsManager) use `Resource: "*"` in the session policy — the execution role's own policy provides the actual resource-level restrictions. This design keeps the session policy under the **AWS 2048-byte packed limit** (long policies with per-resource Conditions easily exceed this)
- **Session policy size limit**: AWS STS `AssumeRole` session policies have a 2048-byte packed limit. If exceeded, `AssumeRole` fails with "Packed policy consumes N% of allotted space". The current policy is ~668 bytes (well under limit). Adding Condition blocks (e.g., `dynamodb:LeadingKeys`, `s3:prefix`) quickly blows past the limit — avoid them in session policies
- **Credential files**: Scoped credentials written to `/tmp/scoped-creds/` in `credential_process` format. OpenClaw uses `AWS_CONFIG_FILE` + `AWS_SDK_LOAD_CONFIG=1` to pick them up
- **OpenClaw env isolation**: OpenClaw spawned with explicit env that excludes `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`, and `AWS_CONTAINER_CREDENTIALS_FULL_URI`
- **Credential refresh**: 45-minute interval timer re-assumes the role and updates credential files (STS self-assume max duration is 1 hour)
- **Trust policy condition**: STS self-assume trust requires sts:RoleSessionName matching "scoped-*" prefix, preventing unconditioned re-assumption
- **Zero-access fallback**: If `EXECUTION_ROLE_ARN` is not set or STS fails, OpenClaw starts with zero AWS access (all credential env vars stripped). Tools will fail gracefully but no cross-user data access is possible
- **Proxy keeps full credentials**: The proxy process is trusted code and retains full execution role credentials for Bedrock, Cognito, and S3 image access (with application-level namespace enforcement)

## Workflow Conventions

### Branch Awareness
Always confirm which git branch you are on BEFORE making any code changes or deployments. If the user specifies a branch, switch to it first and verify with `git branch --show-current`. Never assume the current branch is correct.

### Deployment Target
The only release region is `eu-west-1`. CDK owns the retained immutable ECR
and signing foundation plus digest-bound AgentCore Runtime/Endpoint L1s. The
canonical staging journal and reviewed phase operations are the sole release
path. Direct deployment, mutable tag, endpoint retarget, and Starter Toolkit
paths are prohibited.

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

## Release invariants

- The retained repository name is exactly `personal-operator/bridge` and tags
  are immutable. Runtime input is always an exact `@sha256:` URI.
- A release endpoint is commit-named and never retargeted. Runtime and endpoint
  evidence must match the exact account, region, commit, role, image, and
  version recorded by the transaction.
- An ambiguous phase or rollback remains `UNCERTAIN` until a phase-specific
  authoritative read proves the exact subject persisted or is absent.
- Do not revive the archived Starter Toolkit or direct Runtime mutation path.

### VPC + Bedrock
- **Cross-region inference profiles work through VPC endpoints**: `global.anthropic.claude-opus-4-6-v1` works fine through `bedrock-runtime` VPC endpoint (despite initial suspicion otherwise)
- **Security group egress**: TCP 443 only is sufficient — DNS uses VPC internal resolver (not affected by SG)

### Session Management
- **Session ID is deterministic**: `ses_{userId}_{hash}` — same user always gets same session ID, so `stop-session` with the correct ID is essential after config changes
- **Cold start timing**: VPC mode ~30-60s for ENI creation + image pull. First message triggers init (proxy + OpenClaw startup)

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
