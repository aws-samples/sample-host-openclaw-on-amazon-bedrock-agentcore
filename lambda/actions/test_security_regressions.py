import importlib.util
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import threading

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
send_module = load("action_gmail_send", "gmail_send.py")
reconcile_module = load("action_reconcile", "reconcile.py")

NOW = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
PROVIDER_TIME = NOW - timedelta(seconds=7)


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class ThreadSafeTransitionTable:
    """Minimal Dynamo fake with an atomic conditional update."""

    def __init__(self, item):
        self.item = dict(item)
        self.lock = threading.Lock()

    def get_item(self, **_kwargs):
        with self.lock:
            return {"Item": dict(self.item)}

    def update_item(self, **kwargs):
        with self.lock:
            values = kwargs["ExpressionAttributeValues"]
            if (
                self.item["actionId"] != values[":actionId"]
                or self.item["userId"] != values[":userId"]
                or self.item["state"] != values[":expectedState"]
                or self.item["revision"] != values[":expectedRevision"]
            ):
                raise ConditionalFailure()
            names = kwargs["ExpressionAttributeNames"]
            self.item["state"] = values[":targetState"]
            self.item["revision"] = values[":nextRevision"]
            self.item["updatedAt"] = values[":updatedAt"]
            self.item["lastTransitionId"] = values[":transitionId"]
            for token, name in names.items():
                if token.startswith("#u"):
                    self.item[name] = values[token.replace("#", ":")]
            return {"Attributes": dict(self.item)}


def prepared_item():
    args = {"to": "person@example.net", "subject": "Hello", "body": "Exact"}
    return {
        "PK": "USER#founder-1",
        "SK": "ACTION#action_12345678",
        "actionId": "action_12345678",
        "userId": "founder-1",
        "state": "APPROVED",
        "revision": 7,
        "draftRevision": 4,
        "connectionId": "google_conn_1234",
        "accountEmail": "founder@example.com",
        "senderAddress": "founder@example.com",
        "capability": "gmail.send",
        "resource": "google:gmail:connection:google_conn_1234:account:founder@example.com",
        "args": args,
        "payloadHash": models.canonical_args_hash(args),
        "approvalArgsHash": models.canonical_args_hash(args),
        "approvedArgsHash": models.canonical_args_hash(args),
        "approvalId": "appr_1234567890abcdef",
        "approvalActionId": "action_12345678",
        "approvedActionId": "action_12345678",
        "approvalDraftRevision": 4,
        "approvedDraftRevision": 4,
        "approvalExpiresAt": (NOW + timedelta(minutes=5)).isoformat(),
        "approvedAt": (NOW - timedelta(minutes=1)).isoformat(),
        "ttl": int((NOW + timedelta(days=14)).timestamp()),
    }


def test_identical_concurrent_dispatch_transitions_have_one_operation_winner():
    table = ThreadSafeTransitionTable(prepared_item())
    repo = repository_module.DynamoActionRepository(table, now=lambda: NOW)
    message_id = "<po-aaaaaaaaaaaaaaaaaaaaaaaa@personal-operator.invalid>"

    def claim(operation_id):
        return repo.transition(
            action_id="action_12345678",
            user_id="founder-1",
            expected_state=models.ActionState.APPROVED,
            target_state=models.ActionState.DISPATCHING,
            expected_revision=7,
            transition_id=operation_id,
            updates={
                "messageId": message_id,
                "dispatchOperationId": operation_id,
                "dispatchDraftRevision": 4,
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(claim, "op_dispatch_aaaaaaaa"),
            pool.submit(claim, "op_dispatch_bbbbbbbb"),
        ]
    results, errors = [], []
    for future in futures:
        try:
            results.append(future.result())
        except Exception as error:
            errors.append(error)

    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], machine_module.ConcurrentActionUpdate)
    assert results[0]["dispatchOperationId"] == results[0]["lastTransitionId"]


def test_approval_grant_binds_action_and_exact_draft_revision():
    grant = models.CapabilityGrant(
        action_id="action_12345678",
        draft_revision=4,
        user_id="founder-1",
        capability="gmail.send",
        resource="google:gmail:connection:google_conn_1234:account:founder@example.com",
        args_hash="a" * 64,
        expires_at=NOW + timedelta(minutes=5),
        approval_id="appr_1234567890abcdef",
    )

    with pytest.raises(models.CapabilityDenied):
        grant.assert_authorized(
            action_id="action_other123",
            draft_revision=4,
            user_id="founder-1",
            capability="gmail.send",
            resource=grant.resource,
            args={"not": "the same hash"},
            now=NOW,
        )


def test_typed_draft_revision_derives_exact_account_resource_and_payload():
    args = {"to": "person@example.net", "subject": "Hello", "body": "Exact"}
    draft = models.DraftRevision(
        action_id="action_12345678",
        user_id="founder-1",
        draft_revision=4,
        connection_id="google_conn_1234",
        account_email="founder@example.com",
        sender_address="founder@example.com",
        args=args,
        created_at=NOW,
    )

    assert draft.resource == (
        "google:gmail:connection:google_conn_1234:account:founder@example.com"
    )
    assert draft.payload_hash == models.canonical_args_hash(args)
    with pytest.raises(TypeError):
        draft.args["body"] = "mutated after preparation"


def test_reconciliation_rejects_message_id_match_without_sent_and_exact_payload_evidence():
    record = prepared_item()
    exact_message_id = send_module.deterministic_message_id(
        action_id=record["actionId"],
        draft_revision=record["draftRevision"],
        resource=record["resource"],
        payload_hash=record["payloadHash"],
    )
    record.update(
        state="UNCERTAIN",
        revision=9,
        messageId=exact_message_id,
        dispatchOperationId="op_dispatch_aaaaaaaa",
        dispatchDraftRevision=4,
    )

    class Repository:
        def get(self, **_kwargs):
            return dict(record)

        def transition(self, **_kwargs):
            raise AssertionError("weak evidence must never confirm")

    class WeakProvider:
        def __init__(self):
            self.calls = []

        def find_by_message_id(self, **_kwargs):
            self.calls.append(_kwargs)
            return {"id": "gmail-1", "threadId": "thread-1"}

    repo = Repository()
    provider = WeakProvider()
    reconciler = reconcile_module.GmailEffectReconciler(
        state_machine=machine_module.ActionStateMachine(repo),
        repository=repo,
        provider=provider,
        connection_id="google_conn_1234",
        account_email="founder@example.com",
        sender_address="founder@example.com",
        founder_user_ids={"founder-1"},
        deletion_blocked=lambda _user_id: False,
        now=lambda: NOW,
    )

    assert reconciler.reconcile(
        action_id=record["actionId"], user_id=record["userId"]
    ) is None
    assert len(provider.calls) == 1


def test_effect_receipt_uses_provider_execution_time_not_reconciliation_clock():
    evidence = {
        "id": "gmail-1",
        "threadId": "thread-1",
        "messageId": "<po-aaaaaaaaaaaaaaaaaaaaaaaa@personal-operator.invalid>",
        "connectionId": "google_conn_1234",
        "accountEmail": "founder@example.com",
        "senderAddress": "founder@example.com",
        "recipient": "person@example.net",
        "payloadHash": prepared_item()["payloadHash"],
        "executedAt": PROVIDER_TIME.isoformat(),
        "labels": ["SENT"],
    }

    receipt = models.EffectReceipt.from_provider_evidence(evidence)

    assert receipt.executed_at == PROVIDER_TIME
    assert receipt.labels == ("SENT",)
