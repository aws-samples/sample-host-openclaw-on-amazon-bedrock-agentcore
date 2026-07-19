from __future__ import annotations

import pytest

from capabilities.contracts import (
    TurnCapabilityGrantV1,
    derive_target_tenant_binding,
)
from capabilities.durable import DynamoTurnAuthorityRepository
from capabilities.issuer import TurnCapabilityIssuer
from capabilities.test_composition import MemoryDynamoClient
from capabilities.test_gateway import RELEASE_COMMIT, _catalog


NOW = 1_800_000_000
RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/example"
)


class Repository:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def prepare_turn(self, *, grant, targets):
        self.calls.append((grant, tuple(targets)))
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
    persisted_grant, targets = repository.calls[0]
    assert persisted_grant.to_mapping() == grant
    assert len(targets) == 1
    assert targets[0].grant.tenant_binding == derive_target_tenant_binding(
        "user_alpha"
    )
    assert targets[0].grant.current_request_id == "invocation_12345678"


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
    )

    assert minted["allowedPackIds"]
    assert ("CONTROL", "ROOT") in client.items
    assert ("CONTROL", "GLOBAL") in client.items
    assert ("USER#user_alpha", "BOOTSTRAP") in client.items
    assert all(
        ("USER#user_alpha", f"INSTALL#{pack_id}") in client.items
        for pack_id in minted["allowedPackIds"]
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
        (pk, sk)
        for pk, sk in client.items
        if pk == f"RUNTIME#{RUNTIME_ARN}"
    ]
    assert sorted(runtime_rows) == [
        (
            f"RUNTIME#{RUNTIME_ARN}",
            f"release_{RELEASE_COMMIT}#SESSION#session_12345678",
        ),
        (
            f"RUNTIME#{RUNTIME_ARN}",
            f"release_{RELEASE_COMMIT}#SESSION#session_87654321",
        ),
    ]


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
        repository.prepare_turn(grant=grant, targets=[])

    assert client.items == {}
