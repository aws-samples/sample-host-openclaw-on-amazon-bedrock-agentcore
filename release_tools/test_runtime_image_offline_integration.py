"""Explicit local gate for the sole production runtime-image path.

The gate never loads a serialized runtime closure and never accepts a caller
Docker binary, host, context, or builder. It runs only when explicitly enabled
with two already-reviewed offline package-manager artifacts and the audited
OpenClaw Git repository. All base images must already be present locally.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile

import pytest

from release_tools.image_production_v2 import (
    LocalGitObjectArchiveExporter,
    OfflineBuildkitOciBuilder,
    OfflineContainerImageProbe,
    TrustedImageProducerV2,
    TrustedRuntimeBuildClosureFactoryV2,
    _regular_bytes,
    open_reviewed_local_execution,
)
from release_tools.image_publication import (
    CAPABILITY_TOOL_NAMES,
    MAX_BLOB_BYTES,
    OPENCLAW_RUNTIME_COMMIT,
    OPENCLAW_RUNTIME_TREE,
    _compile_capability_catalog,
)


ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "123456789012"
REGION = "eu-west-1"
CREATED = "2026-07-20T00:00:00Z"


def test_runtime_image_gate_has_no_serialized_closure_or_caller_engine_bypass() -> None:
    source = Path(__file__).read_text(encoding="utf-8")

    assert "RuntimeBuild" + "Closure(" not in source
    assert "PERSONAL_OPERATOR_RUNTIME_BUILD_" + "CLOSURE_DIR" not in source
    assert "PERSONAL_OPERATOR_DOCKER_" + "BIN" not in source
    assert "TrustedRuntimeBuildClosureFactoryV2" in source
    assert "OfflineBuildkitOciBuilder" in source
    assert "OfflineContainerImageProbe" in source


def test_runtime_image_gate_reads_retained_probe_payload_not_plan_descriptor() -> None:
    source = Path(__file__).read_text(encoding="utf-8")

    assert 'bundle.probe_evidence["fresh-1"]' in source
    assert "bundle.plan.probe_" + 'evidence["startupStatus"]' not in source


def _git_value(execution, repository: Path, expression: str) -> str:
    completed = execution.run_git(
        ["-C", str(repository), "rev-parse", "--verify", expression]
    )
    assert completed.returncode == 0, completed.stderr.decode(
        "utf-8", errors="replace"
    )
    value = completed.stdout.decode("ascii").strip()
    assert re.fullmatch(r"[0-9a-f]{40}", value)
    return value


def _catalog_identity(
    archive: bytes, *, release_commit: str
) -> tuple[str, str]:
    files: dict[str, tuple[bytes, int]] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        for member in source.getmembers():
            path = PurePosixPath(member.name)
            assert (
                not path.is_absolute()
                and path.as_posix() == member.name
                and ".." not in path.parts
            )
            if member.isdir():
                continue
            assert member.isreg(), member.name
            if not member.name.startswith("bridge/capabilities/"):
                continue
            reader = source.extractfile(member)
            assert reader is not None
            payload = reader.read()
            assert len(payload) == member.size
            files[member.name] = (payload, member.mode & 0o777)
    source_sha256, catalog_sha256, tools = _compile_capability_catalog(
        files,
        release_commit=release_commit,
    )
    assert tools == CAPABILITY_TOOL_NAMES
    assert source_sha256
    return source_sha256, catalog_sha256


def _required_path(name: str) -> Path:
    value = os.environ.get(name, "")
    assert value, f"{name} is required"
    return Path(value).resolve()


def test_real_runtime_image_builds_twice_offline_and_passes_runtime_probe() -> None:
    if os.environ.get("PERSONAL_OPERATOR_RUN_RUNTIME_IMAGE_INTEGRATION") != "1":
        pytest.skip(
            "set PERSONAL_OPERATOR_RUN_RUNTIME_IMAGE_INTEGRATION=1 with "
            "reviewed offline artifacts and the audited OpenClaw repository"
        )

    openclaw_repository = _required_path(
        "PERSONAL_OPERATOR_OPENCLAW_REPOSITORY"
    )
    openclaw_artifact_path = _required_path(
        "PERSONAL_OPERATOR_OPENCLAW_PACKAGE_MANAGER_ARTIFACT"
    )
    bridge_artifact_path = _required_path(
        "PERSONAL_OPERATOR_BRIDGE_PACKAGE_MANAGER_ARTIFACT"
    )

    with open_reviewed_local_execution() as execution:
        status = execution.run_git(
            [
                "-C",
                str(ROOT),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ]
        )
        assert status.returncode == 0
        assert status.stdout == b"", (
            "real image evidence requires one exact clean commit"
        )
        release_commit = _git_value(execution, ROOT, "HEAD^{commit}")
        release_tree = _git_value(execution, ROOT, "HEAD^{tree}")
        assert _git_value(
            execution,
            openclaw_repository,
            f"{OPENCLAW_RUNTIME_COMMIT}^{{commit}}",
        ) == OPENCLAW_RUNTIME_COMMIT
        assert _git_value(
            execution,
            openclaw_repository,
            f"{OPENCLAW_RUNTIME_COMMIT}^{{tree}}",
        ) == OPENCLAW_RUNTIME_TREE

        exporter = LocalGitObjectArchiveExporter(
            ROOT,
            execution=execution,
        )
        bridge_archive = exporter.export_archive(
            source_commit=release_commit,
            source_tree=release_tree,
            path="bridge",
        )
        _, catalog_sha256 = _catalog_identity(
            bridge_archive,
            release_commit=release_commit,
        )
        trusted_closure = TrustedRuntimeBuildClosureFactoryV2(
            execution=execution,
            release_repository=ROOT,
            openclaw_repository=openclaw_repository,
        ).build(
            release_commit=release_commit,
            release_tree=release_tree,
            openclaw_commit=OPENCLAW_RUNTIME_COMMIT,
            openclaw_tree=OPENCLAW_RUNTIME_TREE,
            openclaw_package_manager_artifact=_regular_bytes(
                openclaw_artifact_path,
                maximum=MAX_BLOB_BYTES,
                label="OpenClaw package-manager artifact",
            ),
            bridge_package_manager_artifact=_regular_bytes(
                bridge_artifact_path,
                maximum=MAX_BLOB_BYTES,
                label="bridge package-manager artifact",
            ),
        )
        builder = OfflineBuildkitOciBuilder(execution=execution)
        probe = OfflineContainerImageProbe(
            execution=execution,
            builder=builder,
        )
        bundle = TrustedImageProducerV2(
            git_archive=exporter,
            builder=builder,
            probe=probe,
        ).prepare(
            source_commit=release_commit,
            source_tree=release_tree,
            account=ACCOUNT,
            region=REGION,
            created=CREATED,
            trusted_runtime_build_closure=trusted_closure,
            expected_capability_catalog_digest=catalog_sha256,
        )

    bundle.validate(expected_plan_sha256=bundle.plan_sha256)
    probe_evidence = json.loads(bundle.probe_evidence["fresh-1"])
    assert bundle.plan.source_commit == release_commit
    assert bundle.plan.source_tree == release_tree
    assert bundle.plan.account == ACCOUNT
    assert bundle.plan.region == REGION
    assert bundle.plan.capability_catalog_digest == catalog_sha256
    assert probe_evidence["startupStatus"] == "HEALTHY"
    assert probe_evidence["credentialsAbsent"] is True
    assert probe_evidence["networkDenied"] is True
    assert probe_evidence["browserArtifactsAbsent"] is True
    assert probe_evidence["modelCallableTools"] == list(
        CAPABILITY_TOOL_NAMES
    )
