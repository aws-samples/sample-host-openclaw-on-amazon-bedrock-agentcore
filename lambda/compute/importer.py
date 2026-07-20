"""Validated, atomic import of a compute job's fresh output tree.

Every produced file is validated fail-closed before a single byte is imported:
symlinks, hardlinks, device/fifo/socket nodes, oversize files, count/total
overflow, unsafe paths, and mid-read mutation are all rejected. Only regular
files whose bytes are stable across a trusted read and re-stat are imported,
atomically, exclusively under ``jobs/<jobId>/`` in the injected output store. A
content-addressed :class:`ComputeReceiptV1` is then persisted keyed by jobId.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence

from capabilities.contracts import (
    ComputeJobSpecV1,
    ComputeReceiptV1,
    ContractValidationError,
    _safe_path,
)

from .models import ResourceProfile


class OutputImportError(RuntimeError):
    """A produced output tree cannot cross the trusted import boundary."""


def _read_bytes(path: str) -> bytes:
    # A dedicated indirection so tests can model a concurrent writer.
    with open(path, "rb") as handle:
        return handle.read()


def _relative_safe_path(root: Path, entry: Path) -> str:
    relative = entry.relative_to(root).as_posix()
    # Reuse the exact frozen path grammar the contracts enforce on file records.
    return _safe_path(relative, "output path")


def collect_outputs(
    output_dir: Any,
    profile: ResourceProfile,
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """Validate one fresh output tree and return sorted content-addressed records.

    Raises :class:`OutputImportError` on any hostile or mutated entry.
    """

    if not isinstance(profile, ResourceProfile):
        raise OutputImportError("output validation requires a resource profile")
    root = Path(output_dir)
    try:
        real_root = root.resolve(strict=True)
    except OSError as error:
        raise OutputImportError("output directory is unavailable") from error
    if not real_root.is_dir():
        raise OutputImportError("output root is not a directory")

    records: list[dict[str, Any]] = []
    blobs: dict[str, bytes] = {}
    total_bytes = 0

    for current, dir_names, file_names in os.walk(real_root, followlinks=False):
        current_path = Path(current)
        # A directory that is itself a symlink must never be traversed.
        for name in list(dir_names):
            entry = current_path / name
            entry_stat = os.lstat(entry)
            if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
                raise OutputImportError("output contains a non-directory tree node")
        for name in file_names:
            entry = current_path / name
            entry_stat = os.lstat(entry)
            mode = entry_stat.st_mode
            if stat.S_ISLNK(mode):
                raise OutputImportError("output contains a symlink")
            if not stat.S_ISREG(mode):
                raise OutputImportError("output contains a non-regular node")
            if entry_stat.st_nlink > 1:
                raise OutputImportError("output contains a hardlinked file")
            # The regular file must resolve strictly inside the fresh root.
            try:
                resolved = entry.resolve(strict=True)
                resolved.relative_to(real_root)
            except (OSError, ValueError) as error:
                raise OutputImportError("output escapes the fresh job root") from error
            if entry_stat.st_size > profile.max_output_file_bytes:
                raise OutputImportError("output file exceeds the single-file cap")

            relative = _try_safe_path(real_root, entry)
            data = _read_bytes(str(entry))
            # Re-stat after the trusted read to detect a concurrent mutation.
            after = os.lstat(entry)
            if (
                after.st_size != entry_stat.st_size
                or len(data) != entry_stat.st_size
                or after.st_mtime_ns != entry_stat.st_mtime_ns
                or after.st_ino != entry_stat.st_ino
            ):
                raise OutputImportError("output changed during import")

            total_bytes += len(data)
            if total_bytes > profile.max_output_total_bytes:
                raise OutputImportError("output exceeds the total-bytes cap")
            if len(records) >= profile.max_output_files:
                raise OutputImportError("output exceeds the file-count cap")

            records.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                }
            )
            blobs[relative] = data

    records.sort(key=lambda record: record["path"])
    paths = [record["path"] for record in records]
    if len(set(paths)) != len(paths):
        raise OutputImportError("output paths are not unique")
    return records, blobs


def _try_safe_path(root: Path, entry: Path) -> str:
    try:
        return _relative_safe_path(root, entry)
    except (ContractValidationError, ValueError) as error:
        raise OutputImportError("output path is not a safe relative path") from error


def import_success(
    *,
    output_store: Any,
    receipt_store: Any,
    spec: ComputeJobSpecV1,
    records: Sequence[Mapping[str, Any]],
    blobs: Mapping[str, bytes],
    input_digest: str,
    started_at: int,
    completed_at: int,
) -> str:
    """Atomically import validated outputs then persist a content receipt.

    The output store commits all files or none exclusively under
    ``<userId>/jobs/<jobId>/``. A mid-import failure raises before any receipt
    is written, leaving no partial job objects and no receipt.
    """

    if not isinstance(spec, ComputeJobSpecV1):
        raise OutputImportError("import requires a validated job spec")
    files = {record["path"]: blobs[record["path"]] for record in records}
    # All-or-nothing output commit under the exact per-job prefix.
    output_store.commit_job(spec.user_id, spec.job_id, files)

    receipt = ComputeReceiptV1.from_mapping(
        {
            "schema": ComputeReceiptV1.SCHEMA,
            "jobId": spec.job_id,
            "status": "SUCCEEDED",
            "imageDigest": spec.image_digest,
            "inputDigest": input_digest,
            "outputFiles": [dict(record) for record in records],
            "startedAt": started_at,
            "completedAt": completed_at,
            "errorCode": None,
        }
    )
    return receipt_store.put_receipt(spec.user_id, receipt)


def issue_failure_receipt(
    *,
    receipt_store: Any,
    spec: ComputeJobSpecV1,
    status: str,
    input_digest: str,
    started_at: int,
    completed_at: int,
    error_code: str,
) -> str:
    """Persist a non-success receipt that can never publish output files."""

    if status == "SUCCEEDED":
        raise OutputImportError("failure receipts cannot claim success")
    receipt = ComputeReceiptV1.from_mapping(
        {
            "schema": ComputeReceiptV1.SCHEMA,
            "jobId": spec.job_id,
            "status": status,
            "imageDigest": spec.image_digest,
            "inputDigest": input_digest,
            "outputFiles": [],
            "startedAt": started_at,
            "completedAt": completed_at,
            "errorCode": error_code,
        }
    )
    return receipt_store.put_receipt(spec.user_id, receipt)


__all__ = [
    "OutputImportError",
    "collect_outputs",
    "import_success",
    "issue_failure_receipt",
]
