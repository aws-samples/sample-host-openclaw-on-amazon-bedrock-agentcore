"""Strict, canonical, AWS-free contracts for immutable staging releases."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import uuid
import fcntl
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Mapping, Protocol, TypeVar


REQUIRED_REGION = "eu-west-1"
RUNTIME_REPOSITORY = "personal-operator/bridge"
MAX_CONTRACT_BYTES = 4 * 1024 * 1024

_SHA_40 = re.compile(r"[0-9a-f]{40}")
_SHA_64 = re.compile(r"[0-9a-f]{64}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_ACCOUNT = re.compile(r"[0-9]{12}")
_RUNTIME_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}")
_RUNTIME_VERSION = re.compile(r"[1-9][0-9]{0,4}")
_BUILDER_IMAGE = re.compile(
    r"public\.ecr\.aws/lambda/python@sha256:[0-9a-f]{64}"
)
_BUILDER_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_SIGNING_PROFILE_NAME = "personal_operator_bridge"


class ContractError(ValueError):
    """A release artifact is ambiguous, noncanonical, or crosses its boundary."""


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ContractError(f"non-finite JSON number is forbidden: {value}")


def _assert_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractError("non-finite JSON number is forbidden")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ContractError("JSON object keys must be strings")
            _assert_finite(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_finite(child)


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Render one canonical UTF-8 JSON object with exactly one trailing newline."""

    if not isinstance(value, Mapping):
        raise ContractError("canonical release artifact must be a JSON object")
    _assert_finite(value)
    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return (rendered + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ContractError("release artifact is not canonical JSON") from error


def parse_canonical_object(payload: bytes) -> dict[str, Any]:
    """Parse canonical JSON while rejecting duplicate keys and alternate bytes."""

    if not isinstance(payload, bytes) or not payload:
        raise ContractError("release artifact bytes are empty")
    if len(payload) > MAX_CONTRACT_BYTES:
        raise ContractError("release artifact exceeds the byte limit")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_constant,
        )
    except ContractError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ContractError("release artifact is invalid JSON") from error
    if not isinstance(value, dict):
        raise ContractError("release artifact must be a JSON object")
    if canonical_json_bytes(value) != payload:
        raise ContractError("release artifact bytes are not canonical")
    return value


def _exact_object(
    value: Mapping[str, Any], expected: set[str], *, label: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        missing = sorted(expected - set(value)) if isinstance(value, Mapping) else []
        extra = sorted(set(value) - expected) if isinstance(value, Mapping) else []
        raise ContractError(
            f"{label} has the wrong fields (missing={missing}, extra={extra})"
        )
    return dict(value)


def _text(value: Any, *, field: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or (pattern is not None and pattern.fullmatch(value) is None):
        raise ContractError(f"{field} is invalid")
    return value


def _count(value: Any, *, field: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ContractError(f"{field} is invalid")
    return value


def _account(value: Any) -> str:
    account = _text(value, field="account", pattern=_ACCOUNT)
    if account == "000000000000":
        raise ContractError("account must not be the synthetic account")
    return account


def _region(value: Any) -> str:
    region = _text(value, field="region")
    if region != REQUIRED_REGION:
        raise ContractError(f"region must be exactly {REQUIRED_REGION}")
    return region


def _safe_path(value: Any, *, field: str) -> str:
    path = _text(value, field=field)
    parsed = PurePosixPath(path)
    if (
        not path
        or parsed.is_absolute()
        or ".." in parsed.parts
        or parsed.as_posix() != path
    ):
        raise ContractError(f"{field} is unsafe")
    return path


def _image_uri(account: str, region: str, digest: str, value: Any) -> str:
    expected = (
        f"{account}.dkr.ecr.{region}.amazonaws.com/"
        f"{RUNTIME_REPOSITORY}@{digest}"
    )
    image_uri = _text(value, field="runtime image URI")
    if image_uri != expected:
        if "@sha256:" not in image_uri:
            raise ContractError("runtime image URI must be immutable")
        raise ContractError("runtime image URI crosses its account, region, or repository")
    return image_uri


def _rollback_reference(
    value: Any, *, account: str, region: str, commit: str
) -> str:
    reference = _text(value, field="rollback reference")
    if not reference:
        return reference
    expected = re.compile(
        rf"rollback:v1:{re.escape(account)}:{re.escape(region)}:"
        rf"{re.escape(commit)}:sha256:[0-9a-f]{{64}}"
    )
    if expected.fullmatch(reference) is None:
        raise ContractError("rollback reference is not exact for this release")
    return reference


class CanonicalContract(Protocol):
    def to_bytes(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class RuntimeContextV3:
    SCHEMA: ClassVar[str] = "personal-operator.runtime-context.v3"
    FIELDS: ClassVar[set[str]] = {
        "schema",
        "sourceCommit",
        "account",
        "region",
        "runtimeId",
        "runtimeEndpointId",
        "runtimeEndpointName",
        "runtimeArn",
        "runtimeVersion",
        "runtimeImageUri",
    }

    source_commit: str
    account: str
    region: str
    runtime_id: str
    runtime_endpoint_id: str
    runtime_endpoint_name: str
    runtime_arn: str
    runtime_version: str
    runtime_image_uri: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RuntimeContextV3":
        value = _exact_object(raw, cls.FIELDS, label="runtime context")
        if value["schema"] != cls.SCHEMA:
            raise ContractError("runtime context schema is invalid")
        commit = _text(value["sourceCommit"], field="source commit", pattern=_SHA_40)
        account = _account(value["account"])
        region = _region(value["region"])
        runtime_id = _text(value["runtimeId"], field="runtime ID", pattern=_RUNTIME_ID)
        endpoint_id = _text(
            value["runtimeEndpointId"], field="runtime endpoint ID", pattern=_RUNTIME_ID
        )
        endpoint_name = _text(value["runtimeEndpointName"], field="runtime endpoint name")
        if endpoint_name != f"release_{commit}":
            raise ContractError("runtime endpoint name is not commit-bound")
        version = _text(
            value["runtimeVersion"], field="runtime version", pattern=_RUNTIME_VERSION
        )
        runtime_arn = _text(value["runtimeArn"], field="runtime ARN")
        arn_pattern = re.compile(
            rf"arn:aws:bedrock-agentcore:{re.escape(region)}:{re.escape(account)}:agent/"
            r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
            r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:[1-9][0-9]{0,4}"
        )
        if arn_pattern.fullmatch(runtime_arn) is None:
            raise ContractError("runtime ARN crosses its account or region")
        if runtime_arn.rsplit(":", 1)[-1] != version:
            raise ContractError("runtime ARN and version differ")
        digest_match = re.search(r"@(sha256:[0-9a-f]{64})$", str(value["runtimeImageUri"]))
        if digest_match is None:
            raise ContractError("runtime image URI must be immutable")
        image_uri = _image_uri(account, region, digest_match.group(1), value["runtimeImageUri"])
        return cls(
            source_commit=commit,
            account=account,
            region=region,
            runtime_id=runtime_id,
            runtime_endpoint_id=endpoint_id,
            runtime_endpoint_name=endpoint_name,
            runtime_arn=runtime_arn,
            runtime_version=version,
            runtime_image_uri=image_uri,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "RuntimeContextV3":
        return cls.from_mapping(parse_canonical_object(payload))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "sourceCommit": self.source_commit,
            "account": self.account,
            "region": self.region,
            "runtimeId": self.runtime_id,
            "runtimeEndpointId": self.runtime_endpoint_id,
            "runtimeEndpointName": self.runtime_endpoint_name,
            "runtimeArn": self.runtime_arn,
            "runtimeVersion": self.runtime_version,
            "runtimeImageUri": self.runtime_image_uri,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())


@dataclass(frozen=True, slots=True)
class RuntimeImageEvidence:
    SCHEMA: ClassVar[str] = "personal-operator.runtime-image-evidence.v1"
    FIELDS: ClassVar[set[str]] = {
        "schema",
        "sourceCommit",
        "sourceTree",
        "account",
        "region",
        "repositoryName",
        "commitTag",
        "imageDigest",
        "imageUri",
        "imageSizeBytes",
        "scanStatus",
        "criticalFindings",
        "highFindings",
        "sbomSha256",
        "provenanceSha256",
        "signingProfileArn",
        "signatureStatus",
    }

    source_commit: str
    source_tree: str
    account: str
    region: str
    repository_name: str
    commit_tag: str
    image_digest: str
    image_uri: str
    image_size_bytes: int
    scan_status: str
    critical_findings: int
    high_findings: int
    sbom_sha256: str
    provenance_sha256: str
    signing_profile_arn: str
    signature_status: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RuntimeImageEvidence":
        value = _exact_object(raw, cls.FIELDS, label="runtime image evidence")
        if value["schema"] != cls.SCHEMA:
            raise ContractError("runtime image evidence schema is invalid")
        commit = _text(value["sourceCommit"], field="source commit", pattern=_SHA_40)
        tree = _text(value["sourceTree"], field="source tree", pattern=_SHA_40)
        account = _account(value["account"])
        region = _region(value["region"])
        repository = _text(value["repositoryName"], field="repository name")
        if repository != RUNTIME_REPOSITORY:
            raise ContractError("runtime image repository is not canonical")
        tag = _text(value["commitTag"], field="commit tag")
        if tag != f"commit-{commit}":
            raise ContractError("runtime image commit tag is not exact")
        digest = _text(value["imageDigest"], field="image digest", pattern=_DIGEST)
        image_uri = _image_uri(account, region, digest, value["imageUri"])
        image_size = _count(value["imageSizeBytes"], field="image size", minimum=1)
        scan_status = _text(value["scanStatus"], field="scan status")
        if scan_status != "COMPLETE":
            raise ContractError("runtime image scan is not complete")
        critical = _count(value["criticalFindings"], field="critical findings")
        high = _count(value["highFindings"], field="high findings")
        if critical or high:
            raise ContractError("runtime image has unreviewed findings")
        sbom = _text(value["sbomSha256"], field="SBOM digest", pattern=_SHA_64)
        provenance = _text(
            value["provenanceSha256"], field="provenance digest", pattern=_SHA_64
        )
        profile = _text(value["signingProfileArn"], field="signing profile ARN")
        expected_profile = (
            f"arn:aws:signer:{region}:{account}:/signing-profiles/"
            f"{_SIGNING_PROFILE_NAME}"
        )
        if profile != expected_profile:
            raise ContractError("signing profile ARN crosses its account or region")
        signature_status = _text(value["signatureStatus"], field="signature status")
        if signature_status != "SIGNED":
            raise ContractError("runtime image signature is not complete")
        return cls(
            source_commit=commit,
            source_tree=tree,
            account=account,
            region=region,
            repository_name=repository,
            commit_tag=tag,
            image_digest=digest,
            image_uri=image_uri,
            image_size_bytes=image_size,
            scan_status=scan_status,
            critical_findings=critical,
            high_findings=high,
            sbom_sha256=sbom,
            provenance_sha256=provenance,
            signing_profile_arn=profile,
            signature_status=signature_status,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "RuntimeImageEvidence":
        return cls.from_mapping(parse_canonical_object(payload))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "sourceCommit": self.source_commit,
            "sourceTree": self.source_tree,
            "account": self.account,
            "region": self.region,
            "repositoryName": self.repository_name,
            "commitTag": self.commit_tag,
            "imageDigest": self.image_digest,
            "imageUri": self.image_uri,
            "imageSizeBytes": self.image_size_bytes,
            "scanStatus": self.scan_status,
            "criticalFindings": self.critical_findings,
            "highFindings": self.high_findings,
            "sbomSha256": self.sbom_sha256,
            "provenanceSha256": self.provenance_sha256,
            "signingProfileArn": self.signing_profile_arn,
            "signatureStatus": self.signature_status,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: str
    sha256: str
    size: int

    def to_mapping(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True, slots=True)
class AssetFile(SourceFile):
    mode: str

    def to_mapping(self) -> dict[str, Any]:
        return {**SourceFile.to_mapping(self), "mode": self.mode}


@dataclass(frozen=True, slots=True)
class Dependency:
    name: str
    version: str

    def to_mapping(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version}


def _source_files(raw: Any, *, asset: bool) -> tuple[SourceFile, ...] | tuple[AssetFile, ...]:
    label = "file inventory" if asset else "source inventory"
    if not isinstance(raw, list) or not raw:
        raise ContractError(f"trusted Lambda {label} is empty")
    result: list[SourceFile] | list[AssetFile] = []
    seen: set[str] = set()
    expected = {"path", "sha256", "size"} | ({"mode"} if asset else set())
    for item in raw:
        value = _exact_object(item, expected, label=label)
        path = _safe_path(value["path"], field=f"{label} path")
        if path in seen:
            raise ContractError(f"trusted Lambda {label} contains duplicate paths")
        seen.add(path)
        digest = _text(value["sha256"], field=f"{label} digest", pattern=_SHA_64)
        size = _count(value["size"], field=f"{label} size")
        if asset:
            mode = _text(value["mode"], field="file mode")
            if mode not in {"0644", "0755"}:
                raise ContractError("trusted Lambda file mode is invalid")
            result.append(AssetFile(path, digest, size, mode))
        else:
            result.append(SourceFile(path, digest, size))
    if [item.path for item in result] != sorted(item.path for item in result):
        raise ContractError(f"trusted Lambda {label} is not canonical")
    return tuple(result)


def _dependencies(raw: Any) -> tuple[Dependency, ...]:
    if not isinstance(raw, list) or not raw:
        raise ContractError("trusted Lambda dependency inventory is empty")
    result: list[Dependency] = []
    for item in raw:
        value = _exact_object(item, {"name", "version"}, label="dependency")
        name = _text(value["name"], field="dependency name")
        version = _text(value["version"], field="dependency version")
        if not name or not version:
            raise ContractError("trusted Lambda dependency is empty")
        result.append(Dependency(name, version))
    expected = sorted(result, key=lambda item: (item.name.casefold(), item.version))
    if result != expected or len({item.name.casefold() for item in result}) != len(result):
        raise ContractError("trusted Lambda dependency inventory is not canonical")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class TrustedLambdaAssetV2:
    SCHEMA: ClassVar[str] = "personal-operator.trusted-lambda-asset.v2"
    FIELDS: ClassVar[set[str]] = {
        "schema",
        "sourceCommit",
        "sourceTree",
        "platform",
        "architecture",
        "python",
        "builderImage",
        "builderImageId",
        "requirementsMode",
        "requirementsSha256",
        "requirementsInputSha256",
        "sourceDateEpoch",
        "payloadBytes",
        "archiveName",
        "archiveBytes",
        "archiveSha256",
        "dependencies",
        "sourceFiles",
        "files",
    }

    source_commit: str
    source_tree: str
    platform: str
    architecture: str
    python: str
    builder_image: str
    builder_image_id: str
    requirements_mode: str
    requirements_sha256: str
    requirements_input_sha256: str
    source_date_epoch: int
    payload_bytes: int
    archive_name: str
    archive_bytes: int
    archive_sha256: str
    dependencies: tuple[Dependency, ...]
    source_files: tuple[SourceFile, ...]
    files: tuple[AssetFile, ...]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TrustedLambdaAssetV2":
        value = _exact_object(raw, cls.FIELDS, label="trusted Lambda asset")
        if value["schema"] != cls.SCHEMA:
            raise ContractError("trusted Lambda asset schema is invalid")
        commit = _text(value["sourceCommit"], field="source commit", pattern=_SHA_40)
        tree = _text(value["sourceTree"], field="source tree", pattern=_SHA_40)
        platform = _text(value["platform"], field="platform")
        architecture = _text(value["architecture"], field="architecture")
        if platform != "linux/arm64" or architecture != "arm64":
            raise ContractError("trusted Lambda architecture is not linux/arm64")
        python = _text(value["python"], field="Python version")
        if python != "3.13":
            raise ContractError("trusted Lambda Python version is not 3.13")
        builder = _text(value["builderImage"], field="builder image", pattern=_BUILDER_IMAGE)
        builder_id = _text(
            value["builderImageId"], field="builder image ID", pattern=_BUILDER_IMAGE_ID
        )
        mode = _text(value["requirementsMode"], field="requirements mode")
        if mode != "sha256-locked":
            raise ContractError("trusted Lambda requirements are not hash locked")
        requirements = _text(
            value["requirementsSha256"], field="requirements digest", pattern=_SHA_64
        )
        requirements_input = _text(
            value["requirementsInputSha256"],
            field="requirements input digest",
            pattern=_SHA_64,
        )
        epoch = _count(value["sourceDateEpoch"], field="source date epoch")
        if epoch != 0:
            raise ContractError("trusted Lambda source date epoch must be zero")
        payload_bytes = _count(value["payloadBytes"], field="payload bytes", minimum=1)
        archive_name = _safe_path(value["archiveName"], field="archive name")
        if archive_name != "trusted-lambda.zip":
            raise ContractError("trusted Lambda archive name is not canonical")
        archive_bytes = _count(value["archiveBytes"], field="archive bytes", minimum=1)
        archive_sha = _text(
            value["archiveSha256"], field="archive digest", pattern=_SHA_64
        )
        dependencies = _dependencies(value["dependencies"])
        source_files = _source_files(value["sourceFiles"], asset=False)
        files = _source_files(value["files"], asset=True)
        assert isinstance(source_files, tuple) and isinstance(files, tuple)
        if payload_bytes != sum(item.size for item in files):
            raise ContractError("trusted Lambda payload size differs from inventory")
        actual = {item.path: item for item in files}
        for source_file in source_files:
            packaged = actual.get(source_file.path)
            if packaged is None or (packaged.sha256, packaged.size) != (
                source_file.sha256,
                source_file.size,
            ):
                raise ContractError("trusted Lambda source differs from file inventory")
        return cls(
            commit,
            tree,
            platform,
            architecture,
            python,
            builder,
            builder_id,
            mode,
            requirements,
            requirements_input,
            epoch,
            payload_bytes,
            archive_name,
            archive_bytes,
            archive_sha,
            dependencies,
            source_files,  # type: ignore[arg-type]
            files,  # type: ignore[arg-type]
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "TrustedLambdaAssetV2":
        return cls.from_mapping(parse_canonical_object(payload))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "sourceCommit": self.source_commit,
            "sourceTree": self.source_tree,
            "platform": self.platform,
            "architecture": self.architecture,
            "python": self.python,
            "builderImage": self.builder_image,
            "builderImageId": self.builder_image_id,
            "requirementsMode": self.requirements_mode,
            "requirementsSha256": self.requirements_sha256,
            "requirementsInputSha256": self.requirements_input_sha256,
            "sourceDateEpoch": self.source_date_epoch,
            "payloadBytes": self.payload_bytes,
            "archiveName": self.archive_name,
            "archiveBytes": self.archive_bytes,
            "archiveSha256": self.archive_sha256,
            "dependencies": [item.to_mapping() for item in self.dependencies],
            "sourceFiles": [item.to_mapping() for item in self.source_files],
            "files": [item.to_mapping() for item in self.files],
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())


LINEAR_TRANSACTION_STATES = (
    "NEW",
    "PREFLIGHTED",
    "FOUNDATION_READY",
    "IMAGE_PUBLISHED",
    "RUNTIME_READY",
    "ENDPOINT_READY",
    "CONTEXT_WRITTEN",
    "CONSUMER_CHANGESETS_READY",
    "CONSUMERS_APPLIED",
    "VERIFIED",
)
TRANSACTION_STATES = frozenset((*LINEAR_TRANSACTION_STATES, "UNCERTAIN", "ROLLED_BACK"))


@dataclass(frozen=True, slots=True)
class StagingTransactionV1:
    SCHEMA: ClassVar[str] = "personal-operator.staging-transaction.v1"
    FIELDS: ClassVar[set[str]] = {
        "schema",
        "transactionId",
        "sourceCommit",
        "sourceTree",
        "account",
        "region",
        "state",
        "lastStableState",
        "revision",
        "runtimeImageDigest",
        "runtimeId",
        "runtimeVersion",
        "runtimeEndpointName",
        "runtimeContextSha256",
        "rollbackReference",
        "uncertainPhase",
    }

    transaction_id: str
    source_commit: str
    source_tree: str
    account: str
    region: str
    state: str
    last_stable_state: str
    revision: int
    runtime_image_digest: str
    runtime_id: str
    runtime_version: str
    runtime_endpoint_name: str
    runtime_context_sha256: str
    rollback_reference: str
    uncertain_phase: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "StagingTransactionV1":
        value = _exact_object(raw, cls.FIELDS, label="staging transaction")
        if value["schema"] != cls.SCHEMA:
            raise ContractError("staging transaction schema is invalid")
        commit = _text(value["sourceCommit"], field="source commit", pattern=_SHA_40)
        tree = _text(value["sourceTree"], field="source tree", pattern=_SHA_40)
        transaction_id = _text(value["transactionId"], field="transaction ID")
        if transaction_id != f"release_{commit}":
            raise ContractError("staging transaction ID is not commit-bound")
        account = _account(value["account"])
        region = _region(value["region"])
        state = _text(value["state"], field="state")
        if state not in TRANSACTION_STATES:
            raise ContractError("staging transaction state is unknown")
        stable = _text(value["lastStableState"], field="last stable state")
        if stable not in LINEAR_TRANSACTION_STATES:
            raise ContractError("staging transaction last stable state is unknown")
        revision = _count(value["revision"], field="revision")
        image_digest = _text(value["runtimeImageDigest"], field="runtime image digest")
        if image_digest and _DIGEST.fullmatch(image_digest) is None:
            raise ContractError("runtime image digest is invalid")
        runtime_id = _text(value["runtimeId"], field="runtime ID")
        if runtime_id and _RUNTIME_ID.fullmatch(runtime_id) is None:
            raise ContractError("runtime ID is invalid")
        runtime_version = _text(value["runtimeVersion"], field="runtime version")
        if runtime_version and _RUNTIME_VERSION.fullmatch(runtime_version) is None:
            raise ContractError("runtime version is invalid")
        endpoint_name = _text(value["runtimeEndpointName"], field="runtime endpoint name")
        if endpoint_name != f"release_{commit}":
            raise ContractError("runtime endpoint name is not commit-bound")
        context_digest = _text(value["runtimeContextSha256"], field="runtime context digest")
        if context_digest and _SHA_64.fullmatch(context_digest) is None:
            raise ContractError("runtime context digest is invalid")
        rollback = _rollback_reference(
            value["rollbackReference"], account=account, region=region, commit=commit
        )
        uncertain_phase = _text(value["uncertainPhase"], field="uncertain phase")
        if state == "NEW":
            if stable != "NEW" or revision != 0 or any(
                (image_digest, runtime_id, runtime_version, context_digest, rollback, uncertain_phase)
            ):
                raise ContractError("NEW staging transaction contains later-phase evidence")
        elif state == "UNCERTAIN":
            if not uncertain_phase or revision < 1:
                raise ContractError("UNCERTAIN staging transaction lacks its phase")
            stable_index = LINEAR_TRANSACTION_STATES.index(stable)
            expected_phase = (
                LINEAR_TRANSACTION_STATES[stable_index + 1]
                if stable_index + 1 < len(LINEAR_TRANSACTION_STATES)
                else None
            )
            if uncertain_phase != expected_phase:
                raise ContractError("UNCERTAIN phase is not the legal next state")
        elif uncertain_phase:
            raise ContractError("uncertain phase is set outside UNCERTAIN")
        if state in LINEAR_TRANSACTION_STATES and state != stable:
            raise ContractError("linear transaction state and last stable state differ")
        evidence_state = stable if state in {"UNCERTAIN", "ROLLED_BACK"} else state
        evidence_index = LINEAR_TRANSACTION_STATES.index(evidence_state)
        image_index = LINEAR_TRANSACTION_STATES.index("IMAGE_PUBLISHED")
        runtime_index = LINEAR_TRANSACTION_STATES.index("RUNTIME_READY")
        context_index = LINEAR_TRANSACTION_STATES.index("CONTEXT_WRITTEN")
        if evidence_index >= image_index and not image_digest:
            raise ContractError("runtime image evidence is missing")
        if evidence_index < image_index and image_digest:
            raise ContractError("runtime image evidence appears before publication")
        if evidence_index >= runtime_index and not (runtime_id and runtime_version):
            raise ContractError("runtime identity evidence is missing")
        if evidence_index < runtime_index and (runtime_id or runtime_version):
            raise ContractError("runtime identity appears before runtime readiness")
        if evidence_index >= context_index and not context_digest:
            raise ContractError("runtime context evidence is missing")
        if evidence_index < context_index and context_digest:
            raise ContractError("runtime context evidence appears before context publication")
        foundation_index = LINEAR_TRANSACTION_STATES.index("FOUNDATION_READY")
        if (evidence_index >= foundation_index or state in {"UNCERTAIN", "ROLLED_BACK"}) and not rollback:
            raise ContractError("exact rollback reference is missing")
        return cls(
            transaction_id,
            commit,
            tree,
            account,
            region,
            state,
            stable,
            revision,
            image_digest,
            runtime_id,
            runtime_version,
            endpoint_name,
            context_digest,
            rollback,
            uncertain_phase,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "StagingTransactionV1":
        return cls.from_mapping(parse_canonical_object(payload))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "transactionId": self.transaction_id,
            "sourceCommit": self.source_commit,
            "sourceTree": self.source_tree,
            "account": self.account,
            "region": self.region,
            "state": self.state,
            "lastStableState": self.last_stable_state,
            "revision": self.revision,
            "runtimeImageDigest": self.runtime_image_digest,
            "runtimeId": self.runtime_id,
            "runtimeVersion": self.runtime_version,
            "runtimeEndpointName": self.runtime_endpoint_name,
            "runtimeContextSha256": self.runtime_context_sha256,
            "rollbackReference": self.rollback_reference,
            "uncertainPhase": self.uncertain_phase,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())


ReleaseContract = (
    RuntimeContextV3
    | RuntimeImageEvidence
    | TrustedLambdaAssetV2
    | StagingTransactionV1
)


def parse_release_contract(payload: bytes) -> ReleaseContract:
    """Parse and fully validate any supported canonical release artifact."""

    value = parse_canonical_object(payload)
    schema = value.get("schema")
    parsers = {
        RuntimeContextV3.SCHEMA: RuntimeContextV3.from_mapping,
        RuntimeImageEvidence.SCHEMA: RuntimeImageEvidence.from_mapping,
        TrustedLambdaAssetV2.SCHEMA: TrustedLambdaAssetV2.from_mapping,
        StagingTransactionV1.SCHEMA: StagingTransactionV1.from_mapping,
    }
    parser = parsers.get(schema)
    if parser is None:
        raise ContractError("release artifact schema is unknown")
    return parser(value)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short write while persisting release artifact")
        offset += written


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_regular_bytes(path: Path) -> bytes:
    """Read one bounded regular file without following a final symlink."""

    target = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as error:
        raise ContractError(f"release artifact is not a regular file: {target}") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ContractError(f"release artifact is not a regular file: {target}")
        chunks: list[bytes] = []
        remaining = MAX_CONTRACT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_CONTRACT_BYTES:
            raise ContractError("release artifact exceeds the byte limit")
        return payload
    finally:
        os.close(descriptor)


def write_new_contract(path: Path, contract: CanonicalContract) -> None:
    """Atomically create one canonical artifact, refusing any existing target."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = contract.to_bytes()
    # Re-parse before persistence so a custom or reconstructed implementation
    # cannot bypass the semantic boundary merely by satisfying the protocol.
    parse_release_contract(payload)
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        try:
            os.link(temporary, target)
        except FileExistsError as error:
            raise ContractError(f"release artifact already exists: {target}") from error
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_replace_contract(
    path: Path,
    expected_payload: bytes,
    contract: CanonicalContract,
) -> None:
    """Durably compare-and-swap one canonical artifact under a file lock."""

    target = Path(path)
    payload = contract.to_bytes()
    parse_release_contract(payload)
    lock_path = target.parent / f".{target.name}.lock"
    lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    temporary: Path | None = None
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        if read_regular_bytes(target) != expected_payload:
            raise ContractError("release artifact changed concurrently")
        temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, target)
        temporary = None
        _fsync_directory(target.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lock_descriptor)
