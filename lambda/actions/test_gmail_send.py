import email
from email import policy
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
send_module = load("action_gmail_send", "gmail_send.py")
reconcile_module = load("action_reconcile", "reconcile.py")
NOW = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
PROVIDER_TIME = NOW - timedelta(seconds=7)
RESOURCE = "google:gmail:connection:google_conn_1234:account:founder@example.com"


def message_id(record):
    return send_module.deterministic_message_id(
        action_id=record["actionId"],
        draft_revision=record["draftRevision"],
        resource=record["resource"],
        payload_hash=record["payloadHash"],
    )


def action(*, state=models.ActionState.APPROVED, revision=7, overrides=None):
    args = {"to": "person@example.net", "subject": "Following up", "body": "Hello again"}
    if overrides:
        args.update(overrides)
    payload_hash = models.canonical_args_hash(args)
    record = {
        "actionId": "action_12345678",
        "userId": "founder-1",
        "state": state.value,
        "revision": revision,
        "draftRevision": 4,
        "connectionId": "google_conn_1234",
        "accountEmail": "founder@example.com",
        "senderAddress": "founder@example.com",
        "args": args,
        "payloadHash": payload_hash,
        "capability": "gmail.send",
        "resource": RESOURCE,
        "approvalArgsHash": payload_hash,
        "approvedArgsHash": payload_hash,
        "approvalId": "appr_1234567890abcdef",
        "approvalActionId": "action_12345678",
        "approvedActionId": "action_12345678",
        "approvalDraftRevision": 4,
        "approvedDraftRevision": 4,
        "approvalExpiresAt": (NOW + timedelta(minutes=5)).isoformat(),
        "approvedAt": (NOW - timedelta(minutes=1)).isoformat(),
    }
    if state in {models.ActionState.DISPATCHING, models.ActionState.UNCERTAIN}:
        record.update(
            messageId=message_id(record),
            dispatchOperationId="op_dispatch_aaaaaaaa",
            dispatchDraftRevision=4,
        )
    return record


def provider_evidence(*, message_id, payload_hash, **overrides):
    evidence = {
        "id": "provider-message-1",
        "threadId": "provider-thread-1",
        "messageId": message_id,
        "connectionId": "google_conn_1234",
        "accountEmail": "founder@example.com",
        "senderAddress": "founder@example.com",
        "recipient": "person@example.net",
        "payloadHash": payload_hash,
        "executedAt": PROVIDER_TIME.isoformat(),
        "labels": ["SENT"],
    }
    evidence.update(overrides)
    return evidence


class Repository:
    def __init__(self, record):
        self.record = dict(record)
        self.transitions = []
        self.lock = threading.Lock()

    def transition(self, **kwargs):
        with self.lock:
            self.transitions.append(kwargs)
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

    def get(self, *, action_id, user_id):
        with self.lock:
            if self.record["actionId"] == action_id and self.record["userId"] == user_id:
                return dict(self.record)
            return None


class Provider:
    def __init__(self, *, error=None, outcome_factory=None, found=None):
        self.error = error
        self.outcome_factory = outcome_factory
        self.found = found
        self.send_calls = []
        self.find_calls = []

    def send_raw(self, **kwargs):
        self.send_calls.append(kwargs)
        if self.error:
            raise self.error
        if self.outcome_factory:
            return self.outcome_factory(kwargs)
        return provider_evidence(
            message_id=kwargs["message_id"], payload_hash=kwargs["payload_hash"]
        )

    def find_by_message_id(self, **kwargs):
        self.find_calls.append(kwargs)
        if self.error:
            raise self.error
        if callable(self.found):
            return self.found(kwargs)
        return self.found


def operation_ids():
    counter = itertools.count(1)
    lock = threading.Lock()

    def next_id():
        with lock:
            return f"op_{next(counter):016d}"

    return next_id


def executor(record, provider=None, founders={"founder-1"}, repo=None):
    repo = repo or Repository(record)
    provider = provider or Provider()
    machine = machine_module.ActionStateMachine(
        repo, operation_id_factory=operation_ids()
    )
    return (
        send_module.GmailSendExecutor(
            state_machine=machine,
            provider=provider,
            founder_user_ids=founders,
            connection_id="google_conn_1234",
            account_email="founder@example.com",
            sender_address="founder@example.com",
            now=lambda: NOW,
        ),
        repo,
        provider,
    )


def reconciler(record, provider):
    repo = Repository(record)
    return (
        reconcile_module.GmailEffectReconciler(
            state_machine=machine_module.ActionStateMachine(
                repo, operation_id_factory=operation_ids()
            ),
            repository=repo,
            provider=provider,
            connection_id="google_conn_1234",
            account_email="founder@example.com",
            sender_address="founder@example.com",
            now=lambda: NOW,
        ),
        repo,
    )


def test_sends_exact_plain_text_once_and_receipt_uses_provider_execution_time():
    record = action()
    gateway, repo, provider = executor(record)

    receipt = gateway.execute(record)

    assert receipt.executed_at == PROVIDER_TIME
    assert receipt.labels == ("SENT",)
    assert len(provider.send_calls) == 1
    assert [call["target_state"] for call in repo.transitions] == [
        models.ActionState.DISPATCHING,
        models.ActionState.CONFIRMED,
    ]
    dispatch = repo.transitions[0]
    assert dispatch["updates"]["dispatchOperationId"] == dispatch["transition_id"]
    sent = provider.send_calls[0]
    message = email.message_from_bytes(sent["raw"], policy=policy.default)
    assert message["From"] == "founder@example.com"
    assert message["To"] == "person@example.net"
    assert message["Message-ID"] == sent["message_id"]
    assert message.get_content().rstrip("\r\n") == "Hello again"
    confirmed = repo.transitions[-1]["updates"]
    assert confirmed["effectReceipt"] == receipt.record()
    assert confirmed["waitingForReply"]["since"] == PROVIDER_TIME.isoformat()
    assert confirmed["confirmationMethod"] == "provider-send-evidence"


@pytest.mark.parametrize(
    "overrides",
    [
        {"cc": "other@example.net"},
        {"bcc": "other@example.net"},
        {"attachments": ["x"]},
        {"html": "<b>x</b>"},
        {"to": "a@example.net,b@example.net"},
        {"subject": "hello\r\nBcc: attacker@example.net"},
        {"body": ""},
    ],
)
def test_rejects_anything_beyond_one_plain_text_allowlisted_email(overrides):
    record = action(overrides=overrides)
    gateway, repo, provider = executor(record)
    with pytest.raises(send_module.SendValidationError):
        gateway.execute(record)
    assert repo.transitions == []
    assert provider.send_calls == []


@pytest.mark.parametrize(
    "mutate,founders",
    [
        (lambda record: record, {"other"}),
        (lambda record: record.update(approvedArgsHash="0" * 64), {"founder-1"}),
        (lambda record: record.update(approvedDraftRevision=5), {"founder-1"}),
        (lambda record: record.update(resource="google:gmail:connection:other_conn_12:account:founder@example.com"), {"founder-1"}),
        (lambda record: record.update(accountEmail="attacker@example.com"), {"founder-1"}),
    ],
)
def test_dispatch_requires_founder_exact_revision_and_google_account(mutate, founders):
    record = action()
    mutate(record)
    gateway, repo, provider = executor(record, founders=founders)
    with pytest.raises((models.CapabilityDenied, send_module.SendValidationError)):
        gateway.execute(record)
    assert repo.transitions == []
    assert provider.send_calls == []


def test_executor_uses_strongly_loaded_action_not_caller_effect_fields():
    persisted = action()
    forged = action(overrides={"to": "attacker@example.net", "body": "forged"})
    gateway, repo, provider = executor(persisted)
    receipt = gateway.execute(forged)
    assert receipt.payload_hash == persisted["payloadHash"]
    sent = email.message_from_bytes(provider.send_calls[0]["raw"], policy=policy.default)
    assert sent["To"] == persisted["args"]["to"]
    assert repo.record["effectReceipt"]["payloadHash"] == persisted["payloadHash"]


def test_expired_approval_never_dispatches():
    record = action()
    record["approvalExpiresAt"] = NOW.isoformat()
    gateway, repo, provider = executor(record)
    with pytest.raises(models.CapabilityDenied):
        gateway.execute(record)
    assert repo.record["state"] == "EXPIRED"
    assert provider.send_calls == []


@pytest.mark.parametrize(
    "provider",
    [
        Provider(error=TimeoutError("response lost")),
        Provider(error=ConnectionError("reset")),
        Provider(outcome_factory=lambda _: None),
        Provider(outcome_factory=lambda _: {"id": "only"}),
        Provider(outcome_factory=lambda call: provider_evidence(message_id=call["message_id"], payload_hash=call["payload_hash"], labels=["INBOX"])),
        Provider(outcome_factory=lambda call: provider_evidence(message_id=call["message_id"], payload_hash="0" * 64)),
    ],
)
def test_provider_faults_become_uncertain_and_never_blind_retry(provider):
    record = action()
    gateway, repo, provider = executor(record, provider=provider)
    with pytest.raises(send_module.EffectUncertain):
        gateway.execute(record)
    with pytest.raises(send_module.EffectUncertain):
        gateway.execute(repo.record)
    assert len(provider.send_calls) == 1
    assert repo.record["state"] == "UNCERTAIN"
    assert repo.record["uncertaintyReason"] == "provider-outcome-unproven"


def test_confirmed_replay_returns_persisted_exact_receipt_without_provider_call():
    record = action(state=models.ActionState.CONFIRMED, revision=9)
    record["messageId"] = message_id(record)
    receipt = models.EffectReceipt.from_provider_evidence(
        provider_evidence(message_id=record["messageId"], payload_hash=record["payloadHash"])
    )
    record["effectReceipt"] = receipt.record()
    gateway, _, provider = executor(record)
    assert gateway.execute(record) == receipt
    assert provider.send_calls == []


def test_uncertain_reconciliation_confirms_only_full_sent_exact_evidence_and_never_sends():
    record = action(state=models.ActionState.UNCERTAIN, revision=9)
    provider = Provider(
        found=lambda call: provider_evidence(
            message_id=call["message_id"], payload_hash=call["payload_hash"]
        )
    )
    gateway, repo = reconciler(record, provider)
    receipt = gateway.reconcile(action_id=record["actionId"], user_id=record["userId"])
    assert receipt.executed_at == PROVIDER_TIME
    assert repo.record["state"] == "CONFIRMED"
    assert repo.record["confirmationMethod"] == "provider-history-reconciliation"
    assert provider.send_calls == []
    assert provider.find_calls[0]["sender_address"] == "founder@example.com"
    assert provider.find_calls[0]["recipient"] == "person@example.net"


@pytest.mark.parametrize(
    "found",
    [
        None,
        {"id": "message-only", "threadId": "thread-only"},
        lambda call: provider_evidence(message_id=call["message_id"], payload_hash=call["payload_hash"], labels=["INBOX"]),
        lambda call: provider_evidence(message_id=call["message_id"], payload_hash=call["payload_hash"], senderAddress="attacker@example.com"),
        lambda call: provider_evidence(message_id=call["message_id"], payload_hash="0" * 64),
    ],
)
def test_weak_or_mismatched_reconciliation_evidence_never_confirms(found):
    record = action(state=models.ActionState.UNCERTAIN, revision=9)
    provider = Provider(found=found)
    gateway, repo = reconciler(record, provider)
    assert gateway.reconcile(action_id=record["actionId"], user_id=record["userId"]) is None
    assert repo.record["state"] == "UNCERTAIN"
    assert repo.transitions == []


class ConfirmWriteFailureRepository(Repository):
    def transition(self, **kwargs):
        if kwargs["target_state"] is models.ActionState.CONFIRMED:
            raise TimeoutError("confirmation write not applied")
        return super().transition(**kwargs)


def test_provider_success_without_durable_confirmation_becomes_uncertain_without_retry():
    record = action()
    repo = ConfirmWriteFailureRepository(record)
    gateway, repo, provider = executor(record, repo=repo)
    with pytest.raises(send_module.EffectUncertain):
        gateway.execute(record)
    assert len(provider.send_calls) == 1
    assert repo.record["state"] == "UNCERTAIN"
    assert "effectReceipt" not in repo.record


def test_concurrent_dispatchers_have_exactly_one_provider_invocation():
    record = action()
    repo = Repository(record)
    provider = Provider()

    def run():
        gateway, _, _ = executor(record, provider=provider, repo=repo)
        return gateway.execute(record)

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(run) for _ in range(24)]
    outcomes, errors = [], []
    for future in futures:
        try:
            outcomes.append(future.result())
        except Exception as error:
            errors.append(error)
    assert len(provider.send_calls) == 1
    assert outcomes
    assert all(receipt == outcomes[0] for receipt in outcomes)
    assert all(isinstance(error, send_module.EffectUncertain) for error in errors)
