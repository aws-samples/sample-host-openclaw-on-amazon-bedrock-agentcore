"""Synth-time tests for the token and cost budget alarms.

Each budget has two alarms: an hourly spike alarm (Sum over 3600 s) and a
daily budget alarm (Sum over 86400 s). The period of each alarm must match its
name and its context key, and every alarm must describe its period and
threshold. The optional ``alarm_email`` context key adds one email
subscription to the ``openclaw-alarms`` topic. No AWS calls.

    pytest tests/test_budget_alarms_synth.py -v
"""

import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from stacks.observability_stack import ObservabilityStack
from stacks.security_stack import SecurityStack
from stacks.token_monitoring_stack import TokenMonitoringStack

_ROOT = Path(__file__).resolve().parent.parent
_ACCOUNT = "123456789012"
_REGION = "us-west-2"

# name -> (metric, period seconds, context key, default threshold)
_ALARMS = {
    "openclaw-hourly-token-spike": ("TotalTokens", 3600, "hourly_token_alarm", 5_000_000),
    "openclaw-daily-token-budget": ("TotalTokens", 86400, "daily_token_budget", 18_000_000),
    "openclaw-hourly-cost-spike": ("EstimatedCostUSD", 3600, "hourly_cost_alarm_usd", 30),
    "openclaw-daily-cost-budget": ("EstimatedCostUSD", 86400, "daily_cost_budget_usd", 100),
}


def _synth(**context):
    app = cdk.App(
        context={
            "account": _ACCOUNT,
            "region": _REGION,
            "availability_zones": [f"{_REGION}a", f"{_REGION}b"],
            **context,
        }
    )
    env = cdk.Environment(account=_ACCOUNT, region=_REGION)
    security = SecurityStack(app, "OpenClawSecurity", env=env)
    observability = ObservabilityStack(
        app, "OpenClawObservability", cmk_arn=security.cmk.key_arn, env=env
    )
    token = TokenMonitoringStack(
        app,
        "OpenClawTokenMonitoring",
        invocation_log_group=observability.invocation_log_group,
        alarm_topic=observability.alarm_topic,
        cmk_arn=security.cmk.key_arn,
        env=env,
    )
    return Template.from_stack(token), Template.from_stack(observability)


def _budget_alarms(template):
    alarms = {}
    for logical_id, res in template.find_resources("AWS::CloudWatch::Alarm").items():
        props = res["Properties"]
        if props.get("Namespace") == "OpenClaw/TokenUsage":
            alarms[props["AlarmName"]] = (logical_id, props)
    return alarms


@pytest.fixture(scope="module")
def default_alarms():
    token_tpl, _ = _synth()
    return _budget_alarms(token_tpl)


def test_exactly_four_budget_alarms(default_alarms):
    assert sorted(default_alarms) == sorted(_ALARMS)


@pytest.mark.parametrize("name", sorted(_ALARMS))
def test_alarm_period_statistic_and_default_threshold(default_alarms, name):
    metric, period, _key, threshold = _ALARMS[name]
    _, props = default_alarms[name]
    assert props["MetricName"] == metric
    assert props["Statistic"] == "Sum"
    assert props["Period"] == period
    assert props["EvaluationPeriods"] == 1
    assert props["Threshold"] == threshold
    assert props["ComparisonOperator"] == "GreaterThanThreshold"
    assert props["TreatMissingData"] == "notBreaching"
    assert len(props["AlarmActions"]) == 1


@pytest.mark.parametrize("name", sorted(_ALARMS))
def test_period_matches_name(default_alarms, name):
    _, props = default_alarms[name]
    expected = 3600 if "-hourly-" in name else 86400
    assert props["Period"] == expected


@pytest.mark.parametrize("name", sorted(_ALARMS))
def test_description_states_period_threshold_and_key(default_alarms, name):
    _, period, key, threshold = _ALARMS[name]
    description = default_alarms[name][1]["AlarmDescription"]
    assert f"({period} s)" in description
    assert ("1 hour" if period == 3600 else "1 day") in description
    assert f"{threshold:,}" in description
    assert f"context {key}" in description


def test_daily_alarms_keep_their_logical_ids(default_alarms):
    # Changing a logical ID would replace the alarm on upgrade.
    assert default_alarms["openclaw-daily-token-budget"][0].startswith("DailyTokenBudgetAlarm")
    assert default_alarms["openclaw-daily-cost-budget"][0].startswith("DailyCostBudgetAlarm")


def test_context_overrides_each_threshold():
    overrides = {
        "hourly_token_alarm": 1_234_567,
        "daily_token_budget": 7_654_321,
        "hourly_cost_alarm_usd": 12.5,
        "daily_cost_budget_usd": 250,
    }
    token_tpl, _ = _synth(**overrides)
    alarms = _budget_alarms(token_tpl)
    for name, (_metric, _period, key, _default) in _ALARMS.items():
        props = alarms[name][1]
        assert props["Threshold"] == overrides[key], name
        assert f"{overrides[key]:,}" in props["AlarmDescription"], name


@pytest.mark.parametrize("bad", [0, -5, "abc"])
def test_invalid_threshold_fails_synth(bad):
    with pytest.raises(ValueError):
        _synth(daily_cost_budget_usd=bad)


def test_cdk_json_defaults_match_stack_defaults():
    context = json.loads((_ROOT / "cdk.json").read_text())["context"]
    for _name, (_metric, _period, key, default) in _ALARMS.items():
        assert context[key] == default, key
    assert context["alarm_email"] == ""


def test_no_email_subscription_by_default():
    _, obs_tpl = _synth()
    obs_tpl.resource_count_is("AWS::SNS::Subscription", 0)


def test_alarm_email_adds_one_email_subscription():
    token_tpl, obs_tpl = _synth(alarm_email="alerts@example.com")
    subs = obs_tpl.find_resources("AWS::SNS::Subscription")
    assert len(subs) == 1
    props = next(iter(subs.values()))["Properties"]
    assert props["Protocol"] == "email"
    assert props["Endpoint"] == "alerts@example.com"
    topics = obs_tpl.find_resources("AWS::SNS::Topic")
    assert props["TopicArn"] == {"Ref": next(iter(topics))}
    # The subscription lives with the topic, not in the token stack.
    token_tpl.resource_count_is("AWS::SNS::Subscription", 0)


@pytest.mark.parametrize("bad", ["not-an-email", "a@example.com,b@example.com", "a b@example.com"])
def test_invalid_alarm_email_fails_synth(bad):
    with pytest.raises(ValueError):
        _synth(alarm_email=bad)
