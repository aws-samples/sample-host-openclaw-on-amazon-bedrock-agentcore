from __future__ import annotations

import json

import pytest

from capabilities.contracts import (
    TurnCapabilityGrantV1,
    derive_target_tenant_binding,
)
from capabilities.durable import DynamoTurnAuthorityRepository
from capabilities.issuer import TurnCapabilityIssuer
from capabilities.retention import (
    DynamoCapabilityDeletionAdapter,
    derive_deletion_subject_binding,
    subject_partition_key,
)
from capabilities.test_composition import MemoryDynamoClient
from capabilities.test_gateway import RELEASE_COMMIT, _catalog


NOW = 1_800_000_000
RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/example"
)


class Repository:
    def __init__(self, error=None, *, enabled_pack_ids=None, installation_error=None):
        self.calls = []
        self.error = error
        self.installation_error = installation_error
        self.installation_reads = []
        self.enabled_pack_ids = enabled_pack_ids

    def strong_read_enabled_pack_ids(self, *, user_id, issued_at):
        self.installation_reads.append((user_id, issued_at))
        if self.installation_error is not None:
            raise self.installation_error
        if self.enabled_pack_ids is not None:
            return tuple(self.enabled_pack_ids)
        return tuple(sorted(pack["packId"] for pack in _catalog().packs))

    def prepare_turn(self, *, grant, targets, delivery_context=None):
        self.calls.append((grant, tuple(targets), delivery_context))
        if self.error is not None:
            raise self.error


def issuer(repository):
    return TurnCapabilityIssuer(
        catalog=_catalog(),
        authority_repository=repository,
        runtime_arn=RUNTIME_ARN,
        runtime_qualifier=f"release_{RELEASE_COMMIT}",
        clock=lambda: NOW,
        nonce_factory=lambda: "nonce_12345678",
    )


def test_mints_and_persists_exact_current_turn_and_target_authority():
    repository = Repository()
    grant = issuer(repository).mint(
        user_id="user_alpha",
        session_id="session_12345678",
        invocation_id="invocation_12345678",
        message_text="read https://example.com/exact",
        scheduled_read_only=False,
        channel="telegram",
        actor_id="telegram:42",
    )

    assert grant["sub"] == "user_alpha"
    assert grant["sessionId"] == "session_12345678"
    assert grant["runtimeArn"] == RUNTIME_ARN
    assert grant["invocationId"] == "invocation_12345678"
    assert grant["releaseCommit"] == RELEASE_COMMIT
    assert grant["iat"] == NOW
    assert grant["exp"] == NOW + 300
    assert len(grant["allowedOperationIds"]) == 10
    assert len(grant["targetGrantHashes"]) == 1
    persisted_grant, targets, delivery_context = repository.calls[0]
    assert persisted_grant.to_mapping() == grant
    assert len(targets) == 1
    assert targets[0].grant.tenant_binding == derive_target_tenant_binding(
        "user_alpha"
    )
    assert targets[0].grant.current_request_id == "invocation_12345678"
    assert delivery_context == {
        "channel": "telegram",
        "actorId": "telegram:42",
        "chatId": "42",
    }
    assert repository.installation_reads == [("user_alpha", NOW)]


def test_mint_omits_only_current_disabled_or_killed_packs_and_their_operations():
    repository = Repository(
        enabled_pack_ids=("schedule.list", "workspace.file-read")
    )

    grant = issuer(repository).mint(
        user_id="user_alpha",
        session_id="session_12345678",
        invocation_id="invocation_12345678",
        message_text="hello",
        scheduled_read_only=False,
    )

    assert grant["allowedPackIds"] == ["schedule.list", "workspace.file-read"]
    assert grant["allowedOperationIds"] == [
        "schedule.list",
        "workspace.file.read",
    ]


def test_mint_fails_closed_before_persistence_when_installations_are_unavailable():
    repository = Repository(
        installation_error=RuntimeError("installation authority unavailable")
    )

    with pytest.raises(RuntimeError, match="installation authority unavailable"):
        issuer(repository).mint(
            user_id="user_alpha",
            session_id="session_12345678",
            invocation_id="invocation_12345678",
            message_text="hello",
            scheduled_read_only=False,
        )

    assert repository.calls == []


def test_scheduled_grant_contains_only_catalog_derived_read_and_propose_operations():
    repository = Repository()
    grant = issuer(repository).mint(
        user_id="user_alpha",
        session_id="session_12345678",
        invocation_id="invocation_12345678",
        message_text="",
        scheduled_read_only=True,
    )

    assert grant["allowedOperationIds"] == [
        "schedule.cancel.propose",
        "schedule.list",
        "schedule.propose",
        "web.exact.read",
        "workspace.file.list",
        "workspace.file.read",
    ]
    assert "compute.status" not in grant["allowedOperationIds"]
    assert "workspace.file.write" not in grant["allowedOperationIds"]


def test_never_returns_a_grant_when_durable_authority_persistence_fails():
    repository = Repository(RuntimeError("persistence unavailable"))

    with pytest.raises(RuntimeError, match="persistence unavailable"):
        issuer(repository).mint(
            user_id="user_alpha",
            session_id="session_12345678",
            invocation_id="invocation_12345678",
            message_text="hello",
            scheduled_read_only=False,
        )


def _durable_issuer(client, *, nonce="nonce_12345678"):
    catalog = _catalog()
    return TurnCapabilityIssuer(
        catalog=catalog,
        authority_repository=DynamoTurnAuthorityRepository(
            client=client,
            table_name="capability-state",
            catalog=catalog,
        ),
        runtime_arn=RUNTIME_ARN,
        runtime_qualifier=f"release_{RELEASE_COMMIT}",
        clock=lambda: NOW,
        nonce_factory=lambda: nonce,
    )


def test_durable_authority_bootstraps_once_then_missing_or_killed_state_fails_closed():
    client = MemoryDynamoClient()
    minted = _durable_issuer(client).mint(
        user_id="user_alpha",
        session_id="session_12345678",
        invocation_id="invocation_12345678",
        message_text="read https://example.com/exact",
        scheduled_read_only=False,
        channel="telegram",
        actor_id="telegram:42",
    )

    assert minted["allowedPackIds"]
    assert ("CONTROL", "ROOT") in client.items
    assert ("CONTROL", "GLOBAL") in client.items
    subject_pk = subject_partition_key("user_alpha")
    assert (subject_pk, "BOOTSTRAP") in client.items
    assert all(
        (subject_pk, f"AUTHORITY#INSTALL#{pack_id}") in client.items
        for pack_id in minted["allowedPackIds"]
    )
    assert client.items[(subject_pk, "TURN#invocation_12345678")][
        "recordJson"
    ] == json.dumps(minted, sort_keys=True, separators=(",", ":"))
    delivery = client.items[(subject_pk, "DELIVERY#invocation_12345678")]
    assert delivery["recordJson"] == (
        '{"actorId":"telegram:42","channel":"telegram","chatId":"42",'
        '"invocationId":"invocation_12345678",'
        '"schema":"personal-operator.turn-delivery-context.v1",'
        '"userId":"user_alpha"}'
    )

    client.items[("CONTROL", "GLOBAL")]["recordJson"] = '{"enabled":true}'
    with pytest.raises(RuntimeError, match="global kill switch"):
        _durable_issuer(client, nonce="nonce_87654321").mint(
            user_id="user_alpha",
            session_id="session_12345678",
            invocation_id="invocation_87654321",
            message_text="hello",
            scheduled_read_only=False,
        )

    del client.items[("CONTROL", "GLOBAL")]
    with pytest.raises(RuntimeError, match="global kill switch"):
        _durable_issuer(client, nonce="nonce_abcdef12").mint(
            user_id="user_alpha",
            session_id="session_12345678",
            invocation_id="invocation_abcdef12",
            message_text="hello",
            scheduled_read_only=False,
        )
    assert ("CONTROL", "GLOBAL") not in client.items


def test_durable_authority_rows_are_hashed_owned_and_ttl_bounded():
    client = MemoryDynamoClient()
    minted = _durable_issuer(client).mint(
        user_id="user_alpha",
        session_id="session_12345678",
        invocation_id="invocation_12345678",
        message_text="read https://example.com/exact",
        scheduled_read_only=False,
        channel="telegram",
        actor_id="telegram:42",
    )
    binding = derive_deletion_subject_binding("user_alpha")
    pk = subject_partition_key("user_alpha")
    subject_items = {
        sk: item for (item_pk, sk), item in client.items.items() if item_pk == pk
    }

    assert not any(
        raw_pk.startswith(("USER#", "SESSION#", "RUNTIME#", "TURN#", "TENANT#"))
        for raw_pk, _ in client.items
    )
    assert subject_items["DELETION"]["ownerBinding"] == binding
    assert "ttl" not in subject_items["DELETION"]
    assert "ttl" not in subject_items["BOOTSTRAP"]
    authority_ttl = NOW + 90 * 24 * 60 * 60
    for sk, item in subject_items.items():
        assert item["ownerBinding"] == binding
        if sk.startswith("AUTHORITY#"):
            assert item["ttl"] == authority_ttl
        elif sk not in {"DELETION", "BOOTSTRAP"}:
            assert item["ttl"] == minted["exp"]
    target = next(
        item for sk, item in subject_items.items() if sk.startswith("TARGET#")
    )
    assert "https://example.com/exact" in target["recordJson"]
    assert target["ttl"] == minted["exp"]


def test_durable_issuer_preserves_other_enabled_packs_when_one_is_paused_or_killed():
    client = MemoryDynamoClient()
    first = _durable_issuer(client).mint(
        user_id="user_alpha",
        session_id="session_12345678",
        invocation_id="invocation_12345678",
        message_text="hello",
        scheduled_read_only=False,
    )
    pk = subject_partition_key("user_alpha")
    paused_pack = "schedule.list"
    killed_pack = "compute.run"
    for pack_id, state, killed in (
        (paused_pack, "PAUSED", False),
        (killed_pack, "PAUSED", True),
    ):
        key = (pk, f"AUTHORITY#INSTALL#{pack_id}")
        record = json.loads(client.items[key]["recordJson"])
        record["state"] = state
        record["killSwitch"] = killed
        record["policyRevision"] += 1
        client.items[key]["recordJson"] = json.dumps(
            record, sort_keys=True, separators=(",", ":")
        )
        client.items[key]["version"] += 1

    second = _durable_issuer(client, nonce="nonce_87654321").mint(
        user_id="user_alpha",
        session_id="session_12345678",
        invocation_id="invocation_87654321",
        message_text="hello",
        scheduled_read_only=False,
    )

    assert paused_pack in first["allowedPackIds"]
    assert killed_pack in first["allowedPackIds"]
    assert paused_pack not in second["allowedPackIds"]
    assert killed_pack not in second["allowedPackIds"]
    assert set(second["allowedPackIds"]) == set(first["allowedPackIds"]) - {
        paused_pack,
        killed_pack,
    }


def test_missing_installation_after_bootstrap_never_recreates_default_authority():
    client = MemoryDynamoClient()
    _durable_issuer(client).mint(
        user_id="user_alpha",
        session_id="session_12345678",
        invocation_id="invocation_12345678",
        message_text="hello",
        scheduled_read_only=False,
    )
    key = (
        subject_partition_key("user_alpha"),
        "AUTHORITY#INSTALL#schedule.list",
    )
    del client.items[key]

    with pytest.raises(RuntimeError, match="installation"):
        _durable_issuer(client, nonce="nonce_87654321").mint(
            user_id="user_alpha",
            session_id="session_12345678",
            invocation_id="invocation_87654321",
            message_text="hello",
            scheduled_read_only=False,
        )

    assert key not in client.items


def test_hashed_deletion_tombstone_blocks_issuer_without_recreating_raw_authority():
    client = MemoryDynamoClient()
    deletion = DynamoCapabilityDeletionAdapter(
        client=client,
        table_name="capability-state",
    )
    deletion.establish_deletion_fence("user_alpha")

    with pytest.raises(RuntimeError, match="deletion"):
        _durable_issuer(client).mint(
            user_id="user_alpha",
            session_id="session_12345678",
            invocation_id="invocation_12345678",
            message_text="hello",
            scheduled_read_only=False,
        )

    pk = subject_partition_key("user_alpha")
    assert [key for key in client.items if key[0] == pk] == [(pk, "DELETION")]
    assert "user_alpha" not in repr(client.items)


def test_durable_runtime_authority_is_session_partitioned_for_multiple_users():
    client = MemoryDynamoClient()
    for user_id, session_id, invocation_id, nonce in (
        ("user_alpha", "session_12345678", "invocation_12345678", "nonce_12345678"),
        ("user_beta", "session_87654321", "invocation_87654321", "nonce_87654321"),
    ):
        _durable_issuer(client, nonce=nonce).mint(
            user_id=user_id,
            session_id=session_id,
            invocation_id=invocation_id,
            message_text="hello",
            scheduled_read_only=False,
        )

    runtime_rows = [
        (pk, sk, json.loads(item["recordJson"]))
        for (pk, sk), item in client.items.items()
        if sk.startswith("RUNTIME#")
    ]
    assert len(runtime_rows) == 2
    assert {record["userId"] for _, _, record in runtime_rows} == {
        "user_alpha",
        "user_beta",
    }
    assert all(
        pk == subject_partition_key(record["userId"])
        and record["runtimeArn"] == RUNTIME_ARN
        for pk, _, record in runtime_rows
    )


def test_durable_authority_rejects_operation_inventory_that_differs_from_packs():
    client = MemoryDynamoClient()
    catalog = _catalog()
    captured = Repository()
    mapping = issuer(captured).mint(
        user_id="user_alpha",
        session_id="session_12345678",
        invocation_id="invocation_12345678",
        message_text="hello",
        scheduled_read_only=False,
    )
    mapping["allowedPackIds"] = ["schedule.list"]
    mapping["allowedOperationIds"] = ["compute.run"]
    grant = TurnCapabilityGrantV1.from_mapping(mapping)
    repository = DynamoTurnAuthorityRepository(
        client=client,
        table_name="capability-state",
        catalog=catalog,
    )

    with pytest.raises(ValueError, match="operation authority"):
        repository.prepare_turn(grant=grant, targets=[], delivery_context=None)

    assert client.items == {}
