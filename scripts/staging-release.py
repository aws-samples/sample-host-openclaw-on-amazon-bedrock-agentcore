#!/usr/bin/env python3
"""Repository entry point for the immutable staging release transaction."""

from __future__ import annotations

from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from release_tools.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
