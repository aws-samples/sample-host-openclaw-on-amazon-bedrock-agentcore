from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from actions.models import (
    ActionState,
    CapabilityDenied,
    CapabilityGrant,
    canonical_args_hash,
)

from .retention import DeletionPending
from .services import ApprovalWebService, RetentionSweepService, WorkspaceService


NOW = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
USER = "founder-1"
OTHER = "other-user"
ACTION_ID = "action_12345678"
RESOURCE = (
    "google:gmail:connection:google_conn_1234:account:founder@example.com"
)
ARGS = {
    "to": "person@example.net",
    "subject": "Following up",
    "body": "Hello again",
}
HASH = canonical_args_hash(ARGS)


def grant(**overrides) -> CapabilityGrant:
    values = {
        "action_id": ACTION_ID,
        "draft_revision": 4,
        "user_id": USER,
        "capability": "gmail.send",
        "resource": RESOURCE,
        "args_hash": HASH,
        "expires_at": NOW + timedelta(minutes=5),
        "approval_id": "appr_1234567890abcdef",
    }
    values.update(overrides)
    return CapabilityGrant(**values)


def pending(**overrides) -> dict[str, object]:
    record: dict[str, object] = {
        "actionId": ACTION_ID,
        "userId": USER,
        "state": ActionState.APPROVAL_PENDING.value,
        "revision": 2,
        "draftRevision": 4,
        "connectionId": "google_conn_1234",
        "accountEmail": "founder@example.com",
        "senderAddress": "founder@example.com",
        "capability": "gmail.send",
        "resource": RESOURCE,
        "args": dict(ARGS),
        "payloadHash": HASH,
        "approvalId": "appr_1234567890abcdef",
        "approvalActionId": ACTION_ID,
        "approvalDraftRevision": 4,
        "approvalArgsHash": HASH,
        "approvalExpiresAt": (NOW + timedelta(minutes=5)).isoformat(),
    }
    record.update(overrides)
    return record


class Reader:
    def __init__(self, record):
        self.record = dict(record)
        self.calls = []

    def get(self, *, action_id, user_id):
        self.calls.append((action_id, user_id))
        if action_id != self.record.get("actionId") or user_id != self.record.get(
            "userId"
        ):
            return None
        return dict(self.record)


class ApprovalService:
    def __init__(self, reader: Reader, decoded: CapabilityGrant):
        self.reader = reader
        self.decoded = decoded
        self.calls = []

    def decode(self, token):
        self.calls.append(("decode", token))
        return self.decoded

    def approve(self, **kwargs):
        self.calls.append(("approve", kwargs))
        record = dict(self.reader.record)
        record.update(
            state=ActionState.APPROVED.value,
            revision=record["revision"] + 1,
            approvedActionId=record["actionId"],
            approvedDraftRevision=record["draftRevision"],
            approvedArgsHash=record["payloadHash"],
            approvedAt=NOW.isoformat(),
        )
        self.reader.record = record
        return dict(record)

    def reject(self, **kwargs):
        self.calls.append(("reject", kwargs))
        record = dict(self.reader.record)
        record.update(
            state=ActionState.REJECTED.value,
            revision=record["revision"] + 1,
            rejectedAt=NOW.isoformat(),
        )
        self.reader.record = record
        return dict(record)


class Receipt:
    def record(self):
        return {
            "providerMessageId": "gmail-message-1",
            "providerThreadId": "gmail-thread-1",
            "messageId": "<po-aaaaaaaaaaaaaaaaaaaaaaaa@personal-operator.invalid>",
            "connectionId": "google_conn_1234",
            "accountEmail": "founder@example.com",
            "senderAddress": "founder@example.com",
            "recipient": "person@example.net",
            "payloadHash": HASH,
            "executedAt": NOW.isoformat(),
            "labels": ["SENT"],
        }


class Executor:
    def __init__(self, events, reader):
        self.events = events
        self.reader = reader

    def execute(self, action):
        self.events.append(("execute", dict(action)))
        assert action["state"] == ActionState.APPROVED.value
        self.reader.record.update(
            state=ActionState.CONFIRMED.value,
            revision=action["revision"] + 2,
            effectReceipt=Receipt().record(),
        )
        return Receipt()


def service(*, record=None, decoded=None, now=lambda: NOW):
    reader = Reader(record or pending())
    approvals = ApprovalService(reader, decoded or grant())
    events = []

    def executor_factory(action):
        events.append(("prepare-executor", dict(action)))
        return Executor(events, reader)

    return (
        ApprovalWebService(
            approval_service=approvals,
            action_reader=reader,
            executor_factory=executor_factory,
            founder_user_ids={USER},
            now=now,
        ),
        approvals,
        reader,
        events,
    )


def test_preview_decodes_and_strongly_reads_without_transition_or_effect():
    web, approvals, reader, events = service()

    result = web.preview(token="signed-token", acting_user_id=USER)

    assert result == {
        "actionId": ACTION_ID,
        "userId": USER,
        "state": "APPROVAL_PENDING",
        "revision": 2,
        "draftRevision": 4,
        "args": ARGS,
        "payloadHash": HASH,
        "expiresAt": (NOW + timedelta(minutes=5)).isoformat(),
    }
    assert approvals.calls == [("decode", "signed-token")]
    assert reader.calls == [(ACTION_ID, USER)]
    assert events == []


@pytest.mark.parametrize(
    "record_overrides,grant_overrides",
    [
        ({"state": "APPROVED"}, {}),
        ({"approvalId": "appr_different_123456"}, {}),
        ({"approvalActionId": "action_other123"}, {}),
        ({"approvalDraftRevision": 5}, {}),
        ({"approvalArgsHash": "0" * 64}, {}),
        ({"payloadHash": "0" * 64}, {}),
        ({"resource": RESOURCE + "-other"}, {}),
        ({"approvalExpiresAt": (NOW + timedelta(minutes=4)).isoformat()}, {}),
        ({}, {"user_id": OTHER}),
        ({}, {"action_id": "action_other123"}),
    ],
)
def test_preview_rejects_every_cross_binding_or_stale_pending_record(
    record_overrides, grant_overrides
):
    web, _, _, events = service(
        record=pending(**record_overrides), decoded=grant(**grant_overrides)
    )

    with pytest.raises((CapabilityDenied, PermissionError, ValueError)):
        web.preview(token="signed-token", acting_user_id=USER)

    assert events == []


def test_preview_rejects_expired_grant_without_expiring_or_consuming_action():
    web, approvals, reader, events = service(
        record=pending(approvalExpiresAt=NOW.isoformat()),
        decoded=grant(expires_at=NOW),
    )

    with pytest.raises(CapabilityDenied, match="expired"):
        web.preview(token="signed-token", acting_user_id=USER)

    assert approvals.calls == [("decode", "signed-token")]
    assert reader.record["state"] == "APPROVAL_PENDING"
    assert events == []


def test_approve_validates_exact_pending_revision_then_prepares_and_executes_once():
    web, approvals, _, events = service()

    result = web.approve(
        action_id=ACTION_ID,
        revision=2,
        acting_user_id=USER,
        token="signed-token",
        args=ARGS,
    )

    assert [event[0] for event in events] == ["prepare-executor", "execute"]
    assert [call[0] for call in approvals.calls] == ["decode", "approve"]
    assert result["actionId"] == ACTION_ID
    assert result["userId"] == USER
    assert result["state"] == "CONFIRMED"
    assert result["receipt"]["providerMessageId"] == "gmail-message-1"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"acting_user_id": OTHER},
        {"action_id": "action_other123"},
        {"revision": 3},
        {"args": {**ARGS, "body": "tampered"}},
    ],
)
def test_approve_wrong_user_action_revision_or_args_never_prepares_executor(kwargs):
    web, approvals, _, events = service()
    values = {
        "action_id": ACTION_ID,
        "revision": 2,
        "acting_user_id": USER,
        "token": "signed-token",
        "args": ARGS,
    }
    values.update(kwargs)

    with pytest.raises((CapabilityDenied, PermissionError, ValueError)):
        web.approve(**values)

    assert not [call for call in approvals.calls if call[0] == "approve"]
    assert events == []


def test_reject_is_founder_and_exact_pending_revision_bound():
    web, approvals, _, events = service()

    result = web.reject(
        action_id=ACTION_ID,
        revision=2,
        acting_user_id=USER,
    )

    assert result == {
        "actionId": ACTION_ID,
        "userId": USER,
        "state": "REJECTED",
        "revision": 3,
    }
    assert approvals.calls[-1][0] == "reject"
    assert events == []

    other, other_approvals, _, other_events = service()
    with pytest.raises(PermissionError):
        other.reject(
            action_id=ACTION_ID,
            revision=2,
            acting_user_id=OTHER,
        )
    assert not [call for call in other_approvals.calls if call[0] == "reject"]
    assert other_events == []


class WorkspaceStore:
    def __init__(self, files):
        self.files = files
        self.calls = []

    def workspace_files(self, user_id):
        self.calls.append(user_id)
        return dict(self.files)


class Runtime:
    def __init__(self, status):
        self.value = status
        self.calls = []

    def status(self, user_id):
        self.calls.append(user_id)
        return dict(self.value)


def test_workspace_returns_only_exact_user_paths_sizes_and_bounded_runtime_status():
    workspace = WorkspaceStore(
        {"memory.md": b"hello", "notes/plan.md": b"# plan\n"}
    )
    runtime = Runtime(
        {
            "userId": USER,
            "sessionId": "sensitive-session",
            "runtimeArn": "sensitive-runtime",
            "state": "IDLE",
            "workspaceReceipt": {
                "generation": "g-12345678-1234-4123-8123-123456789abc",
                "manifestSha256": "a" * 64,
            },
        }
    )

    result = WorkspaceService(
        workspace_store=workspace, runtime_driver=runtime
    ).get(USER)

    assert result == {
        "userId": USER,
        "runtimeState": "IDLE",
        "workspaceReceipt": {
            "generation": "g-12345678-1234-4123-8123-123456789abc",
            "manifestSha256": "a" * 64,
        },
        "files": [
            {"path": "memory.md", "size": 5},
            {"path": "notes/plan.md", "size": 7},
        ],
    }
    assert workspace.calls == [USER]
    assert runtime.calls == [USER]
    assert "sessionId" not in result and "runtimeArn" not in result


@pytest.mark.parametrize(
    "files,status",
    [
        ({"../other/secret": b"x"}, {"userId": USER, "state": "IDLE", "workspaceReceipt": None}),
        ({"memory.md": "not-bytes"}, {"userId": USER, "state": "IDLE", "workspaceReceipt": None}),
        ({}, {"userId": OTHER, "state": "IDLE", "workspaceReceipt": None}),
        ({}, {"userId": USER, "state": "ROOT", "workspaceReceipt": None}),
    ],
)
def test_workspace_fails_closed_on_escaped_files_or_cross_user_status(files, status):
    with pytest.raises((ValueError, RuntimeError)):
        WorkspaceService(
            workspace_store=WorkspaceStore(files), runtime_driver=Runtime(status)
        ).get(USER)


class SweepTable:
    def __init__(self, items, *, intents=()):
        self.items = list(items)
        self.intents = list(intents)
        self.scans = []
        self.deleted = []
        self.cursor_items = {}

    def scan(self, **kwargs):
        self.scans.append(kwargs)
        expression = kwargs.get("FilterExpression", "")
        if any("userId" in item and "PK" not in item for item in self.items):
            selected = self.items
        elif "recordType" in expression or ":pending" in kwargs.get(
            "ExpressionAttributeValues", {}
        ):
            selected = self.intents
        else:
            selected = self.items
        return {"Items": [dict(item) for item in selected]}

    def delete_item(self, **kwargs):
        self.deleted.append(kwargs)
        return {}

    def get_item(self, **kwargs):
        key = kwargs["Key"]
        item = self.cursor_items.get((key["PK"], key["SK"]))
        return {} if item is None else {"Item": dict(item)}

    def update_item(self, **kwargs):
        key = kwargs["Key"]
        values = kwargs["ExpressionAttributeValues"]
        identity = (key["PK"], key["SK"])
        current = self.cursor_items.get(identity)
        generation = 0 if current is None else current["generation"]
        if generation != values[":expectedGeneration"]:
            raise RuntimeError("conditional conflict")
        item = {
            **key,
            "recordType": values[":recordType"],
            "cursor": values[":cursor"],
            "generation": values[":nextGeneration"],
        }
        self.cursor_items[identity] = item
        return {"Attributes": dict(item)}


class SweepDeletion:
    def __init__(self, pending=(), active=()):
        self.pending = set(pending)
        self.active = set(active)
        self.calls = []
        self.inactive_calls = []

    def reconcile(self, user_id):
        self.calls.append(user_id)
        if user_id in self.pending:
            raise DeletionPending("still stopping")
        return {"status": "deleted", "userId": user_id}

    def delete_inactive(
        self,
        user_id,
        *,
        observed_updated_at_ms,
        observed_revision,
        inactive_before_ms,
    ):
        self.inactive_calls.append(
            {
                "userId": user_id,
                "observedUpdatedAtMs": observed_updated_at_ms,
                "observedRevision": observed_revision,
                "inactiveBeforeMs": inactive_before_ms,
            }
        )
        if user_id in self.pending:
            raise DeletionPending("still stopping")
        if user_id in self.active:
            return {"status": "active", "userId": user_id}
        return {"status": "expired", "userId": user_id}


def test_retention_sweep_conditionally_expires_records_and_resumes_tombstones():
    control = SweepTable(
        [
            {"PK": "CONNECT#" + "a" * 64, "SK": "CONNECT", "ttl": 99},
            {"PK": "SESSION#" + "b" * 64, "SK": "SESSION", "ttl": 100},
        ]
    )
    runtime = SweepTable(
        [
            {
                "userId": "founder-1",
                "state": "DELETING",
                "tombstonedAt": 1,
                "purgeReason": "ACCOUNT_DELETION",
            },
            {
                "userId": "other-user",
                "state": "DELETING",
                "tombstonedAt": 2,
                "purgeReason": "ACCOUNT_DELETION",
            },
        ]
    )
    deletion = SweepDeletion(pending={"other-user"})

    result = RetentionSweepService(
        control_table=control,
        runtime_table=runtime,
        deletion=deletion,
        now=lambda: 100,
    ).sweep()

    assert result == {
        "status": "ok",
        "expired": 2,
        "deletionsCompleted": 1,
        "deletionsPending": 1,
        "inactiveCandidates": 0,
        "inactivityFencesLost": 0,
    }
    assert deletion.calls == ["founder-1", "other-user"]
    assert [call["Key"] for call in control.deleted] == [
        {"PK": "CONNECT#" + "a" * 64, "SK": "CONNECT"},
        {"PK": "SESSION#" + "b" * 64, "SK": "SESSION"},
    ]
    assert all(
        call["ConditionExpression"] == "#ttl=:ttl" for call in control.deleted
    )


def test_retention_sweep_fails_loudly_when_action_maintenance_reports_failure():
    class Actions:
        def __init__(self):
            self.calls = 0

        def run(self):
            self.calls += 1
            return {
                "status": "ok",
                "processed": 7,
                "failed": 1,
                "hasMore": True,
            }

    actions = Actions()
    with pytest.raises(RuntimeError, match="action maintenance failed"):
        RetentionSweepService(
            control_table=SweepTable([]),
            runtime_table=SweepTable([]),
            deletion=SweepDeletion(),
            action_maintenance=actions,
            now=lambda: 4_000_000,
        ).sweep()

    assert actions.calls == 1


def test_pending_account_deletion_advances_before_poisoned_action_maintenance():
    class PoisonedActions:
        def __init__(self):
            self.calls = 0

        def run(self):
            self.calls += 1
            raise RuntimeError("poisoned uncertain action")

    intent = {
        "PK": "DELETION#" + "a" * 64,
        "SK": "DELETION",
        "recordType": "DELETION_INTENT",
        "userId": "founder-1",
        "purgeReason": "ACCOUNT_DELETION",
        "deletionStatus": "PENDING",
        "requestedAt": 1,
    }
    deletion = SweepDeletion(pending={"founder-1"})
    actions = PoisonedActions()

    with pytest.raises(RuntimeError, match="poisoned uncertain action"):
        RetentionSweepService(
            control_table=SweepTable([], intents=[intent]),
            runtime_table=SweepTable([]),
            deletion=deletion,
            action_maintenance=actions,
            now=lambda: 4_000_000,
        ).sweep()

    assert deletion.calls == ["founder-1"]
    assert actions.calls == 1


def test_retention_sweep_rejects_invalid_action_maintenance_result():
    class Actions:
        @staticmethod
        def run():
            return {"status": "ok", "processed": -1, "failed": 0, "hasMore": False}

    with pytest.raises(RuntimeError, match="action maintenance"):
        RetentionSweepService(
            control_table=SweepTable([]),
            runtime_table=SweepTable([]),
            deletion=SweepDeletion(),
            action_maintenance=Actions(),
            now=lambda: 4_000_000,
        ).sweep()


def test_retention_sweep_uses_30_day_millisecond_cutoff_and_skips_activity_race():
    now_seconds = 4_000_000
    inactive_before_ms = (now_seconds - 30 * 24 * 60 * 60) * 1_000
    clock_calls = []

    def clock():
        clock_calls.append(now_seconds)
        return now_seconds

    control = SweepTable([])
    runtime = SweepTable(
        [
            {
                "userId": "inactive-user",
                "state": "IDLE",
                "updatedAt": inactive_before_ms,
                "revision": 7,
            },
            {
                "userId": "raced-user",
                "state": "COLD",
                "updatedAt": inactive_before_ms - 1,
                "revision": 8,
            },
        ]
    )
    deletion = SweepDeletion(active={"raced-user"})

    result = RetentionSweepService(
        control_table=control,
        runtime_table=runtime,
        deletion=deletion,
        now=clock,
    ).sweep()

    assert result == {
        "status": "ok",
        "expired": 0,
        "deletionsCompleted": 1,
        "deletionsPending": 0,
        "inactiveCandidates": 2,
        "inactivityFencesLost": 1,
    }
    scan = runtime.scans[0]
    assert scan["ExpressionAttributeValues"][":inactiveBefore"] == inactive_before_ms
    assert scan["ConsistentRead"] is True
    assert scan["Limit"] == 25
    assert deletion.calls == []
    assert clock_calls == [now_seconds]
    assert deletion.inactive_calls == [
        {
            "userId": "inactive-user",
            "observedUpdatedAtMs": inactive_before_ms,
            "observedRevision": 7,
            "inactiveBeforeMs": inactive_before_ms,
        },
        {
            "userId": "raced-user",
            "observedUpdatedAtMs": inactive_before_ms - 1,
            "observedRevision": 8,
            "inactiveBeforeMs": inactive_before_ms,
        },
    ]


@pytest.mark.parametrize(
    "field,value",
    [("updatedAt", True), ("updatedAt", 1.5), ("revision", True), ("revision", 0)],
)
def test_retention_rejects_malformed_inactive_fence_before_any_deletion(field, value):
    now_seconds = 4_000_000
    inactive_before_ms = (now_seconds - 30 * 24 * 60 * 60) * 1_000
    item = {
        "userId": "inactive-user",
        "state": "IDLE",
        "updatedAt": inactive_before_ms,
        "revision": 7,
    }
    item[field] = value
    control = SweepTable([])
    runtime = SweepTable([item])
    deletion = SweepDeletion()

    with pytest.raises(RuntimeError, match="deletion reconciliation scan"):
        RetentionSweepService(
            control_table=control,
            runtime_table=runtime,
            deletion=deletion,
            now=lambda: now_seconds,
        ).sweep()

    assert deletion.calls == []
    assert deletion.inactive_calls == []


def test_retention_sweep_rejects_malformed_scan_results_before_unbounded_deletion():
    control = SweepTable(
        [{"PK": "SESSION#" + "a" * 64, "SK": "SESSION", "ttl": 101}]
    )
    runtime = SweepTable([])
    with pytest.raises(RuntimeError, match="expired record"):
        RetentionSweepService(
            control_table=control,
            runtime_table=runtime,
            deletion=SweepDeletion(),
            now=lambda: 100,
        ).sweep()
    assert control.deleted == []


def test_retention_paginates_past_filtered_active_rows_to_later_workspace_candidate():
    now_seconds = 4_000_000
    inactive_before_ms = (now_seconds - 30 * 24 * 60 * 60) * 1_000

    class RuntimePages:
        def __init__(self):
            self.scans = []

        def scan(self, **kwargs):
            self.scans.append(kwargs)
            if len(self.scans) == 1:
                return {
                    "Items": [],
                    "LastEvaluatedKey": {"userId": "active-user-025"},
                }
            assert kwargs["ExclusiveStartKey"] == {"userId": "active-user-025"}
            return {
                "Items": [
                    {
                        "userId": "later-inactive",
                        "state": "IDLE",
                        "updatedAt": inactive_before_ms,
                        "revision": 9,
                    }
                ]
            }

    runtime = RuntimePages()
    deletion = SweepDeletion()

    result = RetentionSweepService(
        control_table=SweepTable([]),
        runtime_table=runtime,
        deletion=deletion,
        now=lambda: now_seconds,
    ).sweep()

    assert len(runtime.scans) == 2
    assert result["inactiveCandidates"] == 1
    assert result["deletionsCompleted"] == 1
    assert deletion.inactive_calls[0]["userId"] == "later-inactive"


def test_retention_intent_scan_resumes_after_bounded_invocation():
    class IntentPages(SweepTable):
        def scan(self, **kwargs):
            self.scans.append(kwargs)
            values = kwargs.get("ExpressionAttributeValues", {})
            if ":now" in values:
                return {"Items": []}
            if ":intent" not in values:
                return {"Items": []}
            if "ExclusiveStartKey" not in kwargs:
                return {
                    "Items": [],
                    "LastEvaluatedKey": {"PK": "OTHER#head", "SK": "HEAD"},
                }
            assert kwargs["ExclusiveStartKey"] == {
                "PK": "OTHER#head",
                "SK": "HEAD",
            }
            return {
                "Items": [
                    {
                        "PK": "DELETION#" + "a" * 64,
                        "SK": "DELETION",
                        "recordType": "DELETION_INTENT",
                        "userId": "later-user",
                        "purgeReason": "ACCOUNT_DELETION",
                        "deletionStatus": "PENDING",
                        "requestedAt": 1,
                    }
                ]
            }

    control = IntentPages([])
    runtime = SweepTable([])
    deletion = SweepDeletion()

    first = RetentionSweepService(
        control_table=control,
        runtime_table=runtime,
        deletion=deletion,
        now=lambda: 4_000_000,
        max_scan_pages=1,
    ).sweep()
    second = RetentionSweepService(
        control_table=control,
        runtime_table=runtime,
        deletion=deletion,
        now=lambda: 4_000_000,
        max_scan_pages=1,
    ).sweep()

    assert first["deletionsCompleted"] == 0
    assert second["deletionsCompleted"] == 1
    assert deletion.calls == ["later-user"]


def test_retention_runtime_scan_resumes_after_bounded_invocation():
    now_seconds = 4_000_000
    inactive_before_ms = (now_seconds - 30 * 24 * 60 * 60) * 1_000

    class RuntimePages:
        def __init__(self):
            self.scans = []

        def scan(self, **kwargs):
            self.scans.append(kwargs)
            if "ExclusiveStartKey" not in kwargs:
                return {
                    "Items": [],
                    "LastEvaluatedKey": {"userId": "active-user"},
                }
            assert kwargs["ExclusiveStartKey"] == {"userId": "active-user"}
            return {
                "Items": [
                    {
                        "userId": "later-inactive",
                        "state": "IDLE",
                        "updatedAt": inactive_before_ms,
                        "revision": 9,
                    }
                ]
            }

    control = SweepTable([])
    runtime = RuntimePages()
    deletion = SweepDeletion()

    first = RetentionSweepService(
        control_table=control,
        runtime_table=runtime,
        deletion=deletion,
        now=lambda: now_seconds,
        max_scan_pages=1,
    ).sweep()
    second = RetentionSweepService(
        control_table=control,
        runtime_table=runtime,
        deletion=deletion,
        now=lambda: now_seconds,
        max_scan_pages=1,
    ).sweep()

    assert first["inactiveCandidates"] == 0
    assert second["inactiveCandidates"] == 1
    assert [call["userId"] for call in deletion.inactive_calls] == [
        "later-inactive"
    ]


def test_retention_does_not_advance_past_malformed_runtime_candidate():
    now_seconds = 4_000_000

    class RuntimePage:
        @staticmethod
        def scan(**_kwargs):
            return {
                "Items": [
                    {
                        "userId": "broken-user",
                        "state": "IDLE",
                        "updatedAt": True,
                        "revision": 1,
                    }
                ],
                "LastEvaluatedKey": {"userId": "broken-user"},
            }

    control = SweepTable([])
    with pytest.raises(RuntimeError, match="deletion reconciliation scan"):
        RetentionSweepService(
            control_table=control,
            runtime_table=RuntimePage(),
            deletion=SweepDeletion(),
            now=lambda: now_seconds,
            max_scan_pages=1,
        ).sweep()

    assert (
        "SYSTEM#RETENTION_SWEEP",
        "CURSOR#RUNTIME_CANDIDATES",
    ) not in control.cursor_items


def test_retention_excludes_completed_runtime_tombstone_and_completed_intent():
    control = SweepTable(
        [],
        intents=[
            {
                "PK": "DELETION#" + "a" * 64,
                "SK": "DELETION",
                "recordType": "DELETION_INTENT",
                "userId": "completed-intent-user",
                "purgeReason": "ACCOUNT_DELETION",
                "deletionStatus": "COMPLETED",
                "requestedAt": 1,
                "finalizingAt": 2,
                "completedAt": 3,
            }
        ],
    )
    runtime = SweepTable(
        [
            {
                "userId": "completed-runtime-user",
                "state": "DELETING",
                "tombstonedAt": 1,
                "purgeReason": "ACCOUNT_DELETION",
                "purgeCompletedAt": 2,
            }
        ]
    )
    deletion = SweepDeletion()

    result = RetentionSweepService(
        control_table=control,
        runtime_table=runtime,
        deletion=deletion,
        now=lambda: 4_000_000,
    ).sweep()

    assert result["deletionsCompleted"] == 0
    assert result["deletionsPending"] == 0
    assert deletion.calls == []
    assert deletion.inactive_calls == []


def test_retention_reconciles_intent_without_runtime_tombstone_and_continues_after_poison():
    control = SweepTable(
        [],
        intents=[
            {
                "PK": "DELETION#" + "a" * 64,
                "SK": "DELETION",
                "recordType": "DELETION_INTENT",
                "userId": "a-poisoned-user",
                "purgeReason": "ACCOUNT_DELETION",
                "deletionStatus": "PENDING",
                "requestedAt": 1,
            },
            {
                "PK": "DELETION#" + "b" * 64,
                "SK": "DELETION",
                "recordType": "DELETION_INTENT",
                "userId": "b-healthy-user",
                "purgeReason": "ACCOUNT_DELETION",
                "deletionStatus": "PENDING",
                "requestedAt": 2,
            },
        ],
    )
    deletion = SweepDeletion(pending={"a-poisoned-user"})

    result = RetentionSweepService(
        control_table=control,
        runtime_table=SweepTable([]),
        deletion=deletion,
        now=lambda: 4_000_000,
    ).sweep()

    assert deletion.calls == ["a-poisoned-user", "b-healthy-user"]
    assert result["deletionsPending"] == 1
    assert result["deletionsCompleted"] == 1


def test_retention_reconciles_pending_and_finalizing_but_never_completed_intents():
    control = SweepTable(
        [],
        intents=[
            {
                "PK": "DELETION#" + "a" * 64,
                "SK": "DELETION",
                "recordType": "DELETION_INTENT",
                "userId": "pending-user",
                "purgeReason": "ACCOUNT_DELETION",
                "deletionStatus": "PENDING",
                "requestedAt": 1,
            },
            {
                "PK": "DELETION#" + "b" * 64,
                "SK": "DELETION",
                "recordType": "DELETION_INTENT",
                "userId": "finalizing-user",
                "purgeReason": "ACCOUNT_DELETION",
                "deletionStatus": "FINALIZING",
                "requestedAt": 1,
                "finalizingAt": 2,
            },
            {
                "PK": "DELETION#" + "c" * 64,
                "SK": "DELETION",
                "recordType": "DELETION_INTENT",
                "userId": "completed-user",
                "purgeReason": "ACCOUNT_DELETION",
                "deletionStatus": "COMPLETED",
                "requestedAt": 1,
                "finalizingAt": 2,
                "completedAt": 3,
            },
        ],
    )
    deletion = SweepDeletion(pending={"pending-user"})

    result = RetentionSweepService(
        control_table=control,
        runtime_table=SweepTable([]),
        deletion=deletion,
        now=lambda: 4_000_000,
    ).sweep()

    assert deletion.calls == ["finalizing-user", "pending-user"]
    assert result["deletionsCompleted"] == 1
    assert result["deletionsPending"] == 1
    intent_scan = next(
        scan for scan in control.scans if ":intent" in scan["ExpressionAttributeValues"]
    )
    assert intent_scan["ConsistentRead"] is True
    assert intent_scan["ExpressionAttributeValues"][":finalizing"] == "FINALIZING"


def test_retention_resumes_workspace_expiry_with_its_original_atomic_cutoff():
    deletion = SweepDeletion()
    runtime = SweepTable(
        [
            {
                "userId": "workspace-pending",
                "state": "DELETING",
                "tombstonedAt": 3_000,
                "purgeReason": "WORKSPACE_EXPIRY",
                "purgeObservedUpdatedAt": 1_000,
                "purgeObservedRevision": 7,
                "purgeInactiveBefore": 2_000,
            }
        ]
    )

    result = RetentionSweepService(
        control_table=SweepTable([]),
        runtime_table=runtime,
        deletion=deletion,
        now=lambda: 4_000_000,
    ).sweep()

    assert result["deletionsCompleted"] == 1
    assert deletion.inactive_calls == [
        {
            "userId": "workspace-pending",
            "observedUpdatedAtMs": 1_000,
            "observedRevision": 7,
            "inactiveBeforeMs": 2_000,
        }
    ]


def test_retention_rejects_invalid_runtime_cursor_before_following_it():
    class InvalidCursorRuntime:
        def __init__(self):
            self.calls = 0

        def scan(self, **_kwargs):
            self.calls += 1
            return {"Items": [], "LastEvaluatedKey": {"userId": "../escape"}}

    runtime = InvalidCursorRuntime()
    with pytest.raises(RuntimeError, match="pagination"):
        RetentionSweepService(
            control_table=SweepTable([]),
            runtime_table=runtime,
            deletion=SweepDeletion(),
            now=lambda: 4_000_000,
        ).sweep()

    assert runtime.calls == 1
