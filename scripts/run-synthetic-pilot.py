#!/usr/bin/env python3
"""Emit only the canonical credential-free v1 synthetic cohort report."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "lambda"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from tests.integration.synthetic_pilot_v1 import run_synthetic_pilot  # noqa: E402


def main() -> int:
    first = run_synthetic_pilot()
    second = run_synthetic_pilot()
    if first.report_bytes != second.report_bytes:
        raise RuntimeError("synthetic pilot report is nondeterministic")
    if first.external_call_ledger or second.external_call_ledger:
        raise RuntimeError("synthetic pilot crossed an external boundary")
    sys.stdout.buffer.write(first.report_bytes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
