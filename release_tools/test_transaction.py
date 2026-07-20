from __future__ import annotations

import hashlib
import os
import struct
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

import release_tools.contracts as contracts
from release_tools.contracts import (
    AbortRetainedEvidenceV2,
    ContractError,
    FailedRetainedEvidenceV2,
    FoundationRuntimeInputsV1,
    MutationRequestV2,
    MAX_CONTRACT_BYTES,
    MAX_PRIVATE_MUTATION_ARTIFACT_BYTES,
    PRIVATE_MUTATION_ENVELOPE_HEADER_BYTES,
    PRIVATE_MUTATION_ENVELOPE_MAGIC,
    PrivateMutationEnvelopeV2,
    ReleasePlanV2,
    ReleaseStepFailureObservationV2,
    ReleaseStepObservationV2,
    ResolvedMutationRequestV2,
    StagingTransactionV1,
    StagingTransactionV2,
    write_new_private_mutation_envelope,
)
from release_tools.test_contracts import (
    _foundation_runtime_inputs_v1,
    _release_plan_v2,
)
from release_tools.transaction import (
    ObservationDisposition,
    TransactionError,
    TransactionJournal,
    TransactionJournalV2,
)


ACCOUNT = "123456789012"
COMMIT = "a" * 40
TREE = "b" * 40
DIGEST = "sha256:" + "c" * 64
OPERATION = "sha256:" + "f" * 64
ROLLBACK = (
    f"rollback:v1:{ACCOUNT}:eu-west-1:{COMMIT}:sha256:" + "d" * 64
)
AGENTCORE_STACK_ID = (
    f"arn:aws:cloudformation:eu-west-1:{ACCOUNT}:stack/OpenClawAgentCore/"
    "00000000-0000-0000-0000-000000000001"
)


def _consumer_stack_id(stack_name: str, marker: int) -> str:
    return (
        f"arn:aws:cloudformation:eu-west-1:{ACCOUNT}:stack/{stack_name}/"
        f"00000000-0000-0000-0000-{marker:012d}"
    )


def _consumer_change_set_id(marker: int) -> str:
    return (
        f"arn:aws:cloudformation:eu-west-1:{ACCOUNT}:changeSet/"
        f"release-{COMMIT}/00000000-0000-0000-0000-{marker:012d}"
    )


def _plan_v2(*, artifact_digest: str = "1" * 64) -> ReleasePlanV2:
    value = deepcopy(_release_plan_v2())
    artifacts = value["artifacts"]
    steps = value["steps"]
    assert isinstance(artifacts, list)
    assert isinstance(steps, list)
    first_step = steps[0]
    first_request = next(
        item for item in artifacts if item["path"] == first_step["requestArtifact"]
    )
    first_request["sha256"] = artifact_digest
    first_step["requestSha256"] = artifact_digest
    first_step["expectedRequestSha256"] = artifact_digest
    return ReleasePlanV2.from_mapping(value)


def _plan_v2_with_request_payload(
    *,
    step_ordinal: int,
    payload: bytes,
    recorded_size: int | None = None,
) -> ReleasePlanV2:
    value = deepcopy(_release_plan_v2())
    artifacts = value["artifacts"]
    steps = value["steps"]
    assert isinstance(artifacts, list)
    assert isinstance(steps, list)
    step = steps[step_ordinal]
    artifact = next(
        item for item in artifacts if item["path"] == step["requestArtifact"]
    )
    digest = hashlib.sha256(payload).hexdigest()
    artifact["size"] = len(payload) if recorded_size is None else recorded_size
    artifact["sha256"] = digest
    step["requestSha256"] = digest
    step["expectedRequestSha256"] = digest
    return ReleasePlanV2.from_mapping(value)


def _phase_evidence(phase: str) -> dict[str, str]:
    return {
        "foundation": {},
        "image": {"runtime_image_digest": DIGEST},
        "runtime": {
            "agent_core_stack_id": AGENTCORE_STACK_ID,
            "runtime_id": "Runtime-ABCDEFGHIJ",
            "runtime_version": "7",
            "runtime_arn": (
                "arn:aws:bedrock-agentcore:eu-west-1:123456789012:agent/"
                "12345678-1234-1234-1234-123456789abc:7"
            ),
        },
        "endpoint": {
            "agent_core_stack_id": AGENTCORE_STACK_ID,
            "runtime_endpoint_id": "Endpoint-ABCDEFGHIJ",
        },
        "context": {"runtime_context_sha256": "5" * 64},
        "router-cron-cs": {"router_cron_changesets_sha256": "6" * 64},
        "router-cron": {"router_cron_application_sha256": "7" * 64},
        "scheduler-cs": {"scheduler_changeset_sha256": "8" * 64},
        "scheduler": {"scheduler_application_sha256": "9" * 64},
        "web-cs": {"web_changeset_sha256": "a" * 64},
        "web": {"web_application_sha256": "b" * 64},
        "verify": {"verification_sha256": "c" * 64},
    }[phase]


def _foundation_inputs(
    plan: ReleasePlanV2 | None = None,
) -> FoundationRuntimeInputsV1:
    plan = plan or _plan_v2()
    value = _foundation_runtime_inputs_v1()
    value.update(
        {
            "sourceCommit": plan.source_commit,
            "sourceTree": plan.source_tree,
            "account": plan.account,
            "region": plan.region,
            "releasePlanSha256": plan.digest(),
            "derivationVersion": plan.derivation_version,
        }
    )
    return FoundationRuntimeInputsV1.from_mapping(value)


_DEFAULT_FOUNDATION = object()
_OBSERVATION_ALIASES = {
    "agent_core_stack_id": "agentCoreStackId",
    "runtime_image_digest": "runtimeImageDigest",
    "runtime_id": "runtimeId",
    "runtime_version": "runtimeVersion",
    "runtime_arn": "runtimeArn",
    "runtime_endpoint_id": "runtimeEndpointId",
    "runtime_context_sha256": "runtimeContextSha256",
    "router_target_stack_id": "routerTargetStackId",
    "router_change_set_id": "routerChangeSetId",
    "cron_target_stack_id": "cronTargetStackId",
    "cron_change_set_id": "cronChangeSetId",
    "router_cron_changesets_sha256": "routerCronChangesetsSha256",
    "router_cron_application_sha256": "routerCronApplicationSha256",
    "scheduler_changeset_sha256": "schedulerChangesetSha256",
    "scheduler_target_stack_id": "schedulerTargetStackId",
    "scheduler_change_set_id": "schedulerChangeSetId",
    "scheduler_application_sha256": "schedulerApplicationSha256",
    "web_changeset_sha256": "webChangesetSha256",
    "web_target_stack_id": "webTargetStackId",
    "web_change_set_id": "webChangeSetId",
    "web_application_sha256": "webApplicationSha256",
    "verification_sha256": "verificationSha256",
}


def _observation(
    journal: TransactionJournalV2,
    *,
    observer_evidence_sha256: str | None = None,
    derived: dict[str, str] | None = None,
    foundation_inputs: FoundationRuntimeInputsV1 | None | object = _DEFAULT_FOUNDATION,
) -> ReleaseStepObservationV2:
    index = journal.current.completed_step_count
    step = journal.plan.steps[index]
    next_phase = (
        journal.plan.steps[index + 1].phase
        if index + 1 < len(journal.plan.steps)
        else None
    )
    boundary = next_phase != step.phase
    owned = (
        _phase_evidence(step.phase)
        if boundary or step.phase == "runtime"
        else {}
    )
    if step.kind == "CHANGESET_CREATE":
        identity = {
            "OpenClawRouter": {
                "router_target_stack_id": _consumer_stack_id(
                    "OpenClawRouter", 1
                ),
                "router_change_set_id": _consumer_change_set_id(1),
            },
            "OpenClawCron": {
                "cron_target_stack_id": _consumer_stack_id("OpenClawCron", 2),
                "cron_change_set_id": _consumer_change_set_id(2),
            },
            "PersonalOperatorScheduler": {
                "scheduler_target_stack_id": _consumer_stack_id(
                    "PersonalOperatorScheduler", 3
                ),
                "scheduler_change_set_id": _consumer_change_set_id(3),
            },
            "PersonalOperatorWeb": {
                "web_target_stack_id": _consumer_stack_id(
                    "PersonalOperatorWeb", 4
                ),
                "web_change_set_id": _consumer_change_set_id(4),
            },
        }
        stack_name = step.subject.split(":stack:", 1)[1].split(":release:", 1)[0]
        owned = {**owned, **identity[stack_name]}
    if derived is not None:
        owned = {**owned, **derived}
    if foundation_inputs is _DEFAULT_FOUNDATION:
        foundation_inputs = (
            _foundation_inputs(journal.plan)
            if step.phase == "foundation" and boundary
            else None
        )
    value: dict[str, object] = {
        "schema": ReleaseStepObservationV2.SCHEMA,
        "planSha256": journal.plan.digest(),
        "stepId": step.step_id,
        "subject": step.subject,
        "observerEvidenceSha256": observer_evidence_sha256
        or hashlib.sha256(f"observer:{step.step_id}".encode()).hexdigest(),
        "foundationRuntimeInputs": (
            foundation_inputs.to_mapping()
            if isinstance(foundation_inputs, FoundationRuntimeInputsV1)
            else {}
        ),
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
        "schedulerChangesetSha256": "",
        "schedulerTargetStackId": "",
        "schedulerChangeSetId": "",
        "schedulerApplicationSha256": "",
        "webTargetStackId": "",
        "webChangeSetId": "",
        "webChangesetSha256": "",
        "webApplicationSha256": "",
        "verificationSha256": "",
    }
    for name, derived_value in owned.items():
        value[_OBSERVATION_ALIASES[name]] = derived_value
    return ReleaseStepObservationV2.from_mapping(value)


def _abort_evidence(
    journal: TransactionJournalV2,
    *,
    stop_reason: str = "RELEASE_STOP_CONDITION",
) -> AbortRetainedEvidenceV2:
    count = journal.current.completed_step_count
    return AbortRetainedEvidenceV2.from_mapping(
        {
            "schema": AbortRetainedEvidenceV2.SCHEMA,
            "planSha256": journal.plan.digest(),
            "completedPrefixSha256": journal.completed_prefix_sha256(),
            "completedStepCount": count,
            "retainedSteps": [
                {"stepId": step.step_id, "subject": step.subject}
                for step in journal.plan.steps[:count]
            ],
            "stableState": journal.current.last_stable_state,
            "stopReason": stop_reason,
        }
    )


def _failed_retained_evidence(
    journal: TransactionJournalV2,
    *,
    failure_reason: str = "CLOUDFORMATION_STACK_FAILED",
    provider: str = "CLOUDFORMATION",
    terminal_status: str = "CREATE_FAILED",
    observer_evidence_sha256: str = "e" * 64,
) -> FailedRetainedEvidenceV2:
    step = journal.plan.steps[journal.current.completed_step_count]
    operation_sha256 = (
        journal.current.uncertain_operation_sha256
        if journal.current.state == "UNCERTAIN"
        else journal.operation_sha256()
    )
    observation = ReleaseStepFailureObservationV2.from_mapping(
        {
            "schema": ReleaseStepFailureObservationV2.SCHEMA,
            "planSha256": journal.plan.digest(),
            "stepId": step.step_id,
            "subject": step.subject,
            "operationSha256": operation_sha256,
            "provider": provider,
            "terminalStatus": terminal_status,
            "failureReason": failure_reason,
            "observerEvidenceSha256": observer_evidence_sha256,
        }
    )
    return FailedRetainedEvidenceV2.from_mapping(
        {
            "schema": FailedRetainedEvidenceV2.SCHEMA,
            "planSha256": journal.plan.digest(),
            "completedPrefixSha256": journal.completed_prefix_sha256(),
            "completedStepCount": journal.current.completed_step_count,
            "stableState": journal.current.last_stable_state,
            "failureObservation": observation.to_mapping(),
        }
    )


def _mutation_request(journal: TransactionJournalV2) -> MutationRequestV2:
    count = journal.current.completed_step_count
    step = journal.plan.steps[count]
    return MutationRequestV2.from_mapping(
        {
            "schema": MutationRequestV2.SCHEMA,
            "transactionId": journal.plan.transaction_id,
            "planSha256": journal.plan.digest(),
            "completedPrefixSha256": journal.completed_prefix_sha256(),
            "stepId": step.step_id,
            "operationSha256": journal.operation_sha256(),
            "kind": step.kind,
            "subject": step.subject,
            "requestArtifact": step.request_artifact,
            "requestSha256": step.request_sha256,
        },
        plan=journal.plan,
        completed_step_count=count,
        completed_prefix_sha256=journal.completed_prefix_sha256(),
    )


def _resolved_mutation_request(
    journal: TransactionJournalV2,
    *,
    request_artifact_size: int,
) -> ResolvedMutationRequestV2:
    current = journal.current
    next_step = journal.plan.steps[current.completed_step_count]
    return ResolvedMutationRequestV2.from_mapping(
        {
            "schema": ResolvedMutationRequestV2.SCHEMA,
            "mutationRequest": _mutation_request(journal).to_mapping(),
            "sourceCommit": journal.plan.source_commit,
            "sourceTree": journal.plan.source_tree,
            "account": journal.plan.account,
            "region": journal.plan.region,
            "stepPhase": next_step.phase,
            "requestArtifactSize": request_artifact_size,
            "expectedTemplateSha256": next_step.expected_template_sha256,
            "expectedTemplateParameterSha256": (
                next_step.expected_template_parameter_sha256
            ),
            "expectedObservedRequestSha256": (
                next_step.expected_observed_request_sha256
            ),
            "expectedContentSha256": next_step.expected_content_sha256,
            "foundationRuntimeInputs": (
                _foundation_inputs(journal.plan).to_mapping()
                if current.foundation_inputs_sha256
                else {}
            ),
            "agentCoreStackId": current.agent_core_stack_id,
            "runtimeImageDigest": current.runtime_image_digest,
            "runtimeId": current.runtime_id,
            "runtimeVersion": current.runtime_version,
            "runtimeArn": current.runtime_arn,
            "runtimeEndpointId": current.runtime_endpoint_id,
            "runtimeContextSha256": current.runtime_context_sha256,
            "routerTargetStackId": current.router_target_stack_id,
            "routerChangeSetId": current.router_change_set_id,
            "cronTargetStackId": current.cron_target_stack_id,
            "cronChangeSetId": current.cron_change_set_id,
            "routerCronChangesetsSha256": current.router_cron_changesets_sha256,
            "routerCronApplicationSha256": current.router_cron_application_sha256,
            "schedulerTargetStackId": current.scheduler_target_stack_id,
            "schedulerChangeSetId": current.scheduler_change_set_id,
            "schedulerChangesetSha256": current.scheduler_changeset_sha256,
            "schedulerApplicationSha256": current.scheduler_application_sha256,
            "webTargetStackId": current.web_target_stack_id,
            "webChangeSetId": current.web_change_set_id,
            "webChangesetSha256": current.web_changeset_sha256,
            "webApplicationSha256": current.web_application_sha256,
        }
    )


def _create(tmp_path: Path) -> TransactionJournal:
    return TransactionJournal.create(
        tmp_path / "release-transaction.json",
        source_commit=COMMIT,
        source_tree=TREE,
        account=ACCOUNT,
        region="eu-west-1",
    )


def _complete_v1(
    journal: TransactionJournal,
    target_state: str,
    evidence: dict[str, str] | None = None,
) -> StagingTransactionV1:
    journal.begin_mutation(
        target_state,
        rollback_reference=ROLLBACK,
        operation_sha256=OPERATION,
    )
    return journal.reconcile(
        persisted=True,
        operation_sha256=OPERATION,
        evidence=evidence,
    )


def test_journal_allows_only_the_legal_linear_next_state(tmp_path: Path) -> None:
    journal = _create(tmp_path)

    assert journal.current.state == "NEW"
    journal.advance_local("PREFLIGHTED")
    assert journal.current.state == "PREFLIGHTED"
    assert journal.resume_target() == "FOUNDATION_READY"

    with pytest.raises(TransactionError, match="next state"):
        journal.advance_local("IMAGE_PUBLISHED")
    with pytest.raises(TransactionError, match="mutation phase"):
        journal.advance_local("FOUNDATION_READY")


def test_mutating_phase_is_journaled_uncertain_before_the_operation(
    tmp_path: Path,
) -> None:
    journal = _create(tmp_path)
    journal.advance_local("PREFLIGHTED")
    journal.begin_mutation(
        "FOUNDATION_READY",
        rollback_reference=ROLLBACK,
        operation_sha256=OPERATION,
    )
    observed = TransactionJournal.load(journal.path).current
    result = journal.reconcile(
        persisted=True,
        operation_sha256=OPERATION,
    )

    assert observed.state == "UNCERTAIN"
    assert observed.last_stable_state == "PREFLIGHTED"
    assert observed.uncertain_phase == "FOUNDATION_READY"
    assert observed.uncertain_operation_sha256 == OPERATION
    assert result.state == "FOUNDATION_READY"
    assert result.uncertain_operation_sha256 == ""
    assert result.rollback_reference == ROLLBACK
    assert result.revision == 3


def test_partial_failure_stays_uncertain_and_blocks_later_phases(tmp_path: Path) -> None:
    journal = _create(tmp_path)
    journal.advance_local("PREFLIGHTED")

    journal.begin_mutation(
        "FOUNDATION_READY",
        rollback_reference=ROLLBACK,
        operation_sha256=OPERATION,
    )

    def fail_after_boundary() -> None:
        raise TimeoutError("provider response was lost")

    with pytest.raises(TimeoutError, match="lost"):
        fail_after_boundary()

    reloaded = TransactionJournal.load(journal.path)
    assert reloaded.current.state == "UNCERTAIN"
    with pytest.raises(TransactionError, match="reconcile"):
        reloaded.resume_target()
    with pytest.raises(TransactionError, match="UNCERTAIN"):
        reloaded.begin_mutation(
            "FOUNDATION_READY",
            rollback_reference=ROLLBACK,
            operation_sha256=OPERATION,
        )

    restored = reloaded.reconcile(
        persisted=False,
        operation_sha256=OPERATION,
    )
    assert restored.state == "PREFLIGHTED"
    assert TransactionJournal.load(journal.path).resume_target() == "FOUNDATION_READY"


def test_reconcile_persisted_requires_exact_phase_evidence(tmp_path: Path) -> None:
    journal = _create(tmp_path)
    journal.advance_local("PREFLIGHTED")
    journal.begin_mutation("FOUNDATION_READY", rollback_reference=ROLLBACK, operation_sha256=OPERATION)
    journal.reconcile(persisted=True, operation_sha256=OPERATION)
    journal.begin_mutation("IMAGE_PUBLISHED", rollback_reference=ROLLBACK, operation_sha256=OPERATION)

    with pytest.raises((ContractError, TransactionError), match="image"):
        journal.reconcile(persisted=True, operation_sha256=OPERATION)

    reconciled = journal.reconcile(
        persisted=True,
        operation_sha256=OPERATION,
        evidence={"runtime_image_digest": DIGEST},
    )
    assert reconciled.state == "IMAGE_PUBLISHED"
    assert reconciled.runtime_image_digest == DIGEST


@pytest.mark.parametrize(
    ("phase", "prior_evidence"),
    [
        ("RUNTIME_READY", {"runtime_image_digest": "sha256:" + "e" * 64}),
        ("ENDPOINT_READY", {"runtime_id": "Runtime-ZZZZZZZZZZ"}),
        ("CONTEXT_WRITTEN", {"runtime_version": "8"}),
        ("CONSUMER_CHANGESETS_READY", {"runtime_context_sha256": "f" * 64}),
    ],
)
def test_reconciliation_cannot_rewrite_evidence_owned_by_prior_phases(
    tmp_path: Path,
    phase: str,
    prior_evidence: dict[str, str],
) -> None:
    journal = _create(tmp_path)
    journal.advance_local("PREFLIGHTED")
    _complete_v1(journal, "FOUNDATION_READY")
    _complete_v1(
        journal,
        "IMAGE_PUBLISHED",
        {"runtime_image_digest": DIGEST},
    )
    if phase in {
        "ENDPOINT_READY",
        "CONTEXT_WRITTEN",
        "CONSUMER_CHANGESETS_READY",
    }:
        _complete_v1(
            journal,
            "RUNTIME_READY",
            {
                "runtime_id": "Runtime-ABCDEFGHIJ",
                "runtime_version": "7",
            },
        )
    if phase in {"CONTEXT_WRITTEN", "CONSUMER_CHANGESETS_READY"}:
        _complete_v1(journal, "ENDPOINT_READY")
    if phase == "CONSUMER_CHANGESETS_READY":
        _complete_v1(
            journal,
            "CONTEXT_WRITTEN",
            {"runtime_context_sha256": "1" * 64},
        )
    journal.begin_mutation(phase, rollback_reference=ROLLBACK, operation_sha256=OPERATION)
    before = journal.current

    with pytest.raises(TransactionError, match="evidence fields"):
        journal.reconcile(
            persisted=True,
            operation_sha256=OPERATION,
            evidence=prior_evidence,
        )

    after = TransactionJournal.load(journal.path).current
    assert after == before


def test_absent_reconciliation_rejects_all_claimed_evidence(tmp_path: Path) -> None:
    journal = _create(tmp_path)
    journal.advance_local("PREFLIGHTED")
    journal.begin_mutation("FOUNDATION_READY", rollback_reference=ROLLBACK, operation_sha256=OPERATION)

    with pytest.raises(TransactionError, match="absent.*evidence"):
        journal.reconcile(
            persisted=False,
            operation_sha256=OPERATION,
            evidence={"runtime_image_digest": DIGEST},
        )

    assert TransactionJournal.load(journal.path).current.state == "UNCERTAIN"


def test_reconciliation_requires_the_exact_recorded_operation_digest(
    tmp_path: Path,
) -> None:
    journal = _create(tmp_path)
    journal.advance_local("PREFLIGHTED")
    journal.begin_mutation(
        "FOUNDATION_READY",
        rollback_reference=ROLLBACK,
        operation_sha256=OPERATION,
    )

    with pytest.raises(TransactionError, match="operation digest"):
        journal.reconcile(
            persisted=False,
            operation_sha256="sha256:" + "0" * 64,
        )

    assert TransactionJournal.load(journal.path).current.state == "UNCERTAIN"


def test_resume_and_rollback_are_bound_to_the_exact_recorded_reference(
    tmp_path: Path,
) -> None:
    journal = _create(tmp_path)
    journal.advance_local("PREFLIGHTED")
    _complete_v1(journal, "FOUNDATION_READY")
    _complete_v1(
        journal,
        "IMAGE_PUBLISHED",
        {"runtime_image_digest": DIGEST},
    )
    _complete_v1(
        journal,
        "RUNTIME_READY",
        {
            "runtime_id": "Runtime-ABCDEFGHIJ",
            "runtime_version": "7",
        },
    )
    _complete_v1(journal, "ENDPOINT_READY")
    _complete_v1(
        journal,
        "CONTEXT_WRITTEN",
        {"runtime_context_sha256": "1" * 64},
    )
    _complete_v1(
        journal,
        "CONSUMER_CHANGESETS_READY",
        {"consumer_changesets_sha256": "2" * 64},
    )
    _complete_v1(
        journal,
        "CONSUMERS_APPLIED",
        {"consumer_application_sha256": "3" * 64},
    )
    _complete_v1(
        journal,
        "VERIFIED",
        {"verification_sha256": "4" * 64},
    )

    assert journal.current.consumer_changesets_sha256 == "2" * 64
    assert journal.current.consumer_application_sha256 == "3" * 64
    assert journal.current.verification_sha256 == "4" * 64

    with pytest.raises(TransactionError, match="does not match"):
        journal.begin_rollback(ROLLBACK[:-1] + "e", operation_sha256=OPERATION)

    journal.begin_rollback(ROLLBACK, operation_sha256=OPERATION)
    rolled_back = journal.reconcile_rollback(
        persisted=True,
        operation_sha256=OPERATION,
    )
    assert rolled_back.state == "ROLLED_BACK"
    assert rolled_back.last_stable_state == "VERIFIED"
    assert journal.resume_target() is None


def test_no_direct_rollback_completion_bypass_exists(tmp_path: Path) -> None:
    journal = _create(tmp_path)
    journal.advance_local("PREFLIGHTED")
    _complete_v1(journal, "FOUNDATION_READY")

    assert not hasattr(journal, "record_rollback")
    assert not hasattr(journal, "run_mutation")


def test_stale_journal_writer_cannot_overwrite_a_newer_revision(tmp_path: Path) -> None:
    first = _create(tmp_path)
    stale = TransactionJournal.load(first.path)

    first.advance_local("PREFLIGHTED")

    with pytest.raises(TransactionError, match="changed concurrently"):
        stale.advance_local("PREFLIGHTED")
    assert TransactionJournal.load(first.path).current.revision == 1


def test_atomic_replace_fsyncs_payload_and_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _create(tmp_path)
    original = journal.path.read_bytes()
    replacement = StagingTransactionV1.from_mapping(
        {
            **journal.current.to_mapping(),
            "state": "PREFLIGHTED",
            "lastStableState": "PREFLIGHTED",
            "revision": 1,
        }
    )
    fsync_calls: list[int] = []
    replace_calls: list[tuple[object, object]] = []
    real_fsync = contracts.os.fsync
    real_replace = contracts.os.replace

    def observed_fsync(descriptor: int) -> None:
        fsync_calls.append(descriptor)
        real_fsync(descriptor)

    def observed_replace(source, target) -> None:
        replace_calls.append((source, target))
        real_replace(source, target)

    monkeypatch.setattr(contracts.os, "fsync", observed_fsync)
    monkeypatch.setattr(contracts.os, "replace", observed_replace)

    contracts.atomic_replace_contract(journal.path, original, replacement)

    assert len(fsync_calls) >= 2
    assert len(replace_calls) == 1
    assert StagingTransactionV1.from_bytes(journal.path.read_bytes()).revision == 1


def test_journal_rejects_symlink_and_noncanonical_state(tmp_path: Path) -> None:
    journal = _create(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(journal.path)

    with pytest.raises(TransactionError, match="regular file"):
        TransactionJournal.load(link)

    journal.path.write_text('{"schema":"x"}\n', encoding="utf-8")
    with pytest.raises((ContractError, TransactionError)):
        TransactionJournal.load(journal.path)


def test_transaction_module_has_no_aws_dependency() -> None:
    source = (Path(__file__).parent / "transaction.py").read_text(encoding="utf-8")

    assert "boto3" not in source
    assert "aws_cdk" not in source
    assert "subprocess" not in source


def _create_v2(tmp_path: Path, plan: ReleasePlanV2 | None = None) -> TransactionJournalV2:
    return TransactionJournalV2.create(
        tmp_path / "release-transaction-v2.json",
        plan=plan or _plan_v2(),
    )


def _advance_v2_until_phase(
    journal: TransactionJournalV2,
    phase: str,
    *,
    evidence_overrides: dict[str, str] | None = None,
    derived_overrides: dict[str, dict[str, str]] | None = None,
) -> None:
    evidence_overrides = evidence_overrides or {}
    derived_overrides = derived_overrides or {}
    while True:
        step = journal.resume_step()
        assert step is not None
        if phase in {step["phase"], f"{step['phase']}:{step['kind']}"}:
            return
        observer_evidence_sha256 = evidence_overrides.get(
            step["id"], hashlib.sha256(f"evidence:{step['id']}".encode()).hexdigest()
        )
        observation = _observation(
            journal,
            observer_evidence_sha256=observer_evidence_sha256,
            derived=derived_overrides.get(step["id"]),
        )
        if step["mutation"]:
            journal.begin_step()
            journal.reconcile_step(
                disposition=ObservationDisposition.PRESENT,
                operation_sha256=journal.current.uncertain_operation_sha256,
                observation=observation,
            )
        else:
            journal.complete_observation(observation=observation)


def test_v2_journal_binds_the_immutable_plan_before_preflight(tmp_path: Path) -> None:
    plan = _plan_v2()
    journal = _create_v2(tmp_path, plan)

    assert journal.current.state == "NEW"
    assert journal.current.plan_sha256 == plan.digest()
    assert journal.current.completed_step_count == 0
    assert journal.current.completed_steps == ()
    assert journal.advance_preflight().state == "PREFLIGHTED"
    assert journal.resume_step()["id"] == plan.to_mapping()["steps"][0]["id"]


def test_v2_journal_reparses_hostile_replaced_plan_before_persistence(
    tmp_path: Path,
) -> None:
    plan = _plan_v2()
    forged = replace(
        plan,
        steps=(
            replace(plan.steps[0], subject="release:hostile:baseline"),
            *plan.steps[1:],
        ),
    )
    path = tmp_path / "forged-plan.json"

    with pytest.raises(TransactionError, match="release plan is invalid"):
        TransactionJournalV2.create(path, plan=forged)

    assert not path.exists()


def test_v2_begin_step_persists_exact_plan_bound_uncertainty(tmp_path: Path) -> None:
    plan = _plan_v2()
    journal = _create_v2(tmp_path, plan)
    journal.advance_preflight()
    journal.complete_observation(
        observation=_observation(journal, observer_evidence_sha256="d" * 64)
    )

    uncertain = journal.begin_step()
    reloaded = TransactionJournalV2.load(journal.path, plan=plan)

    assert uncertain.state == "UNCERTAIN"
    assert reloaded.current == uncertain
    assert uncertain.uncertain_step_id == plan.to_mapping()["steps"][1]["id"]
    assert uncertain.uncertain_operation_sha256 == journal.operation_sha256()
    assert uncertain.uncertain_operation_sha256.startswith("sha256:")


def test_v2_absent_reconciliation_retries_the_same_step_without_advancing(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    journal.complete_observation(
        observation=_observation(journal, observer_evidence_sha256="d" * 64)
    )
    journal.begin_step()
    operation = journal.current.uncertain_operation_sha256

    restored = journal.reconcile_step(
        disposition=ObservationDisposition.ABSENT,
        operation_sha256=operation,
    )

    assert restored.state == "PREFLIGHTED"
    assert restored.completed_step_count == 1
    assert restored.uncertain_step_id == ""
    assert journal.operation_sha256() == operation


def test_v2_pending_reconciliation_remains_uncertain_without_claiming_evidence(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    journal.complete_observation(
        observation=_observation(journal, observer_evidence_sha256="d" * 64)
    )
    journal.begin_step()
    before = journal.current

    pending = journal.reconcile_step(
        disposition=ObservationDisposition.PENDING,
        operation_sha256=before.uncertain_operation_sha256,
    )

    assert pending == before
    assert TransactionJournalV2.load(journal.path, plan=journal.plan).current == before


def test_v2_partial_phase_records_only_an_exact_completed_prefix(
    tmp_path: Path,
) -> None:
    plan = _plan_v2()
    journal = _create_v2(tmp_path, plan)
    journal.advance_preflight()
    baseline_evidence = "d" * 64

    observation = _observation(
        journal, observer_evidence_sha256=baseline_evidence
    )
    partial = journal.complete_observation(observation=observation)

    assert partial.state == "PREFLIGHTED"
    assert partial.last_stable_state == "PREFLIGHTED"
    assert partial.completed_step_count == 1
    assert partial.completed_steps[0].step_id == plan.to_mapping()["steps"][0]["id"]
    assert partial.completed_steps[0].evidence_sha256 == observation.digest()
    assert partial.rollback_baseline_sha256 == observation.digest()
    assert journal.resume_step()["phase"] == "foundation"


def test_v2_phase_boundary_requires_only_its_exact_owned_evidence(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    foundation_steps = [
        step for step in journal.plan.steps if step.phase == "foundation"
    ]
    for step in foundation_steps[:-1]:
        evidence = hashlib.sha256(step.step_id.encode()).hexdigest()
        observation = _observation(
            journal, observer_evidence_sha256=evidence
        )
        if step.mutation:
            journal.begin_step()
            journal.reconcile_step(
                disposition=ObservationDisposition.PRESENT,
                operation_sha256=journal.current.uncertain_operation_sha256,
                observation=observation,
            )
        else:
            journal.complete_observation(observation=observation)
    journal.begin_step()
    operation = journal.current.uncertain_operation_sha256

    with pytest.raises(TransactionError, match="derived values"):
        journal.reconcile_step(
            disposition=ObservationDisposition.PRESENT,
            operation_sha256=operation,
            observation=_observation(
                journal,
                observer_evidence_sha256="e" * 64,
                foundation_inputs=None,
            ),
        )
    with pytest.raises(TransactionError, match="derived values"):
        journal.reconcile_step(
            disposition=ObservationDisposition.PRESENT,
            operation_sha256=operation,
            observation=_observation(
                journal,
                observer_evidence_sha256="e" * 64,
                derived={
                "verification_sha256": "f" * 64,
                },
            ),
        )

    crossed = _foundation_runtime_inputs_v1()
    crossed["sourceCommit"] = "d" * 40
    with pytest.raises(TransactionError, match="identity differs"):
        journal.reconcile_step(
            disposition=ObservationDisposition.PRESENT,
            operation_sha256=operation,
            observation=_observation(
                journal,
                observer_evidence_sha256="e" * 64,
                foundation_inputs=FoundationRuntimeInputsV1.from_mapping(crossed),
            ),
        )

    canonical_observation = _observation(
        journal,
        observer_evidence_sha256="e" * 64,
        foundation_inputs=_foundation_inputs(journal.plan),
    )
    forged_inputs = replace(
        canonical_observation.foundation_runtime_inputs,
        source_tree="d" * 40,
    )
    forged_observation = replace(
        canonical_observation,
        foundation_runtime_inputs=forged_inputs,
    )
    before = journal.current
    with pytest.raises(TransactionError, match="identity differs"):
        journal.reconcile_step(
            disposition=ObservationDisposition.PRESENT,
            operation_sha256=operation,
            observation=forged_observation,
        )
    assert journal.current == before

    complete = journal.reconcile_step(
        disposition=ObservationDisposition.PRESENT,
        operation_sha256=operation,
        observation=_observation(
            journal,
            observer_evidence_sha256="e" * 64,
            foundation_inputs=_foundation_inputs(),
        ),
    )
    assert complete.state == "FOUNDATION_READY"
    assert complete.foundation_inputs_sha256 == _foundation_inputs().digest()


def test_v2_agentcore_hardening_preserves_runtime_identity_and_version_order(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, "runtime:AGENTCORE_HARDEN")
    journal.begin_step()
    operation = journal.current.uncertain_operation_sha256
    before = journal.current
    invalid = (
        (
            {
                "runtime_id": "Runtime-KLMNOPQRST",
                "runtime_version": "7",
                "runtime_arn": (
                    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:agent/"
                    "12345678-1234-1234-1234-123456789abc:7"
                ),
            },
            "runtime ID",
        ),
        (
            {
                "runtime_id": "Runtime-ABCDEFGHIJ",
                "runtime_version": "7",
                "runtime_arn": (
                    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:agent/"
                    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee:7"
                ),
            },
            "ARN base",
        ),
        (
            {
                "runtime_id": "Runtime-ABCDEFGHIJ",
                "runtime_version": "6",
                "runtime_arn": (
                    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:agent/"
                    "12345678-1234-1234-1234-123456789abc:6"
                ),
            },
            "regressed",
        ),
    )
    for derived, match in invalid:
        with pytest.raises(TransactionError, match=match):
            journal.reconcile_step(
                disposition=ObservationDisposition.PRESENT,
                operation_sha256=operation,
                observation=_observation(journal, derived=derived),
            )
        assert journal.current == before

    completed = journal.reconcile_step(
        disposition=ObservationDisposition.PRESENT,
        operation_sha256=operation,
        observation=_observation(
            journal,
            derived={
                "runtime_id": "Runtime-ABCDEFGHIJ",
                "runtime_version": "8",
                "runtime_arn": (
                    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:agent/"
                    "12345678-1234-1234-1234-123456789abc:8"
                ),
            },
        ),
    )
    assert completed.runtime_id == "Runtime-ABCDEFGHIJ"
    assert completed.runtime_version == "8"


def test_v2_agentcore_stack_id_rejects_same_name_replacement(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, "runtime:STACK_UPDATE")

    assert journal.current.agent_core_stack_id == AGENTCORE_STACK_ID
    journal.begin_step()
    hostile = replace(
        _observation(journal),
        agent_core_stack_id=AGENTCORE_STACK_ID[:-1] + "2",
    )

    with pytest.raises(TransactionError, match="AgentCore stack ID"):
        journal.reconcile_step(
            disposition=ObservationDisposition.PRESENT,
            operation_sha256=journal.current.uncertain_operation_sha256,
            observation=hostile,
        )


def test_v2_consumer_execute_binds_exact_observed_ids_not_reused_names(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, "router-cron:CHANGESET_EXECUTE")
    resolved = _resolved_mutation_request(
        journal,
        request_artifact_size=next(
            artifact.size
            for artifact in journal.plan.artifacts
            if artifact.path
            == journal.plan.steps[journal.current.completed_step_count].request_artifact
        ),
    )

    assert resolved.router_target_stack_id == _consumer_stack_id(
        "OpenClawRouter", 1
    )
    assert resolved.router_change_set_id == _consumer_change_set_id(1)
    for hostile in (
        replace(
            resolved,
            router_target_stack_id=_consumer_stack_id("OpenClawRouter", 9),
        ),
        replace(
            resolved,
            router_change_set_id=_consumer_change_set_id(9),
        ),
    ):
        with pytest.raises(ContractError, match="generated inputs differ"):
            hostile.validate_transaction(journal.plan, journal.current)


def test_v2_consumer_identity_changes_every_dependent_operation_prefix(
    tmp_path: Path,
) -> None:
    plan = _plan_v2()
    router_create = next(
        step
        for step in plan.steps
        if step.kind == "CHANGESET_CREATE"
        and "stack:OpenClawRouter:" in step.subject
    )
    baseline = _create_v2(tmp_path / "baseline", plan)
    changed = _create_v2(tmp_path / "changed", plan)
    baseline.advance_preflight()
    changed.advance_preflight()
    _advance_v2_until_phase(baseline, "router-cron:CHANGESET_EXECUTE")
    _advance_v2_until_phase(
        changed,
        "router-cron:CHANGESET_EXECUTE",
        derived_overrides={
            router_create.step_id: {
                "router_target_stack_id": _consumer_stack_id(
                    "OpenClawRouter", 9
                ),
                "router_change_set_id": _consumer_change_set_id(9),
            }
        },
    )

    assert baseline.current.completed_step_count == changed.current.completed_step_count
    assert baseline.completed_prefix_sha256() != changed.completed_prefix_sha256()
    assert baseline.operation_sha256() != changed.operation_sha256()


def test_v2_changeset_observation_ids_are_atomic_exact_and_step_owned(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, "router-cron-cs:CHANGESET_CREATE")
    observation = _observation(journal)
    count = journal.current.completed_step_count

    observation.validate_plan_step(
        journal.plan,
        completed_step_count=count,
        prior_agent_core_stack_id=journal.current.agent_core_stack_id,
        prior_runtime_id=journal.current.runtime_id,
        prior_runtime_version=journal.current.runtime_version,
        prior_runtime_arn=journal.current.runtime_arn,
    )
    hostile = (
        replace(
            observation,
            router_target_stack_id=observation.router_target_stack_id.replace(
                ACCOUNT, "999999999999"
            ),
        ),
        replace(
            observation,
            router_change_set_id=observation.router_change_set_id.replace(
                f"release-{COMMIT}", f"release-{'d' * 40}"
            ),
        ),
        replace(observation, router_change_set_id=""),
        replace(
            observation,
            cron_target_stack_id=_consumer_stack_id("OpenClawCron", 8),
            cron_change_set_id=_consumer_change_set_id(8),
        ),
    )
    for candidate in hostile:
        with pytest.raises(ContractError, match="atomic|derived values|subject"):
            candidate.validate_plan_step(
                journal.plan,
                completed_step_count=count,
                prior_agent_core_stack_id=journal.current.agent_core_stack_id,
                prior_runtime_id=journal.current.runtime_id,
                prior_runtime_version=journal.current.runtime_version,
                prior_runtime_arn=journal.current.runtime_arn,
            )


def test_v2_downstream_operations_bind_the_exact_completed_evidence_prefix(
    tmp_path: Path,
) -> None:
    plan = _plan_v2()
    cases = (
        (
            "runtime:AGENTCORE_HARDEN",
            "runtime",
            0,
            {
                "runtime_id": "Runtime-KLMNOPQRST",
                "runtime_version": "8",
                "runtime_arn": (
                    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:agent/"
                    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee:8"
                ),
            },
        ),
        (
            "endpoint",
            "runtime",
            -1,
            {
                "runtime_id": "Runtime-ABCDEFGHIJ",
                "runtime_version": "8",
                "runtime_arn": (
                    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:agent/"
                    "12345678-1234-1234-1234-123456789abc:8"
                ),
            },
        ),
        (
            "context",
            "endpoint",
            -1,
            {"runtime_endpoint_id": "Endpoint-KLMNOPQRST"},
        ),
        (
            "router-cron",
            "router-cron-cs",
            -1,
            {"router_cron_changesets_sha256": "d" * 64},
        ),
    )
    for case_index, (
        stop_phase,
        changed_phase,
        changed_step_index,
        changed_derived,
    ) in enumerate(cases):
        baseline = TransactionJournalV2.create(
            tmp_path / f"baseline-{case_index}.json", plan=plan
        )
        changed = TransactionJournalV2.create(
            tmp_path / f"changed-{case_index}.json", plan=plan
        )
        baseline.advance_preflight()
        changed.advance_preflight()
        phase_steps = [step for step in plan.steps if step.phase == changed_phase]
        changed_step = phase_steps[changed_step_index]
        _advance_v2_until_phase(baseline, stop_phase)
        _advance_v2_until_phase(
            changed,
            stop_phase,
            derived_overrides={changed_step.step_id: changed_derived},
        )

        assert baseline.current.completed_step_count == changed.current.completed_step_count
        assert baseline.completed_prefix_sha256() != changed.completed_prefix_sha256()
        assert baseline.operation_sha256() != changed.operation_sha256()


def test_v2_journal_operation_binds_exact_dynamic_update_template(
    tmp_path: Path,
) -> None:
    baseline_value = _release_plan_v2()
    changed_value = deepcopy(baseline_value)
    changed_steps = changed_value["steps"]
    assert isinstance(changed_steps, list)
    changed_update = next(
        step
        for step in changed_steps
        if step["phase"] == "runtime" and step["kind"] == "STACK_UPDATE"
    )
    changed_update["expectedTemplateSha256"] = "0" * 64
    baseline = _create_v2(
        tmp_path / "baseline-template",
        ReleasePlanV2.from_mapping(baseline_value),
    )
    changed = _create_v2(
        tmp_path / "changed-template",
        ReleasePlanV2.from_mapping(changed_value),
    )
    baseline.advance_preflight()
    changed.advance_preflight()
    _advance_v2_until_phase(baseline, "runtime:STACK_UPDATE")
    _advance_v2_until_phase(changed, "runtime:STACK_UPDATE")

    assert baseline.plan.digest() != changed.plan.digest()
    assert baseline.current.plan_sha256 != changed.current.plan_sha256
    assert baseline.operation_sha256() != changed.operation_sha256()


def test_v2_resolved_mutation_request_binds_precloud_plan_and_generated_inputs(
    tmp_path: Path,
) -> None:
    prototype = _plan_v2()
    web_ordinal = next(
        step.ordinal for step in prototype.steps if step.phase == "web"
    )
    request_payload = b'{"operation":"apply-web"}\n'
    plan = _plan_v2_with_request_payload(
        step_ordinal=web_ordinal,
        payload=request_payload,
    )
    journal = _create_v2(tmp_path, plan)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, "web")
    resolved = _resolved_mutation_request(
        journal, request_artifact_size=len(request_payload)
    )

    resolved.validate_transaction(journal.plan, journal.current)
    assert (
        resolved.source_commit,
        resolved.source_tree,
        resolved.account,
        resolved.region,
        resolved.step_phase,
    ) == (COMMIT, TREE, ACCOUNT, "eu-west-1", "web")
    assert ResolvedMutationRequestV2.from_bytes(resolved.to_bytes()) == resolved
    original_digest = resolved.digest()
    assert original_digest == hashlib.sha256(resolved.to_bytes()).hexdigest()
    with pytest.raises(ContractError, match="fields"):
        ResolvedMutationRequestV2.from_mapping(
            {**resolved.to_mapping(), "driverRequestSha256": "f" * 64}
        )
    artifact_path = tmp_path / "web-request.json"
    artifact_path.write_bytes(request_payload)
    envelope_path = tmp_path / "web-request.private"
    written = write_new_private_mutation_envelope(
        envelope_path,
        resolved_request=resolved,
        request_artifact_path=artifact_path,
        plan=journal.plan,
        transaction=journal.current,
    )
    artifact_link = tmp_path / "web-request-link.json"
    artifact_link.symlink_to(artifact_path)
    with pytest.raises(ContractError, match="regular file"):
        write_new_private_mutation_envelope(
            tmp_path / "linked-artifact.private",
            resolved_request=resolved,
            request_artifact_path=artifact_link,
            plan=journal.plan,
            transaction=journal.current,
        )
    parsed = PrivateMutationEnvelopeV2.from_path(
        envelope_path, plan=journal.plan, transaction=journal.current
    )

    assert parsed == written
    assert parsed.resolved_request == resolved
    assert parsed.request_artifact_size == len(request_payload)
    assert parsed.request_artifact_sha256 == hashlib.sha256(
        request_payload
    ).hexdigest()
    assert parsed.request_artifact_offset == (
        len(PRIVATE_MUTATION_ENVELOPE_MAGIC)
        + PRIVATE_MUTATION_ENVELOPE_HEADER_BYTES
        + len(resolved.to_bytes())
    )
    assert MAX_PRIVATE_MUTATION_ARTIFACT_BYTES >= 4 * 1024 * 1024 * 1024

    envelope_bytes = envelope_path.read_bytes()
    with pytest.raises(ContractError, match="exact UNCERTAIN intent"):
        with PrivateMutationEnvelopeV2.open_verified(
            envelope_path,
            plan=journal.plan,
            transaction=journal.current,
            scratch_dir=tmp_path / "pre-intent-scratch",
        ):
            raise AssertionError("stable state must not yield dispatch authority")
    assert PrivateMutationEnvelopeV2.from_path(
        envelope_path,
        plan=journal.plan,
        transaction=journal.current,
        scratch_dir=tmp_path / "diagnostic-scratch",
    ) == parsed
    journal.begin_step()
    unsafe_scratch = tmp_path / "unsafe-scratch"
    unsafe_scratch.mkdir(mode=0o755)
    unsafe_scratch.chmod(0o755)
    with pytest.raises(ContractError, match="scratch"):
        with PrivateMutationEnvelopeV2.open_verified(
            envelope_path,
            plan=journal.plan,
            transaction=journal.current,
            scratch_dir=unsafe_scratch,
        ):
            raise AssertionError("unsafe scratch must not yield a capability")
    scratch_dir = tmp_path / "verified-snapshots"
    retained = None
    with PrivateMutationEnvelopeV2.open_verified(
        envelope_path,
        plan=journal.plan,
        transaction=journal.current,
        scratch_dir=scratch_dir,
    ) as verified:
        retained = verified
        assert verified.metadata == parsed
        assert verified.resolved_request == resolved
        assert verified.read_artifact_bytes(limit=len(request_payload)) == (
            request_payload
        )
        with pytest.raises(ContractError, match="read limit"):
            verified.read_artifact_bytes(limit=len(request_payload) - 1)
        assert b"".join(verified.iter_artifact_chunks(chunk_size=7)) == (
            request_payload
        )
        replacement = tmp_path / "replacement.private"
        replacement.write_bytes(b"hostile replacement")
        os.replace(replacement, envelope_path)
        assert verified.read_artifact_bytes(limit=len(request_payload)) == (
            request_payload
        )
        assert list(scratch_dir.iterdir()) == []
    assert retained is not None
    with pytest.raises(ContractError, match="closed"):
        retained.read_artifact_bytes(limit=len(request_payload))
    with pytest.raises(ContractError, match="closed"):
        list(retained.iter_artifact_chunks())
    envelope_path.write_bytes(envelope_bytes)

    for name, invalid in (
        ("substituted", b"x" * len(request_payload)),
        ("truncated", request_payload[:-1]),
        ("appended", request_payload + b"x"),
    ):
        invalid_path = tmp_path / f"{name}.private"
        invalid_path.write_bytes(
            envelope_bytes[: parsed.request_artifact_offset] + invalid
        )
        with pytest.raises(ContractError, match="artifact (?:digest|size)"):
            PrivateMutationEnvelopeV2.from_path(
                invalid_path, plan=journal.plan, transaction=journal.current
            )

    bad_magic = tmp_path / "bad-magic.private"
    bad_magic.write_bytes(b"X" + envelope_bytes[1:])
    with pytest.raises(ContractError, match="magic"):
        PrivateMutationEnvelopeV2.from_path(
            bad_magic, plan=journal.plan, transaction=journal.current
        )

    noncanonical_header = b"{ " + resolved.to_bytes()[1:]
    noncanonical = tmp_path / "noncanonical-header.private"
    noncanonical.write_bytes(
        PRIVATE_MUTATION_ENVELOPE_MAGIC
        + struct.pack(">I", len(noncanonical_header))
        + noncanonical_header
        + request_payload
    )
    with pytest.raises(ContractError, match="canonical"):
        PrivateMutationEnvelopeV2.from_path(
            noncanonical, plan=journal.plan, transaction=journal.current
        )

    symlink = tmp_path / "envelope-link.private"
    symlink.symlink_to(envelope_path)
    with pytest.raises(ContractError, match="regular file"):
        with PrivateMutationEnvelopeV2.open_verified(
            symlink,
            plan=journal.plan,
            transaction=journal.current,
            scratch_dir=tmp_path / "symlink-scratch",
        ):
            raise AssertionError("symlink must never yield a verified capability")

    assert resolved.foundation_runtime_inputs is not None
    candidates = (
        replace(resolved, source_commit="d" * 40),
        replace(resolved, source_tree="d" * 40),
        replace(resolved, account="999999999999"),
            replace(resolved, region="us-east-1"),
            replace(resolved, step_phase="endpoint"),
            replace(resolved, expected_template_sha256="f" * 64),
            replace(resolved, expected_template_parameter_sha256="f" * 64),
        replace(resolved, expected_observed_request_sha256="f" * 64),
        replace(resolved, expected_content_sha256="f" * 64),
        replace(
            resolved,
            mutation_request=replace(
                resolved.mutation_request, subject="release:hostile:subject"
            ),
        ),
        replace(
            resolved,
            foundation_runtime_inputs=replace(
                resolved.foundation_runtime_inputs, source_tree="d" * 40
            ),
        ),
            replace(resolved, runtime_id="Runtime-KLMNOPQRST"),
            replace(resolved, agent_core_stack_id=AGENTCORE_STACK_ID[:-1] + "9"),
            replace(resolved, runtime_context_sha256="f" * 64),
        replace(resolved, router_cron_application_sha256="f" * 64),
        replace(resolved, scheduler_application_sha256="f" * 64),
        replace(resolved, web_changeset_sha256="f" * 64),
    )
    for candidate in candidates:
        assert candidate.digest() != original_digest
        with pytest.raises(ContractError, match="differ|region|subject"):
            candidate.validate_transaction(journal.plan, journal.current)


def test_v2_resolved_mutation_request_binds_exact_next_step_expectations(
    tmp_path: Path,
) -> None:
    plan = _plan_v2()
    journal = _create_v2(tmp_path, plan)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, "foundation:ASSET_PUBLISH")
    next_step = journal.plan.steps[journal.current.completed_step_count]
    assert next_step.kind == "ASSET_PUBLISH"
    base = _resolved_mutation_request(
        journal,
        request_artifact_size=next(
            artifact.size
            for artifact in journal.plan.artifacts
            if artifact.path == next_step.request_artifact
        ),
    ).to_mapping()
    base.update(
        {
            "expectedTemplateParameterSha256": (
                next_step.expected_template_parameter_sha256
            ),
            "expectedObservedRequestSha256": (
                next_step.expected_observed_request_sha256
            ),
            "expectedContentSha256": next_step.expected_content_sha256,
        }
    )

    resolved = ResolvedMutationRequestV2.from_mapping(base)

    resolved.validate_transaction(journal.plan, journal.current)
    assert resolved.expected_template_sha256 == ""
    assert resolved.expected_template_parameter_sha256 == ""
    assert resolved.expected_observed_request_sha256 == ""
    assert resolved.expected_content_sha256 == next_step.expected_content_sha256
    with pytest.raises(ContractError, match="expectations differ"):
        replace(
            resolved,
            expected_content_sha256="f" * 64,
        ).validate_transaction(journal.plan, journal.current)


def test_v2_resolved_runtime_update_binds_exact_plan_template_digest(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, "runtime:STACK_UPDATE")
    next_step = journal.plan.steps[journal.current.completed_step_count]
    artifact = next(
        item
        for item in journal.plan.artifacts
        if item.path == next_step.request_artifact
    )
    resolved = _resolved_mutation_request(
        journal,
        request_artifact_size=artifact.size,
    )

    assert resolved.expected_template_sha256 == next_step.expected_template_sha256
    assert resolved.expected_template_sha256
    assert resolved.expected_template_parameter_sha256 == ""
    assert ResolvedMutationRequestV2.from_bytes(resolved.to_bytes()) == resolved
    resolved.validate_transaction(journal.plan, journal.current)
    with pytest.raises(ContractError, match="expectations differ"):
        replace(
            resolved,
            expected_template_sha256="f" * 64,
        ).validate_transaction(journal.plan, journal.current)


def test_v2_resolved_mutation_request_rejects_mismatched_plan_inventory_size(
    tmp_path: Path,
) -> None:
    request_payload = b'{"operation":"bootstrap"}\n'
    plan = _plan_v2_with_request_payload(
        step_ordinal=1,
        payload=request_payload,
        recorded_size=len(request_payload) + 1,
    )
    journal = _create_v2(tmp_path, plan)
    journal.advance_preflight()
    journal.complete_observation(observation=_observation(journal))
    resolved = _resolved_mutation_request(
        journal, request_artifact_size=len(request_payload)
    )

    with pytest.raises(ContractError, match="artifact size"):
        resolved.validate_transaction(journal.plan, journal.current)


def test_v2_private_envelope_rejects_fifo_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_payload = b'{"operation":"bootstrap"}\n'
    plan = _plan_v2_with_request_payload(step_ordinal=1, payload=request_payload)
    journal = _create_v2(tmp_path, plan)
    journal.advance_preflight()
    journal.complete_observation(observation=_observation(journal))
    resolved = _resolved_mutation_request(
        journal, request_artifact_size=len(request_payload)
    )
    fifo = tmp_path / "request.fifo"
    os.mkfifo(fifo)
    original_open = contracts.os.open

    def require_nonblocking(path: object, flags: int, *args: object) -> int:
        if os.fspath(path) == os.fspath(fifo):
            assert flags & os.O_NONBLOCK
        return original_open(path, flags, *args)

    monkeypatch.setattr(contracts.os, "open", require_nonblocking)
    with pytest.raises(ContractError, match="regular file"):
        with PrivateMutationEnvelopeV2.open_verified(
            fifo,
            plan=journal.plan,
            transaction=journal.current,
            scratch_dir=tmp_path / "scratch-reader",
        ):
            raise AssertionError("FIFO must never yield a verified capability")
    with pytest.raises(ContractError, match="regular file"):
        write_new_private_mutation_envelope(
            tmp_path / "fifo.private",
            resolved_request=resolved,
            request_artifact_path=fifo,
            plan=journal.plan,
            transaction=journal.current,
        )


def test_v2_verified_envelope_rejects_oversize_and_source_snapshot_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_payload = b'{"operation":"bootstrap"}\n'
    plan = _plan_v2_with_request_payload(step_ordinal=1, payload=request_payload)
    journal = _create_v2(tmp_path, plan)
    journal.advance_preflight()
    journal.complete_observation(observation=_observation(journal))
    resolved = _resolved_mutation_request(
        journal, request_artifact_size=len(request_payload)
    )
    artifact_path = tmp_path / "request.json"
    artifact_path.write_bytes(request_payload)
    envelope_path = tmp_path / "request.private"
    write_new_private_mutation_envelope(
        envelope_path,
        resolved_request=resolved,
        request_artifact_path=artifact_path,
        plan=journal.plan,
        transaction=journal.current,
    )
    journal.begin_step()

    original_limit = contracts.MAX_PRIVATE_MUTATION_ARTIFACT_BYTES
    monkeypatch.setattr(
        contracts,
        "MAX_PRIVATE_MUTATION_ARTIFACT_BYTES",
        len(request_payload) - 1,
    )
    with pytest.raises(ContractError, match="exceeds the limit"):
        with PrivateMutationEnvelopeV2.open_verified(
            envelope_path,
            plan=journal.plan,
            transaction=journal.current,
            scratch_dir=tmp_path / "oversize-scratch",
        ):
            raise AssertionError("oversize artifact must not yield a capability")

    monkeypatch.setattr(
        contracts, "MAX_PRIVATE_MUTATION_ARTIFACT_BYTES", original_limit
    )
    monkeypatch.setattr(contracts, "_same_file_snapshot", lambda *_: False)
    with pytest.raises(ContractError, match="changed while reading"):
        with PrivateMutationEnvelopeV2.open_verified(
            envelope_path,
            plan=journal.plan,
            transaction=journal.current,
            scratch_dir=tmp_path / "race-scratch",
        ):
            raise AssertionError("raced source must not yield a capability")
    assert list((tmp_path / "race-scratch").iterdir()) == []


def test_v2_private_mutation_envelope_streams_artifacts_above_json_limit(
    tmp_path: Path,
) -> None:
    request_payload = b"x" * (MAX_CONTRACT_BYTES + 4096)
    plan = _plan_v2_with_request_payload(step_ordinal=1, payload=request_payload)
    journal = _create_v2(tmp_path, plan)
    journal.advance_preflight()
    journal.complete_observation(observation=_observation(journal))
    resolved = _resolved_mutation_request(
        journal, request_artifact_size=len(request_payload)
    )
    artifact_path = tmp_path / "large-request.bin"
    artifact_path.write_bytes(request_payload)
    envelope_path = tmp_path / "large-request.private"

    write_new_private_mutation_envelope(
        envelope_path,
        resolved_request=resolved,
        request_artifact_path=artifact_path,
        plan=journal.plan,
        transaction=journal.current,
    )
    parsed = PrivateMutationEnvelopeV2.from_path(
        envelope_path, plan=journal.plan, transaction=journal.current
    )

    assert parsed.request_artifact_size > MAX_CONTRACT_BYTES
    assert parsed.request_artifact_sha256 == hashlib.sha256(
        request_payload
    ).hexdigest()


@pytest.mark.parametrize("reserved", (b"operationSha256", b"driverRequestSha256"))
def test_v2_private_mutation_envelope_rejects_operation_fields_in_artifact(
    tmp_path: Path,
    reserved: bytes,
) -> None:
    prefix = b"x" * (1024 * 1024 - len(reserved) // 2)
    request_payload = prefix + reserved + b"=hostile"
    plan = _plan_v2_with_request_payload(step_ordinal=1, payload=request_payload)
    journal = _create_v2(tmp_path, plan)
    journal.advance_preflight()
    journal.complete_observation(observation=_observation(journal))
    resolved = _resolved_mutation_request(
        journal, request_artifact_size=len(request_payload)
    )
    artifact_path = tmp_path / f"{reserved.decode()}.bin"
    artifact_path.write_bytes(request_payload)

    with pytest.raises(ContractError, match="reserved operation field"):
        write_new_private_mutation_envelope(
            tmp_path / f"{reserved.decode()}.private",
            resolved_request=resolved,
            request_artifact_path=artifact_path,
            plan=journal.plan,
            transaction=journal.current,
        )

    manual = tmp_path / f"manual-{reserved.decode()}.private"
    header = resolved.to_bytes()
    manual.write_bytes(
        PRIVATE_MUTATION_ENVELOPE_MAGIC
        + struct.pack(">I", len(header))
        + header
        + request_payload
    )
    with pytest.raises(ContractError, match="reserved operation field"):
        PrivateMutationEnvelopeV2.from_path(
            manual, plan=journal.plan, transaction=journal.current
        )


def test_v2_exact_twelve_phase_progression_reaches_verified_without_rollback(
    tmp_path: Path,
) -> None:
    plan = _plan_v2()
    journal = _create_v2(tmp_path, plan)
    journal.advance_preflight()
    steps = plan.to_mapping()["steps"]

    for index, step in enumerate(steps):
        evidence_sha256 = hashlib.sha256(f"evidence:{index}".encode()).hexdigest()
        observation = _observation(
            journal, observer_evidence_sha256=evidence_sha256
        )
        if step["mutation"]:
            journal.begin_step()
            result = journal.reconcile_step(
                disposition=ObservationDisposition.PRESENT,
                operation_sha256=journal.current.uncertain_operation_sha256,
                observation=observation,
            )
        else:
            result = journal.complete_observation(observation=observation)
        assert result.completed_step_count == index + 1

    assert journal.current.state == "VERIFIED"
    assert journal.current.last_stable_state == "VERIFIED"
    assert journal.current.runtime_image_digest == DIGEST
    assert journal.current.verification_sha256 == "c" * 64
    assert journal.resume_step() is None
    assert not hasattr(journal, "begin_rollback")
    assert not hasattr(journal, "run_mutation")


def test_v2_abort_retained_is_auditable_terminal_stable_prefix(
    tmp_path: Path,
) -> None:
    plan = _plan_v2()
    journal = _create_v2(tmp_path, plan)
    journal.advance_preflight()
    journal.complete_observation(
        observation=_observation(journal, observer_evidence_sha256="d" * 64)
    )

    evidence = _abort_evidence(journal)
    aborted = journal.abort_retained(evidence=evidence)
    reloaded = TransactionJournalV2.load(journal.path, plan=plan)

    assert aborted.state == "ABORTED_RETAINED"
    assert aborted.last_stable_state == "PREFLIGHTED"
    assert aborted.completed_step_count == 1
    assert aborted.abort_evidence_sha256 == evidence.digest()
    assert reloaded.current == aborted
    assert journal.resume_step() is None
    with pytest.raises(TransactionError, match="cannot abort"):
        journal.abort_retained(evidence=_abort_evidence(journal))


def test_v2_abort_retained_rejects_new_uncertain_or_invalid_evidence(
    tmp_path: Path,
) -> None:
    plan = _plan_v2()
    fresh = TransactionJournalV2.create(tmp_path / "fresh.json", plan=plan)
    with pytest.raises(TransactionError, match="cannot abort"):
        fresh.abort_retained(evidence=_abort_evidence(fresh))

    uncertain = TransactionJournalV2.create(tmp_path / "uncertain.json", plan=plan)
    uncertain.advance_preflight()
    uncertain.complete_observation(
        observation=_observation(uncertain, observer_evidence_sha256="d" * 64)
    )
    abort_evidence = _abort_evidence(uncertain)
    uncertain.begin_step()
    with pytest.raises(TransactionError, match="reconcile before abort"):
        uncertain.abort_retained(evidence=abort_evidence)

    stable = TransactionJournalV2.create(tmp_path / "stable.json", plan=plan)
    stable.advance_preflight()
    forged = replace(_abort_evidence(stable), plan_sha256="e" * 64)
    with pytest.raises(TransactionError, match="plan differs"):
        stable.abort_retained(evidence=forged)


def test_v2_failed_retained_reconciliation_atomically_aborts_exact_intent(
    tmp_path: Path,
) -> None:
    plan = _plan_v2()
    journal = _create_v2(tmp_path, plan)
    journal.advance_preflight()
    journal.complete_observation(
        observation=_observation(journal, observer_evidence_sha256="d" * 64)
    )
    uncertain = journal.begin_step()
    evidence = _failed_retained_evidence(journal)
    failure_observation_sha256 = evidence.failure_observation.digest()
    assert contracts.parse_release_contract(evidence.to_bytes()) == evidence
    assert (
        contracts.parse_release_contract(evidence.failure_observation.to_bytes())
        == evidence.failure_observation
    )

    retained = journal.reconcile_step(
        disposition=ObservationDisposition.FAILED_RETAINED,
        operation_sha256=uncertain.uncertain_operation_sha256,
        failure_evidence=evidence,
    )
    reloaded = TransactionJournalV2.load(journal.path, plan=plan).current

    assert retained.state == "ABORTED_RETAINED"
    assert retained.last_stable_state == "PREFLIGHTED"
    assert retained.completed_step_count == 1
    assert retained.completed_steps == uncertain.completed_steps
    assert retained.abort_evidence_sha256 == ""
    assert retained.failed_retained_evidence_sha256 == evidence.digest()
    assert retained.failure_observation_sha256 == failure_observation_sha256
    assert retained.failed_step_id == uncertain.uncertain_step_id
    assert retained.failed_subject == plan.steps[1].subject
    assert retained.failed_operation_sha256 == uncertain.uncertain_operation_sha256
    assert retained.failure_reason == "CLOUDFORMATION_STACK_FAILED"
    assert retained.uncertain_step_id == ""
    assert retained.uncertain_operation_sha256 == ""
    assert reloaded == retained
    assert journal.resume_step() is None


def test_v2_failed_retained_requires_typed_exact_failure_and_never_aliases_absent(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    journal.complete_observation(
        observation=_observation(journal, observer_evidence_sha256="d" * 64)
    )
    journal.begin_step()
    before = journal.current
    evidence = _failed_retained_evidence(journal)

    with pytest.raises(TransactionError, match="FAILED_RETAINED.*evidence"):
        journal.reconcile_step(
            disposition=ObservationDisposition.FAILED_RETAINED,
            operation_sha256=before.uncertain_operation_sha256,
        )
    with pytest.raises(TransactionError, match="ABSENT.*failure"):
        journal.reconcile_step(
            disposition=ObservationDisposition.ABSENT,
            operation_sha256=before.uncertain_operation_sha256,
            failure_evidence=evidence,
        )
    with pytest.raises(TransactionError, match="operation"):
        journal.reconcile_step(
            disposition=ObservationDisposition.FAILED_RETAINED,
            operation_sha256="sha256:" + "0" * 64,
            failure_evidence=evidence,
        )
    forged = replace(
        evidence,
        failure_observation=replace(
            evidence.failure_observation,
            subject="cfn:123456789012:eu-west-1:stack:Other:release:" + COMMIT,
        ),
    )
    with pytest.raises(TransactionError, match="subject"):
        journal.reconcile_step(
            disposition=ObservationDisposition.FAILED_RETAINED,
            operation_sha256=before.uncertain_operation_sha256,
            failure_evidence=forged,
        )

    assert TransactionJournalV2.load(journal.path, plan=journal.plan).current == before


@pytest.mark.parametrize(
    ("reason", "status"),
    (
        ("IMAGE_SCAN_FAILED", "SCAN_POLICY_FAILED"),
        ("IMAGE_SIGNING_FAILED", "SIGNATURE_VERIFICATION_FAILED"),
    ),
)
def test_v2_read_only_image_failure_atomically_retains_exact_stable_prefix(
    tmp_path: Path,
    reason: str,
    status: str,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, "image:IMAGE_OBSERVE")
    before = journal.current
    operation_sha256 = journal.operation_sha256()
    evidence = _failed_retained_evidence(
        journal,
        failure_reason=reason,
        provider="ECR",
        terminal_status=status,
    )

    retained = journal.fail_observation_retained(evidence=evidence)
    reloaded = TransactionJournalV2.load(journal.path, plan=journal.plan).current

    assert retained.state == "ABORTED_RETAINED"
    assert retained.last_stable_state == before.last_stable_state
    assert retained.completed_step_count == before.completed_step_count
    assert retained.completed_steps == before.completed_steps
    assert retained.failed_step_id == journal.plan.steps[
        before.completed_step_count
    ].step_id
    assert retained.failed_operation_sha256 == operation_sha256
    assert retained.failure_reason == reason
    assert retained.uncertain_step_id == ""
    assert retained.uncertain_operation_sha256 == ""
    assert reloaded == retained
    assert journal.resume_step() is None


def test_v2_read_only_failure_rejects_mutation_prefix_and_hostile_evidence(
    tmp_path: Path,
) -> None:
    mutation = _create_v2(tmp_path / "mutation")
    mutation.advance_preflight()
    mutation.complete_observation(observation=_observation(mutation))
    mutation_evidence = _failed_retained_evidence(
        mutation,
        failure_reason="CLOUDFORMATION_STACK_FAILED",
        provider="CLOUDFORMATION",
        terminal_status="CREATE_FAILED",
    )
    with pytest.raises(TransactionError, match="read-only"):
        mutation.fail_observation_retained(evidence=mutation_evidence)

    observed = _create_v2(tmp_path / "observed")
    observed.advance_preflight()
    _advance_v2_until_phase(observed, "image:IMAGE_OBSERVE")
    evidence = _failed_retained_evidence(
        observed,
        failure_reason="IMAGE_SCAN_FAILED",
        provider="ECR",
        terminal_status="SCAN_POLICY_FAILED",
    )
    for hostile, match in (
        (replace(evidence, plan_sha256="f" * 64), "plan differs"),
        (
            replace(
                evidence,
                failure_observation=replace(
                    evidence.failure_observation,
                    operation_sha256="sha256:" + "0" * 64,
                ),
            ),
            "operation differs",
        ),
        (
            replace(
                evidence,
                failure_observation=replace(
                    evidence.failure_observation,
                    step_id=observed.plan.steps[0].step_id,
                ),
            ),
            "plan or step differs",
        ),
    ):
        before = observed.current
        with pytest.raises(TransactionError, match=match):
            observed.fail_observation_retained(evidence=hostile)
        assert observed.current == before


@pytest.mark.parametrize(
    "step_selector",
    ("foundation:BASELINE_OBSERVE", "verify:VERIFY"),
)
def test_v2_read_only_failure_is_closed_to_image_observation(
    tmp_path: Path,
    step_selector: str,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, step_selector)
    evidence = _failed_retained_evidence(
        journal,
        failure_reason="IMAGE_SCAN_FAILED",
        provider="ECR",
        terminal_status="SCAN_POLICY_FAILED",
    )
    before = journal.current

    with pytest.raises(TransactionError, match="step kind"):
        journal.fail_observation_retained(evidence=evidence)

    assert journal.current == before


@pytest.mark.parametrize(
    ("step_selector", "reason", "provider", "status"),
    [
        (
            "foundation:BOOTSTRAP_STACK",
            "CLOUDFORMATION_STACK_FAILED",
            "CLOUDFORMATION",
            "CREATE_FAILED",
        ),
        (
            "runtime:STACK_UPDATE",
            "CLOUDFORMATION_STACK_FAILED",
            "CLOUDFORMATION",
            "UPDATE_ROLLBACK_COMPLETE",
        ),
        (
            "router-cron-cs:CHANGESET_CREATE",
            "CLOUDFORMATION_CHANGESET_FAILED",
            "CLOUDFORMATION",
            "FAILED",
        ),
        (
            "router-cron:CHANGESET_EXECUTE",
            "CLOUDFORMATION_STACK_FAILED",
            "CLOUDFORMATION",
            "CREATE_FAILED",
        ),
        (
            "runtime:AGENTCORE_HARDEN",
            "AGENTCORE_UPDATE_FAILED",
            "AGENTCORE",
            "UPDATE_FAILED",
        ),
        (
            "image:IMAGE_PUBLISH",
            "IMAGE_SCAN_FAILED",
            "ECR",
            "SCAN_POLICY_FAILED",
        ),
        (
            "image:IMAGE_PUBLISH",
            "IMAGE_SIGNING_FAILED",
            "ECR",
            "SIGNATURE_VERIFICATION_FAILED",
        ),
        (
            "foundation:ASSET_PUBLISH",
            "ASSET_SUBJECT_CONFLICT",
            "S3",
            "RETAINED_OBJECT_CONFLICT",
        ),
        (
            "image:IMAGE_PUBLISH",
            "IMAGE_SUBJECT_CONFLICT",
            "ECR",
            "IMMUTABLE_SUBJECT_CONFLICT",
        ),
        (
            "image:IMAGE_PUBLISH",
            "IMAGE_PARTIAL_CLOSURE",
            "ECR",
            "RETAINED_PARTIAL_CLOSURE",
        ),
        (
            "context:RUNTIME_CONTEXT_WRITE",
            "RUNTIME_CONTEXT_CONFLICT",
            "LOCAL_FILESYSTEM",
            "EXISTING_CONTENT_CONFLICT",
        ),
        (
            "foundation:BOOTSTRAP_STACK",
            "CF_SUBJECT_CONFLICT",
            "CLOUDFORMATION",
            "CREATE_COMPLETE",
        ),
        (
            "runtime:STACK_UPDATE",
            "CF_SUBJECT_CONFLICT",
            "CLOUDFORMATION",
            "UPDATE_COMPLETE",
        ),
        (
            "router-cron-cs:CHANGESET_CREATE",
            "CF_SUBJECT_CONFLICT",
            "CLOUDFORMATION",
            "CREATE_COMPLETE",
        ),
        (
            "router-cron:CHANGESET_EXECUTE",
            "CF_SUBJECT_CONFLICT",
            "CLOUDFORMATION",
            "CREATE_COMPLETE",
        ),
        (
            "runtime:AGENTCORE_HARDEN",
            "AGENTCORE_SUBJECT_CONFLICT",
            "AGENTCORE",
            "READY",
        ),
    ],
)
def test_v2_failed_retained_reason_matrix_is_closed_by_exact_step_kind(
    tmp_path: Path,
    step_selector: str,
    reason: str,
    provider: str,
    status: str,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, step_selector)
    journal.begin_step()
    evidence = _failed_retained_evidence(
        journal,
        failure_reason=reason,
        provider=provider,
        terminal_status=status,
    )

    result = journal.reconcile_step(
        disposition=ObservationDisposition.FAILED_RETAINED,
        operation_sha256=journal.current.uncertain_operation_sha256,
        failure_evidence=evidence,
    )

    assert result.state == "ABORTED_RETAINED"
    assert result.failure_reason == reason
    assert result.failure_observation_sha256 == evidence.failure_observation.digest()


def test_v2_failed_retained_rejects_reason_for_a_different_step_kind(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    journal.complete_observation(
        observation=_observation(journal, observer_evidence_sha256="d" * 64)
    )
    journal.begin_step()
    evidence = _failed_retained_evidence(
        journal,
        failure_reason="AGENTCORE_UPDATE_FAILED",
        provider="AGENTCORE",
        terminal_status="UPDATE_FAILED",
    )

    with pytest.raises(TransactionError, match="reason.*step kind"):
        journal.reconcile_step(
            disposition=ObservationDisposition.FAILED_RETAINED,
            operation_sha256=journal.current.uncertain_operation_sha256,
            failure_evidence=evidence,
        )


def test_v2_failed_retained_rejects_terminal_status_for_a_different_operation(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, "runtime:STACK_UPDATE")
    journal.begin_step()
    evidence = _failed_retained_evidence(
        journal,
        failure_reason="CLOUDFORMATION_STACK_FAILED",
        provider="CLOUDFORMATION",
        terminal_status="CREATE_FAILED",
    )

    with pytest.raises(TransactionError, match="status.*step kind"):
        journal.reconcile_step(
            disposition=ObservationDisposition.FAILED_RETAINED,
            operation_sha256=journal.current.uncertain_operation_sha256,
            failure_evidence=evidence,
        )


def test_v2_subject_conflict_is_terminal_kind_specific_and_never_retryable(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, "runtime:STACK_UPDATE")
    uncertain = journal.begin_step()
    retained_prefix = uncertain.completed_steps
    retained_baseline = uncertain.rollback_baseline_sha256
    retained_stable_state = uncertain.last_stable_state
    conflict = _failed_retained_evidence(
        journal,
        failure_reason="CF_SUBJECT_CONFLICT",
        provider="CLOUDFORMATION",
        terminal_status="UPDATE_COMPLETE",
    )

    for disposition in (
        ObservationDisposition.ABSENT,
        ObservationDisposition.PENDING,
    ):
        with pytest.raises(TransactionError, match=f"{disposition.value}.*failure"):
            journal.reconcile_step(
                disposition=disposition,
                operation_sha256=journal.current.uncertain_operation_sha256,
                failure_evidence=conflict,
            )
    wrong_status = replace(
        conflict,
        failure_observation=replace(
            conflict.failure_observation,
            terminal_status="CREATE_COMPLETE",
        ),
    )
    with pytest.raises(TransactionError, match="status.*step kind"):
        journal.reconcile_step(
            disposition=ObservationDisposition.FAILED_RETAINED,
            operation_sha256=journal.current.uncertain_operation_sha256,
            failure_evidence=wrong_status,
        )
    wrong_provider = replace(
        conflict,
        failure_observation=replace(
            conflict.failure_observation,
            provider="AGENTCORE",
        ),
    )
    with pytest.raises(TransactionError, match="provider differs"):
        journal.reconcile_step(
            disposition=ObservationDisposition.FAILED_RETAINED,
            operation_sha256=journal.current.uncertain_operation_sha256,
            failure_evidence=wrong_provider,
        )

    retained = journal.reconcile_step(
        disposition=ObservationDisposition.FAILED_RETAINED,
        operation_sha256=journal.current.uncertain_operation_sha256,
        failure_evidence=conflict,
    )
    assert retained.state == "ABORTED_RETAINED"
    assert retained.failure_reason == "CF_SUBJECT_CONFLICT"
    assert retained.completed_steps == retained_prefix
    assert retained.rollback_baseline_sha256 == retained_baseline
    assert retained.last_stable_state == retained_stable_state
    assert retained.uncertain_step_id == ""
    assert retained.uncertain_operation_sha256 == ""


def test_v2_plan_or_artifact_substitution_is_rejected_on_load(
    tmp_path: Path,
) -> None:
    plan = _plan_v2()
    journal = _create_v2(tmp_path, plan)
    journal.advance_preflight()
    changed = _plan_v2(artifact_digest="f" * 64)

    assert changed.digest() != plan.digest()
    with pytest.raises((ContractError, TransactionError), match="plan"):
        TransactionJournalV2.load(journal.path, plan=changed)


def test_v2_nonprefix_or_replayed_completed_step_is_rejected(tmp_path: Path) -> None:
    plan = _plan_v2()
    journal = _create_v2(tmp_path, plan)
    journal.advance_preflight()
    journal.complete_observation(
        observation=_observation(journal, observer_evidence_sha256="d" * 64)
    )
    value = journal.current.to_mapping()
    value["completedSteps"][0]["stepId"] = plan.to_mapping()["steps"][1]["id"]
    journal.path.write_bytes(contracts.canonical_json_bytes(value))

    with pytest.raises((ContractError, TransactionError), match="prefix"):
        TransactionJournalV2.load(journal.path, plan=plan)


def test_v2_reconciliation_requires_the_recorded_operation_and_shape(
    tmp_path: Path,
) -> None:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    journal.complete_observation(
        observation=_observation(journal, observer_evidence_sha256="d" * 64)
    )
    journal.begin_step()
    before = journal.current
    observation = _observation(journal, observer_evidence_sha256="f" * 64)

    with pytest.raises(TransactionError, match="operation digest"):
        journal.reconcile_step(
            disposition=ObservationDisposition.ABSENT,
            operation_sha256="sha256:" + "0" * 64,
        )
    with pytest.raises(TransactionError, match="PRESENT.*observation"):
        journal.reconcile_step(
            disposition=ObservationDisposition.PRESENT,
            operation_sha256=before.uncertain_operation_sha256,
        )
    with pytest.raises(TransactionError, match="ABSENT.*observation"):
        journal.reconcile_step(
            disposition=ObservationDisposition.ABSENT,
            operation_sha256=before.uncertain_operation_sha256,
            observation=observation,
        )

    assert TransactionJournalV2.load(journal.path, plan=journal.plan).current == before


def test_v2_stale_writer_cannot_overwrite_a_newer_revision(tmp_path: Path) -> None:
    plan = _plan_v2()
    current = _create_v2(tmp_path, plan)
    stale = TransactionJournalV2.load(current.path, plan=plan)
    current.advance_preflight()

    with pytest.raises(TransactionError, match="changed concurrently"):
        stale.advance_preflight()


def test_v2_transaction_remains_aws_and_driver_free() -> None:
    source = (Path(__file__).parent / "transaction.py").read_text(encoding="utf-8")

    assert "boto3" not in source
    assert "aws_cdk" not in source
    assert "subprocess" not in source
    assert "--journal" not in source
