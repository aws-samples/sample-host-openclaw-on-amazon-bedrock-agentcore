from __future__ import annotations

import io
import json
import zipfile

import pytest

from capabilities.contracts import (
    ImportPlanV1 as CanonicalImportPlanV1,
    ImportReceiptV1,
)

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
    def __init__(self, records=None, files=None):
        self._records = records if records is not None else _required_records(
            memory=[{"text": "remember this"}],
            schedules=[{"name": "weekly", "state": "ENABLED", "nextRunAt": 42}],
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
        self.pending = None
        self.activated_hashes = set()

    def load_generation(self, user_id):
        return self.generation

    def load_live(self, user_id):
        return self.state

    def prepare_activation(
        self,
        user_id,
        *,
        bundle_hash,
        expected_generation,
        staged,
    ):
        if expected_generation != self.generation:
            raise ImportRejected("staged generation changed")
        if bundle_hash in self.activated_hashes:
            raise ImportRejected("portable bundle was already activated")
        approval_id = f"pia_{bundle_hash}"
        candidate = {
            "activationApproval": approval_id,
            "bundleHash": bundle_hash,
            "expectedGeneration": expected_generation,
        }
        if self.pending not in (None, candidate):
            raise ImportRejected("another portable activation is pending")
        self.pending = candidate
        return dict(candidate)

    def activate_once(
        self,
        user_id,
        *,
        bundle_hash,
        activation_approval,
        expected_generation,
        staged,
    ):
        self.writes += 1
        if self.fail_on_write:
            raise RuntimeError("staging store unavailable")
        if expected_generation != self.generation:
            raise ImportRejected("staged generation changed")
        if bundle_hash in self.activated_hashes:
            raise ImportRejected("portable bundle was already activated")
        if self.pending != {
            "activationApproval": activation_approval,
            "bundleHash": bundle_hash,
            "expectedGeneration": expected_generation,
        }:
            raise ImportRejected("portable activation approval is invalid")
        self.generation += 1
        self.activated_hashes.add(bundle_hash)
        self.pending = None
        self.state = {
            "userId": user_id,
            "generation": self.generation,
            "bundleHash": bundle_hash,
            "staged": staged,
        }
        return self.generation


class ForbiddenStore(StagingStore):
    def prepare_activation(self, *args, **kwargs):
        raise AssertionError("dry-run must not write to the staging store")

    def activate_once(self, *args, **kwargs):
        raise AssertionError("dry-run must not write to the staging store")

    def load_generation(self, user_id):
        return 0


def _bundle_bytes(source=None) -> bytes:
    return PortableExporter(source or Source()).build(USER).zip_bytes


def _build_plan(importer, raw, user_id=USER):
    return importer.build_plan(raw, target_user_id=user_id)


def _prepare(importer, raw, user_id=USER):
    plan = _build_plan(importer, raw, user_id)
    return importer.prepare_activation(
        raw,
        target_user_id=user_id,
        approved_bundle_hash=plan.bundle_hash,
        approved_plan_id=plan.plan_id,
        approved_base_generation=plan.base_generation,
    )


def _activate(importer, raw, user_id=USER):
    prepared = _prepare(importer, raw, user_id)
    return importer.activate(
        raw,
        approved_bundle_hash=prepared.bundle_hash,
        approved_plan_id=prepared.plan_id,
        approved_base_generation=prepared.base_generation,
        target_user_id=user_id,
        activation_approval=prepared.activation_approval,
        expected_generation=prepared.expected_generation,
    )


def _write_manifest(entries: dict, manifest: dict, *, rehash: bool = True) -> None:
    if rehash:
        manifest["bundleHash"] = complete_bundle_hash(manifest)
    entries["manifest.json"] = canonical_json(manifest)


def _replace_record(entries: dict, category: str, payload: bytes) -> None:
    path = f"records/{category}.json"
    entries[path] = payload
    manifest = json.loads(entries["manifest.json"])
    descriptor = next(item for item in manifest["objects"] if item["path"] == path)
    descriptor["size"] = len(payload)
    descriptor["sha256"] = object_sha256(payload)
    _write_manifest(entries, manifest)


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


def test_dry_run_plan_is_canonical_inert_and_never_writes():
    importer = PortableImporter(staging=ForbiddenStore())
    plan = _build_plan(importer, _bundle_bytes())
    assert isinstance(plan, ImportPlanV1)
    assert plan.bundle_hash and len(plan.bundle_hash) == 64
    assert plan.user_id == USER
    assert plan.base_generation == "generation_00000000000000000000"
    assert plan.object_count == 7
    assert plan.total_bytes > 0
    assert plan.schedules_disabled is True
    assert plan.connectors_disconnected is True
    assert plan.effects_replayable is False


# ---- content addressing rejections --------------------------------------


def test_hash_mismatch_rejected():
    entries = _entries(_bundle_bytes())
    # Corrupt a declared object without updating its descriptor.
    entries["files/memory.md"] = b"tampered\n"
    with pytest.raises(BundleIntegrityError):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries))


def test_size_mismatch_rejected():
    entries = _entries(_bundle_bytes())
    parsed = json.loads(entries["manifest.json"])
    for entry in parsed["objects"]:
        if entry["path"] == "files/memory.md":
            entry["size"] = entry["size"] + 10  # keep sha to isolate size check
    _write_manifest(entries, parsed)
    with pytest.raises(BundleIntegrityError, match="size"):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries))


def test_decompression_bomb_rejected_before_reading_entries():
    # A bundle that is small compressed but inflates past the uncompressed
    # ceiling must be rejected on ZipInfo.file_size, before any entry is read
    # into memory — otherwise a ~1.5 MiB bundle can OOM the import Lambda.
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index in range(60):
            info = zipfile.ZipInfo(
                f"workspace/zero_{index:03d}.bin", date_time=(1980, 1, 1, 0, 0, 0)
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, b"\x00" * (5 * 1024 * 1024))
    bomb = output.getvalue()
    assert len(bomb) < 4 * 1024 * 1024  # under the HTTP-boundary cap
    with pytest.raises(BundleIntegrityError, match="uncompressed|size"):
        _build_plan(PortableImporter(staging=StagingStore()), bomb)


def test_single_oversize_entry_rejected_on_declared_size():
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("workspace/big.bin", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o600 << 16
        archive.writestr(info, b"\x00" * (6 * 1024 * 1024))
    with pytest.raises(BundleIntegrityError, match="uncompressed|size"):
        _build_plan(PortableImporter(staging=StagingStore()), output.getvalue())


def test_unsupported_zip_compression_fails_as_bundle_integrity_error():
    raw = bytearray(_bundle_bytes())
    local = raw.index(b"PK\x03\x04")
    central = raw.index(b"PK\x01\x02")
    raw[local + 8 : local + 10] = (99).to_bytes(2, "little")
    raw[central + 10 : central + 12] = (99).to_bytes(2, "little")

    with pytest.raises(BundleIntegrityError, match="zip|read"):
        _build_plan(PortableImporter(staging=StagingStore()), bytes(raw))


def test_noncanonical_manifest_rejected():
    entries = _entries(_bundle_bytes())
    parsed = json.loads(entries["manifest.json"])
    # Re-serialize with spaces / unsorted framing -> not byte-canonical.
    entries["manifest.json"] = json.dumps(parsed, indent=2).encode("utf-8")
    with pytest.raises(BundleIntegrityError):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries))


def test_manifest_self_hash_is_verified_over_every_other_frozen_field():
    entries = _entries(_bundle_bytes())
    manifest = json.loads(entries["manifest.json"])
    manifest["createdAt"] = manifest["createdAt"] + 1
    _write_manifest(entries, manifest, rehash=False)

    with pytest.raises(BundleIntegrityError, match="bundle hash"):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries))


def test_manifest_object_order_cannot_alias_one_bundle_into_a_new_hash():
    entries = _entries(_bundle_bytes())
    parsed = json.loads(entries["manifest.json"])
    parsed["objects"] = list(reversed(parsed["objects"]))
    _write_manifest(entries, parsed)

    with pytest.raises(BundleIntegrityError, match="manifest fields"):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries))


def test_manifest_rejects_top_level_authority_smuggling():
    entries = _entries(_bundle_bytes())
    parsed = json.loads(entries["manifest.json"])
    parsed["credentials"] = {"refresh_token": "synthetic-do-not-import"}
    _write_manifest(entries, parsed)
    staging = StagingStore()

    with pytest.raises(BundleIntegrityError, match="manifest fields"):
        _prepare(PortableImporter(staging=staging), _rezip(entries))

    assert staging.pending is None
    assert staging.writes == 0


def test_plan_and_activation_use_the_frozen_canonical_contracts():
    staging = StagingStore()
    importer = PortableImporter(staging=staging, now=lambda: 1_800_000_100)
    raw = _bundle_bytes()

    plan = importer.build_plan(raw, target_user_id=USER)
    assert isinstance(plan, CanonicalImportPlanV1)
    assert plan.user_id == USER
    assert plan.base_generation == "generation_00000000000000000000"

    prepared = importer.prepare_activation(
        raw,
        target_user_id=USER,
        approved_bundle_hash=plan.bundle_hash,
        approved_plan_id=plan.plan_id,
        approved_base_generation=plan.base_generation,
    )
    receipt = importer.activate(
        raw,
        approved_bundle_hash=plan.bundle_hash,
        approved_plan_id=plan.plan_id,
        approved_base_generation=plan.base_generation,
        target_user_id=USER,
        activation_approval=prepared.activation_approval,
        expected_generation=prepared.expected_generation,
    )

    assert isinstance(receipt, ImportReceiptV1)
    assert receipt.plan_id == plan.plan_id
    assert receipt.activated_generation == "generation_00000000000000000001"


def test_manifest_cannot_embed_an_exporting_owner_claim():
    entries = _entries(_bundle_bytes())
    parsed = json.loads(entries["manifest.json"])
    parsed["userId"] = "../../another-user"
    _write_manifest(entries, parsed)

    with pytest.raises(BundleIntegrityError, match="manifest fields"):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries))


def test_manifest_must_explicitly_exclude_every_nonportable_authority_class():
    entries = _entries(_bundle_bytes())
    manifest = json.loads(entries["manifest.json"])
    manifest["excludedClasses"].remove("SESSIONS")
    _write_manifest(entries, manifest)

    with pytest.raises(ImportRejected, match="excluded classes"):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries))


@pytest.mark.parametrize(
    ("path", "wrong_type"),
    [("records/memory.json", "FILE"), ("files/memory.md", "MEMORY")],
)
def test_object_descriptor_type_must_match_its_path(path, wrong_type):
    entries = _entries(_bundle_bytes())
    parsed = json.loads(entries["manifest.json"])
    descriptor = next(entry for entry in parsed["objects"] if entry["path"] == path)
    descriptor["type"] = wrong_type
    _write_manifest(entries, parsed)

    with pytest.raises(BundleIntegrityError, match="path"):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries))


@pytest.mark.parametrize("token", [b"NaN", b"Infinity", b"-Infinity"])
def test_nonfinite_record_json_is_rejected_without_preparation(token):
    entries = _entries(_bundle_bytes())
    payload = b'[{"value":' + token + b"}]"
    _replace_record(entries, "memory", payload)
    staging = StagingStore()

    with pytest.raises(BundleIntegrityError, match="record object is not JSON"):
        _prepare(PortableImporter(staging=staging), _rezip(entries))

    assert staging.pending is None
    assert staging.writes == 0


@pytest.mark.parametrize(
    "payload",
    [
        b'[ { "text" : "noncanonical whitespace" } ]',
        b'[{"text":"first","text":"duplicate"}]',
    ],
)
def test_noncanonical_or_duplicate_key_record_json_is_rejected(payload):
    entries = _entries(_bundle_bytes())
    _replace_record(entries, "memory", payload)

    with pytest.raises(BundleIntegrityError, match="record object"):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries))


def test_duplicate_or_extra_or_missing_paths_rejected():
    # Extra zip entry not declared in the manifest.
    entries = _entries(_bundle_bytes())
    entries["files/rogue.md"] = b"surprise\n"
    with pytest.raises(BundleIntegrityError):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries))

    # Missing declared object.
    entries2 = _entries(_bundle_bytes())
    del entries2["files/memory.md"]
    with pytest.raises(BundleIntegrityError):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries2))


def test_path_traversal_rejected():
    entries = _entries(_bundle_bytes())
    parsed = json.loads(entries["manifest.json"])
    payload = b"evil\n"
    parsed["objects"].append(
        {
            "path": "files/../../etc/passwd",
            "type": "FILE",
            "size": len(payload),
            "sha256": object_sha256(payload),
        }
    )
    parsed["objects"].sort(key=lambda entry: entry["path"])
    _write_manifest(entries, parsed)
    entries["files/../../etc/passwd"] = payload
    with pytest.raises(BundleIntegrityError):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries))


def test_workspace_path_aliases_are_rejected_before_live_state_can_overwrite():
    entries = _entries(_bundle_bytes())
    parsed = json.loads(entries["manifest.json"])
    for path, payload in (
        ("files/a//b.txt", b"aliased"),
        ("files/a/b.txt", b"canonical"),
    ):
        entries[path] = payload
        parsed["objects"].append(
            {
                "path": path,
                "type": "FILE",
                "size": len(payload),
                "sha256": object_sha256(payload),
            }
        )
    parsed["objects"].sort(key=lambda entry: entry["path"])
    _write_manifest(entries, parsed)

    with pytest.raises(BundleIntegrityError, match="manifest fields|workspace path"):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries))


def test_malformed_bundle_and_v1_tag_rejected():
    with pytest.raises(BundleIntegrityError):
        _build_plan(PortableImporter(staging=StagingStore()), b"not a zip")

    # A valid zip lacking a manifest.
    with pytest.raises(BundleIntegrityError):
        _build_plan(
            PortableImporter(staging=StagingStore()), _rezip({"a.txt": b"x"})
        )

    # v1 format tag must be rejected (no compat alias).
    entries = _entries(_bundle_bytes())
    parsed = json.loads(entries["manifest.json"])
    parsed["schema"] = "personal-operator.export.v1"
    _write_manifest(entries, parsed)
    with pytest.raises(BundleIntegrityError):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries))


# ---- policy rejections ---------------------------------------------------


@pytest.mark.parametrize(
    "secret_key",
    [
        "refresh_token",
        "sessionToken",
        "session_token",
        "id_token",
        "apiKey",
        "api-key",
        "authToken",
        "authorization",
    ],
)
def test_secret_corpus_rejected(secret_key):
    src = Source(records=_required_records(memory=[{"note": "ok"}]))
    # Build a valid bundle, then re-serialize a memory record with a secret key.
    entries = _entries(PortableExporter(src).build(USER).zip_bytes)
    poisoned = [{secret_key: "synthetic-do-not-import"}]
    payload = canonical_json(poisoned)
    _replace_record(entries, "memory", payload)
    with pytest.raises(ImportRejected):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries))


@pytest.mark.parametrize("status", ["CONNECTED", "connected", "ConNeCtEd"])
def test_active_authority_rejected(status):
    # A connection/live-authority record must never be importable.
    entries = _entries(_bundle_bytes())
    payload = canonical_json(
        [{"provider": "google-gmail-readonly", "status": status}]
    )
    _replace_record(entries, "memory", payload)
    with pytest.raises(ImportRejected):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries))


@pytest.mark.parametrize(
    "state", ["APPROVAL_PENDING", "approval_pending", "Pending", "uncertain"]
)
def test_pending_effects_rejected(state):
    entries = _entries(_bundle_bytes())
    payload = canonical_json([{"state": state, "actionId": "action_1"}])
    _replace_record(entries, "memory", payload)
    with pytest.raises(ImportRejected):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries))


def test_deletion_tombstone_rejected():
    entries = _entries(_bundle_bytes())
    payload = canonical_json([{"recordType": "USER_TOMBSTONE", "deletionStatus": "COMPLETED"}])
    _replace_record(entries, "memory", payload)
    with pytest.raises(ImportRejected):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries))


def test_manifest_cannot_override_nonreplayable_landing_policy():
    entries = _entries(_bundle_bytes())
    parsed = json.loads(entries["manifest.json"])
    parsed["landing"] = {"receipts": {"replayable": True}}
    _write_manifest(entries, parsed)
    with pytest.raises(BundleIntegrityError, match="manifest fields"):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries))


def test_manifest_rejects_extra_landing_fields_before_activation_can_mutate_generation():
    entries = _entries(_bundle_bytes())
    parsed = json.loads(entries["manifest.json"])
    parsed["landing"] = {"futureAuthority": "ENABLED"}
    _write_manifest(entries, parsed)

    staging = StagingStore()
    with pytest.raises(BundleIntegrityError, match="manifest fields"):
        _prepare(PortableImporter(staging=staging), _rezip(entries))

    assert staging.generation == 0
    assert staging.writes == 0
    assert staging.pending is None


@pytest.mark.parametrize(
    "category",
    [
        "memory",
        "schedules",
        "installed_packs",
        "connectors",
        "compute_receipts",
        "receipts",
    ],
)
def test_structured_live_records_reject_non_object_rows(category):
    entries = _entries(_bundle_bytes())
    payload = canonical_json(["not-a-live-record"])
    _replace_record(entries, category, payload)

    with pytest.raises(ImportRejected, match="structured record"):
        _build_plan(PortableImporter(staging=StagingStore()), _rezip(entries))


# ---- activation ----------------------------------------------------------


def test_activation_success_lands_disabled_disconnected_nonreplayable():
    staging = StagingStore()
    importer = PortableImporter(staging=staging)
    raw = _bundle_bytes()
    prepared = _prepare(importer, raw)
    result = importer.activate(
        raw,
        approved_bundle_hash=prepared.bundle_hash,
        approved_plan_id=prepared.plan_id,
        approved_base_generation=prepared.base_generation,
        target_user_id=USER,
        activation_approval=prepared.activation_approval,
        expected_generation=prepared.expected_generation,
    )
    assert result.state == "ACTIVATED"
    assert result.activated_generation == "generation_00000000000000000001"
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


def test_missing_required_record_categories_are_rejected_before_activation():
    entries = _entries(_bundle_bytes())
    import json

    for path in ("records/memory.json", "records/receipts.json"):
        entries.pop(path)
    parsed = json.loads(entries["manifest.json"])
    parsed["objects"] = [
        entry
        for entry in parsed["objects"]
        if entry["path"] not in {"records/memory.json", "records/receipts.json"}
    ]
    _write_manifest(entries, parsed)
    raw = _rezip(entries)
    staging = StagingStore()
    importer = PortableImporter(staging=staging)

    with pytest.raises(BundleIntegrityError, match="record categories"):
        _prepare(importer, raw)

    assert staging.state is None
    assert staging.generation == 0


def test_activation_bound_to_exact_bundle_hash():
    staging = StagingStore()
    importer = PortableImporter(staging=staging)
    raw = _bundle_bytes()
    prepared = _prepare(importer, raw)
    with pytest.raises(ImportRejected):
        importer.activate(
            raw,
            approved_bundle_hash="0" * 64,
            approved_plan_id=prepared.plan_id,
            approved_base_generation=prepared.base_generation,
            target_user_id=USER,
            activation_approval=prepared.activation_approval,
            expected_generation=prepared.expected_generation,
        )
    assert staging.writes == 0
    assert staging.generation == 0


def test_activation_cas_atomic_on_stale_generation():
    staging = StagingStore()
    importer = PortableImporter(staging=staging)
    raw = _bundle_bytes()
    prepared = _prepare(importer, raw)
    # Someone else advances the generation between plan and activate.
    staging.generation = 5
    with pytest.raises(ImportRejected):
        # The importer captured expected_generation at plan/activate start;
        # simulate stale by pinning expected via a second concurrent swap.
        importer.activate(
            raw,
            approved_bundle_hash=prepared.bundle_hash,
            approved_plan_id=prepared.plan_id,
            approved_base_generation=prepared.base_generation,
            target_user_id=USER,
            activation_approval=prepared.activation_approval,
            expected_generation=0,
        )
    assert staging.state is None


def test_preview_is_bound_to_base_generation_before_any_staging_write():
    staging = StagingStore()
    importer = PortableImporter(staging=staging)
    raw = _bundle_bytes()
    plan = _build_plan(importer, raw)

    # A different activation wins after preview but before preparation.
    staging.generation = 1
    with pytest.raises(ImportRejected, match="generation"):
        importer.prepare_activation(
            raw,
            target_user_id=USER,
            approved_bundle_hash=plan.bundle_hash,
            approved_plan_id=plan.plan_id,
            approved_base_generation=plan.base_generation,
        )

    assert staging.pending is None
    assert staging.writes == 0


def test_failure_atomicity_leaves_no_partial_state():
    staging = StagingStore(fail_on_write=True)
    importer = PortableImporter(staging=staging)
    raw = _bundle_bytes()
    prepared = _prepare(importer, raw)
    with pytest.raises(ImportUncertain):
        importer.activate(
            raw,
            approved_bundle_hash=prepared.bundle_hash,
            approved_plan_id=prepared.plan_id,
            approved_base_generation=prepared.base_generation,
            target_user_id=USER,
            activation_approval=prepared.activation_approval,
            expected_generation=prepared.expected_generation,
        )
    assert staging.state is None


def test_three_user_isolation_binds_to_caller_identity():
    # A portable bundle carries no source principal. Activation binds all rows
    # to the authenticated target user from the approved plan.
    raw = _bundle_bytes()
    plan_hash = complete_bundle_hash(
        __import__("json").loads(_entries(raw)["manifest.json"])
    )
    for caller in (OTHER, THIRD):
        staging = StagingStore()
        importer = PortableImporter(staging=staging)
        prepared = _prepare(importer, raw, caller)
        result = importer.activate(
            raw,
            approved_bundle_hash=plan_hash,
            approved_plan_id=prepared.plan_id,
            approved_base_generation=prepared.base_generation,
            target_user_id=caller,
            activation_approval=prepared.activation_approval,
            expected_generation=prepared.expected_generation,
        )
        assert result.state == "ACTIVATED"
        assert result.user_id == caller
        assert staging.state["userId"] == caller
        assert all(
            row["userId"] == caller
            for row in staging.state["staged"]["records"]["schedules"]
        )
        assert all(
            row["userId"] == caller
            for row in staging.state["staged"]["records"]["installed_packs"]
        )


def test_identical_bundle_replay_is_rejected_without_state_or_generation_change():
    staging = StagingStore()
    importer = PortableImporter(staging=staging)
    raw = _bundle_bytes()
    prepared = _prepare(importer, raw)
    importer.activate(
        raw,
        approved_bundle_hash=prepared.bundle_hash,
        approved_plan_id=prepared.plan_id,
        approved_base_generation=prepared.base_generation,
        target_user_id=USER,
        activation_approval=prepared.activation_approval,
        expected_generation=prepared.expected_generation,
    )
    before_generation = staging.generation
    before_state = dict(staging.state)
    before_writes = staging.writes

    with pytest.raises(ImportRejected, match="already activated|replay"):
        _prepare(importer, raw)

    assert staging.generation == before_generation
    assert staging.state == before_state
    assert staging.writes == before_writes
