# Personal Operator Runtime Hardening Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Every behavior change follows red-green-refactor and receives a separate review before the next task starts.

**Goal:** Replace the imported experimental OpenClaw runtime with a fail-closed, single-user runtime that exposes only four namespaced workspace tools plus safe web retrieval, never receives product/provider authority, and preserves user workspace bytes and deletions across restarts.

**Architecture:** OpenClaw remains the conversational runtime, but it runs with the `minimal` tool profile and one repository-owned plugin loaded from an explicit path. The plugin derives the user's S3 namespace only from server-controlled environment, while the trusted Node contract owns initialization and credential setup. A session is immutably bound to one canonical user. Restore completes before OpenClaw starts, and synchronization uses a manifest so failed copies and user deletions cannot silently resurrect data.

**Tech Stack:** Node.js 24.15+, OpenClaw source commit `4bfaccafd62ac2ff2e70ca1decc40fb1297ab438`, OpenClaw plugin SDK, TypeBox, AWS STS/S3 SDK v3, Node test runner.

## Frozen Security Decisions

- OpenClaw's exact approved tools are `session_status`, `web_search`, `web_fetch`, `po_file_list`, `po_file_read`, `po_file_write`, and `po_file_delete`.
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

- [ ] Add failing tests that compare the approved tool and plugin lists exactly, reject every forbidden tool family, and prove no caller-controlled identity or namespace reaches a file key.
- [ ] Add failing contract tests proving generated OpenClaw config uses `minimal`, loads only the repository plugin, contains no full/host execution or dangerous control-UI bypass, and exports no Telegram/provider secret to the child.
- [ ] Add failing Docker/static tests proving ClawHub, API-key, cron, and legacy executable skill directories are absent from the image and source tree.
- [ ] Implement the repository plugin with strict TypeBox schemas, relative-path validation, bounded UTF-8 reads/writes, deterministic listing, and server-derived S3 prefixing. Reject absolute paths, traversal, control characters, oversized payloads, directories, and symlink-like keys.
- [ ] Implement and share the frozen runtime policy between full and lightweight modes. Lightweight mode may expose only the same safe semantic capabilities and must not shell out.
- [ ] Generate the local gateway token at process start. Remove Telegram secret fetching, progressive Telegram delivery, user API-key management, ClawHub, and runtime scheduling from the contract and instructions.
- [ ] Ensure OpenClaw is built from the pinned source and the image copies only the reviewed plugin. Lock all new dependencies with `npm ci` compatibility.
- [ ] Run focused tests, all bridge tests serially, Node syntax checks, Python static product tests, source searches for forbidden capabilities, and `git diff --check`.
- [ ] Commit as `feat(runtime): freeze openclaw tool boundary`.

### Task 2: Immutable user binding and S3-only scoped credentials

**Files:**
- Create: `bridge/session-binding.js`
- Create: `bridge/session-binding.test.js`
- Modify: `bridge/agentcore-contract.js`
- Modify: `bridge/scoped-credentials.js`
- Modify: `bridge/scoped-credentials.test.js`
- Modify: `bridge/proxy-identity.test.js`
- Modify: `bridge/plugins/personal-operator/index.js`
- Modify: `bridge/plugins/personal-operator/index.test.js`

**Interfaces:**
- `SessionBinding.bindOrAssert({internalUserId, namespace})` binds once and throws `SESSION_IDENTITY_MISMATCH` on any later mismatch.
- `buildSessionPolicy({bucket, namespace, cmkArn})` returns only prefix-constrained S3 actions and, when configured, exact-key KMS use.
- `createScopedCredentials(...)` either returns scoped temporary credentials or throws; callers never inherit the container role.

- [ ] Add failing two-user tests proving a warm runtime cannot be rebound, mismatched requests are rejected before chat/history/workspace mutation, and namespace is never accepted from model or tool input.
- [ ] Replace actor-derived mutable identity with one canonical server-resolved `{internalUserId, namespace}` binding established before restore or configuration.
- [ ] Add failing policy snapshots proving object access is limited to `arn:aws:s3:::<bucket>/<namespace>/*`, `ListBucket` is constrained by `s3:prefix`, no wildcard resource exists, and Scheduler, EventBridge, DynamoDB, Secrets Manager, IAM, STS, and PassRole actions are absent.
- [ ] Implement an exact S3 policy with the minimum list/get/put/delete operations. Include KMS encrypt/decrypt/data-key operations only against one configured CMK and only when S3 object encryption requires them.
- [ ] Make STS or credential-file failure fatal and remove all full-role fallback behavior. Sanitize child environment so only the scoped credential set is visible to OpenClaw/plugin code.
- [ ] Pin or fail region handling to `eu-west-1`, including STS/S3 clients and generated configuration.
- [ ] Run focused tests, the full serial bridge suite, policy/static credential scans, a generated-policy negative assertion for a second namespace, and `git diff --check`.
- [ ] Commit as `feat(runtime): bind sessions to scoped user credentials`.

### Task 3: Lossless restore, mount, synchronization, and deletion

**Files:**
- Create: `bridge/workspace-lifecycle.js`
- Create: `bridge/workspace-lifecycle.test.js`
- Modify: `bridge/agentcore-contract.js`
- Modify: `bridge/workspace-sync.js`
- Modify: `bridge/workspace-sync.test.js`
- Modify: `bridge/Dockerfile`
- Modify: `bridge/entrypoint.sh`

**Interfaces:**
- `prepareWorkspace({sourceDir, mountedDir, namespace})` completes restore/copy/mount verification before configuration or OpenClaw startup.
- `restoreWorkspace(namespace)` returns a typed restore result and fails closed on partial copy, hash mismatch, unsafe path, or unavailable storage.
- `saveWorkspace(namespace)` writes changed objects and a versioned manifest, then deletes only manifest-owned paths removed locally.

- [ ] Add failing ordering tests proving OpenClaw configuration/start cannot occur before a verified mount and completed restore, including an initially empty mounted volume.
- [ ] Add failure-injection tests for interrupted S3 downloads, failed cross-device copies, nonempty destination conflicts, hash mismatch, unsafe keys, and unavailable storage. The sole good copy must remain intact after every failure.
- [ ] Add round-trip tests for create/update/delete/rename, empty files, nested paths, Unicode names, and restart recovery. Prove deleted files do not resurrect and unknown remote objects are never deleted.
- [ ] Implement an atomic staging-and-rename restore where supported, verified copy fallback where not, and explicit lifecycle states. Never swallow a trust-bearing restore/mount error.
- [ ] Persist a versioned manifest containing normalized relative path, size, and SHA-256. Upload data before the new manifest; delete only objects present in the previous manifest but absent locally; write the new manifest last.
- [ ] Remove the native API-key credential-scan exemption and retain strict secret-pattern rejection for all workspace files.
- [ ] Make deployment/session-storage configuration errors fatal in the infrastructure contract rather than silently degrading to ephemeral storage.
- [ ] Run focused lifecycle/sync tests repeatedly, full serial bridge tests, shell syntax checks, static startup-order checks, and `git diff --check`.
- [ ] Commit as `feat(runtime): make workspace lifecycle lossless`.

## Runtime Hardening Exit Gate

Runtime hardening is complete only when all three tasks are separately implemented, specification-reviewed, and code-quality-reviewed; the full serial bridge suite and product configuration tests pass; forbidden-capability searches return no runtime exposure; a two-user negative canary passes; and workspace deletion survives a synthetic restart. Cloud behavior remains provisional until later staging evidence exists.
