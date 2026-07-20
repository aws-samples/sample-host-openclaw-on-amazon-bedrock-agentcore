# Task 2 Independent Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every Important blocker in the Task 2 independent remediation review and resolve the evidence-backed DynamoDB CMK permission minor without adding adapters or deployment authority.

**Architecture:** Preserve the existing admission-before-ledger design by carrying the durable target grant's exact claimed call IDs into live admission and exempting only that exact already-claimed call from aggregate exhaustion. Treat every rejection from the already-invoked Lambda transport as delivery-ambiguous at the relay boundary. Cross-bind each reviewed source inventory row to the corresponding actual packaged file row, and align the exact constrained KMS action set with the local CDK reference and AWS's documented DynamoDB CMK cryptographic operations.

**Tech Stack:** Python 3.13, pytest, DynamoDB low-level client contracts, Node.js 24 `node:test`, AWS SDK v3 Lambda client, AWS CDK, Ruff, Black.

## Global Constraints

- Keep all production capability adapters disabled.
- Preserve tenant, exact-grant, tool-use, stable logical-operation, turn-budget, and target-use isolation.
- Reads receive at most one retry of the exact call; mutations are never automatically replayed.
- Keep IAM resources exact and retain `kms:CallerAccount` plus regional `kms:ViaService` constraints.
- Do not deploy, invoke AWS, access providers, use credentials, push, browse, or send real messages.

---

### Task 1: Durable target replay and retry admission

**Files:**
- Modify: `lambda/capabilities/test_composition.py`
- Modify: `lambda/capabilities/admission.py`
- Modify: `lambda/capabilities/durable.py`
- Modify: `lambda/capabilities/test_gateway.py`

**Interfaces:**
- Consumes: `LiveTargetGrant`, `DynamoAdmissionRepository`, `DynamoCapabilityLedger`, and `CapabilityGateway.invoke()`.
- Produces: `LiveTargetGrant.claimed_call_ids: tuple[str, ...]` and exact-call exemption from aggregate target exhaustion.

- [ ] **Step 1: Write hostile durable gateway tests**

Add tests using `MemoryDynamoClient` and the actual Dynamo repository/ledger that seed a `maxUses=1` web target, dispatch through a test adapter, then prove exact cached replay, ignored-response recovery, one exact read retry, retry exhaustion, one target use, one turn/pack budget charge, denial of a fresh call, and no cross-grant cache return.

- [ ] **Step 2: Run tests to verify RED**

Run: `AWS_REGION=eu-west-1 AWS_DEFAULT_REGION=eu-west-1 PYTHONPATH=lambda uv run --with-requirements requirements.txt --with-requirements lambda/requirements.txt --with pytest python -m pytest -q lambda/capabilities/test_composition.py`

Expected: the exact replay and same-call retry assertions fail with `TARGET_GRANT_EXHAUSTED` after the first target claim.

- [ ] **Step 3: Preserve and validate exact claimed calls**

Extend `LiveTargetGrant` with a canonical tuple of `call_<64 lowercase hex>` identities. Convert the durable JSON claim list to that tuple. In `_admit_target`, admit an exhausted matching target only when `call.call_id` is already present; every fresh call remains exhausted. Update the in-memory repository double so a successful target claim updates the same live state.

- [ ] **Step 4: Run focused Python GREEN**

Run the RED command again plus `lambda/capabilities/test_gateway.py`.

Expected: all tests pass, adapter dispatch remains bounded, and durable target/budget counters remain one.

- [ ] **Step 5: Commit**

Commit message: `fix(capabilities): admit claimed target replay`

### Task 2: Typed Lambda delivery ambiguity

**Files:**
- Modify: `bridge/capability-relay.test.js`
- Modify: `bridge/capability-relay.js`

**Interfaces:**
- Consumes: `createLambdaGatewayTransport()` and `CapabilityRelay.call()`.
- Produces: validated `FAILED_RETRYABLE/SAFE_RETRY` read results and `UNCERTAIN/RECONCILE_ONLY` mutation results for every Lambda `FunctionError`, missing/malformed payload, or SDK rejection after transport invocation.

- [ ] **Step 1: Write real-transport hostile tests**

Inject a Lambda client into `createLambdaGatewayTransport()` for each of four responses: `FunctionError`, absent `Payload`, malformed JSON `Payload`, and rejected `send()`. Exercise each transport through `CapabilityRelay` as both `web.exact.read` and `workspace.file.write`; assert no promise rejects, results validate against the call, and retry policies match operation risk.

- [ ] **Step 2: Run tests to verify RED on Node 24**

Run: `PATH=/opt/homebrew/opt/node@24/bin:$PATH node --test --test-concurrency=1 bridge/capability-relay.test.js`

Expected: Lambda-generated `CapabilityRelayError` escapes for FunctionError, absent payload, and malformed payload.

- [ ] **Step 3: Normalize the invoked transport boundary**

Remove the special rethrow for `CapabilityRelayError` inside `CapabilityRelay.#dispatch`. All errors raised by `#gatewayTransport(entry.envelope)` are conservatively delivery-ambiguous and become a local typed result selected by the catalog retry mode. Constructor, grant, argument, quota, and local admission errors remain outside this catch and continue to reject before transport.

- [ ] **Step 4: Run focused Node GREEN**

Run the RED command again.

Expected: all relay tests pass and each hostile Lambda case dispatches exactly once.

- [ ] **Step 5: Commit**

Commit message: `fix(relay): type all lambda delivery ambiguity`

### Task 3: Packaged source-byte cross-binding

**Files:**
- Modify: `tests/test_trusted_lambda_packaging.py`
- Modify: `stacks/trusted_lambda_asset.py`

**Interfaces:**
- Consumes: manifest `files`, manifest `sourceFiles`, repository source inventory, and actual asset inventory.
- Produces: a resolver proof that every reviewed source path, digest, and size is identical in the actual packaged payload, including catalog plus exactly 20 schemas.

- [ ] **Step 1: Write substitution and removal tests**

Add a test helper that rebaselines only `files`, `SHA256SUMS`, `payloadBytes`, and `ASSET.sha256`. Prove the current resolver wrongly accepts a substituted `capabilities/gateway.py`, a removed catalog, and a removed schema when `sourceFiles` remains unchanged.

- [ ] **Step 2: Run tests to verify RED**

Run: `AWS_REGION=eu-west-1 AWS_DEFAULT_REGION=eu-west-1 uv run --with-requirements requirements.txt --with pytest python -m pytest -q tests/test_trusted_lambda_packaging.py`

Expected: all three hostile assets are accepted, so the rejection assertions fail.

- [ ] **Step 3: Cross-bind source and payload inventories**

Index validated `files` by path and require every validated `sourceFiles` row to match its actual row's `path`, `sha256`, and `size`. Check required handlers against actual paths, require the actual catalog, and require exactly the same 20 actual schema paths as the reviewed source inventory.

- [ ] **Step 4: Run focused packaging GREEN**

Run the RED command again and the trusted asset shell verifier tests.

Expected: all packaging tests pass and self-consistent metadata cannot authenticate substituted or removed reviewed bytes.

- [ ] **Step 5: Commit**

Commit message: `fix(packaging): bind reviewed source to payload`

### Task 4: Exact DynamoDB CMK runtime actions

**Files:**
- Modify: `tests/test_capability_stack.py`
- Modify: `stacks/capability_stack.py`

**Interfaces:**
- Consumes: local CDK customer-managed-key table `grant_read_write_data()` synthesis and AWS DynamoDB CMK documentation.
- Produces: exact `kms:GenerateDataKey*` and `kms:ReEncrypt*` runtime authority under the existing exact CMK, caller-account, and regional via-service conditions.

- [ ] **Step 1: Write a CDK reference behavior test**

Synthesize a minimal CMK-encrypted DynamoDB table, call `grant_read_write_data()` for a Lambda-like role, extract its KMS actions, and require the capability stack's constrained KMS statement to contain the same cryptographic data-plane set without `kms:CreateGrant`.

- [ ] **Step 2: Run tests to verify RED**

Run: `AWS_REGION=eu-west-1 AWS_DEFAULT_REGION=eu-west-1 uv run --with-requirements requirements.txt --with pytest python -m pytest -q tests/test_capability_stack.py`

Expected: production is missing `kms:ReEncrypt*` and uses `kms:GenerateDataKey` instead of the reference wildcard action.

- [ ] **Step 3: Apply the exact constrained action correction**

Replace `kms:GenerateDataKey` with `kms:GenerateDataKey*` and add `kms:ReEncrypt*`; preserve the existing exact key resource and `StringEquals` constraints. Do not grant `kms:CreateGrant` to the Lambda runtime role.

- [ ] **Step 4: Run focused CDK GREEN and isolated nag**

Run the focused stack tests and isolated `AwsSolutionsChecks` synthesis.

Expected: tests pass and `NONCOMPLIANT 0` remains true.

- [ ] **Step 5: Commit**

Commit message: `fix(iam): complete constrained dynamodb cmk actions`

### Task 5: Verification and review closure

**Files:**
- Modify: `.superpowers/sdd/v1-task-2-report.md`
- Modify: `.superpowers/sdd/v1-task-2-fix-review-report.md`

**Interfaces:**
- Consumes: RED/GREEN evidence and exact final Git identity.
- Produces: a review package with every Important finding and CMK minor resolved.

- [ ] **Step 1: Run focused and complete verification**

Run the complete Node 24 suite, complete Python aggregate, release boundaries, E2E session-control tests, targeted Ruff/Black, `bash -n`, `compileall`, `git diff --check`, direct credential scan, and isolated CDK nag synthesis.

- [ ] **Step 2: Update both evidence reports**

Record the exact RED failure counts, GREEN counts, official AWS DynamoDB/KMS source rationale, commits, scope boundary, and no-deployment statement. Change the independent review verdict only after every proof is fresh.

- [ ] **Step 3: Commit and verify exact head**

Commit message: `docs(evidence): approve task 2 independent fixes`

Run the complete Node and Python aggregates at that report commit and require a clean worktree.
