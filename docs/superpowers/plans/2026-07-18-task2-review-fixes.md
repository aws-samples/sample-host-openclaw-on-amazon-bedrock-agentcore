# Task 2 Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILLS: use
> `superpowers:receiving-code-review`, `superpowers:systematic-debugging`,
> `superpowers:test-driven-development`, and
> `superpowers:verification-before-completion` while executing this plan.

**Goal:** Close the Task 2 hostile-review findings by isolating replay state per
tenant and grant, fencing ambiguous mutations across fresh tool-use IDs,
bounding read retries, enforcing release/catalog parity before runtime child
construction, and packaging a real disabled-adapter production gateway backed
by strong DynamoDB reads and durable state.

**Architecture:** Derive a stable tenant binding from the exact authenticated
grant and a stable logical operation key from tenant, invocation, operation,
tool, and argument digest. Persist both before returning cached state, and use
the logical key as the no-redispatch fence for ambiguous mutations. The bridge
keeps the same rules locally and only resubmits a validated retryable read
envelope. The runtime validates immutable release metadata before loading
plugins or spawning children. The Lambda asset carries the canonical catalog
and all schemas; cold start recompiles and verifies them before constructing a
gateway with strong-read authority records, durable tenant-scoped ledger state,
and no enabled adapters.

**Constraints:** No deploy, push, AWS/provider/browser calls, credentials,
messages, or real effects. Every behavior change follows RED -> GREEN and lands
in a separate focused commit. Exact aggregate evidence is appended to the Task
2 report before completion.

---

### Task 1: Isolate ledger state and fence logical effects

**Files:**

- Modify: `lambda/capabilities/ledger.py`
- Modify: `lambda/capabilities/gateway.py`
- Modify: `lambda/capabilities/test_gateway.py`

- [ ] Add failing tests proving identical call identities for two authenticated
  tenants cannot share cached results, quotas, target claims, or tool-use IDs.
- [ ] Add failing tests proving a lost mutation completion becomes durable
  `UNCERTAIN` and a fresh tool-use ID cannot redispatch the logical effect.
- [ ] Add failing tests proving read retry is same-call-only, bounded to one
  retry, and does not recharge target or turn budgets.
- [ ] Persist exact tenant/grant binding and reject mismatches before cache
  lookup; derive and persist a stable logical operation key.
- [ ] Run focused gateway tests and commit the ledger/gateway fix.

### Task 2: Bound relay retry and map ambiguous mutation delivery

**Files:**

- Modify: `bridge/capability-relay.js`
- Modify: `bridge/capability-relay.test.js`

- [ ] Add failing tests for post-send mutation loss, fresh-tool logical fences,
  one exact-envelope read retry, retry exhaustion, and fresh-tool bypass.
- [ ] Return typed `UNCERTAIN`/`RECONCILE_ONLY` for ambiguous mutation delivery.
- [ ] Permit only the same tool use to resubmit the byte-identical envelope
  after a validated `FAILED_RETRYABLE` read result, with two total attempts.
- [ ] Run focused Node 24 relay tests and commit the relay fix.

### Task 3: Enforce release/catalog parity before runtime construction

**Files:**

- Modify: `bridge/capability-catalog.js`
- Modify: `bridge/capability-catalog.test.js`
- Modify: `bridge/agentcore-contract.js`
- Modify: `bridge/agentcore-contract.test.js`
- Modify: `bridge/Dockerfile`

- [ ] Add failing tests showing release or catalog mismatch aborts before the
  OpenClaw plugin is loaded, a listener starts, or any child is spawned.
- [ ] Load immutable image release metadata and verify the compiled catalog at
  the first parent startup boundary.
- [ ] Require Docker build arguments for the exact release and catalog digest
  and verify them during image construction.
- [ ] Run focused Node 24 and Docker static tests and commit the startup fix.

### Task 4: Package and compose the production gateway

**Files:**

- Create: `lambda/capabilities/durable.py`
- Create: `lambda/capabilities/composition.py`
- Create: `lambda/capabilities/test_composition.py`
- Modify: `lambda/capabilities/gateway.py`
- Modify: `scripts/build-trusted-lambda-asset.sh`
- Modify: `stacks/trusted_lambda_asset.py`
- Modify: `stacks/capability_stack.py`
- Modify: `app.py`
- Modify: relevant asset and CDK tests

- [ ] Add failing offline tests for packaged catalog/schema inventory, cold
  start drift rejection, authenticated handler composition, strong reads,
  durable tenant-scoped ledger calls, and disabled adapters.
- [ ] Package the exact catalog source and all 20 schemas in the authenticated
  Lambda manifest and verify their bytes at cold start.
- [ ] Implement a production DynamoDB authority repository using consistent
  reads and a durable tenant/logical-key ledger with conditional writes.
- [ ] Synthesize one exact encrypted state table and grant only the required
  table and KMS actions; bind exact release, catalog digest, caller, and table
  values into Lambda environment.
- [ ] Run focused Python/CDK/asset tests and commit the production composition.

### Task 5: Aggregate verification and handoff

- [ ] Run the full Node 24 bridge suite, aggregate Python suite, direct secret
  scan, syntax compilation, CDK synthesis/nag checks, and `git diff --check`.
- [ ] Append exact RED/GREEN commands, outputs, commit IDs, and remaining limits
  to `.superpowers/sdd/v1-task-2-report.md`.
- [ ] Verify the worktree is clean and report the ordered commit stack for
  integration and independent re-review.
