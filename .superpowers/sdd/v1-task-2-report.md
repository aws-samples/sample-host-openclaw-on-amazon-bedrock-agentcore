# Task 2 report: immutable catalog, trusted relay, and admission gateway

## Scope and starting point

- Worktree: `/private/tmp/personal-operator-v1-capabilities`
- Branch: `codex/po-v1-capabilities`
- Exact start: `7e44b6684ac0cf4965ce734664ba13b60fdb7a59`
- No deployment, push, provider call, browser call, real message, or AWS call
  was performed.

## Baseline

Command:

```text
cd bridge && node --test --test-concurrency=1 runtime-policy.test.js lightweight-agent.test.js plugins/personal-operator/index.test.js invocation-handler.test.js agentcore-runtime-guard.test.js
```

Exact summary:

```text
1..16
# tests 76
# pass 76
# fail 0
```

The selected Python contract/IAM baseline also passed `24/24`.

## Commit 1: immutable JavaScript catalog and registry parity

Commit: `eda2553 feat(capabilities): freeze immutable tool catalog`

### RED

The first catalog/parity run failed `11/60`: the loader and reviewed runtime
artifacts did not exist, the registries exposed only four tools, and the six
new tools had no disabled-adapter boundary. A hostile same-byte schema symlink
was initially accepted (`1` focused failure), and the bridge-image parity
check found no catalog/schema artifacts (`1` focused failure).

### GREEN

```text
cd bridge && node --test --test-concurrency=1 capability-catalog.test.js runtime-policy.test.js lightweight-agent.test.js plugins/personal-operator/index.test.js
1..12
# tests 60
# pass 60
# fail 0
```

The loader now rejects symlinks and verifies the exact adjacent reviewed
catalog plus 20 schema artifacts. Full plugin, lightweight/warm-up, runtime
policy, catalog, and image inventories are byte/identity-parity checked.
Repository boundary assertions passed `3/3`.

## Commit 2: trusted per-turn relay

Commit: `af28644 feat(capabilities): add trusted bridge relay`

### RED

The initial relay/lifecycle run failed `11/76`: there was no relay, parent
turn binding, loopback adapter, gateway transport, or server tool-use identity.
A later rendering RED failed both model-facing paths because they returned raw
objects rather than native tool-result text.

### GREEN

```text
cd bridge && node --test --test-concurrency=1 capability-relay.test.js plugins/personal-operator/index.test.js lightweight-agent.test.js invocation-handler.test.js agentcore-contract-lifecycle.test.js
1..13
# tests 76
# pass 76
# fail 0
```

The parent retains the typed grant only around model execution. The child gets
only a literal loopback relay address; grants, nonces, gateway ARN, and other
trusted authority do not enter its environment, model input, arguments,
results, workspace, or logs. The broader bridge suite excluding only the
Node-22-incompatible SQLite test passed `319/319`; final aggregate evidence
below uses the required Node 24 runtime and includes SQLite.

## Commit 3: Python admission gateway and replay ledger

Commit: `1a0c780 feat(capabilities): add admission gateway ledger`

### RED

```text
PYTHONPATH=lambda .venv/bin/python -m pytest -q lambda/capabilities/test_gateway.py
FFFFFFFFFFFFFFFFFFFFFFF                                                  [100%]
23 failed in 0.50s
```

Every failure was the expected absent admission, ledger, or gateway module.

### GREEN

```text
PYTHONPATH=lambda .venv/bin/python -m pytest -q lambda/capabilities/test_gateway.py lambda/capabilities/test_contracts.py
167 passed in 3.65s
```

Admission checks the exact IAM caller, release/catalog/call identity, trusted
clock, grant expiry, pack/operation/input quota, and strong-read live deletion,
global kill, user, session, runtime, installation, pack kill, and exact target
state. It rechecks deletion at the last application-controlled point before
adapter dispatch. The atomic ledger binds nonce, tool-use identity, turn and
pack budgets, exact replay, safe read retry, and non-replayable `UNCERTAIN`
mutation results. Production adapters and ambient data clients remain absent;
the packaged Lambda entry point is explicitly fail closed.

## Commit 4: CDK, IAM, packaging, and browser tombstone

This report is included in the fourth requested commit.

### RED

The initial focused synth run failed `6/6`: the capability stack was absent,
AgentCore accepted no gateway ARN, the runtime still contained an optional
Browser resource/IAM path, and the expected exact invoke statements were not
present.

### GREEN

```text
PYTHONPATH=lambda .venv/bin/python -m pytest -q tests/test_capability_stack.py tests/test_product_configuration.py tests/test_trusted_lambda_packaging.py
61 passed in 21.37s
```

The new stack packages one ARM64 Python 3.13 fail-closed gateway. Its role has
only `logs:CreateLogStream` and `logs:PutLogEvents` against the one exact log
group. The runtime role may invoke the existing exact workspace broker and the
new exact gateway ARN; it receives no provider, browser, scheduler, DynamoDB,
Secrets Manager, MCP, or adapter authority. `enable_browser=true` is rejected;
Task 10 must introduce a separate trusted Browser Gateway. The handler is now
required by local tests, source/asset manifests, offline import verification,
and the canonical phase-1 deploy list.

An isolated `AwsSolutionsChecks` synthesis produced only compliant or
evidence-suppressed findings: IAM4 compliant, IAM5 compliant/suppressed only
for the exact log-stream suffix, and L1 suppressed for Python 3.13.

## Final verification

The first full Node 24 aggregate exposed one stale Commit-1 assertion that
still expected a raw model-facing result after Commit 2 intentionally wrapped
results in native `content/details`. The assertion was aligned with the already
focused-tested boundary. The complete suite then passed:

```text
cd bridge && PATH=/opt/homebrew/opt/node@24/bin:$PATH npm test
tests 328
suites 57
pass 328
fail 0
duration_ms 13273.8455
```

The Python aggregate's first run passed `1118` tests and had one environment
prerequisite failure because this isolated worktree did not yet contain the
generated `web/dist/index.html`. Building the web asset from its lockfile made
that existing app-synth test pass. The complete aggregate then passed:

```text
AWS_REGION=eu-west-1 AWS_DEFAULT_REGION=eu-west-1 \
PYTHONPATH=lambda/router:lambda .venv/bin/python -m pytest -q \
  lambda/router lambda/worker lambda/workflows lambda/actions \
  lambda/capabilities lambda/control lambda/web lambda/cron \
  lambda/workspace_broker tests/test_capability_stack.py \
  tests/test_product_configuration.py \
  tests/test_telegram_queue_infrastructure.py tests/test_deploy_safety.py \
  tests/test_trusted_lambda_packaging.py \
  tests/test_verify_agentcore_storage.py tests/test_web_stack.py \
  tests/security tests/integration
1119 passed, 10 subtests passed in 86.60s
```

A direct forbidden-authority search over the relay, gateway, ledger, admission,
catalog loader, and capability stack was clean. Three redundant credential and
IAM boundary tests passed `3/3`. `compileall` and `git diff --check` also passed.

No authoritative external, deployment, provider, browser, message, AWS, push,
or production-readiness claim is made by this report.

## Independent-review remediation

The hostile review identified four release-blocking gaps in the original
implementation: replay state was not tenant-isolated, mutation transport loss
could be replayed under a fresh tool-use ID, the bridge could listen before
release/catalog parity was proved, and the packaged Lambda still used a
fail-closed placeholder rather than the durable gateway composition. The
remediation was kept inside Task 2; no adapter, provider, browser, scheduler,
secret, or deployment path was added.

Commits:

- `47f5a3f docs(plan): map task 2 review fixes`
- `d327745 fix(capabilities): isolate replay and fence effects`
- `9666cd2 fix(relay): bound retries and fence ambiguous effects`
- `2f1934e fix(runtime): gate startup on release catalog parity`
- `aca6635 fix(capabilities): compose durable packaged gateway`
- `ce6c87e test(capabilities): isolate sdk imports in aggregate suite`

### Review RED evidence

- Four hostile ledger/gateway tests failed before tenant-scoped authority
  keys, full grant binding, stable logical-call fencing, and conservative
  in-flight/uncertain mutation handling existed.
- Three relay retry/fencing tests failed initially; a fourth failed when the
  fresh-tool-use-ID bypass case was made explicit.
- Five startup tests failed before release metadata was authenticated and the
  parent delayed child construction and listener binding until the gate
  passed.
- Four CDK/packaging tests failed before the exact DynamoDB table, CMK, IAM,
  release environment, and authenticated source set existed.
- Three production-composition tests failed before the real Dynamo repository,
  durable ledger, and cold-start assembly existed.

### Review GREEN result

The final boundary now derives tenant identity from the authenticated
session/runtime/release/catalog tuple, binds the complete grant before cache
or dispatch, and scopes turn, call, tool-use, and stable logical-operation
records to that tenant. Reads receive at most one same-call retry without
budget recharge. Mutations are fenced across fresh tool-use IDs; ambiguous
post-dispatch completion is a typed `UNCERTAIN`/reconcile-only result and is
never automatically replayed.

The bridge image contains authenticated release metadata and refuses startup
on a commit or catalog mismatch before constructing the child or opening a
listener. The packaged Lambda now compiles the reviewed catalog, constructs
the strongly consistent DynamoDB-backed admission repository and durable
ledger, and exposes no production adapters. Configuration or storage failure
remains typed and fail closed.

The stack provisions one deletion-protected, retained, point-in-time-recovery,
pay-per-request DynamoDB table encrypted by one customer-managed key. The
gateway role is limited to exact log, table, and key resources; DynamoDB calls
are limited to `GetItem`, `PutItem`, `UpdateItem`, and
`TransactWriteItems`, and KMS use is constrained through DynamoDB in
`eu-west-1` for the deployment account. Isolated `AwsSolutionsChecks`
synthesis reported `NONCOMPLIANT 0`.

Focused final evidence included:

```text
lambda/capabilities
176 passed in 2.78s

tests/test_capability_stack.py tests/test_trusted_lambda_packaging.py
21 passed in 6.37s

tests/security/test_release_boundaries.py
13 passed in 1.38s

tests/e2e/test_session_control.py
11 passed in 0.09s
```

The exact review-remediated code candidate was
`ce6c87e4413b2ae8a251d691b6d9a52611aca67d`, tree
`6804a22c3456a249c08b3740d11c53b1f9c5b7f6`. Its complete Python aggregate
passed `1131` tests plus `10` subtests in `111.45s`; the complete Node 24
aggregate passed `338/338`. Ruff, Black, `bash -n`, `compileall`,
`git diff --check`, authenticated asset parity, and a direct credential scan
were clean.

No deployment, provider invocation, browser action, real message, AWS call,
push, or cloud mutation was performed during review remediation.

## Independent remediation-review closure

The independent review of `eef24e5` found three Important blockers and one
CMK-permission minor. The follow-up remained inside Task 2 and is implemented
by:

- `7fdba8e docs(plan): close task 2 independent review`
- `617388f fix(capabilities): admit claimed target replay`
- `16e4102 fix(relay): type all lambda delivery ambiguity`
- `480c333 fix(packaging): bind reviewed source to payload`
- `50e29e6 fix(iam): complete constrained dynamodb cmk actions`

### Durable target replay and retry

Three production-composition regressions failed before the change. With a
real `DynamoAdmissionRepository`, real `DynamoCapabilityLedger`, and
`maxUses=1`, the first read succeeded but exact cached replay and the one
allowed same-call retry were denied as `TARGET_GRANT_EXHAUSTED`.

`LiveTargetGrant` now carries a canonical sorted tuple of exact
`call_<64 lowercase hex>` identities, requires that its use count equal that
inventory, and rejects use counts beyond the grant. The Dynamo repository
preserves the validated `claimedCallIds`. Admission exempts only the exact
already-claimed call from aggregate exhaustion, allowing the ledger to return
its cached result or one same-call read retry. Fresh calls remain denied. The
hostile durable tests also prove one target use, one turn/pack budget charge,
two adapter calls at most, retry exhaustion, and rejection under a different
exact grant binding.

RED: `3 failed, 4 passed`. Focused GREEN with the gateway suite:
`35 passed`.

### Typed Lambda ambiguity

Three real-transport tests initially failed because Lambda `FunctionError`,
missing payload, and malformed payload errors escaped as
`CapabilityRelayError`. A second RED (`2 failed, 13 passed`) proved that a
schema-invalid or sensitive post-dispatch response could also escape.

The relay now treats every exception after invoking the gateway transport,
including result-contract validation failure, as delivery-ambiguous. Reads
receive a validated `FAILED_RETRYABLE/SAFE_RETRY` result; mutations receive a
validated `UNCERTAIN/RECONCILE_ONLY` result. Constructor, grant, argument,
quota, and local-admission errors remain outside that boundary and still fail
before transport. Node 24 focused GREEN: `15/15` relay tests.

### Reviewed source to packaged payload binding

Three hostile packaging tests initially rebaselined `files`, `SHA256SUMS`,
`MANIFEST.json`, and `ASSET.sha256` while leaving `sourceFiles` unchanged. The
old resolver accepted a substituted `capabilities/gateway.py`, a removed
catalog, and a removed schema (`3 failed, 15 passed`).

The CDK resolver now requires every reviewed source path, digest, and size to
equal its actual packaged row. Required handlers are checked against the
actual payload, and the actual capability inventory must contain the reviewed
catalog plus exactly the same 20 schema paths. Focused GREEN: `18/18`.

### DynamoDB CMK action evidence

The local CDK `Table.grant_read_write_data()` reference synthesis proved the
exact cryptographic data-plane set includes `kms:GenerateDataKey*` and
`kms:ReEncrypt*`; the initial comparison failed `1/7`. AWS's DynamoDB
documentation likewise lists `Encrypt`, `Decrypt`, both re-encrypt directions,
both data-key variants, and `DescribeKey` among the required customer-managed
key permissions. `CreateGrant` is used by the identity selecting/configuring
the table key and was not added to the Lambda runtime role. Sources:
[DynamoDB encryption usage notes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/encryption.usagenotes.html),
[KMS permissions reference](https://docs.aws.amazon.com/kms/latest/developerguide/kms-api-permissions-reference.html),
and the
[AWS CDK DynamoDB table source](https://github.com/aws/aws-cdk/blob/main/packages/aws-cdk-lib/aws-dynamodb/lib/table.ts).

The gateway role now has `kms:GenerateDataKey*` and `kms:ReEncrypt*` under the
same exact CMK resource, caller account, regional DynamoDB `ViaService`, and no
`CreateGrant`. The first nag run correctly reported two IAM5 action-wildcard
findings; evidence-scoped suppressions for only those two action families
reduced the isolated report to `NONCOMPLIANT=0`. Focused CDK GREEN: `7/7`.

### Final code-candidate evidence

The reviewed implementation candidate was
`50e29e66b1bbc09d125c11b3b58a7b5a4215fada`, tree
`654a914d31394585752426169a7dc83324c82439`. Fresh gates at that code head:

```text
capabilities + capability stack + trusted packaging
204 passed in 28.07s

focused Node 24 capability/runtime boundary
105 passed; 0 failed

release boundaries
13 passed

session-control E2E
11 passed

complete Node 24 aggregate
343 passed; 58 suites; 0 failed; duration_ms 69867.842542

complete Python aggregate
1138 passed, 10 subtests passed in 88.95s

isolated AwsSolutionsChecks
NONCOMPLIANT=0
```

Targeted Ruff and Black, `bash -n`, `compileall`, `git diff --check`, and the
direct credential scan were clean. No deployment, push, AWS call, provider
invocation, product-browser action, real message, credential access, or cloud
mutation was performed. The only external read was the approved lookup of the
three primary AWS/CDK documentation sources cited above.
