# Bedrock Guardrails — Operational Runbook

## Overview

OpenClaw deploys AWS Bedrock Guardrails via the `OpenClawGuardrails` CDK stack to provide content-level defense on every Bedrock Converse/ConverseStream API call. The proxy (`agentcore-proxy.js`) injects `guardrailConfig` into every request — Bedrock evaluates the guardrail server-side.

**Input scope.** The proxy wraps only the person's latest message in `guardContent` (`bridge/guardrail-scope.js`), so input filters assess that text and not the whole transcript: OpenClaw adds its own user-role blocks (a `Runtime: …` trailer, `<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>` data) that together trip `PROMPT_ATTACK`, and a card number blocked earlier in the session would otherwise block every later turn. When the trailing user turn is only tool results (the model is mid tool-call), the most recent earlier user text is tagged instead — tool results cannot carry `guardContent`, and an untagged request falls back to assessing everything, which on us-west-2 staging blocked every turn in which the model used a tool once the history held a card number. Model output is always assessed in full. `PROMPT_ATTACK` at strength HIGH still blocks some imperative phrasings of the user's own text ("Using your browser tool, open … and tell me …" → `PROMPT_ATTACK:LOW` → blocked; "What is the title of the page at …? You can use the browser." passes) — that is filter tuning in `stacks/guardrails_stack.py`.

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

Then redeploy:

```bash
source .venv/bin/activate
cdk deploy OpenClawGuardrails OpenClawAgentCore --require-approval never
```

When disabled, the `GuardrailsStack` creates no resources, `AgentCoreStack` skips the `bedrock:ApplyGuardrail` grant, and `scripts/deploy.sh` does not set `BEDROCK_GUARDRAIL_ID` on the runtime, so the proxy skips `guardrailConfig` injection. Re-run Phase 2 (`./scripts/deploy.sh --runtime-only`) after toggling so the runtime environment matches.

---

## Updating Guardrail Policies

1. Modify `stacks/guardrails_stack.py` (e.g., add a topic denial, change filter strength, add a PII type)
2. Deploy the guardrails stack:
   ```bash
   source .venv/bin/activate
   cdk deploy OpenClawGuardrails --require-approval never
   ```
3. Publish a new version: CloudFormation only creates a new `CfnGuardrailVersion` when that resource changes, so also change its `description` (e.g. `"v2: lower prompt-attack strength"`). Policy edits alone update only the guardrail's working draft, and the runtime stays on the version it was deployed with.
4. Update the runtime so it gets the new `BEDROCK_GUARDRAIL_VERSION`: `./scripts/deploy.sh --runtime-only`. The runtime logs `[proxy] Bedrock Guardrails enabled: <id> v<version>` on startup; check the new number there
5. Run the red team eval to verify the change didn't regress pass rates:
   ```bash
   cd redteam && npx promptfoo@latest eval --config evalconfig.yaml
   ```
6. Run the prompt eval against the old and new versions (next section) to see which prompts changed verdict.

---

## Evaluating Prompts Against a Guardrail Version

`scripts/guardrail-eval.py` calls `bedrock-runtime` `ApplyGuardrail` with `source=INPUT` for each prompt in a fixture file. No model is invoked. It prints one row per prompt (id, expected, actual, which policy fired, e.g. `contentPolicy PROMPT_ATTACK (confidence LOW, strength HIGH) BLOCKED` or `sensitiveInformation CREDIT_DEBIT_CARD_NUMBER BLOCKED`) and never prints prompt text or PII matches.

```bash
GUARDRAIL_ID=$(aws cloudformation describe-stacks --stack-name OpenClawGuardrails \
  --query "Stacks[0].Outputs[?OutputKey=='GuardrailId'].OutputValue" --output text --region $CDK_DEFAULT_REGION)
GUARDRAIL_VERSION=$(aws cloudformation describe-stacks --stack-name OpenClawGuardrails \
  --query "Stacks[0].Outputs[?OutputKey=='GuardrailVersion'].OutputValue" --output text --region $CDK_DEFAULT_REGION)

python3 scripts/guardrail-eval.py --guardrail-id "$GUARDRAIL_ID" --version "$GUARDRAIL_VERSION" \
  --fixtures tests/fixtures/guardrail_prompts.json --region $CDK_DEFAULT_REGION
```

- Exit code `0`: every verdict matched; `1`: at least one mismatch; `2`: fixture or API error (for example a missing `bedrock:ApplyGuardrail` permission).
- The shipped fixtures are synthetic. The `allow` entries imitate scheduled-brief prompts (persona rules, a news digest, a browser request); each one with a `schedule_name` is also sent as `<id>+cron` with the `[Scheduled task: <name>] ` prefix the cron Lambda adds, which shows whether the prefix changes the verdict. The `block` entries are probes (public test card number, CVV, the AWS documentation example access key, a prompt-injection string) that the default configuration must block.
- To check your own schedule prompts, copy the fixture file outside the repository, add entries (`{"id": ..., "expected": "allow", "schedule_name": ..., "text": ...}`) and pass that path to `--fixtures`. Do not commit real users' prompts.
- Each prompt is billed as a guardrail evaluation (see Cost Estimates).

### Known limitation: scheduled-task prompts

At `PROMPT_ATTACK` input strength `HIGH`, ordinary scheduled-brief prompts can be blocked as prompt attacks: an instruction to deliver the brief to the user on a named channel, or persona-style rules at the start of the prompt. A digest about cryptocurrency prices can also match the `CryptoScams` denied topic. The cron task then returns the blocked message instead of the brief. Run this eval against your own cron prompts before enabling guardrails for existing users, then reword the prompts or tune `stacks/guardrails_stack.py` (and publish a new version, see above).

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
- Filter strength does not change the price (billing is per text unit). The `guardrails_content_filter_level` context key is not read by the stack; strengths are set in `stacks/guardrails_stack.py`

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

**Environment variables** (set on the runtime by `scripts/deploy.sh` Phase 2, read from the `OpenClawGuardrails` stack outputs `GuardrailId` / `GuardrailVersion`):
- `BEDROCK_GUARDRAIL_ID` — guardrail identifier
- `BEDROCK_GUARDRAIL_VERSION` — pinned guardrail version

The deploy fails if `enable_guardrails` is true and either output resolves empty, so a runtime is never configured with guardrails silently off. On startup the proxy logs `[proxy] Bedrock Guardrails enabled: <id> v<version>`; on a block it logs `[guardrail] intervention on streaming response` (or `non-streaming`).

**IAM**: Execution role has `bedrock:ApplyGuardrail` permission (scoped to `arn:aws:bedrock:{region}:{account}:guardrail/*`), granted by `AgentCoreStack` from the `guardrail_id` that `app.py` passes in. `tests/test_guardrail_wiring_synth.py` asserts this at synth time.
