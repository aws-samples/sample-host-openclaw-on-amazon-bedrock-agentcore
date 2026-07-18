from __future__ import annotations

from dataclasses import dataclass

import pytest

from .auth import (
    AuthenticationError,
    ConnectTicketError,
    OpaqueSessionManager,
    SignedConnectTickets,
)


class OneTimeStore:
    def __init__(self):
        self.items = {}

    def put_once(self, key, record, *, expires_at):
        if key in self.items:
            raise RuntimeError("duplicate")
        self.items[key] = {**record, "expiresAt": expires_at}

    def pop_once(self, key):
        return self.items.pop(key, None)


class SessionStore:
    def __init__(self):
        self.items = {}

    def create(self, key, record, *, expires_at):
        self.items[key] = {**record, "expiresAt": expires_at}

    def get(self, key):
        return self.items.get(key)

    def revoke(self, key):
        item = self.items.get(key)
        if item:
            item["revoked"] = True


@dataclass
class Clock:
    value: int = 1_700_000_000

    def __call__(self):
        return self.value


class DeterministicRandom:
    def __init__(self):
        self.counter = 0

    def __call__(self, length: int) -> bytes:
        self.counter += 1
        return bytes((index + self.counter) % 256 for index in range(length))


def test_connect_ticket_is_signed_user_bound_short_lived_and_one_time():
    clock = Clock()
    store = OneTimeStore()
    tickets = SignedConnectTickets(
        secret=b"t" * 32,
        store=store,
        now=clock,
        random_bytes=DeterministicRandom(),
    )

    token = tickets.issue(
        user_id="user_founder",
        return_path="/workspace",
        ttl_seconds=300,
    )

    assert token.startswith("poct2.")
    redemption = tickets.consume(token)
    assert redemption.user_id == "user_founder"
    assert redemption.return_path == "/workspace"
    with pytest.raises(ConnectTicketError, match="used"):
        tickets.consume(token)


def test_connect_ticket_rejects_tamper_expiry_and_wrong_stored_binding():
    clock = Clock()
    store = OneTimeStore()
    tickets = SignedConnectTickets(
        secret=b"t" * 32,
        store=store,
        now=clock,
        random_bytes=DeterministicRandom(),
    )
    token = tickets.issue(
        user_id="user_founder", return_path="/connections", ttl_seconds=300
    )
    with pytest.raises(ConnectTicketError, match="signature"):
        tickets.consume(token[:-1] + ("A" if token[-1] != "A" else "B"))

    token = tickets.issue(
        user_id="user_founder", return_path="/connections", ttl_seconds=300
    )
    clock.value += 301
    with pytest.raises(ConnectTicketError, match="expired"):
        tickets.consume(token)

    clock.value -= 301
    token = tickets.issue(
        user_id="user_founder", return_path="/connections", ttl_seconds=300
    )
    only = next(reversed(store.items.values()))
    only["userId"] = "user_attacker"
    with pytest.raises(ConnectTicketError, match="binding"):
        tickets.consume(token)


@pytest.mark.parametrize(
    "return_path",
    [
        "/",
        "/connections",
        "/workspace",
        "/export",
        "/delete",
        "/workspace?draft=draft_action_12345678",
    ],
)
def test_connect_ticket_v2_accepts_only_exact_pilot_return_paths(return_path):
    tickets = SignedConnectTickets(
        secret=b"t" * 32,
        store=OneTimeStore(),
        now=Clock(),
        random_bytes=DeterministicRandom(),
    )

    redemption = tickets.consume(
        tickets.issue(user_id="user_founder", return_path=return_path)
    )

    assert redemption.return_path == return_path


@pytest.mark.parametrize(
    "return_path",
    [
        "",
        "connections",
        "//attacker.example",
        "/../workspace",
        "/connections?next=https://attacker.example",
        "/workspace?draft=bad!draft",
        "/workspace?draft=draft_action_12345678&next=/delete",
        "/approve/signed-token",
    ],
)
def test_connect_ticket_v2_rejects_open_redirects_and_unallowlisted_paths(return_path):
    tickets = SignedConnectTickets(
        secret=b"t" * 32,
        store=OneTimeStore(),
        now=Clock(),
        random_bytes=DeterministicRandom(),
    )

    with pytest.raises(ValueError, match="return path"):
        tickets.issue(user_id="user_founder", return_path=return_path)


def test_connect_ticket_v2_cross_user_attempt_does_not_consume_ticket():
    tickets = SignedConnectTickets(
        secret=b"t" * 32,
        store=OneTimeStore(),
        now=Clock(),
        random_bytes=DeterministicRandom(),
    )
    token = tickets.issue(user_id="user_founder", return_path="/export")

    with pytest.raises(ConnectTicketError, match="identity"):
        tickets.consume(token, expected_user_id="user_attacker")

    redemption = tickets.consume(token, expected_user_id="user_founder")
    assert redemption.user_id == "user_founder"
    assert redemption.return_path == "/export"


def test_legacy_v1_ticket_drain_can_return_only_to_connections():
    tickets = SignedConnectTickets(
        secret=b"t" * 32,
        store=OneTimeStore(),
        now=Clock(),
        random_bytes=DeterministicRandom(),
    )

    token = tickets.issue_legacy_v1(user_id="user_founder")
    redemption = tickets.consume(token)

    assert token.startswith("poct1.")
    assert redemption.user_id == "user_founder"
    assert redemption.return_path == "/connections"


def test_session_cookie_is_opaque_host_only_secure_and_stores_only_digests():
    clock = Clock()
    store = SessionStore()
    sessions = OpaqueSessionManager(
        secret=b"s" * 32,
        store=store,
        now=clock,
        random_bytes=DeterministicRandom(),
    )

    issued = sessions.issue(user_id="user_founder", ttl_seconds=900)

    assert issued.cookie.startswith("__Host-po_session=")
    assert "; Path=/; Secure; HttpOnly; SameSite=Lax" in issued.cookie
    assert "user_founder" not in issued.cookie
    serialized = repr(store.items)
    assert issued.session_token not in serialized
    assert issued.csrf_token not in serialized
    identity = sessions.authenticate(
        cookie_header=issued.cookie,
        csrf_token=issued.csrf_token,
        require_csrf=True,
    )
    assert identity.user_id == "user_founder"


def test_mutations_require_matching_csrf_and_live_session_then_logout_revokes():
    clock = Clock()
    store = SessionStore()
    sessions = OpaqueSessionManager(
        secret=b"s" * 32,
        store=store,
        now=clock,
        random_bytes=DeterministicRandom(),
    )
    issued = sessions.issue(user_id="user_founder", ttl_seconds=60)

    for csrf in (None, "wrong"):
        with pytest.raises(AuthenticationError, match="CSRF"):
            sessions.authenticate(
                cookie_header=issued.cookie,
                csrf_token=csrf,
                require_csrf=True,
            )

    sessions.revoke(cookie_header=issued.cookie)
    with pytest.raises(AuthenticationError, match="revoked"):
        sessions.authenticate(cookie_header=issued.cookie)

    issued = sessions.issue(user_id="user_founder", ttl_seconds=60)
    clock.value += 61
    with pytest.raises(AuthenticationError, match="expired"):
        sessions.authenticate(cookie_header=issued.cookie)


def test_cookie_parser_rejects_duplicate_or_oversized_session_values():
    sessions = OpaqueSessionManager(
        secret=b"s" * 32,
        store=SessionStore(),
        now=Clock(),
        random_bytes=DeterministicRandom(),
    )

    with pytest.raises(AuthenticationError):
        sessions.authenticate(cookie_header="__Host-po_session=a; __Host-po_session=b")
    with pytest.raises(AuthenticationError):
        sessions.authenticate(cookie_header=f"__Host-po_session={'a' * 1000}")
