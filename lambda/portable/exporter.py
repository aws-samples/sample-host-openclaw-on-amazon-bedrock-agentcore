"""Deterministic content-addressed portable-state v2 export.

The exporter mirrors the v1 ``UserExporter`` source contract
(``records_for_user`` + ``workspace_files``) so composition's ``_ExportSource``
is reused unchanged.  It preserves the v1 byte-reproducibility invariants
(fixed 1980 timestamps, 0o600 attrs, sorted entries, deflate) and adds a
per-object content-addressed manifest plus the complete-bundle hash that
activation binds to.
"""

from __future__ import annotations

from dataclasses import dataclass
import io
from typing import Mapping
import zipfile

from .manifest import (
    RECORD_CATEGORIES,
    CATEGORY_TYPES,
    PortableError,
    build_manifest,
    canonical_json,
    complete_bundle_hash,
    descriptor,
    safe_path,
    user_id as _user_id,
)
from .records import normalize_export_records


@dataclass(frozen=True, slots=True)
class ExportBundleV2:
    """A byte-reproducible portable bundle and its content address."""

    zip_bytes: bytes
    bundle_hash: str
    manifest: dict


class PortableExporter:
    # Lambda synchronous responses are capped at 6 MiB and base64 adds ~1/3.
    MAX_SYNC_ARCHIVE_BYTES = 4 * 1024 * 1024

    def __init__(
        self,
        source,
        *,
        max_files: int = 1_000,
        max_entry_bytes: int = 5 * 1024 * 1024,
        max_total_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self._source = source
        self._max_files = max_files
        self._max_entry = max_entry_bytes
        self._max_total = max_total_bytes

    def build(self, user_id: str) -> ExportBundleV2:
        owner_id = _user_id(user_id)
        snapshot = getattr(self._source, "snapshot_for_user", None)
        if callable(snapshot):
            captured = snapshot(owner_id)
            if not isinstance(captured, tuple) or len(captured) != 2:
                raise PortableError("export source returned an invalid snapshot")
            records, files = captured
        else:
            records = self._source.records_for_user(owner_id)
            files = self._source.workspace_files(owner_id)
        if not isinstance(records, Mapping) or not isinstance(files, Mapping):
            raise PortableError("export source returned invalid data")
        if set(records) != RECORD_CATEGORIES:
            raise PortableError("record category is not exportable")
        try:
            records = normalize_export_records(records, owner_id=owner_id)
        except PortableError:
            raise
        except (TypeError, ValueError) as error:
            raise PortableError("record export is invalid") from error

        entries: dict[str, bytes] = {}
        objects: list[dict] = []

        for category in sorted(records):
            try:
                payload = canonical_json(records[category])
            except (TypeError, ValueError) as error:
                raise PortableError("record export is not JSON") from error
            path = f"records/{category}.json"
            entries[path] = payload
            objects.append(descriptor(path, CATEGORY_TYPES[category], payload))

        for raw_path, content in files.items():
            safe = safe_path(raw_path)
            if not isinstance(content, (bytes, bytearray)):
                raise PortableError("workspace export content must be bytes")
            payload = bytes(content)
            path = f"files/{safe}"
            entries[path] = payload
            objects.append(descriptor(path, CATEGORY_TYPES["workspace"], payload))

        manifest = build_manifest(objects=objects)
        manifest_bytes = canonical_json(manifest)
        entries["manifest.json"] = manifest_bytes

        if len(entries) > self._max_files:
            raise PortableError("export contains too many files")
        total = 0
        for content in entries.values():
            if len(content) > self._max_entry:
                raise PortableError("export entry exceeds its size limit")
            total += len(content)
        if total > self._max_total:
            raise PortableError("export exceeds its total size limit")

        output = io.BytesIO()
        with zipfile.ZipFile(
            output,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            for path in sorted(entries):
                info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, entries[path])
        result = output.getvalue()
        if len(result) > self.MAX_SYNC_ARCHIVE_BYTES:
            raise PortableError(
                "export archive exceeds the synchronous delivery limit"
            )
        return ExportBundleV2(
            zip_bytes=result,
            bundle_hash=complete_bundle_hash(manifest),
            manifest=manifest,
        )

    def build_zip(self, user_id: str) -> bytes:
        """Implement the trusted web export port with portable-v2 bytes."""

        return self.build(user_id).zip_bytes
