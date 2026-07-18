"""Privacy-minimal read model for the invite-only consumer pilot."""

from __future__ import annotations

import re
from typing import Mapping

from workflows.gmail.repository import DynamoGmailRepository, READONLY_PROVIDER


_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9_-]{8,128}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CONNECTION_STATES = frozenset(
    {"CONNECTED", "REAUTH_REQUIRED", "DISCONNECTED"}
)
_RUNTIME_STATES = frozenset(
    {
        "COLD",
        "STARTING",
        "READY",
        "BUSY",
        "IDLE",
        "UNHEALTHY",
        "QUARANTINED",
        "DELETING",
    }
)
_SCAN_FIELDS = {
    "scanId",
    "status",
    "startedAt",
    "completedAt",
    "resultCount",
    "failureCode",
    "feedback",
}
_SCAN_STATES = frozenset({"RUNNING", "SUCCEEDED", "EMPTY", "FAILED"})
_FAILURE_CODES = frozenset(
    {"AUTHORIZATION", "PROVIDER_UNAVAILABLE", "RANKING", "INTERNAL"}
)
_DISCONNECT_PREFIXES = ("GMAIL#DRAFT#", "TELEGRAM_CALLBACK#")
_DISCONNECT_PAGE_SIZE = 25
_DISCONNECT_MAX_PAGES = 40


class ConnectionDisconnectPending(RuntimeError):
    """A bounded disconnect purge made progress and requires another pass."""


def _user_id(value: object) -> str:
    if not isinstance(value, str) or _USER_ID.fullmatch(value) is None:
        raise ValueError("user identity is invalid")
    return value


def _positive_int(value: object, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("overview projection is invalid")
    if (allow_zero and value < 0) or (not allow_zero and value <= 0):
        raise RuntimeError("overview projection is invalid")
    return value


def _scan_projection(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != _SCAN_FIELDS:
        raise RuntimeError("last scan projection is invalid")
    scan_id = value.get("scanId")
    status = value.get("status")
    started_at = value.get("startedAt")
    completed_at = value.get("completedAt")
    result_count = value.get("resultCount")
    failure_code = value.get("failureCode")
    feedback = value.get("feedback")
    if (
        not isinstance(scan_id, str)
        or _OPAQUE_ID.fullmatch(scan_id) is None
        or status not in _SCAN_STATES
    ):
        raise RuntimeError("last scan projection is invalid")
    _positive_int(started_at)
    if feedback not in {None, "USEFUL", "NOT_USEFUL"}:
        raise RuntimeError("last scan projection is invalid")
    if status == "RUNNING":
        valid = completed_at is None and result_count is None and failure_code is None
    else:
        try:
            _positive_int(completed_at)
        except RuntimeError:
            valid = False
        else:
            valid = completed_at >= started_at
        if status == "SUCCEEDED":
            valid = (
                valid
                and isinstance(result_count, int)
                and not isinstance(result_count, bool)
                and 1 <= result_count <= 3
                and failure_code is None
            )
        elif status == "EMPTY":
            valid = valid and result_count == 0 and failure_code is None
        elif status == "FAILED":
            valid = (
                valid
                and result_count is None
                and failure_code in _FAILURE_CODES
                and feedback is None
            )
    if not valid:
        raise RuntimeError("last scan projection is invalid")
    return {field: value[field] for field in _SCAN_FIELDS}


class DynamoConnectionLifecycle:
    """Inspect or remove the local envelope without KMS or provider access."""

    def __init__(self, table, *, repository=None) -> None:
        if not callable(getattr(table, "delete_item", None)):
            raise TypeError("connection table is invalid")
        self._table = table
        self._repository = repository or DynamoGmailRepository(table)
        if not callable(getattr(self._repository, "get", None)):
            raise TypeError("connection repository is invalid")

    def status(self, user_id: str) -> str:
        user_id = _user_id(user_id)
        connection_status = getattr(self._repository, "connection_status", None)
        if callable(connection_status):
            return connection_status(user_id)
        envelope = self._repository.get(
            user_id=user_id,
            provider=READONLY_PROVIDER,
        )
        return "DISCONNECTED" if envelope is None else "CONNECTED"

    def _delete_exact(self, key: Mapping[str, str]) -> None:
        try:
            self._table.delete_item(Key=dict(key))
        except Exception as error:
            try:
                response = self._table.get_item(Key=dict(key), ConsistentRead=True)
            except Exception as read_error:
                raise RuntimeError(
                    "connection purge outcome is uncertain"
                ) from read_error
            item = response.get("Item") if isinstance(response, Mapping) else None
            if not isinstance(response, Mapping) or item is not None:
                raise RuntimeError("connection purge outcome is uncertain") from error

    def _purge_prefix(self, user_id: str, prefix: str) -> None:
        start_key = None
        for page_number in range(_DISCONNECT_MAX_PAGES):
            request = {
                "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
                "ExpressionAttributeValues": {
                    ":pk": f"USER#{user_id}",
                    ":prefix": prefix,
                },
                "ConsistentRead": True,
                "ScanIndexForward": True,
                "Limit": _DISCONNECT_PAGE_SIZE,
            }
            if start_key is not None:
                request["ExclusiveStartKey"] = start_key
            response = self._table.query(**request)
            items = response.get("Items") if isinstance(response, Mapping) else None
            if not isinstance(items, list) or len(items) > _DISCONNECT_PAGE_SIZE:
                raise RuntimeError("connection purge returned an invalid page")
            for item in items:
                if (
                    not isinstance(item, Mapping)
                    or item.get("PK") != f"USER#{user_id}"
                    or not isinstance(item.get("SK"), str)
                    or not item["SK"].startswith(prefix)
                ):
                    raise RuntimeError("connection purge crossed its namespace")
                self._delete_exact({"PK": item["PK"], "SK": item["SK"]})
            start_key = response.get("LastEvaluatedKey")
            if start_key is None:
                return
            if (
                not isinstance(start_key, Mapping)
                or set(start_key) != {"PK", "SK"}
                or start_key.get("PK") != f"USER#{user_id}"
                or not isinstance(start_key.get("SK"), str)
                or not start_key["SK"].startswith(prefix)
            ):
                raise RuntimeError("connection purge returned an invalid cursor")
            if page_number == _DISCONNECT_MAX_PAGES - 1:
                raise ConnectionDisconnectPending(
                    "connection purge requires another bounded pass"
                )

    def disconnect(self, user_id: str) -> str:
        user_id = _user_id(user_id)
        begin_disconnect = getattr(self._repository, "begin_disconnect", None)
        finish_disconnect = getattr(self._repository, "finish_disconnect", None)
        if callable(begin_disconnect) and callable(finish_disconnect):
            generation = begin_disconnect(user_id)
            self._delete_exact(
                {
                    "PK": f"USER#{user_id}",
                    "SK": f"CONNECTION#{READONLY_PROVIDER}",
                }
            )
            self._delete_exact(
                {"PK": f"USER#{user_id}", "SK": "GMAIL#OPPORTUNITIES"}
            )
            for prefix in _DISCONNECT_PREFIXES:
                self._purge_prefix(user_id, prefix)
            finish_disconnect(user_id, generation)
            if self.status(user_id) != "DISCONNECTED":
                raise RuntimeError("connection disconnect outcome is uncertain")
            return "DISCONNECTED"
        key = {
            "PK": f"USER#{user_id}",
            "SK": f"CONNECTION#{READONLY_PROVIDER}",
        }
        try:
            self._table.delete_item(Key=key)
        except Exception as error:
            try:
                reconciled = self.status(user_id)
            except Exception:
                reconciled = None
            if reconciled != "DISCONNECTED":
                raise RuntimeError("connection disconnect outcome is uncertain") from error
            return "DISCONNECTED"
        if self.status(user_id) != "DISCONNECTED":
            raise RuntimeError("connection disconnect outcome is uncertain")
        return "DISCONNECTED"


class PilotOverviewService:
    """Aggregate counts and typed state without exposing private source content."""

    def __init__(self, *, connections, workspace, gmail_workspace, scans) -> None:
        required = (
            (connections, "status"),
            (workspace, "get"),
            (gmail_workspace, "get"),
            (scans, "latest"),
        )
        if any(not callable(getattr(port, method, None)) for port, method in required):
            raise TypeError("overview port is invalid")
        self._connections = connections
        self._workspace = workspace
        self._gmail = gmail_workspace
        self._scans = scans

    def get(self, user_id: str) -> dict[str, object]:
        user_id = _user_id(user_id)
        connection = self._connections.status(user_id)
        if connection not in _CONNECTION_STATES:
            raise RuntimeError("connection status is invalid")
        workspace = self._workspace.get(user_id)
        if (
            not isinstance(workspace, Mapping)
            or set(workspace)
            != {"userId", "runtimeState", "workspaceReceipt", "files"}
            or workspace.get("userId") != user_id
            or workspace.get("runtimeState") not in _RUNTIME_STATES
            or not isinstance(workspace.get("files"), list)
            or len(workspace["files"]) > 8
            or any(not isinstance(item, Mapping) for item in workspace["files"])
        ):
            raise RuntimeError("workspace overview projection is invalid")
        receipt = workspace.get("workspaceReceipt")
        if receipt is not None and (
            not isinstance(receipt, Mapping)
            or set(receipt) != {"generation", "manifestSha256"}
            or not isinstance(receipt.get("generation"), str)
            or _OPAQUE_ID.fullmatch(receipt["generation"]) is None
            or not isinstance(receipt.get("manifestSha256"), str)
            or _SHA256.fullmatch(receipt["manifestSha256"]) is None
        ):
            raise RuntimeError("workspace overview projection is invalid")
        gmail = self._gmail.get(user_id)
        if (
            not isinstance(gmail, Mapping)
            or set(gmail) != {"userId", "opportunities", "drafts"}
            or gmail.get("userId") != user_id
            or not isinstance(gmail.get("opportunities"), list)
            or not isinstance(gmail.get("drafts"), list)
            or len(gmail["opportunities"]) > 3
            or len(gmail["drafts"]) > 100
        ):
            raise RuntimeError("Gmail overview projection is invalid")
        scan = _scan_projection(self._scans.latest(user_id))
        if (
            connection == "CONNECTED"
            and scan is not None
            and scan["status"] == "FAILED"
            and scan["failureCode"] == "AUTHORIZATION"
        ):
            connection = "REAUTH_REQUIRED"
        return {
            "version": "personal-operator.pilot-overview.v1",
            "externalEffects": False,
            "connection": {
                "provider": READONLY_PROVIDER,
                "status": connection,
                "access": "READ_ONLY",
            },
            "lastScan": scan,
            "workspace": {
                "runtimeState": workspace["runtimeState"],
                "workspaceReceipt": dict(receipt) if receipt is not None else None,
                "fileCount": len(workspace["files"]),
                "opportunityCount": len(gmail["opportunities"]),
                "draftCount": len(gmail["drafts"]),
            },
            "capability": {
                "provider": READONLY_PROVIDER,
                "mode": "READ_ONLY",
                "externalEffects": False,
            },
            "export": {
                "format": "ZIP",
                "encrypted": False,
                "deterministic": True,
                "includes": ["memory", "receipts", "schedules", "workspace"],
            },
            "deletion": {
                "status": "AVAILABLE",
                "minimumReconciliationMinutes": 30,
            },
        }
