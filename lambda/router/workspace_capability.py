"""Trusted router-minted workspace capability contract.

The token is a short-lived bearer capability, not user input.  It binds one
canonical internal user to one exact AgentCore session.  Only trusted Lambdas
may read the signing key; the runtime sees the signed token but cannot mint or
widen it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable

try:
    from .runtime_state import canonical_session_id, canonical_user_id
except ImportError:  # focused tests and direct trusted Lambda asset
    from runtime_state import canonical_session_id, canonical_user_id


DOMAIN = b"personal-operator-workspace-capability-v1\0"
MAX_TOKEN_BYTES = 2_048
MAX_TTL_SECONDS = 9 * 60 * 60
DEFAULT_TTL_SECONDS = MAX_TTL_SECONDS
CLAIM_KEYS = frozenset(
    {"aud", "exp", "iat", "namespace", "sessionId", "sub", "v"}
)


class WorkspaceCapabilityError(ValueError):
    """The capability is malformed, forged, expired, or misbound."""


def _key(value: bytes | str) -> bytes:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    if not isinstance(encoded, bytes) or len(encoded) < 32:
        raise WorkspaceCapabilityError("capability signing key is invalid")
    return encoded


def _audience(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or not value.isascii()
        or any(character.isspace() for character in value)
    ):
        raise WorkspaceCapabilityError("capability audience is invalid")
    return value


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(segment: str) -> bytes:
    if not isinstance(segment, str) or not segment or "=" in segment:
        raise WorkspaceCapabilityError("capability encoding is invalid")
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (ValueError, UnicodeEncodeError) as error:
        raise WorkspaceCapabilityError("capability encoding is invalid") from error
    if _b64encode(raw) != segment:
        raise WorkspaceCapabilityError("capability encoding is not canonical")
    return raw


def _json_object(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise WorkspaceCapabilityError("capability has duplicate claims")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                WorkspaceCapabilityError("capability contains a non-finite value")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkspaceCapabilityError("capability payload is invalid") from error
    if not isinstance(value, dict) or set(value) != CLAIM_KEYS:
        raise WorkspaceCapabilityError("capability claim set is invalid")
    return value


class WorkspaceCapabilitySigner:
    def __init__(
        self,
        *,
        key_provider: Callable[[], bytes | str],
        audience: str,
        clock: Callable[[], int | float] = time.time,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        if not callable(key_provider) or not callable(clock):
            raise TypeError("capability key provider and clock must be callable")
        if (
            not isinstance(ttl_seconds, int)
            or not 1 <= ttl_seconds <= MAX_TTL_SECONDS
        ):
            raise ValueError("capability TTL is outside its fixed maximum")
        self._key_provider = key_provider
        self._audience = _audience(audience)
        self._clock = clock
        self._ttl_seconds = ttl_seconds

    def mint(self, *, user_id: str, session_id: str) -> str:
        user_id = canonical_user_id(user_id)
        session_id = canonical_session_id(session_id)
        now = int(self._clock())
        claims = {
            "aud": self._audience,
            "exp": now + self._ttl_seconds,
            "iat": now,
            "namespace": user_id,
            "sessionId": session_id,
            "sub": user_id,
            "v": 1,
        }
        payload = _b64encode(
            json.dumps(
                claims, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
        )
        signature = hmac.new(
            _key(self._key_provider()), DOMAIN + payload.encode("ascii"), hashlib.sha256
        ).digest()
        token = f"{payload}.{_b64encode(signature)}"
        if len(token.encode("ascii")) > MAX_TOKEN_BYTES:
            raise WorkspaceCapabilityError("capability exceeds its size bound")
        return token


def verify_workspace_capability(
    token: str,
    *,
    key: bytes | str,
    audience: str,
    now: int | float | None = None,
) -> dict:
    if (
        not isinstance(token, str)
        or not token.isascii()
        or not 1 <= len(token.encode("ascii")) <= MAX_TOKEN_BYTES
        or token.count(".") != 1
    ):
        raise WorkspaceCapabilityError("capability token is invalid")
    payload, supplied_signature = token.split(".")
    expected_signature = hmac.new(
        _key(key), DOMAIN + payload.encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(_b64decode(supplied_signature), expected_signature):
        raise WorkspaceCapabilityError("capability signature is invalid")

    claims = _json_object(_b64decode(payload))
    try:
        user_id = canonical_user_id(claims["sub"])
        namespace = canonical_user_id(claims["namespace"])
        session_id = canonical_session_id(claims["sessionId"])
    except (TypeError, ValueError) as error:
        raise WorkspaceCapabilityError("capability identity is invalid") from error
    if namespace != user_id:
        raise WorkspaceCapabilityError("capability namespace is misbound")
    if claims["aud"] != _audience(audience):
        raise WorkspaceCapabilityError("capability audience is invalid")
    if claims["v"] != 1:
        raise WorkspaceCapabilityError("capability version is invalid")
    issued_at = claims["iat"]
    expires_at = claims["exp"]
    if (
        not isinstance(issued_at, int)
        or isinstance(issued_at, bool)
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or expires_at <= issued_at
        or expires_at - issued_at > MAX_TTL_SECONDS
    ):
        raise WorkspaceCapabilityError("capability lifetime is invalid")
    current = int(time.time() if now is None else now)
    if issued_at > current + 30:
        raise WorkspaceCapabilityError("capability is not active")
    if expires_at <= current:
        raise WorkspaceCapabilityError("capability expired")
    return {
        "aud": claims["aud"],
        "exp": expires_at,
        "iat": issued_at,
        "namespace": namespace,
        "sessionId": session_id,
        "sub": user_id,
        "v": 1,
    }
