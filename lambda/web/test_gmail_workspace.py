from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from workflows.gmail.models import DraftRevision

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
    return GmailWorkspaceService(table, now=lambda: NOW)


class ConnectionFence:
    def __init__(self, *, generation=6, connected=True):
        self.generation = generation
        self.connected = connected
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
        self.saved.append(kwargs)


class ApprovalSuperseder:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def supersede_pending(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


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

    assert superseder.calls == [
        {
            "action_id": ACTION,
            "user_id": USER,
            "expected_draft_revision": 1,
            "current_draft_revision": 2,
        }
    ]
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
