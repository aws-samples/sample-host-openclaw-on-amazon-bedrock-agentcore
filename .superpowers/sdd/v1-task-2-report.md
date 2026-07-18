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
