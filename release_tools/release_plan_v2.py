"""Pre-cloud, stage-aware assembly for the Personal Operator release v2.

This module deliberately owns no subprocess, network, SDK, credential, or
mutation authority.  Its CloudAssembly reader pins one caller-selected,
owner-controlled directory descriptor and reads every trust-bearing file
through that descriptor without following links.  The later plan assembler
consumes only the retained byte values returned here.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, Sequence
import zipfile

from release_tools.agentcore_hardening_v2 import (
    AgentCoreHardeningError,
    AgentCoreHardeningOperationV1,
)
from release_tools.asset_publication_v2 import (
    ASSET_ARTIFACT_HEADER_BYTES,
    ASSET_ARTIFACT_MAGIC,
    MAX_ASSET_HEADER_BYTES,
    AssetPublicationError,
    AssetPublicationV2,
)
from release_tools.baseline_observer_v2 import (
    BaselineObservationRequestV1,
    BaselineObserverV2Error,
)
from release_tools.cloudformation_v2 import (
    BOOTSTRAP_STACK,
    FOUNDATION_STACKS,
    CloudFormationMutationError,
    CloudFormationOperationV2,
)
from release_tools.contracts import (
    MAX_CONTRACT_BYTES,
    ContractError,
    ReleasePlanV2,
    canonical_json_bytes,
    parse_canonical_object,
)
from release_tools.image_publication import (
    IMAGE_EFFECT_MAGIC,
    OCI_EMPTY_CONFIG_MEDIA_TYPE,
    PROVENANCE_ARTIFACT_TYPE,
    REPOSITORY_NAME,
    SBOM_ARTIFACT_TYPE,
    ArtifactSubstitutionError,
    ImagePublicationEffectV1,
    ImagePublicationError,
    ImagePublicationPlanV1,
)
from release_tools.stack_drift_v2 import StackDriftError, StackDriftOperationV1


REQUIRED_REGION = "eu-west-1"
ASSEMBLY_STAGES = ("foundation", "runtime", "endpoint", "consumer")
STACKS = (
    "OpenClawVpc",
    "OpenClawSecurity",
    "OpenClawGuardrails",
    "PersonalOperatorCapabilities",
    "OpenClawAgentCore",
    "OpenClawObservability",
    "OpenClawRouter",
    "OpenClawCron",
    "PersonalOperatorScheduler",
    "PersonalOperatorWeb",
)
CONSUMER_STACKS = (
    "OpenClawRouter",
    "OpenClawCron",
    "PersonalOperatorScheduler",
    "PersonalOperatorWeb",
)
STACK_DEPENDENCIES: Mapping[str, tuple[str, ...]] = {
    "OpenClawVpc": (),
    "OpenClawSecurity": (),
    "OpenClawGuardrails": ("OpenClawSecurity",),
    "PersonalOperatorCapabilities": ("OpenClawSecurity",),
    "OpenClawAgentCore": (
        "PersonalOperatorCapabilities",
        "OpenClawVpc",
        "OpenClawGuardrails",
        "OpenClawSecurity",
    ),
    "OpenClawObservability": ("OpenClawSecurity",),
    "OpenClawRouter": ("OpenClawSecurity", "OpenClawAgentCore"),
    "OpenClawCron": (),
    "PersonalOperatorScheduler": ("OpenClawRouter", "OpenClawSecurity"),
    "PersonalOperatorWeb": (
        "PersonalOperatorScheduler",
        "OpenClawSecurity",
        "PersonalOperatorCapabilities",
        "OpenClawRouter",
        "OpenClawAgentCore",
    ),
}

_ACCOUNT = re.compile(r"[0-9]{12}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"[1-9][0-9]*\.[0-9]+\.[0-9]+")
_DESTINATION = re.compile(r"([0-9]{12})-eu-west-1-[0-9a-f]{8}")
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_TEMPLATE_BYTES = 32 * 1024 * 1024
_MAX_FILE_ASSET_BYTES = 300 * 1024 * 1024
_MAX_ZIP_ASSET_ENTRIES = 100_000
_MAX_ZIP_ASSET_DEPTH = 64
_MAX_ZIP_MEMBER_PATH_BYTES = 1_024
_BOOTSTRAP_PARAMETER = "/cdk-bootstrap/hnb659fds/version"
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


class ReleasePlanAssemblyError(RuntimeError):
    """Pre-cloud inputs do not prove one exact immutable release."""


def _stability_hook(_stage: str, _name: str) -> None:
    """Test-only race injection point; production deliberately does nothing."""


def _exact_account(value: object) -> str:
    if (
        not isinstance(value, str)
        or _ACCOUNT.fullmatch(value) is None
        or value == "000000000000"
    ):
        raise ReleasePlanAssemblyError("release account is invalid")
    return value


def _exact_region(value: object) -> str:
    if value != REQUIRED_REGION:
        raise ReleasePlanAssemblyError(
            f"release region must be exactly {REQUIRED_REGION}"
        )
    return REQUIRED_REGION


def _direct_name(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ReleasePlanAssemblyError(f"{label} path is unsafe")
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or parsed.as_posix() != value
        or len(parsed.parts) != 1
        or value in {".", ".."}
    ):
        raise ReleasePlanAssemblyError(f"{label} path is unsafe")
    return value


def _safe_source_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ReleasePlanAssemblyError(f"{label} path is unsafe")
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or parsed.as_posix() != value
        or ".." in parsed.parts
        or value == "."
    ):
        raise ReleasePlanAssemblyError(f"{label} path is unsafe")
    return value


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleasePlanAssemblyError(
                "CloudAssembly JSON contains a duplicate key"
            )
        result[key] = value
    return result


def _json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise ReleasePlanAssemblyError(
            f"{label} is not strict UTF-8"
        ) from error

    def finite_float(raw: str) -> float:
        value = float(raw)
        if not math.isfinite(value):
            raise ReleasePlanAssemblyError(
                f"{label} contains a non-finite JSON number"
            )
        return value

    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_float=finite_float,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ReleasePlanAssemblyError(
                    f"{label} contains a non-finite JSON number"
                )
            ),
        )
    except ReleasePlanAssemblyError:
        raise
    except RecursionError as error:
        raise ReleasePlanAssemblyError(
            f"{label} JSON nesting exceeds its limit"
        ) from error
    except (TypeError, ValueError) as error:
        raise ReleasePlanAssemblyError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ReleasePlanAssemblyError(f"{label} is not a JSON object")
    return value


def _open_pinned_directory(root: Path) -> int:
    try:
        descriptor = os.open(os.fspath(root), _DIRECTORY_FLAGS)
    except OSError as error:
        raise ReleasePlanAssemblyError(
            "CloudAssembly root is not a safe directory"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ReleasePlanAssemblyError(
                "CloudAssembly root is not an owner-controlled directory"
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read_regular_at(
    root_descriptor: int,
    name: str,
    *,
    label: str,
    maximum: int,
    minimum: int = 1,
) -> bytes:
    name = _direct_name(name, label=label)
    try:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=root_descriptor)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENXIO, errno.EACCES}:
            detail = "unsafe link or non-regular file"
        else:
            detail = "unavailable file"
        raise ReleasePlanAssemblyError(f"{label} is an {detail}") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or not minimum <= before.st_size <= maximum
        ):
            raise ReleasePlanAssemblyError(
                f"{label} is not an exact owner-controlled regular file"
            )

        def read_once() -> bytes:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise ReleasePlanAssemblyError(f"{label} exceeds its size limit")
                chunks.append(chunk)
            return b"".join(chunks)

        first = read_once()
        _stability_hook("after-first-read", name)
        os.lseek(descriptor, 0, os.SEEK_SET)
        second = read_once()
        after = os.fstat(descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if first != second or any(
            getattr(before, field) != getattr(after, field)
            for field in stable_fields
        ):
            raise ReleasePlanAssemblyError(f"{label} is unstable")
        if len(first) != before.st_size:
            raise ReleasePlanAssemblyError(f"{label} changed while being read")
        return first
    finally:
        os.close(descriptor)


def _open_owner_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    label: str,
) -> int:
    name = _direct_name(name, label=label)
    try:
        descriptor = os.open(
            name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor
        )
    except OSError as error:
        raise ReleasePlanAssemblyError(
            f"{label} is an unsafe or unavailable directory"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ReleasePlanAssemblyError(
                f"{label} is not an owner-controlled directory"
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_source_directory_at(
    root_descriptor: int,
    source_path: str,
) -> int:
    current = os.dup(root_descriptor)
    try:
        for component in PurePosixPath(source_path).parts:
            child = _open_owner_directory_at(
                current,
                component,
                label="CDK ZIP asset source",
            )
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


@dataclass(frozen=True, slots=True)
class _ZipTreeEvidenceV2:
    directories: tuple[
        tuple[str, tuple[int, ...], tuple[str, ...]], ...
    ]
    files: tuple[tuple[str, tuple[int, ...], int, str], ...]
    node_count: int
    total_size: int


def _retained_zip_bytes_at(
    root_descriptor: int,
    source_path: str,
) -> bytes:
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_gid",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )

    def signature(metadata: os.stat_result) -> tuple[int, ...]:
        return tuple(getattr(metadata, field) for field in stable_fields)

    def scan_tree(
        *, retain_payloads: bool
    ) -> tuple[_ZipTreeEvidenceV2, tuple[tuple[str, bytes], ...]]:
        source_descriptor = _open_source_directory_at(
            root_descriptor, source_path
        )
        directories: list[
            tuple[str, tuple[int, ...], tuple[str, ...]]
        ] = []
        files: list[tuple[str, tuple[int, ...], int, str]] = []
        payloads: list[tuple[str, bytes]] = []
        total_size = 0
        node_count = 1
        if node_count > _MAX_ZIP_ASSET_ENTRIES:
            os.close(source_descriptor)
            raise ReleasePlanAssemblyError(
                "CDK ZIP asset source exceeds its retained limit"
            )

        def names_at(
            directory_descriptor: int,
            *,
            count_nodes: bool,
        ) -> tuple[str, ...]:
            nonlocal node_count
            names: list[str] = []
            try:
                with os.scandir(directory_descriptor) as iterator:
                    for entry in iterator:
                        if count_nodes:
                            node_count += 1
                            if node_count > _MAX_ZIP_ASSET_ENTRIES:
                                raise ReleasePlanAssemblyError(
                                    "CDK ZIP asset source exceeds its retained limit"
                                )
                        elif len(names) >= _MAX_ZIP_ASSET_ENTRIES:
                            raise ReleasePlanAssemblyError(
                                "CDK ZIP asset source exceeds its retained limit"
                            )
                        names.append(
                            _direct_name(
                                entry.name, label="CDK ZIP asset member"
                            )
                        )
            except ReleasePlanAssemblyError:
                raise
            except OSError as error:
                raise ReleasePlanAssemblyError(
                    "CDK ZIP asset source cannot be enumerated"
                ) from error
            names.sort()
            return tuple(names)

        def visit(
            directory_descriptor: int,
            *,
            prefix: str,
            depth: int,
        ) -> None:
            nonlocal total_size
            if depth > _MAX_ZIP_ASSET_DEPTH:
                raise ReleasePlanAssemblyError(
                    "CDK ZIP asset source exceeds its depth limit"
                )
            before = os.fstat(directory_descriptor)
            first_names = names_at(
                directory_descriptor, count_nodes=True
            )
            for name in first_names:
                relative = f"{prefix}{name}"
                try:
                    encoded_relative = relative.encode(
                        "utf-8", errors="strict"
                    )
                except UnicodeError as error:
                    raise ReleasePlanAssemblyError(
                        "CDK ZIP asset member path is not strict UTF-8"
                    ) from error
                if len(encoded_relative) > _MAX_ZIP_MEMBER_PATH_BYTES:
                    raise ReleasePlanAssemblyError(
                        "CDK ZIP asset member path exceeds its limit"
                    )
                try:
                    metadata = os.stat(
                        name,
                        dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    )
                except OSError as error:
                    raise ReleasePlanAssemblyError(
                        "CDK ZIP asset member is unavailable"
                    ) from error
                if stat.S_ISDIR(metadata.st_mode):
                    child = _open_owner_directory_at(
                        directory_descriptor,
                        name,
                        label="CDK ZIP asset source",
                    )
                    try:
                        if signature(metadata) != signature(os.fstat(child)):
                            raise ReleasePlanAssemblyError(
                                "CDK ZIP asset source is unstable"
                            )
                        visit(
                            child,
                            prefix=f"{relative}/",
                            depth=depth + 1,
                        )
                    finally:
                        os.close(child)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise ReleasePlanAssemblyError(
                        "CDK ZIP asset source contains a non-regular member"
                    )
                remaining = _MAX_FILE_ASSET_BYTES - total_size
                payload = _read_regular_at(
                    directory_descriptor,
                    name,
                    label="CDK ZIP asset member",
                    maximum=remaining,
                    minimum=0,
                )
                try:
                    after_file = os.stat(
                        name,
                        dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    )
                except OSError as error:
                    raise ReleasePlanAssemblyError(
                        "CDK ZIP asset member is unavailable"
                    ) from error
                if signature(metadata) != signature(after_file):
                    raise ReleasePlanAssemblyError(
                        "CDK ZIP asset source is unstable"
                    )
                total_size += len(payload)
                files.append(
                    (
                        relative,
                        signature(after_file),
                        len(payload),
                        hashlib.sha256(payload).hexdigest(),
                    )
                )
                if retain_payloads:
                    payloads.append((relative, payload))
            second_names = names_at(
                directory_descriptor, count_nodes=False
            )
            after = os.fstat(directory_descriptor)
            if (
                first_names != second_names
                or signature(before) != signature(after)
            ):
                raise ReleasePlanAssemblyError(
                    "CDK ZIP asset source is unstable"
                )
            directories.append(
                (prefix.removesuffix("/"), signature(after), second_names)
            )

        try:
            visit(source_descriptor, prefix="", depth=0)
        finally:
            os.close(source_descriptor)
        return (
            _ZipTreeEvidenceV2(
                tuple(directories),
                tuple(files),
                node_count,
                total_size,
            ),
            tuple(payloads),
        )

    first_evidence, entries = scan_tree(retain_payloads=True)
    if not entries:
        raise ReleasePlanAssemblyError(
            "CDK ZIP asset source has no retained regular files"
        )
    second_evidence, second_payloads = scan_tree(retain_payloads=False)
    if second_payloads or first_evidence != second_evidence:
        raise ReleasePlanAssemblyError(
            "CDK ZIP asset source is unstable across tree verification"
        )
    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_STORED,
        strict_timestamps=True,
    ) as archive:
        for relative, payload in entries:
            member = zipfile.ZipInfo(
                relative,
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            member.create_system = 3
            member.compress_type = zipfile.ZIP_STORED
            member.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(member, payload)
    retained = output.getvalue()
    if not 1 <= len(retained) <= _MAX_FILE_ASSET_BYTES:
        raise ReleasePlanAssemblyError(
            "CDK ZIP asset retained archive exceeds its size limit"
        )
    return retained


@dataclass(frozen=True, slots=True)
class CloudAssemblyTemplateV2:
    stack_name: str
    template_file: str
    template_bytes: bytes
    template_asset_id: str
    dependencies: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CloudAssemblyAssetV2:
    asset_id: str
    source_path: str
    packaging: str
    bucket_name: str
    object_key: str
    region: str
    destination_id: str
    assume_role_arn: str
    source_bytes: bytes


@dataclass(frozen=True, slots=True)
class CloudAssemblyStageV2:
    stage: str
    account: str
    region: str
    manifest_bytes: bytes
    templates: tuple[CloudAssemblyTemplateV2, ...]
    assets: tuple[CloudAssemblyAssetV2, ...]

    def template(self, stack_name: str) -> bytes:
        for template in self.templates:
            if template.stack_name == stack_name:
                return template.template_bytes
        raise ReleasePlanAssemblyError("CloudAssembly stack template is absent")


class TrustedCloudAssemblyReaderV2:
    """Strict descriptor-relative reader for one synthesized CDK stage."""

    _AUXILIARY_ARTIFACTS: ClassVar[Mapping[str, str]] = {
        "Tree": "cdk:tree",
        "aws-cdk-lib/feature-flag-report": "cdk:feature-flag-report",
    }

    @classmethod
    def read(
        cls,
        root: str | Path,
        *,
        stage: str,
        account: str,
        region: str,
    ) -> CloudAssemblyStageV2:
        if stage not in ASSEMBLY_STAGES:
            raise ReleasePlanAssemblyError("CloudAssembly stage is unknown")
        account = _exact_account(account)
        region = _exact_region(region)
        root_descriptor = _open_pinned_directory(Path(root))
        try:
            manifest_bytes = _read_regular_at(
                root_descriptor,
                "manifest.json",
                label="CloudAssembly manifest",
                maximum=_MAX_MANIFEST_BYTES,
            )
            manifest = _json_object(
                manifest_bytes, label="CloudAssembly manifest"
            )
            cls._validate_manifest_header(manifest)
            raw_artifacts = manifest["artifacts"]
            if not isinstance(raw_artifacts, dict):
                raise ReleasePlanAssemblyError(
                    "CloudAssembly artifact inventory is invalid"
                )
            expected_ids = set(STACKS) | {f"{name}.assets" for name in STACKS}
            extras = set(raw_artifacts) - expected_ids
            for artifact_id in extras:
                expected_type = cls._AUXILIARY_ARTIFACTS.get(artifact_id)
                artifact = raw_artifacts[artifact_id]
                actual_type = (
                    artifact.get("type") if isinstance(artifact, dict) else None
                )
                if expected_type is None or actual_type != expected_type:
                    raise ReleasePlanAssemblyError(
                        "CloudAssembly contains an unknown artifact type"
                    )
            if set(raw_artifacts) - extras != expected_ids:
                raise ReleasePlanAssemblyError(
                    "CloudAssembly stack and asset inventory is not exact"
                )

            templates: list[CloudAssemblyTemplateV2] = []
            all_assets: dict[str, CloudAssemblyAssetV2] = {}
            parsed_asset_manifests: dict[
                str, tuple[CloudAssemblyAssetV2, ...]
            ] = {}
            for stack_name in STACKS:
                asset_artifact_id = f"{stack_name}.assets"
                asset_file = cls._asset_manifest_file(
                    raw_artifacts[asset_artifact_id], artifact_id=asset_artifact_id
                )
                asset_manifest_bytes = _read_regular_at(
                    root_descriptor,
                    asset_file,
                    label=f"{stack_name} asset manifest",
                    maximum=_MAX_MANIFEST_BYTES,
                )
                parsed_assets = cls._parse_asset_manifest(
                    _json_object(
                        asset_manifest_bytes,
                        label=f"{stack_name} asset manifest",
                    ),
                    root_descriptor=root_descriptor,
                    account=account,
                    region=region,
                )
                parsed_asset_manifests[stack_name] = parsed_assets
                for asset in parsed_assets:
                    previous = all_assets.get(asset.asset_id)
                    if previous is not None and previous != asset:
                        raise ReleasePlanAssemblyError(
                            "CloudAssembly duplicate asset ID has conflicting content "
                            "or destination"
                        )
                    all_assets[asset.asset_id] = asset

                stack_artifact = cls._stack_artifact(
                    raw_artifacts[stack_name],
                    stack_name=stack_name,
                    account=account,
                    region=region,
                )
                template_file = stack_artifact["template_file"]
                template_bytes = _read_regular_at(
                    root_descriptor,
                    template_file,
                    label=f"{stack_name} template",
                    maximum=_MAX_TEMPLATE_BYTES,
                )
                template = _json_object(
                    template_bytes, label=f"{stack_name} template"
                )
                cls._validate_template_object(template, stack_name=stack_name)
                template_assets = [
                    item
                    for item in parsed_assets
                    if item.source_path == template_file
                    and item.packaging == "file"
                    and item.object_key == f"{item.asset_id}.json"
                ]
                if (
                    len(template_assets) != 1
                    or template_assets[0].source_bytes != template_bytes
                    or stack_artifact["template_asset_id"]
                    != template_assets[0].asset_id
                ):
                    raise ReleasePlanAssemblyError(
                        "CloudAssembly templateFile and asset-manifest bindings differ"
                    )
                templates.append(
                    CloudAssemblyTemplateV2(
                        stack_name=stack_name,
                        template_file=template_file,
                        template_bytes=template_bytes,
                        template_asset_id=template_assets[0].asset_id,
                        dependencies=STACK_DEPENDENCIES[stack_name],
                    )
                )
            cls._validate_stage_semantics(stage, templates)
            return CloudAssemblyStageV2(
                stage=stage,
                account=account,
                region=region,
                manifest_bytes=manifest_bytes,
                templates=tuple(templates),
                assets=tuple(all_assets[key] for key in sorted(all_assets)),
            )
        finally:
            os.close(root_descriptor)

    @staticmethod
    def _validate_manifest_header(manifest: Mapping[str, Any]) -> None:
        if set(manifest) != {
            "version",
            "artifacts",
            "missing",
            "minimumCliVersion",
        }:
            raise ReleasePlanAssemblyError(
                "CloudAssembly manifest fields are not exact"
            )
        if (
            not isinstance(manifest["version"], str)
            or _VERSION.fullmatch(manifest["version"]) is None
            or not isinstance(manifest["minimumCliVersion"], str)
            or _VERSION.fullmatch(manifest["minimumCliVersion"]) is None
            or manifest["missing"] != []
        ):
            raise ReleasePlanAssemblyError(
                "CloudAssembly manifest version or missing context is invalid"
            )

    @staticmethod
    def _asset_manifest_file(
        raw: object, *, artifact_id: str
    ) -> str:
        if not isinstance(raw, dict) or set(raw) != {"type", "properties"}:
            raise ReleasePlanAssemblyError(
                "CloudAssembly asset artifact fields are not exact"
            )
        if raw["type"] != "cdk:asset-manifest":
            raise ReleasePlanAssemblyError(
                "CloudAssembly asset artifact type is invalid"
            )
        properties = raw["properties"]
        if not isinstance(properties, dict) or set(properties) != {
            "file",
            "requiresBootstrapStackVersion",
            "bootstrapStackVersionSsmParameter",
        }:
            raise ReleasePlanAssemblyError(
                "CloudAssembly asset manifest properties are not exact"
            )
        expected_file = artifact_id.removesuffix(".assets") + ".assets.json"
        if (
            properties["file"] != expected_file
            or properties["requiresBootstrapStackVersion"] != 6
            or properties["bootstrapStackVersionSsmParameter"]
            != _BOOTSTRAP_PARAMETER
        ):
            raise ReleasePlanAssemblyError(
                "CloudAssembly asset manifest binding is invalid"
            )
        return _direct_name(properties["file"], label="asset manifest")

    @staticmethod
    def _parse_asset_manifest(
        manifest: Mapping[str, Any],
        *,
        root_descriptor: int,
        account: str,
        region: str,
    ) -> tuple[CloudAssemblyAssetV2, ...]:
        if set(manifest) != {"version", "files", "dockerImages"}:
            raise ReleasePlanAssemblyError(
                "CDK asset manifest fields are not exact"
            )
        if (
            not isinstance(manifest["version"], str)
            or _VERSION.fullmatch(manifest["version"]) is None
            or manifest["dockerImages"] != {}
            or not isinstance(manifest["files"], dict)
            or not manifest["files"]
        ):
            raise ReleasePlanAssemblyError(
                "CDK asset manifest is invalid or contains Docker authority"
            )
        bucket = f"cdk-hnb659fds-assets-{account}-{region}"
        role = (
            "arn:${AWS::Partition}:iam::"
            f"{account}:role/cdk-hnb659fds-file-publishing-role-{account}-{region}"
        )
        result: list[CloudAssemblyAssetV2] = []
        for asset_id in sorted(manifest["files"]):
            raw = manifest["files"][asset_id]
            if _SHA256.fullmatch(asset_id) is None:
                raise ReleasePlanAssemblyError("CDK asset ID is invalid")
            if not isinstance(raw, dict) or set(raw) != {
                "displayName",
                "source",
                "destinations",
            }:
                raise ReleasePlanAssemblyError(
                    "CDK asset fields are not exact"
                )
            if not isinstance(raw["displayName"], str) or not raw["displayName"]:
                raise ReleasePlanAssemblyError("CDK asset display name is invalid")
            source = raw["source"]
            if not isinstance(source, dict) or set(source) != {
                "path",
                "packaging",
            }:
                raise ReleasePlanAssemblyError("CDK asset source is invalid")
            source_path = _safe_source_path(
                source["path"], label="CDK asset source"
            )
            packaging = source["packaging"]
            if packaging not in {"file", "zip"}:
                raise ReleasePlanAssemblyError(
                    "CDK asset packaging is not closed"
                )
            destinations = raw["destinations"]
            if not isinstance(destinations, dict) or len(destinations) != 1:
                raise ReleasePlanAssemblyError(
                    "CDK asset destination inventory is not exact"
                )
            destination_id, destination = next(iter(destinations.items()))
            if (
                not isinstance(destination_id, str)
                or _DESTINATION.fullmatch(destination_id) is None
                or not destination_id.startswith(f"{account}-{region}-")
                or not isinstance(destination, dict)
                or set(destination)
                != {"bucketName", "objectKey", "region", "assumeRoleArn"}
            ):
                raise ReleasePlanAssemblyError(
                    "CDK asset destination identity is invalid"
                )
            expected_extension = "json" if packaging == "file" else "zip"
            object_key = destination["objectKey"]
            if (
                destination["bucketName"] != bucket
                or destination["region"] != region
                or destination["assumeRoleArn"] != role
                or object_key != f"{asset_id}.{expected_extension}"
            ):
                raise ReleasePlanAssemblyError(
                    "CDK asset destination crosses its account, region, or ID"
                )
            if packaging == "file":
                source_bytes = _read_regular_at(
                    root_descriptor,
                    _direct_name(source_path, label="CDK file asset source"),
                    label="CDK file asset source",
                    maximum=_MAX_FILE_ASSET_BYTES,
                )
            else:
                source_bytes = _retained_zip_bytes_at(
                    root_descriptor, source_path
                )
            result.append(
                CloudAssemblyAssetV2(
                    asset_id=asset_id,
                    source_path=source_path,
                    packaging=packaging,
                    bucket_name=bucket,
                    object_key=object_key,
                    region=region,
                    destination_id=destination_id,
                    assume_role_arn=role,
                    source_bytes=source_bytes,
                )
            )
        return tuple(result)

    @staticmethod
    def _stack_artifact(
        raw: object,
        *,
        stack_name: str,
        account: str,
        region: str,
    ) -> dict[str, str]:
        if not isinstance(raw, dict) or set(raw) != {
            "type",
            "environment",
            "properties",
            "dependencies",
            "additionalMetadataFile",
            "displayName",
        }:
            raise ReleasePlanAssemblyError(
                "CloudAssembly stack artifact fields are not exact"
            )
        if (
            raw["type"] != "aws:cloudformation:stack"
            or raw["environment"] != f"aws://{account}/{region}"
            or raw["displayName"] != stack_name
            or raw["additionalMetadataFile"] != f"{stack_name}.metadata.json"
        ):
            raise ReleasePlanAssemblyError(
                "CloudAssembly stack environment or identity crosses its account"
            )
        asset_artifact = f"{stack_name}.assets"
        dependencies = raw["dependencies"]
        if (
            not isinstance(dependencies, list)
            or len(dependencies) != len(set(dependencies))
            or set(dependencies)
            != {*STACK_DEPENDENCIES[stack_name], asset_artifact}
        ):
            raise ReleasePlanAssemblyError(
                "CloudAssembly stack dependency topology is not exact"
            )
        properties = raw["properties"]
        if not isinstance(properties, dict) or set(properties) != {
            "templateFile",
            "terminationProtection",
            "validateOnSynth",
            "assumeRoleArn",
            "cloudFormationExecutionRoleArn",
            "stackTemplateAssetObjectUrl",
            "requiresBootstrapStackVersion",
            "bootstrapStackVersionSsmParameter",
            "additionalDependencies",
            "lookupRole",
        }:
            raise ReleasePlanAssemblyError(
                "CloudAssembly stack properties are not exact"
            )
        template_file = _direct_name(
            properties["templateFile"], label="CloudAssembly template"
        )
        if template_file != f"{stack_name}.template.json":
            raise ReleasePlanAssemblyError(
                "CloudAssembly templateFile binding is invalid"
            )
        prefix = f"s3://cdk-hnb659fds-assets-{account}-{region}/"
        template_url = properties["stackTemplateAssetObjectUrl"]
        if (
            not isinstance(template_url, str)
            or not template_url.startswith(prefix)
            or not template_url.endswith(".json")
            or _SHA256.fullmatch(
                template_url.removeprefix(prefix).removesuffix(".json")
            )
            is None
        ):
            raise ReleasePlanAssemblyError(
                "CloudAssembly template asset URL is invalid"
            )
        template_asset_id = template_url.removeprefix(prefix).removesuffix(
            ".json"
        )
        deploy_role = (
            "arn:${AWS::Partition}:iam::"
            f"{account}:role/cdk-hnb659fds-deploy-role-{account}-{region}"
        )
        cfn_role = (
            "arn:${AWS::Partition}:iam::"
            f"{account}:role/cdk-hnb659fds-cfn-exec-role-{account}-{region}"
        )
        lookup_role = properties["lookupRole"]
        expected_lookup_role = {
            "arn": (
                "arn:${AWS::Partition}:iam::"
                f"{account}:role/cdk-hnb659fds-lookup-role-{account}-{region}"
            ),
            "requiresBootstrapStackVersion": 8,
            "bootstrapStackVersionSsmParameter": _BOOTSTRAP_PARAMETER,
        }
        if (
            properties["terminationProtection"] is not False
            or properties["validateOnSynth"] is not False
            or properties["assumeRoleArn"] != deploy_role
            or properties["cloudFormationExecutionRoleArn"] != cfn_role
            or properties["requiresBootstrapStackVersion"] != 6
            or properties["bootstrapStackVersionSsmParameter"]
            != _BOOTSTRAP_PARAMETER
            or properties["additionalDependencies"] != [asset_artifact]
            or lookup_role != expected_lookup_role
        ):
            raise ReleasePlanAssemblyError(
                "CloudAssembly stack deployment properties are not exact"
            )
        return {
            "template_file": template_file,
            "template_asset_id": template_asset_id,
        }

    @staticmethod
    def _validate_template_object(
        template: Mapping[str, Any], *, stack_name: str
    ) -> None:
        resources = template.get("Resources")
        if not isinstance(resources, dict):
            raise ReleasePlanAssemblyError(
                f"{stack_name} template resource inventory is invalid"
            )
        for resource in resources.values():
            if (
                not isinstance(resource, dict)
                or not isinstance(resource.get("Type"), str)
            ):
                raise ReleasePlanAssemblyError(
                    f"{stack_name} template contains an invalid resource"
                )

    @staticmethod
    def _validate_stage_semantics(
        stage: str, templates: list[CloudAssemblyTemplateV2]
    ) -> None:
        agentcore_types = {
            "AWS::BedrockAgentCore::Runtime",
            "AWS::BedrockAgentCore::RuntimeEndpoint",
        }
        runtime_count = 0
        endpoint_count = 0
        for retained in templates:
            template = _json_object(
                retained.template_bytes,
                label=f"{retained.stack_name} template",
            )
            resource_types = [
                item["Type"]
                for item in template.get("Resources", {}).values()
            ]
            present = agentcore_types.intersection(resource_types)
            if present and retained.stack_name != "OpenClawAgentCore":
                raise ReleasePlanAssemblyError(
                    "CloudAssembly stage semantics are not exact"
                )
            if retained.stack_name == "OpenClawAgentCore":
                runtime_count = resource_types.count(
                    "AWS::BedrockAgentCore::Runtime"
                )
                endpoint_count = resource_types.count(
                    "AWS::BedrockAgentCore::RuntimeEndpoint"
                )
        expected = {
            "foundation": (0, 0),
            "runtime": (1, 0),
            "endpoint": (1, 1),
            "consumer": (1, 1),
        }[stage]
        if (runtime_count, endpoint_count) != expected:
            raise ReleasePlanAssemblyError(
                "CloudAssembly stage semantics are not exact"
            )


_COMMIT = re.compile(r"[0-9a-f]{40}")
_PLAN_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,511}")
_STATIC_KINDS = frozenset({"RUNTIME_CONTEXT_WRITE", "VERIFY"})
_MUTATION_KINDS = frozenset(
    {
        "BOOTSTRAP_STACK",
        "ASSET_PUBLISH",
        "AGENTCORE_HARDEN",
        "STACK_CREATE",
        "STACK_UPDATE",
        "STACK_DRIFT_CHECK",
        "IMAGE_PUBLISH",
        "RUNTIME_CONTEXT_WRITE",
        "CHANGESET_CREATE",
        "CHANGESET_EXECUTE",
    }
)


def _plan_path(value: object) -> str:
    if not isinstance(value, str) or _PLAN_PATH.fullmatch(value) is None:
        raise ReleasePlanAssemblyError("request artifact path is unsafe")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or ".." in parsed.parts or parsed.as_posix() != value:
        raise ReleasePlanAssemblyError("request artifact path is unsafe")
    return value


@dataclass(frozen=True, slots=True)
class PreclosedStaticRequestV2:
    """Acyclic static request for steps whose live inputs come from the prefix.

    The request intentionally carries neither a release-plan digest nor any
    Runtime, Endpoint, context, or verification result.  The outer v2 mutation
    request binds the eventual plan and completed prefix without creating a
    plan/request hash cycle.
    """

    SCHEMA: ClassVar[str] = "personal-operator.preclosed-static-request.v2"
    FIELDS: ClassVar[set[str]] = {
        "schema",
        "kind",
        "sourceCommit",
        "sourceTree",
        "account",
        "region",
        "subject",
    }

    kind: str
    source_commit: str
    source_tree: str
    account: str
    region: str
    subject: str

    def __post_init__(self) -> None:
        if self.kind not in _STATIC_KINDS:
            raise ReleasePlanAssemblyError(
                "preclosed static request kind is not closed"
            )
        if (
            not isinstance(self.source_commit, str)
            or _COMMIT.fullmatch(self.source_commit) is None
            or not isinstance(self.source_tree, str)
            or _COMMIT.fullmatch(self.source_tree) is None
        ):
            raise ReleasePlanAssemblyError(
                "preclosed static request source identity is invalid"
            )
        _exact_account(self.account)
        _exact_region(self.region)
        expected = {
            "RUNTIME_CONTEXT_WRITE": (
                f"release:{self.account}:{self.region}:{self.source_commit}:"
                "artifact:build/runtime-context.json"
            ),
            "VERIFY": (
                f"release:{self.account}:{self.region}:"
                f"{self.source_commit}:verify"
            ),
        }[self.kind]
        if self.subject != expected:
            raise ReleasePlanAssemblyError(
                "preclosed static request crosses its exact subject"
            )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "PreclosedStaticRequestV2":
        try:
            raw = parse_canonical_object(payload)
        except ContractError as error:
            raise ReleasePlanAssemblyError(
                "preclosed static request is not canonical"
            ) from error
        if set(raw) != cls.FIELDS or raw.get("schema") != cls.SCHEMA:
            raise ReleasePlanAssemblyError(
                "preclosed static request fields are not exact"
            )
        kind = raw.get("kind")
        commit = raw.get("sourceCommit")
        tree = raw.get("sourceTree")
        account = raw.get("account")
        region = raw.get("region")
        subject = raw.get("subject")
        if kind not in _STATIC_KINDS:
            raise ReleasePlanAssemblyError(
                "preclosed static request kind is not closed"
            )
        if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
            raise ReleasePlanAssemblyError(
                "preclosed static request commit is invalid"
            )
        if not isinstance(tree, str) or _COMMIT.fullmatch(tree) is None:
            raise ReleasePlanAssemblyError(
                "preclosed static request tree is invalid"
            )
        account = _exact_account(account)
        region = _exact_region(region)
        if not isinstance(subject, str) or not subject or len(subject) > 512:
            raise ReleasePlanAssemblyError(
                "preclosed static request subject is invalid"
            )
        if "*" in subject or any(character.isspace() for character in subject):
            raise ReleasePlanAssemblyError(
                "preclosed static request subject is invalid"
            )
        expected = {
            "RUNTIME_CONTEXT_WRITE": (
                f"release:{account}:{region}:{commit}:"
                "artifact:build/runtime-context.json"
            ),
            "VERIFY": f"release:{account}:{region}:{commit}:verify",
        }[kind]
        if subject != expected:
            raise ReleasePlanAssemblyError(
                "preclosed static request crosses its exact subject"
            )
        return cls(kind, commit, tree, account, region, subject)

    def to_mapping(self) -> dict[str, str]:
        return {
            "schema": self.SCHEMA,
            "kind": self.kind,
            "sourceCommit": self.source_commit,
            "sourceTree": self.source_tree,
            "account": self.account,
            "region": self.region,
            "subject": self.subject,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class PreclosedRequestArtifactV2:
    step_id: str
    path: str
    payload: bytes

    def __post_init__(self) -> None:
        if (
            not isinstance(self.step_id, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", self.step_id) is None
        ):
            raise ReleasePlanAssemblyError("preclosed step ID is invalid")
        _plan_path(self.path)
        if not isinstance(self.payload, bytes) or not self.payload:
            raise ReleasePlanAssemblyError(
                "preclosed request artifact bytes are invalid"
            )


@dataclass(frozen=True, slots=True)
class PreclosedReleaseArtifactsV2:
    source_commit: str
    source_tree: str
    account: str
    region: str
    driver_sha256: str
    evidence_runtime_sha256: str
    foundation_assembly: Path
    runtime_assembly: Path
    endpoint_assembly: Path
    consumer_assembly: Path
    requests: tuple[PreclosedRequestArtifactV2, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_commit, str)
            or _COMMIT.fullmatch(self.source_commit) is None
        ):
            raise ReleasePlanAssemblyError("release source commit is invalid")
        if (
            not isinstance(self.source_tree, str)
            or _COMMIT.fullmatch(self.source_tree) is None
        ):
            raise ReleasePlanAssemblyError("release source tree is invalid")
        _exact_account(self.account)
        _exact_region(self.region)
        if (
            not isinstance(self.driver_sha256, str)
            or _SHA256.fullmatch(self.driver_sha256) is None
            or not isinstance(self.evidence_runtime_sha256, str)
            or _SHA256.fullmatch(self.evidence_runtime_sha256) is None
        ):
            raise ReleasePlanAssemblyError(
                "release driver or evidence-runtime digest is invalid"
            )
        if not isinstance(self.requests, tuple) or not self.requests:
            raise ReleasePlanAssemblyError(
                "preclosed request artifact inventory is empty"
            )
        if any(
            not isinstance(item, PreclosedRequestArtifactV2)
            for item in self.requests
        ):
            raise ReleasePlanAssemblyError(
                "preclosed request artifact inventory is not typed"
            )
        step_ids = [item.step_id for item in self.requests]
        paths = [item.path for item in self.requests]
        if len(step_ids) != len(set(step_ids)) or len(paths) != len(set(paths)):
            raise ReleasePlanAssemblyError(
                "preclosed request artifact IDs and paths are not unique"
            )

    def request_mapping(self) -> Mapping[str, PreclosedRequestArtifactV2]:
        return MappingProxyType({item.step_id: item for item in self.requests})


@dataclass(frozen=True, slots=True)
class AssembledReleasePlanV2:
    plan: ReleasePlanV2
    payloads: tuple[tuple[str, bytes], ...]
    stages: tuple[CloudAssemblyStageV2, ...]

    def payload(self, path: str) -> bytes:
        for candidate, payload in self.payloads:
            if candidate == path:
                return payload
        raise ReleasePlanAssemblyError("plan-bound artifact payload is absent")

    def payload_mapping(self) -> Mapping[str, bytes]:
        return MappingProxyType(dict(self.payloads))


@dataclass(frozen=True, slots=True)
class _StepInputV2:
    step_id: str
    phase: str
    kind: str
    subject: str
    artifact: PreclosedRequestArtifactV2
    expected_template_sha256: str = ""
    expected_template_parameter_sha256: str = ""
    expected_observed_request_sha256: str = ""
    expected_content_sha256: str = ""

    def to_mapping(self, ordinal: int) -> dict[str, object]:
        digest = hashlib.sha256(self.artifact.payload).hexdigest()
        return {
            "id": self.step_id,
            "phase": self.phase,
            "ordinal": ordinal,
            "kind": self.kind,
            "subject": self.subject,
            "mutation": self.kind in _MUTATION_KINDS,
            "requestArtifact": self.artifact.path,
            "requestSha256": digest,
            "expectedTemplateSha256": self.expected_template_sha256,
            "expectedTemplateParameterSha256": (
                self.expected_template_parameter_sha256
            ),
            "expectedRequestSha256": digest,
            "expectedObservedRequestSha256": (
                self.expected_observed_request_sha256
            ),
            "expectedContentSha256": self.expected_content_sha256,
        }


def _parse_asset_artifact(
    payload: bytes,
) -> tuple[AssetPublicationV2, bytes]:
    try:
        minimum = len(ASSET_ARTIFACT_MAGIC) + ASSET_ARTIFACT_HEADER_BYTES
        if len(payload) <= minimum or not payload.startswith(ASSET_ARTIFACT_MAGIC):
            raise ReleasePlanAssemblyError(
                "asset publication artifact magic is invalid"
            )
        offset = len(ASSET_ARTIFACT_MAGIC)
        header_size = struct.unpack(">I", payload[offset : offset + 4])[0]
        if not 1 <= header_size <= MAX_ASSET_HEADER_BYTES:
            raise ReleasePlanAssemblyError(
                "asset publication artifact header is unbounded"
            )
        header_start = offset + 4
        header_end = header_start + header_size
        if header_end >= len(payload):
            raise ReleasePlanAssemblyError(
                "asset publication artifact is truncated"
            )
        metadata = AssetPublicationV2.from_header_bytes(
            payload[header_start:header_end]
        )
        body = payload[header_end:]
        if (
            len(body) != metadata.content_size
            or hashlib.sha256(body).hexdigest() != metadata.content_sha256
        ):
            raise ReleasePlanAssemblyError(
                "asset publication content differs from its typed header"
            )
        return metadata, body
    except ReleasePlanAssemblyError:
        raise
    except (AssetPublicationError, OverflowError, struct.error) as error:
        raise ReleasePlanAssemblyError(
            "asset publication artifact is invalid"
        ) from error


def _image_effect_id(payload: bytes) -> str:
    try:
        if not payload.startswith(IMAGE_EFFECT_MAGIC):
            raise ReleasePlanAssemblyError(
                "image effect artifact magic is invalid"
            )
        offset = len(IMAGE_EFFECT_MAGIC)
        header_size = struct.unpack(">I", payload[offset : offset + 4])[0]
        if not 1 <= header_size <= 64 * 1024:
            raise ReleasePlanAssemblyError(
                "image effect artifact header is invalid"
            )
        header = _json_object(
            payload[offset + 4 : offset + 4 + header_size],
            label="image effect artifact header",
        )
        effect_id = header.get("effectId")
        if not isinstance(effect_id, str) or not effect_id:
            raise ReleasePlanAssemblyError(
                "image effect artifact ID is invalid"
            )
        return effect_id
    except ReleasePlanAssemblyError:
        raise
    except (ContractError, OverflowError, struct.error) as error:
        raise ReleasePlanAssemblyError(
            "image effect artifact header is invalid"
        ) from error


def _descriptor_mapping(raw: object, *, label: str) -> dict[str, object]:
    if (
        not isinstance(raw, dict)
        or set(raw) != {"mediaType", "digest", "size"}
        or not isinstance(raw.get("mediaType"), str)
        or not isinstance(raw.get("digest"), str)
        or not isinstance(raw.get("size"), int)
        or isinstance(raw.get("size"), bool)
    ):
        raise ReleasePlanAssemblyError(f"{label} descriptor is invalid")
    return dict(raw)


def _validate_image_effect_closure(
    plan: ImagePublicationPlanV1,
    effects: Sequence[ImagePublicationEffectV1],
) -> tuple[tuple[ImagePublicationEffectV1, ...], int]:
    if len({item.effect_id for item in effects}) != len(effects):
        raise ReleasePlanAssemblyError("image effect IDs are not unique")
    by_kind: dict[str, list[ImagePublicationEffectV1]] = {}
    for effect in effects:
        by_kind.setdefault(effect.effect_kind, []).append(effect)
    for kind in (
        "ECR_SUBJECT_MANIFEST_PUT",
        "ECR_SBOM_REFERRER_PUT",
        "ECR_PROVENANCE_REFERRER_PUT",
    ):
        if len(by_kind.get(kind, [])) != 1:
            raise ReleasePlanAssemblyError(
                "image manifest effect inventory is not exact"
            )
    subject_effect = by_kind["ECR_SUBJECT_MANIFEST_PUT"][0]
    sbom_effect = by_kind["ECR_SBOM_REFERRER_PUT"][0]
    provenance_effect = by_kind["ECR_PROVENANCE_REFERRER_PUT"][0]
    if (
        subject_effect.digest != plan.subject.digest
        or subject_effect.media_type != plan.subject.media_type
        or subject_effect.size != plan.subject.size
        or sbom_effect.digest != plan.sbom_manifest.digest
        or sbom_effect.media_type != plan.sbom_manifest.media_type
        or sbom_effect.size != plan.sbom_manifest.size
        or provenance_effect.digest != plan.provenance_manifest.digest
        or provenance_effect.media_type != plan.provenance_manifest.media_type
        or provenance_effect.size != plan.provenance_manifest.size
        or sbom_effect.subject_digest != plan.subject.digest
        or provenance_effect.subject_digest != plan.subject.digest
        or sbom_effect.artifact_type != SBOM_ARTIFACT_TYPE
        or provenance_effect.artifact_type != PROVENANCE_ARTIFACT_TYPE
    ):
        raise ReleasePlanAssemblyError(
            "image manifest or referrer target crosses its typed plan"
        )

    subject_manifest = _json_object(
        subject_effect.payload, label="image subject manifest"
    )
    sbom_manifest = _json_object(
        sbom_effect.payload, label="image SBOM manifest"
    )
    provenance_manifest = _json_object(
        provenance_effect.payload, label="image provenance manifest"
    )
    if (
        _descriptor_mapping(
            subject_manifest.get("config"), label="subject config"
        )
        != plan.config.to_mapping()
        or subject_manifest.get("layers")
        != [item.to_mapping() for item in plan.layers]
    ):
        raise ReleasePlanAssemblyError(
            "image subject manifest differs from its typed plan"
        )
    expected_blobs: dict[str, dict[str, object]] = {}

    def register_blob(descriptor: object) -> None:
        digest = getattr(descriptor, "digest", None)
        mapping = getattr(descriptor, "to_mapping", lambda: None)()
        if not isinstance(digest, str) or not isinstance(mapping, dict):
            raise ReleasePlanAssemblyError(
                "image plan blob descriptor is invalid"
            )
        previous = expected_blobs.get(digest)
        if previous is not None:
            if previous != mapping:
                raise ReleasePlanAssemblyError(
                    "image plan blob descriptor digest collides"
                )
            return
        expected_blobs[digest] = mapping

    for descriptor in (
        plan.config,
        *plan.layers,
        plan.sbom_payload,
        plan.provenance_payload,
    ):
        register_blob(descriptor)
    for label, manifest, expected_payload in (
        ("SBOM", sbom_manifest, plan.sbom_payload),
        ("provenance", provenance_manifest, plan.provenance_payload),
    ):
        if _descriptor_mapping(
            manifest.get("subject"), label=f"{label} referrer subject"
        ) != plan.subject.to_mapping():
            raise ReleasePlanAssemblyError(
                f"image {label} referrer payload subject differs from the typed plan"
            )
        config = _descriptor_mapping(
            manifest.get("config"), label=f"{label} empty config"
        )
        if config["mediaType"] != OCI_EMPTY_CONFIG_MEDIA_TYPE:
            raise ReleasePlanAssemblyError(
                f"image {label} empty config is invalid"
            )
        layers = manifest.get("layers")
        if layers != [expected_payload.to_mapping()]:
            raise ReleasePlanAssemblyError(
                f"image {label} payload closure differs"
            )
        digest = config["digest"]
        previous = expected_blobs.get(digest)
        if previous is not None:
            if previous != config:
                raise ReleasePlanAssemblyError(
                    "image blob descriptor collides across manifests"
                )
            continue
        expected_blobs[digest] = config
    blob_effects = by_kind.get("ECR_BLOB_PUT", [])
    actual_blobs = {
        item.digest: {
            "mediaType": item.media_type,
            "digest": item.digest,
            "size": item.size,
        }
        for item in blob_effects
    }
    if len(actual_blobs) != len(blob_effects) or actual_blobs != expected_blobs:
        raise ReleasePlanAssemblyError(
            "image blob effect closure differs from the typed plan"
        )
    ordered_blobs = tuple(sorted(blob_effects, key=lambda item: item.provider_subject))
    ordered = (
        *ordered_blobs,
        subject_effect,
        sbom_effect,
        provenance_effect,
    )
    if len({item.digest for item in ordered}) != len(ordered):
        raise ReleasePlanAssemblyError(
            "image effect digest inventory is not unique"
        )
    return tuple(ordered), len(ordered_blobs)


class ReleasePlanAssemblerV2:
    """Build one deterministic ReleasePlanV2 from retained pre-cloud bytes."""

    @classmethod
    def assemble(
        cls,
        source: PreclosedReleaseArtifactsV2,
        *,
        _retained_stages: tuple[CloudAssemblyStageV2, ...] | None = None,
    ) -> AssembledReleasePlanV2:
        if not isinstance(source, PreclosedReleaseArtifactsV2):
            raise ReleasePlanAssemblyError(
                "release assembler input is not preclosed and typed"
            )
        if _retained_stages is None:
            foundation = TrustedCloudAssemblyReaderV2.read(
                source.foundation_assembly,
                stage="foundation",
                account=source.account,
                region=source.region,
            )
            runtime = TrustedCloudAssemblyReaderV2.read(
                source.runtime_assembly,
                stage="runtime",
                account=source.account,
                region=source.region,
            )
            endpoint = TrustedCloudAssemblyReaderV2.read(
                source.endpoint_assembly,
                stage="endpoint",
                account=source.account,
                region=source.region,
            )
            if source.consumer_assembly != source.endpoint_assembly:
                consumer = TrustedCloudAssemblyReaderV2.read(
                    source.consumer_assembly,
                    stage="consumer",
                    account=source.account,
                    region=source.region,
                )
            else:
                consumer = cls._consumer_view(endpoint)
            stages = (foundation, runtime, endpoint, consumer)
        else:
            if type(_retained_stages) is not tuple:
                raise ReleasePlanAssemblyError(
                    "retained CloudAssembly stages are not exact"
                )
            stages = _retained_stages
        if (
            len(stages) != len(ASSEMBLY_STAGES)
            or any(type(item) is not CloudAssemblyStageV2 for item in stages)
            or tuple(item.stage for item in stages) != ASSEMBLY_STAGES
            or any(
                (item.account, item.region) != (source.account, source.region)
                for item in stages
            )
        ):
            raise ReleasePlanAssemblyError(
                "retained CloudAssembly stage identity is not exact"
            )
        stage_by_name = {item.stage: item for item in stages}
        cls._validate_consumer_stage(
            stage_by_name["endpoint"], stage_by_name["consumer"]
        )
        requests = source.request_mapping()
        steps: list[_StepInputV2] = []
        consumed: set[str] = set()

        def artifact(step_id: str) -> PreclosedRequestArtifactV2:
            try:
                value = requests[step_id]
            except KeyError as error:
                raise ReleasePlanAssemblyError(
                    f"preclosed request artifact is missing for {step_id}"
                ) from error
            consumed.add(step_id)
            return value

        def identity(values: tuple[str, str, str, str], *, label: str) -> None:
            expected = (
                source.source_commit,
                source.source_tree,
                source.account,
                source.region,
            )
            if values != expected:
                raise ReleasePlanAssemblyError(
                    f"{label} crosses the exact release identity"
                )

        baseline_artifact = artifact("foundation-baseline")
        try:
            baseline = BaselineObservationRequestV1.from_bytes(
                baseline_artifact.payload
            )
        except BaselineObserverV2Error as error:
            raise ReleasePlanAssemblyError(
                "baseline request artifact is invalid"
            ) from error
        if (
            baseline.account,
            baseline.region,
            baseline.source_commit,
        ) != (source.account, source.region, source.source_commit):
            raise ReleasePlanAssemblyError(
                "baseline request crosses the exact release identity"
            )
        release_subject = (
            f"release:{source.account}:{source.region}:{source.source_commit}"
        )
        steps.append(
            _StepInputV2(
                "foundation-baseline",
                "foundation",
                "BASELINE_OBSERVE",
                f"{release_subject}:baseline",
                baseline_artifact,
            )
        )

        bootstrap = cls._cloudformation(
            artifact("foundation-bootstrap-cdktoolkit"),
            kind="BOOTSTRAP_STACK",
            stack_name=BOOTSTRAP_STACK,
            source=source,
        )
        steps.append(
            cls._cloudformation_step(
                "foundation-bootstrap-cdktoolkit",
                "foundation",
                bootstrap,
                requests["foundation-bootstrap-cdktoolkit"],
            )
        )
        steps.append(
            cls._drift_step(
                artifact("foundation-drift-cdktoolkit"),
                phase="foundation",
                stack_name=BOOTSTRAP_STACK,
                source=source,
            )
        )

        assets = cls._merged_assets(stages)
        for expected_asset in assets:
            step_id = f"foundation-asset-{expected_asset.asset_id}"
            request = artifact(step_id)
            metadata, body = _parse_asset_artifact(request.payload)
            identity(
                (
                    metadata.source_commit,
                    metadata.source_tree,
                    metadata.account,
                    metadata.region,
                ),
                label="asset publication request",
            )
            if (
                metadata.asset_id != expected_asset.asset_id
                or metadata.bucket_name != expected_asset.bucket_name
                or metadata.object_key != expected_asset.object_key
                or metadata.content_type
                != (
                    "application/json"
                    if expected_asset.object_key.endswith(".json")
                    else "application/zip"
                )
                or body != expected_asset.source_bytes
            ):
                raise ReleasePlanAssemblyError(
                    "asset publication artifact differs from CloudAssembly"
                )
            steps.append(
                _StepInputV2(
                    step_id,
                    "foundation",
                    "ASSET_PUBLISH",
                    f"cdk:asset:{metadata.asset_id}",
                    request,
                    expected_content_sha256=metadata.content_sha256,
                )
            )

        foundation_stage = stage_by_name["foundation"]
        for stack_name in FOUNDATION_STACKS:
            slug = stack_name.lower()
            create_id = f"foundation-create-{slug}"
            operation = cls._cloudformation(
                artifact(create_id),
                kind="STACK_CREATE",
                stack_name=stack_name,
                source=source,
                stage=foundation_stage,
            )
            steps.append(
                cls._cloudformation_step(
                    create_id,
                    "foundation",
                    operation,
                    requests[create_id],
                )
            )
            steps.append(
                cls._drift_step(
                    artifact(f"foundation-drift-{slug}"),
                    phase="foundation",
                    stack_name=stack_name,
                    source=source,
                )
            )

        image_observe = artifact("image-observe")
        if image_observe.path != "build/image-publication-plan.json":
            raise ReleasePlanAssemblyError(
                "image publication plan artifact path is not exact"
            )
        try:
            image_plan = ImagePublicationPlanV1.from_bytes(image_observe.payload)
        except ImagePublicationError as error:
            raise ReleasePlanAssemblyError(
                "image publication plan artifact is invalid"
            ) from error
        identity(
            (
                image_plan.source_commit,
                image_plan.source_tree,
                image_plan.account,
                image_plan.region,
            ),
            label="image publication plan",
        )
        publication_sha256 = image_plan.publication_plan_sha256
        parsed_effects: list[tuple[ImagePublicationEffectV1, PreclosedRequestArtifactV2]] = []
        for step_id, candidate in requests.items():
            if not step_id.startswith("image-ecr-"):
                continue
            try:
                effect_id = _image_effect_id(candidate.payload)
                effect = ImagePublicationEffectV1.from_private_bytes(
                    candidate.payload,
                    expected_private_file_sha256=hashlib.sha256(
                        candidate.payload
                    ).hexdigest(),
                    expected_effect_id=effect_id,
                    expected_publication_plan_sha256=publication_sha256,
                )
            except (ArtifactSubstitutionError, ImagePublicationError) as error:
                raise ReleasePlanAssemblyError(
                    "image effect artifact is invalid"
                ) from error
            if step_id != f"image-{effect.effect_id}":
                raise ReleasePlanAssemblyError(
                    "image effect step ID differs from its typed artifact"
                )
            identity(
                (
                    effect.source_commit,
                    effect.source_tree,
                    effect.account,
                    effect.region,
                ),
                label="image effect request",
            )
            parsed_effects.append((effect, candidate))
        ordered_effects, blob_count = _validate_image_effect_closure(
            image_plan, [item for item, _artifact in parsed_effects]
        )
        effect_artifacts = {
            effect.effect_id: candidate for effect, candidate in parsed_effects
        }
        for effect in ordered_effects:
            request = effect_artifacts[effect.effect_id]
            consumed.add(request.step_id)
            steps.append(
                _StepInputV2(
                    request.step_id,
                    "image",
                    "IMAGE_PUBLISH",
                    effect.provider_subject,
                    request,
                    expected_content_sha256=effect.digest.removeprefix(
                        "sha256:"
                    ),
                )
            )
        image_subject = (
            f"ecr:{source.account}:{source.region}:repository:{REPOSITORY_NAME}:"
            f"release:{source.source_commit}"
        )
        steps.append(
            _StepInputV2(
                "image-observe",
                "image",
                "IMAGE_OBSERVE",
                image_subject,
                image_observe,
                expected_content_sha256=image_plan.subject.digest.removeprefix(
                    "sha256:"
                ),
            )
        )

        runtime_update = cls._cloudformation(
            artifact("runtime-update-agentcore"),
            kind="STACK_UPDATE",
            stack_name="OpenClawAgentCore",
            source=source,
            stage=stage_by_name["runtime"],
        )
        steps.append(
            cls._cloudformation_step(
                "runtime-update-agentcore",
                "runtime",
                runtime_update,
                requests["runtime-update-agentcore"],
            )
        )
        steps.append(
            cls._drift_step(
                artifact("runtime-drift-agentcore"),
                phase="runtime",
                stack_name="OpenClawAgentCore",
                source=source,
            )
        )
        harden_artifact = artifact("runtime-harden-agentcore")
        try:
            harden = AgentCoreHardeningOperationV1.from_bytes(
                harden_artifact.payload
            )
        except AgentCoreHardeningError as error:
            raise ReleasePlanAssemblyError(
                "AgentCore hardening artifact is invalid"
            ) from error
        identity(
            (
                harden.source_commit,
                harden.source_tree,
                harden.account,
                harden.region,
            ),
            label="AgentCore hardening request",
        )
        steps.append(
            _StepInputV2(
                "runtime-harden-agentcore",
                "runtime",
                "AGENTCORE_HARDEN",
                harden.subject,
                harden_artifact,
            )
        )

        endpoint_update = cls._cloudformation(
            artifact("endpoint-update-agentcore"),
            kind="STACK_UPDATE",
            stack_name="OpenClawAgentCore",
            source=source,
            stage=stage_by_name["endpoint"],
        )
        steps.append(
            cls._cloudformation_step(
                "endpoint-update-agentcore",
                "endpoint",
                endpoint_update,
                requests["endpoint-update-agentcore"],
            )
        )
        steps.append(
            cls._drift_step(
                artifact("endpoint-drift-agentcore"),
                phase="endpoint",
                stack_name="OpenClawAgentCore",
                source=source,
            )
        )

        context_artifact = artifact("context-write")
        context_request = PreclosedStaticRequestV2.from_bytes(
            context_artifact.payload
        )
        if context_request.kind != "RUNTIME_CONTEXT_WRITE":
            raise ReleasePlanAssemblyError(
                "context request kind is not exact"
            )
        identity(
            (
                context_request.source_commit,
                context_request.source_tree,
                context_request.account,
                context_request.region,
            ),
            label="context request",
        )
        steps.append(
            _StepInputV2(
                "context-write",
                "context",
                "RUNTIME_CONTEXT_WRITE",
                context_request.subject,
                context_artifact,
            )
        )

        consumer_stage = stage_by_name["consumer"]
        phase_by_stack = {
            "OpenClawRouter": ("router-cron-cs", "router-cron"),
            "OpenClawCron": ("router-cron-cs", "router-cron"),
            "PersonalOperatorScheduler": ("scheduler-cs", "scheduler"),
            "PersonalOperatorWeb": ("web-cs", "web"),
        }

        def append_change_set_create(stack_name: str) -> None:
            create_phase, _execute_phase = phase_by_stack[stack_name]
            slug = stack_name.lower()
            create_id = f"{create_phase}-create-{slug}"
            operation = cls._cloudformation(
                artifact(create_id),
                kind="CHANGESET_CREATE",
                stack_name=stack_name,
                source=source,
                stage=consumer_stage,
            )
            steps.append(
                cls._cloudformation_step(
                    create_id,
                    create_phase,
                    operation,
                    requests[create_id],
                )
            )

        def append_change_set_execute(stack_name: str) -> None:
            _create_phase, execute_phase = phase_by_stack[stack_name]
            slug = stack_name.lower()
            execute_id = f"{execute_phase}-execute-{slug}"
            operation = cls._cloudformation(
                artifact(execute_id),
                kind="CHANGESET_EXECUTE",
                stack_name=stack_name,
                source=source,
            )
            steps.append(
                cls._cloudformation_step(
                    execute_id,
                    execute_phase,
                    operation,
                    requests[execute_id],
                )
            )
            steps.append(
                cls._drift_step(
                    artifact(f"{execute_phase}-drift-{slug}"),
                    phase=execute_phase,
                    stack_name=stack_name,
                    source=source,
                )
            )

        for stack_name in ("OpenClawRouter", "OpenClawCron"):
            append_change_set_create(stack_name)
        for stack_name in ("OpenClawRouter", "OpenClawCron"):
            append_change_set_execute(stack_name)
        append_change_set_create("PersonalOperatorScheduler")
        append_change_set_execute("PersonalOperatorScheduler")
        append_change_set_create("PersonalOperatorWeb")
        append_change_set_execute("PersonalOperatorWeb")

        verify_artifact = artifact("verify")
        verify = PreclosedStaticRequestV2.from_bytes(verify_artifact.payload)
        if verify.kind != "VERIFY":
            raise ReleasePlanAssemblyError("verify request kind is not exact")
        identity(
            (
                verify.source_commit,
                verify.source_tree,
                verify.account,
                verify.region,
            ),
            label="verify request",
        )
        steps.append(
            _StepInputV2(
                "verify",
                "verify",
                "VERIFY",
                verify.subject,
                verify_artifact,
            )
        )

        if consumed != set(requests):
            raise ReleasePlanAssemblyError(
                "preclosed request artifact inventory contains an orphan"
            )
        expected_count = 38 + len(assets) + blob_count
        if len(steps) != expected_count:
            raise ReleasePlanAssemblyError(
                "release step cardinality differs from 38 + A + B"
            )
        artifact_inventory = sorted(
            (
                {
                    "path": item.path,
                    "size": len(item.payload),
                    "sha256": hashlib.sha256(item.payload).hexdigest(),
                }
                for item in requests.values()
            ),
            key=lambda item: item["path"],
        )
        image_digest = image_plan.subject.digest
        image_uri = (
            f"{source.account}.dkr.ecr.{source.region}.amazonaws.com/"
            f"{REPOSITORY_NAME}@{image_digest}"
        )
        value = {
            "schema": ReleasePlanV2.SCHEMA,
            "transactionId": f"release_{source.source_commit}",
            "sourceCommit": source.source_commit,
            "sourceTree": source.source_tree,
            "account": source.account,
            "region": source.region,
            "releaseMode": "CLEAN_ACCOUNT",
            "driverSha256": source.driver_sha256,
            "evidenceRuntimeSha256": source.evidence_runtime_sha256,
            "runtimeImageDigest": image_digest,
            "runtimeImageUri": image_uri,
            "runtimeEndpointName": f"release_{source.source_commit}",
            "contextRelativePath": "build/runtime-context.json",
            "foundationInputsRelativePath": (
                "build/foundation-runtime-inputs.json"
            ),
            "derivationVersion": "foundation-runtime-inputs-v1",
            "artifacts": artifact_inventory,
            "steps": [
                step.to_mapping(ordinal) for ordinal, step in enumerate(steps)
            ],
            "rollbackTarget": {"mode": "NO_PRIOR_RELEASE"},
        }
        try:
            plan = ReleasePlanV2.from_mapping(value)
        except ContractError as error:
            raise ReleasePlanAssemblyError(
                "assembled release plan violates the closed v2 contract"
            ) from error
        try:
            plan_bytes = plan.to_bytes()
            if len(plan_bytes) > MAX_CONTRACT_BYTES:
                raise ReleasePlanAssemblyError(
                    "assembled release plan exceeds the canonical byte limit"
                )
            plan = ReleasePlanV2.from_bytes(plan_bytes)
        except ReleasePlanAssemblyError:
            raise
        except (ContractError, RecursionError) as error:
            raise ReleasePlanAssemblyError(
                "assembled release plan cannot be canonically reparsed"
            ) from error
        payloads = tuple(
            sorted(
                ((item.path, item.payload) for item in requests.values()),
                key=lambda item: item[0],
            )
        )
        return AssembledReleasePlanV2(plan, payloads, stages)

    @staticmethod
    def _merged_assets(
        stages: Sequence[CloudAssemblyStageV2],
    ) -> tuple[CloudAssemblyAssetV2, ...]:
        merged: dict[str, CloudAssemblyAssetV2] = {}
        for stage in stages:
            for asset in stage.assets:
                previous = merged.get(asset.asset_id)
                if previous is None:
                    merged[asset.asset_id] = asset
                    continue
                comparable = (
                    "source_path",
                    "packaging",
                    "bucket_name",
                    "object_key",
                    "region",
                    "destination_id",
                    "assume_role_arn",
                    "source_bytes",
                )
                if any(
                    getattr(previous, field) != getattr(asset, field)
                    for field in comparable
                ):
                    raise ReleasePlanAssemblyError(
                        "duplicate CDK asset ID crosses source, content, or destination"
                    )
        return tuple(merged[key] for key in sorted(merged))

    @staticmethod
    def _validate_consumer_stage(
        endpoint: CloudAssemblyStageV2,
        consumer: CloudAssemblyStageV2,
    ) -> None:
        if (
            endpoint.manifest_bytes != consumer.manifest_bytes
            or endpoint.templates != consumer.templates
            or endpoint.assets != consumer.assets
        ):
            raise ReleasePlanAssemblyError(
                "CloudAssembly consumer stage semantics are not exact"
            )

    @staticmethod
    def _consumer_view(endpoint: CloudAssemblyStageV2) -> CloudAssemblyStageV2:
        """Derive Consumer from the exact retained Endpoint bytes once."""

        if type(endpoint) is not CloudAssemblyStageV2 or endpoint.stage != "endpoint":
            raise ReleasePlanAssemblyError(
                "retained endpoint CloudAssembly stage is not exact"
            )
        return CloudAssemblyStageV2(
            stage="consumer",
            account=endpoint.account,
            region=endpoint.region,
            manifest_bytes=endpoint.manifest_bytes,
            templates=endpoint.templates,
            assets=endpoint.assets,
        )

    @staticmethod
    def _cloudformation(
        artifact: PreclosedRequestArtifactV2,
        *,
        kind: str,
        stack_name: str,
        source: PreclosedReleaseArtifactsV2,
        stage: CloudAssemblyStageV2 | None = None,
    ) -> CloudFormationOperationV2:
        try:
            operation = CloudFormationOperationV2.from_bytes(artifact.payload)
        except CloudFormationMutationError as error:
            raise ReleasePlanAssemblyError(
                "CloudFormation request artifact is invalid"
            ) from error
        if (
            operation.kind,
            operation.stack_name,
            operation.source_commit,
            operation.source_tree,
            operation.account,
            operation.region,
        ) != (
            kind,
            stack_name,
            source.source_commit,
            source.source_tree,
            source.account,
            source.region,
        ):
            raise ReleasePlanAssemblyError(
                "CloudFormation request crosses its exact release step"
            )
        if stage is not None:
            template = next(
                item for item in stage.templates if item.stack_name == stack_name
            )
            try:
                reviewed = operation.reviewed_template_body.encode(
                    "utf-8", errors="strict"
                )
            except UnicodeError as error:
                raise ReleasePlanAssemblyError(
                    "CloudFormation reviewed template is not strict UTF-8"
                ) from error
            if (
                reviewed != template.template_bytes
                or operation.template_asset_id != template.template_asset_id
            ):
                raise ReleasePlanAssemblyError(
                    "CloudFormation request template differs from its stage"
                )
        return operation

    @staticmethod
    def _cloudformation_step(
        step_id: str,
        phase: str,
        operation: CloudFormationOperationV2,
        artifact: PreclosedRequestArtifactV2,
    ) -> _StepInputV2:
        stack_subject = (
            f"cfn:{operation.account}:{operation.region}:stack:"
            f"{operation.stack_name}:release:{operation.source_commit}"
        )
        dynamic_template_sha256 = ""
        if operation.kind == "STACK_UPDATE":
            dynamic_template_sha256 = hashlib.sha256(
                operation.reviewed_template_body.encode("utf-8")
            ).hexdigest()
        return _StepInputV2(
            step_id,
            phase,
            operation.kind,
            stack_subject,
            artifact,
            expected_template_sha256=dynamic_template_sha256,
            expected_template_parameter_sha256=(
                operation.expected_template_parameter_sha256
            ),
            expected_observed_request_sha256=(
                operation.expected_observed_request_sha256
            ),
        )

    @staticmethod
    def _drift_step(
        artifact: PreclosedRequestArtifactV2,
        *,
        phase: str,
        stack_name: str,
        source: PreclosedReleaseArtifactsV2,
    ) -> _StepInputV2:
        try:
            operation = StackDriftOperationV1.from_bytes(artifact.payload)
        except StackDriftError as error:
            raise ReleasePlanAssemblyError(
                "stack drift request artifact is invalid"
            ) from error
        if (
            operation.stack_name,
            operation.phase,
            operation.occurrence,
            operation.source_commit,
            operation.source_tree,
            operation.account,
            operation.region,
        ) != (
            stack_name,
            phase,
            artifact.step_id,
            source.source_commit,
            source.source_tree,
            source.account,
            source.region,
        ):
            raise ReleasePlanAssemblyError(
                "stack drift request crosses its exact release step"
            )
        return _StepInputV2(
            artifact.step_id,
            phase,
            "STACK_DRIFT_CHECK",
            operation.subject,
            artifact,
        )


def assemble_release_plan_v2(
    source: PreclosedReleaseArtifactsV2,
) -> AssembledReleasePlanV2:
    """Functional entry point for the authority-free deterministic assembler."""

    return ReleasePlanAssemblerV2.assemble(source)
