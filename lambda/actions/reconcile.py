"""Provider-evidence reconciliation for uncertain Gmail effects."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Mapping

try:
    from .gmail_send import deterministic_message_id, validate_email_args
    from .models import (
        ActionState,
        EffectReceipt,
        WaitingForReply,
        canonical_args_hash,
        gmail_resource,
    )
except ImportError:
    from action_gmail_send import deterministic_message_id, validate_email_args
    from action_models import (
        ActionState,
        EffectReceipt,
        WaitingForReply,
        canonical_args_hash,
        gmail_resource,
    )


class ReconciliationDeferred(RuntimeError):
    """Provider observation or durable confirmation did not complete safely."""


class GmailEffectReconciler:
    def __init__(
        self,
        *,
        state_machine,
        repository,
        provider,
        connection_id: str,
        account_email: str,
        sender_address: str,
        founder_user_ids: Iterable[str],
        deletion_blocked,
        now=None,
    ) -> None:
        self._resource = gmail_resource(
            connection_id=connection_id, account_email=account_email
        )
        if sender_address != account_email:
            raise ValueError("v0 sender must equal the bound Google account")
        self._machine = state_machine
        self._repository = repository
        self._provider = provider
        self._connection_id = connection_id
        self._account_email = account_email
        self._sender_address = sender_address
        self._founder_user_ids = frozenset(founder_user_ids)
        if (
            not self._founder_user_ids
            or any(
                not isinstance(value, str) or not value or len(value) > 128
                for value in self._founder_user_ids
            )
        ):
            raise ValueError("Gmail reconciliation founder identities are invalid")
        if not callable(deletion_blocked):
            raise ValueError("Gmail reconciliation deletion fence is required")
        self._deletion_blocked = deletion_blocked
        self._now = now or (lambda: datetime.now(timezone.utc))

    def reconcile(self, *, action_id: str, user_id: str) -> EffectReceipt | None:
        if user_id not in self._founder_user_ids:
            return None
        action = self._repository.get(action_id=action_id, user_id=user_id)
        if not isinstance(action, Mapping):
            return None
        try:
            state = ActionState(action["state"])
            if state not in {ActionState.DISPATCHING, ActionState.UNCERTAIN}:
                return None
            observed_at = self._now()
            ttl = action.get("ttl")
            if (
                not isinstance(observed_at, datetime)
                or observed_at.tzinfo is None
                or isinstance(ttl, bool)
                or not isinstance(ttl, int)
                or ttl <= int(observed_at.timestamp())
            ):
                return None
            args = validate_email_args(action["args"])
            payload_hash = canonical_args_hash(args)
            draft_revision = action["draftRevision"]
            action_revision = action["revision"]
            if (
                isinstance(draft_revision, bool)
                or not isinstance(draft_revision, int)
                or draft_revision < 1
                or isinstance(action_revision, bool)
                or not isinstance(action_revision, int)
                or action_revision < 1
                or action.get("actionId") != action_id
                or action.get("userId") != user_id
                or payload_hash != action.get("payloadHash")
                or payload_hash != action.get("approvalArgsHash")
                or payload_hash != action.get("approvedArgsHash")
                or action.get("approvalActionId") != action_id
                or action.get("approvedActionId") != action_id
                or action.get("approvalDraftRevision") != draft_revision
                or action.get("approvedDraftRevision") != draft_revision
                or action.get("dispatchDraftRevision") != draft_revision
                or action.get("connectionId") != self._connection_id
                or action.get("accountEmail") != self._account_email
                or action.get("senderAddress") != self._sender_address
                or action.get("resource") != self._resource
            ):
                return None
            message_id = deterministic_message_id(
                action_id=action_id,
                draft_revision=draft_revision,
                resource=self._resource,
                payload_hash=payload_hash,
            )
            if action.get("messageId") != message_id:
                return None
            try:
                blocked = self._deletion_blocked(user_id)
            except Exception:
                raise ReconciliationDeferred(
                    "account deletion fence is unavailable"
                ) from None
            if not isinstance(blocked, bool):
                raise ReconciliationDeferred(
                    "account deletion fence returned an invalid result"
                )
            if blocked:
                return None
            try:
                evidence = self._provider.find_by_message_id(
                    message_id=message_id,
                    sender_address=self._sender_address,
                    recipient=args["to"],
                    payload_hash=payload_hash,
                )
            except Exception:
                raise ReconciliationDeferred(
                    "Gmail provider observation is deferred"
                ) from None
            if evidence is None:
                return None
            receipt = EffectReceipt.from_provider_evidence(evidence)
            if (
                receipt.message_id != message_id
                or receipt.connection_id != self._connection_id
                or receipt.account_email != self._account_email
                or receipt.sender_address != self._sender_address
                or receipt.recipient != args["to"]
                or receipt.payload_hash != payload_hash
            ):
                return None
            tracker = WaitingForReply(
                action_id=action_id,
                draft_revision=draft_revision,
                connection_id=self._connection_id,
                account_email=self._account_email,
                recipient=args["to"],
                message_id=message_id,
                provider_thread_id=receipt.provider_thread_id,
                since=receipt.executed_at,
            )
        except ReconciliationDeferred:
            raise
        except Exception:
            return None
        confirmed_at = observed_at.astimezone(timezone.utc)
        try:
            self._machine.transition(
                action_id=action_id,
                user_id=user_id,
                current=state,
                target=ActionState.CONFIRMED,
                revision=action_revision,
                updates={
                    "effectReceipt": receipt.record(),
                    "waitingForReply": tracker.record(),
                    "confirmationMethod": "provider-history-reconciliation",
                    "confirmedAt": confirmed_at.isoformat(),
                },
            )
        except Exception:
            raise ReconciliationDeferred(
                "Gmail confirmation persistence is deferred"
            ) from None
        return receipt
