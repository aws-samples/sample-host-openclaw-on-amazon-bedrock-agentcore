# Bedrock Guardrails — Operational Runbook

## Overview

OpenClaw deploys AWS Bedrock Guardrails via the `OpenClawGuardrails` CDK stack to provide content-level defense on every Bedrock Converse/ConverseStream API call. The proxy (`agentcore-proxy.js`) injects `guardrailConfig` into every request — Bedrock evaluates the guardrail server-side.

**Input scope.** The proxy wraps every text block in the latest user turn in `guardContent` (`bridge/guardrail-scope.js`). Input filters assess that turn instead of reassessing blocked text from earlier turns. Text is never exempted because it begins with an internal-looking marker such as `<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>`. When the trailing turn contains only tool results, all text in the most recent earlier user turn is tagged instead; tool results remain unchanged. If no user text exists, the request retains the default assessment scope. Model output is always assessed in full. The headless provider configuration keeps OpenClaw's generated Runtime metadata in system messages; see [AgentCore Runtime V2 compatibility](runtime-v2.md#lifecycle-and-compatibility).

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

Do not disable Guardrails and redeploy the shared stacks until you have verified that no saved route, pinned AgentCore Runtime version or active/in-flight session depends on the Guardrail. Preserve the shared Guardrail, its published versions and the execution role's `bedrock:ApplyGuardrail` permission while any dependency remains or cannot be ruled out. Creating a new endpoint does not retire older releases or their dependencies.

Only after that dependency check, set in `cdk.json`:

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

Redeploying with the flag disabled removes the shared Guardrail resources and the execution role's `bedrock:ApplyGuardrail` grant. `scripts/deploy.sh` also omits `BEDROCK_GUARDRAIL_ID` from the new AgentCore Runtime release, so its proxy skips `guardrailConfig` injection. Deploy that release through a new immutable V2 endpoint using the [runbook](runtime-v2.md); do not repoint an endpoint already serving enrolled users.

---

## Updating Guardrail Policies

1. Modify the policy in `stacks/guardrails_stack.py` and change the `CfnGuardrailVersion` resource's `description` (e.g. `"v2: lower prompt-attack strength"`) **before deployment**. Policy edits alone update the working draft; the version resource must change to publish a new numbered version.
2. Record the deployed version, deploy both edits, then read back the new output:
   ```bash
   source .venv/bin/activate
   OLD_GUARDRAIL_VERSION=$(aws cloudformation describe-stacks --stack-name OpenClawGuardrails \
     --region "$CDK_DEFAULT_REGION" \
     --query "Stacks[0].Outputs[?OutputKey=='GuardrailVersion'].OutputValue" --output text)
   cdk deploy OpenClawGuardrails --require-approval never
   NEW_GUARDRAIL_VERSION=$(aws cloudformation describe-stacks --stack-name OpenClawGuardrails \
     --region "$CDK_DEFAULT_REGION" \
     --query "Stacks[0].Outputs[?OutputKey=='GuardrailVersion'].OutputValue" --output text)
   [[ "$NEW_GUARDRAIL_VERSION" =~ ^[1-9][0-9]*$ ]] && \
     [ "$NEW_GUARDRAIL_VERSION" != "$OLD_GUARDRAIL_VERSION" ]
   ```
3. Stop if deployment or either output check fails. A missing, non-numeric or unchanged output does not establish a newly published version.
4. Deploy a new V2 release with the verified `BEDROCK_GUARDRAIL_VERSION`: `./scripts/deploy.sh --runtime-only --endpoint-name release_2`, using an unused endpoint name. This consumes the stack output; it does not publish a Guardrail version. Existing enrolled users keep their pinned AgentCore Runtime version. Check the new number in `[proxy] Bedrock Guardrails enabled: <id> v<version>` on startup.
   To direct new registrations to this release, run `./scripts/deploy.sh --phase3 --manifest .runtime/runtime-v2.json` after successful warmup.
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

### Known limitation: sub-agent requests

Sub-agent requests go through the same Bedrock proxy, and the guardrail assesses their latest user turn: the task text the main agent wrote for the sub-agent. That text often reads like an instruction to an assistant ("you are a research assistant, find ..."), so it can be blocked as a prompt attack in the same way as a scheduled-task prompt. The sub-agent then returns the guardrail's blocked message to the main agent instead of a result. If sub-agent tasks are blocked, check the [guardrail trace logs](#guardrail-trace-logs) for the policy that fired and tune `stacks/guardrails_stack.py`.

---

## Wiring and End-to-End Tests

How the guardrail reaches the AgentCore Runtime and how the end-to-end tests prove it (moved from the README).

### Guardrail wiring

The guardrail is only applied if two things are true at once, and `scripts/deploy.sh` sets both:

1. **IAM** — `app.py` passes the `OpenClawGuardrails` outputs into `AgentCoreStack`, which grants the AgentCore Runtime execution role `bedrock:ApplyGuardrail`. `tests/test_guardrail_wiring_synth.py` asserts this at synth time so a rebase cannot drop it again (it did once, in #30 — see #100).
2. **AgentCore Runtime env** — Phase 2 of `scripts/deploy.sh` reads the `GuardrailId` / `GuardrailVersion` outputs of `OpenClawGuardrails` (exact `OutputKey` match) and passes them to the AgentCore Runtime as `BEDROCK_GUARDRAIL_ID` / `BEDROCK_GUARDRAIL_VERSION`. `bridge/agentcore-proxy.js` only injects `guardrailConfig` into Bedrock calls when `BEDROCK_GUARDRAIL_ID` is set. With `enable_guardrails: true` (the default) the deploy **fails** if either output resolves empty rather than silently shipping an AgentCore Runtime without guardrails; with `enable_guardrails: false` the variables are simply not set.

Exporting `BEDROCK_GUARDRAIL_ID` in your own shell does **not** configure the AgentCore Runtime — that only happens through `scripts/deploy.sh` (or `--runtime-only`). An AgentCore Runtime that has it set logs `[proxy] Bedrock Guardrails enabled: <id> v<version>` on startup.

### Guardrail E2E Tests

`tests/e2e/test_guardrail_wiring.py` verifies the wiring end to end. `test_guardrail_blocks_harmful_content` passes only when the application log (`/openclaw/container`, scoped by the canonical actor from the saved V2 route) contains the proxy's `[guardrail] intervention ...` line, i.e. Bedrock returned `stopReason: guardrail_intervened`. A refusal written by the model itself does not count, so this test fails on a deployment where the guardrail is deployed but not wired.

```bash
pytest tests/e2e/test_guardrail_wiring.py -v
```

The `TestGuardrailSecurity` test class in `bot_test.py` (6 tests) exercises guardrail behaviour through the full Telegram webhook pipeline. It is gated on `BEDROCK_GUARDRAIL_ID` being set **in the test runner's shell** (this only selects the tests; the AgentCore Runtime gets its value from the deploy):

```bash
# Requires deployed stack + guardrail ID
export BEDROCK_GUARDRAIL_ID=$(aws cloudformation describe-stacks \
  --stack-name OpenClawGuardrails \
  --query "Stacks[0].Outputs[?OutputKey=='GuardrailId'].OutputValue" \
  --output text --region ap-southeast-2)
pytest tests/e2e/bot_test.py -v -k GuardrailSecurity
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

**Environment variables** (set on the AgentCore Runtime by `scripts/deploy.sh` Phase 2, read from the `OpenClawGuardrails` stack outputs `GuardrailId` / `GuardrailVersion`):
- `BEDROCK_GUARDRAIL_ID` — guardrail identifier
- `BEDROCK_GUARDRAIL_VERSION` — pinned guardrail version

The deploy fails if `enable_guardrails` is true and either output resolves empty, so an AgentCore Runtime is never configured with guardrails silently off. On startup the proxy logs `[proxy] Bedrock Guardrails enabled: <id> v<version>`; on a block it logs `[guardrail] intervention on streaming response` (or `non-streaming`).

**IAM**: Execution role has `bedrock:ApplyGuardrail` permission (scoped to `arn:aws:bedrock:{region}:{account}:guardrail/*`), granted by `AgentCoreStack` from the `guardrail_id` that `app.py` passes in. `tests/test_guardrail_wiring_synth.py` asserts this at synth time.
