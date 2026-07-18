# Bedrock Guardrails — Operational Runbook

> **Archived upstream operational reference.** Do not deploy or update stacks
> from this document. Personal Operator infrastructure changes use the
> exact-commit, write-ahead journal in [OPERATIONS.md](OPERATIONS.md) through
> `scripts/staging-release.py` (or its `scripts/deploy.sh` compatibility shim).
> Direct CDK deployment bypasses reviewed change sets and is prohibited.

## Overview

OpenClaw deploys AWS Bedrock Guardrails via the `OpenClawGuardrails` CDK stack to provide content-level defense on every Bedrock Converse/ConverseStream API call. The proxy (`agentcore-proxy.js`) injects `guardrailConfig` into every request — Bedrock evaluates the guardrail server-side.

**Stack**: `OpenClawGuardrails` (`stacks/guardrails_stack.py`)
**Default**: Enabled (`enable_guardrails = true`)

---

## What Is Configured

### Content Filters

| Category | Input Strength | Output Strength |
|----------|---------------|-----------------|
| HATE | HIGH | HIGH |
| INSULTS | MEDIUM | HIGH |
| SEXUAL | HIGH | HIGH |
| VIOLENCE | HIGH | HIGH |
| MISCONDUCT | HIGH | HIGH |
| PROMPT_ATTACK | HIGH | NONE (input only) |

### Topic Denial (6 denied topics)

| Topic | Definition |
|-------|-----------|
| CryptoScams | Investment schemes, pump-and-dump, fake token promotion |
| Phishing | Phishing emails, fake login pages, social engineering |
| SelfHarm | Instructions or encouragement for self-harm or suicide |
| WeaponsManufacturing | Building weapons, explosives, dangerous devices |
| MalwareCreation | Ransomware, keyloggers, trojans, exploit code |
| IdentityFraud | Fake IDs, forging documents, identity theft |

### PII Filters (10 entity types)

| Entity | Action |
|--------|--------|
| EMAIL | ANONYMIZE (configurable via `guardrails_pii_action`) |
| PHONE | ANONYMIZE (configurable) |
| CREDIT_DEBIT_CARD_NUMBER | BLOCK (always) |
| CREDIT_DEBIT_CARD_CVV | BLOCK (always) |
| CREDIT_DEBIT_CARD_EXPIRY | BLOCK (always) |
| AWS_ACCESS_KEY | BLOCK |
| AWS_SECRET_KEY | BLOCK |
| USERNAME | ANONYMIZE (configurable) |
| PASSWORD | BLOCK |
| PIN | BLOCK |

### Word Filters

- **Managed profanity list**: AWS-managed (enabled)
- **Custom block list**: `AKIA`, `aws_secret_access_key`, `aws_access_key_id`, `openclaw/gateway-token`, `openclaw/cognito-password-secret`, `/tmp/scoped-creds`, `credential_process`

### Custom Regex Patterns

| Name | Pattern | Action |
|------|---------|--------|
| AWSAccessKeyId | `AKIA[0-9A-Z]{16}` | BLOCK |
| AWSSecretKey | `[0-9a-zA-Z/+=]{40}` | ANONYMIZE |
| GenericAPIKey | `sk-[a-zA-Z0-9]{20,}` | ANONYMIZE |

---

## Enable / Disable

### Enable (default)

Guardrails are enabled by default. No action needed.

### Disable

Set in `cdk.json`:

```json
{
  "context": {
    "enable_guardrails": false
  }
}
```

The former upstream workflow deployed these stacks directly. That path is
retired and must not be reconstructed from repository history. Treat a change
to this setting as a new exact-commit release candidate: run the credential-free
local gates, create the canonical staging journal, and follow the separately
reviewed phase sequence in [OPERATIONS.md](OPERATIONS.md). No change is active
until that journaled release closes its external gates.

When disabled, the `GuardrailsStack` creates no resources. The proxy receives empty guardrail env vars and skips `guardrailConfig` injection.

---

## Updating Guardrail Policies

1. Modify `stacks/guardrails_stack.py` (e.g., add a topic denial, change filter strength, add a PII type)
2. Run the credential-free release checks against the exact clean commit:
   ```bash
   ./scripts/test-local.sh
   git diff --check
   ```
3. Use the preflight and separately reviewed journaled phases documented in
   [OPERATIONS.md](OPERATIONS.md). Do not invoke CDK directly.
4. A successful journaled release creates a new `CfnGuardrailVersion`. The
   container picks up the new version on the next session start.
5. Run the red team eval to verify the change didn't regress pass rates:
   ```bash
   cd redteam && npx promptfoo@latest eval --config evalconfig.yaml
   ```

---

## Monitoring

### CloudWatch Metrics

Bedrock publishes guardrail metrics to the `AWS/Bedrock` namespace:

- `GuardrailsInvocations` — total guardrail evaluations
- `GuardrailsBlocked` — requests blocked by guardrails

### Guardrail Trace Logs

When Bedrock invocation logging is enabled (configured in `ObservabilityStack`), guardrail evaluations appear in the invocation logs at `/aws/bedrock/invocation-logs`. Each log entry includes:

- `guardrailAction`: `NONE`, `INTERVENED`, or `GUARDRAIL_INTERVENED`
- `guardrailOutputs`: which policy triggered and what action was taken

Query via CloudWatch Logs Insights:

```
fields @timestamp, guardrailAction, guardrailOutputs
| filter guardrailAction = "GUARDRAIL_INTERVENED"
| sort @timestamp desc
| limit 50
```

---

## Cost Estimates

| Monthly Message Volume | Estimated Guardrail Cost |
|-----------------------|-------------------------|
| 1,000 messages | ~$0.75 |
| 10,000 messages | ~$7.50 |
| 100,000 messages | ~$75 |
| 1,000,000 messages | ~$750 |

Pricing: ~$0.75 per 1,000 text units (input + output). See [AWS Bedrock Guardrails Pricing](https://aws.amazon.com/bedrock/pricing/#Guardrails).

To reduce cost:
- Set `"enable_guardrails": false` — removes all guardrail charges
- Use `guardrails_content_filter_level: "MEDIUM"` or `"LOW"` for reduced sensitivity

---

## Red Team Evidence

The `redteam/` directory validates guardrail effectiveness. Results from 62 test cases:

| Metric | Without Guardrails | With Guardrails | Improvement |
|--------|-------------------|-----------------|-------------|
| Overall pass rate | ~77% | ~93% | +16pp |
| Harmful content blocked | ~30% | ~95% | +65pp |
| PII redaction rate | ~10% | ~90% | +80pp |
| Topic denial effectiveness | ~20% | ~95% | +75pp |

To reproduce:

```bash
cd redteam && npm install
AWS_REGION=ap-southeast-2 npx promptfoo@latest eval --config evalconfig.yaml
npx promptfoo@latest view
```

---

## Architecture

```
User message → Router Lambda → AgentCore → agentcore-proxy.js
                                              │
                                              ├── guardrailConfig injected into
                                              │   every ConverseStream call
                                              │
                                              v
                                        Amazon Bedrock
                                              │
                                    ┌─────────┴─────────┐
                                    │  Guardrail eval    │
                                    │  (server-side)     │
                                    │                    │
                                    │  Content filters   │
                                    │  Topic denial      │
                                    │  PII filters       │
                                    │  Word filters      │
                                    │  Custom regex      │
                                    │  Prompt attack     │
                                    └─────────┬─────────┘
                                              │
                                    ┌─────────┴─────────┐
                                    │ Pass: Model        │
                                    │ response returned  │
                                    │                    │
                                    │ Block: Rejection   │
                                    │ message returned   │
                                    └───────────────────┘
```

**Environment variables** (set by CDK on the container):
- `BEDROCK_GUARDRAIL_ID` — guardrail identifier
- `BEDROCK_GUARDRAIL_VERSION` — pinned guardrail version

**IAM**: Execution role has `bedrock:ApplyGuardrail` permission (scoped to `arn:aws:bedrock:{region}:{account}:guardrail/*`).
