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


class DisconnectTable:
    def __init__(self, items):
        self.items = {
            (item["PK"], item["SK"]): dict(item) for item in items
        }

    def get_item(self, **kwargs):
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        item = self.items.get(key)
        return {"Item": dict(item)} if item is not None else {}

    def put_item(self, **kwargs):
        item = dict(kwargs["Item"])
        key = (item["PK"], item["SK"])
        if kwargs.get("ConditionExpression") and key in self.items:
            raise RuntimeError("condition")
        self.items[key] = item
        return {}

    def update_item(self, **kwargs):
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        item = self.items.get(key)
        values = kwargs["ExpressionAttributeValues"]
        expected_statuses = {
            value
            for name, value in values.items()
            if name.startswith(":expectedStatus")
        }
        if (
            item is None
            or item.get("generation") != values[":expected"]
            or expected_statuses
            and item.get("status") not in expected_statuses
        ):
            raise RuntimeError("condition")
        item["generation"] = values[":next"]
        item["status"] = values[":status"]
        item["updatedAt"] = values[":now"]
        return {}

    def query(self, **kwargs):
        values = kwargs["ExpressionAttributeValues"]
        pk = values[":pk"]
        prefix = values[":prefix"]
        matches = [
            dict(item)
            for (item_pk, item_sk), item in sorted(self.items.items())
            if item_pk == pk and item_sk.startswith(prefix)
        ]
        return {"Items": matches[: kwargs["Limit"]]}

    def delete_item(self, **kwargs):
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        self.items.pop(key, None)
        return {}


def test_disconnect_fences_writers_and_purges_all_gmail_derived_namespaces():
    pk = f"USER#{USER}"
    table = DisconnectTable(
        [
            {
                "PK": pk,
                "SK": f"CONNECTION#{PROVIDER}",
                "envelope": {
                    "format": "personal-operator.oauth-envelope.v1",
                    "binding": "a" * 64,
                    "wrappedKey": "wrapped",
                    "nonce": "nonce",
                    "ciphertext": "ciphertext",
                },
            },
            {"PK": pk, "SK": "GMAIL#OPPORTUNITIES", "private": "address"},
            {"PK": pk, "SK": "GMAIL#DRAFT#draft_12345678#0000000001", "private": "body"},
            {"PK": pk, "SK": "TELEGRAM_CALLBACK#" + "a" * 64, "private": "source"},
            {"PK": pk, "SK": "MEMORY#main", "private": "preserve"},
        ]
    )
    lifecycle = DynamoConnectionLifecycle(table)

    assert lifecycle.disconnect(USER) == "DISCONNECTED"

    remaining = {sk: item for (item_pk, sk), item in table.items.items() if item_pk == pk}
    assert set(remaining) == {"GMAIL#CONNECTION_FENCE", "MEMORY#main"}
    assert remaining["GMAIL#CONNECTION_FENCE"]["status"] == "DISCONNECTED"
    assert remaining["GMAIL#CONNECTION_FENCE"]["generation"] == 1
    assert lifecycle.status(USER) == "DISCONNECTED"
    assert lifecycle.disconnect(USER) == "DISCONNECTED"
    assert remaining["MEMORY#main"]["private"] == "preserve"


def test_disconnect_keeps_fence_pending_when_delete_and_confirmation_read_fail():
    pk = f"USER#{USER}"
    connection_key = (pk, f"CONNECTION#{PROVIDER}")

    class AmbiguousConnectionDelete(DisconnectTable):
        def __init__(self, items):
            super().__init__(items)
            self.fail_connection_delete = True
            self.confirming_connection_delete = False

        def delete_item(self, **kwargs):
            key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
            if key == connection_key and self.fail_connection_delete:
                self.confirming_connection_delete = True
                raise TimeoutError("delete outcome is unknown")
            return super().delete_item(**kwargs)

        def get_item(self, **kwargs):
            key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
            if key == connection_key and self.confirming_connection_delete:
                raise TimeoutError("confirmation read is unavailable")
            return super().get_item(**kwargs)

    table = AmbiguousConnectionDelete(
        [
            {
                "PK": pk,
                "SK": f"CONNECTION#{PROVIDER}",
                "envelope": {
                    "format": "personal-operator.oauth-envelope.v1",
                    "binding": "a" * 64,
                    "wrappedKey": "wrapped",
                    "nonce": "nonce",
                    "ciphertext": "ciphertext",
                },
            }
        ]
    )
    lifecycle = DynamoConnectionLifecycle(table)

    with pytest.raises(RuntimeError, match="uncertain"):
        lifecycle.disconnect(USER)

    assert connection_key in table.items
    fence = table.items[(pk, "GMAIL#CONNECTION_FENCE")]
    assert fence["generation"] == 1
    assert fence["status"] == "DISCONNECTING"

    table.fail_connection_delete = False
    table.confirming_connection_delete = False
    assert lifecycle.disconnect(USER) == "DISCONNECTED"
    assert connection_key not in table.items
    assert table.items[(pk, "GMAIL#CONNECTION_FENCE")]["status"] == (
        "DISCONNECTED"
    )
