#!/usr/bin/env python3
"""Container entrypoint shim for the networkless compute job.

The image ships the reviewed ``compute`` package alongside this shim. All fence
logic lives in :mod:`compute.runner` (the reviewed package module); this file
only wires the interpreter path and delegates so the Docker ENTRYPOINT stays a
single stable command. The real Docker build/ARM64/static-scan gates are OPEN
and are not exercised locally.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The image lays the package tree at /app/lambda; make it importable first.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "lambda"
if _PACKAGE_ROOT.is_dir():
    sys.path.insert(0, str(_PACKAGE_ROOT))

from compute.runner import main  # noqa: E402


if __name__ == "__main__":  # pragma: no cover - container entry
    raise SystemExit(main())
