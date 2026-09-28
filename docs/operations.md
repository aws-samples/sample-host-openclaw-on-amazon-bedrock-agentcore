# Operations

Operator commands for a deployed stack. Moved out of the [README](../README.md).


## Check runtime status

```bash
# The Runtime is created by the Starter Toolkit, not CDK; deploy.sh writes its id
# into cdk.json context (runtime_id) and .bedrock_agentcore.yaml.
RUNTIME_ID=$(python3 -c "import json; print(json.load(open('cdk.json'))['context']['runtime_id'])")

aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id $RUNTIME_ID \
  --region $CDK_DEFAULT_REGION
```

## Check DynamoDB identity table

```bash
aws dynamodb scan --table-name openclaw-identity --region $CDK_DEFAULT_REGION
```

## Deploy new bridge version

```bash
# 1. Bump image_version in cdk.json (or use -c image_version=N on the CLI)
#    This forces AgentCore to pull the new container image.
# 2. Build + push image
VERSION=$(python3 -c "import json; print(json.load(open('cdk.json'))['context']['image_version'])")
docker build --platform linux/arm64 -t openclaw-bridge:v${VERSION} bridge/
docker tag openclaw-bridge:v${VERSION} \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:v${VERSION}
aws ecr get-login-password --region $CDK_DEFAULT_REGION | \
  docker login --username AWS --password-stdin \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com
docker push \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:v${VERSION}
# 3. CDK deploy
cdk deploy OpenClawAgentCore --require-approval never
# 4. New sessions will use the new image automatically (per-user idle termination)
```

## Run tests

```bash
cd bridge && node --test *.test.js                     # all bridge unit tests (539 tests, Node 24)
cd bridge && node --test proxy-identity.test.js       # identity + workspace tests
cd bridge && node --test image-support.test.js         # image upload + multimodal tests
cd bridge && node --test lightweight-agent.test.js     # lightweight agent tools + buildToolArgs tests
cd bridge && node --test subagent-routing.test.js      # subagent model routing + detection tests
cd bridge && node --test content-extraction.test.js    # recursive content block extraction tests
cd bridge && node --test scoped-credentials.test.js    # per-user STS credential scoping tests
cd bridge && node --test workspace-sync.test.js        # workspace sync + SQLite snapshot tests
cd bridge && node --test state-storage.test.js         # local state dir / session-storage mirror + restore tests
cd bridge && node --test read-body.test.js             # UTF-8 request body decoding + size cap (8 tests)
cd bridge && node --test openclaw-tool-deny.test.js    # channel-delivery tools denied in openclaw.json (7 tests)
cd bridge/skills/api-keys && node --test migrate.test.js # api-keys migrate.js/native.js key-loss tests (9 tests)
cd bridge && node --test gateway-mcp.test.js           # Gateway MCP config, bearer refresh, Cognito token provider (12 tests)
cd bridge && node --test runtime-skills.test.js        # runtime skill manifest + cold-start reinstall (42 tests)
node --test lambda/gateway_tools/*.test.js             # Gateway tool Lambdas: JWT verification, namespace scoping, interceptor (31 tests, Node 24)
cd bridge/skills/s3-user-files && AWS_REGION=$CDK_DEFAULT_REGION node --test common.test.js  # S3 skill tests
cd lambda/router && python -m pytest test_image_upload.py -v        # image upload unit tests
cd lambda/router && python -m pytest test_content_extraction.py -v  # content block extraction tests
cd lambda/router && python -m pytest test_markdown_html.py -v       # markdown-to-HTML conversion tests
cd lambda/router && python -m pytest test_slack.py test_feishu.py -v # Slack + Feishu handler tests
cd lambda/router && python -m pytest test_telegram_chunking.py -v   # Telegram UTF-16 chunking (router)
cd lambda/cron && python -m pytest test_telegram_chunking.py test_feishu_delivery.py -v # cron Telegram chunking + Feishu delivery
python -m pytest tests/test_agentcore_exec.py -v                     # operator CLI tests (mocked boto3)
python -m pytest tests/test_guardrail_eval.py -v                     # guardrail eval script tests (stubbed ApplyGuardrail)
python -m pytest tests/test_state_bucket_lifecycle_synth.py -v       # user-files bucket lifecycle rules (synth, no AWS)
python -m pytest tests/test_gateway_stack_synth.py -v                # OpenClawGateway synth: flag off leaves the 8 templates unchanged, flag on adds the stack (10 tests, no AWS)

# E2E tests (requires deployed stack + E2E_TELEGRAM_CHAT_ID/E2E_TELEGRAM_USER_ID env vars)
pytest tests/e2e/bot_test.py -v -k smoke               # connectivity + webhook auth
pytest tests/e2e/bot_test.py -v -k lifecycle            # full message lifecycle
pytest tests/e2e/bot_test.py -v -k cold_start           # new session creation
pytest tests/e2e/bot_test.py -v -k warmup               # warm-up shim verification
pytest tests/e2e/bot_test.py -v -k full_startup          # full OpenClaw startup + timing (~5min)
pytest tests/e2e/bot_test.py -v -k ScopedCredentials     # S3 file write/read/delete via scoped creds
pytest tests/e2e/bot_test.py -v -k conversation          # multi-turn + rapid-fire
pytest tests/e2e/bot_test.py -v -k SkillManagement       # clawhub skill install/uninstall/list
pytest tests/e2e/bot_test.py -v -k ApiKeyManagement      # API key storage (native + Secrets Manager)
pytest tests/e2e/bot_test.py -v -k CronSchedule          # cron lifecycle + CRON# DynamoDB record check
pytest tests/e2e/bot_test.py -v -k GuardrailSecurity     # guardrail content filtering (requires BEDROCK_GUARDRAIL_ID env var, see below)
pytest tests/e2e/test_guardrail_wiring.py -v             # guardrail WIRING: passes only on a logged guardrail intervention
pytest tests/e2e/test_gateway_tools.py -v -m gateway     # Gateway MCP tools (4 tests; needs enable_gateway=true, skipped when OpenClawGateway is not deployed)
pytest tests/e2e/bot_test.py -v                          # all E2E tests
```

## Security validation

```bash
cdk synth   # Runs cdk-nag AwsSolutions checks — should produce no errors
```
