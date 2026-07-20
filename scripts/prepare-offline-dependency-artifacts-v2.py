#!/usr/bin/env python3
"""Prepare two-attempt, offline-proven dependency artifacts.

The command has no cloud/provider client. Real network acquisition remains
behind the module's explicit integration gate, and output is accepted only at
a caller-provided path that does not already exist.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from release_tools.offline_dependency_artifacts_v2 import (
    ArtifactGenerationError,
    canonical_result,
    prepare_offline_dependency_artifacts,
)


class _ClosedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise ArtifactGenerationError("command arguments are invalid")


def _parser() -> argparse.ArgumentParser:
    parser = _ClosedArgumentParser(
        description="Prepare deterministic offline dependency artifacts"
    )
    parser.add_argument("--openclaw-repository", type=Path, required=True)
    parser.add_argument("--release-repository", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        openclaw, bridge = prepare_offline_dependency_artifacts(
            openclaw_repository=arguments.openclaw_repository,
            release_repository=arguments.release_repository,
            output=arguments.output,
        )
        result = canonical_result(
            output=Path(arguments.output.name),
            openclaw=openclaw,
            bridge=bridge,
        )
    except (ArtifactGenerationError, OSError):
        print(
            "offline dependency artifact generation rejected",
            file=sys.stderr,
        )
        return 1
    print(result.decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
