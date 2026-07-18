from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from workflows.gmail.models import Opportunity, SourceEvidence

from .telegram_cards import (
    CardActionAlreadyUsed,
    CardActionRejected,
    DynamoTelegramCardActions,
    ReadOnlyGmailDraftPreparer,
)


NOW = datetime(2026, 7, 18, 9, 30, tzinfo=timezone.utc)
USER = "pilot_alpha"
CHAT = "9001"
ACTOR = "telegram:42"


class ConditionalFailure(Exception):
    pass


class Table:
    def __init__(self):
        self.items = {}
        self.puts = []
        self.updates = []
        self.ambiguous_after_update = False
        self.decimal_numbers = False

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        key = (item["PK"], item["SK"])
        self.puts.append(kwargs)
        if key in self.items:
            raise ConditionalFailure("duplicate")
        self.items[key] = dict(item)
        return {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        item = self.items.get(key)
        values = kwargs["ExpressionAttributeValues"]
        if (
            item is None
            or item.get("userId") != values[":userId"]
            or item.get("chatId") != values[":chatId"]
            or item.get("actorId") != values[":actorId"]
            or item.get("action") != values[":action"]
            or item.get("ttl", 0) <= values[":now"]
            or "consumedAt" in item
        ):
            raise ConditionalFailure("condition")
        item["consumedAt"] = (
            Decimal(values[":now"])
            if self.decimal_numbers
            else values[":now"]
        )
        if self.ambiguous_after_update:
            raise TimeoutError("result unknown")
        return {"Attributes": dict(item)}

    def get_item(self, **kwargs):
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        item = self.items.get(key)
        return {"Item": dict(item)} if item is not None else {}

    def delete_item(self, **kwargs):
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        item = self.items.get(key)
        expected = kwargs.get("ExpressionAttributeValues", {}).get(":generation")
        if expected is not None and (
            item is None or item.get("connectionGeneration") != expected
        ):
            raise ConditionalFailure("generation changed")
        self.items.pop(key, None)
        return {}


class Fence:
    def __init__(self, generation=1, connected=True):
        self.generation = generation
        self.connected = connected

    def connected_generation(self, user_id):
        assert user_id == USER
        if not self.connected:
            raise RuntimeError("disconnected")
        return self.generation

    def assert_generation(self, user_id, generation, *, require_connected=False):
        assert user_id == USER
        if generation != self.generation or (require_connected and not self.connected):
            raise RuntimeError("stale generation")

    def opportunities_match(self, user_id, generation, opportunities):
        assert user_id == USER
        return self.connected and generation == self.generation and list(opportunities) == [
            opportunity()
        ]


def opportunity() -> Opportunity:
    waiting = NOW - timedelta(days=7)
    source = SourceEvidence(
        source_id="gmail:thread_a:message_a",
        thread_id="thread_a",
        deep_link="https://mail.google.com/mail/u/0/#inbox/thread_a",
        correspondent="ada@example.net",
        subject="Proposal",
        excerpt="Could you send the next draft?",
        waiting_since=waiting,
    )
    return Opportunity(
        id="opp_1234567890abcdef",
        user_id=USER,
        source=source,
        waiting_since=waiting,
        title="Reply to Ada",
        reason="Ada has been waiting seven days.",
        confidence=0.91,
    )


def store(table: Table, tokens=None) -> DynamoTelegramCardActions:
    available = iter(tokens or [
        "AAAAAAAAAAAAAAAAAAAAAA",
        "BBBBBBBBBBBBBBBBBBBBBB",
        "CCCCCCCCCCCCCCCCCCCCCC",
        "DDDDDDDDDDDDDDDDDDDDDD",
    ])
    return DynamoTelegramCardActions(
        table,
        now=lambda: NOW,
        token_factory=lambda: next(available),
        conditional_failure_types=(ConditionalFailure,),
    )


def test_issues_one_source_backed_card_with_four_opaque_bounded_actions():
    table = Table()

    cards = store(table).issue(
        user_id=USER,
        chat_id=CHAT,
        actor_id=ACTOR,
        opportunities=[opportunity()],
    )

    assert len(cards) == 1
    card = cards[0].to_control()
    assert card == {
        "title": "Reply to Ada",
        "reason": "Ada has been waiting seven days.",
        "sourceUrl": "https://mail.google.com/mail/u/0/#inbox/thread_a",
        "buttons": [
            {"text": "Edit", "callbackData": "poc1:edit:AAAAAAAAAAAAAAAAAAAAAA"},
            {"text": "Prepare", "callbackData": "poc1:prepare:BBBBBBBBBBBBBBBBBBBBBB"},
            {"text": "Skip", "callbackData": "poc1:skip:CCCCCCCCCCCCCCCCCCCCCC"},
            {"text": "Why", "callbackData": "poc1:why:DDDDDDDDDDDDDDDDDDDDDD"},
        ],
    }
    assert len(table.puts) == 4
    for call in table.puts:
        item = call["Item"]
        assert item["PK"] == f"USER#{USER}"
        assert item["SK"].startswith("TELEGRAM_CALLBACK#")
        assert item["userId"] == USER
        assert item["chatId"] == CHAT
        assert item["actorId"] == ACTOR
        assert item["ttl"] == int((NOW + timedelta(days=14)).timestamp())
        assert "callbackData" not in item
        assert "body" not in str(item).lower()


def test_consumes_exact_user_chat_actor_action_once_and_reconstructs_source():
    table = Table()
    cards = store(table).issue(
        user_id=USER,
        chat_id=CHAT,
        actor_id=ACTOR,
        opportunities=[opportunity()],
    )
    why = cards[0].to_control()["buttons"][3]["callbackData"]
    actions = DynamoTelegramCardActions(table, now=lambda: NOW)

    consumed = actions.consume(
        user_id=USER,
        chat_id=CHAT,
        actor_id=ACTOR,
        callback_data=why,
    )

    assert consumed.action == "why"
    assert consumed.opportunity == opportunity()
    with pytest.raises(CardActionAlreadyUsed):
        actions.consume(
            user_id=USER,
            chat_id=CHAT,
            actor_id=ACTOR,
            callback_data=why,
        )


def test_disconnect_generation_invalidates_issued_callback_and_blocks_draft_recreation():
    table = Table()
    fence = Fence()
    tokens = iter(
        [
            "AAAAAAAAAAAAAAAAAAAAAA",
            "BBBBBBBBBBBBBBBBBBBBBB",
            "CCCCCCCCCCCCCCCCCCCCCC",
            "DDDDDDDDDDDDDDDDDDDDDD",
        ]
    )
    actions = DynamoTelegramCardActions(
        table,
        now=lambda: NOW,
        token_factory=lambda: next(tokens),
        conditional_failure_types=(ConditionalFailure,),
        connection_fence=fence,
    )
    cards = actions.issue(
        user_id=USER,
        chat_id=CHAT,
        actor_id=ACTOR,
        opportunities=[opportunity()],
    )
    prepare = cards[0].to_control()["buttons"][1]["callbackData"]
    assert all(item["connectionGeneration"] == 1 for item in table.items.values())

    fence.generation = 2
    fence.connected = False

    with pytest.raises(CardActionRejected):
        actions.consume(
            user_id=USER,
            chat_id=CHAT,
            actor_id=ACTOR,
            callback_data=prepare,
        )


def test_cross_tenant_chat_actor_and_forged_action_are_rejected_before_use():
    table = Table()
    cards = store(table).issue(
        user_id=USER,
        chat_id=CHAT,
        actor_id=ACTOR,
        opportunities=[opportunity()],
    )
    prepare = cards[0].to_control()["buttons"][1]["callbackData"]
    actions = DynamoTelegramCardActions(table, now=lambda: NOW)

    for mutation in (
        {"user_id": "pilot_bravo"},
        {"chat_id": "9002"},
        {"actor_id": "telegram:43"},
        {"callback_data": prepare.replace(":prepare:", ":why:")},
    ):
        arguments = {
            "user_id": USER,
            "chat_id": CHAT,
            "actor_id": ACTOR,
            "callback_data": prepare,
            **mutation,
        }
        with pytest.raises(CardActionRejected):
            actions.consume(**arguments)

    assert all("consumedAt" not in item for item in table.items.values())


def test_ambiguous_consume_is_reconciled_as_used_and_never_returns_effect_work():
    table = Table()
    cards = store(table).issue(
        user_id=USER,
        chat_id=CHAT,
        actor_id=ACTOR,
        opportunities=[opportunity()],
    )
    prepare = cards[0].to_control()["buttons"][1]["callbackData"]
    table.ambiguous_after_update = True

    with pytest.raises(CardActionAlreadyUsed):
        DynamoTelegramCardActions(table, now=lambda: NOW).consume(
            user_id=USER,
            chat_id=CHAT,
            actor_id=ACTOR,
            callback_data=prepare,
        )

    assert len(table.updates) == 1


def test_dynamodb_decimal_numbers_are_accepted_only_when_integral():
    table = Table()
    cards = store(table).issue(
        user_id=USER,
        chat_id=CHAT,
        actor_id=ACTOR,
        opportunities=[opportunity()],
    )
    why = cards[0].to_control()["buttons"][3]["callbackData"]
    for item in table.items.values():
        item["ttl"] = Decimal(item["ttl"])
        item["createdAt"] = Decimal(item["createdAt"])
    table.decimal_numbers = True

    consumed = DynamoTelegramCardActions(table, now=lambda: NOW).consume(
        user_id=USER,
        chat_id=CHAT,
        actor_id=ACTOR,
        callback_data=why,
    )

    assert consumed.action == "why"


def test_prepare_persists_a_deterministic_source_bound_local_draft_without_send():
    class Repository:
        def __init__(self):
            self.calls = []

        def save_draft(self, **kwargs):
            self.calls.append(kwargs)

    repository = Repository()
    preparer = ReadOnlyGmailDraftPreparer(repository, now=lambda: NOW)

    first = preparer.prepare(user_id=USER, opportunity=opportunity())
    second = preparer.prepare(user_id=USER, opportunity=opportunity())

    assert first == second
    assert first.action_id.startswith("draft_")
    assert first.to == "ada@example.net"
    assert first.subject == "Re: Proposal"
    assert "Proposal" in first.body
    assert [call["expires_at"] for call in repository.calls] == [
        int((NOW + timedelta(days=14)).timestamp()),
        int((NOW + timedelta(days=14)).timestamp()),
    ]
    assert not hasattr(preparer, "send")


def test_prepare_forwards_the_consumed_connection_generation_to_the_draft_write():
    class Repository:
        def __init__(self):
            self.calls = []

        def save_draft(self, **kwargs):
            self.calls.append(kwargs)

    repository = Repository()
    preparer = ReadOnlyGmailDraftPreparer(repository, now=lambda: NOW)

    preparer.prepare(
        user_id=USER,
        opportunity=opportunity(),
        connection_generation=9,
    )

    assert repository.calls[0]["expected_generation"] == 9
