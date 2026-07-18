from __future__ import annotations

import io
import json

import pytest

from router.product_commands import ProductCommand
from worker.control_client import ControlPlaneUncertain, LambdaProductCommandHandler
from worker.telegram_cards import TelegramCommandResult


TRACE = "po1_" + "a" * 64


class Lambda:
    def __init__(self, response=None, error=None):
        self.response = response or {
            "StatusCode": 200,
            "Payload": io.BytesIO(json.dumps({
                "status": "ok", "userId": "user_founder", "traceId": TRACE, "text": "safe"
            }).encode()),
        }
        self.error = error
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def test_control_client_sends_one_exact_bound_request_and_returns_text():
    client = Lambda()
    handler = LambdaProductCommandHandler(
        client, function_name="personal-operator-control-command:live"
    )
    result = handler.handle(
        user_id="user_founder",
        command=ProductCommand("/scan"),
        channel="telegram",
        trace_id=TRACE,
        idempotency_key=TRACE,
        chat_id="42",
        actor_id="telegram:42",
    )
    assert result == "safe"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["FunctionName"] == "personal-operator-control-command:live"
    assert call["InvocationType"] == "RequestResponse"
    assert json.loads(call["Payload"]) == {
        "action": "productCommand",
        "userId": "user_founder",
        "channel": "telegram",
        "command": "/scan",
        "chatId": "42",
        "actorId": "telegram:42",
        "traceId": TRACE,
        "idempotencyKey": TRACE,
    }


def test_control_client_accepts_only_exact_validated_card_schema():
    response = {
        "status": "ok",
        "userId": "user_founder",
        "traceId": TRACE,
        "text": "Reply to Ada",
        "telegram": {
            "inlineKeyboard": [[
                {"text": "Edit", "callbackData": "poc1:edit:AAAAAAAAAAAAAAAAAAAAAA"},
                {"text": "Prepare", "callbackData": "poc1:prepare:BBBBBBBBBBBBBBBBBBBBBB"},
                {"text": "Skip", "callbackData": "poc1:skip:CCCCCCCCCCCCCCCCCCCCCC"},
                {"text": "Why", "callbackData": "poc1:why:DDDDDDDDDDDDDDDDDDDDDD"},
            ]]
        },
    }
    client = Lambda(response={
        "StatusCode": 200,
        "Payload": io.BytesIO(json.dumps(response).encode()),
    })

    result = LambdaProductCommandHandler(
        client, function_name="personal-operator-control-command:live"
    ).handle(
        user_id="user_founder",
        command=ProductCommand("/scan"),
        channel="telegram",
        trace_id=TRACE,
        idempotency_key=TRACE,
        chat_id="42",
        actor_id="telegram:42",
    )

    assert isinstance(result, TelegramCommandResult)
    assert result.reply_markup()["inline_keyboard"][0][1]["text"] == "Prepare"


def test_callback_control_request_contains_only_bound_opaque_action():
    client = Lambda()
    handler = LambdaProductCommandHandler(
        client, function_name="personal-operator-control-command:live"
    )

    assert handler.handle_callback(
        user_id="user_founder",
        channel="telegram",
        trace_id=TRACE,
        idempotency_key=TRACE,
        chat_id="42",
        actor_id="telegram:42",
        callback_data="poc1:why:DDDDDDDDDDDDDDDDDDDDDD",
    ) == "safe"

    assert json.loads(client.calls[0]["Payload"]) == {
        "action": "telegramCallback",
        "userId": "user_founder",
        "channel": "telegram",
        "chatId": "42",
        "actorId": "telegram:42",
        "callbackData": "poc1:why:DDDDDDDDDDDDDDDDDDDDDD",
        "traceId": TRACE,
        "idempotencyKey": TRACE,
    }


def test_control_deletion_fence_requires_one_exact_bound_boolean():
    response = {
        "status": "ok",
        "userId": "user_founder",
        "traceId": TRACE,
        "blocked": True,
    }
    client = Lambda(response={
        "StatusCode": 200,
        "Payload": io.BytesIO(json.dumps(response).encode()),
    })
    handler = LambdaProductCommandHandler(
        client, function_name="personal-operator-control-command:live"
    )

    assert handler.deletion_blocked(
        user_id="user_founder",
        channel="telegram",
        trace_id=TRACE,
        idempotency_key=TRACE,
    ) is True
    assert json.loads(client.calls[0]["Payload"]) == {
        "action": "deletionFence",
        "userId": "user_founder",
        "channel": "telegram",
        "traceId": TRACE,
        "idempotencyKey": TRACE,
    }


def test_control_response_duplicate_keys_are_rejected_instead_of_last_wins():
    raw = (
        '{"status":"ok","userId":"user_founder","traceId":"'
        + TRACE
        + '","blocked":false,"blocked":true}'
    ).encode()
    handler = LambdaProductCommandHandler(
        Lambda(response={"StatusCode": 200, "Payload": io.BytesIO(raw)}),
        function_name="personal-operator-control-command:live",
    )

    with pytest.raises(ControlPlaneUncertain):
        handler.deletion_blocked(
            user_id="user_founder",
            channel="telegram",
            trace_id=TRACE,
            idempotency_key=TRACE,
        )


@pytest.mark.parametrize(
    "client",
    [
        Lambda(error=TimeoutError("unknown")),
        Lambda(response={"StatusCode": 200, "FunctionError": "Unhandled", "Payload": io.BytesIO(b"{}")}),
        Lambda(response={"StatusCode": 200, "Payload": io.BytesIO(b'{"status":"ok","text":"forged"}')}),
    ],
)
def test_control_client_fails_uncertain_without_retry(client):
    handler = LambdaProductCommandHandler(
        client, function_name="personal-operator-control-command:live"
    )
    with pytest.raises(ControlPlaneUncertain):
        handler.handle(
            user_id="user_founder",
            command=ProductCommand("/scan"),
            channel="telegram",
            trace_id=TRACE,
            idempotency_key=TRACE,
            chat_id="42",
            actor_id="telegram:42",
        )
    assert len(client.calls) == 1
