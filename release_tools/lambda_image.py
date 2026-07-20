"""Strict local validation for the immutable Lambda Python builder image."""

from __future__ import annotations

from pathlib import Path
import platform
import re
import sys


_REQUIRED_OS_RELEASE = {"ID": "amzn", "VERSION_ID": "2023"}
_TOKEN = re.compile(r"[A-Za-z0-9._-]{1,64}")
_MAX_OS_RELEASE_BYTES = 16_384


class LambdaImageValidationError(RuntimeError):
    """The local immutable builder image differs from the release boundary."""


def _release_token(raw: str) -> str:
    value = raw.strip()
    if _TOKEN.fullmatch(value) is not None:
        return value
    if (
        len(value) >= 2
        and value.startswith('"')
        and value.endswith('"')
        and _TOKEN.fullmatch(value[1:-1]) is not None
    ):
        return value[1:-1]
    raise LambdaImageValidationError("Lambda builder OS metadata is malformed")


def validate_amazon_linux_2023(os_release: str) -> None:
    """Accept only exact, uniquely bound Amazon Linux 2023 identity fields."""

    if (
        not isinstance(os_release, str)
        or not os_release
        or "\x00" in os_release
        or len(os_release.encode("utf-8")) > _MAX_OS_RELEASE_BYTES
    ):
        raise LambdaImageValidationError("Lambda builder OS metadata is invalid")
    observed: dict[str, str] = {}
    for raw_line in os_release.splitlines():
        key, separator, raw_value = raw_line.partition("=")
        if not separator or key not in _REQUIRED_OS_RELEASE:
            continue
        if key in observed:
            raise LambdaImageValidationError(
                "Lambda builder OS metadata is ambiguous"
            )
        observed[key] = _release_token(raw_value)
    if observed != _REQUIRED_OS_RELEASE:
        raise LambdaImageValidationError("Lambda builder OS identity differs")


def validate_lambda_builder_environment() -> None:
    """Validate the exact in-container Python, architecture, and OS identity."""

    if sys.version_info[:2] != (3, 13):
        raise LambdaImageValidationError("Lambda builder Python version differs")
    if platform.machine() not in {"aarch64", "arm64"}:
        raise LambdaImageValidationError("Lambda builder architecture differs")
    try:
        os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise LambdaImageValidationError(
            "Lambda builder OS metadata is unavailable"
        ) from error
    validate_amazon_linux_2023(os_release)


__all__ = [
    "LambdaImageValidationError",
    "validate_amazon_linux_2023",
    "validate_lambda_builder_environment",
]
