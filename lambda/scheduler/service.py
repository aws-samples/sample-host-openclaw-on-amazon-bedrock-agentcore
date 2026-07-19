"""Trusted scheduler state machine and the fire -> strong-read -> enqueue path.

Networkless: every AWS dependency (EventBridge Scheduler, the DynamoDB control
table, and the per-user FIFO) is an injected Protocol implementation. The
service holds no connector, browser, or provider authority; a fired schedule
can only enqueue a deterministic occurrence that the worker later processes as
a read-only turn or a reminder notification.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence

try:  # package import
    from capabilities.contracts import (
        ScheduleOccurrenceV1,
        ScheduleSpecV1,
        canonical_json_bytes,
        canonical_sha256,
        derive_occurrence_id,
    )
    from scheduler.models import (
        SchedulePayloadError,
        SchedulePayloadV1,
        build_schedule_spec,
        derive_schedule_id,
        make_occurrence,
    )
except ImportError:  # direct Lambda asset / focused tests
    from contracts import (  # type: ignore[no-redef]
        ScheduleOccurrenceV1,
        ScheduleSpecV1,
        canonical_json_bytes,
        canonical_sha256,
        derive_occurrence_id,
    )
    from models import (  # type: ignore[no-redef]
        SchedulePayloadError,
        SchedulePayloadV1,
        build_schedule_spec,
        derive_schedule_id,
        make_occurrence,
    )


PROPOSAL_TTL_SECONDS = 15 * 60
_OUTCOME_STATUSES = frozenset({"ENQUEUED", "DUPLICATE", "STALE"})
OCCURRENCE_BODY_SCHEMA = "personal-operator.schedule-occurrence-body.v1"


class SchedulerError(RuntimeError):
    """A scheduler control-plane operation cannot proceed."""


class SchedulerUncertain(RuntimeError):
    """A provider persistence outcome is uncertain; never auto-retry or act."""


class ScheduledEffectDenied(RuntimeError):
    """A scheduled turn attempted an operation outside read/propose authority."""


# The load-bearing runtime-path invariant. A scheduled READ_ONLY_AGENT_TURN may
# only reach operations that are pure reads (approval NONE / target-grant read)
# or proposal-only (EXACT_ONE_TIME_PROPOSAL, which prepares but never
# dispatches). Every dispatch/mutation class operation is structurally excluded,
# and the networkless compute boundary is out of the scheduled surface entirely.
_SCHEDULED_READ_APPROVAL_MODES = frozenset(
    {"NONE", "CURRENT_REQUEST_TARGET_GRANT"}
)
_SCHEDULED_PROPOSAL_APPROVAL_MODE = "EXACT_ONE_TIME_PROPOSAL"
# Mutations that persist a durable effect are excluded even though a read may be
# approval-mode NONE, because a scheduled turn must never mutate state.
_SCHEDULED_EXCLUDED_RISK_CLASSES = frozenset(
    {"LOCAL_MUTATION", "DURABLE_MUTATION", "EXTERNAL_EFFECT", "IRREVERSIBLE_EFFECT"}
)
# Compute is a Task 8 boundary, not part of the scheduler's read-only surface.
_SCHEDULED_EXCLUDED_CREDENTIAL_BOUNDARIES = frozenset({"NETWORKLESS_COMPUTE"})


def _catalog_operation_metadata() -> Mapping[str, Mapping[str, Any]]:
    try:
        from capabilities.contracts import FROZEN_CATALOG_PACKS_V1
    except ImportError:  # direct Lambda asset / focused tests
        from contracts import FROZEN_CATALOG_PACKS_V1  # type: ignore[no-redef]
    metadata: dict[str, Mapping[str, Any]] = {}
    for pack in FROZEN_CATALOG_PACKS_V1:
        operation = pack["operations"][0]
        metadata[operation["operationId"]] = {
            "approvalMode": pack["approvalPolicy"]["mode"],
            "riskClass": pack["riskClass"],
            "credentialBoundary": pack["credentialBoundary"],
        }
    return metadata


def scheduled_read_only_operations() -> set[str]:
    """The exact set of operations a scheduled turn may reach (read/propose)."""

    allowed: set[str] = set()
    for operation_id, meta in _catalog_operation_metadata().items():
        if meta["credentialBoundary"] in _SCHEDULED_EXCLUDED_CREDENTIAL_BOUNDARIES:
            continue
        is_proposal = meta["approvalMode"] == _SCHEDULED_PROPOSAL_APPROVAL_MODE
        is_read = (
            meta["approvalMode"] in _SCHEDULED_READ_APPROVAL_MODES
            and meta["riskClass"] not in _SCHEDULED_EXCLUDED_RISK_CLASSES
        )
        if is_proposal or is_read:
            allowed.add(operation_id)
    return allowed


def assert_scheduled_turn_operation_allowed(
    operation_id: str, *, external_effects: bool
) -> None:
    """Deny any scheduled-turn operation outside the read/propose surface.

    A scheduled turn must never carry external-effect authority, so a truthy
    ``external_effects`` marker denies unconditionally. This is a pure
    structural check that makes zero effect calls.
    """

    if external_effects:
        raise ScheduledEffectDenied(
            "a scheduled turn can never carry external-effect authority"
        )
    if operation_id not in scheduled_read_only_operations():
        raise ScheduledEffectDenied(
            "a scheduled turn can only read or prepare a fresh proposal"
        )


@dataclass(frozen=True, slots=True)
class SchedulerProposal:
    proposal_ref: str
    args_hash: str
    expires_at: int

    def to_mapping(self) -> dict[str, Any]:
        return {
            "proposalRef": self.proposal_ref,
            "argsHash": self.args_hash,
            "expiresAt": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class SchedulerOutcome:
    status: str

    def __post_init__(self) -> None:
        if self.status not in _OUTCOME_STATUSES:
            raise SchedulerError("scheduler outcome status is unsupported")


class ScheduleControlRepository(Protocol):
    """Strong-read authority control table; never returns cached data."""

    def put_proposal(self, proposal_ref: str, record: Mapping[str, Any]) -> None: ...

    def read_proposal(self, proposal_ref: str) -> Mapping[str, Any] | None: ...

    def commit_schedule(
        self, spec: ScheduleSpecV1, delivery_target: Mapping[str, Any], *, expect_absent: bool
    ) -> None: ...

    def replace_schedule(self, spec: ScheduleSpecV1, *, expect_revision: int) -> None: ...

    def strong_read_schedule(self, schedule_id: str) -> ScheduleSpecV1 | None: ...

    def read_delivery_target(self, schedule_id: str) -> Mapping[str, Any] | None: ...

    def list_schedules(self, user_id: str) -> Sequence[ScheduleSpecV1]: ...

    def list_enabled_schedules(self, user_id: str) -> Sequence[ScheduleSpecV1]: ...

    def put_occurrence_if_absent(self, occurrence: ScheduleOccurrenceV1) -> bool: ...


class EventBridgeScheduler(Protocol):
    def create_one_time_schedule(
        self, *, schedule_id: str, generation: int, payload: SchedulePayloadV1
    ) -> None: ...

    def delete_schedule(self, schedule_id: str) -> None: ...


class OccurrenceQueue(Protocol):
    def send_occurrence(
        self, *, message_group_id: str, message_deduplication_id: str, body: str
    ) -> None: ...


class SchedulerService:
    def __init__(
        self,
        *,
        repository: ScheduleControlRepository,
        scheduler: EventBridgeScheduler,
        queue: OccurrenceQueue,
        clock: Callable[[], int],
        nonce_factory: Callable[[], str],
        uncertain_errors: tuple[type[BaseException], ...] = (),
    ) -> None:
        if not callable(clock):
            raise TypeError("scheduler clock must be callable")
        if not callable(nonce_factory):
            raise TypeError("scheduler nonce factory must be callable")
        self._repository = repository
        self._scheduler = scheduler
        self._queue = queue
        self._clock = clock
        self._nonce_factory = nonce_factory
        self._uncertain = tuple(uncertain_errors)

    # --- helpers -------------------------------------------------------
    def _now(self) -> int:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SchedulerError("scheduler clock is invalid")
        return value

    def _proposal_ref(self, user_id: str, args_hash: str, nonce: str) -> str:
        digest = hashlib.sha256(b"personal-operator.schedule-proposal.v1\0")
        for component in (user_id, args_hash, nonce):
            digest.update(component.encode("utf-8"))
            digest.update(b"\0")
        return f"prop_{digest.hexdigest()}"

    def _delivery_target(self, delivery_target: Mapping[str, Any]) -> dict[str, Any]:
        if (
            not isinstance(delivery_target, Mapping)
            or set(delivery_target) != {"chatId", "actorId"}
            or not isinstance(delivery_target.get("chatId"), str)
            or not isinstance(delivery_target.get("actorId"), str)
        ):
            raise SchedulerError("schedule delivery target is invalid")
        return {
            "chatId": delivery_target["chatId"],
            "actorId": delivery_target["actorId"],
        }

    def _require_schedule(self, schedule_id: str) -> ScheduleSpecV1:
        spec = self._repository.strong_read_schedule(schedule_id)
        if spec is None:
            raise SchedulerError("schedule does not exist")
        return spec

    def _create_live_schedule(self, spec: ScheduleSpecV1) -> None:
        payload = SchedulePayloadV1(
            schedule_id=spec.schedule_id,
            generation=spec.revision,
            fire_time=spec.definition["runAt"],
        )
        try:
            self._scheduler.create_one_time_schedule(
                schedule_id=spec.schedule_id,
                generation=spec.revision,
                payload=payload,
            )
        except self._uncertain as error:
            raise SchedulerUncertain(
                "EventBridge schedule creation is uncertain"
            ) from error

    def _delete_live_schedule(self, schedule_id: str) -> None:
        try:
            self._scheduler.delete_schedule(schedule_id)
        except self._uncertain as error:
            raise SchedulerUncertain(
                "EventBridge schedule deletion is uncertain"
            ) from error

    # --- control plane -------------------------------------------------
    def propose(
        self,
        *,
        user_id: str,
        task_type: str,
        definition: Mapping[str, Any],
        delivery_target: Mapping[str, Any],
    ) -> SchedulerProposal:
        """Durably record a proposal. NEVER creates an EventBridge schedule."""

        delivery = self._delivery_target(delivery_target)
        # Validate the definition/type through the frozen contract by building a
        # provisional (never-live) spec identity.
        schedule_id = derive_schedule_id(user_id, self._nonce_factory())
        spec = build_schedule_spec(
            schedule_id=schedule_id,
            user_id=user_id,
            task_type=task_type,
            definition=definition,
            revision=1,
            state="PAUSED",
            next_run_at=None,
        )
        args_hash = canonical_sha256(
            {
                "userId": user_id,
                "taskType": task_type,
                "definition": dict(definition),
                "deliveryTarget": delivery,
            }
        )
        now = self._now()
        proposal_ref = self._proposal_ref(user_id, args_hash, self._nonce_factory())
        self._repository.put_proposal(
            proposal_ref,
            {
                "proposalRef": proposal_ref,
                "scheduleId": schedule_id,
                "userId": user_id,
                "taskType": task_type,
                "definition": dict(definition),
                "definitionHash": spec.definition_hash,
                "deliveryTarget": delivery,
                "argsHash": args_hash,
                "expiresAt": now + PROPOSAL_TTL_SECONDS,
            },
        )
        return SchedulerProposal(
            proposal_ref=proposal_ref,
            args_hash=args_hash,
            expires_at=now + PROPOSAL_TTL_SECONDS,
        )

    def confirm(self, proposal_ref: str) -> ScheduleSpecV1:
        """Trusted control-plane transition to ENABLED at revision 1."""

        record = self._repository.read_proposal(proposal_ref)
        if not isinstance(record, Mapping):
            raise SchedulerError("proposal does not exist")
        now = self._now()
        expires_at = record.get("expiresAt")
        if not isinstance(expires_at, int) or expires_at < now:
            raise SchedulerError("proposal has expired")
        spec = build_schedule_spec(
            schedule_id=record["scheduleId"],
            user_id=record["userId"],
            task_type=record["taskType"],
            definition=record["definition"],
            revision=1,
            state="ENABLED",
        )
        delivery = self._delivery_target(record["deliveryTarget"])
        self._repository.commit_schedule(spec, delivery, expect_absent=True)
        self._create_live_schedule(spec)
        return spec

    def update(
        self, schedule_id: str, *, definition: Mapping[str, Any]
    ) -> ScheduleSpecV1:
        """Bump generation, rebuild the definition hash, and replace the schedule."""

        current = self._require_schedule(schedule_id)
        if current.state == "CANCELLED":
            raise SchedulerError("a cancelled schedule is terminal")
        new_spec = build_schedule_spec(
            schedule_id=schedule_id,
            user_id=current.user_id,
            task_type=current.task_type,
            definition=definition,
            revision=current.revision + 1,
            state="ENABLED",
        )
        self._repository.replace_schedule(new_spec, expect_revision=current.revision)
        # Old-generation fires become stale. Delete the old EventBridge schedule
        # and create the new-generation one-time schedule.
        self._delete_live_schedule(schedule_id)
        self._create_live_schedule(new_spec)
        return new_spec

    def pause(self, schedule_id: str) -> ScheduleSpecV1:
        current = self._require_schedule(schedule_id)
        if current.state == "CANCELLED":
            raise SchedulerError("a cancelled schedule is terminal")
        new_spec = build_schedule_spec(
            schedule_id=schedule_id,
            user_id=current.user_id,
            task_type=current.task_type,
            definition=current.definition,
            revision=current.revision + 1,
            state="PAUSED",
            next_run_at=None,
        )
        self._repository.replace_schedule(new_spec, expect_revision=current.revision)
        self._delete_live_schedule(schedule_id)
        return new_spec

    def cancel(self, schedule_id: str) -> ScheduleSpecV1:
        current = self._require_schedule(schedule_id)
        if current.state == "CANCELLED":
            return current
        new_spec = build_schedule_spec(
            schedule_id=schedule_id,
            user_id=current.user_id,
            task_type=current.task_type,
            definition=current.definition,
            revision=current.revision + 1,
            state="CANCELLED",
            next_run_at=None,
        )
        self._repository.replace_schedule(new_spec, expect_revision=current.revision)
        self._delete_live_schedule(schedule_id)
        return new_spec

    def import_schedule(
        self,
        *,
        user_id: str,
        task_type: str,
        definition: Mapping[str, Any],
        delivery_target: Mapping[str, Any],
    ) -> ScheduleSpecV1:
        """Import portable state DISABLED: PAUSED, nextRunAt=None, no schedule."""

        delivery = self._delivery_target(delivery_target)
        schedule_id = derive_schedule_id(user_id, self._nonce_factory())
        spec = build_schedule_spec(
            schedule_id=schedule_id,
            user_id=user_id,
            task_type=task_type,
            definition=definition,
            revision=1,
            state="PAUSED",
            next_run_at=None,
        )
        self._repository.commit_schedule(spec, delivery, expect_absent=True)
        # Deliberately NO EventBridge schedule for imported (disabled) state.
        return spec

    # --- fire path -----------------------------------------------------
    def fire(self, payload: SchedulePayloadV1) -> SchedulerOutcome:
        """Strong-read, generation-fence, then enqueue exactly one occurrence."""

        if not isinstance(payload, SchedulePayloadV1):
            raise SchedulePayloadError("fire requires a validated payload")
        spec = self._repository.strong_read_schedule(payload.schedule_id)
        # Generation / staleness fence. Missing, not-enabled, or a superseded
        # generation is a durable no-op that enqueues nothing.
        if (
            spec is None
            or spec.state != "ENABLED"
            or spec.revision != payload.generation
            or spec.next_run_at != payload.fire_time
        ):
            return SchedulerOutcome(status="STALE")

        occurrence = make_occurrence(
            schedule_id=payload.schedule_id,
            generation=payload.generation,
            occurrence_time=payload.fire_time,
            status="QUEUED",
        )
        # Idempotent conditional-put: a duplicate fire at the same
        # generation+time is a no-op before any enqueue.
        if not self._repository.put_occurrence_if_absent(occurrence):
            return SchedulerOutcome(status="DUPLICATE")

        delivery = self._repository.read_delivery_target(payload.schedule_id)
        if not isinstance(delivery, Mapping):
            raise SchedulerError("schedule delivery target is unavailable")
        body = self._occurrence_body(spec, occurrence, delivery)
        try:
            self._queue.send_occurrence(
                message_group_id=spec.user_id,
                message_deduplication_id=occurrence.occurrence_id,
                body=body,
            )
        except self._uncertain as error:
            raise SchedulerUncertain(
                "occurrence enqueue persistence is uncertain"
            ) from error
        return SchedulerOutcome(status="ENQUEUED")

    def _occurrence_body(
        self,
        spec: ScheduleSpecV1,
        occurrence: ScheduleOccurrenceV1,
        delivery: Mapping[str, Any],
    ) -> str:
        """Deterministic occurrence body the worker parses before QueueEnvelope.

        A READ_ONLY_AGENT_TURN carries the load-bearing read-only markers so the
        turn's capability grant can only read or PREPARE a fresh proposal and can
        never dispatch a connector/browser effect. A REMINDER carries only the
        fixed reminder text for one Telegram notification.
        """

        content_field = "message" if spec.task_type == "REMINDER" else "prompt"
        body = {
            "schema": OCCURRENCE_BODY_SCHEMA,
            "occurrenceId": occurrence.occurrence_id,
            "scheduleId": spec.schedule_id,
            "userId": spec.user_id,
            "generation": occurrence.generation,
            "occurrenceTime": occurrence.occurrence_time,
            "taskType": spec.task_type,
            "chatId": delivery["chatId"],
            "actorId": delivery["actorId"],
            content_field: spec.definition[content_field],
            "scheduled": True,
            "externalEffects": False,
        }
        return json.dumps(
            body,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )

    # --- reads / deletion ---------------------------------------------
    def list_schedules(self, user_id: str) -> list[ScheduleSpecV1]:
        return list(self._repository.list_schedules(user_id))

    def list_enabled_schedules(self, user_id: str) -> list[ScheduleSpecV1]:
        return list(self._repository.list_enabled_schedules(user_id))

    def purge_user_schedules(self, user_id: str) -> int:
        """Delete every live schedule; return the count still ENABLED.

        Repeatable/idempotent so the account-deletion path can call it until it
        returns zero before marking deletion complete.
        """

        for spec in self.list_enabled_schedules(user_id):
            self.cancel(spec.schedule_id)
        return len(self.list_enabled_schedules(user_id))


__all__ = [
    "OCCURRENCE_BODY_SCHEMA",
    "EventBridgeScheduler",
    "OccurrenceQueue",
    "ScheduleControlRepository",
    "ScheduledEffectDenied",
    "SchedulerError",
    "SchedulerOutcome",
    "SchedulerProposal",
    "SchedulerService",
    "SchedulerUncertain",
    "assert_scheduled_turn_operation_allowed",
    "scheduled_read_only_operations",
]
