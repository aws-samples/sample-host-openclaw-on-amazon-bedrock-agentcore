"""Atomic, single-item runtime state and lease repository."""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from enum import Enum

_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{32,255}")
_RUNTIME_ARN = re.compile(
    r"arn:aws:bedrock-agentcore:(?P<region>[a-z]{2}-[a-z]+-[1-9]):"
    r"(?P<account>[0-9]{12}):agent/"
    r"(?P<agent>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}):(?P<version>[1-9][0-9]{0,4})"
)
_RELEASE_ENDPOINT = re.compile(r"release_[0-9a-f]{40}")


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


class InactivityFenceLost(RuntimeStateError):
    """The scanned inactivity snapshot no longer matches the live record."""


class StaleLease(RuntimeStateError):
    def __init__(self, record: "RuntimeRecord") -> None:
        super().__init__(f"stale runtime lease for {record.user_id}")
        self.record = record


class DuplicateTraceUncertain(RuntimeStateError):
    def __init__(self, record: "RuntimeRecord") -> None:
        super().__init__(f"duplicate invocation trace for {record.user_id}")
        self.record = record


@dataclass(frozen=True)
class RuntimeRecord:
    user_id: str
    session_id: str | None
    runtime_arn: str
    runtime_qualifier: str
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
    last_mutation_id: str | None
    stop_operation_id: str | None
    purge_reason: str | None = None
    purge_completed_at: int | None = None
    workspace_stop_verified_at: int | None = None
    purge_observed_updated_at: int | None = None
    purge_observed_revision: int | None = None
    purge_inactive_before: int | None = None
    last_purge_reason: str | None = None
    last_purge_completed_at: int | None = None

    def public(self) -> dict:
        return {
            "userId": self.user_id,
            "sessionId": self.session_id,
            "runtimeArn": self.runtime_arn,
            "runtimeQualifier": self.runtime_qualifier,
            "state": self.state.value,
            "revision": self.revision,
            "leaseEpoch": self.lease_epoch,
            "updatedAt": self.updated_at,
            "tombstonedAt": self.tombstoned_at,
            "purgeReason": self.purge_reason,
            "purgeCompletedAt": self.purge_completed_at,
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


def canonical_runtime_arn(runtime_arn: str) -> str:
    value = str(runtime_arn or "")
    match = _RUNTIME_ARN.fullmatch(value)
    if match is None or match.group("region") != "eu-west-1":
        raise ValueError("invalid exact AgentCore runtime ARN")
    return value


def runtime_lineage(runtime_arn: str) -> tuple[str, str, str]:
    value = canonical_runtime_arn(runtime_arn)
    match = _RUNTIME_ARN.fullmatch(value)
    assert match is not None
    return match.group("region"), match.group("account"), match.group("agent")


def canonical_runtime_qualifier(qualifier: str) -> str:
    value = str(qualifier or "")
    if _RELEASE_ENDPOINT.fullmatch(value) is None:
        raise ValueError("runtime qualifier must be an exact release endpoint")
    return value


def deterministic_id(prefix: str, operation: str, *parts: object) -> str:
    if prefix not in {"mut", "op"} or not operation:
        raise ValueError("invalid deterministic identity kind")
    canonical = "\0".join(["personal-operator-runtime-v1", operation, *map(str, parts)])
    return f"{prefix}_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _is_conditional(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    return bool(
        isinstance(response, dict)
        and response.get("Error", {}).get("Code")
        == "ConditionalCheckFailedException"
    )


def _is_ambiguous(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return True
    response = getattr(error, "response", None)
    code = (
        response.get("Error", {}).get("Code")
        if isinstance(response, dict)
        else None
    )
    if code in {
        "InternalServerError",
        "ServiceUnavailable",
        "RequestTimeout",
        "RequestTimeoutException",
        "ThrottlingException",
        "ProvisionedThroughputExceededException",
    }:
        return True
    return type(error).__name__ in {
        "EndpointConnectionError",
        "ConnectionClosedError",
        "ReadTimeoutError",
        "ConnectTimeoutError",
    }


class RuntimeStateRepository:
    """Stores tombstone, session mapping, and fenced lease in one DynamoDB item."""

    def __init__(
        self,
        table,
        *,
        runtime_arn: str,
        runtime_qualifier: str,
        clock_ms=None,
        session_id_factory=None,
    ) -> None:
        self.table = table
        self.runtime_arn = canonical_runtime_arn(runtime_arn)
        self.runtime_qualifier = canonical_runtime_qualifier(runtime_qualifier)
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.session_id_factory = session_id_factory or generate_session_id

    def _record(self, item: dict) -> RuntimeRecord:
        try:
            record = RuntimeRecord(
                user_id=canonical_user_id(item["userId"]),
                session_id=(
                    canonical_session_id(item["sessionId"])
                    if item.get("sessionId") is not None
                    else None
                ),
                runtime_arn=canonical_runtime_arn(item["runtimeArn"]),
                runtime_qualifier=canonical_runtime_qualifier(
                    item["runtimeQualifier"]
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
                last_mutation_id=item.get("lastMutationId"),
                stop_operation_id=item.get("stopOperationId"),
                purge_reason=item.get("purgeReason"),
                purge_completed_at=(
                    int(item["purgeCompletedAt"])
                    if item.get("purgeCompletedAt") is not None
                    else None
                ),
                workspace_stop_verified_at=(
                    int(item["workspaceStopVerifiedAt"])
                    if item.get("workspaceStopVerifiedAt") is not None
                    else None
                ),
                purge_observed_updated_at=(
                    int(item["purgeObservedUpdatedAt"])
                    if item.get("purgeObservedUpdatedAt") is not None
                    else None
                ),
                purge_observed_revision=(
                    int(item["purgeObservedRevision"])
                    if item.get("purgeObservedRevision") is not None
                    else None
                ),
                purge_inactive_before=(
                    int(item["purgeInactiveBefore"])
                    if item.get("purgeInactiveBefore") is not None
                    else None
                ),
                last_purge_reason=item.get("lastPurgeReason"),
                last_purge_completed_at=(
                    int(item["lastPurgeCompletedAt"])
                    if item.get("lastPurgeCompletedAt") is not None
                    else None
                ),
            )
            if record.purge_reason not in {None, "ACCOUNT_DELETION", "WORKSPACE_EXPIRY"}:
                raise ValueError("invalid purge reason")
            if record.last_purge_reason not in {None, "WORKSPACE_EXPIRY"}:
                raise ValueError("invalid completed purge reason")
            positive = (
                record.purge_completed_at,
                record.workspace_stop_verified_at,
                record.purge_observed_updated_at,
                record.purge_observed_revision,
                record.purge_inactive_before,
                record.last_purge_completed_at,
            )
            if any(value is not None and value <= 0 for value in positive):
                raise ValueError("invalid purge timestamp or revision")
            return record
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeStateError("corrupt runtime-state record") from error

    def get(self, user_id: str) -> RuntimeRecord | None:
        user_id = canonical_user_id(user_id)
        response = self.table.get_item(
            Key={"userId": user_id}, ConsistentRead=True
        )
        item = response.get("Item")
        return self._record(item) if item else None

    def binding_matches(self, record: RuntimeRecord) -> bool:
        return (
            record.runtime_arn == self.runtime_arn
            and record.runtime_qualifier == self.runtime_qualifier
        )

    def _reconcile(
        self,
        user_id: str,
        mutation_id: str,
        expected,
    ) -> RuntimeRecord | None:
        current = self.get(user_id)
        if (
            current is not None
            and current.last_mutation_id == mutation_id
            and expected(current)
        ):
            return current
        return None

    def _conditional_update(
        self,
        operation: str,
        *,
        user_id: str,
        mutation_id: str,
        expected,
        **kwargs,
    ) -> RuntimeRecord:
        try:
            response = self.table.update_item(ReturnValues="ALL_NEW", **kwargs)
            return self._record(response["Attributes"])
        except Exception as error:
            if _is_conditional(error) or _is_ambiguous(error):
                try:
                    reconciled = self._reconcile(
                        user_id, mutation_id, expected
                    )
                except Exception:
                    reconciled = None
                if reconciled is not None:
                    return reconciled
            if _is_conditional(error):
                raise LeaseLost(operation) from error
            if _is_ambiguous(error):
                raise RuntimeStateError(
                    f"runtime {operation} update outcome is uncertain"
                ) from error
            raise RuntimeStateError(f"runtime {operation} update failed") from error

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
            runtime_arn=self.runtime_arn,
            runtime_qualifier=self.runtime_qualifier,
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
            last_mutation_id=None,
            stop_operation_id=None,
        )
        mutation_id = deterministic_id(
            "mut",
            "ensure",
            candidate.user_id,
            candidate.session_id,
            candidate.runtime_arn,
            candidate.runtime_qualifier,
        )
        candidate = RuntimeRecord(
            **{**candidate.__dict__, "last_mutation_id": mutation_id}
        )
        item = {
            "userId": candidate.user_id,
            "sessionId": candidate.session_id,
            "runtimeArn": candidate.runtime_arn,
            "runtimeQualifier": candidate.runtime_qualifier,
            "state": candidate.state.value,
            "revision": candidate.revision,
            "leaseEpoch": candidate.lease_epoch,
            "createdAt": now,
            "updatedAt": now,
            "lastMutationId": mutation_id,
        }
        try:
            self.table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(userId)",
            )
            return candidate
        except Exception as error:
            if not (_is_conditional(error) or _is_ambiguous(error)):
                raise RuntimeStateError("runtime-state creation failed") from error
        try:
            winner = self.get(user_id)
        except Exception as read_error:
            raise RuntimeStateError(
                "runtime-state creation outcome is uncertain"
            ) from read_error
        if winner is None:
            if _is_ambiguous(error):
                raise RuntimeStateError(
                    "runtime-state creation outcome is uncertain"
                ) from error
            raise RuntimeStateError("runtime-state race had no winner")
        if winner.tombstoned_at is not None or winner.state is RuntimeState.DELETING:
            raise TombstonedUser(winner.user_id)
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
        now = int(self.clock_ms())
        mutation_id = deterministic_id(
            "mut", "acquire", user_id, owner, trace_id, self.runtime_arn
        )
        try:
            response = self.table.update_item(
                Key={"userId": user_id},
                UpdateExpression=(
                    "SET leaseOwner=:owner, leaseExpiresAt=:until, "
                    "lastTraceId=:trace, #state=:busy, updatedAt=:now, "
                    "lastMutationId=:mutation, "
                    "leaseEpoch=if_not_exists(leaseEpoch,:zero)+:one, "
                    "revision=if_not_exists(revision,:zero)+:one"
                ),
                ConditionExpression=(
                    "attribute_exists(userId) AND "
                    "attribute_not_exists(tombstonedAt) AND "
                    "runtimeArn=:runtimeArn AND "
                    "runtimeQualifier=:runtimeQualifier AND "
                    "#state <> :deleting AND #state <> :quarantined AND "
                    "#state <> :unhealthy AND "
                    "(attribute_not_exists(lastTraceId) OR lastTraceId <> :trace) AND "
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
                    ":runtimeArn": self.runtime_arn,
                    ":runtimeQualifier": self.runtime_qualifier,
                    ":mutation": mutation_id,
                    ":now": now,
                    ":zero": 0,
                    ":one": 1,
                },
                ReturnValues="ALL_NEW",
            )
            return self._record(response["Attributes"])
        except Exception as error:
            if _is_ambiguous(error):
                reconciled = self._reconcile(
                    user_id,
                    mutation_id,
                    lambda value: value.state is RuntimeState.BUSY
                    and value.lease_owner == owner
                    and value.last_trace_id == trace_id,
                )
                if reconciled is not None:
                    return reconciled
                raise RuntimeStateError(
                    "runtime lease acquisition outcome is uncertain"
                ) from error
            if not _is_conditional(error):
                raise RuntimeStateError("runtime lease acquisition failed") from error
        current = self.get(user_id)
        if current is None:
            raise RuntimeStateError("runtime state disappeared during acquisition")
        if current.last_trace_id == trace_id:
            raise DuplicateTraceUncertain(current)
        self._assert_available(current)
        if not self.binding_matches(current):
            raise RuntimeUnavailable("runtime binding requires fenced recovery")
        if current.lease_owner and current.lease_expires_at is not None:
            if current.lease_expires_at < now:
                raise StaleLease(current)
            raise LeaseBusy(user_id)
        raise LeaseBusy(user_id)

    @staticmethod
    def _lease_condition() -> str:
        return (
            "leaseOwner=:owner AND leaseEpoch=:epoch AND "
            "leaseExpiresAt >= :now AND attribute_not_exists(tombstonedAt) AND "
            "sessionId=:oldSession AND runtimeArn=:runtimeArn AND "
            "runtimeQualifier=:runtimeQualifier"
        )

    def fence_stale(
        self,
        stale: RuntimeRecord,
        *,
        owner: str,
        trace_id: str,
        lease_ms: int,
    ) -> RuntimeRecord:
        now = int(self.clock_ms())
        new_epoch = stale.lease_epoch + 1
        stop_operation_id = deterministic_id(
            "op",
            "stale-stop",
            stale.user_id,
            stale.session_id,
            stale.runtime_arn,
            stale.runtime_qualifier,
            new_epoch,
        )
        mutation_id = deterministic_id(
            "mut", "stale-fence", stop_operation_id, owner, trace_id
        )
        return self._conditional_update(
            "stale-fence",
            user_id=stale.user_id,
            mutation_id=mutation_id,
            expected=lambda value: value.state is RuntimeState.UNHEALTHY
            and value.lease_owner == owner
            and value.lease_epoch == new_epoch
            and value.stop_operation_id == stop_operation_id,
            Key={"userId": stale.user_id},
            UpdateExpression=(
                "SET leaseOwner=:newOwner, leaseEpoch=:newEpoch, "
                "leaseExpiresAt=:until, lastTraceId=:trace, #state=:unhealthy, "
                "stopOperationId=:stopOperation, lastMutationId=:mutation, "
                "updatedAt=:now, revision=if_not_exists(revision,:zero)+:one"
            ),
            ConditionExpression=(
                "leaseOwner=:oldOwner AND leaseEpoch=:oldEpoch AND "
                "leaseExpiresAt < :now AND attribute_not_exists(tombstonedAt) AND "
                "#state <> :deleting AND #state <> :quarantined AND "
                "sessionId=:oldSession AND runtimeArn=:runtimeArn AND "
                "runtimeQualifier=:runtimeQualifier"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":newOwner": owner,
                ":newEpoch": new_epoch,
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
                ":oldSession": stale.session_id,
                ":runtimeArn": stale.runtime_arn,
                ":runtimeQualifier": stale.runtime_qualifier,
                ":stopOperation": stop_operation_id,
                ":mutation": mutation_id,
            },
        )

    def heartbeat(
        self, lease: RuntimeRecord, *, lease_ms: int
    ) -> RuntimeRecord:
        if lease_ms <= 0:
            raise ValueError("lease duration must be positive")
        now = int(self.clock_ms())
        until = now + lease_ms
        mutation_id = deterministic_id(
            "mut", "heartbeat", lease.user_id, lease.lease_epoch, until
        )
        return self._conditional_update(
            "heartbeat",
            user_id=lease.user_id,
            mutation_id=mutation_id,
            expected=lambda value: value.lease_owner == lease.lease_owner
            and value.lease_epoch == lease.lease_epoch
            and value.lease_expires_at == until,
            Key={"userId": lease.user_id},
            UpdateExpression=(
                "SET leaseExpiresAt=:until, updatedAt=:now, "
                "lastMutationId=:mutation"
            ),
            ConditionExpression=self._lease_condition(),
            ExpressionAttributeValues={
                ":until": until,
                ":now": now,
                ":owner": lease.lease_owner,
                ":epoch": lease.lease_epoch,
                ":oldSession": lease.session_id,
                ":runtimeArn": lease.runtime_arn,
                ":runtimeQualifier": lease.runtime_qualifier,
                ":mutation": mutation_id,
            },
        )

    def rotate_after_fence(
        self, lease: RuntimeRecord, *, session_id: str, lease_ms: int
    ) -> RuntimeRecord:
        now = int(self.clock_ms())
        session_id = canonical_session_id(session_id)
        mutation_id = deterministic_id(
            "mut",
            "rotate-after-fence",
            lease.user_id,
            lease.lease_epoch,
            lease.session_id,
            session_id,
            self.runtime_arn,
        )
        return self._conditional_update(
            "rotate-after-fence",
            user_id=lease.user_id,
            mutation_id=mutation_id,
            expected=lambda value: value.session_id == session_id
            and value.state is RuntimeState.BUSY
            and value.lease_owner == lease.lease_owner
            and value.lease_epoch == lease.lease_epoch
            and self.binding_matches(value),
            Key={"userId": lease.user_id},
            UpdateExpression=(
                "SET sessionId=:session, #state=:busy, leaseExpiresAt=:until, "
                "runtimeArn=:newRuntimeArn, runtimeQualifier=:newRuntimeQualifier, "
                "lastMutationId=:mutation, updatedAt=:now, "
                "revision=if_not_exists(revision,:zero)+:one "
                "REMOVE stopOperationId"
            ),
            ConditionExpression=(
                self._lease_condition() + " AND #state=:unhealthy"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":session": session_id,
                ":busy": RuntimeState.BUSY.value,
                ":until": now + lease_ms,
                ":now": now,
                ":zero": 0,
                ":one": 1,
                ":owner": lease.lease_owner,
                ":epoch": lease.lease_epoch,
                ":unhealthy": RuntimeState.UNHEALTHY.value,
                ":oldSession": lease.session_id,
                ":runtimeArn": lease.runtime_arn,
                ":runtimeQualifier": lease.runtime_qualifier,
                ":newRuntimeArn": self.runtime_arn,
                ":newRuntimeQualifier": self.runtime_qualifier,
                ":mutation": mutation_id,
            },
        )

    def finalize_success(
        self, lease: RuntimeRecord, *, invocation_id: str, receipt: dict
    ) -> RuntimeRecord:
        now = int(self.clock_ms())
        mutation_id = deterministic_id(
            "mut",
            "finalize-success",
            lease.user_id,
            lease.lease_epoch,
            invocation_id,
            receipt["generation"],
            receipt["manifestSha256"],
        )
        return self._conditional_update(
            "finalize-success",
            user_id=lease.user_id,
            mutation_id=mutation_id,
            expected=lambda value: value.state is RuntimeState.IDLE
            and value.last_invocation_id == invocation_id
            and value.last_workspace_generation == receipt["generation"]
            and value.last_workspace_manifest_sha256
            == receipt["manifestSha256"],
            Key={"userId": lease.user_id},
            UpdateExpression=(
                "SET #state=:idle, updatedAt=:now, lastInvocationId=:invocation, "
                "lastWorkspaceGeneration=:generation, "
                "lastWorkspaceManifestSha256=:sha, "
                "lastMutationId=:mutation, "
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
                ":oldSession": lease.session_id,
                ":runtimeArn": lease.runtime_arn,
                ":runtimeQualifier": lease.runtime_qualifier,
                ":mutation": mutation_id,
            },
        )

    def quarantine(self, lease: RuntimeRecord) -> RuntimeRecord:
        now = int(self.clock_ms())
        mutation_id = deterministic_id(
            "mut",
            "quarantine",
            lease.user_id,
            lease.session_id,
            lease.runtime_arn,
            lease.runtime_qualifier,
            lease.lease_owner,
            lease.lease_epoch,
        )
        if lease.lease_owner is None:
            owner_condition = "attribute_not_exists(leaseOwner)"
            owner_values = {}
        else:
            owner_condition = "leaseOwner=:owner"
            owner_values = {":owner": lease.lease_owner}
        return self._conditional_update(
            "quarantine",
            user_id=lease.user_id,
            mutation_id=mutation_id,
            expected=lambda value: value.state is RuntimeState.QUARANTINED
            and value.lease_owner is None,
            Key={"userId": lease.user_id},
            UpdateExpression=(
                "SET #state=:quarantined, updatedAt=:now, "
                "lastMutationId=:mutation, "
                "revision=if_not_exists(revision,:zero)+:one "
                "REMOVE leaseOwner, leaseExpiresAt"
            ),
            ConditionExpression=(
                f"{owner_condition} AND leaseEpoch=:epoch AND "
                "attribute_not_exists(tombstonedAt) AND "
                "sessionId=:oldSession AND runtimeArn=:runtimeArn AND "
                "runtimeQualifier=:runtimeQualifier AND #state <> :deleting"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                **owner_values,
                ":epoch": lease.lease_epoch,
                ":oldSession": lease.session_id,
                ":runtimeArn": lease.runtime_arn,
                ":runtimeQualifier": lease.runtime_qualifier,
                ":quarantined": RuntimeState.QUARANTINED.value,
                ":deleting": RuntimeState.DELETING.value,
                ":now": now,
                ":mutation": mutation_id,
                ":zero": 0,
                ":one": 1,
            },
        )

    def finalize_failure(
        self, lease: RuntimeRecord, *, state: RuntimeState
    ) -> RuntimeRecord:
        if state not in {RuntimeState.UNHEALTHY, RuntimeState.QUARANTINED}:
            raise ValueError("failure state must be UNHEALTHY or QUARANTINED")
        if state is RuntimeState.QUARANTINED:
            return self.quarantine(lease)
        now = int(self.clock_ms())
        mutation_id = deterministic_id(
            "mut", "finalize-unhealthy", lease.user_id, lease.lease_epoch
        )
        return self._conditional_update(
            "finalize-unhealthy",
            user_id=lease.user_id,
            mutation_id=mutation_id,
            expected=lambda value: value.state is RuntimeState.UNHEALTHY
            and value.lease_owner is None,
            Key={"userId": lease.user_id},
            UpdateExpression=(
                "SET #state=:state, updatedAt=:now, lastMutationId=:mutation, "
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
                ":oldSession": lease.session_id,
                ":runtimeArn": lease.runtime_arn,
                ":runtimeQualifier": lease.runtime_qualifier,
                ":mutation": mutation_id,
            },
        )

    def fence_binding_mismatch(
        self,
        stale: RuntimeRecord,
        *,
        owner: str,
        trace_id: str,
        lease_ms: int,
    ) -> RuntimeRecord:
        if self.binding_matches(stale):
            raise ValueError("runtime binding already matches")
        now = int(self.clock_ms())
        new_epoch = stale.lease_epoch + 1
        stop_operation_id = deterministic_id(
            "op",
            "binding-stop",
            stale.user_id,
            stale.session_id,
            stale.runtime_arn,
            stale.runtime_qualifier,
            new_epoch,
        )
        mutation_id = deterministic_id(
            "mut", "binding-fence", stop_operation_id, owner, trace_id
        )
        return self._conditional_update(
            "binding-fence",
            user_id=stale.user_id,
            mutation_id=mutation_id,
            expected=lambda value: value.state is RuntimeState.UNHEALTHY
            and value.lease_owner == owner
            and value.lease_epoch == new_epoch
            and value.stop_operation_id == stop_operation_id,
            Key={"userId": stale.user_id},
            UpdateExpression=(
                "SET leaseOwner=:owner, leaseEpoch=:newEpoch, "
                "leaseExpiresAt=:until, lastTraceId=:trace, #state=:unhealthy, "
                "stopOperationId=:stopOperation, lastMutationId=:mutation, "
                "updatedAt=:now, revision=if_not_exists(revision,:zero)+:one"
            ),
            ConditionExpression=(
                "attribute_not_exists(tombstonedAt) AND #state <> :deleting AND "
                "sessionId=:oldSession AND runtimeArn=:oldRuntimeArn AND "
                "runtimeQualifier=:oldRuntimeQualifier AND leaseEpoch=:oldEpoch AND "
                "(attribute_not_exists(leaseExpiresAt) OR leaseExpiresAt < :now)"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":owner": owner,
                ":newEpoch": new_epoch,
                ":oldEpoch": stale.lease_epoch,
                ":until": now + lease_ms,
                ":trace": trace_id,
                ":unhealthy": RuntimeState.UNHEALTHY.value,
                ":deleting": RuntimeState.DELETING.value,
                ":oldSession": stale.session_id,
                ":oldRuntimeArn": stale.runtime_arn,
                ":oldRuntimeQualifier": stale.runtime_qualifier,
                ":stopOperation": stop_operation_id,
                ":mutation": mutation_id,
                ":now": now,
                ":zero": 0,
                ":one": 1,
            },
        )

    def rotate_binding(
        self, lease: RuntimeRecord, *, session_id: str
    ) -> RuntimeRecord:
        session_id = canonical_session_id(session_id)
        now = int(self.clock_ms())
        mutation_id = deterministic_id(
            "mut",
            "rotate-binding",
            lease.user_id,
            lease.lease_epoch,
            lease.runtime_arn,
            self.runtime_arn,
            session_id,
        )
        return self._conditional_update(
            "rotate-binding",
            user_id=lease.user_id,
            mutation_id=mutation_id,
            expected=lambda value: value.session_id == session_id
            and value.state is RuntimeState.COLD
            and self.binding_matches(value)
            and value.lease_owner is None,
            Key={"userId": lease.user_id},
            UpdateExpression=(
                "SET sessionId=:newSession, runtimeArn=:newRuntimeArn, "
                "runtimeQualifier=:newRuntimeQualifier, #state=:cold, "
                "updatedAt=:now, lastMutationId=:mutation, "
                "revision=if_not_exists(revision,:zero)+:one "
                "REMOVE leaseOwner, leaseExpiresAt, stopOperationId"
            ),
            ConditionExpression=(
                "leaseOwner=:owner AND leaseEpoch=:epoch AND "
                "attribute_not_exists(tombstonedAt) AND #state=:unhealthy AND "
                "sessionId=:oldSession AND runtimeArn=:oldRuntimeArn AND "
                "runtimeQualifier=:oldRuntimeQualifier"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":owner": lease.lease_owner,
                ":epoch": lease.lease_epoch,
                ":oldSession": lease.session_id,
                ":oldRuntimeArn": lease.runtime_arn,
                ":oldRuntimeQualifier": lease.runtime_qualifier,
                ":newSession": session_id,
                ":newRuntimeArn": self.runtime_arn,
                ":newRuntimeQualifier": self.runtime_qualifier,
                ":unhealthy": RuntimeState.UNHEALTHY.value,
                ":cold": RuntimeState.COLD.value,
                ":now": now,
                ":mutation": mutation_id,
                ":zero": 0,
                ":one": 1,
            },
        )

    def begin_stop(
        self,
        current: RuntimeRecord,
        *,
        owner: str,
        trace_id: str,
        lease_ms: int,
    ) -> RuntimeRecord:
        if current.tombstoned_at is not None or current.state is RuntimeState.DELETING:
            raise TombstonedUser(current.user_id)
        now = int(self.clock_ms())
        new_epoch = current.lease_epoch + 1
        stop_operation_id = deterministic_id(
            "op",
            "explicit-stop",
            current.user_id,
            current.session_id,
            current.runtime_arn,
            current.runtime_qualifier,
            new_epoch,
        )
        mutation_id = deterministic_id(
            "mut", "begin-stop", stop_operation_id, owner, trace_id
        )
        return self._conditional_update(
            "begin-stop",
            user_id=current.user_id,
            mutation_id=mutation_id,
            expected=lambda value: value.state is RuntimeState.UNHEALTHY
            and value.lease_owner == owner
            and value.lease_epoch == new_epoch
            and value.stop_operation_id == stop_operation_id,
            Key={"userId": current.user_id},
            UpdateExpression=(
                "SET leaseOwner=:owner, leaseEpoch=:newEpoch, "
                "leaseExpiresAt=:until, lastTraceId=:trace, #state=:unhealthy, "
                "stopOperationId=:stopOperation, lastMutationId=:mutation, "
                "updatedAt=:now, revision=if_not_exists(revision,:zero)+:one"
            ),
            ConditionExpression=(
                "attribute_not_exists(tombstonedAt) AND #state <> :deleting AND "
                "sessionId=:oldSession AND runtimeArn=:runtimeArn AND "
                "runtimeQualifier=:runtimeQualifier AND leaseEpoch=:oldEpoch AND "
                "(attribute_not_exists(leaseExpiresAt) OR leaseExpiresAt < :now)"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":owner": owner,
                ":newEpoch": new_epoch,
                ":oldEpoch": current.lease_epoch,
                ":until": now + lease_ms,
                ":trace": trace_id,
                ":unhealthy": RuntimeState.UNHEALTHY.value,
                ":deleting": RuntimeState.DELETING.value,
                ":oldSession": current.session_id,
                ":runtimeArn": current.runtime_arn,
                ":runtimeQualifier": current.runtime_qualifier,
                ":stopOperation": stop_operation_id,
                ":mutation": mutation_id,
                ":now": now,
                ":zero": 0,
                ":one": 1,
            },
        )

    def rotate_after_stop(
        self, lease: RuntimeRecord, *, session_id: str
    ) -> RuntimeRecord:
        now = int(self.clock_ms())
        session_id = canonical_session_id(session_id)
        mutation_id = deterministic_id(
            "mut",
            "rotate-after-stop",
            lease.user_id,
            lease.lease_epoch,
            lease.session_id,
            session_id,
            self.runtime_arn,
        )
        return self._conditional_update(
            "rotate-after-stop",
            user_id=lease.user_id,
            mutation_id=mutation_id,
            expected=lambda value: value.session_id == session_id
            and value.state is RuntimeState.COLD
            and value.lease_owner is None
            and self.binding_matches(value),
            Key={"userId": lease.user_id},
            UpdateExpression=(
                "SET sessionId=:session, #state=:cold, runtimeArn=:newRuntimeArn, "
                "runtimeQualifier=:newRuntimeQualifier, updatedAt=:now, "
                "lastMutationId=:mutation, "
                "revision=if_not_exists(revision,:zero)+:one "
                "REMOVE leaseOwner, leaseExpiresAt, stopOperationId"
            ),
            ConditionExpression=(
                "leaseOwner=:owner AND leaseEpoch=:epoch AND "
                "attribute_not_exists(tombstonedAt) AND "
                "sessionId=:oldSession AND runtimeArn=:runtimeArn AND "
                "runtimeQualifier=:runtimeQualifier"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":session": session_id,
                ":cold": RuntimeState.COLD.value,
                ":now": now,
                ":zero": 0,
                ":one": 1,
                ":owner": lease.lease_owner,
                ":epoch": lease.lease_epoch,
                ":oldSession": lease.session_id,
                ":runtimeArn": lease.runtime_arn,
                ":runtimeQualifier": lease.runtime_qualifier,
                ":newRuntimeArn": self.runtime_arn,
                ":newRuntimeQualifier": self.runtime_qualifier,
                ":mutation": mutation_id,
            },
        )

    def begin_purge(
        self, user_id: str, *, owner: str, lease_ms: int
    ) -> RuntimeRecord:
        user_id = canonical_user_id(user_id)
        if lease_ms <= 0:
            raise ValueError("lease duration must be positive")
        now = int(self.clock_ms())
        candidate_operation_id = deterministic_id(
            "op", "purge", user_id, owner
        )
        mutation_id = deterministic_id(
            "mut", "begin-purge", user_id, owner, now
        )
        try:
            response = self.table.update_item(
                Key={"userId": user_id},
                UpdateExpression=(
                    "SET tombstonedAt=if_not_exists(tombstonedAt,:now), "
                    "#state=:deleting, purgeReason=:purgeReason, "
                    "leaseOwner=:owner, leaseExpiresAt=:until, "
                    "leaseEpoch=if_not_exists(leaseEpoch,:zero)+:one, "
                    "runtimeArn=if_not_exists(runtimeArn,:runtimeArn), "
                    "runtimeQualifier=if_not_exists(runtimeQualifier,:runtimeQualifier), "
                    "stopOperationId=if_not_exists(stopOperationId,:operation), "
                    "createdAt=if_not_exists(createdAt,:now), updatedAt=:now, "
                    "lastMutationId=:mutation, "
                    "revision=if_not_exists(revision,:zero)+:one"
                ),
                ConditionExpression=(
                    "attribute_not_exists(purgeCompletedAt) AND "
                    "(attribute_not_exists(leaseExpiresAt) OR leaseExpiresAt < :now)"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":now": now,
                    ":until": now + lease_ms,
                    ":deleting": RuntimeState.DELETING.value,
                    ":purgeReason": "ACCOUNT_DELETION",
                    ":owner": owner,
                    ":runtimeArn": self.runtime_arn,
                    ":runtimeQualifier": self.runtime_qualifier,
                    ":operation": candidate_operation_id,
                    ":mutation": mutation_id,
                    ":zero": 0,
                    ":one": 1,
                },
                ReturnValues="ALL_NEW",
            )
            return self._record(response["Attributes"])
        except Exception as error:
            if _is_conditional(error) or _is_ambiguous(error):
                try:
                    reconciled = self._reconcile(
                        user_id,
                        mutation_id,
                        lambda value: value.state is RuntimeState.DELETING
                        and value.tombstoned_at is not None
                        and value.purge_reason == "ACCOUNT_DELETION"
                        and value.lease_owner == owner,
                    )
                except Exception:
                    reconciled = None
                if reconciled is not None:
                    return reconciled
            if not _is_conditional(error):
                if _is_ambiguous(error):
                    raise RuntimeStateError(
                        "runtime purge fencing outcome is uncertain"
                    ) from error
                raise RuntimeStateError("runtime purge fencing failed") from error
        current = self.get(user_id)
        if current is None:
            raise RuntimeUnavailable("runtime does not exist")
        if (
            current.tombstoned_at is not None
            and current.state is RuntimeState.DELETING
            and current.session_id is None
            and current.lease_owner is None
        ):
            return current
        raise LeaseBusy(user_id)

    def begin_inactive_purge(
        self,
        user_id: str,
        *,
        owner: str,
        lease_ms: int,
        observed_updated_at_ms: int,
        observed_revision: int,
        inactive_before_ms: int,
    ) -> RuntimeRecord:
        """Tombstone only the exact inactive snapshot selected by retention."""

        user_id = canonical_user_id(user_id)
        if not isinstance(owner, str) or not owner:
            raise ValueError("inactive purge owner is required")
        integer_fields = {
            "lease duration": lease_ms,
            "observed update millisecond": observed_updated_at_ms,
            "observed revision": observed_revision,
            "inactive cutoff millisecond": inactive_before_ms,
        }
        for label, value in integer_fields.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label} is invalid")
        if observed_updated_at_ms > inactive_before_ms:
            raise ValueError("observed runtime is not inactive")
        raw_now = self.clock_ms()
        if isinstance(raw_now, bool) or not isinstance(raw_now, int) or raw_now <= 0:
            raise RuntimeStateError("runtime millisecond clock is invalid")
        now = raw_now
        operation_id = deterministic_id(
            "op",
            "inactive-purge",
            user_id,
            observed_updated_at_ms,
            observed_revision,
        )
        mutation_id = deterministic_id(
            "mut", "begin-inactive-purge", operation_id, owner, now
        )
        try:
            response = self.table.update_item(
                Key={"userId": user_id},
                UpdateExpression=(
                    "SET tombstonedAt=if_not_exists(tombstonedAt,:now), "
                    "#state=:deleting, purgeReason=:purgeReason, "
                    "purgeObservedUpdatedAt=:observedUpdatedAt, "
                    "purgeObservedRevision=:observedRevision, "
                    "purgeInactiveBefore=:inactiveBefore, "
                    "leaseOwner=:owner, leaseExpiresAt=:until, "
                    "leaseEpoch=if_not_exists(leaseEpoch,:zero)+:one, "
                    "stopOperationId=:operation, updatedAt=:now, "
                    "lastMutationId=:mutation, "
                    "revision=if_not_exists(revision,:zero)+:one"
                ),
                ConditionExpression=(
                    "attribute_exists(userId) AND ("
                    "(attribute_not_exists(tombstonedAt) AND "
                    "#state <> :deleting AND updatedAt=:observedUpdatedAt AND "
                    "revision=:observedRevision AND updatedAt <= :inactiveBefore) OR "
                    "(attribute_exists(tombstonedAt) AND #state=:deleting AND "
                    "purgeReason=:purgeReason AND "
                    "purgeObservedUpdatedAt=:observedUpdatedAt AND "
                    "purgeObservedRevision=:observedRevision AND "
                    "purgeInactiveBefore=:inactiveBefore AND "
                    "attribute_not_exists(purgeCompletedAt))) AND "
                    "runtimeArn=:runtimeArn AND "
                    "runtimeQualifier=:runtimeQualifier AND "
                    "((attribute_not_exists(leaseOwner) AND "
                    "attribute_not_exists(leaseExpiresAt)) OR "
                    "(attribute_exists(leaseOwner) AND leaseExpiresAt < :now))"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":now": now,
                    ":until": now + lease_ms,
                    ":deleting": RuntimeState.DELETING.value,
                    ":purgeReason": "WORKSPACE_EXPIRY",
                    ":owner": owner,
                    ":runtimeArn": self.runtime_arn,
                    ":runtimeQualifier": self.runtime_qualifier,
                    ":observedUpdatedAt": observed_updated_at_ms,
                    ":observedRevision": observed_revision,
                    ":inactiveBefore": inactive_before_ms,
                    ":operation": operation_id,
                    ":mutation": mutation_id,
                    ":zero": 0,
                    ":one": 1,
                },
                ReturnValues="ALL_NEW",
            )
            return self._record(response["Attributes"])
        except Exception as error:
            if _is_conditional(error) or _is_ambiguous(error):
                try:
                    reconciled = self._reconcile(
                        user_id,
                        mutation_id,
                        lambda value: value.state is RuntimeState.DELETING
                        and value.tombstoned_at is not None
                        and value.purge_reason == "WORKSPACE_EXPIRY"
                        and value.lease_owner == owner
                        and value.stop_operation_id == operation_id,
                    )
                except Exception:
                    reconciled = None
                if reconciled is not None:
                    return reconciled
            if _is_conditional(error):
                try:
                    current = self.get(user_id)
                except Exception:
                    current = None
                if (
                    current is not None
                    and current.state is RuntimeState.DELETING
                    and current.tombstoned_at is not None
                    and current.purge_reason == "WORKSPACE_EXPIRY"
                    and current.purge_observed_updated_at == observed_updated_at_ms
                    and current.purge_observed_revision == observed_revision
                    and current.purge_inactive_before == inactive_before_ms
                    and current.purge_completed_at is None
                ):
                    raise LeaseBusy(user_id) from error
                raise InactivityFenceLost(user_id) from error
            if _is_ambiguous(error):
                raise RuntimeStateError(
                    "inactive runtime purge fencing outcome is uncertain"
                ) from error
            raise RuntimeStateError("inactive runtime purge fencing failed") from error

    def finish_purge(self, lease: RuntimeRecord) -> RuntimeRecord:
        now = int(self.clock_ms())
        mutation_id = deterministic_id(
            "mut",
            "finish-purge",
            lease.user_id,
            lease.lease_epoch,
            lease.stop_operation_id,
        )
        if lease.session_id is None and lease.lease_owner is None:
            return lease
        session_condition = (
            "sessionId=:oldSession"
            if lease.session_id is not None
            else "attribute_not_exists(sessionId)"
        )
        session_values = (
            {":oldSession": lease.session_id}
            if lease.session_id is not None
            else {}
        )
        return self._conditional_update(
            "finish-purge",
            user_id=lease.user_id,
            mutation_id=mutation_id,
            expected=lambda value: value.tombstoned_at is not None
            and value.state is RuntimeState.DELETING
            and value.purge_reason == "ACCOUNT_DELETION"
            and value.purge_completed_at is not None
            and value.session_id is None
            and value.lease_owner is None,
            Key={"userId": lease.user_id},
            UpdateExpression=(
                "SET purgeCompletedAt=:now, updatedAt=:now, lastMutationId=:mutation, "
                "revision=if_not_exists(revision,:zero)+:one "
                "REMOVE sessionId, leaseOwner, leaseExpiresAt, lastTraceId, "
                "lastInvocationId, lastWorkspaceGeneration, "
                "lastWorkspaceManifestSha256"
            ),
            ConditionExpression=(
                "leaseOwner=:owner AND leaseEpoch=:epoch AND "
                "attribute_exists(tombstonedAt) AND #state=:deleting AND "
                "purgeReason=:accountReason AND "
                f"{session_condition} AND runtimeArn=:runtimeArn AND "
                "runtimeQualifier=:runtimeQualifier"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":owner": lease.lease_owner,
                ":epoch": lease.lease_epoch,
                ":deleting": RuntimeState.DELETING.value,
                ":accountReason": "ACCOUNT_DELETION",
                **session_values,
                ":runtimeArn": lease.runtime_arn,
                ":runtimeQualifier": lease.runtime_qualifier,
                ":now": now,
                ":mutation": mutation_id,
                ":zero": 0,
                ":one": 1,
            },
        )

    def finish_inactive_stop(self, lease: RuntimeRecord) -> RuntimeRecord:
        """Record a verified stop while retaining the workspace-expiry fence."""

        now = int(self.clock_ms())
        mutation_id = deterministic_id(
            "mut",
            "finish-inactive-stop",
            lease.user_id,
            lease.lease_epoch,
            lease.stop_operation_id,
        )
        if (
            lease.session_id is None
            and lease.lease_owner is None
            and lease.workspace_stop_verified_at is not None
        ):
            return lease
        session_condition = (
            "sessionId=:oldSession"
            if lease.session_id is not None
            else "attribute_not_exists(sessionId)"
        )
        session_values = (
            {":oldSession": lease.session_id}
            if lease.session_id is not None
            else {}
        )
        return self._conditional_update(
            "finish-inactive-stop",
            user_id=lease.user_id,
            mutation_id=mutation_id,
            expected=lambda value: value.tombstoned_at is not None
            and value.state is RuntimeState.DELETING
            and value.purge_reason == "WORKSPACE_EXPIRY"
            and value.workspace_stop_verified_at is not None
            and value.session_id is None
            and value.lease_owner is None,
            Key={"userId": lease.user_id},
            UpdateExpression=(
                "SET workspaceStopVerifiedAt=:now, updatedAt=:now, "
                "lastMutationId=:mutation, "
                "revision=if_not_exists(revision,:zero)+:one "
                "REMOVE sessionId, leaseOwner, leaseExpiresAt, lastTraceId, "
                "lastInvocationId"
            ),
            ConditionExpression=(
                "leaseOwner=:owner AND leaseEpoch=:epoch AND "
                "attribute_exists(tombstonedAt) AND #state=:deleting AND "
                "purgeReason=:workspaceReason AND "
                f"{session_condition} AND runtimeArn=:runtimeArn AND "
                "runtimeQualifier=:runtimeQualifier"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":owner": lease.lease_owner,
                ":epoch": lease.lease_epoch,
                ":deleting": RuntimeState.DELETING.value,
                ":workspaceReason": "WORKSPACE_EXPIRY",
                **session_values,
                ":runtimeArn": lease.runtime_arn,
                ":runtimeQualifier": lease.runtime_qualifier,
                ":now": now,
                ":mutation": mutation_id,
                ":zero": 0,
                ":one": 1,
            },
        )

    def complete_workspace_expiry(
        self, current: RuntimeRecord, *, session_id: str
    ) -> RuntimeRecord:
        """Clear only a verified workspace tombstone into a fresh cold session."""

        if (
            current.state is not RuntimeState.DELETING
            or current.tombstoned_at is None
            or current.purge_reason != "WORKSPACE_EXPIRY"
            or current.workspace_stop_verified_at is None
            or current.session_id is not None
            or current.lease_owner is not None
        ):
            raise LeaseLost("workspace-expiry-complete")
        session_id = canonical_session_id(session_id)
        now = int(self.clock_ms())
        mutation_id = deterministic_id(
            "mut",
            "complete-workspace-expiry",
            current.user_id,
            current.revision,
            current.workspace_stop_verified_at,
            session_id,
        )
        return self._conditional_update(
            "complete-workspace-expiry",
            user_id=current.user_id,
            mutation_id=mutation_id,
            expected=lambda value: value.state is RuntimeState.COLD
            and value.session_id == session_id
            and value.tombstoned_at is None
            and value.purge_reason is None
            and value.last_purge_reason == "WORKSPACE_EXPIRY",
            Key={"userId": current.user_id},
            UpdateExpression=(
                "SET sessionId=:session, #state=:cold, updatedAt=:now, "
                "lastPurgeReason=:workspaceReason, lastPurgeCompletedAt=:now, "
                "lastMutationId=:mutation, "
                "revision=if_not_exists(revision,:zero)+:one "
                "REMOVE tombstonedAt, purgeReason, purgeCompletedAt, "
                "workspaceStopVerifiedAt, purgeObservedUpdatedAt, "
                "purgeObservedRevision, purgeInactiveBefore, stopOperationId, "
                "lastTraceId, lastInvocationId, lastWorkspaceGeneration, "
                "lastWorkspaceManifestSha256"
            ),
            ConditionExpression=(
                "revision=:revision AND updatedAt=:updatedAt AND "
                "attribute_exists(tombstonedAt) AND #state=:deleting AND "
                "purgeReason=:workspaceReason AND "
                "workspaceStopVerifiedAt=:stopVerifiedAt AND "
                "attribute_not_exists(sessionId) AND "
                "attribute_not_exists(leaseOwner) AND "
                "attribute_not_exists(leaseExpiresAt) AND "
                "runtimeArn=:runtimeArn AND runtimeQualifier=:runtimeQualifier"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":session": session_id,
                ":cold": RuntimeState.COLD.value,
                ":deleting": RuntimeState.DELETING.value,
                ":workspaceReason": "WORKSPACE_EXPIRY",
                ":revision": current.revision,
                ":updatedAt": current.updated_at,
                ":stopVerifiedAt": current.workspace_stop_verified_at,
                ":runtimeArn": current.runtime_arn,
                ":runtimeQualifier": current.runtime_qualifier,
                ":now": now,
                ":mutation": mutation_id,
                ":zero": 0,
                ":one": 1,
            },
        )

    def mark_purge_uncertain(self, lease: RuntimeRecord) -> RuntimeRecord:
        now = int(self.clock_ms())
        mutation_id = deterministic_id(
            "mut",
            "purge-uncertain",
            lease.user_id,
            lease.lease_epoch,
            lease.stop_operation_id,
        )
        session_condition = (
            "sessionId=:oldSession"
            if lease.session_id is not None
            else "attribute_not_exists(sessionId)"
        )
        session_values = (
            {":oldSession": lease.session_id}
            if lease.session_id is not None
            else {}
        )
        return self._conditional_update(
            "purge-uncertain",
            user_id=lease.user_id,
            mutation_id=mutation_id,
            expected=lambda value: value.state is RuntimeState.DELETING
            and value.tombstoned_at is not None
            and value.lease_owner is None,
            Key={"userId": lease.user_id},
            UpdateExpression=(
                "SET #state=:deleting, updatedAt=:now, lastMutationId=:mutation, "
                "revision=if_not_exists(revision,:zero)+:one "
                "REMOVE leaseOwner, leaseExpiresAt"
            ),
            ConditionExpression=(
                "leaseOwner=:owner AND leaseEpoch=:epoch AND "
                "attribute_exists(tombstonedAt) AND "
                f"{session_condition} AND runtimeArn=:runtimeArn AND "
                "runtimeQualifier=:runtimeQualifier"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":owner": lease.lease_owner,
                ":epoch": lease.lease_epoch,
                ":deleting": RuntimeState.DELETING.value,
                **session_values,
                ":runtimeArn": lease.runtime_arn,
                ":runtimeQualifier": lease.runtime_qualifier,
                ":now": now,
                ":mutation": mutation_id,
                ":zero": 0,
                ":one": 1,
            },
        )
