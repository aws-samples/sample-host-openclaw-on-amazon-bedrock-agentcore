"""Deterministic, aggregate-only cohort reporting."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import json
from typing import Any, ClassVar

from observability.events import MAX_EVENT_COUNT, OperationalEventV1


MAX_PARTICIPANT_COUNT = 10_000


class CohortReportValidationError(ValueError):
    """A report input was not safe, bounded, and already validated."""


def _participant_count(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_PARTICIPANT_COUNT:
        raise CohortReportValidationError(
            "participant count must be a bounded positive aggregate"
        )
    return value


def _event_sort_key(event: OperationalEventV1) -> tuple[str, str, str, str]:
    return (
        event.environment,
        event.component,
        event.operation,
        event.outcome,
    )


@dataclass(frozen=True, slots=True, repr=False)
class CohortReportV1:
    """A deterministic cohort total with no individual participant rows."""

    SCHEMA: ClassVar[str] = "personal-operator.cohort-report.v1"

    participant_count: int
    events: tuple[OperationalEventV1, ...]

    def __post_init__(self) -> None:
        _participant_count(self.participant_count)
        if type(self.events) is not tuple or any(
            type(event) is not OperationalEventV1 for event in self.events
        ):
            raise CohortReportValidationError(
                "report events must be validated OperationalEventV1 values"
            )
        keys = [_event_sort_key(event) for event in self.events]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise CohortReportValidationError(
                "report events must be unique deterministic aggregates"
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "participantCount": self.participant_count,
            "events": [event.to_mapping() for event in self.events],
        }

    def to_canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_mapping(),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")


def build_cohort_report(
    events: Iterable[OperationalEventV1], *, participant_count: int
) -> CohortReportV1:
    """Validate, aggregate, and sort event counts into one closed report."""

    validated_participant_count = _participant_count(participant_count)
    totals: dict[tuple[str, str, str, str], int] = {}
    for event in events:
        if type(event) is not OperationalEventV1:
            raise CohortReportValidationError(
                "report events must be validated OperationalEventV1 values"
            )
        key = _event_sort_key(event)
        total = totals.get(key, 0) + event.count
        if total > MAX_EVENT_COUNT:
            raise CohortReportValidationError(
                "operational event aggregate exceeds the bounded maximum"
            )
        totals[key] = total

    aggregated = tuple(
        OperationalEventV1(
            environment=environment,
            component=component,
            operation=operation,
            outcome=outcome,
            count=totals[(environment, component, operation, outcome)],
        )
        for environment, component, operation, outcome in sorted(totals)
    )
    return CohortReportV1(
        participant_count=validated_participant_count,
        events=aggregated,
    )


__all__ = [
    "MAX_PARTICIPANT_COUNT",
    "CohortReportV1",
    "CohortReportValidationError",
    "build_cohort_report",
]
