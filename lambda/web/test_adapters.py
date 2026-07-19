from __future__ import annotations

from decimal import Decimal
import hashlib
import io

import pytest

from .adapters import (
    DataAdapterError,
    DataDeletionPending,
    DynamoUserDataStore,
    DynamoUserFootprintStore,
    S3WorkspaceStore,
)


USER = "user_founder"
EXPECTED_MAX_USER_RECORD_ITEMS = 1_000
EXPECTED_MAX_USER_RECORD_PAGES = 20
PAYLOAD_HASH = "a" * 64
MESSAGE_ID = "<po-aaaaaaaaaaaaaaaaaaaaaaaa@personal-operator.invalid>"


def _effect_receipt() -> dict[str, object]:
    return {
        "providerMessageId": "gmail-message-1",
        "providerThreadId": "gmail-thread-1",
        "messageId": MESSAGE_ID,
        "connectionId": "google_conn_1234",
        "accountEmail": "founder@example.com",
        "senderAddress": "founder@example.com",
        "recipient": "person@example.net",
        "payloadHash": PAYLOAD_HASH,
        "executedAt": "2026-07-18T12:00:00+00:00",
        "labels": ["SENT"],
    }


def _confirmed_action() -> dict[str, object]:
    return {
        "PK": f"USER#{USER}",
        "SK": "ACTION#action_12345678",
        "actionId": "action_12345678",
        "userId": USER,
        "state": "CONFIRMED",
        "connectionId": "google_conn_1234",
        "accountEmail": "founder@example.com",
        "senderAddress": "founder@example.com",
        "args": {"to": "person@example.net", "subject": "Hi", "body": "Body"},
        "payloadHash": PAYLOAD_HASH,
        "messageId": MESSAGE_ID,
        "effectReceipt": _effect_receipt(),
    }


class S3:
    def __init__(self):
        self.deleted = []
        self.aborted = []
        self.list_prefixes = []
        self.objects = {
            f"{USER}/files/memory.md": b"memory",
            f"{USER}/files/notes/plan.md": b"plan",
            f"{USER}/.system/workspace/v1/current.json": b"internal",
            f"{USER}/.system/workspace/v1/generations/deleted-parent/payload": b"old",
            "user_other/files/memory.md": b"other",
        }

    def list_objects_v2(self, **kwargs):
        self.list_prefixes.append(kwargs["Prefix"])
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

    def list_object_versions(self, **kwargs):
        prefix = kwargs["Prefix"]
        return {
            "Versions": [
                {"Key": key, "VersionId": "v1"}
                for key in self.objects
                if key.startswith(prefix)
            ],
            "DeleteMarkers": [
                {"Key": f"{prefix}old.md", "VersionId": "marker1"}
            ],
            "IsTruncated": False,
        }

    def list_multipart_uploads(self, **_kwargs):
        return {"Uploads": [], "IsTruncated": False}

    def abort_multipart_upload(self, **kwargs):
        self.aborted.append(kwargs)
        return {}

    def delete_objects(self, **kwargs):
        self.deleted.extend(kwargs["Delete"]["Objects"])
        return {"Deleted": kwargs["Delete"]["Objects"]}


def test_workspace_export_is_user_prefixed_and_delete_removes_versions_and_markers():
    s3 = S3()
    store = S3WorkspaceStore(s3, bucket_name="workspace-bucket")

    assert store.workspace_files(USER) == {
        "memory.md": b"memory",
        "notes/plan.md": b"plan",
    }
    assert s3.list_prefixes == [f"{USER}/files/"]
    store.delete_namespace(USER)
    assert {item["Key"] for item in s3.deleted} == {
        f"{USER}/files/memory.md",
        f"{USER}/files/notes/plan.md",
        f"{USER}/.system/workspace/v1/current.json",
        f"{USER}/.system/workspace/v1/generations/deleted-parent/payload",
        f"{USER}/old.md",
    }
    assert all("VersionId" in item for item in s3.deleted)


class DeletionS3:
    def __init__(
        self,
        *,
        multipart_pages=None,
        version_pages=None,
        delete_responses=None,
        abort_responses=None,
    ):
        self.multipart_pages = list(
            multipart_pages
            if multipart_pages is not None
            else [{"Uploads": [], "IsTruncated": False}]
        )
        self.version_pages = list(
            version_pages
            if version_pages is not None
            else [{"Versions": [], "DeleteMarkers": [], "IsTruncated": False}]
        )
        self.delete_responses = list(delete_responses or [])
        self.abort_responses = list(abort_responses or [])
        self.multipart_requests = []
        self.version_requests = []
        self.aborted = []
        self.delete_requests = []
        self.events = []

    def list_multipart_uploads(self, **kwargs):
        self.multipart_requests.append(kwargs)
        self.events.append("list-multipart")
        return self.multipart_pages.pop(0)

    def abort_multipart_upload(self, **kwargs):
        self.aborted.append(kwargs)
        self.events.append("abort-multipart")
        if self.abort_responses:
            return self.abort_responses.pop(0)
        return {}

    def list_object_versions(self, **kwargs):
        self.version_requests.append(kwargs)
        self.events.append("list-versions")
        return self.version_pages.pop(0)

    def delete_objects(self, **kwargs):
        self.delete_requests.append(kwargs)
        self.events.append("delete-versions")
        if self.delete_responses:
            return self.delete_responses.pop(0)
        return {"Deleted": kwargs["Delete"]["Objects"]}


def test_workspace_deletion_aborts_exact_user_multipart_uploads_across_pages_first():
    prefix = f"{USER}/"
    s3 = DeletionS3(
        multipart_pages=[
            {
                "Uploads": [{"Key": f"{prefix}files/a.md", "UploadId": "upload-a"}],
                "IsTruncated": True,
                "NextKeyMarker": f"{prefix}files/a.md",
                "NextUploadIdMarker": "upload-a",
            },
            {
                "Uploads": [{"Key": f"{prefix}.system/payload", "UploadId": "upload-b"}],
                "IsTruncated": False,
            },
        ]
    )

    S3WorkspaceStore(s3, bucket_name="workspace-bucket").delete_namespace(USER)

    assert s3.multipart_requests == [
        {"Bucket": "workspace-bucket", "Prefix": prefix, "MaxUploads": 1_000},
        {
            "Bucket": "workspace-bucket",
            "Prefix": prefix,
            "MaxUploads": 1_000,
            "KeyMarker": f"{prefix}files/a.md",
            "UploadIdMarker": "upload-a",
        },
    ]
    assert s3.aborted == [
        {"Bucket": "workspace-bucket", "Key": f"{prefix}files/a.md", "UploadId": "upload-a"},
        {"Bucket": "workspace-bucket", "Key": f"{prefix}.system/payload", "UploadId": "upload-b"},
    ]
    assert s3.version_requests == [
        {"Bucket": "workspace-bucket", "Prefix": prefix, "MaxKeys": 1_000}
    ]
    assert s3.events == [
        "list-multipart",
        "abort-multipart",
        "list-multipart",
        "abort-multipart",
        "list-versions",
    ]


def test_workspace_deletion_with_zero_multipart_uploads_still_deletes_versions():
    s3 = DeletionS3()

    S3WorkspaceStore(s3, bucket_name="workspace-bucket").delete_namespace(USER)

    assert len(s3.multipart_requests) == 1
    assert s3.aborted == []
    assert len(s3.version_requests) == 1


@pytest.mark.parametrize(
    "page",
    [
        None,
        {"Uploads": None, "IsTruncated": False},
        {"Uploads": [], "IsTruncated": "false"},
        {"Uploads": [], "IsTruncated": True, "NextKeyMarker": "next"},
        {"Uploads": [], "IsTruncated": False, "Prefix": "user_other/"},
        {
            "Uploads": [],
            "IsTruncated": False,
            "NextKeyMarker": f"{USER}/files/a",
            "NextUploadIdMarker": "upload-a",
        },
        {
            "Uploads": [
                {"Key": f"{USER}/files/a", "UploadId": "same"},
                {"Key": f"{USER}/files/a", "UploadId": "same"},
            ],
            "IsTruncated": False,
        },
    ],
)
def test_workspace_deletion_rejects_malformed_or_ambiguous_multipart_listing(page):
    s3 = DeletionS3(multipart_pages=[page])

    with pytest.raises(DataAdapterError):
        S3WorkspaceStore(s3, bucket_name="workspace-bucket").delete_namespace(USER)

    assert s3.aborted == []
    assert s3.version_requests == []


@pytest.mark.parametrize(
    "upload",
    [
        {"Key": "user_founder_collision/file", "UploadId": "upload"},
        {"Key": f"{USER}/files/a", "UploadId": ""},
        {"Key": f"{USER}/files/a", "UploadId": 123},
    ],
)
def test_workspace_deletion_binds_every_multipart_upload_to_exact_namespace(upload):
    s3 = DeletionS3(
        multipart_pages=[{"Uploads": [upload], "IsTruncated": False}]
    )

    with pytest.raises(DataAdapterError):
        S3WorkspaceStore(s3, bucket_name="workspace-bucket").delete_namespace(USER)

    assert s3.aborted == []
    assert s3.version_requests == []


def test_workspace_deletion_rejects_repeated_multipart_pagination_cursor():
    prefix = f"{USER}/"
    marker = f"{prefix}files/a"
    page = {
        "Uploads": [],
        "IsTruncated": True,
        "NextKeyMarker": marker,
        "NextUploadIdMarker": "upload-a",
    }
    s3 = DeletionS3(multipart_pages=[dict(page), dict(page)])

    with pytest.raises(DataAdapterError):
        S3WorkspaceStore(s3, bucket_name="workspace-bucket").delete_namespace(USER)

    assert s3.aborted == []
    assert s3.version_requests == []


def test_workspace_deletion_keeps_completion_pending_when_abort_is_ambiguous():
    s3 = DeletionS3(
        multipart_pages=[
            {
                "Uploads": [
                    {"Key": f"{USER}/files/a", "UploadId": "upload-a"}
                ],
                "IsTruncated": False,
            }
        ],
        abort_responses=[None],
    )

    with pytest.raises(DataAdapterError):
        S3WorkspaceStore(s3, bucket_name="workspace-bucket").delete_namespace(USER)

    assert s3.version_requests == []


@pytest.mark.parametrize(
    "response",
    [
        None,
        {"Deleted": [{"Key": f"{USER}/files/wrong", "VersionId": "v1"}]},
        {"Deleted": [{"Key": f"{USER}/files/a", "VersionId": "v1"}], "Errors": None},
    ],
)
def test_workspace_deletion_requires_exact_delete_objects_evidence(response):
    requested = {"Key": f"{USER}/files/a", "VersionId": "v1"}
    s3 = DeletionS3(
        version_pages=[
            {"Versions": [requested], "DeleteMarkers": [], "IsTruncated": False}
        ],
        delete_responses=[response],
    )

    with pytest.raises(DataAdapterError):
        S3WorkspaceStore(s3, bucket_name="workspace-bucket").delete_namespace(USER)


@pytest.mark.parametrize(
    "pages",
    [
        [{"Versions": [], "DeleteMarkers": [], "IsTruncated": "false"}],
        [
            {
                "Versions": [],
                "DeleteMarkers": [],
                "IsTruncated": False,
                "Prefix": "user_other/",
            }
        ],
        [
            {
                "Versions": [],
                "DeleteMarkers": [],
                "IsTruncated": False,
                "NextKeyMarker": f"{USER}/files/a",
                "NextVersionIdMarker": "v1",
            }
        ],
        [
            {
                "Versions": [],
                "DeleteMarkers": [],
                "IsTruncated": True,
                "NextKeyMarker": f"{USER}/files/a",
            }
        ],
        [
            {
                "Versions": [],
                "DeleteMarkers": [],
                "IsTruncated": True,
                "NextKeyMarker": f"{USER}/files/a",
                "NextVersionIdMarker": "v1",
            },
            {
                "Versions": [],
                "DeleteMarkers": [],
                "IsTruncated": True,
                "NextKeyMarker": f"{USER}/files/a",
                "NextVersionIdMarker": "v1",
            },
        ],
    ],
)
def test_workspace_deletion_rejects_ambiguous_version_pagination(pages):
    s3 = DeletionS3(version_pages=pages)

    with pytest.raises(DataAdapterError):
        S3WorkspaceStore(s3, bucket_name="workspace-bucket").delete_namespace(USER)


class Table:
    def __init__(self):
        self.items = [
            {"PK": f"USER#{USER}", "SK": "MEMORY#main", "text": "remember"},
            {"PK": f"USER#{USER}", "SK": "SCHEDULE#weekly", "name": "weekly"},
            _confirmed_action(),
            {"PK": f"USER#{USER}", "SK": "RECEIPT#legacy", "effectReceipt": {"id": "legacy"}},
            {"PK": f"USER#{USER}", "SK": "CONNECTION#google", "envelope": {"secret": "x"}},
            {"PK": f"USER#{USER}", "SK": "WEB_REVOKED", "revoked": True},
        ]
        self.deleted = []

    def query(self, **kwargs):
        values = kwargs["ExpressionAttributeValues"]
        prefix = values.get(":prefix")
        return {
            "Items": [
                dict(item)
                for item in self.items
                if item["PK"] == values[":pk"]
                and (prefix is None or item["SK"].startswith(prefix))
            ]
        }

    def delete_item(self, **kwargs):
        self.deleted.append(kwargs["Key"])


def test_user_records_export_allowlist_excludes_connections_and_deletion_removes_marker():
    table = Table()
    store = DynamoUserDataStore(table)

    records = store.records_for_user(USER)
    assert records == {
        "memory": [{"text": "remember"}],
        "schedules": [{"name": "weekly"}],
        "installed_packs": [],
        "connectors": [{"connectorId": "google", "state": "DISCONNECTED"}],
        "compute_receipts": [],
        "receipts": [_effect_receipt()],
    }
    store.revoke_all(USER)
    assert {item["SK"] for item in table.deleted} == {"CONNECTION#google"}

    table.deleted.clear()
    store.delete_user_records(USER)
    assert {item["SK"] for item in table.deleted} == {
        "MEMORY#main",
        "SCHEDULE#weekly",
        "ACTION#action_12345678",
        "RECEIPT#legacy",
        "CONNECTION#google",
        "WEB_REVOKED",
    }


def test_user_record_export_logically_skips_expired_ttl_during_dynamodb_lag():
    table = PaginatedTable(
        [
            {
                "Items": [
                    {
                        "PK": f"USER#{USER}",
                        "SK": "MEMORY#expired",
                        "text": "must-not-export",
                        "ttl": 100,
                    },
                    {
                        "PK": f"USER#{USER}",
                        "SK": "MEMORY#live",
                        "text": "still-live",
                        "ttl": 101,
                    },
                ]
            }
        ]
    )

    records = DynamoUserDataStore(table, now=lambda: 100).records_for_user(USER)

    assert records["memory"] == [{"text": "still-live", "ttl": 101}]


def test_user_record_export_normalizes_integral_boto3_decimals_to_json_ints():
    table = PaginatedTable(
        [
            {
                "Items": [
                    {
                        "PK": f"USER#{USER}",
                        "SK": "MEMORY#decimal",
                        "ttl": Decimal("101"),
                        "revision": Decimal("7"),
                        "nested": [{"count": Decimal("2")}],
                    }
                ]
            }
        ]
    )

    records = DynamoUserDataStore(table, now=lambda: 100).records_for_user(USER)

    assert records["memory"] == [
        {
            "ttl": 101,
            "revision": 7,
            "nested": [{"count": 2}],
        }
    ]


def test_user_record_export_rejects_fractional_decimal_without_precision_loss():
    table = PaginatedTable(
        [
            {
                "Items": [
                    {
                        "PK": f"USER#{USER}",
                        "SK": "MEMORY#fractional",
                        "exact": Decimal(
                            "0.12345678901234567890123456789012345678"
                        ),
                    }
                ]
            }
        ]
    )

    with pytest.raises(DataAdapterError, match="number is not exactly portable"):
        DynamoUserDataStore(table).records_for_user(USER)


def test_portable_native_source_reads_installations_and_typed_compute_history_only():
    compute_receipt = {
        "schema": "personal-operator.compute-receipt.v1",
        "jobId": "job_" + "a" * 64,
        "status": "FAILED",
        "imageDigest": "sha256:" + "b" * 64,
        "inputDigest": "c" * 64,
        "outputFiles": [],
        "startedAt": 100,
        "completedAt": 101,
        "errorCode": "SYNTHETIC_FAILURE",
    }
    table = PaginatedTable(
        [
            {
                "Items": [
                    {
                        "PK": f"USER#{USER}",
                        "SK": "COMPUTE_RECEIPT#job_" + "a" * 64,
                        "computeReceipt": compute_receipt,
                    },
                    {
                        "PK": f"USER#{USER}",
                        "SK": "CONNECTION#google-gmail-readonly",
                        "envelope": {"ciphertext": "must-not-export"},
                    },
                ]
            }
        ]
    )

    class InstallationTable:
        def __init__(self):
            self.reads = []

        def get_item(self, **request):
            self.reads.append(request)
            if request["Key"]["SK"] != "INSTALL#schedule.list":
                return {}
            return {
                "Item": {
                    "PK": f"USER#{USER}",
                    "SK": "INSTALL#schedule.list",
                    "recordJson": (
                        '{"catalogDigest":"' + "d" * 64
                        + '","connectionRefs":["conn_12345678"],'
                        '"killSwitch":false,"packId":"schedule.list",'
                        '"policyRevision":1,'
                        '"schema":"personal-operator.capability-installation.v1",'
                        '"state":"ENABLED","userId":"' + USER + '"}'
                    ),
                    "version": 1,
                }
            }

    installations = InstallationTable()
    records = DynamoUserDataStore(
        table,
        installation_table=installations,
    ).records_for_user(USER)

    assert records["installed_packs"] == [
        {
            "schema": "personal-operator.capability-installation.v1",
            "userId": USER,
            "packId": "schedule.list",
            "catalogDigest": "d" * 64,
            "state": "PAUSED",
            "policyRevision": 1,
            "connectionRefs": [],
            "killSwitch": True,
        }
    ]
    assert records["connectors"] == [
        {"connectorId": "google-gmail-readonly", "state": "DISCONNECTED"}
    ]
    assert records["compute_receipts"] == [compute_receipt]
    assert "ciphertext" not in str(records)
    assert installations.reads
    assert all(read["ConsistentRead"] is True for read in installations.reads)


@pytest.mark.parametrize("response", [None, [], "malformed", 1])
def test_portable_installation_read_rejects_malformed_success_response(response):
    class InstallationTable:
        def get_item(self, **_request):
            return response

    with pytest.raises(DataAdapterError, match="installation read"):
        DynamoUserDataStore(
            PaginatedTable([{"Items": []}]),
            installation_table=InstallationTable(),
        ).records_for_user(USER)


@pytest.mark.parametrize("ttl", [True, 0, 1.5, "100"])
def test_user_record_export_fails_closed_on_malformed_ttl(ttl):
    table = PaginatedTable(
        [
            {
                "Items": [
                    {
                        "PK": f"USER#{USER}",
                        "SK": "MEMORY#bad-ttl",
                        "ttl": ttl,
                    }
                ]
            }
        ]
    )

    with pytest.raises(DataAdapterError, match="TTL"):
        DynamoUserDataStore(table, now=lambda: 100).records_for_user(USER)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda action: action["effectReceipt"].update(labels=["INBOX"]),
        lambda action: action["effectReceipt"].update(payloadHash="b" * 64),
        lambda action: action.update(userId="other-user"),
    ],
)
def test_confirmed_action_receipt_must_be_valid_and_exactly_bound(mutation):
    action = _confirmed_action()
    mutation(action)
    table = PaginatedTable([{"Items": [action]}])

    with pytest.raises(DataAdapterError, match="confirmed action receipt"):
        DynamoUserDataStore(table).records_for_user(USER)


class PaginatedTable:
    def __init__(self, pages):
        self.pages = list(pages)
        self.queries = []
        self.deleted = []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        return self.pages[len(self.queries) - 1]

    def delete_item(self, **kwargs):
        self.deleted.append(kwargs["Key"])


def _item(sk: str) -> dict[str, str]:
    return {"PK": f"USER#{USER}", "SK": sk}


def _cursor(sk: str) -> dict[str, str]:
    return {"PK": f"USER#{USER}", "SK": sk}


def test_user_record_query_follows_every_page_with_an_exact_bounded_request():
    cursor = _cursor("MEMORY#main")
    table = PaginatedTable(
        [
            {"Items": [_item("MEMORY#main")], "LastEvaluatedKey": cursor},
            {"Items": [_item("SCHEDULE#weekly")]},
        ]
    )

    records = DynamoUserDataStore(table).records_for_user(USER)

    assert records == {
        "memory": [{}],
        "schedules": [{}],
        "installed_packs": [],
        "connectors": [],
        "compute_receipts": [],
        "receipts": [],
    }
    assert len(table.queries) == 2
    assert table.queries[0]["Limit"] == 100
    assert "ExclusiveStartKey" not in table.queries[0]
    assert table.queries[1]["ExclusiveStartKey"] == cursor


def test_revoke_and_delete_cover_records_after_the_first_page():
    revoke_pages = [
        {
            "Items": [_item("CONNECTION#google")],
            "LastEvaluatedKey": _cursor("CONNECTION#google"),
        },
        {"Items": [_item("CONNECTION#calendar")]},
    ]
    delete_pages = [
        {
            "Items": [_item("MEMORY#main"), _item("CONNECTION#google")],
            "LastEvaluatedKey": _cursor("CONNECTION#google"),
        },
        {
            "Items": [_item("CONNECTION#calendar"), _item("WEB_REVOKED")]
        },
    ]
    revoke_table = PaginatedTable(revoke_pages)
    delete_table = PaginatedTable(delete_pages)

    DynamoUserDataStore(revoke_table).revoke_all(USER)
    DynamoUserDataStore(delete_table).delete_user_records(USER)

    assert {item["SK"] for item in revoke_table.deleted} == {
        "CONNECTION#google",
        "CONNECTION#calendar",
    }
    assert {item["SK"] for item in delete_table.deleted} == {
        "MEMORY#main",
        "CONNECTION#google",
        "CONNECTION#calendar",
        "WEB_REVOKED",
    }


@pytest.mark.parametrize(
    "cursor",
    [
        {},
        [],
        {"PK": f"USER#{USER}"},
        {"PK": "USER#user_other", "SK": "MEMORY#main"},
        {"PK": f"USER#{USER}", "SK": ""},
        {"PK": f"USER#{USER}", "SK": "MEMORY#main", "extra": "value"},
    ],
)
def test_malformed_user_record_cursor_fails_before_any_delete(cursor):
    table = PaginatedTable(
        [{"Items": [_item("CONNECTION#google")], "LastEvaluatedKey": cursor}]
    )

    with pytest.raises(DataAdapterError, match="pagination is invalid"):
        DynamoUserDataStore(table).revoke_all(USER)

    assert table.deleted == []


def test_user_record_item_overflow_fails_before_any_delete():
    table = PaginatedTable(
        [
            {
                "Items": [
                    _item(f"CONNECTION#provider-{index}")
                    for index in range(EXPECTED_MAX_USER_RECORD_ITEMS + 1)
                ]
            }
        ]
    )

    with pytest.raises(DataAdapterError, match="deletion query"):
        DynamoUserDataStore(table).revoke_all(USER)

    assert table.deleted == []


def test_user_record_page_overflow_makes_bounded_progress_and_requests_retry():
    pages = [
        {
            "Items": [_item(f"CONNECTION#provider-{index}")],
            "LastEvaluatedKey": _cursor(f"CONNECTION#provider-{index}"),
        }
        for index in range(EXPECTED_MAX_USER_RECORD_PAGES)
    ]
    table = PaginatedTable(pages)

    with pytest.raises(DataDeletionPending, match="another pass"):
        DynamoUserDataStore(table).revoke_all(USER)

    assert len(table.queries) == EXPECTED_MAX_USER_RECORD_PAGES
    assert len(table.deleted) == EXPECTED_MAX_USER_RECORD_PAGES


class MutableUserTable:
    def __init__(self, items):
        self.items = {
            (item["PK"], item["SK"]): dict(item)
            for item in items
        }
        self.queries = []
        self.deleted = []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        values = kwargs["ExpressionAttributeValues"]
        partition_key = values[":pk"]
        prefix = values.get(":prefix")
        start = kwargs.get("ExclusiveStartKey", {}).get("SK")
        rows = sorted(
            (
                dict(item)
                for (pk, sk), item in self.items.items()
                if pk == partition_key
                and (prefix is None or sk.startswith(prefix))
                and (start is None or sk > start)
            ),
            key=lambda item: item["SK"],
        )
        page = rows[: kwargs["Limit"]]
        response = {"Items": page}
        if len(rows) > len(page):
            response["LastEvaluatedKey"] = {
                "PK": partition_key,
                "SK": page[-1]["SK"],
            }
        return response

    def delete_item(self, **kwargs):
        key = kwargs["Key"]
        self.deleted.append(dict(key))
        self.items.pop((key["PK"], key["SK"]), None)


def test_user_record_deletion_completes_across_bounded_reconciliation_passes():
    table = MutableUserTable(
        [
            _item(f"MEMORY#{index:04d}")
            for index in range(2_101)
        ]
        + [_item("WEB_REVOKED")]
    )
    store = DynamoUserDataStore(table)

    with pytest.raises(DataDeletionPending, match="another pass"):
        store.delete_user_records(USER)

    assert len(table.deleted) == 2_000
    store.delete_user_records(USER)
    assert len(table.deleted) == 2_102
    assert list(table.items.values()) == []


class ExternalTable:
    name = "openclaw-identity"

    def __init__(
        self,
        *,
        partition_pages=None,
        index_pages=None,
        conditional_fail_keys=(),
        strong_items=None,
        transaction_error=None,
        transaction_error_after_commit=None,
    ):
        self.partition_pages = list(partition_pages or [{"Items": []}])
        self.index_pages = list(index_pages or [{"Items": []}])
        self.partition_calls = []
        self.index_calls = []
        self.deleted = []
        self.put_items = []
        self.get_calls = []
        self.transactions = []
        self.events = []
        self.conditional_fail_keys = [dict(key) for key in conditional_fail_keys]
        self.strong_items = {
            (item["PK"], item["SK"]): dict(item)
            for item in (strong_items or [])
        }
        self.transaction_error = transaction_error
        self.transaction_error_after_commit = transaction_error_after_commit
        self.meta = type("TableMeta", (), {"client": self})()

    def query(self, **kwargs):
        self.events.append("index-query" if kwargs.get("IndexName") else "partition-query")
        if kwargs.get("IndexName"):
            self.index_calls.append(kwargs)
            return self.index_pages[len(self.index_calls) - 1]
        self.partition_calls.append(kwargs)
        return self.partition_pages[len(self.partition_calls) - 1]

    def delete_item(self, **kwargs):
        self.events.append("delete")
        if (
            kwargs.get("ConditionExpression")
            and kwargs["Key"] in self.conditional_fail_keys
        ):
            error = RuntimeError("conditional")
            error.response = {
                "Error": {"Code": "ConditionalCheckFailedException"}
            }
            raise error
        self.deleted.append(kwargs)
        return {}

    def put_item(self, **kwargs):
        self.events.append("put")
        item = dict(kwargs["Item"])
        self.put_items.append(kwargs)
        self.strong_items[(item["PK"], item["SK"])] = item
        return {}

    def get_item(self, **kwargs):
        self.events.append("get")
        self.get_calls.append(kwargs)
        item = self.strong_items.get((kwargs["Key"]["PK"], kwargs["Key"]["SK"]))
        return {"Item": dict(item)} if item is not None else {}

    @staticmethod
    def _decode(values):
        return {
            key: next(iter(value.values()))
            for key, value in values.items()
        }

    def transact_write_items(self, **kwargs):
        self.events.append("transaction")
        self.transactions.append(kwargs)
        if self.transaction_error is not None:
            raise self.transaction_error
        decoded_deletes = []
        decoded_puts = []
        for operation in kwargs["TransactItems"]:
            if "Delete" in operation:
                key = self._decode(operation["Delete"]["Key"])
                if key in self.conditional_fail_keys:
                    error = RuntimeError("conditional")
                    error.response = {
                        "Error": {"Code": "ConditionalCheckFailedException"}
                    }
                    raise error
                decoded_deletes.append(key)
            elif "Put" in operation:
                decoded_puts.append(self._decode(operation["Put"]["Item"]))
        # Apply only after every condition has passed, matching DynamoDB's
        # all-or-nothing transaction boundary.
        for item in decoded_puts:
            self.strong_items[(item["PK"], item["SK"])] = item
        for key in decoded_deletes:
            self.deleted.append({"Key": key, "Transact": True})
            self.strong_items.pop((key["PK"], key["SK"]), None)
        if self.transaction_error_after_commit is not None:
            raise self.transaction_error_after_commit
        return {}


def test_external_user_footprint_deletes_historical_identity_and_message_rows():
    channel_pk = "CHANNEL#telegram:123456"
    identity = ExternalTable(
        partition_pages=[
            {
                "Items": [
                    {"PK": f"USER#{USER}", "SK": "PROFILE"},
                    {"PK": f"USER#{USER}", "SK": channel_pk},
                ]
            }
        ],
        index_pages=[
            {
                "Items": [
                    {"PK": channel_pk, "SK": "PROFILE", "userId": USER},
                    {"PK": "BIND#ABCDEF12", "SK": "BIND", "userId": USER},
                ]
            }
        ],
    )
    event_id = "po1_" + "a" * 64
    ledger = ExternalTable(
        index_pages=[{"Items": [{"eventId": event_id, "userId": USER}]}]
    )
    control = ExternalTable(
        index_pages=[
            {
                "Items": [
                    {"PK": "SESSION#" + "a" * 64, "SK": "SESSION", "userId": USER},
                    {"PK": "CONNECT#" + "b" * 64, "SK": "CONNECT", "userId": USER},
                    {"PK": "OAUTHSTATE#" + "c" * 64, "SK": "OAUTHSTATE", "userId": USER},
                    {"PK": "OAUTH_STATE#" + "d" * 64, "SK": "OAUTH_STATE", "userId": USER},
                    {
                        "PK": "DELETION#"
                        + hashlib.sha256(USER.encode("utf-8")).hexdigest(),
                        "SK": "DELETION",
                        "userId": USER,
                    },
                ]
            }
        ]
    )

    DynamoUserFootprintStore(
        control_table=control,
        identity_table=identity,
        message_ledger_table=ledger,
    ).delete_user_records(USER)

    assert identity.partition_calls[0]["ConsistentRead"] is True
    assert identity.partition_calls[0]["ExpressionAttributeValues"] == {
        ":pk": f"USER#{USER}"
    }
    assert "ConsistentRead" not in identity.index_calls[0]
    assert identity.index_calls[0]["IndexName"] == "userId-index"
    identity_keys = [call["Key"] for call in identity.deleted]
    assert {"PK": f"USER#{USER}", "SK": "PROFILE"} in identity_keys
    assert {"PK": f"USER#{USER}", "SK": channel_pk} in identity_keys
    assert {"PK": channel_pk, "SK": "PROFILE"} in identity_keys
    assert {"PK": "ALLOW#telegram:123456", "SK": "ALLOW"} in identity_keys
    assert {"PK": "BIND#ABCDEF12", "SK": "BIND"} in identity_keys
    assert ledger.deleted == [
        {
            "Key": {"eventId": event_id},
            "ConditionExpression": "userId=:userId",
            "ExpressionAttributeValues": {":userId": USER},
        }
    ]
    assert [call["Key"] for call in control.deleted] == [
        {"PK": "SESSION#" + "a" * 64, "SK": "SESSION"},
        {"PK": "CONNECT#" + "b" * 64, "SK": "CONNECT"},
        {"PK": "OAUTHSTATE#" + "c" * 64, "SK": "OAUTHSTATE"},
        {"PK": "OAUTH_STATE#" + "d" * 64, "SK": "OAUTH_STATE"},
    ]


def test_external_footprint_pass_is_bounded_and_resumes_from_the_start():
    cursor = {"PK": f"USER#{USER}", "SK": "PROFILE"}
    identity = ExternalTable(
        partition_pages=[
            {
                "Items": [{"PK": f"USER#{USER}", "SK": "PROFILE"}],
                "LastEvaluatedKey": cursor,
            }
        ]
    )
    store = DynamoUserFootprintStore(
        identity_table=identity,
        message_ledger_table=ExternalTable(),
        page_size=1,
        max_pages=1,
    )

    with pytest.raises(DataDeletionPending, match="another pass"):
        store.delete_user_records(USER)

    assert [call["Key"] for call in identity.deleted] == [
        {"PK": f"USER#{USER}", "SK": "PROFILE"}
    ]


def test_external_footprint_rejects_cross_tenant_index_row_before_deleting_it():
    identity = ExternalTable(
        index_pages=[
            {
                "Items": [
                    {
                        "PK": "CHANNEL#telegram:123456",
                        "SK": "PROFILE",
                        "userId": "other-user",
                    }
                ]
            }
        ]
    )

    with pytest.raises(DataAdapterError, match="binding"):
        DynamoUserFootprintStore(
            identity_table=identity,
            message_ledger_table=ExternalTable(),
        ).delete_user_records(USER)

    assert identity.deleted == []


def test_stale_historical_backref_never_deletes_remapped_channel_allowlist():
    channel_pk = "CHANNEL#telegram:123456"
    forward_key = {"PK": channel_pk, "SK": "PROFILE"}
    identity = ExternalTable(
        partition_pages=[
            {
                "Items": [
                    {"PK": f"USER#{USER}", "SK": channel_pk},
                ]
            }
        ],
        conditional_fail_keys=[forward_key],
        strong_items=[
            {
                "PK": channel_pk,
                "SK": "PROFILE",
                "userId": "user_remapped",
                "channel": "telegram",
                "channelUserId": "123456",
            }
        ],
    )

    DynamoUserFootprintStore(
        identity_table=identity,
        message_ledger_table=ExternalTable(),
    ).delete_user_records(USER)

    deleted_keys = [call["Key"] for call in identity.deleted]
    assert forward_key not in deleted_keys
    assert {"PK": "ALLOW#telegram:123456", "SK": "ALLOW"} not in deleted_keys
    assert {"PK": f"USER#{USER}", "SK": channel_pk} in deleted_keys


def test_missing_backref_index_fallback_deletes_owned_channel_allowlist():
    channel_pk = "CHANNEL#telegram:123456"
    identity = ExternalTable(
        partition_pages=[{"Items": [{"PK": f"USER#{USER}", "SK": "PROFILE"}]}],
        index_pages=[
            {
                "Items": [
                    {"PK": channel_pk, "SK": "PROFILE", "userId": USER},
                ]
            }
        ],
    )

    DynamoUserFootprintStore(
        identity_table=identity,
        message_ledger_table=ExternalTable(),
    ).delete_user_records(USER)

    deleted_keys = [call["Key"] for call in identity.deleted]
    assert len(identity.transactions) == 1
    assert {"PK": channel_pk, "SK": "PROFILE"} in deleted_keys
    assert {"PK": "ALLOW#telegram:123456", "SK": "ALLOW"} in deleted_keys


def test_missing_backref_index_fallback_preserves_remapped_channel_allowlist():
    channel_pk = "CHANNEL#telegram:123456"
    forward_key = {"PK": channel_pk, "SK": "PROFILE"}
    identity = ExternalTable(
        index_pages=[
            {
                "Items": [
                    {"PK": channel_pk, "SK": "PROFILE", "userId": USER},
                ]
            }
        ],
        conditional_fail_keys=[forward_key],
        strong_items=[
            {
                "PK": channel_pk,
                "SK": "PROFILE",
                "userId": "user_remapped",
                "channel": "telegram",
                "channelUserId": "123456",
            }
        ],
    )

    DynamoUserFootprintStore(
        identity_table=identity,
        message_ledger_table=ExternalTable(),
    ).delete_user_records(USER)

    deleted_keys = [call["Key"] for call in identity.deleted]
    assert forward_key not in deleted_keys
    assert {"PK": "ALLOW#telegram:123456", "SK": "ALLOW"} not in deleted_keys


def test_identity_deletion_atomically_fences_channel_and_removes_owned_mapping_invite():
    channel_key = "telegram:123456"
    channel_pk = f"CHANNEL#{channel_key}"
    identity = ExternalTable(
        partition_pages=[
            {
                "Items": [
                    {"PK": f"USER#{USER}", "SK": channel_pk},
                ]
            }
        ],
    )

    DynamoUserFootprintStore(
        identity_table=identity,
        message_ledger_table=ExternalTable(),
    ).delete_user_records(USER)

    user_digest = hashlib.sha256(USER.encode()).hexdigest()
    assert identity.put_items[0]["Item"] == {
        "PK": f"USER_TOMBSTONE#{user_digest}",
        "SK": "TOMBSTONE",
        "markerVersion": "1",
    }
    transaction = identity.transactions[0]["TransactItems"]
    assert len(transaction) == 3
    channel_digest = hashlib.sha256(channel_key.encode()).hexdigest()
    assert transaction[0]["Put"]["Item"] == {
        "PK": {"S": f"CHANNEL_TOMBSTONE#{channel_digest}"},
        "SK": {"S": "TOMBSTONE"},
        "markerVersion": {"S": "1"},
    }
    assert transaction[1]["Delete"]["Key"] == {
        "PK": {"S": channel_pk},
        "SK": {"S": "PROFILE"},
    }
    assert transaction[1]["Delete"]["ConditionExpression"] == "userId = :userId"
    assert transaction[2]["Delete"]["Key"] == {
        "PK": {"S": f"ALLOW#{channel_key}"},
        "SK": {"S": "ALLOW"},
    }
    assert channel_key not in str(transaction[0])
    assert USER not in str(identity.put_items[0]["Item"])
    assert identity.events.index("put") < identity.events.index("partition-query")
    assert identity.events.index("partition-query") < identity.events.index(
        "transaction"
    )


def test_failed_channel_fence_transaction_cannot_strand_a_reusable_invite():
    channel_key = "telegram:123456"
    channel_pk = f"CHANNEL#{channel_key}"
    identity = ExternalTable(
        partition_pages=[
            {"Items": [{"PK": f"USER#{USER}", "SK": channel_pk}]}
        ],
        strong_items=[
            {
                "PK": channel_pk,
                "SK": "PROFILE",
                "userId": USER,
                "channel": "telegram",
                "channelUserId": "123456",
            }
        ],
        transaction_error=RuntimeError("injected ALLOW delete failure"),
    )

    with pytest.raises(DataAdapterError, match="channel fence.*uncertain"):
        DynamoUserFootprintStore(
            identity_table=identity,
            message_ledger_table=ExternalTable(),
        ).delete_user_records(USER)

    deleted_keys = [call["Key"] for call in identity.deleted]
    assert {"PK": channel_pk, "SK": "PROFILE"} not in deleted_keys
    assert {"PK": f"ALLOW#{channel_key}", "SK": "ALLOW"} not in deleted_keys
    assert (
        f"CHANNEL_TOMBSTONE#{hashlib.sha256(channel_key.encode()).hexdigest()}",
        "TOMBSTONE",
    ) not in identity.strong_items


def test_ambiguous_committed_channel_fence_is_proven_by_strong_reads_and_retries():
    channel_key = "telegram:123456"
    channel_pk = f"CHANNEL#{channel_key}"
    identity = ExternalTable(
        partition_pages=[
            {"Items": [{"PK": f"USER#{USER}", "SK": channel_pk}]}
        ],
        transaction_error_after_commit=RuntimeError("response lost"),
    )

    DynamoUserFootprintStore(
        identity_table=identity,
        message_ledger_table=ExternalTable(),
    ).delete_user_records(USER)

    deleted_keys = [call["Key"] for call in identity.deleted]
    assert len(identity.transactions) == 1
    assert {"PK": channel_pk, "SK": "PROFILE"} in deleted_keys
    assert {"PK": f"ALLOW#{channel_key}", "SK": "ALLOW"} in deleted_keys
    assert all(call["ConsistentRead"] is True for call in identity.get_calls)
