"""RED-first narrow client tests for the schedule approval Lambda."""

from __future__ import annotations

import io
import json

import pytest

from web.schedule_control import LambdaScheduleControlClient


FUNCTION_ARN = (
    "arn:aws:lambda:eu-west-1:123456789012:function:"
    "personal-operator-scheduler-control"
)
PROPOSAL = "proposal_" + "a" * 64


class LambdaClient:
    def __init__(self):
        self.calls = []
        self.response = {}

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "StatusCode": 200,
            "Payload": io.BytesIO(
                json.dumps(
                    self.response, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ),
        }


def client():
    raw = LambdaClient()
    return LambdaScheduleControlClient(client=raw, function_arn=FUNCTION_ARN), raw


def test_preview_invokes_exact_control_function_and_validates_response():
    control, raw = client()
    raw.response = {
        "proposalRef": PROPOSAL,
        "operationId": "schedule.propose",
        "scheduleId": "sch_" + "b" * 64,
        "revision": 1,
        "argsHash": "c" * 64,
        "arguments": {
            "taskType": "REMINDER",
            "definition": {
                "message": "synthetic reminder",
                "runAt": 1_800_000_600,
                "timezone": "Europe/London",
            },
        },
        "expiresAt": 1_800_000_900,
        "state": "PENDING",
    }

    preview = control.preview(user_id="user_a1", proposal_ref=PROPOSAL)

    assert preview == raw.response
    call = raw.calls[0]
    assert call["FunctionName"] == FUNCTION_ARN
    assert call["InvocationType"] == "RequestResponse"
    assert json.loads(call["Payload"]) == {
        "action": "PREVIEW",
        "userId": "user_a1",
        "proposalRef": PROPOSAL,
    }


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_approve_and_reject_echo_exact_preview_binding(verb):
    control, raw = client()
    status = "SUCCEEDED" if verb == "approve" else "REJECTED"
    raw.response = {
        "status": status,
        "proposalRef": PROPOSAL,
        "scheduleId": "sch_" + "b" * 64,
        "revision": 1,
    }

    outcome = getattr(control, verb)(
        user_id="user_a1",
        proposal_ref=PROPOSAL,
        revision=1,
        args_hash="c" * 64,
    )

    assert outcome == raw.response
    assert json.loads(raw.calls[0]["Payload"]) == {
        "action": verb.upper(),
        "userId": "user_a1",
        "proposalRef": PROPOSAL,
        "revision": 1,
        "argsHash": "c" * 64,
    }


def test_account_deletion_purge_returns_only_bounded_remaining_count():
    control, raw = client()
    raw.response = {"remaining": 0}

    assert control.purge_user_schedules("user_a1") == 0
    assert json.loads(raw.calls[0]["Payload"]) == {
        "action": "PURGE_USER",
        "userId": "user_a1",
    }

    for poisoned in ({}, {"remaining": -1}, {"remaining": 0, "extra": 1}):
        raw.response = poisoned
        with pytest.raises(Exception):
            control.purge_user_schedules("user_a1")


def test_reconcile_is_observation_only_request_with_a_typed_outcome():
    control, raw = client()
    raw.response = {
        "status": "UNCERTAIN",
        "proposalRef": PROPOSAL,
        "scheduleId": "sch_" + "b" * 64,
        "revision": 1,
    }

    assert control.reconcile(user_id="user_a1", proposal_ref=PROPOSAL) == raw.response
    assert json.loads(raw.calls[0]["Payload"]) == {
        "action": "RECONCILE",
        "userId": "user_a1",
        "proposalRef": PROPOSAL,
    }


def test_function_errors_malformed_json_and_extra_preview_fields_fail_closed():
    control, raw = client()
    raw.response = {"remaining": 0}
    original = raw.invoke

    def function_error(**kwargs):
        response = original(**kwargs)
        response["FunctionError"] = "Unhandled"
        return response

    raw.invoke = function_error
    with pytest.raises(Exception, match="failed"):
        control.purge_user_schedules("user_a1")

    raw.invoke = lambda **_kwargs: {
        "StatusCode": 200,
        "Payload": io.BytesIO(b'{"remaining":0,"remaining":1}'),
    }
    with pytest.raises(Exception, match="invalid"):
        control.purge_user_schedules("user_a1")
