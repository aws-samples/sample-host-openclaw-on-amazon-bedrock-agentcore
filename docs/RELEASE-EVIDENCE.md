# Personal Operator v1 Release Evidence

## Verdict

**NOT RELEASED / NOT DEPLOYABLE YET.** Status:
**staging deployment path implemented and locally verified; not deployed**.
The repository remains a locally verified **pre-production local
prototype**. This ledger records synthetic, credential-free development
evidence from 2026-07-18. It is not evidence of an AWS deployment or of a real
Telegram, Google, Gmail, OpenAI, or AgentCore interaction.

No cloud resource was created or changed. No image was pushed. No OAuth account
was connected. No Telegram or email message was sent.

## Verification identity

- Evidence target: the clean Git commit containing this ledger. The immutable
  commit SHA is reported by the final handoff rather than embedded here, because
  a commit cannot contain its own hash.
- Imported upstream AWS sample: `e13e385ec44a3776e571ec48001904e9394cc20e`
- OpenClaw source: `4bfaccafd62ac2ff2e70ca1decc40fb1297ab438`
- OpenClaw declared version: `2026.7.2`
- Bridge base image: `node:24.15.0-slim`
- Local Node: `v24.18.0`
- Local Python: `3.12.0`
- Local pytest: `9.1.1`
- Region contract: exactly `eu-west-1`

The aggregate gate is rerun after the evidence commit. A passing command against
any earlier dirty tree is development evidence only and does not establish a
release candidate.

## Complete local gate

Command:

```bash
PATH="/opt/homebrew/opt/node@24/bin:$PATH" ./scripts/test-local.sh
```

Result on 2026-07-18: **passed**.

| Gate | Result |
|---|---|
| Bridge and web lockfile installs with lifecycle scripts disabled | passed; zero reported npm vulnerabilities |
| Python unit, security, and integration tests | 1,039 passed plus 10 subtests |
| Local end-to-end session-control tests | 11 passed |
| Serialized bridge/runtime Node tests | 313 passed |
| Web UI tests | 5 passed |
| Web production build | passed |
| JavaScript syntax | passed |
| Python compilation | passed |
| Strict release contracts, ECR/AgentCore adapters, journal, and CLI tests | passed with injected fakes only |
| Deterministic trusted Lambda ZIP double-build in pure Python | byte-identical; Docker import gate open |
| Repository whitespace/diff contract | passed |
| Hermetic offline CDK synthesis contract | passed |
| CDK cdk-nag contract | passed with zero findings |

The suite includes the following hostile cases:

- 100 concurrent copies of one Telegram update retain one immutable FIFO
  identity and 100 worker replays execute and deliver only once;
- private Telegram identity is actor-bound at the public, queue, legacy, and
  end-to-end fixture boundaries;
- runtime sessions, release endpoints, credentials, workspace prefixes, and
  broker capabilities are server-bound and cross-user attempts fail closed;
- only four curated model tools exist; the mutable upstream `session_status`
  built-in is denied, model visibility is pinned to the one loopback AgentCore
  route, and both runtime modes expose no URL,
  browser, search, shell, scheduler, marketplace, MCP, or generic network tool;
- attempted query-string, prior-turn, fetched-page, and canonicalization
  exfiltration targets produce an unknown-tool result before DNS or network;
- provider writes require exact persisted approval authority, recheck deletion
  immediately before dispatch, and become terminal no-resend `UNCERTAIN` on an
  ambiguous result;
- export and deletion are user-prefix bounded, include multipart/version
  handling, and fail closed on malformed, repeated, or ambiguous provider
  evidence;
- a three-user Cartesian export canary contains no cross-user bytes;
- the complete synthetic founder journey covers connect, Gmail scan, approval,
  one confirmed receipt, bounded export, and deletion without real providers;
- production source and browser inputs contain no detected high-confidence
  literal AWS, GitHub, Slack, OpenAI, Google, Telegram, or private-key secret;
- active observability templates do not enable model payload logging;
- staging transaction failures are write-ahead `UNCERTAIN`, cannot skip a
  phase, require explicit reconciliation, reject digest-only image claims and
  empty endpoint claims, and cannot retarget a retained release endpoint;
- the shared trusted asset covers five unique handler modules across six Lambda functions;
  the web handler intentionally serves both web and maintenance. Fake-container
  subprocess tests inspect the executed boundary, and failed republication
  preserves the prior artifact rather than deleting it.

## Task 11 local operational evidence

The credential-free v1 harness executes three synthetic participant journeys
and constructs the cohort report from events emitted only after each associated
assertion succeeds. It covers invite, connect, read-only connector, scan, card,
feedback, draft/workspace, proposal-only/read-only schedule, compute denial,
portable export/import, inert landing, identical replay denial, and both
deletion passes. Production-shaped compute returns `ADAPTER_DISABLED`; this is
containment evidence, not successful compute-isolation evidence.

The connect, feedback, and workspace rows causally depend on the production
`GoogleReadonlyOAuthFlow.start/complete`,
`DynamoScanMeasurements.feedback`, and
`GmailWorkspaceService.get/edit_draft` methods. Their external dependencies are
local injected state, token client/vault, Dynamo-shaped tables, Gmail input,
repository, and a fail-closed approval superseder; no provider adapter is
substituted for those production methods. Hostile monkeypatch tests prove that
blocking any named method prevents a successful run. Raw
`socket.socket.connect`, `connect_ex`, `sendto`, and every available send method
are patched, actively probed, and denied in addition to the higher-level
network/provider boundaries.

```bash
PYTHONPATH=lambda ./.venv/bin/python -m pytest -q \
  tests/integration/test_synthetic_pilot_v1.py
PYTHONPATH=lambda ./.venv/bin/python scripts/run-synthetic-pilot.py
```

The standalone runner executes the journey twice and emits output only when
the canonical report bytes are identical and both external-call ledgers are
empty. The report schema is `personal-operator.cohort-report.v1`; its only
payload is a participant count plus sorted, aggregated operational events.
Tests reject participant IDs, invite bearers, source/thread/message values,
addresses, subjects, excerpts, draft/workspace content, and provider canaries
from those bytes.

This remains local synthetic development evidence. No remote AWS API,
AgentCore, Telegram, Google/Gmail provider endpoint, model provider, connector
server, or browser provider was invoked. No cloud resource, remote account,
real pilot, or external effect was created. Only the DLQ and native maintenance
heartbeat currently have truthful live metric producers; uncertain effect,
scan failure, aged deletion, connector drift, and compute isolation are
template/synthetic alarms only. Live observability, Task 8 compute completion,
Task 10 connector/browser approval enforcement, and the moderated-pilot gate
all remain **OPEN**.

## Dependency and license inventory

The inventory was generated twice into independent temporary directories:

```bash
./.venv/bin/python scripts/generate-release-inventory.py --output-dir <temp>
```

The two trees were byte-identical and contained no repository or home-directory
absolute path.

- CycloneDX 1.5 components: 221
- npm lock records: 177
- Python records: 44
- license `NOASSERTION`: 44, all Python records
- `personal-operator.cdx.json` SHA-256:
  `577c8d76b4a41b588686978f9f68b58c3e6225769df470a9fcaa3ee966dbe28e`
- `dependency-licenses.csv` SHA-256:
  `d3f0bd8765c5573ef80e73328d114609e1f9d9dcbe7f08942faeb4751da5a000`

This is a source/lock inventory, not a final runtime-image SBOM. It covers the
hash-locked Lambda Python dependency tree and npm lock records. The separate v2
Lambda manifest locally binds deterministic ZIP bytes to commit/tree, builder
digest and ID, requirements, platform, inventories, byte counts, and archive
SHA-256. Docker-backed Python 3.13/ARM64 import proof and runtime base-image/
OpenClaw pnpm attestation remain open.

## Release blockers and external gates

Every external gate remains open. “Implemented” below refers only to reviewed
code and local fake/offline evidence.

| Gate | State | Evidence still required |
|---|---|---|
| OPEN — runtime image push | open | exact commit-tagged digest in retained `personal-operator/bridge` |
| OPEN — managed signing | open | one completed Notation OCI signature from the retained profile |
| OPEN — authoritative image scan | open | completed scan with zero unreviewed high/critical findings plus SBOM and provenance referrers |
| OPEN — CloudFormation change-set execution | open | separately reviewed exact-account changesets and human execution record |
| OPEN — AgentCore runtime readiness | open | exact READY runtime/version/image/role/storage evidence |
| OPEN — consumer application | open | reviewed consumer changesets applied to the exact RuntimeContextV3 |
| OPEN — moderated pilot | open | synthetic staging journey, performance/cost results, then separate pilot authorization |

Additional blockers:

1. Docker was unavailable on this host. The exact Lambda Python 3.13/ARM64
   double-build and network-disabled imports for five unique handlers remain
   unproven, even though independent pure-Python ZIP builds were byte-identical.
2. No runtime image, final image SBOM/provenance, signature, scan result, or
   immutable ECR digest exists. No image was pushed.
3. No authoritative runtime-context v3 artifact exists. Local tests only prove
   strict rejection and injected READY evidence behavior.
4. No AWS account was authorized and no CloudFormation change set was created
   or executed. IAM, storage, alarms, retention, backup, and recovery behavior
   remain template evidence only.
5. Telegram, Google OAuth/Gmail, and OpenAI credentials were absent. Provider
   scopes, callbacks, receipts, idempotency, and reconciliation use fakes.
6. Local tests cover lifecycle semantics and deletion drains but establish no
   deployed completion SLA.
7. A founder identity, exact provider account, allowlisted recipient, payload
   hash, fresh approval, and separate human gate remain mandatory before any
   Gmail effect.

## What the local result means

The local result establishes that the repository's deterministic contracts,
state machines, privacy boundaries, static infrastructure, synthetic flows,
and failure behavior agree with the approved plan on this host. It does not establish
that AWS or any external provider behaves as the fakes and templates predict.

The next authorized milestone is a dedicated non-production staging audit using
the checklist in `docs/OPERATIONS.md`. Until every applicable external gate is
closed against one clean commit and immutable image digest, the product remains
a **pre-production local
prototype**.
