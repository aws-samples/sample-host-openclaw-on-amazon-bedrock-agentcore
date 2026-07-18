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
        envelope = self._repository.get(
            user_id=user_id,
            provider=READONLY_PROVIDER,
        )
        return "DISCONNECTED" if envelope is None else "CONNECTED"

    def disconnect(self, user_id: str) -> str:
        user_id = _user_id(user_id)
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
