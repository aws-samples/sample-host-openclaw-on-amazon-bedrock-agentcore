# Personal Operator v1

[![License: MIT-0](https://img.shields.io/badge/License-MIT--0-blue.svg)](LICENSE)
[![Status: Pre-production](https://img.shields.io/badge/Status-Pre--production-orange.svg)]()

> **Pre-production** — v1 is implemented and locally verified source, not a
> deployed service or a production authorization boundary. Use only public or
> synthetic data. Do not provide real credentials, private messages, or
> customer data.

Personal Operator is an invite-only personal AI computer reached through
Telegram and a mobile web control surface. OpenClaw runs inside a replaceable,
provider-credential-free AgentCore runtime: it receives no durable provider,
messaging, browser, connector, or approval credential. After exact session
admission, the trusted broker can issue a short-lived, namespace-scoped
workspace AWS session to the local workspace plugin; that credential never
enters model context, workspace content, tool arguments/results, or logs.
Identity, approval authority, durable effect state, and every external effect
stay in the trusted control plane outside the model.

## Current implementation

The integrated v1 source implements:

- signed Telegram ingress, ordered per-user work, and one isolated runtime
  session and durable logical workspace per user;
- invite and browser-session controls, read-only Gmail OAuth, bounded scanning,
  opportunity cards, feedback, and revisioned local draft/workspace editing;
- an exact-current-request public URL reader with DNS/address pinning and no
  standing network authority;
- governed schedule listing plus one-time proposal and cancel-proposal flows;
- deterministic portable export, staged inert import with replay denial, and
  resumable two-pass account deletion;
- metadata-only operational events, alarms, and a deterministic three-person
  synthetic journey and aggregate cohort report.

The implementation is locally tested with injected fakes, network-denial
canaries, deterministic artifacts, an offline AWS-shaped synthesis, and
cdk-nag. No AWS deployment evidence has been produced. No runtime image has
been pushed or signed, no CloudFormation change set has been executed, no
AgentCore runtime has been invoked, and no real provider account or message has
been used.

Production compute remains disabled: `po_compute_run` and `po_compute_status`
are in the frozen catalog but active composition returns `ADAPTER_DISABLED`
and creates no compute resources or launcher authority. Connector and Browser
Gateway planes remain disabled; the synthetic MCP adapter still needs exact
approval, one-time-use, and expiry enforcement before either plane can be
enabled.

## Runtime boundary now enforced

The runtime package, OpenClaw policy, plugin manifest, compiled catalog,
trusted gateway, and schemas agree on exactly these ten model-visible tools:

| Tool | Implemented boundary |
|---|---|
| `po_file_list` | Bounded list in the server-bound workspace namespace |
| `po_file_read` | Bounded UTF-8 read of one validated relative path |
| `po_file_write` | Same-input idempotent write in the scoped workspace |
| `po_file_delete` | Same-input idempotent deletion of one exact path |
| `po_web_read` | Bounded credential-free GET under a current-request target grant |
| `po_schedule_list` | Strongly read the user's trusted schedule records |
| `po_schedule_propose` | Create only a one-time, approval-bound proposal |
| `po_schedule_cancel_propose` | Propose cancellation of one exact revision |
| `po_compute_run` | Catalogued but fail-closed as `ADAPTER_DISABLED` |
| `po_compute_status` | Catalogued but fail-closed as `ADAPTER_DISABLED` |

Scheduled turns are read-only: their trusted envelope requires
`externalEffects=false`, and admission permits only catalog-derived read and
proposal operations. A scheduled occurrence cannot dispatch connector,
browser, email, or another provider effect.

OpenClaw loads only the repository-owned `personal-operator` plugin. Dynamic
MCP discovery, ClawHub, arbitrary plugins, executable skills, generic
capability calls, cross-session tools, browser tools, and arbitrary shell
execution are disabled. In particular, arbitrary shell execution is disabled.
AgentCore's separate one-shot command and interactive-shell APIs are explicitly
denied by retained resource policies on both the runtime and its immutable
release endpoint; the live evidence adapter requires both exact denies.
Model visibility is pinned to the single loopback
`agentcore/bedrock-agentcore` route, with no fallback provider.

Workspace calls use short-lived credentials minted by the trusted broker for
one exact user prefix. The runtime cannot call STS or construct its own session
policy. The AgentCore execution role can invoke only Bedrock through the frozen
EU inference profile and the two exact trusted Lambda mediators; it has no S3,
Scheduler, EventBridge, DynamoDB, Secrets Manager, browser, connector, or
compute authority.

Public URL reads are not generic runtime network egress. The authenticated
current request derives a tenant-bound, expiring, one-use target grant; the
trusted reader resolves and pins a canonical public HTTPS destination and
returns bounded content through the Task-3 gateway kernel.

Telegram delivery remains outside the runtime, which never receives a Telegram
token or calls the Telegram API. Active connector and browser composition is
disabled. The Gmail executor crosses the complete Task-3 admission,
idempotency, deletion, approval, and uncertainty kernel; the latent synthetic
MCP/browser adapter is not enableable until equivalent approval, one-time-use,
and expiry enforcement exists. For the bridge, ordinary slash-prefixed input is model text.
The exact `/new` and `/reset` controls fail closed because the bridge lacks
`operator.admin`.

Active observability uses bounded aggregate metrics and closed JSON event codes
only. It does not enable model invocation text or image payload logging, and
the runtime sets `DISABLE_ADOT_OBSERVABILITY=true` so payload-rich AgentCore
application observability is disabled. The archived legacy token-monitoring
stack is not active. API access logs keep only method, fixed route template,
status, response length, and latency; they exclude request IDs, source
addresses, raw paths, payloads, and exception data.

These controls are pre-production. Local tests and synthesis do not prove the
behavior of deployed IAM, networking, AgentCore, S3, or provider integrations.

## Frozen v1 baseline

| Setting | Value |
|---|---|
| AWS region | `eu-west-1` |
| Bedrock inference profile | `eu.anthropic.claude-sonnet-4-6` |
| Node.js image | `24.15.0-slim@sha256:4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d` |
| OpenClaw package version | `2026.7.2` |
| OpenClaw source commit | `4bfaccafd62ac2ff2e70ca1decc40fb1297ab438` |
| Registration | Invite-only |
| Connector plane | Disabled |
| Browser Gateway | Disabled |
| Production compute | Disabled |
| Inactive pilot workspace retention | 30 days |
| Runtime endpoint | `release_<exact-candidate-commit>`; mutable `DEFAULT` is rejected |

The runtime-image recipe pins OpenClaw `2026.7.2` to the audited immutable
source commit, validates its declared package version, and installs from its
frozen lockfile when the image is built. This ledger does not claim an exact
candidate-bound Docker build or image until the separate release evidence is
recorded. See [docs/UPSTREAM.md](docs/UPSTREAM.md) for provenance and license
inventory.

## Local validation

Create the Python environment once:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

Run the complete provider-free local gate:

```bash
PYTHON="$PWD/.venv/bin/python" \
PATH="/opt/homebrew/opt/node@24/bin:$PATH" \
./scripts/test-local.sh 2>&1 | tee /tmp/personal-operator-local.log
grep -Fx 'All local checks passed.' /tmp/personal-operator-local.log
```

The shell wrapper historically ended with output that could mask a failed
subcheck. Treat the exact final line as the aggregate success condition; do not
infer success from the shell status alone. The gate runs Python unit, security,
release, and integration tests; end-to-end session tests; serialized runtime
Node tests; web tests and production build; JavaScript and Python syntax;
whitespace; hermetic offline CDK synthesis; and zero-finding cdk-nag checks.

The Docker-backed release-asset gate is separate. It builds and verifies the
hash-locked Python 3.13/ARM64 Lambda artifact against one clean exact commit and
runtime context. It can prove local packaging and deterministic bytes; it does
not push an image, deploy a stack, or establish live readiness.

## Release boundary

All external gates remain explicitly open until exact live evidence is created
and independently inspected:

| Gate | State |
|---|---|
| Runtime image publication | OPEN |
| Managed image signing | OPEN |
| Authoritative image scan, SBOM, and provenance | OPEN |
| CloudFormation deployment | OPEN |
| AgentCore runtime and consumer readiness | OPEN |
| Connector/provider effects | OPEN |
| Browser Gateway | OPEN |
| Networkless compute | OPEN |
| Moderated pilot | OPEN |

The current evidence ledger is
[docs/V1-IMPLEMENTATION-EVIDENCE.md](docs/V1-IMPLEMENTATION-EVIDENCE.md).
Operational containment and stop criteria are in
[docs/OPERATIONS.md](docs/OPERATIONS.md). The detailed authority and privacy
contracts are [docs/CAPABILITY-BOUNDARY.md](docs/CAPABILITY-BOUNDARY.md) and
[docs/PRIVACY-BOUNDARY.md](docs/PRIVACY-BOUNDARY.md).

## Repository map

```text
app.py                  AWS CDK composition and exact release inputs
bridge/                 Provider-credential-free AgentCore/OpenClaw bridge
lambda/capabilities/    Catalog compiler, grants, admission kernel, adapters
lambda/scheduler/       Trusted schedule proposal and read-only occurrence plane
lambda/portable/        Deterministic export and non-replayable staged import
lambda/router/          Signed ingress and ordered runtime invocation
lambda/worker/          FIFO worker and durable Telegram delivery ledger
lambda/web/             Mobile control surface, lifecycle, export, deletion
specs/capabilities/     Canonical ten-tool catalog and strict JSON schemas
stacks/                 CDK resources and least-authority IAM/network boundaries
release_tools/          Release contracts, evidence adapters, journal, and CLI
tests/                  Unit, security, replay, integration, and journey proofs
```

This work retains the imported AWS sample's MIT No Attribution license. See
[LICENSE](LICENSE) and [docs/UPSTREAM.md](docs/UPSTREAM.md).
