# AGENTS.md — openclaw-on-agentcore

AgentCore Runtime V2 only: use `runtime_manifest` for CDK, `.runtime/runtime-v2.json` for the SDK controller, and immutable endpoints starting with `release_1`. Phase 1 can omit the manifest; Phase 3 requires verified readback and full-chat warmup. Never delete SESSION records to reset tests. See [the runbook](docs/runtime-v2.md).

## Build & Validation Commands (Backpressure)

### CDK (Python)
```bash
# Synth — must pass before committing any CDK change
cd /home/ec2-user/projects/openclaw-on-agentcore
source .venv/bin/activate
cdk synth 2>&1

# Run Python unit tests
uv run --no-project --python .venv/bin/python python -m pytest tests/ --ignore=tests/e2e -v

# Type check (if mypy configured)
mypy stacks/ --ignore-missing-imports 2>&1
```

### Bridge (Node.js)
```bash
# Syntax check — must pass before committing any .js change
node --check bridge/agentcore-proxy.js
node --check bridge/agentcore-contract.js
node --check bridge/runtime-lifecycle.js

# Run bridge tests (if any)
npm test --prefix bridge
```

### Red Team folder
```bash
# Validate promptfoo config (after creating redteam/)
cd redteam && npm install && npx promptfoo eval --dry-run 2>&1
```

### E2E Tests
```bash
# Run existing E2E tests (must not regress)
cd /home/ec2-user/projects/openclaw-on-agentcore
pytest tests/e2e/ -v 2>&1

# Key journeys that must pass after proxy/CDK changes:
# - tests/e2e/bot_test.py — core message flow
```

---

## ECC Slash Commands (Claude Code built-ins)

| Command | When to use |
|---------|-------------|
| `/tdd` | Before implementing any new function, class, or CDK construct |
| `/verify` | After every change — build + type + lint check |
| `/build-fix` | When `cdk synth` or `node --check` fails — do NOT manually guess |
| `/e2e` | After proxy changes or CDK stack changes that touch the message flow |
| `/security-review` | Before marking any CDK or proxy task complete |
| `/update-docs` | Once ALL tasks done and E2E green — updates README, ENV vars, runbook |

---

## Key Files

| File | Purpose |
|------|---------|
| `stacks/agentcore_stack.py` | CDK foundation, Browser, storage and IAM |
| `scripts/runtime_deploy.py` | Pinned SDK V2 AgentCore Runtime controller; `.runtime/runtime-v2.json` |
| `stacks/security_stack.py` | KMS, Secrets Manager, Cognito |
| `stacks/guardrails_stack.py` | NEW — Bedrock Guardrails (create this) |
| `app.py` | CDK stack wiring |
| `bridge/agentcore-proxy.js` | Bedrock ConverseStream proxy — inject guardrailConfig here |
| `bridge/agentcore-contract.js` | Container entrypoint |
| `bridge/runtime-lifecycle.js` | V2 bootstrap, immutable binding and readiness |
| `tests/e2e/bot_test.py` | E2E bot tests |
| `docs/redteam-design.md` | Full design context |
| `cdk.json` | CDK context flags (add enable_guardrails here) |

---

## Commit Message Format
```
<type>(<scope>): <description>

Types: feat, fix, docs, test, chore, refactor
Scopes: cdk, bridge, redteam, docs

Examples:
feat(cdk): add GuardrailsStack with content filters and PII redaction
feat(bridge): inject guardrailConfig into ConverseStream calls
test(bridge): add unit tests for guardrail config injection
docs(redteam): add promptfoo test suites for jailbreak attacks
```

---

## Learnings
_(updated during loop — add lessons learned here)_

- `cdk synth` must be run from the virtualenv: `source .venv/bin/activate && cdk synth`
- `CfnGuardrail` is an L1 construct — use `aws_cdk.aws_bedrock.CfnGuardrail`
- `guardrailConfig` must be `undefined` (not null) when no guardrail ID — use conditional spread
- Check `enable_guardrails` context with `self.node.try_get_context("enable_guardrails")`, default to `True`
