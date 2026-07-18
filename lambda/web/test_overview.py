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
    name = "control-table"

    def __init__(self, items):
        self.items = {
            (item["PK"], item["SK"]): dict(item) for item in items
        }

        class _Meta:
            pass

        self.meta = _Meta()
        self.meta.client = self

    @staticmethod
    def _decode_value(value):
        if "S" in value:
            return value["S"]
        if "N" in value:
            return int(value["N"])
        if "BOOL" in value:
            return value["BOOL"]
        if "NULL" in value:
            return None
        if "M" in value:
            return {n: DisconnectTable._decode_value(f) for n, f in value["M"].items()}
        if "L" in value:
            return [DisconnectTable._decode_value(f) for f in value["L"]]
        raise AssertionError(value)

    @classmethod
    def _decode_item(cls, item):
        return {n: cls._decode_value(f) for n, f in item.items()}

    def transact_write_items(self, **kwargs):
        operations = kwargs["TransactItems"]
        pending = {k: dict(v) for k, v in self.items.items()}
        for operation in operations:
            if "ConditionCheck" in operation:
                check = operation["ConditionCheck"]
                decoded_key = self._decode_item(check["Key"])
                item = pending.get((decoded_key["PK"], decoded_key["SK"]))
                values = {
                    n: self._decode_value(v)
                    for n, v in check["ExpressionAttributeValues"].items()
                }
                if not (
                    item is not None
                    and item.get("generation") == values[":generation"]
                    and item.get("status") == values[":status"]
                ):
                    raise RuntimeError("transaction condition rejected")
            elif "Delete" in operation:
                decoded_key = self._decode_item(operation["Delete"]["Key"])
                pending.pop((decoded_key["PK"], decoded_key["SK"]), None)
            else:
                raise AssertionError(operation)
        self.items = pending

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


def test_slow_disconnect_runner_cannot_delete_a_newly_reconnected_envelope():
    # Reproduces the reported same-generation interleaving: a slow runner
    # captured DISCONNECTING generation g; a faster runner finished; the user
    # reconnected with a new envelope; the slow runner resumes its deletes. The
    # fenced deletes must fail closed so the reconnected records survive.
    from workflows.gmail.repository import (
        ConnectionFenceError,
        DynamoGmailRepository,
    )

    pk = f"USER#{USER}"

    class FakeClient:
        def __init__(self, table):
            self.table = table

        def transact_write_items(self, **kwargs):
            self.table.transact_write_items(**kwargs)

    class TransactionalTable(DisconnectTable):
        name = "control-table"

        def __init__(self, items):
            super().__init__(items)

            class Meta:
                pass

            self.meta = Meta()
            self.meta.client = FakeClient(self)

        @staticmethod
        def _decode_value(value):
            if "S" in value:
                return value["S"]
            if "N" in value:
                number = int(value["N"])
                return number
            if "BOOL" in value:
                return value["BOOL"]
            if "NULL" in value:
                return None
            if "M" in value:
                return {n: TransactionalTable._decode_value(f) for n, f in value["M"].items()}
            if "L" in value:
                return [TransactionalTable._decode_value(f) for f in value["L"]]
            raise AssertionError(value)

        @classmethod
        def _decode_item(cls, item):
            return {n: cls._decode_value(f) for n, f in item.items()}

        def transact_write_items(self, **kwargs):
            operations = kwargs["TransactItems"]
            pending = {k: dict(v) for k, v in self.items.items()}
            for operation in operations:
                if "ConditionCheck" in operation:
                    check = operation["ConditionCheck"]
                    key = (
                        self._decode_item(check["Key"])["PK"],
                        self._decode_item(check["Key"])["SK"],
                    )
                    values = {
                        n: self._decode_value(v)
                        for n, v in check["ExpressionAttributeValues"].items()
                    }
                    item = pending.get(key)
                    if not (
                        item is not None
                        and item.get("generation") == values[":generation"]
                        and item.get("status") == values[":status"]
                    ):
                        raise RuntimeError("transaction condition rejected")
                elif "Delete" in operation:
                    key_item = self._decode_item(operation["Delete"]["Key"])
                    pending.pop((key_item["PK"], key_item["SK"]), None)
                else:
                    raise AssertionError(operation)
            self.items = pending

    table = TransactionalTable(
        [
            {
                "PK": pk,
                "SK": f"CONNECTION#{PROVIDER}",
                "connectionGeneration": 1,
                "envelope": {"format": "personal-operator.oauth-envelope.v1"},
            }
        ]
    )
    repo = DynamoGmailRepository(table)
    lifecycle = DynamoConnectionLifecycle(table, repository=repo)

    # Slow runner begins the disconnect and captures generation 1.
    generation = repo.begin_disconnect(USER)
    assert generation == 1

    # A faster runner finishes and the user reconnects with a NEW envelope at
    # generation 2 (fence CONNECTED).
    repo.finish_disconnect(USER, generation)
    table.items[(pk, "GMAIL#CONNECTION_FENCE")] = {
        "PK": pk,
        "SK": "GMAIL#CONNECTION_FENCE",
        "recordType": "GMAIL_CONNECTION_FENCE",
        "userId": USER,
        "generation": 2,
        "status": "CONNECTED",
        "updatedAt": 1,
    }
    reconnected = {
        "PK": pk,
        "SK": f"CONNECTION#{PROVIDER}",
        "connectionGeneration": 2,
        "envelope": {"format": "personal-operator.oauth-envelope.v1", "fresh": True},
    }
    table.items[(pk, f"CONNECTION#{PROVIDER}")] = dict(reconnected)

    # The slow runner resumes its fenced delete against the stale generation 1.
    with pytest.raises(ConnectionFenceError):
        repo.delete_under_disconnecting_fence(
            USER,
            generation,
            {"PK": pk, "SK": f"CONNECTION#{PROVIDER}"},
        )

    assert table.items[(pk, f"CONNECTION#{PROVIDER}")] == reconnected
    assert repo.connection_status(USER) == "CONNECTED"


def test_disconnect_keeps_fence_pending_when_delete_and_confirmation_read_fail():
    pk = f"USER#{USER}"
    connection_key = (pk, f"CONNECTION#{PROVIDER}")

    class AmbiguousConnectionDelete(DisconnectTable):
        def __init__(self, items):
            super().__init__(items)
            self.fail_connection_delete = True
            self.confirming_connection_delete = False

        def transact_write_items(self, **kwargs):
            deletes = [
                self._decode_item(op["Delete"]["Key"])
                for op in kwargs["TransactItems"]
                if "Delete" in op
            ]
            if (
                self.fail_connection_delete
                and any(
                    (d["PK"], d["SK"]) == connection_key for d in deletes
                )
            ):
                # The write's outcome is unknown and the confirming read below
                # is also unavailable, so the purge must stay pending.
                self.confirming_connection_delete = True
                raise TimeoutError("delete outcome is unknown")
            return super().transact_write_items(**kwargs)

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
                "connectionGeneration": 0,
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
