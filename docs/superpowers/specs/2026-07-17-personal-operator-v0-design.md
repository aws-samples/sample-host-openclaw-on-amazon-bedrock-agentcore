# Personal Operator v0 Design

## Product

Personal Operator is a consumer personal AI computer reached through one shared
Telegram bot. Each user has a persistent logical workspace backed by isolated,
replaceable compute. In v0 the assistant can create or retain scoped workspace
files; model-callable URL retrieval, public search, scheduling, and code
execution remain deferred. Gmail is connected through a
separate trusted plane. Sensitive external effects are proposed first and may
be executed only by that trusted capability gateway after exact user approval.

The first governed application is Gmail. External pilots receive read-only follow-up discovery, source-backed cards, draft generation, edits, and preparation. Only the founder account receives incremental `gmail.send` scope for controlled, allowlisted tests with approval, idempotency, reconciliation, and receipts.

## Architecture

Telegram messages enter API Gateway, are authenticated and deduplicated by the Router Lambda, and are serialized per user through SQS FIFO. Deterministic product commands and governed app workflows remain in the trusted control plane. General requests are sent through a `RuntimeDriver` to an isolated Bedrock AgentCore session running OpenClaw.

The runtime is ephemeral. AgentCore Session Storage and a KMS-encrypted, versioned S3 namespace make it appear persistent. A user lease prevents concurrent writable sessions. OpenClaw receives only user-scoped workspace credentials and curated tools; it never receives Telegram, Google, database, approval-signing, or cross-user credentials.

The control plane stores identity, connections, workflow state, approvals, receipts, audit events, delivery outbox entries, and deletion tombstones in DynamoDB. Google credentials are envelope-encrypted with KMS and are accessible only to trusted provider adapters. A CloudFront-hosted web application handles connection, approval, workspace export, and deletion.

## Runtime boundary

- AWS region: `eu-west-1`.
- Bedrock model: `eu.anthropic.claude-sonnet-4-6`.
- OpenClaw: exactly `2026.7.2`, built on Node.js `24.15` or newer and pinned by immutable ECR digest for releases.
- One backend-generated AgentCore session identifier per user; clients never choose session IDs.
- Default idle timeout: 1,800 seconds. Maximum lifetime: 28,800 seconds.
- Workspace restore on startup; save after successful state-changing turns, periodically, and on shutdown.
- Runtime tools: exactly four scoped workspace file operations. The upstream
  `session_status` tool is denied because it can persist model overrides. The
  visible model catalog is restricted to the single loopback AgentCore route.
  URL retrieval, search, scheduling, and code execution are deferred. A future
  URL reader must authorize exact user-selected targets outside the model tool
  loop so workspace contents cannot be sent to model-selected destinations.
- No arbitrary ClawHub installation, user-supplied API keys, arbitrary MCP/plugin installation, or provider credentials inside the runtime.

## Trusted application boundary

Provider reads and writes live outside OpenClaw. The runtime or Telegram UI may create a proposed action, but only the capability gateway can execute it. Grants bind user, capability, resource, payload hash, expiry, and approval. Provider timeouts become `UNCERTAIN` and are reconciled; they are never blindly retried.

Gmail pilot scanning is bounded to at most 50 sent threads from 3–30 days ago. Every opportunity retains its Gmail thread/message IDs, excerpt, reason, and deep link. Structured model output is rejected if it references an ID not present in the supplied source set. At most three cards are shown.

## Data lifecycle

- Raw Gmail bodies are transient and never logged.
- Derived excerpts and drafts expire after 14 days.
- Audit and effect receipts expire after 90 days.
- Inactive pilot workspaces expire after 30 days.
- Deletion first persists an authority fence. Gmail and Telegram strongly
  recheck it at the last application-controlled point before provider
  dispatch; a network request that already crossed that point cannot be
  recalled. Local authority and active data are removed asynchronously in two
  purge passes; v0 makes no 24-hour completion claim.
- A deletion tombstone prevents stale runtime snapshots from restoring deleted state.
- Export contains user-authored files, saved memory, schedules, and receipts in documented JSON/ZIP form.

## v0 acceptance

- Three synthetic users pass cross-tenant canary tests with zero leakage.
- Telegram acknowledges a cold-start request within 15 seconds.
- Runtime replacement preserves files, memory, and schedules.
- At least 7 of 10 moderated Gmail users find one source-backed card useful within five minutes.
- External pilots never receive Gmail write scope.
- At least 9 of 10 controlled founder sends confirm correctly, with zero duplicates and zero unauthorized effects.
- Every confirmed effect has a matching exact-payload approval and receipt.

## Explicitly deferred

Calendar, Microsoft, payments, Estonia-specific integrations, persistent authenticated browser profiles, arbitrary skills/MCP servers, web terminal, billing, WhatsApp, and native applications are post-v0 work. The architecture remains broad; Gmail is the first governed capability, not the product boundary.
