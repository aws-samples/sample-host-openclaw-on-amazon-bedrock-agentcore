import importlib.util
from pathlib import Path
import sys
from unittest.mock import MagicMock

import pytest


WORKER_DIR = Path(__file__).resolve().parent
ROUTER_DIR = WORKER_DIR.parent / "router"
sys.path.insert(0, str(ROUTER_DIR))
spec = importlib.util.spec_from_file_location(
    "worker_processing_ledger", WORKER_DIR / "processing_ledger.py"
)
ledger_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ledger_module
assert spec.loader is not None
spec.loader.exec_module(ledger_module)

from event_identity import derive_event_trace  # noqa: E402
QueueEnvelope = ledger_module.QueueEnvelope


OWNER_A = "worker-" + "a" * 32
OWNER_B = "worker-" + "b" * 32


class AwsError(Exception):
    def __init__(self, code="ConditionalCheckFailedException"):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def envelope(message="hello"):
    return QueueEnvelope(
        user_id="user_a1",
        channel="telegram",
        update_id="100",
        trace_id=derive_event_trace("telegram", "user_a1", "100"),
        kind="message",
        payload={
            "chatId": "9001",
            "actorId": "telegram:42",
            "message": message,
        },
    )


def test_claim_is_one_conditional_event_record_with_bound_request_digest():
    table = MagicMock()
    ledger = ledger_module.DynamoProcessingLedger(table, clock_ms=lambda: 1_000)
    item = envelope()

    claim = ledger.claim_processing(item, owner=OWNER_A)

    assert claim.state == "CLAIMED"
    assert claim.key == item.trace_id
    put = table.put_item.call_args.kwargs
    assert put["ConditionExpression"] == "attribute_not_exists(eventId)"
    assert put["Item"]["eventId"] == item.trace_id
    assert put["Item"]["requestSha256"] == item.request_sha256
    assert put["Item"]["processingOwner"] == OWNER_A
    assert put["Item"]["state"] == "PROCESSING"


def test_same_event_with_changed_body_is_a_terminal_collision():
    table = MagicMock()
    table.put_item.side_effect = AwsError()
    original = envelope()
    changed = envelope("changed")
    table.get_item.return_value = {
        "Item": {
            "eventId": original.trace_id,
            "requestSha256": original.request_sha256,
            "userId": original.user_id,
            "traceId": original.trace_id,
            "state": "PROCESSING",
                "processingOwner": OWNER_A,
            "processingEpoch": 1,
            "claimExpiresAt": 2_000,
        }
    }
    ledger = ledger_module.DynamoProcessingLedger(table, clock_ms=lambda: 1_000)

    with pytest.raises(ledger_module.EventIdentityCollision):
        ledger.claim_processing(changed, owner=OWNER_B)


def test_unresolved_processing_claim_is_never_reclaimed_or_reexecuted():
    table = MagicMock()
    table.put_item.side_effect = AwsError()
    item = envelope()
    table.get_item.return_value = {
        "Item": {
            "eventId": item.trace_id,
            "requestSha256": item.request_sha256,
            "userId": item.user_id,
            "traceId": item.trace_id,
            "state": "PROCESSING",
            "processingOwner": OWNER_A,
            "processingEpoch": 1,
            "claimExpiresAt": 900,
        }
    }
    table.update_item.return_value = {
        "Attributes": {
            **table.get_item.return_value["Item"],
            "state": "PROCESSING_UNCERTAIN",
        }
    }
    ledger = ledger_module.DynamoProcessingLedger(table, clock_ms=lambda: 1_000)

    claim = ledger.claim_processing(item, owner=OWNER_B)

    assert claim.state == "PROCESSING_UNCERTAIN"
    update = table.update_item.call_args.kwargs
    assert ":processing" in update["ExpressionAttributeValues"]
    assert ":uncertain" in update["ExpressionAttributeValues"]
    assert "processingEpoch" in update["ConditionExpression"]


def test_result_completion_reconciles_an_ambiguous_applied_write():
    table = MagicMock()
    table.update_item.side_effect = TimeoutError("response lost")
    item = envelope()
    result = "done"
    result_sha = ledger_module.result_sha256(result)
    table.get_item.return_value = {
        "Item": {
            "eventId": item.trace_id,
            "requestSha256": item.request_sha256,
            "userId": item.user_id,
            "traceId": item.trace_id,
            "state": "RESULT_READY",
            "result": result,
            "resultSha256": result_sha,
        }
    }
    ledger = ledger_module.DynamoProcessingLedger(table, clock_ms=lambda: 1_000)
    claim = ledger_module.LedgerClaim(
        key=item.trace_id,
        request_sha256=item.request_sha256,
        state="CLAIMED",
        owner=OWNER_A,
        epoch=1,
    )

    ready = ledger.complete_result(claim, result)

    assert ready.state == "RESULT_READY"
    assert ready.result == result
    table.get_item.assert_called_with(
        Key={"eventId": item.trace_id}, ConsistentRead=True
    )


def test_delivery_claim_precedes_send_and_ambiguous_confirmation_reconciles():
    table = MagicMock()
    item = envelope()
    result = "done"
    ready_item = {
        "eventId": item.trace_id,
        "requestSha256": item.request_sha256,
        "userId": item.user_id,
        "traceId": item.trace_id,
        "state": "DELIVERY_IN_FLIGHT",
        "result": result,
        "resultSha256": ledger_module.result_sha256(result),
        "deliveryOwner": OWNER_A,
        "deliveryEpoch": 1,
    }
    table.update_item.side_effect = [
        {"Attributes": ready_item},
        TimeoutError("confirmation response lost"),
    ]
    table.get_item.return_value = {
        "Item": {
            **ready_item,
            "state": "DELIVERED",
            "providerMessageId": "tg-1",
        }
    }
    ledger = ledger_module.DynamoProcessingLedger(table, clock_ms=lambda: 1_000)
    ready = ledger_module.LedgerClaim(
        key=item.trace_id,
        request_sha256=item.request_sha256,
        state="RESULT_READY",
        result=result,
    )

    delivery = ledger.begin_delivery(ready, owner=OWNER_A)
    assert delivery.state == "DELIVERY_CLAIMED"
    ledger.confirm_delivery(delivery, {"providerMessageId": "tg-1"})

    first = table.update_item.call_args_list[0].kwargs
    assert first["ExpressionAttributeValues"][":inflight"] == "DELIVERY_IN_FLIGHT"
    assert "#state=:ready" in first["ConditionExpression"]
    table.get_item.assert_called_with(
        Key={"eventId": item.trace_id}, ConsistentRead=True
    )
