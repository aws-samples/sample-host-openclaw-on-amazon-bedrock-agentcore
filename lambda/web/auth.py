"""Signed connect tickets and opaque browser sessions for the trusted web UI."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import re
import time
from typing import Callable, Mapping


_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_OPAQUE = re.compile(r"[A-Za-z0-9_-]{32,256}")
_COOKIE_NAME = "__Host-po_session"


class ConnectTicketError(ValueError):
    pass


class AuthenticationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class IssuedSession:
    session_token: str
    csrf_token: str
    cookie: str
    expires_at: int


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    user_id: str
    session_key: str
    expires_at: int


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64url(value: str) -> bytes:
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as error:
        raise ConnectTicketError("connect ticket encoding is invalid") from error


def _user_id(value: object) -> str:
    if not isinstance(value, str) or _USER_ID.fullmatch(value) is None:
        raise ValueError("user identity is invalid")
    return value


def _secret(value: object) -> bytes:
    if not isinstance(value, bytes) or len(value) < 32:
        raise ValueError("signing secret must contain at least 32 bytes")
    return value


def _digest(secret: bytes, purpose: bytes, value: str) -> str:
    return hmac.new(secret, purpose + b"\0" + value.encode(), hashlib.sha256).hexdigest()


class SignedConnectTickets:
    """Issue one-time Telegram-to-web tickets without putting identity in URLs unsigned."""

    def __init__(
        self,
        *,
        secret: bytes,
        store,
        now: Callable[[], int] | None = None,
        random_bytes: Callable[[int], bytes] | None = None,
    ) -> None:
        self._secret = _secret(secret)
        self._store = store
        self._now = now or (lambda: int(time.time()))
        self._random = random_bytes or os.urandom

    def issue(self, *, user_id: str, ttl_seconds: int = 300) -> str:
        user_id = _user_id(user_id)
        if isinstance(ttl_seconds, bool) or not 60 <= ttl_seconds <= 600:
            raise ValueError("connect ticket TTL must be between 60 and 600 seconds")
        now = int(self._now())
        jti = _b64url(self._random(24))
        nonce = _b64url(self._random(24))
        payload = {
            "exp": now + ttl_seconds,
            "iat": now,
            "jti": jti,
            "nonce": nonce,
            "typ": "connect",
            "uid": user_id,
            "v": 1,
        }
        encoded = _b64url(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
        signed = f"poct1.{encoded}"
        signature = _b64url(hmac.new(self._secret, signed.encode(), hashlib.sha256).digest())
        token = f"{signed}.{signature}"
        key = _digest(self._secret, b"connect-jti", jti)
        self._store.put_once(
            key,
            {"userId": user_id, "nonce": nonce, "issuedAt": now},
            expires_at=payload["exp"],
        )
        return token

    def consume(self, token: object) -> str:
        if not isinstance(token, str) or len(token) > 2_048:
            raise ConnectTicketError("connect ticket is invalid")
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != "poct1":
            raise ConnectTicketError("connect ticket is invalid")
        expected = _b64url(
            hmac.new(
                self._secret,
                f"{parts[0]}.{parts[1]}".encode(),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(parts[2], expected):
            raise ConnectTicketError("connect ticket signature is invalid")
        try:
            payload = json.loads(_unb64url(parts[1]))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ConnectTicketError("connect ticket payload is invalid") from error
        required = {"exp", "iat", "jti", "nonce", "typ", "uid", "v"}
        if not isinstance(payload, dict) or set(payload) != required:
            raise ConnectTicketError("connect ticket payload is invalid")
        if payload.get("typ") != "connect" or payload.get("v") != 1:
            raise ConnectTicketError("connect ticket type is invalid")
        try:
            user_id = _user_id(payload["uid"])
        except ValueError as error:
            raise ConnectTicketError("connect ticket identity is invalid") from error
        now = int(self._now())
        if (
            isinstance(payload["iat"], bool)
            or not isinstance(payload["iat"], int)
            or isinstance(payload["exp"], bool)
            or not isinstance(payload["exp"], int)
            or payload["iat"] > now + 30
            or payload["exp"] <= now
            or payload["exp"] - payload["iat"] > 600
            or not isinstance(payload["jti"], str)
            or _OPAQUE.fullmatch(payload["jti"]) is None
            or not isinstance(payload["nonce"], str)
            or _OPAQUE.fullmatch(payload["nonce"]) is None
        ):
            raise ConnectTicketError("connect ticket expired or is invalid")
        key = _digest(self._secret, b"connect-jti", payload["jti"])
        record = self._store.pop_once(key)
        if not isinstance(record, Mapping):
            raise ConnectTicketError("connect ticket was already used or revoked")
        if (
            record.get("userId") != user_id
            or record.get("nonce") != payload["nonce"]
            or int(record.get("expiresAt", 0)) != payload["exp"]
        ):
            raise ConnectTicketError("connect ticket binding is invalid")
        return user_id


def _cookie_value(cookie_header: object) -> str:
    if not isinstance(cookie_header, str) or len(cookie_header) > 8_192:
        raise AuthenticationError("session cookie is missing")
    values: list[str] = []
    for field in cookie_header.split(";"):
        name, separator, value = field.strip().partition("=")
        if separator and name == _COOKIE_NAME:
            values.append(value)
    if len(values) != 1 or _OPAQUE.fullmatch(values[0]) is None:
        raise AuthenticationError("session cookie is invalid")
    return values[0]


class OpaqueSessionManager:
    def __init__(
        self,
        *,
        secret: bytes,
        store,
        now: Callable[[], int] | None = None,
        random_bytes: Callable[[int], bytes] | None = None,
    ) -> None:
        self._secret = _secret(secret)
        self._store = store
        self._now = now or (lambda: int(time.time()))
        self._random = random_bytes or os.urandom

    def issue(self, *, user_id: str, ttl_seconds: int = 86_400) -> IssuedSession:
        user_id = _user_id(user_id)
        if isinstance(ttl_seconds, bool) or not 60 <= ttl_seconds <= 604_800:
            raise ValueError("session TTL must be between one minute and seven days")
        now = int(self._now())
        session_token = _b64url(self._random(32))
        csrf_token = _b64url(self._random(32))
        key = _digest(self._secret, b"session", session_token)
        expires_at = now + ttl_seconds
        self._store.create(
            key,
            {
                "userId": user_id,
                "csrfDigest": _digest(self._secret, b"csrf", csrf_token),
                "createdAt": now,
                "revoked": False,
            },
            expires_at=expires_at,
        )
        return IssuedSession(
            session_token=session_token,
            csrf_token=csrf_token,
            cookie=(
                f"{_COOKIE_NAME}={session_token}; Path=/; Secure; HttpOnly; "
                "SameSite=Lax"
            ),
            expires_at=expires_at,
        )

    def authenticate(
        self,
        *,
        cookie_header: object,
        csrf_token: object = None,
        require_csrf: bool = False,
    ) -> SessionIdentity:
        session_token = _cookie_value(cookie_header)
        key = _digest(self._secret, b"session", session_token)
        record = self._store.get(key)
        if not isinstance(record, Mapping):
            raise AuthenticationError("session is missing")
        if record.get("revoked") is True:
            raise AuthenticationError("session is revoked")
        now = int(self._now())
        expires_at = record.get("expiresAt")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at <= now:
            raise AuthenticationError("session expired")
        try:
            user_id = _user_id(record.get("userId"))
        except ValueError as error:
            raise AuthenticationError("session identity is invalid") from error
        if require_csrf:
            if not isinstance(csrf_token, str) or _OPAQUE.fullmatch(csrf_token) is None:
                raise AuthenticationError("CSRF token is missing or invalid")
            supplied = _digest(self._secret, b"csrf", csrf_token)
            expected = record.get("csrfDigest")
            if not isinstance(expected, str) or not hmac.compare_digest(supplied, expected):
                raise AuthenticationError("CSRF token does not match session")
        return SessionIdentity(user_id=user_id, session_key=key, expires_at=expires_at)

    def revoke(self, *, cookie_header: object) -> None:
        session_token = _cookie_value(cookie_header)
        self._store.revoke(_digest(self._secret, b"session", session_token))
