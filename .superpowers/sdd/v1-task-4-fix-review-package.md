# Task 4 remediation review package

## Review range

- Original independently reviewed candidate: `d4b8f97`
- Final code candidate before this package: `bac9ce94a8dfc0f16ce69178fd769cfa140cb491`
- Final code tree: `5c52f1927046030b7d30f61a114f0f4a43a140cb`
- Review command: `git diff --find-renames d4b8f97..bac9ce94a8dfc0f16ce69178fd769cfa140cb491`

The package commit itself contains evidence only. Review production behavior
against the code candidate above, then confirm the evidence-only delta does not
change that conclusion.

## Remediation commits

```text
4d8641a fix(release): scope reconciliation evidence
09062a4 fix(release): retain signing foundation
ff12a44 docs(release): retire mutable deployment path
11f19a2 fix(release): verify OCI attestation contents
25b5983 docs(release): record review remediation evidence
057dc9e fix(release): bind authoritative phase reconciliation
6841570 docs(release): record reconciliation remediation
b010748 fix(release): behavior-test atomic Lambda packaging
bac9ce9 fix(release): require typed live phase evidence
```

## Required hostile checks

Re-evaluate every Critical, Important, and Minor item in
`.superpowers/sdd/v1-task-4-review-report.md`. In particular, do not approve
unless the code, rather than comments or source-string assertions, proves all
of the following:

1. An operator cannot choose a persisted/absent outcome or inject a local
   evidence file. Reconciliation runs the exact digest-bound operation and
   leaves ambiguity `UNCERTAIN`.
2. A later phase cannot rewrite prior image, runtime, or context identity, and
   rollback cannot complete without a write-ahead intent from `VERIFIED`.
3. Every dispatch and observation reauthenticates the exact account and pins
   all AWS region variables to `eu-west-1`.
4. Image stabilization requires a full exact `RuntimeImageEvidence`; an
   endpoint cannot stabilize on `{}` and requires a full exact
   `RuntimeContextV3`. Context publication proves the digest of those exact
   canonical bytes.
5. `compose_production_evidence` really wires the strict ECR and AgentCore
   adapters with injected clients and performs no ambient client/session or
   credential work at import/construction time.
6. OCI SBOM and provenance manifests/blobs are retrieved and authenticated,
   name the exact image subject, and bind the exact commit, tree, build context,
   builder, and builder inputs.
7. The retained signing foundation and safe repository guidance remain exact.
8. Packaging security is exercised through subprocess/fake-command behavior,
   artifact inspection, and publication fault injection. A failed rebuild
   cannot delete or replace the prior verified asset.
9. No cloud, Docker, deploy, push, provider, or credentialed operation is
   necessary to run the local tests.

## Fresh verification on the code candidate

```text
.venv/bin/python -m pytest -q release_tools
110 passed in 13.80s

Applicable packaging/deploy/security/product/storage slice
114 passed in 24.82s

scripts/test-local.sh
1063 Python passed plus 10 subtests
11 E2E passed
313 bridge Node passed
5 web passed
web production build passed
JavaScript syntax passed
Python syntax passed
repository whitespace passed
offline CDK synthesis passed
cdk-nag passed
All local checks passed.
```

The aggregate used the existing local Python environment and Node 24. It made
no AWS call or deployment. The exact real Docker Python 3.13/ARM64 gate and all
documented external staging gates remain open.

## Expected review output

Write `.superpowers/sdd/v1-task-4-fix-review-report.md` with:

- spec compliance;
- strengths;
- Critical, Important, and Minor findings with file/line evidence;
- a finding-by-finding disposition of the original report;
- exact commands run and their outputs;
- `Approved` or `Needs fixes`.

No approval may rely only on this package or the implementation report. Inspect
the diff and execute independent focused probes.
