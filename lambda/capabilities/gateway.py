"""Typed capability admission gateway with no production adapters enabled."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

from .admission import (
    AdmissionDenied,
    AdmissionGate,
    AdmissionRepository,
    AdmittedCall,
)
from .contracts import (
    CapabilityCallV1,
    CapabilityCatalogV1,
    CapabilityResultV1,
    ContractValidationError,
    canonical_json_bytes,
)
from .ledger import (
    CapabilityLedger,
    LedgerDenied,
    LedgerDisposition,
)


@dataclass(frozen=True, slots=True)
class AdapterOutcome:
    status: str
    data: Mapping[str, Any] = field(default_factory=dict)
    provenance_refs: Sequence[str] = ()
    proposal_ref: str | None = None
    receipt_ref: str | None = None
    error_code: str | None = None
    retry_policy: str = "NONE"


class CapabilityAdapter(Protocol):
    def invoke(self, admitted: AdmittedCall) -> AdapterOutcome: ...


def _result(
    call: CapabilityCallV1,
    *,
    status: str,
    data: Mapping[str, Any] | None = None,
    provenance_refs: Sequence[str] = (),
    proposal_ref: str | None = None,
    receipt_ref: str | None = None,
    error_code: str | None = None,
    retry_policy: str = "NONE",
) -> CapabilityResultV1:
    return CapabilityResultV1.from_mapping(
        {
            "schema": CapabilityResultV1.SCHEMA,
            "callId": call.call_id,
            "invocationId": call.invocation_id,
            "toolUseId": call.tool_use_id,
            "catalogDigest": call.catalog_digest,
            "operationId": call.operation_id,
            "toolName": call.tool_name,
            "argsHash": call.args_hash,
            "status": status,
            "data": dict(data or {}),
            "provenanceRefs": list(provenance_refs),
            "proposalRef": proposal_ref,
            "receiptRef": receipt_ref,
            "errorCode": error_code,
            "retryPolicy": retry_policy,
        }
    ).validate_against_call(call)


def _denied(call: CapabilityCallV1, code: str) -> CapabilityResultV1:
    safe_code = code if isinstance(code, str) and 0 < len(code) <= 128 else "DENIED"
    return _result(call, status="DENIED", error_code=safe_code)


def _ambiguous(
    call: CapabilityCallV1,
    retry_mode: str,
    code: str,
) -> CapabilityResultV1:
    if retry_mode == "READ_ONLY":
        return _result(
            call,
            status="FAILED_RETRYABLE",
            error_code=code,
            retry_policy="SAFE_RETRY",
        )
    return _result(
        call,
        status="UNCERTAIN",
        error_code=code,
        retry_policy="RECONCILE_ONLY",
    )


def _catalog_operation_ids(catalog: CapabilityCatalogV1) -> frozenset[str]:
    return frozenset(
        operation["operationId"]
        for pack in catalog.packs
        for operation in pack["operations"]
    )


class CapabilityGateway:
    """Admit, replay-protect, and dispatch one exact catalog operation."""

    def __init__(
        self,
        *,
        catalog: CapabilityCatalogV1,
        repository: AdmissionRepository,
        ledger: CapabilityLedger,
        adapters: Mapping[str, CapabilityAdapter] | None = None,
        allowed_caller_arn: str,
        clock: Callable[[], int],
    ) -> None:
        if not isinstance(catalog, CapabilityCatalogV1):
            raise TypeError("gateway requires a validated catalog")
        configured = dict(adapters or {})
        unknown = set(configured) - _catalog_operation_ids(catalog)
        if unknown:
            raise ValueError("gateway adapter is absent from the frozen catalog")
        if any(
            not callable(getattr(adapter, "invoke", None))
            for adapter in configured.values()
        ):
            raise TypeError("gateway adapters must implement invoke")
        self._admission = AdmissionGate(
            catalog=catalog,
            repository=repository,
            allowed_caller_arn=allowed_caller_arn,
            clock=clock,
        )
        self._ledger = ledger
        self._adapters = configured

    def invoke(
        self,
        call: CapabilityCallV1 | Mapping[str, Any],
        iam_context: Mapping[str, Any],
    ) -> CapabilityResultV1:
        validated_call = (
            call
            if isinstance(call, CapabilityCallV1)
            else CapabilityCallV1.from_mapping(call)
        )
        try:
            admitted = self._admission.admit(validated_call, iam_context)
        except AdmissionDenied as error:
            return _denied(validated_call, error.code)

        try:
            claim = self._ledger.begin(
                call=validated_call,
                grant=admitted.grant,
                pack_id=admitted.pack_id,
                pack_max_calls=admitted.pack["quotaPolicy"]["maxCallsPerTurn"],
                retry_mode=admitted.retry_mode,
            )
        except LedgerDenied as error:
            return _denied(validated_call, error.code)
        except Exception:
            return _denied(validated_call, "CAPABILITY_LEDGER_UNAVAILABLE")

        if claim.disposition is LedgerDisposition.CACHED:
            if claim.result is None:
                raise RuntimeError("completed ledger entry has no typed result")
            return claim.result
        if claim.disposition is LedgerDisposition.IN_FLIGHT:
            return _ambiguous(
                validated_call,
                admitted.retry_mode,
                "CAPABILITY_CALL_IN_FLIGHT",
            )
        if claim.disposition is LedgerDisposition.LOGICAL_FENCE:
            return _ambiguous(
                validated_call,
                admitted.retry_mode,
                "CAPABILITY_LOGICAL_EFFECT_UNCERTAIN",
            )
        if claim.disposition is LedgerDisposition.RETRY_EXHAUSTED:
            return _denied(
                validated_call,
                "CAPABILITY_READ_RETRY_EXHAUSTED",
            )

        adapter = self._adapters.get(validated_call.operation_id)
        if adapter is None:
            result = _denied(validated_call, "ADAPTER_DISABLED")
            return self._complete_result(admitted, result, effect_dispatched=False)

        try:
            self._admission.claim_target(admitted)
            self._admission.recheck_deletion_fence(admitted)
        except AdmissionDenied as error:
            result = _denied(validated_call, error.code)
            return self._complete_result(admitted, result, effect_dispatched=False)

        try:
            outcome = adapter.invoke(admitted)
            result = self._result_from_adapter(admitted, outcome)
        except Exception:
            result = _ambiguous(
                validated_call,
                admitted.retry_mode,
                "ADAPTER_OUTCOME_UNAVAILABLE",
            )
        return self._complete_result(admitted, result, effect_dispatched=True)

    def _complete_result(
        self,
        admitted: AdmittedCall,
        result: CapabilityResultV1,
        *,
        effect_dispatched: bool,
    ) -> CapabilityResultV1:
        try:
            self._ledger.complete(
                call=admitted.call,
                grant=admitted.grant,
                result=result,
            )
            return result
        except Exception:
            if not effect_dispatched:
                return result

        ambiguous = _ambiguous(
            admitted.call,
            admitted.retry_mode,
            "CAPABILITY_COMPLETION_UNAVAILABLE",
        )
        try:
            # A transient post-dispatch completion failure must leave a durable
            # typed fence. If persistence is still unavailable, the original
            # IN_FLIGHT logical claim remains the conservative fence.
            self._ledger.complete(
                call=admitted.call,
                grant=admitted.grant,
                result=ambiguous,
            )
        except Exception:
            pass
        return ambiguous

    @staticmethod
    def _result_from_adapter(
        admitted: AdmittedCall, outcome: AdapterOutcome
    ) -> CapabilityResultV1:
        if not isinstance(outcome, AdapterOutcome):
            raise TypeError("adapter returned an untyped outcome")
        output_size = len(canonical_json_bytes(outcome.data))
        if output_size > admitted.pack["quotaPolicy"]["maxOutputBytes"]:
            raise ValueError("adapter output exceeds the pack quota")
        return _result(
            admitted.call,
            status=outcome.status,
            data=outcome.data,
            provenance_refs=outcome.provenance_refs,
            proposal_ref=outcome.proposal_ref,
            receipt_ref=outcome.receipt_ref,
            error_code=outcome.error_code,
            retry_policy=outcome.retry_policy,
        )


WEB_READ_OPERATION_ID = "web.exact.read"


def build_web_read_adapter(
    *,
    resolver: Callable[[str], Any],
    connect: Callable[..., Any],
    clock: Callable[[], int],
    max_redirects: int = 0,
) -> "CapabilityAdapter":
    """Build the gateway-mediated exact-target web reader adapter.

    This is the single wiring point for the reader: it can only enter the
    system as an ``adapters`` entry keyed by ``WEB_READ_OPERATION_ID`` on a
    :class:`CapabilityGateway`, and it holds no network authority beyond the
    explicit ``resolver``/``connect`` seams passed here. The production
    composition never calls this, so ``web.exact.read`` stays a disabled
    (fail-closed) adapter and the verifier remains offline and credential-free.
    """

    from .web_reader import build_web_read_adapter as _build

    return _build(
        resolver=resolver,
        connect=connect,
        clock=clock,
        max_redirects=max_redirects,
    )


def lambda_handler(event: Any, _context: Any) -> dict[str, Any]:
    """Invoke the cold-start verified, durable, disabled-adapter composition."""

    if (
        not isinstance(event, Mapping)
        or set(event) != {"schema", "grant", "call"}
        or event.get("schema") != "personal-operator.capability-relay-envelope.v1"
    ):
        raise ValueError("capability relay envelope is invalid")
    try:
        call = CapabilityCallV1.from_mapping(event["call"])
    except (ContractValidationError, TypeError, ValueError) as error:
        raise ValueError("capability relay call is invalid") from error
    try:
        from .composition import get_production_composition

        composition = get_production_composition()
    except Exception:
        return _denied(call, "GATEWAY_CONFIGURATION_INVALID").to_mapping()
    return composition.invoke(event).to_mapping()


__all__ = [
    "AdapterOutcome",
    "CapabilityAdapter",
    "CapabilityGateway",
    "WEB_READ_OPERATION_ID",
    "build_web_read_adapter",
    "lambda_handler",
]
