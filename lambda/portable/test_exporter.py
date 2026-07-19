from __future__ import annotations

import io
import json
import zipfile

import pytest

from portable import (
    FORMAT,
    PortableError,
    PortableExporter,
    complete_bundle_hash,
    object_sha256,
)


USER = "user_founder"


class Source:
    """Minimal export source mirroring the composition _ExportSource contract."""

    def __init__(self, records=None, files=None):
        self._records = records if records is not None else {
            "memory": [{"text": "remember this"}],
            "schedules": [{"name": "weekly", "state": "ENABLED", "nextRunAt": 123}],
            "receipts": [
                {
                    "providerMessageId": "gmail-1",
                    "providerThreadId": "thread-1",
                    "messageId": "msg_00000000",
                    "connectionId": "conn_00000000",
                    "accountEmail": "founder@example.com",
                    "senderAddress": "founder@example.com",
                    "recipient": "ada@example.com",
                    "payloadHash": "a" * 64,
                    "executedAt": "2026-07-10T10:00:00+00:00",
                    "labels": ["SENT"],
                }
            ],
        }
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


def test_per_object_coverage_and_format_tag():
    bundle = PortableExporter(Source()).build(USER)
    manifest = _read_manifest(bundle.zip_bytes)
    assert manifest["format"] == FORMAT
    assert manifest["userId"] == USER
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
            assert entry["category"] in {"memory", "schedules", "receipts", "workspace"}
    assert bundle.bundle_hash == complete_bundle_hash(manifest)


def test_landing_states_are_stamped():
    bundle = PortableExporter(Source()).build(USER)
    manifest = _read_manifest(bundle.zip_bytes)
    assert manifest["landing"]["schedules"] == "DISABLED"
    assert manifest["landing"]["connectors"] == "DISCONNECTED"
    assert manifest["landing"]["receipts"]["replayable"] is False


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


def test_unsafe_workspace_paths_rejected():
    for bad in ["../secret", "/absolute", "a/../../secret", "a\\win", "", ".hidden"]:
        with pytest.raises(PortableError):
            PortableExporter(Source(files={bad: b"data"})).build(USER)


def test_deletion_exclusion_regression_matches_adapter_categories():
    # The DynamoDB source only ever surfaces memory/schedules/receipts; the v2
    # exporter must not widen that allowlist.
    from portable.manifest import RECORD_CATEGORIES

    assert RECORD_CATEGORIES == frozenset({"memory", "schedules", "receipts"})
