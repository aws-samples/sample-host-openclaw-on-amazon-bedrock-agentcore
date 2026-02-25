---
name: e2e-bot-testing
description: "Run automated E2E tests against the deployed OpenClaw bot — webhook simulation, session reset, CloudWatch log verification"
user-invocable: true
---

# E2E Bot Testing

Automated end-to-end testing for the deployed OpenClaw bot. Simulates Telegram webhook POSTs to the API Gateway and verifies the full message lifecycle via CloudWatch log tailing.

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
| `python -m tests.e2e.bot_test --health` | Check API Gateway is reachable |
| `python -m tests.e2e.bot_test --send "Hello" --tail-logs` | Send message + verify lifecycle |
| `python -m tests.e2e.bot_test --reset --send "Hello" --tail-logs` | Cold start test |
| `python -m tests.e2e.bot_test --reset-user` | Full user reset |
| `python -m tests.e2e.bot_test --conversation multi_turn --tail-logs` | Multi-turn conversation test |
| `python -m tests.e2e.bot_test --conversation rapid_fire --tail-logs` | Rapid-fire message test |
| `pytest tests/e2e/bot_test.py -v -k smoke` | Pytest: smoke test |
| `pytest tests/e2e/bot_test.py -v -k cold_start` | Pytest: cold start test |
| `pytest tests/e2e/bot_test.py -v` | Pytest: all E2E tests |
| `pytest -m "not e2e"` | Skip E2E tests in fast CI |

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
| `AgentCore response body (first 500 chars): X` | Got response |
| `Response to send (len=N): X` | Formatted for Telegram |
| `Telegram response sent to chat_id=X` | Completion marker |
| `New session created: X for X` | Cold start detected |
| `New user created: X for X` | New user created |

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
  session.py      - DynamoDB session/user reset
  log_tailer.py   - CloudWatch log tailing with pattern matching
  bot_test.py     - CLI entrypoint (argparse) + pytest test classes
  conftest.py     - pytest fixtures, auto-mark e2e, conversation scenarios
```

## Config Auto-Discovery

All configuration is resolved automatically from AWS — no hardcoded values:

| Config | Source |
|--------|--------|
| API URL | CloudFormation `OpenClawRouter` stack output `ApiUrl` |
| Webhook secret | Secrets Manager `openclaw/webhook-secret` |
| Region | `CDK_DEFAULT_REGION` env -> `cdk.json` context -> boto3 session |
| Log group | `/openclaw/lambda/router` (hardcoded, matches `stacks/router_stack.py`) |
| Identity table | `openclaw-identity` (hardcoded, matches `stacks/router_stack.py`) |
| Telegram IDs | `E2E_TELEGRAM_CHAT_ID` / `E2E_TELEGRAM_USER_ID` env vars |

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
| Webhook POST | 30s | API Gateway timeout |
| Health check | 10s | Simple GET |
| Poll interval | 5s | CloudWatch polling frequency |
| Rapid-fire delay | 1s | Between rapid messages |
| Normal delay | 5s | Between conversation turns |

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Config error: Output not found | Stack not deployed | `cdk deploy OpenClawRouter` |
| 401 Unauthorized | Webhook secret mismatch | Check Secrets Manager `openclaw/webhook-secret` |
| Tail timeout, no logs | Wrong region or log group | Verify `CDK_DEFAULT_REGION` |
| Tail timeout, partial logs | AgentCore cold start | Increase `--timeout` to 600 |
| Session not found | First-time user | Use `--send` first to create user, then `--reset` |
