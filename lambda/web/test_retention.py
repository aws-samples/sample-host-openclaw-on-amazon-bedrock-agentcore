from __future__ import annotations

import io
import json
import zipfile

import pytest

from .retention import (
    DeletionCoordinator,
    DeletionPending,
    DynamoExpirySweeper,
    DynamoSweepCursorStore,
    ExportBoundaryError,
    SweepCursorLease,
    UserExporter,
    assert_logically_live,
)


USER = "user_founder"


class CursorTable:
    def __init__(self, *, lose_response=False):
        self.items = {}
        self.lose_response = lose_response

    def get_item(self, **kwargs):
        item = self.items.get((kwargs["Key"]["PK"], kwargs["Key"]["SK"]))
        return {} if item is None else {"Item": dict(item)}

    def update_item(self, **kwargs):
        key = kwargs["Key"]
        values = kwargs["ExpressionAttributeValues"]
        identity = (key["PK"], key["SK"])
        current = self.items.get(identity)
        generation = 0 if current is None else current["generation"]
        if generation != values[":expectedGeneration"]:
            raise RuntimeError("conditional conflict")
        item = {
            **key,
            "recordType": values[":recordType"],
            "cursor": values[":cursor"],
            "generation": values[":nextGeneration"],
        }
        self.items[identity] = item
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("response lost after commit")
        return {"Attributes": dict(item)}


class Recorder:
    def __init__(self, events, name, *, error=None, result=None):
        self.events = events
        self.name = name
        self.error = error
        self.result = result

    def __getattr__(self, method):
        def called(*args, **kwargs):
            self.events.append((self.name, method, args, kwargs))
            if self.error:
                raise self.error
            return self.result

        return called


class DeletionSessionRecorder:
    def __init__(self, events, clock):
        self.events = events
        self.clock = clock
        self.intent = None

    def begin_deletion(self, user_id):
        self.events.append(("sessions", "begin_deletion", (user_id,), {}))
        if self.intent is None:
            self.intent = {
                "userId": user_id,
                "deletionStatus": "PENDING",
                "purgeReason": "ACCOUNT_DELETION",
                "requestedAt": self.clock[0],
                "finalizingAt": None,
                "completedAt": None,
            }
        return dict(self.intent)

    def get_deletion_intent(self, user_id):
        assert self.intent is None or self.intent["userId"] == user_id
        return dict(self.intent) if self.intent is not None else None

    def revoke_all(self, user_id):
        self.events.append(("sessions", "revoke_all", (user_id,), {}))

    def mark_deletion_finalizing(self, user_id):
        self.events.append(("sessions", "mark_deletion_finalizing", (user_id,), {}))
        self.intent.update(
            deletionStatus="FINALIZING",
            finalizingAt=self.clock[0],
        )
        return dict(self.intent)

    def complete_deletion(self, user_id, *, finalizing_before_ms):
        self.events.append(("sessions", "complete_deletion", (user_id,), {}))
        assert self.intent["finalizingAt"] <= finalizing_before_ms
        self.intent = {
            "userId": user_id,
            "deletionStatus": "COMPLETED",
            "purgeReason": "ACCOUNT_DELETION",
            "requestedAt": None,
            "finalizingAt": None,
            "completedAt": self.clock[0],
        }
        return dict(self.intent)


def test_logical_expiry_rejects_before_dynamodb_ttl_deletes_item():
    assert assert_logically_live({"ttl": 101}, now=100)["ttl"] == 101
    with pytest.raises(ValueError, match="expired"):
        assert_logically_live({"ttl": 100}, now=100)
    with pytest.raises(ValueError, match="TTL"):
        assert_logically_live({"ttl": True}, now=100)


def test_scheduled_sweep_conditionally_deletes_only_allowlisted_expired_records():
    class Table:
        def __init__(self):
            self.deleted = []

        def scan(self, **kwargs):
            assert kwargs["ExpressionAttributeValues"] == {":now": 1_000}
            return {
                "Items": [
                    {"PK": "SESSION#" + "a" * 64, "SK": "SESSION", "ttl": 900},
                    {
                        "PK": "USER#user_founder",
                        "SK": "ACTION#action_12345678",
                        "state": "CONFIRMED",
                        "ttl": 950,
                    },
                    {
                        "PK": "USER#user_founder",
                        "SK": "ACTION#action_uncertain1",
                        "state": "UNCERTAIN",
                        "ttl": 951,
                    },
                    {
                        "PK": "USER#user_founder",
                        "SK": "TELEGRAM_CALLBACK#" + "b" * 64,
                        "recordType": "TELEGRAM_CARD_ACTION",
                        "userId": "user_founder",
                        "ttl": 952,
                    },
                    {
                        "PK": "SCANUSER#" + "c" * 64,
                        "SK": "SCAN#00000000001700000000#" + "d" * 32,
                        "recordType": "PILOT_SCAN_MEASUREMENT_V1",
                        "ttl": 953,
                    },
                ]
            }

        def delete_item(self, **kwargs):
            self.deleted.append(kwargs)
            return {}

    table = Table()
    result = DynamoExpirySweeper(table, now=lambda: 1_000).sweep()

    assert result == {"status": "ok", "expired": 5}
    assert len(table.deleted) == 5
    assert all(
        call["ConditionExpression"] == "#ttl=:ttl"
        for call in table.deleted
    )


def test_retention_cursor_reconciles_response_loss_and_rejects_stale_writer():
    table = CursorTable(lose_response=True)
    store = DynamoSweepCursorStore(table)
    lease = store.load("expiry")
    cursor = {"PK": "SESSION#" + "a" * 64, "SK": "SESSION"}

    saved = store.save(lease, cursor)

    assert saved == SweepCursorLease("expiry", cursor, 1)
    assert store.load("expiry") == saved
    with pytest.raises(RuntimeError, match="cursor write failed"):
        store.save(lease, None)


@pytest.mark.parametrize("response", [[], {"Item": None}])
def test_retention_cursor_load_fails_closed_on_malformed_response(response):
    class Table:
        @staticmethod
        def get_item(**_kwargs):
            return response

    with pytest.raises(RuntimeError, match="cursor read"):
        DynamoSweepCursorStore(Table()).load("expiry")


def test_expiry_sweep_persists_progress_and_resumes_after_page_cap():
    class Table(CursorTable):
        def __init__(self):
            super().__init__()
            self.scans = []
            self.deleted = []

        def scan(self, **kwargs):
            self.scans.append(kwargs)
            if "ExclusiveStartKey" not in kwargs:
                return {
                    "Items": [
                        {
                            "PK": "SESSION#" + "a" * 64,
                            "SK": "SESSION",
                            "ttl": 900,
                        }
                    ],
                    "LastEvaluatedKey": {
                        "PK": "SESSION#" + "a" * 64,
                        "SK": "SESSION",
                    },
                }
            assert kwargs["ExclusiveStartKey"] == {
                "PK": "SESSION#" + "a" * 64,
                "SK": "SESSION",
            }
            return {
                "Items": [
                    {
                        "PK": "SESSION#" + "b" * 64,
                        "SK": "SESSION",
                        "ttl": 901,
                    }
                ]
            }

        def delete_item(self, **kwargs):
            self.deleted.append(kwargs)
            return {}

    table = Table()
    store = DynamoSweepCursorStore(table)
    sweeper = DynamoExpirySweeper(
        table,
        now=lambda: 1_000,
        cursor_store=store,
        max_pages=1,
    )

    assert sweeper.sweep() == {"status": "ok", "expired": 1}
    assert store.load("expiry").cursor == {
        "PK": "SESSION#" + "a" * 64,
        "SK": "SESSION",
    }
    assert sweeper.sweep() == {"status": "ok", "expired": 1}
    assert store.load("expiry").cursor is None
    assert len(table.deleted) == 2


def test_expiry_sweep_stops_successfully_at_item_cap_after_saving_progress():
    class Table(CursorTable):
        def __init__(self):
            super().__init__()
            self.scans = 0
            self.deleted = []

        def scan(self, **_kwargs):
            self.scans += 1
            marker = str(self.scans)
            return {
                "Items": [
                    {
                        "PK": "SESSION#" + marker * 64,
                        "SK": "SESSION",
                        "ttl": 900,
                    }
                ],
                "LastEvaluatedKey": {
                    "PK": "SESSION#" + marker * 64,
                    "SK": "SESSION",
                },
            }

        def delete_item(self, **kwargs):
            self.deleted.append(kwargs)
            return {}

    table = Table()
    store = DynamoSweepCursorStore(table)
    sweeper = DynamoExpirySweeper(
        table,
        now=lambda: 1_000,
        cursor_store=store,
    )
    sweeper.MAX_ITEMS = 1

    assert sweeper.sweep() == {"status": "ok", "expired": 1}
    assert table.scans == 1
    assert store.load("expiry").cursor == {
        "PK": "SESSION#" + "1" * 64,
        "SK": "SESSION",
    }


def test_scheduled_sweep_fails_closed_on_unrecognized_or_nonterminal_ttl_record():
    class Table:
        def __init__(self, item):
            self.item = item
            self.deleted = []

        def scan(self, **_kwargs):
            return {"Items": [self.item]}

        def delete_item(self, **kwargs):
            self.deleted.append(kwargs)

    for item in (
        {"PK": "OTHER#x", "SK": "UNKNOWN", "ttl": 1},
        {
            "PK": "USER#user_founder",
            "SK": "ACTION#action_12345678",
            "state": "APPROVED",
            "ttl": 1,
        },
        {
            "PK": "USER#user_founder",
            "SK": "TELEGRAM_CALLBACK#" + "b" * 64,
            "recordType": "forged",
            "userId": "user_founder",
            "ttl": 1,
        },
    ):
        table = Table(item)
        with pytest.raises(RuntimeError, match="allowlisted"):
            DynamoExpirySweeper(table, now=lambda: 1_000).sweep()
        assert table.deleted == []


@pytest.mark.parametrize("invalid_now", [True, 1.5, 0, -1])
def test_scheduled_sweep_requires_exact_positive_epoch_seconds(invalid_now):
    class Table:
        def scan(self, **_kwargs):
            return {"Items": []}

    with pytest.raises(RuntimeError, match="retention clock"):
        DynamoExpirySweeper(Table(), now=lambda: invalid_now).sweep()


def test_deletion_persists_intent_then_revokes_authority_before_runtime_and_data_removal():
    events = []
    clock = [1_000_000]
    coordinator = DeletionCoordinator(
        session_store=DeletionSessionRecorder(events, clock),
        connection_store=Recorder(events, "connections"),
        runtime_driver=Recorder(
            events,
            "runtime",
            result={
                "userId": USER,
                "state": "DELETING",
                "purgeReason": "ACCOUNT_DELETION",
                "purgeCompletedAt": 1_000,
            },
        ),
        workspace_store=Recorder(events, "workspace"),
        record_store=Recorder(events, "records"),
        footprint_store=Recorder(events, "footprint"),
        clock_ms=lambda: clock[0],
    )

    with pytest.raises(DeletionPending):
        coordinator.delete(USER)

    assert [(name, method) for name, method, _, _ in events] == [
        ("sessions", "begin_deletion"),
        ("sessions", "revoke_all"),
        ("connections", "revoke_all"),
        ("runtime", "purge"),
        ("workspace", "delete_namespace"),
        ("records", "delete_user_records"),
        ("footprint", "delete_user_records"),
        ("sessions", "mark_deletion_finalizing"),
    ]

    clock[0] += coordinator.FINALIZATION_GRACE_MS
    result = coordinator.reconcile(USER)

    assert result == {"status": "deleted", "userId": USER}
    assert [(name, method) for name, method, _, _ in events][-7:] == [
        ("sessions", "revoke_all"),
        ("connections", "revoke_all"),
        ("runtime", "purge"),
        ("workspace", "delete_namespace"),
        ("records", "delete_user_records"),
        ("footprint", "delete_user_records"),
        ("sessions", "complete_deletion"),
    ]


def test_deletion_drain_outlives_minimum_sts_credentials_with_race_margin():
    # AssumeRole credentials cannot be shorter than 15 minutes. The final purge
    # must happen strictly later, even when issuance races with the first
    # deletion fence, so exfiltrated credentials cannot recreate the namespace.
    assert DeletionCoordinator.FINALIZATION_GRACE_MS >= 30 * 60 * 1_000


def test_uncertain_runtime_purge_keeps_workspace_and_records_for_reconciliation():
    events = []
    clock = [1_000_000]
    coordinator = DeletionCoordinator(
        session_store=DeletionSessionRecorder(events, clock),
        connection_store=Recorder(events, "connections"),
        runtime_driver=Recorder(events, "runtime", error=TimeoutError("unknown")),
        workspace_store=Recorder(events, "workspace"),
        record_store=Recorder(events, "records"),
        footprint_store=Recorder(events, "footprint"),
        clock_ms=lambda: clock[0],
    )

    with pytest.raises(DeletionPending):
        coordinator.delete(USER)

    assert [(name, method) for name, method, _, _ in events] == [
        ("sessions", "begin_deletion"),
        ("sessions", "revoke_all"),
        ("connections", "revoke_all"),
        ("runtime", "purge"),
    ]


def test_deletion_intent_precedes_and_survives_first_fallible_revocation_failure():
    events = []

    class IntentThenFail:
        def __init__(self):
            self.intent = None

        def begin_deletion(self, user_id):
            events.append(("intent", user_id))
            self.intent = {
                "userId": user_id,
                "deletionStatus": "PENDING",
                "purgeReason": "ACCOUNT_DELETION",
                "requestedAt": 1_000_000,
                "finalizingAt": None,
                "completedAt": None,
            }
            return dict(self.intent)

        def get_deletion_intent(self, user_id):
            assert self.intent["userId"] == user_id
            return dict(self.intent)

        def revoke_all(self, user_id):
            events.append(("revoke", user_id))
            raise TimeoutError("revocation outcome unknown")

    coordinator = DeletionCoordinator(
        session_store=IntentThenFail(),
        connection_store=Recorder(events, "connections"),
        runtime_driver=Recorder(events, "runtime"),
        workspace_store=Recorder(events, "workspace"),
        record_store=Recorder(events, "records"),
        footprint_store=Recorder(events, "footprint"),
        clock_ms=lambda: 1_000_000,
    )

    with pytest.raises(DeletionPending):
        coordinator.delete(USER)

    assert events == [("intent", USER), ("revoke", USER)]


def test_finalization_removes_a_write_that_lands_after_the_first_purge():
    events = []
    clock = [1_000_000]
    sessions = DeletionSessionRecorder(events, clock)

    class Records:
        def __init__(self):
            self.values = ["before"]
            self.purges = 0

        def delete_user_records(self, user_id):
            assert user_id == USER
            self.purges += 1
            self.values.clear()

    records = Records()
    footprint = Records()
    coordinator = DeletionCoordinator(
        session_store=sessions,
        connection_store=Recorder(events, "connections"),
        runtime_driver=Recorder(
            events,
            "runtime",
            result={
                "userId": USER,
                "state": "DELETING",
                "purgeReason": "ACCOUNT_DELETION",
                "purgeCompletedAt": 1_000,
            },
        ),
        workspace_store=Recorder(events, "workspace"),
        record_store=records,
        footprint_store=footprint,
        clock_ms=lambda: clock[0],
    )

    with pytest.raises(DeletionPending):
        coordinator.delete(USER)
    assert records.values == []
    assert footprint.values == []
    assert sessions.intent["deletionStatus"] == "FINALIZING"

    # Models an already-running control invocation committing after the first
    # purge. The FINALIZING grace must outlive it before the second purge.
    records.values.append("late-in-flight-write")
    footprint.values.append("late-ledger-write")
    assert coordinator.reconcile(USER) == {"status": "pending", "userId": USER}
    assert records.values == ["late-in-flight-write"]
    assert footprint.values == ["late-ledger-write"]

    clock[0] += coordinator.FINALIZATION_GRACE_MS
    assert coordinator.reconcile(USER) == {"status": "deleted", "userId": USER}
    assert records.values == []
    assert footprint.values == []
    assert records.purges == 2
    assert footprint.purges == 2
    assert sessions.intent["deletionStatus"] == "COMPLETED"

    assert coordinator.reconcile(USER) == {"status": "deleted", "userId": USER}
    assert records.purges == 2
    assert footprint.purges == 2


def test_inactive_expiry_deletes_only_workspace_then_resets_runtime_for_return():
    events = []

    class RuntimeExpiry:
        def purge_inactive(self, user_id, **kwargs):
            events.append(("runtime", "purge_inactive", (user_id,), kwargs))
            return {
                "state": "DELETING",
                "userId": user_id,
                "purgeReason": "WORKSPACE_EXPIRY",
            }

        def complete_inactive_purge(self, user_id):
            events.append(("runtime", "complete_inactive_purge", (user_id,), {}))
            return {
                "state": "COLD",
                "userId": user_id,
                "tombstonedAt": None,
                "purgeReason": None,
            }

    coordinator = DeletionCoordinator(
        session_store=Recorder(events, "sessions"),
        connection_store=Recorder(events, "connections"),
        runtime_driver=RuntimeExpiry(),
        workspace_store=Recorder(events, "workspace"),
        record_store=Recorder(events, "records"),
        footprint_store=Recorder(events, "footprint"),
    )

    result = coordinator.delete_inactive(
        USER,
        observed_updated_at_ms=1_000,
        observed_revision=7,
        inactive_before_ms=2_000,
    )

    assert result == {"status": "expired", "userId": USER}
    assert [(name, method) for name, method, _, _ in events] == [
        ("runtime", "purge_inactive"),
        ("workspace", "delete_namespace"),
        ("runtime", "complete_inactive_purge"),
    ]
    runtime_call = events[0]
    assert runtime_call[3] == {
        "observed_updated_at_ms": 1_000,
        "observed_revision": 7,
        "inactive_before_ms": 2_000,
    }


def test_inactive_deletion_race_preserves_all_authority_and_data():
    events = []
    coordinator = DeletionCoordinator(
        session_store=Recorder(events, "sessions"),
        connection_store=Recorder(events, "connections"),
        runtime_driver=Recorder(events, "runtime", result=None),
        workspace_store=Recorder(events, "workspace"),
        record_store=Recorder(events, "records"),
        footprint_store=Recorder(events, "footprint"),
    )

    result = coordinator.delete_inactive(
        USER,
        observed_updated_at_ms=1_000,
        observed_revision=7,
        inactive_before_ms=2_000,
    )

    assert result == {"status": "active", "userId": USER}
    assert [(name, method) for name, method, _, _ in events] == [
        ("runtime", "purge_inactive")
    ]


class ExportSource:
    def records_for_user(self, user_id):
        assert user_id == USER
        return {
            "memory": [{"text": "remember this"}],
            "schedules": [{"name": "weekly"}],
            "receipts": [{"providerMessageId": "gmail-1"}],
        }

    def workspace_files(self, user_id):
        assert user_id == USER
        return {
            "notes/plan.md": b"# Plan\n",
            "memory.md": b"remember this\n",
        }


def test_export_is_bounded_zip_with_user_data_and_no_credentials():
    archive = UserExporter(ExportSource()).build_zip(USER)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        assert set(bundle.namelist()) == {
            "manifest.json",
            "records/memory.json",
            "records/receipts.json",
            "records/schedules.json",
            "workspace/memory.md",
            "workspace/notes/plan.md",
        }
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["format"] == "personal-operator.export.v1"
        assert manifest["userId"] == USER
        assert b"credential" not in archive.lower()


@pytest.mark.parametrize(
    "path",
    ["../secret", "/absolute", "a/../../secret", "a\\windows", "", ".hidden"],
)
def test_export_rejects_unsafe_workspace_paths(path):
    class Unsafe(ExportSource):
        def workspace_files(self, user_id):
            return {path: b"data"}

    with pytest.raises(ExportBoundaryError):
        UserExporter(Unsafe()).build_zip(USER)


def test_export_rejects_secret_shaped_records_and_size_overflow():
    class Secret(ExportSource):
        def records_for_user(self, user_id):
            return {"connections": [{"refresh_token": "nope"}]}

    with pytest.raises(ExportBoundaryError, match="record category"):
        UserExporter(Secret()).build_zip(USER)

    class Huge(ExportSource):
        def workspace_files(self, user_id):
            return {"huge.bin": b"x" * 1025}

    with pytest.raises(ExportBoundaryError, match="entry"):
        UserExporter(Huge(), max_entry_bytes=1024).build_zip(USER)


def test_export_rejects_incompressible_archive_above_sync_response_limit():
    class Incompressible(ExportSource):
        def workspace_files(self, user_id):
            # Deterministic high-entropy bytes exercise the final ZIP size,
            # rather than only the pre-compression source-byte limit.
            import random

            return {"random.bin": random.Random(7).randbytes(4_300_000)}

    with pytest.raises(ExportBoundaryError, match="delivery limit"):
        UserExporter(Incompressible()).build_zip(USER)


def test_export_cap_leaves_proxy_envelope_margin_below_lambda_six_mib():
    encoded_ceiling = 4 * ((UserExporter.MAX_SYNC_ARCHIVE_BYTES + 2) // 3)
    assert encoded_ceiling + 2_048 < 6 * 1024 * 1024
