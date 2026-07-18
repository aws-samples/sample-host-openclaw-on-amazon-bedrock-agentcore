"""Durable, linear staging transaction journal with fail-closed recovery."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from release_tools.contracts import (
    ContractError,
    LINEAR_TRANSACTION_STATES,
    StagingTransactionV1,
    atomic_replace_contract,
    read_regular_bytes,
    write_new_contract,
)


Evidence = Mapping[str, str]
Mutation = Callable[[], Evidence]
_PHASE_EVIDENCE_FIELDS = {
    "FOUNDATION_READY": frozenset(),
    "IMAGE_PUBLISHED": frozenset({"runtime_image_digest"}),
    "RUNTIME_READY": frozenset({"runtime_id", "runtime_version"}),
    "ENDPOINT_READY": frozenset(),
    "CONTEXT_WRITTEN": frozenset({"runtime_context_sha256"}),
    "CONSUMER_CHANGESETS_READY": frozenset({"consumer_changesets_sha256"}),
    "CONSUMERS_APPLIED": frozenset({"consumer_application_sha256"}),
    "VERIFIED": frozenset({"verification_sha256"}),
}
_LOCAL_STATES = {"PREFLIGHTED"}


class TransactionError(RuntimeError):
    """The journal cannot safely perform the requested state transition."""


class TransactionJournal:
    """One compare-and-swap writer for a canonical staging transaction."""

    def __init__(
        self,
        path: Path,
        current: StagingTransactionV1,
        payload: bytes,
    ) -> None:
        self.path = Path(path)
        self.current = current
        self._payload = payload

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        source_commit: str,
        source_tree: str,
        account: str,
        region: str,
    ) -> "TransactionJournal":
        value = StagingTransactionV1.from_mapping(
            {
                "schema": StagingTransactionV1.SCHEMA,
                "transactionId": f"release_{source_commit}",
                "sourceCommit": source_commit,
                "sourceTree": source_tree,
                "account": account,
                "region": region,
                "state": "NEW",
                "lastStableState": "NEW",
                "revision": 0,
                "runtimeImageDigest": "",
                "runtimeId": "",
                "runtimeVersion": "",
                "runtimeEndpointName": f"release_{source_commit}",
                "runtimeContextSha256": "",
                "consumerChangesetsSha256": "",
                "consumerApplicationSha256": "",
                "verificationSha256": "",
                "rollbackReference": "",
                "uncertainPhase": "",
                "uncertainOperationSha256": "",
            }
        )
        write_new_contract(Path(path), value)
        return cls(Path(path), value, value.to_bytes())

    @classmethod
    def load(cls, path: Path) -> "TransactionJournal":
        try:
            payload = read_regular_bytes(Path(path))
            current = StagingTransactionV1.from_bytes(payload)
        except ContractError as error:
            if "regular file" in str(error):
                raise TransactionError(str(error)) from error
            raise
        return cls(Path(path), current, payload)

    def _persist(self, candidate: StagingTransactionV1) -> StagingTransactionV1:
        try:
            atomic_replace_contract(self.path, self._payload, candidate)
        except ContractError as error:
            if "changed concurrently" in str(error):
                raise TransactionError("staging transaction changed concurrently") from error
            raise
        self.current = candidate
        self._payload = candidate.to_bytes()
        return candidate

    def _next_state(self) -> str | None:
        if self.current.state == "UNCERTAIN":
            raise TransactionError("UNCERTAIN staging transaction must reconcile first")
        if self.current.state in {"ROLLED_BACK", "VERIFIED"}:
            return None
        try:
            index = LINEAR_TRANSACTION_STATES.index(self.current.state)
        except ValueError as error:
            raise TransactionError("staging transaction state is not resumable") from error
        if index + 1 == len(LINEAR_TRANSACTION_STATES):
            return None
        return LINEAR_TRANSACTION_STATES[index + 1]

    def resume_target(self) -> str | None:
        """Return the one legal next phase, or fail closed on uncertainty."""

        return self._next_state()

    def advance_local(self, target_state: str) -> StagingTransactionV1:
        """Advance a proven non-mutating local phase."""

        expected = self._next_state()
        if target_state != expected:
            raise TransactionError(
                f"target is not the legal next state: expected {expected}, got {target_state}"
            )
        if target_state not in _LOCAL_STATES:
            raise TransactionError(f"{target_state} is a mutation phase")
        mapping = self.current.to_mapping()
        mapping.update(
            {
                "state": target_state,
                "lastStableState": target_state,
                "revision": self.current.revision + 1,
            }
        )
        return self._persist(StagingTransactionV1.from_mapping(mapping))

    def begin_mutation(
        self,
        target_state: str,
        *,
        rollback_reference: str,
        operation_sha256: str,
    ) -> StagingTransactionV1:
        """Persist write-ahead UNCERTAIN intent before crossing a mutation boundary."""

        if self.current.state == "UNCERTAIN":
            raise TransactionError("staging transaction is already UNCERTAIN")
        expected = self._next_state()
        if target_state != expected:
            raise TransactionError(
                f"target is not the legal next state: expected {expected}, got {target_state}"
            )
        if target_state in _LOCAL_STATES:
            raise TransactionError(f"{target_state} is not a mutation phase")
        mapping = self.current.to_mapping()
        mapping.update(
            {
                "state": "UNCERTAIN",
                "lastStableState": self.current.state,
                "revision": self.current.revision + 1,
                "rollbackReference": rollback_reference,
                "uncertainPhase": target_state,
                "uncertainOperationSha256": operation_sha256,
            }
        )
        return self._persist(StagingTransactionV1.from_mapping(mapping))

    def reconcile(
        self,
        *,
        persisted: bool,
        operation_sha256: str,
        evidence: Evidence | None = None,
    ) -> StagingTransactionV1:
        """Resolve UNCERTAIN using authoritative live-state evidence."""

        if self.current.state != "UNCERTAIN":
            raise TransactionError("only an UNCERTAIN phase can be reconciled")
        if self.current.uncertain_phase == "ROLLBACK":
            raise TransactionError("UNCERTAIN rollback requires rollback reconciliation")
        if operation_sha256 != self.current.uncertain_operation_sha256:
            raise TransactionError(
                "reconciliation operation digest differs from the journal"
            )
        supplied = dict(evidence or {})
        if not persisted and supplied:
            raise TransactionError("absent reconciliation accepts no evidence")
        expected_fields = _PHASE_EVIDENCE_FIELDS.get(self.current.uncertain_phase)
        if expected_fields is None:
            raise TransactionError("UNCERTAIN phase has no evidence ownership rule")
        if persisted and set(supplied) != expected_fields:
            raise TransactionError(
                "reconciliation evidence fields differ from the uncertain phase: "
                f"expected {sorted(expected_fields)}, got {sorted(supplied)}"
            )
        target = (
            self.current.uncertain_phase
            if persisted
            else self.current.last_stable_state
        )
        mapping = self.current.to_mapping()
        mapping.update(
            {
                "state": target,
                "lastStableState": target,
                "revision": self.current.revision + 1,
                "uncertainPhase": "",
                "uncertainOperationSha256": "",
            }
        )
        aliases = {
            "runtime_image_digest": "runtimeImageDigest",
            "runtime_id": "runtimeId",
            "runtime_version": "runtimeVersion",
            "runtime_context_sha256": "runtimeContextSha256",
            "consumer_changesets_sha256": "consumerChangesetsSha256",
            "consumer_application_sha256": "consumerApplicationSha256",
            "verification_sha256": "verificationSha256",
        }
        for name, value in supplied.items():
            mapping[aliases[name]] = value
        return self._persist(StagingTransactionV1.from_mapping(mapping))

    def begin_rollback(
        self,
        rollback_reference: str,
        *,
        operation_sha256: str,
    ) -> StagingTransactionV1:
        """Durably record rollback intent for one fully verified transaction."""

        if self.current.state == "UNCERTAIN":
            raise TransactionError("staging transaction is already UNCERTAIN")
        if self.current.state != "VERIFIED":
            raise TransactionError("only a VERIFIED transaction can roll back")
        if rollback_reference != self.current.rollback_reference:
            raise TransactionError("rollback reference does not match the journal")
        mapping = self.current.to_mapping()
        mapping.update(
            {
                "state": "UNCERTAIN",
                "lastStableState": "VERIFIED",
                "revision": self.current.revision + 1,
                "uncertainPhase": "ROLLBACK",
                "uncertainOperationSha256": operation_sha256,
            }
        )
        return self._persist(StagingTransactionV1.from_mapping(mapping))

    def reconcile_rollback(
        self,
        *,
        persisted: bool,
        operation_sha256: str,
    ) -> StagingTransactionV1:
        """Resolve write-ahead rollback intent from authoritative evidence."""

        if (
            self.current.state != "UNCERTAIN"
            or self.current.uncertain_phase != "ROLLBACK"
        ):
            raise TransactionError("only an UNCERTAIN rollback can be reconciled")
        if operation_sha256 != self.current.uncertain_operation_sha256:
            raise TransactionError(
                "rollback reconciliation operation digest differs from the journal"
            )
        mapping = self.current.to_mapping()
        mapping.update(
            {
                "state": "ROLLED_BACK" if persisted else "VERIFIED",
                "lastStableState": "VERIFIED",
                "revision": self.current.revision + 1,
                "uncertainPhase": "",
                "uncertainOperationSha256": "",
            }
        )
        return self._persist(StagingTransactionV1.from_mapping(mapping))

    def run_mutation(
        self,
        target_state: str,
        *,
        rollback_reference: str,
        operation_sha256: str,
        operation: Mutation,
    ) -> StagingTransactionV1:
        """Run one mutation only after durable intent; ambiguity remains UNCERTAIN."""

        self.begin_mutation(
            target_state,
            rollback_reference=rollback_reference,
            operation_sha256=operation_sha256,
        )
        evidence = operation()
        if not isinstance(evidence, Mapping):
            raise TransactionError("mutation evidence must be a mapping")
        return self.reconcile(
            persisted=True,
            operation_sha256=operation_sha256,
            evidence=evidence,
        )
