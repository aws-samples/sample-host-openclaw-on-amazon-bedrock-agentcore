# Task 5 report: invite-only consumer journey

## Outcome

Task 5's external-pilot slice is implemented and locally verified on branch
`codex/po-v1-experience`. It remains pre-production and was not deployed. The
implementation used synthetic/public inputs only and made no provider, AWS, or
other cloud call during the journey or verification gates.

- Base: `7e44b6684ac0cf4965ce734664ba13b60fdb7a59`
- Verified implementation head: `50a08e7ad3afe952b18736d479b5fbda66acbd8c`
- Required final subject: `feat(pilot): add invite-only consumer journey`

## Delivered contracts

### Invite and ingress

- Added exact `poi1_<32 base64url>` invitations with a seven-day default,
  bounded issuance TTL, digest-only persistence, atomic identity creation and
  redemption, same-actor idempotent reconciliation, revocation, expiry, race,
  replay, and deletion-tombstone handling.
- Added a local operator CLI for issue, revoke, and inspect operations. The
  bearer is returned only to the issuer; storage and reconciliation use its
  SHA-256 digest.
- Telegram `/start <invite>` is redeemed before ordinary identity resolution.
  Only canonical `/start` crosses FIFO; the invite bearer does not enter queue
  bytes, logs, metrics, or persisted identity records.
- External-pilot router synthesis fails closed if open registration is enabled.

### Bound browser sessions

- Upgraded production issuance to signed v2 tickets with an exact five-minute
  lifetime, one-time atomic consumption, and return-path binding.
- Allowed destinations are `/`, `/connections`, `/workspace`, `/export`,
  `/delete`, and one bounded `/workspace?draft=<opaque-id>` form. Legacy v1
  drain is restricted to `/connections`.
- A live session cannot be replaced by another user's ticket, and a denied
  cross-user attempt does not consume the intended user's ticket.
- Every Telegram browser destination now uses a one-time ticket, including
  welcome, status, workspace, export, delete, scan fallback, and draft edit.

### Read-only pilot surface and lifecycle

- Added authenticated `GET /api/overview`, local Gmail disconnect, current
  session logout, typed scan feedback, and the corresponding CloudFront/API
  routes.
- The overview projects only typed state and counts: connection/re-auth state,
  latest scan, runtime, workspace receipt, file/opportunity/draft counts,
  capability, export, and deletion. It always emits
  `externalEffects: false`; an authorization scan failure projects
  `REAUTH_REQUIRED` without a provider call.
- The mobile web shell renders source-backed Gmail cards, local immutable draft
  edits, feedback, workspace/runtime receipt, deterministic export, disconnect,
  logout, and deletion. Pilot views expose no send or request-approval control.
- Export copy now says deterministic, unencrypted ZIP. Deletion copy states the
  minimum 30-minute reconciliation floor.
- The founder-only approval route remains a separate optional lane, as required
  by the frozen design; it is not reachable from the pilot overview.

### Privacy-safe state and callback behavior

- Added bounded `RUNNING`, `SUCCEEDED`, `EMPTY`, and `FAILED` scan state plus one
  `USEFUL` or `NOT_USEFUL` bit per terminal successful scan.
- Scan partitions use an HMAC-pseudonymous user key and contain no raw identity,
  provider/source ID, address, subject, body, excerpt, or URL. They have a
  30-day TTL and are included in bounded account deletion.
- Telegram callback envelopes now carry a bounded `callbackQueryId`. The worker
  best-effort acknowledges it with fixed text before business processing;
  acknowledgement failure is independent of the exactly-once business ledger.
- The provider-free synthetic journey exercises three isolated pilots from
  opaque invite through welcome, web connect, synthetic read-only OAuth, scan,
  source card, local edit, feedback, overview, workspace, deterministic export,
  disconnect/logout, and deletion reconciliation. It records zero external
  effect calls.

## TDD and regression evidence

Production changes were introduced behind focused red tests for invitation
races/replay/expiry/revocation/tombstones, ticket expiry/replay/return paths,
overview and measurement schemas, callback acknowledgement, lifecycle routes,
UI effect exclusion, mobile navigation, and wording.

The final synthetic test first failed on an exact copy assertion and then
passed after matching the product's safer wording (`No button sends email`).
The first aggregate run then exposed one stale pre-existing replay fixture:
`TelegramWebhookIngress` now requires an invite redeemer, but the fixture had
not supplied one. The constructor remained strict; the fixture received a
fail-if-called redeemer for its ordinary-message path. Its reproducer and the
combined integration slice then passed.

## Fresh verification

The exact implementation head was verified with:

```text
PATH=/opt/homebrew/opt/node@24/bin:$PATH \
PYTHON=/Users/konstantin.tuzikov/Documents/personal-operator/.venv/bin/python \
./scripts/test-local.sh
```

Result: exit `0`, `All local checks passed.` The script provided:

- Python unit/security/integration: `1007 passed, 10 subtests passed`;
- E2E session-control: `11 passed`;
- bridge Node suite: `313 passed`, `0 failed`;
- web UI: `11 passed`;
- Node 24 production build: passed;
- JavaScript and Python syntax: passed;
- repository whitespace: passed;
- hermetic offline CDK synthesis: passed;
- cdk-nag contract: passed.

A focused post-fix integration run also reported `11 passed` for the synthetic
pilot and replay/tenant-isolation files.

## Visual verification limitation

The in-app browser had no available browser surface (`browser list` returned
none), so no interactive visual/browser claim is made. I did not switch to an
unapproved browser backend. Mobile reachability is instead covered by the
static breakpoint guard and jsdom component tests; the Node 24 production build
is also fresh. A later human or available in-app-browser pass should still
inspect the built shell at narrow and wide viewports.

## Serial dependency retained for central integration

The brief's founder-only rule that a local draft edit must atomically stale an
existing pending approval is intentionally not implemented in this branch. It
overlaps Task 3's action transaction and must be resolved centrally when Task 3
is integrated, as required by Integration gate A. External pilots remain
read-only and never receive approval or send authority in this Task 5 slice.

## Commits

1. `12261402d8acd871bca0f515f4f24f967e1773c5` — opaque one-time invites
2. `32df9c10f24d6bed4cfb38b299ac8703190d7ddc` — Telegram invite redemption
3. `c6206f71554e0d8e00de52f734640143693ac07d` — return-path-bound tickets
4. `b8b4aaef2a59ba12a0e831e0b793575f6ca0c14a` — overview and lifecycle
5. `f6e8cd6f7154afdfed134951f1a36d11df14e388` — independent callback ACK
6. `c0aaee8a63582e1b9b53235e48d0d62ec1aad2ae` — read-only mobile UI
7. `50a08e7ad3afe952b18736d479b5fbda66acbd8c` — three-pilot synthetic journey
