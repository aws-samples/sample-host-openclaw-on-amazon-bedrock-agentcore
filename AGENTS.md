# Agent guidance — Personal Operator v1

Personal Operator v1 is a pre-production consumer assistant with a frozen,
provider-credential-free AgentCore model runtime and a separate trusted control
plane. Start with `README.md`, `docs/V1-IMPLEMENTATION-EVIDENCE.md`,
`docs/OPERATIONS.md`, and the approved v1 design/plan under
`docs/superpowers/`.

## Non-negotiable boundary

- Region is exactly `eu-west-1`.
- Use public or synthetic data only until an external gate is separately
  authorized and closed with exact live evidence.
- The model-visible surface is the ten `po_*` tools in
  `specs/capabilities/catalog-v1.json`; do not add dynamic MCP, ClawHub,
  arbitrary plugins/skills, browser/computer tools, or shell execution.
- Runtime code receives no durable provider, channel, browser, connector, or
  approval credential. Its short-lived AWS workspace session is limited to one
  server-derived namespace and must never enter model context or logs.
- Connector and Browser Gateway composition stays disabled. Compute stays
  `ADAPTER_DISABLED`. Scheduled turns require `externalEffects=false`.
- Do not treat local tests, synthesis, or source shape as AWS deployment,
  signing, scan, provider, compute-isolation, or pilot evidence.
- The current eight-phase release CLI is not authorized for mutation. Replace
  and independently review its transaction/composer path before deployment.

## Working discipline

- Preserve unrelated changes and use one writer per file.
- Use RED -> GREEN -> REFACTOR for behavior changes.
- Run bridge tests serially with Node 24.
- Commit locally only when the task explicitly requests it; never push from the
  pre-production audit workflow.

Run the aggregate gate with the project interpreter and Node 24:

```bash
PYTHON=/Users/konstantin.tuzikov/Documents/personal-operator/.venv/bin/python \
PATH="/opt/homebrew/opt/node@24/bin:$PATH" \
./scripts/test-local.sh 2>&1 | tee /tmp/personal-operator-local.log
grep -Fx 'All local checks passed.' /tmp/personal-operator-local.log
```

The wrapper's shell status is not sufficient; require the literal acceptance
line. The authoritative local evidence index is
`docs/V1-IMPLEMENTATION-EVIDENCE.md`.
