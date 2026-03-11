# OpenClaw on AgentCore — Solution Architecture

## High-Level Architecture

```
                                                      +---------------------------+
                                                      |        End Users          |
                                                      |    Telegram     Slack     |
                                                      +--+---------------+-------+
                                                         |               |
                                              (webhook HTTPS over internet)
                                                         |               |
+--------------------------------------------------------+---------------+-------------------+
|  AWS Account                                                                               |
|                                                                                            |
|  +----------------------------------------------+                                         |
|  |  API Gateway HTTP API                        |                                         |
|  |  (openclaw-router)                           |                                         |
|  |                                              |                                         |
|  |  POST /webhook/telegram  --> Lambda          |                                         |
|  |  POST /webhook/slack     --> Lambda          |                                         |
|  |  GET  /health            --> Lambda          |                                         |
|  |  (all other paths --> 404, no Lambda invoke) |                                         |
|  |                                              |                                         |
|  |  Throttling: burst 50, sustained 100 req/s   |                                         |
|  +----------------------+-----------------------+                                         |
|                         |                                                                  |
|  +----------------------v-----------------------+                                         |
|  |  Router Lambda (openclaw-router)             |                                         |
|  |                                              |                                         |
|  |  1. Validate webhook signature               |                                         |
|  |     - Telegram: X-Telegram-Bot-Api-Secret-   |                                         |
|  |       Token header                           |                                         |
|  |     - Slack: X-Slack-Signature HMAC-SHA256   |                                         |
|  |  2. Self-invoke async (return 200 to caller) |                                         |
|  |  3. Resolve user in DynamoDB identity table  |                                         |
|  |  4. Get/create per-user AgentCore session    |                                         |
|  |  5. InvokeAgentRuntime(sessionId=per-user)   |                                         |
|  |  6. Send response back to channel API        |                                         |
|  +----------------------+-----------------------+                                         |
|                         |                                                                  |
|          +--------------+--------------+                                                   |
|          |                             |                                                   |
|  +-------v--------+          +--------v---------+                                         |
|  | DynamoDB       |          | Secrets Manager   |                                         |
|  | (identity)     |          | - gateway-token   |                                         |
|  |                |          | - webhook-secret  |                                         |
|  | CHANNEL# items |          | - cognito-password|                                         |
|  | USER# items    |          | - channels/*      |                                         |
|  | SESSION items  |          +-------------------+                                         |
|  | BIND# items    |                                                                        |
|  | ALLOW# items   |                                                                        |
|  | CRON# items    |                                                                        |
|  +----------------+                                                                        |
|                         |                                                                  |
|  +---------------------------------------------------+                                    |
|  |  VPC (10.0.0.0/16)                                |                                    |
|  |  +----------------------------------------------+ |                                    |
|  |  |  Public Subnets (2 AZ)                       | |                                    |
|  |  |  +----------------+  +---------------------+ | |                                    |
|  |  |  | NAT Gateway    |  | Internet Gateway    | | |                                    |
|  |  |  +-------+--------+  +----------+----------+ | |                                    |
|  |  +----------|-------------------------|---------+ |                                    |
|  |  |          v                         |           |                                    |
|  |  |  Private Subnets (2 AZ)            |           |                                    |
|  |  |                                                |                                    |
|  |  |  +------------------------------------------+  |                                    |
|  |  |  |  AgentCore Runtime Container (ARM64)     |  |                                    |
|  |  |  |  (per-user microVM — managed serverless) |  |                                    |
|  |  |  |                                          |  |                                    |
|  |  |  |  +--------------+  +-----------------+   |  |                                    |
|  |  |  |  | agentcore-   |  | OpenClaw        |   |  |                                    |
|  |  |  |  | contract.js  |  | Gateway         |   |  |                                    |
|  |  |  |  | (port 8080)  |  | (port 18789)    |   |  |                                    |
|  |  |  |  | - /ping      |  | - headless mode |   |  |                                    |
|  |  |  |  | - /invoke    |  | - no channels   |   |  |                                    |
|  |  |  |  | - WS bridge  |  | - tools & skills|   |  |                                    |
|  |  |  |  +--------------+  +--------+--------+   |  |                                    |
|  |  |  |                             |            |  |                                    |
|  |  |  |                    +--------v--------+   |  |                                    |
|  |  |  |                    | agentcore-      |   |  |                                    |
|  |  |  |                    | proxy.js        |   |  |                                    |
|  |  |  |                    | (port 18790)    |   |  |                                    |
|  |  |  |                    | - OpenAI compat |   |  |                                    |
|  |  |  |                    | - Converse API  |   |  |                                    |
|  |  |  |                    | - SSE streaming |   |  |                                    |
|  |  |  |                    +--------+--------+   |  |                                    |
|  |  |  +---------------------|--------------------+  |                                    |
|  |  |                        |                       |                                    |
|  |  |  +---------------------v--------------------+  |                                    |
|  |  |  |  VPC Endpoints (Interface)               |  |                                    |
|  |  |  |  - bedrock-runtime    - ecr.api          |  |                                    |
|  |  |  |  - secretsmanager     - ecr.dkr          |  |                                    |
|  |  |  |  - logs               - monitoring       |  |                                    |
|  |  |  |  - ssm                                   |  |                                    |
|  |  |  |  + S3 Gateway Endpoint                   |  |                                    |
|  |  |  +------------------------------------------+  |                                    |
|  |  +------------------------------------------------+                                    |
|  |                            |                                                            |
|  |         +------------------+------------------+                                         |
|  |         |                                     |                                         |
|  |  +------v-----------+               +---------v----------+                              |
|  |  | Amazon Bedrock   |               | S3 User Files      |                              |
|  |  | ConverseStream   |               | Bucket             |                              |
|  |  | API              |               | - {ns}/.openclaw/  |                              |
|  |  | MiniMax M2.1     |               | - {ns}/files/      |                              |
|  |  +------------------+               +--------------------+                              |
|  |                                                                                         |
+--+-----------------------------------------------------------------------------------------+
```

## Per-User Session Lifecycle

```
  User sends first message on Telegram
         |
         v
  API Gateway HTTP API (POST /webhook/telegram)
         |
         v
  Router Lambda validates X-Telegram-Bot-Api-Secret-Token
         |
         v
  Self-invoke async (returns 200 to Telegram immediately)
         |
         v
  Resolve user in DynamoDB (create if new)
  Get/create session (ses_{user_id}_{uuid})
         |
         v
  InvokeAgentRuntime(runtimeSessionId = per-user session ID)
         |
         v
  AgentCore creates new microVM for this session
         |
         v
  Container starts -> contract server (port 8080) -> /ping = Healthy
         |
         v
  First /invocations {action: "chat"}:
    1. Restore .openclaw/ from S3  (workspace-sync.js)
    2. Start agentcore-proxy.js (port 18790) with USER_ID env
    3. Write headless OpenClaw config (no channels)
    4. Start OpenClaw gateway (port 18789, ~4 min startup)
    5. Start periodic workspace saves (every 5 min)
         |
         v
  WebSocket bridge: auth -> chat.send -> streaming deltas -> final
         |
         v
  Router Lambda sends response to Telegram via sendMessage API
         |
         v
  (Subsequent messages reuse the warm microVM — fast response)
         |
  ... idle for 30 min (configurable) ...
         |
         v
  AgentCore sends SIGTERM:
    1. Save .openclaw/ to S3  (final workspace save)
    2. Kill child processes
    3. Exit
         |
         v
  (Next message: new microVM created, workspace restored from S3)
```

## Container Internal Architecture

```
+-----------------------------------------------------------------------+
|  AgentCore Runtime Container (node:22-slim, ARM64, per-user)          |
|                                                                       |
|  entrypoint.sh starts contract server immediately:                    |
|                                                                       |
|  agentcore-contract.js (port 8080)         <-- MUST START FIRST      |
|    |-- GET /ping -> {"status":"Healthy"}   (allows idle termination)  |
|    |-- POST /invocations {action:"chat"}   (triggers lazy init)      |
|    |-- POST /invocations {action:"status"} (health info)             |
|    |                                                                  |
|    |-- On first chat (lazy init):                                    |
|    |   1. Fetch secrets from Secrets Manager                         |
|    |   2. Restore .openclaw/ from S3 (workspace-sync.js)             |
|    |   3. Start agentcore-proxy.js (port 18790)                      |
|    |   4. Write headless OpenClaw config (no channels)               |
|    |   5. Start OpenClaw gateway (port 18789) — ~4 min startup       |
|    |   6. Start periodic workspace saves                              |
|    |                                                                  |
|    |-- On subsequent chats:                                          |
|    |   WebSocket bridge to OpenClaw:                                 |
|    |   connect -> auth(token) -> chat.send -> deltas -> final        |
|    |                                                                  |
|    |-- On SIGTERM:                                                   |
|        Save .openclaw/ to S3 -> kill children -> exit                |
|                                                                       |
|  agentcore-proxy.js (port 18790)                                      |
|    |-- POST /v1/chat/completions -> Bedrock ConverseStream            |
|    |-- GET /v1/models -> available models                             |
|    |-- GET /health -> proxy status                                    |
|    |-- Cognito auto-provisioning (HMAC passwords)                     |
|    |-- Per-user workspace files (AGENTS.md, SOUL.md, etc.)            |
|                                                                       |
|  OpenClaw Gateway (port 18789) — headless mode                        |
|    |-- No channel connections (messages bridged via WebSocket)         |
|    |-- Full tool profile (web, filesystem, runtime, sessions, etc.)   |
|    |-- 4 custom skills (s3-user-files, eventbridge-cron,              |
|    |       clawhub-manage, api-keys)                                  |
|                                                                       |
|  NODE_OPTIONS: --dns-result-order=ipv4first                           |
|                --no-network-family-autoselection                       |
|                -r /app/force-ipv4.js                                  |
+-----------------------------------------------------------------------+
```

## Observability Pipeline

```
+------------------+     +--------------------+     +------------------+
| Amazon Bedrock   |     | CloudWatch Logs    |     | Lambda           |
| ConverseStream   |---->| /aws/bedrock/      |---->| token-metrics    |
| (invocations)    |     | invocation-logs    |     | processor        |
+------------------+     +--------------------+     +--------+---------+
                                                             |
                                              +--------------+--------------+
                                              |                             |
                                     +--------v--------+          +--------v--------+
                                     | DynamoDB        |          | CloudWatch      |
                                     | (single-table)  |          | Custom Metrics  |
                                     |                 |          | OpenClaw/       |
                                     | PK: USER#id     |          | TokenUsage      |
                                     | SK: DATE#...    |          |                 |
                                     | GSI1: CHANNEL#  |          | - InputTokens   |
                                     | GSI2: MODEL#    |          | - OutputTokens  |
                                     | GSI3: DATE/COST |          | - TotalTokens   |
                                     | TTL: 90 days    |          | - EstCostUSD    |
                                     +-----------------+          +--------+--------+
                                                                           |
                                                                  +--------v--------+
                                                                  | CloudWatch      |
                                                                  | Dashboards      |
                                                                  | + Alarms        |
                                                                  |                 |
                                                                  | - Operations    |
                                                                  | - Token         |
                                                                  |   Analytics     |
                                                                  | - Budget alarms |
                                                                  | - Anomaly det.  |
                                                                  +---------+-------+
                                                                            |
                                                                   +--------v--------+
                                                                   | SNS Topic       |
                                                                   | (alarm notif.)  |
                                                                   +-----------------+
```

## Identity Flow

```
  Telegram user sends message
         |
         v
  API Gateway -> Router Lambda
         |
         v
  Extract channel user ID (e.g. "telegram:123456789")
         |
         v
  DynamoDB lookup: CHANNEL#telegram:123456789
         |
         +-- Not found: create new user (user_<uuid>)
         |   -> CHANNEL# item, USER# profile, SESSION item
         +-- Found: get existing userId
         |
         v
  InvokeAgentRuntime(sessionId from DynamoDB SESSION item)
         |
         v
  Container starts -> agentcore-proxy.js
         |
         v
  USER_ID env var set by contract server (e.g. "telegram:123456789")
         |
         v
  derivePassword(actorId) = HMAC-SHA256(secret, actorId).slice(0, 32)
         |
         +-- Cognito AdminGetUser (check if exists)
         |      |
         |      +-- Not found: AdminCreateUser + AdminSetUserPassword
         |      +-- Found: continue
         |
         v
  AdminInitiateAuth (ADMIN_USER_PASSWORD_AUTH)
         |
         v
  JWT IdToken (cached per user, 60s early refresh)
         |
         v
  Bedrock ConverseStream call with user-specific workspace context
```

## Cross-Channel Account Linking

```
  User on Telegram: "link"
         |
         v
  Router Lambda generates 8-char bind code
  Stores in DynamoDB: BIND#A1B2C3D4 -> userId (10 min TTL)
         |
         v
  Bot replies: "Your link code is A1B2C3D4 (valid 10 min)"

  User on Slack: "link A1B2C3D4"
         |
         v
  Router Lambda looks up BIND#A1B2C3D4
  Finds userId from Telegram
         |
         v
  Creates CHANNEL#slack:U12345 -> same userId
  Creates USER#userId CHANNEL#slack:U12345 record
  Deletes bind code
         |
         v
  Both channels now route to same user, session, and workspace
```

## CDK Stack Dependencies

```
  OpenClawVpc ─────────────┐
                            │
  OpenClawSecurity ─────────┤
                            │
                    ┌───────v───────┐
                    │ OpenClawAgent │
                    │ Core          │
                    └───────┬───────┘
                            │
              ┌─────────────┼─────────────┐
              │             │             │
      ┌───────v───────┐     │     ┌───────v───────┐
      │ OpenClawRouter│     │     │ OpenClawCron  │
      └───────────────┘     │     └───────────────┘
                            │
  OpenClawObservability ────┤
                            │
                    ┌───────v───────┐
                    │ OpenClawToken │
                    │ Monitoring    │
                    └───────────────┘
```

## Security Controls

| Layer | Control | Details |
|---|---|---|
| API Gateway | Explicit routes | Only `/webhook/telegram`, `/webhook/slack`, `/health` — all others 404 |
| API Gateway | Throttling | Burst: 50, sustained: 100 req/s |
| Webhook | Telegram validation | `X-Telegram-Bot-Api-Secret-Token` header against Secrets Manager secret |
| Webhook | Slack validation | `X-Slack-Signature` HMAC-SHA256 with 5-minute replay window |
| Network | VPC + private subnets | Container runs in private subnets, egress via NAT |
| Network | SG egress HTTPS only | Container outbound traffic restricted to TCP 443 |
| Network | VPC endpoints (7) | Bedrock, ECR, Secrets Manager, Logs, Metrics, SSM, S3 |
| Encryption | KMS CMK | All data encrypted with customer-managed key (S3, DynamoDB, SNS, CloudTrail, Secrets Manager) |
| Secrets | Secrets Manager | 7 secrets: gateway token, webhook secret, cognito HMAC, 4 channel tokens |
| Identity | DynamoDB | Channel-to-user mapping, cross-channel binding, session management |
| Identity | Cognito User Pool | Auto-provisioned users with HMAC-derived passwords |
| IAM | Least privilege | cdk-nag AwsSolutions checks enforced at synth |
| Storage | S3 encryption | KMS-encrypted user files bucket, SSL enforced, public access blocked |
| Audit | CloudTrail | API call logging with S3 storage and file validation |
| Monitoring | CloudWatch alarms | Error rates, latency, throttles, budget thresholds |
| Container | Tool deny list | `read` tool blocked (prevents credential access); `exec` allowed for skill management (STS-scoped); proxy on loopback only |
| Cost | Token budgets | Daily token/cost alarms with anomaly detection |
