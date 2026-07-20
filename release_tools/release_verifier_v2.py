"""Trusted, read-only terminal verification for clean-account release v2.

The verifier never accepts a caller-provided evidence inventory.  It obtains
the exact completed prefix from the journal-bound :class:`ReleaseEvidenceStoreV2`,
which first audits every retained record, provider-effect receipt, transition,
and commit.  Only then does this module perform fresh IAM, AgentCore, and local
runtime-context observations.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import re
from typing import Any, ClassVar, Mapping, Sequence

from release_tools.aws_authority_v2 import AttestedAwsClientV2, AwsAuthorityError
from release_tools.contracts import (
    ContractError,
    FoundationRuntimeInputsV1,
    ReleasePlanV2,
    RetainedStepEvidenceV2,
    RuntimeConfigurationV1,
    StagingTransactionV2,
    _canonical_release_plan_v2,
    _completed_prefix_sha256,
    canonical_json_bytes,
    expected_execution_role_arn,
    parse_canonical_object,
)
from release_tools.evidence_store_v2 import (
    EvidenceStoreV2Error,
    ReleaseEvidenceStoreV2,
    _journal_path_sha256,
)
from release_tools.runtime_context_v2 import (
    RuntimeContextFileV2,
    RuntimeContextV2Error,
    RuntimeContextWriteRequestV2,
    derive_trusted_runtime_context_inputs,
)
from release_tools.runtime_iam_observer_v2 import (
    RuntimeIamObservationRequestV1,
    RuntimeIamObserverV2,
    RuntimeIamObserverV2Error,
)
from release_tools.transaction import ObservationDisposition


_SHA256 = re.compile(r"[0-9a-f]{64}")
_PROVIDER_ENDPOINT_ARN = re.compile(
    r"arn:aws:bedrock-agentcore:eu-west-1:([0-9]{12}):agentEndpoint/"
    r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
    r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}"
)
_WORKLOAD_IDENTITY_ARN = re.compile(
    r"arn:aws:bedrock-agentcore:eu-west-1:([0-9]{12}):"
    r"workload-identity-directory/default/workload-identity/"
    r"[A-Za-z0-9_.-]{3,255}"
)
_VERIFICATION_OBSERVATION_TOKEN = object()

_RUNTIME_RESPONSE_REQUIRED_FIELDS = frozenset(
    {
        "agentRuntimeId",
        "agentRuntimeName",
        "agentRuntimeVersion",
        "agentRuntimeArn",
        "roleArn",
        "description",
        "status",
        "agentRuntimeArtifact",
        "environmentVariables",
        "filesystemConfigurations",
        "lifecycleConfiguration",
        "metadataConfiguration",
        "networkConfiguration",
        "protocolConfiguration",
        "workloadIdentityDetails",
    }
)
_RUNTIME_RESPONSE_OPTIONAL_FIELDS = frozenset(
    {
        "authorizerConfiguration",
        "requestHeaderConfiguration",
        "failureReason",
        # Provider-owned timestamps are explicitly non-authority-bearing.
        "createdAt",
        "lastUpdatedAt",
    }
)

_IMAGE_PROJECTION_FIELDS = frozenset(
    {
        "repositoryName",
        "commitTag",
        "runtimeImageDigest",
        "imageUri",
        "scanStatus",
        "criticalFindings",
        "highFindings",
        "sbomManifestDigest",
        "provenanceManifestDigest",
        "signingProfileArn",
        "signatureStatus",
    }
)
_ENDPOINT_PROJECTION_FIELDS = frozenset(
    {
        "agentCoreStackId",
        "cloudFormationTemplateSha256",
        "cloudFormationRequestSha256",
        "runtimeId",
        "runtimeVersion",
        "runtimeArn",
        "runtimeConfiguration",
        "runtimeConfigurationSha256",
        "guardrailId",
        "guardrailVersion",
        "requiresMMDSV2",
        "requiresServiceS3Endpoint",
        "endpointId",
        "endpointName",
        "endpointArn",
        "workloadIdentityArn",
    }
)
_VERIFICATION_PROJECTION_FIELDS = frozenset(
    {
        "planSha256",
        "transactionSha256",
        "completedPrefixSha256",
        "retainedPrefixSha256",
        "evidenceStoreSha256",
        "journalPathSha256",
        "journalExecutionId",
        "journalRevision",
        "completedRecordCount",
        "foundationInputsSha256",
        "runtimeImageDigest",
        "imageObservationSha256",
        "runtimeId",
        "runtimeVersion",
        "runtimeArn",
        "runtimeEndpointId",
        "runtimeEndpointName",
        "runtimeEndpointArn",
        "runtimeWorkloadIdentityArn",
        "runtimeConfigurationSha256",
        "runtimeIamRequestSha256",
        "runtimeIamObservationSha256",
        "runtimeContextSha256",
        "runtimeContextObservationSha256",
        "guardrailId",
        "guardrailVersion",
    }
)


class ReleaseVerifierV2Error(RuntimeError):
    """The release cannot satisfy the exact terminal verification contract."""


class ReleaseVerifierV2Ambiguous(ReleaseVerifierV2Error):
    """A fresh read failed or changed; never infer a successful release."""


@dataclass(frozen=True, slots=True, init=False)
class ReleaseVerificationObservationV2:
    """Private canonical evidence for the sole final ``VERIFY`` step."""

    SCHEMA: ClassVar[str] = "personal-operator.canonical-read-observation.v2"

    service: str
    operation: str
    subject: str
    disposition: ObservationDisposition
    provider_status: str
    projection_bytes: bytes

    def __init__(
        self,
        *,
        subject: str,
        projection: Mapping[str, Any],
        _token: object | None = None,
    ) -> None:
        if _token is not _VERIFICATION_OBSERVATION_TOKEN:
            raise ReleaseVerifierV2Error(
                "release verification observation is not constructible"
            )
        if (
            not isinstance(subject, str)
            or not subject
            or "\x00" in subject
            or not isinstance(projection, Mapping)
            or set(projection) != _VERIFICATION_PROJECTION_FIELDS
        ):
            raise ReleaseVerifierV2Error(
                "release verification observation is invalid"
            )
        try:
            projection_bytes = canonical_json_bytes(dict(projection))
            if set(parse_canonical_object(projection_bytes)) != (
                _VERIFICATION_PROJECTION_FIELDS
            ):
                raise ContractError("verification projection fields differ")
        except (ContractError, TypeError, ValueError) as error:
            raise ReleaseVerifierV2Error(
                "release verification projection is not canonical"
            ) from error
        object.__setattr__(self, "service", "local-release-verifier")
        object.__setattr__(self, "operation", "verify_release")
        object.__setattr__(self, "subject", subject)
        object.__setattr__(
            self, "disposition", ObservationDisposition.PRESENT
        )
        object.__setattr__(self, "provider_status", "VERIFIED")
        object.__setattr__(self, "projection_bytes", projection_bytes)

    def projection(self) -> dict[str, Any]:
        return parse_canonical_object(self.projection_bytes)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "service": self.service,
            "operation": self.operation,
            "subject": self.subject,
            "disposition": self.disposition.value,
            "providerStatus": self.provider_status,
            "projection": self.projection(),
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


def _canonical_transaction(
    transaction: StagingTransactionV2, plan: ReleasePlanV2
) -> StagingTransactionV2:
    try:
        return StagingTransactionV2.from_bytes(
            transaction.to_bytes(), plan=plan
        )
    except (AttributeError, ContractError, TypeError, ValueError) as error:
        raise ReleaseVerifierV2Error(
            "release verification transaction differs from its plan"
        ) from error


def _require_preverify_boundary(
    plan: ReleasePlanV2, transaction: StagingTransactionV2
) -> object:
    verify_steps = [step for step in plan.steps if step.kind == "VERIFY"]
    if len(verify_steps) != 1:
        raise ReleaseVerifierV2Error(
            "release plan does not have one exact VERIFY step"
        )
    verify = verify_steps[0]
    expected_subject = (
        f"release:{plan.account}:{plan.region}:{plan.source_commit}:verify"
    )
    if (
        verify.ordinal != len(plan.steps) - 1
        or verify.phase != "verify"
        or verify.mutation
        or verify.subject != expected_subject
        or transaction.state != "WEB_APPLIED"
        or transaction.last_stable_state != "WEB_APPLIED"
        or transaction.completed_step_count != verify.ordinal
        or transaction.completed_step_count != len(plan.steps) - 1
        or transaction.verification_sha256
    ):
        raise ReleaseVerifierV2Error(
            "release verification requires the exact stable pre-VERIFY boundary"
        )
    return verify


def _canonical_prefix(
    *,
    plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
    records: object,
    store: ReleaseEvidenceStoreV2,
    journal_path: Path,
    journal_execution_id: str,
) -> tuple[RetainedStepEvidenceV2, ...]:
    if not isinstance(records, tuple) or len(records) != (
        transaction.completed_step_count
    ):
        raise ReleaseVerifierV2Error(
            "evidence store did not return the complete ordered prefix"
        )
    path_sha256 = _journal_path_sha256(journal_path)
    completed_mappings: list[dict[str, str]] = []
    canonical: list[RetainedStepEvidenceV2] = []
    prior_revision = -1
    for ordinal, candidate in enumerate(records):
        try:
            record = RetainedStepEvidenceV2.from_bytes(candidate.to_bytes())
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise ReleaseVerifierV2Error(
                "evidence store returned a noncanonical retained record"
            ) from error
        completed = transaction.completed_steps[ordinal]
        step = plan.steps[ordinal]
        if (
            record != candidate
            or record.digest() != completed.evidence_sha256
            or completed.step_id != step.step_id
            or record.step_id != step.step_id
            or record.subject != step.subject
            or record.plan_sha256 != plan.digest()
            or record.evidence_store_sha256 != store.identity_sha256
            or record.journal_path_sha256 != path_sha256
            or record.journal_execution_id != journal_execution_id
            or record.completed_prefix_sha256
            != _completed_prefix_sha256(completed_mappings)
            or record.disposition != "PRESENT"
            or record.step_observation is None
            or record.journal_revision <= prior_revision
        ):
            raise ReleaseVerifierV2Error(
                "retained prefix crosses its plan, store, or journal"
            )
        canonical.append(record)
        completed_mappings.append(completed.to_mapping())
        prior_revision = record.journal_revision
    if not canonical or canonical[-1].journal_revision + 1 != transaction.revision:
        raise ReleaseVerifierV2Error(
            "retained prefix does not close the current journal revision"
        )
    return tuple(canonical)


def _single_record(
    plan: ReleasePlanV2,
    records: Sequence[RetainedStepEvidenceV2],
    *,
    phase: str,
    kind: str,
) -> tuple[object, RetainedStepEvidenceV2]:
    matches = [
        (plan.steps[index], record)
        for index, record in enumerate(records)
        if plan.steps[index].phase == phase and plan.steps[index].kind == kind
    ]
    if len(matches) != 1:
        raise ReleaseVerifierV2Error(
            f"retained {phase}/{kind} record is not singular"
        )
    return matches[0]


def _foundation_inputs(
    plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
    records: Sequence[RetainedStepEvidenceV2],
) -> FoundationRuntimeInputsV1:
    candidates = [
        (index, record.step_observation.foundation_runtime_inputs)
        for index, record in enumerate(records)
        if record.step_observation is not None
        and record.step_observation.foundation_runtime_inputs is not None
    ]
    foundation_end = max(
        step.ordinal for step in plan.steps if step.phase == "foundation"
    )
    if len(candidates) != 1 or candidates[0][0] != foundation_end:
        raise ReleaseVerifierV2Error(
            "retained foundation inputs are not singular at their exact owner"
        )
    foundation = candidates[0][1]
    assert foundation is not None
    try:
        foundation = FoundationRuntimeInputsV1.from_bytes(
            foundation.to_bytes()
        )
        foundation.validate_plan_identity(plan)
    except (AttributeError, ContractError, TypeError, ValueError) as error:
        raise ReleaseVerifierV2Error(
            "retained foundation inputs cross the release"
        ) from error
    if (
        foundation.digest() != transaction.foundation_inputs_sha256
        or foundation.agent_core_stack_id != transaction.agent_core_stack_id
        or not foundation.guardrail_id
        or not foundation.guardrail_version
        or not foundation.guardrail_arn
    ):
        raise ReleaseVerifierV2Error(
            "retained foundation inputs differ from the release journal"
        )
    return foundation


def _verify_image_closure(
    *,
    plan: ReleasePlanV2,
    record: RetainedStepEvidenceV2,
) -> str:
    observer = record.observer_evidence_mapping()
    projection = observer.get("projection")
    expected_profile = (
        f"arn:aws:signer:{plan.region}:{plan.account}:/signing-profiles/"
        "personal_operator_bridge"
    )
    if (
        observer.get("service") != "ecr"
        or observer.get("operation") != "describe_image_scan_findings"
        or observer.get("disposition") != "PRESENT"
        or observer.get("providerStatus") != "COMPLETE"
        or not isinstance(projection, Mapping)
        or set(projection) != _IMAGE_PROJECTION_FIELDS
        or projection.get("repositoryName") != "personal-operator/bridge"
        or projection.get("commitTag") != f"commit-{plan.source_commit}"
        or projection.get("runtimeImageDigest") != plan.runtime_image_digest
        or projection.get("imageUri") != plan.runtime_image_uri
        or projection.get("scanStatus") != "COMPLETE"
        or type(projection.get("criticalFindings")) is not int
        or projection.get("criticalFindings") != 0
        or type(projection.get("highFindings")) is not int
        or projection.get("highFindings") != 0
        or projection.get("signatureStatus") != "SIGNED"
        or projection.get("signingProfileArn") != expected_profile
    ):
        raise ReleaseVerifierV2Error(
            "retained image scan or signature closure is not exact"
        )
    for field in ("sbomManifestDigest", "provenanceManifestDigest"):
        value = projection.get(field)
        if (
            not isinstance(value, str)
            or not value.startswith("sha256:")
            or _SHA256.fullmatch(value.removeprefix("sha256:")) is None
        ):
            raise ReleaseVerifierV2Error(
                "retained image referrer digest is invalid"
            )
    if projection["sbomManifestDigest"] == projection["provenanceManifestDigest"]:
        raise ReleaseVerifierV2Error(
            "retained image referrer identities alias"
        )
    return record.digest()


def _endpoint_projection(
    *,
    plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
    record: RetainedStepEvidenceV2,
    foundation: FoundationRuntimeInputsV1,
) -> tuple[dict[str, Any], RuntimeConfigurationV1]:
    observer = record.observer_evidence_mapping()
    projection = observer.get("projection")
    if (
        observer.get("service") != "bedrock-agentcore-control"
        or observer.get("operation") != "get_agent_runtime_endpoint"
        or observer.get("disposition") != "PRESENT"
        or observer.get("providerStatus") != "READY"
        or not isinstance(projection, Mapping)
        or set(projection) != _ENDPOINT_PROJECTION_FIELDS
        or projection.get("agentCoreStackId") != transaction.agent_core_stack_id
        or projection.get("runtimeId") != transaction.runtime_id
        or projection.get("runtimeVersion") != transaction.runtime_version
        or projection.get("runtimeArn") != transaction.runtime_arn
        or projection.get("endpointId") != transaction.runtime_endpoint_id
        or projection.get("endpointName") != plan.runtime_endpoint_name
        or projection.get("requiresMMDSV2") is not True
        or projection.get("requiresServiceS3Endpoint") is not False
        or projection.get("guardrailId") != foundation.guardrail_id
        or projection.get("guardrailVersion") != foundation.guardrail_version
    ):
        raise ReleaseVerifierV2Error(
            "retained AgentCore endpoint evidence is not exact"
        )
    endpoint_arn = projection.get("endpointArn")
    endpoint_arn_match = (
        _PROVIDER_ENDPOINT_ARN.fullmatch(endpoint_arn)
        if isinstance(endpoint_arn, str)
        else None
    )
    workload_identity_arn = projection.get("workloadIdentityArn")
    workload_identity_match = (
        _WORKLOAD_IDENTITY_ARN.fullmatch(workload_identity_arn)
        if isinstance(workload_identity_arn, str)
        else None
    )
    if (
        endpoint_arn_match is None
        or endpoint_arn_match.group(1) != plan.account
        or workload_identity_match is None
        or workload_identity_match.group(1) != plan.account
    ):
        raise ReleaseVerifierV2Error(
            "retained AgentCore endpoint or workload identity ARN crosses the release"
        )
    try:
        configuration = RuntimeConfigurationV1.from_mapping(
            projection["runtimeConfiguration"],
            runtime_image_uri=plan.runtime_image_uri,
            account=plan.account,
            region=plan.region,
        )
    except (ContractError, TypeError, ValueError) as error:
        raise ReleaseVerifierV2Error(
            "retained AgentCore runtime configuration is invalid"
        ) from error
    role = expected_execution_role_arn(plan.account, plan.region)
    if (
        projection.get("runtimeConfigurationSha256")
        != configuration.digest_for_role(role)
        or configuration.subnet_ids != foundation.private_subnet_ids
        or configuration.security_group_ids
        != foundation.runtime_security_group_ids
    ):
        raise ReleaseVerifierV2Error(
            "retained AgentCore configuration differs from foundation"
        )
    return dict(projection), configuration


def _validate_runtime_mapping(
    *,
    raw: object,
    plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
    foundation: FoundationRuntimeInputsV1,
    expected_workload_identity_arn: str,
) -> tuple[dict[str, Any], RuntimeConfigurationV1]:
    if not isinstance(raw, Mapping):
        raise ReleaseVerifierV2Ambiguous(
            "AgentCore runtime response is malformed"
        )
    expected_role = expected_execution_role_arn(plan.account, plan.region)
    expected_workload_match = _WORKLOAD_IDENTITY_ARN.fullmatch(
        expected_workload_identity_arn
    )
    workload = raw.get("workloadIdentityDetails")
    if (
        expected_workload_match is None
        or expected_workload_match.group(1) != plan.account
        or not isinstance(workload, Mapping)
        or set(workload) != {"workloadIdentityArn"}
        or workload.get("workloadIdentityArn")
        != expected_workload_identity_arn
    ):
        raise ReleaseVerifierV2Error(
            "AgentCore workload identity crosses the exact runtime"
        )
    if (
        not _RUNTIME_RESPONSE_REQUIRED_FIELDS <= set(raw)
        or not set(raw) <= (
            _RUNTIME_RESPONSE_REQUIRED_FIELDS
            | _RUNTIME_RESPONSE_OPTIONAL_FIELDS
        )
        or raw.get("status") != "READY"
        or raw.get("agentRuntimeId") != transaction.runtime_id
        or raw.get("agentRuntimeName") != "personal_operator_bridge"
        or raw.get("agentRuntimeVersion") != transaction.runtime_version
        or raw.get("agentRuntimeArn") != transaction.runtime_arn
        or raw.get("roleArn") != expected_role
        or raw.get("description")
        != (
            "Personal Operator immutable bridge runtime at commit "
            f"{plan.source_commit}"
        )
    ):
        raise ReleaseVerifierV2Error(
            "fresh AgentCore runtime identity or status differs"
        )
    if raw.get("failureReason") not in (None, ""):
        raise ReleaseVerifierV2Error(
            "READY AgentCore runtime carries a failure reason"
        )
    network = raw.get("networkConfiguration")
    if not isinstance(network, Mapping) or set(network) != {
        "networkMode",
        "networkModeConfig",
    }:
        raise ReleaseVerifierV2Error(
            "fresh AgentCore runtime network configuration is not exact"
        )
    vpc = network.get("networkModeConfig")
    if (
        not isinstance(vpc, Mapping)
        or not {"securityGroups", "subnets"} <= set(vpc)
        or not set(vpc)
        <= {"securityGroups", "subnets", "requireServiceS3Endpoint"}
    ):
        raise ReleaseVerifierV2Error(
            "fresh AgentCore runtime VPC configuration is not exact"
        )
    service_s3_endpoint = vpc.get("requireServiceS3Endpoint", False)
    if service_s3_endpoint is not False:
        raise ReleaseVerifierV2Error(
            "fresh AgentCore runtime requires the service S3 endpoint"
        )
    normalized_network = dict(network)
    normalized_vpc = dict(vpc)
    normalized_vpc.pop("requireServiceS3Endpoint", None)
    normalized_network["networkModeConfig"] = normalized_vpc
    configuration_mapping = {
        "agentRuntimeArtifact": raw.get("agentRuntimeArtifact"),
        "authorizerConfiguration": raw.get("authorizerConfiguration", {}),
        "environmentVariables": raw.get("environmentVariables"),
        "filesystemConfigurations": raw.get("filesystemConfigurations"),
        "lifecycleConfiguration": raw.get("lifecycleConfiguration"),
        "metadataConfiguration": raw.get("metadataConfiguration"),
        "networkConfiguration": normalized_network,
        "protocolConfiguration": raw.get("protocolConfiguration"),
        "requestHeaderConfiguration": raw.get(
            "requestHeaderConfiguration", {}
        ),
    }
    try:
        configuration = RuntimeConfigurationV1.from_mapping(
            configuration_mapping,
            runtime_image_uri=plan.runtime_image_uri,
            account=plan.account,
            region=plan.region,
        )
    except (ContractError, TypeError, ValueError) as error:
        raise ReleaseVerifierV2Error(
            "fresh AgentCore runtime configuration differs"
        ) from error
    environment = dict(configuration.environment_variables)
    if (
        configuration.subnet_ids != foundation.private_subnet_ids
        or configuration.security_group_ids
        != foundation.runtime_security_group_ids
        or environment.get("S3_USER_FILES_BUCKET")
        != foundation.user_files_bucket_name
        or environment.get("CAPABILITY_GATEWAY_FUNCTION_ARN")
        != foundation.capability_gateway_function_arn
        or environment.get("WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME")
        != foundation.workspace_broker_function_name
        or environment.get("BEDROCK_GUARDRAIL_ID")
        != foundation.guardrail_id
        or environment.get("BEDROCK_GUARDRAIL_VERSION")
        != foundation.guardrail_version
    ):
        raise ReleaseVerifierV2Error(
            "fresh AgentCore runtime crosses foundation inputs"
        )
    projection = {
        "runtimeId": transaction.runtime_id,
        "runtimeVersion": transaction.runtime_version,
        "runtimeArn": transaction.runtime_arn,
        "workloadIdentityArn": expected_workload_identity_arn,
        "runtimeConfiguration": configuration.to_mapping(),
        "runtimeConfigurationSha256": configuration.digest_for_role(
            expected_role
        ),
        "guardrailId": foundation.guardrail_id,
        "guardrailVersion": foundation.guardrail_version,
        "requiresMMDSV2": True,
        "requiresServiceS3Endpoint": service_s3_endpoint,
    }
    return projection, configuration


def _command_deny_policy_bytes(*, raw: object, resource_arn: str) -> bytes:
    if not isinstance(raw, Mapping) or set(raw) != {"policy"}:
        raise ReleaseVerifierV2Ambiguous(
            "AgentCore command-deny policy response is malformed"
        )
    encoded = raw.get("policy")
    if not isinstance(encoded, str) or not encoded or "\x00" in encoded:
        raise ReleaseVerifierV2Ambiguous(
            "AgentCore command-deny policy is malformed"
        )

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate policy key")
            result[key] = value
        return result

    try:
        policy = json.loads(
            encoded,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite policy value")
            ),
        )
        policy_bytes = canonical_json_bytes(policy)
    except (ContractError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ReleaseVerifierV2Ambiguous(
            "AgentCore command-deny policy is malformed"
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
    if policy_bytes != canonical_json_bytes(expected):
        raise ReleaseVerifierV2Error(
            "AgentCore command-deny policy differs"
        )
    return encoded.encode("utf-8")


def _validate_endpoint_mapping(
    *,
    raw: object,
    plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
    expected_endpoint_arn: str,
) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise ReleaseVerifierV2Ambiguous(
            "AgentCore endpoint response is malformed"
        )
    endpoint_arn_match = _PROVIDER_ENDPOINT_ARN.fullmatch(
        expected_endpoint_arn
    )
    if endpoint_arn_match is None or endpoint_arn_match.group(1) != plan.account:
        raise ReleaseVerifierV2Error(
            "expected AgentCore provider endpoint ARN crosses the release"
        )
    required_fields = {
        "id",
        "name",
        "agentRuntimeEndpointArn",
        "agentRuntimeArn",
        "liveVersion",
        "targetVersion",
        "status",
    }
    allowed_fields = required_fields | {
        "createdAt",
        "lastUpdatedAt",
        "description",
        "failureReason",
    }
    if (
        not required_fields <= set(raw)
        or not set(raw) <= allowed_fields
        or raw.get("failureReason") not in (None, "")
        or raw.get("status") != "READY"
        or raw.get("id") != transaction.runtime_endpoint_id
        or raw.get("name") != plan.runtime_endpoint_name
        or raw.get("agentRuntimeEndpointArn") != expected_endpoint_arn
        or raw.get("agentRuntimeArn") != transaction.runtime_arn
        or raw.get("liveVersion") != transaction.runtime_version
        or raw.get("targetVersion") != transaction.runtime_version
    ):
        raise ReleaseVerifierV2Error(
            "fresh AgentCore endpoint identity or status differs"
        )
    return {
        "endpointId": transaction.runtime_endpoint_id,
        "endpointName": plan.runtime_endpoint_name,
        "endpointArn": expected_endpoint_arn,
    }


def _endpoint_boundary_transaction(
    *,
    plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
    records: Sequence[RetainedStepEvidenceV2],
) -> tuple[StagingTransactionV2, tuple[RetainedStepEvidenceV2, ...]]:
    endpoint_end = max(
        step.ordinal for step in plan.steps if step.phase == "endpoint"
    ) + 1
    endpoint_records = tuple(records[:endpoint_end])
    if not endpoint_records:
        raise ReleaseVerifierV2Error("release has no retained endpoint prefix")
    boundary = replace(
        transaction,
        state="ENDPOINT_READY",
        last_stable_state="ENDPOINT_READY",
        completed_step_count=endpoint_end,
        completed_steps=transaction.completed_steps[:endpoint_end],
        runtime_context_sha256="",
        router_target_stack_id="",
        router_change_set_id="",
        cron_target_stack_id="",
        cron_change_set_id="",
        router_cron_changesets_sha256="",
        router_cron_application_sha256="",
        scheduler_target_stack_id="",
        scheduler_change_set_id="",
        scheduler_changeset_sha256="",
        scheduler_application_sha256="",
        web_target_stack_id="",
        web_change_set_id="",
        web_changeset_sha256="",
        web_application_sha256="",
        verification_sha256="",
        revision=endpoint_records[-1].journal_revision + 1,
    )
    try:
        boundary = StagingTransactionV2.from_bytes(
            boundary.to_bytes(), plan=plan
        )
    except (ContractError, TypeError, ValueError) as error:
        raise ReleaseVerifierV2Error(
            "retained endpoint prefix cannot reconstruct its exact boundary"
        ) from error
    return boundary, endpoint_records


class ReleaseVerifierV2:
    """Perform the sole trusted, network-injected final verification read."""

    def __init__(
        self,
        *,
        runtime_iam_observer: RuntimeIamObserverV2,
        runtime_iam_request: RuntimeIamObservationRequestV1,
        agentcore: AttestedAwsClientV2,
        runtime_context_file: RuntimeContextFileV2,
    ) -> None:
        if not isinstance(runtime_iam_observer, RuntimeIamObserverV2):
            raise ReleaseVerifierV2Error(
                "release verifier requires the exact runtime IAM observer"
            )
        if not isinstance(runtime_iam_request, RuntimeIamObservationRequestV1):
            raise ReleaseVerifierV2Error(
                "release verifier requires a typed runtime IAM request"
            )
        try:
            request = RuntimeIamObservationRequestV1.from_bytes(
                runtime_iam_request.to_bytes()
            )
        except (
            AttributeError,
            RuntimeIamObserverV2Error,
            TypeError,
            ValueError,
        ) as error:
            raise ReleaseVerifierV2Error(
                "release verifier runtime IAM request is not canonical"
            ) from error
        if not isinstance(agentcore, AttestedAwsClientV2):
            raise ReleaseVerifierV2Error(
                "release verifier requires an attested AgentCore client"
            )
        try:
            agentcore.require_scope(
                service="bedrock-agentcore-control",
                account=request.account,
                region=request.region,
                capability="observer",
            )
        except AwsAuthorityError as error:
            raise ReleaseVerifierV2Error(
                "release verifier AgentCore client crosses its authority"
            ) from error
        if not isinstance(runtime_context_file, RuntimeContextFileV2):
            raise ReleaseVerifierV2Error(
                "release verifier requires the exact runtime context reader"
            )
        self._runtime_iam_observer = runtime_iam_observer
        self._runtime_iam_request = request
        self._agentcore = agentcore
        self._runtime_context_file = runtime_context_file

    def _observe_agentcore(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        foundation: FoundationRuntimeInputsV1,
        endpoint_provider_arn: str,
        workload_identity_arn: str,
    ) -> tuple[dict[str, Any], RuntimeConfigurationV1, dict[str, str]]:
        arguments = {
            "agentRuntimeId": transaction.runtime_id,
            "agentRuntimeVersion": transaction.runtime_version,
        }
        endpoint_arguments = {
            "agentRuntimeId": transaction.runtime_id,
            "endpointName": plan.runtime_endpoint_name,
        }
        runtime_resource_arn = (
            f"arn:aws:bedrock-agentcore:{plan.region}:{plan.account}:runtime/"
            f"{transaction.runtime_id}"
        )
        endpoint_resource_arn = (
            f"{runtime_resource_arn}/runtime-endpoint/"
            f"{transaction.runtime_endpoint_id}"
        )
        try:
            runtime_first = self._agentcore.invoke(
                "get_agent_runtime", **arguments
            )
            endpoint_first = self._agentcore.invoke(
                "get_agent_runtime_endpoint", **endpoint_arguments
            )
            runtime_policy_first = self._agentcore.invoke(
                "get_resource_policy", resourceArn=runtime_resource_arn
            )
            endpoint_policy_first = self._agentcore.invoke(
                "get_resource_policy", resourceArn=endpoint_resource_arn
            )
            runtime_second = self._agentcore.invoke(
                "get_agent_runtime", **arguments
            )
            endpoint_second = self._agentcore.invoke(
                "get_agent_runtime_endpoint", **endpoint_arguments
            )
            runtime_policy_second = self._agentcore.invoke(
                "get_resource_policy", resourceArn=runtime_resource_arn
            )
            endpoint_policy_second = self._agentcore.invoke(
                "get_resource_policy", resourceArn=endpoint_resource_arn
            )
        except Exception as error:
            raise ReleaseVerifierV2Ambiguous(
                "fresh AgentCore verification read failed"
            ) from error
        first_runtime, first_configuration = _validate_runtime_mapping(
            raw=runtime_first,
            plan=plan,
            transaction=transaction,
            foundation=foundation,
            expected_workload_identity_arn=workload_identity_arn,
        )
        second_runtime, second_configuration = _validate_runtime_mapping(
            raw=runtime_second,
            plan=plan,
            transaction=transaction,
            foundation=foundation,
            expected_workload_identity_arn=workload_identity_arn,
        )
        first_endpoint = _validate_endpoint_mapping(
            raw=endpoint_first,
            plan=plan,
            transaction=transaction,
            expected_endpoint_arn=endpoint_provider_arn,
        )
        second_endpoint = _validate_endpoint_mapping(
            raw=endpoint_second,
            plan=plan,
            transaction=transaction,
            expected_endpoint_arn=endpoint_provider_arn,
        )
        runtime_policy_first_bytes = _command_deny_policy_bytes(
            raw=runtime_policy_first, resource_arn=runtime_resource_arn
        )
        endpoint_policy_first_bytes = _command_deny_policy_bytes(
            raw=endpoint_policy_first, resource_arn=endpoint_resource_arn
        )
        runtime_policy_second_bytes = _command_deny_policy_bytes(
            raw=runtime_policy_second, resource_arn=runtime_resource_arn
        )
        endpoint_policy_second_bytes = _command_deny_policy_bytes(
            raw=endpoint_policy_second, resource_arn=endpoint_resource_arn
        )
        if (
            canonical_json_bytes(first_runtime)
            != canonical_json_bytes(second_runtime)
            or first_configuration != second_configuration
            or first_endpoint != second_endpoint
            or runtime_policy_first_bytes != runtime_policy_second_bytes
            or endpoint_policy_first_bytes != endpoint_policy_second_bytes
        ):
            raise ReleaseVerifierV2Ambiguous(
                "AgentCore runtime or endpoint changed between exact reads"
            )
        return first_runtime, first_configuration, first_endpoint

    def verify(
        self,
        *,
        plan: ReleasePlanV2,
        transaction: StagingTransactionV2,
        journal_path: Path,
        journal_execution_id: str,
        evidence_store: ReleaseEvidenceStoreV2,
    ) -> ReleaseVerificationObservationV2:
        try:
            canonical_plan = _canonical_release_plan_v2(plan)
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise ReleaseVerifierV2Error(
                "release verification plan is not canonical"
            ) from error
        current = _canonical_transaction(transaction, canonical_plan)
        verify_step = _require_preverify_boundary(canonical_plan, current)
        if not isinstance(evidence_store, ReleaseEvidenceStoreV2):
            raise ReleaseVerifierV2Error(
                "release verification requires the exact evidence store"
            )
        if (
            not isinstance(journal_execution_id, str)
            or _SHA256.fullmatch(journal_execution_id) is None
        ):
            raise ReleaseVerifierV2Error(
                "release verification journal execution is invalid"
            )
        exact_path = Path(journal_path)
        try:
            retained = evidence_store.retained_prefix_for_execution(
                plan=canonical_plan,
                transaction=current,
                journal_path=exact_path,
                journal_execution_id=journal_execution_id,
            )
        except (EvidenceStoreV2Error, OSError, TypeError, ValueError) as error:
            raise ReleaseVerifierV2Error(
                "release evidence prefix audit failed"
            ) from error
        records = _canonical_prefix(
            plan=canonical_plan,
            transaction=current,
            records=retained,
            store=evidence_store,
            journal_path=exact_path,
            journal_execution_id=journal_execution_id,
        )
        foundation = _foundation_inputs(canonical_plan, current, records)

        _image_step, image_record = _single_record(
            canonical_plan, records, phase="image", kind="IMAGE_OBSERVE"
        )
        image_observation_sha256 = _verify_image_closure(
            plan=canonical_plan, record=image_record
        )

        endpoint_step, endpoint_record = _single_record(
            canonical_plan, records, phase="endpoint", kind="STACK_UPDATE"
        )
        retained_endpoint, retained_configuration = _endpoint_projection(
            plan=canonical_plan,
            transaction=current,
            record=endpoint_record,
            foundation=foundation,
        )

        iam_request = self._runtime_iam_request
        if (
            iam_request.account != canonical_plan.account
            or iam_request.region != canonical_plan.region
            or iam_request.source_commit != canonical_plan.source_commit
            or iam_request.source_tree != canonical_plan.source_tree
            or iam_request.stack_id != current.agent_core_stack_id
            or iam_request.foundation_runtime_inputs != foundation
            or iam_request.foundation_inputs_sha256
            != current.foundation_inputs_sha256
            or iam_request.reviewed_template_sha256
            != endpoint_step.expected_template_sha256
            or iam_request.reviewed_template_sha256
            != retained_endpoint["cloudFormationTemplateSha256"]
        ):
            raise ReleaseVerifierV2Error(
                "runtime IAM observation request crosses retained endpoint evidence"
            )
        try:
            iam_observation = self._runtime_iam_observer.observe(iam_request)
        except RuntimeIamObserverV2Error as error:
            raise ReleaseVerifierV2Error(
                "fresh runtime IAM observation failed"
            ) from error
        if (
            iam_observation.disposition is not ObservationDisposition.PRESENT
            or iam_observation.provider_status != "EXACT_RUNTIME_ROLE"
            or iam_observation.subject
            != expected_execution_role_arn(
                canonical_plan.account, canonical_plan.region
            )
            or iam_observation.request_sha256 != iam_request.digest()
        ):
            raise ReleaseVerifierV2Error(
                "runtime IAM carries missing or excess authority"
            )

        live_runtime, live_configuration, live_endpoint = (
            self._observe_agentcore(
                plan=canonical_plan,
                transaction=current,
                foundation=foundation,
                endpoint_provider_arn=retained_endpoint["endpointArn"],
                workload_identity_arn=retained_endpoint[
                    "workloadIdentityArn"
                ],
            )
        )
        if (
            live_configuration != retained_configuration
            or live_runtime["runtimeConfigurationSha256"]
            != retained_endpoint["runtimeConfigurationSha256"]
            or live_runtime["runtimeConfiguration"]
            != retained_endpoint["runtimeConfiguration"]
            or live_runtime["guardrailId"]
            != retained_endpoint["guardrailId"]
            or live_runtime["guardrailVersion"]
            != retained_endpoint["guardrailVersion"]
            or live_runtime["workloadIdentityArn"]
            != retained_endpoint["workloadIdentityArn"]
            or live_endpoint["endpointId"] != retained_endpoint["endpointId"]
            or live_endpoint["endpointName"]
            != retained_endpoint["endpointName"]
            or live_endpoint["endpointArn"]
            != retained_endpoint["endpointArn"]
        ):
            raise ReleaseVerifierV2Error(
                "fresh AgentCore state differs from retained endpoint evidence"
            )

        endpoint_boundary, endpoint_prefix = _endpoint_boundary_transaction(
            plan=canonical_plan,
            transaction=current,
            records=records,
        )
        context_request = RuntimeContextWriteRequestV2.from_plan(canonical_plan)
        try:
            trusted_context = derive_trusted_runtime_context_inputs(
                request=context_request,
                plan=canonical_plan,
                transaction=endpoint_boundary,
                retained_prefix=endpoint_prefix,
            )
            context_observation = self._runtime_context_file.observe(
                request=context_request,
                trusted_inputs=trusted_context,
            )
        except RuntimeContextV2Error as error:
            raise ReleaseVerifierV2Error(
                "fresh runtime context derivation or observation failed"
            ) from error
        context_projection = context_observation.projection()
        if (
            context_observation.disposition
            is not ObservationDisposition.PRESENT
            or context_observation.provider_status != "PRESENT"
            or context_projection.get("expectedRuntimeContextSha256")
            != current.runtime_context_sha256
            or context_projection.get("observedRuntimeContextSha256")
            != current.runtime_context_sha256
            or context_projection.get("endpointEvidenceSha256")
            != endpoint_record.digest()
            or context_projection.get("runtimeEndpointArn")
            != retained_endpoint["endpointArn"]
            or context_projection.get("workloadIdentityArn")
            != retained_endpoint["workloadIdentityArn"]
        ):
            raise ReleaseVerifierV2Error(
                "fresh runtime context differs from the journal"
            )

        completed_prefix_sha256 = _completed_prefix_sha256(
            [item.to_mapping() for item in current.completed_steps]
        )
        retained_prefix_sha256 = hashlib.sha256(
            canonical_json_bytes(
                {
                    "schema": "personal-operator.retained-prefix-audit.v2",
                    "records": [record.digest() for record in records],
                }
            )
        ).hexdigest()
        return ReleaseVerificationObservationV2(
            subject=verify_step.subject,
            projection={
                "planSha256": canonical_plan.digest(),
                "transactionSha256": hashlib.sha256(
                    current.to_bytes()
                ).hexdigest(),
                "completedPrefixSha256": completed_prefix_sha256,
                "retainedPrefixSha256": retained_prefix_sha256,
                "evidenceStoreSha256": evidence_store.identity_sha256,
                "journalPathSha256": _journal_path_sha256(exact_path),
                "journalExecutionId": journal_execution_id,
                "journalRevision": current.revision,
                "completedRecordCount": len(records),
                "foundationInputsSha256": current.foundation_inputs_sha256,
                "runtimeImageDigest": current.runtime_image_digest,
                "imageObservationSha256": image_observation_sha256,
                "runtimeId": current.runtime_id,
                "runtimeVersion": current.runtime_version,
                "runtimeArn": current.runtime_arn,
                "runtimeEndpointId": current.runtime_endpoint_id,
                "runtimeEndpointName": canonical_plan.runtime_endpoint_name,
                "runtimeEndpointArn": live_endpoint["endpointArn"],
                "runtimeWorkloadIdentityArn": live_runtime[
                    "workloadIdentityArn"
                ],
                "runtimeConfigurationSha256": live_runtime[
                    "runtimeConfigurationSha256"
                ],
                "runtimeIamRequestSha256": iam_request.digest(),
                "runtimeIamObservationSha256": iam_observation.digest(),
                "runtimeContextSha256": current.runtime_context_sha256,
                "runtimeContextObservationSha256": (
                    context_observation.digest()
                ),
                "guardrailId": foundation.guardrail_id,
                "guardrailVersion": foundation.guardrail_version,
            },
            _token=_VERIFICATION_OBSERVATION_TOKEN,
        )


__all__ = [
    "ReleaseVerificationObservationV2",
    "ReleaseVerifierV2",
    "ReleaseVerifierV2Ambiguous",
    "ReleaseVerifierV2Error",
]
