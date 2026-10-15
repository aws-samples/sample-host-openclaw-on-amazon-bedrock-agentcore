"""V1 cron Lambda: never send raw invocation errors; a failed run gets a fixed notice.

Backport of #147 (cron side). A botocore read timeout names the AgentCore
endpoint URL, which embeds the runtime ARN and the AWS account ID, and the V1
Lambda forwarded f"Agent invocation failed: {e}" to Telegram. Users now get one
fixed notice; logs get a fixed message plus the exception type only. A run that
fails or returns nothing also gets the notice instead of silence or the
runtime's own error text. Async retry is off, so a failed brief is not re-run.
"""

import json
import logging
from unittest.mock import patch

import pytest

from tests.cron_v1_helpers import ACTOR, READY, USER, FakeRuntime, cron_event, load_cron_lambda, run_app

# Placeholder account and ARN in the shape of a real botocore read timeout.
LEAKY_TEXT = (
    'Read timeout on endpoint URL: "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/'
    'arn%3Aaws%3Abedrock-agentcore%3Aus-west-2%3A123456789012%3Aruntime%2Ftest/invocations"'
)
SECRET_MARKERS = ("123456789012", "arn%3Aaws", "arn:aws", "amazonaws.com", "Read timeout")
NOTICE = (
    "[Scheduled: Morning brief] Your scheduled task did not return a result this time. "
    "It will run again at the next scheduled time."
)


def _assert_clean(text):
    for marker in SECRET_MARKERS:
        assert marker not in text, f"{marker!r} leaked into {text!r}"


def test_invoke_failure_returns_fixed_text_and_logs_only_the_type(caplog):
    module = load_cron_lambda()
    module.agentcore_client = FakeRuntime([TimeoutError(LEAKY_TEXT)])
    with caplog.at_level(logging.INFO):
        result = module.invoke_agentcore("ses_" + "s" * 40, "cron", USER, ACTOR, "telegram", "hi")
    assert result == {"response": "Agent invocation failed.", "status": "failed"}
    assert "TimeoutError" in caplog.text
    for record in caplog.records:
        _assert_clean(record.getMessage())
        assert record.exc_info is None or record.exc_info[1] is None


@pytest.mark.parametrize("failure", [
    TimeoutError(LEAKY_TEXT),                                   # invocation raised
    {"status": "error", "response": LEAKY_TEXT},                # contract error reply
    {"status": "error", "response": "Agent not ready after initialization."},
    {"error": "Missing userId, actorId, or message"},           # contract 400 body
    {"response": ""},                                           # empty run
    {"response": "  \n "},                                      # whitespace only
    {"userId": USER},                                           # no response field
    b"",                                                        # empty body
], ids=["exception", "status-error-leaky", "status-error-shim", "error-field",
        "empty", "whitespace", "no-response", "empty-body"])
def test_failed_or_empty_run_sends_one_fixed_notice(failure, caplog):
    module = load_cron_lambda()
    module.agentcore_client = FakeRuntime([READY, failure])
    with caplog.at_level(logging.INFO), patch.object(module, "deliver_response") as deliver:
        assert module.handler(cron_event(), None)["statusCode"] == 503
    deliver.assert_called_once_with("telegram", "123", NOTICE)
    for record in caplog.records:
        _assert_clean(record.getMessage())
    # Exactly one cron turn: a failure is reported, never retried in the Lambda.
    assert module.agentcore_client.actions == ["warmup", "cron"]


def test_successful_run_is_delivered_unchanged():
    module = load_cron_lambda()
    module.agentcore_client = FakeRuntime([READY, {"response": "Here is your brief."}])
    with patch.object(module, "deliver_response") as deliver:
        assert module.handler(cron_event(), None)["statusCode"] == 200
    deliver.assert_called_once_with("telegram", "123", "Here is your brief.")


def test_cron_payload_carries_only_v1_fields_and_the_caller_budget():
    """The V1 contract reads callerBudgetMs (see test_cron_caller_budget.py); no other V2 fields."""
    module = load_cron_lambda()
    module.agentcore_client = FakeRuntime([READY, {"response": "ok"}])
    with patch.object(module, "deliver_response"):
        module.handler(cron_event(), None)
    cron_payload = json.loads(module.agentcore_client.calls[1]["payload"])
    assert set(cron_payload) == {"action", "userId", "actorId", "channel", "message", "callerBudgetMs"}


def test_cron_lambda_never_async_retries_a_brief(tmp_path):
    resources = run_app(tmp_path)["OpenClawCron.template.json"]["Resources"]
    function_id = next(
        key for key, resource in resources.items()
        if resource["Type"] == "AWS::Lambda::Function"
        and resource["Properties"].get("FunctionName") == "openclaw-cron-executor"
    )
    configs = [r["Properties"] for r in resources.values() if r["Type"] == "AWS::Lambda::EventInvokeConfig"]
    assert len(configs) == 1, "a timed-out brief must not be re-run by Lambda's async retry"
    assert configs[0]["MaximumRetryAttempts"] == 0
    assert configs[0]["FunctionName"] == {"Ref": function_id}
    function = resources[function_id]["Properties"]
    assert function["Timeout"] == int(function["Environment"]["Variables"]["LAMBDA_TIMEOUT_SECONDS"])
