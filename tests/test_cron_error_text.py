"""The cron Lambda must never send raw exception text to a user's channel.

A botocore read timeout names the AgentCore endpoint URL, which embeds the
runtime ARN and the AWS account ID. Users get a fixed message; logs get a
fixed message plus the exception type only.
"""

import json
import logging
from unittest.mock import patch

import pytest

from tests.test_runtime_routing import (
    FakeRuntime, FakeTable, USER, ACTOR, candidate, cron_event, cron_table, load_lambda,
)

# Placeholder account and ARN in the shape of a real botocore read timeout.
LEAKY_TEXT = (
    'Read timeout on endpoint URL: "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/'
    'arn%3Aaws%3Abedrock-agentcore%3Aus-west-2%3A123456789012%3Aruntime%2Fcandidate-123/invocations"'
)
SECRET_MARKERS = ("123456789012", "arn%3Aaws", "arn:aws", "amazonaws.com", "Read timeout")


def _assert_clean(text):
    for marker in SECRET_MARKERS:
        assert marker not in text, f"{marker!r} leaked into {text!r}"


def test_invoke_failure_response_is_fixed_text_without_exception_detail(caplog):
    module = load_lambda("cron")
    module.identity_table = FakeTable(candidate())
    module.agentcore_client = FakeRuntime([RuntimeError(LEAKY_TEXT)])
    with caplog.at_level(logging.INFO):
        result = module.invoke_agentcore(module.get_or_create_session(USER), "cron", USER, ACTOR, "telegram", "hi")
    assert result["status"] == "failed"
    _assert_clean(result["response"])
    for record in caplog.records:
        _assert_clean(record.getMessage())
        assert record.exc_info is None or record.exc_info[1] is None


@pytest.mark.parametrize("failure", [
    RuntimeError(LEAKY_TEXT),
    {"status": "error", "response": LEAKY_TEXT},
    {"status": "unknown_outcome", "response": LEAKY_TEXT},
])
def test_cron_failure_tells_the_user_with_a_fixed_message(failure, caplog):
    module = load_lambda("cron")
    module.identity_table = cron_table()
    module.agentcore_client = FakeRuntime([{"status": "ready", "phase": "USER_READY"}, failure])
    event = {**cron_event(), "scheduleName": "Morning brief"}
    with caplog.at_level(logging.INFO), patch.object(module, "deliver_response") as deliver:
        assert module.handler(event, None)["statusCode"] == 503
    deliver.assert_called_once()
    channel, target, text = deliver.call_args.args
    assert (channel, target) == ("telegram", "123")
    assert "Morning brief" in text
    _assert_clean(text)
    for record in caplog.records:
        _assert_clean(record.getMessage())
    # Exactly one cron turn: a failure is reported, never retried.
    assert [json.loads(call["payload"])["action"] for call in module.agentcore_client.calls] == ["warmup", "cron"]
