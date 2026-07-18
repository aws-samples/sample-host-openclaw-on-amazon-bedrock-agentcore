"""RED-first tests for the scheduler value types layered over frozen contracts."""

from __future__ import annotations

import json

import pytest

from scheduler.models import (
    SCHEDULE_PAYLOAD_SCHEMA,
    SchedulePayloadError,
    SchedulePayloadV1,
    build_schedule_spec,
    derive_schedule_id,
    make_occurrence,
)

from capabilities.contracts import (
    ContractValidationError,
    ScheduleOccurrenceV1,
    ScheduleSpecV1,
    derive_occurrence_id,
)

SCHEDULE_ID = derive_schedule_id("user_a1", "nonce0000000001")
FIRE_TIME = 1_800_000_600


def _reminder_definition():
    return {"message": "call mum", "runAt": FIRE_TIME, "timezone": "Europe/London"}


def _agent_definition():
    return {"prompt": "read my inbox", "runAt": FIRE_TIME, "timezone": "Europe/London"}


def test_schedule_payload_carries_only_opaque_id_generation_firetime():
    payload = SchedulePayloadV1(
        schedule_id=SCHEDULE_ID, generation=3, fire_time=FIRE_TIME
    )
    assert payload.to_mapping() == {
        "schema": SCHEDULE_PAYLOAD_SCHEMA,
        "scheduleId": SCHEDULE_ID,
        "generation": 3,
        "fireTime": FIRE_TIME,
    }
    # Round-trips through the exact canonical wire bytes.
    assert SchedulePayloadV1.from_json(payload.to_json()).to_mapping() == (
        payload.to_mapping()
    )

    # Any user content, extra key, or missing key is rejected.
    forbidden = [
        {"message": "hi"},
        {"prompt": "hi"},
        {"userId": "user_a1"},
        {"taskType": "REMINDER"},
        {"definition": {}},
        {"extra": 1},
    ]
    base = payload.to_mapping()
    for extra in forbidden:
        with pytest.raises(SchedulePayloadError):
            SchedulePayloadV1.from_mapping({**base, **extra})
    with pytest.raises(SchedulePayloadError):
        SchedulePayloadV1.from_mapping({"schema": SCHEDULE_PAYLOAD_SCHEMA})


def test_schedule_payload_rejects_duplicate_and_noncanonical_bytes():
    good = SchedulePayloadV1(
        schedule_id=SCHEDULE_ID, generation=1, fire_time=FIRE_TIME
    ).to_json()
    # Duplicate JSON key.
    duplicate = good[:-1] + f',"generation":9}}'
    with pytest.raises(SchedulePayloadError):
        SchedulePayloadV1.from_json(duplicate)
    # Non-finite / constant tokens.
    with pytest.raises(SchedulePayloadError):
        SchedulePayloadV1.from_json(
            json.dumps(
                {
                    "schema": SCHEDULE_PAYLOAD_SCHEMA,
                    "scheduleId": SCHEDULE_ID,
                    "generation": 1,
                    "fireTime": FIRE_TIME,
                }
            ).replace(str(FIRE_TIME), "NaN")
        )
    # Wrong schema discriminator.
    with pytest.raises(SchedulePayloadError):
        SchedulePayloadV1.from_mapping(
            {
                "schema": "personal-operator.telegram-envelope.v1",
                "scheduleId": SCHEDULE_ID,
                "generation": 1,
                "fireTime": FIRE_TIME,
            }
        )
    # generation must be >= 1, fireTime >= 0.
    with pytest.raises(SchedulePayloadError):
        SchedulePayloadV1(schedule_id=SCHEDULE_ID, generation=0, fire_time=FIRE_TIME)
    with pytest.raises(SchedulePayloadError):
        SchedulePayloadV1(schedule_id=SCHEDULE_ID, generation=1, fire_time=-1)
    with pytest.raises(SchedulePayloadError):
        SchedulePayloadV1(schedule_id="short", generation=1, fire_time=FIRE_TIME)


def test_only_reminder_and_read_only_agent_turn_task_types_accepted():
    reminder = build_schedule_spec(
        schedule_id=SCHEDULE_ID,
        user_id="user_a1",
        task_type="REMINDER",
        definition=_reminder_definition(),
        revision=1,
        state="ENABLED",
    )
    assert isinstance(reminder, ScheduleSpecV1)
    assert reminder.task_type == "REMINDER"

    agent = build_schedule_spec(
        schedule_id=SCHEDULE_ID,
        user_id="user_a1",
        task_type="READ_ONLY_AGENT_TURN",
        definition=_agent_definition(),
        revision=1,
        state="ENABLED",
    )
    assert agent.task_type == "READ_ONLY_AGENT_TURN"

    # Any other task type is rejected via the frozen contract enum.
    for bad in ("DISPATCH", "CONNECTOR_TURN", "reminder", "", "READ_WRITE_AGENT_TURN"):
        with pytest.raises(ContractValidationError):
            build_schedule_spec(
                schedule_id=SCHEDULE_ID,
                user_id="user_a1",
                task_type=bad,
                definition=_reminder_definition(),
                revision=1,
                state="ENABLED",
            )


def test_occurrence_id_binds_schedule_generation_and_firetime():
    occurrence = make_occurrence(
        schedule_id=SCHEDULE_ID, generation=2, occurrence_time=FIRE_TIME, status="QUEUED"
    )
    assert isinstance(occurrence, ScheduleOccurrenceV1)
    expected = derive_occurrence_id(SCHEDULE_ID, 2, FIRE_TIME)
    assert occurrence.occurrence_id == expected

    # Deterministic and generation/time bound.
    assert derive_occurrence_id(SCHEDULE_ID, 2, FIRE_TIME) == expected
    assert derive_occurrence_id(SCHEDULE_ID, 3, FIRE_TIME) != expected
    assert derive_occurrence_id(SCHEDULE_ID, 2, FIRE_TIME + 1) != expected

    # A mismatched occurrenceId fails ScheduleOccurrenceV1 validation.
    with pytest.raises(ContractValidationError):
        ScheduleOccurrenceV1.from_mapping(
            {
                "schema": ScheduleOccurrenceV1.SCHEMA,
                "occurrenceId": derive_occurrence_id(SCHEDULE_ID, 3, FIRE_TIME),
                "scheduleId": SCHEDULE_ID,
                "generation": 2,
                "occurrenceTime": FIRE_TIME,
                "status": "QUEUED",
            }
        )


def test_derive_schedule_id_is_opaque_and_leaks_nothing():
    schedule_id = derive_schedule_id("user_a1", "nonce0000000001")
    # Opaque handle: the clear userId never appears in it.
    assert "user_a1" not in schedule_id
    # Deterministic for the same inputs, distinct per user or nonce.
    assert schedule_id == derive_schedule_id("user_a1", "nonce0000000001")
    assert schedule_id != derive_schedule_id("user_a2", "nonce0000000001")
    assert schedule_id != derive_schedule_id("user_a1", "nonce0000000002")
