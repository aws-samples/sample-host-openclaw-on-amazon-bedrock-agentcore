from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import html
import http.client
import io
import itertools
import json
from pathlib import Path
import sys
import socket
from types import SimpleNamespace
import urllib.request
from urllib.parse import parse_qs, quote, urlencode, urlparse
import zipfile

import pytest

from tests.provider_test_support import BaseClient, boto3


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lambda"))

from control.index import ControlApplication
from control.invites import DynamoPilotInvites
from control.telegram_cards import (
    CardActionRejected,
    DynamoTelegramCardActions,
    ReadOnlyGmailDraftPreparer,
)
from control.test_invites import MemoryInviteTable
from router.telegram_ingress import TelegramWebhookIngress
from web.auth import OpaqueSessionManager, SignedConnectTickets
from web.index import WebApplication
from web.gmail_workspace import GmailWorkspaceService
from web.overview import DynamoConnectionLifecycle, PilotOverviewService
from web.retention import DeletionCoordinator, UserExporter
from worker.control_client import LambdaProductCommandHandler
from worker.index import WorkerDependencies, process_sqs_event
from workflows.gmail.models import Opportunity as GmailOpportunity, SourceEvidence
from workflows.gmail.oauth import (
    GMAIL_READONLY_SCOPE,
    GOOGLE_AUTHORIZATION_ENDPOINT,
    GoogleReadonlyOAuthFlow,
)
from workflows.gmail.repository import (
    ConnectionFenceError,
    DynamoGmailRepository,
    READONLY_PROVIDER,
)


ORIGIN = "https://operator.example"
NOW_SECONDS = 1_800_000_000
PARTICIPANTS = (701, 702, 703)


class ExternalCallSentinel:
    """Fail closed if the hermetic pilot journey reaches an external boundary."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def forbidden(self, label: str):
        def fail(*_args, **_kwargs):
            self.calls.append(label)
            raise AssertionError(f"external boundary reached: {label}")

        return fail


@pytest.fixture
def external_call_sentinel(monkeypatch):
    sentinel = ExternalCallSentinel()
    monkeypatch.setattr(socket, "create_connection", sentinel.forbidden("socket"))
    monkeypatch.setattr(socket, "getaddrinfo", sentinel.forbidden("dns"))
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        sentinel.forbidden("urllib"),
    )
    monkeypatch.setattr(
        http.client.HTTPConnection,
        "connect",
        sentinel.forbidden("http"),
    )
    monkeypatch.setattr(
        http.client.HTTPSConnection,
        "connect",
        sentinel.forbidden("https"),
    )

    import requests
    from control import composition as control_composition
    from router.runtime_driver import AgentCoreAdapter
    from web import composition as web_composition
    from worker import index as worker_index
    from worker.telegram_delivery import TelegramDeliveryAdapter
    from workflows.gmail.oauth import GoogleOAuthTokenClient
    from workflows.gmail.ranker import OpenAIResponsesAdapter
    from workflows.gmail.scanner import GoogleGmailApiClient

    monkeypatch.setattr(boto3, "client", sentinel.forbidden("boto3.client"))
    monkeypatch.setattr(boto3, "resource", sentinel.forbidden("boto3.resource"))
    monkeypatch.setattr(boto3, "Session", sentinel.forbidden("boto3.Session"))
    monkeypatch.setattr(
        boto3.session.Session,
        "client",
        sentinel.forbidden("boto3.Session.client"),
    )
    monkeypatch.setattr(
        boto3.session.Session,
        "resource",
        sentinel.forbidden("boto3.Session.resource"),
    )
    monkeypatch.setattr(
        requests.Session,
        "request",
        sentinel.forbidden("requests"),
    )
    monkeypatch.setattr(
        BaseClient,
        "_make_api_call",
        sentinel.forbidden("botocore.api"),
    )
    monkeypatch.setattr(
        GoogleOAuthTokenClient,
        "exchange_code",
        sentinel.forbidden("google.oauth.exchange"),
    )
    monkeypatch.setattr(
        GoogleOAuthTokenClient,
        "refresh",
        sentinel.forbidden("google.oauth.refresh"),
    )
    monkeypatch.setattr(
        GoogleGmailApiClient,
        "list_threads",
        sentinel.forbidden("gmail.list"),
    )
    monkeypatch.setattr(
        GoogleGmailApiClient,
        "get_thread",
        sentinel.forbidden("gmail.get"),
    )
    monkeypatch.setattr(
        OpenAIResponsesAdapter,
        "create",
        sentinel.forbidden("openai.responses"),
    )
    monkeypatch.setattr(
        TelegramDeliveryAdapter,
        "send_message",
        sentinel.forbidden("telegram.send"),
    )
    monkeypatch.setattr(
        TelegramDeliveryAdapter,
        "acknowledge_callback",
        sentinel.forbidden("telegram.ack"),
    )
    monkeypatch.setattr(
        AgentCoreAdapter,
        "invoke",
        sentinel.forbidden("agentcore.invoke"),
    )
    monkeypatch.setattr(
        control_composition,
        "build_production_application",
        sentinel.forbidden("control.composition"),
    )
    monkeypatch.setattr(
        web_composition,
        "build_production_application",
        sentinel.forbidden("web.composition"),
    )
    monkeypatch.setattr(
        worker_index,
        "_build_production_dependencies",
        sentinel.forbidden("worker.composition"),
    )

    # Canary three independent layers so a final empty call ledger is proof
    # that installed sentinels were live, rather than a never-mutated list.
    with pytest.raises(AssertionError):
        socket.create_connection(("example.invalid", 443))
    with pytest.raises(AssertionError):
        boto3.client("s3")
    with pytest.raises(AssertionError):
        GoogleOAuthTokenClient.exchange_code(object())
    assert sentinel.calls == ["socket", "boto3.client", "google.oauth.exchange"]
    sentinel.calls.clear()
    return sentinel


class DeterministicBytes:
    def __init__(self) -> None:
        self._counter = itertools.count(1)

    def __call__(self, size: int) -> bytes:
        seed = hashlib.sha512(
            f"synthetic-pilot-{next(self._counter)}".encode()
        ).digest()
        return (seed * ((size // len(seed)) + 1))[:size]


class InMemoryIdentityAndSessionStore:
    def __init__(self) -> None:
        self.tickets: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.revoked_users: set[str] = set()
        self.deletion_intents: dict[str, dict] = {}
        self.now_ms = NOW_SECONDS * 1_000

    def put_once(self, key, record, *, expires_at):
        if key in self.tickets:
            raise RuntimeError("synthetic ticket collision")
        self.tickets[key] = {**record, "expiresAt": expires_at}

    def pop_once(self, key):
        return self.tickets.pop(key, None)

    def create(self, key, record, *, expires_at):
        if key in self.sessions:
            raise RuntimeError("synthetic session collision")
        self.sessions[key] = {**record, "expiresAt": expires_at}

    def get(self, key):
        record = self.sessions.get(key)
        if record is None:
            return None
        return {
            **record,
            "revoked": record.get("revoked") is True
            or record.get("userId") in self.revoked_users,
        }

    def revoke(self, key):
        if key in self.sessions:
            self.sessions[key]["revoked"] = True

    def revoke_all(self, user_id):
        self.revoked_users.add(user_id)
        for record in self.sessions.values():
            if record.get("userId") == user_id:
                record["revoked"] = True

    def begin_deletion(self, user_id):
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
        value = self.deletion_intents.get(user_id)
        return dict(value) if value is not None else None

    def mark_deletion_finalizing(self, user_id):
        record = self.deletion_intents[user_id]
        record["deletionStatus"] = "FINALIZING"
        record["finalizingAt"] = self.now_ms
        return dict(record)

    def complete_deletion(self, user_id, *, finalizing_before_ms):
        record = self.deletion_intents[user_id]
        assert record["finalizingAt"] <= finalizing_before_ms
        completed = {
            "userId": user_id,
            "purgeReason": "ACCOUNT_DELETION",
            "deletionStatus": "COMPLETED",
            "requestedAt": None,
            "finalizingAt": None,
            "completedAt": self.now_ms,
        }
        self.deletion_intents[user_id] = completed
        return dict(completed)


class LocalFifoQueue:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.pending: list[dict] = []

    def send_message(self, **request):
        self.calls.append(dict(request))
        self.pending.append(dict(request))
        return {
            "MessageId": f"synthetic-{len(self.calls)}",
            "SequenceNumber": str(len(self.calls)),
        }

    def process_next(self, dependencies: WorkerDependencies):
        request = self.pending.pop(0)
        record = {
            "messageId": f"local-fifo-{len(self.calls) - len(self.pending)}",
            "receiptHandle": "local-only",
            "body": request["MessageBody"],
            "attributes": {
                "MessageGroupId": request["MessageGroupId"],
                "MessageDeduplicationId": request["MessageDeduplicationId"],
            },
            "messageAttributes": {},
            "eventSource": "aws:sqs",
        }
        return process_sqs_event({"Records": [record]}, dependencies)


@dataclass
class LocalLedgerRecord:
    request_sha256: str
    state: str = "PROCESSING"
    result: str | None = None


@dataclass
class LocalLedgerClaim:
    key: str
    state: str
    result: str | None = None


class LocalLedger:
    """Deterministic local implementation of the worker claim/outbox port."""

    def __init__(self) -> None:
        self.records: dict[str, LocalLedgerRecord] = {}

    def claim_processing(self, envelope, *, owner):
        del owner
        key = envelope.message_deduplication_id
        record = self.records.get(key)
        if record is None:
            self.records[key] = LocalLedgerRecord(envelope.request_sha256)
            return LocalLedgerClaim(key, "CLAIMED")
        if record.request_sha256 != envelope.request_sha256:
            raise RuntimeError("local event identity collision")
        return LocalLedgerClaim(key, record.state, record.result)

    def complete_result(self, claim, result):
        record = self.records[claim.key]
        if record.state != "PROCESSING":
            raise RuntimeError("local result claim was lost")
        record.state = "RESULT_READY"
        record.result = result
        return LocalLedgerClaim(claim.key, record.state, result)

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
            return LocalLedgerClaim(claim.key, "DELIVERY_CLAIMED", record.result)
        return LocalLedgerClaim(claim.key, record.state, record.result)

    def confirm_delivery(self, claim, receipt):
        if not receipt.get("providerMessageId"):
            raise RuntimeError("local delivery returned no receipt")
        self.records[claim.key].state = "DELIVERED"

    def mark_delivery_uncertain(self, claim, *, error_type):
        del error_type
        record = self.records[claim.key]
        if record.state == "DELIVERY_IN_FLIGHT":
            record.state = "DELIVERY_UNCERTAIN"


class LocalTelegramOutbox:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.acknowledgements: list[str] = []

    def acknowledge_callback(self, *, callback_query_id):
        self.acknowledgements.append(callback_query_id)

    def send_message(self, **request):
        self.calls.append(dict(request))
        return {"providerMessageId": f"local-telegram-{len(self.calls)}"}


class LocalControlLambda:
    def __init__(self, control: ControlApplication) -> None:
        self._control = control
        self.calls: list[dict] = []

    def invoke(self, **request):
        self.calls.append(dict(request))
        payload = json.loads(request["Payload"])
        try:
            result = self._control.handle(payload)
        except Exception as error:
            return {
                "StatusCode": 200,
                "FunctionError": "Unhandled",
                "Payload": io.BytesIO(
                    json.dumps({"errorType": type(error).__name__}).encode()
                ),
            }
        return {
            "StatusCode": 200,
            "Payload": io.BytesIO(
                json.dumps(result, separators=(",", ":")).encode()
            ),
        }


class ForbiddenRuntime:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def invoke(self, *_args, **_kwargs):
        self.calls.append((_args, _kwargs))
        raise AssertionError("product commands must not invoke the runtime")


class LocalRuntimeDeletionFence:
    def __init__(self, store: InMemoryIdentityAndSessionStore) -> None:
        self._store = store

    def is_account_deleted(self, user_id):
        record = self._store.get_deletion_intent(user_id)
        return bool(record and record.get("deletionStatus") == "COMPLETED")


@dataclass(frozen=True)
class Source:
    deep_link: str


@dataclass(frozen=True)
class Opportunity:
    id: str
    user_id: str
    title: str
    reason: str
    source: Source


class SyntheticPilotState:
    def __init__(self) -> None:
        self.connections: dict[str, str] = {}
        self.opportunities: dict[str, list[Opportunity]] = {}
        self.drafts: dict[str, list[dict]] = {}
        self.scans: dict[str, dict] = {}
        self.purge_calls: list[tuple[str, str]] = []

    def ensure_user(self, user_id: str) -> None:
        self.connections.setdefault(user_id, "DISCONNECTED")
        digest = hashlib.sha256(user_id.encode()).hexdigest()
        self.opportunities.setdefault(
            user_id,
            [
                Opportunity(
                    id=f"opp_{digest[:16]}",
                    user_id=user_id,
                    title=f"Synthetic follow-up {digest[:8]}",
                    reason="A source-backed thread has no reply in the pilot window.",
                    source=Source(
                        f"https://mail.google.test/mail/u/0/#inbox/{digest[:24]}"
                    ),
                )
            ],
        )
        self.drafts.setdefault(user_id, [])


class SyntheticConnections:
    def __init__(self, state: SyntheticPilotState) -> None:
        self._state = state
        self._purge_callbacks: list[object] = []

    def add_purge_callback(self, callback) -> None:
        self._purge_callbacks.append(callback)

    def status(self, user_id):
        self._state.ensure_user(user_id)
        return self._state.connections[user_id]

    def disconnect(self, user_id):
        self._state.ensure_user(user_id)
        self._state.connections[user_id] = "DISCONNECTED"
        self._state.opportunities[user_id] = []
        self._state.drafts[user_id] = []
        for callback in self._purge_callbacks:
            callback(user_id)
        return "DISCONNECTED"

    def revoke_all(self, user_id):
        self.disconnect(user_id)
        self._state.purge_calls.append(("connections", user_id))


class SyntheticOAuth:
    def __init__(self, state: SyntheticPilotState) -> None:
        self._state = state
        self._states: dict[str, str] = {}
        self.calls: list[tuple] = []

    def start(self, *, user_id, redirect_uri):
        self._state.ensure_user(user_id)
        state = f"state-{hashlib.sha256(user_id.encode()).hexdigest()[:24]}"
        self._states[user_id] = state
        self.calls.append(("start", user_id, redirect_uri))
        return SimpleNamespace(
            url=f"https://accounts.google.test/authorize?state={quote(state)}"
        )

    def complete(self, *, user_id, state, code):
        if self._states.get(user_id) != state or code != "synthetic-code":
            raise PermissionError("synthetic OAuth binding mismatch")
        self._state.connections[user_id] = "CONNECTED"
        self.calls.append(("complete", user_id))


class SyntheticGmail:
    def __init__(self, state: SyntheticPilotState) -> None:
        self._state = state

    def scan(self, *, user_id):
        self._state.ensure_user(user_id)
        if self._state.connections[user_id] != "CONNECTED":
            raise PermissionError("read-only connection is unavailable")
        return list(self._state.opportunities[user_id])


class SyntheticMeasurements:
    def __init__(self, state: SyntheticPilotState) -> None:
        self._state = state

    @staticmethod
    def _scan_id(user_id: str) -> str:
        suffix = base64.urlsafe_b64encode(
            hashlib.sha256(user_id.encode()).digest()[:24]
        ).decode().rstrip("=")
        return f"scan_{NOW_SECONDS:020d}_{suffix}"

    def start(self, user_id):
        scan_id = self._scan_id(user_id)
        self._state.scans[user_id] = {
            "scanId": scan_id,
            "status": "RUNNING",
            "startedAt": NOW_SECONDS,
            "completedAt": None,
            "resultCount": None,
            "failureCode": None,
            "feedback": None,
        }
        return scan_id

    def complete(self, user_id, scan_id, *, result_count):
        record = self._bound(user_id, scan_id)
        record.update(
            status="EMPTY" if result_count == 0 else "SUCCEEDED",
            completedAt=NOW_SECONDS + 1,
            resultCount=result_count,
        )
        return dict(record)

    def fail(self, user_id, scan_id, *, failure_code):
        record = self._bound(user_id, scan_id)
        record.update(
            status="FAILED",
            completedAt=NOW_SECONDS + 1,
            failureCode=failure_code,
        )
        return dict(record)

    def latest(self, user_id):
        record = self._state.scans.get(user_id)
        return dict(record) if record is not None else None

    def feedback(self, user_id, scan_id, *, response):
        record = self._bound(user_id, scan_id)
        if record["status"] not in {"SUCCEEDED", "EMPTY"}:
            raise ValueError("synthetic scan is not complete")
        if record["feedback"] not in {None, response}:
            raise ValueError("synthetic feedback is already recorded")
        record["feedback"] = response
        return dict(record)

    def delete_user_records(self, user_id):
        self._state.scans.pop(user_id, None)
        self._state.purge_calls.append(("measurements", user_id))

    def _bound(self, user_id, scan_id):
        record = self._state.scans.get(user_id)
        if record is None or record["scanId"] != scan_id:
            raise PermissionError("synthetic scan belongs to another user")
        return record


class SyntheticCard:
    def __init__(self, opportunity: Opportunity, buttons: list[dict]) -> None:
        self._opportunity = opportunity
        self._buttons = buttons

    def to_control(self):
        return {
            "title": self._opportunity.title,
            "reason": self._opportunity.reason,
            "sourceUrl": self._opportunity.source.deep_link,
            "buttons": list(self._buttons),
        }


class SyntheticCardActions:
    def __init__(self) -> None:
        self._records: dict[str, tuple[str, str, str, str, Opportunity]] = {}

    def issue(self, *, user_id, chat_id, actor_id, opportunities):
        cards = []
        for opportunity in opportunities:
            buttons = []
            for action in ("edit", "prepare", "skip", "why"):
                digest = base64.urlsafe_b64encode(
                    hashlib.sha256(
                        f"{user_id}:{opportunity.id}:{action}".encode()
                    ).digest()[:18]
                ).decode().rstrip("=")
                callback = f"poc1:{action}:{digest}"
                buttons.append({"text": action.title(), "callbackData": callback})
                self._records[callback] = (
                    user_id,
                    chat_id,
                    actor_id,
                    action,
                    opportunity,
                )
            cards.append(SyntheticCard(opportunity, buttons))
        return cards

    def consume(self, *, user_id, chat_id, actor_id, callback_data):
        record = self._records.get(callback_data)
        if record is None or record[:3] != (user_id, chat_id, actor_id):
            raise CardActionRejected("synthetic card binding mismatch")
        self._records.pop(callback_data)
        return SimpleNamespace(action=record[3], opportunity=record[4])

    def purge_user(self, user_id):
        self._records = {
            callback: record
            for callback, record in self._records.items()
            if record[0] != user_id
        }


class SyntheticGmailWorkspace:
    def __init__(self, state: SyntheticPilotState) -> None:
        self._state = state

    def get(self, user_id):
        self._state.ensure_user(user_id)
        return {
            "userId": user_id,
            "opportunities": [
                {
                    "id": item.id,
                    "title": item.title,
                    "reason": item.reason,
                    "sourceUrl": item.source.deep_link,
                }
                for item in self._state.opportunities[user_id]
            ],
            "drafts": [dict(item) for item in self._state.drafts[user_id]],
        }

    def edit_draft(self, *, user_id, action_id, revision, subject, body):
        drafts = self._state.drafts.get(user_id, [])
        match = next(
            (item for item in drafts if item["actionId"] == action_id),
            None,
        )
        if match is None or match["revision"] != revision:
            raise PermissionError("synthetic draft binding mismatch")
        match.update(
            revision=revision + 1,
            subject=subject,
            body=body,
            payloadHash=hashlib.sha256(f"{subject}\0{body}".encode()).hexdigest(),
        )
        return {"draft": dict(match)}


class SyntheticDraftPreparer:
    def __init__(self, state: SyntheticPilotState) -> None:
        self._state = state

    def prepare(self, *, user_id, opportunity):
        if opportunity.user_id != user_id:
            raise PermissionError("synthetic opportunity belongs to another user")
        action_id = "draft_" + hashlib.sha256(
            f"{user_id}:{opportunity.id}".encode()
        ).hexdigest()[:20]
        drafts = self._state.drafts[user_id]
        existing = next(
            (item for item in drafts if item["actionId"] == action_id),
            None,
        )
        if existing is None:
            drafts.append(
                {
                    "actionId": action_id,
                    "revision": 1,
                    "subject": "",
                    "body": "",
                    "payloadHash": hashlib.sha256(b"").hexdigest(),
                }
            )
        return SimpleNamespace(action_id=action_id, revision=1)


class SyntheticWorkspace:
    def __init__(self, state: SyntheticPilotState) -> None:
        self._state = state

    def get(self, user_id):
        self._state.ensure_user(user_id)
        digest = hashlib.sha256(user_id.encode()).hexdigest()
        return {
            "userId": user_id,
            "runtimeState": "IDLE",
            "workspaceReceipt": {
                "generation": f"gen_{digest[:16]}",
                "manifestSha256": digest,
            },
            "files": [{"path": "memory.md", "size": len(user_id)}],
        }

    def delete_namespace(self, user_id):
        self._state.drafts.pop(user_id, None)
        self._state.opportunities.pop(user_id, None)
        self._state.purge_calls.append(("workspace", user_id))


class SyntheticExportSource:
    def records_for_user(self, user_id):
        return {
            "memory": [{"text": f"private synthetic memory for {user_id}"}],
            "receipts": [],
            "schedules": [],
        }

    def workspace_files(self, user_id):
        return {"memory.md": f"workspace:{user_id}".encode()}


class SyntheticRuntime:
    def __init__(self, state: SyntheticPilotState) -> None:
        self._state = state

    def purge(self, user_id):
        self._state.purge_calls.append(("runtime", user_id))
        return {
            "userId": user_id,
            "state": "DELETING",
            "purgeReason": "ACCOUNT_DELETION",
            "purgeCompletedAt": NOW_SECONDS,
        }


class SyntheticPurgePort:
    def __init__(self, state: SyntheticPilotState, name: str) -> None:
        self._state = state
        self._name = name

    def delete_user_records(self, user_id):
        self._state.purge_calls.append((self._name, user_id))


class NoTasks:
    def list_open(self, _user_id):
        return []


class NoApprovals:
    def preview(self, **_request):
        raise AssertionError("external pilots have no approval controls")

    approve = preview
    reject = preview


class NoRetention:
    def sweep(self):
        raise AssertionError("synthetic journey does not run retention")


class LocalConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class LocalDynamoTable:
    """Small Dynamo-compatible store for real Gmail local adapters only."""

    name = "local-gmail-table"

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        self.meta = SimpleNamespace(client=self)

    @staticmethod
    def _key(value):
        return value["PK"], value["SK"]

    def get_item(self, **request):
        item = self.items.get(self._key(request["Key"]))
        return {"Item": deepcopy(item)} if item is not None else {}

    def put_item(self, **request):
        item = deepcopy(request["Item"])
        key = self._key(item)
        if request.get("ConditionExpression") and key in self.items:
            raise LocalConditionalFailure("conditional put")
        self.items[key] = item
        return {}

    def delete_item(self, **request):
        key = self._key(request["Key"])
        item = self.items.get(key)
        expected = request.get("ExpressionAttributeValues", {}).get(":generation")
        if expected is not None and (
            item is None or item.get("connectionGeneration") != expected
        ):
            raise LocalConditionalFailure("conditional delete")
        old = self.items.pop(key, None)
        return {"Attributes": deepcopy(old)} if old is not None else {}

    def update_item(self, **request):
        key = self._key(request["Key"])
        item = self.items.get(key)
        values = request["ExpressionAttributeValues"]
        if ":expected" in values:
            expected_statuses = {
                value
                for name, value in values.items()
                if name.startswith(":expectedStatus")
            }
            if (
                item is None
                or item.get("generation") != values[":expected"]
                or (
                    expected_statuses
                    and item.get("status") not in expected_statuses
                )
            ):
                raise LocalConditionalFailure("stale fence")
            item.update(
                generation=values[":next"],
                status=values[":status"],
                updatedAt=values[":now"],
            )
            return {}
        if ":recordType" in values and values[":recordType"] == "TELEGRAM_CARD_ACTION":
            if (
                item is None
                or item.get("userId") != values[":userId"]
                or item.get("chatId") != values[":chatId"]
                or item.get("actorId") != values[":actorId"]
                or item.get("action") != values[":action"]
                or item.get("ttl", 0) <= values[":now"]
                or "consumedAt" in item
            ):
                raise LocalConditionalFailure("card unavailable")
            item["consumedAt"] = values[":now"]
            return {"Attributes": deepcopy(item)}
        raise AssertionError(f"unsupported local update: {request}")

    def query(self, **request):
        values = request["ExpressionAttributeValues"]
        pk = values[":pk"]
        prefix = values.get(":prefix", values.get(":sk"))
        matches = sorted(
            (
                deepcopy(item)
                for (item_pk, item_sk), item in self.items.items()
                if item_pk == pk and item_sk.startswith(prefix)
            ),
            key=lambda item: item["SK"],
            reverse=request.get("ScanIndexForward") is False,
        )
        start = request.get("ExclusiveStartKey")
        if start is not None:
            start_key = self._key(start)
            keys = [self._key(item) for item in matches]
            if start_key in keys:
                matches = matches[keys.index(start_key) + 1 :]
            else:
                matches = [
                    item
                    for item in matches
                    if (
                        item["SK"] < start["SK"]
                        if request.get("ScanIndexForward") is False
                        else item["SK"] > start["SK"]
                    )
                ]
        limit = request.get("Limit", len(matches))
        page = matches[:limit]
        response = {"Items": page}
        if len(matches) > limit:
            response["LastEvaluatedKey"] = {
                "PK": page[-1]["PK"],
                "SK": page[-1]["SK"],
            }
        return response

    @classmethod
    def _decode_value(cls, value):
        if "S" in value:
            return value["S"]
        if "N" in value:
            number = Decimal(value["N"])
            return int(number) if number == number.to_integral_value() else number
        if "BOOL" in value:
            return value["BOOL"]
        if "NULL" in value:
            return None
        if "M" in value:
            return {
                name: cls._decode_value(field)
                for name, field in value["M"].items()
            }
        if "L" in value:
            return [cls._decode_value(field) for field in value["L"]]
        raise AssertionError(f"unsupported local Dynamo value: {value!r}")

    @classmethod
    def _decode_item(cls, item):
        return {name: cls._decode_value(field) for name, field in item.items()}

    def transact_write_items(self, **request):
        pending = deepcopy(self.items)
        for operation in request["TransactItems"]:
            if "ConditionCheck" in operation:
                check = operation["ConditionCheck"]
                key = self._key(self._decode_item(check["Key"]))
                item = pending.get(key)
                expression = check["ConditionExpression"]
                values = {
                    name: self._decode_value(value)
                    for name, value in check.get(
                        "ExpressionAttributeValues", {}
                    ).items()
                }
                if expression.startswith("attribute_not_exists"):
                    valid = item is None
                elif expression == "connectionGeneration=:generation":
                    valid = (
                        item is not None
                        and item.get("connectionGeneration")
                        == values[":generation"]
                    )
                elif expression == "generation=:generation AND #status=:status":
                    valid = (
                        item is not None
                        and item.get("generation") == values[":generation"]
                        and item.get("status") == values[":status"]
                    )
                else:
                    valid = (
                        item is not None
                        and item.get("generation") == values[":generation"]
                        and item.get("status")
                        in {
                            value
                            for name, value in values.items()
                            if name.startswith(":status")
                        }
                    )
                if not valid:
                    raise LocalConditionalFailure("transaction condition")
            elif "Put" in operation:
                put = operation["Put"]
                item = self._decode_item(put["Item"])
                key = self._key(item)
                if put.get("ConditionExpression") and key in pending:
                    raise LocalConditionalFailure("transaction put")
                pending[key] = item
            elif "Update" in operation:
                update = operation["Update"]
                key = self._key(self._decode_item(update["Key"]))
                item = pending.get(key)
                values = {
                    name: self._decode_value(value)
                    for name, value in update[
                        "ExpressionAttributeValues"
                    ].items()
                }
                if (
                    item is None
                    or item.get("generation") != values[":generation"]
                    or item.get("status")
                    not in {values[":disconnected"], values[":connected"]}
                ):
                    raise LocalConditionalFailure("transaction update")
                item["status"] = values[":connected"]
                item["updatedAt"] = values[":now"]
            elif "Delete" in operation:
                key = self._key(self._decode_item(operation["Delete"]["Key"]))
                pending.pop(key, None)
            else:
                raise AssertionError(f"unsupported local transaction: {operation!r}")
        self.items = pending
        return {}


def _web_event(method, path, *, cookie=None, csrf=None, body=None, query=None):
    headers = {"origin": ORIGIN}
    if cookie is not None:
        headers["cookie"] = cookie
    if csrf is not None:
        headers["x-po-csrf"] = csrf
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "rawPath": path,
        "rawQueryString": urlencode(query or {}),
        "headers": headers,
        "body": json.dumps(body) if body is not None else "",
        "isBase64Encoded": False,
    }


def test_real_local_gmail_adapters_purge_and_fence_disconnect(
    external_call_sentinel,
) -> None:
    now = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
    table = LocalDynamoTable()
    repository = DynamoGmailRepository(
        table,
        conditional_failure_types=(LocalConditionalFailure,),
        now=lambda: now,
    )
    user_id = "pilot_real"
    envelope = {
        "format": "personal-operator.oauth-envelope.v1",
        "binding": "b" * 64,
        "wrappedKey": "synthetic-wrapped-key",
        "nonce": "synthetic-nonce",
        "ciphertext": "synthetic-ciphertext",
    }
    repository.put(
        user_id=user_id,
        provider=READONLY_PROVIDER,
        record=envelope,
        expected_generation=0,
        allow_disconnected=True,
    )
    repository.activate_connection(user_id, 0)
    waiting = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)
    source = SourceEvidence(
        source_id="gmail:thread_real:message_real",
        thread_id="thread_real",
        deep_link=(
            "https://mail.google.com/mail/u/0/#inbox/thread_real"
        ),
        correspondent="ada@example.net",
        subject="Pilot follow-up",
        excerpt="A bounded local-only excerpt.",
        waiting_since=waiting,
    )
    opportunity = GmailOpportunity(
        id="opp_real_12345678",
        user_id=user_id,
        source=source,
        waiting_since=waiting,
        title="Reply to Ada",
        reason="Ada is waiting for a reply.",
        confidence=0.9,
    )
    repository.replace_opportunities(
        user_id=user_id,
        records=[
            {
                "id": opportunity.id,
                "userId": user_id,
                "source": {
                    "sourceId": source.source_id,
                    "threadId": source.thread_id,
                    "deepLink": source.deep_link,
                    "correspondent": source.correspondent,
                    "subject": source.subject,
                    "excerpt": source.excerpt,
                },
                "waitingSince": waiting.isoformat(),
                "title": opportunity.title,
                "reason": opportunity.reason,
                "confidence": opportunity.confidence,
            }
        ],
        expires_at=int(now.timestamp()) + 14 * 24 * 60 * 60,
        expected_generation=0,
    )
    tokens = iter(
        (
            "AAAAAAAAAAAAAAAAAAAAAA",
            "BBBBBBBBBBBBBBBBBBBBBB",
            "CCCCCCCCCCCCCCCCCCCCCC",
            "DDDDDDDDDDDDDDDDDDDDDD",
        )
    )
    cards = DynamoTelegramCardActions(
        table,
        now=lambda: now,
        token_factory=lambda: next(tokens),
        conditional_failure_types=(LocalConditionalFailure,),
        connection_fence=repository,
    )
    issued = cards.issue(
        user_id=user_id,
        chat_id="701",
        actor_id="telegram:701",
        opportunities=[opportunity],
    )
    stale_callback = issued[0].to_control()["buttons"][3]["callbackData"]
    ReadOnlyGmailDraftPreparer(repository, now=lambda: now).prepare(
        user_id=user_id,
        opportunity=opportunity,
        connection_generation=0,
    )
    workspace = GmailWorkspaceService(
        table,
        repository=repository,
        enforce_connection_fence=True,
        now=lambda: now,
    )
    assert len(workspace.get(user_id)["opportunities"]) == 1
    assert len(workspace.get(user_id)["drafts"]) == 1

    class LocalTokenClient:
        def __init__(self):
            self.calls = []

        def exchange_code(self, **request):
            self.calls.append(request)
            return {
                "access_token": "local-access",
                "refresh_token": "local-refresh",
                "scope": GMAIL_READONLY_SCOPE,
            }

    class LocalVault:
        def __init__(self):
            self.calls = []

        def save(self, **request):
            self.calls.append(request)

    token_client = LocalTokenClient()
    local_vault = LocalVault()
    oauth = GoogleReadonlyOAuthFlow(
        state_store=repository,
        token_client=token_client,
        token_vault=local_vault,
        connection_fence=repository,
        client_id="local-client",
        authorization_endpoint=GOOGLE_AUTHORIZATION_ENDPOINT,
        allowed_redirect_uris={"https://operator.example/oauth/google/callback"},
        now=lambda: now,
        random_bytes=lambda size: bytes(range(size)),
    )
    stale_authorization = oauth.start(
        user_id=user_id,
        redirect_uri="https://operator.example/oauth/google/callback",
    )

    lifecycle = DynamoConnectionLifecycle(table, repository=repository)
    assert lifecycle.disconnect(user_id) == "DISCONNECTED"

    assert workspace.get(user_id) == {
        "userId": user_id,
        "opportunities": [],
        "drafts": [],
    }
    assert {
        sk
        for (pk, sk) in table.items
        if pk == f"USER#{user_id}"
    } == {"GMAIL#CONNECTION_FENCE"}
    with pytest.raises(CardActionRejected):
        cards.consume(
            user_id=user_id,
            chat_id="701",
            actor_id="telegram:701",
            callback_data=stale_callback,
        )
    with pytest.raises(ConnectionFenceError):
        oauth.complete(
            user_id=user_id,
            state=stale_authorization.state,
            code="stale-code",
        )
    assert token_client.calls == []
    assert local_vault.calls == []
    with pytest.raises(ConnectionFenceError):
        repository.replace_opportunities(
            user_id=user_id,
            records=[],
            expires_at=int(now.timestamp()) + 14 * 24 * 60 * 60,
            expected_generation=0,
        )

    repeated_disconnect_state = oauth.start(
        user_id=user_id,
        redirect_uri="https://operator.example/oauth/google/callback",
    )
    assert lifecycle.disconnect(user_id) == "DISCONNECTED"
    fence = table.items[(f"USER#{user_id}", "GMAIL#CONNECTION_FENCE")]
    assert fence["generation"] == 2
    assert fence["status"] == "DISCONNECTED"
    with pytest.raises(ConnectionFenceError):
        oauth.complete(
            user_id=user_id,
            state=repeated_disconnect_state.state,
            code="same-generation-race",
        )
    with pytest.raises(ConnectionFenceError):
        repository.put(
            user_id=user_id,
            provider=READONLY_PROVIDER,
            record=envelope,
            expected_generation=1,
            allow_disconnected=True,
        )
    assert {
        sk
        for (pk, sk) in table.items
        if pk == f"USER#{user_id}"
    } == {"GMAIL#CONNECTION_FENCE"}

    assert external_call_sentinel.calls == []


def test_three_isolated_pilots_complete_provider_free_read_only_journey(
    external_call_sentinel,
) -> None:
    random = DeterministicBytes()
    invite_table = MemoryInviteTable()
    invites = DynamoPilotInvites(
        invite_table,
        now=lambda: NOW_SECONDS,
        random_bytes=random,
    )
    users_by_actor: dict[int, str] = {}
    queue = LocalFifoQueue()
    ingress = TelegramWebhookIngress(
        secret_provider=lambda: "synthetic-webhook-secret",
        resolve_user=lambda _channel, actor, _name: (
            (users_by_actor[int(actor)], False)
            if int(actor) in users_by_actor
            else (None, False)
        ),
        redeem_invite=lambda token, channel, actor, name: invites.redeem(
            token,
            channel=channel,
            channel_user_id=actor,
            display_name=name,
        ).user_id,
        sqs_client=queue,
        queue_url="https://sqs.eu-west-1.amazonaws.com/1/synthetic.fifo",
    )

    store = InMemoryIdentityAndSessionStore()
    tickets = SignedConnectTickets(
        secret=b"synthetic-ticket-signing-key-32b",
        store=store,
        now=lambda: NOW_SECONDS,
        random_bytes=random,
    )
    sessions = OpaqueSessionManager(
        secret=b"synthetic-session-signing-key-32",
        store=store,
        now=lambda: NOW_SECONDS,
        random_bytes=random,
    )
    state = SyntheticPilotState()
    connections = SyntheticConnections(state)
    oauth = SyntheticOAuth(state)
    measurements = SyntheticMeasurements(state)
    gmail_workspace = SyntheticGmailWorkspace(state)
    workspace = SyntheticWorkspace(state)
    card_actions = SyntheticCardActions()
    connections.add_purge_callback(card_actions.purge_user)
    control = ControlApplication(
        tickets=tickets,
        gmail=SyntheticGmail(state),
        tasks=NoTasks(),
        deletion_intents=store,
        web_origin=ORIGIN,
        card_actions=card_actions,
        draft_preparer=SyntheticDraftPreparer(state),
        scan_measurements=measurements,
    )
    overview = PilotOverviewService(
        connections=connections,
        workspace=workspace,
        gmail_workspace=gmail_workspace,
        scans=measurements,
    )
    deletion = DeletionCoordinator(
        session_store=store,
        connection_store=connections,
        runtime_driver=SyntheticRuntime(state),
        workspace_store=workspace,
        record_store=measurements,
        footprint_store=SyntheticPurgePort(state, "footprint"),
        clock_ms=lambda: store.now_ms,
    )
    web = WebApplication(
        tickets=tickets,
        sessions=sessions,
        oauth=oauth,
        approvals=NoApprovals(),
        workspace=workspace,
        gmail_workspace=gmail_workspace,
        exporter=UserExporter(SyntheticExportSource()),
        deletion=deletion,
        retention=NoRetention(),
        overview=overview,
        connections=connections,
        scans=measurements,
        web_origin=ORIGIN,
        google_redirect_uri=f"{ORIGIN}/oauth/google/callback",
    )
    local_lambda = LocalControlLambda(control)
    command_handler = LambdaProductCommandHandler(
        local_lambda,
        function_name="personal-operator-control-command:local",
    )
    outbox = LocalTelegramOutbox()
    runtime = ForbiddenRuntime()
    worker_dependencies = WorkerDependencies(
        runtime_driver=runtime,
        command_handler=command_handler,
        telegram_delivery=outbox,
        ledger=LocalLedger(),
        control_deletion_fence=command_handler,
        deletion_fence=LocalRuntimeDeletionFence(store),
    )

    invite_tokens: list[str] = []
    sessions_by_user: dict[str, tuple[str, str]] = {}
    source_by_user: dict[str, str] = {}
    callback_by_user: dict[str, str] = {}
    draft_by_user: dict[str, str] = {}
    scan_by_user: dict[str, str] = {}

    for offset, actor in enumerate(PARTICIPANTS, 1):
        issued = invites.issue()
        invite_tokens.append(issued.token)
        update = {
            "update_id": 1_000 + offset,
            "message": {
                "message_id": 10 + offset,
                "chat": {"id": actor, "type": "private"},
                "from": {"id": actor, "first_name": f"Pilot {offset}"},
                "text": f"/start {issued.token}",
            },
        }
        accepted = ingress.handle(
            json.dumps(update),
            {"x-telegram-bot-api-secret-token": "synthetic-webhook-secret"},
        )
        assert accepted == {"statusCode": 200, "body": "ok"}
        wire = json.loads(queue.calls[-1]["MessageBody"])
        assert wire["payload"]["command"] == "/start"
        assert issued.token not in queue.calls[-1]["MessageBody"]
        user_id = wire["userId"]
        users_by_actor[actor] = user_id
        state.ensure_user(user_id)

        processed = queue.process_next(worker_dependencies)
        assert processed == {"batchItemFailures": []}
        welcome_text = html.unescape(outbox.calls[-1]["html"])
        link = next(
            line for line in welcome_text.splitlines() if line.startswith(ORIGIN)
        )
        ticket = parse_qs(urlparse(link).query)["ticket"][0]
        connected = web.handle(
            _web_event("POST", "/api/session/connect", body={"ticket": ticket})
        )
        assert connected["statusCode"] == 201
        session_body = json.loads(connected["body"])
        assert session_body["returnPath"] == "/connections"
        cookie = connected["headers"]["Set-Cookie"]
        csrf = session_body["csrfToken"]

        authorization = web.handle(
            _web_event("GET", "/oauth/google/start", cookie=cookie)
        )
        assert authorization["statusCode"] == 302
        oauth_state = parse_qs(
            urlparse(authorization["headers"]["Location"]).query
        )["state"][0]
        completed = web.handle(
            _web_event(
                "GET",
                "/oauth/google/callback",
                cookie=cookie,
                query={"state": oauth_state, "code": "synthetic-code"},
            )
        )
        assert completed["statusCode"] == 302

        scan_update = {
            "update_id": 2_000 + offset,
            "message": {
                "message_id": 20 + offset,
                "chat": {"id": actor, "type": "private"},
                "from": {"id": actor, "first_name": f"Pilot {offset}"},
                "text": "/scan",
            },
        }
        assert ingress.handle(
            json.dumps(scan_update),
            {"x-telegram-bot-api-secret-token": "synthetic-webhook-secret"},
        ) == {"statusCode": 200, "body": "ok"}
        processed = queue.process_next(worker_dependencies)
        assert processed == {"batchItemFailures": []}
        scan_delivery = outbox.calls[-1]
        scan_text = html.unescape(scan_delivery["html"])
        assert "No button sends email" in scan_text
        source = state.opportunities[user_id][0].source.deep_link
        source_by_user[user_id] = source
        assert source in scan_text
        keyboard = scan_delivery["reply_markup"]["inline_keyboard"][0]
        edit_callback = keyboard[0]["callback_data"]
        callback_by_user[user_id] = keyboard[3]["callback_data"]
        callback_update = {
            "update_id": 3_000 + offset,
            "callback_query": {
                "id": f"synthetic_callback_{offset}",
                "from": {"id": actor, "first_name": f"Pilot {offset}"},
                "message": {
                    "message_id": 30 + offset,
                    "chat": {"id": actor, "type": "private"},
                },
                "data": edit_callback,
            },
        }
        assert ingress.handle(
            json.dumps(callback_update),
            {"x-telegram-bot-api-secret-token": "synthetic-webhook-secret"},
        ) == {"statusCode": 200, "body": "ok"}
        processed = queue.process_next(worker_dependencies)
        assert processed == {"batchItemFailures": []}
        edit_text = html.unescape(outbox.calls[-1]["html"])
        assert "Nothing was sent" in edit_text
        assert f"synthetic_callback_{offset}" in outbox.acknowledgements
        draft_link = next(
            line for line in edit_text.splitlines() if line.startswith(ORIGIN)
        )
        draft_ticket = parse_qs(urlparse(draft_link).query)["ticket"][0]
        draft_session = web.handle(
            _web_event(
                "POST",
                "/api/session/connect",
                cookie=cookie,
                body={"ticket": draft_ticket},
            )
        )
        assert draft_session["statusCode"] == 201
        draft_session_body = json.loads(draft_session["body"])
        assert draft_session_body["returnPath"].startswith("/workspace?draft=")
        cookie = draft_session["headers"]["Set-Cookie"]
        csrf = draft_session_body["csrfToken"]

        gmail = web.handle(_web_event("GET", "/api/gmail", cookie=cookie))
        assert gmail["statusCode"] == 200
        gmail_body = json.loads(gmail["body"])
        assert gmail_body["userId"] == user_id
        assert [item["sourceUrl"] for item in gmail_body["opportunities"]] == [
            source
        ]
        draft = gmail_body["drafts"][0]
        draft_by_user[user_id] = draft["actionId"]
        edited = web.handle(
            _web_event(
                "POST",
                f"/api/gmail/drafts/{draft['actionId']}",
                cookie=cookie,
                csrf=csrf,
                body={
                    "revision": 1,
                    "subject": f"Synthetic reply {offset}",
                    "body": "A private local-only draft.",
                },
            )
        )
        assert edited["statusCode"] == 200
        assert json.loads(edited["body"])["draft"]["revision"] == 2

        overview_response = web.handle(
            _web_event("GET", "/api/overview", cookie=cookie)
        )
        assert overview_response["statusCode"] == 200
        projected = json.loads(overview_response["body"])
        assert projected["externalEffects"] is False
        assert projected["capability"]["externalEffects"] is False
        assert projected["connection"]["status"] == "CONNECTED"
        assert projected["workspace"]["opportunityCount"] == 1
        assert projected["workspace"]["draftCount"] == 1
        assert source not in overview_response["body"]
        scan_id = projected["lastScan"]["scanId"]
        scan_by_user[user_id] = scan_id
        feedback = web.handle(
            _web_event(
                "POST",
                f"/api/scans/{scan_id}/feedback",
                cookie=cookie,
                csrf=csrf,
                body={"response": "USEFUL"},
            )
        )
        assert feedback["statusCode"] == 200

        exported = web.handle(_web_event("GET", "/api/export", cookie=cookie))
        assert exported["statusCode"] == 200
        archive_bytes = base64.b64decode(exported["body"])
        assert archive_bytes == UserExporter(SyntheticExportSource()).build_zip(
            user_id
        )
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["userId"] == user_id
            assert archive.read("workspace/memory.md") == f"workspace:{user_id}".encode()
        sessions_by_user[user_id] = (cookie, csrf)

    assert len(set(users_by_actor.values())) == 3
    assert not any(token in repr(queue.calls) for token in invite_tokens)
    assert len(oauth.calls) == 6
    assert external_call_sentinel.calls == []
    assert queue.pending == []
    assert runtime.calls == []
    assert len(local_lambda.calls) == 27

    users = list(users_by_actor.values())
    actor_by_user = {user_id: actor for actor, user_id in users_by_actor.items()}

    # Every owner/requester pairing gets a fresh ticket. A live foreign
    # session is rejected without consuming it; the owner can then redeem it.
    for owner, requester in itertools.product(users, repeat=2):
        token = tickets.issue(user_id=owner, return_path="/workspace")
        requester_cookie, _ = sessions_by_user[requester]
        attempted = web.handle(
            _web_event(
                "POST",
                "/api/session/connect",
                cookie=requester_cookie,
                body={"ticket": token},
            )
        )
        if owner == requester:
            accepted = attempted
        else:
            assert attempted["statusCode"] == 400
            owner_cookie, _ = sessions_by_user[owner]
            accepted = web.handle(
                _web_event(
                    "POST",
                    "/api/session/connect",
                    cookie=owner_cookie,
                    body={"ticket": token},
                )
            )
        assert accepted["statusCode"] == 201
        accepted_body = json.loads(accepted["body"])
        assert accepted_body["returnPath"] == "/workspace"
        sessions_by_user[owner] = (
            accepted["headers"]["Set-Cookie"],
            accepted_body["csrfToken"],
        )

    matrix_update = itertools.count(4_000)
    # Cards are bound over the full owner/requester product. Foreign actors
    # fail through the real FIFO/worker/control path without consuming the
    # owner's handle; the owner succeeds exactly once.
    for owner in users:
        actor = actor_by_user[owner]
        update_id = next(matrix_update)
        scan_update = {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "chat": {"id": actor, "type": "private"},
                "from": {"id": actor, "first_name": "Matrix owner"},
                "text": "/scan",
            },
        }
        assert ingress.handle(
            json.dumps(scan_update),
            {"x-telegram-bot-api-secret-token": "synthetic-webhook-secret"},
        )["statusCode"] == 200
        assert queue.process_next(worker_dependencies) == {"batchItemFailures": []}
        keyboard = outbox.calls[-1]["reply_markup"]["inline_keyboard"][0]
        tested_callback = keyboard[3]["callback_data"]
        callback_by_user[owner] = keyboard[0]["callback_data"]
        for requester in [value for value in users if value != owner] + [owner]:
            requester_actor = actor_by_user[requester]
            callback_update_id = next(matrix_update)
            before_deliveries = len(outbox.calls)
            callback_update = {
                "update_id": callback_update_id,
                "callback_query": {
                    "id": f"matrix_callback_{callback_update_id}",
                    "from": {
                        "id": requester_actor,
                        "first_name": "Matrix requester",
                    },
                    "message": {
                        "message_id": callback_update_id,
                        "chat": {"id": requester_actor, "type": "private"},
                    },
                    "data": tested_callback,
                },
            }
            assert ingress.handle(
                json.dumps(callback_update),
                {"x-telegram-bot-api-secret-token": "synthetic-webhook-secret"},
            )["statusCode"] == 200
            processed = queue.process_next(worker_dependencies)
            if requester == owner:
                assert processed == {"batchItemFailures": []}
                assert len(outbox.calls) == before_deliveries + 1
            else:
                assert processed["batchItemFailures"]
                assert len(outbox.calls) == before_deliveries
        scan_by_user[owner] = measurements.latest(owner)["scanId"]

    # Scan IDs and draft action IDs remain owner-bound for all nine browser
    # session combinations.
    for owner, requester in itertools.product(users, repeat=2):
        requester_cookie, requester_csrf = sessions_by_user[requester]
        feedback = web.handle(
            _web_event(
                "POST",
                f"/api/scans/{scan_by_user[owner]}/feedback",
                cookie=requester_cookie,
                csrf=requester_csrf,
                body={"response": "USEFUL"},
            )
        )
        assert feedback["statusCode"] == (200 if owner == requester else 403)

    for owner in users:
        owner_cookie, _ = sessions_by_user[owner]
        current = json.loads(
            web.handle(_web_event("GET", "/api/gmail", cookie=owner_cookie))["body"]
        )
        owner_draft = next(
            item for item in current["drafts"]
            if item["actionId"] == draft_by_user[owner]
        )
        for requester in users:
            requester_cookie, requester_csrf = sessions_by_user[requester]
            edited = web.handle(
                _web_event(
                    "POST",
                    f"/api/gmail/drafts/{draft_by_user[owner]}",
                    cookie=requester_cookie,
                    csrf=requester_csrf,
                    body={
                        "revision": owner_draft["revision"],
                        "subject": f"Matrix edit for {owner}",
                        "body": f"Private matrix body for {owner}",
                    },
                )
            )
            assert edited["statusCode"] == (200 if owner == requester else 403)

    for requester in users:
        requester_cookie, _ = sessions_by_user[requester]
        overview_response = web.handle(
            _web_event("GET", "/api/overview", cookie=requester_cookie)
        )
        assert overview_response["statusCode"] == 200
        projection = json.loads(overview_response["body"])
        digest = hashlib.sha256(requester.encode()).hexdigest()
        assert projection["workspace"]["workspaceReceipt"]["generation"] == (
            f"gen_{digest[:16]}"
        )
        assert projection["workspace"]["opportunityCount"] == 1
        assert projection["workspace"]["draftCount"] == 1
        assert all(source not in overview_response["body"] for source in source_by_user.values())

        exported = web.handle(
            _web_event("GET", "/api/export", cookie=requester_cookie)
        )
        archive_bytes = base64.b64decode(exported["body"])
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            exported_content = b"\n".join(
                archive.read(name) for name in archive.namelist()
            )
        assert requester.encode() in exported_content
        assert all(
            other.encode() not in exported_content
            for other in users
            if other != requester
        )

    first_cookie, first_csrf = sessions_by_user[users[0]]

    disconnected = web.handle(
        _web_event(
            "POST",
            f"/api/connections/{READONLY_PROVIDER}/disconnect",
            cookie=first_cookie,
            csrf=first_csrf,
            body={},
        )
    )
    assert json.loads(disconnected["body"]) == {
        "provider": READONLY_PROVIDER,
        "status": "DISCONNECTED",
        "remoteGrantRevoked": False,
    }
    first_overview = web.handle(
        _web_event("GET", "/api/overview", cookie=first_cookie)
    )
    assert json.loads(first_overview["body"])["connection"]["status"] == (
        "DISCONNECTED"
    )
    first_gmail = json.loads(
        web.handle(_web_event("GET", "/api/gmail", cookie=first_cookie))["body"]
    )
    assert first_gmail["opportunities"] == []
    assert first_gmail["drafts"] == []
    for other in users[1:]:
        other_cookie, _ = sessions_by_user[other]
        other_gmail = json.loads(
            web.handle(_web_event("GET", "/api/gmail", cookie=other_cookie))["body"]
        )
        assert len(other_gmail["opportunities"]) == 1
        assert len(other_gmail["drafts"]) == 1

    stale_update_id = next(matrix_update)
    stale_actor = actor_by_user[users[0]]
    stale_callback = {
        "update_id": stale_update_id,
        "callback_query": {
            "id": f"stale_callback_{stale_update_id}",
            "from": {"id": stale_actor, "first_name": "Disconnected owner"},
            "message": {
                "message_id": stale_update_id,
                "chat": {"id": stale_actor, "type": "private"},
            },
            "data": callback_by_user[users[0]],
        },
    }
    before_deliveries = len(outbox.calls)
    assert ingress.handle(
        json.dumps(stale_callback),
        {"x-telegram-bot-api-secret-token": "synthetic-webhook-secret"},
    )["statusCode"] == 200
    assert queue.process_next(worker_dependencies)["batchItemFailures"]
    assert len(outbox.calls) == before_deliveries

    # Logout one disposable session for every owner; the primary session and
    # every other tenant remain live.
    for owner in users:
        token = tickets.issue(user_id=owner, return_path="/")
        disposable = web.handle(
            _web_event("POST", "/api/session/connect", body={"ticket": token})
        )
        disposable_body = json.loads(disposable["body"])
        disposable_cookie = disposable["headers"]["Set-Cookie"]
        logged_out = web.handle(
            _web_event(
                "POST",
                "/api/session/logout",
                cookie=disposable_cookie,
                csrf=disposable_body["csrfToken"],
                body={},
            )
        )
        assert logged_out["statusCode"] == 204
        assert web.handle(
            _web_event("GET", "/api/overview", cookie=disposable_cookie)
        )["statusCode"] == 401
        for expected_live in users:
            primary_cookie, _ = sessions_by_user[expected_live]
            assert web.handle(
                _web_event("GET", "/api/overview", cookie=primary_cookie)
            )["statusCode"] == 200

    # Delete every tenant sequentially and check the complete cross-user
    # session matrix after each new durable deletion fence.
    deleted: set[str] = set()
    for owner in users:
        owner_cookie, owner_csrf = sessions_by_user[owner]
        deletion_requested = web.handle(
            _web_event(
                "POST",
                "/api/delete",
                cookie=owner_cookie,
                csrf=owner_csrf,
                body={"confirm": "DELETE"},
            )
        )
        assert deletion_requested["statusCode"] == 202
        deleted.add(owner)
        for requester in users:
            requester_cookie, _ = sessions_by_user[requester]
            assert web.handle(
                _web_event("GET", "/api/overview", cookie=requester_cookie)
            )["statusCode"] == (401 if requester in deleted else 200)

    store.now_ms += DeletionCoordinator.FINALIZATION_GRACE_MS - 1
    for owner in users:
        assert deletion.reconcile(owner) == {
            "status": "pending",
            "userId": owner,
        }
    store.now_ms += 1
    for owner in users:
        assert deletion.reconcile(owner) == {
            "status": "deleted",
            "userId": owner,
        }
    assert {
        (name, user_id)
        for name, user_id in state.purge_calls
        if name in {"connections", "runtime", "workspace", "measurements", "footprint"}
    } == {
        (name, user_id)
        for name, user_id in itertools.product(
            {"connections", "runtime", "workspace", "measurements", "footprint"},
            users,
        )
    }
    assert queue.pending == []
    assert runtime.calls == []
    assert external_call_sentinel.calls == []
