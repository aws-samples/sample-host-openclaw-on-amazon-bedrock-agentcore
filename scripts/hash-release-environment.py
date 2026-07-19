#!/usr/bin/env python3
"""Print the reviewed digest for the production release evidence SDK."""

from __future__ import annotations

from pathlib import Path
import sys


if not sys.flags.isolated or not sys.flags.no_site:
    print(
        "release environment hash: isolated Python with site loading disabled "
        "is required",
        file=sys.stderr,
    )
    raise SystemExit(2)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from release_tools.evidence_runtime import (  # noqa: E402
    EvidenceRuntimeError,
    snapshot_evidence_runtime,
)


def main() -> int:
    site_packages = (
        REPOSITORY_ROOT
        / ".venv"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    try:
        digest = snapshot_evidence_runtime(site_packages)
    except EvidenceRuntimeError as error:
        print(f"release environment hash: {error}", file=sys.stderr)
        return 1
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
