import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from unittest.mock import MagicMock

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
machine_module = load("action_state_machine", "state_machine.py")
repository_module = load("action_repository", "repository.py")
NOW = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)


class AwsError(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


def draft(action_id="action_12345678", revision=4):
    return models.DraftRevision(
        action_id=action_id,
        user_id="founder-1",
        draft_revision=revision,
        connection_id="google_conn_1234",
        account_email="founder@example.com",
        sender_address="founder@example.com",
        args={"to": "person@example.net", "subject": "Following up", "body": "Hello again"},
        created_at=NOW,
    )


def prepared_record():
    value = draft()
    return {
        "PK": "USER#founder-1",
        "SK": "ACTION#action_12345678",
        "actionId": value.action_id,
        "userId": value.user_id,
        "state": "PREPARED",
        "revision": 1,
        "draftRevision": value.draft_revision,
        "connectionId": value.connection_id,
        "accountEmail": value.account_email,
        "senderAddress": value.sender_address,
        "capability": "gmail.send",
        "resource": value.resource,
        "args": dict(value.args),
        "payloadHash": value.payload_hash,
        "createdAt": NOW.isoformat(),
        "updatedAt": NOW.isoformat(),
        "ttl": int((NOW + timedelta(days=14)).timestamp()),
    }


def repository(table):
    return repository_module.DynamoActionRepository(table, now=lambda: NOW)


def approval_updates():
    return {
        "approvalId": "appr_1234567890abcdef",
        "approvalActionId": "action_12345678",
        "approvalDraftRevision": 4,
        "approvalArgsHash": draft().payload_hash,
        "approvalExpiresAt": (NOW + timedelta(minutes=5)).isoformat(),
        "approvalRequestedAt": NOW.isoformat(),
    }


def applied_response(base):
    def applied(**kwargs):
        values = kwargs["ExpressionAttributeValues"]
        names = kwargs["ExpressionAttributeNames"]
        item = {
            **base,
            "state": values[":targetState"],
            "revision": values[":nextRevision"],
            "lastTransitionId": values[":transitionId"],
        }
        for token, name in names.items():
            if token.startswith("#u"):
                item[name] = values[token.replace("#", ":")]
        return {"Attributes": item}

    return applied


def test_create_prepared_accepts_only_typed_immutable_draft_boundary():
    table = MagicMock()
    created = repository(table).create_prepared(draft=draft())

    assert created["draftRevision"] == 4
    assert created["resource"] == draft().resource
    assert created["connectionId"] == "google_conn_1234"
    assert created["ttl"] == int((NOW + timedelta(days=14)).timestamp())
    assert table.put_item.call_args.kwargs["Item"]["ttl"] == created["ttl"]
    assert table.put_item.call_args.kwargs["ConditionExpression"] == (
        "attribute_not_exists(PK) AND attribute_not_exists(SK)"
    )
    with pytest.raises(TypeError):
        repository(table).create_prepared(draft={"forged": True})


def test_create_reconciles_response_loss_only_for_exact_typed_revision():
    table = MagicMock()
    table.put_item.side_effect = TimeoutError("response lost")
    table.get_item.side_effect = lambda **_: {
        "Item": dict(table.put_item.call_args.kwargs["Item"])
    }

    created = repository(table).create_prepared(draft=draft())

    assert created["creationId"] == table.put_item.call_args.kwargs["Item"]["creationId"]


def test_create_does_not_accept_a_corrupt_or_extended_retention_boundary():
    table = MagicMock()
    table.put_item.side_effect = TimeoutError("response lost")

    def corrupt_read(**_kwargs):
        item = dict(table.put_item.call_args.kwargs["Item"])
        item["ttl"] += 1
        return {"Item": item}

    table.get_item.side_effect = corrupt_read
    with pytest.raises(machine_module.ConcurrentActionUpdate):
        repository(table).create_prepared(draft=draft())


def test_create_rejects_same_key_for_different_draft_revision():
    table = MagicMock()
    table.put_item.side_effect = AwsError()
    table.get_item.return_value = {"Item": {**prepared_record(), "draftRevision": 3}}

    with pytest.raises(machine_module.ConcurrentActionUpdate):
        repository(table).create_prepared(draft=draft())


def test_transition_fences_binding_state_revision_draft_and_unique_operation():
    table = MagicMock()
    table.update_item.side_effect = applied_response(prepared_record())

    updated = repository(table).transition(
        action_id="action_12345678",
        user_id="founder-1",
        expected_state=models.ActionState.PREPARED,
        target_state=models.ActionState.APPROVAL_PENDING,
        expected_revision=1,
        transition_id="op_request_123456789",
        updates=approval_updates(),
    )

    call = table.update_item.call_args.kwargs
    condition = call["ConditionExpression"]
    for fragment in (
        "#actionId=:actionId",
        "#userId=:userId",
        "#state=:expectedState",
        "#revision=:expectedRevision",
        "#draftRevision=:expectedDraftRevision",
        "#ttl>:transitionEpoch",
    ):
        assert fragment in condition
    assert call["ExpressionAttributeValues"][":transitionEpoch"] == int(NOW.timestamp())
    assert ":retentionTtl" not in call["ExpressionAttributeValues"]
    assert updated["ttl"] == int((NOW + timedelta(days=14)).timestamp())
    assert updated["lastTransitionId"] == "op_request_123456789"


def test_ambiguous_transition_is_reconciled_only_by_exact_operation_marker():
    table = MagicMock()

    def lost(**kwargs):
        table.get_item.return_value = applied_response(prepared_record())(**kwargs)
        table.get_item.return_value = {
            "Item": table.get_item.return_value["Attributes"]
        }
        raise TimeoutError("response lost")

    table.update_item.side_effect = lost
    updated = repository(table).transition(
        action_id="action_12345678",
        user_id="founder-1",
        expected_state=models.ActionState.PREPARED,
        target_state=models.ActionState.APPROVAL_PENDING,
        expected_revision=1,
        transition_id="op_request_123456789",
        updates=approval_updates(),
    )
    assert updated["state"] == "APPROVAL_PENDING"

    table = MagicMock()
    table.update_item.side_effect = AwsError()
    table.get_item.return_value = {
        "Item": {
            **prepared_record(),
            **approval_updates(),
            "state": "APPROVAL_PENDING",
            "revision": 2,
            "lastTransitionId": "op_other_12345678901",
        }
    }
    with pytest.raises(machine_module.ConcurrentActionUpdate):
        repository(table).transition(
            action_id="action_12345678",
            user_id="founder-1",
            expected_state=models.ActionState.PREPARED,
            target_state=models.ActionState.APPROVAL_PENDING,
            expected_revision=1,
            transition_id="op_request_123456789",
            updates=approval_updates(),
        )


@pytest.mark.parametrize(
    "updates",
    [
        {},
        {**approval_updates(), "extra": True},
        {key: value for key, value in approval_updates().items() if key != "approvalActionId"},
        {**approval_updates(), "approvalActionId": "action_other123"},
        {**approval_updates(), "approvalDraftRevision": 0},
    ],
)
def test_approval_transition_requires_complete_exact_schema(updates):
    table = MagicMock()
    with pytest.raises(ValueError):
        repository(table).transition(
            action_id="action_12345678",
            user_id="founder-1",
            expected_state=models.ActionState.PREPARED,
            target_state=models.ActionState.APPROVAL_PENDING,
            expected_revision=1,
            transition_id="op_request_123456789",
            updates=updates,
        )
    table.update_item.assert_not_called()


def test_dispatch_transition_requires_operation_marker_to_equal_unique_caller_id():
    table = MagicMock()
    with pytest.raises(ValueError):
        repository(table).transition(
            action_id="action_12345678",
            user_id="founder-1",
            expected_state=models.ActionState.APPROVED,
            target_state=models.ActionState.DISPATCHING,
            expected_revision=7,
            transition_id="op_dispatch_aaaaaaaa",
            updates={
                "messageId": "<po-aaaaaaaaaaaaaaaaaaaaaaaa@personal-operator.invalid>",
                "dispatchOperationId": "op_dispatch_bbbbbbbb",
                "dispatchDraftRevision": 4,
            },
        )


def test_approval_and_dispatch_are_atomically_fenced_by_both_retention_deadlines():
    pending = {
        **prepared_record(),
        **approval_updates(),
        "state": "APPROVAL_PENDING",
        "revision": 2,
    }
    approved_updates = {
        "approvalId": pending["approvalId"],
        "approvedActionId": pending["actionId"],
        "approvedDraftRevision": pending["draftRevision"],
        "approvedArgsHash": pending["payloadHash"],
        "approvedAt": NOW.isoformat(),
    }
    table = MagicMock()
    table.update_item.side_effect = applied_response(pending)
    approved = repository(table).transition(
        action_id=pending["actionId"],
        user_id=pending["userId"],
        expected_state=models.ActionState.APPROVAL_PENDING,
        target_state=models.ActionState.APPROVED,
        expected_revision=2,
        transition_id="op_approve_123456789",
        updates=approved_updates,
    )
    condition = table.update_item.call_args.kwargs["ConditionExpression"]
    assert "#ttl>:transitionEpoch" in condition
    assert "#approvalExpiresAt>:updatedAt" in condition

    table = MagicMock()
    table.update_item.side_effect = applied_response(approved)
    repository(table).transition(
        action_id=approved["actionId"],
        user_id=approved["userId"],
        expected_state=models.ActionState.APPROVED,
        target_state=models.ActionState.DISPATCHING,
        expected_revision=3,
        transition_id="op_dispatch_aaaaaaaa",
        updates={
            "messageId": "<po-aaaaaaaaaaaaaaaaaaaaaaaa@personal-operator.invalid>",
            "dispatchOperationId": "op_dispatch_aaaaaaaa",
            "dispatchDraftRevision": approved["draftRevision"],
        },
    )
    condition = table.update_item.call_args.kwargs["ConditionExpression"]
    assert "#ttl>:transitionEpoch" in condition
    assert "#approvalExpiresAt>:updatedAt" in condition


def valid_confirmation():
    receipt = models.EffectReceipt(
        provider_message_id="gmail-1",
        provider_thread_id="thread-1",
        message_id="<po-aaaaaaaaaaaaaaaaaaaaaaaa@personal-operator.invalid>",
        connection_id="google_conn_1234",
        account_email="founder@example.com",
        sender_address="founder@example.com",
        recipient="person@example.net",
        payload_hash=draft().payload_hash,
        executed_at=NOW,
        labels=("SENT",),
    )
    tracker = models.WaitingForReply(
        action_id="action_12345678",
        draft_revision=4,
        connection_id="google_conn_1234",
        account_email="founder@example.com",
        recipient="person@example.net",
        message_id=receipt.message_id,
        provider_thread_id=receipt.provider_thread_id,
        since=NOW,
    )
    return {
        "effectReceipt": receipt.record(),
        "waitingForReply": tracker.record(),
        "confirmationMethod": "provider-history-reconciliation",
        "confirmedAt": NOW.isoformat(),
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["effectReceipt"].pop("labels"),
        lambda value: value["effectReceipt"].update(labels=["INBOX"]),
        lambda value: value["waitingForReply"].update(providerThreadId="other"),
        lambda value: value.update(confirmationMethod="caller-assertion"),
    ],
)
def test_confirmation_requires_validated_sent_receipt_and_exact_reply_tracker(mutation):
    updates = valid_confirmation()
    mutation(updates)
    with pytest.raises(ValueError):
        repository(MagicMock()).transition(
            action_id="action_12345678",
            user_id="founder-1",
            expected_state=models.ActionState.UNCERTAIN,
            target_state=models.ActionState.CONFIRMED,
            expected_revision=9,
            transition_id="op_confirm_123456789",
            updates=updates,
        )


def test_terminal_action_and_receipt_gain_exact_ninety_day_ttl():
    table = MagicMock()

    def applied(**kwargs):
        values = kwargs["ExpressionAttributeValues"]
        return {
            "Attributes": {
                **prepared_record(),
                **valid_confirmation(),
                "state": "CONFIRMED",
                "revision": 10,
                "lastTransitionId": "op_confirm_123456789",
                "ttl": values[":retentionTtl"],
            }
        }

    table.update_item.side_effect = applied
    result = repository(table).transition(
        action_id="action_12345678",
        user_id="founder-1",
        expected_state=models.ActionState.UNCERTAIN,
        target_state=models.ActionState.CONFIRMED,
        expected_revision=9,
        transition_id="op_confirm_123456789",
        updates=valid_confirmation(),
    )

    expected = int((NOW + timedelta(days=90)).timestamp())
    call = table.update_item.call_args.kwargs
    assert call["ExpressionAttributeValues"][":retentionTtl"] == expected
    assert "#retentionTtl=:retentionTtl" in call["UpdateExpression"]
    assert "#ttl>:transitionEpoch" in call["ConditionExpression"]
    assert result["ttl"] == expected


def test_dispatch_confirmation_is_also_atomically_fenced_by_live_retention():
    table = MagicMock()

    def applied(**kwargs):
        values = kwargs["ExpressionAttributeValues"]
        return {
            "Attributes": {
                **prepared_record(),
                **valid_confirmation(),
                "state": "CONFIRMED",
                "revision": 9,
                "lastTransitionId": "op_confirm_123456789",
                "ttl": values[":retentionTtl"],
            }
        }

    table.update_item.side_effect = applied
    repository(table).transition(
        action_id="action_12345678",
        user_id="founder-1",
        expected_state=models.ActionState.DISPATCHING,
        target_state=models.ActionState.CONFIRMED,
        expected_revision=8,
        transition_id="op_confirm_123456789",
        updates=valid_confirmation(),
    )

    call = table.update_item.call_args.kwargs
    assert "#ttl>:transitionEpoch" in call["ConditionExpression"]
    assert call["ExpressionAttributeValues"][":transitionEpoch"] == int(
        NOW.timestamp()
    )


def test_uncertain_effect_quarantine_gains_exact_ninety_day_ttl():
    table = MagicMock()

    def applied(**kwargs):
        values = kwargs["ExpressionAttributeValues"]
        return {
            "Attributes": {
                **prepared_record(),
                "state": "UNCERTAIN",
                "revision": 9,
                "lastTransitionId": "op_uncertain_123456789",
                "uncertainAt": NOW.isoformat(),
                "uncertaintyReason": "provider-outcome-unproven",
                "uncertainDraftRevision": 4,
                "ttl": values[":retentionTtl"],
            }
        }

    table.update_item.side_effect = applied
    result = repository(table).transition(
        action_id="action_12345678",
        user_id="founder-1",
        expected_state=models.ActionState.DISPATCHING,
        target_state=models.ActionState.UNCERTAIN,
        expected_revision=8,
        transition_id="op_uncertain_123456789",
        updates={
            "uncertainAt": NOW.isoformat(),
            "uncertaintyReason": "provider-outcome-unproven",
            "uncertainDraftRevision": 4,
        },
    )

    expected = int((NOW + timedelta(days=90)).timestamp())
    call = table.update_item.call_args.kwargs
    assert call["ExpressionAttributeValues"][":retentionTtl"] == expected
    assert result["ttl"] == expected


def test_get_is_strongly_consistent_and_rejects_cross_binding_corruption():
    table = MagicMock()
    table.get_item.return_value = {"Item": {**prepared_record(), "userId": "other"}}
    with pytest.raises(repository_module.ActionRepositoryError):
        repository(table).get(action_id="action_12345678", user_id="founder-1")
    assert table.get_item.call_args.kwargs["ConsistentRead"] is True
