"""RED-first state/race tests for the trusted scheduler service."""

from __future__ import annotations

import pytest

from scheduler.conftest import (
    DELIVERY_TARGET,
    ProviderUncertainError,
    agent_turn_definition,
    reminder_definition,
)
from scheduler.models import SchedulePayloadV1, derive_schedule_id
from scheduler.service import (
    SchedulerOutcome,
    SchedulerService,
    SchedulerUncertain,
)


def _propose_confirm(service, *, task_type="REMINDER", definition=None):
    definition = definition or reminder_definition()
    proposal = service.propose(
        user_id="user_a1",
        task_type=task_type,
        definition=definition,
        delivery_target=DELIVERY_TARGET,
    )
    spec = service.confirm(proposal.proposal_ref)
    return proposal, spec


def test_propose_creates_proposal_not_a_live_schedule(
    service, repository, eventbridge
):
    proposal = service.propose(
        user_id="user_a1",
        task_type="REMINDER",
        definition=reminder_definition(),
        delivery_target=DELIVERY_TARGET,
    )

    # A proposal mirrors the gateway proposal shape and creates no schedule.
    assert set(proposal.to_mapping()) == {"proposalRef", "argsHash", "expiresAt"}
    assert eventbridge.created == {}
    assert repository.schedules == {}
    # The proposal is durable and NOT live (no ENABLED schedule yet).
    assert service.list_schedules("user_a1") == []


def test_confirm_enables_at_revision_one_and_creates_one_eventbridge_schedule_with_opaque_payload(
    service, repository, eventbridge
):
    proposal, spec = _propose_confirm(service)

    assert spec.state == "ENABLED"
    assert spec.revision == 1
    assert spec.next_run_at == reminder_definition()["runAt"]

    # Exactly one one-time EventBridge schedule with the opaque payload.
    assert list(eventbridge.created) == [spec.schedule_id]
    created = eventbridge.created[spec.schedule_id]
    assert created["generation"] == 1
    payload = SchedulePayloadV1.from_mapping(created["payload"])
    assert payload.schedule_id == spec.schedule_id
    assert payload.generation == 1
    assert payload.fire_time == reminder_definition()["runAt"]
    # The payload leaks no user content.
    assert "user_a1" not in payload.to_json()
    assert "water" not in payload.to_json()


def test_update_bumps_generation_replaces_schedule_and_stales_old_generation_fires(
    service, repository, eventbridge
):
    proposal, spec = _propose_confirm(service)
    new_definition = reminder_definition(run_at=1_800_009_999)

    updated = service.update(spec.schedule_id, definition=new_definition)

    assert updated.revision == 2
    assert updated.next_run_at == 1_800_009_999
    # Old generation schedule deleted, new-generation schedule created.
    assert spec.schedule_id in eventbridge.deleted
    assert eventbridge.created[spec.schedule_id]["generation"] == 2

    # An old-generation fire is now stale and enqueues nothing.
    stale = service.fire(
        SchedulePayloadV1(
            schedule_id=spec.schedule_id, generation=1, fire_time=spec.next_run_at
        )
    )
    assert stale.status == "STALE"


def test_pause_clears_next_run_and_deletes_live_schedule(
    service, repository, eventbridge
):
    proposal, spec = _propose_confirm(service)

    paused = service.pause(spec.schedule_id)

    assert paused.state == "PAUSED"
    assert paused.next_run_at is None
    assert spec.schedule_id in eventbridge.deleted


def test_cancel_is_terminal_and_deletes_live_schedule(
    service, repository, eventbridge
):
    proposal, spec = _propose_confirm(service)

    cancelled = service.cancel(spec.schedule_id)

    assert cancelled.state == "CANCELLED"
    assert cancelled.next_run_at is None
    assert spec.schedule_id in eventbridge.deleted
    # Terminal: cannot be re-enabled/updated.
    with pytest.raises(Exception):
        service.update(spec.schedule_id, definition=reminder_definition())


def test_fire_strong_reads_then_enqueues_exactly_one_occurrence_into_per_user_fifo(
    service, repository, eventbridge, occurrence_queue
):
    proposal, spec = _propose_confirm(service)

    result = service.fire(
        SchedulePayloadV1(
            schedule_id=spec.schedule_id, generation=1, fire_time=spec.next_run_at
        )
    )

    assert result.status == "ENQUEUED"
    assert "strong_read_schedule" in repository.trace
    assert len(occurrence_queue.sends) == 1
    send = occurrence_queue.sends[0]
    assert send["MessageGroupId"] == "user_a1"
    from capabilities.contracts import derive_occurrence_id

    expected_occ = derive_occurrence_id(spec.schedule_id, 1, spec.next_run_at)
    assert send["MessageDeduplicationId"] == expected_occ


def test_duplicate_fire_same_generation_and_time_is_idempotent_noop(
    service, repository, eventbridge, occurrence_queue
):
    proposal, spec = _propose_confirm(service)
    payload = SchedulePayloadV1(
        schedule_id=spec.schedule_id, generation=1, fire_time=spec.next_run_at
    )

    first = service.fire(payload)
    second = service.fire(payload)

    assert first.status == "ENQUEUED"
    assert second.status == "DUPLICATE"
    # Only one occurrence enqueued despite two fires.
    assert len(occurrence_queue.sends) == 1


def test_stale_generation_fire_is_noop_and_enqueues_nothing(
    service, repository, eventbridge, occurrence_queue
):
    proposal, spec = _propose_confirm(service)
    service.update(spec.schedule_id, definition=reminder_definition(run_at=1_800_001_111))

    result = service.fire(
        SchedulePayloadV1(
            schedule_id=spec.schedule_id, generation=1, fire_time=spec.next_run_at
        )
    )

    assert result.status == "STALE"
    assert occurrence_queue.sends == []


def test_fire_after_pause_or_cancel_enqueues_nothing(
    service, repository, eventbridge, occurrence_queue
):
    proposal, spec = _propose_confirm(service)
    service.pause(spec.schedule_id)

    paused_fire = service.fire(
        SchedulePayloadV1(
            schedule_id=spec.schedule_id, generation=1, fire_time=spec.next_run_at
        )
    )
    assert paused_fire.status == "STALE"
    assert occurrence_queue.sends == []

    # A cancelled schedule is equally inert.
    proposal2, spec2 = _propose_confirm(service)
    service.cancel(spec2.schedule_id)
    cancelled_fire = service.fire(
        SchedulePayloadV1(
            schedule_id=spec2.schedule_id, generation=1, fire_time=spec2.next_run_at
        )
    )
    assert cancelled_fire.status == "STALE"
    assert occurrence_queue.sends == []


def test_fire_missing_schedule_is_noop(service, occurrence_queue):
    ghost = derive_schedule_id("user_a1", "nonce0000009999")
    result = service.fire(
        SchedulePayloadV1(schedule_id=ghost, generation=1, fire_time=1_800_000_600)
    )
    assert result.status == "STALE"
    assert occurrence_queue.sends == []


def test_eventbridge_uncertain_persistence_on_confirm_is_UNCERTAIN_and_never_auto_acts(
    repository, eventbridge, occurrence_queue, clock, nonce_sequence
):
    scheduler = __import__(
        "scheduler.service", fromlist=["SchedulerService"]
    ).SchedulerService
    ebridge_uncertain = type(eventbridge)(create_uncertain=True)
    service = scheduler(
        repository=repository,
        scheduler=ebridge_uncertain,
        queue=occurrence_queue,
        clock=clock,
        nonce_factory=nonce_sequence,
        uncertain_errors=(ProviderUncertainError,),
    )
    proposal = service.propose(
        user_id="user_a1",
        task_type="REMINDER",
        definition=reminder_definition(),
        delivery_target=DELIVERY_TARGET,
    )

    with pytest.raises(SchedulerUncertain):
        service.confirm(proposal.proposal_ref)
    # No auto-retry, no auto-act: nothing was created, nothing enqueued.
    assert ebridge_uncertain.created == {}
    assert occurrence_queue.sends == []


def test_sqs_uncertain_persistence_on_fire_is_UNCERTAIN_and_never_auto_acts(
    repository, eventbridge, clock, nonce_sequence
):
    from scheduler.conftest import FakeOccurrenceQueue

    queue_uncertain = FakeOccurrenceQueue(uncertain=True)
    service = SchedulerService(
        repository=repository,
        scheduler=eventbridge,
        queue=queue_uncertain,
        clock=clock,
        nonce_factory=nonce_sequence,
        uncertain_errors=(ProviderUncertainError,),
    )
    proposal = service.propose(
        user_id="user_a1",
        task_type="REMINDER",
        definition=reminder_definition(),
        delivery_target=DELIVERY_TARGET,
    )
    spec = service.confirm(proposal.proposal_ref)

    with pytest.raises(SchedulerUncertain):
        service.fire(
            SchedulePayloadV1(
                schedule_id=spec.schedule_id, generation=1, fire_time=spec.next_run_at
            )
        )
    assert queue_uncertain.sends == []


def test_deletion_fence_deletes_live_schedules_before_completion(
    service, repository, eventbridge
):
    _propose_confirm(service)
    proposal2, spec2 = _propose_confirm(service)

    assert len(service.list_enabled_schedules("user_a1")) == 2

    # The deletion hook must delete every live schedule and report clean only
    # once none remain ENABLED.
    remaining = service.purge_user_schedules("user_a1")
    assert remaining == 0
    assert service.list_enabled_schedules("user_a1") == []
    # Each live EventBridge schedule was deleted.
    assert spec2.schedule_id in eventbridge.deleted


def test_import_creates_schedules_disabled_with_no_eventbridge_schedule(
    service, repository, eventbridge
):
    imported = service.import_schedule(
        user_id="user_a1",
        task_type="READ_ONLY_AGENT_TURN",
        definition=agent_turn_definition(),
        delivery_target=DELIVERY_TARGET,
    )

    assert imported.state == "PAUSED"
    assert imported.next_run_at is None
    # No EventBridge schedule created on import.
    assert eventbridge.created == {}
    assert imported.schedule_id not in eventbridge.deleted


def test_scheduler_outcome_is_a_typed_status_value():
    outcome = SchedulerOutcome(status="ENQUEUED")
    assert outcome.status == "ENQUEUED"
    with pytest.raises(Exception):
        SchedulerOutcome(status="NOT_A_STATUS")
