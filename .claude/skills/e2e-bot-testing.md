---
name: e2e-bot-testing
description: "Run automated E2E tests against the deployed OpenClaw bot — webhook simulation, saved V2 routes, CloudWatch log verification, full OpenClaw completion, ClawHub skill validation, and sub-agent skill verification. Use this skill whenever testing a deployment, verifying cold start behavior, checking if OpenClaw is fully up, measuring startup phase timing, testing sub-agent skills (deep-research-pro, task-decomposer), or diagnosing why messages fail."
user-invocable: true
---

# E2E Bot Testing

Automated end-to-end testing for the deployed OpenClaw bot. Simulates Telegram webhook POSTs to the API Gateway and verifies the full message lifecycle via CloudWatch log tailing. Includes phase timing measurement and full OpenClaw startup verification.

## Prerequisites

Install the [pinned SDK and pytest](../../docs/runtime-v2.md#offline-checks), then run the commands below from the repository root.

```bash
# Required env vars — your real Telegram chat/user IDs
export E2E_TELEGRAM_CHAT_ID=123456789
export E2E_TELEGRAM_USER_ID=123456789

# Region (auto-detected from CDK_DEFAULT_REGION or cdk.json if not set)
export CDK_DEFAULT_REGION=us-west-2
```

## Quick Reference

| Command | Purpose |
|---------|---------|
| `uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k smoke` | Connectivity + webhook validation |
| `uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k lifecycle` | Full message lifecycle |
| `uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k cold_start` | First registration on a new identity |
| `uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k full_startup` | V2 readiness and full OpenClaw completion |
| `uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k subagent` | Sub-agent skill verification |
| `uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k ScopedCredentials` | Scoped S3 credentials (file ops) |
| `uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k TestApiKeyManagement` | API key storage (native + Secrets Manager) |
| `uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k TestSkillManagement` | ClawHub skill install/uninstall lifecycle |
| `uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k TestCronSchedule` | Cron schedule lifecycle + DynamoDB CRON# record verification |
| `uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k conversation` | Multi-turn conversation tests |
| `uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v` | All E2E tests |
| `uv run --no-project --python .venv/bin/python python -m pytest tests/ --ignore=tests/e2e -m "not e2e"` | Skip E2E tests in fast CI |

### CLI (ad-hoc testing)

| Command | Purpose |
|---------|---------|
| `uv run --no-project --python .venv/bin/python python -m tests.e2e.bot_test --stop-session` | Stop the saved test session; preserve its route |
| `uv run --no-project --python .venv/bin/python python -m tests.e2e.bot_test --health` | Check API Gateway is reachable |
| `uv run --no-project --python .venv/bin/python python -m tests.e2e.bot_test --send "Hello" --tail-logs` | Send message + verify lifecycle |
| `uv run --no-project --python .venv/bin/python python -m tests.e2e.bot_test --conversation multi_turn --tail-logs` | Multi-turn test |
| `uv run --no-project --python .venv/bin/python python -m tests.e2e.bot_test --subagent --tail-logs` | Sub-agent skill test |
| `uv run --no-project --python .venv/bin/python python -m tests.e2e.bot_test --scoped-creds --tail-logs` | Scoped credentials test |
| `uv run --no-project --python .venv/bin/python python -m tests.e2e.bot_test --api-keys --tail-logs` | API key management test (native + SM) |
| `uv run --no-project --python .venv/bin/python python -m tests.e2e.bot_test --skill-manage --tail-logs` | Skill management test (list, install, uninstall) |
| `uv run --no-project --python .venv/bin/python python -m tests.e2e.bot_test --cron --tail-logs` | Cron schedule lifecycle test (create, verify DynamoDB, list, delete) |

## V2 startup evidence

Snapshot eligibility and full readiness follow the [V2 lifecycle contract](../../docs/runtime-v2.md#lifecycle-and-compatibility). All chat and cron wait for `USER_READY`.

`get_agent_status(cfg)` invokes the saved SESSION ARN/endpoint/session and checks V2 binding metadata. Readiness requires `lifecycle.state == "USER_READY"`, `openclawReady` and `proxyReady`. `TestFullStartup` additionally runs a single diagnostic chat with shared admission and requires `execution.engine == "openclaw"`, `execution.phase == "completed"`, V2 readiness and exact binding fields. The Router's log preview is truncated and is not sufficient completion metadata.

The diagnostic chat makes a billed model call without channel delivery. It never retries uncertain outcomes; retained admission needs reconciliation. No latency lower bound or claimed cold-start timing is used. `TestColdStart` requires a genuinely new dedicated identity and skips an existing registration without deleting it. ClawHub, API key, Browser, scoped-credential, conversation, Guardrail and Cron functional tests remain available.

### Sub-agent skill verification (TestSubagent)

Two ClawHub skills spawn sub-agents for parallel work: `deep-research-pro` and `task-decomposer`. `TestSubagent` checks response length, V2 readiness and the proxy's sub-agent request counter when available.

```bash
# Run sub-agent tests only
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k subagent

# Ad-hoc CLI testing
uv run --no-project --python .venv/bin/python python -m tests.e2e.bot_test --subagent --tail-logs
```

**What the tests verify:**
1. Bound V2 lifecycle reports USER_READY
2. `task-decomposer` produces a response of at least 100 characters
3. `deep-research-pro` produces a response of at least 200 characters
4. When both status reads expose `subagentRequestCount`, its value increases after the skill invocation

**Evidence limits:**
- A counter increase shows that the proxy received requests using the sub-agent model route. It does not establish response quality or successful completion of every sub-agent.
- If either counter read is unavailable, the test reports that gap and omits the counter assertion; sub-agent execution remains unverified.
- Response-length assertions alone do not prove sub-agent health or which Bedrock model was used.

Container stdout/stderr is available in `/openclaw/container`. `container_logs.py` reads streams for the saved canonical actor; use those logs to investigate execution details.

**Sub-agent configuration path** (for debugging failures):
```
cdk.json: subagent_model_id → agentcore_stack.py: SUBAGENT_BEDROCK_MODEL_ID env →
  agentcore-contract.js: writeOpenClawConfig() →
    openclaw.json: agents.defaults.subagents.model = "agentcore/bedrock-agentcore-subagent"
      → proxy resolveModelId() → Bedrock ConverseStream (SUBAGENT_BEDROCK_MODEL_ID or MODEL_ID)
```

**Subagent verification**: The proxy detects subagent requests by the distinct model name (`bedrock-agentcore-subagent`), increments `subagentRequestCount`, and exposes it via `/health` and the contract `status` endpoint. E2E tests assert the increase only when both counter reads are available.

### API key management verification (TestApiKeyManagement)

Tests native-file and Secrets Manager API key storage through full OpenClaw after V2 USER_READY.

```bash
# Run API key tests only
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k TestApiKeyManagement

# Ad-hoc CLI testing
uv run --no-project --python .venv/bin/python python -m tests.e2e.bot_test --api-keys --tail-logs
```

**Tools tested:**
| Tool | Backend | Purpose |
|------|---------|---------|
| `manage_api_key` | Native file (`.openclaw/user-api-keys.json`) | Set/get/list/delete API keys |
| `manage_secret` | AWS Secrets Manager (`openclaw/user/{ns}/{key}`) | Set/get/list/delete secrets |
| `retrieve_api_key` | Both (SM first, native fallback) | Unified key lookup |
| `migrate_api_key` | Both | Move keys between backends |

**What the tests verify:**
1. `manage_api_key` set → get roundtrip (native file storage works)
2. `manage_secret` set → retrieve (Secrets Manager CRUD works through scoped credentials)
3. `retrieve_api_key` unified lookup (checks SM first, falls back to native)
4. List operations for both backends
5. Cleanup: delete from both backends

The setup waits for V2 readiness and cleans up test keys while retaining the SESSION route.

### Skill management verification (TestSkillManagement)

Tests the clawhub-manage skill lifecycle: list installed skills, install a new skill, verify it appears, uninstall it, and verify it's gone. Runs through full OpenClaw after V2 readiness.

```bash
# Run skill management tests only
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k TestSkillManagement

# Ad-hoc CLI testing
uv run --no-project --python .venv/bin/python python -m tests.e2e.bot_test --skill-manage --tail-logs
```

**What the tests verify:**
1. List pre-installed skills (baseline check — 5 ClawHub skills expected)
2. Install a test skill (`hackernews` — lightweight, no API key)
3. Verify the installed skill appears in the list
4. Uninstall the test skill
5. Verify the skill is removed from the list

The setup retains the saved route and waits for V2 readiness before skill operations.

### Cron schedule verification (TestCronSchedule)

Tests the eventbridge-cron skill lifecycle and **critically verifies DynamoDB CRON# records are created under the correct partition key**. This test catches scoped credential misconfigurations where DynamoDB LeadingKeys exclude the internal userId — the exact bug that caused cron schedules to silently stop executing in March 2026.

```bash
# Run cron tests only
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k TestCronSchedule

# Ad-hoc CLI testing
uv run --no-project --python .venv/bin/python python -m tests.e2e.bot_test --cron --tail-logs
```

**What the tests verify:**
1. Create a far-future one-time schedule (`cron(0 0 1 1 ? 2099)`) — never fires
2. **CRON# DynamoDB record exists under `USER#{internalUserId}`** — the critical regression check
3. EventBridge schedule exists with correct expression and state
4. Bot can list schedules and see the test schedule (reads DynamoDB via scoped creds)
5. Bot can delete the schedule
6. CRON# DynamoDB record is cleaned up after deletion
7. EventBridge schedule is cleaned up after deletion

**Why this test matters:**
The cron executor Lambda verifies schedule ownership by looking up `CRON#{scheduleId}` under `USER#{internalUserId}` (e.g., `USER#user_abc123`). If the scoped credentials only allow `USER#{actorId}` (e.g., `USER#telegram:12345`), the cron skill can create EventBridge schedules but silently fails to write the DynamoDB ownership record. Schedules appear to work but the Lambda rejects every execution with "Schedule not owned by user — skipping execution."

The fixture retains the V2 session route and removes stale test schedules from prior runs (both EventBridge and DynamoDB).

| Troubleshooting | Cause | Fix |
|----------------|-------|-----|
| `test_cron_record_exists` fails | Scoped credentials DynamoDB LeadingKeys missing `USER#{internalUserId}` | Add `internalUserId` to `buildSessionPolicy()` in `scoped-credentials.js` |
| `test_create_schedule` timeout | V2 activation or skill execution incomplete | Inspect lifecycle and eventbridge-cron tool logs |
| `test_list_schedules` shows empty | Scoped creds can't read `USER#{internalUserId}` CRON# records | Same fix as `test_cron_record_exists` |
| `test_eventbridge_schedule_exists` fails | Scheduler IAM permissions missing | Check `scoped-credentials.js` includes `scheduler:CreateSchedule` |

## How It Works

### Architecture

```
CLI / pytest
    |
    v
webhook.py --POST--> API Gateway --> Router Lambda --> AgentCore --> Bedrock
    |                                     |
    v                                     v
session.py --DynamoDB--> Identity Table   CloudWatch Logs
    |                                     |
    v                                     v
log_tailer.py --filter_log_events--> Pattern matching --> TailResult
```

### Verification Flow

1. **Build payload**: Craft realistic Telegram Update JSON with randomized IDs
2. **POST webhook**: Send to `{api_url}/webhook/telegram` with `X-Telegram-Bot-Api-Secret-Token`
3. **Lambda processes**: Router Lambda validates secret, resolves user, invokes AgentCore
4. **AgentCore responds**: Per-user microVM processes message, sends response to Telegram
5. **Verify via logs**: Poll CloudWatch `filter_log_events` for these log markers:

| Log Pattern | Meaning |
|-------------|---------|
| `Telegram: user=X actor=X session=X text_len=N images=N` | Message received |
| `Invoking AgentCore: arn=X qualifier=X session=X` | AgentCore invoked |
| `AgentCore response body (first 2000 chars): X` | Got response (JSON, parse for full text) |
| `Response to send (len=N): X` | Formatted for Telegram (truncated) |
| `Telegram response sent to chat_id=X` | Completion marker |
| `New session created: X for X` | Cold start detected |
| `New user created: X for X` | New user created |

`AgentCore response` logs only the first 200 characters of the JSON body. `Response to send` logs up to 2,000 characters of extracted text; neither is guaranteed to contain the full response. The tailer also recognizes contract-owned Telegram streaming. FullStartup reads complete execution metadata through the saved V2 route.

### Test Classes

| Class | Purpose |
|---|---|
| `TestSmoke` | Connectivity and webhook auth |
| `TestMessageLifecycle` | Full channel lifecycle |
| `TestColdStart` | First registration; skips an existing identity |
| `TestFullStartup` | V2 readiness, OpenClaw completion and binding metadata |
| `TestSubagent` | Subagent skill verification |
| `TestScopedCredentials` | Scoped S3 file operations |
| `TestApiKeyManagement` | Native and Secrets Manager API keys |
| `TestSkillManagement` | ClawHub install/uninstall |
| `TestCronSchedule` / `TestCronExecution` | Schedule lifecycle and delivery |
| `TestBrowserFeature` | Browser tools |
| `TestGuardrailSecurity` | Application security |
| `TestConversation` | Multi-turn scenarios |

These tests retain SESSION records. Only an explicit `--stop-session` diagnostic stops a saved session; it is not part of the runner.

### Conversation Scenarios

Pre-defined scenarios in `tests/e2e/conftest.py`:

- **greeting**: Single friendly message
- **multi_turn**: 3-message conversation testing session continuity
- **task_request**: Ask the bot to perform a creative task
- **rapid_fire**: 2 messages sent ~1s apart testing queue handling

## Module Structure

```
tests/e2e/
  config.py       - AWS config auto-discovery (CF outputs, Secrets Manager, cdk.json)
  webhook.py      - Build + POST Telegram webhook payloads
  session.py      - Saved V2 route/status, full completion evidence, route-preserving stop
  log_tailer.py   - CloudWatch log tailing with pattern matching
  bot_test.py     - CLI entrypoint (argparse) + pytest test classes
  conftest.py     - pytest fixtures, auto-mark e2e, conversation scenarios
```

## Saved V2 sessions

Never delete SESSION or identity records to force a cold start. `_stop_agentcore_session(cfg)` resolves the saved route before stopping its exact ARN/endpoint/session, and retains the records. Stopping a session is not a new registration, migration, state-checkpoint proof or guarantee of the next invocation's startup path. The only stop CLI is `--stop-session`; old reset flags are unsupported.

Cold-start registration testing requires a previously unused dedicated identity. Existing or incomplete routes must be reconciled, not relabeled V2 or filled from a default AgentCore Runtime. Avoid parallel test suites against the same identity.

## Config Auto-Discovery

All configuration is resolved automatically from AWS — no hardcoded values:

| Config | Source |
|--------|--------|
| API URL | CloudFormation `OpenClawRouter` stack output `ApiUrl` |
| Webhook secret | Secrets Manager `openclaw/webhook-secret` |
| AgentCore Runtime ARN | Saved `USER#<userId>` / `SESSION.runtimeArn`; endpoint/session come from the same record |
| Region | `CDK_DEFAULT_REGION` env → `cdk.json` context → boto3 session |
| Log group | `/openclaw/lambda/router` (hardcoded, matches `stacks/router_stack.py`) |
| Identity table | `openclaw-identity` (hardcoded, matches `stacks/router_stack.py`) |
| Telegram IDs | `E2E_TELEGRAM_CHAT_ID` / `E2E_TELEGRAM_USER_ID` env vars |

## Deployment Verification Workflow

The V2 runner defaults to `TestSmoke` and `TestMessageLifecycle`. Repeat `--test-node tests/e2e/path.py::ClassName` to replace those defaults. Even `--skip-deploy` verifies `.runtime/runtime-v2.json` (or `--manifest`) for enrollment readiness. The runner does not delete sessions, stop runtimes or change image versions. Selected feature tests still perform live model/tool operations.

With `E2E_TELEGRAM_CHAT_ID` set, it attempts Telegram start and post-pytest completion notices. Notifications are best effort; an earlier deployment/verification failure exits before the completion notice. Pytest output is saved to `/tmp/e2e-results.txt`, overridable with `E2E_RESULTS_FILE`, and a text-derived summary is printed and included in the completion notice. The exit status comes from pytest; retain the full output when reporting results.

Use the following feature suites for regression checks. Live E2E uses real AWS resources, model/tool calls and channel messages; use a dedicated V2 test identity and its saved route:

```bash
# 1. Smoke — API Gateway is up, webhook auth works
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k smoke

# 2. Lifecycle — message flows end-to-end (uses existing warm session)
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k lifecycle

# 3. First registration — requires a genuinely new test identity
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k cold_start


# 5. Full startup — V2 readiness, execution completion and immutable binding
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k full_startup

# 6. Sub-agent skills — verify deep-research-pro and task-decomposer
#    (Requires full OpenClaw; waits for startup if needed)
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k subagent

# 7. Cron schedule — create, verify CRON# DynamoDB record, list, delete
#    (Critical for scoped credentials validation)
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k TestCronSchedule

# 8. Skill management — install/uninstall ClawHub skills
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k TestSkillManagement

# 9. Conversations — multi-turn and rapid-fire
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v -k conversation

# Or run everything:
uv run --no-project --python .venv/bin/python python -m pytest tests/e2e/bot_test.py -v
```

Record the saved route, image/version and exact run results. Skipped required suites do not establish release readiness.

## Adding New Test Scenarios

1. Add scenario to `SCENARIOS` dict in `conftest.py`:

```python
SCENARIOS = {
    # ...existing...
    "new_scenario": [
        "First message",
        "Follow-up message",
    ],
}
```

2. The scenario is automatically available as:
   - CLI: `uv run --no-project --python .venv/bin/python python -m tests.e2e.bot_test --conversation new_scenario --tail-logs`
   - pytest: via the `conversation_scenario` parametrized fixture

## Adding New Log Patterns

If new log lines are added to `lambda/router/index.py`:

1. Add regex to `log_tailer.py` matching the exact format string
2. Add field to `TailResult` dataclass
3. Add parsing in `_parse_line()` function
4. Update the log pattern table in this skill file

## Timeouts

| Operation | Default | Notes |
|-----------|---------|-------|
| Log tail | 300s | Observation budget; not a startup-latency guarantee |
| Full startup poll | 600s | Waits for full OpenClaw (10 min max) |
| Sub-agent skill timeout | 600s | Sub-agent skills may take several minutes |
| Startup poll interval | 30s | Between read-only bound status checks |
| Webhook POST | 30s | API Gateway timeout |
| Health check | 10s | Simple GET |
| Poll interval | 5s | CloudWatch log polling frequency |
| Rapid-fire delay | 1s | Between rapid messages |
| Normal delay | 5s | Between conversation turns |

## Observability

The application logger sends container output to `/openclaw/container`, with stream prefixes derived from the saved canonical actor. `container_logs.py` and Guardrail wiring tests use that prefix. Router lifecycle logs remain in `/openclaw/lambda/router`.

Native AgentCore Runtime log delivery is separate and is not inferred from application logs. Historical empty native streams do not establish that current application diagnostics are absent. Router text previews are truncated; use bound status and full completion metadata for V2 assertions.

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Config error: Output not found | Stack not deployed | `./scripts/deploy.sh --phase3 --manifest .runtime/runtime-v2.json`; if the manifest is missing, follow the [initial deployment steps](../../docs/runtime-v2.md#deploy-and-verify-v2) |
| 401 Unauthorized | Webhook secret mismatch | Check Secrets Manager `openclaw/webhook-secret` |
| Tail timeout, no logs | Wrong region or log group | Verify `CDK_DEFAULT_REGION` |
| Tail timeout, partial (received + invoked, no sent) | AgentCore cold start exceeds timeout | Increase `--timeout` to 600 |
| Session not found | Missing registration or incomplete ownership | Register only a genuinely new dedicated identity; reconcile existing ownership without deleting records |
| "I received an unexpected response" | Proxy returned error instead of choices | Check proxy error detail in Lambda logs: `AgentCore response body` JSON |
| "Proxy error: X is not defined" | Code error in `agentcore-proxy.js` | Fix the ReferenceError in proxy code, rebuild container |
| USER_READY not reached | Activation failed or is pending | Inspect lifecycle, strict restore, credentials, gateway and Browser logs |
| `openclaw: missing dist/entry.(m)js` | Docker COPY resolved symlinks | Use `ln -s` instead of `COPY --from=builder` for `/usr/local/bin/openclaw` |
| New container code not taking effect | AgentCore caches image digests at deploy time | Deploy through the SDK controller with a new immutable endpoint |
| Cold-start registration test skips | Test identity already registered | Use a genuinely new dedicated identity; retain the existing route |
| Route changed during tests | Concurrent registration or operator change | Stop testing and reconcile the saved route |
| Sub-agent test: short response | Skill didn't invoke sub-agents, or model answered directly | Check `openclaw.json` has correct `subagents` config; verify skill is loaded (`ls /skills/`) |
| Sub-agent test: readiness failure | V2 activation incomplete | Inspect lifecycle and container logs before another turn |
| Sub-agent test: timeout | Sub-agent processing exceeded Lambda timeout | Check `router_lambda_timeout_seconds` (default 300s); sub-agent tasks may need longer |
| API key test: manage_secret fails | Scoped STS credentials missing SM permissions | Check `agentcore_stack.py` has `secretsmanager:*` on `openclaw/user/*`; verify `EXECUTION_ROLE_ARN` env var is set |
| API key test: key value not in response | Model didn't use the tool or redacted the value | Check prompt is explicit about tool name and action; verify tool is in TOOLS array |
| API key test: "scheduled for deletion" instead of "deleted" | Expected — SM uses 7-day recovery window | Test accepts "scheduled" as a valid confirmation word |

### Docker Container Diagnostics

When OpenClaw never fully starts, test the container locally:

```bash
# Verify openclaw binary works (should print version, not crash)
docker run --rm --platform linux/arm64 --entrypoint bash \
  openclaw-bridge:v30 -c "openclaw --version"

# Check clawhub skills are installed
docker run --rm --platform linux/arm64 --entrypoint bash \
  openclaw-bridge:v30 -c "ls /skills/"

# Verify symlinks (must be symlinks, not regular files)
docker run --rm --platform linux/arm64 --entrypoint bash \
  openclaw-bridge:v30 -c "ls -la /usr/local/bin/openclaw /usr/local/bin/clawhub"
```

If `openclaw --version` throws `missing dist/entry.(m)js`, the multi-stage Docker build is copying the binary instead of symlinking it. The binary uses relative `import("./dist/entry.js")` which resolves relative to the binary's location — it only works when the binary is a symlink into the package directory.

### Image Version Deployment

Use the pinned SDK controller, preserve the previous manifest and choose an unused release endpoint. A CDK-only deployment or image push does not update an existing saved route.

```bash
./scripts/deploy.sh --runtime-only --endpoint-name release_2
./scripts/deploy.sh --phase3 --manifest .runtime/runtime-v2.json
```
