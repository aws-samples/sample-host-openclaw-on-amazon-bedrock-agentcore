"""Offline fakes for the trusted scheduler control plane.

Every AWS boundary (control table strong-read, EventBridge Scheduler, and the
per-user FIFO) is an injected in-memory fake. No test creates a client, makes a
network call, or holds provider/connector authority.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

_LAMBDA_ROOT = Path(__file__).resolve().parents[1]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))


class ProviderUncertainError(RuntimeError):
    """A fake provider whose durable persistence outcome is unknown."""


class FakeControlRepository:
    """Strong-read control table: proposals, schedules, and occurrences."""

    def __init__(self) -> None:
        self.proposals: dict[str, dict[str, Any]] = {}
        self.schedules: dict[str, dict[str, Any]] = {}
        self.occurrences: set[str] = set()
        self.completed_occurrences: list[str] = []
        self.occurrence_delivery_states: dict[str, str] = {}
        self.occurrence_statuses: dict[str, str] = {}
        self.trace: list[str] = []

    # --- proposals -----------------------------------------------------
    def put_proposal(self, record) -> None:
        if record.proposal_id in self.proposals:
            raise RuntimeError("proposal already exists")
        self.proposals[record.proposal_id] = {
            "record": record,
            "state": "PENDING",
        }

    def claim_proposal(self, *, user_id, proposal_ref, args_hash, now):
        self.trace.append("claim_proposal")
        row = self.proposals.get(proposal_ref)
        if row is None or row["state"] != "PENDING":
            raise RuntimeError("proposal is unavailable")
        record = row["record"]
        if (
            record.user_id != user_id
            or record.args_hash != args_hash
            or now >= record.expires_at
        ):
            raise RuntimeError("proposal binding is invalid")
        row["state"] = "CLAIMED"
        return record

    # --- schedules -----------------------------------------------------
    def commit_schedule(self, spec, delivery_target, *, expect_absent: bool) -> None:
        if expect_absent and spec.schedule_id in self.schedules:
            raise RuntimeError("schedule already exists")
        self.schedules[spec.schedule_id] = {
            "spec": spec,
            "deliveryTarget": dict(delivery_target),
        }

    def replace_schedule(self, spec, *, expect_revision: int) -> None:
        current = self.schedules.get(spec.schedule_id)
        if current is None:
            raise RuntimeError("schedule does not exist")
        if current["spec"].revision != expect_revision:
            raise RuntimeError("schedule revision fence lost")
        current["spec"] = spec

    def strong_read_schedule(self, schedule_id: str):
        self.trace.append("strong_read_schedule")
        record = self.schedules.get(schedule_id)
        return None if record is None else record["spec"]

    def read_delivery_target(self, schedule_id: str) -> Mapping[str, Any] | None:
        record = self.schedules.get(schedule_id)
        return None if record is None else dict(record["deliveryTarget"])

    def list_schedules(self, user_id: str) -> list:
        return [
            record["spec"]
            for record in self.schedules.values()
            if record["spec"].user_id == user_id
        ]

    def list_enabled_schedules(self, user_id: str) -> list:
        return [
            spec
            for spec in self.list_schedules(user_id)
            if spec.state == "ENABLED"
        ]

    # --- occurrences ---------------------------------------------------
    def put_occurrence_if_absent(
        self, spec, occurrence, delivery_target
    ) -> bool:
        self.trace.append("put_occurrence_if_absent")
        current = self.schedules.get(spec.schedule_id)
        if (
            current is None
            or current["spec"] != spec
            or current["deliveryTarget"] != dict(delivery_target)
        ):
            return False
        if occurrence.occurrence_id in self.occurrences:
            return False
        self.occurrences.add(occurrence.occurrence_id)
        self.occurrence_delivery_states[occurrence.occurrence_id] = "PENDING"
        self.occurrence_statuses[occurrence.occurrence_id] = occurrence.status
        return True

    def complete_occurrence(self, spec, occurrence, *, now, delivery_state):
        from scheduler.models import build_schedule_spec

        self.trace.append("complete_occurrence")
        current = self.schedules.get(spec.schedule_id)
        if (
            current is None
            or current["spec"] != spec
            or self.occurrence_delivery_states.get(occurrence.occurrence_id)
            != "PENDING"
            or delivery_state not in {"ENQUEUED", "UNCERTAIN"}
        ):
            return False
        current["spec"] = build_schedule_spec(
            schedule_id=spec.schedule_id,
            user_id=spec.user_id,
            task_type=spec.task_type,
            definition=spec.definition,
            revision=spec.revision + 1,
            state="PAUSED",
            next_run_at=None,
        )
        self.completed_occurrences.append(occurrence.occurrence_id)
        self.occurrence_delivery_states[occurrence.occurrence_id] = delivery_state
        self.occurrence_statuses[occurrence.occurrence_id] = (
            "QUEUED" if delivery_state == "ENQUEUED" else "FAILED"
        )
        return True


class FakeEventBridgeScheduler:
    """Records one-time schedule creation and deletion; never calls AWS."""

    def __init__(self, *, create_uncertain=False, delete_uncertain=False) -> None:
        self.created: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []
        self.create_uncertain = create_uncertain
        self.delete_uncertain = delete_uncertain

    def create_one_time_schedule(self, *, schedule_id, generation, payload) -> None:
        if self.create_uncertain:
            raise ProviderUncertainError("eventbridge create persistence uncertain")
        self.created[schedule_id] = {
            "generation": generation,
            "payload": payload.to_mapping(),
        }

    def delete_schedule(self, schedule_id: str) -> None:
        if self.delete_uncertain:
            raise ProviderUncertainError("eventbridge delete persistence uncertain")
        self.deleted.append(schedule_id)
        self.created.pop(schedule_id, None)


class FakeOccurrenceQueue:
    """Records FIFO sends; never calls AWS."""

    def __init__(self, *, uncertain=False) -> None:
        self.sends: list[dict[str, Any]] = []
        self.uncertain = uncertain

    def send_occurrence(self, *, message_group_id, message_deduplication_id, body) -> None:
        if self.uncertain:
            raise ProviderUncertainError("sqs send persistence uncertain")
        self.sends.append(
            {
                "MessageGroupId": message_group_id,
                "MessageDeduplicationId": message_deduplication_id,
                "MessageBody": body,
            }
        )


@pytest.fixture
def nonce_sequence():
    values = iter(f"nonce{i:016d}" for i in range(1, 1000))

    def factory() -> str:
        return next(values)

    return factory


@pytest.fixture
def clock():
    state = {"now": 1_800_000_000}

    def now() -> int:
        return state["now"]

    now.state = state  # type: ignore[attr-defined]
    return now


@pytest.fixture
def repository():
    return FakeControlRepository()


@pytest.fixture
def eventbridge():
    return FakeEventBridgeScheduler()


@pytest.fixture
def occurrence_queue():
    return FakeOccurrenceQueue()


@pytest.fixture
def service(repository, eventbridge, occurrence_queue, clock, nonce_sequence):
    from scheduler.service import SchedulerService

    return SchedulerService(
        repository=repository,
        scheduler=eventbridge,
        queue=occurrence_queue,
        clock=clock,
        nonce_factory=nonce_sequence,
        catalog_digest="a" * 64,
        uncertain_errors=(ProviderUncertainError,),
    )


DELIVERY_TARGET = {"chatId": "42", "actorId": "telegram:42"}


def reminder_definition(*, run_at=1_800_000_600):
    return {
        "message": "water the plants",
        "runAt": run_at,
        "timezone": "Europe/London",
    }


def agent_turn_definition(*, run_at=1_800_000_600):
    return {
        "prompt": "summarize my unread newsletters",
        "runAt": run_at,
        "timezone": "Europe/London",
    }
