"""Durable, linear staging transaction journal with fail-closed recovery."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Mapping

from release_tools.contracts import (
    AbortRetainedEvidenceV2,
    ContractError,
    FailedRetainedEvidenceV2,
    LINEAR_TRANSACTION_STATES,
    ReleasePlanV2,
    ReleaseStepObservationV2,
    StagingTransactionV1,
    StagingTransactionV2,
    _canonical_release_plan_v2,
    _completed_prefix_sha256,
    _release_operation_sha256,
    atomic_replace_contract,
    read_regular_bytes,
    write_new_contract,
)
from release_tools.evidence_store_v2 import (
    EvidenceStoreV2Error,
    ReleaseEvidenceStoreV2,
    ReleaseOutcomeComposerV2,
    VerifiedStepOutcomeV2,
    _JOURNAL_TRANSITION_TOKEN,
)


Evidence = Mapping[str, str]
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

_V2_PHASE_STATES = {
    "foundation": "FOUNDATION_READY",
    "image": "IMAGE_PUBLISHED",
    "runtime": "RUNTIME_READY",
    "endpoint": "ENDPOINT_READY",
    "context": "CONTEXT_WRITTEN",
    "router-cron-cs": "ROUTER_CRON_CHANGESETS_READY",
    "router-cron": "ROUTER_CRON_APPLIED",
    "scheduler-cs": "SCHEDULER_CHANGESET_READY",
    "scheduler": "SCHEDULER_APPLIED",
    "web-cs": "WEB_CHANGESET_READY",
    "web": "WEB_APPLIED",
    "verify": "VERIFIED",
}
class TransactionError(RuntimeError):
    """The journal cannot safely perform the requested state transition."""


class ObservationDisposition(str, Enum):
    """One authoritative observation of an exact v2 plan step."""

    ABSENT = "ABSENT"
    PENDING = "PENDING"
    PRESENT = "PRESENT"
    FAILED_RETAINED = "FAILED_RETAINED"


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


class TransactionJournalV2:
    """Plan-, execution-, and evidence-store-bound clean-account journal."""

    def __init__(
        self,
        path: Path,
        *,
        plan: ReleasePlanV2,
        current: StagingTransactionV2,
        payload: bytes,
        evidence_store: ReleaseEvidenceStoreV2,
        journal_execution_id: str,
    ) -> None:
        if not isinstance(evidence_store, ReleaseEvidenceStoreV2):
            raise TransactionError("v2 journal requires a release evidence store")
        try:
            canonical_plan = _canonical_release_plan_v2(plan)
            canonical_current = StagingTransactionV2.from_bytes(
                current.to_bytes(), plan=canonical_plan
            )
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise TransactionError(f"invalid v2 journal boundary: {error}") from error
        if payload != canonical_current.to_bytes():
            raise TransactionError("v2 journal payload differs from its typed state")
        self.path = Path(path)
        self.plan = canonical_plan
        self.current = canonical_current
        self._payload = payload
        self.evidence_store = evidence_store
        self.journal_execution_id = journal_execution_id
        self._audit()

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        plan: ReleasePlanV2,
        evidence_store: ReleaseEvidenceStoreV2,
    ) -> "TransactionJournalV2":
        if not isinstance(plan, ReleasePlanV2):
            raise TransactionError("v2 transaction requires a typed release plan")
        if not isinstance(evidence_store, ReleaseEvidenceStoreV2):
            raise TransactionError("v2 transaction requires an evidence store")
        try:
            plan = _canonical_release_plan_v2(plan)
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise TransactionError(f"v2 release plan is invalid: {error}") from error
        plan_value = plan.to_mapping()
        value = StagingTransactionV2.from_mapping(
            {
                "schema": StagingTransactionV2.SCHEMA,
                "transactionId": plan_value["transactionId"],
                "sourceCommit": plan_value["sourceCommit"],
                "sourceTree": plan_value["sourceTree"],
                "account": plan_value["account"],
                "region": plan_value["region"],
                "state": "NEW",
                "lastStableState": "NEW",
                "planSha256": plan.digest(),
                "completedStepCount": 0,
                "completedSteps": [],
                "foundationInputsSha256": "",
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
                "rollbackBaselineSha256": "",
                "abortEvidenceSha256": "",
                "failedRetainedEvidenceSha256": "",
                "failureObservationSha256": "",
                "failedStepId": "",
                "failedSubject": "",
                "failedOperationSha256": "",
                "failureReason": "",
                "uncertainStepId": "",
                "uncertainOperationSha256": "",
                "revision": 0,
            },
            plan=plan,
        )
        try:
            execution_id = evidence_store.bind_new_journal(
                journal_path=Path(path),
                plan=plan,
                initial_transaction=value,
            )
            write_new_contract(Path(path), value)
        except (ContractError, EvidenceStoreV2Error, OSError) as error:
            raise TransactionError(str(error)) from error
        return cls(
            Path(path),
            plan=plan,
            current=value,
            payload=value.to_bytes(),
            evidence_store=evidence_store,
            journal_execution_id=execution_id,
        )

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        plan: ReleasePlanV2,
        evidence_store: ReleaseEvidenceStoreV2,
    ) -> "TransactionJournalV2":
        if not isinstance(plan, ReleasePlanV2):
            raise TransactionError("v2 transaction requires a typed release plan")
        if not isinstance(evidence_store, ReleaseEvidenceStoreV2):
            raise TransactionError("v2 transaction requires an evidence store")
        try:
            plan = _canonical_release_plan_v2(plan)
            execution_id = evidence_store.bind_existing_journal(
                journal_path=Path(path), plan=plan
            )
            payload = read_regular_bytes(Path(path))
            current = StagingTransactionV2.from_bytes(payload, plan=plan)
        except (
            AttributeError,
            ContractError,
            EvidenceStoreV2Error,
            TypeError,
            ValueError,
        ) as error:
            if "regular file" in str(error):
                raise TransactionError(str(error)) from error
            raise TransactionError(f"invalid v2 journal boundary: {error}") from error
        if current.plan_sha256 != plan.digest():
            raise TransactionError("v2 transaction plan differs from the journal")
        journal = cls(
            Path(path),
            plan=plan,
            current=current,
            payload=payload,
            evidence_store=evidence_store,
            journal_execution_id=execution_id,
        )
        journal._recover_pending_transition()
        return journal

    def _recover_pending_transition(self) -> None:
        """Finalize one already-retained transition without provider access."""

        try:
            transition = self.evidence_store.pending_recovery_transition(
                plan=self.plan,
                transaction=self.current,
                journal_path=self.path,
                journal_execution_id=self.journal_execution_id,
                _token=_JOURNAL_TRANSITION_TOKEN,
            )
        except (ContractError, EvidenceStoreV2Error, OSError) as error:
            raise TransactionError(
                f"journal recovery audit failed: {error}"
            ) from error
        if transition is None:
            return

        prior_payload = transition.prior_transaction.to_bytes()
        next_transaction = transition.next_transaction
        next_payload = next_transaction.to_bytes()
        if self._payload == prior_payload:
            try:
                atomic_replace_contract(
                    self.path, prior_payload, next_transaction
                )
            except ContractError as error:
                if "changed concurrently" in str(error):
                    raise TransactionError(
                        "staging transaction changed concurrently"
                    ) from error
                raise TransactionError(
                    f"journal recovery CAS failed: {error}"
                ) from error
            self.current = next_transaction
            self._payload = next_payload
        elif self._payload != next_payload:
            raise TransactionError(
                "journal recovery state differs from its transition"
            )

        try:
            self.evidence_store.commit_transition(
                transition, _token=_JOURNAL_TRANSITION_TOKEN
            )
        except (ContractError, EvidenceStoreV2Error, OSError) as error:
            raise TransactionError(
                "journal recovery commit failed"
            ) from error
        self._audit()

    def _audit(self) -> None:
        try:
            self.evidence_store.audit_prefix(
                plan=self.plan,
                transaction=self.current,
                journal_path=self.path,
                journal_execution_id=self.journal_execution_id,
            )
        except (ContractError, EvidenceStoreV2Error, OSError) as error:
            try:
                if read_regular_bytes(self.path) != self._payload:
                    raise TransactionError(
                        "staging transaction changed concurrently"
                    ) from error
            except TransactionError:
                raise
            except (ContractError, OSError):
                pass
            raise TransactionError(str(error)) from error

    def _persist(self, candidate: StagingTransactionV2) -> StagingTransactionV2:
        try:
            canonical_plan = _canonical_release_plan_v2(self.plan)
            candidate = StagingTransactionV2.from_bytes(
                candidate.to_bytes(), plan=canonical_plan
            )
            transition = self.evidence_store.prepare_transition(
                plan=canonical_plan,
                prior=self.current,
                next_transaction=candidate,
                journal_path=self.path,
                journal_execution_id=self.journal_execution_id,
                _token=_JOURNAL_TRANSITION_TOKEN,
            )
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise TransactionError(f"invalid v2 journal candidate: {error}") from error
        except (EvidenceStoreV2Error, OSError) as error:
            raise TransactionError(str(error)) from error
        try:
            atomic_replace_contract(self.path, self._payload, candidate)
        except ContractError as error:
            if "changed concurrently" in str(error):
                raise TransactionError("staging transaction changed concurrently") from error
            raise
        self.current = candidate
        self.plan = canonical_plan
        self._payload = candidate.to_bytes()
        try:
            self.evidence_store.commit_transition(
                transition, _token=_JOURNAL_TRANSITION_TOKEN
            )
        except (ContractError, EvidenceStoreV2Error, OSError) as error:
            raise TransactionError(
                "journal CAS completed but its transition commit failed"
            ) from error
        self._audit()
        return candidate

    def _steps(self) -> list[dict[str, object]]:
        raw = self.plan.to_mapping()["steps"]
        if not isinstance(raw, list):
            raise TransactionError("v2 release plan steps are malformed")
        return raw

    def _next_step(self) -> dict[str, object] | None:
        steps = self._steps()
        count = self.current.completed_step_count
        if count < 0 or count > len(steps):
            raise TransactionError("v2 completed step cursor is invalid")
        return None if count == len(steps) else dict(steps[count])

    def resume_step(self) -> dict[str, object] | None:
        """Return only the exact next plan step, never a caller-selected phase."""

        self._audit()
        if self.current.state == "UNCERTAIN":
            raise TransactionError("UNCERTAIN v2 transaction must reconcile first")
        if self.current.state in {"VERIFIED", "ABORTED_RETAINED", "ROLLED_BACK"}:
            return None
        if self.current.state == "NEW":
            raise TransactionError("v2 transaction must preflight before execution")
        return self._next_step()

    def advance_preflight(self) -> StagingTransactionV2:
        self._audit()
        if self.current.state != "NEW":
            raise TransactionError("v2 preflight cannot rewind or repeat")
        mapping = self.current.to_mapping()
        mapping.update(
            {
                "state": "PREFLIGHTED",
                "lastStableState": "PREFLIGHTED",
                "revision": self.current.revision + 1,
            }
        )
        return self._persist(
            StagingTransactionV2.from_mapping(mapping, plan=self.plan)
        )

    def operation_sha256(self) -> str:
        """Derive the one operation identity from plan bytes and next step."""

        self._audit()
        if self.current.state == "UNCERTAIN":
            return self.current.uncertain_operation_sha256
        step = self._next_step()
        if step is None:
            raise TransactionError("v2 transaction has no next operation")
        typed_step = self.plan.steps[self.current.completed_step_count]
        return _release_operation_sha256(
            self.plan.digest(),
            typed_step,
            self.completed_prefix_sha256(),
        )

    def completed_prefix_sha256(self) -> str:
        """Hash the exact canonical evidence prefix already retained."""

        self._audit()
        return _completed_prefix_sha256(
            [step.to_mapping() for step in self.current.completed_steps]
        )

    def begin_step(self) -> StagingTransactionV2:
        """Write one exact next-step intent before any external dispatch."""

        self._audit()
        if self.current.state == "UNCERTAIN":
            raise TransactionError("v2 transaction is already UNCERTAIN")
        if self.current.state in {"NEW", "VERIFIED", "ABORTED_RETAINED", "ROLLED_BACK"}:
            raise TransactionError("v2 transaction has no executable next step")
        step = self._next_step()
        if step is None:
            raise TransactionError("v2 transaction has no executable next step")
        if step.get("mutation") is not True:
            raise TransactionError(
                "v2 non-mutating observation must complete without write-ahead intent"
            )
        operation_sha256 = self.operation_sha256()
        mapping = self.current.to_mapping()
        mapping.update(
            {
                "state": "UNCERTAIN",
                "uncertainStepId": step["id"],
                "uncertainOperationSha256": operation_sha256,
                "revision": self.current.revision + 1,
            }
        )
        return self._persist(
            StagingTransactionV2.from_mapping(mapping, plan=self.plan)
        )

    def outcome_composer(self) -> ReleaseOutcomeComposerV2:
        """Return the sole production path for a retained provider outcome."""

        self._audit()
        return self.evidence_store.composer(
            plan=self.plan,
            journal_path=self.path,
            journal_execution_id=self.journal_execution_id,
        )

    def _claim_outcome(
        self,
        *,
        outcome: VerifiedStepOutcomeV2,
    ):
        if not isinstance(outcome, VerifiedStepOutcomeV2):
            raise TransactionError("transition requires a verified retained outcome")
        try:
            plan = _canonical_release_plan_v2(self.plan)
            retained = outcome._claim(
                store=self.evidence_store,
                plan=plan,
                transaction=self.current,
                journal_path=self.path,
                journal_execution_id=self.journal_execution_id,
            )
        except (
            AttributeError,
            ContractError,
            EvidenceStoreV2Error,
            TypeError,
            ValueError,
        ) as error:
            raise TransactionError(str(error)) from error
        return retained

    def _record_present(
        self,
        *,
        step: Mapping[str, object],
        retained,
    ) -> StagingTransactionV2:
        observation = retained.step_observation
        if observation is None or retained.disposition != "PRESENT":
            raise TransactionError("retained outcome is not PRESENT")
        try:
            plan = _canonical_release_plan_v2(self.plan)
            observation = ReleaseStepObservationV2.from_bytes(
                observation.to_bytes()
            )
            observation.validate_plan_step(
                plan,
                completed_step_count=self.current.completed_step_count,
                prior_agent_core_stack_id=self.current.agent_core_stack_id,
                prior_runtime_id=self.current.runtime_id,
                prior_runtime_version=self.current.runtime_version,
                prior_runtime_arn=self.current.runtime_arn,
            )
        except (
            AttributeError,
            ContractError,
            TypeError,
            ValueError,
        ) as error:
            raise TransactionError(str(error)) from error
        phase = step.get("phase")
        if not isinstance(phase, str) or phase not in _V2_PHASE_STATES:
            raise TransactionError("v2 plan step phase is invalid")
        steps = self._steps()
        next_index = self.current.completed_step_count + 1
        phase_complete = (
            next_index == len(steps)
            or steps[next_index].get("phase") != phase
        )
        evidence_sha256 = retained.digest()

        mapping = self.current.to_mapping()
        completed = list(mapping["completedSteps"])
        completed.append(
            {"stepId": step["id"], "evidenceSha256": evidence_sha256}
        )
        mapping.update(
            {
                "completedStepCount": next_index,
                "completedSteps": completed,
                "state": (
                    _V2_PHASE_STATES[phase]
                    if phase_complete
                    else self.current.last_stable_state
                ),
                "lastStableState": (
                    _V2_PHASE_STATES[phase]
                    if phase_complete
                    else self.current.last_stable_state
                ),
                "uncertainStepId": "",
                "uncertainOperationSha256": "",
                "revision": self.current.revision + 1,
            }
        )
        if step.get("kind") == "BASELINE_OBSERVE":
            mapping["rollbackBaselineSha256"] = evidence_sha256
        if observation.foundation_runtime_inputs is not None:
            mapping["foundationInputsSha256"] = (
                observation.foundation_runtime_inputs.digest()
            )
            mapping["agentCoreStackId"] = (
                observation.foundation_runtime_inputs.agent_core_stack_id
            )
        observed_values = {
            "agentCoreStackId": observation.agent_core_stack_id,
            "runtimeImageDigest": observation.runtime_image_digest,
            "runtimeId": observation.runtime_id,
            "runtimeVersion": observation.runtime_version,
            "runtimeArn": observation.runtime_arn,
            "runtimeEndpointId": observation.runtime_endpoint_id,
            "runtimeContextSha256": observation.runtime_context_sha256,
            "routerTargetStackId": observation.router_target_stack_id,
            "routerChangeSetId": observation.router_change_set_id,
            "cronTargetStackId": observation.cron_target_stack_id,
            "cronChangeSetId": observation.cron_change_set_id,
            "routerCronChangesetsSha256": (
                observation.router_cron_changesets_sha256
            ),
            "routerCronApplicationSha256": (
                observation.router_cron_application_sha256
            ),
            "schedulerChangesetSha256": observation.scheduler_changeset_sha256,
            "schedulerTargetStackId": observation.scheduler_target_stack_id,
            "schedulerChangeSetId": observation.scheduler_change_set_id,
            "schedulerApplicationSha256": (
                observation.scheduler_application_sha256
            ),
            "webTargetStackId": observation.web_target_stack_id,
            "webChangeSetId": observation.web_change_set_id,
            "webChangesetSha256": observation.web_changeset_sha256,
            "webApplicationSha256": observation.web_application_sha256,
            "verificationSha256": observation.verification_sha256,
        }
        mapping.update(
            {name: value for name, value in observed_values.items() if value}
        )
        return self._persist(
            StagingTransactionV2.from_mapping(mapping, plan=plan)
        )

    def _record_failed(self, *, retained) -> StagingTransactionV2:
        failure = retained.failure_observation
        if failure is None or retained.disposition != "FAILED_RETAINED":
            raise TransactionError("retained outcome is not a terminal failure")
        mapping = self.current.to_mapping()
        mapping.update(
            {
                "state": "ABORTED_RETAINED",
                "abortEvidenceSha256": "",
                "failedRetainedEvidenceSha256": retained.digest(),
                "failureObservationSha256": failure.digest(),
                "failedStepId": failure.step_id,
                "failedSubject": failure.subject,
                "failedOperationSha256": failure.operation_sha256,
                "failureReason": failure.failure_reason,
                "uncertainStepId": "",
                "uncertainOperationSha256": "",
                "revision": self.current.revision + 1,
            }
        )
        return self._persist(
            StagingTransactionV2.from_mapping(mapping, plan=self.plan)
        )

    def complete_observation(
        self, *, outcome: VerifiedStepOutcomeV2
    ) -> StagingTransactionV2:
        """Consume one retained outcome for the exact read-only next step."""

        self._audit()
        if self.current.state in {
            "NEW",
            "UNCERTAIN",
            "VERIFIED",
            "ABORTED_RETAINED",
            "ROLLED_BACK",
        }:
            raise TransactionError("v2 transaction cannot record this observation")
        step = self._next_step()
        if step is None or step.get("mutation") is not False:
            raise TransactionError("v2 next plan step is not a read-only observation")
        retained = self._claim_outcome(outcome=outcome)
        if retained.disposition == "PRESENT":
            return self._record_present(step=step, retained=retained)
        if retained.disposition == "FAILED_RETAINED":
            return self._record_failed(retained=retained)
        if retained.disposition not in {"ABSENT", "PENDING"}:
            raise TransactionError("retained read-only outcome is invalid")
        mapping = self.current.to_mapping()
        mapping["revision"] = self.current.revision + 1
        return self._persist(
            StagingTransactionV2.from_mapping(mapping, plan=self.plan)
        )

    def reconcile_step(
        self,
        *,
        outcome: VerifiedStepOutcomeV2,
    ) -> StagingTransactionV2:
        """Resolve one intent using one private retained outcome capability."""

        self._audit()
        if self.current.state != "UNCERTAIN":
            raise TransactionError("only an UNCERTAIN v2 step can reconcile")
        step = self._next_step()
        if step is None or step.get("id") != self.current.uncertain_step_id:
            raise TransactionError("UNCERTAIN v2 step is not the exact plan prefix")
        retained = self._claim_outcome(outcome=outcome)
        if retained.disposition == "FAILED_RETAINED":
            return self._record_failed(retained=retained)
        if retained.disposition == "PRESENT":
            return self._record_present(step=step, retained=retained)
        if retained.disposition in {"ABSENT", "PENDING"}:
            try:
                dispatch_state = self.evidence_store.dispatch_attempt_state(
                    plan=self.plan,
                    transaction=self.current,
                    journal_path=self.path,
                    journal_execution_id=self.journal_execution_id,
                )
            except (ContractError, EvidenceStoreV2Error, OSError) as error:
                raise TransactionError(
                    "dispatch-attempt audit failed during reconciliation"
                ) from error
            # Once an exact durable dispatch marker exists, neither eventual
            # absence nor a retryable observation may advance the journal or
            # erase write-ahead intent.  The same revision remains UNCERTAIN
            # until authoritative PRESENT or FAILED_RETAINED evidence exists.
            if dispatch_state.attempted:
                return self.current
            mapping = self.current.to_mapping()
            if retained.disposition == "ABSENT":
                mapping.update(
                    {
                        "state": self.current.last_stable_state,
                        "uncertainStepId": "",
                        "uncertainOperationSha256": "",
                    }
                )
            mapping["revision"] = self.current.revision + 1
            return self._persist(
                StagingTransactionV2.from_mapping(mapping, plan=self.plan)
            )
        raise TransactionError("retained mutation outcome is invalid")

    def abort_retained(
        self, *, evidence: AbortRetainedEvidenceV2
    ) -> StagingTransactionV2:
        """Terminally retain an exact stable clean-account prefix."""

        self._audit()
        if self.current.state == "UNCERTAIN":
            raise TransactionError("UNCERTAIN v2 transaction must reconcile before abort")
        if self.current.state in {
            "NEW",
            "VERIFIED",
            "ABORTED_RETAINED",
            "ROLLED_BACK",
        }:
            raise TransactionError("v2 transaction cannot abort from this state")
        if not isinstance(evidence, AbortRetainedEvidenceV2):
            raise TransactionError("retained abort requires typed evidence")
        try:
            evidence = AbortRetainedEvidenceV2.from_bytes(evidence.to_bytes())
            evidence.validate_transaction(self.plan, self.current)
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise TransactionError(str(error)) from error
        mapping = self.current.to_mapping()
        mapping.update(
            {
                "state": "ABORTED_RETAINED",
                "abortEvidenceSha256": evidence.digest(),
                "revision": self.current.revision + 1,
            }
        )
        return self._persist(
            StagingTransactionV2.from_mapping(mapping, plan=self.plan)
        )
