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
read-only follow-up discovery and draft preparation. A founder-only send path
is planned behind exact-payload approval, idempotency, reconciliation, and
effect receipts. None of those application workflows is complete at the
foundation stage.

## Current state

Task 1 establishes the frozen product configuration and a reproducible local
baseline over the imported AWS sample. It does not deploy infrastructure, send
messages, or claim that the inherited runtime is least-privilege. Known
upstream test failures are recorded in [docs/BASELINE.md](docs/BASELINE.md).

The implementation proceeds in reviewed tasks described by the approved
[design](docs/superpowers/specs/2026-07-17-personal-operator-v0-design.md) and
[plan](docs/superpowers/plans/2026-07-17-personal-operator-v0.md).

## Frozen runtime baseline

| Setting | v0 value |
|---|---|
| AWS region | `eu-west-1` |
| Bedrock model | `eu.anthropic.claude-sonnet-4-6` |
| Node.js image | `24.15.0-slim` |
| OpenClaw package version | `2026.7.2` |
| OpenClaw source commit | `4bfaccafd62ac2ff2e70ca1decc40fb1297ab438` |
| Registration | Invite-only |
| Browser | Disabled |
| Inactive pilot workspace retention | 30 days |
| AgentCore runtime identifiers | Empty until an explicit deployment |

OpenClaw `2026.7.2` is not a published npm release. The bridge image fetches
the audited immutable source commit, verifies the package version, and builds
it with the source lockfile. It does not install community marketplace skills.
See [docs/UPSTREAM.md](docs/UPSTREAM.md) for the source ledger.

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

The local script runs Python unit tests, serialized Node tests with
`AWS_REGION=eu-west-1`, JavaScript and Python syntax checks, and an offline CDK
synthesis contract using a synthetic account number. It never deploys or
requires cloud credentials. Until later tasks remove the imported defects, the
aggregate command is expected to report them and exit non-zero.

## Product boundaries

- Invite-only access; browser automation is disabled for v0.
- No arbitrary marketplace skill, MCP server, plugin, or user API-key install.
- OpenClaw never receives Telegram, Google, database, approval-signing, or
  cross-user credentials.
- Raw Gmail bodies are transient and must not be persisted or logged.
- External effects require a trusted capability gateway and exact approval.
- Provider timeouts become `UNCERTAIN` and must be reconciled before retry.
- No deployment, push, paid resource creation, or real message is part of the
  local baseline workflow.

## Repository map

```text
app.py                  AWS CDK application entry point
cdk.json                Frozen product and runtime defaults
bridge/                 AgentCore/OpenClaw container bridge
lambda/                 Imported router and cron functions
stacks/                 Imported CDK stacks
tests/                  Static product and end-to-end contracts
scripts/test-local.sh   Credential-free local baseline command
docs/BASELINE.md        Reproducible imported and current test evidence
docs/UPSTREAM.md        Upstream source and license ledger
```

## Upstream and license

This work retains the AWS sample's MIT No Attribution license and upstream Git
remote. The imported commit and runtime source provenance are recorded in
[docs/UPSTREAM.md](docs/UPSTREAM.md). See [LICENSE](LICENSE).
