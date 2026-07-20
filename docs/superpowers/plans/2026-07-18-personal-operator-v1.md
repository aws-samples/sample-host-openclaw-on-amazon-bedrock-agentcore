# Personal Operator v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use
> `superpowers:subagent-driven-development` or
> `superpowers:executing-plans`. Every production behavior change is
> test-driven and receives a separate review before its integration gate.

**Goal:** Extend the proven v0 into a locally complete, deployable staging
candidate with a governed capability kernel, networkless Linux jobs, trusted
scheduling, portable state, curated connector/browser boundaries, and a
simple invite-only consumer pilot.

**Architecture:** Keep OpenClaw as a credential-free conversational runtime.
Expose only release-owned `po_*` schemas through a trusted loopback relay. A
server-side capability gateway validates live user, runtime, release, catalog,
installation, target, quota, replay, and deletion state before invoking an
isolated adapter. Provider effects remain persisted, exactly approved,
reconciled, and receipted. Staging artifacts and runtime bindings are immutable
and transactionally promoted.

**Tech stack:** Python 3.12/3.13, AWS CDK v2, CloudFormation AgentCore L1
resources, Lambda, ECR, KMS, DynamoDB, S3, SQS FIFO, EventBridge Scheduler,
CloudWatch, Node.js 24, OpenClaw 2026.7.2, React/Vite, canonical JSON, injected
fake provider clients, and a local synthetic MCP proof.

## Global constraints

- Base commit is exactly `0fad2ce4758dda4b2b1221dbb4db0eee3e6c8fe3`.
- Work only in the Personal Operator worktrees. Do not modify Tasa Verify.
- Preserve all unrelated user worktrees and `stash@{0}: task3-root-red-tests`.
- Region is exactly `eu-west-1`; model/runtime pins remain frozen unless a
  separately reviewed dependency task changes them.
- Do not deploy, push, create paid resources, use real provider credentials,
  send messages, or execute provider effects during local implementation.
- OpenClaw receives no provider/browser/database/approval/cross-user
  credentials and no general shell, browser, cron, dynamic MCP, arbitrary
  plugin, ClawHub, or user-key surface.
- One writer owns each file in a parallel wave. Integration is serial.
- All new behavior follows RED -> GREEN -> REFACTOR. Each task report includes
  exact RED and GREEN commands and outputs.
- A provider or cloud timeout with uncertain persistence is `UNCERTAIN`, never
  an automatic retry.
- Completion claims require fresh aggregate evidence and independent review.

## Execution graph

```text
Task 1 contracts
  |
  +-- Task 2 capability catalog/relay/gateway -- Task 3 action kernel
  |
  +-- Task 4 staging artifacts/runtime transaction
  |
  +-- Task 5 pilot invite/web/measurement
              |
              +-- integration gate A
                    |
                    +-- Task 6 URL reader
                    +-- Task 7 scheduler
                    +-- Task 8 compute capsule
                    +-- Task 9 portable state
                    +-- Task 10 connector/browser proof
                              |
                              +-- Task 11 observability/pilot harness
                                        |
                                        +-- Task 12 hostile release audit
```

Tasks 2, 4, and 5 run in parallel after Task 1. Tasks 6-10 run in parallel
only after integration gate A closes. Task 12 is serial and uses a clean
integration worktree.

---

### Task 1: Freeze canonical v1 contracts and catalog source

**Files:**

- Create: `lambda/capabilities/__init__.py`
- Create: `lambda/capabilities/contracts.py`
- Create: `lambda/capabilities/catalog.py`
- Create: `lambda/capabilities/test_contracts.py`
- Create: `specs/capabilities/catalog-v1.json`
- Create: `specs/capabilities/schemas/*.json`
- Create: `docs/CAPABILITY-BOUNDARY.md`
- Modify: `docs/PRIVACY-BOUNDARY.md`

**Interfaces:**

- strict frozen value types for `CapabilityCatalogV1`,
  `CapabilityInstallationV1`, `TurnCapabilityGrantV1`, `CapabilityCallV1`,
  `CapabilityResultV1`, `TargetGrantV1`, `ActionProposalV1`,
  `EffectReceiptV1`, `ScheduleSpecV1`, `ScheduleOccurrenceV1`,
  `ComputeJobSpecV1`, `ComputeReceiptV1`, `ConnectorManifestV1`,
  `PortableStateManifestV2`, `ImportPlanV1`, and `ImportReceiptV1`;
- `parse_canonical_json(bytes, expected_schema)` rejects duplicate keys,
  aliases, extras, noncanonical bytes, nonfinite numbers, and overflow;
- `compile_catalog(release_commit, schema_dir)` returns canonical bytes and a
  digest-bound immutable catalog.

- [ ] Write failing tests for canonical round trips, duplicate/extra/alias
  rejection, every size/count/grammar boundary, enum exhaustiveness, digest
  binding, and mutation after construction.
- [ ] Add failing catalog fixtures covering the four v0 workspace tools and six
  v1 capability tools with exact risk, approval, retry, retention, quota, and
  deletion metadata.
- [ ] Implement strict contract parsing/serialization and the deterministic
  catalog compiler without AWS/provider imports.
- [ ] Prove two compiles are byte-identical and one schema-byte change changes
  the digest.
- [ ] Document the credential holder, authority decision, effect executor,
  retry behavior, and deletion behavior for every catalog entry.
- [ ] Run focused tests, Python compilation, secret scan, and `git diff --check`.
- [ ] Commit as `feat(capabilities): freeze governed contracts`.

### Task 2: Immutable tool catalog, trusted relay, and admission gateway

**Files:**

- Create: `bridge/capability-catalog.js`
- Create: `bridge/capability-catalog.test.js`
- Create: `bridge/capability-relay.js`
- Create: `bridge/capability-relay.test.js`
- Modify: `bridge/plugins/personal-operator/index.js`
- Modify: `bridge/plugins/personal-operator/index.test.js`
- Modify: `bridge/runtime-policy.js`
- Modify: `bridge/runtime-policy.test.js`
- Modify: `bridge/agentcore-contract.js`
- Create: `lambda/capabilities/gateway.py`
- Create: `lambda/capabilities/admission.py`
- Create: `lambda/capabilities/ledger.py`
- Create: `lambda/capabilities/test_gateway.py`
- Modify: `stacks/agentcore_stack.py`
- Create: `stacks/capability_stack.py`
- Modify: `app.py`

**Interfaces:**

- `CapabilityRelay.bind_turn(grant)` retains authority only in trusted memory;
- `CapabilityRelay.call(tool_use_id, tool_name, args)` derives deterministic
  `callId` and forwards a server-owned envelope;
- `CapabilityGateway.invoke(call, iam_context)` validates the live binding,
  catalog, installation, target grants, call budget, replay ledger, and
  deletion fence before adapter dispatch.

- [ ] Write failing parity tests comparing catalog, full plugin, warm-up path,
  runtime policy, and gateway operation registry exactly.
- [ ] Write failing relay tests proving grants/tokens never enter child env,
  model input, tool arguments/results, workspace, or logs.
- [ ] Write failing gateway tests for wrong user/session/runtime/version,
  release/catalog drift, expired grant, replay, argument mutation, quota,
  disabled pack, kill switch, and deletion fence; denied calls must invoke no
  adapter.
- [ ] Add the six explicit v1 tool schemas; keep all adapters disabled until
  their own tasks close. Do not add a generic capability tool.
- [ ] Implement deterministic call identity, replay-safe read retry, typed
  results, bounded errors, and `UNCERTAIN` mutation handling.
- [ ] Give the runtime role only exact invoke authority for the gateway; it
  receives no provider, browser, scheduler, DynamoDB, Secrets Manager, or MCP
  authority.
- [ ] Run focused Python/Node tests, synthesized IAM assertions, credential
  absence searches, and existing runtime boundary tests.
- [ ] Commit as `feat(capabilities): add trusted relay and gateway`.

### Task 3: Generalize the action and connector kernel

**Files:**

- Create: `lambda/actions/connectors.py`
- Create: `lambda/actions/proposals.py`
- Create: `lambda/actions/receipts.py`
- Create: `lambda/actions/test_connectors.py`
- Modify: `lambda/actions/models.py`
- Modify: `lambda/actions/state_machine.py`
- Modify: `lambda/actions/gmail_send.py`
- Modify: `lambda/actions/reconcile.py`
- Modify: `lambda/actions/test_state_machine.py`
- Modify: `lambda/actions/test_gmail_send.py`

**Interfaces:**

- `ConnectorAdapter.read(context, operation, args)`;
- `ConnectorAdapter.prepare(...) -> ActionProposalV1`;
- `ConnectorAdapter.dispatch(approved_action) -> EffectReceiptV1`;
- `ConnectorAdapter.reconcile(action) -> EffectReceiptV1 | None`;
- `ConnectorAdapter.revoke(connection_ref)`.

- [ ] Write failing adapter contract tests proving no dispatch without a
  persisted approved exact proposal and no credential resolution before the
  selected adapter is admitted.
- [ ] Write failing compatibility tests for every current Gmail approval,
  founder-only, deterministic ID, timeout, reconciliation, and no-resend rule.
- [ ] Extract generic proposals and receipts beneath the Gmail wrapper without
  changing existing public behavior or stored-record compatibility.
- [ ] Recheck the deletion fence immediately before provider dispatch and
  retain `UNCERTAIN` on ambiguous provider acceptance.
- [ ] Run the full actions, web approval, synthetic founder journey, and
  provider fault-injection suites.
- [ ] Commit as `refactor(actions): add governed connector kernel`.

### Task 4: Immutable staging release foundation

**Files:**

- Create: `release_tools/__init__.py`
- Create: `release_tools/contracts.py`
- Create: `release_tools/ecr.py`
- Create: `release_tools/agentcore.py`
- Create: `release_tools/transaction.py`
- Create: `release_tools/test_*.py`
- Create: `scripts/staging-release.py`
- Modify: `scripts/deploy.sh`
- Modify: `scripts/build-trusted-lambda-asset.sh`
- Modify: `stacks/trusted_lambda_asset.py`
- Modify: `stacks/agentcore_stack.py`
- Modify: `app.py`
- Modify: `tests/test_deploy_safety.py`
- Modify: `tests/test_trusted_lambda_packaging.py`
- Modify: `docs/OPERATIONS.md`
- Modify: `docs/RELEASE-EVIDENCE.md`

**Interfaces:**

- strict `RuntimeImageEvidence`, `RuntimeContextV3`,
  `TrustedLambdaAssetV2`, and `StagingTransactionV1`;
- deterministic Lambda ZIP and manifest;
- private immutable-tag `personal-operator/bridge` ECR repository;
- direct CloudFormation `AWS::BedrockAgentCore::Runtime` and
  `AWS::BedrockAgentCore::RuntimeEndpoint`, with endpoint
  `release_<40-sha>`;
- credential-free `--preflight`, plus explicit phase/resume/status/rollback
  CLI modes that require credentials only at the mutation boundary.

- [ ] Write failing tests for duplicate/extra/cross-account/wrong-region
  context, mutable tags, endpoint collisions, unknown runtime states,
  transaction-state violations, partial failures, ambiguity, resume, and
  rollback.
- [ ] Make the trusted Lambda builder emit a deterministic ZIP bound to exact
  commit/tree, builder digest, requirements, file inventory, architecture,
  size, and SHA-256; CDK must consume that exact archive hash.
- [ ] Add one private ECR repository with immutable tags, scanning, lifecycle,
  managed signing, and exact execution-role pull policy.
- [ ] Bind runtime and endpoint resources to an immutable `@sha256:` URI and
  never retarget an existing release endpoint.
- [ ] Implement atomic canonical release artifacts and an `UNCERTAIN` journal
  state; no later phase runs after an unproven mutation.
- [ ] Keep all tests credential-free with injected fake clients. Do not deploy.
- [ ] Update evidence wording to “implemented and locally verified; not
  deployed,” leaving every external gate open.
- [ ] Run release tests, deterministic double-build where Docker is available,
  offline synth, cdk-nag, and the aggregate gate.
- [ ] Commit as `feat(release): add immutable staging transaction`.

### Task 5: Consumer invite, overview, source-card, and lifecycle experience

**Files:**

- Create: `lambda/control/invites.py`
- Create: `lambda/control/test_invites.py`
- Create: `scripts/pilot-invites.py`
- Modify: `lambda/router/message_queue.py`
- Modify: `lambda/router/index.py`
- Modify: `lambda/worker/index.py`
- Modify: `lambda/control/index.py`
- Modify: `lambda/web/auth.py`
- Modify: `lambda/web/index.py`
- Modify: `lambda/web/retention.py`
- Create: `lambda/web/overview.py`
- Create: `lambda/web/measurements.py`
- Create: `lambda/web/test_overview.py`
- Modify: `web/src/**`
- Modify: `stacks/router_stack.py`
- Modify: `stacks/web_stack.py`

**Interfaces:**

- one-time opaque Telegram deep-link invite issue/redeem/revoke;
- session tickets with allowlisted return path;
- `GET /api/overview`;
- `POST /api/connections/google-gmail-readonly/disconnect`;
- `POST /api/session/logout`;
- typed latest scan status and privacy-safe scan feedback.

- [ ] Write failing invite tests for atomic redemption races, replay, expiry,
  revocation, malformed tokens, deletion tombstones, and plaintext token
  absence from logs, queues, metrics, and storage.
- [ ] Write failing session tests for cross-user use, five-minute expiry,
  one-time consumption, and exact return-path allowlist.
- [ ] Write failing overview/UI tests for connection/re-authentication, last
  scan, workspace/runtime, export/delete, mobile navigation, and
  `externalEffects:false` with no send/approval controls.
- [ ] Persist bounded scan state and one privacy-safe useful/not-useful response
  per scan; exclude identities, source IDs, addresses, subjects, bodies, and
  excerpts.
- [ ] Carry `callbackQueryId` to the worker and acknowledge it independently of
  exactly-once business processing.
- [ ] Make local draft edits stale any pending founder approval; keep the
  external pilot read-only.
- [ ] Correct export and deletion wording, including deterministic unencrypted
  ZIP and the 30-minute minimum reconciliation floor.
- [ ] Run focused router/control/web tests, web accessibility/mobile tests,
  production build, and the synthetic pilot flow.
- [ ] Commit as `feat(pilot): add invite-only consumer journey`.

## Integration gate A

- [ ] Merge Tasks 2, 4, and 5 only after each has a clean task review.
- [ ] Integrate Task 3 after resolving action-contract overlap centrally.
- [ ] Run catalog parity, runtime boundary, actions, web, integration, full
  aggregate, offline synth, and cdk-nag gates.
- [ ] Verify runtime role still has no provider/browser/scheduler/secret/data
  authority beyond exact capability-gateway invocation.
- [ ] Record the exact integration commit before opening Tasks 6-10.

### Task 6: Exact-target public URL reader

**Files:**

- Create: `lambda/capabilities/web_reader.py`
- Create: `lambda/capabilities/target_grants.py`
- Create: `lambda/capabilities/test_web_reader.py`
- Modify: `lambda/capabilities/gateway.py`
- Modify: `bridge/plugins/personal-operator/index.js`
- Modify: `bridge/lightweight-agent.js`

- [ ] Write failing hostile tests for target modification, previous-turn and
  workspace-derived URLs, private/special/metadata IPs, mixed DNS answers,
  DNS rebinding, encoded redirects, changed hosts, MIME/size/time overflow, and
  page-instruction exfiltration. Denials must make zero network calls.
- [ ] Derive grants only from exact URLs in the current authenticated message.
- [ ] Implement GET-only public fetching with DNS pinning, bounded same-origin
  redirects, no cookies/auth headers, sanitization, and untrusted provenance.
- [ ] Prove the runtime’s old helper remains unregistered and cannot be reached
  without the gateway.
- [ ] Run the hostile corpus, parity tests, and log/content-retention checks.
- [ ] Commit as `feat(capabilities): add exact target web reader`.

### Task 7: Trusted scheduler and read-only occurrences

**Files:**

- Create: `lambda/scheduler/__init__.py`
- Create: `lambda/scheduler/models.py`
- Create: `lambda/scheduler/service.py`
- Create: `lambda/scheduler/ingress.py`
- Create: `lambda/scheduler/test_*.py`
- Create: `stacks/scheduler_stack.py`
- Modify: `app.py`
- Modify: `lambda/capabilities/gateway.py`
- Modify: `lambda/worker/index.py`

- [ ] Write failing state/race tests for propose/confirm/update/pause/cancel,
  duplicate fires, stale generations, provider ambiguity, deletion, and import.
- [ ] Implement only `REMINDER` and `READ_ONLY_AGENT_TURN` task types.
- [ ] Send only opaque schedule ID, generation, and fire time through
  EventBridge; strong-read then enqueue a deterministic occurrence into the
  existing per-user FIFO.
- [ ] Prove scheduled work cannot execute connector/browser effects and can
  only read or prepare a fresh proposal.
- [ ] Export definitions and import them disabled; delete live schedules before
  deletion completion.
- [ ] Run focused tests, IAM assertions, duplicate-fire fault tests, synth, and
  deletion lifecycle tests.
- [ ] Commit as `feat(scheduler): add governed read-only schedules`.

### Task 8: Networkless Linux compute capsule

**Files:**

- Create: `lambda/compute/__init__.py`
- Create: `lambda/compute/models.py`
- Create: `lambda/compute/service.py`
- Create: `lambda/compute/importer.py`
- Create: `lambda/compute/test_*.py`
- Create: `compute/Dockerfile`
- Create: `compute/runner.py`
- Create: `compute/seccomp.json`
- Create: `stacks/compute_stack.py`
- Modify: `app.py`
- Modify: `lambda/capabilities/gateway.py`

- [ ] Write failing spec/input/output tests for immutable image digest, input
  hashes, quotas, invalid paths, symlinks, hardlinks, devices, size/count
  overflow, changed outputs, timeout, OOM, fork bomb, and cross-user access.
- [ ] Build a non-root read-only-root job image with no package installer,
  credentials, network route, live workspace mount, or ambient AWS providers.
- [ ] Stage immutable inputs and a fresh output directory; kill the whole job
  tree on deadline/resource breach.
- [ ] Import validated outputs atomically only under `jobs/<jobId>/` and issue a
  content-addressed receipt.
- [ ] Prove DNS, internet, VPC endpoint, IMDS, and credential-provider attempts
  fail in the local sandbox harness.
- [ ] Run three-user Cartesian tests, malicious output corpus, image/static
  scan, synth, and catalog parity tests.
- [ ] Commit as `feat(compute): add networkless linux jobs`.

### Task 9: Portable state v2 and staged import

**Files:**

- Create: `lambda/portable/__init__.py`
- Create: `lambda/portable/manifest.py`
- Create: `lambda/portable/exporter.py`
- Create: `lambda/portable/importer.py`
- Create: `lambda/portable/test_*.py`
- Modify: `lambda/web/retention.py`
- Modify: `lambda/web/index.py`
- Modify: `web/src/**`

- [ ] Write failing deterministic export tests with per-object path/type/size/
  hash coverage and explicit include/exclude categories.
- [ ] Write failing import tests for malformed/noncanonical bundles, hash and
  size mismatch, duplicate paths, traversal, secrets, active authority,
  pending effects, deletion tombstones, replay, failure atomicity, and
  cross-user activation.
- [ ] Implement content-addressed v2 export and a dry-run `ImportPlanV1`.
- [ ] Bind activation approval to the exact complete bundle hash and atomically
  compare-and-swap the staged generation.
- [ ] Import schedules disabled, connectors disconnected, and immutable past
  receipts non-replayable; exclude credentials, sessions, grants, approvals,
  runtime internals, and pending/uncertain effects.
- [ ] Run byte-reproducibility, secret-corpus, replay, deletion, and three-user
  isolation tests.
- [ ] Commit as `feat(portable): add content addressed state transfer`.

### Task 10: Curated connector SDK and browser authority boundary

**Files:**

- Create: `lambda/connectors/__init__.py`
- Create: `lambda/connectors/manifest.py`
- Create: `lambda/connectors/mcp.py`
- Create: `lambda/connectors/synthetic.py`
- Create: `lambda/connectors/test_*.py`
- Create: `lambda/browser/__init__.py`
- Create: `lambda/browser/gateway.py`
- Create: `lambda/browser/test_gateway.py`
- Create: `stacks/browser_stack.py`
- Modify: `stacks/agentcore_stack.py`
- Modify: `lambda/capabilities/gateway.py`

- [ ] Write failing connector tests for manifest/tool-list drift, unknown tools,
  schema mutation, arbitrary endpoints, credential leakage, malicious results,
  oversize, timeout, and deletion.
- [ ] Implement reviewed-build-time schema locking and a synthetic local MCP
  adapter entirely behind the capability gateway.
- [ ] Keep OpenClaw unaware of MCP URLs, OAuth tokens, server configuration,
  and discovered tool lists.
- [ ] Move all browser IAM out of the runtime role. Implement a disabled-by-
  default Browser Gateway contract for exact targets, profile refs,
  observation redaction, credential injection, and action proposals.
- [ ] Prove no authenticated browser or real new connector is enabled, and all
  browser submit/upload/send/delete operations require the generic action
  kernel.
- [ ] Run gateway, credential-isolation, schema-drift, IAM, synth, and cdk-nag
  tests.
- [ ] Commit as `feat(connectors): add curated adapter boundary`.

### Task 11: Privacy-safe observability and moderated pilot harness

**Files:**

- Create: `lambda/observability/events.py`
- Create: `lambda/observability/report.py`
- Create: `lambda/observability/test_*.py`
- Create: `tests/integration/test_synthetic_pilot_v1.py`
- Create: `scripts/run-synthetic-pilot.py`
- Modify: `stacks/observability_stack.py`
- Modify: `docs/OPERATIONS.md`
- Modify: `docs/RELEASE-EVIDENCE.md`

- [ ] Write failing schema tests rejecting identities, provider/source IDs,
  addresses, subjects, bodies, excerpts, URLs, model content, workspace content,
  tokens, and credentials from metrics or reports.
- [ ] Add bounded invite/OAuth/scan/card/feedback/draft/export/deletion,
  capability, schedule, compute, connector, queue, maintenance, and uncertain
  effect metrics.
- [ ] Add alarms for DLQ, any uncertain effect, repeated scan failure, missing
  maintenance heartbeat, aged deletion, connector drift, and compute isolation
  failure.
- [ ] Run a credential-free three-participant synthetic journey from invite
  through connect, scan, feedback, workspace, compute/schedule, export/import,
  and deletion.
- [ ] Emit an aggregate cohort report containing no personal or source data.
- [ ] Document cohort pause, provider shutdown, DLQ handling without unsafe
  redrive, schedule/compute kill switches, deletion, and pilot stop criteria.
- [ ] Commit as `test(pilot): add private v1 operational evidence`.

### Task 12: Integrated hostile review and release evidence

**Files:**

- Modify: `scripts/test-local.sh`
- Modify: `tests/security/**`
- Modify: `docs/RELEASE-EVIDENCE.md`
- Modify: `README.md`
- Create: `docs/V1-IMPLEMENTATION-EVIDENCE.md`

- [ ] Start from a clean exact integration commit and build every ignored
  prerequisite artifact in the documented order.
- [ ] Run focused security suites, full aggregate, Python compilation, Node
  syntax/tests, production web build, deterministic artifact tests, offline
  synth, and cdk-nag.
- [ ] Run static credential, forbidden-runtime-capability, dynamic-MCP,
  browser-IAM, networkless-compute, catalog-parity, cross-tenant, target-grant,
  schedule-effect, import-replay, and log-content searches/tests.
- [ ] Commission an independent specification review and code/security review;
  resolve every high/critical finding and rerun affected gates.
- [ ] Record exact commit, command, count, digest, and pass/fail evidence.
- [ ] Leave AWS/provider/pilot gates explicitly open unless exact external
  evidence was actually produced and inspected.
- [ ] Update README with the implemented consumer surface and precise
  preproduction boundary.
- [ ] Commit as `test(release): verify personal operator v1 locally`.

