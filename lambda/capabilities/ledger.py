"""Atomic replay and per-turn budget ledger interfaces for capability calls."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import Protocol

from .contracts import (
    CapabilityCallV1,
    CapabilityResultV1,
    TurnCapabilityGrantV1,
    canonical_sha256,
)


class LedgerDenied(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LedgerDisposition(str, Enum):
    NEW = "NEW"
    RETRY = "RETRY"
    CACHED = "CACHED"
    IN_FLIGHT = "IN_FLIGHT"
    LOGICAL_FENCE = "LOGICAL_FENCE"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"


@dataclass(frozen=True, slots=True)
class LedgerClaim:
    disposition: LedgerDisposition
    result: CapabilityResultV1 | None = None


class CapabilityLedger(Protocol):
    def begin(
        self,
        *,
        call: CapabilityCallV1,
        grant: TurnCapabilityGrantV1,
        pack_id: str,
        pack_max_calls: int,
        retry_mode: str,
        retention_max_days: int = 0,
    ) -> LedgerClaim: ...

    def complete(
        self,
        *,
        call: CapabilityCallV1,
        grant: TurnCapabilityGrantV1,
        result: CapabilityResultV1,
    ) -> None: ...


@dataclass(slots=True)
class _Entry:
    call: CapabilityCallV1
    tenant_binding: str
    grant_binding: str
    logical_key: str
    attempts: int
    state: str
    result: CapabilityResultV1 | None


@dataclass(slots=True)
class _TurnState:
    grant_binding: str
    call_ids: set[str]
    pack_call_ids: dict[str, set[str]]


def derive_tenant_binding(grant: TurnCapabilityGrantV1) -> str:
    """Bind replay state to one exact authenticated release/runtime tenant."""

    if not isinstance(grant, TurnCapabilityGrantV1):
        raise TypeError("tenant binding requires a validated turn grant")
    return canonical_sha256(
        {
            "sub": grant.sub,
            "sessionId": grant.session_id,
            "runtimeArn": grant.runtime_arn,
            "runtimeQualifier": grant.runtime_qualifier,
            "releaseCommit": grant.release_commit,
            "catalogDigest": grant.catalog_digest,
        }
    )


def derive_logical_call_key(
    call: CapabilityCallV1,
    tenant_binding: str,
) -> str:
    """Return the stable effect identity independent of a fresh tool-use ID."""

    if not isinstance(call, CapabilityCallV1):
        raise TypeError("logical call key requires a validated call")
    if not isinstance(tenant_binding, str) or len(tenant_binding) != 64:
        raise TypeError("logical call key requires an exact tenant binding")
    return canonical_sha256(
        {
            "tenantBinding": tenant_binding,
            "invocationId": call.invocation_id,
            "catalogDigest": call.catalog_digest,
            "operationId": call.operation_id,
            "toolName": call.tool_name,
            "argsHash": call.args_hash,
        }
    )


class InMemoryCapabilityLedger:
    """A process-local atomic ledger for tests and fail-closed local prototypes.

    Production composition must inject a durable conditional-write implementation;
    the Lambda entry point intentionally has no default repository or ledger.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._tool_uses: dict[tuple[str, str, str], tuple[str, str, str]] = {}
        self._turns: dict[tuple[str, str], _TurnState] = {}
        self._logical_calls: dict[tuple[str, str], str] = {}

    def begin(
        self,
        *,
        call: CapabilityCallV1,
        grant: TurnCapabilityGrantV1,
        pack_id: str,
        pack_max_calls: int,
        retry_mode: str,
        retention_max_days: int = 0,
    ) -> LedgerClaim:
        if not isinstance(call, CapabilityCallV1) or not isinstance(
            grant, TurnCapabilityGrantV1
        ):
            raise TypeError("ledger requires validated call and grant contracts")
        if not isinstance(pack_id, str) or not pack_id:
            raise TypeError("ledger requires an exact pack ID")
        if (
            isinstance(pack_max_calls, bool)
            or not isinstance(pack_max_calls, int)
            or pack_max_calls < 1
        ):
            raise TypeError("ledger pack budget is invalid")
        if retry_mode not in {"READ_ONLY", "IDEMPOTENT", "DEDUPE_KEY_REQUIRED"}:
            raise TypeError("ledger retry mode is invalid")
        if (
            isinstance(retention_max_days, bool)
            or not isinstance(retention_max_days, int)
            or not 0 <= retention_max_days <= 365
        ):
            raise TypeError("ledger retention policy is invalid")

        tenant_binding = derive_tenant_binding(grant)
        grant_binding = canonical_sha256(grant.to_mapping())
        logical_key = derive_logical_call_key(call, tenant_binding)
        turn_key = (tenant_binding, call.invocation_id)
        entry_key = (tenant_binding, call.call_id)
        tool_key = (tenant_binding, call.invocation_id, call.tool_use_id)
        tool_identity = (call.operation_id, call.args_hash, call.call_id)
        with self._lock:
            turn = self._turns.get(turn_key)
            if turn is None:
                turn = _TurnState(
                    grant_binding=grant_binding,
                    call_ids=set(),
                    pack_call_ids={},
                )
                self._turns[turn_key] = turn
            elif turn.grant_binding != grant_binding:
                raise LedgerDenied("CAPABILITY_GRANT_BINDING_MISMATCH")

            known_tool = self._tool_uses.get(tool_key)
            if known_tool is not None and known_tool != tool_identity:
                raise LedgerDenied("CAPABILITY_ARGUMENT_MUTATION")

            entry = self._entries.get(entry_key)
            if entry is not None:
                if (
                    entry.tenant_binding != tenant_binding
                    or entry.grant_binding != grant_binding
                ):
                    raise LedgerDenied("CAPABILITY_GRANT_BINDING_MISMATCH")
                if entry.call.to_bytes() != call.to_bytes():
                    raise LedgerDenied("CAPABILITY_CALL_ID_CONFLICT")
                if entry.state == "IN_FLIGHT":
                    return LedgerClaim(LedgerDisposition.IN_FLIGHT)
                if (
                    entry.result is not None
                    and entry.result.status == "FAILED_RETRYABLE"
                    and retry_mode == "READ_ONLY"
                ):
                    if entry.attempts >= 2:
                        return LedgerClaim(LedgerDisposition.RETRY_EXHAUSTED)
                    entry.attempts += 1
                    entry.state = "IN_FLIGHT"
                    entry.result = None
                    return LedgerClaim(LedgerDisposition.RETRY)
                return LedgerClaim(LedgerDisposition.CACHED, entry.result)

            prior_call_id = self._logical_calls.get((tenant_binding, logical_key))
            if prior_call_id is not None and prior_call_id != call.call_id:
                if retry_mode == "READ_ONLY":
                    prior_entry = self._entries.get((tenant_binding, prior_call_id))
                    if prior_entry is None or (
                        prior_entry.state == "IN_FLIGHT"
                        or (
                            prior_entry.result is not None
                            and prior_entry.result.status == "FAILED_RETRYABLE"
                        )
                    ):
                        raise LedgerDenied("CAPABILITY_READ_RETRY_REQUIRES_SAME_CALL")
                else:
                    return LedgerClaim(LedgerDisposition.LOGICAL_FENCE)

            pack_calls = turn.pack_call_ids.setdefault(pack_id, set())
            if (
                len(turn.call_ids) >= grant.max_calls
                or len(pack_calls) >= pack_max_calls
            ):
                raise LedgerDenied("CAPABILITY_CALL_BUDGET_EXCEEDED")

            turn.call_ids.add(call.call_id)
            pack_calls.add(call.call_id)
            self._tool_uses[tool_key] = tool_identity
            self._logical_calls[(tenant_binding, logical_key)] = call.call_id
            self._entries[entry_key] = _Entry(
                call=call,
                tenant_binding=tenant_binding,
                grant_binding=grant_binding,
                logical_key=logical_key,
                attempts=1,
                state="IN_FLIGHT",
                result=None,
            )
            return LedgerClaim(LedgerDisposition.NEW)

    def complete(
        self,
        *,
        call: CapabilityCallV1,
        grant: TurnCapabilityGrantV1,
        result: CapabilityResultV1,
    ) -> None:
        if (
            not isinstance(call, CapabilityCallV1)
            or not isinstance(result, CapabilityResultV1)
            or not isinstance(grant, TurnCapabilityGrantV1)
        ):
            raise TypeError("ledger completion requires validated contracts")
        result.validate_against_call(call)
        tenant_binding = derive_tenant_binding(grant)
        grant_binding = canonical_sha256(grant.to_mapping())
        with self._lock:
            entry = self._entries.get((tenant_binding, call.call_id))
            if entry is None or entry.call.to_bytes() != call.to_bytes():
                raise LedgerDenied("CAPABILITY_CALL_NOT_CLAIMED")
            if (
                entry.tenant_binding != tenant_binding
                or entry.grant_binding != grant_binding
            ):
                raise LedgerDenied("CAPABILITY_GRANT_BINDING_MISMATCH")
            if entry.state != "IN_FLIGHT":
                if (
                    entry.result is not None
                    and entry.result.to_bytes() == result.to_bytes()
                ):
                    return
                raise LedgerDenied("CAPABILITY_RESULT_CONFLICT")
            entry.state = "COMPLETE"
            entry.result = result


__all__ = [
    "CapabilityLedger",
    "InMemoryCapabilityLedger",
    "LedgerClaim",
    "LedgerDenied",
    "LedgerDisposition",
    "derive_logical_call_key",
    "derive_tenant_binding",
]
