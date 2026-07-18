"""Tests for the typed Telegram FIFO boundary."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest


ROUTER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROUTER_DIR))

from message_queue import (  # noqa: E402
    EnvelopeValidationError,
    QueueEnvelope,
    build_fifo_send_request,
)


def envelope(**overrides):
    values = {
        "user_id": "user_ab12",
        "channel": "telegram",
        "update_id": "42001",
        "trace_id": "trace_42001",
        "kind": "message",
        "payload": {
            "chatId": "991",
            "actorId": "telegram:77",
            "message": "please summarize this",
        },
    }
    values.update(overrides)
    return QueueEnvelope(**values)


def test_wire_envelope_has_exact_typed_shape_and_canonical_json():
    item = envelope()

    wire = item.to_wire()
    assert tuple(wire) == ("userId", "channel", "updateId", "traceId", "kind", "payload")
    assert json.loads(item.to_json()) == wire
    assert QueueEnvelope.from_json(item.to_json()) == item


def test_fifo_group_is_user_and_dedupe_is_stable_for_receipt_replays():
    first = envelope(trace_id="receipt_a")
    replay = envelope(trace_id="receipt_b")
    mutated_receipt = envelope(
        trace_id="receipt_c",
        payload={"chatId": "991", "actorId": "telegram:77", "message": "mutated retry body"},
    )

    assert first.message_group_id == "user_ab12"
    assert first.message_deduplication_id == replay.message_deduplication_id
    assert first.message_deduplication_id != mutated_receipt.message_deduplication_id
    assert len(first.message_deduplication_id) == 64
    assert envelope(update_id="42002").message_deduplication_id != first.message_deduplication_id
    assert envelope(user_id="user_other").message_deduplication_id != first.message_deduplication_id


def test_one_hundred_replayed_updates_have_one_fifo_identity():
    dedupe_ids = {
        envelope(trace_id=f"receipt_{index}").message_deduplication_id
        for index in range(100)
    }
    assert len(dedupe_ids) == 1


def test_fifo_request_contains_explicit_group_and_deduplication_ids():
    item = envelope()

    request = build_fifo_send_request("https://sqs.eu-west-1.amazonaws.com/1/router.fifo", item)

    assert request == {
        "QueueUrl": "https://sqs.eu-west-1.amazonaws.com/1/router.fifo",
        "MessageBody": item.to_json(),
        "MessageGroupId": item.message_group_id,
        "MessageDeduplicationId": item.message_deduplication_id,
    }


@pytest.mark.parametrize(
    "unsafe_payload",
    [
        {"chatId": "1", "actorId": "telegram:7", "message": "x", "botToken": "secret"},
        {"chatId": "1", "actorId": "telegram:7", "message": {"text": "x", "api_key": "secret"}},
        {"chatId": "1", "actorId": "telegram:7", "message": "x", "runtimeSessionId": "client"},
        {"chatId": "1", "actorId": "telegram:7", "message": {"credentials": {"x": "y"}}},
        {"chatId": "1", "actorId": "telegram:7", "message": "x", "headers": {"Authorization": "x"}},
    ],
)
def test_credentials_sessions_and_opaque_request_headers_are_rejected(unsafe_payload):
    with pytest.raises(EnvelopeValidationError):
        envelope(payload=unsafe_payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_id", "../other"),
        ("channel", "slack"),
        ("update_id", ""),
        ("trace_id", "contains whitespace"),
        ("kind", "effect"),
    ],
)
def test_identity_and_discriminator_bounds_are_fail_closed(field, value):
    with pytest.raises(EnvelopeValidationError):
        envelope(**{field: value})


def test_payload_shape_must_match_kind():
    with pytest.raises(EnvelopeValidationError):
        envelope(payload={"chatId": "1", "actorId": "telegram:7", "command": "/status"})

    command = envelope(
        kind="command",
        payload={"chatId": "1", "actorId": "telegram:7", "command": "/status"},
    )
    assert command.payload["command"] == "/status"


def test_json_parser_rejects_extra_fields_nonfinite_numbers_and_oversize_body():
    wire = envelope().to_wire()
    wire["unexpected"] = True
    with pytest.raises(EnvelopeValidationError):
        QueueEnvelope.from_wire(wire)

    bad_number = envelope().to_wire()
    bad_number["payload"]["message"] = {"value": math.inf}
    with pytest.raises(EnvelopeValidationError):
        QueueEnvelope.from_wire(bad_number)

    with pytest.raises(EnvelopeValidationError):
        QueueEnvelope.from_json("{")

    with pytest.raises(EnvelopeValidationError):
        QueueEnvelope.from_json(" " * 140_000)


def test_message_text_may_discuss_tokens_without_becoming_a_credential_field():
    item = envelope(payload={
        "chatId": "1",
        "actorId": "telegram:7",
        "message": "Explain how OAuth tokens work without using any credentials.",
    })
    assert "OAuth tokens" in item.payload["message"]


def test_constructed_envelope_cannot_be_mutated_after_validation():
    item = envelope()

    with pytest.raises(AttributeError):
        item.user_id = "user_other"

    exposed = item.payload
    exposed["chatId"] = "777"
    assert item.payload["chatId"] == "991"
    assert json.loads(item.to_json())["payload"]["chatId"] == "991"


def test_duplicate_json_keys_are_rejected_instead_of_last_value_winning():
    body = envelope().to_json().replace(
        '"userId":"user_ab12"',
        '"userId":"user_other","userId":"user_ab12"',
    )
    with pytest.raises(EnvelopeValidationError):
        QueueEnvelope.from_json(body)
