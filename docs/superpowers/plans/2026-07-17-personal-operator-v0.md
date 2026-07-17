# Personal Operator v0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the pinned experimental AWS OpenClaw sample into an invite-only consumer personal AI computer with isolated persistent runtime, Telegram UX, a read-only Gmail pilot workflow, and one founder-only approval-gated send path.

**Architecture:** Keep general agent work in per-user AgentCore microVM sessions and keep credentials and external effects in a separate trusted control plane. Serialize Telegram work per user, persist logical state in DynamoDB/S3, use typed provider/action contracts, and fail closed on authorization or ambiguous effects.

**Tech Stack:** Python 3.12, AWS CDK v2, Lambda, API Gateway, SQS FIFO, DynamoDB, KMS, S3, EventBridge, CloudWatch, Node.js 24, OpenClaw 2026.7.2, Bedrock Claude Sonnet 4.6 EU profile, React/Vite, Google OAuth/Gmail API, OpenAI Responses structured output.

## Global Constraints

- Work only in `/Users/konstantin.tuzikov/Documents/personal-operator`; do not modify Tasa Verify.
- Preserve upstream commit `e13e385ec44a3776e571ec48001904e9394cc20e` in the source ledger.
- Deploy only to `eu-west-1`; use model `eu.anthropic.claude-sonnet-4-6`.
- Use Node.js `24.15` or newer and OpenClaw exactly `2026.7.2`.
- Keep registration invite-only and pilot Gmail access read-only.
- Never place Gmail, Telegram, database, approval-signing, or cross-user credentials inside OpenClaw.
- Do not install arbitrary ClawHub skills, arbitrary MCP servers, user API keys, or unreviewed plugins.
- Raw Gmail bodies are transient and must not be persisted or logged.
- A provider timeout must become `UNCERTAIN`; never retry an external effect without reconciliation.
- All new behavior follows test-first red-green-refactor. Configuration and documentation changes require static contract tests when practical.
- Do not deploy, push, create paid resources, or send real email without explicit credentials and a fresh preflight showing the exact target.

---

### Task 1: Product foundation and reproducible baseline

**Files:**
- Create: `docs/UPSTREAM.md`
- Create: `docs/BASELINE.md`
- Create: `tests/test_product_configuration.py`
- Create: `scripts/test-local.sh`
- Modify: `cdk.json`
- Modify: `bridge/package.json`
- Modify: `bridge/Dockerfile`
- Modify: `README.md`

**Interfaces:**
- Produces a reproducible local test command and frozen product/runtime configuration consumed by every later task.

- [ ] Add failing static configuration tests asserting the region/model/runtime defaults, Node base image, OpenClaw pin, invite-only registration, disabled browser, empty imported runtime IDs, and absence of ClawHub installation in the image.
- [ ] Run `./.venv/bin/python -m pytest tests/test_product_configuration.py -v` and capture the expected failures against the imported sample.
- [ ] Change product configuration to `eu-west-1`, `eu.anthropic.claude-sonnet-4-6`, browser disabled, 30-day pilot workspace retention, empty runtime IDs, and a fresh image version.
- [ ] Upgrade the bridge image and package engine to Node 24.15+, pin OpenClaw 2026.7.2, and remove ClawHub CLI/community-skill installation from the image.
- [ ] Add `scripts/test-local.sh` that runs Python unit tests, Node tests deterministically with `AWS_REGION=eu-west-1`, syntax checks, and CDK contract checks without cloud credentials.
- [ ] Record the upstream commit/license, imported test failures, local toolchain, and commands in `docs/UPSTREAM.md` and `docs/BASELINE.md`; update the README product identity without claiming production readiness.
- [ ] Run focused tests green, then the local suite. Record any remaining imported failures that Task 2 intentionally removes.
- [ ] Commit as `chore(foundation): establish personal operator baseline`.

### Task 2: Curated and least-privilege OpenClaw runtime

**Files:**
- Create: `bridge/runtime-policy.js`
- Create: `bridge/runtime-policy.test.js`
- Modify: `bridge/agentcore-contract.js`
- Modify: `bridge/lightweight-agent.js`
- Modify: `bridge/scoped-credentials.js`
- Modify: `bridge/scoped-credentials.test.js`
- Modify: `bridge/Dockerfile`
- Delete: `bridge/skills/clawhub-manage/**`
- Delete: `bridge/skills/api-keys/**`

**Interfaces:**
- Produces: `buildRuntimePolicy()` and `buildSessionPolicy(namespace, bucketArn)` with only scoped workspace and approved scheduling access.

- [ ] Write failing tests proving arbitrary skill/API-key tools are absent, OpenClaw deny/allow rules are explicit, provider secrets are stripped, and session policies cannot read another namespace.
- [ ] Run the focused tests and verify failures are caused by the experimental full-tool configuration.
- [ ] Implement one frozen runtime policy shared by warm-up and full OpenClaw configuration; remove ClawHub/API-key scripts and all corresponding Secrets Manager/DynamoDB permissions.
- [ ] Restrict workspace keys to the server-derived namespace and retain zero-access fallback if scoped STS assumption fails.
- [ ] Regenerate runtime instructions so they describe only available curated capabilities and never encourage users to paste secrets.
- [ ] Run bridge tests, syntax checks, static credential scans, and source searches for deleted capabilities.
- [ ] Commit as `feat(runtime): enforce curated least-privilege tools`.

### Task 3: Runtime driver, leases, and durable workspace lifecycle

**Files:**
- Create: `lambda/router/runtime_driver.py`
- Create: `lambda/router/runtime_state.py`
- Create: `lambda/router/test_runtime_driver.py`
- Modify: `lambda/router/index.py`
- Modify: `bridge/workspace-sync.js`
- Modify: `bridge/workspace-sync.test.js`
- Modify: `stacks/router_stack.py`

**Interfaces:**
- Produces: `RuntimeDriver.ensure(user_id)`, `invoke(user_id, request, trace_id)`, `status(user_id)`, `snapshot(user_id)`, `stop(user_id)`, and `purge(user_id)`.
- Produces runtime states `COLD|STARTING|READY|BUSY|IDLE|UNHEALTHY|QUARANTINED|DELETING`.

- [ ] Write failing tests for server-generated session mapping, conditional per-user leases, stale lease takeover, no client-provided session IDs, post-turn snapshot signalling, and tombstoned-user refusal.
- [ ] Implement the runtime state repository and AgentCore adapter behind the interface without changing Telegram behavior.
- [ ] Make successful state-changing turns request an S3 flush while retaining periodic and shutdown saves.
- [ ] Add DynamoDB lease/session records and least-privilege router permissions.
- [ ] Run unit tests plus existing workspace/identity tests and CDK synthesis contract tests.
- [ ] Commit as `feat(runtime): add durable user runtime driver`.

### Task 4: Ordered Telegram product router and commands

**Files:**
- Create: `lambda/router/product_commands.py`
- Create: `lambda/router/message_queue.py`
- Create: `lambda/router/test_product_commands.py`
- Create: `lambda/worker/index.py`
- Create: `lambda/worker/test_worker.py`
- Modify: `lambda/router/index.py`
- Modify: `stacks/router_stack.py`

**Interfaces:**
- Consumes: `RuntimeDriver.invoke`.
- Produces deterministic commands `/start`, `/connect`, `/scan`, `/tasks`, `/workspace`, `/status`, and `/delete`.
- Produces FIFO messages `{userId, channel, updateId, traceId, kind, payload}` with `MessageGroupId=userId`.

- [ ] Write failing tests for webhook-secret rejection, update deduplication, immediate acknowledgement, command routing, per-user ordering, worker retry/dead-letter behavior, and free-form runtime routing.
- [ ] Replace recursive Lambda self-invocation with SQS FIFO enqueue and a separate worker Lambda.
- [ ] Keep Telegram formatting/delivery in the trusted worker and prohibit bot-token propagation to runtime payloads.
- [ ] Add queue, DLQ, IAM, alarms, explicit routes, throttling, and environment wiring in CDK.
- [ ] Run router/worker unit tests, one hundred replayed duplicate updates, and CDK contract tests.
- [ ] Commit as `feat(telegram): add ordered consumer command router`.

### Task 5: Read-only Gmail opportunity and draft workflow

**Files:**
- Create: `lambda/workflows/gmail/oauth.py`
- Create: `lambda/workflows/gmail/scanner.py`
- Create: `lambda/workflows/gmail/ranker.py`
- Create: `lambda/workflows/gmail/models.py`
- Create: `lambda/workflows/gmail/test_scanner.py`
- Create: `lambda/workflows/gmail/test_ranker.py`
- Create: `lambda/workflows/index.py`
- Modify: `stacks/security_stack.py`
- Modify: `stacks/router_stack.py`

**Interfaces:**
- Produces `Opportunity{id,userId,source,waitingSince,title,reason,confidence}` and `DraftRevision{actionId,revision,to,subject,body,payloadHash}`.
- Consumes only Google read-only OAuth tokens for pilot accounts.

- [ ] Write failing scanner tests covering the 3–30 day range, 50-thread cap, latest-human-outbound/no-reply rule, bulk/no-reply exclusion, transient raw bodies, and source deep links.
- [ ] Write failing ranker tests for at-most-three results, OpenAI structured output, `store:false`, source-ID membership validation, and safe failure on malformed or invented IDs.
- [ ] Implement OAuth PKCE/state binding and KMS envelope encryption behind provider-connection interfaces.
- [ ] Implement scanner and ranker with dependency-injected Gmail/OpenAI clients and log redaction.
- [ ] Persist only derived source excerpts, opportunities, and draft revisions with 14-day TTL.
- [ ] Render full Telegram cards with `Edit`, `Prepare`, `Skip`, and `Why`; pilots cannot reach a send transition.
- [ ] Run synthetic fixture, prompt-injection, logging, and scope-split tests.
- [ ] Commit as `feat(gmail): add source-backed read-only follow-ups`.

### Task 6: Capability gateway, exact approval, and effect receipts

**Files:**
- Create: `lambda/actions/models.py`
- Create: `lambda/actions/state_machine.py`
- Create: `lambda/actions/gmail_send.py`
- Create: `lambda/actions/reconcile.py`
- Create: `lambda/actions/test_state_machine.py`
- Create: `lambda/actions/test_gmail_send.py`
- Modify: `stacks/router_stack.py`
- Modify: `stacks/security_stack.py`

**Interfaces:**
- Produces action states `PREPARED|APPROVAL_PENDING|APPROVED|DISPATCHING|CONFIRMED|REJECTED|UNCERTAIN|EXPIRED|STALE|CANCELLED`.
- Produces `CapabilityGrant` bound to `userId`, capability, resource, `argsHash`, expiry, and approval ID.
- Produces `EffectReceipt{providerMessageId,providerThreadId,payloadHash,executedAt}`.

- [ ] Write failing tests for every legal/illegal state transition, canonical payload hashing, approval expiry/replay/tamper/wrong-user protection, founder-only scope, deterministic Message-ID, and idempotent dispatch.
- [ ] Implement exact-payload approval and conditional DynamoDB transitions.
- [ ] Implement plain-text, allowlisted founder Gmail sending with no CC/BCC/attachments.
- [ ] On provider timeout, persist `UNCERTAIN`; reconcile by deterministic Message-ID/provider history before any retry.
- [ ] Emit a receipt and waiting-for-reply tracker only after confirmed provider evidence.
- [ ] Run ten synthetic fault-injection sequences around the provider call and concurrent approval replay tests.
- [ ] Commit as `feat(actions): add approval-gated gmail receipts`.

### Task 7: Consumer web surface, export, retention, and deletion

**Files:**
- Create: `web/**`
- Create: `lambda/web/index.py`
- Create: `lambda/web/auth.py`
- Create: `lambda/web/retention.py`
- Create: `lambda/web/test_auth.py`
- Create: `lambda/web/test_retention.py`
- Create: `stacks/web_stack.py`
- Modify: `app.py`

**Interfaces:**
- Produces `/oauth/google/start`, `/oauth/google/callback`, `/approve/:token`, `/api/actions/:id/approve`, `/api/actions/:id/reject`, `/api/workspace`, `/api/export`, and `/api/delete`.

- [ ] Write failing tests for signed Telegram connect tickets, OAuth PKCE/state/nonce, opaque secure sessions, CSRF, approval GET-without-effect, matching-user POST, and one-time tokens.
- [ ] Build minimal React/Vite connect, approval, workspace, export, and delete pages with accessible loading/error states.
- [ ] Add CloudFront/S3 hosting and explicit API routes; do not expose the OpenClaw gateway.
- [ ] Implement TTL policy, token revocation, schedule cleanup, workspace purge, deletion tombstones, and stale-snapshot restore refusal.
- [ ] Implement JSON/ZIP export for user-authored files, memory, schedules, and receipts.
- [ ] Run auth, deletion, tombstone, retention, accessibility, and production build tests.
- [ ] Commit as `feat(web): add trusted consumer control surface`.

### Task 8: Integrated security, fault, and release verification

**Files:**
- Create: `tests/integration/**`
- Create: `tests/security/**`
- Create: `docs/OPERATIONS.md`
- Create: `docs/PRIVACY-BOUNDARY.md`
- Create: `docs/RELEASE-EVIDENCE.md`
- Modify: `scripts/test-local.sh`
- Modify: `README.md`

**Interfaces:**
- Produces a release evidence ledger and deterministic staging preflight; it does not deploy or send email by itself.

- [ ] Add cross-tenant Cartesian canary tests, 100x webhook replay/concurrency, provider fault injection, prompt-injection fixtures, credential-absence checks, retention/deletion tests, and a complete synthetic founder journey.
- [ ] Add static checks for no public gateway, no runtime provider secrets, user-prefix-only STS access, immutable image pin metadata, and forbidden capabilities.
- [ ] Generate an SBOM and dependency/license inventory without committing secrets or local environment data.
- [ ] Run the complete local suite, Python compilation, Node syntax/tests, web production build, CDK synth/contract checks, secret scan, and repository diff review.
- [ ] Document exact pass/fail evidence and any external blockers such as missing AWS, Telegram, Google, or OpenAI credentials.
- [ ] Prepare but do not execute the `eu-west-1` staging deployment preflight or any real Gmail send.
- [ ] Commit as `test(release): verify personal operator v0`.
