"""Production-port portable-v2 round-trip and durable replay regression."""

from __future__ import annotations

import base64
import copy
import io
import json
from pathlib import Path
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lambda"))

from portable import FORMAT, PortableExporter, PortableImporter
from portable.live import PortableLiveProjection
from portable.staging import DynamoStagedImportStore
from portable.test_staging import FakeBlobs, FakeTable
from capabilities.schedule_port import (
    DynamoScheduleCapabilityPort,
    DynamoScheduleDefinitionReader,
)
from capabilities.test_schedule_port import MemorySchedulerClient
from scheduler.models import build_schedule_spec
from web.composition import _ExportSource
from web.test_index import USER, bootstrap, event, setup_app


class ExportRecords:
    def records_for_user(self, user_id):
        assert user_id == USER
        return {
            "memory": [{"kind": "note", "text": "portable memory"}],
            "schedules": [
                {
                    "scheduleId": "sched_portable_1",
                    "name": "weekly review",
                    "state": "ENABLED",
                    "nextRunAt": 1_800_000_000,
                }
            ],
            "installed_packs": [
                {
                    "schema": "personal-operator.capability-installation.v1",
                    "userId": USER,
                    "packId": "schedule.list",
                    "catalogDigest": "a" * 64,
                    "state": "ENABLED",
                    "policyRevision": 1,
                    "connectionRefs": [],
                    "killSwitch": False,
                }
            ],
            "connectors": [
                {
                    "connectorId": "google-gmail-readonly",
                    "state": "DISCONNECTED",
                }
            ],
            "compute_receipts": [
                {
                    "schema": "personal-operator.compute-receipt.v1",
                    "jobId": "job_" + "b" * 64,
                    "status": "FAILED",
                    "imageDigest": "sha256:" + "c" * 64,
                    "inputDigest": "d" * 64,
                    "outputFiles": [],
                    "startedAt": 100,
                    "completedAt": 101,
                    "errorCode": "SYNTHETIC_FAILURE",
                }
            ],
            "receipts": [
                {
                    "providerMessageId": "synthetic-history-only",
                    "providerThreadId": "synthetic-thread-only",
                    "messageId": "<po-aaaaaaaaaaaaaaaaaaaaaaaa@personal-operator.invalid>",
                    "connectionId": "conn_00000000",
                    "accountEmail": "founder@example.com",
                    "senderAddress": "founder@example.com",
                    "recipient": "ada@example.com",
                    "payloadHash": "e" * 64,
                    "executedAt": "2026-07-10T10:00:00+00:00",
                    "labels": ["SENT"],
                }
            ],
        }


class ExportWorkspace:
    def workspace_files(self, user_id):
        assert user_id == USER
        return {"notes/portable.md": b"portable workspace\n"}


class EmptyRecords:
    def records_for_user(self, user_id):
        assert user_id == USER
        return {
            "memory": [],
            "schedules": [],
            "installed_packs": [],
            "connectors": [],
            "compute_receipts": [],
            "receipts": [],
        }


class EmptyWorkspace:
    def workspace_files(self, user_id):
        assert user_id == USER
        return {}


def test_route_v2_roundtrip_live_activation_and_identical_replay_denial():
    app, tickets, *_ = setup_app()
    cookie, csrf = bootstrap(app, tickets)
    app._exporter = PortableExporter(
        _ExportSource(records=ExportRecords(), workspace=ExportWorkspace())
    )

    exported = app.handle(event("GET", "/api/export", cookie=cookie))
    assert exported["statusCode"] == 200
    archive = base64.b64decode(exported["body"], validate=True)

    table = FakeTable()
    blobs = FakeBlobs()
    store = DynamoStagedImportStore(table, blobs=blobs)
    importer = PortableImporter(staging=store)
    read_only_plan = importer.build_plan(archive, target_user_id=USER)
    assert read_only_plan.object_count == 7
    assert read_only_plan.schedules_disabled is True
    assert read_only_plan.connectors_disconnected is True
    assert read_only_plan.effects_replayable is False

    target = _ExportSource(
        records=EmptyRecords(),
        workspace=EmptyWorkspace(),
        portable=store,
    )
    app._importer = importer
    app._exporter = PortableExporter(target)
    app._workspace._workspace = target
    bundle = base64.b64encode(archive).decode("ascii")

    prepared_response = app.handle(
        event(
            "POST",
            "/api/import/plan",
            cookie=cookie,
            csrf=csrf,
            body={"bundle": bundle},
        )
    )
    assert prepared_response["statusCode"] == 200
    prepared = json.loads(prepared_response["body"])
    assert prepared["bundleHash"] == read_only_plan.bundle_hash
    assert prepared["planId"] == read_only_plan.plan_id
    assert prepared["baseGeneration"] == read_only_plan.base_generation
    assert "expectedGeneration" not in prepared
    assert "activationApproval" not in prepared
    assert table.items == {}
    assert blobs.objects == {}

    activated = app.handle(
        event(
            "POST",
            "/api/import/activate",
            cookie=cookie,
            csrf=csrf,
            body={
                "bundle": bundle,
                "bundleHash": prepared["bundleHash"],
                "planId": prepared["planId"],
                "baseGeneration": prepared["baseGeneration"],
                "confirm": True,
            },
        )
    )
    assert activated["statusCode"] == 200
    activation_receipt = json.loads(activated["body"])
    assert activation_receipt["state"] == "ACTIVATED"
    assert activation_receipt["activatedGeneration"] == (
        "generation_00000000000000000001"
    )

    live = store.load_live(USER)
    assert live["generation"] == 1
    assert live["bundleHash"] == prepared["bundleHash"]
    assert live["staged"]["format"] == FORMAT
    assert live["staged"]["landing"] == {
        "schedules": "DISABLED",
        "installedPacks": "PAUSED",
        "connectors": "DISCONNECTED",
        "computeReceipts": {"replayable": False},
        "receipts": {"replayable": False},
    }
    assert target.records_for_user(USER)["schedules"] == [
        {
            "scheduleId": "sched_portable_1",
            "name": "weekly review",
            "state": "DISABLED",
            "userId": USER,
        }
    ]
    assert target.records_for_user(USER)["installed_packs"][0]["state"] == "PAUSED"
    assert target.records_for_user(USER)["installed_packs"][0]["killSwitch"] is True
    assert target.records_for_user(USER)["connectors"] == [
        {"connectorId": "google-gmail-readonly", "state": "DISCONNECTED"}
    ]
    assert target.records_for_user(USER)["compute_receipts"][0]["status"] == "FAILED"
    assert target.workspace_files(USER) == {
        "notes/portable.md": b"portable workspace\n"
    }
    # Historical receipts live only in the inert portable generation; no
    # ACTION# record or connector envelope exists for an effect dispatcher.
    only_record = next(iter(table.items.values()))
    assert only_record["SK"] == "PORTABLE#LIVE_STATE"
    assert not any(key.startswith("ACTION#") for key in only_record)
    assert "connections" not in live["staged"]["records"]

    before_table = copy.deepcopy(table.items)
    before_generation = store.load_generation(USER)
    replay = app.handle(
        event(
            "POST",
            "/api/import/plan",
            cookie=cookie,
            csrf=csrf,
            body={"bundle": bundle},
        )
    )
    assert replay["statusCode"] == 200
    replay_plan = json.loads(replay["body"])
    assert replay_plan["baseGeneration"] == "generation_00000000000000000001"
    assert store.load_generation(USER) == before_generation
    assert table.items == before_table

    replay_activation = app.handle(
        event(
            "POST",
            "/api/import/activate",
            cookie=cookie,
            csrf=csrf,
            body={
                "bundle": bundle,
                "bundleHash": replay_plan["bundleHash"],
                "planId": replay_plan["planId"],
                "baseGeneration": replay_plan["baseGeneration"],
                "confirm": True,
            },
        )
    )
    assert replay_activation["statusCode"] == 400
    assert store.load_generation(USER) == before_generation
    assert table.items == before_table


def test_native_governed_schedule_roundtrip_lands_inert_and_lists_without_provider_dispatch():
    target_user = "user_imported"
    intruder_user = "user_intruder"

    class ProviderAwareSchedulerClient(MemorySchedulerClient):
        def __init__(self):
            super().__init__()
            self.provider_calls = []

        def create_schedule(self, **request):
            self.provider_calls.append(("create", request))
            raise AssertionError("portable projection must never arm EventBridge")

        def delete_schedule(self, **request):
            self.provider_calls.append(("delete", request))
            raise AssertionError("portable projection must never delete EventBridge")

    scheduler = ProviderAwareSchedulerClient()
    source_spec = build_schedule_spec(
        schedule_id="schedule_portable_12345678",
        user_id=USER,
        task_type="REMINDER",
        definition={
            "message": "synthetic private reminder content",
            "runAt": 1_800_003_600,
            "timezone": "Europe/Tallinn",
        },
        revision=3,
        state="ENABLED",
    )
    foreign_spec = build_schedule_spec(
        schedule_id="schedule_foreign_12345678",
        user_id=intruder_user,
        task_type="REMINDER",
        definition={
            "message": "must never cross tenants",
            "runAt": 1_800_007_200,
            "timezone": "Europe/Tallinn",
        },
        revision=1,
        state="ENABLED",
    )
    scheduler.seed_schedule(source_spec)
    scheduler.seed_schedule(foreign_spec)
    scheduler_items_before = copy.deepcopy(scheduler.items)
    source = _ExportSource(
        records=EmptyRecords(),
        workspace=EmptyWorkspace(),
        schedules=DynamoScheduleDefinitionReader(
            client=scheduler,
            table_name="personal-operator-scheduler-control",
        ),
    )

    bundle = PortableExporter(source).build(USER)

    with zipfile.ZipFile(io.BytesIO(bundle.zip_bytes)) as archive:
        exported_schedules = json.loads(archive.read("records/schedules.json"))
    expected_portable = source_spec.to_mapping()
    expected_portable.pop("schema")
    expected_portable.pop("nextRunAt")
    expected_portable["state"] = "DISABLED"
    assert exported_schedules == [expected_portable]
    assert foreign_spec.schedule_id not in str(exported_schedules)

    table = FakeTable()
    store = DynamoStagedImportStore(table, blobs=FakeBlobs())
    importer = PortableImporter(staging=store, now=lambda: 1_800_000_100)
    plan = importer.build_plan(bundle.zip_bytes, target_user_id=target_user)
    prepared = importer.prepare_activation(
        bundle.zip_bytes,
        target_user_id=target_user,
        approved_bundle_hash=plan.bundle_hash,
        approved_plan_id=plan.plan_id,
        approved_base_generation=plan.base_generation,
    )
    importer.activate(
        bundle.zip_bytes,
        target_user_id=target_user,
        approved_bundle_hash=prepared.bundle_hash,
        approved_plan_id=prepared.plan_id,
        approved_base_generation=prepared.base_generation,
        activation_approval=prepared.activation_approval,
        expected_generation=prepared.expected_generation,
    )

    live = store.load_live(target_user)
    landed = live["staged"]["records"]["schedules"]
    assert landed == [{**expected_portable, "userId": target_user}]
    assert live["staged"]["landing"]["schedules"] == "DISABLED"
    assert live["staged"]["landing"]["receipts"]["replayable"] is False
    assert "nextRunAt" not in landed[0]
    assert "deliveryTarget" not in landed[0]

    operational = DynamoScheduleCapabilityPort(
        client=scheduler,
        table_name="personal-operator-scheduler-control",
        authority_table_name="personal-operator-capability-state",
        catalog_digest="c" * 64,
        clock=lambda: 1_800_000_100,
        nonce_factory=lambda: "nonce_12345678",
        imported_schedules=PortableLiveProjection(store),
    )
    target_view = operational.list_view(target_user)

    assert target_view == {
        "schedules": [
            {
                "scheduleId": source_spec.schedule_id,
                "taskType": "REMINDER",
                "state": "PAUSED",
                "nextRunAt": None,
            }
        ]
    }
    assert set(target_view["schedules"][0]) == {
        "scheduleId",
        "taskType",
        "state",
        "nextRunAt",
    }
    assert "synthetic private reminder content" not in str(target_view)
    assert operational.list_view(intruder_user)["schedules"] == [
        {
            "scheduleId": foreign_spec.schedule_id,
            "taskType": "REMINDER",
            "state": "ENABLED",
            "nextRunAt": 1_800_007_200,
        }
    ]
    assert scheduler.items == scheduler_items_before
    assert scheduler.put_calls == []
    assert scheduler.provider_calls == []
