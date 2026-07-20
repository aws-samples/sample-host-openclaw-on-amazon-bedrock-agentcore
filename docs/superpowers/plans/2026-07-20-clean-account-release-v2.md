# Personal Operator v1 clean-account release v2 plan

**Date:** 2026-07-20
**Status:** In progress; every AWS mutation remains blocked until the terminal
reviews and local release gates pass.

## Objective

Replace the non-deployable eight-phase staging scaffold with a versioned,
immutable-plan transaction that can safely recover a clean-account release.
The release remains synthetic and effect-free: connector, browser, compute,
provider, Telegram-delivery, and pilot gates stay disabled.

## Non-negotiable invariants

- Preserve v1 journal parsing for read-only historical inspection. Never
  reinterpret a v1 journal as v2.
- Bind the clean commit/tree, exact artifacts, driver bytes, image subject,
  phase graph, and rollback posture before the first product mutation.
- Derive every operation identity from the immutable plan, exact next step, and
  canonical completed evidence prefix. Generated runtime, endpoint, context,
  and change-set observations therefore change every dependent operation.
- Persist write-ahead intent before one exact mutation step. Mutation output is
  acknowledgement only; only the in-package live observer supplies evidence.
- Treat an exact completed prefix as recoverable. Unknown, contradictory, or
  non-prefix state remains `UNCERTAIN`.
- Never give a mutation process the authoritative journal path or descriptor.
- Stage AgentCore Runtime, MMDSv2 verification/hardening, and Endpoint serially.
- Use generated endpoint ID/ARN for IAM and resource policies; retain endpoint
  name only as the invocation qualifier.
- Build the runtime from an exact Git archive, probe it without credentials,
  and bind exact OCI, SPDX, provenance, signing, and scan subjects.
- Region is exactly `eu-west-1`. No provider calls or real messages.

## Phase graph

1. `foundation`: live clean baseline; bootstrap; exact assets; VPC, security,
   guardrails, capabilities, AgentCore foundation, observability; derive typed
   foundation runtime inputs.
2. `image`: publish each plan-bound OCI blob as one journaled effect, then the
   subject manifest plus commit tag, SBOM referrer manifest, and provenance
   referrer manifest; reconcile exact signature and completed clean scan.
3. `runtime`: update the existing AgentCore foundation stack to create Runtime
   and its command-deny policy; then run one always-planned, separately journaled
   MMDSv2 hardening/reconciliation step and retain its exact versioned ARN.
4. `endpoint`: update the AgentCore stack to create the retained commit-bound
   endpoint and its deny policy
   against the retained MMDSv2 runtime version.
5. `context`: collect and atomically write `RuntimeContextV3` in-package.
6. `router-cron-cs`: create exact Router and Cron change sets.
7. `router-cron`: execute and verify Router, then Cron.
8. `scheduler-cs`: create the exact Scheduler change set.
9. `scheduler`: execute and verify Scheduler.
10. `web-cs`: create the exact Web change set.
11. `web`: execute and verify Web.
12. `verify`: read-only aggregate verification.

One CLI invocation crosses at most one internal mutation step.

## Closed step recipe and subjects

Let `A` be the exact twelve-digit account, `R` be `eu-west-1`, and `C` be the
exact forty-character source commit. Subjects use these canonical forms:

- release subject: `release:A:R:C:<suffix>`;
- stack subject: `cfn:A:R:stack:<stack-name>:release:C`;
- image subject:
  `ecr:A:R:repository:personal-operator/bridge:release:C`;
- image blob effect:
  `ecr:A:R:repository:personal-operator/bridge:blob:sha256:<digest>`;
- image subject-manifest effect:
  `ecr:A:R:repository:personal-operator/bridge:subject-manifest:sha256:<digest>:tag:commit-C`;
- image referrer effects:
  `ecr:A:R:repository:personal-operator/bridge:<sbom|provenance>-referrer-manifest:sha256:<digest>:subject:sha256:<runtime-image-digest>`;
- CDK asset subject: `cdk:asset:<lowercase-sha256>`. The asset subject itself
  names the synthesized asset ID and S3 object-key fingerprint, which may be a
  source or custom fingerprint rather than the uploaded payload SHA-256. Its
  containing plan and operation bind that key identity to `A`, `R`, and `C`;
  `expectedContentSha256` independently binds the uploaded payload bytes.

The ordered recipe is exact:

| Phase | Kind and subject, in order |
| --- | --- |
| `foundation` | `BASELINE_OBSERVE release:A:R:C:baseline`; `BOOTSTRAP_STACK` for `CDKToolkit`; one or more `ASSET_PUBLISH cdk:asset:<sha256>` entries sorted uniquely by subject; then `STACK_CREATE` for `OpenClawVpc`, `OpenClawSecurity`, `OpenClawGuardrails`, `PersonalOperatorCapabilities`, `OpenClawAgentCore`, and `OpenClawObservability` |
| `image` | one `IMAGE_PUBLISH` per OCI blob, sorted uniquely by subject; `IMAGE_PUBLISH` for the exact subject manifest and commit tag; `IMAGE_PUBLISH` for the SBOM referrer manifest; `IMAGE_PUBLISH` for the provenance referrer manifest; then `IMAGE_OBSERVE` against the aggregate release subject |
| `runtime` | `STACK_UPDATE` for `OpenClawAgentCore`; then `AGENTCORE_HARDEN agentcore:A:R:runtime:personal_operator_bridge:release:C:mmdsv2` |
| `endpoint` | `STACK_UPDATE` for `OpenClawAgentCore` |
| `context` | `RUNTIME_CONTEXT_WRITE release:A:R:C:artifact:build/runtime-context.json` |
| `router-cron-cs` | `CHANGESET_CREATE` for `OpenClawRouter`, then `OpenClawCron` |
| `router-cron` | `CHANGESET_EXECUTE` for `OpenClawRouter`, then `OpenClawCron` |
| `scheduler-cs` | `CHANGESET_CREATE` for `PersonalOperatorScheduler` |
| `scheduler` | `CHANGESET_EXECUTE` for `PersonalOperatorScheduler` |
| `web-cs` | `CHANGESET_CREATE` for `PersonalOperatorWeb` |
| `web` | `CHANGESET_EXECUTE` for `PersonalOperatorWeb` |
| `verify` | `VERIFY release:A:R:C:verify` |

Every step owns one unique request artifact. `AGENTCORE_HARDEN` is always in
the plan: authoritative observation may reconcile it as already present, but a
required `UpdateAgentRuntime` can never be hidden inside the Runtime stack
operation.

The image phase contains at least one blob effect. Blob subjects and digests
are lexical and unique, followed by the three manifests in the fixed order
above. Each request-artifact digest independently binds the complete private
effect file (magic, canonical header, and raw payload), while
`expectedContentSha256` binds only the selected raw blob or manifest digest
named by its provider subject. No artifact or journal step can hide multiple
registry writes; multipart transfer is contained inside the single final
blob-digest effect. Unknown or partial outcomes reconcile independently by
blob or manifest subject before the aggregate read-only observation can run.
The `IMAGE_OBSERVE` step uniquely owns
`build/image-publication-plan.json`; its request digest binds the canonical
`ImagePublicationPlanV1`. Preflight must parse that artifact and every effect
header, require one shared publication-plan digest, prove the exact blob and
manifest closure, and match each current step's subject and content. Referrer
subjects also carry the exact runtime-image target digest, so an unrelated
referrer closure cannot satisfy the plan.

The step digest fields have separate, closed meanings:

| Field | Non-empty exactly for | Meaning |
| --- | --- | --- |
| `requestSha256` and `expectedRequestSha256` | every step | Two compatibility-preserving bindings to the same immutable raw request-artifact bytes; neither is live provider evidence. |
| `expectedTemplateSha256` | Runtime and Endpoint `STACK_UPDATE` | SHA-256 of the exact reviewed raw UTF-8 update-template body bytes. It is plan-derived, excludes the completed-prefix-derived parameters, and is independent from both the request artifact and `expectedObservedRequestSha256`. |
| `expectedObservedRequestSha256` | `BOOTSTRAP_STACK`, `STACK_CREATE`, `STACK_UPDATE`, `CHANGESET_CREATE`, and `CHANGESET_EXECUTE` | The precomputed canonical CloudFormation observer projection of persistent request/security fields. It excludes template parameters, idempotency tokens, AWS-generated IDs, and generated result content, and may not alias any artifact, template/parameter, or content digest. |
| `expectedTemplateParameterSha256` | foundation `BOOTSTRAP_STACK`/`STACK_CREATE` and consumer `CHANGESET_CREATE` | The pre-cloud processed-template plus exact-parameter digest. Runtime and endpoint `STACK_UPDATE` leave it empty because their exact parameters are derived from typed completed-prefix inputs. |
| `expectedContentSha256` | `ASSET_PUBLISH`, `IMAGE_PUBLISH`, and `IMAGE_OBSERVE` | Immutable pre-cloud content only. Asset content is the uploaded payload digest and is intentionally independent from the synthesized asset ID in its subject. Each image-publish content digest equals its exact effect-subject digest; the subject-manifest effect alone equals the hex payload of `runtimeImageDigest`. Aggregate image observation also binds that runtime-image digest. |

Every other field/kind combination is exactly empty. In particular, the plan
does not fabricate content digests for `AGENTCORE_HARDEN`,
`RUNTIME_CONTEXT_WRITE`, change-set creation or execution, or `VERIFY`.
Those results belong only to canonical `ReleaseStepObservationV2` evidence.
Runtime and endpoint stack requests are reconstructed from the retained raw
artifact plus journal-owned typed inputs. Their exact reviewed raw UTF-8
template-body bytes must match `expectedTemplateSha256`, while the dynamic parameters are
validated separately at dispatch and by the live observer. Change-set
execution likewise reconciles the live application against the prior
journal-owned change-set observation, not against a preplanned application
hash.

Every `PRESENT` result is one canonical `ReleaseStepObservationV2`, never a
caller-selected evidence digest plus a second derived-value map. Its exact bytes
bind the plan digest, step ID and subject, observer-evidence digest, and the exact
step-owned generated values: full foundation inputs, image digest, atomic Runtime
ID/version/versioned ARN, Endpoint ID, context digest, change-set/application
digest, or verification digest. Foundation inputs also own the exact
`OpenClawAgentCore` CloudFormation StackId. Runtime and endpoint observations
must repeat that same StackId. Each individual consumer `CHANGESET_CREATE`
observation owns an atomic exact target StackId and commit-bound ChangeSetId;
Router, Cron, Scheduler, and Web pairs are retained separately rather than
hidden in the aggregate change-set digest. The journal reparses the typed observation,
validates ownership, computes its SHA-256 in-package, appends that digest to the
completed prefix, and copies generated state from those same canonical bytes.
Changing a generated value therefore changes the observation digest, completed
prefix, and every dependent operation even when observer evidence is unchanged.

CloudFormation IDs use the exact `eu-west-1` account-bound ARN, canonical stack
name, and a bounded non-empty provider identifier. ChangeSetIds additionally
contain exactly `release-C`. A consumer `CHANGESET_CREATE` must begin only from the
typed `ABSENT` precondition required by `ChangeSetType=CREATE`; its `PRESENT`
observation captures CloudFormation's generated target StackId and ChangeSetId.
All later Runtime/Endpoint updates must use the retained AgentCore StackId ARN,
and consumer execution must use the retained ChangeSetId ARN (with the matching target
StackId available for observation). Neither path resolves a logical name again.
A same-name delete/recreate therefore fails identity validation instead of
redirecting a mutation.

The last foundation observation carries `FoundationRuntimeInputsV1`. In
addition to its live outputs, that contract binds `sourceCommit`, `sourceTree`,
`account`, `region`, `releasePlanSha256`, and the exact
`foundation-runtime-inputs-v1` derivation version, plus the exact observed
`OpenClawAgentCore` StackId. Runtime hardening must retain the StackId, observed
Runtime ID, and ARN base and may retain or increase, never decrease, the Runtime
version. Endpoint creation also retains that StackId.

The immutable pre-cloud artifact never contains `operationSha256`; doing so
would create a plan-digest/request-digest cycle. `MutationRequestV2` supplies
the operation and completed-prefix digests outside that artifact while binding
its exact path and digest. Before dispatch, a canonical
`ResolvedMutationRequestV2` combines that request with all generated inputs
owned by the completed prefix: full foundation inputs, image and Runtime tuple,
Endpoint and context tuple, and downstream change-set/application digests. The
small canonical header also binds the immutable request-artifact path, byte
length, and SHA-256 plus the exact source commit, source tree, account, region,
step phase, and the next step's exact template/parameter, observable-request,
and immutable-content expectations. It carries the retained AgentCore StackId
and the separately journal-owned Router, Cron, Scheduler, and Web target
StackId/ChangeSetId pairs. Provider binders compare their typed request and
payload to those plan-derived header fields and select only the exact relevant
ARN identity before any call. It
does not Base64-expand artifact bytes or impose the
canonical-JSON size limit on an OCI closure. `PrivateMutationEnvelopeV2` is the
single driver-visible binary file: fixed magic, a four-byte big-endian canonical
header length, the `ResolvedMutationRequestV2` bytes, then the raw request
artifact stream. Trusted assembly and parsing stream the artifact, cap it at
8 GiB, and require its exact plan-bound length and SHA-256. The same framing
accepts a normal request or the image content-store bundle. Artifact bytes may
not contain the reserved `operationSha256` or `driverRequestSha256` fields, so
operation identity exists only in the post-plan header.

`from_path` returns diagnostics only. An authority-bearing consumer must use
`open_verified(..., scratch_dir=...)`: the source is opened nonblocking without
following a final symlink, fully validated, and copied while hashing into a
private `0600` exclusive scratch file. The writer is fsynced and closed; the
snapshot is reopened read-only without following links and immediately
unlinked. The context retains that descriptor and exposes bounded bytes or a
resetting chunk iterator. It never reopens the source path and fails after
close. `from_path` may run against a stable pre-intent transaction because it
cannot return the descriptor. `open_verified` requires the journal already to
contain the exact matching `UNCERTAIN` step and operation; no dispatch
capability exists before write-ahead intent. A provider-specific binder must
parse the retained raw segment and combine it with the same retained header;
generated CloudFormation parameters may not be injected into an independently
reconstructed operation. No root or second artifact path reaches the driver,
the header digest is computed in-package, and there is no free caller-supplied
driver request digest.

A stable, nonterminal clean-account prefix may enter `ABORTED_RETAINED` only
from canonical `AbortRetainedEvidenceV2`. Its bytes bind the plan digest,
completed-prefix digest and count, every retained step ID and subject, stable
state, and one closed stop reason. The journal validates those bytes and
computes the stored abort digest in-package. An `UNCERTAIN` step must reconcile
first; `ROLLED_BACK` remains impossible for `NO_PRIOR_RELEASE`.

An authoritative terminal failure is never mapped to `ABSENT` or `PENDING`.
A mutation first enters `UNCERTAIN`; reconciliation then uses the separate
`FAILED_RETAINED` disposition and a canonical nested
`ReleaseStepFailureObservationV2`. A read-only next step never creates mutation
intent. Its terminal scan or signing failure instead uses the atomic
`fail_observation_retained` transition directly from the exact stable prefix.
That transition accepts only the plan's exact read-only next step and derives
the same plan-, step-, and prefix-bound operation identity; this digest is step
identity, not write-ahead intent.

For both paths, `FailedRetainedEvidenceV2` binds the canonical observation to
the plan, completed-prefix digest and count, stable state, exact failed step and
subject, and exact operation. The journal computes the failure-observation and
failed-retained-evidence digests in-package, preserves the failed step,
subject, operation, reason, stable state, and completed prefix, clears any
uncertainty, and atomically enters `ABORTED_RETAINED`. The clean-account
`NO_PRIOR_RELEASE` posture never becomes a rollback claim.

The failure matrix is closed by exact step kind, provider, reason, and terminal
status:

| Step kind | Provider | Failure reason | Exact terminal status |
| --- | --- | --- | --- |
| `BOOTSTRAP_STACK`, `STACK_CREATE`, `CHANGESET_EXECUTE` | `CLOUDFORMATION` | `CLOUDFORMATION_STACK_FAILED` | `CREATE_FAILED`, `ROLLBACK_COMPLETE`, or `ROLLBACK_FAILED` |
| `STACK_UPDATE` | `CLOUDFORMATION` | `CLOUDFORMATION_STACK_FAILED` | `UPDATE_FAILED`, `UPDATE_ROLLBACK_COMPLETE`, or `UPDATE_ROLLBACK_FAILED` |
| `CHANGESET_CREATE` | `CLOUDFORMATION` | `CLOUDFORMATION_CHANGESET_FAILED` | `FAILED` |
| `BOOTSTRAP_STACK`, `STACK_CREATE`, `CHANGESET_CREATE`, `CHANGESET_EXECUTE` | `CLOUDFORMATION` | `CF_SUBJECT_CONFLICT` | `CREATE_COMPLETE` |
| `STACK_UPDATE` | `CLOUDFORMATION` | `CF_SUBJECT_CONFLICT` | `UPDATE_COMPLETE` |
| `AGENTCORE_HARDEN` | `AGENTCORE` | `AGENTCORE_UPDATE_FAILED` | `UPDATE_FAILED` |
| `AGENTCORE_HARDEN` | `AGENTCORE` | `AGENTCORE_SUBJECT_CONFLICT` | `READY` |
| `ASSET_PUBLISH` | `S3` | `ASSET_SUBJECT_CONFLICT` | `RETAINED_OBJECT_CONFLICT` |
| `IMAGE_PUBLISH` | `ECR` | `IMAGE_SUBJECT_CONFLICT` | `IMMUTABLE_SUBJECT_CONFLICT` |
| `IMAGE_PUBLISH` | `ECR` | `IMAGE_PARTIAL_CLOSURE` | `RETAINED_PARTIAL_CLOSURE` |
| `IMAGE_PUBLISH`, `IMAGE_OBSERVE` | `ECR` | `IMAGE_SCAN_FAILED` | `SCAN_POLICY_FAILED` |
| `IMAGE_PUBLISH`, `IMAGE_OBSERVE` | `ECR` | `IMAGE_SIGNING_FAILED` | `SIGNATURE_VERIFICATION_FAILED` |
| `RUNTIME_CONTEXT_WRITE` | `LOCAL_FILESYSTEM` | `RUNTIME_CONTEXT_CONFLICT` | `EXISTING_CONTENT_CONFLICT` |

`CF_SUBJECT_CONFLICT` and `AGENTCORE_SUBJECT_CONFLICT` mean the provider is in
a nominally healthy status but the observed live identity or security-bearing
configuration differs from the exact planned subject after intent. They are
terminal retained conflicts, never absence and never retryable pending state.
Healthy statuses are invalid for every other failure reason, and retryable,
absent, unknown, or wrong-kind statuses cannot produce failure evidence.

Numbered Bedrock guardrail versions use the same canonical form in foundation
inputs and runtime configuration: `DRAFT` or `[1-9][0-9]{0,7}`. Before a real
release, the runtime/context binder and live observer must additionally prove
that the runtime environment guardrail ID and version equal the typed
foundation inputs. The current context observation contract carries only the
context digest, so that caller wiring remains an explicit open implementation
item.

All public trust boundaries canonically serialize and reparse `ReleasePlanV2`,
its nested steps/artifacts, foundation inputs, observations, mutation requests,
resolved envelopes, abort evidence, and journal state before persistence or
validation. A frozen dataclass produced by `dataclasses.replace` is not trusted
merely because it has the expected Python type.

## Implementation slices

### 1. Runtime cloud-readiness defect closure

- Install TLS roots in both runtime image stages.
- Inject the exact capability gateway ARN into Runtime.
- Require that ARN and `DISABLE_ADOT_OBSERVABILITY=true` in live evidence.
- Run focused release/security suites, independent review, and full aggregate.

Acceptance: committed independently with a clean tree and literal
`All local checks passed.` evidence.

### 2. Immutable v2 contracts and journal

- Add strict `ReleasePlanV2`, `FoundationRuntimeInputsV1`,
  `MutationRequestV2`, `ReleaseStepObservationV2`,
  `ReleaseStepFailureObservationV2`, `ResolvedMutationRequestV2`,
  `AbortRetainedEvidenceV2`, `FailedRetainedEvidenceV2`, and
  `StagingTransactionV2` contracts beside v1, plus the streaming binary
  `PrivateMutationEnvelopeV2` framing outside the canonical-JSON parser.
- Add a plan-prefix `TransactionJournalV2` with typed `ABSENT`, `PENDING`,
  `PRESENT`, and terminal `FAILED_RETAINED` mutation reconciliation. Only
  `PRESENT` accepts a success observation; `FAILED_RETAINED` accepts exact
  failure evidence. A stable read-only failure uses the separate atomic
  `fail_observation_retained` API without mutation intent. The journal computes
  observation, failure, and abort digests internally.
- Bind every operation to plan bytes, the exact next step, the completed
  evidence prefix, and referenced artifact bytes; never clear historical plan
  identity.
- Record `NO_PRIOR_RELEASE` for a clean account. Such a transaction can end in
  `ABORTED_RETAINED`, never falsely claim `ROLLED_BACK`.

Acceptance: hostile tests cover plan substitution, phase reorder/omission,
unsafe artifacts, non-prefix evidence, step replay, and crash boundaries.

### 3. AgentCore staging and canonical ARN evidence

- Add strict foundation/runtime/endpoint synthesis stages.
- Verify command-deny policies on canonical `runtime/{runtimeId}` and
  `runtime/{runtimeId}/runtime-endpoint/{endpointId}` resources.
- Observe the created Runtime version. If MMDSv2 is not explicitly true,
  perform one exact `UpdateAgentRuntime` with complete reviewed configuration,
  `requireMMDSV2=true`, and no deprecated S3 endpoint flag; wait for READY.
- Bind Endpoint to the retained exact version.
- Pass exact endpoint ID/canonical ARN into Router and Web IAM while keeping
  the endpoint name as qualifier.

Acceptance: installed-model, synth, fake-service, drift, and cross-account
tests prove each boundary.

### 4. Stage-aware release assembly and driver

- Build a pre-cloud plan from exact Git/artifact bytes.
- Synthesize foundation without a runtime context; synthesize later stages only
  from typed, live-derived inputs.
- Add one reviewed mutation dispatcher with closed operation kinds. It receives
  a private request file only—never journal/root paths or observer results.
- Make reconciliation observer-only and atomically write derived contracts
  in-package.

Acceptance: empty-account simulation reaches every exact step; failures before
and after each fake AWS call recover only an exact prefix.

### 5. Image publication

- Export the exact tracked `bridge` tree; exclude tests and mutable local files.
- Produce two fresh ARM64 OCI closures and require identical descriptors.
- Probe nonroot identity, immutable roots, TLS roots, release/catalog binding,
  startup health, and network denial.
- Generate and validate exact SPDX 2.3 and SLSA v1 referrers.
- Publish through injected ECR APIs using the immutable plan; reconcile exact
  manifest/referrer digests, managed signing, and completed zero-high/critical
  scan through the independent observer.

Acceptance: producer-to-observer round trip and hostile partial-upload,
collision, descriptor, archive, and artifact-substitution tests pass.

### 6. Terminal review and synthetic deployment

- Run focused security suites and the full aggregate from one clean commit.
- Commission independent specification and security reviews; resolve all
  Critical/Important findings RED-first.
- Deploy only the synthetic/no-effect stack in account `264911721456`, region
  `eu-west-1`, through the accepted transaction.
- Exercise `/ping`, one credential-free model turn, workspace file operations,
  read-only schedule listing, isolation, alarms, logs, and teardown/containment
  evidence. Do not enable connector/browser/compute/provider planes.

Acceptance: exact live evidence is recorded without personal/source data;
external connector/browser/compute/provider/pilot gates remain explicitly open.

## Stop conditions

Stop before mutation if the plan is not immutable, a live subject is ambiguous,
MMDSv2 cannot be proven, the image or referrers differ, signature/scan evidence
is incomplete, a required observer uses a caller-selected value, or any
Critical/Important review finding remains open.
