from __future__ import annotations

import json
from pathlib import Path

import pytest

from release_tools.contracts import ContractError, RuntimeContextV3
from release_tools.release_assets import build_cdk_context


ACCOUNT = "123456789012"
REGION = "eu-west-1"
COMMIT = "a" * 40
IMAGE = (
    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
    "personal-operator/bridge@sha256:" + "b" * 64
)


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
