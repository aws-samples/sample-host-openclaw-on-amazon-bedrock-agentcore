"""Conditionally fenced action transitions and action-revision-bound approvals."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import secrets
from typing import Callable, Iterable, Mapping

try:
    from .models import (
        ActionState,
        CapabilityDenied,
        CapabilityGrant,
        canonical_args_hash,
        gmail_resource,
    )
except ImportError:
    from action_models import (
        ActionState,
        CapabilityDenied,
        CapabilityGrant,
        canonical_args_hash,
        gmail_resource,
    )


class IllegalTransition(ValueError):
    pass


class ConcurrentActionUpdate(RuntimeError):
    pass


class InvalidApprovalToken(ValueError):
    pass


_OPERATION_ID = re.compile(r"[A-Za-z0-9_-]{16,128}")

_LEGAL = {
    ActionState.PREPARED: {
        ActionState.APPROVAL_PENDING,
        ActionState.CANCELLED,
        ActionState.STALE,
    },
    ActionState.APPROVAL_PENDING: {
        ActionState.APPROVED,
        ActionState.REJECTED,
        ActionState.EXPIRED,
        ActionState.STALE,
        ActionState.CANCELLED,
    },
    ActionState.APPROVED: {
        ActionState.DISPATCHING,
        ActionState.EXPIRED,
        ActionState.STALE,
        ActionState.CANCELLED,
    },
    ActionState.DISPATCHING: {ActionState.CONFIRMED, ActionState.UNCERTAIN},
    ActionState.UNCERTAIN: {ActionState.CONFIRMED},
    ActionState.CONFIRMED: set(),
    ActionState.REJECTED: set(),
    ActionState.EXPIRED: set(),
    ActionState.STALE: set(),
    ActionState.CANCELLED: set(),
}


def assert_transition(current: ActionState, target: ActionState) -> ActionState:
    if not isinstance(current, ActionState) or not isinstance(target, ActionState):
        raise IllegalTransition("states must be ActionState values")
    if target not in _LEGAL[current]:
        raise IllegalTransition(f"illegal action transition {current.value}->{target.value}")
    return target


def _default_operation_id() -> str:
    return "op_" + secrets.token_urlsafe(24)


def _default_approval_id() -> str:
    return "appr_" + secrets.token_urlsafe(24)


class ActionStateMachine:
    def __init__(
        self,
        repository,
        *,
        operation_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._operation_id_factory = operation_id_factory or _default_operation_id

    def new_operation_id(self) -> str:
        value = self._operation_id_factory()
        if not isinstance(value, str) or _OPERATION_ID.fullmatch(value) is None:
            raise ValueError("operation ID factory returned an invalid identity")
        return value

    def transition(
        self,
        *,
        action_id: str,
        user_id: str,
        current: ActionState,
        target: ActionState,
        revision: int,
        updates: Mapping[str, object],
        operation_id: str | None = None,
    ):
        assert_transition(current, target)
        transition_id = operation_id or self.new_operation_id()
        if _OPERATION_ID.fullmatch(transition_id) is None:
            raise ValueError("operation_id is invalid")
        return self._repository.transition(
            action_id=action_id,
            user_id=user_id,
            expected_state=current,
            target_state=target,
            expected_revision=revision,
            transition_id=transition_id,
            updates=dict(updates),
        )

    def get(self, *, action_id: str, user_id: str):
        getter = getattr(self._repository, "get", None)
        if not callable(getter):
            raise ConcurrentActionUpdate("action repository cannot read exact state")
        return getter(action_id=action_id, user_id=user_id)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise InvalidApprovalToken("approval token encoding is invalid")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as error:
        raise InvalidApprovalToken("approval token encoding is invalid") from error
    if _b64(decoded) != value:
        raise InvalidApprovalToken("approval token encoding is noncanonical")
    return decoded


class ApprovalTokenCodec:
    def __init__(self, signing_key: bytes) -> None:
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise ValueError("approval signing key must contain at least 256 bits")
        self._key = signing_key

    def encode(self, grant: CapabilityGrant) -> str:
        if not isinstance(grant, CapabilityGrant):
            raise TypeError("approval token requires CapabilityGrant")
        payload = json.dumps(
            grant.token_record(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        encoded = _b64(payload)
        signature = _b64(hmac.new(self._key, encoded.encode("ascii"), hashlib.sha256).digest())
        return f"{encoded}.{signature}"

    def decode(self, token: str) -> CapabilityGrant:
        if not isinstance(token, str) or token.count(".") != 1 or len(token) > 4_096:
            raise InvalidApprovalToken("approval token has an invalid shape")
        encoded, signature = token.split(".")
        expected = hmac.new(self._key, encoded.encode("ascii"), hashlib.sha256).digest()
        supplied = _unb64(signature)
        if not hmac.compare_digest(expected, supplied):
            raise InvalidApprovalToken("approval token signature is invalid")
        try:
            record = json.loads(_unb64(encoded))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise InvalidApprovalToken("approval token payload is invalid") from error
        required = {
            "actionId",
            "approvalId",
            "argsHash",
            "capability",
            "draftRevision",
            "expiresAt",
            "resource",
            "userId",
        }
        if not isinstance(record, dict) or set(record) != required:
            raise InvalidApprovalToken("approval token fields are invalid")
        try:
            return CapabilityGrant(
                action_id=record["actionId"],
                draft_revision=record["draftRevision"],
                user_id=record["userId"],
                capability=record["capability"],
                resource=record["resource"],
                args_hash=record["argsHash"],
                expires_at=datetime.fromisoformat(record["expiresAt"]),
                approval_id=record["approvalId"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidApprovalToken("approval token grant is invalid") from error


class ApprovalService:
    def __init__(
        self,
        *,
        state_machine: ActionStateMachine,
        token_codec: ApprovalTokenCodec,
        founder_user_ids: Iterable[str],
        now: Callable[[], datetime] | None = None,
        approval_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._machine = state_machine
        self._codec = token_codec
        self._founders = frozenset(founder_user_ids)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._approval_id_factory = approval_id_factory or _default_approval_id

    @staticmethod
    def _exact_time(value: object, label: str) -> datetime:
        try:
            parsed = (
                value
                if isinstance(value, datetime)
                else datetime.fromisoformat(value) if isinstance(value, str) else None
            )
        except ValueError as error:
            raise CapabilityDenied(f"{label} is invalid") from error
        if not isinstance(parsed, datetime) or parsed.tzinfo is None:
            raise CapabilityDenied(f"{label} is invalid")
        return parsed.astimezone(timezone.utc)

    def _founder(self, user_id: str) -> None:
        if user_id not in self._founders:
            raise CapabilityDenied("email effects are founder-only")

    def _record(
        self,
        *,
        action_id: str,
        user_id: str,
        state: ActionState,
        revision: int,
    ) -> Mapping[str, object]:
        record = self._machine.get(action_id=action_id, user_id=user_id)
        if not isinstance(record, Mapping):
            raise ConcurrentActionUpdate("action does not exist")
        if (
            record.get("actionId") != action_id
            or record.get("userId") != user_id
            or record.get("state") != state.value
            or isinstance(record.get("revision"), bool)
            or not isinstance(record.get("revision"), int)
            or record.get("revision") != revision
        ):
            raise ConcurrentActionUpdate("action state or revision was already consumed")
        return record

    @staticmethod
    def _assert_exact_action(
        record: Mapping[str, object],
        *,
        action_id: str,
        user_id: str,
        args: Mapping[str, object],
    ) -> tuple[str, int, str]:
        exact_hash = canonical_args_hash(args)
        draft_revision = record.get("draftRevision")
        connection_id = record.get("connectionId")
        account_email = record.get("accountEmail")
        sender_address = record.get("senderAddress")
        try:
            expected_resource = gmail_resource(
                connection_id=connection_id, account_email=account_email
            )
        except (TypeError, ValueError) as error:
            raise CapabilityDenied("action has no exact Google account binding") from error
        if (
            record.get("actionId") != action_id
            or record.get("userId") != user_id
            or isinstance(draft_revision, bool)
            or not isinstance(draft_revision, int)
            or draft_revision < 1
            or sender_address != account_email
            or record.get("capability") != "gmail.send"
            or record.get("resource") != expected_resource
            or record.get("args") != dict(args)
            or record.get("payloadHash") != exact_hash
        ):
            raise CapabilityDenied("approval does not match the persisted exact action revision")
        return exact_hash, draft_revision, expected_resource

    def _new_approval_id(self) -> str:
        value = self._approval_id_factory()
        if not isinstance(value, str) or _OPERATION_ID.fullmatch(value) is None:
            raise ValueError("approval ID factory returned an invalid identity")
        return value

    def decode(self, token: str) -> CapabilityGrant:
        return self._codec.decode(token)

    def request_approval(
        self,
        *,
        action_id: str,
        revision: int,
        acting_user_id: str,
        args: Mapping[str, object],
        expires_at: datetime,
    ) -> str:
        self._founder(acting_user_id)
        record = self._record(
            action_id=action_id,
            user_id=acting_user_id,
            state=ActionState.PREPARED,
            revision=revision,
        )
        args_hash, draft_revision, resource = self._assert_exact_action(
            record,
            action_id=action_id,
            user_id=acting_user_id,
            args=args,
        )
        expiry = self._exact_time(expires_at, "expires_at")
        now = self._exact_time(self._now(), "now")
        if expiry < now:
            raise CapabilityDenied("approval request already expired")
        grant = CapabilityGrant(
            action_id=action_id,
            draft_revision=draft_revision,
            user_id=acting_user_id,
            capability="gmail.send",
            resource=resource,
            args_hash=args_hash,
            expires_at=expiry,
            approval_id=self._new_approval_id(),
        )
        self._machine.transition(
            action_id=action_id,
            user_id=acting_user_id,
            current=ActionState.PREPARED,
            target=ActionState.APPROVAL_PENDING,
            revision=revision,
            updates={
                "approvalId": grant.approval_id,
                "approvalActionId": action_id,
                "approvalDraftRevision": draft_revision,
                "approvalArgsHash": grant.args_hash,
                "approvalExpiresAt": grant.expires_at.isoformat(),
                "approvalRequestedAt": now.isoformat(),
            },
        )
        return self._codec.encode(grant)

    def approve(
        self,
        *,
        action_id: str,
        revision: int,
        acting_user_id: str,
        token: str,
        args: Mapping[str, object],
    ):
        self._founder(acting_user_id)
        record = self._record(
            action_id=action_id,
            user_id=acting_user_id,
            state=ActionState.APPROVAL_PENDING,
            revision=revision,
        )
        args_hash, draft_revision, resource = self._assert_exact_action(
            record,
            action_id=action_id,
            user_id=acting_user_id,
            args=args,
        )
        grant = self._codec.decode(token)
        pending_expiry = self._exact_time(
            record.get("approvalExpiresAt"), "approvalExpiresAt"
        )
        now = self._exact_time(self._now(), "now")
        try:
            if (
                record.get("approvalActionId") != action_id
                or record.get("approvalDraftRevision") != draft_revision
                or not hmac.compare_digest(str(record.get("approvalId", "")), grant.approval_id)
                or not hmac.compare_digest(str(record.get("approvalArgsHash", "")), grant.args_hash)
                or pending_expiry != grant.expires_at
                or not hmac.compare_digest(args_hash, grant.args_hash)
            ):
                raise CapabilityDenied("approval token does not belong to this action revision")
            grant.assert_authorized(
                action_id=action_id,
                draft_revision=draft_revision,
                user_id=acting_user_id,
                capability="gmail.send",
                resource=resource,
                args=args,
                now=now,
            )
        except CapabilityDenied:
            if now >= pending_expiry:
                try:
                    self._machine.transition(
                        action_id=action_id,
                        user_id=acting_user_id,
                        current=ActionState.APPROVAL_PENDING,
                        target=ActionState.EXPIRED,
                        revision=revision,
                        updates={"expiredAt": now.isoformat()},
                    )
                except Exception:
                    pass
            raise
        return self._machine.transition(
            action_id=action_id,
            user_id=acting_user_id,
            current=ActionState.APPROVAL_PENDING,
            target=ActionState.APPROVED,
            revision=revision,
            updates={
                "approvalId": grant.approval_id,
                "approvedActionId": action_id,
                "approvedDraftRevision": draft_revision,
                "approvedArgsHash": grant.args_hash,
                "approvedAt": now.isoformat(),
            },
        )

    def reject(self, *, action_id: str, revision: int, acting_user_id: str):
        self._founder(acting_user_id)
        self._record(
            action_id=action_id,
            user_id=acting_user_id,
            state=ActionState.APPROVAL_PENDING,
            revision=revision,
        )
        now = self._exact_time(self._now(), "now")
        return self._machine.transition(
            action_id=action_id,
            user_id=acting_user_id,
            current=ActionState.APPROVAL_PENDING,
            target=ActionState.REJECTED,
            revision=revision,
            updates={"rejectedAt": now.isoformat()},
        )

    def expire(self, *, action_id: str, revision: int, user_id: str):
        record = self._record(
            action_id=action_id,
            user_id=user_id,
            state=ActionState.APPROVAL_PENDING,
            revision=revision,
        )
        now = self._exact_time(self._now(), "now")
        expiry = self._exact_time(record.get("approvalExpiresAt"), "approvalExpiresAt")
        if now < expiry:
            raise CapabilityDenied("approval has not expired")
        return self._machine.transition(
            action_id=action_id,
            user_id=user_id,
            current=ActionState.APPROVAL_PENDING,
            target=ActionState.EXPIRED,
            revision=revision,
            updates={"expiredAt": now.isoformat()},
        )

    def mark_stale(
        self,
        *,
        action_id: str,
        revision: int,
        user_id: str,
        expected_draft_revision: int,
        current_draft_revision: int,
    ):
        record = self._machine.get(action_id=action_id, user_id=user_id)
        if (
            not isinstance(record, Mapping)
            or record.get("revision") != revision
            or record.get("draftRevision") != expected_draft_revision
            or isinstance(current_draft_revision, bool)
            or not isinstance(current_draft_revision, int)
            or current_draft_revision <= expected_draft_revision
        ):
            raise ConcurrentActionUpdate("draft revision is not the expected stale boundary")
        state = ActionState(record["state"])
        if state not in {
            ActionState.PREPARED,
            ActionState.APPROVAL_PENDING,
            ActionState.APPROVED,
        }:
            raise IllegalTransition("action can no longer be marked stale")
        now = self._exact_time(self._now(), "now")
        return self._machine.transition(
            action_id=action_id,
            user_id=user_id,
            current=state,
            target=ActionState.STALE,
            revision=revision,
            updates={
                "staleAt": now.isoformat(),
                "staleReason": "newer-draft-revision",
                "staleDraftRevision": expected_draft_revision,
                "supersededByDraftRevision": current_draft_revision,
            },
        )
