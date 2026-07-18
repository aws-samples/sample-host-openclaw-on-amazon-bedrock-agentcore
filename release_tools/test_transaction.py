from __future__ import annotations

import os
from pathlib import Path

import pytest

import release_tools.contracts as contracts
from release_tools.contracts import ContractError, StagingTransactionV1
from release_tools.transaction import TransactionError, TransactionJournal


ACCOUNT = "123456789012"
COMMIT = "a" * 40
TREE = "b" * 40
DIGEST = "sha256:" + "c" * 64
OPERATION = "sha256:" + "f" * 64
ROLLBACK = (
    f"rollback:v1:{ACCOUNT}:eu-west-1:{COMMIT}:sha256:" + "d" * 64
)


def _create(tmp_path: Path) -> TransactionJournal:
    return TransactionJournal.create(
        tmp_path / "release-transaction.json",
        source_commit=COMMIT,
        source_tree=TREE,
        account=ACCOUNT,
        region="eu-west-1",
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
    observed: list[StagingTransactionV1] = []

    def operation() -> dict[str, str]:
        observed.append(TransactionJournal.load(journal.path).current)
        return {}

    result = journal.run_mutation(
        "FOUNDATION_READY",
        rollback_reference=ROLLBACK,
        operation_sha256=OPERATION,
        operation=operation,
    )

    assert observed[0].state == "UNCERTAIN"
    assert observed[0].last_stable_state == "PREFLIGHTED"
    assert observed[0].uncertain_phase == "FOUNDATION_READY"
    assert observed[0].uncertain_operation_sha256 == OPERATION
    assert result.state == "FOUNDATION_READY"
    assert result.uncertain_operation_sha256 == ""
    assert result.rollback_reference == ROLLBACK
    assert result.revision == 3


def test_partial_failure_stays_uncertain_and_blocks_later_phases(tmp_path: Path) -> None:
    journal = _create(tmp_path)
    journal.advance_local("PREFLIGHTED")

    def fail_after_boundary() -> dict[str, str]:
        raise TimeoutError("provider response was lost")

    with pytest.raises(TimeoutError, match="lost"):
        journal.run_mutation(
            "FOUNDATION_READY",
            rollback_reference=ROLLBACK,
            operation_sha256=OPERATION,
            operation=fail_after_boundary,
        )

    reloaded = TransactionJournal.load(journal.path)
    assert reloaded.current.state == "UNCERTAIN"
    with pytest.raises(TransactionError, match="reconcile"):
        reloaded.resume_target()
    with pytest.raises(TransactionError, match="UNCERTAIN"):
        reloaded.run_mutation(
            "FOUNDATION_READY",
            rollback_reference=ROLLBACK,
            operation_sha256=OPERATION,
            operation=lambda: {},
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
    journal.run_mutation(
        "FOUNDATION_READY", rollback_reference=ROLLBACK, operation_sha256=OPERATION, operation=lambda: {}
    )
    journal.run_mutation(
        "IMAGE_PUBLISHED",
        rollback_reference=ROLLBACK,
        operation_sha256=OPERATION,
        operation=lambda: {"runtime_image_digest": DIGEST},
    )
    if phase in {
        "ENDPOINT_READY",
        "CONTEXT_WRITTEN",
        "CONSUMER_CHANGESETS_READY",
    }:
        journal.run_mutation(
            "RUNTIME_READY",
            rollback_reference=ROLLBACK,
            operation_sha256=OPERATION,
            operation=lambda: {
                "runtime_id": "Runtime-ABCDEFGHIJ",
                "runtime_version": "7",
            },
        )
    if phase in {"CONTEXT_WRITTEN", "CONSUMER_CHANGESETS_READY"}:
        journal.run_mutation(
            "ENDPOINT_READY", rollback_reference=ROLLBACK, operation_sha256=OPERATION, operation=lambda: {}
        )
    if phase == "CONSUMER_CHANGESETS_READY":
        journal.run_mutation(
            "CONTEXT_WRITTEN",
            rollback_reference=ROLLBACK,
            operation_sha256=OPERATION,
            operation=lambda: {"runtime_context_sha256": "1" * 64},
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
    journal.run_mutation(
        "FOUNDATION_READY", rollback_reference=ROLLBACK, operation_sha256=OPERATION, operation=lambda: {}
    )
    journal.run_mutation(
        "IMAGE_PUBLISHED",
        rollback_reference=ROLLBACK,
        operation_sha256=OPERATION,
        operation=lambda: {"runtime_image_digest": DIGEST},
    )
    journal.run_mutation(
        "RUNTIME_READY",
        rollback_reference=ROLLBACK,
        operation_sha256=OPERATION,
        operation=lambda: {
            "runtime_id": "Runtime-ABCDEFGHIJ",
            "runtime_version": "7",
        },
    )
    journal.run_mutation(
        "ENDPOINT_READY", rollback_reference=ROLLBACK, operation_sha256=OPERATION, operation=lambda: {}
    )
    journal.run_mutation(
        "CONTEXT_WRITTEN",
        rollback_reference=ROLLBACK,
        operation_sha256=OPERATION,
        operation=lambda: {"runtime_context_sha256": "1" * 64},
    )
    journal.run_mutation(
        "CONSUMER_CHANGESETS_READY",
        rollback_reference=ROLLBACK,
        operation_sha256=OPERATION,
        operation=lambda: {"consumer_changesets_sha256": "2" * 64},
    )
    journal.run_mutation(
        "CONSUMERS_APPLIED",
        rollback_reference=ROLLBACK,
        operation_sha256=OPERATION,
        operation=lambda: {"consumer_application_sha256": "3" * 64},
    )
    journal.run_mutation(
        "VERIFIED",
        rollback_reference=ROLLBACK,
        operation_sha256=OPERATION,
        operation=lambda: {"verification_sha256": "4" * 64},
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
    journal.run_mutation(
        "FOUNDATION_READY", rollback_reference=ROLLBACK, operation_sha256=OPERATION, operation=lambda: {}
    )

    assert not hasattr(journal, "record_rollback")


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
