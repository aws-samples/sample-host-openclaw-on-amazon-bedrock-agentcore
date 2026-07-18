from __future__ import annotations

from copy import deepcopy

import pytest

from release_tools.agentcore import (
    AgentCoreEvidenceAdapter,
    AgentCoreEvidenceAmbiguous,
    AgentCoreEvidenceError,
    AgentCoreEvidenceIncomplete,
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
ROLE_ARN = (
    f"arn:aws:iam::{ACCOUNT}:role/"
    "openclaw-agentcore-execution-role-eu-west-1"
)
IMAGE_URI = (
    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
    "personal-operator/bridge@sha256:" + "b" * 64
)


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
        "networkConfiguration": {"networkMode": "VPC"},
        "filesystemConfigurations": [
            {"sessionStorage": {"mountPath": "/mnt/workspace"}}
        ],
        "protocolConfiguration": {"serverProtocol": "HTTP"},
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
    }
    value.update(overrides)
    return value


class FakeAgentCore:
    def __init__(self) -> None:
        self.runtime = _runtime()
        self.endpoint = _endpoint()
        self.listing: dict = {"runtimeEndpoints": []}
        self.calls: list[tuple[str, dict]] = []
        self.failure: Exception | None = None

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


def _collect(adapter: AgentCoreEvidenceAdapter):
    return adapter.collect_context(
        source_commit=COMMIT,
        account=ACCOUNT,
        region=REGION,
        runtime_id=RUNTIME_ID,
        runtime_version=VERSION,
        expected_role_arn=ROLE_ARN,
        runtime_image_uri=IMAGE_URI,
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
    assert fake.calls == [
        (
            "get_agent_runtime",
            {
                "agentRuntimeId": RUNTIME_ID,
                "agentRuntimeVersion": VERSION,
            },
        ),
        (
            "get_agent_runtime_endpoint",
            {"agentRuntimeId": RUNTIME_ID, "endpointName": ENDPOINT_NAME},
        ),
    ]


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
        ("runtime", {"roleArn": ROLE_ARN.replace(ACCOUNT, "999999999999")}),
        ("runtime", {"filesystemConfigurations": []}),
        ("runtime", {"protocolConfiguration": {"serverProtocol": "HTTPS"}}),
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
