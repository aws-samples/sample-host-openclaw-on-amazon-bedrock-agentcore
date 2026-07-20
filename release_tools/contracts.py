"""Strict, canonical, AWS-free contracts for immutable staging releases."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import struct
import uuid
import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Iterator, Mapping, Protocol, Sequence, TypeVar


REQUIRED_REGION = "eu-west-1"
RUNTIME_REPOSITORY = "personal-operator/bridge"
MAX_CONTRACT_BYTES = 4 * 1024 * 1024
FOUNDATION_RELEASE_STACKS = (
    "OpenClawVpc",
    "OpenClawSecurity",
    "OpenClawGuardrails",
    "PersonalOperatorCapabilities",
    "OpenClawAgentCore",
    "OpenClawObservability",
)
CONSUMER_RELEASE_STACKS = (
    "OpenClawRouter",
    "PersonalOperatorWeb",
    "OpenClawCron",
    "PersonalOperatorScheduler",
)

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
_SUBNET_ID = re.compile(r"subnet-(?:[0-9a-f]{8}|[0-9a-f]{17})")
_SECURITY_GROUP_ID = re.compile(r"sg-(?:[0-9a-f]{8}|[0-9a-f]{17})")

_REQUIRED_RUNTIME_ENVIRONMENT = frozenset(
    {
        "AWS_DEFAULT_REGION",
        "AWS_REGION",
        "BEDROCK_MODEL_ID",
        "CAPABILITY_GATEWAY_FUNCTION_ARN",
        "DISABLE_ADOT_OBSERVABILITY",
        "S3_USER_FILES_BUCKET",
        "WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME",
        "WORKSPACE_SYNC_INTERVAL_MS",
    }
)
_OPTIONAL_RUNTIME_ENVIRONMENT = frozenset(
    {
        "BEDROCK_GUARDRAIL_ID",
        "BEDROCK_GUARDRAIL_VERSION",
        "SUBAGENT_BEDROCK_MODEL_ID",
    }
)


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


def _exact_digest_inventory(
    value: Any,
    expected_names: tuple[str, ...],
    *,
    label: str,
) -> tuple[tuple[str, str], ...]:
    inventory = _exact_object(value, set(expected_names), label=label)
    if any(
        not isinstance(inventory[name], str)
        or _SHA_64.fullmatch(inventory[name]) is None
        for name in expected_names
    ):
        raise ContractError(f"{label} contains a malformed digest")
    return tuple((name, inventory[name]) for name in expected_names)


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


_CLOUDFORMATION_OPAQUE_ID = r"[A-Za-z0-9][A-Za-z0-9._+=,@-]{0,255}"
_CLOUDFORMATION_STACK_ID = re.compile(
    rf"arn:aws:cloudformation:[a-z0-9-]+:[0-9]{{12}}:stack/"
    rf"[A-Za-z][A-Za-z0-9-]{{0,127}}/{_CLOUDFORMATION_OPAQUE_ID}"
)
_CLOUDFORMATION_CHANGE_SET_ID = re.compile(
    rf"arn:aws:cloudformation:[a-z0-9-]+:[0-9]{{12}}:changeSet/"
    rf"[A-Za-z0-9][A-Za-z0-9-]{{0,127}}/{_CLOUDFORMATION_OPAQUE_ID}"
)


def _optional_cloudformation_id(
    value: Any,
    *,
    change_set: bool,
    field: str,
) -> str:
    identifier = _text(value, field=field)
    pattern = (
        _CLOUDFORMATION_CHANGE_SET_ID
        if change_set
        else _CLOUDFORMATION_STACK_ID
    )
    if identifier and pattern.fullmatch(identifier) is None:
        raise ContractError(f"{field} is invalid")
    return identifier


def _cloudformation_stack_id(
    value: Any,
    *,
    account: str,
    region: str,
    stack_name: str,
    field: str,
) -> str:
    stack_id = _text(value, field=field)
    pattern = re.compile(
        rf"arn:aws:cloudformation:{re.escape(region)}:{re.escape(account)}:"
        rf"stack/{re.escape(stack_name)}/{_CLOUDFORMATION_OPAQUE_ID}"
    )
    if pattern.fullmatch(stack_id) is None:
        raise ContractError(f"{field} crosses its exact stack ID subject")
    return stack_id


def _cloudformation_change_set_id(
    value: Any,
    *,
    account: str,
    region: str,
    source_commit: str,
    field: str,
) -> str:
    change_set_id = _text(value, field=field)
    pattern = re.compile(
        rf"arn:aws:cloudformation:{re.escape(region)}:{re.escape(account)}:"
        rf"changeSet/release-{re.escape(source_commit)}/"
        rf"{_CLOUDFORMATION_OPAQUE_ID}"
    )
    if pattern.fullmatch(change_set_id) is None:
        raise ContractError(f"{field} crosses its exact change-set ID subject")
    return change_set_id


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


def expected_execution_role_arn(account: str, region: str) -> str:
    """Return the only execution role that can satisfy this release contract."""

    return (
        f"arn:aws:iam::{account}:role/"
        f"openclaw-agentcore-execution-role-{region}"
    )


@dataclass(frozen=True, slots=True)
class RuntimeConfigurationV1:
    """Immutable canonical subset of every trust-bearing AgentCore setting."""

    FIELDS: ClassVar[set[str]] = {
        "agentRuntimeArtifact",
        "authorizerConfiguration",
        "environmentVariables",
        "filesystemConfigurations",
        "lifecycleConfiguration",
        "metadataConfiguration",
        "networkConfiguration",
        "protocolConfiguration",
        "requestHeaderConfiguration",
    }

    runtime_image_uri: str
    subnet_ids: tuple[str, ...]
    security_group_ids: tuple[str, ...]
    environment_variables: tuple[tuple[str, str], ...]
    idle_runtime_session_timeout: int
    max_lifetime: int

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        runtime_image_uri: str,
        account: str,
        region: str,
    ) -> "RuntimeConfigurationV1":
        account = _account(account)
        region = _region(region)
        value = _exact_object(raw, cls.FIELDS, label="runtime configuration")

        artifact = _exact_object(
            value["agentRuntimeArtifact"],
            {"containerConfiguration"},
            label="runtime artifact",
        )
        container = _exact_object(
            artifact["containerConfiguration"],
            {"containerUri"},
            label="runtime container configuration",
        )
        if container["containerUri"] != runtime_image_uri:
            raise ContractError("runtime configuration image differs")
        if value["authorizerConfiguration"] != {}:
            raise ContractError("runtime authorizer configuration is not disabled")
        if value["requestHeaderConfiguration"] != {}:
            raise ContractError(
                "runtime request header configuration is not disabled"
            )
        if value["metadataConfiguration"] != {"requireMMDSV2": True}:
            raise ContractError("runtime metadata configuration is not hardened")

        network = _exact_object(
            value["networkConfiguration"],
            {"networkMode", "networkModeConfig"},
            label="runtime network configuration",
        )
        if network["networkMode"] != "VPC":
            raise ContractError("runtime network configuration is not VPC")
        vpc = _exact_object(
            network["networkModeConfig"],
            {"securityGroups", "subnets"},
            label="runtime VPC configuration",
        )
        subnet_ids = cls._identifiers(
            vpc["subnets"], pattern=_SUBNET_ID, label="runtime subnet"
        )
        security_group_ids = cls._identifiers(
            vpc["securityGroups"],
            pattern=_SECURITY_GROUP_ID,
            label="runtime security group",
        )

        environment = value["environmentVariables"]
        if not isinstance(environment, Mapping):
            raise ContractError("runtime environment is malformed")
        keys = set(environment)
        allowed = _REQUIRED_RUNTIME_ENVIRONMENT | _OPTIONAL_RUNTIME_ENVIRONMENT
        if not _REQUIRED_RUNTIME_ENVIRONMENT.issubset(keys) or not keys <= allowed:
            raise ContractError("runtime environment contains missing or unreviewed fields")
        if any(
            not isinstance(key, str)
            or not isinstance(item, str)
            or not item
            for key, item in environment.items()
        ):
            raise ContractError("runtime environment contains an invalid value")
        if (
            environment["AWS_REGION"] != region
            or environment["AWS_DEFAULT_REGION"] != region
        ):
            raise ContractError("runtime environment region is not release-bound")
        if environment["DISABLE_ADOT_OBSERVABILITY"] != "true":
            raise ContractError("runtime observability suppression is not enabled")
        expected_gateway_arn = (
            f"arn:aws:lambda:{region}:{account}:function:"
            "personal-operator-capability-gateway"
        )
        if environment["CAPABILITY_GATEWAY_FUNCTION_ARN"] != expected_gateway_arn:
            raise ContractError("runtime capability gateway crosses its exact subject")
        if re.fullmatch(r"[1-9][0-9]*", environment["WORKSPACE_SYNC_INTERVAL_MS"]) is None:
            raise ContractError("runtime environment sync interval is invalid")
        guardrail_fields = {
            "BEDROCK_GUARDRAIL_ID",
            "BEDROCK_GUARDRAIL_VERSION",
        }
        if keys & guardrail_fields and not guardrail_fields <= keys:
            raise ContractError("runtime environment guardrail fields are incomplete")
        if (
            "BEDROCK_GUARDRAIL_VERSION" in environment
            and re.fullmatch(
                r"(?:DRAFT|[1-9][0-9]{0,7})",
                environment["BEDROCK_GUARDRAIL_VERSION"],
            )
            is None
        ):
            raise ContractError("runtime environment guardrail version is invalid")

        if value["filesystemConfigurations"] != [
            {"sessionStorage": {"mountPath": "/mnt/workspace"}}
        ]:
            raise ContractError("runtime filesystem configuration is not canonical")
        if value["protocolConfiguration"] != {"serverProtocol": "HTTP"}:
            raise ContractError("runtime protocol configuration is not HTTP")

        lifecycle = _exact_object(
            value["lifecycleConfiguration"],
            {"idleRuntimeSessionTimeout", "maxLifetime"},
            label="runtime lifecycle configuration",
        )
        idle_timeout = _count(
            lifecycle["idleRuntimeSessionTimeout"],
            field="runtime idle timeout",
            minimum=1,
        )
        max_lifetime = _count(
            lifecycle["maxLifetime"],
            field="runtime maximum lifetime",
            minimum=1,
        )
        if max_lifetime < idle_timeout:
            raise ContractError("runtime lifecycle maximum is below its idle timeout")

        return cls(
            runtime_image_uri=runtime_image_uri,
            subnet_ids=subnet_ids,
            security_group_ids=security_group_ids,
            environment_variables=tuple(sorted(environment.items())),
            idle_runtime_session_timeout=idle_timeout,
            max_lifetime=max_lifetime,
        )

    @staticmethod
    def _identifiers(
        raw: Any,
        *,
        pattern: re.Pattern[str],
        label: str,
    ) -> tuple[str, ...]:
        if (
            not isinstance(raw, list)
            or not raw
            or any(
                not isinstance(value, str) or pattern.fullmatch(value) is None
                for value in raw
            )
            or raw != sorted(raw)
            or len(set(raw)) != len(raw)
        ):
            raise ContractError(f"{label} inventory is not exact and canonical")
        return tuple(raw)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "agentRuntimeArtifact": {
                "containerConfiguration": {"containerUri": self.runtime_image_uri}
            },
            "authorizerConfiguration": {},
            "environmentVariables": dict(self.environment_variables),
            "filesystemConfigurations": [
                {"sessionStorage": {"mountPath": "/mnt/workspace"}}
            ],
            "lifecycleConfiguration": {
                "idleRuntimeSessionTimeout": self.idle_runtime_session_timeout,
                "maxLifetime": self.max_lifetime,
            },
            "networkConfiguration": {
                "networkMode": "VPC",
                "networkModeConfig": {
                    "securityGroups": list(self.security_group_ids),
                    "subnets": list(self.subnet_ids),
                },
            },
            "metadataConfiguration": {"requireMMDSV2": True},
            "protocolConfiguration": {"serverProtocol": "HTTP"},
            "requestHeaderConfiguration": {},
        }

    def digest_for_role(self, execution_role_arn: str) -> str:
        return hashlib.sha256(
            canonical_json_bytes(
                {
                    "executionRoleArn": execution_role_arn,
                    "runtimeConfiguration": self.to_mapping(),
                }
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class ProductionObservationConfigV1:
    """Reviewed, credential-free inputs for authoritative release observation."""

    SCHEMA: ClassVar[str] = "personal-operator.production-observation-config.v1"
    FIELDS: ClassVar[set[str]] = {
        "schema",
        "sourceCommit",
        "sourceTree",
        "account",
        "region",
        "buildContext",
        "builderId",
        "builderInputs",
        "runtimeSubnetIds",
        "runtimeSecurityGroupIds",
        "runtimeEnvironmentVariables",
        "runtimeIdleSessionTimeout",
        "runtimeMaxLifetime",
        "foundationStackTemplateParameterDigests",
        "runtimeStackTemplateParameterDigest",
        "consumerStackTemplateParameterDigests",
        "consumerChangeSetContentDigests",
        "foundationStackRequestDigests",
        "runtimeStackRequestDigest",
        "consumerStackRequestDigests",
        "evidenceRuntimeSha256",
    }

    source_commit: str
    source_tree: str
    account: str
    region: str
    build_context: str
    builder_id: str
    builder_inputs: tuple[str, ...]
    runtime_subnet_ids: tuple[str, ...]
    runtime_security_group_ids: tuple[str, ...]
    runtime_environment_variables: tuple[tuple[str, str], ...]
    runtime_idle_session_timeout: int
    runtime_max_lifetime: int
    foundation_stack_template_parameter_digests: tuple[tuple[str, str], ...]
    runtime_stack_template_parameter_digest: str
    consumer_stack_template_parameter_digests: tuple[tuple[str, str], ...]
    consumer_change_set_content_digests: tuple[tuple[str, str], ...]
    foundation_stack_request_digests: tuple[tuple[str, str], ...]
    runtime_stack_request_digest: str
    consumer_stack_request_digests: tuple[tuple[str, str], ...]
    evidence_runtime_sha256: str

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "ProductionObservationConfigV1":
        value = _exact_object(
            raw,
            cls.FIELDS,
            label="production observation config",
        )
        if value["schema"] != cls.SCHEMA:
            raise ContractError("production observation config schema is invalid")
        commit = _text(
            value["sourceCommit"],
            field="source commit",
            pattern=_SHA_40,
        )
        tree = _text(
            value["sourceTree"],
            field="source tree",
            pattern=_SHA_40,
        )
        account = _account(value["account"])
        region = _region(value["region"])
        build_context = _safe_path(value["buildContext"], field="build context")
        if build_context == "." or len(build_context) > 256:
            raise ContractError("build context is invalid")
        builder_id = _text(value["builderId"], field="builder ID")
        if (
            len(builder_id) > 512
            or re.fullmatch(r"https://[^\s]+", builder_id) is None
        ):
            raise ContractError("builder ID is invalid")

        raw_builder_inputs = value["builderInputs"]
        if (
            not isinstance(raw_builder_inputs, list)
            or not raw_builder_inputs
            or len(raw_builder_inputs) > 64
            or any(
                not isinstance(item, str) or _DIGEST.fullmatch(item) is None
                for item in raw_builder_inputs
            )
            or raw_builder_inputs != sorted(raw_builder_inputs)
            or len(set(raw_builder_inputs)) != len(raw_builder_inputs)
        ):
            raise ContractError(
                "builder input inventory is not exact and canonical"
            )

        runtime_image_uri = (
            f"{account}.dkr.ecr.{region}.amazonaws.com/"
            f"{RUNTIME_REPOSITORY}@sha256:{'0' * 64}"
        )
        runtime = RuntimeConfigurationV1.from_mapping(
            {
                "agentRuntimeArtifact": {
                    "containerConfiguration": {"containerUri": runtime_image_uri}
                },
                "authorizerConfiguration": {},
                "environmentVariables": value["runtimeEnvironmentVariables"],
                "filesystemConfigurations": [
                    {"sessionStorage": {"mountPath": "/mnt/workspace"}}
                ],
                "lifecycleConfiguration": {
                    "idleRuntimeSessionTimeout": value[
                        "runtimeIdleSessionTimeout"
                    ],
                    "maxLifetime": value["runtimeMaxLifetime"],
                },
                "networkConfiguration": {
                    "networkMode": "VPC",
                    "networkModeConfig": {
                        "securityGroups": value["runtimeSecurityGroupIds"],
                        "subnets": value["runtimeSubnetIds"],
                    },
                },
                "metadataConfiguration": {"requireMMDSV2": True},
                "protocolConfiguration": {"serverProtocol": "HTTP"},
                "requestHeaderConfiguration": {},
            },
            runtime_image_uri=runtime_image_uri,
            account=account,
            region=region,
        )
        foundation_stack_digests = _exact_digest_inventory(
            value["foundationStackTemplateParameterDigests"],
            FOUNDATION_RELEASE_STACKS,
            label="foundation stack digest inventory",
        )
        runtime_stack_digest = _text(
            value["runtimeStackTemplateParameterDigest"],
            field="runtime stack digest",
            pattern=_SHA_64,
        )
        consumer_stack_digests = _exact_digest_inventory(
            value["consumerStackTemplateParameterDigests"],
            CONSUMER_RELEASE_STACKS,
            label="consumer stack digest inventory",
        )
        consumer_change_set_digests = _exact_digest_inventory(
            value["consumerChangeSetContentDigests"],
            CONSUMER_RELEASE_STACKS,
            label="consumer change-set digest inventory",
        )
        foundation_stack_request_digests = _exact_digest_inventory(
            value["foundationStackRequestDigests"],
            FOUNDATION_RELEASE_STACKS,
            label="foundation stack request digest inventory",
        )
        runtime_stack_request_digest = _text(
            value["runtimeStackRequestDigest"],
            field="runtime stack request digest",
            pattern=_SHA_64,
        )
        consumer_stack_request_digests = _exact_digest_inventory(
            value["consumerStackRequestDigests"],
            CONSUMER_RELEASE_STACKS,
            label="consumer stack request digest inventory",
        )
        evidence_runtime_sha256 = _text(
            value["evidenceRuntimeSha256"],
            field="evidence runtime digest",
            pattern=_SHA_64,
        )
        return cls(
            source_commit=commit,
            source_tree=tree,
            account=account,
            region=region,
            build_context=build_context,
            builder_id=builder_id,
            builder_inputs=tuple(raw_builder_inputs),
            runtime_subnet_ids=runtime.subnet_ids,
            runtime_security_group_ids=runtime.security_group_ids,
            runtime_environment_variables=runtime.environment_variables,
            runtime_idle_session_timeout=runtime.idle_runtime_session_timeout,
            runtime_max_lifetime=runtime.max_lifetime,
            foundation_stack_template_parameter_digests=(
                foundation_stack_digests
            ),
            runtime_stack_template_parameter_digest=runtime_stack_digest,
            consumer_stack_template_parameter_digests=consumer_stack_digests,
            consumer_change_set_content_digests=consumer_change_set_digests,
            foundation_stack_request_digests=foundation_stack_request_digests,
            runtime_stack_request_digest=runtime_stack_request_digest,
            consumer_stack_request_digests=consumer_stack_request_digests,
            evidence_runtime_sha256=evidence_runtime_sha256,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ProductionObservationConfigV1":
        return cls.from_mapping(parse_canonical_object(payload))

    @property
    def execution_role_arn(self) -> str:
        return expected_execution_role_arn(self.account, self.region)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "sourceCommit": self.source_commit,
            "sourceTree": self.source_tree,
            "account": self.account,
            "region": self.region,
            "buildContext": self.build_context,
            "builderId": self.builder_id,
            "builderInputs": list(self.builder_inputs),
            "runtimeSubnetIds": list(self.runtime_subnet_ids),
            "runtimeSecurityGroupIds": list(self.runtime_security_group_ids),
            "runtimeEnvironmentVariables": dict(
                self.runtime_environment_variables
            ),
            "runtimeIdleSessionTimeout": self.runtime_idle_session_timeout,
            "runtimeMaxLifetime": self.runtime_max_lifetime,
            "foundationStackTemplateParameterDigests": dict(
                self.foundation_stack_template_parameter_digests
            ),
            "runtimeStackTemplateParameterDigest": (
                self.runtime_stack_template_parameter_digest
            ),
            "consumerStackTemplateParameterDigests": dict(
                self.consumer_stack_template_parameter_digests
            ),
            "consumerChangeSetContentDigests": dict(
                self.consumer_change_set_content_digests
            ),
            "foundationStackRequestDigests": dict(
                self.foundation_stack_request_digests
            ),
            "runtimeStackRequestDigest": self.runtime_stack_request_digest,
            "consumerStackRequestDigests": dict(
                self.consumer_stack_request_digests
            ),
            "evidenceRuntimeSha256": self.evidence_runtime_sha256,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


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
        "executionRoleArn",
        "runtimeConfiguration",
        "runtimeConfigurationSha256",
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
    execution_role_arn: str
    runtime_configuration: RuntimeConfigurationV1
    runtime_configuration_sha256: str

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
        role_arn = _text(value["executionRoleArn"], field="execution role ARN")
        if role_arn != expected_execution_role_arn(account, region):
            raise ContractError("execution role ARN is not deterministic for the release")
        configuration = RuntimeConfigurationV1.from_mapping(
            value["runtimeConfiguration"],
            runtime_image_uri=image_uri,
            account=account,
            region=region,
        )
        configuration_sha256 = _text(
            value["runtimeConfigurationSha256"],
            field="runtime configuration digest",
            pattern=_SHA_64,
        )
        if configuration_sha256 != configuration.digest_for_role(role_arn):
            raise ContractError("runtime configuration digest differs from exact bytes")
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
            execution_role_arn=role_arn,
            runtime_configuration=configuration,
            runtime_configuration_sha256=configuration_sha256,
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
            "executionRoleArn": self.execution_role_arn,
            "runtimeConfiguration": self.runtime_configuration.to_mapping(),
            "runtimeConfigurationSha256": self.runtime_configuration_sha256,
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
        "consumerChangesetsSha256",
        "consumerApplicationSha256",
        "verificationSha256",
        "rollbackReference",
        "uncertainPhase",
        "uncertainOperationSha256",
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
    consumer_changesets_sha256: str
    consumer_application_sha256: str
    verification_sha256: str
    rollback_reference: str
    uncertain_phase: str
    uncertain_operation_sha256: str

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
        if state == "ROLLED_BACK" and stable != "VERIFIED":
            raise ContractError(
                "ROLLED_BACK staging transaction requires VERIFIED last stable state"
            )
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
        changesets_digest = _text(
            value["consumerChangesetsSha256"],
            field="consumer changesets digest",
        )
        if changesets_digest and _SHA_64.fullmatch(changesets_digest) is None:
            raise ContractError("consumer changesets digest is invalid")
        application_digest = _text(
            value["consumerApplicationSha256"],
            field="consumer application digest",
        )
        if application_digest and _SHA_64.fullmatch(application_digest) is None:
            raise ContractError("consumer application digest is invalid")
        verification_digest = _text(
            value["verificationSha256"],
            field="verification digest",
        )
        if verification_digest and _SHA_64.fullmatch(verification_digest) is None:
            raise ContractError("verification digest is invalid")
        rollback = _rollback_reference(
            value["rollbackReference"], account=account, region=region, commit=commit
        )
        uncertain_phase = _text(value["uncertainPhase"], field="uncertain phase")
        operation_digest = _text(
            value["uncertainOperationSha256"],
            field="uncertain operation digest",
        )
        if state != "UNCERTAIN" and operation_digest:
            raise ContractError(
                "uncertain operation digest is set outside UNCERTAIN"
            )
        if state == "NEW":
            if stable != "NEW" or revision != 0 or any(
                (
                    image_digest,
                    runtime_id,
                    runtime_version,
                    context_digest,
                    changesets_digest,
                    application_digest,
                    verification_digest,
                    rollback,
                    uncertain_phase,
                    operation_digest,
                )
            ):
                raise ContractError("NEW staging transaction contains later-phase evidence")
        elif state == "UNCERTAIN":
            if not uncertain_phase or revision < 1:
                raise ContractError("UNCERTAIN staging transaction lacks its phase")
            if _DIGEST.fullmatch(operation_digest) is None:
                raise ContractError(
                    "UNCERTAIN staging transaction lacks its exact operation digest"
                )
            if uncertain_phase == "ROLLBACK":
                if stable != "VERIFIED" or not rollback:
                    raise ContractError(
                        "UNCERTAIN rollback requires one verified transaction"
                    )
            else:
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
        phase_evidence = (
            (
                changesets_digest,
                "CONSUMER_CHANGESETS_READY",
                "consumer changesets",
            ),
            (
                application_digest,
                "CONSUMERS_APPLIED",
                "consumer application",
            ),
            (verification_digest, "VERIFIED", "verification"),
        )
        for digest, owner_state, label in phase_evidence:
            owner_index = LINEAR_TRANSACTION_STATES.index(owner_state)
            if evidence_index >= owner_index and not digest:
                raise ContractError(f"{label} evidence is missing")
            if evidence_index < owner_index and digest:
                raise ContractError(f"{label} evidence appears before its owned phase")
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
            changesets_digest,
            application_digest,
            verification_digest,
            rollback,
            uncertain_phase,
            operation_digest,
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
            "consumerChangesetsSha256": self.consumer_changesets_sha256,
            "consumerApplicationSha256": self.consumer_application_sha256,
            "verificationSha256": self.verification_sha256,
            "rollbackReference": self.rollback_reference,
            "uncertainPhase": self.uncertain_phase,
            "uncertainOperationSha256": self.uncertain_operation_sha256,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())


RELEASE_V2_PHASES = (
    "foundation",
    "image",
    "runtime",
    "endpoint",
    "context",
    "router-cron-cs",
    "router-cron",
    "scheduler-cs",
    "scheduler",
    "web-cs",
    "web",
    "verify",
)
RELEASE_V2_PHASE_STATES = {
    "foundation": "FOUNDATION_READY",
    "image": "IMAGE_PUBLISHED",
    "runtime": "RUNTIME_READY",
    "endpoint": "ENDPOINT_READY",
    "context": "CONTEXT_WRITTEN",
    "router-cron-cs": "ROUTER_CRON_CHANGESETS_READY",
    "router-cron": "ROUTER_CRON_APPLIED",
    "scheduler-cs": "SCHEDULER_CHANGESET_READY",
    "scheduler": "SCHEDULER_APPLIED",
    "web-cs": "WEB_CHANGESET_READY",
    "web": "WEB_APPLIED",
    "verify": "VERIFIED",
}
RELEASE_V2_LINEAR_STATES = (
    "NEW",
    "PREFLIGHTED",
    *(RELEASE_V2_PHASE_STATES[phase] for phase in RELEASE_V2_PHASES),
)
RELEASE_V2_TRANSACTION_STATES = frozenset(
    (*RELEASE_V2_LINEAR_STATES, "UNCERTAIN", "ABORTED_RETAINED", "ROLLED_BACK")
)
RELEASE_V2_STEP_KINDS = frozenset(
    {
        "BASELINE_OBSERVE",
        "BOOTSTRAP_STACK",
        "ASSET_PUBLISH",
        "AGENTCORE_HARDEN",
        "STACK_CREATE",
        "STACK_UPDATE",
        "IMAGE_PUBLISH",
        "IMAGE_OBSERVE",
        "RUNTIME_CONTEXT_WRITE",
        "CHANGESET_CREATE",
        "CHANGESET_EXECUTE",
        "VERIFY",
    }
)
_RELEASE_V2_MUTATION_KINDS = RELEASE_V2_STEP_KINDS - {
    "BASELINE_OBSERVE",
    "IMAGE_OBSERVE",
    "VERIFY",
}
_RELEASE_V2_PHASE_KINDS = {
    "foundation": frozenset(
        {"BASELINE_OBSERVE", "BOOTSTRAP_STACK", "ASSET_PUBLISH", "STACK_CREATE"}
    ),
    "image": frozenset({"IMAGE_PUBLISH", "IMAGE_OBSERVE"}),
    "runtime": frozenset({"STACK_UPDATE", "AGENTCORE_HARDEN"}),
    "endpoint": frozenset({"STACK_UPDATE"}),
    "context": frozenset({"RUNTIME_CONTEXT_WRITE"}),
    "router-cron-cs": frozenset({"CHANGESET_CREATE"}),
    "router-cron": frozenset({"CHANGESET_EXECUTE"}),
    "scheduler-cs": frozenset({"CHANGESET_CREATE"}),
    "scheduler": frozenset({"CHANGESET_EXECUTE"}),
    "web-cs": frozenset({"CHANGESET_CREATE"}),
    "web": frozenset({"CHANGESET_EXECUTE"}),
    "verify": frozenset({"VERIFY"}),
}
_RELEASE_V2_TEMPLATE_BINDING_PHASE_KINDS = frozenset(
    {
        ("foundation", "BOOTSTRAP_STACK"),
        ("foundation", "STACK_CREATE"),
        ("router-cron-cs", "CHANGESET_CREATE"),
        ("scheduler-cs", "CHANGESET_CREATE"),
        ("web-cs", "CHANGESET_CREATE"),
    }
)
_RELEASE_V2_DYNAMIC_TEMPLATE_BINDING_PHASE_KINDS = frozenset(
    {
        ("runtime", "STACK_UPDATE"),
        ("endpoint", "STACK_UPDATE"),
    }
)
_RELEASE_V2_OBSERVED_REQUEST_BINDING_KINDS = frozenset(
    {
        "BOOTSTRAP_STACK",
        "STACK_CREATE",
        "STACK_UPDATE",
        "CHANGESET_CREATE",
        "CHANGESET_EXECUTE",
    }
)
_RELEASE_V2_CONTENT_BINDING_KINDS = frozenset(
    {
        "ASSET_PUBLISH",
        "IMAGE_PUBLISH",
        "IMAGE_OBSERVE",
    }
)
_STEP_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")


def _optional_sha256(value: Any, *, field: str) -> str:
    digest = _text(value, field=field)
    if digest and _SHA_64.fullmatch(digest) is None:
        raise ContractError(f"{field} is invalid")
    return digest


def _release_subject(value: Any) -> str:
    subject = _text(value, field="step subject")
    if (
        not subject
        or len(subject) > 512
        or "*" in subject
        or any(character.isspace() for character in subject)
    ):
        raise ContractError("step subject is not exact")
    return subject


def _release_operation_sha256(
    plan_sha256: str,
    step: "ReleaseStepV2",
    completed_prefix_sha256: str,
) -> str:
    prefix_digest = _text(
        completed_prefix_sha256,
        field="operation completed prefix digest",
        pattern=_SHA_64,
    )
    return "sha256:" + hashlib.sha256(
        canonical_json_bytes(
            {
                "schema": "personal-operator.release-operation.v2",
                "planSha256": plan_sha256,
                "completedPrefixSha256": prefix_digest,
                "step": step.to_mapping(),
            }
        )
    ).hexdigest()


def _completed_prefix_sha256(
    completed_steps: Sequence[Mapping[str, Any]],
) -> str:
    canonical_steps: list[dict[str, str]] = []
    for raw_step in completed_steps:
        step = _exact_object(
            raw_step,
            {"stepId", "evidenceSha256"},
            label="completed prefix step",
        )
        canonical_steps.append(
            {
                "stepId": _text(
                    step["stepId"], field="completed prefix step ID", pattern=_STEP_ID
                ),
                "evidenceSha256": _text(
                    step["evidenceSha256"],
                    field="completed prefix evidence digest",
                    pattern=_SHA_64,
                ),
            }
        )
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "schema": "personal-operator.release-completed-prefix.v2",
                "completedSteps": canonical_steps,
            }
        )
    ).hexdigest()


def _release_v2_subject(account: str, region: str, commit: str, suffix: str) -> str:
    return f"release:{account}:{region}:{commit}:{suffix}"


def _release_v2_stack_subject(
    account: str,
    region: str,
    commit: str,
    stack_name: str,
) -> str:
    return f"cfn:{account}:{region}:stack:{stack_name}:release:{commit}"


def _validate_release_v2_step_shape(
    steps: list["ReleaseStepV2"],
    *,
    account: str,
    region: str,
    commit: str,
    context_path: str,
    image_digest: str,
) -> None:
    """Reject a phase-labelled list that is not the closed release recipe."""

    by_phase = {
        phase: [step for step in steps if step.phase == phase]
        for phase in RELEASE_V2_PHASES
    }
    foundation = by_phase["foundation"]
    baseline = (
        "BASELINE_OBSERVE",
        _release_v2_subject(account, region, commit, "baseline"),
    )
    bootstrap = (
        "BOOTSTRAP_STACK",
        _release_v2_stack_subject(account, region, commit, "CDKToolkit"),
    )
    if len(foundation) < 3 or (
        foundation[0].kind,
        foundation[0].subject,
    ) != baseline or (
        foundation[1].kind,
        foundation[1].subject,
    ) != bootstrap:
        raise ContractError(
            "foundation baseline and bootstrap subjects must precede every mutation"
        )

    asset_end = 2
    while (
        asset_end < len(foundation)
        and foundation[asset_end].kind == "ASSET_PUBLISH"
    ):
        asset_end += 1
    assets = foundation[2:asset_end]
    if not assets:
        raise ContractError("foundation requires at least one exact CDK asset")
    asset_subjects = [step.subject for step in assets]
    if asset_subjects != sorted(asset_subjects) or len(set(asset_subjects)) != len(
        asset_subjects
    ):
        raise ContractError("foundation CDK asset subjects are not sorted and unique")
    for step in assets:
        if re.fullmatch(r"cdk:asset:[0-9a-f]{64}", step.subject) is None:
            raise ContractError("foundation CDK asset subject is not exact")

    expected_foundation_stacks = [
        (
            "STACK_CREATE",
            _release_v2_stack_subject(account, region, commit, stack_name),
        )
        for stack_name in FOUNDATION_RELEASE_STACKS
    ]
    actual_foundation_stacks = [
        (step.kind, step.subject) for step in foundation[asset_end:]
    ]
    if actual_foundation_stacks != expected_foundation_stacks:
        raise ContractError("foundation stack create recipe is not exact")

    image_prefix = f"ecr:{account}:{region}:repository:{RUNTIME_REPOSITORY}"
    image_subject = f"{image_prefix}:release:{commit}"
    image = by_phase["image"]
    if (
        len(image) < 5
        or image[-1].kind != "IMAGE_OBSERVE"
        or image[-1].subject != image_subject
        or any(step.kind != "IMAGE_PUBLISH" for step in image[:-1])
    ):
        raise ContractError("image effect recipe is not exact")
    if image[-1].request_artifact != "build/image-publication-plan.json":
        raise ContractError("image publication plan artifact path is not exact")
    publishes = image[:-1]
    blobs = publishes[:-3]
    if not blobs:
        raise ContractError("image effect recipe requires at least one blob")
    blob_pattern = re.compile(
        rf"{re.escape(image_prefix)}:blob:sha256:([0-9a-f]{{64}})"
    )
    blob_digests: list[str] = []
    for step in blobs:
        matched = blob_pattern.fullmatch(step.subject)
        if matched is None:
            raise ContractError("image blob subject is not exact")
        blob_digests.append(matched.group(1))
    if blob_digests != sorted(blob_digests) or len(set(blob_digests)) != len(
        blob_digests
    ):
        raise ContractError("image blob digests are not sorted and unique")

    subject_manifest, sbom_referrer, provenance_referrer = publishes[-3:]
    image_hex = image_digest.removeprefix("sha256:")
    expected_subject_manifest = (
        f"{image_prefix}:subject-manifest:sha256:{image_hex}:"
        f"tag:commit-{commit}"
    )
    if subject_manifest.subject != expected_subject_manifest:
        raise ContractError("image subject manifest differs from the plan")
    referrer_patterns = (
        re.compile(
            rf"{re.escape(image_prefix)}:sbom-referrer-manifest:"
            rf"sha256:([0-9a-f]{{64}}):subject:sha256:{image_hex}"
        ),
        re.compile(
            rf"{re.escape(image_prefix)}:provenance-referrer-manifest:"
            rf"sha256:([0-9a-f]{{64}}):subject:sha256:{image_hex}"
        ),
    )
    referrer_digests: list[str] = []
    for step, pattern in zip(
        (sbom_referrer, provenance_referrer), referrer_patterns, strict=True
    ):
        matched = pattern.fullmatch(step.subject)
        if matched is None:
            raise ContractError("image referrer manifest recipe is not exact")
        referrer_digests.append(matched.group(1))
    effect_digests = [*blob_digests, image_hex, *referrer_digests]
    effect_subjects = [step.subject for step in publishes]
    if len(set(effect_digests)) != len(effect_digests) or len(
        set(effect_subjects)
    ) != len(effect_subjects):
        raise ContractError("image effect digests and subjects are not unique")
    for step, digest in zip(publishes, effect_digests, strict=True):
        if step.expected_content_sha256 != digest:
            raise ContractError("image effect subject differs from its content")

    def stack_subject(name: str) -> str:
        return _release_v2_stack_subject(account, region, commit, name)
    exact_shapes: dict[str, list[tuple[str, str]]] = {
        "runtime": [
            ("STACK_UPDATE", stack_subject("OpenClawAgentCore")),
            (
                "AGENTCORE_HARDEN",
                (
                    f"agentcore:{account}:{region}:runtime:personal_operator_bridge:"
                    f"release:{commit}:mmdsv2"
                ),
            ),
        ],
        "endpoint": [
            ("STACK_UPDATE", stack_subject("OpenClawAgentCore")),
        ],
        "context": [
            (
                "RUNTIME_CONTEXT_WRITE",
                _release_v2_subject(
                    account, region, commit, f"artifact:{context_path}"
                ),
            ),
        ],
        "router-cron-cs": [
            ("CHANGESET_CREATE", stack_subject("OpenClawRouter")),
            ("CHANGESET_CREATE", stack_subject("OpenClawCron")),
        ],
        "router-cron": [
            ("CHANGESET_EXECUTE", stack_subject("OpenClawRouter")),
            ("CHANGESET_EXECUTE", stack_subject("OpenClawCron")),
        ],
        "scheduler-cs": [
            ("CHANGESET_CREATE", stack_subject("PersonalOperatorScheduler")),
        ],
        "scheduler": [
            ("CHANGESET_EXECUTE", stack_subject("PersonalOperatorScheduler")),
        ],
        "web-cs": [
            ("CHANGESET_CREATE", stack_subject("PersonalOperatorWeb")),
        ],
        "web": [
            ("CHANGESET_EXECUTE", stack_subject("PersonalOperatorWeb")),
        ],
        "verify": [
            ("VERIFY", _release_v2_subject(account, region, commit, "verify")),
        ],
    }
    for phase, expected in exact_shapes.items():
        actual = [(step.kind, step.subject) for step in by_phase[phase]]
        if actual != expected:
            raise ContractError(f"{phase} phase recipe is not exact")


@dataclass(frozen=True, slots=True)
class ReleaseArtifactV2:
    path: str
    size: int
    sha256: str

    def to_mapping(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class ReleaseStepV2:
    step_id: str
    phase: str
    ordinal: int
    kind: str
    subject: str
    mutation: bool
    request_artifact: str
    request_sha256: str
    expected_template_sha256: str
    expected_template_parameter_sha256: str
    expected_request_sha256: str
    expected_observed_request_sha256: str
    expected_content_sha256: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "id": self.step_id,
            "phase": self.phase,
            "ordinal": self.ordinal,
            "kind": self.kind,
            "subject": self.subject,
            "mutation": self.mutation,
            "requestArtifact": self.request_artifact,
            "requestSha256": self.request_sha256,
            "expectedTemplateSha256": self.expected_template_sha256,
            "expectedTemplateParameterSha256": (
                self.expected_template_parameter_sha256
            ),
            "expectedRequestSha256": self.expected_request_sha256,
            "expectedObservedRequestSha256": (
                self.expected_observed_request_sha256
            ),
            "expectedContentSha256": self.expected_content_sha256,
        }


@dataclass(frozen=True, slots=True)
class ReleasePlanV2:
    """Closed clean-account release plan with an immutable artifact inventory."""

    SCHEMA: ClassVar[str] = "personal-operator.release-plan.v2"
    FIELDS: ClassVar[set[str]] = {
        "schema",
        "transactionId",
        "sourceCommit",
        "sourceTree",
        "account",
        "region",
        "releaseMode",
        "driverSha256",
        "evidenceRuntimeSha256",
        "runtimeImageDigest",
        "runtimeImageUri",
        "runtimeEndpointName",
        "contextRelativePath",
        "foundationInputsRelativePath",
        "derivationVersion",
        "artifacts",
        "steps",
        "rollbackTarget",
    }

    transaction_id: str
    source_commit: str
    source_tree: str
    account: str
    region: str
    release_mode: str
    driver_sha256: str
    evidence_runtime_sha256: str
    runtime_image_digest: str
    runtime_image_uri: str
    runtime_endpoint_name: str
    context_relative_path: str
    foundation_inputs_relative_path: str
    derivation_version: str
    artifacts: tuple[ReleaseArtifactV2, ...]
    steps: tuple[ReleaseStepV2, ...]
    rollback_mode: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ReleasePlanV2":
        value = _exact_object(raw, cls.FIELDS, label="release plan")
        if value["schema"] != cls.SCHEMA:
            raise ContractError("release plan schema is invalid")
        commit = _text(value["sourceCommit"], field="source commit", pattern=_SHA_40)
        tree = _text(value["sourceTree"], field="source tree", pattern=_SHA_40)
        transaction_id = _text(value["transactionId"], field="transaction ID")
        if transaction_id != f"release_{commit}":
            raise ContractError("release plan transaction ID is not commit-bound")
        account = _account(value["account"])
        region = _region(value["region"])
        release_mode = _text(value["releaseMode"], field="release mode")
        if release_mode != "CLEAN_ACCOUNT":
            raise ContractError("release mode must be CLEAN_ACCOUNT")
        driver_sha256 = _text(
            value["driverSha256"], field="driver digest", pattern=_SHA_64
        )
        evidence_runtime_sha256 = _text(
            value["evidenceRuntimeSha256"],
            field="evidence runtime digest",
            pattern=_SHA_64,
        )
        image_digest = _text(
            value["runtimeImageDigest"],
            field="runtime image digest",
            pattern=_DIGEST,
        )
        image_uri = _image_uri(
            account, region, image_digest, value["runtimeImageUri"]
        )
        endpoint_name = _text(
            value["runtimeEndpointName"], field="runtime endpoint name"
        )
        if endpoint_name != f"release_{commit}":
            raise ContractError("runtime endpoint name is not commit-bound")
        context_path = _safe_path(
            value["contextRelativePath"], field="context relative path"
        )
        if context_path != "build/runtime-context.json":
            raise ContractError("context relative path is not canonical")
        foundation_path = _safe_path(
            value["foundationInputsRelativePath"],
            field="foundation inputs relative path",
        )
        if foundation_path != "build/foundation-runtime-inputs.json":
            raise ContractError("foundation inputs relative path is not canonical")
        derivation = _text(value["derivationVersion"], field="derivation version")
        if derivation != "foundation-runtime-inputs-v1":
            raise ContractError("derivation version is not supported")
        rollback = _exact_object(
            value["rollbackTarget"], {"mode"}, label="rollback target"
        )
        if rollback != {"mode": "NO_PRIOR_RELEASE"}:
            raise ContractError("rollback target must be NO_PRIOR_RELEASE")

        raw_artifacts = value["artifacts"]
        if not isinstance(raw_artifacts, list) or not raw_artifacts:
            raise ContractError("release artifact inventory is empty")
        artifacts: list[ReleaseArtifactV2] = []
        for raw_artifact in raw_artifacts:
            artifact = _exact_object(
                raw_artifact, {"path", "size", "sha256"}, label="release artifact"
            )
            path = _safe_path(artifact["path"], field="artifact path")
            if path == "." or len(path) > 512:
                raise ContractError("artifact path is invalid")
            size = _count(artifact["size"], field="artifact size", minimum=1)
            digest = _text(
                artifact["sha256"], field="artifact digest", pattern=_SHA_64
            )
            artifacts.append(ReleaseArtifactV2(path, size, digest))
        artifact_paths = [artifact.path for artifact in artifacts]
        if artifact_paths != sorted(artifact_paths) or len(set(artifact_paths)) != len(
            artifact_paths
        ):
            raise ContractError("release artifact inventory is not sorted and unique")
        artifact_digests = {artifact.path: artifact.sha256 for artifact in artifacts}

        raw_steps = value["steps"]
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ContractError("release plan step inventory is empty")
        steps: list[ReleaseStepV2] = []
        step_ids: set[str] = set()
        request_artifacts: list[str] = []
        step_fields = {
            "id",
            "phase",
            "ordinal",
            "kind",
            "subject",
            "mutation",
            "requestArtifact",
            "requestSha256",
            "expectedTemplateSha256",
            "expectedTemplateParameterSha256",
            "expectedRequestSha256",
            "expectedObservedRequestSha256",
            "expectedContentSha256",
        }
        for ordinal, raw_step in enumerate(raw_steps):
            step = _exact_object(raw_step, step_fields, label="release plan step")
            step_id = _text(step["id"], field="step ID", pattern=_STEP_ID)
            if step_id in step_ids:
                raise ContractError("release plan step IDs are not unique")
            step_ids.add(step_id)
            actual_ordinal = _count(step["ordinal"], field="step ordinal")
            if actual_ordinal != ordinal:
                raise ContractError("release plan step ordinal is not contiguous")
            phase = _text(step["phase"], field="step phase")
            if phase not in RELEASE_V2_PHASES:
                raise ContractError("release plan step phase is unknown")
            kind = _text(step["kind"], field="step kind")
            if kind not in RELEASE_V2_STEP_KINDS:
                raise ContractError("release plan step kind is unknown")
            if kind not in _RELEASE_V2_PHASE_KINDS[phase]:
                raise ContractError("release plan step kind is invalid for its phase")
            mutation = step["mutation"]
            if not isinstance(mutation, bool) or mutation != (
                kind in _RELEASE_V2_MUTATION_KINDS
            ):
                raise ContractError("release plan step mutation flag is invalid")
            subject = _release_subject(step["subject"])
            request_artifact = _text(
                step["requestArtifact"], field="request artifact"
            )
            request_sha256 = _optional_sha256(
                step["requestSha256"], field="request digest"
            )
            if not request_artifact or not request_sha256:
                raise ContractError("step request artifact binding is incomplete")
            request_artifact = _safe_path(
                request_artifact, field="request artifact"
            )
            if artifact_digests.get(request_artifact) != request_sha256:
                raise ContractError(
                    "step request artifact binding does not match the inventory"
                )
            request_artifacts.append(request_artifact)
            expected_template_sha256 = _optional_sha256(
                step["expectedTemplateSha256"],
                field="expected update template digest",
            )
            expected_template = _optional_sha256(
                step["expectedTemplateParameterSha256"],
                field="expected template parameter digest",
            )
            expected_request = _optional_sha256(
                step["expectedRequestSha256"],
                field="expected request digest",
            )
            expected_observed_request = _optional_sha256(
                step["expectedObservedRequestSha256"],
                field="expected observed request digest",
            )
            expected_content = _optional_sha256(
                step["expectedContentSha256"],
                field="expected content digest",
            )
            if bool(expected_template_sha256) != (
                (phase, kind)
                in _RELEASE_V2_DYNAMIC_TEMPLATE_BINDING_PHASE_KINDS
            ):
                raise ContractError(
                    "step update template binding differs from its kind"
                )
            if bool(expected_template) != (
                (phase, kind) in _RELEASE_V2_TEMPLATE_BINDING_PHASE_KINDS
            ):
                raise ContractError(
                    "step template evidence binding differs from its kind"
                )
            if expected_request != request_sha256:
                raise ContractError(
                    "step expected request evidence binding differs from its artifact"
                )
            if bool(expected_observed_request) != (
                kind in _RELEASE_V2_OBSERVED_REQUEST_BINDING_KINDS
            ):
                raise ContractError(
                    "step expected observed request binding differs from its kind"
                )
            if bool(expected_content) != (
                kind in _RELEASE_V2_CONTENT_BINDING_KINDS
            ):
                raise ContractError(
                    "step content evidence binding differs from its kind"
                )
            if expected_observed_request and expected_observed_request in {
                request_sha256,
                expected_template_sha256,
                expected_template,
                expected_content,
            }:
                raise ContractError(
                    "step expected observed request digest aliases another binding"
                )
            if expected_template_sha256 and expected_template_sha256 in {
                request_sha256,
                expected_template,
                expected_observed_request,
                expected_content,
            }:
                raise ContractError(
                    "step update template digest aliases another binding"
                )
            if kind == "IMAGE_OBSERVE" and (
                expected_content != image_digest.removeprefix("sha256:")
            ):
                raise ContractError(
                    "step image content digest differs from the plan"
                )
            steps.append(
                ReleaseStepV2(
                    step_id,
                    phase,
                    ordinal,
                    kind,
                    subject,
                    mutation,
                    request_artifact,
                    request_sha256,
                    expected_template_sha256,
                    expected_template,
                    expected_request,
                    expected_observed_request,
                    expected_content,
                )
            )
        ordered_phase_runs: list[str] = []
        for step in steps:
            if not ordered_phase_runs or ordered_phase_runs[-1] != step.phase:
                ordered_phase_runs.append(step.phase)
        if tuple(ordered_phase_runs) != RELEASE_V2_PHASES:
            raise ContractError(
                "release plan phases are missing, reordered, or noncontiguous"
            )
        _validate_release_v2_step_shape(
            steps,
            account=account,
            region=region,
            commit=commit,
            context_path=context_path,
            image_digest=image_digest,
        )
        if (
            len(request_artifacts) != len(set(request_artifacts))
            or set(request_artifacts) != set(artifact_paths)
        ):
            raise ContractError(
                "release artifact inventory reference set is not exact"
            )
        return cls(
            transaction_id,
            commit,
            tree,
            account,
            region,
            release_mode,
            driver_sha256,
            evidence_runtime_sha256,
            image_digest,
            image_uri,
            endpoint_name,
            context_path,
            foundation_path,
            derivation,
            tuple(artifacts),
            tuple(steps),
            rollback["mode"],
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ReleasePlanV2":
        return cls.from_mapping(parse_canonical_object(payload))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "transactionId": self.transaction_id,
            "sourceCommit": self.source_commit,
            "sourceTree": self.source_tree,
            "account": self.account,
            "region": self.region,
            "releaseMode": self.release_mode,
            "driverSha256": self.driver_sha256,
            "evidenceRuntimeSha256": self.evidence_runtime_sha256,
            "runtimeImageDigest": self.runtime_image_digest,
            "runtimeImageUri": self.runtime_image_uri,
            "runtimeEndpointName": self.runtime_endpoint_name,
            "contextRelativePath": self.context_relative_path,
            "foundationInputsRelativePath": self.foundation_inputs_relative_path,
            "derivationVersion": self.derivation_version,
            "artifacts": [artifact.to_mapping() for artifact in self.artifacts],
            "steps": [step.to_mapping() for step in self.steps],
            "rollbackTarget": {"mode": self.rollback_mode},
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


def _canonical_release_plan_v2(plan: ReleasePlanV2) -> ReleasePlanV2:
    if not isinstance(plan, ReleasePlanV2):
        raise ContractError("release plan is not typed")
    try:
        payload = plan.to_bytes()
    except (AttributeError, TypeError, ValueError) as error:
        raise ContractError("release plan cannot be canonically serialized") from error
    return ReleasePlanV2.from_bytes(payload)


@dataclass(frozen=True, slots=True)
class FoundationRuntimeInputsV1:
    """Observed foundation outputs used to derive the runtime-only stage."""

    SCHEMA: ClassVar[str] = "personal-operator.foundation-runtime-inputs.v1"
    FIELDS: ClassVar[set[str]] = {
        "schema",
        "sourceCommit",
        "sourceTree",
        "account",
        "region",
        "releasePlanSha256",
        "derivationVersion",
        "privateSubnetIds",
        "runtimeSecurityGroupIds",
        "userFilesBucketName",
        "capabilityGatewayFunctionArn",
        "workspaceBrokerFunctionName",
        "agentCoreStackId",
        "guardrailId",
        "guardrailVersion",
        "guardrailArn",
        "foundationSnapshotSha256",
    }

    source_commit: str
    source_tree: str
    account: str
    region: str
    release_plan_sha256: str
    derivation_version: str
    private_subnet_ids: tuple[str, ...]
    runtime_security_group_ids: tuple[str, ...]
    user_files_bucket_name: str
    capability_gateway_function_arn: str
    workspace_broker_function_name: str
    agent_core_stack_id: str
    guardrail_id: str
    guardrail_version: str
    guardrail_arn: str
    foundation_snapshot_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "FoundationRuntimeInputsV1":
        value = _exact_object(raw, cls.FIELDS, label="foundation runtime inputs")
        if value["schema"] != cls.SCHEMA:
            raise ContractError("foundation runtime inputs schema is invalid")
        commit = _text(value["sourceCommit"], field="source commit", pattern=_SHA_40)
        tree = _text(value["sourceTree"], field="source tree", pattern=_SHA_40)
        account = _account(value["account"])
        region = _region(value["region"])
        release_plan_sha256 = _text(
            value["releasePlanSha256"],
            field="release plan digest",
            pattern=_SHA_64,
        )
        derivation_version = _text(
            value["derivationVersion"], field="derivation version"
        )
        if derivation_version != "foundation-runtime-inputs-v1":
            raise ContractError("foundation derivation version is not supported")
        subnet_ids = RuntimeConfigurationV1._identifiers(
            value["privateSubnetIds"], pattern=_SUBNET_ID, label="private subnet"
        )
        security_group_ids = RuntimeConfigurationV1._identifiers(
            value["runtimeSecurityGroupIds"],
            pattern=_SECURITY_GROUP_ID,
            label="runtime security group",
        )
        bucket = _text(value["userFilesBucketName"], field="user files bucket")
        if bucket != f"openclaw-user-files-{account}-{region}":
            raise ContractError("user files bucket is not account-bound")
        gateway = _text(
            value["capabilityGatewayFunctionArn"], field="capability gateway ARN"
        )
        expected_gateway = (
            f"arn:aws:lambda:{region}:{account}:function:"
            "personal-operator-capability-gateway"
        )
        if gateway != expected_gateway:
            raise ContractError("capability gateway ARN crosses its exact subject")
        broker = _text(
            value["workspaceBrokerFunctionName"], field="workspace broker name"
        )
        if broker != "personal-operator-workspace-credential-broker":
            raise ContractError("workspace broker name is not canonical")
        agent_core_stack_id = _cloudformation_stack_id(
            value["agentCoreStackId"],
            account=account,
            region=region,
            stack_name="OpenClawAgentCore",
            field="AgentCore stack ID",
        )
        guardrail_id = _text(value["guardrailId"], field="guardrail ID")
        guardrail_version = _text(
            value["guardrailVersion"], field="guardrail version"
        )
        guardrail_arn = _text(value["guardrailArn"], field="guardrail ARN")
        guardrail_values = (guardrail_id, guardrail_version, guardrail_arn)
        if any(guardrail_values) != all(guardrail_values):
            raise ContractError("guardrail identity, version, and ARN must be atomic")
        if guardrail_id:
            if re.fullmatch(r"[a-z0-9]+", guardrail_id) is None:
                raise ContractError("guardrail ID is not canonical")
            if re.fullmatch(r"(?:DRAFT|[1-9][0-9]{0,7})", guardrail_version) is None:
                raise ContractError("guardrail version is not canonical")
            if guardrail_arn != (
                f"arn:aws:bedrock:{region}:{account}:guardrail/{guardrail_id}"
            ):
                raise ContractError("guardrail ARN crosses its exact subject")
        snapshot = _text(
            value["foundationSnapshotSha256"],
            field="foundation snapshot digest",
            pattern=_SHA_64,
        )
        return cls(
            commit,
            tree,
            account,
            region,
            release_plan_sha256,
            derivation_version,
            subnet_ids,
            security_group_ids,
            bucket,
            gateway,
            broker,
            agent_core_stack_id,
            guardrail_id,
            guardrail_version,
            guardrail_arn,
            snapshot,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "FoundationRuntimeInputsV1":
        return cls.from_mapping(parse_canonical_object(payload))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "sourceCommit": self.source_commit,
            "sourceTree": self.source_tree,
            "account": self.account,
            "region": self.region,
            "releasePlanSha256": self.release_plan_sha256,
            "derivationVersion": self.derivation_version,
            "privateSubnetIds": list(self.private_subnet_ids),
            "runtimeSecurityGroupIds": list(self.runtime_security_group_ids),
            "userFilesBucketName": self.user_files_bucket_name,
            "capabilityGatewayFunctionArn": self.capability_gateway_function_arn,
            "workspaceBrokerFunctionName": self.workspace_broker_function_name,
            "agentCoreStackId": self.agent_core_stack_id,
            "guardrailId": self.guardrail_id,
            "guardrailVersion": self.guardrail_version,
            "guardrailArn": self.guardrail_arn,
            "foundationSnapshotSha256": self.foundation_snapshot_sha256,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def validate_plan_identity(self, plan: ReleasePlanV2) -> None:
        canonical_self = FoundationRuntimeInputsV1.from_bytes(self.to_bytes())
        canonical_plan = _canonical_release_plan_v2(plan)
        if (
            canonical_self.source_commit,
            canonical_self.source_tree,
            canonical_self.account,
            canonical_self.region,
            canonical_self.release_plan_sha256,
            canonical_self.derivation_version,
        ) != (
            canonical_plan.source_commit,
            canonical_plan.source_tree,
            canonical_plan.account,
            canonical_plan.region,
            canonical_plan.digest(),
            canonical_plan.derivation_version,
        ):
            raise ContractError(
                "foundation runtime inputs identity differs from the release plan"
            )


@dataclass(frozen=True, slots=True)
class ReleaseStepObservationV2:
    """Canonical observer evidence and every derived value for one plan step."""

    SCHEMA: ClassVar[str] = "personal-operator.release-step-observation.v2"
    FIELDS: ClassVar[set[str]] = {
        "schema",
        "planSha256",
        "stepId",
        "subject",
        "observerEvidenceSha256",
        "foundationRuntimeInputs",
        "agentCoreStackId",
        "runtimeImageDigest",
        "runtimeId",
        "runtimeVersion",
        "runtimeArn",
        "runtimeEndpointId",
        "runtimeContextSha256",
        "routerTargetStackId",
        "routerChangeSetId",
        "cronTargetStackId",
        "cronChangeSetId",
        "routerCronChangesetsSha256",
        "routerCronApplicationSha256",
        "schedulerTargetStackId",
        "schedulerChangeSetId",
        "schedulerChangesetSha256",
        "schedulerApplicationSha256",
        "webTargetStackId",
        "webChangeSetId",
        "webChangesetSha256",
        "webApplicationSha256",
        "verificationSha256",
    }

    plan_sha256: str
    step_id: str
    subject: str
    observer_evidence_sha256: str
    foundation_runtime_inputs: FoundationRuntimeInputsV1 | None
    agent_core_stack_id: str
    runtime_image_digest: str
    runtime_id: str
    runtime_version: str
    runtime_arn: str
    runtime_endpoint_id: str
    runtime_context_sha256: str
    router_target_stack_id: str
    router_change_set_id: str
    cron_target_stack_id: str
    cron_change_set_id: str
    router_cron_changesets_sha256: str
    router_cron_application_sha256: str
    scheduler_target_stack_id: str
    scheduler_change_set_id: str
    scheduler_changeset_sha256: str
    scheduler_application_sha256: str
    web_target_stack_id: str
    web_change_set_id: str
    web_changeset_sha256: str
    web_application_sha256: str
    verification_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ReleaseStepObservationV2":
        value = _exact_object(raw, cls.FIELDS, label="release step observation")
        if value["schema"] != cls.SCHEMA:
            raise ContractError("release step observation schema is invalid")
        plan_sha256 = _text(
            value["planSha256"], field="plan digest", pattern=_SHA_64
        )
        step_id = _text(value["stepId"], field="step ID", pattern=_STEP_ID)
        subject = _release_subject(value["subject"])
        observer_evidence = _text(
            value["observerEvidenceSha256"],
            field="observer evidence digest",
            pattern=_SHA_64,
        )
        raw_foundation = value["foundationRuntimeInputs"]
        if raw_foundation == {}:
            foundation_inputs = None
        elif isinstance(raw_foundation, Mapping):
            foundation_inputs = FoundationRuntimeInputsV1.from_mapping(raw_foundation)
        else:
            raise ContractError("foundation runtime inputs are malformed")
        agent_core_stack_id = _optional_cloudformation_id(
            value["agentCoreStackId"],
            change_set=False,
            field="AgentCore stack ID",
        )
        image_digest = _text(
            value["runtimeImageDigest"], field="runtime image digest"
        )
        if image_digest and _DIGEST.fullmatch(image_digest) is None:
            raise ContractError("runtime image digest is invalid")
        runtime_id = _text(value["runtimeId"], field="runtime ID")
        runtime_version = _text(value["runtimeVersion"], field="runtime version")
        runtime_arn = _text(value["runtimeArn"], field="runtime ARN")
        if len({bool(runtime_id), bool(runtime_version), bool(runtime_arn)}) != 1:
            raise ContractError("runtime ID, version, and ARN must be atomic")
        if runtime_id:
            if _RUNTIME_ID.fullmatch(runtime_id) is None:
                raise ContractError("runtime ID is invalid")
            if _RUNTIME_VERSION.fullmatch(runtime_version) is None:
                raise ContractError("runtime version is invalid")
            generic_runtime_arn = re.compile(
                r"arn:aws:bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:agent/"
                r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
                r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:[1-9][0-9]{0,4}"
            )
            if generic_runtime_arn.fullmatch(runtime_arn) is None:
                raise ContractError("runtime ARN is invalid")
            if runtime_arn.rsplit(":", 1)[-1] != runtime_version:
                raise ContractError("runtime ARN and version differ")
        endpoint_id = _text(value["runtimeEndpointId"], field="runtime endpoint ID")
        if endpoint_id and _RUNTIME_ID.fullmatch(endpoint_id) is None:
            raise ContractError("runtime endpoint ID is invalid")
        consumer_ids = {
            field: _optional_cloudformation_id(
                value[field],
                change_set=field.endswith("ChangeSetId"),
                field=label,
            )
            for field, label in (
                ("routerTargetStackId", "router target stack ID"),
                ("routerChangeSetId", "router change-set ID"),
                ("cronTargetStackId", "cron target stack ID"),
                ("cronChangeSetId", "cron change-set ID"),
                ("schedulerTargetStackId", "scheduler target stack ID"),
                ("schedulerChangeSetId", "scheduler change-set ID"),
                ("webTargetStackId", "web target stack ID"),
                ("webChangeSetId", "web change-set ID"),
            )
        }
        digest_fields = (
            ("runtimeContextSha256", "runtime context digest"),
            ("routerCronChangesetsSha256", "router cron changesets digest"),
            ("routerCronApplicationSha256", "router cron application digest"),
            ("schedulerChangesetSha256", "scheduler changeset digest"),
            ("schedulerApplicationSha256", "scheduler application digest"),
            ("webChangesetSha256", "web changeset digest"),
            ("webApplicationSha256", "web application digest"),
            ("verificationSha256", "verification digest"),
        )
        digests = {
            field: _optional_sha256(value[field], field=label)
            for field, label in digest_fields
        }
        return cls(
            plan_sha256,
            step_id,
            subject,
            observer_evidence,
            foundation_inputs,
            agent_core_stack_id,
            image_digest,
            runtime_id,
            runtime_version,
            runtime_arn,
            endpoint_id,
            digests["runtimeContextSha256"],
            consumer_ids["routerTargetStackId"],
            consumer_ids["routerChangeSetId"],
            consumer_ids["cronTargetStackId"],
            consumer_ids["cronChangeSetId"],
            digests["routerCronChangesetsSha256"],
            digests["routerCronApplicationSha256"],
            consumer_ids["schedulerTargetStackId"],
            consumer_ids["schedulerChangeSetId"],
            digests["schedulerChangesetSha256"],
            digests["schedulerApplicationSha256"],
            consumer_ids["webTargetStackId"],
            consumer_ids["webChangeSetId"],
            digests["webChangesetSha256"],
            digests["webApplicationSha256"],
            digests["verificationSha256"],
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ReleaseStepObservationV2":
        return cls.from_mapping(parse_canonical_object(payload))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "planSha256": self.plan_sha256,
            "stepId": self.step_id,
            "subject": self.subject,
            "observerEvidenceSha256": self.observer_evidence_sha256,
            "foundationRuntimeInputs": (
                self.foundation_runtime_inputs.to_mapping()
                if self.foundation_runtime_inputs is not None
                else {}
            ),
            "agentCoreStackId": self.agent_core_stack_id,
            "runtimeImageDigest": self.runtime_image_digest,
            "runtimeId": self.runtime_id,
            "runtimeVersion": self.runtime_version,
            "runtimeArn": self.runtime_arn,
            "runtimeEndpointId": self.runtime_endpoint_id,
            "runtimeContextSha256": self.runtime_context_sha256,
            "routerTargetStackId": self.router_target_stack_id,
            "routerChangeSetId": self.router_change_set_id,
            "cronTargetStackId": self.cron_target_stack_id,
            "cronChangeSetId": self.cron_change_set_id,
            "routerCronChangesetsSha256": self.router_cron_changesets_sha256,
            "routerCronApplicationSha256": self.router_cron_application_sha256,
            "schedulerTargetStackId": self.scheduler_target_stack_id,
            "schedulerChangeSetId": self.scheduler_change_set_id,
            "schedulerChangesetSha256": self.scheduler_changeset_sha256,
            "schedulerApplicationSha256": self.scheduler_application_sha256,
            "webTargetStackId": self.web_target_stack_id,
            "webChangeSetId": self.web_change_set_id,
            "webChangesetSha256": self.web_changeset_sha256,
            "webApplicationSha256": self.web_application_sha256,
            "verificationSha256": self.verification_sha256,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def validate_plan_step(
        self,
        plan: ReleasePlanV2,
        *,
        completed_step_count: int,
        prior_agent_core_stack_id: str = "",
        prior_runtime_id: str = "",
        prior_runtime_version: str = "",
        prior_runtime_arn: str = "",
    ) -> None:
        canonical = ReleaseStepObservationV2.from_bytes(self.to_bytes())
        canonical_plan = _canonical_release_plan_v2(plan)
        count = _count(completed_step_count, field="completed step count")
        if count >= len(canonical_plan.steps):
            raise ContractError("release step observation has no next plan step")
        step = canonical_plan.steps[count]
        if canonical.plan_sha256 != canonical_plan.digest():
            raise ContractError("release step observation plan differs")
        if (canonical.step_id, canonical.subject) != (step.step_id, step.subject):
            raise ContractError("release step observation subject differs from the plan")
        next_phase = (
            canonical_plan.steps[count + 1].phase
            if count + 1 < len(canonical_plan.steps)
            else None
        )
        phase_complete = next_phase != step.phase
        stack_subject = lambda stack_name: (
            f"cfn:{canonical_plan.account}:{canonical_plan.region}:stack:"
            f"{stack_name}:release:{canonical_plan.source_commit}"
        )
        consumer_identity_pairs = {
            "router_identity": (
                canonical.router_target_stack_id,
                canonical.router_change_set_id,
                "OpenClawRouter",
            ),
            "cron_identity": (
                canonical.cron_target_stack_id,
                canonical.cron_change_set_id,
                "OpenClawCron",
            ),
            "scheduler_identity": (
                canonical.scheduler_target_stack_id,
                canonical.scheduler_change_set_id,
                "PersonalOperatorScheduler",
            ),
            "web_identity": (
                canonical.web_target_stack_id,
                canonical.web_change_set_id,
                "PersonalOperatorWeb",
            ),
        }
        for label, (target_stack_id, change_set_id, _) in (
            consumer_identity_pairs.items()
        ):
            if bool(target_stack_id) != bool(change_set_id):
                raise ContractError(
                    f"release step observation {label} fields are not atomic"
                )
        expected = {
            "foundation": step.phase == "foundation" and phase_complete,
            "agentcore_stack": step.phase in {"runtime", "endpoint"},
            "image": step.phase == "image" and phase_complete,
            "runtime": step.phase == "runtime",
            "endpoint": step.phase == "endpoint",
            "context": step.phase == "context",
            "router_cs": step.phase == "router-cron-cs" and phase_complete,
            "router_identity": (
                step.kind == "CHANGESET_CREATE"
                and step.subject == stack_subject("OpenClawRouter")
            ),
            "cron_identity": (
                step.kind == "CHANGESET_CREATE"
                and step.subject == stack_subject("OpenClawCron")
            ),
            "router": step.phase == "router-cron" and phase_complete,
            "scheduler_cs": step.phase == "scheduler-cs",
            "scheduler_identity": (
                step.kind == "CHANGESET_CREATE"
                and step.subject == stack_subject("PersonalOperatorScheduler")
            ),
            "scheduler": step.phase == "scheduler",
            "web_cs": step.phase == "web-cs",
            "web_identity": (
                step.kind == "CHANGESET_CREATE"
                and step.subject == stack_subject("PersonalOperatorWeb")
            ),
            "web": step.phase == "web",
            "verify": step.phase == "verify",
        }
        actual = {
            "foundation": canonical.foundation_runtime_inputs is not None,
            "agentcore_stack": bool(canonical.agent_core_stack_id),
            "image": bool(canonical.runtime_image_digest),
            "runtime": bool(canonical.runtime_id),
            "endpoint": bool(canonical.runtime_endpoint_id),
            "context": bool(canonical.runtime_context_sha256),
            "router_cs": bool(canonical.router_cron_changesets_sha256),
            "router_identity": bool(canonical.router_target_stack_id),
            "cron_identity": bool(canonical.cron_target_stack_id),
            "router": bool(canonical.router_cron_application_sha256),
            "scheduler_cs": bool(canonical.scheduler_changeset_sha256),
            "scheduler_identity": bool(canonical.scheduler_target_stack_id),
            "scheduler": bool(canonical.scheduler_application_sha256),
            "web_cs": bool(canonical.web_changeset_sha256),
            "web_identity": bool(canonical.web_target_stack_id),
            "web": bool(canonical.web_application_sha256),
            "verify": bool(canonical.verification_sha256),
        }
        if actual != expected:
            raise ContractError("release step observation derived values are not exact")
        if canonical.foundation_runtime_inputs is not None:
            canonical.foundation_runtime_inputs.validate_plan_identity(canonical_plan)
        if canonical.agent_core_stack_id:
            _cloudformation_stack_id(
                canonical.agent_core_stack_id,
                account=canonical_plan.account,
                region=canonical_plan.region,
                stack_name="OpenClawAgentCore",
                field="AgentCore stack ID",
            )
            if canonical.agent_core_stack_id != prior_agent_core_stack_id:
                raise ContractError(
                    "release step observation changed the AgentCore stack ID"
                )
        for _, (target_stack_id, change_set_id, stack_name) in (
            consumer_identity_pairs.items()
        ):
            if not target_stack_id:
                continue
            _cloudformation_stack_id(
                target_stack_id,
                account=canonical_plan.account,
                region=canonical_plan.region,
                stack_name=stack_name,
                field=f"{stack_name} target stack ID",
            )
            _cloudformation_change_set_id(
                change_set_id,
                account=canonical_plan.account,
                region=canonical_plan.region,
                source_commit=canonical_plan.source_commit,
                field=f"{stack_name} change-set ID",
            )
        if canonical.runtime_image_digest and (
            canonical.runtime_image_digest != canonical_plan.runtime_image_digest
        ):
            raise ContractError("release step observation image differs from the plan")
        if canonical.runtime_id:
            expected_arn = re.compile(
                rf"arn:aws:bedrock-agentcore:{re.escape(canonical_plan.region)}:"
                rf"{re.escape(canonical_plan.account)}:agent/"
                r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
                r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:[1-9][0-9]{0,4}"
            )
            if expected_arn.fullmatch(canonical.runtime_arn) is None:
                raise ContractError("release step observation runtime ARN crosses the plan")
        prior_values = (prior_runtime_id, prior_runtime_version, prior_runtime_arn)
        if any(prior_values) != all(prior_values):
            raise ContractError("prior runtime ID, version, and ARN must be atomic")
        if step.kind == "AGENTCORE_HARDEN":
            if not all(prior_values):
                raise ContractError("AgentCore hardening lacks prior runtime identity")
            if canonical.runtime_id != prior_runtime_id:
                raise ContractError("AgentCore hardening changed the runtime ID")
            if canonical.runtime_arn.rsplit(":", 1)[0] != prior_runtime_arn.rsplit(
                ":", 1
            )[0]:
                raise ContractError("AgentCore hardening changed the runtime ARN base")
            if int(canonical.runtime_version) < int(prior_runtime_version):
                raise ContractError("AgentCore hardening regressed the runtime version")


@dataclass(frozen=True, slots=True)
class MutationRequestV2:
    """One write-ahead request bound to the exact next mutating plan step."""

    SCHEMA: ClassVar[str] = "personal-operator.mutation-request.v2"
    FIELDS: ClassVar[set[str]] = {
        "schema",
        "transactionId",
        "planSha256",
        "completedPrefixSha256",
        "stepId",
        "operationSha256",
        "kind",
        "subject",
        "requestArtifact",
        "requestSha256",
    }

    transaction_id: str
    plan_sha256: str
    completed_prefix_sha256: str
    step_id: str
    operation_sha256: str
    kind: str
    subject: str
    request_artifact: str
    request_sha256: str

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        plan: ReleasePlanV2 | None = None,
        completed_step_count: int | None = None,
        completed_prefix_sha256: str | None = None,
    ) -> "MutationRequestV2":
        value = _exact_object(raw, cls.FIELDS, label="mutation request")
        if value["schema"] != cls.SCHEMA:
            raise ContractError("mutation request schema is invalid")
        transaction_id = _text(value["transactionId"], field="transaction ID")
        if re.fullmatch(r"release_[0-9a-f]{40}", transaction_id) is None:
            raise ContractError("mutation request transaction ID is invalid")
        plan_sha256 = _text(
            value["planSha256"], field="plan digest", pattern=_SHA_64
        )
        completed_prefix = _text(
            value["completedPrefixSha256"],
            field="completed prefix digest",
            pattern=_SHA_64,
        )
        step_id = _text(value["stepId"], field="step ID", pattern=_STEP_ID)
        operation_sha256 = _text(
            value["operationSha256"], field="operation digest", pattern=_DIGEST
        )
        kind = _text(value["kind"], field="step kind")
        if kind not in RELEASE_V2_STEP_KINDS:
            raise ContractError("mutation request kind is unknown")
        if kind not in _RELEASE_V2_MUTATION_KINDS:
            raise ContractError("mutation request kind is not a mutation")
        subject = _release_subject(value["subject"])
        request_artifact = _text(
            value["requestArtifact"], field="request artifact"
        )
        request_sha256 = _optional_sha256(
            value["requestSha256"], field="request digest"
        )
        if request_artifact:
            request_artifact = _safe_path(
                request_artifact, field="request artifact"
            )
            if not request_sha256:
                raise ContractError("mutation request artifact lacks its digest")
        elif request_sha256:
            raise ContractError("mutation request digest has no artifact")
        request = cls(
            transaction_id,
            plan_sha256,
            completed_prefix,
            step_id,
            operation_sha256,
            kind,
            subject,
            request_artifact,
            request_sha256,
        )
        if (
            plan is not None
            or completed_step_count is not None
            or completed_prefix_sha256 is not None
        ):
            if (
                plan is None
                or completed_step_count is None
                or completed_prefix_sha256 is None
            ):
                raise ContractError(
                    "mutation request plan, cursor, and completed prefix are atomic"
                )
            request.validate_plan(
                plan,
                completed_step_count=completed_step_count,
                completed_prefix_sha256=completed_prefix_sha256,
            )
        return request

    @classmethod
    def from_bytes(
        cls,
        payload: bytes,
        *,
        plan: ReleasePlanV2 | None = None,
        completed_step_count: int | None = None,
        completed_prefix_sha256: str | None = None,
    ) -> "MutationRequestV2":
        return cls.from_mapping(
            parse_canonical_object(payload),
            plan=plan,
            completed_step_count=completed_step_count,
            completed_prefix_sha256=completed_prefix_sha256,
        )

    def validate_plan(
        self,
        plan: ReleasePlanV2,
        *,
        completed_step_count: int,
        completed_prefix_sha256: str,
    ) -> None:
        self = MutationRequestV2.from_bytes(self.to_bytes())
        plan = _canonical_release_plan_v2(plan)
        count = _count(completed_step_count, field="completed step count")
        if self.transaction_id != plan.transaction_id:
            raise ContractError("mutation request transaction differs from the plan")
        if self.plan_sha256 != plan.digest():
            raise ContractError("mutation request plan digest differs")
        if count >= len(plan.steps):
            raise ContractError("mutation request has no next plan step")
        step = plan.steps[count]
        if not step.mutation:
            raise ContractError("next plan step is not a mutation")
        completed_prefix = _text(
            completed_prefix_sha256,
            field="completed prefix digest",
            pattern=_SHA_64,
        )
        if self.completed_prefix_sha256 != completed_prefix:
            raise ContractError(
                "mutation request completed prefix differs from the journal"
            )
        expected_operation = _release_operation_sha256(
            plan.digest(),
            step,
            completed_prefix,
        )
        if self.operation_sha256 != expected_operation:
            raise ContractError("mutation request operation differs from the plan")
        comparisons = (
            (self.step_id, step.step_id, "next step"),
            (self.kind, step.kind, "kind"),
            (self.subject, step.subject, "subject"),
            (self.request_artifact, step.request_artifact, "request artifact"),
            (self.request_sha256, step.request_sha256, "request digest"),
        )
        for actual, expected, label in comparisons:
            if actual != expected:
                raise ContractError(f"mutation request {label} differs from the plan")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "transactionId": self.transaction_id,
            "planSha256": self.plan_sha256,
            "completedPrefixSha256": self.completed_prefix_sha256,
            "stepId": self.step_id,
            "operationSha256": self.operation_sha256,
            "kind": self.kind,
            "subject": self.subject,
            "requestArtifact": self.request_artifact,
            "requestSha256": self.request_sha256,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())


@dataclass(frozen=True, slots=True)
class CompletedReleaseStepV2:
    step_id: str
    evidence_sha256: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "stepId": self.step_id,
            "evidenceSha256": self.evidence_sha256,
        }


_FAILED_RETAINED_KIND_REASON_STATUSES = {
    "BOOTSTRAP_STACK": {
        "CLOUDFORMATION_STACK_FAILED": frozenset(
            {"CREATE_FAILED", "ROLLBACK_COMPLETE", "ROLLBACK_FAILED"}
        ),
        "CF_SUBJECT_CONFLICT": frozenset({"CREATE_COMPLETE"}),
    },
    "STACK_CREATE": {
        "CLOUDFORMATION_STACK_FAILED": frozenset(
            {"CREATE_FAILED", "ROLLBACK_COMPLETE", "ROLLBACK_FAILED"}
        ),
        "CF_SUBJECT_CONFLICT": frozenset({"CREATE_COMPLETE"}),
    },
    "STACK_UPDATE": {
        "CLOUDFORMATION_STACK_FAILED": frozenset(
            {
                "UPDATE_FAILED",
                "UPDATE_ROLLBACK_COMPLETE",
                "UPDATE_ROLLBACK_FAILED",
            }
        ),
        "CF_SUBJECT_CONFLICT": frozenset({"UPDATE_COMPLETE"}),
    },
    "CHANGESET_CREATE": {
        "CLOUDFORMATION_CHANGESET_FAILED": frozenset({"FAILED"}),
        "CF_SUBJECT_CONFLICT": frozenset({"CREATE_COMPLETE"}),
    },
    "CHANGESET_EXECUTE": {
        "CLOUDFORMATION_STACK_FAILED": frozenset(
            {"CREATE_FAILED", "ROLLBACK_COMPLETE", "ROLLBACK_FAILED"}
        ),
        "CF_SUBJECT_CONFLICT": frozenset({"CREATE_COMPLETE"}),
    },
    "ASSET_PUBLISH": {
        "ASSET_SUBJECT_CONFLICT": frozenset({"RETAINED_OBJECT_CONFLICT"})
    },
    "AGENTCORE_HARDEN": {
        "AGENTCORE_UPDATE_FAILED": frozenset({"UPDATE_FAILED"}),
        "AGENTCORE_SUBJECT_CONFLICT": frozenset({"READY"}),
    },
    "IMAGE_PUBLISH": {
        "IMAGE_SCAN_FAILED": frozenset({"SCAN_POLICY_FAILED"}),
        "IMAGE_SIGNING_FAILED": frozenset({"SIGNATURE_VERIFICATION_FAILED"}),
        "IMAGE_SUBJECT_CONFLICT": frozenset({"IMMUTABLE_SUBJECT_CONFLICT"}),
        "IMAGE_PARTIAL_CLOSURE": frozenset({"RETAINED_PARTIAL_CLOSURE"}),
    },
    "IMAGE_OBSERVE": {
        "IMAGE_SCAN_FAILED": frozenset({"SCAN_POLICY_FAILED"}),
        "IMAGE_SIGNING_FAILED": frozenset({"SIGNATURE_VERIFICATION_FAILED"}),
    },
    "RUNTIME_CONTEXT_WRITE": {
        "RUNTIME_CONTEXT_CONFLICT": frozenset({"EXISTING_CONTENT_CONFLICT"})
    },
}
_FAILED_RETAINED_KIND_REASONS = {
    kind: frozenset(reason_statuses)
    for kind, reason_statuses in _FAILED_RETAINED_KIND_REASON_STATUSES.items()
}
_FAILED_RETAINED_REASONS = frozenset(
    reason
    for reasons in _FAILED_RETAINED_KIND_REASONS.values()
    for reason in reasons
)
_FAILED_RETAINED_REASON_PROVIDERS = {
    "CLOUDFORMATION_STACK_FAILED": "CLOUDFORMATION",
    "CLOUDFORMATION_CHANGESET_FAILED": "CLOUDFORMATION",
    "CF_SUBJECT_CONFLICT": "CLOUDFORMATION",
    "ASSET_SUBJECT_CONFLICT": "S3",
    "AGENTCORE_UPDATE_FAILED": "AGENTCORE",
    "AGENTCORE_SUBJECT_CONFLICT": "AGENTCORE",
    "IMAGE_SCAN_FAILED": "ECR",
    "IMAGE_SIGNING_FAILED": "ECR",
    "IMAGE_SUBJECT_CONFLICT": "ECR",
    "IMAGE_PARTIAL_CLOSURE": "ECR",
    "RUNTIME_CONTEXT_CONFLICT": "LOCAL_FILESYSTEM",
}
_FAILED_RETAINED_REASON_STATUSES = {
    reason: frozenset(
        status
        for reason_statuses in _FAILED_RETAINED_KIND_REASON_STATUSES.values()
        for candidate_reason, statuses in reason_statuses.items()
        if candidate_reason == reason
        for status in statuses
    )
    for reason in _FAILED_RETAINED_REASONS
}


def _validate_failure_provider_status(
    *,
    reason: str,
    provider: str,
    status: str,
) -> None:
    expected_provider = _FAILED_RETAINED_REASON_PROVIDERS[reason]
    if provider != expected_provider:
        raise ContractError("failure observation provider differs from its reason")
    if status not in _FAILED_RETAINED_REASON_STATUSES[reason]:
        raise ContractError("failure observation status differs from its reason")


@dataclass(frozen=True, slots=True)
class ReleaseStepFailureObservationV2:
    """Canonical authoritative observation of one terminal retained failure."""

    SCHEMA: ClassVar[str] = (
        "personal-operator.release-step-failure-observation.v2"
    )
    FIELDS: ClassVar[set[str]] = {
        "schema",
        "planSha256",
        "stepId",
        "subject",
        "operationSha256",
        "provider",
        "terminalStatus",
        "failureReason",
        "observerEvidenceSha256",
    }

    plan_sha256: str
    step_id: str
    subject: str
    operation_sha256: str
    provider: str
    terminal_status: str
    failure_reason: str
    observer_evidence_sha256: str

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "ReleaseStepFailureObservationV2":
        value = _exact_object(
            raw, cls.FIELDS, label="release step failure observation"
        )
        if value["schema"] != cls.SCHEMA:
            raise ContractError("release step failure observation schema is invalid")
        plan_sha256 = _text(
            value["planSha256"], field="failure plan digest", pattern=_SHA_64
        )
        step_id = _text(
            value["stepId"], field="failed step ID", pattern=_STEP_ID
        )
        subject = _release_subject(value["subject"])
        operation_sha256 = _text(
            value["operationSha256"],
            field="failed operation digest",
            pattern=_DIGEST,
        )
        provider = _text(value["provider"], field="failure provider")
        status = _text(value["terminalStatus"], field="terminal failure status")
        reason = _text(value["failureReason"], field="failure reason")
        if reason not in _FAILED_RETAINED_REASONS:
            raise ContractError("failure observation reason is not canonical")
        _validate_failure_provider_status(
            reason=reason,
            provider=provider,
            status=status,
        )
        observer_evidence_sha256 = _text(
            value["observerEvidenceSha256"],
            field="failure observer evidence digest",
            pattern=_SHA_64,
        )
        return cls(
            plan_sha256,
            step_id,
            subject,
            operation_sha256,
            provider,
            status,
            reason,
            observer_evidence_sha256,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ReleaseStepFailureObservationV2":
        return cls.from_mapping(parse_canonical_object(payload))

    def to_mapping(self) -> dict[str, str]:
        return {
            "schema": self.SCHEMA,
            "planSha256": self.plan_sha256,
            "stepId": self.step_id,
            "subject": self.subject,
            "operationSha256": self.operation_sha256,
            "provider": self.provider,
            "terminalStatus": self.terminal_status,
            "failureReason": self.failure_reason,
            "observerEvidenceSha256": self.observer_evidence_sha256,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def validate_transaction_step(
        self,
        plan: "ReleasePlanV2",
        transaction: "StagingTransactionV2",
    ) -> None:
        observation = ReleaseStepFailureObservationV2.from_bytes(self.to_bytes())
        plan = _canonical_release_plan_v2(plan)
        transaction = StagingTransactionV2.from_bytes(
            transaction.to_bytes(), plan=plan
        )
        if transaction.completed_step_count >= len(plan.steps):
            raise ContractError("failure observation has no exact next step")
        step = plan.steps[transaction.completed_step_count]
        completed_prefix_sha256 = _completed_prefix_sha256(
            [item.to_mapping() for item in transaction.completed_steps]
        )
        if transaction.state == "UNCERTAIN":
            expected_operation_sha256 = transaction.uncertain_operation_sha256
        elif transaction.state in {
            "NEW",
            "VERIFIED",
            "ABORTED_RETAINED",
            "ROLLED_BACK",
        }:
            raise ContractError(
                "failure observation requires an active exact step"
            )
        elif step.mutation:
            raise ContractError(
                "stable failure observation requires a read-only next step"
            )
        else:
            expected_operation_sha256 = _release_operation_sha256(
                plan.digest(),
                step,
                completed_prefix_sha256,
            )
        if (
            observation.plan_sha256 != plan.digest()
            or observation.step_id != step.step_id
        ):
            raise ContractError("failure observation plan or step differs")
        if observation.subject != step.subject:
            raise ContractError("failure observation subject differs")
        if observation.operation_sha256 != expected_operation_sha256:
            raise ContractError("failure observation operation differs")
        expected_statuses = _FAILED_RETAINED_KIND_REASON_STATUSES.get(
            step.kind, {}
        ).get(observation.failure_reason, frozenset())
        if observation.terminal_status not in expected_statuses:
            raise ContractError(
                "failure observation reason or status differs from the step kind"
            )


@dataclass(frozen=True, slots=True)
class StagingTransactionV2:
    """Plan-prefix release journal for a clean account with retained aborts."""

    SCHEMA: ClassVar[str] = "personal-operator.staging-transaction.v2"
    FIELDS: ClassVar[set[str]] = {
        "schema",
        "transactionId",
        "sourceCommit",
        "sourceTree",
        "account",
        "region",
        "state",
        "lastStableState",
        "planSha256",
        "completedStepCount",
        "completedSteps",
        "foundationInputsSha256",
        "agentCoreStackId",
        "runtimeImageDigest",
        "runtimeId",
        "runtimeVersion",
        "runtimeArn",
        "runtimeEndpointId",
        "runtimeContextSha256",
        "routerTargetStackId",
        "routerChangeSetId",
        "cronTargetStackId",
        "cronChangeSetId",
        "routerCronChangesetsSha256",
        "routerCronApplicationSha256",
        "schedulerTargetStackId",
        "schedulerChangeSetId",
        "schedulerChangesetSha256",
        "schedulerApplicationSha256",
        "webTargetStackId",
        "webChangeSetId",
        "webChangesetSha256",
        "webApplicationSha256",
        "verificationSha256",
        "rollbackBaselineSha256",
        "abortEvidenceSha256",
        "failedRetainedEvidenceSha256",
        "failureObservationSha256",
        "failedStepId",
        "failedSubject",
        "failedOperationSha256",
        "failureReason",
        "uncertainStepId",
        "uncertainOperationSha256",
        "revision",
    }

    transaction_id: str
    source_commit: str
    source_tree: str
    account: str
    region: str
    state: str
    last_stable_state: str
    plan_sha256: str
    completed_step_count: int
    completed_steps: tuple[CompletedReleaseStepV2, ...]
    foundation_inputs_sha256: str
    agent_core_stack_id: str
    runtime_image_digest: str
    runtime_id: str
    runtime_version: str
    runtime_arn: str
    runtime_endpoint_id: str
    runtime_context_sha256: str
    router_target_stack_id: str
    router_change_set_id: str
    cron_target_stack_id: str
    cron_change_set_id: str
    router_cron_changesets_sha256: str
    router_cron_application_sha256: str
    scheduler_target_stack_id: str
    scheduler_change_set_id: str
    scheduler_changeset_sha256: str
    scheduler_application_sha256: str
    web_target_stack_id: str
    web_change_set_id: str
    web_changeset_sha256: str
    web_application_sha256: str
    verification_sha256: str
    rollback_baseline_sha256: str
    abort_evidence_sha256: str
    failed_retained_evidence_sha256: str
    failure_observation_sha256: str
    failed_step_id: str
    failed_subject: str
    failed_operation_sha256: str
    failure_reason: str
    uncertain_step_id: str
    uncertain_operation_sha256: str
    revision: int

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        plan: ReleasePlanV2 | None = None,
    ) -> "StagingTransactionV2":
        value = _exact_object(raw, cls.FIELDS, label="staging transaction v2")
        if value["schema"] != cls.SCHEMA:
            raise ContractError("staging transaction v2 schema is invalid")
        commit = _text(value["sourceCommit"], field="source commit", pattern=_SHA_40)
        tree = _text(value["sourceTree"], field="source tree", pattern=_SHA_40)
        transaction_id = _text(value["transactionId"], field="transaction ID")
        if transaction_id != f"release_{commit}":
            raise ContractError("staging transaction v2 ID is not commit-bound")
        account = _account(value["account"])
        region = _region(value["region"])
        state = _text(value["state"], field="state")
        if state not in RELEASE_V2_TRANSACTION_STATES:
            raise ContractError("staging transaction v2 state is unknown")
        if state == "ROLLED_BACK":
            raise ContractError("CLEAN_ACCOUNT release cannot enter ROLLED_BACK")
        last_stable = _text(value["lastStableState"], field="last stable state")
        if last_stable not in RELEASE_V2_LINEAR_STATES:
            raise ContractError("staging transaction v2 last stable state is unknown")
        if state in RELEASE_V2_LINEAR_STATES and state != last_stable:
            raise ContractError("linear state and last stable state differ")
        plan_sha256 = _text(
            value["planSha256"], field="plan digest", pattern=_SHA_64
        )
        completed_count = _count(
            value["completedStepCount"], field="completed step count"
        )
        raw_completed = value["completedSteps"]
        if not isinstance(raw_completed, list) or len(raw_completed) != completed_count:
            raise ContractError("completed step count differs from its inventory")
        completed: list[CompletedReleaseStepV2] = []
        completed_ids: set[str] = set()
        for raw_step in raw_completed:
            step = _exact_object(
                raw_step,
                {"stepId", "evidenceSha256"},
                label="completed step",
            )
            step_id = _text(step["stepId"], field="completed step ID", pattern=_STEP_ID)
            if step_id in completed_ids:
                raise ContractError("completed step IDs are not unique")
            completed_ids.add(step_id)
            evidence = _text(
                step["evidenceSha256"],
                field="completed step evidence digest",
                pattern=_SHA_64,
            )
            completed.append(CompletedReleaseStepV2(step_id, evidence))
        foundation_inputs = _optional_sha256(
            value["foundationInputsSha256"], field="foundation inputs digest"
        )
        agent_core_stack_id = _optional_cloudformation_id(
            value["agentCoreStackId"],
            change_set=False,
            field="AgentCore stack ID",
        )
        if agent_core_stack_id:
            agent_core_stack_id = _cloudformation_stack_id(
                agent_core_stack_id,
                account=account,
                region=region,
                stack_name="OpenClawAgentCore",
                field="AgentCore stack ID",
            )
        image_digest = _text(value["runtimeImageDigest"], field="runtime image digest")
        if image_digest and _DIGEST.fullmatch(image_digest) is None:
            raise ContractError("runtime image digest is invalid")
        runtime_id = _text(value["runtimeId"], field="runtime ID")
        if runtime_id and _RUNTIME_ID.fullmatch(runtime_id) is None:
            raise ContractError("runtime ID is invalid")
        runtime_version = _text(value["runtimeVersion"], field="runtime version")
        if runtime_version and _RUNTIME_VERSION.fullmatch(runtime_version) is None:
            raise ContractError("runtime version is invalid")
        runtime_arn = _text(value["runtimeArn"], field="runtime ARN")
        if len({bool(runtime_id), bool(runtime_version), bool(runtime_arn)}) != 1:
            raise ContractError("runtime ID, version, and ARN must be atomic")
        if runtime_arn:
            runtime_arn_pattern = re.compile(
                rf"arn:aws:bedrock-agentcore:{re.escape(region)}:"
                rf"{re.escape(account)}:agent/"
                r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
                r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:[1-9][0-9]{0,4}"
            )
            if runtime_arn_pattern.fullmatch(runtime_arn) is None:
                raise ContractError("runtime ARN crosses its account or region")
            if runtime_arn.rsplit(":", 1)[-1] != runtime_version:
                raise ContractError("runtime ARN and version differ")
        endpoint_id = _text(value["runtimeEndpointId"], field="runtime endpoint ID")
        if endpoint_id and _RUNTIME_ID.fullmatch(endpoint_id) is None:
            raise ContractError("runtime endpoint ID is invalid")
        consumer_ids: dict[str, str] = {}
        for prefix, stack_name in (
            ("router", "OpenClawRouter"),
            ("cron", "OpenClawCron"),
            ("scheduler", "PersonalOperatorScheduler"),
            ("web", "PersonalOperatorWeb"),
        ):
            stack_field = f"{prefix}TargetStackId"
            change_set_field = f"{prefix}ChangeSetId"
            target_stack_id = _optional_cloudformation_id(
                value[stack_field],
                change_set=False,
                field=f"{prefix} target stack ID",
            )
            change_set_id = _optional_cloudformation_id(
                value[change_set_field],
                change_set=True,
                field=f"{prefix} change-set ID",
            )
            if bool(target_stack_id) != bool(change_set_id):
                raise ContractError(
                    f"{prefix} target stack and change-set IDs are not atomic"
                )
            if target_stack_id:
                target_stack_id = _cloudformation_stack_id(
                    target_stack_id,
                    account=account,
                    region=region,
                    stack_name=stack_name,
                    field=f"{prefix} target stack ID",
                )
                change_set_id = _cloudformation_change_set_id(
                    change_set_id,
                    account=account,
                    region=region,
                    source_commit=commit,
                    field=f"{prefix} change-set ID",
                )
            consumer_ids[stack_field] = target_stack_id
            consumer_ids[change_set_field] = change_set_id
        nonempty_stack_ids = [
            consumer_ids[field]
            for field in consumer_ids
            if field.endswith("TargetStackId") and consumer_ids[field]
        ]
        nonempty_change_set_ids = [
            consumer_ids[field]
            for field in consumer_ids
            if field.endswith("ChangeSetId") and consumer_ids[field]
        ]
        if len(set(nonempty_stack_ids)) != len(nonempty_stack_ids) or len(
            set(nonempty_change_set_ids)
        ) != len(nonempty_change_set_ids):
            raise ContractError("consumer CloudFormation IDs are not unique")
        evidence_fields = (
            ("runtimeContextSha256", "runtime context digest"),
            ("routerCronChangesetsSha256", "router cron changesets digest"),
            ("routerCronApplicationSha256", "router cron application digest"),
            ("schedulerChangesetSha256", "scheduler changeset digest"),
            ("schedulerApplicationSha256", "scheduler application digest"),
            ("webChangesetSha256", "web changeset digest"),
            ("webApplicationSha256", "web application digest"),
            ("verificationSha256", "verification digest"),
            ("rollbackBaselineSha256", "rollback baseline digest"),
        )
        evidence = {
            field: _optional_sha256(value[field], field=label)
            for field, label in evidence_fields
        }
        abort_evidence = _optional_sha256(
            value["abortEvidenceSha256"], field="abort evidence digest"
        )
        failed_retained_evidence = _optional_sha256(
            value["failedRetainedEvidenceSha256"],
            field="failed retained evidence digest",
        )
        failure_observation = _optional_sha256(
            value["failureObservationSha256"],
            field="failure observation digest",
        )
        failed_step = _text(value["failedStepId"], field="failed step ID")
        if failed_step and _STEP_ID.fullmatch(failed_step) is None:
            raise ContractError("failed step ID is invalid")
        failed_subject = _text(value["failedSubject"], field="failed subject")
        if failed_subject:
            failed_subject = _release_subject(failed_subject)
        failed_operation = _text(
            value["failedOperationSha256"], field="failed operation digest"
        )
        if failed_operation and _DIGEST.fullmatch(failed_operation) is None:
            raise ContractError("failed operation digest is invalid")
        failure_reason = _text(value["failureReason"], field="failure reason")
        failure_fields = (
            failed_retained_evidence,
            failure_observation,
            failed_step,
            failed_subject,
            failed_operation,
            failure_reason,
        )
        if state == "ABORTED_RETAINED":
            if bool(abort_evidence) == bool(failed_retained_evidence):
                raise ContractError(
                    "ABORTED_RETAINED requires exactly one terminal evidence type"
                )
            if failed_retained_evidence:
                if not all(failure_fields):
                    raise ContractError(
                        "failed retained evidence fields are not atomic"
                    )
                if failure_reason not in _FAILED_RETAINED_REASONS:
                    raise ContractError("failed retained reason is not canonical")
            elif any(failure_fields):
                raise ContractError(
                    "failure evidence fields are set for a clean retained abort"
                )
        elif abort_evidence or any(failure_fields):
            raise ContractError(
                "terminal retained evidence is set outside ABORTED_RETAINED"
            )
        uncertain_step = _text(value["uncertainStepId"], field="uncertain step ID")
        uncertain_operation = _text(
            value["uncertainOperationSha256"], field="uncertain operation digest"
        )
        if state == "UNCERTAIN":
            if (
                _STEP_ID.fullmatch(uncertain_step) is None
                or _DIGEST.fullmatch(uncertain_operation) is None
            ):
                raise ContractError("uncertain transaction lacks exact step and operation")
        elif uncertain_step or uncertain_operation:
            raise ContractError("uncertain fields are set outside UNCERTAIN")
        revision = _count(value["revision"], field="revision")
        if state == "UNCERTAIN" and revision < 1:
            raise ContractError("uncertain transaction revision is invalid")
        if state == "NEW" and (
            last_stable != "NEW"
            or completed_count
            or revision
            or any(
                (
                    foundation_inputs,
                    agent_core_stack_id,
                    image_digest,
                    runtime_id,
                    runtime_arn,
                    endpoint_id,
                    *consumer_ids.values(),
                    abort_evidence,
                    failed_retained_evidence,
                    failure_observation,
                    failed_step,
                    failed_subject,
                    failed_operation,
                    failure_reason,
                    *evidence.values(),
                )
            )
        ):
            raise ContractError("NEW staging transaction v2 contains later evidence")
        transaction = cls(
            transaction_id,
            commit,
            tree,
            account,
            region,
            state,
            last_stable,
            plan_sha256,
            completed_count,
            tuple(completed),
            foundation_inputs,
            agent_core_stack_id,
            image_digest,
            runtime_id,
            runtime_version,
            runtime_arn,
            endpoint_id,
            evidence["runtimeContextSha256"],
            consumer_ids["routerTargetStackId"],
            consumer_ids["routerChangeSetId"],
            consumer_ids["cronTargetStackId"],
            consumer_ids["cronChangeSetId"],
            evidence["routerCronChangesetsSha256"],
            evidence["routerCronApplicationSha256"],
            consumer_ids["schedulerTargetStackId"],
            consumer_ids["schedulerChangeSetId"],
            evidence["schedulerChangesetSha256"],
            evidence["schedulerApplicationSha256"],
            consumer_ids["webTargetStackId"],
            consumer_ids["webChangeSetId"],
            evidence["webChangesetSha256"],
            evidence["webApplicationSha256"],
            evidence["verificationSha256"],
            evidence["rollbackBaselineSha256"],
            abort_evidence,
            failed_retained_evidence,
            failure_observation,
            failed_step,
            failed_subject,
            failed_operation,
            failure_reason,
            uncertain_step,
            uncertain_operation,
            revision,
        )
        if plan is not None:
            transaction.validate_plan(plan)
        return transaction

    @classmethod
    def from_bytes(
        cls,
        payload: bytes,
        *,
        plan: ReleasePlanV2 | None = None,
    ) -> "StagingTransactionV2":
        return cls.from_mapping(parse_canonical_object(payload), plan=plan)

    def validate_plan(self, plan: ReleasePlanV2) -> None:
        self = StagingTransactionV2.from_bytes(self.to_bytes())
        plan = _canonical_release_plan_v2(plan)
        if (
            self.transaction_id,
            self.source_commit,
            self.source_tree,
            self.account,
            self.region,
        ) != (
            plan.transaction_id,
            plan.source_commit,
            plan.source_tree,
            plan.account,
            plan.region,
        ):
            raise ContractError("staging transaction v2 identity differs from the plan")
        if self.plan_sha256 != plan.digest():
            raise ContractError("staging transaction v2 plan digest differs")
        if self.completed_step_count > len(plan.steps):
            raise ContractError("completed step count exceeds the plan")
        expected_prefix = tuple(
            step.step_id for step in plan.steps[: self.completed_step_count]
        )
        actual_prefix = tuple(step.step_id for step in self.completed_steps)
        if actual_prefix != expected_prefix:
            raise ContractError("completed steps are not the exact plan prefix")

        expected_stable = "PREFLIGHTED"
        for phase in RELEASE_V2_PHASES:
            phase_end = max(
                step.ordinal for step in plan.steps if step.phase == phase
            ) + 1
            if self.completed_step_count >= phase_end:
                expected_stable = RELEASE_V2_PHASE_STATES[phase]
        if self.state == "NEW":
            if self.completed_step_count != 0 or self.last_stable_state != "NEW":
                raise ContractError("NEW state is not at the plan origin")
        elif self.last_stable_state != expected_stable:
            raise ContractError("last stable state is not at the plan phase boundary")
        if self.state in RELEASE_V2_LINEAR_STATES and self.state not in {
            "NEW",
            expected_stable,
        }:
            raise ContractError("stable state is not at the plan phase boundary")
        if self.state == "UNCERTAIN":
            if self.completed_step_count >= len(plan.steps):
                raise ContractError("uncertain transaction has no next plan step")
            next_step = plan.steps[self.completed_step_count]
            if not next_step.mutation:
                raise ContractError("uncertain next plan step is not a mutation")
            if self.uncertain_step_id != next_step.step_id:
                raise ContractError("uncertain step is not the exact next plan step")
            if self.uncertain_operation_sha256 != _release_operation_sha256(
                plan.digest(),
                next_step,
                _completed_prefix_sha256(
                    [step.to_mapping() for step in self.completed_steps]
                ),
            ):
                raise ContractError(
                    "uncertain operation is not bound to the exact next plan step"
                )
        if self.failed_retained_evidence_sha256:
            if self.completed_step_count >= len(plan.steps):
                raise ContractError("failed retained transaction has no failed step")
            failed_step = plan.steps[self.completed_step_count]
            if self.failed_step_id != failed_step.step_id:
                raise ContractError("failed retained step differs from the plan")
            if self.failed_subject != failed_step.subject:
                raise ContractError("failed retained subject differs from the plan")
            expected_operation = _release_operation_sha256(
                plan.digest(),
                failed_step,
                _completed_prefix_sha256(
                    [step.to_mapping() for step in self.completed_steps]
                ),
            )
            if self.failed_operation_sha256 != expected_operation:
                raise ContractError("failed retained operation differs from the plan")
            if self.failure_reason not in _FAILED_RETAINED_KIND_REASONS.get(
                failed_step.kind, frozenset()
            ):
                raise ContractError("failed retained reason differs from the step kind")
        if self.state == "ROLLED_BACK" and plan.release_mode == "CLEAN_ACCOUNT":
            raise ContractError("CLEAN_ACCOUNT release cannot enter ROLLED_BACK")

        foundation_end = max(
            step.ordinal for step in plan.steps if step.phase == "foundation"
        ) + 1
        if bool(self.agent_core_stack_id) != (
            self.completed_step_count >= foundation_end
        ):
            raise ContractError(
                "AgentCore stack ID has the wrong phase ownership"
            )
        consumer_identity_fields = {
            "OpenClawRouter": (
                self.router_target_stack_id,
                self.router_change_set_id,
            ),
            "OpenClawCron": (
                self.cron_target_stack_id,
                self.cron_change_set_id,
            ),
            "PersonalOperatorScheduler": (
                self.scheduler_target_stack_id,
                self.scheduler_change_set_id,
            ),
            "PersonalOperatorWeb": (
                self.web_target_stack_id,
                self.web_change_set_id,
            ),
        }
        for stack_name, identity in consumer_identity_fields.items():
            subject = (
                f"cfn:{plan.account}:{plan.region}:stack:{stack_name}:"
                f"release:{plan.source_commit}"
            )
            threshold = next(
                step.ordinal + 1
                for step in plan.steps
                if step.kind == "CHANGESET_CREATE" and step.subject == subject
            )
            expected_identity = self.completed_step_count >= threshold
            if any(identity) != all(identity) or bool(identity[0]) != expected_identity:
                raise ContractError(
                    f"{stack_name} CloudFormation IDs have the wrong step ownership"
                )

        completed_kinds = {
            step.kind for step in plan.steps[: self.completed_step_count]
        }
        expected_baseline = "BASELINE_OBSERVE" in completed_kinds
        if bool(self.rollback_baseline_sha256) != expected_baseline:
            raise ContractError("rollback baseline evidence has the wrong ownership")
        phase_evidence = {
            "foundation": ("foundation inputs", self.foundation_inputs_sha256),
            "image": ("runtime image", self.runtime_image_digest),
            "runtime": (
                "runtime identity",
                (
                    self.runtime_id
                    if self.runtime_id and self.runtime_version and self.runtime_arn
                    else ""
                ),
            ),
            "endpoint": ("runtime endpoint", self.runtime_endpoint_id),
            "context": ("runtime context", self.runtime_context_sha256),
            "router-cron-cs": (
                "router cron changesets",
                self.router_cron_changesets_sha256,
            ),
            "router-cron": (
                "router cron application",
                self.router_cron_application_sha256,
            ),
            "scheduler-cs": (
                "scheduler changeset",
                self.scheduler_changeset_sha256,
            ),
            "scheduler": (
                "scheduler application",
                self.scheduler_application_sha256,
            ),
            "web-cs": ("web changeset", self.web_changeset_sha256),
            "web": ("web application", self.web_application_sha256),
            "verify": ("verification", self.verification_sha256),
        }
        for phase in RELEASE_V2_PHASES:
            phase_end = max(
                step.ordinal for step in plan.steps if step.phase == phase
            ) + 1
            evidence_threshold = phase_end
            if phase == "runtime":
                evidence_threshold = min(
                    step.ordinal for step in plan.steps if step.phase == phase
                ) + 1
            expected = self.completed_step_count >= evidence_threshold
            label, actual = phase_evidence[phase]
            if bool(actual) != expected:
                raise ContractError(f"{label} evidence has the wrong phase ownership")
        if self.runtime_image_digest and (
            self.runtime_image_digest != plan.runtime_image_digest
        ):
            raise ContractError("runtime image evidence differs from the plan")

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
            "planSha256": self.plan_sha256,
            "completedStepCount": self.completed_step_count,
            "completedSteps": [step.to_mapping() for step in self.completed_steps],
            "foundationInputsSha256": self.foundation_inputs_sha256,
            "agentCoreStackId": self.agent_core_stack_id,
            "runtimeImageDigest": self.runtime_image_digest,
            "runtimeId": self.runtime_id,
            "runtimeVersion": self.runtime_version,
            "runtimeArn": self.runtime_arn,
            "runtimeEndpointId": self.runtime_endpoint_id,
            "runtimeContextSha256": self.runtime_context_sha256,
            "routerTargetStackId": self.router_target_stack_id,
            "routerChangeSetId": self.router_change_set_id,
            "cronTargetStackId": self.cron_target_stack_id,
            "cronChangeSetId": self.cron_change_set_id,
            "routerCronChangesetsSha256": self.router_cron_changesets_sha256,
            "routerCronApplicationSha256": self.router_cron_application_sha256,
            "schedulerTargetStackId": self.scheduler_target_stack_id,
            "schedulerChangeSetId": self.scheduler_change_set_id,
            "schedulerChangesetSha256": self.scheduler_changeset_sha256,
            "schedulerApplicationSha256": self.scheduler_application_sha256,
            "webTargetStackId": self.web_target_stack_id,
            "webChangeSetId": self.web_change_set_id,
            "webChangesetSha256": self.web_changeset_sha256,
            "webApplicationSha256": self.web_application_sha256,
            "verificationSha256": self.verification_sha256,
            "rollbackBaselineSha256": self.rollback_baseline_sha256,
            "abortEvidenceSha256": self.abort_evidence_sha256,
            "failedRetainedEvidenceSha256": self.failed_retained_evidence_sha256,
            "failureObservationSha256": self.failure_observation_sha256,
            "failedStepId": self.failed_step_id,
            "failedSubject": self.failed_subject,
            "failedOperationSha256": self.failed_operation_sha256,
            "failureReason": self.failure_reason,
            "uncertainStepId": self.uncertain_step_id,
            "uncertainOperationSha256": self.uncertain_operation_sha256,
            "revision": self.revision,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())


_ABORT_RETAINED_REASONS = frozenset(
    {
        "RELEASE_STOP_CONDITION",
        "SECURITY_REVIEW_FINDING",
        "EXTERNAL_GATE_OPEN",
        "OPERATOR_REQUEST",
    }
)


@dataclass(frozen=True, slots=True)
class RetainedReleaseStepV2:
    step_id: str
    subject: str

    def to_mapping(self) -> dict[str, str]:
        return {"stepId": self.step_id, "subject": self.subject}


@dataclass(frozen=True, slots=True)
class AbortRetainedEvidenceV2:
    """Canonical reason and exact retained prefix for a terminal clean abort."""

    SCHEMA: ClassVar[str] = "personal-operator.abort-retained-evidence.v2"
    FIELDS: ClassVar[set[str]] = {
        "schema",
        "planSha256",
        "completedPrefixSha256",
        "completedStepCount",
        "retainedSteps",
        "stableState",
        "stopReason",
    }

    plan_sha256: str
    completed_prefix_sha256: str
    completed_step_count: int
    retained_steps: tuple[RetainedReleaseStepV2, ...]
    stable_state: str
    stop_reason: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AbortRetainedEvidenceV2":
        value = _exact_object(raw, cls.FIELDS, label="abort retained evidence")
        if value["schema"] != cls.SCHEMA:
            raise ContractError("abort retained evidence schema is invalid")
        plan_sha256 = _text(
            value["planSha256"], field="plan digest", pattern=_SHA_64
        )
        prefix_sha256 = _text(
            value["completedPrefixSha256"],
            field="completed prefix digest",
            pattern=_SHA_64,
        )
        count = _count(value["completedStepCount"], field="completed step count")
        raw_steps = value["retainedSteps"]
        if not isinstance(raw_steps, list) or len(raw_steps) != count:
            raise ContractError("retained step inventory differs from its count")
        retained: list[RetainedReleaseStepV2] = []
        for raw_step in raw_steps:
            step = _exact_object(
                raw_step, {"stepId", "subject"}, label="retained step"
            )
            retained.append(
                RetainedReleaseStepV2(
                    _text(step["stepId"], field="retained step ID", pattern=_STEP_ID),
                    _release_subject(step["subject"]),
                )
            )
        stable_state = _text(value["stableState"], field="stable state")
        if stable_state not in RELEASE_V2_LINEAR_STATES:
            raise ContractError("abort retained stable state is unknown")
        stop_reason = _text(value["stopReason"], field="stop reason")
        if stop_reason not in _ABORT_RETAINED_REASONS:
            raise ContractError("abort retained stop reason is not canonical")
        return cls(
            plan_sha256,
            prefix_sha256,
            count,
            tuple(retained),
            stable_state,
            stop_reason,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "AbortRetainedEvidenceV2":
        return cls.from_mapping(parse_canonical_object(payload))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "planSha256": self.plan_sha256,
            "completedPrefixSha256": self.completed_prefix_sha256,
            "completedStepCount": self.completed_step_count,
            "retainedSteps": [step.to_mapping() for step in self.retained_steps],
            "stableState": self.stable_state,
            "stopReason": self.stop_reason,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def validate_transaction(
        self,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
    ) -> None:
        self = AbortRetainedEvidenceV2.from_bytes(self.to_bytes())
        plan = _canonical_release_plan_v2(plan)
        transaction = StagingTransactionV2.from_bytes(
            transaction.to_bytes(), plan=plan
        )
        if transaction.state in {
            "NEW",
            "UNCERTAIN",
            "VERIFIED",
            "ABORTED_RETAINED",
            "ROLLED_BACK",
        }:
            raise ContractError("transaction is not a stable nonterminal prefix")
        expected_prefix = _completed_prefix_sha256(
            [step.to_mapping() for step in transaction.completed_steps]
        )
        expected_retained = tuple(
            RetainedReleaseStepV2(step.step_id, step.subject)
            for step in plan.steps[: transaction.completed_step_count]
        )
        if self.plan_sha256 != plan.digest():
            raise ContractError("abort evidence plan differs")
        if self.completed_prefix_sha256 != expected_prefix:
            raise ContractError("abort evidence completed prefix differs")
        if self.completed_step_count != transaction.completed_step_count:
            raise ContractError("abort evidence completed step count differs")
        if self.retained_steps != expected_retained:
            raise ContractError("abort evidence retained subjects differ")
        if self.stable_state != transaction.last_stable_state:
            raise ContractError("abort evidence stable state differs")


@dataclass(frozen=True, slots=True)
class FailedRetainedEvidenceV2:
    """Exact prefix plus canonical terminal failure for retained abort."""

    SCHEMA: ClassVar[str] = "personal-operator.failed-retained-evidence.v2"
    FIELDS: ClassVar[set[str]] = {
        "schema",
        "planSha256",
        "completedPrefixSha256",
        "completedStepCount",
        "stableState",
        "failureObservation",
    }

    plan_sha256: str
    completed_prefix_sha256: str
    completed_step_count: int
    stable_state: str
    failure_observation: ReleaseStepFailureObservationV2

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "FailedRetainedEvidenceV2":
        value = _exact_object(raw, cls.FIELDS, label="failed retained evidence")
        if value["schema"] != cls.SCHEMA:
            raise ContractError("failed retained evidence schema is invalid")
        plan_sha256 = _text(
            value["planSha256"],
            field="failed retained plan digest",
            pattern=_SHA_64,
        )
        prefix_sha256 = _text(
            value["completedPrefixSha256"],
            field="failed retained completed prefix digest",
            pattern=_SHA_64,
        )
        count = _count(
            value["completedStepCount"],
            field="failed retained completed step count",
        )
        stable_state = _text(
            value["stableState"], field="failed retained stable state"
        )
        if stable_state not in RELEASE_V2_LINEAR_STATES:
            raise ContractError("failed retained stable state is unknown")
        observation = ReleaseStepFailureObservationV2.from_mapping(
            value["failureObservation"]
        )
        return cls(
            plan_sha256,
            prefix_sha256,
            count,
            stable_state,
            observation,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "FailedRetainedEvidenceV2":
        return cls.from_mapping(parse_canonical_object(payload))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "planSha256": self.plan_sha256,
            "completedPrefixSha256": self.completed_prefix_sha256,
            "completedStepCount": self.completed_step_count,
            "stableState": self.stable_state,
            "failureObservation": self.failure_observation.to_mapping(),
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def validate_transaction(
        self,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
    ) -> None:
        evidence = FailedRetainedEvidenceV2.from_bytes(self.to_bytes())
        plan = _canonical_release_plan_v2(plan)
        transaction = StagingTransactionV2.from_bytes(
            transaction.to_bytes(), plan=plan
        )
        if transaction.completed_step_count >= len(plan.steps):
            raise ContractError("failed retained evidence has no exact next step")
        next_step = plan.steps[transaction.completed_step_count]
        stable_read_only = (
            transaction.state
            not in {
                "NEW",
                "UNCERTAIN",
                "VERIFIED",
                "ABORTED_RETAINED",
                "ROLLED_BACK",
            }
            and not next_step.mutation
        )
        if transaction.state != "UNCERTAIN" and not stable_read_only:
            raise ContractError(
                "failed retained evidence requires exact mutation intent or a "
                "stable read-only next step"
            )
        expected_prefix = _completed_prefix_sha256(
            [step.to_mapping() for step in transaction.completed_steps]
        )
        if evidence.plan_sha256 != plan.digest():
            raise ContractError("failed retained evidence plan differs")
        if evidence.completed_prefix_sha256 != expected_prefix:
            raise ContractError("failed retained evidence completed prefix differs")
        if evidence.completed_step_count != transaction.completed_step_count:
            raise ContractError("failed retained evidence completed step count differs")
        if evidence.stable_state != transaction.last_stable_state:
            raise ContractError("failed retained evidence stable state differs")
        evidence.failure_observation.validate_transaction_step(plan, transaction)


@dataclass(frozen=True, slots=True)
class ResolvedMutationRequestV2:
    """Static plan request plus exact generated inputs for a private driver file."""

    SCHEMA: ClassVar[str] = "personal-operator.resolved-mutation-request.v2"
    FIELDS: ClassVar[set[str]] = {
        "schema",
        "mutationRequest",
        "sourceCommit",
        "sourceTree",
        "account",
        "region",
        "stepPhase",
        "requestArtifactSize",
        "expectedTemplateSha256",
        "expectedTemplateParameterSha256",
        "expectedObservedRequestSha256",
        "expectedContentSha256",
        "foundationRuntimeInputs",
        "agentCoreStackId",
        "runtimeImageDigest",
        "runtimeId",
        "runtimeVersion",
        "runtimeArn",
        "runtimeEndpointId",
        "runtimeContextSha256",
        "routerTargetStackId",
        "routerChangeSetId",
        "cronTargetStackId",
        "cronChangeSetId",
        "routerCronChangesetsSha256",
        "routerCronApplicationSha256",
        "schedulerTargetStackId",
        "schedulerChangeSetId",
        "schedulerChangesetSha256",
        "schedulerApplicationSha256",
        "webTargetStackId",
        "webChangeSetId",
        "webChangesetSha256",
        "webApplicationSha256",
    }

    mutation_request: MutationRequestV2
    source_commit: str
    source_tree: str
    account: str
    region: str
    step_phase: str
    request_artifact_size: int
    expected_template_sha256: str
    expected_template_parameter_sha256: str
    expected_observed_request_sha256: str
    expected_content_sha256: str
    foundation_runtime_inputs: FoundationRuntimeInputsV1 | None
    agent_core_stack_id: str
    runtime_image_digest: str
    runtime_id: str
    runtime_version: str
    runtime_arn: str
    runtime_endpoint_id: str
    runtime_context_sha256: str
    router_target_stack_id: str
    router_change_set_id: str
    cron_target_stack_id: str
    cron_change_set_id: str
    router_cron_changesets_sha256: str
    router_cron_application_sha256: str
    scheduler_target_stack_id: str
    scheduler_change_set_id: str
    scheduler_changeset_sha256: str
    scheduler_application_sha256: str
    web_target_stack_id: str
    web_change_set_id: str
    web_changeset_sha256: str
    web_application_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ResolvedMutationRequestV2":
        value = _exact_object(raw, cls.FIELDS, label="resolved mutation request")
        if value["schema"] != cls.SCHEMA:
            raise ContractError("resolved mutation request schema is invalid")
        raw_request = value["mutationRequest"]
        if not isinstance(raw_request, Mapping):
            raise ContractError("resolved mutation request is malformed")
        request = MutationRequestV2.from_mapping(raw_request)
        source_commit = _text(
            value["sourceCommit"], field="source commit", pattern=_SHA_40
        )
        source_tree = _text(
            value["sourceTree"], field="source tree", pattern=_SHA_40
        )
        account = _account(value["account"])
        region = _region(value["region"])
        step_phase = _text(value["stepPhase"], field="step phase")
        if step_phase not in RELEASE_V2_PHASES:
            raise ContractError("resolved mutation step phase is unknown")
        request_artifact_size = _count(
            value["requestArtifactSize"],
            field="request artifact size",
            minimum=1,
        )
        expected_template_sha256 = _optional_sha256(
            value["expectedTemplateSha256"],
            field="expected update template digest",
        )
        expected_template_parameter_sha256 = _optional_sha256(
            value["expectedTemplateParameterSha256"],
            field="expected template/parameter digest",
        )
        expected_observed_request_sha256 = _optional_sha256(
            value["expectedObservedRequestSha256"],
            field="expected observed request digest",
        )
        expected_content_sha256 = _optional_sha256(
            value["expectedContentSha256"],
            field="expected content digest",
        )
        raw_foundation = value["foundationRuntimeInputs"]
        if raw_foundation == {}:
            foundation_inputs = None
        elif isinstance(raw_foundation, Mapping):
            foundation_inputs = FoundationRuntimeInputsV1.from_mapping(raw_foundation)
        else:
            raise ContractError("resolved foundation runtime inputs are malformed")
        agent_core_stack_id = _optional_cloudformation_id(
            value["agentCoreStackId"],
            change_set=False,
            field="AgentCore stack ID",
        )
        if agent_core_stack_id:
            agent_core_stack_id = _cloudformation_stack_id(
                agent_core_stack_id,
                account=account,
                region=region,
                stack_name="OpenClawAgentCore",
                field="AgentCore stack ID",
            )
        if foundation_inputs is not None and (
            foundation_inputs.agent_core_stack_id != agent_core_stack_id
        ):
            raise ContractError(
                "resolved AgentCore stack ID differs from foundation inputs"
            )
        image_digest = _text(
            value["runtimeImageDigest"], field="runtime image digest"
        )
        if image_digest and _DIGEST.fullmatch(image_digest) is None:
            raise ContractError("runtime image digest is invalid")
        runtime_id = _text(value["runtimeId"], field="runtime ID")
        runtime_version = _text(value["runtimeVersion"], field="runtime version")
        runtime_arn = _text(value["runtimeArn"], field="runtime ARN")
        if len({bool(runtime_id), bool(runtime_version), bool(runtime_arn)}) != 1:
            raise ContractError("runtime ID, version, and ARN must be atomic")
        if runtime_id:
            if _RUNTIME_ID.fullmatch(runtime_id) is None:
                raise ContractError("runtime ID is invalid")
            if _RUNTIME_VERSION.fullmatch(runtime_version) is None:
                raise ContractError("runtime version is invalid")
            generic_runtime_arn = re.compile(
                r"arn:aws:bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:agent/"
                r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
                r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:[1-9][0-9]{0,4}"
            )
            if generic_runtime_arn.fullmatch(runtime_arn) is None:
                raise ContractError("runtime ARN is invalid")
            if runtime_arn.rsplit(":", 1)[-1] != runtime_version:
                raise ContractError("runtime ARN and version differ")
        endpoint_id = _text(value["runtimeEndpointId"], field="runtime endpoint ID")
        if endpoint_id and _RUNTIME_ID.fullmatch(endpoint_id) is None:
            raise ContractError("runtime endpoint ID is invalid")
        consumer_ids: dict[str, str] = {}
        for prefix, stack_name in (
            ("router", "OpenClawRouter"),
            ("cron", "OpenClawCron"),
            ("scheduler", "PersonalOperatorScheduler"),
            ("web", "PersonalOperatorWeb"),
        ):
            stack_field = f"{prefix}TargetStackId"
            change_set_field = f"{prefix}ChangeSetId"
            target_stack_id = _optional_cloudformation_id(
                value[stack_field],
                change_set=False,
                field=f"{prefix} target stack ID",
            )
            change_set_id = _optional_cloudformation_id(
                value[change_set_field],
                change_set=True,
                field=f"{prefix} change-set ID",
            )
            if bool(target_stack_id) != bool(change_set_id):
                raise ContractError(
                    f"resolved {prefix} CloudFormation IDs are not atomic"
                )
            if target_stack_id:
                target_stack_id = _cloudformation_stack_id(
                    target_stack_id,
                    account=account,
                    region=region,
                    stack_name=stack_name,
                    field=f"{prefix} target stack ID",
                )
                change_set_id = _cloudformation_change_set_id(
                    change_set_id,
                    account=account,
                    region=region,
                    source_commit=source_commit,
                    field=f"{prefix} change-set ID",
                )
            consumer_ids[stack_field] = target_stack_id
            consumer_ids[change_set_field] = change_set_id
        digest_fields = (
            ("runtimeContextSha256", "runtime context digest"),
            ("routerCronChangesetsSha256", "router cron changesets digest"),
            ("routerCronApplicationSha256", "router cron application digest"),
            ("schedulerChangesetSha256", "scheduler changeset digest"),
            ("schedulerApplicationSha256", "scheduler application digest"),
            ("webChangesetSha256", "web changeset digest"),
            ("webApplicationSha256", "web application digest"),
        )
        digests = {
            field: _optional_sha256(value[field], field=label)
            for field, label in digest_fields
        }
        return cls(
            request,
            source_commit,
            source_tree,
            account,
            region,
            step_phase,
            request_artifact_size,
            expected_template_sha256,
            expected_template_parameter_sha256,
            expected_observed_request_sha256,
            expected_content_sha256,
            foundation_inputs,
            agent_core_stack_id,
            image_digest,
            runtime_id,
            runtime_version,
            runtime_arn,
            endpoint_id,
            digests["runtimeContextSha256"],
            consumer_ids["routerTargetStackId"],
            consumer_ids["routerChangeSetId"],
            consumer_ids["cronTargetStackId"],
            consumer_ids["cronChangeSetId"],
            digests["routerCronChangesetsSha256"],
            digests["routerCronApplicationSha256"],
            consumer_ids["schedulerTargetStackId"],
            consumer_ids["schedulerChangeSetId"],
            digests["schedulerChangesetSha256"],
            digests["schedulerApplicationSha256"],
            consumer_ids["webTargetStackId"],
            consumer_ids["webChangeSetId"],
            digests["webChangesetSha256"],
            digests["webApplicationSha256"],
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ResolvedMutationRequestV2":
        return cls.from_mapping(parse_canonical_object(payload))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "mutationRequest": self.mutation_request.to_mapping(),
            "sourceCommit": self.source_commit,
            "sourceTree": self.source_tree,
            "account": self.account,
            "region": self.region,
            "stepPhase": self.step_phase,
            "requestArtifactSize": self.request_artifact_size,
            "expectedTemplateSha256": self.expected_template_sha256,
            "expectedTemplateParameterSha256": (
                self.expected_template_parameter_sha256
            ),
            "expectedObservedRequestSha256": (
                self.expected_observed_request_sha256
            ),
            "expectedContentSha256": self.expected_content_sha256,
            "foundationRuntimeInputs": (
                self.foundation_runtime_inputs.to_mapping()
                if self.foundation_runtime_inputs is not None
                else {}
            ),
            "agentCoreStackId": self.agent_core_stack_id,
            "runtimeImageDigest": self.runtime_image_digest,
            "runtimeId": self.runtime_id,
            "runtimeVersion": self.runtime_version,
            "runtimeArn": self.runtime_arn,
            "runtimeEndpointId": self.runtime_endpoint_id,
            "runtimeContextSha256": self.runtime_context_sha256,
            "routerTargetStackId": self.router_target_stack_id,
            "routerChangeSetId": self.router_change_set_id,
            "cronTargetStackId": self.cron_target_stack_id,
            "cronChangeSetId": self.cron_change_set_id,
            "routerCronChangesetsSha256": self.router_cron_changesets_sha256,
            "routerCronApplicationSha256": self.router_cron_application_sha256,
            "schedulerTargetStackId": self.scheduler_target_stack_id,
            "schedulerChangeSetId": self.scheduler_change_set_id,
            "schedulerChangesetSha256": self.scheduler_changeset_sha256,
            "schedulerApplicationSha256": self.scheduler_application_sha256,
            "webTargetStackId": self.web_target_stack_id,
            "webChangeSetId": self.web_change_set_id,
            "webChangesetSha256": self.web_changeset_sha256,
            "webApplicationSha256": self.web_application_sha256,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def validate_transaction(
        self,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
    ) -> None:
        self = ResolvedMutationRequestV2.from_bytes(self.to_bytes())
        plan = _canonical_release_plan_v2(plan)
        transaction = StagingTransactionV2.from_bytes(
            transaction.to_bytes(), plan=plan
        )
        if transaction.state in {
            "NEW",
            "VERIFIED",
            "ABORTED_RETAINED",
            "ROLLED_BACK",
        }:
            raise ContractError("transaction has no resolvable mutation request")
        prefix = _completed_prefix_sha256(
            [step.to_mapping() for step in transaction.completed_steps]
        )
        self.mutation_request.validate_plan(
            plan,
            completed_step_count=transaction.completed_step_count,
            completed_prefix_sha256=prefix,
        )
        if (
            self.source_commit,
            self.source_tree,
            self.account,
            self.region,
        ) != (
            plan.source_commit,
            plan.source_tree,
            plan.account,
            plan.region,
        ):
            raise ContractError("resolved mutation plan identity differs")
        next_step = plan.steps[transaction.completed_step_count]
        if self.step_phase != next_step.phase:
            raise ContractError("resolved mutation step phase differs from the plan")
        if (
            self.expected_template_sha256,
            self.expected_template_parameter_sha256,
            self.expected_observed_request_sha256,
            self.expected_content_sha256,
        ) != (
            next_step.expected_template_sha256,
            next_step.expected_template_parameter_sha256,
            next_step.expected_observed_request_sha256,
            next_step.expected_content_sha256,
        ):
            raise ContractError(
                "resolved mutation next-step expectations differ from the plan"
            )
        artifact = next(
            (
                item
                for item in plan.artifacts
                if item.path == self.mutation_request.request_artifact
            ),
            None,
        )
        if artifact is None:
            raise ContractError("resolved mutation request artifact is not in the plan")
        if self.request_artifact_size != artifact.size:
            raise ContractError("resolved mutation request artifact size differs")
        if transaction.state == "UNCERTAIN" and (
            self.mutation_request.operation_sha256
            != transaction.uncertain_operation_sha256
        ):
            raise ContractError("resolved mutation operation differs from intent")
        if bool(self.foundation_runtime_inputs) != bool(
            transaction.foundation_inputs_sha256
        ):
            raise ContractError("resolved foundation inputs have wrong ownership")
        if self.foundation_runtime_inputs is not None:
            self.foundation_runtime_inputs.validate_plan_identity(plan)
            if (
                self.foundation_runtime_inputs.digest()
                != transaction.foundation_inputs_sha256
            ):
                raise ContractError("resolved foundation inputs differ from journal")
        actual_state = (
            self.agent_core_stack_id,
            self.runtime_image_digest,
            self.runtime_id,
            self.runtime_version,
            self.runtime_arn,
            self.runtime_endpoint_id,
            self.runtime_context_sha256,
            self.router_target_stack_id,
            self.router_change_set_id,
            self.cron_target_stack_id,
            self.cron_change_set_id,
            self.router_cron_changesets_sha256,
            self.router_cron_application_sha256,
            self.scheduler_target_stack_id,
            self.scheduler_change_set_id,
            self.scheduler_changeset_sha256,
            self.scheduler_application_sha256,
            self.web_target_stack_id,
            self.web_change_set_id,
            self.web_changeset_sha256,
            self.web_application_sha256,
        )
        expected_state = (
            transaction.agent_core_stack_id,
            transaction.runtime_image_digest,
            transaction.runtime_id,
            transaction.runtime_version,
            transaction.runtime_arn,
            transaction.runtime_endpoint_id,
            transaction.runtime_context_sha256,
            transaction.router_target_stack_id,
            transaction.router_change_set_id,
            transaction.cron_target_stack_id,
            transaction.cron_change_set_id,
            transaction.router_cron_changesets_sha256,
            transaction.router_cron_application_sha256,
            transaction.scheduler_target_stack_id,
            transaction.scheduler_change_set_id,
            transaction.scheduler_changeset_sha256,
            transaction.scheduler_application_sha256,
            transaction.web_target_stack_id,
            transaction.web_change_set_id,
            transaction.web_changeset_sha256,
            transaction.web_application_sha256,
        )
        if actual_state != expected_state:
            raise ContractError("resolved mutation generated inputs differ from journal")


PRIVATE_MUTATION_ENVELOPE_MAGIC = b"PO-PRIVATE-MUTATION-V2\x00"
PRIVATE_MUTATION_ENVELOPE_HEADER_BYTES = 4
MAX_PRIVATE_MUTATION_ARTIFACT_BYTES = 8 * 1024 * 1024 * 1024
_PRIVATE_MUTATION_STREAM_CHUNK_BYTES = 1024 * 1024
_PRIVATE_MUTATION_RESERVED_FIELDS = (
    b"operationSha256",
    b"driverRequestSha256",
)
_PRIVATE_MUTATION_RESERVED_TAIL_BYTES = max(
    len(field) for field in _PRIVATE_MUTATION_RESERVED_FIELDS
) - 1


def _read_exact_descriptor(descriptor: int, size: int, *, label: str) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(_PRIVATE_MUTATION_STREAM_CHUNK_BYTES, remaining))
        if not chunk:
            raise ContractError(f"private mutation envelope {label} is truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _same_file_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _scan_private_mutation_artifact(
    prior_tail: bytes,
    chunk: bytes,
) -> bytes:
    window = prior_tail + chunk
    if any(field in window for field in _PRIVATE_MUTATION_RESERVED_FIELDS):
        raise ContractError(
            "private mutation request artifact contains a reserved operation field"
        )
    return window[-_PRIVATE_MUTATION_RESERVED_TAIL_BYTES:]


@dataclass(frozen=True, slots=True)
class PrivateMutationEnvelopeV2:
    """Validated metadata for one header-plus-raw-artifact driver file."""

    resolved_request: ResolvedMutationRequestV2
    resolved_request_sha256: str
    request_artifact_offset: int
    request_artifact_size: int
    request_artifact_sha256: str
    envelope_size: int

    @classmethod
    @contextmanager
    def open_verified(
        cls,
        path: Path,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        scratch_dir: Path,
    ) -> Iterator["VerifiedPrivateMutationV2"]:
        """Yield an authority-bearing, unlinked read-only artifact snapshot."""

        verified = _open_verified_private_mutation(
            Path(path),
            plan=plan,
            transaction=transaction,
            scratch_dir=Path(scratch_dir),
            require_uncertain_intent=True,
        )
        try:
            yield verified
        finally:
            verified.close()

    @classmethod
    def from_path(
        cls,
        path: Path,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        scratch_dir: Path | None = None,
    ) -> "PrivateMutationEnvelopeV2":
        """Return non-authorizing diagnostics after closing the sealed snapshot."""

        target = Path(path)
        verified = _open_verified_private_mutation(
            target,
            plan=plan,
            transaction=transaction,
            scratch_dir=(
                Path(scratch_dir)
                if scratch_dir is not None
                else target.parent / ".private-mutation-snapshots"
            ),
            require_uncertain_intent=False,
        )
        try:
            return verified.metadata
        finally:
            verified.close()


_VERIFIED_PRIVATE_MUTATION_TOKEN = object()


class VerifiedPrivateMutationV2:
    """Capability over one validated, unlinked, read-only artifact snapshot."""

    __slots__ = ("_metadata", "_descriptor", "_closed", "_active")

    def __init__(
        self,
        metadata: PrivateMutationEnvelopeV2,
        descriptor: int,
        *,
        _token: object,
    ) -> None:
        if _token is not _VERIFIED_PRIVATE_MUTATION_TOKEN:
            raise ContractError("verified private mutation capability is not constructible")
        self._metadata = metadata
        self._descriptor = descriptor
        self._closed = False
        self._active = False

    @property
    def metadata(self) -> PrivateMutationEnvelopeV2:
        self._require_open()
        return self._metadata

    @property
    def resolved_request(self) -> ResolvedMutationRequestV2:
        return self.metadata.resolved_request

    def _require_open(self) -> None:
        if self._closed:
            raise ContractError("verified private mutation capability is closed")
        snapshot = os.fstat(self._descriptor)
        if (
            not stat.S_ISREG(snapshot.st_mode)
            or snapshot.st_size != self._metadata.request_artifact_size
        ):
            raise ContractError("verified private mutation snapshot is invalid")

    def reset(self) -> None:
        self._require_open()
        if self._active:
            raise ContractError("verified private mutation artifact is already streaming")
        os.lseek(self._descriptor, 0, os.SEEK_SET)

    def read_artifact_bytes(self, *, limit: int) -> bytes:
        self._require_open()
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < self._metadata.request_artifact_size
        ):
            raise ContractError("verified private mutation artifact exceeds the read limit")
        if self._active:
            raise ContractError("verified private mutation artifact is already streaming")
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        payload = _read_exact_descriptor(
            self._descriptor,
            self._metadata.request_artifact_size,
            label="snapshot artifact",
        )
        if os.read(self._descriptor, 1):
            raise ContractError("verified private mutation snapshot has trailing bytes")
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        if hashlib.sha256(payload).hexdigest() != self._metadata.request_artifact_sha256:
            raise ContractError("verified private mutation snapshot digest differs")
        return payload

    def iter_artifact_chunks(
        self,
        *,
        chunk_size: int = _PRIVATE_MUTATION_STREAM_CHUNK_BYTES,
    ) -> Iterator[bytes]:
        self._require_open()
        if (
            isinstance(chunk_size, bool)
            or not isinstance(chunk_size, int)
            or not 1 <= chunk_size <= _PRIVATE_MUTATION_STREAM_CHUNK_BYTES
        ):
            raise ContractError("verified private mutation chunk size is invalid")
        if self._active:
            raise ContractError("verified private mutation artifact is already streaming")
        self._active = True
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        remaining = self._metadata.request_artifact_size
        try:
            while remaining:
                chunk = os.read(self._descriptor, min(chunk_size, remaining))
                if not chunk:
                    raise ContractError(
                        "verified private mutation snapshot is truncated"
                    )
                remaining -= len(chunk)
                yield chunk
            if os.read(self._descriptor, 1):
                raise ContractError(
                    "verified private mutation snapshot has trailing bytes"
                )
        finally:
            if not self._closed:
                os.lseek(self._descriptor, 0, os.SEEK_SET)
            self._active = False

    def close(self) -> None:
        if not self._closed:
            os.close(self._descriptor)
            self._descriptor = -1
            self._closed = True


def _open_verified_private_mutation(
    target: Path,
    *,
    plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
    scratch_dir: Path,
    require_uncertain_intent: bool,
) -> VerifiedPrivateMutationV2:
    source_descriptor = -1
    directory_descriptor = -1
    snapshot_writer = -1
    snapshot_reader = -1
    snapshot_name = f"private-mutation-{uuid.uuid4().hex}.snapshot"
    snapshot_linked = False
    try:
        source_flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            source_descriptor = os.open(target, source_flags)
        except OSError as error:
            raise ContractError(
                f"private mutation envelope is not a regular file: {target}"
            ) from error
        source_before = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_before.st_mode):
            raise ContractError(
                f"private mutation envelope is not a regular file: {target}"
            )
        prefix_size = (
            len(PRIVATE_MUTATION_ENVELOPE_MAGIC)
            + PRIVATE_MUTATION_ENVELOPE_HEADER_BYTES
        )
        if source_before.st_size <= prefix_size:
            raise ContractError("private mutation envelope is truncated")
        magic = _read_exact_descriptor(
            source_descriptor,
            len(PRIVATE_MUTATION_ENVELOPE_MAGIC),
            label="magic",
        )
        if magic != PRIVATE_MUTATION_ENVELOPE_MAGIC:
            raise ContractError("private mutation envelope magic is invalid")
        header_length_bytes = _read_exact_descriptor(
            source_descriptor,
            PRIVATE_MUTATION_ENVELOPE_HEADER_BYTES,
            label="header length",
        )
        header_size = struct.unpack(">I", header_length_bytes)[0]
        if not 1 <= header_size <= MAX_CONTRACT_BYTES:
            raise ContractError("private mutation envelope header size is invalid")
        artifact_offset = prefix_size + header_size
        if source_before.st_size <= artifact_offset:
            raise ContractError("private mutation envelope artifact is empty")
        artifact_size = source_before.st_size - artifact_offset
        if artifact_size > MAX_PRIVATE_MUTATION_ARTIFACT_BYTES:
            raise ContractError("private mutation envelope artifact exceeds the limit")
        header_bytes = _read_exact_descriptor(
            source_descriptor, header_size, label="header"
        )
        resolved = ResolvedMutationRequestV2.from_bytes(header_bytes)
        canonical_transaction = StagingTransactionV2.from_bytes(
            transaction.to_bytes(), plan=plan
        )
        resolved.validate_transaction(plan, canonical_transaction)
        if require_uncertain_intent and (
            canonical_transaction.state != "UNCERTAIN"
            or resolved.mutation_request.step_id
            != canonical_transaction.uncertain_step_id
            or resolved.mutation_request.operation_sha256
            != canonical_transaction.uncertain_operation_sha256
        ):
            raise ContractError(
                "authority-bearing private mutation requires the exact UNCERTAIN intent"
            )
        if artifact_size != resolved.request_artifact_size:
            raise ContractError("private mutation envelope artifact size differs")

        scratch_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            directory_descriptor = os.open(scratch_dir, directory_flags)
        except OSError as error:
            raise ContractError("private mutation scratch is not a directory") from error
        directory_stat = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != os.geteuid()
            or stat.S_IMODE(directory_stat.st_mode) & 0o077
        ):
            raise ContractError("private mutation scratch is not a directory")
        snapshot_writer = os.open(
            snapshot_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        snapshot_linked = True
        os.fchmod(snapshot_writer, 0o600)
        artifact_digest = hashlib.sha256()
        reserved_tail = b""
        remaining = artifact_size
        while remaining:
            chunk = os.read(
                source_descriptor,
                min(_PRIVATE_MUTATION_STREAM_CHUNK_BYTES, remaining),
            )
            if not chunk:
                raise ContractError("private mutation envelope artifact is truncated")
            reserved_tail = _scan_private_mutation_artifact(reserved_tail, chunk)
            artifact_digest.update(chunk)
            _write_all(snapshot_writer, chunk)
            remaining -= len(chunk)
        if os.read(source_descriptor, 1):
            raise ContractError("private mutation envelope has trailing bytes")
        os.fsync(snapshot_writer)
        source_after = os.fstat(source_descriptor)
        if not _same_file_snapshot(source_before, source_after):
            raise ContractError("private mutation envelope changed while reading")
        artifact_sha256 = artifact_digest.hexdigest()
        if artifact_sha256 != resolved.mutation_request.request_sha256:
            raise ContractError("private mutation envelope artifact digest differs")
        os.close(snapshot_writer)
        snapshot_writer = -1
        snapshot_reader = os.open(
            snapshot_name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_descriptor,
        )
        snapshot_stat = os.fstat(snapshot_reader)
        if (
            not stat.S_ISREG(snapshot_stat.st_mode)
            or snapshot_stat.st_size != artifact_size
            or stat.S_IMODE(snapshot_stat.st_mode) != 0o600
            or snapshot_stat.st_uid != os.geteuid()
            or snapshot_stat.st_nlink != 1
        ):
            raise ContractError("private mutation snapshot is not sealed")
        os.unlink(snapshot_name, dir_fd=directory_descriptor)
        snapshot_linked = False
        if os.fstat(snapshot_reader).st_nlink != 0:
            raise ContractError("private mutation snapshot remains path-addressable")
        os.fsync(directory_descriptor)
        snapshot_digest = hashlib.sha256()
        snapshot_remaining = artifact_size
        while snapshot_remaining:
            chunk = os.read(
                snapshot_reader,
                min(_PRIVATE_MUTATION_STREAM_CHUNK_BYTES, snapshot_remaining),
            )
            if not chunk:
                raise ContractError("private mutation snapshot is truncated")
            snapshot_digest.update(chunk)
            snapshot_remaining -= len(chunk)
        if os.read(snapshot_reader, 1):
            raise ContractError("private mutation snapshot has trailing bytes")
        if snapshot_digest.hexdigest() != artifact_sha256:
            raise ContractError("private mutation snapshot digest differs")
        os.lseek(snapshot_reader, 0, os.SEEK_SET)
        metadata = PrivateMutationEnvelopeV2(
            resolved,
            hashlib.sha256(header_bytes).hexdigest(),
            artifact_offset,
            artifact_size,
            artifact_sha256,
            source_before.st_size,
        )
        verified = VerifiedPrivateMutationV2(
            metadata,
            snapshot_reader,
            _token=_VERIFIED_PRIVATE_MUTATION_TOKEN,
        )
        snapshot_reader = -1
        return verified
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if snapshot_writer >= 0:
            os.close(snapshot_writer)
        if snapshot_reader >= 0:
            os.close(snapshot_reader)
        if snapshot_linked and directory_descriptor >= 0:
            try:
                os.unlink(snapshot_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


ReleaseContract = (
    ProductionObservationConfigV1
    | RuntimeContextV3
    | RuntimeImageEvidence
    | TrustedLambdaAssetV2
    | StagingTransactionV1
    | ReleasePlanV2
    | FoundationRuntimeInputsV1
    | MutationRequestV2
    | StagingTransactionV2
    | ReleaseStepObservationV2
    | ReleaseStepFailureObservationV2
    | AbortRetainedEvidenceV2
    | FailedRetainedEvidenceV2
    | ResolvedMutationRequestV2
)


def parse_release_contract(payload: bytes) -> ReleaseContract:
    """Parse and fully validate any supported canonical release artifact."""

    value = parse_canonical_object(payload)
    schema = value.get("schema")
    parsers = {
        ProductionObservationConfigV1.SCHEMA: (
            ProductionObservationConfigV1.from_mapping
        ),
        RuntimeContextV3.SCHEMA: RuntimeContextV3.from_mapping,
        RuntimeImageEvidence.SCHEMA: RuntimeImageEvidence.from_mapping,
        TrustedLambdaAssetV2.SCHEMA: TrustedLambdaAssetV2.from_mapping,
        StagingTransactionV1.SCHEMA: StagingTransactionV1.from_mapping,
        ReleasePlanV2.SCHEMA: ReleasePlanV2.from_mapping,
        FoundationRuntimeInputsV1.SCHEMA: FoundationRuntimeInputsV1.from_mapping,
        MutationRequestV2.SCHEMA: MutationRequestV2.from_mapping,
        StagingTransactionV2.SCHEMA: StagingTransactionV2.from_mapping,
        ReleaseStepObservationV2.SCHEMA: ReleaseStepObservationV2.from_mapping,
        ReleaseStepFailureObservationV2.SCHEMA: (
            ReleaseStepFailureObservationV2.from_mapping
        ),
        AbortRetainedEvidenceV2.SCHEMA: AbortRetainedEvidenceV2.from_mapping,
        FailedRetainedEvidenceV2.SCHEMA: FailedRetainedEvidenceV2.from_mapping,
        ResolvedMutationRequestV2.SCHEMA: ResolvedMutationRequestV2.from_mapping,
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


def write_new_private_mutation_envelope(
    path: Path,
    *,
    resolved_request: ResolvedMutationRequestV2,
    request_artifact_path: Path,
    plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
) -> PrivateMutationEnvelopeV2:
    """Stream one validated request artifact behind its canonical small header."""

    try:
        resolved = ResolvedMutationRequestV2.from_bytes(resolved_request.to_bytes())
    except (AttributeError, TypeError, ValueError) as error:
        raise ContractError("resolved mutation request is not canonical") from error
    resolved.validate_transaction(plan, transaction)
    header = resolved.to_bytes()
    if not 1 <= len(header) <= MAX_CONTRACT_BYTES:
        raise ContractError("private mutation envelope header size is invalid")

    source = Path(request_artifact_path)
    source_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        source_descriptor = os.open(source, source_flags)
    except OSError as error:
        raise ContractError(
            f"private mutation request artifact is not a regular file: {source}"
        ) from error
    target = Path(path)
    temporary: Path | None = None
    try:
        source_before = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_before.st_mode):
            raise ContractError(
                f"private mutation request artifact is not a regular file: {source}"
            )
        if source_before.st_size != resolved.request_artifact_size:
            raise ContractError("private mutation request artifact size differs")
        if source_before.st_size > MAX_PRIVATE_MUTATION_ARTIFACT_BYTES:
            raise ContractError("private mutation request artifact exceeds the limit")

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        target_descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        artifact_digest = hashlib.sha256()
        reserved_tail = b""
        copied = 0
        try:
            _write_all(target_descriptor, PRIVATE_MUTATION_ENVELOPE_MAGIC)
            _write_all(target_descriptor, struct.pack(">I", len(header)))
            _write_all(target_descriptor, header)
            while True:
                chunk = os.read(
                    source_descriptor, _PRIVATE_MUTATION_STREAM_CHUNK_BYTES
                )
                if not chunk:
                    break
                copied += len(chunk)
                if copied > MAX_PRIVATE_MUTATION_ARTIFACT_BYTES:
                    raise ContractError(
                        "private mutation request artifact exceeds the limit"
                    )
                reserved_tail = _scan_private_mutation_artifact(
                    reserved_tail, chunk
                )
                artifact_digest.update(chunk)
                _write_all(target_descriptor, chunk)
            os.fsync(target_descriptor)
        finally:
            os.close(target_descriptor)

        source_after = os.fstat(source_descriptor)
        if not _same_file_snapshot(source_before, source_after):
            raise ContractError("private mutation request artifact changed while reading")
        if copied != resolved.request_artifact_size:
            raise ContractError("private mutation request artifact size differs")
        artifact_sha256 = artifact_digest.hexdigest()
        if artifact_sha256 != resolved.mutation_request.request_sha256:
            raise ContractError("private mutation request artifact digest differs")
        try:
            os.link(temporary, target)
        except FileExistsError as error:
            raise ContractError(
                f"private mutation envelope already exists: {target}"
            ) from error
        _fsync_directory(target.parent)
        artifact_offset = (
            len(PRIVATE_MUTATION_ENVELOPE_MAGIC)
            + PRIVATE_MUTATION_ENVELOPE_HEADER_BYTES
            + len(header)
        )
        return PrivateMutationEnvelopeV2(
            resolved,
            hashlib.sha256(header).hexdigest(),
            artifact_offset,
            copied,
            artifact_sha256,
            artifact_offset + copied,
        )
    finally:
        os.close(source_descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
