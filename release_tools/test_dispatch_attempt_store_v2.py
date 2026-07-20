"""Evidence-store and journal integration for the universal dispatch fence."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from release_tools.contracts import ResolvedMutationRequestV2
from release_tools.dispatch_attempt_v2 import DispatchAttemptError
from release_tools.evidence_store_v2 import EvidenceStoreV2Error
from release_tools.test_transaction import (
    _advance_v2_until_phase,
    _create_v2,
    _resolved_mutation_request,
    _retained_outcome,
)
from release_tools.transaction import (
    ObservationDisposition,
    TransactionJournalV2,
)


def _begin(
    tmp_path: Path,
    target: str = "foundation:ASSET_PUBLISH",
) -> tuple[TransactionJournalV2, ResolvedMutationRequestV2]:
    journal = _create_v2(tmp_path)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, target)
    step = journal.plan.steps[journal.current.completed_step_count]
    size = next(
        artifact.size
        for artifact in journal.plan.artifacts
        if artifact.path == step.request_artifact
    )
    resolved = _resolved_mutation_request(
        journal,
        request_artifact_size=size,
    )
    journal.begin_step()
    resolved.validate_transaction(journal.plan, journal.current)
    return journal, resolved


def _arm(
    journal: TransactionJournalV2,
    resolved: ResolvedMutationRequestV2,
    *,
    provider: str = "S3",
):
    return journal.evidence_store.arm_current_dispatch(
        plan=journal.plan,
        transaction=journal.current,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
        resolved_request=resolved,
        provider=provider,
    )


def test_store_arms_one_exact_fresh_authority_and_retains_marker(
    tmp_path: Path,
) -> None:
    journal, resolved = _begin(tmp_path)
    authority = _arm(journal, resolved)
    state = journal.evidence_store.dispatch_attempt_state(
        plan=journal.plan,
        transaction=journal.current,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )

    assert state.attempted is True
    assert state.attempt is not None
    assert state.attempt.resolved_request_sha256 == resolved.digest()
    assert state.attempt.provider == "S3"
    assert authority.consume(
        provider="S3",
        operation_sha256=resolved.mutation_request.operation_sha256,
        resolved_request_sha256=resolved.digest(),
    ) == state.attempt

    with pytest.raises((EvidenceStoreV2Error, DispatchAttemptError), match="already"):
        _arm(journal, resolved)


@pytest.mark.parametrize(
    ("target", "provider"),
    [
        ("foundation:STACK_CREATE", "CLOUDFORMATION"),
        ("foundation:ASSET_PUBLISH", "S3"),
        ("image:IMAGE_PUBLISH", "ECR"),
        ("runtime:AGENTCORE_HARDEN", "AGENTCORE"),
        ("context:RUNTIME_CONTEXT_WRITE", "LOCAL_FILESYSTEM"),
    ],
)
def test_store_uses_closed_step_provider_routes(
    tmp_path: Path, target: str, provider: str
) -> None:
    journal, resolved = _begin(tmp_path, target)
    _arm(journal, resolved, provider=provider)

    crossed = "S3" if provider != "S3" else "ECR"
    with pytest.raises(EvidenceStoreV2Error, match="provider"):
        _arm(journal, resolved, provider=crossed)


def test_store_rejects_crossed_resolved_request_without_marker(
    tmp_path: Path,
) -> None:
    journal, resolved = _begin(tmp_path)
    crossed = replace(
        resolved,
        mutation_request=replace(
            resolved.mutation_request,
            operation_sha256="0" * 64,
        ),
    )

    with pytest.raises(EvidenceStoreV2Error, match="resolved"):
        _arm(journal, crossed)

    state = journal.evidence_store.dispatch_attempt_state(
        plan=journal.plan,
        transaction=journal.current,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )
    assert state.attempted is False


def test_absent_without_dispatch_marker_can_clear_intent(tmp_path: Path) -> None:
    journal, _resolved = _begin(tmp_path)
    outcome = _retained_outcome(journal, ObservationDisposition.ABSENT)

    cleared = journal.reconcile_step(outcome=outcome)

    assert cleared.state == cleared.last_stable_state
    assert cleared.uncertain_step_id == ""
    assert cleared.uncertain_operation_sha256 == ""


@pytest.mark.parametrize(
    "disposition",
    [ObservationDisposition.ABSENT, ObservationDisposition.PENDING],
)
def test_observation_after_durable_attempt_never_clears_or_revises_intent(
    tmp_path: Path,
    disposition: ObservationDisposition,
) -> None:
    journal, resolved = _begin(tmp_path)
    _arm(journal, resolved)
    before = journal.current
    outcome = _retained_outcome(journal, disposition)

    retained = journal.reconcile_step(outcome=outcome)

    assert retained == before
    reloaded = TransactionJournalV2.load(
        journal.path,
        plan=journal.plan,
        evidence_store=journal.evidence_store,
    )
    assert reloaded.current == before
    assert (
        reloaded.evidence_store.dispatch_attempt_state(
            plan=reloaded.plan,
            transaction=reloaded.current,
            journal_path=reloaded.path,
            journal_execution_id=reloaded.journal_execution_id,
        ).attempted
        is True
    )


def test_marker_is_bound_to_exact_store_path_execution_and_revision(
    tmp_path: Path,
) -> None:
    journal, resolved = _begin(tmp_path / "source")
    _arm(journal, resolved)
    stale = replace(journal.current, revision=journal.current.revision + 1)

    with pytest.raises(EvidenceStoreV2Error):
        journal.evidence_store.dispatch_attempt_state(
            plan=journal.plan,
            transaction=stale,
            journal_path=journal.path,
            journal_execution_id=journal.journal_execution_id,
        )
    with pytest.raises(EvidenceStoreV2Error):
        journal.evidence_store.dispatch_attempt_state(
            plan=journal.plan,
            transaction=journal.current,
            journal_path=journal.path,
            journal_execution_id="0" * 64,
        )


def test_unexpected_dispatch_inventory_fails_closed(tmp_path: Path) -> None:
    journal, _resolved = _begin(tmp_path)
    crossed = (
        journal.evidence_store.root
        / journal.plan.digest()
        / "dispatch-crossed.json"
    )
    crossed.write_bytes(b'{"schema":"crossed"}\n')
    crossed.chmod(0o400)

    with pytest.raises(EvidenceStoreV2Error, match="inventory"):
        journal.evidence_store.dispatch_attempt_state(
            plan=journal.plan,
            transaction=journal.current,
            journal_path=journal.path,
            journal_execution_id=journal.journal_execution_id,
        )
