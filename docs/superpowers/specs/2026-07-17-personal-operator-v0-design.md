# Personal Operator v0 Design

## Product

Personal Operator is a consumer personal AI computer reached through one shared Telegram bot. Each user has a persistent logical workspace backed by isolated, replaceable compute. The assistant can research the public web, create and retain files, run restricted workspace tasks, schedule work, and connect to consumer applications. Sensitive external effects are proposed by the runtime but executed only by a separate trusted capability gateway after exact user approval.

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
- Runtime tools: public search/fetch, scoped workspace file operations, EventBridge schedules, and restricted workspace execution.
- No arbitrary ClawHub installation, user-supplied API keys, arbitrary MCP/plugin installation, or provider credentials inside the runtime.

## Trusted application boundary

Provider reads and writes live outside OpenClaw. The runtime or Telegram UI may create a proposed action, but only the capability gateway can execute it. Grants bind user, capability, resource, payload hash, expiry, and approval. Provider timeouts become `UNCERTAIN` and are reconciled; they are never blindly retried.

Gmail pilot scanning is bounded to at most 50 sent threads from 3–30 days ago. Every opportunity retains its Gmail thread/message IDs, excerpt, reason, and deep link. Structured model output is rejected if it references an ID not present in the supplied source set. At most three cards are shown.

## Data lifecycle

- Raw Gmail bodies are transient and never logged.
- Derived excerpts and drafts expire after 14 days.
- Audit and effect receipts expire after 90 days.
- Inactive pilot workspaces expire after 30 days.
- Deletion revokes provider access immediately and purges workspace, schedules, credentials, and derived records within 24 hours.
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
