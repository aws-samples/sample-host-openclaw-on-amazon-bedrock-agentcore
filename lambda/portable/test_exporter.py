from __future__ import annotations

import io
import json
import zipfile

import pytest

from capabilities.contracts import PortableStateManifestV2
from capabilities.contracts import ImportPlanV1

from portable import (
    FORMAT,
    PortableError,
    PortableExporter,
    complete_bundle_hash,
    object_sha256,
)


USER = "user_founder"
CATALOG_DIGEST = "b" * 64


def _required_records(**overrides):
    records = {
        "memory": [],
        "schedules": [],
        "installed_packs": [],
        "connectors": [],
        "compute_receipts": [],
        "receipts": [],
    }
    records.update(overrides)
    return records


class Source:
    """Minimal export source mirroring the composition _ExportSource contract."""

    def __init__(self, records=None, files=None):
        self._records = records if records is not None else _required_records(
            memory=[{"text": "remember this"}],
            schedules=[{"name": "weekly", "state": "ENABLED", "nextRunAt": 123}],
            installed_packs=[
                {
                    "schema": "personal-operator.capability-installation.v1",
                    "userId": USER,
                    "packId": "schedule.list",
                    "catalogDigest": CATALOG_DIGEST,
                    "state": "ENABLED",
                    "policyRevision": 1,
                    "connectionRefs": [],
                    "killSwitch": False,
                }
            ],
            connectors=[
                {
                    "connectorId": "google-gmail-readonly",
                    "state": "DISCONNECTED",
                }
            ],
            compute_receipts=[
                {
                    "schema": "personal-operator.compute-receipt.v1",
                    "jobId": "job_" + "c" * 64,
                    "status": "FAILED",
                    "imageDigest": "sha256:" + "d" * 64,
                    "inputDigest": "e" * 64,
                    "outputFiles": [],
                    "startedAt": 100,
                    "completedAt": 101,
                    "errorCode": "SYNTHETIC_FAILURE",
                }
            ],
            receipts=[
                {
                    "providerMessageId": "gmail-1",
                    "providerThreadId": "thread-1",
                    "messageId": "<po-aaaaaaaaaaaaaaaaaaaaaaaa@personal-operator.invalid>",
                    "connectionId": "conn_00000000",
                    "accountEmail": "founder@example.com",
                    "senderAddress": "founder@example.com",
                    "recipient": "ada@example.com",
                    "payloadHash": "a" * 64,
                    "executedAt": "2026-07-10T10:00:00+00:00",
                    "labels": ["SENT"],
                }
            ],
        )
        self._files = files if files is not None else {
            "notes/plan.md": b"# Plan\n",
            "memory.md": b"remember this\n",
        }

    def records_for_user(self, user_id):
        assert user_id == USER
        return self._records

    def workspace_files(self, user_id):
        assert user_id == USER
        return self._files


def _read_manifest(archive: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        return json.loads(bundle.read("manifest.json"))


def test_byte_reproducibility_across_independent_calls():
    first = PortableExporter(Source()).build(USER)
    second = PortableExporter(Source()).build(USER)
    assert first.zip_bytes == second.zip_bytes
    assert first.bundle_hash == second.bundle_hash


def test_manifest_is_the_frozen_portable_state_v2_contract():
    bundle = PortableExporter(Source()).build(USER)
    manifest = _read_manifest(bundle.zip_bytes)

    assert PortableStateManifestV2.from_mapping(manifest).to_mapping() == manifest
    assert manifest["schema"] == PortableStateManifestV2.SCHEMA
    assert manifest["bundleHash"] == bundle.bundle_hash


def test_web_export_port_returns_exact_v2_bytes_accepted_by_importer():
    """The production web port must not fall back to the legacy v1 ZIP."""

    class ReadOnlyStore:
        def load_generation(self, user_id):
            return 0

        def load_live(self, user_id):
            return None

        def prepare_activation(self, *args, **kwargs):
            raise AssertionError("planning the exported bytes must remain read-only")

        def activate_once(self, *args, **kwargs):
            raise AssertionError("planning the exported bytes must not activate")

    from portable import PortableImporter

    archive = PortableExporter(Source()).build_zip(USER)
    plan = PortableImporter(staging=ReadOnlyStore()).build_plan(
        archive, target_user_id=USER
    )

    assert isinstance(plan, ImportPlanV1)
    assert plan.bundle_hash == complete_bundle_hash(_read_manifest(archive))
    assert _read_manifest(archive)["schema"] == FORMAT


def test_exporter_uses_one_source_snapshot_for_records_and_workspace():
    class SnapshotSource:
        def snapshot_for_user(self, user_id):
            assert user_id == USER
            return (
                _required_records(memory=[{"snapshot": 1}]),
                {"snapshot.md": b"same generation\n"},
            )

        def records_for_user(self, user_id):
            raise AssertionError("split record read could mix portable generations")

        def workspace_files(self, user_id):
            raise AssertionError("split workspace read could mix portable generations")

    archive = PortableExporter(SnapshotSource()).build_zip(USER)
    manifest = _read_manifest(archive)

    assert {entry["path"] for entry in manifest["objects"]} == {
        "records/memory.json",
        "records/schedules.json",
        "records/installed_packs.json",
        "records/connectors.json",
        "records/compute_receipts.json",
        "records/receipts.json",
        "files/snapshot.md",
    }


def test_per_object_coverage_and_format_tag():
    bundle = PortableExporter(Source()).build(USER)
    manifest = _read_manifest(bundle.zip_bytes)
    assert manifest["schema"] == FORMAT
    with zipfile.ZipFile(io.BytesIO(bundle.zip_bytes)) as archive:
        names = set(archive.namelist())
        declared = {entry["path"] for entry in manifest["objects"]}
        # manifest.json is the framing object; every other zip entry must be
        # declared, and every declared object must be present.
        assert names - {"manifest.json"} == declared
        for entry in manifest["objects"]:
            payload = archive.read(entry["path"])
            assert entry["size"] == len(payload)
            assert entry["sha256"] == object_sha256(payload)
            assert entry["type"] in {
                "FILE",
                "MEMORY",
                "SCHEDULE",
                "INSTALLATION",
                "CONNECTOR",
                "COMPUTE_RECEIPT",
                "EFFECT_RECEIPT",
            }
    assert bundle.bundle_hash == complete_bundle_hash(manifest)


def test_manifest_excludes_every_authority_bearing_class():
    bundle = PortableExporter(Source()).build(USER)
    manifest = _read_manifest(bundle.zip_bytes)
    assert manifest["excludedClasses"] == [
        "APPROVALS",
        "CREDENTIALS",
        "GRANTS",
        "PENDING_EFFECTS",
        "RUNTIME_INTERNALS",
        "SESSIONS",
        "TOMBSTONES",
    ]


def test_export_normalizes_authority_bearing_metadata_to_inert_rows():
    bundle = PortableExporter(Source()).build(USER)
    with zipfile.ZipFile(io.BytesIO(bundle.zip_bytes)) as archive:
        schedules = json.loads(archive.read("records/schedules.json"))
        packs = json.loads(archive.read("records/installed_packs.json"))

    assert schedules == [
        {"name": "weekly", "state": "DISABLED", "userId": USER}
    ]
    assert packs[0]["state"] == "PAUSED"
    assert packs[0]["killSwitch"] is True
    assert packs[0]["connectionRefs"] == []


def test_include_exclude_categories_reject_unknown_record_category():
    src = Source(records={"connections": [{"provider": "google"}]})
    with pytest.raises(PortableError):
        PortableExporter(src).build(USER)


def test_credentials_never_appear_in_bundle_bytes():
    bundle = PortableExporter(Source()).build(USER)
    lower = bundle.zip_bytes.lower()
    assert b"refresh_token" not in lower
    assert b"credential" not in lower
    assert b"cookie" not in lower


def test_secret_shaped_record_fields_fail_before_any_bundle_is_emitted():
    source = Source(
        records=_required_records(
            memory=[{"password": "synthetic-do-not-export"}]
        )
    )

    with pytest.raises(PortableError, match="secret-shaped"):
        PortableExporter(source).build(USER)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_record_numbers_are_not_exportable_json(value):
    source = Source(
        records=_required_records(memory=[{"value": value}])
    )

    with pytest.raises(PortableError, match="not JSON"):
        PortableExporter(source).build(USER)


def test_unsafe_workspace_paths_rejected():
    for bad in ["../secret", "/absolute", "a/../../secret", "a\\win", "", ".hidden"]:
        with pytest.raises(PortableError):
            PortableExporter(Source(files={bad: b"data"})).build(USER)


def test_required_portable_record_categories_cover_the_v2_design():
    from portable.manifest import RECORD_CATEGORIES

    assert RECORD_CATEGORIES == frozenset(
        {
            "memory",
            "schedules",
            "installed_packs",
            "connectors",
            "compute_receipts",
            "receipts",
        }
    )
