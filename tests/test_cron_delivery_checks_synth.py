"""Synth tests for the Cron Lambda's scheduled-task delivery checks (no AWS calls).

- cron_legacy_records ("read", the default, or "off") and cron_target_check
  ("refuse", the default, or "log") reach the Cron Lambda as
  CRON_LEGACY_RECORDS and CRON_TARGET_CHECK. Any other value fails the synth.
- Changing them changes only those two variables: no IAM change. The Cron
  role already has GetItem and UpdateItem on the whole identity table, which
  the SCHED#<userId> record read and the FIRE#<scheduleId> writes need.

    pytest tests/test_cron_delivery_checks_synth.py -v
"""

import copy

import aws_cdk as cdk
import pytest

from stacks.cron_stack import CRON_LEGACY_RECORDS_MODES, CRON_TARGET_CHECK_MODES, context_choice
from tests._app_synth import ACCOUNT, REGION, synth_app

FUNCTION = "openclaw-cron-executor"
IDENTITY_TABLE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/openclaw-identity"
DEFAULTS = {"CRON_LEGACY_RECORDS": "read", "CRON_TARGET_CHECK": "refuse"}
CHOICES = {"cron_legacy_records": CRON_LEGACY_RECORDS_MODES, "cron_target_check": CRON_TARGET_CHECK_MODES}


def _cron_function(template):
    (fn,) = [res for res in template["Resources"].values() if res["Type"] == "AWS::Lambda::Function"
             and res["Properties"].get("FunctionName") == FUNCTION]
    return fn


def _env(templates):
    return _cron_function(templates["OpenClawCron"])["Properties"]["Environment"]["Variables"]


def _as_list(value):
    return [value] if isinstance(value, str) else list(value)


def test_the_defaults_reach_the_cron_lambda():
    env = _env(synth_app())
    assert {key: env[key] for key in DEFAULTS} == DEFAULTS


def test_other_modes_change_only_their_variables():
    default = synth_app()
    changed = synth_app(cron_legacy_records="off", cron_target_check="log")
    env = _env(changed)
    assert (env["CRON_LEGACY_RECORDS"], env["CRON_TARGET_CHECK"]) == ("off", "log")
    assert sorted(default) == sorted(changed)
    for stack in default:
        expected = copy.deepcopy(changed[stack])
        if stack == "OpenClawCron":
            _cron_function(expected)["Properties"]["Environment"]["Variables"].update(DEFAULTS)
        assert expected == default[stack], stack


def test_the_cron_role_can_read_and_write_schedule_items():
    template = synth_app()["OpenClawCron"]
    role = _cron_function(template)["Properties"]["Role"]["Fn::GetAtt"][0]
    statements = [statement for res in template["Resources"].values() if res["Type"] == "AWS::IAM::Policy"
                  and {"Ref": role} in res["Properties"]["Roles"]
                  for statement in res["Properties"]["PolicyDocument"]["Statement"]]
    actions = {action for s in statements
               if s["Effect"] == "Allow" and "Condition" not in s and IDENTITY_TABLE_ARN in _as_list(s["Resource"])
               for action in _as_list(s["Action"])}
    assert {"dynamodb:GetItem", "dynamodb:UpdateItem"} <= actions


@pytest.mark.parametrize("key, value, expected", [
    ("cron_legacy_records", None, "read"), ("cron_legacy_records", "", "read"),
    ("cron_legacy_records", "  ", "read"), ("cron_legacy_records", " READ ", "read"),
    ("cron_legacy_records", "Off", "off"),
    ("cron_target_check", None, "refuse"), ("cron_target_check", "", "refuse"),
    ("cron_target_check", "refuse", "refuse"), ("cron_target_check", " LOG ", "log"),
])
def test_valid_values(key, value, expected):
    context = {} if value is None else {key: value}
    assert context_choice(cdk.App(context=context).node, key, CHOICES[key]) == expected


@pytest.mark.parametrize("key, message", [
    ("cron_legacy_records", "context cron_legacy_records must be read or off"),
    ("cron_target_check", "context cron_target_check must be refuse or log"),
])
@pytest.mark.parametrize("value", ["on", "true", "refused", "logs", "none", True, False, 0, 1, ["log"], {"x": 1}])
def test_other_values_fail_at_synth(key, message, value):
    with pytest.raises(ValueError, match=message):
        context_choice(cdk.App(context={key: value}).node, key, CHOICES[key])
