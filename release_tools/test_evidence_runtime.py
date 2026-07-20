from __future__ import annotations

import csv
from pathlib import Path

import pytest

from release_tools.evidence_runtime import (
    EvidenceRuntimeError,
    snapshot_evidence_runtime,
)


def _distribution(
    site_packages: Path,
    *,
    name: str = "example-sdk",
    version: str = "1.2.3",
) -> Path:
    package = site_packages / "example_sdk"
    package.mkdir(parents=True)
    module = package / "__init__.py"
    module.write_text("VALUE = 'reviewed'\n", encoding="utf-8")
    dist_info = site_packages / f"example_sdk-{version}.dist-info"
    dist_info.mkdir()
    metadata = dist_info / "METADATA"
    metadata.write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        encoding="utf-8",
    )
    record = dist_info / "RECORD"
    with record.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        for path in (module, metadata, record):
            writer.writerow((path.relative_to(site_packages).as_posix(), "", ""))
    return module


def test_snapshot_binds_and_retains_the_exact_distribution_files(
    tmp_path: Path,
) -> None:
    site_packages = tmp_path / "site-packages"
    module = _distribution(site_packages)

    first = snapshot_evidence_runtime(
        site_packages,
        destination=tmp_path / "retained",
        required_distributions=("example_sdk",),
    )

    assert len(first) == 64
    retained = tmp_path / "retained" / "example_sdk" / "__init__.py"
    assert retained.read_bytes() == module.read_bytes()
    assert retained.stat().st_mode & 0o777 == 0o400

    module.write_text("VALUE = 'mutated'\n", encoding="utf-8")
    second = snapshot_evidence_runtime(
        site_packages,
        required_distributions=("example-sdk",),
    )
    assert second != first


def test_snapshot_rejects_missing_or_symlinked_distribution_files(
    tmp_path: Path,
) -> None:
    site_packages = tmp_path / "site-packages"
    module = _distribution(site_packages)

    with pytest.raises(EvidenceRuntimeError, match="missing required"):
        snapshot_evidence_runtime(
            site_packages,
            required_distributions=("not-installed",),
        )

    target = tmp_path / "outside.py"
    target.write_text("VALUE = 'unreviewed'\n", encoding="utf-8")
    module.unlink()
    module.symlink_to(target)
    with pytest.raises(EvidenceRuntimeError, match="escapes|symlinked"):
        snapshot_evidence_runtime(
            site_packages,
            required_distributions=("example-sdk",),
        )


def test_snapshot_requires_a_new_empty_destination(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    _distribution(site_packages)
    destination = tmp_path / "retained"
    destination.mkdir()
    (destination / "attacker").write_text("present\n", encoding="utf-8")

    with pytest.raises(EvidenceRuntimeError, match="unavailable|not empty"):
        snapshot_evidence_runtime(
            site_packages,
            destination=destination,
            required_distributions=("example-sdk",),
        )
