"""Founder-only conversion of source-backed follow-ups into governed actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Callable, Mapping

from actions.models import ActionState, DraftRevision as ActionDraftRevision, gmail_resource
from actions.state_machine import ConcurrentActionUpdate
from .gmail.models import DraftRevision as LocalDraftRevision, Opportunity


APPROVAL_TTL = timedelta(minutes=15)
_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_ACTION_ID = re.compile(r"[A-Za-z0-9_-]{8,128}")
_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,2048}")


def _utc(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def founder_action_id(
    *,
    user_id: str,
    opportunity: Opportunity,
    connection_id: str,
    account_email: str,
) -> str:
    identity = json.dumps(
        {
            "accountEmail": account_email,
            "connectionId": connection_id,
            "opportunityId": opportunity.id,
            "sourceId": opportunity.source.source_id,
            "userId": user_id,
            "version": 1,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "gmail_fu_" + hashlib.sha256(identity).hexdigest()[:32]


def founder_draft_revision(
    *,
    user_id: str,
    opportunity: Opportunity,
    connection_id: str,
    account_email: str,
) -> LocalDraftRevision:
    """Build the one exact revision displayed locally and governed for send."""

    if not isinstance(opportunity, Opportunity) or opportunity.user_id != user_id:
        raise TypeError("founder draft requires a bound Opportunity")
    action_id = founder_action_id(
        user_id=user_id,
        opportunity=opportunity,
        connection_id=connection_id,
        account_email=account_email,
    )
    return LocalDraftRevision.create(
        action_id=action_id,
        revision=1,
        to=opportunity.source.correspondent,
        subject="Following up",
        body=(
            "Hello,\n\n"
            "Just following up on my previous email.\n\n"
            "Best,"
        ),
    )


@dataclass(frozen=True, slots=True)
class PreparedApproval:
    action_id: str
    token: str

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or _ACTION_ID.fullmatch(self.action_id) is None:
            raise ValueError("prepared approval action identity is invalid")
        if not isinstance(self.token, str) or _TOKEN.fullmatch(self.token) is None:
            raise ValueError("prepared approval token is invalid")


class FounderApprovalProducer:
    """Prepare one deterministic follow-up; never approve or execute it."""

    def __init__(
        self,
        *,
        action_repository,
        approval_service,
        draft_reader,
        founder_user_id: str,
        connection_id: str,
        account_email: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not isinstance(founder_user_id, str)
            or _USER_ID.fullmatch(founder_user_id) is None
        ):
            raise ValueError("founder identity is invalid")
        # Validate the exact send binding at composition time without loading
        # any send refresh token or granting an effect capability.
        gmail_resource(
            connection_id=connection_id,
            account_email=account_email,
        )
        if not callable(getattr(draft_reader, "latest_draft", None)):
            raise ValueError("founder draft reader is required")
        self._actions = action_repository
        self._approvals = approval_service
        self._drafts = draft_reader
        self._founder = founder_user_id
        self._connection_id = connection_id
        self._account_email = account_email
        self._now = now or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _record(
        value: object,
        *,
        draft: ActionDraftRevision,
    ) -> tuple[str, int]:
        if not isinstance(value, Mapping):
            raise ConcurrentActionUpdate("prepared action persistence returned invalid data")
        state = value.get("state")
        revision = value.get("revision")
        if (
            value.get("actionId") != draft.action_id
            or value.get("userId") != draft.user_id
            or value.get("draftRevision") != draft.draft_revision
            or value.get("args") != dict(draft.args)
            or state
            not in {
                ActionState.PREPARED.value,
                ActionState.APPROVAL_PENDING.value,
            }
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise ConcurrentActionUpdate(
                "existing action does not match the deterministic draft"
            )
        return state, revision

    def prepare(
        self,
        *,
        user_id: str,
        opportunity: Opportunity,
        draft: LocalDraftRevision | None = None,
        expected_generation: int | None = None,
    ) -> PreparedApproval | None:
        # The authority check is intentionally first: external pilot scans do
        # not parse, persist, sign, or otherwise touch the effect pipeline.
        if user_id != self._founder:
            return None
        if not isinstance(opportunity, Opportunity) or opportunity.user_id != user_id:
            raise TypeError("founder approval requires a bound Opportunity")
        expected_action_id = founder_action_id(
            user_id=user_id,
            opportunity=opportunity,
            connection_id=self._connection_id,
            account_email=self._account_email,
        )
        if draft is None:
            local_draft = founder_draft_revision(
                user_id=user_id,
                opportunity=opportunity,
                connection_id=self._connection_id,
                account_email=self._account_email,
            )
        elif (
            not isinstance(draft, LocalDraftRevision)
            or draft.action_id != expected_action_id
            or draft.to != opportunity.source.correspondent
        ):
            raise ConcurrentActionUpdate(
                "displayed draft does not match the founder action binding"
            )
        else:
            local_draft = draft
        action_id = local_draft.action_id
        draft = ActionDraftRevision(
            action_id=local_draft.action_id,
            user_id=user_id,
            draft_revision=local_draft.revision,
            connection_id=self._connection_id,
            account_email=self._account_email,
            sender_address=self._account_email,
            args={
                "to": local_draft.to,
                "subject": local_draft.subject,
                "body": local_draft.body,
            },
            # A source-derived timestamp makes retries byte-identical while the
            # action ID and payload remain bound to this exact opportunity.
            created_at=opportunity.waiting_since,
        )
        before = self._drafts.latest_draft(
            user_id=user_id,
            action_id=action_id,
            expected_generation=expected_generation,
        )
        if before != local_draft:
            raise ConcurrentActionUpdate(
                "displayed draft changed before approval preparation"
            )
        record = self._actions.create_prepared(draft=draft)
        state, revision = self._record(record, draft=draft)
        current = self._drafts.latest_draft(
            user_id=user_id,
            action_id=action_id,
            expected_generation=expected_generation,
        )
        if current != local_draft:
            if (
                isinstance(current, LocalDraftRevision)
                and current.action_id == action_id
                and current.revision > local_draft.revision
            ):
                try:
                    self._approvals.mark_stale(
                        action_id=action_id,
                        revision=revision,
                        user_id=user_id,
                        expected_draft_revision=local_draft.revision,
                        current_draft_revision=current.revision,
                    )
                except Exception:
                    raise ConcurrentActionUpdate(
                        "displayed draft changed while approval was prepared"
                    ) from None
            raise ConcurrentActionUpdate(
                "displayed draft changed while approval was prepared"
            )
        if state == ActionState.APPROVAL_PENDING.value:
            token = self._approvals.pending_token(
                action_id=action_id,
                acting_user_id=user_id,
            )
        else:
            now = _utc(self._now(), "approval clock")
            try:
                token = self._approvals.request_approval(
                    action_id=action_id,
                    revision=revision,
                    acting_user_id=user_id,
                    args=dict(draft.args),
                    expires_at=now + APPROVAL_TTL,
                )
            except ConcurrentActionUpdate:
                token = self._approvals.pending_token(
                    action_id=action_id,
                    acting_user_id=user_id,
                )
        return PreparedApproval(action_id=action_id, token=token)
