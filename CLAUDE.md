# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Re-architecture of the OpenClaw-on-AWS single EC2 deployment to run on AWS Bedrock AgentCore's managed serverless runtime. The project replaces EC2 with AgentCore Runtime (Strands Agents) for AI reasoning and ECS Fargate for the OpenClaw Node.js messaging bridge, adding session isolation, memory, observability, and granular token usage monitoring.

## Tech Stack

- **Infrastructure**: CDK v2 (Python)
- **Agent**: Strands Agents (Python) on Bedrock AgentCore Runtime
- **Messaging Bridge**: OpenClaw (Node.js) containerized on ECS Fargate
- **Models**: Claude Sonnet 4.6 (default: `au.anthropic.claude-sonnet-4-6` via Bedrock cross-region inference)
- **Observability**: CloudWatch, X-Ray, OpenTelemetry, custom CloudWatch dashboards
- **Token Monitoring**: Lambda + DynamoDB + CloudWatch custom metrics
- **Security**: VPC endpoints, WAF, CloudFront, KMS, Secrets Manager, cdk-nag

## Architecture

```
Users (Telegram/Discord/Slack/Browser)
  → CloudFront + WAF + CF Function (token auth, Web UI only)
  → Public ALB (restricted to CloudFront origin-facing IPs)
  → ECS Fargate (OpenClaw messaging bridge + proxy, private subnet)
  → [PROXY_MODE switch]:
      "bedrock-direct" → Bedrock ConverseStream API → Claude Sonnet 4.6
      "agentcore"      → AgentCore Runtime Endpoint → Strands Agent (VPC) → Bedrock + Memory

Identity: Cognito User Pool (admin-provisioned) → Proxy auto-creates users from channel IDs
          → HMAC-derived passwords (secret in Secrets Manager) → JWT tokens cached per user
          → CfnWorkloadIdentity + Runtime JWT authorizer (enforcement requires CfnGateway)

AgentCore Memory (semantic + user-prefs + summary strategies, KMS-encrypted, 90d TTL)

Telegram/Discord/Slack ← OpenClaw channel providers (long-polling, outbound only)

Bedrock invocation logs → CloudWatch Logs → Lambda → DynamoDB (token usage) → CloudWatch custom metrics → dashboards + budget alarms
```

Key decisions:
- Fargate handles WebSocket connections, Web UI, and channel management; a local proxy adapter translates OpenAI-format requests to either direct Bedrock ConverseStream API calls or AgentCore Runtime invocations (controlled by `PROXY_MODE` env var)
- Public ALB (not internal) because CloudFront VPC Origins do not support WebSocket upgrade — ALB is restricted to CloudFront origin-facing IPs only via managed prefix list
- CloudFront Function validates gateway token (exact value comparison, not presence-only)
- All secrets in Secrets Manager with KMS CMK encryption, never in code or env vars
- OpenClaw `controlUi.allowInsecureAuth: true` is required because ALB→Fargate is HTTP (TLS terminates at CloudFront)
- Node.js 22 in VPC without IPv6 requires `force-ipv4.js` DNS patch (see Gotchas)
- Channel tokens are validated at startup — channels with placeholder/missing tokens are skipped to prevent retry loops

## Implementation Structure

The project follows a 10-task sequential implementation plan defined in `implementation-guide.md`:

1. **CDK Project Scaffolding & VPC Foundation** — VPC, subnets, VPC endpoints, security groups, CloudTrail ✅
2. **AgentCore Strands Agent** — `agent/my_agent.py` with BedrockAgentCoreApp entrypoint, OTel instrumentation ✅
3. **AgentCore Memory Integration** — Short-term (session) + long-term memory, namespaced by actor_id ✅
4. **Secrets Management & Security Hardening** — Secrets Manager, KMS CMK, cdk-nag, IAM role separation ✅
5. **OpenClaw Messaging Bridge on Fargate** — `bridge/Dockerfile`, `bridge/entrypoint.sh`, Fargate service ✅
6. **CloudFront + WAF for Web UI** — ALB, CloudFront distribution, WAF rules, token auth ✅
7. **Fargate ↔ AgentCore Integration** — Proxy adapter with Bedrock ConverseStream + SSE streaming ✅
8. **Observability & Bedrock Invocation Logging** — X-Ray, invocation logs, operations dashboard, alarms ✅
9. **Token Usage Monitoring & Budget Alerts** — Lambda processor, DynamoDB single-table design, analytics dashboard ✅
10. **End-to-End Integration Testing** — Telegram channel verified, WebSocket e2e test, streaming confirmed ✅

## Expected Commands

### CDK
```bash
cdk synth                                    # synthesize (runs from project root)
cdk deploy --all --require-approval never     # deploy all stacks
cdk deploy OpenClawFargate                    # deploy single stack
cdk diff                                      # preview changes
cdk destroy                                   # tear down
```

### Fargate / Docker
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
# Switch to AgentCore mode (update cdk.json proxy_mode → "agentcore", then redeploy Fargate):
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
- **GSI1**: channel aggregation — PK: `CHANNEL#<channel>`, SK: `DATE#<yyyy-mm-dd>`
- **GSI2**: model aggregation — PK: `MODEL#<model_id>`, SK: `DATE#<yyyy-mm-dd>`
- **GSI3**: daily cost ranking — PK: `DATE#<yyyy-mm-dd>`, SK: `COST#<estimated_cost>`
- TTL for automatic cleanup (default 90 days)

## Key Configuration Points

- CDK context variables in `cdk.json` control all tunable thresholds (daily token budget, cost budget, anomaly detection band width, TTL days)
- Proxy mode: `proxy_mode` in `cdk.json` — `"bedrock-direct"` (default) or `"agentcore"` — controls whether proxy routes through AgentCore Runtime or calls Bedrock directly
- Default Bedrock model: `au.anthropic.claude-sonnet-4-6` (set in `cdk.json` → Fargate env var → proxy)
- CloudFront domain: set in `cdk.json` as `cloudfront_domain` → Fargate env var → entrypoint.sh `allowedOrigins`
- Fargate sizing: 256 CPU / 1024 MiB (configurable via `cdk.json`)
- WAF rate limiting: 100 req/5min per IP
- CloudWatch log retention: 30 days for Fargate container logs
- OpenClaw startup takes ~4 minutes (plugin registration, bonjour, etc.) before channels connect

## Deployment Status

All 7 CDK stacks deployed to account 657117630614 / ap-southeast-2:
- OpenClawVpc, OpenClawSecurity, OpenClawAgentCore, OpenClawFargate, OpenClawEdge, OpenClawObservability, OpenClawTokenMonitoring
- CloudFront URL: `https://d34s8ria53v6u2.cloudfront.net`
- Fargate service running and healthy
- Telegram channel: connected and responding (`@Openclaw_agentcore_bot`)
- Discord/Slack channels: placeholder tokens — update via Secrets Manager when ready
- WebSocket e2e streaming: verified working through CloudFront

### AI Path (Feature-Flagged)

The proxy supports two modes controlled by the `PROXY_MODE` environment variable (set in `cdk.json`):

**`bedrock-direct` (default)** — Direct Bedrock, no AgentCore:
```
Telegram/Browser → OpenClaw → agentcore-proxy.js (port 18790) → Bedrock ConverseStream API → Claude Sonnet 4.6
```

**`agentcore`** — Full AgentCore Runtime with Memory:
```
Telegram/Browser → OpenClaw → agentcore-proxy.js (port 18790) → AgentCore Runtime Endpoint → Strands Agent → Bedrock + Memory
```

### AgentCore Component Status

| Component | CDK Resource | Status | Notes |
|---|---|---|---|
| **AgentCore Runtime** | `CfnRuntime` (container-based, VPC mode) | CDK resource defined | Requires agent image pushed to ECR before deploy |
| **Runtime Endpoint** | `CfnRuntimeEndpoint` | CDK resource defined | Production endpoint `openclaw-agent-live` |
| **AgentCore Memory** | `CfnMemory` (semantic + user-prefs + summary) | CDK resource defined | KMS-encrypted, 90-day event expiry |
| **Memory Execution Role** | IAM Role (bedrock.amazonaws.com) | CDK resource defined | InvokeModel for memory extraction |
| **Agent SG** | EC2 SecurityGroup in AgentCore stack | CDK resource defined | HTTPS from VPC CIDR |
| **VPC Endpoint** | `bedrock-agentcore-runtime` interface endpoint | CDK resource defined | In VPC stack with other endpoints |
| **Proxy Feature Flag** | `PROXY_MODE` env var | Defaults to `bedrock-direct` | Set to `agentcore` to enable AgentCore path |
| **WorkloadIdentity** | `CfnWorkloadIdentity` | CDK resource defined | `openclaw-identity` — registered in AgentCore |
| **Cognito User Pool** | `cognito.UserPool` in Security stack | CDK resource defined | Self-signup disabled, admin-provisioned only |
| **Cognito App Client** | `UserPoolClient` (`openclaw-proxy`) | CDK resource defined | ADMIN_USER_PASSWORD_AUTH flow |
| **Password Secret** | Secrets Manager (`openclaw/cognito-password-secret`) | CDK resource defined | HMAC secret for deterministic password derivation |
| **Runtime JWT Authorizer** | `authorizer_configuration` on CfnRuntime | CDK resource defined | Cognito OIDC discovery URL + audience |
| **Proxy Auto-Provisioning** | `agentcore-proxy.js` Cognito integration | Implemented | Auto-creates users, caches JWT tokens |
| **AgentCore Gateway** | Not configured | **Not deployed** | Required for JWT enforcement (future) |

### Remaining Work

- **Build & push agent Docker image** to `openclaw-agent` ECR repo before deploying `OpenClawAgentCore` stack
- **Build & push bridge Docker image** with Cognito SDK (`@aws-sdk/client-cognito-identity-provider`)
- **Deploy CDK stacks** in order: VPC → Security → AgentCore → Fargate (with `proxy_mode=bedrock-direct` first)
- **Verify Runtime + Endpoint reach ACTIVE** status before switching proxy mode
- **Switch to `proxy_mode=agentcore`** and verify Telegram still works with memory persistence
- **Verify Cognito auto-provisioning**: send Telegram message → check Cognito console for auto-created user
- **CfnGateway**: add AgentCore Gateway to enforce JWT auth on runtime invocations (currently preparatory)
- **Map channel user IDs**: configure OpenClaw to pass `x-openclaw-actor-id` headers with channel-specific user IDs (e.g. `telegram:6087229962`)
- Set up Discord channel (create bot, store token, redeploy)
- Set up Slack channel (pending team approval for Slack app)
- Cognito hosted UI for Web UI authentication (replace gateway token auth, future)
- Validate observability dashboards and alarms

## Gotchas & Patterns

### CDK (Python)
- `logs.RetentionDays` is an enum, not constructable from int — use the helper in `stacks/__init__.py`
- `SnsAction` lives in `aws_cloudwatch_actions`, not `aws_cloudwatch`
- CloudTrail uses `cloud_watch_log_group` (singular), not `cloud_watch_logs_group`
- Cross-stack cyclic deps: use string ARN params + inline `add_to_policy()` instead of `grant_*()` methods
- ControlTower hook requires `default_root_object` on CloudFront distributions
- AgentCore resources (`CfnRuntime`, `CfnRuntimeEndpoint`, `CfnMemory`) are in `aws_cdk.aws_bedrockagentcore` — agent image must be pushed to ECR before `CfnRuntime` deploy
- Bedrock logging `largeDataDeliveryS3Config` fails validation if `bucketName` is empty — omit the block entirely
- ALB `add_listener()` auto-creates `0.0.0.0/0` ingress by default — always use `open=False`
- Removing cross-stack exports: deploy the importing stack first (remove imports), then the exporting stack (remove exports)
- CloudFormation drift on deleted resources: remove from template → deploy → add back → deploy (2-step)

### OpenClaw
- Requires Node >= 22.12.0 — Dockerfile uses `node:22-slim`
- Correct start command: `openclaw gateway run --port 18789 --bind lan --verbose` (not `openclaw start`)
- Config requires `gateway.mode: "local"` or `--allow-unconfigured` flag
- Auth token key: `gateway.auth.token` (not `gateway.token`)
- WebSocket auth protocol: `type: "req"` / `method: "connect"` with `client.id: "openclaw-control-ui"`, protocol version 3, and `auth: { token }` — NOT HMAC challenge-response
- `controlUi.allowInsecureAuth: true` is required when ALB→Fargate is HTTP (gateway checks X-Forwarded-Proto)
- `controlUi.allowedOrigins` must include the CloudFront domain for Web UI access
- Channel config is object-keyed: Telegram uses `botToken`, Discord uses `token`, Slack uses `botToken`
- Telegram `dmPolicy: "open"` requires `allowFrom: ["*"]` — validation error otherwise
- WhatsApp requires interactive session auth (QR code), cannot be configured via secret token
- Gateway is WebSocket-only on port 18789 — HTTP health checks must target the proxy on port 18790
- Streaming: agent events with `stream: "assistant"` and `data.delta` for text deltas; `chat` events with `state: "final"` for completion

### Node.js 22 + VPC IPv6 Issue
- **Critical**: Node.js 22's Happy Eyeballs (`autoSelectFamily`) fails in VPCs without IPv6 support
- Symptoms: `ETIMEDOUT` on IPv4 + `ENETUNREACH` on IPv6 for external APIs (Telegram, Discord, etc.)
- `curl` works but Node.js `fetch`/`https.get` fails — because `autoSelectFamily` tries both address families
- Fix: `bridge/force-ipv4.js` patches `dns.lookup()` to force `family: 4`, loaded via `NODE_OPTIONS="-r /app/force-ipv4.js"`
- Also set `--dns-result-order=ipv4first --no-network-family-autoselection` in NODE_OPTIONS
- `/proc/sys/net/ipv6/conf/all/disable_ipv6` is not writable in Fargate (read-only `/proc/sys`)

### ECS / Fargate
- EC2 instance requires `sudo docker` (ec2-user not in docker group)
- Push image to ECR before deploying stack — otherwise ECS tasks fail with `CannotPullContainerError`
- ALB auto-creates SG egress for target port but NOT for health check port — add explicitly
- Force new deployment after image push: `aws ecs update-service --force-new-deployment`
- ROLLBACK_FAILED stacks: delete with `--retain-resources <logicalId>` after they transition to DELETE_FAILED
- OpenClaw takes ~4 minutes from container start to gateway listening (plugin init phase)
- Channel token validation in entrypoint.sh skips Discord/Slack with placeholder tokens to prevent retry loops

### Security
- ALB listeners must use `open=False` to prevent CDK from auto-creating `0.0.0.0/0` ingress rules
- CloudFront Function validates gateway token by exact value (not just presence) — token read from Secrets Manager at `cdk synth` time via boto3
- Public ALB SG restricted to CloudFront origin-facing IPs via managed prefix list `pl-b8a742d1`

### Cognito Identity
- Cognito User Pool has self-signup disabled — all users are auto-provisioned by the proxy via `AdminCreateUser`
- Passwords are HMAC-derived: `HMAC-SHA256(secret, actorId)` truncated to 32 chars — deterministic, never stored
- The HMAC secret is in Secrets Manager (`openclaw/cognito-password-secret`), fetched at container startup
- Cognito usernames are channel-prefixed (e.g. `telegram:6087229962`) — colons are allowed in Cognito usernames
- JWT tokens are cached per user with 60s early refresh to avoid expiry during requests
- Runtime JWT authorizer is configured but **not enforced** without CfnGateway — direct SDK invocation uses SigV4
- `AdminInitiateAuth` requires `ADMIN_USER_PASSWORD_AUTH` enabled on the app client
