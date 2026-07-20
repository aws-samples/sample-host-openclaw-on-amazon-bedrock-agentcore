"""Bounded, resumable maintenance for action expiry and Gmail recovery.

The maintenance path can only observe provider history and move an already
durable action through its existing state machine.  It has no send interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Callable, Mapping

try:
    from .models import ActionState
except ImportError:
    from action_models import ActionState


class ActionMaintenanceError(RuntimeError):
    pass


_ACTION_PREFIX = "ACTION#"
_CURSOR_KEY = {
    "PK": "SYSTEM#ACTION_MAINTENANCE",
    "SK": "CURSOR#V1",
}


def _utc(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ActionMaintenanceError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _cursor(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if (
        not isinstance(value, Mapping)
        or set(value) != {"PK", "SK"}
        or not isinstance(value.get("PK"), str)
        or not value["PK"]
        or len(value["PK"]) > 2_048
        or not isinstance(value.get("SK"), str)
        or not value["SK"]
        or len(value["SK"]) > 1_024
        or "\x00" in value["PK"]
        or "\x00" in value["SK"]
    ):
        raise ActionMaintenanceError("action maintenance cursor is invalid")
    return {"PK": value["PK"], "SK": value["SK"]}


def _reference(value: object) -> tuple[str, str]:
    if not isinstance(value, Mapping):
        raise ActionMaintenanceError("action maintenance item is invalid")
    action_id = value.get("actionId")
    user_id = value.get("userId")
    if (
        not isinstance(action_id, str)
        or not 8 <= len(action_id) <= 128
        or not isinstance(user_id, str)
        or not 1 <= len(user_id) <= 128
        or value.get("PK") != f"USER#{user_id}"
        or value.get("SK") != f"ACTION#{action_id}"
        or "\x00" in action_id
        or "\x00" in user_id
    ):
        raise ActionMaintenanceError("action maintenance binding is invalid")
    return action_id, user_id


def _operation_id(
    *, action_id: str, user_id: str, revision: int, target: ActionState
) -> str:
    digest = hashlib.sha256(
        (
            "personal-operator-action-maintenance-v1\0"
            f"{user_id}\0{action_id}\0{revision}\0{target.value}"
        ).encode("utf-8")
    ).hexdigest()[:32]
    return f"maint_{digest}"


class ActionLifecycleMaintainer:
    """Expire unused authority and reconcile in-flight effects without sending."""

    def __init__(
        self,
        *,
        repository,
        state_machine,
        reconciler_factory: Callable[[Mapping[str, object]], object],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if repository is None or state_machine is None or not callable(reconciler_factory):
            raise ValueError("action maintenance dependencies are required")
        self._repository = repository
        self._machine = state_machine
        self._reconciler_factory = reconciler_factory
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _record(self, *, action_id: str, user_id: str) -> dict[str, object] | None:
        record = self._repository.get(action_id=action_id, user_id=user_id)
        if record is None:
            return None
        if not isinstance(record, Mapping):
            raise ActionMaintenanceError("action maintenance read is invalid")
        exact = dict(record)
        _reference(exact)
        revision = exact.get("revision")
        draft_revision = exact.get("draftRevision")
        ttl = exact.get("ttl")
        if (
            exact.get("actionId") != action_id
            or exact.get("userId") != user_id
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or isinstance(draft_revision, bool)
            or not isinstance(draft_revision, int)
            or draft_revision < 1
            or isinstance(ttl, bool)
            or not isinstance(ttl, int)
            or ttl <= 0
        ):
            raise ActionMaintenanceError("action maintenance record is invalid")
        try:
            ActionState(exact.get("state"))
        except (TypeError, ValueError):
            raise ActionMaintenanceError("action maintenance state is invalid") from None
        return exact

    @staticmethod
    def _approval_expired(record: Mapping[str, object], now: datetime) -> bool:
        if record.get("state") not in {
            ActionState.APPROVAL_PENDING.value,
            ActionState.APPROVED.value,
        }:
            return False
        value = record.get("approvalExpiresAt")
        try:
            expiry = datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            expiry = None
        if not isinstance(expiry, datetime) or expiry.tzinfo is None:
            raise ActionMaintenanceError("approval expiry is invalid")
        return now >= expiry.astimezone(timezone.utc)

    def _transition(
        self,
        *,
        record: Mapping[str, object],
        target: ActionState,
        updates: Mapping[str, object],
    ) -> Mapping[str, object]:
        state = ActionState(record["state"])
        return self._machine.transition(
            action_id=record["actionId"],
            user_id=record["userId"],
            current=state,
            target=target,
            revision=record["revision"],
            operation_id=_operation_id(
                action_id=record["actionId"],
                user_id=record["userId"],
                revision=record["revision"],
                target=target,
            ),
            updates=updates,
        )

    def maintain(self, *, action_id: str, user_id: str) -> str:
        record = self._record(action_id=action_id, user_id=user_id)
        if record is None:
            return "missing"
        now = _utc(self._now(), "now")
        state = ActionState(record["state"])

        # An effect-bearing record whose retention has elapsed has no remaining
        # authority to consult a provider or to gain a fresh terminal TTL.
        # DynamoDB expiry owns physical removal; this path is observation-only.
        if state in {ActionState.DISPATCHING, ActionState.UNCERTAIN} and int(
            now.timestamp()
        ) >= record["ttl"]:
            return "retention-expired"

        if state in {ActionState.DISPATCHING, ActionState.UNCERTAIN}:
            reconciler = self._reconciler_factory(record)
            reconcile = getattr(reconciler, "reconcile", None)
            if not callable(reconcile):
                raise ActionMaintenanceError("Gmail reconciler is invalid")
            try:
                reconcile(action_id=action_id, user_id=user_id)
            except Exception:
                pass
            current = self._record(action_id=action_id, user_id=user_id)
            if current is None:
                return "missing"
            current_state = ActionState(current["state"])
            if current_state is ActionState.CONFIRMED:
                return "confirmed"
            if current_state is ActionState.DISPATCHING:
                self._transition(
                    record=current,
                    target=ActionState.UNCERTAIN,
                    updates={
                        "uncertainAt": now.isoformat(),
                        "uncertaintyReason": "provider-outcome-unproven",
                        "uncertainDraftRevision": current["draftRevision"],
                    },
                )
            return "uncertain"

        if state in {
            ActionState.CONFIRMED,
            ActionState.REJECTED,
            ActionState.EXPIRED,
            ActionState.STALE,
            ActionState.CANCELLED,
        }:
            return "terminal"

        expired = int(now.timestamp()) >= record["ttl"] or self._approval_expired(
            record, now
        )
        if not expired:
            return "active"
        if state is ActionState.PREPARED:
            target = ActionState.CANCELLED
            updates = {
                "cancelledAt": now.isoformat(),
                "cancellationReason": "action-retention-expired",
            }
        elif state in {ActionState.APPROVAL_PENDING, ActionState.APPROVED}:
            target = ActionState.EXPIRED
            updates = {"expiredAt": now.isoformat()}
        else:
            raise ActionMaintenanceError("action maintenance state is unsupported")
        self._transition(record=record, target=target, updates=updates)
        return target.value.lower()


class DynamoActionPageSource:
    """One bounded base-table page; callers must persist and resume its cursor."""

    def __init__(self, table, *, page_size: int = 100) -> None:
        if table is None:
            raise ValueError("action table is required")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
            raise ValueError("action maintenance page_size must be between 1 and 100")
        self._table = table
        self._page_size = page_size

    def page(
        self, *, cursor: Mapping[str, str] | None
    ) -> tuple[list[dict[str, object]], dict[str, str] | None]:
        start = _cursor(cursor)
        request: dict[str, object] = {
            "Limit": self._page_size,
            "FilterExpression": "begins_with(#sk,:actionPrefix)",
            "ProjectionExpression": "PK, SK, actionId, userId",
            "ExpressionAttributeNames": {"#sk": "SK"},
            "ExpressionAttributeValues": {":actionPrefix": _ACTION_PREFIX},
        }
        if start is not None:
            request["ExclusiveStartKey"] = start
        try:
            response = self._table.scan(**request)
        except Exception as error:
            raise ActionMaintenanceError("action maintenance scan failed") from error
        items = response.get("Items") if isinstance(response, Mapping) else None
        if (
            not isinstance(items, list)
            or len(items) > self._page_size
            or any(not isinstance(item, Mapping) for item in items)
        ):
            raise ActionMaintenanceError("action maintenance scan response is invalid")
        next_cursor = _cursor(response.get("LastEvaluatedKey"))
        if next_cursor is not None and next_cursor == start:
            raise ActionMaintenanceError("action maintenance cursor did not advance")
        return [dict(item) for item in items], next_cursor


@dataclass(frozen=True, slots=True)
class CursorLease:
    cursor: dict[str, str] | None
    generation: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "cursor", _cursor(self.cursor))
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ActionMaintenanceError("action maintenance generation is invalid")


class DynamoActionCursorStore:
    """Conditional durable cursor so bounded scans cannot starve later records."""

    def __init__(self, table, *, now: Callable[[], datetime] | None = None) -> None:
        if table is None:
            raise ValueError("action cursor table is required")
        self._table = table
        self._now = now or (lambda: datetime.now(timezone.utc))

    def load(self) -> CursorLease:
        try:
            response = self._table.get_item(Key=_CURSOR_KEY, ConsistentRead=True)
        except Exception as error:
            raise ActionMaintenanceError("action maintenance cursor read failed") from error
        item = response.get("Item") if isinstance(response, Mapping) else None
        if item is None:
            return CursorLease(None, 0)
        if (
            not isinstance(item, Mapping)
            or item.get("PK") != _CURSOR_KEY["PK"]
            or item.get("SK") != _CURSOR_KEY["SK"]
            or item.get("kind") != "ACTION_MAINTENANCE_CURSOR_V1"
        ):
            raise ActionMaintenanceError("action maintenance cursor record is invalid")
        return CursorLease(item.get("cursor"), item.get("generation"))

    def save(
        self, lease: CursorLease, cursor: Mapping[str, str] | None
    ) -> None:
        if not isinstance(lease, CursorLease):
            raise TypeError("cursor save requires a CursorLease")
        next_cursor = _cursor(cursor)
        next_generation = lease.generation + 1
        now = _utc(self._now(), "now").isoformat()
        try:
            response = self._table.update_item(
                Key=_CURSOR_KEY,
                UpdateExpression=(
                    "SET #kind=:kind, #cursor=:cursor, #generation=:nextGeneration, "
                    "#updatedAt=:updatedAt"
                ),
                ConditionExpression=(
                    "(attribute_not_exists(#generation) AND :expectedGeneration=:zero) "
                    "OR #generation=:expectedGeneration"
                ),
                ExpressionAttributeNames={
                    "#kind": "kind",
                    "#cursor": "cursor",
                    "#generation": "generation",
                    "#updatedAt": "updatedAt",
                },
                ExpressionAttributeValues={
                    ":kind": "ACTION_MAINTENANCE_CURSOR_V1",
                    ":cursor": next_cursor,
                    ":nextGeneration": next_generation,
                    ":expectedGeneration": lease.generation,
                    ":zero": 0,
                    ":updatedAt": now,
                },
                ReturnValues="ALL_NEW",
            )
            attributes = response.get("Attributes") if isinstance(response, Mapping) else None
            if (
                not isinstance(attributes, Mapping)
                or attributes.get("generation") != next_generation
                or attributes.get("cursor") != next_cursor
            ):
                raise ActionMaintenanceError("action maintenance cursor write is invalid")
        except Exception as error:
            try:
                current = self.load()
            except Exception:
                current = None
            if current == CursorLease(next_cursor, next_generation):
                return
            raise ActionMaintenanceError("action maintenance cursor write failed") from error


class ActionMaintenanceRunner:
    def __init__(
        self,
        *,
        page_source: DynamoActionPageSource,
        lifecycle: ActionLifecycleMaintainer,
        cursor_store,
        max_pages: int = 20,
    ) -> None:
        if page_source is None or lifecycle is None or cursor_store is None:
            raise ValueError("action maintenance runner dependencies are required")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 20:
            raise ValueError("action maintenance max_pages must be between 1 and 20")
        self._pages = page_source
        self._lifecycle = lifecycle
        self._cursors = cursor_store
        self._max_pages = max_pages

    def run(self) -> dict[str, object]:
        lease = self._cursors.load()
        if not isinstance(lease, CursorLease):
            raise ActionMaintenanceError("action maintenance cursor lease is invalid")
        cursor = lease.cursor
        processed = 0
        failed = 0
        next_cursor = cursor
        for _ in range(self._max_pages):
            items, next_cursor = self._pages.page(cursor=cursor)
            for item in items:
                try:
                    action_id, user_id = _reference(item)
                    self._lifecycle.maintain(
                        action_id=action_id,
                        user_id=user_id,
                    )
                    processed += 1
                except Exception:
                    failed += 1
            # Commit progress after every bounded page. A later provider stall
            # or Lambda deadline can replay at most this page, never the whole
            # scan or permanently starve later actions.
            self._cursors.save(lease, next_cursor)
            lease = CursorLease(next_cursor, lease.generation + 1)
            if next_cursor is None:
                break
            cursor = next_cursor
        if failed:
            noun = "item" if failed == 1 else "items"
            raise ActionMaintenanceError(
                f"{failed} action maintenance {noun} failed; retry follows cursor wrap"
            )
        return {
            "status": "ok",
            "processed": processed,
            "failed": failed,
            "hasMore": next_cursor is not None,
        }
