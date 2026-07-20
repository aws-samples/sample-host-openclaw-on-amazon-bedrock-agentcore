"""Accepted, closed in-package controller for clean-account release v2.

The controller is the sole composition root for :class:`ReleaseRunnerV2`.
Provider selection remains the runner's immutable route table; this module
only supplies exact typed collaborators built from an authenticated AWS
authority and pinned local stores.  Provider acknowledgements are never used
as completion evidence: every uncertain mutation is reconciled by an existing
production observer.

The AgentCore lane durably retains its exact two-read precondition before
dispatch, then reloads that precondition and the resulting-version receipt
after restart.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from pathlib import Path
from types import MappingProxyType
from typing import Iterator, Mapping

from release_tools.asset_publication_v2 import S3AssetPublisher
from release_tools.agentcore_hardening_v2 import (
    AgentCoreHardeningDispatcherV1,
    AgentCoreHardeningInspectorV1,
    AgentCoreHardeningObserverV1,
    AgentCoreHardeningOperationV1,
    AgentCoreHardeningPreconditionV1,
    _verified_retained_receipt as _verified_retained_agentcore_receipt,
    validate_agentcore_hardening_authority,
    validate_agentcore_hardening_preflight,
)
from release_tools.aws_authority_v2 import (
    AttestedAwsClientV2,
    AuthenticatedAwsAuthorityV2,
    AwsAuthorityError,
)
from release_tools.baseline_observer_v2 import (
    BaselineObservationRequestV1,
    BaselineObserverV2,
)
from release_tools.cloudformation_v2 import (
    CloudFormationMutationDispatcher,
    CloudFormationOperationV2,
    validate_cloudformation_preflight,
)
from release_tools.contracts import (
    ContractError,
    MAX_PRIVATE_MUTATION_ARTIFACT_BYTES,
    PrivateMutationEnvelopeV2,
    ReleasePlanV2,
    ResolvedMutationRequestV2,
    StagingTransactionV2,
    VerifiedPrivateMutationV2,
    canonical_json_bytes,
)
from release_tools.dispatch_attempt_v2 import (
    FreshDispatchAuthorityV1,
    ReleaseDispatchAttemptV1,
)
from release_tools.evidence_store_v2 import (
    EvidenceStoreV2Error,
    ReleaseEvidenceStoreV2,
)
from release_tools.image_publication import (
    ArtifactSubstitutionError,
    EcrImagePublisher,
    ImagePublicationEffectV1,
    validate_image_publication_preflight,
)
from release_tools.production_observer_v2 import ProductionObserverV2
from release_tools.release_artifact_store_v2 import (
    ReleaseArtifactBundleV2,
    ReleaseArtifactStoreV2Error,
)
from release_tools.release_runner_v2 import (
    RELEASE_KIND_ROUTES_V2,
    ReleaseProviderRouteV2,
    ReleaseRunnerCollaboratorsV2,
    ReleaseRunnerStepResultV2,
    ReleaseRunnerV2,
    ResolvedReleaseStepV2,
)
from release_tools.release_verifier_v2 import ReleaseVerifierV2
from release_tools.runtime_context_v2 import (
    RuntimeContextFileV2,
    RuntimeContextWriteRequestV2,
    derive_trusted_runtime_context_inputs,
)
from release_tools.runtime_iam_observer_v2 import (
    RuntimeIamObservationRequestV1,
    RuntimeIamObserverV2,
    exact_operation_tags,
)
from release_tools.stack_drift_v2 import (
    StackDriftDispatcherV1,
    StackDriftObserverV1,
    StackDriftOperationV1,
    _predecessor_stack_id,
    _verified_retained_receipt as _verified_retained_stack_drift_receipt,
    validate_stack_drift_dispatch,
    validate_stack_drift_preflight,
)
from release_tools.transaction import TransactionJournalV2


class ReleaseControllerV2Error(RuntimeError):
    """The accepted composition root or current closed route is invalid."""


@dataclass(frozen=True, slots=True)
class AcceptedReleaseRouteSupportV2:
    lane: str
    mutation: bool
    supported: bool
    implementation: str

    def __post_init__(self) -> None:
        if self.lane not in {
            "agentcore",
            "cloudformation",
            "ecr",
            "local_filesystem",
            "s3",
            "verifier",
        }:
            raise ReleaseControllerV2Error("accepted route lane is invalid")
        if not isinstance(self.mutation, bool) or not isinstance(
            self.supported, bool
        ):
            raise ReleaseControllerV2Error("accepted route mode is invalid")
        if not self.implementation:
            raise ReleaseControllerV2Error(
                "accepted route implementation is missing"
            )


def _support(kind: str, implementation: str) -> AcceptedReleaseRouteSupportV2:
    route = RELEASE_KIND_ROUTES_V2[kind]
    return AcceptedReleaseRouteSupportV2(
        lane=route.lane,
        mutation=route.mutation,
        supported=True,
        implementation=implementation,
    )


ACCEPTED_RELEASE_ROUTE_SUPPORT_V2: Mapping[
    str, AcceptedReleaseRouteSupportV2
] = MappingProxyType(
    {
        "BASELINE_OBSERVE": _support("BASELINE_OBSERVE", "BaselineObserverV2"),
        "BOOTSTRAP_STACK": _support(
            "BOOTSTRAP_STACK", "CloudFormationMutationDispatcher"
        ),
        "ASSET_PUBLISH": _support("ASSET_PUBLISH", "S3AssetPublisher"),
        "AGENTCORE_HARDEN": _support(
            "AGENTCORE_HARDEN",
            "AgentCoreHardeningDispatcherV1",
        ),
        "STACK_CREATE": _support(
            "STACK_CREATE", "CloudFormationMutationDispatcher"
        ),
        "STACK_UPDATE": _support(
            "STACK_UPDATE", "CloudFormationMutationDispatcher"
        ),
        "STACK_DRIFT_CHECK": _support(
            "STACK_DRIFT_CHECK", "StackDriftDispatcherV1"
        ),
        "IMAGE_PUBLISH": _support("IMAGE_PUBLISH", "EcrImagePublisher"),
        "IMAGE_OBSERVE": _support("IMAGE_OBSERVE", "ProductionObserverV2"),
        "RUNTIME_CONTEXT_WRITE": _support(
            "RUNTIME_CONTEXT_WRITE", "RuntimeContextFileV2"
        ),
        "CHANGESET_CREATE": _support(
            "CHANGESET_CREATE", "CloudFormationMutationDispatcher"
        ),
        "CHANGESET_EXECUTE": _support(
            "CHANGESET_EXECUTE", "CloudFormationMutationDispatcher"
        ),
        "VERIFY": _support("VERIFY", "ReleaseVerifierV2"),
    }
)


def _exact_directory(value: object, *, label: str) -> Path:
    if not isinstance(value, Path) or not value.is_absolute() or value.name in {
        "",
        ".",
        "..",
    }:
        raise ReleaseControllerV2Error(f"{label} is invalid")
    return value


class _ControllerStateV2:
    """Closed state shared only by the six fixed runner lanes."""

    def __init__(
        self,
        *,
        plan: ReleasePlanV2,
        authority: AuthenticatedAwsAuthorityV2,
        journal: TransactionJournalV2,
        evidence_store: ReleaseEvidenceStoreV2,
        artifact_bundle: ReleaseArtifactBundleV2,
        envelope_directory: Path,
        scratch_directory: Path,
        runtime_context_root: Path,
    ) -> None:
        if type(plan) is not ReleasePlanV2:
            raise ReleaseControllerV2Error(
                "controller requires one concrete release plan"
            )
        if type(authority) is not AuthenticatedAwsAuthorityV2:
            raise ReleaseControllerV2Error(
                "controller requires authenticated AWS authority"
            )
        if type(journal) is not TransactionJournalV2:
            raise ReleaseControllerV2Error(
                "controller requires one concrete v2 journal"
            )
        if type(evidence_store) is not ReleaseEvidenceStoreV2:
            raise ReleaseControllerV2Error(
                "controller requires one concrete evidence store"
            )
        if type(artifact_bundle) is not ReleaseArtifactBundleV2:
            raise ReleaseControllerV2Error(
                "controller requires one pinned artifact bundle"
            )
        try:
            canonical = ReleasePlanV2.from_bytes(plan.to_bytes())
            journal_plan = ReleasePlanV2.from_bytes(journal.plan.to_bytes())
            bundle_plan = ReleasePlanV2.from_bytes(
                artifact_bundle.plan.to_bytes()
            )
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise ReleaseControllerV2Error(
                "controller plan authority is not canonical"
            ) from error
        if canonical != plan or journal_plan != canonical or bundle_plan != canonical:
            raise ReleaseControllerV2Error(
                "controller plan, journal, and artifact bundle differ"
            )
        if journal.evidence_store is not evidence_store:
            raise ReleaseControllerV2Error(
                "controller evidence store is not journal-bound"
            )
        if (authority.account, authority.region) != (
            canonical.account,
            canonical.region,
        ):
            raise ReleaseControllerV2Error(
                "controller AWS authority crosses the release plan"
            )
        envelope = _exact_directory(
            envelope_directory, label="private envelope directory"
        )
        scratch = _exact_directory(
            scratch_directory, label="private snapshot directory"
        )
        context_root = _exact_directory(
            runtime_context_root, label="runtime context root"
        )
        if len({envelope, scratch, context_root}) != 3:
            raise ReleaseControllerV2Error(
                "controller private directories must be separate"
            )

        self.plan = canonical
        self.authority = authority
        self.journal = journal
        self.evidence_store = evidence_store
        self.artifact_bundle = artifact_bundle
        self.envelope_directory = envelope
        self.scratch_directory = scratch
        self.runtime_context_file = RuntimeContextFileV2(context_root)

        try:
            self.cf_mutation = authority.mutation_client("cloudformation")
            self.s3_mutation = authority.mutation_client("s3")
            self.ecr_mutation = authority.mutation_client("ecr")
            self.agentcore_mutation = authority.mutation_client(
                "bedrock-agentcore-control"
            )
            self.cf_observer = authority.observer_client("cloudformation")
            self.s3_observer = authority.observer_client("s3")
            self.ecr_observer = authority.observer_client("ecr")
            self.agentcore_observer = authority.observer_client(
                "bedrock-agentcore-control"
            )
            self.signer_observer = authority.observer_client("signer")
            self.cloudtrail_observer = authority.observer_client("cloudtrail")
            self.iam_observer = authority.observer_client("iam")
        except AwsAuthorityError as error:
            raise ReleaseControllerV2Error(
                "controller AWS authority lacks its exact closed clients"
            ) from error

        self.cf_dispatcher = CloudFormationMutationDispatcher(self.cf_mutation)
        self.s3_publisher = S3AssetPublisher(self.s3_mutation)
        self.ecr_publisher = EcrImagePublisher(self.ecr_mutation)
        self.stack_drift_dispatcher = StackDriftDispatcherV1(self.cf_mutation)
        self.stack_drift_observer = StackDriftObserverV1(self.cf_observer)
        self.agentcore_hardening_inspector = AgentCoreHardeningInspectorV1(
            self.agentcore_observer
        )
        self.agentcore_hardening_dispatcher = AgentCoreHardeningDispatcherV1(
            self.agentcore_mutation
        )
        self.agentcore_hardening_observer = AgentCoreHardeningObserverV1(
            self.agentcore_observer
        )
        self.baseline_observer = BaselineObserverV2(
            account=canonical.account,
            region=canonical.region,
            cloudformation=self.cf_observer,
        )
        self.production_observer = ProductionObserverV2(
            account=canonical.account,
            region=canonical.region,
            s3=self.s3_observer,
            cloudformation=self.cf_observer,
            ecr=self.ecr_observer,
            agentcore=self.agentcore_observer,
            signer=self.signer_observer,
            cloudtrail=self.cloudtrail_observer,
        )
        self.image_plan, self.image_preflight = self._image_preflight()

    def _artifact(self, logical_path: str) -> bytes:
        candidates = tuple(
            item for item in self.plan.artifacts if item.path == logical_path
        )
        if len(candidates) != 1:
            raise ReleaseControllerV2Error(
                "controller request artifact is missing or ambiguous"
            )
        expected = candidates[0]
        try:
            payload = b"".join(
                self.artifact_bundle.iter_verified_chunks(logical_path)
            )
        except (ReleaseArtifactStoreV2Error, OSError) as error:
            raise ReleaseControllerV2Error(
                "controller request artifact could not be verified"
            ) from error
        if (
            len(payload) != expected.size
            or hashlib.sha256(payload).hexdigest() != expected.sha256
        ):
            raise ReleaseControllerV2Error(
                "controller request artifact differs from the release plan"
            )
        return payload

    def _image_preflight(self):
        image_steps = tuple(step for step in self.plan.steps if step.phase == "image")
        observe = tuple(step for step in image_steps if step.kind == "IMAGE_OBSERVE")
        effects = tuple(step for step in image_steps if step.kind == "IMAGE_PUBLISH")
        if len(observe) != 1 or not effects:
            raise ReleaseControllerV2Error(
                "controller image route inventory is incomplete"
            )
        publication_payload = self._artifact(observe[0].request_artifact)
        parsed: list[ImagePublicationEffectV1] = []
        try:
            for step in effects:
                prefix = "image-"
                if not step.step_id.startswith(prefix):
                    raise ArtifactSubstitutionError(
                        "image effect step identity is invalid"
                    )
                payload = self._artifact(step.request_artifact)
                parsed.append(
                    ImagePublicationEffectV1.from_private_bytes(
                        payload,
                        expected_private_file_sha256=step.request_sha256,
                        expected_effect_id=step.step_id.removeprefix(prefix),
                        expected_publication_plan_sha256=observe[0].request_sha256,
                    )
                )
            return validate_image_publication_preflight(
                publication_payload,
                tuple(parsed),
                release_plan=self.plan,
            )
        except (ArtifactSubstitutionError, ContractError, ValueError) as error:
            raise ReleaseControllerV2Error(
                "controller image preflight could not be closed"
            ) from error

    def current(self) -> StagingTransactionV2:
        try:
            current = StagingTransactionV2.from_bytes(
                self.journal.current.to_bytes(), plan=self.plan
            )
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise ReleaseControllerV2Error(
                "controller journal cursor is not canonical"
            ) from error
        if self.journal.evidence_store is not self.evidence_store:
            raise ReleaseControllerV2Error(
                "controller journal evidence binding changed"
            )
        return current

    def validate_resolution(
        self, resolution: ResolvedReleaseStepV2
    ) -> StagingTransactionV2:
        if type(resolution) is not ResolvedReleaseStepV2:
            raise ReleaseControllerV2Error(
                "controller route requires the exact resolved step"
            )
        current = self.current()
        if current.completed_step_count >= len(self.plan.steps):
            raise ReleaseControllerV2Error(
                "controller journal has no exact current step"
            )
        expected = self.plan.steps[current.completed_step_count]
        route = RELEASE_KIND_ROUTES_V2.get(expected.kind)
        if (
            resolution.step != expected
            or route is None
            or resolution.route != route
            or resolution.route.mutation != expected.mutation
        ):
            raise ReleaseControllerV2Error(
                "controller collaborator input crosses the journal cursor"
            )
        resolved = resolution.resolved_request
        if expected.mutation:
            if type(resolved) is not ResolvedMutationRequestV2:
                raise ReleaseControllerV2Error(
                    "controller mutation lacks its exact resolved request"
                )
            try:
                resolved.validate_transaction(self.plan, current)
            except (ContractError, TypeError, ValueError) as error:
                raise ReleaseControllerV2Error(
                    "controller resolved mutation crosses the journal"
                ) from error
        elif resolved is not None:
            raise ReleaseControllerV2Error(
                "controller read-only route carries mutation authority"
            )
        return current

    def validate_verified(
        self,
        resolution: ResolvedReleaseStepV2,
        verified: VerifiedPrivateMutationV2,
    ) -> None:
        current = self.validate_resolution(resolution)
        if type(verified) is not VerifiedPrivateMutationV2:
            raise ReleaseControllerV2Error(
                "controller mutation lacks a verified private snapshot"
            )
        try:
            if verified.resolved_request != resolution.resolved_request:
                raise ReleaseControllerV2Error(
                    "controller verified mutation crosses its resolution"
                )
            verified.resolved_request.validate_transaction(self.plan, current)
        except ContractError as error:
            raise ReleaseControllerV2Error(
                "controller verified mutation is closed or invalid"
            ) from error

    def require_lane(
        self, resolution: ResolvedReleaseStepV2, *, lane: str
    ) -> StagingTransactionV2:
        current = self.validate_resolution(resolution)
        if resolution.route.lane != lane:
            raise ReleaseControllerV2Error(
                "controller collaborator input crosses its fixed lane"
            )
        return current

    def _envelope_path(self, resolution: ResolvedReleaseStepV2) -> Path:
        current = self.validate_resolution(resolution)
        resolved = resolution.resolved_request
        if resolved is None:
            raise ReleaseControllerV2Error(
                "controller observation lacks a resolved mutation"
            )
        return self.envelope_directory / (
            f"{resolution.step.ordinal:04d}-"
            f"r{current.revision:08d}-"
            f"{resolution.step.step_id}-{resolved.digest()}.private-mutation"
        )

    @contextmanager
    def open_envelope(
        self, resolution: ResolvedReleaseStepV2
    ) -> Iterator[VerifiedPrivateMutationV2]:
        current = self.validate_resolution(resolution)
        try:
            with PrivateMutationEnvelopeV2.open_verified(
                self._envelope_path(resolution),
                plan=self.plan,
                transaction=current,
                scratch_dir=self.scratch_directory,
            ) as verified:
                self.validate_verified(resolution, verified)
                yield verified
        except ReleaseControllerV2Error:
            raise
        except (ContractError, OSError, ValueError) as error:
            raise ReleaseControllerV2Error(
                "controller retained mutation envelope is unavailable"
            ) from error

    def retained_prefix(self, transaction: StagingTransactionV2):
        try:
            return self.evidence_store.retained_prefix_for_execution(
                plan=self.plan,
                transaction=transaction,
                journal_path=self.journal.path,
                journal_execution_id=self.journal.journal_execution_id,
            )
        except (EvidenceStoreV2Error, OSError, TypeError, ValueError) as error:
            raise ReleaseControllerV2Error(
                "controller retained evidence prefix is unavailable"
            ) from error

    def stable_before_intent(self) -> StagingTransactionV2:
        current = self.current()
        if current.state != "UNCERTAIN" or current.revision < 1:
            return current
        raw = current.to_mapping()
        raw.update(
            {
                "state": current.last_stable_state,
                "uncertainStepId": "",
                "uncertainOperationSha256": "",
                "revision": current.revision - 1,
            }
        )
        try:
            return StagingTransactionV2.from_mapping(raw, plan=self.plan)
        except (ContractError, TypeError, ValueError) as error:
            raise ReleaseControllerV2Error(
                "controller cannot reconstruct the exact pre-intent boundary"
            ) from error

    def assert_current_route_supported(self) -> None:
        """Stop before the runner can write intent or arm a dispatch marker."""

        current = self.current()
        if current.state in {"VERIFIED", "ABORTED_RETAINED", "ROLLED_BACK"}:
            return
        if current.completed_step_count >= len(self.plan.steps):
            raise ReleaseControllerV2Error(
                "controller journal has no exact current step"
            )
        kind = self.plan.steps[current.completed_step_count].kind
        support = ACCEPTED_RELEASE_ROUTE_SUPPORT_V2.get(kind)
        if support is None:
            raise ReleaseControllerV2Error(
                "controller current kind has no accepted route"
            )
        if not support.supported:
            raise ReleaseControllerV2Error(
                f"{kind} is not an accepted release route"
            )


class _CloudFormationLaneV2:
    def __init__(self, state: _ControllerStateV2) -> None:
        self._state = state

    def dispatch(
        self,
        *,
        resolution: ResolvedReleaseStepV2,
        verified_mutation: VerifiedPrivateMutationV2,
        fresh_authority: FreshDispatchAuthorityV1,
    ) -> ReleaseDispatchAttemptV1:
        self._state.require_lane(resolution, lane="cloudformation")
        self._state.validate_verified(resolution, verified_mutation)
        payload = verified_mutation.read_artifact_bytes(
            limit=MAX_PRIVATE_MUTATION_ARTIFACT_BYTES
        )
        if resolution.step.kind == "STACK_DRIFT_CHECK":
            operation = StackDriftOperationV1.from_bytes(payload)
            preflight = validate_stack_drift_preflight(
                operation, release_plan=self._state.plan
            )
            sink = self._state.evidence_store.stack_drift_receipt_sink(
                plan=self._state.plan,
                transaction=self._state.current(),
                journal_path=self._state.journal.path,
                journal_execution_id=self._state.journal.journal_execution_id,
            )
            dispatch = validate_stack_drift_dispatch(
                verified_mutation, preflight, sink
            )
            return self._state.stack_drift_dispatcher.dispatch(
                dispatch, fresh_authority
            )
        operation = CloudFormationOperationV2.from_bytes(payload)
        preflight = validate_cloudformation_preflight(
            operation, release_plan=self._state.plan
        )
        return self._state.cf_dispatcher.dispatch(
            verified_mutation,
            preflight,
            fresh_authority=fresh_authority,
        )

    def observe(self, *, resolution: ResolvedReleaseStepV2) -> object:
        current = self._state.require_lane(resolution, lane="cloudformation")
        if resolution.step.kind == "BASELINE_OBSERVE":
            request = BaselineObservationRequestV1.from_bytes(
                self._state._artifact(resolution.step.request_artifact)
            )
            return self._state.baseline_observer.observe(request)
        with self._state.open_envelope(resolution) as verified:
            payload = verified.read_artifact_bytes(
                limit=MAX_PRIVATE_MUTATION_ARTIFACT_BYTES
            )
            if resolution.step.kind == "STACK_DRIFT_CHECK":
                operation = StackDriftOperationV1.from_bytes(payload)
                preflight = validate_stack_drift_preflight(
                    operation, release_plan=self._state.plan
                )
                sink = self._state.evidence_store.stack_drift_receipt_sink(
                    plan=self._state.plan,
                    transaction=current,
                    journal_path=self._state.journal.path,
                    journal_execution_id=self._state.journal.journal_execution_id,
                )
                dispatch = validate_stack_drift_dispatch(
                    verified, preflight, sink
                )
                (
                    bound_operation,
                    resolved,
                    bound_sink,
                    plan,
                    transaction,
                    predecessor,
                ) = dispatch._binding()
                attempted, receipt_payload = bound_sink._load()
                if not attempted or receipt_payload is None:
                    raise ReleaseControllerV2Error(
                        "stack drift dispatch receipt is missing"
                    )
                stack_id = _predecessor_stack_id(
                    plan=plan,
                    transaction=transaction,
                    predecessor=predecessor,
                    operation=bound_operation,
                )
                receipt = _verified_retained_stack_drift_receipt(
                    receipt_payload,
                    resolved=resolved,
                    plan=plan,
                    transaction=transaction,
                    predecessor=predecessor,
                    stack_id=stack_id,
                )
                return self._state.stack_drift_observer.observe(receipt)
            operation = CloudFormationOperationV2.from_bytes(payload)
            preflight = validate_cloudformation_preflight(
                operation, release_plan=self._state.plan
            )
            if resolution.step.phase == "runtime":
                return self._state.production_observer.observe_agentcore_runtime_stack(
                    verified, preflight
                )
            if resolution.step.phase == "endpoint":
                return self._state.production_observer.observe_agentcore_endpoint(
                    verified, preflight
                )
            return self._state.production_observer.observe_cloudformation(
                verified, preflight
            )


class _S3LaneV2:
    def __init__(self, state: _ControllerStateV2) -> None:
        self._state = state

    def dispatch(
        self,
        *,
        resolution: ResolvedReleaseStepV2,
        verified_mutation: VerifiedPrivateMutationV2,
        fresh_authority: FreshDispatchAuthorityV1,
    ) -> ReleaseDispatchAttemptV1:
        self._state.require_lane(resolution, lane="s3")
        self._state.validate_verified(resolution, verified_mutation)
        return self._state.s3_publisher.publish(
            verified_mutation, fresh_authority=fresh_authority
        )

    def observe(self, *, resolution: ResolvedReleaseStepV2) -> object:
        self._state.require_lane(resolution, lane="s3")
        with self._state.open_envelope(resolution) as verified:
            return self._state.production_observer.observe_asset(verified)


class _EcrLaneV2:
    def __init__(self, state: _ControllerStateV2) -> None:
        self._state = state

    def dispatch(
        self,
        *,
        resolution: ResolvedReleaseStepV2,
        verified_mutation: VerifiedPrivateMutationV2,
        fresh_authority: FreshDispatchAuthorityV1,
    ) -> ReleaseDispatchAttemptV1:
        self._state.require_lane(resolution, lane="ecr")
        self._state.validate_verified(resolution, verified_mutation)
        return self._state.ecr_publisher.publish_effect(
            verified_mutation,
            self._state.image_preflight,
            fresh_authority=fresh_authority,
        )

    def observe(self, *, resolution: ResolvedReleaseStepV2) -> object:
        current = self._state.require_lane(resolution, lane="ecr")
        if resolution.step.kind == "IMAGE_OBSERVE":
            capability = self._state.image_preflight.bind_current_observe(
                release_plan=self._state.plan,
                transaction=current,
            )
            return self._state.production_observer.observe_image_release(capability)
        with self._state.open_envelope(resolution) as verified:
            return self._state.production_observer.observe_image_effect(
                verified, self._state.image_preflight
            )


class _AgentCoreLaneV2:
    def __init__(self, state: _ControllerStateV2) -> None:
        self._state = state

    def _authority(
        self,
        *,
        resolution: ResolvedReleaseStepV2,
        verified_mutation: VerifiedPrivateMutationV2,
    ):
        current = self._state.require_lane(resolution, lane="agentcore")
        if resolution.step.kind != "AGENTCORE_HARDEN":
            raise ReleaseControllerV2Error(
                "AgentCore collaborator received a crossed route"
            )
        self._state.validate_verified(resolution, verified_mutation)
        resolved = resolution.resolved_request
        if resolved is None:
            raise ReleaseControllerV2Error(
                "AgentCore collaborator lacks its resolved request"
            )
        operation = AgentCoreHardeningOperationV1.from_bytes(
            verified_mutation.read_artifact_bytes(
                limit=MAX_PRIVATE_MUTATION_ARTIFACT_BYTES
            )
        )
        preflight = validate_agentcore_hardening_preflight(
            operation, release_plan=self._state.plan
        )
        sink = self._state.evidence_store.agentcore_hardening_receipt_sink(
            plan=self._state.plan,
            transaction=current,
            journal_path=self._state.journal.path,
            journal_execution_id=self._state.journal.journal_execution_id,
        )
        authority = validate_agentcore_hardening_authority(
            resolved, preflight, current, sink
        )
        return authority, sink

    def dispatch(
        self,
        *,
        resolution: ResolvedReleaseStepV2,
        verified_mutation: VerifiedPrivateMutationV2,
        fresh_authority: FreshDispatchAuthorityV1,
    ) -> ReleaseDispatchAttemptV1:
        authority, _ = self._authority(
            resolution=resolution,
            verified_mutation=verified_mutation,
        )
        precondition = self._state.agentcore_hardening_inspector.inspect(
            authority
        )
        return self._state.agentcore_hardening_dispatcher.dispatch(
            authority, precondition, fresh_authority
        )

    def observe(self, *, resolution: ResolvedReleaseStepV2) -> object:
        with self._state.open_envelope(resolution) as verified:
            authority, sink = self._authority(
                resolution=resolution,
                verified_mutation=verified,
            )
            precondition_payload = sink._load_precondition()
            if precondition_payload is None:
                raise ReleaseControllerV2Error(
                    "AgentCore hardening precondition is missing"
                )
            precondition = AgentCoreHardeningPreconditionV1.from_bytes(
                precondition_payload
            )
            precondition._binding(authority)
            attempted, receipt_payload = sink._load()
            if not attempted or receipt_payload is None:
                raise ReleaseControllerV2Error(
                    "AgentCore hardening dispatch receipt is missing"
                )
            receipt = _verified_retained_agentcore_receipt(
                receipt_payload,
                authority=authority,
                precondition=precondition,
            )
            return self._state.agentcore_hardening_observer.observe(
                authority, receipt
            )


class _RuntimeContextLaneV2:
    def __init__(self, state: _ControllerStateV2) -> None:
        self._state = state

    def _inputs(self, resolution: ResolvedReleaseStepV2):
        current = self._state.require_lane(
            resolution, lane="local_filesystem"
        )
        stable = self._state.stable_before_intent()
        request = RuntimeContextWriteRequestV2.from_bytes(
            self._state._artifact(resolution.step.request_artifact)
        )
        trusted = derive_trusted_runtime_context_inputs(
            request=request,
            plan=self._state.plan,
            transaction=stable,
            # The store must audit the bytes of the *current* durable journal
            # (UNCERTAIN after the runner's write-ahead transition).  The
            # derivation itself intentionally receives the canonical prior
            # stable revision because context facts are owned by the completed
            # endpoint prefix, never by the write intent.
            retained_prefix=self._state.retained_prefix(current),
        )
        if (
            current.state == "UNCERTAIN"
            and trusted.operation_sha256 != current.uncertain_operation_sha256
        ):
            raise ReleaseControllerV2Error(
                "runtime context pre-intent authority crosses the current intent"
            )
        return request, trusted

    def dispatch(
        self,
        *,
        resolution: ResolvedReleaseStepV2,
        verified_mutation: VerifiedPrivateMutationV2,
        fresh_authority: FreshDispatchAuthorityV1,
    ) -> ReleaseDispatchAttemptV1:
        self._state.validate_verified(resolution, verified_mutation)
        request, trusted = self._inputs(resolution)
        resolved = resolution.resolved_request
        if resolved is None:
            raise ReleaseControllerV2Error(
                "runtime context dispatch lacks a resolved request"
            )
        return self._state.runtime_context_file.write(
            request=request,
            trusted_inputs=trusted,
            resolved_request=resolved,
            fresh_authority=fresh_authority,
        )

    def observe(self, *, resolution: ResolvedReleaseStepV2) -> object:
        request, trusted = self._inputs(resolution)
        return self._state.runtime_context_file.observe(
            request=request, trusted_inputs=trusted
        )


class _VerifierLaneV2:
    def __init__(self, state: _ControllerStateV2) -> None:
        self._state = state

    def observe(self, *, resolution: ResolvedReleaseStepV2) -> object:
        current = self._state.require_lane(resolution, lane="verifier")
        if resolution.step.kind != "VERIFY":
            raise ReleaseControllerV2Error(
                "verifier collaborator received a crossed route"
            )
        prefix = self._state.retained_prefix(current)
        foundation_values = tuple(
            record.step_observation.foundation_runtime_inputs
            for record in prefix
            if record.step_observation is not None
            and record.step_observation.foundation_runtime_inputs is not None
        )
        endpoint_steps = tuple(
            step
            for step in self._state.plan.steps
            if step.phase == "endpoint" and step.kind == "STACK_UPDATE"
        )
        if len(foundation_values) != 1 or len(endpoint_steps) != 1:
            raise ReleaseControllerV2Error(
                "release verifier retained authority is incomplete"
            )
        foundation = foundation_values[0]
        endpoint_operation = CloudFormationOperationV2.from_bytes(
            self._state._artifact(endpoint_steps[0].request_artifact)
        )
        body = endpoint_operation.reviewed_template_body
        tags = exact_operation_tags(
            source_commit=self._state.plan.source_commit,
            source_tree=self._state.plan.source_tree,
        )
        iam_request = RuntimeIamObservationRequestV1.from_mapping(
            {
                "schema": RuntimeIamObservationRequestV1.SCHEMA,
                "account": self._state.plan.account,
                "region": self._state.plan.region,
                "sourceCommit": self._state.plan.source_commit,
                "sourceTree": self._state.plan.source_tree,
                "stackId": current.agent_core_stack_id,
                "logicalRoleId": "ExecutionRole",
                "reviewedTemplateBody": body,
                "reviewedTemplateSha256": hashlib.sha256(
                    body.encode("utf-8")
                ).hexdigest(),
                "foundationRuntimeInputs": foundation.to_mapping(),
                "foundationInputsSha256": foundation.digest(),
                "operationTagsSha256": hashlib.sha256(
                    canonical_json_bytes({"tags": tags})
                ).hexdigest(),
            }
        )
        iam_observer = RuntimeIamObserverV2(
            account=self._state.plan.account,
            region=self._state.plan.region,
            iam=self._state.iam_observer,
        )
        verifier = ReleaseVerifierV2(
            runtime_iam_observer=iam_observer,
            runtime_iam_request=iam_request,
            agentcore=self._state.agentcore_observer,
            runtime_context_file=self._state.runtime_context_file,
        )
        return verifier.verify(
            plan=self._state.plan,
            transaction=current,
            journal_path=self._state.journal.path,
            journal_execution_id=self._state.journal.journal_execution_id,
            evidence_store=self._state.evidence_store,
        )


class AcceptedReleaseControllerV2:
    """Serial accepted controller; caller chooses neither route nor provider."""

    def __init__(
        self,
        *,
        plan: ReleasePlanV2,
        authority: AuthenticatedAwsAuthorityV2,
        journal: TransactionJournalV2,
        evidence_store: ReleaseEvidenceStoreV2,
        artifact_bundle: ReleaseArtifactBundleV2,
        envelope_directory: Path,
        scratch_directory: Path,
        runtime_context_root: Path,
    ) -> None:
        state = _ControllerStateV2(
            plan=plan,
            authority=authority,
            journal=journal,
            evidence_store=evidence_store,
            artifact_bundle=artifact_bundle,
            envelope_directory=envelope_directory,
            scratch_directory=scratch_directory,
            runtime_context_root=runtime_context_root,
        )
        cloudformation = _CloudFormationLaneV2(state)
        s3 = _S3LaneV2(state)
        ecr = _EcrLaneV2(state)
        agentcore = _AgentCoreLaneV2(state)
        local_filesystem = _RuntimeContextLaneV2(state)
        verifier = _VerifierLaneV2(state)
        collaborators = ReleaseRunnerCollaboratorsV2(
            artifact_bundle=state.artifact_bundle,
            envelope_directory=state.envelope_directory,
            scratch_directory=state.scratch_directory,
            cloudformation=cloudformation,
            s3=s3,
            ecr=ecr,
            agentcore=agentcore,
            local_filesystem=local_filesystem,
            verifier=verifier,
        )
        self._state = state
        self._collaborators = collaborators
        self._runner = ReleaseRunnerV2(
            journal=journal,
            evidence_store=evidence_store,
            collaborators=collaborators,
        )

    @property
    def collaborators(self) -> ReleaseRunnerCollaboratorsV2:
        """The exact closed bundle, exposed for static audit and test only."""

        return self._collaborators

    def run_one(self) -> ReleaseRunnerStepResultV2 | None:
        self._state.assert_current_route_supported()
        return self._runner.run_one()


__all__ = [
    "ACCEPTED_RELEASE_ROUTE_SUPPORT_V2",
    "AcceptedReleaseControllerV2",
    "AcceptedReleaseRouteSupportV2",
    "ReleaseControllerV2Error",
]
