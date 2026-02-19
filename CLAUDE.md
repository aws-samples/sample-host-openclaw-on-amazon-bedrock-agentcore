# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Re-architecture of the OpenClaw-on-AWS single EC2 deployment to run on AWS Bedrock AgentCore's managed serverless runtime. The project replaces EC2 with AgentCore Runtime (Strands Agents) for AI reasoning and ECS Fargate for the OpenClaw Node.js messaging bridge, adding session isolation, memory, observability, and granular token usage monitoring.

## Tech Stack

- **Infrastructure**: CDK v2 (Python)
- **Agent**: Strands Agents (Python) on Bedrock AgentCore Runtime
- **Messaging Bridge**: OpenClaw (Node.js) containerized on ECS Fargate
- **Models**: Claude Sonnet 4.6 (default: `au.anthropic.claude-sonnet-4-6` via Bedrock cross-region inference)
- **Identity**: Cognito User Pool + AgentCore WorkloadIdentity (HMAC-derived passwords, JWT tokens)
- **Observability**: CloudWatch, X-Ray, OpenTelemetry, custom CloudWatch dashboards
- **Token Monitoring**: Lambda + DynamoDB + CloudWatch custom metrics
- **Security**: VPC endpoints, WAF, CloudFront, KMS, Secrets Manager, cdk-nag

## Architecture

```
                              +--------------------------+
                              |     Users / Clients      |
                              | Telegram  Discord  Slack |
                              |       Web Browser        |
                              +------------+-------------+
                                           |
                              +------------v-------------+
                              |   CloudFront (CDN/TLS)   |
                              |   + WAF (rate limiting)   |
                              |   + CF Function (token    |
                              |     auth, Web UI only)    |
                              +------------+-------------+
                                           |
                            HTTPS (origin-facing IPs only)
                                           |
                              +------------v-------------+
                              |     Public ALB           |
                              | (restricted to CF IPs    |
                              |  via managed prefix list)|
                              +------------+-------------+
                                           |
                         +-----------------+------------------+
                         |        Private Subnet (VPC)        |
                         |                                    |
                         |  +------------------------------+  |
                         |  |    ECS Fargate Task          |  |
                         |  |                              |  |
                         |  |  +------------------------+  |  |
                         |  |  | OpenClaw Gateway       |  |  |
                         |  |  | (port 18789)           |  |  |
                         |  |  | - Channel providers    |  |  |
                         |  |  | - WebSocket / Web UI   |  |  |
                         |  |  +----------+-------------+  |  |
                         |  |             |                 |  |
                         |  |  +----------v-------------+  |  |
                         |  |  | agentcore-proxy.js     |  |  |
                         |  |  | (port 18790)           |  |  |
                         |  |  | - OpenAI -> Bedrock    |  |  |
                         |  |  | - Cognito auto-prov.   |  |  |
                         |  |  | - JWT token caching    |  |  |
                         |  |  | - SSE streaming        |  |  |
                         |  |  +----------+-------------+  |  |
                         |  +------------------------------+  |
                         |                |                    |
                         |     +----------+----------+        |
                         |     |  PROXY_MODE switch  |        |
                         |     +----+----------+----+         |
                         |          |          |              |
                         |  bedrock-direct  agentcore         |
                         |          |          |              |
                         +----------+----------+--------------+
                                    |          |
                         +----------v---+  +---v--------------+
                         | Bedrock API  |  | AgentCore Runtime|
                         | ConverseStr. |  | (VPC mode)       |
                         | Claude 4.6   |  | Strands Agent    |
                         +--------------+  +---+---------+----+
                                               |         |
                                        +------v--+  +---v----------+
                                        | Bedrock |  | AgentCore    |
                                        | Claude  |  | Memory       |
                                        | 4.6     |  | (semantic,   |
                                        +---------+  |  user-prefs, |
                                                     |  summary)    |
                                                     +--------------+

  +----------------------------------------------------------------+
  |                    Identity Flow                                |
  |                                                                |
  |  OpenClaw system prompt contains "chat_id": "telegram:NNN"     |
  |    -> Proxy extracts actorId + channel from system message     |
  |    -> Fallbacks: Session: line, "channel" field, Runtime line, |
  |       x-openclaw-* headers, parsed.user, "default-user"       |
  |    -> Proxy derives HMAC password from secret + actorId        |
  |    -> AdminCreateUser (if new) + AdminSetUserPassword          |
  |    -> AdminInitiateAuth -> JWT IdToken                         |
  |    -> Token cached per user (60s early refresh)                |
  |    -> CfnWorkloadIdentity registered in AgentCore              |
  |    -> Runtime JWT authorizer configured (Cognito OIDC)         |
  |    -> [Enforcement requires CfnGateway — future]               |
  +----------------------------------------------------------------+

  +----------------------------------------------------------------+
  |                 Observability Pipeline                          |
  |                                                                |
  |  Bedrock invocation logs -> CloudWatch Logs                    |
  |    -> Lambda processor -> DynamoDB (token usage, single-table) |
  |    -> CloudWatch custom metrics -> Dashboards + Budget alarms  |
  +----------------------------------------------------------------+
```

### Request Flow Summary

| Component | Port | Role |
|---|---|---|
| CloudFront | 443 | TLS termination, WAF, CF Function token auth |
| Public ALB | 443 | Routes to Fargate (restricted to CF origin IPs) |
| OpenClaw Gateway | 18789 | WebSocket, Web UI, channel management |
| agentcore-proxy.js | 18790 | OpenAI-to-Bedrock translation, per-user identity extraction, Cognito auto-provisioning, SSE streaming |
| Bedrock ConverseStream | - | Direct model invocation (`bedrock-direct` mode) |
| AgentCore Runtime | - | Managed agent runtime with memory (`agentcore` mode) |

### Key Design Decisions

- Fargate handles WebSocket connections, Web UI, and channel management; a local proxy adapter translates OpenAI-format requests to either direct Bedrock ConverseStream API calls or AgentCore Runtime invocations (controlled by `PROXY_MODE` env var)
- Public ALB (not internal) because CloudFront VPC Origins do not support WebSocket upgrade — ALB is restricted to CloudFront origin-facing IPs only via managed prefix list
- CloudFront Function validates gateway token (exact value comparison, not presence-only)
- All secrets in Secrets Manager with KMS CMK encryption, never in code or env vars
- OpenClaw `controlUi.allowInsecureAuth: true` is required because ALB->Fargate is HTTP (TLS terminates at CloudFront)
- Node.js 22 in VPC without IPv6 requires `force-ipv4.js` DNS patch (see Gotchas)
- Channel tokens are validated at startup — channels with placeholder/missing tokens are skipped to prevent retry loops
- Cognito passwords are HMAC-derived (deterministic, never stored) — enables `AdminInitiateAuth` without per-user password storage

## Project Structure

```
openclaw-on-agentcore/
  app.py                          # CDK app entry point — wires all stacks
  cdk.json                        # CDK context config (model, sizing, thresholds)
  requirements.txt                # Python deps (aws-cdk-lib, cdk-nag)
  implementation-guide.md         # 10-task implementation plan (all complete)
  stacks/
    __init__.py                   # Shared helper (retention_days enum converter)
    vpc_stack.py                  # VPC, subnets, NAT, VPC endpoints, flow logs
    security_stack.py             # KMS CMK, Secrets Manager, Cognito User Pool, CloudTrail
    agentcore_stack.py            # Runtime, Endpoint, Memory, WorkloadIdentity, IAM roles
    fargate_stack.py              # ECS cluster, Fargate service, ALB, task def, IAM
    edge_stack.py                 # CloudFront, WAF, CF Function (token auth)
    observability_stack.py        # Dashboards, alarms, invocation log group
    token_monitoring_stack.py     # Lambda processor, DynamoDB, analytics dashboard
  agent/
    my_agent.py                   # Strands Agent (Python) — BedrockAgentCoreApp entrypoint
    Dockerfile                    # Agent container image
    requirements.txt              # Agent Python deps
  bridge/
    agentcore-proxy.js            # Proxy adapter: OpenAI -> Bedrock/AgentCore + identity extraction + Cognito
    entrypoint.sh                 # Container startup: fetch secrets, write config, launch
    force-ipv4.js                 # DNS patch for Node.js 22 IPv6 issue
    Dockerfile                    # Bridge container image (node:22-slim + OpenClaw)
  lambda/
    token_metrics/index.py        # Bedrock invocation log processor -> DynamoDB + CW metrics
  scripts/
    deploy.sh                     # Full deployment script
    rotate-token.sh               # Gateway token rotation
    test-e2e.js                   # WebSocket e2e streaming test
```

## CDK Stacks (7 stacks)

| Stack | Key Resources | Cross-Stack Dependencies |
|---|---|---|
| **OpenClawVpc** | VPC, subnets (2 AZ), NAT, 7 VPC endpoints, flow logs | None |
| **OpenClawSecurity** | KMS CMK, Secrets Manager (gateway token, channel tokens, Cognito password), Cognito User Pool + app client, CloudTrail | None |
| **OpenClawAgentCore** | CfnRuntime, CfnRuntimeEndpoint, CfnMemory, CfnWorkloadIdentity, agent SG, IAM roles | VPC, Security (CMK, Cognito) |
| **OpenClawFargate** | ECS cluster, Fargate service, ALB, task definition, IAM | VPC, Security (secrets, CMK, Cognito), AgentCore (IDs) |
| **OpenClawEdge** | CloudFront distribution, WAF WebACL, CF Function | Fargate (ALB) |
| **OpenClawObservability** | Operations dashboard, invocation log group, alarms, SNS topic | Fargate (service) |
| **OpenClawTokenMonitoring** | Lambda processor, DynamoDB (single-table), analytics dashboard | Observability (log group, SNS) |

## Expected Commands

### Full Deployment (recommended for first-time or complete redeploy)
```bash
./scripts/deploy.sh                            # full deploy: CDK stacks + Docker images + ECS restart
./scripts/deploy.sh --skip-images              # CDK stacks only (no image rebuild)
./scripts/deploy.sh --profile my-profile       # use a specific AWS profile
```

The deploy script handles:
1. Install CDK Python dependencies
2. `cdk synth` (includes cdk-nag checks)
3. Deploy foundation stacks (VPC, Security, AgentCore, Fargate) — creates ECR repos
4. Build and push Docker images (bridge + agent) to ECR
5. Force new ECS deployment to pick up latest images
6. Deploy remaining stacks (Edge, Observability, Token Monitoring)
7. Print deployment summary with URLs and next steps

### CDK (manual)
```bash
cdk synth                                    # synthesize (runs from project root)
cdk deploy --all --require-approval never     # deploy all stacks
cdk deploy OpenClawFargate                    # deploy single stack
cdk diff                                      # preview changes
cdk destroy                                   # tear down
```

### Fargate / Docker (manual image deploy)
```bash
sudo docker build -t openclaw-bridge bridge/                                              # build image
aws ecr get-login-password --region ap-southeast-2 | sudo docker login --username AWS --password-stdin 657117630614.dkr.ecr.ap-southeast-2.amazonaws.com
sudo docker tag openclaw-bridge:latest 657117630614.dkr.ecr.ap-southeast-2.amazonaws.com/openclaw-bridge:latest
sudo docker push 657117630614.dkr.ecr.ap-southeast-2.amazonaws.com/openclaw-bridge:latest # push to ECR
aws ecs update-service --cluster OpenClawFargate-ClusterEB0386A7-jBeMl7IesCR7 \
    --service OpenClawFargate-BridgeService9466B11E-alCU8E0HEeqN \
    --force-new-deployment --region ap-southeast-2                                         # deploy new image
```

### E2E Test
```bash
GATEWAY_TOKEN=$(aws secretsmanager get-secret-value --secret-id openclaw/gateway-token --region ap-southeast-2 --query SecretString --output text)
node scripts/test-e2e.js  # requires GATEWAY_TOKEN env var
```

### Channel Setup
```bash
# Telegram: get token from @BotFather, then:
aws secretsmanager update-secret --secret-id openclaw/channels/telegram --secret-string 'BOT_TOKEN' --region ap-southeast-2

# Discord: get token from Developer Portal, then:
aws secretsmanager update-secret --secret-id openclaw/channels/discord --secret-string 'BOT_TOKEN' --region ap-southeast-2

# Slack: get token from api.slack.com/apps, then:
aws secretsmanager update-secret --secret-id openclaw/channels/slack --secret-string 'BOT_TOKEN' --region ap-southeast-2

# After updating any token, force a new deployment:
aws ecs update-service --cluster OpenClawFargate-ClusterEB0386A7-jBeMl7IesCR7 \
    --service OpenClawFargate-BridgeService9466B11E-alCU8E0HEeqN \
    --force-new-deployment --region ap-southeast-2
```

### AgentCore Runtime / Agent Docker
```bash
sudo docker build -t openclaw-agent agent/                                                  # build agent image
aws ecr get-login-password --region ap-southeast-2 | sudo docker login --username AWS --password-stdin 657117630614.dkr.ecr.ap-southeast-2.amazonaws.com
sudo docker tag openclaw-agent:latest 657117630614.dkr.ecr.ap-southeast-2.amazonaws.com/openclaw-agent:latest
sudo docker push 657117630614.dkr.ecr.ap-southeast-2.amazonaws.com/openclaw-agent:latest    # push to ECR (must exist before CfnRuntime deploy)

# Check runtime status
aws bedrock-agentcore get-runtime --agent-runtime-id <RUNTIME_ID> --region ap-southeast-2
aws bedrock-agentcore get-runtime-endpoint --agent-runtime-endpoint-id <ENDPOINT_ID> --region ap-southeast-2
aws bedrock-agentcore get-memory --memory-id <MEMORY_ID> --region ap-southeast-2
```

### Proxy Mode Switch
```bash
# Switch to AgentCore mode (update cdk.json proxy_mode -> "agentcore", then redeploy Fargate):
cdk deploy OpenClawFargate -c proxy_mode=agentcore

# Rollback to direct Bedrock mode:
cdk deploy OpenClawFargate -c proxy_mode=bedrock-direct
```

### Cognito Identity
```bash
# List auto-provisioned users
aws cognito-idp list-users --user-pool-id <POOL_ID> --region ap-southeast-2

# Check a specific user
aws cognito-idp admin-get-user --user-pool-id <POOL_ID> --username "telegram:6087229962" --region ap-southeast-2

# List workload identities
aws bedrock-agentcore list-workload-identities --region ap-southeast-2
```

### Security Validation
```bash
cdk synth  # should pass cdk-nag checks with no errors
```

## DynamoDB Token Usage Table Design (Task 9)

Single-table design with composite keys:
- **PK**: `USER#<actor_id>`, **SK**: `DATE#<yyyy-mm-dd>#CHANNEL#<channel>#SESSION#<session_id>`
- **GSI1**: channel aggregation -- PK: `CHANNEL#<channel>`, SK: `DATE#<yyyy-mm-dd>`
- **GSI2**: model aggregation -- PK: `MODEL#<model_id>`, SK: `DATE#<yyyy-mm-dd>`
- **GSI3**: daily cost ranking -- PK: `DATE#<yyyy-mm-dd>`, SK: `COST#<estimated_cost>`
- TTL for automatic cleanup (default 90 days)

## Key Configuration Points

- CDK context variables in `cdk.json` control all tunable thresholds (daily token budget, cost budget, anomaly detection band width, TTL days)
- **New deployers**: update `account` and `region` in `cdk.json` to match your AWS account before deploying; `cloudfront_domain` will be set after the first `cdk deploy OpenClawEdge`
- Proxy mode: `proxy_mode` in `cdk.json` -- `"bedrock-direct"` (default) or `"agentcore"` -- controls whether proxy routes through AgentCore Runtime or calls Bedrock directly
- Default Bedrock model: `au.anthropic.claude-sonnet-4-6` (set in `cdk.json` -> Fargate env var -> proxy)
- CloudFront domain: set in `cdk.json` as `cloudfront_domain` -> Fargate env var -> entrypoint.sh `allowedOrigins`
- Fargate sizing: 256 CPU / 1024 MiB (configurable via `cdk.json`)
- WAF rate limiting: 100 req/5min per IP
- CloudWatch log retention: 30 days for Fargate container logs
- OpenClaw startup takes ~4 minutes (plugin registration, bonjour, etc.) before channels connect
- Container images are **not** pre-built -- users must build and push Docker images to ECR (the `deploy.sh` script handles this automatically)
- ECR repositories (`openclaw-bridge`, `openclaw-agent`) are created by CDK in the Fargate and AgentCore stacks respectively

## Deployment Status

All 7 CDK stacks deployed to account 657117630614 / ap-southeast-2:
- OpenClawVpc, OpenClawSecurity, OpenClawAgentCore, OpenClawFargate, OpenClawEdge, OpenClawObservability, OpenClawTokenMonitoring
- CloudFront URL: `https://d34s8ria53v6u2.cloudfront.net`
- Fargate service running and healthy
- Telegram channel: connected and responding (`@Openclaw_agentcore_bot`)
- Discord/Slack channels: placeholder tokens -- update via Secrets Manager when ready
- WebSocket e2e streaming: verified working through CloudFront

### AI Path (Feature-Flagged)

The proxy supports two modes controlled by the `PROXY_MODE` environment variable (set in `cdk.json`):

**`bedrock-direct` (default)** -- Direct Bedrock, no AgentCore:
```
Telegram/Browser -> OpenClaw -> agentcore-proxy.js (port 18790) -> Bedrock ConverseStream API -> Claude Sonnet 4.6
```

**`agentcore`** -- Full AgentCore Runtime with Memory:
```
Telegram/Browser -> OpenClaw -> agentcore-proxy.js (port 18790) -> AgentCore Runtime Endpoint -> Strands Agent -> Bedrock + Memory
```

### AgentCore Component Status

| Component | CDK Resource | Status | Notes |
|---|---|---|---|
| **AgentCore Runtime** | `CfnRuntime` (container-based, VPC mode) | Deployed | `openclaw_agent-Stv3n03Lta` |
| **Runtime Endpoint** | `CfnRuntimeEndpoint` | Deployed | `openclaw_agent_live` |
| **AgentCore Memory** | `CfnMemory` (semantic + user-prefs + summary) | Deployed | `openclaw_memory-ZEvU7zB6fu`, KMS-encrypted, 90-day event expiry |
| **Memory Execution Role** | IAM Role (bedrock + bedrock-agentcore) | Deployed | InvokeModel for memory extraction |
| **Agent SG** | EC2 SecurityGroup in AgentCore stack | Deployed | HTTPS from VPC CIDR |
| **VPC Endpoint** | `bedrock-agentcore-runtime` | **Removed** | Service unavailable in ap-southeast-2; re-add when available |
| **Proxy Feature Flag** | `PROXY_MODE` env var | Active (`bedrock-direct`) | Set to `agentcore` to enable AgentCore path |
| **WorkloadIdentity** | `CfnWorkloadIdentity` | Deployed | `openclaw_identity` |
| **Cognito User Pool** | `cognito.UserPool` in Security stack | Deployed | `ap-southeast-2_sqDasprUW`, self-signup disabled |
| **Cognito App Client** | `UserPoolClient` (`openclaw-proxy`) | Deployed | `4lujqi9ev9bdkjlkaa10nsr0rh`, ADMIN_USER_PASSWORD_AUTH |
| **Password Secret** | Secrets Manager (`openclaw/cognito-password-secret`) | Deployed | HMAC secret for deterministic password derivation |
| **Runtime JWT Authorizer** | `authorizer_configuration` on CfnRuntime | Deployed | Cognito OIDC discovery URL + audience |
| **Proxy Auto-Provisioning** | `agentcore-proxy.js` Cognito integration | Active | Auto-creates users, caches JWT tokens |
| **AgentCore Gateway** | Not configured | **Not deployed** | Required for JWT enforcement (future) |

### Remaining Work

- ~~**Verify Cognito auto-provisioning**~~: ✅ Done — Telegram message auto-creates Cognito user `telegram:6087229962` (CONFIRMED status)
- ~~**Map channel user IDs**~~: ✅ Done — proxy extracts `chat_id` from OpenClaw system prompt JSON (e.g. `"chat_id": "telegram:6087229962"`), with fallbacks for `Session:` line / `"channel"` field / `Runtime:` line / headers / `user` field / `default-user`
- **Switch to `proxy_mode=agentcore`** and verify Telegram still works with memory persistence
- **CfnGateway**: add AgentCore Gateway to enforce JWT auth on runtime invocations (currently preparatory)
- Set up Discord channel (create bot, store token, redeploy)
- Set up Slack channel (pending team approval for Slack app)
- Cognito hosted UI for Web UI authentication (replace gateway token auth, future)
- Validate observability dashboards and alarms

## Gotchas & Patterns

### CDK (Python)
- `logs.RetentionDays` is an enum, not constructable from int -- use the helper in `stacks/__init__.py`
- `SnsAction` lives in `aws_cloudwatch_actions`, not `aws_cloudwatch`
- CloudTrail uses `cloud_watch_log_group` (singular), not `cloud_watch_logs_group`
- Cross-stack cyclic deps: use string ARN params + inline `add_to_policy()` instead of `grant_*()` methods
- ControlTower hook requires `default_root_object` on CloudFront distributions
- AgentCore resources (`CfnRuntime`, `CfnRuntimeEndpoint`, `CfnMemory`, `CfnWorkloadIdentity`) are in `aws_cdk.aws_bedrockagentcore` -- agent image must be pushed to ECR before `CfnRuntime` deploy
- AgentCore resource names must match `^[a-zA-Z][a-zA-Z0-9_]{0,47}$` -- use underscores, not hyphens
- AgentCore `EventExpiryDuration` is in **days** (max 365), not seconds
- AgentCore Memory execution role must trust **both** `bedrock.amazonaws.com` and `bedrock-agentcore.amazonaws.com`
- `bedrock-agentcore-runtime` VPC endpoint service does not exist in ap-southeast-2 -- omit from VPC stack
- Bedrock logging `largeDataDeliveryS3Config` fails validation if `bucketName` is empty -- omit the block entirely
- ALB `add_listener()` auto-creates `0.0.0.0/0` ingress by default -- always use `open=False`
- Removing cross-stack exports: deploy the importing stack first (remove imports), then the exporting stack (remove exports)
- CloudFormation drift on deleted resources: remove from template -> deploy -> add back -> deploy (2-step)

### OpenClaw
- Requires Node >= 22.12.0 -- Dockerfile uses `node:22-slim`
- Correct start command: `openclaw gateway run --port 18789 --bind lan --verbose` (not `openclaw start`)
- Config requires `gateway.mode: "local"` or `--allow-unconfigured` flag
- Auth token key: `gateway.auth.token` (not `gateway.token`)
- WebSocket auth protocol: `type: "req"` / `method: "connect"` with `client.id: "openclaw-control-ui"`, protocol version 3, and `auth: { token }` -- NOT HMAC challenge-response
- `controlUi.allowInsecureAuth: true` is required when ALB->Fargate is HTTP (gateway checks X-Forwarded-Proto)
- `controlUi.allowedOrigins` must include the CloudFront domain for Web UI access
- Channel config is object-keyed: Telegram uses `botToken`, Discord uses `token`, Slack uses `botToken`
- Telegram `dmPolicy: "open"` requires `allowFrom: ["*"]` -- validation error otherwise
- WhatsApp requires interactive session auth (QR code), cannot be configured via secret token
- Gateway is WebSocket-only on port 18789 -- HTTP health checks must target the proxy on port 18790
- Streaming: agent events with `stream: "assistant"` and `data.delta` for text deltas; `chat` events with `state: "final"` for completion
- OpenClaw does NOT pass sender identity (channel user ID, sender name) in HTTP headers or the OpenAI `user` field when calling the model provider endpoint -- the `openai-completions.js:buildParams()` constructs the request with only `model`, `messages`, `stream`, and optional `temperature`/`max_tokens`
- The system prompt includes a JSON example containing `"chat_id": "telegram:6087229962"` and `"channel": "telegram"` -- this is the only reliable signal for per-user identity extraction in the proxy

### Node.js 22 + VPC IPv6 Issue
- **Critical**: Node.js 22's Happy Eyeballs (`autoSelectFamily`) fails in VPCs without IPv6 support
- Symptoms: `ETIMEDOUT` on IPv4 + `ENETUNREACH` on IPv6 for external APIs (Telegram, Discord, etc.)
- `curl` works but Node.js `fetch`/`https.get` fails -- because `autoSelectFamily` tries both address families
- Fix: `bridge/force-ipv4.js` patches `dns.lookup()` to force `family: 4`, loaded via `NODE_OPTIONS="-r /app/force-ipv4.js"`
- Also set `--dns-result-order=ipv4first --no-network-family-autoselection` in NODE_OPTIONS
- `/proc/sys/net/ipv6/conf/all/disable_ipv6` is not writable in Fargate (read-only `/proc/sys`)

### ECS / Fargate
- EC2 instance requires `sudo docker` (ec2-user not in docker group)
- Push image to ECR before deploying stack -- otherwise ECS tasks fail with `CannotPullContainerError`
- ALB auto-creates SG egress for target port but NOT for health check port -- add explicitly
- Force new deployment after image push: `aws ecs update-service --force-new-deployment`
- ROLLBACK_FAILED stacks: delete with `--retain-resources <logicalId>` after they transition to DELETE_FAILED
- OpenClaw takes ~4 minutes from container start to gateway listening (plugin init phase)
- Channel token validation in entrypoint.sh skips Discord/Slack with placeholder tokens to prevent retry loops

### Security
- ALB listeners must use `open=False` to prevent CDK from auto-creating `0.0.0.0/0` ingress rules
- CloudFront Function validates gateway token by exact value (not just presence) -- token read from Secrets Manager at `cdk synth` time via boto3
- Public ALB SG restricted to CloudFront origin-facing IPs via managed prefix list `pl-b8a742d1`

### Cognito Identity
- Cognito User Pool has self-signup disabled -- all users are auto-provisioned by the proxy via `AdminCreateUser`
- Passwords are HMAC-derived: `HMAC-SHA256(secret, actorId)` truncated to 32 chars -- deterministic, never stored
- The HMAC secret is in Secrets Manager (`openclaw/cognito-password-secret`), fetched at container startup
- Cognito usernames are channel-prefixed (e.g. `telegram:6087229962`) -- colons are allowed in Cognito usernames
- JWT tokens are cached per user with 60s early refresh to avoid expiry during requests
- Runtime JWT authorizer is configured but **not enforced** without CfnGateway -- direct SDK invocation uses SigV4
- `AdminInitiateAuth` requires `ADMIN_USER_PASSWORD_AUTH` enabled on the app client
- Actor identity is extracted from OpenClaw's system prompt `"chat_id"` field (e.g. `"chat_id": "telegram:6087229962"`) -- OpenClaw does NOT pass sender identity in headers or the OpenAI `user` field; the system prompt JSON is the only reliable signal
- Identity extraction priority: (1) `x-openclaw-*` headers, (2) `chat_id` from system prompt, (3) `Session:` line from system prompt, (4) `"channel"` field from system prompt, (5) `Runtime:` line, (6) OpenAI `user` field, (7) `default-user`
