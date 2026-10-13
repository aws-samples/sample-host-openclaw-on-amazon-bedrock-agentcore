import copy
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import boto3
from botocore.exceptions import ClientError
from botocore.stub import Stubber
import pytest

from tests.test_runtime_routing import ARN, FakeTable, FakeRuntime, load_lambda, routing
from tests.test_runtime_routing_synth import manifest
from runtime_enrollment import reconcile_actor_owner, enroll_new_user
from stacks.runtime_config import runtime_target_from_manifest


def canonical(channel, channel_id):
    return "user_" + hashlib.sha256(f"{channel}:{channel_id}".encode()).hexdigest()[:16]


class FakeRegistrationClient:
    def __init__(self, table):
        self.table = table
        self.calls = []
        self.before_commit = lambda: None
        self.hooks = threading.local()

    def transact_write_items(self, **kwargs):
        self.calls.append(kwargs)
        if not getattr(self.hooks, "running", False):
            self.hooks.running = True
            try:
                self.before_commit()
            finally:
                self.hooks.running = False
        records = []
        for transaction in kwargs["TransactItems"]:
            operation, params = next(iter(transaction.items()))
            assert params["TableName"] == "local-test"
            decode = lambda item: {key: value["S"] if "S" in value else int(value["N"]) for key, value in item.items()}
            record = decode(params.get("Item", params.get("Key")))
            condition = {
                "ConditionExpression": params.get("ConditionExpression", ""),
                "ExpressionAttributeValues": decode(params.get("ExpressionAttributeValues", {})),
                "ExpressionAttributeNames": params.get("ExpressionAttributeNames", {}),
            }
            records.append((operation, record, condition))
        assert len(records) >= 5
        assert len({(r["PK"], r["SK"]) for _, r, _ in records}) == len(records)
        with self.table.lock:
            if any(not self.table._check(condition, self.table.items.get((r["PK"], r["SK"])))
                   for _, r, condition in records):
                raise ClientError({"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems")
            for operation, record, _ in records:
                if operation == "Put":
                    self.table.items[(record["PK"], record["SK"])] = copy.deepcopy(record)
                elif operation == "Delete":
                    self.table.items.pop((record["PK"], record["SK"]), None)
        return {}


def router():
    module = load_lambda("router")
    module.REGISTRATION_OPEN = True
    module.RUNTIME_TARGET = runtime_target_from_manifest(manifest())
    module.identity_table = FakeTable()
    module.identity_client = FakeRegistrationClient(module.identity_table)
    return module


def test_first_registration_atomically_enrolls_and_router_cron_use_same_v2_route():
    module = router()
    user_id, is_new = module.resolve_user("telegram", "123", "Example")
    assert is_new and user_id == canonical("telegram", "123")
    assert len(module.identity_table.items) == 5
    profile = module.identity_table.items[(f"USER#{user_id}", "PROFILE")]
    assert profile["runtimeEnrollment"] == "V2"
    route = module.get_or_create_session(user_id)
    assert route.platform_version == "V2" and route.generation == 1
    assert route.actor_id == "telegram:123"
    assert route.snapshot["runtimeVersion"] == manifest()["agentRuntimeVersion"]
    assert route.snapshot["configurationHash"] == manifest()["configurationHash"]
    runtime = module.agentcore_client = FakeRuntime([{"response": "chat"}, {"response": "cron"}])
    module.invoke_agent_runtime(route, user_id, "telegram:123", "telegram", "hi")
    cron = load_lambda("cron")
    cron.identity_table, cron.agentcore_client = module.identity_table, runtime
    cron.invoke_agentcore(cron.get_or_create_session(user_id), "cron", user_id, "telegram:123", "telegram", "hi")
    assert len(runtime.calls) == 2
    assert runtime.calls[0]["runtimeSessionId"] == runtime.calls[1]["runtimeSessionId"]
    assert all(call["agentRuntimeArn"] == manifest()["agentRuntimeArn"] for call in runtime.calls)


def test_concurrent_registration_of_same_actor_has_one_canonical_user_and_route():
    module = router()
    barrier = threading.Barrier(12)
    module.identity_client.before_commit = lambda: barrier.wait(timeout=5)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: module.resolve_user("telegram", "123"), range(12)))
    assert {result[0] for result in results} == {canonical("telegram", "123")}
    assert sum(result[1] for result in results) == 1
    assert len(module.identity_client.calls) == 12
    assert len(module.identity_table.items) == 5
    assert len({item["sessionId"] for item in module.identity_table.items.values() if item["SK"] == "SESSION"}) == 1


def test_concurrent_registrations_with_different_release_targets_use_winners_route():
    first, second = router(), router()
    second.identity_table = first.identity_table
    second.identity_client = FakeRegistrationClient(first.identity_table)
    second.RUNTIME_TARGET = {
        **second.RUNTIME_TARGET,
        "runtimeArn": manifest()["agentRuntimeArn"].replace("candidate-123", "nextrelease-456"),
        "endpointName": "release_next", "runtimeVersion": "8",
    }
    barrier = threading.Barrier(2)
    for module in (first, second):
        module.identity_client.before_commit = lambda: barrier.wait(timeout=5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda module: module.resolve_user("telegram", "123"), (first, second)))
    assert sum(result[1] for result in results) == 1
    assert results[0][0] == results[1][0]
    assert first.get_or_create_session(results[0][0]) == second.get_or_create_session(results[1][0])
    assert len(first.identity_table.items) == 5


def test_parallel_registrations_on_two_channels_keep_distinct_actor_namespaces():
    module = router()
    barrier = threading.Barrier(2)
    module.identity_client.before_commit = lambda: barrier.wait(timeout=5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda channel: module.resolve_user(channel, "123"), ("telegram", "slack")))
    assert len({result[0] for result in results}) == 2
    assert len(module.identity_table.items) == 10
    for channel, (user_id, _) in zip(("telegram", "slack"), results):
        assert user_id == canonical(channel, "123")
        assert module.get_or_create_session(user_id).actor_id == f"{channel}:123"


def test_existing_canonical_profile_missing_session_requires_reconciliation():
    module = router()
    table = module.identity_table
    table.items[("CHANNEL#telegram:123", "PROFILE")] = {
        "PK": "CHANNEL#telegram:123", "SK": "PROFILE", "userId": "old-random-id",
    }
    table.items[("USER#old-random-id", "PROFILE")] = {
        "PK": "USER#old-random-id", "SK": "PROFILE", "userId": "old-random-id",
    }
    assert module.resolve_user("telegram", "123") == ("old-random-id", False)
    with pytest.raises(routing.RouteUnavailable, match="reconciliation"):
        module.get_or_create_session("old-random-id")
    assert not module.identity_client.calls


def test_existing_identity_missing_session_without_v1_fails_closed():
    module = router()
    module.identity_table.items[("CHANNEL#telegram:123", "PROFILE")] = {
        "PK": "CHANNEL#telegram:123", "SK": "PROFILE", "userId": "old-random-id",
    }
    user_id, new = module.resolve_user("telegram", "123")
    assert not new
    with pytest.raises(routing.RouteUnavailable):
        module.get_or_create_session(user_id)
    assert not module.identity_client.calls
    assert len(module.identity_table.items) == 1


def test_enrolled_identity_losing_session_cannot_fall_back_to_v1():
    module = router()
    user_id, _ = module.resolve_user("telegram", "123")
    del module.identity_table.items[(f"USER#{user_id}", "SESSION")]
    assert module.resolve_user("telegram", "123") == (user_id, False)
    with pytest.raises(routing.RouteUnavailable):
        module.get_or_create_session(user_id)
    assert len(module.identity_client.calls) == 1
    assert (f"USER#{user_id}", "SESSION") not in module.identity_table.items


def test_enrolled_identity_losing_profile_and_session_also_fails_closed():
    module = router()
    user_id, _ = module.resolve_user("telegram", "123")
    del module.identity_table.items[(f"USER#{user_id}", "SESSION")]
    del module.identity_table.items[(f"USER#{user_id}", "PROFILE")]
    assert module.resolve_user("telegram", "123") == (user_id, False)
    with pytest.raises(routing.RouteUnavailable):
        module.get_or_create_session(user_id)
    assert len(module.identity_client.calls) == 1


@pytest.mark.parametrize("existing_sort_key", ["PROFILE", "SESSION", "CHANNEL#telegram:123"])
def test_existing_canonical_record_without_channel_cannot_be_reenrolled(existing_sort_key):
    module = router()
    user_id = canonical("telegram", "123")
    existing = {"PK": f"USER#{user_id}", "SK": existing_sort_key, "preserve": "yes"}
    module.identity_table.items[(existing["PK"], existing["SK"])] = existing
    with pytest.raises(routing.RouteUnavailable):
        module.resolve_user("telegram", "123")
    assert list(module.identity_table.items.values()) == [existing]


def test_no_manifest_target_disables_registration_without_any_write():
    module = router()
    module.RUNTIME_TARGET = None
    assert module.resolve_user("telegram", "123") == (None, False)
    assert not module.identity_client.calls
    assert not module.identity_table.items


def test_allowlist_remains_required_for_new_v2_registration():
    module = router()
    module.REGISTRATION_OPEN = False
    assert module.resolve_user("telegram", "123") == (None, False)
    assert not module.identity_client.calls and not module.identity_table.items


def test_unknown_transaction_outcome_is_not_retried_or_replaced():
    module = router()
    def fail():
        raise TimeoutError("commit outcome unknown")
    module.identity_client.before_commit = fail
    with pytest.raises(TimeoutError):
        module.resolve_user("telegram", "123")
    assert len(module.identity_client.calls) == 1 and not module.identity_table.items


def test_commit_then_timeout_is_recovered_by_authoritative_identity_read_only():
    module = router()
    transact = module.identity_client.transact_write_items

    def commit_then_timeout(**kwargs):
        transact(**kwargs)
        raise TimeoutError("response lost after commit")

    module.identity_client.transact_write_items = commit_then_timeout
    with pytest.raises(TimeoutError):
        module.resolve_user("telegram", "123")
    assert len(module.identity_table.items) == 5
    session = module.get_or_create_session(canonical("telegram", "123"))
    assert module.resolve_user("telegram", "123") == (canonical("telegram", "123"), False)
    assert module.get_or_create_session(canonical("telegram", "123")) == session
    assert len(module.identity_client.calls) == 1


def add_v1_bind_target(module):
    table = module.identity_table
    table.items[("USER#existing-v1", "PROFILE")] = {
        "PK": "USER#existing-v1", "SK": "PROFILE", "userId": "existing-v1",
    }
    table.items[("BIND#LINKCODE", "BIND")] = {
        "PK": "BIND#LINKCODE", "SK": "BIND", "userId": "existing-v1", "ttl": int(time.time()) + 600,
    }


def add_v2_bind_target(module):
    user_id, _ = module.resolve_user("telegram", "owner")
    code = module.create_bind_code(user_id)
    record = module.identity_table.items.pop((f"BIND#{code}", "BIND"))
    record["PK"] = "BIND#LINKCODE"
    module.identity_table.items[("BIND#LINKCODE", "BIND")] = record
    return user_id


def test_v2_actor_cannot_be_rebound_into_v1_namespace():
    module = router()
    user_id, _ = module.resolve_user("telegram", "123")
    add_v1_bind_target(module)
    before = copy.deepcopy(module.identity_table.items)
    assert module.redeem_bind_code("LINKCODE", "telegram", "123") == (None, False)
    assert module.identity_table.items == before
    assert module.create_bind_code("existing-v1") is None


def test_unregistered_channel_can_explicitly_link_to_v2_canonical_actor():
    module = router()
    user_id, _ = module.resolve_user("telegram", "123")
    route = module.get_or_create_session(user_id)
    code = module.create_bind_code(user_id)
    assert module.redeem_bind_code(code, "slack", "456") == (user_id, True)
    assert module.resolve_user("slack", "456") == (user_id, False)
    assert module.get_or_create_session(user_id) == route
    assert ("USER#" + canonical("slack", "456"), "SESSION") not in module.identity_table.items


def test_enrollment_winning_link_race_cannot_be_overwritten():
    module = router()
    add_v2_bind_target(module)
    module.identity_client.before_commit = lambda: module.resolve_user("slack", "456")
    assert module.redeem_bind_code("LINKCODE", "slack", "456") == (None, False)
    record = module.identity_table.items[("CHANNEL#slack:456", "PROFILE")]
    assert record["userId"] == canonical("slack", "456")
    assert module.get_or_create_session(record["userId"]).platform_version == "V2"


def test_v2_link_winning_registration_race_creates_no_phantom_user():
    module = router()
    owner = add_v2_bind_target(module)
    module.identity_client.before_commit = lambda: module.redeem_bind_code("LINKCODE", "slack", "456")
    assert module.resolve_user("slack", "456") == (owner, False)
    assert not any(key[0] == f"USER#{canonical('slack', '456')}" for key in module.identity_table.items)
    assert module.get_or_create_session(owner).platform_version == "V2"


def test_transaction_uses_valid_low_level_dynamodb_shapes_without_network():
    module = router()
    module.resolve_user("telegram", "123")
    params = module.identity_client.calls[0]
    client = boto3.client("dynamodb", region_name="us-west-2", aws_access_key_id="local", aws_secret_access_key="local")
    with Stubber(client) as stubber:
        stubber.add_response("transact_write_items", {}, params)
        assert client.transact_write_items(**params) == {}
        stubber.assert_no_pending_responses()


def test_missing_channel_with_legacy_random_canonical_backref_blocks_enrollment():
    module = router()
    table = module.identity_table
    for sort_key in ("PROFILE", "SESSION", "CHANNEL#telegram:123"):
        table.items[("USER#old-random-id", sort_key)] = {
            "PK": "USER#old-random-id", "SK": sort_key, "userId": "old-random-id",
        }
    before = copy.deepcopy(table.items)
    with pytest.raises(routing.RouteUnavailable, match="retained ownership"):
        module.resolve_user("telegram", "123")
    assert table.items == before and not module.identity_client.calls


@pytest.mark.parametrize("keep_owner", [True, False])
def test_v2_missing_channel_cannot_reregister_or_redeem_v1_link(keep_owner):
    module = router()
    module.resolve_user("telegram", "123")
    del module.identity_table.items[("CHANNEL#telegram:123", "PROFILE")]
    if not keep_owner:
        del module.identity_table.items[("ACTOR#telegram:123", "OWNER")]
    add_v1_bind_target(module)
    before = copy.deepcopy(module.identity_table.items)
    with pytest.raises(routing.RouteUnavailable, match="retained ownership"):
        module.resolve_user("telegram", "123")
    assert module.redeem_bind_code("LINKCODE", "telegram", "123") == (None, False)
    assert module.identity_table.items == before


def test_legacy_reconciliation_continues_after_empty_filtered_page():
    module = router()
    table = module.identity_table
    for number in range(100):
        table.items[(f"NOISE#{number:03}", "PROFILE")] = {"PK": f"NOISE#{number:03}", "SK": "PROFILE"}
    table.items[("USER#old-random", "CHANNEL#telegram:123")] = {
        "PK": "USER#old-random", "SK": "CHANNEL#telegram:123",
    }
    with pytest.raises(routing.RouteUnavailable, match="retained ownership"):
        module.resolve_user("telegram", "123")
    scans = [params for operation, params in table.calls if operation == "scan"]
    assert len(scans) == 2
    assert all(scan["ConsistentRead"] is True for scan in scans)
    assert not module.identity_client.calls


@pytest.mark.parametrize("scan_fails", [True, False])
def test_failed_or_incomplete_reconciliation_never_registers(scan_fails):
    module = router()

    def scan(**kwargs):
        if scan_fails:
            raise TimeoutError("Legacy scan unavailable")
        return {"Items": [], "LastEvaluatedKey": {"PK": "more", "SK": "more"}}

    module.identity_table.scan = scan
    with pytest.raises((TimeoutError, routing.RouteUnavailable)):
        module.resolve_user("telegram", "123")
    assert not module.identity_client.calls and not module.identity_table.items


def test_v2_link_cannot_transfer_another_users_actor_or_state():
    module = router()
    original_user, _ = module.resolve_user("telegram", "123")
    add_v2_bind_target(module)
    before = copy.deepcopy(module.identity_table.items)
    assert module.redeem_bind_code("LINKCODE", "telegram", "123") == (None, False)
    owner = reconcile_actor_owner(module.identity_table, "telegram:123")
    assert owner["userId"] == original_user and owner["platformVersion"] == "V2"
    assert module.identity_table.items == before


@pytest.mark.parametrize("caller", ["router", "cron"])
def test_bound_channel_dispatches_with_canonical_actor_and_actual_delivery_identity(caller):
    module = router()
    user = add_v2_bind_target(module)
    route = module.get_or_create_session(user)
    assert module.redeem_bind_code("LINKCODE", "slack", "456") == (user, True)
    runtime = FakeRuntime()
    target = module if caller == "router" else load_lambda("cron")
    target.identity_table, target.agentcore_client = module.identity_table, runtime
    if caller == "router":
        result = target.invoke_agent_runtime(route, user, "slack:456", "slack", "hi", channel_target="C123")
    else:
        result = target.invoke_agentcore(route, "cron", user, "slack:456", "slack", "hi", channel_target="C123")
    assert result["response"] == "ok"
    payload = json.loads(runtime.calls[0]["payload"])
    assert payload["actorId"] == "telegram:owner"
    assert payload["channelActorId"] == "slack:456"
    assert payload["channel"] == "slack" and payload["channelTarget"] == "C123"
    assert payload["userId"] == user and payload["runtimeSessionId"] == route.session_id
    assert module.get_or_create_session(user) == route


@pytest.mark.parametrize("missing", ["owner", "channel", "backref"])
def test_unverified_channel_alias_never_invokes_canonical_runtime(missing):
    module = router()
    user = add_v2_bind_target(module)
    assert module.redeem_bind_code("LINKCODE", "slack", "456") == (user, True)
    key = {
        "owner": ("ACTOR#slack:456", "OWNER"), "channel": ("CHANNEL#slack:456", "PROFILE"),
        "backref": (f"USER#{user}", "CHANNEL#slack:456"),
    }[missing]
    del module.identity_table.items[key]
    module.agentcore_client = FakeRuntime()
    module.invoke_agent_runtime(module.get_or_create_session(user), user, "slack:456", "slack", "hi")
    assert not module.agentcore_client.calls


@pytest.mark.parametrize("mutation", ["expired", "draining", "generation", "code-owner", "canonical-owner"])
def test_binding_transaction_rechecks_code_route_and_ownership_at_commit(mutation):
    module = router()
    user = add_v2_bind_target(module)
    calls = len(module.identity_client.calls)

    def race():
        items = module.identity_table.items
        if mutation == "expired":
            items[("BIND#LINKCODE", "BIND")]["ttl"] = 0
        elif mutation == "draining":
            items[(f"USER#{user}", "SESSION")]["routeStatus"] = "DRAINING"
        elif mutation == "generation":
            items[(f"USER#{user}", "SESSION")]["generation"] += 1
        elif mutation == "code-owner":
            items[("BIND#LINKCODE", "BIND")]["userId"] = "other"
        else:
            items[("ACTOR#telegram:owner", "OWNER")]["userId"] = "other"

    module.identity_client.before_commit = race
    assert module.redeem_bind_code("LINKCODE", "slack", "456") == (None, False)
    assert len(module.identity_client.calls) == calls + 1
    assert ("ACTOR#slack:456", "OWNER") not in module.identity_table.items


def test_binding_code_can_only_be_redeemed_once_under_concurrency():
    module = router()
    user = add_v2_bind_target(module)
    barrier = threading.Barrier(2)
    module.identity_client.before_commit = lambda: barrier.wait(timeout=5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda channel_id: module.redeem_bind_code("LINKCODE", "slack", channel_id),
                                ["456", "789"]))
    assert results.count((user, True)) == 1
    assert results.count((None, False)) == 1
    assert ("BIND#LINKCODE", "BIND") not in module.identity_table.items
    assert len([key for key in module.identity_table.items if key[0].startswith("ACTOR#slack:")]) == 1


@pytest.mark.parametrize("target", [None, {}, {"platformVersion": "V1", "runtimeArn": ARN, "endpointName": "DEFAULT"}])
def test_enrollment_never_creates_v1_or_unverified_routes(target):
    module = router()
    with pytest.raises(routing.RouteUnavailable):
        enroll_new_user(module.identity_table, module.identity_client, "local-test", "slack", "456", "", target)
    assert not module.identity_client.calls and not module.identity_table.items


def test_legacy_actor_owner_is_rejected_without_reading_it_as_v2():
    module = router()
    module.identity_table.items[("ACTOR#slack:456", "OWNER")] = {
        "PK": "ACTOR#slack:456", "SK": "OWNER", "platformVersion": "V1",
        "userId": "old", "actorId": "slack:456", "ownerGeneration": 1,
    }
    before = copy.deepcopy(module.identity_table.items)
    with pytest.raises(routing.RouteUnavailable, match="reconciliation"):
        module.resolve_user("slack", "456")
    assert module.identity_table.items == before


def test_bound_channel_image_upload_uses_canonical_namespace_and_actual_delivery_target():
    module = router()
    user = add_v2_bind_target(module)
    assert module.redeem_bind_code("LINKCODE", "slack", "456") == (user, True)
    module.agentcore_client = FakeRuntime([{"response": "image received"}])
    event = {"type": "event_callback", "event": {
        "type": "message", "user": "456", "channel": "C123", "text": "hi",
        "files": [{"mimetype": "image/png", "url_private_download": "https://example.invalid/image.png"}],
    }}
    with patch.object(module, "_get_slack_tokens", return_value=("token", "signing")), patch.object(
        module, "_download_slack_file", return_value=(b"image", "image/png", "file.png"),
    ), patch.object(module, "_upload_image_to_s3", return_value="telegram_owner/_uploads/image.png") as upload, patch.object(
        module, "_slack_progress_notify",
    ), patch.object(module, "send_slack_message") as send:
        assert module.handle_slack(event)["statusCode"] == 200
    upload.assert_called_once_with(b"image", "telegram_owner", "image/png")
    send.assert_called_once_with("C123", "image received", "token")
    payload = json.loads(module.agentcore_client.calls[0]["payload"])
    assert payload["actorId"] == "telegram:owner" and payload["channelActorId"] == "slack:456"
    assert payload["channelTarget"] == "C123"


def test_bound_channel_cron_uses_canonical_session_and_delivers_to_scheduled_channel():
    module = router()
    user = add_v2_bind_target(module)
    assert module.redeem_bind_code("LINKCODE", "slack", "456") == (user, True)
    module.identity_table.items[(f"USER#{user}", "CRON#schedule123")] = {
        "PK": f"USER#{user}", "SK": "CRON#schedule123",
    }
    cron = load_lambda("cron")
    cron.identity_table = module.identity_table
    cron.agentcore_client = FakeRuntime([{"status": "ready", "phase": "USER_READY"}, {"response": "done"}])
    event = {
        "userId": user, "actorId": "telegram:owner", "channelActorId": "slack:456",
        "channel": "slack", "channelTarget": "C123", "message": "hi", "scheduleId": "schedule123",
    }
    with patch.object(cron, "deliver_response") as deliver:
        assert cron.handler(event, None)["statusCode"] == 200
    deliver.assert_called_once_with("slack", "C123", "done")
    for call in cron.agentcore_client.calls:
        payload = json.loads(call["payload"])
        assert payload["actorId"] == "telegram:owner" and payload["channelActorId"] == "slack:456"
        assert payload["channelTarget"] == "C123"


def test_binding_transaction_uses_low_level_dynamodb_shapes_without_network():
    module = router()
    user = add_v2_bind_target(module)
    assert module.redeem_bind_code("LINKCODE", "slack", "456") == (user, True)
    params = module.identity_client.calls[-1]
    assert len(params["TransactItems"]) == 9
    client = boto3.client("dynamodb", region_name="us-west-2", aws_access_key_id="local", aws_secret_access_key="local")
    with Stubber(client) as stubber:
        stubber.add_response("transact_write_items", {}, params)
        assert client.transact_write_items(**params) == {}
        stubber.assert_no_pending_responses()


@pytest.mark.parametrize("sort_key", ["PROFILE", "SESSION"])
def test_orphaned_identity_record_prevents_claiming_channel_with_bind_code(sort_key):
    module = router()
    add_v2_bind_target(module)
    pk = f"USER#{canonical('slack', '456')}"
    module.identity_table.items[(pk, sort_key)] = {"PK": pk, "SK": sort_key}
    before = copy.deepcopy(module.identity_table.items)
    assert module.redeem_bind_code("LINKCODE", "slack", "456") == (None, False)
    assert module.identity_table.items == before


def test_old_v2_endpoint_remains_pinned_after_new_registration_target_changes():
    module = router()
    user, _ = module.resolve_user("telegram", "123")
    route = module.get_or_create_session(user)
    module.RUNTIME_TARGET = {**module.RUNTIME_TARGET, "endpointName": "release_2", "runtimeVersion": "8"}
    assert module.get_or_create_session(user) == route
    assert module.get_or_create_session(user).endpoint_name == "release_1"
