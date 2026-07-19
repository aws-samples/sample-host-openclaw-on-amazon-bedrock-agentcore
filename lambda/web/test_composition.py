from __future__ import annotations

import json
import ast
import io
import sys
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace

import pytest

from portable import FORMAT

from . import composition


REGION = "eu-west-1"
RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:"
    "agent/12345678-1234-1234-1234-123456789abc:1"
)
RELEASE_ENDPOINT = "release_" + "a" * 40


class SecretClient:
    def __init__(self, values):
        self.values = dict(values)
        self.calls = []
        self.management_calls = []

    def get_secret_value(self, *, SecretId):
        self.calls.append(SecretId)
        return {"SecretString": self.values[SecretId]}

    def describe_secret(self, *, SecretId):
        self.management_calls.append(("describe", SecretId))
        raise AssertionError("secret lifecycle must remain lazy during composition")

    def delete_secret(self, **kwargs):
        self.management_calls.append(("delete", kwargs))
        raise AssertionError("secret lifecycle must remain lazy during composition")


class SecretApiError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class DeletionSecretClient:
    ARN = (
        "arn:aws:secretsmanager:eu-west-1:123456789012:"
        "secret:google-send-AbCd12"
    )

    def __init__(self, *, descriptions, deletion=None):
        self.descriptions = list(descriptions)
        self.deletion = deletion
        self.calls = []

    def describe_secret(self, *, SecretId):
        self.calls.append(("describe", SecretId))
        response = self.descriptions.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def delete_secret(self, **kwargs):
        self.calls.append(("delete", kwargs))
        if isinstance(self.deletion, BaseException):
            raise self.deletion
        return self.deletion


class LocalConnectionRevoker:
    def __init__(self, events):
        self.events = events

    def revoke_all(self, user_id):
        self.events.append(("local", user_id))


def test_account_record_deletion_includes_pseudonymous_scan_measurements():
    events = []

    class Deleter:
        def __init__(self, name):
            self.name = name

        def delete_user_records(self, user_id):
            events.append((self.name, user_id))

    records = composition.CompositeUserRecordDeleter(
        Deleter("records"),
        Deleter("scan-measurements"),
    )

    records.delete_user_records("pilot-1")

    assert events == [("records", "pilot-1"), ("scan-measurements", "pilot-1")]


def live_send_secret():
    return {"Name": "google-send", "ARN": DeletionSecretClient.ARN}


def scheduled_send_secret():
    return {
        **live_send_secret(),
        "DeletedDate": datetime(2026, 7, 25, tzinfo=timezone.utc),
    }


def accepted_send_secret_deletion():
    return {
        **live_send_secret(),
        "DeletionDate": datetime(2026, 7, 25, tzinfo=timezone.utc),
    }


def test_founder_connection_revocation_schedules_exact_secret_before_local_records():
    local_events = []
    secrets = DeletionSecretClient(
        descriptions=[live_send_secret()],
        deletion=accepted_send_secret_deletion(),
    )
    revoker = composition.CompositeConnectionRevoker(
        composition.SecretsManagerFounderConnectionRevoker(
            secret_client=secrets,
            secret_id="google-send",
            founder_user_id="founder-1",
        ),
        LocalConnectionRevoker(local_events),
    )

    revoker.revoke_all("founder-1")

    assert secrets.calls == [
        ("describe", "google-send"),
        (
            "delete",
            {"SecretId": "google-send", "RecoveryWindowInDays": 7},
        ),
    ]
    assert local_events == [("local", "founder-1")]


def test_pilot_connection_revocation_never_touches_founder_secret():
    local_events = []
    secrets = DeletionSecretClient(descriptions=[])
    revoker = composition.CompositeConnectionRevoker(
        composition.SecretsManagerFounderConnectionRevoker(
            secret_client=secrets,
            secret_id="google-send",
            founder_user_id="founder-1",
        ),
        LocalConnectionRevoker(local_events),
    )

    revoker.revoke_all("pilot-1")

    assert secrets.calls == []
    assert local_events == [("local", "pilot-1")]


def test_deployment_without_founder_needs_no_send_secret_lifecycle_client():
    local_events = []
    revoker = composition.CompositeConnectionRevoker(
        composition.SecretsManagerFounderConnectionRevoker(
            secret_client=object(),
            secret_id="",
            founder_user_id=None,
        ),
        LocalConnectionRevoker(local_events),
    )

    revoker.revoke_all("pilot-1")

    assert local_events == [("local", "pilot-1")]


def test_founder_connection_revocation_is_idempotent_while_secret_is_scheduled():
    local_events = []
    secrets = DeletionSecretClient(
        descriptions=[scheduled_send_secret(), scheduled_send_secret()]
    )
    revoker = composition.CompositeConnectionRevoker(
        composition.SecretsManagerFounderConnectionRevoker(
            secret_client=secrets,
            secret_id="google-send",
            founder_user_id="founder-1",
        ),
        LocalConnectionRevoker(local_events),
    )

    revoker.revoke_all("founder-1")
    revoker.revoke_all("founder-1")

    assert secrets.calls == [
        ("describe", "google-send"),
        ("describe", "google-send"),
    ]
    assert local_events == [
        ("local", "founder-1"),
        ("local", "founder-1"),
    ]


def test_ambiguous_secret_delete_is_accepted_only_after_exact_scheduled_state():
    local_events = []
    secrets = DeletionSecretClient(
        descriptions=[live_send_secret(), scheduled_send_secret()],
        deletion=TimeoutError("outcome unknown"),
    )
    revoker = composition.CompositeConnectionRevoker(
        composition.SecretsManagerFounderConnectionRevoker(
            secret_client=secrets,
            secret_id="google-send",
            founder_user_id="founder-1",
        ),
        LocalConnectionRevoker(local_events),
    )

    revoker.revoke_all("founder-1")

    assert secrets.calls == [
        ("describe", "google-send"),
        (
            "delete",
            {"SecretId": "google-send", "RecoveryWindowInDays": 7},
        ),
        ("describe", "google-send"),
    ]
    assert local_events == [("local", "founder-1")]


def test_unreconciled_secret_delete_fails_closed_before_local_revocation():
    local_events = []
    secrets = DeletionSecretClient(
        descriptions=[live_send_secret(), live_send_secret()],
        deletion=TimeoutError("outcome unknown"),
    )
    revoker = composition.CompositeConnectionRevoker(
        composition.SecretsManagerFounderConnectionRevoker(
            secret_client=secrets,
            secret_id="google-send",
            founder_user_id="founder-1",
        ),
        LocalConnectionRevoker(local_events),
    )

    with pytest.raises(
        composition.ProductionConfigurationError,
        match="revocation outcome is unproven",
    ) as raised:
        revoker.revoke_all("founder-1")

    assert raised.value.__cause__ is None
    assert local_events == []


def test_absent_exact_founder_secret_is_an_idempotent_revocation_result():
    local_events = []
    secrets = DeletionSecretClient(
        descriptions=[SecretApiError("ResourceNotFoundException")]
    )
    revoker = composition.CompositeConnectionRevoker(
        composition.SecretsManagerFounderConnectionRevoker(
            secret_client=secrets,
            secret_id="google-send",
            founder_user_id="founder-1",
        ),
        LocalConnectionRevoker(local_events),
    )

    revoker.revoke_all("founder-1")

    assert secrets.calls == [("describe", "google-send")]
    assert local_events == [("local", "founder-1")]


def base_env(monkeypatch):
    values = {
        "AWS_REGION": REGION,
        "CONTROL_TABLE_NAME": "control-table",
        "RUNTIME_STATE_TABLE_NAME": "runtime-table",
        "CAPABILITY_STATE_TABLE_NAME": "capability-state-table",
        "SCHEDULER_CONTROL_FUNCTION_ARN": (
            "arn:aws:lambda:eu-west-1:123456789012:function:"
            "personal-operator-scheduler-control"
        ),
        "IDENTITY_TABLE_NAME": "identity-table",
        "MESSAGE_LEDGER_TABLE_NAME": "message-ledger-table",
        "USER_FILES_BUCKET_NAME": "workspace-bucket",
        "AGENTCORE_RUNTIME_ARN": RUNTIME_ARN,
        "AGENTCORE_QUALIFIER": RELEASE_ENDPOINT,
        "WEB_AUTH_SECRET_ID": "web-auth",
        "APPROVAL_SIGNING_SECRET_ID": "approval-signing",
        "GOOGLE_READONLY_OAUTH_SECRET_ID": "google-readonly",
        "GOOGLE_SEND_OAUTH_SECRET_ID": "google-send",
        "OAUTH_KMS_KEY_ID": "arn:aws:kms:eu-west-1:123456789012:key/1234",
        "FOUNDER_USER_IDS": "founder-1",
        "WEB_ORIGIN": "https://app.personal-operator.example",
        "GOOGLE_REDIRECT_URI": (
            "https://app.personal-operator.example/oauth/google/callback"
        ),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    return values


def test_exact_region_is_checked_before_any_aws_or_provider_import(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delitem(sys.modules, "boto3", raising=False)

    with pytest.raises(
        composition.ProductionConfigurationError, match="exact eu-west-1"
    ):
        composition.build_production_application()


def test_founder_allowlist_accepts_at_most_one_exact_identity():
    assert composition._founder_ids("") == frozenset()
    assert composition._founder_ids("founder-1") == frozenset({"founder-1"})
    with pytest.raises(
        composition.ProductionConfigurationError,
        match="exactly one",
    ):
        composition._founder_ids("founder-1,founder-2")


def test_json_provider_secret_rejects_placeholder_unknown_and_missing_fields():
    required = {"client_id", "client_secret"}
    for value in (
        {"client_id": "REPLACE_ME", "client_secret": "secret"},
        {"client_id": "client"},
        {"client_id": "client", "client_secret": "secret", "access_token": "x"},
    ):
        secrets = SecretClient({"provider": json.dumps(value)})
        with pytest.raises(composition.ProductionConfigurationError):
            composition._json_secret(
                secrets,
                "provider",
                required=required,
                optional={"bootstrap_nonce"},
            )


def test_send_provider_secret_is_not_read_until_exact_executor_is_prepared():
    secrets = SecretClient(
        {
            "send": json.dumps(
                {
                    "client_id": "REPLACE_ME",
                    "client_secret": "REPLACE_ME",
                    "refresh_token": "REPLACE_ME",
                    "email": "REPLACE_ME",
                    "connection_id": "REPLACE_ME",
                    "user_id": "founder-1",
                    "bootstrap_nonce": "random",
                }
            )
        }
    )
    class Revoker:
        def revoke_all(self, _connection_ref):
            raise AssertionError("revocation must not run during dispatch construction")

    factory = composition.ProductionGmailExecutorFactory(
        secret_client=secrets,
        secret_id="send",
        state_machine=object(),
        founder_user_ids={"founder-1"},
        deletion_blocked=lambda _user_id: False,
        connection_revoker=Revoker(),
    )
    assert secrets.calls == []

    with pytest.raises(
        composition.ProductionConfigurationError, match="placeholder"
    ):
        factory(
            {
                "actionId": "action_12345678",
                "userId": "founder-1",
                "connectionId": "google_conn_1234",
                "accountEmail": "founder@example.com",
                "senderAddress": "founder@example.com",
            }
        )

    assert secrets.calls == ["send"]


def test_production_gmail_factory_requires_revocation_and_returns_kernel_dispatcher(
    monkeypatch,
):
    class Revoker:
        def revoke_all(self, _connection_ref):
            pass

    with pytest.raises((TypeError, ValueError)):
        composition.ProductionGmailExecutorFactory(
            secret_client=object(),
            secret_id="send",
            state_machine=object(),
            founder_user_ids={"founder-1"},
            deletion_blocked=lambda _user_id: False,
        )

    factory = composition.ProductionGmailExecutorFactory(
        secret_client=object(),
        secret_id="send",
        state_machine=object(),
        founder_user_ids={"founder-1"},
        deletion_blocked=lambda _user_id: False,
        connection_revoker=Revoker(),
    )
    monkeypatch.setattr(factory, "_assert_deletion_allows", lambda action: action["userId"])
    monkeypatch.setattr(
        factory,
        "_provider",
        lambda _action: (object(), "google_conn_1234", "founder@example.com", "founder-1"),
    )

    dispatcher = factory(
        {
            "actionId": "action_12345678",
            "userId": "founder-1",
            "connectionId": "google_conn_1234",
            "accountEmail": "founder@example.com",
            "senderAddress": "founder@example.com",
        }
    )

    from actions.connectors import GenericConnectorKernel

    assert isinstance(dispatcher, GenericConnectorKernel)
    assert callable(dispatcher.dispatch)


@pytest.mark.parametrize("reconciliation", [False, True])
def test_deletion_intent_blocks_send_secret_and_oauth_before_provider_construction(
    reconciliation,
):
    secrets = SecretClient({"send": "must-not-be-read"})

    class Tokens:
        def __init__(self):
            self.calls = []

        def refresh(self, **kwargs):
            self.calls.append(kwargs)
            raise AssertionError("OAuth refresh must be unreachable")

    tokens = Tokens()
    factory = composition.ProductionGmailExecutorFactory(
        secret_client=secrets,
        secret_id="send",
        state_machine=object(),
        founder_user_ids={"founder-1"},
        deletion_blocked=lambda user_id: user_id == "founder-1",
        connection_revoker=LocalConnectionRevoker([]),
        token_client=tokens,
    )
    action = {
        "actionId": "action_12345678",
        "userId": "founder-1",
        "connectionId": "google_conn_1234",
        "accountEmail": "founder@example.com",
        "senderAddress": "founder@example.com",
    }

    if reconciliation:
        observer = composition.ProductionGmailReconcilerFactory(
            provider_factory=factory,
            repository=object(),
            state_machine=object(),
            founder_user_ids={"founder-1"},
        )(action)
        assert observer.reconcile(
            action_id="action_12345678", user_id="founder-1"
        ) is None
    else:
        with pytest.raises(
            composition.AccountDeletionBlocked,
            match="account deletion",
        ):
            factory(action)

    assert secrets.calls == []
    assert tokens.calls == []


def test_uncertain_deletion_read_blocks_reconciliation_before_secret_or_oauth():
    secrets = SecretClient({"send": "must-not-be-read"})

    class Tokens:
        def __init__(self):
            self.calls = []

        def refresh(self, **kwargs):
            self.calls.append(kwargs)
            raise AssertionError("OAuth refresh must be unreachable")

    def unavailable(_user_id):
        raise TimeoutError("strong deletion read unavailable")

    tokens = Tokens()
    providers = composition.ProductionGmailExecutorFactory(
        secret_client=secrets,
        secret_id="send",
        state_machine=object(),
        founder_user_ids={"founder-1"},
        deletion_blocked=unavailable,
        connection_revoker=LocalConnectionRevoker([]),
        token_client=tokens,
    )
    observers = composition.ProductionGmailReconcilerFactory(
        provider_factory=providers,
        repository=object(),
        state_machine=object(),
        founder_user_ids={"founder-1"},
    )

    with pytest.raises(
        composition.DeletionFenceUnavailable,
        match="unavailable",
    ) as raised:
        observers(
            {
                "actionId": "action_12345678",
                "userId": "founder-1",
                "connectionId": "google_conn_1234",
                "accountEmail": "founder@example.com",
                "senderAddress": "founder@example.com",
            }
        )

    assert raised.value.__cause__ is None
    assert secrets.calls == []
    assert tokens.calls == []


def test_send_executor_rejects_cross_founder_secret_before_token_refresh():
    secrets = SecretClient(
        {
            "send": json.dumps(
                {
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                    "refresh_token": "refresh-token",
                    "email": "founder@example.com",
                    "connection_id": "google_conn_1234",
                    "user_id": "founder-1",
                }
            )
        }
    )

    class Tokens:
        def __init__(self):
            self.calls = []

        def refresh(self, **kwargs):
            self.calls.append(kwargs)
            return "access-token"

    tokens = Tokens()
    factory = composition.ProductionGmailExecutorFactory(
        secret_client=secrets,
        secret_id="send",
        state_machine=object(),
        founder_user_ids={"founder-1", "founder-2"},
        deletion_blocked=lambda _user_id: False,
        connection_revoker=LocalConnectionRevoker([]),
        token_client=tokens,
    )

    with pytest.raises(PermissionError, match="founder identity"):
        factory(
            {
                "actionId": "action_12345678",
                "userId": "founder-2",
                "connectionId": "google_conn_1234",
                "accountEmail": "founder@example.com",
                "senderAddress": "founder@example.com",
            }
        )

    assert secrets.calls == ["send"]
    assert tokens.calls == []


def test_user_workspace_view_reads_only_files_prefix_not_internal_snapshots():
    class S3:
        def __init__(self):
            self.prefixes = []
            self.objects = {
                "founder-1/files/memory.md": b"hello",
                "founder-1/files/notes/plan.md": b"plan",
                "founder-1/.system/workspace/v1/current.json": b"internal",
                (
                    "founder-1/.system/workspace/v1/"
                    "generations/deleted-parent/payload"
                ): b"old-internal-generation",
                "other-user/files/secret.md": b"other",
            }

        def list_objects_v2(self, **kwargs):
            self.prefixes.append(kwargs["Prefix"])
            return {
                "Contents": [
                    {"Key": key, "Size": len(value)}
                    for key, value in self.objects.items()
                    if key.startswith(kwargs["Prefix"])
                ],
                "IsTruncated": False,
            }

        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}

    s3 = S3()
    files = composition.S3UserFilesStore(
        s3, bucket_name="workspace-bucket"
    ).workspace_files("founder-1")

    assert files == {"memory.md": b"hello", "notes/plan.md": b"plan"}
    assert s3.prefixes == ["founder-1/files/"]
    assert all(".system" not in path for path in files)


@pytest.mark.parametrize(
    "pages",
    [
        [
            {
                "Contents": [],
                "IsTruncated": "false",
                "NextContinuationToken": "next",
            },
            {"Contents": [], "IsTruncated": False},
        ],
        [
            {
                "Contents": [],
                "IsTruncated": False,
                "Prefix": "other-user/files/",
            }
        ],
        [
            {
                "Contents": [],
                "IsTruncated": False,
                "NextContinuationToken": "impossible-next-page",
            }
        ],
        [
            {
                "Contents": [],
                "IsTruncated": True,
                "NextContinuationToken": "same",
            },
            {
                "Contents": [],
                "IsTruncated": True,
                "NextContinuationToken": "same",
            },
        ],
        [
            *[
                {
                    "Contents": [],
                    "IsTruncated": True,
                    "NextContinuationToken": f"page-{page}",
                }
                for page in range(20)
            ],
            {"Contents": [], "IsTruncated": False},
        ],
    ],
)
def test_user_workspace_view_rejects_ambiguous_or_unbounded_pagination(pages):
    class S3:
        def __init__(self):
            self.pages = list(pages)

        def list_objects_v2(self, **_kwargs):
            return self.pages.pop(0)

    with pytest.raises(composition.DataAdapterError):
        composition.S3UserFilesStore(
            S3(), bucket_name="workspace-bucket"
        ).workspace_files("founder-1")


class TokenResponse:
    def __init__(self, payload):
        self.status = 200
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self._payload


def test_founder_token_requires_readonly_plus_incremental_send_and_one_attempt():
    calls = []

    def urlopen(request, *, timeout):
        calls.append((request, timeout))
        return TokenResponse(
            {
                "access_token": "short-lived-access",
                "expires_in": 3_600,
                "scope": " ".join(sorted(composition.FOUNDER_GMAIL_SCOPES)),
                "token_type": "Bearer",
            }
        )

    token = composition._GoogleSendTokenClient(urlopen=urlopen).refresh(
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token",
    )

    assert token == "short-lived-access"
    assert len(calls) == 1
    assert calls[0][0].full_url == "https://oauth2.googleapis.com/token"
    assert calls[0][1] == 5


@pytest.mark.parametrize(
    "scope",
    [
        composition.GMAIL_SEND_SCOPE,
        (
            f"{composition.GMAIL_SEND_SCOPE} "
            "https://www.googleapis.com/auth/drive.readonly"
        ),
    ],
)
def test_founder_token_rejects_insufficient_or_extra_google_authority(scope):
    calls = []

    def urlopen(_request, *, timeout):
        calls.append(timeout)
        return TokenResponse(
            {
                "access_token": "short-lived-access",
                "expires_in": 3_600,
                "scope": scope,
                "token_type": "Bearer",
            }
        )

    with pytest.raises(
        composition.ProductionConfigurationError,
        match="OAuth refresh failed",
    ) as raised:
        composition._GoogleSendTokenClient(urlopen=urlopen).refresh(
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
        )
    assert len(calls) == 1
    assert raised.value.__cause__ is None


class FakeTable:
    def __init__(self, name):
        self.name = name

    def put_item(self, **_kwargs):
        raise AssertionError("lazy production port was unexpectedly called")

    def update_item(self, **_kwargs):
        raise AssertionError("lazy production port was unexpectedly called")

    def query(self, **_kwargs):
        raise AssertionError("lazy production port was unexpectedly called")

    def get_item(self, **_kwargs):
        raise AssertionError("lazy production port was unexpectedly called")

    def delete_item(self, **_kwargs):
        raise AssertionError("lazy production port was unexpectedly called")


class FakeDynamoResource:
    def __init__(self):
        self.tables = {}

    def Table(self, name):
        self.tables.setdefault(name, FakeTable(name))
        return self.tables[name]


class FakeS3:
    pass


class FakeAgentCore:
    meta = SimpleNamespace(region_name=REGION)


class FakeKms:
    pass


class FakeDynamoClient:
    def get_item(self, **_kwargs):
        raise AssertionError("capability deletion fence was unexpectedly called")

    def put_item(self, **_kwargs):
        raise AssertionError("capability deletion fence was unexpectedly called")

    def update_item(self, **_kwargs):
        raise AssertionError("capability deletion fence was unexpectedly called")

    def query(self, **_kwargs):
        raise AssertionError("capability deletion fence was unexpectedly called")

    def transact_write_items(self, **_kwargs):
        raise AssertionError("capability deletion fence was unexpectedly called")


class FakeLambda:
    def invoke(self, **_kwargs):
        raise AssertionError("schedule control was unexpectedly called")


def install_fake_aws(monkeypatch, secret_client):
    dynamo = FakeDynamoResource()

    class Boto3(ModuleType):
        @staticmethod
        def resource(name, **kwargs):
            assert name == "dynamodb"
            assert kwargs["region_name"] == REGION
            assert kwargs["config"].retries["max_attempts"] == 0
            return dynamo

        @staticmethod
        def client(name, **kwargs):
            assert kwargs["region_name"] == REGION
            assert kwargs["config"].retries["max_attempts"] == 0
            return {
                "secretsmanager": secret_client,
                "kms": FakeKms(),
                "s3": FakeS3(),
                "bedrock-agentcore": FakeAgentCore(),
                "dynamodb": FakeDynamoClient(),
                "lambda": FakeLambda(),
            }[name]

    class Config:
        def __init__(self, **kwargs):
            self.retries = kwargs.get("retries", {})
            self.connect_timeout = kwargs.get("connect_timeout")
            self.read_timeout = kwargs.get("read_timeout")

    boto3 = Boto3("boto3")
    botocore = ModuleType("botocore")
    botocore_config = ModuleType("botocore.config")
    botocore_config.Config = Config
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", botocore_config)
    return dynamo


def test_production_builder_reads_only_web_auth_secret_and_defers_provider_paths(
    monkeypatch,
):
    base_env(monkeypatch)
    secrets = SecretClient(
        {
            "web-auth": "w" * 64,
            "approval-signing": "a" * 64,
            "google-readonly": json.dumps(
                {
                    "client_id": "REPLACE_ME",
                    "client_secret": "REPLACE_ME",
                    "bootstrap_nonce": "random",
                }
            ),
            "google-send": json.dumps(
                {
                    "client_id": "REPLACE_ME",
                    "client_secret": "REPLACE_ME",
                    "refresh_token": "REPLACE_ME",
                    "email": "REPLACE_ME",
                    "connection_id": "REPLACE_ME",
                    "user_id": "founder-1",
                    "bootstrap_nonce": "random",
                }
            ),
        }
    )
    install_fake_aws(monkeypatch, secrets)

    application = composition.build_production_application()

    assert application is not None
    from portable.exporter import PortableExporter

    assert isinstance(application._exporter, PortableExporter)
    assert (
        application._importer._staging
        is application._workspace._workspace._portable._store
    )
    assert (
        application._exporter._source._records._installation_table.name
        == "capability-state-table"
    )
    assert application._retention._action_maintenance is not None
    assert isinstance(
        application._deletion._connections,
        composition.CompositeConnectionRevoker,
    )
    provider_revoker, local_revoker = application._deletion._connections._revokers
    assert isinstance(
        provider_revoker,
        composition.KernelConnectionRevoker,
    )
    founder_revoker = provider_revoker._kernel._adapter._connection_revoker
    assert isinstance(
        founder_revoker,
        composition.SecretsManagerFounderConnectionRevoker,
    )
    assert founder_revoker._secret_id == "google-send"
    assert founder_revoker._founder_user_id == "founder-1"
    records, scans, capability_deletion = application._deletion._records._deleters
    assert local_revoker is records
    assert scans is application._scans
    assert isinstance(
        capability_deletion,
        composition.DynamoCapabilityDeletionAdapter,
    )
    assert application._deletion._authority_fence is capability_deletion
    assert (
        application._connections._repository
        is application._gmail_workspace._repository
    )
    assert application._gmail_workspace._enforce_connection_fence is True
    assert secrets.management_calls == []
    assert secrets.calls == ["web-auth"]
    with pytest.raises(
        composition.ProductionConfigurationError, match="placeholder"
    ):
        application._oauth.start(
            user_id="founder-1",
            redirect_uri=(
                "https://app.personal-operator.example/oauth/google/callback"
            ),
        )
    assert secrets.calls == ["web-auth", "google-readonly"]


def test_export_source_strong_reads_and_merges_active_portable_generation():
    class Records:
        def records_for_user(self, user_id):
            assert user_id == "founder-1"
            return {
                "memory": [{"native": True}],
                "schedules": [],
                "installed_packs": [],
                "connectors": [],
                "compute_receipts": [],
                "receipts": [],
            }

    class Workspace:
        def workspace_files(self, user_id):
            assert user_id == "founder-1"
            return {"native.md": b"native", "shared.md": b"newer-native"}

    class Portable:
        def __init__(self):
            self.reads = []

        def load_live(self, user_id):
            self.reads.append(user_id)
            return {
                "userId": user_id,
                "generation": 1,
                "bundleHash": "a" * 64,
                "staged": {
                    "format": FORMAT,
                    "records": {
                        "memory": [{"imported": True}],
                        "schedules": [
                            {
                                "name": "weekly",
                                "state": "DISABLED",
                                "userId": user_id,
                            }
                        ],
                        "installed_packs": [],
                        "connectors": [],
                        "compute_receipts": [],
                        "receipts": [],
                    },
                    "workspace": {
                        "imported.md": {
                            "encoding": "base64",
                            "data": "aW1wb3J0ZWQ=",
                            "sha256": "5f54227b74fbba7743c47cd286b4873f2e17331518d56facfc03e34cde4a0950",
                        },
                        "shared.md": {
                            "encoding": "base64",
                            "data": "b2xkZXItaW1wb3J0",
                            "sha256": "c4e7bb6d36c1e3ab374bb2c886e7a60b93905fd006fd997deb720aaafa85defc",
                        },
                    },
                    "landing": {
                        "schedules": "DISABLED",
                        "installedPacks": "PAUSED",
                        "connectors": "DISCONNECTED",
                        "computeReceipts": {"replayable": False},
                        "receipts": {"replayable": False},
                    },
                },
            }

    portable = Portable()
    source = composition._ExportSource(
        records=Records(), workspace=Workspace(), portable=portable
    )

    records = source.records_for_user("founder-1")
    files = source.workspace_files("founder-1")

    assert records["memory"] == [{"imported": True}, {"native": True}]
    assert records["schedules"] == [
        {"name": "weekly", "state": "DISABLED", "userId": "founder-1"}
    ]
    assert records["installed_packs"] == []
    assert records["connectors"] == []
    assert records["compute_receipts"] == []
    assert records["receipts"] == []
    assert files == {
        "imported.md": b"imported",
        "shared.md": b"newer-native",
        "native.md": b"native",
    }
    assert portable.reads == ["founder-1", "founder-1"]


def test_source_has_no_top_level_google_or_openai_provider_imports():
    with open(composition.__file__, encoding="utf-8") as source:
        tree = ast.parse(source.read())
    top_level = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    imported = {
        alias.name.split(".", 1)[0]
        for node in top_level
        for alias in node.names
    }
    assert imported.isdisjoint({"google", "googleapiclient", "openai"})
