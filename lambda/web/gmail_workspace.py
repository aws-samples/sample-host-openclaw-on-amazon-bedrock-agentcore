"""Trusted web projection and revision service for derived Gmail pilot data.

The browser can read only the bounded records already produced by the
read-only Gmail workflow. Editing creates another immutable local draft
revision; this service has no provider client and cannot send email.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re
from typing import Mapping

from workflows.gmail.models import DraftRevision
from workflows.gmail.repository import (
    DraftRevisionConflictError,
    DynamoGmailRepository,
)


DERIVED_RECORD_TTL = timedelta(days=14)
MAX_DRAFT_PAGES = 4
DRAFT_PAGE_SIZE = 25
_USER_ID = re.compile(r"[A-Za-z0-9._:@+-]{1,128}")
_ACTION_ID = re.compile(r"[A-Za-z0-9_-]{8,128}")
_DRAFT_KEY = re.compile(
    r"GMAIL#DRAFT#(?P<action>[A-Za-z0-9_-]{8,128})#(?P<revision>[0-9]{10})"
)
_SOURCE_ID = re.compile(r"gmail:[A-Za-z0-9_-]{1,128}:[A-Za-z0-9_-]{1,128}")
_OPPORTUNITY_FIELDS = {
    "id",
    "userId",
    "source",
    "waitingSince",
    "title",
    "reason",
    "confidence",
}
_SOURCE_FIELDS = {
    "sourceId",
    "threadId",
    "deepLink",
    "correspondent",
    "subject",
    "excerpt",
}
_DRAFT_FIELDS = {"actionId", "revision", "to", "subject", "body", "payloadHash"}


class GmailWorkspaceRecordError(RuntimeError):
    """A persisted trusted record no longer matches its frozen schema."""


class DraftEditConflict(RuntimeError):
    """The requested local draft revision is absent or no longer current."""


def _identifier(value: object, *, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _text(
    value: object,
    *,
    label: str,
    limit: int,
    allow_empty: bool = False,
) -> str:
    if (
        not isinstance(value, str)
        or "\x00" in value
        or len(value) > limit
        or (not allow_empty and not value)
    ):
        raise GmailWorkspaceRecordError(f"stored {label} is invalid")
    return value


def _integer(value: object, *, label: str, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise GmailWorkspaceRecordError(f"stored {label} is invalid")
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise GmailWorkspaceRecordError(f"stored {label} is invalid")
        result = int(value)
    elif isinstance(value, int):
        result = value
    else:
        raise GmailWorkspaceRecordError(f"stored {label} is invalid")
    if result <= 0 or (maximum is not None and result > maximum):
        raise GmailWorkspaceRecordError(f"stored {label} is invalid")
    return result


def _generation(value: object) -> int:
    if isinstance(value, bool):
        raise GmailWorkspaceRecordError("stored connection generation is invalid")
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise GmailWorkspaceRecordError("stored connection generation is invalid")
        value = int(value)
    if not isinstance(value, int) or value < 0:
        raise GmailWorkspaceRecordError("stored connection generation is invalid")
    return value


def _request_revision(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 9_999_999_998
    ):
        raise ValueError("revision must be a positive current revision")
    return value


def _aware_iso(value: object, *, label: str) -> str:
    value = _text(value, label=label, limit=64)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise GmailWorkspaceRecordError(f"stored {label} is invalid") from error
    if parsed.tzinfo is None:
        raise GmailWorkspaceRecordError(f"stored {label} is invalid")
    return value


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise GmailWorkspaceRecordError("stored opportunity confidence is invalid")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise GmailWorkspaceRecordError(
            "stored opportunity confidence is invalid"
        ) from None
    if not 0.0 <= number <= 1.0:
        raise GmailWorkspaceRecordError("stored opportunity confidence is invalid")
    return number


def _opportunity_projection(record: object, *, user_id: str) -> dict[str, object]:
    if not isinstance(record, Mapping) or set(record) != _OPPORTUNITY_FIELDS:
        raise GmailWorkspaceRecordError("stored opportunity fields are invalid")
    if record.get("userId") != user_id:
        raise GmailWorkspaceRecordError("stored opportunity belongs to another user")
    source = record.get("source")
    if not isinstance(source, Mapping) or set(source) != _SOURCE_FIELDS:
        raise GmailWorkspaceRecordError("stored opportunity source is invalid")
    source_id = _text(source.get("sourceId"), label="sourceId", limit=270)
    if _SOURCE_ID.fullmatch(source_id) is None:
        raise GmailWorkspaceRecordError("stored opportunity sourceId is invalid")
    thread_id = _text(source.get("threadId"), label="threadId", limit=128)
    deep_link = _text(source.get("deepLink"), label="deepLink", limit=512)
    if deep_link != f"https://mail.google.com/mail/u/0/#inbox/{thread_id}":
        raise GmailWorkspaceRecordError("stored opportunity deep link is invalid")
    # Validate but deliberately do not expose the stored bounded excerpt. The
    # card reason is the product-facing derived explanation.
    _text(source.get("excerpt"), label="excerpt", limit=280, allow_empty=True)
    return {
        "id": _text(record.get("id"), label="opportunity id", limit=128),
        "title": _text(record.get("title"), label="title", limit=120),
        "reason": _text(record.get("reason"), label="reason", limit=280),
        "waitingSince": _aware_iso(record.get("waitingSince"), label="waitingSince"),
        "sourceUrl": deep_link,
        "correspondent": _text(
            source.get("correspondent"), label="correspondent", limit=320
        ),
        "subject": _text(
            source.get("subject"), label="subject", limit=200, allow_empty=True
        ),
        "confidence": _confidence(record.get("confidence")),
    }


def _draft_projection(
    item: object,
    *,
    user_id: str,
    now_epoch: int,
    expected_generation: int | None = None,
) -> dict | None:
    fields = {"PK", "SK", "draft", "ttl"}
    if not isinstance(item, Mapping) or set(item) not in {
        frozenset(fields),
        frozenset({*fields, "connectionGeneration"}),
    }:
        raise GmailWorkspaceRecordError("stored draft record fields are invalid")
    if item.get("PK") != f"USER#{user_id}":
        raise GmailWorkspaceRecordError("stored draft belongs to another user")
    sk = item.get("SK")
    match = _DRAFT_KEY.fullmatch(sk) if isinstance(sk, str) else None
    if match is None:
        raise GmailWorkspaceRecordError("stored draft key is invalid")
    ttl = _integer(item.get("ttl"), label="draft ttl")
    if expected_generation is not None:
        stored_generation = item.get("connectionGeneration")
        if stored_generation is None or _generation(stored_generation) != expected_generation:
            return None
    if ttl <= now_epoch:
        return None
    stored = item.get("draft")
    if not isinstance(stored, Mapping) or set(stored) != _DRAFT_FIELDS:
        raise GmailWorkspaceRecordError("stored draft payload fields are invalid")
    revision = _integer(
        stored.get("revision"), label="draft revision", maximum=9_999_999_999
    )
    if (
        stored.get("actionId") != match.group("action")
        or revision != int(match.group("revision"))
    ):
        raise GmailWorkspaceRecordError("stored draft key does not bind its payload")
    try:
        draft = DraftRevision(
            action_id=stored.get("actionId"),
            revision=revision,
            to=stored.get("to"),
            subject=stored.get("subject"),
            body=stored.get("body"),
            payload_hash=stored.get("payloadHash"),
        )
    except (TypeError, ValueError):
        raise GmailWorkspaceRecordError("stored draft payload is invalid") from None
    return {
        "actionId": draft.action_id,
        "revision": draft.revision,
        "to": draft.to,
        "subject": draft.subject,
        "body": draft.body,
        "payloadHash": draft.payload_hash,
    }


class GmailWorkspaceService:
    """Read and revise session-bound, local Gmail pilot artifacts."""

    def __init__(
        self,
        table,
        *,
        repository=None,
        approval_superseder=None,
        enforce_connection_fence: bool = False,
        now=None,
    ) -> None:
        self._table = table
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._repository = repository or DynamoGmailRepository(table, now=self._now)
        if approval_superseder is not None and not callable(
            getattr(approval_superseder, "supersede_pending", None)
        ):
            raise TypeError("approval superseder is invalid")
        self._approval_superseder = approval_superseder
        if not isinstance(enforce_connection_fence, bool):
            raise TypeError("connection fence setting is invalid")
        self._enforce_connection_fence = enforce_connection_fence

    def _now_utc(self) -> datetime:
        current = self._now()
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise GmailWorkspaceRecordError("Gmail workspace clock is invalid")
        return current.astimezone(timezone.utc)

    def _opportunities(
        self,
        user_id: str,
        *,
        now_epoch: int,
        expected_generation: int | None = None,
    ) -> list[dict]:
        response = self._table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": "GMAIL#OPPORTUNITIES"},
            ConsistentRead=True,
        )
        if not isinstance(response, Mapping):
            raise GmailWorkspaceRecordError("opportunity read returned an invalid result")
        item = response.get("Item")
        if item is None:
            return []
        if (
            not isinstance(item, Mapping)
            or set(item)
            not in {
                frozenset({"PK", "SK", "opportunities", "ttl"}),
                frozenset(
                    {"PK", "SK", "opportunities", "ttl", "connectionGeneration"}
                ),
            }
            or item.get("PK") != f"USER#{user_id}"
            or item.get("SK") != "GMAIL#OPPORTUNITIES"
        ):
            raise GmailWorkspaceRecordError("stored opportunity record is invalid")
        ttl = _integer(item.get("ttl"), label="opportunity ttl")
        if expected_generation is not None:
            stored_generation = item.get("connectionGeneration")
            if (
                stored_generation is None
                or _generation(stored_generation) != expected_generation
            ):
                return []
        if ttl <= now_epoch:
            return []
        records = item.get("opportunities")
        if not isinstance(records, list) or len(records) > 3:
            raise GmailWorkspaceRecordError("stored opportunity list is invalid")
        projected = [
            _opportunity_projection(record, user_id=user_id) for record in records
        ]
        identifiers = [record["id"] for record in projected]
        if len(set(identifiers)) != len(identifiers):
            raise GmailWorkspaceRecordError("stored opportunity IDs are not unique")
        return projected

    def _drafts(
        self,
        user_id: str,
        *,
        now_epoch: int,
        expected_generation: int | None = None,
    ) -> list[dict]:
        latest: dict[str, dict] = {}
        start_key = None
        for page_number in range(MAX_DRAFT_PAGES):
            request = {
                "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
                "ExpressionAttributeValues": {
                    ":pk": f"USER#{user_id}",
                    ":sk": "GMAIL#DRAFT#",
                },
                "ConsistentRead": True,
                "ScanIndexForward": True,
                "Limit": DRAFT_PAGE_SIZE,
            }
            if start_key is not None:
                request["ExclusiveStartKey"] = start_key
            response = self._table.query(**request)
            if not isinstance(response, Mapping) or not isinstance(
                response.get("Items", []), list
            ):
                raise GmailWorkspaceRecordError("draft query returned an invalid result")
            for item in response.get("Items", []):
                draft = _draft_projection(
                    item,
                    user_id=user_id,
                    now_epoch=now_epoch,
                    expected_generation=expected_generation,
                )
                if draft is None:
                    continue
                current = latest.get(draft["actionId"])
                if current is None or draft["revision"] > current["revision"]:
                    latest[draft["actionId"]] = draft
            start_key = response.get("LastEvaluatedKey")
            if start_key is None:
                break
            if (
                not isinstance(start_key, Mapping)
                or set(start_key) != {"PK", "SK"}
                or start_key.get("PK") != f"USER#{user_id}"
                or not isinstance(start_key.get("SK"), str)
                or not start_key["SK"].startswith("GMAIL#DRAFT#")
            ):
                raise GmailWorkspaceRecordError("draft query cursor is invalid")
            if page_number == MAX_DRAFT_PAGES - 1:
                raise GmailWorkspaceRecordError("draft record bound was exceeded")
        return [latest[action_id] for action_id in sorted(latest)]

    def get(self, user_id: str) -> dict[str, object]:
        user_id = _identifier(user_id, label="user_id", pattern=_USER_ID)
        now_epoch = int(self._now_utc().timestamp())
        generation = None
        if self._enforce_connection_fence:
            try:
                generation = self._repository.connected_generation(user_id)
            except RuntimeError:
                return {"userId": user_id, "opportunities": [], "drafts": []}
        opportunities = self._opportunities(
            user_id,
            now_epoch=now_epoch,
            expected_generation=generation,
        )
        drafts = self._drafts(
            user_id,
            now_epoch=now_epoch,
            expected_generation=generation,
        )
        if generation is not None:
            try:
                self._repository.assert_generation(
                    user_id, generation, require_connected=True
                )
            except RuntimeError:
                return {"userId": user_id, "opportunities": [], "drafts": []}
        return {
            "userId": user_id,
            "opportunities": opportunities,
            "drafts": drafts,
        }

    def _latest_draft(
        self,
        *,
        user_id: str,
        action_id: str,
        now_epoch: int,
        expected_generation: int | None = None,
    ) -> dict | None:
        response = self._table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": f"USER#{user_id}",
                ":sk": f"GMAIL#DRAFT#{action_id}#",
            },
            ConsistentRead=True,
            ScanIndexForward=False,
            Limit=1,
        )
        if not isinstance(response, Mapping) or not isinstance(
            response.get("Items", []), list
        ):
            raise GmailWorkspaceRecordError("draft query returned an invalid result")
        items = response.get("Items", [])
        if len(items) > 1:
            raise GmailWorkspaceRecordError("draft query exceeded its exact bound")
        if not items:
            return None
        draft = _draft_projection(
            items[0],
            user_id=user_id,
            now_epoch=now_epoch,
            expected_generation=expected_generation,
        )
        if draft is not None and draft["actionId"] != action_id:
            raise GmailWorkspaceRecordError("draft query crossed an action boundary")
        return draft

    def edit_draft(
        self,
        *,
        user_id: str,
        action_id: str,
        revision: int,
        subject: str,
        body: str,
    ) -> dict[str, dict[str, object]]:
        user_id = _identifier(user_id, label="user_id", pattern=_USER_ID)
        action_id = _identifier(action_id, label="action_id", pattern=_ACTION_ID)
        revision = _request_revision(revision)
        if self._approval_superseder is None:
            raise DraftEditConflict(
                "draft approval supersession boundary is unavailable"
            )
        # Validate all caller-controlled content before making a storage read.
        try:
            preliminary = DraftRevision.create(
                action_id=action_id,
                revision=revision + 1,
                to="immutable@example.invalid",
                subject=subject,
                body=body,
            )
        except (TypeError, ValueError) as error:
            raise ValueError(str(error)) from None

        now = self._now_utc()
        generation = None
        if self._enforce_connection_fence:
            try:
                generation = self._repository.connected_generation(user_id)
            except RuntimeError:
                raise DraftEditConflict("draft is not available") from None
        current = self._latest_draft(
            user_id=user_id,
            action_id=action_id,
            now_epoch=int(now.timestamp()),
            expected_generation=generation,
        )
        if current is None:
            raise DraftEditConflict("draft is not available")
        if current["revision"] != revision:
            raise DraftEditConflict("draft current revision has changed")
        revised = DraftRevision.create(
            action_id=action_id,
            revision=revision + 1,
            to=current["to"],
            subject=preliminary.subject,
            body=preliminary.body,
        )
        expires_at = int((now + DERIVED_RECORD_TTL).timestamp())
        # The kernel-owned boundary must atomically persist this immutable
        # revision with either an exact active-action -> STALE transition or an
        # exact proof that the action item is still absent.  This closes both
        # create-before-edit and edit-before-create interleavings on the same
        # DynamoDB ACTION item used by approval transitions.
        outcome = self._approval_superseder.supersede_pending(
            action_id=action_id,
            user_id=user_id,
            expected_draft_revision=revision,
            current_draft_revision=revised.revision,
            draft=revised,
            expires_at=expires_at,
            expected_generation=generation,
        )
        if not isinstance(outcome, Mapping) or dict(outcome) != {
            "draftPersisted": True,
            "actionId": action_id,
            "userId": user_id,
            "draftRevision": revised.revision,
            "payloadHash": revised.payload_hash,
        }:
            raise DraftEditConflict("atomic draft edit outcome is unproven")
        try:
            # Reconcile through the ordinary immutable-write port as a second
            # exact, idempotent proof.  Production has already committed this
            # byte-identical record inside the atomic authority transaction;
            # this call cannot create a different revision or restore action
            # authority.
            arguments = {
                "user_id": user_id,
                "draft": revised,
                "expires_at": expires_at,
            }
            if generation is not None:
                arguments["expected_generation"] = generation
            self._repository.save_draft(**arguments)
        except DraftRevisionConflictError:
            raise DraftEditConflict("draft current revision has changed") from None
        return {
            "draft": {
                "actionId": revised.action_id,
                "revision": revised.revision,
                "to": revised.to,
                "subject": revised.subject,
                "body": revised.body,
                "payloadHash": revised.payload_hash,
            }
        }
