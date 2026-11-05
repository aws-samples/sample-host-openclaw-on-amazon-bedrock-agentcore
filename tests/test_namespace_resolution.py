"""Stored user namespaces: resolution, the NS# owner check and the MIGRATING route state.

A SESSION without a namespace keeps the legacy namespace (the runtime actor with
':' as '_'). A stored namespace is used only when NS#<namespace>/OWNER, which
is outside the user's own partition, names the same user. All IDs are synthetic.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# The Router imports its own modules (documents.py, telegram_group.py) by name.
_ROUTER_DIR = str(Path(__file__).resolve().parents[1] / "lambda" / "router")
if _ROUTER_DIR not in sys.path:
    sys.path.insert(0, _ROUTER_DIR)

from tests.test_runtime_routing import (  # noqa: E402
    ACTOR, SESSION, USER, FakeRuntime, FakeTable, candidate, cron_event, cron_table, load_lambda, routing,
)

LEGACY = "telegram_123"
OPAQUE = "ns_" + "abcdefghijklmnopqrstuvwxyz"[:26]
OTHER_OPAQUE = "ns_" + "z" * 26
OTHER_USER = "user_" + "f" * 16
SESSION_KEY = (f"USER#{USER}", "SESSION")


def owner_item(namespace, user_id=USER):
    return {"PK": f"NS#{namespace}", "SK": "OWNER", "userId": user_id}


def stored(table, namespace, owner=USER):
    """Store namespace on the route and, unless owner is None, its NS# owner record."""
    table.items[SESSION_KEY]["namespace"] = namespace
    if owner is not None:
        table.items[(f"NS#{namespace}", "OWNER")] = owner_item(namespace, owner)
    return table


def gets(table):
    return [call[1]["Key"]["PK"] for call in table.calls if call[0] == "get"]


def test_route_without_a_stored_namespace_uses_the_legacy_namespace_and_reads_no_owner_record():
    table = FakeTable(candidate())
    route = routing.resolve_route(table, USER)
    assert route.namespace == LEGACY and not route.namespace_stored
    assert route.payload_context() == {"runtimeSessionId": SESSION, "routingGeneration": 1, "namespace": LEGACY}
    assert gets(table) == [f"USER#{USER}"]


@pytest.mark.parametrize("namespace", [OPAQUE, LEGACY])
def test_stored_namespace_named_by_its_owner_record_is_used(namespace):
    table = stored(FakeTable(candidate()), namespace)
    route = routing.resolve_route(table, USER)
    assert route.namespace == namespace and route.namespace_stored
    assert route.payload_context()["namespace"] == namespace
    owner_read = [call[1] for call in table.calls if call[1]["Key"]["PK"] == f"NS#{namespace}"]
    assert owner_read == [{"Key": {"PK": f"NS#{namespace}", "SK": "OWNER"}, "ConsistentRead": True}]


@pytest.mark.parametrize("owner", [OTHER_USER, None, ""])
def test_stored_namespace_is_not_trusted_without_an_owner_record_naming_the_user(owner):
    # Only the NS# owner record, outside the user's partition, confirms a stored namespace.
    table = stored(FakeTable(candidate()), OPAQUE, owner=OTHER_USER if owner == OTHER_USER else None)
    if owner == "":
        table.items[(f"NS#{OPAQUE}", "OWNER")] = {"PK": f"NS#{OPAQUE}", "SK": "OWNER"}
    with pytest.raises(routing.RouteUnavailable, match="not owned by this user"):
        routing.resolve_route(table, USER)


@pytest.mark.parametrize("value", ["telegram_999", "slack_U123", "ns_short", "NS_" + "A" * 26, "", 7, None])
def test_stored_namespace_of_another_actor_or_shape_is_refused_before_any_owner_lookup(value):
    table = FakeTable(candidate())
    table.items[SESSION_KEY]["namespace"] = value
    if isinstance(value, str) and value:
        table.items[(f"NS#{value}", "OWNER")] = owner_item(value)
    with pytest.raises(routing.RouteUnavailable, match="Invalid stored namespace"):
        routing.resolve_route(table, USER)
    assert gets(table) == [f"USER#{USER}"]


def test_migrating_route_is_refused_as_route_migrating():
    table = FakeTable({**candidate(), "routeStatus": "MIGRATING"})
    with pytest.raises(routing.RouteMigrating) as raised:
        routing.resolve_route(table, USER)
    assert isinstance(raised.value, routing.RouteUnavailable)


@pytest.mark.parametrize("before,after", [(OPAQUE, OTHER_OPAQUE), (None, OPAQUE), (OPAQUE, None)])
def test_admission_fails_when_the_stored_namespace_changes_after_resolution(before, after):
    table = FakeTable(candidate())
    if before:
        stored(table, before)
    route = routing.resolve_route(table, USER)
    if after:
        stored(table, after)
    else:
        del table.items[SESSION_KEY]["namespace"]
    with pytest.raises(routing.RouteUnavailable):
        with routing.admit_route(table, route, USER, ACTOR):
            pytest.fail("a route whose namespace changed was admitted")
    assert table.items[SESSION_KEY].get("inFlight", 0) == 0


def test_router_payload_carries_the_stored_namespace():
    module = load_lambda("router")
    module.identity_table = stored(FakeTable(candidate()), OPAQUE)
    module.agentcore_client = runtime = FakeRuntime([{"response": "ok"}])
    route = module.get_or_create_session(USER)
    module.invoke_agent_runtime(route, USER, ACTOR, "telegram", "hi")
    payload = json.loads(runtime.calls[0]["payload"])
    assert payload["namespace"] == OPAQUE and payload["actorId"] == ACTOR


def test_router_refuses_a_namespace_its_owner_record_does_not_confirm():
    module = load_lambda("router")
    module.identity_table = stored(FakeTable(candidate()), OPAQUE, owner=OTHER_USER)
    with pytest.raises(module.RouteUnavailable):
        module.get_or_create_session(USER)


@pytest.mark.parametrize("migrating", [False, True])
def test_router_route_or_reply_sends_the_maintenance_reply_only_while_migrating(migrating):
    module = load_lambda("router")
    item = {**candidate(), "routeStatus": "MIGRATING"} if migrating else candidate()
    module.identity_table = FakeTable(item)
    replies = []
    route = module._route_or_reply(USER, replies.append)
    if migrating:
        assert route is None
        assert replies == ["Maintenance in progress, please send that again in a minute."]
    else:
        assert route.namespace == LEGACY and replies == []


def stored_cron_table(namespace=OPAQUE, owner=USER):
    return stored(cron_table(), namespace, owner)


def test_cron_payloads_and_brief_audio_use_the_stored_namespace():
    module = load_lambda("cron")
    module.identity_table = stored_cron_table()
    module.agentcore_client = FakeRuntime([{"status": "ready", "phase": "USER_READY"}, {"response": "done"}])
    module.find_run_audio_key = MagicMock(return_value=f"{OPAQUE}/brief.mp3")
    module.deliver_brief_audio = MagicMock()
    with patch.object(module, "deliver_response"):
        assert module.handler(cron_event(), None)["statusCode"] == 200
    assert [json.loads(call["payload"])["namespace"] for call in module.agentcore_client.calls] == [OPAQUE, OPAQUE]
    assert module.find_run_audio_key.call_args.args[0] == OPAQUE
    assert module.deliver_brief_audio.call_args.args[3] == OPAQUE


def test_cron_brief_audio_under_the_legacy_prefix_is_refused_for_a_stored_namespace():
    module = load_lambda("cron")
    assert module._valid_audio_key(f"{OPAQUE}/brief.mp3", OPAQUE)
    assert not module._valid_audio_key(f"{LEGACY}/brief.mp3", OPAQUE)


@pytest.mark.parametrize("change", ["unconfirmed", "migrating"])
def test_cron_does_not_run_for_an_unconfirmed_namespace_or_a_migrating_route(change):
    module = load_lambda("cron")
    if change == "unconfirmed":
        module.identity_table = stored_cron_table(owner=OTHER_USER)
    else:
        module.identity_table = cron_table()
        module.identity_table.items[SESSION_KEY]["routeStatus"] = "MIGRATING"
    module.agentcore_client = FakeRuntime()
    with patch.object(module, "deliver_response") as deliver:
        result = module.handler(cron_event(), None)
    assert result == {"statusCode": 503, "body": "Runtime route unavailable"}
    assert not module.agentcore_client.calls
    deliver.assert_not_called()


# --- Telegram end to end (real enrollment, FakeTable) --------------------------------

from tests.test_telegram_groups import (  # noqa: E402
    BOT, GROUP, MEMBER, canonical, group_message, router, run, runtime_payloads,
)


def private_message(text="hello", **extra):
    message = {"message_id": 5, "chat": {"id": MEMBER, "type": "private"},
               "from": {"id": MEMBER, "first_name": "Alex"}, "text": text}
    message.update(extra)
    return {"message": message}


def session_of(module, user):
    return module.identity_table.items[(f"USER#{user}", "SESSION")]


def test_telegram_private_chat_gets_the_maintenance_reply_while_migrating():
    module = router()
    run(module, private_message("first"))  # enrolls the user and runs one turn
    session_of(module, canonical("telegram", MEMBER))["routeStatus"] = "MIGRATING"
    api = run(module, private_message("second"))
    assert len(module.agentcore_client.calls) == 1
    reply, = api.sent()
    assert reply["chat_id"] == MEMBER and reply["text"] == module.MIGRATION_REPLY


def test_telegram_group_gets_the_maintenance_reply_in_the_group_while_migrating():
    module = router()
    run(module, group_message(f"@{BOT} first"))
    session_of(module, canonical("telegram_group", GROUP))["routeStatus"] = "MIGRATING"
    api = run(module, group_message(f"@{BOT} second", message_id=43))
    assert len(module.agentcore_client.calls) == 1
    reply, = api.sent()
    assert reply["chat_id"] == str(GROUP) and reply["text"] == module.MIGRATION_REPLY
    assert reply["reply_parameters"]["message_id"] == 43


def test_telegram_turn_of_a_user_with_a_stored_namespace_uses_it_for_uploads_and_the_payload():
    module = router()
    run(module, private_message("first"))
    user = canonical("telegram", MEMBER)
    session_of(module, user)["namespace"] = OPAQUE
    module.identity_table.items[(f"NS#{OPAQUE}", "OWNER")] = owner_item(OPAQUE, user)
    uploads = []

    def upload(data, namespace, content_type):
        uploads.append(namespace)
        return f"{namespace}/_uploads/img_1_abcd.png"

    update = private_message("", photo=[{"file_id": "f1"}], caption="what is this?")
    update["message"].pop("text")
    with patch.object(module, "_download_telegram_image", return_value=(b"png", "image/png", "a.png")), \
            patch.object(module, "_upload_image_to_s3", side_effect=upload):
        run(module, update)
    assert uploads == [OPAQUE]
    payload = runtime_payloads(module)[-1]
    assert payload["namespace"] == OPAQUE
    assert payload["message"]["images"][0]["s3Key"].startswith(f"{OPAQUE}/_uploads/")


def test_telegram_turn_with_a_namespace_the_owner_record_does_not_confirm_never_runs():
    module = router()
    run(module, private_message("first"))
    user = canonical("telegram", MEMBER)
    session_of(module, user)["namespace"] = OPAQUE  # no NS# owner record confirms it
    with pytest.raises(module.RouteUnavailable, match="not owned by this user"):
        run(module, private_message("second"))
    assert len(module.agentcore_client.calls) == 1
    module.identity_table.items[(f"NS#{OPAQUE}", "OWNER")] = owner_item(OPAQUE, OTHER_USER)
    with pytest.raises(module.RouteUnavailable, match="not owned by this user"):
        run(module, private_message("third"))
    assert len(module.agentcore_client.calls) == 1


# --- Slack channels and Feishu group chats (channel_identity_mode "channel") --------------

from tests import test_channel_identity_mode as conversations  # noqa: E402


def enrolled_conversation(kind):
    """A router whose Slack channel or Feishu group identity has run one turn."""
    module = conversations.router()
    if kind == "slack":
        conversations.run_slack(module, conversations.slack_event())
        actor = f"slack_channel:{conversations.CHANNEL}"
    else:
        conversations.run_feishu(module, conversations.feishu_event())
        actor = f"feishu_group:{conversations.FEISHU_CHAT}"
    return module, actor, conversations.canonical(actor)


@pytest.mark.parametrize("kind", ["slack", "feishu"])
def test_conversation_identity_gets_the_maintenance_reply_while_migrating(kind):
    module, _actor, user = enrolled_conversation(kind)
    session_of(module, user)["routeStatus"] = "MIGRATING"
    if kind == "slack":
        api = conversations.run_slack(module, conversations.slack_event(event_id="Ev0002", ts="1700000000.000300"))
        post, = api.slack_posts()
        assert post["channel"] == conversations.CHANNEL and post["text"] == module.MIGRATION_REPLY
        assert post["thread_ts"] == "1700000000.000300"
    else:
        api = conversations.run_feishu(module, conversations.feishu_event(event_id="evt_0002"))
        assert api.feishu_sends() == [(conversations.FEISHU_CHAT, module.MIGRATION_REPLY)]
    assert len(module.agentcore_client.calls) == 1


@pytest.mark.parametrize("kind", ["slack", "feishu"])
@pytest.mark.parametrize("stored", ["legacy", "opaque"])
def test_conversation_identity_uploads_and_payloads_use_its_stored_namespace(kind, stored):
    module, actor, user = enrolled_conversation(kind)
    namespace = actor.replace(":", "_") if stored == "legacy" else OPAQUE
    session_of(module, user)["namespace"] = namespace
    module.identity_table.items[(f"NS#{namespace}", "OWNER")] = owner_item(namespace, user)
    uploads = []

    def upload(data, upload_namespace, content_type):
        uploads.append(upload_namespace)
        return f"{upload_namespace}/_uploads/img_1_abcd.png"

    with patch.object(module, "_upload_image_to_s3", side_effect=upload):
        if kind == "slack":
            envelope = conversations.slack_event(
                text=f"<@{conversations.BOT_USER}> what is this?", event_type="message", subtype="file_share",
                event_id="Ev0003", ts="1700000000.000400",
                files=[{"mimetype": "image/png", "url_private_download": "https://files.slack.com/x"}])
            with patch.object(module, "_download_slack_file", return_value=(b"png", "image/png", "a.png")):
                conversations.run_slack(module, envelope)
        else:
            envelope = conversations.feishu_event(event_id="evt_0003")
            message = envelope["event"]["message"]
            message.update(message_type="image", content=json.dumps({"image_key": "img_v3_fixture"}))
            with patch.object(module, "_download_feishu_image", return_value=(b"png", "image/png", "a.png")):
                conversations.run_feishu(module, envelope)
    assert uploads == [namespace]
    payload = conversations.payloads(module)[-1]
    assert payload["namespace"] == namespace and payload["actorId"] == actor
