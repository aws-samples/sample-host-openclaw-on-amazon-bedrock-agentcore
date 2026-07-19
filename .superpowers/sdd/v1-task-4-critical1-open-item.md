# Task 4 Critical #1 — closed by independently reviewed implementation

## Status

The initial remediation commit `de4d1e9` was **REJECTED** by independent hostile
review. Every Critical/Important review finding received a separate RED-first
follow-up implementation. Independent hostile fresh-clone re-review then
**ACCEPTED** exact commit `8cbe70cb51ebc7bfa57ba2ae38ad8f44e5e3c204`,
tree `d66b5bdb29844875f669664fefd512c41b57f92b`, with no unresolved
Critical or Important finding. Its focused gate passed 302 tests plus
compileall, Bash syntax, diff, and executable-bit checks. The accepted content
is integrated as `f0390f4`. External Docker/AWS/deploy/provider gates remain
open, and no local result is deployment evidence.

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
  template/parameter and request/security digests for every foundation,
  runtime, and consumer stack plus complete content digests for all consumer
  change sets. Each proposed processed change-set template and its direct
  parameters must also match the reviewed final stack digest.
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
- Every stack observation requires the real processed-template SDK shape,
  rejects a nonempty stack policy, and binds security-bearing request fields.
  Consumer application/verification fingerprints contain only reviewed stack
  content digests rather than arbitrary outputs or generated IDs.
- The production entrypoint is isolated from `PYTHONPATH`, `PYTHONHOME`, and
  `sitecustomize`, rejects interpreter overrides and a different `--root`, and
  revalidates the exact clean checkout before dispatch and again before live
  composition. The shell shim pins `PATH` before resolving itself; runbook
  invocation clears `BASH_ENV`. Git resolution ignores ambient `PATH` and
  `GIT_*` redirection.
- Account discovery, the mutation child, and the SDK observer use one exact
  sanitized credential environment. Fixed login-user AWS config and login
  cache paths plus the declared Boto3 CRT dependency support a prior user-run
  `aws login` without accepting ambient alternate credential paths.
- SDK endpoint overrides and ambient SDK proxies are disabled. Artifact reads
  use an explicit proxy-free HTTPS opener/context, and account discovery uses a
  validated absolute AWS CLI path rather than ambient `PATH`.
- RED-first regression tests prove forged driver observations are ignored,
  all phases use live authority, observation config mutation changes the
  operation digest, missing config stops before write-ahead, and hostile
  ECR/AgentCore/CloudFormation responses cannot select persistence. Hostile
  probes cover reviewed-template drift, arbitrary or truncated change-set
  content and proposed-template drift, stack policies, request/security-field
  drift, missing stable prerequisites, orphan runtimes hidden behind absent
  CloudFormation outputs, changed runtime context, partial AgentCore rollback
  state, proxy poisoning, Python import poisoning, dirty/changed checkouts, Git
  repository redirection, and executable `PATH` shadowing.

## Gate posture

Critical #1 is closed at the exact independently accepted subject recorded
above. External Docker/AWS/signing/scan/runtime/deploy gates remain OPEN. A
later authorized AWS run must still ensure the mutation driver creates every
consumer change set under the exact deterministic name `release-<40-sha>`
expected by the live authority.
