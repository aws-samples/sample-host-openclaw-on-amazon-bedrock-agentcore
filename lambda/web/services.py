"""Trusted web-facing action and workspace services.

The HTTP boundary deliberately delegates all security-sensitive reads here.
Approval preview is a read-only operation: it decodes a signed grant and
performs one exact, strongly consistent action read through the injected
repository.  Only a later CSRF-protected POST may transition or execute.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
from pathlib import PurePosixPath
import re
from typing import Callable, Iterable, Mapping, TypeVar

from actions.models import (
    ActionState,
    CapabilityDenied,
    EffectReceipt,
    canonical_args_hash,
    gmail_resource,
)
from actions.gmail_send import validate_email_args
from .retention import (
    DeletionPending,
    DynamoExpirySweeper,
    DynamoSweepCursorStore,
    SweepCursorLease,
    _sweep_cursor,
)


_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_ACTION_ID = re.compile(r"[A-Za-z0-9_-]{8,128}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GENERATION = re.compile(
    r"g-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_RUNTIME_STATES = frozenset(
    {
        "COLD",
        "STARTING",
        "READY",
        "BUSY",
        "IDLE",
        "UNHEALTHY",
        "QUARANTINED",
        "DELETING",
    }
)
_MAX_WORKSPACE_FILES = 1_000
_INACTIVE_RETENTION_SECONDS = 30 * 24 * 60 * 60
_T = TypeVar("_T")


def _user_id(value: object) -> str:
    if not isinstance(value, str) or _USER_ID.fullmatch(value) is None:
        raise ValueError("user identity is invalid")
    return value


def _action_id(value: object) -> str:
    if not isinstance(value, str) or _ACTION_ID.fullmatch(value) is None:
        raise ValueError("action identity is invalid")
    return value


def _revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("action revision is invalid")
    return value


def _time(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError as error:
        raise CapabilityDenied(f"{label} is invalid") from error
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise CapabilityDenied(f"{label} is invalid")
    return parsed.astimezone(timezone.utc)


def _same(left: object, right: object) -> bool:
    return hmac.compare_digest(str(left), str(right))


class ApprovalWebService:
    """Bind browser approval operations to one persisted action revision."""

    def __init__(
        self,
        *,
        approval_service,
        action_reader,
        executor_factory: Callable[[Mapping[str, object]], object],
        revocation_kernel=None,
        founder_user_ids: Iterable[str],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        founders = frozenset(_user_id(value) for value in founder_user_ids)
        if not callable(executor_factory):
            raise TypeError("executor_factory must be callable")
        self._approvals = approval_service
        self._actions = action_reader
        self._executor_factory = executor_factory
        if revocation_kernel is not None and not callable(
            getattr(revocation_kernel, "revoke", None)
        ):
            raise TypeError("revocation kernel is invalid")
        self._revocations = revocation_kernel
        self._founders = founders
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _founder(self, user_id: object) -> str:
        user_id = _user_id(user_id)
        if user_id not in self._founders:
            raise CapabilityDenied("email effects are founder-only")
        return user_id

    @staticmethod
    def _record(
        value: object,
        *,
        action_id: str,
        user_id: str,
        revision: int | None = None,
    ) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise CapabilityDenied("approval action does not exist")
        record = dict(value)
        record_revision = record.get("revision")
        if (
            record.get("actionId") != action_id
            or record.get("userId") != user_id
            or record.get("state") != ActionState.APPROVAL_PENDING.value
            or isinstance(record_revision, bool)
            or not isinstance(record_revision, int)
            or record_revision < 1
            or (revision is not None and record_revision != revision)
        ):
            raise CapabilityDenied(
                "approval does not reference the exact pending action revision"
            )
        return record

    def _pending(
        self,
        *,
        token: str,
        acting_user_id: str,
        action_id: str | None = None,
        revision: int | None = None,
        args: Mapping[str, object] | None = None,
    ) -> tuple[dict[str, object], object]:
        user_id = self._founder(acting_user_id)
        if action_id is not None:
            action_id = _action_id(action_id)
        if revision is not None:
            revision = _revision(revision)
        grant = self._approvals.decode(token)
        grant_action = _action_id(getattr(grant, "action_id", None))
        grant_user = _user_id(getattr(grant, "user_id", None))
        if (
            grant_user != user_id
            or (action_id is not None and grant_action != action_id)
        ):
            raise CapabilityDenied("approval token belongs to another authority")
        record = self._record(
            self._actions.get(action_id=grant_action, user_id=user_id),
            action_id=grant_action,
            user_id=user_id,
            revision=revision,
        )
        record_args = record.get("args")
        if not isinstance(record_args, Mapping):
            raise CapabilityDenied("approval action payload is invalid")
        try:
            exact_args = validate_email_args(record_args)
        except (TypeError, ValueError) as error:
            raise CapabilityDenied("approval action payload is invalid") from error
        exact_hash = canonical_args_hash(exact_args)
        draft_revision = record.get("draftRevision")
        connection_id = record.get("connectionId")
        account_email = record.get("accountEmail")
        try:
            resource = gmail_resource(
                connection_id=connection_id,
                account_email=account_email,
            )
        except (TypeError, ValueError) as error:
            raise CapabilityDenied("approval account binding is invalid") from error
        pending_expiry = _time(record.get("approvalExpiresAt"), "approval expiry")
        now = _time(self._now(), "approval clock")
        if (
            isinstance(draft_revision, bool)
            or not isinstance(draft_revision, int)
            or draft_revision < 1
            or record.get("senderAddress") != account_email
            or record.get("capability") != "gmail.send"
            or not _same(record.get("resource"), resource)
            or not _same(record.get("payloadHash"), exact_hash)
            or not _same(record.get("approvalActionId"), grant_action)
            or record.get("approvalDraftRevision") != draft_revision
            or not _same(record.get("approvalId"), getattr(grant, "approval_id", None))
            or not _same(
                record.get("approvalArgsHash"), getattr(grant, "args_hash", None)
            )
            or not _same(record.get("approvalArgsHash"), exact_hash)
            or draft_revision != getattr(grant, "draft_revision", None)
            or record.get("capability") != getattr(grant, "capability", None)
            or not _same(resource, getattr(grant, "resource", None))
            or pending_expiry != getattr(grant, "expires_at", None)
        ):
            raise CapabilityDenied(
                "approval token does not match the persisted exact action revision"
            )
        if args is not None:
            if not isinstance(args, Mapping) or dict(args) != exact_args:
                raise CapabilityDenied("approval payload does not match the pending draft")
        grant.assert_authorized(
            action_id=grant_action,
            draft_revision=draft_revision,
            user_id=user_id,
            capability="gmail.send",
            resource=resource,
            args=exact_args,
            now=now,
        )
        return record, grant

    def preview(self, *, token: str, acting_user_id: str) -> dict[str, object]:
        record, grant = self._pending(
            token=token,
            acting_user_id=acting_user_id,
        )
        return {
            "actionId": grant.action_id,
            "userId": grant.user_id,
            "state": ActionState.APPROVAL_PENDING.value,
            "revision": record["revision"],
            "draftRevision": record["draftRevision"],
            "args": dict(record["args"]),
            "payloadHash": record["payloadHash"],
            "expiresAt": grant.expires_at.isoformat(),
        }

    def approve(
        self,
        *,
        action_id: str,
        revision: int,
        acting_user_id: str,
        token: str,
        args: Mapping[str, object],
    ) -> dict[str, object]:
        record, _ = self._pending(
            token=token,
            acting_user_id=acting_user_id,
            action_id=action_id,
            revision=revision,
            args=args,
        )
        # Resolve and validate the exact send connection before consuming the
        # approval transition. This function must not perform the effect.
        dispatcher = self._executor_factory(record)
        dispatch = getattr(dispatcher, "dispatch", None)
        if not callable(dispatch):
            raise TypeError("executor factory returned an invalid connector dispatcher")
        approved = self._approvals.approve(
            action_id=action_id,
            revision=revision,
            acting_user_id=acting_user_id,
            token=token,
            args=args,
        )
        if (
            not isinstance(approved, Mapping)
            or approved.get("actionId") != action_id
            or approved.get("userId") != acting_user_id
            or approved.get("state") != ActionState.APPROVED.value
        ):
            raise RuntimeError("approval transition returned an invalid action")
        receipt = dispatch(approved)
        receipt_record = getattr(receipt, "record", None)
        if not callable(receipt_record):
            raise RuntimeError("Gmail execution returned no effect receipt")
        exact_receipt = receipt_record()
        if not isinstance(exact_receipt, Mapping):
            raise RuntimeError("Gmail execution receipt is invalid")
        try:
            exact_receipt = EffectReceipt.from_record(exact_receipt).record()
        except (TypeError, ValueError) as error:
            raise RuntimeError("Gmail execution receipt is invalid") from error
        final = self._actions.get(action_id=action_id, user_id=acting_user_id)
        if (
            not isinstance(final, Mapping)
            or final.get("actionId") != action_id
            or final.get("userId") != acting_user_id
            or final.get("state") != ActionState.CONFIRMED.value
            or final.get("effectReceipt") != dict(exact_receipt)
        ):
            raise RuntimeError("confirmed Gmail action could not be strongly read")
        return {
            "actionId": action_id,
            "userId": acting_user_id,
            "state": ActionState.CONFIRMED.value,
            "revision": _revision(final.get("revision")),
            "receipt": dict(exact_receipt),
        }

    def reject(
        self,
        *,
        action_id: str,
        revision: int,
        acting_user_id: str,
    ) -> dict[str, object]:
        user_id = self._founder(acting_user_id)
        action_id = _action_id(action_id)
        revision = _revision(revision)
        self._record(
            self._actions.get(action_id=action_id, user_id=user_id),
            action_id=action_id,
            user_id=user_id,
            revision=revision,
        )
        rejected = self._approvals.reject(
            action_id=action_id,
            revision=revision,
            acting_user_id=user_id,
        )
        if (
            not isinstance(rejected, Mapping)
            or rejected.get("actionId") != action_id
            or rejected.get("userId") != user_id
            or rejected.get("state") != ActionState.REJECTED.value
        ):
            raise RuntimeError("rejection transition returned an invalid action")
        return {
            "actionId": action_id,
            "userId": user_id,
            "state": ActionState.REJECTED.value,
            "revision": _revision(rejected.get("revision")),
        }

    @staticmethod
    def _revocation_operation_id(
        *, action_id: str, user_id: str, revision: int, connection_ref: str
    ) -> str:
        digest = hashlib.sha256(
            (
                "personal-operator-web-approval-revocation-v1\0"
                f"{user_id}\0{action_id}\0{revision}\0{connection_ref}"
            ).encode("utf-8")
        ).hexdigest()[:32]
        return f"web_revoke_{digest}"

    def revoke(
        self,
        *,
        action_id: str,
        revision: int,
        acting_user_id: str,
    ) -> dict[str, object]:
        """Revoke one exact action through the production connector kernel."""

        user_id = self._founder(acting_user_id)
        action_id = _action_id(action_id)
        revision = _revision(revision)
        record = self._actions.get(action_id=action_id, user_id=user_id)
        if not isinstance(record, Mapping):
            raise CapabilityDenied("revocable action does not exist")
        record = dict(record)
        state = record.get("state")
        record_revision = record.get("revision")
        if (
            record.get("actionId") != action_id
            or record.get("userId") != user_id
            or state
            not in {
                ActionState.PREPARED.value,
                ActionState.APPROVAL_PENDING.value,
                ActionState.APPROVED.value,
                ActionState.CANCELLED.value,
            }
            or isinstance(record_revision, bool)
            or not isinstance(record_revision, int)
            or (
                state != ActionState.CANCELLED.value
                and record_revision != revision
            )
            or (
                state == ActionState.CANCELLED.value
                and record_revision != revision + 1
            )
        ):
            raise CapabilityDenied(
                "revocation does not reference the exact action generation"
            )
        args = record.get("args")
        try:
            exact_args = validate_email_args(args)
            exact_hash = canonical_args_hash(exact_args)
            connection_ref = record.get("connectionId")
            account_email = record.get("accountEmail")
            resource = gmail_resource(
                connection_id=connection_ref,
                account_email=account_email,
            )
        except (TypeError, ValueError) as error:
            raise CapabilityDenied("revocable action binding is invalid") from error
        if (
            record.get("senderAddress") != account_email
            or record.get("capability") != "gmail.send"
            or not _same(record.get("resource"), resource)
            or not _same(record.get("payloadHash"), exact_hash)
        ):
            raise CapabilityDenied("revocable action binding is invalid")
        revoke = getattr(self._revocations, "revoke", None)
        if not callable(revoke):
            raise RuntimeError("approval revocation kernel is unavailable")
        operation_id = self._revocation_operation_id(
            action_id=action_id,
            user_id=user_id,
            revision=revision,
            connection_ref=connection_ref,
        )
        revoked = revoke(
            connection_ref,
            action_id=action_id,
            user_id=user_id,
            revision=revision,
            operation_id=operation_id,
        )
        if (
            not isinstance(revoked, Mapping)
            or revoked.get("actionId") != action_id
            or revoked.get("userId") != user_id
            or revoked.get("connectionId") != connection_ref
            or revoked.get("state") != ActionState.CANCELLED.value
            or revoked.get("revision") != revision + 1
            or revoked.get("lastTransitionId") != operation_id
            or revoked.get("cancellationReason") != "approval-revoked"
        ):
            raise RuntimeError("approval revocation outcome is unproven")
        return {
            "actionId": action_id,
            "userId": user_id,
            "state": ActionState.CANCELLED.value,
            "revision": revision + 1,
        }


def _workspace_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or "\\" in value
    ):
        raise ValueError("workspace path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(
            part in {"", ".", ".."} or part.startswith(".")
            for part in path.parts
        )
        or path.as_posix() != value
    ):
        raise ValueError("workspace path is invalid")
    return value


class WorkspaceService:
    """Return a bounded, credential-free view of one user's workspace."""

    def __init__(self, *, workspace_store, runtime_driver) -> None:
        self._workspace = workspace_store
        self._runtime = runtime_driver

    def get(self, user_id: str) -> dict[str, object]:
        user_id = _user_id(user_id)
        files = self._workspace.workspace_files(user_id)
        status = self._runtime.status(user_id)
        if not isinstance(files, Mapping) or len(files) > _MAX_WORKSPACE_FILES:
            raise RuntimeError("workspace listing is invalid")
        if (
            not isinstance(status, Mapping)
            or status.get("userId") != user_id
            or status.get("state") not in _RUNTIME_STATES
        ):
            raise RuntimeError("runtime status belongs to another user or is invalid")
        result_files = []
        validated_files: dict[str, bytes] = {}
        for path, content in files.items():
            path = _workspace_path(path)
            if not isinstance(content, bytes):
                raise RuntimeError("workspace listing returned non-bytes content")
            if path in validated_files:
                raise RuntimeError("workspace listing returned duplicate paths")
            validated_files[path] = content
        for path in sorted(validated_files):
            content = validated_files[path]
            result_files.append({"path": path, "size": len(content)})
        receipt = status.get("workspaceReceipt")
        if receipt is not None:
            if (
                not isinstance(receipt, Mapping)
                or set(receipt) != {"generation", "manifestSha256"}
                or not isinstance(receipt.get("generation"), str)
                or _GENERATION.fullmatch(receipt["generation"]) is None
                or not isinstance(receipt.get("manifestSha256"), str)
                or _SHA256.fullmatch(receipt["manifestSha256"]) is None
            ):
                raise RuntimeError("runtime workspace receipt is invalid")
            receipt = dict(receipt)
        return {
            "userId": user_id,
            "runtimeState": status["state"],
            "workspaceReceipt": receipt,
            "files": result_files,
        }


class RetentionSweepService:
    """Bounded Dynamo expiry plus resumable account-deletion reconciliation."""

    def __init__(
        self,
        *,
        control_table,
        runtime_table,
        deletion,
        action_maintenance=None,
        now: Callable[[], object] | None = None,
        max_deletions: int = 25,
        max_scan_pages: int = 40,
        cursor_store=None,
    ) -> None:
        if (
            isinstance(max_deletions, bool)
            or not isinstance(max_deletions, int)
            or not 1 <= max_deletions <= 100
        ):
            raise ValueError("retention sweep bounds are invalid")
        if (
            isinstance(max_scan_pages, bool)
            or not isinstance(max_scan_pages, int)
            or not 1 <= max_scan_pages <= 40
        ):
            raise ValueError("retention scan page bound is invalid")
        if cursor_store is None:
            cursor_store = DynamoSweepCursorStore(control_table)
        if not callable(getattr(cursor_store, "load", None)) or not callable(
            getattr(cursor_store, "save", None)
        ):
            raise ValueError("retention sweep cursor store is invalid")
        self._control = control_table
        self._runtime = runtime_table
        self._deletion = deletion
        if action_maintenance is not None and not callable(
            getattr(action_maintenance, "run", None)
        ):
            raise ValueError("action maintenance runner is invalid")
        self._action_maintenance = action_maintenance
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._max_deletions = max_deletions
        self._max_scan_pages = max_scan_pages
        self._cursors = cursor_store
        self._expiry = DynamoExpirySweeper(
            control_table,
            now=self._epoch,
            cursor_store=cursor_store,
        )

    def _epoch(self) -> int:
        value = self._now()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise RuntimeError("retention clock must be timezone-aware")
            value = int(value.astimezone(timezone.utc).timestamp())
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuntimeError("retention clock is invalid")
        return value

    def _scan_pages(
        self,
        table,
        request: Mapping[str, object],
        *,
        cursor_name: str,
        cursor_fields: tuple[str, ...],
        decode_item: Callable[[Mapping[str, object]], _T],
    ) -> list[_T]:
        lease = self._cursors.load(cursor_name)
        if (
            not isinstance(lease, SweepCursorLease)
            or lease.name != cursor_name
        ):
            raise RuntimeError("deletion reconciliation cursor lease is invalid")
        start_key = lease.cursor
        selected: list[_T] = []
        matched = 0
        seen_cursors: set[tuple[object, ...]] = set()
        if start_key is not None:
            seen_cursors.add(tuple(start_key[field] for field in cursor_fields))
        for _page in range(self._max_scan_pages):
            remaining = self._max_deletions - matched
            if remaining <= 0:
                return selected
            page_request = dict(request)
            requested_limit = page_request.get("Limit")
            if (
                isinstance(requested_limit, bool)
                or not isinstance(requested_limit, int)
                or requested_limit <= 0
            ):
                raise RuntimeError("deletion reconciliation scan is invalid")
            page_request["Limit"] = min(requested_limit, remaining)
            if start_key is not None:
                page_request["ExclusiveStartKey"] = start_key
            response = table.scan(**page_request)
            items = response.get("Items") if isinstance(response, Mapping) else None
            if (
                not isinstance(items, list)
                or len(items) > page_request["Limit"]
                or any(not isinstance(item, Mapping) for item in items)
            ):
                raise RuntimeError("deletion reconciliation scan is invalid")
            decoded = [decode_item(item) for item in items]
            try:
                cursor = _sweep_cursor(
                    cursor_name,
                    response.get("LastEvaluatedKey"),
                )
            except RuntimeError as error:
                raise RuntimeError(
                    "deletion reconciliation pagination is invalid"
                ) from error
            if cursor is not None:
                signature = tuple(cursor[field] for field in cursor_fields)
                if signature in seen_cursors:
                    raise RuntimeError(
                        "deletion reconciliation pagination is invalid"
                    )
                seen_cursors.add(signature)
            matched += len(items)
            selected.extend(decoded)
            if cursor != start_key or start_key is not None:
                lease = self._cursors.save(lease, cursor)
                if (
                    not isinstance(lease, SweepCursorLease)
                    or lease.name != cursor_name
                    or lease.cursor != cursor
                ):
                    raise RuntimeError(
                        "deletion reconciliation cursor lease is invalid"
                    )
            if cursor is None or matched >= self._max_deletions:
                return selected
            start_key = cursor
        return selected

    @staticmethod
    def _decode_intent(item: Mapping[str, object]) -> str | None:
        pk = item.get("PK")
        status = item.get("deletionStatus")
        requested_at = item.get("requestedAt")
        finalizing_at = item.get("finalizingAt")
        completed_at = item.get("completedAt")
        try:
            user_id = _user_id(item.get("userId"))
        except ValueError as error:
            raise RuntimeError("deletion intent scan is invalid") from error
        if (
            item.get("SK") != "DELETION"
            or not isinstance(pk, str)
            or re.fullmatch(r"DELETION#[0-9a-f]{64}", pk) is None
            or item.get("recordType") != "DELETION_INTENT"
            or item.get("purgeReason") != "ACCOUNT_DELETION"
            or status not in {"PENDING", "FINALIZING", "COMPLETED"}
            or isinstance(requested_at, bool)
            or not isinstance(requested_at, int)
            or requested_at <= 0
        ):
            raise RuntimeError("deletion intent scan is invalid")
        if status == "PENDING":
            if finalizing_at is not None or completed_at is not None:
                raise RuntimeError("deletion intent scan is invalid")
            return user_id
        if (
            isinstance(finalizing_at, bool)
            or not isinstance(finalizing_at, int)
            or finalizing_at < requested_at
            or (
                status == "FINALIZING"
                and completed_at is not None
            )
        ):
            raise RuntimeError("deletion intent scan is invalid")
        if status == "FINALIZING":
            return user_id
        if (
            isinstance(completed_at, bool)
            or not isinstance(completed_at, int)
            or completed_at < finalizing_at
        ):
            raise RuntimeError("deletion intent scan is invalid")
        return None

    def _pending_intents(self) -> list[str]:
        decoded = self._scan_pages(
            self._control,
            {
                "FilterExpression": (
                    "#recordType=:intent AND "
                    "(deletionStatus=:pending OR deletionStatus=:finalizing)"
                ),
                "ProjectionExpression": (
                    "PK, SK, #recordType, userId, purgeReason, deletionStatus, "
                    "requestedAt, finalizingAt, completedAt"
                ),
                "ExpressionAttributeNames": {"#recordType": "recordType"},
                "ExpressionAttributeValues": {
                    ":intent": "DELETION_INTENT",
                    ":pending": "PENDING",
                    ":finalizing": "FINALIZING",
                },
                "ConsistentRead": True,
                "Limit": self._max_deletions,
            },
            cursor_name="deletion-intents",
            cursor_fields=("PK", "SK"),
            decode_item=self._decode_intent,
        )
        users = [user_id for user_id in decoded if user_id is not None]
        if len(set(users)) != len(users):
            raise RuntimeError("deletion intent scan returned duplicate users")
        return users

    @staticmethod
    def _decode_runtime_candidate(
        item: Mapping[str, object],
        *,
        inactive_before_ms: int,
    ) -> tuple[str, str, int, int, int] | None:
        try:
            user_id = _user_id(item.get("userId"))
        except ValueError as error:
            raise RuntimeError(
                "deletion reconciliation scan is invalid"
            ) from error
        state = item.get("state")
        if state == "DELETING" and "tombstonedAt" in item:
            tombstoned_at = item.get("tombstonedAt")
            completed_at = item.get("purgeCompletedAt")
            if (
                isinstance(tombstoned_at, bool)
                or not isinstance(tombstoned_at, int)
                or tombstoned_at <= 0
            ):
                raise RuntimeError("deletion reconciliation scan is invalid")
            reason = item.get("purgeReason")
            if reason not in {"ACCOUNT_DELETION", "WORKSPACE_EXPIRY"}:
                raise RuntimeError("deletion reconciliation scan is invalid")
            if completed_at is not None:
                if (
                    isinstance(completed_at, bool)
                    or not isinstance(completed_at, int)
                    or completed_at <= 0
                ):
                    raise RuntimeError("deletion reconciliation scan is invalid")
                return None
            if reason == "ACCOUNT_DELETION":
                return ("account", user_id, 0, 0, 0)
            observed_at = item.get("purgeObservedUpdatedAt")
            observed_revision = item.get("purgeObservedRevision")
            observed_cutoff = item.get("purgeInactiveBefore")
            if (
                isinstance(observed_at, bool)
                or not isinstance(observed_at, int)
                or observed_at <= 0
                or isinstance(observed_revision, bool)
                or not isinstance(observed_revision, int)
                or observed_revision < 1
                or isinstance(observed_cutoff, bool)
                or not isinstance(observed_cutoff, int)
                or observed_cutoff <= 0
                or observed_at > observed_cutoff
            ):
                raise RuntimeError("deletion reconciliation scan is invalid")
            return (
                "workspace",
                user_id,
                observed_at,
                observed_revision,
                observed_cutoff,
            )
        updated_at = item.get("updatedAt")
        revision = item.get("revision")
        if (
            state not in _RUNTIME_STATES
            or state == "DELETING"
            or "tombstonedAt" in item
            or item.get("purgeReason") is not None
            or item.get("purgeCompletedAt") is not None
            or isinstance(updated_at, bool)
            or not isinstance(updated_at, int)
            or updated_at <= 0
            or updated_at > inactive_before_ms
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise RuntimeError("deletion reconciliation scan is invalid")
        return (
            "workspace",
            user_id,
            updated_at,
            revision,
            inactive_before_ms,
        )

    def _runtime_candidates(
        self, *, inactive_before_ms: int
    ) -> tuple[list[str], list[tuple[str, int, int, int]]]:
        decoded = self._scan_pages(
            self._runtime,
            {
                "FilterExpression": (
                    "(#state=:deleting AND attribute_exists(tombstonedAt) AND "
                    "attribute_not_exists(purgeCompletedAt)) OR "
                    "(#updatedAt <= :inactiveBefore AND "
                    "attribute_not_exists(tombstonedAt) AND #state <> :deleting)"
                ),
                "ProjectionExpression": (
                    "userId, #state, tombstonedAt, #updatedAt, revision, "
                    "purgeReason, purgeCompletedAt, purgeObservedUpdatedAt, "
                    "purgeObservedRevision, purgeInactiveBefore"
                ),
                "ExpressionAttributeNames": {
                    "#state": "state",
                    "#updatedAt": "updatedAt",
                },
                "ExpressionAttributeValues": {
                    ":deleting": "DELETING",
                    ":inactiveBefore": inactive_before_ms,
                },
                "ConsistentRead": True,
                "Limit": self._max_deletions,
            },
            cursor_name="runtime-candidates",
            cursor_fields=("userId",),
            decode_item=lambda item: self._decode_runtime_candidate(
                item,
                inactive_before_ms=inactive_before_ms,
            ),
        )
        account: list[str] = []
        workspace: list[tuple[str, int, int, int]] = []
        users: list[str] = []
        for candidate in decoded:
            if candidate is None:
                continue
            kind, user_id, updated_at, revision, observed_cutoff = candidate
            users.append(user_id)
            if kind == "account":
                account.append(user_id)
            elif kind == "workspace":
                workspace.append(
                    (user_id, updated_at, revision, observed_cutoff)
                )
            else:
                raise RuntimeError("deletion reconciliation scan is invalid")
        if len(set(users)) != len(users):
            raise RuntimeError("deletion reconciliation returned duplicate users")
        return account, workspace

    def sweep(self) -> dict[str, object]:
        epoch_seconds = self._epoch()
        inactive_before_ms = max(
            0, (epoch_seconds - _INACTIVE_RETENTION_SECONDS) * 1_000
        )
        completed = 0
        pending = 0
        account_users: set[str] = set()

        def reconcile_accounts(users: Iterable[str]) -> None:
            nonlocal completed, pending
            for user_id in sorted(set(users) - account_users):
                account_users.add(user_id)
                try:
                    outcome = self._deletion.reconcile(user_id)
                    if not isinstance(outcome, Mapping):
                        raise RuntimeError(
                            "deletion reconciliation returned invalid data"
                        )
                    if outcome == {"status": "deleted", "userId": user_id}:
                        completed += 1
                    elif outcome == {"status": "pending", "userId": user_id}:
                        pending += 1
                    else:
                        raise RuntimeError(
                            "deletion reconciliation returned invalid data"
                        )
                except DeletionPending:
                    pending += 1

        # Explicit account intents are the highest-priority durable authority
        # fence. Advance them before runtime scans, expiry rows, or provider
        # reconciliation so a poisoned lower-priority item cannot starve an
        # account deletion indefinitely.
        reconcile_accounts(self._pending_intents())
        runtime_accounts, inactive = self._runtime_candidates(
            inactive_before_ms=inactive_before_ms
        )
        reconcile_accounts(runtime_accounts)
        inactive = [entry for entry in inactive if entry[0] not in account_users]
        fences_lost = 0
        for user_id, updated_at, revision, observed_cutoff in sorted(inactive):
            try:
                result = self._deletion.delete_inactive(
                    user_id,
                    observed_updated_at_ms=updated_at,
                    observed_revision=revision,
                    inactive_before_ms=observed_cutoff,
                )
                if not isinstance(result, Mapping) or result.get("userId") != user_id:
                    raise RuntimeError("inactive deletion returned invalid data")
                if result == {"status": "active", "userId": user_id}:
                    fences_lost += 1
                elif result == {"status": "expired", "userId": user_id}:
                    completed += 1
                else:
                    raise RuntimeError("inactive deletion returned invalid data")
            except DeletionPending:
                pending += 1

        expiry_result = self._expiry.sweep(now=epoch_seconds)
        if (
            not isinstance(expiry_result, Mapping)
            or set(expiry_result) != {"status", "expired"}
            or expiry_result.get("status") != "ok"
            or isinstance(expiry_result.get("expired"), bool)
            or not isinstance(expiry_result.get("expired"), int)
            or expiry_result["expired"] < 0
        ):
            raise RuntimeError("expiry sweep returned invalid data")
        expired = expiry_result["expired"]

        action_result = None
        if self._action_maintenance is not None:
            action_result = self._action_maintenance.run()
            if (
                not isinstance(action_result, Mapping)
                or set(action_result)
                != {"status", "processed", "failed", "hasMore"}
                or action_result.get("status") != "ok"
                or isinstance(action_result.get("processed"), bool)
                or not isinstance(action_result.get("processed"), int)
                or action_result["processed"] < 0
                or isinstance(action_result.get("failed"), bool)
                or not isinstance(action_result.get("failed"), int)
                or action_result["failed"] < 0
                or not isinstance(action_result.get("hasMore"), bool)
            ):
                raise RuntimeError("action maintenance returned invalid data")
            if action_result["failed"]:
                raise RuntimeError(
                    "action maintenance failed and requires an observable retry"
                )
        result = {
            "status": "ok",
            "expired": expired,
            "deletionsCompleted": completed,
            "deletionsPending": pending,
            "inactiveCandidates": len(inactive),
            "inactivityFencesLost": fences_lost,
        }
        if action_result is not None:
            result.update(
                actionsProcessed=action_result["processed"],
                actionsFailed=action_result["failed"],
                actionsRemaining=action_result["hasMore"],
            )
        return result
