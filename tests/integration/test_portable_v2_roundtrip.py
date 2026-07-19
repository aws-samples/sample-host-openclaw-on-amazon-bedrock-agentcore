"""Production-port portable-v2 round-trip and durable replay regression."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lambda"))

from portable import FORMAT, PortableExporter, PortableImporter
from portable.staging import DynamoStagedImportStore
from portable.test_staging import FakeBlobs, FakeTable
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
