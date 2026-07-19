#!/usr/bin/env python3
"""Repository entry point for the immutable staging release transaction."""

from __future__ import annotations

import sys


if not sys.flags.isolated or not sys.flags.no_site:
    print(
        "staging release: the production entrypoint requires isolated Python "
        "with site loading disabled; "
        "use scripts/deploy.sh",
        file=sys.stderr,
    )
    raise SystemExit(2)


from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from release_tools.cli import main  # noqa: E402


if __name__ == "__main__":
    site_packages = (
        REPOSITORY_ROOT
        / ".venv"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    raise SystemExit(main(production_site_packages=site_packages))
