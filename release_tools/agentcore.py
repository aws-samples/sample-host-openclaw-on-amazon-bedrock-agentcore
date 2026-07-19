"""Injected AgentCore evidence adapter for one immutable runtime release."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Protocol, Sequence

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
_PENDING = {"CREATING", "UPDATING"}
_FAILED = {"CREATE_FAILED", "UPDATE_FAILED", "DELETING"}
_KNOWN = _PENDING | _FAILED | {"READY"}


class AgentCoreClient(Protocol):
    def list_agent_runtimes(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_agent_runtime(self, **kwargs: Any) -> dict[str, Any]: ...

    def list_agent_runtime_endpoints(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_agent_runtime_endpoint(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_resource_policy(self, **kwargs: Any) -> dict[str, Any]: ...


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


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentCoreEvidenceError(f"{label} response is malformed")
    return value


def _list(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise AgentCoreEvidenceError(f"{label} response is malformed")
    return value


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


def _sorted_runtime_configuration(runtime: Mapping[str, Any]) -> dict[str, Any]:
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
        if "requireServiceS3Endpoint" not in vpc_copy:
            raise AgentCoreEvidenceAmbiguous(
                "live runtime does not prove the service-managed S3 endpoint "
                "disposition"
            )
        if vpc_copy["requireServiceS3Endpoint"] is not False:
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

    def __init__(self, client: AgentCoreClient) -> None:
        self._client = client

    def _call(self, method_name: str, **arguments: Any) -> dict[str, Any]:
        method = getattr(self._client, method_name, None)
        if method is None or not callable(method):
            raise AgentCoreEvidenceError(
                f"injected AgentCore adapter lacks {method_name}"
            )
        try:
            return _object(method(**arguments), label=method_name)
        except (TimeoutError, ConnectionError) as error:
            raise AgentCoreEvidenceAmbiguous(
                f"{method_name} ended without authoritative evidence"
            ) from error
        except Exception as error:
            response = getattr(error, "response", None)
            body = response.get("Error") if isinstance(response, dict) else None
            code = body.get("Code") if isinstance(body, dict) else None
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
        execution_role_arn = expected_execution_role_arn(account, region)
        try:
            expected_configuration = RuntimeConfigurationV1.from_mapping(
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
        except (ContractError, TypeError) as error:
            raise AgentCoreEvidenceError(
                f"expected runtime configuration is invalid: {error}"
            ) from error
        runtime = self._call(
            "get_agent_runtime",
            agentRuntimeId=runtime_id,
            agentRuntimeVersion=runtime_version,
        )
        for field in ("authorizerConfiguration", "requestHeaderConfiguration"):
            value = runtime.get(field)
            if value not in (None, {}):
                raise AgentCoreEvidenceError(
                    f"runtime {field} grants unreviewed authority"
                )
        if runtime.get("metadataConfiguration") != {"requireMMDSV2": True}:
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
        if runtime.get("roleArn") != execution_role_arn:
            raise AgentCoreEvidenceError("runtime role differs from the release role")
        self._assert_command_deny_policy(runtime.get("agentRuntimeArn"))
        try:
            live_configuration = RuntimeConfigurationV1.from_mapping(
                _sorted_runtime_configuration(runtime),
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
        return runtime, live_configuration

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
            endpoint.get("agentRuntimeEndpointArn")
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
