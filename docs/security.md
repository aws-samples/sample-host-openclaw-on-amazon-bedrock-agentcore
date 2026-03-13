# OpenClaw on AgentCore — Security Architecture

## 1. Executive Summary

OpenClaw on AgentCore implements a **security-first, defense-in-depth architecture** that leverages AWS managed services to provide enterprise-grade protection for a multi-user AI messaging bot. The design philosophy follows three core principles:

- **Per-user microVM isolation** — Each user gets a dedicated AgentCore Runtime container (hardware-level Firecracker microVM) with no shared OS kernel between users
- **Zero-trust credential scoping** — STS session policies restrict each user's AWS permissions to their own S3 namespace, DynamoDB records, and EventBridge schedules
- **Defense-in-depth** — 10+ security layers from network to application level, with no single point of failure

This document catalogs every security mechanism across 7 CDK stacks, the bridge container, Router Lambda, and operational tooling.

---

## 2. Threat Model: Cloud-Native vs Local/VPS

Running an AI agent with tool access (bash, web fetch, file operations) on a local machine or VPS exposes significant risks. The table below compares threats and their mitigations on AgentCore versus a traditional deployment.

| Threat | Local PC / VPS | AgentCore on AWS |
|---|---|---|
| **Multi-tenant data leakage** | Shared filesystem; one user can read another's data | Per-user Firecracker microVMs + STS session-scoped S3 credentials restrict access to `{namespace}/*` |
| **Credential theft** | Plaintext `.env` files, shell history | Secrets Manager (KMS-encrypted, audited via CloudTrail); credentials never written to disk or env vars |
| **Network exposure** | Open ports, direct internet exposure | VPC private subnets, no public IPs on containers, 7+ VPC endpoints keep traffic on AWS backbone |
| **Abuse / runaway costs** | No budget controls | Daily token budget (1M tokens), daily cost budget ($5 USD), anomaly detection, CloudWatch alarms |
| **Audit trail** | None or manual log review | CloudTrail (most accounts already have one; optional dedicated trail available), CloudWatch access logs, Bedrock invocation logging |
| **Lateral movement** | Full host access, all env vars | STS scoped credentials + tool deny list + credential env var blocklist; zero-access fallback if STS fails |
| **OS / container CVEs** | Manual patching | ECR image scanning on push, managed runtime, multi-stage Docker build (no build tools in runtime) |
| **DDoS / abuse** | Direct exposure, no rate limits | API Gateway throttling (burst 50, sustained 100 req/s), webhook signature validation |
| **Secret rotation** | Manual, often forgotten | Secrets Manager with 15-min cache TTL, STS credential refresh every 45 min |
| **User access control** | None or basic auth | DynamoDB allowlist (registration_open=false), Cognito admin-provisioned identities |
| **DNS rebinding / SSRF** | No protection | IP blocklists (loopback, private, link-local, IMDS), DNS resolution validation, redirect chain validation |
| **Compliance evidence** | None | cdk-nag AwsSolutions checks enforced at deploy time, CloudTrail file validation |

---

## 3. Defense-in-Depth Layers

### 3.1 Network Security

All AgentCore containers run in VPC private subnets with no public IP addresses. AWS API traffic stays on the AWS backbone via VPC endpoints.

| Control | Implementation | CDK Stack |
|---|---|---|
| VPC with private subnets | `10.0.0.0/16`, 2 AZs, `PRIVATE_WITH_EGRESS` subnets | `VpcStack` |
| NAT Gateway | Single NAT for controlled outbound access | `VpcStack` |
| 7 Interface VPC endpoints | Bedrock Runtime, SSM, ECR (API + Docker), Secrets Manager, CloudWatch Logs, CloudWatch Monitoring | `VpcStack` |
| 1 Gateway VPC endpoint | S3 (free, no security group needed) | `VpcStack` |
| VPC endpoint security group | HTTPS-only ingress from VPC CIDR (`10.0.0.0/16`), no outbound | `VpcStack` |
| Container security group | Egress: TCP 443 only (HTTPS). Ingress: TCP 443 from VPC CIDR | `AgentCoreStack` |
| VPC flow logs | All traffic logged to CloudWatch (configurable retention) | `VpcStack` |

**Why it matters**: Containers cannot be reached directly from the internet. All AWS service calls transit VPC endpoints (never the public internet). The only internet-facing component is API Gateway, which has its own security controls.

### 3.2 API Gateway & Webhook Security

The Router API Gateway HTTP API uses explicit routes — only three paths are exposed. All other paths return 404 from API Gateway without invoking Lambda.

| Control | Implementation |
|---|---|
| Explicit routes only | `POST /webhook/telegram`, `POST /webhook/slack`, `GET /health` — no catch-all integration |
| Rate limiting | Burst: 50, sustained: 100 req/s (configured on default stage) |
| Detailed metrics | Enabled on default stage for monitoring request patterns |
| Access logging | JSON-format logs to `/openclaw/api-access` CloudWatch log group |
| Telegram validation | `X-Telegram-Bot-Api-Secret-Token` header checked via `hmac.compare_digest()` (constant-time comparison). Fail-closed: rejects if no secret configured |
| Slack validation | `X-Slack-Signature` HMAC-SHA256 with `v0:{timestamp}:{body}` base string. 5-minute replay window rejects stale requests. Fail-closed: rejects if no signing secret configured |
| Async dispatch | Router Lambda self-invokes with `InvocationType=Event`, returns 200 immediately to webhook callers |

**Key detail**: Both Telegram and Slack webhook validators use `hmac.compare_digest()` for constant-time comparison, preventing timing side-channel attacks.

### 3.3 Identity & Access Control

| Control | Implementation |
|---|---|
| User allowlist | `registration_open=false` by default; new users require an `ALLOW#` record in DynamoDB |
| DynamoDB identity table | `CHANNEL#`, `USER#`, `SESSION`, `BIND#`, `ALLOW#`, `CRON#` record types with composite key schema |
| Cognito User Pool | Self-signup disabled (`self_sign_up_enabled=False`); users auto-provisioned by proxy via `AdminCreateUser` |
| HMAC-derived passwords | `HMAC-SHA256(secret, actorId).slice(0, 32)` — deterministic, never stored or transmitted to end users |
| Account recovery disabled | `AccountRecovery.NONE` — no password reset flows (passwords are derived, not chosen) |
| Cross-channel binding | Time-limited bind codes (10-min TTL in DynamoDB, one-time use). Binding bypasses allowlist since it links to an already-approved user |
| JWT token caching | Per-user JWT cached with 60-second early refresh window |
| Slack retry deduplication | `x-slack-retry-num` header detected; retries ignored to prevent duplicate processing |

### 3.4 Per-User Isolation (Zero Trust)

This is the most critical security layer. Even if the AI agent is instructed to access another user's data, STS session policies and the credential env var blocklist make it impossible.

#### AgentCore MicroVM Isolation

Each user session runs in a dedicated Firecracker microVM managed by AgentCore Runtime. There is no shared OS kernel between users — this is hardware-level isolation, not just container namespacing.

#### STS Session-Scoped Credentials

On container init, the contract server calls `STS:AssumeRole` on the execution role with a **session policy** that restricts:

| Resource | Scope |
|---|---|
| S3 objects | `arn:aws:s3:::{bucket}/{namespace}/*` only |
| S3 list | Prefix condition: `{namespace}/*` and `{namespace}` |
| Secrets Manager | `openclaw/user/{namespace}/*` — per-user API key storage, max 10 secrets |
| DynamoDB | `ForAllValues:StringLike` on `dynamodb:LeadingKeys`: `USER#{actorId}`, `CHANNEL#{actorId}`, and `USER#{internalUserId}` (for CRON# and SESSION records stored under the internal user ID) |
| EventBridge | Schedule group: `openclaw-cron/*` |
| IAM PassRole | Only the EventBridge scheduler role |
| KMS | `kms:Decrypt`, `kms:GenerateDataKey`, `kms:GenerateDataKeyWithoutPlaintext` on project CMK only (when set) |

Implementation: `bridge/scoped-credentials.js` — `buildSessionPolicy()` constructs the JSON policy, `createScopedCredentials()` calls STS.

#### Credential Environment Blocklist

Seven AWS credential environment variables are explicitly stripped from the OpenClaw child process:

```
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
AWS_SESSION_TOKEN
AWS_CONTAINER_CREDENTIALS_RELATIVE_URI
AWS_CONTAINER_CREDENTIALS_FULL_URI
AWS_WEB_IDENTITY_TOKEN_FILE
AWS_ROLE_ARN
```

OpenClaw receives credentials only via `AWS_CONFIG_FILE` pointing to a `credential_process` that reads the scoped credential file.

#### Trust Policy Condition

The execution role's trust policy requires `sts:RoleSessionName` matching `scoped-*` prefix for self-assumption, preventing unconditioned re-assumption of the full role.

#### Credential File Atomicity

Credential files are written using write-to-tmp-then-rename (`fs.writeFileSync` to `.tmp`, then `fs.renameSync`) to prevent `credential_process` from reading partially-written files during refresh.

#### Zero-Access Fallback

If `EXECUTION_ROLE_ARN` is not set or STS `AssumeRole` fails, OpenClaw starts with zero AWS access — all credential env vars are stripped, no `credential_process` is configured. Tools fail gracefully but no cross-user data access is possible.

#### Credential Refresh

A 45-minute interval timer re-assumes the role and updates credential files (STS self-assume max duration is 1 hour for role chaining).

#### Proxy Trust Boundary

The proxy process (`agentcore-proxy.js`) is trusted code and retains full execution role credentials for Bedrock, Cognito, and S3 image access. Application-level namespace enforcement (prefix validation, path traversal checks) provides the security boundary here.

### 3.5 Encryption

| Scope | Mechanism | Details |
|---|---|---|
| **At rest** | KMS CMK with auto-rotation | Applied to: Secrets Manager, DynamoDB identity table, S3 user files bucket, SNS alarm topic. Optional CloudTrail bucket uses SSE-S3 (AES-256). Token usage table uses default DynamoDB encryption |
| **In transit** | HTTPS enforced | VPC endpoints use HTTPS; S3 bucket `enforce_ssl=True`; API Gateway endpoints are HTTPS-only |
| **CloudTrail integrity** | File validation enabled | Cryptographic digest files allow detection of log tampering |
| **S3 versioning** | Enabled on user files + CloudTrail buckets | Protects against accidental deletion, enables recovery |
| **CMK removal policy** | `RemovalPolicy.RETAIN` | Prevents accidental key deletion during `cdk destroy` |

### 3.6 Secrets Management

Seven system secrets are stored in AWS Secrets Manager, all encrypted with the project KMS CMK:

| Secret | Purpose |
|---|---|
| `openclaw/gateway-token` | Auto-generated 64-char token for WebSocket auth |
| `openclaw/webhook-secret` | 64-char token for Telegram/Slack webhook validation |
| `openclaw/cognito-password-secret` | HMAC key for deriving Cognito user passwords |
| `openclaw/channels/telegram` | Telegram Bot API token |
| `openclaw/channels/slack` | Slack bot token + signing secret (JSON) |
| `openclaw/channels/discord` | Discord bot token (placeholder) |
| `openclaw/channels/whatsapp` | WhatsApp bot token (placeholder) |

#### Per-User API Key Storage (Secrets Manager)

Users frequently need to store third-party API keys for skills and integrations. Rather than relying on **insecure plaintext `.env` files** (no encryption, no audit trail, readable by any process), the `api-keys` skill stores user secrets in **AWS Secrets Manager**:

| Property | Detail |
|---|---|
| **Path** | `openclaw/user/{namespace}/{key_name}` |
| **Encryption** | KMS CMK (customer-managed, auto-rotating) |
| **Isolation** | STS session-scoped credentials — each user can only access `openclaw/user/{their_namespace}/*` |
| **Audit** | Every access logged in CloudTrail |
| **Limits** | Max 10 secrets per user; alphanumeric key names, max 64 chars |
| **Proactive** | Agent detects key patterns (`sk-...`, `ghp_...`, `AKIA...`, `xoxb-...`) in messages and offers to store securely |
| **Migration** | Built-in `migrate` tool moves keys from native file → Secrets Manager (or reverse) |
| **Fallback** | Native file backend (`.openclaw/user-api-keys.json`, S3-synced with KMS encryption) available for quick prototyping |

**Why Secrets Manager over `.env` files:**
- `.env` files are plaintext on disk — readable by any process, no access auditing, easily leaked via git commits or shell history
- Secrets Manager provides KMS encryption at rest, per-access CloudTrail logging, and IAM-scoped access control
- The `read` tool is denied in the tool deny list, but `.env` files could still be read via other means — Secrets Manager access is enforced at the IAM layer regardless of tool configuration

**Rotation and caching**:
- Secrets Manager values cached for 15 minutes in Lambda (rotated secrets reflected within 15 min)
- STS scoped credentials refresh every 45 minutes
- Cognito passwords are deterministic (HMAC-derived) — never stored or user-facing
- Gateway token and webhook secret fetched eagerly at container boot

### 3.7 Application-Level Security

#### SSRF Prevention

The lightweight agent's `web_fetch` tool implements multi-layer SSRF protection:

| Check | Details |
|---|---|
| IP blocklist (pre-connect) | Blocks loopback (`127.*`), private (`10.*`, `172.16-31.*`, `192.168.*`), link-local (`169.254.*` — covers IMDS), RFC 6598, IPv6 unique local/link-local, IPv4-mapped IPv6 addresses |
| Hostname blocklist | `localhost`, `metadata.google.internal`, `metadata.internal`, `instance-data` |
| DNS rebinding mitigation | `validateResolvedIps()` resolves hostname and checks resolved IPs against blocklist before connecting |
| Redirect validation | Each redirect target re-validated (URL safety + DNS resolution) with depth limit of 3 |
| Protocol restriction | Only `http:` and `https:` allowed |

#### Path Traversal Prevention

- **Workspace sync** (`workspace-sync.js`): `path.resolve()` + `startsWith(resolvedBase)` check on every restored file
- **S3 user files skill**: Filename sanitization regex, path component validation
- **Proxy image access**: S3 key validated against user's namespace prefix + `..` rejection

#### Input Sanitization

| Area | Sanitization |
|---|---|
| S3 filenames | Regex validation, namespace prefix enforcement |
| User namespaces | `VALID_NAMESPACE = /^[a-zA-Z][a-zA-Z0-9_-]{1,64}$/` |
| Web fetch HTML output | Script/style/noscript block removal, HTML entity decoding, tag stripping |
| Workspace content | `sanitizeWorkspaceContent()` escapes code fence characters (backticks, tildes) to prevent fence-break injection in system prompts |
| Search queries | Truncated to 500 characters |
| Image format validation | Checked against `VALID_BEDROCK_FORMATS` set (jpeg, png, gif, webp) |

#### Tool Security

| Control | Details |
|---|---|
| Tool deny list | `read`, `write`, `edit`, `apply_patch`, `browser`, `canvas`, `cron`, `gateway` blocked — prevents credential reads, local filesystem writes (use S3 skill instead), and admin operations. `exec` is deliberately NOT denied — skills like `clawhub-manage` need it; scoped STS credentials limit blast radius |
| Child process safety | `execFile` with array args (no shell interpolation) for all tool invocations |
| Tool environment filtering | `NODE_OPTIONS` stripped of `--inspect`, `--require`, `--import`, `-r` flags to prevent debug/injection in child processes |
| Resource limits | 1 MB `maxBuffer`, 512 KB web fetch body, 50 KB text output, 30s tool timeout, 10 iteration max |
| Cron abuse prevention | Minimum 5-minute schedule interval enforced by EventBridge cron skill |

### 3.8 Container Security

| Control | Implementation |
|---|---|
| ARM64 minimal image | `node:22-slim` — minimal attack surface |
| Multi-stage Docker build | Builder stage installs tools; runtime stage copies only needed artifacts (no git, pip, or build tools) |
| ECR image scanning on push | `image_scan_on_push=True` — CVE detection on every push |
| IPv4 DNS patch | `force-ipv4.js` patches `dns.lookup()` for VPC compatibility (Node.js 22 Happy Eyeballs IPv6 issue) |
| V8 compile cache | Pre-warmed at build time for faster cold starts (no runtime compilation) |
| No Docker-in-Docker | Sub-agent sandbox set to `off` — no Docker inside AgentCore microVMs (microVMs already provide isolation) |

### 3.9 Audit, Monitoring & Observability

#### CloudTrail (Optional — Off by Default)

Most AWS accounts already have an organization-level or account-level CloudTrail. Deploying a second trail adds cost (S3 storage for log delivery, CloudWatch log group) with no additional security benefit. For this reason, the project's dedicated CloudTrail is **disabled by default**.

To enable it, set `"enable_cloudtrail": true` in `cdk.json` and redeploy. This creates:

| Feature | Configuration |
|---|---|
| API call audit trail | Multi-region disabled (single-region deployment), global service events included |
| File validation | Enabled — cryptographic integrity checks detect log tampering |
| Storage | Dedicated S3 bucket (SSE-S3, versioned, SSL enforced, public access blocked) |
| CloudWatch delivery | Trail logs also sent to CloudWatch log group for real-time analysis |

#### CloudWatch Dashboards

| Dashboard | Widgets |
|---|---|
| **OpenClaw-Operations** | Bedrock invocations/errors/throttles, Bedrock latency (p99), AgentCore invocations/errors/latency, Router Lambda invocations/errors/throttles/duration |
| **OpenClaw-Token-Analytics** | Input vs output tokens, estimated cost (USD), invocation counts, single-value KPIs |

#### CloudWatch Alarms

| Alarm | Threshold | Action |
|---|---|---|
| Bedrock server errors | > 5 per 5 min, 3 eval periods | SNS notification |
| Bedrock latency (p99) | > 10,000 ms, 3 eval periods | SNS notification |
| Bedrock throttles | > 1 per 5 min, 3 eval periods | SNS notification |
| Router Lambda errors | > 5 per 5 min, 3 eval periods | SNS notification |
| Daily token budget | > 1,000,000 tokens per hour | SNS notification |
| Daily cost budget | > $5 USD per hour | SNS notification |

#### Anomaly Detection

CloudWatch anomaly detector on `TotalTokens` metric (OpenClaw/TokenUsage namespace) — learns normal usage patterns and alerts on deviations.

#### Bedrock Invocation Logging

All Bedrock model invocations logged to `/aws/bedrock/invocation-logs` (text data only; image data delivery disabled for privacy). A subscription filter routes logs to the token metrics Lambda for processing.

#### Namespace Restriction

Container `cloudwatch:PutMetricData` permission is conditioned on `cloudwatch:namespace` matching `OpenClaw/AgentCore` or `OpenClaw/TokenUsage` — prevents metric injection or alarm falsification.

### 3.10 Session Lifecycle & Cost Control

| Control | Configuration |
|---|---|
| Idle timeout | 30 min (`session_idle_timeout=1800`) — container returns `Healthy` (not `HealthyBusy`) to allow natural idle termination |
| Max session lifetime | 8 hours (`session_max_lifetime=28800`) — hard cap on session duration |
| DynamoDB TTL | Bind codes: 10 min; token records: 90 days; user files: 365 days |
| Point-in-time recovery | Enabled on identity table and token usage table |
| Pay-per-request billing | DynamoDB PAY_PER_REQUEST — no over-provisioned capacity |
| SIGTERM grace period | 10s for final workspace save before exit (AgentCore gives 15s total) |
| Workspace sync | Periodic saves every 5 min; `openclaw.json` excluded from sync (always programmatically generated) |

### 3.11 Bedrock Guardrails (Content Filtering)

Bedrock Guardrails add a content-level defense layer that evaluates both user inputs and model outputs. Deployed via CDK as `OpenClawGuardrails` stack (opt-in, default ON).

| Policy | Coverage |
|---|---|
| **Content filters** | HATE, INSULTS, SEXUAL, VIOLENCE, MISCONDUCT (HIGH strength), PROMPT_ATTACK (input only) |
| **Topic denial** | 6 denied topics: crypto scams, phishing, self-harm, weapons manufacturing, malware creation, identity fraud |
| **Word filters** | Managed profanity + 7 custom terms (credential patterns, internal identifiers) |
| **PII filters** | 10 entity types: EMAIL, PHONE, credit cards, AWS keys, PASSWORD, PIN, USERNAME |
| **Custom regex** | AWS access key (`AKIA[0-9A-Z]{16}`), AWS secret key (40-char base64), generic API key (`sk-...`) |

**How it works**: The proxy injects `guardrailConfig` into every Bedrock Converse/ConverseStream API call. Bedrock evaluates the guardrail server-side — blocked inputs get a rejection message, blocked outputs are replaced or anonymized. The IAM role has `bedrock:ApplyGuardrail` permission.

**Opt-out**: Set `"enable_guardrails": false` in `cdk.json`. This skips the entire `GuardrailsStack` — no guardrail resources, no guardrail charges, no content filtering.

> **Cost note**: Guardrails add ~$0.75 per 1,000 text units on top of model inference costs. See [AWS Bedrock Guardrails Pricing](https://aws.amazon.com/bedrock/pricing/#Guardrails). Disabling guardrails removes content-level protections but other security layers (STS scoping, tool deny list, SSRF protection) remain active.

---

## 4. AWS Cloud-Native Security Value

These are the security capabilities that AWS managed services provide — capabilities unavailable when running on a local PC or VPS.

| Service | Security Value |
|---|---|
| **AgentCore Runtime** | Per-user Firecracker microVM isolation; managed serverless containers; no shared OS kernel between users; automatic session lifecycle management |
| **KMS** | Customer-managed encryption keys with automatic annual rotation; envelope encryption; all key usage audited via CloudTrail |
| **Secrets Manager** | Centralized secret storage with KMS encryption; audit trail for every access; no secrets in code, env files, or container images |
| **VPC Endpoints** | AWS API traffic never traverses the public internet; reduces network attack surface; private DNS resolution |
| **CloudTrail** | Immutable API audit log; cryptographic file validation detects tampering; provides compliance evidence. Most accounts already have one — optional dedicated trail available via `enable_cloudtrail` |
| **CloudWatch** | Real-time operational monitoring; anomaly detection on token usage; budget alarms; two operational dashboards |
| **S3** | Versioned, KMS-encrypted storage with SSL enforcement, public access blocking, and lifecycle-managed expiration |
| **DynamoDB** | Encryption at rest (identity table: KMS CMK; token usage table: AWS-owned key), point-in-time recovery, TTL auto-expiry, IAM condition-based access (LeadingKeys) |
| **Cognito** | Managed identity provider with admin-only user provisioning; JWT tokens; no password storage in application code |
| **API Gateway** | Built-in rate limiting (burst + sustained), JSON access logging, explicit route control, TLS termination |
| **STS** | Session-scoped credentials with fine-grained IAM policies; time-limited (1 hour); auditable via CloudTrail |
| **ECR** | Private container registry with image scanning on push (CVE detection); IAM-controlled pull access |
| **EventBridge Scheduler** | IAM-controlled scheduling; PassRole conditions prevent privilege escalation; schedule group isolation |
| **Bedrock Guardrails** | Server-side content filtering on every Converse/ConverseStream call; content filters, topic denial, PII redaction, word filters, custom regex; KMS-encrypted guardrail configuration |
| **IAM + cdk-nag** | Least-privilege enforcement at deploy time; automated compliance checking across all 8 stacks |
| **SNS** | KMS-encrypted alarm topic; service-to-service communication for alarm delivery |

---

## 5. Compliance & cdk-nag

All 8 CDK stacks run cdk-nag `AwsSolutions` checks at `cdk synth` time. This catches security misconfigurations before any infrastructure is deployed. Key areas validated:

| Category | What cdk-nag Checks |
|---|---|
| **IAM** | No wildcard `*` actions/resources without justification; no AWS-managed policies where custom policies are feasible; least-privilege enforcement |
| **Encryption** | S3 buckets encrypted; DynamoDB tables encrypted; SNS topics use KMS; Secrets Manager secrets have rotation configured |
| **Network** | Security groups don't allow unrestricted ingress (`0.0.0.0/0`); VPC flow logs enabled |
| **API Gateway** | Access logging configured; authorization on all routes; throttling enabled |
| **Lambda** | Latest runtime versions; no overly permissive execution roles |
| **Cognito** | Password complexity; MFA enforcement; advanced security features |
| **S3** | Server access logging; versioning; SSL enforcement; public access blocking |
| **SNS** | SSL enforcement on topic policy |

Where a rule doesn't apply to this architecture (e.g., Cognito MFA for non-interactive service identities, or Telegram webhooks that can't use IAM auth), the suppression is documented inline in the stack code with a rationale string explaining why. All suppressions use `cdk_nag.NagPackSuppression` with an `id` and `reason` field for auditability

---

## 6. Potential Extensions (Not Yet Implemented)

Security enhancements that could be added for additional defense hardening:

| Extension | Purpose | Threat Addressed |
|---|---|---|
| **AWS WAF** on API Gateway | Block malicious IPs, geo-restrict, managed rule sets (OWASP Top 10) | DDoS, credential stuffing, injection attacks |
| **AWS DNS Firewall** (Route 53 Resolver) | Block DNS queries to known C&C domains, prevent data exfiltration via DNS tunneling | Reverse shell, C&C callbacks, DNS exfiltration |
| **AWS Network Firewall** | Deep packet inspection, IDS/IPS for VPC traffic | Lateral movement, protocol-level attacks |
| **AWS Shield Advanced** | DDoS protection with 24/7 response team, cost protection guarantee | Volumetric DDoS |
| **GuardDuty** | AI-driven threat detection for CloudTrail, VPC Flow Logs, DNS logs | Compromised credentials, crypto mining, reconnaissance |
| **Security Hub** | Centralized security findings, CIS benchmarks, automated remediation workflows | Compliance drift, misconfigurations |
| **Macie** | S3 data classification, PII detection in user files | Sensitive data in user-uploaded files |
| **AWS Config Rules** | Continuous compliance monitoring, configuration drift detection | Infrastructure changes that weaken security posture |
| **Automatic secret rotation** (Secrets Manager) | Rotate gateway and webhook tokens on schedule | Long-lived credential compromise |
| **VPC endpoint policies** | Restrict which S3 buckets and DynamoDB tables are reachable via endpoints | Endpoint abuse for cross-account data access |
| **S3 Object Lock** | WORM (write-once-read-many) for CloudTrail audit logs | Log tampering after the fact |
| **Container image signing** (ECR + AWS Signer) | Verify image provenance before deployment | Supply chain attacks |
| **Private CA** (ACM PCA) | mTLS between internal components | Man-in-the-middle within VPC |

---

## 7. Security Operations Quick Reference

### Rotating Secrets

```bash
# Rotate a channel bot token
aws secretsmanager update-secret \
  --secret-id openclaw/channels/telegram \
  --secret-string 'NEW_BOT_TOKEN' \
  --region $CDK_DEFAULT_REGION

# The Router Lambda will pick up the new value within 15 minutes (cache TTL).
# To force immediate refresh, redeploy the Lambda:
cdk deploy OpenClawRouter --require-approval never
```

### Managing the User Allowlist

```bash
# Add a user (after they message the bot and get their ID)
./scripts/manage-allowlist.sh add telegram:123456789

# Remove a user
./scripts/manage-allowlist.sh remove telegram:123456789

# List all allowed users
./scripts/manage-allowlist.sh list
```

### Reviewing CloudTrail Logs (When `enable_cloudtrail` Is Enabled)

```bash
# Find the CloudTrail S3 bucket
TRAIL_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name OpenClawSecurity \
  --query "Stacks[0].Outputs" \
  --output json --region $CDK_DEFAULT_REGION | jq -r '.[]? | select(.OutputKey | contains("CloudTrail")) | .OutputValue' 2>/dev/null)

# List recent log files
aws s3 ls s3://$TRAIL_BUCKET/AWSLogs/ --recursive --region $CDK_DEFAULT_REGION | tail -20

# Query via CloudWatch Logs Insights (faster for recent events)
aws logs start-query \
  --log-group-name "/aws/cloudtrail" \
  --start-time $(date -d '1 hour ago' +%s) \
  --end-time $(date +%s) \
  --query-string 'fields @timestamp, eventName, userIdentity.arn | filter eventSource = "secretsmanager.amazonaws.com" | sort @timestamp desc | limit 20' \
  --region $CDK_DEFAULT_REGION
```

### Responding to Budget Alarms

1. **Check which user is consuming tokens** — Query the Token Analytics dashboard or DynamoDB:
   ```bash
   aws dynamodb query \
     --table-name openclaw-token-usage \
     --index-name GSI3 \
     --key-condition-expression "GSI3PK = :pk" \
     --expression-attribute-values '{":pk": {"S": "DATE#2026-03-04"}}' \
     --scan-index-forward false --limit 5 \
     --region $CDK_DEFAULT_REGION
   ```

2. **Temporarily disable a user** — Remove from allowlist (existing sessions continue until idle timeout):
   ```bash
   ./scripts/manage-allowlist.sh remove telegram:123456789
   ```

3. **Force-terminate a session** — Sessions terminate naturally after idle timeout (30 min). No manual kill is needed in normal operation.

### Investigating CloudWatch Anomalies

1. **Check the anomaly detector**:
   ```bash
   aws cloudwatch describe-anomaly-detectors \
     --namespace OpenClaw/TokenUsage \
     --metric-name TotalTokens \
     --region $CDK_DEFAULT_REGION
   ```

2. **Review recent token metrics**:
   ```bash
   aws cloudwatch get-metric-statistics \
     --namespace OpenClaw/TokenUsage \
     --metric-name TotalTokens \
     --start-time $(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%S) \
     --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
     --period 3600 --statistics Sum \
     --region $CDK_DEFAULT_REGION
   ```

3. **Check the Operations dashboard** in the AWS Console:
   - CloudWatch > Dashboards > `OpenClaw-Operations`
   - CloudWatch > Dashboards > `OpenClaw-Token-Analytics`

### Reviewing Container Security

```bash
# Check ECR image scan results
aws ecr describe-image-scan-findings \
  --repository-name openclaw-bridge \
  --image-id imageTag=v$(python3 -c "import json; print(json.load(open('cdk.json'))['context']['image_version'])") \
  --region $CDK_DEFAULT_REGION

# Check for CRITICAL/HIGH vulnerabilities
aws ecr describe-image-scan-findings \
  --repository-name openclaw-bridge \
  --image-id imageTag=v$(python3 -c "import json; print(json.load(open('cdk.json'))['context']['image_version'])") \
  --query 'imageScanFindings.findingSeverityCounts' \
  --region $CDK_DEFAULT_REGION
```

---

## 8. Reporting Security Issues

See [CONTRIBUTING.md](../CONTRIBUTING.md#security-issue-notifications) for information on reporting security vulnerabilities.
