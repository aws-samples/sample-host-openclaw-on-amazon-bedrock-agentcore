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

from event_identity import derive_event_trace  # noqa: E402

QueueEnvelope = worker.QueueEnvelope


class FakeRuntimeDriver:
    def __init__(self, response=None):
        self.response = response or {"response": "**Done** <safely>"}
        self.calls = []

    def invoke(self, user_id, request, trace_id, *, scheduled_read_only=False):
        # Mirror the real RuntimeDriver contract: reject any request carrying
        # caller-controlled authority fields, so a fake can never mask the
        # divergence that the scheduled read-only path must respect.
        allowed = {"message", "actorId", "channel"}
        if "message" not in request or set(request) - allowed:
            raise ValueError("runtime request contains caller-controlled authority")
        self.calls.append((user_id, request, trace_id, scheduled_read_only))
        return self.response


class FakeCommandHandler:
    def __init__(self, response=None):
        self.calls = []
        self.response = response

    def handle(self, **kwargs):
        self.calls.append(kwargs)
        return self.response or f"handled {kwargs['command'].name}"

    def handle_callback(self, **kwargs):
        self.calls.append(kwargs)
        return self.response or "callback handled"


class FakeDelivery:
    def __init__(self, fail_times=0, *, acknowledgement_error=None):
        self.fail_times = fail_times
        self.calls = []
        self.acknowledgements = []
        self.acknowledgement_error = acknowledgement_error

    def acknowledge_callback(self, *, callback_query_id):
        self.acknowledgements.append(callback_query_id)
        if self.acknowledgement_error is not None:
            raise self.acknowledgement_error

    def send_message(self, **kwargs):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("synthetic pre-send failure")
        self.calls.append(kwargs)
        return {"providerMessageId": f"tg-{len(self.calls)}"}


@dataclass
class Record:
    request_sha256: str
    state: str = "PROCESSING"
    result: str | None = None


@dataclass
class Claim:
    key: str
    state: str
    result: str | None = None


class FakeLedger:
    def __init__(self):
        self.records = {}
        self.fail_complete = False
        self.fail_confirm = False

    def claim_processing(self, envelope, *, owner):
        del owner
        key = envelope.message_deduplication_id
        record = self.records.get(key)
        if record is None:
            record = Record(request_sha256=envelope.request_sha256)
            self.records[key] = record
            return Claim(key, "CLAIMED")
        if record.request_sha256 != envelope.request_sha256:
            raise worker.WorkerContractError("same event identity has different content")
        return Claim(key, record.state, record.result)

    def complete_result(self, claim, result):
        if self.fail_complete:
            raise RuntimeError("synthetic result persistence ambiguity")
        record = self.records[claim.key]
        assert record.state == "PROCESSING"
        record.state = "RESULT_READY"
        record.result = result
        return Claim(claim.key, "RESULT_READY", result)

    def mark_processing_uncertain(self, claim, *, error_type):
        del error_type
        record = self.records[claim.key]
        if record.state == "PROCESSING":
            record.state = "PROCESSING_UNCERTAIN"

    def begin_delivery(self, claim, *, owner):
        del owner
        record = self.records[claim.key]
        if record.state == "RESULT_READY":
            record.state = "DELIVERY_IN_FLIGHT"
            return Claim(claim.key, "DELIVERY_CLAIMED", record.result)
        return Claim(claim.key, record.state, record.result)

    def confirm_delivery(self, claim, receipt):
        assert receipt["providerMessageId"].startswith("tg-")
        if self.fail_confirm:
            raise RuntimeError("synthetic delivery receipt persistence ambiguity")
        self.records[claim.key].state = "DELIVERED"

    def mark_delivery_uncertain(self, claim, *, error_type):
        del error_type
        record = self.records[claim.key]
        if record.state == "DELIVERY_IN_FLIGHT":
            record.state = "DELIVERY_UNCERTAIN"


class ActiveDeletionFence:
    def __init__(self, *, deleted=False, error=None):
        self.deleted = deleted
        self.error = error
        self.calls = []

    def is_account_deleted(self, user_id):
        self.calls.append(user_id)
        if self.error:
            raise self.error
        return self.deleted


class ActiveControlDeletionFence:
    def __init__(self, *, blocked=False, error=None):
        self.blocked = blocked
        self.error = error
        self.calls = []

    def deletion_blocked(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.blocked


def dependencies(
    *,
    runtime=None,
    commands=None,
    delivery=None,
    ledger=None,
    deletion_fence=None,
    control_deletion_fence=None,
):
    return worker.WorkerDependencies(
        runtime_driver=runtime or FakeRuntimeDriver(),
        command_handler=commands or FakeCommandHandler(),
        telegram_delivery=delivery or FakeDelivery(),
        ledger=ledger or FakeLedger(),
        deletion_fence=deletion_fence or ActiveDeletionFence(),
        control_deletion_fence=(
            control_deletion_fence or ActiveControlDeletionFence()
        ),
    )


def make_envelope(
    *,
    update_id="100",
    trace_id=None,
    kind="message",
    payload=None,
):
    if payload is None:
        payload = {
            "chatId": "42",
            "actorId": "telegram:42",
            "message": "hello",
        }
    return QueueEnvelope(
        user_id="user_a1",
        channel="telegram",
        update_id=update_id,
        trace_id=trace_id or derive_event_trace("telegram", "user_a1", update_id),
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
        "chatId": "42",
        "actorId": "telegram:42",
        "message": {"text": "hello", "images": [{"s3Key": "user_a1/_uploads/x.png", "contentType": "image/png"}]},
    })

    worker.process_envelope(item, deps)

    assert len(runtime.calls) == 1
    user_id, request, trace_id, scheduled_read_only = runtime.calls[0]
    assert user_id == "user_a1"
    assert scheduled_read_only is False  # an interactive turn, not scheduled
    assert trace_id == derive_event_trace("telegram", "user_a1", "100")
    assert set(request) == {"channel", "actorId", "message"}
    serialized = json.dumps(request).lower()
    assert "token" not in serialized
    assert "credential" not in serialized
    assert "chatid" not in serialized
    assert delivery.calls[0]["chat_id"] == "42"
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
            payload={"chatId": "42", "actorId": "telegram:42", "command": name},
        )
        worker.process_envelope(item, deps)

    assert runtime.calls == []
    assert [call["command"].name for call in commands.calls] == [
        "/start", "/connect", "/scan", "/tasks", "/workspace", "/status", "/delete"
    ]
    assert len(delivery.calls) == 7


def test_scan_card_result_reaches_one_validated_inline_keyboard_delivery():
    result = worker.TelegramCommandResult(
        text="**Reply to Ada**\nWaiting seven days.",
        inline_keyboard=((
            ("Edit", "poc1:edit:AAAAAAAAAAAAAAAAAAAAAA"),
            ("Prepare", "poc1:prepare:BBBBBBBBBBBBBBBBBBBBBB"),
            ("Skip", "poc1:skip:CCCCCCCCCCCCCCCCCCCCCC"),
            ("Why", "poc1:why:DDDDDDDDDDDDDDDDDDDDDD"),
        ),),
    )
    commands = FakeCommandHandler(response=result)
    delivery = FakeDelivery()

    worker.process_envelope(
        make_envelope(
            kind="command",
            payload={"chatId": "42", "actorId": "telegram:42", "command": "/scan"},
        ),
        dependencies(commands=commands, delivery=delivery),
    )

    assert commands.calls[0]["chat_id"] == "42"
    assert commands.calls[0]["actor_id"] == "telegram:42"
    assert delivery.calls[0]["html"].startswith("<b>Reply to Ada</b>")
    assert delivery.calls[0]["reply_markup"] == result.reply_markup()


def test_callback_routes_to_control_and_new_update_replay_cannot_repeat_effect():
    commands = FakeCommandHandler()
    ledger = FakeLedger()
    delivery = FakeDelivery()
    item = make_envelope(
        update_id="201",
        kind="callback",
        payload={
            "chatId": "42",
            "actorId": "telegram:42",
            "callbackData": "poc1:prepare:BBBBBBBBBBBBBBBBBBBBBB",
            "callbackQueryId": "telegram_callback_query_201",
        },
    )
    deps = dependencies(commands=commands, ledger=ledger, delivery=delivery)

    worker.process_envelope(item, deps)
    worker.process_envelope(item, deps)

    assert len(commands.calls) == 1
    assert commands.calls[0]["callback_data"] == (
        "poc1:prepare:BBBBBBBBBBBBBBBBBBBBBB"
    )
    assert commands.calls[0]["chat_id"] == "42"
    assert len(delivery.calls) == 1
    assert delivery.acknowledgements == [
        "telegram_callback_query_201",
        "telegram_callback_query_201",
    ]


def test_callback_acknowledgement_failure_never_blocks_or_leaks(caplog):
    provider_error_sentinel = "Telegram acknowledgement source-private error"
    delivery = FakeDelivery(
        acknowledgement_error=TimeoutError(provider_error_sentinel)
    )
    commands = FakeCommandHandler()
    item = make_envelope(
        update_id="202",
        kind="callback",
        payload={
            "chatId": "42",
            "actorId": "telegram:42",
            "callbackData": "poc1:why:BBBBBBBBBBBBBBBBBBBBBB",
            "callbackQueryId": "telegram_callback_query_202",
        },
    )

    worker.process_envelope(
        item,
        dependencies(commands=commands, delivery=delivery),
    )

    assert delivery.acknowledgements == ["telegram_callback_query_202"]
    assert len(commands.calls) == 1
    assert len(delivery.calls) == 1
    assert provider_error_sentinel not in caplog.text
    assert [json.loads(record.getMessage()) for record in caplog.records] == [
        {
            "component": "worker",
            "event": "callback_acknowledgement_failed",
            "level": "WARNING",
            "schema": "personal-operator.log.v1",
        }
    ]
    assert all(record.args == () for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records)


def test_durable_account_tombstone_is_checked_before_ledger_claim():
    fence = ActiveDeletionFence(deleted=True)
    ledger = FakeLedger()
    delivery = FakeDelivery()

    worker.process_envelope(
        make_envelope(),
        dependencies(deletion_fence=fence, ledger=ledger, delivery=delivery),
    )

    assert fence.calls == ["user_a1"]
    assert ledger.records == {}
    assert delivery.calls == []


def test_first_control_deletion_intent_blocks_before_runtime_or_ledger_tombstone():
    fence = ActiveControlDeletionFence(blocked=True)
    runtime_fence = ActiveDeletionFence()
    ledger = FakeLedger()
    runtime = FakeRuntimeDriver()

    worker.process_envelope(
        make_envelope(),
        dependencies(
            control_deletion_fence=fence,
            deletion_fence=runtime_fence,
            ledger=ledger,
            runtime=runtime,
        ),
    )

    assert fence.calls[0] == {
        "user_id": "user_a1",
        "channel": "telegram",
        "trace_id": derive_event_trace("telegram", "user_a1", "100"),
        "idempotency_key": derive_event_trace("telegram", "user_a1", "100"),
    }
    assert runtime_fence.calls == []
    assert ledger.records == {}
    assert runtime.calls == []


def test_deletion_is_rechecked_after_delivery_claim_immediately_before_telegram():
    class FenceAppearsBeforeDelivery(ActiveControlDeletionFence):
        def deletion_blocked(self, **kwargs):
            self.calls.append(kwargs)
            return len(self.calls) == 2

    fence = FenceAppearsBeforeDelivery()
    runtime = FakeRuntimeDriver()
    delivery = FakeDelivery()
    ledger = FakeLedger()
    item = make_envelope()

    worker.process_envelope(
        item,
        dependencies(
            control_deletion_fence=fence,
            runtime=runtime,
            delivery=delivery,
            ledger=ledger,
        ),
    )

    assert len(fence.calls) == 2
    assert len(runtime.calls) == 1
    assert delivery.calls == []
    assert ledger.records[item.message_deduplication_id].state == (
        "DELIVERY_UNCERTAIN"
    )


def test_control_deletion_fence_ambiguity_retries_before_ledger_claim():
    fence = ActiveControlDeletionFence(error=TimeoutError("unknown"))
    ledger = FakeLedger()
    item = make_envelope()

    result = worker.process_sqs_event(
        {"Records": [sqs_record(item)]},
        dependencies(control_deletion_fence=fence, ledger=ledger),
    )

    assert result == {"batchItemFailures": [{"itemIdentifier": "sqs-100"}]}
    assert ledger.records == {}


def test_deletion_fence_lookup_failure_retries_without_creating_ledger_state():
    fence = ActiveDeletionFence(error=TimeoutError("unknown"))
    ledger = FakeLedger()
    item = make_envelope()

    result = worker.process_sqs_event(
        {"Records": [sqs_record(item)]},
        dependencies(deletion_fence=fence, ledger=ledger),
    )

    assert result == {"batchItemFailures": [{"itemIdentifier": "sqs-100"}]}
    assert ledger.records == {}


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


def test_ambiguous_delivery_is_never_sent_again_on_retry():
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

    try:
        worker.process_envelope(item, deps)
    except worker.WorkerContractError as error:
        assert "uncertain" in str(error).lower()
    else:
        raise AssertionError("ambiguous delivery replay must remain failed")

    assert len(runtime.calls) == 1
    assert len(delivery.calls) == 0


def test_runtime_success_then_result_persistence_failure_never_reexecutes():
    runtime = FakeRuntimeDriver()
    ledger = FakeLedger()
    ledger.fail_complete = True
    deps = dependencies(runtime=runtime, ledger=ledger)
    item = make_envelope()

    for _ in range(2):
        try:
            worker.process_envelope(item, deps)
        except RuntimeError:
            pass

    assert len(runtime.calls) == 1
    assert ledger.records[item.message_deduplication_id].state == "PROCESSING_UNCERTAIN"


def test_send_success_then_receipt_persistence_failure_never_resends():
    runtime = FakeRuntimeDriver()
    delivery = FakeDelivery()
    ledger = FakeLedger()
    ledger.fail_confirm = True
    deps = dependencies(runtime=runtime, delivery=delivery, ledger=ledger)
    item = make_envelope()

    for _ in range(2):
        try:
            worker.process_envelope(item, deps)
        except RuntimeError:
            pass

    assert len(runtime.calls) == 1
    assert len(delivery.calls) == 1
    assert ledger.records[item.message_deduplication_id].state == "DELIVERY_UNCERTAIN"


def test_fifo_batch_returns_partial_failures_and_stops_after_first_failure():
    class SelectiveRuntime(FakeRuntimeDriver):
        def invoke(self, user_id, request, trace_id):
            if request["message"] == "fail":
                raise RuntimeError("synthetic runtime failure")
            return super().invoke(user_id, request, trace_id)

    runtime = SelectiveRuntime()
    deps = dependencies(runtime=runtime)
    first = make_envelope(update_id="1", payload={"chatId": "42", "actorId": "telegram:42", "message": "ok"})
    second = make_envelope(update_id="2", payload={"chatId": "42", "actorId": "telegram:42", "message": "fail"})
    third = make_envelope(update_id="3", payload={"chatId": "42", "actorId": "telegram:42", "message": "must wait"})

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


def test_malformed_record_is_failed_without_leaking_body_or_trace_identifier(caplog):
    deps = dependencies()
    trace_sentinel = "source-private-trace-sentinel"
    record = {
        "messageId": trace_sentinel,
        "body": "{not-json",
        "attributes": {"MessageGroupId": "user_a1", "MessageDeduplicationId": "x"},
        "eventSource": "aws:sqs",
    }

    result = worker.process_sqs_event({"Records": [record]}, deps)

    assert result == {"batchItemFailures": [{"itemIdentifier": trace_sentinel}]}
    assert trace_sentinel not in caplog.text
    assert [json.loads(record.getMessage()) for record in caplog.records] == [
        {
            "component": "worker",
            "event": "fifo_record_failed",
            "level": "WARNING",
            "schema": "personal-operator.log.v1",
        }
    ]
    assert all(record.args == () for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records)


def test_runtime_cannot_claim_it_streamed_directly_to_telegram():
    deps = dependencies(runtime=FakeRuntimeDriver({"response": "x", "streamed": True}))

    try:
        worker.process_envelope(make_envelope(), deps)
    except worker.WorkerContractError as error:
        assert "stream" in str(error).lower()
    else:
        raise AssertionError("untrusted runtime streaming claim must be rejected")


# --- Task 7: scheduled read-only occurrences ---------------------------------

import hashlib as _hashlib

sys.path.insert(0, str(WORKER_DIR.parent / "capabilities"))
from contracts import derive_occurrence_id as _derive_occurrence_id  # noqa: E402

OCCURRENCE_BODY_SCHEMA = "personal-operator.schedule-occurrence-body.v1"
SCHEDULE_ID = "sch_" + "a" * 64


def occurrence_body(*, task_type="REMINDER", generation=1, occurrence_time=1_800_000_600, content=None):
    occurrence_id = _derive_occurrence_id(SCHEDULE_ID, generation, occurrence_time)
    content_field = "message" if task_type == "REMINDER" else "prompt"
    if content is None:
        content = "water the plants" if task_type == "REMINDER" else "read my inbox"
    body = {
        "schema": OCCURRENCE_BODY_SCHEMA,
        "occurrenceId": occurrence_id,
        "scheduleId": SCHEDULE_ID,
        "userId": "user_a1",
        "generation": generation,
        "occurrenceTime": occurrence_time,
        "taskType": task_type,
        "chatId": "42",
        "actorId": "telegram:42",
        content_field: content,
        "scheduled": True,
        "externalEffects": False,
    }
    return json.dumps(body, ensure_ascii=True, separators=(",", ":"), sort_keys=True, allow_nan=False), occurrence_id


def occurrence_sqs_record(body, occurrence_id, message_id="sqs-occ-1"):
    return {
        "messageId": message_id,
        "receiptHandle": "synthetic",
        "body": body,
        "attributes": {
            "MessageGroupId": "user_a1",
            "MessageDeduplicationId": occurrence_id,
        },
        "messageAttributes": {},
        "eventSource": "aws:sqs",
    }


def test_scheduled_agent_turn_uses_the_real_runtime_contract_and_marks_read_only():
    # C1/C2 regression: a READ_ONLY_AGENT_TURN must build a request the REAL
    # RuntimeDriver._validated_request accepts (only {message, actorId, channel},
    # requires 'message') and carry read-only via a trusted invoke parameter that
    # stamps the payload — never via caller-controlled request fields.
    import importlib.util
    import pathlib
    import sys as _sys

    root = pathlib.Path(worker.__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "router_runtime_driver_real", root / "router" / "runtime_driver.py"
    )
    rd = importlib.util.module_from_spec(spec)
    _sys.modules["router_runtime_driver_real"] = rd
    spec.loader.exec_module(rd)

    class ContractEnforcingDriver:
        def __init__(self):
            self.payloads = []

        def invoke(self, user_id, request, trace_id, *, scheduled_read_only=False):
            # Raises ValueError if the worker sends a non-conforming request.
            validated = rd.RuntimeDriver._validated_request(user_id, request)
            payload = {"action": "chat", "message": validated["message"]}
            if scheduled_read_only:
                payload["externalEffects"] = False
            self.payloads.append(payload)
            return {"response": "ok"}

    driver = ContractEnforcingDriver()
    deps = dependencies(runtime=driver)
    body, occurrence_id = occurrence_body(task_type="READ_ONLY_AGENT_TURN")
    occurrence = worker.ScheduledOccurrence(json.loads(body))

    # Must not raise: the request conforms to the real runtime contract.
    worker._occurrence_result(occurrence, deps)

    assert driver.payloads, "the scheduled turn never reached the runtime"
    assert driver.payloads[0]["externalEffects"] is False


def test_worker_processes_scheduled_occurrence_at_most_once_reusing_ledger_state_machine():
    ledger = FakeLedger()
    delivery = FakeDelivery()
    deps = dependencies(ledger=ledger, delivery=delivery)
    body, occurrence_id = occurrence_body(task_type="REMINDER")
    record = occurrence_sqs_record(body, occurrence_id)

    first = worker.process_sqs_event({"Records": [record]}, deps)
    second = worker.process_sqs_event({"Records": [record]}, deps)

    assert first == {"batchItemFailures": []}
    assert second == {"batchItemFailures": []}
    # At-most-once delivery despite a duplicate fire replay.
    assert len(delivery.calls) == 1
    # The occurrence reused the existing ledger state machine keyed by dedupe id.
    assert list(ledger.records) == [occurrence_id]
    assert ledger.records[occurrence_id].state == "DELIVERED"


def test_scheduled_reminder_delivers_once_and_read_only_turn_invokes_runtime_with_external_effects_false():
    # REMINDER: delivered as a notification, runtime is never invoked.
    runtime = FakeRuntimeDriver()
    delivery = FakeDelivery()
    deps = dependencies(runtime=runtime, delivery=delivery)
    body, occurrence_id = occurrence_body(task_type="REMINDER", content="**call mum**")
    worker.process_sqs_event({"Records": [occurrence_sqs_record(body, occurrence_id)]}, deps)

    assert runtime.calls == []
    assert len(delivery.calls) == 1
    assert delivery.calls[0]["chat_id"] == "42"
    assert delivery.calls[0]["html"] == "<b>call mum</b>"

    # READ_ONLY_AGENT_TURN: the runtime is invoked with the read-only markers.
    runtime2 = FakeRuntimeDriver({"response": "here is your digest"})
    delivery2 = FakeDelivery()
    deps2 = dependencies(runtime=runtime2, delivery=delivery2)
    body2, occ2 = occurrence_body(task_type="READ_ONLY_AGENT_TURN", content="read inbox")
    worker.process_sqs_event({"Records": [occurrence_sqs_record(body2, occ2, message_id="sqs-occ-2")]}, deps2)

    assert len(runtime2.calls) == 1
    user_id, request, trace_id, scheduled_read_only = runtime2.calls[0]
    assert user_id == "user_a1"
    # Read-only authority travels through the trusted invoke parameter, NOT via
    # caller-controlled request fields (the real runtime contract rejects those).
    assert scheduled_read_only is True
    assert request["message"] == "read inbox"
    assert set(request) <= {"message", "actorId", "channel"}
    # The read-only turn's request carries no chatId or provider secret.
    serialized = json.dumps(request).lower()
    assert "chatid" not in serialized
    assert "token" not in serialized
    assert len(delivery2.calls) == 1
    assert delivery2.calls[0]["html"] == "here is your digest"


def test_scheduled_occurrence_body_with_forbidden_shape_is_rejected():
    deps = dependencies()
    # An occurrence that lies about its bound occurrenceId is rejected.
    body, occurrence_id = occurrence_body(task_type="REMINDER")
    tampered = json.loads(body)
    tampered["occurrenceId"] = _derive_occurrence_id(SCHEDULE_ID, 9, 1_800_000_600)
    record = occurrence_sqs_record(
        json.dumps(tampered, separators=(",", ":"), sort_keys=True), occurrence_id
    )
    result = worker.process_sqs_event({"Records": [record]}, deps)
    assert result == {"batchItemFailures": [{"itemIdentifier": "sqs-occ-1"}]}
