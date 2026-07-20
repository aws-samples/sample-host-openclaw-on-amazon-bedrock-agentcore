"""Credential-free, deterministic Personal Operator v1 pilot evidence harness.

This module is test-only by location.  It deliberately composes production
contracts with local stores and provider fakes; nothing here is packaged into
any Lambda or runtime image.
"""

from __future__ import annotations

import base64
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import itertools
from pathlib import Path
import socket
import sys
from unittest.mock import patch
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
LAMBDA_ROOT = ROOT / "lambda"
if str(LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(LAMBDA_ROOT))

from capabilities.test_gateway import (  # noqa: E402 - test-only composition
    _call as capability_call,
    _gateway as capability_gateway,
    _iam as capability_iam,
    _rebind_repository,
)
from control.invites import DynamoPilotInvites  # noqa: E402
from control.test_invites import MemoryInviteTable  # noqa: E402
from observability.events import OperationalEventV1  # noqa: E402
from observability.report import build_cohort_report  # noqa: E402
from portable import PortableExporter, PortableImporter  # noqa: E402
from portable.manifest import ImportRejected  # noqa: E402
from portable.staging import DynamoStagedImportStore  # noqa: E402
from portable.test_staging import FakeBlobs, FakeTable  # noqa: E402
from scheduler.models import build_schedule_spec  # noqa: E402
from scheduler.proposals import build_create_schedule_proposal  # noqa: E402
from scheduler.service import (  # noqa: E402
    assert_scheduled_turn_operation_allowed,
    scheduled_read_only_operations,
)
from web.auth import OpaqueSessionManager, SignedConnectTickets  # noqa: E402
from web.gmail_workspace import GmailWorkspaceService  # noqa: E402
from web.measurements import DynamoScanMeasurements  # noqa: E402
from web.retention import (  # noqa: E402
    DeletionCoordinator,
    DeletionPending,
)
from workflows.founder_approval import founder_draft_revision  # noqa: E402
from workflows.gmail.models import (  # noqa: E402
    DraftRevision,
    Opportunity,
    opportunity_id,
)
from workflows.gmail.oauth import (  # noqa: E402
    GMAIL_READONLY_SCOPE,
    GOOGLE_AUTHORIZATION_ENDPOINT,
    GoogleReadonlyOAuthFlow,
)
from workflows.gmail.repository import DynamoGmailRepository  # noqa: E402
from workflows.gmail.scanner import GmailScanner  # noqa: E402


NOW_SECONDS = 1_800_000_000
NOW = datetime.fromtimestamp(NOW_SECONDS, tz=timezone.utc)
ACTORS = ("871001", "871002", "871003")
REPORT_CANARIES = (
    *ACTORS,
    "pilot-source-1",
    "pilot-source-2",
    "pilot-source-3",
    "message-1",
    "message-2",
    "message-3",
    "contact-1@example.invalid",
    "contact-2@example.invalid",
    "contact-3@example.invalid",
    "Private synthetic workspace 1",
    "Private synthetic workspace 2",
    "Private synthetic workspace 3",
    "Synthetic private subject 1",
    "Synthetic private subject 2",
    "Synthetic private subject 3",
)


class _DeterministicBytes:
    def __init__(self) -> None:
        self._counter = itertools.count(1)

    def __call__(self, size: int) -> bytes:
        digest = hashlib.sha512(
            f"personal-operator-v1-pilot-{next(self._counter)}".encode()
        ).digest()
        return (digest * ((size // len(digest)) + 1))[:size]


class _LocalIdentityStore:
    """Combined one-time, session, and deletion-intent test port."""

    def __init__(self) -> None:
        self.one_time: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.revoked_users: set[str] = set()
        self.deletion_intents: dict[str, dict] = {}
        self.now_ms = NOW_SECONDS * 1_000

    def put_once(self, key, record, *, expires_at):
        if key in self.one_time:
            raise RuntimeError("one-time key collision")
        self.one_time[key] = {**record, "expiresAt": expires_at}

    def pop_once(self, key):
        return self.one_time.pop(key, None)

    def create(self, key, record, *, expires_at):
        if key in self.sessions:
            raise RuntimeError("session key collision")
        self.sessions[key] = {**record, "expiresAt": expires_at}

    def get(self, key):
        record = self.sessions.get(key)
        if record is None:
            return None
        return {
            **record,
            "revoked": bool(
                record.get("revoked") is True
                or record.get("userId") in self.revoked_users
            ),
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
        return dict(
            self.deletion_intents.setdefault(
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
        )

    def get_deletion_intent(self, user_id):
        value = self.deletion_intents.get(user_id)
        return dict(value) if value is not None else None

    def mark_deletion_finalizing(self, user_id):
        value = self.deletion_intents[user_id]
        value["deletionStatus"] = "FINALIZING"
        value["finalizingAt"] = self.now_ms
        return dict(value)

    def complete_deletion(self, user_id, *, finalizing_before_ms):
        value = self.deletion_intents[user_id]
        if value["finalizingAt"] > finalizing_before_ms:
            raise RuntimeError("deletion drain is incomplete")
        complete = {
            "userId": user_id,
            "purgeReason": "ACCOUNT_DELETION",
            "deletionStatus": "COMPLETED",
            "requestedAt": None,
            "finalizingAt": None,
            "completedAt": self.now_ms,
        }
        self.deletion_intents[user_id] = complete
        return dict(complete)


class _SyntheticGmailClient:
    def __init__(self, thread: dict) -> None:
        self._thread = thread
        self.calls: list[tuple] = []

    def list_threads(self, *, query, max_results):
        self.calls.append(("list", query, max_results))
        return [{"id": self._thread["id"]}]

    def get_thread(self, *, thread_id, format):
        self.calls.append(("get", thread_id, format))
        if thread_id != self._thread["id"] or format != "full":
            raise AssertionError("synthetic Gmail lookup crossed its exact source")
        return self._thread


class _LocalOAuthStateStore:
    def __init__(self) -> None:
        self.records: dict[str, dict] = {}

    def put_once(self, key, record, *, expires_at):
        if key in self.records:
            raise RuntimeError("synthetic OAuth state collision")
        self.records[key] = {**record, "ttl": expires_at}

    def pop_once(self, key):
        value = self.records.pop(key, None)
        if value is None:
            return None
        value = dict(value)
        value.pop("ttl")
        return value


class _LocalOAuthTokenClient:
    def __init__(self, *, code: str, redirect_uri: str) -> None:
        self._code = code
        self._redirect_uri = redirect_uri
        self.calls: list[dict] = []

    def exchange_code(self, **request):
        if (
            set(request)
            != {"code", "code_verifier", "redirect_uri", "client_id"}
            or request["code"] != self._code
            or request["redirect_uri"] != self._redirect_uri
            or request["client_id"] != "synthetic-local-client"
            or not isinstance(request["code_verifier"], str)
            or not request["code_verifier"]
        ):
            raise AssertionError("synthetic OAuth exchange lost its exact binding")
        self.calls.append(dict(request))
        return {
            "access_token": "synthetic-local-access-token",
            "refresh_token": "synthetic-local-refresh-token",
            "scope": GMAIL_READONLY_SCOPE,
            "token_type": "Bearer",
            "expires_in": 3_600,
        }


class _LocalOAuthTokenVault:
    def __init__(self, *, user_id: str) -> None:
        self._user_id = user_id
        self.calls: list[dict] = []

    def save(self, **request):
        token = request.get("token")
        if (
            set(request) != {"user_id", "provider", "token"}
            or request.get("user_id") != self._user_id
            or request.get("provider") != "google-gmail-readonly"
            or not isinstance(token, dict)
            or token.get("scope") != GMAIL_READONLY_SCOPE
        ):
            raise AssertionError("synthetic OAuth vault lost its exact binding")
        self.calls.append(dict(request))


class _LocalConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _LocalMeasurementTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}

    @staticmethod
    def _key(value):
        return value["PK"], value["SK"]

    def put_item(self, **request):
        item = deepcopy(request["Item"])
        key = self._key(item)
        if key in self.items:
            raise _LocalConditionalFailure()
        self.items[key] = item
        return {}

    def update_item(self, **request):
        key = self._key(request["Key"])
        item = self.items.get(key)
        if item is None:
            raise _LocalConditionalFailure()
        values = request["ExpressionAttributeValues"]
        if ":count" in values:
            if item.get("status") != "RUNNING":
                raise _LocalConditionalFailure()
            item.update(
                status=values[":status"],
                completedAt=values[":completed"],
                resultCount=values[":count"],
            )
        elif ":feedback" in values:
            if item.get("status") not in {"SUCCEEDED", "EMPTY"} or (
                "feedback" in item
            ):
                raise _LocalConditionalFailure()
            item["feedback"] = values[":feedback"]
        else:  # pragma: no cover - guarded by the bounded rehearsal
            raise AssertionError("unexpected scan measurement update")
        return {"Attributes": deepcopy(item)}

    def query(self, **request):
        values = request["ExpressionAttributeValues"]
        pk = values[":pk"]
        prefix = values[":prefix"]
        items = [
            deepcopy(item)
            for (item_pk, sk), item in self.items.items()
            if item_pk == pk and sk.startswith(prefix)
        ]
        items.sort(key=lambda item: item["SK"], reverse=True)
        return {"Items": items[: request.get("Limit", len(items))]}

    def get_item(self, **request):
        item = self.items.get(self._key(request["Key"]))
        return {"Item": deepcopy(item)} if item is not None else {}

    def delete_item(self, **request):
        self.items.pop(self._key(request["Key"]), None)
        return {}


class _LocalGmailTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}

    @staticmethod
    def _key(value):
        return value["PK"], value["SK"]

    def put_item(self, **request):
        item = deepcopy(request["Item"])
        key = self._key(item)
        if request.get("ConditionExpression") and key in self.items:
            raise _LocalConditionalFailure()
        self.items[key] = item
        return {}

    def get_item(self, **request):
        item = self.items.get(self._key(request["Key"]))
        return {"Item": deepcopy(item)} if item is not None else {}

    def query(self, **request):
        values = request["ExpressionAttributeValues"]
        pk = values[":pk"]
        prefix = values[":sk"]
        items = sorted(
            (
                deepcopy(item)
                for (item_pk, sk), item in self.items.items()
                if item_pk == pk and sk.startswith(prefix)
            ),
            key=lambda item: item["SK"],
            reverse=request.get("ScanIndexForward") is False,
        )
        limit = request.get("Limit", len(items))
        return {"Items": items[:limit]}


class _LocalApprovalSuperseder:
    """One exact fail-closed approval transition for the local rehearsal."""

    def __init__(
        self,
        *,
        user_id: str,
        action_id: str,
        revision: int,
        repository,
    ) -> None:
        self._user_id = user_id
        self._action_id = action_id
        self._revision = revision
        self._repository = repository
        self._state = "APPROVAL_PENDING"
        self.calls: list[dict] = []

    def supersede_pending(
        self,
        *,
        action_id: str,
        user_id: str,
        expected_draft_revision: int,
        current_draft_revision: int,
        draft: DraftRevision,
        expires_at: int,
        expected_generation: int | None,
    ):
        if (
            self._state != "APPROVAL_PENDING"
            or user_id != self._user_id
            or action_id != self._action_id
            or expected_draft_revision != self._revision
            or current_draft_revision != self._revision + 1
            or not isinstance(draft, DraftRevision)
            or draft.action_id != action_id
            or draft.revision != current_draft_revision
            or expected_generation is not None
        ):
            raise AssertionError("synthetic approval supersession was not exact")
        self._repository.save_draft(
            user_id=user_id,
            draft=draft,
            expires_at=expires_at,
        )
        call = {
            "action_id": action_id,
            "user_id": user_id,
            "expected_draft_revision": expected_draft_revision,
            "current_draft_revision": current_draft_revision,
        }
        self.calls.append(call)
        self._state = "STALE"
        return {
            "draftPersisted": True,
            "actionId": action_id,
            "userId": user_id,
            "draftRevision": draft.revision,
            "payloadHash": draft.payload_hash,
        }


class _PortableSource:
    def __init__(
        self,
        *,
        user_id: str,
        participant: int,
        schedule,
        workspace_text: str,
    ) -> None:
        self._user_id = user_id
        self._participant = participant
        self._schedule = schedule
        self._workspace_text = workspace_text

    def records_for_user(self, user_id):
        if user_id != self._user_id:
            raise AssertionError("portable export crossed its participant")
        return {
            "memory": [
                {
                    "userId": user_id,
                    "kind": "note",
                    "text": f"Synthetic private memory {self._participant}",
                }
            ],
            "schedules": [self._schedule.to_mapping()],
            "installed_packs": [],
            "connectors": [
                {
                    "connectorId": "google-gmail-readonly",
                    "state": "CONNECTED",
                }
            ],
            "compute_receipts": [],
            "receipts": [],
        }

    def workspace_files(self, user_id):
        if user_id != self._user_id:
            raise AssertionError("portable workspace crossed its participant")
        return {"notes/pilot.md": self._workspace_text.encode("utf-8")}


class _DeletionPorts:
    def __init__(self, *, users: set[str], connections: dict[str, str]) -> None:
        self.users = users
        self.connections = connections
        self.calls: list[tuple[str, str]] = []
        self.workspace: dict[str, object] = {user: object() for user in users}

    def establish_deletion_fence(self, user_id):
        self._check(user_id)
        self.calls.append(("fence", user_id))
        return True

    def revoke_all(self, user_id):
        self._check(user_id)
        self.connections[user_id] = "DISCONNECTED"
        self.calls.append(("connections", user_id))

    def purge_user_schedules(self, user_id):
        self._check(user_id)
        self.calls.append(("schedules", user_id))
        return 0

    def purge(self, user_id):
        self._check(user_id)
        self.calls.append(("runtime", user_id))
        return {
            "userId": user_id,
            "state": "DELETING",
            "purgeReason": "ACCOUNT_DELETION",
            "purgeCompletedAt": NOW_SECONDS,
        }

    def delete_namespace(self, user_id):
        self._check(user_id)
        self.workspace.pop(user_id, None)
        self.calls.append(("workspace", user_id))

    def delete_user_records(self, user_id):
        self._check(user_id)
        self.calls.append(("records", user_id))

    def delete_footprint(self, user_id):
        self._check(user_id)
        self.calls.append(("footprint", user_id))

    def _check(self, user_id):
        if user_id not in self.users:
            raise AssertionError("deletion crossed its participant cohort")


class _FootprintPort:
    def __init__(self, ports: _DeletionPorts) -> None:
        self._ports = ports

    def delete_user_records(self, user_id):
        self._ports.delete_footprint(user_id)


class _ExternalBoundarySentinel:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.installed: set[str] = set()

    def forbidden(self, label: str):
        def fail(*_args, **_kwargs):
            self.calls.append(label)
            raise AssertionError(f"external boundary reached: {label}")

        return fail

    def prove(self, label: str, target) -> None:
        try:
            target()
        except AssertionError as error:
            if str(error) != f"external boundary reached: {label}":
                raise
        else:  # pragma: no cover - means a sentinel patch silently failed
            raise AssertionError(f"external sentinel was not installed: {label}")
        self.installed.add(label)


@contextmanager
def _hermetic_boundaries():
    import boto3
    import requests
    from botocore.client import BaseClient
    from router.runtime_driver import AgentCoreAdapter
    from worker.telegram_delivery import TelegramDeliveryAdapter
    from workflows.gmail.oauth import GoogleOAuthTokenClient
    from workflows.gmail.ranker import OpenAIResponsesAdapter
    from workflows.gmail.scanner import GoogleGmailApiClient

    sentinel = _ExternalBoundarySentinel()
    boto_session = boto3.session.Session
    raw_socket_targets = [
        (socket.socket, name, f"socket.socket.{name}")
        for name in (
            "connect",
            "connect_ex",
            "sendto",
            "send",
            "sendall",
            "sendmsg",
            "sendfile",
        )
        if hasattr(socket.socket, name)
    ]
    targets = (
        (socket, "create_connection", "socket"),
        (socket, "getaddrinfo", "dns"),
        *raw_socket_targets,
        (urllib.request, "urlopen", "urllib"),
        (http.client.HTTPConnection, "connect", "http"),
        (http.client.HTTPSConnection, "connect", "https"),
        (requests.Session, "request", "requests"),
        (boto3, "client", "boto3.client"),
        (boto3, "resource", "boto3.resource"),
        (boto3, "Session", "boto3.Session"),
        (boto_session, "client", "boto3.Session.client"),
        (boto_session, "resource", "boto3.Session.resource"),
        (BaseClient, "_make_api_call", "botocore.api"),
        (GoogleOAuthTokenClient, "exchange_code", "google.oauth.exchange"),
        (GoogleOAuthTokenClient, "refresh", "google.oauth.refresh"),
        (GoogleGmailApiClient, "list_threads", "gmail.list"),
        (GoogleGmailApiClient, "get_thread", "gmail.get"),
        (OpenAIResponsesAdapter, "create", "model.provider"),
        (TelegramDeliveryAdapter, "send_message", "telegram.send"),
        (
            TelegramDeliveryAdapter,
            "acknowledge_callback",
            "telegram.ack",
        ),
        (AgentCoreAdapter, "invoke", "agentcore.invoke"),
    )
    with ExitStack() as stack:
        probes = []
        for owner, name, label in targets:
            stack.enter_context(patch.object(owner, name, sentinel.forbidden(label)))
            probes.append((label, lambda owner=owner, name=name: getattr(owner, name)()))
        for label, probe in probes:
            sentinel.prove(label, probe)
        expected = {label for _, _, label in targets}
        if sentinel.installed != expected or set(sentinel.calls) != expected:
            raise AssertionError("external boundary sentinel activation is incomplete")
        sentinel.calls.clear()
        yield sentinel


@dataclass(frozen=True, slots=True)
class PilotRun:
    report_bytes: bytes
    participants_completed: int
    external_call_ledger: tuple[str, ...]


def _event(component: str, operation: str, outcome: str) -> OperationalEventV1:
    return OperationalEventV1.from_mapping(
        {
            "schema": "personal-operator.operational-event.v1",
            "environment": "synthetic",
            "component": component,
            "operation": operation,
            "outcome": outcome,
            "count": 1,
        }
    )


def _gmail_thread(participant: int, connected_address: str) -> dict:
    body = f"Private source excerpt {participant}"
    return {
        "id": f"pilot-source-{participant}",
        "messages": [
            {
                "id": f"message-{participant}",
                "internalDate": str(
                    int((NOW - timedelta(days=8)).timestamp() * 1_000)
                ),
                "labelIds": ["SENT"],
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "From", "value": connected_address},
                        {
                            "name": "To",
                            "value": f"contact-{participant}@example.invalid",
                        },
                        {
                            "name": "Subject",
                            "value": f"Synthetic private subject {participant}",
                        },
                    ],
                    "body": {
                        "data": base64.urlsafe_b64encode(body.encode())
                        .decode("ascii")
                        .rstrip("=")
                    },
                },
            }
        ],
    }


def _assert_compute_disabled(user_id: str, participant: int) -> None:
    catalog, repository, adapter, gateway, _ = capability_gateway(
        operation_id="compute.run",
        adapter=False,
    )
    session_id = f"session_{participant:016d}"
    _rebind_repository(
        repository,
        user_id=user_id,
        session_id=session_id,
    )
    result = gateway.invoke(
        capability_call(
            catalog,
            "compute.run",
            {
                "command": {"mode": "SCRIPT", "value": "print('local')"},
                "inputPaths": [],
                "network": "NONE",
                "resourceProfile": "SMALL",
            },
            tool_use_id=f"tooluse_compute{participant:08d}",
        ),
        capability_iam(
            catalog,
            grant_overrides={
                "sub": user_id,
                "sessionId": session_id,
                "nonce": "nonce_876543210abcdef",
            },
        ),
    )
    if result.status != "DENIED" or result.error_code != "ADAPTER_DISABLED":
        raise AssertionError("production-shaped compute was not adapter-disabled")
    if adapter.calls:
        raise AssertionError("disabled compute reached an adapter")


def _portable_round_trip(
    *, user_id: str, participant: int, schedule, workspace_text: str
) -> tuple[object, object]:
    source = _PortableSource(
        user_id=user_id,
        participant=participant,
        schedule=schedule,
        workspace_text=workspace_text,
    )
    bundle = PortableExporter(source).build(user_id)
    table = FakeTable()
    store = DynamoStagedImportStore(table, blobs=FakeBlobs())
    importer = PortableImporter(staging=store, now=lambda: NOW_SECONDS + participant)
    plan = importer.build_plan(bundle.zip_bytes, target_user_id=user_id)
    if not (
        plan.schedules_disabled
        and plan.connectors_disconnected
        and plan.effects_replayable is False
    ):
        raise AssertionError("portable plan did not preserve inert landing policy")
    prepared = importer.prepare_activation(
        bundle.zip_bytes,
        target_user_id=user_id,
        approved_bundle_hash=plan.bundle_hash,
        approved_plan_id=plan.plan_id,
        approved_base_generation=plan.base_generation,
    )
    receipt = importer.activate(
        bundle.zip_bytes,
        target_user_id=user_id,
        approved_bundle_hash=prepared.bundle_hash,
        approved_plan_id=prepared.plan_id,
        approved_base_generation=prepared.base_generation,
        activation_approval=prepared.activation_approval,
        expected_generation=prepared.expected_generation,
    )
    live = store.load_live(user_id)
    landing = live["staged"]["landing"]
    if receipt.state != "ACTIVATED" or landing != {
        "schedules": "DISABLED",
        "installedPacks": "PAUSED",
        "connectors": "DISCONNECTED",
        "computeReceipts": {"replayable": False},
        "receipts": {"replayable": False},
    }:
        raise AssertionError("portable state did not land inert")
    if live["staged"]["records"]["connectors"] != [
        {"connectorId": "google-gmail-readonly", "state": "DISCONNECTED"}
    ]:
        raise AssertionError("portable connector authority was restored")
    landed_schedule = live["staged"]["records"]["schedules"][0]
    if landed_schedule["state"] != "DISABLED" or "nextRunAt" in landed_schedule:
        raise AssertionError("portable schedule authority was restored")

    before = deepcopy(table.items)
    before_generation = store.load_generation(user_id)
    replay_denied = False
    try:
        replay_plan = importer.build_plan(bundle.zip_bytes, target_user_id=user_id)
        importer.prepare_activation(
            bundle.zip_bytes,
            target_user_id=user_id,
            approved_bundle_hash=replay_plan.bundle_hash,
            approved_plan_id=replay_plan.plan_id,
            approved_base_generation=replay_plan.base_generation,
        )
    except ImportRejected:
        replay_denied = True
    if not replay_denied:
        raise AssertionError("identical portable bundle replay was not denied")
    if store.load_generation(user_id) != before_generation:
        raise AssertionError("portable replay advanced the generation")
    if table.items != before:
        raise AssertionError("portable replay changed live state")
    return bundle, receipt


def run_synthetic_pilot() -> PilotRun:
    events: list[OperationalEventV1] = []
    random_bytes = _DeterministicBytes()
    invite_tokens: list[str] = []
    dynamic_canaries: list[str] = []
    users: list[str] = []
    completed: list[str] = []
    connections: dict[str, str] = {}
    sessions: dict[str, object] = {}
    workspaces: dict[str, dict] = {}
    cards: dict[str, str] = {}

    with _hermetic_boundaries() as external:
        invites = DynamoPilotInvites(
            MemoryInviteTable(),
            now=lambda: NOW_SECONDS,
            random_bytes=random_bytes,
        )
        identity_store = _LocalIdentityStore()
        tickets = SignedConnectTickets(
            secret=b"synthetic-pilot-ticket-secret-v1",
            store=identity_store,
            now=lambda: NOW_SECONDS,
            random_bytes=random_bytes,
        )
        session_manager = OpaqueSessionManager(
            secret=b"synthetic-pilot-session-secret-v1",
            store=identity_store,
            now=lambda: NOW_SECONDS,
            random_bytes=random_bytes,
        )

        for participant, actor in enumerate(ACTORS, 1):
            issued = invites.issue()
            invite_tokens.append(issued.token)
            redemption = invites.redeem(
                issued.token,
                channel="telegram",
                channel_user_id=actor,
                display_name=f"Synthetic Pilot {participant}",
            )
            if not redemption.created or redemption.user_id in users:
                raise AssertionError("pilot invitation did not bind one participant")
            user_id = redemption.user_id
            users.append(user_id)
            events.append(_event("control", "invite", "succeeded"))

            connect_ticket = tickets.issue(
                user_id=user_id,
                return_path="/connections",
            )
            connected = tickets.consume(connect_ticket)
            if connected.user_id != user_id or connected.return_path != "/connections":
                raise AssertionError("connect ticket lost its participant binding")
            session = session_manager.issue(user_id=user_id)
            authenticated = session_manager.authenticate(
                cookie_header=session.cookie,
                csrf_token=session.csrf_token,
                require_csrf=True,
            )
            if authenticated.user_id != user_id:
                raise AssertionError("browser session crossed its participant")
            sessions[user_id] = session

            if GMAIL_READONLY_SCOPE != "https://www.googleapis.com/auth/gmail.readonly":
                raise AssertionError("synthetic connector is not read-only")
            redirect_uri = "https://operator.example/oauth/google/callback"
            oauth_state = _LocalOAuthStateStore()
            token_client = _LocalOAuthTokenClient(
                code=f"synthetic-code-{participant}",
                redirect_uri=redirect_uri,
            )
            token_vault = _LocalOAuthTokenVault(user_id=user_id)
            oauth = GoogleReadonlyOAuthFlow(
                state_store=oauth_state,
                token_client=token_client,
                token_vault=token_vault,
                client_id="synthetic-local-client",
                authorization_endpoint=GOOGLE_AUTHORIZATION_ENDPOINT,
                allowed_redirect_uris={redirect_uri},
                now=lambda: NOW,
                random_bytes=random_bytes,
            )
            authorization = oauth.start(
                user_id=user_id,
                redirect_uri=redirect_uri,
            )
            if (
                authorization.code_challenge_method != "S256"
                or not authorization.state
                or not authorization.url.startswith(
                    GOOGLE_AUTHORIZATION_ENDPOINT + "?"
                )
            ):
                raise AssertionError("synthetic OAuth start was not exact")
            oauth.complete(
                user_id=user_id,
                state=authorization.state,
                code=f"synthetic-code-{participant}",
            )
            if (
                len(token_client.calls) != 1
                or len(token_vault.calls) != 1
                or oauth_state.records
            ):
                raise AssertionError("synthetic OAuth completion was not consumed once")
            events.append(_event("oauth", "connect", "succeeded"))
            connections[user_id] = "CONNECTED_READ_ONLY"
            events.append(_event("connector", "connect", "succeeded"))

            connected_address = f"pilot-{participant}@example.invalid"
            gmail = _SyntheticGmailClient(
                _gmail_thread(participant, connected_address)
            )
            sources = GmailScanner(
                gmail,
                connected_address=connected_address,
                now=lambda: NOW,
            ).scan()
            if len(sources) != 1 or len(gmail.calls) != 2:
                raise AssertionError("bounded synthetic scan did not complete")
            source = sources[0]
            if source.source_id != (
                f"gmail:pilot-source-{participant}:message-{participant}"
            ):
                raise AssertionError("scan source was not exact")
            events.append(_event("scan", "scan", "succeeded"))

            opportunity = Opportunity(
                id=opportunity_id(user_id, source.source_id),
                user_id=user_id,
                source=source,
                waiting_since=source.waiting_since,
                title=f"Synthetic follow-up {participant}",
                reason="A source-backed thread has no reply in the pilot window.",
                confidence=0.9,
            )

            card_ref = "card_" + hashlib.sha256(
                f"{user_id}\0{source.source_id}".encode()
            ).hexdigest()
            cards[card_ref] = user_id
            if cards.get(card_ref) != user_id:
                raise AssertionError("scan card was not participant-bound")
            events.append(_event("cards", "card", "succeeded"))

            measurements = DynamoScanMeasurements(
                _LocalMeasurementTable(),
                identity_key=hashlib.sha256(
                    b"synthetic-local-scan-measurement-identity"
                ).digest(),
                now=lambda: NOW_SECONDS,
                random_bytes=random_bytes,
            )
            scan_ref = measurements.start(user_id)
            terminal_scan = measurements.complete(
                user_id,
                scan_ref,
                result_count=len(sources),
            )
            recorded_feedback = measurements.feedback(
                user_id,
                scan_ref,
                response="USEFUL",
            )
            if (
                terminal_scan["status"] != "SUCCEEDED"
                or recorded_feedback["scanId"] != scan_ref
                or recorded_feedback["feedback"] != "USEFUL"
            ):
                raise AssertionError("scan feedback was not participant-bound")
            events.append(_event("feedback", "feedback", "succeeded"))

            draft = founder_draft_revision(
                user_id=user_id,
                opportunity=opportunity,
                connection_id=f"gmail_readonly_{participant:02d}",
                account_email=connected_address,
            )
            gmail_table = _LocalGmailTable()
            gmail_repository = DynamoGmailRepository(
                gmail_table,
                conditional_failure_types=(_LocalConditionalFailure,),
                now=lambda: NOW,
            )
            derived_expires_at = int((NOW + timedelta(days=14)).timestamp())
            gmail_repository.replace_opportunities(
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
                        "waitingSince": opportunity.waiting_since.isoformat(),
                        "title": opportunity.title,
                        "reason": opportunity.reason,
                        "confidence": opportunity.confidence,
                    }
                ],
                expires_at=derived_expires_at,
            )
            gmail_repository.save_draft(
                user_id=user_id,
                draft=draft,
                expires_at=derived_expires_at,
            )
            superseder = _LocalApprovalSuperseder(
                user_id=user_id,
                action_id=draft.action_id,
                revision=draft.revision,
                repository=gmail_repository,
            )
            gmail_workspace = GmailWorkspaceService(
                gmail_table,
                repository=gmail_repository,
                approval_superseder=superseder,
                now=lambda: NOW,
            )
            workspace_view = gmail_workspace.get(user_id)
            if (
                workspace_view.get("userId") != user_id
                or len(workspace_view.get("opportunities", [])) != 1
                or workspace_view["opportunities"][0]["id"] != opportunity.id
                or workspace_view.get("drafts")
                != [
                    {
                        "actionId": draft.action_id,
                        "revision": draft.revision,
                        "to": draft.to,
                        "subject": draft.subject,
                        "body": draft.body,
                        "payloadHash": draft.payload_hash,
                    }
                ]
            ):
                raise AssertionError("workspace read lost its exact Gmail binding")
            events.append(_event("workspace", "workspace", "succeeded"))

            edited_subject = f"Synthetic edited private subject {participant}"
            edited_body = f"Private local-only edited draft {participant}"
            edited = gmail_workspace.edit_draft(
                user_id=user_id,
                action_id=draft.action_id,
                revision=draft.revision,
                subject=edited_subject,
                body=edited_body,
            )["draft"]
            if (
                edited["actionId"] != draft.action_id
                or edited["revision"] != draft.revision + 1
                or edited["to"] != draft.to
                or edited["subject"] != edited_subject
                or edited["body"] != edited_body
                or edited["payloadHash"]
                != DraftRevision.compute_payload_hash(
                    to=draft.to,
                    subject=edited_subject,
                    body=edited_body,
                )
                or superseder.calls
                != [
                    {
                        "action_id": draft.action_id,
                        "user_id": user_id,
                        "expected_draft_revision": draft.revision,
                        "current_draft_revision": draft.revision + 1,
                    }
                ]
            ):
                raise AssertionError("workspace edit lost its exact draft binding")
            workspace_text = f"Private synthetic workspace {participant}"
            workspaces[user_id] = {
                "draft": edited,
                "source": source,
                "file": workspace_text,
            }
            events.append(_event("workspace", "draft", "succeeded"))

            schedule_definition = {
                "prompt": f"Read only synthetic workspace {participant}",
                "runAt": NOW_SECONDS + 3_600 + participant,
                "timezone": "Europe/Tallinn",
            }
            schedule_proposal = build_create_schedule_proposal(
                catalog_digest="a" * 64,
                user_id=user_id,
                invocation_id=f"invocation_{participant:016d}",
                task_type="READ_ONLY_AGENT_TURN",
                definition=schedule_definition,
                delivery_target={"actorId": f"telegram:{actor}", "chatId": actor},
                now=NOW_SECONDS,
                nonce=f"nonce_{participant:016d}",
            )
            assert_scheduled_turn_operation_allowed(
                "schedule.propose",
                external_effects=False,
            )
            if (
                schedule_proposal.proposal.operation_id != "schedule.propose"
                or schedule_proposal.proposal.approval_policy != "EXACT_ONE_TIME"
                or schedule_proposal.proposal.arguments["taskType"]
                != "READ_ONLY_AGENT_TURN"
                or "gmail.send" in scheduled_read_only_operations()
            ):
                raise AssertionError("scheduled turn was not proposal-only/read-only")
            events.append(_event("capability_gateway", "capability", "succeeded"))
            events.append(_event("scheduler", "schedule", "pending"))

            _assert_compute_disabled(user_id, participant)
            events.append(_event("compute", "compute", "disabled"))

            schedule = build_schedule_spec(
                schedule_id=schedule_proposal.schedule_id,
                user_id=user_id,
                task_type="READ_ONLY_AGENT_TURN",
                definition=schedule_definition,
                revision=1,
                state="PAUSED",
                next_run_at=None,
            )
            bundle, receipt = _portable_round_trip(
                user_id=user_id,
                participant=participant,
                schedule=schedule,
                workspace_text=workspace_text,
            )
            if receipt.user_id != user_id or bundle.bundle_hash != receipt.bundle_hash:
                raise AssertionError("portable receipt lost its participant binding")
            events.extend(
                (
                    _event("portable", "export", "succeeded"),
                    _event("portable", "import", "succeeded"),
                    _event("portable", "import", "inert"),
                    _event("portable", "import", "replay_denied"),
                )
            )
            completed.append(user_id)

            dynamic_canaries.extend(
                (
                    issued.token,
                    connect_ticket,
                    user_id,
                    source.source_id,
                    source.deep_link,
                    source.correspondent,
                    source.subject,
                    source.excerpt,
                    workspace_text,
                    draft.body,
                    edited_subject,
                    edited_body,
                    authorization.state,
                    "synthetic-local-access-token",
                    "synthetic-local-refresh-token",
                )
            )

        if len(set(users)) != 3 or set(completed) != set(users):
            raise AssertionError("synthetic cohort did not complete exactly once")
        if any(
            session_manager.authenticate(cookie_header=sessions[user].cookie).user_id
            != user
            for user in users
        ):
            raise AssertionError("synthetic cohort session isolation failed")

        deletion_ports = _DeletionPorts(
            users=set(users),
            connections=connections,
        )
        deletion = DeletionCoordinator(
            session_store=identity_store,
            authority_fence=deletion_ports,
            connection_store=deletion_ports,
            runtime_driver=deletion_ports,
            workspace_store=deletion_ports,
            record_store=deletion_ports,
            footprint_store=_FootprintPort(deletion_ports),
            schedule_store=deletion_ports,
            clock_ms=lambda: identity_store.now_ms,
        )
        for user_id in users:
            try:
                deletion.delete(user_id)
            except DeletionPending:
                pass
            else:
                raise AssertionError("first deletion pass claimed terminal completion")
            intent = identity_store.get_deletion_intent(user_id)
            if intent["deletionStatus"] != "FINALIZING":
                raise AssertionError("first deletion pass lacks a durable fence")
            events.append(_event("control", "deletion", "pending"))

        identity_store.now_ms += DeletionCoordinator.FINALIZATION_GRACE_MS
        for user_id in users:
            if deletion.reconcile(user_id) != {
                "status": "deleted",
                "userId": user_id,
            }:
                raise AssertionError("second deletion pass did not complete")
            expected_ports = {
                "fence",
                "connections",
                "schedules",
                "runtime",
                "workspace",
                "records",
                "footprint",
            }
            counts = {
                name: deletion_ports.calls.count((name, user_id))
                for name in expected_ports
            }
            if set(counts.values()) != {2}:
                raise AssertionError("two-phase deletion did not repeat every purge")
            if connections[user_id] != "DISCONNECTED":
                raise AssertionError("deletion left connector authority")
            if identity_store.get_deletion_intent(user_id)["deletionStatus"] != (
                "COMPLETED"
            ):
                raise AssertionError("deletion completion marker is absent")
            events.append(_event("control", "deletion", "succeeded"))

        if external.calls:
            raise AssertionError("synthetic pilot crossed an external boundary")
        report = build_cohort_report(events, participant_count=len(completed))
        report_bytes = report.to_canonical_bytes()
        for canary in (*REPORT_CANARIES, *invite_tokens, *dynamic_canaries):
            if canary.encode("utf-8") in report_bytes:
                raise AssertionError("cohort report retained private/source data")
        return PilotRun(
            report_bytes=report_bytes,
            participants_completed=len(completed),
            external_call_ledger=tuple(external.calls),
        )


__all__ = [
    "PilotRun",
    "REPORT_CANARIES",
    "run_synthetic_pilot",
]
