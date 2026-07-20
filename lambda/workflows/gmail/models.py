"""Bounded records that may cross the trusted Gmail workflow boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re


_SOURCE_ID = re.compile(r"^gmail:[A-Za-z0-9_-]{1,128}:[A-Za-z0-9_-]{1,128}$")
_ACTION_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def _bounded(value: str, label: str, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    value = value.strip()
    if "\x00" in value or (not allow_empty and not value) or len(value) > limit:
        raise ValueError(f"{label} must contain at most {limit} characters")
    return value


def _exact_body(value: str, *, byte_limit: int = 20_000) -> str:
    """Validate a draft body without normalizing a single character."""

    if not isinstance(value, str):
        raise TypeError("body must be text")
    if not value or "\x00" in value:
        raise ValueError("body must be non-empty text without NUL bytes")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise ValueError("body must be valid UTF-8 text") from error
    if len(encoded) > byte_limit:
        raise ValueError(f"body must contain at most {byte_limit} UTF-8 bytes")
    return value


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    """A bounded, derived Gmail fact; never a raw message body."""

    source_id: str
    thread_id: str
    deep_link: str
    correspondent: str
    subject: str
    excerpt: str
    waiting_since: datetime

    def __post_init__(self) -> None:
        source_id = _bounded(self.source_id, "source_id", 270)
        if not _SOURCE_ID.fullmatch(source_id):
            raise ValueError("source_id must bind one Gmail thread and message")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "thread_id", _bounded(self.thread_id, "thread_id", 128))
        deep_link = _bounded(self.deep_link, "deep_link", 512)
        expected = f"https://mail.google.com/mail/u/0/#inbox/{self.thread_id}"
        if deep_link != expected:
            raise ValueError("deep_link must be a canonical Gmail thread link")
        object.__setattr__(self, "deep_link", deep_link)
        object.__setattr__(self, "correspondent", _bounded(self.correspondent, "correspondent", 320))
        object.__setattr__(self, "subject", _bounded(self.subject, "subject", 200, allow_empty=True))
        object.__setattr__(self, "excerpt", _bounded(self.excerpt, "excerpt", 280, allow_empty=True))
        object.__setattr__(self, "waiting_since", _utc(self.waiting_since, "waiting_since"))

    def prompt_record(self) -> dict[str, object]:
        return {
            "sourceId": self.source_id,
            "correspondent": self.correspondent,
            "subject": self.subject,
            "excerpt": self.excerpt,
            "waitingSince": self.waiting_since.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class Opportunity:
    id: str
    user_id: str
    source: SourceEvidence
    waiting_since: datetime
    title: str
    reason: str
    confidence: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _bounded(self.id, "id", 128))
        object.__setattr__(self, "user_id", _bounded(self.user_id, "user_id", 128))
        if not isinstance(self.source, SourceEvidence):
            raise TypeError("source must be SourceEvidence")
        waiting_since = _utc(self.waiting_since, "waiting_since")
        if waiting_since != self.source.waiting_since:
            raise ValueError("waiting_since must be derived from the bound source")
        object.__setattr__(self, "waiting_since", waiting_since)
        object.__setattr__(self, "title", _bounded(self.title, "title", 120))
        object.__setattr__(self, "reason", _bounded(self.reason, "reason", 280))
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise TypeError("confidence must be numeric")
        confidence = float(self.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between zero and one")
        object.__setattr__(self, "confidence", confidence)


def opportunity_id(user_id: str, source_id: str) -> str:
    digest = hashlib.sha256(f"{user_id}\0{source_id}".encode()).hexdigest()[:32]
    return f"opp_{digest}"


@dataclass(frozen=True, slots=True)
class DraftRevision:
    action_id: str
    revision: int
    to: str
    subject: str
    body: str
    payload_hash: str

    def __post_init__(self) -> None:
        action_id = _bounded(self.action_id, "action_id", 128)
        if not _ACTION_ID.fullmatch(action_id):
            raise ValueError("action_id has an invalid format")
        object.__setattr__(self, "action_id", action_id)
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        object.__setattr__(self, "to", _bounded(self.to, "to", 320))
        object.__setattr__(self, "subject", _bounded(self.subject, "subject", 200))
        object.__setattr__(self, "body", _exact_body(self.body))
        if not re.fullmatch(r"[0-9a-f]{64}", self.payload_hash or ""):
            raise ValueError("payload_hash must be lowercase SHA-256")
        if self.payload_hash != self.compute_payload_hash(
            to=self.to,
            subject=self.subject,
            body=self.body,
        ):
            raise ValueError("payload_hash does not match the exact draft payload")

    @staticmethod
    def compute_payload_hash(*, to: str, subject: str, body: str) -> str:
        canonical = json.dumps(
            {"body": body, "subject": subject, "to": to},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(canonical).hexdigest()

    @classmethod
    def create(
        cls,
        *,
        action_id: str,
        revision: int,
        to: str,
        subject: str,
        body: str,
    ) -> "DraftRevision":
        normalized_to = _bounded(to, "to", 320)
        normalized_subject = _bounded(subject, "subject", 200)
        exact_body = _exact_body(body)
        return cls(
            action_id=action_id,
            revision=revision,
            to=normalized_to,
            subject=normalized_subject,
            body=exact_body,
            payload_hash=cls.compute_payload_hash(
                to=normalized_to,
                subject=normalized_subject,
                body=exact_body,
            ),
        )
