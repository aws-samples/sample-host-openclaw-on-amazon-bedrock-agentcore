#!/usr/bin/env python3
"""Inactive image-shape entrypoint shim for the local compute harness.

The reference image shape places the ``compute`` package alongside this shim.
All defense-in-depth API fence logic lives in :mod:`compute.runner`; this file
only wires the interpreter path and delegates. There is no active image or
launcher. Docker build, ARM64, static-scan, and live-isolation gates are OPEN.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The image lays the package tree at /app/lambda; make it importable first.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "lambda"
if _PACKAGE_ROOT.is_dir():
    sys.path.insert(0, str(_PACKAGE_ROOT))

# ``python -m compute.runner`` first creates a namespace package from this
# shim's parent directory. Remove only that namespace before resolving the real
# regular package now placed first on sys.path; otherwise the import recurses
# into this shim.
if __package__ == "compute":
    sys.modules.pop("compute", None)

from compute.runner import main  # noqa: E402


if __name__ == "__main__":  # pragma: no cover - container entry
    raise SystemExit(main())
