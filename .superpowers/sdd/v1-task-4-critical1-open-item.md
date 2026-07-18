# Task 4 Critical #1 — OPEN deploy-gate item

## Status

Task 4 remediation is **4 of 5 findings fixed** at takeover-branch fork
`0095d2b` (branch `codex/po-v1-infra-takeover`, forked from the prior writer's
`codex/po-v1-infra`). The remaining **Critical #1** is intentionally deferred as
an OPEN external-deploy gate, consistent with the standing mandate that
Docker/AWS/deploy/provider gates remain open and that no partial is promoted
into a completion claim.

## Findings disposition

Independent assessment (workflow `wf_b0028e5c-881`, assess phase) confirmed at
`0095d2b`:

- **Important #1 (rollback contract requires VERIFIED source)** — FIXED
  (`release_tools/contracts.py`).
- **Important #3 (driver environment sanitization)** — FIXED
  (`release_tools/cli.py` `_sanitized_environment`: allowlist build, pins
  account/region, strips endpoint/path/PYTHONPATH). Test `test_cli.py:517-537`.
- **Important #4 (content-free SBOM rejected)** — FIXED
  (`release_tools/ecr.py` `_validate_sbom`: full SPDX-2.3 content validation
  bound to the exact image subject digest + OCI purl). Tests
  `release_tools/test_ecr.py:560-596`.
- **Important #5 (unsafe mutable/auto-approve runbooks removed)** — FIXED
  (`docs/guardrails.md`, `docs/security.md` archival banners routing to the
  immutable `eu-west-1` staging CLI; `git grep "require-approval never"` clean
  in those two files).

## The open Critical #1

**Defect:** In `release_tools/cli.py` the operator-supplied `--driver`
executable is invoked twice per phase — `mode="mutate"` (legitimate cloud
mutation) and `mode="observe"`. The `observe` invocation's STDOUT is parsed by
`_observation()` / `_phase_evidence()` and trusted as authoritative live
evidence, then fed to `journal.reconcile()`. An operator can therefore choose
the outcome (`PERSISTED`/`ABSENT`) and forge `RuntimeImageEvidence` /
`RuntimeContextV3` via a driver they control, defeating the `UNCERTAIN`
crash-safety and immutable-subject invariants.

The strict, correct live-evidence reader — `ProductionEvidenceComposer` /
`compose_production_evidence` in `release_tools/production_observation.py` —
already exists and is fully unit-tested (`release_tools/test_production_observation.py`),
but has **zero callers on the release path** (`grep -n
"production_observation\|compose_production_evidence" release_tools/cli.py`
returns nothing).

**Why deferred, not faked green:** the release CLI is only exercised at a real
AWS deploy, which is explicitly gated until after Integration Gate A and an
authorized deployment decision. Closing Critical #1 correctly is a
release-state-machine change, because the composer today covers only 3 of the 8
transaction phases (image/endpoint/context) and has no absent-outcome
semantics; foundation/runtime/consumer-changesets/consumers/verify/rollback all
need an in-package observation authority too. This must be hand-implemented with
strict TDD and an independent hostile review immediately before any real deploy.

**Required fix (scoped for the pre-deploy implementation):**
- Move the authoritative observation off driver STDOUT onto an in-package
  authority resolved after account discovery, pinned `eu-west-1`, credential-lazy
  (production builds boto3 ECR/AgentCore clients + `ArtifactBlobReader` and calls
  `compose_production_evidence`; a test seam injects a fake).
- Cover every phase, not only the 3 the composer currently exposes.
- Preserve UNCERTAIN on ambiguity; never accept an operator-chosen
  `PERSISTED`/`ABSENT`; fail closed if the live authority is unavailable.
- Re-point the `observe`-path tests in `release_tools/test_cli.py` and add a
  RED-first test proving a forged-driver `observe` STDOUT cannot advance the
  journal.

## Gate posture

External Docker/AWS/signing/scan/runtime/deploy gates remain OPEN. Do not deploy
until Critical #1 is closed and independently re-reviewed.
