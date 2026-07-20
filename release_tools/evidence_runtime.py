"""Authenticate and snapshot the minimal Python SDK release boundary."""

from __future__ import annotations

import hashlib
from importlib import metadata
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Iterable

from release_tools.contracts import canonical_json_bytes


REQUIRED_EVIDENCE_DISTRIBUTIONS = (
    "awscrt",
    "boto3",
    "botocore",
    "jmespath",
    "python-dateutil",
    "s3transfer",
    "six",
    "urllib3",
)
MAX_EVIDENCE_FILE_BYTES = 64 * 1024 * 1024
MAX_EVIDENCE_RUNTIME_BYTES = 192 * 1024 * 1024


class EvidenceRuntimeError(RuntimeError):
    """The installed SDK runtime cannot form reviewed release evidence."""


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _regular_file_digest(
    source: Path,
    destination: Path | None,
) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise EvidenceRuntimeError(
            f"evidence runtime file is unavailable: {source.name}"
        ) from error
    output_descriptor: int | None = None
    digest = hashlib.sha256()
    size = 0
    try:
        observed = os.fstat(descriptor)
        observed_identity = (
            observed.st_dev,
            observed.st_ino,
            observed.st_mode,
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
        )
        if not stat.S_ISREG(observed.st_mode):
            raise EvidenceRuntimeError("evidence runtime contains a non-file")
        if observed.st_size > MAX_EVIDENCE_FILE_BYTES:
            raise EvidenceRuntimeError("evidence runtime file exceeds its bound")
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            output_descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o400,
            )
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_EVIDENCE_FILE_BYTES:
                raise EvidenceRuntimeError(
                    "evidence runtime file exceeds its bound"
                )
            digest.update(chunk)
            if output_descriptor is not None:
                view = memoryview(chunk)
                while view:
                    written = os.write(output_descriptor, view)
                    if written <= 0:
                        raise EvidenceRuntimeError(
                            "evidence runtime snapshot write failed"
                        )
                    view = view[written:]
        after = os.fstat(descriptor)
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if after_identity != observed_identity:
            raise EvidenceRuntimeError(
                "evidence runtime file changed while being retained"
            )
        if output_descriptor is not None:
            os.fsync(output_descriptor)
    finally:
        os.close(descriptor)
        if output_descriptor is not None:
            os.close(output_descriptor)
    return digest.hexdigest(), size


def snapshot_evidence_runtime(
    site_packages: Path,
    *,
    destination: Path | None = None,
    required_distributions: Iterable[str] = REQUIRED_EVIDENCE_DISTRIBUTIONS,
) -> str:
    """Hash and optionally retain the exact SDK distribution file boundary."""

    try:
        source_root = Path(site_packages).resolve(strict=True)
    except OSError as error:
        raise EvidenceRuntimeError(
            "evidence runtime site-packages does not exist"
        ) from error
    if not source_root.is_dir():
        raise EvidenceRuntimeError("evidence runtime site-packages is not a directory")
    retained_root: Path | None = None
    if destination is not None:
        retained_root = Path(destination)
        try:
            retained_root.mkdir(mode=0o700)
        except OSError as error:
            raise EvidenceRuntimeError(
                "evidence runtime snapshot directory is unavailable"
            ) from error
        if any(retained_root.iterdir()):
            raise EvidenceRuntimeError(
                "evidence runtime snapshot directory is not empty"
            )

    installed: dict[str, metadata.Distribution] = {}
    for distribution in metadata.distributions(path=[str(source_root)]):
        name = distribution.metadata.get("Name")
        if isinstance(name, str) and name:
            normalized = _normalized_distribution_name(name)
            if normalized in installed:
                raise EvidenceRuntimeError(
                    "evidence runtime has duplicate distributions"
                )
            installed[normalized] = distribution

    required = tuple(
        sorted({_normalized_distribution_name(name) for name in required_distributions})
    )
    if not required:
        raise EvidenceRuntimeError("evidence runtime distribution set is empty")
    missing = [name for name in required if name not in installed]
    if missing:
        raise EvidenceRuntimeError(
            "evidence runtime is missing required distributions: "
            + ", ".join(missing)
        )

    manifest_distributions: list[dict[str, object]] = []
    observed_paths: set[str] = set()
    total_bytes = 0
    for name in required:
        distribution = installed[name]
        version = distribution.version
        if not isinstance(version, str) or not version:
            raise EvidenceRuntimeError("evidence runtime version is missing")
        files: list[dict[str, object]] = []
        for entry in sorted(distribution.files or (), key=lambda item: str(item)):
            relative = PurePosixPath(str(entry))
            if relative.is_absolute() or ".." in relative.parts:
                # Console entrypoints outside site-packages are not imported by
                # the in-process SDK authority and are deliberately excluded.
                continue
            if relative.suffix == ".pyc" or "__pycache__" in relative.parts:
                continue
            relative_text = relative.as_posix()
            if relative_text in observed_paths:
                raise EvidenceRuntimeError(
                    "evidence runtime distribution files overlap"
                )
            observed_paths.add(relative_text)
            candidate = source_root.joinpath(*relative.parts)
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(source_root)
            except (OSError, ValueError) as error:
                raise EvidenceRuntimeError(
                    "evidence runtime file escapes site-packages"
                ) from error
            if resolved != candidate.absolute() or candidate.is_symlink():
                raise EvidenceRuntimeError(
                    "evidence runtime contains a symlinked file"
                )
            file_digest, file_size = _regular_file_digest(
                candidate,
                retained_root.joinpath(*relative.parts)
                if retained_root is not None
                else None,
            )
            total_bytes += file_size
            if total_bytes > MAX_EVIDENCE_RUNTIME_BYTES:
                raise EvidenceRuntimeError(
                    "evidence runtime exceeds its total byte bound"
                )
            files.append(
                {
                    "path": relative_text,
                    "sha256": file_digest,
                    "size": file_size,
                }
            )
        if not files:
            raise EvidenceRuntimeError(
                f"evidence runtime distribution {name} has no retained files"
            )
        manifest_distributions.append(
            {"name": name, "version": version, "files": files}
        )

    executable_digest, executable_size = _regular_file_digest(
        Path(sys.executable).resolve(strict=True),
        None,
    )
    manifest = {
        "schema": "personal-operator.evidence-runtime.v1",
        "pythonImplementation": sys.implementation.name,
        "pythonVersion": ".".join(str(item) for item in sys.version_info[:3]),
        "interpreter": {
            "sha256": executable_digest,
            "size": executable_size,
        },
        "distributions": manifest_distributions,
    }
    return hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()


__all__ = [
    "EvidenceRuntimeError",
    "REQUIRED_EVIDENCE_DISTRIBUTIONS",
    "snapshot_evidence_runtime",
]
