import base64
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
import importlib.util
from pathlib import Path
import sys

import pytest


ACTIONS_DIR = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ACTIONS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


models = load("action_models", "models.py")
send_module = load("action_gmail_send", "gmail_send.py")
PROVIDER_TIME = datetime(2026, 7, 18, 11, 59, 53, tzinfo=timezone.utc)


class Request:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def execute(self):
        if self.error:
            raise self.error
        return self.result


class Messages:
    def __init__(self):
        self.send_result = {"id": "gmail-1", "threadId": "thread-1"}
        self.list_result = {"messages": []}
        self.get_result = None
        self.send_calls = []
        self.list_calls = []
        self.get_calls = []

    def send(self, **kwargs):
        self.send_calls.append(kwargs)
        return Request(self.send_result)

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return Request(self.list_result)

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return Request(self.get_result)


class Users:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class GmailService:
    def __init__(self, messages):
        self._users = Users(messages)

    def users(self):
        return self._users


def adapter(messages):
    return send_module.GmailApiAdapter(
        GmailService(messages),
        connection_id="google_conn_1234",
        account_email="founder@example.com",
        user_id="me",
    )


def raw_message(message_id, *, sender="founder@example.com", recipient="person@example.net", body="Hello"):
    message = EmailMessage(policy=SMTP)
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = "Hi"
    message["Message-ID"] = message_id
    message.set_content(body, subtype="plain", charset="utf-8")
    return message.as_bytes(policy=SMTP)


def provider_record(raw, *, labels=None, timestamp=PROVIDER_TIME):
    return {
        "id": "gmail-1",
        "threadId": "thread-1",
        "labelIds": labels or ["SENT"],
        "internalDate": str(int(timestamp.timestamp() * 1000)),
        "raw": base64.urlsafe_b64encode(raw).decode("ascii"),
    }


def test_send_returns_only_exact_sent_evidence_with_provider_execution_time():
    messages = Messages()
    message_id = "<po-aaaaaaaaaaaaaaaaaaaaaaaa@personal-operator.invalid>"
    raw = raw_message(message_id)
    args = {"to": "person@example.net", "subject": "Hi", "body": "Hello"}
    payload_hash = models.canonical_args_hash(args)
    messages.get_result = provider_record(raw)

    evidence = adapter(messages).send_raw(
        raw=raw,
        message_id=message_id,
        idempotency_key=message_id,
        payload_hash=payload_hash,
    )

    assert evidence == {
        "id": "gmail-1",
        "threadId": "thread-1",
        "messageId": message_id,
        "connectionId": "google_conn_1234",
        "accountEmail": "founder@example.com",
        "senderAddress": "founder@example.com",
        "recipient": "person@example.net",
        "payloadHash": payload_hash,
        "executedAt": PROVIDER_TIME.isoformat(),
        "labels": ["SENT"],
    }
    assert messages.get_calls == [{"userId": "me", "id": "gmail-1", "format": "raw"}]
    assert base64.urlsafe_b64decode(messages.send_calls[0]["body"]["raw"]) == raw


@pytest.mark.parametrize(
    "message_id,idempotency_key",
    [
        ("<different@invalid>", "<different@invalid>"),
        ("<po-aaaaaaaaaaaaaaaaaaaaaaaa@personal-operator.invalid>", "other"),
    ],
)
def test_send_refuses_unbound_idempotency_identity(message_id, idempotency_key):
    valid = "<po-bbbbbbbbbbbbbbbbbbbbbbbb@personal-operator.invalid>"
    raw = raw_message(valid)
    with pytest.raises(send_module.SendValidationError):
        adapter(Messages()).send_raw(
            raw=raw,
            message_id=message_id,
            idempotency_key=idempotency_key,
            payload_hash=models.canonical_args_hash(
                {"to": "person@example.net", "subject": "Hi", "body": "Hello"}
            ),
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda record: record.update(labelIds=["INBOX"]),
        lambda record: record.update(internalDate="not-time"),
        lambda record: record.update(raw=base64.urlsafe_b64encode(raw_message("<po-aaaaaaaaaaaaaaaaaaaaaaaa@personal-operator.invalid>", sender="attacker@example.com")).decode("ascii")),
        lambda record: record.update(raw=base64.urlsafe_b64encode(raw_message("<po-aaaaaaaaaaaaaaaaaaaaaaaa@personal-operator.invalid>", body="Changed")).decode("ascii")),
    ],
)
def test_send_never_confirms_without_sent_sender_payload_and_time_evidence(mutate):
    message_id = "<po-aaaaaaaaaaaaaaaaaaaaaaaa@personal-operator.invalid>"
    raw = raw_message(message_id)
    messages = Messages()
    messages.get_result = provider_record(raw)
    mutate(messages.get_result)
    with pytest.raises(send_module.ProviderEvidenceAmbiguous):
        adapter(messages).send_raw(
            raw=raw,
            message_id=message_id,
            idempotency_key=message_id,
            payload_hash=models.canonical_args_hash(
                {"to": "person@example.net", "subject": "Hi", "body": "Hello"}
            ),
        )


def test_history_lookup_uses_exact_rfc822_id_and_verifies_full_sent_message():
    messages = Messages()
    message_id = "<po-aaaaaaaaaaaaaaaaaaaaaaaa@personal-operator.invalid>"
    raw = raw_message(message_id)
    payload_hash = models.canonical_args_hash(
        {"to": "person@example.net", "subject": "Hi", "body": "Hello"}
    )
    messages.list_result = {"messages": [{"id": "gmail-1"}]}
    messages.get_result = provider_record(raw)

    found = adapter(messages).find_by_message_id(
        message_id=message_id,
        sender_address="founder@example.com",
        recipient="person@example.net",
        payload_hash=payload_hash,
    )

    assert found["labels"] == ["SENT"]
    assert found["executedAt"] == PROVIDER_TIME.isoformat()
    assert messages.list_calls[0]["q"] == (
        "rfc822msgid:po-aaaaaaaaaaaaaaaaaaaaaaaa@personal-operator.invalid"
    )


def test_history_none_is_proven_no_match_and_duplicates_are_ambiguous():
    message_id = "<po-aaaaaaaaaaaaaaaaaaaaaaaa@personal-operator.invalid>"
    kwargs = {
        "message_id": message_id,
        "sender_address": "founder@example.com",
        "recipient": "person@example.net",
        "payload_hash": "a" * 64,
    }
    messages = Messages()
    assert adapter(messages).find_by_message_id(**kwargs) is None
    messages.list_result = {"messages": [{"id": "one"}, {"id": "two"}]}
    with pytest.raises(send_module.ProviderEvidenceAmbiguous):
        adapter(messages).find_by_message_id(**kwargs)
