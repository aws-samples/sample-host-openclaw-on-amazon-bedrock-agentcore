"""Focused guards for the production hermetic runtime builder command.

These assertions bind the build-container isolation flags that must never
regress: a networkless, read-only, capability-dropped container with a build
tmpfs large enough to hold the real ~2.1 GiB offline artifact plus its install
expansion.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from release_tools import image_production_v2 as image_production
from release_tools import image_publication
from release_tools.image_production_v2 import BUILD_TMPFS_BYTES


ROOT = Path(__file__).resolve().parents[1]
_SOURCE = (ROOT / "release_tools" / "image_production_v2.py").read_text(
    encoding="utf-8"
)
_PUBLICATION_SOURCE = (
    ROOT / "release_tools" / "image_publication.py"
).read_text(encoding="utf-8")


def test_build_tmpfs_capacity_is_at_least_eight_gib() -> None:
    assert BUILD_TMPFS_BYTES >= 8 * 1024 * 1024 * 1024


def test_build_command_uses_the_named_tmpfs_capacity_constant() -> None:
    assert "size={BUILD_TMPFS_BYTES}" in _SOURCE


def test_build_command_does_not_pin_the_legacy_four_gib_tmpfs() -> None:
    assert "size=4294967296" not in _SOURCE


def test_build_command_preserves_hermetic_isolation_flags() -> None:
    for flag in (
        "--network=none",
        "--pull=never",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
    ):
        assert flag in _SOURCE


def test_build_runtime_never_write_bytes_the_whole_artifact_into_the_input_root() -> None:
    """The consumer must stream the retained artifact, not buffer it.

    Proves the retained-file streaming defect cannot regress: the build input
    root is filled by ``stream_into`` and no ``write_bytes`` of the artifact
    payload survives in the builder body.
    """

    source = inspect.getsource(
        image_production.ProductionHermeticRuntimeBuilderV2.build_runtime
    )
    assert "artifact.source.stream_into(" in source
    assert ".write_bytes(package_artifact)" not in source
    assert "write_bytes(\n                package_artifact" not in source
    assert "package_artifact" not in source


def test_offline_artifact_contract_has_no_full_artifact_materialization() -> None:
    """The contract must stream the tar, never buffer the whole artifact.

    Guards against the primary memory bomb: an ``io.BytesIO(artifact.payload)``
    (or ``.payload`` materialization) that loads the entire ~2.1 GiB blob.
    """

    source = inspect.getsource(image_publication._offline_artifact_contract)
    assert "io.BytesIO(artifact.payload)" not in source
    assert "artifact.payload" not in source
    assert "io.BytesIO(distribution)" not in source
    assert "artifact.source.open()" in source


def test_retained_regular_file_never_issues_an_unbounded_read() -> None:
    """Every retained-file read path must pass an explicit chunk size."""

    for member in (
        image_publication.RetainedRegularFile.establish,
        image_publication.RetainedRegularFile.open,
        image_publication.RetainedRegularFile.stream_into,
        image_publication._stream_copy_exclusive,
        image_publication._stream_reader_sha256_size,
    ):
        source = inspect.getsource(member)
        assert ".read()" not in source
        assert ".read(-1)" not in source
