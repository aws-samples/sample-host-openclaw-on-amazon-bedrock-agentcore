# Operations

Operator commands for a deployed stack. Moved out of the [README](../README.md). AgentCore Runtime V2 uses the pinned SDK controller (`boto3` / `botocore` 1.43.95); see the [AgentCore Runtime V2 runbook](runtime-v2.md) for setup and verification. Deployment commands operate on live AWS resources.

## Check AgentCore Runtime status

```bash
uv run --no-project --python .venv-deploy/bin/python python scripts/runtime_deploy.py verify \
  --region "$CDK_DEFAULT_REGION" --manifest .runtime/runtime-v2.json --require-enrollment-ready
```

The manifest records the deployment target. For a particular user, read `USER#<userId>` / `SESSION`: its `runtimeArn`, `endpointName`, `sessionId`, generation and canonical actor are authoritative even after a newer release is deployed. Do not substitute the current manifest for an existing route.

## Inspect sessions

Use the identity table's saved V2 route when inspecting or stopping a test session. `tests/e2e/session.py` resolves the route before stopping it and retains all identity/session records. Stop does not prove checkpoint completion or authorize migration. Missing or incomplete routes require reconciliation; there is no default AgentCore Runtime fallback or session-deletion reset.

Unknown invocation outcomes retain `activeAdmissions` and `inFlight`. Reconcile the exact invocation before changing state; do not clear ownership records or admission tokens to force a new registration.

## Deploy a new bridge version

Install the `uv` environments described in [Quick Start](../README.md#quick-start). Set `image_version` or supply a reviewed ARM64 image. Preserve the previous release manifest, choose an unused endpoint name, then run:

```bash
./scripts/deploy.sh --runtime-only --endpoint-name release_2
./scripts/deploy.sh --phase3
```

Add `--image-uri "$IMAGE_URI"` to use a prebuilt image. The default manifest is `.runtime/runtime-v2.json`, passed to CDK as `runtime_manifest`. Initial deployment uses `release_1`; subsequent changed releases require new immutable endpoint names. Existing users retain their saved endpoint/version. A CDK-only deployment does not update the AgentCore Runtime image.

## Update shared stacks

```bash
./scripts/deploy.sh --phase1
./scripts/deploy.sh --phase3 --manifest .runtime/runtime-v2.json
```

Phase 1 needs no manifest. Phase 3 requires successful warmup and live V2 readback; it grants invocation permissions and configures new registrations. Preserve older endpoints and dependencies used by existing routes. Platform selectors, V1 maintenance, fallback and enrollment opt-in flags are removed.

## Run tests

Install the pinned SDK and pytest using the [AgentCore Runtime V2 offline setup](runtime-v2.md#offline-checks). Those checks use fake clients/servers and synthetic CDK context. Run each command below independently from the repository root. Keep Router and Cron tests in separate Python processes because both use a module named `index`. Channel E2E uses real AWS resources, model/tool calls and channel messages; use a dedicated V2 test identity and its saved route.

```bash
cd bridge && node --test *.test.js                     # bridge unit tests (Node 24)
cd bridge && node --test proxy-identity.test.js       # identity + workspace tests
cd bridge && node --test image-support.test.js         # image upload + multimodal tests
cd bridge && node --test subagent-routing.test.js      # subagent model routing + detection tests
cd bridge && node --test content-extraction.test.js    # recursive content block extraction tests
cd bridge && node --test scoped-credentials.test.js    # per-user STS credential scoping tests
cd bridge && node --test workspace-sync.test.js        # workspace sync + SQLite snapshot tests
cd bridge && node --test state-storage.test.js         # local state dir / session-storage mirror + restore tests
cd bridge && node --test read-body.test.js             # UTF-8 request body decoding + size cap (8 tests)
cd bridge && node --test openclaw-tool-deny.test.js    # channel-delivery tools denied in openclaw.json (7 tests)
cd bridge/skills/api-keys && node --test migrate.test.js # api-keys migrate.js/native.js key-loss tests (9 tests)
cd bridge && node --test gateway-mcp.test.js           # Gateway MCP config and Cognito token provider (8 tests)
cd bridge && node --test runtime-skills.test.js        # runtime skill manifest + cold-start reinstall (48 tests)
node --test lambda/gateway_tools/*.test.js             # Gateway tool Lambdas: JWT verification, namespace scoping, interceptor (31 tests, Node 24)
cd bridge/skills/s3-user-files && AWS_REGION=$CDK_DEFAULT_REGION node --test common.test.js  # S3 skill tests
PYTHONPATH=lambda/shared/python:lambda/router uv run --no-project --python .venv/bin/python python -m pytest lambda/router -q
PYTHONPATH=lambda/shared/python:lambda/cron uv run --no-project --python .venv/bin/python python -m pytest lambda/cron -q
uv run --no-project --python .venv/bin/python python -m pytest tests/test_agentcore_exec.py -v                     # operator CLI tests (mocked boto3)
uv run --no-project --python .venv/bin/python python -m pytest tests/test_guardrail_eval.py -v                     # guardrail eval script tests (stubbed ApplyGuardrail)
uv run --no-project --python .venv/bin/python python -m pytest tests/test_state_bucket_lifecycle_synth.py -v       # user-files bucket lifecycle rules (synth, no AWS)
uv run --no-project --python .venv/bin/python python -m pytest tests/test_gateway_stack_synth.py -v                # OpenClawGateway synth: flag off leaves the 8 templates unchanged, flag on adds the stack (10 tests, no AWS)

# E2E tests (requires deployed stack + E2E_TELEGRAM_CHAT_ID/E2E_TELEGRAM_USER_ID env vars)
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k smoke               # connectivity + webhook auth
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k lifecycle            # full message lifecycle
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k cold_start           # first registration; skips existing identities
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k full_startup          # V2 USER_READY + full OpenClaw completion
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k ScopedCredentials     # S3 file write/read/delete via scoped creds
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k conversation          # multi-turn + rapid-fire
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k SkillManagement       # clawhub skill install/uninstall/list
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k ApiKeyManagement      # API key storage (native + Secrets Manager)
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k CronSchedule          # cron lifecycle + CRON# DynamoDB record check
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k GuardrailSecurity     # guardrail content filtering (requires BEDROCK_GUARDRAIL_ID env var, see below)
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/test_guardrail_wiring.py -v             # guardrail WIRING: passes only on a logged guardrail intervention
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/test_gateway_tools.py -v -m gateway     # Gateway MCP tools (4 tests; needs enable_gateway=true, skipped when OpenClawGateway is not deployed)
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v                          # all E2E tests
```

`scripts/e2e-deploy-and-test.sh` defaults to `tests/e2e/bot_test.py::TestSmoke` and `tests/e2e/bot_test.py::TestMessageLifecycle`. Repeating `--test-node` replaces that selection. `--skip-deploy` still requires manifest enrollment verification. The runner never deletes SESSION records, stops AgentCore Runtime sessions or rewrites `image_version`.

When `E2E_TELEGRAM_CHAT_ID` is set, the runner attempts a Telegram start notice before deployment/verification and a completion notice after pytest finishes. Notification failures do not change the test result. Deployment or verification failures exit before the completion notice. Pytest output is saved to `${E2E_RESULTS_FILE:-/tmp/e2e-results.txt}` and summarized from the log; the runner returns pytest's exit code. Consult the full output for authoritative test results.

```bash
# Live E2E: use a dedicated V2 test identity and its saved route.
./scripts/e2e-deploy-and-test.sh --skip-deploy --manifest .runtime/runtime-v2.json
./scripts/e2e-deploy-and-test.sh --skip-deploy \
  --test-node tests/e2e/bot_test.py::TestFullStartup \
  --test-node tests/e2e/bot_test.py::TestConversation
```

Selected tests retain their own model/tool side effects, in addition to the runner's Telegram notices. A saved user's route can refer to an older release than the manifest; record both when attributing evidence. Skipped or unexecuted suites do not count as release evidence.

## Security validation

```bash
cdk synth   # Runs cdk-nag AwsSolutions checks — should produce no errors
```
