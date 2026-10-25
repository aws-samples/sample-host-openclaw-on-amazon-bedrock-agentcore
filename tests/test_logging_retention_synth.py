"""Tests for log retention and Router reply logging (no AWS calls).

- cloudwatch_log_retention_days is rounded up to a value CloudWatch Logs
  accepts, read from cdk.json (int) or ``-c`` (string), and anything else fails
  synth with a clear error instead of failing the deployment.
- /openclaw/container gets that retention on create as well as on update, so
  a fresh install does not keep container logs forever.
- The Router logs only the length of a Telegram reply unless the test-only
  router_log_reply_text key is set; the E2E log tailer reads both forms.

    pytest tests/test_logging_retention_synth.py -v
"""

import ast
import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Annotations, Match, Template

from stacks import normalize_retention_days, retention_days
from stacks.observability_stack import ObservabilityStack
from stacks.security_stack import SecurityStack
from tests._app_synth import ACCOUNT, REGION, resources, synth_app
from tests.e2e.log_tailer import TailResult, _parse_line

_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("raw, days", [
    (1, 1), (30, 30), ("30", 30), (" 90 ", 90), (45, 60), ("45", 60), (366, 400),
    (1827, 1827), (1828, 2192), (3653, 3653),
])
def test_retention_is_rounded_up_to_a_cloudwatch_value(raw, days):
    assert normalize_retention_days(raw) == days
    assert retention_days(raw) is retention_days(days)


@pytest.mark.parametrize("raw", [0, -1, 3654, "abc", "30.5", 30.5, True, None, ""])
def test_invalid_retention_fails_with_a_clear_error(raw):
    with pytest.raises(ValueError, match="cloudwatch_log_retention_days"):
        normalize_retention_days(raw)


def _observability(**context):
    app = cdk.App(context={"account": ACCOUNT, "region": REGION,
                           "availability_zones": [f"{REGION}a", f"{REGION}b"], **context})
    env = cdk.Environment(account=ACCOUNT, region=REGION)
    security = SecurityStack(app, "OpenClawSecurity", env=env)
    return ObservabilityStack(app, "OpenClawObservability", cmk_arn=security.cmk.key_arn, env=env)


def _sdk_calls(stack):
    template = Template.from_stack(stack).to_json()
    return {lid: res for lid, res in template["Resources"].items() if res["Type"] == "Custom::AWS"}


def _retention_calls(stack):
    calls = _sdk_calls(stack)
    found = {}
    for lid, res in calls.items():
        for event in ("Create", "Update"):
            raw = res["Properties"].get(event)
            if isinstance(raw, dict):  # Fn::Join around tokens such as the partition
                raw = "".join(x if isinstance(x, str) else "TOKEN" for x in raw["Fn::Join"][1])
            call = json.loads(raw or "null")
            if call and call["action"] == "putRetentionPolicy":
                assert call["parameters"]["logGroupName"] == "/openclaw/container"
                found.setdefault(event, set()).add(call["parameters"]["retentionInDays"])
    return found


def test_container_retention_is_applied_on_create_and_update():
    found = _retention_calls(_observability())
    assert found["Create"] == {30}
    assert found["Update"] == {30}


def test_container_retention_waits_for_the_group_to_exist():
    stack = _observability()
    template = Template.from_stack(stack).to_json()
    (lid,) = [k for k in _sdk_calls(stack) if k.startswith("ContainerLogGroupRetention")]
    ensure = [k for k in _sdk_calls(stack) if k.startswith("EnsureContainerLogGroup")]
    assert set(ensure) <= set(template["Resources"][lid]["DependsOn"])
    assert "Delete" not in template["Resources"][lid]["Properties"]


@pytest.mark.parametrize("raw, days", [("90", 90), (45, 60)])
def test_container_retention_uses_the_normalized_value(raw, days):
    found = _retention_calls(_observability(cloudwatch_log_retention_days=raw))
    assert found["Create"] == {days} and found["Update"] == {days}


def test_rounded_retention_is_reported_at_synth():
    stack = _observability(cloudwatch_log_retention_days=45)
    warnings = Annotations.from_stack(stack).find_warning("*", Match.string_like_regexp("45.*60 days"))
    assert warnings


def test_invalid_retention_fails_synth():
    with pytest.raises(ValueError, match="cloudwatch_log_retention_days"):
        _observability(cloudwatch_log_retention_days="forever")


def _router_env(templates):
    (fn,) = [res for _, _, res in resources(templates, "AWS::Lambda::Function")
             if res["Properties"].get("FunctionName") == "openclaw-router"]
    return fn["Properties"]["Environment"]["Variables"]


def test_router_does_not_log_reply_text_by_default():
    assert "LOG_REPLY_TEXT" not in _router_env(synth_app())


def test_router_logs_reply_text_only_when_opted_in():
    assert _router_env(synth_app(router_log_reply_text="true"))["LOG_REPLY_TEXT"] == "true"


def test_reply_text_log_line_is_behind_the_flag():
    """The only log call that formats the reply text sits under `if LOG_REPLY_TEXT:`."""
    tree = ast.parse((_ROOT / "lambda" / "router" / "index.py").read_text())
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
    request = "2026-01-01T00:00:00Z\t00000000-0000-0000-0000-000000000000\t"
    result = TailResult()
    _parse_line(f"[INFO]\t{request}Telegram response ready (len=12, streamed=False)", result)
    assert result.response_len == 12 and result.response_text == ""
    _parse_line(f"[INFO]\t{request}Response to send (len=12): hello there!", result)
    assert result.response_len == 12 and result.response_text == "hello there!"
