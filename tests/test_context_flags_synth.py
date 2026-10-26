"""Boolean context keys and the token-metrics log retention (no AWS calls).

- ``cdk -c key=value`` passes every value as a string, so the boolean keys
  enable_gateway, enable_codebuild, enable_guardrails and enable_cloudtrail
  accept "true"/"false" in any case as well as JSON booleans from cdk.json.
  Before, ``-c enable_gateway=true`` did nothing and ``-c enable_cloudtrail=false``
  or ``-c enable_guardrails=false`` left the feature on.
- /openclaw/lambda/token-metrics follows cloudwatch_log_retention_days, with
  the same rounding as the other log groups.

    pytest tests/test_context_flags_synth.py -v
"""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from stacks import context_flag
from stacks.guardrails_stack import GuardrailsStack
from stacks.security_stack import SecurityStack
from tests._app_synth import ACCOUNT, REGION, resources, synth_app

_ENV = cdk.Environment(account=ACCOUNT, region=REGION)
_TOKEN_METRICS_LOG = "/openclaw/lambda/token-metrics"


def _app(**context):
    return cdk.App(context={"account": ACCOUNT, "region": REGION,
                            "availability_zones": [f"{REGION}a", f"{REGION}b"], **context})


# --- context_flag ---------------------------------------------------------

@pytest.mark.parametrize("value", [True, "true", "True", "TRUE", " true "])
def test_flag_true(value):
    assert context_flag(_app(flag=value).node, "flag") is True


@pytest.mark.parametrize("value", [False, "false", "False", "FALSE", " false "])
def test_flag_false(value):
    assert context_flag(_app(flag=value).node, "flag", default=True) is False


@pytest.mark.parametrize("default", [True, False])
@pytest.mark.parametrize("value", [None, "", "  "])
def test_flag_unset_returns_default(value, default):
    context = {} if value is None else {"flag": value}
    assert context_flag(_app(**context).node, "flag", default=default) is default


@pytest.mark.parametrize("value", ["yes", "1", "0", "on", "ture", 1, 0])
def test_flag_rejects_other_values(value):
    with pytest.raises(ValueError, match="context flag must be true or false"):
        context_flag(_app(flag=value).node, "flag")


# --- enable_guardrails (default on) --------------------------------------

def _guardrail_count(**context):
    app = _app(**context)
    security = SecurityStack(app, "OpenClawSecurity", env=_ENV)
    stack = GuardrailsStack(app, "OpenClawGuardrails", cmk_arn=security.cmk.key_arn, env=_ENV)
    return len(Template.from_stack(stack).find_resources("AWS::Bedrock::Guardrail"))


@pytest.mark.parametrize("value", [None, True, "true", "TRUE"])
def test_guardrails_on(value):
    assert _guardrail_count(**({} if value is None else {"enable_guardrails": value})) == 1


@pytest.mark.parametrize("value", [False, "false", "FALSE"])
def test_guardrails_off(value):
    assert _guardrail_count(enable_guardrails=value) == 0


# --- enable_cloudtrail (default off) -------------------------------------

def _trail_count(**context):
    stack = SecurityStack(_app(**context), "OpenClawSecurity", env=_ENV)
    return len(Template.from_stack(stack).find_resources("AWS::CloudTrail::Trail"))


@pytest.mark.parametrize("value", [None, False, "false", "FALSE"])
def test_cloudtrail_off(value):
    assert _trail_count(**({} if value is None else {"enable_cloudtrail": value})) == 0


@pytest.mark.parametrize("value", [True, "true", "TRUE"])
def test_cloudtrail_on(value):
    assert _trail_count(enable_cloudtrail=value) == 1


# --- whole app: enable_gateway, enable_codebuild, token-metrics retention --

def _token_metrics_retention(templates):
    (days,) = [res["Properties"].get("RetentionInDays")
               for _, _, res in resources(templates, "AWS::Logs::LogGroup")
               if res["Properties"].get("LogGroupName") == _TOKEN_METRICS_LOG]
    return days


def test_default_app_has_no_gateway_or_build_stack_and_30_day_token_logs():
    templates = synth_app()
    assert "OpenClawGateway" not in templates
    assert "OpenClawBuild" not in templates
    assert _token_metrics_retention(templates) == 30


def test_cli_string_true_adds_gateway():
    assert "OpenClawGateway" in synth_app(enable_gateway="true")


def test_cli_string_false_leaves_gateway_out():
    assert "OpenClawGateway" not in synth_app(enable_gateway="false")


def test_cli_strings_any_case_and_retention_rounding():
    templates = synth_app(enable_gateway="TRUE", enable_codebuild="True",
                          cloudwatch_log_retention_days="45")
    assert "OpenClawGateway" in templates
    assert "OpenClawBuild" in templates
    assert _token_metrics_retention(templates) == 60
