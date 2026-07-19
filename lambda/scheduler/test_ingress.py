"""RED-first tests for the EventBridge target ingress Lambda entry."""

from __future__ import annotations

import json

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
        invocation_id="invocation_12345678",
        task_type="REMINDER",
        definition=reminder_definition(),
        delivery_target=DELIVERY_TARGET,
    )
    return service.confirm(
        user_id="user_a1",
        proposal_ref=proposal.proposal_ref,
        args_hash=proposal.args_hash,
    )


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


def test_packaged_production_handler_strong_reads_and_enqueues_with_injected_aws_clients(
    service, monkeypatch
):
    spec = _confirmed(service)
    event = SchedulePayloadV1(
        schedule_id=spec.schedule_id,
        generation=spec.revision,
        fire_time=spec.next_run_at,
    ).to_mapping()

    class Dynamo:
        def __init__(self):
            self.get_calls = []
            self.put_calls = []

        def get_item(self, **kwargs):
            self.get_calls.append(kwargs)
            return {
                "Item": {
                    "PK": {"S": f"SCHEDULE#{spec.schedule_id}"},
                    "SK": {"S": "STATE"},
                    "userId": {"S": spec.user_id},
                    "scheduleUserId": {"S": spec.user_id},
                    "scheduleSortKey": {
                        "S": f"SCHEDULE#{spec.schedule_id}"
                    },
                    "recordJson": {
                        "S": json.dumps(
                            spec.to_mapping(),
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    },
                    "deliveryJson": {
                        "S": json.dumps(
                            DELIVERY_TARGET,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    },
                }
            }

        def put_item(self, **kwargs):
            self.put_calls.append(kwargs)
            return {}

    class Sqs:
        def __init__(self):
            self.calls = []

        def send_message(self, **kwargs):
            self.calls.append(kwargs)
            return {"MessageId": "synthetic-message"}

    dynamo = Dynamo()
    sqs = Sqs()
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("SCHEDULER_CONTROL_TABLE_NAME", "scheduler-control")
    monkeypatch.setenv(
        "SCHEDULER_UPDATE_QUEUE_URL",
        "https://sqs.eu-west-1.amazonaws.com/123456789012/update.fifo",
    )
    monkeypatch.setattr(
        ingress,
        "_aws_client",
        lambda service_name: {"dynamodb": dynamo, "sqs": sqs}[service_name],
        raising=False,
    )
    ingress.configure_service_factory(None)

    result = ingress.lambda_handler(event, None)

    assert result == {"status": "ENQUEUED"}
    assert dynamo.get_calls[0]["ConsistentRead"] is True
    assert dynamo.put_calls[0]["ConditionExpression"] == (
        "attribute_not_exists(PK) AND attribute_not_exists(SK)"
    )
    assert len(sqs.calls) == 1
    assert sqs.calls[0]["MessageGroupId"] == spec.user_id
    assert sqs.calls[0]["MessageBody"].find('"externalEffects":false') >= 0
    ingress.configure_service_factory(None)
