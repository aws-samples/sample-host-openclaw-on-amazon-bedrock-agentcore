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
        self.trace: list[str] = []

    # --- proposals -----------------------------------------------------
    def put_proposal(self, proposal_ref: str, record: Mapping[str, Any]) -> None:
        self.proposals[proposal_ref] = dict(record)

    def read_proposal(self, proposal_ref: str) -> Mapping[str, Any] | None:
        self.trace.append("read_proposal")
        record = self.proposals.get(proposal_ref)
        return dict(record) if record is not None else None

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
    def put_occurrence_if_absent(self, occurrence) -> bool:
        self.trace.append("put_occurrence_if_absent")
        if occurrence.occurrence_id in self.occurrences:
            return False
        self.occurrences.add(occurrence.occurrence_id)
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
