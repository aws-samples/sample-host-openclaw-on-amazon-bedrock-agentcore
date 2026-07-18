"""Content-addressed portable-state v2 schema, hashing, and path validation.

This module is the single canonical home for the workspace path validator so
both the v1 exporter (``web.retention``) and the v2 portable transfer share one
implementation.  The complete-bundle hash is computed over the canonical JSON
manifest, decoupling activation binding from ZIP container framing while still
transitively covering every object (each descriptor carries the object's
SHA-256).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Mapping


FORMAT = "personal-operator.portable.v2"

# Categories that may appear in a portable bundle.
INCLUDE_CATEGORIES = frozenset({"memory", "schedules", "receipts", "workspace"})
# Categories that are structurally documented but MUST NEVER be emitted or
# accepted.  They carry live authority, secrets, or uncertain effects.
EXCLUDE_CATEGORIES = frozenset(
    {
        "credentials",
        "sessions",
        "grants",
        "approvals",
        "runtime_internals",
        "pending_effects",
        "tombstones",
    }
)

# Record categories that are serialized from ``records_for_user``.  ``workspace``
# is materialized from authored files rather than a record category.
RECORD_CATEGORIES = frozenset({"memory", "schedules", "receipts"})

_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")

# Secret-shaped keys that must never cross the portability boundary.  Matching
# is case-insensitive and substring-based so ``refreshToken``/``refresh_token``
# and nested variants are all caught.
SECRET_KEY_MARKERS = (
    "refresh_token",
    "refreshtoken",
    "access_token",
    "accesstoken",
    "client_secret",
    "clientsecret",
    "cookie",
    "csrf",
    "sessionid",
    "session_id",
    "grant",
    "approvaltoken",
    "approval_token",
    "authorization",
    "bearer",
    "privatekey",
    "private_key",
    "password",
    "secret",
)


class PortableError(Exception):
    """Base error for the portable-state boundary."""


class BundleIntegrityError(PortableError):
    """A bundle is malformed, non-canonical, or fails content addressing."""


class ImportRejected(PortableError):
    """A structurally valid bundle violates an import safety policy."""


class ImportUncertain(PortableError):
    """Activation could not be confirmed; no partial state may be assumed."""


def user_id(value: object) -> str:
    if not isinstance(value, str) or _USER_ID.fullmatch(value) is None:
        raise PortableError("user identity is invalid")
    return value


def canonical_json(value: object) -> bytes:
    """Deterministic UTF-8 JSON with sorted keys and no incidental whitespace."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def object_sha256(payload: bytes) -> str:
    if not isinstance(payload, (bytes, bytearray)):
        raise PortableError("portable object payload must be bytes")
    return hashlib.sha256(bytes(payload)).hexdigest()


def safe_path(value: object) -> str:
    """Reject absolute, traversal, hidden, empty, and overlong workspace paths."""

    if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value:
        raise PortableError("workspace path is invalid")
    path = PurePosixPath(value)
    parts = path.parts
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
        or not parts
    ):
        raise PortableError("workspace path is invalid")
    return path.as_posix()


def descriptor(path: str, category: str, type_: str, payload: bytes) -> dict:
    """Build the canonical per-object descriptor for ``path``."""

    if category not in INCLUDE_CATEGORIES:
        raise PortableError("portable object category is not exportable")
    return {
        "path": path,
        "category": category,
        "type": type_,
        "size": len(payload),
        "sha256": object_sha256(payload),
    }


def default_landing() -> dict:
    """Non-negotiable landing state stamped into every portable bundle."""

    return {
        "schedules": "DISABLED",
        "connectors": "DISCONNECTED",
        "receipts": {"replayable": False},
    }


def build_manifest(*, owner_id: str, objects: list, landing: dict) -> dict:
    return {
        "format": FORMAT,
        "userId": owner_id,
        "landing": landing,
        "objects": sorted(objects, key=lambda entry: entry["path"]),
    }


def complete_bundle_hash(manifest: Mapping) -> str:
    """Content address of the whole bundle: SHA-256 over the canonical manifest.

    The manifest embeds every object's SHA-256, so this hash transitively
    binds all content plus the format tag and exporting userId, without
    depending on ZIP framing or compression.
    """

    if not isinstance(manifest, Mapping):
        raise BundleIntegrityError("portable manifest is invalid")
    return object_sha256(canonical_json(manifest))


def scan_for_secrets(value: object, *, _depth: int = 0) -> None:
    """Raise ``ImportRejected`` if any secret-shaped key is present."""

    if _depth > 64:
        raise ImportRejected("portable object nesting is too deep")
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str):
                folded = key.casefold()
                if any(marker in folded for marker in SECRET_KEY_MARKERS):
                    raise ImportRejected("portable bundle embeds a secret-shaped key")
            scan_for_secrets(nested, _depth=_depth + 1)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            scan_for_secrets(nested, _depth=_depth + 1)
