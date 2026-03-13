# OpenClaw Red Team — Bedrock Guardrails Before/After

Developer-only adversarial testing harness for OpenClaw's Bedrock Guardrails. Uses [promptfoo](https://promptfoo.dev/) to generate and evaluate adversarial prompts against the Bedrock model with and without guardrails.

**This folder is NOT deployed** — it is a local testing tool for security evaluation.

## Prerequisites

- AWS credentials with Bedrock access (`bedrock:InvokeModel`, `bedrock:Converse`)
- Deployed `OpenClawGuardrails` stack (for hardened provider)
- Node.js 18+

## Setup

```bash
cd redteam
cp .env.example .env
# Fill in AWS credentials and guardrail IDs

npm install
```

Get guardrail IDs from CDK outputs:

```bash
export BEDROCK_GUARDRAIL_ID=$(aws cloudformation describe-stacks \
  --stack-name OpenClawGuardrails \
  --query "Stacks[0].Outputs[?OutputKey=='GuardrailId'].OutputValue" \
  --output text --region ap-southeast-2)
export BEDROCK_GUARDRAIL_VERSION=$(aws cloudformation describe-stacks \
  --stack-name OpenClawGuardrails \
  --query "Stacks[0].Outputs[?OutputKey=='GuardrailVersion'].OutputValue" \
  --output text --region ap-southeast-2)
```

## The Before/After Story

### Act 1 — Baseline (Vulnerable)

Run the red team suite against the model **without** guardrails:

```bash
npm run run:baseline
```

Expected: many test failures — jailbreaks succeed, PII is generated, harmful content passes through.

### Act 2 — Deploy Guardrails

Deploy the `OpenClawGuardrails` CDK stack and redeploy `OpenClawAgentCore`:

```bash
cd .. && source .venv/bin/activate
cdk deploy OpenClawGuardrails OpenClawAgentCore --require-approval never
```

### Act 3 — Hardened (Improved)

Run the same suite against the model **with** guardrails:

```bash
npm run run:hardened
```

Expected: significant improvement — content filters block harmful content, topic denial catches scam/phishing prompts, PII filters redact sensitive data, prompt attack detection flags jailbreaks.

### Side-by-Side Comparison

Run both providers and view the comparison report:

```bash
npm run run        # runs promptfoo redteam against both providers
npm run report     # opens interactive HTML report
```

## Commands

| Command | Description |
|---------|-------------|
| `npm run generate` | Generate adversarial test cases |
| `npm run run` | Run full red team (both providers) |
| `npm run run:baseline` | Run baseline only (no guardrails) |
| `npm run run:hardened` | Run hardened only (with guardrails) |
| `npm run report` | Open interactive report in browser |
| `npm run view` | Start promptfoo web UI |

## Test Categories

| File | Tests | Guardrail Mapping |
|------|-------|-------------------|
| `jailbreaks.yaml` | 4 | PROMPT_ATTACK content filter |
| `prompt-injection.yaml` | 4 | PROMPT_ATTACK content filter |
| `harmful-content.yaml` | 4 | VIOLENCE, MISCONDUCT filters + topic denial |
| `pii-fishing.yaml` | 4 | PII filters + custom regex |
| `topic-denial.yaml` | 5 | 6 denied topics (crypto, phishing, malware, etc.) |
| `credential-extraction.yaml` | 4 | Word filters + PII filters |
| `tool-abuse.yaml` | 8 | SSRF, namespace traversal, injection, resource exhaustion |
| `credential-channel.yaml` | 8 | Channel secrets, identity confusion, infra metadata |
| `content-filters.yaml` | 7 | HATE, SEXUAL, INSULTS filters + EMAIL/PHONE/PASSWORD/PIN PII |
| `regex-pii.yaml` | 4 | AWS secret key regex, OpenAI `sk-` regex, word filters |
| `encoding-bypasses.yaml` | 6 | Base64, ROT13, multilingual, Unicode, fragmented bypasses |
| `session-context.yaml` | 4 | Session hijacking, context poisoning, workspace injection |

**Total: 62 test cases across 12 files.**

## Grader Configuration

The `llm-rubric` assertions use **Bedrock Claude Sonnet** as the grader — no `OPENAI_API_KEY` needed. This is configured in `evalconfig.yaml` under `defaultTest.options.provider`.

## Expected Results

| Metric | Baseline | Hardened |
|--------|----------|---------|
| Overall pass rate | ~40-60% | ~85-95% |
| Harmful content blocked | ~30% | ~95% |
| PII redaction rate | ~10% | ~90% |
| Jailbreak resistance | ~50% | ~85% |
| Topic denial effectiveness | ~20% | ~95% |
| Tool abuse resistance | ~20% | ~80% |
| Credential leakage blocked | ~30% | ~90% |

## Saving Result Snapshots

```bash
# After a baseline run
cp .promptfoo/output/latest.json results/$(date +%Y%m%d)-baseline.json

# After a hardened run
cp .promptfoo/output/latest.json results/$(date +%Y%m%d)-hardened.json
```

## Cost

Each full red team run costs approximately $5-15 depending on `numTests` settings in `promptfooconfig.yaml`. The adversarial prompt generator (Claude Sonnet) and target model (MiniMax M2.1) both incur Bedrock charges. Guardrail evaluation adds ~$0.75 per 1,000 text units on the hardened provider.
