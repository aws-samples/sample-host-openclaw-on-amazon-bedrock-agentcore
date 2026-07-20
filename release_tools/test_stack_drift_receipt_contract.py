from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

import release_tools.contracts as contracts
from release_tools.contracts import (
    ContractError,
    FoundationRuntimeInputsV1,
    ReleasePlanV2,
    ReleaseStepV2,
    ReleaseStepObservationV2,
    RetainedStepEvidenceV2,
    ResolvedMutationRequestV2,
    StagingTransactionV2,
    _completed_prefix_sha256,
    _release_operation_sha256,
    _release_outcome_operation_sha256,
    canonical_json_bytes,
    parse_release_contract,
)
from release_tools.test_contracts import (
    ACCOUNT,
    COMMIT,
    REGION,
    V2_PHASES,
    _foundation_runtime_inputs_v1,
    _mutation_request_v2,
    _release_plan_v2,
    _staging_transaction_v2,
)


EVIDENCE_STORE_SHA256 = "7" * 64
JOURNAL_PATH_SHA256 = "8" * 64
JOURNAL_EXECUTION_ID = "9" * 64
DRIFT_DETECTION_ID = "12345678-1234-4abc-8def-123456789abc"


def _stack_id(stack_name: str, marker: str = "1") -> str:
    return (
        f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{stack_name}/"
        f"00000000-0000-0000-0000-{marker * 12}"
    )


def _plan() -> ReleasePlanV2:
    return ReleasePlanV2.from_mapping(_release_plan_v2())


def _transaction(
    plan: ReleasePlanV2,
    *,
    ordinal: int = 2,
    uncertain: bool = True,
    predecessor_evidence: RetainedStepEvidenceV2 | None = None,
) -> StagingTransactionV2:
    if (
        predecessor_evidence is None
        and plan.steps[ordinal].kind == "STACK_DRIFT_CHECK"
    ):
        predecessor_evidence = _predecessor_evidence(
            plan, drift_ordinal=ordinal
        )
    phase_ends = {
        phase: max(step.ordinal for step in plan.steps if step.phase == phase) + 1
        for phase in V2_PHASES
    }
    last_stable_state = "PREFLIGHTED"
    for phase in V2_PHASES:
        if ordinal >= phase_ends[phase]:
            last_stable_state = contracts.RELEASE_V2_PHASE_STATES[phase]
    value = _staging_transaction_v2(
        plan,
        completed_step_count=ordinal,
        state="UNCERTAIN" if uncertain else last_stable_state,
        last_stable_state=last_stable_state,
    )
    if value["foundationInputsSha256"]:
        foundation_value = _foundation_runtime_inputs_v1()
        foundation_value.update(
            sourceCommit=plan.source_commit,
            sourceTree=plan.source_tree,
            account=plan.account,
            region=plan.region,
            releasePlanSha256=plan.digest(),
            derivationVersion=plan.derivation_version,
        )
        value["foundationInputsSha256"] = FoundationRuntimeInputsV1.from_mapping(
            foundation_value
        ).digest()
    if predecessor_evidence is not None:
        assert ordinal >= 1
        value["completedSteps"][-1]["evidenceSha256"] = (
            predecessor_evidence.digest()
        )
    value["revision"] = ordinal + 2
    if uncertain:
        step = plan.steps[ordinal]
        prefix = _completed_prefix_sha256(value["completedSteps"])
        value["uncertainStepId"] = step.step_id
        value["uncertainOperationSha256"] = _release_operation_sha256(
            plan.digest(), step, prefix
        )
    return StagingTransactionV2.from_mapping(value, plan=plan)


def _resolved_mapping(
    plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
    *,
    predecessor_observer_sha256: str | None = None,
) -> dict[str, object]:
    ordinal = transaction.completed_step_count
    step = plan.steps[ordinal]
    artifact = next(
        item for item in plan.artifacts if item.path == step.request_artifact
    )
    is_drift = step.kind == "STACK_DRIFT_CHECK"
    stack_name = (
        step.subject.split(":stack:", 1)[1].split(":release:", 1)[0]
        if is_drift
        else ""
    )
    if is_drift and predecessor_observer_sha256 is None:
        predecessor = _predecessor_evidence(plan, drift_ordinal=ordinal)
        assert predecessor.digest() == transaction.completed_steps[-1].evidence_sha256
        predecessor_observer_sha256 = predecessor.observer_evidence_sha256
    foundation_mapping: dict[str, object] = {}
    if transaction.foundation_inputs_sha256:
        foundation_mapping = _foundation_runtime_inputs_v1()
        foundation_mapping.update(
            sourceCommit=plan.source_commit,
            sourceTree=plan.source_tree,
            account=plan.account,
            region=plan.region,
            releasePlanSha256=plan.digest(),
            derivationVersion=plan.derivation_version,
        )
    mutation_request = _mutation_request_v2(plan, ordinal)
    mutation_request.update(
        completedPrefixSha256=_completed_prefix_sha256(
            [item.to_mapping() for item in transaction.completed_steps]
        ),
        operationSha256=(
            transaction.uncertain_operation_sha256
            or _release_operation_sha256(
                plan.digest(),
                step,
                _completed_prefix_sha256(
                    [item.to_mapping() for item in transaction.completed_steps]
                ),
            )
        ),
    )
    return {
        "schema": ResolvedMutationRequestV2.SCHEMA,
        "mutationRequest": mutation_request,
        "sourceCommit": plan.source_commit,
        "sourceTree": plan.source_tree,
        "account": plan.account,
        "region": plan.region,
        "stepPhase": step.phase,
        "requestArtifactSize": artifact.size,
        "expectedTemplateSha256": step.expected_template_sha256,
        "expectedTemplateParameterSha256": (
            step.expected_template_parameter_sha256
        ),
        "expectedObservedRequestSha256": step.expected_observed_request_sha256,
        "expectedContentSha256": step.expected_content_sha256,
        "foundationRuntimeInputs": foundation_mapping,
        "agentCoreStackId": transaction.agent_core_stack_id,
        "runtimeImageDigest": transaction.runtime_image_digest,
        "runtimeId": transaction.runtime_id,
        "runtimeVersion": transaction.runtime_version,
        "runtimeArn": transaction.runtime_arn,
        "runtimeEndpointId": transaction.runtime_endpoint_id,
        "runtimeContextSha256": transaction.runtime_context_sha256,
        "routerTargetStackId": transaction.router_target_stack_id,
        "routerChangeSetId": transaction.router_change_set_id,
        "cronTargetStackId": transaction.cron_target_stack_id,
        "cronChangeSetId": transaction.cron_change_set_id,
        "routerCronChangesetsSha256": (
            transaction.router_cron_changesets_sha256
        ),
        "routerCronApplicationSha256": (
            transaction.router_cron_application_sha256
        ),
        "schedulerTargetStackId": transaction.scheduler_target_stack_id,
        "schedulerChangeSetId": transaction.scheduler_change_set_id,
        "schedulerChangesetSha256": transaction.scheduler_changeset_sha256,
        "schedulerApplicationSha256": (
            transaction.scheduler_application_sha256
        ),
        "webTargetStackId": transaction.web_target_stack_id,
        "webChangeSetId": transaction.web_change_set_id,
        "webChangesetSha256": transaction.web_changeset_sha256,
        "webApplicationSha256": transaction.web_application_sha256,
        "predecessorStackId": _stack_id(stack_name) if is_drift else "",
        "predecessorEvidenceSha256": (
            transaction.completed_steps[-1].evidence_sha256 if is_drift else ""
        ),
        "predecessorObserverEvidenceSha256": (
            predecessor_observer_sha256 if is_drift else ""
        ),
    }


def _predecessor_observer_shape(step: ReleaseStepV2) -> tuple[str, str, str]:
    kind = step.kind
    phase = step.phase
    if kind in {"BOOTSTRAP_STACK", "STACK_CREATE", "CHANGESET_EXECUTE"}:
        return "cloudformation", "describe_stacks", "stackId"
    if kind == "STACK_UPDATE" and phase == "runtime":
        return (
            "bedrock-agentcore-control",
            "get_agent_runtime",
            "agentCoreStackId",
        )
    if kind == "STACK_UPDATE" and phase == "endpoint":
        return (
            "bedrock-agentcore-control",
            "get_agent_runtime_endpoint",
            "agentCoreStackId",
        )
    raise AssertionError("test drift predecessor is not a supported stack write")


def _step_observation(
    *,
    plan: ReleasePlanV2,
    step: ReleaseStepV2,
    observer_sha256: str,
) -> ReleaseStepObservationV2:
    return ReleaseStepObservationV2.from_mapping(
        {
            "schema": ReleaseStepObservationV2.SCHEMA,
            "planSha256": plan.digest(),
            "stepId": step.step_id,
            "subject": step.subject,
            "observerEvidenceSha256": observer_sha256,
            "foundationRuntimeInputs": {},
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
    )


def _predecessor_evidence(
    plan: ReleasePlanV2,
    *,
    drift_ordinal: int = 2,
    stack_id: str | None = None,
    service: str | None = None,
    operation: str | None = None,
    projection: dict[str, object] | None = None,
    disposition: str = "PRESENT",
    evidence_store_sha256: str = EVIDENCE_STORE_SHA256,
    journal_path_sha256: str = JOURNAL_PATH_SHA256,
    journal_execution_id: str = JOURNAL_EXECUTION_ID,
) -> RetainedStepEvidenceV2:
    predecessor = plan.steps[drift_ordinal - 1]
    expected_service, expected_operation, projection_field = (
        _predecessor_observer_shape(predecessor)
    )
    stack_name = predecessor.subject.split(":stack:", 1)[1].split(
        ":release:", 1
    )[0]
    actual_stack_id = stack_id or _stack_id(stack_name)
    observer = {
        "schema": RetainedStepEvidenceV2.OBSERVER_SCHEMA,
        "service": service or expected_service,
        "operation": operation or expected_operation,
        "subject": predecessor.subject,
        "disposition": disposition,
        "providerStatus": "TEST_PRESENT",
        "projection": (
            projection
            if projection is not None
            else {projection_field: actual_stack_id}
        ),
    }
    observer_sha256 = hashlib.sha256(canonical_json_bytes(observer)).hexdigest()
    completed_before = _staging_transaction_v2(
        plan,
        completed_step_count=drift_ordinal - 1,
    )["completedSteps"]
    prefix_sha256 = _completed_prefix_sha256(completed_before)
    release_operation_sha256 = _release_operation_sha256(
        plan.digest(), predecessor, prefix_sha256
    )
    journal_revision = drift_ordinal + 1
    step_observation = (
        _step_observation(
            plan=plan,
            step=predecessor,
            observer_sha256=observer_sha256,
        )
        if disposition == "PRESENT"
        else None
    )
    return RetainedStepEvidenceV2.from_mapping(
        {
            "schema": RetainedStepEvidenceV2.SCHEMA,
            "planSha256": plan.digest(),
            "completedPrefixSha256": prefix_sha256,
            "evidenceStoreSha256": evidence_store_sha256,
            "journalPathSha256": journal_path_sha256,
            "journalExecutionId": journal_execution_id,
            "journalRevision": journal_revision,
            "stepId": predecessor.step_id,
            "subject": predecessor.subject,
            "operationSha256": _release_outcome_operation_sha256(
                release_operation_sha256=release_operation_sha256,
                journal_path_sha256=journal_path_sha256,
                journal_execution_id=journal_execution_id,
                journal_revision=journal_revision,
            ),
            "releaseOperationSha256": release_operation_sha256,
            "disposition": disposition,
            "observerEvidenceSha256": observer_sha256,
            "observerEvidence": observer,
            "stepObservationSha256": (
                step_observation.digest() if step_observation else ""
            ),
            "stepObservation": (
                step_observation.to_mapping() if step_observation else {}
            ),
            "failureObservationSha256": "",
            "failureObservation": {},
        }
    )


def _receipt_mapping(
    plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
    resolved: ResolvedMutationRequestV2,
) -> dict[str, object]:
    step = plan.steps[transaction.completed_step_count]
    return {
        "schema": "personal-operator.stack-drift-dispatch-receipt.v1",
        "releasePlanSha256": plan.digest(),
        "evidenceStoreSha256": EVIDENCE_STORE_SHA256,
        "journalPathSha256": JOURNAL_PATH_SHA256,
        "journalExecutionId": JOURNAL_EXECUTION_ID,
        "journalRevision": transaction.revision,
        "completedPrefixSha256": _completed_prefix_sha256(
            [item.to_mapping() for item in transaction.completed_steps]
        ),
        "stepId": step.step_id,
        "subject": step.subject,
        "releaseOperationSha256": transaction.uncertain_operation_sha256,
        "stackId": resolved.predecessor_stack_id,
        "predecessorEvidenceSha256": resolved.predecessor_evidence_sha256,
        "predecessorObserverEvidenceSha256": (
            resolved.predecessor_observer_evidence_sha256
        ),
        "driftDetectionId": DRIFT_DETECTION_ID,
    }


def _record_stack_id(record: RetainedStepEvidenceV2) -> str:
    projection = record.observer_evidence_mapping()["projection"]
    for field in ("stackId", "agentCoreStackId"):
        value = projection.get(field)
        if isinstance(value, str):
            return value
    raise AssertionError("test predecessor record lacks a projected stack ID")


def _bound_receipt_fixture(
    plan: ReleasePlanV2,
    predecessor: RetainedStepEvidenceV2,
    *,
    drift_ordinal: int = 2,
    resolved_stack_id: str | None = None,
) -> tuple[StagingTransactionV2, ResolvedMutationRequestV2, object]:
    transaction = _transaction(
        plan,
        ordinal=drift_ordinal,
        predecessor_evidence=predecessor,
    )
    resolved_mapping = _resolved_mapping(
        plan,
        transaction,
        predecessor_observer_sha256=predecessor.observer_evidence_sha256,
    )
    resolved_mapping["predecessorStackId"] = (
        resolved_stack_id or _record_stack_id(predecessor)
    )
    resolved = ResolvedMutationRequestV2.from_mapping(resolved_mapping)
    receipt = contracts.StackDriftDispatchReceiptV1.from_mapping(
        _receipt_mapping(plan, transaction, resolved)
    )
    return transaction, resolved, receipt


def _validate_receipt(
    receipt: object,
    plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
    resolved: ResolvedMutationRequestV2,
    *,
    predecessor_evidence: RetainedStepEvidenceV2 | None = None,
) -> None:
    predecessor_evidence = predecessor_evidence or _predecessor_evidence(
        plan, drift_ordinal=transaction.completed_step_count
    )
    receipt.validate_transaction(  # type: ignore[attr-defined]
        plan,
        transaction,
        resolved_request=resolved,
        predecessor_evidence=predecessor_evidence,
        evidence_store_sha256=EVIDENCE_STORE_SHA256,
        journal_path_sha256=JOURNAL_PATH_SHA256,
        journal_execution_id=JOURNAL_EXECUTION_ID,
    )


def test_resolved_drift_request_binds_exact_predecessor_authority() -> None:
    plan = _plan()
    transaction = _transaction(plan)
    expected = _resolved_mapping(plan, transaction)

    resolved = ResolvedMutationRequestV2.from_mapping(expected)

    assert resolved.to_mapping() == expected
    assert ResolvedMutationRequestV2.from_bytes(resolved.to_bytes()) == resolved
    assert parse_release_contract(resolved.to_bytes()) == resolved
    resolved.validate_transaction(plan, transaction)
    assert resolved.predecessor_stack_id == _stack_id("CDKToolkit")
    assert (
        resolved.predecessor_evidence_sha256
        == transaction.completed_steps[-1].evidence_sha256
    )
    assert (
        resolved.predecessor_observer_evidence_sha256
        == _predecessor_evidence(plan).observer_evidence_sha256
    )


def test_stack_drift_dispatch_receipt_round_trips_and_validates() -> None:
    plan = _plan()
    transaction = _transaction(plan)
    resolved = ResolvedMutationRequestV2.from_mapping(
        _resolved_mapping(plan, transaction)
    )
    receipt_type = contracts.StackDriftDispatchReceiptV1
    expected = _receipt_mapping(plan, transaction, resolved)

    receipt = receipt_type.from_mapping(expected)

    assert receipt.to_mapping() == expected
    assert receipt_type.from_bytes(receipt.to_bytes()) == receipt
    assert receipt.digest() == hashlib.sha256(receipt.to_bytes()).hexdigest()
    assert parse_release_contract(receipt.to_bytes()) == receipt
    _validate_receipt(receipt, plan, transaction, resolved)


@pytest.mark.parametrize(
    "field",
    (
        "predecessorStackId",
        "predecessorEvidenceSha256",
        "predecessorObserverEvidenceSha256",
    ),
)
def test_resolved_request_requires_every_predecessor_field_even_when_empty(
    field: str,
) -> None:
    plan = _plan()
    for ordinal in (1, 2):
        transaction = _transaction(plan, ordinal=ordinal)
        candidate = _resolved_mapping(plan, transaction)
        candidate.pop(field)

        with pytest.raises(ContractError, match="fields"):
            ResolvedMutationRequestV2.from_mapping(candidate)


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("predecessorStackId", ""),
        ("predecessorEvidenceSha256", ""),
        ("predecessorObserverEvidenceSha256", ""),
        ("predecessorEvidenceSha256", "sha256:" + "f" * 64),
        ("predecessorObserverEvidenceSha256", "F" * 64),
    ),
)
def test_resolved_drift_request_rejects_absent_or_noncanonical_predecessor(
    field: str,
    invalid: str,
) -> None:
    plan = _plan()
    transaction = _transaction(plan)
    candidate = _resolved_mapping(plan, transaction)
    candidate[field] = invalid

    with pytest.raises(ContractError, match="predecessor"):
        ResolvedMutationRequestV2.from_mapping(candidate)


@pytest.mark.parametrize(
    "hostile_stack_id",
    (
        _stack_id("CDKToolkit").replace(ACCOUNT, "999999999999"),
        _stack_id("CDKToolkit").replace(REGION, "us-east-1"),
        _stack_id("OtherStack"),
        f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/CDKToolkit/",
    ),
)
def test_resolved_drift_request_rejects_cross_subject_stack_id(
    hostile_stack_id: str,
) -> None:
    plan = _plan()
    transaction = _transaction(plan)
    candidate = _resolved_mapping(plan, transaction)
    candidate["predecessorStackId"] = hostile_stack_id

    with pytest.raises(ContractError, match="stack ID"):
        ResolvedMutationRequestV2.from_mapping(candidate)


def test_resolved_nondrift_request_forbids_all_predecessor_authority() -> None:
    plan = _plan()
    transaction = _transaction(plan, ordinal=1)
    expected = _resolved_mapping(plan, transaction)
    resolved = ResolvedMutationRequestV2.from_mapping(expected)

    resolved.validate_transaction(plan, transaction)
    assert resolved.predecessor_stack_id == ""
    assert resolved.predecessor_evidence_sha256 == ""
    assert resolved.predecessor_observer_evidence_sha256 == ""

    for field, forged in (
        ("predecessorStackId", _stack_id("CDKToolkit")),
        ("predecessorEvidenceSha256", "d" * 64),
        ("predecessorObserverEvidenceSha256", "e" * 64),
    ):
        candidate = {**expected, field: forged}
        with pytest.raises(ContractError, match="non-drift"):
            ResolvedMutationRequestV2.from_mapping(candidate)


def test_resolved_drift_request_rejects_forged_cross_step_predecessor() -> None:
    plan = _plan()
    transaction = _transaction(plan)
    resolved = ResolvedMutationRequestV2.from_mapping(
        _resolved_mapping(plan, transaction)
    )
    forged = replace(
        resolved,
        predecessor_evidence_sha256=transaction.completed_steps[0].evidence_sha256,
    )

    with pytest.raises(ContractError, match="predecessor evidence"):
        forged.validate_transaction(plan, transaction)


def _valid_receipt_fixture() -> tuple[
    ReleasePlanV2,
    StagingTransactionV2,
    ResolvedMutationRequestV2,
    object,
]:
    plan = _plan()
    transaction = _transaction(plan)
    resolved = ResolvedMutationRequestV2.from_mapping(
        _resolved_mapping(plan, transaction)
    )
    receipt = contracts.StackDriftDispatchReceiptV1.from_mapping(
        _receipt_mapping(plan, transaction, resolved)
    )
    return plan, transaction, resolved, receipt


@pytest.mark.parametrize(
    "field",
    (
        "schema",
        "releasePlanSha256",
        "evidenceStoreSha256",
        "journalPathSha256",
        "journalExecutionId",
        "journalRevision",
        "completedPrefixSha256",
        "stepId",
        "subject",
        "releaseOperationSha256",
        "stackId",
        "predecessorEvidenceSha256",
        "predecessorObserverEvidenceSha256",
        "driftDetectionId",
    ),
)
def test_stack_drift_receipt_requires_every_exact_field(field: str) -> None:
    plan, transaction, resolved, _ = _valid_receipt_fixture()
    candidate = _receipt_mapping(plan, transaction, resolved)
    candidate.pop(field)

    with pytest.raises(ContractError, match="fields"):
        contracts.StackDriftDispatchReceiptV1.from_mapping(candidate)


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("schema", "personal-operator.stack-drift-dispatch-receipt.v2"),
        ("releasePlanSha256", "sha256:" + "0" * 64),
        ("evidenceStoreSha256", "0" * 63),
        ("journalPathSha256", "F" * 64),
        ("journalExecutionId", ""),
        ("journalRevision", 0),
        ("journalRevision", True),
        ("completedPrefixSha256", "sha256:" + "0" * 64),
        ("stepId", "Wrong_Step"),
        ("subject", "cfn:not-a-drift-subject"),
        ("releaseOperationSha256", "0" * 64),
        ("stackId", ""),
        ("predecessorEvidenceSha256", "sha256:" + "0" * 64),
        ("predecessorObserverEvidenceSha256", "0" * 63),
    ),
)
def test_stack_drift_receipt_rejects_noncanonical_scalar_fields(
    field: str,
    invalid: object,
) -> None:
    plan, transaction, resolved, _ = _valid_receipt_fixture()
    candidate = _receipt_mapping(plan, transaction, resolved)
    candidate[field] = invalid

    with pytest.raises(ContractError):
        contracts.StackDriftDispatchReceiptV1.from_mapping(candidate)


@pytest.mark.parametrize(
    "invalid",
    (
        DRIFT_DETECTION_ID.upper(),
        "{" + DRIFT_DETECTION_ID + "}",
        DRIFT_DETECTION_ID.replace("-", ""),
        DRIFT_DETECTION_ID + "0",
        "not-a-uuid",
    ),
)
def test_stack_drift_receipt_requires_canonical_uuid_detection_id(
    invalid: str,
) -> None:
    plan, transaction, resolved, _ = _valid_receipt_fixture()
    candidate = _receipt_mapping(plan, transaction, resolved)
    candidate["driftDetectionId"] = invalid

    with pytest.raises(ContractError, match="canonical UUID"):
        contracts.StackDriftDispatchReceiptV1.from_mapping(candidate)


@pytest.mark.parametrize(
    "hostile_stack_id",
    (
        _stack_id("CDKToolkit").replace(ACCOUNT, "999999999999"),
        _stack_id("CDKToolkit").replace(REGION, "us-east-1"),
        _stack_id("OtherStack"),
    ),
)
def test_stack_drift_receipt_rejects_account_region_or_name_stack_collision(
    hostile_stack_id: str,
) -> None:
    plan, transaction, resolved, _ = _valid_receipt_fixture()
    candidate = _receipt_mapping(plan, transaction, resolved)
    candidate["stackId"] = hostile_stack_id

    with pytest.raises(ContractError, match="stack ID"):
        contracts.StackDriftDispatchReceiptV1.from_mapping(candidate)


@pytest.mark.parametrize(
    ("subject", "stack_id", "parse_only"),
    (
        (
            f"cfn:999999999999:{REGION}:stack:CDKToolkit:release:{COMMIT}:drift",
            (
                f"arn:aws:cloudformation:{REGION}:999999999999:"
                "stack/CDKToolkit/00000000-0000-0000-0000-000000000001"
            ),
            False,
        ),
        (
            f"cfn:{ACCOUNT}:us-east-1:stack:CDKToolkit:release:{COMMIT}:drift",
            (
                f"arn:aws:cloudformation:us-east-1:{ACCOUNT}:"
                "stack/CDKToolkit/00000000-0000-0000-0000-000000000001"
            ),
            True,
        ),
        (
            f"cfn:{ACCOUNT}:{REGION}:stack:OtherStack:release:{COMMIT}:drift",
            _stack_id("OtherStack"),
            False,
        ),
    ),
)
def test_stack_drift_receipt_never_crosses_subject_account_region_or_name(
    subject: str,
    stack_id: str,
    parse_only: bool,
) -> None:
    plan, transaction, resolved, _ = _valid_receipt_fixture()
    candidate = _receipt_mapping(plan, transaction, resolved)
    candidate.update(subject=subject, stackId=stack_id)
    if parse_only:
        with pytest.raises(ContractError, match="region"):
            contracts.StackDriftDispatchReceiptV1.from_mapping(candidate)
        return
    receipt = contracts.StackDriftDispatchReceiptV1.from_mapping(candidate)
    with pytest.raises(ContractError):
        _validate_receipt(receipt, plan, transaction, resolved)


@pytest.mark.parametrize(
    ("attribute", "forged"),
    (
        ("release_plan_sha256", "0" * 64),
        ("evidence_store_sha256", "0" * 64),
        ("journal_path_sha256", "0" * 64),
        ("journal_execution_id", "0" * 64),
        ("journal_revision", 99),
        ("completed_prefix_sha256", "0" * 64),
        ("step_id", "99-foundation-stack-drift-check"),
        (
            "subject",
            f"cfn:{ACCOUNT}:{REGION}:stack:CDKToolkit:release:{'f' * 40}:drift",
        ),
        ("release_operation_sha256", "sha256:" + "0" * 64),
        ("stack_id", _stack_id("CDKToolkit", "2")),
        ("predecessor_evidence_sha256", "0" * 64),
        ("predecessor_observer_evidence_sha256", "0" * 64),
    ),
)
def test_stack_drift_receipt_rejects_every_transaction_binding_forgery(
    attribute: str,
    forged: object,
) -> None:
    plan, transaction, resolved, receipt = _valid_receipt_fixture()
    hostile = replace(receipt, **{attribute: forged})

    with pytest.raises(ContractError):
        _validate_receipt(hostile, plan, transaction, resolved)


def test_stack_drift_receipt_rejects_cross_step_receipt() -> None:
    plan, transaction, resolved, _ = _valid_receipt_fixture()
    candidate = _receipt_mapping(plan, transaction, resolved)
    other_step = plan.steps[5]
    assert other_step.kind == "STACK_DRIFT_CHECK"
    candidate.update(
        stepId=other_step.step_id,
        subject=other_step.subject,
        stackId=_stack_id("OpenClawVpc"),
    )
    receipt = contracts.StackDriftDispatchReceiptV1.from_mapping(candidate)

    with pytest.raises(ContractError):
        _validate_receipt(receipt, plan, transaction, resolved)


def test_stack_drift_receipt_rejects_stable_or_non_drift_transaction() -> None:
    plan, transaction, resolved, receipt = _valid_receipt_fixture()
    stable = _transaction(plan, uncertain=False)
    stable_resolved = ResolvedMutationRequestV2.from_mapping(
        _resolved_mapping(plan, stable)
    )
    with pytest.raises(ContractError, match="UNCERTAIN"):
        _validate_receipt(receipt, plan, stable, stable_resolved)

    non_drift = _transaction(plan, ordinal=1)
    non_drift_resolved = ResolvedMutationRequestV2.from_mapping(
        _resolved_mapping(plan, non_drift)
    )
    non_drift_step = plan.steps[1]
    candidate = _receipt_mapping(plan, transaction, resolved)
    candidate.update(
        journalRevision=non_drift.revision,
        completedPrefixSha256=_completed_prefix_sha256(
            [item.to_mapping() for item in non_drift.completed_steps]
        ),
        stepId=non_drift_step.step_id,
        subject=f"{non_drift_step.subject}:drift",
        releaseOperationSha256=non_drift.uncertain_operation_sha256,
        stackId=_stack_id("CDKToolkit"),
        predecessorEvidenceSha256=(
            non_drift.completed_steps[-1].evidence_sha256
        ),
    )
    non_drift_receipt = contracts.StackDriftDispatchReceiptV1.from_mapping(
        candidate
    )
    with pytest.raises(ContractError, match="not a drift check"):
        _validate_receipt(
            non_drift_receipt,
            plan,
            non_drift,
            non_drift_resolved,
            predecessor_evidence=_predecessor_evidence(plan),
        )


def test_stack_drift_receipt_cross_checks_caller_and_resolved_authority() -> None:
    plan, transaction, resolved, receipt = _valid_receipt_fixture()
    predecessor = _predecessor_evidence(plan)

    for field, invalid in (
        ("evidence_store_sha256", "0" * 64),
        ("journal_path_sha256", "0" * 64),
        ("journal_execution_id", "0" * 64),
    ):
        kwargs = {
            "resolved_request": resolved,
            "predecessor_evidence": predecessor,
            "evidence_store_sha256": EVIDENCE_STORE_SHA256,
            "journal_path_sha256": JOURNAL_PATH_SHA256,
            "journal_execution_id": JOURNAL_EXECUTION_ID,
        }
        kwargs[field] = invalid
        with pytest.raises(ContractError):
            receipt.validate_transaction(plan, transaction, **kwargs)


def test_stack_drift_receipt_rejects_all_three_agree_predecessor_rebinding() -> None:
    plan = _plan()
    predecessor = _predecessor_evidence(plan)
    transaction = _transaction(plan, predecessor_evidence=predecessor)
    resolved = ResolvedMutationRequestV2.from_mapping(
        _resolved_mapping(
            plan,
            transaction,
            predecessor_observer_sha256=predecessor.observer_evidence_sha256,
        )
    )
    forged_stack_id = _stack_id("CDKToolkit", "2")
    forged_observer_sha256 = "0" * 64
    forged_resolved = replace(
        resolved,
        predecessor_stack_id=forged_stack_id,
        predecessor_observer_evidence_sha256=forged_observer_sha256,
    )
    forged_receipt = contracts.StackDriftDispatchReceiptV1.from_mapping(
        _receipt_mapping(plan, transaction, forged_resolved)
    )

    with pytest.raises(ContractError):
        forged_receipt.validate_transaction(
            plan,
            transaction,
            resolved_request=forged_resolved,
            predecessor_evidence=predecessor,
            evidence_store_sha256=EVIDENCE_STORE_SHA256,
            journal_path_sha256=JOURNAL_PATH_SHA256,
            journal_execution_id=JOURNAL_EXECUTION_ID,
        )


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("evidence_store_sha256", "0" * 64),
        ("journal_path_sha256", "0" * 64),
        ("journal_execution_id", "0" * 64),
    ),
)
def test_stack_drift_receipt_rejects_self_consistent_crossed_record_identity(
    field: str,
    invalid: str,
) -> None:
    plan = _plan()
    kwargs = {field: invalid}
    predecessor = _predecessor_evidence(plan, **kwargs)
    transaction, resolved, receipt = _bound_receipt_fixture(plan, predecessor)

    with pytest.raises(ContractError):
        _validate_receipt(
            receipt,
            plan,
            transaction,
            resolved,
            predecessor_evidence=predecessor,
        )


def test_stack_drift_receipt_rejects_crossed_record_digest_plan_or_step() -> None:
    plan = _plan()
    predecessor = _predecessor_evidence(plan)
    transaction, resolved, receipt = _bound_receipt_fixture(plan, predecessor)
    other = _predecessor_evidence(
        plan,
        stack_id=_stack_id("CDKToolkit", "2"),
    )
    with pytest.raises(ContractError):
        _validate_receipt(
            receipt,
            plan,
            transaction,
            resolved,
            predecessor_evidence=other,
        )

    for crossed in (
        replace(predecessor, plan_sha256="0" * 64),
        replace(predecessor, step_id=plan.steps[0].step_id),
        replace(predecessor, subject=plan.steps[0].subject),
    ):
        crossed_transaction, crossed_resolved, crossed_receipt = (
            _bound_receipt_fixture(plan, crossed)
        )
        with pytest.raises(ContractError):
            _validate_receipt(
                crossed_receipt,
                plan,
                crossed_transaction,
                crossed_resolved,
                predecessor_evidence=crossed,
            )


def test_stack_drift_receipt_requires_present_predecessor_record() -> None:
    plan = _plan()
    predecessor = _predecessor_evidence(plan, disposition="PENDING")
    transaction, resolved, receipt = _bound_receipt_fixture(plan, predecessor)

    with pytest.raises(ContractError, match="PRESENT"):
        _validate_receipt(
            receipt,
            plan,
            transaction,
            resolved,
            predecessor_evidence=predecessor,
        )


@pytest.mark.parametrize(
    ("service", "operation"),
    (
        ("cloudformation", "get_template"),
        ("bedrock-agentcore-control", "get_agent_runtime"),
        ("s3", "head_object"),
    ),
)
def test_stack_drift_receipt_rejects_wrong_predecessor_observer_pair(
    service: str,
    operation: str,
) -> None:
    plan = _plan()
    predecessor = _predecessor_evidence(
        plan,
        service=service,
        operation=operation,
    )
    transaction, resolved, receipt = _bound_receipt_fixture(plan, predecessor)

    with pytest.raises(ContractError, match="service|operation"):
        _validate_receipt(
            receipt,
            plan,
            transaction,
            resolved,
            predecessor_evidence=predecessor,
        )


@pytest.mark.parametrize(
    "projection",
    (
        {},
        {"agentCoreStackId": _stack_id("CDKToolkit")},
        {
            "stackId": _stack_id("CDKToolkit"),
            "agentCoreStackId": _stack_id("CDKToolkit"),
        },
        {"stackId": 7},
        {"stackId": "not-an-arn"},
    ),
)
def test_stack_drift_receipt_rejects_missing_alternate_or_ambiguous_stack_projection(
    projection: dict[str, object],
) -> None:
    plan = _plan()
    predecessor = _predecessor_evidence(plan, projection=projection)
    transaction, resolved, receipt = _bound_receipt_fixture(
        plan,
        predecessor,
        resolved_stack_id=_stack_id("CDKToolkit"),
    )

    with pytest.raises(ContractError, match="projection|stack ID"):
        _validate_receipt(
            receipt,
            plan,
            transaction,
            resolved,
            predecessor_evidence=predecessor,
        )


def test_stack_drift_receipt_accepts_exact_pair_for_all_thirteen_drifts() -> None:
    plan = _plan()
    drift_ordinals = [
        step.ordinal for step in plan.steps if step.kind == "STACK_DRIFT_CHECK"
    ]
    assert len(drift_ordinals) == 13

    for drift_ordinal in drift_ordinals:
        predecessor = _predecessor_evidence(
            plan,
            drift_ordinal=drift_ordinal,
        )
        transaction, resolved, receipt = _bound_receipt_fixture(
            plan,
            predecessor,
            drift_ordinal=drift_ordinal,
        )
        _validate_receipt(
            receipt,
            plan,
            transaction,
            resolved,
            predecessor_evidence=predecessor,
        )
