"""Scheduler value types layered over the frozen v1 contracts.

These types add only the generation fence and the opaque EventBridge wire
payload. The schedule/occurrence shapes and their identity derivations are the
frozen contracts (:mod:`capabilities.contracts`); this module never re-parses
or re-defines them so the two authorities cannot drift.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

try:  # package import
    from capabilities.contracts import (
        ScheduleOccurrenceV1,
        ScheduleSpecV1,
        canonical_json_bytes,
        canonical_sha256,
        derive_occurrence_id,
    )
except ImportError:  # direct Lambda asset / focused tests
    from contracts import (  # type: ignore[no-redef]
        ScheduleOccurrenceV1,
        ScheduleSpecV1,
        canonical_json_bytes,
        canonical_sha256,
        derive_occurrence_id,
    )


SCHEDULE_PAYLOAD_SCHEMA = "personal-operator.schedule-fire-payload.v1"
MAX_PAYLOAD_BYTES = 4096
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_NONCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
_MAX_SAFE_INTEGER = 2**53 - 1
_WIRE_FIELDS = ("schema", "scheduleId", "generation", "fireTime")


class SchedulePayloadError(ValueError):
    """The EventBridge -> ingress payload cannot cross the trusted boundary."""


def derive_schedule_id(user_id: str, nonce: str) -> str:
    """Opaque, deterministic schedule handle derived from userId + server nonce.

    The clear userId is never recoverable from the handle; the strong-read of
    the control table recovers it. Pure function like ``derive_occurrence_id``.
    """

    if not isinstance(user_id, str) or _USER_ID.fullmatch(user_id) is None:
        raise SchedulePayloadError("schedule userId is invalid")
    if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
        raise SchedulePayloadError("schedule server nonce is invalid")
    digest = hashlib.sha256(b"personal-operator.schedule-id.v1\0")
    digest.update(user_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(nonce.encode("utf-8"))
    digest.update(b"\0")
    return f"sch_{digest.hexdigest()}"


@dataclass(frozen=True, slots=True)
class SchedulePayloadV1:
    """The opaque EventBridge target payload: no user content, no clear userId."""

    schedule_id: str
    generation: int
    fire_time: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schedule_id, str)
            or _OPAQUE_ID.fullmatch(self.schedule_id) is None
        ):
            raise SchedulePayloadError("payload scheduleId is invalid")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
            or self.generation > _MAX_SAFE_INTEGER
        ):
            raise SchedulePayloadError("payload generation must be an integer >= 1")
        if (
            isinstance(self.fire_time, bool)
            or not isinstance(self.fire_time, int)
            or self.fire_time < 0
            or self.fire_time > _MAX_SAFE_INTEGER
        ):
            raise SchedulePayloadError("payload fireTime must be an integer >= 0")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": SCHEDULE_PAYLOAD_SCHEMA,
            "scheduleId": self.schedule_id,
            "generation": self.generation,
            "fireTime": self.fire_time,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_mapping(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )

    @classmethod
    def from_mapping(cls, value: Any) -> "SchedulePayloadV1":
        if not isinstance(value, Mapping) or set(value) != set(_WIRE_FIELDS):
            raise SchedulePayloadError(
                "payload must carry only schema, scheduleId, generation, fireTime"
            )
        if value.get("schema") != SCHEDULE_PAYLOAD_SCHEMA:
            raise SchedulePayloadError("payload schema discriminator is invalid")
        return cls(
            schedule_id=value["scheduleId"],
            generation=value["generation"],
            fire_time=value["fireTime"],
        )

    @classmethod
    def from_json(cls, body: Any) -> "SchedulePayloadV1":
        if (
            not isinstance(body, str)
            or not body
            or len(body.encode("utf-8")) > MAX_PAYLOAD_BYTES
        ):
            raise SchedulePayloadError("payload body must be bounded UTF-8 JSON text")

        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise SchedulePayloadError("duplicate JSON key")
                result[key] = item
            return result

        try:
            parsed = json.loads(
                body,
                object_pairs_hook=reject_duplicates,
                parse_constant=lambda token: (_ for _ in ()).throw(
                    SchedulePayloadError(token)
                ),
            )
        except SchedulePayloadError:
            raise
        except (TypeError, ValueError) as error:
            raise SchedulePayloadError("payload body is not valid JSON") from error
        return cls.from_mapping(parsed)


def build_schedule_spec(
    *,
    schedule_id: str,
    user_id: str,
    task_type: str,
    definition: Mapping[str, Any],
    revision: int,
    state: str,
    next_run_at: int | None = None,
) -> ScheduleSpecV1:
    """Build one validated ScheduleSpecV1, deriving definitionHash + nextRunAt.

    ``taskType`` acceptance rides entirely on the frozen contract enum so the
    only accepted task types are REMINDER and READ_ONLY_AGENT_TURN.
    """

    if state == "ENABLED" and next_run_at is None and isinstance(definition, Mapping):
        next_run_at = definition.get("runAt")
    timezone = definition.get("timezone") if isinstance(definition, Mapping) else None
    return ScheduleSpecV1.from_mapping(
        {
            "schema": ScheduleSpecV1.SCHEMA,
            "scheduleId": schedule_id,
            "userId": user_id,
            "taskType": task_type,
            "definition": dict(definition) if isinstance(definition, Mapping) else definition,
            "definitionHash": canonical_sha256(definition),
            "revision": revision,
            "state": state,
            "timezone": timezone,
            "nextRunAt": next_run_at,
        }
    )


def make_occurrence(
    *, schedule_id: str, generation: int, occurrence_time: int, status: str
) -> ScheduleOccurrenceV1:
    """Build a deterministic occurrence whose id binds generation + time."""

    return ScheduleOccurrenceV1.from_mapping(
        {
            "schema": ScheduleOccurrenceV1.SCHEMA,
            "occurrenceId": derive_occurrence_id(
                schedule_id, generation, occurrence_time
            ),
            "scheduleId": schedule_id,
            "generation": generation,
            "occurrenceTime": occurrence_time,
            "status": status,
        }
    )


__all__ = [
    "MAX_PAYLOAD_BYTES",
    "SCHEDULE_PAYLOAD_SCHEMA",
    "SchedulePayloadError",
    "SchedulePayloadV1",
    "build_schedule_spec",
    "derive_schedule_id",
    "make_occurrence",
    "canonical_json_bytes",
]
