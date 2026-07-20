# Personal Operator Runtime Hardening Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Every behavior change follows red-green-refactor and receives a separate review before the next task starts.

**Goal:** Replace the imported experimental OpenClaw runtime with a fail-closed, single-user runtime that exposes only four namespaced workspace tools, never receives product/provider authority, and preserves user workspace bytes and deletions across restarts.

**Architecture:** OpenClaw remains the conversational runtime, but it runs with the `minimal` tool profile and one repository-owned plugin loaded from an explicit path. The plugin derives the user's S3 namespace only from server-controlled environment, while the trusted Node contract owns initialization and credential setup. A session is immutably bound to one canonical user. Restore completes before OpenClaw starts, and synchronization uses a manifest so failed copies and user deletions cannot silently resurrect data.

**Tech Stack:** Node.js 24.15+, OpenClaw source commit `4bfaccafd62ac2ff2e70ca1decc40fb1297ab438`, OpenClaw plugin SDK, TypeBox, AWS STS/S3 SDK v3, Node test runner.

## Frozen Security Decisions

- OpenClaw's exact approved tools are `po_file_list`, `po_file_read`, `po_file_write`, and `po_file_delete`. The `minimal` profile's mutable `session_status` built-in is explicitly denied, and the visible model catalog contains only the loopback `agentcore/bedrock-agentcore` route with no fallback. URL retrieval and search are deferred. No model-callable network tool may be combined with credential-bearing workspace reads; a future reader must authorize exact user-selected targets outside the model tool loop.
- `exec`, `process`, generic filesystem tools, browser tools, cron, gateway controls, cross-session tools, subagents, arbitrary plugins, arbitrary MCP servers, ClawHub, and user API-key storage are forbidden.
- Linux/code execution is disabled in this credential-bearing runtime. A later credential-free sandbox may provide it through a typed capability boundary.
- The only loaded plugin is `personal-operator`, from `/app/plugins/personal-operator`; the plugin accepts no user ID or S3 namespace argument.
- Telegram, Gmail, Google OAuth, DynamoDB, approval-signing, action-execution, and cross-user credentials never enter the OpenClaw process. Telegram delivery leaves this runtime entirely.
- The OpenClaw child receives only a short-lived, user-prefix-scoped AWS session and non-authoritative model/runtime configuration. Failure to mint scoped credentials is fatal; there is no full-role fallback.
- Runtime region is exactly `eu-west-1`. Missing region may use the product default; any explicit different region fails initialization.
- Each AgentCore session binds once to `{internalUserId, namespace}` and rejects every later mismatched request before reading or mutating workspace state.
- Restore and session-storage mounting finish and are verified before OpenClaw configuration or startup. Failed restore or copy never deletes the only source copy.
- A persisted manifest records synchronized paths and hashes. Saving propagates deletions for paths previously owned by the manifest, while never deleting unknown objects.

---

### Task 1: Frozen tool surface and product-secret isolation

**Files:**
- Create: `bridge/runtime-policy.js`
- Create: `bridge/runtime-policy.test.js`
- Create: `bridge/plugins/personal-operator/openclaw.plugin.json`
- Create: `bridge/plugins/personal-operator/index.js`
- Create: `bridge/plugins/personal-operator/index.test.js`
- Modify: `bridge/agentcore-contract.js`
- Modify: `bridge/lightweight-agent.js`
- Modify: `bridge/lightweight-agent.test.js`
- Modify: `bridge/Dockerfile`
- Modify: `bridge/package.json`
- Modify: `bridge/package-lock.json`
- Modify: `bridge/CLAUDE.md`
- Modify: `tests/test_product_configuration.py`
- Delete: `bridge/skills/clawhub-manage/**`
- Delete: `bridge/skills/api-keys/**`
- Delete: `bridge/skills/eventbridge-cron/**`

**Interfaces:**
- `buildRuntimePolicy()` returns the frozen OpenClaw tool/plugin policy.
- `buildOpenClawConfig(options)` returns a testable configuration object and writes no file.
- The `personal-operator` plugin registers exactly `po_file_list`, `po_file_read`, `po_file_write`, and `po_file_delete`; all object keys are rooted under `PERSONAL_OPERATOR_WORKSPACE_PREFIX`.

- [x] Add failing tests that compare the approved tool and plugin lists exactly, reject every forbidden tool family, and prove no caller-controlled identity or namespace reaches a file key.
- [x] Add failing contract tests proving generated OpenClaw config uses `minimal`, loads only the repository plugin, contains no full/host execution or dangerous control-UI bypass, and exports no Telegram/provider secret to the child.
- [x] Add failing Docker/static tests proving ClawHub, API-key, cron, and legacy executable skill directories are absent from the image and source tree.
- [x] Implement the repository plugin with strict TypeBox schemas, relative-path validation, bounded UTF-8 reads/writes, deterministic listing, and server-derived S3 prefixing. Reject absolute paths, traversal, control characters, oversized payloads, directories, symlink-like keys, and internal top-level prefixes. User objects live only below `<namespace>/files/`.
- [x] Implement and share the frozen runtime policy between full and lightweight modes. Lightweight mode exposes only the same safe semantic capabilities, wraps web content as untrusted, pins DNS through the connection, and never shells out.
- [x] Generate the local gateway token at process start. Remove Telegram secret fetching, progressive Telegram delivery, user API-key management, ClawHub, and runtime scheduling from the contract and instructions. Disable text commands and use only `operator.read` plus `operator.write` gateway scopes.
- [x] Ensure OpenClaw is built from the pinned source and the image copies only the reviewed plugin. Lock all new dependencies with `npm ci` compatibility. Configure `agents.defaults.skills: []` and prove the pinned eligible skill inventory is empty.
- [x] Run focused tests, all bridge tests serially, Node syntax checks, Python static product tests, source searches for forbidden capabilities, pinned plugin/config/skill/live-web/gateway-scope proofs, and `git diff --check`.
- [x] Commit as `feat(runtime): freeze openclaw tool boundary`, then close review findings in `fix(runtime): close effective capability gaps`.

### Task 2: Immutable user binding and S3-only scoped credentials

**Files:**
- Create: `bridge/session-binding.js`
- Create: `bridge/session-binding.test.js`
- Create: `bridge/invocation-handler.js`
- Create: `bridge/invocation-handler.test.js`
- Modify: `bridge/agentcore-contract.js`
- Modify: `bridge/scoped-credentials.js`
- Modify: `bridge/scoped-credentials.test.js`
- Modify: `bridge/workspace-sync.js`
- Modify: `bridge/workspace-sync.test.js`
- Modify: `bridge/runtime-policy.js`
- Modify: `bridge/runtime-policy.test.js`
- Modify: `bridge/proxy-identity.test.js`
- Modify: `bridge/proxy-runtime-boundary.test.js`
- Modify: `bridge/image-support.test.js`
- Modify: `bridge/agentcore-proxy.js`
- Modify: `bridge/lightweight-agent.js`
- Modify: `bridge/lightweight-agent.test.js`
- Modify: `bridge/plugins/personal-operator/index.js`
- Modify: `bridge/plugins/personal-operator/index.test.js`
- Modify: `lambda/router/index.py`
- Create: `lambda/router/test_runtime_identity.py`
- Modify: `lambda/cron/index.py`
- Create: `lambda/cron/test_identity.py`
- Modify: `stacks/agentcore_stack.py`
- Modify: `stacks/router_stack.py`
- Modify: `stacks/cron_stack.py`
- Modify: `app.py`
- Modify: `scripts/deploy.sh`
- Modify: `scripts/test-local.sh`
- Modify: `scripts/e2e-deploy-and-test.sh`
- Modify: `tests/test_product_configuration.py`
- Modify: `tests/e2e/config.py`
- Modify: `tests/e2e/session.py`
- Modify: `tests/e2e/bot_test.py`

**Interfaces:**
- `canonicalNamespace(internalUserId)` returns that exact validated internal identifier; no actor/channel transformation is permitted.
- `SessionBinding.bindOrAssert({internalUserId, namespace})` binds synchronously once and throws `SESSION_IDENTITY_MISMATCH` on any later mismatch.
- `buildSessionPolicy({bucket, namespace, cmkArn})` returns only prefix-constrained S3 actions and, when configured, exact-key KMS use.
- `createScopedCredentials(...)` either returns scoped temporary credentials or throws; callers never inherit the container role.

- [x] Add failing two-user and ordering tests proving a warm runtime cannot be rebound and a mismatch is rejected before the first `await`, initialization, clock/counter change, chat/history/proxy/workspace mutation, or S3 call. Guard warm-up, chat, cron, and user-bound status.
- [x] Replace every actor-, channel-, file-, header-, environment-, and message-derived runtime identity with one canonical server-resolved `{internalUserId, namespace}` where `namespace === internalUserId`. Actor/channel remain delivery metadata only. Remove `default-user`, `/tmp/current-identity.json`, mutable identity-file logic, and caller-provided session/namespace fallbacks.
- [x] Make router and image paths use the internal user ID across Telegram, Slack, and Feishu. Two linked channel actors for one account must invoke the same `runtimeUserId`; a second user must remain disjoint.
- [x] Disable the inherited direct cron path until Task 4's trusted FIFO scheduler exists. Remove its payload-user fallback and stop granting Scheduler, DynamoDB, or PassRole authority to the runtime.
- [x] Add failing policy snapshots proving object access is limited to `arn:aws:s3:::<bucket>/<namespace>/*`, `ListBucket` is constrained by `s3:prefix`, no wildcard resource exists, and Scheduler, EventBridge, DynamoDB, Secrets Manager, IAM, STS, and PassRole actions are absent.
- [x] Implement an exact S3 policy with only list/get/put/delete. Include KMS encrypt/decrypt/data-key operations only against the exact configured CMK with `kms:ViaService=s3.eu-west-1.amazonaws.com` and exact caller-account conditions.
- [x] Introduce a separate workspace-session role whose base role can access only the workspace bucket and exact CMK. The broad AgentCore execution role may assume only that role and has no direct workspace, Scheduler, DynamoDB, or PassRole grant.
- [x] Make STS, credential-file, refresh, and workspace-client configuration failure fatal. Remove ambient/default credential providers and zero-access/full-role fallback. Sanitize the child environment to an exact allowlist with metadata disabled and `/dev/null` shared credentials.
- [x] Require exact `eu-west-1` in bridge, plugin, router, cron, infrastructure, deploy, and e2e paths; an explicitly different region fails before AWS access.
- [x] Run focused tests, the full serial bridge/Python suite, policy/static credential scans, generated-policy second-user negatives, synthesized-role assertions, spoofed-identity tests that exercise production exports/process behavior, and the two-user canary.
- [x] Commit as `feat(runtime): bind sessions to scoped user credentials`.

### Task 3: Lossless restore, mount, synchronization, and deletion

**Files:**
- Create: `bridge/workspace-manifest.js`
- Create: `bridge/workspace-manifest.test.js`
- Create: `bridge/sqlite-snapshot.js`
- Create: `bridge/sqlite-snapshot.test.js`
- Create: `bridge/workspace-lifecycle.js`
- Create: `bridge/workspace-lifecycle.test.js`
- Create: `bridge/agentcore-contract-lifecycle.test.js`
- Modify: `bridge/agentcore-contract.js`
- Rewrite: `bridge/workspace-sync.js` as an injected `WorkspaceSnapshotStore`
- Rewrite: `bridge/workspace-sync.test.js`
- Modify: `bridge/Dockerfile`
- Modify: `bridge/entrypoint.sh`
- Create: `scripts/verify-agentcore-storage.py`
- Create: `tests/test_verify_agentcore_storage.py`
- Modify: `scripts/deploy.sh`
- Modify: `stacks/agentcore_stack.py`
- Modify: `tests/test_product_configuration.py`
- Modify: `scripts/test-local.sh`
- Modify: `README.md`

**Interfaces:**
- `WorkspaceSnapshotStore` commits immutable payloads and a canonical manifest, then compare-and-swaps `<namespace>/.system/workspace/v1/current.json`.
- `prepareWorkspace({seedDir, mountedDir, namespace})` verifies the mount, restores into staging, atomically activates `live`, and only then permits configuration or OpenClaw startup.
- `SqliteSnapshot` creates and verifies a stable OpenClaw database snapshot using the pinned backup/VACUUM path; live WAL/SHM/journal files are never copied.
- `WorkspaceLifecycle.commitAfterTurn()` resolves only after the new pointer is durable; one promise-tail mutex serializes periodic, post-turn, and shutdown commits.

- [x] Define and test the canonical snapshot format under `<namespace>/.system/workspace/v1/`: immutable generation payloads, canonical sorted manifest, and a small current pointer binding generation, parent, manifest hash, and commit time. Reject noncanonical JSON, duplicate/unsafe paths, special files, hard links, unsupported databases, and all size/count overflow.
- [x] Add ordering/failure tests for payloads -> immutable manifest -> writable-lease recheck -> CAS current pointer -> post-commit GC. Use `IfNoneMatch` for immutable objects/first pointer and `IfMatch` for later pointers. Reconcile ambiguous failures by rereading the pointer; quarantine stale/conflicting writers. Never delete before pointer commit.
- [x] Make deletion logical by absence from the current manifest. Retain current and parent generations; GC may delete only a validated older ancestor's exact declared keys. Unknown objects and incomplete generations remain untouched for bucket lifecycle cleanup.
- [x] Snapshot every live OpenClaw SQLite database through a verified database-level snapshot; add a WAL-only committed-row round trip and corrupt/unsupported database negatives. Never copy live `.sqlite`, `-wal`, `-shm`, or `-journal` bytes.
- [x] Verify `/mnt/workspace` is the configured writable mount with a create/fsync/read/unlink probe. Restore into private staging with streamed size/hash checks, write a generation-bound ready marker, atomically rename to `live`, and link the home state only after verification. S3 is authoritative; AgentCore session storage is a verified working cache.
- [x] A missing pointer means a new user seeded from an immutable image directory. A malformed pointer, legacy flat layout, missing payload, unavailable S3, failed mount, failed copy, or hash mismatch stops initialization without disturbing the last good tree.
- [x] Serialize every save. A successful turn is not acknowledged until its pointer CAS succeeds. On post-turn persistence failure, return a retryable storage error, mark the runtime `QUARANTINED`, and reject later turns.
- [x] Shutdown enters `DRAINING`, stops periodic work, waits for the active turn/commit, stops OpenClaw and closes WAL, performs one final verified snapshot, then stops support processes. Failure or timeout exits nonzero.
- [x] Remove the native API-key credential-scan exemption and retain strict secret-pattern rejection for every workspace file.
- [x] Make deployment/session-storage configuration fatal: exact eu-west-1, one `/mnt/workspace` mount, READY polling, refetched filesystem comparison, versioning/KMS/public-access verification, and no warning-only ephemeral fallback.
- [x] Run manifest/store/lifecycle/SQLite tests with injected failures around every S3 operation, concurrent-writer and restart round trips, full serial bridge tests, storage-verifier tests, shell syntax, CDK/static startup-order checks, and `git diff --check`.
- [x] Commit as `feat(runtime): make workspace lifecycle lossless`.

## Runtime Hardening Exit Gate

Runtime hardening is complete only when all three tasks are separately implemented, specification-reviewed, and code-quality-reviewed; the full serial bridge suite and product configuration tests pass; forbidden-capability searches return no runtime exposure; a two-user negative canary passes; and workspace deletion survives a synthetic restart. Cloud behavior remains provisional until later staging evidence exists.
