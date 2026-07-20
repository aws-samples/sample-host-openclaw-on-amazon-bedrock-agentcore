# Personal Operator v1 Privacy Boundary

This document describes the implemented local boundary and the remaining
deployment claims. It is not a privacy policy and does not claim that a cloud
deployment has been verified.

## v1 catalog and implementation boundary

The frozen source catalog names the four workspace tools `po_file_list`,
`po_file_read`, `po_file_write`, and `po_file_delete`, plus `po_web_read`,
`po_schedule_list`, `po_schedule_propose`, `po_schedule_cancel_propose`,
`po_compute_run`, and `po_compute_status`. The runtime package, plugin manifest,
trusted relay, admission gateway, and schemas have exact local
release/catalog/schema parity for those ten tools. That source integration is
not deployment evidence: runtime image publication, IAM/network behavior,
AgentCore readiness, and every real provider gate remain open.

The catalog does not place provider, browser, database, approval-signing, or
cross-user credentials in OpenClaw, model input, workspace, grants, tool
arguments, results, or logs. The target reader is credential-free and bound to
an exact current-request public URL grant. Schedule authority remains in the
trusted control plane. Active production composition injects no compute
adapter, staging path, launcher, or collector: both compute operations return
`ADAPTER_DISABLED`. The retained same-interpreter runner fences are defense in
depth, not an isolation boundary; image, launcher, and live isolation gates
remain open. Task 8 operational completion remains OPEN. Durable mutations
remain exact proposals requiring one-time approval; standing approval and
irreversible effects are rejected.

Catalog retention and deletion fields are mandatory admission metadata, not a
claim that deletion execution is implemented by the catalog compiler. Live
services must still strong-read the account-deletion fence at their last
application-controlled point, cancel stale work, and purge under the lifecycle
described below. The detailed per-tool credential holder, authority decider,
executor, retry, quota, retention, and deletion rules are frozen in
`docs/CAPABILITY-BOUNDARY.md`.

## Trust split

The Telegram router, ordered worker, web control surface, OAuth adapters,
approval service, capability gateway, and provider adapters form the trusted
application plane. They may handle identity, encrypted provider tokens, action
state, and delivery/effect receipts.

OpenClaw is a replaceable per-user conversational runtime. It receives only a
canonical internal user ID, a temporary AWS session restricted to that user's
S3 namespace, model/runtime configuration, and exactly ten curated tools. The
upstream `session_status` built-in is explicitly denied because it can persist
a model/provider override. The visible catalog and primary selection contain
only the loopback `agentcore/bedrock-agentcore` route, with no fallback. Its
gateway is loopback-only. The runtime itself has no arbitrary network,
Telegram delivery, Google, OpenAI ranking, DynamoDB, approval-signing, Gmail
send, arbitrary shell, browser, direct scheduler, dynamic MCP, marketplace, or
plugin-install authority. `po_web_read` crosses a separate exact-target trusted
reader under a current-request grant. Schedule tools cross the trusted control
plane, and scheduled turns remain read/propose-only. Both compute tools return
`ADAPTER_DISABLED` because production composition contains no compute adapter
or launcher.

AgentCore's platform-level command and interactive-shell APIs are outside the
model tool catalog and would share the container filesystem and environment.
Retained resource policies on both the runtime and immutable endpoint therefore
deny both actions for every principal, and the live evidence adapter requires
the exact deny documents.

The temporary session is not self-issued. The runtime role cannot assume the
workspace role or construct a session policy; it can only invoke the exact
trusted credential broker. The ordered worker mints an HMAC-bound capability
after acquiring the exact runtime session. The broker strongly rereads live
runtime state, requires the same user, session, runtime ARN, and immutable
release qualifier, derives the S3/KMS policy from trusted configuration, and
issues credentials for at most 15 minutes. The capability is consumed by
trusted bridge code and is never included in model input or child-process
environment.

Provider data never becomes authority merely because a model read it. Gmail
text is untrusted input. A model can rank known source
IDs or propose content; it cannot approve, dispatch, confirm, or reconcile an
effect.

## Data flows

### Telegram

The public webhook accepts a bounded signed update. The router resolves the
platform actor to a server-owned internal user, derives one immutable event ID,
and enqueues an exact FIFO envelope. The runtime sees only the allowlisted
channel, actor delivery label, and message; it never sees the bot token, webhook
secret, chat delivery credential, or caller-selected runtime session ID.

Opportunity buttons use opaque, one-time, tenant/chat/actor-bound handles. The
worker checks the account-deletion intent before creating any processing-ledger
state. After claiming an outbox delivery, it strongly rechecks that intent at
the last application-controlled point before calling Telegram. A present or
unavailable fence produces no provider call and an uncertain delivery record.
v0 deliberately keeps one Telegram provider call inside the durable outbox and
therefore does not also call `answerCallbackQuery`; Telegram's button spinner
can linger briefly even though the follow-up message is sent.

### Gmail read-only pilot

The trusted scanner requests at most 50 sent threads in the 3-to-30-day window.
It requires Gmail's `SENT` label, excludes automated/bulk correspondents, and
extracts a bounded derived excerpt. Raw message bodies are transient parser
input and are neither persisted nor included in application logs.

The ranker receives only bounded source ID, correspondent, subject, excerpt,
and waiting time. The OpenAI Responses request uses structured output and
`store:false`. Output is rejected if it is incomplete, refused, malformed,
duplicates a source, or names an ID not supplied by the application.

### Gmail founder effect

Read-only OAuth and founder-send OAuth are separate capabilities. A send is
limited to one plain-text recipient with no CC, BCC, HTML, or attachment. The
grant binds user, action, draft revision, exact Google connection/account,
capability, canonical payload hash, approval ID, and expiry. Confirmation
requires exact provider `SENT` evidence and creates an effect receipt plus a
waiting-for-reply tracker. A timeout becomes `UNCERTAIN` and is not retried.
After claiming the dispatch fence, the executor strongly rechecks account
deletion at the last application-controlled point before calling Gmail. A
present or unavailable fence produces no send and a no-resend `UNCERTAIN`
record.

### Web control surface

A Telegram connect URL carries a signed, one-time, five-minute ticket. The
browser exchanges it for an opaque `Secure`, `HttpOnly`, `SameSite=Lax`,
host-only cookie and a separate CSRF token. Stores retain only HMAC digests, not
bearer values. Approval GET is a read-only preview; approve, reject, and delete
are matching-user CSRF-protected POST operations.

Exports are deterministic bounded ZIP files containing only user-authored
workspace files plus the `memory`, `schedules`, and `receipts` record classes.
Paths, counts, entry sizes, total size, and user namespace are validated. v0
delivers the archive synchronously and rejects any final ZIP above 4 MiB so
base64 encoding and the proxy envelope remain below Lambda's response limit.

## Stored data and current lifecycle

| Class | Storage boundary | Implemented lifecycle |
|---|---|---|
| One-time connect ticket | DynamoDB digest record | Logical expiry, maximum 10 minutes; consumed once |
| Web session | DynamoDB digest record | Logical expiry, maximum 7 days; default 24 hours; user-wide revocation marker |
| OAuth state/PKCE/nonce | Trusted control table | Exact one-time state binding and logical expiry |
| Google refresh/access tokens | Read-only tokens use a KMS envelope in the trusted record store; the one founder-send credential uses its own Secrets Manager secret | Local read-only records are deleted on account deletion. Deleting the exact founder account first schedules the deployment-managed send secret for deletion with Secrets Manager's 7-day recovery window. The remote Google grant is not revoked by v0 |
| Gmail raw body | Process memory in trusted scanner | Transient only; never intentionally persisted or logged |
| Derived Gmail source, opportunity, draft | Trusted control table | Exact 14-day TTL and application-side logical-expiry checks |
| Telegram callback authority | Trusted control table under the exact user partition | One-time use and 14-day TTL |
| Action, approval, receipt, reply tracker | Trusted control table | Executable/nonterminal records expire after 14 days; terminal and no-resend `UNCERTAIN` records expire after 90 days; logical expiry is enforced before DynamoDB cleanup |
| Telegram processing/delivery ledger | Dedicated DynamoDB table | Terminal and uncertain outcomes receive a 90-day TTL; account deletion also removes indexed user rows |
| Identity anti-recreation markers | Identity DynamoDB table | Permanent SHA-256 markers for the canonical internal user ID and each deleted canonical channel key; they retain no raw user or channel ID and are used only to prevent post-deletion registration or binding |
| Telegram update queue | Encrypted SQS FIFO and DLQ | Main queue retains messages for at most 4 days; dead-letter messages for at most 14 days; account deletion cannot selectively erase an already-enqueued message |
| Workspace snapshot | Versioned, KMS-encrypted user S3 namespace | Current and parent generations retained for recovery; 30 days of inactivity triggers runtime/workspace-only purge; account deletion aborts incomplete multipart uploads and removes every user object version and delete marker after runtime purge |
| Web assets and access logs | Separate blocked-public-access S3 buckets | Versioned assets; bounded noncurrent/log lifecycle in the synthesized stack |
| Runtime and application logs | CloudWatch | Context-configured 30-day retention; payload-rich ADOT application observability is disabled and application-emitted payload/secret logging is prohibited |
| DynamoDB recovery history | Point-in-time recovery | Historical table recovery follows AWS PITR retention and is not selectively rewritten by account deletion |

The active observability stack uses aggregate AWS service metrics for its
dashboards and alarms. It does not enable model invocation text or image
payload logging. The runtime sets `DISABLE_ADOT_OBSERVABILITY=true` so
payload-rich AgentCore application observability is disabled; ordinary
platform operational logs remain. The archived legacy token-monitoring stack
is not active, and live CloudWatch inspection remains OPEN.

The hourly maintenance path performs bounded action reconciliation, TTL
cleanup, account-deletion finalization, and inactive-workspace cleanup. Local
tests prove the transitions and retention fields; they do not prove that an
AWS schedule ran on time. No deletion-completion or retention SLA is claimed
until the deployed schedule and alarms have staging evidence.

## Account deletion

Deletion is asynchronous and intentionally uses two passes:

1. A strongly read, hashed account-deletion intent is persisted before any
   fallible operation. Session authentication and the ordered worker check this
   fence and refuse new work.
2. The trusted plane revokes web sessions and local provider authority, stops
   and tombstones the runtime, aborts every listed incomplete multipart upload
   below the exact user S3 namespace, requires exact S3 deletion evidence for
   every version and delete marker, and deletes user records from the control,
   identity, callback, and processing-ledger stores. Any malformed, repeated,
   cross-namespace, or ambiguous S3 result leaves deletion pending. For the one
   configured founder identity, it first schedules the exact founder-send
   Secrets Manager secret for deletion
   with a 7-day recovery window; an ambiguous secret-management result leaves
   deletion pending. Ambiguous runtime purge also leaves deletion pending and
   fails closed.
   Before scanning identity rows, it strongly proves a permanent hashed user
   marker in the identity table. Registration, bind-code creation, and bind-code
   redemption condition their atomic writes on that marker being absent. For
   every owned channel, one DynamoDB transaction establishes the hashed channel
   marker and deletes both the forward mapping and its exact `ALLOW` invitation;
   a remapped channel is preserved. A failed or ambiguous transaction keeps
   deletion pending unless strong reads prove the complete atomic result.
3. A `FINALIZING` fence starts a minimum 30-minute credential-and-invocation
   drain period.
   Hourly maintenance then repeats the complete purge to remove any late write
   from work that was already running, and only afterward replaces the intent
   with a minimal completed tombstone containing no raw user ID.

The completed tombstone prevents an old session or stale workspace snapshot
from recreating the account. This disables active application authority, but
does not erase every historical byte immediately: the founder-send secret
remains recoverable by deployment operators during the 7-day Secrets Manager
window; already-enqueued SQS/DLQ payloads, retained logs, S3 access logs, and
DynamoDB recovery history age out under the periods listed above. v0 does not
call Google's remote token-revocation endpoint; the user must also remove
Personal Operator in Google Account settings. No deployed completion SLA is
claimed.

The permanent identity markers are pseudonymous, not anonymous: SHA-256 of a
known candidate identifier can be compared with a marker. They intentionally
have no TTL because removing them would permit account or channel recreation.
They contain no display name, raw internal user ID, raw channel ID, provider
credential, or user content.

The final Gmail and Telegram checks stop effects that have not started. They
cannot recall an HTTPS request that already crossed the provider-dispatch
boundary or invalidate a Google access token already held by a running
process. The 30-minute drain and second purge remove late local writes; they do
not reverse an external provider effect.

## Logging exclusions

Application-emitted runtime and router message fields contain only closed
metadata: schema, component or event code, severity, bounded count, and
allowlisted status. Console arguments, child stdout/stderr, identities, paths,
provider errors, exception messages and stacks, prompts, workspace bytes, and
responses are discarded before the application emits a message. AWS Lambda
and other platform envelopes/system records still add operational request,
timing, and lifecycle metadata. AgentCore's payload-rich ADOT application
observability is explicitly disabled with `DISABLE_ADOT_OBSERVABILITY=true`;
ordinary platform operational logs remain. CloudWatch is not a
response-inspection transport, and exact live retained-field inspection
remains OPEN. The former E2E log tailer now fails before constructing a
CloudWatch client; live response checks must use direct AgentCore invocation
evidence, while provider/message journeys remain OPEN.

Never log or include in release evidence:

- raw email bodies or full provider responses;
- OAuth authorization codes, access/refresh tokens, client secrets, or PKCE
  verifier values;
- Telegram bot tokens or webhook secrets;
- web cookies, CSRF tokens, one-time connect tickets, or approval bearer tokens;
- scoped AWS credential files or execution-role credentials;
- decrypted workspace contents or another user's identifiers.

That list is an exclusion boundary, not permission to add identifiers to
application log messages. Application-emitted runtime/router fields are limited
to the exact closed set above; platform envelopes are separately constrained
and must be inspected live. Typed state, receipt, and trace identifiers may
appear only inside their existing trusted non-log contracts and stores when
those contracts require them; they are not application-emitted log fields or
release-report dimensions.

## Locally tested, not deployment-proven

Local tests cover three-user Cartesian namespace isolation, 100 duplicate
updates, exact runtime child-environment filtering, exact S3/KMS session
policy generation, prompt-injection source membership, approval and provider
fault transitions, bounded export, deletion ordering, and session revocation.

They do not prove AWS IAM enforcement, AgentCore microVM isolation, KMS key
policy behavior, S3 consistency, CloudFront/API logging, Google or Telegram
provider behavior, OpenAI retention policy, image vulnerability status, or
real deletion latency. Those remain staging and provider-assurance gates.
