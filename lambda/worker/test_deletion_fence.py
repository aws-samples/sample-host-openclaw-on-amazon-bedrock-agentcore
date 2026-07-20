from __future__ import annotations

from types import SimpleNamespace

import pytest

from .deletion_fence import DeletionFenceError, RuntimeAccountDeletionFence


class Repository:
    def __init__(self, record):
        self.record = record
        self.calls = []

    def get(self, user_id):
        self.calls.append(user_id)
        return self.record


def record(*, state="READY", tombstoned_at=None, purge_reason=None):
    return SimpleNamespace(
        state=SimpleNamespace(value=state),
        tombstoned_at=tombstoned_at,
        purge_reason=purge_reason,
    )


def test_exact_account_deletion_tombstone_blocks_queued_work():
    repository = Repository(record(
        state="DELETING",
        tombstoned_at=1_000,
        purge_reason="ACCOUNT_DELETION",
    ))

    assert RuntimeAccountDeletionFence(repository).is_account_deleted("user_a1") is True
    assert repository.calls == ["user_a1"]


def test_active_and_workspace_expiry_records_do_not_become_account_deletion():
    for candidate in (
        None,
        record(),
        record(
            state="DELETING",
            tombstoned_at=1_000,
            purge_reason="WORKSPACE_EXPIRY",
        ),
    ):
        assert RuntimeAccountDeletionFence(
            Repository(candidate)
        ).is_account_deleted("user_a1") is False


@pytest.mark.parametrize(
    "candidate",
    [
        record(state="DELETING", tombstoned_at=None, purge_reason="ACCOUNT_DELETION"),
        record(state="READY", tombstoned_at=1_000, purge_reason="ACCOUNT_DELETION"),
        record(state="DELETING", tombstoned_at=1_000, purge_reason=None),
    ],
)
def test_partial_account_deletion_authority_is_fail_closed(candidate):
    with pytest.raises(DeletionFenceError):
        RuntimeAccountDeletionFence(Repository(candidate)).is_account_deleted("user_a1")
