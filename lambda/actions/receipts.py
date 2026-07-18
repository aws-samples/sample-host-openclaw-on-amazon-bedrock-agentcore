"""Connector-generic effect-evidence contract.

``EffectReceiptV1`` generalizes the capability-specific
``actions.models.EffectReceipt`` into the connector-generic evidence a
dispatched effect must prove: capability / resource / connection / a provider
effect identity / payload hash / execution time / evidence labels, plus the
``record()`` / ``from_record()`` / ``from_provider_evidence()`` round-trip.

CRITICAL: this is a NON-invasive Protocol. ``EffectReceipt.record()`` and
``from_record()`` key sets are validated byte-for-byte by the repository on the
CONFIRMED transition, so this contract must be satisfied by the *existing*
``EffectReceipt`` without renaming ``providerMessageId``/``providerThreadId``/
``messageId``/``labels``/etc. The connector-generic accessors
(``capability``/``connection_ref``/``provider_effect_id``/``evidence_labels``)
are exposed as computed views over the frozen fields.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, Protocol, Sequence, runtime_checkable


@runtime_checkable
class EffectReceiptV1(Protocol):
    """The connector-generic evidence contract a dispatched effect proves.

    Structurally satisfied by ``actions.models.EffectReceipt``. Do NOT redefine
    the persisted ``record()`` key set here.
    """

    payload_hash: str
    executed_at: datetime

    @property
    def capability(self) -> str: ...

    @property
    def resource(self) -> str: ...

    @property
    def connection_ref(self) -> str: ...

    @property
    def provider_effect_id(self) -> str: ...

    @property
    def evidence_labels(self) -> Sequence[str]: ...

    def record(self) -> Mapping[str, object]: ...

    @classmethod
    def from_record(cls, record: object) -> "EffectReceiptV1": ...

    @classmethod
    def from_provider_evidence(cls, evidence: object) -> "EffectReceiptV1": ...
