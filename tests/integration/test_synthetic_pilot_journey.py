from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import itertools
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlencode, urlparse
import zipfile


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lambda"))

from control.index import ControlApplication
from control.invites import DynamoPilotInvites
from control.telegram_cards import CardActionRejected
from control.test_invites import ConditionalFailure, MemoryInviteTable
from router.event_identity import derive_event_trace
from router.telegram_ingress import TelegramWebhookIngress
from web.auth import OpaqueSessionManager, SignedConnectTickets
from web.index import WebApplication
from web.overview import PilotOverviewService
from web.retention import DeletionCoordinator, UserExporter
from workflows.gmail.repository import READONLY_PROVIDER


ORIGIN = "https://operator.example"
NOW_SECONDS = 1_800_000_000
PARTICIPANTS = (701, 702, 703)


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


class SyntheticQueue:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send_message(self, **request):
        self.calls.append(dict(request))
        return {
            "MessageId": f"synthetic-{len(self.calls)}",
            "SequenceNumber": str(len(self.calls)),
        }


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
        self.effect_calls: list[object] = []
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

    def status(self, user_id):
        self._state.ensure_user(user_id)
        return self._state.connections[user_id]

    def disconnect(self, user_id):
        self._state.ensure_user(user_id)
        self._state.connections[user_id] = "DISCONNECTED"
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
        record = self._records.pop(callback_data, None)
        if record is None or record[:3] != (user_id, chat_id, actor_id):
            raise CardActionRejected("synthetic card binding mismatch")
        return SimpleNamespace(action=record[3], opportunity=record[4])


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


def _control_request(wire: dict) -> dict:
    return {
        "action": "productCommand",
        "userId": wire["userId"],
        "channel": wire["channel"],
        "command": wire["payload"]["command"],
        "chatId": wire["payload"]["chatId"],
        "actorId": wire["payload"]["actorId"],
        "traceId": wire["traceId"],
        "idempotencyKey": wire["traceId"],
    }


def _command(user_id: str, actor: int, command: str, update: int) -> dict:
    trace = derive_event_trace("telegram", user_id, str(update))
    return {
        "action": "productCommand",
        "userId": user_id,
        "channel": "telegram",
        "command": command,
        "chatId": str(actor),
        "actorId": f"telegram:{actor}",
        "traceId": trace,
        "idempotencyKey": trace,
    }


def test_three_isolated_pilots_complete_provider_free_read_only_journey() -> None:
    random = DeterministicBytes()
    invite_table = MemoryInviteTable()
    invites = DynamoPilotInvites(
        invite_table,
        now=lambda: NOW_SECONDS,
        random_bytes=random,
        conditional_failure_types=(ConditionalFailure,),
    )
    queue = SyntheticQueue()
    ingress = TelegramWebhookIngress(
        secret_provider=lambda: "synthetic-webhook-secret",
        resolve_user=lambda *_args: (_ for _ in ()).throw(
            AssertionError("invite redemption must precede ordinary resolution")
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
    control = ControlApplication(
        tickets=tickets,
        gmail=SyntheticGmail(state),
        tasks=NoTasks(),
        deletion_intents=store,
        web_origin=ORIGIN,
        card_actions=SyntheticCardActions(),
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

    invite_tokens: list[str] = []
    sessions_by_user: dict[str, tuple[str, str]] = {}
    users_by_actor: dict[int, str] = {}
    source_by_user: dict[str, str] = {}

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

        welcome = control.handle(_control_request(wire))
        link = next(
            line for line in welcome["text"].splitlines() if line.startswith(ORIGIN)
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

        scan = control.handle(
            _command(user_id, actor, "/scan", 2_000 + offset)
        )
        assert "No button sends email" in scan["text"]
        source = state.opportunities[user_id][0].source.deep_link
        source_by_user[user_id] = source
        assert source in scan["text"]
        edit_callback = scan["telegram"]["inlineKeyboard"][0][0]["callbackData"]
        trace = derive_event_trace("telegram", user_id, str(3_000 + offset))
        edit = control.handle(
            {
                "action": "telegramCallback",
                "userId": user_id,
                "channel": "telegram",
                "chatId": str(actor),
                "actorId": f"telegram:{actor}",
                "callbackData": edit_callback,
                "traceId": trace,
                "idempotencyKey": trace,
            }
        )
        assert "Nothing was sent" in edit["text"]
        draft_link = next(
            line for line in edit["text"].splitlines() if line.startswith(ORIGIN)
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
    assert state.effect_calls == []

    users = list(users_by_actor.values())
    first_cookie, first_csrf = sessions_by_user[users[0]]
    second_cookie, _ = sessions_by_user[users[1]]
    cross_user_ticket = tickets.issue(
        user_id=users[1],
        return_path="/workspace",
    )
    denied = web.handle(
        _web_event(
            "POST",
            "/api/session/connect",
            cookie=first_cookie,
            body={"ticket": cross_user_ticket},
        )
    )
    assert denied["statusCode"] == 400
    accepted = web.handle(
        _web_event(
            "POST",
            "/api/session/connect",
            cookie=second_cookie,
            body={"ticket": cross_user_ticket},
        )
    )
    assert accepted["statusCode"] == 201
    sessions_by_user[users[1]] = (
        accepted["headers"]["Set-Cookie"],
        json.loads(accepted["body"])["csrfToken"],
    )

    disconnected = web.handle(
        _web_event(
            "POST",
            f"/api/connections/{READONLY_PROVIDER}/disconnect",
            cookie=first_cookie,
            csrf=first_csrf,
            body={},
        )
    )
    assert json.loads(disconnected["body"])["status"] == "DISCONNECTED"
    first_overview = web.handle(
        _web_event("GET", "/api/overview", cookie=first_cookie)
    )
    assert json.loads(first_overview["body"])["connection"]["status"] == (
        "DISCONNECTED"
    )

    second_cookie, second_csrf = sessions_by_user[users[1]]
    logged_out = web.handle(
        _web_event(
            "POST",
            "/api/session/logout",
            cookie=second_cookie,
            csrf=second_csrf,
            body={},
        )
    )
    assert logged_out["statusCode"] == 204
    assert web.handle(
        _web_event("GET", "/api/overview", cookie=second_cookie)
    )["statusCode"] == 401

    third_cookie, third_csrf = sessions_by_user[users[2]]
    deletion_requested = web.handle(
        _web_event(
            "POST",
            "/api/delete",
            cookie=third_cookie,
            csrf=third_csrf,
            body={"confirm": "DELETE"},
        )
    )
    assert deletion_requested["statusCode"] == 202
    assert web.handle(
        _web_event("GET", "/api/overview", cookie=third_cookie)
    )["statusCode"] == 401
    assert web.handle(
        _web_event("GET", "/api/overview", cookie=first_cookie)
    )["statusCode"] == 200

    store.now_ms += DeletionCoordinator.FINALIZATION_GRACE_MS - 1
    assert deletion.reconcile(users[2]) == {
        "status": "pending",
        "userId": users[2],
    }
    store.now_ms += 1
    assert deletion.reconcile(users[2]) == {
        "status": "deleted",
        "userId": users[2],
    }
    assert state.effect_calls == []
