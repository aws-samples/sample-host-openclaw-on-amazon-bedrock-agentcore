"""Atomic, single-item runtime state and lease repository."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from enum import Enum

_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{32,255}")


class RuntimeState(str, Enum):
    COLD = "COLD"
    STARTING = "STARTING"
    READY = "READY"
    BUSY = "BUSY"
    IDLE = "IDLE"
    UNHEALTHY = "UNHEALTHY"
    QUARANTINED = "QUARANTINED"
    DELETING = "DELETING"


ALL_RUNTIME_STATES = frozenset(state.value for state in RuntimeState)


class RuntimeStateError(RuntimeError):
    pass


class TombstonedUser(RuntimeStateError):
    pass


class RuntimeUnavailable(RuntimeStateError):
    pass


class LeaseBusy(RuntimeStateError):
    pass


class LeaseLost(RuntimeStateError):
    pass


class StaleLease(RuntimeStateError):
    def __init__(self, record: "RuntimeRecord") -> None:
        super().__init__(f"stale runtime lease for {record.user_id}")
        self.record = record


@dataclass(frozen=True)
class RuntimeRecord:
    user_id: str
    session_id: str | None
    state: RuntimeState
    revision: int
    lease_owner: str | None
    lease_epoch: int
    lease_expires_at: int | None
    last_trace_id: str | None
    last_invocation_id: str | None
    last_workspace_generation: str | None
    last_workspace_manifest_sha256: str | None
    created_at: int
    updated_at: int
    tombstoned_at: int | None

    def public(self) -> dict:
        return {
            "userId": self.user_id,
            "sessionId": self.session_id,
            "state": self.state.value,
            "revision": self.revision,
            "leaseEpoch": self.lease_epoch,
            "updatedAt": self.updated_at,
            "tombstonedAt": self.tombstoned_at,
            "workspaceReceipt": (
                {
                    "generation": self.last_workspace_generation,
                    "manifestSha256": self.last_workspace_manifest_sha256,
                }
                if self.last_workspace_generation
                and self.last_workspace_manifest_sha256
                else None
            ),
        }


def canonical_user_id(user_id: str) -> str:
    value = str(user_id or "")
    if _USER_ID.fullmatch(value) is None:
        raise ValueError("invalid internal user identity")
    return value


def canonical_session_id(session_id: str) -> str:
    value = str(session_id or "")
    if _SESSION_ID.fullmatch(value) is None:
        raise ValueError("invalid server runtime session identity")
    return value


def generate_session_id() -> str:
    return canonical_session_id(f"ses_{uuid.uuid4().hex}")


def _is_conditional(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    return bool(
        isinstance(response, dict)
        and response.get("Error", {}).get("Code")
        == "ConditionalCheckFailedException"
    )


class RuntimeStateRepository:
    """Stores tombstone, session mapping, and fenced lease in one DynamoDB item."""

    def __init__(self, table, *, clock_ms=None, session_id_factory=None) -> None:
        self.table = table
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.session_id_factory = session_id_factory or generate_session_id

    @staticmethod
    def _record(item: dict) -> RuntimeRecord:
        try:
            return RuntimeRecord(
                user_id=canonical_user_id(item["userId"]),
                session_id=(
                    canonical_session_id(item["sessionId"])
                    if item.get("sessionId") is not None
                    else None
                ),
                state=RuntimeState(item["state"]),
                revision=int(item.get("revision", 0)),
                lease_owner=item.get("leaseOwner"),
                lease_epoch=int(item.get("leaseEpoch", 0)),
                lease_expires_at=(
                    int(item["leaseExpiresAt"])
                    if item.get("leaseExpiresAt") is not None
                    else None
                ),
                last_trace_id=item.get("lastTraceId"),
                last_invocation_id=item.get("lastInvocationId"),
                last_workspace_generation=item.get("lastWorkspaceGeneration"),
                last_workspace_manifest_sha256=item.get(
                    "lastWorkspaceManifestSha256"
                ),
                created_at=int(item["createdAt"]),
                updated_at=int(item["updatedAt"]),
                tombstoned_at=(
                    int(item["tombstonedAt"])
                    if item.get("tombstonedAt") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeStateError("corrupt runtime-state record") from error

    def get(self, user_id: str) -> RuntimeRecord | None:
        user_id = canonical_user_id(user_id)
        response = self.table.get_item(
            Key={"userId": user_id}, ConsistentRead=True
        )
        item = response.get("Item")
        return self._record(item) if item else None

    @staticmethod
    def _assert_available(record: RuntimeRecord) -> None:
        if record.tombstoned_at is not None or record.state is RuntimeState.DELETING:
            raise TombstonedUser(record.user_id)
        if record.state in {RuntimeState.QUARANTINED, RuntimeState.UNHEALTHY}:
            raise RuntimeUnavailable(
                f"runtime for {record.user_id} is {record.state.value.lower()}"
            )

    def ensure(self, user_id: str) -> RuntimeRecord:
        user_id = canonical_user_id(user_id)
        now = int(self.clock_ms())
        candidate = RuntimeRecord(
            user_id=user_id,
            session_id=canonical_session_id(self.session_id_factory()),
            state=RuntimeState.COLD,
            revision=1,
            lease_owner=None,
            lease_epoch=0,
            lease_expires_at=None,
            last_trace_id=None,
            last_invocation_id=None,
            last_workspace_generation=None,
            last_workspace_manifest_sha256=None,
            created_at=now,
            updated_at=now,
            tombstoned_at=None,
        )
        item = {
            "userId": candidate.user_id,
            "sessionId": candidate.session_id,
            "state": candidate.state.value,
            "revision": candidate.revision,
            "leaseEpoch": candidate.lease_epoch,
            "createdAt": now,
            "updatedAt": now,
        }
        try:
            self.table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(userId)",
            )
            return candidate
        except Exception as error:
            if not _is_conditional(error):
                raise RuntimeStateError("runtime-state creation failed") from error
        winner = self.get(user_id)
        if winner is None:
            raise RuntimeStateError("runtime-state race had no winner")
        self._assert_available(winner)
        return winner

    def acquire(
        self,
        user_id: str,
        *,
        owner: str,
        trace_id: str,
        lease_ms: int,
    ) -> RuntimeRecord:
        user_id = canonical_user_id(user_id)
        if not owner or not trace_id or lease_ms <= 0:
            raise ValueError("lease owner, trace, and duration are required")
        for attempt in range(2):
            now = int(self.clock_ms())
            try:
                response = self.table.update_item(
                    Key={"userId": user_id},
                    UpdateExpression=(
                        "SET leaseOwner=:owner, leaseExpiresAt=:until, "
                        "lastTraceId=:trace, #state=:busy, updatedAt=:now, "
                        "leaseEpoch=if_not_exists(leaseEpoch,:zero)+:one, "
                        "revision=if_not_exists(revision,:zero)+:one"
                    ),
                    ConditionExpression=(
                        "attribute_exists(userId) AND "
                        "attribute_not_exists(tombstonedAt) AND "
                        "#state <> :deleting AND #state <> :quarantined AND "
                        "#state <> :unhealthy AND "
                        "(attribute_not_exists(leaseExpiresAt) OR leaseOwner = :owner)"
                    ),
                    ExpressionAttributeNames={"#state": "state"},
                    ExpressionAttributeValues={
                        ":owner": owner,
                        ":until": now + lease_ms,
                        ":trace": trace_id,
                        ":busy": RuntimeState.BUSY.value,
                        ":deleting": RuntimeState.DELETING.value,
                        ":quarantined": RuntimeState.QUARANTINED.value,
                        ":unhealthy": RuntimeState.UNHEALTHY.value,
                        ":now": now,
                        ":zero": 0,
                        ":one": 1,
                    },
                    ReturnValues="ALL_NEW",
                )
                return self._record(response["Attributes"])
            except Exception as error:
                if not _is_conditional(error):
                    raise RuntimeStateError("runtime lease acquisition failed") from error
            current = self.get(user_id)
            if current is None:
                raise RuntimeStateError("runtime state disappeared during acquisition")
            self._assert_available(current)
            if current.lease_owner and current.lease_expires_at is not None:
                if current.lease_expires_at < now:
                    raise StaleLease(current)
                raise LeaseBusy(user_id)
            if attempt == 1:
                raise LeaseBusy(user_id)
        raise AssertionError("unreachable")

    @staticmethod
    def _lease_condition() -> str:
        return (
            "leaseOwner=:owner AND leaseEpoch=:epoch AND "
            "leaseExpiresAt >= :now AND attribute_not_exists(tombstonedAt)"
        )

    def _conditional_update(self, operation: str, **kwargs) -> RuntimeRecord:
        try:
            response = self.table.update_item(ReturnValues="ALL_NEW", **kwargs)
            return self._record(response["Attributes"])
        except Exception as error:
            if _is_conditional(error):
                raise LeaseLost(operation) from error
            raise RuntimeStateError(f"runtime {operation} update failed") from error

    def fence_stale(
        self,
        stale: RuntimeRecord,
        *,
        owner: str,
        trace_id: str,
        lease_ms: int,
    ) -> RuntimeRecord:
        now = int(self.clock_ms())
        return self._conditional_update(
            stale.user_id,
            Key={"userId": stale.user_id},
            UpdateExpression=(
                "SET leaseOwner=:newOwner, leaseEpoch=:newEpoch, "
                "leaseExpiresAt=:until, lastTraceId=:trace, #state=:unhealthy, "
                "updatedAt=:now, revision=if_not_exists(revision,:zero)+:one"
            ),
            ConditionExpression=(
                "leaseOwner=:oldOwner AND leaseEpoch=:oldEpoch AND "
                "leaseExpiresAt < :now AND attribute_not_exists(tombstonedAt) AND "
                "#state <> :deleting AND #state <> :quarantined"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":newOwner": owner,
                ":newEpoch": stale.lease_epoch + 1,
                ":until": now + lease_ms,
                ":trace": trace_id,
                ":unhealthy": RuntimeState.UNHEALTHY.value,
                ":now": now,
                ":zero": 0,
                ":one": 1,
                ":oldOwner": stale.lease_owner,
                ":oldEpoch": stale.lease_epoch,
                ":deleting": RuntimeState.DELETING.value,
                ":quarantined": RuntimeState.QUARANTINED.value,
            },
        )

    def heartbeat(
        self, lease: RuntimeRecord, *, lease_ms: int
    ) -> RuntimeRecord:
        if lease_ms <= 0:
            raise ValueError("lease duration must be positive")
        now = int(self.clock_ms())
        return self._conditional_update(
            lease.user_id,
            Key={"userId": lease.user_id},
            UpdateExpression="SET leaseExpiresAt=:until, updatedAt=:now",
            ConditionExpression=self._lease_condition(),
            ExpressionAttributeValues={
                ":until": now + lease_ms,
                ":now": now,
                ":owner": lease.lease_owner,
                ":epoch": lease.lease_epoch,
            },
        )

    def rotate_after_fence(
        self, lease: RuntimeRecord, *, session_id: str, lease_ms: int
    ) -> RuntimeRecord:
        now = int(self.clock_ms())
        return self._conditional_update(
            lease.user_id,
            Key={"userId": lease.user_id},
            UpdateExpression=(
                "SET sessionId=:session, #state=:busy, leaseExpiresAt=:until, "
                "updatedAt=:now, revision=if_not_exists(revision,:zero)+:one"
            ),
            ConditionExpression=(
                self._lease_condition() + " AND #state=:unhealthy"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":session": canonical_session_id(session_id),
                ":busy": RuntimeState.BUSY.value,
                ":until": now + lease_ms,
                ":now": now,
                ":zero": 0,
                ":one": 1,
                ":owner": lease.lease_owner,
                ":epoch": lease.lease_epoch,
                ":unhealthy": RuntimeState.UNHEALTHY.value,
            },
        )

    def finalize_success(
        self, lease: RuntimeRecord, *, invocation_id: str, receipt: dict
    ) -> RuntimeRecord:
        now = int(self.clock_ms())
        return self._conditional_update(
            lease.user_id,
            Key={"userId": lease.user_id},
            UpdateExpression=(
                "SET #state=:idle, updatedAt=:now, lastInvocationId=:invocation, "
                "lastWorkspaceGeneration=:generation, "
                "lastWorkspaceManifestSha256=:sha, "
                "revision=if_not_exists(revision,:zero)+:one "
                "REMOVE leaseOwner, leaseExpiresAt"
            ),
            ConditionExpression=self._lease_condition(),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":idle": RuntimeState.IDLE.value,
                ":now": now,
                ":invocation": invocation_id,
                ":generation": receipt["generation"],
                ":sha": receipt["manifestSha256"],
                ":zero": 0,
                ":one": 1,
                ":owner": lease.lease_owner,
                ":epoch": lease.lease_epoch,
            },
        )

    def finalize_failure(
        self, lease: RuntimeRecord, *, state: RuntimeState
    ) -> RuntimeRecord:
        if state not in {RuntimeState.UNHEALTHY, RuntimeState.QUARANTINED}:
            raise ValueError("failure state must be UNHEALTHY or QUARANTINED")
        now = int(self.clock_ms())
        return self._conditional_update(
            lease.user_id,
            Key={"userId": lease.user_id},
            UpdateExpression=(
                "SET #state=:state, updatedAt=:now, "
                "revision=if_not_exists(revision,:zero)+:one "
                "REMOVE leaseOwner, leaseExpiresAt"
            ),
            ConditionExpression=self._lease_condition(),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":state": state.value,
                ":now": now,
                ":zero": 0,
                ":one": 1,
                ":owner": lease.lease_owner,
                ":epoch": lease.lease_epoch,
            },
        )

    def rotate_after_stop(
        self, lease: RuntimeRecord, *, session_id: str
    ) -> RuntimeRecord:
        now = int(self.clock_ms())
        return self._conditional_update(
            lease.user_id,
            Key={"userId": lease.user_id},
            UpdateExpression=(
                "SET sessionId=:session, #state=:cold, updatedAt=:now, "
                "revision=if_not_exists(revision,:zero)+:one "
                "REMOVE leaseOwner, leaseExpiresAt"
            ),
            ConditionExpression=self._lease_condition(),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":session": canonical_session_id(session_id),
                ":cold": RuntimeState.COLD.value,
                ":now": now,
                ":zero": 0,
                ":one": 1,
                ":owner": lease.lease_owner,
                ":epoch": lease.lease_epoch,
            },
        )

    def begin_purge(
        self, user_id: str, *, owner: str, lease_ms: int
    ) -> RuntimeRecord:
        user_id = canonical_user_id(user_id)
        if lease_ms <= 0:
            raise ValueError("lease duration must be positive")
        now = int(self.clock_ms())
        try:
            response = self.table.update_item(
                Key={"userId": user_id},
                UpdateExpression=(
                    "SET tombstonedAt=:now, #state=:deleting, leaseOwner=:owner, "
                    "leaseExpiresAt=:until, "
                    "leaseEpoch=if_not_exists(leaseEpoch,:zero)+:one, "
                    "createdAt=if_not_exists(createdAt,:now), updatedAt=:now, "
                    "revision=if_not_exists(revision,:zero)+:one"
                ),
                ConditionExpression=(
                    "attribute_not_exists(tombstonedAt) AND "
                    "(attribute_not_exists(#state) OR #state <> :deleting) AND "
                    "(attribute_not_exists(leaseExpiresAt) OR leaseExpiresAt < :now)"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":now": now,
                    ":until": now + lease_ms,
                    ":deleting": RuntimeState.DELETING.value,
                    ":owner": owner,
                    ":zero": 0,
                    ":one": 1,
                },
                ReturnValues="ALL_NEW",
            )
            return self._record(response["Attributes"])
        except Exception as error:
            if not _is_conditional(error):
                raise RuntimeStateError("runtime purge fencing failed") from error
        current = self.get(user_id)
        if current is None:
            raise RuntimeUnavailable("runtime does not exist")
        if current.tombstoned_at is not None:
            raise TombstonedUser(user_id)
        raise LeaseBusy(user_id)

    def finish_purge(self, lease: RuntimeRecord) -> RuntimeRecord:
        now = int(self.clock_ms())
        return self._conditional_update(
            lease.user_id,
            Key={"userId": lease.user_id},
            UpdateExpression=(
                "SET updatedAt=:now, revision=if_not_exists(revision,:zero)+:one "
                "REMOVE sessionId, leaseOwner, leaseExpiresAt, lastTraceId, "
                "lastInvocationId, lastWorkspaceGeneration, "
                "lastWorkspaceManifestSha256"
            ),
            ConditionExpression=(
                "leaseOwner=:owner AND leaseEpoch=:epoch AND "
                "attribute_exists(tombstonedAt) AND #state=:deleting"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":owner": lease.lease_owner,
                ":epoch": lease.lease_epoch,
                ":deleting": RuntimeState.DELETING.value,
                ":now": now,
                ":zero": 0,
                ":one": 1,
            },
        )

    def mark_purge_uncertain(self, lease: RuntimeRecord) -> RuntimeRecord:
        now = int(self.clock_ms())
        return self._conditional_update(
            lease.user_id,
            Key={"userId": lease.user_id},
            UpdateExpression=(
                "SET #state=:quarantined, updatedAt=:now, "
                "revision=if_not_exists(revision,:zero)+:one "
                "REMOVE leaseOwner, leaseExpiresAt"
            ),
            ConditionExpression=(
                "leaseOwner=:owner AND leaseEpoch=:epoch AND "
                "attribute_exists(tombstonedAt)"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":owner": lease.lease_owner,
                ":epoch": lease.lease_epoch,
                ":quarantined": RuntimeState.QUARANTINED.value,
                ":now": now,
                ":zero": 0,
                ":one": 1,
            },
        )
