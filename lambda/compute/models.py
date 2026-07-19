"""Frozen networkless compute models: profiles, identity, and job specs.

This module holds only pure, dependency-free helpers. It derives the
content-addressed job identity that realizes the ``DEDUPE_KEY_REQUIRED`` retry
policy, binds the immutable input manifest, and constructs a validated
:class:`ComputeJobSpecV1` from a single pinned image digest. No ambient
authority, filesystem, or network access is reachable from here.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from capabilities.contracts import (
    ComputeJobSpecV1,
    ContractValidationError,
    canonical_sha256,
)

_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")

_JOB_ID_DOMAIN = b"personal-operator.compute-job-id.v1"


@dataclass(frozen=True, slots=True)
class ResourceProfile:
    """One immutable envelope of every enforced breach guard for a job class."""

    name: str
    deadline_seconds: int
    cpu_seconds: int
    memory_bytes: int
    pids_limit: int
    file_size_bytes: int
    max_output_files: int
    max_output_file_bytes: int
    max_output_total_bytes: int

    def __post_init__(self) -> None:
        for attribute in (
            "deadline_seconds",
            "cpu_seconds",
            "memory_bytes",
            "pids_limit",
            "file_size_bytes",
            "max_output_files",
            "max_output_file_bytes",
            "max_output_total_bytes",
        ):
            value = getattr(self, attribute)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"resource profile {attribute} must be positive")
        # The frozen file-records contract caps a single file at 64 MiB.
        if self.max_output_file_bytes > 64 * 1024 * 1024:
            raise ValueError("single output file cap exceeds the contract limit")
        if self.max_output_total_bytes < self.max_output_file_bytes:
            raise ValueError("total output cap cannot be below the single-file cap")


SMALL = ResourceProfile(
    name="SMALL",
    deadline_seconds=30,
    cpu_seconds=25,
    memory_bytes=256 * 1024 * 1024,
    pids_limit=64,
    file_size_bytes=8 * 1024 * 1024,
    max_output_files=32,
    max_output_file_bytes=8 * 1024 * 1024,
    max_output_total_bytes=16 * 1024 * 1024,
)

RESOURCE_PROFILES: Mapping[str, ResourceProfile] = MappingProxyType({"SMALL": SMALL})


def resolve_profile(name: str) -> ResourceProfile:
    profile = RESOURCE_PROFILES.get(name)
    if profile is None:
        raise ContractValidationError("resourceProfile is unsupported")
    return profile


def _hash_identity(domain: bytes, *values: str) -> str:
    import hashlib

    digest = hashlib.sha256(domain + b"\0")
    for value in values:
        if not isinstance(value, str):
            raise ContractValidationError("job identity components must be strings")
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def derive_job_id(*, user_id: str, invocation_id: str, args_hash: str) -> str:
    """Return the content-addressed dedupe key for one compute submission.

    The identity binds the requesting user, the turn invocation, and the exact
    canonical arguments hash. Re-deriving with identical inputs is byte-stable;
    any single changed component yields a distinct ``_OPAQUE_ID``-valid id.
    """

    for label, value in (
        ("userId", user_id),
        ("invocationId", invocation_id),
        ("argsHash", args_hash),
    ):
        if not isinstance(value, str) or not value:
            raise ContractValidationError(f"{label} is invalid")
    return f"job_{_hash_identity(_JOB_ID_DOMAIN, user_id, invocation_id, args_hash)}"


def derive_input_digest(input_files: Sequence[Mapping[str, Any]]) -> str:
    """Bind the sorted, content-hashed input manifest to one stable digest."""

    manifest = [
        {
            "path": record["path"],
            "sha256": record["sha256"],
            "size": record["size"],
        }
        for record in input_files
    ]
    manifest.sort(key=lambda item: item["path"])
    return canonical_sha256(manifest)


def build_job_spec(
    *,
    job_id: str,
    user_id: str,
    image_digest: str,
    command: Mapping[str, Any],
    input_files: Sequence[Mapping[str, Any]],
    profile: ResourceProfile,
    now: int,
) -> ComputeJobSpecV1:
    """Construct a validated job spec from the single pinned image digest.

    The image digest is the only trusted-caller pin. Any attempt to supply a
    mutable tag or a malformed digest fails closed at contract validation.
    """

    if not isinstance(image_digest, str) or _IMAGE_DIGEST.fullmatch(image_digest) is None:
        raise ContractValidationError("compute image must be a pinned sha256 digest")
    if not isinstance(profile, ResourceProfile):
        raise ContractValidationError("compute job requires a resource profile")
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise ContractValidationError("compute job clock is invalid")
    return ComputeJobSpecV1.from_mapping(
        {
            "schema": ComputeJobSpecV1.SCHEMA,
            "jobId": job_id,
            "userId": user_id,
            "imageDigest": image_digest,
            "command": dict(command),
            "inputFiles": [dict(record) for record in input_files],
            "resourceProfile": profile.name,
            "deadline": now + profile.deadline_seconds,
            "network": "NONE",
        }
    )


__all__ = [
    "RESOURCE_PROFILES",
    "ResourceProfile",
    "SMALL",
    "build_job_spec",
    "derive_input_digest",
    "derive_job_id",
    "resolve_profile",
]
