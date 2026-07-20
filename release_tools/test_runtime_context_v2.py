from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

import release_tools.runtime_context_v2 as runtime_context_module
from release_tools.contracts import (
    FoundationRuntimeInputsV1,
    MAX_CONTRACT_BYTES,
    ReleasePlanV2,
    ReleaseStepObservationV2,
    ResolvedMutationRequestV2,
    RetainedStepEvidenceV2,
    RuntimeContextV3,
    StagingTransactionV2,
    _completed_prefix_sha256,
    _release_operation_sha256,
    _release_outcome_operation_sha256,
    canonical_json_bytes,
)
from release_tools.dispatch_attempt_v2 import (
    DispatchAttemptError,
    FreshDispatchAuthorityV1,
    ReleaseDispatchAttemptV1,
    _mint_fresh_dispatch_authority,
)
from release_tools.runtime_context_v2 import (
    RuntimeContextFileV2,
    RuntimeContextV2Ambiguous,
    RuntimeContextV2Error,
    RuntimeContextWriteRequestV2,
    TrustedRuntimeContextInputsV2,
    derive_trusted_runtime_context_inputs,
)
from release_tools.release_plan_v2 import PreclosedStaticRequestV2
from release_tools.test_contracts import (
    ACCOUNT,
    COMMIT,
    ENDPOINT_ID,
    IMAGE_URI,
    REGION,
    ROLE_ARN,
    RUNTIME_ARN,
    RUNTIME_ID,
    TREE,
    _runtime_configuration,
)
from release_tools.test_transaction import (
    _advance_v2_until_phase,
    _create_v2,
    _plan_v2,
    _plan_v2_with_request_payload,
    _resolved_mutation_request,
)
from release_tools.transaction import ObservationDisposition


_GUARDRAIL_ID = "abcdefghij"
_GUARDRAIL_VERSION = "1"
_ENDPOINT_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:agentEndpoint/"
    "87654321-4321-4321-4321-cba987654321"
)
_WORKLOAD_IDENTITY_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
    "workload-identity-directory/default/workload-identity/"
    "personal_operator_bridge-0123456789"
)


def _guardrailed_runtime_configuration(
    *,
    guardrail_id: str = _GUARDRAIL_ID,
    guardrail_version: str = _GUARDRAIL_VERSION,
) -> dict[str, object]:
    configuration = _runtime_configuration()
    environment = dict(configuration["environmentVariables"])
    environment.update(
        {
            "BEDROCK_GUARDRAIL_ID": guardrail_id,
            "BEDROCK_GUARDRAIL_VERSION": guardrail_version,
        }
    )
    configuration["environmentVariables"] = environment
    return configuration


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
    *,
    guardrail_id: str = _GUARDRAIL_ID,
    guardrail_version: str = _GUARDRAIL_VERSION,
) -> dict[str, object]:
    configuration = _guardrailed_runtime_configuration(
        guardrail_id=guardrail_id,
        guardrail_version=guardrail_version,
    )
    return {
        "agentCoreStackId": (
            f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/"
            "OpenClawAgentCore/00000000-0000-0000-0000-000000000001"
        ),
        "cloudFormationTemplateSha256": "1" * 64,
        "cloudFormationRequestSha256": "2" * 64,
        "runtimeId": RUNTIME_ID,
        "runtimeVersion": "7",
        "runtimeArn": RUNTIME_ARN,
        "runtimeConfiguration": configuration,
        "runtimeConfigurationSha256": _configuration_sha256(configuration),
        "guardrailId": guardrail_id,
        "guardrailVersion": guardrail_version,
        "requiresMMDSV2": True,
        "requiresServiceS3Endpoint": True,
        "endpointId": ENDPOINT_ID,
        "endpointName": f"release_{COMMIT}",
        "endpointArn": _ENDPOINT_ARN,
        "workloadIdentityArn": _WORKLOAD_IDENTITY_ARN,
    }


def _foundation_ordinal(plan: ReleasePlanV2) -> int:
    expected_subject = (
        f"cfn:{plan.account}:{plan.region}:stack:OpenClawObservability:"
        f"release:{plan.source_commit}:drift"
    )
    ordinals = [
        step.ordinal
        for step in plan.steps
        if (step.phase, step.kind, step.subject)
        == ("foundation", "STACK_DRIFT_CHECK", expected_subject)
    ]
    assert len(ordinals) == 1
    return ordinals[0]


def _replace_foundation_inputs(
    record: RetainedStepEvidenceV2,
    foundation: FoundationRuntimeInputsV1 | None,
) -> RetainedStepEvidenceV2:
    assert record.step_observation is not None
    observation_mapping = record.step_observation.to_mapping()
    observation_mapping["foundationRuntimeInputs"] = (
        foundation.to_mapping() if foundation is not None else {}
    )
    observation = ReleaseStepObservationV2.from_mapping(observation_mapping)
    record_mapping = record.to_mapping()
    record_mapping.update(
        {
            "stepObservationSha256": observation.digest(),
            "stepObservation": observation.to_mapping(),
        }
    )
    return RetainedStepEvidenceV2.from_mapping(record_mapping)


def _replace_endpoint_projection(
    record: RetainedStepEvidenceV2,
    projection: dict[str, object],
) -> RetainedStepEvidenceV2:
    assert record.step_observation is not None
    provider_mapping = record.observer_evidence_mapping()
    provider_mapping["projection"] = projection
    provider_digest = hashlib.sha256(
        canonical_json_bytes(provider_mapping)
    ).hexdigest()
    observation = replace(
        record.step_observation,
        observer_evidence_sha256=provider_digest,
    )
    record_mapping = record.to_mapping()
    record_mapping.update(
        {
            "observerEvidenceSha256": provider_digest,
            "observerEvidence": provider_mapping,
            "stepObservationSha256": observation.digest(),
            "stepObservation": observation.to_mapping(),
        }
    )
    return RetainedStepEvidenceV2.from_mapping(record_mapping)


def _rechain_prefix(
    *,
    plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
    records: list[RetainedStepEvidenceV2],
    foundation_inputs_sha256: str | None = None,
) -> tuple[StagingTransactionV2, tuple[RetainedStepEvidenceV2, ...]]:
    completed: list[dict[str, str]] = []
    canonical_records: list[RetainedStepEvidenceV2] = []
    for ordinal, original in enumerate(records):
        step = plan.steps[ordinal]
        prefix = _completed_prefix_sha256(completed)
        release_operation = _release_operation_sha256(
            plan.digest(), step, prefix
        )
        mapping = original.to_mapping()
        mapping.update(
            {
                "completedPrefixSha256": prefix,
                "releaseOperationSha256": release_operation,
                "operationSha256": _release_outcome_operation_sha256(
                    release_operation_sha256=release_operation,
                    journal_path_sha256=original.journal_path_sha256,
                    journal_execution_id=original.journal_execution_id,
                    journal_revision=original.journal_revision,
                ),
            }
        )
        canonical = RetainedStepEvidenceV2.from_mapping(mapping)
        canonical_records.append(canonical)
        completed.append(
            {"stepId": step.step_id, "evidenceSha256": canonical.digest()}
        )
    transaction_mapping = transaction.to_mapping()
    transaction_mapping["completedSteps"] = completed
    if foundation_inputs_sha256 is not None:
        transaction_mapping["foundationInputsSha256"] = (
            foundation_inputs_sha256
        )
    canonical_transaction = StagingTransactionV2.from_mapping(
        transaction_mapping, plan=plan
    )
    return canonical_transaction, tuple(canonical_records)


def _static_context_request(
    *, source_tree: str = TREE,
) -> PreclosedStaticRequestV2:
    return PreclosedStaticRequestV2(
        "RUNTIME_CONTEXT_WRITE",
        COMMIT,
        source_tree,
        ACCOUNT,
        REGION,
        (
            f"release:{ACCOUNT}:{REGION}:{COMMIT}:"
            "artifact:build/runtime-context.json"
        ),
    )


def _plan_with_static_context_request(
    request: PreclosedStaticRequestV2 | None = None,
    *,
    recorded_size: int | None = None,
) -> ReleasePlanV2:
    request = request or _static_context_request()
    base = _plan_v2()
    ordinal = next(
        step.ordinal
        for step in base.steps
        if step.kind == "RUNTIME_CONTEXT_WRITE"
    )
    return _plan_v2_with_request_payload(
        step_ordinal=ordinal,
        payload=request.to_bytes(),
        recorded_size=recorded_size,
    )


@pytest.fixture(scope="module")
def retained_context_prefix(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("runtime-context-prefix")
    journal = _create_v2(root, _plan_with_static_context_request())
    journal.advance_preflight()
    _advance_v2_until_phase(journal, "context")
    assert journal.resume_step()["kind"] == "RUNTIME_CONTEXT_WRITE"
    inventory = {
        record.digest(): record
        for record in journal.evidence_store._all_records(
            plan_sha256=journal.plan.digest()
        )
    }
    records = list(
        inventory[completed.evidence_sha256]
        for completed in journal.current.completed_steps
    )
    endpoint_ordinal = next(
        step.ordinal
        for step in journal.plan.steps
        if step.phase == "endpoint" and step.kind == "STACK_UPDATE"
    )
    drift_ordinal = endpoint_ordinal + 1
    endpoint_step = journal.plan.steps[endpoint_ordinal]
    endpoint = records[endpoint_ordinal]
    provider_mapping = {
        "schema": RetainedStepEvidenceV2.OBSERVER_SCHEMA,
        "service": "bedrock-agentcore-control",
        "operation": "get_agent_runtime_endpoint",
        "subject": endpoint_step.subject,
        "disposition": "PRESENT",
        "providerStatus": "READY",
        "projection": _endpoint_projection(),
    }
    provider_digest = hashlib.sha256(
        canonical_json_bytes(provider_mapping)
    ).hexdigest()
    assert endpoint.step_observation is not None
    endpoint_observation = replace(
        endpoint.step_observation,
        observer_evidence_sha256=provider_digest,
    )
    endpoint_mapping = endpoint.to_mapping()
    endpoint_mapping.update(
        {
            "observerEvidenceSha256": provider_digest,
            "observerEvidence": provider_mapping,
            "stepObservationSha256": endpoint_observation.digest(),
            "stepObservation": endpoint_observation.to_mapping(),
        }
    )
    records[endpoint_ordinal] = RetainedStepEvidenceV2.from_mapping(
        endpoint_mapping
    )
    completed = [item.to_mapping() for item in journal.current.completed_steps]
    completed[endpoint_ordinal]["evidenceSha256"] = records[
        endpoint_ordinal
    ].digest()

    drift = records[drift_ordinal]
    drift_step = journal.plan.steps[drift_ordinal]
    drift_prefix = _completed_prefix_sha256(completed[:drift_ordinal])
    drift_release_operation = _release_operation_sha256(
        journal.plan.digest(), drift_step, drift_prefix
    )
    drift_mapping = drift.to_mapping()
    drift_mapping.update(
        {
            "completedPrefixSha256": drift_prefix,
            "releaseOperationSha256": drift_release_operation,
            "operationSha256": _release_outcome_operation_sha256(
                release_operation_sha256=drift_release_operation,
                journal_path_sha256=drift.journal_path_sha256,
                journal_execution_id=drift.journal_execution_id,
                journal_revision=drift.journal_revision,
            ),
        }
    )
    records[drift_ordinal] = RetainedStepEvidenceV2.from_mapping(drift_mapping)
    completed[drift_ordinal]["evidenceSha256"] = records[drift_ordinal].digest()
    transaction_mapping = journal.current.to_mapping()
    transaction_mapping["completedSteps"] = completed
    transaction = StagingTransactionV2.from_mapping(
        transaction_mapping, plan=journal.plan
    )
    request = RuntimeContextWriteRequestV2.from_plan(journal.plan)
    trusted = derive_trusted_runtime_context_inputs(
        request=request,
        plan=journal.plan,
        transaction=transaction,
        retained_prefix=tuple(records),
    )
    return journal.plan, transaction, tuple(records), request, trusted


def _private_root(tmp_path: Path) -> Path:
    root = tmp_path / "private-release"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    return root


def _target(root: Path) -> Path:
    return root / "build" / "runtime-context.json"


def _resolved_context_request(
    plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
    request: RuntimeContextWriteRequestV2,
) -> ResolvedMutationRequestV2:
    step = plan.steps[transaction.completed_step_count]
    prefix_sha256 = _completed_prefix_sha256(
        [item.to_mapping() for item in transaction.completed_steps]
    )
    operation_sha256 = _release_operation_sha256(
        plan.digest(), step, prefix_sha256
    )
    current_mapping = transaction.to_mapping()
    current_mapping.update(
        {
            "state": "UNCERTAIN",
            "lastStableState": transaction.last_stable_state,
            "uncertainStepId": step.step_id,
            "uncertainOperationSha256": operation_sha256,
            "revision": transaction.revision + 1,
        }
    )
    current = StagingTransactionV2.from_mapping(current_mapping, plan=plan)
    cursor = SimpleNamespace(
        plan=plan,
        current=current,
        completed_prefix_sha256=lambda: prefix_sha256,
        operation_sha256=lambda: operation_sha256,
    )
    resolved = _resolved_mutation_request(
        cursor,
        request_artifact_size=len(request.to_bytes()),
    )
    resolved.validate_transaction(plan, current)
    return resolved


def _fresh_context_authority(
    resolved: ResolvedMutationRequestV2,
    *,
    provider: str = "LOCAL_FILESYSTEM",
    operation_sha256: str | None = None,
    resolved_request_sha256: str | None = None,
) -> FreshDispatchAuthorityV1:
    request = resolved.mutation_request
    attempt = ReleaseDispatchAttemptV1.from_mapping(
        {
            "schema": ReleaseDispatchAttemptV1.SCHEMA,
            "releasePlanSha256": request.plan_sha256,
            "evidenceStoreSha256": "1" * 64,
            "journalPathSha256": "2" * 64,
            "journalExecutionId": "3" * 64,
            "journalRevision": 1,
            "completedPrefixSha256": request.completed_prefix_sha256,
            "stepId": request.step_id,
            "subject": request.subject,
            "operationSha256": operation_sha256 or request.operation_sha256,
            "resolvedRequestSha256": (
                resolved_request_sha256 or resolved.digest()
            ),
            "provider": provider,
        }
    )
    return _mint_fresh_dispatch_authority(attempt)


def _write_context(
    boundary: RuntimeContextFileV2,
    *,
    plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
    request: RuntimeContextWriteRequestV2,
    trusted_inputs: TrustedRuntimeContextInputsV2,
):
    resolved = _resolved_context_request(plan, transaction, request)
    return boundary.write(
        request=request,
        trusted_inputs=trusted_inputs,
        resolved_request=resolved,
        fresh_authority=_fresh_context_authority(resolved),
    )


@pytest.mark.parametrize(
    "mode",
    (
        "missing",
        "duck",
        "crossed-provider",
        "crossed-operation",
        "crossed-resolved",
        "consumed",
    ),
)
def test_writer_requires_exact_fresh_attempt_before_any_filesystem_effect(
    tmp_path: Path,
    retained_context_prefix,
    mode: str,
) -> None:
    plan, transaction, _records, request, trusted = retained_context_prefix
    root = _private_root(tmp_path)
    resolved = _resolved_context_request(plan, transaction, request)
    fresh: object | None
    if mode == "missing":
        fresh = None
    elif mode == "duck":
        fresh = SimpleNamespace(consume=lambda **_kwargs: None)
    elif mode == "crossed-provider":
        fresh = _fresh_context_authority(resolved, provider="S3")
    elif mode == "crossed-operation":
        fresh = _fresh_context_authority(
            resolved, operation_sha256="sha256:" + "0" * 64
        )
    elif mode == "crossed-resolved":
        fresh = _fresh_context_authority(
            resolved, resolved_request_sha256="0" * 64
        )
    else:
        fresh = _fresh_context_authority(resolved)
        fresh.consume(
            provider="LOCAL_FILESYSTEM",
            operation_sha256=resolved.mutation_request.operation_sha256,
            resolved_request_sha256=resolved.digest(),
        )

    with pytest.raises((RuntimeContextV2Error, DispatchAttemptError)):
        RuntimeContextFileV2(root).write(
            request=request,
            trusted_inputs=trusted,
            resolved_request=resolved,
            fresh_authority=fresh,
        )

    assert not _target(root).exists()
    assert not _target(root).parent.exists()


@pytest.mark.parametrize("crossed", ("source", "operation", "artifact"))
def test_writer_rejects_crossed_resolved_request_before_filesystem_effect(
    tmp_path: Path,
    retained_context_prefix,
    crossed: str,
) -> None:
    plan, transaction, _records, request, trusted = retained_context_prefix
    root = _private_root(tmp_path)
    resolved = _resolved_context_request(plan, transaction, request)
    if crossed == "source":
        hostile = replace(resolved, source_tree="d" * 40)
    elif crossed == "operation":
        hostile = replace(
            resolved,
            mutation_request=replace(
                resolved.mutation_request,
                operation_sha256="sha256:" + "0" * 64,
            ),
        )
    else:
        hostile = replace(
            resolved,
            mutation_request=replace(
                resolved.mutation_request,
                request_artifact="requests/crossed-context.json",
            ),
        )

    with pytest.raises(RuntimeContextV2Error, match="resolved mutation"):
        RuntimeContextFileV2(root).write(
            request=request,
            trusted_inputs=trusted,
            resolved_request=hostile,
            fresh_authority=_fresh_context_authority(hostile),
        )

    assert not _target(root).exists()
    assert not _target(root).parent.exists()


def test_request_contains_only_static_plan_identity(retained_context_prefix) -> None:
    plan, _transaction, _records, request, trusted = retained_context_prefix

    assert request.to_mapping() == {
        "schema": "personal-operator.preclosed-static-request.v2",
        "kind": "RUNTIME_CONTEXT_WRITE",
        "sourceCommit": plan.source_commit,
        "sourceTree": plan.source_tree,
        "account": plan.account,
        "region": plan.region,
        "subject": (
            f"release:{plan.account}:{plan.region}:{plan.source_commit}:"
            "artifact:build/runtime-context.json"
        ),
    }
    assert not {
        "planSha256",
        "completedPrefixSha256",
        "runtimeImageDigest",
        "runtimeImageUri",
        "runtimeEndpointName",
        "contextRelativePath",
        "runtimeId",
        "runtimeVersion",
        "runtimeArn",
        "runtimeEndpointId",
        "runtimeConfiguration",
    }.intersection(request.to_mapping())
    with pytest.raises(RuntimeContextV2Error, match="not constructible"):
        TrustedRuntimeContextInputsV2(  # type: ignore[call-arg]
            runtime_id=RUNTIME_ID,
            runtime_version="7",
            runtime_arn=RUNTIME_ARN,
        )
    assert trusted.runtime_context(request).runtime_id == RUNTIME_ID


def test_request_bytes_are_exact_preclosed_static_request_and_round_trip(
    retained_context_prefix,
) -> None:
    _plan, _transaction, _records, request, _trusted = retained_context_prefix
    expected = _static_context_request()

    assert request.to_bytes() == expected.to_bytes()
    assert request.digest() == expected.digest()
    assert RuntimeContextWriteRequestV2.from_bytes(request.to_bytes()) == request


def test_static_request_has_no_plan_hash_fixed_point(
    retained_context_prefix,
) -> None:
    plan, _transaction, _records, request, _trusted = retained_context_prefix
    alternate_mapping = plan.to_mapping()
    alternate_mapping["driverSha256"] = "9" * 64
    alternate = ReleasePlanV2.from_mapping(alternate_mapping)

    assert alternate.digest() != plan.digest()
    assert RuntimeContextWriteRequestV2.from_plan(alternate).digest() == (
        request.digest()
    )


def test_retained_prefix_derives_and_binds_every_context_fact(
    retained_context_prefix,
) -> None:
    plan, transaction, records, request, trusted = retained_context_prefix

    context = trusted.runtime_context(request)

    assert context.to_mapping() == {
        "schema": RuntimeContextV3.SCHEMA,
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
        "runtimeConfiguration": _guardrailed_runtime_configuration(),
        "runtimeConfigurationSha256": _configuration_sha256(
            _guardrailed_runtime_configuration()
        ),
    }
    assert trusted.source_tree == TREE
    assert trusted.plan_sha256 == plan.digest()
    assert trusted.completed_prefix_sha256 == hashlib.sha256(
        runtime_context_module.canonical_json_bytes(
            {
                "schema": "personal-operator.release-completed-prefix.v2",
                "completedSteps": [
                    item.to_mapping() for item in transaction.completed_steps
                ],
            }
        )
    ).hexdigest()
    assert trusted.endpoint_evidence_sha256 in {
        record.digest() for record in records
    }


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("source_tree", "9" * 40, "plan"),
    ],
)
def test_crossed_static_identity_or_path_is_rejected(
    retained_context_prefix,
    field: str,
    value: str,
    match: str,
) -> None:
    plan, transaction, records, request, _trusted = retained_context_prefix
    crossed = replace(request, **{field: value})

    with pytest.raises(RuntimeContextV2Error, match=match):
        derive_trusted_runtime_context_inputs(
            request=crossed,
            plan=plan,
            transaction=transaction,
            retained_prefix=records,
        )


@pytest.mark.parametrize("field", ("account", "subject"))
def test_static_request_parser_rejects_crossed_exact_subject(field: str) -> None:
    value = _static_context_request().to_mapping()
    value[field] = "999999999999" if field == "account" else "release:crossed"

    with pytest.raises(RuntimeContextV2Error, match="not canonical"):
        RuntimeContextWriteRequestV2.from_bytes(canonical_json_bytes(value))


def test_crossed_request_artifact_digest_plan_and_size_are_rejected(
    retained_context_prefix,
) -> None:
    plan, transaction, records, request, _trusted = retained_context_prefix
    crossed = _static_context_request(source_tree="9" * 40)
    crossed_request = RuntimeContextWriteRequestV2.from_bytes(
        crossed.to_bytes()
    )
    crossed_plan = _plan_with_static_context_request(crossed)

    with pytest.raises(RuntimeContextV2Error, match="plan identity"):
        derive_trusted_runtime_context_inputs(
            request=crossed_request,
            plan=crossed_plan,
            transaction=transaction,
            retained_prefix=records,
        )
    with pytest.raises(RuntimeContextV2Error, match="artifact digest"):
        request.validate_plan(crossed_plan)

    wrong_size = _plan_with_static_context_request(
        recorded_size=len(request.to_bytes()) + 1
    )
    with pytest.raises(RuntimeContextV2Error, match="artifact size"):
        request.validate_plan(wrong_size)

    assert request.validate_plan(plan) == plan


def test_crossed_or_incomplete_retained_prefix_is_rejected(
    retained_context_prefix,
) -> None:
    plan, transaction, records, request, _trusted = retained_context_prefix

    with pytest.raises(RuntimeContextV2Error, match="exact completed prefix"):
        derive_trusted_runtime_context_inputs(
            request=request,
            plan=plan,
            transaction=transaction,
            retained_prefix=records[:-1],
        )
    with pytest.raises(RuntimeContextV2Error, match="ordered completed prefix"):
        derive_trusted_runtime_context_inputs(
            request=request,
            plan=plan,
            transaction=transaction,
            retained_prefix=tuple(reversed(records)),
        )


@pytest.mark.parametrize(
    ("guardrail_id", "guardrail_version"),
    [
        ("klmnopqrst", _GUARDRAIL_VERSION),
        (_GUARDRAIL_ID, "2"),
    ],
)
def test_endpoint_and_configuration_cannot_cross_final_foundation_guardrail(
    tmp_path: Path,
    retained_context_prefix,
    guardrail_id: str,
    guardrail_version: str,
) -> None:
    plan, transaction, retained, request, _trusted = retained_context_prefix
    records = list(retained)
    endpoint_ordinal = next(
        step.ordinal
        for step in plan.steps
        if step.phase == "endpoint" and step.kind == "STACK_UPDATE"
    )
    projection = _endpoint_projection(
        guardrail_id=guardrail_id,
        guardrail_version=guardrail_version,
    )
    assert projection["guardrailId"] == (
        projection["runtimeConfiguration"]["environmentVariables"][
            "BEDROCK_GUARDRAIL_ID"
        ]
    )
    assert projection["guardrailVersion"] == (
        projection["runtimeConfiguration"]["environmentVariables"][
            "BEDROCK_GUARDRAIL_VERSION"
        ]
    )
    records[endpoint_ordinal] = _replace_endpoint_projection(
        records[endpoint_ordinal], projection
    )
    crossed_transaction, crossed_records = _rechain_prefix(
        plan=plan,
        transaction=transaction,
        records=records,
    )
    root = _private_root(tmp_path)

    with pytest.raises(RuntimeContextV2Error, match="foundation guardrail"):
        trusted = derive_trusted_runtime_context_inputs(
            request=request,
            plan=plan,
            transaction=crossed_transaction,
            retained_prefix=crossed_records,
        )
        _write_context(
            RuntimeContextFileV2(root),
            plan=plan,
            transaction=crossed_transaction,
            request=request,
            trusted_inputs=trusted,
        )

    assert not _target(root).exists()


def test_endpoint_policy_resource_cannot_masquerade_as_provider_api_arn(
    retained_context_prefix,
) -> None:
    plan, transaction, retained, request, _trusted = retained_context_prefix
    records = list(retained)
    endpoint_ordinal = next(
        step.ordinal
        for step in plan.steps
        if step.phase == "endpoint" and step.kind == "STACK_UPDATE"
    )
    projection = _endpoint_projection()
    projection["endpointArn"] = (
        f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/"
        f"{RUNTIME_ID}/runtime-endpoint/{ENDPOINT_ID}"
    )
    records[endpoint_ordinal] = _replace_endpoint_projection(
        records[endpoint_ordinal], projection
    )
    crossed_transaction, crossed_records = _rechain_prefix(
        plan=plan,
        transaction=transaction,
        records=records,
    )

    with pytest.raises(RuntimeContextV2Error, match="endpoint identity"):
        derive_trusted_runtime_context_inputs(
            request=request,
            plan=plan,
            transaction=crossed_transaction,
            retained_prefix=crossed_records,
        )


def test_endpoint_workload_identity_cannot_cross_the_release_account(
    retained_context_prefix,
) -> None:
    plan, transaction, retained, request, _trusted = retained_context_prefix
    records = list(retained)
    endpoint_ordinal = next(
        step.ordinal
        for step in plan.steps
        if step.phase == "endpoint" and step.kind == "STACK_UPDATE"
    )
    projection = _endpoint_projection()
    projection["workloadIdentityArn"] = _WORKLOAD_IDENTITY_ARN.replace(
        ACCOUNT, "999999999999"
    )
    records[endpoint_ordinal] = _replace_endpoint_projection(
        records[endpoint_ordinal], projection
    )
    crossed_transaction, crossed_records = _rechain_prefix(
        plan=plan,
        transaction=transaction,
        records=records,
    )

    with pytest.raises(RuntimeContextV2Error, match="endpoint identity"):
        derive_trusted_runtime_context_inputs(
            request=request,
            plan=plan,
            transaction=crossed_transaction,
            retained_prefix=crossed_records,
        )


@pytest.mark.parametrize("case", ("missing", "duplicate", "wrong-phase"))
def test_exact_phase_final_foundation_record_is_required(
    retained_context_prefix,
    case: str,
) -> None:
    plan, transaction, retained, request, _trusted = retained_context_prefix
    records = list(retained)
    foundation_ordinal = _foundation_ordinal(plan)
    foundation = records[
        foundation_ordinal
    ].step_observation.foundation_runtime_inputs
    assert foundation is not None
    if case in {"missing", "wrong-phase"}:
        records[foundation_ordinal] = _replace_foundation_inputs(
            records[foundation_ordinal], None
        )
    if case == "duplicate":
        records[0] = _replace_foundation_inputs(records[0], foundation)
    elif case == "wrong-phase":
        image_ordinal = next(
            step.ordinal for step in plan.steps if step.phase == "image"
        )
        records[image_ordinal] = _replace_foundation_inputs(
            records[image_ordinal], foundation
        )
    crossed_transaction, crossed_records = _rechain_prefix(
        plan=plan,
        transaction=transaction,
        records=records,
    )

    with pytest.raises(RuntimeContextV2Error, match="foundation record"):
        derive_trusted_runtime_context_inputs(
            request=request,
            plan=plan,
            transaction=crossed_transaction,
            retained_prefix=crossed_records,
        )


def test_foundation_record_digest_must_match_the_retained_journal(
    retained_context_prefix,
) -> None:
    plan, transaction, retained, request, _trusted = retained_context_prefix
    records = list(retained)
    ordinal = _foundation_ordinal(plan)
    foundation = records[ordinal].step_observation.foundation_runtime_inputs
    assert foundation is not None
    forged = replace(foundation, foundation_snapshot_sha256="4" * 64)
    records[ordinal] = _replace_foundation_inputs(records[ordinal], forged)
    crossed_transaction, crossed_records = _rechain_prefix(
        plan=plan,
        transaction=transaction,
        records=records,
    )

    with pytest.raises(RuntimeContextV2Error, match="foundation.*digest"):
        derive_trusted_runtime_context_inputs(
            request=request,
            plan=plan,
            transaction=crossed_transaction,
            retained_prefix=crossed_records,
        )


def test_foundation_record_agentcore_stack_must_match_the_journal(
    retained_context_prefix,
) -> None:
    plan, transaction, retained, request, _trusted = retained_context_prefix
    records = list(retained)
    ordinal = _foundation_ordinal(plan)
    foundation = records[ordinal].step_observation.foundation_runtime_inputs
    assert foundation is not None
    forged = replace(
        foundation,
        agent_core_stack_id=foundation.agent_core_stack_id[:-1] + "2",
    )
    records[ordinal] = _replace_foundation_inputs(records[ordinal], forged)
    crossed_transaction, crossed_records = _rechain_prefix(
        plan=plan,
        transaction=transaction,
        records=records,
        foundation_inputs_sha256=forged.digest(),
    )

    with pytest.raises(RuntimeContextV2Error, match="foundation AgentCore"):
        derive_trusted_runtime_context_inputs(
            request=request,
            plan=plan,
            transaction=crossed_transaction,
            retained_prefix=crossed_records,
        )


def test_caller_forged_foundation_identity_is_rejected(
    retained_context_prefix,
) -> None:
    plan, transaction, retained, request, _trusted = retained_context_prefix
    records = list(retained)
    ordinal = _foundation_ordinal(plan)
    foundation = records[ordinal].step_observation.foundation_runtime_inputs
    assert foundation is not None
    forged = replace(foundation, source_tree="d" * 40)
    records[ordinal] = _replace_foundation_inputs(records[ordinal], forged)
    crossed_transaction, crossed_records = _rechain_prefix(
        plan=plan,
        transaction=transaction,
        records=records,
        foundation_inputs_sha256=forged.digest(),
    )

    with pytest.raises(RuntimeContextV2Error, match="foundation.*plan"):
        derive_trusted_runtime_context_inputs(
            request=request,
            plan=plan,
            transaction=crossed_transaction,
            retained_prefix=crossed_records,
        )


def test_writer_creates_exact_canonical_read_only_file_and_is_idempotent(
    tmp_path: Path,
    retained_context_prefix,
) -> None:
    plan, transaction, _records, request, trusted = retained_context_prefix
    root = _private_root(tmp_path)
    boundary = RuntimeContextFileV2(root)

    created = _write_context(
        boundary,
        plan=plan,
        transaction=transaction,
        request=request,
        trusted_inputs=trusted,
    )
    present = _write_context(
        boundary,
        plan=plan,
        transaction=transaction,
        request=request,
        trusted_inputs=trusted,
    )
    observed = boundary.observe(request=request, trusted_inputs=trusted)

    target = _target(root)
    details = target.stat()
    assert created.provider == "LOCAL_FILESYSTEM"
    assert present == created
    assert observed.provider_status == "PRESENT"
    assert stat.S_IMODE(details.st_mode) == 0o400
    assert details.st_nlink == 1
    assert target.parent.stat().st_uid == os.geteuid()
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert target.read_bytes() == trusted.runtime_context(request).to_bytes()
    assert not list(target.parent.glob(".*.tmp"))


def test_stable_different_preexisting_context_is_failed_retained(
    tmp_path: Path,
    retained_context_prefix,
) -> None:
    plan, transaction, _records, request, trusted = retained_context_prefix
    root = _private_root(tmp_path)
    boundary = RuntimeContextFileV2(root)
    target = _target(root)
    target.parent.mkdir(mode=0o700)
    different = trusted.runtime_context(request).to_mapping()
    different["runtimeEndpointId"] = "Endpoint-ZYXWVUTSRQ"
    payload = RuntimeContextV3.from_mapping(different).to_bytes()
    target.write_bytes(payload)
    os.chmod(target, 0o400)

    resolved = _resolved_context_request(plan, transaction, request)
    fresh = _fresh_context_authority(resolved)
    attempt = boundary.write(
        request=request,
        trusted_inputs=trusted,
        resolved_request=resolved,
        fresh_authority=fresh,
    )
    result = boundary.observe(request=request, trusted_inputs=trusted)

    assert attempt.provider == "LOCAL_FILESYSTEM"
    with pytest.raises(DispatchAttemptError, match="already consumed"):
        fresh.consume(
            provider="LOCAL_FILESYSTEM",
            operation_sha256=resolved.mutation_request.operation_sha256,
            resolved_request_sha256=resolved.digest(),
        )
    assert result.disposition is ObservationDisposition.FAILED_RETAINED
    assert result.provider_status == "EXISTING_CONTENT_CONFLICT"
    assert target.read_bytes() == payload


def test_concurrent_writers_never_clobber(
    tmp_path: Path,
    retained_context_prefix,
) -> None:
    plan, transaction, _records, request, trusted = retained_context_prefix
    root = _private_root(tmp_path)

    def write_once(_marker: int):
        try:
            return _write_context(
                RuntimeContextFileV2(root),
                plan=plan,
                transaction=transaction,
                request=request,
                trusted_inputs=trusted,
            )
        except RuntimeContextV2Ambiguous:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(write_once, range(16)))

    completed = [item for item in results if item is not None]
    assert completed
    assert all(item.provider == "LOCAL_FILESYSTEM" for item in completed)
    observed = RuntimeContextFileV2(root).observe(
        request=request, trusted_inputs=trusted
    )
    assert observed.disposition is ObservationDisposition.PRESENT
    assert _target(root).read_bytes() == trusted.runtime_context(request).to_bytes()
    assert not list(_target(root).parent.glob(".*.tmp"))


@pytest.mark.parametrize("kind", ("symlink", "hardlink", "fifo", "wrong-mode"))
def test_observer_fails_closed_for_unsafe_existing_targets(
    tmp_path: Path,
    retained_context_prefix,
    kind: str,
) -> None:
    plan, transaction, _records, request, trusted = retained_context_prefix
    root = _private_root(tmp_path)
    target = _target(root)
    target.parent.mkdir(mode=0o700)
    payload = trusted.runtime_context(request).to_bytes()
    source = root / "source.json"
    source.write_bytes(payload)
    os.chmod(source, 0o400)
    if kind == "symlink":
        target.symlink_to(source)
    elif kind == "hardlink":
        os.link(source, target)
    elif kind == "fifo":
        os.mkfifo(target, 0o400)
    else:
        target.write_bytes(payload)
        os.chmod(target, 0o600)

    with pytest.raises(RuntimeContextV2Ambiguous, match=kind.replace("-", " ")):
        RuntimeContextFileV2(root).observe(
            request=request, trusted_inputs=trusted
        )


@pytest.mark.parametrize("kind", ("oversize", "noncanonical"))
def test_observer_fails_closed_for_untrusted_bytes(
    tmp_path: Path,
    retained_context_prefix,
    kind: str,
) -> None:
    plan, transaction, _records, request, trusted = retained_context_prefix
    root = _private_root(tmp_path)
    target = _target(root)
    target.parent.mkdir(mode=0o700)
    target.write_bytes(
        b"x" * (MAX_CONTRACT_BYTES + 1)
        if kind == "oversize"
        else b'{"schema":"personal-operator.runtime-context.v3"}'
    )
    os.chmod(target, 0o400)

    with pytest.raises(RuntimeContextV2Ambiguous, match=kind):
        RuntimeContextFileV2(root).observe(
            request=request, trusted_inputs=trusted
        )


def test_observer_rejects_unstable_two_read_snapshot(
    tmp_path: Path,
    retained_context_prefix,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, transaction, _records, request, trusted = retained_context_prefix
    root = _private_root(tmp_path)
    boundary = RuntimeContextFileV2(root)
    _write_context(
        boundary,
        plan=plan,
        transaction=transaction,
        request=request,
        trusted_inputs=trusted,
    )
    real_read = runtime_context_module._read_candidate_once
    calls = 0

    def unstable(*args, **kwargs):
        nonlocal calls
        snapshot, payload = real_read(*args, **kwargs)
        calls += 1
        if calls == 2:
            snapshot = replace(snapshot, mtime_ns=snapshot.mtime_ns + 1)
        return snapshot, payload

    monkeypatch.setattr(runtime_context_module, "_read_candidate_once", unstable)
    with pytest.raises(RuntimeContextV2Ambiguous, match="unstable"):
        boundary.observe(request=request, trusted_inputs=trusted)


def test_parent_replacement_is_detected_before_publish(
    tmp_path: Path,
    retained_context_prefix,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, transaction, _records, request, trusted = retained_context_prefix
    root = _private_root(tmp_path)
    build = root / "build"

    def replace_parent(stage: str) -> None:
        if stage == "before_publish":
            build.rename(root / "moved-build")
            build.mkdir(mode=0o700)

    monkeypatch.setattr(runtime_context_module, "_fault_hook", replace_parent)
    with pytest.raises(RuntimeContextV2Ambiguous, match="directory.*replaced"):
        _write_context(
            RuntimeContextFileV2(root),
            plan=plan,
            transaction=transaction,
            request=request,
            trusted_inputs=trusted,
        )
    assert not _target(root).exists()
    assert not list((root / "moved-build").glob(".*.tmp"))


@pytest.mark.parametrize("stage", ("after_write", "after_publish"))
def test_crash_boundaries_are_observed_without_replay(
    tmp_path: Path,
    retained_context_prefix,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    plan, transaction, _records, request, trusted = retained_context_prefix
    root = _private_root(tmp_path)

    def crash(candidate: str) -> None:
        if candidate == stage:
            raise RuntimeError("synthetic crash")

    monkeypatch.setattr(runtime_context_module, "_fault_hook", crash)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        _write_context(
            RuntimeContextFileV2(root),
            plan=plan,
            transaction=transaction,
            request=request,
            trusted_inputs=trusted,
        )
    monkeypatch.setattr(runtime_context_module, "_fault_hook", lambda _stage: None)

    recovered = RuntimeContextFileV2(root).observe(
        request=request, trusted_inputs=trusted
    )

    if stage == "after_write":
        assert recovered.disposition is ObservationDisposition.ABSENT
        assert not _target(root).exists()
    else:
        assert recovered.disposition is ObservationDisposition.PRESENT
        assert _target(root).read_bytes() == trusted.runtime_context(request).to_bytes()
    assert not list(root.rglob(".*.tmp"))


def test_absent_observation_and_unsafe_root_are_fail_closed(
    tmp_path: Path,
    retained_context_prefix,
) -> None:
    _plan, _transaction, _records, request, trusted = retained_context_prefix
    root = _private_root(tmp_path)
    result = RuntimeContextFileV2(root).observe(
        request=request, trusted_inputs=trusted
    )
    assert result.disposition is ObservationDisposition.ABSENT
    assert result.provider_status == "NOT_FOUND"

    os.chmod(root, 0o755)
    with pytest.raises(RuntimeContextV2Ambiguous, match="owner-only"):
        RuntimeContextFileV2(root).observe(
            request=request, trusted_inputs=trusted
        )


def test_wrong_owner_candidate_validation_is_fail_closed() -> None:
    details = os.stat(__file__)
    values = list(details)
    values[4] = os.geteuid() + 1
    wrong_owner = os.stat_result(values)

    with pytest.raises(RuntimeContextV2Ambiguous, match="wrong owner"):
        runtime_context_module._validate_candidate_stat(
            wrong_owner,
            expected_uid=os.geteuid(),
        )


def test_runtime_context_slice_has_no_network_or_aws_dependency() -> None:
    source = (Path(__file__).parent / "runtime_context_v2.py").read_text(
        encoding="utf-8"
    )

    assert "boto3" not in source
    assert "subprocess" not in source
    assert "urllib" not in source
    assert "requests" not in source
