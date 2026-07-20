"""Universal durable-attempt identity and one-shot dispatch capability.

The filesystem persistence belongs to ``ReleaseEvidenceStoreV2``.  This module
keeps the retained record exact and makes the in-memory authority private,
single-use, and bound to one provider operation and resolved request.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, ClassVar, Mapping

from release_tools.contracts import (
    ContractError,
    canonical_json_bytes,
    parse_canonical_object,
)


_SHA256 = re.compile(r"[0-9a-f]{64}")
_RELEASE_OPERATION = re.compile(r"sha256:[0-9a-f]{64}")
_PROVIDERS = frozenset(
    {
        "AGENTCORE",
        "CLOUDFORMATION",
        "ECR",
        "LOCAL_FILESYSTEM",
        "S3",
    }
)
_AUTHORITY_TOKEN = object()


class DispatchAttemptError(RuntimeError):
    """A dispatch marker or its one-shot authority is invalid."""


def _exact_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DispatchAttemptError(f"{label} is invalid")
    return value


def _identity_text(value: object, *, label: str, maximum: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or "\x00" in value
    ):
        raise DispatchAttemptError(f"{label} is invalid")
    if label == "dispatch subject" and (
        value in {".", ".."}
        or value.startswith("../")
        or value.endswith("/..")
        or "/../" in value
    ):
        raise DispatchAttemptError(f"{label} is invalid")
    return value


def _release_operation(value: object) -> str:
    if not isinstance(value, str) or _RELEASE_OPERATION.fullmatch(value) is None:
        raise DispatchAttemptError("dispatch operation digest is invalid")
    return value


@dataclass(frozen=True, slots=True)
class ReleaseDispatchAttemptV1:
    """Canonical durable marker for one exact current mutation operation."""

    SCHEMA: ClassVar[str] = "personal-operator.release-dispatch-attempt.v1"
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema",
            "releasePlanSha256",
            "evidenceStoreSha256",
            "journalPathSha256",
            "journalExecutionId",
            "journalRevision",
            "completedPrefixSha256",
            "stepId",
            "subject",
            "operationSha256",
            "resolvedRequestSha256",
            "provider",
        }
    )

    release_plan_sha256: str
    evidence_store_sha256: str
    journal_path_sha256: str
    journal_execution_id: str
    journal_revision: int
    completed_prefix_sha256: str
    step_id: str
    subject: str
    operation_sha256: str
    resolved_request_sha256: str
    provider: str

    def __post_init__(self) -> None:
        _exact_sha256(self.release_plan_sha256, label="release plan digest")
        _exact_sha256(self.evidence_store_sha256, label="evidence store digest")
        _exact_sha256(self.journal_path_sha256, label="journal path digest")
        _exact_sha256(self.journal_execution_id, label="journal execution identity")
        if (
            isinstance(self.journal_revision, bool)
            or not isinstance(self.journal_revision, int)
            or self.journal_revision < 1
        ):
            raise DispatchAttemptError("journal revision is invalid")
        _exact_sha256(self.completed_prefix_sha256, label="completed prefix digest")
        _identity_text(self.step_id, label="dispatch step ID")
        _identity_text(self.subject, label="dispatch subject")
        _release_operation(self.operation_sha256)
        _exact_sha256(
            self.resolved_request_sha256, label="resolved request digest"
        )
        if self.provider not in _PROVIDERS:
            raise DispatchAttemptError("dispatch provider is invalid")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ReleaseDispatchAttemptV1":
        if not isinstance(raw, Mapping) or set(raw) != cls.FIELDS:
            raise DispatchAttemptError("dispatch attempt fields are invalid")
        if raw.get("schema") != cls.SCHEMA:
            raise DispatchAttemptError("dispatch attempt schema is invalid")
        return cls(
            release_plan_sha256=_exact_sha256(
                raw.get("releasePlanSha256"), label="release plan digest"
            ),
            evidence_store_sha256=_exact_sha256(
                raw.get("evidenceStoreSha256"), label="evidence store digest"
            ),
            journal_path_sha256=_exact_sha256(
                raw.get("journalPathSha256"), label="journal path digest"
            ),
            journal_execution_id=_exact_sha256(
                raw.get("journalExecutionId"),
                label="journal execution identity",
            ),
            journal_revision=raw.get("journalRevision"),
            completed_prefix_sha256=_exact_sha256(
                raw.get("completedPrefixSha256"),
                label="completed prefix digest",
            ),
            step_id=_identity_text(raw.get("stepId"), label="dispatch step ID"),
            subject=_identity_text(raw.get("subject"), label="dispatch subject"),
            operation_sha256=_release_operation(raw.get("operationSha256")),
            resolved_request_sha256=_exact_sha256(
                raw.get("resolvedRequestSha256"),
                label="resolved request digest",
            ),
            provider=(
                raw["provider"]
                if isinstance(raw.get("provider"), str)
                else ""
            ),
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ReleaseDispatchAttemptV1":
        try:
            raw = parse_canonical_object(payload)
            value = cls.from_mapping(raw)
        except DispatchAttemptError:
            raise
        except (ContractError, TypeError, ValueError) as error:
            raise DispatchAttemptError(
                "dispatch attempt is not canonical"
            ) from error
        if value.to_bytes() != payload:
            raise DispatchAttemptError("dispatch attempt is not canonical")
        return value

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "releasePlanSha256": self.release_plan_sha256,
            "evidenceStoreSha256": self.evidence_store_sha256,
            "journalPathSha256": self.journal_path_sha256,
            "journalExecutionId": self.journal_execution_id,
            "journalRevision": self.journal_revision,
            "completedPrefixSha256": self.completed_prefix_sha256,
            "stepId": self.step_id,
            "subject": self.subject,
            "operationSha256": self.operation_sha256,
            "resolvedRequestSha256": self.resolved_request_sha256,
            "provider": self.provider,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


class FreshDispatchAuthorityV1:
    """One-shot in-memory authority minted only for a newly retained marker."""

    __slots__ = ("_attempt", "_consumed")

    def __init__(
        self, attempt: ReleaseDispatchAttemptV1, *, _token: object | None = None
    ) -> None:
        if _token is not _AUTHORITY_TOKEN:
            raise DispatchAttemptError(
                "fresh dispatch authority is not constructible"
            )
        if not isinstance(attempt, ReleaseDispatchAttemptV1):
            raise DispatchAttemptError("fresh dispatch attempt is invalid")
        self._attempt = ReleaseDispatchAttemptV1.from_bytes(attempt.to_bytes())
        self._consumed = False

    def consume(
        self,
        *,
        provider: str,
        operation_sha256: str,
        resolved_request_sha256: str,
    ) -> ReleaseDispatchAttemptV1:
        if self._consumed:
            raise DispatchAttemptError("fresh dispatch authority is already consumed")
        if (
            provider != self._attempt.provider
            or operation_sha256 != self._attempt.operation_sha256
            or resolved_request_sha256
            != self._attempt.resolved_request_sha256
        ):
            raise DispatchAttemptError("fresh dispatch authority binding differs")
        self._consumed = True
        return self._attempt


def _mint_fresh_dispatch_authority(
    attempt: ReleaseDispatchAttemptV1,
) -> FreshDispatchAuthorityV1:
    """Package-private constructor used only after an O_EXCL store append."""

    return FreshDispatchAuthorityV1(attempt, _token=_AUTHORITY_TOKEN)


@dataclass(frozen=True, slots=True)
class DispatchAttemptStateV1:
    """Closed read result: no marker, or one exact retained marker."""

    attempted: bool
    attempt: ReleaseDispatchAttemptV1 | None

    def __post_init__(self) -> None:
        if not isinstance(self.attempted, bool):
            raise DispatchAttemptError("dispatch attempt state is invalid")
        if self.attempted != (self.attempt is not None):
            raise DispatchAttemptError("dispatch attempt state is invalid")
        if self.attempt is not None:
            if not isinstance(self.attempt, ReleaseDispatchAttemptV1):
                raise DispatchAttemptError("dispatch attempt state is invalid")
            canonical = ReleaseDispatchAttemptV1.from_bytes(
                self.attempt.to_bytes()
            )
            if canonical != self.attempt:
                raise DispatchAttemptError("dispatch attempt state is invalid")

    @classmethod
    def absent(cls) -> "DispatchAttemptStateV1":
        return cls(attempted=False, attempt=None)

    @classmethod
    def retained(
        cls, attempt: ReleaseDispatchAttemptV1
    ) -> "DispatchAttemptStateV1":
        return cls(attempted=True, attempt=attempt)


__all__ = [
    "DispatchAttemptError",
    "DispatchAttemptStateV1",
    "FreshDispatchAuthorityV1",
    "ReleaseDispatchAttemptV1",
]
