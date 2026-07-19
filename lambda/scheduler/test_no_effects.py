"""LOAD-BEARING: a scheduled read-only turn cannot dispatch any effect.

These tests prove the runtime-path layer of the no-effects invariant with zero
effect calls. The allowed-operation set is derived from the frozen capability
catalog so read/propose stays allowed and every mutation/dispatch operation is
structurally denied for a scheduled READ_ONLY_AGENT_TURN.
"""

from __future__ import annotations

import json

import pytest

from scheduler.conftest import DELIVERY_TARGET, agent_turn_definition
from scheduler.service import (
    ScheduledEffectDenied,
    assert_scheduled_turn_operation_allowed,
    scheduled_read_only_operations,
)


READ_OR_PROPOSE = {
    "schedule.list",
    "workspace.file.list",
    "workspace.file.read",
    "schedule.propose",
    "schedule.cancel.propose",
}
FORBIDDEN_SCHEDULED_OPERATIONS = {
    # A persisted scheduled prompt is not a current authenticated request and
    # therefore cannot authorize an exact-target network read.
    "web.exact.read",
    "workspace.file.write",
    "workspace.file.delete",
    "compute.run",
}


def test_scheduled_read_only_operation_set_is_exactly_read_and_propose():
    assert scheduled_read_only_operations() == READ_OR_PROPOSE


def test_scheduled_read_only_turn_cannot_dispatch_connector_or_browser_effect():
    # A scheduled turn attempting a dispatch/mutation operation is denied, and
    # the denial itself makes zero effect calls (it is a pure structural check).
    for operation_id in FORBIDDEN_SCHEDULED_OPERATIONS:
        with pytest.raises(ScheduledEffectDenied):
            assert_scheduled_turn_operation_allowed(
                operation_id, external_effects=False
            )

    # Even a read operation is denied if the effect marker is ever flipped on,
    # so a scheduled turn can never carry external-effect authority.
    with pytest.raises(ScheduledEffectDenied):
        assert_scheduled_turn_operation_allowed(
            "schedule.list", external_effects=True
        )
    # An unknown operation id is denied too.
    with pytest.raises(ScheduledEffectDenied):
        assert_scheduled_turn_operation_allowed("gmail.send", external_effects=False)


def test_scheduled_turn_can_read_or_prepare_a_fresh_proposal_only():
    for operation_id in READ_OR_PROPOSE:
        # No exception: read and propose are the only things a scheduled turn
        # can do, and they never dispatch an effect.
        assert_scheduled_turn_operation_allowed(operation_id, external_effects=False)


def test_scheduled_agent_occurrence_body_marks_external_effects_false(service):
    proposal = service.propose(
        user_id="user_a1",
        invocation_id="invocation_12345678",
        task_type="READ_ONLY_AGENT_TURN",
        definition=agent_turn_definition(),
        delivery_target=DELIVERY_TARGET,
    )
    spec = service.confirm(
        user_id="user_a1",
        proposal_ref=proposal.proposal_ref,
        args_hash=proposal.args_hash,
    )
    from scheduler.models import SchedulePayloadV1

    service.fire(
        SchedulePayloadV1(
            schedule_id=spec.schedule_id, generation=1, fire_time=spec.next_run_at
        )
    )
    # The enqueued occurrence carries the read-only markers the worker forwards
    # to the runtime so the turn's grant can only read or prepare a proposal.
    body = json.loads(service._queue.sends[0]["MessageBody"])
    assert body["scheduled"] is True
    assert body["externalEffects"] is False
    assert body["taskType"] == "READ_ONLY_AGENT_TURN"
    assert "prompt" in body
