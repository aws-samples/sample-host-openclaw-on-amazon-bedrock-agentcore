"""Router Lambda: secret and token fetch failures log a fixed message plus the exception type.

CodeQL py/clear-text-logging-sensitive-data flagged
``logger.warning("Failed to fetch secret %s: %s", secret_id, e)`` in
lambda/router/index.py. The secret id names the channel credential and the raw
error text can carry ARNs, account IDs or request details. The cron Lambda
already logs ``type(e).__name__`` only; the router now does the same. All IDs
here are placeholders.
"""

import ast
import importlib.util
import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "lambda" / "router" / "index.py"
SHARED = ROOT / "lambda" / "shared" / "python"
if SHARED.is_dir() and str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

SECRET_ID = "openclaw/channels/telegram-test-secret-id"
LEAKY_ERROR = (
    "AccessDeniedException: User arn:aws:sts::123456789012:assumed-role/test is not "
    "authorized to perform secretsmanager:GetSecretValue on resource " + SECRET_ID
)
MARKERS = (SECRET_ID, "123456789012", "arn:aws", "AccessDeniedException: User", "not authorized")


def _load_router():
    spec = importlib.util.spec_from_file_location("router_secret_logging_under_test", ROUTER)
    module = importlib.util.module_from_spec(spec)
    env = {
        "AGENTCORE_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test",
        "AGENTCORE_QUALIFIER": "DEFAULT",
        "IDENTITY_TABLE_NAME": "openclaw-identity",
        "AWS_REGION": "us-west-2",
    }
    with patch.dict(os.environ, env), patch("boto3.client", MagicMock()), \
            patch("boto3.resource", MagicMock()):
        spec.loader.exec_module(module)
    return module


def _assert_clean(caplog):
    assert caplog.records, "expected a log line"
    for record in caplog.records:
        text = record.getMessage()
        for marker in MARKERS:
            assert marker not in text, f"{marker!r} leaked into {text!r}"
        assert record.exc_info is None or record.exc_info[1] is None


class LeakyError(Exception):
    pass


def test_get_secret_failure_logs_neither_secret_id_nor_error_text(caplog):
    module = _load_router()
    module._token_cache.clear()
    module.secrets_client = MagicMock()
    module.secrets_client.get_secret_value.side_effect = LeakyError(LEAKY_ERROR)
    with caplog.at_level(logging.DEBUG):
        assert module._get_secret(SECRET_ID) == ""
    _assert_clean(caplog)
    assert "LeakyError" in caplog.text


def test_feishu_tenant_token_failure_logs_only_the_type(caplog):
    module = _load_router()
    module._feishu_token_cache["token"] = ""
    module._feishu_token_cache["expires_at"] = 0
    with patch.object(module, "_get_feishu_credentials", return_value=("cli_test", "app-secret-test", "", "")), \
            patch.object(module.urllib_request, "urlopen", side_effect=LeakyError(LEAKY_ERROR + " app-secret-test")):
        with caplog.at_level(logging.DEBUG):
            assert module._get_feishu_tenant_token() == ""
    _assert_clean(caplog)
    assert "app-secret-test" not in caplog.text
    assert "LeakyError" in caplog.text


def _logger_calls(node):
    for child in ast.walk(node):
        if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name) and child.func.value.id == "logger"):
            yield child


def _is_type_name(arg, exc_name):
    # type(e).__name__
    return (isinstance(arg, ast.Attribute) and arg.attr == "__name__"
            and isinstance(arg.value, ast.Call) and isinstance(arg.value.func, ast.Name)
            and arg.value.func.id == "type" and len(arg.value.args) == 1
            and isinstance(arg.value.args[0], ast.Name) and arg.value.args[0].id == exc_name)


@pytest.mark.parametrize("path", [
    ROUTER,
    ROOT / "lambda" / "token_metrics" / "index.py",
], ids=["router", "token_metrics"])
def test_no_secret_fetch_handler_logs_secret_id_or_raw_exception(path):
    """Every copy of a secret/token fetch function (router defines _get_secret twice)."""
    tree = ast.parse(path.read_text())
    targets = {"_get_secret", "_get_feishu_tenant_token", "_get_feishu_credentials", "_get_slack_tokens"}
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or fn.name not in targets:
            continue
        for handler in (n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)):
            for call in _logger_calls(handler):
                for arg in call.args[1:]:
                    names = {n.id for n in ast.walk(arg) if isinstance(n, ast.Name)}
                    if "secret_id" in names:
                        offenders.append(f"{fn.name}:{call.lineno} logs secret_id")
                    if handler.name and handler.name in names and not _is_type_name(arg, handler.name):
                        offenders.append(f"{fn.name}:{call.lineno} logs raw {handler.name}")
                if call.keywords and any(k.arg == "exc_info" for k in call.keywords):
                    offenders.append(f"{fn.name}:{call.lineno} logs exc_info")
    assert not offenders, offenders
