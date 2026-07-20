from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path

import pytest

import release_tools.evidence_store_v2 as evidence_store_module
import release_tools.production_observer_v2 as production_observer_module
import release_tools.release_verifier_v2 as release_verifier_module
import release_tools.runtime_context_v2 as runtime_context_module
import release_tools.transaction as transaction_module
from release_tools.agentcore_hardening_v2 import (
    AgentCoreHardeningDispatchReceiptV1,
    AgentCoreHardeningPreconditionV1,
    _reviewed_runtime,
)
from release_tools.contracts import (
    ContractError,
    RetainedStepEvidenceV2,
    StackDriftDispatchReceiptV1,
    canonical_json_bytes,
    parse_canonical_object,
    parse_release_contract,
)
from release_tools.evidence_store_v2 import (
    EvidenceStoreV2Error,
    ReleaseEvidenceStoreV2,
    VerifiedStepOutcomeV2,
)
from release_tools.production_observer_v2 import _new_observation
from release_tools.release_plan_v2 import ReleasePlanAssemblerV2
from release_tools.test_release_plan_v2 import _preclosed_source
from release_tools.test_transaction import (
    _consumer_change_set_id,
    _consumer_stack_id,
    _create_v2,
    _failed_retained_evidence,
    _observation,
    _retained_outcome,
    _retained_present_evidence,
    _resolved_mutation_request,
)
from release_tools.test_agentcore_hardening_v2 import _runtime
from release_tools.transaction import (
    ObservationDisposition,
    TransactionJournalV2,
    TransactionError,
)


def _provider_observation(journal, *, marker: str = "canonical"):
    step = journal.plan.steps[journal.current.completed_step_count]
    return _new_observation(
        service="cloudformation",
        operation="describe_stacks",
        subject=step.subject,
        disposition=ObservationDisposition.PRESENT,
        provider_status="TEST_PRESENT",
        projection={"marker": marker, "stepId": step.step_id},
    )


def _retain(
    store: ReleaseEvidenceStoreV2,
    journal,
    *,
    marker: str = "canonical",
    derived: dict[str, str] | None = None,
):
    if store is not journal.evidence_store:
        raise AssertionError("test outcome must use the journal-bound store")
    observation = _observation(
        journal,
        observer_evidence_sha256=hashlib.sha256(marker.encode()).hexdigest(),
        derived=derived,
    )
    return _retained_present_evidence(journal, observation)


def _advance_to_ordinal(journal, ordinal: int) -> None:
    """Advance with canonical synthetic retained evidence to one exact step."""

    while journal.current.completed_step_count < ordinal:
        step = journal.plan.steps[journal.current.completed_step_count]
        next_phase = (
            journal.plan.steps[step.ordinal + 1].phase
            if step.ordinal + 1 < len(journal.plan.steps)
            else None
        )
        derived = (
            {"runtime_image_digest": journal.plan.runtime_image_digest}
            if step.phase == "image" and next_phase != step.phase
            else None
        )
        if step.mutation:
            journal.begin_step()
            journal.reconcile_step(
                outcome=_retain(
                    journal.evidence_store,
                    journal,
                    marker=f"prefix-{step.ordinal}",
                    derived=derived,
                )
            )
        else:
            journal.complete_observation(
                outcome=_retain(
                    journal.evidence_store,
                    journal,
                    marker=f"prefix-{step.ordinal}",
                    derived=derived,
                )
            )


def _durable_agentcore_precondition(journal, sink):
    step = journal.plan.steps[journal.current.completed_step_count]
    artifact = next(
        item
        for item in journal.plan.artifacts
        if item.path == step.request_artifact
    )
    resolved = _resolved_mutation_request(
        journal, request_artifact_size=artifact.size
    )
    raw = _runtime(
        resolved,
        metadata={"requireMMDSV2": True},
        service_s3_endpoint=False,
    )
    reviewed = _reviewed_runtime(
        raw,
        resolved=resolved,
        expected_version=resolved.runtime_version,
        expected_arn=resolved.runtime_arn,
        require_hardened=False,
    )
    value = AgentCoreHardeningPreconditionV1.from_mapping(
        {
            "schema": AgentCoreHardeningPreconditionV1.SCHEMA,
            "receiptAuthority": sink._backend.authority.to_mapping(),
            "resolvedRequestSha256": resolved.digest(),
            "authoritySha256": "9" * 64,
            "account": journal.plan.account,
            "region": journal.plan.region,
            "runtimeObservationSha256": reviewed.digest(),
            "runtimeObservation": parse_canonical_object(
                reviewed.projection_bytes
            ),
            "mode": "NOOP",
        }
    )
    sink._retain_precondition(value.to_bytes())
    return value, resolved


def _runtime_context_projection(
    journal, *, context_sha256: str = "e" * 64
) -> dict[str, object]:
    endpoint_ordinal = next(
        step.ordinal
        for step in journal.plan.steps
        if step.phase == "endpoint" and step.kind == "STACK_UPDATE"
    )
    return {
        "planSha256": journal.plan.digest(),
        "completedPrefixSha256": journal.completed_prefix_sha256(),
        "sourceCommit": journal.plan.source_commit,
        "sourceTree": journal.plan.source_tree,
        "contextRelativePath": journal.plan.context_relative_path,
        "runtimeImageDigest": journal.current.runtime_image_digest,
        "runtimeEndpointName": journal.plan.runtime_endpoint_name,
        "runtimeEndpointArn": (
            f"arn:aws:bedrock-agentcore:{journal.plan.region}:"
            f"{journal.plan.account}:agentEndpoint/"
            "12345678-1234-4234-8234-123456789abc"
        ),
        "workloadIdentityArn": (
            f"arn:aws:bedrock-agentcore:{journal.plan.region}:"
            f"{journal.plan.account}:workload-identity-directory/default/"
            "workload-identity/personal_operator_bridge"
        ),
        "endpointEvidenceSha256": (
            journal.current.completed_steps[endpoint_ordinal].evidence_sha256
        ),
        "expectedRuntimeContextSha256": context_sha256,
        "observedRuntimeContextSha256": context_sha256,
        "size": 1024,
    }


def _release_verification_projection(journal) -> dict[str, object]:
    records = journal.evidence_store.retained_prefix_for_execution(
        plan=journal.plan,
        transaction=journal.current,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )

    def single(phase: str, kind: str):
        matches = [
            record
            for ordinal, record in enumerate(records)
            if journal.plan.steps[ordinal].phase == phase
            and journal.plan.steps[ordinal].kind == kind
        ]
        assert len(matches) == 1
        return matches[0]

    foundation = [
        record.step_observation.foundation_runtime_inputs
        for record in records
        if record.step_observation is not None
        and record.step_observation.foundation_runtime_inputs is not None
    ]
    assert len(foundation) == 1
    endpoint_arn = (
        f"arn:aws:bedrock-agentcore:{journal.plan.region}:"
        f"{journal.plan.account}:agentEndpoint/"
        "12345678-1234-4234-8234-123456789abc"
    )
    return {
        "planSha256": journal.plan.digest(),
        "transactionSha256": hashlib.sha256(
            journal.current.to_bytes()
        ).hexdigest(),
        "completedPrefixSha256": journal.completed_prefix_sha256(),
        "retainedPrefixSha256": hashlib.sha256(
            canonical_json_bytes(
                {
                    "schema": "personal-operator.retained-prefix-audit.v2",
                    "records": [record.digest() for record in records],
                }
            )
        ).hexdigest(),
        "evidenceStoreSha256": journal.evidence_store.identity_sha256,
        "journalPathSha256": evidence_store_module._journal_path_sha256(
            journal.path
        ),
        "journalExecutionId": journal.journal_execution_id,
        "journalRevision": journal.current.revision,
        "completedRecordCount": len(records),
        "foundationInputsSha256": journal.current.foundation_inputs_sha256,
        "runtimeImageDigest": journal.current.runtime_image_digest,
        "imageObservationSha256": single(
            "image", "IMAGE_OBSERVE"
        ).digest(),
        "runtimeId": journal.current.runtime_id,
        "runtimeVersion": journal.current.runtime_version,
        "runtimeArn": journal.current.runtime_arn,
        "runtimeEndpointId": journal.current.runtime_endpoint_id,
        "runtimeEndpointName": journal.plan.runtime_endpoint_name,
        "runtimeEndpointArn": endpoint_arn,
        "runtimeWorkloadIdentityArn": (
            f"arn:aws:bedrock-agentcore:{journal.plan.region}:"
            f"{journal.plan.account}:workload-identity-directory/default/"
            "workload-identity/personal_operator_bridge"
        ),
        "runtimeConfigurationSha256": "0" * 64,
        "runtimeIamRequestSha256": "1" * 64,
        "runtimeIamObservationSha256": "2" * 64,
        "runtimeContextSha256": journal.current.runtime_context_sha256,
        "runtimeContextObservationSha256": "3" * 64,
        "guardrailId": foundation[0].guardrail_id,
        "guardrailVersion": foundation[0].guardrail_version,
    }


def _stack_name(step) -> str:
    return step.subject.split(":stack:", 1)[1].split(":release:", 1)[0]


def _foundation_outputs(stack_name: str) -> dict[str, str]:
    return {
        "OpenClawGuardrails": {
            "GuardrailId": "guardrail123",
            "GuardrailVersion": "7",
            "GuardrailArn": (
                "arn:aws:bedrock:eu-west-1:123456789012:"
                "guardrail/guardrail123"
            ),
        },
        "PersonalOperatorCapabilities": {
            "CapabilityGatewayFunctionArn": (
                "arn:aws:lambda:eu-west-1:123456789012:function:"
                "personal-operator-capability-gateway"
            )
        },
        "OpenClawAgentCore": {
            "PrivateSubnetIds": (
                "subnet-00000000000000001,subnet-00000000000000002"
            ),
            "SecurityGroupId": "sg-00000000000000001",
            "UserFilesBucketName": (
                "openclaw-user-files-123456789012-eu-west-1"
            ),
            "WorkspaceCredentialBrokerFunctionName": (
                "personal-operator-workspace-credential-broker"
            ),
        },
    }.get(stack_name, {})


def _complete_production_foundation(journal):
    """Run production-shaped provider evidence through the final drift."""

    journal.advance_preflight()
    baseline = journal.plan.steps[0]
    baseline_read = _new_observation(
        service="cloudformation",
        operation="describe_stacks",
        subject=baseline.subject,
        disposition=ObservationDisposition.PRESENT,
        provider_status="CLEAN_ACCOUNT",
        projection={
            "account": journal.plan.account,
            "inventory": [],
            "region": journal.plan.region,
            "requestSha256": "0" * 64,
            "sourceCommit": journal.plan.source_commit,
            "sweeps": 2,
        },
    )
    journal.complete_observation(
        outcome=journal.outcome_composer().compose(
            transaction=journal.current,
            provider_observation=baseline_read,
        )
    )
    final_outcome = None
    while journal.current.completed_step_count < 16:
        journal.begin_step()
        step = journal.plan.steps[journal.current.completed_step_count]
        if step.kind in {"BOOTSTRAP_STACK", "STACK_CREATE"}:
            stack_name = _stack_name(step)
            stack_id = (
                "arn:aws:cloudformation:eu-west-1:123456789012:stack/"
                f"{stack_name}/stack-{step.ordinal}"
            )
            provider = _new_observation(
                service="cloudformation",
                operation="describe_stacks",
                subject=step.subject,
                disposition=ObservationDisposition.PRESENT,
                provider_status="CREATE_COMPLETE",
                projection={
                    "stackId": stack_id,
                    "templateParameterSha256": (
                        step.expected_template_parameter_sha256
                    ),
                    "observedRequestSha256": (
                        step.expected_observed_request_sha256
                    ),
                    "outputs": _foundation_outputs(stack_name),
                },
            )
        elif step.kind == "ASSET_PUBLISH":
            provider = _new_observation(
                service="s3",
                operation="head_object",
                subject=step.subject,
                disposition=ObservationDisposition.PRESENT,
                provider_status="PRESENT",
                projection={
                    "assetId": step.subject.removeprefix("cdk:asset:"),
                    "contentSha256": step.expected_content_sha256,
                },
            )
        else:
            assert step.kind == "STACK_DRIFT_CHECK"
            predecessor_digest = journal.current.completed_steps[-1].evidence_sha256
            predecessor = next(
                record
                for record in journal.evidence_store._all_records(
                    plan_sha256=journal.plan.digest()
                )
                if record.digest() == predecessor_digest
            )
            predecessor_projection = (
                predecessor.observer_evidence_mapping()["projection"]
            )
            stack_id = predecessor_projection["stackId"]
            sink = journal.evidence_store.stack_drift_receipt_sink(
                plan=journal.plan,
                transaction=journal.current,
                journal_path=journal.path,
                journal_execution_id=journal.journal_execution_id,
            )
            receipt = StackDriftDispatchReceiptV1.from_mapping(
                {
                    "schema": StackDriftDispatchReceiptV1.SCHEMA,
                    "releasePlanSha256": journal.plan.digest(),
                    "evidenceStoreSha256": (
                        journal.evidence_store.identity_sha256
                    ),
                    "journalPathSha256": (
                        evidence_store_module._journal_path_sha256(journal.path)
                    ),
                    "journalExecutionId": journal.journal_execution_id,
                    "journalRevision": journal.current.revision,
                    "completedPrefixSha256": journal.completed_prefix_sha256(),
                    "stepId": step.step_id,
                    "subject": step.subject,
                    "releaseOperationSha256": (
                        journal.current.uncertain_operation_sha256
                    ),
                    "stackId": stack_id,
                    "predecessorEvidenceSha256": predecessor.digest(),
                    "predecessorObserverEvidenceSha256": (
                        predecessor.observer_evidence_sha256
                    ),
                    "driftDetectionId": (
                        f"00000000-0000-4000-8000-{step.ordinal:012d}"
                    ),
                }
            )
            assert sink._begin_attempt() is True
            sink._retain(receipt.to_bytes())
            provider = _new_observation(
                service="cloudformation",
                operation="describe_stack_drift_detection_status",
                subject=step.subject,
                disposition=ObservationDisposition.PRESENT,
                provider_status="IN_SYNC",
                projection={
                    "dispatchReceiptSha256": receipt.digest(),
                    "driftDetectionId": receipt.drift_detection_id,
                    "stackId": stack_id,
                    "predecessorEvidenceSha256": predecessor.digest(),
                    "predecessorObserverEvidenceSha256": (
                        predecessor.observer_evidence_sha256
                    ),
                    "resourceDriftCount": 0,
                    "closingStackSha256": "1" * 64,
                    "closingTemplateSha256": "2" * 64,
                    "closingStackPolicySha256": "3" * 64,
                },
            )
        final_outcome = journal.outcome_composer().compose(
            transaction=journal.current,
            provider_observation=provider,
        )
        journal.reconcile_step(outcome=final_outcome)
    return final_outcome


def test_stack_drift_receipt_sink_is_durable_and_restart_fail_closed(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    _advance_to_ordinal(journal, 2)
    journal.begin_step()
    store = journal.evidence_store

    sink = store.stack_drift_receipt_sink(
        plan=journal.plan,
        transaction=journal.current,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )
    assert sink._load() == (False, None)
    assert sink._begin_attempt() is True
    assert sink._load() == (True, None)

    evidence_root = store.root
    plan = journal.plan
    journal_path = journal.path
    execution_id = journal.journal_execution_id
    store.close()
    recovered_store = ReleaseEvidenceStoreV2(evidence_root)
    recovered = TransactionJournalV2.load(
        journal_path,
        plan=plan,
        evidence_store=recovered_store,
    )
    recovered_sink = recovered_store.stack_drift_receipt_sink(
        plan=plan,
        transaction=recovered.current,
        journal_path=journal_path,
        journal_execution_id=execution_id,
    )
    assert recovered_sink._load() == (True, None)
    assert recovered_sink._begin_attempt() is False


def test_agentcore_hardening_receipt_sink_binds_exact_store_path_and_execution(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path / "primary")
    journal.advance_preflight()
    _advance_to_ordinal(journal, 24)
    journal.begin_step()

    sink = journal.evidence_store.agentcore_hardening_receipt_sink(
        plan=journal.plan,
        transaction=journal.current,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )
    assert sink._load() == (False, None)
    binding = sink._backend.binding()
    assert binding == {
        "evidenceStoreSha256": journal.evidence_store.identity_sha256,
        "journalPathSha256": evidence_store_module._journal_path_sha256(
            journal.path
        ),
        "journalExecutionId": journal.journal_execution_id,
    }
    precondition, _ = _durable_agentcore_precondition(journal, sink)
    assert sink._load_precondition() == precondition.to_bytes()
    assert sink._begin_attempt() is True

    crossed = _create_v2(
        tmp_path / "crossed",
        journal.plan,
        evidence_store=journal.evidence_store,
    )
    crossed.advance_preflight()
    _advance_to_ordinal(crossed, 24)
    crossed.begin_step()
    crossed_sink = crossed.evidence_store.agentcore_hardening_receipt_sink(
        plan=crossed.plan,
        transaction=crossed.current,
        journal_path=crossed.path,
        journal_execution_id=crossed.journal_execution_id,
    )
    assert crossed_sink._load() == (False, None)
    assert crossed_sink._load_precondition() is None


def test_agentcore_precondition_only_boundary_survives_store_restart(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    _advance_to_ordinal(journal, 24)
    journal.begin_step()
    sink = journal.evidence_store.agentcore_hardening_receipt_sink(
        plan=journal.plan,
        transaction=journal.current,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )
    precondition, _ = _durable_agentcore_precondition(journal, sink)
    evidence_root = journal.evidence_store.root
    plan = journal.plan
    path = journal.path
    execution = journal.journal_execution_id
    journal.evidence_store.close()

    reopened_store = ReleaseEvidenceStoreV2(evidence_root)
    reopened = TransactionJournalV2.load(
        path, plan=plan, evidence_store=reopened_store
    )
    reopened_sink = reopened_store.agentcore_hardening_receipt_sink(
        plan=plan,
        transaction=reopened.current,
        journal_path=path,
        journal_execution_id=execution,
    )

    assert reopened_sink._load_precondition() == precondition.to_bytes()
    assert reopened_sink._load() == (False, None)
    assert reopened_sink._begin_attempt() is True
    reopened_store.close()


@pytest.mark.parametrize("damage", ["missing", "torn", "crossed", "duplicate"])
def test_agentcore_precondition_inventory_damage_fails_closed(
    tmp_path: Path,
    damage: str,
) -> None:
    journal = _create_v2(tmp_path / damage)
    journal.advance_preflight()
    _advance_to_ordinal(journal, 24)
    journal.begin_step()
    sink = journal.evidence_store.agentcore_hardening_receipt_sink(
        plan=journal.plan,
        transaction=journal.current,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )
    precondition, _ = _durable_agentcore_precondition(journal, sink)
    assert sink._begin_attempt() is True
    plan_root = journal.evidence_store.root / journal.plan.digest()
    target = next(plan_root.glob("receipt-agentcore-hardening-*-precondition.bin"))
    if damage == "missing":
        target.unlink()
    elif damage == "torn":
        target.chmod(0o600)
        target.write_bytes(precondition.to_bytes()[:-1])
        target.chmod(0o400)
    elif damage == "crossed":
        raw = parse_canonical_object(precondition.to_bytes())
        authority = raw["receiptAuthority"]
        assert isinstance(authority, dict)
        authority["journalExecutionId"] = "f" * 64
        target.chmod(0o600)
        target.write_bytes(canonical_json_bytes(raw))
        target.chmod(0o400)
    else:
        duplicate = target.with_name(
            target.name.replace("-precondition.bin", "-copy-precondition.bin")
        )
        duplicate.write_bytes(precondition.to_bytes())
        duplicate.chmod(0o400)

    with pytest.raises(EvidenceStoreV2Error, match="precondition|receipt|inventory"):
        journal.evidence_store.audit_prefix(
            plan=journal.plan,
            transaction=journal.current,
            journal_path=journal.path,
            journal_execution_id=journal.journal_execution_id,
        )


def test_stack_drift_observation_is_transitively_bound_to_durable_receipt(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    journal.complete_observation(
        outcome=_retain(journal.evidence_store, journal, marker="baseline")
    )
    journal.begin_step()
    bootstrap = journal.plan.steps[journal.current.completed_step_count]
    stack_id = (
        "arn:aws:cloudformation:eu-west-1:123456789012:stack/"
        "CDKToolkit/bootstrap-1"
    )
    bootstrap_read = _new_observation(
        service="cloudformation",
        operation="describe_stacks",
        subject=bootstrap.subject,
        disposition=ObservationDisposition.PRESENT,
        provider_status="CREATE_COMPLETE",
        projection={
            "stackId": stack_id,
            "templateParameterSha256": (
                bootstrap.expected_template_parameter_sha256
            ),
            "observedRequestSha256": (
                bootstrap.expected_observed_request_sha256
            ),
        },
    )
    bootstrap_outcome = journal.outcome_composer().compose(
        transaction=journal.current,
        provider_observation=bootstrap_read,
    )
    journal.reconcile_step(outcome=bootstrap_outcome)
    journal.begin_step()
    step = journal.plan.steps[journal.current.completed_step_count]
    predecessor = bootstrap_outcome.retained_evidence
    sink = journal.evidence_store.stack_drift_receipt_sink(
        plan=journal.plan,
        transaction=journal.current,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )
    receipt = StackDriftDispatchReceiptV1.from_mapping(
        {
            "schema": StackDriftDispatchReceiptV1.SCHEMA,
            "releasePlanSha256": journal.plan.digest(),
            "evidenceStoreSha256": journal.evidence_store.identity_sha256,
            "journalPathSha256": evidence_store_module._journal_path_sha256(
                journal.path
            ),
            "journalExecutionId": journal.journal_execution_id,
            "journalRevision": journal.current.revision,
            "completedPrefixSha256": journal.completed_prefix_sha256(),
            "stepId": step.step_id,
            "subject": step.subject,
            "releaseOperationSha256": (
                journal.current.uncertain_operation_sha256
            ),
            "stackId": stack_id,
            "predecessorEvidenceSha256": predecessor.digest(),
            "predecessorObserverEvidenceSha256": (
                predecessor.observer_evidence_sha256
            ),
            "driftDetectionId": "12345678-1234-4234-8234-123456789abc",
        }
    )
    assert sink._begin_attempt() is True
    sink._retain(receipt.to_bytes())
    drift_read = _new_observation(
        service="cloudformation",
        operation="describe_stack_drift_detection_status",
        subject=step.subject,
        disposition=ObservationDisposition.PRESENT,
        provider_status="IN_SYNC",
        projection={
            "dispatchReceiptSha256": receipt.digest(),
            "driftDetectionId": receipt.drift_detection_id,
            "stackId": stack_id,
            "predecessorEvidenceSha256": predecessor.digest(),
            "predecessorObserverEvidenceSha256": (
                predecessor.observer_evidence_sha256
            ),
            "resourceDriftCount": 0,
            "closingStackSha256": "1" * 64,
            "closingTemplateSha256": "2" * 64,
            "closingStackPolicySha256": "3" * 64,
        },
    )

    crossed_projection = drift_read.projection()
    crossed_projection["dispatchReceiptSha256"] = "f" * 64
    crossed_read = _new_observation(
        service="cloudformation",
        operation="describe_stack_drift_detection_status",
        subject=step.subject,
        disposition=ObservationDisposition.PRESENT,
        provider_status="IN_SYNC",
        projection=crossed_projection,
    )
    with pytest.raises(EvidenceStoreV2Error, match="receipt|authority"):
        journal.outcome_composer().compose(
            transaction=journal.current,
            provider_observation=crossed_read,
        )

    outcome = journal.outcome_composer().compose(
        transaction=journal.current,
        provider_observation=drift_read,
    )
    journal.reconcile_step(outcome=outcome)
    journal.evidence_store.audit_prefix(
        plan=journal.plan,
        transaction=journal.current,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )
    with ReleaseEvidenceStoreV2(journal.evidence_store.root) as reopened:
        reopened.audit_prefix(
            plan=journal.plan,
            transaction=journal.current,
            journal_path=journal.path,
            journal_execution_id=journal.journal_execution_id,
        )

    receipt_path = next(
        (journal.evidence_store.root / journal.plan.digest()).glob(
            "receipt-stack-drift-*-receipt.bin"
        )
    )
    receipt_path.chmod(0o600)
    receipt_path.write_bytes(receipt.to_bytes() + b"\n")
    with pytest.raises(
        EvidenceStoreV2Error, match="receipt|mode|inventory|retained"
    ):
        journal.evidence_store.audit_prefix(
            plan=journal.plan,
            transaction=journal.current,
            journal_path=journal.path,
            journal_execution_id=journal.journal_execution_id,
        )


def test_agentcore_hardening_observation_owns_exact_receipt_version(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    _advance_to_ordinal(journal, 24)
    prior_version = journal.current.runtime_version
    journal.begin_step()
    step = journal.plan.steps[journal.current.completed_step_count]
    sink = journal.evidence_store.agentcore_hardening_receipt_sink(
        plan=journal.plan,
        transaction=journal.current,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )
    precondition, resolved = _durable_agentcore_precondition(journal, sink)
    receipt = AgentCoreHardeningDispatchReceiptV1.from_mapping(
        {
            "schema": AgentCoreHardeningDispatchReceiptV1.SCHEMA,
            "releasePlanSha256": journal.plan.digest(),
            "transactionId": journal.plan.transaction_id,
            "sourceCommit": journal.plan.source_commit,
            "sourceTree": journal.plan.source_tree,
            "account": journal.plan.account,
            "region": journal.plan.region,
            "evidenceStoreSha256": journal.evidence_store.identity_sha256,
            "journalPathSha256": evidence_store_module._journal_path_sha256(
                journal.path
            ),
            "journalExecutionId": journal.journal_execution_id,
            "journalRevision": journal.current.revision,
            "completedPrefixSha256": journal.completed_prefix_sha256(),
            "stepId": step.step_id,
            "subject": step.subject,
            "operationSha256": journal.current.uncertain_operation_sha256,
            "resolvedRequestSha256": resolved.digest(),
            "preconditionSha256": precondition.digest(),
            "mode": "NOOP",
            "runtimeId": journal.current.runtime_id,
            "priorRuntimeVersion": prior_version,
            "resultingRuntimeVersion": prior_version,
            "resultingRuntimeArn": journal.current.runtime_arn,
            "updateRequestSha256": "",
            "providerAcknowledgementStatus": "READY",
        }
    )
    assert sink._begin_attempt() is True
    sink._retain(receipt.to_bytes())
    provider = _new_observation(
        service="bedrock-agentcore-control",
        operation="get_agent_runtime",
        subject=step.subject,
        disposition=ObservationDisposition.PRESENT,
        provider_status="READY",
        projection={
            "agentCoreStackId": journal.current.agent_core_stack_id,
            "runtimeId": receipt.runtime_id,
            "runtimeVersion": receipt.resulting_runtime_version,
            "runtimeArn": receipt.resulting_runtime_arn,
            "runtimeConfigurationSha256": "6" * 64,
            "hardeningReceiptSha256": receipt.digest(),
            "preconditionSha256": receipt.precondition_sha256,
            "guardrailId": "guardrail123",
            "guardrailVersion": "7",
            "requiresMMDSV2": True,
            "requiresServiceS3Endpoint": False,
        },
    )
    outcome = journal.outcome_composer().compose(
        transaction=journal.current,
        provider_observation=provider,
    )
    assert outcome.step_observation is not None
    assert outcome.step_observation.runtime_version == prior_version
    journal.reconcile_step(outcome=outcome)
    journal.evidence_store.audit_prefix(
        plan=journal.plan,
        transaction=journal.current,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )


def test_image_observe_and_local_context_own_terminal_digests(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    _advance_to_ordinal(journal, 21)
    image_step = journal.plan.steps[journal.current.completed_step_count]
    image_read = _new_observation(
        service="ecr",
        operation="describe_image_scan_findings",
        subject=image_step.subject,
        disposition=ObservationDisposition.PRESENT,
        provider_status="COMPLETE",
        projection={
            "runtimeImageDigest": journal.plan.runtime_image_digest,
            "scanStatus": "COMPLETE",
            "signatureStatus": "SIGNED",
        },
    )
    image_outcome = journal.outcome_composer().compose(
        transaction=journal.current,
        provider_observation=image_read,
    )
    journal.complete_observation(outcome=image_outcome)
    assert journal.current.runtime_image_digest == (
        journal.plan.runtime_image_digest
    )

    _advance_to_ordinal(journal, 27)
    journal.begin_step()
    context_step = journal.plan.steps[journal.current.completed_step_count]
    context_sha256 = "e" * 64
    context_read = runtime_context_module.RuntimeContextLocalObservationV2(
        subject=context_step.subject,
        disposition=ObservationDisposition.PRESENT,
        provider_status="CREATED",
        projection=_runtime_context_projection(
            journal, context_sha256=context_sha256
        ),
        _token=runtime_context_module._LOCAL_OBSERVATION_TOKEN,
    )
    context_outcome = journal.outcome_composer().compose(
        transaction=journal.current,
        provider_observation=context_read,
    )
    assert context_outcome.step_observation is not None
    assert context_outcome.step_observation.runtime_context_sha256 == (
        context_sha256
    )
    journal.reconcile_step(outcome=context_outcome)
    assert journal.current.runtime_context_sha256 == context_sha256


def test_runtime_context_observation_rejects_crossed_plan_projection(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    _advance_to_ordinal(journal, 27)
    journal.begin_step()
    context_step = journal.plan.steps[journal.current.completed_step_count]
    context_sha256 = "e" * 64
    crossed_plan_mapping = journal.plan.to_mapping()
    crossed_plan_mapping["driverSha256"] = "9" * 64
    crossed_plan = type(journal.plan).from_mapping(crossed_plan_mapping)
    assert crossed_plan.digest() != journal.plan.digest()
    assert crossed_plan.source_commit == journal.plan.source_commit
    assert crossed_plan.steps == journal.plan.steps
    projection = _runtime_context_projection(
        journal, context_sha256=context_sha256
    )
    projection["planSha256"] = crossed_plan.digest()
    crossed = runtime_context_module.RuntimeContextLocalObservationV2(
        subject=context_step.subject,
        disposition=ObservationDisposition.PRESENT,
        provider_status="CREATED",
        projection=projection,
        _token=runtime_context_module._LOCAL_OBSERVATION_TOKEN,
    )

    with pytest.raises(EvidenceStoreV2Error, match="runtime context"):
        journal.outcome_composer().compose(
            transaction=journal.current,
            provider_observation=crossed,
        )


def test_runtime_context_endpoint_owner_tracks_shifted_plan_ordinal(
    tmp_path: Path,
) -> None:
    assembled = ReleasePlanAssemblerV2.assemble(
        _preclosed_source(tmp_path / "source", extra_assets=1)
    )
    journal = _create_v2(tmp_path / "journal", assembled.plan)
    journal.advance_preflight()
    context_ordinal = next(
        step.ordinal
        for step in journal.plan.steps
        if step.kind == "RUNTIME_CONTEXT_WRITE"
    )
    endpoint_ordinal = next(
        step.ordinal
        for step in journal.plan.steps
        if step.phase == "endpoint" and step.kind == "STACK_UPDATE"
    )
    assert endpoint_ordinal != 25
    _advance_to_ordinal(journal, context_ordinal)
    journal.begin_step()
    context_step = journal.plan.steps[context_ordinal]
    context_read = runtime_context_module.RuntimeContextLocalObservationV2(
        subject=context_step.subject,
        disposition=ObservationDisposition.PRESENT,
        provider_status="CREATED",
        projection=_runtime_context_projection(journal),
        _token=runtime_context_module._LOCAL_OBSERVATION_TOKEN,
    )

    outcome = journal.outcome_composer().compose(
        transaction=journal.current,
        provider_observation=context_read,
    )

    assert outcome.step_observation is not None
    assert outcome.step_observation.runtime_context_sha256 == "e" * 64


def test_runtime_context_projection_rejects_crossed_and_malformed_fields(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    context_ordinal = next(
        step.ordinal
        for step in journal.plan.steps
        if step.kind == "RUNTIME_CONTEXT_WRITE"
    )
    _advance_to_ordinal(journal, context_ordinal)
    journal.begin_step()
    context_step = journal.plan.steps[context_ordinal]
    valid = _runtime_context_projection(journal)
    hostile = (
        {**valid, "completedPrefixSha256": "0" * 64},
        {**valid, "sourceCommit": "0" * 40},
        {**valid, "sourceTree": "0" * 40},
        {**valid, "contextRelativePath": "build/crossed.json"},
        {**valid, "runtimeImageDigest": "sha256:" + "0" * 64},
        {**valid, "runtimeEndpointName": "crossed-endpoint"},
        {**valid, "runtimeEndpointArn": journal.current.runtime_arn},
        {
            **valid,
            "workloadIdentityArn": journal.current.runtime_arn,
        },
        {**valid, "endpointEvidenceSha256": "0" * 64},
        {**valid, "observedRuntimeContextSha256": "0" * 64},
        {**valid, "expectedRuntimeContextSha256": "not-a-digest"},
        {**valid, "size": True},
        {**valid, "size": 0},
        {**valid, "size": evidence_store_module.MAX_CONTRACT_BYTES + 1},
        {**valid, "unexpected": "field"},
        {name: value for name, value in valid.items() if name != "sourceTree"},
    )

    for projection in hostile:
        observation = runtime_context_module.RuntimeContextLocalObservationV2(
            subject=context_step.subject,
            disposition=ObservationDisposition.PRESENT,
            provider_status="CREATED",
            projection=projection,
            _token=runtime_context_module._LOCAL_OBSERVATION_TOKEN,
        )
        with pytest.raises(EvidenceStoreV2Error, match="runtime context"):
            journal.outcome_composer().compose(
                transaction=journal.current,
                provider_observation=observation,
            )


def test_runtime_context_projection_status_matches_disposition(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    context_ordinal = next(
        step.ordinal
        for step in journal.plan.steps
        if step.kind == "RUNTIME_CONTEXT_WRITE"
    )
    _advance_to_ordinal(journal, context_ordinal)
    journal.begin_step()
    valid = _runtime_context_projection(journal)
    accepted = (
        ("PRESENT", "PRESENT", valid),
        ("PRESENT", "CREATED", valid),
        (
            "ABSENT",
            "NOT_FOUND",
            {**valid, "observedRuntimeContextSha256": "", "size": 0},
        ),
        (
            "FAILED_RETAINED",
            "EXISTING_CONTENT_CONFLICT",
            {**valid, "observedRuntimeContextSha256": "0" * 64},
        ),
    )
    for disposition, provider_status, projection in accepted:
        journal.evidence_store._validate_runtime_context_projection(
            plan=journal.plan,
            transaction=journal.current,
            journal_path_sha256=(
                evidence_store_module._journal_path_sha256(journal.path)
            ),
            journal_execution_id=journal.journal_execution_id,
            disposition=disposition,
            provider_status=provider_status,
            projection=projection,
        )

    rejected = (
        ("PENDING", "PENDING", valid),
        ("ABSENT", "PRESENT", valid),
        ("PRESENT", "NOT_FOUND", valid),
        (
            "FAILED_RETAINED",
            "EXISTING_CONTENT_CONFLICT",
            valid,
        ),
    )
    for disposition, provider_status, projection in rejected:
        with pytest.raises(EvidenceStoreV2Error, match="runtime context"):
            journal.evidence_store._validate_runtime_context_projection(
                plan=journal.plan,
                transaction=journal.current,
                journal_path_sha256=(
                    evidence_store_module._journal_path_sha256(journal.path)
                ),
                journal_execution_id=journal.journal_execution_id,
                disposition=disposition,
                provider_status=provider_status,
                projection=projection,
            )

    context_step = journal.plan.steps[context_ordinal]
    absent = runtime_context_module.RuntimeContextLocalObservationV2(
        subject=context_step.subject,
        disposition=ObservationDisposition.ABSENT,
        provider_status="NOT_FOUND",
        projection={
            **valid,
            "observedRuntimeContextSha256": "",
            "size": 0,
        },
        _token=runtime_context_module._LOCAL_OBSERVATION_TOKEN,
    )
    outcome = journal.outcome_composer().compose(
        transaction=journal.current,
        provider_observation=absent,
    )
    assert outcome.retained_evidence.disposition == "ABSENT"


def test_endpoint_id_is_owned_only_by_phase_final_drift(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    _advance_to_ordinal(journal, 25)
    journal.begin_step()
    update_step = journal.plan.steps[journal.current.completed_step_count]
    endpoint_id = "Endpoint-ZYXWVUTSRQ"
    update_read = _new_observation(
        service="bedrock-agentcore-control",
        operation="get_agent_runtime_endpoint",
        subject=update_step.subject,
        disposition=ObservationDisposition.PRESENT,
        provider_status="READY",
        projection={
            "agentCoreStackId": journal.current.agent_core_stack_id,
            "runtimeId": journal.current.runtime_id,
            "runtimeVersion": journal.current.runtime_version,
            "runtimeArn": journal.current.runtime_arn,
            "endpointId": endpoint_id,
            "endpointName": journal.plan.runtime_endpoint_name,
            "endpointArn": (
                "arn:aws:bedrock-agentcore:eu-west-1:123456789012:"
                "agentEndpoint/12345678-1234-4234-8234-123456789abc"
            ),
            "workloadIdentityArn": (
                "arn:aws:bedrock-agentcore:eu-west-1:123456789012:"
                "workload-identity-directory/default/workload-identity/"
                "personal_operator_bridge"
            ),
        },
    )
    update_outcome = journal.outcome_composer().compose(
        transaction=journal.current,
        provider_observation=update_read,
    )
    assert update_outcome.step_observation is not None
    assert update_outcome.step_observation.runtime_endpoint_id == ""
    journal.reconcile_step(outcome=update_outcome)
    assert journal.current.runtime_endpoint_id == ""

    journal.begin_step()
    drift_step = journal.plan.steps[journal.current.completed_step_count]
    predecessor = update_outcome.retained_evidence
    sink = journal.evidence_store.stack_drift_receipt_sink(
        plan=journal.plan,
        transaction=journal.current,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )
    receipt = StackDriftDispatchReceiptV1.from_mapping(
        {
            "schema": StackDriftDispatchReceiptV1.SCHEMA,
            "releasePlanSha256": journal.plan.digest(),
            "evidenceStoreSha256": journal.evidence_store.identity_sha256,
            "journalPathSha256": evidence_store_module._journal_path_sha256(
                journal.path
            ),
            "journalExecutionId": journal.journal_execution_id,
            "journalRevision": journal.current.revision,
            "completedPrefixSha256": journal.completed_prefix_sha256(),
            "stepId": drift_step.step_id,
            "subject": drift_step.subject,
            "releaseOperationSha256": journal.current.uncertain_operation_sha256,
            "stackId": journal.current.agent_core_stack_id,
            "predecessorEvidenceSha256": predecessor.digest(),
            "predecessorObserverEvidenceSha256": (
                predecessor.observer_evidence_sha256
            ),
            "driftDetectionId": "87654321-4321-4321-8321-cba987654321",
        }
    )
    assert sink._begin_attempt() is True
    sink._retain(receipt.to_bytes())
    drift_read = _new_observation(
        service="cloudformation",
        operation="describe_stack_drift_detection_status",
        subject=drift_step.subject,
        disposition=ObservationDisposition.PRESENT,
        provider_status="IN_SYNC",
        projection={
            "dispatchReceiptSha256": receipt.digest(),
            "driftDetectionId": receipt.drift_detection_id,
            "stackId": receipt.stack_id,
            "predecessorEvidenceSha256": predecessor.digest(),
            "predecessorObserverEvidenceSha256": (
                predecessor.observer_evidence_sha256
            ),
            "resourceDriftCount": 0,
            "closingStackSha256": "1" * 64,
            "closingTemplateSha256": "2" * 64,
            "closingStackPolicySha256": "3" * 64,
        },
    )
    drift_outcome = journal.outcome_composer().compose(
        transaction=journal.current,
        provider_observation=drift_read,
    )
    assert drift_outcome.step_observation is not None
    assert drift_outcome.step_observation.runtime_endpoint_id == endpoint_id
    journal.reconcile_step(outcome=drift_outcome)
    assert journal.current.runtime_endpoint_id == endpoint_id


def test_v2_free_observation_cannot_advance_but_retained_capability_can(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    observation = _observation(journal)

    with pytest.raises(TypeError, match="unexpected keyword"):
        journal.complete_observation(observation=observation)

    store = journal.evidence_store
    evidence = _retain(store, journal)
    completed = journal.complete_observation(outcome=evidence)

    assert completed.completed_step_count == 1
    assert completed.completed_steps[0].evidence_sha256 == (
        evidence.retained_evidence.digest()
    )
    store.audit_prefix(
        plan=journal.plan,
        transaction=completed,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )


def test_retained_prefix_resolver_returns_only_current_completed_records(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    _advance_to_ordinal(journal, 2)
    stale = journal.current

    retained = journal.evidence_store.retained_prefix_for_execution(
        plan=journal.plan,
        transaction=journal.current,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )

    assert isinstance(retained, tuple)
    assert tuple(item.digest() for item in retained) == tuple(
        item.evidence_sha256 for item in journal.current.completed_steps
    )
    assert tuple(item.step_id for item in retained) == tuple(
        item.step_id for item in journal.current.completed_steps
    )

    _advance_to_ordinal(journal, 3)
    with pytest.raises(EvidenceStoreV2Error, match="current journal"):
        journal.evidence_store.retained_prefix_for_execution(
            plan=journal.plan,
            transaction=stale,
            journal_path=journal.path,
            journal_execution_id=journal.journal_execution_id,
        )


def test_retained_prefix_resolver_rejects_crossed_and_missing_records(
    tmp_path: Path,
) -> None:
    first = _create_v2(tmp_path / "first")
    second = _create_v2(tmp_path / "second", first.plan)
    for journal in (first, second):
        journal.advance_preflight()
        _advance_to_ordinal(journal, 1)

    with pytest.raises(EvidenceStoreV2Error):
        first.evidence_store.retained_prefix_for_execution(
            plan=first.plan,
            transaction=second.current,
            journal_path=first.path,
            journal_execution_id=first.journal_execution_id,
        )
    with pytest.raises(EvidenceStoreV2Error):
        first.evidence_store.retained_prefix_for_execution(
            plan=first.plan,
            transaction=first.current,
            journal_path=second.path,
            journal_execution_id=second.journal_execution_id,
        )

    record = first.evidence_store.retained_prefix_for_execution(
        plan=first.plan,
        transaction=first.current,
        journal_path=first.path,
        journal_execution_id=first.journal_execution_id,
    )[0]
    record_path = (
        first.evidence_store.root
        / first.plan.digest()
        / first.evidence_store._record_name(record)
    )
    record_path.unlink()
    with pytest.raises(EvidenceStoreV2Error, match="missing|invalid"):
        first.evidence_store.retained_prefix_for_execution(
            plan=first.plan,
            transaction=first.current,
            journal_path=first.path,
            journal_execution_id=first.journal_execution_id,
        )


def test_release_verification_observation_has_one_exact_composer_pair() -> None:
    assert ReleaseEvidenceStoreV2._allowed_provider_pairs(
        phase="verify",
        kind="VERIFY",
        subject="release:123456789012:eu-west-1:" + "a" * 40 + ":verify",
        disposition="PRESENT",
    ) == frozenset({("local-release-verifier", "verify_release")})

    with pytest.raises(
        release_verifier_module.ReleaseVerifierV2Error,
        match="not constructible",
    ):
        release_verifier_module.ReleaseVerificationObservationV2(
            subject="release:123456789012:eu-west-1:"
            + "a" * 40
            + ":verify",
            projection={},
        )


def test_private_release_verifier_observation_alone_closes_verify(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    verify_ordinal = next(
        step.ordinal for step in journal.plan.steps if step.kind == "VERIFY"
    )
    _advance_to_ordinal(journal, verify_ordinal)
    step = journal.plan.steps[verify_ordinal]
    projection = _release_verification_projection(journal)

    tampered_values = {
        "planSha256": "f" * 64,
        "transactionSha256": "f" * 64,
        "completedPrefixSha256": "f" * 64,
        "retainedPrefixSha256": "f" * 64,
        "evidenceStoreSha256": "f" * 64,
        "journalPathSha256": "f" * 64,
        "journalExecutionId": "f" * 64,
        "journalRevision": journal.current.revision + 1,
        "completedRecordCount": len(journal.current.completed_steps) + 1,
        "foundationInputsSha256": "f" * 64,
        "runtimeImageDigest": "sha256:" + "f" * 64,
        "imageObservationSha256": "f" * 64,
        "runtimeId": "Runtime-ZYXWVUTSRQ",
        "runtimeVersion": "99",
        "runtimeArn": journal.current.runtime_arn.rsplit(":", 1)[0] + ":99",
        "runtimeEndpointId": "Endpoint-ZYXWVUTSRQ",
        "runtimeEndpointName": "crossed-endpoint",
        "runtimeEndpointArn": (
            f"arn:aws:bedrock-agentcore:{journal.plan.region}:"
            f"{journal.plan.account}:runtime/{journal.current.runtime_id}/"
            f"runtime-endpoint/{journal.current.runtime_endpoint_id}"
        ),
        "runtimeWorkloadIdentityArn": journal.current.runtime_arn,
        "runtimeConfigurationSha256": "not-a-digest",
        "runtimeIamRequestSha256": "not-a-digest",
        "runtimeIamObservationSha256": "not-a-digest",
        "runtimeContextSha256": "f" * 64,
        "runtimeContextObservationSha256": "not-a-digest",
        "guardrailId": "crossed-guardrail",
        "guardrailVersion": "99",
    }
    for field, value in tampered_values.items():
        with pytest.raises(EvidenceStoreV2Error, match="verification"):
            journal.evidence_store._validate_release_verification_projection(
                plan=journal.plan,
                transaction=journal.current,
                journal_path_sha256=(
                    evidence_store_module._journal_path_sha256(journal.path)
                ),
                journal_execution_id=journal.journal_execution_id,
                projection={**projection, field: value},
            )
    for malformed in (
        {**projection, "unexpected": "field"},
        {
            name: value
            for name, value in projection.items()
            if name != "transactionSha256"
        },
    ):
        with pytest.raises(EvidenceStoreV2Error, match="verification"):
            journal.evidence_store._validate_release_verification_projection(
                plan=journal.plan,
                transaction=journal.current,
                journal_path_sha256=(
                    evidence_store_module._journal_path_sha256(journal.path)
                ),
                journal_execution_id=journal.journal_execution_id,
                projection=malformed,
            )

    generic = production_observer_module.CanonicalReadObservationV2(
        service="local-release-verifier",
        operation="verify_release",
        subject=step.subject,
        disposition=ObservationDisposition.PRESENT,
        provider_status="VERIFIED",
        projection_bytes=canonical_json_bytes(projection),
        _token=production_observer_module._OBSERVATION_TOKEN,
    )
    with pytest.raises(EvidenceStoreV2Error, match="private release verifier"):
        journal.outcome_composer().compose(
            transaction=journal.current,
            provider_observation=generic,
        )

    crossed_projection = {**projection, "journalExecutionId": "f" * 64}
    crossed = release_verifier_module.ReleaseVerificationObservationV2(
        subject=step.subject,
        projection=crossed_projection,
        _token=release_verifier_module._VERIFICATION_OBSERVATION_TOKEN,
    )
    with pytest.raises(EvidenceStoreV2Error, match="verification projection"):
        journal.outcome_composer().compose(
            transaction=journal.current,
            provider_observation=crossed,
        )

    verified = release_verifier_module.ReleaseVerificationObservationV2(
        subject=step.subject,
        projection=projection,
        _token=release_verifier_module._VERIFICATION_OBSERVATION_TOKEN,
    )
    outcome = journal.outcome_composer().compose(
        transaction=journal.current,
        provider_observation=verified,
    )
    assert outcome.step_observation is not None
    assert len(outcome.step_observation.verification_sha256) == 64
    completed = journal.complete_observation(outcome=outcome)
    assert completed.state == "VERIFIED"
    journal.evidence_store.audit_prefix(
        plan=journal.plan,
        transaction=completed,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )


def test_retained_step_evidence_is_canonical_and_transitively_binds_raw_bytes(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    store = journal.evidence_store
    evidence = _retain(store, journal)
    record = evidence.retained_evidence

    assert RetainedStepEvidenceV2.from_bytes(record.to_bytes()) == record
    assert parse_release_contract(record.to_bytes()) == record
    assert record.step_observation.observer_evidence_sha256 == (
        record.observer_evidence_sha256
    )
    value = record.to_mapping()
    value["observerEvidence"]["projection"]["marker"] = "substituted"
    with pytest.raises(ContractError, match="observer evidence digest"):
        RetainedStepEvidenceV2.from_mapping(value)


def test_retained_capability_is_private_single_use_and_target_bound(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    replay_target = _create_v2(tmp_path / "replay-target", journal.plan)
    replay_target.advance_preflight()
    store = journal.evidence_store

    with pytest.raises(EvidenceStoreV2Error, match="not constructible"):
        VerifiedStepOutcomeV2(  # type: ignore[call-arg]
            store=store,
            record=None,
            record_name="forged",
            payload=b"forged",
        )

    evidence = _retain(store, journal)
    with pytest.raises(EvidenceStoreV2Error, match="composer-owned"):
        store._retain_record(evidence.retained_evidence)
    journal.complete_observation(outcome=evidence)
    with pytest.raises(TransactionError, match="already consumed"):
        replay_target.complete_observation(outcome=evidence)


def test_journal_cas_failure_consumes_only_capability_not_retained_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    store = journal.evidence_store
    evidence = _retain(store, journal, marker="cas-crash")
    real_atomic_replace = transaction_module.atomic_replace_contract

    def fail_cas(*_args, **_kwargs):
        raise ContractError("release artifact changed concurrently")

    monkeypatch.setattr(
        transaction_module, "atomic_replace_contract", fail_cas
    )
    with pytest.raises(TransactionError, match="changed concurrently"):
        journal.complete_observation(outcome=evidence)
    assert journal.current.completed_step_count == 0
    assert evidence.path.exists()

    monkeypatch.setattr(
        transaction_module, "atomic_replace_contract", real_atomic_replace
    )
    recovered = _retain(store, journal, marker="cas-crash")
    journal.complete_observation(outcome=recovered)
    store.audit_prefix(
        plan=journal.plan,
        transaction=journal.current,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )


def _crash_before_journal_cas(
    monkeypatch: pytest.MonkeyPatch,
    transition,
) -> None:
    real_atomic_replace = transaction_module.atomic_replace_contract

    def fail_cas(*_args, **_kwargs):
        raise ContractError("release artifact changed concurrently")

    monkeypatch.setattr(
        transaction_module, "atomic_replace_contract", fail_cas
    )
    with pytest.raises(TransactionError, match="changed concurrently"):
        transition()
    monkeypatch.setattr(
        transaction_module, "atomic_replace_contract", real_atomic_replace
    )


def _forbid_provider_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_provider_call(*_args, **_kwargs):
        raise AssertionError("crash recovery must not call a provider composer")

    monkeypatch.setattr(
        evidence_store_module.ReleaseEvidenceStoreV2,
        "composer",
        fail_provider_call,
    )


def test_load_recovers_retained_pending_then_accepts_present_without_reobserve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    journal.complete_observation(
        outcome=_retain(journal.evidence_store, journal, marker="baseline")
    )
    journal.begin_step()
    prior = journal.current
    pending = _retained_outcome(
        journal, ObservationDisposition.PENDING, marker="pending-crash"
    )

    _crash_before_journal_cas(
        monkeypatch,
        lambda: journal.reconcile_step(outcome=pending),
    )
    assert journal.path.read_bytes() == prior.to_bytes()
    _forbid_provider_composition(monkeypatch)

    recovered = TransactionJournalV2.load(
        journal.path,
        plan=journal.plan,
        evidence_store=journal.evidence_store,
    )

    assert recovered.current == replace(prior, revision=prior.revision + 1)
    present = _retained_outcome(
        recovered,
        ObservationDisposition.PRESENT,
        observation=_observation(recovered),
        marker="present-after-pending",
    )
    completed = recovered.reconcile_step(outcome=present)
    assert completed.completed_step_count == prior.completed_step_count + 1


def test_load_recovers_retained_absent_then_accepts_present_without_reobserve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    prior = journal.current
    absent = _retained_outcome(
        journal, ObservationDisposition.ABSENT, marker="absent-crash"
    )

    _crash_before_journal_cas(
        monkeypatch,
        lambda: journal.complete_observation(outcome=absent),
    )
    assert journal.path.read_bytes() == prior.to_bytes()
    _forbid_provider_composition(monkeypatch)

    recovered = TransactionJournalV2.load(
        journal.path,
        plan=journal.plan,
        evidence_store=journal.evidence_store,
    )

    assert recovered.current == replace(prior, revision=prior.revision + 1)
    present = _retained_outcome(
        recovered,
        ObservationDisposition.PRESENT,
        observation=_observation(recovered),
        marker="present-after-absent",
    )
    completed = recovered.complete_observation(outcome=present)
    assert completed.completed_step_count == 1


@pytest.mark.parametrize(
    "disposition",
    (ObservationDisposition.PRESENT, ObservationDisposition.FAILED_RETAINED),
)
def test_load_finalizes_retained_terminal_or_completed_tip_without_reobserve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disposition: ObservationDisposition,
) -> None:
    journal = _create_v2(tmp_path / disposition.value.lower())
    journal.advance_preflight()
    journal.complete_observation(
        outcome=_retain(journal.evidence_store, journal, marker="baseline")
    )
    journal.begin_step()
    prior_revision = journal.current.revision
    if disposition == ObservationDisposition.PRESENT:
        outcome = _retained_outcome(
            journal,
            disposition,
            observation=_observation(journal),
            marker="present-crash",
        )
    else:
        outcome = _retained_outcome(
            journal,
            disposition,
            failure_evidence=_failed_retained_evidence(journal),
            marker="failure-crash",
        )
    prior_payload = journal.path.read_bytes()

    _crash_before_journal_cas(
        monkeypatch,
        lambda: journal.reconcile_step(outcome=outcome),
    )
    transition_path = next(
        outcome.path.parent.glob(
            f"transition-{journal.journal_execution_id}-{prior_revision:08d}.json"
        )
    )
    transition = parse_release_contract(transition_path.read_bytes())
    _forbid_provider_composition(monkeypatch)

    recovered = TransactionJournalV2.load(
        journal.path,
        plan=journal.plan,
        evidence_store=journal.evidence_store,
    )

    assert prior_payload == transition.prior_transaction.to_bytes()
    assert recovered.current == transition.next_transaction
    if disposition == ObservationDisposition.PRESENT:
        assert recovered.current.completed_step_count == 2
    else:
        assert recovered.current.state == "ABORTED_RETAINED"


def test_load_rejects_contradictory_uncommitted_tip_before_any_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    absent = _retained_outcome(
        journal, ObservationDisposition.ABSENT, marker="tampered-tip"
    )
    _crash_before_journal_cas(
        monkeypatch,
        lambda: journal.complete_observation(outcome=absent),
    )
    journal_payload = journal.path.read_bytes()
    transition_path = next(
        absent.path.parent.glob(
            f"transition-{journal.journal_execution_id}-00000001.json"
        )
    )
    transition = parse_release_contract(transition_path.read_bytes())
    value = transition.to_mapping()
    value["transitionKind"] = "OUTCOME_PENDING"
    transition_path.unlink()
    transition_path.write_bytes(canonical_json_bytes(value))
    transition_path.chmod(0o400)
    commit_path = absent.path.parent / (
        f"commit-{journal.journal_execution_id}-00000001.json"
    )

    with pytest.raises(TransactionError, match="classification|transition"):
        TransactionJournalV2.load(
            journal.path,
            plan=journal.plan,
            evidence_store=journal.evidence_store,
        )

    assert journal.path.read_bytes() == journal_payload
    assert not commit_path.exists()


def test_load_recovery_is_idempotent_after_cas_and_commit_crashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    outcome = _retained_outcome(
        journal, ObservationDisposition.ABSENT, marker="commit-crash"
    )
    store = journal.evidence_store
    real_commit_transition = store.commit_transition

    def fail_commit(*_args, **_kwargs):
        raise EvidenceStoreV2Error("simulated transition commit crash")

    monkeypatch.setattr(store, "commit_transition", fail_commit)
    with pytest.raises(TransactionError, match="transition commit failed"):
        journal.complete_observation(outcome=outcome)
    advanced_payload = journal.path.read_bytes()
    monkeypatch.setattr(store, "commit_transition", real_commit_transition)

    real_write_commit = store._write_transition_commit
    monkeypatch.setattr(store, "_write_transition_commit", fail_commit)
    with pytest.raises(TransactionError, match="recover|commit"):
        TransactionJournalV2.load(
            journal.path, plan=journal.plan, evidence_store=store
        )
    assert journal.path.read_bytes() == advanced_payload
    monkeypatch.setattr(store, "_write_transition_commit", real_write_commit)

    recovered = TransactionJournalV2.load(
        journal.path, plan=journal.plan, evidence_store=store
    )
    commit_path = outcome.path.parent / (
        f"commit-{journal.journal_execution_id}-00000001.json"
    )
    commit_payload = commit_path.read_bytes()
    reloaded = TransactionJournalV2.load(
        journal.path, plan=journal.plan, evidence_store=store
    )

    assert recovered.current == reloaded.current
    assert journal.path.read_bytes() == advanced_payload
    assert commit_path.read_bytes() == commit_payload


def test_orphan_record_is_reused_after_crash_before_journal_cas(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    store = journal.evidence_store

    orphan = _retain(store, journal, marker="orphan")
    orphan_payload = orphan.path.read_bytes()
    recovered = _retain(store, journal, marker="orphan")

    assert recovered.path == orphan.path
    assert recovered.path.read_bytes() == orphan_payload
    journal.complete_observation(outcome=recovered)
    store.audit_prefix(
        plan=journal.plan,
        transaction=journal.current,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )


@pytest.mark.parametrize("damage", ["symlink", "truncated", "substituted"])
def test_capability_rechecks_append_only_record_before_journal_advance(
    tmp_path: Path,
    damage: str,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    store = journal.evidence_store
    evidence = _retain(store, journal)
    path = evidence.path
    payload = path.read_bytes()
    path.unlink()
    if damage == "symlink":
        replacement = tmp_path / "replacement.json"
        replacement.write_bytes(payload)
        path.symlink_to(replacement)
    elif damage == "truncated":
        path.write_bytes(payload[: len(payload) // 2])
    else:
        path.write_bytes(payload.replace(b"canonical", b"hostile__"))

    with pytest.raises(
        TransactionError,
        match="retained (?:evidence|outcome)|not a regular file|mode|inventory",
    ):
        journal.complete_observation(outcome=evidence)
    assert journal.current.completed_step_count == 0


def test_prefix_audit_rejects_missing_or_substituted_completed_record(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    store = journal.evidence_store
    evidence = _retain(store, journal)
    journal.complete_observation(outcome=evidence)
    store.audit_prefix(
        plan=journal.plan,
        transaction=journal.current,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )

    payload = evidence.path.read_bytes()
    evidence.path.unlink()
    with pytest.raises(
        EvidenceStoreV2Error, match="missing|regular file|inventory"
    ):
        store.audit_prefix(
            plan=journal.plan,
            transaction=journal.current,
            journal_path=journal.path,
            journal_execution_id=journal.journal_execution_id,
        )

    evidence.path.write_bytes(payload.replace(b"canonical", b"hostile__"))
    with pytest.raises(
        EvidenceStoreV2Error, match="invalid|differs|canonical|mode|inventory"
    ):
        store.audit_prefix(
            plan=journal.plan,
            transaction=journal.current,
            journal_path=journal.path,
            journal_execution_id=journal.journal_execution_id,
        )


def test_evidence_record_fsyncs_payload_and_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    store = journal.evidence_store
    calls: list[int] = []
    real_fsync = evidence_store_module.os.fsync

    def observed(descriptor: int) -> None:
        calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(evidence_store_module.os, "fsync", observed)
    _retain(store, journal)

    assert len(calls) >= 2


def test_present_reconciliation_requires_retained_capability(tmp_path: Path) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    store = journal.evidence_store
    baseline = _retain(store, journal)
    journal.complete_observation(outcome=baseline)
    journal.begin_step()
    operation = journal.current.uncertain_operation_sha256

    with pytest.raises(TypeError, match="unexpected keyword"):
        journal.reconcile_step(
            disposition=ObservationDisposition.PRESENT,
            operation_sha256=operation,
            observation=_observation(journal),
        )

    retained = _retain(store, journal, marker="mutation")
    completed = journal.reconcile_step(outcome=retained)
    assert completed.completed_step_count == 2


def test_cross_journal_replay_completed_count_1(tmp_path: Path) -> None:
    first = _create_v2(tmp_path / "first")
    replay = _create_v2(tmp_path / "replay", first.plan)
    first.advance_preflight()
    replay.advance_preflight()
    store = first.evidence_store
    outcome = _retain(store, first, marker="cross-journal")

    with pytest.raises(TransactionError, match="journal|execution|path|store"):
        replay.complete_observation(outcome=outcome)
    assert replay.current.completed_step_count == 0


def test_advanced_after_prior_record_deleted_2(tmp_path: Path) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    store = journal.evidence_store
    baseline = _retain(store, journal, marker="baseline")
    journal.complete_observation(outcome=baseline)
    baseline.path.unlink()

    with pytest.raises(TransactionError, match="prefix|missing|record"):
        journal.begin_step()
    assert journal.current.completed_step_count == 1


def test_arbitrary_failure_digest_terminal_state_ABORTED_RETAINED(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    store = journal.evidence_store
    journal.complete_observation(outcome=_retain(store, journal))
    journal.begin_step()
    caller_built = _failed_retained_evidence(journal)

    with pytest.raises(
        (TypeError, TransactionError), match="retained|outcome|unexpected"
    ):
        journal.reconcile_step(
            disposition=ObservationDisposition.FAILED_RETAINED,
            operation_sha256=journal.current.uncertain_operation_sha256,
            failure_evidence=caller_built,
        )
    assert journal.current.state == "UNCERTAIN"


def test_typed_failure_must_reconstruct_from_raw_provider_before_cas(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    journal.complete_observation(
        outcome=_retain(journal.evidence_store, journal, marker="baseline")
    )
    journal.begin_step()
    before = journal.current
    evidence = _failed_retained_evidence(journal)
    crossed = _retained_outcome(
        journal,
        ObservationDisposition.FAILED_RETAINED,
        failure_evidence=evidence,
        service="s3",
        operation="head_object",
    )

    with pytest.raises(
        TransactionError, match="provider failure status|raw provider"
    ):
        journal.reconcile_step(outcome=crossed)
    assert journal.current == before
    assert journal.path.read_bytes() == before.to_bytes()


def test_unretained_absent_clears_intent_PREFLIGHTED_True(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    store = journal.evidence_store
    journal.complete_observation(outcome=_retain(store, journal))
    journal.begin_step()

    with pytest.raises(
        (TypeError, TransactionError), match="retained|outcome|unexpected"
    ):
        journal.reconcile_step(
            disposition=ObservationDisposition.ABSENT,
            operation_sha256=journal.current.uncertain_operation_sha256,
        )
    assert journal.current.state == "UNCERTAIN"


def test_alternate_records_same_operation_True_True(tmp_path: Path) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    store = journal.evidence_store
    first = _retain(store, journal, marker="first")

    with pytest.raises(EvidenceStoreV2Error, match="operation|alternate"):
        _retain(store, journal, marker="second")
    assert first.path.exists()


@pytest.mark.parametrize(
    "proof_name",
    ("record_link_count_before_claim_2", "hardlinked_record_accepted_1"),
)
def test_hardlinked_record_is_rejected_before_claim(
    tmp_path: Path,
    proof_name: str,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    store = journal.evidence_store
    outcome = _retain(store, journal, marker=proof_name)
    os.link(outcome.path, tmp_path / f"{proof_name}.json")
    assert outcome.path.stat().st_nlink == 2

    with pytest.raises(TransactionError, match="link|record"):
        journal.complete_observation(outcome=outcome)
    assert journal.current.completed_step_count == 0


def test_prefix_audit_accepted_journal_derived_digest_mismatch_True(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    store = journal.evidence_store
    journal.complete_observation(outcome=_retain(store, journal))
    forged = replace(journal.current, rollback_baseline_sha256="f" * 64)

    with pytest.raises(EvidenceStoreV2Error, match="derived|rollback|prefix"):
        store.audit_prefix(
            plan=journal.plan,
            transaction=forged,
            journal_path=journal.path,
            journal_execution_id=journal.journal_execution_id,
        )


@pytest.mark.parametrize("case", ("stable", "uncertain", "terminal-failure"))
def test_journal_revision_requires_an_exact_persisted_transition_chain(
    tmp_path: Path,
    case: str,
) -> None:
    journal = _create_v2(tmp_path / case)
    journal.advance_preflight()
    journal.complete_observation(
        outcome=_retain(journal.evidence_store, journal, marker="baseline")
    )
    if case in {"uncertain", "terminal-failure"}:
        journal.begin_step()
    if case == "terminal-failure":
        failure = _failed_retained_evidence(journal)
        outcome = _retained_outcome(
            journal,
            ObservationDisposition.FAILED_RETAINED,
            failure_evidence=failure,
        )
        journal.reconcile_step(outcome=outcome)
    forged = replace(journal.current, revision=journal.current.revision + 10)

    with pytest.raises(
        EvidenceStoreV2Error, match="revision|transition|journal chain"
    ):
        journal.evidence_store.audit_prefix(
            plan=journal.plan,
            transaction=forged,
            journal_path=journal.path,
            journal_execution_id=journal.journal_execution_id,
        )

    journal.path.write_bytes(forged.to_bytes())
    with pytest.raises(TransactionError, match="revision|transition|journal chain"):
        transaction_module.TransactionJournalV2.load(
            journal.path,
            plan=journal.plan,
            evidence_store=journal.evidence_store,
        )


def test_record_open_is_nonblocking_and_rejects_insecure_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    store = journal.evidence_store
    outcome = _retain(store, journal)
    outcome.path.chmod(0o640)
    observed_flags: list[int] = []
    real_open = evidence_store_module.os.open

    def observe_open(path, flags, *args, **kwargs):
        observed_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(evidence_store_module.os, "open", observe_open)
    with pytest.raises(TransactionError, match="mode|permission|record"):
        journal.complete_observation(outcome=outcome)
    assert any(flags & os.O_NONBLOCK for flags in observed_flags)


def test_retained_directory_identity_rejects_parent_replacement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    store = ReleaseEvidenceStoreV2(root)
    journal = _create_v2(tmp_path, evidence_store=store)
    journal.advance_preflight()
    outcome = _retain(store, journal)
    payload = outcome.path.read_bytes()
    relative = outcome.path.relative_to(root)
    moved = tmp_path / "moved-evidence"
    root.rename(moved)
    replacement = root / relative.parent
    replacement.mkdir(parents=True, mode=0o700)
    (root / relative).write_bytes(payload)

    with pytest.raises(
        TransactionError, match="directory|replaced|identity|root|mode"
    ):
        journal.complete_observation(outcome=outcome)


def test_provider_service_operation_mismatch_is_rejected(tmp_path: Path) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    store = journal.evidence_store

    with pytest.raises(EvidenceStoreV2Error, match="provider|unsupported|kind"):
        journal.outcome_composer().compose(
            transaction=journal.current,
            provider_observation=_new_observation(
                service="ecr",
                operation="batch_get_image",
                subject=journal.plan.steps[0].subject,
                disposition=ObservationDisposition.PRESENT,
                provider_status="PRESENT",
                projection={"marker": "wrong-provider-for-baseline"},
            ),
        )


def test_nonempty_baseline_is_exact_retained_terminal_failure(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    step = journal.plan.steps[0]
    provider = _new_observation(
        service="cloudformation",
        operation="describe_stacks",
        subject=step.subject,
        disposition=ObservationDisposition.FAILED_RETAINED,
        provider_status="NONEMPTY_ACCOUNT",
        projection={
            "account": journal.plan.account,
            "inventory": [
                {
                    "stackName": "OpenClawVpc",
                    "state": "PRESENT",
                    "stackStatus": "CREATE_COMPLETE",
                }
            ],
            "region": journal.plan.region,
            "requestSha256": "1" * 64,
            "sourceCommit": journal.plan.source_commit,
            "sweeps": 2,
        },
    )
    outcome = journal.outcome_composer().compose(
        transaction=journal.current,
        provider_observation=provider,
    )
    assert outcome.failure_observation is not None
    assert outcome.failure_observation.failure_reason == "BASELINE_NOT_CLEAN"
    journal.complete_observation(outcome=outcome)
    assert journal.current.state == "ABORTED_RETAINED"
    assert journal.current.failure_reason == "BASELINE_NOT_CLEAN"
    journal.evidence_store.audit_prefix(
        plan=journal.plan,
        transaction=journal.current,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )


@pytest.mark.parametrize(
    ("disposition", "status", "expected_state", "expected_count"),
    (
        (ObservationDisposition.ABSENT, "NOT_FOUND", "PREFLIGHTED", 1),
        (
            ObservationDisposition.PENDING,
            "CREATE_IN_PROGRESS",
            "UNCERTAIN",
            1,
        ),
        (
            ObservationDisposition.PRESENT,
            "CREATE_COMPLETE",
            "PREFLIGHTED",
            2,
        ),
        (
            ObservationDisposition.FAILED_RETAINED,
            "CREATE_FAILED",
            "ABORTED_RETAINED",
            1,
        ),
    ),
)
def test_official_composer_retains_every_provider_disposition(
    tmp_path: Path,
    disposition: ObservationDisposition,
    status: str,
    expected_state: str,
    expected_count: int,
) -> None:
    journal = _create_v2(tmp_path / disposition.value.lower())
    journal.advance_preflight()
    journal.complete_observation(
        outcome=_retain(journal.evidence_store, journal, marker="baseline")
    )
    journal.begin_step()
    step = journal.plan.steps[journal.current.completed_step_count]
    projection = {"stackName": "CDKToolkit"}
    if disposition == ObservationDisposition.PRESENT:
        projection = {
            "templateParameterSha256": step.expected_template_parameter_sha256,
            "observedRequestSha256": step.expected_observed_request_sha256,
        }
    provider = _new_observation(
        service="cloudformation",
        operation="describe_stacks",
        subject=step.subject,
        disposition=disposition,
        provider_status=status,
        projection=projection,
    )

    outcome = journal.outcome_composer().compose(
        transaction=journal.current,
        provider_observation=provider,
    )
    record = outcome.retained_evidence
    assert record.observer_evidence_sha256 == provider.digest()
    assert record.observer_evidence_mapping() == provider.to_mapping()
    assert (record.step_observation is not None) == (
        disposition == ObservationDisposition.PRESENT
    )
    assert (record.failure_observation is not None) == (
        disposition == ObservationDisposition.FAILED_RETAINED
    )

    completed = journal.reconcile_step(outcome=outcome)
    assert completed.state == expected_state
    assert completed.completed_step_count == expected_count
    journal.evidence_store.audit_prefix(
        plan=journal.plan,
        transaction=completed,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )


def test_syntactic_phase_digest_from_provider_cannot_advance(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    journal.complete_observation(
        outcome=_retain(journal.evidence_store, journal, marker="baseline")
    )
    journal.begin_step()
    step = journal.plan.steps[journal.current.completed_step_count]
    provider = _new_observation(
        service="cloudformation",
        operation="describe_stacks",
        subject=step.subject,
        disposition=ObservationDisposition.PRESENT,
        provider_status="CREATE_COMPLETE",
        projection={
            "templateParameterSha256": step.expected_template_parameter_sha256,
            "observedRequestSha256": step.expected_observed_request_sha256,
            "routerCronChangesetsSha256": "f" * 64,
        },
    )

    with pytest.raises(EvidenceStoreV2Error, match="caller-supplied derived"):
        journal.outcome_composer().compose(
            transaction=journal.current,
            provider_observation=provider,
        )
    assert journal.current.state == "UNCERTAIN"
    assert journal.current.completed_step_count == 1


def test_final_foundation_requires_observer_derived_runtime_inputs(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    final_outcome = _complete_production_foundation(journal)
    observation = final_outcome.step_observation
    assert observation is not None
    foundation = observation.foundation_runtime_inputs
    assert foundation is not None
    assert foundation.private_subnet_ids == (
        "subnet-00000000000000001",
        "subnet-00000000000000002",
    )
    assert foundation.runtime_security_group_ids == (
        "sg-00000000000000001",
    )
    assert foundation.user_files_bucket_name == (
        "openclaw-user-files-123456789012-eu-west-1"
    )
    assert foundation.capability_gateway_function_arn.endswith(
        ":function:personal-operator-capability-gateway"
    )
    assert foundation.workspace_broker_function_name == (
        "personal-operator-workspace-credential-broker"
    )
    assert foundation.guardrail_id == "guardrail123"
    assert foundation.guardrail_version == "7"
    assert foundation.agent_core_stack_id.endswith(
        ":stack/OpenClawAgentCore/stack-12"
    )
    assert journal.current.foundation_inputs_sha256 == foundation.digest()
    assert journal.current.agent_core_stack_id == foundation.agent_core_stack_id

    retained = journal.evidence_store._all_records(
        plan_sha256=journal.plan.digest()
    )
    by_digest = {record.digest(): record for record in retained}
    phase_records = []
    for item in journal.current.completed_steps[:16]:
        record = by_digest[item.evidence_sha256]
        phase_records.append(
            {
                "stepId": record.step_id,
                "subject": record.subject,
                "operationSha256": record.release_operation_sha256,
                "observerEvidenceSha256": record.observer_evidence_sha256,
            }
        )
    expected_snapshot = hashlib.sha256(
        canonical_json_bytes(
            {
                "schema": "personal-operator.phase-evidence.v1",
                "planSha256": journal.plan.digest(),
                "phase": "foundation",
                "records": phase_records,
            }
        )
    ).hexdigest()
    assert foundation.foundation_snapshot_sha256 == expected_snapshot
    with ReleaseEvidenceStoreV2(journal.evidence_store.root) as reopened:
        reopened.audit_prefix(
            plan=journal.plan,
            transaction=journal.current,
            journal_path=journal.path,
            journal_execution_id=journal.journal_execution_id,
        )


def test_terminal_phase_digest_is_derived_from_ordered_raw_provider_records(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    first_phase_outcome = None
    while True:
        step = journal.plan.steps[journal.current.completed_step_count]
        if step.phase == "router-cron-cs" and first_phase_outcome is not None:
            break
        if step.mutation:
            journal.begin_step()
            outcome = _retain(
                journal.evidence_store,
                journal,
                marker=f"aggregate-{step.ordinal}",
            )
            journal.reconcile_step(outcome=outcome)
        else:
            outcome = _retain(
                journal.evidence_store,
                journal,
                marker=f"aggregate-{step.ordinal}",
            )
            journal.complete_observation(outcome=outcome)
        if step.phase == "router-cron-cs":
            first_phase_outcome = outcome

    assert first_phase_outcome is not None
    journal.begin_step()
    step = journal.plan.steps[journal.current.completed_step_count]
    provider = _new_observation(
        service="cloudformation",
        operation="describe_change_set",
        subject=step.subject,
        disposition=ObservationDisposition.PRESENT,
        provider_status="CREATE_COMPLETE",
        projection={
            "stackId": _consumer_stack_id("OpenClawCron", 2),
            "changeSetId": _consumer_change_set_id(2),
        },
    )
    expected = hashlib.sha256(
        canonical_json_bytes(
            {
                "schema": "personal-operator.phase-evidence.v1",
                "planSha256": journal.plan.digest(),
                "phase": "router-cron-cs",
                "records": [
                    {
                        "stepId": first_phase_outcome.retained_evidence.step_id,
                        "subject": first_phase_outcome.retained_evidence.subject,
                        "operationSha256": (
                            first_phase_outcome.retained_evidence.release_operation_sha256
                        ),
                        "observerEvidenceSha256": (
                            first_phase_outcome.retained_evidence.observer_evidence_sha256
                        ),
                    },
                    {
                        "stepId": step.step_id,
                        "subject": step.subject,
                        "operationSha256": (
                            journal.current.uncertain_operation_sha256
                        ),
                        "observerEvidenceSha256": provider.digest(),
                    },
                ],
            }
        )
    ).hexdigest()

    outcome = journal.outcome_composer().compose(
        transaction=journal.current,
        provider_observation=provider,
    )
    assert outcome.step_observation is not None
    assert outcome.step_observation.router_cron_changesets_sha256 == expected
    completed = journal.reconcile_step(outcome=outcome)
    assert completed.router_cron_changesets_sha256 == expected
