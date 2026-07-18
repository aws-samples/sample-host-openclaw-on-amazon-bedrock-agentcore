"""Google read-only OAuth and KMS envelope storage primitives.

The module contains no web framework or global AWS clients. Callers provide a
one-time state store, the token endpoint client, and persistence adapters.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from typing import Callable, Iterable, Mapping
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen as _stdlib_urlopen


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
STATE_TTL = timedelta(minutes=10)
MAX_TOKEN_RESPONSE_BYTES = 64 * 1024


class OAuthStateError(ValueError):
    pass


class OAuthScopeError(ValueError):
    pass


class TokenEnvelopeError(ValueError):
    pass


class OAuthTokenProviderError(RuntimeError):
    """A token endpoint failure with no provider or credential detail."""


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _bounded_text(value: str, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        raise ValueError(f"{label} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    url: str
    state: str
    code_challenge: str
    code_challenge_method: str = "S256"


class GoogleReadonlyOAuthFlow:
    def __init__(
        self,
        *,
        state_store,
        token_client,
        token_vault,
        client_id: str,
        authorization_endpoint: str,
        allowed_redirect_uris: Iterable[str],
        now: Callable[[], datetime] | None = None,
        random_bytes: Callable[[int], bytes] | None = None,
    ) -> None:
        self._state_store = state_store
        self._token_client = token_client
        self._token_vault = token_vault
        self._client_id = _bounded_text(client_id, "client_id", 512)
        self._authorization_endpoint = _bounded_text(
            authorization_endpoint, "authorization_endpoint", 1_024
        )
        if self._authorization_endpoint != GOOGLE_AUTHORIZATION_ENDPOINT:
            raise ValueError("authorization_endpoint must be Google's exact endpoint")
        if isinstance(allowed_redirect_uris, (str, bytes)):
            raise ValueError("allowed_redirect_uris must contain exact callback URIs")
        redirects = list(allowed_redirect_uris)
        if not redirects or len(redirects) > 16:
            raise ValueError("allowed_redirect_uris must contain 1-16 callback URIs")
        validated_redirects: set[str] = set()
        for redirect in redirects:
            redirect = _bounded_text(redirect, "redirect_uri", 1_024)
            parsed = urlsplit(redirect)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
            ):
                raise ValueError("redirect_uri must be an exact HTTPS callback")
            validated_redirects.add(redirect)
        if len(validated_redirects) != len(redirects):
            raise ValueError("allowed_redirect_uris must be unique")
        self._allowed_redirect_uris = frozenset(validated_redirects)
        self._now = now or _utc_now
        self._random_bytes = random_bytes or os.urandom

    def start(self, *, user_id: str, redirect_uri: str) -> AuthorizationRequest:
        user_id = _bounded_text(user_id, "user_id", 128)
        redirect_uri = _bounded_text(redirect_uri, "redirect_uri", 1_024)
        if redirect_uri not in self._allowed_redirect_uris:
            raise ValueError("redirect_uri is not registered for this deployment")
        state = _b64url(self._random_bytes(32))
        verifier = _b64url(self._random_bytes(64))
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        expires_at = self._now().astimezone(timezone.utc) + STATE_TTL
        state_key = hashlib.sha256(state.encode("ascii")).hexdigest()
        self._state_store.put_once(
            state_key,
            {
                "user_id": user_id,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
                "expires_at": expires_at.isoformat(),
            },
            expires_at=int(expires_at.timestamp()),
        )
        params = urlencode(
            {
                "access_type": "offline",
                "client_id": self._client_id,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "include_granted_scopes": "false",
                "prompt": "consent",
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": GMAIL_READONLY_SCOPE,
                "state": state,
            }
        )
        return AuthorizationRequest(
            url=f"{self._authorization_endpoint}?{params}",
            state=state,
            code_challenge=challenge,
        )

    def complete(self, *, user_id: str, state: str, code: str) -> None:
        user_id = _bounded_text(user_id, "user_id", 128)
        state = _bounded_text(state, "state", 512)
        code = _bounded_text(code, "code", 4_096)
        state_key = hashlib.sha256(state.encode("ascii")).hexdigest()
        record = self._state_store.pop_once(state_key)
        if not isinstance(record, Mapping):
            raise OAuthStateError("OAuth state is missing, expired, or already used")
        try:
            expires_at = datetime.fromisoformat(str(record["expires_at"]))
            bound_user = record["user_id"]
            redirect_uri = record["redirect_uri"]
            verifier = record["code_verifier"]
        except (KeyError, TypeError, ValueError) as error:
            raise OAuthStateError("OAuth state record is invalid") from error
        if expires_at.tzinfo is None or self._now().astimezone(timezone.utc) > expires_at.astimezone(timezone.utc):
            raise OAuthStateError("OAuth state expired")
        if not isinstance(bound_user, str) or not hmac.compare_digest(bound_user, user_id):
            raise OAuthStateError("OAuth state belongs to another user")
        token = self._token_client.exchange_code(
            code=code,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            client_id=self._client_id,
        )
        if not isinstance(token, Mapping):
            raise OAuthScopeError("Google returned an invalid token response")
        scopes = set(str(token.get("scope", "")).split())
        if scopes != {GMAIL_READONLY_SCOPE}:
            raise OAuthScopeError("Google token is not limited to Gmail read-only")
        if not isinstance(token.get("access_token"), str) or not token["access_token"]:
            raise OAuthScopeError("Google returned no access token")
        if not isinstance(token.get("refresh_token"), str) or not token["refresh_token"]:
            raise OAuthScopeError("Google returned no initial refresh token")
        if token.get("token_type") not in {None, "Bearer"}:
            raise OAuthScopeError("Google returned an invalid token type")
        expires_in = token.get("expires_in")
        if expires_in is not None and (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or not 1 <= expires_in <= 86_400
        ):
            raise OAuthScopeError("Google returned an invalid token expiry")
        self._token_vault.save(
            user_id=user_id,
            provider="google-gmail-readonly",
            token=dict(token),
        )


class GoogleOAuthTokenClient:
    """One-attempt form client pinned to Google's canonical token endpoint."""

    def __init__(self, *, client_secret: str, urlopen=None) -> None:
        self._client_secret = _bounded_text(client_secret, "client_secret", 4_096)
        self._urlopen = urlopen or _stdlib_urlopen

    @staticmethod
    def _token(payload: object, *, require_refresh: bool) -> dict[str, object]:
        if not isinstance(payload, Mapping):
            raise ValueError("token response is not an object")
        allowed = {
            "access_token",
            "refresh_token",
            "expires_in",
            "scope",
            "token_type",
        }
        if not set(payload).issubset(allowed):
            raise ValueError("token response contains unknown fields")
        access_token = _bounded_text(payload.get("access_token"), "access_token", 16_384)
        scope = _bounded_text(payload.get("scope"), "scope", 4_096)
        if set(scope.split()) != {GMAIL_READONLY_SCOPE}:
            raise ValueError("token response scope is invalid")
        if payload.get("token_type") != "Bearer":
            raise ValueError("token response type is invalid")
        expires_in = payload.get("expires_in")
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or not 1 <= expires_in <= 86_400
        ):
            raise ValueError("token response expiry is invalid")
        token: dict[str, object] = {
            "access_token": access_token,
            "expires_in": expires_in,
            "scope": GMAIL_READONLY_SCOPE,
            "token_type": "Bearer",
        }
        refresh = payload.get("refresh_token")
        if refresh is not None:
            token["refresh_token"] = _bounded_text(
                refresh, "refresh_token", 16_384
            )
        if require_refresh and "refresh_token" not in token:
            raise ValueError("initial token response has no refresh token")
        return token

    def _request(self, fields: Mapping[str, str], *, require_refresh: bool) -> dict[str, object]:
        try:
            encoded = urlencode(dict(fields)).encode("ascii")
            request = Request(
                GOOGLE_TOKEN_ENDPOINT,
                data=encoded,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                method="POST",
            )
            with self._urlopen(request, timeout=10) as response:
                if getattr(response, "status", None) != 200:
                    raise ValueError("token endpoint did not return success")
                body = response.read(MAX_TOKEN_RESPONSE_BYTES + 1)
            if not isinstance(body, bytes) or len(body) > MAX_TOKEN_RESPONSE_BYTES:
                raise ValueError("token response is too large")
            payload = json.loads(body.decode("utf-8", "strict"))
            return self._token(payload, require_refresh=require_refresh)
        except Exception:
            # Never preserve exception context: HTTP/client errors can include
            # form fields containing codes, verifier, client secret, or token.
            raise OAuthTokenProviderError(
                "Google OAuth token request failed"
            ) from None

    def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        client_id: str,
    ) -> dict[str, object]:
        fields = {
            "client_id": _bounded_text(client_id, "client_id", 512),
            "client_secret": self._client_secret,
            "code": _bounded_text(code, "code", 4_096),
            "code_verifier": _bounded_text(
                code_verifier, "code_verifier", 256
            ),
            "grant_type": "authorization_code",
            "redirect_uri": _bounded_text(redirect_uri, "redirect_uri", 1_024),
        }
        return self._request(fields, require_refresh=True)

    def refresh(self, *, refresh_token: str, client_id: str) -> dict[str, object]:
        refresh_token = _bounded_text(
            refresh_token, "refresh_token", 16_384
        )
        token = self._request(
            {
                "client_id": _bounded_text(client_id, "client_id", 512),
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            require_refresh=False,
        )
        token.setdefault("refresh_token", refresh_token)
        return token


def _context(user_id: str, provider: str) -> dict[str, str]:
    return {
        "application": "personal-operator",
        "provider": provider,
        "userId": user_id,
    }


def _aad(context: Mapping[str, str]) -> bytes:
    return json.dumps(context, sort_keys=True, separators=(",", ":")).encode()


def _binding(context: Mapping[str, str]) -> str:
    return hashlib.sha256(_aad(context)).hexdigest()


class KmsEnvelopeTokenVault:
    """Encrypt token JSON with a per-write data key wrapped by a KMS CMK."""

    def __init__(self, *, kms_client, key_id: str, record_store, aead) -> None:
        self._kms = kms_client
        self._key_id = _bounded_text(key_id, "key_id", 2_048)
        self._store = record_store
        self._aead = aead

    def save(self, *, user_id: str, provider: str, token: Mapping[str, object]) -> None:
        context = _context(
            _bounded_text(user_id, "user_id", 128),
            _bounded_text(provider, "provider", 128),
        )
        try:
            plaintext = json.dumps(
                dict(token), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise TokenEnvelopeError("token must be bounded JSON") from error
        if not plaintext or len(plaintext) > 64 * 1024:
            raise TokenEnvelopeError("token payload is empty or too large")
        generated = self._kms.generate_data_key(
            KeyId=self._key_id,
            KeySpec="AES_256",
            EncryptionContext=context,
        )
        data_key = generated.get("Plaintext")
        wrapped_key = generated.get("CiphertextBlob")
        if not isinstance(data_key, bytes) or len(data_key) != 32 or not isinstance(wrapped_key, bytes):
            raise TokenEnvelopeError("KMS returned an invalid data key")
        encrypted = self._aead.encrypt(
            key=data_key,
            plaintext=plaintext,
            associated_data=_aad(context),
        )
        nonce = encrypted.get("nonce") if isinstance(encrypted, Mapping) else None
        ciphertext = encrypted.get("ciphertext") if isinstance(encrypted, Mapping) else None
        if not isinstance(nonce, bytes) or not isinstance(ciphertext, bytes):
            raise TokenEnvelopeError("AEAD returned an invalid envelope")
        record = {
            "format": "personal-operator.oauth-envelope.v1",
            "binding": _binding(context),
            "wrappedKey": _b64url(wrapped_key),
            "nonce": _b64url(nonce),
            "ciphertext": _b64url(ciphertext),
        }
        self._store.put(user_id=user_id, provider=provider, record=record)

    def load(self, *, user_id: str, provider: str) -> dict[str, object] | None:
        context = _context(
            _bounded_text(user_id, "user_id", 128),
            _bounded_text(provider, "provider", 128),
        )
        record = self._store.get(user_id=user_id, provider=provider)
        if record is None:
            return None
        if not isinstance(record, Mapping) or record.get("format") != "personal-operator.oauth-envelope.v1":
            raise TokenEnvelopeError("token envelope has an unknown format")
        if not isinstance(record.get("binding"), str) or not hmac.compare_digest(
            record["binding"], _binding(context)
        ):
            raise TokenEnvelopeError("token envelope belongs to another authority")
        try:
            wrapped_key = _unb64url(record["wrappedKey"])
            nonce = _unb64url(record["nonce"])
            ciphertext = _unb64url(record["ciphertext"])
        except (KeyError, TypeError, ValueError) as error:
            raise TokenEnvelopeError("token envelope encoding is invalid") from error
        decrypted = self._kms.decrypt(
            CiphertextBlob=wrapped_key,
            KeyId=self._key_id,
            EncryptionContext=context,
        )
        data_key = decrypted.get("Plaintext")
        if not isinstance(data_key, bytes) or len(data_key) != 32:
            raise TokenEnvelopeError("KMS returned an invalid plaintext key")
        try:
            plaintext = self._aead.decrypt(
                key=data_key,
                nonce=nonce,
                ciphertext=ciphertext,
                associated_data=_aad(context),
            )
            token = json.loads(plaintext)
        except Exception as error:
            raise TokenEnvelopeError("token envelope authentication failed") from error
        if not isinstance(token, dict):
            raise TokenEnvelopeError("decrypted token is invalid")
        return token


class CryptographyAesGcm:
    """Small adapter; deployment packaging must include `cryptography`."""

    def encrypt(self, *, key: bytes, plaintext: bytes, associated_data: bytes):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as error:  # pragma: no cover - deployment dependency guard
            raise RuntimeError("cryptography is required for token encryption") from error
        nonce = os.urandom(12)
        return {
            "nonce": nonce,
            "ciphertext": AESGCM(key).encrypt(nonce, plaintext, associated_data),
        }

    def decrypt(
        self,
        *,
        key: bytes,
        nonce: bytes,
        ciphertext: bytes,
        associated_data: bytes,
    ) -> bytes:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as error:  # pragma: no cover - deployment dependency guard
            raise RuntimeError("cryptography is required for token encryption") from error
        return AESGCM(key).decrypt(nonce, ciphertext, associated_data)
