"""RED-first hostile tests for the networkless compute model layer."""

from __future__ import annotations

import re

import pytest

from capabilities.contracts import ComputeJobSpecV1, canonical_sha256

from compute import models

_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
PINNED_DIGEST = "sha256:" + "a" * 64


def _command():
    return {"mode": "ARGV", "value": ["python", "job.py"]}


def _input_files():
    return [
        {"path": "in/a.txt", "sha256": "1" * 64, "size": 3},
        {"path": "in/b.txt", "sha256": "2" * 64, "size": 5},
    ]


def test_small_profile_pins_networkless_isolation_limits():
    profile = models.SMALL
    assert profile.name == "SMALL"
    # Every breach guard the runner must enforce has one positive, bounded value.
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
        value = getattr(profile, attribute)
        assert isinstance(value, int) and not isinstance(value, bool)
        assert value > 0
    # The contract caps a single output file at 64 MiB; the profile stays within.
    assert profile.max_output_file_bytes <= 64 * 1024 * 1024
    assert profile.max_output_total_bytes >= profile.max_output_file_bytes
    assert models.RESOURCE_PROFILES["SMALL"] is profile


def test_job_id_is_content_addressed_and_byte_stable():
    first = models.derive_job_id(
        user_id="user_alpha",
        invocation_id="invocation_12345678",
        args_hash="3" * 64,
    )
    again = models.derive_job_id(
        user_id="user_alpha",
        invocation_id="invocation_12345678",
        args_hash="3" * 64,
    )
    assert first == again
    assert _OPAQUE_ID.fullmatch(first) is not None
    assert first.startswith("job_")


def test_job_id_changes_when_any_dedupe_component_changes():
    base = dict(
        user_id="user_alpha",
        invocation_id="invocation_12345678",
        args_hash="3" * 64,
    )
    baseline = models.derive_job_id(**base)
    variants = [
        {**base, "user_id": "user_beta"},
        {**base, "invocation_id": "invocation_87654321"},
        {**base, "args_hash": "4" * 64},
    ]
    derived = {models.derive_job_id(**variant) for variant in variants}
    assert baseline not in derived
    assert len(derived) == len(variants)


def test_input_digest_binds_the_sorted_content_hashed_manifest():
    files = _input_files()
    digest = models.derive_input_digest(files)
    assert digest == canonical_sha256(sorted(files, key=lambda item: item["path"]))
    # Reordering the manifest cannot change the bound identity.
    assert models.derive_input_digest(list(reversed(files))) == digest
    # A single mutated byte in the staged manifest yields a different digest.
    mutated = [dict(files[0], sha256="9" * 64), files[1]]
    assert models.derive_input_digest(mutated) != digest


def test_build_job_spec_uses_only_the_single_pinned_image_digest():
    spec = models.build_job_spec(
        job_id="job_" + "a" * 64,
        user_id="user_alpha",
        image_digest=PINNED_DIGEST,
        command=_command(),
        input_files=_input_files(),
        profile=models.SMALL,
        now=1_800_000_000,
    )
    assert isinstance(spec, ComputeJobSpecV1)
    assert spec.image_digest == PINNED_DIGEST
    assert spec.network == "NONE"
    assert spec.resource_profile == "SMALL"
    assert spec.deadline == 1_800_000_000 + models.SMALL.deadline_seconds


def test_build_job_spec_rejects_a_non_pinned_or_mutable_image_digest():
    for bad in ("latest", "sha256:zz", "", "sha256:" + "a" * 63, None):
        with pytest.raises(Exception):
            models.build_job_spec(
                job_id="job_" + "a" * 64,
                user_id="user_alpha",
                image_digest=bad,
                command=_command(),
                input_files=_input_files(),
                profile=models.SMALL,
                now=1_800_000_000,
            )
