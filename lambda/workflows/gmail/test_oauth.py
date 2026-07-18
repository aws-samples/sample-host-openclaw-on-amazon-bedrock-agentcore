import base64
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs

import pytest


GMAIL_DIR = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("gmail_oauth", GMAIL_DIR / "oauth.py")
oauth = importlib.util.module_from_spec(spec)
sys.modules["gmail_oauth"] = oauth
assert spec.loader is not None
spec.loader.exec_module(oauth)

NOW = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)


class StateStore:
    def __init__(self):
        self.items = {}

    def put_once(self, key, value, *, expires_at):
        assert key not in self.items
        self.items[key] = (value, expires_at)

    def pop_once(self, key):
        item = self.items.pop(key, None)
        return None if item is None else item[0]


class TokenClient:
    def __init__(self, token):
        self.token = token
        self.calls = []

    def exchange_code(self, **kwargs):
        self.calls.append(kwargs)
        return self.token


class Vault:
    def __init__(self):
        self.saved = []

    def save(self, *, user_id, provider, token):
        self.saved.append((user_id, provider, token))


def test_oauth_start_uses_pkce_and_callback_is_one_time_user_bound():
    state_store = StateStore()
    token_client = TokenClient(
        {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "scope": "https://www.googleapis.com/auth/gmail.readonly",
            "expires_in": 3600,
        }
    )
    vault = Vault()
    flow = oauth.GoogleReadonlyOAuthFlow(
        state_store=state_store,
        token_client=token_client,
        token_vault=vault,
        client_id="client-id",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        allowed_redirect_uris={"https://app.example/oauth/google/callback"},
        now=lambda: NOW,
        random_bytes=lambda count: bytes(range(count)),
    )

    request = flow.start(
        user_id="user-1",
        redirect_uri="https://app.example/oauth/google/callback",
    )

    assert request.state
    assert request.code_challenge_method == "S256"
    assert "gmail.readonly" in request.url
    assert "gmail.send" not in request.url
    state_key = hashlib.sha256(request.state.encode()).hexdigest()
    record = state_store.items[state_key][0]
    assert "code_verifier" in record
    assert "state" not in record

    flow.complete(
        user_id="user-1",
        state=request.state,
        code="one-time-code",
    )
    assert vault.saved == [
        (
            "user-1",
            "google-gmail-readonly",
            token_client.token,
        )
    ]
    call = token_client.calls[0]
    verifier = call["code_verifier"]
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert request.code_challenge == expected
    assert call["redirect_uri"] == "https://app.example/oauth/google/callback"

    with pytest.raises(oauth.OAuthStateError):
        flow.complete(user_id="user-1", state=request.state, code="replay")


def test_oauth_rejects_wrong_user_expired_state_and_scope_escalation():
    def make_flow(token, now=lambda: NOW):
        store = StateStore()
        flow = oauth.GoogleReadonlyOAuthFlow(
            state_store=store,
            token_client=TokenClient(token),
            token_vault=Vault(),
            client_id="client-id",
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            allowed_redirect_uris={"https://app.example/cb"},
            now=now,
        )
        return flow

    base_token = {
        "access_token": "a",
        "refresh_token": "r",
        "scope": "https://www.googleapis.com/auth/gmail.readonly",
    }
    flow = make_flow(base_token)
    request = flow.start(user_id="user-a", redirect_uri="https://app.example/cb")
    with pytest.raises(oauth.OAuthStateError):
        flow.complete(user_id="user-b", state=request.state, code="code")

    current = [NOW]
    flow = make_flow(base_token, now=lambda: current[0])
    request = flow.start(user_id="user-a", redirect_uri="https://app.example/cb")
    current[0] = NOW + timedelta(minutes=11)
    with pytest.raises(oauth.OAuthStateError):
        flow.complete(user_id="user-a", state=request.state, code="code")

    flow = make_flow(
        {
            **base_token,
            "scope": "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send",
        }
    )
    request = flow.start(user_id="user-a", redirect_uri="https://app.example/cb")
    with pytest.raises(oauth.OAuthScopeError):
        flow.complete(user_id="user-a", state=request.state, code="code")


def test_oauth_rejects_unregistered_redirect_and_missing_initial_refresh_token():
    vault = Vault()
    flow = oauth.GoogleReadonlyOAuthFlow(
        state_store=StateStore(),
        token_client=TokenClient(
            {
                "access_token": "access",
                "scope": oauth.GMAIL_READONLY_SCOPE,
            }
        ),
        token_vault=vault,
        client_id="client-id",
        authorization_endpoint=oauth.GOOGLE_AUTHORIZATION_ENDPOINT,
        allowed_redirect_uris={"https://app.example/oauth/google/callback"},
        now=lambda: NOW,
    )

    with pytest.raises(ValueError):
        flow.start(user_id="user-1", redirect_uri="https://evil.example/callback")

    request = flow.start(
        user_id="user-1",
        redirect_uri="https://app.example/oauth/google/callback",
    )
    with pytest.raises(oauth.OAuthScopeError):
        flow.complete(user_id="user-1", state=request.state, code="code")
    assert vault.saved == []


class FakeHttpResponse:
    def __init__(self, payload, *, status=200):
        self.status = status
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self._body[:size]


def test_google_token_client_uses_exact_endpoint_and_form_for_exchange_and_refresh():
    calls = []
    responses = iter(
        [
            FakeHttpResponse(
                {
                    "access_token": "access-one",
                    "refresh_token": "refresh-one",
                    "expires_in": 3600,
                    "scope": oauth.GMAIL_READONLY_SCOPE,
                    "token_type": "Bearer",
                }
            ),
            FakeHttpResponse(
                {
                    "access_token": "access-two",
                    "expires_in": 3600,
                    "scope": oauth.GMAIL_READONLY_SCOPE,
                    "token_type": "Bearer",
                }
            ),
        ]
    )

    def urlopen(request, *, timeout):
        calls.append((request, timeout))
        return next(responses)

    client = oauth.GoogleOAuthTokenClient(
        client_secret="client-secret",
        urlopen=urlopen,
    )
    exchanged = client.exchange_code(
        code="one-time-code",
        code_verifier="pkce-verifier",
        redirect_uri="https://app.example/oauth/google/callback",
        client_id="client-id",
    )
    refreshed = client.refresh(
        refresh_token="refresh-one",
        client_id="client-id",
    )

    assert exchanged["refresh_token"] == "refresh-one"
    assert refreshed["access_token"] == "access-two"
    assert refreshed["refresh_token"] == "refresh-one"
    assert len(calls) == 2
    exchange_request, timeout = calls[0]
    assert exchange_request.full_url == oauth.GOOGLE_TOKEN_ENDPOINT
    assert exchange_request.get_method() == "POST"
    assert timeout == 10
    assert "client-secret" not in exchange_request.full_url
    assert parse_qs(exchange_request.data.decode()) == {
        "client_id": ["client-id"],
        "client_secret": ["client-secret"],
        "code": ["one-time-code"],
        "code_verifier": ["pkce-verifier"],
        "grant_type": ["authorization_code"],
        "redirect_uri": ["https://app.example/oauth/google/callback"],
    }
    refresh_request, _ = calls[1]
    assert refresh_request.full_url == oauth.GOOGLE_TOKEN_ENDPOINT
    assert parse_qs(refresh_request.data.decode()) == {
        "client_id": ["client-id"],
        "client_secret": ["client-secret"],
        "grant_type": ["refresh_token"],
        "refresh_token": ["refresh-one"],
    }


def test_google_token_client_redacts_network_and_provider_failures():
    def failing_urlopen(request, *, timeout):
        raise RuntimeError("one-time-code client-secret refresh-secret")

    client = oauth.GoogleOAuthTokenClient(
        client_secret="client-secret",
        urlopen=failing_urlopen,
    )
    with pytest.raises(oauth.OAuthTokenProviderError) as failed:
        client.exchange_code(
            code="one-time-code",
            code_verifier="pkce-verifier",
            redirect_uri="https://app.example/oauth/google/callback",
            client_id="client-id",
        )
    assert str(failed.value) == "Google OAuth token request failed"
    assert "one-time-code" not in repr(failed.value)


class FakeKms:
    def __init__(self):
        self.calls = []

    def generate_data_key(self, **kwargs):
        self.calls.append(("generate", kwargs))
        return {"Plaintext": b"k" * 32, "CiphertextBlob": b"wrapped-key"}

    def decrypt(self, **kwargs):
        self.calls.append(("decrypt", kwargs))
        return {"Plaintext": b"k" * 32}


class FakeAead:
    def encrypt(self, *, key, plaintext, associated_data):
        assert key == b"k" * 32
        return {"nonce": b"n" * 12, "ciphertext": plaintext[::-1] + associated_data[:1]}

    def decrypt(self, *, key, nonce, ciphertext, associated_data):
        assert key == b"k" * 32 and nonce == b"n" * 12
        return ciphertext[:-1][::-1]


class CipherStore:
    def __init__(self):
        self.records = {}

    def put(self, *, user_id, provider, record):
        self.records[(user_id, provider)] = record

    def get(self, *, user_id, provider):
        return self.records.get((user_id, provider))


def test_token_vault_uses_kms_envelope_context_and_never_stores_plaintext():
    kms = FakeKms()
    store = CipherStore()
    vault = oauth.KmsEnvelopeTokenVault(
        kms_client=kms,
        key_id="arn:aws:kms:eu-west-1:123:key/test",
        record_store=store,
        aead=FakeAead(),
    )
    token = {"access_token": "access-secret", "refresh_token": "refresh-secret"}

    vault.save(user_id="user-1", provider="google-gmail-readonly", token=token)

    serialized = json.dumps(
        store.records[("user-1", "google-gmail-readonly")],
        default=lambda value: value.hex(),
    )
    assert "access-secret" not in serialized
    assert "refresh-secret" not in serialized
    generation = kms.calls[0][1]
    assert generation["KeySpec"] == "AES_256"
    assert generation["EncryptionContext"] == {
        "application": "personal-operator",
        "provider": "google-gmail-readonly",
        "userId": "user-1",
    }
    assert vault.load(user_id="user-1", provider="google-gmail-readonly") == token
    assert kms.calls[-1][1]["EncryptionContext"] == generation["EncryptionContext"]


def test_token_vault_rejects_cross_user_ciphertext_swap():
    kms = FakeKms()
    store = CipherStore()
    vault = oauth.KmsEnvelopeTokenVault(
        kms_client=kms,
        key_id="key",
        record_store=store,
        aead=FakeAead(),
    )
    vault.save(
        user_id="user-a",
        provider="google-gmail-readonly",
        token={"access_token": "a"},
    )
    store.records[("user-b", "google-gmail-readonly")] = store.records[
        ("user-a", "google-gmail-readonly")
    ]

    with pytest.raises(oauth.TokenEnvelopeError):
        vault.load(user_id="user-b", provider="google-gmail-readonly")
