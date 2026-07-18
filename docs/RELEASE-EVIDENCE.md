# Personal Operator v0 Release Evidence

## Verdict

**NOT RELEASED / NOT DEPLOYABLE YET.** The repository now contains a locally
verified **pre-production local
prototype**. This ledger records synthetic, credential-free development
evidence from 2026-07-18. It is not evidence of an AWS deployment or of a real
Telegram, Google, Gmail, OpenAI, or AgentCore interaction.

No cloud resource was created or changed. No image was pushed. No OAuth account
was connected. No Telegram or email message was sent.

## Verification identity

- Branch: `codex/personal-operator-v0`
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
| Python unit, security, and integration tests | 946 passed plus 10 subtests |
| Local end-to-end session-control tests | 11 passed |
| Serialized bridge/runtime Node tests | 313 passed |
| Web UI tests | 5 passed |
| Web production build | passed |
| JavaScript syntax | passed |
| Python compilation | passed |
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
- active observability templates do not enable model payload logging.

## Dependency and license inventory

The inventory was generated twice into independent temporary directories:

```bash
./.venv/bin/python scripts/generate-release-inventory.py --output-dir <temp>
```

The two trees were byte-identical and contained no repository or home-directory
absolute path.

- CycloneDX 1.5 components: 220
- npm lock records: 177
- Python records: 43
- license `NOASSERTION`: 43, all Python records
- `personal-operator.cdx.json` SHA-256:
  `cf269119ede9e2efbd4335b4639ddada655376285e2ba1901515cd3da746d27a`
- `dependency-licenses.csv` SHA-256:
  `74225f6590d5a7aac1b7e8c14d0420890502efbb201a7d0fb86ca824d10bc632`

This is a source/lock inventory, not a final image SBOM. It covers the
hash-locked Lambda Python dependency tree and npm lock records. It does not
prove the built Lambda asset, base-image packages, or the OpenClaw pnpm graph.

## Release blockers

1. **Runtime provisioning is intentionally absent.** `scripts/deploy.sh --full`
   and `--runtime-only` fail before credentials or cloud calls. An immutable
   AgentCore runtime/version/endpoint must be provisioned by a reviewed,
   reproducible path before the CDK stack can bind it.
2. **No Lambda deployment bundle proof.** The trusted Python 3.13/ARM64 asset
   must be built with the reviewed digest-pinned Lambda image and its provider
   imports verified with networking disabled. Docker is not installed on this
   host, so the release-asset gate could not run.
3. **No image build, scan, SBOM, signature, push, or immutable ECR digest.** The
   bridge Dockerfile and pins are locally tested, but no container artifact has
   been produced or attested.
4. **No exact runtime-context v3 artifact.** The release gate correctly rejects
   placeholder runtime ARN, version, release endpoint, image digest, account,
   or commit bindings.
5. **No cloud staging evidence.** AWS CLI and CDK CLI are absent, and no account
   was authorized. Synthesized IAM, AgentCore, S3/KMS, DynamoDB, SQS,
   CloudFront, API Gateway, WAF, alarms, retention, backup, and recovery
   behavior remain unverified in AWS.
6. **No provider evidence.** Telegram, Google OAuth/Gmail, and OpenAI credentials
   were intentionally absent. Provider scopes, callbacks, receipts,
   idempotency, and reconciliation are proven only against injected fakes.
7. **No deployed lifecycle SLA.** Local tests cover 14/30/90-day lifecycle
   semantics, permanent identity anti-recreation markers, hourly reconciliation,
   and the 30-minute deletion drain, but do not prove a deployed schedule or a
   completion time.
8. **No performance or usefulness acceptance.** Cold start, runtime replacement,
   concurrent-user behavior, cost, and moderated-pilot usefulness have not been
   measured in staging.
9. **No real-effect authorization.** A founder identity, exact provider account,
   allowlisted recipient, payload hash, fresh approval, and separate human gate
   are required before any Gmail effect.

## What the local result means

The local result establishes that the repository's deterministic contracts,
state machines, privacy boundaries, static infrastructure, synthetic flows,
and failure behavior agree with the v0 plan on this host. It does not establish
that AWS or any external provider behaves as the fakes and templates predict.

The next authorized milestone is a dedicated non-production staging audit using
the checklist in `docs/OPERATIONS.md`. Until every applicable external gate is
closed against one clean commit and immutable image digest, the product remains
a **pre-production local
prototype**.
