"""Strong runtime-tombstone guard for queued work after account deletion."""

from __future__ import annotations


class DeletionFenceError(RuntimeError):
    pass


class RuntimeAccountDeletionFence:
    def __init__(self, repository) -> None:
        if not callable(getattr(repository, "get", None)):
            raise TypeError("runtime deletion repository is invalid")
        self._repository = repository

    def is_account_deleted(self, user_id: str) -> bool:
        record = self._repository.get(user_id)
        if record is None:
            return False
        state = getattr(record, "state", None)
        state = getattr(state, "value", state)
        tombstoned_at = getattr(record, "tombstoned_at", None)
        purge_reason = getattr(record, "purge_reason", None)
        exact_account_tombstone = bool(
            state == "DELETING"
            and isinstance(tombstoned_at, int)
            and not isinstance(tombstoned_at, bool)
            and tombstoned_at > 0
            and purge_reason == "ACCOUNT_DELETION"
        )
        if exact_account_tombstone:
            return True
        if purge_reason == "WORKSPACE_EXPIRY":
            return False
        if (
            state == "DELETING"
            or tombstoned_at is not None
            or purge_reason == "ACCOUNT_DELETION"
        ):
            raise DeletionFenceError("runtime deletion authority is inconsistent")
        return False
