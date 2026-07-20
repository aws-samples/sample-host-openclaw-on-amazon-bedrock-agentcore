from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from release_tools.contracts import (
    AbortRetainedEvidenceV2,
    ContractError,
    FoundationRuntimeInputsV1,
    MutationRequestV2,
    ProductionObservationConfigV1,
    ReleasePlanV2,
    ReleaseStepObservationV2,
    ResolvedMutationRequestV2,
    RuntimeConfigurationV1,
    RuntimeContextV3,
    RuntimeImageEvidence,
    StagingTransactionV1,
    StagingTransactionV2,
    TrustedLambdaAssetV2,
    canonical_json_bytes,
    parse_canonical_object,
    parse_release_contract,
    write_new_contract,
)
from release_tools.image_publication import (
    OCI_CONFIG_MEDIA_TYPE,
    ImagePublicationEffectV1,
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
AGENTCORE_STACK_ID = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/OpenClawAgentCore/"
    "00000000-0000-0000-0000-000000000001"
)


def _consumer_stack_id(stack_name: str, marker: int) -> str:
    return (
        f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{stack_name}/"
        f"00000000-0000-0000-0000-{marker:012d}"
    )


def _consumer_change_set_id(marker: int) -> str:
    return (
        f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:changeSet/"
        f"release-{COMMIT}/00000000-0000-0000-0000-{marker:012d}"
    )
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
BUILDER_INPUTS = ("sha256:" + "d" * 64, "sha256:" + "e" * 64)
FOUNDATION_STACKS = (
    "OpenClawVpc",
    "OpenClawSecurity",
    "OpenClawGuardrails",
    "PersonalOperatorCapabilities",
    "OpenClawAgentCore",
    "OpenClawObservability",
)
CONSUMER_STACKS = (
    "OpenClawRouter",
    "PersonalOperatorWeb",
    "OpenClawCron",
    "PersonalOperatorScheduler",
)

V2_PHASES = (
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


def _release_subject(suffix: str) -> str:
    return f"release:{ACCOUNT}:{REGION}:{COMMIT}:{suffix}"


def _stack_subject(name: str) -> str:
    return f"cfn:{ACCOUNT}:{REGION}:stack:{name}:release:{COMMIT}"


def _v2_steps_and_artifacts() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    asset_id = hashlib.sha256(b"synthesized-cdk-asset-id").hexdigest()
    asset_payload_sha256 = hashlib.sha256(b"trusted-cdk-asset").hexdigest()
    image_subject = (
        f"ecr:{ACCOUNT}:{REGION}:repository:personal-operator/bridge:"
        f"release:{COMMIT}"
    )
    image_effect_prefix = (
        f"ecr:{ACCOUNT}:{REGION}:repository:personal-operator/bridge"
    )
    image_blob_digests = sorted(
        {
            hashlib.sha256(b"runtime-config-blob").hexdigest(),
            hashlib.sha256(b"runtime-layer-blob").hexdigest(),
        }
    )
    image_manifest_digest = DIGEST.removeprefix("sha256:")
    image_publication_plan_sha256 = hashlib.sha256(
        b"image-publication-plan"
    ).hexdigest()
    sbom_referrer_digest = hashlib.sha256(b"sbom-referrer-manifest").hexdigest()
    provenance_referrer_digest = hashlib.sha256(
        b"provenance-referrer-manifest"
    ).hexdigest()
    definitions = (
        ("foundation", "BASELINE_OBSERVE", False, _release_subject("baseline"), ""),
        ("foundation", "BOOTSTRAP_STACK", True, _stack_subject("CDKToolkit"), ""),
        (
            "foundation",
            "ASSET_PUBLISH",
            True,
            f"cdk:asset:{asset_id}",
            asset_payload_sha256,
        ),
        *(
            ("foundation", "STACK_CREATE", True, _stack_subject(stack), "")
            for stack in FOUNDATION_STACKS
        ),
        *(
            (
                "image",
                "IMAGE_PUBLISH",
                True,
                f"{image_effect_prefix}:blob:sha256:{digest}",
                digest,
            )
            for digest in image_blob_digests
        ),
        (
            "image",
            "IMAGE_PUBLISH",
            True,
            (
                f"{image_effect_prefix}:subject-manifest:sha256:"
                f"{image_manifest_digest}:tag:commit-{COMMIT}"
            ),
            image_manifest_digest,
        ),
        (
            "image",
            "IMAGE_PUBLISH",
            True,
            (
                f"{image_effect_prefix}:sbom-referrer-manifest:sha256:"
                f"{sbom_referrer_digest}:subject:sha256:{image_manifest_digest}"
            ),
            sbom_referrer_digest,
        ),
        (
            "image",
            "IMAGE_PUBLISH",
            True,
            (
                f"{image_effect_prefix}:provenance-referrer-manifest:sha256:"
                f"{provenance_referrer_digest}:subject:sha256:"
                f"{image_manifest_digest}"
            ),
            provenance_referrer_digest,
        ),
        ("image", "IMAGE_OBSERVE", False, image_subject, ""),
        ("runtime", "STACK_UPDATE", True, _stack_subject("OpenClawAgentCore"), ""),
        (
            "runtime",
            "AGENTCORE_HARDEN",
            True,
            (
                f"agentcore:{ACCOUNT}:{REGION}:runtime:personal_operator_bridge:"
                f"release:{COMMIT}:mmdsv2"
            ),
            "",
        ),
        ("endpoint", "STACK_UPDATE", True, _stack_subject("OpenClawAgentCore"), ""),
        (
            "context",
            "RUNTIME_CONTEXT_WRITE",
            True,
            _release_subject("artifact:build/runtime-context.json"),
            "",
        ),
        ("router-cron-cs", "CHANGESET_CREATE", True, _stack_subject("OpenClawRouter"), ""),
        ("router-cron-cs", "CHANGESET_CREATE", True, _stack_subject("OpenClawCron"), ""),
        ("router-cron", "CHANGESET_EXECUTE", True, _stack_subject("OpenClawRouter"), ""),
        ("router-cron", "CHANGESET_EXECUTE", True, _stack_subject("OpenClawCron"), ""),
        (
            "scheduler-cs",
            "CHANGESET_CREATE",
            True,
            _stack_subject("PersonalOperatorScheduler"),
            "",
        ),
        (
            "scheduler",
            "CHANGESET_EXECUTE",
            True,
            _stack_subject("PersonalOperatorScheduler"),
            "",
        ),
        ("web-cs", "CHANGESET_CREATE", True, _stack_subject("PersonalOperatorWeb"), ""),
        ("web", "CHANGESET_EXECUTE", True, _stack_subject("PersonalOperatorWeb"), ""),
        ("verify", "VERIFY", False, _release_subject("verify"), ""),
    )
    expanded_definitions: list[tuple[str, str, bool, str, str]] = []
    stack_mutations = {
        "BOOTSTRAP_STACK",
        "STACK_CREATE",
        "STACK_UPDATE",
        "CHANGESET_EXECUTE",
    }
    for phase, kind, mutation, subject, content_override in definitions:
        expanded_definitions.append(
            (phase, kind, mutation, subject, content_override)
        )
        if kind in stack_mutations:
            expanded_definitions.append(
                (phase, "STACK_DRIFT_CHECK", True, f"{subject}:drift", "")
            )
    definitions = tuple(expanded_definitions)
    artifacts: list[dict[str, object]] = []
    steps: list[dict[str, object]] = []
    for ordinal, (phase, kind, mutation, subject, content_override) in enumerate(definitions):
        step_id = f"{ordinal:02d}-{phase}-{kind.lower().replace('_', '-')}"
        path = (
            "build/image-publication-plan.json"
            if kind == "IMAGE_OBSERVE"
            else f"requests/{step_id}.json"
        )
        digest = (
            image_publication_plan_sha256
            if kind == "IMAGE_OBSERVE"
            else hashlib.sha256(path.encode()).hexdigest()
        )
        artifacts.append({"path": path, "size": ordinal + 1, "sha256": digest})
        steps.append(
            {
                "id": step_id,
                "phase": phase,
                "ordinal": ordinal,
                "kind": kind,
                "subject": subject,
                "mutation": mutation,
                "requestArtifact": path,
                "requestSha256": digest,
                "expectedTemplateSha256": (
                    hashlib.sha256(f"template-body:{step_id}".encode()).hexdigest()
                    if (phase, kind)
                    in {
                        ("runtime", "STACK_UPDATE"),
                        ("endpoint", "STACK_UPDATE"),
                    }
                    else ""
                ),
                "expectedTemplateParameterSha256": (
                    hashlib.sha256(f"template:{step_id}".encode()).hexdigest()
                    if kind
                    in {
                        "BOOTSTRAP_STACK",
                        "STACK_CREATE",
                        "CHANGESET_CREATE",
                    }
                    else ""
                ),
                "expectedRequestSha256": digest,
                "expectedObservedRequestSha256": (
                    hashlib.sha256(
                        f"observed-request:{step_id}".encode()
                    ).hexdigest()
                    if kind
                    in {
                        "BOOTSTRAP_STACK",
                        "STACK_CREATE",
                        "STACK_UPDATE",
                        "CHANGESET_CREATE",
                        "CHANGESET_EXECUTE",
                    }
                    else ""
                ),
                "expectedContentSha256": (
                    DIGEST.removeprefix("sha256:")
                    if kind in {"IMAGE_PUBLISH", "IMAGE_OBSERVE"}
                    else hashlib.sha256(f"content:{step_id}".encode()).hexdigest()
                    if kind == "ASSET_PUBLISH"
                    else ""
                ),
            }
        )
        if content_override:
            steps[-1]["expectedContentSha256"] = content_override
    artifacts.sort(key=lambda artifact: artifact["path"])
    return steps, artifacts


def _release_plan_v2() -> dict[str, object]:
    steps, artifacts = _v2_steps_and_artifacts()
    return {
        "schema": "personal-operator.release-plan.v2",
        "transactionId": f"release_{COMMIT}",
        "sourceCommit": COMMIT,
        "sourceTree": TREE,
        "account": ACCOUNT,
        "region": REGION,
        "releaseMode": "CLEAN_ACCOUNT",
        "driverSha256": "1" * 64,
        "evidenceRuntimeSha256": "2" * 64,
        "runtimeImageDigest": DIGEST,
        "runtimeImageUri": IMAGE_URI,
        "runtimeEndpointName": f"release_{COMMIT}",
        "contextRelativePath": "build/runtime-context.json",
        "foundationInputsRelativePath": "build/foundation-runtime-inputs.json",
        "derivationVersion": "foundation-runtime-inputs-v1",
        "artifacts": artifacts,
        "steps": steps,
        "rollbackTarget": {"mode": "NO_PRIOR_RELEASE"},
    }


def _foundation_runtime_inputs_v1() -> dict[str, object]:
    guardrail_id = "abcdefghij"
    plan = ReleasePlanV2.from_mapping(_release_plan_v2())
    return {
        "schema": "personal-operator.foundation-runtime-inputs.v1",
        "sourceCommit": COMMIT,
        "sourceTree": TREE,
        "account": ACCOUNT,
        "region": REGION,
        "releasePlanSha256": plan.digest(),
        "derivationVersion": "foundation-runtime-inputs-v1",
        "privateSubnetIds": list(SUBNET_IDS),
        "runtimeSecurityGroupIds": list(SECURITY_GROUP_IDS),
        "userFilesBucketName": f"openclaw-user-files-{ACCOUNT}-{REGION}",
        "capabilityGatewayFunctionArn": (
            f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:"
            "personal-operator-capability-gateway"
        ),
        "workspaceBrokerFunctionName": (
            "personal-operator-workspace-credential-broker"
        ),
        "agentCoreStackId": AGENTCORE_STACK_ID,
        "guardrailId": guardrail_id,
        "guardrailVersion": "1",
        "guardrailArn": (
            f"arn:aws:bedrock:{REGION}:{ACCOUNT}:guardrail/{guardrail_id}"
        ),
        "foundationSnapshotSha256": "3" * 64,
    }


def _completed_prefix_sha256(
    completed: list[dict[str, object]],
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "schema": "personal-operator.release-completed-prefix.v2",
                "completedSteps": completed,
            }
        )
    ).hexdigest()


def _completed_steps(plan: ReleasePlanV2, count: int) -> list[dict[str, object]]:
    return [
        {
            "stepId": step.step_id,
            "evidenceSha256": hashlib.sha256(
                f"evidence:{step.step_id}".encode()
            ).hexdigest(),
        }
        for step in plan.steps[:count]
    ]


def _mutation_request_v2(plan: ReleasePlanV2, ordinal: int) -> dict[str, object]:
    step = plan.to_mapping()["steps"][ordinal]
    assert isinstance(step, dict)
    completed_prefix_sha256 = _completed_prefix_sha256(
        _completed_steps(plan, ordinal)
    )
    operation_sha256 = "sha256:" + hashlib.sha256(
        canonical_json_bytes(
            {
                "schema": "personal-operator.release-operation.v2",
                "planSha256": plan.digest(),
                "completedPrefixSha256": completed_prefix_sha256,
                "step": step,
            }
        )
    ).hexdigest()
    return {
        "schema": "personal-operator.mutation-request.v2",
        "transactionId": f"release_{COMMIT}",
        "planSha256": plan.digest(),
        "completedPrefixSha256": completed_prefix_sha256,
        "stepId": step["id"],
        "operationSha256": operation_sha256,
        "kind": step["kind"],
        "subject": step["subject"],
        "requestArtifact": step["requestArtifact"],
        "requestSha256": step["requestSha256"],
    }


def _staging_transaction_v2(
    plan: ReleasePlanV2,
    *,
    completed_step_count: int = 0,
    state: str = "PREFLIGHTED",
    last_stable_state: str | None = None,
) -> dict[str, object]:
    plan_steps = plan.to_mapping()["steps"]
    assert isinstance(plan_steps, list)
    completed = _completed_steps(plan, completed_step_count)
    value: dict[str, object] = {
        "schema": "personal-operator.staging-transaction.v2",
        "transactionId": f"release_{COMMIT}",
        "sourceCommit": COMMIT,
        "sourceTree": TREE,
        "account": ACCOUNT,
        "region": REGION,
        "state": state,
        "lastStableState": last_stable_state or state,
        "planSha256": plan.digest(),
        "completedStepCount": completed_step_count,
        "completedSteps": completed,
        "foundationInputsSha256": "",
        "agentCoreStackId": "",
        "runtimeImageDigest": "",
        "runtimeId": "",
        "runtimeVersion": "",
        "runtimeArn": "",
        "runtimeEndpointId": "",
        "runtimeContextSha256": "",
        "routerTargetStackId": "",
        "routerChangeSetId": "",
        "cronTargetStackId": "",
        "cronChangeSetId": "",
        "routerCronChangesetsSha256": "",
        "routerCronApplicationSha256": "",
        "schedulerTargetStackId": "",
        "schedulerChangeSetId": "",
        "schedulerChangesetSha256": "",
        "schedulerApplicationSha256": "",
        "webTargetStackId": "",
        "webChangeSetId": "",
        "webChangesetSha256": "",
        "webApplicationSha256": "",
        "verificationSha256": "",
        "rollbackBaselineSha256": "",
        "abortEvidenceSha256": "",
        "failedRetainedEvidenceSha256": "",
        "failureObservationSha256": "",
        "failedStepId": "",
        "failedSubject": "",
        "failedOperationSha256": "",
        "failureReason": "",
        "uncertainStepId": "",
        "uncertainOperationSha256": "",
        "revision": completed_step_count,
    }
    if completed_step_count >= 1:
        value["rollbackBaselineSha256"] = "5" * 64
    phase_ends = {
        phase: max(step.ordinal for step in plan.steps if step.phase == phase) + 1
        for phase in V2_PHASES
    }
    first_runtime = min(
        step.ordinal for step in plan.steps if step.phase == "runtime"
    ) + 1
    phase_evidence = (
        (phase_ends["foundation"], "foundationInputsSha256", "6" * 64),
        (phase_ends["foundation"], "agentCoreStackId", AGENTCORE_STACK_ID),
        (phase_ends["image"], "runtimeImageDigest", DIGEST),
        (first_runtime, "runtimeId", RUNTIME_ID),
        (first_runtime, "runtimeVersion", "7"),
        (
            first_runtime,
            "runtimeArn",
            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:agent/"
            "12345678-1234-1234-1234-123456789abc:7",
        ),
        (phase_ends["endpoint"], "runtimeEndpointId", ENDPOINT_ID),
        (phase_ends["context"], "runtimeContextSha256", "7" * 64),
        (phase_ends["router-cron-cs"], "routerCronChangesetsSha256", "8" * 64),
        (phase_ends["router-cron"], "routerCronApplicationSha256", "9" * 64),
        (phase_ends["scheduler-cs"], "schedulerChangesetSha256", "a" * 64),
        (phase_ends["scheduler"], "schedulerApplicationSha256", "b" * 64),
        (phase_ends["web-cs"], "webChangesetSha256", "c" * 64),
        (phase_ends["web"], "webApplicationSha256", "d" * 64),
        (phase_ends["verify"], "verificationSha256", "e" * 64),
    )
    for threshold, field, evidence in phase_evidence:
        if completed_step_count >= threshold:
            value[field] = evidence
    for stack_name, prefix, marker in (
        ("OpenClawRouter", "router", 1),
        ("OpenClawCron", "cron", 2),
        ("PersonalOperatorScheduler", "scheduler", 3),
        ("PersonalOperatorWeb", "web", 4),
    ):
        subject = _stack_subject(stack_name)
        threshold = next(
            step.ordinal + 1
            for step in plan.steps
            if step.kind == "CHANGESET_CREATE" and step.subject == subject
        )
        if completed_step_count >= threshold:
            value[f"{prefix}TargetStackId"] = _consumer_stack_id(
                stack_name, marker
            )
            value[f"{prefix}ChangeSetId"] = _consumer_change_set_id(marker)
    if state == "ABORTED_RETAINED":
        value["abortEvidenceSha256"] = "f" * 64
    return value


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
        "foundationStackTemplateParameterDigests": {
            name: hashlib.sha256(f"foundation:{name}".encode()).hexdigest()
            for name in FOUNDATION_STACKS
        },
        "runtimeStackTemplateParameterDigest": "6" * 64,
        "consumerStackTemplateParameterDigests": {
            name: hashlib.sha256(f"consumer:{name}".encode()).hexdigest()
            for name in CONSUMER_STACKS
        },
        "consumerChangeSetContentDigests": {
            name: hashlib.sha256(f"change-set:{name}".encode()).hexdigest()
            for name in CONSUMER_STACKS
        },
        "foundationStackRequestDigests": {
            name: hashlib.sha256(f"foundation-request:{name}".encode()).hexdigest()
            for name in FOUNDATION_STACKS
        },
        "runtimeStackRequestDigest": "7" * 64,
        "consumerStackRequestDigests": {
            name: hashlib.sha256(f"consumer-request:{name}".encode()).hexdigest()
            for name in CONSUMER_STACKS
        },
        "evidenceRuntimeSha256": "9" * 64,
    }


def _runtime_configuration() -> dict[str, object]:
    return {
        "agentRuntimeArtifact": {
            "containerConfiguration": {"containerUri": IMAGE_URI}
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


@pytest.mark.parametrize("version", ("1", "42", "99999999"))
def test_runtime_configuration_accepts_canonical_numbered_guardrail_version(
    version: str,
) -> None:
    value = _runtime_configuration()
    environment = value["environmentVariables"]
    assert isinstance(environment, dict)
    environment.update(
        BEDROCK_GUARDRAIL_ID="abcdefghij",
        BEDROCK_GUARDRAIL_VERSION=version,
    )

    parsed = RuntimeConfigurationV1.from_mapping(
        value,
        runtime_image_uri=IMAGE_URI,
        account=ACCOUNT,
        region=REGION,
    )

    assert dict(parsed.environment_variables)["BEDROCK_GUARDRAIL_VERSION"] == version


@pytest.mark.parametrize("version", ("0", "01", "100000000", "1.0", "draft"))
def test_runtime_configuration_rejects_noncanonical_guardrail_version(
    version: str,
) -> None:
    value = _runtime_configuration()
    environment = value["environmentVariables"]
    assert isinstance(environment, dict)
    environment.update(
        BEDROCK_GUARDRAIL_ID="abcdefghij",
        BEDROCK_GUARDRAIL_VERSION=version,
    )

    with pytest.raises(ContractError, match="guardrail version"):
        RuntimeConfigurationV1.from_mapping(
            value,
            runtime_image_uri=IMAGE_URI,
            account=ACCOUNT,
            region=REGION,
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
            lambda value: value["runtimeEnvironmentVariables"].update(  # type: ignore[index]
                DISABLE_ADOT_OBSERVABILITY="false"
            ),
            "observability",
        ),
        (
            lambda value: value["runtimeEnvironmentVariables"].pop(  # type: ignore[index]
                "DISABLE_ADOT_OBSERVABILITY"
            ),
            "environment",
        ),
        (
            lambda value: value["runtimeEnvironmentVariables"].update(  # type: ignore[index]
                CAPABILITY_GATEWAY_FUNCTION_ARN=(
                    "arn:aws:lambda:eu-west-1:999999999999:function:"
                    "personal-operator-capability-gateway"
                )
            ),
            "gateway",
        ),
        (
            lambda value: value["runtimeEnvironmentVariables"].pop(  # type: ignore[index]
                "CAPABILITY_GATEWAY_FUNCTION_ARN"
            ),
            "environment",
        ),
        (
            lambda value: value.update(runtimeMaxLifetime=1799),
            "lifecycle",
        ),
        (
            lambda value: value[  # type: ignore[index]
                "foundationStackTemplateParameterDigests"
            ].pop("OpenClawVpc"),
            "foundation stack",
        ),
        (
            lambda value: value[  # type: ignore[index]
                "consumerStackTemplateParameterDigests"
            ].update(OpenClawRouter="not-a-digest"),
            "consumer stack",
        ),
        (
            lambda value: value.update(
                runtimeStackTemplateParameterDigest="7" * 63
            ),
            "runtime stack",
        ),
        (
            lambda value: value[  # type: ignore[index]
                "consumerChangeSetContentDigests"
            ].update(Unexpected="8" * 64),
            "consumer change-set",
        ),
        (
            lambda value: value[  # type: ignore[index]
                "foundationStackRequestDigests"
            ].update(OpenClawVpc="invalid"),
            "foundation stack request",
        ),
        (
            lambda value: value.update(runtimeStackRequestDigest="8" * 63),
            "runtime stack request",
        ),
        (
            lambda value: value[  # type: ignore[index]
                "consumerStackRequestDigests"
            ].pop("OpenClawRouter"),
            "consumer stack request",
        ),
        (
            lambda value: value.update(evidenceRuntimeSha256="9" * 63),
            "evidence runtime",
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
            lambda value: value["runtimeConfiguration"].update(  # type: ignore[index]
                authorizerConfiguration={"customJWTAuthorizer": {}}
            ),
            "authorizer",
        ),
        (
            lambda value: value["runtimeConfiguration"].update(  # type: ignore[index]
                requestHeaderConfiguration={
                    "requestHeaderAllowlist": ["Authorization"]
                }
            ),
            "request header",
        ),
        (
            lambda value: value["runtimeConfiguration"].update(  # type: ignore[index]
                metadataConfiguration={"requireMMDSV2": False}
            ),
            "metadata",
        ),
        (
            lambda value: value["runtimeConfiguration"][  # type: ignore[index]
                "networkConfiguration"
            ]["networkModeConfig"].update(subnets=["subnet-99999999999999999"]),
            "configuration digest",
        ),
        (
            lambda value: value["runtimeConfiguration"][  # type: ignore[index]
                "networkConfiguration"
            ]["networkModeConfig"].update(requireServiceS3Endpoint=False),
            "runtime VPC configuration",
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


def test_release_plan_v2_round_trips_closed_clean_account_sequence() -> None:
    expected = _release_plan_v2()
    payload = canonical_json_bytes(expected)

    parsed = ReleasePlanV2.from_bytes(payload)

    assert parsed.to_mapping() == expected
    assert parsed.to_bytes() == payload
    assert parsed.digest() == hashlib.sha256(payload).hexdigest()
    assert tuple(
        dict.fromkeys(step["phase"] for step in parsed.to_mapping()["steps"])
    ) == V2_PHASES
    assert parse_release_contract(payload) == parsed
    with pytest.raises(FrozenInstanceError):
        parsed.steps[0].phase = "image"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda value: value.update(extra="x"), "fields"),
        (lambda value: value.update(releaseMode="UPGRADE"), "release mode"),
        (
            lambda value: value.update(rollbackTarget={"mode": "DELETE_ALL"}),
            "rollback target",
        ),
        (lambda value: value.update(region="us-east-1"), "region"),
        (lambda value: value.update(runtimeImageDigest="f" * 64), "image digest"),
        (
            lambda value: value.update(
                runtimeImageUri=value["runtimeImageUri"].replace(ACCOUNT, "999999999999")
            ),
            "image URI",
        ),
        (
            lambda value: value.update(runtimeEndpointName="mutable-endpoint"),
            "endpoint",
        ),
        (
            lambda value: value.update(contextRelativePath="build/../context.json"),
            "context",
        ),
        (
            lambda value: value.update(
                foundationInputsRelativePath="foundation-runtime-inputs.json"
            ),
            "foundation",
        ),
        (
            lambda value: value.update(derivationVersion="latest"),
            "derivation",
        ),
    ],
)
def test_release_plan_v2_rejects_identity_or_open_rollback_drift(
    mutate: object,
    match: str,
) -> None:
    value = _release_plan_v2()
    assert callable(mutate)
    mutate(value)

    with pytest.raises(ContractError, match=match):
        ReleasePlanV2.from_mapping(value)


def test_release_plan_v2_rejects_noncanonical_artifact_inventory() -> None:
    value = _release_plan_v2()
    artifacts = value["artifacts"]
    assert isinstance(artifacts, list)

    for invalid in (
        list(reversed(artifacts)),
        [*artifacts, deepcopy(artifacts[0])],
        [{**artifacts[0], "path": "../request.json"}, *artifacts[1:]],
        [{**artifacts[0], "size": 0}, *artifacts[1:]],
        [{**artifacts[0], "sha256": "sha256:" + "1" * 64}, *artifacts[1:]],
    ):
        candidate = {**value, "artifacts": invalid}
        with pytest.raises(ContractError, match="artifact"):
            ReleasePlanV2.from_mapping(candidate)


def test_release_plan_v2_requires_every_phase_as_one_contiguous_ordered_run() -> None:
    value = _release_plan_v2()
    steps = value["steps"]
    assert isinstance(steps, list)

    missing_phase = [
        deepcopy(step) for step in steps if step["phase"] != "scheduler-cs"
    ]
    for ordinal, step in enumerate(missing_phase):
        step["ordinal"] = ordinal
    with pytest.raises(ContractError, match="phase"):
        ReleasePlanV2.from_mapping({**value, "steps": missing_phase})

    returned_phase = deepcopy(steps)
    returned_index = next(
        index for index, step in enumerate(returned_phase) if step["phase"] == "endpoint"
    )
    returned_phase[returned_index]["phase"] = "foundation"
    returned_phase[returned_index]["kind"] = "STACK_CREATE"
    returned_phase[returned_index]["mutation"] = True
    returned_phase[returned_index]["expectedTemplateSha256"] = ""
    returned_phase[returned_index]["expectedTemplateParameterSha256"] = "f" * 64
    returned_phase[returned_index]["expectedContentSha256"] = ""
    with pytest.raises(ContractError, match="phase"):
        ReleasePlanV2.from_mapping({**value, "steps": returned_phase})


def test_release_plan_v2_requires_clean_baseline_before_any_mutation() -> None:
    value = _release_plan_v2()
    steps = deepcopy(value["steps"])
    assert isinstance(steps, list)
    steps[:3] = [steps[1], steps[2], steps[0]]
    for ordinal, step in enumerate(steps):
        step["ordinal"] = ordinal

    with pytest.raises(ContractError, match="baseline"):
        ReleasePlanV2.from_mapping({**value, "steps": steps})


def _remove_v2_step(
    value: dict[str, object],
    predicate: object,
) -> dict[str, object]:
    assert callable(predicate)
    candidate = deepcopy(value)
    steps = candidate["steps"]
    artifacts = candidate["artifacts"]
    assert isinstance(steps, list)
    assert isinstance(artifacts, list)
    removed = next(step for step in steps if predicate(step))
    steps.remove(removed)
    removed_steps = [removed]
    if removed["kind"] in {
        "BOOTSTRAP_STACK",
        "STACK_CREATE",
        "STACK_UPDATE",
        "CHANGESET_EXECUTE",
    }:
        drift = next(
            step
            for step in steps
            if step["kind"] == "STACK_DRIFT_CHECK"
            and step["phase"] == removed["phase"]
            and step["subject"] == f"{removed['subject']}:drift"
        )
        steps.remove(drift)
        removed_steps.append(drift)
    removed_artifacts = {step["requestArtifact"] for step in removed_steps}
    artifacts[:] = [
        artifact
        for artifact in artifacts
        if artifact["path"] not in removed_artifacts
    ]
    for ordinal, step in enumerate(steps):
        step["ordinal"] = ordinal
    return candidate


def test_release_plan_v2_requires_exact_foundation_runtime_and_consumer_recipe() -> None:
    value = _release_plan_v2()

    candidates = (
        _remove_v2_step(
            value,
            lambda step: step["phase"] == "foundation"
            and step["kind"] == "ASSET_PUBLISH",
        ),
        _remove_v2_step(
            value,
            lambda step: step["subject"] == _stack_subject("OpenClawVpc"),
        ),
        _remove_v2_step(
            value,
            lambda step: step["kind"] == "AGENTCORE_HARDEN",
        ),
        _remove_v2_step(
            value,
            lambda step: step["phase"] == "router-cron-cs"
            and step["subject"] == _stack_subject("OpenClawCron"),
        ),
    )
    for candidate in candidates:
        with pytest.raises(ContractError, match="recipe|asset|phase"):
            ReleasePlanV2.from_mapping(candidate)

    wrong_subject = deepcopy(value)
    wrong_steps = wrong_subject["steps"]
    assert isinstance(wrong_steps, list)
    stack = next(
        step
        for step in wrong_steps
        if step["subject"] == _stack_subject("OpenClawVpc")
    )
    drift = next(
        step
        for step in wrong_steps
        if step["subject"] == f"{_stack_subject('OpenClawVpc')}:drift"
    )
    stack["subject"] = _stack_subject("OtherVpc")
    drift["subject"] = f"{stack['subject']}:drift"
    with pytest.raises(ContractError, match="recipe"):
        ReleasePlanV2.from_mapping(wrong_subject)

    wrong_runtime_kind = deepcopy(value)
    runtime_step = next(
        step
        for step in wrong_runtime_kind["steps"]
        if step["phase"] == "runtime" and step["kind"] == "STACK_UPDATE"
    )
    runtime_step["kind"] = "STACK_CREATE"
    with pytest.raises(ContractError, match="kind|recipe"):
        ReleasePlanV2.from_mapping(wrong_runtime_kind)


def test_release_plan_v2_accepts_only_sorted_unique_content_bound_cdk_assets() -> None:
    value = _release_plan_v2()
    steps = value["steps"]
    artifacts = value["artifacts"]
    assert isinstance(steps, list)
    assert isinstance(artifacts, list)
    source = deepcopy(
        next(step for step in steps if step["kind"] == "ASSET_PUBLISH")
    )
    source.update(
        id="02b-foundation-asset-publish",
        subject="cdk:asset:" + "f" * 64,
        requestArtifact="requests/02b-foundation-asset-publish.json",
        requestSha256="e" * 64,
        expectedRequestSha256="e" * 64,
        expectedObservedRequestSha256="",
        expectedContentSha256="d" * 64,
    )
    insertion = next(
        index for index, step in enumerate(steps) if step["kind"] == "STACK_CREATE"
    )
    steps.insert(insertion, source)
    artifacts.append(
        {
            "path": source["requestArtifact"],
            "size": 1,
            "sha256": source["requestSha256"],
        }
    )
    artifacts.sort(key=lambda artifact: artifact["path"])
    for ordinal, step in enumerate(steps):
        step["ordinal"] = ordinal

    parsed = ReleasePlanV2.from_mapping(value)
    assets = [step for step in parsed.steps if step.kind == "ASSET_PUBLISH"]
    assert [step.subject for step in assets] == sorted(
        step.subject for step in assets
    )
    assert all(
        step.subject.removeprefix("cdk:asset:") != step.expected_content_sha256
        for step in assets
    )

    unsorted = deepcopy(value)
    asset_indexes = [
        index
        for index, step in enumerate(unsorted["steps"])
        if step["kind"] == "ASSET_PUBLISH"
    ]
    left, right = asset_indexes
    unsorted["steps"][left], unsorted["steps"][right] = (
        unsorted["steps"][right],
        unsorted["steps"][left],
    )
    for ordinal, step in enumerate(unsorted["steps"]):
        step["ordinal"] = ordinal
    with pytest.raises(ContractError, match="sorted and unique"):
        ReleasePlanV2.from_mapping(unsorted)

    duplicate = deepcopy(value)
    duplicate["steps"][right]["subject"] = duplicate["steps"][left]["subject"]
    with pytest.raises(ContractError, match="sorted and unique"):
        ReleasePlanV2.from_mapping(duplicate)

    malformed = deepcopy(value)
    malformed["steps"][left]["subject"] = "cdk:asset:not-an-asset-id"
    with pytest.raises(ContractError, match="asset subject"):
        ReleasePlanV2.from_mapping(malformed)


def test_release_plan_v2_requires_image_publish_then_terminal_observation() -> None:
    value = _release_plan_v2()
    steps = value["steps"]
    assert isinstance(steps, list)

    without_observation = [
        deepcopy(step) for step in steps if step["kind"] != "IMAGE_OBSERVE"
    ]
    for ordinal, step in enumerate(without_observation):
        step["ordinal"] = ordinal
    with pytest.raises(ContractError, match="image.*recipe"):
        ReleasePlanV2.from_mapping({**value, "steps": without_observation})

    reordered = deepcopy(steps)
    image_indexes = [
        index for index, step in enumerate(reordered) if step["phase"] == "image"
    ]
    left, right = image_indexes[:2]
    reordered[left], reordered[right] = reordered[right], reordered[left]
    for ordinal, step in enumerate(reordered):
        step["ordinal"] = ordinal
    with pytest.raises(ContractError, match="image.*(?:recipe|sorted)"):
        ReleasePlanV2.from_mapping({**value, "steps": reordered})


def test_release_plan_v2_requires_one_exact_ordered_image_effect_per_step() -> None:
    value = _release_plan_v2()
    steps = value["steps"]
    artifacts = value["artifacts"]
    assert isinstance(steps, list)
    assert isinstance(artifacts, list)
    image_steps = [step for step in steps if step["phase"] == "image"]
    publishes = image_steps[:-1]

    assert len(publishes) == 5
    assert [step["kind"] for step in publishes] == ["IMAGE_PUBLISH"] * 5
    assert image_steps[-1]["kind"] == "IMAGE_OBSERVE"
    assert [step["subject"] for step in publishes[:2]] == sorted(
        step["subject"] for step in publishes[:2]
    )
    assert ":subject-manifest:" in publishes[2]["subject"]
    assert ":sbom-referrer-manifest:" in publishes[3]["subject"]
    assert ":provenance-referrer-manifest:" in publishes[4]["subject"]
    assert image_steps[-1]["requestArtifact"] == (
        "build/image-publication-plan.json"
    )
    for step in publishes:
        assert step["subject"].split(":sha256:", 1)[1].split(":", 1)[0] == (
            step["expectedContentSha256"]
        )
        assert step["requestSha256"] != step["expectedContentSha256"]

    duplicate = deepcopy(value)
    duplicate_steps = [
        step for step in duplicate["steps"] if step["phase"] == "image"
    ]
    first_blob, second_blob = duplicate_steps[:2]
    second_blob["subject"] = first_blob["subject"]
    second_blob["expectedContentSha256"] = first_blob["expectedContentSha256"]
    with pytest.raises(ContractError, match="image.*(?:digest|subject).*(?:unique|duplicate)"):
        ReleasePlanV2.from_mapping(duplicate)

    crossed = deepcopy(value)
    crossed_publish = next(
        step
        for step in crossed["steps"]
        if step["phase"] == "image" and ":blob:" in step["subject"]
    )
    crossed_publish["subject"] = crossed_publish["subject"].replace(
        f"ecr:{ACCOUNT}:{REGION}:", f"ecr:999999999999:{REGION}:"
    )
    with pytest.raises(ContractError, match="image.*(?:subject|recipe)"):
        ReleasePlanV2.from_mapping(crossed)

    without_blobs = deepcopy(value)
    blob_artifacts = {
        step["requestArtifact"]
        for step in without_blobs["steps"]
        if step["phase"] == "image" and ":blob:" in step["subject"]
    }
    without_blobs["steps"] = [
        step
        for step in without_blobs["steps"]
        if step["requestArtifact"] not in blob_artifacts
    ]
    without_blobs["artifacts"] = [
        artifact
        for artifact in without_blobs["artifacts"]
        if artifact["path"] not in blob_artifacts
    ]
    for ordinal, step in enumerate(without_blobs["steps"]):
        step["ordinal"] = ordinal
    with pytest.raises(ContractError, match="image.*(?:blob|recipe)"):
        ReleasePlanV2.from_mapping(without_blobs)

    manifest_order = deepcopy(value)
    manifest_indexes = [
        index
        for index, step in enumerate(manifest_order["steps"])
        if step["phase"] == "image" and "referrer-manifest" in step["subject"]
    ]
    left, right = manifest_indexes
    manifest_order["steps"][left], manifest_order["steps"][right] = (
        manifest_order["steps"][right],
        manifest_order["steps"][left],
    )
    for ordinal, step in enumerate(manifest_order["steps"]):
        step["ordinal"] = ordinal
    with pytest.raises(ContractError, match="image.*recipe"):
        ReleasePlanV2.from_mapping(manifest_order)

    wrong_referrer_target = deepcopy(value)
    referrer = next(
        step
        for step in wrong_referrer_target["steps"]
        if ":sbom-referrer-manifest:" in step["subject"]
    )
    referrer["subject"] = referrer["subject"].replace(
        f":subject:{DIGEST}", ":subject:sha256:" + "f" * 64
    )
    with pytest.raises(ContractError, match="image referrer.*recipe"):
        ReleasePlanV2.from_mapping(wrong_referrer_target)

    wrong_observe_artifact = deepcopy(value)
    observe = next(
        step
        for step in wrong_observe_artifact["steps"]
        if step["kind"] == "IMAGE_OBSERVE"
    )
    old_path = observe["requestArtifact"]
    observe["requestArtifact"] = "build/unbound-image-plan.json"
    artifact = next(
        item
        for item in wrong_observe_artifact["artifacts"]
        if item["path"] == old_path
    )
    artifact["path"] = observe["requestArtifact"]
    wrong_observe_artifact["artifacts"].sort(key=lambda item: item["path"])
    with pytest.raises(ContractError, match="image publication plan artifact"):
        ReleasePlanV2.from_mapping(wrong_observe_artifact)

    wrong_manifest = deepcopy(value)
    manifest = next(
        step
        for step in wrong_manifest["steps"]
        if ":subject-manifest:" in step["subject"]
    )
    replacement_digest = "f" * 64
    manifest["subject"] = manifest["subject"].replace(
        DIGEST.removeprefix("sha256:"), replacement_digest
    )
    manifest["expectedContentSha256"] = replacement_digest
    with pytest.raises(ContractError, match="image.*(?:digest|manifest|plan)"):
        ReleasePlanV2.from_mapping(wrong_manifest)


def test_release_plan_v2_accepts_distinct_real_image_effect_file_digest(
    tmp_path: Path,
) -> None:
    payload = b"runtime-config-blob"
    content_sha256 = hashlib.sha256(payload).hexdigest()
    effect = ImagePublicationEffectV1(
        publication_plan_sha256="1" * 64,
        effect_id=f"ecr-blob-{content_sha256}",
        effect_kind="ECR_BLOB_PUT",
        source_commit=COMMIT,
        source_tree=TREE,
        account=ACCOUNT,
        region=REGION,
        digest=f"sha256:{content_sha256}",
        media_type=OCI_CONFIG_MEDIA_TYPE,
        size=len(payload),
        tag=None,
        subject_digest=None,
        artifact_type=None,
        payload=payload,
    )
    descriptor = effect.write_private_file(tmp_path / "image-effect.private")
    assert descriptor["sha256"] != content_sha256
    assert descriptor["expectedContent"] == f"sha256:{content_sha256}"

    value = _release_plan_v2()
    step = next(
        step
        for step in value["steps"]
        if step["subject"] == descriptor["providerSubject"]
    )
    artifact = next(
        artifact
        for artifact in value["artifacts"]
        if artifact["path"] == step["requestArtifact"]
    )
    step["requestSha256"] = descriptor["sha256"]
    step["expectedRequestSha256"] = descriptor["sha256"]
    artifact["sha256"] = descriptor["sha256"]
    artifact["size"] = descriptor["size"]

    parsed = ReleasePlanV2.from_mapping(value)
    parsed_step = next(
        item for item in parsed.steps if item.subject == descriptor["providerSubject"]
    )
    assert parsed_step.request_sha256 == descriptor["sha256"]
    assert parsed_step.expected_content_sha256 == content_sha256


def test_release_plan_v2_binds_step_kinds_requests_and_ordinals() -> None:
    value = _release_plan_v2()
    steps = value["steps"]
    assert isinstance(steps, list)

    candidates = []
    invalid_ordinal = deepcopy(steps)
    invalid_ordinal[1]["ordinal"] = 9
    candidates.append((invalid_ordinal, "ordinal"))
    duplicate_id = deepcopy(steps)
    duplicate_id[1]["id"] = duplicate_id[0]["id"]
    candidates.append((duplicate_id, "unique"))
    unknown_kind = deepcopy(steps)
    unknown_kind[1]["kind"] = "ARBITRARY_SHELL"
    candidates.append((unknown_kind, "kind"))
    wrong_mutation = deepcopy(steps)
    wrong_mutation[0]["mutation"] = True
    candidates.append((wrong_mutation, "mutation"))
    missing_artifact = deepcopy(steps)
    missing_artifact[1]["requestArtifact"] = "requests/missing.json"
    candidates.append((missing_artifact, "artifact"))
    mismatched_request = deepcopy(steps)
    mismatched_request[1]["requestSha256"] = "f" * 64
    candidates.append((mismatched_request, "request"))

    for invalid, match in candidates:
        with pytest.raises(ContractError, match=match):
            ReleasePlanV2.from_mapping({**value, "steps": invalid})


@pytest.mark.parametrize(
    ("phase", "kind", "field"),
    [
        ("foundation", "BASELINE_OBSERVE", "requestArtifact"),
        ("foundation", "BOOTSTRAP_STACK", "requestSha256"),
        (
            "foundation",
            "BOOTSTRAP_STACK",
            "expectedTemplateParameterSha256",
        ),
        (
            "foundation",
            "BOOTSTRAP_STACK",
            "expectedObservedRequestSha256",
        ),
        ("foundation", "ASSET_PUBLISH", "expectedContentSha256"),
        ("image", "IMAGE_PUBLISH", "expectedRequestSha256"),
    ],
)
def test_release_plan_v2_requires_kind_specific_request_and_evidence_bindings(
    phase: str,
    kind: str,
    field: str,
) -> None:
    value = _release_plan_v2()
    steps = deepcopy(value["steps"])
    assert isinstance(steps, list)
    step = next(
        step
        for step in steps
        if step["phase"] == phase and step["kind"] == kind
    )
    step[field] = ""

    with pytest.raises(ContractError, match="binding"):
        ReleasePlanV2.from_mapping({**value, "steps": steps})


def test_release_plan_v2_separates_artifact_and_observed_request_digests() -> None:
    value = _release_plan_v2()
    steps = deepcopy(value["steps"])
    assert isinstance(steps, list)

    cfn_step = next(step for step in steps if step["kind"] == "STACK_CREATE")
    cfn_step["expectedObservedRequestSha256"] = cfn_step["requestSha256"]
    with pytest.raises(ContractError, match="observed request.*alias"):
        ReleasePlanV2.from_mapping({**value, "steps": steps})

    steps = deepcopy(value["steps"])
    asset_step = next(step for step in steps if step["kind"] == "ASSET_PUBLISH")
    asset_step["expectedObservedRequestSha256"] = "f" * 64
    with pytest.raises(ContractError, match="observed request.*kind"):
        ReleasePlanV2.from_mapping({**value, "steps": steps})


def test_release_plan_v2_separates_dynamic_update_template_from_static_parameters() -> None:
    value = _release_plan_v2()
    for phase in ("runtime", "endpoint"):
        steps = deepcopy(value["steps"])
        assert isinstance(steps, list)
        update = next(
            step
            for step in steps
            if step["phase"] == phase and step["kind"] == "STACK_UPDATE"
        )
        update["expectedTemplateSha256"] = ""
        with pytest.raises(ContractError, match="template.*binding"):
            ReleasePlanV2.from_mapping({**value, "steps": steps})

    steps = deepcopy(value["steps"])
    runtime_update = next(
        step
        for step in steps
        if step["phase"] == "runtime" and step["kind"] == "STACK_UPDATE"
    )
    runtime_update["expectedTemplateParameterSha256"] = "e" * 64
    with pytest.raises(ContractError, match="template.*kind"):
        ReleasePlanV2.from_mapping({**value, "steps": steps})

    steps = deepcopy(value["steps"])
    runtime_update = next(
        step
        for step in steps
        if step["phase"] == "runtime" and step["kind"] == "STACK_UPDATE"
    )
    runtime_update["expectedTemplateSha256"] = "sha256:" + "e" * 64
    with pytest.raises(ContractError, match="update template digest"):
        ReleasePlanV2.from_mapping({**value, "steps": steps})

    steps = deepcopy(value["steps"])
    cross_kind = next(step for step in steps if step["kind"] == "AGENTCORE_HARDEN")
    cross_kind["expectedTemplateSha256"] = "e" * 64
    with pytest.raises(ContractError, match="template.*binding"):
        ReleasePlanV2.from_mapping({**value, "steps": steps})

    for kind in (
        "AGENTCORE_HARDEN",
        "RUNTIME_CONTEXT_WRITE",
        "CHANGESET_CREATE",
        "CHANGESET_EXECUTE",
        "VERIFY",
    ):
        steps = deepcopy(value["steps"])
        generated = next(step for step in steps if step["kind"] == kind)
        generated["expectedContentSha256"] = "e" * 64
        with pytest.raises(ContractError, match="content.*kind"):
            ReleasePlanV2.from_mapping({**value, "steps": steps})


def test_release_plan_v2_image_content_is_the_exact_planned_digest() -> None:
    value = _release_plan_v2()
    steps = deepcopy(value["steps"])
    assert isinstance(steps, list)
    image_observe = next(step for step in steps if step["kind"] == "IMAGE_OBSERVE")
    image_observe["expectedContentSha256"] = "e" * 64

    with pytest.raises(ContractError, match="image content.*plan"):
        ReleasePlanV2.from_mapping({**value, "steps": steps})


def test_release_plan_v2_rejects_unreferenced_inventory_artifacts() -> None:
    value = _release_plan_v2()
    artifacts = deepcopy(value["artifacts"])
    assert isinstance(artifacts, list)
    artifacts.append(
        {
            "path": "requests/zz-unreferenced.json",
            "size": 1,
            "sha256": "f" * 64,
        }
    )

    with pytest.raises(ContractError, match="inventory.*reference"):
        ReleasePlanV2.from_mapping({**value, "artifacts": artifacts})


def test_foundation_runtime_inputs_v1_round_trips_exact_outputs() -> None:
    expected = _foundation_runtime_inputs_v1()
    payload = canonical_json_bytes(expected)

    parsed = FoundationRuntimeInputsV1.from_bytes(payload)

    assert parsed.to_mapping() == expected
    assert parsed.to_bytes() == payload
    assert parse_release_contract(payload) == parsed


def test_foundation_runtime_inputs_v1_binds_exact_agentcore_stack_id() -> None:
    value = _foundation_runtime_inputs_v1()
    value["agentCoreStackId"] = AGENTCORE_STACK_ID

    parsed = FoundationRuntimeInputsV1.from_mapping(value)

    assert parsed.agent_core_stack_id == AGENTCORE_STACK_ID
    for hostile in (
        AGENTCORE_STACK_ID.replace(ACCOUNT, "999999999999"),
        AGENTCORE_STACK_ID.replace("OpenClawAgentCore", "OpenClawVpc"),
    ):
        candidate = deepcopy(value)
        candidate["agentCoreStackId"] = hostile
        with pytest.raises(ContractError, match="stack ID"):
            FoundationRuntimeInputsV1.from_mapping(candidate)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda value: value.update(extra="x"), "fields"),
        (lambda value: value.update(sourceCommit="A" * 40), "commit"),
        (lambda value: value.update(sourceTree="B" * 40), "tree"),
        (
            lambda value: value.update(releasePlanSha256="sha256:" + "1" * 64),
            "plan digest",
        ),
        (
            lambda value: value.update(derivationVersion="latest"),
            "derivation version",
        ),
        (
            lambda value: value.update(privateSubnetIds=list(reversed(SUBNET_IDS))),
            "subnet",
        ),
        (
            lambda value: value.update(
                runtimeSecurityGroupIds=[SECURITY_GROUP_IDS[0]] * 2
            ),
            "security group",
        ),
        (
            lambda value: value.update(userFilesBucketName="some-bucket"),
            "bucket",
        ),
        (
            lambda value: value.update(capabilityGatewayFunctionArn="*"),
            "gateway",
        ),
        (
            lambda value: value.update(workspaceBrokerFunctionName="broker-latest"),
            "broker",
        ),
        (lambda value: value.update(guardrailVersion=""), "guardrail"),
        (lambda value: value.update(guardrailArn="arn:aws:bedrock:*"), "guardrail"),
        (
            lambda value: value.update(foundationSnapshotSha256="sha256:" + "3" * 64),
            "snapshot",
        ),
    ],
)
def test_foundation_runtime_inputs_v1_rejects_cross_subject_or_partial_outputs(
    mutate: object,
    match: str,
) -> None:
    value = _foundation_runtime_inputs_v1()
    assert callable(mutate)
    mutate(value)

    with pytest.raises(ContractError, match=match):
        FoundationRuntimeInputsV1.from_mapping(value)


def test_foundation_runtime_inputs_v1_allows_guardrail_to_be_atomically_absent() -> None:
    value = _foundation_runtime_inputs_v1()
    value.update(guardrailId="", guardrailVersion="", guardrailArn="")

    parsed = FoundationRuntimeInputsV1.from_mapping(value)

    assert parsed.guardrail_id == ""
    assert parsed.guardrail_version == ""
    assert parsed.guardrail_arn == ""


def test_foundation_runtime_inputs_v1_rejects_a_different_plan_identity() -> None:
    plan = ReleasePlanV2.from_mapping(_release_plan_v2())
    for field, invalid in (
        ("sourceCommit", "d" * 40),
        ("sourceTree", "d" * 40),
        ("releasePlanSha256", "d" * 64),
    ):
        value = _foundation_runtime_inputs_v1()
        value[field] = invalid
        inputs = FoundationRuntimeInputsV1.from_mapping(value)

        with pytest.raises(ContractError, match="identity differs"):
            inputs.validate_plan_identity(plan)


def test_release_step_observation_v2_binds_observer_and_derived_bytes() -> None:
    plan = ReleasePlanV2.from_mapping(_release_plan_v2())
    ordinal = max(
        step.ordinal for step in plan.steps if step.phase == "foundation"
    )
    step = plan.steps[ordinal]
    value = {
        "schema": ReleaseStepObservationV2.SCHEMA,
        "planSha256": plan.digest(),
        "stepId": step.step_id,
        "subject": step.subject,
        "observerEvidenceSha256": "4" * 64,
        "foundationRuntimeInputs": _foundation_runtime_inputs_v1(),
        "agentCoreStackId": "",
        "runtimeImageDigest": "",
        "runtimeId": "",
        "runtimeVersion": "",
        "runtimeArn": "",
        "runtimeEndpointId": "",
        "runtimeContextSha256": "",
        "routerTargetStackId": "",
        "routerChangeSetId": "",
        "cronTargetStackId": "",
        "cronChangeSetId": "",
        "routerCronChangesetsSha256": "",
        "routerCronApplicationSha256": "",
        "schedulerTargetStackId": "",
        "schedulerChangeSetId": "",
        "schedulerChangesetSha256": "",
        "schedulerApplicationSha256": "",
        "webTargetStackId": "",
        "webChangeSetId": "",
        "webChangesetSha256": "",
        "webApplicationSha256": "",
        "verificationSha256": "",
    }
    observation = ReleaseStepObservationV2.from_mapping(value)

    observation.validate_plan_step(plan, completed_step_count=ordinal)
    assert observation.digest() == hashlib.sha256(observation.to_bytes()).hexdigest()
    assert parse_release_contract(observation.to_bytes()) == observation

    assert observation.foundation_runtime_inputs is not None
    hostile = replace(
        observation,
        foundation_runtime_inputs=replace(
            observation.foundation_runtime_inputs, source_tree="d" * 40
        ),
    )
    with pytest.raises(ContractError, match="identity differs"):
        hostile.validate_plan_step(plan, completed_step_count=ordinal)


def test_mutation_request_v2_is_bound_to_the_exact_next_plan_step() -> None:
    plan = ReleasePlanV2.from_mapping(_release_plan_v2())
    expected = _mutation_request_v2(plan, 2)
    payload = canonical_json_bytes(expected)

    parsed = MutationRequestV2.from_bytes(
        payload,
        plan=plan,
        completed_step_count=2,
        completed_prefix_sha256=expected["completedPrefixSha256"],
    )

    assert parsed.to_mapping() == expected
    assert parsed.to_bytes() == payload
    assert parse_release_contract(payload) == MutationRequestV2.from_mapping(expected)
    parsed.validate_plan(
        plan,
        completed_step_count=2,
        completed_prefix_sha256=expected["completedPrefixSha256"],
    )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("transactionId", "release_other", "transaction"),
        ("planSha256", "0" * 64, "plan"),
        ("completedPrefixSha256", "7" * 64, "completed prefix"),
        ("stepId", "03-foundation-stack-create", "next"),
        ("operationSha256", "4" * 64, "operation"),
        ("operationSha256", "sha256:" + "0" * 64, "operation"),
        ("kind", "STACK_UPDATE", "kind"),
        ("subject", "other", "subject"),
        ("requestArtifact", "requests/other.json", "artifact"),
        ("requestSha256", "0" * 64, "request"),
    ],
)
def test_mutation_request_v2_rejects_any_plan_or_next_step_drift(
    field: str,
    value: object,
    match: str,
) -> None:
    plan = ReleasePlanV2.from_mapping(_release_plan_v2())
    request = _mutation_request_v2(plan, 2)
    completed_prefix_sha256 = request["completedPrefixSha256"]
    request[field] = value

    with pytest.raises(ContractError, match=match):
        MutationRequestV2.from_mapping(
            request,
            plan=plan,
            completed_step_count=2,
            completed_prefix_sha256=completed_prefix_sha256,
        )


def test_mutation_request_v2_rejects_a_non_mutating_next_step() -> None:
    plan = ReleasePlanV2.from_mapping(_release_plan_v2())
    request = _mutation_request_v2(plan, 0)

    with pytest.raises(ContractError, match="not a mutation"):
        MutationRequestV2.from_mapping(
            request,
            plan=plan,
            completed_step_count=0,
            completed_prefix_sha256=request["completedPrefixSha256"],
        )

    with pytest.raises(ContractError, match="not a mutation"):
        MutationRequestV2.from_mapping(request)


def test_staging_transaction_v2_accepts_partial_and_phase_boundary_prefixes() -> None:
    plan = ReleasePlanV2.from_mapping(_release_plan_v2())
    foundation_end = max(
        step.ordinal for step in plan.steps if step.phase == "foundation"
    ) + 1
    partial = _staging_transaction_v2(
        plan,
        completed_step_count=1,
        state="PREFLIGHTED",
    )
    boundary = _staging_transaction_v2(
        plan,
        completed_step_count=foundation_end,
        state="FOUNDATION_READY",
    )

    parsed_partial = StagingTransactionV2.from_mapping(partial, plan=plan)
    parsed_boundary = StagingTransactionV2.from_mapping(boundary, plan=plan)

    assert parsed_partial.to_mapping() == partial
    assert parsed_boundary.to_mapping() == boundary
    assert StagingTransactionV2.from_bytes(
        parsed_boundary.to_bytes(), plan=plan
    ) == parsed_boundary
    parsed_partial.validate_plan(plan)
    parsed_boundary.validate_plan(plan)
    assert parse_release_contract(parsed_boundary.to_bytes()) == (
        StagingTransactionV2.from_mapping(boundary)
    )


def test_staging_transaction_v2_uncertainty_names_the_exact_next_step() -> None:
    plan = ReleasePlanV2.from_mapping(_release_plan_v2())
    foundation_end = max(
        step.ordinal for step in plan.steps if step.phase == "foundation"
    ) + 1
    next_step = plan.to_mapping()["steps"][foundation_end]
    completed = _completed_steps(plan, foundation_end)
    completed_prefix_sha256 = _completed_prefix_sha256(completed)
    operation_sha256 = "sha256:" + hashlib.sha256(
        canonical_json_bytes(
            {
                "schema": "personal-operator.release-operation.v2",
                "planSha256": plan.digest(),
                "completedPrefixSha256": completed_prefix_sha256,
                "step": next_step,
            }
        )
    ).hexdigest()
    value = _staging_transaction_v2(
        plan,
        completed_step_count=foundation_end,
        state="UNCERTAIN",
        last_stable_state="FOUNDATION_READY",
    )
    value.update(
        uncertainStepId=next_step["id"],
        uncertainOperationSha256=operation_sha256,
    )

    parsed = StagingTransactionV2.from_mapping(value, plan=plan)

    assert parsed.to_mapping() == value
    for field, invalid in (
        ("uncertainStepId", "wrong-step"),
        ("uncertainOperationSha256", "f" * 64),
        ("uncertainOperationSha256", "sha256:" + "0" * 64),
        ("revision", 0),
    ):
        candidate = {**value, field: invalid}
        with pytest.raises(ContractError, match="uncertain"):
            StagingTransactionV2.from_mapping(candidate, plan=plan)


def test_staging_transaction_v2_rejects_non_prefix_or_wrong_stable_boundary() -> None:
    plan = ReleasePlanV2.from_mapping(_release_plan_v2())
    foundation_end = max(
        step.ordinal for step in plan.steps if step.phase == "foundation"
    ) + 1
    value = _staging_transaction_v2(
        plan,
        completed_step_count=foundation_end,
        state="FOUNDATION_READY",
    )
    wrong_prefix = deepcopy(value)
    wrong_prefix["completedSteps"][1]["stepId"] = "different-step"
    with pytest.raises(ContractError, match="prefix"):
        StagingTransactionV2.from_mapping(wrong_prefix, plan=plan)

    wrong_boundary = {**value, "state": "IMAGE_PUBLISHED", "lastStableState": "IMAGE_PUBLISHED"}
    with pytest.raises(ContractError, match="stable state"):
        StagingTransactionV2.from_mapping(wrong_boundary, plan=plan)


def test_staging_transaction_v2_requires_atomic_versioned_runtime_arn() -> None:
    plan = ReleasePlanV2.from_mapping(_release_plan_v2())
    first_runtime_end = min(
        step.ordinal for step in plan.steps if step.phase == "runtime"
    ) + 1
    value = _staging_transaction_v2(
        plan,
        completed_step_count=first_runtime_end,
        state="IMAGE_PUBLISHED",
    )

    for field, invalid, match in (
        ("runtimeArn", "", "atomic"),
        (
            "runtimeArn",
            value["runtimeArn"].replace(ACCOUNT, "999999999999"),
            "account or region",
        ),
        ("runtimeVersion", "8", "ARN and version"),
    ):
        candidate = {**value, field: invalid}
        with pytest.raises(ContractError, match=match):
            StagingTransactionV2.from_mapping(candidate, plan=plan)


def test_clean_account_transaction_can_abort_retained_but_never_roll_back() -> None:
    plan = ReleasePlanV2.from_mapping(_release_plan_v2())
    foundation_end = max(
        step.ordinal for step in plan.steps if step.phase == "foundation"
    ) + 1
    retained = _staging_transaction_v2(
        plan,
        completed_step_count=foundation_end,
        state="ABORTED_RETAINED",
        last_stable_state="FOUNDATION_READY",
    )

    parsed = StagingTransactionV2.from_mapping(retained, plan=plan)

    assert parsed.state == "ABORTED_RETAINED"
    assert parsed.abort_evidence_sha256 == "f" * 64
    with pytest.raises(ContractError, match="exactly one terminal evidence"):
        StagingTransactionV2.from_mapping(
            {**retained, "abortEvidenceSha256": ""}, plan=plan
        )
    with pytest.raises(ContractError, match="outside ABORTED_RETAINED"):
        StagingTransactionV2.from_mapping(
            {
                **retained,
                "state": "FOUNDATION_READY",
                "abortEvidenceSha256": "f" * 64,
            },
            plan=plan,
        )
    rolled_back = {**retained, "state": "ROLLED_BACK"}
    with pytest.raises(ContractError, match="CLEAN_ACCOUNT.*ROLLED_BACK"):
        StagingTransactionV2.from_mapping(rolled_back, plan=plan)
    with pytest.raises(ContractError, match="CLEAN_ACCOUNT.*ROLLED_BACK"):
        StagingTransactionV2.from_mapping(rolled_back)


def test_abort_retained_evidence_v2_binds_exact_stable_prefix_and_reason() -> None:
    plan = ReleasePlanV2.from_mapping(_release_plan_v2())
    foundation_end = max(
        step.ordinal for step in plan.steps if step.phase == "foundation"
    ) + 1
    transaction = StagingTransactionV2.from_mapping(
        _staging_transaction_v2(
            plan,
            completed_step_count=foundation_end,
            state="FOUNDATION_READY",
        ),
        plan=plan,
    )
    value = {
        "schema": AbortRetainedEvidenceV2.SCHEMA,
        "planSha256": plan.digest(),
        "completedPrefixSha256": _completed_prefix_sha256(
            [step.to_mapping() for step in transaction.completed_steps]
        ),
        "completedStepCount": foundation_end,
        "retainedSteps": [
            {"stepId": step.step_id, "subject": step.subject}
            for step in plan.steps[:foundation_end]
        ],
        "stableState": "FOUNDATION_READY",
        "stopReason": "SECURITY_REVIEW_FINDING",
    }
    evidence = AbortRetainedEvidenceV2.from_mapping(value)

    evidence.validate_transaction(plan, transaction)
    assert evidence.digest() == hashlib.sha256(evidence.to_bytes()).hexdigest()
    assert parse_release_contract(evidence.to_bytes()) == evidence

    for candidate in (
        replace(evidence, plan_sha256="f" * 64),
        replace(evidence, completed_prefix_sha256="f" * 64),
        replace(evidence, stable_state="IMAGE_PUBLISHED"),
    ):
        with pytest.raises(ContractError, match="differs"):
            candidate.validate_transaction(plan, transaction)
    with pytest.raises(ContractError, match="stop reason"):
        AbortRetainedEvidenceV2.from_mapping(
            {**value, "stopReason": "FREE_FORM_REASON"}
        )
