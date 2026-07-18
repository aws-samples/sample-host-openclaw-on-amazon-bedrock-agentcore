"""Production composition for provider-backed trusted product commands."""

from __future__ import annotations

import json
import os
import re
from typing import Callable, Mapping

from actions.models import gmail_resource
from actions.repository import DynamoActionRepository
from actions.state_machine import ActionStateMachine, ApprovalService, ApprovalTokenCodec
from .index import ControlApplication
from .telegram_cards import DynamoTelegramCardActions, ReadOnlyGmailDraftPreparer
from web.auth import SignedConnectTickets
from web.measurements import DynamoScanMeasurements
from web.stores import DynamoWebStore
from workflows.founder_approval import FounderApprovalProducer
from workflows.gmail.oauth import CryptographyAesGcm, GoogleOAuthTokenClient, KmsEnvelopeTokenVault
from workflows.gmail.ranker import GmailOpportunityRanker
from workflows.gmail.repository import DynamoGmailRepository
from workflows.gmail.scanner import GmailScanner, GoogleGmailApiClient
from workflows.index import GmailPilotWorkflow


REQUIRED_REGION = "eu-west-1"
READONLY_PROVIDER = "google-gmail-readonly"
_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")


class ProductionConfigurationError(RuntimeError):
    pass


class LazyGmailService:
    """Keep optional provider credentials off every non-scan command path."""

    def __init__(self, factory: Callable[[], object]) -> None:
        if not callable(factory):
            raise TypeError("Gmail service factory must be callable")
        self._factory = factory
        self._service = None

    def scan(self, *, user_id: str):
        if self._service is None:
            candidate = self._factory()
            if not callable(getattr(candidate, "scan", None)):
                raise TypeError("Gmail service factory returned an invalid service")
            self._service = candidate
        return self._service.scan(user_id=user_id)


class LazyFounderApprovalProducer:
    """Do not resolve approval signing authority for any nonfounder pilot."""

    def __init__(
        self,
        *,
        founder_user_id: str,
        factory: Callable[[], object],
    ) -> None:
        if _USER_ID.fullmatch(founder_user_id or "") is None:
            raise ValueError("founder identity is invalid")
        if not callable(factory):
            raise TypeError("founder approval producer factory must be callable")
        self._founder = founder_user_id
        self._factory = factory
        self._producer = None

    def prepare(self, *, user_id: str, opportunity):
        if user_id != self._founder:
            return None
        if self._producer is None:
            candidate = self._factory()
            if not callable(getattr(candidate, "prepare", None)):
                raise TypeError("founder approval producer factory returned an invalid value")
            self._producer = candidate
        return self._producer.prepare(user_id=user_id, opportunity=opportunity)


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ProductionConfigurationError(f"control configuration missing: {name}")
    return value


def _exact_founder_id(value: object) -> str:
    if not isinstance(value, str):
        raise ProductionConfigurationError("control requires exactly one founder")
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if len(parts) != 1 or _USER_ID.fullmatch(parts[0]) is None:
        raise ProductionConfigurationError("control requires exactly one founder")
    return parts[0]


def _optional_founder_binding() -> tuple[str, str, str] | None:
    """Return an exact effect binding, or keep every pilot read-only."""

    names = (
        "FOUNDER_USER_IDS",
        "GMAIL_SEND_CONNECTION_ID",
        "GMAIL_SEND_ACCOUNT_EMAIL",
    )
    values = tuple(os.environ.get(name, "") for name in names)
    configured = tuple(bool(value) for value in values)
    if not any(configured):
        return None
    if not all(configured):
        raise ProductionConfigurationError(
            "founder effect configuration requires all three binding values"
        )
    founder_user_id = _exact_founder_id(values[0])
    connection_id = values[1]
    account_email = values[2]
    if "REPLACE_ME" in values:
        raise ProductionConfigurationError("founder effect binding is a placeholder")
    try:
        gmail_resource(
            connection_id=connection_id,
            account_email=account_email,
        )
    except (TypeError, ValueError) as error:
        raise ProductionConfigurationError("founder effect binding is invalid") from error
    return founder_user_id, connection_id, account_email


def _require_founder_account(
    *,
    user_id: str,
    connected_address: str,
    founder_user_id: str,
    founder_account_email: str,
) -> None:
    if (
        user_id == founder_user_id
        and connected_address.casefold() != founder_account_email.casefold()
    ):
        raise RuntimeError("founder Gmail read account binding is invalid")


def _build_google_readonly_service(
    credentials,
    *,
    http_factory=None,
    authorized_http_factory=None,
    build_service=None,
):
    """Build the read-only client with one bounded transport attempt.

    Google request objects separately receive ``num_retries=0`` in the
    read-only adapter. This transport timeout bounds the underlying socket so a
    provider stall cannot consume the complete command invocation.
    """

    if (
        http_factory is None
        or authorized_http_factory is None
        or build_service is None
    ):
        from google_auth_httplib2 import AuthorizedHttp
        from googleapiclient.discovery import build
        from httplib2 import Http

        http_factory = http_factory or Http
        authorized_http_factory = authorized_http_factory or AuthorizedHttp
        build_service = build_service or build
    transport = http_factory(timeout=10)
    authorized = authorized_http_factory(
        credentials,
        http=transport,
        max_refresh_attempts=0,
    )
    return build_service(
        "gmail",
        "v1",
        http=authorized,
        cache_discovery=False,
    )


def _secret_string(client, secret_id: str) -> str:
    try:
        response = client.get_secret_value(SecretId=secret_id)
        value = response.get("SecretString")
    except Exception:
        raise ProductionConfigurationError("control secret is unavailable") from None
    if not isinstance(value, str) or not value or len(value) > 64 * 1024:
        raise ProductionConfigurationError("control secret is invalid")
    return value


def _json_secret(client, secret_id: str, fields: set[str]) -> dict[str, str]:
    try:
        value = json.loads(_secret_string(client, secret_id))
    except json.JSONDecodeError:
        raise ProductionConfigurationError("control secret JSON is invalid") from None
    if not isinstance(value, dict) or not fields.issubset(value):
        raise ProductionConfigurationError("control secret fields are invalid")
    result = {field: value[field] for field in fields}
    if any(not isinstance(item, str) or not item or item == "REPLACE_ME" for item in result.values()):
        raise ProductionConfigurationError("control secret is still a placeholder")
    return result


class DynamoTaskReader:
    def __init__(self, table) -> None:
        self._table = table

    def list_open(self, user_id: str) -> list[dict[str, str]]:
        if not isinstance(user_id, str) or _USER_ID.fullmatch(user_id) is None:
            raise ValueError("user identity is invalid")
        response = self._table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={
                ":pk": f"USER#{user_id}",
                ":prefix": "ACTION#",
            },
            ConsistentRead=True,
            Limit=50,
        )
        items = response.get("Items") if isinstance(response, Mapping) else None
        if not isinstance(items, list):
            raise RuntimeError("task store returned invalid data")
        open_states = {"PREPARED", "APPROVAL_PENDING", "APPROVED", "DISPATCHING", "UNCERTAIN"}
        return [
            {
                "title": str(item.get("title") or item.get("capability") or "Task")[:120],
                "state": str(item.get("state", ""))[:40],
            }
            for item in items
            if isinstance(item, Mapping) and item.get("state") in open_states
        ][:10]


class ProductionGmailService:
    """Decrypt, refresh, and use Gmail tokens only inside this trusted Lambda."""

    def __init__(
        self,
        *,
        token_vault,
        token_client,
        client_id: str,
        repository,
        openai_api_key: str,
        openai_model: str,
        founder_user_id: str | None,
        founder_account_email: str | None,
    ) -> None:
        self._vault = token_vault
        self._tokens = token_client
        self._client_id = client_id
        self._repository = repository
        self._openai_key = openai_api_key
        self._openai_model = openai_model
        self._founder_user_id = founder_user_id
        self._founder_account_email = founder_account_email

    def scan(self, *, user_id: str):
        generation = self._repository.connected_generation(user_id)
        token = self._vault.load(user_id=user_id, provider=READONLY_PROVIDER)
        self._repository.assert_generation(
            user_id, generation, require_connected=True
        )
        if not isinstance(token, Mapping) or not isinstance(token.get("refresh_token"), str):
            raise RuntimeError("Gmail read-only connection is not configured")
        refreshed = self._tokens.refresh(
            refresh_token=token["refresh_token"],
            client_id=self._client_id,
        )
        self._vault.save(
            user_id=user_id,
            provider=READONLY_PROVIDER,
            token=refreshed,
            expected_generation=generation,
        )
        try:
            from google.oauth2.credentials import Credentials
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("trusted provider dependencies are not packaged") from None
        credentials = Credentials(token=refreshed["access_token"])
        service = _build_google_readonly_service(credentials)
        try:
            profile = service.users().getProfile(userId="me").execute(num_retries=0)
        except Exception:
            raise RuntimeError("Gmail profile lookup failed") from None
        connected_address = profile.get("emailAddress") if isinstance(profile, Mapping) else None
        if not isinstance(connected_address, str) or "@" not in connected_address:
            raise RuntimeError("Gmail profile is invalid")
        _require_founder_account(
            user_id=user_id,
            connected_address=connected_address,
            founder_user_id=self._founder_user_id,
            founder_account_email=self._founder_account_email,
        )
        scanner = GmailScanner(
            GoogleGmailApiClient(service),
            connected_address=connected_address,
        )
        ranker = GmailOpportunityRanker(
            OpenAI(api_key=self._openai_key, max_retries=0, timeout=20.0),
            model=self._openai_model,
        )
        return GmailPilotWorkflow(
            scanner=scanner,
            ranker=ranker,
            repository=self._repository,
        ).scan(user_id=user_id, expected_generation=generation)


def build_production_application() -> ControlApplication:
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if region != REQUIRED_REGION:
        raise ProductionConfigurationError("control Lambda requires exact eu-west-1 region")
    import boto3
    from botocore.config import Config

    no_retries = Config(
        connect_timeout=3,
        read_timeout=5,
        retries={"max_attempts": 0},
    )
    dynamodb = boto3.resource("dynamodb", region_name=region, config=no_retries)
    secrets = boto3.client("secretsmanager", region_name=region, config=no_retries)
    kms = boto3.client("kms", region_name=region, config=no_retries)
    table = dynamodb.Table(_required("CONTROL_TABLE_NAME"))
    web_secret = _secret_string(secrets, _required("WEB_AUTH_SECRET_ID")).encode()
    if len(web_secret) < 32:
        raise ProductionConfigurationError("web auth secret is too short")
    repository = DynamoGmailRepository(table)
    web_store = DynamoWebStore(table)
    founder_binding = _optional_founder_binding()
    founder_user_id = founder_binding[0] if founder_binding is not None else None
    founder_connection_id = founder_binding[1] if founder_binding is not None else None
    founder_account_email = founder_binding[2] if founder_binding is not None else None

    def provider_service() -> ProductionGmailService:
        google = _json_secret(
            secrets,
            _required("GOOGLE_READONLY_OAUTH_SECRET_ID"),
            {"client_id", "client_secret"},
        )
        openai_raw = _secret_string(secrets, _required("OPENAI_API_KEY_SECRET_ID"))
        try:
            parsed_openai = json.loads(openai_raw)
        except json.JSONDecodeError:
            parsed_openai = None
        openai_key = (
            parsed_openai.get("api_key")
            if isinstance(parsed_openai, Mapping)
            else openai_raw
        )
        if (
            not isinstance(openai_key, str)
            or not openai_key
            or openai_key == "REPLACE_ME"
        ):
            raise ProductionConfigurationError("OpenAI secret is still a placeholder")
        vault = KmsEnvelopeTokenVault(
            kms_client=kms,
            key_id=_required("OAUTH_KMS_KEY_ID"),
            record_store=repository,
            aead=CryptographyAesGcm(),
        )
        return ProductionGmailService(
            token_vault=vault,
            token_client=GoogleOAuthTokenClient(client_secret=google["client_secret"]),
            client_id=google["client_id"],
            repository=repository,
            openai_api_key=openai_key,
            openai_model=os.environ.get("OPENAI_RANKER_MODEL", "gpt-5-mini"),
            founder_user_id=founder_user_id,
            founder_account_email=founder_account_email,
        )

    def founder_approval_producer() -> FounderApprovalProducer:
        if founder_binding is None:
            raise ProductionConfigurationError("founder effects are disabled")
        assert founder_user_id is not None
        assert founder_connection_id is not None
        assert founder_account_email is not None
        signing_secret = _secret_string(
            secrets,
            _required("APPROVAL_SIGNING_SECRET_ID"),
        ).encode("utf-8")
        if len(signing_secret) < 32 or signing_secret == b"REPLACE_ME":
            raise ProductionConfigurationError("approval signing secret is invalid")
        action_repository = DynamoActionRepository(table)
        state_machine = ActionStateMachine(action_repository)
        return FounderApprovalProducer(
            action_repository=action_repository,
            approval_service=ApprovalService(
                state_machine=state_machine,
                token_codec=ApprovalTokenCodec(signing_secret),
                founder_user_ids={founder_user_id},
            ),
            founder_user_id=founder_user_id,
            connection_id=founder_connection_id,
            account_email=founder_account_email,
        )

    return ControlApplication(
        tickets=SignedConnectTickets(
            secret=web_secret,
            store=web_store,
        ),
        gmail=LazyGmailService(provider_service),
        tasks=DynamoTaskReader(table),
        deletion_intents=web_store,
        web_origin=_required("WEB_ORIGIN"),
        approval_producer=(
            LazyFounderApprovalProducer(
                founder_user_id=founder_user_id,
                factory=founder_approval_producer,
            )
            if founder_user_id is not None
            else None
        ),
        card_actions=DynamoTelegramCardActions(
            table,
            connection_fence=repository,
        ),
        draft_preparer=ReadOnlyGmailDraftPreparer(repository),
        scan_measurements=DynamoScanMeasurements(
            table,
            identity_key=web_secret,
        ),
    )
