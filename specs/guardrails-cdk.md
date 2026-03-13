# Spec: Bedrock Guardrails CDK Stack

## Goal
Add AWS Bedrock Guardrails to the CDK infrastructure as an opt-in feature (default ON).

## New file: `stacks/guardrails_stack.py`

### cdk.json context flags

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `enable_guardrails` | `true` | bool | Create guardrail resource. If false, all exports are None |
| `guardrails_content_filter_level` | `"HIGH"` | str | LOW / MEDIUM / HIGH — maps to filter strength |
| `guardrails_pii_action` | `"ANONYMIZE"` | str | ANONYMIZE (redact) or BLOCK (reject response) |

### GuardrailsStack behaviour

- Read `enable_guardrails` context (default `True`)
- If disabled: expose `self.guardrail_id = None`, `self.guardrail_version = None`, skip all resource creation
- If enabled: create `aws_bedrock.CfnGuardrail` with:

**Content filters** (strength maps to level: LOW→LOW, MEDIUM→MEDIUM, HIGH→HIGH):
- HATE, INSULTS, SEXUAL, VIOLENCE, MISCONDUCT — input+output, HIGH strength
- PROMPT_ATTACK — input only, HIGH strength

**Topic denial policies** (6 topics, deny action):
- Cryptocurrency scams / rug pulls
- Phishing / credential harvesting
- Self-harm / suicide instructions
- Weapons manufacturing / explosives
- Malware / ransomware creation
- Identity fraud / document forgery

**Word filters**:
- Enable managed profanity filter
- Custom block list: `AKIA`, `openclaw/gateway-token`, `scoped-creds`, `aws_secret_access_key`

**Sensitive information (PII)**:
- EMAIL, PHONE, CREDIT_DEBIT_CARD_NUMBER, CREDIT_DEBIT_CVV, AWS_ACCESS_KEY, AWS_SECRET_KEY
- Password, PIN
- Action: use `guardrails_pii_action` context value (ANONYMIZE or BLOCK)

**Custom regex patterns**:
- AWS access key: `AKIA[0-9A-Z]{16}` → BLOCK
- AWS secret key: `[0-9a-zA-Z/+]{40}` (40 chars) → ANONYMIZE
- OpenAI-style key: `sk-[a-zA-Z0-9]{48}` → BLOCK

### Outputs / exports
- `self.guardrail_id: str | None` — physical resource ID
- `self.guardrail_version: str | None` — "DRAFT" always (versioned guardrails are a separate resource)
- `self.guardrail_arn: str | None` — full ARN

### Stack wiring (app.py)
- Instantiate `GuardrailsStack` after `SecurityStack`, before `AgentCoreStack`
- Pass `guardrails_stack` to `AgentCoreStack` so it can inject env vars

### IAM
- Add `bedrock:ApplyGuardrail` permission to the AgentCore execution role (in `agentcore_stack.py`)
- Condition: only if `guardrail_arn` is not None

### CDK outputs (CfnOutput)
- `GuardrailId` — for reference in docs / scripts
- `GuardrailArn`

### cdk-nag
- No known violations for CfnGuardrail — suppress any false positives with documented reason

### Cost callout (in README and docs/security.md)
Add a "💰 Cost note" section:
- ~$0.75 per 1,000 text units
- ~$1.50 per 1,000 image units  
- Disable: `"enable_guardrails": false` in `cdk.json`
- Warning: disabling reduces content safety protections
- Link: https://aws.amazon.com/bedrock/pricing/#Guardrails
