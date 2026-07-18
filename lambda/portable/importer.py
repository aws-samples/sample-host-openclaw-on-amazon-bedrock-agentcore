"""Staged import: dry-run plan, exact-hash activation, and safe landing.

Parsing is total and hostile-input aware: content addressing, canonical-JSON
framing, path safety, and category boundaries are enforced before any policy
check runs.  ``build_plan`` is pure and never writes.  ``activate`` recomputes
the complete-bundle hash, requires exact equality to the caller-approved hash,
and lands through a single atomic compare-and-swap of a staged generation.

Landing invariants that can never be relaxed:
  * schedules land ``DISABLED`` with no armed next run,
  * connectors land ``DISCONNECTED`` and never write a connection envelope,
  * receipts land NON-REPLAYABLE so imported history cannot re-dispatch a
    past effect,
  * activation binds to the CALLER's identity, ignoring any embedded owner
    claim (three-user isolation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import io
import json
import zipfile
from typing import Mapping

from .manifest import (
    FORMAT,
    RECORD_CATEGORIES,
    BundleIntegrityError,
    ImportRejected,
    ImportUncertain,
    canonical_json,
    complete_bundle_hash,
    object_sha256,
    safe_path,
    scan_for_secrets,
    user_id as _user_id,
)


MAX_BUNDLE_BYTES = 50 * 1024 * 1024
MAX_ENTRIES = 1_000

# Record states that prove a still-open or uncertain effect.  A portable bundle
# may only carry immutable, terminal, non-replayable history.
_PENDING_STATES = frozenset(
    {
        "APPROVAL_PENDING",
        "PENDING",
        "SENDING",
        "DISPATCHING",
        "IN_FLIGHT",
        "QUEUED",
        "UNCERTAIN",
        "RECONCILING",
    }
)
# Live-authority / secret / tombstone markers that must never be importable,
# regardless of which record category tries to smuggle them.
_ACTIVE_AUTHORITY_MARKERS = frozenset({"CONNECTED", "DISCONNECTING", "CONNECTING"})
_TOMBSTONE_RECORD_TYPES = frozenset(
    {"USER_TOMBSTONE", "CHANNEL_TOMBSTONE", "TOMBSTONE"}
)
_FORBIDDEN_RECORD_TYPES = frozenset(
    {"GMAIL_CONNECTION_FENCE"} | _TOMBSTONE_RECORD_TYPES
)


@dataclass(frozen=True, slots=True)
class ImportPlanV1:
    """Typed DRY-RUN summary of a validated bundle.  No mutation implied."""

    bundle_hash: str
    counts: dict
    landing: dict
    owner_claim: str
    rejections: tuple = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class _ParsedBundle:
    manifest: dict
    bundle_hash: str
    records: dict
    workspace: dict


class PortableImporter:
    def __init__(self, *, staging) -> None:
        if not callable(getattr(staging, "swap", None)) or not callable(
            getattr(staging, "load_generation", None)
        ):
            raise TypeError("staged-generation store is invalid")
        self._staging = staging

    # -- parsing / integrity ------------------------------------------------

    def _parse(self, bundle_bytes: object) -> _ParsedBundle:
        if not isinstance(bundle_bytes, (bytes, bytearray)):
            raise BundleIntegrityError("portable bundle must be bytes")
        if len(bundle_bytes) > MAX_BUNDLE_BYTES:
            raise BundleIntegrityError("portable bundle exceeds its size limit")
        try:
            archive = zipfile.ZipFile(io.BytesIO(bytes(bundle_bytes)))
        except zipfile.BadZipFile as error:
            raise BundleIntegrityError("portable bundle is not a zip") from error
        with archive:
            names = archive.namelist()
            if len(names) > MAX_ENTRIES:
                raise BundleIntegrityError("portable bundle has too many entries")
            if len(names) != len(set(names)):
                raise BundleIntegrityError("portable bundle has duplicate entries")
            if "manifest.json" not in names:
                raise BundleIntegrityError("portable bundle has no manifest")
            payloads = {name: archive.read(name) for name in names}

        manifest_bytes = payloads["manifest.json"]
        try:
            manifest = json.loads(manifest_bytes)
        except (ValueError, TypeError) as error:
            raise BundleIntegrityError("portable manifest is not JSON") from error
        if not isinstance(manifest, Mapping):
            raise BundleIntegrityError("portable manifest is invalid")
        # Reject non-canonical framing: the manifest must be byte-identical to a
        # canonical re-serialization of its parsed content.
        if canonical_json(manifest) != manifest_bytes:
            raise BundleIntegrityError("portable manifest is not canonical")
        if manifest.get("format") != FORMAT:
            raise BundleIntegrityError("portable bundle format tag is invalid")

        owner = manifest.get("userId")
        if not isinstance(owner, str):
            raise BundleIntegrityError("portable manifest owner is invalid")
        landing = manifest.get("landing")
        if not isinstance(landing, Mapping):
            raise BundleIntegrityError("portable manifest landing is invalid")

        objects = manifest.get("objects")
        if not isinstance(objects, list):
            raise BundleIntegrityError("portable manifest objects are invalid")

        declared_paths: set[str] = set()
        records: dict[str, object] = {}
        workspace: dict[str, bytes] = {}
        for entry in objects:
            self._verify_descriptor(entry, payloads, declared_paths)
            path = entry["path"]
            category = entry["category"]
            payload = payloads[path]
            if category in RECORD_CATEGORIES:
                try:
                    records[category] = json.loads(payload)
                except (ValueError, TypeError) as error:
                    raise BundleIntegrityError(
                        "portable record object is not JSON"
                    ) from error
            else:  # workspace
                workspace[path[len("workspace/") :]] = payload

        # Every non-manifest zip entry must be declared (no extra/missing).
        zip_objects = set(payloads) - {"manifest.json"}
        if zip_objects != declared_paths:
            raise BundleIntegrityError(
                "portable bundle entries do not match the manifest"
            )

        return _ParsedBundle(
            manifest=dict(manifest),
            bundle_hash=complete_bundle_hash(manifest),
            records=records,
            workspace=workspace,
        )

    @staticmethod
    def _verify_descriptor(entry, payloads, declared_paths) -> None:
        if not isinstance(entry, Mapping) or set(entry) != {
            "path",
            "category",
            "type",
            "size",
            "sha256",
        }:
            raise BundleIntegrityError("portable object descriptor is invalid")
        path = entry["path"]
        category = entry["category"]
        if not isinstance(path, str):
            raise BundleIntegrityError("portable object path is invalid")
        if path in declared_paths:
            raise BundleIntegrityError("portable bundle declares a duplicate path")
        # Category and prefix must agree, and the path must be traversal-safe.
        if category in RECORD_CATEGORIES:
            if path != f"records/{category}.json":
                raise BundleIntegrityError("portable record path is invalid")
        elif category == "workspace":
            if not path.startswith("workspace/"):
                raise BundleIntegrityError("portable workspace path is invalid")
            try:
                safe_path(path[len("workspace/") :])
            except Exception as error:
                raise BundleIntegrityError(
                    "portable workspace path is unsafe"
                ) from error
        else:
            raise BundleIntegrityError("portable object category is invalid")
        if path not in payloads:
            raise BundleIntegrityError("portable manifest references a missing object")
        payload = payloads[path]
        size = entry["size"]
        if isinstance(size, bool) or not isinstance(size, int) or size != len(payload):
            raise BundleIntegrityError("portable object size does not match")
        sha = entry["sha256"]
        if not isinstance(sha, str) or sha != object_sha256(payload):
            raise BundleIntegrityError("portable object hash does not match")
        declared_paths.add(path)

    # -- policy -------------------------------------------------------------

    @staticmethod
    def _enforce_policy(bundle: _ParsedBundle) -> None:
        landing = bundle.manifest["landing"]
        receipts_landing = landing.get("receipts")
        if (
            not isinstance(receipts_landing, Mapping)
            or receipts_landing.get("replayable") is not False
        ):
            raise ImportRejected("imported receipts must land non-replayable")
        if landing.get("schedules") != "DISABLED":
            raise ImportRejected("imported schedules must land disabled")
        if landing.get("connectors") != "DISCONNECTED":
            raise ImportRejected("imported connectors must land disconnected")

        for category, payload in bundle.records.items():
            rows = payload if isinstance(payload, list) else [payload]
            for row in rows:
                scan_for_secrets(row)
                if not isinstance(row, Mapping):
                    continue
                record_type = row.get("recordType")
                if isinstance(record_type, str) and (
                    record_type in _FORBIDDEN_RECORD_TYPES
                    or record_type.upper() in _TOMBSTONE_RECORD_TYPES
                ):
                    raise ImportRejected("portable bundle carries a forbidden record")
                if "deletionStatus" in row:
                    raise ImportRejected("portable bundle carries a deletion tombstone")
                status = row.get("status")
                if isinstance(status, str) and status in _ACTIVE_AUTHORITY_MARKERS:
                    raise ImportRejected("portable bundle carries live authority")
                if "connectionEnvelope" in row or "refreshToken" in row:
                    raise ImportRejected("portable bundle carries live authority")
                state = row.get("state")
                if isinstance(state, str) and state in _PENDING_STATES:
                    raise ImportRejected("portable bundle carries a pending effect")

    # -- public API ---------------------------------------------------------

    def build_plan(self, bundle_bytes: object) -> ImportPlanV1:
        bundle = self._parse(bundle_bytes)
        self._enforce_policy(bundle)
        counts = {
            category: (
                len(payload) if isinstance(payload, list) else 1
            )
            for category, payload in bundle.records.items()
        }
        for category in RECORD_CATEGORIES:
            counts.setdefault(category, 0)
        counts["workspace"] = len(bundle.workspace)
        return ImportPlanV1(
            bundle_hash=bundle.bundle_hash,
            counts=counts,
            landing=dict(bundle.manifest["landing"]),
            owner_claim=bundle.manifest["userId"],
            rejections=(),
        )

    def _staged_payload(self, bundle: _ParsedBundle) -> dict:
        schedules = []
        for row in bundle.records.get("schedules", []) or []:
            if isinstance(row, Mapping):
                landed = {
                    key: value
                    for key, value in row.items()
                    if key not in {"nextRunAt", "nextRun"}
                }
                landed["state"] = "DISABLED"
                schedules.append(landed)
            else:
                schedules.append(row)
        records = {
            category: payload
            for category, payload in bundle.records.items()
            if category != "schedules"
        }
        records["schedules"] = schedules
        # Connectors are never materialized as an envelope.
        records.pop("connections", None)
        return {
            "format": FORMAT,
            "records": records,
            "workspace": dict(bundle.workspace),
            "landing": dict(bundle.manifest["landing"]),
        }

    def activate(
        self,
        bundle_bytes: object,
        *,
        approved_bundle_hash: object,
        target_user_id: object,
        expected_generation: object = None,
    ) -> dict:
        caller = _user_id(target_user_id)
        bundle = self._parse(bundle_bytes)
        self._enforce_policy(bundle)
        # Bind activation to the EXACT complete-bundle hash the caller approved.
        if (
            not isinstance(approved_bundle_hash, str)
            or approved_bundle_hash != bundle.bundle_hash
        ):
            raise ImportRejected("approved bundle hash does not match the bundle")

        if expected_generation is None:
            expected_generation = self._staging.load_generation(caller)
        if isinstance(expected_generation, bool) or not isinstance(
            expected_generation, int
        ):
            raise ImportRejected("staged generation is invalid")

        staged = self._staged_payload(bundle)
        try:
            generation = self._staging.swap(
                caller,
                expected_generation=expected_generation,
                staged=staged,
            )
        except ImportRejected:
            raise
        except Exception as error:
            # Any non-CAS failure is uncertain; report it without claiming a
            # partial activation happened.  The single-CAS swap guarantees no
            # partial state exists regardless of this outcome.
            raise ImportUncertain("staged activation is uncertain") from error
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise ImportRejected("staged activation returned an invalid generation")
        return {
            "status": "activated",
            "userId": caller,
            "generation": generation,
            "bundleHash": bundle.bundle_hash,
        }
