"""Exact-tenant S3 and DynamoDB adapters for export and deletion."""

from __future__ import annotations

from collections import Counter
import hashlib
import re
import time
from typing import Mapping

from actions.models import EffectReceipt


_USER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_ACTION_ID = re.compile(r"[A-Za-z0-9_-]{8,128}")
MAX_WORKSPACE_FILES = 1_000
MAX_WORKSPACE_LIST_PAGES = 20
MAX_WORKSPACE_DELETE_PAGES = 1_000
MAX_WORKSPACE_ENTRY_BYTES = 5 * 1024 * 1024
MAX_WORKSPACE_TOTAL_BYTES = 50 * 1024 * 1024
MAX_USER_RECORD_ITEMS = 1_000
MAX_USER_RECORD_PAGES = 20
USER_RECORD_PAGE_SIZE = 100
EXTERNAL_DELETE_PAGE_SIZE = 100
EXTERNAL_DELETE_MAX_PAGES = 20
_EVENT_ID = re.compile(r"po1_[0-9a-f]{64}")
_CHANNEL_RECORD = re.compile(r"CHANNEL#(?:telegram|slack|feishu):[^\x00-\x1f]{1,256}")
_BIND_RECORD = re.compile(r"BIND#[A-F0-9]{8}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_IDENTITY_TABLE = re.compile(r"[A-Za-z0-9_.-]{3,255}")


class DataAdapterError(RuntimeError):
    pass


class DataDeletionPending(DataAdapterError):
    """A bounded pass made progress but more exact user rows remain."""


def _conditional_failure(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    return bool(
        isinstance(response, Mapping)
        and response.get("Error", {}).get("Code")
        == "ConditionalCheckFailedException"
    )


def _user(value: object) -> str:
    if not isinstance(value, str) or _USER.fullmatch(value) is None:
        raise ValueError("user identity is invalid")
    return value


class S3WorkspaceStore:
    def __init__(self, client, *, bucket_name: str) -> None:
        if not isinstance(bucket_name, str) or not bucket_name or len(bucket_name) > 255:
            raise ValueError("workspace bucket name is invalid")
        self._client = client
        self._bucket = bucket_name

    @staticmethod
    def _prefix(user_id: str) -> str:
        return f"{_user(user_id)}/"

    @classmethod
    def _files_prefix(cls, user_id: str) -> str:
        return f"{cls._prefix(user_id)}files/"

    def workspace_files(self, user_id: str) -> dict[str, bytes]:
        prefix = self._files_prefix(user_id)
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
                if (
                    not isinstance(key, str)
                    or not key.startswith(prefix)
                    or key == prefix
                    or isinstance(size, bool)
                    or not isinstance(size, int)
                    or size < 0
                    or size > MAX_WORKSPACE_ENTRY_BYTES
                    or len(result) >= MAX_WORKSPACE_FILES
                ):
                    raise DataAdapterError("workspace object violates export limits")
                body = self._client.get_object(Bucket=self._bucket, Key=key).get("Body")
                content = body.read(MAX_WORKSPACE_ENTRY_BYTES + 1) if hasattr(body, "read") else None
                if not isinstance(content, bytes) or len(content) != size:
                    raise DataAdapterError("workspace object changed during export")
                total += len(content)
                if total > MAX_WORKSPACE_TOTAL_BYTES:
                    raise DataAdapterError("workspace export exceeds total limit")
                result[key[len(prefix):]] = content
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

    def delete_namespace(self, user_id: str) -> None:
        prefix = self._prefix(user_id)
        self._abort_multipart_uploads(prefix)
        self._delete_object_versions(prefix)

    def _abort_multipart_uploads(self, prefix: str) -> None:
        key_marker = None
        upload_id_marker = None
        seen_uploads: set[tuple[str, str]] = set()
        seen_cursors: set[tuple[str, str]] = set()

        for _page in range(MAX_WORKSPACE_DELETE_PAGES):
            request = {
                "Bucket": self._bucket,
                "Prefix": prefix,
                "MaxUploads": 1_000,
            }
            if key_marker:
                request["KeyMarker"] = key_marker
            if upload_id_marker:
                request["UploadIdMarker"] = upload_id_marker
            response = self._client.list_multipart_uploads(**request)
            if not isinstance(response, Mapping):
                raise DataAdapterError("workspace multipart listing failed")
            if (
                ("Bucket" in response and response.get("Bucket") != self._bucket)
                or ("Prefix" in response and response.get("Prefix") != prefix)
                or (
                    "MaxUploads" in response
                    and response.get("MaxUploads") != 1_000
                )
            ):
                raise DataAdapterError("workspace multipart listing is ambiguous")
            uploads = response.get("Uploads", [])
            if not isinstance(uploads, list):
                raise DataAdapterError("workspace multipart listing is invalid")

            page_uploads: list[tuple[str, str]] = []
            for item in uploads:
                key = item.get("Key") if isinstance(item, Mapping) else None
                upload_id = (
                    item.get("UploadId") if isinstance(item, Mapping) else None
                )
                if (
                    not isinstance(key, str)
                    or not key.startswith(prefix)
                    or not isinstance(upload_id, str)
                    or not upload_id
                ):
                    raise DataAdapterError(
                        "workspace multipart upload escaped its namespace"
                    )
                candidate = (key, upload_id)
                if candidate in seen_uploads:
                    raise DataAdapterError(
                        "workspace multipart upload listing is ambiguous"
                    )
                seen_uploads.add(candidate)
                page_uploads.append(candidate)

            truncated = response.get("IsTruncated")
            if not isinstance(truncated, bool):
                raise DataAdapterError("workspace multipart pagination is invalid")
            if not truncated and (
                response.get("NextKeyMarker") is not None
                or response.get("NextUploadIdMarker") is not None
            ):
                raise DataAdapterError("workspace multipart pagination is invalid")
            next_cursor = None
            if truncated:
                next_key = response.get("NextKeyMarker")
                next_upload_id = response.get("NextUploadIdMarker")
                next_cursor = (next_key, next_upload_id)
                if (
                    not isinstance(next_key, str)
                    or not next_key.startswith(prefix)
                    or not isinstance(next_upload_id, str)
                    or not next_upload_id
                    or next_cursor in seen_cursors
                ):
                    raise DataAdapterError(
                        "workspace multipart pagination is invalid"
                    )
                seen_cursors.add(next_cursor)

            for key, upload_id in page_uploads:
                aborted = self._client.abort_multipart_upload(
                    Bucket=self._bucket,
                    Key=key,
                    UploadId=upload_id,
                )
                if not isinstance(aborted, Mapping):
                    raise DataAdapterError("workspace multipart abort was ambiguous")

            if not truncated:
                return
            assert next_cursor is not None
            key_marker, upload_id_marker = next_cursor
        raise DataAdapterError("workspace multipart listing exceeded its page bound")

    def _delete_object_versions(self, prefix: str) -> None:
        key_marker = None
        version_marker = None
        seen_objects: set[tuple[str, str]] = set()
        seen_cursors: set[tuple[str, str]] = set()

        for _page in range(MAX_WORKSPACE_DELETE_PAGES):
            request = {"Bucket": self._bucket, "Prefix": prefix, "MaxKeys": 1_000}
            if key_marker:
                request["KeyMarker"] = key_marker
            if version_marker:
                request["VersionIdMarker"] = version_marker
            response = self._client.list_object_versions(**request)
            if not isinstance(response, Mapping):
                raise DataAdapterError("workspace version listing failed")
            if (
                ("Name" in response and response.get("Name") != self._bucket)
                or ("Prefix" in response and response.get("Prefix") != prefix)
                or ("MaxKeys" in response and response.get("MaxKeys") != 1_000)
            ):
                raise DataAdapterError("workspace version listing is ambiguous")
            objects: list[dict[str, str]] = []
            for group in (response.get("Versions", []), response.get("DeleteMarkers", [])):
                if not isinstance(group, list):
                    raise DataAdapterError("workspace version listing is invalid")
                for item in group:
                    key = item.get("Key") if isinstance(item, Mapping) else None
                    version = item.get("VersionId") if isinstance(item, Mapping) else None
                    if (
                        not isinstance(key, str)
                        or not key.startswith(prefix)
                        or not isinstance(version, str)
                        or not version
                    ):
                        raise DataAdapterError("workspace deletion escaped its namespace")
                    candidate = (key, version)
                    if candidate in seen_objects:
                        raise DataAdapterError(
                            "workspace version listing is ambiguous"
                        )
                    seen_objects.add(candidate)
                    objects.append({"Key": key, "VersionId": version})
            for start in range(0, len(objects), 1_000):
                batch = objects[start:start + 1_000]
                if not batch:
                    continue
                deleted = self._client.delete_objects(
                    Bucket=self._bucket,
                    Delete={"Objects": batch, "Quiet": False},
                )
                if not isinstance(deleted, Mapping):
                    raise DataAdapterError("workspace version deletion was ambiguous")
                errors = deleted.get("Errors", [])
                evidence = deleted.get("Deleted")
                if (
                    not isinstance(errors, list)
                    or errors
                    or not isinstance(evidence, list)
                ):
                    raise DataAdapterError("workspace version deletion was incomplete")
                evidence_pairs = []
                for item in evidence:
                    if not isinstance(item, Mapping):
                        raise DataAdapterError(
                            "workspace version deletion was incomplete"
                        )
                    key = item.get("Key")
                    version = item.get("VersionId")
                    if not isinstance(key, str) or not isinstance(version, str):
                        raise DataAdapterError(
                            "workspace version deletion was incomplete"
                        )
                    evidence_pairs.append((key, version))
                requested_pairs = [
                    (item["Key"], item["VersionId"]) for item in batch
                ]
                if Counter(evidence_pairs) != Counter(requested_pairs):
                    raise DataAdapterError(
                        "workspace version deletion evidence did not match"
                    )

            truncated = response.get("IsTruncated")
            if not isinstance(truncated, bool):
                raise DataAdapterError("workspace deletion pagination is invalid")
            if not truncated and (
                response.get("NextKeyMarker") is not None
                or response.get("NextVersionIdMarker") is not None
            ):
                raise DataAdapterError("workspace deletion pagination is invalid")
            if not truncated:
                return
            next_key = response.get("NextKeyMarker")
            next_version = response.get("NextVersionIdMarker")
            cursor = (next_key, next_version)
            if (
                not isinstance(next_key, str)
                or not next_key.startswith(prefix)
                or not isinstance(next_version, str)
                or not next_version
                or cursor in seen_cursors
            ):
                raise DataAdapterError("workspace deletion pagination is invalid")
            seen_cursors.add(cursor)
            key_marker, version_marker = cursor
        raise DataAdapterError("workspace deletion exceeded its page bound")


class DynamoUserDataStore:
    def __init__(self, table, *, now=None) -> None:
        self._table = table
        self._now = now or (lambda: int(time.time()))

    def _epoch(self) -> int:
        value = self._now()
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DataAdapterError("user record retention clock is invalid")
        return value

    def _items(self, user_id: str) -> list[Mapping]:
        user_id = _user(user_id)
        partition_key = f"USER#{user_id}"
        items: list[Mapping] = []
        start_key = None
        seen_cursors: set[tuple[str, str]] = set()

        for _page in range(MAX_USER_RECORD_PAGES):
            request = {
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": {":pk": partition_key},
                "ConsistentRead": True,
                "Limit": USER_RECORD_PAGE_SIZE,
            }
            if start_key is not None:
                request["ExclusiveStartKey"] = start_key
            response = self._table.query(**request)
            page_items = (
                response.get("Items") if isinstance(response, Mapping) else None
            )
            if (
                not isinstance(page_items, list)
                or any(not isinstance(item, Mapping) for item in page_items)
            ):
                raise DataAdapterError("user record query failed")
            if len(page_items) > USER_RECORD_PAGE_SIZE:
                raise DataAdapterError("user record query exceeded its item bound")
            if len(items) + len(page_items) > MAX_USER_RECORD_ITEMS:
                raise DataAdapterError("user record query exceeded its item bound")
            items.extend(page_items)

            cursor = response.get("LastEvaluatedKey")
            if cursor is None:
                return items
            if (
                not isinstance(cursor, Mapping)
                or set(cursor) != {"PK", "SK"}
                or cursor.get("PK") != partition_key
                or not isinstance(cursor.get("SK"), str)
                or not cursor["SK"]
            ):
                raise DataAdapterError("user record pagination is invalid")
            cursor_signature = (partition_key, cursor["SK"])
            if cursor_signature in seen_cursors:
                raise DataAdapterError("user record pagination is invalid")
            seen_cursors.add(cursor_signature)
            start_key = {"PK": partition_key, "SK": cursor["SK"]}

        raise DataAdapterError("user record query exceeded its page bound")

    def _delete_partition_records(
        self,
        user_id: str,
        *,
        sk_prefix: str | None = None,
        preserve_sks: frozenset[str] = frozenset(),
    ) -> None:
        """Delete one bounded pass and restart safely on reconciliation."""

        user_id = _user(user_id)
        partition_key = f"USER#{user_id}"
        start_key = None
        seen_cursors: set[tuple[str, str]] = set()
        for _page in range(MAX_USER_RECORD_PAGES):
            expression = "PK = :pk"
            values = {":pk": partition_key}
            if sk_prefix is not None:
                expression += " AND begins_with(SK, :prefix)"
                values[":prefix"] = sk_prefix
            request = {
                "KeyConditionExpression": expression,
                "ExpressionAttributeValues": values,
                "ConsistentRead": True,
                "Limit": USER_RECORD_PAGE_SIZE,
            }
            if start_key is not None:
                request["ExclusiveStartKey"] = start_key
            response = self._table.query(**request)
            page_items = (
                response.get("Items") if isinstance(response, Mapping) else None
            )
            if (
                not isinstance(page_items, list)
                or len(page_items) > USER_RECORD_PAGE_SIZE
                or any(not isinstance(item, Mapping) for item in page_items)
            ):
                raise DataAdapterError("user record deletion query failed")
            keys: list[tuple[dict[str, str], str | None]] = []
            for item in page_items:
                pk = item.get("PK")
                sk = item.get("SK")
                if (
                    pk != partition_key
                    or not isinstance(sk, str)
                    or not sk
                    or len(sk) > 512
                    or (sk_prefix is not None and not sk.startswith(sk_prefix))
                ):
                    raise DataAdapterError("user record deletion binding is invalid")
                if sk not in preserve_sks:
                    keys.append({"PK": pk, "SK": sk})

            cursor = response.get("LastEvaluatedKey")
            if cursor is not None:
                if (
                    not isinstance(cursor, Mapping)
                    or set(cursor) != {"PK", "SK"}
                    or cursor.get("PK") != partition_key
                    or not isinstance(cursor.get("SK"), str)
                    or not cursor["SK"]
                ):
                    raise DataAdapterError("user record deletion pagination is invalid")
                signature = (partition_key, cursor["SK"])
                if signature in seen_cursors:
                    raise DataAdapterError("user record deletion pagination is invalid")
                seen_cursors.add(signature)

            # Every key and the next cursor are validated before this page makes
            # progress. A later bounded pass starts at the now-smaller prefix.
            for key in keys:
                try:
                    self._table.delete_item(Key=key)
                except Exception as error:
                    raise DataAdapterError("user record deletion is uncertain") from error
            if cursor is None:
                return
            start_key = dict(cursor)
        raise DataDeletionPending("user record deletion requires another pass")

    def records_for_user(self, user_id: str) -> dict[str, list[object]]:
        user_id = _user(user_id)
        now = self._epoch()
        records = {"memory": [], "schedules": [], "receipts": []}
        for item in self._items(user_id):
            sk = item.get("SK")
            if not isinstance(sk, str):
                raise DataAdapterError("user record key is invalid")
            ttl = item.get("ttl")
            if ttl is not None:
                if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
                    raise DataAdapterError("user record TTL is invalid")
                if ttl <= now:
                    continue
            if sk.startswith("MEMORY#"):
                records["memory"].append({key: value for key, value in item.items() if key not in {"PK", "SK"}})
            elif sk.startswith("SCHEDULE#"):
                records["schedules"].append({key: value for key, value in item.items() if key not in {"PK", "SK"}})
            elif sk.startswith("ACTION#") and item.get("state") == "CONFIRMED":
                try:
                    action_id = item.get("actionId")
                    args = item.get("args")
                    receipt = EffectReceipt.from_record(item.get("effectReceipt"))
                    if (
                        not isinstance(action_id, str)
                        or _ACTION_ID.fullmatch(action_id) is None
                        or sk != f"ACTION#{action_id}"
                        or item.get("PK") != f"USER#{user_id}"
                        or item.get("userId") != user_id
                        or not isinstance(args, Mapping)
                        or receipt.message_id != item.get("messageId")
                        or receipt.connection_id != item.get("connectionId")
                        or receipt.account_email != item.get("accountEmail")
                        or receipt.sender_address != item.get("senderAddress")
                        or receipt.recipient != args.get("to")
                        or receipt.payload_hash != item.get("payloadHash")
                    ):
                        raise ValueError("receipt is not bound to the action")
                except (TypeError, ValueError):
                    raise DataAdapterError(
                        "confirmed action receipt is invalid"
                    ) from None
                records["receipts"].append(receipt.record())
        return records

    def revoke_all(self, user_id: str) -> None:
        self._delete_partition_records(user_id, sk_prefix="CONNECTION#")

    def delete_user_records(self, user_id: str) -> None:
        self._delete_partition_records(user_id)


class DynamoUserFootprintStore:
    """Bounded, repeatable purge across legacy identity and message ledgers.

    The identity table uses both an exact USER# partition (needed for historical
    back-references that predate the GSI attribute) and a userId-index (needed
    for CHANNEL#/BIND# rows outside that partition). The message ledger uses a
    separate userId-index because its primary key is the immutable event ID.
    """

    INDEX_NAME = "userId-index"

    def __init__(
        self,
        *,
        identity_table,
        message_ledger_table,
        control_table=None,
        page_size: int = EXTERNAL_DELETE_PAGE_SIZE,
        max_pages: int = EXTERNAL_DELETE_MAX_PAGES,
    ) -> None:
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= EXTERNAL_DELETE_PAGE_SIZE
            or isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= EXTERNAL_DELETE_MAX_PAGES
        ):
            raise ValueError("external deletion bounds are invalid")
        self._identity = identity_table
        identity_table_name = getattr(identity_table, "name", None)
        if (
            not isinstance(identity_table_name, str)
            or _IDENTITY_TABLE.fullmatch(identity_table_name) is None
        ):
            raise ValueError("identity table name is invalid")
        self._identity_table_name = identity_table_name
        self._ledger = message_ledger_table
        self._control = control_table
        self._page_size = page_size
        self._max_pages = max_pages

    @staticmethod
    def _string_item(item: Mapping[str, str]) -> dict:
        return {key: {"S": value} for key, value in item.items()}

    @staticmethod
    def _user_tombstone(user_id: str) -> dict[str, str]:
        digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
        return {
            "PK": f"USER_TOMBSTONE#{digest}",
            "SK": "TOMBSTONE",
            "markerVersion": "1",
        }

    @staticmethod
    def _channel_tombstone(channel_pk: str) -> dict[str, str]:
        if _CHANNEL_RECORD.fullmatch(channel_pk) is None:
            raise DataAdapterError("identity channel binding is invalid")
        digest = hashlib.sha256(channel_pk[8:].encode("utf-8")).hexdigest()
        return {
            "PK": f"CHANNEL_TOMBSTONE#{digest}",
            "SK": "TOMBSTONE",
            "markerVersion": "1",
        }

    def _strong_identity_item(self, key: Mapping[str, str]) -> Mapping | None:
        try:
            response = self._identity.get_item(
                Key=dict(key),
                ConsistentRead=True,
            )
        except Exception as error:
            raise DataAdapterError("identity fence proof read failed") from error
        if not isinstance(response, Mapping):
            raise DataAdapterError("identity fence proof read failed")
        item = response.get("Item")
        if item is None:
            return None
        if not isinstance(item, Mapping):
            raise DataAdapterError("identity fence proof is invalid")
        return item

    def _ensure_user_tombstone(self, user_id: str) -> None:
        """Establish the permanent same-table fence before any identity scan."""
        marker = self._user_tombstone(user_id)
        try:
            self._identity.put_item(Item=marker)
        except Exception:
            # A lost success response is allowed only when a strong read proves
            # the exact marker. No purge begins without that proof.
            pass
        if self._strong_identity_item(
            {"PK": marker["PK"], "SK": marker["SK"]}
        ) != marker:
            raise DataAdapterError("identity user tombstone is uncertain")

    def _fence_owned_channel(self, channel_pk: str, user_id: str) -> bool:
        """Atomically tombstone a channel and remove its mapping plus invite.

        Returns False when a strong read proves the channel was remapped to a
        different user. Every other ambiguous outcome remains retryable.
        """
        marker = self._channel_tombstone(channel_pk)
        channel_key = channel_pk[8:]
        forward_key = {"PK": channel_pk, "SK": "PROFILE"}
        allow_key = {"PK": f"ALLOW#{channel_key}", "SK": "ALLOW"}
        transaction = [
            {
                "Put": {
                    "TableName": self._identity_table_name,
                    "Item": self._string_item(marker),
                }
            },
            {
                "Delete": {
                    "TableName": self._identity_table_name,
                    "Key": self._string_item(forward_key),
                    "ConditionExpression": "userId = :userId",
                    "ExpressionAttributeValues": {":userId": {"S": user_id}},
                }
            },
            {
                "Delete": {
                    "TableName": self._identity_table_name,
                    "Key": self._string_item(allow_key),
                }
            },
        ]
        try:
            self._identity.meta.client.transact_write_items(
                TransactItems=transaction
            )
            return True
        except Exception:
            # DynamoDB transactions are atomic, but the response can be lost.
            # Strong reads prove either committed success or a safe remap.
            forward = self._strong_identity_item(forward_key)
            tombstone = self._strong_identity_item(
                {"PK": marker["PK"], "SK": marker["SK"]}
            )
            if forward is None and tombstone == marker:
                return True
            if forward is not None:
                if (
                    forward.get("PK") != channel_pk
                    or forward.get("SK") != "PROFILE"
                    or not isinstance(forward.get("userId"), str)
                    or _USER.fullmatch(forward["userId"]) is None
                ):
                    raise DataAdapterError("identity channel fence proof is invalid")
                if forward["userId"] != user_id:
                    return False
            raise DataAdapterError("identity channel fence is uncertain")

    @staticmethod
    def _identity_cursor(value: object, *, user_id: str, indexed: bool) -> dict:
        expected = {"PK", "SK", "userId"} if indexed else {"PK", "SK"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise DataAdapterError("identity deletion pagination is invalid")
        pk = value.get("PK")
        sk = value.get("SK")
        if (
            not isinstance(pk, str)
            or not pk
            or len(pk) > 512
            or not isinstance(sk, str)
            or not sk
            or len(sk) > 512
            or (indexed and value.get("userId") != user_id)
            or (not indexed and pk != f"USER#{user_id}")
        ):
            raise DataAdapterError("identity deletion pagination is invalid")
        return dict(value)

    @staticmethod
    def _ledger_cursor(value: object, *, user_id: str) -> dict:
        if (
            not isinstance(value, Mapping)
            or set(value) != {"eventId", "userId"}
            or value.get("userId") != user_id
            or not isinstance(value.get("eventId"), str)
            or _EVENT_ID.fullmatch(value["eventId"]) is None
        ):
            raise DataAdapterError("message-ledger deletion pagination is invalid")
        return dict(value)

    @staticmethod
    def _control_cursor(value: object, *, user_id: str) -> dict:
        if (
            not isinstance(value, Mapping)
            or set(value) != {"PK", "SK", "userId"}
            or value.get("userId") != user_id
            or not isinstance(value.get("PK"), str)
            or not value["PK"]
            or len(value["PK"]) > 512
            or not isinstance(value.get("SK"), str)
            or not value["SK"]
            or len(value["SK"]) > 512
        ):
            raise DataAdapterError("control-record deletion pagination is invalid")
        return dict(value)

    @staticmethod
    def _delete_bound(table, *, key: Mapping, user_id: str) -> bool:
        try:
            table.delete_item(
                Key=dict(key),
                ConditionExpression="userId=:userId",
                ExpressionAttributeValues={":userId": user_id},
            )
            return True
        except Exception as error:
            # A remapped/reused key is not this user's data and must survive.
            if _conditional_failure(error):
                return False
            raise DataAdapterError("external user-record deletion is uncertain") from error

    def _delete_identity_partition(self, user_id: str) -> None:
        partition = f"USER#{user_id}"
        cursor = None
        seen = set()
        for _page in range(self._max_pages):
            request = {
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": {":pk": partition},
                "ConsistentRead": True,
                "Limit": self._page_size,
            }
            if cursor is not None:
                request["ExclusiveStartKey"] = cursor
            response = self._identity.query(**request)
            items = response.get("Items") if isinstance(response, Mapping) else None
            if (
                not isinstance(items, list)
                or len(items) > self._page_size
                or any(not isinstance(item, Mapping) for item in items)
            ):
                raise DataAdapterError("identity user-partition query failed")

            validated: list[tuple[dict[str, str], str | None]] = []
            for item in items:
                pk = item.get("PK")
                sk = item.get("SK")
                if (
                    pk != partition
                    or not isinstance(sk, str)
                    or not sk
                    or len(sk) > 512
                ):
                    raise DataAdapterError("identity user-partition binding is invalid")
                channel_pk = sk if sk.startswith("CHANNEL#") else None
                if channel_pk is not None and _CHANNEL_RECORD.fullmatch(channel_pk) is None:
                    raise DataAdapterError("identity channel back-reference is invalid")
                validated.append(({"PK": pk, "SK": sk}, channel_pk))

            # Validate the complete page before any side effect.
            for key, channel_pk in validated:
                if channel_pk is not None:
                    self._fence_owned_channel(channel_pk, user_id)
                try:
                    self._identity.delete_item(Key=key)
                except Exception as error:
                    raise DataAdapterError(
                        "identity user-partition deletion is uncertain"
                    ) from error

            raw_cursor = response.get("LastEvaluatedKey")
            if raw_cursor is None:
                return
            cursor = self._identity_cursor(raw_cursor, user_id=user_id, indexed=False)
            signature = (cursor["PK"], cursor["SK"])
            if signature in seen:
                raise DataAdapterError("identity deletion pagination is invalid")
            seen.add(signature)
        raise DataDeletionPending("identity user-partition deletion requires another pass")

    def _delete_identity_index(self, user_id: str) -> None:
        cursor = None
        seen = set()
        for _page in range(self._max_pages):
            request = {
                "IndexName": self.INDEX_NAME,
                "KeyConditionExpression": "userId = :userId",
                "ExpressionAttributeValues": {":userId": user_id},
                "ProjectionExpression": "PK, SK, userId",
                "Limit": self._page_size,
            }
            if cursor is not None:
                request["ExclusiveStartKey"] = cursor
            response = self._identity.query(**request)
            items = response.get("Items") if isinstance(response, Mapping) else None
            if (
                not isinstance(items, list)
                or len(items) > self._page_size
                or any(not isinstance(item, Mapping) for item in items)
            ):
                raise DataAdapterError("identity user-index query failed")
            keys: list[dict[str, str]] = []
            for item in items:
                pk = item.get("PK")
                sk = item.get("SK")
                allowed = (
                    item.get("userId") == user_id
                    and isinstance(pk, str)
                    and isinstance(sk, str)
                    and (
                        (pk == f"USER#{user_id}" and bool(sk) and len(sk) <= 512)
                        or (_CHANNEL_RECORD.fullmatch(pk) is not None and sk == "PROFILE")
                        or (_BIND_RECORD.fullmatch(pk) is not None and sk == "BIND")
                    )
                )
                if not allowed:
                    raise DataAdapterError("identity user-index binding is invalid")
                channel_pk = pk if _CHANNEL_RECORD.fullmatch(pk) is not None else None
                keys.append(({"PK": pk, "SK": sk}, channel_pk))
            for key, channel_pk in keys:
                if channel_pk is not None:
                    self._fence_owned_channel(channel_pk, user_id)
                else:
                    self._delete_bound(
                        self._identity,
                        key=key,
                        user_id=user_id,
                    )

            raw_cursor = response.get("LastEvaluatedKey")
            if raw_cursor is None:
                return
            cursor = self._identity_cursor(raw_cursor, user_id=user_id, indexed=True)
            signature = (cursor["userId"], cursor["PK"], cursor["SK"])
            if signature in seen:
                raise DataAdapterError("identity deletion pagination is invalid")
            seen.add(signature)
        raise DataDeletionPending("identity user-index deletion requires another pass")

    def _delete_message_ledger(self, user_id: str) -> None:
        cursor = None
        seen = set()
        for _page in range(self._max_pages):
            request = {
                "IndexName": self.INDEX_NAME,
                "KeyConditionExpression": "userId = :userId",
                "ExpressionAttributeValues": {":userId": user_id},
                "ProjectionExpression": "eventId, userId",
                "Limit": self._page_size,
            }
            if cursor is not None:
                request["ExclusiveStartKey"] = cursor
            response = self._ledger.query(**request)
            items = response.get("Items") if isinstance(response, Mapping) else None
            if (
                not isinstance(items, list)
                or len(items) > self._page_size
                or any(not isinstance(item, Mapping) for item in items)
            ):
                raise DataAdapterError("message-ledger user-index query failed")
            keys: list[dict[str, str]] = []
            for item in items:
                event_id = item.get("eventId")
                if (
                    item.get("userId") != user_id
                    or not isinstance(event_id, str)
                    or _EVENT_ID.fullmatch(event_id) is None
                ):
                    raise DataAdapterError("message-ledger user binding is invalid")
                keys.append({"eventId": event_id})
            for key in keys:
                self._delete_bound(self._ledger, key=key, user_id=user_id)

            raw_cursor = response.get("LastEvaluatedKey")
            if raw_cursor is None:
                return
            cursor = self._ledger_cursor(raw_cursor, user_id=user_id)
            signature = (cursor["userId"], cursor["eventId"])
            if signature in seen:
                raise DataAdapterError("message-ledger deletion pagination is invalid")
            seen.add(signature)
        raise DataDeletionPending("message-ledger deletion requires another pass")

    def _delete_control_index(self, user_id: str) -> None:
        if self._control is None:
            return
        cursor = None
        seen = set()
        deletion_pk = "DELETION#" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()
        for _page in range(self._max_pages):
            request = {
                "IndexName": self.INDEX_NAME,
                "KeyConditionExpression": "userId = :userId",
                "ExpressionAttributeValues": {":userId": user_id},
                "ProjectionExpression": "PK, SK, userId",
                "Limit": self._page_size,
            }
            if cursor is not None:
                request["ExclusiveStartKey"] = cursor
            response = self._control.query(**request)
            items = response.get("Items") if isinstance(response, Mapping) else None
            if (
                not isinstance(items, list)
                or len(items) > self._page_size
                or any(not isinstance(item, Mapping) for item in items)
            ):
                raise DataAdapterError("control-record user-index query failed")
            keys: list[dict[str, str]] = []
            for item in items:
                pk = item.get("PK")
                sk = item.get("SK")
                if item.get("userId") != user_id or not isinstance(pk, str) or not isinstance(sk, str):
                    raise DataAdapterError("control-record user binding is invalid")
                if pk == f"USER#{user_id}" and sk:
                    # The strongly consistent USER# partition pass owns these;
                    # an eventually consistent GSI can still return stale keys.
                    continue
                if pk == deletion_pk and sk == "DELETION":
                    # This hashed authority fence is intentionally retained.
                    continue
                prefix, separator, digest = pk.partition("#")
                recognized = (
                    separator == "#"
                    and _DIGEST.fullmatch(digest) is not None
                    and (prefix, sk)
                    in {
                        ("SESSION", "SESSION"),
                        ("CONNECT", "CONNECT"),
                        ("OAUTHSTATE", "OAUTHSTATE"),
                        ("OAUTH_STATE", "OAUTH_STATE"),
                    }
                )
                if not recognized:
                    raise DataAdapterError("control-record user binding is invalid")
                keys.append({"PK": pk, "SK": sk})
            for key in keys:
                self._delete_bound(self._control, key=key, user_id=user_id)

            raw_cursor = response.get("LastEvaluatedKey")
            if raw_cursor is None:
                return
            cursor = self._control_cursor(raw_cursor, user_id=user_id)
            signature = (cursor["userId"], cursor["PK"], cursor["SK"])
            if signature in seen:
                raise DataAdapterError("control-record deletion pagination is invalid")
            seen.add(signature)
        raise DataDeletionPending("control-record deletion requires another pass")

    def delete_user_records(self, user_id: str) -> None:
        user_id = _user(user_id)
        self._ensure_user_tombstone(user_id)
        # Each pass starts at the exact beginning. Rows deleted by a prior
        # bounded pass disappear, so a later scheduled reconciliation resumes
        # without trusting a caller-supplied cursor.
        self._delete_identity_partition(user_id)
        self._delete_identity_index(user_id)
        self._delete_message_ledger(user_id)
        self._delete_control_index(user_id)
