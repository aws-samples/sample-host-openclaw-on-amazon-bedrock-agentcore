#!/usr/bin/env python3
"""Emit development evidence from the concrete in-process runtime builder.

The serialized closure is intentionally one-way: the production image command
cannot load it. Production mints and consumes the same private capability in a
single process from reviewed tools, exact Git objects, and pinned offline
package-manager artifacts.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from release_tools.image_production_v2 import (
    TrustedRuntimeBuildClosureFactoryV2,
    _regular_bytes,
    open_reviewed_local_execution,
)
from release_tools.image_publication import (
    MAX_BLOB_BYTES,
    ImagePublicationError,
    RuntimeBuildClosureError,
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build exact development runtime-closure evidence"
    )
    parser.add_argument("--release-repository", type=Path, required=True)
    parser.add_argument("--openclaw-repository", type=Path, required=True)
    parser.add_argument(
        "--openclaw-package-manager-artifact", type=Path, required=True
    )
    parser.add_argument(
        "--bridge-package-manager-artifact", type=Path, required=True
    )
    parser.add_argument("--release-commit", required=True)
    parser.add_argument("--release-tree", required=True)
    parser.add_argument("--openclaw-commit", required=True)
    parser.add_argument("--openclaw-tree", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _write_closure(output: Path, artifacts: dict[str, bytes]) -> None:
    output.mkdir(mode=0o755, parents=False, exist_ok=False)
    for name, payload in sorted(artifacts.items()):
        descriptor = os.open(
            output / name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short closure write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.output.exists() or arguments.output.is_symlink():
            raise RuntimeBuildClosureError(
                "runtime closure output already exists"
            )
        with open_reviewed_local_execution() as execution:
            capability = TrustedRuntimeBuildClosureFactoryV2(
                execution=execution,
                release_repository=arguments.release_repository,
                openclaw_repository=arguments.openclaw_repository,
            ).build(
                release_commit=arguments.release_commit,
                release_tree=arguments.release_tree,
                openclaw_commit=arguments.openclaw_commit,
                openclaw_tree=arguments.openclaw_tree,
                openclaw_package_manager_artifact=_regular_bytes(
                    arguments.openclaw_package_manager_artifact,
                    maximum=MAX_BLOB_BYTES,
                    label="OpenClaw package-manager artifact",
                ),
                bridge_package_manager_artifact=_regular_bytes(
                    arguments.bridge_package_manager_artifact,
                    maximum=MAX_BLOB_BYTES,
                    label="bridge package-manager artifact",
                ),
            )
        _write_closure(arguments.output, capability.development_artifacts())
    except (OSError, ImagePublicationError) as error:
        print(f"runtime build closure rejected: {error}", file=sys.stderr)
        return 1
    print(
        _canonical_json(
            {
                "schema": "personal-operator.runtime-build-closure-result.v1",
                "manifestSha256": capability.manifest_sha256,
                "output": str(arguments.output),
            }
        ).decode("utf-8")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
