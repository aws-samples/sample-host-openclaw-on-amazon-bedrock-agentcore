# Task 4 Critical #1 — implemented, awaiting independent review

## Status

The initial remediation commit `de4d1e9` was **REJECTED** by independent hostile
review. Every Critical/Important review finding now has a separate RED-first
follow-up implementation, but Critical #1 remains **OPEN pending independent
re-review of the follow-up commit**. External Docker/AWS/deploy/provider gates
remain open, and no local result is deployment evidence.

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

## Original Critical #1

**Original defect:** Before this remediation, the operator-supplied `--driver`
executable was invoked twice per phase — `mode="mutate"` (legitimate cloud
mutation) and `mode="observe"`. The `observe` invocation's STDOUT was parsed by
`_observation()` / `_phase_evidence()` and trusted as authoritative live
evidence, then fed to `journal.reconcile()`. An operator could therefore choose
the outcome (`PERSISTED`/`ABSENT`) and forge `RuntimeImageEvidence` /
`RuntimeContextV3` via a controlled driver, defeating the `UNCERTAIN`
crash-safety and immutable-subject invariants.

## Implemented remediation

- `release_tools/cli.py` invokes the reviewed executable only for mutation.
  After a second exact-account check it calls the in-package
  `ProductionEvidenceComposer`; operator STDOUT has no observation path.
- A strict `personal-operator.production-observation-config.v1` is required
  before mutation. Its canonical bytes and the exact executable bytes are
  jointly bound into the journal operation digest. The config includes reviewed
  template/parameter digests for every foundation, runtime, and consumer stack
  plus complete content digests for all consumer change sets.
- The composer resolves all eight forward phases plus rollback from injected
  ECR, AgentCore, and CloudFormation clients. It validates exact account,
  `eu-west-1`, commit, tree, immutable artifacts, runtime configuration, stack
  identity/state/content, deterministic `release-<40-sha>` consumer change
  sets, and exact equality between live content and the reviewed config.
- Normal-phase `ABSENT` requires all phase-owned subjects absent and every
  `lastStableState` prerequisite still exactly present. Missing prerequisites,
  partial presence, service errors, pagination, malformed or conflicting
  evidence, and rollback mismatch fail closed and leave the journal
  `UNCERTAIN`.
- Rollback additionally queries the retained AgentCore runtime and endpoint;
  CloudFormation absence alone cannot complete it. Both AgentCore subjects must
  be coherently absent or still form the exact release context.
- SDK endpoint overrides and ambient SDK proxies are disabled. Artifact reads
  use an explicit proxy-free HTTPS opener/context, and account discovery uses a
  validated absolute AWS CLI path rather than ambient `PATH`.
- RED-first regression tests prove forged driver observations are ignored,
  all phases use live authority, observation config mutation changes the
  operation digest, missing config stops before write-ahead, and hostile
  ECR/AgentCore/CloudFormation responses cannot select persistence. Hostile
  probes cover reviewed-template drift, arbitrary or truncated change-set
  content, missing stable prerequisites, partial AgentCore rollback state,
  proxy poisoning, and `PATH` shadowing.

## Gate posture

External Docker/AWS/signing/scan/runtime/deploy gates remain OPEN. Do not deploy
until this implementation has passed independent hostile review and the
reviewed commit has passed the required local gates. A later authorized AWS
run must also ensure the mutation driver creates every consumer change set
under the exact deterministic name `release-<40-sha>` expected by the live
authority.
