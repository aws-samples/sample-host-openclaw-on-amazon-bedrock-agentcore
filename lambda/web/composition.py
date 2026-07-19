"""Production composition for the trusted consumer web control plane.

Provider and approval secrets are deliberately lazy.  A user can inspect or
delete their workspace while Google remains an invalid staging placeholder;
the relevant OAuth or send path alone fails closed when it resolves that
placeholder.  No provider credential is ever supplied to RuntimeDriver.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from typing import Callable, Iterable, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen as _stdlib_urlopen

from actions.gmail_send import GmailApiAdapter, GmailSendExecutor
from actions.connectors import GenericConnectorKernel, GmailConnectorAdapter
from actions.maintenance import (
    ActionLifecycleMaintainer,
    ActionMaintenanceRunner,
    DynamoActionCursorStore,
    DynamoActionPageSource,
)
from actions.models import CapabilityDenied
from actions.reconcile import GmailEffectReconciler
from actions.repository import DynamoActionRepository
from actions.state_machine import ActionStateMachine, ApprovalService, ApprovalTokenCodec
from capabilities.retention import DynamoCapabilityDeletionAdapter
from capabilities.schedule_port import DynamoScheduleDefinitionReader
from router.runtime_driver import (
    AgentCoreAdapter,
    NoWorkspaceCapabilitySigner,
    RuntimeDriver,
)
from router.runtime_state import RuntimeStateRepository
from workflows.gmail.oauth import (
    GMAIL_READONLY_SCOPE,
    GOOGLE_AUTHORIZATION_ENDPOINT,
    GOOGLE_TOKEN_ENDPOINT,
    CryptographyAesGcm,
    GoogleOAuthTokenClient,
    GoogleReadonlyOAuthFlow,
    KmsEnvelopeTokenVault,
)
from workflows.gmail.repository import DynamoGmailRepository

from portable.importer import PortableImporter
from portable.exporter import PortableExporter
from portable.live import PortableLiveProjection
from portable.manifest import RECORD_CATEGORIES
from portable.staging import DynamoStagedImportStore, S3PortableBlobStore

from .adapters import (
    MAX_WORKSPACE_ENTRY_BYTES,
    MAX_WORKSPACE_FILES,
    MAX_WORKSPACE_LIST_PAGES,
    MAX_WORKSPACE_TOTAL_BYTES,
    DataAdapterError,
    DynamoUserFootprintStore,
    DynamoUserDataStore,
    S3WorkspaceStore,
)
from .auth import OpaqueSessionManager, SignedConnectTickets
from .gmail_workspace import GmailWorkspaceService
from .index import WebApplication
from .measurements import DynamoScanMeasurements
from .overview import DynamoConnectionLifecycle, PilotOverviewService
from .retention import DeletionCoordinator
from .schedule_control import LambdaScheduleControlClient
from .services import ApprovalWebService, RetentionSweepService, WorkspaceService
from .stores import DynamoOAuthStateStore, DynamoWebStore


REQUIRED_REGION = "eu-west-1"
SCHEDULER_CONTROL_TABLE_NAME = "personal-operator-scheduler-control"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
FOUNDER_GMAIL_SCOPES = frozenset({GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE})
MAX_TOKEN_RESPONSE_BYTES = 64 * 1024
_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_CONNECTION_REF = re.compile(r"[A-Za-z0-9_-]{8,128}")


class ProductionConfigurationError(RuntimeError):
    """A deployment binding or secret is absent, malformed, or placeholder."""


class AccountDeletionBlocked(CapabilityDenied):
    """A durable deletion intent has removed provider authority."""


class DeletionFenceUnavailable(ProductionConfigurationError):
    """The exact strongly consistent deletion state could not be proven."""


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ProductionConfigurationError(f"web configuration missing: {name}")
    return value


def _exact_region() -> str:
    configured = {
        value
        for value in (
            os.environ.get("AWS_REGION"),
            os.environ.get("AWS_DEFAULT_REGION"),
            os.environ.get("AWS_REGION_LOCK"),
        )
        if value
    }
    if configured != {REQUIRED_REGION}:
        raise ProductionConfigurationError(
            "web Lambda requires exact eu-west-1 region"
        )
    return REQUIRED_REGION


def _secret_string(client, secret_id: str) -> str:
    try:
        response = client.get_secret_value(SecretId=secret_id)
        value = response.get("SecretString")
    except Exception:
        raise ProductionConfigurationError("web secret is unavailable") from None
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 64 * 1024
        or "\x00" in value
    ):
        raise ProductionConfigurationError("web secret is invalid")
    return value


def _signing_secret(client, secret_id: str, *, purpose: str) -> bytes:
    value = _secret_string(client, secret_id)
    if value == "REPLACE_ME" or len(value.encode("utf-8")) < 32:
        raise ProductionConfigurationError(f"{purpose} secret is invalid")
    return value.encode("utf-8")


def _json_secret(
    client,
    secret_id: str,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, str]:
    try:
        parsed = json.loads(_secret_string(client, secret_id))
    except json.JSONDecodeError:
        raise ProductionConfigurationError("provider secret JSON is invalid") from None
    optional = optional or set()
    if (
        not isinstance(parsed, dict)
        or not required.issubset(parsed)
        or not set(parsed).issubset(required | optional)
    ):
        raise ProductionConfigurationError("provider secret fields are invalid")
    values = {name: parsed[name] for name in required}
    if any(
        not isinstance(value, str)
        or not value
        or value == "REPLACE_ME"
        or "\x00" in value
        for value in values.values()
    ):
        raise ProductionConfigurationError("provider secret is still a placeholder")
    return values


def _founder_ids(raw: str) -> frozenset[str]:
    if not isinstance(raw, str) or len(raw) > 4_096:
        raise ProductionConfigurationError("founder user allowlist is invalid")
    values = [value.strip() for value in raw.split(",") if value.strip()]
    if len(values) > 1:
        raise ProductionConfigurationError(
            "exactly one founder identity is supported"
        )
    if len(set(values)) != len(values) or any(
        _USER_ID.fullmatch(value) is None for value in values
    ):
        raise ProductionConfigurationError("founder user allowlist is invalid")
    return frozenset(values)


def _secret_not_found(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    return bool(
        isinstance(response, Mapping)
        and isinstance(response.get("Error"), Mapping)
        and response["Error"].get("Code") == "ResourceNotFoundException"
    )


class SecretsManagerFounderConnectionRevoker:
    """Idempotently disable the one deployment-managed founder send credential."""

    RECOVERY_WINDOW_DAYS = 7

    def __init__(
        self,
        *,
        secret_client,
        secret_id: str,
        founder_user_id: str | None,
    ) -> None:
        if founder_user_id is not None and (
            not isinstance(founder_user_id, str)
            or _USER_ID.fullmatch(founder_user_id) is None
        ):
            raise ValueError("founder identity is invalid")
        if founder_user_id is None:
            self._secrets = secret_client
            self._secret_id = secret_id
            self._founder_user_id = None
            return
        describes = callable(getattr(secret_client, "describe_secret", None))
        deletes = callable(getattr(secret_client, "delete_secret", None))
        if not describes or not deletes:
            raise ValueError("founder send secret client is invalid")
        if (
            not isinstance(secret_id, str)
            or not secret_id
            or len(secret_id) > 512
            or "\x00" in secret_id
        ):
            raise ValueError("founder send secret identity is invalid")
        self._secrets = secret_client
        self._secret_id = secret_id
        self._founder_user_id = founder_user_id

    def _response(self, value: object, *, date_field: str) -> bool:
        if not isinstance(value, Mapping):
            raise ProductionConfigurationError(
                "founder send secret revocation response is invalid"
            )
        name = value.get("Name")
        arn = value.get("ARN")
        if (
            name != self._secret_id
            or not isinstance(arn, str)
            or f":secret:{self._secret_id}-" not in arn
        ):
            raise ProductionConfigurationError(
                "founder send secret revocation binding is invalid"
            )
        deletion_date = value.get(date_field)
        if deletion_date is None:
            return False
        if not isinstance(deletion_date, datetime) or deletion_date.tzinfo is None:
            raise ProductionConfigurationError(
                "founder send secret deletion date is invalid"
            )
        return True

    def _state(self) -> str:
        try:
            response = self._secrets.describe_secret(SecretId=self._secret_id)
        except Exception as error:
            if _secret_not_found(error):
                return "absent"
            raise ProductionConfigurationError(
                "founder send secret revocation state is unavailable"
            ) from None
        return "scheduled" if self._response(response, date_field="DeletedDate") else "live"

    def _reconcile_delete(self) -> None:
        if self._state() not in {"scheduled", "absent"}:
            raise ProductionConfigurationError(
                "founder send secret revocation outcome is unproven"
            ) from None

    def revoke_all(self, user_id: str) -> None:
        if not isinstance(user_id, str) or _USER_ID.fullmatch(user_id) is None:
            raise ValueError("user identity is invalid")
        if user_id != self._founder_user_id:
            return
        if self._state() in {"scheduled", "absent"}:
            return
        try:
            response = self._secrets.delete_secret(
                SecretId=self._secret_id,
                RecoveryWindowInDays=self.RECOVERY_WINDOW_DAYS,
            )
        except Exception:
            self._reconcile_delete()
            return
        try:
            accepted = self._response(response, date_field="DeletionDate")
        except ProductionConfigurationError:
            self._reconcile_delete()
            return
        if not accepted:
            self._reconcile_delete()


class CompositeConnectionRevoker:
    """Run exact authority revokers in fail-closed order."""

    def __init__(self, *revokers) -> None:
        if not revokers or any(
            not callable(getattr(revoker, "revoke_all", None)) for revoker in revokers
        ):
            raise ValueError("connection revoker is invalid")
        self._revokers = tuple(revokers)

    def revoke_all(self, user_id: str) -> None:
        if not isinstance(user_id, str) or _USER_ID.fullmatch(user_id) is None:
            raise ValueError("user identity is invalid")
        for revoker in self._revokers:
            revoker.revoke_all(user_id)


class BoundConnectionAuthorityRevoker:
    """Bind one connector reference to its exact user-scoped authority."""

    def __init__(
        self,
        *,
        authority_revoker,
        connection_ref: str,
        user_id: str,
    ) -> None:
        if not callable(getattr(authority_revoker, "revoke_all", None)):
            raise ValueError("connection authority revoker is invalid")
        if (
            not isinstance(connection_ref, str)
            or _CONNECTION_REF.fullmatch(connection_ref) is None
        ):
            raise ValueError("connection revocation binding is invalid")
        if not isinstance(user_id, str) or _USER_ID.fullmatch(user_id) is None:
            raise ValueError("connection user binding is invalid")
        self._authority_revoker = authority_revoker
        self._connection_ref = connection_ref
        self._user_id = user_id

    def revoke_all(self, connection_ref: str) -> None:
        if (
            not isinstance(connection_ref, str)
            or _CONNECTION_REF.fullmatch(connection_ref) is None
            or connection_ref != self._connection_ref
        ):
            raise CapabilityDenied("connection revocation binding mismatch")
        self._authority_revoker.revoke_all(self._user_id)


class FounderKernelDeletionRevoker:
    """Map account deletion to one exactly bound connector-kernel revoke."""

    def __init__(
        self,
        *,
        kernel,
        founder_user_id: str | None,
        connection_ref: str | None,
    ) -> None:
        if (founder_user_id is None) != (connection_ref is None):
            raise ValueError("founder deletion connection binding is incomplete")
        if founder_user_id is not None:
            if not callable(getattr(kernel, "revoke", None)):
                raise ValueError("founder deletion kernel is invalid")
            if _USER_ID.fullmatch(founder_user_id) is None:
                raise ValueError("founder deletion user binding is invalid")
            if _CONNECTION_REF.fullmatch(connection_ref or "") is None:
                raise ValueError("founder deletion connection binding is invalid")
        self._kernel = kernel
        self._founder_user_id = founder_user_id
        self._connection_ref = connection_ref

    def revoke_all(self, user_id: str) -> None:
        if not isinstance(user_id, str) or _USER_ID.fullmatch(user_id) is None:
            raise ValueError("user identity is invalid")
        if self._founder_user_id is None or user_id != self._founder_user_id:
            return
        assert self._connection_ref is not None
        self._kernel.revoke(self._connection_ref)


class _UnavailableConnectionAuthorityRevoker:
    """Fail closed on the control-only adapter, which has no bound connection."""

    @staticmethod
    def revoke_all(_connection_ref: str) -> None:
        raise ProductionConfigurationError(
            "control-only Gmail adapter has no connection revocation binding"
        )


class _UnavailableGmailExecutor:
    @staticmethod
    def execute(_action):
        raise RuntimeError("control-only Gmail adapter cannot dispatch")


class CompositeUserRecordDeleter:
    """Remove raw-user and pseudonymous user partitions in fixed order."""

    def __init__(self, *deleters) -> None:
        if not deleters or any(
            not callable(getattr(deleter, "delete_user_records", None))
            for deleter in deleters
        ):
            raise ValueError("user record deleter is invalid")
        self._deleters = tuple(deleters)

    def delete_user_records(self, user_id: str) -> None:
        if not isinstance(user_id, str) or _USER_ID.fullmatch(user_id) is None:
            raise ValueError("user identity is invalid")
        for deleter in self._deleters:
            deleter.delete_user_records(user_id)


class _LazyPort:
    """Publish a small lazy interface without caching failed configuration."""

    def __init__(self, factory: Callable[[], object]) -> None:
        self._factory = factory
        self._value = None
        self._lock = threading.Lock()

    def _get(self):
        if self._value is None:
            with self._lock:
                if self._value is None:
                    candidate = self._factory()
                    if candidate is None:
                        raise ProductionConfigurationError(
                            "lazy trusted service could not be constructed"
                        )
                    self._value = candidate
        return self._value


class LazyOAuthPort(_LazyPort):
    def start(self, **kwargs):
        return self._get().start(**kwargs)

    def complete(self, **kwargs):
        return self._get().complete(**kwargs)


class LazyApprovalPort(_LazyPort):
    def preview(self, **kwargs):
        return self._get().preview(**kwargs)

    def approve(self, **kwargs):
        return self._get().approve(**kwargs)

    def reject(self, **kwargs):
        return self._get().reject(**kwargs)


class _ExportSource:
    """Strong-read native plus active portable state for web/export surfaces."""

    def __init__(self, *, records, workspace, portable=None, schedules=None) -> None:
        if schedules is not None and not callable(
            getattr(schedules, "definitions_for_user", None)
        ):
            raise TypeError("native schedule export projection is invalid")
        self._records = records
        self._workspace = workspace
        self._schedules = schedules
        self._portable = (
            None if portable is None else PortableLiveProjection(portable)
        )

    def _live(self, user_id: str) -> tuple[dict[str, list], dict[str, bytes]]:
        if self._portable is None:
            return ({category: [] for category in RECORD_CATEGORIES}, {})
        snapshot = self._portable.snapshot_for_user(user_id)
        return snapshot.records, snapshot.workspace

    def records_for_user(self, user_id: str):
        native = self._native_records(user_id)
        imported, _workspace = self._live(user_id)
        return self._merge_records(native, imported)

    def _native_records(self, user_id: str):
        native = self._records.records_for_user(user_id)
        if self._schedules is None:
            return native
        if not isinstance(native, Mapping) or set(native) != RECORD_CATEGORIES:
            raise DataAdapterError("native export records are invalid")
        definitions = self._schedules.definitions_for_user(user_id)
        if not isinstance(definitions, list) or any(
            not isinstance(item, Mapping) for item in definitions
        ):
            raise DataAdapterError("native schedule export is invalid")
        rows = native.get("schedules")
        if not isinstance(rows, list):
            raise DataAdapterError("native export records are invalid")
        return {
            **native,
            "schedules": [*rows, *(dict(item) for item in definitions)],
        }

    @staticmethod
    def _merge_records(native, imported):
        if not isinstance(native, Mapping) or set(native) != RECORD_CATEGORIES:
            raise DataAdapterError("native export records are invalid")
        result = {}
        for category in RECORD_CATEGORIES:
            rows = native.get(category)
            if not isinstance(rows, list):
                raise DataAdapterError("native export records are invalid")
            result[category] = [*imported[category], *rows]
        return result

    def workspace_files(self, user_id: str):
        native = self._workspace.workspace_files(user_id)
        _records, imported = self._live(user_id)
        return self._merge_workspace(native, imported)

    @staticmethod
    def _merge_workspace(native, imported):
        if not isinstance(native, Mapping):
            raise DataAdapterError("native workspace is invalid")
        return {**imported, **native}

    def snapshot_for_user(self, user_id: str):
        """Capture one portable generation for both halves of one export."""

        native_records = self._native_records(user_id)
        native_workspace = self._workspace.workspace_files(user_id)
        imported_records, imported_workspace = self._live(user_id)
        return (
            self._merge_records(native_records, imported_records),
            self._merge_workspace(native_workspace, imported_workspace),
        )


class S3UserFilesStore:
    """Read only the runtime plugin's `<user>/files/` authored namespace.

    Workspace lifecycle snapshots live beside it below `<user>/.system/` and
    are trust-bearing implementation objects, not user export entries.
    Account deletion still uses S3WorkspaceStore over the entire user prefix.
    """

    def __init__(self, client, *, bucket_name: str) -> None:
        if not isinstance(bucket_name, str) or not bucket_name or len(bucket_name) > 255:
            raise ValueError("workspace bucket name is invalid")
        self._client = client
        self._bucket = bucket_name

    @staticmethod
    def _prefix(user_id: str) -> str:
        if not isinstance(user_id, str) or _USER_ID.fullmatch(user_id) is None:
            raise ValueError("user identity is invalid")
        return f"{user_id}/files/"

    def workspace_files(self, user_id: str) -> dict[str, bytes]:
        prefix = self._prefix(user_id)
        token = None
        seen_tokens: set[str] = set()
        result: dict[str, bytes] = {}
        total = 0
        for _page in range(MAX_WORKSPACE_LIST_PAGES):
            request = {"Bucket": self._bucket, "Prefix": prefix, "MaxKeys": 1_000}
            if token:
                request["ContinuationToken"] = token
            response = self._client.list_objects_v2(**request)
            if not isinstance(response, Mapping):
                raise DataAdapterError("workspace listing failed")
            if (
                ("Name" in response and response.get("Name") != self._bucket)
                or ("Prefix" in response and response.get("Prefix") != prefix)
                or ("MaxKeys" in response and response.get("MaxKeys") != 1_000)
            ):
                raise DataAdapterError("workspace listing is ambiguous")
            contents = response.get("Contents", [])
            if not isinstance(contents, list):
                raise DataAdapterError("workspace listing failed")
            for item in contents:
                key = item.get("Key") if isinstance(item, Mapping) else None
                size = item.get("Size") if isinstance(item, Mapping) else None
                relative = (
                    key[len(prefix) :]
                    if isinstance(key, str) and key.startswith(prefix)
                    else None
                )
                if (
                    not isinstance(relative, str)
                    or not relative
                    or len(relative) > 512
                    or "\\" in relative
                    or any(
                        segment in {"", ".", ".."} or segment.startswith(".")
                        for segment in relative.split("/")
                    )
                    or isinstance(size, bool)
                    or not isinstance(size, int)
                    or not 0 <= size <= MAX_WORKSPACE_ENTRY_BYTES
                    or relative in result
                    or len(result) >= MAX_WORKSPACE_FILES
                ):
                    raise DataAdapterError("workspace object violates export limits")
                response_body = self._client.get_object(
                    Bucket=self._bucket,
                    Key=key,
                )
                body = (
                    response_body.get("Body")
                    if isinstance(response_body, Mapping)
                    else None
                )
                content = (
                    body.read(MAX_WORKSPACE_ENTRY_BYTES + 1)
                    if hasattr(body, "read")
                    else None
                )
                if not isinstance(content, bytes) or len(content) != size:
                    raise DataAdapterError("workspace object changed during export")
                total += size
                if total > MAX_WORKSPACE_TOTAL_BYTES:
                    raise DataAdapterError("workspace export exceeds total limit")
                result[relative] = content
            truncated = response.get("IsTruncated")
            if not isinstance(truncated, bool):
                raise DataAdapterError("workspace pagination is invalid")
            if not truncated and response.get("NextContinuationToken") is not None:
                raise DataAdapterError("workspace pagination is invalid")
            if not truncated:
                return result
            token = response.get("NextContinuationToken")
            if (
                not isinstance(token, str)
                or not token
                or token in seen_tokens
            ):
                raise DataAdapterError("workspace pagination is invalid")
            seen_tokens.add(token)
        raise DataAdapterError("workspace listing exceeded its page bound")


class _GoogleSendTokenClient:
    """One-attempt exact-endpoint OAuth refresh for the separate send grant."""

    def __init__(self, *, urlopen=None) -> None:
        self._urlopen = urlopen or _stdlib_urlopen

    @staticmethod
    def _text(value: object, label: str, limit: int) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > limit
            or "\x00" in value
        ):
            raise ProductionConfigurationError(f"Gmail send {label} is invalid")
        return value

    def refresh(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> str:
        form = urlencode(
            {
                "client_id": self._text(client_id, "client_id", 512),
                "client_secret": self._text(client_secret, "client_secret", 4_096),
                "grant_type": "refresh_token",
                "refresh_token": self._text(
                    refresh_token, "refresh_token", 16_384
                ),
            }
        ).encode("ascii")
        try:
            request = Request(
                GOOGLE_TOKEN_ENDPOINT,
                data=form,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                method="POST",
            )
            with self._urlopen(request, timeout=5) as response:
                if getattr(response, "status", None) != 200:
                    raise ValueError("token endpoint was not successful")
                raw = response.read(MAX_TOKEN_RESPONSE_BYTES + 1)
            if not isinstance(raw, bytes) or len(raw) > MAX_TOKEN_RESPONSE_BYTES:
                raise ValueError("token response exceeded its bound")
            payload = json.loads(raw.decode("utf-8", "strict"))
            if not isinstance(payload, Mapping) or not set(payload).issubset(
                {"access_token", "expires_in", "scope", "token_type"}
            ):
                raise ValueError("token response fields are invalid")
            if payload.get("token_type") != "Bearer":
                raise ValueError("token response type is invalid")
            # GmailApiAdapter proves the provider result by strongly reading
            # the exact SENT message after dispatch. The elevated founder
            # credential therefore has the normal read-only scope plus the one
            # incremental send scope, and no other Google authority.
            if set(str(payload.get("scope", "")).split()) != FOUNDER_GMAIL_SCOPES:
                raise ValueError("token response scope is invalid")
            expires = payload.get("expires_in")
            if (
                isinstance(expires, bool)
                or not isinstance(expires, int)
                or not 1 <= expires <= 86_400
            ):
                raise ValueError("token response expiry is invalid")
            return self._text(payload.get("access_token"), "access_token", 16_384)
        except Exception:
            # Provider and urllib exceptions can contain submitted credential
            # material, so they never cross this boundary as context.
            raise ProductionConfigurationError(
                "Gmail send OAuth refresh failed"
            ) from None


class ProductionGmailExecutorFactory:
    """Resolve the exact founder send connection without performing an effect."""

    _FIELDS = {
        "client_id",
        "client_secret",
        "refresh_token",
        "email",
        "connection_id",
        "user_id",
    }

    def __init__(
        self,
        *,
        secret_client,
        secret_id: str,
        state_machine,
        founder_user_ids: Iterable[str],
        deletion_blocked,
        connection_revoker,
        token_client=None,
    ) -> None:
        if not callable(deletion_blocked):
            raise ValueError("Gmail effect deletion fence is required")
        if not callable(getattr(connection_revoker, "revoke_all", None)):
            raise ValueError("Gmail connector revoker is required")
        self._secrets = secret_client
        self._secret_id = secret_id
        self._machine = state_machine
        self._founders = frozenset(founder_user_ids)
        self._deletion_blocked = deletion_blocked
        self._connection_revoker = connection_revoker
        self._tokens = token_client or _GoogleSendTokenClient()

    def _assert_deletion_allows(self, action: Mapping[str, object]) -> str:
        if not isinstance(action, Mapping):
            raise TypeError("pending action must be a mapping")
        user_id = action.get("userId")
        if user_id not in self._founders:
            raise CapabilityDenied("email effects are founder-only")
        try:
            blocked = self._deletion_blocked(user_id)
        except Exception:
            raise DeletionFenceUnavailable(
                "account deletion fence is unavailable"
            ) from None
        if not isinstance(blocked, bool):
            raise DeletionFenceUnavailable(
                "account deletion fence returned an invalid result"
            )
        if blocked:
            raise AccountDeletionBlocked(
                "account deletion blocks Gmail provider access"
            )
        return user_id

    def _provider(
        self, action: Mapping[str, object]
    ) -> tuple[GmailApiAdapter, str, str, str]:
        if not isinstance(action, Mapping):
            raise TypeError("pending action must be a mapping")
        user_id = action.get("userId")
        if user_id not in self._founders:
            raise CapabilityDenied("email effects are founder-only")
        account_email = action.get("accountEmail")
        connection_id = action.get("connectionId")
        if (
            not isinstance(account_email, str)
            or not isinstance(connection_id, str)
            or action.get("senderAddress") != account_email
        ):
            raise CapabilityDenied("pending action has no exact send connection")
        connection = _json_secret(
            self._secrets,
            self._secret_id,
            required=self._FIELDS,
            optional={"bootstrap_nonce"},
        )
        if (
            connection["user_id"] != user_id
            or connection["email"] != account_email
            or connection["connection_id"] != connection_id
        ):
            raise CapabilityDenied(
                "send OAuth secret does not match the exact founder identity and approved Google account"
            )
        access_token = self._tokens.refresh(
            client_id=connection["client_id"],
            client_secret=connection["client_secret"],
            refresh_token=connection["refresh_token"],
        )
        try:
            # Imported only on this founder-only approval path.  The short-lived
            # Credentials object has no refresh token, preventing implicit retry
            # or scope escalation during the provider effect.
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError:
            raise ProductionConfigurationError(
                "trusted Google provider dependencies are not packaged"
            ) from None
        gmail = build(
            "gmail",
            "v1",
            credentials=Credentials(token=access_token),
            cache_discovery=False,
            static_discovery=True,
        )
        provider = GmailApiAdapter(
            gmail,
            connection_id=connection_id,
            account_email=account_email,
            timeout_seconds=3,
        )
        return provider, connection_id, account_email, user_id

    def __call__(self, action: Mapping[str, object]):
        self._assert_deletion_allows(action)
        provider, connection_id, account_email, user_id = self._provider(action)
        executor = GmailSendExecutor(
            state_machine=self._machine,
            provider=provider,
            founder_user_ids=self._founders,
            connection_id=connection_id,
            account_email=account_email,
            sender_address=account_email,
            deletion_blocked=self._deletion_blocked,
        )
        return GenericConnectorKernel(
            GmailConnectorAdapter(
                executor=executor,
                provider=provider,
                connection_revoker=BoundConnectionAuthorityRevoker(
                    authority_revoker=self._connection_revoker,
                    connection_ref=connection_id,
                    user_id=user_id,
                ),
            )
        )


class _NoopGmailReconciler:
    """Observation-only safe sink for records outside current founder authority."""

    @staticmethod
    def reconcile(*, action_id: str, user_id: str):
        del action_id, user_id
        return None


class KernelGmailReconciler:
    """Adapt maintenance's exact IDs to one kernel-bound action observation."""

    def __init__(self, *, kernel, action: Mapping[str, object]) -> None:
        if not callable(getattr(kernel, "reconcile", None)):
            raise ValueError("Gmail reconciliation kernel is invalid")
        if not isinstance(action, Mapping):
            raise TypeError("reconciliation action must be a mapping")
        action_id = action.get("actionId")
        user_id = action.get("userId")
        if not isinstance(action_id, str) or not isinstance(user_id, str):
            raise CapabilityDenied("reconciliation action binding is invalid")
        self._kernel = kernel
        self._action = dict(action)
        self._action_id = action_id
        self._user_id = user_id

    def reconcile(self, *, action_id: str, user_id: str):
        if action_id != self._action_id or user_id != self._user_id:
            raise CapabilityDenied("reconciliation action binding mismatch")
        return self._kernel.reconcile(dict(self._action))


class ProductionGmailReconcilerFactory:
    """Build an exact-account history observer; this factory has no send method."""

    def __init__(
        self,
        *,
        provider_factory: ProductionGmailExecutorFactory,
        repository,
        state_machine,
        founder_user_ids: Iterable[str],
    ) -> None:
        self._providers = provider_factory
        self._repository = repository
        self._machine = state_machine
        self._founders = frozenset(founder_user_ids)

    def __call__(self, action: Mapping[str, object]):
        if not isinstance(action, Mapping):
            raise TypeError("reconciliation action must be a mapping")
        if action.get("userId") not in self._founders:
            return _NoopGmailReconciler()
        try:
            self._providers._assert_deletion_allows(action)
        except AccountDeletionBlocked:
            return _NoopGmailReconciler()
        provider, connection_id, account_email, user_id = self._providers._provider(
            action
        )
        reconciler = GmailEffectReconciler(
            state_machine=self._machine,
            repository=self._repository,
            provider=provider,
            connection_id=connection_id,
            account_email=account_email,
            sender_address=account_email,
            founder_user_ids=self._founders,
            deletion_blocked=self._providers._deletion_blocked,
        )
        kernel = GenericConnectorKernel(
            GmailConnectorAdapter(
                executor=_UnavailableGmailExecutor(),
                reconciler=reconciler,
                provider=provider,
                connection_revoker=BoundConnectionAuthorityRevoker(
                    authority_revoker=self._providers._connection_revoker,
                    connection_ref=connection_id,
                    user_id=user_id,
                ),
            )
        )
        return KernelGmailReconciler(kernel=kernel, action=action)


def build_production_application() -> WebApplication:
    region = _exact_region()
    import boto3
    from botocore.config import Config

    no_retries = Config(retries={"max_attempts": 0})
    dynamodb = boto3.resource("dynamodb", region_name=region, config=no_retries)
    dynamodb_client = boto3.client(
        "dynamodb", region_name=region, config=no_retries
    )
    lambda_client = boto3.client("lambda", region_name=region, config=no_retries)
    secrets = boto3.client(
        "secretsmanager", region_name=region, config=no_retries
    )
    kms = boto3.client("kms", region_name=region, config=no_retries)
    s3 = boto3.client("s3", region_name=region, config=no_retries)
    agentcore = boto3.client(
        "bedrock-agentcore",
        region_name=region,
        config=Config(
            connect_timeout=5,
            read_timeout=20,
            retries={"max_attempts": 0},
        ),
    )

    control_table = dynamodb.Table(_required("CONTROL_TABLE_NAME"))
    runtime_table = dynamodb.Table(_required("RUNTIME_STATE_TABLE_NAME"))
    capability_table = dynamodb.Table(_required("CAPABILITY_STATE_TABLE_NAME"))
    identity_table = dynamodb.Table(_required("IDENTITY_TABLE_NAME"))
    message_ledger_table = dynamodb.Table(_required("MESSAGE_LEDGER_TABLE_NAME"))
    web_store = DynamoWebStore(control_table)
    web_secret = _signing_secret(
        secrets,
        _required("WEB_AUTH_SECRET_ID"),
        purpose="web auth",
    )
    founders = _founder_ids(os.environ.get("FOUNDER_USER_IDS", ""))
    web_origin = _required("WEB_ORIGIN").rstrip("/")
    redirect_uri = _required("GOOGLE_REDIRECT_URI")
    if redirect_uri != f"{web_origin}/oauth/google/callback":
        raise ProductionConfigurationError(
            "Google redirect URI must be the exact same-origin callback"
        )

    runtime_repository = RuntimeStateRepository(
        runtime_table,
        runtime_arn=_required("AGENTCORE_RUNTIME_ARN"),
        runtime_qualifier=_required("AGENTCORE_QUALIFIER"),
    )
    runtime_driver = RuntimeDriver(
        repository=runtime_repository,
        adapter=AgentCoreAdapter(
            agentcore,
            runtime_arn=_required("AGENTCORE_RUNTIME_ARN"),
            qualifier=_required("AGENTCORE_QUALIFIER"),
            region=region,
        ),
        workspace_capability_signer=NoWorkspaceCapabilitySigner(),
        lease_ms=120_000,
        max_execution_ms=30_000,
    )
    deletion_workspace_store = S3WorkspaceStore(
        s3, bucket_name=_required("USER_FILES_BUCKET_NAME")
    )
    authored_files_store = S3UserFilesStore(
        s3, bucket_name=_required("USER_FILES_BUCKET_NAME")
    )
    record_store = DynamoUserDataStore(
        control_table,
        installation_table=capability_table,
    )
    portable_store = DynamoStagedImportStore(
        control_table,
        blobs=S3PortableBlobStore(
            s3,
            bucket_name=_required("USER_FILES_BUCKET_NAME"),
        ),
    )
    gmail_repository = DynamoGmailRepository(control_table)
    action_repository = DynamoActionRepository(control_table)
    action_machine = ActionStateMachine(action_repository)
    founder_user_id = next(iter(founders), None)
    founder_connection_ref = (
        _required("GMAIL_SEND_CONNECTION_ID")
        if founder_user_id is not None
        else None
    )
    send_secret_id = (
        _required("GOOGLE_SEND_OAUTH_SECRET_ID")
        if founders
        else os.environ.get("GOOGLE_SEND_OAUTH_SECRET_ID", "")
    )
    founder_connection_revoker = SecretsManagerFounderConnectionRevoker(
        secret_client=secrets,
        secret_id=send_secret_id,
        founder_user_id=founder_user_id,
    )
    gmail_provider_factory = ProductionGmailExecutorFactory(
        secret_client=secrets,
        secret_id=send_secret_id,
        state_machine=action_machine,
        founder_user_ids=founders,
        deletion_blocked=lambda user_id: (
            web_store.get_deletion_intent(user_id) is not None
        ),
        connection_revoker=founder_connection_revoker,
    )
    gmail_control_kernel = GenericConnectorKernel(
        GmailConnectorAdapter(
            executor=_UnavailableGmailExecutor(),
            repository=action_repository,
            draft_editor=gmail_repository,
            state_machine=action_machine,
            connection_revoker=(
                BoundConnectionAuthorityRevoker(
                    authority_revoker=founder_connection_revoker,
                    connection_ref=founder_connection_ref,
                    user_id=founder_user_id,
                )
                if founder_connection_ref is not None
                and founder_user_id is not None
                else _UnavailableConnectionAuthorityRevoker()
            ),
        )
    )
    founder_deletion_revoker = FounderKernelDeletionRevoker(
        kernel=gmail_control_kernel,
        founder_user_id=founder_user_id,
        connection_ref=founder_connection_ref,
    )

    def oauth_factory():
        google = _json_secret(
            secrets,
            _required("GOOGLE_READONLY_OAUTH_SECRET_ID"),
            required={"client_id", "client_secret"},
            optional={"bootstrap_nonce"},
        )
        vault = KmsEnvelopeTokenVault(
            kms_client=kms,
            key_id=_required("OAUTH_KMS_KEY_ID"),
            record_store=gmail_repository,
            aead=CryptographyAesGcm(),
        )
        return GoogleReadonlyOAuthFlow(
            state_store=DynamoOAuthStateStore(control_table),
            token_client=GoogleOAuthTokenClient(
                client_secret=google["client_secret"]
            ),
            token_vault=vault,
            connection_fence=gmail_repository,
            client_id=google["client_id"],
            authorization_endpoint=GOOGLE_AUTHORIZATION_ENDPOINT,
            allowed_redirect_uris={redirect_uri},
        )

    def approvals_factory():
        approvals = ApprovalService(
            state_machine=action_machine,
            token_codec=ApprovalTokenCodec(
                _signing_secret(
                    secrets,
                    _required("APPROVAL_SIGNING_SECRET_ID"),
                    purpose="approval signing",
                )
            ),
            founder_user_ids=founders,
        )
        return ApprovalWebService(
            approval_service=approvals,
            action_reader=action_repository,
            executor_factory=gmail_provider_factory,
            founder_user_ids=founders,
        )

    action_maintenance = ActionMaintenanceRunner(
        # One action per durable cursor step keeps a provider timeout from
        # losing unrelated progress. The hourly run advances up to 20 steps.
        page_source=DynamoActionPageSource(control_table, page_size=1),
        lifecycle=ActionLifecycleMaintainer(
            repository=action_repository,
            state_machine=action_machine,
            reconciler_factory=ProductionGmailReconcilerFactory(
                provider_factory=gmail_provider_factory,
                repository=action_repository,
                state_machine=action_machine,
                founder_user_ids=founders,
            ),
        ),
        cursor_store=DynamoActionCursorStore(control_table),
        max_pages=20,
    )

    scans = DynamoScanMeasurements(
        control_table,
        identity_key=web_secret,
    )
    schedule_control = LambdaScheduleControlClient(
        client=lambda_client,
        function_arn=_required("SCHEDULER_CONTROL_FUNCTION_ARN"),
    )
    capability_deletion = DynamoCapabilityDeletionAdapter(
        client=dynamodb_client,
        table_name=_required("CAPABILITY_STATE_TABLE_NAME"),
    )
    deletion = DeletionCoordinator(
        session_store=web_store,
        authority_fence=capability_deletion,
        connection_store=CompositeConnectionRevoker(
            founder_deletion_revoker,
            record_store,
        ),
        runtime_driver=runtime_driver,
        workspace_store=deletion_workspace_store,
        record_store=CompositeUserRecordDeleter(
            record_store,
            scans,
            capability_deletion,
        ),
        footprint_store=DynamoUserFootprintStore(
            control_table=control_table,
            identity_table=identity_table,
            message_ledger_table=message_ledger_table,
        ),
        schedule_store=schedule_control,
    )
    portable_source = _ExportSource(
        records=record_store,
        workspace=authored_files_store,
        portable=portable_store,
        schedules=DynamoScheduleDefinitionReader(
            client=dynamodb_client,
            table_name=SCHEDULER_CONTROL_TABLE_NAME,
        ),
    )
    workspace = WorkspaceService(
        workspace_store=portable_source,
        runtime_driver=runtime_driver,
    )
    gmail_workspace = GmailWorkspaceService(
        control_table,
        repository=gmail_repository,
        approval_superseder=gmail_control_kernel,
        enforce_connection_fence=True,
    )
    connections = DynamoConnectionLifecycle(
        control_table,
        repository=gmail_repository,
    )
    overview = PilotOverviewService(
        connections=connections,
        workspace=workspace,
        gmail_workspace=gmail_workspace,
        scans=scans,
    )
    return WebApplication(
        tickets=SignedConnectTickets(secret=web_secret, store=web_store),
        sessions=OpaqueSessionManager(secret=web_secret, store=web_store),
        oauth=LazyOAuthPort(oauth_factory),
        approvals=LazyApprovalPort(approvals_factory),
        workspace=workspace,
        gmail_workspace=gmail_workspace,
        exporter=PortableExporter(portable_source),
        importer=PortableImporter(
            staging=portable_store
        ),
        deletion=deletion,
        retention=RetentionSweepService(
            control_table=control_table,
            runtime_table=runtime_table,
            deletion=deletion,
            action_maintenance=action_maintenance,
        ),
        overview=overview,
        connections=connections,
        scans=scans,
        schedule_control=schedule_control,
        web_origin=web_origin,
        google_redirect_uri=redirect_uri,
    )
