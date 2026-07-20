#!/usr/bin/env python3
"""Build, probe and materialize one local-only runtime image publication.

No AWS client is imported or called.  The command remains fail-closed until the
two real offline package-manager artifact digests are reviewed and committed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from release_tools.image_production_v2 import (
    LocalGitObjectArchiveExporter,
    OfflineBuildkitOciBuilder,
    OfflineContainerImageProbe,
    TrustedImageProducerV2,
    TrustedRuntimeBuildClosureFactoryV2,
    open_reviewed_local_execution,
)
from release_tools.image_publication import (
    ImagePublicationError,
    RetainedRegularFile,
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


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare exact local runtime image publication artifacts"
    )
    parser.add_argument("--release-repository", type=Path, required=True)
    parser.add_argument("--openclaw-repository", type=Path, required=True)
    parser.add_argument(
        "--openclaw-package-manager-artifact", type=Path, required=True
    )
    parser.add_argument(
        "--bridge-package-manager-artifact", type=Path, required=True
    )
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--openclaw-commit", required=True)
    parser.add_argument("--openclaw-tree", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--created", required=True)
    parser.add_argument(
        "--expected-capability-catalog-digest", required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _write_exclusive(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short artifact write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _materialize(output: Path, bundle) -> None:
    parent = output.parent
    if output.exists() or output.is_symlink():
        raise ImagePublicationError("image publication output already exists")
    parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{output.name}.tmp-",
            dir=parent,
        )
    )
    try:
        temporary.chmod(0o700)
        effects_root = temporary / "effects"
        effects_root.mkdir(mode=0o700)
        plan_payload = bundle.plan.to_bytes()
        _write_exclusive(temporary / "image-publication-plan.json", plan_payload)
        inventory: list[dict[str, object]] = [
            {
                "path": "image-publication-plan.json",
                "sha256": _sha256(plan_payload),
                "size": len(plan_payload),
            }
        ]
        effects = bundle.publication_effects(
            expected_plan_sha256=bundle.plan_sha256
        )
        for ordinal, effect in enumerate(effects):
            payload = effect.to_private_bytes()
            relative = f"effects/{ordinal:02d}-{effect.effect_id}.private"
            _write_exclusive(temporary / relative, payload)
            inventory.append(
                {
                    "path": relative,
                    "sha256": _sha256(payload),
                    "size": len(payload),
                }
            )
        manifest = _canonical_json(
            {
                "schema": "personal-operator.image-production-output.v1",
                "publicationPlanSha256": bundle.plan_sha256,
                "runtimeImageDigest": bundle.plan.subject.digest,
                "artifacts": sorted(inventory, key=lambda item: item["path"]),
            }
        )
        _write_exclusive(temporary / "manifest.json", manifest)
        directory = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        os.rename(temporary, output)
        parent_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        with open_reviewed_local_execution() as execution:
            closure = TrustedRuntimeBuildClosureFactoryV2(
                execution=execution,
                release_repository=arguments.release_repository,
                openclaw_repository=arguments.openclaw_repository,
            ).build(
                release_commit=arguments.source_commit,
                release_tree=arguments.source_tree,
                openclaw_commit=arguments.openclaw_commit,
                openclaw_tree=arguments.openclaw_tree,
                openclaw_package_manager_artifact=RetainedRegularFile.establish(
                    arguments.openclaw_package_manager_artifact,
                    label="OpenClaw package-manager artifact",
                ),
                bridge_package_manager_artifact=RetainedRegularFile.establish(
                    arguments.bridge_package_manager_artifact,
                    label="bridge package-manager artifact",
                ),
            )
            builder = OfflineBuildkitOciBuilder(execution=execution)
            probe = OfflineContainerImageProbe(
                execution=execution,
                builder=builder,
            )
            producer = TrustedImageProducerV2(
                git_archive=LocalGitObjectArchiveExporter(
                    arguments.release_repository,
                    execution=execution,
                ),
                builder=builder,
                probe=probe,
            )
            bundle = producer.prepare(
                source_commit=arguments.source_commit,
                source_tree=arguments.source_tree,
                account=arguments.account,
                region=arguments.region,
                created=arguments.created,
                expected_capability_catalog_digest=(
                    arguments.expected_capability_catalog_digest
                ),
                trusted_runtime_build_closure=closure,
            )
        _materialize(arguments.output, bundle)
    except (OSError, ImagePublicationError, RuntimeBuildClosureError) as error:
        print(f"runtime image publication rejected: {error}", file=sys.stderr)
        return 1
    print(
        _canonical_json(
            {
                "schema": "personal-operator.image-production-result.v1",
                "output": str(arguments.output),
                "publicationPlanSha256": bundle.plan_sha256,
                "runtimeImageDigest": bundle.plan.subject.digest,
            }
        ).decode("utf-8")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
