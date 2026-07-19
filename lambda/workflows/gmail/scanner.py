"""Read-only Gmail thread scanner with a deliberately narrow data egress."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
import json
import logging
import re
from typing import Callable, Iterable, Mapping

try:
    from .models import SourceEvidence
except ImportError:  # direct file loading in Lambda/unit tests
    from gmail_models import SourceEvidence


LOG = logging.getLogger(__name__)
_LOG_SCHEMA = "personal-operator.log.v1"
_LOG_WARNING_EVENTS = frozenset(
    {"thread_listing_failed", "thread_processing_failed"}
)
SEARCH_QUERY = "in:sent older_than:3d newer_than:30d -in:chats"
MAX_THREADS = 50
MAX_MIME_DEPTH = 8
MAX_MIME_PARTS = 64
MAX_ENCODED_BODY_BYTES = 256 * 1024
MAX_DECODED_BODY_BYTES = 64 * 1024
_AUTOMATED_LOCAL = re.compile(
    r"^(?:no[._-]?reply|do[._-]?not[._-]?reply|mailer[._-]?daemon|notifications?)$",
    re.IGNORECASE,
)


def _log_warning(event: str) -> None:
    """Emit one closed metadata record without provider or exception data."""

    if event not in _LOG_WARNING_EVENTS:
        raise ValueError("Gmail scanner log event is not allowlisted")
    LOG.warning(
        json.dumps(
            {
                "component": "gmail",
                "event": event,
                "level": "WARNING",
                "schema": _LOG_SCHEMA,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


class GmailProviderError(RuntimeError):
    """A canonical provider failure that is never logged verbatim."""


class GmailPayloadBoundsError(ValueError):
    """A full-message MIME tree exceeded the scanner's fixed parsing budget."""


class GoogleGmailApiClient:
    """Minimal read-only adapter around an injected Google Gmail API service.

    The injected object is the authorized ``googleapiclient`` service. This
    adapter intentionally exposes only thread listing and retrieval; it has no
    message-send, draft-create, or mailbox-mutation surface.
    """

    def __init__(self, service, *, mailbox: str = "me") -> None:
        if not isinstance(mailbox, str) or not mailbox or len(mailbox) > 320:
            raise ValueError("mailbox is invalid")
        self._service = service
        self._mailbox = mailbox

    def list_threads(self, *, query: str, max_results: int) -> list[dict[str, str]]:
        if not isinstance(query, str) or not query or len(query) > 1_024:
            raise ValueError("query is invalid")
        if isinstance(max_results, bool) or not isinstance(max_results, int):
            raise ValueError("max_results is invalid")
        bounded_max = min(MAX_THREADS, max(1, max_results))
        try:
            response = (
                self._service.users()
                .threads()
                .list(userId=self._mailbox, q=query, maxResults=bounded_max)
                .execute(num_retries=0)
            )
        except Exception:
            # Provider exceptions can include request metadata. Never retain or
            # propagate their message across the trusted adapter boundary.
            raise GmailProviderError("Gmail thread listing failed") from None
        if not isinstance(response, Mapping) or not isinstance(
            response.get("threads", []), list
        ):
            raise GmailProviderError("Gmail thread listing returned invalid data")
        result: list[dict[str, str]] = []
        for item in response.get("threads", [])[:bounded_max]:
            if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                raise GmailProviderError("Gmail thread listing returned invalid data")
            result.append({"id": item["id"]})
        return result

    def get_thread(self, *, thread_id: str, format: str) -> Mapping[str, object]:
        if (
            not isinstance(thread_id, str)
            or not thread_id
            or len(thread_id) > 128
            or not isinstance(format, str)
            or format not in {"full"}
        ):
            raise ValueError("thread request is invalid")
        try:
            response = (
                self._service.users()
                .threads()
                .get(
                    userId=self._mailbox,
                    id=thread_id,
                    format=format,
                )
                .execute(num_retries=0)
            )
        except Exception:
            raise GmailProviderError("Gmail thread retrieval failed") from None
        if not isinstance(response, Mapping):
            raise GmailProviderError("Gmail thread retrieval returned invalid data")
        return response


def _headers(message: Mapping[str, object]) -> dict[str, str]:
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        return {}
    raw = payload.get("headers")
    if not isinstance(raw, list):
        return {}
    result: dict[str, str] = {}
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        value = entry.get("value")
        if isinstance(name, str) and isinstance(value, str):
            result[name.lower()] = value
    return result


def _addresses(value: str) -> list[str]:
    return [address.casefold() for _, address in getaddresses([value]) if address]


def _is_automated(headers: Mapping[str, str], correspondent: str) -> bool:
    precedence = headers.get("precedence", "").casefold()
    if precedence in {"bulk", "junk", "list"}:
        return True
    if headers.get("list-id") or headers.get("list-unsubscribe"):
        return True
    auto_submitted = headers.get("auto-submitted", "").casefold()
    if auto_submitted and auto_submitted != "no":
        return True
    local = correspondent.rsplit("@", 1)[0]
    return bool(_AUTOMATED_LOCAL.fullmatch(local))


class _BodyBudget:
    __slots__ = ("parts", "encoded_bytes", "decoded_bytes")

    def __init__(self) -> None:
        self.parts = 0
        self.encoded_bytes = 0
        self.decoded_bytes = 0


def _decode_body(
    payload: Mapping[str, object],
    *,
    depth: int = 0,
    budget: _BodyBudget | None = None,
) -> str:
    if depth > MAX_MIME_DEPTH:
        raise GmailPayloadBoundsError("Gmail MIME tree is too deep")
    budget = budget or _BodyBudget()
    budget.parts += 1
    if budget.parts > MAX_MIME_PARTS:
        raise GmailPayloadBoundsError("Gmail MIME tree has too many parts")
    body = payload.get("body")
    if isinstance(body, Mapping) and isinstance(body.get("data"), str):
        data = body["data"]
        budget.encoded_bytes += len(data)
        if budget.encoded_bytes > MAX_ENCODED_BODY_BYTES:
            raise GmailPayloadBoundsError("Gmail encoded body is too large")
        padded = data + "=" * (-len(data) % 4)
        try:
            decoded = base64.b64decode(
                padded,
                altchars=b"-_",
                validate=True,
            )
        except (ValueError, TypeError) as error:
            raise GmailPayloadBoundsError("Gmail body encoding is invalid") from error
        budget.decoded_bytes += len(decoded)
        if budget.decoded_bytes > MAX_DECODED_BODY_BYTES:
            raise GmailPayloadBoundsError("Gmail decoded body is too large")
        return decoded.decode("utf-8", "replace")
    parts = payload.get("parts")
    if isinstance(parts, list):
        if len(parts) > MAX_MIME_PARTS:
            raise GmailPayloadBoundsError("Gmail MIME tree has too many parts")
        text_parts: list[str] = []
        for part in parts:
            if not isinstance(part, Mapping):
                raise GmailPayloadBoundsError("Gmail MIME part is invalid")
            if str(part.get("mimeType", "")).casefold() not in {
                "text/plain",
                "multipart/alternative",
                "multipart/mixed",
            }:
                continue
            text_parts.append(
                _decode_body(part, depth=depth + 1, budget=budget)
            )
        return "\n".join(part for part in text_parts if part)
    return ""


def _excerpt(message: Mapping[str, object]) -> str:
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        return ""
    text = _decode_body(payload).replace("\x00", "")
    text = re.split(r"\nOn .{0,200}wrote:\s*\n", text, maxsplit=1, flags=re.IGNORECASE)[0]
    text = re.sub(r"\s+", " ", text).strip()
    return text[:280]


def _message_time(message: Mapping[str, object]) -> datetime:
    value = message.get("internalDate")
    if not isinstance(value, str) or not value.isdigit():
        raise ValueError("Gmail message has no valid internalDate")
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


class GmailScanner:
    """Select unanswered human outbound threads without retaining raw bodies."""

    def __init__(
        self,
        client,
        *,
        connected_address: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(connected_address, str) or "@" not in connected_address:
            raise ValueError("connected_address must be an email address")
        self._client = client
        self._connected_address = connected_address.casefold().strip()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def scan(self) -> list[SourceEvidence]:
        try:
            listed = self._client.list_threads(
                query=SEARCH_QUERY, max_results=MAX_THREADS
            )
        except Exception:
            _log_warning("thread_listing_failed")
            return []
        if not isinstance(listed, list):
            return []
        results: list[SourceEvidence] = []
        for item in listed[:MAX_THREADS]:
            if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                continue
            try:
                thread = self._client.get_thread(thread_id=item["id"], format="full")
                candidate = self._candidate(thread)
            except Exception:
                # Provider records and exception details are untrusted.
                _log_warning("thread_processing_failed")
                continue
            if candidate is not None:
                results.append(candidate)
        return results

    def _candidate(self, thread: Mapping[str, object]) -> SourceEvidence | None:
        thread_id = thread.get("id")
        messages = thread.get("messages")
        if not isinstance(thread_id, str) or not isinstance(messages, list) or not messages:
            return None
        ordered = sorted(
            (message for message in messages if isinstance(message, Mapping)),
            key=_message_time,
        )
        if not ordered:
            return None

        human_events: list[tuple[Mapping[str, object], bool, str, dict[str, str]]] = []
        for message in ordered:
            headers = _headers(message)
            senders = _addresses(headers.get("from", ""))
            recipients = _addresses(headers.get("to", ""))
            from_connected_address = self._connected_address in senders
            label_ids = message.get("labelIds")
            has_sent_label = (
                isinstance(label_ids, list)
                and len(label_ids) <= 64
                and all(isinstance(label, str) for label in label_ids)
                and "SENT" in label_ids
            )
            if from_connected_address and not has_sent_label:
                # The From header is untrusted. Gmail's system SENT label is
                # required before a record can be treated as user-authored.
                continue
            outbound = from_connected_address and has_sent_label
            correspondent_candidates = recipients if outbound else senders
            correspondent = next(
                (address for address in correspondent_candidates if address != self._connected_address),
                "",
            )
            if not correspondent or _is_automated(headers, correspondent):
                continue
            human_events.append((message, outbound, correspondent, headers))

        if not human_events:
            return None
        message, outbound, correspondent, headers = human_events[-1]
        if not outbound:
            return None
        sent_at = _message_time(message)
        now = self._now().astimezone(timezone.utc)
        if sent_at > now - timedelta(days=3) or sent_at < now - timedelta(days=30):
            return None
        message_id = message.get("id")
        if not isinstance(message_id, str):
            return None
        subject = re.sub(r"\s+", " ", headers.get("subject", "")).strip()[:200]
        return SourceEvidence(
            source_id=f"gmail:{thread_id}:{message_id}",
            thread_id=thread_id,
            deep_link=f"https://mail.google.com/mail/u/0/#inbox/{thread_id}",
            correspondent=correspondent,
            subject=subject,
            excerpt=_excerpt(message),
            waiting_since=sent_at,
        )
