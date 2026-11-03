"""Router Lambda (release/v1): Telegram reply text stays out of the logs by default.

The Router logged up to 2,000 characters of every Telegram reply at INFO, so
whatever the agent said, including a stored API key it was asked to show or a
presigned S3 URL, was kept in /openclaw/lambda/router. Backport of #214 and
#216: every turn logs the reply length only, and the text is logged only when
the test-only router_log_reply_text key is set, for the E2E log tailer. All IDs
and values are placeholders.

    pytest tests/test_router_reply_logging.py -v
"""

import ast
import importlib.util
import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.e2e.log_tailer import TailResult, _parse_line
from tests.test_table_protection_synth import _synth_app

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "lambda" / "router" / "index.py"
for _path in (ROOT / "lambda" / "shared" / "python", ROUTER.parent):
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# Same value as SM_KEY_VALUE in tests/e2e/bot_test.py.
KEY_VALUE = "secure-test-value-67890"
PRESIGNED_URL = (
    "https://openclaw-user-files-123456789012-us-west-2.s3.us-west-2.amazonaws.com/telegram_1/report.pdf"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=TESTCREDENTIAL%2F20260101%2Fus-west-2%2Fs3%2Faws4_request"
    "&X-Amz-Expires=3600&X-Amz-Signature=0123456789abcdef"
)
REPLY = f"Your key is {KEY_VALUE}. Download the report: {PRESIGNED_URL}"
REQUEST = "2026-01-01T00:00:00Z\t00000000-0000-0000-0000-000000000000\t"


def _router_env(tmp_path, **context):
    templates = _synth_app(tmp_path, **context)
    (fn,) = [res for template in templates.values() for res in template.get("Resources", {}).values()
             if res["Type"] == "AWS::Lambda::Function"
             and res["Properties"].get("FunctionName") == "openclaw-router"]
    return fn["Properties"]["Environment"]["Variables"]


def test_router_does_not_log_reply_text_by_default(tmp_path):
    assert "LOG_REPLY_TEXT" not in _router_env(tmp_path)


@pytest.mark.parametrize("value", [True, "true"], ids=["cdk-json", "cli-string"])
def test_router_logs_reply_text_only_when_opted_in(tmp_path, value):
    assert _router_env(tmp_path, router_log_reply_text=value)["LOG_REPLY_TEXT"] == "true"


def test_reply_text_log_line_is_behind_the_flag():
    """The only log call that formats the reply text sits under `if LOG_REPLY_TEXT:`."""
    tree = ast.parse(ROUTER.read_text())
    guarded, unguarded = [], []

    def visit(node, under_flag):
        for child in ast.iter_child_nodes(node):
            flag = under_flag or (isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                                  and node.test.id == "LOG_REPLY_TEXT" and child in node.body)
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and isinstance(child.func.value, ast.Name) and child.func.value.id == "logger"
                    and child.args and isinstance(child.args[0], ast.Constant)
                    and "Response to send" in str(child.args[0].value)):
                (guarded if flag else unguarded).append(child.lineno)
            visit(child, flag)

    visit(tree, False)
    assert guarded and not unguarded, (guarded, unguarded)


def test_log_tailer_reads_length_without_text_and_text_when_logged():
    result = TailResult()
    _parse_line(f"[INFO]\t{REQUEST}Telegram response ready (len=12, streamed=False)", result)
    assert result.response_len == 12 and result.response_text == ""
    _parse_line(f"[INFO]\t{REQUEST}Response to send (len=12): hello there!", result)
    assert result.response_len == 12 and result.response_text == "hello there!"


def _load_router(log_reply_text=None):
    spec = importlib.util.spec_from_file_location("router_reply_logging_under_test", ROUTER)
    module = importlib.util.module_from_spec(spec)
    env = {
        "AGENTCORE_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test",
        "AGENTCORE_QUALIFIER": "DEFAULT",
        "IDENTITY_TABLE_NAME": "openclaw-identity",
        "AWS_REGION": "us-west-2",
    }
    with patch.dict(os.environ, env), patch("boto3.client", MagicMock()), \
            patch("boto3.resource", MagicMock()):
        os.environ.pop("LOG_REPLY_TEXT", None)
        if log_reply_text is not None:
            os.environ["LOG_REPLY_TEXT"] = log_reply_text
        spec.loader.exec_module(module)
    return module


def _telegram_turn(module, caplog):
    """One private-chat turn whose agent reply carries a key value and a presigned URL."""
    sent = []
    with patch.object(module, "_get_telegram_token", return_value="900000001:test-token"), \
            patch.object(module, "resolve_user", return_value=("user_test", False)), \
            patch.object(module, "get_or_create_session", return_value="ses_test"), \
            patch.object(module, "send_telegram_typing"), \
            patch.object(module, "_periodic_typing"), \
            patch.object(module, "send_telegram_message"), \
            patch.object(module, "invoke_agent_runtime", return_value={"response": REPLY}), \
            patch.object(module, "_send_telegram_chunks",
                         side_effect=lambda chat_id, text, token, **kwargs: sent.append(text)), \
            caplog.at_level(logging.DEBUG):
        module.handle_telegram({"message": {
            "message_id": 1, "text": "Show me my key and the report link",
            "chat": {"id": 111111111, "type": "private"},
            "from": {"id": 111111111, "first_name": "Alex"},
        }})
    assert sent == [REPLY], "the user still receives the whole reply"
    return [record.getMessage() for record in caplog.records]


def _tail(messages):
    """What tests/e2e/log_tailer.py reads from these lines in CloudWatch Logs."""
    result = TailResult()
    for message in messages:
        _parse_line(f"[INFO]\t{REQUEST}{message}", result)
    return result


def test_telegram_reply_text_stays_out_of_the_logs_by_default(caplog):
    messages = _telegram_turn(_load_router(), caplog)
    logged = "\n".join(messages)
    assert KEY_VALUE not in logged
    assert "X-Amz-Signature" not in logged and "X-Amz-Credential" not in logged
    tail = _tail(messages)
    assert tail.response_len == len(REPLY) and tail.response_text == "" and tail.telegram_sent


def test_opted_in_reply_text_still_reaches_the_e2e_log_tailer(caplog):
    # bot_test.py looks for the stored key value in tail.response_text this way.
    tail = _tail(_telegram_turn(_load_router(log_reply_text="true"), caplog))
    assert tail.response_text == REPLY and KEY_VALUE in tail.response_text
    assert tail.response_len == len(REPLY) and tail.telegram_sent
