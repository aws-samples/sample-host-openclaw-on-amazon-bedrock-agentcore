"""Closed, one-step local orchestration for the clean-account release v2.

This module chooses no caller-supplied driver.  The immutable route table maps
every frozen plan kind to one named collaborator lane.  A mutation is dispatched
only after the journal has durably entered ``UNCERTAIN``, the exact request has
been resolved from the audited retained prefix, its private envelope has been
opened as a verified snapshot, and the evidence store has minted a fresh
one-shot dispatch authority.  A process resuming an ``UNCERTAIN`` step can only
observe and reconcile; it can never dispatch the effect again.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from release_tools.contracts import (
    ContractError,
    FoundationRuntimeInputsV1,
    MutationRequestV2,
    PrivateMutationEnvelopeV2,
    ReleasePlanV2,
    ReleaseStepV2,
    ResolvedMutationRequestV2,
    StagingTransactionV2,
    VerifiedPrivateMutationV2,
)
from release_tools.dispatch_attempt_v2 import (
    DispatchAttemptError,
    FreshDispatchAuthorityV1,
    ReleaseDispatchAttemptV1,
)
from release_tools.evidence_store_v2 import (
    EvidenceStoreV2Error,
    ReleaseEvidenceStoreV2,
)
from release_tools.release_artifact_store_v2 import (
    ReleaseArtifactBundleV2,
)
from release_tools.transaction import TransactionError, TransactionJournalV2


class ReleaseRunnerV2Error(RuntimeError):
    """The local runner boundary or exact current step is invalid."""


@dataclass(frozen=True, slots=True)
class ReleaseProviderRouteV2:
    """One compile-time route from a frozen plan kind to a provider lane."""

    provider: str
    lane: str
    mutation: bool

    def __post_init__(self) -> None:
        if self.provider not in {
            "AGENTCORE",
            "CLOUDFORMATION",
            "ECR",
            "LOCAL_FILESYSTEM",
            "S3",
        }:
            raise ReleaseRunnerV2Error("release provider route is invalid")
        if self.lane not in {
            "agentcore",
            "cloudformation",
            "ecr",
            "local_filesystem",
            "s3",
            "verifier",
        }:
            raise ReleaseRunnerV2Error("release provider lane is invalid")
        if not isinstance(self.mutation, bool):
            raise ReleaseRunnerV2Error("release provider route mode is invalid")


RELEASE_KIND_ROUTES_V2: Mapping[str, ReleaseProviderRouteV2] = MappingProxyType(
    {
        "BASELINE_OBSERVE": ReleaseProviderRouteV2(
            "CLOUDFORMATION", "cloudformation", False
        ),
        "BOOTSTRAP_STACK": ReleaseProviderRouteV2(
            "CLOUDFORMATION", "cloudformation", True
        ),
        "ASSET_PUBLISH": ReleaseProviderRouteV2("S3", "s3", True),
        "AGENTCORE_HARDEN": ReleaseProviderRouteV2(
            "AGENTCORE", "agentcore", True
        ),
        "STACK_CREATE": ReleaseProviderRouteV2(
            "CLOUDFORMATION", "cloudformation", True
        ),
        "STACK_UPDATE": ReleaseProviderRouteV2(
            "CLOUDFORMATION", "cloudformation", True
        ),
        "STACK_DRIFT_CHECK": ReleaseProviderRouteV2(
            "CLOUDFORMATION", "cloudformation", True
        ),
        "IMAGE_PUBLISH": ReleaseProviderRouteV2("ECR", "ecr", True),
        "IMAGE_OBSERVE": ReleaseProviderRouteV2("ECR", "ecr", False),
        "RUNTIME_CONTEXT_WRITE": ReleaseProviderRouteV2(
            "LOCAL_FILESYSTEM", "local_filesystem", True
        ),
        "CHANGESET_CREATE": ReleaseProviderRouteV2(
            "CLOUDFORMATION", "cloudformation", True
        ),
        "CHANGESET_EXECUTE": ReleaseProviderRouteV2(
            "CLOUDFORMATION", "cloudformation", True
        ),
        "VERIFY": ReleaseProviderRouteV2(
            "LOCAL_FILESYSTEM", "verifier", False
        ),
    }
)


class ReleaseProviderCollaboratorV2(Protocol):
    """Typed lane boundary; the closed bundle, never a string, selects it."""

    def dispatch(
        self,
        *,
        resolution: "ResolvedReleaseStepV2",
        verified_mutation: VerifiedPrivateMutationV2,
        fresh_authority: FreshDispatchAuthorityV1,
    ) -> ReleaseDispatchAttemptV1: ...

    def observe(self, *, resolution: "ResolvedReleaseStepV2") -> object: ...


@dataclass(frozen=True, slots=True)
class ReleaseRunnerCollaboratorsV2:
    """Closed, exhaustively named local collaborator bundle."""

    artifact_bundle: ReleaseArtifactBundleV2
    envelope_directory: Path
    scratch_directory: Path
    cloudformation: ReleaseProviderCollaboratorV2
    s3: ReleaseProviderCollaboratorV2
    ecr: ReleaseProviderCollaboratorV2
    agentcore: ReleaseProviderCollaboratorV2
    local_filesystem: ReleaseProviderCollaboratorV2
    verifier: ReleaseProviderCollaboratorV2

    def __post_init__(self) -> None:
        if type(self.artifact_bundle) is not ReleaseArtifactBundleV2:
            raise ReleaseRunnerV2Error(
                "runner requires one concrete pinned artifact bundle"
            )
        for value, label in (
            (self.envelope_directory, "private envelope directory"),
            (self.scratch_directory, "private snapshot directory"),
        ):
            if not isinstance(value, Path) or value.name in {"", ".", ".."}:
                raise ReleaseRunnerV2Error(f"{label} is invalid")
        if self.envelope_directory == self.scratch_directory:
            raise ReleaseRunnerV2Error(
                "private envelope and snapshot directories must be separate"
            )
        for lane, collaborator in (
            ("cloudformation", self.cloudformation),
            ("s3", self.s3),
            ("ecr", self.ecr),
            ("agentcore", self.agentcore),
            ("local_filesystem", self.local_filesystem),
            ("verifier", self.verifier),
        ):
            if not callable(getattr(collaborator, "observe", None)):
                raise ReleaseRunnerV2Error(
                    f"{lane} collaborator lacks its observer boundary"
                )
            if lane != "verifier" and not callable(
                getattr(collaborator, "dispatch", None)
            ):
                raise ReleaseRunnerV2Error(
                    f"{lane} collaborator lacks its dispatch boundary"
                )

    def collaborator(
        self, route: ReleaseProviderRouteV2
    ) -> ReleaseProviderCollaboratorV2:
        """Resolve only a route-table-produced lane, never caller input."""

        if route.lane == "cloudformation":
            return self.cloudformation
        if route.lane == "s3":
            return self.s3
        if route.lane == "ecr":
            return self.ecr
        if route.lane == "agentcore":
            return self.agentcore
        if route.lane == "local_filesystem":
            return self.local_filesystem
        if route.lane == "verifier":
            return self.verifier
        raise ReleaseRunnerV2Error("release route has no closed collaborator")


@dataclass(frozen=True, slots=True)
class ResolvedReleaseStepV2:
    """Exact audited current step and, for writes, its generated request."""

    step: ReleaseStepV2
    route: ReleaseProviderRouteV2
    resolved_request: ResolvedMutationRequestV2 | None

    def __post_init__(self) -> None:
        if not isinstance(self.step, ReleaseStepV2):
            raise ReleaseRunnerV2Error("resolved release step is invalid")
        if not isinstance(self.route, ReleaseProviderRouteV2):
            raise ReleaseRunnerV2Error("resolved release route is invalid")
        if self.route.mutation != self.step.mutation:
            raise ReleaseRunnerV2Error("release route mutation mode differs")
        if self.step.mutation != (self.resolved_request is not None):
            raise ReleaseRunnerV2Error("resolved mutation authority is incomplete")
        if self.resolved_request is not None:
            canonical = ResolvedMutationRequestV2.from_bytes(
                self.resolved_request.to_bytes()
            )
            if canonical != self.resolved_request:
                raise ReleaseRunnerV2Error(
                    "resolved mutation request is not canonical"
                )


@dataclass(frozen=True, slots=True)
class ReleaseRunnerStepResultV2:
    step_id: str
    phase: str
    kind: str
    provider: str
    action: str
    state: str
    revision: int

    def __post_init__(self) -> None:
        if self.action not in {
            "DISPATCHED_UNCERTAIN",
            "OBSERVED_READ_ONLY",
            "OBSERVED_UNCERTAIN",
        }:
            raise ReleaseRunnerV2Error("runner result action is invalid")
        if not self.step_id or not self.phase or not self.kind:
            raise ReleaseRunnerV2Error("runner result step identity is invalid")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool):
            raise ReleaseRunnerV2Error("runner result revision is invalid")


def _foundation_from_prefix(
    *,
    current: StagingTransactionV2,
    prefix: tuple[Any, ...],
) -> FoundationRuntimeInputsV1 | None:
    candidates = tuple(
        record.step_observation.foundation_runtime_inputs
        for record in prefix
        if record.step_observation is not None
        and record.step_observation.foundation_runtime_inputs is not None
    )
    if not current.foundation_inputs_sha256:
        if candidates:
            raise ReleaseRunnerV2Error(
                "retained foundation owner differs from the journal"
            )
        return None
    if len(candidates) != 1:
        raise ReleaseRunnerV2Error(
            "retained foundation owner is missing or ambiguous"
        )
    foundation = FoundationRuntimeInputsV1.from_bytes(candidates[0].to_bytes())
    if foundation.digest() != current.foundation_inputs_sha256:
        raise ReleaseRunnerV2Error(
            "retained foundation owner differs from the journal"
        )
    return foundation


def _predecessor_stack_id(record: Any) -> str:
    raw = record.observer_evidence_mapping()
    projection = raw.get("projection")
    if isinstance(projection, Mapping):
        for name in ("stackId", "targetStackId"):
            value = projection.get(name)
            if isinstance(value, str) and value:
                return value
    observation = record.step_observation
    if observation is not None:
        for value in (
            observation.agent_core_stack_id,
            observation.router_target_stack_id,
            observation.cron_target_stack_id,
            observation.scheduler_target_stack_id,
            observation.web_target_stack_id,
        ):
            if value:
                return value
    raise ReleaseRunnerV2Error(
        "stack drift predecessor lacks its exact observed stack ID"
    )


def _resolved_mutation(
    *,
    plan: ReleasePlanV2,
    current: StagingTransactionV2,
    step: ReleaseStepV2,
    completed_prefix_sha256: str,
    prefix: tuple[Any, ...],
) -> ResolvedMutationRequestV2:
    if current.state != "UNCERTAIN" or (
        current.uncertain_step_id != step.step_id
        or not current.uncertain_operation_sha256
    ):
        raise ReleaseRunnerV2Error(
            "mutation resolution requires durable UNCERTAIN intent"
        )
    matches = tuple(
        artifact
        for artifact in plan.artifacts
        if artifact.path == step.request_artifact
    )
    if len(matches) != 1:
        raise ReleaseRunnerV2Error(
            "current request artifact is missing or ambiguous"
        )
    artifact = matches[0]
    mutation_request = MutationRequestV2.from_mapping(
        {
            "schema": MutationRequestV2.SCHEMA,
            "transactionId": plan.transaction_id,
            "planSha256": plan.digest(),
            "completedPrefixSha256": completed_prefix_sha256,
            "stepId": step.step_id,
            "operationSha256": current.uncertain_operation_sha256,
            "kind": step.kind,
            "subject": step.subject,
            "requestArtifact": step.request_artifact,
            "requestSha256": step.request_sha256,
        },
        plan=plan,
        completed_step_count=current.completed_step_count,
        completed_prefix_sha256=completed_prefix_sha256,
    )
    foundation = _foundation_from_prefix(current=current, prefix=prefix)
    predecessor_stack_id = ""
    predecessor_evidence_sha256 = ""
    predecessor_observer_evidence_sha256 = ""
    if step.kind == "STACK_DRIFT_CHECK":
        if not prefix or not current.completed_steps:
            raise ReleaseRunnerV2Error(
                "stack drift resolution lacks its retained predecessor"
            )
        predecessor = prefix[-1]
        completed = current.completed_steps[-1]
        if (
            predecessor.digest() != completed.evidence_sha256
            or predecessor.step_id != completed.step_id
        ):
            raise ReleaseRunnerV2Error(
                "stack drift predecessor differs from the completed prefix"
            )
        predecessor_stack_id = _predecessor_stack_id(predecessor)
        predecessor_evidence_sha256 = predecessor.digest()
        predecessor_observer_evidence_sha256 = (
            predecessor.observer_evidence_sha256
        )
    value = ResolvedMutationRequestV2.from_mapping(
        {
            "schema": ResolvedMutationRequestV2.SCHEMA,
            "mutationRequest": mutation_request.to_mapping(),
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
            "expectedObservedRequestSha256": (
                step.expected_observed_request_sha256
            ),
            "expectedContentSha256": step.expected_content_sha256,
            "foundationRuntimeInputs": (
                foundation.to_mapping() if foundation is not None else {}
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
            "routerCronChangesetsSha256": (
                current.router_cron_changesets_sha256
            ),
            "routerCronApplicationSha256": (
                current.router_cron_application_sha256
            ),
            "schedulerTargetStackId": current.scheduler_target_stack_id,
            "schedulerChangeSetId": current.scheduler_change_set_id,
            "schedulerChangesetSha256": current.scheduler_changeset_sha256,
            "schedulerApplicationSha256": (
                current.scheduler_application_sha256
            ),
            "webTargetStackId": current.web_target_stack_id,
            "webChangeSetId": current.web_change_set_id,
            "webChangesetSha256": current.web_changeset_sha256,
            "webApplicationSha256": current.web_application_sha256,
            "predecessorStackId": predecessor_stack_id,
            "predecessorEvidenceSha256": predecessor_evidence_sha256,
            "predecessorObserverEvidenceSha256": (
                predecessor_observer_evidence_sha256
            ),
        }
    )
    value.validate_transaction(plan, current)
    return value


def resolve_current_step_v2(
    journal: TransactionJournalV2,
    evidence_store: ReleaseEvidenceStoreV2,
) -> ResolvedReleaseStepV2 | None:
    """Resolve only the exact durable cursor of one concrete journal/store."""

    if type(journal) is not TransactionJournalV2:
        raise ReleaseRunnerV2Error("runner requires a concrete v2 journal")
    if type(evidence_store) is not ReleaseEvidenceStoreV2:
        raise ReleaseRunnerV2Error("runner requires a concrete evidence store")
    if journal.evidence_store is not evidence_store:
        raise ReleaseRunnerV2Error(
            "runner evidence store is not the journal-bound store"
        )
    try:
        plan = ReleasePlanV2.from_bytes(journal.plan.to_bytes())
        current = StagingTransactionV2.from_bytes(
            journal.current.to_bytes(), plan=plan
        )
        prefix = evidence_store.retained_prefix_for_execution(
            plan=plan,
            transaction=current,
            journal_path=journal.path,
            journal_execution_id=journal.journal_execution_id,
        )
    except (ContractError, EvidenceStoreV2Error, TransactionError) as error:
        raise ReleaseRunnerV2Error(
            "current release cursor could not be audited"
        ) from error
    if current.state in {"VERIFIED", "ABORTED_RETAINED", "ROLLED_BACK"}:
        return None
    if current.state == "NEW":
        raise ReleaseRunnerV2Error("release journal is not preflighted")
    count = current.completed_step_count
    if count >= len(plan.steps):
        raise ReleaseRunnerV2Error("nonterminal release has no current step")
    step = plan.steps[count]
    route = RELEASE_KIND_ROUTES_V2.get(step.kind)
    if route is None:
        raise ReleaseRunnerV2Error("current plan kind has no closed route")
    if route.mutation != step.mutation:
        raise ReleaseRunnerV2Error("current plan route mode differs")
    if step.mutation:
        if current.state != "UNCERTAIN":
            raise ReleaseRunnerV2Error(
                "mutation resolution requires durable UNCERTAIN intent"
            )
        resolved = _resolved_mutation(
            plan=plan,
            current=current,
            step=step,
            completed_prefix_sha256=journal.completed_prefix_sha256(),
            prefix=prefix,
        )
    else:
        if current.state == "UNCERTAIN":
            raise ReleaseRunnerV2Error(
                "read-only step cannot own an UNCERTAIN intent"
            )
        resolved = None
    return ResolvedReleaseStepV2(step, route, resolved)


class ReleaseRunnerV2:
    """Perform at most one provider observation or one fresh dispatch."""

    def __init__(
        self,
        *,
        journal: TransactionJournalV2,
        evidence_store: ReleaseEvidenceStoreV2,
        collaborators: ReleaseRunnerCollaboratorsV2,
    ) -> None:
        if type(journal) is not TransactionJournalV2:
            raise ReleaseRunnerV2Error("runner requires a concrete v2 journal")
        if type(evidence_store) is not ReleaseEvidenceStoreV2:
            raise ReleaseRunnerV2Error("runner requires a concrete evidence store")
        if type(collaborators) is not ReleaseRunnerCollaboratorsV2:
            raise ReleaseRunnerV2Error(
                "runner requires its closed collaborator bundle"
            )
        if journal.evidence_store is not evidence_store:
            raise ReleaseRunnerV2Error(
                "runner evidence store is not the journal-bound store"
            )
        self._journal = journal
        self._evidence_store = evidence_store
        self._collaborators = collaborators

    def _result(
        self, resolution: ResolvedReleaseStepV2, *, action: str
    ) -> ReleaseRunnerStepResultV2:
        return ReleaseRunnerStepResultV2(
            step_id=resolution.step.step_id,
            phase=resolution.step.phase,
            kind=resolution.step.kind,
            provider=resolution.route.provider,
            action=action,
            state=self._journal.current.state,
            revision=self._journal.current.revision,
        )

    def _observe(
        self,
        resolution: ResolvedReleaseStepV2,
        *,
        uncertain: bool,
    ) -> ReleaseRunnerStepResultV2:
        collaborator = self._collaborators.collaborator(resolution.route)
        provider_observation = collaborator.observe(resolution=resolution)
        outcome = self._journal.outcome_composer().compose(
            transaction=self._journal.current,
            provider_observation=provider_observation,
        )
        if uncertain:
            self._journal.reconcile_step(outcome=outcome)
            return self._result(resolution, action="OBSERVED_UNCERTAIN")
        self._journal.complete_observation(outcome=outcome)
        return self._result(resolution, action="OBSERVED_READ_ONLY")

    def _dispatch(
        self, resolution: ResolvedReleaseStepV2
    ) -> ReleaseRunnerStepResultV2:
        resolved = resolution.resolved_request
        if resolved is None:
            raise ReleaseRunnerV2Error(
                "mutation dispatch lacks its resolved request"
            )
        target = self._collaborators.envelope_directory / (
            f"{resolution.step.ordinal:04d}-"
            f"r{self._journal.current.revision:08d}-"
            f"{resolution.step.step_id}-"
            f"{resolved.digest()}.private-mutation"
        )
        self._collaborators.artifact_bundle.write_private_mutation_envelope(
            target,
            resolved_request=resolved,
            transaction=self._journal.current,
        )
        with PrivateMutationEnvelopeV2.open_verified(
            target,
            plan=self._journal.plan,
            transaction=self._journal.current,
            scratch_dir=self._collaborators.scratch_directory,
        ) as verified:
            if (
                verified.resolved_request != resolved
                or verified.metadata.request_artifact_sha256
                != resolved.mutation_request.request_sha256
            ):
                raise ReleaseRunnerV2Error(
                    "verified mutation envelope differs from the exact resolution"
                )
            fresh = self._evidence_store.arm_current_dispatch(
                plan=self._journal.plan,
                transaction=self._journal.current,
                journal_path=self._journal.path,
                journal_execution_id=self._journal.journal_execution_id,
                resolved_request=verified.resolved_request,
                provider=resolution.route.provider,
            )
            if not isinstance(fresh, FreshDispatchAuthorityV1):
                raise ReleaseRunnerV2Error(
                    "evidence store did not mint fresh dispatch authority"
                )
            attempt = self._collaborators.collaborator(
                resolution.route
            ).dispatch(
                resolution=resolution,
                verified_mutation=verified,
                fresh_authority=fresh,
            )
            try:
                fresh.consume(
                    provider=resolution.route.provider,
                    operation_sha256=(
                        resolved.mutation_request.operation_sha256
                    ),
                    resolved_request_sha256=resolved.digest(),
                )
            except DispatchAttemptError:
                pass
            else:
                raise ReleaseRunnerV2Error(
                    "dispatcher did not consume fresh dispatch authority"
                )
        if not isinstance(attempt, ReleaseDispatchAttemptV1):
            raise ReleaseRunnerV2Error(
                "dispatcher did not consume fresh dispatch authority"
            )
        canonical_attempt = ReleaseDispatchAttemptV1.from_bytes(
            attempt.to_bytes()
        )
        if (
            canonical_attempt.provider != resolution.route.provider
            or canonical_attempt.operation_sha256
            != resolved.mutation_request.operation_sha256
            or canonical_attempt.resolved_request_sha256 != resolved.digest()
        ):
            raise ReleaseRunnerV2Error(
                "dispatcher returned crossed dispatch authority"
            )
        retained_attempt = self._evidence_store.dispatch_attempt_state(
            plan=self._journal.plan,
            transaction=self._journal.current,
            journal_path=self._journal.path,
            journal_execution_id=self._journal.journal_execution_id,
        )
        if retained_attempt.attempt != canonical_attempt:
            raise ReleaseRunnerV2Error(
                "dispatcher returned a non-retained dispatch authority"
            )
        if self._journal.current.state != "UNCERTAIN":
            raise ReleaseRunnerV2Error(
                "dispatcher changed the journal outside observer reconciliation"
            )
        return self._result(resolution, action="DISPATCHED_UNCERTAIN")

    def run_one(self) -> ReleaseRunnerStepResultV2 | None:
        if self._journal.current.state == "NEW":
            self._journal.advance_preflight()
        if self._journal.current.state in {
            "VERIFIED",
            "ABORTED_RETAINED",
            "ROLLED_BACK",
        }:
            return None
        was_uncertain = self._journal.current.state == "UNCERTAIN"
        if not was_uncertain:
            count = self._journal.current.completed_step_count
            if count >= len(self._journal.plan.steps):
                raise ReleaseRunnerV2Error("nonterminal release has no current step")
            if self._journal.plan.steps[count].mutation:
                self._journal.begin_step()
        resolution = resolve_current_step_v2(
            self._journal, self._evidence_store
        )
        if resolution is None:
            return None
        if was_uncertain:
            return self._observe(resolution, uncertain=True)
        if not resolution.step.mutation:
            return self._observe(resolution, uncertain=False)
        return self._dispatch(resolution)


__all__ = [
    "RELEASE_KIND_ROUTES_V2",
    "ReleaseProviderCollaboratorV2",
    "ReleaseProviderRouteV2",
    "ReleaseRunnerCollaboratorsV2",
    "ReleaseRunnerStepResultV2",
    "ReleaseRunnerV2",
    "ReleaseRunnerV2Error",
    "ResolvedReleaseStepV2",
    "resolve_current_step_v2",
]
