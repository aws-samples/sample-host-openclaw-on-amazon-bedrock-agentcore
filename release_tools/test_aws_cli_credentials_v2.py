"""Credential-free tests for the direct bootstrap credential value."""

from __future__ import annotations

from pathlib import Path

import pytest

from release_tools.aws_cli_credentials_v2 import (
    AWS_BOOTSTRAP_PROFILE,
    AwsBootstrapCredentialError,
    FrozenAwsBootstrapCredentialsV1,
    freeze_bootstrap_credentials,
)


def test_freeze_accepts_only_exact_static_bootstrap_material() -> None:
    frozen = freeze_bootstrap_credentials(
        profile=AWS_BOOTSTRAP_PROFILE,
        access_key="AKIA" + "A" * 16,
        secret_key="s" * 40,
    )

    assert type(frozen) is FrozenAwsBootstrapCredentialsV1
    assert frozen.access_key == "AKIA" + "A" * 16
    assert frozen.secret_key == "s" * 40
    assert frozen.token is None
    assert "AKIA" not in repr(frozen)
    assert "s" * 40 not in repr(frozen)


@pytest.mark.parametrize(
    ("profile", "access_key", "secret_key"),
    [
        ("default", "AKIA" + "A" * 16, "s" * 40),
        (AWS_BOOTSTRAP_PROFILE, "ASIA" + "A" * 16, "s" * 40),
        (AWS_BOOTSTRAP_PROFILE, "AKIA" + "a" * 16, "s" * 40),
        (AWS_BOOTSTRAP_PROFILE, "AKIA" + "A" * 15, "s" * 40),
        (AWS_BOOTSTRAP_PROFILE, "AKIA" + "A" * 16, "short"),
        (AWS_BOOTSTRAP_PROFILE, "AKIA" + "A" * 16, "s" * 39 + " "),
    ],
)
def test_freeze_rejects_crossed_profile_or_nonexact_material(
    profile: str,
    access_key: str,
    secret_key: str,
) -> None:
    with pytest.raises(AwsBootstrapCredentialError):
        freeze_bootstrap_credentials(
            profile=profile,
            access_key=access_key,
            secret_key=secret_key,
        )


def test_frozen_bootstrap_value_is_exact_type_and_immutable() -> None:
    frozen = freeze_bootstrap_credentials(
        profile=AWS_BOOTSTRAP_PROFILE,
        access_key="AKIA" + "A" * 16,
        secret_key="s" * 40,
    )

    with pytest.raises((AttributeError, TypeError)):
        frozen.access_key = "AKIA" + "B" * 16  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        frozen.secret_key = "t" * 40  # type: ignore[misc]


def test_bridge_contains_no_process_or_executable_authority() -> None:
    source = __import__(
        "release_tools.aws_cli_credentials_v2",
        fromlist=["placeholder"],
    ).__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8").casefold()
    assert "subprocess" not in text
    assert "executable" not in text
    assert "aws_binary" not in text
    assert "export-credentials" not in text
