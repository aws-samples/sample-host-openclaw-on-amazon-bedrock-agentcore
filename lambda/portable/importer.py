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

import base64
from dataclasses import dataclass
import hashlib
import io
import re
import time
import zipfile
from typing import Mapping

from capabilities.contracts import (
    ContractValidationError,
    ImportPlanV1,
    ImportReceiptV1,
    PortableStateManifestV2,
)

from .manifest import (
    EXCLUDED_CLASSES,
    FORMAT,
    RECORD_CATEGORIES,
    TYPE_CATEGORIES,
    BundleIntegrityError,
    ImportRejected,
    ImportUncertain,
    canonical_json,
    complete_bundle_hash,
    default_landing,
    object_sha256,
    safe_path,
    scan_for_secrets,
    strict_json_loads,
    user_id as _user_id,
)
from .records import retarget_records, validate_bundle_records


MAX_BUNDLE_BYTES = 50 * 1024 * 1024
MAX_ENTRIES = 1_000
# The compressed bundle is bounded above, but a small bundle can inflate to
# gigabytes. Bound each entry and the cumulative decompressed size using the
# declared ZipInfo.file_size BEFORE reading any entry, matching the exporter's
# per-entry (5 MiB) and total (50 MiB) limits, so a decompression bomb is
# rejected without ever materializing its payload in memory.
MAX_ENTRY_UNCOMPRESSED_BYTES = 5 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 50 * 1024 * 1024

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
_BASE_GENERATION = re.compile(r"generation_([0-9]{20})")


def _generation_id(value: object) -> str:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 9_007_199_254_740_991
    ):
        raise ImportRejected("portable base generation is invalid")
    return f"generation_{value:020d}"


def _generation_number(value: object) -> int:
    if not isinstance(value, str):
        raise ImportRejected("portable base generation is invalid")
    matched = _BASE_GENERATION.fullmatch(value)
    if matched is None:
        raise ImportRejected("portable base generation is invalid")
    number = int(matched.group(1))
    if _generation_id(number) != value:
        raise ImportRejected("portable base generation is invalid")
    return number


def _plan_id(user_id: str, bundle_hash: str, base_generation: str) -> str:
    digest = hashlib.sha256(b"personal-operator.import-plan.v1\0")
    for value in (user_id, bundle_hash, base_generation):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return f"importplan_{digest.hexdigest()}"


@dataclass(frozen=True, slots=True)
class PreparedActivationV1:
    """One canonical plan plus its durable exact activation approval."""

    plan: ImportPlanV1
    activation_approval: str
    expected_generation: int

    @property
    def bundle_hash(self) -> str:
        return self.plan.bundle_hash

    @property
    def plan_id(self) -> str:
        return self.plan.plan_id

    @property
    def base_generation(self) -> str:
        return self.plan.base_generation


@dataclass(frozen=True, slots=True)
class _ParsedBundle:
    manifest: dict
    bundle_hash: str
    records: dict
    workspace: dict


class PortableImporter:
    def __init__(self, *, staging, now=None) -> None:
        required = {
            "activate_once",
            "load_generation",
            "load_live",
            "prepare_activation",
        }
        if any(not callable(getattr(staging, method, None)) for method in required):
            raise TypeError("staged-generation store is invalid")
        if now is not None and not callable(now):
            raise TypeError("portable import clock is invalid")
        self._staging = staging
        self._now = now or (lambda: int(time.time()))

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
            # Bound decompressed size on declared ZipInfo.file_size BEFORE the
            # manifest lookup or any read, so a decompression bomb is rejected
            # without materializing a payload.
            total_uncompressed = 0
            for info in archive.infolist():
                if info.file_size > MAX_ENTRY_UNCOMPRESSED_BYTES:
                    raise BundleIntegrityError(
                        "portable bundle entry exceeds its uncompressed size limit"
                    )
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    raise BundleIntegrityError(
                        "portable bundle exceeds its total uncompressed size limit"
                    )
            if "manifest.json" not in names:
                raise BundleIntegrityError("portable bundle has no manifest")
            # Read each entry under a hard ceiling so a lying ZipInfo.file_size
            # cannot inflate past the per-entry bound during decompression.
            payloads = {}
            for name in names:
                try:
                    with archive.open(name) as stream:
                        payload = stream.read(MAX_ENTRY_UNCOMPRESSED_BYTES + 1)
                except Exception as error:
                    raise BundleIntegrityError(
                        "portable bundle entry could not be read"
                    ) from error
                if len(payload) > MAX_ENTRY_UNCOMPRESSED_BYTES:
                    raise BundleIntegrityError(
                        "portable bundle entry exceeds its uncompressed size limit"
                    )
                payloads[name] = payload

        manifest_bytes = payloads["manifest.json"]
        try:
            manifest = strict_json_loads(manifest_bytes)
        except (ValueError, TypeError) as error:
            raise BundleIntegrityError("portable manifest is not JSON") from error
        if not isinstance(manifest, Mapping):
            raise BundleIntegrityError("portable manifest is invalid")
        # Reject non-canonical framing: the manifest must be byte-identical to a
        # canonical re-serialization of its parsed content.
        if canonical_json(manifest) != manifest_bytes:
            raise BundleIntegrityError("portable manifest is not canonical")
        try:
            manifest = PortableStateManifestV2.from_mapping(manifest).to_mapping()
        except ContractValidationError as error:
            raise BundleIntegrityError("portable manifest fields are invalid") from error
        if manifest["bundleHash"] != complete_bundle_hash(manifest):
            raise BundleIntegrityError("portable manifest bundle hash is invalid")

        objects = manifest["objects"]

        declared_paths: set[str] = set()
        records: dict[str, object] = {}
        workspace: dict[str, bytes] = {}
        for entry in objects:
            category = self._verify_descriptor(entry, payloads, declared_paths)
            path = entry["path"]
            payload = payloads[path]
            if category in RECORD_CATEGORIES:
                try:
                    records[category] = strict_json_loads(payload)
                except (ValueError, TypeError) as error:
                    raise BundleIntegrityError(
                        "portable record object is not JSON"
                    ) from error
                if canonical_json(records[category]) != payload:
                    raise BundleIntegrityError(
                        "portable record object is not canonical"
                    )
            else:  # workspace
                workspace[path[len("files/") :]] = payload

        # Every non-manifest zip entry must be declared (no extra/missing).
        zip_objects = set(payloads) - {"manifest.json"}
        if zip_objects != declared_paths:
            raise BundleIntegrityError(
                "portable bundle entries do not match the manifest"
            )
        if set(records) != RECORD_CATEGORIES:
            raise BundleIntegrityError("portable record categories are invalid")

        return _ParsedBundle(
            manifest=dict(manifest),
            bundle_hash=manifest["bundleHash"],
            records=records,
            workspace=workspace,
        )

    @staticmethod
    def _verify_descriptor(entry, payloads, declared_paths) -> str:
        if not isinstance(entry, Mapping) or set(entry) != {
            "path",
            "type",
            "size",
            "sha256",
        }:
            raise BundleIntegrityError("portable object descriptor is invalid")
        path = entry["path"]
        object_type = entry["type"]
        if not isinstance(path, str):
            raise BundleIntegrityError("portable object path is invalid")
        if path in declared_paths:
            raise BundleIntegrityError("portable bundle declares a duplicate path")
        # Category and prefix must agree, and the path must be traversal-safe.
        category = TYPE_CATEGORIES.get(object_type)
        if category in RECORD_CATEGORIES:
            if path != f"records/{category}.json":
                raise BundleIntegrityError("portable record path is invalid")
        elif category == "workspace":
            if not path.startswith("files/"):
                raise BundleIntegrityError("portable workspace path is invalid")
            try:
                safe_path(path[len("files/") :])
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
        return category

    # -- policy -------------------------------------------------------------

    @staticmethod
    def _enforce_policy(bundle: _ParsedBundle) -> None:
        if not set(EXCLUDED_CLASSES).issubset(bundle.manifest["excludedClasses"]):
            raise ImportRejected("portable excluded classes are incomplete")
        validate_bundle_records(bundle.records)

        for category, payload in bundle.records.items():
            if not isinstance(payload, list) or any(
                not isinstance(row, Mapping) for row in payload
            ):
                raise ImportRejected(
                    "portable live state requires structured record rows"
                )
            for row in payload:
                scan_for_secrets(row)
                record_type = row.get("recordType")
                if isinstance(record_type, str) and (
                    record_type.upper() in _FORBIDDEN_RECORD_TYPES
                ):
                    raise ImportRejected("portable bundle carries a forbidden record")
                normalized_keys = {
                    re.sub(r"[^a-z0-9]", "", key.casefold())
                    for key in row
                    if isinstance(key, str)
                }
                if "deletionstatus" in normalized_keys:
                    raise ImportRejected("portable bundle carries a deletion tombstone")
                status = row.get("status")
                if (
                    isinstance(status, str)
                    and status.upper() in _ACTIVE_AUTHORITY_MARKERS
                ):
                    raise ImportRejected("portable bundle carries live authority")
                if "connectionenvelope" in normalized_keys:
                    raise ImportRejected("portable bundle carries live authority")
                state = row.get("state")
                if isinstance(state, str) and state.upper() in _PENDING_STATES:
                    raise ImportRejected("portable bundle carries a pending effect")

    # -- public API ---------------------------------------------------------

    @staticmethod
    def _plan(
        bundle: _ParsedBundle,
        *,
        target_user_id: str,
        base_generation: int,
    ) -> ImportPlanV1:
        base = _generation_id(base_generation)
        return ImportPlanV1.from_mapping(
            {
                "schema": ImportPlanV1.SCHEMA,
                "planId": _plan_id(target_user_id, bundle.bundle_hash, base),
                "userId": target_user_id,
                "bundleHash": bundle.bundle_hash,
                "baseGeneration": base,
                "objectCount": len(bundle.manifest["objects"]),
                "totalBytes": sum(
                    descriptor["size"] for descriptor in bundle.manifest["objects"]
                ),
                "schedulesDisabled": True,
                "connectorsDisconnected": True,
                "effectsReplayable": False,
            }
        )

    def build_plan(
        self,
        bundle_bytes: object,
        *,
        target_user_id: object,
    ) -> ImportPlanV1:
        caller = _user_id(target_user_id)
        bundle = self._parse(bundle_bytes)
        self._enforce_policy(bundle)
        base_generation = self._staging.load_generation(caller)
        return self._plan(
            bundle,
            target_user_id=caller,
            base_generation=base_generation,
        )

    def _approved_plan(
        self,
        bundle: _ParsedBundle,
        *,
        target_user_id: str,
        approved_bundle_hash: object,
        approved_plan_id: object,
        approved_base_generation: object,
    ) -> tuple[ImportPlanV1, int]:
        expected_generation = _generation_number(approved_base_generation)
        plan = self._plan(
            bundle,
            target_user_id=target_user_id,
            base_generation=expected_generation,
        )
        if (
            approved_bundle_hash != plan.bundle_hash
            or approved_plan_id != plan.plan_id
            or approved_base_generation != plan.base_generation
        ):
            raise ImportRejected("approved import plan does not match the bundle")
        return plan, expected_generation

    def _staged_payload(self, bundle: _ParsedBundle, *, target_user_id: str) -> dict:
        records = retarget_records(
            bundle.records,
            target_user_id=target_user_id,
        )
        return {
            "format": FORMAT,
            "records": records,
            "workspace": {
                path: {
                    "encoding": "base64",
                    "data": base64.b64encode(payload).decode("ascii"),
                    "sha256": object_sha256(payload),
                }
                for path, payload in bundle.workspace.items()
            },
            "landing": default_landing(),
        }

    def prepare_activation(
        self,
        bundle_bytes: object,
        *,
        target_user_id: object,
        approved_bundle_hash: object,
        approved_plan_id: object,
        approved_base_generation: object,
    ) -> PreparedActivationV1:
        """Prepare one durable approval without changing the live generation."""

        caller = _user_id(target_user_id)
        bundle = self._parse(bundle_bytes)
        self._enforce_policy(bundle)
        plan, expected_generation = self._approved_plan(
            bundle,
            target_user_id=caller,
            approved_bundle_hash=approved_bundle_hash,
            approved_plan_id=approved_plan_id,
            approved_base_generation=approved_base_generation,
        )
        approval = self._staging.prepare_activation(
            caller,
            bundle_hash=bundle.bundle_hash,
            expected_generation=expected_generation,
            staged=self._staged_payload(bundle, target_user_id=caller),
        )
        if (
            not isinstance(approval, Mapping)
            or set(approval)
            != {"activationApproval", "bundleHash", "expectedGeneration"}
            or not isinstance(approval.get("activationApproval"), str)
            or not 8 <= len(approval["activationApproval"]) <= 128
            or approval.get("bundleHash") != bundle.bundle_hash
            or approval.get("expectedGeneration") != expected_generation
        ):
            raise ImportUncertain("portable activation approval is invalid")
        return PreparedActivationV1(
            plan=plan,
            activation_approval=approval["activationApproval"],
            expected_generation=expected_generation,
        )

    def activate(
        self,
        bundle_bytes: object,
        *,
        approved_bundle_hash: object,
        approved_plan_id: object,
        approved_base_generation: object,
        target_user_id: object,
        activation_approval: object,
        expected_generation: object,
    ) -> ImportReceiptV1:
        caller = _user_id(target_user_id)
        bundle = self._parse(bundle_bytes)
        self._enforce_policy(bundle)
        plan, plan_generation = self._approved_plan(
            bundle,
            target_user_id=caller,
            approved_bundle_hash=approved_bundle_hash,
            approved_plan_id=approved_plan_id,
            approved_base_generation=approved_base_generation,
        )

        if (
            not isinstance(activation_approval, str)
            or not 8 <= len(activation_approval) <= 128
        ):
            raise ImportRejected("portable activation approval is invalid")
        if (
            isinstance(expected_generation, bool)
            or not isinstance(expected_generation, int)
            or expected_generation != plan_generation
        ):
            raise ImportRejected("staged generation is invalid")

        imported_at = self._now()
        try:
            receipt = ImportReceiptV1.from_mapping(
                {
                    "schema": ImportReceiptV1.SCHEMA,
                    "planId": plan.plan_id,
                    "userId": caller,
                    "bundleHash": plan.bundle_hash,
                    "state": "ACTIVATED",
                    "activatedGeneration": _generation_id(
                        expected_generation + 1
                    ),
                    "importedAt": imported_at,
                }
            )
        except (ContractValidationError, ImportRejected) as error:
            raise ImportUncertain("portable import receipt is invalid") from error

        staged = self._staged_payload(bundle, target_user_id=caller)
        try:
            generation = self._staging.activate_once(
                caller,
                bundle_hash=bundle.bundle_hash,
                activation_approval=activation_approval,
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
        if generation != expected_generation + 1:
            raise ImportUncertain("staged activation returned an invalid generation")
        return receipt
