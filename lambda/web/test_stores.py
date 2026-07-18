from __future__ import annotations

from copy import deepcopy

import pytest

from .stores import DynamoOAuthStateStore, DynamoWebStore, WebStoreError


class Table:
    def __init__(self):
        self.items = {}
        self.calls = []

    def put_item(self, **kwargs):
        self.calls.append(("put", kwargs))
        item = deepcopy(kwargs["Item"])
        key = (item["PK"], item["SK"])
        if key in self.items:
            error = RuntimeError("conditional")
            error.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
            raise error
        self.items[key] = item
        return {}

    def delete_item(self, **kwargs):
        self.calls.append(("delete", kwargs))
        item = self.items.pop((kwargs["Key"]["PK"], kwargs["Key"]["SK"]), None)
        return {"Attributes": deepcopy(item)} if item else {}

    def get_item(self, **kwargs):
        self.calls.append(("get", kwargs))
        item = self.items.get((kwargs["Key"]["PK"], kwargs["Key"]["SK"]))
        return {"Item": deepcopy(item)} if item else {}

    def query(self, **kwargs):
        self.calls.append(("query", kwargs))
        if kwargs.get("IndexName") == "userId-index":
            user_id = kwargs["ExpressionAttributeValues"][":userId"]
            matches = [item for item in self.items.values() if item.get("userId") == user_id]
        else:
            user_pk = kwargs["ExpressionAttributeValues"][":pk"]
            prefix = kwargs["ExpressionAttributeValues"][":prefix"]
            matches = [
                item for item in self.items.values()
                if item["PK"] == user_pk and item["SK"].startswith(prefix)
            ]
        return {"Items": deepcopy(matches)}

    def update_item(self, **kwargs):
        self.calls.append(("update", kwargs))
        item = self.items[(kwargs["Key"]["PK"], kwargs["Key"]["SK"])]
        if item["SK"] == "DELETION":
            values = kwargs["ExpressionAttributeValues"]
            if "finalizingAt=:now" in kwargs["UpdateExpression"]:
                item.update(
                    {
                        "deletionStatus": "FINALIZING",
                        "deletionStatusPk": "DELETION#FINALIZING",
                        "deletionStatusSk": values[":statusSk"],
                        "finalizingAt": values[":now"],
                    }
                )
            else:
                assert item["finalizingAt"] <= values[":finalizingBefore"]
                item.update(
                    {
                        "deletionStatus": "COMPLETED",
                        "completedAt": values[":now"],
                    }
                )
                for field in (
                    "userId",
                    "deletionStatusPk",
                    "deletionStatusSk",
                    "requestedAt",
                    "finalizingAt",
                ):
                    item.pop(field, None)
            return {"Attributes": deepcopy(item)}
        item["revoked"] = True
        return {}


def test_connect_records_are_conditional_bounded_and_atomically_consumed():
    table = Table()
    store = DynamoWebStore(table)
    record = {"userId": "user_founder", "nonce": "n" * 32, "issuedAt": 100}

    store.put_once("a" * 64, record, expires_at=200)
    assert store.pop_once("a" * 64) == {**record, "expiresAt": 200}
    assert store.pop_once("a" * 64) is None


def test_v2_connect_record_immutably_binds_allowlisted_return_path():
    table = Table()
    store = DynamoWebStore(table)
    record = {
        "userId": "user_founder",
        "nonce": "n" * 32,
        "issuedAt": 100,
        "returnPath": "/workspace?draft=draft_action_12345678",
    }

    store.put_once("b" * 64, record, expires_at=200)

    assert store.pop_once("b" * 64) == {**record, "expiresAt": 200}
    with pytest.raises(ValueError, match="connect record"):
        store.put_once(
            "c" * 64,
            {**record, "next": "https://attacker.example"},
            expires_at=200,
        )


def test_sessions_use_digest_gsi_and_can_be_revoked_individually_or_by_user():
    table = Table()
    store = DynamoWebStore(table)
    base = {"userId": "user_founder", "csrfDigest": "c" * 64, "createdAt": 100, "revoked": False}
    store.create("a" * 64, base, expires_at=200)
    store.create("b" * 64, base, expires_at=200)

    assert store.get("a" * 64) == {**base, "expiresAt": 200}
    store.revoke("a" * 64)
    assert store.get("a" * 64)["revoked"] is True
    store.revoke_all("user_founder")
    assert store.get("b" * 64)["revoked"] is True


def test_global_user_revocation_marker_blocks_even_unindexed_session():
    table = Table()
    store = DynamoWebStore(table)
    base = {"userId": "user_founder", "csrfDigest": "c" * 64, "createdAt": 100, "revoked": False}
    store.create("a" * 64, base, expires_at=200)

    store.revoke_all("user_founder")
    # Even if GSI cleanup were eventually consistent, this strongly-read
    # marker blocks the session at authentication time.
    table.items[("SESSION#" + "a" * 64, "SESSION")]["revoked"] = False
    assert store.get("a" * 64)["revoked"] is True


def test_global_revocation_never_misclassifies_other_user_index_records():
    table = Table()
    store = DynamoWebStore(table, clock_ms=lambda: 100_000)
    store.create(
        "a" * 64,
        {
            "userId": "user_founder",
            "csrfDigest": "c" * 64,
            "createdAt": 100,
            "revoked": False,
        },
        expires_at=200,
    )
    store.begin_deletion("user_founder")
    table.items[("TELEGRAM_CALLBACK#" + "b" * 64, "TELEGRAM_CALLBACK")] = {
        "PK": "TELEGRAM_CALLBACK#" + "b" * 64,
        "SK": "TELEGRAM_CALLBACK",
        "recordType": "TELEGRAM_CARD_ACTION",
        "userId": "user_founder",
    }

    store.revoke_all("user_founder")

    assert store.get("a" * 64)["revoked"] is True
    assert not any(operation == "query" for operation, _ in table.calls)


def test_account_deletion_intent_is_durable_queryable_and_revokes_session_before_cleanup():
    table = Table()
    clock = [100_000]
    store = DynamoWebStore(table, clock_ms=lambda: clock[0])
    base = {
        "userId": "user_founder",
        "csrfDigest": "c" * 64,
        "createdAt": 100,
        "revoked": False,
    }
    store.create("a" * 64, base, expires_at=200)

    pending = store.begin_deletion("user_founder")

    assert pending == {
        "userId": "user_founder",
        "deletionStatus": "PENDING",
        "purgeReason": "ACCOUNT_DELETION",
        "requestedAt": 100_000,
        "finalizingAt": None,
        "completedAt": None,
    }
    assert store.get_deletion_intent("user_founder") == pending
    assert store.get("a" * 64)["revoked"] is True
    stored = next(item for item in table.items.values() if item.get("SK") == "DELETION")
    assert stored["recordType"] == "DELETION_INTENT"
    assert stored["deletionStatusPk"] == "DELETION#PENDING"
    assert stored["deletionStatusSk"] == "00000000000000100000#user_founder"

    clock[0] = 200_000
    finalizing = store.mark_deletion_finalizing("user_founder")

    assert finalizing == {
        **pending,
        "deletionStatus": "FINALIZING",
        "finalizingAt": 200_000,
    }
    assert store.get("a" * 64)["revoked"] is True

    with pytest.raises(WebStoreError, match="grace"):
        store.complete_deletion(
            "user_founder",
            finalizing_before_ms=199_999,
        )

    clock[0] = 1_200_000
    completed = store.complete_deletion(
        "user_founder",
        finalizing_before_ms=300_000,
    )

    assert completed == {
        "userId": "user_founder",
        "deletionStatus": "COMPLETED",
        "purgeReason": "ACCOUNT_DELETION",
        "requestedAt": None,
        "finalizingAt": None,
        "completedAt": 1_200_000,
    }
    raw_tombstone = next(
        item for item in table.items.values() if item.get("SK") == "DELETION"
    )
    assert set(raw_tombstone) == {
        "PK",
        "SK",
        "recordType",
        "purgeReason",
        "deletionStatus",
        "completedAt",
    }
    deletion_reads = [
        request
        for operation, request in table.calls
        if operation == "get" and request["Key"]["SK"] == "DELETION"
    ]
    assert deletion_reads
    assert all(request["ConsistentRead"] is True for request in deletion_reads)


def test_revoke_reconciles_an_applied_but_response_lost_update():
    class ResponseLostTable(Table):
        def __init__(self):
            super().__init__()
            self.lost_once = True

        def update_item(self, **kwargs):
            result = super().update_item(**kwargs)
            if (
                self.items[(kwargs["Key"]["PK"], kwargs["Key"]["SK"])]["SK"]
                == "SESSION"
                and self.lost_once
            ):
                self.lost_once = False
                raise TimeoutError("session revocation response was lost")
            return result

    table = ResponseLostTable()
    store = DynamoWebStore(table)
    base = {
        "userId": "user_founder",
        "csrfDigest": "c" * 64,
        "createdAt": 100,
        "revoked": False,
    }
    store.create("a" * 64, base, expires_at=200)

    # The DynamoDB write commits but its response is lost. revoke() must not
    # strand logout: it reconciles with a strong read and returns normally
    # because the exact session is proven revoked.
    store.revoke("a" * 64)

    assert store.get("a" * 64)["revoked"] is True


def test_revoke_reraises_when_revocation_cannot_be_proven():
    class NeverAppliesTable(Table):
        def update_item(self, **kwargs):
            if self.items[(kwargs["Key"]["PK"], kwargs["Key"]["SK"])]["SK"] == (
                "SESSION"
            ):
                raise TimeoutError("session revocation outcome is unknown")
            return super().update_item(**kwargs)

    table = NeverAppliesTable()
    store = DynamoWebStore(table)
    base = {
        "userId": "user_founder",
        "csrfDigest": "c" * 64,
        "createdAt": 100,
        "revoked": False,
    }
    store.create("a" * 64, base, expires_at=200)

    with pytest.raises(TimeoutError):
        store.revoke("a" * 64)


def test_oauth_state_has_a_separate_exact_one_time_schema():
    table = Table()
    store = DynamoOAuthStateStore(table)
    record = {
        "user_id": "user_founder",
        "redirect_uri": "https://app.example/oauth/google/callback",
        "code_verifier": "v" * 64,
        "expires_at": "2026-07-17T12:10:00+00:00",
    }

    store.put_once("d" * 64, record, expires_at=1_784_290_200)
    assert store.pop_once("d" * 64) == record
    assert store.pop_once("d" * 64) is None
    assert ("CONNECT#" + "d" * 64, "CONNECT") not in table.items
