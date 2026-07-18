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
        operation=operation,
    )

    assert observed[0].state == "UNCERTAIN"
    assert observed[0].last_stable_state == "PREFLIGHTED"
    assert observed[0].uncertain_phase == "FOUNDATION_READY"
    assert result.state == "FOUNDATION_READY"
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
            operation=lambda: {},
        )

    restored = reloaded.reconcile(persisted=False)
    assert restored.state == "PREFLIGHTED"
    assert TransactionJournal.load(journal.path).resume_target() == "FOUNDATION_READY"


def test_reconcile_persisted_requires_exact_phase_evidence(tmp_path: Path) -> None:
    journal = _create(tmp_path)
    journal.advance_local("PREFLIGHTED")
    journal.begin_mutation("FOUNDATION_READY", rollback_reference=ROLLBACK)
    journal.reconcile(persisted=True)
    journal.begin_mutation("IMAGE_PUBLISHED", rollback_reference=ROLLBACK)

    with pytest.raises((ContractError, TransactionError), match="image"):
        journal.reconcile(persisted=True)

    reconciled = journal.reconcile(
        persisted=True,
        evidence={"runtime_image_digest": DIGEST},
    )
    assert reconciled.state == "IMAGE_PUBLISHED"
    assert reconciled.runtime_image_digest == DIGEST


def test_resume_and_rollback_are_bound_to_the_exact_recorded_reference(
    tmp_path: Path,
) -> None:
    journal = _create(tmp_path)
    journal.advance_local("PREFLIGHTED")
    journal.run_mutation(
        "FOUNDATION_READY", rollback_reference=ROLLBACK, operation=lambda: {}
    )

    with pytest.raises(TransactionError, match="does not match"):
        journal.record_rollback(ROLLBACK[:-1] + "e")

    rolled_back = journal.record_rollback(ROLLBACK)
    assert rolled_back.state == "ROLLED_BACK"
    assert journal.resume_target() is None


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
