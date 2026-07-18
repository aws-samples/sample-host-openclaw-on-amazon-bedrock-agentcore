from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from release_tools.contracts import (
    ContractError,
    RuntimeContextV3,
    RuntimeImageEvidence,
    StagingTransactionV1,
    TrustedLambdaAssetV2,
    canonical_json_bytes,
    parse_canonical_object,
    write_new_contract,
)


ACCOUNT = "123456789012"
REGION = "eu-west-1"
COMMIT = "a" * 40
TREE = "b" * 40
DIGEST = "sha256:" + "c" * 64
IMAGE_URI = (
    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
    f"personal-operator/bridge@{DIGEST}"
)
RUNTIME_ID = "Runtime-ABCDEFGHIJ"
ENDPOINT_ID = "Endpoint-ABCDEFGHIJ"
RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:agent/"
    "12345678-1234-1234-1234-123456789abc:7"
)


def _runtime_context() -> dict[str, object]:
    return {
        "schema": "personal-operator.runtime-context.v3",
        "sourceCommit": COMMIT,
        "account": ACCOUNT,
        "region": REGION,
        "runtimeId": RUNTIME_ID,
        "runtimeEndpointId": ENDPOINT_ID,
        "runtimeEndpointName": f"release_{COMMIT}",
        "runtimeArn": RUNTIME_ARN,
        "runtimeVersion": "7",
        "runtimeImageUri": IMAGE_URI,
    }


def _image_evidence() -> dict[str, object]:
    return {
        "schema": "personal-operator.runtime-image-evidence.v1",
        "sourceCommit": COMMIT,
        "sourceTree": TREE,
        "account": ACCOUNT,
        "region": REGION,
        "repositoryName": "personal-operator/bridge",
        "commitTag": f"commit-{COMMIT}",
        "imageDigest": DIGEST,
        "imageUri": IMAGE_URI,
        "imageSizeBytes": 123456,
        "scanStatus": "COMPLETE",
        "criticalFindings": 0,
        "highFindings": 0,
        "sbomSha256": "d" * 64,
        "provenanceSha256": "e" * 64,
        "signingProfileArn": (
            f"arn:aws:signer:{REGION}:{ACCOUNT}:/signing-profiles/"
            "personal_operator_bridge"
        ),
        "signatureStatus": "SIGNED",
    }


def _trusted_asset() -> dict[str, object]:
    file_payload = b"print('trusted')\n"
    file_digest = hashlib.sha256(file_payload).hexdigest()
    return {
        "schema": "personal-operator.trusted-lambda-asset.v2",
        "sourceCommit": COMMIT,
        "sourceTree": TREE,
        "platform": "linux/arm64",
        "architecture": "arm64",
        "python": "3.13",
        "builderImage": "public.ecr.aws/lambda/python@sha256:" + "1" * 64,
        "builderImageId": "sha256:" + "2" * 64,
        "requirementsMode": "sha256-locked",
        "requirementsSha256": "3" * 64,
        "requirementsInputSha256": "4" * 64,
        "sourceDateEpoch": 0,
        "payloadBytes": len(file_payload),
        "archiveName": "trusted-lambda.zip",
        "archiveBytes": 321,
        "archiveSha256": "5" * 64,
        "dependencies": [{"name": "boto3", "version": "1.2.3"}],
        "sourceFiles": [
            {"path": "router/index.py", "sha256": file_digest, "size": len(file_payload)}
        ],
        "files": [
            {
                "path": "router/index.py",
                "sha256": file_digest,
                "size": len(file_payload),
                "mode": "0644",
            }
        ],
    }


def _transaction() -> dict[str, object]:
    return {
        "schema": "personal-operator.staging-transaction.v1",
        "transactionId": f"release_{COMMIT}",
        "sourceCommit": COMMIT,
        "sourceTree": TREE,
        "account": ACCOUNT,
        "region": REGION,
        "state": "NEW",
        "lastStableState": "NEW",
        "revision": 0,
        "runtimeImageDigest": "",
        "runtimeId": "",
        "runtimeVersion": "",
        "runtimeEndpointName": f"release_{COMMIT}",
        "runtimeContextSha256": "",
        "rollbackReference": "",
        "uncertainPhase": "",
    }


def test_runtime_context_v3_is_exact_canonical_and_immutable() -> None:
    expected = _runtime_context()
    payload = canonical_json_bytes(expected)

    parsed = RuntimeContextV3.from_bytes(payload)

    assert parsed.to_bytes() == payload
    assert parsed.to_mapping() == expected
    with pytest.raises(FrozenInstanceError):
        parsed.region = "us-east-1"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda value: value.update(extra="x"), "fields"),
        (lambda value: value.update(region="us-east-1"), "region"),
        (lambda value: value.update(account="210987654321"), "account"),
        (lambda value: value.update(runtimeEndpointName="release_" + "f" * 40), "endpoint"),
        (
            lambda value: value.update(
                runtimeImageUri=(
                    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
                    "personal-operator/bridge:latest"
                )
            ),
            "immutable",
        ),
    ],
)
def test_runtime_context_rejects_extra_cross_boundary_or_mutable_values(
    mutate, match: str
) -> None:
    value = _runtime_context()
    mutate(value)

    with pytest.raises(ContractError, match=match):
        RuntimeContextV3.from_mapping(value)


def test_canonical_parser_rejects_duplicates_and_noncanonical_bytes() -> None:
    with pytest.raises(ContractError, match="duplicate"):
        parse_canonical_object(b'{"schema":"x","schema":"x"}\n')

    with pytest.raises(ContractError, match="canonical"):
        RuntimeContextV3.from_bytes(
            (json.dumps(_runtime_context(), indent=2) + "\n").encode("utf-8")
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("commitTag", "latest", "commit tag"),
        ("imageDigest", "c" * 64, "digest"),
        ("repositoryName", "other/repository", "repository"),
        ("scanStatus", "IN_PROGRESS", "scan"),
        ("signatureStatus", "PENDING", "signature"),
        ("criticalFindings", 1, "findings"),
        ("highFindings", 1, "findings"),
    ],
)
def test_runtime_image_evidence_requires_exact_release_proof(
    field: str, value: object, match: str
) -> None:
    evidence = _image_evidence()
    evidence[field] = value

    with pytest.raises(ContractError, match=match):
        RuntimeImageEvidence.from_mapping(evidence)


def test_runtime_image_evidence_round_trips_canonically() -> None:
    evidence = RuntimeImageEvidence.from_mapping(_image_evidence())

    assert RuntimeImageEvidence.from_bytes(evidence.to_bytes()) == evidence
    assert evidence.image_digest == DIGEST


def test_trusted_lambda_asset_v2_binds_archive_and_sorted_inventories() -> None:
    asset = TrustedLambdaAssetV2.from_mapping(_trusted_asset())

    assert asset.archive_name == "trusted-lambda.zip"
    assert asset.archive_sha256 == "5" * 64
    assert TrustedLambdaAssetV2.from_bytes(asset.to_bytes()) == asset

    unsorted = _trusted_asset()
    unsorted["files"] = [
        {"path": "z.py", "sha256": "6" * 64, "size": 1, "mode": "0644"},
        *unsorted["files"],  # type: ignore[list-item]
    ]
    with pytest.raises(ContractError, match="canonical"):
        TrustedLambdaAssetV2.from_mapping(unsorted)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("architecture", "x86_64", "architecture"),
        ("archiveName", "../trusted.zip", "archive"),
        ("archiveBytes", 0, "archive"),
        ("sourceCommit", "A" * 40, "commit"),
        ("sourceTree", "b" * 39, "tree"),
    ],
)
def test_trusted_lambda_asset_rejects_boundary_drift(
    field: str, value: object, match: str
) -> None:
    manifest = _trusted_asset()
    manifest[field] = value

    with pytest.raises(ContractError, match=match):
        TrustedLambdaAssetV2.from_mapping(manifest)


def test_staging_transaction_contract_enforces_new_state_invariants() -> None:
    transaction = StagingTransactionV1.from_mapping(_transaction())

    assert transaction.state == "NEW"
    assert transaction.to_mapping() == _transaction()

    invalid = _transaction()
    invalid["runtimeId"] = RUNTIME_ID
    with pytest.raises(ContractError, match="NEW"):
        StagingTransactionV1.from_mapping(invalid)

    with pytest.raises(ContractError, match="state"):
        StagingTransactionV1.from_mapping({**_transaction(), "state": "SURPRISE"})


def test_write_new_contract_is_atomic_and_never_clobbers(tmp_path: Path) -> None:
    path = tmp_path / "release" / "runtime-context.json"
    context = RuntimeContextV3.from_mapping(_runtime_context())

    write_new_contract(path, context)
    original = path.read_bytes()

    with pytest.raises(ContractError, match="already exists"):
        write_new_contract(path, context)
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".*.tmp"))


def test_write_new_contract_revalidates_reconstructed_value_objects(tmp_path: Path) -> None:
    context = RuntimeContextV3.from_mapping(_runtime_context())

    with pytest.raises(ContractError, match="region"):
        write_new_contract(
            tmp_path / "runtime-context.json",
            replace(context, region="us-east-1"),
        )

    assert not (tmp_path / "runtime-context.json").exists()
