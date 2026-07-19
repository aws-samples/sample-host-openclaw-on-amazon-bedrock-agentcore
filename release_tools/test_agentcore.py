from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
import pytest

from release_tools.agentcore import (
    AgentCoreEvidenceAdapter,
    AgentCoreEvidenceAmbiguous,
    AgentCoreEvidenceError,
    AgentCoreEvidenceIncomplete,
    AgentCoreEndpointAbsent,
    AgentCoreRuntimeAbsent,
)


ACCOUNT = "123456789012"
REGION = "eu-west-1"
COMMIT = "a" * 40
VERSION = "7"
RUNTIME_ID = "personal_operator_bridge-0123456789"
ENDPOINT_ID = "release_endpoint-0123456789"
ENDPOINT_NAME = f"release_{COMMIT}"
RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
    f"agent/12345678-1234-1234-1234-123456789abc:{VERSION}"
)
ENDPOINT_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
    "agentEndpoint/87654321-4321-4321-4321-cba987654321"
)
RUNTIME_RESOURCE_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME_ID}"
)
ENDPOINT_RESOURCE_ARN = (
    f"{RUNTIME_RESOURCE_ARN}/runtime-endpoint/{ENDPOINT_ID}"
)
ROLE_ARN = (
    f"arn:aws:iam::{ACCOUNT}:role/"
    "openclaw-agentcore-execution-role-eu-west-1"
)
IMAGE_URI = (
    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
    "personal-operator/bridge@sha256:" + "b" * 64
)
SUBNET_IDS = ("subnet-00000000000000001", "subnet-00000000000000002")
SECURITY_GROUP_IDS = ("sg-00000000000000001",)
ENVIRONMENT = {
    "AWS_DEFAULT_REGION": REGION,
    "AWS_REGION": REGION,
    "BEDROCK_MODEL_ID": "eu.anthropic.claude-sonnet-4-20250514-v1:0",
    "CAPABILITY_GATEWAY_FUNCTION_ARN": (
        f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:"
        "personal-operator-capability-gateway"
    ),
    "DISABLE_ADOT_OBSERVABILITY": "true",
    "S3_USER_FILES_BUCKET": "personal-operator-user-files-123456789012",
    "WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME": "workspace-credential-broker",
    "WORKSPACE_SYNC_INTERVAL_MS": "300000",
}
IDLE_TIMEOUT = 1800
MAX_LIFETIME = 28800


def _runtime(**overrides):
    value = {
        "agentRuntimeId": RUNTIME_ID,
        "agentRuntimeName": "personal_operator_bridge",
        "agentRuntimeVersion": VERSION,
        "agentRuntimeArn": RUNTIME_ARN,
        "status": "READY",
        "roleArn": ROLE_ARN,
        "agentRuntimeArtifact": {
            "containerConfiguration": {"containerUri": IMAGE_URI}
        },
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {
                "requireServiceS3Endpoint": False,
                "securityGroups": list(SECURITY_GROUP_IDS),
                "subnets": list(SUBNET_IDS),
            },
        },
        "environmentVariables": dict(ENVIRONMENT),
        "filesystemConfigurations": [
            {"sessionStorage": {"mountPath": "/mnt/workspace"}}
        ],
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": IDLE_TIMEOUT,
            "maxLifetime": MAX_LIFETIME,
        },
        "metadataConfiguration": {"requireMMDSV2": True},
    }
    value.update(overrides)
    return value


def _endpoint(**overrides):
    value = {
        "id": ENDPOINT_ID,
        "name": ENDPOINT_NAME,
        "status": "READY",
        "liveVersion": VERSION,
        "targetVersion": VERSION,
        "agentRuntimeArn": RUNTIME_ARN,
        "agentRuntimeEndpointArn": ENDPOINT_ARN,
    }
    value.update(overrides)
    return value


class FakeAgentCore:
    def __init__(self) -> None:
        self.runtime = _runtime()
        self.endpoint = _endpoint()
        self.listing: dict = {"runtimeEndpoints": []}
        self.runtimes: dict = {"agentRuntimes": []}
        self.calls: list[tuple[str, dict]] = []
        self.failure: Exception | None = None
        self.policies = {
            RUNTIME_RESOURCE_ARN: _command_deny_policy(RUNTIME_RESOURCE_ARN),
            ENDPOINT_RESOURCE_ARN: _command_deny_policy(ENDPOINT_RESOURCE_ARN),
        }

    def _respond(self, name: str, arguments: dict, value: dict) -> dict:
        self.calls.append((name, arguments))
        if self.failure is not None:
            raise self.failure
        return deepcopy(value)

    def get_agent_runtime(self, **kwargs) -> dict:
        return self._respond("get_agent_runtime", kwargs, self.runtime)

    def list_agent_runtime_endpoints(self, **kwargs) -> dict:
        return self._respond(
            "list_agent_runtime_endpoints", kwargs, self.listing
        )

    def get_agent_runtime_endpoint(self, **kwargs) -> dict:
        return self._respond(
            "get_agent_runtime_endpoint", kwargs, self.endpoint
        )

    def list_agent_runtimes(self, **kwargs) -> dict:
        return self._respond("list_agent_runtimes", kwargs, self.runtimes)

    def get_resource_policy(self, **kwargs) -> dict:
        resource_arn = kwargs.get("resourceArn")
        return self._respond(
            "get_resource_policy",
            kwargs,
            {"policy": self.policies.get(resource_arn)},
        )


def _command_deny_policy(resource_arn: str) -> str:
    return json.dumps(
        {
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
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _collect(adapter: AgentCoreEvidenceAdapter):
    return adapter.collect_context(
        source_commit=COMMIT,
        account=ACCOUNT,
        region=REGION,
        runtime_id=RUNTIME_ID,
        runtime_version=VERSION,
        runtime_image_uri=IMAGE_URI,
        expected_subnet_ids=SUBNET_IDS,
        expected_security_group_ids=SECURITY_GROUP_IDS,
        expected_environment_variables=ENVIRONMENT,
        expected_idle_runtime_session_timeout=IDLE_TIMEOUT,
        expected_max_lifetime=MAX_LIFETIME,
    )


def test_collects_one_ready_digest_bound_runtime_context() -> None:
    fake = FakeAgentCore()

    context = _collect(AgentCoreEvidenceAdapter(fake))

    assert context.source_commit == COMMIT
    assert context.runtime_id == RUNTIME_ID
    assert context.runtime_endpoint_id == ENDPOINT_ID
    assert context.runtime_endpoint_name == ENDPOINT_NAME
    assert context.runtime_version == VERSION
    assert context.runtime_arn == RUNTIME_ARN
    assert context.runtime_image_uri == IMAGE_URI
    assert context.execution_role_arn == ROLE_ARN
    assert context.runtime_configuration.subnet_ids == SUBNET_IDS
    assert context.runtime_configuration.security_group_ids == SECURITY_GROUP_IDS
    assert len(context.runtime_configuration_sha256) == 64
    assert fake.calls == [
        (
            "get_agent_runtime",
            {
                "agentRuntimeId": RUNTIME_ID,
                "agentRuntimeVersion": VERSION,
            },
        ),
        (
            "get_resource_policy",
            {"resourceArn": RUNTIME_RESOURCE_ARN},
        ),
        (
            "get_agent_runtime_endpoint",
            {"agentRuntimeId": RUNTIME_ID, "endpointName": ENDPOINT_NAME},
        ),
        (
            "get_resource_policy",
            {"resourceArn": ENDPOINT_RESOURCE_ARN},
        ),
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(Effect="Allow"),
        lambda value: value.update(Principal={"AWS": ROLE_ARN}),
        lambda value: value.update(
            Action=["bedrock-agentcore:InvokeAgentRuntimeCommand"]
        ),
        lambda value: value.update(Resource=ENDPOINT_ARN),
        lambda value: value.update(Sid="DifferentBoundary"),
    ],
)
def test_rejects_any_runtime_command_policy_drift(mutation) -> None:
    fake = FakeAgentCore()
    policy = json.loads(fake.policies[RUNTIME_RESOURCE_ARN])
    mutation(policy["Statement"][0])
    fake.policies[RUNTIME_RESOURCE_ARN] = json.dumps(policy)

    with pytest.raises(AgentCoreEvidenceError, match="command.*policy"):
        _collect(AgentCoreEvidenceAdapter(fake))


@pytest.mark.parametrize("value", [None, "", "not-json", "[]", "{}"])
def test_rejects_missing_or_malformed_runtime_command_policy(value) -> None:
    fake = FakeAgentCore()
    fake.policies[RUNTIME_RESOURCE_ARN] = value

    with pytest.raises(AgentCoreEvidenceError, match="command.*policy"):
        _collect(AgentCoreEvidenceAdapter(fake))


def test_rejects_endpoint_command_policy_drift() -> None:
    fake = FakeAgentCore()
    policy = json.loads(fake.policies[ENDPOINT_RESOURCE_ARN])
    policy["Statement"][0]["Action"] = [
        "bedrock-agentcore:InvokeAgentRuntimeCommand"
    ]
    fake.policies[ENDPOINT_RESOURCE_ARN] = json.dumps(policy)

    with pytest.raises(AgentCoreEvidenceError, match="command.*policy"):
        _collect(AgentCoreEvidenceAdapter(fake))


def test_live_runtime_accepts_only_explicitly_disabled_service_s3_endpoint() -> None:
    fake = FakeAgentCore()
    fake.runtime["networkConfiguration"]["networkModeConfig"][
        "requireServiceS3Endpoint"
    ] = False

    context = _collect(AgentCoreEvidenceAdapter(fake))

    assert "requireServiceS3Endpoint" not in context.runtime_configuration.to_mapping()[
        "networkConfiguration"
    ]["networkModeConfig"]


def test_missing_live_service_s3_endpoint_field_is_authoritatively_retired() -> None:
    fake = FakeAgentCore()
    del fake.runtime["networkConfiguration"]["networkModeConfig"][
        "requireServiceS3Endpoint"
    ]

    context = _collect(AgentCoreEvidenceAdapter(fake))

    assert "requireServiceS3Endpoint" not in context.runtime_configuration.to_mapping()[
        "networkConfiguration"
    ]["networkModeConfig"]


@pytest.mark.parametrize("value", [True, 0, 1, "false", None, [], {}])
def test_live_service_s3_endpoint_disposition_rejects_every_value_except_false(
    value,
) -> None:
    fake = FakeAgentCore()
    fake.runtime["networkConfiguration"]["networkModeConfig"][
        "requireServiceS3Endpoint"
    ] = value

    with pytest.raises(AgentCoreEvidenceError, match="service-managed S3 endpoint"):
        _collect(AgentCoreEvidenceAdapter(fake))


def test_collects_runtime_identity_without_requiring_an_endpoint() -> None:
    fake = FakeAgentCore()
    adapter = AgentCoreEvidenceAdapter(fake)

    identity = adapter.collect_runtime_identity(
        source_commit=COMMIT,
        account=ACCOUNT,
        region=REGION,
        runtime_id=RUNTIME_ID,
        runtime_version=VERSION,
        runtime_image_uri=IMAGE_URI,
        expected_subnet_ids=SUBNET_IDS,
        expected_security_group_ids=SECURITY_GROUP_IDS,
        expected_environment_variables=ENVIRONMENT,
        expected_idle_runtime_session_timeout=IDLE_TIMEOUT,
        expected_max_lifetime=MAX_LIFETIME,
    )

    assert identity == (RUNTIME_ID, VERSION)
    assert fake.calls == [
        (
            "get_agent_runtime",
            {
                "agentRuntimeId": RUNTIME_ID,
                "agentRuntimeVersion": VERSION,
            },
        ),
        ("get_resource_policy", {"resourceArn": RUNTIME_RESOURCE_ARN}),
    ]


def test_runtime_identity_rejects_a_mismatched_data_plane_arn() -> None:
    fake = FakeAgentCore()
    fake.runtime["agentRuntimeArn"] = RUNTIME_ARN.rsplit(":", 1)[0] + ":6"

    with pytest.raises(AgentCoreEvidenceError, match="runtime ARN"):
        AgentCoreEvidenceAdapter(fake).collect_runtime_identity(
            **_hardening_arguments()
        )


def _hardening_arguments() -> dict:
    return {
        "source_commit": COMMIT,
        "account": ACCOUNT,
        "region": REGION,
        "runtime_id": RUNTIME_ID,
        "runtime_version": VERSION,
        "runtime_image_uri": IMAGE_URI,
        "expected_subnet_ids": SUBNET_IDS,
        "expected_security_group_ids": SECURITY_GROUP_IDS,
        "expected_environment_variables": ENVIRONMENT,
        "expected_idle_runtime_session_timeout": IDLE_TIMEOUT,
        "expected_max_lifetime": MAX_LIFETIME,
    }


def test_mmdsv2_hardening_is_a_noop_when_the_exact_version_is_already_hardened() -> None:
    fake = FakeAgentCore()

    identity = AgentCoreEvidenceAdapter(fake).harden_runtime_mmdsv2(
        **_hardening_arguments()
    )

    assert identity.runtime_id == RUNTIME_ID
    assert identity.runtime_version == VERSION
    assert identity.runtime_arn == RUNTIME_ARN
    assert fake.calls == [
        (
            "get_agent_runtime",
            {
                "agentRuntimeId": RUNTIME_ID,
                "agentRuntimeVersion": VERSION,
            },
        ),
        ("get_resource_policy", {"resourceArn": RUNTIME_RESOURCE_ARN}),
    ]


@pytest.mark.parametrize("value", [0, 1])
def test_mmdsv2_hardening_requires_a_literal_boolean(value) -> None:
    fake = FakeAgentCore()
    fake.runtime["metadataConfiguration"] = {"requireMMDSV2": value}

    with pytest.raises(AgentCoreEvidenceError, match="metadata"):
        AgentCoreEvidenceAdapter(fake).harden_runtime_mmdsv2(
            **_hardening_arguments()
        )


@pytest.mark.parametrize(
    "metadata",
    [None, {}, {"requireMMDSV2": False}, {"requireMMDSV2": None}],
)
def test_mmdsv2_hardening_updates_the_full_reviewed_config_and_waits_exact_version(
    metadata,
) -> None:
    resulting_version = "8"
    resulting_runtime_arn = RUNTIME_ARN.rsplit(":", 1)[0] + ":8"

    class HardeningAgentCore(FakeAgentCore):
        def __init__(self) -> None:
            super().__init__()
            if metadata is None:
                self.runtime.pop("metadataConfiguration")
            else:
                self.runtime["metadataConfiguration"] = metadata
            self.ready_observations = [
                _runtime(
                    agentRuntimeVersion=resulting_version,
                    agentRuntimeArn=resulting_runtime_arn,
                    status="UPDATING",
                ),
                _runtime(
                    agentRuntimeVersion=resulting_version,
                    agentRuntimeArn=resulting_runtime_arn,
                ),
            ]

        def update_agent_runtime(self, **kwargs) -> dict:
            return self._respond(
                "update_agent_runtime",
                kwargs,
                {
                    "agentRuntimeId": RUNTIME_ID,
                    "agentRuntimeVersion": resulting_version,
                    "agentRuntimeArn": resulting_runtime_arn,
                    "status": "UPDATING",
                },
            )

        def get_agent_runtime(self, **kwargs) -> dict:
            if kwargs.get("agentRuntimeVersion") == resulting_version:
                value = self.ready_observations.pop(0)
                return self._respond("get_agent_runtime", kwargs, value)
            return super().get_agent_runtime(**kwargs)

    fake = HardeningAgentCore()
    adapter = AgentCoreEvidenceAdapter(
        fake,
        sleep=lambda _: None,
        runtime_ready_attempts=3,
    )

    identity = adapter.harden_runtime_mmdsv2(**_hardening_arguments())

    assert identity.runtime_id == RUNTIME_ID
    assert identity.runtime_version == resulting_version
    assert identity.runtime_arn == resulting_runtime_arn
    assert identity.to_mapping() == {
        "runtimeId": RUNTIME_ID,
        "runtimeVersion": resulting_version,
        "runtimeArn": resulting_runtime_arn,
    }
    update_calls = [call for call in fake.calls if call[0] == "update_agent_runtime"]
    assert len(update_calls) == 1
    request = update_calls[0][1]
    client_token = request.pop("clientToken")
    assert client_token.startswith("mmdsv2-")
    assert len(client_token) == len("mmdsv2-") + 64
    assert request == {
        "agentRuntimeId": RUNTIME_ID,
        "agentRuntimeArtifact": {
            "containerConfiguration": {"containerUri": IMAGE_URI}
        },
        "roleArn": ROLE_ARN,
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {
                "securityGroups": list(SECURITY_GROUP_IDS),
                "subnets": list(SUBNET_IDS),
            },
        },
        "description": (
            "Personal Operator immutable bridge runtime at commit " + COMMIT
        ),
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": IDLE_TIMEOUT,
            "maxLifetime": MAX_LIFETIME,
        },
        "metadataConfiguration": {"requireMMDSV2": True},
        "environmentVariables": ENVIRONMENT,
        "filesystemConfigurations": [
            {"sessionStorage": {"mountPath": "/mnt/workspace"}}
        ],
    }
    assert "requireServiceS3Endpoint" not in json.dumps(request)
    ready_gets = [
        arguments
        for name, arguments in fake.calls
        if name == "get_agent_runtime"
        and arguments.get("agentRuntimeVersion") == resulting_version
    ]
    assert ready_gets == [
        {
            "agentRuntimeId": RUNTIME_ID,
            "agentRuntimeVersion": resulting_version,
        },
        {
            "agentRuntimeId": RUNTIME_ID,
            "agentRuntimeVersion": resulting_version,
        },
    ]


@pytest.mark.parametrize(
    ("service_s3_disposition", "expected_update_value"),
    [(True, False), (False, None), ("absent", None)],
)
def test_runtime_hardening_sends_the_transitional_s3_field_only_to_disable_true(
    service_s3_disposition,
    expected_update_value,
) -> None:
    resulting_version = "8"
    resulting_runtime_arn = RUNTIME_ARN.rsplit(":", 1)[0] + ":8"

    class HardeningAgentCore(FakeAgentCore):
        def __init__(self) -> None:
            super().__init__()
            self.runtime["metadataConfiguration"] = {"requireMMDSV2": False}
            if service_s3_disposition == "absent":
                del self.runtime["networkConfiguration"]["networkModeConfig"][
                    "requireServiceS3Endpoint"
                ]
            else:
                self.runtime["networkConfiguration"]["networkModeConfig"][
                    "requireServiceS3Endpoint"
                ] = service_s3_disposition
            self.hardened = _runtime(
                agentRuntimeVersion=resulting_version,
                agentRuntimeArn=resulting_runtime_arn,
            )
            if service_s3_disposition == "absent":
                del self.hardened["networkConfiguration"]["networkModeConfig"][
                    "requireServiceS3Endpoint"
                ]

        def update_agent_runtime(self, **kwargs) -> dict:
            return self._respond(
                "update_agent_runtime",
                kwargs,
                {
                    "agentRuntimeId": RUNTIME_ID,
                    "agentRuntimeVersion": resulting_version,
                    "agentRuntimeArn": resulting_runtime_arn,
                    "status": "UPDATING",
                },
            )

        def get_agent_runtime(self, **kwargs) -> dict:
            if kwargs.get("agentRuntimeVersion") == resulting_version:
                return self._respond("get_agent_runtime", kwargs, self.hardened)
            return super().get_agent_runtime(**kwargs)

    fake = HardeningAgentCore()

    identity = AgentCoreEvidenceAdapter(fake).harden_runtime_mmdsv2(
        **_hardening_arguments()
    )

    assert identity.runtime_arn == resulting_runtime_arn
    request = next(
        arguments
        for name, arguments in fake.calls
        if name == "update_agent_runtime"
    )
    network_mode = request["networkConfiguration"]["networkModeConfig"]
    if expected_update_value is None:
        assert "requireServiceS3Endpoint" not in network_mode
    else:
        assert network_mode["requireServiceS3Endpoint"] is expected_update_value


def test_runtime_hardening_updates_when_only_service_s3_endpoint_is_true() -> None:
    resulting_version = "8"
    resulting_runtime_arn = RUNTIME_ARN.rsplit(":", 1)[0] + ":8"

    class HardeningAgentCore(FakeAgentCore):
        def update_agent_runtime(self, **kwargs) -> dict:
            return self._respond(
                "update_agent_runtime",
                kwargs,
                {
                    "agentRuntimeId": RUNTIME_ID,
                    "agentRuntimeVersion": resulting_version,
                    "agentRuntimeArn": resulting_runtime_arn,
                    "status": "UPDATING",
                },
            )

        def get_agent_runtime(self, **kwargs) -> dict:
            if kwargs.get("agentRuntimeVersion") == resulting_version:
                return self._respond(
                    "get_agent_runtime",
                    kwargs,
                    _runtime(
                        agentRuntimeVersion=resulting_version,
                        agentRuntimeArn=resulting_runtime_arn,
                    ),
                )
            return super().get_agent_runtime(**kwargs)

    fake = HardeningAgentCore()
    fake.runtime["networkConfiguration"]["networkModeConfig"][
        "requireServiceS3Endpoint"
    ] = True

    identity = AgentCoreEvidenceAdapter(fake).harden_runtime_mmdsv2(
        **_hardening_arguments()
    )

    assert identity.runtime_version == resulting_version
    request = next(
        arguments
        for name, arguments in fake.calls
        if name == "update_agent_runtime"
    )
    assert request["networkConfiguration"]["networkModeConfig"][
        "requireServiceS3Endpoint"
    ] is False


def test_mmdsv2_hardening_rejects_other_drift_before_mutation() -> None:
    class HardeningAgentCore(FakeAgentCore):
        def update_agent_runtime(self, **kwargs) -> dict:
            return self._respond("update_agent_runtime", kwargs, {})

    fake = HardeningAgentCore()
    fake.runtime["metadataConfiguration"] = {"requireMMDSV2": False}
    fake.runtime["environmentVariables"] = {
        **ENVIRONMENT,
        "AWS_REGION": "us-east-1",
    }

    with pytest.raises(AgentCoreEvidenceError, match="runtime configuration"):
        AgentCoreEvidenceAdapter(fake).harden_runtime_mmdsv2(
            **_hardening_arguments()
        )

    assert all(name != "update_agent_runtime" for name, _ in fake.calls)


def test_mmdsv2_hardening_fails_closed_when_result_never_becomes_ready() -> None:
    class HardeningAgentCore(FakeAgentCore):
        def __init__(self) -> None:
            super().__init__()
            self.runtime["metadataConfiguration"] = {"requireMMDSV2": False}

        def update_agent_runtime(self, **kwargs) -> dict:
            return self._respond(
                "update_agent_runtime",
                kwargs,
                {
                    "agentRuntimeId": RUNTIME_ID,
                    "agentRuntimeVersion": "8",
                    "agentRuntimeArn": RUNTIME_ARN.rsplit(":", 1)[0] + ":8",
                    "status": "UPDATING",
                },
            )

        def get_agent_runtime(self, **kwargs) -> dict:
            if kwargs.get("agentRuntimeVersion") == "8":
                return self._respond(
                    "get_agent_runtime",
                    kwargs,
                    _runtime(
                        agentRuntimeVersion="8",
                        agentRuntimeArn=RUNTIME_ARN.rsplit(":", 1)[0] + ":8",
                        status="UPDATING",
                    ),
                )
            return super().get_agent_runtime(**kwargs)

    adapter = AgentCoreEvidenceAdapter(
        HardeningAgentCore(),
        sleep=lambda _: None,
        runtime_ready_attempts=2,
    )

    with pytest.raises(AgentCoreEvidenceAmbiguous, match="did not become READY"):
        adapter.harden_runtime_mmdsv2(**_hardening_arguments())


def test_missing_exact_runtime_is_authoritative_absence() -> None:
    class ResourceNotFound(Exception):
        response = {"Error": {"Code": "ResourceNotFoundException"}}

    fake = FakeAgentCore()
    fake.failure = ResourceNotFound("missing")

    with pytest.raises(AgentCoreRuntimeAbsent, match="absent"):
        _collect(AgentCoreEvidenceAdapter(fake))


def test_missing_endpoint_is_distinct_from_missing_retained_runtime() -> None:
    class ResourceNotFound(Exception):
        response = {"Error": {"Code": "ResourceNotFoundException"}}

    class MissingEndpointAgentCore(FakeAgentCore):
        def get_agent_runtime_endpoint(self, **kwargs):
            raise ResourceNotFound("missing endpoint")

    with pytest.raises(AgentCoreEndpointAbsent, match="absent"):
        _collect(AgentCoreEvidenceAdapter(MissingEndpointAgentCore()))


def test_retained_runtime_and_endpoint_disposition_is_exact_or_coherently_absent() -> None:
    adapter = AgentCoreEvidenceAdapter(FakeAgentCore())
    assert adapter.observe_retained_disposition(
        source_commit=COMMIT,
        account=ACCOUNT,
        region=REGION,
        runtime_id=RUNTIME_ID,
        runtime_version=VERSION,
        runtime_image_uri=IMAGE_URI,
        expected_subnet_ids=SUBNET_IDS,
        expected_security_group_ids=SECURITY_GROUP_IDS,
        expected_environment_variables=ENVIRONMENT,
        expected_idle_runtime_session_timeout=IDLE_TIMEOUT,
        expected_max_lifetime=MAX_LIFETIME,
    ) == (
        "PRESENT",
        hashlib.sha256(
            _collect(AgentCoreEvidenceAdapter(FakeAgentCore())).to_bytes()
        ).hexdigest(),
    )

    class ResourceNotFound(Exception):
        response = {"Error": {"Code": "ResourceNotFoundException"}}

    class MissingBoth(FakeAgentCore):
        def get_agent_runtime(self, **kwargs):
            raise ResourceNotFound("missing runtime")

        def get_agent_runtime_endpoint(self, **kwargs):
            raise ResourceNotFound("missing endpoint")

    absent = AgentCoreEvidenceAdapter(MissingBoth())
    assert absent.observe_retained_disposition(
        source_commit=COMMIT,
        account=ACCOUNT,
        region=REGION,
        runtime_id=RUNTIME_ID,
        runtime_version=VERSION,
        runtime_image_uri=IMAGE_URI,
        expected_subnet_ids=SUBNET_IDS,
        expected_security_group_ids=SECURITY_GROUP_IDS,
        expected_environment_variables=ENVIRONMENT,
        expected_idle_runtime_session_timeout=IDLE_TIMEOUT,
        expected_max_lifetime=MAX_LIFETIME,
    ) == ("ABSENT", "")

    replacement = MissingBoth()
    replacement.runtimes = {
        "agentRuntimes": [
            {
                "agentRuntimeName": "personal_operator_bridge",
                "agentRuntimeId": "replacement_runtime-9876543210",
            }
        ]
    }
    with pytest.raises(AgentCoreEvidenceError, match="still exists"):
        AgentCoreEvidenceAdapter(replacement).observe_retained_disposition(
            source_commit=COMMIT,
            account=ACCOUNT,
            region=REGION,
            runtime_id=RUNTIME_ID,
            runtime_version=VERSION,
            runtime_image_uri=IMAGE_URI,
            expected_subnet_ids=SUBNET_IDS,
            expected_security_group_ids=SECURITY_GROUP_IDS,
            expected_environment_variables=ENVIRONMENT,
            expected_idle_runtime_session_timeout=IDLE_TIMEOUT,
            expected_max_lifetime=MAX_LIFETIME,
        )

    paginated = MissingBoth()
    paginated.runtimes = {"agentRuntimes": [], "nextToken": "more"}
    with pytest.raises(AgentCoreEvidenceAmbiguous, match="paginated"):
        AgentCoreEvidenceAdapter(paginated).observe_retained_disposition(
            source_commit=COMMIT,
            account=ACCOUNT,
            region=REGION,
            runtime_id=RUNTIME_ID,
            runtime_version=VERSION,
            runtime_image_uri=IMAGE_URI,
            expected_subnet_ids=SUBNET_IDS,
            expected_security_group_ids=SECURITY_GROUP_IDS,
            expected_environment_variables=ENVIRONMENT,
            expected_idle_runtime_session_timeout=IDLE_TIMEOUT,
            expected_max_lifetime=MAX_LIFETIME,
        )

    class MissingEndpoint(FakeAgentCore):
        def get_agent_runtime_endpoint(self, **kwargs):
            raise ResourceNotFound("missing endpoint")

    with pytest.raises(AgentCoreEvidenceError, match="partial"):
        AgentCoreEvidenceAdapter(MissingEndpoint()).observe_retained_disposition(
            source_commit=COMMIT,
            account=ACCOUNT,
            region=REGION,
            runtime_id=RUNTIME_ID,
            runtime_version=VERSION,
            runtime_image_uri=IMAGE_URI,
            expected_subnet_ids=SUBNET_IDS,
            expected_security_group_ids=SECURITY_GROUP_IDS,
            expected_environment_variables=ENVIRONMENT,
            expected_idle_runtime_session_timeout=IDLE_TIMEOUT,
            expected_max_lifetime=MAX_LIFETIME,
        )


def test_endpoint_name_must_be_unused_before_the_create_mutation() -> None:
    fake = FakeAgentCore()
    adapter = AgentCoreEvidenceAdapter(fake)

    adapter.assert_endpoint_name_available(
        runtime_id=RUNTIME_ID,
        source_commit=COMMIT,
        account=ACCOUNT,
        region=REGION,
    )
    fake.listing = {
        "runtimeEndpoints": [
            {"id": ENDPOINT_ID, "name": ENDPOINT_NAME, "status": "READY"}
        ]
    }

    with pytest.raises(AgentCoreEvidenceError, match="collision"):
        adapter.assert_endpoint_name_available(
            runtime_id=RUNTIME_ID,
            source_commit=COMMIT,
            account=ACCOUNT,
            region=REGION,
        )


def test_runtime_absence_requires_a_complete_exact_name_inventory() -> None:
    fake = FakeAgentCore()
    adapter = AgentCoreEvidenceAdapter(fake)

    adapter.assert_runtime_name_absent(
        source_commit=COMMIT,
        account=ACCOUNT,
        region=REGION,
    )
    assert fake.calls == [("list_agent_runtimes", {"maxResults": 100})]

    fake = FakeAgentCore()
    fake.runtimes = {
        "agentRuntimes": [
            {
                "agentRuntimeArn": RUNTIME_ARN,
                "agentRuntimeId": RUNTIME_ID,
                "agentRuntimeVersion": VERSION,
                "agentRuntimeName": "personal_operator_bridge",
                "status": "READY",
            }
        ]
    }
    with pytest.raises(AgentCoreEvidenceError, match="still exists"):
        AgentCoreEvidenceAdapter(fake).assert_runtime_name_absent(
            source_commit=COMMIT,
            account=ACCOUNT,
            region=REGION,
        )

    fake = FakeAgentCore()
    fake.runtimes = {"agentRuntimes": [], "nextToken": "truncated"}
    with pytest.raises(AgentCoreEvidenceAmbiguous, match="paginated"):
        AgentCoreEvidenceAdapter(fake).assert_runtime_name_absent(
            source_commit=COMMIT,
            account=ACCOUNT,
            region=REGION,
        )


@pytest.mark.parametrize(
    ("subject", "status", "error_type"),
    [
        ("runtime", "CREATING", AgentCoreEvidenceIncomplete),
        ("runtime", "FUTURE_STATE", AgentCoreEvidenceError),
        ("runtime", "UPDATE_FAILED", AgentCoreEvidenceError),
        ("endpoint", "UPDATING", AgentCoreEvidenceIncomplete),
        ("endpoint", "FUTURE_STATE", AgentCoreEvidenceError),
        ("endpoint", "CREATE_FAILED", AgentCoreEvidenceError),
    ],
)
def test_unknown_pending_and_failed_states_are_not_release_evidence(
    subject: str,
    status: str,
    error_type: type[Exception],
) -> None:
    fake = FakeAgentCore()
    if subject == "runtime":
        fake.runtime["status"] = status
    else:
        fake.endpoint["status"] = status

    with pytest.raises(error_type, match="status"):
        _collect(AgentCoreEvidenceAdapter(fake))


@pytest.mark.parametrize(
    ("subject", "replacement"),
    [
        ("runtime", {"agentRuntimeArtifact": {"containerConfiguration": {"containerUri": IMAGE_URI.replace("b", "c")}}}),
        ("runtime", {"roleArn": f"arn:aws:iam::{ACCOUNT}:role/caller-selected"}),
        (
            "runtime",
            {
                "networkConfiguration": {
                    "networkMode": "VPC",
                    "networkModeConfig": {
                        "securityGroups": list(SECURITY_GROUP_IDS),
                        "subnets": ["subnet-99999999999999999"],
                    },
                }
            },
        ),
        (
            "runtime",
            {
                "networkConfiguration": {
                    "networkMode": "VPC",
                    "networkModeConfig": {
                        "securityGroups": ["sg-99999999999999999"],
                        "subnets": list(SUBNET_IDS),
                    },
                }
            },
        ),
        (
            "runtime",
            {
                "environmentVariables": {
                    **ENVIRONMENT,
                    "AWS_SECRET_ACCESS_KEY": "not-a-real-secret",
                }
            },
        ),
        (
            "runtime",
            {
                "environmentVariables": {
                    **ENVIRONMENT,
                    "AWS_DEFAULT_REGION": "us-east-1",
                }
            },
        ),
        (
            "runtime",
            {
                "lifecycleConfiguration": {
                    "idleRuntimeSessionTimeout": IDLE_TIMEOUT,
                    "maxLifetime": MAX_LIFETIME + 1,
                }
            },
        ),
        ("runtime", {"filesystemConfigurations": []}),
        ("runtime", {"protocolConfiguration": {"serverProtocol": "HTTPS"}}),
        (
            "runtime",
            {
                "authorizerConfiguration": {
                    "customJWTAuthorizer": {
                        "allowedAudience": ["attacker"],
                        "discoveryUrl": "https://attacker.invalid",
                    }
                }
            },
        ),
        (
            "runtime",
            {
                "requestHeaderConfiguration": {
                    "requestHeaderAllowlist": ["Authorization", "Cookie"]
                }
            },
        ),
        ("runtime", {"metadataConfiguration": {"requireMMDSV2": False}}),
        ("runtime", {"metadataConfiguration": {}}),
        ("endpoint", {"liveVersion": "6"}),
        ("endpoint", {"targetVersion": "8"}),
        ("endpoint", {"name": "DEFAULT"}),
        ("endpoint", {"agentRuntimeArn": RUNTIME_ARN.replace(":7", ":6")}),
    ],
)
def test_runtime_or_endpoint_drift_fails_closed(
    subject: str,
    replacement: dict,
) -> None:
    fake = FakeAgentCore()
    if subject == "runtime":
        fake.runtime.update(replacement)
    else:
        fake.endpoint.update(replacement)

    with pytest.raises(AgentCoreEvidenceError):
        _collect(AgentCoreEvidenceAdapter(fake))


def test_timeout_after_dispatch_is_ambiguous() -> None:
    fake = FakeAgentCore()
    fake.failure = TimeoutError("unknown acceptance")

    with pytest.raises(AgentCoreEvidenceAmbiguous, match="authoritative"):
        _collect(AgentCoreEvidenceAdapter(fake))


@pytest.mark.parametrize(
    "failure",
    [
        EndpointConnectionError(endpoint_url="https://agentcore.invalid"),
        ReadTimeoutError(
            endpoint_url="https://agentcore.invalid",
            error=TimeoutError("response was not observed"),
        ),
        ClientError(
            {
                "Error": {
                    "Code": "ThrottlingException",
                    "Message": "rate exceeded",
                },
                "ResponseMetadata": {"HTTPStatusCode": 429},
            },
            "UpdateAgentRuntime",
        ),
        ClientError(
            {
                "Error": {
                    "Code": "InternalServerException",
                    "Message": "service unavailable",
                },
                "ResponseMetadata": {"HTTPStatusCode": 503},
            },
            "UpdateAgentRuntime",
        ),
    ],
)
def test_runtime_hardening_classifies_retryable_update_failures_as_unknown_effect(
    failure,
) -> None:
    class FailingUpdateAgentCore(FakeAgentCore):
        def update_agent_runtime(self, **kwargs) -> dict:
            self.calls.append(("update_agent_runtime", kwargs))
            raise failure

    fake = FailingUpdateAgentCore()
    fake.runtime["metadataConfiguration"] = {"requireMMDSV2": False}

    with pytest.raises(AgentCoreEvidenceAmbiguous, match="unknown effect"):
        AgentCoreEvidenceAdapter(fake).harden_runtime_mmdsv2(
            **_hardening_arguments()
        )

    assert any(name == "update_agent_runtime" for name, _ in fake.calls)


@pytest.mark.parametrize("response", [None, [], {}, {"agentRuntimeId": RUNTIME_ID}])
def test_runtime_hardening_malformed_update_ack_is_unknown_effect(response) -> None:
    class MalformedUpdateAgentCore(FakeAgentCore):
        def update_agent_runtime(self, **kwargs):
            self.calls.append(("update_agent_runtime", kwargs))
            return response

    fake = MalformedUpdateAgentCore()
    fake.runtime["metadataConfiguration"] = {"requireMMDSV2": False}

    with pytest.raises(AgentCoreEvidenceAmbiguous, match="unknown effect"):
        AgentCoreEvidenceAdapter(fake).harden_runtime_mmdsv2(
            **_hardening_arguments()
        )

    assert any(name == "update_agent_runtime" for name, _ in fake.calls)


def test_runtime_hardening_keeps_nonretryable_update_rejection_terminal() -> None:
    failure = ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": "request is invalid",
            },
            "ResponseMetadata": {"HTTPStatusCode": 400},
        },
        "UpdateAgentRuntime",
    )

    class RejectedUpdateAgentCore(FakeAgentCore):
        def update_agent_runtime(self, **kwargs) -> dict:
            raise failure

    fake = RejectedUpdateAgentCore()
    fake.runtime["metadataConfiguration"] = {"requireMMDSV2": False}

    with pytest.raises(AgentCoreEvidenceError) as captured:
        AgentCoreEvidenceAdapter(fake).harden_runtime_mmdsv2(
            **_hardening_arguments()
        )

    assert not isinstance(captured.value, AgentCoreEvidenceAmbiguous)


def test_unreviewed_expected_environment_is_rejected_before_live_calls() -> None:
    fake = FakeAgentCore()
    adapter = AgentCoreEvidenceAdapter(fake)

    with pytest.raises(AgentCoreEvidenceError, match="environment"):
        adapter.collect_context(
            source_commit=COMMIT,
            account=ACCOUNT,
            region=REGION,
            runtime_id=RUNTIME_ID,
            runtime_version=VERSION,
            runtime_image_uri=IMAGE_URI,
            expected_subnet_ids=SUBNET_IDS,
            expected_security_group_ids=SECURITY_GROUP_IDS,
            expected_environment_variables={
                **ENVIRONMENT,
                "AWS_ACCESS_KEY_ID": "not-a-real-key",
            },
            expected_idle_runtime_session_timeout=IDLE_TIMEOUT,
            expected_max_lifetime=MAX_LIFETIME,
        )

    assert fake.calls == []


def test_paginated_or_duplicate_endpoint_lookup_is_ambiguous() -> None:
    fake = FakeAgentCore()
    adapter = AgentCoreEvidenceAdapter(fake)
    fake.listing = {"runtimeEndpoints": [], "nextToken": "more"}
    with pytest.raises(AgentCoreEvidenceAmbiguous, match="paginated"):
        adapter.assert_endpoint_name_available(
            runtime_id=RUNTIME_ID,
            source_commit=COMMIT,
            account=ACCOUNT,
            region=REGION,
        )

    fake.listing = {
        "runtimeEndpoints": [
            {"id": ENDPOINT_ID, "name": ENDPOINT_NAME, "status": "READY"},
            {
                "id": "other_endpoint-0123456789",
                "name": ENDPOINT_NAME,
                "status": "READY",
            },
        ]
    }
    with pytest.raises(AgentCoreEvidenceAmbiguous, match="duplicate"):
        adapter.assert_endpoint_name_available(
            runtime_id=RUNTIME_ID,
            source_commit=COMMIT,
            account=ACCOUNT,
            region=REGION,
        )
