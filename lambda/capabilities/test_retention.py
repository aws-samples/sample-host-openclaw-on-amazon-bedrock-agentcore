"""Privacy-safe retention and account-deletion proofs for capability state."""

from __future__ import annotations

import json

import pytest

from capabilities.retention import (
    DELETION_FENCE_SCHEMA,
    DynamoCapabilityDeletionAdapter,
    derive_deletion_subject_binding,
    subject_partition_key,
)
from capabilities.test_composition import MemoryDynamoClient


USER_ALPHA = "user_alpha"
USER_BETA = "user_beta"


def _record(*, enabled: bool, subject_binding: str) -> str:
    return json.dumps(
        {
            "schema": DELETION_FENCE_SCHEMA,
            "enabled": enabled,
            "subjectBinding": subject_binding,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _seed_subject_row(
    client: MemoryDynamoClient,
    *,
    user_id: str,
    sk: str,
    record: dict,
    ttl: int = 1_900_000_000,
) -> None:
    binding = derive_deletion_subject_binding(user_id)
    client.put(
        subject_partition_key(user_id),
        sk,
        ownerBinding=binding,
        recordJson=json.dumps(record, sort_keys=True, separators=(",", ":")),
        ttl=ttl,
        version=1,
    )


def test_subject_binding_is_domain_separated_stable_and_contains_no_raw_user_id():
    alpha = derive_deletion_subject_binding(USER_ALPHA)

    assert len(alpha) == 64
    assert alpha == derive_deletion_subject_binding(USER_ALPHA)
    assert alpha != derive_deletion_subject_binding(USER_BETA)
    assert USER_ALPHA not in alpha
    assert subject_partition_key(USER_ALPHA) == f"SUBJECT#{alpha}"


@pytest.mark.parametrize("user_id", ["", "a", "user space", True, None])
def test_subject_binding_rejects_noncanonical_user_identity(user_id):
    with pytest.raises((TypeError, ValueError), match="user"):
        derive_deletion_subject_binding(user_id)


def test_deletion_adapter_rejects_page_too_small_to_guarantee_progress():
    with pytest.raises(ValueError, match="page size"):
        DynamoCapabilityDeletionAdapter(
            client=MemoryDynamoClient(),
            table_name="capability-state",
            page_size=1,
        )


def test_establish_deletion_fence_is_monotonic_idempotent_and_privacy_safe():
    client = MemoryDynamoClient()
    adapter = DynamoCapabilityDeletionAdapter(
        client=client,
        table_name="capability-state",
    )
    binding = derive_deletion_subject_binding(USER_ALPHA)
    key = (subject_partition_key(USER_ALPHA), "DELETION")

    assert adapter.establish_deletion_fence(USER_ALPHA) is True
    assert adapter.establish_deletion_fence(USER_ALPHA) is True
    assert client.items[key] == {
        "PK": key[0],
        "SK": key[1],
        "ownerBinding": binding,
        "recordJson": _record(enabled=True, subject_binding=binding),
        "version": 1,
    }
    assert USER_ALPHA not in repr(client.items)


def test_establish_deletion_fence_reconciles_exact_false_to_true_without_downgrade():
    client = MemoryDynamoClient()
    binding = derive_deletion_subject_binding(USER_ALPHA)
    pk = subject_partition_key(USER_ALPHA)
    client.put(
        pk,
        "DELETION",
        ownerBinding=binding,
        recordJson=_record(enabled=False, subject_binding=binding),
        version=1,
    )
    adapter = DynamoCapabilityDeletionAdapter(
        client=client,
        table_name="capability-state",
    )

    assert adapter.establish_deletion_fence(USER_ALPHA) is True
    assert client.items[(pk, "DELETION")]["recordJson"] == _record(
        enabled=True,
        subject_binding=binding,
    )
    assert client.items[(pk, "DELETION")]["version"] == 2


def test_delete_user_records_requires_fence_before_removing_any_row():
    client = MemoryDynamoClient()
    _seed_subject_row(
        client,
        user_id=USER_ALPHA,
        sk="AUTHORITY#PROFILE",
        record={"userId": USER_ALPHA},
    )
    before = dict(client.items)
    adapter = DynamoCapabilityDeletionAdapter(
        client=client,
        table_name="capability-state",
    )

    with pytest.raises(RuntimeError, match="deletion fence"):
        adapter.delete_user_records(USER_ALPHA)

    assert client.items == before


def test_delete_user_records_purges_full_subject_inventory_and_only_keeps_fence():
    client = MemoryDynamoClient()
    adapter = DynamoCapabilityDeletionAdapter(
        client=client,
        table_name="capability-state",
        page_size=2,
    )
    adapter.establish_deletion_fence(USER_ALPHA)
    adapter.establish_deletion_fence(USER_BETA)
    alpha_rows = {
        "AUTHORITY#PROFILE": {"userId": USER_ALPHA},
        "AUTHORITY#INSTALL#web.exact-read": {"userId": USER_ALPHA},
        "SESSION#session_12345678": {"userId": USER_ALPHA},
        "RUNTIME#runtime_12345678": {"userId": USER_ALPHA},
        "TURN#invocation_12345678": {"sub": USER_ALPHA},
        "DELIVERY#invocation_12345678": {
            "actorId": "telegram:42",
            "chatId": "42",
        },
        "TARGET#" + "a" * 64: {"normalizedTarget": "https://example.com/private"},
        "LEDGER#CALL#" + "b" * 64: {
            "callJson": "source-bearing arguments",
            "resultJson": "source-bearing result",
        },
    }
    for sk, record in alpha_rows.items():
        _seed_subject_row(
            client,
            user_id=USER_ALPHA,
            sk=sk,
            record=record,
        )
    _seed_subject_row(
        client,
        user_id=USER_BETA,
        sk="AUTHORITY#PROFILE",
        record={"userId": USER_BETA},
    )

    assert adapter.delete_user_records(USER_ALPHA) is None

    alpha_pk = subject_partition_key(USER_ALPHA)
    assert [key for key in client.items if key[0] == alpha_pk] == [
        (alpha_pk, "DELETION")
    ]
    assert (subject_partition_key(USER_BETA), "AUTHORITY#PROFILE") in client.items
    assert all(call["ConsistentRead"] is True for call in client.query_calls)
    retained = repr(
        [
            item
            for (pk, _), item in client.items.items()
            if pk == alpha_pk
        ]
    )
    for forbidden in (
        USER_ALPHA,
        "telegram:42",
        "\"chatId\": \"42\"",
        "https://example.com/private",
        "source-bearing",
    ):
        assert forbidden not in retained


def test_delete_user_records_fails_before_deleting_page_with_cross_owner_row():
    client = MemoryDynamoClient()
    adapter = DynamoCapabilityDeletionAdapter(
        client=client,
        table_name="capability-state",
    )
    adapter.establish_deletion_fence(USER_ALPHA)
    _seed_subject_row(
        client,
        user_id=USER_ALPHA,
        sk="AUTHORITY#PROFILE",
        record={"userId": USER_ALPHA},
    )
    key = (subject_partition_key(USER_ALPHA), "SESSION#session_12345678")
    _seed_subject_row(
        client,
        user_id=USER_ALPHA,
        sk=key[1],
        record={"userId": USER_ALPHA},
    )
    client.items[key]["ownerBinding"] = derive_deletion_subject_binding(USER_BETA)
    before = dict(client.items)

    with pytest.raises(RuntimeError, match="owner"):
        adapter.delete_user_records(USER_ALPHA)

    assert client.items == before


def test_delete_user_records_fails_closed_on_ambiguous_query_shape():
    class AmbiguousClient(MemoryDynamoClient):
        def query(self, **kwargs):
            self.query_calls.append(kwargs)
            return {"Items": "not-a-list"}

    client = AmbiguousClient()
    adapter = DynamoCapabilityDeletionAdapter(
        client=client,
        table_name="capability-state",
    )
    adapter.establish_deletion_fence(USER_ALPHA)
    before = dict(client.items)

    with pytest.raises(RuntimeError, match="inventory"):
        adapter.delete_user_records(USER_ALPHA)

    assert client.items == before
