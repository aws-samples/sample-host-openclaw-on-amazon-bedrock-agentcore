from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import io
import itertools
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lambda"))

from actions.gmail_send import EffectUncertain, GmailSendExecutor
from actions.connectors import GenericConnectorKernel, GmailConnectorAdapter
from actions.models import ActionState, canonical_args_hash, gmail_resource
from actions.state_machine import (
    ActionStateMachine,
    ApprovalService,
    ApprovalTokenCodec,
    ConcurrentActionUpdate,
)
from control.index import ControlApplication, ControlRequestError
from router.event_identity import derive_event_trace
from web.auth import OpaqueSessionManager, SignedConnectTickets
from web.index import WebApplication
from web.retention import DeletionCoordinator, UserExporter
from web.services import ApprovalWebService


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
USER = "founder_one"
ACTION_ID = "action_12345678"
CONNECTION_ID = "google_conn_1234"
ACCOUNT = "founder@example.com"
ARGS = {
    "to": "person@example.net",
    "subject": "Following up",
    "body": "Hello again",
}
RESOURCE = gmail_resource(connection_id=CONNECTION_ID, account_email=ACCOUNT)
ORIGIN = "https://operator.example"


def _ids(prefix: str):
    counter = itertools.count(1)

    def next_id() -> str:
        return f"{prefix}_{next(counter):016d}"

    return next_id


def _random_bytes():
    counter = itertools.count(1)

    def random(size: int) -> bytes:
        seed = hashlib.sha512(f"synthetic-{next(counter)}".encode()).digest()
        return (seed * ((size // len(seed)) + 1))[:size]

    return random


class InMemoryWebStore:
    def __init__(self, events: list[str]) -> None:
        self.tickets: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.revoked_users: set[str] = set()
        self.deletion_intents: dict[str, dict] = {}
        self.events = events
        self.now_ms = 1_000_000

    def put_once(self, key, record, *, expires_at):
        if key in self.tickets:
            raise RuntimeError("ticket collision")
        self.tickets[key] = {**record, "expiresAt": expires_at}

    def pop_once(self, key):
        return self.tickets.pop(key, None)

    def create(self, key, record, *, expires_at):
        if key in self.sessions:
            raise RuntimeError("session collision")
        self.sessions[key] = {**record, "expiresAt": expires_at}

    def get(self, key):
        value = self.sessions.get(key)
        if value is None:
            return None
        return {
            **value,
            "revoked": value.get("revoked") is True
            or value.get("userId") in self.revoked_users,
        }

    def revoke(self, key):
        if key in self.sessions:
            self.sessions[key]["revoked"] = True

    def revoke_all(self, user_id):
        self.events.append(f"sessions.revoke:{user_id}")
        self.revoked_users.add(user_id)
        for record in self.sessions.values():
            if record.get("userId") == user_id:
                record["revoked"] = True

    def begin_deletion(self, user_id):
        self.events.append(f"sessions.intent:{user_id}")
        return self.deletion_intents.setdefault(
            user_id,
            {
                "userId": user_id,
                "purgeReason": "ACCOUNT_DELETION",
                "deletionStatus": "PENDING",
                "requestedAt": self.now_ms,
                "finalizingAt": None,
                "completedAt": None,
            },
        )

    def get_deletion_intent(self, user_id):
        record = self.deletion_intents.get(user_id)
        return dict(record) if record is not None else None

    def mark_deletion_finalizing(self, user_id):
        self.events.append(f"sessions.finalizing:{user_id}")
        record = self.deletion_intents[user_id]
        record["deletionStatus"] = "FINALIZING"
        record["finalizingAt"] = self.now_ms
        return dict(record)

    def complete_deletion(self, user_id, *, finalizing_before_ms):
        self.events.append(f"sessions.complete:{user_id}")
        record = self.deletion_intents[user_id]
        assert record["finalizingAt"] <= finalizing_before_ms
        self.deletion_intents[user_id] = {
            "userId": user_id,
            "deletionStatus": "COMPLETED",
            "purgeReason": "ACCOUNT_DELETION",
            "requestedAt": None,
            "finalizingAt": None,
            "completedAt": self.now_ms,
        }
        return dict(self.deletion_intents[user_id])


class ActionRepository:
    def __init__(self, record: dict) -> None:
        self.record = dict(record)
        self.transitions: list[dict] = []

    def get(self, *, action_id, user_id):
        if self.record["actionId"] == action_id and self.record["userId"] == user_id:
            return dict(self.record)
        return None

    def transition(self, **request):
        if (
            self.record["actionId"] != request["action_id"]
            or self.record["userId"] != request["user_id"]
            or self.record["state"] != request["expected_state"].value
            or self.record["revision"] != request["expected_revision"]
        ):
            raise ConcurrentActionUpdate("synthetic conditional write lost")
        self.transitions.append(dict(request))
        self.record.update(request["updates"])
        self.record["state"] = request["target_state"].value
        self.record["revision"] += 1
        self.record["lastTransitionId"] = request["transition_id"]
        return dict(self.record)


def _prepared_action() -> dict:
    return {
        "actionId": ACTION_ID,
        "userId": USER,
        "state": ActionState.PREPARED.value,
        "revision": 1,
        "draftRevision": 1,
        "connectionId": CONNECTION_ID,
        "accountEmail": ACCOUNT,
        "senderAddress": ACCOUNT,
        "args": dict(ARGS),
        "payloadHash": canonical_args_hash(ARGS),
        "ttl": int((NOW + timedelta(days=14)).timestamp()),
        "capability": "gmail.send",
        "resource": RESOURCE,
    }


def _approval_service(repository: ActionRepository):
    machine = ActionStateMachine(repository, operation_id_factory=_ids("op"))
    service = ApprovalService(
        state_machine=machine,
        token_codec=ApprovalTokenCodec(b"a" * 32),
        founder_user_ids={USER},
        now=lambda: NOW,
        approval_id_factory=_ids("appr"),
    )
    return machine, service


class ApprovalWebPort:
    def __init__(self, service: ApprovalService, repository: ActionRepository) -> None:
        self.service = service
        self.repository = repository

    def preview(self, *, token, acting_user_id):
        grant = self.service.decode(token)
        if grant.user_id != acting_user_id:
            raise PermissionError("approval does not belong to this user")
        record = self.repository.get(action_id=grant.action_id, user_id=acting_user_id)
        if (
            not isinstance(record, dict)
            or record.get("state") != ActionState.APPROVAL_PENDING.value
            or record.get("approvalId") != grant.approval_id
            or record.get("approvalArgsHash") != grant.args_hash
        ):
            raise PermissionError("approval is not pending")
        grant.assert_authorized(
            action_id=record["actionId"],
            draft_revision=record["draftRevision"],
            user_id=acting_user_id,
            capability=record["capability"],
            resource=record["resource"],
            args=record["args"],
            now=NOW,
        )
        return {
            "actionId": record["actionId"],
            "revision": record["revision"],
            "draftRevision": record["draftRevision"],
            "args": record["args"],
            "payloadHash": record["payloadHash"],
            "state": record["state"],
        }

    def approve(self, **request):
        record = self.service.approve(**request)
        return {
            "actionId": record["actionId"],
            "revision": record["revision"],
            "state": record["state"],
        }

    def reject(self, **request):
        record = self.service.reject(**request)
        return {
            "actionId": record["actionId"],
            "revision": record["revision"],
            "state": record["state"],
        }


class EvidenceProvider:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict] = []

    def send_raw(self, **request):
        self.calls.append(dict(request))
        if self.error:
            raise self.error
        return {
            "id": "provider-message-1",
            "threadId": "provider-thread-1",
            "messageId": request["message_id"],
            "connectionId": CONNECTION_ID,
            "accountEmail": ACCOUNT,
            "senderAddress": ACCOUNT,
            "recipient": ARGS["to"],
            "payloadHash": request["payload_hash"],
            "executedAt": (NOW - timedelta(seconds=1)).isoformat(),
            "labels": ["SENT"],
        }


class ExportSource:
    def __init__(self, repository: ActionRepository) -> None:
        self.repository = repository

    def records_for_user(self, user_id):
        assert user_id == USER
        receipts = []
        if isinstance(self.repository.record.get("effectReceipt"), dict):
            receipts.append(dict(self.repository.record["effectReceipt"]))
        return {
            "memory": [{"text": "A synthetic founder memory"}],
            "schedules": [{"title": "Synthetic follow-up"}],
            "receipts": receipts,
        }

    def workspace_files(self, user_id):
        assert user_id == USER
        return {"notes/founder.txt": b"synthetic founder workspace"}


class WorkspaceView:
    def get(self, user_id):
        return {"userId": user_id, "status": "ready", "files": ["notes/founder.txt"]}


class GmailWorkspaceView:
    def get(self, user_id):
        return {"userId": user_id, "opportunities": [], "drafts": []}

    def edit_draft(self, **_request):
        raise AssertionError("synthetic journey does not edit a Gmail draft")


class UnusedOAuth:
    def start(self, **_request):
        raise AssertionError("synthetic journey does not contact Google OAuth")

    def complete(self, **_request):
        raise AssertionError("synthetic journey does not contact Google OAuth")


class UnusedRetention:
    def sweep(self):
        raise AssertionError("synthetic journey does not run retention")


class UnusedImporter:
    def build_plan(self, *_args, **_kwargs):
        raise AssertionError("founder journey does not stage a portable import")

    def prepare_activation(self, *_args, **_kwargs):
        raise AssertionError("founder journey does not prepare a portable import")

    def activate(self, *_args, **_kwargs):
        raise AssertionError("founder journey does not activate a portable import")


class UnusedPilotPorts:
    def get(self, _user_id):
        raise AssertionError("founder journey does not request the pilot overview")

    def disconnect(self, _user_id):
        raise AssertionError("founder journey does not disconnect Gmail")

    def feedback(self, _user_id, _scan_id, *, response):
        raise AssertionError(f"founder journey does not record {response} feedback")


class UnusedScheduleControl:
    def preview(self, **_request):
        raise AssertionError("founder journey does not use schedule proposals")

    approve = preview
    reject = preview
    reconcile = preview

    @staticmethod
    def purge_user_schedules(_user_id):
        return 0


class SyntheticDeletionAuthority:
    @staticmethod
    def establish_deletion_fence(_user_id):
        return True


class DeletionDependency:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events

    def revoke_all(self, user_id):
        self.events.append(f"{self.name}.revoke:{user_id}")

    def purge(self, user_id):
        self.events.append(f"{self.name}.purge:{user_id}")
        return {
            "userId": user_id,
            "state": "DELETING",
            "purgeReason": "ACCOUNT_DELETION",
            "purgeCompletedAt": 1,
        }

    def delete_namespace(self, user_id):
        self.events.append(f"{self.name}.delete:{user_id}")

    def delete_user_records(self, user_id):
        self.events.append(f"{self.name}.delete:{user_id}")


class FakeGmailCards:
    def scan(self, *, user_id):
        assert user_id == USER
        return [
            SimpleNamespace(
                title="Ada is waiting",
                reason="A source-backed message has had no reply for eight days.",
                source=SimpleNamespace(
                    deep_link="https://mail.google.com/mail/u/0/#inbox/thread-1"
                ),
            )
        ]


class NoTasks:
    def list_open(self, user_id):
        assert user_id == USER
        return []


def _control_request(command: str, update_id: str) -> dict[str, str]:
    trace = derive_event_trace("telegram", USER, update_id)
    return {
        "action": "productCommand",
        "userId": USER,
        "channel": "telegram",
        "command": command,
        "traceId": trace,
        "idempotencyKey": trace,
    }


def _web_event(method: str, path: str, *, headers=None, body=None) -> dict:
    event = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"origin": ORIGIN, **(headers or {})},
    }
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def test_complete_synthetic_founder_connect_approve_receipt_export_delete_journey() -> None:
    events: list[str] = []
    store = InMemoryWebStore(events)
    random = _random_bytes()
    tickets = SignedConnectTickets(
        secret=b"web-auth-signing-key-at-least-32-bytes",
        store=store,
        now=lambda: int(NOW.timestamp()),
        random_bytes=random,
    )
    sessions = OpaqueSessionManager(
        secret=b"web-auth-signing-key-at-least-32-bytes",
        store=store,
        now=lambda: int(NOW.timestamp()),
        random_bytes=random,
    )
    repository = ActionRepository(_prepared_action())
    machine, approvals = _approval_service(repository)
    approval_token = approvals.request_approval(
        action_id=ACTION_ID,
        revision=1,
        acting_user_id=USER,
        args=ARGS,
        expires_at=NOW + timedelta(minutes=5),
    )
    assert repository.record["state"] == "APPROVAL_PENDING"
    provider = EvidenceProvider()

    def executor_factory(_pending):
        executor = GmailSendExecutor(
            state_machine=machine,
            provider=provider,
            founder_user_ids={USER},
            connection_id=CONNECTION_ID,
            account_email=ACCOUNT,
            sender_address=ACCOUNT,
            deletion_blocked=lambda _user_id: False,
            now=lambda: NOW,
        )
        return GenericConnectorKernel(
            GmailConnectorAdapter(
                executor=executor,
                connection_revoker=DeletionDependency("gmail", events),
            )
        )

    control = ControlApplication(
        tickets=tickets,
        gmail=FakeGmailCards(),
        tasks=NoTasks(),
        deletion_intents=store,
        web_origin=ORIGIN,
    )
    scan = control.handle(_control_request("/scan", "100"))
    assert scan["status"] == "ok"
    assert "Ada is waiting" in scan["text"]
    assert "https://mail.google.com/" in scan["text"]
    connect = control.handle(_control_request("/connect", "101"))
    connect_url = next(line for line in connect["text"].splitlines() if line.startswith("https://"))
    ticket = parse_qs(urlparse(connect_url).query)["ticket"][0]

    schedule_control = UnusedScheduleControl()
    deletion = DeletionCoordinator(
        session_store=store,
        authority_fence=SyntheticDeletionAuthority(),
        connection_store=DeletionDependency("connections", events),
        runtime_driver=DeletionDependency("runtime", events),
        workspace_store=DeletionDependency("workspace", events),
        record_store=DeletionDependency("records", events),
        footprint_store=DeletionDependency("footprint", events),
        schedule_store=schedule_control,
        clock_ms=lambda: store.now_ms,
    )
    web = WebApplication(
        tickets=tickets,
        sessions=sessions,
        oauth=UnusedOAuth(),
        approvals=ApprovalWebService(
            approval_service=approvals,
            action_reader=repository,
            executor_factory=executor_factory,
            founder_user_ids={USER},
            now=lambda: NOW,
        ),
        workspace=WorkspaceView(),
        gmail_workspace=GmailWorkspaceView(),
        exporter=UserExporter(ExportSource(repository)),
        importer=UnusedImporter(),
        deletion=deletion,
        retention=UnusedRetention(),
        overview=UnusedPilotPorts(),
        connections=UnusedPilotPorts(),
        scans=UnusedPilotPorts(),
        schedule_control=schedule_control,
        web_origin=ORIGIN,
        google_redirect_uri=f"{ORIGIN}/oauth/google/callback",
    )

    connected = web.handle(
        _web_event("POST", "/api/session/connect", body={"ticket": ticket})
    )
    assert connected["statusCode"] == 201
    cookie = connected["headers"]["Set-Cookie"]
    csrf = json.loads(connected["body"])["csrfToken"]
    replay = web.handle(
        _web_event("POST", "/api/session/connect", body={"ticket": ticket})
    )
    assert replay["statusCode"] == 400

    preview = web.handle(
        _web_event(
            "GET",
            f"/approve/{approval_token}",
            headers={"cookie": cookie},
        )
    )
    assert preview["statusCode"] == 200
    assert json.loads(preview["body"])["payloadHash"] == canonical_args_hash(ARGS)
    assert repository.record["state"] == "APPROVAL_PENDING"
    assert len(repository.transitions) == 1

    wrong_csrf = web.handle(
        _web_event(
            "POST",
            f"/api/actions/{ACTION_ID}/approve",
            headers={"cookie": cookie, "x-po-csrf": "x" * 43},
            body={"token": approval_token, "revision": 2, "args": ARGS},
        )
    )
    assert wrong_csrf["statusCode"] == 401
    assert repository.record["state"] == "APPROVAL_PENDING"

    approved = web.handle(
        _web_event(
            "POST",
            f"/api/actions/{ACTION_ID}/approve",
            headers={"cookie": cookie, "x-po-csrf": csrf},
            body={"token": approval_token, "revision": 2, "args": ARGS},
        )
    )
    assert approved["statusCode"] == 200
    approved_body = json.loads(approved["body"])
    assert approved_body["state"] == "CONFIRMED"
    assert len(provider.calls) == 1
    assert repository.record["state"] == "CONFIRMED"
    receipt_record = approved_body["receipt"]
    assert receipt_record["payloadHash"] == canonical_args_hash(ARGS)
    assert repository.record["effectReceipt"] == receipt_record
    assert repository.record["waitingForReply"]["providerThreadId"] == (
        "provider-thread-1"
    )

    workspace = web.handle(
        _web_event("GET", "/api/workspace", headers={"cookie": cookie})
    )
    assert workspace["statusCode"] == 200
    assert json.loads(workspace["body"])["userId"] == USER

    exported = web.handle(
        _web_event("GET", "/api/export", headers={"cookie": cookie})
    )
    assert exported["statusCode"] == 200
    assert exported["isBase64Encoded"] is True
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported["body"]))) as archive:
        receipt_records = json.loads(archive.read("records/receipts.json"))
        assert archive.read("workspace/notes/founder.txt") == (
            b"synthetic founder workspace"
        )
    assert receipt_records == [receipt_record]

    deleted = web.handle(
        _web_event(
            "POST",
            "/api/delete",
            headers={"cookie": cookie, "x-po-csrf": csrf},
            body={"confirm": "DELETE"},
        )
    )
    assert deleted["statusCode"] == 202
    assert json.loads(deleted["body"]) == {"status": "deletion_pending"}
    assert events == [
        f"sessions.intent:{USER}",
        f"sessions.revoke:{USER}",
        f"connections.revoke:{USER}",
        f"runtime.purge:{USER}",
        f"workspace.delete:{USER}",
        f"records.delete:{USER}",
        f"footprint.delete:{USER}",
        f"sessions.finalizing:{USER}",
    ]
    after_delete = web.handle(
        _web_event("GET", "/api/workspace", headers={"cookie": cookie})
    )
    assert after_delete["statusCode"] == 401

    with pytest.raises(ControlRequestError, match="deletion"):
        control.handle(_control_request("/start", "102"))

    store.now_ms += deletion.FINALIZATION_GRACE_MS
    assert deletion.reconcile(USER) == {"status": "deleted", "userId": USER}
    assert events[-7:] == [
        f"sessions.revoke:{USER}",
        f"connections.revoke:{USER}",
        f"runtime.purge:{USER}",
        f"workspace.delete:{USER}",
        f"records.delete:{USER}",
        f"footprint.delete:{USER}",
        f"sessions.complete:{USER}",
    ]


def test_provider_timeout_after_exact_approval_becomes_uncertain_and_never_resends() -> None:
    repository = ActionRepository(_prepared_action())
    machine, approvals = _approval_service(repository)
    token = approvals.request_approval(
        action_id=ACTION_ID,
        revision=1,
        acting_user_id=USER,
        args=ARGS,
        expires_at=NOW + timedelta(minutes=5),
    )
    approvals.approve(
        action_id=ACTION_ID,
        revision=2,
        acting_user_id=USER,
        token=token,
        args=ARGS,
    )
    provider = EvidenceProvider(error=TimeoutError("synthetic response lost"))
    executor = GmailSendExecutor(
        state_machine=machine,
        provider=provider,
        founder_user_ids={USER},
        connection_id=CONNECTION_ID,
        account_email=ACCOUNT,
        sender_address=ACCOUNT,
        deletion_blocked=lambda _user_id: False,
        now=lambda: NOW,
    )

    with pytest.raises(EffectUncertain):
        executor.execute(repository.record)
    with pytest.raises(EffectUncertain):
        executor.execute(repository.record)

    assert len(provider.calls) == 1
    assert repository.record["state"] == "UNCERTAIN"
    assert repository.record["uncertaintyReason"] == "provider-outcome-unproven"
