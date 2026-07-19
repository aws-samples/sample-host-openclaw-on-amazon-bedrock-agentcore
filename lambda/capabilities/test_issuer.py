from __future__ import annotations

import pytest

from capabilities.issuer import TurnCapabilityIssuer
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
