"""Privacy-safe capability retention and exact account-deletion boundary."""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import re
from typing import Any, Mapping

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError


DELETION_FENCE_SCHEMA = "personal-operator.capability-deletion-fence.v1"
CAPABILITY_BOOTSTRAP_SCHEMA = "personal-operator.user-authority-bootstrap.v2"
_SUBJECT_DOMAIN = b"personal-operator.capability-deletion-subject.v1\0"
_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_SUBJECT_BINDING = re.compile(r"[0-9a-f]{64}")
_SORT_KEY = re.compile(r"[A-Za-z0-9!_.#:-]{1,1024}")
_CONDITIONAL_ERRORS = frozenset(
    {"ConditionalCheckFailedException", "TransactionCanceledException"}
)
_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()


def _user_id(value: object) -> str:
    if not isinstance(value, str) or _USER_ID.fullmatch(value) is None:
        raise ValueError("capability deletion user identity is invalid")
    return value


def derive_deletion_subject_binding(user_id: str) -> str:
    """Return a domain-separated opaque subject binding for deletion state."""

    user_id = _user_id(user_id)
    digest = hashlib.sha256(_SUBJECT_DOMAIN)
    encoded = user_id.encode("utf-8", errors="strict")
    digest.update(len(encoded).to_bytes(4, "big"))
    digest.update(encoded)
    return digest.hexdigest()


def subject_partition_key(user_id: str) -> str:
    return f"SUBJECT#{derive_deletion_subject_binding(user_id)}"


def _serialize(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _SERIALIZER.serialize(item) for key, item in value.items()}


def _deserialize(value: Mapping[str, Any]) -> dict[str, Any]:
    def normalize(item: Any) -> Any:
        if isinstance(item, Decimal):
            if item != item.to_integral_value():
                raise RuntimeError("capability deletion inventory contains a float")
            return int(item)
        if isinstance(item, list):
            return [normalize(nested) for nested in item]
        if isinstance(item, dict):
            return {key: normalize(nested) for key, nested in item.items()}
        return item

    if not isinstance(value, Mapping):
        raise RuntimeError("capability deletion inventory item is invalid")
    return {
        key: normalize(_DESERIALIZER.deserialize(item))
        for key, item in value.items()
    }


def _conditional(error: BaseException) -> bool:
    return isinstance(error, ClientError) and (
        error.response.get("Error", {}).get("Code") in _CONDITIONAL_ERRORS
    )


def _fence_record(binding: str, *, enabled: bool) -> dict[str, Any]:
    if _SUBJECT_BINDING.fullmatch(binding) is None:
        raise ValueError("capability deletion subject binding is invalid")
    return {
        "schema": DELETION_FENCE_SCHEMA,
        "enabled": enabled,
        "subjectBinding": binding,
    }


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class DynamoCapabilityDeletionAdapter:
    """Fence then purge one exact hashed capability subject partition."""

    def __init__(
        self,
        *,
        client: Any,
        table_name: str,
        page_size: int = 24,
    ) -> None:
        required = ("get_item", "put_item", "update_item", "query", "transact_write_items")
        if client is None or any(
            not callable(getattr(client, method, None)) for method in required
        ):
            raise TypeError("capability deletion adapter requires exact Dynamo methods")
        if not isinstance(table_name, str) or not table_name:
            raise ValueError("capability deletion adapter requires an exact table")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 2 <= page_size <= 24
        ):
            raise ValueError("capability deletion page size is invalid")
        self._client = client
        self._table_name = table_name
        self._page_size = page_size

    @staticmethod
    def _identity(user_id: str) -> tuple[str, str]:
        binding = derive_deletion_subject_binding(user_id)
        return binding, f"SUBJECT#{binding}"

    def _get(self, pk: str, sk: str) -> dict[str, Any] | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=_serialize({"PK": pk, "SK": sk}),
            ConsistentRead=True,
        )
        if not isinstance(response, Mapping):
            raise RuntimeError("capability deletion fence read is ambiguous")
        raw = response.get("Item")
        return None if raw is None else _deserialize(raw)

    @staticmethod
    def _validate_fence(
        item: Mapping[str, Any] | None,
        *,
        binding: str,
        pk: str,
    ) -> bool | None:
        if item is None:
            return None
        expected_fields = {"PK", "SK", "ownerBinding", "recordJson", "version"}
        if set(item) != expected_fields:
            raise RuntimeError("capability deletion fence is malformed")
        if (
            item.get("PK") != pk
            or item.get("SK") != "DELETION"
            or item.get("ownerBinding") != binding
            or isinstance(item.get("version"), bool)
            or not isinstance(item.get("version"), int)
            or item["version"] < 1
        ):
            raise RuntimeError("capability deletion fence binding is invalid")
        try:
            record = json.loads(item["recordJson"])
        except (TypeError, ValueError):
            raise RuntimeError("capability deletion fence record is invalid") from None
        for enabled in (False, True):
            if record == _fence_record(binding, enabled=enabled):
                return enabled
        raise RuntimeError("capability deletion fence record is invalid")

    def _read_fence(self, binding: str, pk: str) -> bool | None:
        return self._validate_fence(
            self._get(pk, "DELETION"),
            binding=binding,
            pk=pk,
        )

    def establish_deletion_fence(self, user_id: str) -> bool:
        """Persist and strongly prove the one-way privacy-safe deny marker."""

        binding, pk = self._identity(user_id)
        current = self._read_fence(binding, pk)
        if current is True:
            return True
        true_json = _canonical(_fence_record(binding, enabled=True))
        try:
            if current is None:
                self._client.put_item(
                    TableName=self._table_name,
                    Item=_serialize(
                        {
                            "PK": pk,
                            "SK": "DELETION",
                            "ownerBinding": binding,
                            "recordJson": true_json,
                            "version": 1,
                        }
                    ),
                    ConditionExpression="attribute_not_exists(PK)",
                )
            else:
                false_json = _canonical(_fence_record(binding, enabled=False))
                item = self._get(pk, "DELETION")
                version = None if item is None else item.get("version")
                if isinstance(version, bool) or not isinstance(version, int):
                    raise RuntimeError("capability deletion fence version is invalid")
                self._client.update_item(
                    TableName=self._table_name,
                    Key=_serialize({"PK": pk, "SK": "DELETION"}),
                    UpdateExpression="SET #record = :record, #version = :next",
                    ConditionExpression=(
                        "#owner = :owner AND #record = :prior AND #version = :expected"
                    ),
                    ExpressionAttributeNames={
                        "#owner": "ownerBinding",
                        "#record": "recordJson",
                        "#version": "version",
                    },
                    ExpressionAttributeValues=_serialize(
                        {
                            ":owner": binding,
                            ":record": true_json,
                            ":prior": false_json,
                            ":next": version + 1,
                            ":expected": version,
                        }
                    ),
                )
        except Exception as error:
            # A lost Dynamo response is safe only after a fresh strong read gives
            # positive proof of the exact monotonic deny marker.
            if self._read_fence(binding, pk) is True:
                return True
            if _conditional(error):
                raise RuntimeError("capability deletion fence raced unsafely") from error
            raise
        if self._read_fence(binding, pk) is not True:
            raise RuntimeError("capability deletion fence could not be proven")
        return True

    @staticmethod
    def _validate_inventory_item(
        item: Mapping[str, Any],
        *,
        binding: str,
        pk: str,
    ) -> str:
        if item.get("PK") != pk:
            raise RuntimeError("capability deletion inventory crossed a partition")
        sk = item.get("SK")
        if not isinstance(sk, str) or _SORT_KEY.fullmatch(sk) is None:
            raise RuntimeError("capability deletion inventory key is invalid")
        if sk == "DELETION":
            DynamoCapabilityDeletionAdapter._validate_fence(
                item,
                binding=binding,
                pk=pk,
            )
            return sk
        if sk == "BOOTSTRAP":
            expected_fields = {"PK", "SK", "ownerBinding", "recordJson", "version"}
            if set(item) != expected_fields:
                raise RuntimeError("capability deletion bootstrap is malformed")
            if (
                item.get("ownerBinding") != binding
                or isinstance(item.get("version"), bool)
                or not isinstance(item.get("version"), int)
                or item["version"] < 1
            ):
                raise RuntimeError("capability deletion bootstrap binding is invalid")
            try:
                record = json.loads(item["recordJson"])
            except (TypeError, ValueError):
                raise RuntimeError(
                    "capability deletion bootstrap record is invalid"
                ) from None
            if (
                not isinstance(record, dict)
                or set(record) != {"schema", "subjectBinding", "catalogDigest"}
                or record.get("schema") != CAPABILITY_BOOTSTRAP_SCHEMA
                or record.get("subjectBinding") != binding
                or not isinstance(record.get("catalogDigest"), str)
                or _SUBJECT_BINDING.fullmatch(record["catalogDigest"]) is None
            ):
                raise RuntimeError("capability deletion bootstrap record is invalid")
            return sk
        if item.get("ownerBinding") != binding:
            raise RuntimeError("capability deletion inventory owner is invalid")
        ttl = item.get("ttl")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
            raise RuntimeError("capability deletion inventory retention is invalid")
        return sk

    def _query(self, pk: str) -> tuple[list[dict[str, Any]], bool]:
        response = self._client.query(
            TableName=self._table_name,
            KeyConditionExpression="#pk = :pk",
            ExpressionAttributeNames={"#pk": "PK"},
            ExpressionAttributeValues=_serialize({":pk": pk}),
            ConsistentRead=True,
            Limit=self._page_size,
        )
        if not isinstance(response, Mapping) or not isinstance(
            response.get("Items"), list
        ):
            raise RuntimeError("capability deletion inventory response is invalid")
        items = [_deserialize(item) for item in response["Items"]]
        has_more = response.get("LastEvaluatedKey") is not None
        return items, has_more

    def delete_user_records(self, user_id: str) -> None:
        """Delete every exact owned row while preserving the active deny marker."""

        binding, pk = self._identity(user_id)
        if self._read_fence(binding, pk) is not True:
            raise RuntimeError("capability deletion fence is not active")

        # Each successful transaction removes at least one row. Restarting the
        # strong query at the partition head avoids cursor/owner loss after a
        # response-loss retry or a concurrent reconciliation invocation.
        for _ in range(100_000):
            items, has_more = self._query(pk)
            candidates: list[str] = []
            for item in items:
                sk = self._validate_inventory_item(item, binding=binding, pk=pk)
                if sk != "DELETION":
                    candidates.append(sk)
            if not candidates:
                if has_more:
                    raise RuntimeError("capability deletion inventory made no progress")
                if self._read_fence(binding, pk) is not True:
                    raise RuntimeError("capability deletion fence was not retained")
                return None

            actions: list[dict[str, Any]] = [
                {
                    "ConditionCheck": {
                        "TableName": self._table_name,
                        "Key": _serialize({"PK": pk, "SK": "DELETION"}),
                        "ConditionExpression": "#owner = :owner AND #record = :record",
                        "ExpressionAttributeNames": {
                            "#owner": "ownerBinding",
                            "#record": "recordJson",
                        },
                        "ExpressionAttributeValues": _serialize(
                            {
                                ":owner": binding,
                                ":record": _canonical(
                                    _fence_record(binding, enabled=True)
                                ),
                            }
                        ),
                    }
                }
            ]
            actions.extend(
                {
                    "Delete": {
                        "TableName": self._table_name,
                        "Key": _serialize({"PK": pk, "SK": sk}),
                        "ConditionExpression": "#owner = :owner",
                        "ExpressionAttributeNames": {"#owner": "ownerBinding"},
                        "ExpressionAttributeValues": _serialize({":owner": binding}),
                    }
                }
                for sk in candidates
            )
            try:
                self._client.transact_write_items(TransactItems=actions)
            except Exception as error:
                if _conditional(error) and self._read_fence(binding, pk) is True:
                    continue
                raise RuntimeError(
                    "capability deletion inventory transaction is uncertain"
                ) from error
        raise RuntimeError("capability deletion inventory exceeded its progress bound")


__all__ = [
    "CAPABILITY_BOOTSTRAP_SCHEMA",
    "DELETION_FENCE_SCHEMA",
    "DynamoCapabilityDeletionAdapter",
    "derive_deletion_subject_binding",
    "subject_partition_key",
]
