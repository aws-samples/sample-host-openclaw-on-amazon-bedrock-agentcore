from __future__ import annotations

import base64
import json
from urllib.parse import urlencode

import pytest

from .auth import OpaqueSessionManager, SignedConnectTickets
from .index import WebApplication
from .test_auth import Clock, DeterministicRandom, OneTimeStore, SessionStore


USER = "user_founder"
ORIGIN = "https://app.personal-operator.example"


class OAuth:
    def __init__(self):
        self.calls = []

    def start(self, *, user_id, redirect_uri):
        self.calls.append(("start", user_id, redirect_uri))
        return type("Authorization", (), {"url": "https://accounts.google.test/auth"})()

    def complete(self, *, user_id, state, code):
        self.calls.append(("complete", user_id, state, code))


class Approvals:
    def __init__(self):
        self.calls = []

    def preview(self, *, token, acting_user_id):
        self.calls.append(("preview", token, acting_user_id))
        return {"actionId": "action_12345678", "userId": acting_user_id}

    def approve(self, **kwargs):
        self.calls.append(("approve", kwargs))
        return {"state": "APPROVED"}

    def reject(self, **kwargs):
        self.calls.append(("reject", kwargs))
        return {"state": "REJECTED"}


class Workspace:
    def get(self, user_id):
        return {"userId": user_id, "files": ["memory.md"]}


class GmailWorkspace:
    def __init__(self):
        self.calls = []

    def get(self, user_id):
        self.calls.append(("get", user_id))
        return {
            "userId": user_id,
            "opportunities": [{"id": "opp_12345678", "title": "Ada is waiting"}],
            "drafts": [
                {
                    "actionId": "draft_action_12345678",
                    "revision": 1,
                    "to": "ada@example.com",
                    "subject": "A reply",
                    "body": "Hello Ada",
                    "payloadHash": "a" * 64,
                }
            ],
        }

    def edit_draft(self, **kwargs):
        self.calls.append(("edit", kwargs))
        return {
            "draft": {
                "actionId": kwargs["action_id"],
                "revision": kwargs["revision"] + 1,
                "to": "ada@example.com",
                "subject": kwargs["subject"],
                "body": kwargs["body"],
                "payloadHash": "b" * 64,
            }
        }


class Exporter:
    def build_zip(self, user_id):
        assert user_id == USER
        return b"PK\x03\x04synthetic"


class Deletion:
    def __init__(self):
        self.calls = []

    def delete(self, user_id):
        self.calls.append(user_id)
        return {"status": "deleted", "userId": user_id}


class Retention:
    def __init__(self):
        self.calls = 0

    def sweep(self):
        self.calls += 1
        return {"status": "ok", "expired": 3}


class Overview:
    def __init__(self):
        self.calls = []

    def get(self, user_id):
        self.calls.append(user_id)
        return {
            "version": "personal-operator.pilot-overview.v1",
            "externalEffects": False,
            "connection": {
                "provider": "google-gmail-readonly",
                "status": "CONNECTED",
                "access": "READ_ONLY",
            },
        }


class Connections:
    def __init__(self):
        self.calls = []

    def disconnect(self, user_id):
        self.calls.append(user_id)
        return "DISCONNECTED"


class Scans:
    def __init__(self):
        self.calls = []

    def feedback(self, user_id, scan_id, *, response):
        self.calls.append((user_id, scan_id, response))
        return {
            "scanId": scan_id,
            "status": "SUCCEEDED",
            "feedback": response,
        }


def event(method, path, *, body=None, cookie=None, csrf=None, query=None, origin=ORIGIN):
    headers = {"origin": origin} if origin is not None else {}
    if cookie:
        headers["cookie"] = cookie
    if csrf:
        headers["x-po-csrf"] = csrf
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "rawPath": path,
        "rawQueryString": urlencode(query or {}),
        "headers": headers,
        "queryStringParameters": query or {},
        "body": json.dumps(body) if body is not None else "",
        "isBase64Encoded": False,
    }


def setup_app():
    clock = Clock()
    random = DeterministicRandom()
    ticket_store = OneTimeStore()
    session_store = SessionStore()
    tickets = SignedConnectTickets(
        secret=b"t" * 32,
        store=ticket_store,
        now=clock,
        random_bytes=random,
    )
    sessions = OpaqueSessionManager(
        secret=b"s" * 32,
        store=session_store,
        now=clock,
        random_bytes=random,
    )
    oauth = OAuth()
    approvals = Approvals()
    deletion = Deletion()
    retention = Retention()
    gmail_workspace = GmailWorkspace()
    overview = Overview()
    connections = Connections()
    scans = Scans()
    app = WebApplication(
        tickets=tickets,
        sessions=sessions,
        oauth=oauth,
        approvals=approvals,
        workspace=Workspace(),
        gmail_workspace=gmail_workspace,
        exporter=Exporter(),
        deletion=deletion,
        retention=retention,
        overview=overview,
        connections=connections,
        scans=scans,
        web_origin=ORIGIN,
        google_redirect_uri=f"{ORIGIN}/oauth/google/callback",
    )
    return app, tickets, oauth, approvals, deletion, gmail_workspace, retention


def bootstrap(app, tickets):
    token = tickets.issue(user_id=USER, return_path="/connections")
    response = app.handle(event("POST", "/api/session/connect", body={"ticket": token}))
    assert response["statusCode"] == 201
    payload = json.loads(response["body"])
    return response["headers"]["Set-Cookie"], payload["csrfToken"]


def test_connect_bootstrap_consumes_ticket_and_returns_secure_session_once():
    app, tickets, *_ = setup_app()
    token = tickets.issue(user_id=USER, return_path="/workspace")

    response = app.handle(event("POST", "/api/session/connect", body={"ticket": token}))

    assert response["statusCode"] == 201
    assert response["headers"]["Set-Cookie"].startswith("__Host-po_session=")
    payload = json.loads(response["body"])
    assert payload["csrfToken"]
    assert payload["returnPath"] == "/workspace"
    assert app.handle(
        event("POST", "/api/session/connect", body={"ticket": token})
    )["statusCode"] == 400


def test_connect_ticket_cannot_replace_a_different_users_live_session():
    app, tickets, *_ = setup_app()
    founder_cookie, _ = bootstrap(app, tickets)
    attacker_token = tickets.issue(
        user_id="user_attacker",
        return_path="/delete",
    )

    denied = app.handle(
        event(
            "POST",
            "/api/session/connect",
            body={"ticket": attacker_token},
            cookie=founder_cookie,
        )
    )

    assert denied["statusCode"] == 400
    retried = app.handle(
        event(
            "POST",
            "/api/session/connect",
            body={"ticket": attacker_token},
        )
    )
    assert retried["statusCode"] == 201
    assert json.loads(retried["body"])["returnPath"] == "/delete"


@pytest.mark.parametrize("stale_kind", ["expired", "revoked"])
def test_connect_ticket_recovers_from_an_expired_or_revoked_cookie(stale_kind):
    app, tickets, *_ = setup_app()
    stale_cookie, _ = bootstrap(app, tickets)
    if stale_kind == "expired":
        app._sessions._now.value += 86_401
    else:
        app._sessions.revoke(cookie_header=stale_cookie)
    token = tickets.issue(user_id="user_recovered", return_path="/workspace")

    response = app.handle(
        event(
            "POST",
            "/api/session/connect",
            body={"ticket": token},
            cookie=stale_cookie,
        )
    )

    assert response["statusCode"] == 201
    replacement = response["headers"]["Set-Cookie"]
    assert replacement != stale_cookie
    assert json.loads(response["body"])["returnPath"] == "/workspace"
    assert app._sessions.authenticate(cookie_header=replacement).user_id == (
        "user_recovered"
    )


def test_connect_ticket_preserves_store_outages_and_is_not_consumed():
    app, tickets, *_ = setup_app()
    incumbent_cookie, _ = bootstrap(app, tickets)
    token = tickets.issue(user_id=USER, return_path="/export")
    original_get = app._sessions._store.get
    app._sessions._store.get = lambda _key: (_ for _ in ()).throw(
        RuntimeError("store unavailable")
    )

    failed = app.handle(
        event(
            "POST",
            "/api/session/connect",
            body={"ticket": token},
            cookie=incumbent_cookie,
        )
    )

    assert failed["statusCode"] == 409
    app._sessions._store.get = original_get
    retried = app.handle(
        event("POST", "/api/session/connect", body={"ticket": token})
    )
    assert retried["statusCode"] == 201
    assert json.loads(retried["body"])["returnPath"] == "/export"


def test_legacy_v1_connect_ticket_drain_returns_only_to_connections():
    app, tickets, *_ = setup_app()
    token = tickets.issue_legacy_v1(user_id=USER)

    response = app.handle(
        event("POST", "/api/session/connect", body={"ticket": token})
    )

    assert response["statusCode"] == 201
    assert json.loads(response["body"])["returnPath"] == "/connections"


def test_oauth_pkce_flow_is_bound_to_authenticated_browser_session():
    app, tickets, oauth, *_ = setup_app()
    cookie, _ = bootstrap(app, tickets)

    started = app.handle(event("GET", "/oauth/google/start", cookie=cookie))
    assert started["statusCode"] == 302
    assert started["headers"]["Location"] == "https://accounts.google.test/auth"
    assert oauth.calls == [
        ("start", USER, f"{ORIGIN}/oauth/google/callback")
    ]

    completed = app.handle(
        event(
            "GET",
            "/oauth/google/callback",
            cookie=cookie,
            query={"state": "state-1", "code": "code-1"},
        )
    )
    assert completed["statusCode"] == 302
    assert completed["headers"]["Location"] == f"{ORIGIN}/connections?google=connected"
    assert oauth.calls[-1] == ("complete", USER, "state-1", "code-1")


def test_overview_is_authenticated_and_external_effects_are_always_false():
    app, tickets, *_ = setup_app()
    assert app.handle(event("GET", "/api/overview"))["statusCode"] == 401
    cookie, _ = bootstrap(app, tickets)

    response = app.handle(event("GET", "/api/overview", cookie=cookie))

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["externalEffects"] is False
    assert payload["connection"]["access"] == "READ_ONLY"
    assert app._overview.calls == [USER]


def test_disconnect_is_csrf_bound_local_and_idempotent():
    app, tickets, *_ = setup_app()
    cookie, csrf = bootstrap(app, tickets)

    missing_csrf = app.handle(
        event(
            "POST",
            "/api/connections/google-gmail-readonly/disconnect",
            body={},
            cookie=cookie,
        )
    )
    assert missing_csrf["statusCode"] == 401

    for _ in range(2):
        response = app.handle(
            event(
                "POST",
                "/api/connections/google-gmail-readonly/disconnect",
                body={},
                cookie=cookie,
                csrf=csrf,
            )
        )
        assert response["statusCode"] == 200
        assert json.loads(response["body"]) == {
            "provider": "google-gmail-readonly",
            "status": "DISCONNECTED",
            "remoteGrantRevoked": False,
        }
    assert app._connections.calls == [USER, USER]


def test_logout_revokes_only_the_current_session_and_clears_cookie():
    app, tickets, *_ = setup_app()
    cookie, csrf = bootstrap(app, tickets)

    response = app.handle(
        event(
            "POST",
            "/api/session/logout",
            body={},
            cookie=cookie,
            csrf=csrf,
        )
    )

    assert response["statusCode"] == 204
    assert response["headers"]["Set-Cookie"] == (
        "__Host-po_session=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0"
    )
    assert app.handle(event("GET", "/api/overview", cookie=cookie))["statusCode"] == 401


def test_bounded_disconnect_reports_pending_truthfully_and_completes_on_retry():
    from .overview import ConnectionDisconnectPending

    class BoundedConnections:
        def __init__(self):
            self.calls = 0

        def disconnect(self, user_id):
            assert user_id == USER
            self.calls += 1
            if self.calls == 1:
                raise ConnectionDisconnectPending("another bounded pass required")
            return "DISCONNECTED"

    app, tickets, *_ = setup_app()
    cookie, csrf = bootstrap(app, tickets)
    app._connections = BoundedConnections()

    pending = app.handle(
        event(
            "POST",
            "/api/connections/google-gmail-readonly/disconnect",
            body={},
            cookie=cookie,
            csrf=csrf,
        )
    )
    assert pending["statusCode"] == 202
    # The purge is incomplete: the response must not claim the account is
    # disconnected, or the UI would drop the retry path and strand the fence.
    assert json.loads(pending["body"]) == {
        "provider": "google-gmail-readonly",
        "status": "DISCONNECTING",
        "remoteGrantRevoked": False,
    }

    done = app.handle(
        event(
            "POST",
            "/api/connections/google-gmail-readonly/disconnect",
            body={},
            cookie=cookie,
            csrf=csrf,
        )
    )
    assert done["statusCode"] == 200
    assert json.loads(done["body"]) == {
        "provider": "google-gmail-readonly",
        "status": "DISCONNECTED",
        "remoteGrantRevoked": False,
    }


def test_logout_still_clears_cookie_after_applied_but_response_lost_revocation():
    from .stores import DynamoWebStore
    from .test_stores import Table

    class ResponseLostTable(Table):
        def __init__(self):
            super().__init__()
            self.lost_once = True

        def update_item(self, **kwargs):
            result = super().update_item(**kwargs)
            if (
                self.items[(kwargs["Key"]["PK"], kwargs["Key"]["SK"])]["SK"]
                == "SESSION"
                and self.lost_once
            ):
                self.lost_once = False
                raise TimeoutError("session revocation response was lost")
            return result

    clock = Clock()
    random = DeterministicRandom()
    store = DynamoWebStore(ResponseLostTable())
    tickets = SignedConnectTickets(
        secret=b"t" * 32, store=store, now=clock, random_bytes=random
    )
    sessions = OpaqueSessionManager(
        secret=b"s" * 32, store=store, now=clock, random_bytes=random
    )
    app = WebApplication(
        tickets=tickets,
        sessions=sessions,
        oauth=OAuth(),
        approvals=Approvals(),
        workspace=Workspace(),
        gmail_workspace=GmailWorkspace(),
        exporter=Exporter(),
        deletion=Deletion(),
        retention=Retention(),
        overview=Overview(),
        connections=Connections(),
        scans=Scans(),
        web_origin=ORIGIN,
        google_redirect_uri=f"{ORIGIN}/oauth/google/callback",
    )
    token = tickets.issue(user_id=USER, return_path="/connections")
    connect = app.handle(event("POST", "/api/session/connect", body={"ticket": token}))
    cookie = connect["headers"]["Set-Cookie"]
    csrf = json.loads(connect["body"])["csrfToken"]

    response = app.handle(
        event("POST", "/api/session/logout", body={}, cookie=cookie, csrf=csrf)
    )

    # The revocation write committed even though its response was lost, so the
    # endpoint still returns the cookie-clearing response and the now-revoked
    # session cannot be reused.
    assert response["statusCode"] == 204
    assert response["headers"]["Set-Cookie"] == (
        "__Host-po_session=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0"
    )
    assert app.handle(event("GET", "/api/overview", cookie=cookie))["statusCode"] == 401


def test_scan_feedback_is_csrf_bound_and_accepts_only_one_bounded_bit():
    app, tickets, *_ = setup_app()
    cookie, csrf = bootstrap(app, tickets)
    scan_id = "scan_00000000001700000000_" + "s" * 32

    rejected = app.handle(
        event(
            "POST",
            f"/api/scans/{scan_id}/feedback",
            body={"response": "Ada was useful"},
            cookie=cookie,
            csrf=csrf,
        )
    )
    assert rejected["statusCode"] == 400

    response = app.handle(
        event(
            "POST",
            f"/api/scans/{scan_id}/feedback",
            body={"response": "USEFUL"},
            cookie=cookie,
            csrf=csrf,
        )
    )

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {
        "scanId": scan_id,
        "feedback": "USEFUL",
    }
    assert app._scans.calls == [(USER, scan_id, "USEFUL")]


def test_oauth_callback_rejects_duplicate_or_malformed_raw_query_before_oauth():
    app, tickets, oauth, *_ = setup_app()
    cookie, _ = bootstrap(app, tickets)

    for raw_query in (
        "state=first&%73tate=second&code=code-1",
        "state=%ZZ&code=code-1",
        "state=state-1&code",
    ):
        request = event(
            "GET",
            "/oauth/google/callback",
            cookie=cookie,
            query={"state": "collapsed-state", "code": "collapsed-code"},
        )
        request["rawQueryString"] = raw_query

        assert app.handle(request)["statusCode"] == 400

    assert [call for call in oauth.calls if call[0] == "complete"] == []


def test_safe_get_navigation_may_omit_origin_but_mutations_may_not():
    app, tickets, oauth, *_ = setup_app()
    cookie, csrf = bootstrap(app, tickets)

    started = app.handle(
        event("GET", "/oauth/google/start", cookie=cookie, origin=None)
    )
    assert started["statusCode"] == 302
    assert oauth.calls[-1][0] == "start"
    denied = app.handle(
        event(
            "POST",
            "/api/delete",
            cookie=cookie,
            csrf=csrf,
            body={"confirm": "DELETE"},
            origin=None,
        )
    )
    assert denied["statusCode"] == 403


def test_exact_scheduled_retention_event_bypasses_http_auth_only_for_sweep():
    app, *_, retention = setup_app()
    scheduled = {
        "detail-type": "ScheduledRetentionSweep",
        "source": "personal-operator.retention",
        "version": 1,
    }

    assert app.handle(scheduled) == {"status": "ok", "expired": 3}
    assert retention.calls == 1
    assert app.handle({**scheduled, "version": 2})["statusCode"] == 400


def test_gmail_workspace_read_and_draft_edit_are_session_and_revision_scoped():
    app, tickets, _, _, _, gmail_workspace, _ = setup_app()
    cookie, csrf = bootstrap(app, tickets)

    workspace = app.handle(event("GET", "/api/gmail", cookie=cookie))
    assert workspace["statusCode"] == 200
    assert json.loads(workspace["body"])["userId"] == USER
    assert gmail_workspace.calls == [("get", USER)]

    edit = app.handle(
        event(
            "POST",
            "/api/gmail/drafts/draft_action_12345678",
            cookie=cookie,
            csrf=csrf,
            body={
                "revision": 1,
                "subject": "Updated subject",
                "body": "Updated exact body",
            },
        )
    )
    assert edit["statusCode"] == 200
    assert json.loads(edit["body"])["draft"]["revision"] == 2
    assert gmail_workspace.calls[-1] == (
        "edit",
        {
            "user_id": USER,
            "action_id": "draft_action_12345678",
            "revision": 1,
            "subject": "Updated subject",
            "body": "Updated exact body",
        },
    )


def test_gmail_draft_edit_requires_csrf_exact_body_and_valid_action_path():
    app, tickets, _, _, _, gmail_workspace, _ = setup_app()
    cookie, csrf = bootstrap(app, tickets)
    path = "/api/gmail/drafts/draft_action_12345678"
    body = {"revision": 1, "subject": "Updated", "body": "Updated body"}

    assert app.handle(event("POST", path, cookie=cookie, body=body))["statusCode"] == 401
    assert app.handle(
        event(
            "POST",
            path,
            cookie=cookie,
            csrf=csrf,
            body={**body, "to": "attacker@example.com"},
        )
    )["statusCode"] == 400
    assert app.handle(
        event(
            "POST",
            "/api/gmail/drafts/bad!action",
            cookie=cookie,
            csrf=csrf,
            body=body,
        )
    )["statusCode"] == 404
    assert gmail_workspace.calls == []


def test_approval_get_only_previews_and_post_requires_csrf_and_same_session_user():
    app, tickets, _, approvals, _, _, _ = setup_app()
    cookie, csrf = bootstrap(app, tickets)

    preview = app.handle(event("GET", "/approve/signed-token", cookie=cookie))
    assert preview["statusCode"] == 200
    assert approvals.calls == [("preview", "signed-token", USER)]

    body = {"token": "signed-token", "revision": 2, "args": {"to": "a@example.com"}}
    denied = app.handle(
        event("POST", "/api/actions/action_12345678/approve", cookie=cookie, body=body)
    )
    assert denied["statusCode"] == 401
    assert [call for call in approvals.calls if call[0] == "approve"] == []

    approved = app.handle(
        event(
            "POST",
            "/api/actions/action_12345678/approve",
            cookie=cookie,
            csrf=csrf,
            body=body,
        )
    )
    assert approved["statusCode"] == 200
    call = approvals.calls[-1]
    assert call[0] == "approve"
    assert call[1]["acting_user_id"] == USER


def test_reject_export_workspace_and_confirmed_delete_are_session_scoped():
    app, tickets, _, approvals, deletion, _, _ = setup_app()
    cookie, csrf = bootstrap(app, tickets)

    workspace = app.handle(event("GET", "/api/workspace", cookie=cookie))
    assert json.loads(workspace["body"])["userId"] == USER

    archive = app.handle(event("GET", "/api/export", cookie=cookie))
    assert archive["isBase64Encoded"] is True
    assert base64.b64decode(archive["body"]) == b"PK\x03\x04synthetic"

    rejected = app.handle(
        event(
            "POST",
            "/api/actions/action_12345678/reject",
            cookie=cookie,
            csrf=csrf,
            body={"revision": 2},
        )
    )
    assert rejected["statusCode"] == 200
    assert approvals.calls[-1][1]["acting_user_id"] == USER

    assert app.handle(
        event("POST", "/api/delete", cookie=cookie, csrf=csrf, body={"confirm": "no"})
    )["statusCode"] == 400
    deleted = app.handle(
        event(
            "POST",
            "/api/delete",
            cookie=cookie,
            csrf=csrf,
            body={"confirm": "DELETE"},
        )
    )
    assert deleted["statusCode"] == 200
    assert deletion.calls == [USER]


def test_origin_body_shape_method_and_unknown_routes_fail_closed():
    app, tickets, *_ = setup_app()
    cookie, csrf = bootstrap(app, tickets)
    wrong_origin = event("GET", "/api/workspace", cookie=cookie)
    wrong_origin["headers"]["origin"] = "https://attacker.example"
    assert app.handle(wrong_origin)["statusCode"] == 403
    assert app.handle(event("PUT", "/api/workspace", cookie=cookie))["statusCode"] == 405
    assert app.handle(event("GET", "/not-real", cookie=cookie))["statusCode"] == 404
    malformed = event("POST", "/api/delete", cookie=cookie, csrf=csrf)
    malformed["body"] = "{bad"
    assert app.handle(malformed)["statusCode"] == 400


def test_nested_duplicate_json_keys_are_rejected_before_approval_dispatch():
    app, tickets, _, approvals, _, _, _ = setup_app()
    cookie, csrf = bootstrap(app, tickets)
    request = event(
        "POST",
        "/api/actions/action_12345678/approve",
        cookie=cookie,
        csrf=csrf,
    )
    request["body"] = (
        '{"token":"signed-token","revision":2,'
        '"args":{"to":"safe@example.com","to":"attacker@example.com"}}'
    )

    assert app.handle(request)["statusCode"] == 400
    assert [call for call in approvals.calls if call[0] == "approve"] == []
