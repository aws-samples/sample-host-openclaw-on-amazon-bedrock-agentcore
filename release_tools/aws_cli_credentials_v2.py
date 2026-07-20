"""Frozen in-memory credentials for the exact direct bootstrap profile.

The owner-only shared credentials file remains the sole durable copy.  This
module validates the one fixed static profile and exposes only a redacted,
immutable value for explicit retained-SDK session construction.
"""

from __future__ import annotations

from dataclasses import dataclass


AWS_BOOTSTRAP_PROFILE = "personal-operator-bootstrap"


class AwsBootstrapCredentialError(RuntimeError):
    """The exact direct bootstrap credential value is unavailable."""


@dataclass(frozen=True, slots=True, repr=False)
class FrozenAwsBootstrapCredentialsV1:
    """Static bootstrap material retained only for one authority lifetime."""

    access_key: str
    secret_key: str
    token: None = None

    def __repr__(self) -> str:
        return "<frozen direct AWS bootstrap credentials>"


def freeze_bootstrap_credentials(
    *,
    profile: str,
    access_key: object,
    secret_key: object,
) -> FrozenAwsBootstrapCredentialsV1:
    """Validate and freeze the exact long-term bootstrap credential shape."""

    if profile != AWS_BOOTSTRAP_PROFILE:
        raise AwsBootstrapCredentialError("AWS bootstrap profile is not exact")
    if (
        not isinstance(access_key, str)
        or len(access_key) != 20
        or not access_key.startswith("AKIA")
        or not access_key.isascii()
        or not access_key.isalnum()
        or not access_key.isupper()
        or not isinstance(secret_key, str)
        or len(secret_key) != 40
        or not secret_key.isascii()
        or any(
            character.isspace() or ord(character) < 0x21
            for character in secret_key
        )
    ):
        raise AwsBootstrapCredentialError(
            "AWS bootstrap credentials are invalid"
        )
    return FrozenAwsBootstrapCredentialsV1(
        access_key=access_key,
        secret_key=secret_key,
    )


__all__ = [
    "AWS_BOOTSTRAP_PROFILE",
    "AwsBootstrapCredentialError",
    "FrozenAwsBootstrapCredentialsV1",
    "freeze_bootstrap_credentials",
]
