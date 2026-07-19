from __future__ import annotations

import io
import pytest

from portable.manifest import FORMAT, ImportRejected, ImportUncertain
from portable.staging import DynamoStagedImportStore, S3PortableBlobStore


USER = "user_founder"


class ConditionalError(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    """Minimal conditional single-item table imitating DynamoDB CAS semantics."""

    def __init__(self, *, outage=False):
        self.items = {}
        self.outage = outage
        self.writes = 0

    @staticmethod
    def _pk(key):
        return (key["PK"], key["SK"])

    def get_item(self, *, Key, ConsistentRead=False):
        if self.outage:
            raise RuntimeError("dynamo unavailable")
        item = self.items.get(self._pk(Key))
        return {"Item": item} if item is not None else {}

    def update_item(self, *, Key, ExpressionAttributeValues, **_kwargs):
        self.writes += 1
        if self.outage:
            raise RuntimeError("dynamo unavailable")
        pk = self._pk(Key)
        current = self.items.get(pk)
        expected = ExpressionAttributeValues[":expected"]
        current_generation = current["generation"] if current else 0
        if current_generation != expected:
            raise ConditionalError()
        history = set(current.get("activatedBundleHashes", set())) if current else set()
        bundle_hash = ExpressionAttributeValues[":bundleHash"]
        if bundle_hash in history:
            raise ConditionalError()
        item = dict(current or {})
        item.update(
            {
                "PK": Key["PK"],
                "SK": Key["SK"],
                "recordType": ExpressionAttributeValues[":recordType"],
                "userId": ExpressionAttributeValues[":userId"],
                "generation": current_generation,
            }
        )
        if ":nextGeneration" not in ExpressionAttributeValues:
            pending = item.get("activationApproval")
            approval = ExpressionAttributeValues[":approval"]
            if pending not in (None, approval):
                raise ConditionalError()
            item.update(
                {
                    "activationApproval": approval,
                    "approvalBundleHash": bundle_hash,
                    "approvalGeneration": expected,
                    "approvalBlobKey": ExpressionAttributeValues[":blobKey"],
                    "approvalBlobSha256": ExpressionAttributeValues[":blobSha256"],
                }
            )
        else:
            if (
                item.get("activationApproval")
                != ExpressionAttributeValues[":approval"]
                or item.get("approvalBundleHash") != bundle_hash
                or item.get("approvalGeneration") != expected
                or item.get("approvalBlobKey")
                != ExpressionAttributeValues[":blobKey"]
                or item.get("approvalBlobSha256")
                != ExpressionAttributeValues[":blobSha256"]
            ):
                raise ConditionalError()
            history.add(bundle_hash)
            item.update(
                {
                    "liveBundleHash": bundle_hash,
                    "liveBlobKey": ExpressionAttributeValues[":blobKey"],
                    "liveBlobSha256": ExpressionAttributeValues[":blobSha256"],
                    "activatedBundleHashes": history,
                    "generation": ExpressionAttributeValues[":nextGeneration"],
                }
            )
            item.pop("activationApproval", None)
            item.pop("approvalBundleHash", None)
            item.pop("approvalGeneration", None)
            item.pop("approvalBlobKey", None)
            item.pop("approvalBlobSha256", None)
        self.items[pk] = item
        return {"Attributes": self.items[pk]}


class CommitThenMalformedResponseTable(FakeTable):
    def __init__(self, *, malformed_on):
        super().__init__()
        self.malformed_on = malformed_on

    def update_item(self, **request):
        response = super().update_item(**request)
        is_activation = ":nextGeneration" in request["ExpressionAttributeValues"]
        if self.malformed_on == ("activate" if is_activation else "prepare"):
            return {}
        return response


class FakeBlobs:
    def __init__(self):
        self.objects = {}
        self.stage_calls = 0
        self.load_calls = 0

    def stage(self, user_id, *, bundle_hash, expected_generation, staged):
        self.stage_calls += 1
        key = f"{user_id}/.system/portable/v2/{bundle_hash}/{expected_generation}.json"
        existing = self.objects.get(key)
        if existing is not None and existing != staged:
            raise ImportUncertain("portable blob changed")
        self.objects[key] = staged
        return {"blobKey": key, "blobSha256": bundle_hash}

    def load(self, user_id, *, blob_key, blob_sha256):
        self.load_calls += 1
        assert blob_key.startswith(f"{user_id}/.system/portable/v2/")
        return self.objects[blob_key]


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.puts = []
        self.gets = []

    def put_object(self, **request):
        self.puts.append(dict(request))
        key = request["Key"]
        if key in self.objects:
            raise RuntimeError("PreconditionFailed")
        self.objects[key] = request["Body"]
        return {"ETag": "synthetic"}

    def get_object(self, **request):
        self.gets.append(dict(request))
        return {"Body": io.BytesIO(self.objects[request["Key"]])}


def _staged():
    return {"format": FORMAT, "records": {}, "workspace": {}}


def test_s3_blob_store_is_immutable_idempotent_and_hash_verified():
    client = FakeS3()
    blobs = S3PortableBlobStore(client, bucket_name="synthetic-user-files")
    staged = _staged()

    first = blobs.stage(
        USER,
        bundle_hash="a" * 64,
        expected_generation=0,
        staged=staged,
    )
    second = blobs.stage(
        USER,
        bundle_hash="a" * 64,
        expected_generation=0,
        staged=staged,
    )

    assert first == second
    assert first["blobKey"].startswith(
        f"{USER}/.system/portable/v2/imports/{'a' * 64}/"
    )
    assert blobs.load(
        USER,
        blob_key=first["blobKey"],
        blob_sha256=first["blobSha256"],
    ) == staged
    assert client.puts[0]["IfNoneMatch"] == "*"
    client.objects[first["blobKey"]] = b"{}"
    with pytest.raises(ImportUncertain):
        blobs.load(
            USER,
            blob_key=first["blobKey"],
            blob_sha256=first["blobSha256"],
        )


def test_first_activation_advances_from_zero():
    store = DynamoStagedImportStore(FakeTable(), blobs=FakeBlobs())
    assert store.load_generation(USER) == 0
    approval = store.prepare_activation(
        USER,
        bundle_hash="a" * 64,
        expected_generation=0,
        staged=_staged(),
    )
    generation = store.activate_once(
        USER,
        bundle_hash="a" * 64,
        activation_approval=approval["activationApproval"],
        expected_generation=0,
        staged=_staged(),
    )
    assert generation == 1
    assert store.load_generation(USER) == 1
    live = store.load_live(USER)
    assert live["bundleHash"] == "a" * 64
    assert live["staged"] == _staged()


def test_stale_generation_is_rejected_without_partial_write():
    table = FakeTable()
    store = DynamoStagedImportStore(table, blobs=FakeBlobs())
    approval = store.prepare_activation(
        USER, bundle_hash="a" * 64, expected_generation=0, staged=_staged()
    )
    store.activate_once(
        USER,
        bundle_hash="a" * 64,
        activation_approval=approval["activationApproval"],
        expected_generation=0,
        staged=_staged(),
    )
    with pytest.raises(ImportRejected):
        store.activate_once(
            USER,
            bundle_hash="b" * 64,
            activation_approval=approval["activationApproval"],
            expected_generation=0,
            staged=_staged(),
        )
    # The generation only advanced once; the losing swap wrote nothing.
    assert store.load_generation(USER) == 1


def test_outage_is_uncertain_not_a_silent_success():
    store = DynamoStagedImportStore(FakeTable(outage=True), blobs=FakeBlobs())
    with pytest.raises(ImportUncertain):
        store.prepare_activation(
            USER, bundle_hash="a" * 64, expected_generation=0, staged=_staged()
        )


def test_prepare_reconciles_committed_write_with_malformed_success_response():
    table = CommitThenMalformedResponseTable(malformed_on="prepare")
    store = DynamoStagedImportStore(table, blobs=FakeBlobs())

    approval = store.prepare_activation(
        USER, bundle_hash="a" * 64, expected_generation=0, staged=_staged()
    )

    assert approval["bundleHash"] == "a" * 64
    assert approval["expectedGeneration"] == 0
    assert approval["activationApproval"].startswith("pia_")


def test_activation_reconciles_committed_write_with_malformed_success_response():
    table = CommitThenMalformedResponseTable(malformed_on="activate")
    store = DynamoStagedImportStore(table, blobs=FakeBlobs())
    approval = store.prepare_activation(
        USER, bundle_hash="a" * 64, expected_generation=0, staged=_staged()
    )

    generation = store.activate_once(
        USER,
        bundle_hash="a" * 64,
        activation_approval=approval["activationApproval"],
        expected_generation=0,
        staged=_staged(),
    )

    assert generation == 1
    assert store.load_live(USER)["bundleHash"] == "a" * 64


def test_oversized_staged_payload_is_rejected(monkeypatch):
    import portable.staging as staging_module

    monkeypatch.setattr(staging_module, "_MAX_STAGED_BYTES", 128)
    store = DynamoStagedImportStore(FakeTable(), blobs=FakeBlobs())
    with pytest.raises(ImportRejected):
        store.prepare_activation(
            USER,
            bundle_hash="a" * 64,
            expected_generation=0,
            staged={"blob": "x" * 129},
        )


def test_durable_bundle_replay_is_rejected_without_an_update():
    table = FakeTable()
    store = DynamoStagedImportStore(table, blobs=FakeBlobs())
    approval = store.prepare_activation(
        USER, bundle_hash="a" * 64, expected_generation=0, staged=_staged()
    )
    store.activate_once(
        USER,
        bundle_hash="a" * 64,
        activation_approval=approval["activationApproval"],
        expected_generation=0,
        staged=_staged(),
    )
    before = dict(table.items)
    before_writes = table.writes

    with pytest.raises(ImportRejected, match="already activated|replay"):
        store.prepare_activation(
            USER, bundle_hash="a" * 64, expected_generation=1, staged=_staged()
        )

    assert table.items == before
    assert table.writes == before_writes


def test_full_replay_ledger_rejects_before_staging_a_new_pending_approval():
    table = FakeTable()
    table.items[(f"USER#{USER}", "PORTABLE#LIVE_STATE")] = {
        "PK": f"USER#{USER}",
        "SK": "PORTABLE#LIVE_STATE",
        "recordType": "PORTABLE_LIVE_STATE_V2",
        "userId": USER,
        "generation": 128,
        "activatedBundleHashes": {f"{index:064x}" for index in range(128)},
    }
    blobs = FakeBlobs()
    store = DynamoStagedImportStore(table, blobs=blobs)

    with pytest.raises(ImportRejected, match="ledger is full"):
        store.prepare_activation(
            USER,
            bundle_hash="f" * 64,
            expected_generation=128,
            staged=_staged(),
        )

    assert blobs.stage_calls == 0
    assert table.writes == 0


def test_real_importer_activates_workspace_as_dynamo_safe_live_state():
    from portable import PortableExporter, PortableImporter

    class Source:
        def records_for_user(self, user_id):
            return {
                "memory": [],
                "schedules": [],
                "installed_packs": [],
                "connectors": [],
                "compute_receipts": [],
                "receipts": [],
            }

        def workspace_files(self, user_id):
            return {"notes/plan.md": b"portable bytes\n"}

    table = FakeTable()
    store = DynamoStagedImportStore(table, blobs=FakeBlobs())
    importer = PortableImporter(staging=store)
    bundle = PortableExporter(Source()).build(USER)
    plan = importer.build_plan(bundle.zip_bytes, target_user_id=USER)
    prepared = importer.prepare_activation(
        bundle.zip_bytes,
        target_user_id=USER,
        approved_bundle_hash=plan.bundle_hash,
        approved_plan_id=plan.plan_id,
        approved_base_generation=plan.base_generation,
    )
    importer.activate(
        bundle.zip_bytes,
        approved_bundle_hash=prepared.bundle_hash,
        approved_plan_id=prepared.plan_id,
        approved_base_generation=prepared.base_generation,
        target_user_id=USER,
        activation_approval=prepared.activation_approval,
        expected_generation=prepared.expected_generation,
    )

    live = store.load_live(USER)
    assert live["staged"]["workspace"] == {
        "notes/plan.md": {
            "encoding": "base64",
            "data": "cG9ydGFibGUgYnl0ZXMK",
            "sha256": "278731122cbb217b274381f3fcdb3d21e09dbfa2e037858dad9a6a1268cf59cd",
        }
    }


def test_large_live_state_is_blob_backed_and_not_embedded_in_dynamo():
    table = FakeTable()
    blobs = FakeBlobs()
    store = DynamoStagedImportStore(table, blobs=blobs)
    staged = {
        "format": FORMAT,
        "records": {"memory": [], "schedules": [], "receipts": []},
        "workspace": {
            "large.bin": {
                "encoding": "base64",
                "data": "eA==" * (300 * 1024),
                "sha256": "a" * 64,
            }
        },
        "landing": {
            "schedules": "DISABLED",
            "connectors": "DISCONNECTED",
            "receipts": {"replayable": False},
        },
    }
    approval = store.prepare_activation(
        USER, bundle_hash="b" * 64, expected_generation=0, staged=staged
    )
    store.activate_once(
        USER,
        bundle_hash="b" * 64,
        activation_approval=approval["activationApproval"],
        expected_generation=0,
        staged=staged,
    )

    item = next(iter(table.items.values()))
    assert "staged" not in item
    assert item["liveBlobKey"].startswith(f"{USER}/.system/portable/v2/")
    assert store.load_live(USER)["staged"] == staged
    assert blobs.stage_calls == 2
    assert blobs.load_calls == 1
