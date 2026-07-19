"""RED-first tests for the EventBridge target ingress Lambda entry."""

from __future__ import annotations

import pytest

from scheduler.conftest import (
    DELIVERY_TARGET,
    reminder_definition,
)
from scheduler.models import SchedulePayloadV1
from scheduler import ingress


def _confirmed(service):
    proposal = service.propose(
        user_id="user_a1",
        task_type="REMINDER",
        definition=reminder_definition(),
        delivery_target=DELIVERY_TARGET,
    )
    return service.confirm(proposal.proposal_ref)


def test_ingress_delegates_valid_opaque_payload_to_service_fire(
    service, occurrence_queue
):
    spec = _confirmed(service)
    event = SchedulePayloadV1(
        schedule_id=spec.schedule_id, generation=1, fire_time=spec.next_run_at
    ).to_mapping()

    outcome = ingress.handle_fire(event, service)

    assert outcome.status == "ENQUEUED"
    assert len(occurrence_queue.sends) == 1


def test_ingress_rejects_payload_with_user_content_or_extra_keys_and_makes_zero_effect_calls(
    service, occurrence_queue, eventbridge
):
    spec = _confirmed(service)
    base = SchedulePayloadV1(
        schedule_id=spec.schedule_id, generation=1, fire_time=spec.next_run_at
    ).to_mapping()

    poisoned = [
        {**base, "message": "leak"},
        {**base, "prompt": "leak"},
        {**base, "userId": "user_a1"},
        {**base, "taskType": "REMINDER"},
        {**base, "extra": 1},
        {"schema": base["schema"]},
    ]
    before_sends = len(occurrence_queue.sends)
    for event in poisoned:
        with pytest.raises(Exception):
            ingress.handle_fire(event, service)
    # Zero effect calls: nothing enqueued, nothing created.
    assert len(occurrence_queue.sends) == before_sends
    # No new EventBridge schedules and the fire path never dispatched.


def test_ingress_composition_requires_exact_region_and_injects_no_provider_clients(
    monkeypatch
):
    # Wrong region fails closed before any client is created.
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    with pytest.raises(RuntimeError):
        ingress.build_scheduler_service(env={"AWS_REGION": "us-east-1"})

    # The ingress module imports no connector/browser/provider modules.
    import inspect

    source = inspect.getsource(ingress)
    for forbidden in (
        "bedrock-agentcore",
        "browser",
        "connector",
        "gmail",
        "secretsmanager",
        "InvokeAgentRuntime",
    ):
        assert forbidden not in source


def test_ingress_lambda_handler_uses_injected_factory_without_touching_aws(service):
    spec = _confirmed(service)
    ingress.configure_service_factory(lambda: service)
    try:
        event = SchedulePayloadV1(
            schedule_id=spec.schedule_id, generation=1, fire_time=spec.next_run_at
        ).to_mapping()
        result = ingress.lambda_handler(event, None)
        assert result == {"status": "ENQUEUED"}
    finally:
        ingress.configure_service_factory(None)
