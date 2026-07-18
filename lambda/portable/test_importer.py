from __future__ import annotations

import io
import zipfile

import pytest

from portable import (
    BundleIntegrityError,
    ImportPlanV1,
    ImportRejected,
    PortableExporter,
    PortableImporter,
    canonical_json,
    complete_bundle_hash,
)
from portable.manifest import ImportUncertain, object_sha256


USER = "user_founder"
OTHER = "user_second"
THIRD = "user_third"


class Source:
    def __init__(self, records=None, files=None):
        self._records = records if records is not None else {
            "memory": [{"text": "remember this"}],
            "schedules": [{"name": "weekly", "state": "ENABLED", "nextRunAt": 42}],
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
        self._files = files if files is not None else {"memory.md": b"remember\n"}

    def records_for_user(self, user_id):
        return self._records

    def workspace_files(self, user_id):
        return self._files


class StagingStore:
    """In-memory staged-generation CAS store imitating the Dynamo cursor CAS."""

    def __init__(self, *, fail_on_write=False):
        self.generation = 0
        self.state = None
        self.fail_on_write = fail_on_write
        self.writes = 0

    def load_generation(self, user_id):
        return self.generation

    def swap(self, user_id, *, expected_generation, staged):
        self.writes += 1
        if self.fail_on_write:
            raise RuntimeError("staging store unavailable")
        if expected_generation != self.generation:
            raise ImportRejected("staged generation changed")
        self.generation += 1
        self.state = {"userId": user_id, "generation": self.generation, "staged": staged}
        return self.generation


class ForbiddenStore(StagingStore):
    def swap(self, *args, **kwargs):
        raise AssertionError("dry-run must not write to the staging store")

    def load_generation(self, user_id):
        return 0


def _bundle_bytes(source=None) -> bytes:
    return PortableExporter(source or Source()).build(USER).zip_bytes


def _rezip(entries: dict) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(entries):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, entries[path])
    return output.getvalue()


def _entries(archive_bytes: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


# ---- dry-run plan --------------------------------------------------------


def test_dry_run_plan_has_counts_and_hash_and_never_writes():
    importer = PortableImporter(staging=ForbiddenStore())
    plan = importer.build_plan(_bundle_bytes())
    assert isinstance(plan, ImportPlanV1)
    assert plan.bundle_hash and len(plan.bundle_hash) == 64
    assert plan.counts["memory"] == 1
    assert plan.counts["schedules"] == 1
    assert plan.counts["receipts"] == 1
    assert plan.landing["schedules"] == "DISABLED"
    assert plan.landing["connectors"] == "DISCONNECTED"


# ---- content addressing rejections --------------------------------------


def test_hash_mismatch_rejected():
    entries = _entries(_bundle_bytes())
    # Corrupt a declared object without updating its descriptor.
    entries["workspace/memory.md"] = b"tampered\n"
    with pytest.raises(BundleIntegrityError):
        PortableImporter(staging=StagingStore()).build_plan(_rezip(entries))


def test_size_mismatch_rejected():
    import json

    entries = _entries(_bundle_bytes())
    parsed = json.loads(entries["manifest.json"])
    for entry in parsed["objects"]:
        if entry["path"] == "workspace/memory.md":
            entry["size"] = entry["size"] + 10  # keep sha to isolate size check
    entries["manifest.json"] = canonical_json(parsed)
    with pytest.raises(BundleIntegrityError):
        PortableImporter(staging=StagingStore()).build_plan(_rezip(entries))


def test_noncanonical_manifest_rejected():
    entries = _entries(_bundle_bytes())
    import json

    parsed = json.loads(entries["manifest.json"])
    # Re-serialize with spaces / unsorted framing -> not byte-canonical.
    entries["manifest.json"] = json.dumps(parsed, indent=2).encode("utf-8")
    with pytest.raises(BundleIntegrityError):
        PortableImporter(staging=StagingStore()).build_plan(_rezip(entries))


def test_duplicate_or_extra_or_missing_paths_rejected():
    # Extra zip entry not declared in the manifest.
    entries = _entries(_bundle_bytes())
    entries["workspace/rogue.md"] = b"surprise\n"
    with pytest.raises(BundleIntegrityError):
        PortableImporter(staging=StagingStore()).build_plan(_rezip(entries))

    # Missing declared object.
    entries2 = _entries(_bundle_bytes())
    del entries2["workspace/memory.md"]
    with pytest.raises(BundleIntegrityError):
        PortableImporter(staging=StagingStore()).build_plan(_rezip(entries2))


def test_path_traversal_rejected():
    entries = _entries(_bundle_bytes())
    import json

    parsed = json.loads(entries["manifest.json"])
    payload = b"evil\n"
    parsed["objects"].append(
        {
            "path": "workspace/../../etc/passwd",
            "category": "workspace",
            "type": "file",
            "size": len(payload),
            "sha256": object_sha256(payload),
        }
    )
    entries["manifest.json"] = canonical_json(parsed)
    entries["workspace/../../etc/passwd"] = payload
    with pytest.raises(BundleIntegrityError):
        PortableImporter(staging=StagingStore()).build_plan(_rezip(entries))


def test_malformed_bundle_and_v1_tag_rejected():
    with pytest.raises(BundleIntegrityError):
        PortableImporter(staging=StagingStore()).build_plan(b"not a zip")

    # A valid zip lacking a manifest.
    with pytest.raises(BundleIntegrityError):
        PortableImporter(staging=StagingStore()).build_plan(_rezip({"a.txt": b"x"}))

    # v1 format tag must be rejected (no compat alias).
    entries = _entries(_bundle_bytes())
    import json

    parsed = json.loads(entries["manifest.json"])
    parsed["format"] = "personal-operator.export.v1"
    entries["manifest.json"] = canonical_json(parsed)
    with pytest.raises(BundleIntegrityError):
        PortableImporter(staging=StagingStore()).build_plan(_rezip(entries))


# ---- policy rejections ---------------------------------------------------


def test_secret_corpus_rejected():
    src = Source(records={
        "memory": [{"note": "ok"}],
        "schedules": [],
        "receipts": [],
    })
    # Build a valid bundle, then re-serialize a memory record with a secret key.
    entries = _entries(PortableExporter(src).build(USER).zip_bytes)
    import json

    poisoned = [{"refresh_token": "AQAAsecret"}]
    payload = canonical_json(poisoned)
    entries["records/memory.json"] = payload
    parsed = json.loads(entries["manifest.json"])
    for entry in parsed["objects"]:
        if entry["path"] == "records/memory.json":
            entry["size"] = len(payload)
            entry["sha256"] = object_sha256(payload)
    entries["manifest.json"] = canonical_json(parsed)
    with pytest.raises(ImportRejected):
        PortableImporter(staging=StagingStore()).build_plan(_rezip(entries))


def test_active_authority_rejected():
    # A connection/live-authority record must never be importable.
    entries = _entries(_bundle_bytes())
    import json

    payload = canonical_json([{"provider": "google-gmail-readonly", "status": "CONNECTED"}])
    entries["records/receipts.json"] = payload
    parsed = json.loads(entries["manifest.json"])
    for entry in parsed["objects"]:
        if entry["path"] == "records/receipts.json":
            entry["size"] = len(payload)
            entry["sha256"] = object_sha256(payload)
    entries["manifest.json"] = canonical_json(parsed)
    with pytest.raises(ImportRejected):
        PortableImporter(staging=StagingStore()).build_plan(_rezip(entries))


def test_pending_effects_rejected():
    entries = _entries(_bundle_bytes())
    import json

    payload = canonical_json([{"state": "APPROVAL_PENDING", "actionId": "action_1"}])
    entries["records/receipts.json"] = payload
    parsed = json.loads(entries["manifest.json"])
    for entry in parsed["objects"]:
        if entry["path"] == "records/receipts.json":
            entry["size"] = len(payload)
            entry["sha256"] = object_sha256(payload)
    entries["manifest.json"] = canonical_json(parsed)
    with pytest.raises(ImportRejected):
        PortableImporter(staging=StagingStore()).build_plan(_rezip(entries))


def test_deletion_tombstone_rejected():
    entries = _entries(_bundle_bytes())
    import json

    payload = canonical_json([{"recordType": "USER_TOMBSTONE", "deletionStatus": "COMPLETED"}])
    entries["records/memory.json"] = payload
    parsed = json.loads(entries["manifest.json"])
    for entry in parsed["objects"]:
        if entry["path"] == "records/memory.json":
            entry["size"] = len(payload)
            entry["sha256"] = object_sha256(payload)
    entries["manifest.json"] = canonical_json(parsed)
    with pytest.raises(ImportRejected):
        PortableImporter(staging=StagingStore()).build_plan(_rezip(entries))


def test_replay_rejected_when_landing_not_stamped_nonreplayable():
    entries = _entries(_bundle_bytes())
    import json

    parsed = json.loads(entries["manifest.json"])
    parsed["landing"]["receipts"] = {"replayable": True}
    entries["manifest.json"] = canonical_json(parsed)
    with pytest.raises(ImportRejected):
        PortableImporter(staging=StagingStore()).build_plan(_rezip(entries))


# ---- activation ----------------------------------------------------------


def test_activation_success_lands_disabled_disconnected_nonreplayable():
    staging = StagingStore()
    importer = PortableImporter(staging=staging)
    raw = _bundle_bytes()
    plan = importer.build_plan(raw)
    result = importer.activate(
        raw,
        approved_bundle_hash=plan.bundle_hash,
        target_user_id=USER,
    )
    assert result["status"] == "activated"
    assert staging.generation == 1
    staged = staging.state["staged"]
    # Schedules land DISABLED, never armed.
    for sched in staged["records"]["schedules"]:
        assert sched.get("state") == "DISABLED"
        assert "nextRunAt" not in sched and "nextRun" not in sched
    # Receipts land non-replayable.
    assert staged["landing"]["receipts"]["replayable"] is False
    # No CONNECTION# envelope is ever written.
    assert "connections" not in staged["records"]
    assert staged["landing"]["connectors"] == "DISCONNECTED"


def test_activation_bound_to_exact_bundle_hash():
    staging = StagingStore()
    importer = PortableImporter(staging=staging)
    raw = _bundle_bytes()
    with pytest.raises(ImportRejected):
        importer.activate(raw, approved_bundle_hash="0" * 64, target_user_id=USER)
    assert staging.writes == 0
    assert staging.generation == 0


def test_activation_cas_atomic_on_stale_generation():
    staging = StagingStore()
    importer = PortableImporter(staging=staging)
    raw = _bundle_bytes()
    plan = importer.build_plan(raw)
    # Someone else advances the generation between plan and activate.
    staging.generation = 5
    with pytest.raises(ImportRejected):
        # The importer captured expected_generation at plan/activate start;
        # simulate stale by pinning expected via a second concurrent swap.
        importer.activate(
            raw,
            approved_bundle_hash=plan.bundle_hash,
            target_user_id=USER,
            expected_generation=0,
        )
    assert staging.state is None


def test_failure_atomicity_leaves_no_partial_state():
    staging = StagingStore(fail_on_write=True)
    importer = PortableImporter(staging=staging)
    raw = _bundle_bytes()
    plan = importer.build_plan(raw)
    with pytest.raises(ImportUncertain):
        importer.activate(raw, approved_bundle_hash=plan.bundle_hash, target_user_id=USER)
    assert staging.state is None


def test_three_user_isolation_binds_to_caller_identity():
    # Bundle exported by USER carries userId=USER, but activation binds to the
    # caller's identity, not the embedded owner claim.
    raw = _bundle_bytes()
    plan_hash = complete_bundle_hash(
        __import__("json").loads(_entries(raw)["manifest.json"])
    )
    for caller in (OTHER, THIRD):
        staging = StagingStore()
        importer = PortableImporter(staging=staging)
        result = importer.activate(
            raw, approved_bundle_hash=plan_hash, target_user_id=caller
        )
        assert result["status"] == "activated"
        assert staging.state["userId"] == caller
