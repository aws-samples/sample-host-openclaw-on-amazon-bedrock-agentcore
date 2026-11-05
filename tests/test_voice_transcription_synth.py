"""Synth tests for voice transcription (voice_transcription), off by default.

- Off (the default) adds nothing: no VOICE_* variables, no Transcribe or
  voice_* S3 statements, so the templates are what they were before the setting.
- On, the Router gets the VOICE_* variables and two statements: the three
  Transcribe job actions on transcription-job/oc-*, and GetObject, DeleteObject
  and DeleteObjectVersion on */_uploads/voice_*. Nothing else in any stack
  changes, and cdk-nag reports no unsuppressed errors.
- Values outside the documented ranges fail the synth.

No AWS calls.

    pytest tests/test_voice_transcription_synth.py -v
"""

import copy

import aws_cdk as cdk
import pytest

from stacks.router_stack import voice_transcription
from tests._app_synth import ACCOUNT, REGION, resources, synth_app, synth_errors

ON = {"voice_transcription": "transcribe", "voice_language_options": ["en-US", "zh-CN"]}
TRANSCRIBE_ACTIONS = {"transcribe:StartTranscriptionJob", "transcribe:GetTranscriptionJob",
                      "transcribe:DeleteTranscriptionJob"}
VOICE_OBJECT_ACTIONS = {"s3:GetObject", "s3:DeleteObject", "s3:DeleteObjectVersion"}
JOBS_ARN = f"arn:aws:transcribe:{REGION}:{ACCOUNT}:transcription-job/oc-*"


def _router(templates):
    stack = templates["OpenClawRouter"]
    function_id, function = next((lid, res) for lid, res in stack["Resources"].items()
                                 if res["Type"] == "AWS::Lambda::Function"
                                 and res["Properties"].get("FunctionName") == "openclaw-router")
    role = function["Properties"]["Role"]["Fn::GetAtt"][0]
    policies = {lid: res for lid, res in stack["Resources"].items()
                if res["Type"] == "AWS::IAM::Policy" and {"Ref": role} in res["Properties"]["Roles"]}
    return stack, function_id, policies


def _actions(statement):
    actions = statement["Action"]
    return {actions} if isinstance(actions, str) else set(actions)


def _is_voice_statement(statement):
    return bool(_actions(statement) & TRANSCRIBE_ACTIONS) or "_uploads/voice_" in str(statement["Resource"])


def _all_statements(templates):
    for _, _, res in resources(templates, "AWS::IAM::Policy"):
        yield from res["Properties"]["PolicyDocument"]["Statement"]


def test_off_by_default_adds_nothing():
    templates = synth_app()
    stack, function_id, _ = _router(templates)
    variables = stack["Resources"][function_id]["Properties"]["Environment"]["Variables"]
    assert not [name for name in variables if name.startswith("VOICE_")]
    assert not [s for s in _all_statements(templates) if _is_voice_statement(s)]
    assert "transcribe" not in str(templates).lower().replace("transcribed", "")


def test_on_adds_the_variables_and_two_statements():
    stack, function_id, policies = _router(synth_app(**ON))
    variables = stack["Resources"][function_id]["Properties"]["Environment"]["Variables"]
    assert {name: value for name, value in variables.items() if name.startswith("VOICE_")} == {
        "VOICE_TRANSCRIPTION": "transcribe",
        "VOICE_MAX_SECONDS": "300",
        "VOICE_TRANSCRIBE_TIMEOUT_SECONDS": "45",
        "VOICE_LANGUAGE_OPTIONS": '["en-US","zh-CN"]',
    }
    statements = [s for policy in policies.values() for s in policy["Properties"]["PolicyDocument"]["Statement"]
                  if _is_voice_statement(s)]
    assert len(statements) == 2
    jobs, objects = sorted(statements, key=lambda s: "s3:" in str(s["Action"]))
    assert jobs["Effect"] == "Allow" and _actions(jobs) == TRANSCRIBE_ACTIONS and jobs["Resource"] == JOBS_ARN
    assert objects["Effect"] == "Allow" and _actions(objects) == VOICE_OBJECT_ACTIONS
    join = objects["Resource"]["Fn::Join"][1]
    assert "Fn::ImportValue" in str(join[0]) and join[1:] == ["/*/_uploads/voice_*"]  # the user-files bucket
    assert "Condition" not in jobs and "Condition" not in objects


def test_turning_it_on_changes_only_the_router_variables_and_role_policy():
    off, on = synth_app(), synth_app(**ON)
    assert sorted(off) == sorted(on)
    for name in off:
        if name != "OpenClawRouter":
            assert on[name] == off[name], name
    stack, function_id, policies = _router(on)
    stack = copy.deepcopy(stack)
    variables = stack["Resources"][function_id]["Properties"]["Environment"]["Variables"]
    for name in [name for name in variables if name.startswith("VOICE_")]:
        del variables[name]
    for policy_id in policies:
        document = stack["Resources"][policy_id]["Properties"]["PolicyDocument"]
        document["Statement"] = [s for s in document["Statement"] if not _is_voice_statement(s)]
    for template in (stack, off_stack := copy.deepcopy(off["OpenClawRouter"])):
        for res in template["Resources"].values():
            res.pop("Metadata", None)  # cdk-nag suppressions name the voice resources when on
    assert stack == off_stack


def test_no_unsuppressed_nag_errors_with_it_on():
    assert synth_errors(**ON) == []


@pytest.mark.parametrize("context, expected", [
    ({}, None),
    ({"voice_transcription": "off"}, None),
    ({"voice_transcription": " OFF "}, None),
    ({"voice_transcription": ""}, None),
    ({"voice_transcription": "off", "voice_max_seconds": "not checked when off"}, None),
    ({"voice_transcription": "transcribe"},
     {"VOICE_TRANSCRIPTION": "transcribe", "VOICE_MAX_SECONDS": "300", "VOICE_TRANSCRIBE_TIMEOUT_SECONDS": "45"}),
    ({"voice_transcription": "Transcribe", "voice_max_seconds": 120, "voice_transcribe_timeout_seconds": "30",
      "voice_language_options": '["en-US", "ja-JP"]'},
     {"VOICE_TRANSCRIPTION": "transcribe", "VOICE_MAX_SECONDS": "120", "VOICE_TRANSCRIBE_TIMEOUT_SECONDS": "30",
      "VOICE_LANGUAGE_OPTIONS": '["en-US","ja-JP"]'}),
    ({"voice_transcription": "transcribe", "voice_language_options": []},
     {"VOICE_TRANSCRIPTION": "transcribe", "VOICE_MAX_SECONDS": "300", "VOICE_TRANSCRIBE_TIMEOUT_SECONDS": "45"}),
])
def test_valid_values(context, expected):
    assert voice_transcription(cdk.App(context=context)) == expected


@pytest.mark.parametrize("context, message", [
    ({"voice_transcription": "on"}, "voice_transcription must be off or transcribe"),
    ({"voice_transcription": True}, "voice_transcription must be off or transcribe"),
    ({"voice_transcription": "transcribe", "voice_max_seconds": 0}, "'voice_max_seconds' must be >= 1"),
    ({"voice_transcription": "transcribe", "voice_max_seconds": 3601}, "voice_max_seconds must be <= 3600"),
    ({"voice_transcription": "transcribe", "voice_transcribe_timeout_seconds": 4},
     "'voice_transcribe_timeout_seconds' must be >= 5"),
    ({"voice_transcription": "transcribe", "voice_transcribe_timeout_seconds": 121},
     "voice_transcribe_timeout_seconds must be <= 120"),
    ({"voice_transcription": "transcribe", "voice_language_options": ["en-US"]}, "voice_language_options"),
    ({"voice_transcription": "transcribe", "voice_language_options": ["en-US", "english"]},
     "voice_language_options"),
    ({"voice_transcription": "transcribe", "voice_language_options": ["en-US", "en-US"]}, "voice_language_options"),
    ({"voice_transcription": "transcribe", "voice_language_options": "en-US,zh-CN"}, "voice_language_options"),
    ({"voice_transcription": "transcribe", "voice_language_options": {"en": "US"}}, "voice_language_options"),
])
def test_other_values_fail_at_synth(context, message):
    with pytest.raises(ValueError, match=message):
        voice_transcription(cdk.App(context=context))
