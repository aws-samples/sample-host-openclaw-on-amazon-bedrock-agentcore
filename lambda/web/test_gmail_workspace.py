from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from actions.connectors import GenericConnectorKernel, GmailConnectorAdapter
from actions.models import ActionState, CapabilityDenied, gmail_resource
from actions.state_machine import (
    ActionStateMachine,
    ApprovalService,
    ApprovalTokenCodec,
)
from control.telegram_cards import ReadOnlyGmailDraftPreparer
from workflows import founder_approval as founder_approval_module
from workflows.founder_approval import FounderApprovalProducer
from workflows.gmail.models import DraftRevision
from workflows.gmail.models import Opportunity, SourceEvidence
from workflows.gmail.repository import DynamoGmailRepository

from .gmail_workspace import (
    DraftEditConflict,
    GmailWorkspaceRecordError,
    GmailWorkspaceService,
)


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
USER = "user_founder"
ACTION = "draft_action_12345678"


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class Table:
    def __init__(self, items=None):
        self.items = {
            (item["PK"], item["SK"]): dict(item) for item in (items or [])
        }
        self.get_calls = []
        self.query_calls = []

    def get_item(self, **kwargs):
        self.get_calls.append(kwargs)
        item = self.items.get((kwargs["Key"]["PK"], kwargs["Key"]["SK"]))
        return {"Item": dict(item)} if item is not None else {}

    def put_item(self, **kwargs):
        item = dict(kwargs["Item"])
        key = (item["PK"], item["SK"])
        if kwargs.get("ConditionExpression") and key in self.items:
            raise ConditionalFailure()
        self.items[key] = item
        return {}

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        values = kwargs["ExpressionAttributeValues"]
        pk = values[":pk"]
        prefix = values[":sk"]
        matching = sorted(
            (
                dict(item)
                for (item_pk, item_sk), item in self.items.items()
                if item_pk == pk and item_sk.startswith(prefix)
            ),
            key=lambda item: item["SK"],
            reverse=kwargs.get("ScanIndexForward") is False,
        )
        start = kwargs.get("ExclusiveStartKey")
        if start:
            start_key = (start["PK"], start["SK"])
            keys = [(item["PK"], item["SK"]) for item in matching]
            matching = matching[keys.index(start_key) + 1 :]
        limit = kwargs.get("Limit", len(matching))
        page = matching[:limit]
        response = {"Items": page}
        if len(matching) > limit:
            response["LastEvaluatedKey"] = {
                "PK": page[-1]["PK"],
                "SK": page[-1]["SK"],
            }
        return response


def draft_item(
    *,
    user=USER,
    action=ACTION,
    revision=1,
    to="ada@example.com",
    subject="Original subject",
    body="Original body",
    ttl=None,
):
    draft = DraftRevision.create(
        action_id=action,
        revision=revision,
        to=to,
        subject=subject,
        body=body,
    )
    return {
        "PK": f"USER#{user}",
        "SK": f"GMAIL#DRAFT#{action}#{revision:010d}",
        "draft": {
            "actionId": draft.action_id,
            "revision": Decimal(draft.revision),
            "to": draft.to,
            "subject": draft.subject,
            "body": draft.body,
            "payloadHash": draft.payload_hash,
        },
        "ttl": Decimal(ttl or int((NOW + timedelta(days=14)).timestamp())),
    }


def opportunity_item(*, user=USER, ttl=None):
    return {
        "PK": f"USER#{user}",
        "SK": "GMAIL#OPPORTUNITIES",
        "opportunities": [
            {
                "id": "opp_0123456789abcdef0123456789abcdef",
                "userId": user,
                "source": {
                    "sourceId": "gmail:thread_1:message_1",
                    "threadId": "thread_1",
                    "deepLink": "https://mail.google.com/mail/u/0/#inbox/thread_1",
                    "correspondent": "Ada <ada@example.com>",
                    "subject": "Waiting for your answer",
                    "excerpt": "A bounded derived excerpt",
                },
                "waitingSince": "2026-07-10T08:30:00+00:00",
                "title": "Ada is waiting",
                "reason": "You promised a reply last week.",
                "confidence": Decimal("0.9"),
            }
        ],
        "ttl": Decimal(ttl or int((NOW + timedelta(days=14)).timestamp())),
    }


def service(table):
    return GmailWorkspaceService(
        table,
        approval_superseder=ApprovalSuperseder(),
        now=lambda: NOW,
    )


class ConnectionFence:
    def __init__(self, *, generation=6, connected=True, save_error=None):
        self.generation = generation
        self.connected = connected
        self.save_error = save_error
        self.saved = []

    def connected_generation(self, user_id):
        assert user_id == USER
        if not self.connected:
            raise RuntimeError("disconnected")
        return self.generation

    def assert_generation(self, user_id, generation, *, require_connected=False):
        assert user_id == USER
        if generation != self.generation or (require_connected and not self.connected):
            raise RuntimeError("stale")

    def save_draft(self, **kwargs):
        if self.save_error is not None:
            raise self.save_error
        self.saved.append(kwargs)


class ApprovalSuperseder:
    def __init__(self, error=None):
        self.calls = []
        self.error = error
        self.state = "APPROVAL_PENDING"

    def supersede_pending(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        self.state = "STALE"
        return {
            "draftPersisted": True,
            "actionId": kwargs["action_id"],
            "userId": kwargs["user_id"],
            "draftRevision": kwargs["current_draft_revision"],
            "payloadHash": kwargs["draft"].payload_hash,
        }


def test_get_returns_only_live_derived_opportunities_and_latest_draft_per_action():
    expired = int((NOW - timedelta(seconds=1)).timestamp())
    table = Table(
        [
            opportunity_item(),
            draft_item(revision=1),
            draft_item(revision=2, subject="Latest subject", body="Latest body"),
            draft_item(action="expired_action_123", ttl=expired),
            opportunity_item(user="other_user"),
            draft_item(user="other_user", action="other_action_123"),
        ]
    )

    result = service(table).get(USER)

    assert result == {
        "userId": USER,
        "opportunities": [
            {
                "id": "opp_0123456789abcdef0123456789abcdef",
                "title": "Ada is waiting",
                "reason": "You promised a reply last week.",
                "waitingSince": "2026-07-10T08:30:00+00:00",
                "sourceUrl": "https://mail.google.com/mail/u/0/#inbox/thread_1",
                "correspondent": "Ada <ada@example.com>",
                "subject": "Waiting for your answer",
                "confidence": 0.9,
            }
        ],
        "drafts": [
            {
                "actionId": ACTION,
                "revision": 2,
                "to": "ada@example.com",
                "subject": "Latest subject",
                "body": "Latest body",
                "payloadHash": DraftRevision.compute_payload_hash(
                    to="ada@example.com",
                    subject="Latest subject",
                    body="Latest body",
                ),
            }
        ],
    }
    assert table.get_calls == [
        {
            "Key": {"PK": f"USER#{USER}", "SK": "GMAIL#OPPORTUNITIES"},
            "ConsistentRead": True,
        }
    ]
    assert table.query_calls
    assert all(call["ConsistentRead"] is True for call in table.query_calls)
    assert all(
        call["ExpressionAttributeValues"][":pk"] == f"USER#{USER}"
        for call in table.query_calls
    )


def test_get_hides_an_expired_opportunity_record():
    table = Table(
        [
            opportunity_item(
                ttl=int((NOW - timedelta(seconds=1)).timestamp())
            )
        ]
    )

    assert service(table).get(USER)["opportunities"] == []


def test_disconnected_workspace_hides_all_gmail_derived_content():
    table = Table([opportunity_item(), draft_item()])
    fence = ConnectionFence(connected=False)
    workspace = GmailWorkspaceService(
        table,
        repository=fence,
        approval_superseder=ApprovalSuperseder(),
        enforce_connection_fence=True,
        now=lambda: NOW,
    )

    assert workspace.get(USER) == {
        "userId": USER,
        "opportunities": [],
        "drafts": [],
    }
    assert table.get_calls == []
    assert table.query_calls == []


def test_edit_requires_current_revision_and_creates_immutable_recipient_revision():
    table = Table([draft_item()])

    result = service(table).edit_draft(
        user_id=USER,
        action_id=ACTION,
        revision=1,
        subject="  Updated subject  ",
        body="Updated body\nwith exact whitespace. ",
    )

    expected_hash = DraftRevision.compute_payload_hash(
        to="ada@example.com",
        subject="Updated subject",
        body="Updated body\nwith exact whitespace. ",
    )
    assert result == {
        "draft": {
            "actionId": ACTION,
            "revision": 2,
            "to": "ada@example.com",
            "subject": "Updated subject",
            "body": "Updated body\nwith exact whitespace. ",
            "payloadHash": expected_hash,
        }
    }
    stored = table.items[
        (f"USER#{USER}", f"GMAIL#DRAFT#{ACTION}#0000000002")
    ]
    assert stored["draft"]["to"] == "ada@example.com"
    assert stored["ttl"] == int((NOW + timedelta(days=14)).timestamp())


def test_fenced_edit_forwards_the_generation_captured_before_reading():
    item = draft_item()
    item["connectionGeneration"] = 6
    table = Table([item])
    fence = ConnectionFence()
    workspace = GmailWorkspaceService(
        table,
        repository=fence,
        approval_superseder=ApprovalSuperseder(),
        enforce_connection_fence=True,
        now=lambda: NOW,
    )

    workspace.edit_draft(
        user_id=USER,
        action_id=ACTION,
        revision=1,
        subject="Updated subject",
        body="Updated body",
    )

    assert fence.saved[0]["expected_generation"] == 6


def test_edit_stales_pending_approval_before_persisting_new_draft_revision():
    item = draft_item()
    item["connectionGeneration"] = 6
    table = Table([item])
    fence = ConnectionFence()
    superseder = ApprovalSuperseder()
    workspace = GmailWorkspaceService(
        table,
        repository=fence,
        approval_superseder=superseder,
        enforce_connection_fence=True,
        now=lambda: NOW,
    )

    workspace.edit_draft(
        user_id=USER,
        action_id=ACTION,
        revision=1,
        subject="Updated subject",
        body="Updated body",
    )

    assert len(superseder.calls) == 1
    call = superseder.calls[0]
    assert call["action_id"] == ACTION
    assert call["user_id"] == USER
    assert call["expected_draft_revision"] == 1
    assert call["current_draft_revision"] == 2
    assert call["draft"].revision == 2
    assert call["expected_generation"] == 6
    assert len(fence.saved) == 1


def test_edit_fails_closed_without_saving_when_approval_staling_fails():
    item = draft_item()
    item["connectionGeneration"] = 6
    table = Table([item])
    fence = ConnectionFence()
    superseder = ApprovalSuperseder(RuntimeError("stale transition failed"))
    workspace = GmailWorkspaceService(
        table,
        repository=fence,
        approval_superseder=superseder,
        enforce_connection_fence=True,
        now=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="stale transition failed"):
        workspace.edit_draft(
            user_id=USER,
            action_id=ACTION,
            revision=1,
            subject="Updated subject",
            body="Updated body",
        )

    assert fence.saved == []
    assert superseder.state == "APPROVAL_PENDING"


def test_edit_refuses_to_write_without_the_approval_supersession_boundary():
    item = draft_item()
    item["connectionGeneration"] = 6
    table = Table([item])
    fence = ConnectionFence()
    workspace = GmailWorkspaceService(
        table,
        repository=fence,
        enforce_connection_fence=True,
        now=lambda: NOW,
    )

    with pytest.raises(DraftEditConflict, match="supersession.*unavailable"):
        workspace.edit_draft(
            user_id=USER,
            action_id=ACTION,
            revision=1,
            subject="Updated subject",
            body="Updated body",
        )

    assert fence.saved == []


def test_edit_save_failure_keeps_old_authority_stale_without_new_revision():
    item = draft_item()
    item["connectionGeneration"] = 6
    table = Table([item])
    error = RuntimeError("draft persistence unavailable")
    fence = ConnectionFence(save_error=error)
    superseder = ApprovalSuperseder()
    workspace = GmailWorkspaceService(
        table,
        repository=fence,
        approval_superseder=superseder,
        enforce_connection_fence=True,
        now=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="persistence unavailable") as raised:
        workspace.edit_draft(
            user_id=USER,
            action_id=ACTION,
            revision=1,
            subject="Updated subject",
            body="Updated body",
        )

    assert raised.value is error
    assert superseder.state == "STALE"
    assert fence.saved == []
    assert set(table.items) == {
        (f"USER#{USER}", f"GMAIL#DRAFT#{ACTION}#0000000001")
    }


def test_edit_fences_action_creation_inside_the_draft_save_gap():
    item = draft_item()
    item["connectionGeneration"] = 6
    table = Table([item])

    class AbsentThenPreparing:
        def __init__(self):
            self.state = None

        def supersede_pending(self, **_kwargs):
            # The competing approval wins the shared ACTION-item condition.
            # A production atomic editor reports no successful draft outcome.
            self.state = "APPROVAL_PENDING"
            return None

    superseder = AbsentThenPreparing()

    class PrepareInsideSave(ConnectionFence):
        def save_draft(self, **kwargs):
            # Deterministic inverse interleaving: the edit already observed no
            # action, then revision 1 gains approval authority immediately
            # before the immutable revision-2 write.
            superseder.state = "APPROVAL_PENDING"
            super().save_draft(**kwargs)

    fence = PrepareInsideSave()
    workspace = GmailWorkspaceService(
        table,
        repository=fence,
        approval_superseder=superseder,
        enforce_connection_fence=True,
        now=lambda: NOW,
    )

    with pytest.raises(DraftEditConflict, match="atomic draft edit"):
        workspace.edit_draft(
            user_id=USER,
            action_id=ACTION,
            revision=1,
            subject="Updated subject",
            body="Updated body",
        )

    assert superseder.state == "APPROVAL_PENDING"
    assert fence.saved == []


def test_edit_rejects_stale_or_missing_draft_without_writing():
    table = Table([draft_item(revision=2)])
    before = dict(table.items)

    with pytest.raises(DraftEditConflict, match="current revision"):
        service(table).edit_draft(
            user_id=USER,
            action_id=ACTION,
            revision=1,
            subject="Changed",
            body="Changed body",
        )
    with pytest.raises(DraftEditConflict, match="not available"):
        service(table).edit_draft(
            user_id=USER,
            action_id="missing_action_123",
            revision=1,
            subject="Changed",
            body="Changed body",
        )

    assert table.items == before


def test_edit_rejects_invalid_input_before_querying_or_writing():
    table = Table([draft_item()])

    for revision in (True, 0, Decimal(1)):
        with pytest.raises(ValueError):
            service(table).edit_draft(
                user_id=USER,
                action_id=ACTION,
                revision=revision,
                subject="Changed",
                body="Changed body",
            )
    with pytest.raises(ValueError):
        service(table).edit_draft(
            user_id=USER,
            action_id=ACTION,
            revision=1,
            subject="",
            body="Changed body",
        )


def test_corrupt_or_cross_tenant_records_fail_closed():
    bad_opportunity = opportunity_item()
    bad_opportunity["opportunities"][0]["userId"] = "other_user"
    table = Table([bad_opportunity])

    with pytest.raises(GmailWorkspaceRecordError):
        service(table).get(USER)

    bad_draft = draft_item()
    bad_draft["draft"]["to"] = "attacker@example.com"
    with pytest.raises(GmailWorkspaceRecordError):
        service(Table([bad_draft])).get(USER)


def test_displayed_founder_draft_edit_stales_the_real_pending_action():
    founder = "founder-1"
    connection_ref = "google_conn_1234"
    account_email = "founder@example.com"
    source = SourceEvidence(
        source_id="gmail:thread1:message1",
        thread_id="thread1",
        deep_link="https://mail.google.com/mail/u/0/#inbox/thread1",
        correspondent="person@example.net",
        subject="Quarterly plan",
        excerpt="A bounded derived excerpt",
        waiting_since=NOW - timedelta(days=8),
    )
    opportunity = Opportunity(
        id="opp_12345678",
        user_id=founder,
        source=source,
        waiting_since=source.waiting_since,
        title="Follow up with person",
        reason="They have not replied",
        confidence=0.9,
    )

    class ActionRepository:
        def __init__(self):
            self.record = None

        def create_prepared(self, *, draft):
            if self.record is None:
                self.record = {
                    "actionId": draft.action_id,
                    "userId": draft.user_id,
                    "state": ActionState.PREPARED.value,
                    "revision": 1,
                    "draftRevision": draft.draft_revision,
                    "connectionId": draft.connection_id,
                    "accountEmail": draft.account_email,
                    "senderAddress": draft.sender_address,
                    "capability": "gmail.send",
                    "resource": gmail_resource(
                        connection_id=draft.connection_id,
                        account_email=draft.account_email,
                    ),
                    "args": dict(draft.args),
                    "payloadHash": draft.payload_hash,
                    "ttl": int((NOW + timedelta(days=14)).timestamp()),
                }
            elif (
                self.record["state"] == ActionState.STALE.value
                and self.record["supersededByDraftRevision"]
                == draft.draft_revision
            ):
                for stale_or_approval in {
                    "approvalId",
                    "approvalActionId",
                    "approvalDraftRevision",
                    "approvalArgsHash",
                    "approvalExpiresAt",
                    "approvalRequestedAt",
                    "staleAt",
                    "staleReason",
                    "staleDraftRevision",
                    "supersededByDraftRevision",
                }:
                    self.record.pop(stale_or_approval, None)
                self.record.update(
                    {
                        "state": ActionState.PREPARED.value,
                        "revision": self.record["revision"] + 1,
                        "draftRevision": draft.draft_revision,
                        "args": dict(draft.args),
                        "payloadHash": draft.payload_hash,
                        "ttl": int((NOW + timedelta(days=14)).timestamp()),
                    }
                )
            return dict(self.record)

        def get(self, *, action_id, user_id):
            if self.record is None:
                return None
            if (
                self.record["actionId"] != action_id
                or self.record["userId"] != user_id
            ):
                return None
            return dict(self.record)

        def transition(
            self,
            *,
            action_id,
            user_id,
            expected_state,
            target_state,
            expected_revision,
            transition_id,
            updates,
        ):
            assert self.record is not None
            if (
                self.record["actionId"] != action_id
                or self.record["userId"] != user_id
                or self.record["state"] != expected_state.value
                or self.record["revision"] != expected_revision
            ):
                raise RuntimeError("action transition lost its exact fence")
            self.record.update(updates)
            self.record["state"] = target_state.value
            self.record["revision"] = expected_revision + 1
            self.record["lastTransitionId"] = transition_id
            return dict(self.record)

    table = Table()
    gmail_repository = DynamoGmailRepository(table, now=lambda: NOW)
    displayed = ReadOnlyGmailDraftPreparer(
        gmail_repository,
        draft_factory=lambda **request: founder_approval_module.founder_draft_revision(
            user_id=request["user_id"],
            opportunity=request["opportunity"],
            connection_id=connection_ref,
            account_email=account_email,
        ),
        now=lambda: NOW,
    ).prepare(user_id=founder, opportunity=opportunity)

    actions = ActionRepository()
    machine = ActionStateMachine(actions)
    approvals = ApprovalService(
        state_machine=machine,
        token_codec=ApprovalTokenCodec(b"a" * 32),
        founder_user_ids={founder},
        now=lambda: NOW,
        approval_id_factory=lambda: "approval_123456789",
    )
    class UnusedConnectionRevoker:
        @staticmethod
        def revoke_all(_connection_ref):
            raise AssertionError("draft edit must not revoke the connection")

    class LocalAtomicDraftEditor:
        def save_superseding_draft(self, **kwargs):
            draft = kwargs["draft"]
            record = actions.get(
                action_id=draft.action_id,
                user_id=kwargs["user_id"],
            )
            if record is not None:
                machine.stale_for_new_draft(
                    action_id=draft.action_id,
                    user_id=kwargs["user_id"],
                    revision=record["revision"],
                    expected_draft_revision=kwargs[
                        "expected_draft_revision"
                    ],
                    current_draft_revision=draft.revision,
                    now=NOW,
                )
            gmail_repository.save_draft(
                user_id=kwargs["user_id"],
                draft=draft,
                expires_at=kwargs["expires_at"],
            )
            return {
                "draftPersisted": True,
                "actionId": draft.action_id,
                "userId": kwargs["user_id"],
                "draftRevision": draft.revision,
                "payloadHash": draft.payload_hash,
            }

    kernel = GenericConnectorKernel(
        GmailConnectorAdapter(
            executor=object(),
            repository=actions,
            draft_editor=LocalAtomicDraftEditor(),
            state_machine=machine,
            connection_revoker=UnusedConnectionRevoker(),
            now=lambda: NOW,
        )
    )
    workspace = GmailWorkspaceService(
        table,
        repository=gmail_repository,
        approval_superseder=kernel,
        now=lambda: NOW,
    )
    first_edit = workspace.edit_draft(
        user_id=founder,
        action_id=displayed.action_id,
        revision=displayed.revision,
        subject="Updated subject",
        body="Updated body",
    )["draft"]
    assert first_edit["revision"] == 2
    assert actions.record is None

    current_displayed = ReadOnlyGmailDraftPreparer(
        gmail_repository,
        draft_factory=lambda **request: founder_approval_module.founder_draft_revision(
            user_id=request["user_id"],
            opportunity=request["opportunity"],
            connection_id=connection_ref,
            account_email=account_email,
        ),
        now=lambda: NOW,
    ).prepare(user_id=founder, opportunity=opportunity)
    assert current_displayed.revision == 2
    producer = FounderApprovalProducer(
        action_repository=actions,
        approval_service=approvals,
        draft_reader=gmail_repository,
        founder_user_id=founder,
        connection_id=connection_ref,
        account_email=account_email,
        now=lambda: NOW,
    )
    pending = producer.prepare(
        user_id=founder,
        opportunity=opportunity,
        draft=current_displayed,
    )
    assert pending is not None
    assert pending.action_id == displayed.action_id
    assert actions.record["state"] == ActionState.APPROVAL_PENDING.value

    visible = workspace.get(founder)["drafts"][0]
    assert visible["actionId"] == pending.action_id
    assert visible["revision"] == actions.record["draftRevision"]
    assert {
        "to": visible["to"],
        "subject": visible["subject"],
        "body": visible["body"],
    } == actions.record["args"]
    assert visible["payloadHash"] == actions.record["payloadHash"]

    revised = workspace.edit_draft(
        user_id=founder,
        action_id=visible["actionId"],
        revision=visible["revision"],
        subject="Updated again",
        body="Updated body again",
    )

    assert revised["draft"]["revision"] == 3
    assert actions.record["state"] == ActionState.STALE.value
    assert actions.record["staleDraftRevision"] == 2
    assert actions.record["supersededByDraftRevision"] == 3

    latest = gmail_repository.latest_draft(
        user_id=founder,
        action_id=displayed.action_id,
    )
    assert latest is not None and latest.revision == 3
    replacement = producer.prepare(
        user_id=founder,
        opportunity=opportunity,
        draft=latest,
    )

    assert replacement is not None
    assert replacement.token != pending.token
    assert actions.record["state"] == ActionState.APPROVAL_PENDING.value
    assert actions.record["draftRevision"] == 3
    assert actions.record["revision"] > 3
    with pytest.raises(CapabilityDenied):
        approvals.approve(
            action_id=replacement.action_id,
            revision=actions.record["revision"],
            acting_user_id=founder,
            token=pending.token,
            args=actions.record["args"],
        )
    approved = approvals.approve(
        action_id=replacement.action_id,
        revision=actions.record["revision"],
        acting_user_id=founder,
        token=replacement.token,
        args=actions.record["args"],
    )
    assert approved["state"] == ActionState.APPROVED.value
    assert approved["approvedDraftRevision"] == 3
