"""Synth-time tests for the cognito_advanced_security_mode context key.

Cognito threat protection needs the Plus feature plan, which is billed per
monthly active user, so it is opt-in. With the key unset (or OFF) the user
pool has no UserPoolAddOns and no UserPoolTier, the same as before the key
existed. AUDIT and ENFORCED set the matching AdvancedSecurityMode and the
PLUS tier. Any other value fails synth. No AWS calls.

    pytest tests/test_cognito_threat_protection_synth.py -v
"""

import json
from pathlib import Path

import pytest

import aws_cdk as cdk
from aws_cdk.assertions import Annotations, Match, Template
import cdk_nag

from stacks.security_stack import SecurityStack

_ROOT = Path(__file__).resolve().parent.parent
_ENV = cdk.Environment(account="123456789012", region="us-west-2")


def _synth(**context_overrides):
    context = json.loads((_ROOT / "cdk.json").read_text())["context"]
    context.update({"account": _ENV.account, "region": _ENV.region})
    context.update(context_overrides)
    app = cdk.App(context=context)
    stack = SecurityStack(app, "TestSecurity", env=_ENV)
    cdk.Aspects.of(app).add(cdk_nag.AwsSolutionsChecks())
    return stack


def _user_pool(stack):
    pools = Template.from_stack(stack).find_resources("AWS::Cognito::UserPool")
    assert len(pools) == 1
    return next(iter(pools.values()))["Properties"]


def _nag_errors(stack, rule):
    return Annotations.from_stack(stack).find_error(
        "*", Match.string_like_regexp(f"AwsSolutions-{rule}")
    )


def test_cdk_json_default_is_off():
    context = json.loads((_ROOT / "cdk.json").read_text())["context"]
    assert context["cognito_advanced_security_mode"] == "OFF"


@pytest.mark.parametrize("value", [None, "", "OFF", "off"])
def test_default_leaves_threat_protection_off(value):
    overrides = {} if value is None else {"cognito_advanced_security_mode": value}
    if value is None:
        # Simulate a cdk.json that does not carry the key at all.
        context = json.loads((_ROOT / "cdk.json").read_text())["context"]
        context.pop("cognito_advanced_security_mode", None)
        context.update({"account": _ENV.account, "region": _ENV.region})
        app = cdk.App(context=context)
        stack = SecurityStack(app, "TestSecurity", env=_ENV)
        cdk.Aspects.of(app).add(cdk_nag.AwsSolutionsChecks())
    else:
        stack = _synth(**overrides)
    props = _user_pool(stack)
    assert "UserPoolAddOns" not in props
    assert "UserPoolTier" not in props
    assert _nag_errors(stack, "COG3") == []
    assert _nag_errors(stack, "COG8") == []


@pytest.mark.parametrize("value", ["AUDIT", "ENFORCED", "enforced"])
def test_opt_in_sets_mode_and_plus_tier(value):
    stack = _synth(cognito_advanced_security_mode=value)
    props = _user_pool(stack)
    assert props["UserPoolAddOns"] == {"AdvancedSecurityMode": value.upper()}
    assert props["UserPoolTier"] == "PLUS"
    assert _nag_errors(stack, "COG3") == []
    assert _nag_errors(stack, "COG8") == []


def test_password_policy_unchanged_by_mode():
    off = _user_pool(_synth())["Policies"]
    on = _user_pool(_synth(cognito_advanced_security_mode="ENFORCED"))["Policies"]
    assert off == on
    assert off["PasswordPolicy"]["RequireUppercase"] is True
    assert off["PasswordPolicy"]["RequireNumbers"] is True


@pytest.mark.parametrize("value", ["ON", "true", "FULL_FUNCTION", "audit-only"])
def test_invalid_value_rejected(value):
    with pytest.raises(Exception, match="cognito_advanced_security_mode must be one of"):
        _synth(cognito_advanced_security_mode=value)
