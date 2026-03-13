# IMPLEMENTATION_PLAN.md

## Goal
Add AWS Bedrock Guardrails + promptfoo red team testing to openclaw-on-agentcore.

## Tasks

### Phase 1: Design Doc Finalisation
- [x] **TASK-1**: Merge `docs/redteam-guardrails-addendum.md` into `docs/redteam-design.md`
  - Addendum file never existed separately — opt-in/opt-out content already in `docs/redteam-design.md` (sections 2.9, cost callout)
  - No commit needed

### Phase 2: CDK — GuardrailsStack
- [x] **TASK-2**: Create `stacks/guardrails_stack.py`
  - Implemented `GuardrailsStack` per `specs/guardrails-cdk.md`
  - Read `enable_guardrails` context (default True), `guardrails_pii_action` context
  - Content filters (6), topic denial (6), word filters (7 + profanity), PII (10 entities), custom regex (3)
  - CfnGuardrailVersion for production pinning
  - Expose `guardrail_id`, `guardrail_version`, `guardrail_arn`
  - CfnOutputs for GuardrailId, GuardrailVersion, GuardrailArn
  - PII entity types verified against CloudFormation docs (EMAIL, PHONE, not EMAIL_ADDRESS/PHONE_NUMBER)

- [x] **TASK-3**: Wire `GuardrailsStack` into `app.py`
  - Instantiated after SecurityStack, before AgentCoreStack
  - Passed `guardrail_id` and `guardrail_version` to AgentCoreStack (with `or ""` for None safety)
  - Added `guardrail_id`/`guardrail_version` params to AgentCoreStack constructor (minimal, for synth)
  - `cdk synth` passed — all 8 stacks including OpenClawGuardrails

- [x] **TASK-4**: Update `stacks/agentcore_stack.py`
  - Added `BEDROCK_GUARDRAIL_ID` + `BEDROCK_GUARDRAIL_VERSION` env vars to container
  - Added `bedrock:ApplyGuardrail` IAM permission (conditional on `guardrail_id`)
  - Updated cdk-nag IAM5 suppression with guardrail ARN wildcard
  - `cdk synth` passed

### Phase 3: Bridge — agentcore-proxy.js
- [x] **TASK-5**: Update `bridge/agentcore-proxy.js`
  - Read env vars, build `guardrailConfig` (undefined when ID absent)
  - Injected into both Converse + ConverseStream params via spread
  - Trace logging: non-streaming `response.trace.guardrail`, streaming `event.metadata.trace.guardrail`
  - Intervention handling: `stopReason: "guardrail_intervened"` logged at WARN, no retry
  - `node --check` passed

### Phase 4: Red Team Folder
- [x] **TASK-6**: Scaffold `redteam/` folder
  - Created folder structure: `providers/`, `tests/`, `results/`
  - `package.json`, `.gitignore`, `.env.example`, `system-prompt.txt`
  - `promptfooconfig.yaml` with full redteam config (20+ plugins, 7 strategies, 2 frameworks)
  - `providers/baseline.yaml` + `providers/hardened.yaml`
  - `results/.gitkeep` + `results/README.md`

- [ ] **TASK-7**: Write test suites
  - `tests/jailbreaks.yaml`
  - `tests/prompt-injection.yaml`
  - `tests/harmful-content.yaml`
  - `tests/pii-fishing.yaml`
  - `tests/topic-denial.yaml`
  - `tests/credential-extraction.yaml`
  - Min 3 prompts + assertions per file (mapped to threat model in `docs/security.md`)
  - Commit: `feat(redteam): add promptfoo test suites for all attack categories`

- [ ] **TASK-8**: Write `redteam/README.md`
  - Prerequisites, setup, before/after story, commands
  - Commit: `docs(redteam): add README with before/after demo story`

### Phase 5: Documentation
- [ ] **TASK-9**: Update `docs/security.md`
  - Add Bedrock Guardrails to Section 3 (Defense-in-Depth)
  - Add cost note (💰 Cost note callout)
  - Add to Section 6 (Potential Extensions) — remove guardrails since now implemented
  - Commit: `docs: update security.md with Bedrock Guardrails coverage`

- [ ] **TASK-10**: Update `README.md`
  - Add Bedrock Guardrails to features/architecture section
  - Add `enable_guardrails` to cdk.json configuration table
  - Add 💰 cost note
  - Commit: `docs: add Bedrock Guardrails to README`

## Learnings
- TASK-1: Addendum file was never created separately; content was written directly into redteam-design.md
- TASK-2: CloudFormation PII entity types use short names: `EMAIL` not `EMAIL_ADDRESS`, `PHONE` not `PHONE_NUMBER`, `AWS_ACCESS_KEY` not `AWS_ACCESS_KEY_ID`

## Status
IN_PROGRESS
