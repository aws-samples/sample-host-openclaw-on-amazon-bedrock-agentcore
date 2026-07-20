"""Exact one-shot CloudFormation stack-drift dispatch and observation.

This module has no SDK construction, credential, filesystem, process, or
journal authority.  It consumes an unforgeable retained mutation snapshot, an
attested single-service AWS client, and a separately minted append-only receipt
sink.  A durable attempt marker is written before ``DetectStackDrift`` so a
missing receipt after a possible effect can never authorize a retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping, Protocol

from release_tools.aws_authority_v2 import AttestedAwsClientV2, AwsAuthorityError
from release_tools.contracts import (
    ContractError,
    MAX_CONTRACT_BYTES,
    ReleasePlanV2,
    RetainedStepEvidenceV2,
    ResolvedMutationRequestV2,
    StackDriftDispatchReceiptV1,
    StagingTransactionV2,
    VerifiedPrivateMutationV2,
    canonical_json_bytes,
    parse_canonical_object,
)
from release_tools.dispatch_attempt_v2 import (
    DispatchAttemptError,
    FreshDispatchAuthorityV1,
    ReleaseDispatchAttemptV1,
)
from release_tools.production_observer_v2 import (
    CanonicalReadObservationV2,
    ProductionObserverV2Error,
    _new_observation,
)
from release_tools.transaction import ObservationDisposition


REQUIRED_REGION = "eu-west-1"
_ACCOUNT = re.compile(r"[0-9]{12}")
_SHA40 = re.compile(r"[0-9a-f]{40}")
_STACK_ID = re.compile(
    r"arn:aws:cloudformation:eu-west-1:([0-9]{12}):stack/"
    r"([A-Za-z][A-Za-z0-9-]{0,127})/([A-Za-z0-9-]{1,128})"
)
_STACKS = frozenset(
    {
        "CDKToolkit",
        "OpenClawVpc",
        "OpenClawSecurity",
        "OpenClawGuardrails",
        "PersonalOperatorCapabilities",
        "OpenClawAgentCore",
        "OpenClawObservability",
        "OpenClawRouter",
        "OpenClawCron",
        "PersonalOperatorScheduler",
        "PersonalOperatorWeb",
    }
)
_DRIFT_PHASES = frozenset(
    {"foundation", "runtime", "endpoint", "router-cron", "scheduler", "web"}
)
_DRIFT_OCCURRENCES = {
    ("foundation", "CDKToolkit"): "foundation-drift-cdktoolkit",
    ("foundation", "OpenClawVpc"): "foundation-drift-openclawvpc",
    ("foundation", "OpenClawSecurity"): "foundation-drift-openclawsecurity",
    ("foundation", "OpenClawGuardrails"): "foundation-drift-openclawguardrails",
    (
        "foundation",
        "PersonalOperatorCapabilities",
    ): "foundation-drift-personaloperatorcapabilities",
    (
        "foundation",
        "OpenClawAgentCore",
    ): "foundation-drift-openclawagentcore",
    (
        "foundation",
        "OpenClawObservability",
    ): "foundation-drift-openclawobservability",
    ("runtime", "OpenClawAgentCore"): "runtime-drift-agentcore",
    ("endpoint", "OpenClawAgentCore"): "endpoint-drift-agentcore",
    ("router-cron", "OpenClawRouter"): "router-cron-drift-openclawrouter",
    ("router-cron", "OpenClawCron"): "router-cron-drift-openclawcron",
    (
        "scheduler",
        "PersonalOperatorScheduler",
    ): "scheduler-drift-personaloperatorscheduler",
    ("web", "PersonalOperatorWeb"): "web-drift-personaloperatorweb",
}
_MAX_RESOURCE_DRIFT_PAGES = 100
_RESOURCE_DRIFT_PAGE_SIZE = 100
_REVIEWED_RESOURCE_DRIFT_STATUSES = ("IN_SYNC", "MODIFIED", "DELETED")


class StackDriftError(RuntimeError):
    """A requested drift action crosses the closed release boundary."""


class StackDriftDispatchAmbiguous(StackDriftError):
    """A dispatch may have taken effect and must remain UNCERTAIN."""


class StackDriftObservationAmbiguous(StackDriftError):
    """A read did not produce stable authoritative drift evidence."""


def _text(value: object, *, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise StackDriftError(f"{label} is invalid")
    return value


_OPERATION_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class StackDriftOperationV1:
    """Canonical pre-cloud request naming one reviewed logical stack."""

    SCHEMA = "personal-operator.stack-drift-operation.v1"
    FIELDS = {
        "schema",
        "account",
        "region",
        "sourceCommit",
        "sourceTree",
        "stackName",
        "phase",
        "occurrence",
    }

    account: str
    region: str
    source_commit: str
    source_tree: str
    stack_name: str
    phase: str
    occurrence: str

    def __init__(
        self,
        *,
        account: str,
        region: str,
        source_commit: str,
        source_tree: str,
        stack_name: str,
        phase: str,
        occurrence: str,
        _token: object | None = None,
    ) -> None:
        if _token is not _OPERATION_TOKEN:
            raise StackDriftError("stack drift operation is not directly constructible")
        object.__setattr__(self, "account", account)
        object.__setattr__(self, "region", region)
        object.__setattr__(self, "source_commit", source_commit)
        object.__setattr__(self, "source_tree", source_tree)
        object.__setattr__(self, "stack_name", stack_name)
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "occurrence", occurrence)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "StackDriftOperationV1":
        if not isinstance(raw, Mapping) or set(raw) != cls.FIELDS:
            raise StackDriftError("stack drift operation fields are not exact")
        if raw.get("schema") != cls.SCHEMA:
            raise StackDriftError("stack drift operation schema is invalid")
        account = _text(raw.get("account"), label="account", pattern=_ACCOUNT)
        if account == "000000000000":
            raise StackDriftError("account is invalid")
        region = _text(raw.get("region"), label="region")
        if region != REQUIRED_REGION:
            raise StackDriftError(
                f"stack drift region must be exactly {REQUIRED_REGION}"
            )
        commit = _text(
            raw.get("sourceCommit"), label="source commit", pattern=_SHA40
        )
        tree = _text(raw.get("sourceTree"), label="source tree", pattern=_SHA40)
        stack_name = _text(raw.get("stackName"), label="stack name")
        if stack_name not in _STACKS:
            raise StackDriftError("stack name is outside the release catalog")
        phase = _text(raw.get("phase"), label="phase")
        occurrence = _text(raw.get("occurrence"), label="occurrence")
        if _DRIFT_OCCURRENCES.get((phase, stack_name)) != occurrence:
            raise StackDriftError(
                "stack drift phase and occurrence are outside the release catalog"
            )
        return cls(
            account=account,
            region=region,
            source_commit=commit,
            source_tree=tree,
            stack_name=stack_name,
            phase=phase,
            occurrence=occurrence,
            _token=_OPERATION_TOKEN,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "StackDriftOperationV1":
        try:
            return cls.from_mapping(parse_canonical_object(payload))
        except StackDriftError:
            raise
        except (ContractError, TypeError, ValueError) as error:
            raise StackDriftError("stack drift operation is not canonical") from error

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "account": self.account,
            "region": self.region,
            "sourceCommit": self.source_commit,
            "sourceTree": self.source_tree,
            "stackName": self.stack_name,
            "phase": self.phase,
            "occurrence": self.occurrence,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    @property
    def subject(self) -> str:
        return (
            f"cfn:{self.account}:{self.region}:stack:{self.stack_name}:"
            f"release:{self.source_commit}:drift"
        )


_PREFLIGHT_TOKEN = object()


class VerifiedStackDriftPreflightV1:
    """Capability binding one canonical drift request to one exact plan step."""

    __slots__ = ("_plan_sha256", "_request_sha256", "_operation", "_plan")

    def __init__(
        self,
        *,
        release_plan_sha256: str,
        request_sha256: str,
        operation: StackDriftOperationV1,
        release_plan: ReleasePlanV2 | None = None,
        _token: object | None = None,
    ) -> None:
        if _token is not _PREFLIGHT_TOKEN:
            raise StackDriftError("verified stack drift preflight is not constructible")
        self._plan_sha256 = release_plan_sha256
        self._request_sha256 = request_sha256
        self._operation = operation
        if not isinstance(release_plan, ReleasePlanV2):
            raise StackDriftError("verified stack drift preflight lacks its plan")
        self._plan = release_plan

    def _bind_verified(
        self, verified: VerifiedPrivateMutationV2
    ) -> tuple[StackDriftOperationV1, ResolvedMutationRequestV2]:
        if not isinstance(verified, VerifiedPrivateMutationV2):
            raise StackDriftError(
                "stack drift dispatch requires a verified private mutation"
            )
        try:
            resolved = verified.resolved_request
            metadata = verified.metadata
            payload = verified.read_artifact_bytes(limit=MAX_CONTRACT_BYTES)
        except ContractError as error:
            raise StackDriftError(
                "stack drift verified mutation is closed or invalid"
            ) from error
        operation = StackDriftOperationV1.from_bytes(payload)
        request = resolved.mutation_request
        if (
            request.plan_sha256 != self._plan_sha256
            or request.request_sha256 != self._request_sha256
            or metadata.request_artifact_sha256 != self._request_sha256
            or operation != self._operation
        ):
            raise StackDriftError(
                "stack drift verified mutation differs from exact preflight"
            )
        if (
            request.kind != "STACK_DRIFT_CHECK"
            or request.subject != operation.subject
            or request.step_id != operation.occurrence
            or resolved.step_phase != operation.phase
            or (
                resolved.account,
                resolved.region,
                resolved.source_commit,
                resolved.source_tree,
            )
            != (
                operation.account,
                operation.region,
                operation.source_commit,
                operation.source_tree,
            )
            or not resolved.predecessor_stack_id
            or not resolved.predecessor_evidence_sha256
            or not resolved.predecessor_observer_evidence_sha256
        ):
            raise StackDriftError(
                "stack drift resolved request differs from its predecessor authority"
            )
        _validate_stack_id(
            resolved.predecessor_stack_id,
            account=operation.account,
            stack_name=operation.stack_name,
        )
        return operation, resolved


def validate_stack_drift_preflight(
    operation: StackDriftOperationV1,
    *,
    release_plan: ReleasePlanV2,
) -> VerifiedStackDriftPreflightV1:
    """Bind one exact request artifact to its unique mandatory drift step."""

    if not isinstance(operation, StackDriftOperationV1) or not isinstance(
        release_plan, ReleasePlanV2
    ):
        raise StackDriftError("stack drift preflight inputs are invalid")
    try:
        canonical_operation = StackDriftOperationV1.from_bytes(operation.to_bytes())
        plan = ReleasePlanV2.from_bytes(release_plan.to_bytes())
    except (ContractError, StackDriftError) as error:
        raise StackDriftError("stack drift preflight inputs are invalid") from error
    if (
        canonical_operation.account,
        canonical_operation.region,
        canonical_operation.source_commit,
        canonical_operation.source_tree,
    ) != (plan.account, plan.region, plan.source_commit, plan.source_tree):
        raise StackDriftError("stack drift operation crosses its release-plan identity")
    payload = canonical_operation.to_bytes()
    request_sha256 = hashlib.sha256(payload).hexdigest()
    matches = tuple(
        step for step in plan.steps if step.request_sha256 == request_sha256
    )
    if len(matches) != 1:
        raise StackDriftError("stack drift operation is not uniquely planned")
    step = matches[0]
    artifact = next(
        (item for item in plan.artifacts if item.path == step.request_artifact), None
    )
    if (
        step.kind != "STACK_DRIFT_CHECK"
        or step.subject != canonical_operation.subject
        or step.phase != canonical_operation.phase
        or step.step_id != canonical_operation.occurrence
        or artifact is None
        or artifact.sha256 != request_sha256
        or artifact.size != len(payload)
        or step.expected_request_sha256 != request_sha256
        or step.expected_template_sha256
        or step.expected_template_parameter_sha256
        or step.expected_observed_request_sha256
        or step.expected_content_sha256
    ):
        raise StackDriftError("stack drift operation differs from its exact plan step")
    return VerifiedStackDriftPreflightV1(
        release_plan_sha256=plan.digest(),
        request_sha256=request_sha256,
        operation=canonical_operation,
        release_plan=plan,
        _token=_PREFLIGHT_TOKEN,
    )


class _ReceiptBackend(Protocol):
    def load(self) -> tuple[bool, bytes | None]: ...

    def begin_attempt(self) -> bool: ...

    def retain(self, payload: bytes) -> None: ...


_SINK_TOKEN = object()


class StackDriftReceiptSinkV1:
    """Opaque capability over one exact append-only dispatch-receipt slot."""

    __slots__ = (
        "_backend",
        "_transaction",
        "_predecessor",
        "_pinned_payload",
    )

    def __init__(
        self,
        *,
        backend: _ReceiptBackend,
        transaction: StagingTransactionV2,
        predecessor_evidence: RetainedStepEvidenceV2,
        _token: object | None = None,
    ) -> None:
        if _token is not _SINK_TOKEN:
            raise StackDriftError("stack drift receipt sink is not constructible")
        self._backend = backend
        self._transaction = transaction
        self._predecessor = predecessor_evidence
        self._pinned_payload: bytes | None = None

    def _authority(
        self,
        plan: ReleasePlanV2,
    ) -> tuple[StagingTransactionV2, RetainedStepEvidenceV2]:
        try:
            transaction = StagingTransactionV2.from_bytes(
                self._transaction.to_bytes(), plan=plan
            )
            predecessor = RetainedStepEvidenceV2.from_bytes(
                self._predecessor.to_bytes()
            )
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise StackDriftError(
                "stack drift sink transaction or predecessor authority is invalid"
            ) from error
        if (
            transaction.state != "UNCERTAIN"
            or transaction.completed_step_count < 1
            or not transaction.completed_steps
            or transaction.completed_steps[-1].evidence_sha256
            != predecessor.digest()
            or transaction.revision != predecessor.journal_revision + 2
        ):
            raise StackDriftError(
                "stack drift sink transaction and predecessor authority differ"
            )
        return transaction, predecessor

    def _load(self) -> tuple[bool, bytes | None]:
        try:
            result = self._backend.load()
        except Exception as error:
            raise StackDriftError("stack drift receipt sink cannot be read") from error
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not isinstance(result[0], bool)
            or (result[1] is not None and not isinstance(result[1], bytes))
            or (result[1] is not None and not result[0])
        ):
            raise StackDriftError("stack drift receipt sink state is invalid")
        payload = result[1]
        if payload is not None:
            if self._pinned_payload is None:
                self._pinned_payload = payload
            elif self._pinned_payload != payload:
                raise StackDriftError(
                    "retained stack drift receipt changed after exact load"
                )
        return result

    def _begin_attempt(self) -> bool:
        try:
            result = self._backend.begin_attempt()
        except Exception as error:
            raise StackDriftError(
                "stack drift receipt attempt cannot be retained"
            ) from error
        if not isinstance(result, bool):
            raise StackDriftError("stack drift receipt attempt result is invalid")
        return result

    def _retain(self, payload: bytes) -> None:
        try:
            self._backend.retain(payload)
        except Exception as error:
            raise StackDriftDispatchAmbiguous(
                "stack drift receipt was not retained; release remains UNCERTAIN"
            ) from error


def _new_stack_drift_receipt_sink(
    backend: _ReceiptBackend,
    *,
    transaction: StagingTransactionV2,
    predecessor_evidence: RetainedStepEvidenceV2,
) -> StackDriftReceiptSinkV1:
    """Trusted-package hook used by the evidence store to mint one sink."""

    if any(
        not callable(getattr(backend, method, None))
        for method in ("load", "begin_attempt", "retain")
    ):
        raise StackDriftError("stack drift receipt backend is invalid")
    if not isinstance(transaction, StagingTransactionV2) or not isinstance(
        predecessor_evidence, RetainedStepEvidenceV2
    ):
        raise StackDriftError(
            "stack drift receipt sink lacks exact transaction evidence"
        )
    try:
        canonical_transaction = StagingTransactionV2.from_bytes(
            transaction.to_bytes()
        )
        canonical_predecessor = RetainedStepEvidenceV2.from_bytes(
            predecessor_evidence.to_bytes()
        )
    except (ContractError, TypeError, ValueError) as error:
        raise StackDriftError(
            "stack drift receipt sink authority is not canonical"
        ) from error
    return StackDriftReceiptSinkV1(
        backend=backend,
        transaction=canonical_transaction,
        predecessor_evidence=canonical_predecessor,
        _token=_SINK_TOKEN,
    )


_DISPATCH_TOKEN = object()


class VerifiedStackDriftDispatchV1:
    """Private capability combining preflight, intent, and exact receipt sink."""

    __slots__ = ("_verified", "_preflight", "_sink")

    def __init__(
        self,
        *,
        verified: VerifiedPrivateMutationV2,
        preflight: VerifiedStackDriftPreflightV1,
        sink: StackDriftReceiptSinkV1,
        _token: object | None = None,
    ) -> None:
        if _token is not _DISPATCH_TOKEN:
            raise StackDriftError(
                "verified stack drift dispatch is not constructible"
            )
        self._verified = verified
        self._preflight = preflight
        self._sink = sink

    def _binding(
        self,
    ) -> tuple[
        StackDriftOperationV1,
        ResolvedMutationRequestV2,
        StackDriftReceiptSinkV1,
        ReleasePlanV2,
        StagingTransactionV2,
        RetainedStepEvidenceV2,
    ]:
        operation, resolved = self._preflight._bind_verified(self._verified)
        plan = ReleasePlanV2.from_bytes(self._preflight._plan.to_bytes())
        transaction, predecessor = self._sink._authority(plan)
        stack_id = _predecessor_stack_id(
            plan=plan,
            transaction=transaction,
            predecessor=predecessor,
            operation=operation,
        )
        probe = _expected_receipt(
            resolved=resolved,
            transaction=transaction,
            predecessor=predecessor,
            stack_id=stack_id,
            detection_id="00000000-0000-0000-0000-000000000000",
        )
        _validate_receipt_authority(
            probe,
            plan=plan,
            transaction=transaction,
            resolved=resolved,
            predecessor=predecessor,
        )
        return operation, resolved, self._sink, plan, transaction, predecessor


def validate_stack_drift_dispatch(
    verified: VerifiedPrivateMutationV2,
    preflight: VerifiedStackDriftPreflightV1,
    receipt_sink: StackDriftReceiptSinkV1,
) -> VerifiedStackDriftDispatchV1:
    """Mint dispatch authority only from all three independent capabilities."""

    if not isinstance(preflight, VerifiedStackDriftPreflightV1):
        raise StackDriftError("stack drift dispatch lacks verified preflight")
    if not isinstance(receipt_sink, StackDriftReceiptSinkV1):
        raise StackDriftError("stack drift dispatch lacks an exact receipt sink")
    preflight._bind_verified(verified)
    receipt_sink._load()
    authority = VerifiedStackDriftDispatchV1(
        verified=verified,
        preflight=preflight,
        sink=receipt_sink,
        _token=_DISPATCH_TOKEN,
    )
    authority._binding()
    return authority


_RECEIPT_TOKEN = object()


class VerifiedStackDriftReceiptV1:
    """Capability over one byte-exact synchronously retained receipt."""

    __slots__ = ("_receipt",)

    def __init__(
        self,
        *,
        receipt: StackDriftDispatchReceiptV1,
        _token: object | None = None,
    ) -> None:
        if _token is not _RECEIPT_TOKEN:
            raise StackDriftError("verified stack drift receipt is not constructible")
        self._receipt = receipt

    @property
    def receipt(self) -> StackDriftDispatchReceiptV1:
        return StackDriftDispatchReceiptV1.from_bytes(self._receipt.to_bytes())


def _predecessor_stack_id(
    *,
    plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
    predecessor: RetainedStepEvidenceV2,
    operation: StackDriftOperationV1,
) -> str:
    count = transaction.completed_step_count
    if count < 1 or count >= len(plan.steps):
        raise StackDriftError("stack drift predecessor position is invalid")
    step = plan.steps[count - 1]
    expected_pair: tuple[str, str, str]
    if step.kind in {"BOOTSTRAP_STACK", "STACK_CREATE", "CHANGESET_EXECUTE"}:
        expected_pair = ("cloudformation", "describe_stacks", "stackId")
    elif step.kind == "STACK_UPDATE" and step.phase == "runtime":
        expected_pair = (
            "bedrock-agentcore-control",
            "get_agent_runtime",
            "agentCoreStackId",
        )
    elif step.kind == "STACK_UPDATE" and step.phase == "endpoint":
        expected_pair = (
            "bedrock-agentcore-control",
            "get_agent_runtime_endpoint",
            "agentCoreStackId",
        )
    else:
        raise StackDriftError(
            "stack drift predecessor has no exact observer authority"
        )
    observer = predecessor.observer_evidence_mapping()
    service, observer_operation, stack_field = expected_pair
    projection = observer.get("projection")
    if (
        predecessor.disposition != "PRESENT"
        or observer.get("service") != service
        or observer.get("operation") != observer_operation
        or not isinstance(projection, Mapping)
        or {
            field
            for field in ("stackId", "agentCoreStackId")
            if field in projection
        }
        != {stack_field}
    ):
        raise StackDriftError(
            "stack drift predecessor observer authority is invalid"
        )
    stack_id = projection.get(stack_field)
    if not isinstance(stack_id, str):
        raise StackDriftError("stack drift predecessor StackId is invalid")
    return _validate_stack_id(
        stack_id,
        account=operation.account,
        stack_name=operation.stack_name,
    )


def _expected_receipt(
    *,
    resolved: ResolvedMutationRequestV2,
    transaction: StagingTransactionV2,
    predecessor: RetainedStepEvidenceV2,
    stack_id: str,
    detection_id: str,
) -> StackDriftDispatchReceiptV1:
    request = resolved.mutation_request
    try:
        return StackDriftDispatchReceiptV1.from_mapping(
            {
                "schema": StackDriftDispatchReceiptV1.SCHEMA,
                "releasePlanSha256": request.plan_sha256,
                "evidenceStoreSha256": predecessor.evidence_store_sha256,
                "journalPathSha256": predecessor.journal_path_sha256,
                "journalExecutionId": predecessor.journal_execution_id,
                "journalRevision": transaction.revision,
                "completedPrefixSha256": request.completed_prefix_sha256,
                "stepId": request.step_id,
                "subject": request.subject,
                "releaseOperationSha256": request.operation_sha256,
                "stackId": stack_id,
                "predecessorEvidenceSha256": predecessor.digest(),
                "predecessorObserverEvidenceSha256": (
                    predecessor.observer_evidence_sha256
                ),
                "driftDetectionId": detection_id,
            }
        )
    except ContractError as error:
        raise StackDriftError("stack drift dispatch receipt is invalid") from error


def _validate_receipt_authority(
    receipt: StackDriftDispatchReceiptV1,
    *,
    plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
    resolved: ResolvedMutationRequestV2,
    predecessor: RetainedStepEvidenceV2,
) -> None:
    try:
        receipt.validate_transaction(
            plan,
            transaction,
            resolved_request=resolved,
            predecessor_evidence=predecessor,
            evidence_store_sha256=predecessor.evidence_store_sha256,
            journal_path_sha256=predecessor.journal_path_sha256,
            journal_execution_id=predecessor.journal_execution_id,
        )
    except ContractError as error:
        raise StackDriftError(
            "stack drift receipt transaction authority is invalid"
        ) from error


def _verified_retained_receipt(
    payload: bytes,
    *,
    resolved: ResolvedMutationRequestV2,
    plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
    predecessor: RetainedStepEvidenceV2,
    stack_id: str,
) -> VerifiedStackDriftReceiptV1:
    try:
        receipt = StackDriftDispatchReceiptV1.from_bytes(payload)
    except ContractError as error:
        raise StackDriftError("retained stack drift receipt is invalid") from error
    expected = _expected_receipt(
        resolved=resolved,
        transaction=transaction,
        predecessor=predecessor,
        stack_id=stack_id,
        detection_id=receipt.drift_detection_id,
    )
    if expected.to_bytes() != payload:
        raise StackDriftError("retained stack drift receipt differs from dispatch")
    _validate_receipt_authority(
        receipt,
        plan=plan,
        transaction=transaction,
        resolved=resolved,
        predecessor=predecessor,
    )
    return VerifiedStackDriftReceiptV1(receipt=receipt, _token=_RECEIPT_TOKEN)


def _validate_client(
    client: object,
    *,
    account: str,
    capability: str,
) -> AttestedAwsClientV2:
    if not isinstance(client, AttestedAwsClientV2):
        raise StackDriftError("stack drift requires an attested AWS client")
    try:
        client.require_scope(
            service="cloudformation",
            account=account,
            region=REQUIRED_REGION,
            capability=capability,
        )
    except AwsAuthorityError as error:
        raise StackDriftError(
            "stack drift AWS authority crosses its subject"
        ) from error
    return client


class StackDriftDispatcherV1:
    """Start one exact detection and synchronously retain its generated UUID."""

    def __init__(self, client: object) -> None:
        self._client = client

    def dispatch(
        self,
        authority: VerifiedStackDriftDispatchV1,
        fresh_authority: FreshDispatchAuthorityV1,
    ) -> ReleaseDispatchAttemptV1:
        if not isinstance(authority, VerifiedStackDriftDispatchV1):
            raise StackDriftError(
                "stack drift dispatch requires verified private authority"
            )
        if type(fresh_authority) is not FreshDispatchAuthorityV1:
            raise StackDriftError(
                "stack drift dispatch requires exact fresh dispatch authority"
            )
        (
            operation,
            resolved,
            sink,
            plan,
            transaction,
            predecessor,
        ) = authority._binding()
        stack_id = _predecessor_stack_id(
            plan=plan,
            transaction=transaction,
            predecessor=predecessor,
            operation=operation,
        )
        client = _validate_client(
            self._client, account=operation.account, capability="mutation"
        )
        attempted, retained = sink._load()
        if retained is not None:
            _verified_retained_receipt(
                retained,
                resolved=resolved,
                plan=plan,
                transaction=transaction,
                predecessor=predecessor,
                stack_id=stack_id,
            )
            raise StackDriftDispatchAmbiguous(
                "stack drift already has a retained receipt and cannot be replayed; "
                "release remains UNCERTAIN"
            )
        if attempted:
            raise StackDriftDispatchAmbiguous(
                "stack drift was attempted without a receipt; release remains UNCERTAIN"
            )
        try:
            consumed_attempt = fresh_authority.consume(
                provider="CLOUDFORMATION",
                operation_sha256=resolved.mutation_request.operation_sha256,
                resolved_request_sha256=resolved.digest(),
            )
        except DispatchAttemptError as error:
            raise StackDriftError(
                "stack drift fresh dispatch authority differs from its operation"
            ) from error
        if not sink._begin_attempt():
            attempted, retained = sink._load()
            if retained is not None:
                _verified_retained_receipt(
                    retained,
                    resolved=resolved,
                    plan=plan,
                    transaction=transaction,
                    predecessor=predecessor,
                    stack_id=stack_id,
                )
            raise StackDriftDispatchAmbiguous(
                "stack drift attempt raced without a receipt; release remains UNCERTAIN"
            )
        try:
            response = client.invoke("detect_stack_drift", StackName=stack_id)
        except Exception as error:
            raise StackDriftDispatchAmbiguous(
                "stack drift dispatch has an unknown effect; release remains UNCERTAIN"
            ) from error
        if not isinstance(response, Mapping):
            raise StackDriftDispatchAmbiguous(
                "stack drift dispatch response is malformed; release remains UNCERTAIN"
            )
        detection_id = response.get("StackDriftDetectionId")
        try:
            receipt = _expected_receipt(
                resolved=resolved,
                transaction=transaction,
                predecessor=predecessor,
                stack_id=stack_id,
                detection_id=(detection_id if isinstance(detection_id, str) else ""),
            )
            _validate_receipt_authority(
                receipt,
                plan=plan,
                transaction=transaction,
                resolved=resolved,
                predecessor=predecessor,
            )
        except StackDriftError as error:
            raise StackDriftDispatchAmbiguous(
                "stack drift dispatch response lacks transaction authority; "
                "release remains UNCERTAIN"
            ) from error
        payload = receipt.to_bytes()
        sink._retain(payload)
        try:
            attempted_after, retained_after = sink._load()
        except StackDriftError as error:
            raise StackDriftDispatchAmbiguous(
                "stack drift receipt confirmation is unavailable; "
                "release remains UNCERTAIN"
            ) from error
        if not attempted_after or retained_after != payload:
            raise StackDriftDispatchAmbiguous(
                "stack drift receipt retention is not byte-exact; "
                "release remains UNCERTAIN"
            )
        _verified_retained_receipt(
            retained_after,
            resolved=resolved,
            plan=plan,
            transaction=transaction,
            predecessor=predecessor,
            stack_id=stack_id,
        )
        return consumed_attempt


def _receipt_identity(
    receipt: StackDriftDispatchReceiptV1,
) -> tuple[str, str, str]:
    match = re.fullmatch(
        r"cfn:([0-9]{12}):(eu-west-1):stack:"
        r"([A-Za-z][A-Za-z0-9-]{0,127}):release:([0-9a-f]{40}):drift",
        receipt.subject,
    )
    if match is None or match.group(3) not in _STACKS:
        raise StackDriftError("retained stack drift receipt subject is invalid")
    account, region, stack_name = match.group(1), match.group(2), match.group(3)
    _validate_stack_id(receipt.stack_id, account=account, stack_name=stack_name)
    return account, region, stack_name


def _validate_stack_id(value: str, *, account: str, stack_name: str) -> str:
    match = _STACK_ID.fullmatch(value) if isinstance(value, str) else None
    if match is None or (match.group(1), match.group(2)) != (account, stack_name):
        raise StackDriftError("CloudFormation retained StackId is invalid")
    return value


def _base_projection(receipt: StackDriftDispatchReceiptV1) -> dict[str, Any]:
    return {
        "dispatchReceiptSha256": receipt.digest(),
        "driftDetectionId": receipt.drift_detection_id,
        "stackId": receipt.stack_id,
        "predecessorEvidenceSha256": receipt.predecessor_evidence_sha256,
        "predecessorObserverEvidenceSha256": (
            receipt.predecessor_observer_evidence_sha256
        ),
    }


def _canonical_provider_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise StackDriftObservationAmbiguous(
                "CloudFormation closing evidence is malformed"
            )
        return {
            key: _canonical_provider_value(value[key]) for key in sorted(value)
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_provider_value(item) for item in value]
    raise StackDriftObservationAmbiguous(
        "CloudFormation closing evidence is malformed"
    )


def _provider_timestamp(value: object, *, label: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise StackDriftObservationAmbiguous(f"{label} timestamp is malformed")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class _DetectionSnapshot:
    stack_id: str
    detection_id: str
    detection_status: str
    drift_status: str | None
    resource_count: int | None
    reason: str | None
    timestamp: datetime


class StackDriftObserverV1:
    """Observe only the exact retained detection ID and predecessor StackId."""

    def __init__(self, client: object) -> None:
        self._client = client

    def _call(self, method: str, **kwargs: Any) -> Mapping[str, Any]:
        try:
            response = self._client.invoke(method, **kwargs)
        except Exception as error:
            raise StackDriftObservationAmbiguous(
                f"cloudformation.{method} failed without authoritative evidence"
            ) from error
        if not isinstance(response, Mapping):
            raise StackDriftObservationAmbiguous(
                f"cloudformation.{method} returned malformed evidence"
            )
        return response

    def observe(
        self, verified_receipt: VerifiedStackDriftReceiptV1
    ) -> CanonicalReadObservationV2:
        if not isinstance(verified_receipt, VerifiedStackDriftReceiptV1):
            raise StackDriftError(
                "stack drift observation requires a verified retained receipt"
            )
        receipt = verified_receipt.receipt
        account, _, _ = _receipt_identity(receipt)
        self._client = _validate_client(
            self._client, account=account, capability="observer"
        )
        snapshot = self._detection_snapshot(receipt)
        detection_status = snapshot.detection_status
        drift_status = snapshot.drift_status
        count = snapshot.resource_count
        reason = snapshot.reason
        projection = {
            **_base_projection(receipt),
            "driftDetectionTimestamp": snapshot.timestamp.isoformat(),
        }
        if detection_status == "DETECTION_IN_PROGRESS":
            if (
                drift_status not in {None, "UNKNOWN", "NOT_CHECKED"}
                or count not in {None, 0}
                or reason is not None
            ):
                raise StackDriftObservationAmbiguous(
                    "CloudFormation drift detection status is contradictory"
                )
            return _new_observation(
                service="cloudformation",
                operation="describe_stack_drift_detection_status",
                subject=receipt.subject,
                disposition=ObservationDisposition.PENDING,
                provider_status=detection_status,
                projection=projection,
            )
        if detection_status == "DETECTION_FAILED":
            if (
                drift_status not in {None, "UNKNOWN", "NOT_CHECKED"}
                or count not in {None, 0}
            ):
                raise StackDriftObservationAmbiguous(
                    "CloudFormation drift detection status is contradictory"
                )
            return _new_observation(
                service="cloudformation",
                operation="describe_stack_drift_detection_status",
                subject=receipt.subject,
                disposition=ObservationDisposition.FAILED_RETAINED,
                provider_status="DETECTION_FAILED",
                projection=projection,
            )
        if detection_status != "DETECTION_COMPLETE" or reason is not None:
            raise StackDriftObservationAmbiguous(
                "CloudFormation drift detection status is unreviewed"
            )
        if drift_status not in {"DRIFTED", "IN_SYNC"} or count is None:
            raise StackDriftObservationAmbiguous(
                "CloudFormation completed drift detection status is incomplete"
            )
        if drift_status == "DRIFTED":
            if count < 1:
                raise StackDriftObservationAmbiguous(
                    "CloudFormation drift detection status is contradictory"
                )
            return _new_observation(
                service="cloudformation",
                operation="describe_stack_drift_detection_status",
                subject=receipt.subject,
                disposition=ObservationDisposition.FAILED_RETAINED,
                provider_status="DRIFTED",
                projection={**projection, "resourceDriftCount": count},
            )
        if drift_status != "IN_SYNC" or count != 0:
            raise StackDriftObservationAmbiguous(
                "CloudFormation drift detection status is contradictory"
            )
        resource_count = self._resource_drift_count(
            receipt.stack_id,
            detection_timestamp=snapshot.timestamp,
        )
        if self._detection_snapshot(receipt) != snapshot:
            raise StackDriftObservationAmbiguous(
                "CloudFormation drift detection changed during observation"
            )
        stack_digest, template_digest, policy_digest = self._closing_identity(
            receipt.stack_id,
            drift_status=snapshot.drift_status,
            drift_timestamp=snapshot.timestamp,
        )
        if resource_count:
            raise StackDriftObservationAmbiguous(
                "CloudFormation resource drift evidence crosses retained detection"
            )
        return _new_observation(
            service="cloudformation",
            operation="describe_stack_drift_detection_status",
            subject=receipt.subject,
            disposition=ObservationDisposition.PRESENT,
            provider_status="IN_SYNC",
            projection={
                **projection,
                "resourceDriftCount": 0,
                "closingStackSha256": stack_digest,
                "closingTemplateSha256": template_digest,
                "closingStackPolicySha256": policy_digest,
            },
        )

    def _detection_snapshot(
        self,
        receipt: StackDriftDispatchReceiptV1,
    ) -> _DetectionSnapshot:
        response = self._call(
            "describe_stack_drift_detection_status",
            StackDriftDetectionId=receipt.drift_detection_id,
        )
        if (
            response.get("StackId") != receipt.stack_id
            or response.get("StackDriftDetectionId") != receipt.drift_detection_id
        ):
            raise StackDriftError(
                "CloudFormation stack drift detection identity differs from receipt"
            )
        detection_status = response.get("DetectionStatus")
        drift_status = response.get("StackDriftStatus")
        count = response.get("DriftedStackResourceCount")
        reason = response.get("DetectionStatusReason")
        timestamp = _provider_timestamp(
            response.get("Timestamp"), label="CloudFormation drift detection"
        )
        if (
            not isinstance(detection_status, str)
            or drift_status is not None
            and not isinstance(drift_status, str)
            or count is not None
            and (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
            )
            or reason is not None
            and (not isinstance(reason, str) or not reason)
        ):
            raise StackDriftObservationAmbiguous(
                "CloudFormation drift detection status is malformed"
            )
        return _DetectionSnapshot(
            stack_id=receipt.stack_id,
            detection_id=receipt.drift_detection_id,
            detection_status=detection_status,
            drift_status=drift_status,
            resource_count=count,
            reason=reason,
            timestamp=timestamp,
        )

    def _resource_drift_count(
        self,
        stack_id: str,
        *,
        detection_timestamp: datetime,
    ) -> int:
        token = ""
        seen: set[str] = set()
        count = 0
        for _ in range(_MAX_RESOURCE_DRIFT_PAGES):
            arguments: dict[str, Any] = {
                "StackName": stack_id,
                "StackResourceDriftStatusFilters": list(
                    _REVIEWED_RESOURCE_DRIFT_STATUSES
                ),
                "MaxResults": _RESOURCE_DRIFT_PAGE_SIZE,
            }
            if token:
                arguments["NextToken"] = token
            response = self._call("describe_stack_resource_drifts", **arguments)
            drifts = response.get("StackResourceDrifts")
            if not isinstance(drifts, list) or any(
                not isinstance(item, Mapping) for item in drifts
            ):
                raise StackDriftObservationAmbiguous(
                    "CloudFormation resource drift evidence is malformed"
                )
            for item in drifts:
                if item.get("StackId") != stack_id:
                    raise StackDriftError(
                        "CloudFormation resource drift differs from retained StackId"
                    )
                logical_id = item.get("LogicalResourceId")
                resource_type = item.get("ResourceType")
                status = item.get("StackResourceDriftStatus")
                resource_timestamp = _provider_timestamp(
                    item.get("Timestamp"),
                    label="CloudFormation resource drift",
                )
                if (
                    not isinstance(logical_id, str)
                    or not logical_id
                    or "\x00" in logical_id
                    or not isinstance(resource_type, str)
                    or not resource_type
                    or "\x00" in resource_type
                ):
                    raise StackDriftObservationAmbiguous(
                        "CloudFormation resource drift evidence is malformed"
                    )
                if status not in _REVIEWED_RESOURCE_DRIFT_STATUSES:
                    raise StackDriftObservationAmbiguous(
                        "CloudFormation resource drift status is unreviewed"
                    )
                if resource_timestamp < detection_timestamp:
                    raise StackDriftObservationAmbiguous(
                        "CloudFormation resource drift timestamp precedes "
                        "retained detection initiation"
                    )
                if status != "IN_SYNC":
                    count += 1
            next_token = response.get("NextToken", "")
            if next_token in (None, ""):
                return count
            if (
                not isinstance(next_token, str)
                or "\x00" in next_token
                or next_token in seen
            ):
                raise StackDriftObservationAmbiguous(
                    "CloudFormation resource drift pagination token cycle"
                )
            seen.add(next_token)
            token = next_token
        raise StackDriftObservationAmbiguous(
            "CloudFormation resource drift pagination exceeded its bound"
        )

    def _closing_identity(
        self,
        stack_id: str,
        *,
        drift_status: str,
        drift_timestamp: datetime,
    ) -> tuple[str, str, str]:
        first = self._read_closure(
            stack_id,
            drift_status=drift_status,
            drift_timestamp=drift_timestamp,
        )
        second = self._read_closure(
            stack_id,
            drift_status=drift_status,
            drift_timestamp=drift_timestamp,
        )
        if first[:3] != second[:3]:
            raise StackDriftObservationAmbiguous(
                "CloudFormation closing stack/template/policy changed"
            )
        if first[3] not in (None, "") or second[3] not in (None, ""):
            raise StackDriftError(
                "CloudFormation closing stack has an unreviewed stack policy"
            )
        return first[:3]

    def _read_closure(
        self,
        stack_id: str,
        *,
        drift_status: str,
        drift_timestamp: datetime,
    ) -> tuple[str, str, str, Any]:
        response = self._call("describe_stacks", StackName=stack_id)
        if response.get("NextToken") not in (None, ""):
            raise StackDriftObservationAmbiguous(
                "CloudFormation closing stack evidence is paginated"
            )
        stacks = response.get("Stacks")
        if not isinstance(stacks, list) or len(stacks) != 1:
            raise StackDriftObservationAmbiguous(
                "CloudFormation closing stack evidence is not singular"
            )
        stack = stacks[0]
        if not isinstance(stack, Mapping) or stack.get("StackId") != stack_id:
            raise StackDriftError(
                "CloudFormation closing stack differs from retained StackId"
            )
        match = _STACK_ID.fullmatch(stack_id)
        assert match is not None
        if stack.get("StackName") != match.group(2):
            raise StackDriftError(
                "CloudFormation closing stack differs from retained StackId"
            )
        if stack.get("StackStatus") not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
            raise StackDriftError(
                "CloudFormation closing stack is not in a reviewed stable status"
            )
        if stack.get("EnableTerminationProtection") is not True:
            raise StackDriftError(
                "CloudFormation closing stack lacks termination protection"
            )
        drift_information = stack.get("DriftInformation")
        if (
            not isinstance(drift_information, Mapping)
            or set(drift_information)
            != {"StackDriftStatus", "LastCheckTimestamp"}
            or drift_information.get("StackDriftStatus") != drift_status
            or _provider_timestamp(
                drift_information.get("LastCheckTimestamp"),
                label="CloudFormation latest stack drift",
            )
            != drift_timestamp
        ):
            raise StackDriftObservationAmbiguous(
                "CloudFormation latest stack drift differs from retained detection"
            )
        stable_stack = dict(stack)
        stack_digest = hashlib.sha256(
            canonical_json_bytes(_canonical_provider_value(stable_stack))
        ).hexdigest()

        template_response = self._call(
            "get_template", StackName=stack_id, TemplateStage="Processed"
        )
        stages = template_response.get("StagesAvailable")
        if stages is not None and (
            not isinstance(stages, list)
            or "Processed" not in stages
            or any(not isinstance(item, str) for item in stages)
        ):
            raise StackDriftObservationAmbiguous(
                "CloudFormation closing template evidence is malformed"
            )
        template = template_response.get("TemplateBody")
        if isinstance(template, str):
            try:
                template = json.loads(template)
            except (TypeError, ValueError) as error:
                raise StackDriftObservationAmbiguous(
                    "CloudFormation closing template evidence is malformed"
                ) from error
        if not isinstance(template, Mapping):
            raise StackDriftObservationAmbiguous(
                "CloudFormation closing template evidence is malformed"
            )
        template_digest = hashlib.sha256(
            canonical_json_bytes(_canonical_provider_value(dict(template)))
        ).hexdigest()

        policy_response = self._call("get_stack_policy", StackName=stack_id)
        policy = policy_response.get("StackPolicyBody", "")
        policy_digest = hashlib.sha256(
            canonical_json_bytes(
                {"stackPolicyBody": _canonical_provider_value(policy)}
            )
        ).hexdigest()
        return stack_digest, template_digest, policy_digest, policy


__all__ = [
    "StackDriftDispatchAmbiguous",
    "StackDriftDispatcherV1",
    "StackDriftError",
    "StackDriftObservationAmbiguous",
    "StackDriftObserverV1",
    "StackDriftOperationV1",
    "StackDriftReceiptSinkV1",
    "VerifiedStackDriftDispatchV1",
    "VerifiedStackDriftPreflightV1",
    "VerifiedStackDriftReceiptV1",
    "validate_stack_drift_dispatch",
    "validate_stack_drift_preflight",
]
