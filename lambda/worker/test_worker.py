"""Offline tests for the ordered trusted Telegram worker."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path


WORKER_DIR = Path(__file__).resolve().parent
ROUTER_DIR = WORKER_DIR.parent / "router"
sys.path.insert(0, str(ROUTER_DIR))

spec = importlib.util.spec_from_file_location("personal_operator_worker", WORKER_DIR / "index.py")
worker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = worker
spec.loader.exec_module(worker)

from message_queue import QueueEnvelope  # noqa: E402


class FakeRuntimeDriver:
    def __init__(self, response=None):
        self.response = response or {"response": "**Done** <safely>"}
        self.calls = []

    def invoke(self, user_id, request, trace_id):
        self.calls.append((user_id, request, trace_id))
        return self.response


class FakeCommandHandler:
    def __init__(self):
        self.calls = []

    def handle(self, **kwargs):
        self.calls.append(kwargs)
        return f"handled {kwargs['command'].name}"


class FakeDelivery:
    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.calls = []

    def send_message(self, **kwargs):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("synthetic pre-send failure")
        self.calls.append(kwargs)
        return {"providerMessageId": f"tg-{len(self.calls)}"}


@dataclass
class Record:
    result: str | None = None
    delivered: bool = False


class FakeLedger:
    def __init__(self):
        self.records = {}

    def get_result(self, key):
        return self.records.get(key, Record()).result

    def put_result_if_absent(self, key, result):
        record = self.records.setdefault(key, Record())
        if record.result is None:
            record.result = result
        return record.result

    def is_delivered(self, key):
        return self.records.get(key, Record()).delivered

    def mark_delivered(self, key, receipt):
        assert receipt["providerMessageId"].startswith("tg-")
        self.records.setdefault(key, Record()).delivered = True


def dependencies(*, runtime=None, commands=None, delivery=None, ledger=None):
    return worker.WorkerDependencies(
        runtime_driver=runtime or FakeRuntimeDriver(),
        command_handler=commands or FakeCommandHandler(),
        telegram_delivery=delivery or FakeDelivery(),
        ledger=ledger or FakeLedger(),
    )


def make_envelope(*, update_id="100", trace_id="trace_100", kind="message", payload=None):
    if payload is None:
        payload = {
            "chatId": "9001",
            "actorId": "telegram:42",
            "message": "hello",
        }
    return QueueEnvelope(
        user_id="user_a1",
        channel="telegram",
        update_id=update_id,
        trace_id=trace_id,
        kind=kind,
        payload=payload,
    )


def sqs_record(item, message_id=None, group_id=None, dedupe_id=None):
    return {
        "messageId": message_id or f"sqs-{item.update_id}",
        "receiptHandle": "synthetic",
        "body": item.to_json(),
        "attributes": {
            "MessageGroupId": group_id or item.message_group_id,
            "MessageDeduplicationId": dedupe_id or item.message_deduplication_id,
        },
        "messageAttributes": {},
        "eventSource": "aws:sqs",
    }


def test_free_form_input_invokes_runtime_with_minimal_secret_free_request():
    runtime = FakeRuntimeDriver()
    delivery = FakeDelivery()
    deps = dependencies(runtime=runtime, delivery=delivery)
    item = make_envelope(payload={
        "chatId": "9001",
        "actorId": "telegram:42",
        "message": {"text": "hello", "images": [{"s3Key": "user_a1/_uploads/x.png", "contentType": "image/png"}]},
    })

    worker.process_envelope(item, deps)

    assert len(runtime.calls) == 1
    user_id, request, trace_id = runtime.calls[0]
    assert user_id == "user_a1"
    assert trace_id == "trace_100"
    assert set(request) == {"channel", "actorId", "message", "invocationId"}
    assert request["invocationId"] == item.message_deduplication_id
    serialized = json.dumps(request).lower()
    assert "token" not in serialized
    assert "credential" not in serialized
    assert "chatid" not in serialized
    assert delivery.calls[0]["chat_id"] == "9001"
    assert delivery.calls[0]["html"] == "<b>Done</b> &lt;safely&gt;"


def test_every_product_command_routes_locally_and_never_reaches_runtime():
    runtime = FakeRuntimeDriver()
    commands = FakeCommandHandler()
    delivery = FakeDelivery()
    deps = dependencies(runtime=runtime, commands=commands, delivery=delivery)

    for index, name in enumerate(("/start", "/connect", "/scan", "/tasks", "/workspace", "/status", "/delete")):
        item = make_envelope(
            update_id=str(index + 1),
            kind="command",
            payload={"chatId": "9001", "actorId": "telegram:42", "command": name},
        )
        worker.process_envelope(item, deps)

    assert runtime.calls == []
    assert [call["command"].name for call in commands.calls] == [
        "/start", "/connect", "/scan", "/tasks", "/workspace", "/status", "/delete"
    ]
    assert len(delivery.calls) == 7


def test_successful_duplicate_replay_does_not_repeat_runtime_or_delivery():
    runtime = FakeRuntimeDriver()
    delivery = FakeDelivery()
    ledger = FakeLedger()
    deps = dependencies(runtime=runtime, delivery=delivery, ledger=ledger)
    item = make_envelope()

    worker.process_envelope(item, deps)
    worker.process_envelope(item, deps)

    assert len(runtime.calls) == 1
    assert len(delivery.calls) == 1


def test_delivery_retry_uses_cached_runtime_result_instead_of_reinvoking():
    runtime = FakeRuntimeDriver()
    delivery = FakeDelivery(fail_times=1)
    ledger = FakeLedger()
    deps = dependencies(runtime=runtime, delivery=delivery, ledger=ledger)
    item = make_envelope()

    try:
        worker.process_envelope(item, deps)
    except RuntimeError as error:
        assert "synthetic" in str(error)
    else:
        raise AssertionError("first delivery must fail")

    worker.process_envelope(item, deps)

    assert len(runtime.calls) == 1
    assert len(delivery.calls) == 1


def test_fifo_batch_returns_partial_failures_and_stops_after_first_failure():
    class SelectiveRuntime(FakeRuntimeDriver):
        def invoke(self, user_id, request, trace_id):
            if request["message"] == "fail":
                raise RuntimeError("synthetic runtime failure")
            return super().invoke(user_id, request, trace_id)

    runtime = SelectiveRuntime()
    deps = dependencies(runtime=runtime)
    first = make_envelope(update_id="1", payload={"chatId": "1", "actorId": "telegram:42", "message": "ok"})
    second = make_envelope(update_id="2", payload={"chatId": "1", "actorId": "telegram:42", "message": "fail"})
    third = make_envelope(update_id="3", payload={"chatId": "1", "actorId": "telegram:42", "message": "must wait"})

    result = worker.process_sqs_event(
        {"Records": [sqs_record(first), sqs_record(second), sqs_record(third)]},
        deps,
    )

    assert result == {
        "batchItemFailures": [
            {"itemIdentifier": "sqs-2"},
            {"itemIdentifier": "sqs-3"},
        ]
    }
    assert [call[1]["message"] for call in runtime.calls] == ["ok"]


def test_queue_record_group_and_deduplication_attributes_are_verified():
    deps = dependencies()
    item = make_envelope()

    wrong_group = worker.process_sqs_event(
        {"Records": [sqs_record(item, group_id="user_other")]},
        deps,
    )
    wrong_dedupe = worker.process_sqs_event(
        {"Records": [sqs_record(item, dedupe_id="0" * 64)]},
        deps,
    )

    assert wrong_group == {"batchItemFailures": [{"itemIdentifier": "sqs-100"}]}
    assert wrong_dedupe == {"batchItemFailures": [{"itemIdentifier": "sqs-100"}]}


def test_malformed_record_is_failed_without_leaking_body_content():
    deps = dependencies()
    record = {
        "messageId": "malformed-1",
        "body": "{not-json",
        "attributes": {"MessageGroupId": "user_a1", "MessageDeduplicationId": "x"},
        "eventSource": "aws:sqs",
    }

    result = worker.process_sqs_event({"Records": [record]}, deps)

    assert result == {"batchItemFailures": [{"itemIdentifier": "malformed-1"}]}


def test_runtime_cannot_claim_it_streamed_directly_to_telegram():
    deps = dependencies(runtime=FakeRuntimeDriver({"response": "x", "streamed": True}))

    try:
        worker.process_envelope(make_envelope(), deps)
    except worker.WorkerContractError as error:
        assert "stream" in str(error).lower()
    else:
        raise AssertionError("untrusted runtime streaming claim must be rejected")

