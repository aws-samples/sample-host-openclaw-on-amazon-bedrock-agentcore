# Personal Operator v0

[![License: MIT-0](https://img.shields.io/badge/License-MIT--0-blue.svg)](LICENSE)
[![Status: Pre-production](https://img.shields.io/badge/Status-Pre--production-orange.svg)]()

> **Pre-production** — this repository is an implementation workbench, not a
> deployed service or a production authorization boundary. Do not use it with
> real credentials, private messages, or customer data.

Personal Operator is an invite-only personal AI computer reached through one
shared Telegram bot. The intended v0 gives each user an isolated, replaceable
runtime with a persistent logical workspace. General agent work runs in
OpenClaw on Amazon Bedrock AgentCore; credentials and sensitive external
effects remain in a separate trusted control plane.

The first governed application is Gmail. External pilots are limited to
read-only follow-up discovery and draft preparation. A separate founder-only
send credential is reachable only through exact-payload approval, a durable
dispatch fence, provider reconciliation, and an effect receipt.

## Current state

The v0 implementation is complete as a local, synthetic prototype. It includes
the hardened per-user runtime; signed Telegram ingress and a per-user FIFO
worker; deterministic product commands; read-only Gmail OAuth, scanning,
ranking, and revisioned draft editing; founder-only approval-gated sending; a
trusted browser control surface; export; 14/30/90-day lifecycle enforcement;
and two-pass resumable account deletion. Deployment assets are fail-closed:
real-account synthesis requires a verified Lambda Python 3.13 ARM64 bundle and
consumer wiring requires an exact commit-bound private-ECR runtime digest,
while the raw source escape is limited to the impossible account used by local
tests.

Active observability uses aggregate AWS service metrics and alarms only. It
does not enable model invocation text or image payload logging, and the
archived legacy token-monitoring stack is not active.

This is not deployment evidence. The deterministic release contracts, retained
ECR/signing template, direct immutable AgentCore Runtime/Endpoint L1s, and
write-ahead phase CLI are implemented and locally verified only. The Docker
Lambda import gate and every AWS image, signing, scan, change-set, readiness,
consumer, and pilot gate remain open. No AWS stack has been deployed, and no
Telegram, Google, OpenAI, or Gmail credential or real message was used. The
remaining gates are recorded in
[docs/RELEASE-EVIDENCE.md](docs/RELEASE-EVIDENCE.md).

The implementation proceeds in reviewed tasks described by the approved
[design](docs/superpowers/specs/2026-07-17-personal-operator-v0-design.md) and
[plan](docs/superpowers/plans/2026-07-17-personal-operator-v0.md).

## Runtime boundary now enforced

- OpenClaw uses the `minimal` profile but explicitly denies its mutable
  `session_status` built-in. The effective surface is exactly `po_file_list`,
  `po_file_read`, `po_file_write`, and `po_file_delete`. Model visibility and
  selection are pinned to the single loopback `agentcore/bedrock-agentcore`
  route; no fallback provider is visible.
  Model-callable URL retrieval and search are deliberately deferred: combining
  workspace reads with arbitrary network egress would create a data-exfiltration
  path. A later URL reader must authorize targets outside the model tool loop.
- The image loads only the repository-owned `personal-operator` plugin from an
  explicit path. All inherited executable skill trees are removed, bundled
  skills are disabled, and the agent's effective skill inventory is empty.
- Workspace tools accept only relative paths and bounded UTF-8 content. Their
  S3 keys are confined to `<server workspace prefix>/files/`; root runtime
  state, `.openclaw`, `_uploads`, and internal namespaces are unreachable.
- For the gateway bridge, ordinary slash-prefixed input is model text. The
  exact `/new` and `/reset` controls fail closed because they require admin,
  while the bridge requests only `operator.read` and `operator.write`; a live
  pinned-gateway proof rejected `config.patch` for missing `operator.admin`
  and left the config byte-identical.
- In the runtime, arbitrary shell execution is disabled, along with generic filesystem,
  process, scheduling, UI automation, cross-session, delegated-worker, MCP,
  marketplace-plugin, and user credential capabilities.
- The loopback gateway token is generated inside each runtime. Telegram delivery remains outside the runtime, which never fetches a Telegram token or
  calls the Telegram API.
- A warm runtime binds synchronously to one server-resolved internal user ID.
  Later mismatched invocations fail before initialization, workspace access,
  model execution, or mutable counters. The runtime receives only expiring,
  explicit S3 credentials for that user's exact namespace.
- The runtime cannot call STS or construct its own S3 session policy. A trusted
  broker alone can assume the bucket-wide base role. It accepts only a
  worker-minted HMAC capability, strongly rechecks the exact live
  user/session/runtime/release binding, derives the namespace policy on the
  server, and returns credentials valid for at most 15 minutes. Refresh is
  single-flight every 10 minutes; any denial quarantines the runtime.
- S3 is authoritative for durable OpenClaw state. Each save writes immutable
  content-addressed payloads and a canonical manifest, then advances one
  compare-and-swap pointer. Deletion is represented by absence from the new
  manifest, and ambiguous writes quarantine the runtime instead of guessing.
- Startup verifies the exact writable AgentCore mount, restores into private
  staging, validates every streamed hash, atomically activates the live tree,
  and starts OpenClaw only afterward. SQLite state is copied through the pinned
  database backup helper rather than by copying live database or WAL bytes.
- Successful turns are held until their durable pointer commit completes.
  Shutdown drains active work, stops OpenClaw to close SQLite, takes a final
  verified snapshot, and exits nonzero if persistence cannot be proven.

These are pre-production controls. Their local tests and offline synthesis do
not prove the behavior of a deployed AgentCore runtime, S3 bucket, or IAM role.

## Frozen runtime baseline

| Setting | v0 value |
|---|---|
| AWS region | `eu-west-1` |
| Bedrock model | `eu.anthropic.claude-sonnet-4-6` |
| Node.js image | `24.15.0-slim@sha256:4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d` |
| OpenClaw package version | `2026.7.2` |
| OpenClaw source commit | `4bfaccafd62ac2ff2e70ca1decc40fb1297ab438` |
| Registration | Invite-only |
| Browser | Disabled |
| Inactive pilot workspace retention | 30 days |
| AgentCore runtime identifiers | Empty until an explicit deployment |
| AgentCore release endpoint | `release_<exact-candidate-commit>`; mutable `DEFAULT` is rejected |

OpenClaw `2026.7.2` is not a published npm release. The bridge image fetches
the audited immutable source commit, verifies the package version, builds it
with the source lockfile, and loads the reviewed plugin package directly. See
[docs/UPSTREAM.md](docs/UPSTREAM.md) for the source ledger.

## Local validation

Create the Python environment once:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

Run the focused product contract:

```bash
./.venv/bin/python -m pytest tests/test_product_configuration.py -v
```

Run the complete local baseline:

```bash
./scripts/test-local.sh
```

The local script runs the Python unit, release-foundation, security,
integration, replay, and synthetic-journey suites; serialized runtime Node
tests; web tests and a production build; JavaScript/Python syntax checks;
repository whitespace checks; and offline CDK/cdk-nag synthesis using a synthetic account number. It
never deploys or requires cloud credentials. Cloud-specific behavior remains
provisional until a later credentialed staging gate.

The separate release-candidate gate requires a clean exact commit, Docker, an
explicitly reviewed immutable AWS Lambda Python image digest, a commit-bound
runtime-context v3 file, its exact private-ECR runtime digest, and a
non-synthetic account-shaped value. It builds and verifies the transitive,
hash-locked Python 3.13/ARM64 asset, reruns the local suite, and synthesizes all
stacks without AWS credentials:

```bash
export PERSONAL_OPERATOR_RELEASE_ACCOUNT=123456789012
export PERSONAL_OPERATOR_RELEASE_COMMIT="$(git rev-parse HEAD)"
export TRUSTED_LAMBDA_BUILD_IMAGE='public.ecr.aws/lambda/python@sha256:<reviewed-digest>'
export PERSONAL_OPERATOR_RUNTIME_CONTEXT_FILE="$PWD/build/runtime-context.json"
export PERSONAL_OPERATOR_RUNTIME_IMAGE_URI='123456789012.dkr.ecr.eu-west-1.amazonaws.com/personal-operator/bridge@sha256:<reviewed-digest>'
./scripts/test-release-assets.sh
```

This command never deploys, but it does pull the pinned public builder image
and hash-locked Python packages. It refuses an absent, mismatched, or
placeholder runtime binding. A pass is packaging and synthesis evidence, not
cloud behavior evidence.

## Release boundary

- v0 remains invite-only with browser automation disabled.
- The runtime does not allow arbitrary marketplace skill, MCP server,
  plugin, or user API-key installation.
- The OpenClaw boundary never receives Telegram, Google, database,
  approval-signing, or cross-user credentials.
- Raw Gmail bodies are transient and are not intentionally persisted or logged.
- External effects require the trusted capability gateway and exact
  approval.
- Provider timeouts become `UNCERTAIN` and cannot be retried before
  reconciliation.
- No deployment, push, paid resource creation, or real message is part of the
  local implementation workflow.

## Repository map

```text
app.py                  AWS CDK application entry point
cdk.json                Frozen product and runtime defaults
bridge/                 AgentCore/OpenClaw container bridge
lambda/router/          Signed ingress and AgentCore runtime driver
lambda/worker/          Ordered worker and durable Telegram delivery ledger
lambda/control/         Deterministic product-command application boundary
lambda/workflows/       Read-only Gmail application workflow
lambda/actions/         Approval, effect, and reconciliation state machines
lambda/web/             Browser auth, approval, export, retention, deletion
web/                    React/Vite consumer control surface
stacks/                 CDK infrastructure and exact IAM boundaries
release_tools/          Strict artifacts, evidence adapters, journal, and CLI
tests/                  Unit, security, replay, and synthetic journey contracts
scripts/test-local.sh   Credential-free aggregate local gate
scripts/test-release-assets.sh
                        Docker-backed exact-asset release gate (no deployment)
scripts/build-trusted-lambda-asset.sh
                        Verified ARM64 dependency asset builder
docs/BASELINE.md        Reproducible imported and current test evidence
docs/UPSTREAM.md        Upstream source and license ledger
docs/RELEASE-EVIDENCE.md
                        Current evidence and unclosed external gates
```

## Upstream and license

This work retains the AWS sample's MIT No Attribution license and upstream Git
remote. The imported commit and runtime source provenance are recorded in
[docs/UPSTREAM.md](docs/UPSTREAM.md). See [LICENSE](LICENSE).
