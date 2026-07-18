# Personal Operator v1 Capability Boundary

This document describes the frozen source contracts and release catalog. It is
local implementation evidence only: it does not claim that the six v1 tools are
installed in OpenClaw, admitted by a gateway, deployed, or production-ready.

## Canonical contract rule

Capability contracts are dependency-free, immutable values. Their wire form is
sorted-key UTF-8 JSON with no whitespace or trailing newline. Parsing rejects
duplicate keys, alternate spellings, unknown fields, noncanonical bytes,
non-finite or unsafe numbers, booleans in integer fields, unsafe identifiers or
paths, and configured byte, depth, string, collection, and total-node overflow.
Repository catalog/schema artifacts use that encoding plus one trailing LF.

`catalogDigest` is non-self-referential: it is SHA-256 of the compiled canonical
catalog with only `catalogDigest` omitted. Each input/output schema digest is
SHA-256 over the exact canonical schema artifact bytes, including its single
trailing LF. The compiled catalog binds one exact lowercase 40-character release
commit. A release, catalog, schema, runtime, or gateway mismatch must fail closed.

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
| `po_web_read` | `PUBLIC_READ`; current-request target grant, never standing approval | No credential is held; gateway validates the exact current authenticated request, normalized public HTTPS target, expiry, and use count | Trusted network reader performs bounded GET only; read-only retry remains bound to the same live grant | No durable content retention; `4 / 4 KiB / 64 KiB`; deletion fence revokes grants and purges references |
| `po_schedule_list` | `LOCAL_READ`; no prompt | Trusted control plane owns schedule records; gateway strong-reads user, installation, catalog, and deletion state | Scheduler control adapter reads only; read-only retry | Control record, 90 days; `8 / 256 KiB / 1 MiB`; deletion fence cancels schedules and purges records |
| `po_schedule_propose` | `DURABLE_MUTATION`; exact one-time proposal approval | Trusted control plane holds schedule authority; proposal binds user, definition/hash, revision, invocation, and expiry | Scheduler control adapter applies only an approved persisted proposal; retries require the same dedupe key and uncertainty stops for reconciliation | Control record, 90 days; `8 / 256 KiB / 256 KiB`; deletion fence cancels schedules and purges records |
| `po_schedule_cancel_propose` | `DURABLE_MUTATION`; exact one-time proposal approval | Trusted control plane holds schedule authority; proposal binds exact user, schedule revision, invocation, and expiry | Scheduler control adapter cancels only the approved revision; retries require the same dedupe key and uncertainty stops for reconciliation | Control record, 90 days; `8 / 256 KiB / 256 KiB`; deletion fence cancels schedules and purges records |
| `po_compute_run` | `LOCAL_MUTATION`; no prompt inside fixed quota | No provider credential enters the job; gateway validates user, image digest, copied input hashes, resource profile, deadline, and deletion fence | Trusted compute service launches a disposable networkless sandbox and imports validated outputs; retries require the same job dedupe key | Job receipt, 90 days; `2 / 1 MiB / 1 MiB`; fence, cancel, and purge job/input/output state |
| `po_compute_status` | `LOCAL_READ`; no prompt | Trusted compute service owns job records; exact user/job namespace and deletion state decide authority | Compute control adapter reads status/receipts only; read-only retry | Job receipt, 90 days; `8 / 256 KiB / 1 MiB`; fence, cancel, and purge job/input/output state |

The catalog contains exactly these ten tools: the existing four workspace tools
and the six named v1 tools. Connector, MCP, browser, generic shell, generic
capability-call, plugin-install, and payment tools are absent. Catalog metadata
is an admission contract, not an authority source by itself: later gateway and
adapter code must strong-read live installation and deletion state at the last
application-controlled point before execution.
