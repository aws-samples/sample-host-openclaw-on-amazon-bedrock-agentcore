"""Typed, fail-closed SQS FIFO messages for the Telegram product worker."""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from typing import Any, Mapping

try:
    from .event_identity import assert_event_trace
    from .product_commands import parse_product_command
except ImportError:  # direct Lambda asset and focused tests
    from event_identity import assert_event_trace
    from product_commands import parse_product_command


MAX_ENVELOPE_BYTES = 128 * 1024
MAX_MESSAGE_TEXT_CHARS = 16_384
MAX_JSON_DEPTH = 8
MAX_JSON_COLLECTION_ITEMS = 64

_WIRE_KEYS = ("userId", "channel", "updateId", "traceId", "kind", "payload")
_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_UPDATE_ID = re.compile(r"[0-9]{1,20}")
_CHAT_ID = re.compile(r"-?[0-9]{1,20}")
_ACTOR_ID = re.compile(r"telegram:[0-9]{1,20}")
_S3_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_/-]{1,1023}(?:\.[A-Za-z0-9]{1,16})?")
_CONTENT_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# Normalized to alphanumerics before comparison, so spelling variants such as
# api_key and runtime-session-id are rejected too.
_FORBIDDEN_KEYS = {
    "apikey",
    "authorization",
    "bottoken",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "googletoken",
    "headers",
    "password",
    "providertoken",
    "runtimesessionid",
    "secret",
    "sessionid",
    "telegramtoken",
    "webhooksecret",
}


class EnvelopeValidationError(ValueError):
    """The message cannot cross the trusted router-to-worker boundary."""


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise EnvelopeValidationError("payload exceeds maximum JSON depth")
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str) and len(value) > MAX_MESSAGE_TEXT_CHARS:
            raise EnvelopeValidationError("payload string exceeds maximum length")
        return
    if isinstance(value, int):
        if abs(value) > 2**53 - 1:
            raise EnvelopeValidationError("payload integer exceeds interoperable range")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EnvelopeValidationError("payload contains a non-finite number")
        return
    if isinstance(value, list):
        if len(value) > MAX_JSON_COLLECTION_ITEMS:
            raise EnvelopeValidationError("payload list is too large")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, Mapping):
        if len(value) > MAX_JSON_COLLECTION_ITEMS:
            raise EnvelopeValidationError("payload object is too large")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise EnvelopeValidationError("payload keys must be bounded strings")
            if _normalized_key(key) in _FORBIDDEN_KEYS:
                raise EnvelopeValidationError("payload contains a forbidden control-plane field")
            _validate_json_value(item, depth=depth + 1)
        return
    raise EnvelopeValidationError("payload contains a non-JSON value")


def _require_string(name: str, value: Any, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise EnvelopeValidationError(f"invalid {name}")
    return value


def _validate_structured_message(user_id: str, message: Mapping[str, Any]) -> None:
    if set(message) != {"text", "images"}:
        raise EnvelopeValidationError("structured message has unexpected fields")
    text = message["text"]
    images = message["images"]
    if not isinstance(text, str) or len(text) > MAX_MESSAGE_TEXT_CHARS:
        raise EnvelopeValidationError("invalid structured message text")
    if not isinstance(images, list) or not 1 <= len(images) <= 4:
        raise EnvelopeValidationError("structured message requires one to four images")
    for image in images:
        if not isinstance(image, Mapping) or set(image) != {"s3Key", "contentType"}:
            raise EnvelopeValidationError("invalid image reference")
        s3_key = image["s3Key"]
        content_type = image["contentType"]
        if not isinstance(s3_key, str) or _S3_KEY.fullmatch(s3_key) is None:
            raise EnvelopeValidationError("invalid image object key")
        if not s3_key.startswith(f"{user_id}/"):
            raise EnvelopeValidationError("image reference is outside the user's namespace")
        if content_type not in _CONTENT_TYPES:
            raise EnvelopeValidationError("unsupported image content type")


def _validate_payload(user_id: str, kind: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise EnvelopeValidationError("payload must be an object")
    _validate_json_value(payload)
    expected = {"chatId", "actorId", "command" if kind == "command" else "message"}
    if set(payload) != expected:
        raise EnvelopeValidationError("payload fields do not match the envelope kind")

    chat_id = _require_string("Telegram chat ID", payload["chatId"], _CHAT_ID)
    actor_id = _require_string("Telegram actor ID", payload["actorId"], _ACTOR_ID)
    if kind == "command":
        command = parse_product_command(payload["command"])
        if command is None or payload["command"].lower().split("@", 1)[0] != command.name:
            raise EnvelopeValidationError("invalid product command")
        return {"chatId": chat_id, "actorId": actor_id, "command": command.name}

    message = payload["message"]
    if isinstance(message, str):
        if not message or len(message) > MAX_MESSAGE_TEXT_CHARS:
            raise EnvelopeValidationError("message text must be non-empty and bounded")
    elif isinstance(message, Mapping):
        _validate_structured_message(user_id, message)
    else:
        raise EnvelopeValidationError("message must be text or a structured image message")
    return {"chatId": chat_id, "actorId": actor_id, "message": deepcopy(message)}


class QueueEnvelope:
    """An immutable-at-the-boundary queue envelope with canonical wire bytes."""

    __slots__ = (
        "user_id",
        "channel",
        "update_id",
        "trace_id",
        "kind",
        "_json",
        "_dedupe_id",
        "_request_sha256",
        "_sealed",
    )

    def __init__(
        self,
        *,
        user_id: str,
        channel: str,
        update_id: str,
        trace_id: str,
        kind: str,
        payload: Mapping[str, Any],
    ) -> None:
        object.__setattr__(self, "_sealed", False)
        self.user_id = _require_string("user ID", user_id, _USER_ID)
        if channel != "telegram":
            raise EnvelopeValidationError("unsupported queue channel")
        self.channel = channel
        self.update_id = _require_string("update ID", update_id, _UPDATE_ID)
        try:
            self.trace_id = assert_event_trace(
                trace_id,
                channel=self.channel,
                user_id=self.user_id,
                platform_event_id=self.update_id,
            )
        except ValueError as error:
            raise EnvelopeValidationError(str(error)) from error
        if kind not in {"command", "message"}:
            raise EnvelopeValidationError("unsupported message kind")
        self.kind = kind
        validated_payload = _validate_payload(self.user_id, self.kind, payload)

        wire = {
            "userId": self.user_id,
            "channel": self.channel,
            "updateId": self.update_id,
            "traceId": self.trace_id,
            "kind": self.kind,
            "payload": validated_payload,
        }
        self._json = json.dumps(
            wire,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(self._json.encode("utf-8")) > MAX_ENVELOPE_BYTES:
            raise EnvelopeValidationError("queue envelope exceeds maximum size")
        stable_content = json.dumps(
            {
                "userId": self.user_id,
                "channel": self.channel,
                "updateId": self.update_id,
                "kind": self.kind,
                "payload": validated_payload,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        self._dedupe_id = self.trace_id
        self._request_sha256 = hashlib.sha256(
            b"personal-operator-telegram-envelope-v1\0"
            + stable_content.encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("QueueEnvelope is immutable")
        object.__setattr__(self, name, value)

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self._json)["payload"]

    @property
    def message_group_id(self) -> str:
        return self.user_id

    @property
    def message_deduplication_id(self) -> str:
        return self._dedupe_id

    @property
    def request_sha256(self) -> str:
        """Digest used by the ledger to reject same-event content collisions."""

        return self._request_sha256

    def to_wire(self) -> dict[str, Any]:
        return json.loads(self._json)

    def to_json(self) -> str:
        return self._json

    @classmethod
    def from_wire(cls, wire: Any) -> "QueueEnvelope":
        if not isinstance(wire, Mapping) or set(wire) != set(_WIRE_KEYS):
            raise EnvelopeValidationError("queue envelope must have the exact wire fields")
        return cls(
            user_id=wire["userId"],
            channel=wire["channel"],
            update_id=wire["updateId"],
            trace_id=wire["traceId"],
            kind=wire["kind"],
            payload=wire["payload"],
        )

    @classmethod
    def from_json(cls, body: Any) -> "QueueEnvelope":
        if (
            not isinstance(body, str)
            or not body
            or len(body.encode("utf-8")) > MAX_ENVELOPE_BYTES
        ):
            raise EnvelopeValidationError("queue body must be bounded UTF-8 JSON text")
        try:
            def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate JSON key")
                    result[key] = value
                return result

            wire = json.loads(
                body,
                object_pairs_hook=reject_duplicates,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise EnvelopeValidationError("queue body is not valid JSON") from error
        return cls.from_wire(wire)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, QueueEnvelope) and self.to_wire() == other.to_wire()

    def __repr__(self) -> str:
        return (
            f"QueueEnvelope(user_id={self.user_id!r}, channel={self.channel!r}, "
            f"update_id={self.update_id!r}, trace_id={self.trace_id!r}, "
            f"kind={self.kind!r}, payload=<redacted>)"
        )


def build_fifo_send_request(queue_url: str, envelope: QueueEnvelope) -> dict[str, str]:
    """Build explicit FIFO arguments without making an AWS call."""

    if (
        not isinstance(queue_url, str)
        or not queue_url.startswith("https://")
        or not queue_url.endswith(".fifo")
        or len(queue_url) > 2_048
    ):
        raise EnvelopeValidationError("invalid FIFO queue URL")
    if not isinstance(envelope, QueueEnvelope):
        raise TypeError("envelope must be a QueueEnvelope")
    return {
        "QueueUrl": queue_url,
        "MessageBody": envelope.to_json(),
        "MessageGroupId": envelope.message_group_id,
        "MessageDeduplicationId": envelope.message_deduplication_id,
    }
