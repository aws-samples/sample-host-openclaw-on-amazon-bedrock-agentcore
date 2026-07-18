import importlib.util
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import itertools
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
NOW = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)


def test_canonical_payload_hash_is_order_independent_but_exact_value_sensitive():
    first = {"to": "a@example.net", "subject": "Hello", "body": "One"}
    assert models.canonical_args_hash(first) == models.canonical_args_hash(
        {"body": "One", "to": "a@example.net", "subject": "Hello"}
    )
    assert models.canonical_args_hash(first) != models.canonical_args_hash(
        {**first, "body": "One "}
    )
    for invalid in [{"x": float("nan")}, {"x": b"bytes"}, {"x": {1, 2}}]:
        with pytest.raises((TypeError, ValueError)):
            models.canonical_args_hash(invalid)


def grant(action_id="action_12345678", revision=4):
    args = {"to": "a@example.net", "subject": "Hi", "body": "Exact"}
    return models.CapabilityGrant(
        action_id=action_id,
        draft_revision=revision,
        user_id="founder-1",
        capability="gmail.send",
        resource="google:gmail:connection:google_conn_1234:account:founder@example.com",
        args_hash=models.canonical_args_hash(args),
        expires_at=NOW + timedelta(minutes=5),
        approval_id="appr_1234567890abcdef",
    )


def test_grant_binds_action_draft_user_account_payload_expiry_and_approval():
    value = grant()
    args = {"to": "a@example.net", "subject": "Hi", "body": "Exact"}
    assert value.assert_authorized(
        action_id=value.action_id,
        draft_revision=4,
        user_id=value.user_id,
        capability=value.capability,
        resource=value.resource,
        args=args,
        now=NOW,
    ) is value
    mutations = [
        {"action_id": "action_other123"},
        {"draft_revision": 5},
        {"user_id": "other"},
        {"resource": "google:gmail:connection:other_conn_12:account:founder@example.com"},
        {"args": {**args, "body": "changed"}},
        {"now": NOW + timedelta(minutes=5)},
    ]
    for mutation in mutations:
        call = {
            "action_id": value.action_id,
            "draft_revision": 4,
            "user_id": value.user_id,
            "capability": value.capability,
            "resource": value.resource,
            "args": args,
            "now": NOW,
            **mutation,
        }
        with pytest.raises(models.CapabilityDenied):
            value.assert_authorized(**call)


LEGAL = {
    models.ActionState.PREPARED: {models.ActionState.APPROVAL_PENDING, models.ActionState.CANCELLED, models.ActionState.STALE},
    models.ActionState.APPROVAL_PENDING: {models.ActionState.APPROVED, models.ActionState.REJECTED, models.ActionState.EXPIRED, models.ActionState.STALE, models.ActionState.CANCELLED},
    models.ActionState.APPROVED: {models.ActionState.DISPATCHING, models.ActionState.EXPIRED, models.ActionState.STALE, models.ActionState.CANCELLED},
    models.ActionState.DISPATCHING: {models.ActionState.CONFIRMED, models.ActionState.UNCERTAIN},
    models.ActionState.UNCERTAIN: {models.ActionState.CONFIRMED},
    models.ActionState.CONFIRMED: set(),
    models.ActionState.REJECTED: set(),
    models.ActionState.EXPIRED: set(),
    models.ActionState.STALE: set(),
    models.ActionState.CANCELLED: set(),
}


def test_every_state_transition_is_explicitly_allowed_or_rejected():
    for current in models.ActionState:
        for target in models.ActionState:
            if target in LEGAL[current]:
                assert machine_module.assert_transition(current, target) == target
            else:
                with pytest.raises(machine_module.IllegalTransition):
                    machine_module.assert_transition(current, target)


class ActionRepository:
    def __init__(self, record):
        self.record = dict(record)
        self.calls = []
        self.lock = threading.Lock()

    def get(self, *, action_id, user_id):
        with self.lock:
            if self.record["actionId"] == action_id and self.record["userId"] == user_id:
                return dict(self.record)
            return None

    def transition(self, **kwargs):
        with self.lock:
            self.calls.append(kwargs)
            if (
                self.record["state"] != kwargs["expected_state"].value
                or self.record["revision"] != kwargs["expected_revision"]
            ):
                raise machine_module.ConcurrentActionUpdate("lost race")
            self.record["state"] = kwargs["target_state"].value
            self.record["revision"] += 1
            self.record["lastTransitionId"] = kwargs["transition_id"]
            self.record.update(kwargs["updates"])
            return dict(self.record)


def prepared_action(action_id="action_12345678", revision=4):
    args = {"to": "a@example.net", "subject": "Hi", "body": "Exact"}
    return {
        "actionId": action_id,
        "userId": "founder-1",
        "state": "PREPARED",
        "revision": 1,
        "draftRevision": revision,
        "connectionId": "google_conn_1234",
        "accountEmail": "founder@example.com",
        "senderAddress": "founder@example.com",
        "capability": "gmail.send",
        "resource": "google:gmail:connection:google_conn_1234:account:founder@example.com",
        "args": args,
        "payloadHash": models.canonical_args_hash(args),
    }


def id_factory(prefix):
    counter = itertools.count(1)
    lock = threading.Lock()

    def next_id():
        with lock:
            return f"{prefix}_{next(counter):016d}"

    return next_id


def approval_service(record, *, now=lambda: NOW, repo=None):
    repo = repo or ActionRepository(record)
    machine = machine_module.ActionStateMachine(
        repo, operation_id_factory=id_factory("op")
    )
    service = machine_module.ApprovalService(
        state_machine=machine,
        token_codec=machine_module.ApprovalTokenCodec(b"p" * 32),
        founder_user_ids={"founder-1"},
        now=now,
        approval_id_factory=id_factory("appr"),
    )
    return service, repo


def request_approval(service, record, *, expires_at=None):
    return service.request_approval(
        action_id=record["actionId"],
        revision=record["revision"],
        acting_user_id=record["userId"],
        args=record["args"],
        expires_at=expires_at or NOW + timedelta(minutes=5),
    )


def test_state_machine_assigns_a_fresh_unforgeable_operation_per_caller():
    repo = ActionRepository(prepared_action())
    machine = machine_module.ActionStateMachine(repo, operation_id_factory=id_factory("op"))
    machine.transition(
        action_id="action_12345678",
        user_id="founder-1",
        current=models.ActionState.PREPARED,
        target=models.ActionState.CANCELLED,
        revision=1,
        updates={"cancelledAt": NOW.isoformat(), "cancellationReason": "test"},
    )
    assert repo.calls[0]["transition_id"].startswith("op_")


def test_token_is_tamper_evident_and_roundtrips_action_and_draft_binding():
    codec = machine_module.ApprovalTokenCodec(b"s" * 32)
    token = codec.encode(grant())
    assert codec.decode(token) == grant()
    with pytest.raises(machine_module.InvalidApprovalToken):
        codec.decode(token[:-1] + ("A" if token[-1] != "A" else "B"))


def test_request_approval_generates_and_reserves_approval_id_inside_trusted_service():
    record = prepared_action()
    service, repo = approval_service(record)

    token = request_approval(service, record)
    decoded = service.decode(token)

    assert decoded.action_id == record["actionId"]
    assert decoded.draft_revision == record["draftRevision"]
    assert decoded.approval_id.startswith("appr_")
    assert repo.record["approvalId"] == decoded.approval_id
    assert repo.record["approvalActionId"] == record["actionId"]
    assert repo.record["approvalDraftRevision"] == record["draftRevision"]
    with pytest.raises(TypeError):
        service.request_approval(
            action_id=record["actionId"],
            revision=2,
            acting_user_id=record["userId"],
            args=record["args"],
            expires_at=NOW + timedelta(minutes=5),
            approval_id="caller_chosen_1234",
        )


def test_approval_is_one_time_founder_only_and_exact_action_revision():
    record = prepared_action()
    service, repo = approval_service(record)
    token = request_approval(service, record)
    approved = service.approve(
        action_id=record["actionId"],
        revision=2,
        acting_user_id=record["userId"],
        token=token,
        args=record["args"],
    )
    assert approved["approvedActionId"] == record["actionId"]
    assert approved["approvedDraftRevision"] == record["draftRevision"]
    with pytest.raises(machine_module.ConcurrentActionUpdate):
        service.approve(
            action_id=record["actionId"],
            revision=2,
            acting_user_id=record["userId"],
            token=token,
            args=record["args"],
        )
    with pytest.raises(models.CapabilityDenied):
        service.reject(action_id=record["actionId"], revision=3, acting_user_id="pilot-2")


def test_token_for_same_payload_cannot_approve_other_action_or_new_draft():
    first = prepared_action()
    service, _ = approval_service(first)
    token = request_approval(service, first)
    for second in [prepared_action("action_other123", 4), prepared_action("action_12345678", 5)]:
        other, other_repo = approval_service(second)
        request_approval(other, second)
        with pytest.raises(models.CapabilityDenied):
            other.approve(
                action_id=second["actionId"],
                revision=2,
                acting_user_id=second["userId"],
                token=token,
                args=second["args"],
            )
        assert other_repo.record["state"] == "APPROVAL_PENDING"


def test_expiry_is_exclusive_and_reject_is_terminal():
    record = prepared_action()
    service, repo = approval_service(record)
    token = request_approval(service, record, expires_at=NOW)
    with pytest.raises(models.CapabilityDenied):
        service.approve(
            action_id=record["actionId"], revision=2, acting_user_id=record["userId"], token=token, args=record["args"]
        )
    assert repo.record["state"] == "EXPIRED"

    record = prepared_action()
    service, repo = approval_service(record)
    token = request_approval(service, record)
    assert service.reject(action_id=record["actionId"], revision=2, acting_user_id=record["userId"])["state"] == "REJECTED"
    with pytest.raises(machine_module.ConcurrentActionUpdate):
        service.approve(action_id=record["actionId"], revision=2, acting_user_id=record["userId"], token=token, args=record["args"])


def test_newer_draft_revision_atomically_stales_old_prepared_action():
    record = prepared_action(revision=4)
    service, repo = approval_service(record)
    stale = service.mark_stale(
        action_id=record["actionId"],
        revision=1,
        user_id=record["userId"],
        expected_draft_revision=4,
        current_draft_revision=5,
    )
    assert stale["state"] == "STALE"
    assert stale["supersededByDraftRevision"] == 5
    with pytest.raises(machine_module.ConcurrentActionUpdate):
        request_approval(service, record)


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class ThreadSafeDynamoTable:
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
                or (
                    ":expectedDraftRevision" in values
                    and self.item["draftRevision"] != values[":expectedDraftRevision"]
                )
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


def dynamo_item(record):
    return {
        **record,
        "PK": f"USER#{record['userId']}",
        "SK": f"ACTION#{record['actionId']}",
        "createdAt": NOW.isoformat(),
        "updatedAt": NOW.isoformat(),
    }


def test_concrete_dynamo_repository_allows_exactly_one_concurrent_approval():
    record = prepared_action()
    table = ThreadSafeDynamoTable(dynamo_item(record))
    repo = repository_module.DynamoActionRepository(table, now=lambda: NOW)
    service, _ = approval_service(record, repo=repo)
    token = request_approval(service, record)

    def approve():
        return service.approve(
            action_id=record["actionId"],
            revision=2,
            acting_user_id=record["userId"],
            token=token,
            args=record["args"],
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(approve) for _ in range(32)]
    results, errors = [], []
    for future in futures:
        try:
            results.append(future.result())
        except Exception as error:
            errors.append(error)
    assert len(results) == 1
    assert len(errors) == 31
    assert table.item["state"] == "APPROVED"
    assert all(isinstance(error, machine_module.ConcurrentActionUpdate) for error in errors)
