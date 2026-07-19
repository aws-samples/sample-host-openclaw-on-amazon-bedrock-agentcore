# Task 4 Critical #1 — OPEN after integrated release-path audit

## Status

**OPEN / deployment blocker.** A later integrated audit on 2026-07-19 showed
that the earlier closure claim was incorrect. The current release
observe/reconcile path in `release_tools/cli.py` still trusts the operator
`--driver` output as live evidence. The in-package
`ProductionEvidenceComposer` exists and has injected live AWS adapters, but no
production caller routes all release phases through it.

Earlier commits `8cbe70cb51ebc7bfa57ba2ae38ad8f44e5e3c204` and
`f0390f4` remain historical local test subjects only. Their review cannot close
this later-discovered integrated caller defect and must not be used as deploy
authorization.

## Critical defect

The reviewed mutation executable is legitimately operator supplied for cloud
mutation. It must never select the observation result. In the current CLI, its
stdout can still drive `PERSISTED`/`ABSENT` reconciliation and forge phase
evidence. That defeats write-ahead `UNCERTAIN`, immutable-subject, and
authoritative-live-observation invariants.

The existing composer does not repair the defect by existing unused. Until the
CLI itself invokes it, ambiguity, partial presence, pagination, malformed live
responses, and driver-controlled output can be classified incorrectly.

## Required remediation before any AWS mutation

1. Add RED tests proving driver stdout cannot influence observation for every
   forward phase and rollback/reconciliation path.
2. Route all eight retained phases through the in-package
   `ProductionEvidenceComposer` with authenticated, injected ECR, AgentCore,
   and CloudFormation adapters.
3. Keep timeouts, service errors, pagination, partial state, missing stable
   prerequisites, and conflicting evidence `UNCERTAIN`; never infer success
   from mutation-process output.
4. Reconcile runtime, endpoint, command-deny policies, image/signing/scan
   evidence, change-set content, stack content, and rollback subjects against
   one exact reviewed operation/config identity.
5. Independently specification-review and security-review the exact integrated
   caller, then rerun focused release suites and the full aggregate gate.

## Gate posture

No deployment, push, signing, scan, AgentCore invocation, provider effect, or
pilot may use the current transaction. Credential-free `--preflight` and
`--status` remain local inspection surfaces only. External gates remain OPEN.
