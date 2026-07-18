import base64
import json

import pytest

from workspace_capability import (
    WorkspaceCapabilityError,
    WorkspaceCapabilitySigner,
    verify_workspace_capability,
)


SECRET = b"s" * 64
USER = "user_A"
SESSION = "ses_123456789012345678901234567890"
AUDIENCE = "personal-operator-workspace-credential-broker"


def _decode(segment: str) -> dict:
    padding = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + padding))


def _encode(value: dict) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_mints_and_verifies_exact_user_session_namespace_capability():
    signer = WorkspaceCapabilitySigner(
        key_provider=lambda: SECRET,
        audience=AUDIENCE,
        clock=lambda: 1_000,
        ttl_seconds=900,
    )

    token = signer.mint(user_id=USER, session_id=SESSION)
    claims = verify_workspace_capability(
        token,
        key=SECRET,
        audience=AUDIENCE,
        now=1_001,
    )

    assert claims == {
        "aud": AUDIENCE,
        "exp": 1_900,
        "iat": 1_000,
        "namespace": USER,
        "sessionId": SESSION,
        "sub": USER,
        "v": 1,
    }


def test_rejects_cross_user_tampering_without_accepting_the_original_signature():
    signer = WorkspaceCapabilitySigner(
        key_provider=lambda: SECRET,
        audience=AUDIENCE,
        clock=lambda: 1_000,
    )
    token = signer.mint(user_id=USER, session_id=SESSION)
    payload, signature = token.split(".")
    claims = _decode(payload)
    claims["sub"] = "user_B"
    claims["namespace"] = "user_B"
    tampered = f"{_encode(claims)}.{signature}"

    with pytest.raises(WorkspaceCapabilityError, match="signature"):
        verify_workspace_capability(
            tampered,
            key=SECRET,
            audience=AUDIENCE,
            now=1_001,
        )


@pytest.mark.parametrize(
    ("now", "audience", "message"),
    [
        (37_001, AUDIENCE, "expired"),
        (1_001, "another-broker", "audience"),
    ],
)
def test_rejects_expired_or_wrong_audience_capabilities(now, audience, message):
    signer = WorkspaceCapabilitySigner(
        key_provider=lambda: SECRET,
        audience=AUDIENCE,
        clock=lambda: 1_000,
    )
    token = signer.mint(user_id=USER, session_id=SESSION)

    with pytest.raises(WorkspaceCapabilityError, match=message):
        verify_workspace_capability(
            token,
            key=SECRET,
            audience=audience,
            now=now,
        )
