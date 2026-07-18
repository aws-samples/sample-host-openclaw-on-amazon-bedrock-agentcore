"""Canonical capability, draft, provider-evidence, and effect records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import hmac
import json
import re
from types import MappingProxyType
from typing import Mapping, Sequence


class ActionState(str, Enum):
    PREPARED = "PREPARED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVED = "APPROVED"
    DISPATCHING = "DISPATCHING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    UNCERTAIN = "UNCERTAIN"
    EXPIRED = "EXPIRED"
    STALE = "STALE"
    CANCELLED = "CANCELLED"


class CapabilityDenied(PermissionError):
    pass


_ACTION_ID = re.compile(r"[A-Za-z0-9_-]{8,128}")
_CONNECTION_ID = re.compile(r"[A-Za-z0-9_-]{8,128}")
_EMAIL = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
)
_MESSAGE_ID = re.compile(r"<po-[0-9a-f]{24}@personal-operator\.invalid>")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PROVIDER_LABEL = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("capability arguments must be canonical JSON") from error


def canonical_args_hash(args: Mapping[str, object]) -> str:
    if not isinstance(args, Mapping):
        raise TypeError("capability arguments must be a mapping")
    return hashlib.sha256(_canonical_json(dict(args))).hexdigest()


def _bounded(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{label} is invalid")
    return value


def _match(value: str, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _positive_revision(value: int, label: str = "draft_revision") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} is invalid")
    return value


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parsed_utc(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError as error:
        raise ValueError(f"{label} is invalid") from error
    return _utc(parsed, label)


def gmail_resource(*, connection_id: str, account_email: str) -> str:
    connection_id = _match(connection_id, "connection_id", _CONNECTION_ID)
    account_email = _match(account_email, "account_email", _EMAIL)
    return f"google:gmail:connection:{connection_id}:account:{account_email}"


@dataclass(frozen=True, slots=True)
class DraftRevision:
    """Immutable typed boundary between an editable draft and PREPARED action."""

    action_id: str
    user_id: str
    draft_revision: int
    connection_id: str
    account_email: str
    sender_address: str
    args: Mapping[str, object]
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", _match(self.action_id, "action_id", _ACTION_ID))
        object.__setattr__(self, "user_id", _bounded(self.user_id, "user_id", 128))
        object.__setattr__(
            self, "draft_revision", _positive_revision(self.draft_revision)
        )
        object.__setattr__(
            self,
            "connection_id",
            _match(self.connection_id, "connection_id", _CONNECTION_ID),
        )
        object.__setattr__(
            self, "account_email", _match(self.account_email, "account_email", _EMAIL)
        )
        object.__setattr__(
            self, "sender_address", _match(self.sender_address, "sender_address", _EMAIL)
        )
        if self.sender_address != self.account_email:
            raise ValueError("v0 sender_address must equal the bound Google account")
        if not isinstance(self.args, Mapping):
            raise TypeError("draft args must be a mapping")
        exact_args = dict(self.args)
        _canonical_json(exact_args)
        if set(exact_args) != {"to", "subject", "body"}:
            raise ValueError("Gmail draft accepts only to, subject, and body")
        recipient = exact_args["to"]
        subject = exact_args["subject"]
        body = exact_args["body"]
        if not isinstance(recipient, str) or _EMAIL.fullmatch(recipient) is None:
            raise ValueError("draft recipient is invalid")
        if (
            not isinstance(subject, str)
            or not subject
            or len(subject) > 200
            or "\r" in subject
            or "\n" in subject
        ):
            raise ValueError("draft subject is invalid")
        if not isinstance(body, str) or not body or len(body) > 20_000 or "\x00" in body:
            raise ValueError("draft body is invalid")
        object.__setattr__(self, "args", MappingProxyType(exact_args))
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))

    @property
    def resource(self) -> str:
        return gmail_resource(
            connection_id=self.connection_id, account_email=self.account_email
        )

    @property
    def payload_hash(self) -> str:
        return canonical_args_hash(self.args)


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    action_id: str
    draft_revision: int
    user_id: str
    capability: str
    resource: str
    args_hash: str
    expires_at: datetime
    approval_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", _match(self.action_id, "action_id", _ACTION_ID))
        object.__setattr__(
            self, "draft_revision", _positive_revision(self.draft_revision)
        )
        object.__setattr__(self, "user_id", _bounded(self.user_id, "user_id", 128))
        object.__setattr__(self, "capability", _bounded(self.capability, "capability", 128))
        object.__setattr__(self, "resource", _bounded(self.resource, "resource", 512))
        object.__setattr__(self, "args_hash", _match(self.args_hash, "args_hash", _SHA256))
        object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))
        approval_id = _bounded(self.approval_id, "approval_id", 128)
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", approval_id):
            raise ValueError("approval_id has an invalid format")
        object.__setattr__(self, "approval_id", approval_id)

    def assert_authorized(
        self,
        *,
        action_id: str,
        draft_revision: int,
        user_id: str,
        capability: str,
        resource: str,
        args: Mapping[str, object],
        now: datetime,
    ) -> "CapabilityGrant":
        actual = (
            action_id,
            draft_revision,
            user_id,
            capability,
            resource,
            canonical_args_hash(args),
        )
        expected = (
            self.action_id,
            self.draft_revision,
            self.user_id,
            self.capability,
            self.resource,
            self.args_hash,
        )
        if any(
            not hmac.compare_digest(str(left), str(right))
            for left, right in zip(actual, expected, strict=True)
        ):
            raise CapabilityDenied("capability grant does not match this exact action revision")
        if _utc(now, "now") >= self.expires_at:
            raise CapabilityDenied("capability grant expired")
        return self

    def token_record(self) -> dict[str, object]:
        return {
            "actionId": self.action_id,
            "approvalId": self.approval_id,
            "argsHash": self.args_hash,
            "capability": self.capability,
            "draftRevision": self.draft_revision,
            "expiresAt": self.expires_at.isoformat(),
            "resource": self.resource,
            "userId": self.user_id,
        }


def _labels(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not 1 <= len(value) <= 32
        or any(not isinstance(label, str) or _PROVIDER_LABEL.fullmatch(label) is None for label in value)
    ):
        raise ValueError("provider labels are invalid")
    result = tuple(sorted(value))
    if len(set(result)) != len(result) or "SENT" not in result:
        raise ValueError("provider evidence does not prove the SENT label")
    return result


@dataclass(frozen=True, slots=True)
class EffectReceipt:
    provider_message_id: str
    provider_thread_id: str
    message_id: str
    connection_id: str
    account_email: str
    sender_address: str
    recipient: str
    payload_hash: str
    executed_at: datetime
    labels: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_message_id", _bounded(self.provider_message_id, "provider_message_id", 512)
        )
        object.__setattr__(
            self, "provider_thread_id", _bounded(self.provider_thread_id, "provider_thread_id", 512)
        )
        object.__setattr__(self, "message_id", _match(self.message_id, "message_id", _MESSAGE_ID))
        object.__setattr__(
            self, "connection_id", _match(self.connection_id, "connection_id", _CONNECTION_ID)
        )
        object.__setattr__(self, "account_email", _match(self.account_email, "account_email", _EMAIL))
        object.__setattr__(self, "sender_address", _match(self.sender_address, "sender_address", _EMAIL))
        if self.sender_address != self.account_email:
            raise ValueError("effect sender does not match the bound Google account")
        object.__setattr__(self, "recipient", _match(self.recipient, "recipient", _EMAIL))
        object.__setattr__(self, "payload_hash", _match(self.payload_hash, "payload_hash", _SHA256))
        object.__setattr__(self, "executed_at", _utc(self.executed_at, "executed_at"))
        object.__setattr__(self, "labels", _labels(self.labels))

    @classmethod
    def from_provider_evidence(cls, evidence: object) -> "EffectReceipt":
        required = {
            "id",
            "threadId",
            "messageId",
            "connectionId",
            "accountEmail",
            "senderAddress",
            "recipient",
            "payloadHash",
            "executedAt",
            "labels",
        }
        if not isinstance(evidence, Mapping) or set(evidence) != required:
            raise ValueError("provider evidence schema is invalid")
        return cls(
            provider_message_id=evidence["id"],
            provider_thread_id=evidence["threadId"],
            message_id=evidence["messageId"],
            connection_id=evidence["connectionId"],
            account_email=evidence["accountEmail"],
            sender_address=evidence["senderAddress"],
            recipient=evidence["recipient"],
            payload_hash=evidence["payloadHash"],
            executed_at=_parsed_utc(evidence["executedAt"], "executedAt"),
            labels=tuple(evidence["labels"]),
        )

    @classmethod
    def from_record(cls, record: object) -> "EffectReceipt":
        required = {
            "providerMessageId",
            "providerThreadId",
            "messageId",
            "connectionId",
            "accountEmail",
            "senderAddress",
            "recipient",
            "payloadHash",
            "executedAt",
            "labels",
        }
        if not isinstance(record, Mapping) or set(record) != required:
            raise ValueError("effect receipt schema is invalid")
        return cls(
            provider_message_id=record["providerMessageId"],
            provider_thread_id=record["providerThreadId"],
            message_id=record["messageId"],
            connection_id=record["connectionId"],
            account_email=record["accountEmail"],
            sender_address=record["senderAddress"],
            recipient=record["recipient"],
            payload_hash=record["payloadHash"],
            executed_at=_parsed_utc(record["executedAt"], "executedAt"),
            labels=tuple(record["labels"]),
        )

    def record(self) -> dict[str, object]:
        return {
            "providerMessageId": self.provider_message_id,
            "providerThreadId": self.provider_thread_id,
            "messageId": self.message_id,
            "connectionId": self.connection_id,
            "accountEmail": self.account_email,
            "senderAddress": self.sender_address,
            "recipient": self.recipient,
            "payloadHash": self.payload_hash,
            "executedAt": self.executed_at.isoformat(),
            "labels": list(self.labels),
        }


@dataclass(frozen=True, slots=True)
class WaitingForReply:
    action_id: str
    draft_revision: int
    connection_id: str
    account_email: str
    recipient: str
    message_id: str
    provider_thread_id: str
    since: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", _match(self.action_id, "action_id", _ACTION_ID))
        object.__setattr__(self, "draft_revision", _positive_revision(self.draft_revision))
        object.__setattr__(self, "connection_id", _match(self.connection_id, "connection_id", _CONNECTION_ID))
        object.__setattr__(self, "account_email", _match(self.account_email, "account_email", _EMAIL))
        object.__setattr__(self, "recipient", _match(self.recipient, "recipient", _EMAIL))
        object.__setattr__(self, "message_id", _match(self.message_id, "message_id", _MESSAGE_ID))
        object.__setattr__(self, "provider_thread_id", _bounded(self.provider_thread_id, "provider_thread_id", 512))
        object.__setattr__(self, "since", _utc(self.since, "since"))

    @classmethod
    def from_record(cls, record: object) -> "WaitingForReply":
        required = {
            "actionId",
            "draftRevision",
            "connectionId",
            "accountEmail",
            "recipient",
            "messageId",
            "providerThreadId",
            "since",
        }
        if not isinstance(record, Mapping) or set(record) != required:
            raise ValueError("waiting-for-reply tracker schema is invalid")
        return cls(
            action_id=record["actionId"],
            draft_revision=record["draftRevision"],
            connection_id=record["connectionId"],
            account_email=record["accountEmail"],
            recipient=record["recipient"],
            message_id=record["messageId"],
            provider_thread_id=record["providerThreadId"],
            since=_parsed_utc(record["since"], "since"),
        )

    def record(self) -> dict[str, object]:
        return {
            "actionId": self.action_id,
            "draftRevision": self.draft_revision,
            "connectionId": self.connection_id,
            "accountEmail": self.account_email,
            "recipient": self.recipient,
            "messageId": self.message_id,
            "providerThreadId": self.provider_thread_id,
            "since": self.since.isoformat(),
        }
