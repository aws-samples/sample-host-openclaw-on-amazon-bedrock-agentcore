from __future__ import annotations

import pytest

from . import composition
from .composition import (
    LazyFounderApprovalProducer,
    ProductionConfigurationError,
    _build_google_readonly_service,
    _exact_founder_id,
    _optional_founder_binding,
    _require_founder_account,
)
from .telegram_cards import DynamoTelegramCardActions, ReadOnlyGmailDraftPreparer


class Producer:
    def __init__(self):
        self.calls = []

    def prepare(self, **kwargs):
        self.calls.append(kwargs)
        return "prepared"


class LazyTable:
    def __init__(self, name):
        self.name = name

    def put_item(self, **_kwargs):
        raise AssertionError("lazy control store was unexpectedly called")

    def update_item(self, **_kwargs):
        raise AssertionError("lazy control store was unexpectedly called")

    def query(self, **_kwargs):
        raise AssertionError("lazy control store was unexpectedly called")

    def get_item(self, **_kwargs):
        raise AssertionError("lazy control store was unexpectedly called")

    def delete_item(self, **_kwargs):
        raise AssertionError("lazy control store was unexpectedly called")


def test_google_read_transport_has_one_attempt_and_a_bounded_socket_timeout():
    calls = []
    raw_http = object()
    authorized_http = object()
    service = object()

    result = _build_google_readonly_service(
        "credentials",
        http_factory=lambda **kwargs: calls.append(("http", kwargs)) or raw_http,
        authorized_http_factory=lambda credentials, **kwargs: calls.append(
            ("authorized", credentials, kwargs)
        ) or authorized_http,
        build_service=lambda *args, **kwargs: calls.append(
            ("build", args, kwargs)
        ) or service,
    )

    assert result is service
    assert calls == [
        ("http", {"timeout": 10}),
        (
            "authorized",
            "credentials",
            {"http": raw_http, "max_refresh_attempts": 0},
        ),
        (
            "build",
            ("gmail", "v1"),
            {"http": authorized_http, "cache_discovery": False},
        ),
    ]


def test_lazy_founder_producer_keeps_every_other_pilot_read_only():
    builds = []
    producer = Producer()
    lazy = LazyFounderApprovalProducer(
        founder_user_id="founder-1",
        factory=lambda: builds.append("build") or producer,
    )

    assert lazy.prepare(user_id="pilot-1", opportunity=object()) is None
    assert builds == []
    assert producer.calls == []

    opportunity = object()
    assert lazy.prepare(user_id="founder-1", opportunity=opportunity) == "prepared"
    assert lazy.prepare(user_id="founder-1", opportunity=opportunity) == "prepared"
    assert builds == ["build"]
    assert producer.calls == [
        {"user_id": "founder-1", "opportunity": opportunity},
        {"user_id": "founder-1", "opportunity": opportunity},
    ]


@pytest.mark.parametrize(
    "value",
    ["", "founder-1,pilot-1", "founder-1,founder-1", "../founder"],
)
def test_control_requires_exactly_one_valid_effect_founder(value):
    with pytest.raises(ProductionConfigurationError):
        _exact_founder_id(value)

    assert _exact_founder_id("founder-1") == "founder-1"


def test_founder_read_source_must_match_the_bound_send_account():
    _require_founder_account(
        user_id="founder-1",
        connected_address="founder@example.com",
        founder_user_id="founder-1",
        founder_account_email="founder@example.com",
    )
    _require_founder_account(
        user_id="pilot-1",
        connected_address="pilot@example.net",
        founder_user_id="founder-1",
        founder_account_email="founder@example.com",
    )

    with pytest.raises(RuntimeError, match="account binding"):
        _require_founder_account(
            user_id="founder-1",
            connected_address="other@example.net",
            founder_user_id="founder-1",
            founder_account_email="founder@example.com",
        )


def test_founder_effect_binding_is_optional_but_never_partially_configured(monkeypatch):
    names = (
        "FOUNDER_USER_IDS",
        "GMAIL_SEND_CONNECTION_ID",
        "GMAIL_SEND_ACCOUNT_EMAIL",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    assert _optional_founder_binding() is None

    monkeypatch.setenv("FOUNDER_USER_IDS", "founder-1")
    with pytest.raises(ProductionConfigurationError, match="all three"):
        _optional_founder_binding()

    monkeypatch.setenv("GMAIL_SEND_CONNECTION_ID", "google_conn_1234")
    monkeypatch.setenv("GMAIL_SEND_ACCOUNT_EMAIL", "founder@example.com")
    assert _optional_founder_binding() == (
        "founder-1",
        "google_conn_1234",
        "founder@example.com",
    )


def test_production_without_founder_binding_keeps_all_pilots_read_only(
    monkeypatch,
):
    class Dynamo:
        @staticmethod
        def Table(name):
            return LazyTable(name)

    class Secrets:
        def __init__(self):
            self.reads = []

        def get_secret_value(self, *, SecretId):
            self.reads.append(SecretId)
            if SecretId == "web-auth":
                return {"SecretString": "w" * 32}
            raise AssertionError(f"unexpected eager secret read: {SecretId}")

    secrets = Secrets()
    import boto3

    monkeypatch.setattr(
        boto3,
        "resource",
        lambda service, **kwargs: Dynamo() if service == "dynamodb" else None,
    )
    monkeypatch.setattr(
        boto3,
        "client",
        lambda service, **kwargs: secrets if service == "secretsmanager" else object(),
    )
    for key, value in {
        "AWS_REGION": "eu-west-1",
        "CONTROL_TABLE_NAME": "control-table",
        "WEB_AUTH_SECRET_ID": "web-auth",
        "WEB_ORIGIN": "https://app.personal-operator.example",
    }.items():
        monkeypatch.setenv(key, value)
    for name in (
        "FOUNDER_USER_IDS",
        "GMAIL_SEND_CONNECTION_ID",
        "GMAIL_SEND_ACCOUNT_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)

    application = composition.build_production_application()

    assert application._approval_producer is None
    assert isinstance(application._card_actions, DynamoTelegramCardActions)
    assert isinstance(application._draft_preparer, ReadOnlyGmailDraftPreparer)
    assert (
        application._card_actions._connection_fence
        is application._draft_preparer._repository
    )
    assert secrets.reads == ["web-auth"]


def test_production_composition_wires_the_lazy_producer_without_a_send_secret(
    monkeypatch,
):
    class Dynamo:
        @staticmethod
        def Table(name):
            return LazyTable(name)

    class Secrets:
        def __init__(self):
            self.reads = []

        def get_secret_value(self, *, SecretId):
            self.reads.append(SecretId)
            if SecretId == "web-auth":
                return {"SecretString": "w" * 32}
            raise AssertionError(f"unexpected eager secret read: {SecretId}")

    secrets = Secrets()
    import boto3

    monkeypatch.setattr(
        boto3,
        "resource",
        lambda service, **kwargs: Dynamo() if service == "dynamodb" else None,
    )
    monkeypatch.setattr(
        boto3,
        "client",
        lambda service, **kwargs: secrets if service == "secretsmanager" else object(),
    )
    for key, value in {
        "AWS_REGION": "eu-west-1",
        "CONTROL_TABLE_NAME": "control-table",
        "WEB_AUTH_SECRET_ID": "web-auth",
        "FOUNDER_USER_IDS": "founder-1",
        "GMAIL_SEND_CONNECTION_ID": "google_conn_1234",
        "GMAIL_SEND_ACCOUNT_EMAIL": "founder@example.com",
        "WEB_ORIGIN": "https://app.personal-operator.example",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GOOGLE_SEND_OAUTH_SECRET_ID", raising=False)

    application = composition.build_production_application()

    assert isinstance(
        application._approval_producer,
        LazyFounderApprovalProducer,
    )
    assert application._approval_producer.prepare(
        user_id="pilot-1",
        opportunity=object(),
    ) is None
    assert secrets.reads == ["web-auth"]
