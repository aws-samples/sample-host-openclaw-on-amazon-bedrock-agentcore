"""Exact one-time schedule approval boundary and production adapters.

The capability gateway can only prepare immutable proposals.  This module is
the separate trusted control-plane consumer that binds a browser-authenticated
user to one proposal, commits the local generation fence before any provider
call, and makes ambiguous provider outcomes durable ``UNCERTAIN`` records.
Ordinary approval never retries an uncertain effect; reconciliation observes
provider state and never dispatches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import time
from typing import Any, Callable, Mapping, Protocol

from botocore.exceptions import ClientError

from capabilities.contracts import (
    CapabilityInstallationV1,
    ContractValidationError,
    ScheduleSpecV1,
    canonical_json_bytes,
)
from capabilities.durable import DynamoAdmissionRepository
from capabilities.retention import (
    DELETION_FENCE_SCHEMA,
    derive_deletion_subject_binding,
    subject_partition_key,
)
from scheduler.models import SchedulePayloadV1, build_schedule_spec
from scheduler.proposals import (
    PHYSICAL_RETENTION_SECONDS,
    ScheduleProposalRecordV1,
)


_USER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PROPOSAL_STATES = frozenset(
    {"PENDING", "APPLYING", "SUCCEEDED", "UNCERTAIN", "REJECTED", "STALE"}
)
_OUTCOME_STATES = frozenset({"SUCCEEDED", "UNCERTAIN", "REJECTED", "STALE"})
_TABLE = re.compile(r"[A-Za-z0-9_.-]{3,255}")
_GROUP = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_INGRESS_ARN = re.compile(
    r"arn:aws:lambda:eu-west-1:[0-9]{12}:function:"
    r"personal-operator-scheduler-ingress"
)
_ROLE_ARN = re.compile(
    r"arn:aws:iam::[0-9]{12}:role/"
    r"personal-operator-scheduler-invoke-eu-west-1"
)
REQUIRED_REGION = "eu-west-1"
_MAX_LIVE_SCHEDULES = 256
_COUNTER_SK = "CONTROL#SCHEDULE_COUNT"


class ScheduleControlError(RuntimeError):
    """An approval request is unavailable, stale, or not exactly bound."""


@dataclass(frozen=True, slots=True)
class ControlOutcome:
    status: str
    proposal_ref: str
    schedule_id: str
    revision: int

    def __post_init__(self) -> None:
        if self.status not in _OUTCOME_STATES:
            raise ValueError("schedule control outcome status is invalid")
        if (
            not isinstance(self.proposal_ref, str)
            or _OPAQUE.fullmatch(self.proposal_ref) is None
            or not isinstance(self.schedule_id, str)
            or _OPAQUE.fullmatch(self.schedule_id) is None
            or isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ValueError("schedule control outcome binding is invalid")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "proposalRef": self.proposal_ref,
            "scheduleId": self.schedule_id,
            "revision": self.revision,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ControlOutcome":
        if not isinstance(value, Mapping) or set(value) != {
            "status",
            "proposalRef",
            "scheduleId",
            "revision",
        }:
            raise ValueError("schedule control outcome shape is invalid")
        return cls(
            status=value["status"],
            proposal_ref=value["proposalRef"],
            schedule_id=value["scheduleId"],
            revision=value["revision"],
        )


@dataclass(frozen=True, slots=True)
class ProposalSnapshot:
    record: ScheduleProposalRecordV1
    state: str
    version: int
    outcome: ControlOutcome | None

    def __post_init__(self) -> None:
        if not isinstance(self.record, ScheduleProposalRecordV1):
            raise TypeError("proposal snapshot requires the shared contract")
        if self.state not in _PROPOSAL_STATES:
            raise ValueError("proposal snapshot state is invalid")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise ValueError("proposal snapshot version is invalid")
        terminal = self.state in _OUTCOME_STATES
        if terminal != isinstance(self.outcome, ControlOutcome):
            raise ValueError("proposal snapshot terminal outcome is invalid")
        if self.outcome is not None and (
            self.outcome.status != self.state
            or self.outcome.proposal_ref != self.record.proposal_id
            or self.outcome.schedule_id != self.record.schedule_id
        ):
            raise ValueError("proposal snapshot outcome crossed its binding")


@dataclass(frozen=True, slots=True)
class ScheduleSnapshot:
    spec: ScheduleSpecV1
    delivery_target: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.spec, ScheduleSpecV1):
            raise TypeError("schedule snapshot requires the frozen contract")
        if (
            not isinstance(self.delivery_target, Mapping)
            or set(self.delivery_target) != {"actorId", "chatId"}
            or not all(isinstance(value, str) for value in self.delivery_target.values())
            or self.delivery_target["actorId"]
            != f"telegram:{self.delivery_target['chatId']}"
        ):
            raise ValueError("schedule snapshot delivery target is invalid")


class ScheduleControlRepository(Protocol):
    def strong_read_proposal(
        self, *, user_id: str, proposal_ref: str
    ) -> ProposalSnapshot | None: ...

    def strong_read_schedule(self, schedule_id: str) -> ScheduleSnapshot | None: ...

    def claim_create(
        self,
        snapshot: ProposalSnapshot,
        spec: ScheduleSpecV1,
        delivery_target: Mapping[str, str],
        *,
        now: int,
    ) -> bool: ...

    def claim_cancel(
        self,
        snapshot: ProposalSnapshot,
        current: ScheduleSnapshot,
        cancelled: ScheduleSpecV1,
        *,
        now: int,
    ) -> bool: ...

    def finish_proposal(
        self,
        snapshot: ProposalSnapshot,
        *,
        status: str,
        outcome: ControlOutcome,
        now: int,
    ) -> bool: ...

    def stale_after_claim(
        self,
        snapshot: ProposalSnapshot,
        claimed: ScheduleSnapshot,
        *,
        outcome: ControlOutcome,
        now: int,
        remove_schedule: bool,
    ) -> bool: ...

    def reject_proposal(
        self,
        snapshot: ProposalSnapshot,
        *,
        outcome: ControlOutcome,
        now: int,
    ) -> bool: ...

    def list_user_schedules(self, user_id: str) -> tuple[ScheduleSnapshot, ...]: ...

    def list_user_schedule_orphans(self, user_id: str) -> tuple[str, ...]: ...

    def delete_orphan_owner(self, *, user_id: str, schedule_id: str) -> bool: ...

    def fence_schedule_for_purge(
        self, current: ScheduleSnapshot, *, now: int
    ) -> ScheduleSnapshot | None: ...

    def delete_schedule_partition(self, current: ScheduleSnapshot) -> bool: ...

    def delete_user_proposals(self, user_id: str) -> bool: ...


class ScheduleProvider(Protocol):
    def create_one_time_schedule(
        self, *, spec: ScheduleSpecV1, payload: SchedulePayloadV1
    ) -> None: ...

    def delete_schedule(self, *, schedule_id: str) -> None: ...

    def observe_schedule(
        self,
        *,
        schedule_id: str,
        expected_payload: SchedulePayloadV1 | None = None,
    ) -> str: ...


class ScheduleControlService:
    """Consume one exact schedule proposal at a trusted approval boundary."""

    def __init__(
        self,
        *,
        repository: ScheduleControlRepository,
        provider: ScheduleProvider,
        catalog_digest: str,
        clock: Callable[[], int],
        authority_guard: Callable[[str, str], None],
        uncertain_errors: tuple[type[BaseException], ...] = (),
    ) -> None:
        if not isinstance(catalog_digest, str) or _SHA256.fullmatch(catalog_digest) is None:
            raise ValueError("schedule control catalog digest is invalid")
        if not callable(clock):
            raise TypeError("schedule control clock is invalid")
        if not callable(authority_guard):
            raise TypeError("schedule control authority guard is invalid")
        if not isinstance(uncertain_errors, tuple) or any(
            not isinstance(error, type) or not issubclass(error, BaseException)
            for error in uncertain_errors
        ):
            raise TypeError("schedule uncertain error classes are invalid")
        self._repository = repository
        self._provider = provider
        self._catalog_digest = catalog_digest
        self._clock = clock
        self._authority_guard = authority_guard
        self._uncertain = uncertain_errors

    def _now(self) -> int:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ScheduleControlError("schedule control clock is invalid")
        return value

    @staticmethod
    def _identities(user_id: str, proposal_ref: str) -> None:
        if not isinstance(user_id, str) or _USER.fullmatch(user_id) is None:
            raise ScheduleControlError("schedule control user is invalid")
        if (
            not isinstance(proposal_ref, str)
            or _OPAQUE.fullmatch(proposal_ref) is None
        ):
            raise ScheduleControlError("schedule proposal identity is invalid")

    def _read(self, *, user_id: str, proposal_ref: str) -> ProposalSnapshot:
        self._identities(user_id, proposal_ref)
        try:
            snapshot = self._repository.strong_read_proposal(
                user_id=user_id, proposal_ref=proposal_ref
            )
        except Exception as error:
            raise ScheduleControlError("schedule proposal strong read failed") from error
        if snapshot is None:
            raise ScheduleControlError("schedule proposal is unavailable")
        if (
            snapshot.record.user_id != user_id
            or snapshot.record.proposal_id != proposal_ref
        ):
            raise ScheduleControlError("schedule proposal crossed its tenant binding")
        return snapshot

    def _validate_request(
        self,
        snapshot: ProposalSnapshot,
        *,
        revision: int,
        args_hash: str,
        now: int,
        require_live: bool,
    ) -> None:
        proposal = snapshot.record.proposal
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or not isinstance(args_hash, str)
            or _SHA256.fullmatch(args_hash) is None
            or proposal.catalog_digest != self._catalog_digest
            or proposal.revision != revision
            or proposal.args_hash != args_hash
            or proposal.approval_policy != "EXACT_ONE_TIME"
            or proposal.operation_id
            not in {"schedule.propose", "schedule.cancel.propose"}
        ):
            raise ScheduleControlError("schedule approval binding is invalid")
        if require_live and now >= proposal.expires_at:
            raise ScheduleControlError("schedule proposal has expired")

    @staticmethod
    def _outcome(
        snapshot: ProposalSnapshot, *, status: str, revision: int
    ) -> ControlOutcome:
        return ControlOutcome(
            status=status,
            proposal_ref=snapshot.record.proposal_id,
            schedule_id=snapshot.record.schedule_id,
            revision=revision,
        )

    def _after_lost_claim(
        self,
        *,
        user_id: str,
        snapshot: ProposalSnapshot,
        revision: int,
        args_hash: str,
    ) -> ControlOutcome:
        current = self._read(
            user_id=user_id, proposal_ref=snapshot.record.proposal_id
        )
        self._validate_request(
            current,
            revision=revision,
            args_hash=args_hash,
            now=self._now(),
            require_live=False,
        )
        if current.outcome is None or current.state not in {"SUCCEEDED", "UNCERTAIN"}:
            raise ScheduleControlError("schedule proposal claim was lost")
        return current.outcome

    def preview(self, *, user_id: str, proposal_ref: str) -> dict[str, Any]:
        snapshot = self._read(user_id=user_id, proposal_ref=proposal_ref)
        record = snapshot.record
        if record.proposal.catalog_digest != self._catalog_digest:
            raise ScheduleControlError("schedule proposal catalog is stale")
        return {
            "proposalRef": record.proposal_id,
            "operationId": record.proposal.operation_id,
            "scheduleId": record.schedule_id,
            "revision": record.proposal.revision,
            "argsHash": record.args_hash,
            "arguments": dict(record.proposal.arguments),
            "expiresAt": record.expires_at,
            "state": snapshot.state,
        }

    def approve(
        self,
        *,
        user_id: str,
        proposal_ref: str,
        revision: int,
        args_hash: str,
    ) -> ControlOutcome:
        snapshot = self._read(user_id=user_id, proposal_ref=proposal_ref)
        now = self._now()
        self._validate_request(
            snapshot,
            revision=revision,
            args_hash=args_hash,
            now=now,
            require_live=snapshot.state == "PENDING",
        )
        if snapshot.state in {"SUCCEEDED", "UNCERTAIN"}:
            assert snapshot.outcome is not None
            return snapshot.outcome
        if snapshot.state != "PENDING":
            raise ScheduleControlError("schedule proposal is not approvable")

        record = snapshot.record
        try:
            self._authority_guard(user_id, record.proposal.operation_id)
        except Exception as error:
            raise ScheduleControlError("schedule live authority is unavailable") from error
        if record.proposal.operation_id == "schedule.propose":
            spec = build_schedule_spec(
                schedule_id=record.schedule_id,
                user_id=user_id,
                task_type=record.proposal.arguments["taskType"],
                definition=record.proposal.arguments["definition"],
                revision=1,
                state="ENABLED",
            )
            if not self._repository.claim_create(
                snapshot, spec, record.delivery_target, now=now
            ):
                return self._after_lost_claim(
                    user_id=user_id,
                    snapshot=snapshot,
                    revision=revision,
                    args_hash=args_hash,
                )
            applied_revision = spec.revision
            claimed = ScheduleSnapshot(
                spec=spec, delivery_target=record.delivery_target
            )
            remove_claimed_schedule = True
            provider_call = lambda: self._provider.create_one_time_schedule(
                spec=spec,
                payload=SchedulePayloadV1(
                    schedule_id=spec.schedule_id,
                    generation=spec.revision,
                    fire_time=spec.next_run_at,
                ),
            )
        else:
            current = self._repository.strong_read_schedule(record.schedule_id)
            if (
                current is None
                or current.spec.user_id != user_id
                or current.spec.revision != record.proposal.revision
                or current.spec.state == "CANCELLED"
            ):
                raise ScheduleControlError("schedule cancellation proposal is stale")
            cancelled = build_schedule_spec(
                schedule_id=current.spec.schedule_id,
                user_id=current.spec.user_id,
                task_type=current.spec.task_type,
                definition=current.spec.definition,
                revision=current.spec.revision + 1,
                state="CANCELLED",
                next_run_at=None,
            )
            if not self._repository.claim_cancel(
                snapshot, current, cancelled, now=now
            ):
                return self._after_lost_claim(
                    user_id=user_id,
                    snapshot=snapshot,
                    revision=revision,
                    args_hash=args_hash,
                )
            applied_revision = cancelled.revision
            claimed = ScheduleSnapshot(
                spec=cancelled, delivery_target=current.delivery_target
            )
            remove_claimed_schedule = False
            provider_call = lambda: self._provider.delete_schedule(
                schedule_id=record.schedule_id
            )

        post_claim_now = now
        try:
            self._authority_guard(user_id, record.proposal.operation_id)
            post_claim_now = self._now()
            if post_claim_now >= record.expires_at:
                raise ScheduleControlError("schedule proposal expired after claim")
        except Exception:
            outcome = self._outcome(
                snapshot, status="STALE", revision=applied_revision
            )
            if not self._repository.stale_after_claim(
                snapshot,
                claimed,
                outcome=outcome,
                now=post_claim_now,
                remove_schedule=remove_claimed_schedule,
            ):
                raise ScheduleControlError(
                    "stale schedule claim could not be durably fenced"
                )
            return outcome

        try:
            provider_call()
        except self._uncertain:
            outcome = self._outcome(
                snapshot, status="UNCERTAIN", revision=applied_revision
            )
            if not self._repository.finish_proposal(
                snapshot, status="UNCERTAIN", outcome=outcome, now=self._now()
            ):
                raise ScheduleControlError(
                    "uncertain schedule effect could not be fenced"
                )
            return outcome

        outcome = self._outcome(
            snapshot, status="SUCCEEDED", revision=applied_revision
        )
        if not self._repository.finish_proposal(
            snapshot, status="SUCCEEDED", outcome=outcome, now=self._now()
        ):
            raise ScheduleControlError("schedule success could not be persisted")
        return outcome

    def reject(
        self,
        *,
        user_id: str,
        proposal_ref: str,
        revision: int,
        args_hash: str,
    ) -> ControlOutcome:
        snapshot = self._read(user_id=user_id, proposal_ref=proposal_ref)
        now = self._now()
        self._validate_request(
            snapshot,
            revision=revision,
            args_hash=args_hash,
            now=now,
            require_live=snapshot.state == "PENDING",
        )
        if snapshot.state == "REJECTED":
            assert snapshot.outcome is not None
            return snapshot.outcome
        if snapshot.state != "PENDING":
            raise ScheduleControlError("schedule proposal is not rejectable")
        outcome = self._outcome(
            snapshot,
            status="REJECTED",
            revision=snapshot.record.proposal.revision,
        )
        if not self._repository.reject_proposal(
            snapshot, outcome=outcome, now=now
        ):
            raise ScheduleControlError("schedule rejection claim was lost")
        return outcome

    def reconcile(self, *, user_id: str, proposal_ref: str) -> ControlOutcome:
        snapshot = self._read(user_id=user_id, proposal_ref=proposal_ref)
        if snapshot.state == "SUCCEEDED":
            assert snapshot.outcome is not None
            return snapshot.outcome
        if snapshot.state not in {"APPLYING", "UNCERTAIN"}:
            raise ScheduleControlError("schedule proposal is not reconcilable")
        if snapshot.state == "UNCERTAIN":
            assert snapshot.outcome is not None
            applied_revision = snapshot.outcome.revision
        else:
            applied_revision = (
                1
                if snapshot.record.proposal.operation_id == "schedule.propose"
                else snapshot.record.proposal.revision + 1
            )
        operation = snapshot.record.proposal.operation_id
        expected_payload = None
        if operation == "schedule.propose":
            expected_spec = build_schedule_spec(
                schedule_id=snapshot.record.schedule_id,
                user_id=snapshot.record.user_id,
                task_type=snapshot.record.proposal.arguments["taskType"],
                definition=snapshot.record.proposal.arguments["definition"],
                revision=1,
                state="ENABLED",
            )
            expected_payload = SchedulePayloadV1(
                schedule_id=expected_spec.schedule_id,
                generation=expected_spec.revision,
                fire_time=expected_spec.next_run_at,
            )
        observation = self._provider.observe_schedule(
            schedule_id=snapshot.record.schedule_id,
            expected_payload=expected_payload,
        )
        positive = (operation == "schedule.propose" and observation == "PRESENT") or (
            operation == "schedule.cancel.propose" and observation == "MISSING"
        )
        if not positive:
            if observation not in {"PRESENT", "MISSING", "UNKNOWN"}:
                raise ScheduleControlError("schedule provider observation is invalid")
            if snapshot.state == "UNCERTAIN":
                assert snapshot.outcome is not None
                return snapshot.outcome
            outcome = self._outcome(
                snapshot, status="UNCERTAIN", revision=applied_revision
            )
            if not self._repository.finish_proposal(
                snapshot, status="UNCERTAIN", outcome=outcome, now=self._now()
            ):
                raise ScheduleControlError("schedule uncertainty fence was lost")
            return outcome
        outcome = self._outcome(
            snapshot,
            status="SUCCEEDED",
            revision=applied_revision,
        )
        if not self._repository.finish_proposal(
            snapshot, status="SUCCEEDED", outcome=outcome, now=self._now()
        ):
            raise ScheduleControlError("schedule reconciliation fence was lost")
        return outcome

    def purge_user_schedules(self, user_id: str) -> int:
        """Fence and remove all schedules/proposals behind account deletion.

        A nonzero result is deliberately retryable by the deletion coordinator;
        no local record is removed until the provider deletion call has returned
        positively.  Exceptions are treated as uncertain and leave the durable
        cancelled generation fence in place.
        """

        if not isinstance(user_id, str) or _USER.fullmatch(user_id) is None:
            raise ScheduleControlError("schedule purge user is invalid")
        try:
            schedules = self._repository.list_user_schedules(user_id)
        except Exception as error:
            raise ScheduleControlError("schedule purge inventory is unavailable") from error
        if not isinstance(schedules, tuple):
            raise ScheduleControlError("schedule purge inventory is invalid")
        remaining = 0
        for current in schedules:
            if (
                not isinstance(current, ScheduleSnapshot)
                or current.spec.user_id != user_id
            ):
                raise ScheduleControlError("schedule purge crossed a tenant boundary")
            fenced = current
            if current.spec.state == "ENABLED":
                fenced = self._repository.fence_schedule_for_purge(
                    current, now=self._now()
                )
                if fenced is None:
                    remaining += 1
                    continue
            try:
                self._provider.delete_schedule(
                    schedule_id=current.spec.schedule_id
                )
            except self._uncertain:
                remaining += 1
                continue
            if not self._repository.delete_schedule_partition(fenced):
                remaining += 1
        try:
            orphans = self._repository.list_user_schedule_orphans(user_id)
        except Exception as error:
            raise ScheduleControlError(
                "schedule orphan inventory is unavailable"
            ) from error
        if (
            not isinstance(orphans, tuple)
            or any(
                not isinstance(schedule_id, str)
                or _OPAQUE.fullmatch(schedule_id) is None
                for schedule_id in orphans
            )
        ):
            raise ScheduleControlError("schedule orphan inventory is invalid")
        for schedule_id in orphans:
            try:
                self._provider.delete_schedule(schedule_id=schedule_id)
            except self._uncertain:
                remaining += 1
                continue
            if not self._repository.delete_orphan_owner(
                user_id=user_id, schedule_id=schedule_id
            ):
                remaining += 1
        if remaining:
            return remaining
        if not self._repository.delete_user_proposals(user_id):
            return 1
        return 0


def _attribute_string(item: Mapping[str, Any], name: str) -> str:
    value = item.get(name)
    if not isinstance(value, Mapping) or set(value) != {"S"}:
        raise RuntimeError("scheduler control record is invalid")
    result = value.get("S")
    if not isinstance(result, str):
        raise RuntimeError("scheduler control record is invalid")
    return result


def _attribute_integer(item: Mapping[str, Any], name: str) -> int:
    value = item.get(name)
    if not isinstance(value, Mapping) or set(value) != {"N"}:
        raise RuntimeError("scheduler control record is invalid")
    raw = value.get("N")
    if not isinstance(raw, str) or re.fullmatch(r"0|[1-9][0-9]*", raw) is None:
        raise RuntimeError("scheduler control record is invalid")
    return int(raw)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _conditional(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") in {
        "ConditionalCheckFailedException",
        "TransactionCanceledException",
    }


class DynamoScheduleControlRepository:
    """Strong-read exact proposal/schedule records and atomic local intents."""

    def __init__(
        self,
        *,
        client: Any,
        table_name: str,
        capability_table_name: str,
    ) -> None:
        if any(
            not callable(getattr(client, method, None))
            for method in (
                "get_item",
                "transact_write_items",
                "update_item",
                "query",
                "batch_write_item",
            )
        ):
            raise TypeError("scheduler control requires exact DynamoDB methods")
        if not isinstance(table_name, str) or _TABLE.fullmatch(table_name) is None:
            raise ValueError("scheduler control table name is invalid")
        if (
            not isinstance(capability_table_name, str)
            or _TABLE.fullmatch(capability_table_name) is None
        ):
            raise ValueError("scheduler capability table name is invalid")
        self._client = client
        self._table_name = table_name
        self._capability_table_name = capability_table_name

    @staticmethod
    def _proposal_key(user_id: str, proposal_ref: str) -> dict[str, Any]:
        return {
            "PK": {"S": f"USER#{user_id}"},
            "SK": {"S": f"PROPOSAL#{proposal_ref}"},
        }

    @staticmethod
    def _schedule_key(schedule_id: str) -> dict[str, Any]:
        return {
            "PK": {"S": f"SCHEDULE#{schedule_id}"},
            "SK": {"S": "STATE"},
        }

    def strong_read_proposal(
        self, *, user_id: str, proposal_ref: str
    ) -> ProposalSnapshot | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=self._proposal_key(user_id, proposal_ref),
            ConsistentRead=True,
        )
        if not isinstance(response, Mapping):
            raise RuntimeError("scheduler proposal strong read is invalid")
        item = response.get("Item")
        if item is None:
            return None
        if not isinstance(item, Mapping):
            raise RuntimeError("scheduler proposal record is invalid")
        state = _attribute_string(item, "state")
        expected_fields = {
            "PK",
            "SK",
            "proposalUserId",
            "proposalSortKey",
            "recordJson",
            "state",
            "version",
            "ttl",
        }
        if state in _OUTCOME_STATES:
            expected_fields.add("outcomeJson")
        if set(item) != expected_fields:
            raise RuntimeError("scheduler proposal record is invalid")
        try:
            record = ScheduleProposalRecordV1.from_mapping(
                json.loads(_attribute_string(item, "recordJson"))
            )
        except (TypeError, ValueError):
            raise RuntimeError("scheduler proposal record is invalid") from None
        if (
            _attribute_string(item, "PK") != f"USER#{user_id}"
            or _attribute_string(item, "SK") != f"PROPOSAL#{proposal_ref}"
            or _attribute_string(item, "proposalUserId") != user_id
            or _attribute_string(item, "proposalSortKey")
            != f"{record.created_at:020d}#{proposal_ref}"
            or record.user_id != user_id
            or record.proposal_id != proposal_ref
            or _attribute_integer(item, "ttl")
            != record.created_at + PHYSICAL_RETENTION_SECONDS
        ):
            raise RuntimeError("scheduler proposal record binding is invalid")
        version = _attribute_integer(item, "version")
        outcome = None
        if state in _OUTCOME_STATES:
            try:
                outcome = ControlOutcome.from_mapping(
                    json.loads(_attribute_string(item, "outcomeJson"))
                )
            except (TypeError, ValueError):
                raise RuntimeError("scheduler proposal outcome is invalid") from None
        try:
            return ProposalSnapshot(
                record=record,
                state=state,
                version=version,
                outcome=outcome,
            )
        except (TypeError, ValueError):
            raise RuntimeError("scheduler proposal record is invalid") from None

    def strong_read_schedule(self, schedule_id: str) -> ScheduleSnapshot | None:
        if not isinstance(schedule_id, str) or _OPAQUE.fullmatch(schedule_id) is None:
            raise ValueError("schedule identity is invalid")
        response = self._client.get_item(
            TableName=self._table_name,
            Key=self._schedule_key(schedule_id),
            ConsistentRead=True,
        )
        if not isinstance(response, Mapping):
            raise RuntimeError("scheduler state strong read is invalid")
        item = response.get("Item")
        if item is None:
            return None
        required = {
            "PK",
            "SK",
            "userId",
            "scheduleUserId",
            "scheduleSortKey",
            "recordJson",
            "deliveryJson",
        }
        if not isinstance(item, Mapping) or set(item) not in (
            required,
            required | {"ttl"},
        ):
            raise RuntimeError("scheduler state record is invalid")
        try:
            spec = ScheduleSpecV1.from_mapping(
                json.loads(_attribute_string(item, "recordJson"))
            )
            delivery = json.loads(_attribute_string(item, "deliveryJson"))
            snapshot = ScheduleSnapshot(spec=spec, delivery_target=delivery)
        except (TypeError, ValueError):
            raise RuntimeError("scheduler state record is invalid") from None
        if (
            _attribute_string(item, "PK") != f"SCHEDULE#{schedule_id}"
            or _attribute_string(item, "SK") != "STATE"
            or _attribute_string(item, "userId") != spec.user_id
            or _attribute_string(item, "scheduleUserId") != spec.user_id
            or _attribute_string(item, "scheduleSortKey")
            != f"SCHEDULE#{schedule_id}"
            or spec.schedule_id != schedule_id
        ):
            raise RuntimeError("scheduler state record binding is invalid")
        if "ttl" in item and (
            spec.state == "ENABLED" or _attribute_integer(item, "ttl") < 1
        ):
            raise RuntimeError("scheduler state retention is invalid")
        return snapshot

    @staticmethod
    def _schedule_item(
        spec: ScheduleSpecV1, delivery_target: Mapping[str, str]
    ) -> dict[str, Any]:
        snapshot = ScheduleSnapshot(spec=spec, delivery_target=delivery_target)
        return {
            "PK": {"S": f"SCHEDULE#{spec.schedule_id}"},
            "SK": {"S": "STATE"},
            "userId": {"S": spec.user_id},
            "scheduleUserId": {"S": spec.user_id},
            "scheduleSortKey": {"S": f"SCHEDULE#{spec.schedule_id}"},
            "recordJson": {"S": _canonical_json(spec.to_mapping())},
            "deliveryJson": {
                "S": _canonical_json(dict(snapshot.delivery_target))
            },
        }

    def _deletion_fence_condition(
        self, user_id: str, *, enabled: bool
    ) -> dict[str, Any]:
        binding = derive_deletion_subject_binding(user_id)
        return {
            "TableName": self._capability_table_name,
            "Key": {
                "PK": {"S": subject_partition_key(user_id)},
                "SK": {"S": "DELETION"},
            },
            "ConditionExpression": "#owner = :owner AND #record = :record",
            "ExpressionAttributeNames": {
                "#owner": "ownerBinding",
                "#record": "recordJson",
            },
            "ExpressionAttributeValues": {
                ":owner": {"S": binding},
                ":record": {
                    "S": _canonical_json(
                        {
                            "schema": DELETION_FENCE_SCHEMA,
                            "enabled": enabled,
                            "subjectBinding": binding,
                        }
                    )
                },
            },
        }

    def _deletion_condition(self, user_id: str) -> dict[str, Any]:
        return self._deletion_fence_condition(user_id, enabled=False)

    def _active_deletion_condition(self, user_id: str) -> dict[str, Any]:
        return self._deletion_fence_condition(user_id, enabled=True)

    @staticmethod
    def _proposal_claim(snapshot: ProposalSnapshot, *, now: int) -> dict[str, Any]:
        lifetime = snapshot.record.expires_at - snapshot.record.created_at
        return {
            "TableName": "",  # filled by the repository instance
            "Key": DynamoScheduleControlRepository._proposal_key(
                snapshot.record.user_id, snapshot.record.proposal_id
            ),
            "UpdateExpression": "SET #state = :applying, #version = :next",
            "ConditionExpression": (
                "#state = :pending AND #version = :expected "
                "AND recordJson = :record AND ttl > :liveAfter"
            ),
            "ExpressionAttributeNames": {
                "#state": "state",
                "#version": "version",
            },
            "ExpressionAttributeValues": {
                ":pending": {"S": "PENDING"},
                ":applying": {"S": "APPLYING"},
                ":expected": {"N": str(snapshot.version)},
                ":next": {"N": str(snapshot.version + 1)},
                ":record": {
                    "S": _canonical_json(snapshot.record.to_mapping())
                },
                ":liveAfter": {
                    "N": str(now + PHYSICAL_RETENTION_SECONDS - lifetime)
                },
            },
        }

    def _counter_increment(self, user_id: str) -> dict[str, Any]:
        return {
            "TableName": self._table_name,
            "Key": {
                "PK": {"S": f"USER#{user_id}"},
                "SK": {"S": _COUNTER_SK},
            },
            "UpdateExpression": (
                "SET liveCount = if_not_exists(liveCount, :zero) + :one, "
                "recordType = if_not_exists(recordType, :counter), "
                "userId = if_not_exists(userId, :user)"
            ),
            "ConditionExpression": (
                "(attribute_not_exists(liveCount) OR liveCount < :max) AND "
                "(attribute_not_exists(recordType) OR recordType = :counter) AND "
                "(attribute_not_exists(userId) OR userId = :user)"
            ),
            "ExpressionAttributeValues": {
                ":zero": {"N": "0"},
                ":one": {"N": "1"},
                ":max": {"N": str(_MAX_LIVE_SCHEDULES)},
                ":counter": {"S": "SCHEDULE_COUNTER"},
                ":user": {"S": user_id},
            },
        }

    def _counter_decrement(self, user_id: str) -> dict[str, Any]:
        return {
            "TableName": self._table_name,
            "Key": {
                "PK": {"S": f"USER#{user_id}"},
                "SK": {"S": _COUNTER_SK},
            },
            "UpdateExpression": "SET liveCount = liveCount - :one",
            "ConditionExpression": (
                "recordType = :counter AND userId = :user "
                "AND liveCount > :zero"
            ),
            "ExpressionAttributeValues": {
                ":one": {"N": "1"},
                ":zero": {"N": "0"},
                ":counter": {"S": "SCHEDULE_COUNTER"},
                ":user": {"S": user_id},
            },
        }

    def claim_create(
        self,
        snapshot: ProposalSnapshot,
        spec: ScheduleSpecV1,
        delivery_target: Mapping[str, str],
        *,
        now: int,
    ) -> bool:
        if snapshot.state != "PENDING" or spec.user_id != snapshot.record.user_id:
            raise ValueError("schedule create claim binding is invalid")
        claim = self._proposal_claim(snapshot, now=now)
        claim["TableName"] = self._table_name
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {"ConditionCheck": self._deletion_condition(spec.user_id)},
                    {"Update": claim},
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": self._schedule_item(spec, delivery_target),
                            "ConditionExpression": (
                                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                            ),
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": {
                                "PK": {"S": f"USER#{spec.user_id}"},
                                "SK": {"S": f"SCHEDULE#{spec.schedule_id}"},
                                "recordType": {"S": "SCHEDULE_OWNER"},
                                "scheduleId": {"S": spec.schedule_id},
                            },
                            "ConditionExpression": (
                                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                            ),
                        }
                    },
                    {"Update": self._counter_increment(spec.user_id)},
                ]
            )
            return True
        except ClientError as error:
            if _conditional(error):
                return False
            raise


    def claim_cancel(
        self,
        snapshot: ProposalSnapshot,
        current: ScheduleSnapshot,
        cancelled: ScheduleSpecV1,
        *,
        now: int,
    ) -> bool:
        if (
            snapshot.state != "PENDING"
            or current.spec.schedule_id != snapshot.record.schedule_id
            or cancelled.schedule_id != current.spec.schedule_id
            or cancelled.user_id != current.spec.user_id
            or cancelled.state != "CANCELLED"
            or cancelled.revision != current.spec.revision + 1
        ):
            raise ValueError("schedule cancellation claim binding is invalid")
        claim = self._proposal_claim(snapshot, now=now)
        claim["TableName"] = self._table_name
        ttl = now + PHYSICAL_RETENTION_SECONDS
        writes = [
            {"ConditionCheck": self._deletion_condition(current.spec.user_id)},
            {"Update": claim},
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": self._schedule_key(current.spec.schedule_id),
                    "UpdateExpression": "SET recordJson = :cancelled, ttl = :ttl",
                    "ConditionExpression": (
                        "recordJson = :current AND userId = :user"
                    ),
                    "ExpressionAttributeValues": {
                        ":current": {
                            "S": _canonical_json(current.spec.to_mapping())
                        },
                        ":cancelled": {
                            "S": _canonical_json(cancelled.to_mapping())
                        },
                        ":ttl": {"N": str(ttl)},
                        ":user": {"S": current.spec.user_id},
                    },
                }
            },
        ]
        if current.spec.state == "ENABLED":
            writes.append({"Update": self._counter_decrement(current.spec.user_id)})
        try:
            self._client.transact_write_items(
                TransactItems=writes
            )
            return True
        except ClientError as error:
            if _conditional(error):
                return False
            raise

    def finish_proposal(
        self,
        snapshot: ProposalSnapshot,
        *,
        status: str,
        outcome: ControlOutcome,
        now: int,
    ) -> bool:
        if status not in {"SUCCEEDED", "UNCERTAIN"} or outcome.status != status:
            raise ValueError("schedule proposal terminal status is invalid")
        expected_state = (
            "UNCERTAIN" if snapshot.state == "UNCERTAIN" else "APPLYING"
        )
        expected_version = (
            snapshot.version + 1
            if snapshot.state == "PENDING"
            else snapshot.version
        )
        return self._terminal_update(
            snapshot,
            expected_state=expected_state,
            expected_version=expected_version,
            status=status,
            outcome=outcome,
        )

    def stale_after_claim(
        self,
        snapshot: ProposalSnapshot,
        claimed: ScheduleSnapshot,
        *,
        outcome: ControlOutcome,
        now: int,
        remove_schedule: bool,
    ) -> bool:
        if (
            snapshot.state != "PENDING"
            or not isinstance(claimed, ScheduleSnapshot)
            or claimed.spec.schedule_id != snapshot.record.schedule_id
            or claimed.spec.user_id != snapshot.record.user_id
            or outcome.status != "STALE"
            or outcome.proposal_ref != snapshot.record.proposal_id
            or outcome.schedule_id != claimed.spec.schedule_id
            or isinstance(now, bool)
            or not isinstance(now, int)
            or now < 0
        ):
            raise ValueError("stale schedule claim binding is invalid")
        if not remove_schedule:
            return self._terminal_update(
                snapshot,
                expected_state="APPLYING",
                expected_version=snapshot.version + 1,
                status="STALE",
                outcome=outcome,
            )
        proposal_update = {
            "TableName": self._table_name,
            "Key": self._proposal_key(
                snapshot.record.user_id, snapshot.record.proposal_id
            ),
            "UpdateExpression": (
                "SET #state = :stale, #version = :next, outcomeJson = :outcome"
            ),
            "ConditionExpression": (
                "#state = :applying AND #version = :expected "
                "AND recordJson = :record"
            ),
            "ExpressionAttributeNames": {
                "#state": "state",
                "#version": "version",
            },
            "ExpressionAttributeValues": {
                ":stale": {"S": "STALE"},
                ":applying": {"S": "APPLYING"},
                ":expected": {"N": str(snapshot.version + 1)},
                ":next": {"N": str(snapshot.version + 2)},
                ":record": {"S": _canonical_json(snapshot.record.to_mapping())},
                ":outcome": {"S": _canonical_json(outcome.to_mapping())},
            },
        }
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {"Update": proposal_update},
                    {
                        "Delete": {
                            "TableName": self._table_name,
                            "Key": self._schedule_key(claimed.spec.schedule_id),
                            "ConditionExpression": (
                                "recordJson = :record AND userId = :user"
                            ),
                            "ExpressionAttributeValues": {
                                ":record": {
                                    "S": _canonical_json(claimed.spec.to_mapping())
                                },
                                ":user": {"S": claimed.spec.user_id},
                            },
                        }
                    },
                    {
                        "Delete": {
                            "TableName": self._table_name,
                            "Key": {
                                "PK": {"S": f"USER#{claimed.spec.user_id}"},
                                "SK": {"S": f"SCHEDULE#{claimed.spec.schedule_id}"},
                            },
                            "ConditionExpression": (
                                "recordType = :owner AND scheduleId = :schedule"
                            ),
                            "ExpressionAttributeValues": {
                                ":owner": {"S": "SCHEDULE_OWNER"},
                                ":schedule": {"S": claimed.spec.schedule_id},
                            },
                        }
                    },
                    {"Update": self._counter_decrement(claimed.spec.user_id)},
                ]
            )
            return True
        except ClientError as error:
            if _conditional(error):
                return False
            raise

    def reject_proposal(
        self,
        snapshot: ProposalSnapshot,
        *,
        outcome: ControlOutcome,
        now: int,
    ) -> bool:
        if snapshot.state != "PENDING" or outcome.status != "REJECTED":
            raise ValueError("schedule proposal rejection is invalid")
        return self._terminal_update(
            snapshot,
            expected_state="PENDING",
            expected_version=snapshot.version,
            status="REJECTED",
            outcome=outcome,
        )

    def _terminal_update(
        self,
        snapshot: ProposalSnapshot,
        *,
        expected_state: str,
        expected_version: int,
        status: str,
        outcome: ControlOutcome,
    ) -> bool:
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key=self._proposal_key(
                    snapshot.record.user_id, snapshot.record.proposal_id
                ),
                UpdateExpression=(
                    "SET #state = :status, #version = :next, outcomeJson = :outcome"
                ),
                ConditionExpression=(
                    "#state = :expectedState AND #version = :expectedVersion "
                    "AND recordJson = :record"
                ),
                ExpressionAttributeNames={
                    "#state": "state",
                    "#version": "version",
                },
                ExpressionAttributeValues={
                    ":status": {"S": status},
                    ":next": {"N": str(expected_version + 1)},
                    ":expectedState": {"S": expected_state},
                    ":expectedVersion": {"N": str(expected_version)},
                    ":record": {
                        "S": _canonical_json(snapshot.record.to_mapping())
                    },
                    ":outcome": {"S": _canonical_json(outcome.to_mapping())},
                },
            )
            return True
        except ClientError as error:
            if _conditional(error):
                return False
            raise

    def _user_schedule_ids(self, user_id: str) -> tuple[str, ...]:
        if not isinstance(user_id, str) or _USER.fullmatch(user_id) is None:
            raise ValueError("schedule inventory user is invalid")
        seen: set[str] = set()
        start_key = None
        seen_pages: set[str] = set()
        while True:
            request = {
                "TableName": self._table_name,
                "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
                "ExpressionAttributeValues": {
                    ":pk": {"S": f"USER#{user_id}"},
                    ":prefix": {"S": "SCHEDULE#"},
                },
                "ProjectionExpression": "PK, SK, recordType, scheduleId",
                "ConsistentRead": True,
                "Limit": 1000,
            }
            if start_key is not None:
                request["ExclusiveStartKey"] = start_key
            response = self._client.query(**request)
            items = response.get("Items") if isinstance(response, Mapping) else None
            if not isinstance(items, list):
                raise RuntimeError("schedule inventory page is invalid")
            for item in items:
                if not isinstance(item, Mapping) or set(item) != {
                    "PK",
                    "SK",
                    "recordType",
                    "scheduleId",
                }:
                    raise RuntimeError("schedule inventory record is invalid")
                pk = _attribute_string(item, "PK")
                sk = _attribute_string(item, "SK")
                schedule_id = _attribute_string(item, "scheduleId")
                if (
                    pk != f"USER#{user_id}"
                    or sk != f"SCHEDULE#{schedule_id}"
                    or _attribute_string(item, "recordType") != "SCHEDULE_OWNER"
                    or _OPAQUE.fullmatch(schedule_id) is None
                ):
                    raise RuntimeError("schedule inventory crossed its binding")
                if schedule_id in seen:
                    raise RuntimeError("schedule inventory contains a duplicate")
                seen.add(schedule_id)
            start_key = response.get("LastEvaluatedKey")
            if start_key is None:
                break
            marker = json.dumps(start_key, sort_keys=True, separators=(",", ":"))
            if marker in seen_pages:
                raise RuntimeError("schedule inventory pagination repeated")
            seen_pages.add(marker)
        return tuple(sorted(seen))

    def list_user_schedules(self, user_id: str) -> tuple[ScheduleSnapshot, ...]:
        snapshots: list[ScheduleSnapshot] = []
        for schedule_id in self._user_schedule_ids(user_id):
            snapshot = self.strong_read_schedule(schedule_id)
            if snapshot is None:
                continue
            if snapshot.spec.user_id != user_id:
                raise RuntimeError("schedule inventory authority changed")
            snapshots.append(snapshot)
        return tuple(sorted(snapshots, key=lambda value: value.spec.schedule_id))

    def list_user_schedule_orphans(self, user_id: str) -> tuple[str, ...]:
        orphans = []
        for schedule_id in self._user_schedule_ids(user_id):
            snapshot = self.strong_read_schedule(schedule_id)
            if snapshot is None:
                orphans.append(schedule_id)
            elif snapshot.spec.user_id != user_id:
                raise RuntimeError("schedule orphan inventory crossed its binding")
        return tuple(orphans)

    def delete_orphan_owner(self, *, user_id: str, schedule_id: str) -> bool:
        if (
            not isinstance(user_id, str)
            or _USER.fullmatch(user_id) is None
            or not isinstance(schedule_id, str)
            or _OPAQUE.fullmatch(schedule_id) is None
        ):
            raise ValueError("schedule orphan binding is invalid")
        pk = f"SCHEDULE#{schedule_id}"
        start_key = None
        seen_pages: set[str] = set()
        while True:
            request = {
                "TableName": self._table_name,
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": {":pk": {"S": pk}},
                "ProjectionExpression": "PK, SK",
                "ConsistentRead": True,
                "Limit": 1000,
            }
            if start_key is not None:
                request["ExclusiveStartKey"] = start_key
            response = self._client.query(**request)
            items = response.get("Items") if isinstance(response, Mapping) else None
            if not isinstance(items, list):
                return False
            occurrence_keys = []
            for item in items:
                if not isinstance(item, Mapping) or set(item) != {"PK", "SK"}:
                    return False
                item_pk = _attribute_string(item, "PK")
                item_sk = _attribute_string(item, "SK")
                if item_pk != pk or not item_sk.startswith("OCCURRENCE#"):
                    return False
                occurrence_keys.append(
                    {"PK": {"S": item_pk}, "SK": {"S": item_sk}}
                )
            if occurrence_keys and not self._delete_keys(occurrence_keys):
                return False
            start_key = response.get("LastEvaluatedKey")
            if start_key is None:
                break
            marker = json.dumps(start_key, sort_keys=True, separators=(",", ":"))
            if marker in seen_pages:
                return False
            seen_pages.add(marker)
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "ConditionCheck": {
                            "TableName": self._table_name,
                            "Key": self._schedule_key(schedule_id),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                    {
                        "Delete": {
                            "TableName": self._table_name,
                            "Key": {
                                "PK": {"S": f"USER#{user_id}"},
                                "SK": {"S": f"SCHEDULE#{schedule_id}"},
                            },
                            "ConditionExpression": (
                                "recordType = :owner AND scheduleId = :schedule"
                            ),
                            "ExpressionAttributeValues": {
                                ":owner": {"S": "SCHEDULE_OWNER"},
                                ":schedule": {"S": schedule_id},
                            },
                        }
                    },
                ]
            )
            return True
        except ClientError as error:
            if _conditional(error):
                return False
            raise

    def fence_schedule_for_purge(
        self, current: ScheduleSnapshot, *, now: int
    ) -> ScheduleSnapshot | None:
        if not isinstance(current, ScheduleSnapshot):
            raise TypeError("schedule purge fence requires a snapshot")
        if current.spec.state != "ENABLED":
            return current
        cancelled = build_schedule_spec(
            schedule_id=current.spec.schedule_id,
            user_id=current.spec.user_id,
            task_type=current.spec.task_type,
            definition=current.spec.definition,
            revision=current.spec.revision + 1,
            state="CANCELLED",
            next_run_at=None,
        )
        ttl = now + PHYSICAL_RETENTION_SECONDS
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": self._schedule_key(current.spec.schedule_id),
                            "UpdateExpression": (
                                "SET recordJson = :cancelled, ttl = :ttl"
                            ),
                            "ConditionExpression": (
                                "recordJson = :current AND userId = :user"
                            ),
                            "ExpressionAttributeValues": {
                                ":current": {
                                    "S": _canonical_json(current.spec.to_mapping())
                                },
                                ":cancelled": {
                                    "S": _canonical_json(cancelled.to_mapping())
                                },
                                ":ttl": {"N": str(ttl)},
                                ":user": {"S": current.spec.user_id},
                            },
                        }
                    },
                    {"Update": self._counter_decrement(current.spec.user_id)},
                ]
            )
        except ClientError as error:
            if _conditional(error):
                return None
            raise
        return ScheduleSnapshot(
            spec=cancelled,
            delivery_target=current.delivery_target,
        )

    def _delete_keys(self, keys: list[dict[str, Any]]) -> bool:
        for offset in range(0, len(keys), 25):
            response = self._client.batch_write_item(
                RequestItems={
                    self._table_name: [
                        {"DeleteRequest": {"Key": key}}
                        for key in keys[offset : offset + 25]
                    ]
                }
            )
            if not isinstance(response, Mapping):
                return False
            unprocessed = response.get("UnprocessedItems")
            if unprocessed not in ({}, None):
                if not isinstance(unprocessed, Mapping) or any(
                    not isinstance(items, list) or items for items in unprocessed.values()
                ):
                    return False
        return True

    def delete_schedule_partition(self, current: ScheduleSnapshot) -> bool:
        if not isinstance(current, ScheduleSnapshot):
            raise TypeError("schedule purge requires a snapshot")
        pk = f"SCHEDULE#{current.spec.schedule_id}"
        has_state = False
        start_key = None
        seen_pages: set[str] = set()
        while True:
            request = {
                "TableName": self._table_name,
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": {":pk": {"S": pk}},
                "ProjectionExpression": "PK, SK",
                "ConsistentRead": True,
                "Limit": 1000,
            }
            if start_key is not None:
                request["ExclusiveStartKey"] = start_key
            response = self._client.query(**request)
            items = response.get("Items") if isinstance(response, Mapping) else None
            if not isinstance(items, list):
                return False
            occurrence_keys = []
            for item in items:
                if not isinstance(item, Mapping) or set(item) != {"PK", "SK"}:
                    return False
                item_pk = _attribute_string(item, "PK")
                item_sk = _attribute_string(item, "SK")
                if item_pk != pk or (
                    item_sk != "STATE" and not item_sk.startswith("OCCURRENCE#")
                ):
                    return False
                if item_sk == "STATE":
                    has_state = True
                else:
                    occurrence_keys.append(
                        {"PK": {"S": item_pk}, "SK": {"S": item_sk}}
                    )
            if occurrence_keys and not self._delete_keys(occurrence_keys):
                return False
            start_key = response.get("LastEvaluatedKey")
            if start_key is None:
                break
            marker = json.dumps(start_key, sort_keys=True, separators=(",", ":"))
            if marker in seen_pages:
                return False
            seen_pages.add(marker)
        if not has_state:
            return False
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Delete": {
                            "TableName": self._table_name,
                            "Key": self._schedule_key(current.spec.schedule_id),
                            "ConditionExpression": (
                                "recordJson = :record AND userId = :user"
                            ),
                            "ExpressionAttributeValues": {
                                ":record": {
                                    "S": _canonical_json(current.spec.to_mapping())
                                },
                                ":user": {"S": current.spec.user_id},
                            },
                        }
                    },
                    {
                        "Delete": {
                            "TableName": self._table_name,
                            "Key": {
                                "PK": {"S": f"USER#{current.spec.user_id}"},
                                "SK": {"S": f"SCHEDULE#{current.spec.schedule_id}"},
                            },
                            "ConditionExpression": (
                                "recordType = :owner AND scheduleId = :schedule"
                            ),
                            "ExpressionAttributeValues": {
                                ":owner": {"S": "SCHEDULE_OWNER"},
                                ":schedule": {"S": current.spec.schedule_id},
                            },
                        }
                    },
                ]
            )
            return True
        except ClientError as error:
            if _conditional(error):
                return False
            raise

    def delete_user_proposals(self, user_id: str) -> bool:
        if not isinstance(user_id, str) or _USER.fullmatch(user_id) is None:
            raise ValueError("proposal purge user is invalid")
        start_key = None
        seen_pages: set[str] = set()
        while True:
            request = {
                "TableName": self._table_name,
                "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
                "ExpressionAttributeValues": {
                    ":pk": {"S": f"USER#{user_id}"},
                    ":prefix": {"S": "PROPOSAL#"},
                },
                "ProjectionExpression": "PK, SK",
                "ConsistentRead": True,
                "Limit": 1000,
            }
            if start_key is not None:
                request["ExclusiveStartKey"] = start_key
            response = self._client.query(**request)
            items = response.get("Items") if isinstance(response, Mapping) else None
            if not isinstance(items, list):
                return False
            keys: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, Mapping) or set(item) != {"PK", "SK"}:
                    return False
                pk = _attribute_string(item, "PK")
                sk = _attribute_string(item, "SK")
                if pk != f"USER#{user_id}" or not sk.startswith("PROPOSAL#"):
                    return False
                keys.append({"PK": {"S": pk}, "SK": {"S": sk}})
            if keys and not self._delete_keys(keys):
                return False
            start_key = response.get("LastEvaluatedKey")
            if start_key is None:
                break
            marker = json.dumps(start_key, sort_keys=True, separators=(",", ":"))
            if marker in seen_pages:
                return False
            seen_pages.add(marker)
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {"ConditionCheck": self._active_deletion_condition(user_id)},
                    {
                        "Delete": {
                            "TableName": self._table_name,
                            "Key": {
                                "PK": {"S": f"USER#{user_id}"},
                                "SK": {"S": _COUNTER_SK},
                            },
                        }
                    }
                ]
            )
            return True
        except ClientError as error:
            if _conditional(error):
                return False
            raise


class DynamoScheduleApprovalAuthority:
    """Recheck mutable user, deletion, global, and per-pack authority."""

    _PACKS = {
        "schedule.propose": "schedule.propose",
        "schedule.cancel.propose": "schedule.cancel-propose",
    }

    def __init__(
        self,
        *,
        repository: DynamoAdmissionRepository,
        catalog_digest: str,
    ) -> None:
        if not isinstance(repository, DynamoAdmissionRepository):
            raise TypeError("schedule approval authority repository is invalid")
        if (
            not isinstance(catalog_digest, str)
            or _SHA256.fullmatch(catalog_digest) is None
        ):
            raise ValueError("schedule approval authority catalog is invalid")
        self._repository = repository
        self._catalog_digest = catalog_digest

    def assert_enabled(self, user_id: str, operation_id: str) -> None:
        if operation_id not in self._PACKS:
            raise ScheduleControlError("schedule approval operation is invalid")
        if self._repository.strong_read_deletion_fence(user_id):
            raise ScheduleControlError("schedule approval deletion fence is active")
        if self._repository.strong_read_global_kill_switch():
            raise ScheduleControlError("schedule approval global kill switch is active")
        user = self._repository.strong_read_user(user_id)
        if (
            not isinstance(user, Mapping)
            or set(user) != {"userId", "state", "deletionFence"}
            or user.get("userId") != user_id
            or user.get("state") != "ACTIVE"
            or user.get("deletionFence") is not False
        ):
            raise ScheduleControlError("schedule approval user is not active")
        raw = self._repository.strong_read_installation(
            user_id, self._PACKS[operation_id]
        )
        try:
            installation = (
                raw
                if isinstance(raw, CapabilityInstallationV1)
                else CapabilityInstallationV1.from_mapping(raw)
            )
        except (ContractValidationError, TypeError, ValueError):
            raise ScheduleControlError(
                "schedule approval installation is invalid"
            ) from None
        if (
            installation.user_id != user_id
            or installation.pack_id != self._PACKS[operation_id]
            or installation.catalog_digest != self._catalog_digest
            or installation.state != "ENABLED"
            or installation.kill_switch
        ):
            raise ScheduleControlError("schedule approval pack is not enabled")


class EventBridgeSchedulerAdapter:
    """Exact EventBridge Scheduler adapter with opaque names and observations."""

    def __init__(
        self,
        *,
        client: Any,
        ingress_function_arn: str,
        invoke_role_arn: str,
        group_name: str,
    ) -> None:
        if any(
            not callable(getattr(client, method, None))
            for method in ("create_schedule", "delete_schedule", "get_schedule")
        ):
            raise TypeError("EventBridge Scheduler client is invalid")
        if (
            not isinstance(ingress_function_arn, str)
            or _INGRESS_ARN.fullmatch(ingress_function_arn) is None
        ):
            raise ValueError("scheduler ingress function ARN is invalid")
        if (
            not isinstance(invoke_role_arn, str)
            or _ROLE_ARN.fullmatch(invoke_role_arn) is None
        ):
            raise ValueError("scheduler invoke role ARN is invalid")
        if not isinstance(group_name, str) or _GROUP.fullmatch(group_name) is None:
            raise ValueError("scheduler group name is invalid")
        self._client = client
        self.ingress_function_arn = ingress_function_arn
        self.invoke_role_arn = invoke_role_arn
        self._group_name = group_name

    @staticmethod
    def provider_name(schedule_id: str) -> str:
        if not isinstance(schedule_id, str) or _OPAQUE.fullmatch(schedule_id) is None:
            raise ValueError("schedule identity is invalid")
        return "po-" + hashlib.sha256(schedule_id.encode("utf-8")).hexdigest()[:60]

    @staticmethod
    def _client_token(action: str, schedule_id: str, revision: int) -> str:
        digest = hashlib.sha256(b"personal-operator.scheduler-provider.v1\0")
        digest.update(action.encode("ascii"))
        digest.update(b"\0")
        digest.update(schedule_id.encode("ascii"))
        digest.update(b"\0")
        digest.update(str(revision).encode("ascii"))
        return digest.hexdigest()

    def create_one_time_schedule(
        self, *, spec: ScheduleSpecV1, payload: SchedulePayloadV1
    ) -> None:
        if (
            not isinstance(spec, ScheduleSpecV1)
            or not isinstance(payload, SchedulePayloadV1)
            or spec.state != "ENABLED"
            or spec.next_run_at is None
            or payload.schedule_id != spec.schedule_id
            or payload.generation != spec.revision
            or payload.fire_time != spec.next_run_at
        ):
            raise ValueError("EventBridge schedule request is invalid")
        timestamp = datetime.fromtimestamp(spec.next_run_at, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        self._client.create_schedule(
            Name=self.provider_name(spec.schedule_id),
            GroupName=self._group_name,
            ScheduleExpression=f"at({timestamp})",
            ScheduleExpressionTimezone="UTC",
            FlexibleTimeWindow={"Mode": "OFF"},
            State="ENABLED",
            ActionAfterCompletion="DELETE",
            ClientToken=self._client_token("create", spec.schedule_id, spec.revision),
            Target={
                "Arn": self.ingress_function_arn,
                "RoleArn": self.invoke_role_arn,
                "Input": payload.to_json(),
                "RetryPolicy": {
                    "MaximumEventAgeInSeconds": 60,
                    "MaximumRetryAttempts": 0,
                },
            },
        )

    def delete_schedule(self, *, schedule_id: str) -> None:
        try:
            self._client.delete_schedule(
                Name=self.provider_name(schedule_id),
                GroupName=self._group_name,
                ClientToken=self._client_token("delete", schedule_id, 1),
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == (
                "ResourceNotFoundException"
            ):
                return
            raise

    def observe_schedule(
        self,
        *,
        schedule_id: str,
        expected_payload: SchedulePayloadV1 | None = None,
    ) -> str:
        if expected_payload is not None and (
            not isinstance(expected_payload, SchedulePayloadV1)
            or expected_payload.schedule_id != schedule_id
        ):
            raise ValueError("schedule observation binding is invalid")
        name = self.provider_name(schedule_id)
        try:
            response = self._client.get_schedule(
                Name=name, GroupName=self._group_name
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == (
                "ResourceNotFoundException"
            ):
                return "MISSING"
            return "UNKNOWN"
        except Exception:
            return "UNKNOWN"
        if not isinstance(response, Mapping):
            return "UNKNOWN"
        target = response.get("Target")
        if (
            response.get("Name") != name
            or response.get("GroupName") != self._group_name
            or not isinstance(target, Mapping)
            or set(target) != {"Arn", "RoleArn", "Input", "RetryPolicy"}
            or target.get("Arn") != self.ingress_function_arn
            or target.get("RoleArn") != self.invoke_role_arn
            or not isinstance(target.get("Input"), str)
            or target.get("RetryPolicy")
            != {
                "MaximumEventAgeInSeconds": 60,
                "MaximumRetryAttempts": 0,
            }
        ):
            return "UNKNOWN"
        try:
            payload = SchedulePayloadV1.from_json(target["Input"])
        except (TypeError, ValueError):
            return "UNKNOWN"
        if payload.schedule_id != schedule_id:
            return "UNKNOWN"
        timestamp = datetime.fromtimestamp(
            payload.fire_time, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S")
        if (
            response.get("ScheduleExpression") != f"at({timestamp})"
            or response.get("ScheduleExpressionTimezone") != "UTC"
            or response.get("FlexibleTimeWindow") != {"Mode": "OFF"}
            or response.get("State") != "ENABLED"
            or response.get("ActionAfterCompletion") != "DELETE"
        ):
            return "UNKNOWN"
        if expected_payload is not None and payload.to_json() != expected_payload.to_json():
            return "UNKNOWN"
        return "PRESENT"


_service_factory: Callable[[], ScheduleControlService] | None = None
_production_service: ScheduleControlService | None = None


def configure_service_factory(
    factory: Callable[[], ScheduleControlService] | None,
) -> None:
    global _service_factory, _production_service
    if factory is not None and not callable(factory):
        raise TypeError("schedule control service factory is invalid")
    _service_factory = factory
    _production_service = None


def _aws_client(service_name: str):
    import boto3
    from botocore.config import Config

    return boto3.client(
        service_name,
        region_name=REQUIRED_REGION,
        config=Config(retries={"max_attempts": 0}),
    )


def _required_env(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{name} is required")
    return value


def build_control_service(
    *, env: Mapping[str, str] = os.environ
) -> ScheduleControlService:
    region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
    if region != REQUIRED_REGION or env.get("AWS_REGION_LOCK") != REQUIRED_REGION:
        raise RuntimeError("schedule control requires exact eu-west-1 region")
    dynamodb_client = _aws_client("dynamodb")
    catalog_digest = _required_env(env, "CAPABILITY_CATALOG_DIGEST")
    capability_table_name = _required_env(env, "CAPABILITY_STATE_TABLE_NAME")
    repository = DynamoScheduleControlRepository(
        client=dynamodb_client,
        table_name=_required_env(env, "SCHEDULER_CONTROL_TABLE_NAME"),
        capability_table_name=capability_table_name,
    )
    authority = DynamoScheduleApprovalAuthority(
        repository=DynamoAdmissionRepository(
            client=dynamodb_client,
            table_name=capability_table_name,
        ),
        catalog_digest=catalog_digest,
    )
    provider = EventBridgeSchedulerAdapter(
        client=_aws_client("scheduler"),
        ingress_function_arn=_required_env(
            env, "SCHEDULER_INGRESS_FUNCTION_ARN"
        ),
        invoke_role_arn=_required_env(env, "SCHEDULER_INVOKE_ROLE_ARN"),
        group_name=_required_env(env, "SCHEDULER_GROUP_NAME"),
    )
    return ScheduleControlService(
        repository=repository,
        provider=provider,
        catalog_digest=catalog_digest,
        clock=lambda: int(time.time()),
        authority_guard=authority.assert_enabled,
        uncertain_errors=(Exception,),
    )


def handle_control(
    event: Any, service: ScheduleControlService
) -> dict[str, Any]:
    if not isinstance(event, Mapping) or not isinstance(service, ScheduleControlService):
        raise ScheduleControlError("schedule control request is invalid")
    action = event.get("action")
    if action in {"PREVIEW", "RECONCILE"}:
        expected = {"action", "userId", "proposalRef"}
    elif action == "PURGE_USER":
        expected = {"action", "userId"}
    elif action in {"APPROVE", "REJECT"}:
        expected = {"action", "userId", "proposalRef", "revision", "argsHash"}
    else:
        raise ScheduleControlError("schedule control action is invalid")
    if set(event) != expected:
        raise ScheduleControlError("schedule control request shape is invalid")
    if action == "PURGE_USER":
        return {
            "remaining": service.purge_user_schedules(event["userId"]),
        }
    if action == "PREVIEW":
        return service.preview(
            user_id=event["userId"], proposal_ref=event["proposalRef"]
        )
    if action == "APPROVE":
        outcome = service.approve(
            user_id=event["userId"],
            proposal_ref=event["proposalRef"],
            revision=event["revision"],
            args_hash=event["argsHash"],
        )
    elif action == "REJECT":
        outcome = service.reject(
            user_id=event["userId"],
            proposal_ref=event["proposalRef"],
            revision=event["revision"],
            args_hash=event["argsHash"],
        )
    else:
        outcome = service.reconcile(
            user_id=event["userId"], proposal_ref=event["proposalRef"]
        )
    return outcome.to_mapping()


def lambda_handler(event: Any, _context: Any) -> dict[str, Any]:
    global _production_service
    if _service_factory is not None:
        service = _service_factory()
    else:
        if _production_service is None:
            _production_service = build_control_service()
        service = _production_service
    return handle_control(event, service)


__all__ = [
    "ControlOutcome",
    "DynamoScheduleApprovalAuthority",
    "DynamoScheduleControlRepository",
    "EventBridgeSchedulerAdapter",
    "ProposalSnapshot",
    "ScheduleControlError",
    "ScheduleControlRepository",
    "ScheduleControlService",
    "ScheduleProvider",
    "ScheduleSnapshot",
    "build_control_service",
    "configure_service_factory",
    "handle_control",
    "lambda_handler",
]
