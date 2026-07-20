from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from release_tools.contracts import (
    ContractError,
    RuntimeContextV3,
    canonical_json_bytes,
)
from release_tools.release_assets import build_cdk_context


ACCOUNT = "123456789012"
REGION = "eu-west-1"
COMMIT = "a" * 40
IMAGE = (
    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
    "personal-operator/bridge@sha256:" + "b" * 64
)
ROLE_ARN = (
    f"arn:aws:iam::{ACCOUNT}:role/"
    f"openclaw-agentcore-execution-role-{REGION}"
)
SUBNET_IDS = ("subnet-00000000000000001", "subnet-00000000000000002")
SECURITY_GROUP_IDS = ("sg-00000000000000001",)
RUNTIME_ENVIRONMENT = {
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


def _runtime_configuration() -> dict[str, object]:
    return {
        "agentRuntimeArtifact": {
            "containerConfiguration": {"containerUri": IMAGE}
        },
        "authorizerConfiguration": {},
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
        "metadataConfiguration": {"requireMMDSV2": True},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "requestHeaderConfiguration": {},
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


def _context() -> RuntimeContextV3:
    return RuntimeContextV3.from_mapping(
        {
            "schema": RuntimeContextV3.SCHEMA,
            "sourceCommit": COMMIT,
            "account": ACCOUNT,
            "region": REGION,
            "runtimeId": "Runtime-ABCDEFGHIJ",
            "runtimeEndpointId": "Endpoint-ABCDEFGHIJ",
            "runtimeEndpointName": f"release_{COMMIT}",
            "runtimeArn": (
                f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:agent/"
                "12345678-1234-1234-1234-123456789abc:7"
            ),
            "runtimeVersion": "7",
            "runtimeImageUri": IMAGE,
            "executionRoleArn": ROLE_ARN,
            "runtimeConfiguration": _runtime_configuration(),
            "runtimeConfigurationSha256": _runtime_configuration_sha256(),
        }
    )


def test_cdk_context_is_derived_only_from_the_canonical_runtime_contract(
    tmp_path: Path,
) -> None:
    config = tmp_path / "cdk.json"
    runtime = tmp_path / "runtime.json"
    config.write_text(
        json.dumps({"context": {"region": REGION}}), encoding="utf-8"
    )
    runtime.write_bytes(_context().to_bytes())

    result = build_cdk_context(
        config,
        runtime,
        source_commit=COMMIT,
        account=ACCOUNT,
        region=REGION,
        runtime_image_uri=IMAGE,
    )

    assert result["region"] == REGION
    assert result["runtime_source_commit"] == COMMIT
    assert result["runtime_id"] == "Runtime-ABCDEFGHIJ"
    assert result["runtime_endpoint_id"] == "Endpoint-ABCDEFGHIJ"
    assert result["runtime_endpoint_name"] == f"release_{COMMIT}"
    assert result["runtime_version"] == "7"
    assert result["runtime_arn"].endswith(":7")
    assert result["runtime_image_uri"] == IMAGE


def test_cdk_context_rejects_noncanonical_or_cross_release_runtime(
    tmp_path: Path,
) -> None:
    config = tmp_path / "cdk.json"
    runtime = tmp_path / "runtime.json"
    config.write_text('{"context":{}}\n', encoding="utf-8")
    canonical = _context().to_bytes()

    runtime.write_bytes(canonical.rstrip())
    with pytest.raises(ContractError):
        build_cdk_context(
            config,
            runtime,
            source_commit=COMMIT,
            account=ACCOUNT,
            region=REGION,
            runtime_image_uri=IMAGE,
        )

    runtime.write_bytes(canonical)
    with pytest.raises(ContractError, match="bound"):
        build_cdk_context(
            config,
            runtime,
            source_commit="c" * 40,
            account=ACCOUNT,
            region=REGION,
            runtime_image_uri=IMAGE,
        )
