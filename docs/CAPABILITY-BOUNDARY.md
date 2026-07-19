# Personal Operator v1 Capability Boundary

This document describes the frozen source contracts, exact ten-tool release
catalog, runtime plugin, and trusted admission boundary. The integrated source
contains exactly ten model-visible `po_*` tools and locally tested gateway
adapters. This is local implementation evidence only; it does not prove an AWS
deployment, live AgentCore authority, provider readiness, or production safety.

## Canonical contract rule

Capability contracts are dependency-free, immutable values. Their wire form is
sorted-key UTF-8 JSON with no whitespace or trailing newline. Parsing rejects
duplicate keys, alternate spellings, unknown fields, noncanonical bytes,
all floating-point values, unsafe integers, booleans in integer fields,
container cycles, unsafe identifiers or paths, and configured byte, depth,
string, collection, and total-node overflow.
Repository catalog/schema artifacts use that encoding plus one trailing LF.

`catalogDigest` is non-self-referential: it is SHA-256 of the compiled canonical
catalog with only `catalogDigest` omitted. Each input/output schema digest is
SHA-256 over the exact canonical schema artifact bytes, including its single
trailing LF, and direct catalog parsing requires every digest in its reviewed
operation-specific position. The compiled catalog binds one exact lowercase
40-character release commit. A release, catalog, schema, runtime, or gateway
mismatch must fail closed.
Capability calls bind the catalog digest, exact operation/tool pair, invocation,
tool-use identity, and canonical argument hash into `callId`; operation-specific
input, output, proposal, and schedule shapes reject all extra fields. A result is
accepted for use only after contextual validation against its originating call;
that path binds the full call identity, operation-specific output identity, and
the allowed receipt/provenance shape.

Schedule proposals remain bound to the frozen catalog operation and tool.
Connector proposals require a separate contextual constructor that binds a
self-digesting connector manifest, one active unfenced connection, the exact
PREPARE operation, trusted resource, and trusted normalized display arguments.

Public URL grants accept only canonical HTTPS authorities. Non-global IP
literals, IPv4-mapped IPv6, numeric and hostname aliases, userinfo, ports,
fragments, and ambiguous encodings fail closed. The target hash covers the full
grant except the hash itself, including expiry and maximum uses. This parser
boundary does not replace execution-time DNS resolution and address pinning.

The trusted relay retains `TurnCapabilityGrantV1`; the model sees neither that
grant nor provider, browser, database, approval-signing, or cross-user
credentials. `STANDING` approval is reserved and rejected. No catalog entry
supports payments or another irreversible effect.

## Frozen catalog responsibilities

Every row is one catalog pack and one model-visible tool. Quotas below are
per-turn call/input/output byte limits. `STOP_AND_RECONCILE` applies to every
ambiguous effect; it never means blind replay.

| Tool | Risk and approval | Credential holder and authority decision | Executor and retry | Retention, quota, and deletion |
|---|---|---|---|---|
| `po_file_list` | `LOCAL_READ`; no prompt; current scoped workspace session | Trusted workspace broker holds the short-lived scoped AWS session; exact user namespace and live session decide authority | Workspace adapter lists only the bound namespace; read-only retry | Workspace lifecycle, 30 days; `8 / 256 KiB / 1 MiB`; deletion fence then purge with workspace |
| `po_file_read` | `LOCAL_READ`; no prompt; current scoped workspace session | Trusted workspace broker holds the scoped session; exact relative path plus namespace decide authority | Workspace adapter reads one bounded UTF-8 file; read-only retry | Workspace lifecycle, 30 days; `8 / 256 KiB / 256 KiB`; deletion fence then purge with workspace |
| `po_file_write` | `LOCAL_MUTATION`; no second prompt within the scoped session | Trusted workspace broker holds the scoped session; exact user namespace, path, and quota decide authority | Workspace adapter creates/replaces exact bytes; retry is idempotent only for the same bound input | Workspace lifecycle, 30 days; `8 / 256 KiB / 256 KiB`; deletion fence then purge with workspace |
| `po_file_delete` | `LOCAL_MUTATION`; no second prompt within the scoped session | Trusted workspace broker holds the scoped session; exact user namespace and path decide authority | Workspace adapter deletes one exact path; same-input retry is idempotent | Workspace lifecycle, 30 days; `8 / 256 KiB / 256 KiB`; deletion fence then purge with workspace |
| `po_web_read` | `PUBLIC_READ`; current-request target grant, never standing approval | No credential is held; gateway validates the exact current authenticated invocation and tenant-partitioned grant, normalized public HTTPS target, expiry, and use count | Trusted network reader performs bounded GET only; read-only retry remains bound to the same live grant | No durable content retention; `4 / 4 KiB / 64 KiB`; deletion fence revokes grants and purges references |
| `po_schedule_list` | `LOCAL_READ`; no prompt | Trusted control plane owns schedule records; gateway strong-reads user, installation, catalog, and deletion state | Scheduler control adapter reads only; read-only retry | Control record, 90 days; `8 / 256 KiB / 1 MiB`; deletion fence cancels schedules and purges records |
| `po_schedule_propose` | `DURABLE_MUTATION`; exact one-time proposal approval | Trusted control plane holds schedule authority; proposal binds user, definition/hash, revision, invocation, and expiry | Scheduler control adapter applies only an approved persisted proposal; retries require the same dedupe key and uncertainty stops for reconciliation | Control record, 90 days; `8 / 256 KiB / 256 KiB`; deletion fence cancels schedules and purges records |
| `po_schedule_cancel_propose` | `DURABLE_MUTATION`; exact one-time proposal approval | Trusted control plane holds schedule authority; proposal binds exact user, schedule revision, invocation, and expiry | Scheduler control adapter cancels only the approved revision; retries require the same dedupe key and uncertainty stops for reconciliation | Control record, 90 days; `8 / 256 KiB / 256 KiB`; deletion fence cancels schedules and purges records |
| `po_compute_run` | Catalog contract only; operational approval and risk boundary remain unverified | Active production composition injects no compute adapter or credential authority | Returns `ADAPTER_DISABLED`; no staging, launch, or collection transport is active | Intended receipt/quota/deletion contract is source-local only; image, launcher, live isolation, and Task 8 operational completion remain OPEN |
| `po_compute_status` | Catalog contract only; no active job exists to inspect | Active production composition injects no compute adapter or job-store authority | Returns `ADAPTER_DISABLED`; the retained status adapter is a local harness only | Intended receipt/quota/deletion contract is source-local only; image, launcher, live isolation, and Task 8 operational completion remain OPEN |

The catalog contains exactly these ten tools: the existing four workspace tools
and the six named v1 tools. Connector, MCP, browser, generic shell, generic
capability-call, plugin-install, and payment tools are absent. Catalog metadata
is an admission contract, not an authority source by itself: later gateway and
adapter code must strong-read live installation and deletion state at the last
application-controlled point before execution.

AgentCore also exposes platform-level one-shot command and interactive-shell
APIs independently of the model catalog. The release stack attaches retained
resource policies to both the runtime and immutable endpoint that explicitly
deny both actions for every principal. Live context collection accepts only the
exact deny documents; an absent, malformed, permissive, partial, or retargeted
policy fails closed.

The retained compute service, runner, transport protocols, Dockerfile, and
standalone `ComputeStack` are inactive reference material. `app.py` does not
instantiate that stack or require a compute image digest. Same-interpreter
Python API fences are defense in depth, not an isolation boundary. The active
capability composition returns `ADAPTER_DISABLED`; Task 8 operational
completion remains OPEN.
