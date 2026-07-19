from __future__ import annotations

import hashlib

import pytest

from portable.live import PortableLiveProjection
from portable.manifest import FORMAT, ImportUncertain, default_landing


USER = "user_founder"


def _records():
    return {
        "memory": [{"text": "remember"}],
        "schedules": [
            {"name": "weekly", "state": "DISABLED", "userId": USER}
        ],
        "installed_packs": [
            {
                "schema": "personal-operator.capability-installation.v1",
                "userId": USER,
                "packId": "schedule.list",
                "catalogDigest": "a" * 64,
                "state": "PAUSED",
                "policyRevision": 1,
                "connectionRefs": [],
                "killSwitch": True,
            }
        ],
        "connectors": [
            {"connectorId": "google-gmail-readonly", "state": "DISCONNECTED"}
        ],
        "compute_receipts": [
            {
                "schema": "personal-operator.compute-receipt.v1",
                "jobId": "job_" + "b" * 64,
                "status": "FAILED",
                "imageDigest": "sha256:" + "c" * 64,
                "inputDigest": "d" * 64,
                "outputFiles": [],
                "startedAt": 10,
                "completedAt": 11,
                "errorCode": "SYNTHETIC_FAILURE",
            }
        ],
        "receipts": [
            {
                "providerMessageId": "gmail-1",
                "providerThreadId": "thread-1",
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


class Store:
    def __init__(self, live):
        self.live = live
        self.reads = []

    def load_live(self, user_id):
        self.reads.append(user_id)
        return self.live


def _live(user_id=USER):
    payload = b"x"
    return {
        "userId": user_id,
        "generation": 3,
        "bundleHash": "f" * 64,
        "staged": {
            "format": FORMAT,
            "records": _records(),
            "workspace": {
                "notes/a.txt": {
                    "encoding": "base64",
                    "data": "eA==",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            },
            "landing": default_landing(),
        },
    }


def test_projection_strong_reads_every_inert_domain_view_from_one_generation():
    store = Store(_live())
    projection = PortableLiveProjection(store)

    snapshot = projection.snapshot_for_user(USER)

    assert snapshot.generation == 3
    assert snapshot.bundle_hash == "f" * 64
    assert snapshot.records == _records()
    assert snapshot.workspace == {"notes/a.txt": b"x"}
    assert store.reads == [USER]

    assert projection.disabled_schedules(USER)[0]["state"] == "DISABLED"
    assert projection.installed_pack_metadata(USER)[0]["killSwitch"] is True
    assert projection.disconnected_connectors(USER)[0]["state"] == "DISCONNECTED"
    assert projection.compute_receipt_history(USER)[0]["status"] == "FAILED"
    assert projection.effect_receipt_history(USER)[0]["providerMessageId"] == "gmail-1"

    # The projection exposes list/history only. It cannot fire, admit, dedupe,
    # dispatch, reconcile, or restore a connection envelope.
    for forbidden in (
        "strong_read_schedule",
        "list_enabled_schedules",
        "strong_read_installation",
        "get_receipt",
        "dispatch",
        "reconcile",
    ):
        assert not hasattr(projection, forbidden)


def test_projection_rejects_cross_user_or_noncanonical_live_state():
    with pytest.raises(ImportUncertain, match="portable live state"):
        PortableLiveProjection(Store(_live("user_other"))).snapshot_for_user(USER)

    invalid = _live()
    invalid["staged"]["records"]["connectors"][0]["state"] = "CONNECTED"
    with pytest.raises(ImportUncertain, match="portable live state"):
        PortableLiveProjection(Store(invalid)).snapshot_for_user(USER)

    invalid = _live()
    invalid["bundleHash"] = "z" * 64
    with pytest.raises(ImportUncertain, match="portable live state"):
        PortableLiveProjection(Store(invalid)).snapshot_for_user(USER)

    invalid = _live()
    invalid["staged"]["credentials"] = {"opaque": "must-not-be-ignored"}
    with pytest.raises(ImportUncertain, match="portable live state"):
        PortableLiveProjection(Store(invalid)).snapshot_for_user(USER)
