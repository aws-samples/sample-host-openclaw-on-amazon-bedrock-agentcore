import base64
import importlib.util
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pytest


GMAIL_DIR = Path(__file__).resolve().parent


def _load(name: str, filename: str | None = None):
    spec = importlib.util.spec_from_file_location(
        name, GMAIL_DIR / (filename or f"{name}.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


models = _load("gmail_models", "models.py")
scanner_module = _load("gmail_scanner", "scanner.py")


NOW = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)


def _encoded(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _message(
    message_id: str,
    *,
    sender: str,
    recipient: str,
    subject: str,
    age_days: int,
    body: str,
    extra_headers: dict[str, str] | None = None,
    label_ids: list[str] | None = None,
):
    headers = {
        "From": sender,
        "To": recipient,
        "Subject": subject,
        **(extra_headers or {}),
    }
    return {
        "id": message_id,
        "internalDate": str(int((NOW - timedelta(days=age_days)).timestamp() * 1000)),
        "labelIds": (
            list(label_ids)
            if label_ids is not None
            else (["SENT"] if "me@example.com" in sender.casefold() else ["INBOX"])
        ),
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": key, "value": value} for key, value in headers.items()],
            "body": {"data": _encoded(body)},
        },
    }


class FakeGmail:
    def __init__(self, threads):
        self.threads = {thread["id"]: thread for thread in threads}
        self.search_calls = []
        self.get_calls = []

    def list_threads(self, *, query, max_results):
        self.search_calls.append((query, max_results))
        return [{"id": key} for key in self.threads]

    def get_thread(self, *, thread_id, format):
        self.get_calls.append((thread_id, format))
        return self.threads[thread_id]


def _thread(thread_id: str, *messages):
    return {"id": thread_id, "messages": list(messages)}


def test_scans_only_three_to_thirty_day_unanswered_human_outbound_threads():
    gmail = FakeGmail(
        [
            _thread(
                "winner",
                _message(
                    "m1",
                    sender="Me <me@example.com>",
                    recipient="Ada <ada@example.net>",
                    subject="Project follow-up",
                    age_days=8,
                    body="Hi Ada, are you still interested?\n\nOn old text wrote:\nquoted",
                ),
            ),
            _thread(
                "replied",
                _message(
                    "m2",
                    sender="me@example.com",
                    recipient="bob@example.net",
                    subject="Hello",
                    age_days=8,
                    body="Checking in",
                ),
                _message(
                    "m3",
                    sender="bob@example.net",
                    recipient="me@example.com",
                    subject="Re: Hello",
                    age_days=7,
                    body="Already replied",
                ),
            ),
            _thread(
                "too-new",
                _message(
                    "m4",
                    sender="me@example.com",
                    recipient="c@example.net",
                    subject="New",
                    age_days=2,
                    body="Too soon",
                ),
            ),
            _thread(
                "too-old",
                _message(
                    "m5",
                    sender="me@example.com",
                    recipient="d@example.net",
                    subject="Old",
                    age_days=31,
                    body="Too late",
                ),
            ),
        ]
    )

    sources = scanner_module.GmailScanner(
        gmail,
        connected_address="ME@example.com",
        now=lambda: NOW,
    ).scan()

    assert len(sources) == 1
    source = sources[0]
    assert isinstance(source, models.SourceEvidence)
    assert source.source_id == "gmail:winner:m1"
    assert source.deep_link == "https://mail.google.com/mail/u/0/#inbox/winner"
    assert source.correspondent == "ada@example.net"
    assert source.subject == "Project follow-up"
    assert source.excerpt == "Hi Ada, are you still interested?"
    assert source.waiting_since == NOW - timedelta(days=8)
    assert gmail.search_calls == [
        ("in:sent older_than:3d newer_than:30d -in:chats", 50)
    ]
    assert all(format == "full" for _, format in gmail.get_calls)


def test_excludes_bulk_automated_and_no_reply_correspondents():
    candidates = [
        _thread(
            "bulk",
            _message(
                "b1",
                sender="me@example.com",
                recipient="team@example.net",
                subject="Newsletter",
                age_days=8,
                body="Bulk",
                extra_headers={"Precedence": "bulk"},
            ),
        ),
        _thread(
            "list",
            _message(
                "l1",
                sender="me@example.com",
                recipient="list@example.net",
                subject="List",
                age_days=9,
                body="List",
                extra_headers={"List-Id": "community.example.net"},
            ),
        ),
        _thread(
            "auto",
            _message(
                "a1",
                sender="me@example.com",
                recipient="robot@example.net",
                subject="Auto",
                age_days=10,
                body="Auto",
                extra_headers={"Auto-Submitted": "auto-generated"},
            ),
        ),
        _thread(
            "noreply",
            _message(
                "n1",
                sender="me@example.com",
                recipient="no-reply@example.net",
                subject="No reply",
                age_days=11,
                body="No reply",
            ),
        ),
    ]

    assert scanner_module.GmailScanner(
        FakeGmail(candidates),
        connected_address="me@example.com",
        now=lambda: NOW,
    ).scan() == []


def test_from_header_alone_cannot_spoof_an_outbound_message_without_sent_label():
    gmail = FakeGmail(
        [
            _thread(
                "spoofed",
                _message(
                    "m1",
                    sender="me@example.com",
                    recipient="person@example.net",
                    subject="Not actually sent",
                    age_days=8,
                    body="Untrusted provider record",
                    label_ids=["INBOX"],
                ),
            )
        ]
    )

    assert scanner_module.GmailScanner(
        gmail,
        connected_address="me@example.com",
        now=lambda: NOW,
    ).scan() == []


@pytest.mark.parametrize("payload", [
    {
        "mimeType": "text/plain",
        "body": {"data": "A" * (256 * 1024 + 1)},
    },
    {
        "mimeType": "multipart/mixed",
        "body": {},
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _encoded("x")}}
            for _ in range(65)
        ],
    },
])
def test_oversized_or_excessive_mime_parts_fail_closed(payload):
    message = _message(
        "m1",
        sender="me@example.com",
        recipient="person@example.net",
        subject="Bounded",
        age_days=8,
        body="placeholder",
    )
    payload["headers"] = message["payload"]["headers"]
    message["payload"] = payload
    gmail = FakeGmail([_thread("bounded", message)])

    assert scanner_module.GmailScanner(
        gmail,
        connected_address="me@example.com",
        now=lambda: NOW,
    ).scan() == []


def test_excessive_mime_nesting_fails_closed():
    leaf = {"mimeType": "text/plain", "body": {"data": _encoded("hello")}}
    payload = leaf
    for _ in range(9):
        payload = {
            "mimeType": "multipart/alternative",
            "body": {},
            "parts": [payload],
        }
    message = _message(
        "m1",
        sender="me@example.com",
        recipient="person@example.net",
        subject="Bounded",
        age_days=8,
        body="placeholder",
    )
    payload["headers"] = message["payload"]["headers"]
    message["payload"] = payload

    assert scanner_module.GmailScanner(
        FakeGmail([_thread("nested", message)]),
        connected_address="me@example.com",
        now=lambda: NOW,
    ).scan() == []


def test_caps_provider_fetch_at_fifty_and_returns_only_derived_bounded_data():
    long_secret = "PRIVATE-" + "x" * 2_000
    gmail = FakeGmail(
        [
            _thread(
                f"t-{index}",
                _message(
                    f"m-{index}",
                    sender="me@example.com",
                    recipient=f"person-{index}@example.net",
                    subject="S" * 500,
                    age_days=4,
                    body=long_secret,
                ),
            )
            for index in range(60)
        ]
    )

    results = scanner_module.GmailScanner(
        gmail,
        connected_address="me@example.com",
        now=lambda: NOW,
    ).scan()

    assert len(results) == 50
    assert len(gmail.get_calls) == 50
    assert all(len(item.subject) <= 200 for item in results)
    assert all(len(item.excerpt) <= 280 for item in results)
    assert all(not hasattr(item, "raw_body") for item in results)


def test_malformed_provider_records_fail_closed_without_leaking_body(caplog):
    secret = "TOP-SECRET-CONTENT"
    gmail = FakeGmail(
        [
            {"id": "missing-messages"},
            _thread(
                "bad-date",
                {
                    **_message(
                        "bad",
                        sender="me@example.com",
                        recipient="ada@example.net",
                        subject="Bad",
                        age_days=5,
                        body=secret,
                    ),
                    "internalDate": "not-a-date",
                },
            ),
        ]
    )

    results = scanner_module.GmailScanner(
        gmail,
        connected_address="me@example.com",
        now=lambda: NOW,
    ).scan()

    assert results == []
    assert secret not in caplog.text


def test_provider_listing_failure_is_redacted_and_fails_closed(caplog):
    error_type = type("PRIVATE_LIST_EXCEPTION_CLASS_CANARY", (RuntimeError,), {})
    private_message = (
        "PRIVATE_LIST_EXCEPTION_MESSAGE_CANARY ACCESS-TOKEN raw email body"
    )

    class FailingGmail:
        def list_threads(self, *, query, max_results):
            raise error_type(private_message)

    caplog.clear()
    assert scanner_module.GmailScanner(
        FailingGmail(),
        connected_address="me@example.com",
        now=lambda: NOW,
    ).scan() == []
    expected = json.dumps(
        {
            "component": "gmail",
            "event": "thread_listing_failed",
            "level": "WARNING",
            "schema": "personal-operator.log.v1",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    assert all(
        canary not in caplog.text
        for canary in (
            "PRIVATE_LIST_EXCEPTION_CLASS_CANARY",
            "PRIVATE_LIST_EXCEPTION_MESSAGE_CANARY",
            "ACCESS-TOKEN",
            "raw email body",
        )
    )
    assert [record.getMessage() for record in caplog.records] == [expected]
    assert all(record.args == () for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records)


def test_provider_thread_failure_logs_no_source_or_exception_content(caplog):
    error_type = type("PRIVATE_THREAD_EXCEPTION_CLASS_CANARY", (RuntimeError,), {})
    source_id = "PRIVATE_SOURCE_THREAD_ID_CANARY"
    private_message = (
        "PRIVATE_THREAD_EXCEPTION_MESSAGE_CANARY person@example.net raw body"
    )

    class FailingGmail:
        def list_threads(self, *, query, max_results):
            return [{"id": source_id}]

        def get_thread(self, *, thread_id, format):
            assert thread_id == source_id
            assert format == "full"
            raise error_type(private_message)

    caplog.clear()
    assert scanner_module.GmailScanner(
        FailingGmail(),
        connected_address="me@example.com",
        now=lambda: NOW,
    ).scan() == []
    expected = json.dumps(
        {
            "component": "gmail",
            "event": "thread_processing_failed",
            "level": "WARNING",
            "schema": "personal-operator.log.v1",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    assert all(
        canary not in caplog.text
        for canary in (
            "PRIVATE_THREAD_EXCEPTION_CLASS_CANARY",
            "PRIVATE_THREAD_EXCEPTION_MESSAGE_CANARY",
            "PRIVATE_SOURCE_THREAD_ID_CANARY",
            "person@example.net",
            "raw body",
        )
    )
    assert [record.getMessage() for record in caplog.records] == [expected]
    assert all(record.args == () for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records)


def test_google_adapter_disables_library_level_retries_for_every_provider_read():
    execute_calls = []

    class Request:
        def __init__(self, payload):
            self.payload = payload

        def execute(self, **kwargs):
            execute_calls.append(kwargs)
            return self.payload

    class Threads:
        def list(self, **kwargs):
            return Request({"threads": [{"id": "thread-1"}]})

        def get(self, **kwargs):
            return Request({"id": "thread-1", "messages": []})

    class Users:
        def threads(self):
            return Threads()

    class Service:
        def users(self):
            return Users()

    client = scanner_module.GoogleGmailApiClient(Service())

    assert client.list_threads(query="in:sent", max_results=1) == [{"id": "thread-1"}]
    assert client.get_thread(thread_id="thread-1", format="full") == {
        "id": "thread-1",
        "messages": [],
    }
    assert execute_calls == [{"num_retries": 0}, {"num_retries": 0}]
