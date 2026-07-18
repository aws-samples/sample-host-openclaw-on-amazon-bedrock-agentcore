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

    token = tickets.issue(user_id="user_founder", ttl_seconds=300)

    assert token.startswith("poct1.")
    assert tickets.consume(token) == "user_founder"
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
    token = tickets.issue(user_id="user_founder", ttl_seconds=60)
    with pytest.raises(ConnectTicketError, match="signature"):
        tickets.consume(token[:-1] + ("A" if token[-1] != "A" else "B"))

    token = tickets.issue(user_id="user_founder", ttl_seconds=60)
    clock.value += 61
    with pytest.raises(ConnectTicketError, match="expired"):
        tickets.consume(token)

    clock.value -= 61
    token = tickets.issue(user_id="user_founder", ttl_seconds=60)
    only = next(reversed(store.items.values()))
    only["userId"] = "user_attacker"
    with pytest.raises(ConnectTicketError, match="binding"):
        tickets.consume(token)


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
