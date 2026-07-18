from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from release_tools.contracts import (
    ContractError,
    ProductionObservationConfigV1,
    RuntimeContextV3,
    RuntimeImageEvidence,
    StagingTransactionV1,
    TrustedLambdaAssetV2,
    canonical_json_bytes,
    parse_canonical_object,
    parse_release_contract,
    write_new_contract,
)


ACCOUNT = "123456789012"
REGION = "eu-west-1"
COMMIT = "a" * 40
TREE = "b" * 40
DIGEST = "sha256:" + "c" * 64
OPERATION = "sha256:" + "f" * 64
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
    "S3_USER_FILES_BUCKET": "personal-operator-user-files-123456789012",
    "WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME": "workspace-credential-broker",
    "WORKSPACE_SYNC_INTERVAL_MS": "300000",
}
BUILDER_INPUTS = ("sha256:" + "d" * 64, "sha256:" + "e" * 64)


def _production_observation_config() -> dict[str, object]:
    return {
        "schema": "personal-operator.production-observation-config.v1",
        "sourceCommit": COMMIT,
        "sourceTree": TREE,
        "account": ACCOUNT,
        "region": REGION,
        "buildContext": "bridge",
        "builderId": "https://personal-operator.invalid/builders/bridge-v1",
        "builderInputs": list(BUILDER_INPUTS),
        "runtimeSubnetIds": list(SUBNET_IDS),
        "runtimeSecurityGroupIds": list(SECURITY_GROUP_IDS),
        "runtimeEnvironmentVariables": dict(RUNTIME_ENVIRONMENT),
        "runtimeIdleSessionTimeout": 1800,
        "runtimeMaxLifetime": 28800,
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
        "executionRoleArn": ROLE_ARN,
        "runtimeConfiguration": _runtime_configuration(),
        "runtimeConfigurationSha256": _runtime_configuration_sha256(),
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
        "consumerChangesetsSha256": "",
        "consumerApplicationSha256": "",
        "verificationSha256": "",
        "rollbackReference": "",
        "uncertainPhase": "",
        "uncertainOperationSha256": "",
    }


def _transaction_at(state: str) -> dict[str, object]:
    states = (
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
    index = states.index(state)
    value = {
        **_transaction(),
        "state": state,
        "lastStableState": state,
        "revision": index,
    }
    if index >= states.index("FOUNDATION_READY"):
        value["rollbackReference"] = (
            f"rollback:v1:{ACCOUNT}:{REGION}:{COMMIT}:sha256:" + "9" * 64
        )
    if index >= states.index("IMAGE_PUBLISHED"):
        value["runtimeImageDigest"] = DIGEST
    if index >= states.index("RUNTIME_READY"):
        value["runtimeId"] = RUNTIME_ID
        value["runtimeVersion"] = "7"
    if index >= states.index("CONTEXT_WRITTEN"):
        value["runtimeContextSha256"] = "5" * 64
    if index >= states.index("CONSUMER_CHANGESETS_READY"):
        value["consumerChangesetsSha256"] = "6" * 64
    if index >= states.index("CONSUMERS_APPLIED"):
        value["consumerApplicationSha256"] = "7" * 64
    if index >= states.index("VERIFIED"):
        value["verificationSha256"] = "8" * 64
    return value


def test_runtime_context_v3_is_exact_canonical_and_immutable() -> None:
    expected = _runtime_context()
    payload = canonical_json_bytes(expected)

    parsed = RuntimeContextV3.from_bytes(payload)

    assert parsed.to_bytes() == payload
    assert parsed.to_mapping() == expected
    with pytest.raises(FrozenInstanceError):
        parsed.region = "us-east-1"  # type: ignore[misc]


def test_production_observation_config_is_canonical_digest_bound_and_derives_role() -> None:
    expected = _production_observation_config()
    payload = canonical_json_bytes(expected)

    parsed = ProductionObservationConfigV1.from_bytes(payload)

    assert parsed.to_mapping() == expected
    assert parsed.to_bytes() == payload
    assert parsed.digest() == hashlib.sha256(payload).hexdigest()
    assert parsed.execution_role_arn == ROLE_ARN
    assert "executionRoleArn" not in parsed.to_mapping()
    assert parse_release_contract(payload) == parsed


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda value: value.update(extra="x"), "fields"),
        (lambda value: value.update(sourceCommit="A" * 40), "commit"),
        (lambda value: value.update(sourceTree="b" * 39), "tree"),
        (lambda value: value.update(account="000000000000"), "account"),
        (lambda value: value.update(region="us-east-1"), "region"),
        (lambda value: value.update(buildContext="../bridge"), "build context"),
        (lambda value: value.update(builderId="not-https"), "builder"),
        (lambda value: value.update(builderInputs=[]), "builder input"),
        (
            lambda value: value.update(builderInputs=list(reversed(BUILDER_INPUTS))),
            "builder input",
        ),
        (
            lambda value: value.update(builderInputs=[BUILDER_INPUTS[0]] * 2),
            "builder input",
        ),
        (
            lambda value: value.update(runtimeSubnetIds=list(reversed(SUBNET_IDS))),
            "subnet",
        ),
        (
            lambda value: value.update(
                runtimeSecurityGroupIds=[SECURITY_GROUP_IDS[0]] * 2
            ),
            "security group",
        ),
        (
            lambda value: value["runtimeEnvironmentVariables"].update(  # type: ignore[index]
                AWS_SECRET_ACCESS_KEY="not-a-real-secret"
            ),
            "environment",
        ),
        (
            lambda value: value["runtimeEnvironmentVariables"].update(  # type: ignore[index]
                AWS_REGION="us-east-1"
            ),
            "environment",
        ),
        (
            lambda value: value.update(runtimeMaxLifetime=1799),
            "lifecycle",
        ),
        (
            lambda value: value.update(executionRoleArn=ROLE_ARN),
            "fields",
        ),
    ],
)
def test_production_observation_config_rejects_identity_or_configuration_drift(
    mutate,
    match: str,
) -> None:
    value = _production_observation_config()
    mutate(value)

    with pytest.raises(ContractError, match=match):
        ProductionObservationConfigV1.from_mapping(value)


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


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda value: value.update(
                executionRoleArn=(
                    f"arn:aws:iam::{ACCOUNT}:role/caller-selected-same-account-role"
                )
            ),
            "execution role",
        ),
        (
            lambda value: value["runtimeConfiguration"][  # type: ignore[index]
                "environmentVariables"
            ].update(AWS_SECRET_ACCESS_KEY="not-a-real-secret"),
            "environment",
        ),
        (
            lambda value: value["runtimeConfiguration"][  # type: ignore[index]
                "environmentVariables"
            ].update(AWS_REGION="us-east-1"),
            "environment",
        ),
        (
            lambda value: value["runtimeConfiguration"][  # type: ignore[index]
                "networkConfiguration"
            ]["networkModeConfig"].update(subnets=["subnet-99999999999999999"]),
            "configuration digest",
        ),
        (
            lambda value: value.update(runtimeConfigurationSha256="0" * 64),
            "configuration digest",
        ),
    ],
)
def test_runtime_context_binds_deterministic_role_and_complete_configuration(
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


def test_uncertain_transaction_is_bound_to_one_exact_operation_digest() -> None:
    uncertain = {
        **_transaction(),
        "state": "UNCERTAIN",
        "lastStableState": "PREFLIGHTED",
        "revision": 2,
        "rollbackReference": (
            f"rollback:v1:{ACCOUNT}:{REGION}:{COMMIT}:sha256:" + "9" * 64
        ),
        "uncertainPhase": "FOUNDATION_READY",
        "uncertainOperationSha256": OPERATION,
    }

    parsed = StagingTransactionV1.from_mapping(uncertain)

    assert parsed.uncertain_operation_sha256 == OPERATION
    for invalid in ("", "f" * 64, "sha256:" + "F" * 64):
        with pytest.raises(ContractError, match="operation"):
            StagingTransactionV1.from_mapping(
                {**uncertain, "uncertainOperationSha256": invalid}
            )
    with pytest.raises(ContractError, match="operation"):
        StagingTransactionV1.from_mapping(
            {**_transaction(), "uncertainOperationSha256": OPERATION}
        )


def test_rolled_back_transaction_requires_verified_last_stable_state() -> None:
    rolled_back = {
        **_transaction(),
        "state": "ROLLED_BACK",
        "lastStableState": "FOUNDATION_READY",
        "revision": 2,
        "rollbackReference": (
            f"rollback:v1:{ACCOUNT}:{REGION}:{COMMIT}:sha256:" + "9" * 64
        ),
    }

    with pytest.raises(ContractError, match="ROLLED_BACK.*VERIFIED"):
        StagingTransactionV1.from_mapping(rolled_back)


@pytest.mark.parametrize(
    ("field", "owned_state", "prior_state"),
    [
        (
            "consumerChangesetsSha256",
            "CONSUMER_CHANGESETS_READY",
            "CONTEXT_WRITTEN",
        ),
        (
            "consumerApplicationSha256",
            "CONSUMERS_APPLIED",
            "CONSUMER_CHANGESETS_READY",
        ),
        ("verificationSha256", "VERIFIED", "CONSUMERS_APPLIED"),
    ],
)
def test_transaction_phase_evidence_is_exact_and_appears_only_at_owned_state(
    field: str,
    owned_state: str,
    prior_state: str,
) -> None:
    owned = _transaction_at(owned_state)

    parsed = StagingTransactionV1.from_mapping(owned)

    assert parsed.to_mapping() == owned
    with pytest.raises(ContractError, match="evidence is missing"):
        StagingTransactionV1.from_mapping({**owned, field: ""})
    with pytest.raises(ContractError, match="evidence appears before"):
        StagingTransactionV1.from_mapping(
            {**_transaction_at(prior_state), field: "f" * 64}
        )
    with pytest.raises(ContractError, match="digest"):
        StagingTransactionV1.from_mapping(
            {**owned, field: "sha256:" + "f" * 64}
        )


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
