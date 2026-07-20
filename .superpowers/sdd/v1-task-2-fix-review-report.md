# Task 2 remediation review

**Verdict: Approved after fixes.**

Reviewed source head:
`eef24e505c8680ceb719274daa97a8107d282dbc`

Reviewed tree:
`1134155d8a3d44e8a1f8eee1ec2f2648b415b91c`

Review base:
`d7cd5ed06f55eb3b3be881f059dcb7a0fc5adf28`

## Spec Compliance

The remediation closes the two original safety-critical failures: replay state
is scoped by an authenticated tenant and exact grant binding, and a mutation's
logical identity is durably fenced across fresh tool-use IDs before a second
adapter dispatch. It also places release/catalog validation before runtime
construction, packages a real strong-read DynamoDB composition with no enabled
adapters, and keeps the conversational runtime limited to exact Lambda invoke
authority.

Task 2 is not yet approvable because the actual durable target-grant path makes
same-call read replay/retry fail after its first claimed use, one post-send
Lambda error family still escapes instead of becoming a typed result, and the
CDK asset resolver does not prove that packaged source bytes equal the reviewed
source inventory. These are direct regressions against the original Important
findings and remediation plan.

## Verified closure evidence

- Tenant/grant isolation: `lambda/capabilities/ledger.py:154-185` namespaces
  entries, turns, and tool-use state by `derive_tenant_binding(grant)` and
  rejects a different exact grant before cache return. The Dynamo implementation
  uses the tenant binding as its partition key and repeats those checks at
  `lambda/capabilities/durable.py:312-346`. Existing in-memory and durable
  Cartesian probes passed.
- Stable mutation fence: the durable transaction writes the call, tool-use,
  budget, and logical-effect records together at
  `lambda/capabilities/durable.py:461-497`; a fresh tool-use ID for the same
  mutation returns `LOGICAL_FENCE` at lines 387-394 and invokes no adapter. The
  relay adds the same in-process fence at
  `bridge/capability-relay.js:635-649`.
- Bounded ordinary read retry: the relay retains the exact frozen envelope and
  caps it at two attempts at `bridge/capability-relay.js:604-631,690-734`; both
  in-memory and Dynamo ledgers cap attempts at two without adding another turn
  or pack budget charge.
- Startup parity: `bridge/agentcore-contract.js:143-147,245-275,1577-1590`
  validates image-owned release metadata before requiring the lightweight
  runtime or listening on the contract port. `bridge/Dockerfile:92-105`
  requires and verifies the release/catalog build inputs.
- Production composition: `lambda/capabilities/composition.py:37-56,84-121`
  recompiles the packaged catalog at cold start, constructs strongly consistent
  Dynamo authority/ledger implementations, and supplies `adapters={}`.
  `lambda/capabilities/durable.py:84-90,231-238` sets `ConsistentRead=True` on
  authority and ledger reads.
- Authority boundary: `stacks/capability_stack.py:110-145` limits the gateway
  role to its exact log group, state table, and CMK conditions;
  `stacks/agentcore_stack.py:182-193` gives the runtime exact invoke statements
  for the workspace broker and capability gateway, without direct capability
  table/KMS/provider/browser/scheduler authority.

Fresh focused commands at the frozen head passed:

```text
AWS_REGION=eu-west-1 AWS_DEFAULT_REGION=eu-west-1 PYTHONPATH=lambda \
  /Users/konstantin.tuzikov/Documents/personal-operator/.venv/bin/python \
  -m pytest -q lambda/capabilities tests/test_capability_stack.py \
  tests/test_trusted_lambda_packaging.py
197 passed in 9.80s
```

```text
PATH=/opt/homebrew/opt/node@24/bin:$PATH node --test \
  --test-concurrency=1 bridge/capability-catalog.test.js \
  bridge/capability-relay.test.js bridge/capability-startup.test.js \
  bridge/runtime-policy.test.js bridge/lightweight-agent.test.js \
  bridge/plugins/personal-operator/index.test.js \
  bridge/invocation-handler.test.js bridge/agentcore-runtime-guard.test.js
tests 100; pass 100; fail 0
```

`git diff --check d7cd5ed..eef24e5` also passed. The worktree was clean before
this review report was created.

## Findings

### Critical

None. I reproduced no remaining cross-tenant cache return or duplicate mutation
dispatch in the reviewed paths.

### Important

1. **The production target-grant repository makes exact replay and the one
   allowed same-call retry fail after the first dispatch.**
   `DynamoAdmissionRepository.strong_read_target_grant()` validates
   `claimedCallIds` but discards it when constructing `LiveTargetGrant`
   (`lambda/capabilities/durable.py:135-151`). Admission then rejects any record
   whose aggregate `uses` reached `maxUses` at
   `lambda/capabilities/admission.py:327-335`, before either the replay ledger or
   the idempotent `claim_target_use()` check can see that this exact `callId`
   already owns the use. The in-memory test double masks the defect because its
   `LiveTargetGrant.uses` value never increases. A hostile test against the
   actual Dynamo repository/ledger with `maxUses=1` produced:

   ```text
   first=SUCCEEDED replay=DENIED
   replayError=TARGET_GRANT_EXHAUSTED adapterCalls=1
   claimedCallIds=[the exact first callId] uses=1
   ```

   The same ordering prevents a target-granted `FAILED_RETRYABLE` read from
   reaching its one allowed exact-envelope retry. Preserve and validate claimed
   call IDs in live target state, admit the already-claimed exact call before
   the exhaustion test, and add durable gateway tests for cached replay,
   response-loss recovery, retry success, retry exhaustion, and no additional
   target-use or budget charge.

2. **Post-send Lambda `FunctionError`, missing-payload, and invalid-response
   failures still escape as exceptions instead of typed read/mutation results.**
   `createLambdaGatewayTransport()` converts those post-invocation conditions
   into `CapabilityRelayError` at `bridge/capability-relay.js:999-1005`, but the
   relay explicitly rethrows that class at lines 703-713. A hostile mutation
   probe observed the first call reject with
   `CAPABILITY_GATEWAY_FAILED`; only a later fresh-tool call returned
   `UNCERTAIN`, and the adapter transport count remained one. The no-redispatch
   fence therefore works, but the original call still violates the required
   bounded typed-result contract, and a read cannot take its exact retry path.
   Classify all errors after transport invocation according to operation risk
   (`FAILED_RETRYABLE` for reads, `UNCERTAIN/RECONCILE_ONLY` for mutations), or
   give transport errors an explicit trusted pre-send/post-send classification.
   Add tests through the real `createLambdaGatewayTransport()` boundary for
   `FunctionError`, absent payload, malformed payload, and SDK rejection.

3. **The CDK trusted-asset resolver authenticates a self-consistent manifest but
   does not bind packaged source bytes to the reviewed `sourceFiles` bytes.**
   `stacks/trusted_lambda_asset.py:244-271` independently checks `files` against
   the asset and `sourceFiles` against the repository, but never requires the
   matching path/digest/size rows to equal one another. `_REQUIRED_HANDLERS` is
   also checked against `sourceFiles`, not the actual payload. I replaced the
   packaged `capabilities/gateway.py`, recomputed `files`, `SHA256SUMS`,
   `MANIFEST.json`, and `ASSET.sha256`, left the reviewed `sourceFiles` row
   unchanged, and `resolve_trusted_lambda_asset(..., account=<real account>)`
   accepted the substituted gateway. The build script's normal build/verify
   path performs the missing cross-check, but direct CDK synthesis relies on
   this resolver as its authentication gate. Require every `sourceFiles` row to
   have an identical path/digest/size row in actual `files`, require the catalog
   and all 20 schemas in actual payload inventory, and add behavior tests for a
   removed catalog/schema and substituted gateway with self-consistent metadata.

### Minor

1. `stacks/capability_stack.py:130-145` grants exact
   `kms:GenerateDataKey` and no `kms:ReEncrypt*`; the local CDK
   `Table.grant_read_write_data()` reference synthesis for a customer-managed
   DynamoDB key emits `kms:GenerateDataKey*` and `kms:ReEncrypt*` in addition to
   Encrypt/Decrypt/DescribeKey. Before deployment, validate the manually
   narrowed set against DynamoDB's required CMK data-plane operations and add a
   behavior/evidence test. Keep the existing exact key, account, region, and
   `kms:ViaService` constraints.

## Remediation verification

The findings above describe the frozen `eef24e5` review state. The follow-up
implementation candidate is
`50e29e66b1bbc09d125c11b3b58a7b5a4215fada`, tree
`654a914d31394585752426169a7dc83324c82439`.

1. Durable target state now preserves and validates exact claimed call IDs.
   Aggregate exhaustion exempts only the exact already-claimed call, so cached
   replay and one same-call read retry reach the tenant/grant-bound ledger while
   a fresh call remains denied. Hostile tests prove response-loss recovery,
   success and exhaustion paths, one target use, one budget charge, at most two
   adapter calls, and no different-grant cache return. RED: `3 failed`;
   focused GREEN with gateway coverage: `35 passed`.
2. The invoked gateway-transport boundary now converts Lambda
   `FunctionError`, absent payload, malformed JSON, schema-invalid payload,
   sensitive/mismatched result, and SDK rejection into validated typed results
   selected by operation risk. No raw `CapabilityRelayError` escapes after
   possible dispatch. RED: `3 failed`, then `2 failed` for the expanded
   post-dispatch contract; Node 24 relay GREEN: `15/15`.
3. The CDK resolver cross-binds every reviewed `sourceFiles` path/digest/size
   row to actual `files`, checks handlers against actual payload, and requires
   the actual reviewed catalog and exact 20-schema inventory. Substituted
   gateway, removed catalog, and removed schema all failed before the fix and
   are now rejected. RED: `3 failed`; GREEN: `18/18`.
4. A local CDK customer-managed DynamoDB reference and AWS primary
   documentation support `kms:GenerateDataKey*` and `kms:ReEncrypt*`. The
   runtime role now has those exact action families under the existing exact
   CMK, caller-account, and regional DynamoDB via-service conditions, without
   `CreateGrant`. CDK RED: `1 failed`; GREEN: `7/7`. Evidence-scoped nag
   suppressions cover only the two required action wildcards, and isolated
   `AwsSolutionsChecks` reports `NONCOMPLIANT=0`.

Fresh candidate gates:

```text
focused Python capability/CDK/packaging: 204 passed
focused Node 24 boundary: 105 passed, 0 failed
release boundary: 13 passed
session-control E2E: 11 passed
complete Node 24: 343 passed, 58 suites, 0 failed
complete Python: 1138 passed, 10 subtests passed
AwsSolutionsChecks: NONCOMPLIANT=0
```

Official CMK evidence:

- https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/encryption.usagenotes.html
- https://docs.aws.amazon.com/kms/latest/developerguide/kms-api-permissions-reference.html
- https://github.com/aws/aws-cdk/blob/main/packages/aws-cdk-lib/aws-dynamodb/lib/table.ts

Targeted Ruff/Black, shell syntax, `compileall`, `git diff --check`, and the
direct credential scan were clean. No deployment, push, AWS/provider/product
browser invocation, real message, credential access, or cloud mutation was
performed.

## Final assessment

**Approved after fixes.** The two original Critical findings remain closed;
all three Important follow-up findings and the CMK minor now have hostile
behavioral regressions and fresh passing evidence. The gateway remains fail
closed with production adapters disabled.
