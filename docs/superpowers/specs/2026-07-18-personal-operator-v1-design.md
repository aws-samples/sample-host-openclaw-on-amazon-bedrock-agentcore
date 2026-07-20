# Personal Operator v1 Design

## Status and release boundary

This document is the authoritative design for the first post-v0 Personal
Operator implementation. It starts from clean commit
`0fad2ce4758dda4b2b1221dbb4db0eee3e6c8fe3` and preserves every v0 security
invariant unless this document explicitly strengthens it.

The implementation may be completed and verified locally without AWS or
provider credentials. It is not deployed merely because its source, tests, or
CloudFormation synthesize. A staging release requires immutable external
artifact evidence, an exact account and region preflight, and explicit human
authorization. No real email, browser effect, or other provider mutation is
part of the local implementation gate.

## Product

Personal Operator is a consumer personal AI computer reached through a simple
chat and web experience. The user should experience one persistent operator
that remembers their work, can run bounded Linux jobs, read exact web targets,
prepare reminders and scheduled work, and connect curated personal services.
The implementation is deliberately not a general-purpose credential-bearing
computer: broad power is assembled from isolated, typed capabilities whose
authority is held outside the conversational runtime.

OpenClaw remains the conversational runtime. It receives a stable personal
workspace and a fixed, release-owned tool catalog. It never receives Google,
Telegram, browser-profile, database, approval-signing, or cross-user
credentials. It cannot install arbitrary skills, MCP servers, or tools at
runtime. Linux execution is a separate disposable, credential-free capsule.
Browser and connector access are separate trusted services. Every external
mutation is proposed, exactly approved where required, reconciled, and
receipted by the control plane.

The first consumer pilot remains invite-only. Its default provider capability
is read-only Gmail opportunity discovery. Wider capabilities are implemented
behind explicit installation and release gates, not silently enabled for pilot
accounts.

## System shape

```text
Telegram / Web
      |
      v
trusted ingress -> per-user FIFO -> trusted worker -> RuntimeDriver
                                              |          |
                                              |          v
                                              |   AgentCore / OpenClaw
                                              |   fixed po_* tools only
                                              |          |
                                              v          v
                                       action kernel <- loopback relay
                                              |
                                    capability admission gateway
                                      /       |        \
                              compute     scheduler   connectors/browser
                              capsule        plane       adapters
                                      \       |        /
                                       receipts + portable state
```

The parent bridge retains a short-lived turn grant in trusted memory and
offers a private loopback relay. The OpenClaw child sees model-safe tool
schemas, not bearer grants or provider credentials. The relay injects
server-owned user, session, runtime, release, invocation, catalog, and call
identity. The capability gateway strong-reads live authority and deletion
state before any adapter invocation.

## Frozen trust decisions

1. OpenClaw keeps the existing four workspace tools and may gain only explicit
   `po_*` tools listed in the immutable release catalog. There is no generic
   `po_capability_call`, dynamic MCP discovery, ClawHub, arbitrary plugin URL,
   user API key, shell, browser, cron, or provider credential in the runtime.
2. Tool availability is release-owned. A per-user installation can disable a
   catalog entry but cannot download code or change its schema.
3. Credentials and browser cookies remain inside the selected trusted adapter.
   They are absent from OpenClaw environment, workspace, model input, logs,
   grants, and tool results.
4. Linux execution is a separate networkless job sandbox with no live
   workspace mount and no AWS or provider credentials. Inputs are copied by
   hash and outputs are imported beneath a fresh job namespace after
   validation.
5. Schedules are control-plane records. EventBridge receives only an opaque
   schedule ID, generation, and occurrence time, then enqueues work through
   the existing per-user FIFO. Scheduled turns may read or prepare proposals;
   they receive no standing external-effect authority.
6. Exact user-supplied public URL reads use an expiring target grant derived
   from the current authenticated request. Model-created, workspace-derived,
   previous-turn, modified, private-network, metadata, or redirect-escaped
   targets are denied before DNS or HTTP access.
7. Durable and external mutations use the action state machine. Approval binds
   the complete canonical payload, user, connection, capability, resource,
   revision, expiry, and originating invocation. Ambiguous effects become
   `UNCERTAIN` and are reconciled; they are never blindly replayed.
8. Account deletion first persists an authority fence. Every gateway and
   adapter rechecks it at its last application-controlled point. Stale jobs,
   schedules, imports, runtimes, or provider callbacks cannot restore
   authority or data.
9. Staging runtime releases use an immutable ECR digest, one exact runtime
   version, and a never-retargeted endpoint named `release_<40-sha>`. Consumer
   stacks never bind `DEFAULT`.
10. The only deployment region is `eu-west-1`. Local synth uses the existing
    impossible test account and makes no deployment claim.

## Canonical capability contracts

All contract documents are strict canonical JSON: exact field set, sorted
keys, UTF-8, finite numbers only, explicit byte/count bounds, duplicate-key
rejection, no aliases, and a single trailing newline only where the enclosing
artifact contract requires it.

### `CapabilityCatalogV1`

- `schema`: `personal-operator.capability-catalog.v1`
- `releaseCommit`: exact lowercase forty-character Git SHA
- `catalogDigest`: SHA-256 of the catalog with this field omitted
- `packs`: ordered `CapabilityPackV1` list

### `CapabilityPackV1`

- `packId`, `version`, `riskClass`, and `credentialBoundary`
- explicit operation IDs and model-visible tool names
- exact input/output schema digests
- approval, target, retry, quota, retention, and deletion policies

### `CapabilityInstallationV1`

- exact `userId`, `packId`, and `catalogDigest`
- `ENABLED | PAUSED | REVOKED`
- policy revision, connection references, and kill-switch state

### `TurnCapabilityGrantV1`

- `sub`, `sessionId`, `runtimeArn`, `runtimeQualifier`, `invocationId`
- `releaseCommit`, `catalogDigest`
- allowed pack and operation IDs plus exact target-grant hashes
- `iat`, `exp`, `maxCalls`, and nonce

The grant is retained by the trusted relay. It is never rendered into a model
message or forwarded as a tool argument.

### `CapabilityCallV1` and `CapabilityResultV1`

The call binds `callId`, invocation, tool name, canonical arguments, and
`argsHash`. `callId` is deterministic from invocation, tool-use identity, and
argument hash. Results are exactly one of:

- `SUCCEEDED`
- `PENDING_APPROVAL`
- `DENIED`
- `FAILED_RETRYABLE`
- `UNCERTAIN`

The bounded result contains provenance/source references, a proposal or
receipt reference when applicable, a safe error code, and an explicit retry
policy. It never contains provider credentials or raw bearer grants.

### Supporting contracts

- `TargetGrantV1`: exact normalized target, method, redirect policy, expiry,
  use count, current request/invocation identity, tenant binding, and target
  hash.
- `ActionProposalV1`: user, capability, resource, connection, canonical
  arguments/hash, revision, originating invocation, approval policy, expiry.
- `EffectReceiptV1`: capability, resource, arguments hash, provider evidence
  identity/hash, execution and reconciliation time.
- `ScheduleSpecV1` and `ScheduleOccurrenceV1`.
- `ComputeJobSpecV1` and `ComputeReceiptV1`.
- `ConnectorManifestV1` and `ConnectorConnectionV1`.
- `PortableStateManifestV2`, `ImportPlanV1`, and `ImportReceiptV1`.

## Risk and approval model

Risk classes are:

- `LOCAL_READ`
- `LOCAL_MUTATION`
- `PUBLIC_READ`
- `PRIVATE_READ`
- `DURABLE_MUTATION`
- `EXTERNAL_EFFECT`
- `IRREVERSIBLE_EFFECT`

`STANDING` approval is a reserved enum and is rejected in v1. Payments and
other irreversible effects are unsupported.

| Capability | Risk | v1 approval and authority |
| --- | --- | --- |
| Workspace file read/list | `LOCAL_READ` | Existing scoped session; no prompt |
| Workspace file write/delete | `LOCAL_MUTATION` | Existing scoped session; no prompt |
| Exact public URL GET | `PUBLIC_READ` | Current-request `TargetGrantV1`; no second prompt |
| Schedule list | `LOCAL_READ` | No prompt |
| Schedule create/update/pause/cancel | `DURABLE_MUTATION` | Exact proposal then web/Telegram confirmation |
| Networkless compute | `LOCAL_MUTATION` | No prompt within fixed quotas; fresh output namespace |
| Connector read | `PRIVATE_READ` | Connection consent plus operation policy and intent grant where needed |
| Connector/browser write or submit | `EXTERNAL_EFFECT` | Persisted exact proposal and one-time approval |
| Import | `DURABLE_MUTATION` | Dry run then approval bound to complete bundle hash |
| Payment or irreversible effect | `IRREVERSIBLE_EFFECT` | Unsupported |

## Initial release catalog

The v1 catalog contains the existing four workspace tools and these additional
model-visible tools:

- `po_web_read`
- `po_schedule_list`
- `po_schedule_propose`
- `po_schedule_cancel_propose`
- `po_compute_run`
- `po_compute_status`

Google, MCP, and browser adapters are implemented behind the gateway but are
not model-visible or enabled for external pilots until their own staging and
human gates close. The full and warm-up runtimes must expose identical tool
names and schemas. Any catalog/runtime/gateway mismatch makes startup fail.

## Capability services

### Exact-target web reader

The reader performs only HTTP GET against a currently granted URL. It permits
public IPs only, pins DNS, applies bounded time, size, MIME, and redirect rules,
adds no cookies or authorization headers, and requires a new grant for a host
change. It returns sanitized untrusted text plus canonical URL, retrieval time,
content digest, and source reference. Denied targets cause zero network calls.

### Scheduler

The first task types are `REMINDER` and `READ_ONLY_AGENT_TURN`. Definitions are
revisioned and hash-bound. A deterministic occurrence ID deduplicates provider
retries. Stale generations after update or cancellation are no-ops. Exported
schedules import disabled. Deletion cancels live schedules and stale events
cannot recreate them.

### Networkless Linux compute

`ComputeJobSpecV1` binds an immutable image digest, bounded script or argv,
input file hashes, a fixed resource profile, a deadline, and `network: NONE`.
The sandbox uses a read-only root, non-root user, restricted syscalls and
capabilities, process/CPU/memory/disk limits, copied inputs, and a fresh output
directory. A trusted importer rejects traversal, symlinks, special files,
invalid UTF-8, unexpected types, changed hashes, and overflow before committing
outputs under `jobs/<jobId>/`.

### Connector kernel

Every adapter implements:

```text
read(context, operation, args)
prepare(context, operation, args) -> ActionProposalV1
dispatch(approved_action) -> EffectReceiptV1
reconcile(action) -> EffectReceiptV1 | None
revoke(connection_ref)
```

The existing Gmail behavior is retained through this interface. Runtime MCP
discovery is forbidden: reviewed tool schemas are frozen in
`ConnectorManifestV1`, and live schema drift pauses the connector. A synthetic
local MCP adapter proves isolation and drift handling without enabling a real
new connector.

### Browser boundary

The runtime execution role receives no AgentCore Browser authority. A later
trusted Browser Gateway owns browser sessions and profiles, exact target
grants, observation redaction, and credential injection. v1 may synthesize and
test this boundary, but authenticated browser profiles and model-visible
browser use remain disabled until a separate release gate closes. Every
submit, upload, send, or delete uses the same action and approval protocol as a
connector effect.

## Portable state v2

Export becomes content-addressed and versioned. It covers user-authored files,
structured memory, disabled schedule definitions, installed-pack metadata,
disconnected connector descriptors, compute receipts, and immutable effect
receipts. Every object has path, type, size, and SHA-256 coverage.

Credentials, active sessions, runtime internals, grants, approval tokens,
pending or uncertain effects, and provider authority are excluded. Import is
two-phase: strict parse/bounds/hash validation and dry-run plan, then exact
bundle-hash approval and staged compare-and-swap activation. Failure leaves the
live generation unchanged. Imported schedules are disabled, connectors are
disconnected, and past effects cannot replay.

## Staging release foundation

The staging release package owns:

- strict `RuntimeContextV3`, runtime-image evidence, trusted Lambda artifact,
  and staging-transaction contracts;
- a deterministic Lambda ZIP consumed by CDK with its exact hash;
- one private immutable-tag ECR repository for the bridge;
- build context, SBOM, provenance, scan, and signing evidence;
- direct CloudFormation-managed `AWS::BedrockAgentCore::Runtime` and
  `RuntimeEndpoint` resources bound to the immutable image digest;
- a durable release transaction with preflight, resume, status, and explicit
  rollback states.

Transaction states are:

```text
NEW -> PREFLIGHTED -> FOUNDATION_READY -> IMAGE_PUBLISHED
    -> RUNTIME_READY -> ENDPOINT_READY -> CONTEXT_WRITTEN
    -> CONSUMER_CHANGESETS_READY -> CONSUMERS_APPLIED -> VERIFIED
```

An ambiguous cloud mutation becomes `UNCERTAIN`; later phases do not run until
live state is reconciled. Local implementation tests use injected fake clients
and make no AWS call.

## Consumer pilot experience

The pilot journey is:

```text
opaque invite -> Telegram welcome -> one-time web link
-> read-only Gmail OAuth -> /scan -> source-backed cards
-> local draft/edit -> useful/not-useful feedback
-> workspace/export/delete
```

Invites use one-time opaque Telegram deep links and store only token digests.
All browser destinations use five-minute, single-use, allowlisted-return-path
tickets. The mobile web shell shows connection, last scan, workspace, runtime,
capability, export, and deletion state. Pilot accounts always serialize
`externalEffects: false` and never render send or request-approval controls.

Founder-only effect testing remains a separate optional release lane. A local
draft edit atomically stales any prior pending approval. The approval page
shows the exact account, recipient, subject, body, source, revision, payload
hash, expiry, and one-send warning.

## Privacy-safe observability

Metrics use bounded dimensions only: environment, component, operation, and
outcome. They may cover invite, OAuth, scan, card, feedback, draft, export,
deletion, queue, maintenance, schedule, compute, capability denial, connector
drift, and uncertain effects. They may not contain user IDs, provider IDs,
addresses, source IDs, subjects, bodies, excerpts, URLs, model input/output,
workspace content, tokens, or credentials.

## Completion gates

Local implementation completion requires:

1. Every new behavior was driven by a failing test and has recorded focused
   green evidence.
2. The immutable catalog, runtime plugin, warm-up path, relay, and gateway have
   exact parity.
3. Cross-user Cartesian tests show no grant, connection, schedule, compute,
   output, import, or provider-data crossover.
4. Denied URL targets cause zero DNS/HTTP calls; compute has no network or
   credential source; schedules cannot execute external effects.
5. Existing Gmail exact-approval and `UNCERTAIN` behavior remains green.
6. Export/import secret scans and replay tests pass.
7. The full local aggregate suite, production web build, Python and Node syntax,
   offline CDK synth, and cdk-nag pass from a clean checkout.
8. An independent hostile review reports no unresolved high-severity finding.

Cloud and pilot completion additionally require retained exact-commit artifact
and account evidence. Source completion alone must be reported as implemented
and locally verified, not deployed or production-ready.
