---
name: e2e-bot-testing
description: "Run automated E2E tests against the deployed OpenClaw bot — webhook simulation, session reset, CloudWatch log verification, full OpenClaw startup timing, ClawHub skill validation, and sub-agent skill verification. Use this skill whenever testing a deployment, verifying cold start behavior, checking if OpenClaw is fully up, measuring startup phase timing, testing sub-agent skills (deep-research-pro, task-decomposer), or diagnosing why messages fail."
user-invocable: true
---

# E2E Bot Testing

Automated end-to-end testing for the deployed OpenClaw bot. Simulates Telegram webhook POSTs to the API Gateway and verifies the full message lifecycle via CloudWatch log tailing. Includes phase timing measurement and full OpenClaw startup verification.

## Prerequisites

```bash
# Required env vars — your real Telegram chat/user IDs
export E2E_TELEGRAM_CHAT_ID=123456789
export E2E_TELEGRAM_USER_ID=123456789

# Region (auto-detected from CDK_DEFAULT_REGION or cdk.json if not set)
export CDK_DEFAULT_REGION=ap-southeast-2
```

## Quick Reference

| Command | Purpose |
|---------|---------|
| `pytest tests/e2e/bot_test.py -v -k smoke` | Connectivity + webhook validation |
| `pytest tests/e2e/bot_test.py -v -k lifecycle` | Full message lifecycle |
| `pytest tests/e2e/bot_test.py -v -k cold_start` | Cold start (new session creation) |
| `pytest tests/e2e/bot_test.py -v -k warmup` | Verify warm-up shim footer present |
| `pytest tests/e2e/bot_test.py -v -k full_startup` | Wait for full OpenClaw + timing |
| `pytest tests/e2e/bot_test.py -v -k subagent` | Sub-agent skill verification |
| `pytest tests/e2e/bot_test.py -v -k ScopedCredentials` | Scoped S3 credentials (file ops) |
| `pytest tests/e2e/bot_test.py -v -k conversation` | Multi-turn conversation tests |
| `pytest tests/e2e/bot_test.py -v` | All E2E tests |
| `pytest -m "not e2e"` | Skip E2E tests in fast CI |

### CLI (ad-hoc testing)

| Command | Purpose |
|---------|---------|
| `python -m tests.e2e.bot_test --health` | Check API Gateway is reachable |
| `python -m tests.e2e.bot_test --send "Hello" --tail-logs` | Send message + verify lifecycle |
| `python -m tests.e2e.bot_test --reset --send "Hello" --tail-logs` | Cold start test |
| `python -m tests.e2e.bot_test --reset-user` | Full user reset |
| `python -m tests.e2e.bot_test --conversation multi_turn --tail-logs` | Multi-turn test |
| `python -m tests.e2e.bot_test --subagent --tail-logs` | Sub-agent skill test |
| `python -m tests.e2e.bot_test --scoped-creds --tail-logs` | Scoped credentials test |

## Startup Phases and Timing

The bot has a two-phase startup. Understanding these phases is critical for interpreting test results:

```
Cold Start Timeline
───────────────────────────────────────────────────────────────────
t=0s       Container created (new microVM)
t=~5s      Proxy ready → lightweight agent shim handles messages
           Responses include: "_Warm-up mode — after full startup..._"
t=~2-4min  OpenClaw gateway ready → full runtime handles messages
           Responses have NO warm-up footer
           ClawHub skills available (transcript, deep-research, etc.)
```

| Phase | What Responds | Indicator | Typical Time |
|-------|---------------|-----------|-------------|
| **Warm-up** | Lightweight agent shim (proxy → Bedrock) | `"warm-up mode"` in response | 5-15s from cold start |
| **Full** | OpenClaw gateway (WebSocket bridge) | No warm-up footer | 2-4 min from cold start |

### How to detect which phase is active

The `TailResult.is_warmup` property checks for `"warm-up mode"` (case-insensitive) in the response text. This footer is deterministically appended by `bridge/lightweight-agent.js` and is never present when the full OpenClaw runtime handles the message.

```python
tail = tail_logs(cfg, since_ms=since_ms)
if tail.is_warmup:
    print("Still in warm-up shim mode")
else:
    print("Full OpenClaw is running!")
```

### ClawHub skills (post-warmup only)

These skills are only available after OpenClaw fully starts. Requesting one during warm-up will get a response from the shim (without the skill output):

| Skill | Test Prompt |
|-------|------------|
| `transcript` | `"Get the transcript of https://www.youtube.com/watch?v=dQw4w9WgXcQ"` |
| `deep-research-pro` | `"Research the latest advances in quantum computing"` |
| `jina-reader` | `"Read and summarize https://example.com"` |
| `task-decomposer` | `"Break down the task of building a REST API"` |

The `TestFullStartup` test waits for full startup and verifies responses no longer contain the warm-up footer, confirming ClawHub skills are available.

### Sub-agent skill verification (TestSubagent)

Two ClawHub skills spawn sub-agents for parallel work: `deep-research-pro` and `task-decomposer`. The `TestSubagent` class verifies these skills produce substantial responses after full OpenClaw startup.

```bash
# Run sub-agent tests only
pytest tests/e2e/bot_test.py -v -k subagent

# Ad-hoc CLI testing
python -m tests.e2e.bot_test --subagent --tail-logs
```

**What the tests verify:**
1. OpenClaw is fully started (not in warm-up mode)
2. `task-decomposer` produces structured output (>100 chars) for a decomposition request
3. `deep-research-pro` produces detailed output (>200 chars) for a research request
4. Responses come from full OpenClaw, not the lightweight warm-up shim

**What the tests cannot verify** (observability gap):
- Whether sub-agents were actually spawned (container stdout not in CloudWatch)
- Whether the proxy received multiple requests (one per sub-agent)
- Which model the sub-agents used

The tests are behavioral smoke tests — they confirm the skills produce substantial output after full startup. If sub-agent execution is fundamentally broken (sandbox misconfigured, model routing fails, skill loading fails), the response would be an error or a short deflection, failing the length assertion.

**Sub-agent configuration path** (for debugging failures):
```
cdk.json: subagent_model_id → agentcore_stack.py: SUBAGENT_BEDROCK_MODEL_ID env →
  agentcore-contract.js: writeOpenClawConfig() →
    openclaw.json: agents.defaults.subagents.model = "agentcore/bedrock-agentcore-subagent"
      → proxy resolveModelId() → Bedrock ConverseStream (SUBAGENT_BEDROCK_MODEL_ID or MODEL_ID)
```

**Subagent verification**: The proxy detects subagent requests by the distinct model name (`bedrock-agentcore-subagent`), increments `subagentRequestCount`, and exposes it via `/health` and the contract `status` endpoint. E2E tests assert this count increases after skill invocations.

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

The `AgentCore response body` line contains JSON: `{"response": "full text..."}`. The log tailer parses this JSON to extract the complete response text (up to 2000 chars), which is more reliable than the truncated `Response to send` line.

### Test Classes

| Class | Purpose | Resets Session | Stops Container |
|-------|---------|:-:|:-:|
| `TestSmoke` | Connectivity, webhook auth | | |
| `TestMessageLifecycle` | Full lifecycle via log tailing | | |
| `TestColdStart` | New session creation after reset | ✓ | |
| `TestWarmupShim` | Warm-up footer present on cold start | ✓ | ✓ |
| `TestFullStartup` | Full OpenClaw ready + phase timing | ✓ | ✓ |
| `TestSubagent` | Sub-agent skill verification | | |
| `TestScopedCredentials` | S3 file ops via scoped STS creds | | |
| `TestConversation` | Multi-turn scenarios | | |

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
  session.py      - DynamoDB session/user reset + AgentCore session stop
  log_tailer.py   - CloudWatch log tailing with pattern matching
  bot_test.py     - CLI entrypoint (argparse) + pytest test classes
  conftest.py     - pytest fixtures, auto-mark e2e, conversation scenarios
```

## Session Reset for True Cold Start

A "true cold start" requires **two** cleanup steps:

1. **Delete DDB session record** (`reset_session`) — forces the Router Lambda to create a new session ID on next message
2. **Stop AgentCore session** (`_stop_agentcore_session`) — terminates the running container so a new one is pulled

If you only delete the DDB record, the old container may still be running (warm, with OpenClaw already started). The next message gets a new session ID but reuses the warm container — not a true cold start.

```python
from tests.e2e.session import reset_session, _stop_agentcore_session, get_user_id

# True cold start reset
user_id = get_user_id(cfg)
if user_id:
    reset_session(cfg)            # Delete SESSION record from DDB
    _stop_agentcore_session(cfg)  # Terminate the running container
```

## Config Auto-Discovery

All configuration is resolved automatically from AWS — no hardcoded values:

| Config | Source |
|--------|--------|
| API URL | CloudFormation `OpenClawRouter` stack output `ApiUrl` |
| Webhook secret | Secrets Manager `openclaw/webhook-secret` |
| Runtime ARN | CloudFormation `OpenClawAgentCore` stack output `RuntimeArn` |
| Region | `CDK_DEFAULT_REGION` env → `cdk.json` context → boto3 session |
| Log group | `/openclaw/lambda/router` (hardcoded, matches `stacks/router_stack.py`) |
| Identity table | `openclaw-identity` (hardcoded, matches `stacks/router_stack.py`) |
| Telegram IDs | `E2E_TELEGRAM_CHAT_ID` / `E2E_TELEGRAM_USER_ID` env vars |

## Deployment Verification Workflow

After deploying a new container version, run tests in this order:

```bash
# 1. Smoke — API Gateway is up, webhook auth works
pytest tests/e2e/bot_test.py -v -k smoke

# 2. Lifecycle — message flows end-to-end (uses existing warm session)
pytest tests/e2e/bot_test.py -v -k lifecycle

# 3. Cold start — new session creation works
pytest tests/e2e/bot_test.py -v -k cold_start

# 4. Warm-up shim — lightweight agent responds during cold start
pytest tests/e2e/bot_test.py -v -k warmup

# 5. Full startup — OpenClaw fully starts, phase timing measured
#    (This takes 3-5+ min — it polls until warm-up footer disappears)
pytest tests/e2e/bot_test.py -v -k full_startup

# 6. Sub-agent skills — verify deep-research-pro and task-decomposer
#    (Requires full OpenClaw; waits for startup if needed)
pytest tests/e2e/bot_test.py -v -k subagent

# 7. Conversations — multi-turn and rapid-fire
pytest tests/e2e/bot_test.py -v -k conversation

# Or run everything:
pytest tests/e2e/bot_test.py -v
```

Use subagents for parallel monitoring: launch a background agent to tail CloudWatch logs and check for OpenClaw full startup timing while running the pytest suite in the foreground.

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
   - CLI: `python -m tests.e2e.bot_test --conversation new_scenario --tail-logs`
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
| Log tail | 300s | Accommodates cold start (~2-4 min) |
| Full startup poll | 600s | Waits for full OpenClaw (10 min max) |
| Sub-agent skill timeout | 600s | Sub-agent skills may take several minutes |
| Startup poll interval | 30s | Between status-check messages |
| Webhook POST | 30s | API Gateway timeout |
| Health check | 10s | Simple GET |
| Poll interval | 5s | CloudWatch log polling frequency |
| Rapid-fire delay | 1s | Between rapid messages |
| Normal delay | 5s | Between conversation turns |

## Observability Gaps

Container stdout/stderr (Node.js `console.log` from `agentcore-contract.js`) is **not captured in CloudWatch**. The available log groups show only:

| Log Group | Contents |
|-----------|----------|
| `/openclaw/lambda/router` | Lambda execution logs (message lifecycle) |
| `/aws/vendedlogs/bedrock-agentcore/runtime/APPLICATION_LOGS/...` | OTEL traces (InvokeAgentRuntime spans) |
| `/aws/bedrock-agentcore/runtimes/...-DEFAULT` | Empty `otel-rt-logs` stream |
| `/aws/bedrock-agentcore/runtimes/...-openclaw_agent_live` | Empty `otel-rt-logs` stream |

This means diagnostic messages like `[contract] OpenClaw ready`, `[contract] OpenClaw failed to start`, and `[openclaw:out/err]` are invisible. To diagnose container-level issues, test locally with `docker run`.

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Config error: Output not found | Stack not deployed | `cdk deploy OpenClawRouter` |
| 401 Unauthorized | Webhook secret mismatch | Check Secrets Manager `openclaw/webhook-secret` |
| Tail timeout, no logs | Wrong region or log group | Verify `CDK_DEFAULT_REGION` |
| Tail timeout, partial (received + invoked, no sent) | AgentCore cold start exceeds timeout | Increase `--timeout` to 600 |
| Session not found | First-time user | Use `--send` first to create user, then `--reset` |
| "I received an unexpected response" | Proxy returned error instead of choices | Check proxy error detail in Lambda logs: `AgentCore response body` JSON |
| "Proxy error: X is not defined" | Code error in `agentcore-proxy.js` | Fix the ReferenceError in proxy code, rebuild container |
| Warm-up footer never disappears | OpenClaw binary broken in container | Run `docker run --rm --platform linux/arm64 --entrypoint bash IMAGE -c "openclaw --version"` to check |
| `openclaw: missing dist/entry.(m)js` | Docker COPY resolved symlinks | Use `ln -s` instead of `COPY --from=builder` for `/usr/local/bin/openclaw` |
| New container code not taking effect | AgentCore caches image digests at deploy time | Bump `image_version` in cdk.json AND run `cdk deploy` |
| Cold start test gets warm response | Old container still running | Stop AgentCore session AND delete DDB session record |
| Multiple sessions created during tests | Each test run may create a new session | Expected — each session independently cold-starts |
| Sub-agent test: short response | Skill didn't invoke sub-agents, or model answered directly | Check `openclaw.json` has correct `subagents` config; verify skill is loaded (`ls /skills/`) |
| Sub-agent test: warm-up response | OpenClaw not fully started | Test waits 10 min max; check container logs for startup errors |
| Sub-agent test: timeout | Sub-agent processing exceeded Lambda timeout | Check `router_lambda_timeout_seconds` (default 300s); sub-agent tasks may need longer |

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

AgentCore resolves image tags to digests at CDK deploy time. Pushing new layers to an existing tag without bumping the version **will not take effect**:

```bash
# Correct workflow:
# 1. Edit cdk.json: "image_version": N+1
# 2. Update BUILD_VERSION in bridge/agentcore-contract.js
# 3. Build + push with new tag
docker build --no-cache --platform linux/arm64 -t openclaw-bridge:vN+1 bridge/
docker tag openclaw-bridge:vN+1 ACCOUNT.dkr.ecr.REGION.amazonaws.com/openclaw-bridge:vN+1
docker push ACCOUNT.dkr.ecr.REGION.amazonaws.com/openclaw-bridge:vN+1
# 4. CDK deploy (resolves new digest)
cdk deploy OpenClawAgentCore --require-approval never
```
