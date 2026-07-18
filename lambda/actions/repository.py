"""DynamoDB-backed, conditionally fenced action records.

The repository uses the shared identity table's composite key but never queries
it.  Every write is a single-item conditional mutation, and every exception is
treated as an ambiguous outcome until a strongly consistent read proves that
the exact operation marker was committed.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Callable, Mapping

try:
    from .models import (
        ActionState,
        DraftRevision,
        EffectReceipt,
        WaitingForReply,
        gmail_resource,
    )
    from .state_machine import ConcurrentActionUpdate, assert_transition
except ImportError:  # direct file loading in unit tests/Lambda bundles
    from action_models import (
        ActionState,
        DraftRevision,
        EffectReceipt,
        WaitingForReply,
        gmail_resource,
    )
    from action_state_machine import ConcurrentActionUpdate, assert_transition


_ACTION_ID = re.compile(r"[A-Za-z0-9_-]{8,128}")
_OPERATION_ID = re.compile(r"[A-Za-z0-9_-]{16,128}")
_MESSAGE_ID = re.compile(r"<po-[0-9a-f]{24}@personal-operator\.invalid>")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PROTECTED = frozenset(
    {
        "PK",
        "SK",
        "actionId",
        "userId",
        "state",
        "revision",
        "creationId",
        "lastTransitionId",
    }
)
_TRANSITION_FIELDS: dict[tuple[ActionState, ActionState], frozenset[str]] = {
    (ActionState.PREPARED, ActionState.APPROVAL_PENDING): frozenset(
        {
            "approvalId",
            "approvalActionId",
            "approvalDraftRevision",
            "approvalArgsHash",
            "approvalExpiresAt",
            "approvalRequestedAt",
        }
    ),
    (ActionState.PREPARED, ActionState.CANCELLED): frozenset({"cancelledAt", "cancellationReason"}),
    (ActionState.PREPARED, ActionState.STALE): frozenset(
        {
            "staleAt",
            "staleReason",
            "staleDraftRevision",
            "supersededByDraftRevision",
        }
    ),
    (ActionState.APPROVAL_PENDING, ActionState.APPROVED): frozenset(
        {
            "approvalId",
            "approvedActionId",
            "approvedDraftRevision",
            "approvedArgsHash",
            "approvedAt",
        }
    ),
    (ActionState.APPROVAL_PENDING, ActionState.REJECTED): frozenset(
        {"rejectedAt"}
    ),
    (ActionState.APPROVAL_PENDING, ActionState.EXPIRED): frozenset(
        {"expiredAt"}
    ),
    (ActionState.APPROVAL_PENDING, ActionState.STALE): frozenset(
        {
            "staleAt",
            "staleReason",
            "staleDraftRevision",
            "supersededByDraftRevision",
        }
    ),
    (ActionState.APPROVAL_PENDING, ActionState.CANCELLED): frozenset(
        {"cancelledAt", "cancellationReason"}
    ),
    (ActionState.APPROVED, ActionState.DISPATCHING): frozenset(
        {"messageId", "dispatchOperationId", "dispatchDraftRevision"}
    ),
    (ActionState.APPROVED, ActionState.EXPIRED): frozenset({"expiredAt"}),
    (ActionState.APPROVED, ActionState.STALE): frozenset(
        {
            "staleAt",
            "staleReason",
            "staleDraftRevision",
            "supersededByDraftRevision",
        }
    ),
    (ActionState.APPROVED, ActionState.CANCELLED): frozenset(
        {"cancelledAt", "cancellationReason"}
    ),
    (ActionState.DISPATCHING, ActionState.CONFIRMED): frozenset(
        {
            "effectReceipt",
            "waitingForReply",
            "confirmationMethod",
            "confirmedAt",
        }
    ),
    (ActionState.DISPATCHING, ActionState.UNCERTAIN): frozenset(
        {"uncertainAt", "uncertaintyReason", "uncertainDraftRevision"}
    ),
    (ActionState.UNCERTAIN, ActionState.CONFIRMED): frozenset(
        {
            "effectReceipt",
            "waitingForReply",
            "confirmationMethod",
            "confirmedAt",
        }
    ),
}


class ActionRepositoryError(RuntimeError):
    """The durable result of a repository operation cannot be proven."""


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _bounded(value: str, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\x00" in value
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _action_id(value: str) -> str:
    if not isinstance(value, str) or _ACTION_ID.fullmatch(value) is None:
        raise ValueError("action_id is invalid")
    return value


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("action record values must be canonical JSON") from error


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _operation_id(value: str, label: str = "transition_id") -> str:
    if not isinstance(value, str) or _OPERATION_ID.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _revision(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} is invalid")
    return value


def _iso_utc(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError as error:
        raise ValueError(f"{label} is invalid") from error
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ValueError(f"{label} is invalid")
    return parsed.astimezone(timezone.utc)


def _validate_transition_updates(
    *,
    action_id: str,
    expected_state: ActionState,
    target_state: ActionState,
    transition_id: str,
    updates: Mapping[str, object],
) -> int | None:
    """Validate the complete transition payload and return its draft fence."""

    required = _TRANSITION_FIELDS.get((expected_state, target_state))
    if required is None or set(updates) != required:
        raise ValueError("transition updates violate the exact state contract")
    exact = dict(updates)
    draft_fence = None
    for name in (
        "approvalRequestedAt",
        "approvalExpiresAt",
        "approvedAt",
        "rejectedAt",
        "expiredAt",
        "staleAt",
        "cancelledAt",
        "uncertainAt",
        "confirmedAt",
    ):
        if name in exact:
            _iso_utc(exact[name], name)
    for name in ("approvalArgsHash", "approvedArgsHash"):
        if name in exact and (
            not isinstance(exact[name], str) or _SHA256.fullmatch(exact[name]) is None
        ):
            raise ValueError(f"{name} is invalid")
    for name in (
        "approvalDraftRevision",
        "approvedDraftRevision",
        "dispatchDraftRevision",
        "uncertainDraftRevision",
        "staleDraftRevision",
    ):
        if name in exact:
            current = _revision(exact[name], name)
            if draft_fence is not None and current != draft_fence:
                raise ValueError("transition draft revisions disagree")
            draft_fence = current
    if "supersededByDraftRevision" in exact:
        superseding = _revision(
            exact["supersededByDraftRevision"], "supersededByDraftRevision"
        )
        if draft_fence is None or superseding <= draft_fence:
            raise ValueError("superseding draft revision must be newer")
    for name in ("approvalActionId", "approvedActionId"):
        if name in exact and exact[name] != action_id:
            raise ValueError(f"{name} does not bind the exact action")
    if "approvalId" in exact:
        _operation_id(exact["approvalId"], "approvalId")
    if "messageId" in exact and (
        not isinstance(exact["messageId"], str)
        or _MESSAGE_ID.fullmatch(exact["messageId"]) is None
    ):
        raise ValueError("messageId is invalid")
    if "dispatchOperationId" in exact:
        if exact["dispatchOperationId"] != transition_id:
            raise ValueError("dispatchOperationId must equal the unique transition operation")
    if "uncertaintyReason" in exact and exact["uncertaintyReason"] not in {
        "provider-outcome-unproven",
        "confirmation-persistence-unproven",
    }:
        raise ValueError("uncertaintyReason is invalid")
    if "staleReason" in exact and exact["staleReason"] != "newer-draft-revision":
        raise ValueError("staleReason is invalid")
    if "cancellationReason" in exact:
        _bounded(exact["cancellationReason"], "cancellationReason", 512)
    if target_state is ActionState.CONFIRMED:
        receipt = EffectReceipt.from_record(exact["effectReceipt"])
        tracker = WaitingForReply.from_record(exact["waitingForReply"])
        confirmed_at = _iso_utc(exact["confirmedAt"], "confirmedAt")
        if exact["confirmationMethod"] not in {
            "provider-send-evidence",
            "provider-history-reconciliation",
        }:
            raise ValueError("confirmationMethod is invalid")
        if (
            tracker.action_id != action_id
            or tracker.message_id != receipt.message_id
            or tracker.connection_id != receipt.connection_id
            or tracker.account_email != receipt.account_email
            or tracker.recipient != receipt.recipient
            or tracker.provider_thread_id != receipt.provider_thread_id
            or tracker.since != receipt.executed_at
            or confirmed_at < receipt.executed_at
        ):
            raise ValueError("effect receipt and reply tracker are not exactly bound")
        draft_fence = tracker.draft_revision
    if target_state is ActionState.APPROVAL_PENDING:
        if _iso_utc(exact["approvalExpiresAt"], "approvalExpiresAt") < _iso_utc(
            exact["approvalRequestedAt"], "approvalRequestedAt"
        ):
            raise ValueError("approval expiry precedes its request")
    return draft_fence


def _key(*, action_id: str, user_id: str) -> dict[str, str]:
    action_id = _action_id(action_id)
    user_id = _bounded(user_id, "user_id", 128)
    return {"PK": f"USER#{user_id}", "SK": f"ACTION#{action_id}"}


class DynamoActionRepository:
    """Exact action persistence over a boto3 DynamoDB Table resource."""

    def __init__(
        self,
        table,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if table is None:
            raise ValueError("table is required")
        self._table = table
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _timestamp(self) -> str:
        return _utc(self._now(), "now").isoformat()

    @staticmethod
    def _assert_binding(
        item: Mapping[str, object], *, action_id: str, user_id: str
    ) -> None:
        key = _key(action_id=action_id, user_id=user_id)
        if (
            item.get("PK") != key["PK"]
            or item.get("SK") != key["SK"]
            or item.get("actionId") != action_id
            or item.get("userId") != user_id
        ):
            raise ActionRepositoryError("action record binding is corrupt")

    def _read(self, *, action_id: str, user_id: str) -> dict[str, object] | None:
        try:
            response = self._table.get_item(
                Key=_key(action_id=action_id, user_id=user_id),
                ConsistentRead=True,
            )
        except Exception as error:
            raise ActionRepositoryError(
                "action persistence outcome cannot be reconciled"
            ) from error
        if not isinstance(response, Mapping):
            raise ActionRepositoryError("DynamoDB returned an invalid read response")
        item = response.get("Item")
        if item is None:
            return None
        if not isinstance(item, Mapping):
            raise ActionRepositoryError("DynamoDB returned an invalid action record")
        result = dict(item)
        self._assert_binding(result, action_id=action_id, user_id=user_id)
        return result

    def get(self, *, action_id: str, user_id: str) -> dict[str, object] | None:
        return self._read(action_id=action_id, user_id=user_id)

    def create_prepared(
        self,
        *,
        draft: DraftRevision,
    ) -> dict[str, object]:
        if not isinstance(draft, DraftRevision):
            raise TypeError("create_prepared requires a typed DraftRevision")
        action_id = draft.action_id
        user_id = draft.user_id
        key = _key(action_id=action_id, user_id=user_id)
        capability = "gmail.send"
        resource = gmail_resource(
            connection_id=draft.connection_id,
            account_email=draft.account_email,
        )
        exact_args = dict(draft.args)
        _canonical(exact_args)
        payload_hash = draft.payload_hash
        creation_id = _digest(
            {
                "actionId": action_id,
                "args": exact_args,
                "capability": capability,
                "connectionId": draft.connection_id,
                "accountEmail": draft.account_email,
                "senderAddress": draft.sender_address,
                "draftRevision": draft.draft_revision,
                "payloadHash": payload_hash,
                "resource": resource,
                "userId": user_id,
            }
        )
        timestamp = self._timestamp()
        item: dict[str, object] = {
            **key,
            "actionId": action_id,
            "userId": user_id,
            "state": ActionState.PREPARED.value,
            "revision": 1,
            "draftRevision": draft.draft_revision,
            "connectionId": draft.connection_id,
            "accountEmail": draft.account_email,
            "senderAddress": draft.sender_address,
            "capability": capability,
            "resource": resource,
            "args": exact_args,
            "payloadHash": payload_hash,
            "creationId": creation_id,
            "createdAt": timestamp,
            "updatedAt": timestamp,
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression=(
                    "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                ),
            )
            return dict(item)
        except Exception as error:
            current = self._read(action_id=action_id, user_id=user_id)
            if (
                current is not None
                and current.get("creationId") == creation_id
                and current.get("payloadHash") == payload_hash
                and current.get("draftRevision") == draft.draft_revision
                and current.get("connectionId") == draft.connection_id
                and current.get("accountEmail") == draft.account_email
                and current.get("senderAddress") == draft.sender_address
                and current.get("capability") == capability
                and current.get("resource") == resource
                and current.get("args") == exact_args
            ):
                return current
            raise ConcurrentActionUpdate(
                "action creation lost a conditional race or is unresolved"
            ) from error

    def transition(
        self,
        *,
        action_id: str,
        user_id: str,
        expected_state: ActionState,
        target_state: ActionState,
        expected_revision: int,
        transition_id: str,
        updates: Mapping[str, object],
    ) -> dict[str, object]:
        key = _key(action_id=action_id, user_id=user_id)
        if not isinstance(expected_state, ActionState) or not isinstance(
            target_state, ActionState
        ):
            raise TypeError("expected_state and target_state must be ActionState")
        assert_transition(expected_state, target_state)
        _revision(expected_revision, "expected_revision")
        transition_id = _operation_id(transition_id)
        if not isinstance(updates, Mapping):
            raise TypeError("updates must be a mapping")
        exact_updates = dict(updates)
        draft_fence = _validate_transition_updates(
            action_id=action_id,
            expected_state=expected_state,
            target_state=target_state,
            transition_id=transition_id,
            updates=exact_updates,
        )
        forbidden = _PROTECTED.intersection(exact_updates)
        if forbidden:
            raise ValueError(
                f"transition cannot update protected fields: {sorted(forbidden)!r}"
            )
        if any(
            not isinstance(name, str)
            or not name
            or len(name) > 128
            or "\x00" in name
            for name in exact_updates
        ):
            raise ValueError("transition update field is invalid")
        _canonical(exact_updates)
        next_revision = expected_revision + 1
        names = {
            "#actionId": "actionId",
            "#userId": "userId",
            "#state": "state",
            "#revision": "revision",
            "#updatedAt": "updatedAt",
            "#lastTransitionId": "lastTransitionId",
        }
        values: dict[str, object] = {
            ":actionId": action_id,
            ":userId": user_id,
            ":expectedState": expected_state.value,
            ":targetState": target_state.value,
            ":expectedRevision": expected_revision,
            ":nextRevision": next_revision,
            ":updatedAt": self._timestamp(),
            ":transitionId": transition_id,
        }
        conditions = [
            "#actionId=:actionId",
            "#userId=:userId",
            "#state=:expectedState",
            "#revision=:expectedRevision",
        ]
        if draft_fence is not None:
            names["#draftRevision"] = "draftRevision"
            values[":expectedDraftRevision"] = draft_fence
            conditions.append("#draftRevision=:expectedDraftRevision")
        assignments = [
            "#state=:targetState",
            "#revision=:nextRevision",
            "#updatedAt=:updatedAt",
            "#lastTransitionId=:transitionId",
        ]
        for index, (name, value) in enumerate(sorted(exact_updates.items())):
            name_token = f"#u{index}"
            value_token = f":u{index}"
            names[name_token] = name
            values[value_token] = value
            assignments.append(f"{name_token}={value_token}")
        try:
            response = self._table.update_item(
                Key=key,
                UpdateExpression="SET " + ", ".join(assignments),
                ConditionExpression=" AND ".join(conditions),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
            attributes = response.get("Attributes") if isinstance(response, Mapping) else None
            if not isinstance(attributes, Mapping):
                raise ActionRepositoryError("DynamoDB returned no updated action record")
            result = dict(attributes)
            self._assert_binding(result, action_id=action_id, user_id=user_id)
            if (
                result.get("state") != target_state.value
                or result.get("revision") != next_revision
                or result.get("lastTransitionId") != transition_id
                or any(
                    result.get(name) != value
                    for name, value in exact_updates.items()
                )
            ):
                raise ActionRepositoryError("DynamoDB returned an unexpected action revision")
            return result
        except Exception as error:
            current = self._read(action_id=action_id, user_id=user_id)
            if (
                current is not None
                and current.get("state") == target_state.value
                and current.get("revision") == next_revision
                and current.get("lastTransitionId") == transition_id
                and all(current.get(name) == value for name, value in exact_updates.items())
            ):
                return current
            raise ConcurrentActionUpdate(
                "action transition lost its revision fence or is unresolved"
            ) from error
