"""Synth tests for the per-user daily cost limit (per_user_daily_cost_usd) and /usage.

- The limit reaches the Router and the Cron Lambda as PER_USER_DAILY_COST_USD,
  only when it is on. Off (0, the default) adds nothing, and turning it on
  changes nothing else in any stack: the Router and Cron need no new IAM.
- The token-metrics Lambda writes the daily usage items in the identity table:
  UpdateItem only under USAGE#, GetItem only on CHANNEL# records, and the CMK
  only through DynamoDB.
- openclaw-token-metrics-errors alarms when that Lambda fails or logs a record
  or usage item it could not write.

No AWS calls.

    pytest tests/test_usage_limits_synth.py -v
"""

import copy
import importlib.util
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest

from stacks.context_limits import per_user_daily_cost_usd
from tests._app_synth import ACCOUNT, REGION, resources, synth_app, synth_errors

ROOT = Path(__file__).resolve().parents[1]
IDENTITY_TABLE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/openclaw-identity"
VARIABLE = "PER_USER_DAILY_COST_USD"


def _functions(templates):
    return {res["Properties"].get("FunctionName"): res
            for _, _, res in resources(templates, "AWS::Lambda::Function")}


def _env(templates, name):
    return _functions(templates)[name]["Properties"]["Environment"]["Variables"]


def _without_variable(template):
    template = copy.deepcopy(template)
    for res in template["Resources"].values():
        if res["Type"] == "AWS::Lambda::Function":
            res["Properties"].get("Environment", {}).get("Variables", {}).pop(VARIABLE, None)
    return template


def test_the_limit_is_off_by_default_and_adds_no_variable():
    templates = synth_app()
    for name in ("openclaw-router", "openclaw-cron-executor"):
        assert VARIABLE not in _env(templates, name), name


@pytest.mark.parametrize("value, expected", [(5, "5"), ("2.50", "2.5"), (0.75, "0.75"), (10000, "10000")])
def test_the_limit_reaches_the_router_and_the_cron_lambda(value, expected):
    templates = synth_app(per_user_daily_cost_usd=value)
    for name in ("openclaw-router", "openclaw-cron-executor"):
        assert _env(templates, name)[VARIABLE] == expected, name
    assert VARIABLE not in _env(templates, "openclaw-token-metrics")


def test_turning_the_limit_on_changes_only_that_variable():
    off, on = synth_app(), synth_app(per_user_daily_cost_usd=5)
    assert sorted(off) == sorted(on)
    for stack in off:
        assert _without_variable(on[stack]) == off[stack], stack


def test_no_unsuppressed_nag_errors_with_the_limit_on():
    assert synth_errors(per_user_daily_cost_usd=5) == []


@pytest.mark.parametrize("value, expected", [
    (None, None), ("", None), ("  ", None), (0, None), ("0", None), ("0.00", None),
    (5, "5"), ("5", "5"), (" 7.50 ", "7.5"), (0.5, "0.5"), (10000, "10000"), ("1e3", "1000"),
])
def test_valid_values(value, expected):
    context = {} if value is None else {"per_user_daily_cost_usd": value}
    assert per_user_daily_cost_usd(cdk.App(context=context)) == expected


@pytest.mark.parametrize("value", [-1, "-0.01", 10000.01, "10001", "abc", "5 USD", "nan", "inf", "-inf",
                                   True, False, [5], {"usd": 5}])
def test_other_values_fail_at_synth(value):
    with pytest.raises(ValueError, match="'per_user_daily_cost_usd' must be a number of USD from 0 to 10000"):
        per_user_daily_cost_usd(cdk.App(context={"per_user_daily_cost_usd": value}))


# --------------------------------------------------------------------------- token-metrics Lambda


def _token_statements(templates):
    stack = templates["OpenClawTokenMonitoring"]
    role = _functions(templates)["openclaw-token-metrics"]["Properties"]["Role"]["Fn::GetAtt"][0]
    statements = []
    for res in stack["Resources"].values():
        if res["Type"] == "AWS::IAM::Policy" and {"Ref": role} in res["Properties"]["Roles"]:
            statements += res["Properties"]["PolicyDocument"]["Statement"]
    return statements


def _actions(statement):
    actions = statement["Action"]
    return {actions} if isinstance(actions, str) else set(actions)


def test_token_metrics_lambda_gets_the_identity_table_name():
    assert _env(synth_app(), "openclaw-token-metrics")["IDENTITY_TABLE_NAME"] == "openclaw-identity"


def test_token_metrics_identity_table_access_is_limited_by_leading_keys():
    statements = _token_statements(synth_app())
    on_identity = [s for s in statements if IDENTITY_TABLE_ARN in str(s["Resource"])]
    assert len(on_identity) == 2
    by_action = {frozenset(_actions(s)): s for s in on_identity}
    update = by_action[frozenset({"dynamodb:UpdateItem"})]
    get = by_action[frozenset({"dynamodb:GetItem"})]
    for statement, prefix in ((update, "USAGE#*"), (get, "CHANNEL#*")):
        assert statement["Effect"] == "Allow"
        assert statement["Resource"] == IDENTITY_TABLE_ARN  # the table only, no index or wildcard
        assert statement["Condition"] == {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [prefix]}}


def test_token_metrics_kms_access_is_only_through_dynamodb():
    statements = _token_statements(synth_app())
    kms = [s for s in statements if any(a.startswith("kms:") for a in _actions(s))]
    assert len(kms) == 1
    assert _actions(kms[0]) == {"kms:Decrypt", "kms:DescribeKey", "kms:Encrypt", "kms:ReEncrypt*",
                                "kms:GenerateDataKey*"}
    assert kms[0]["Condition"] == {"StringEquals": {"kms:ViaService": f"dynamodb.{REGION}.amazonaws.com"}}
    assert "Fn::ImportValue" in str(kms[0]["Resource"])  # the CMK's ARN from OpenClawSecurity, not "*"


def _token_metrics_module():
    spec = importlib.util.spec_from_file_location("token_metrics_synth_check",
                                                  ROOT / "lambda" / "token_metrics" / "index.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"TABLE_NAME": "t", "AWS_DEFAULT_REGION": REGION}), \
            patch("boto3.resource", MagicMock()), patch("boto3.client", MagicMock()):
        spec.loader.exec_module(module)
    return module


def test_token_metrics_errors_alarm():
    stack = synth_app()["OpenClawTokenMonitoring"]
    alarms = [res["Properties"] for res in stack["Resources"].values()
              if res["Type"] == "AWS::CloudWatch::Alarm"
              and res["Properties"].get("AlarmName") == "openclaw-token-metrics-errors"]
    assert len(alarms) == 1
    alarm = alarms[0]
    assert alarm["Threshold"] == 0 and alarm["ComparisonOperator"] == "GreaterThanThreshold"
    assert alarm["EvaluationPeriods"] == 1 and alarm["TreatMissingData"] == "notBreaching"
    assert len(alarm["AlarmActions"]) == 1
    metrics = {m["Id"]: m for m in alarm["Metrics"]}
    assert metrics["expr_1"]["Expression"] == "FILL(errors, 0) + FILL(failures, 0)"
    errors, failures = metrics["errors"]["MetricStat"], metrics["failures"]["MetricStat"]
    assert errors["Metric"]["Namespace"] == "AWS/Lambda" and errors["Metric"]["MetricName"] == "Errors"
    assert errors["Metric"]["Dimensions"][0]["Name"] == "FunctionName"
    for stat in (errors, failures):
        assert stat["Period"] == 3600 and stat["Stat"] == "Sum"
    assert failures["Metric"] == {"Namespace": "OpenClaw/Health", "MetricName": "TokenMetricsWriteFailures"}

    filters = [res["Properties"] for res in stack["Resources"].values()
               if res["Type"] == "AWS::Logs::MetricFilter"
               and res["Properties"]["MetricTransformations"][0]["MetricName"] == "TokenMetricsWriteFailures"]
    assert len(filters) == 1
    assert filters[0]["FilterPattern"] == '?"Failed to process log event" ?"Usage aggregate not written"'
    assert filters[0]["LogGroupName"] == {"Ref": next(
        lid for lid, res in stack["Resources"].items() if res["Type"] == "AWS::Logs::LogGroup"
        and res["Properties"].get("LogGroupName") == "/openclaw/lambda/token-metrics")}
    # The filter terms are the Lambda's own log text.
    module = _token_metrics_module()
    assert module.USAGE_WRITE_FAILED == "Usage aggregate not written"
    assert 'logger.exception("Failed to process log event")' in (ROOT / "lambda/token_metrics/index.py").read_text()
