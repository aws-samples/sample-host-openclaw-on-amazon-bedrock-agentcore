import copy
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError, IncompleteReadError, ResponseStreamingError
from botocore.response import StreamingBody
from urllib3.response import HTTPResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lambda/shared/python"))
import runtime_routing as routing
from runtime_invocation import (
    RELEASE_RESERVE_SECONDS, InvocationDeadlineExceeded, InvocationTransportError, invoke_bounded,
)

ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/legacy-123"
V2_ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/candidate-123"
USER = "original-random-user-id"
ACTOR = "telegram:123"
SESSION = "ses_" + "1" * 40


class FakeTable:
    def __init__(self, item=None):
        self.items = {}
        if item:
            self.items[(item["PK"], item["SK"])] = copy.deepcopy(item)
        self.lock = threading.RLock()
        self.calls = []
        self.fail_read = False

    def get_item(self, **kwargs):
        if self.fail_read:
            raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetItem")
        self.calls.append(("get", kwargs))
        with self.lock:
            item = self.items.get(tuple(kwargs["Key"].values()))
            return {"Item": copy.deepcopy(item)} if item else {}

    def _check(self, kwargs, item):
        # Evaluate the resolver's comparison fields against the stored snapshot.
        values = kwargs.get("ExpressionAttributeValues", {})
        names = kwargs.get("ExpressionAttributeNames", {})
        condition = kwargs.get("ConditionExpression", "")
        if condition == "attribute_not_exists(PK)":
            return not item
        if condition == "userId = :currentUser":
            return bool(item) and item.get("userId") == values[":currentUser"]
        for field, variable in (("userId", ":user"), ("ownerGeneration", ":generation"), ("platformVersion", ":platform")):
            if variable in values and f"{field} = {variable}" in condition and (item or {}).get(field) != values[variable]:
                return False
        if "attribute_exists(PK)" in condition and not item:
            return False
        for field, variable in (
            ("runtimeEnrollment", ":platform"), ("enrollmentActorId", ":actor"),
            ("actorId", ":actor"), ("runtimeActorId", ":actor"),
            ("generation", ":generation"), ("sessionId", ":session"),
        ):
            if variable in values and f"{field} = {variable}" in condition and (item or {}).get(field) != values[variable]:
                return False
        if "#ttl >= :now" in condition and (item or {}).get("ttl", 0) < values[":now"]:
            return False
        for alias, name in names.items():
            expected = ":" + name
            if f"{alias} = {expected}" in condition:
                if not item or item.get(name) != values[expected]:
                    return False
            if f"attribute_not_exists({alias})" in condition and name not in ("inFlight", "activeAdmissions"):
                if item and name in item:
                    return False
        if "#inFlight = :zero" in condition and (item or {}).get("inFlight", 0) != 0:
            return False
        if "#inFlight >= :one" in condition and (item or {}).get("inFlight", 0) < 1:
            return False
        tokens = (item or {}).get("activeAdmissions", set())
        if "NOT contains(#admissions, :token)" in condition:
            if values[":token"] in tokens:
                return False
        elif "contains(#admissions, :token)" in condition:
            if values[":token"] not in tokens:
                return False
        elif "attribute_not_exists(#admissions)" in condition and "activeAdmissions" in (item or {}):
            return False
        return True

    def _failure(self):
        raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")

    def put_item(self, **kwargs):
        self.calls.append(("put", kwargs))
        with self.lock:
            item = kwargs["Item"]
            key = item["PK"], item["SK"]
            if not self._check(kwargs, self.items.get(key)):
                self._failure()
            self.items[key] = copy.deepcopy(item)

    def update_item(self, **kwargs):
        self.calls.append(("update", kwargs))
        with self.lock:
            key = tuple(kwargs["Key"].values())
            item = self.items.get(key)
            if not self._check(kwargs, item):
                self._failure()
            values = kwargs["ExpressionAttributeValues"]
            expression = kwargs["UpdateExpression"]
            if ":now" in expression:
                item["lastActivity"] = values[":now"]
            if "ADD #inFlight" in expression:
                item["inFlight"] = item.get("inFlight", 0) + values[":delta"]
            if "#admissions :tokens" in expression:
                tokens = set(item.get("activeAdmissions", set()))
                if "DELETE #admissions" in expression:
                    tokens -= values[":tokens"]
                else:
                    tokens |= values[":tokens"]
                if tokens:
                    item["activeAdmissions"] = tokens
                else:
                    item.pop("activeAdmissions", None)
            if ":draining" in expression:
                item["routeStatus"] = values[":draining"]
            return {"Attributes": copy.deepcopy(item)}

    def delete_item(self, **kwargs):
        with self.lock:
            self.items.pop(tuple(kwargs["Key"].values()), None)

    def scan(self, **kwargs):
        self.calls.append(("scan", kwargs))
        with self.lock:
            keys = sorted(self.items)
            cursor = kwargs.get("ExclusiveStartKey")
            start = keys.index((cursor["PK"], cursor["SK"])) + 1 if cursor else 0
            page = keys[start:start + kwargs["Limit"]]
            actor = kwargs["ExpressionAttributeValues"][":actor"]
            items = [copy.deepcopy(self.items[key]) for key in page]
            response = {"Items": [item for item in items if item["PK"].startswith("USER#") and (
                item["SK"] == f"CHANNEL#{actor}" or item.get("actorId") == actor or item.get("enrollmentActorId") == actor
            )]}
            if start + len(page) < len(keys):
                response["LastEvaluatedKey"] = {"PK": page[-1][0], "SK": page[-1][1]}
            return response


def legacy():
    return {"PK": f"USER#{USER}", "SK": "SESSION", "sessionId": SESSION, "custom": "keep"}


def candidate():
    return {
        **legacy(), "runtimeArn": V2_ARN, "endpointName": "candidate_v2",
        "generation": 1, "platformVersion": "V2", "routeStatus": "ACTIVE", "actorId": ACTOR,
    }


def resolve(table):
    return routing.resolve_route(table, USER)


def test_legacy_route_requires_reconciliation_and_preserves_identity():
    table = FakeTable(legacy())
    with pytest.raises(routing.RouteUnavailable, match="reconciliation"):
        resolve(table)
    assert table.items[(f"USER#{USER}", "SESSION")]["custom"] == "keep"
    assert table.calls[0][1]["ConsistentRead"] is True


def test_concurrent_missing_route_requests_never_create_a_session():
    barrier = threading.Barrier(12)

    class RacingTable(FakeTable):
        def get_item(self, **kwargs):
            result = super().get_item(**kwargs)
            if not result:
                barrier.wait(timeout=5)
            return result

    table = RacingTable({"PK": f"USER#{USER}", "SK": "PROFILE", "userId": USER})
    with ThreadPoolExecutor(max_workers=12) as pool:
        def rejected(_):
            with pytest.raises(routing.RouteUnavailable, match="reconciliation"):
                resolve(table)
        list(pool.map(rejected, range(12)))
    assert len(table.items) == 1
    assert not any(op == "put" for op, _ in table.calls)


def test_failed_read_never_creates_an_untracked_session():
    table = FakeTable()
    table.fail_read = True
    with pytest.raises(ClientError):
        resolve(table)
    assert not table.items


@pytest.mark.parametrize("change", [
    {"runtimeArn": V2_ARN}, {"sessionId": ""}, {"generation": -1},
    {"generation": "1"}, {"generation": 0}, {"generation": True},
    {"generation": 2**53}, {"platformVersion": "V3"}, {"platformVersion": "V1"},
    {"endpointName": V2_ARN}, {"endpointName": "DEFAULT"}, {"actorId": ""}, {"routeStatus": "UNKNOWN"},
])
def test_invalid_routes_fail_closed(change):
    item = candidate()
    if change == {"runtimeArn": V2_ARN}:
        item = {**legacy(), **change}
    else:
        item.update(change)
    with pytest.raises(routing.RouteUnavailable):
        resolve(FakeTable(item))


@pytest.mark.parametrize("field", ["runtimeArn", "endpointName", "generation", "platformVersion", "actorId", "routeStatus"])
def test_missing_v2_route_evidence_requires_reconciliation_without_writing(field):
    item = candidate()
    del item[field]
    table = FakeTable(item)
    with pytest.raises(routing.RouteUnavailable, match="reconciliation"):
        resolve(table)
    assert table.items[(item["PK"], item["SK"])] == item
    assert not any(operation in ("put", "update") for operation, _ in table.calls)


def test_admission_rejects_stale_route_and_does_not_retry_new_target():
    table = FakeTable({**candidate(), "endpointName": "release_old"})
    old = resolve(table)
    table.items[(f"USER#{USER}", "SESSION")] = candidate()
    with pytest.raises(routing.RouteUnavailable):
        with routing.admit_route(table, old, USER, ACTOR):
            pytest.fail("stale runtime must not be invoked")
    assert table.items[(f"USER#{USER}", "SESSION")].get("inFlight", 0) == 0


def test_active_call_blocks_freeze_then_draining_rejects_all_callers():
    table = FakeTable(candidate())
    route = resolve(table)
    with routing.admit_route(table, route, USER, ACTOR):
        assert table.items[(f"USER#{USER}", "SESSION")]["inFlight"] == 1
        with pytest.raises(routing.RouteUnavailable):
            routing.freeze_route(table, route)
    routing.freeze_route(table, route)
    with pytest.raises(routing.RouteUnavailable):
        resolve(table)
    with pytest.raises(routing.RouteUnavailable):
        with routing.admit_route(table, route, USER, ACTOR):
            pytest.fail("frozen route admitted")


def test_v2_different_actor_or_owner_cannot_start_a_second_writer():
    table = FakeTable(candidate())
    route = resolve(table)
    for owner, actor in [(USER, "slack:other"), ("other-user", ACTOR)]:
        with pytest.raises(routing.RouteUnavailable):
            with routing.admit_route(table, route, owner, actor):
                pytest.fail("identity mismatch admitted")


def test_freeze_preserves_v2_session_and_unrelated_fields():
    table = FakeTable(candidate())
    routing.freeze_route(table, resolve(table))
    item = table.items[(f"USER#{USER}", "SESSION")]
    assert item["sessionId"] == SESSION and item["custom"] == "keep"
    assert item["routeStatus"] == "DRAINING"


def load_lambda(name):
    with patch.dict(os.environ, {
        "AGENTCORE_RUNTIME_ARN": ARN, "AGENTCORE_QUALIFIER": "DEFAULT",
        "IDENTITY_TABLE_NAME": "local-test", "AWS_REGION": "us-west-2",
        "AWS_EC2_METADATA_DISABLED": "true",
    }), patch("boto3.resource"), patch("boto3.client"):
        spec = importlib.util.spec_from_file_location(f"routing_test_{name}", ROOT / f"lambda/{name}/index.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if name == "cron":
            module.configured_runtime_client_factory = module.runtime_client_factory
            module.agentcore_client = FakeRuntime()
            module.runtime_client_factory = lambda **kwargs: module.agentcore_client
    return module


class FakeRuntime:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [{"response": "ok"}])

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return {"statusCode": 200, "response": io.BytesIO(json.dumps(response).encode())}


def test_router_and_cron_invoke_same_tuple_and_identity():
    table = FakeTable(candidate())
    runtime = FakeRuntime([{"response": "chat"}, {"response": "cron"}])
    for name in ("router", "cron"):
        module = load_lambda(name)
        module.identity_table, module.agentcore_client = table, runtime
        route = module.get_or_create_session(USER)
        if name == "router":
            module.invoke_agent_runtime(route, USER, ACTOR, "telegram", {"text": "hi", "images": []})
        else:
            module.invoke_agentcore(route, "cron", USER, ACTOR, "telegram", "hi")
    assert len(runtime.calls) == 2
    for call in runtime.calls:
        assert call["agentRuntimeArn"] == V2_ARN
        assert call["qualifier"] == "candidate_v2"
        assert call["runtimeSessionId"] == SESSION
        payload = json.loads(call["payload"])
        assert payload["userId"] == USER and payload["actorId"] == ACTOR
        assert payload["routingGeneration"] == 1 and payload["runtimeSessionId"] == SESSION
    assert table.items[(f"USER#{USER}", "SESSION")]["inFlight"] == 0


@pytest.mark.parametrize("response", [
    {}, {"status": "unknown"}, {"status": "failed"}, {"status": "initializing", "phase": "FAILED"},
    {"status": "ready", "phase": "BINDING"}, {"status": "ready", "phase": "USER_READY", "error": "failure"},
    RuntimeError("unknown invoke outcome"),
])
def test_cron_v2_unknown_or_unready_warmup_does_not_execute(response):
    module = load_lambda("cron")
    module.identity_table = FakeTable(candidate())
    module.agentcore_client = FakeRuntime([response])
    route = module.get_or_create_session(USER)
    assert module.warmup_and_wait(route, USER, ACTOR, "telegram") is False
    assert len(module.agentcore_client.calls) == 1


@pytest.mark.parametrize("item,response", [
    (candidate(), {"status": "ready"}),
    (candidate(), {"status": "ready", "phase": "USER_READY"}),
])
def test_cron_accepts_explicit_v2_ready(item, response):
    module = load_lambda("cron")
    module.identity_table = FakeTable(item)
    module.agentcore_client = FakeRuntime([response])
    assert module.warmup_and_wait(module.get_or_create_session(USER), USER, ACTOR, "telegram")


def test_router_identity_lookup_failure_cannot_replace_canonical_user():
    module = load_lambda("router")
    module.REGISTRATION_OPEN = True
    module.identity_table = FakeTable()
    module.identity_table.fail_read = True
    with pytest.raises(ClientError):
        module.resolve_user("telegram", "123")
    assert not module.identity_table.items


def test_router_keeps_old_random_user_id():
    module = load_lambda("router")
    module.identity_table = FakeTable({"PK": f"CHANNEL#{ACTOR}", "SK": "PROFILE", "userId": USER})
    assert module.resolve_user("telegram", "123") == (USER, False)
    assert module.identity_table.calls[0][1]["ConsistentRead"] is True


def cron_event():
    return {
        "userId": "stale-payload-user", "actorId": ACTOR, "channel": "telegram",
        "channelTarget": "123", "message": "hi", "scheduleId": "schedule123",
    }


def cron_table():
    table = FakeTable(candidate())
    table.items[(f"CHANNEL#{ACTOR}", "PROFILE")] = {
        "PK": f"CHANNEL#{ACTOR}", "SK": "PROFILE", "userId": USER,
    }
    table.items[(f"USER#{USER}", "CRON#schedule123")] = {
        "PK": f"USER#{USER}", "SK": "CRON#schedule123",
    }
    return table


def test_cron_uses_canonical_identity_and_its_schedule_route():
    module = load_lambda("cron")
    module.identity_table = cron_table()
    module.agentcore_client = FakeRuntime([{"status": "ready", "phase": "USER_READY"}, {"response": "done"}])
    with patch.object(module, "deliver_response") as deliver:
        assert module.handler(cron_event(), None)["statusCode"] == 200
    deliver.assert_called_once_with("telegram", "123", "done")
    assert all(json.loads(call["payload"])["userId"] == USER for call in module.agentcore_client.calls)


def test_cron_missing_identity_does_not_fall_back_to_payload_owner():
    module = load_lambda("cron")
    module.identity_table = FakeTable()
    module.agentcore_client = FakeRuntime()
    assert module.handler(cron_event(), None)["statusCode"] == 403
    assert not module.agentcore_client.calls


def test_cron_schedule_owned_by_other_user_cannot_run():
    module = load_lambda("cron")
    module.identity_table = cron_table()
    del module.identity_table.items[(f"USER#{USER}", "CRON#schedule123")]
    module.agentcore_client = FakeRuntime()
    assert module.handler(cron_event(), None)["statusCode"] == 403
    assert not module.agentcore_client.calls


def test_cron_unknown_warmup_stops_before_business_action():
    module = load_lambda("cron")
    module.identity_table = cron_table()
    module.agentcore_client = FakeRuntime([{"status": "unknown"}])
    with patch.object(module, "deliver_response"):
        assert module.handler(cron_event(), None)["statusCode"] == 503
    assert [json.loads(call["payload"])["action"] for call in module.agentcore_client.calls] == ["warmup"]


def test_cron_drain_between_warmup_and_cron_never_restarts_old_session():
    module = load_lambda("cron")
    table = module.identity_table = cron_table()
    module.agentcore_client = FakeRuntime()

    def warmup(route, *args, **kwargs):
        routing.freeze_route(table, route)
        return True

    with patch.object(module, "warmup_and_wait", side_effect=warmup), patch.object(module, "deliver_response"):
        result = module.handler(cron_event(), None)
    assert result["statusCode"] == 503
    assert not module.agentcore_client.calls


def test_invocation_timeout_does_not_retry_or_failover():
    module = load_lambda("router")
    module.identity_table = FakeTable(candidate())
    module.agentcore_client = FakeRuntime([TimeoutError("request may have been accepted")])
    module.invoke_agent_runtime(module.get_or_create_session(USER), USER, ACTOR, "telegram", "hi")
    assert len(module.agentcore_client.calls) == 1


def test_warmup_polls_pending_same_route_then_accepts_ready():
    module = load_lambda("cron")
    module.identity_table = FakeTable(candidate())
    module.agentcore_client = FakeRuntime([
        {"status": "initializing"},
        {"status": "ready"},
    ])
    with patch.object(module.time, "sleep"):
        assert module.warmup_and_wait(module.get_or_create_session(USER), USER, ACTOR, "telegram")
    assert len(module.agentcore_client.calls) == 2
    assert all(call["runtimeSessionId"] == SESSION for call in module.agentcore_client.calls)


def test_warmup_late_ready_cannot_overrun_deadline():
    module = load_lambda("cron")
    module.identity_table = FakeTable(candidate())
    module.agentcore_client = FakeRuntime([{"status": "ready", "phase": "USER_READY"}])
    with patch.object(module.time, "monotonic", side_effect=[0, 0, 100]), patch.object(
        module, "invoke_agentcore", return_value={"status": "ready", "phase": "USER_READY"},
    ):
        assert not module.warmup_and_wait(module.get_or_create_session(USER), USER, ACTOR, "telegram", deadline=50)


@pytest.mark.parametrize("status", ["error", "unknown_outcome"])
def test_cron_runtime_failure_or_unknown_outcome_is_not_success_or_retried(status):
    module = load_lambda("cron")
    module.identity_table = cron_table()
    module.agentcore_client = FakeRuntime([
        {"status": "ready"}, {"status": status, "response": "No retry"},
    ])
    with patch.object(module, "deliver_response"):
        assert module.handler(cron_event(), None)["statusCode"] == 503
    assert [json.loads(call["payload"])["action"] for call in module.agentcore_client.calls] == ["warmup", "cron"]


def test_lambda_loss_leaves_counter_blocked_without_automatic_expiry():
    table = FakeTable({**candidate(), "inFlight": 1, "lastActivity": "2000-01-01T00:00:00Z"})
    with pytest.raises(routing.RouteUnavailable):
        routing.freeze_route(table, resolve(table))
    assert table.items[(f"USER#{USER}", "SESSION")]["inFlight"] == 1


def test_lost_release_response_retried_for_a_cannot_release_b():
    table = FakeTable(candidate())
    route = resolve(table)
    first = routing.admit_route(table, route, USER, ACTOR)
    second = routing.admit_route(table, route, USER, ACTOR)
    first.__enter__()
    first_token = next(iter(table.items[tuple(route.key.values())]["activeAdmissions"]))
    second.__enter__()
    original = table.update_item

    def update(**kwargs):
        result = original(**kwargs)
        if kwargs["ExpressionAttributeValues"].get(":delta") == -1:
            if kwargs["ExpressionAttributeValues"][":token"] == first_token:
                # The first response is lost; SDK retry sends the same request.
                return original(**kwargs)
        return result

    table.update_item = update
    first.__exit__(None, None, None)
    record = table.items[tuple(route.key.values())]
    assert record["inFlight"] == 1
    assert len(record["activeAdmissions"]) == 1
    assert first_token not in record["activeAdmissions"]
    with pytest.raises(routing.RouteUnavailable):
        routing.freeze_route(table, route)
    second.__exit__(None, None, None)
    assert record["inFlight"] == 0 and "activeAdmissions" not in record
    routing.freeze_route(table, route)


def test_lost_acquire_response_sdk_retry_confirms_same_token_once():
    table = FakeTable(candidate())
    route = resolve(table)
    original = table.update_item

    def update(**kwargs):
        result = original(**kwargs)
        if kwargs["ExpressionAttributeValues"].get(":delta") == 1:
            return original(**kwargs)
        return result

    table.update_item = update
    with routing.admit_route(table, route, USER, ACTOR):
        record = table.items[tuple(route.key.values())]
        assert record["inFlight"] == 1 and len(record["activeAdmissions"]) == 1
    assert record["inFlight"] == 0


def fail_releases(table):
    original = table.update_item

    def update(**kwargs):
        if kwargs["ExpressionAttributeValues"].get(":delta") == -1:
            raise TimeoutError("Release response unknown")
        return original(**kwargs)

    table.update_item = update


def test_router_known_streamed_result_survives_release_failure(caplog):
    module = load_lambda("router")
    table = module.identity_table = FakeTable(candidate())
    fail_releases(table)
    expected = {"response": "Already delivered", "streamed": True, "status": "completed"}
    runtime = module.agentcore_client = FakeRuntime([expected])
    result = module.invoke_agent_runtime(resolve(table), USER, ACTOR, "telegram", "hi")
    assert result == expected and len(runtime.calls) == 1
    assert table.items[(f"USER#{USER}", "SESSION")]["inFlight"] == 1
    with pytest.raises(routing.RouteUnavailable):
        routing.freeze_route(table, resolve(table))
    assert "Admission release was not confirmed" in caplog.text


def test_telegram_does_not_send_duplicate_error_after_streamed_success_release_failure():
    module = load_lambda("router")
    module.identity_table = cron_table()
    fail_releases(module.identity_table)
    module.agentcore_client = FakeRuntime([{"response": "Already delivered", "streamed": True}])
    with patch.object(module, "_get_telegram_token", return_value="local"), patch.object(
        module, "send_telegram_typing",
    ), patch.object(module, "_periodic_typing"), patch.object(module, "_send_telegram_chunks") as send:
        module.handle_telegram({"message": {"chat": {"id": 123}, "from": {"id": 123}, "text": "hi"}})
    send.assert_not_called()
    assert len(module.agentcore_client.calls) == 1


def test_cron_known_success_is_delivered_once_when_release_fails():
    module = load_lambda("cron")
    table = module.identity_table = cron_table()
    fail_releases(table)
    runtime = module.agentcore_client = FakeRuntime([{"response": "done"}])
    with patch.object(module, "warmup_and_wait", return_value=True), patch.object(module, "deliver_response") as deliver:
        assert module.handler(cron_event(), None)["statusCode"] == 200
    deliver.assert_called_once_with("telegram", "123", "done")
    assert len(runtime.calls) == 1
    with pytest.raises(routing.RouteUnavailable):
        routing.freeze_route(table, resolve(table))


def test_release_failure_does_not_mask_original_invocation_error():
    table = FakeTable(candidate())
    fail_releases(table)
    original = ValueError("original")
    with pytest.raises(ValueError) as caught:
        with routing.admit_route(table, resolve(table), USER, ACTOR):
            raise original
    assert caught.value is original


class BlockingBody:
    def __init__(self):
        self.started = threading.Event()
        self.unblock = threading.Event()
        self.closed = threading.Event()
        self.timeouts = []

    def set_socket_timeout(self, timeout):
        self.timeouts.append(timeout)

    def read(self, size):
        self.started.set()
        self.unblock.wait(timeout=3)
        return b""

    def close(self):
        self.closed.set()


@pytest.mark.parametrize("stall", ["headers", "body"])
def test_warmup_deadline_bounds_stalled_transport_and_retains_admission(stall):
    module = load_lambda("cron")
    table = module.identity_table = FakeTable(candidate())
    body = BlockingBody()
    calls, budgets = [], []

    class Runtime:
        def invoke_agent_runtime(self, **params):
            calls.append(params)
            if stall == "headers":
                body.started.set()
                body.unblock.wait(timeout=3)
            return {"statusCode": 200, "response": body}

    def factory(**kwargs):
        budgets.append(kwargs)
        return Runtime()

    module.runtime_client_factory = factory
    start = time.monotonic()
    try:
        ready = module.warmup_and_wait(
            resolve(table), USER, ACTOR, "telegram", deadline=start + RELEASE_RESERVE_SECONDS + 0.08,
        )
        assert not ready and time.monotonic() - start < 0.5
        assert body.started.is_set() and len(calls) == 1
        assert 0 < sum(budgets[0].values()) <= 0.08
        if stall == "body":
            assert 0 < body.timeouts[0] <= 0.08
        record = table.items[(f"USER#{USER}", "SESSION")]
        assert record["inFlight"] == 1 and len(record["activeAdmissions"]) == 1
        with pytest.raises(routing.RouteUnavailable):
            routing.freeze_route(table, resolve(table))
    finally:
        body.unblock.set()
        assert body.closed.wait(timeout=1)


def test_no_remaining_io_budget_never_calls_runtime_or_retains_admission():
    table = FakeTable(candidate())
    with pytest.raises(InvocationDeadlineExceeded):
        with routing.admit_route(table, resolve(table), USER, ACTOR):
            invoke_bounded(lambda **kwargs: pytest.fail("No I/O budget"), {}, time.monotonic())
    assert table.items[(f"USER#{USER}", "SESSION")]["inFlight"] == 0
    routing.freeze_route(table, resolve(table))


def test_cron_business_budget_uses_current_lambda_remaining_and_delivery_release_reserves():
    module = load_lambda("cron")
    module.identity_table = cron_table()
    runtime = FakeRuntime([{"response": "done"}])
    budgets = []

    def factory(**kwargs):
        budgets.append(kwargs)
        return runtime

    class Context:
        readings = iter([100_000, 30_000])

        def get_remaining_time_in_millis(self):
            return next(self.readings)

    module.runtime_client_factory = factory
    with patch.object(module, "warmup_and_wait", return_value=True), patch.object(module, "deliver_response"):
        assert module.handler(cron_event(), Context())["statusCode"] == 200
    assert 8 < sum(budgets[0].values()) <= 30 - 15 - RELEASE_RESERVE_SECONDS
    assert len(runtime.calls) == 1


def test_bounded_response_closes_body_and_client_and_configures_sdk_without_retry():
    module = load_lambda("cron")
    with patch.object(module.boto3, "client") as client:
        module.configured_runtime_client_factory(connect_timeout=0.2, read_timeout=0.4)
    config = client.call_args.kwargs["config"]
    assert config.connect_timeout == 0.2 and config.read_timeout == 0.4
    assert config.retries["max_attempts"] == 0
    closed = threading.Event()
    body = io.BytesIO(json.dumps({"response": "done"}).encode())

    class Runtime:
        def invoke_agent_runtime(self, **kwargs):
            return {"statusCode": 200, "response": body}

        def close(self):
            closed.set()

    result = invoke_bounded(lambda **kwargs: Runtime(), {}, time.monotonic() + 7)
    assert json.loads(result[1]) == {"response": "done"}
    assert closed.wait(timeout=1) and body.closed


@pytest.fixture
def sdk_body():
    bodies = []

    def make(content, headers=None, enforce_content_length=True):
        headers = {"Content-Length": str(len(content))} if headers is None else headers
        wire = b"HTTP/1.1 200 OK\r\n" + b"".join(
            f"{key}: {value}\r\n".encode() for key, value in headers.items()
        ) + b"\r\n" + content

        class Socket:
            def __init__(self):
                self.timeouts = []

            def makefile(self, mode):
                raw = io.BytesIO(wire)
                raw._sock = self
                return io.BufferedReader(raw)

            def settimeout(self, timeout):
                self.timeouts.append(timeout)

        sock = Socket()
        response = http.client.HTTPResponse(sock)
        response.begin()
        # Use the SDK's HTTP stack with an in-memory socket; EOF clears response.fp.
        raw = HTTPResponse(
            body=response, headers=dict(response.getheaders()), preload_content=False,
            decode_content=False, enforce_content_length=enforce_content_length,
        )
        body = StreamingBody(raw, response.getheader("Content-Length"))
        bodies.append(body)
        return body, sock

    yield make
    for body in bodies:
        body.close()


@pytest.mark.parametrize("size", [0, 5, 65536, 65537])
@pytest.mark.parametrize("framing", ["length", "chunked", "close"])
def test_bounded_sdk_response_reads_through_eof(sdk_body, size, framing):
    content = b"x" * size
    wire, headers = content, None if framing == "length" else {}
    if framing == "chunked":
        wire = (f"{size:x}\r\n".encode() + content + b"\r\n" if size else b"") + b"0\r\n\r\n"
        headers = {"Transfer-Encoding": "chunked"}
    body, sock = sdk_body(wire, headers)
    runtime = SimpleNamespace(invoke_agent_runtime=lambda **kwargs: {"response": body})
    assert invoke_bounded(lambda **kwargs: runtime, {}, time.monotonic() + 7) == (200, content)
    assert body._raw_stream.closed and body._raw_stream._fp.fp is None
    assert sock.timeouts and all(0 < timeout <= 1 for timeout in sock.timeouts)
    assert sock.timeouts == sorted(sock.timeouts, reverse=True)


@pytest.mark.parametrize("closed", [False, True], ids=["truncated", "disconnected"])
@pytest.mark.parametrize("enforce", [False, True], ids=["botocore-validation", "urllib3-validation"])
def test_bounded_sdk_incomplete_response_retains_admission(sdk_body, closed, enforce):
    body, _ = sdk_body(b"partial", {"Content-Length": "20"}, enforce)
    if closed:
        assert body.read(3) == b"par"
        body._raw_stream.close()
    runtime = SimpleNamespace(invoke_agent_runtime=lambda **kwargs: {"response": body})
    table = FakeTable(candidate())
    with pytest.raises(InvocationTransportError) as caught:
        with routing.admit_route(table, resolve(table), USER, ACTOR):
            invoke_bounded(lambda **kwargs: runtime, {}, time.monotonic() + 7)
    assert isinstance(caught.value.__cause__, ResponseStreamingError if enforce else IncompleteReadError)
    assert caught.value.retain_admission is True
    assert table.items[(f"USER#{USER}", "SESSION")]["inFlight"] == 1


@pytest.mark.parametrize("limit", [65536, 500_000])
@pytest.mark.parametrize("extra", [0, 1], ids=["exact-limit", "over-limit"])
def test_bounded_sdk_response_preserves_size_limit(sdk_body, limit, extra):
    content = b"x" * (limit + extra)
    body, _ = sdk_body(content)
    runtime = SimpleNamespace(invoke_agent_runtime=lambda **kwargs: {"response": body})
    table = FakeTable(candidate())

    def invoke():
        with routing.admit_route(table, resolve(table), USER, ACTOR):
            return invoke_bounded(lambda **kwargs: runtime, {}, time.monotonic() + 7, limit)

    if extra:
        with pytest.raises(InvocationTransportError, match="exceeds size limit") as caught:
            invoke()
        assert isinstance(caught.value.__cause__, ValueError) and caught.value.retain_admission
    else:
        assert invoke() == (200, content)
    assert table.items[(f"USER#{USER}", "SESSION")]["inFlight"] == extra


def test_bounded_sdk_socket_attribute_error_is_not_eof(sdk_body):
    body, sock = sdk_body(b"ready")
    runtime = SimpleNamespace(invoke_agent_runtime=lambda **kwargs: {"response": body})
    error = AttributeError("unexpected socket interface")
    with patch.object(sock, "settimeout", side_effect=error), patch.object(body, "read") as read:
        with pytest.raises(InvocationTransportError) as caught:
            invoke_bounded(lambda **kwargs: runtime, {}, time.monotonic() + 7)
    assert caught.value.__cause__ is error and caught.value.retain_admission
    read.assert_not_called()


@pytest.mark.parametrize("result", [{"response": "done"}, {"status": "unknown_outcome"}, None])
def test_cron_sdk_warmup_reaches_execution_and_preserves_admission(sdk_body, result):
    module = load_lambda("cron")
    table = module.identity_table = cron_table()
    responses = [
        sdk_body(json.dumps({"status": "ready", "phase": "USER_READY"}).encode())[0],
        sdk_body(json.dumps(result).encode() if result is not None else b"")[0],
    ]
    calls = []

    class Runtime:
        def invoke_agent_runtime(self, **kwargs):
            calls.append(json.loads(kwargs["payload"])["action"])
            return {"response": responses.pop(0)}

    module.runtime_client_factory = lambda **kwargs: Runtime()
    with patch.object(module, "deliver_response") as deliver:
        assert module.handler(cron_event(), None)["statusCode"] == (200 if result == {"response": "done"} else 503)
    assert calls == ["warmup", "cron"]
    record = table.items[(f"USER#{USER}", "SESSION")]
    if result == {"response": "done"}:
        deliver.assert_called_once_with("telegram", "123", "done")
        assert record["inFlight"] == 0 and "activeAdmissions" not in record
    else:
        # The user gets one fixed notice, never the runtime's own text.
        deliver.assert_called_once()
        assert deliver.call_args.args[:2] == ("telegram", "123")
        assert deliver.call_args.args[2].endswith(module.CRON_FAILED_MESSAGE)
        assert record["inFlight"] == 1 and len(record["activeAdmissions"]) == 1
