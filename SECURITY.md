# Security

This document describes the security architecture and controls implemented in OpenClaw on AgentCore Runtime.

## Overview

This solution applies defense-in-depth across the network, application, identity, and data layers. The architecture assumes a zero-trust model where each component has minimal permissions and all communication is authenticated and encrypted.

## Network Security

### VPC Isolation

AgentCore containers run in **private VPC subnets** with no direct internet exposure. All AWS service access goes through VPC endpoints:

**Interface Endpoints (7):**
- Bedrock Runtime
- SSM (Systems Manager)
- ECR API
- ECR Docker
- Secrets Manager
- CloudWatch Logs
- CloudWatch Monitoring

**Gateway Endpoint (1):**
- S3

The **only public entry point** is the API Gateway HTTP API, which handles webhook ingestion from Telegram and Slack.

### API Surface Minimization

The API Gateway exposes only three explicit routes:

| Route | Method | Purpose |
|---|---|---|
| `/webhook/telegram` | POST | Telegram webhook ingestion |
| `/webhook/slack` | POST | Slack Events API ingestion |
| `/health` | GET | Health check endpoint |

All other paths return 404 from API Gateway itself **without invoking the Lambda**. This prevents path enumeration and reduces attack surface.

### Rate Limiting

API Gateway applies rate limiting to prevent DDoS attacks:
- **Burst**: 50 requests
- **Sustained**: 100 requests/second

## Authentication & Authorization

### Webhook Authentication

Every incoming webhook request is **cryptographically validated** before processing:

**Telegram**:
- Validates the `X-Telegram-Bot-Api-Secret-Token` header
- Secret is registered with Telegram via the `secret_token` parameter on `setWebhook`
- Requests without valid token are rejected with 401

**Slack**:
- Validates the `X-Slack-Signature` HMAC-SHA256 header
- Uses the Slack app's signing secret for verification
- Includes 5-minute timestamp check to prevent replay attacks
- Requests with invalid signature or expired timestamp are rejected

**Fail-closed**: Requests are rejected if secrets are not configured in Secrets Manager.

### Per-User Isolation

Each user runs in their own **AgentCore microVM** with a dedicated S3 namespace:

- User A's files: `s3://bucket/telegram_123456/...`
- User B's files: `s3://bucket/slack_789012/...`

There is **no shared state** between users. Namespace derivation is system-controlled (from the channel identity) and cannot be influenced by user input.

### STS Session-Scoped Credentials

Beyond microVM isolation, S3 access is restricted at the **IAM level** using STS session policies:

1. On container init, the contract server calls `STS:AssumeRole` on its own execution role with an inline session policy
2. The execution role trust policy requires `sts:RoleSessionName` matching the `scoped-*` prefix, preventing unconditioned re-assumption
3. The session policy restricts S3 to `{bucket}/{namespace}/*` — the user can only access their own prefix
4. The session policy also restricts DynamoDB access to the user's own records via `dynamodb:LeadingKeys` condition (USER#{actorId} and CHANNEL#{actorId} prefixes only)
5. OpenClaw is spawned with these scoped credentials via `credential_process`. Container-level credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_CONTAINER_CREDENTIALS_*`) are stripped from its environment
6. Credentials are refreshed every 45 minutes (STS max session duration is 1 hour)
7. The trusted proxy process retains full execution role credentials for Bedrock, Cognito, and S3 image access (with application-level namespace enforcement)
8. If STS credential creation fails, OpenClaw starts with **zero AWS access** (all credential environment variables stripped) rather than falling back to full execution role credentials. Tools will fail gracefully but no cross-user data access is possible

This provides defense-in-depth: even if OpenClaw's bash tool or a skill attempts to access another user's S3 prefix, the IAM session policy denies the request. The session policy also scopes DynamoDB access to the identity table and EventBridge access to the cron schedule group.

### Identity Management

**Cognito User Pool** provides per-user identity:
- Users are auto-provisioned by the proxy via `AdminCreateUser`
- Passwords are HMAC-derived: `HMAC-SHA256(secret, actorId).slice(0, 32)`
- Passwords are deterministic and never stored
- JWT tokens are cached per user with 60-second early refresh

**AgentCore WorkloadIdentity** integrates with Cognito OIDC for JWT-based authentication between the container and AWS services.

### Least-Privilege IAM

Each component has tightly scoped permissions:

| Component | Permissions |
|---|---|
| Router Lambda | Invoke specific AgentCore Runtime only (not `Resource: *`) |
| Router Lambda | Cognito operations scoped to specific user pool |
| Router Lambda | Secrets Manager access limited to `openclaw/*` prefix |
| AgentCore Container | S3 access scoped to user's namespace prefix via STS session policy |
| AgentCore Container | Bedrock invoke scoped to specific model/inference profile |
| AgentCore Container | Secrets Manager: system secrets (gateway-token, cognito-password-secret) via execution role; per-user API keys (`openclaw/user/{namespace}/*`) via STS-scoped credentials |
| AgentCore Container | CloudWatch PutMetricData restricted to OpenClaw/* namespaces only |
| AgentCore Container | CloudWatch Logs restricted to /openclaw/* log group prefix |
| AgentCore Container | DynamoDB scoped to user's own records via STS session policy |

## Data Protection

### Encryption at Rest

| Service | Encryption |
|---|---|
| S3 | KMS with customer-managed key (CMK) |
| DynamoDB | KMS with customer-managed key (CMK) |
| SNS Topics | KMS with customer-managed key (CMK) |
| Secrets Manager | Customer-managed KMS key |
| CloudWatch Logs | AWS-managed keys |
| CloudTrail S3 | KMS with customer-managed key (CMK) |

### Encryption in Transit

- **TLS** for all AWS API calls
- **HTTPS** for API Gateway (TLS 1.2+)
- **HTTPS** for Telegram/Slack webhook delivery
- **HTTPS** for outbound API calls (Telegram sendMessage, Slack chat.postMessage)

### Secret Management

All sensitive values are stored in **AWS Secrets Manager** encrypted with a customer-managed KMS key:

| Secret | Purpose |
|---|---|
| `openclaw/channels/telegram` | Telegram bot token |
| `openclaw/channels/slack` | Slack bot token + signing secret (JSON) |
| `openclaw/webhook-secret` | Webhook validation token |
| `openclaw/gateway-token` | OpenClaw gateway authentication |
| `openclaw/cognito-secret` | HMAC secret for password derivation |

Secrets are:
- Fetched at runtime, cached with 15-minute TTL, and held in process memory only
- Never written to environment variables, config files, or logs
- Rotated via Secrets Manager (manual rotation supported)

## Container Security

### Hardening Measures

- Request body size limited to **1MB** to prevent memory exhaustion
- Internal error details and stack traces are **never exposed** in API responses
- Container runs on AgentCore's **Firecracker microVM** with hardware-level isolation
- Each user session runs in a **separate microVM** (not shared containers)
- OpenClaw tool deny list blocks the **read** tool to prevent credential access via `/proc` and local file reads. The `exec` tool is allowed — skills like `clawhub-manage` use it to run scripts. STS session-scoped credentials limit the blast radius of any shell commands to the user's own S3 namespace
- Bedrock proxy binds to **127.0.0.1** (loopback only), preventing network access from other containers
- Security group egress restricted to **TCP 443 only** (HTTPS)
- API Gateway **access logging** enabled for forensic analysis
- S3 bucket **versioning** enabled for accidental deletion recovery
- Secrets Manager values cached with **15-minute TTL** (not indefinitely)

### Image Security

- Base image: `node:22-slim` (minimal attack surface)
- Platform: `linux/arm64` (Graviton, reduced vulnerability exposure)
- Dependencies: Only required npm packages installed
- No secrets baked into image

## Audit & Compliance

### CloudTrail

Full audit trail of API activity across all AWS services, including:
- Bedrock model invocations
- Secrets Manager access
- S3 object operations
- DynamoDB operations
- Lambda invocations

### Observability

CloudWatch dashboards and alarms monitor:
- Lambda errors, latency, and throttling
- API Gateway 4xx/5xx error rates
- Bedrock invocation metrics
- Token usage per user (custom metrics)
- Budget alarms for cost control

### Automated Compliance

Every `cdk synth` runs [cdk-nag](https://github.com/cdklabs/cdk-nag) AwsSolutions checks against the entire infrastructure:

```bash
cdk synth  # Runs security checks automatically
```

This catches common misconfigurations before deployment:
- S3 bucket public access
- Unencrypted resources
- Overly permissive IAM policies
- Missing logging configurations

## Reporting Security Issues

See [CONTRIBUTING.md](CONTRIBUTING.md#security-issue-notifications) for information on reporting security vulnerabilities.
