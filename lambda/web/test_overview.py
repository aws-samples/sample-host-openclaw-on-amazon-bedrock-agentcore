from __future__ import annotations

import pytest

from .overview import DynamoConnectionLifecycle, PilotOverviewService


USER = "user_pilot"
PROVIDER = "google-gmail-readonly"


class Connections:
    def __init__(self, status="CONNECTED"):
        self.value = status
        self.calls = []

    def status(self, user_id):
        self.calls.append(("status", user_id))
        return self.value

    def disconnect(self, user_id):
        self.calls.append(("disconnect", user_id))
        self.value = "DISCONNECTED"
        return self.value


class Workspace:
    def get(self, user_id):
        return {
            "userId": user_id,
            "runtimeState": "IDLE",
            "workspaceReceipt": {
                "generation": "gen_1234567890abcdef",
                "manifestSha256": "a" * 64,
            },
            "files": [
                {"path": "memory.md", "size": 42},
                {"path": "notes/today.md", "size": 12},
            ],
        }


class Gmail:
    def get(self, user_id):
        return {
            "userId": user_id,
            "opportunities": [
                {
                    "id": "opp_12345678",
                    "title": "Ada is waiting",
                    "sourceUrl": "https://mail.google.com/mail/u/0/#inbox/thread-1",
                }
            ],
            "drafts": [{"actionId": "draft_12345678", "revision": 1}],
        }


class Scans:
    def latest(self, user_id):
        assert user_id == USER
        return {
            "scanId": "scan_1234567890abcdef",
            "status": "SUCCEEDED",
            "startedAt": 1_700_000_000,
            "completedAt": 1_700_000_012,
            "resultCount": 1,
            "failureCode": None,
            "feedback": None,
        }


def service(*, status="CONNECTED"):
    return PilotOverviewService(
        connections=Connections(status),
        workspace=Workspace(),
        gmail_workspace=Gmail(),
        scans=Scans(),
    )


@pytest.mark.parametrize(
    "status", ["CONNECTED", "REAUTH_REQUIRED", "DISCONNECTED"]
)
def test_overview_is_typed_read_only_and_contains_no_user_content(status):
    result = service(status=status).get(USER)

    assert result == {
        "version": "personal-operator.pilot-overview.v1",
        "externalEffects": False,
        "connection": {
            "provider": PROVIDER,
            "status": status,
            "access": "READ_ONLY",
        },
        "lastScan": {
            "scanId": "scan_1234567890abcdef",
            "status": "SUCCEEDED",
            "startedAt": 1_700_000_000,
            "completedAt": 1_700_000_012,
            "resultCount": 1,
            "failureCode": None,
            "feedback": None,
        },
        "workspace": {
            "runtimeState": "IDLE",
            "workspaceReceipt": {
                "generation": "gen_1234567890abcdef",
                "manifestSha256": "a" * 64,
            },
            "fileCount": 2,
            "opportunityCount": 1,
            "draftCount": 1,
        },
        "capability": {
            "provider": PROVIDER,
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
    serialized = repr(result)
    assert "Ada" not in serialized
    assert "mail.google.com" not in serialized
    assert USER not in serialized


def test_overview_fails_closed_on_cross_user_or_untyped_projection():
    class CrossUserWorkspace(Workspace):
        def get(self, user_id):
            return {**super().get(user_id), "userId": "user_other"}

    with pytest.raises(RuntimeError, match="workspace"):
        PilotOverviewService(
            connections=Connections(),
            workspace=CrossUserWorkspace(),
            gmail_workspace=Gmail(),
            scans=Scans(),
        ).get(USER)

    with pytest.raises(RuntimeError, match="connection"):
        service(status="BROKEN").get(USER)


def test_authorization_scan_failure_projects_reauthentication_without_provider_call():
    class AuthorizationScan:
        def latest(self, user_id):
            assert user_id == USER
            return {
                "scanId": "scan_00000000001700000000_" + "s" * 32,
                "status": "FAILED",
                "startedAt": 1_700_000_000,
                "completedAt": 1_700_000_001,
                "resultCount": None,
                "failureCode": "AUTHORIZATION",
                "feedback": None,
            }

    result = PilotOverviewService(
        connections=Connections("CONNECTED"),
        workspace=Workspace(),
        gmail_workspace=Gmail(),
        scans=AuthorizationScan(),
    ).get(USER)

    assert result["connection"]["status"] == "REAUTH_REQUIRED"


class Table:
    def __init__(self, item=None):
        self.item = item
        self.calls = []

    def get_item(self, **kwargs):
        self.calls.append(("get", kwargs))
        return {"Item": self.item} if self.item is not None else {}

    def delete_item(self, **kwargs):
        self.calls.append(("delete", kwargs))
        self.item = None
        return {}


class Repository:
    def __init__(self, table):
        self.table = table

    def get(self, *, user_id, provider):
        assert provider == PROVIDER
        assert user_id == USER
        return self.table.item


def test_connection_lifecycle_reads_presence_without_decrypting_and_disconnects_locally():
    envelope = {
        "format": "personal-operator.oauth-envelope.v1",
        "binding": "a" * 64,
        "wrappedKey": "wrapped",
        "nonce": "nonce",
        "ciphertext": "ciphertext",
    }
    table = Table(envelope)
    lifecycle = DynamoConnectionLifecycle(table, repository=Repository(table))

    assert lifecycle.status(USER) == "CONNECTED"
    assert lifecycle.disconnect(USER) == "DISCONNECTED"
    assert lifecycle.status(USER) == "DISCONNECTED"
    deletes = [call for call in table.calls if call[0] == "delete"]
    assert deletes == [
        (
            "delete",
            {
                "Key": {
                    "PK": f"USER#{USER}",
                    "SK": f"CONNECTION#{PROVIDER}",
                }
            },
        )
    ]
    assert not hasattr(lifecycle, "kms")
    assert not hasattr(lifecycle, "provider_client")
