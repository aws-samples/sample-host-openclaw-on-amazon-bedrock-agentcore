# OpenClaw on AgentCore — Solution Architecture

## High-Level Architecture

```
                                                      +---------------------------+
                                                      |        End Users          |
                                                      |  Telegram  Discord  Slack |
                                                      +--+--------+--------+-----+
                                                         |        |        |
                                              (bot APIs via HTTPS over internet)
                                                         |        |        |
+--------------------------------------------------------+--------+--------+------------------+
|  AWS Account                                                                                |
|  +---------------------------------------------------+                                      |
|  |  VPC (10.0.0.0/16)                                |                                      |
|  |  +----------------------------------------------+ |                                      |
|  |  |  Public Subnets (2 AZ)                       | |                                      |
|  |  |  +----------------+  +---------------------+ | |                                      |
|  |  |  | NAT Gateway    |  | Internet Gateway    | | |                                      |
|  |  |  +-------+--------+  +----------+----------+ | |                                      |
|  |  +----------|-------------------------|---------+ |                                      |
|  |  |          v                         |           |                                      |
|  |  |  Private Subnets (2 AZ)            |           |                                      |
|  |  |                                                |                                      |
|  |  |  +------------------------------------------+  |                                      |
|  |  |  |  AgentCore Runtime Container (ARM64)     |  |                                      |
|  |  |  |  (managed serverless — no EC2/Fargate)   |  |                                      |
|  |  |  |                                          |  |                                      |
|  |  |  |  +--------------+  +-----------------+   |  |                                      |
|  |  |  |  | agentcore-   |  | OpenClaw        |   |  |                                      |
|  |  |  |  | contract.js  |  | Gateway         |   |  |                                      |
|  |  |  |  | (port 8080)  |  | (port 18789)    |   |  |                                      |
|  |  |  |  | - /ping      |  | - Telegram bot  |   |  |                                      |
|  |  |  |  | - /invoke    |  | - Discord bot   |   |  |                                      |
|  |  |  |  +--------------+  | - Slack bot     |   |  |                                      |
|  |  |  |                    +--------+--------+   |  |                                      |
|  |  |  |                             |            |  |                                      |
|  |  |  |                    +--------v--------+   |  |                                      |
|  |  |  |                    | agentcore-      |   |  |                                      |
|  |  |  |                    | proxy.js        |   |  |                                      |
|  |  |  |                    | (port 18790)    |   |  |                                      |
|  |  |  |                    | - OpenAI compat |   |  |                                      |
|  |  |  |                    | - Converse API  |   |  |                                      |
|  |  |  |                    | - SSE streaming |   |  |                                      |
|  |  |  |                    +--------+--------+   |  |                                      |
|  |  |  +---------------------|--------------------+  |                                      |
|  |  |                        |                       |                                      |
|  |  |  +---------------------v--------------------+  |                                      |
|  |  |  |  VPC Endpoints (Interface)               |  |                                      |
|  |  |  |  - bedrock-runtime    - ecr.api          |  |                                      |
|  |  |  |  - secretsmanager     - ecr.dkr          |  |                                      |
|  |  |  |  - logs               - monitoring       |  |                                      |
|  |  |  |  - ssm                                   |  |                                      |
|  |  |  |  + S3 Gateway Endpoint                   |  |                                      |
|  |  |  +------------------------------------------+  |                                      |
|  |  +------------------------------------------------+                                      |
|  |                            |                                                              |
|  |         +------------------+------------------+                                           |
|  |         |                                     |                                           |
|  |  +------v-----------+               +---------v----------+                                |
|  |  | Amazon Bedrock   |               | Secrets Manager    |                                |
|  |  | ConverseStream   |               | - gateway-token    |                                |
|  |  | API              |               | - cognito-password |                                |
|  |  | Claude Sonnet 4.6|               | - channels/*       |                                |
|  |  +------------------+               +--------------------+                                |
|  |                                                                                           |
+--+-------------------------------------------------------------------------------------------+

```

## Keepalive Architecture

```
+-------------------+         +-------------------+         +----------------------+
| Amazon            |  every  | AWS Lambda        |  invoke | AgentCore Runtime    |
| EventBridge       |-------->| (keepalive)       |-------->| Container            |
| (5-min rule)      |  5 min  |                   |  POST   | /invocations         |
+-------------------+         +-------------------+  action | {"action":"keepalive"}|
                                                    :status +----------------------+
                                                             |
                                                    /ping -> HealthyBusy
                                                    (prevents idle termination)
```

AgentCore Runtime terminates containers after:
- **Idle timeout**: configurable (set to 8 hours)
- **Max lifetime**: 8 hours

The keepalive Lambda invokes the runtime every 5 minutes, and the contract server returns `HealthyBusy` on `/ping` to prevent idle termination.

## Container Internal Architecture

```
+-----------------------------------------------------------------------+
|  AgentCore Runtime Container (node:22-slim, ARM64)                    |
|                                                                       |
|  entrypoint.sh orchestrates startup in order:                         |
|                                                                       |
|  Step 1:  agentcore-contract.js (port 8080)     <-- MUST START FIRST |
|           |-- GET /ping -> {"status":"HealthyBusy"}                   |
|           |-- POST /invocations -> status/keepalive                   |
|           |-- Polls proxy & OpenClaw health every 10s                 |
|                                                                       |
|  Step 2:  Fetch secrets from Secrets Manager                          |
|           |-- Gateway token                                           |
|           |-- Cognito HMAC secret                                     |
|           |-- Channel bot tokens (Telegram, Discord, Slack)           |
|           |-- Skip channels with placeholder tokens                   |
|                                                                       |
|  Step 3:  agentcore-proxy.js (port 18790)                             |
|           |-- POST /v1/chat/completions -> Bedrock ConverseStream     |
|           |-- GET /v1/models -> available models                      |
|           |-- GET /health -> proxy status                             |
|           |-- Cognito auto-provisioning (HMAC passwords)              |
|           |-- Retry logic (3x exponential backoff)                    |
|                                                                       |
|  Step 4:  Write /root/.openclaw/openclaw.json                         |
|           |-- Model provider: agentcore (localhost:18790)              |
|           |-- Enabled channels with valid tokens                      |
|           |-- Gateway auth token                                      |
|                                                                       |
|  Step 5:  openclaw gateway run (port 18789) [foreground]              |
|           |-- Telegram: polling via getUpdates                        |
|           |-- Discord: WebSocket gateway                              |
|           |-- Slack: RTM or Events API                                |
|           |-- Routes messages to proxy as OpenAI completions          |
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
  OpenClaw extracts actor ID (e.g. "telegram:6087229962")
         |
         v
  agentcore-proxy.js receives chat completion request
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
  [Future: pass JWT to AgentCore Gateway for enforcement]
  [Current: direct Bedrock call with SigV4]
```

## CDK Stack Dependencies

```
  OpenClawVpc ─────────────┐
                           │
  OpenClawSecurity ────────┤
                           │
                   ┌───────v───────┐
                   │ OpenClawAgent │
                   │ Core          │
                   └───────┬───────┘
                           │
                   ┌───────v───────┐
                   │ OpenClawKeep  │
                   │ alive         │
                   └───────────────┘

  OpenClawObservability ───┐
                           │
                   ┌───────v───────┐
                   │ OpenClawToken │
                   │ Monitoring    │
                   └───────────────┘
```

## Security Controls

| Layer | Control | Details |
|---|---|---|
| Network | VPC + private subnets | Container runs in private subnets, egress via NAT |
| Network | VPC endpoints (7) | Bedrock, ECR, Secrets Manager, Logs, Metrics, SSM, S3 |
| Encryption | KMS CMK | All secrets encrypted with customer-managed key |
| Secrets | Secrets Manager | Bot tokens, gateway token, Cognito HMAC secret |
| Identity | Cognito User Pool | Auto-provisioned users with HMAC-derived passwords |
| IAM | Least privilege | cdk-nag AwsSolutions checks enforced at synth |
| Audit | CloudTrail | API call logging with S3 storage |
| Monitoring | CloudWatch alarms | Error rates, latency, throttles, budget thresholds |
| Cost | Token budgets | Daily token/cost alarms with anomaly detection |
