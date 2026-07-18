# Task 4 report — immutable staging release foundation

## Scope and safety

- Worktree: `/private/tmp/personal-operator-v1-infra`
- Branch: `codex/po-v1-infra`
- Exact start: `7e44b6684ac0cf4965ce734664ba13b60fdb7a59`
- Local-only implementation. No AWS/CDK deploy, Docker push, signing job,
  changeset, credential discovery, or Git push was performed.

## 4.0 — exact CDK surface

- RED: `/Users/konstantin.tuzikov/Documents/personal-operator/.venv/bin/python -m pytest -q release_tools/test_cdk_surface.py`
  failed `1 failed, 1 passed`: `requirements.txt` still allowed a version range.
- GREEN: the same focused test plus `git diff --check` passed `2 passed`.
- Commit: `eb30304 chore(cdk): pin release surface to 2.261.0`
- Result: `aws-cdk-lib==2.261.0` is exact, and tests exercise the required
  AgentCore Runtime/Endpoint L1 constructor and nested property names.

## 4.1 — strict AWS-free release contracts

- RED 1: focused contract collection failed because `release_tools.contracts`
  did not exist.
- GREEN 1: `release_tools/test_contracts.py` reached `23 passed`; one genuine
  implementation defect in slotted dataclass serialization was exposed and
  corrected during the cycle.
- RED 2: `-k revalidates` failed because a dataclass reconstructed with an
  invalid region could reach persistence without semantic revalidation.
- GREEN 2: contract + CDK-surface tests, Python compilation, and
  `git diff --check` passed `26 passed`.
- Result: four frozen strict contracts reject duplicates, extras,
  noncanonical bytes, nonfinite values, cross-account/region/repository
  bindings, mutable image references, drifted endpoint/version identity,
  malformed inventories, and illegal initial transaction evidence. New
  artifacts use atomic link publication and refuse clobbering.
- Commit: `5377c0c feat(release): freeze immutable release contracts`

## 4.2 — durable transaction journal

- RED: `python -m pytest -q release_tools/test_transaction.py` failed during
  collection because the journal module did not exist.
- GREEN: transaction tests passed `9 passed`; the combined contract,
  transaction, and CDK-surface suite passed `35 passed`, followed by Python
  compilation and `git diff --check`.
- Result: write-ahead `UNCERTAIN` is fsynced before every injected mutation;
  only the single legal next state can run; ambiguous failures block resume;
  reconciliation is explicit; stale writers fail compare-and-swap; replacement
  fsyncs the payload and containing directory; rollback records require the
  exact commit/account/region/digest-bound reference.
- Commit: `9469853 feat(release): add durable staging journal`

## 4.3 — deterministic trusted Lambda ZIP v2

- RED 1: new focused packaging tests failed at collection because the ZIP v2
  builder did not exist.
- RED 2: the CDK handoff test failed because `app.py` still passed only a
  directory path and no authenticated archive hash.
- GREEN: packaging tests passed `18 passed`; the broader packaging/web/CDK
  configuration slice passed `93 passed`; Python compilation and Bash syntax
  passed; offline synthetic-account synthesis completed with seven cdk-nag
  reports and zero findings.
- Result: independent pure-Python builds are byte-identical; the archive has
  sorted fixed ZIP metadata and excludes all manifest files; the external v2
  manifest binds exact commit/tree, builder digest/ID, requirements, source and
  payload inventories, ARM64/Python, byte counts, and archive SHA-256. CDK
  receives the verified ZIP path and its custom asset hash for all six trusted
  Lambda consumers.
- Docker daemon: unavailable on this host. No image was pulled. The Docker
  Python 3.13/ARM64 double-build/import gate remains external; the local
  deterministic builder test ran without Docker.
- Integration note: exact hash propagation required small compatible optional
  parameters in `stacks/router_stack.py` and `stacks/web_stack.py`, beyond the
  brief's primary file list. Synthetic v0 synth keeps the prior source-path
  behavior when no authenticated hash exists.
- Commit: `0a242a2 feat(release): authenticate deterministic Lambda ZIP`

## 4.4 — injected ECR evidence adapter

- RED: focused ECR tests failed during collection because `release_tools.ecr`
  did not exist.
- GREEN: ECR tests reached `16 passed`; the ECR + contract + transaction slice
  passed `49 passed`, followed by Python compilation and `git diff --check`.
- Result: the adapter constructs no SDK session/client and accepts only an
  injected fake-compatible client. It cross-checks immutable repository
  settings, exact commit tag and digest lookups, completed zero-high/critical
  scan, one active SBOM and provenance OCI referrer, one exact
  repository-filtered signing rule, and one completed signature. Pagination,
  duplicates, timeouts, missing evidence, and unknown asynchronous statuses
  are fail-closed or explicitly ambiguous/incomplete.
- Commit: `0e92e4a feat(release): collect immutable ECR evidence`

## 4.5 — retained ECR and signing foundation

- RED: `python -m pytest -q release_tools/test_cdk_release.py` failed
  `3 failed`: there was no owned repository or signing configuration and the
  runtime pull policy still admitted three mutable toolkit repository prefixes.
- GREEN: the focused tests passed `3 passed`; the CDK-release, CDK-surface,
  and product-configuration slice passed, followed by Python compilation and
  `git diff --check`.
- Result: the foundation owns exactly one retained `personal-operator/bridge`
  repository with immutable tags, scan-on-push, KMS rotation, and a frozen
  untagged-image lifecycle. One retained Notation OCI profile feeds one
  repository-filtered automatic signing rule. Runtime pulls are limited to the
  exact repository ARN; only `ecr:GetAuthorizationToken` retains its
  unavoidable wildcard resource.
- Commit: `a316c31 feat(release): own immutable ECR foundation`

## 4.6 — direct immutable AgentCore L1 release

- RED 1: the expanded CDK release test failed because a commit plus exact
  digest was rejected unless the legacy externally provisioned runtime fields
  already existed; no Runtime or RuntimeEndpoint could be synthesized.
- GREEN 1: CDK release tests passed `6 passed`. Empty foundation context emits
  neither resource; exact release inputs emit one stable Runtime and one
  retained `release_<40-sha>` Endpoint, with the reviewed digest, VPC, role,
  environment, `/mnt/workspace`, lifecycle, and HTTP protocol frozen.
- RED 2: the AgentCore evidence tests failed at import because the injected
  adapter did not exist.
- GREEN 2: AgentCore + CDK release tests passed `24 passed`; the complete
  `release_tools`, product-configuration, and storage-verification slice passed
  `136 passed`, followed by Python compilation and `git diff --check`.
- Result: the mutable Starter Toolkit path is no longer part of the CDK model.
  A credential-free adapter accepts only an injected client, refuses endpoint
  name collisions and retargeting, distinguishes pending from ambiguous live
  evidence, rejects unknown/failed states, and emits RuntimeContextV3 only for
  the exact READY digest-bound runtime and endpoint. A synthetic-account
  offline application synth also completed without AWS calls.
- Commit: `a06d08c feat(release): own immutable AgentCore runtime`

## 4.7 — typed staging CLI and compatibility shim

- RED 1: `release_tools/test_cli.py` produced `7 failed, 1 passed` because the
  staging CLI did not exist.
- GREEN 1: CLI + journal + contract tests passed `41 passed`. Subprocess tests
  proved preflight/status never invoke the poisoned AWS executable, an exact
  confirmation precedes credential discovery, the expected STS account is
  checked immediately before dispatch, a failed dispatched phase remains
  `UNCERTAIN`, later phases and unreconciled resume are blocked, explicit
  absent reconciliation permits a safe retry, and rollback is write-ahead and
  limited to the exact VERIFIED transaction.
- RED 2: the canonical release-asset adapter test failed at import because
  `release_tools.release_assets` did not exist.
- GREEN 2: release-asset + deploy-safety tests passed `20 passed`; product
  configuration passed `41 passed`; the combined release/deploy/product/
  packaging/storage slice passed all `182` collected tests, plus Bash syntax,
  Python compilation, and `git diff --check`.
- Result: `scripts/deploy.sh` is now a 17-line exec-only compatibility shim.
  `scripts/staging-release.py` exposes credential-free `--preflight`, exact
  phases, `--resume`, `--status`, and verified-ID `--rollback`. The Python
  package alone owns identity checks, legal transitions, canonical driver
  evidence, durable ambiguity, reconciliation, and rollback. Both shell paths
  have lost their embedded RuntimeContext parser; offline synthesis consumes
  RuntimeContextV3 through the canonical contract adapter.
- Commit: `469bb61 feat(release): add typed staging CLI`

## 4.8 — aggregate gate and truthful release evidence

- RED 1: the revised security documentation/boundary slice failed `3 failed,
  15 passed`: neither document contained the exact locally-verified/not-deployed
  boundary, the seven external gates were not explicitly open, and the shared
  asset surface still said four handlers.
- GREEN 1: that slice passed `18 passed`. Operations and evidence now say
  `staging deployment path implemented and locally verified; not deployed`,
  enumerate every requested open gate, and record five unique handler modules
  across six Lambda functions.
- RED 2: the first aggregate run exposed two stale evidence assertions: the
  packaging test still expected build/deploy logic in the removed shell path,
  and exact dependency-inventory hashes had changed after the CDK pin.
- GREEN 2: the focused corrections passed `20 passed`; dependency inventory is
  again exact at 220 components. The full aggregate gate then passed: 1,039
  Python tests plus 10 subtests, 11 E2E session-control tests, 313 serialized
  bridge tests, 5 web tests, production build, JavaScript/Python syntax,
  whitespace, hermetic offline synth, and zero-finding cdk-nag.
- Result: `scripts/test-local.sh` now discovers `release_tools`; the offline
  release gate consumes RuntimeContextV3 through the canonical adapter. No AWS
  call, deploy, image push, signing job, changeset, runtime mutation, pilot, or
  Docker pull/run occurred. Docker remained unavailable, so its exact ARM64
  build/import gate stays explicitly open.
- Commit: `d4b8f97 docs(release): record local staging boundary`
- Exact post-commit verification: `scripts/test-local.sh` exited 0 on
  `d4b8f97`; the final lines were `PASS: CDK offline synthesis contract`,
  `PASS: CDK cdk-nag contract`, and `All local checks passed.`

## Independent-review remediation checkpoint

The first independent review returned `Needs fixes`. The following bounded
fixes are committed; CLI/live reconciliation and behavioral packaging-test
findings remain open at this checkpoint.

### Phase-owned reconciliation and rollback

RED: `release_tools/test_transaction.py` reported `6 failed, 9 passed`,
reproducing cross-phase evidence replacement, evidence supplied for an absent
outcome, and direct rollback completion before verified write-ahead intent.

GREEN at `4d8641a`: `15 passed`. Each uncertain phase now owns an exact
evidence field set, an absent outcome accepts none, prior stable fields cannot
be supplied or rewritten, and the direct `record_rollback` bypass is removed.

### Retained foundation and safe repository guidance

RED: the focused signing-configuration synthesis test failed because the
resource had no retain policy. GREEN at `09062a4`: `1 passed`; both deletion
and update-replace now retain the signing configuration. README links now name
the v1 sources, the package whitespace regression is removed, and `ff12a44`
replaces active mutable Starter Toolkit, direct Runtime update, `us-west-2`,
and unreviewed deployment guidance with the immutable v1 boundary and open
external gates.

### OCI attestation content

RED: `release_tools/test_ecr.py` reported `21 failed` because no manifest or
blob was retrieved. GREEN at `11f19a2`: the ECR adapter retrieves each exact
OCI artifact manifest, authenticates digest/size/media type and image subject,
retrieves and authenticates the sole bounded content layer, validates SPDX,
and binds in-toto/SLSA provenance to the exact commit, tree, build context,
builder identity, and reviewed builder inputs.

Fresh checkpoint evidence:

```text
.venv/bin/python -m pytest -q release_tools/test_ecr.py release_tools/test_contracts.py
45 passed
.venv/bin/python -m pytest -q release_tools
96 passed
.venv/bin/python -m compileall -q release_tools/ecr.py release_tools/test_ecr.py
git diff --check
PASS
```

All clients and blob reads were injected. No ambient SDK construction, AWS
call, network request, image operation, deploy, or push occurred.
