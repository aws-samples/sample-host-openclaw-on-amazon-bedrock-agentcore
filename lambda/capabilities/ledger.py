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
    ) -> LedgerClaim: ...

    def complete(
        self, call: CapabilityCallV1, result: CapabilityResultV1
    ) -> None: ...


@dataclass(slots=True)
class _Entry:
    call: CapabilityCallV1
    state: str
    result: CapabilityResultV1 | None


@dataclass(slots=True)
class _TurnState:
    binding_hash: str
    call_ids: set[str]
    pack_call_ids: dict[str, set[str]]


class InMemoryCapabilityLedger:
    """A process-local atomic ledger for tests and fail-closed local prototypes.

    Production composition must inject a durable conditional-write implementation;
    the Lambda entry point intentionally has no default repository or ledger.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: dict[str, _Entry] = {}
        self._tool_uses: dict[tuple[str, str], tuple[str, str, str]] = {}
        self._turns: dict[str, _TurnState] = {}

    def begin(
        self,
        *,
        call: CapabilityCallV1,
        grant: TurnCapabilityGrantV1,
        pack_id: str,
        pack_max_calls: int,
        retry_mode: str,
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

        binding_hash = canonical_sha256(grant.to_mapping())
        tool_key = (call.invocation_id, call.tool_use_id)
        tool_identity = (call.operation_id, call.args_hash, call.call_id)
        with self._lock:
            turn = self._turns.get(grant.nonce)
            if turn is None:
                turn = _TurnState(
                    binding_hash=binding_hash,
                    call_ids=set(),
                    pack_call_ids={},
                )
                self._turns[grant.nonce] = turn
            elif turn.binding_hash != binding_hash:
                raise LedgerDenied("GRANT_NONCE_CONFLICT")

            known_tool = self._tool_uses.get(tool_key)
            if known_tool is not None and known_tool != tool_identity:
                raise LedgerDenied("CAPABILITY_ARGUMENT_MUTATION")

            entry = self._entries.get(call.call_id)
            if entry is not None:
                if entry.call.to_bytes() != call.to_bytes():
                    raise LedgerDenied("CAPABILITY_CALL_ID_CONFLICT")
                if entry.state == "IN_FLIGHT":
                    return LedgerClaim(LedgerDisposition.IN_FLIGHT)
                if (
                    entry.result is not None
                    and entry.result.status == "FAILED_RETRYABLE"
                    and retry_mode == "READ_ONLY"
                ):
                    entry.state = "IN_FLIGHT"
                    entry.result = None
                    return LedgerClaim(LedgerDisposition.RETRY)
                return LedgerClaim(LedgerDisposition.CACHED, entry.result)

            pack_calls = turn.pack_call_ids.setdefault(pack_id, set())
            if (
                len(turn.call_ids) >= grant.max_calls
                or len(pack_calls) >= pack_max_calls
            ):
                raise LedgerDenied("CAPABILITY_CALL_BUDGET_EXCEEDED")

            turn.call_ids.add(call.call_id)
            pack_calls.add(call.call_id)
            self._tool_uses[tool_key] = tool_identity
            self._entries[call.call_id] = _Entry(
                call=call,
                state="IN_FLIGHT",
                result=None,
            )
            return LedgerClaim(LedgerDisposition.NEW)

    def complete(self, call: CapabilityCallV1, result: CapabilityResultV1) -> None:
        if not isinstance(call, CapabilityCallV1) or not isinstance(
            result, CapabilityResultV1
        ):
            raise TypeError("ledger completion requires validated contracts")
        result.validate_against_call(call)
        with self._lock:
            entry = self._entries.get(call.call_id)
            if entry is None or entry.call.to_bytes() != call.to_bytes():
                raise LedgerDenied("CAPABILITY_CALL_NOT_CLAIMED")
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
]
