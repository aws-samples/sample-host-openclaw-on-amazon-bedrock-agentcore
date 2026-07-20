from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
import hashlib
import inspect
import os
from pathlib import Path
import shutil

import pytest

import release_tools.release_verifier_v2 as verifier_module
from release_tools.contracts import (
    FoundationRuntimeInputsV1,
    ReleasePlanV2,
    canonical_json_bytes,
    parse_canonical_object,
)
from release_tools.evidence_store_v2 import (
    EvidenceStoreV2Error,
    ReleaseEvidenceStoreV2,
)
from release_tools.production_observer_v2 import _new_observation
from release_tools.release_plan_v2 import PreclosedStaticRequestV2
from release_tools.release_verifier_v2 import (
    ReleaseVerificationObservationV2,
    ReleaseVerifierV2,
    ReleaseVerifierV2Ambiguous,
    ReleaseVerifierV2Error,
)
from release_tools.runtime_context_v2 import (
    RuntimeContextFileV2,
    RuntimeContextWriteRequestV2,
    derive_trusted_runtime_context_inputs,
)
from release_tools.runtime_iam_observer_v2 import (
    ROLE_NAME,
    RuntimeIamObservationRequestV1,
    RuntimeIamObserverV2,
    exact_operation_tags,
)
from release_tools.test_aws_authority_v2 import attested_test_client
from release_tools.test_contracts import (
    ACCOUNT,
    COMMIT,
    DIGEST,
    ENDPOINT_ID,
    REGION,
    ROLE_ARN,
    RUNTIME_ARN,
    RUNTIME_ID,
    TREE,
    _runtime_configuration,
)
from release_tools.test_production_observer_v2 import FakeService
from release_tools.test_runtime_iam_observer_v2 import (
    FakeIam,
    _exact_responses,
    _queue_sweeps,
)
from release_tools.test_runtime_context_v2 import _fresh_context_authority
from release_tools.test_transaction import (
    _create_v2,
    _foundation_inputs,
    _observation,
    _resolved_mutation_request,
    _retained_present_evidence,
)
from release_tools.transaction import ObservationDisposition


PROVIDER_ENDPOINT_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:agentEndpoint/"
    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
)
WORKLOAD_IDENTITY_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
    "workload-identity-directory/default/workload-identity/"
    "personal_operator_bridge-managed0001"
)


@pytest.fixture(autouse=True)
def _fast_unit_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)


def _reviewed_runtime_template() -> str:
    trust = {
        "Statement": [
            {
                "Action": "sts:AssumeRole",
                "Condition": {
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:*"
                        )
                    },
                    "StringEquals": {"aws:SourceAccount": ACCOUNT},
                },
                "Effect": "Allow",
                "Principal": {
                    "Service": "bedrock-agentcore.amazonaws.com"
                },
            }
        ],
        "Version": "2012-10-17",
    }
    template = {
        "Resources": {
            "ExecutionRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "AssumeRolePolicyDocument": trust,
                    "MaxSessionDuration": 3600,
                    "Path": "/",
                    "RoleName": ROLE_NAME,
                },
            },
            "ExecutionRolePolicy": {
                "Type": "AWS::IAM::Policy",
                "Properties": {
                    "PolicyDocument": {
                        "Statement": [
                            {
                                "Action": "bedrock:ApplyGuardrail",
                                "Effect": "Allow",
                                "Resource": {
                                    "Fn::ImportValue": (
                                        "OpenClawGuardrails:"
                                        "ExportsOutputFnGetAttContentGuardrail"
                                        "GuardrailArnB39948C5"
                                    )
                                },
                            }
                        ],
                        "Version": "2012-10-17",
                    },
                    "PolicyName": "personal-operator-runtime",
                    "Roles": [{"Ref": "ExecutionRole"}],
                },
            },
        }
    }
    return canonical_json_bytes(template).decode("utf-8")


def _plan() -> ReleasePlanV2:
    from release_tools.test_transaction import _plan_v2

    value = _plan_v2().to_mapping()
    template_sha256 = hashlib.sha256(
        _reviewed_runtime_template().encode("utf-8")
    ).hexdigest()
    for step in value["steps"]:
        if step["phase"] == "endpoint" and step["kind"] == "STACK_UPDATE":
            step["expectedTemplateSha256"] = template_sha256
        if step["kind"] == "RUNTIME_CONTEXT_WRITE":
            payload = PreclosedStaticRequestV2(
                "RUNTIME_CONTEXT_WRITE",
                COMMIT,
                TREE,
                ACCOUNT,
                REGION,
                step["subject"],
            ).to_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            step["requestSha256"] = digest
            step["expectedRequestSha256"] = digest
            artifact = next(
                item
                for item in value["artifacts"]
                if item["path"] == step["requestArtifact"]
            )
            artifact["size"] = len(payload)
            artifact["sha256"] = digest
    return ReleasePlanV2.from_mapping(value)


def _configuration(
    foundation: FoundationRuntimeInputsV1,
    *,
    guardrail_version: str | None = None,
    require_service_s3_endpoint: bool = False,
) -> dict[str, object]:
    value = _runtime_configuration()
    environment = dict(value["environmentVariables"])
    environment.update(
        {
            "S3_USER_FILES_BUCKET": foundation.user_files_bucket_name,
            "CAPABILITY_GATEWAY_FUNCTION_ARN": (
                foundation.capability_gateway_function_arn
            ),
            "WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME": (
                foundation.workspace_broker_function_name
            ),
            "BEDROCK_GUARDRAIL_ID": foundation.guardrail_id,
            "BEDROCK_GUARDRAIL_VERSION": (
                guardrail_version or foundation.guardrail_version
            ),
        }
    )
    value["environmentVariables"] = environment
    if require_service_s3_endpoint:
        network = deepcopy(value["networkConfiguration"])
        network["networkModeConfig"]["requireServiceS3Endpoint"] = True
        value["networkConfiguration"] = network
    return value


def _configuration_sha256(configuration: dict[str, object]) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "executionRoleArn": ROLE_ARN,
                "runtimeConfiguration": configuration,
            }
        )
    ).hexdigest()


def _endpoint_projection(
    plan: ReleasePlanV2,
    foundation: FoundationRuntimeInputsV1,
    configuration: dict[str, object],
) -> dict[str, object]:
    return {
        "agentCoreStackId": foundation.agent_core_stack_id,
        "cloudFormationTemplateSha256": hashlib.sha256(
            _reviewed_runtime_template().encode("utf-8")
        ).hexdigest(),
        "cloudFormationRequestSha256": "2" * 64,
        "runtimeId": RUNTIME_ID,
        "runtimeVersion": "7",
        "runtimeArn": RUNTIME_ARN,
        "runtimeConfiguration": configuration,
        "runtimeConfigurationSha256": _configuration_sha256(configuration),
        "guardrailId": foundation.guardrail_id,
        "guardrailVersion": foundation.guardrail_version,
        "requiresMMDSV2": True,
        "requiresServiceS3Endpoint": False,
        "endpointId": ENDPOINT_ID,
        "endpointName": plan.runtime_endpoint_name,
        "endpointArn": PROVIDER_ENDPOINT_ARN,
        "workloadIdentityArn": WORKLOAD_IDENTITY_ARN,
    }


def _image_projection(
    plan: ReleasePlanV2,
    *,
    critical: int = 0,
    high: int = 0,
    signature: str = "SIGNED",
) -> dict[str, object]:
    return {
        "repositoryName": "personal-operator/bridge",
        "commitTag": f"commit-{plan.source_commit}",
        "runtimeImageDigest": plan.runtime_image_digest,
        "imageUri": plan.runtime_image_uri,
        "scanStatus": "COMPLETE",
        "criticalFindings": critical,
        "highFindings": high,
        "sbomManifestDigest": "sha256:" + "d" * 64,
        "provenanceManifestDigest": "sha256:" + "e" * 64,
        "signingProfileArn": (
            f"arn:aws:signer:{REGION}:{ACCOUNT}:/signing-profiles/"
            "personal_operator_bridge"
        ),
        "signatureStatus": signature,
    }


class _ReleaseHarness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        critical: int = 0,
        high: int = 0,
        signature: str = "SIGNED",
    ) -> None:
        self.plan = _plan()
        self.journal = _create_v2(tmp_path / "journal", self.plan)
        runtime_root = tmp_path / "runtime-root"
        runtime_root.mkdir()
        runtime_root.chmod(0o700)
        self.runtime_root = runtime_root
        self.context_file = RuntimeContextFileV2(runtime_root)
        self.foundation = _foundation_inputs(self.plan)
        self.configuration = _configuration(self.foundation)
        self.journal.advance_preflight()
        while True:
            step = self.plan.steps[self.journal.current.completed_step_count]
            if step.kind == "VERIFY":
                break
            if step.kind == "IMAGE_OBSERVE":
                provider = _new_observation(
                    service="ecr",
                    operation="describe_image_scan_findings",
                    subject=step.subject,
                    disposition=ObservationDisposition.PRESENT,
                    provider_status="COMPLETE",
                    projection=_image_projection(
                        self.plan,
                        critical=critical,
                        high=high,
                        signature=signature,
                    ),
                )
                outcome = self.journal.outcome_composer().compose(
                    transaction=self.journal.current,
                    provider_observation=provider,
                )
                self.journal.complete_observation(outcome=outcome)
                continue
            if step.phase == "endpoint" and step.kind == "STACK_UPDATE":
                self.journal.begin_step()
                provider = _new_observation(
                    service="bedrock-agentcore-control",
                    operation="get_agent_runtime_endpoint",
                    subject=step.subject,
                    disposition=ObservationDisposition.PRESENT,
                    provider_status="READY",
                    projection=_endpoint_projection(
                        self.plan, self.foundation, self.configuration
                    ),
                )
                outcome = self.journal.outcome_composer().compose(
                    transaction=self.journal.current,
                    provider_observation=provider,
                )
                self.journal.reconcile_step(outcome=outcome)
                continue
            if step.kind == "RUNTIME_CONTEXT_WRITE":
                prefix = (
                    self.journal.evidence_store.retained_prefix_for_execution(
                        plan=self.plan,
                        transaction=self.journal.current,
                        journal_path=self.journal.path,
                        journal_execution_id=(
                            self.journal.journal_execution_id
                        ),
                    )
                )
                request = RuntimeContextWriteRequestV2.from_plan(self.plan)
                trusted = derive_trusted_runtime_context_inputs(
                    request=request,
                    plan=self.plan,
                    transaction=self.journal.current,
                    retained_prefix=prefix,
                )
                self.journal.begin_step()
                resolved = _resolved_mutation_request(
                    self.journal,
                    request_artifact_size=len(request.to_bytes()),
                )
                self.context_file.write(
                    request=request,
                    trusted_inputs=trusted,
                    resolved_request=resolved,
                    fresh_authority=_fresh_context_authority(resolved),
                )
                provider = self.context_file.observe(
                    request=request,
                    trusted_inputs=trusted,
                )
                outcome = self.journal.outcome_composer().compose(
                    transaction=self.journal.current,
                    provider_observation=provider,
                )
                self.journal.reconcile_step(outcome=outcome)
                continue
            observation = _observation(self.journal)
            if step.mutation:
                self.journal.begin_step()
                self.journal.reconcile_step(
                    outcome=_retained_present_evidence(
                        self.journal, observation
                    )
                )
            else:
                self.journal.complete_observation(
                    outcome=_retained_present_evidence(
                        self.journal, observation
                    )
                )

    def iam_request(
        self, *, body: str | None = None
    ) -> RuntimeIamObservationRequestV1:
        tags = exact_operation_tags(
            source_commit=self.plan.source_commit,
            source_tree=self.plan.source_tree,
        )
        body = body or _reviewed_runtime_template()
        return RuntimeIamObservationRequestV1.from_mapping(
            {
                "schema": RuntimeIamObservationRequestV1.SCHEMA,
                "account": self.plan.account,
                "region": self.plan.region,
                "sourceCommit": self.plan.source_commit,
                "sourceTree": self.plan.source_tree,
                "stackId": self.foundation.agent_core_stack_id,
                "logicalRoleId": "ExecutionRole",
                "reviewedTemplateBody": body,
                "reviewedTemplateSha256": hashlib.sha256(
                    body.encode("utf-8")
                ).hexdigest(),
                "foundationRuntimeInputs": self.foundation.to_mapping(),
                "foundationInputsSha256": self.foundation.digest(),
                "operationTagsSha256": hashlib.sha256(
                    canonical_json_bytes({"tags": tags})
                ).hexdigest(),
            }
        )

    def runtime_response(
        self,
        *,
        configuration: dict[str, object] | None = None,
    ) -> dict[str, object]:
        value = configuration or self.configuration
        return {
            "agentRuntimeId": RUNTIME_ID,
            "agentRuntimeName": "personal_operator_bridge",
            "agentRuntimeVersion": "7",
            "agentRuntimeArn": RUNTIME_ARN,
            "roleArn": ROLE_ARN,
            "description": (
                "Personal Operator immutable bridge runtime at commit "
                f"{self.plan.source_commit}"
            ),
            "status": "READY",
            "workloadIdentityDetails": {
                "workloadIdentityArn": WORKLOAD_IDENTITY_ARN
            },
            **deepcopy(value),
        }

    def endpoint_response(self, *, endpoint_id: str = ENDPOINT_ID) -> dict[str, str]:
        return {
            "id": endpoint_id,
            "name": self.plan.runtime_endpoint_name,
            "agentRuntimeEndpointArn": (
                PROVIDER_ENDPOINT_ARN
                if endpoint_id == ENDPOINT_ID
                else (
                    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
                    "agentEndpoint/ffffffff-1111-2222-3333-444444444444"
                )
            ),
            "agentRuntimeArn": RUNTIME_ARN,
            "liveVersion": "7",
            "targetVersion": "7",
            "status": "READY",
        }

    def command_deny_policy(self, resource_arn: str) -> dict[str, str]:
        return {
            "policy": canonical_json_bytes(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "DenyRuntimeCommandExecution",
                            "Effect": "Deny",
                            "Principal": "*",
                            "Action": [
                                "bedrock-agentcore:InvokeAgentRuntimeCommand",
                                (
                                    "bedrock-agentcore:"
                                    "InvokeAgentRuntimeCommandShell"
                                ),
                            ],
                            "Resource": resource_arn,
                        }
                    ],
                }
            ).decode("utf-8")
        }

    def verifier(
        self,
        stack: ExitStack,
        *,
        iam_overrides: dict[str, object] | None = None,
        runtime_reads: tuple[dict[str, object], dict[str, object]] | None = None,
        endpoint_reads: tuple[dict[str, str], dict[str, str]] | None = None,
        policy_reads: tuple[
            dict[str, str], dict[str, str], dict[str, str], dict[str, str]
        ]
        | None = None,
        iam_request: RuntimeIamObservationRequestV1 | None = None,
        runtime_context_file: RuntimeContextFileV2 | None = None,
    ) -> ReleaseVerifierV2:
        request = iam_request or self.iam_request()
        fake_iam = FakeIam()
        if iam_overrides:
            first = {**_exact_responses(request), **iam_overrides}
            _queue_sweeps(fake_iam, request, first=first, second=first)
        else:
            _queue_sweeps(fake_iam, request)
        iam_client = stack.enter_context(
            attested_test_client(fake_iam, service="iam")
        )
        iam_observer = RuntimeIamObserverV2(
            account=self.plan.account,
            region=self.plan.region,
            iam=iam_client,
        )
        agentcore = FakeService("bedrock-agentcore-control")
        runtimes = runtime_reads or (
            self.runtime_response(),
            self.runtime_response(),
        )
        endpoints = endpoint_reads or (
            self.endpoint_response(),
            self.endpoint_response(),
        )
        agentcore.queue(
            "get_agent_runtime", runtimes[0], runtimes[1]
        )
        agentcore.queue(
            "get_agent_runtime_endpoint", endpoints[0], endpoints[1]
        )
        runtime_resource_arn = (
            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/"
            f"{RUNTIME_ID}"
        )
        endpoint_resource_arn = (
            f"{runtime_resource_arn}/runtime-endpoint/{ENDPOINT_ID}"
        )
        policies = policy_reads or (
            self.command_deny_policy(runtime_resource_arn),
            self.command_deny_policy(endpoint_resource_arn),
            self.command_deny_policy(runtime_resource_arn),
            self.command_deny_policy(endpoint_resource_arn),
        )
        agentcore.queue("get_resource_policy", *policies)
        agentcore_client = stack.enter_context(
            attested_test_client(
                agentcore, service="bedrock-agentcore-control"
            )
        )
        return ReleaseVerifierV2(
            runtime_iam_observer=iam_observer,
            runtime_iam_request=request,
            agentcore=agentcore_client,
            runtime_context_file=runtime_context_file or self.context_file,
        )

    def verify(self, verifier: ReleaseVerifierV2):
        return verifier.verify(
            plan=self.plan,
            transaction=self.journal.current,
            journal_path=self.journal.path,
            journal_execution_id=self.journal.journal_execution_id,
            evidence_store=self.journal.evidence_store,
        )


@pytest.fixture(scope="module")
def release_harness(tmp_path_factory: pytest.TempPathFactory) -> _ReleaseHarness:
    original_fsync = os.fsync
    os.fsync = lambda _descriptor: None
    try:
        return _ReleaseHarness(tmp_path_factory.mktemp("release-verifier-v2"))
    finally:
        os.fsync = original_fsync


def test_release_verifier_types_are_explicit() -> None:
    assert ReleaseVerificationObservationV2.SCHEMA == (
        "personal-operator.canonical-read-observation.v2"
    )
    assert ReleaseVerifierV2 is not None
    assert issubclass(ReleaseVerifierV2Error, RuntimeError)


def test_verifier_observation_is_private_and_binds_every_fresh_read(
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness
    with ExitStack() as stack:
        observed = harness.verify(harness.verifier(stack))

    assert observed.service == "local-release-verifier"
    assert observed.operation == "verify_release"
    assert observed.disposition is ObservationDisposition.PRESENT
    assert observed.provider_status == "VERIFIED"
    assert observed.subject == harness.plan.steps[-1].subject
    projection = observed.projection()
    assert set(projection) == {
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
    assert projection["planSha256"] == harness.plan.digest()
    assert projection["journalRevision"] == harness.journal.current.revision
    assert projection["completedRecordCount"] == len(harness.plan.steps) - 1
    assert projection["runtimeImageDigest"] == DIGEST
    assert projection["runtimeId"] == RUNTIME_ID
    assert projection["runtimeEndpointId"] == ENDPOINT_ID
    assert projection["runtimeWorkloadIdentityArn"] == WORKLOAD_IDENTITY_ARN
    assert projection["guardrailId"] == harness.foundation.guardrail_id
    retained = harness.journal.outcome_composer().compose(
        transaction=harness.journal.current,
        provider_observation=observed,
    )
    assert retained.disposition == "PRESENT"
    assert retained.retained_evidence.subject == observed.subject
    with pytest.raises(ReleaseVerifierV2Error, match="not constructible"):
        ReleaseVerificationObservationV2(
            subject=observed.subject,
            projection=projection,
        )


def test_verify_accepts_no_caller_supplied_record_inventory() -> None:
    assert "records" not in inspect.signature(ReleaseVerifierV2.verify).parameters
    assert "retained_prefix" not in inspect.signature(
        ReleaseVerifierV2.verify
    ).parameters


@pytest.mark.parametrize(
    ("critical", "high", "signature"),
    ((1, 0, "SIGNED"), (0, 1, "SIGNED"), (0, 0, "FAILED")),
)
def test_verifier_rejects_scan_or_signature_mismatch(
    release_harness: _ReleaseHarness,
    critical: int,
    high: int,
    signature: str,
) -> None:
    harness = release_harness
    prefix = harness.journal.evidence_store.retained_prefix_for_execution(
        plan=harness.plan,
        transaction=harness.journal.current,
        journal_path=harness.journal.path,
        journal_execution_id=harness.journal.journal_execution_id,
    )
    ordinal = next(
        step.ordinal for step in harness.plan.steps if step.kind == "IMAGE_OBSERVE"
    )
    record = prefix[ordinal]
    observer = record.observer_evidence_mapping()
    observer["projection"] = _image_projection(
        harness.plan,
        critical=critical,
        high=high,
        signature=signature,
    )
    observer_bytes = canonical_json_bytes(observer)
    crossed = replace(
        record,
        observer_evidence_bytes=observer_bytes,
        observer_evidence_sha256=hashlib.sha256(observer_bytes).hexdigest(),
    )
    with pytest.raises(ReleaseVerifierV2Error, match="scan or signature"):
        verifier_module._verify_image_closure(
            plan=harness.plan, record=crossed
        )


def test_verifier_rejects_excess_runtime_iam_authority(
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness
    managed = {
        "AttachedPolicies": [
            {
                "PolicyName": "AdministratorAccess",
                "PolicyArn": "arn:aws:iam::aws:policy/AdministratorAccess",
            }
        ],
        "IsTruncated": False,
    }
    with ExitStack() as stack:
        verifier = harness.verifier(
            stack,
            iam_overrides={"list_attached_role_policies": managed},
        )
        with pytest.raises(ReleaseVerifierV2Error, match="excess authority"):
            harness.verify(verifier)


def test_verifier_rejects_unstable_agentcore_runtime(
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness
    changed = harness.runtime_response()
    changed["agentRuntimeVersion"] = "8"
    with ExitStack() as stack:
        verifier = harness.verifier(
            stack,
            runtime_reads=(harness.runtime_response(), changed),
        )
        with pytest.raises(ReleaseVerifierV2Error, match="runtime identity"):
            harness.verify(verifier)


def test_verifier_rejects_crossed_endpoint(
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness
    crossed = harness.endpoint_response(endpoint_id="Endpoint-KLMNOPQRST")
    with ExitStack() as stack:
        verifier = harness.verifier(
            stack, endpoint_reads=(crossed, crossed)
        )
        with pytest.raises(ReleaseVerifierV2Error, match="endpoint identity"):
            harness.verify(verifier)


def test_verifier_rejects_live_service_s3_endpoint(
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness
    configuration = deepcopy(harness.configuration)
    network = configuration["networkConfiguration"]
    assert isinstance(network, dict)
    vpc = network["networkModeConfig"]
    assert isinstance(vpc, dict)
    vpc["requireServiceS3Endpoint"] = True
    live = harness.runtime_response(configuration=configuration)
    with ExitStack() as stack:
        verifier = harness.verifier(stack, runtime_reads=(live, live))
        with pytest.raises(ReleaseVerifierV2Error, match="service S3"):
            harness.verify(verifier)


def test_verifier_rejects_unreviewed_runtime_security_field(
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness
    live = harness.runtime_response()
    live["serviceEndpointConfiguration"] = {"requireS3Endpoint": False}
    with ExitStack() as stack:
        verifier = harness.verifier(stack, runtime_reads=(live, live))
        with pytest.raises(ReleaseVerifierV2Error, match="identity or status"):
            harness.verify(verifier)


def test_runtime_response_rejects_failure_and_crossed_workload_identity(
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness
    failed = harness.runtime_response()
    failed["failureReason"] = "provider failure on READY"
    with pytest.raises(ReleaseVerifierV2Error, match="failure reason"):
        verifier_module._validate_runtime_mapping(
            raw=failed,
            plan=harness.plan,
            transaction=harness.journal.current,
            foundation=harness.foundation,
            expected_workload_identity_arn=WORKLOAD_IDENTITY_ARN,
        )
    crossed = harness.runtime_response()
    crossed["workloadIdentityDetails"] = {
        "workloadIdentityArn": (
            f"arn:aws:bedrock-agentcore:{REGION}:999999999999:"
            "workload-identity-directory/default/workload-identity/"
            "personal_operator_bridge"
        )
    }
    with pytest.raises(ReleaseVerifierV2Error, match="workload identity"):
        verifier_module._validate_runtime_mapping(
            raw=crossed,
            plan=harness.plan,
            transaction=harness.journal.current,
            foundation=harness.foundation,
            expected_workload_identity_arn=WORKLOAD_IDENTITY_ARN,
        )


def test_runtime_response_requires_exact_retained_workload_identity(
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness
    live = harness.runtime_response()
    projection, _configuration_value = (
        verifier_module._validate_runtime_mapping(
            raw=live,
            plan=harness.plan,
            transaction=harness.journal.current,
            foundation=harness.foundation,
            expected_workload_identity_arn=WORKLOAD_IDENTITY_ARN,
        )
    )
    assert projection["workloadIdentityArn"] == WORKLOAD_IDENTITY_ARN

    missing = harness.runtime_response()
    del missing["workloadIdentityDetails"]
    with pytest.raises(ReleaseVerifierV2Error, match="workload identity"):
        verifier_module._validate_runtime_mapping(
            raw=missing,
            plan=harness.plan,
            transaction=harness.journal.current,
            foundation=harness.foundation,
            expected_workload_identity_arn=WORKLOAD_IDENTITY_ARN,
        )


def test_verifier_rejects_workload_identity_drift_between_sweeps(
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness
    changed = harness.runtime_response()
    changed["workloadIdentityDetails"] = {
        "workloadIdentityArn": (
            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
            "workload-identity-directory/default/workload-identity/"
            "personal_operator_bridge-managed0002"
        )
    }
    with ExitStack() as stack:
        verifier = harness.verifier(
            stack,
            runtime_reads=(harness.runtime_response(), changed),
        )
        with pytest.raises(ReleaseVerifierV2Error, match="workload identity"):
            harness.verify(verifier)


def test_endpoint_response_rejects_failure_and_unreviewed_fields(
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness
    failed = harness.endpoint_response()
    failed["failureReason"] = "provider failure on READY"
    with pytest.raises(ReleaseVerifierV2Error, match="endpoint identity"):
        verifier_module._validate_endpoint_mapping(
            raw=failed,
            plan=harness.plan,
            transaction=harness.journal.current,
            expected_endpoint_arn=PROVIDER_ENDPOINT_ARN,
        )
    widened = harness.endpoint_response()
    widened["routingConfiguration"] = {"target": "unreviewed"}
    with pytest.raises(ReleaseVerifierV2Error, match="endpoint identity"):
        verifier_module._validate_endpoint_mapping(
            raw=widened,
            plan=harness.plan,
            transaction=harness.journal.current,
            expected_endpoint_arn=PROVIDER_ENDPOINT_ARN,
        )


def test_endpoint_provider_arn_is_not_the_command_policy_resource(
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness
    crossed = harness.endpoint_response()
    crossed["agentRuntimeEndpointArn"] = (
        f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/"
        f"{RUNTIME_ID}/runtime-endpoint/{ENDPOINT_ID}"
    )
    with pytest.raises(ReleaseVerifierV2Error, match="endpoint identity"):
        verifier_module._validate_endpoint_mapping(
            raw=crossed,
            plan=harness.plan,
            transaction=harness.journal.current,
            expected_endpoint_arn=PROVIDER_ENDPOINT_ARN,
        )


def test_verifier_rejects_missing_command_deny_policy(
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness
    runtime_arn = (
        f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME_ID}"
    )
    endpoint_arn = f"{runtime_arn}/runtime-endpoint/{ENDPOINT_ID}"
    policies = (
        {},
        harness.command_deny_policy(endpoint_arn),
        harness.command_deny_policy(runtime_arn),
        harness.command_deny_policy(endpoint_arn),
    )
    with ExitStack() as stack:
        verifier = harness.verifier(stack, policy_reads=policies)
        with pytest.raises(ReleaseVerifierV2Ambiguous, match="policy response"):
            harness.verify(verifier)


def test_verifier_rejects_widened_or_wrong_resource_command_policy(
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness
    runtime_arn = (
        f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME_ID}"
    )
    endpoint_arn = f"{runtime_arn}/runtime-endpoint/{ENDPOINT_ID}"
    widened = harness.command_deny_policy("*")
    policies = (
        widened,
        harness.command_deny_policy(endpoint_arn),
        widened,
        harness.command_deny_policy(endpoint_arn),
    )
    with ExitStack() as stack:
        verifier = harness.verifier(stack, policy_reads=policies)
        with pytest.raises(ReleaseVerifierV2Error, match="policy differs"):
            harness.verify(verifier)


def test_verifier_rejects_byte_unstable_command_policy(
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness
    runtime_arn = (
        f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME_ID}"
    )
    endpoint_arn = f"{runtime_arn}/runtime-endpoint/{ENDPOINT_ID}"
    first_runtime = harness.command_deny_policy(runtime_arn)
    second_runtime = {
        "policy": "\n" + first_runtime["policy"] + "\n"
    }
    policies = (
        first_runtime,
        harness.command_deny_policy(endpoint_arn),
        second_runtime,
        harness.command_deny_policy(endpoint_arn),
    )
    with ExitStack() as stack:
        verifier = harness.verifier(stack, policy_reads=policies)
        with pytest.raises(ReleaseVerifierV2Ambiguous, match="changed"):
            harness.verify(verifier)


def test_verifier_rejects_altered_runtime_context(
    tmp_path: Path, release_harness: _ReleaseHarness
) -> None:
    harness = release_harness
    copied_root = tmp_path / "runtime-root"
    shutil.copytree(harness.runtime_root, copied_root)
    copied_root.chmod(0o700)
    context_path = copied_root / harness.plan.context_relative_path
    context_path.chmod(0o600)
    context_path.write_bytes(b'{"altered":true}')
    context_path.chmod(0o444)
    with ExitStack() as stack:
        verifier = harness.verifier(
            stack,
            runtime_context_file=RuntimeContextFileV2(copied_root),
        )
        with pytest.raises(ReleaseVerifierV2Error, match="runtime context"):
            harness.verify(verifier)


def test_verifier_rejects_wrong_preverify_boundary(
    tmp_path: Path,
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness
    earlier = _create_v2(tmp_path / "earlier-journal", harness.plan)
    earlier.advance_preflight()
    with ExitStack() as stack:
        with pytest.raises(ReleaseVerifierV2Error, match="pre-VERIFY"):
            harness.verifier(stack).verify(
                plan=harness.plan,
                transaction=earlier.current,
                journal_path=earlier.path,
                journal_execution_id=earlier.journal_execution_id,
                evidence_store=earlier.evidence_store,
            )


def test_verifier_rejects_cross_journal_execution(
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness
    with ExitStack() as stack:
        with pytest.raises(ReleaseVerifierV2Error, match="prefix audit"):
            harness.verifier(stack).verify(
                plan=harness.plan,
                transaction=harness.journal.current,
                journal_path=harness.journal.path,
                journal_execution_id="9" * 64,
                evidence_store=harness.journal.evidence_store,
            )


def test_verifier_rejects_crossed_plan(
    release_harness: _ReleaseHarness,
) -> None:
    from release_tools.test_transaction import _plan_v2

    harness = release_harness
    with ExitStack() as stack:
        with pytest.raises(ReleaseVerifierV2Error, match="transaction differs"):
            harness.verifier(stack).verify(
                plan=_plan_v2(),
                transaction=harness.journal.current,
                journal_path=harness.journal.path,
                journal_execution_id=harness.journal.journal_execution_id,
                evidence_store=harness.journal.evidence_store,
            )


def test_verifier_rejects_crossed_journal_path(
    tmp_path: Path,
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness
    with ExitStack() as stack:
        with pytest.raises(ReleaseVerifierV2Error, match="prefix audit"):
            harness.verifier(stack).verify(
                plan=harness.plan,
                transaction=harness.journal.current,
                journal_path=tmp_path / "other-journal.json",
                journal_execution_id=harness.journal.journal_execution_id,
                evidence_store=harness.journal.evidence_store,
            )


def test_verifier_rejects_crossed_evidence_store(
    tmp_path: Path,
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness
    with ReleaseEvidenceStoreV2(tmp_path / "other-store") as other_store:
        with ExitStack() as stack:
            with pytest.raises(ReleaseVerifierV2Error, match="prefix audit"):
                harness.verifier(stack).verify(
                    plan=harness.plan,
                    transaction=harness.journal.current,
                    journal_path=harness.journal.path,
                    journal_execution_id=harness.journal.journal_execution_id,
                    evidence_store=other_store,
                )


def test_verifier_fails_closed_when_prefix_audit_reports_deleted_receipt(
    monkeypatch: pytest.MonkeyPatch,
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness

    def missing_receipt(*_args: object, **_kwargs: object) -> object:
        raise EvidenceStoreV2Error(
            "completed provider effect lacks its exact receipt"
        )

    monkeypatch.setattr(
        ReleaseEvidenceStoreV2,
        "retained_prefix_for_execution",
        missing_receipt,
    )
    with ExitStack() as stack:
        with pytest.raises(ReleaseVerifierV2Error, match="prefix audit"):
            harness.verify(harness.verifier(stack))


def test_verifier_rejects_stale_runtime_iam_template(
    release_harness: _ReleaseHarness,
) -> None:
    harness = release_harness
    raw = parse_canonical_object(
        _reviewed_runtime_template().encode("utf-8")
    )
    raw["Metadata"] = {"stale": True}
    stale = harness.iam_request(
        body=canonical_json_bytes(raw).decode("utf-8")
    )
    with ExitStack() as stack:
        verifier = harness.verifier(stack, iam_request=stale)
        with pytest.raises(ReleaseVerifierV2Error, match="crosses retained"):
            harness.verify(verifier)
