"""Injected AgentCore evidence adapter for one immutable runtime release."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

from release_tools.contracts import (
    ContractError,
    RuntimeConfigurationV1,
    RuntimeContextV3,
    expected_execution_role_arn,
)


REQUIRED_REGION = "eu-west-1"
RUNTIME_NAME = "personal_operator_bridge"

_COMMIT = re.compile(r"[0-9a-f]{40}")
_ACCOUNT = re.compile(r"[0-9]{12}")
_RUNTIME_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}")
_VERSION = re.compile(r"[1-9][0-9]{0,4}")
_INVOCATION_ARN = re.compile(
    r"arn:aws(?:-[^:]+)?:bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:agent/"
    r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
    r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:[1-9][0-9]{0,4}"
)
_PENDING = {"CREATING", "UPDATING"}
_FAILED = {"CREATE_FAILED", "UPDATE_FAILED", "DELETING"}
_KNOWN = _PENDING | _FAILED | {"READY"}
_MUTATING_METHODS = frozenset({"update_agent_runtime"})
_RETRYABLE_ERROR_CODES = frozenset(
    {
        "PriorRequestNotComplete",
        "RequestLimitExceeded",
        "RequestTimeout",
        "RequestTimeoutException",
        "SlowDown",
        "TooManyRequestsException",
    }
)


class AgentCoreClient(Protocol):
    def list_agent_runtimes(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_agent_runtime(self, **kwargs: Any) -> dict[str, Any]: ...

    def list_agent_runtime_endpoints(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_agent_runtime_endpoint(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_resource_policy(self, **kwargs: Any) -> dict[str, Any]: ...

    def update_agent_runtime(self, **kwargs: Any) -> dict[str, Any]: ...


class AgentCoreEvidenceError(RuntimeError):
    """Live AgentCore state disproves the release contract."""


class AgentCoreEvidenceAbsent(AgentCoreEvidenceError):
    """The exact runtime or endpoint is authoritatively absent."""


class AgentCoreRuntimeAbsent(AgentCoreEvidenceAbsent):
    """The exact retained runtime is absent."""


class AgentCoreEndpointAbsent(AgentCoreEvidenceAbsent):
    """The exact release endpoint is absent while its runtime may remain."""


class AgentCoreEvidenceIncomplete(AgentCoreEvidenceError):
    """A known asynchronous AgentCore operation has not completed."""


class AgentCoreEvidenceAmbiguous(AgentCoreEvidenceError):
    """Live state cannot prove one exact runtime or endpoint."""


@dataclass(frozen=True, slots=True)
class HardenedRuntimeIdentity:
    """Exact READY runtime identity safe to persist for endpoint creation."""

    runtime_id: str
    runtime_version: str
    runtime_arn: str

    def __post_init__(self) -> None:
        if _RUNTIME_ID.fullmatch(self.runtime_id) is None:
            raise AgentCoreEvidenceError("hardened runtime ID is not canonical")
        if _VERSION.fullmatch(self.runtime_version) is None:
            raise AgentCoreEvidenceError("hardened runtime version is not canonical")
        if (
            _INVOCATION_ARN.fullmatch(self.runtime_arn) is None
            or self.runtime_arn.rsplit(":", 1)[-1] != self.runtime_version
        ):
            raise AgentCoreEvidenceError(
                "hardened runtime ARN is not the exact versioned invocation ARN"
            )

    def to_mapping(self) -> dict[str, str]:
        return {
            "runtimeId": self.runtime_id,
            "runtimeVersion": self.runtime_version,
            "runtimeArn": self.runtime_arn,
        }


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentCoreEvidenceError(f"{label} response is malformed")
    return value


def _list(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise AgentCoreEvidenceError(f"{label} response is malformed")
    return value


def _is_botocore_transport_error(error: BaseException) -> bool:
    """Recognize botocore transport failures without importing site packages.

    The production entrypoint initially imports this module under ``-I -S``;
    its audited dependency path is loaded only later. Exact module/MRO names
    preserve that bootstrap boundary while covering botocore's connection and
    HTTP-client subclasses.
    """

    return any(
        base.__module__ == "botocore.exceptions"
        and base.__name__ in {"ConnectionError", "HTTPClientError"}
        for base in type(error).__mro__
    )


def _identity(
    *,
    source_commit: str,
    account: str,
    region: str,
    runtime_id: str | None = None,
    runtime_version: str | None = None,
) -> None:
    if _COMMIT.fullmatch(source_commit) is None:
        raise AgentCoreEvidenceError("source commit is not canonical")
    if _ACCOUNT.fullmatch(account) is None or account == "000000000000":
        raise AgentCoreEvidenceError("release account is not canonical")
    if region != REQUIRED_REGION:
        raise AgentCoreEvidenceError(
            f"release region must be exactly {REQUIRED_REGION}"
        )
    if runtime_id is not None and _RUNTIME_ID.fullmatch(runtime_id) is None:
        raise AgentCoreEvidenceError("runtime ID is not canonical")
    if (
        runtime_version is not None
        and _VERSION.fullmatch(runtime_version) is None
    ):
        raise AgentCoreEvidenceError("runtime version is not canonical")


def _ready(status: Any, *, subject: str) -> None:
    if status not in _KNOWN:
        raise AgentCoreEvidenceError(
            f"{subject} returned unknown status {status!r}"
        )
    if status in _PENDING:
        raise AgentCoreEvidenceIncomplete(
            f"{subject} status {status} is not ready"
        )
    if status in _FAILED:
        raise AgentCoreEvidenceError(
            f"{subject} entered failed status {status}"
        )


def _runtime_resource_arn(*, account: str, region: str, runtime_id: str) -> str:
    return (
        f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime_id}"
    )


def _endpoint_resource_arn(
    *, account: str, region: str, runtime_id: str, endpoint_id: Any
) -> str:
    if (
        not isinstance(endpoint_id, str)
        or _RUNTIME_ID.fullmatch(endpoint_id) is None
    ):
        raise AgentCoreEvidenceError("runtime endpoint ID is not canonical")
    runtime_arn = _runtime_resource_arn(
        account=account,
        region=region,
        runtime_id=runtime_id,
    )
    return f"{runtime_arn}/runtime-endpoint/{endpoint_id}"


def _requires_mmdsv2(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"requireMMDSV2"}
        and value["requireMMDSV2"] is True
    )


def _is_unhardened_metadata(value: Any) -> bool:
    return value is None or value == {} or (
        isinstance(value, dict)
        and set(value) == {"requireMMDSV2"}
        and (
            value["requireMMDSV2"] is False
            or value["requireMMDSV2"] is None
        )
    )


def _sorted_runtime_configuration(
    runtime: Mapping[str, Any],
    *,
    allow_service_s3_endpoint: bool = False,
) -> dict[str, Any]:
    """Copy and canonicalize only the order-insensitive VPC identifier sets."""

    configuration = {
        field: runtime.get(field) for field in RuntimeConfigurationV1.FIELDS
    }
    for field in ("authorizerConfiguration", "requestHeaderConfiguration"):
        if configuration[field] is None:
            configuration[field] = {}
    network = configuration.get("networkConfiguration")
    if not isinstance(network, Mapping):
        return configuration
    network_copy = dict(network)
    vpc = network_copy.get("networkModeConfig")
    if isinstance(vpc, Mapping):
        vpc_copy = dict(vpc)
        if "requireServiceS3Endpoint" in vpc_copy:
            disposition = vpc_copy["requireServiceS3Endpoint"]
            if disposition is not False and not (
                allow_service_s3_endpoint and disposition is True
            ):
                raise AgentCoreEvidenceError(
                    "runtime service-managed S3 endpoint is not explicitly disabled"
                )
            del vpc_copy["requireServiceS3Endpoint"]
        for field in ("securityGroups", "subnets"):
            identifiers = vpc_copy.get(field)
            if isinstance(identifiers, list) and all(
                isinstance(value, str) for value in identifiers
            ):
                vpc_copy[field] = sorted(identifiers)
        network_copy["networkModeConfig"] = vpc_copy
    configuration["networkConfiguration"] = network_copy
    return configuration


class AgentCoreEvidenceAdapter:
    """Validate AgentCore using only a caller-supplied compatible client."""

    def __init__(
        self,
        client: AgentCoreClient,
        *,
        sleep: Callable[[float], None] = time.sleep,
        runtime_ready_attempts: int = 60,
        runtime_ready_interval_seconds: float = 5.0,
    ) -> None:
        if runtime_ready_attempts < 1:
            raise ValueError("runtime_ready_attempts must be positive")
        if runtime_ready_interval_seconds < 0:
            raise ValueError("runtime_ready_interval_seconds cannot be negative")
        self._client = client
        self._sleep = sleep
        self._runtime_ready_attempts = runtime_ready_attempts
        self._runtime_ready_interval_seconds = runtime_ready_interval_seconds

    def _call(self, method_name: str, **arguments: Any) -> dict[str, Any]:
        method = getattr(self._client, method_name, None)
        if method is None or not callable(method):
            raise AgentCoreEvidenceError(
                f"injected AgentCore adapter lacks {method_name}"
            )
        try:
            return _object(method(**arguments), label=method_name)
        except (TimeoutError, ConnectionError) as error:
            effect = (
                "has unknown effect; authoritative reconciliation is required"
                if method_name in _MUTATING_METHODS
                else "ended without authoritative evidence"
            )
            raise AgentCoreEvidenceAmbiguous(
                f"{method_name} {effect}"
            ) from error
        except Exception as error:
            if _is_botocore_transport_error(error):
                effect = (
                    "has unknown effect; authoritative reconciliation is required"
                    if method_name in _MUTATING_METHODS
                    else "ended without authoritative evidence"
                )
                raise AgentCoreEvidenceAmbiguous(
                    f"{method_name} {effect}"
                ) from error
            if isinstance(error, AgentCoreEvidenceError):
                if method_name in _MUTATING_METHODS:
                    raise AgentCoreEvidenceAmbiguous(
                        f"{method_name} has unknown effect; authoritative "
                        "reconciliation is required"
                    ) from error
                raise
            response = getattr(error, "response", None)
            body = response.get("Error") if isinstance(response, dict) else None
            code = body.get("Code") if isinstance(body, dict) else None
            metadata = (
                response.get("ResponseMetadata")
                if isinstance(response, dict)
                else None
            )
            status = (
                metadata.get("HTTPStatusCode")
                if isinstance(metadata, dict)
                else None
            )
            retryable = (
                isinstance(status, int)
                and (status == 429 or 500 <= status <= 599)
            ) or (
                isinstance(code, str)
                and (
                    "throttl" in code.casefold()
                    or code in _RETRYABLE_ERROR_CODES
                )
            )
            if retryable:
                effect = (
                    "has unknown effect; authoritative reconciliation is required"
                    if method_name in _MUTATING_METHODS
                    else "ended without authoritative evidence"
                )
                raise AgentCoreEvidenceAmbiguous(
                    f"{method_name} {effect}"
                ) from error
            if code == "ResourceNotFoundException":
                if method_name == "get_resource_policy":
                    raise AgentCoreEvidenceError(
                        "required runtime command deny policy is absent"
                    ) from error
                absent = (
                    AgentCoreEndpointAbsent
                    if method_name == "get_agent_runtime_endpoint"
                    else AgentCoreRuntimeAbsent
                )
                raise absent(
                    f"{method_name} exact subject is absent"
                ) from error
            raise AgentCoreEvidenceError(
                f"{method_name} failed without authoritative evidence"
            ) from error

    def _assert_command_deny_policy(self, resource_arn: Any) -> None:
        if not isinstance(resource_arn, str) or not resource_arn:
            raise AgentCoreEvidenceError(
                "runtime command deny policy subject is malformed"
            )
        response = self._call("get_resource_policy", resourceArn=resource_arn)
        encoded = response.get("policy")
        if not isinstance(encoded, str) or not encoded:
            raise AgentCoreEvidenceError(
                "runtime command deny policy is missing or malformed"
            )
        try:
            policy = json.loads(encoded)
        except (TypeError, ValueError) as error:
            raise AgentCoreEvidenceError(
                "runtime command deny policy is missing or malformed"
            ) from error
        expected = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "DenyRuntimeCommandExecution",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": [
                        "bedrock-agentcore:InvokeAgentRuntimeCommand",
                        "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
                    ],
                    "Resource": resource_arn,
                }
            ],
        }
        if policy != expected:
            raise AgentCoreEvidenceError(
                "runtime command deny policy differs from the reviewed boundary"
            )

    def assert_endpoint_name_available(
        self,
        *,
        runtime_id: str,
        source_commit: str,
        account: str,
        region: str,
    ) -> None:
        """Refuse to create or retarget an already allocated release name."""

        _identity(
            source_commit=source_commit,
            account=account,
            region=region,
            runtime_id=runtime_id,
        )
        endpoint_name = f"release_{source_commit}"
        response = self._call(
            "list_agent_runtime_endpoints",
            agentRuntimeId=runtime_id,
            maxResults=100,
        )
        if response.get("nextToken"):
            raise AgentCoreEvidenceAmbiguous(
                "endpoint lookup was paginated"
            )
        endpoints = _list(
            response.get("runtimeEndpoints"), label="runtime endpoints"
        )
        matching: list[dict[str, Any]] = []
        for item in endpoints:
            endpoint = _object(item, label="runtime endpoint")
            if endpoint.get("name") == endpoint_name:
                matching.append(endpoint)
        if len(matching) > 1:
            raise AgentCoreEvidenceAmbiguous(
                "duplicate release endpoint names were returned"
            )
        if matching:
            raise AgentCoreEvidenceError(
                f"release endpoint name collision: {endpoint_name}"
            )

    def assert_runtime_name_absent(
        self,
        *,
        source_commit: str,
        account: str,
        region: str,
    ) -> None:
        """Prove no runtime with the stable release name exists anywhere."""

        _identity(
            source_commit=source_commit,
            account=account,
            region=region,
        )
        response = self._call("list_agent_runtimes", maxResults=100)
        if response.get("nextToken"):
            raise AgentCoreEvidenceAmbiguous("runtime lookup was paginated")
        runtimes = _list(response.get("agentRuntimes"), label="agent runtimes")
        for raw in runtimes:
            runtime = _object(raw, label="agent runtime summary")
            name = runtime.get("agentRuntimeName")
            if not isinstance(name, str) or not name:
                raise AgentCoreEvidenceError(
                    "runtime inventory contains a malformed name"
                )
            if name == RUNTIME_NAME:
                raise AgentCoreEvidenceError(
                    "stable release runtime still exists"
                )

    @staticmethod
    def _expected_runtime_configuration(
        *,
        account: str,
        region: str,
        runtime_image_uri: str,
        expected_subnet_ids: Sequence[str],
        expected_security_group_ids: Sequence[str],
        expected_environment_variables: Mapping[str, str],
        expected_idle_runtime_session_timeout: int,
        expected_max_lifetime: int,
    ) -> RuntimeConfigurationV1:
        return RuntimeConfigurationV1.from_mapping(
            {
                "agentRuntimeArtifact": {
                    "containerConfiguration": {
                        "containerUri": runtime_image_uri
                    }
                },
                "authorizerConfiguration": {},
                "environmentVariables": expected_environment_variables,
                "filesystemConfigurations": [
                    {"sessionStorage": {"mountPath": "/mnt/workspace"}}
                ],
                "lifecycleConfiguration": {
                    "idleRuntimeSessionTimeout": (
                        expected_idle_runtime_session_timeout
                    ),
                    "maxLifetime": expected_max_lifetime,
                },
                "networkConfiguration": {
                    "networkMode": "VPC",
                    "networkModeConfig": {
                        "securityGroups": sorted(expected_security_group_ids),
                        "subnets": sorted(expected_subnet_ids),
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

    def _validate_runtime(
        self,
        runtime: Mapping[str, Any],
        *,
        source_commit: str,
        account: str,
        region: str,
        runtime_id: str,
        runtime_version: str,
        runtime_image_uri: str,
        expected_subnet_ids: Sequence[str],
        expected_security_group_ids: Sequence[str],
        expected_environment_variables: Mapping[str, str],
        expected_idle_runtime_session_timeout: int,
        expected_max_lifetime: int,
        allow_unhardened_metadata: bool = False,
        allow_service_s3_endpoint: bool = False,
    ) -> RuntimeConfigurationV1:
        _identity(
            source_commit=source_commit,
            account=account,
            region=region,
            runtime_id=runtime_id,
            runtime_version=runtime_version,
        )
        execution_role_arn = expected_execution_role_arn(account, region)
        try:
            expected_configuration = self._expected_runtime_configuration(
                account=account,
                region=region,
                runtime_image_uri=runtime_image_uri,
                expected_subnet_ids=expected_subnet_ids,
                expected_security_group_ids=expected_security_group_ids,
                expected_environment_variables=expected_environment_variables,
                expected_idle_runtime_session_timeout=(
                    expected_idle_runtime_session_timeout
                ),
                expected_max_lifetime=expected_max_lifetime,
            )
        except (ContractError, TypeError) as error:
            raise AgentCoreEvidenceError(
                f"expected runtime configuration is invalid: {error}"
            ) from error
        for field in ("authorizerConfiguration", "requestHeaderConfiguration"):
            value = runtime.get(field)
            if value not in (None, {}):
                raise AgentCoreEvidenceError(
                    f"runtime {field} grants unreviewed authority"
                )
        metadata = runtime.get("metadataConfiguration")
        if not _requires_mmdsv2(metadata) and not (
            allow_unhardened_metadata
            and _is_unhardened_metadata(metadata)
        ):
            raise AgentCoreEvidenceError(
                "runtime metadata configuration does not require MMDSv2"
            )
        _ready(runtime.get("status"), subject="runtime")
        if runtime.get("agentRuntimeId") != runtime_id:
            raise AgentCoreEvidenceError("runtime ID differs from the request")
        if runtime.get("agentRuntimeName") != RUNTIME_NAME:
            raise AgentCoreEvidenceError("runtime name is not stable")
        if runtime.get("agentRuntimeVersion") != runtime_version:
            raise AgentCoreEvidenceError("runtime version differs from the request")
        runtime_arn_pattern = re.compile(
            rf"arn:aws:bedrock-agentcore:{re.escape(region)}:"
            rf"{re.escape(account)}:agent/"
            r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
            r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:"
            rf"{re.escape(runtime_version)}"
        )
        runtime_arn = str(runtime.get("agentRuntimeArn") or "")
        if runtime_arn_pattern.fullmatch(runtime_arn) is None:
            raise AgentCoreEvidenceError(
                "runtime ARN differs from the exact requested version"
            )
        if runtime.get("roleArn") != execution_role_arn:
            raise AgentCoreEvidenceError("runtime role differs from the release role")
        self._assert_command_deny_policy(
            _runtime_resource_arn(
                account=account,
                region=region,
                runtime_id=runtime_id,
            )
        )
        live_mapping = _sorted_runtime_configuration(
            runtime,
            allow_service_s3_endpoint=allow_service_s3_endpoint,
        )
        if allow_unhardened_metadata and not _requires_mmdsv2(metadata):
            live_mapping["metadataConfiguration"] = {"requireMMDSV2": True}
        try:
            live_configuration = RuntimeConfigurationV1.from_mapping(
                live_mapping,
                runtime_image_uri=runtime_image_uri,
                account=account,
                region=region,
            )
        except ContractError as error:
            raise AgentCoreEvidenceError(
                f"live runtime configuration is invalid: {error}"
            ) from error
        if live_configuration != expected_configuration:
            raise AgentCoreEvidenceError(
                "live runtime configuration differs from reviewed release configuration"
            )
        return live_configuration

    def _collect_runtime(
        self,
        *,
        source_commit: str,
        account: str,
        region: str,
        runtime_id: str,
        runtime_version: str,
        runtime_image_uri: str,
        expected_subnet_ids: Sequence[str],
        expected_security_group_ids: Sequence[str],
        expected_environment_variables: Mapping[str, str],
        expected_idle_runtime_session_timeout: int,
        expected_max_lifetime: int,
    ) -> tuple[dict[str, Any], RuntimeConfigurationV1]:
        _identity(
            source_commit=source_commit,
            account=account,
            region=region,
            runtime_id=runtime_id,
            runtime_version=runtime_version,
        )
        try:
            self._expected_runtime_configuration(
                account=account,
                region=region,
                runtime_image_uri=runtime_image_uri,
                expected_subnet_ids=expected_subnet_ids,
                expected_security_group_ids=expected_security_group_ids,
                expected_environment_variables=expected_environment_variables,
                expected_idle_runtime_session_timeout=(
                    expected_idle_runtime_session_timeout
                ),
                expected_max_lifetime=expected_max_lifetime,
            )
        except (ContractError, TypeError) as error:
            raise AgentCoreEvidenceError(
                f"expected runtime configuration is invalid: {error}"
            ) from error
        runtime = self._call(
            "get_agent_runtime",
            agentRuntimeId=runtime_id,
            agentRuntimeVersion=runtime_version,
        )
        live_configuration = self._validate_runtime(
            runtime,
            source_commit=source_commit,
            account=account,
            region=region,
            runtime_id=runtime_id,
            runtime_version=runtime_version,
            runtime_image_uri=runtime_image_uri,
            expected_subnet_ids=expected_subnet_ids,
            expected_security_group_ids=expected_security_group_ids,
            expected_environment_variables=expected_environment_variables,
            expected_idle_runtime_session_timeout=(
                expected_idle_runtime_session_timeout
            ),
            expected_max_lifetime=expected_max_lifetime,
        )
        return runtime, live_configuration

    def harden_runtime_mmdsv2(
        self,
        *,
        source_commit: str,
        account: str,
        region: str,
        runtime_id: str,
        runtime_version: str,
        runtime_image_uri: str,
        expected_subnet_ids: Sequence[str],
        expected_security_group_ids: Sequence[str],
        expected_environment_variables: Mapping[str, str],
        expected_idle_runtime_session_timeout: int,
        expected_max_lifetime: int,
    ) -> HardenedRuntimeIdentity:
        """Harden one exact runtime and return its exact READY identity."""

        _identity(
            source_commit=source_commit,
            account=account,
            region=region,
            runtime_id=runtime_id,
            runtime_version=runtime_version,
        )
        try:
            self._expected_runtime_configuration(
                account=account,
                region=region,
                runtime_image_uri=runtime_image_uri,
                expected_subnet_ids=expected_subnet_ids,
                expected_security_group_ids=expected_security_group_ids,
                expected_environment_variables=expected_environment_variables,
                expected_idle_runtime_session_timeout=(
                    expected_idle_runtime_session_timeout
                ),
                expected_max_lifetime=expected_max_lifetime,
            )
        except (ContractError, TypeError) as error:
            raise AgentCoreEvidenceError(
                f"expected runtime configuration is invalid: {error}"
            ) from error
        initial = self._call(
            "get_agent_runtime",
            agentRuntimeId=runtime_id,
            agentRuntimeVersion=runtime_version,
        )
        self._validate_runtime(
            initial,
            source_commit=source_commit,
            account=account,
            region=region,
            runtime_id=runtime_id,
            runtime_version=runtime_version,
            runtime_image_uri=runtime_image_uri,
            expected_subnet_ids=expected_subnet_ids,
            expected_security_group_ids=expected_security_group_ids,
            expected_environment_variables=expected_environment_variables,
            expected_idle_runtime_session_timeout=(
                expected_idle_runtime_session_timeout
            ),
            expected_max_lifetime=expected_max_lifetime,
            allow_unhardened_metadata=True,
            allow_service_s3_endpoint=True,
        )
        initial_network = initial.get("networkConfiguration")
        initial_vpc = (
            initial_network.get("networkModeConfig")
            if isinstance(initial_network, Mapping)
            else None
        )
        disable_service_s3_endpoint = (
            isinstance(initial_vpc, Mapping)
            and initial_vpc.get("requireServiceS3Endpoint") is True
        )
        if (
            _requires_mmdsv2(initial.get("metadataConfiguration"))
            and not disable_service_s3_endpoint
        ):
            return HardenedRuntimeIdentity(
                runtime_id=runtime_id,
                runtime_version=runtime_version,
                runtime_arn=str(initial.get("agentRuntimeArn") or ""),
            )

        execution_role_arn = expected_execution_role_arn(account, region)
        update_request: dict[str, Any] = {
            "agentRuntimeId": runtime_id,
            "agentRuntimeArtifact": {
                "containerConfiguration": {"containerUri": runtime_image_uri}
            },
            "roleArn": execution_role_arn,
            "networkConfiguration": {
                "networkMode": "VPC",
                "networkModeConfig": {
                    "securityGroups": sorted(expected_security_group_ids),
                    "subnets": sorted(expected_subnet_ids),
                },
            },
            "description": (
                "Personal Operator immutable bridge runtime at commit "
                f"{source_commit}"
            ),
            "protocolConfiguration": {"serverProtocol": "HTTP"},
            "lifecycleConfiguration": {
                "idleRuntimeSessionTimeout": (
                    expected_idle_runtime_session_timeout
                ),
                "maxLifetime": expected_max_lifetime,
            },
            "metadataConfiguration": {"requireMMDSV2": True},
            "environmentVariables": dict(expected_environment_variables),
            "filesystemConfigurations": [
                {"sessionStorage": {"mountPath": "/mnt/workspace"}}
            ],
        }
        if disable_service_s3_endpoint:
            update_request["networkConfiguration"]["networkModeConfig"][
                "requireServiceS3Endpoint"
            ] = False
        token_material = json.dumps(
            update_request,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        update_request["clientToken"] = (
            "mmdsv2-" + hashlib.sha256(token_material).hexdigest()
        )
        update = self._call("update_agent_runtime", **update_request)
        try:
            if update.get("agentRuntimeId") != runtime_id:
                raise AgentCoreEvidenceError(
                    "MMDSv2 update returned a different runtime ID"
                )
            resulting_version = update.get("agentRuntimeVersion")
            if (
                not isinstance(resulting_version, str)
                or _VERSION.fullmatch(resulting_version) is None
                or int(resulting_version) <= int(runtime_version)
            ):
                raise AgentCoreEvidenceError(
                    "MMDSv2 update did not return a newer exact runtime version"
                )
            update_status = update.get("status")
            if update_status not in _KNOWN:
                raise AgentCoreEvidenceError(
                    f"MMDSv2 update returned unknown status {update_status!r}"
                )
        except AgentCoreEvidenceError as error:
            raise AgentCoreEvidenceAmbiguous(
                "update_agent_runtime returned malformed acknowledgement with "
                "unknown effect; authoritative reconciliation is required"
            ) from error
        if update_status in _FAILED:
            raise AgentCoreEvidenceError(
                f"MMDSv2 update entered failed status {update_status}"
            )

        for attempt in range(self._runtime_ready_attempts):
            observed = self._call(
                "get_agent_runtime",
                agentRuntimeId=runtime_id,
                agentRuntimeVersion=resulting_version,
            )
            try:
                _ready(observed.get("status"), subject="MMDSv2 runtime")
            except AgentCoreEvidenceIncomplete:
                if attempt + 1 == self._runtime_ready_attempts:
                    break
                self._sleep(self._runtime_ready_interval_seconds)
                continue
            self._validate_runtime(
                observed,
                source_commit=source_commit,
                account=account,
                region=region,
                runtime_id=runtime_id,
                runtime_version=resulting_version,
                runtime_image_uri=runtime_image_uri,
                expected_subnet_ids=expected_subnet_ids,
                expected_security_group_ids=expected_security_group_ids,
                expected_environment_variables=expected_environment_variables,
                expected_idle_runtime_session_timeout=(
                    expected_idle_runtime_session_timeout
                ),
                expected_max_lifetime=expected_max_lifetime,
            )
            return HardenedRuntimeIdentity(
                runtime_id=runtime_id,
                runtime_version=resulting_version,
                runtime_arn=str(observed.get("agentRuntimeArn") or ""),
            )
        raise AgentCoreEvidenceAmbiguous(
            "MMDSv2 runtime did not become READY within the reviewed wait bound"
        )

    def collect_runtime_identity(
        self,
        *,
        source_commit: str,
        account: str,
        region: str,
        runtime_id: str,
        runtime_version: str,
        runtime_image_uri: str,
        expected_subnet_ids: Sequence[str],
        expected_security_group_ids: Sequence[str],
        expected_environment_variables: Mapping[str, str],
        expected_idle_runtime_session_timeout: int,
        expected_max_lifetime: int,
    ) -> tuple[str, str]:
        """Prove one READY runtime without requiring its later endpoint."""

        self._collect_runtime(
            source_commit=source_commit,
            account=account,
            region=region,
            runtime_id=runtime_id,
            runtime_version=runtime_version,
            runtime_image_uri=runtime_image_uri,
            expected_subnet_ids=expected_subnet_ids,
            expected_security_group_ids=expected_security_group_ids,
            expected_environment_variables=expected_environment_variables,
            expected_idle_runtime_session_timeout=(
                expected_idle_runtime_session_timeout
            ),
            expected_max_lifetime=expected_max_lifetime,
        )
        return runtime_id, runtime_version

    def collect_context(
        self,
        *,
        source_commit: str,
        account: str,
        region: str,
        runtime_id: str,
        runtime_version: str,
        runtime_image_uri: str,
        expected_subnet_ids: Sequence[str],
        expected_security_group_ids: Sequence[str],
        expected_environment_variables: Mapping[str, str],
        expected_idle_runtime_session_timeout: int,
        expected_max_lifetime: int,
    ) -> RuntimeContextV3:
        """Collect the canonical v3 context only from exact READY resources."""

        runtime, live_configuration = self._collect_runtime(
            source_commit=source_commit,
            account=account,
            region=region,
            runtime_id=runtime_id,
            runtime_version=runtime_version,
            runtime_image_uri=runtime_image_uri,
            expected_subnet_ids=expected_subnet_ids,
            expected_security_group_ids=expected_security_group_ids,
            expected_environment_variables=expected_environment_variables,
            expected_idle_runtime_session_timeout=(
                expected_idle_runtime_session_timeout
            ),
            expected_max_lifetime=expected_max_lifetime,
        )
        execution_role_arn = expected_execution_role_arn(account, region)
        expected_endpoint_name = f"release_{source_commit}"
        runtime_arn = runtime.get("agentRuntimeArn")

        endpoint = self._call(
            "get_agent_runtime_endpoint",
            agentRuntimeId=runtime_id,
            endpointName=expected_endpoint_name,
        )
        _ready(endpoint.get("status"), subject="endpoint")
        if endpoint.get("name") != expected_endpoint_name:
            raise AgentCoreEvidenceError("endpoint name differs from the release")
        if (
            endpoint.get("liveVersion") != runtime_version
            or endpoint.get("targetVersion") != runtime_version
        ):
            raise AgentCoreEvidenceError(
                "endpoint was retargeted away from the release version"
            )
        if endpoint.get("agentRuntimeArn") != runtime_arn:
            raise AgentCoreEvidenceError("endpoint runtime ARN differs")
        self._assert_command_deny_policy(
            _endpoint_resource_arn(
                account=account,
                region=region,
                runtime_id=runtime_id,
                endpoint_id=endpoint.get("id"),
            )
        )

        try:
            return RuntimeContextV3.from_mapping(
                {
                    "schema": RuntimeContextV3.SCHEMA,
                    "sourceCommit": source_commit,
                    "account": account,
                    "region": region,
                    "runtimeId": runtime_id,
                    "runtimeEndpointId": endpoint.get("id"),
                    "runtimeEndpointName": expected_endpoint_name,
                    "runtimeArn": runtime_arn,
                    "runtimeVersion": runtime_version,
                    "runtimeImageUri": runtime_image_uri,
                    "executionRoleArn": execution_role_arn,
                    "runtimeConfiguration": live_configuration.to_mapping(),
                    "runtimeConfigurationSha256": (
                        live_configuration.digest_for_role(execution_role_arn)
                    ),
                }
            )
        except ContractError as error:
            raise AgentCoreEvidenceError(
                "AgentCore evidence cannot form a canonical runtime context"
            ) from error

    def observe_retained_disposition(
        self,
        *,
        source_commit: str,
        account: str,
        region: str,
        runtime_id: str,
        runtime_version: str,
        runtime_image_uri: str,
        expected_subnet_ids: Sequence[str],
        expected_security_group_ids: Sequence[str],
        expected_environment_variables: Mapping[str, str],
        expected_idle_runtime_session_timeout: int,
        expected_max_lifetime: int,
    ) -> tuple[str, str]:
        """Prove the retained runtime/endpoint are exact or coherently absent."""

        arguments = {
            "source_commit": source_commit,
            "account": account,
            "region": region,
            "runtime_id": runtime_id,
            "runtime_version": runtime_version,
            "runtime_image_uri": runtime_image_uri,
            "expected_subnet_ids": expected_subnet_ids,
            "expected_security_group_ids": expected_security_group_ids,
            "expected_environment_variables": expected_environment_variables,
            "expected_idle_runtime_session_timeout": (
                expected_idle_runtime_session_timeout
            ),
            "expected_max_lifetime": expected_max_lifetime,
        }
        try:
            context = self.collect_context(**arguments)
        except AgentCoreEndpointAbsent as error:
            raise AgentCoreEvidenceError(
                "retained AgentCore disposition is partial: runtime present, endpoint absent"
            ) from error
        except AgentCoreRuntimeAbsent:
            try:
                self._call(
                    "get_agent_runtime_endpoint",
                    agentRuntimeId=runtime_id,
                    endpointName=f"release_{source_commit}",
                )
            except AgentCoreEndpointAbsent:
                self.assert_runtime_name_absent(
                    source_commit=source_commit,
                    account=account,
                    region=region,
                )
                return "ABSENT", ""
            raise AgentCoreEvidenceError(
                "retained AgentCore disposition is partial: runtime absent, endpoint present"
            )
        return "PRESENT", hashlib.sha256(context.to_bytes()).hexdigest()
