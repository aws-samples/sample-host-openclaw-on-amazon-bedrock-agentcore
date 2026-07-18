# Task 5 report: invite-only consumer journey

## Outcome

Task 5's external-pilot slice and its review remediation are implemented on
branch `codex/po-v1-experience`. The executable/test subject below passed the
complete local gate. It remains pre-production and was not deployed, pushed,
or connected to a provider, AWS account, or other cloud runtime.

- Base: `7e44b6684ac0cf4965ce734664ba13b60fdb7a59`
- Verified implementation head: `4c707ae679c76ca98312009672add3d12260f0b8`
- Verified implementation tree: `bcea768d95702f426832b1c7f11e4a7d6672263f`
- Original required subject: `feat(pilot): add invite-only consumer journey`
- Review status: all ten reproduced Important findings have implementation
  fixes and regression coverage; independent final re-review is intentionally
  left to the central integration owner.

The report commit follows the verified subject and changes documentation only.

## Delivered contracts

### Invite, ingress, and browser return

- Exact `poi1_<32 base64url>` invitations have bounded issuance, digest-only
  persistence, atomic identity creation/redemption, same-actor idempotency,
  revocation, expiry, race, replay, and deletion-tombstone handling.
- Production classifies only exact Dynamo conditional cancellation reasons as
  invite rejection. Throttling, conflicts, service errors, transport loss, and
  unproven cancellation reasons remain retryable store failures.
- Every Telegram-controlled text, caption, first name, and username is checked
  for an invite bearer before identity resolution or persistence. Only the
  canonical `/start <invite>` form is consumed, and the bearer never enters
  FIFO bytes, logs, metrics, identity records, or the model path.
- Signed v2 browser tickets are exact-user, one-time, five-minute, and bound to
  an allowlisted return path. A live foreign session cannot consume a ticket;
  an expired, revoked, or malformed cookie can be safely replaced without
  hiding storage outages.

### Read-only surface and privacy lifecycle

- Authenticated overview, workspace, local draft editing, scan feedback,
  disconnect, logout, export, and deletion are exposed through the bounded
  mobile shell. Pilot views contain no send or approval control and always
  project `externalEffects: false`.
- Successful logout clears CSRF and all private React state, unmounts the
  authenticated route tree, and replaces navigation with a dedicated signed-
  out shell.
- Scan measurements are HMAC-pseudonymous, content-free, TTL-bounded, and
  deleted page by page. A population above 4,000 rows makes bounded progress
  and resumes on the next pass instead of repeating a zero-progress failure.
- Callback acknowledgement remains best-effort and independent of the exactly-
  once business ledger.

### Authoritative Gmail disconnect fence

- The typed `GMAIL#CONNECTION_FENCE` generation is authoritative for OAuth,
  refresh-token envelopes, scans, opportunities, draft revisions, Telegram
  callback handles, workspace reads, and card consumption.
- Generation-bound writes use Dynamo `TransactWriteItems`: a fence
  `ConditionCheck` and target `Put` either commit together before disconnect or
  fail after the generation advances. A stale writer cannot overwrite or
  compensating-delete a newer generation.
- Connection activation is transactionally bound to the exact envelope
  generation and cannot revive a `DISCONNECTING` fence. Callback creation uses
  the same atomic boundary.
- Every fresh disconnect, including one starting from `DISCONNECTED`, advances
  the generation and enters `DISCONNECTING`; only a retry of the already-
  pending generation reuses it. Completion is conditional on that exact
  `DISCONNECTING` state.
- Disconnect boundedly purges the envelope, opportunities, all draft
  revisions, and all Telegram callback handles. A failed delete is accepted
  only after a successful consistent read proves absence. Delete/read
  uncertainty remains pending and never reports `DISCONNECTED`.

### Provider-free synthetic journey

- The journey runs real Telegram ingress, local FIFO, real worker event
  processing, real product-command control dispatch, real Gmail repository,
  lifecycle, callback, draft, workspace, scan, session, export, logout, and
  deletion adapters over local stores.
- Live fail-if-called sentinels fence sockets, DNS, HTTP, urllib, requests,
  boto3 construction, botocore API calls, Google OAuth/Gmail, OpenAI, Telegram
  delivery, AgentCore, and production composition roots. Canary calls prove
  the sentinels are installed; the journey then finishes with an empty call
  ledger.
- Three pilots exercise full owner/requester Cartesian isolation for tickets,
  cards, scans, drafts, overview, export, logout, disconnect, and deletion.

## Review remediation and TDD evidence

The first independent review reported seven Important findings. Each was
reproduced with a failing regression before its fix:

1. production invite cancellation classification;
2. invite bearer leakage outside canonical `/start`;
3. incomplete/unfenced Gmail disconnect;
4. zero-progress scan deletion above 4,000 rows;
5. stale-cookie session recovery;
6. private DOM retention after logout;
7. vacuous provider-free journey evidence.

The first remediation re-review found three additional disconnect races. Its
deterministic probes reproduced newer-generation state destruction, same-
generation OAuth resurrection during a repeated disconnect, and a retained
envelope after both delete and confirmation-read timeouts. The added focused
regressions initially reported `4 failed, 23 passed`. After atomic writes,
monotonic disconnect generations, exact status conditions, and fail-closed
delete reconciliation, the expanded focused slice reported `41 passed`.

The first aggregate remediation run also exposed collection-time legacy SDK
stubs: `3 failed, 1024 passed, 2 errors, 10 subtests passed`. Stable references
to the real boto3/botocore types are now captured before collection. The
affected mixed-order slice then reported `355 passed, 10 subtests passed`.

## Fresh exact-subject verification

At implementation head `4c707ae679c76ca98312009672add3d12260f0b8`:

```text
PATH=/opt/homebrew/opt/node@24/bin:$PATH \
PYTHON=/Users/konstantin.tuzikov/Documents/personal-operator/.venv/bin/python \
./scripts/test-local.sh
```

Result: exit `0`, `All local checks passed.`

- Python unit/security/integration: `1038 passed, 10 subtests passed`;
- E2E session control: `11 passed`;
- serialized bridge Node suite: `313 passed`, `0 failed`;
- web UI: `11 passed`;
- Node 24 production build: passed;
- JavaScript and Python syntax: passed;
- repository whitespace contract: passed;
- hermetic offline CDK synthesis: passed;
- cdk-nag contract: passed.

## Visual verification limitation

The in-app browser had no available browser surface, so no interactive visual
claim is made. Mobile reachability is covered by breakpoint guards, jsdom
component tests, private-state logout assertions, and the Node 24 production
build. A later human or available in-app-browser pass should still inspect the
built shell at narrow and wide viewports.

## Serial dependency retained for central integration

The founder-only rule that a local draft edit atomically stales an existing
pending approval remains a Task 3 integration dependency. It overlaps Task 3's
action transaction and must be resolved centrally at Integration Gate A.
External pilots remain read-only and receive neither approval nor send
authority.

## Commits

1. `12261402d8acd871bca0f515f4f24f967e1773c5` - opaque invitations
2. `32df9c10f24d6bed4cfb38b299ac8703190d7ddc` - Telegram redemption
3. `c6206f71554e0d8e00de52f734640143693ac07d` - return-path tickets
4. `b8b4aaef2a59ba12a0e831e0b793575f6ca0c14a` - overview/lifecycle
5. `f6e8cd6f7154afdfed134951f1a36d11df14e388` - callback ACK
6. `c0aaee8a63582e1b9b53235e48d0d62ec1aad2ae` - mobile UI
7. `50a08e7ad3afe952b18736d479b5fbda66acbd8c` - synthetic journey
8. `a52e8eec93cdb654df4033f4559bd3899adb9df3` - initial report
9. `84511720f38391fa5fb10cfd7f849cfacc8637ed` - file normalization
10. `e3d9665ffe2586d05a4ecba30c1c3d56799debe6` - invite outage retry
11. `ebb398fb64b120bdeef8c9cccc2031160c475217` - cancellation classifier
12. `31bdd9beee22c88d22648aab747d9b0625cb63ca` - bearer stripping
13. `3b24ea20db1df00ec9d57ed52465fa81913f1f2a` - disconnect generation
14. `20c2cbd0b3c833435e79d5fce70533150955d8a5` - scan deletion progress
15. `8115ce5d3c958af65310f14b044fe16d5de3d3f7` - stale-cookie recovery
16. `bd26340b5acc5bf193dd3eb2a3bc321c5bf3ab8c` - logout state clearing
17. `175cb59e6ff87bb536995d54fc8cd1c7c7a2663c` - hardened journey
18. `8e5bf4deca242201273f08242b90313bf18e7c12` - real SDK sentinels
19. `f35a505e8bc6ef0947eabaac8b1e9af32723de54` - uncertain deletes
20. `359c84982c894f0fa021e3e2beafd8c5e48ee50b` - atomic writes
21. `4c707ae679c76ca98312009672add3d12260f0b8` - monotonic disconnect fence
