"""Logging, retention and audit-trail settings (no AWS calls).

- The Router logs a Telegram reply's text only when router_log_reply_text is
  set (test deployments, for the E2E log tailer); otherwise only its length.
- The token metrics Lambda does not log the content of a non-JSON record.
- bedrock_invocation_log_text controls textDataDeliveryEnabled (default true).
- /openclaw/container gets the configured retention on create and on change.
- The optional CloudTrail log group is retained, and S3 data events on the
  user-files bucket are added only with cloudtrail_s3_data_events.

All IDs here are placeholders.

    pytest tests/test_logging_retention_synth.py -v
"""

import importlib.util
import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from stacks.observability_stack import ObservabilityStack
from stacks.security_stack import SecurityStack

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "lambda" / "router" / "index.py"
TOKEN_METRICS = ROOT / "lambda" / "token_metrics" / "index.py"
SHARED = ROOT / "lambda" / "shared" / "python"
for _path in (SHARED, ROUTER.parent):
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

_ACCOUNT = "123456789012"
_REGION = "us-west-2"
REPLY = "Here is your key: sk-test-PLACEHOLDER-0000"


# --- Router reply logging ---------------------------------------------------

def _load_router(extra_env=None):
    spec = importlib.util.spec_from_file_location("router_reply_logging_under_test", ROUTER)
    module = importlib.util.module_from_spec(spec)
    env = {
        "AGENTCORE_RUNTIME_ARN": f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:runtime/test",
        "AGENTCORE_QUALIFIER": "DEFAULT",
        "IDENTITY_TABLE_NAME": "openclaw-identity",
        "AWS_REGION": _REGION,
        **(extra_env or {}),
    }
    with patch.dict(os.environ, env), patch("boto3.client", MagicMock()), \
            patch("boto3.resource", MagicMock()):
        spec.loader.exec_module(module)
    return module


def _run_telegram_turn(router, caplog):
    update = {"message": {"text": "show my key", "chat": {"id": 1}, "from": {"id": 2, "first_name": "T"}}}
    with patch.object(router, "_get_telegram_token", return_value="placeholder"), \
            patch.object(router, "resolve_user", return_value=("user_test", False)), \
            patch.object(router, "get_or_create_session", return_value=MagicMock(actor_id="telegram:2")), \
            patch.object(router, "authorize_channel_actor"), \
            patch.object(router, "get_model_override", return_value=None), \
            patch.object(router, "send_telegram_typing"), \
            patch.object(router, "_periodic_typing"), \
            patch.object(router, "invoke_agent_runtime", return_value={"response": REPLY}), \
            patch.object(router, "_send_telegram_chunks") as send, \
            caplog.at_level(logging.INFO):
        router.handle_telegram(update)
    send.assert_called_once()
    return [r.getMessage() for r in caplog.records]


def test_router_does_not_log_reply_text_by_default(caplog):
    lines = _run_telegram_turn(_load_router(), caplog)
    assert f"Response to send (len={len(REPLY)})" in lines
    assert not any("sk-test-PLACEHOLDER" in line for line in lines), lines


def test_router_logs_reply_text_when_enabled_for_tests(caplog):
    lines = _run_telegram_turn(_load_router({"LOG_REPLY_TEXT": "true"}), caplog)
    assert f"Response to send (len={len(REPLY)}): {REPLY}" in lines


def test_e2e_log_tailer_reads_both_forms():
    from tests.e2e.log_tailer import TailResult, _parse_line

    with_text = TailResult()
    _parse_line(f"Response to send (len={len(REPLY)}): {REPLY}", with_text)
    assert (with_text.response_len, with_text.response_text) == (len(REPLY), REPLY)
    length_only = TailResult()
    _parse_line("Response to send (len=42)", length_only)
    assert (length_only.response_len, length_only.response_text) == (42, "")


# --- Token metrics: non-JSON record -----------------------------------------

def test_token_metrics_does_not_log_non_json_record_content(caplog):
    spec = importlib.util.spec_from_file_location("token_metrics_logging_under_test", TOKEN_METRICS)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"AWS_REGION": _REGION, "TABLE_NAME": "t", "AWS_DEFAULT_REGION": _REGION}), \
            patch("boto3.client", MagicMock()), patch("boto3.resource", MagicMock()):
        spec.loader.exec_module(module)
    import base64
    import gzip
    payload = {"logEvents": [{"id": "1", "timestamp": 0, "message": "not json " + REPLY}]}
    data = base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()
    with caplog.at_level(logging.INFO):
        module.handler({"awslogs": {"data": data}}, None)
    lines = [r.getMessage() for r in caplog.records]
    assert any("Skipping non-JSON log entry" in line for line in lines), lines
    assert not any("sk-test-PLACEHOLDER" in line for line in lines), lines


# --- Stacks -------------------------------------------------------------------

def _app(**context):
    return cdk.App(context={"account": _ACCOUNT, "region": _REGION,
                            "availability_zones": [f"{_REGION}a", f"{_REGION}b"], **context})


def _env():
    return cdk.Environment(account=_ACCOUNT, region=_REGION)


def _observability(**context):
    app = _app(**context)
    security = SecurityStack(app, "OpenClawSecurity", env=_env())
    obs = ObservabilityStack(app, "OpenClawObservability", cmk_arn=security.cmk.key_arn, env=_env())
    return Template.from_stack(obs).to_json()


def _security(**context):
    app = _app(**context)
    return Template.from_stack(SecurityStack(app, "OpenClawSecurity", env=_env())).to_json()


def _of(template, kind):
    return {k: v for k, v in template["Resources"].items() if v["Type"] == kind}


def _logging_cr(template):
    [cr] = [v for v in _of(template, "Custom::AWS").values()
            if "PutModelInvocationLoggingConfiguration" in json.dumps(v["Properties"].get("Create"))]
    return cr


@pytest.mark.parametrize("context, expected", [({}, True), ({"bedrock_invocation_log_text": False}, False),
                                               ({"bedrock_invocation_log_text": "false"}, False)])
def test_invocation_log_text_delivery_follows_context(context, expected):
    cr = _logging_cr(_observability(**context))
    for phase in ("Create", "Update"):
        body = json.dumps(cr["Properties"][phase])
        assert f'\\"textDataDeliveryEnabled\\":{str(expected).lower()}' in body, body


def test_invocation_log_text_rejects_invalid_value():
    with pytest.raises(ValueError, match="bedrock_invocation_log_text"):
        _observability(bedrock_invocation_log_text="sometimes")


def _retention_cr(template):
    found = [(k, v) for k, v in _of(template, "Custom::AWS").items()
             if "putRetentionPolicy" in json.dumps(v["Properties"].get("Create"))]
    assert len(found) == 1, found
    return found[0]


@pytest.mark.parametrize("context, days", [({}, 30), ({"cloudwatch_log_retention_days": 90}, 90)])
def test_container_log_group_gets_configured_retention(context, days):
    template = _observability(**context)
    _, cr = _retention_cr(template)
    for phase in ("Create", "Update"):
        body = json.dumps(cr["Properties"][phase])
        assert "/openclaw/container" in body and f'\\"retentionInDays\\":{days}' in body, body
    assert "Delete" not in cr["Properties"]
    [ensure_key] = [k for k, v in _of(template, "Custom::AWS").items()
                    if "createLogGroup" in json.dumps(v["Properties"].get("Create"))]
    assert ensure_key in cr.get("DependsOn", []), "retention must be set after the group exists"


def test_cloudtrail_stays_off_by_default():
    assert not _of(_security(), "AWS::CloudTrail::Trail")


def test_cloudtrail_log_group_is_retained_and_has_no_data_events_by_default():
    template = _security(enable_cloudtrail=True)
    [trail] = _of(template, "AWS::CloudTrail::Trail").values()
    assert trail["Properties"]["IsMultiRegionTrail"] is False
    assert not trail["Properties"].get("EventSelectors")
    [group] = _of(template, "AWS::Logs::LogGroup").values()
    assert group["DeletionPolicy"] == "Retain" and group["UpdateReplacePolicy"] == "Retain"


def test_cloudtrail_s3_data_events_on_user_files_bucket_when_enabled():
    template = _security(enable_cloudtrail=True, cloudtrail_s3_data_events=True)
    [trail] = _of(template, "AWS::CloudTrail::Trail").values()
    [selector] = trail["Properties"]["EventSelectors"]
    assert selector["ReadWriteType"] == "All" and selector["IncludeManagementEvents"] is True
    [resource] = selector["DataResources"]
    assert resource["Type"] == "AWS::S3::Object"
    assert f"openclaw-user-files-{_ACCOUNT}-{_REGION}/" in json.dumps(resource["Values"])


def test_cloudtrail_s3_data_events_need_enable_cloudtrail():
    assert not _of(_security(cloudtrail_s3_data_events=True), "AWS::CloudTrail::Trail")
