"""Runtime driver and single-item state boundary tests."""

from __future__ import annotations

import io
from pathlib import Path
import subprocess
import sys
import threading
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from runtime_driver import (
    AgentCoreAdapter,
    AgentCoreStopUncertain,
    RuntimeDriver,
    RuntimeInvocationUncertain,
)
from runtime_state import (
    ALL_RUNTIME_STATES,
    LeaseBusy,
    LeaseLost,
    RuntimeRecord,
    RuntimeState,
    RuntimeStateError,
    RuntimeStateRepository,
    RuntimeUnavailable,
    StaleLease,
    TombstonedUser,
)


USER = "user_test_01"
SESSION = "ses_0123456789abcdef0123456789abcdef"
NEW_SESSION = "ses_fedcba9876543210fedcba9876543210"
TRACE = "po1_" + "a" * 64
GENERATION = "g-12345678-1234-4123-8123-123456789abc"
SHA256 = "b" * 64
RECEIPT = {"generation": GENERATION, "manifestSha256": SHA256}
RUNTIME_ARN_V1 = (
    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:agent/"
    "12345678-1234-1234-1234-123456789abc:1"
)
RUNTIME_ARN_V2 = (
    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:agent/"
    "12345678-1234-1234-1234-123456789abc:2"
)
OTHER_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:agent/"
    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee:1"
)


class AwsServiceError(Exception):
    def __init__(self, code: str, message: str = "service error") -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


def conditional_error(operation: str = "UpdateItem") -> AwsServiceError:
    return AwsServiceError("ConditionalCheckFailedException", operation)


def not_found_error() -> AwsServiceError:
    return AwsServiceError("ResourceNotFoundException", "gone")


def record(**changes) -> RuntimeRecord:
    base = RuntimeRecord(
        user_id=USER,
        session_id=SESSION,
        runtime_arn=RUNTIME_ARN_V2,
        runtime_qualifier="DEFAULT",
        state=RuntimeState.COLD,
        revision=1,
        lease_owner=None,
        lease_epoch=0,
        lease_expires_at=None,
        last_trace_id=None,
        last_invocation_id=None,
        last_workspace_generation=None,
        last_workspace_manifest_sha256=None,
        created_at=1_000,
        updated_at=1_000,
        tombstoned_at=None,
        last_mutation_id=None,
        stop_operation_id=None,
    )
    return replace(base, **changes)


def item_from_record(value: RuntimeRecord) -> dict:
    item = {
        "userId": value.user_id,
        "runtimeArn": value.runtime_arn,
        "runtimeQualifier": value.runtime_qualifier,
        "state": value.state.value,
        "revision": value.revision,
        "leaseEpoch": value.lease_epoch,
        "createdAt": value.created_at,
        "updatedAt": value.updated_at,
    }
    optional = {
        "sessionId": value.session_id,
        "leaseOwner": value.lease_owner,
        "leaseExpiresAt": value.lease_expires_at,
        "lastTraceId": value.last_trace_id,
        "lastInvocationId": value.last_invocation_id,
        "lastWorkspaceGeneration": value.last_workspace_generation,
        "lastWorkspaceManifestSha256": value.last_workspace_manifest_sha256,
        "tombstonedAt": value.tombstoned_at,
        "lastMutationId": value.last_mutation_id,
        "stopOperationId": value.stop_operation_id,
    }
    item.update({key: field for key, field in optional.items() if field is not None})
    return item


def test_runtime_states_are_the_frozen_public_set():
    assert ALL_RUNTIME_STATES == frozenset(
        {
            "COLD",
            "STARTING",
            "READY",
            "BUSY",
            "IDLE",
            "UNHEALTHY",
            "QUARANTINED",
            "DELETING",
        }
    )


def test_runtime_driver_imports_from_the_deployed_lambda_package_root():
    lambda_root = str(Path("lambda").resolve())
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                f"import sys; sys.path.insert(0, {lambda_root!r}); "
                "import router.runtime_driver"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_ensure_conditionally_creates_server_session_then_reads_race_winner():
    table = MagicMock()
    table.put_item.side_effect = conditional_error("PutItem")
    table.get_item.return_value = {
        "Item": {
            "userId": USER,
            "sessionId": SESSION,
            "runtimeArn": RUNTIME_ARN_V2,
            "runtimeQualifier": "DEFAULT",
            "state": "COLD",
            "revision": 1,
            "leaseEpoch": 0,
            "createdAt": 1_000,
            "updatedAt": 1_000,
        }
    }
    repo = RuntimeStateRepository(
        table,
        runtime_arn=RUNTIME_ARN_V2,
        runtime_qualifier="DEFAULT",
        clock_ms=lambda: 1_000,
        session_id_factory=lambda: NEW_SESSION,
    )

    winner = repo.ensure(USER)

    assert winner.session_id == SESSION
    candidate = table.put_item.call_args.kwargs["Item"]
    assert candidate["sessionId"] == NEW_SESSION
    assert candidate["state"] == "COLD"
    assert "tombstonedAt" not in candidate
    assert table.put_item.call_args.kwargs["ConditionExpression"] == (
        "attribute_not_exists(userId)"
    )
    table.get_item.assert_called_once_with(
        Key={"userId": USER}, ConsistentRead=True
    )


def test_ensure_refuses_same_item_tombstone_after_create_race():
    table = MagicMock()
    table.put_item.side_effect = conditional_error("PutItem")
    table.get_item.return_value = {
        "Item": {
            "userId": USER,
            "runtimeArn": RUNTIME_ARN_V2,
            "runtimeQualifier": "DEFAULT",
            "state": "DELETING",
            "revision": 4,
            "leaseEpoch": 3,
            "createdAt": 1,
            "updatedAt": 2,
            "tombstonedAt": 2,
        }
    }
    repo = RuntimeStateRepository(
        table,
        runtime_arn=RUNTIME_ARN_V2,
        runtime_qualifier="DEFAULT",
        clock_ms=lambda: 3,
    )

    with pytest.raises(TombstonedUser):
        repo.ensure(USER)


def test_ensure_persists_exact_runtime_binding_in_the_same_atomic_item():
    table = MagicMock()
    table.put_item.return_value = {}
    repo = RuntimeStateRepository(
        table,
        runtime_arn=RUNTIME_ARN_V2,
        runtime_qualifier="DEFAULT",
        clock_ms=lambda: 1_000,
        session_id_factory=lambda: NEW_SESSION,
    )

    created = repo.ensure(USER)

    assert created.runtime_arn == RUNTIME_ARN_V2
    assert created.runtime_qualifier == "DEFAULT"
    item = table.put_item.call_args.kwargs["Item"]
    assert item["runtimeArn"] == RUNTIME_ARN_V2
    assert item["runtimeQualifier"] == "DEFAULT"
    assert item["lastMutationId"].startswith("mut_")


def test_lease_and_finalize_conditions_bind_exact_runtime_and_session():
    table = MagicMock()
    table.update_item.side_effect = conditional_error()
    table.get_item.return_value = {
        "Item": {
            "userId": USER,
            "sessionId": SESSION,
            "runtimeArn": RUNTIME_ARN_V2,
            "runtimeQualifier": "DEFAULT",
            "state": "BUSY",
            "revision": 4,
            "leaseOwner": "other",
            "leaseEpoch": 9,
            "leaseExpiresAt": 10_001,
            "createdAt": 1,
            "updatedAt": 2,
        }
    }
    repo = RuntimeStateRepository(
        table,
        runtime_arn=RUNTIME_ARN_V2,
        runtime_qualifier="DEFAULT",
        clock_ms=lambda: 10_000,
    )

    with pytest.raises(LeaseBusy):
        repo.acquire(USER, owner="mine", trace_id=TRACE, lease_ms=30_000)
    acquire = table.update_item.call_args.kwargs
    assert "runtimeArn=:runtimeArn" in acquire["ConditionExpression"]
    assert "runtimeQualifier=:runtimeQualifier" in acquire["ConditionExpression"]

    table.update_item.reset_mock()
    table.update_item.side_effect = conditional_error()
    lease = record(
        runtime_arn=RUNTIME_ARN_V2,
        runtime_qualifier="DEFAULT",
        state=RuntimeState.BUSY,
        lease_owner="owner",
        lease_epoch=7,
        lease_expires_at=20_000,
    )
    with pytest.raises(LeaseLost):
        repo.finalize_success(lease, invocation_id=TRACE, receipt=RECEIPT)
    finalize = table.update_item.call_args.kwargs
    assert "sessionId=:oldSession" in finalize["ConditionExpression"]
    assert "runtimeArn=:runtimeArn" in finalize["ConditionExpression"]
    assert "runtimeQualifier=:runtimeQualifier" in finalize["ConditionExpression"]


def test_acquire_uses_one_conditional_update_and_reports_live_owner():
    table = MagicMock()
    table.update_item.side_effect = conditional_error()
    table.get_item.return_value = {
        "Item": {
            "userId": USER,
            "sessionId": SESSION,
            "runtimeArn": RUNTIME_ARN_V2,
            "runtimeQualifier": "DEFAULT",
            "state": "BUSY",
            "revision": 4,
            "leaseOwner": "other",
            "leaseEpoch": 9,
            "leaseExpiresAt": 10_001,
            "createdAt": 1,
            "updatedAt": 2,
        }
    }
    repo = RuntimeStateRepository(
        table,
        runtime_arn=RUNTIME_ARN_V2,
        runtime_qualifier="DEFAULT",
        clock_ms=lambda: 10_000,
    )

    with pytest.raises(LeaseBusy):
        repo.acquire(USER, owner="mine", trace_id=TRACE, lease_ms=30_000)

    kwargs = table.update_item.call_args.kwargs
    assert "attribute_not_exists(tombstonedAt)" in kwargs["ConditionExpression"]
    assert "leaseOwner = :owner" in kwargs["ConditionExpression"]
    assert "leaseExpiresAt < :now" not in kwargs["ConditionExpression"]
    assert "leaseEpoch=if_not_exists(leaseEpoch,:zero)+:one" in kwargs[
        "UpdateExpression"
    ]
    assert kwargs["ReturnValues"] == "ALL_NEW"


def test_acquire_returns_stale_lease_without_overwriting_it():
    table = MagicMock()
    table.update_item.side_effect = conditional_error()
    stale = {
        "userId": USER,
        "sessionId": SESSION,
        "runtimeArn": RUNTIME_ARN_V2,
        "runtimeQualifier": "DEFAULT",
        "state": "BUSY",
        "revision": 4,
        "leaseOwner": "old",
        "leaseEpoch": 9,
        "leaseExpiresAt": 9_999,
        "createdAt": 1,
        "updatedAt": 2,
    }
    table.get_item.return_value = {"Item": stale}
    repo = RuntimeStateRepository(
        table,
        runtime_arn=RUNTIME_ARN_V2,
        runtime_qualifier="DEFAULT",
        clock_ms=lambda: 10_000,
    )

    with pytest.raises(StaleLease) as failure:
        repo.acquire(USER, owner="mine", trace_id=TRACE, lease_ms=30_000)

    assert failure.value.record.lease_owner == "old"
    assert failure.value.record.lease_epoch == 9
    assert table.update_item.call_count == 1


def test_finalize_and_release_are_fenced_by_owner_and_epoch():
    table = MagicMock()
    table.update_item.side_effect = conditional_error()
    repo = RuntimeStateRepository(
        table,
        runtime_arn=RUNTIME_ARN_V2,
        runtime_qualifier="DEFAULT",
        clock_ms=lambda: 10_000,
    )
    lease = record(
        state=RuntimeState.BUSY,
        lease_owner="owner",
        lease_epoch=7,
        lease_expires_at=20_000,
    )

    with pytest.raises(LeaseLost):
        repo.finalize_success(lease, invocation_id=TRACE, receipt=RECEIPT)

    condition = table.update_item.call_args.kwargs["ConditionExpression"]
    update = table.update_item.call_args.kwargs["UpdateExpression"]
    values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert "leaseOwner=:owner" in condition
    assert "leaseEpoch=:epoch" in condition
    assert "attribute_not_exists(tombstonedAt)" in condition
    assert values[":owner"] == "owner"
    assert values[":epoch"] == 7
    assert "lastWorkspaceGeneration=:generation" in update
    assert "lastWorkspaceManifestSha256=:sha" in update
    assert "lastSnapshot" not in update


def test_heartbeat_and_finalize_require_the_unexpired_owner_epoch_fence():
    table = MagicMock()
    table.update_item.return_value = {
        "Attributes": {
            "userId": USER,
            "sessionId": SESSION,
            "runtimeArn": RUNTIME_ARN_V2,
            "runtimeQualifier": "DEFAULT",
            "state": "BUSY",
            "revision": 2,
            "leaseOwner": "owner",
            "leaseEpoch": 7,
            "leaseExpiresAt": 40_000,
            "createdAt": 1,
            "updatedAt": 10_000,
        }
    }
    repo = RuntimeStateRepository(
        table,
        runtime_arn=RUNTIME_ARN_V2,
        runtime_qualifier="DEFAULT",
        clock_ms=lambda: 10_000,
    )
    lease = record(
        state=RuntimeState.BUSY,
        lease_owner="owner",
        lease_epoch=7,
        lease_expires_at=20_000,
    )

    repo.heartbeat(lease, lease_ms=30_000)

    kwargs = table.update_item.call_args.kwargs
    assert "leaseOwner=:owner" in kwargs["ConditionExpression"]
    assert "leaseEpoch=:epoch" in kwargs["ConditionExpression"]
    assert "leaseExpiresAt >= :now" in kwargs["ConditionExpression"]
    assert kwargs["ExpressionAttributeValues"][":until"] == 40_000


def test_begin_purge_atomically_tombstones_even_when_runtime_never_existed():
    table = MagicMock()
    table.update_item.return_value = {
        "Attributes": {
            "userId": USER,
            "runtimeArn": RUNTIME_ARN_V2,
            "runtimeQualifier": "DEFAULT",
            "state": "DELETING",
            "revision": 1,
            "leaseOwner": "purger",
            "leaseEpoch": 1,
            "leaseExpiresAt": 40_000,
            "createdAt": 10_000,
            "updatedAt": 10_000,
            "tombstonedAt": 10_000,
        }
    }
    repo = RuntimeStateRepository(
        table,
        runtime_arn=RUNTIME_ARN_V2,
        runtime_qualifier="DEFAULT",
        clock_ms=lambda: 10_000,
    )

    tombstone = repo.begin_purge(USER, owner="purger", lease_ms=30_000)

    assert tombstone.session_id is None
    kwargs = table.update_item.call_args.kwargs
    assert "attribute_exists(userId)" not in kwargs["ConditionExpression"]
    assert "createdAt=if_not_exists(createdAt,:now)" in kwargs["UpdateExpression"]
    assert kwargs["ExpressionAttributeValues"][":until"] == 40_000


def test_begin_purge_is_resumable_with_one_durable_stop_operation():
    table = MagicMock()
    table.update_item.return_value = {
        "Attributes": item_from_record(
            record(
                state=RuntimeState.DELETING,
                tombstoned_at=5_000,
                lease_owner="new-purger",
                lease_epoch=8,
                lease_expires_at=40_000,
                stop_operation_id="op_" + "4" * 64,
            )
        )
    }
    repo = RuntimeStateRepository(
        table,
        runtime_arn=RUNTIME_ARN_V2,
        runtime_qualifier="DEFAULT",
        clock_ms=lambda: 10_000,
    )

    resumed = repo.begin_purge(USER, owner="new-purger", lease_ms=30_000)

    assert resumed.stop_operation_id == "op_" + "4" * 64
    kwargs = table.update_item.call_args.kwargs
    assert "tombstonedAt=if_not_exists(tombstonedAt,:now)" in kwargs[
        "UpdateExpression"
    ]
    assert "stopOperationId=if_not_exists(stopOperationId,:operation)" in kwargs[
        "UpdateExpression"
    ]
    assert "#state=:deleting" not in kwargs["ConditionExpression"]
    assert "leaseExpiresAt < :now" in kwargs["ConditionExpression"]


def test_already_finished_tombstone_makes_purge_idempotently_complete():
    table = MagicMock()
    table.update_item.side_effect = conditional_error()
    completed = record(
        session_id=None,
        state=RuntimeState.DELETING,
        tombstoned_at=5_000,
        lease_owner=None,
        lease_expires_at=None,
        stop_operation_id="op_" + "4" * 64,
    )
    table.get_item.return_value = {"Item": item_from_record(completed)}
    repo = RuntimeStateRepository(
        table,
        runtime_arn=RUNTIME_ARN_V2,
        runtime_qualifier="DEFAULT",
        clock_ms=lambda: 10_000,
    )

    result = repo.begin_purge(USER, owner="retry", lease_ms=30_000)

    assert result == completed
    assert table.get_item.call_count >= 1
    assert all(
        call.kwargs == {"Key": {"userId": USER}, "ConsistentRead": True}
        for call in table.get_item.call_args_list
    )


def test_ambiguous_purge_stop_releases_lease_but_keeps_deleting_tombstone():
    table = MagicMock()
    table.update_item.side_effect = conditional_error()
    repo = RuntimeStateRepository(
        table,
        runtime_arn=RUNTIME_ARN_V2,
        runtime_qualifier="DEFAULT",
        clock_ms=lambda: 50_000,
    )
    purging = record(
        state=RuntimeState.DELETING,
        tombstoned_at=10_000,
        lease_owner="purger",
        lease_epoch=4,
        lease_expires_at=40_000,
        stop_operation_id="op_" + "4" * 64,
    )

    with pytest.raises(LeaseLost):
        repo.mark_purge_uncertain(purging)

    kwargs = table.update_item.call_args.kwargs
    assert kwargs["ExpressionAttributeValues"][":deleting"] == "DELETING"
    assert kwargs["ExpressionAttributeValues"].get(":quarantined") is None
    assert "REMOVE leaseOwner, leaseExpiresAt" in kwargs["UpdateExpression"]


def test_ambiguous_finalize_reconciles_the_exact_deterministic_mutation():
    table = MagicMock()
    lease = record(
        state=RuntimeState.BUSY,
        lease_owner="owner",
        lease_epoch=7,
        lease_expires_at=40_000,
    )

    def ambiguous_finalize(**kwargs):
        mutation = kwargs["ExpressionAttributeValues"][":mutation"]
        table.get_item.return_value = {
            "Item": item_from_record(
                replace(
                    lease,
                    state=RuntimeState.IDLE,
                    lease_owner=None,
                    lease_expires_at=None,
                    last_invocation_id=TRACE,
                    last_workspace_generation=GENERATION,
                    last_workspace_manifest_sha256=SHA256,
                    last_mutation_id=mutation,
                )
            )
        }
        raise TimeoutError("response lost")

    table.update_item.side_effect = ambiguous_finalize
    repo = RuntimeStateRepository(
        table,
        runtime_arn=RUNTIME_ARN_V2,
        runtime_qualifier="DEFAULT",
        clock_ms=lambda: 10_000,
    )

    result = repo.finalize_success(lease, invocation_id=TRACE, receipt=RECEIPT)

    assert result.state is RuntimeState.IDLE
    assert result.last_mutation_id.startswith("mut_")
    table.get_item.assert_called_once_with(
        Key={"userId": USER}, ConsistentRead=True
    )


def test_ambiguous_stale_fence_reconciles_owner_epoch_and_stop_operation():
    table = MagicMock()
    stale = record(
        state=RuntimeState.BUSY,
        lease_owner="old",
        lease_epoch=7,
        lease_expires_at=9_000,
    )

    def ambiguous_fence(**kwargs):
        values = kwargs["ExpressionAttributeValues"]
        table.get_item.return_value = {
            "Item": item_from_record(
                replace(
                    stale,
                    state=RuntimeState.UNHEALTHY,
                    lease_owner="new",
                    lease_epoch=8,
                    lease_expires_at=40_000,
                    last_trace_id=TRACE,
                    stop_operation_id=values[":stopOperation"],
                    last_mutation_id=values[":mutation"],
                )
            )
        }
        raise TimeoutError("response lost")

    table.update_item.side_effect = ambiguous_fence
    repo = RuntimeStateRepository(
        table,
        runtime_arn=RUNTIME_ARN_V2,
        runtime_qualifier="DEFAULT",
        clock_ms=lambda: 10_000,
    )

    result = repo.fence_stale(
        stale, owner="new", trace_id=TRACE, lease_ms=30_000
    )

    assert result.lease_epoch == 8
    assert result.stop_operation_id.startswith("op_")


def test_ambiguous_rotation_reconciles_new_session_and_exact_binding():
    table = MagicMock()
    fenced = record(
        state=RuntimeState.UNHEALTHY,
        lease_owner="new",
        lease_epoch=8,
        lease_expires_at=40_000,
        stop_operation_id="op_" + "4" * 64,
    )

    def ambiguous_rotate(**kwargs):
        values = kwargs["ExpressionAttributeValues"]
        table.get_item.return_value = {
            "Item": item_from_record(
                replace(
                    fenced,
                    session_id=NEW_SESSION,
                    state=RuntimeState.BUSY,
                    stop_operation_id=None,
                    last_mutation_id=values[":mutation"],
                )
            )
        }
        raise TimeoutError("response lost")

    table.update_item.side_effect = ambiguous_rotate
    repo = RuntimeStateRepository(
        table,
        runtime_arn=RUNTIME_ARN_V2,
        runtime_qualifier="DEFAULT",
        clock_ms=lambda: 10_000,
    )

    result = repo.rotate_after_fence(
        fenced, session_id=NEW_SESSION, lease_ms=30_000
    )

    assert result.session_id == NEW_SESSION
    assert result.runtime_arn == RUNTIME_ARN_V2
    assert result.state is RuntimeState.BUSY


def test_ambiguous_begin_purge_reconciles_tombstone_and_operation():
    table = MagicMock()

    def ambiguous_purge(**kwargs):
        values = kwargs["ExpressionAttributeValues"]
        table.get_item.return_value = {
            "Item": item_from_record(
                record(
                    state=RuntimeState.DELETING,
                    tombstoned_at=10_000,
                    lease_owner="purger",
                    lease_epoch=1,
                    lease_expires_at=40_000,
                    stop_operation_id=values[":operation"],
                    last_mutation_id=values[":mutation"],
                )
            )
        }
        raise TimeoutError("response lost")

    table.update_item.side_effect = ambiguous_purge
    repo = RuntimeStateRepository(
        table,
        runtime_arn=RUNTIME_ARN_V2,
        runtime_qualifier="DEFAULT",
        clock_ms=lambda: 10_000,
    )

    result = repo.begin_purge(USER, owner="purger", lease_ms=30_000)

    assert result.tombstoned_at == 10_000
    assert result.stop_operation_id.startswith("op_")


class FakeRepository:
    def __init__(self, initial: RuntimeRecord | None = None):
        self.current = initial or record()
        self.runtime_arn = RUNTIME_ARN_V2
        self.runtime_qualifier = "DEFAULT"
        self.events = []
        self.acquire_error = None
        self.finalize_error = None
        self.heartbeat_event = threading.Event()

    def ensure(self, user_id):
        self.events.append(("ensure", user_id))
        if self.current.tombstoned_at is not None:
            raise TombstonedUser(user_id)
        return self.current

    def get(self, user_id):
        self.events.append(("get", user_id))
        return self.current

    def binding_matches(self, value):
        return (
            value.runtime_arn == self.runtime_arn
            and value.runtime_qualifier == self.runtime_qualifier
        )

    def acquire(self, user_id, *, owner, trace_id, lease_ms):
        self.events.append(("acquire", owner, trace_id, lease_ms))
        if self.acquire_error:
            raise self.acquire_error
        self.current = replace(
            self.current,
            state=RuntimeState.BUSY,
            lease_owner=owner,
            lease_epoch=self.current.lease_epoch + 1,
            lease_expires_at=50_000,
            last_trace_id=trace_id,
        )
        return self.current

    def fence_stale(self, stale, *, owner, trace_id, lease_ms):
        self.events.append(("fence", stale.lease_owner, stale.lease_epoch, owner))
        self.current = replace(
            stale,
            state=RuntimeState.UNHEALTHY,
            lease_owner=owner,
            lease_epoch=stale.lease_epoch + 1,
            lease_expires_at=50_000,
            last_trace_id=trace_id,
            stop_operation_id="op_" + "7" * 64,
        )
        return self.current

    def fence_binding_mismatch(self, stale, *, owner, trace_id, lease_ms):
        self.events.append(("binding-fence", stale.runtime_arn, stale.lease_epoch, owner))
        self.current = replace(
            stale,
            state=RuntimeState.UNHEALTHY,
            lease_owner=owner,
            lease_epoch=stale.lease_epoch + 1,
            lease_expires_at=50_000,
            last_trace_id=trace_id,
            stop_operation_id="op_" + "9" * 64,
        )
        return self.current

    def rotate_binding(self, lease, *, session_id):
        self.events.append(("binding-rotate", lease.lease_epoch, session_id))
        self.current = replace(
            lease,
            session_id=session_id,
            runtime_arn=self.runtime_arn,
            runtime_qualifier=self.runtime_qualifier,
            state=RuntimeState.COLD,
            lease_owner=None,
            lease_expires_at=None,
            stop_operation_id=None,
        )
        return self.current

    def rotate_after_fence(self, lease, *, session_id, lease_ms):
        self.events.append(("rotate", lease.lease_epoch, session_id))
        self.current = replace(lease, session_id=session_id, state=RuntimeState.BUSY)
        return self.current

    def finalize_success(self, lease, *, invocation_id, receipt):
        self.events.append(("success", lease.lease_owner, lease.lease_epoch, receipt))
        if self.finalize_error:
            raise self.finalize_error
        self.current = replace(
            lease,
            state=RuntimeState.IDLE,
            lease_owner=None,
            lease_expires_at=None,
            last_invocation_id=invocation_id,
            last_workspace_generation=receipt["generation"],
            last_workspace_manifest_sha256=receipt["manifestSha256"],
        )
        return self.current

    def heartbeat(self, lease, *, lease_ms):
        self.events.append(("heartbeat", lease.lease_owner, lease.lease_epoch))
        self.heartbeat_event.set()
        return lease

    def finalize_failure(self, lease, *, state):
        self.events.append(("failure", lease.lease_owner, lease.lease_epoch, state))
        self.current = replace(
            lease, state=state, lease_owner=None, lease_expires_at=None
        )
        return self.current

    def quarantine(self, lease):
        self.events.append(("quarantine", lease.lease_owner, lease.lease_epoch))
        self.current = replace(
            lease,
            state=RuntimeState.QUARANTINED,
            lease_owner=None,
            lease_expires_at=None,
        )
        return self.current

    def begin_stop(self, current, *, owner, trace_id, lease_ms):
        self.events.append(("begin-stop", current.state, owner, trace_id))
        self.current = replace(
            current,
            state=RuntimeState.UNHEALTHY,
            lease_owner=owner,
            lease_epoch=current.lease_epoch + 1,
            lease_expires_at=50_000,
            last_trace_id=trace_id,
            stop_operation_id="op_" + "8" * 64,
        )
        return self.current

    def rotate_after_stop(self, lease, *, session_id):
        self.events.append(("stopped", lease.lease_epoch, session_id))
        self.current = replace(
            lease,
            session_id=session_id,
            state=RuntimeState.COLD,
            lease_owner=None,
            lease_expires_at=None,
        )
        return self.current

    def begin_purge(self, user_id, *, owner, lease_ms):
        self.events.append(("purge", user_id, owner))
        self.current = replace(
            self.current,
            state=RuntimeState.DELETING,
            tombstoned_at=40_000,
            lease_owner=owner,
            lease_epoch=self.current.lease_epoch + 1,
            lease_expires_at=50_000,
            stop_operation_id="op_" + "6" * 64,
        )
        return self.current

    def finish_purge(self, lease):
        self.events.append(("purged", lease.lease_epoch))
        self.current = replace(lease, session_id=None, lease_owner=None)
        return self.current


class FakeAdapter:
    def __init__(self, response=None):
        self.runtime_arn = RUNTIME_ARN_V2
        self.qualifier = "DEFAULT"
        self.response = response or {
            "status": "ok",
            "internalUserId": USER,
            "response": "ok",
            "workspaceReceipt": RECEIPT,
        }
        self.events = []
        self.error = None
        self.stop_error = None

    def invoke(self, *, session_id, user_id, payload, trace_id):
        self.events.append(("invoke", session_id, user_id, payload, trace_id))
        if self.error:
            raise self.error
        return self.response

    def stop(
        self,
        *,
        session_id,
        operation_id=None,
        runtime_arn=None,
        qualifier=None,
    ):
        self.events.append(
            (
                "stop",
                session_id,
                operation_id,
                runtime_arn or self.runtime_arn,
                qualifier or self.qualifier,
            )
        )
        if self.stop_error:
            raise self.stop_error
        return {"stopped": True, "notFound": False}


def build_driver(repo=None, adapter=None):
    ids = iter(["owner-a", "owner-b", "owner-c"])
    sessions = iter([NEW_SESSION, "ses_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"])
    return RuntimeDriver(
        repository=repo or FakeRepository(),
        adapter=adapter or FakeAdapter(),
        owner_factory=lambda: next(ids),
        session_id_factory=lambda: next(sessions),
        lease_ms=30_000,
        max_execution_ms=20_000,
    )


def test_driver_heartbeats_long_running_invocation_under_same_fence():
    repo = FakeRepository()

    class WaitingAdapter(FakeAdapter):
        def invoke(self, **kwargs):
            assert repo.heartbeat_event.wait(timeout=1)
            return super().invoke(**kwargs)

    driver = RuntimeDriver(
        repository=repo,
        adapter=WaitingAdapter(),
        owner_factory=lambda: "owner-heartbeat",
        session_id_factory=lambda: NEW_SESSION,
        lease_ms=30_000,
        max_execution_ms=20_000,
        heartbeat_interval_ms=1,
    )

    driver.invoke(USER, {"message": "slow"}, TRACE)

    assert any(event[0] == "heartbeat" for event in repo.events)


@pytest.mark.parametrize(
    "forbidden",
    [
        "sessionId",
        "runtimeSessionId",
        "userId",
        "namespace",
        "leaseOwner",
        "leaseEpoch",
        "invocationId",
    ],
)
def test_invoke_rejects_client_controlled_authority_fields(forbidden):
    adapter = FakeAdapter()
    driver = build_driver(adapter=adapter)

    with pytest.raises(ValueError):
        driver.invoke(USER, {"message": "hello", forbidden: "attacker"}, TRACE)

    assert adapter.events == []


def test_successful_chat_uses_server_session_and_persists_bridge_receipt():
    repo = FakeRepository()
    adapter = FakeAdapter()
    driver = build_driver(repo, adapter)

    result = driver.invoke(
        USER,
        {"message": "hello", "actorId": "telegram:5", "channel": "telegram"},
        TRACE,
    )

    assert result["response"] == "ok"
    call = adapter.events[0]
    assert call[0:3] == ("invoke", SESSION, USER)
    assert call[3] == {
        "action": "chat",
        "internalUserId": USER,
        "namespace": USER,
        "actorId": "telegram:5",
        "channel": "telegram",
        "message": "hello",
        "invocationId": TRACE,
    }
    assert repo.current.state is RuntimeState.IDLE
    assert repo.current.last_workspace_generation == GENERATION


def test_driver_requires_exact_identity_and_action_specific_chat_status():
    for response in (
        {
            "status": "ok",
            "internalUserId": "user_other_01",
            "response": "wrong user",
            "workspaceReceipt": RECEIPT,
        },
        {
            "internalUserId": USER,
            "response": "missing status",
            "workspaceReceipt": RECEIPT,
        },
        {
            "status": "snapshotted",
            "internalUserId": USER,
            "response": "wrong action status",
            "workspaceReceipt": RECEIPT,
        },
    ):
        repo = FakeRepository()
        with pytest.raises(RuntimeInvocationUncertain):
            build_driver(repo, FakeAdapter(response)).invoke(
                USER, {"message": "hello"}, TRACE
            )
        assert repo.current.state is RuntimeState.QUARANTINED


@pytest.mark.parametrize(
    "response",
    [
        {
            "status": "ok",
            "internalUserId": USER,
            "response": "ok",
            "workspaceReceipt": RECEIPT,
            "unexpected": "field",
        },
        {
            "status": "ok",
            "internalUserId": USER,
            "response": "ok",
            "errorCode": "SHOULD_NOT_EXIST",
            "workspaceReceipt": RECEIPT,
        },
        {
            "status": "failed",
            "internalUserId": USER,
            "response": "failed",
            "errorCode": "x" * 65,
            "workspaceReceipt": RECEIPT,
        },
        {
            "status": "ok",
            "internalUserId": USER,
            "response": "ok",
            "workspaceReceipt": {**RECEIPT, "unexpected": "field"},
        },
    ],
)
def test_chat_response_rejects_unknown_fields_and_unbounded_error_metadata(response):
    repo = FakeRepository()

    with pytest.raises(RuntimeInvocationUncertain):
        build_driver(repo, FakeAdapter(response)).invoke(
            USER, {"message": "hello"}, TRACE
        )

    assert repo.current.state is RuntimeState.QUARANTINED


def test_committed_bridge_failure_is_a_definite_deliverable_not_a_retry_signal():
    repo = FakeRepository()
    response = {
        "status": "failed",
        "internalUserId": USER,
        "response": "The run failed before producing an answer.",
        "errorCode": "AGENT_RUN_FAILED",
        "workspaceReceipt": RECEIPT,
    }

    result = build_driver(repo, FakeAdapter(response)).invoke(
        USER, {"message": "hello"}, TRACE
    )

    assert result == response
    assert repo.current.state is RuntimeState.IDLE
    assert repo.current.last_invocation_id == TRACE


def test_chat_response_text_has_a_strict_utf8_bound():
    repo = FakeRepository()
    response = {
        "status": "ok",
        "internalUserId": USER,
        "response": "x" * 100_001,
        "workspaceReceipt": RECEIPT,
    }

    with pytest.raises(RuntimeInvocationUncertain, match="response"):
        build_driver(repo, FakeAdapter(response)).invoke(
            USER, {"message": "hello"}, TRACE
        )

    assert repo.current.state is RuntimeState.QUARANTINED


@pytest.mark.parametrize(
    "response",
    [
        {"response": "unproven"},
        {"response": "bad", "workspaceReceipt": {"generation": "bad", "manifestSha256": SHA256}},
        {"response": "retry", "status": "retryable", "workspaceReceipt": RECEIPT},
        {"response": "uncertain", "status": "uncertain", "workspaceReceipt": RECEIPT},
    ],
)
def test_unproven_or_uncertain_chat_quarantines_instead_of_marking_idle(response):
    repo = FakeRepository()
    driver = build_driver(repo, FakeAdapter(response))

    with pytest.raises(RuntimeInvocationUncertain):
        driver.invoke(USER, {"message": "hello"}, TRACE)

    assert repo.current.state is RuntimeState.QUARANTINED
    assert repo.current.last_workspace_generation is None


def test_transport_timeout_quarantines_and_is_not_retried():
    repo = FakeRepository()
    adapter = FakeAdapter()
    adapter.error = TimeoutError("ambiguous")
    driver = build_driver(repo, adapter)

    with pytest.raises(RuntimeInvocationUncertain):
        driver.invoke(USER, {"message": "hello"}, TRACE)

    assert len([event for event in adapter.events if event[0] == "invoke"]) == 1
    assert repo.current.state is RuntimeState.QUARANTINED


def test_quarantine_write_failure_does_not_mask_uncertain_invocation():
    repo = FakeRepository()

    def unavailable_quarantine(_lease):
        raise RuntimeStateError("quarantine outcome uncertain")

    repo.quarantine = unavailable_quarantine
    adapter = FakeAdapter()
    adapter.error = TimeoutError("ambiguous")

    with pytest.raises(RuntimeInvocationUncertain, match="outcome is unknown"):
        build_driver(repo, adapter).invoke(USER, {"message": "hello"}, TRACE)

    assert len([event for event in adapter.events if event[0] == "invoke"]) == 1


def test_stale_takeover_fences_stops_old_session_and_rotates_before_invoke():
    stale = record(
        state=RuntimeState.BUSY,
        lease_owner="old-owner",
        lease_epoch=8,
        lease_expires_at=9_000,
    )
    repo = FakeRepository(stale)
    repo.acquire_error = StaleLease(stale)
    adapter = FakeAdapter()
    driver = build_driver(repo, adapter)

    driver.invoke(USER, {"message": "hello"}, TRACE)

    assert repo.events[1][0] == "acquire"
    assert repo.events[2][0] == "fence"
    assert adapter.events[0][0:3] == (
        "stop",
        SESSION,
        "op_" + "7" * 64,
    )
    assert repo.events[3][0] == "heartbeat"
    assert repo.events[4] == ("rotate", 9, NEW_SESSION)
    assert adapter.events[1][0:3] == ("invoke", NEW_SESSION, USER)


def test_same_trace_stale_lease_is_quarantined_without_reexecution():
    stale = record(
        state=RuntimeState.BUSY,
        lease_owner="old-owner",
        lease_epoch=8,
        lease_expires_at=9_000,
        last_trace_id=TRACE,
    )
    repo = FakeRepository(stale)
    repo.acquire_error = StaleLease(stale)
    adapter = FakeAdapter()

    with pytest.raises(RuntimeInvocationUncertain, match="trace|replay|duplicate"):
        build_driver(repo, adapter).invoke(USER, {"message": "hello"}, TRACE)

    assert repo.current.state is RuntimeState.QUARANTINED
    assert adapter.events == []


def test_runtime_version_mismatch_fences_stops_recorded_version_then_rotates_current():
    old = record(
        runtime_arn=RUNTIME_ARN_V1,
        runtime_qualifier="DEFAULT",
        state=RuntimeState.IDLE,
    )
    repo = FakeRepository(old)
    adapter = FakeAdapter()
    driver = build_driver(repo, adapter)

    current = driver.ensure(USER)

    assert repo.events[1][0] == "binding-fence"
    assert repo.events[2][0] == "heartbeat"
    stop = adapter.events[0]
    assert stop[0:2] == ("stop", SESSION)
    assert stop[2] == "op_" + "9" * 64
    assert stop[3:5] == (RUNTIME_ARN_V1, "DEFAULT")
    assert repo.events[3] == ("binding-rotate", 1, NEW_SESSION)
    assert current.runtime_arn == RUNTIME_ARN_V2
    assert current.session_id == NEW_SESSION
    assert current.state is RuntimeState.COLD


def test_cross_lineage_record_is_quarantined_and_never_stopped():
    foreign = record(runtime_arn=OTHER_RUNTIME_ARN, state=RuntimeState.IDLE)
    repo = FakeRepository(foreign)
    adapter = FakeAdapter()

    with pytest.raises(RuntimeUnavailable, match="lineage"):
        build_driver(repo, adapter).ensure(USER)

    assert repo.current.state is RuntimeState.QUARANTINED
    assert adapter.events == []


def test_ambiguous_stale_stop_quarantines_and_never_invokes():
    stale = record(
        state=RuntimeState.BUSY,
        lease_owner="old-owner",
        lease_epoch=8,
        lease_expires_at=9_000,
    )
    repo = FakeRepository(stale)
    repo.acquire_error = StaleLease(stale)
    adapter = FakeAdapter()
    adapter.stop_error = AgentCoreStopUncertain("unknown")
    driver = build_driver(repo, adapter)

    with pytest.raises(RuntimeInvocationUncertain):
        driver.invoke(USER, {"message": "hello"}, TRACE)

    assert repo.current.state is RuntimeState.QUARANTINED
    assert [event[0] for event in adapter.events] == ["stop"]


def test_finalize_fence_loss_quarantines_call_and_never_overwrites_successor():
    repo = FakeRepository()
    repo.finalize_error = LeaseLost(USER)
    driver = build_driver(repo, FakeAdapter())

    with pytest.raises(RuntimeInvocationUncertain):
        driver.invoke(USER, {"message": "hello"}, TRACE)

    assert not any(event[0] == "failure" for event in repo.events)


def test_unreconciled_finalize_write_is_exposed_as_uncertain_not_a_raw_state_error():
    repo = FakeRepository()
    repo.finalize_error = RuntimeStateError("finalize outcome uncertain")

    with pytest.raises(RuntimeInvocationUncertain, match="lease fence"):
        build_driver(repo, FakeAdapter()).invoke(USER, {"message": "hello"}, TRACE)


def test_snapshot_is_a_trusted_runtime_action_and_requires_receipt():
    repo = FakeRepository(record(state=RuntimeState.IDLE))
    adapter = FakeAdapter(
        {
            "status": "snapshotted",
            "internalUserId": USER,
            "workspaceReceipt": RECEIPT,
        }
    )
    driver = build_driver(repo, adapter)

    receipt = driver.snapshot(USER)

    assert receipt == RECEIPT
    assert adapter.events[0][3] == {
        "action": "snapshot",
        "internalUserId": USER,
        "namespace": USER,
    }
    assert repo.current.last_workspace_manifest_sha256 == SHA256


def test_snapshot_requires_exact_identity_and_snapshotted_status():
    for response in (
        {"status": "ok", "internalUserId": USER, "workspaceReceipt": RECEIPT},
        {
            "status": "snapshotted",
            "internalUserId": "user_other_01",
            "workspaceReceipt": RECEIPT,
        },
    ):
        repo = FakeRepository(record(state=RuntimeState.IDLE))
        with pytest.raises(RuntimeInvocationUncertain):
            build_driver(repo, FakeAdapter(response)).snapshot(USER)
        assert repo.current.state is RuntimeState.QUARANTINED


def test_snapshot_response_rejects_unknown_fields():
    repo = FakeRepository(record(state=RuntimeState.IDLE))
    response = {
        "status": "snapshotted",
        "internalUserId": USER,
        "workspaceReceipt": RECEIPT,
        "response": "unexpected",
    }

    with pytest.raises(RuntimeInvocationUncertain):
        build_driver(repo, FakeAdapter(response)).snapshot(USER)

    assert repo.current.state is RuntimeState.QUARANTINED


@pytest.mark.parametrize(
    "input_request",
    [
        {"message": 7},
        {"message": "x" * 131_073},
        {"message": "x" * 130_900},
        {"message": "hello", "actorId": 5},
        {"message": "hello", "actorId": "x" * 257},
        {"message": "hello", "channel": "email"},
        {"message": {"text": "hi", "images": []}},
        {
            "message": {
                "text": "hi",
                "images": [
                    {
                        "s3Key": "another-user/_uploads/a.png",
                        "contentType": "image/png",
                    }
                ],
            }
        },
        {"message": {"text": "hi", "images": [{"s3Key": object()}]}},
    ],
)
def test_request_values_and_json_bounds_are_validated_before_lease(input_request):
    repo = FakeRepository()
    adapter = FakeAdapter()
    driver = build_driver(repo, adapter)

    with pytest.raises(ValueError):
        driver.invoke(USER, input_request, TRACE)

    assert repo.events == []
    assert adapter.events == []


def test_driver_synchronously_heartbeats_fence_immediately_before_invoke():
    repo = FakeRepository()

    class FencedAdapter(FakeAdapter):
        def invoke(self, **kwargs):
            assert repo.events[-1][0] == "heartbeat"
            return super().invoke(**kwargs)

    build_driver(repo, FencedAdapter()).invoke(USER, {"message": "hello"}, TRACE)


def test_lease_must_outlive_the_entire_lambda_execution_authority():
    with pytest.raises(ValueError, match="execution"):
        RuntimeDriver(
            repository=FakeRepository(),
            adapter=FakeAdapter(),
            lease_ms=300_000,
            max_execution_ms=300_000,
        )


def test_driver_refuses_repository_and_adapter_binding_disagreement():
    repo = FakeRepository()
    repo.runtime_arn = RUNTIME_ARN_V1

    with pytest.raises(ValueError, match="binding"):
        RuntimeDriver(
            repository=repo,
            adapter=FakeAdapter(),
            lease_ms=30_000,
            max_execution_ms=20_000,
        )


def test_status_is_logical_table_state_and_does_not_call_compute_status_api():
    repo = FakeRepository(record(state=RuntimeState.IDLE))
    adapter = FakeAdapter()
    driver = build_driver(repo, adapter)

    status = driver.status(USER)

    assert status["state"] == "IDLE"
    assert status["sessionId"] == SESSION
    assert adapter.events == []


def test_stop_uses_stored_session_then_rotates_to_fresh_cold_mapping():
    repo = FakeRepository(record(state=RuntimeState.IDLE))
    adapter = FakeAdapter()
    driver = build_driver(repo, adapter)

    stopped = driver.stop(USER)

    assert adapter.events[0][0:3] == (
        "stop",
        SESSION,
        "op_" + "8" * 64,
    )
    assert stopped["state"] == "COLD"
    assert stopped["sessionId"] == NEW_SESSION


@pytest.mark.parametrize("failed_state", [RuntimeState.UNHEALTHY, RuntimeState.QUARANTINED])
def test_stop_is_the_recovery_path_for_unhealthy_and_quarantined_runtime(failed_state):
    repo = FakeRepository(record(state=failed_state))
    adapter = FakeAdapter()

    result = build_driver(repo, adapter).stop(USER)

    assert repo.events[1][0] == "begin-stop"
    assert repo.events[2][0] == "heartbeat"
    assert adapter.events[0][0:2] == ("stop", SESSION)
    assert adapter.events[0][2] == "op_" + "8" * 64
    assert result["state"] == "COLD"


def test_quarantine_cas_keeps_owner_epoch_binding_after_lease_expiry():
    table = MagicMock()
    table.update_item.side_effect = conditional_error()
    repo = RuntimeStateRepository(
        table,
        runtime_arn=RUNTIME_ARN_V2,
        runtime_qualifier="DEFAULT",
        clock_ms=lambda: 50_000,
    )
    expired = record(
        state=RuntimeState.BUSY,
        lease_owner="owner",
        lease_epoch=4,
        lease_expires_at=40_000,
    )

    with pytest.raises(LeaseLost):
        repo.quarantine(expired)

    kwargs = table.update_item.call_args.kwargs
    condition = kwargs["ConditionExpression"]
    assert "leaseOwner=:owner" in condition
    assert "leaseEpoch=:epoch" in condition
    assert "attribute_not_exists(tombstonedAt)" in condition
    assert "leaseExpiresAt >= :now" not in condition
    assert "runtimeArn=:runtimeArn" in condition
    assert "runtimeQualifier=:runtimeQualifier" in condition


def test_purge_tombstones_before_stop_and_refuses_future_ensure():
    repo = FakeRepository(record(state=RuntimeState.IDLE))
    adapter = FakeAdapter()
    driver = build_driver(repo, adapter)

    result = driver.purge(USER)

    assert repo.events[0][0] == "purge"
    assert adapter.events[0][0:3] == (
        "stop",
        SESSION,
        "op_" + "6" * 64,
    )
    assert result["state"] == "DELETING"
    with pytest.raises(TombstonedUser):
        driver.ensure(USER)


def test_agentcore_adapter_uses_exact_region_runtime_default_and_server_session():
    client = MagicMock()
    client.meta.region_name = "eu-west-1"
    client.invoke_agent_runtime.return_value = {
        "statusCode": 200,
        "runtimeSessionId": SESSION,
        "response": io.BytesIO(
            b'{"response":"ok","workspaceReceipt":{"generation":"'
            + GENERATION.encode()
            + b'","manifestSha256":"'
            + SHA256.encode()
            + b'"}}'
        ),
    }
    adapter = AgentCoreAdapter(
        client,
        runtime_arn="arn:aws:bedrock-agentcore:eu-west-1:123456789012:agent/12345678-1234-1234-1234-123456789abc:1",
        qualifier="DEFAULT",
        region="eu-west-1",
    )
    payload = {"action": "chat", "message": "hello"}

    response = adapter.invoke(
        session_id=SESSION, user_id=USER, payload=payload, trace_id=TRACE
    )

    assert response["workspaceReceipt"] == RECEIPT
    client.invoke_agent_runtime.assert_called_once_with(
        agentRuntimeArn=adapter.runtime_arn,
        qualifier="DEFAULT",
        runtimeSessionId=SESSION,
        runtimeUserId=USER,
        traceId=TRACE,
        payload=b'{"action":"chat","message":"hello"}',
        contentType="application/json",
        accept="application/json",
    )


def test_agentcore_adapter_rejects_duplicate_json_response_keys():
    client = MagicMock()
    client.meta.region_name = "eu-west-1"
    client.invoke_agent_runtime.return_value = {
        "statusCode": 200,
        "runtimeSessionId": SESSION,
        "response": io.BytesIO(b'{"status":"ok","status":"failed"}'),
    }
    adapter = AgentCoreAdapter(
        client,
        runtime_arn=RUNTIME_ARN_V1,
        qualifier="DEFAULT",
        region="eu-west-1",
    )

    with pytest.raises(RuntimeInvocationUncertain, match="invalid JSON"):
        adapter.invoke(
            session_id=SESSION,
            user_id=USER,
            payload={"action": "chat"},
            trace_id=TRACE,
        )


def test_agentcore_stop_uses_deterministic_valid_token_and_accepts_not_found():
    client = MagicMock()
    client.meta.region_name = "eu-west-1"
    client.stop_runtime_session.side_effect = not_found_error()
    adapter = AgentCoreAdapter(
        client,
        runtime_arn="arn:aws:bedrock-agentcore:eu-west-1:123456789012:agent/12345678-1234-1234-1234-123456789abc:1",
        qualifier="DEFAULT",
        region="eu-west-1",
    )

    result = adapter.stop(
        session_id=SESSION, operation_id="op_" + "0" * 64
    )

    assert result == {"stopped": True, "notFound": True}
    kwargs = client.stop_runtime_session.call_args.kwargs
    assert kwargs["agentRuntimeArn"] == adapter.runtime_arn
    assert kwargs["qualifier"] == "DEFAULT"
    assert kwargs["runtimeSessionId"] == SESSION
    assert len(kwargs["clientToken"]) >= 33
    assert kwargs["clientToken"].isalnum()


def test_agentcore_adapter_rejects_wrong_region_before_client_call():
    client = MagicMock()
    client.meta.region_name = "us-east-1"

    with pytest.raises(RuntimeError, match="eu-west-1"):
        AgentCoreAdapter(
            client,
            runtime_arn="arn:test",
            qualifier="DEFAULT",
            region="eu-west-1",
        )

    client.invoke_agent_runtime.assert_not_called()


@pytest.mark.parametrize(
    "runtime_arn",
    [
        "prefix:eu-west-1:not-an-arn",
        "arn:aws:bedrock-agentcore:eu-west-1:123:agent/not-a-uuid:1",
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:agent/"
        "12345678-1234-1234-1234-123456789abc:1",
        "arn:aws:bedrock-agentcore:eu-west-1:123456789012:agent/"
        "12345678-1234-1234-1234-123456789abc:0",
    ],
)
def test_agentcore_adapter_requires_the_full_exact_runtime_arn_grammar(runtime_arn):
    client = MagicMock()
    client.meta.region_name = "eu-west-1"

    with pytest.raises((RuntimeError, ValueError), match="runtime ARN"):
        AgentCoreAdapter(
            client,
            runtime_arn=runtime_arn,
            qualifier="DEFAULT",
            region="eu-west-1",
        )


def test_agentcore_invoke_requires_exact_outer_session_identity():
    client = MagicMock()
    client.meta.region_name = "eu-west-1"
    client.invoke_agent_runtime.return_value = {
        "statusCode": 200,
        "response": io.BytesIO(
            b'{"status":"ok","internalUserId":"user_test_01",'
            b'"response":"ok","workspaceReceipt":{"generation":"'
            + GENERATION.encode()
            + b'","manifestSha256":"'
            + SHA256.encode()
            + b'"}}'
        ),
    }
    adapter = AgentCoreAdapter(
        client,
        runtime_arn=RUNTIME_ARN_V1,
        qualifier="DEFAULT",
        region="eu-west-1",
    )

    with pytest.raises(RuntimeInvocationUncertain, match="session"):
        adapter.invoke(
            session_id=SESSION,
            user_id=USER,
            payload={"action": "chat", "message": "hello"},
            trace_id=TRACE,
        )


def test_agentcore_stop_token_is_stable_per_operation_and_changes_for_later_stop():
    client = MagicMock()
    client.meta.region_name = "eu-west-1"
    client.stop_runtime_session.return_value = {
        "statusCode": 200,
        "runtimeSessionId": SESSION,
    }
    adapter = AgentCoreAdapter(
        client,
        runtime_arn=RUNTIME_ARN_V2,
        qualifier="DEFAULT",
        region="eu-west-1",
    )

    adapter.stop(session_id=SESSION, operation_id="op_" + "1" * 64)
    first = client.stop_runtime_session.call_args.kwargs["clientToken"]
    adapter.stop(session_id=SESSION, operation_id="op_" + "1" * 64)
    replay = client.stop_runtime_session.call_args.kwargs["clientToken"]
    adapter.stop(session_id=SESSION, operation_id="op_" + "2" * 64)
    later = client.stop_runtime_session.call_args.kwargs["clientToken"]

    assert first == replay
    assert later != first


def test_agentcore_stop_requires_exact_outer_session_identity():
    client = MagicMock()
    client.meta.region_name = "eu-west-1"
    client.stop_runtime_session.return_value = {"statusCode": 200}
    adapter = AgentCoreAdapter(
        client,
        runtime_arn=RUNTIME_ARN_V1,
        qualifier="DEFAULT",
        region="eu-west-1",
    )

    with pytest.raises(AgentCoreStopUncertain, match="session"):
        adapter.stop(session_id=SESSION, operation_id="op_" + "3" * 64)


def test_worker_stack_provisions_exact_runtime_state_boundary():
    from aws_cdk import App, Environment
    from aws_cdk.assertions import Template

    from stacks.router_stack import RouterStack

    account = "123456789012"
    runtime_iam_arn = (
        "arn:aws:bedrock-agentcore:eu-west-1:123456789012:"
        "runtime/openclaw_agent-0123456789"
    )
    app = App(context={"registration_open": "false"})
    stack = RouterStack(
        app,
        "RouterTask3",
        runtime_arn=(
            "arn:aws:bedrock-agentcore:eu-west-1:123456789012:agent/"
            "12345678-1234-1234-1234-123456789abc:1"
        ),
        runtime_iam_arn=runtime_iam_arn,
        runtime_endpoint_name="DEFAULT",
        telegram_token_secret_name="openclaw/channels/telegram",
        slack_token_secret_name="openclaw/channels/slack",
        feishu_token_secret_name="openclaw/channels/feishu",
        webhook_secret_name="openclaw/webhook-secret",
        cmk_arn=f"arn:aws:kms:eu-west-1:{account}:key/test-key",
        user_files_bucket_name="openclaw-user-files-test",
        user_files_bucket_arn="arn:aws:s3:::openclaw-user-files-test",
        env=Environment(account=account, region="eu-west-1"),
    )
    template = Template.from_stack(stack).to_json()
    resources = template["Resources"]

    runtime_table_id, runtime_table = next(
        (logical_id, resource)
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::DynamoDB::Table"
        and resource["Properties"].get("TableName") == "personal-operator-runtime-state"
    )
    assert runtime_table["Properties"]["KeySchema"] == [
        {"AttributeName": "userId", "KeyType": "HASH"}
    ]
    assert runtime_table["Properties"]["BillingMode"] == "PAY_PER_REQUEST"
    assert runtime_table["Properties"]["PointInTimeRecoverySpecification"] == {
        "PointInTimeRecoveryEnabled": True
    }
    assert "TimeToLiveSpecification" not in runtime_table["Properties"]
    assert runtime_table["DeletionPolicy"] == "Retain"

    worker = next(
        resource
        for resource in resources.values()
        if resource["Type"] == "AWS::Lambda::Function"
        and resource["Properties"].get("FunctionName")
        == "personal-operator-telegram-worker"
    )
    env = worker["Properties"]["Environment"]["Variables"]
    assert env["RUNTIME_STATE_TABLE_NAME"] == {"Ref": runtime_table_id}
    assert int(env["RUNTIME_LEASE_MS"]) > int(
        worker["Properties"]["Timeout"]
    ) * 1_000
    assert int(env["LAMBDA_TIMEOUT_SECONDS"]) == int(
        worker["Properties"]["Timeout"]
    )

    router = next(
        resource
        for resource in resources.values()
        if resource["Type"] == "AWS::Lambda::Function"
        and resource["Properties"].get("FunctionName") == "openclaw-router"
    )
    router_env = router["Properties"]["Environment"]["Variables"]
    assert "AGENTCORE_RUNTIME_ARN" not in router_env
    assert "RUNTIME_STATE_TABLE_NAME" not in router_env

    statements = [
        statement
        for resource in resources.values()
        if resource["Type"] == "AWS::IAM::Policy"
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]
    ]
    runtime_state_policy = next(
        statement
        for statement in statements
        if set(
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
        == {
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
        }
    )
    resources_with_state = runtime_state_policy["Resource"]
    if not isinstance(resources_with_state, list):
        resources_with_state = [resources_with_state]
    assert {"Fn::GetAtt": [runtime_table_id, "Arn"]} in resources_with_state

    runtime_policy = next(
        statement
        for statement in statements
        if "bedrock-agentcore:InvokeAgentRuntime"
        in (
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
    )
    assert set(runtime_policy["Action"]) == {
        "bedrock-agentcore:InvokeAgentRuntime",
        "bedrock-agentcore:StopRuntimeSession",
    }
    assert runtime_policy["Resource"] == [
        runtime_iam_arn,
        f"{runtime_iam_arn}/runtime-endpoint/DEFAULT",
    ]


def test_worker_disables_dynamodb_retries_and_binds_driver_execution_authority():
    source = Path("lambda/worker/index.py").read_text(encoding="utf-8")

    assert 'Config(retries={"max_attempts": 0})' in source
    assert 'boto3.resource("dynamodb", region_name=region, config=no_retries)' in source
    assert "RuntimeStateRepository(" in source
    assert 'runtime_arn=required["AGENTCORE_RUNTIME_ARN"]' in source
    assert 'runtime_qualifier=required["AGENTCORE_QUALIFIER"]' in source
    assert "max_execution_ms=maximum_execution_ms" in source
    assert "lease_ms <= maximum_execution_ms" in source
