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

from capabilities.contracts import (
    ContractValidationError,
    PortableStateManifestV2,
)

FORMAT = PortableStateManifestV2.SCHEMA

# Categories that may appear in a portable bundle.
INCLUDE_CATEGORIES = frozenset(
    {
        "memory",
        "schedules",
        "installed_packs",
        "connectors",
        "compute_receipts",
        "receipts",
        "workspace",
    }
)
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
RECORD_CATEGORIES = frozenset(INCLUDE_CATEGORIES - {"workspace"})
CATEGORY_TYPES = {
    "memory": "MEMORY",
    "schedules": "SCHEDULE",
    "installed_packs": "INSTALLATION",
    "connectors": "CONNECTOR",
    "compute_receipts": "COMPUTE_RECEIPT",
    "receipts": "EFFECT_RECEIPT",
    "workspace": "FILE",
}
TYPE_CATEGORIES = {value: key for key, value in CATEGORY_TYPES.items()}
EXCLUDED_CLASSES = (
    "APPROVALS",
    "CREDENTIALS",
    "GRANTS",
    "PENDING_EFFECTS",
    "RUNTIME_INTERNALS",
    "SESSIONS",
    "TOMBSTONES",
)

_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")

# Secret-shaped keys that must never cross the portability boundary.  Matching
# is case-insensitive and substring-based so ``refreshToken``/``refresh_token``
# and nested variants are all caught.
SECRET_KEY_MARKERS = (
    "refreshtoken",
    "accesstoken",
    "idtoken",
    "sessiontoken",
    "authtoken",
    "oauthtoken",
    "securitytoken",
    "apikey",
    "clientsecret",
    "cookie",
    "csrf",
    "sessionid",
    "grant",
    "approvaltoken",
    "authorization",
    "bearer",
    "privatekey",
    "password",
    "secret",
    "credential",
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
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def strict_json_loads(value: object) -> object:
    """Parse interoperable JSON, rejecting constants and duplicate keys."""

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, nested in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = nested
        return result

    return json.loads(
        value,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


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
        or path.as_posix() != value
    ):
        raise PortableError("workspace path is invalid")
    return path.as_posix()


def descriptor(path: str, type_: str, payload: bytes) -> dict:
    """Build the canonical per-object descriptor for ``path``."""

    if type_ not in TYPE_CATEGORIES:
        raise PortableError("portable object type is not exportable")
    return {
        "path": path,
        "type": type_,
        "size": len(payload),
        "sha256": object_sha256(payload),
    }


def default_landing() -> dict:
    """Non-negotiable landing state stamped into every portable bundle."""

    return {
        "schedules": "DISABLED",
        "installedPacks": "PAUSED",
        "connectors": "DISCONNECTED",
        "computeReceipts": {"replayable": False},
        "receipts": {"replayable": False},
    }


def build_manifest(*, objects: list) -> dict:
    """Build the frozen v2 manifest with a deterministic content generation.

    ``bundleHash`` hashes every canonical manifest field except itself.  The
    generation and createdAt values are deterministic state-format metadata so
    the same source snapshot always emits identical bytes.
    """

    ordered = sorted(objects, key=lambda entry: entry["path"])
    generation_root = object_sha256(canonical_json(ordered))
    manifest = {
        "schema": FORMAT,
        "generation": f"generation_{generation_root[:32]}",
        "bundleHash": "0" * 64,
        "objects": ordered,
        "excludedClasses": list(EXCLUDED_CLASSES),
        "createdAt": 0,
    }
    manifest["bundleHash"] = complete_bundle_hash(manifest)
    try:
        return PortableStateManifestV2.from_mapping(manifest).to_mapping()
    except ContractValidationError as error:
        raise PortableError("portable manifest is invalid") from error


def complete_bundle_hash(manifest: Mapping) -> str:
    """Content address of the whole bundle: SHA-256 over the canonical manifest.

    The manifest embeds every object's SHA-256, so this hash transitively
    binds all content plus the frozen schema/generation/exclusion metadata,
    without depending on ZIP framing or compression. The embedded self-hash
    field is the sole field omitted from its own preimage.
    """

    if not isinstance(manifest, Mapping):
        raise BundleIntegrityError("portable manifest is invalid")
    preimage = {
        key: value for key, value in manifest.items() if key != "bundleHash"
    }
    return object_sha256(canonical_json(preimage))


def scan_for_secrets(value: object, *, _depth: int = 0) -> None:
    """Raise ``ImportRejected`` if any secret-shaped key is present."""

    if _depth > 64:
        raise ImportRejected("portable object nesting is too deep")
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str):
                # Normalize separators and casing so camelCase, snake_case,
                # kebab-case, and header-style spellings share one fail-closed
                # credential/session corpus.
                folded = re.sub(r"[^a-z0-9]", "", key.casefold())
                if any(marker in folded for marker in SECRET_KEY_MARKERS):
                    raise ImportRejected("portable bundle embeds a secret-shaped key")
            scan_for_secrets(nested, _depth=_depth + 1)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            scan_for_secrets(nested, _depth=_depth + 1)
