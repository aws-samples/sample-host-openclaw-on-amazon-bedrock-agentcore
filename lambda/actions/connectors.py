"""Connector-generic action kernel.

This module extracts the generic admission/approval/dispatch/reconcile
discipline the Gmail-specific action code already implements, so future
connectors reuse the same guards without reimplementing them.

Design (Task 3): the concrete Gmail guards stay where they are proven, inside
``GmailSendExecutor`` and ``GmailEffectReconciler``. ``GmailConnectorAdapter``
is a thin WRAPPER that composes the existing objects by delegation, so:

* every existing stored-record shape is unchanged;
* every existing public signature (``executor.execute(action)``,
  ``reconciler.reconcile(action_id=, user_id=)``) is unchanged; and
* every fence/hash/expiry guard remains inside the untouched concrete code.

``GenericConnectorKernel`` documents the pure structural delegation shape a
connector follows (get-persisted -> admit -> resolve-creds-lazily ->
recheck-binding+fence -> dispatch/reconcile). It owns NO guards of its own for
v0; it simply forwards to the concrete adapter.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping, Optional, Protocol, runtime_checkable

try:
    from .proposals import ActionProposalV1
    from .receipts import EffectReceiptV1
except ImportError:  # pragma: no cover - bare-module load path (action_connectors)
    from action_proposals import ActionProposalV1
    from action_receipts import EffectReceiptV1


@runtime_checkable
class ConnectorContext(Protocol):
    """The exact caller-bound context a connector operation runs under."""

    user_id: str
    capability: str
    resource: str
    connection_ref: str

    @property
    def now(self): ...


@runtime_checkable
class ConnectorAdapter(Protocol):
    """The connector-generic effect surface every connector implements.

    ``dispatch`` must NEVER perform an effect from an in-memory proposal: it
    receives the persisted, approved action identity/record and re-loads the
    exact persisted state before acting. Credentials must NEVER be resolved
    before the adapter is admitted on the dispatch path.
    """

    def read(
        self, context: ConnectorContext, operation: str, args: Mapping[str, object]
    ) -> Mapping[str, object]:
        """A read-only provider observation. No effect, no state transition."""
        ...

    def prepare(
        self, context: ConnectorContext, operation: str, args: Mapping[str, object]
    ) -> ActionProposalV1:
        """Persist an approvable PREPARED proposal."""
        ...

    def dispatch(self, approved_action: Mapping[str, object]) -> EffectReceiptV1:
        """Perform the exact approved effect for a persisted APPROVED action."""
        ...

    def reconcile(
        self, action: Mapping[str, object]
    ) -> Optional[EffectReceiptV1]:
        """Reconcile a DISPATCHING/UNCERTAIN action; never re-dispatches."""
        ...

    def revoke(self, connection_ref: str) -> None:
        """Revoke the connection. The kernel never holds provider creds."""
        ...


class GenericConnectorKernel:
    """Pure structural delegation to a concrete :class:`ConnectorAdapter`.

    The kernel is trusted-service-side and credential-free: it holds only the
    admitted adapter and forwards. All guards live in the concrete adapter so
    behavior is provably identical to the pre-extraction Gmail path.
    """

    def __init__(self, adapter: ConnectorAdapter) -> None:
        self._adapter = adapter

    def read(self, context, operation, args):
        return self._adapter.read(context, operation, args)

    def prepare(self, context, operation, args):
        return self._adapter.prepare(context, operation, args)

    def dispatch(self, approved_action):
        return self._adapter.dispatch(approved_action)

    def reconcile(self, action):
        return self._adapter.reconcile(action)

    def revoke(self, connection_ref):
        return self._adapter.revoke(connection_ref)

    def supersede_pending(self, **kwargs):
        supersede = getattr(self._adapter, "supersede_pending", None)
        if not callable(supersede):
            raise ValueError("connector adapter has no approval supersede boundary")
        return supersede(**kwargs)


class GmailConnectorAdapter:
    """The Gmail-send concrete :class:`ConnectorAdapter`.

    Composes the existing ``GmailSendExecutor`` / ``GmailEffectReconciler`` /
    ``GmailApiAdapter`` / ``DynamoActionRepository`` / ``ApprovalService`` by
    delegation. Adds no guards and changes no signatures; the wrapper only
    adapts the connector-generic surface to the concrete Gmail methods.
    """

    def __init__(
        self,
        *,
        executor,
        reconciler=None,
        repository=None,
        approval_service=None,
        provider=None,
        connection_revoker=None,
        state_machine=None,
        now=None,
    ) -> None:
        if executor is None:
            raise ValueError("GmailConnectorAdapter requires a GmailSendExecutor")
        if not callable(getattr(connection_revoker, "revoke_all", None)):
            raise ValueError("GmailConnectorAdapter requires a connection revoker")
        self._executor = executor
        self._reconciler = reconciler
        self._repository = repository
        self._approvals = approval_service
        self._provider = provider
        self._connection_revoker = connection_revoker
        self._state_machine = state_machine
        self._now = now or (lambda: datetime.now(timezone.utc))

    # --- read: no-effect provider observation ---------------------------
    def read(
        self, context, operation: str, args: Mapping[str, object]
    ) -> Mapping[str, object]:
        provider = self._provider
        if provider is None:
            raise ValueError("Gmail read requires a provider observation surface")
        if operation == "verify_bound_account":
            provider._assert_bound_provider_account()
            return {"boundAccount": True}
        if operation == "find_by_message_id":
            found = provider.find_by_message_id(
                message_id=args["message_id"],
                sender_address=args["sender_address"],
                recipient=args["recipient"],
                payload_hash=args["payload_hash"],
            )
            return {"found": found}
        raise ValueError(f"unsupported Gmail read operation: {operation!r}")

    # --- prepare: persist an approvable PREPARED proposal ---------------
    def prepare(
        self, context, operation: str, args: Mapping[str, object]
    ) -> ActionProposalV1:
        if self._repository is None:
            raise ValueError("Gmail prepare requires an action repository")
        # The typed DraftRevision IS the ActionProposalV1 for Gmail. The caller
        # supplies a DraftRevision (or the kwargs to build one) via args.
        draft = args["draft"]
        record = self._repository.create_prepared(draft=draft)
        if self._approvals is not None and args.get("request_approval"):
            self._approvals.request_approval(
                action_id=draft.action_id,
                revision=record["revision"],
                acting_user_id=draft.user_id,
                args=dict(draft.args),
                expires_at=args["expires_at"],
            )
        return draft

    # --- dispatch: perform the exact approved effect --------------------
    def dispatch(self, approved_action: Mapping[str, object]) -> EffectReceiptV1:
        # 1:1 delegation. The executor re-loads the persisted action via the
        # state machine and enforces every hash/binding/fence/expiry guard.
        return self._executor.execute(approved_action)

    # --- reconcile: never re-dispatches ---------------------------------
    def reconcile(
        self, action: Mapping[str, object]
    ) -> Optional[EffectReceiptV1]:
        if self._reconciler is None:
            raise ValueError("Gmail reconcile requires a reconciler")
        # Adapt the generic (action) shape to the concrete kwarg signature.
        return self._reconciler.reconcile(
            action_id=action["actionId"],
            user_id=action["userId"],
        )

    # --- revoke: kernel never holds provider creds ----------------------
    def revoke(self, connection_ref: str) -> None:
        self._connection_revoker.revoke_all(connection_ref)

    # --- property 7: a newer draft atomically stales a pending approval -
    def supersede_pending(
        self,
        *,
        action_id: str,
        user_id: str,
        revision: int | None = None,
        expected_draft_revision: int,
        current_draft_revision: int,
    ):
        """Route a newer draft revision through ApprovalService.mark_stale.

        This closes the Task-5-deferred gap: a local draft edit that bumps the
        draft revision must atomically STALE the old pending founder approval,
        under the repository's conditional draft-fence.
        """
        if revision is None:
            if self._repository is None:
                raise ValueError("supersede requires an action repository")
            record = self._repository.get(action_id=action_id, user_id=user_id)
            if record is None:
                return None
            if record.get("state") not in {
                "PREPARED",
                "APPROVAL_PENDING",
                "APPROVED",
            }:
                return None
            revision = record.get("revision")
        if self._approvals is not None:
            return self._approvals.mark_stale(
                action_id=action_id,
                revision=revision,
                user_id=user_id,
                expected_draft_revision=expected_draft_revision,
                current_draft_revision=current_draft_revision,
            )
        if self._state_machine is None:
            raise ValueError("supersede requires an approval state machine")
        return self._state_machine.stale_for_new_draft(
            action_id=action_id,
            revision=revision,
            user_id=user_id,
            expected_draft_revision=expected_draft_revision,
            current_draft_revision=current_draft_revision,
            now=self._now(),
        )
