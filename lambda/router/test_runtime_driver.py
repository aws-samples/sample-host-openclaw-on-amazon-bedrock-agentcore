"""Runtime driver and single-item state boundary tests."""

from __future__ import annotations

import io
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
    )
    return replace(base, **changes)


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


def test_ensure_conditionally_creates_server_session_then_reads_race_winner():
    table = MagicMock()
    table.put_item.side_effect = conditional_error("PutItem")
    table.get_item.return_value = {
        "Item": {
            "userId": USER,
            "sessionId": SESSION,
            "state": "COLD",
            "revision": 1,
            "leaseEpoch": 0,
            "createdAt": 1_000,
            "updatedAt": 1_000,
        }
    }
    repo = RuntimeStateRepository(
        table,
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
            "state": "DELETING",
            "revision": 4,
            "leaseEpoch": 3,
            "createdAt": 1,
            "updatedAt": 2,
            "tombstonedAt": 2,
        }
    }
    repo = RuntimeStateRepository(table, clock_ms=lambda: 3)

    with pytest.raises(TombstonedUser):
        repo.ensure(USER)


def test_acquire_uses_one_conditional_update_and_reports_live_owner():
    table = MagicMock()
    table.update_item.side_effect = conditional_error()
    table.get_item.return_value = {
        "Item": {
            "userId": USER,
            "sessionId": SESSION,
            "state": "BUSY",
            "revision": 4,
            "leaseOwner": "other",
            "leaseEpoch": 9,
            "leaseExpiresAt": 10_001,
            "createdAt": 1,
            "updatedAt": 2,
        }
    }
    repo = RuntimeStateRepository(table, clock_ms=lambda: 10_000)

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
        "state": "BUSY",
        "revision": 4,
        "leaseOwner": "old",
        "leaseEpoch": 9,
        "leaseExpiresAt": 9_999,
        "createdAt": 1,
        "updatedAt": 2,
    }
    table.get_item.return_value = {"Item": stale}
    repo = RuntimeStateRepository(table, clock_ms=lambda: 10_000)

    with pytest.raises(StaleLease) as failure:
        repo.acquire(USER, owner="mine", trace_id=TRACE, lease_ms=30_000)

    assert failure.value.record.lease_owner == "old"
    assert failure.value.record.lease_epoch == 9
    assert table.update_item.call_count == 1


def test_finalize_and_release_are_fenced_by_owner_and_epoch():
    table = MagicMock()
    table.update_item.side_effect = conditional_error()
    repo = RuntimeStateRepository(table, clock_ms=lambda: 10_000)
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
            "state": "BUSY",
            "revision": 2,
            "leaseOwner": "owner",
            "leaseEpoch": 7,
            "leaseExpiresAt": 40_000,
            "createdAt": 1,
            "updatedAt": 10_000,
        }
    }
    repo = RuntimeStateRepository(table, clock_ms=lambda: 10_000)
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
    repo = RuntimeStateRepository(table, clock_ms=lambda: 10_000)

    tombstone = repo.begin_purge(USER, owner="purger", lease_ms=30_000)

    assert tombstone.session_id is None
    kwargs = table.update_item.call_args.kwargs
    assert "attribute_exists(userId)" not in kwargs["ConditionExpression"]
    assert "createdAt=if_not_exists(createdAt,:now)" in kwargs["UpdateExpression"]
    assert kwargs["ExpressionAttributeValues"][":until"] == 40_000


class FakeRepository:
    def __init__(self, initial: RuntimeRecord | None = None):
        self.current = initial or record()
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
        )
        return self.current

    def finish_purge(self, lease):
        self.events.append(("purged", lease.lease_epoch))
        self.current = replace(lease, session_id=None, lease_owner=None)
        return self.current


class FakeAdapter:
    def __init__(self, response=None):
        self.response = response or {
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

    def stop(self, *, session_id):
        self.events.append(("stop", session_id))
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
    assert adapter.events[0] == ("stop", SESSION)
    assert repo.events[3] == ("rotate", 9, NEW_SESSION)
    assert adapter.events[1][0:3] == ("invoke", NEW_SESSION, USER)


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


def test_snapshot_is_a_trusted_runtime_action_and_requires_receipt():
    repo = FakeRepository(record(state=RuntimeState.IDLE))
    adapter = FakeAdapter({"workspaceReceipt": RECEIPT})
    driver = build_driver(repo, adapter)

    receipt = driver.snapshot(USER)

    assert receipt == RECEIPT
    assert adapter.events[0][3] == {
        "action": "snapshot",
        "internalUserId": USER,
        "namespace": USER,
    }
    assert repo.current.last_workspace_manifest_sha256 == SHA256


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

    assert adapter.events == [("stop", SESSION)]
    assert stopped["state"] == "COLD"
    assert stopped["sessionId"] == NEW_SESSION


def test_purge_tombstones_before_stop_and_refuses_future_ensure():
    repo = FakeRepository(record(state=RuntimeState.IDLE))
    adapter = FakeAdapter()
    driver = build_driver(repo, adapter)

    result = driver.purge(USER)

    assert repo.events[0][0] == "purge"
    assert adapter.events == [("stop", SESSION)]
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

    result = adapter.stop(session_id=SESSION)

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


def test_router_stack_provisions_exact_runtime_state_boundary():
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

    router = next(
        resource
        for resource in resources.values()
        if resource["Type"] == "AWS::Lambda::Function"
        and resource["Properties"].get("FunctionName") == "openclaw-router"
    )
    env = router["Properties"]["Environment"]["Variables"]
    assert env["RUNTIME_STATE_TABLE_NAME"] == {"Ref": runtime_table_id}
    assert env["RUNTIME_LEASE_MS"] == "120000"

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
    assert runtime_state_policy["Resource"] == {
        "Fn::GetAtt": [runtime_table_id, "Arn"]
    }

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
        "bedrock-agentcore:InvokeAgentRuntimeForUser",
        "bedrock-agentcore:StopRuntimeSession",
    }
    assert runtime_policy["Resource"] == [
        runtime_iam_arn,
        f"{runtime_iam_arn}/runtime-endpoint/DEFAULT",
    ]
