"""Exact release-v2 AgentCore MMDSv2 hardening and reconciliation.

The pre-cloud request in this module is deliberately static.  Live Runtime
identity and configuration are accepted only from a canonical release plan and
its retained transaction prefix; a later receipt-sink integration supplies the
write-ahead crash boundary for the single permitted provider mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Mapping, Protocol

from release_tools.aws_authority_v2 import AttestedAwsClientV2, AwsAuthorityError

from release_tools.contracts import (
    ContractError,
    ReleasePlanV2,
    ResolvedMutationRequestV2,
    RuntimeConfigurationV1,
    StagingTransactionV2,
    canonical_json_bytes,
    expected_execution_role_arn,
    parse_canonical_object,
)
from release_tools.production_observer_v2 import _new_observation
from release_tools.dispatch_attempt_v2 import (
    DispatchAttemptError,
    FreshDispatchAuthorityV1,
    ReleaseDispatchAttemptV1,
)
from release_tools.transaction import ObservationDisposition


REQUIRED_REGION = "eu-west-1"
RUNTIME_NAME = "personal_operator_bridge"
_ACCOUNT = re.compile(r"[0-9]{12}")
_SHA40 = re.compile(r"[0-9a-f]{40}")
_SHA64 = re.compile(r"[0-9a-f]{64}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}")
_VERSION = re.compile(r"[1-9][0-9]{0,4}")


class AgentCoreHardeningError(RuntimeError):
    """The requested AgentCore hardening action crosses its closed boundary."""


class AgentCoreHardeningDispatchAmbiguous(AgentCoreHardeningError):
    """A hardening update may have taken effect and must never be retried."""


class AgentCoreHardeningObservationAmbiguous(AgentCoreHardeningError):
    """AgentCore did not return stable evidence for one exact Runtime version."""


def _text(
    value: object,
    *,
    label: str,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise AgentCoreHardeningError(f"{label} is invalid")
    return value


_OPERATION_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class AgentCoreHardeningOperationV1:
    """Static request for the one always-planned Runtime hardening step."""

    SCHEMA = "personal-operator.agentcore-hardening-operation.v1"
    FIELDS = {
        "schema",
        "sourceCommit",
        "sourceTree",
        "account",
        "region",
        "runtimeName",
        "metadataConfiguration",
    }

    source_commit: str
    source_tree: str
    account: str
    region: str
    runtime_name: str

    def __init__(
        self,
        *,
        source_commit: str,
        source_tree: str,
        account: str,
        region: str,
        runtime_name: str,
        _token: object | None = None,
    ) -> None:
        if _token is not _OPERATION_TOKEN:
            raise AgentCoreHardeningError(
                "AgentCore hardening operation is not directly constructible"
            )
        object.__setattr__(self, "source_commit", source_commit)
        object.__setattr__(self, "source_tree", source_tree)
        object.__setattr__(self, "account", account)
        object.__setattr__(self, "region", region)
        object.__setattr__(self, "runtime_name", runtime_name)

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "AgentCoreHardeningOperationV1":
        if not isinstance(raw, Mapping) or set(raw) != cls.FIELDS:
            raise AgentCoreHardeningError(
                "AgentCore hardening operation fields are not exact"
            )
        if raw.get("schema") != cls.SCHEMA:
            raise AgentCoreHardeningError(
                "AgentCore hardening operation schema is invalid"
            )
        source_commit = _text(
            raw.get("sourceCommit"), label="source commit", pattern=_SHA40
        )
        source_tree = _text(
            raw.get("sourceTree"), label="source tree", pattern=_SHA40
        )
        account = _text(raw.get("account"), label="account", pattern=_ACCOUNT)
        if account == "000000000000":
            raise AgentCoreHardeningError("account is invalid")
        region = _text(raw.get("region"), label="region")
        if region != REQUIRED_REGION:
            raise AgentCoreHardeningError(
                f"AgentCore hardening region must be exactly {REQUIRED_REGION}"
            )
        if raw.get("runtimeName") != RUNTIME_NAME:
            raise AgentCoreHardeningError(
                "AgentCore hardening runtime name is not stable"
            )
        if raw.get("metadataConfiguration") != {"requireMMDSV2": True}:
            raise AgentCoreHardeningError(
                "AgentCore hardening policy must require MMDSv2"
            )
        return cls(
            source_commit=source_commit,
            source_tree=source_tree,
            account=account,
            region=region,
            runtime_name=RUNTIME_NAME,
            _token=_OPERATION_TOKEN,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "AgentCoreHardeningOperationV1":
        try:
            return cls.from_mapping(parse_canonical_object(payload))
        except AgentCoreHardeningError:
            raise
        except (ContractError, TypeError, ValueError) as error:
            raise AgentCoreHardeningError(
                "AgentCore hardening operation is not canonical"
            ) from error

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "sourceCommit": self.source_commit,
            "sourceTree": self.source_tree,
            "account": self.account,
            "region": self.region,
            "runtimeName": self.runtime_name,
            "metadataConfiguration": {"requireMMDSV2": True},
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    @property
    def subject(self) -> str:
        return (
            f"agentcore:{self.account}:{self.region}:runtime:{RUNTIME_NAME}:"
            f"release:{self.source_commit}:mmdsv2"
        )


_PREFLIGHT_TOKEN = object()


class VerifiedAgentCoreHardeningPreflightV1:
    """Opaque binding between one static request and its unique plan step."""

    __slots__ = ("_operation", "_plan", "_request_sha256")

    def __init__(
        self,
        *,
        operation: AgentCoreHardeningOperationV1,
        release_plan: ReleasePlanV2,
        request_sha256: str,
        _token: object | None = None,
    ) -> None:
        if _token is not _PREFLIGHT_TOKEN:
            raise AgentCoreHardeningError(
                "verified AgentCore hardening preflight is not constructible"
            )
        self._operation = operation
        self._plan = release_plan
        self._request_sha256 = request_sha256

    def _canonical(self) -> tuple[AgentCoreHardeningOperationV1, ReleasePlanV2]:
        try:
            operation = AgentCoreHardeningOperationV1.from_bytes(
                self._operation.to_bytes()
            )
            plan = ReleasePlanV2.from_bytes(self._plan.to_bytes())
        except (AgentCoreHardeningError, ContractError) as error:
            raise AgentCoreHardeningError(
                "verified AgentCore hardening preflight changed"
            ) from error
        if operation.digest() != self._request_sha256:
            raise AgentCoreHardeningError(
                "verified AgentCore hardening request digest changed"
            )
        return operation, plan


def validate_agentcore_hardening_preflight(
    operation: AgentCoreHardeningOperationV1,
    *,
    release_plan: ReleasePlanV2,
) -> VerifiedAgentCoreHardeningPreflightV1:
    """Bind one immutable static request to the unique hardening step."""

    if (
        type(operation) is not AgentCoreHardeningOperationV1
        or type(release_plan) is not ReleasePlanV2
    ):
        raise AgentCoreHardeningError(
            "AgentCore hardening preflight inputs are invalid"
        )
    try:
        canonical_operation = AgentCoreHardeningOperationV1.from_bytes(
            operation.to_bytes()
        )
        plan = ReleasePlanV2.from_bytes(release_plan.to_bytes())
    except (AgentCoreHardeningError, ContractError) as error:
        raise AgentCoreHardeningError(
            "AgentCore hardening preflight inputs are invalid"
        ) from error
    if (
        canonical_operation.source_commit,
        canonical_operation.source_tree,
        canonical_operation.account,
        canonical_operation.region,
    ) != (plan.source_commit, plan.source_tree, plan.account, plan.region):
        raise AgentCoreHardeningError(
            "AgentCore hardening operation crosses its release-plan identity"
        )
    payload = canonical_operation.to_bytes()
    request_sha256 = hashlib.sha256(payload).hexdigest()
    matches = tuple(
        step for step in plan.steps if step.request_sha256 == request_sha256
    )
    if len(matches) != 1:
        raise AgentCoreHardeningError(
            "AgentCore hardening operation is not uniquely planned"
        )
    step = matches[0]
    artifact = next(
        (item for item in plan.artifacts if item.path == step.request_artifact),
        None,
    )
    if (
        step.kind != "AGENTCORE_HARDEN"
        or step.phase != "runtime"
        or step.subject != canonical_operation.subject
        or artifact is None
        or artifact.sha256 != request_sha256
        or artifact.size != len(payload)
        or step.expected_request_sha256 != request_sha256
        or step.expected_template_sha256
        or step.expected_template_parameter_sha256
        or step.expected_observed_request_sha256
        or step.expected_content_sha256
    ):
        raise AgentCoreHardeningError(
            "AgentCore hardening operation differs from its exact plan step"
        )
    return VerifiedAgentCoreHardeningPreflightV1(
        operation=canonical_operation,
        release_plan=plan,
        request_sha256=request_sha256,
        _token=_PREFLIGHT_TOKEN,
    )


class _ReceiptBackend(Protocol):
    def binding(self) -> Mapping[str, str]: ...

    def load(self) -> tuple[bool, bytes | None]: ...

    def load_precondition(self) -> bytes | None: ...

    def begin_attempt(self) -> bool: ...

    def retain(self, payload: bytes) -> None: ...

    def retain_precondition(self, payload: bytes) -> None: ...


@dataclass(frozen=True, slots=True)
class _ReceiptStorageBindingV1:
    evidence_store_sha256: str
    journal_path_sha256: str
    journal_execution_id: str

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "_ReceiptStorageBindingV1":
        if not isinstance(raw, Mapping) or set(raw) != {
            "evidenceStoreSha256",
            "journalPathSha256",
            "journalExecutionId",
        }:
            raise AgentCoreHardeningError(
                "AgentCore hardening receipt storage binding is not exact"
            )
        return cls(
            evidence_store_sha256=_text(
                raw.get("evidenceStoreSha256"),
                label="evidence store binding",
                pattern=_SHA64,
            ),
            journal_path_sha256=_text(
                raw.get("journalPathSha256"),
                label="journal path binding",
                pattern=_SHA64,
            ),
            journal_execution_id=_text(
                raw.get("journalExecutionId"),
                label="journal execution binding",
                pattern=_SHA64,
            ),
        )

    def to_mapping(self) -> dict[str, str]:
        return {
            "evidenceStoreSha256": self.evidence_store_sha256,
            "journalPathSha256": self.journal_path_sha256,
            "journalExecutionId": self.journal_execution_id,
        }


def _receipt_backend_binding(
    backend: _ReceiptBackend,
) -> _ReceiptStorageBindingV1:
    try:
        raw = backend.binding()
        return _ReceiptStorageBindingV1.from_mapping(raw)
    except Exception as error:
        raise AgentCoreHardeningError(
            "AgentCore hardening receipt backend binding is invalid"
        ) from error


_SINK_TOKEN = object()


class AgentCoreHardeningReceiptSinkV1:
    """Opaque append-only receipt slot owned by one uncertain journal step."""

    __slots__ = (
        "_backend",
        "_plan",
        "_transaction",
        "_storage_binding",
        "_pinned_payload",
        "_pinned_precondition",
    )

    def __init__(
        self,
        *,
        backend: _ReceiptBackend,
        release_plan: ReleasePlanV2 | None = None,
        transaction: StagingTransactionV2 | None = None,
        storage_binding: _ReceiptStorageBindingV1 | None = None,
        _token: object | None = None,
    ) -> None:
        if _token is not _SINK_TOKEN:
            raise AgentCoreHardeningError(
                "AgentCore hardening receipt sink is not constructible"
            )
        if (
            type(release_plan) is not ReleasePlanV2
            or type(transaction) is not StagingTransactionV2
            or type(storage_binding) is not _ReceiptStorageBindingV1
        ):
            raise AgentCoreHardeningError(
                "AgentCore hardening receipt sink lacks transaction authority"
            )
        self._backend = backend
        self._plan = release_plan
        self._transaction = transaction
        self._storage_binding = storage_binding
        self._pinned_payload: bytes | None = None
        self._pinned_precondition: bytes | None = None

    def _authority(
        self,
    ) -> tuple[
        ReleasePlanV2,
        StagingTransactionV2,
        _ReceiptStorageBindingV1,
    ]:
        try:
            plan = ReleasePlanV2.from_bytes(self._plan.to_bytes())
            transaction = StagingTransactionV2.from_bytes(
                self._transaction.to_bytes(), plan=plan
            )
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise AgentCoreHardeningError(
                "AgentCore hardening receipt sink authority is invalid"
            ) from error
        backend_binding = _receipt_backend_binding(self._backend)
        if backend_binding != self._storage_binding:
            raise AgentCoreHardeningError(
                "AgentCore hardening receipt backend binding changed"
            )
        return plan, transaction, self._storage_binding

    def _load(self) -> tuple[bool, bytes | None]:
        try:
            result = self._backend.load()
        except Exception as error:
            raise AgentCoreHardeningError(
                "AgentCore hardening receipt sink cannot be read"
            ) from error
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not isinstance(result[0], bool)
            or (result[1] is not None and not isinstance(result[1], bytes))
            or (result[1] is not None and not result[0])
        ):
            raise AgentCoreHardeningError(
                "AgentCore hardening receipt sink state is invalid"
            )
        payload = result[1]
        if payload is not None:
            if self._pinned_payload is None:
                self._pinned_payload = payload
            elif self._pinned_payload != payload:
                raise AgentCoreHardeningError(
                    "retained AgentCore hardening receipt changed"
                )
        return result

    def _begin_attempt(self) -> bool:
        try:
            result = self._backend.begin_attempt()
        except Exception as error:
            raise AgentCoreHardeningError(
                "AgentCore hardening attempt cannot be retained"
            ) from error
        if not isinstance(result, bool):
            raise AgentCoreHardeningError(
                "AgentCore hardening attempt result is invalid"
            )
        return result

    def _load_precondition(self) -> bytes | None:
        try:
            payload = self._backend.load_precondition()
        except Exception as error:
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition cannot be read"
            ) from error
        if payload is not None and not isinstance(payload, bytes):
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition state is invalid"
            )
        if payload is not None:
            if self._pinned_precondition is None:
                self._pinned_precondition = payload
            elif self._pinned_precondition != payload:
                raise AgentCoreHardeningError(
                    "retained AgentCore hardening precondition changed"
                )
        return payload

    def _retain_precondition(self, payload: bytes) -> None:
        if not isinstance(payload, bytes) or not payload:
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition payload is invalid"
            )
        try:
            self._backend.retain_precondition(payload)
        except Exception as error:
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition was not durably retained"
            ) from error

    def _retain(self, payload: bytes) -> None:
        try:
            self._backend.retain(payload)
        except Exception as error:
            raise AgentCoreHardeningDispatchAmbiguous(
                "AgentCore hardening receipt was not retained; release remains "
                "UNCERTAIN"
            ) from error


def _new_agentcore_hardening_receipt_sink(
    backend: _ReceiptBackend,
    *,
    release_plan: ReleasePlanV2,
    transaction: StagingTransactionV2,
    evidence_store_sha256: str,
    journal_path_sha256: str,
    journal_execution_id: str,
) -> AgentCoreHardeningReceiptSinkV1:
    """Trusted-package hook for the evidence store's exact receipt backend."""

    if any(
        not callable(getattr(backend, method, None))
        for method in (
            "binding",
            "load",
            "load_precondition",
            "begin_attempt",
            "retain",
            "retain_precondition",
        )
    ):
        raise AgentCoreHardeningError(
            "AgentCore hardening receipt backend is invalid"
        )
    if (
        type(release_plan) is not ReleasePlanV2
        or type(transaction) is not StagingTransactionV2
    ):
        raise AgentCoreHardeningError(
            "AgentCore hardening receipt sink inputs are invalid"
        )
    try:
        plan = ReleasePlanV2.from_bytes(release_plan.to_bytes())
        canonical_transaction = StagingTransactionV2.from_bytes(
            transaction.to_bytes(), plan=plan
        )
        storage_binding = _ReceiptStorageBindingV1.from_mapping(
            {
                "evidenceStoreSha256": evidence_store_sha256,
                "journalPathSha256": journal_path_sha256,
                "journalExecutionId": journal_execution_id,
            }
        )
    except (AgentCoreHardeningError, ContractError, TypeError, ValueError) as error:
        raise AgentCoreHardeningError(
            "AgentCore hardening receipt sink inputs are invalid"
        ) from error
    if (
        canonical_transaction.state != "UNCERTAIN"
        or canonical_transaction.completed_step_count >= len(plan.steps)
        or plan.steps[canonical_transaction.completed_step_count].kind
        != "AGENTCORE_HARDEN"
    ):
        raise AgentCoreHardeningError(
            "AgentCore hardening receipt sink is not at its uncertain step"
        )
    if _receipt_backend_binding(backend) != storage_binding:
        raise AgentCoreHardeningError(
            "AgentCore hardening receipt backend binding crosses its sink"
        )
    return AgentCoreHardeningReceiptSinkV1(
        backend=backend,
        release_plan=plan,
        transaction=canonical_transaction,
        storage_binding=storage_binding,
        _token=_SINK_TOKEN,
    )


_AUTHORITY_TOKEN = object()


class AgentCoreHardeningAuthorityV1:
    """Opaque conjunction of plan, retained-prefix, request, and receipt authority."""

    __slots__ = ("_resolved", "_preflight", "_transaction", "_sink")

    def __init__(
        self,
        *,
        resolved: ResolvedMutationRequestV2,
        preflight: VerifiedAgentCoreHardeningPreflightV1,
        transaction: StagingTransactionV2,
        sink: AgentCoreHardeningReceiptSinkV1,
        _token: object | None = None,
    ) -> None:
        if _token is not _AUTHORITY_TOKEN:
            raise AgentCoreHardeningError(
                "AgentCore hardening authority is not constructible"
            )
        self._resolved = resolved
        self._preflight = preflight
        self._transaction = transaction
        self._sink = sink

    def _binding(
        self,
    ) -> tuple[
        AgentCoreHardeningOperationV1,
        ReleasePlanV2,
        ResolvedMutationRequestV2,
        StagingTransactionV2,
        AgentCoreHardeningReceiptSinkV1,
        _ReceiptStorageBindingV1,
    ]:
        if (
            type(self._resolved) is not ResolvedMutationRequestV2
            or type(self._preflight) is not VerifiedAgentCoreHardeningPreflightV1
            or type(self._transaction) is not StagingTransactionV2
            or type(self._sink) is not AgentCoreHardeningReceiptSinkV1
        ):
            raise AgentCoreHardeningError(
                "AgentCore hardening retained authority capabilities are invalid"
            )
        operation, plan = self._preflight._canonical()
        try:
            transaction = StagingTransactionV2.from_bytes(
                self._transaction.to_bytes(), plan=plan
            )
            resolved = ResolvedMutationRequestV2.from_bytes(
                self._resolved.to_bytes()
            )
            resolved.validate_transaction(plan, transaction)
        except (AttributeError, ContractError, TypeError, ValueError) as error:
            raise AgentCoreHardeningError(
                "AgentCore hardening retained transaction authority is invalid"
            ) from error
        sink_plan, sink_transaction, storage_binding = self._sink._authority()
        request = resolved.mutation_request
        if (
            sink_plan.to_bytes() != plan.to_bytes()
            or sink_transaction.to_bytes() != transaction.to_bytes()
            or transaction.state != "UNCERTAIN"
            or request.kind != "AGENTCORE_HARDEN"
            or request.subject != operation.subject
            or request.request_sha256 != operation.digest()
            or request.plan_sha256 != plan.digest()
            or request.operation_sha256
            != transaction.uncertain_operation_sha256
            or resolved.step_phase != "runtime"
            or resolved.foundation_runtime_inputs is None
            or not resolved.agent_core_stack_id
            or not resolved.runtime_image_digest
            or not resolved.runtime_id
            or not resolved.runtime_version
            or not resolved.runtime_arn
        ):
            raise AgentCoreHardeningError(
                "AgentCore hardening authority differs from its retained prefix"
            )
        return (
            operation,
            plan,
            resolved,
            transaction,
            self._sink,
            storage_binding,
        )

    def digest(self) -> str:
        operation, plan, resolved, transaction, _, storage_binding = (
            self._binding()
        )
        return hashlib.sha256(
            canonical_json_bytes(
                {
                    "operationSha256": operation.digest(),
                    "planSha256": plan.digest(),
                    "resolvedRequestSha256": resolved.digest(),
                    "transactionSha256": hashlib.sha256(
                        transaction.to_bytes()
                    ).hexdigest(),
                    "receiptStorageBinding": storage_binding.to_mapping(),
                }
            )
        ).hexdigest()


def _precondition_receipt_authority_mapping(
    authority: AgentCoreHardeningAuthorityV1,
) -> dict[str, Any]:
    operation, plan, resolved, transaction, _, storage_binding = (
        authority._binding()
    )
    request = resolved.mutation_request
    return {
        "schema": "personal-operator.provider-receipt-attempt.v2",
        "kind": "agentcore-hardening",
        "releasePlanSha256": plan.digest(),
        "evidenceStoreSha256": storage_binding.evidence_store_sha256,
        "journalPathSha256": storage_binding.journal_path_sha256,
        "journalExecutionId": storage_binding.journal_execution_id,
        "journalRevision": transaction.revision,
        "completedPrefixSha256": request.completed_prefix_sha256,
        "stepId": request.step_id,
        "subject": operation.subject,
        "operationSha256": request.operation_sha256,
    }


def validate_agentcore_hardening_authority(
    resolved_request: ResolvedMutationRequestV2,
    preflight: VerifiedAgentCoreHardeningPreflightV1,
    transaction: StagingTransactionV2,
    receipt_sink: AgentCoreHardeningReceiptSinkV1,
) -> AgentCoreHardeningAuthorityV1:
    """Mint provider authority only from four independently validated inputs."""

    if type(resolved_request) is not ResolvedMutationRequestV2:
        raise AgentCoreHardeningError(
            "AgentCore hardening lacks a resolved mutation request"
        )
    if type(preflight) is not VerifiedAgentCoreHardeningPreflightV1:
        raise AgentCoreHardeningError(
            "AgentCore hardening lacks its verified static preflight"
        )
    if (
        type(transaction) is not StagingTransactionV2
        or type(receipt_sink) is not AgentCoreHardeningReceiptSinkV1
    ):
        raise AgentCoreHardeningError(
            "AgentCore hardening lacks retained transaction authority"
        )
    authority = AgentCoreHardeningAuthorityV1(
        resolved=resolved_request,
        preflight=preflight,
        transaction=transaction,
        sink=receipt_sink,
        _token=_AUTHORITY_TOKEN,
    )
    authority._binding()
    receipt_sink._load()
    return authority


def _validate_client(
    client: object,
    *,
    account: str,
    capability: str,
) -> AttestedAwsClientV2:
    if type(client) is not AttestedAwsClientV2:
        raise AgentCoreHardeningError(
            "AgentCore hardening requires an attested AWS client"
        )
    try:
        client.require_scope(
            service="bedrock-agentcore-control",
            account=account,
            region=REQUIRED_REGION,
            capability=capability,
        )
    except AwsAuthorityError as error:
        raise AgentCoreHardeningError(
            "AgentCore hardening AWS authority crosses its subject"
        ) from error
    return client  # type: ignore[return-value]


def _metadata_state(runtime: Mapping[str, Any]) -> str:
    if "metadataConfiguration" not in runtime:
        return "ABSENT"
    value = runtime["metadataConfiguration"]
    if value == {}:
        return "EMPTY"
    if (
        isinstance(value, Mapping)
        and set(value) == {"requireMMDSV2"}
        and value["requireMMDSV2"] is True
    ):
        return "TRUE"
    if (
        isinstance(value, Mapping)
        and set(value) == {"requireMMDSV2"}
        and value["requireMMDSV2"] is False
    ):
        return "FALSE"
    if (
        isinstance(value, Mapping)
        and set(value) == {"requireMMDSV2"}
        and value["requireMMDSV2"] is None
    ):
        return "NULL"
    raise AgentCoreHardeningError(
        "AgentCore Runtime metadata configuration is unreviewed"
    )


@dataclass(frozen=True, slots=True)
class _ReviewedRuntimeV1:
    configuration: RuntimeConfigurationV1
    metadata_state: str
    service_s3_state: str
    projection_bytes: bytes

    def digest(self) -> str:
        return hashlib.sha256(self.projection_bytes).hexdigest()


def _reviewed_runtime(
    raw: Mapping[str, Any],
    *,
    resolved: ResolvedMutationRequestV2,
    expected_version: str,
    expected_arn: str,
    require_hardened: bool,
    allowed_statuses: frozenset[str] = frozenset({"READY"}),
) -> _ReviewedRuntimeV1:
    foundation = resolved.foundation_runtime_inputs
    if foundation is None:
        raise AgentCoreHardeningError(
            "AgentCore Runtime lacks retained foundation inputs"
        )
    expected_description = (
        "Personal Operator immutable bridge runtime at commit "
        f"{resolved.source_commit}"
    )
    expected_role = expected_execution_role_arn(resolved.account, resolved.region)
    if (
        raw.get("agentRuntimeId") != resolved.runtime_id
        or raw.get("agentRuntimeName") != RUNTIME_NAME
        or raw.get("agentRuntimeVersion") != expected_version
        or raw.get("agentRuntimeArn") != expected_arn
        or raw.get("roleArn") != expected_role
        or raw.get("description") != expected_description
    ):
        raise AgentCoreHardeningError(
            "AgentCore Runtime identity, role, or description differs"
        )
    status = raw.get("status")
    if status not in allowed_statuses:
        raise AgentCoreHardeningObservationAmbiguous(
            "AgentCore Runtime status is not the exact expected state"
        )
    metadata_state = _metadata_state(raw)
    if require_hardened and metadata_state != "TRUE":
        raise AgentCoreHardeningError(
            "AgentCore Runtime no longer requires MMDSv2"
        )
    required_configuration = {
        "agentRuntimeArtifact",
        "environmentVariables",
        "filesystemConfigurations",
        "lifecycleConfiguration",
        "networkConfiguration",
        "protocolConfiguration",
    }
    if not required_configuration <= set(raw):
        raise AgentCoreHardeningError(
            "AgentCore Runtime configuration is incomplete"
        )
    network = raw.get("networkConfiguration")
    if not isinstance(network, Mapping) or set(network) != {
        "networkMode",
        "networkModeConfig",
    }:
        raise AgentCoreHardeningError(
            "AgentCore Runtime network configuration is not exact"
        )
    vpc = network.get("networkModeConfig")
    if not isinstance(vpc, Mapping) or not set(vpc) <= {
        "securityGroups",
        "subnets",
        "requireServiceS3Endpoint",
    } or not {"securityGroups", "subnets"} <= set(vpc):
        raise AgentCoreHardeningError(
            "AgentCore Runtime VPC configuration is not exact"
        )
    if "requireServiceS3Endpoint" not in vpc:
        service_s3_state = "ABSENT"
    elif vpc["requireServiceS3Endpoint"] is True:
        service_s3_state = "TRUE"
    elif vpc["requireServiceS3Endpoint"] is False:
        service_s3_state = "FALSE"
    else:
        raise AgentCoreHardeningError(
            "AgentCore Runtime service S3 endpoint flag is invalid"
        )
    if require_hardened and service_s3_state == "TRUE":
        raise AgentCoreHardeningError(
            "AgentCore Runtime still requires the service S3 endpoint"
        )
    normalized_network = dict(network)
    normalized_vpc = dict(vpc)
    normalized_vpc.pop("requireServiceS3Endpoint", None)
    normalized_network["networkModeConfig"] = normalized_vpc
    configuration_mapping = {
        "agentRuntimeArtifact": raw.get("agentRuntimeArtifact"),
        "authorizerConfiguration": raw.get("authorizerConfiguration", {}),
        "environmentVariables": raw.get("environmentVariables"),
        "filesystemConfigurations": raw.get("filesystemConfigurations"),
        "lifecycleConfiguration": raw.get("lifecycleConfiguration"),
        "metadataConfiguration": {"requireMMDSV2": True},
        "networkConfiguration": normalized_network,
        "protocolConfiguration": raw.get("protocolConfiguration"),
        "requestHeaderConfiguration": raw.get("requestHeaderConfiguration", {}),
    }
    expected_image_uri = (
        f"{resolved.account}.dkr.ecr.{resolved.region}.amazonaws.com/"
        f"personal-operator/bridge@{resolved.runtime_image_digest}"
    )
    try:
        configuration = RuntimeConfigurationV1.from_mapping(
            configuration_mapping,
            runtime_image_uri=expected_image_uri,
            account=resolved.account,
            region=resolved.region,
        )
    except ContractError as error:
        raise AgentCoreHardeningError(
            "AgentCore Runtime configuration is outside the reviewed contract"
        ) from error
    environment = dict(configuration.environment_variables)
    if (
        configuration.subnet_ids != foundation.private_subnet_ids
        or configuration.security_group_ids
        != foundation.runtime_security_group_ids
        or environment.get("S3_USER_FILES_BUCKET")
        != foundation.user_files_bucket_name
        or environment.get("WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME")
        != foundation.workspace_broker_function_name
        or environment.get("BEDROCK_GUARDRAIL_ID", "")
        != foundation.guardrail_id
        or environment.get("BEDROCK_GUARDRAIL_VERSION", "")
        != foundation.guardrail_version
    ):
        raise AgentCoreHardeningError(
            "AgentCore Runtime network, environment, or guardrail differs"
        )
    projection_bytes = canonical_json_bytes(
        {
            "runtimeId": resolved.runtime_id,
            "runtimeVersion": expected_version,
            "runtimeArn": expected_arn,
            "status": status,
            "roleArn": expected_role,
            "description": expected_description,
            "runtimeConfiguration": configuration.to_mapping(),
            "metadataState": metadata_state,
            "serviceS3EndpointState": service_s3_state,
        }
    )
    return _ReviewedRuntimeV1(
        configuration=configuration,
        metadata_state=metadata_state,
        service_s3_state=service_s3_state,
        projection_bytes=projection_bytes,
    )


_PRECONDITION_TOKEN = object()
_INSPECTED_PRECONDITION_TOKEN = object()


class AgentCoreHardeningPreconditionV1:
    """Canonical durable stable read authorizing either a no-op or one update."""

    SCHEMA = "personal-operator.agentcore-hardening-precondition.v1"
    FIELDS = {
        "schema",
        "receiptAuthority",
        "resolvedRequestSha256",
        "authoritySha256",
        "account",
        "region",
        "runtimeObservationSha256",
        "runtimeObservation",
        "mode",
    }
    RECEIPT_AUTHORITY_FIELDS = {
        "schema",
        "kind",
        "releasePlanSha256",
        "evidenceStoreSha256",
        "journalPathSha256",
        "journalExecutionId",
        "journalRevision",
        "completedPrefixSha256",
        "stepId",
        "subject",
        "operationSha256",
    }
    RUNTIME_FIELDS = {
        "runtimeId",
        "runtimeVersion",
        "runtimeArn",
        "status",
        "roleArn",
        "description",
        "runtimeConfiguration",
        "metadataState",
        "serviceS3EndpointState",
    }

    __slots__ = (
        "_receipt_authority",
        "_resolved_request_sha256",
        "_authority_sha256",
        "_account",
        "_region",
        "_runtime",
        "_inspection_token",
    )

    def __init__(
        self,
        *,
        receipt_authority: Mapping[str, Any] | None = None,
        resolved_request_sha256: str = "",
        authority_sha256: str = "",
        account: str = "",
        region: str = "",
        runtime: _ReviewedRuntimeV1 | None = None,
        _inspected_token: object | None = None,
        _token: object | None = None,
    ) -> None:
        if _token is not _PRECONDITION_TOKEN:
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition is not constructible"
            )
        if type(runtime) is not _ReviewedRuntimeV1:
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition lacks reviewed Runtime state"
            )
        self._receipt_authority = self._parse_receipt_authority(
            receipt_authority
        )
        self._resolved_request_sha256 = _text(
            resolved_request_sha256,
            label="resolved request digest",
            pattern=_SHA64,
        )
        self._authority_sha256 = _text(
            authority_sha256,
            label="AgentCore hardening authority digest",
            pattern=_SHA64,
        )
        self._account = _text(account, label="account", pattern=_ACCOUNT)
        self._region = _text(region, label="region")
        self._inspection_token = _inspected_token
        if self._account == "000000000000" or self._region != REQUIRED_REGION:
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition release identity is invalid"
            )
        self._runtime = runtime

    @classmethod
    def _parse_receipt_authority(
        cls, raw: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        if not isinstance(raw, Mapping) or set(raw) != cls.RECEIPT_AUTHORITY_FIELDS:
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition receipt authority is not exact"
            )
        if (
            raw.get("schema")
            != "personal-operator.provider-receipt-attempt.v2"
            or raw.get("kind") != "agentcore-hardening"
        ):
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition receipt authority is invalid"
            )
        revision = raw.get("journalRevision")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition journal revision is invalid"
            )
        result: dict[str, Any] = {
            "schema": raw["schema"],
            "kind": raw["kind"],
            "journalRevision": revision,
        }
        for name, label in (
            ("releasePlanSha256", "release plan digest"),
            ("evidenceStoreSha256", "evidence store digest"),
            ("journalPathSha256", "journal path digest"),
            ("journalExecutionId", "journal execution identity"),
            ("completedPrefixSha256", "completed prefix digest"),
        ):
            result[name] = _text(raw.get(name), label=label, pattern=_SHA64)
        result["stepId"] = _text(raw.get("stepId"), label="step ID")
        result["subject"] = _text(raw.get("subject"), label="subject")
        result["operationSha256"] = _text(
            raw.get("operationSha256"),
            label="operation digest",
            pattern=_DIGEST,
        )
        return result

    @classmethod
    def _runtime_from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        account: str,
        region: str,
        expected_sha256: str,
    ) -> _ReviewedRuntimeV1:
        if not isinstance(raw, Mapping) or set(raw) != cls.RUNTIME_FIELDS:
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition Runtime projection is not exact"
            )
        configuration_raw = raw.get("runtimeConfiguration")
        try:
            artifact = configuration_raw["agentRuntimeArtifact"]
            container = artifact["containerConfiguration"]
            runtime_image_uri = container["containerUri"]
        except (KeyError, TypeError) as error:
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition Runtime projection is invalid"
            ) from error
        if not isinstance(runtime_image_uri, str):
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition Runtime image is invalid"
            )
        try:
            configuration = RuntimeConfigurationV1.from_mapping(
                configuration_raw,
                runtime_image_uri=runtime_image_uri,
                account=account,
                region=region,
            )
        except ContractError as error:
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition Runtime configuration is invalid"
            ) from error
        metadata_state = raw.get("metadataState")
        service_s3_state = raw.get("serviceS3EndpointState")
        if metadata_state not in {"ABSENT", "EMPTY", "TRUE", "FALSE", "NULL"}:
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition metadata state is invalid"
            )
        if service_s3_state not in {"ABSENT", "TRUE", "FALSE"}:
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition service S3 state is invalid"
            )
        projection_bytes = canonical_json_bytes(dict(raw))
        if hashlib.sha256(projection_bytes).hexdigest() != expected_sha256:
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition Runtime digest differs"
            )
        return _ReviewedRuntimeV1(
            configuration=configuration,
            metadata_state=str(metadata_state),
            service_s3_state=str(service_s3_state),
            projection_bytes=projection_bytes,
        )

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "AgentCoreHardeningPreconditionV1":
        if not isinstance(raw, Mapping) or set(raw) != cls.FIELDS:
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition fields are not exact"
            )
        if raw.get("schema") != cls.SCHEMA:
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition schema is invalid"
            )
        account = _text(raw.get("account"), label="account", pattern=_ACCOUNT)
        region = _text(raw.get("region"), label="region")
        runtime_sha256 = _text(
            raw.get("runtimeObservationSha256"),
            label="Runtime observation digest",
            pattern=_SHA64,
        )
        runtime = cls._runtime_from_mapping(
            raw.get("runtimeObservation"),
            account=account,
            region=region,
            expected_sha256=runtime_sha256,
        )
        value = cls(
            receipt_authority=raw.get("receiptAuthority"),
            resolved_request_sha256=raw.get("resolvedRequestSha256"),
            authority_sha256=raw.get("authoritySha256"),
            account=account,
            region=region,
            runtime=runtime,
            _token=_PRECONDITION_TOKEN,
        )
        if raw.get("mode") != value.mode:
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition mode is contradictory"
            )
        return value

    @classmethod
    def from_bytes(cls, payload: bytes) -> "AgentCoreHardeningPreconditionV1":
        try:
            value = cls.from_mapping(parse_canonical_object(payload))
        except AgentCoreHardeningError:
            raise
        except (ContractError, TypeError, ValueError) as error:
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition is not canonical"
            ) from error
        if value.to_bytes() != payload:
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition is not canonical"
            )
        return value

    @property
    def mode(self) -> str:
        if (
            self._runtime.metadata_state == "TRUE"
            and self._runtime.service_s3_state != "TRUE"
        ):
            return "NOOP"
        return "UPDATE"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "receiptAuthority": dict(self._receipt_authority),
            "resolvedRequestSha256": self._resolved_request_sha256,
            "authoritySha256": self._authority_sha256,
            "account": self._account,
            "region": self._region,
            "runtimeObservationSha256": self._runtime.digest(),
            "runtimeObservation": parse_canonical_object(
                self._runtime.projection_bytes
            ),
            "mode": self.mode,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def _binding(
        self, authority: AgentCoreHardeningAuthorityV1
    ) -> _ReviewedRuntimeV1:
        if type(authority) is not AgentCoreHardeningAuthorityV1:
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition lacks exact authority"
            )
        _, _, resolved, _, _, _ = authority._binding()
        if (
            self._authority_sha256 != authority.digest()
            or self._receipt_authority
            != _precondition_receipt_authority_mapping(authority)
            or self._resolved_request_sha256 != resolved.digest()
            or self._account != resolved.account
            or self._region != resolved.region
        ):
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition crosses its authority"
            )
        if not isinstance(self._runtime, _ReviewedRuntimeV1):
            raise AgentCoreHardeningError(
                "AgentCore hardening precondition state is invalid"
            )
        projection = parse_canonical_object(self._runtime.projection_bytes)
        raw = dict(self._runtime.configuration.to_mapping())
        raw.update(
            {
                "agentRuntimeId": projection["runtimeId"],
                "agentRuntimeName": RUNTIME_NAME,
                "agentRuntimeVersion": projection["runtimeVersion"],
                "agentRuntimeArn": projection["runtimeArn"],
                "status": projection["status"],
                "roleArn": projection["roleArn"],
                "description": projection["description"],
            }
        )
        metadata_values: dict[str, object] = {
            "EMPTY": {},
            "TRUE": {"requireMMDSV2": True},
            "FALSE": {"requireMMDSV2": False},
            "NULL": {"requireMMDSV2": None},
        }
        if self._runtime.metadata_state == "ABSENT":
            raw.pop("metadataConfiguration", None)
        else:
            raw["metadataConfiguration"] = metadata_values[
                self._runtime.metadata_state
            ]
        network = dict(raw["networkConfiguration"])
        vpc = dict(network["networkModeConfig"])
        if self._runtime.service_s3_state == "ABSENT":
            vpc.pop("requireServiceS3Endpoint", None)
        else:
            vpc["requireServiceS3Endpoint"] = (
                self._runtime.service_s3_state == "TRUE"
            )
        network["networkModeConfig"] = vpc
        raw["networkConfiguration"] = network
        recovered = _reviewed_runtime(
            raw,
            resolved=resolved,
            expected_version=resolved.runtime_version,
            expected_arn=resolved.runtime_arn,
            require_hardened=False,
        )
        if recovered.projection_bytes != self._runtime.projection_bytes:
            raise AgentCoreHardeningError(
                "retained AgentCore hardening precondition changed"
            )
        return recovered

    def _inspected_binding(
        self, authority: AgentCoreHardeningAuthorityV1
    ) -> _ReviewedRuntimeV1:
        if self._inspection_token is not _INSPECTED_PRECONDITION_TOKEN:
            raise AgentCoreHardeningError(
                "AgentCore hardening dispatch requires an inspected precondition"
            )
        return self._binding(authority)


class AgentCoreHardeningInspectorV1:
    """Perform two exact, stable, observer-only Runtime reads."""

    def __init__(self, client: object) -> None:
        self._client = client

    def inspect(
        self, authority: AgentCoreHardeningAuthorityV1
    ) -> AgentCoreHardeningPreconditionV1:
        if type(authority) is not AgentCoreHardeningAuthorityV1:
            raise AgentCoreHardeningError(
                "AgentCore hardening inspection requires exact authority"
            )
        _, _, resolved, _, _, _ = authority._binding()
        client = _validate_client(
            self._client,
            account=resolved.account,
            capability="observer",
        )
        arguments = {
            "agentRuntimeId": resolved.runtime_id,
            "agentRuntimeVersion": resolved.runtime_version,
        }
        reviewed: list[_ReviewedRuntimeV1] = []
        for _ in range(2):
            try:
                raw = client.invoke("get_agent_runtime", **arguments)
            except Exception as error:
                raise AgentCoreHardeningObservationAmbiguous(
                    "AgentCore Runtime precondition read failed"
                ) from error
            if not isinstance(raw, Mapping):
                raise AgentCoreHardeningObservationAmbiguous(
                    "AgentCore Runtime precondition response is malformed"
                )
            reviewed.append(
                _reviewed_runtime(
                    raw,
                    resolved=resolved,
                    expected_version=resolved.runtime_version,
                    expected_arn=resolved.runtime_arn,
                    require_hardened=False,
                )
            )
        if reviewed[0].projection_bytes != reviewed[1].projection_bytes:
            raise AgentCoreHardeningObservationAmbiguous(
                "AgentCore Runtime changed during hardening precondition reads"
            )
        return AgentCoreHardeningPreconditionV1(
            receipt_authority=_precondition_receipt_authority_mapping(
                authority
            ),
            resolved_request_sha256=resolved.digest(),
            authority_sha256=authority.digest(),
            account=resolved.account,
            region=resolved.region,
            runtime=reviewed[0],
            _inspected_token=_INSPECTED_PRECONDITION_TOKEN,
            _token=_PRECONDITION_TOKEN,
        )


_DISPATCH_RECEIPT_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class AgentCoreHardeningDispatchReceiptV1:
    """Exact acknowledgement retained immediately after the one-shot call."""

    SCHEMA = "personal-operator.agentcore-hardening-dispatch-receipt.v1"
    FIELDS = {
        "schema",
        "releasePlanSha256",
        "transactionId",
        "sourceCommit",
        "sourceTree",
        "account",
        "region",
        "evidenceStoreSha256",
        "journalPathSha256",
        "journalExecutionId",
        "journalRevision",
        "completedPrefixSha256",
        "stepId",
        "subject",
        "operationSha256",
        "resolvedRequestSha256",
        "preconditionSha256",
        "mode",
        "runtimeId",
        "priorRuntimeVersion",
        "resultingRuntimeVersion",
        "resultingRuntimeArn",
        "updateRequestSha256",
        "providerAcknowledgementStatus",
    }

    release_plan_sha256: str
    transaction_id: str
    source_commit: str
    source_tree: str
    account: str
    region: str
    evidence_store_sha256: str
    journal_path_sha256: str
    journal_execution_id: str
    journal_revision: int
    completed_prefix_sha256: str
    step_id: str
    subject: str
    operation_sha256: str
    resolved_request_sha256: str
    precondition_sha256: str
    mode: str
    runtime_id: str
    prior_runtime_version: str
    resulting_runtime_version: str
    resulting_runtime_arn: str
    update_request_sha256: str
    provider_acknowledgement_status: str

    def __init__(
        self,
        *,
        release_plan_sha256: str = "",
        transaction_id: str = "",
        source_commit: str = "",
        source_tree: str = "",
        account: str = "",
        region: str = "",
        evidence_store_sha256: str = "",
        journal_path_sha256: str = "",
        journal_execution_id: str = "",
        journal_revision: int = 0,
        completed_prefix_sha256: str = "",
        step_id: str = "",
        subject: str = "",
        operation_sha256: str = "",
        resolved_request_sha256: str = "",
        precondition_sha256: str = "",
        mode: str = "",
        runtime_id: str = "",
        prior_runtime_version: str = "",
        resulting_runtime_version: str = "",
        resulting_runtime_arn: str = "",
        update_request_sha256: str = "",
        provider_acknowledgement_status: str = "",
        _token: object | None = None,
    ) -> None:
        if _token is not _DISPATCH_RECEIPT_TOKEN:
            raise AgentCoreHardeningError(
                "AgentCore hardening dispatch receipt is not constructible"
            )
        for field, value in (
            ("release_plan_sha256", release_plan_sha256),
            ("transaction_id", transaction_id),
            ("source_commit", source_commit),
            ("source_tree", source_tree),
            ("account", account),
            ("region", region),
            ("evidence_store_sha256", evidence_store_sha256),
            ("journal_path_sha256", journal_path_sha256),
            ("journal_execution_id", journal_execution_id),
            ("journal_revision", journal_revision),
            ("completed_prefix_sha256", completed_prefix_sha256),
            ("step_id", step_id),
            ("subject", subject),
            ("operation_sha256", operation_sha256),
            ("resolved_request_sha256", resolved_request_sha256),
            ("precondition_sha256", precondition_sha256),
            ("mode", mode),
            ("runtime_id", runtime_id),
            ("prior_runtime_version", prior_runtime_version),
            ("resulting_runtime_version", resulting_runtime_version),
            ("resulting_runtime_arn", resulting_runtime_arn),
            ("update_request_sha256", update_request_sha256),
            ("provider_acknowledgement_status", provider_acknowledgement_status),
        ):
            object.__setattr__(self, field, value)

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "AgentCoreHardeningDispatchReceiptV1":
        if not isinstance(raw, Mapping) or set(raw) != cls.FIELDS:
            raise AgentCoreHardeningError(
                "AgentCore hardening dispatch receipt fields are not exact"
            )
        if raw.get("schema") != cls.SCHEMA:
            raise AgentCoreHardeningError(
                "AgentCore hardening dispatch receipt schema is invalid"
            )
        release_plan_sha256 = _text(
            raw.get("releasePlanSha256"),
            label="release plan digest",
            pattern=_SHA64,
        )
        transaction_id = _text(raw.get("transactionId"), label="transaction ID")
        source_commit = _text(
            raw.get("sourceCommit"), label="source commit", pattern=_SHA40
        )
        if transaction_id != f"release_{source_commit}":
            raise AgentCoreHardeningError(
                "AgentCore hardening receipt transaction is not commit-bound"
            )
        source_tree = _text(
            raw.get("sourceTree"), label="source tree", pattern=_SHA40
        )
        account = _text(raw.get("account"), label="account", pattern=_ACCOUNT)
        if account == "000000000000":
            raise AgentCoreHardeningError("account is invalid")
        region = _text(raw.get("region"), label="region")
        if region != REQUIRED_REGION:
            raise AgentCoreHardeningError(
                "AgentCore hardening receipt region is invalid"
            )
        storage_binding = _ReceiptStorageBindingV1.from_mapping(
            {
                "evidenceStoreSha256": raw.get("evidenceStoreSha256"),
                "journalPathSha256": raw.get("journalPathSha256"),
                "journalExecutionId": raw.get("journalExecutionId"),
            }
        )
        revision = raw.get("journalRevision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise AgentCoreHardeningError(
                "AgentCore hardening receipt journal revision is invalid"
            )
        completed_prefix = _text(
            raw.get("completedPrefixSha256"),
            label="completed prefix digest",
            pattern=_SHA64,
        )
        step_id = _text(raw.get("stepId"), label="step ID")
        subject = _text(raw.get("subject"), label="subject")
        expected_subject = (
            f"agentcore:{account}:{region}:runtime:{RUNTIME_NAME}:"
            f"release:{source_commit}:mmdsv2"
        )
        if subject != expected_subject:
            raise AgentCoreHardeningError(
                "AgentCore hardening receipt subject crosses the release"
            )
        operation_sha256 = _text(
            raw.get("operationSha256"),
            label="operation digest",
            pattern=_DIGEST,
        )
        resolved_sha256 = _text(
            raw.get("resolvedRequestSha256"),
            label="resolved request digest",
            pattern=_SHA64,
        )
        precondition_sha256 = _text(
            raw.get("preconditionSha256"),
            label="precondition digest",
            pattern=_SHA64,
        )
        mode = _text(raw.get("mode"), label="dispatch mode")
        if mode not in {"NOOP", "UPDATED"}:
            raise AgentCoreHardeningError(
                "AgentCore hardening receipt mode is invalid"
            )
        runtime_id = _text(
            raw.get("runtimeId"), label="runtime ID", pattern=_RUNTIME_ID
        )
        prior_version = _text(
            raw.get("priorRuntimeVersion"),
            label="prior runtime version",
            pattern=_VERSION,
        )
        resulting_version = _text(
            raw.get("resultingRuntimeVersion"),
            label="resulting runtime version",
            pattern=_VERSION,
        )
        resulting_arn = _text(raw.get("resultingRuntimeArn"), label="runtime ARN")
        expected_arn = re.fullmatch(
            rf"arn:aws:bedrock-agentcore:{re.escape(region)}:"
            rf"{re.escape(account)}:agent/"
            r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
            r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:"
            rf"{re.escape(resulting_version)}",
            resulting_arn,
        )
        if expected_arn is None:
            raise AgentCoreHardeningError(
                "AgentCore hardening receipt runtime ARN is invalid"
            )
        update_request_sha256 = raw.get("updateRequestSha256")
        if not isinstance(update_request_sha256, str):
            raise AgentCoreHardeningError(
                "AgentCore hardening update request digest is invalid"
            )
        acknowledgement = _text(
            raw.get("providerAcknowledgementStatus"),
            label="provider acknowledgement status",
        )
        if mode == "NOOP":
            if (
                resulting_version != prior_version
                or update_request_sha256
                or acknowledgement != "READY"
            ):
                raise AgentCoreHardeningError(
                    "AgentCore hardening no-op receipt is contradictory"
                )
        elif (
            int(resulting_version) <= int(prior_version)
            or _SHA64.fullmatch(update_request_sha256) is None
            or acknowledgement
            not in {"CREATING", "UPDATING", "READY", "UPDATE_FAILED"}
        ):
            raise AgentCoreHardeningError(
                "AgentCore hardening update receipt is contradictory"
            )
        return cls(
            release_plan_sha256=release_plan_sha256,
            transaction_id=transaction_id,
            source_commit=source_commit,
            source_tree=source_tree,
            account=account,
            region=region,
            evidence_store_sha256=storage_binding.evidence_store_sha256,
            journal_path_sha256=storage_binding.journal_path_sha256,
            journal_execution_id=storage_binding.journal_execution_id,
            journal_revision=revision,
            completed_prefix_sha256=completed_prefix,
            step_id=step_id,
            subject=subject,
            operation_sha256=operation_sha256,
            resolved_request_sha256=resolved_sha256,
            precondition_sha256=precondition_sha256,
            mode=mode,
            runtime_id=runtime_id,
            prior_runtime_version=prior_version,
            resulting_runtime_version=resulting_version,
            resulting_runtime_arn=resulting_arn,
            update_request_sha256=update_request_sha256,
            provider_acknowledgement_status=acknowledgement,
            _token=_DISPATCH_RECEIPT_TOKEN,
        )

    @classmethod
    def from_bytes(
        cls, payload: bytes
    ) -> "AgentCoreHardeningDispatchReceiptV1":
        try:
            return cls.from_mapping(parse_canonical_object(payload))
        except AgentCoreHardeningError:
            raise
        except (ContractError, TypeError, ValueError) as error:
            raise AgentCoreHardeningError(
                "AgentCore hardening dispatch receipt is not canonical"
            ) from error

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "releasePlanSha256": self.release_plan_sha256,
            "transactionId": self.transaction_id,
            "sourceCommit": self.source_commit,
            "sourceTree": self.source_tree,
            "account": self.account,
            "region": self.region,
            "evidenceStoreSha256": self.evidence_store_sha256,
            "journalPathSha256": self.journal_path_sha256,
            "journalExecutionId": self.journal_execution_id,
            "journalRevision": self.journal_revision,
            "completedPrefixSha256": self.completed_prefix_sha256,
            "stepId": self.step_id,
            "subject": self.subject,
            "operationSha256": self.operation_sha256,
            "resolvedRequestSha256": self.resolved_request_sha256,
            "preconditionSha256": self.precondition_sha256,
            "mode": self.mode,
            "runtimeId": self.runtime_id,
            "priorRuntimeVersion": self.prior_runtime_version,
            "resultingRuntimeVersion": self.resulting_runtime_version,
            "resultingRuntimeArn": self.resulting_runtime_arn,
            "updateRequestSha256": self.update_request_sha256,
            "providerAcknowledgementStatus": (
                self.provider_acknowledgement_status
            ),
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


def _update_request(
    *,
    authority: AgentCoreHardeningAuthorityV1,
    precondition: AgentCoreHardeningPreconditionV1,
) -> dict[str, Any]:
    operation, plan, resolved, _, _, _ = authority._binding()
    reviewed = precondition._binding(authority)
    configuration = reviewed.configuration.to_mapping()
    network = dict(configuration["networkConfiguration"])
    vpc = dict(network["networkModeConfig"])
    if reviewed.service_s3_state == "TRUE":
        vpc["requireServiceS3Endpoint"] = False
    network["networkModeConfig"] = vpc
    # AgentCore models disabled authorizer/header settings as absent (or as an
    # empty read projection).  Their update shapes are tagged unions, so `{}`
    # is not serializable.  The stable precondition above proves both are
    # disabled before their omission here preserves that reviewed state.
    request: dict[str, Any] = {
        "agentRuntimeId": resolved.runtime_id,
        "agentRuntimeArtifact": configuration["agentRuntimeArtifact"],
        "roleArn": expected_execution_role_arn(
            resolved.account, resolved.region
        ),
        "networkConfiguration": network,
        "description": (
            "Personal Operator immutable bridge runtime at commit "
            f"{resolved.source_commit}"
        ),
        "protocolConfiguration": configuration["protocolConfiguration"],
        "lifecycleConfiguration": configuration["lifecycleConfiguration"],
        "metadataConfiguration": {"requireMMDSV2": True},
        "environmentVariables": configuration["environmentVariables"],
        "filesystemConfigurations": configuration["filesystemConfigurations"],
    }
    token_material = canonical_json_bytes(
        {
            "planSha256": plan.digest(),
            "operationSha256": operation.digest(),
            "resolvedRequestSha256": resolved.digest(),
            "preconditionSha256": precondition.digest(),
            "request": request,
        }
    )
    request["clientToken"] = (
        "mmdsv2-" + hashlib.sha256(token_material).hexdigest()
    )
    return request


def _expected_receipt(
    *,
    authority: AgentCoreHardeningAuthorityV1,
    precondition: AgentCoreHardeningPreconditionV1,
    mode: str,
    resulting_version: str,
    acknowledgement: str,
    update_request_sha256: str,
) -> AgentCoreHardeningDispatchReceiptV1:
    operation, plan, resolved, transaction, _, storage_binding = (
        authority._binding()
    )
    precondition._binding(authority)
    resulting_arn = (
        resolved.runtime_arn
        if mode == "NOOP"
        else resolved.runtime_arn.rsplit(":", 1)[0] + ":" + resulting_version
    )
    return AgentCoreHardeningDispatchReceiptV1.from_mapping(
        {
            "schema": AgentCoreHardeningDispatchReceiptV1.SCHEMA,
            "releasePlanSha256": plan.digest(),
            "transactionId": transaction.transaction_id,
            "sourceCommit": resolved.source_commit,
            "sourceTree": resolved.source_tree,
            "account": resolved.account,
            "region": resolved.region,
            "evidenceStoreSha256": storage_binding.evidence_store_sha256,
            "journalPathSha256": storage_binding.journal_path_sha256,
            "journalExecutionId": storage_binding.journal_execution_id,
            "journalRevision": transaction.revision,
            "completedPrefixSha256": (
                resolved.mutation_request.completed_prefix_sha256
            ),
            "stepId": resolved.mutation_request.step_id,
            "subject": operation.subject,
            "operationSha256": resolved.mutation_request.operation_sha256,
            "resolvedRequestSha256": resolved.digest(),
            "preconditionSha256": precondition.digest(),
            "mode": mode,
            "runtimeId": resolved.runtime_id,
            "priorRuntimeVersion": resolved.runtime_version,
            "resultingRuntimeVersion": resulting_version,
            "resultingRuntimeArn": resulting_arn,
            "updateRequestSha256": update_request_sha256,
            "providerAcknowledgementStatus": acknowledgement,
        }
    )


def _validate_receipt_base(
    receipt: AgentCoreHardeningDispatchReceiptV1,
    *,
    authority: AgentCoreHardeningAuthorityV1,
) -> None:
    operation, plan, resolved, transaction, _, storage_binding = (
        authority._binding()
    )
    if (
        receipt.release_plan_sha256 != plan.digest()
        or receipt.transaction_id != transaction.transaction_id
        or receipt.source_commit != resolved.source_commit
        or receipt.source_tree != resolved.source_tree
        or receipt.account != resolved.account
        or receipt.region != resolved.region
        or receipt.evidence_store_sha256
        != storage_binding.evidence_store_sha256
        or receipt.journal_path_sha256 != storage_binding.journal_path_sha256
        or receipt.journal_execution_id
        != storage_binding.journal_execution_id
        or receipt.journal_revision != transaction.revision
        or receipt.completed_prefix_sha256
        != resolved.mutation_request.completed_prefix_sha256
        or receipt.step_id != resolved.mutation_request.step_id
        or receipt.subject != operation.subject
        or receipt.operation_sha256
        != resolved.mutation_request.operation_sha256
        or receipt.resolved_request_sha256 != resolved.digest()
        or receipt.runtime_id != resolved.runtime_id
        or receipt.prior_runtime_version != resolved.runtime_version
        or receipt.resulting_runtime_arn.rsplit(":", 1)[0]
        != resolved.runtime_arn.rsplit(":", 1)[0]
    ):
        raise AgentCoreHardeningError(
            "retained AgentCore hardening receipt crosses its authority"
        )


_VERIFIED_RECEIPT_TOKEN = object()


class VerifiedAgentCoreHardeningReceiptV1:
    """Capability over one exact, synchronously retained receipt."""

    __slots__ = ("_receipt", "_authority_sha256")

    def __init__(
        self,
        *,
        receipt: AgentCoreHardeningDispatchReceiptV1 | None = None,
        authority_sha256: str = "",
        _token: object | None = None,
    ) -> None:
        if _token is not _VERIFIED_RECEIPT_TOKEN:
            raise AgentCoreHardeningError(
                "verified AgentCore hardening receipt is not constructible"
            )
        if type(receipt) is not AgentCoreHardeningDispatchReceiptV1:
            raise AgentCoreHardeningError(
                "verified AgentCore hardening receipt is invalid"
            )
        self._receipt = receipt
        self._authority_sha256 = authority_sha256

    @property
    def receipt(self) -> AgentCoreHardeningDispatchReceiptV1:
        return AgentCoreHardeningDispatchReceiptV1.from_bytes(
            self._receipt.to_bytes()
        )

    def _binding(
        self, authority: AgentCoreHardeningAuthorityV1
    ) -> AgentCoreHardeningDispatchReceiptV1:
        if type(authority) is not AgentCoreHardeningAuthorityV1:
            raise AgentCoreHardeningError(
                "verified AgentCore hardening receipt lacks exact authority"
            )
        if self._authority_sha256 != authority.digest():
            raise AgentCoreHardeningError(
                "verified AgentCore hardening receipt crosses its authority"
            )
        receipt = self.receipt
        _validate_receipt_base(receipt, authority=authority)
        return receipt


def _verified_retained_receipt(
    payload: bytes,
    *,
    authority: AgentCoreHardeningAuthorityV1,
    precondition: AgentCoreHardeningPreconditionV1,
) -> VerifiedAgentCoreHardeningReceiptV1:
    if (
        type(authority) is not AgentCoreHardeningAuthorityV1
        or type(precondition) is not AgentCoreHardeningPreconditionV1
    ):
        raise AgentCoreHardeningError(
            "retained AgentCore hardening receipt lacks exact authority"
        )
    receipt = AgentCoreHardeningDispatchReceiptV1.from_bytes(payload)
    _validate_receipt_base(receipt, authority=authority)
    precondition._binding(authority)
    if receipt.precondition_sha256 != precondition.digest():
        raise AgentCoreHardeningError(
            "retained AgentCore hardening receipt differs from its precondition"
        )
    if receipt.mode == "NOOP":
        if precondition.mode != "NOOP" or receipt.to_bytes() != _expected_receipt(
            authority=authority,
            precondition=precondition,
            mode="NOOP",
            resulting_version=receipt.prior_runtime_version,
            acknowledgement="READY",
            update_request_sha256="",
        ).to_bytes():
            raise AgentCoreHardeningError(
                "retained AgentCore hardening no-op receipt differs"
            )
    else:
        if precondition.mode != "UPDATE":
            raise AgentCoreHardeningError(
                "retained AgentCore hardening update receipt differs"
            )
        request_digest = hashlib.sha256(
            canonical_json_bytes(
                _update_request(
                    authority=authority,
                    precondition=precondition,
                )
            )
        ).hexdigest()
        if receipt.update_request_sha256 != request_digest:
            raise AgentCoreHardeningError(
                "retained AgentCore hardening update receipt request differs"
            )
    return VerifiedAgentCoreHardeningReceiptV1(
        receipt=receipt,
        authority_sha256=authority.digest(),
        _token=_VERIFIED_RECEIPT_TOKEN,
    )


class AgentCoreHardeningDispatcherV1:
    """Perform at most one exact UpdateAgentRuntime after durable intent."""

    def __init__(self, client: object) -> None:
        self._client = client

    def dispatch(
        self,
        authority: AgentCoreHardeningAuthorityV1,
        precondition: AgentCoreHardeningPreconditionV1,
        fresh_authority: FreshDispatchAuthorityV1,
    ) -> ReleaseDispatchAttemptV1:
        if (
            type(authority) is not AgentCoreHardeningAuthorityV1
            or type(precondition) is not AgentCoreHardeningPreconditionV1
        ):
            raise AgentCoreHardeningError(
                "AgentCore hardening dispatch lacks exact authority"
            )
        if type(fresh_authority) is not FreshDispatchAuthorityV1:
            raise AgentCoreHardeningError(
                "AgentCore hardening dispatch lacks exact fresh dispatch authority"
            )
        _, _, resolved, _, sink, _ = authority._binding()
        precondition._inspected_binding(authority)
        client = _validate_client(
            self._client,
            account=resolved.account,
            capability="mutation",
        )
        attempted, retained = sink._load()
        retained_precondition = sink._load_precondition()
        if retained_precondition is None:
            if attempted or retained is not None:
                raise AgentCoreHardeningDispatchAmbiguous(
                    "AgentCore hardening attempt lacks its durable precondition; "
                    "release remains UNCERTAIN"
                )
            sink._retain_precondition(precondition.to_bytes())
            retained_precondition = sink._load_precondition()
        if retained_precondition != precondition.to_bytes():
            raise AgentCoreHardeningError(
                "retained AgentCore hardening precondition differs"
            )
        durable_precondition = AgentCoreHardeningPreconditionV1.from_bytes(
            retained_precondition
        )
        durable_precondition._binding(authority)
        precondition = durable_precondition
        if retained is not None:
            _verified_retained_receipt(
                retained,
                authority=authority,
                precondition=precondition,
            )
            raise AgentCoreHardeningDispatchAmbiguous(
                "AgentCore hardening already has a retained receipt and cannot be "
                "replayed; release remains UNCERTAIN"
            )
        if attempted:
            raise AgentCoreHardeningDispatchAmbiguous(
                "AgentCore hardening was attempted without a receipt; release "
                "remains UNCERTAIN"
            )
        try:
            consumed_attempt = fresh_authority.consume(
                provider="AGENTCORE",
                operation_sha256=resolved.mutation_request.operation_sha256,
                resolved_request_sha256=resolved.digest(),
            )
        except DispatchAttemptError as error:
            raise AgentCoreHardeningError(
                "AgentCore hardening fresh dispatch authority differs from its "
                "operation"
            ) from error
        if not sink._begin_attempt():
            attempted, retained = sink._load()
            if retained is not None:
                _verified_retained_receipt(
                    retained,
                    authority=authority,
                    precondition=precondition,
                )
            raise AgentCoreHardeningDispatchAmbiguous(
                "AgentCore hardening attempt raced without a receipt; release "
                "remains UNCERTAIN"
            )
        if precondition.mode == "NOOP":
            receipt = _expected_receipt(
                authority=authority,
                precondition=precondition,
                mode="NOOP",
                resulting_version=resolved.runtime_version,
                acknowledgement="READY",
                update_request_sha256="",
            )
        else:
            request = _update_request(
                authority=authority,
                precondition=precondition,
            )
            try:
                response = client.invoke("update_agent_runtime", **request)
            except Exception as error:
                raise AgentCoreHardeningDispatchAmbiguous(
                    "UpdateAgentRuntime has an unknown effect; release remains "
                    "UNCERTAIN"
                ) from error
            try:
                if not isinstance(response, Mapping):
                    raise AgentCoreHardeningError(
                        "UpdateAgentRuntime acknowledgement is malformed"
                    )
                runtime_id = response.get("agentRuntimeId")
                version = response.get("agentRuntimeVersion")
                status = response.get("status")
                if (
                    runtime_id != resolved.runtime_id
                    or not isinstance(version, str)
                    or _VERSION.fullmatch(version) is None
                    or int(version) <= int(resolved.runtime_version)
                    or status
                    not in {"CREATING", "UPDATING", "READY", "UPDATE_FAILED"}
                ):
                    raise AgentCoreHardeningError(
                        "UpdateAgentRuntime acknowledgement is not exact"
                    )
                receipt = _expected_receipt(
                    authority=authority,
                    precondition=precondition,
                    mode="UPDATED",
                    resulting_version=version,
                    acknowledgement=status,
                    update_request_sha256=hashlib.sha256(
                        canonical_json_bytes(request)
                    ).hexdigest(),
                )
            except AgentCoreHardeningError as error:
                raise AgentCoreHardeningDispatchAmbiguous(
                    "UpdateAgentRuntime returned an ambiguous acknowledgement; "
                    "release remains UNCERTAIN"
                ) from error
        payload = receipt.to_bytes()
        sink._retain(payload)
        attempted_after, retained_after = sink._load()
        if not attempted_after or retained_after != payload:
            raise AgentCoreHardeningDispatchAmbiguous(
                "AgentCore hardening receipt retention is not byte-exact; release "
                "remains UNCERTAIN"
            )
        _verified_retained_receipt(
            retained_after,
            authority=authority,
            precondition=precondition,
        )
        return consumed_attempt


class AgentCoreHardeningObserverV1:
    """Observe only the exact no-op or provider-returned Runtime version."""

    def __init__(self, client: object) -> None:
        self._client = client

    def observe(
        self,
        authority: AgentCoreHardeningAuthorityV1,
        verified_receipt: VerifiedAgentCoreHardeningReceiptV1,
    ):
        if (
            type(authority) is not AgentCoreHardeningAuthorityV1
            or type(verified_receipt) is not VerifiedAgentCoreHardeningReceiptV1
        ):
            raise AgentCoreHardeningError(
                "AgentCore hardening observation lacks exact authority"
            )
        _, _, resolved, _, _, _ = authority._binding()
        receipt = verified_receipt._binding(authority)
        client = _validate_client(
            self._client,
            account=resolved.account,
            capability="observer",
        )
        arguments = {
            "agentRuntimeId": receipt.runtime_id,
            "agentRuntimeVersion": receipt.resulting_runtime_version,
        }
        reviewed: list[_ReviewedRuntimeV1] = []
        raw_reads: list[Mapping[str, Any]] = []
        for _ in range(2):
            try:
                raw = client.invoke("get_agent_runtime", **arguments)
            except Exception as error:
                raise AgentCoreHardeningObservationAmbiguous(
                    "AgentCore hardening reconciliation read failed"
                ) from error
            if not isinstance(raw, Mapping):
                raise AgentCoreHardeningObservationAmbiguous(
                    "AgentCore hardening reconciliation response is malformed"
                )
            raw_reads.append(raw)
        statuses = tuple(raw.get("status") for raw in raw_reads)
        if len(set(statuses)) != 1:
            raise AgentCoreHardeningObservationAmbiguous(
                "AgentCore Runtime status changed during reconciliation"
            )
        status = statuses[0]
        if receipt.mode == "NOOP" and status != "READY":
            raise AgentCoreHardeningObservationAmbiguous(
                "no-op AgentCore Runtime is no longer stably READY"
            )
        if status == "READY":
            disposition = ObservationDisposition.PRESENT
            require_hardened = True
        elif receipt.mode == "UPDATED" and status in {"CREATING", "UPDATING"}:
            disposition = ObservationDisposition.PENDING
            require_hardened = False
        elif receipt.mode == "UPDATED" and status == "UPDATE_FAILED":
            disposition = ObservationDisposition.FAILED_RETAINED
            require_hardened = False
        else:
            raise AgentCoreHardeningError(
                "AgentCore Runtime returned an unreviewed reconciliation status"
            )
        for raw in raw_reads:
            reviewed.append(
                _reviewed_runtime(
                    raw,
                    resolved=resolved,
                    expected_version=receipt.resulting_runtime_version,
                    expected_arn=receipt.resulting_runtime_arn,
                    require_hardened=require_hardened,
                    allowed_statuses=frozenset({str(status)}),
                )
            )
        if reviewed[0].projection_bytes != reviewed[1].projection_bytes:
            raise AgentCoreHardeningObservationAmbiguous(
                "AgentCore Runtime changed during exact hardening reconciliation"
            )
        foundation = resolved.foundation_runtime_inputs
        assert foundation is not None
        role = expected_execution_role_arn(resolved.account, resolved.region)
        return _new_observation(
            service="bedrock-agentcore-control",
            operation="get_agent_runtime",
            subject=receipt.subject,
            disposition=disposition,
            provider_status=str(status),
            projection={
                "agentCoreStackId": resolved.agent_core_stack_id,
                "runtimeId": receipt.runtime_id,
                "runtimeVersion": receipt.resulting_runtime_version,
                "runtimeArn": receipt.resulting_runtime_arn,
                "runtimeConfigurationSha256": (
                    reviewed[0].configuration.digest_for_role(role)
                ),
                "hardeningReceiptSha256": receipt.digest(),
                "preconditionSha256": receipt.precondition_sha256,
                "guardrailId": foundation.guardrail_id,
                "guardrailVersion": foundation.guardrail_version,
                "requiresMMDSV2": (
                    reviewed[0].metadata_state == "TRUE"
                ),
                "requiresServiceS3Endpoint": (
                    reviewed[0].service_s3_state == "TRUE"
                ),
            },
        )


__all__ = [
    "AgentCoreHardeningDispatchAmbiguous",
    "AgentCoreHardeningDispatchReceiptV1",
    "AgentCoreHardeningDispatcherV1",
    "AgentCoreHardeningAuthorityV1",
    "AgentCoreHardeningError",
    "AgentCoreHardeningInspectorV1",
    "AgentCoreHardeningObservationAmbiguous",
    "AgentCoreHardeningObserverV1",
    "AgentCoreHardeningOperationV1",
    "AgentCoreHardeningPreconditionV1",
    "AgentCoreHardeningReceiptSinkV1",
    "VerifiedAgentCoreHardeningPreflightV1",
    "VerifiedAgentCoreHardeningReceiptV1",
    "_new_agentcore_hardening_receipt_sink",
    "validate_agentcore_hardening_authority",
    "validate_agentcore_hardening_preflight",
]
