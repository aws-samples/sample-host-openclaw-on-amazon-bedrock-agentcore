from __future__ import annotations

from dataclasses import dataclass

import pytest

from worker.telegram_delivery import render_safe_telegram_html

from .index import ControlApplication, ControlRequestError
from .composition import LazyGmailService


USER = "user_founder"
TRACE = "po1_" + "a" * 64


class Tickets:
    def __init__(self):
        self.calls = []

    def issue(self, *, user_id, return_path):
        assert user_id == USER
        self.calls.append((user_id, return_path))
        return "signed-connect-ticket"


@dataclass
class Source:
    deep_link: str


@dataclass
class Opportunity:
    title: str
    reason: str
    source: Source
    id: str = "opp_1234567890abcdef"
    user_id: str = USER


class Gmail:
    def __init__(self):
        self.calls = []

    def scan(self, *, user_id):
        self.calls.append(user_id)
        return [
            Opportunity("Reply to Ada", "Waiting 7 days", Source("https://mail.google.test/a")),
            Opportunity("Send proposal", "You promised a draft", Source("https://mail.google.test/b")),
        ]


class Tasks:
    def __init__(self):
        self.calls = []

    def list_open(self, user_id):
        self.calls.append(user_id)
        return [{"title": "Approve email", "state": "APPROVAL_PENDING"}]


class DeletionIntents:
    def __init__(self, record=None, *, error=None):
        self.record = record
        self.error = error
        self.calls = []

    def get_deletion_intent(self, user_id):
        self.calls.append(user_id)
        if self.error is not None:
            raise self.error
        return self.record


class ApprovalProducer:
    def __init__(self, token="signed.approval.token"):
        self.token = token
        self.calls = []

    def prepare(self, *, user_id, opportunity):
        self.calls.append((user_id, opportunity))
        if self.token is None:
            return None
        return type(
            "PreparedApproval",
            (),
            {"token": self.token, "action_id": "gmail_fu_12345678"},
        )()


class Card:
    def __init__(self, item, offset=0):
        self.item = item
        self.offset = offset

    def to_control(self):
        return {
            "title": self.item.title,
            "reason": self.item.reason,
            "sourceUrl": self.item.source.deep_link,
            "buttons": [
                {
                    "text": label,
                    "callbackData": f"poc1:{action}:{chr(65 + self.offset + index) * 22}",
                }
                for index, (label, action) in enumerate(
                    (("Edit", "edit"), ("Prepare", "prepare"), ("Skip", "skip"), ("Why", "why"))
                )
            ],
        }


class CardActions:
    def __init__(self):
        self.issues = []
        self.consumes = []
        self.items = {}

    def issue(self, *, user_id, chat_id, actor_id, opportunities):
        self.issues.append((user_id, chat_id, actor_id, opportunities))
        cards = [Card(item, index * 4) for index, item in enumerate(opportunities)]
        for card in cards:
            for button in card.to_control()["buttons"]:
                self.items[button["callbackData"]] = (button["text"].lower(), card.item)
        return cards

    def consume(self, *, user_id, chat_id, actor_id, callback_data):
        self.consumes.append((user_id, chat_id, actor_id, callback_data))
        action, item = self.items[callback_data]
        return type("ConsumedCardAction", (), {"action": action, "opportunity": item})()


class Drafts:
    def __init__(self):
        self.calls = []

    def prepare(self, *, user_id, opportunity):
        self.calls.append((user_id, opportunity))
        return type(
            "PreparedDraft",
            (),
            {"action_id": "draft_1234567890abcdef", "revision": 1},
        )()


def app():
    return ControlApplication(
        tickets=Tickets(),
        gmail=Gmail(),
        tasks=Tasks(),
        deletion_intents=DeletionIntents(),
        web_origin="https://app.personal-operator.example",
        card_actions=CardActions(),
        draft_preparer=Drafts(),
    )


def request(command):
    return {
        "action": "productCommand",
        "userId": USER,
        "channel": "telegram",
        "command": command,
        "chatId": "42",
        "actorId": "telegram:42",
        "traceId": TRACE,
        "idempotencyKey": TRACE,
    }


def callback(callback_data):
    return {
        "action": "telegramCallback",
        "userId": USER,
        "channel": "telegram",
        "chatId": "42",
        "actorId": "telegram:42",
        "callbackData": callback_data,
        "traceId": TRACE,
        "idempotencyKey": TRACE,
    }


def deletion_fence_request():
    return {
        "action": "deletionFence",
        "userId": USER,
        "channel": "telegram",
        "traceId": TRACE,
        "idempotencyKey": TRACE,
    }


def test_connect_returns_one_time_secure_web_link_without_credentials():
    tickets = Tickets()
    application = ControlApplication(
        tickets=tickets,
        gmail=Gmail(),
        tasks=Tasks(),
        deletion_intents=DeletionIntents(),
        web_origin="https://app.personal-operator.example",
        card_actions=CardActions(),
        draft_preparer=Drafts(),
    )
    result = application.handle(request("/connect"))
    assert result == {
        "status": "ok",
        "userId": USER,
        "traceId": TRACE,
        "text": (
            "Open your private control surface:\n"
            "https://app.personal-operator.example/?ticket=signed-connect-ticket\n\n"
            "This link is one-time and expires in five minutes."
        ),
    }
    assert tickets.calls == [(USER, "/connections")]


def test_every_protected_telegram_destination_uses_a_bound_one_time_ticket():
    tickets = Tickets()
    cards = CardActions()
    application = ControlApplication(
        tickets=tickets,
        gmail=Gmail(),
        tasks=Tasks(),
        deletion_intents=DeletionIntents(),
        web_origin="https://app.personal-operator.example",
        card_actions=cards,
        draft_preparer=Drafts(),
    )

    start = application.handle(request("/start"))["text"]
    workspace = application.handle(request("/workspace"))["text"]
    deletion = application.handle(request("/delete"))["text"]
    status = application.handle(request("/status"))["text"]
    scan = application.handle(request("/scan"))
    draft = application.handle(
        callback(scan["telegram"]["inlineKeyboard"][0][0]["callbackData"])
    )["text"]

    assert all(
        "https://app.personal-operator.example/?ticket=signed-connect-ticket" in text
        for text in (start, workspace, deletion, status, draft)
    )
    assert tickets.calls == [
        (USER, "/connections"),
        (USER, "/workspace"),
        (USER, "/export"),
        (USER, "/delete"),
        (USER, "/"),
        (USER, "/workspace?draft=draft_1234567890abcdef"),
    ]


def test_deletion_fence_is_a_strong_read_only_boolean_before_any_product_work():
    active_intents = DeletionIntents()
    active = ControlApplication(
        tickets=Tickets(),
        gmail=Gmail(),
        tasks=Tasks(),
        deletion_intents=active_intents,
        web_origin="https://app.personal-operator.example",
    )
    assert active.handle(deletion_fence_request()) == {
        "status": "ok",
        "userId": USER,
        "traceId": TRACE,
        "blocked": False,
    }
    assert active_intents.calls == [USER]

    blocked_intents = DeletionIntents({
        "userId": USER,
        "purgeReason": "ACCOUNT_DELETION",
        "deletionStatus": "PENDING",
    })
    blocked = ControlApplication(
        tickets=Tickets(),
        gmail=Gmail(),
        tasks=Tasks(),
        deletion_intents=blocked_intents,
        web_origin="https://app.personal-operator.example",
    )
    assert blocked.handle(deletion_fence_request())["blocked"] is True
    assert blocked_intents.calls == [USER]


def test_scan_returns_source_backed_bounded_opportunities():
    result = app().handle(request("/scan"))
    assert result["status"] == "ok"
    assert "Reply to Ada" in result["text"]
    assert "https://mail.google.test/a" in result["text"]
    assert "Send proposal" in result["text"]
    assert [button["text"] for button in result["telegram"]["inlineKeyboard"][0]] == [
        "Edit", "Prepare", "Skip", "Why"
    ]
    assert all(
        set(button) == {"text", "callbackData"}
        for row in result["telegram"]["inlineKeyboard"]
        for button in row
    )


def test_three_worst_case_cards_still_fit_one_escaped_telegram_message():
    class WorstCaseGmail:
        def scan(self, *, user_id):
            assert user_id == USER
            return [
                Opportunity(
                    "&" * 120,
                    "&" * 280,
                    Source(
                        "https://mail.google.com/mail/u/0/#inbox/" + str(index) * 128
                    ),
                    id=f"opp_{index:016d}",
                )
                for index in range(1, 4)
            ]

    application = ControlApplication(
        tickets=Tickets(),
        gmail=WorstCaseGmail(),
        tasks=Tasks(),
        deletion_intents=DeletionIntents(),
        web_origin="https://app.personal-operator.example",
        card_actions=CardActions(),
        draft_preparer=Drafts(),
    )

    result = application.handle(request("/scan"))

    assert render_safe_telegram_html(result["text"])


def test_prepare_callback_surfaces_founder_approval_without_sending():
    producer = ApprovalProducer()
    cards = CardActions()
    drafts = Drafts()
    application = ControlApplication(
        tickets=Tickets(),
        gmail=Gmail(),
        tasks=Tasks(),
        deletion_intents=DeletionIntents(),
        web_origin="https://app.personal-operator.example",
        approval_producer=producer,
        card_actions=cards,
        draft_preparer=drafts,
    )

    scan = application.handle(request("/scan"))
    assert producer.calls == []
    result = application.handle(
        callback(scan["telegram"]["inlineKeyboard"][0][1]["callbackData"])
    )

    assert (
        "https://app.personal-operator.example/approve/signed.approval.token"
        in result["text"]
    )
    assert (
        "https://app.personal-operator.example/?ticket=signed-connect-ticket"
        in result["text"]
    )
    assert producer.calls[0][0] == USER
    assert producer.calls[0][1].title == "Reply to Ada"
    assert drafts.calls[0][1].title == "Reply to Ada"
    assert len(result["text"]) <= 3_500


def test_external_prepare_callback_never_creates_an_approval_or_send_transition():
    cards = CardActions()
    drafts = Drafts()
    producer = ApprovalProducer(token=None)
    application = ControlApplication(
        tickets=Tickets(),
        gmail=Gmail(),
        tasks=Tasks(),
        deletion_intents=DeletionIntents(),
        web_origin="https://app.personal-operator.example",
        approval_producer=producer,
        card_actions=cards,
        draft_preparer=drafts,
    )

    scan = application.handle(request("/scan"))
    result = application.handle(
        callback(scan["telegram"]["inlineKeyboard"][0][1]["callbackData"])
    )
    assert "/approve/" not in result["text"]
    assert "Nothing was sent" in result["text"]
    assert "?ticket=signed-connect-ticket" in result["text"]
    assert len(producer.calls) == 1
    assert len(drafts.calls) == 1


def test_edit_skip_and_why_callbacks_are_source_bound_read_only_responses():
    cards = CardActions()
    drafts = Drafts()
    application = ControlApplication(
        tickets=Tickets(),
        gmail=Gmail(),
        tasks=Tasks(),
        deletion_intents=DeletionIntents(),
        web_origin="https://app.personal-operator.example",
        card_actions=cards,
        draft_preparer=drafts,
    )
    scan = application.handle(request("/scan"))
    buttons = scan["telegram"]["inlineKeyboard"][0]

    edit = application.handle(callback(buttons[0]["callbackData"]))["text"]
    skip = application.handle(callback(buttons[2]["callbackData"]))["text"]
    why = application.handle(callback(buttons[3]["callbackData"]))["text"]

    assert (
        "https://app.personal-operator.example/?ticket=signed-connect-ticket"
        in edit
    )
    assert "Nothing was sent" in edit
    assert len(drafts.calls) == 1
    assert drafts.calls[0][0] == USER
    assert drafts.calls[0][1].title == "Reply to Ada"
    assert "Skipped" in skip and "Nothing was sent" in skip
    assert "Waiting 7 days" in why
    assert "https://mail.google.test/a" in why


def test_tasks_workspace_status_delete_and_start_are_deterministic():
    application = app()
    for command in ("/start", "/tasks", "/workspace", "/status", "/delete"):
        result = application.handle(request(command))
        assert result["status"] == "ok"
        assert 1 <= len(result["text"]) <= 3_500
    assert "Approve email" in application.handle(request("/tasks"))["text"]
    assert "does not delete" in application.handle(request("/delete"))["text"]


def test_control_request_is_exact_and_event_bound():
    for mutation in (
        {**request("/scan"), "botToken": "secret"},
        {**request("/scan"), "idempotencyKey": "po1_" + "b" * 64},
        {**request("/scan"), "userId": "../other"},
        {**request("/scan"), "command": "/unknown"},
    ):
        with pytest.raises(ControlRequestError):
            app().handle(mutation)


def test_provider_composition_is_lazy_and_only_scan_requires_it():
    calls = []

    class Provider:
        def scan(self, *, user_id):
            calls.append(("scan", user_id))
            return []

    lazy = LazyGmailService(lambda: calls.append(("build",)) or Provider())
    application = ControlApplication(
        tickets=Tickets(), gmail=lazy, tasks=Tasks(),
        deletion_intents=DeletionIntents(),
        web_origin="https://app.personal-operator.example",
        card_actions=CardActions(),
        draft_preparer=Drafts(),
    )

    for command in ("/start", "/connect", "/tasks", "/workspace", "/status", "/delete"):
        application.handle(request(command))
    assert calls == []

    application.handle(request("/scan"))
    assert calls == [("build",), ("scan", USER)]


@pytest.mark.parametrize("status", ["PENDING", "FINALIZING", "COMPLETED"])
@pytest.mark.parametrize("command", sorted({"/start", "/connect", "/scan", "/tasks", "/workspace", "/status", "/delete"}))
def test_any_account_deletion_intent_blocks_every_command_before_side_effects(
    status,
    command,
):
    class RecordingTickets:
        def __init__(self):
            self.calls = []

        def issue(self, **kwargs):
            self.calls.append(kwargs)
            return "must-not-be-issued"

    tickets = RecordingTickets()
    gmail = Gmail()
    tasks = Tasks()
    producer = ApprovalProducer()
    intents = DeletionIntents(
        {
            "userId": USER,
            "purgeReason": "ACCOUNT_DELETION",
            "deletionStatus": status,
        }
    )
    application = ControlApplication(
        tickets=tickets,
        gmail=gmail,
        tasks=tasks,
        deletion_intents=intents,
        web_origin="https://app.personal-operator.example",
        approval_producer=producer,
        card_actions=CardActions(),
        draft_preparer=Drafts(),
    )

    with pytest.raises(ControlRequestError, match="deletion"):
        application.handle(request(command))

    assert intents.calls == [USER]
    assert tickets.calls == []
    assert gmail.calls == []
    assert tasks.calls == []
    assert producer.calls == []


def test_deletion_intent_lookup_failure_is_fail_closed_before_side_effects():
    gmail = Gmail()
    with pytest.raises(RuntimeError, match="unavailable"):
        ControlApplication(
            tickets=Tickets(),
            gmail=gmail,
            tasks=Tasks(),
            deletion_intents=DeletionIntents(error=RuntimeError("intent unavailable")),
            web_origin="https://app.personal-operator.example",
        ).handle(request("/scan"))
    assert gmail.calls == []
