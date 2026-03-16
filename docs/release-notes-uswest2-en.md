# Release Notes: OpenClaw us-west-2 Multi-Region Deployment + Feishu Channel

**Branch:** `deploy/starter-toolkit-hybrid`
**Date:** 2026-03-12
**Region:** us-west-2 (Oregon)

---

## 1. Multi-Region Deployment (Hybrid Architecture)

### Situation
OpenClaw was previously deployed only in us-east-1 as a single-region service, using a pure CDK deployment approach. This approach had several pain points:
- **Docker build difficulties**: AgentCore Runtime runs on ARM64, but developer machines are x86. Cross-compiling ARM64 container images locally was slow and error-prone.
- **AgentCore console not displaying metrics**: Suspected to be related to the pure-CDK CfnRuntime deployment method. The GenAI Observability Dashboard showed no Runtime metrics or traces.
- **Slow Runtime cold start**: Under the pure CDK deployment, Runtime cold start took approximately 60 seconds, resulting in poor user experience.
- **Multi-region expansion blocked**: Issues such as IAM role global naming conflicts and ECR permission mismatches prevented cross-region deployment.

### Task
Design and implement a new deployment strategy that resolves the Docker build, console metrics, cold start performance, and cross-region deployment issues, while extending the service to us-west-2.

### Action
- **Adopted a CDK + AgentCore Starter Toolkit hybrid deployment architecture**: CDK manages infrastructure (VPC, IAM, S3, Lambda, API Gateway, DynamoDB, and other resources across 7 stacks), while Starter Toolkit manages Runtime, Endpoint, ECR, and Docker image builds.
- **Improved Docker build workflow**: Starter Toolkit supports two modes -- `--local-build` (build locally with Docker and push) and the default CodeBuild mode (cloud-based ARM64 build with no local Docker requirement). It also supports rapid local development testing via `agentcore dev`.
- **Appended region suffix to IAM role names** (e.g., `openclaw-agentcore-execution-role-us-west-2`) to avoid global naming conflicts.
- **Updated CDK IAM policies to match Starter Toolkit's ECR naming convention** (`bedrock-agentcore-` prefix), resolving a permissions issue that had produced a misleading "initialization exceeded 120s" error.
- **Implemented a 3-phase deployment workflow**: Phase 1 -- CDK foundation stacks; Phase 2 -- Starter Toolkit Runtime; Phase 3 -- CDK dependent stacks.
- **Produced comprehensive operational documentation**: deployment guide, operations runbook, common commands reference, and lessons-learned log.

### Result
- All 7 CDK stacks plus the AgentCore Runtime were successfully deployed in us-west-2 and passed end-to-end validation.
- **AgentCore console metrics restored**: After switching to the Starter Toolkit deployment method, the GenAI Observability Dashboard correctly displayed all Runtime metrics and traces.
- **Runtime cold start reduced from approximately 60 seconds to approximately 1 second**: The change in deployment method drastically shortened startup time.
- First message response on cold start takes approximately 5 seconds (handled directly by the Lightweight Agent); OpenClaw reaches full readiness in approximately 10 seconds.
- The deployment strategy is replicable to other regions, with a standardized 3-phase workflow and supporting documentation now in place.

---

## 2. Feishu (Lark) Channel Integration

### Situation
OpenClaw already supported Telegram and Slack as messaging channels. The team primarily uses Feishu (Lark) for internal communication, so adding Feishu as a channel was necessary to improve accessibility for internal users. Feishu's technical stack differs significantly from Telegram and Slack: event payloads are AES-256-CBC encrypted, authentication uses OAuth tenant_access_token, and message formats and API structures are distinct.

### Task
Implement a complete Feishu channel integration, including:
- Webhook event reception, signature verification, and encrypted event decryption
- Message delivery (both direct messages and group chats)
- Image upload support
- User allowlist management
- One-step configuration script

### Action
- **Added a Feishu handler to the Router Lambda**: webhook signature verification (SHA-256), event decryption (AES-256-CBC), message parsing, group chat @mention filtering, and image download.
- **AES decryption via system OpenSSL**: Used Python ctypes to call `libcrypto.so` directly from the Lambda runtime environment -- zero third-party dependencies, cross-architecture compatible (avoiding the pycryptodome native binary compatibility issues between x86 and ARM64).
- **CDK additions**: API Gateway route for Feishu (`POST /webhook/feishu`), Secrets Manager entry for Feishu credentials, Cron Lambda Feishu message delivery support.
- **Interactive configuration script** (`setup-feishu.sh`): Guides users through Feishu developer console configuration, credential storage, and allowlist setup.

### Result
- Feishu channel passed end-to-end validation: text messages, group chat, and long-running tasks (with progress notifications) all function correctly.
- Event decryption performs well (ctypes/OpenSSL operates at native C speed with no cold start overhead).
- Added 27 Feishu unit tests covering signature verification, event parsing, message delivery, and related scenarios.
- Produced a new-channel integration checklist to guide future development of WhatsApp, Discord, LINE, and other channels.

---

## 3. Container Observability (CloudWatch Logging)

### Situation
AgentCore Runtime does not automatically route container stdout to CloudWatch, making it impossible to diagnose internal container issues such as proxy startup failures, OpenClaw crashes, or credential errors. During us-west-2 deployment debugging, the absence of container logs repeatedly prevented root cause identification.

### Task
Implement reliable container log output to CloudWatch without impacting startup speed or `/ping` health check response time.

### Action
- **Added a `cloudwatch-logger.js` module**: Hooks `console.log/warn/error`, buffers log events, and flushes them in batches to the CloudWatch `/openclaw/container` log group.
- **Initializes at startup** (non-blocking to `/ping`); **flushes on SIGTERM** (ensuring no log loss before shutdown).
- **Added Dockerfile dependency**: `@aws-sdk/client-cloudwatch-logs`.

### Result
- Container logs are now visible in real time, significantly improving troubleshooting efficiency -- from guesswork to log-based root cause analysis.
- Logs are organized into streams by `{namespace}-{timestamp}`, with each user isolated for easy tracing.

---

## 4. Per-User Credential Isolation (STS Session Policy)

### Situation
OpenClaw running inside the container has bash execution capability, which theoretically allows access to other users' S3 files or DynamoDB data via the AWS CLI. STS session-scoped credentials were needed to restrict each user to their own resources. The initial session policy implementation included detailed DynamoDB Condition blocks (`LeadingKeys`) and S3 prefix conditions, causing the policy to exceed the AWS STS **2048-byte packed size limit**.

### Task
Compress the session policy to fit within the 2048-byte limit while maintaining security isolation.

### Action
- **Streamlined the session policy**: S3 retains namespace-level Resource restrictions; DynamoDB, Scheduler, and Secrets Manager use `Resource: "*"` (relying on the execution role's own resource-level restrictions); all Condition blocks were removed.
- **Final policy size: 668 bytes** (33% utilization), well under the 2048-byte limit.

### Result
- Scoped credentials are created successfully, and OpenClaw operates correctly within the restricted environment.
- Each user's S3 access is strictly limited to `{namespace}/*`, with cross-user data isolation enforced.
- Documented the session policy size limit as a known constraint to prevent future issues.

---

## 5. Warm Pool -- Exploration and Decision

### Situation
Under the pure CDK deployment model, AgentCore Runtime cold start took approximately 60 seconds (VPC ENI creation + image pull + container startup), resulting in excessive wait times for the user's first message. A Warm Pool strategy was designed to address this: EventBridge would periodically trigger a Lambda to pre-create AgentCore sessions, allowing a user's first message to claim a pre-warmed session and skip the cold start entirely.

### Task
Implement and validate the Warm Pool strategy, then evaluate whether it remained necessary under the new deployment architecture.

### Action
- **Fully implemented the Warm Pool solution**:
  - `WarmPoolStack` (CDK): Lambda + EventBridge rule checking every minute, maintaining a pool of pre-warmed sessions in DynamoDB (default pool size = 1).
  - `claim_warm_session()` (Router Lambda): Atomically claims a pre-warmed session (DynamoDB conditional delete to prevent race conditions).
  - Supports a `WARM_POOL_ENABLED` environment variable toggle, disabled by default.
- **Deployed and tested**: The stack deployed successfully, the Lambda ran correctly, and pre-warmed sessions were created as expected.
- **Discovered a KMS permissions issue**: DynamoDB uses a customer-managed key (CMK) for encryption, requiring KMS permissions for the Warm Pool Lambda. The CDK code correctly configured the `cmk_arn` parameter, but IAM propagation delay caused the first few invocations to fail before self-recovering.

### Result
- The Warm Pool technical approach was validated and confirmed to be fully functional.
- **However, it was no longer needed**: After switching to the Starter Toolkit deployment method, Runtime cold start dropped from approximately 60 seconds to approximately 1 second. The Lightweight Agent responds to the first message within approximately 5 seconds (including Bedrock model invocation time), providing a sufficiently good user experience.
- **Decision: Remove Warm Pool** to eliminate unnecessary complexity (additional Lambda + EventBridge rule + DynamoDB records + KMS permission management).
- **Technical design documentation was preserved** so the solution can be quickly restored if needed in the future.

> **Key Insight**: The root cause of the 60-second cold start was the pure-CDK CfnRuntime deployment method, not the container startup itself. Switching the deployment method resolved the issue entirely, rendering Warm Pool a solution to a problem that no longer existed.

---

## Key Metrics

| Metric | Before (Pure CDK) | After (Hybrid Deployment) |
|--------|---------------------|----------------------------|
| Runtime cold start | ~60s | **~1s** |
| First message response | ~60s+ | **~5s** (Lightweight Agent) |
| OpenClaw full readiness | ~2-3 min | **~10s** |
| Console metrics display | Unavailable | Functioning normally |
| Docker build | Local cross-compilation (slow / error-prone) | CodeBuild cloud-based or local-build |
| Supported channels | Telegram + Slack | Telegram + Slack + **Feishu (new)** |
| Feishu unit tests | -- | 27 |
| Container logs | Not visible | CloudWatch real-time output |
| Commits | -- | 12 (including 1 merge) |
