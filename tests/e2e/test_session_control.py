"""Fail-closed tests for the E2E cold-start gate."""

from __future__ import annotations

from botocore.exceptions import ClientError
import pytest

from .config import E2EConfig
from . import session


RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:"
    "agent/12345678-1234-1234-1234-123456789abc:7"
)


def config() -> E2EConfig:
    return E2EConfig(
        region="eu-west-1",
        api_url="https://example.invalid",
        webhook_secret="synthetic",
        telegram_chat_id="10001",
        telegram_user_id="10002",
        workspace_session_role_arn=(
            "arn:aws:iam::123456789012:role/"
            "openclaw-workspace-session-role-eu-west-1"
        ),
        runtime_arn=RUNTIME_ARN,
        runtime_endpoint_name="DEFAULT",
    )


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Synthetic")


class FakeTable:
    def __init__(self, *, user_id="user-1", session_id="session-1", error=None):
        self.user_id = user_id
        self.session_id = session_id
        self.error = error
        self.deleted = []

    def get_item(self, *, Key):
        if self.error:
            raise self.error
        if Key["PK"].startswith("CHANNEL#"):
            return {"Item": {"userId": self.user_id}} if self.user_id else {}
        return {"Item": {"sessionId": self.session_id}} if self.session_id else {}

    def delete_item(self, *, Key, ReturnValues):
        if self.error:
            raise self.error
        self.deleted.append((Key, ReturnValues))
        return {"Attributes": {"sessionId": self.session_id}}


class FakeAgentCore:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def stop_runtime_session(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"statusCode": 200}


def install(monkeypatch, table: FakeTable, agentcore: FakeAgentCore) -> None:
    monkeypatch.setattr(session, "_get_table", lambda _cfg: table)
    monkeypatch.setattr(
        session.boto3,
        "client",
        lambda service, **_kwargs: agentcore
        if service == "bedrock-agentcore"
        else (_ for _ in ()).throw(AssertionError(service)),
    )


def test_existing_session_is_stopped_by_exact_arn_and_name_then_deleted(monkeypatch) -> None:
    table = FakeTable()
    agentcore = FakeAgentCore()
    install(monkeypatch, table, agentcore)

    result = session.prepare_cold_start(config())

    assert result == {"hadSession": True, "stopped": True, "recordDeleted": True}
    assert agentcore.calls == [
        {
            "agentRuntimeArn": RUNTIME_ARN,
            "qualifier": "DEFAULT",
            "runtimeSessionId": "session-1",
        }
    ]
    assert table.deleted


def test_no_prior_session_is_a_proven_clean_start_without_stop_or_delete(monkeypatch) -> None:
    table = FakeTable(session_id=None)
    agentcore = FakeAgentCore()
    install(monkeypatch, table, agentcore)

    assert session.prepare_cold_start(config()) == {
        "hadSession": False,
        "stopped": False,
        "recordDeleted": False,
    }
    assert agentcore.calls == []
    assert table.deleted == []


@pytest.mark.parametrize(
    "table",
    [
        FakeTable(user_id=None),
        FakeTable(error=client_error("AccessDeniedException")),
    ],
)
def test_missing_identity_or_dynamodb_failure_is_fatal(monkeypatch, table) -> None:
    install(monkeypatch, table, FakeAgentCore())
    with pytest.raises(session.E2ESessionError):
        session.prepare_cold_start(config())


def test_stop_failure_is_fatal_and_does_not_delete_the_record(monkeypatch) -> None:
    table = FakeTable()
    install(
        monkeypatch,
        table,
        FakeAgentCore(error=client_error("AccessDeniedException")),
    )

    with pytest.raises(session.E2ESessionError, match="stop"):
        session.prepare_cold_start(config())
    assert table.deleted == []


def test_already_terminated_session_still_deletes_the_stale_record(monkeypatch) -> None:
    table = FakeTable()
    install(
        monkeypatch,
        table,
        FakeAgentCore(error=client_error("ResourceNotFoundException")),
    )

    assert session.prepare_cold_start(config()) == {
        "hadSession": True,
        "stopped": False,
        "recordDeleted": True,
    }
    assert table.deleted
