from __future__ import annotations

from dataclasses import replace
import hashlib

import pytest

from release_tools.contracts import (
    RuntimeContextV3,
    RuntimeImageEvidence,
    canonical_json_bytes,
)
from release_tools.production_observation import (
    ProductionEvidenceComposer,
    ProductionObservationError,
    compose_production_evidence,
)


ACCOUNT = "123456789012"
REGION = "eu-west-1"
COMMIT = "a" * 40
TREE = "b" * 40
DIGEST = "sha256:" + "c" * 64
RUNTIME_ID = "Runtime-ABCDEFGHIJ"
VERSION = "7"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/openclaw-agentcore-execution-role-eu-west-1"
BUILDER_INPUT = "sha256:" + "f" * 64
IMAGE_URI = (
    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
    f"personal-operator/bridge@{DIGEST}"
)
SUBNET_IDS = ("subnet-00000000000000001", "subnet-00000000000000002")
SECURITY_GROUP_IDS = ("sg-00000000000000001",)
RUNTIME_ENVIRONMENT = {
    "AWS_DEFAULT_REGION": REGION,
    "AWS_REGION": REGION,
    "BEDROCK_MODEL_ID": "eu.anthropic.claude-sonnet-4-20250514-v1:0",
    "S3_USER_FILES_BUCKET": "personal-operator-user-files-123456789012",
    "WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME": "workspace-credential-broker",
    "WORKSPACE_SYNC_INTERVAL_MS": "300000",
}


def _runtime_configuration() -> dict[str, object]:
    return {
        "agentRuntimeArtifact": {
            "containerConfiguration": {"containerUri": IMAGE_URI}
        },
        "environmentVariables": dict(RUNTIME_ENVIRONMENT),
        "filesystemConfigurations": [
            {"sessionStorage": {"mountPath": "/mnt/workspace"}}
        ],
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": 1800,
            "maxLifetime": 28800,
        },
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {
                "securityGroups": list(SECURITY_GROUP_IDS),
                "subnets": list(SUBNET_IDS),
            },
        },
        "protocolConfiguration": {"serverProtocol": "HTTP"},
    }


def _runtime_configuration_sha256() -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "executionRoleArn": ROLE_ARN,
                "runtimeConfiguration": _runtime_configuration(),
            }
        )
    ).hexdigest()


def _image() -> RuntimeImageEvidence:
    return RuntimeImageEvidence.from_mapping(
        {
            "schema": RuntimeImageEvidence.SCHEMA,
            "sourceCommit": COMMIT,
            "sourceTree": TREE,
            "account": ACCOUNT,
            "region": REGION,
            "repositoryName": "personal-operator/bridge",
            "commitTag": f"commit-{COMMIT}",
            "imageDigest": DIGEST,
            "imageUri": IMAGE_URI,
            "imageSizeBytes": 123,
            "scanStatus": "COMPLETE",
            "criticalFindings": 0,
            "highFindings": 0,
            "sbomSha256": "d" * 64,
            "provenanceSha256": "e" * 64,
            "signingProfileArn": (
                f"arn:aws:signer:{REGION}:{ACCOUNT}:/"
                "signing-profiles/personal_operator_bridge"
            ),
            "signatureStatus": "SIGNED",
        }
    )


def _context() -> RuntimeContextV3:
    return RuntimeContextV3.from_mapping(
        {
            "schema": RuntimeContextV3.SCHEMA,
            "sourceCommit": COMMIT,
            "account": ACCOUNT,
            "region": REGION,
            "runtimeId": RUNTIME_ID,
            "runtimeEndpointId": "ReleaseEndpoint-ABCDEFGHIJ",
            "runtimeEndpointName": f"release_{COMMIT}",
            "runtimeArn": (
                f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
                f"agent/12345678-1234-1234-1234-123456789abc:{VERSION}"
            ),
            "runtimeVersion": VERSION,
            "runtimeImageUri": IMAGE_URI,
            "executionRoleArn": ROLE_ARN,
            "runtimeConfiguration": _runtime_configuration(),
            "runtimeConfigurationSha256": _runtime_configuration_sha256(),
        }
    )


class FakeEcrAdapter:
    def __init__(self, result: RuntimeImageEvidence) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def collect(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class FakeAgentCoreAdapter:
    def __init__(self, result: RuntimeContextV3) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def collect_context(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _composer(
    ecr: FakeEcrAdapter,
    agentcore: FakeAgentCoreAdapter,
) -> ProductionEvidenceComposer:
    return ProductionEvidenceComposer(
        ecr=ecr,
        agentcore=agentcore,
        source_commit=COMMIT,
        source_tree=TREE,
        account=ACCOUNT,
        region=REGION,
        build_context="bridge/",
        builder_id=(
            "https://github.com/example/personal-operator/"
            ".github/workflows/release.yml"
        ),
        builder_inputs=(BUILDER_INPUT,),
        expected_role_arn=ROLE_ARN,
    )


def test_construction_is_credential_lazy_and_image_calls_exact_ecr_adapter() -> None:
    ecr = FakeEcrAdapter(_image())
    agentcore = FakeAgentCoreAdapter(_context())

    composer = _composer(ecr, agentcore)

    assert ecr.calls == []
    assert agentcore.calls == []
    evidence = composer.image_evidence()
    assert evidence == {"runtime_image_evidence": _image().to_mapping()}
    assert ecr.calls == [
        {
            "source_commit": COMMIT,
            "source_tree": TREE,
            "account": ACCOUNT,
            "region": REGION,
            "build_context": "bridge/",
            "builder_id": (
                "https://github.com/example/personal-operator/"
                ".github/workflows/release.yml"
            ),
            "builder_inputs": (BUILDER_INPUT,),
        }
    ]
    assert agentcore.calls == []


def test_production_factory_wires_exact_adapters_without_touching_clients() -> None:
    class PoisonClient:
        def __getattr__(self, name):
            raise AssertionError(f"client accessed during construction: {name}")

    class PoisonBlobReader:
        def read(self, url: str, *, maximum_bytes: int) -> bytes:
            raise AssertionError("blob reader accessed during construction")

    composer = compose_production_evidence(
        ecr_client=PoisonClient(),
        artifact_blob_reader=PoisonBlobReader(),
        agentcore_client=PoisonClient(),
        source_commit=COMMIT,
        source_tree=TREE,
        account=ACCOUNT,
        region=REGION,
        build_context="bridge/",
        builder_id=(
            "https://github.com/example/personal-operator/"
            ".github/workflows/release.yml"
        ),
        builder_inputs=(BUILDER_INPUT,),
        expected_role_arn=ROLE_ARN,
    )

    assert isinstance(composer, ProductionEvidenceComposer)


def test_endpoint_and_context_observations_bind_exact_live_bytes() -> None:
    ecr = FakeEcrAdapter(_image())
    agentcore = FakeAgentCoreAdapter(_context())
    composer = _composer(ecr, agentcore)

    endpoint = composer.endpoint_evidence(
        runtime_id=RUNTIME_ID,
        runtime_version=VERSION,
        runtime_image_digest=DIGEST,
    )
    context = composer.context_evidence(
        runtime_id=RUNTIME_ID,
        runtime_version=VERSION,
        runtime_image_digest=DIGEST,
    )

    assert endpoint == {"runtime_context": _context().to_mapping()}
    assert context == {
        "runtime_context": _context().to_mapping(),
        "runtime_context_sha256": hashlib.sha256(_context().to_bytes()).hexdigest(),
    }
    assert agentcore.calls == [
        {
            "source_commit": COMMIT,
            "account": ACCOUNT,
            "region": REGION,
            "runtime_id": RUNTIME_ID,
            "runtime_version": VERSION,
            "expected_role_arn": ROLE_ARN,
            "runtime_image_uri": IMAGE_URI,
        },
        {
            "source_commit": COMMIT,
            "account": ACCOUNT,
            "region": REGION,
            "runtime_id": RUNTIME_ID,
            "runtime_version": VERSION,
            "expected_role_arn": ROLE_ARN,
            "runtime_image_uri": IMAGE_URI,
        },
    ]


@pytest.mark.parametrize(
    ("adapter", "replacement"),
    [
        ("ecr", replace(_image(), source_tree="0" * 40)),
        ("agentcore", replace(_context(), runtime_version="8")),
    ],
)
def test_composer_rejects_injected_adapter_identity_drift(adapter, replacement) -> None:
    ecr = FakeEcrAdapter(replacement if adapter == "ecr" else _image())
    agentcore = FakeAgentCoreAdapter(
        replacement if adapter == "agentcore" else _context()
    )
    composer = _composer(ecr, agentcore)

    with pytest.raises(ProductionObservationError, match="identity"):
        if adapter == "ecr":
            composer.image_evidence()
        else:
            composer.endpoint_evidence(
                runtime_id=RUNTIME_ID,
                runtime_version=VERSION,
                runtime_image_digest=DIGEST,
            )
