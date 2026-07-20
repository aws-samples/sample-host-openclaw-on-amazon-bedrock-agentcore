"""Reviewed operator command surface over the accepted release v2 session.

This is the one operator entrypoint that drives ``AcceptedReleaseSessionV2``.
It owns no AWS authority of its own: every real-AWS effect is routed through the
accepted session, which authenticates the one explicit bootstrap profile and
advances at most one step.  ``--status``/``--preflight`` are credential-free
inspections that perform no mutation and require no AWS credentials.
``--run-one`` drives exactly one accepted-session step with the same
fail-closed posture as the session itself: an UNCERTAIN outcome exits nonzero
and prints nothing, never a success acknowledgement.  The CLI performs no
dynamic import, ``eval``/``exec``, or caller-data subprocess, and never widens
authority beyond the explicit-profile session it drives.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import sys
from typing import Protocol, Sequence

from release_tools.contracts import ContractError
from release_tools.release_session_v2 import (
    AcceptedReleaseSessionV2,
    ReleaseSessionResultV2,
    ReleaseSessionV2Error,
)


_SHA256 = re.compile(r"[0-9a-f]{64}")
_UNCERTAIN_ACTIONS = frozenset({"DISPATCHED_UNCERTAIN", "OBSERVED_UNCERTAIN"})


class ReleaseCliV2Error(RuntimeError):
    """The requested v2 operator operation is unsafe, ambiguous, or incomplete."""


class ReleaseSessionDriver(Protocol):
    """The exact accepted-session surface this CLI is permitted to drive."""

    @classmethod
    def status(
        cls, root: Path, *, expected_plan_sha256: str
    ) -> ReleaseSessionResultV2: ...

    @classmethod
    def run_one(
        cls,
        root: Path,
        *,
        expected_plan_sha256: str,
        site_packages: Path,
        aws_directory: Path,
    ) -> ReleaseSessionResultV2: ...


def _validated_digest(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ReleaseCliV2Error(
            "--expected-plan-sha256 must be an exact 64-hex plan digest"
        )
    return value


def _validated_directory(value: object, *, label: str) -> Path:
    if value is None:
        raise ReleaseCliV2Error(f"{label} is required for this mode")
    if not isinstance(value, (str, Path)):
        raise ReleaseCliV2Error(f"{label} path is invalid")
    candidate = Path(value)
    if not candidate.is_absolute() or os.fspath(candidate) in {"", ".", ".."}:
        raise ReleaseCliV2Error(f"{label} must be an absolute path")
    if candidate.is_symlink():
        raise ReleaseCliV2Error(f"{label} must not be a symlink")
    try:
        metadata = os.lstat(candidate)
    except OSError as error:
        raise ReleaseCliV2Error(f"{label} does not exist") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseCliV2Error(f"{label} is not a directory")
    return candidate


def _validated_root(value: object) -> Path:
    return _validated_directory(value, label="--root")


def _require_certain(result: ReleaseSessionResultV2) -> None:
    """Fail closed on any ambiguity; an UNCERTAIN step is never a success."""

    if type(result) is not ReleaseSessionResultV2:
        raise ReleaseCliV2Error("release session returned an invalid result")
    if result.state == "UNCERTAIN":
        raise ReleaseCliV2Error(
            "release session outcome is UNCERTAIN; reconcile before proceeding"
        )
    step = result.step_result
    if step is not None and step.get("action") in _UNCERTAIN_ACTIONS:
        raise ReleaseCliV2Error(
            "release session step is UNCERTAIN; reconcile before proceeding"
        )


def _emit(result: ReleaseSessionResultV2) -> None:
    sys.stdout.buffer.write(result.to_bytes())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--status",
        action="store_true",
        help="credential-free canonical session status (no mutation)",
    )
    mode.add_argument(
        "--preflight",
        action="store_true",
        help="credential-free session readiness inspection (no mutation)",
    )
    mode.add_argument(
        "--run-one",
        action="store_true",
        help="drive exactly one accepted-session step (requires AWS profile)",
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument(
        "--aws-directory",
        type=Path,
        help="owner-only directory holding the exact bootstrap AWS profile",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    production_site_packages: Path | None = None,
    session_factory: ReleaseSessionDriver | None = None,
) -> int:
    args = _parser().parse_args(argv)
    session = session_factory if session_factory is not None else AcceptedReleaseSessionV2
    try:
        root = _validated_root(args.root)
        expected_plan_sha256 = _validated_digest(args.expected_plan_sha256)
        if args.status or args.preflight:
            result = session.status(
                root, expected_plan_sha256=expected_plan_sha256
            )
        else:  # --run-one; argparse guarantees exactly one mode
            site_packages = _validated_directory(
                production_site_packages, label="production site-packages"
            )
            aws_directory = _validated_directory(
                args.aws_directory, label="--aws-directory"
            )
            result = session.run_one(
                root,
                expected_plan_sha256=expected_plan_sha256,
                site_packages=site_packages,
                aws_directory=aws_directory,
            )
            _require_certain(result)
    except (
        ContractError,
        OSError,
        ReleaseCliV2Error,
        ReleaseSessionV2Error,
    ) as error:
        print(f"release cli v2: {error}", file=sys.stderr)
        return 1
    _emit(result)
    return 0


__all__ = ["ReleaseCliV2Error", "main"]
