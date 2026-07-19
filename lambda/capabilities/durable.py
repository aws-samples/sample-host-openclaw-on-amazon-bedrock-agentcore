"""Strong-read DynamoDB authority records and durable capability replay state."""

from __future__ import annotations

from decimal import Decimal
import json
import re
from typing import Any, Mapping, Sequence

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from .admission import AdmissionDenied, LiveTargetGrant
from .contracts import (
    CapabilityCallV1,
    CapabilityCatalogV1,
    CapabilityInstallationV1,
    CapabilityResultV1,
    TargetGrantV1,
    TurnCapabilityGrantV1,
    canonical_json_bytes,
    canonical_sha256,
    derive_target_tenant_binding,
)
from .ledger import (
    LedgerClaim,
    LedgerDenied,
    LedgerDisposition,
    derive_logical_call_key,
    derive_tenant_binding,
)
from .retention import (
    DELETION_FENCE_SCHEMA,
    derive_deletion_subject_binding,
    subject_partition_key,
)

_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()
_CONDITIONAL_ERRORS = frozenset(
    {"ConditionalCheckFailedException", "TransactionCanceledException"}
)
_RETRY_MODES = frozenset({"READ_ONLY", "IDEMPOTENT", "DEDUPE_KEY_REQUIRED"})
_FROZEN_RETENTION_DAYS = frozenset({0, 30, 90})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
_CALL_ID = re.compile(r"call_[0-9a-f]{64}")
_MAX_TARGET_GRANTS = 64
_SECONDS_PER_DAY = 24 * 60 * 60


def _serialize_item(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _SERIALIZER.serialize(item) for key, item in value.items()}


def _deserialize_item(value: Mapping[str, Any]) -> dict[str, Any]:
    def normalize(item: Any) -> Any:
        if isinstance(item, Decimal):
            if item != item.to_integral_value():
                raise RuntimeError("Dynamo capability state contains a float")
            return int(item)
        if isinstance(item, list):
            return [normalize(nested) for nested in item]
        if isinstance(item, dict):
            return {key: normalize(nested) for key, nested in item.items()}
        return item

    return {
        key: normalize(_DESERIALIZER.deserialize(item)) for key, item in value.items()
    }


def _is_conditional(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") in _CONDITIONAL_ERRORS


def _json_mapping(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{label} record JSON is invalid") from error
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{label} record is not an object")
    return parsed


def _deletion_fence_record(binding: str, *, enabled: bool) -> dict[str, Any]:
    return {
        "schema": DELETION_FENCE_SCHEMA,
        "enabled": enabled,
        "subjectBinding": binding,
    }


class DynamoAdmissionRepository:
    """Read every live authority record consistently from one exact table."""

    def __init__(self, *, client: Any, table_name: str) -> None:
        if client is None or not callable(getattr(client, "get_item", None)):
            raise TypeError("Dynamo admission repository requires a client")
        if not isinstance(table_name, str) or not table_name:
            raise TypeError("Dynamo admission repository requires a table")
        self._client = client
        self._table_name = table_name

    def _get(self, pk: str, sk: str) -> dict[str, Any] | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=_serialize_item({"PK": pk, "SK": sk}),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        return None if raw is None else _deserialize_item(raw)

    def _record(
        self,
        pk: str,
        sk: str,
        *,
        owner_binding: str | None = None,
        ttl_required: bool = False,
    ) -> dict[str, Any] | None:
        item = self._get(pk, sk)
        if item is None:
            return None
        self._validate_item(
            item,
            pk,
            sk,
            owner_binding=owner_binding,
            ttl_required=ttl_required,
        )
        return _json_mapping(item["recordJson"], "live authority")

    @staticmethod
    def _validate_item(
        item: Mapping[str, Any],
        pk: str,
        sk: str,
        *,
        owner_binding: str | None = None,
        ttl_required: bool = False,
    ) -> None:
        expected = {"PK", "SK", "recordJson", "version"}
        if owner_binding is not None:
            expected.add("ownerBinding")
        if ttl_required:
            expected.add("ttl")
        if set(item) != expected:
            raise RuntimeError("live authority item has unexpected fields")
        if item["PK"] != pk or item["SK"] != sk:
            raise RuntimeError("live authority item key is inconsistent")
        if (
            isinstance(item["version"], bool)
            or not isinstance(item["version"], int)
            or item["version"] < 1
        ):
            raise RuntimeError("live authority item version is invalid")
        if owner_binding is not None and item.get("ownerBinding") != owner_binding:
            raise RuntimeError("live authority item owner is inconsistent")
        if ttl_required and (
            isinstance(item.get("ttl"), bool)
            or not isinstance(item.get("ttl"), int)
            or item["ttl"] <= 0
        ):
            raise RuntimeError("live authority item retention is invalid")

    def _required_flag(self, pk: str, sk: str) -> bool:
        record = self._record(pk, sk)
        if record is None or set(record) != {"enabled"}:
            raise RuntimeError("required live authority flag is unavailable")
        if not isinstance(record["enabled"], bool):
            raise RuntimeError("live authority flag is invalid")
        return record["enabled"]

    def strong_read_global_kill_switch(self) -> bool:
        return self._required_flag("CONTROL", "GLOBAL")

    def strong_read_deletion_fence(self, user_id: str) -> bool:
        binding = derive_deletion_subject_binding(user_id)
        record = self._record(
            subject_partition_key(user_id),
            "DELETION",
            owner_binding=binding,
        )
        expected_base = {
            "schema": DELETION_FENCE_SCHEMA,
            "subjectBinding": binding,
        }
        if record == {**expected_base, "enabled": False}:
            return False
        if record == {**expected_base, "enabled": True}:
            return True
        raise RuntimeError("required deletion fence is unavailable")

    def strong_read_user(self, user_id: str) -> Mapping[str, Any] | None:
        binding = derive_deletion_subject_binding(user_id)
        return self._record(
            subject_partition_key(user_id),
            "AUTHORITY#PROFILE",
            owner_binding=binding,
            ttl_required=True,
        )

    def strong_read_session(
        self, user_id: str, session_id: str
    ) -> Mapping[str, Any] | None:
        binding = derive_deletion_subject_binding(user_id)
        return self._record(
            subject_partition_key(user_id),
            f"SESSION#{session_id}",
            owner_binding=binding,
            ttl_required=True,
        )

    def strong_read_runtime(
        self,
        user_id: str,
        runtime_arn: str,
        runtime_qualifier: str,
        session_id: str,
    ) -> Mapping[str, Any] | None:
        binding = derive_deletion_subject_binding(user_id)
        return self._record(
            subject_partition_key(user_id),
            "RUNTIME#"
            + canonical_sha256(
                {
                    "runtimeArn": runtime_arn,
                    "runtimeQualifier": runtime_qualifier,
                    "sessionId": session_id,
                }
            ),
            owner_binding=binding,
            ttl_required=True,
        )

    def strong_read_turn_grant(
        self, user_id: str, invocation_id: str
    ) -> Mapping[str, Any] | None:
        binding = derive_deletion_subject_binding(user_id)
        return self._record(
            subject_partition_key(user_id),
            f"TURN#{invocation_id}",
            owner_binding=binding,
            ttl_required=True,
        )

    def strong_read_installation(
        self, user_id: str, pack_id: str
    ) -> CapabilityInstallationV1 | Mapping[str, Any] | None:
        binding = derive_deletion_subject_binding(user_id)
        return self._record(
            subject_partition_key(user_id),
            f"AUTHORITY#INSTALL#{pack_id}",
            owner_binding=binding,
            ttl_required=True,
        )

    def persist_target_grants(
        self,
        *,
        tenant_id: str,
        current_request_id: str,
        grants: Sequence[TargetGrantV1],
    ) -> None:
        """Atomically retain fresh grants under their trusted tenant partition."""

        tenant_binding = derive_target_tenant_binding(tenant_id)
        if (
            not isinstance(current_request_id, str)
            or _OPAQUE_ID.fullmatch(current_request_id) is None
        ):
            raise TypeError("target persistence requires an exact request identity")
        if isinstance(grants, (str, bytes)) or not isinstance(grants, Sequence):
            raise TypeError("target persistence requires a grant sequence")
        if len(grants) > _MAX_TARGET_GRANTS:
            raise ValueError("target persistence exceeds its grant bound")

        actions: list[dict[str, Any]] = []
        seen: set[str] = set()
        for grant in grants:
            if not isinstance(grant, TargetGrantV1):
                raise TypeError("target persistence requires validated grants")
            if grant.current_request_id != current_request_id:
                raise ValueError("target grant request binding is not current")
            if grant.tenant_binding != tenant_binding:
                raise ValueError("target grant tenant binding is not current")
            if grant.target_hash in seen:
                raise ValueError("target persistence contains a duplicate grant")
            seen.add(grant.target_hash)
            pk, sk = self._target_key(tenant_id, grant.target_hash)
            record = {
                "grant": grant.to_mapping(),
                "uses": 0,
                "claimedCallIds": [],
            }
            actions.append(
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": _serialize_item(
                            {
                                "PK": pk,
                                "SK": sk,
                                "recordJson": canonical_json_bytes(record).decode(
                                    "utf-8"
                                ),
                                "ownerBinding": derive_deletion_subject_binding(
                                    tenant_id
                                ),
                                "ttl": grant.expires_at,
                                "version": 1,
                            }
                        ),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                }
            )
        if not actions:
            return
        owner_binding = derive_deletion_subject_binding(tenant_id)
        actions.insert(
            0,
            {
                "ConditionCheck": {
                    "TableName": self._table_name,
                    "Key": _serialize_item(
                        {"PK": subject_partition_key(tenant_id), "SK": "DELETION"}
                    ),
                    "ConditionExpression": "#owner = :owner AND #record = :record",
                    "ExpressionAttributeNames": {
                        "#owner": "ownerBinding",
                        "#record": "recordJson",
                    },
                    "ExpressionAttributeValues": _serialize_item(
                        {
                            ":owner": owner_binding,
                            ":record": canonical_json_bytes(
                                _deletion_fence_record(
                                    owner_binding,
                                    enabled=False,
                                )
                            ).decode("utf-8"),
                        }
                    ),
                }
            },
        )
        conditional_error: ClientError | None = None
        try:
            self._client.transact_write_items(TransactItems=actions)
            return
        except ClientError as error:
            if not _is_conditional(error):
                raise
            conditional_error = error

        # A retry after a lost response is idempotent only when every retained
        # row is the exact unused grant from this same request and tenant.
        for grant in grants:
            recovered = self.strong_read_target_grant(
                tenant_id,
                grant.target_hash,
            )
            if (
                recovered is None
                or recovered.grant.to_bytes() != grant.to_bytes()
                or recovered.uses != 0
                or recovered.claimed_call_ids
            ):
                raise RuntimeError(
                    "target grant persistence conflict"
                ) from conditional_error

    @staticmethod
    def _target_key(user_id: str, target_hash: str) -> tuple[str, str]:
        if (
            not isinstance(target_hash, str)
            or _SHA256.fullmatch(target_hash) is None
        ):
            raise TypeError("target state requires exact tenant and target bindings")
        return subject_partition_key(user_id), f"TARGET#{target_hash}"

    def strong_read_target_grant(
        self, user_id: str, target_hash: str
    ) -> LiveTargetGrant | None:
        tenant_binding = derive_target_tenant_binding(user_id)
        owner_binding = derive_deletion_subject_binding(user_id)
        pk, sk = self._target_key(user_id, target_hash)
        record = self._record(
            pk,
            sk,
            owner_binding=owner_binding,
            ttl_required=True,
        )
        if record is None:
            return None
        if set(record) != {"grant", "uses", "claimedCallIds"}:
            raise RuntimeError("live target grant record is invalid")
        claimed = record["claimedCallIds"]
        if (
            not isinstance(claimed, list)
            or any(not isinstance(value, str) for value in claimed)
            or claimed != sorted(set(claimed))
        ):
            raise RuntimeError("live target claim inventory is invalid")
        grant = TargetGrantV1.from_mapping(record["grant"])
        if grant.target_hash != target_hash:
            raise RuntimeError("live target grant binding is invalid")
        if grant.tenant_binding != tenant_binding:
            raise AdmissionDenied("TARGET_GRANT_TENANT_MISMATCH")
        return LiveTargetGrant(
            grant=grant,
            uses=record["uses"],
            claimed_call_ids=tuple(claimed),
        )

    def claim_target_use(
        self,
        user_id: str,
        target_hash: str,
        current_request_id: str,
        call_id: str,
    ) -> bool:
        tenant_binding = derive_target_tenant_binding(user_id)
        owner_binding = derive_deletion_subject_binding(user_id)
        pk, sk = self._target_key(user_id, target_hash)
        if (
            not isinstance(current_request_id, str)
            or _OPAQUE_ID.fullmatch(current_request_id) is None
            or not isinstance(call_id, str)
            or _CALL_ID.fullmatch(call_id) is None
        ):
            raise TypeError("target claim requires exact request and call identities")
        for _ in range(4):
            item = self._get(pk, sk)
            if item is None:
                return False
            self._validate_item(
                item,
                pk,
                sk,
                owner_binding=owner_binding,
                ttl_required=True,
            )
            record = _json_mapping(item.get("recordJson"), "live target")
            if set(record) != {"grant", "uses", "claimedCallIds"}:
                raise RuntimeError("live target grant record is invalid")
            grant = TargetGrantV1.from_mapping(record["grant"])
            claimed = record["claimedCallIds"]
            LiveTargetGrant(
                grant=grant,
                uses=record["uses"],
                claimed_call_ids=tuple(claimed),
            )
            if grant.target_hash != target_hash:
                raise RuntimeError("live target grant binding is invalid")
            if grant.tenant_binding != tenant_binding:
                raise AdmissionDenied("TARGET_GRANT_TENANT_MISMATCH")
            if grant.current_request_id != current_request_id:
                raise AdmissionDenied("TARGET_GRANT_REQUEST_MISMATCH")
            if call_id in claimed:
                return True
            if record["uses"] >= grant.max_uses:
                return False
            updated = {
                **record,
                "uses": record["uses"] + 1,
                "claimedCallIds": sorted([*claimed, call_id]),
            }
            try:
                self._client.update_item(
                    TableName=self._table_name,
                    Key=_serialize_item({"PK": pk, "SK": sk}),
                    UpdateExpression="SET #record = :record, #version = :next",
                    ConditionExpression="#version = :expected",
                    ExpressionAttributeNames={
                        "#record": "recordJson",
                        "#version": "version",
                    },
                    ExpressionAttributeValues=_serialize_item(
                        {
                            ":record": canonical_json_bytes(updated).decode("utf-8"),
                            ":next": item["version"] + 1,
                            ":expected": item["version"],
                        }
                    ),
                )
                return True
            except ClientError as error:
                if not _is_conditional(error):
                    raise
        raise RuntimeError("target claim contention exceeded its bound")


class DynamoTurnAuthorityRepository:
    """Create bounded hashed-subject authority without restoring revocation."""

    _ROOT_MARKER = {"schema": "personal-operator.authority-root.v1"}
    _BOOTSTRAP_SCHEMA = "personal-operator.user-authority-bootstrap.v2"

    def __init__(
        self,
        *,
        client: Any,
        table_name: str,
        catalog: CapabilityCatalogV1,
    ) -> None:
        if not isinstance(catalog, CapabilityCatalogV1):
            raise TypeError("turn authority requires the frozen capability catalog")
        if not callable(getattr(client, "put_item", None)) or not callable(
            getattr(client, "transact_write_items", None)
        ):
            raise TypeError("turn authority requires exact Dynamo write methods")
        self._client = client
        self._table_name = table_name
        self._catalog = catalog
        self._authority_retention_seconds = max(
            pack["retentionPolicy"]["maxDays"] for pack in catalog.packs
        ) * _SECONDS_PER_DAY
        if self._authority_retention_seconds <= 0:
            raise ValueError("turn authority catalog has no bounded retention")
        self._admission = DynamoAdmissionRepository(
            client=client,
            table_name=table_name,
        )

    def _put_record_if_absent(
        self,
        pk: str,
        sk: str,
        record: Mapping[str, Any],
    ) -> bool:
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=_serialize_item(
                    {
                        "PK": pk,
                        "SK": sk,
                        "recordJson": canonical_json_bytes(record).decode("utf-8"),
                        "version": 1,
                    }
                ),
                ConditionExpression="attribute_not_exists(PK)",
            )
            return True
        except ClientError as error:
            if _is_conditional(error):
                return False
            raise

    @staticmethod
    def _subject_item(
        *,
        binding: str,
        pk: str,
        sk: str,
        record: Mapping[str, Any],
        ttl: int | None,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "PK": pk,
            "SK": sk,
            "ownerBinding": binding,
            "recordJson": canonical_json_bytes(record).decode("utf-8"),
            "version": 1,
        }
        if ttl is not None:
            if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
                raise ValueError("turn authority retention is invalid")
            item["ttl"] = ttl
        return item

    def _false_fence_condition(self, binding: str, pk: str) -> dict[str, Any]:
        return {
            "ConditionCheck": {
                "TableName": self._table_name,
                "Key": _serialize_item({"PK": pk, "SK": "DELETION"}),
                "ConditionExpression": "#owner = :owner AND #record = :record",
                "ExpressionAttributeNames": {
                    "#owner": "ownerBinding",
                    "#record": "recordJson",
                },
                "ExpressionAttributeValues": _serialize_item(
                    {
                        ":owner": binding,
                        ":record": canonical_json_bytes(
                            _deletion_fence_record(binding, enabled=False)
                        ).decode("utf-8"),
                    }
                ),
            }
        }

    def _transact(self, actions: Sequence[Mapping[str, Any]]) -> bool:
        try:
            self._client.transact_write_items(TransactItems=list(actions))
            return True
        except ClientError as error:
            if _is_conditional(error):
                return False
            raise

    def _require_exact(
        self,
        pk: str,
        sk: str,
        expected: Mapping[str, Any],
        label: str,
    ) -> None:
        actual = self._admission._record(pk, sk)
        if actual is None or canonical_json_bytes(actual) != canonical_json_bytes(
            expected
        ):
            raise RuntimeError(f"{label} is unavailable or unsafe")

    def _require_subject_record(
        self,
        *,
        user_id: str,
        sk: str,
        expected: Mapping[str, Any],
        label: str,
        ttl_required: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        binding = derive_deletion_subject_binding(user_id)
        pk = subject_partition_key(user_id)
        item = self._admission._get(pk, sk)
        if item is None:
            raise RuntimeError(f"{label} is unavailable or unsafe")
        self._admission._validate_item(
            item,
            pk,
            sk,
            owner_binding=binding,
            ttl_required=ttl_required,
        )
        actual = _json_mapping(item["recordJson"], label)
        if canonical_json_bytes(actual) != canonical_json_bytes(expected):
            raise RuntimeError(f"{label} is unavailable or unsafe")
        return item, actual

    def _ensure_root_authority(self) -> None:
        root = self._admission._record("CONTROL", "ROOT")
        if root is None:
            global_state = self._admission._record("CONTROL", "GLOBAL")
            if global_state is None:
                self._put_record_if_absent(
                    "CONTROL",
                    "GLOBAL",
                    {"enabled": False},
                )
            else:
                self._require_exact(
                    "CONTROL",
                    "GLOBAL",
                    {"enabled": False},
                    "global kill switch",
                )
            self._put_record_if_absent(
                "CONTROL",
                "ROOT",
                self._ROOT_MARKER,
            )
        self._require_exact(
            "CONTROL",
            "ROOT",
            self._ROOT_MARKER,
            "authority root marker",
        )
        self._require_exact(
            "CONTROL",
            "GLOBAL",
            {"enabled": False},
            "global kill switch",
        )

    def _installation(self, user_id: str, pack_id: str) -> dict[str, Any]:
        return CapabilityInstallationV1.from_mapping(
            {
                "schema": CapabilityInstallationV1.SCHEMA,
                "userId": user_id,
                "packId": pack_id,
                "catalogDigest": self._catalog.catalog_digest,
                "state": "ENABLED",
                "policyRevision": 1,
                "connectionRefs": [],
                "killSwitch": False,
            }
        ).to_mapping()

    def _bootstrap_marker(self, binding: str) -> dict[str, Any]:
        return {
            "schema": self._BOOTSTRAP_SCHEMA,
            "subjectBinding": binding,
            "catalogDigest": self._catalog.catalog_digest,
        }

    def _ensure_live_deletion_fence(self, user_id: str) -> tuple[str, str]:
        binding = derive_deletion_subject_binding(user_id)
        pk = subject_partition_key(user_id)
        expected = _deletion_fence_record(binding, enabled=False)
        item = self._admission._get(pk, "DELETION")
        if item is None:
            try:
                self._client.put_item(
                    TableName=self._table_name,
                    Item=_serialize_item(
                        self._subject_item(
                            binding=binding,
                            pk=pk,
                            sk="DELETION",
                            record=expected,
                            ttl=None,
                        )
                    ),
                    ConditionExpression="attribute_not_exists(PK)",
                )
            except ClientError as error:
                if not _is_conditional(error):
                    raise
        record = self._admission._record(
            pk,
            "DELETION",
            owner_binding=binding,
        )
        if record == _deletion_fence_record(binding, enabled=True):
            raise RuntimeError("account deletion fence is active")
        if record != expected:
            raise RuntimeError("account deletion fence is unavailable or unsafe")
        return binding, pk

    def _ensure_user_authority(self, user_id: str, issued_at: int) -> list[CapabilityInstallationV1]:
        if isinstance(issued_at, bool) or not isinstance(issued_at, int) or issued_at < 0:
            raise ValueError("turn authority issued time is invalid")
        binding, pk = self._ensure_live_deletion_fence(user_id)
        authority_ttl = issued_at + self._authority_retention_seconds
        marker = {
            **self._bootstrap_marker(binding),
        }
        records: list[tuple[str, Mapping[str, Any], str]] = [
            (
                "AUTHORITY#PROFILE",
                {
                    "userId": user_id,
                    "state": "ACTIVE",
                    "deletionFence": False,
                },
                "user authority",
            ),
        ]
        records.extend(
            (
                f"AUTHORITY#INSTALL#{pack['packId']}",
                self._installation(user_id, pack["packId"]),
                f"installation {pack['packId']}",
            )
            for pack in self._catalog.packs
        )
        bootstrap = self._admission._record(
            pk,
            "BOOTSTRAP",
            owner_binding=binding,
        )
        if bootstrap is None:
            actions: list[Mapping[str, Any]] = [
                self._false_fence_condition(binding, pk)
            ]
            actions.extend(
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": _serialize_item(
                            self._subject_item(
                                binding=binding,
                                pk=pk,
                                sk=sk,
                                record=record,
                                ttl=authority_ttl,
                            )
                        ),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                }
                for sk, record, _ in records
            )
            actions.append(
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": _serialize_item(
                            self._subject_item(
                                binding=binding,
                                pk=pk,
                                sk="BOOTSTRAP",
                                record=marker,
                                ttl=None,
                            )
                        ),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                }
            )
            self._transact(actions)
        self._require_subject_record(
            user_id=user_id,
            sk="BOOTSTRAP",
            expected=marker,
            label="user authority marker",
            ttl_required=False,
        )

        installations: list[CapabilityInstallationV1] = []
        refresh_actions: list[Mapping[str, Any]] = [
            self._false_fence_condition(binding, pk)
        ]
        for sk, expected_default, label in records:
            item = self._admission._get(pk, sk)
            if item is None:
                raise RuntimeError(f"{label} installation or profile is unavailable")
            self._admission._validate_item(
                item,
                pk,
                sk,
                owner_binding=binding,
                ttl_required=True,
            )
            raw = _json_mapping(item["recordJson"], label)
            if sk == "AUTHORITY#PROFILE":
                if canonical_json_bytes(raw) != canonical_json_bytes(expected_default):
                    raise RuntimeError("user authority is unavailable or unsafe")
            else:
                try:
                    installation = CapabilityInstallationV1.from_mapping(raw)
                except (TypeError, ValueError) as error:
                    raise RuntimeError(f"{label} is invalid") from error
                pack_id = sk.removeprefix("AUTHORITY#INSTALL#")
                if (
                    installation.user_id != user_id
                    or installation.pack_id != pack_id
                    or installation.catalog_digest != self._catalog.catalog_digest
                ):
                    raise RuntimeError(f"{label} binding is invalid")
                installations.append(installation)
            refresh_actions.append(
                {
                    "Update": {
                        "TableName": self._table_name,
                        "Key": _serialize_item({"PK": pk, "SK": sk}),
                        "UpdateExpression": "SET #ttl = :ttl, #version = :next",
                        "ConditionExpression": (
                            "#owner = :owner AND #record = :record "
                            "AND #version = :expected"
                        ),
                        "ExpressionAttributeNames": {
                            "#owner": "ownerBinding",
                            "#record": "recordJson",
                            "#ttl": "ttl",
                            "#version": "version",
                        },
                        "ExpressionAttributeValues": _serialize_item(
                            {
                                ":owner": binding,
                                ":record": item["recordJson"],
                                ":ttl": authority_ttl,
                                ":next": item["version"] + 1,
                                ":expected": item["version"],
                            }
                        ),
                    }
                }
            )
        if not self._transact(refresh_actions):
            if self._admission.strong_read_deletion_fence(user_id):
                raise RuntimeError("account deletion fence is active")
            raise RuntimeError("turn installation authority changed concurrently")
        return installations

    def strong_read_enabled_pack_ids(
        self,
        *,
        user_id: str,
        issued_at: int,
    ) -> tuple[str, ...]:
        self._ensure_root_authority()
        installations = self._ensure_user_authority(user_id, issued_at)
        return tuple(
            sorted(
                installation.pack_id
                for installation in installations
                if installation.state == "ENABLED" and not installation.kill_switch
            )
        )

    def _ensure_subject_record(
        self,
        *,
        user_id: str,
        sk: str,
        record: Mapping[str, Any],
        ttl: int,
        label: str,
        refresh_ttl: bool,
    ) -> None:
        binding, pk = self._ensure_live_deletion_fence(user_id)
        item = self._admission._get(pk, sk)
        if item is None:
            inserted = self._transact(
                [
                    self._false_fence_condition(binding, pk),
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": _serialize_item(
                                self._subject_item(
                                    binding=binding,
                                    pk=pk,
                                    sk=sk,
                                    record=record,
                                    ttl=ttl,
                                )
                            ),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            )
            if inserted:
                return
            if self._admission.strong_read_deletion_fence(user_id):
                raise RuntimeError("account deletion fence is active")
            item = self._admission._get(pk, sk)
        if item is None:
            raise RuntimeError(f"{label} persistence is uncertain")
        self._admission._validate_item(
            item,
            pk,
            sk,
            owner_binding=binding,
            ttl_required=True,
        )
        actual = _json_mapping(item["recordJson"], label)
        if canonical_json_bytes(actual) != canonical_json_bytes(record):
            raise RuntimeError(f"{label} is unavailable or unsafe")
        if item["ttl"] == ttl or (refresh_ttl and item["ttl"] > ttl):
            return
        if not refresh_ttl:
            raise RuntimeError(f"{label} retention is unavailable or unsafe")
        updated = self._transact(
            [
                self._false_fence_condition(binding, pk),
                {
                    "Update": {
                        "TableName": self._table_name,
                        "Key": _serialize_item({"PK": pk, "SK": sk}),
                        "UpdateExpression": "SET #ttl = :ttl, #version = :next",
                        "ConditionExpression": (
                            "#owner = :owner AND #record = :record "
                            "AND #version = :expected"
                        ),
                        "ExpressionAttributeNames": {
                            "#owner": "ownerBinding",
                            "#record": "recordJson",
                            "#ttl": "ttl",
                            "#version": "version",
                        },
                        "ExpressionAttributeValues": _serialize_item(
                            {
                                ":owner": binding,
                                ":record": item["recordJson"],
                                ":ttl": ttl,
                                ":next": item["version"] + 1,
                                ":expected": item["version"],
                            }
                        ),
                    }
                },
            ]
        )
        if not updated:
            raise RuntimeError(f"{label} retention refresh is uncertain")

    def _ensure_turn_bindings(self, grant: TurnCapabilityGrantV1) -> None:
        session = {
            "sessionId": grant.session_id,
            "userId": grant.sub,
            "runtimeArn": grant.runtime_arn,
            "runtimeQualifier": grant.runtime_qualifier,
            "state": "ACTIVE",
        }
        self._ensure_subject_record(
            user_id=grant.sub,
            sk=f"SESSION#{grant.session_id}",
            record=session,
            ttl=grant.exp,
            label="session authority",
            refresh_ttl=True,
        )

        runtime = {
            "runtimeArn": grant.runtime_arn,
            "runtimeQualifier": grant.runtime_qualifier,
            "sessionId": grant.session_id,
            "userId": grant.sub,
            "releaseCommit": grant.release_commit,
            "catalogDigest": grant.catalog_digest,
            "state": "READY",
        }
        runtime_sk = "RUNTIME#" + canonical_sha256(
            {
                "runtimeArn": grant.runtime_arn,
                "runtimeQualifier": grant.runtime_qualifier,
                "sessionId": grant.session_id,
            }
        )
        self._ensure_subject_record(
            user_id=grant.sub,
            sk=runtime_sk,
            record=runtime,
            ttl=grant.exp,
            label="runtime authority",
            refresh_ttl=True,
        )

    def prepare_turn(
        self,
        *,
        grant: TurnCapabilityGrantV1,
        targets: Sequence[LiveTargetGrant],
        delivery_context: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(grant, TurnCapabilityGrantV1):
            raise TypeError("turn authority requires a validated grant")
        if grant.release_commit != self._catalog.release_commit or (
            grant.catalog_digest != self._catalog.catalog_digest
        ):
            raise ValueError("turn authority grant differs from the frozen catalog")
        catalog_pack_ids = {pack["packId"] for pack in self._catalog.packs}
        if not set(grant.allowed_pack_ids).issubset(catalog_pack_ids):
            raise ValueError("turn authority contains a non-catalogued pack")
        catalog_operations = {
            pack["packId"]: pack["operations"][0]["operationId"]
            for pack in self._catalog.packs
        }
        expected_operations = sorted(
            catalog_operations[pack_id] for pack_id in grant.allowed_pack_ids
        )
        if list(grant.allowed_operation_ids) != expected_operations:
            raise ValueError("turn operation authority differs from its packs")
        tenant_binding = derive_target_tenant_binding(grant.sub)
        target_grants: list[TargetGrantV1] = []
        for live in targets:
            if (
                not isinstance(live, LiveTargetGrant)
                or live.uses != 0
                or live.claimed_call_ids
            ):
                raise TypeError("turn authority requires fresh target grants")
            target = live.grant
            if (
                target.tenant_binding != tenant_binding
                or target.current_request_id != grant.invocation_id
            ):
                raise ValueError("turn target authority is not current")
            target_grants.append(target)
        if sorted(target.target_hash for target in target_grants) != list(
            grant.target_grant_hashes
        ):
            raise ValueError("turn target authority inventory differs from grant")

        delivery_record = None
        if delivery_context is not None:
            if (
                not isinstance(delivery_context, Mapping)
                or set(delivery_context) != {"channel", "actorId", "chatId"}
                or delivery_context.get("channel") != "telegram"
                or not isinstance(delivery_context.get("actorId"), str)
                or not isinstance(delivery_context.get("chatId"), str)
                or delivery_context["actorId"]
                != f"telegram:{delivery_context['chatId']}"
                or re.fullmatch(r"[1-9][0-9]{0,19}", delivery_context["chatId"])
                is None
            ):
                raise ValueError("turn delivery context is invalid")
            delivery_record = {
                "schema": "personal-operator.turn-delivery-context.v1",
                "userId": grant.sub,
                "invocationId": grant.invocation_id,
                "channel": "telegram",
                "actorId": delivery_context["actorId"],
                "chatId": delivery_context["chatId"],
            }

        self._ensure_root_authority()
        if self._admission.strong_read_deletion_fence(grant.sub):
            raise RuntimeError("account deletion fence is active")
        for pack_id in grant.allowed_pack_ids:
            raw_installation = self._admission.strong_read_installation(
                grant.sub,
                pack_id,
            )
            try:
                installation = CapabilityInstallationV1.from_mapping(
                    raw_installation
                )
            except (TypeError, ValueError) as error:
                raise RuntimeError("turn installation authority is invalid") from error
            if (
                installation.user_id != grant.sub
                or installation.pack_id != pack_id
                or installation.catalog_digest != self._catalog.catalog_digest
                or installation.state != "ENABLED"
                or installation.kill_switch
            ):
                raise RuntimeError("turn installation authority is not enabled")
        self._ensure_subject_record(
            user_id=grant.sub,
            sk=f"TURN#{grant.invocation_id}",
            record=grant.to_mapping(),
            ttl=grant.exp,
            label="turn grant authority",
            refresh_ttl=False,
        )
        self._ensure_turn_bindings(grant)
        if delivery_record is not None:
            self._ensure_subject_record(
                user_id=grant.sub,
                sk=f"DELIVERY#{grant.invocation_id}",
                record=delivery_record,
                ttl=grant.exp,
                label="turn delivery context",
                refresh_ttl=False,
            )
        self._admission.persist_target_grants(
            tenant_id=grant.sub,
            current_request_id=grant.invocation_id,
            grants=target_grants,
        )


class DynamoCapabilityLedger:
    """Tenant-scoped durable replay, logical-effect, retry, and budget ledger."""

    def __init__(self, *, client: Any, table_name: str) -> None:
        if client is None or not callable(getattr(client, "get_item", None)):
            raise TypeError("Dynamo capability ledger requires a client")
        if not isinstance(table_name, str) or not table_name:
            raise TypeError("Dynamo capability ledger requires a table")
        self._client = client
        self._table_name = table_name

    @staticmethod
    def _pk(user_id: str) -> str:
        return subject_partition_key(user_id)

    @staticmethod
    def _turn_sk(call: CapabilityCallV1) -> str:
        return f"LEDGER#TURN#{call.invocation_id}"

    @staticmethod
    def _call_sk(call_id: str) -> str:
        return f"LEDGER#CALL#{call_id}"

    @staticmethod
    def _tool_sk(call: CapabilityCallV1) -> str:
        return f"LEDGER#TOOL#{call.invocation_id}#{call.tool_use_id}"

    @staticmethod
    def _logical_sk(call: CapabilityCallV1, logical_key: str) -> str:
        return f"LEDGER#LOGICAL#{call.invocation_id}#{logical_key}"

    def _get(
        self,
        pk: str,
        sk: str,
        *,
        owner_binding: str,
    ) -> dict[str, Any] | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=_serialize_item({"PK": pk, "SK": sk}),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        if raw is None:
            return None
        item = _deserialize_item(raw)
        if (
            item.get("PK") != pk
            or item.get("SK") != sk
            or item.get("ownerBinding") != owner_binding
            or isinstance(item.get("ttl"), bool)
            or not isinstance(item.get("ttl"), int)
            or item["ttl"] <= 0
        ):
            raise LedgerDenied("CAPABILITY_LEDGER_CORRUPT")
        return item

    def _put(
        self,
        item: Mapping[str, Any],
        *,
        owner_binding: str,
        ttl: int,
    ) -> dict[str, Any]:
        return {
            "Put": {
                "TableName": self._table_name,
                "Item": _serialize_item(
                    {
                        **item,
                        "ownerBinding": owner_binding,
                        "ttl": ttl,
                    }
                ),
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        }

    def _update(
        self,
        *,
        pk: str,
        sk: str,
        expression: str,
        condition: str,
        names: Mapping[str, str],
        values: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "Update": {
                "TableName": self._table_name,
                "Key": _serialize_item({"PK": pk, "SK": sk}),
                "UpdateExpression": expression,
                "ConditionExpression": condition,
                "ExpressionAttributeNames": dict(names),
                "ExpressionAttributeValues": _serialize_item(values),
            }
        }

    def _transact(
        self,
        actions: list[dict[str, Any]],
        *,
        user_id: str,
        owner_binding: str,
    ) -> bool:
        pk = subject_partition_key(user_id)
        fenced_actions = [
            {
                "ConditionCheck": {
                    "TableName": self._table_name,
                    "Key": _serialize_item({"PK": pk, "SK": "DELETION"}),
                    "ConditionExpression": "#owner = :owner AND #record = :record",
                    "ExpressionAttributeNames": {
                        "#owner": "ownerBinding",
                        "#record": "recordJson",
                    },
                    "ExpressionAttributeValues": _serialize_item(
                        {
                            ":owner": owner_binding,
                            ":record": canonical_json_bytes(
                                _deletion_fence_record(
                                    owner_binding,
                                    enabled=False,
                                )
                            ).decode("utf-8"),
                        }
                    ),
                }
            },
            *actions,
        ]
        try:
            self._client.transact_write_items(TransactItems=fenced_actions)
            return True
        except ClientError as error:
            if _is_conditional(error):
                return False
            raise

    @staticmethod
    def _validate_begin(
        call: CapabilityCallV1,
        grant: TurnCapabilityGrantV1,
        pack_id: str,
        pack_max_calls: int,
        retry_mode: str,
        retention_max_days: int,
    ) -> None:
        if not isinstance(call, CapabilityCallV1) or not isinstance(
            grant, TurnCapabilityGrantV1
        ):
            raise TypeError("ledger requires validated call and grant contracts")
        if not isinstance(pack_id, str) or not pack_id:
            raise TypeError("ledger requires an exact pack ID")
        if (
            isinstance(pack_max_calls, bool)
            or not isinstance(pack_max_calls, int)
            or pack_max_calls < 1
        ):
            raise TypeError("ledger pack budget is invalid")
        if retry_mode not in _RETRY_MODES:
            raise TypeError("ledger retry mode is invalid")
        if (
            isinstance(retention_max_days, bool)
            or not isinstance(retention_max_days, int)
            or retention_max_days not in _FROZEN_RETENTION_DAYS
        ):
            raise TypeError("ledger retention policy is invalid")

    def begin(
        self,
        *,
        call: CapabilityCallV1,
        grant: TurnCapabilityGrantV1,
        pack_id: str,
        pack_max_calls: int,
        retry_mode: str,
        retention_max_days: int = 0,
    ) -> LedgerClaim:
        self._validate_begin(
            call,
            grant,
            pack_id,
            pack_max_calls,
            retry_mode,
            retention_max_days,
        )
        tenant = derive_tenant_binding(grant)
        owner_binding = derive_deletion_subject_binding(grant.sub)
        retention_ttl = (
            grant.exp
            if retention_max_days == 0
            else grant.iat + retention_max_days * _SECONDS_PER_DAY
        )
        grant_binding = canonical_sha256(grant.to_mapping())
        logical_key = derive_logical_call_key(call, tenant)
        pk = self._pk(grant.sub)
        call_json = canonical_json_bytes(call.to_mapping()).decode("utf-8")
        turn_sk = self._turn_sk(call)
        call_sk = self._call_sk(call.call_id)
        tool_sk = self._tool_sk(call)
        logical_sk = self._logical_sk(call, logical_key)

        for _ in range(4):
            turn = self._get(pk, turn_sk, owner_binding=owner_binding)
            if turn is not None and turn.get("grantBinding") != grant_binding:
                raise LedgerDenied("CAPABILITY_GRANT_BINDING_MISMATCH")

            tool = self._get(pk, tool_sk, owner_binding=owner_binding)
            if tool is not None:
                expected_tool = (call.operation_id, call.args_hash, call.call_id)
                actual_tool = (
                    tool.get("operationId"),
                    tool.get("argsHash"),
                    tool.get("callId"),
                )
                if actual_tool != expected_tool:
                    raise LedgerDenied("CAPABILITY_ARGUMENT_MUTATION")

            entry = self._get(pk, call_sk, owner_binding=owner_binding)
            if entry is not None:
                if (
                    entry.get("tenantBinding") != tenant
                    or entry.get("grantBinding") != grant_binding
                ):
                    raise LedgerDenied("CAPABILITY_GRANT_BINDING_MISMATCH")
                if entry.get("callJson") != call_json:
                    raise LedgerDenied("CAPABILITY_CALL_ID_CONFLICT")
                if entry.get("state") == "IN_FLIGHT":
                    return LedgerClaim(LedgerDisposition.IN_FLIGHT)
                result = CapabilityResultV1.from_mapping(
                    _json_mapping(entry.get("resultJson"), "capability result")
                ).validate_against_call(call)
                if result.status == "FAILED_RETRYABLE" and retry_mode == "READ_ONLY":
                    attempts = entry.get("attempts")
                    if not isinstance(attempts, int) or attempts < 1:
                        raise LedgerDenied("CAPABILITY_LEDGER_CORRUPT")
                    if attempts >= 2:
                        return LedgerClaim(LedgerDisposition.RETRY_EXHAUSTED)
                    retry = self._update(
                        pk=pk,
                        sk=call_sk,
                        expression=(
                            "SET #state = :state, #attempts = :attempts, "
                            "#version = :next REMOVE #result"
                        ),
                        condition="#version = :expected",
                        names={
                            "#state": "state",
                            "#attempts": "attempts",
                            "#version": "version",
                            "#result": "resultJson",
                        },
                        values={
                            ":state": "IN_FLIGHT",
                            ":attempts": attempts + 1,
                            ":next": entry["version"] + 1,
                            ":expected": entry["version"],
                        },
                    )
                    if self._transact(
                        [retry],
                        user_id=grant.sub,
                        owner_binding=owner_binding,
                    ):
                        return LedgerClaim(LedgerDisposition.RETRY)
                    continue
                return LedgerClaim(LedgerDisposition.CACHED, result)

            if tool is not None:
                raise LedgerDenied("CAPABILITY_LEDGER_CORRUPT")

            logical = self._get(pk, logical_sk, owner_binding=owner_binding)
            logical_action = None
            if logical is not None:
                prior_call_id = logical.get("callId")
                if prior_call_id != call.call_id:
                    if retry_mode != "READ_ONLY":
                        return LedgerClaim(LedgerDisposition.LOGICAL_FENCE)
                    prior = self._get(
                        pk,
                        self._call_sk(prior_call_id),
                        owner_binding=owner_binding,
                    )
                    if prior is None or prior.get("state") == "IN_FLIGHT":
                        raise LedgerDenied("CAPABILITY_READ_RETRY_REQUIRES_SAME_CALL")
                    prior_result = _json_mapping(
                        prior.get("resultJson"), "prior capability result"
                    )
                    if prior_result.get("status") == "FAILED_RETRYABLE":
                        raise LedgerDenied("CAPABILITY_READ_RETRY_REQUIRES_SAME_CALL")
                    logical_action = self._update(
                        pk=pk,
                        sk=logical_sk,
                        expression=(
                            "SET #call_id = :call_id, #ttl = :ttl, "
                            "#version = :next"
                        ),
                        condition="#call_id = :prior AND #version = :expected",
                        names={
                            "#call_id": "callId",
                            "#ttl": "ttl",
                            "#version": "version",
                        },
                        values={
                            ":call_id": call.call_id,
                            ":ttl": max(logical["ttl"], retention_ttl),
                            ":next": logical["version"] + 1,
                            ":prior": prior_call_id,
                            ":expected": logical["version"],
                        },
                    )

            pack_counts = {} if turn is None else dict(turn.get("packCounts", {}))
            call_count = 0 if turn is None else turn.get("callCount")
            if not isinstance(call_count, int) or any(
                not isinstance(value, int) for value in pack_counts.values()
            ):
                raise LedgerDenied("CAPABILITY_LEDGER_CORRUPT")
            if (
                call_count >= grant.max_calls
                or pack_counts.get(pack_id, 0) >= pack_max_calls
            ):
                raise LedgerDenied("CAPABILITY_CALL_BUDGET_EXCEEDED")
            pack_counts[pack_id] = pack_counts.get(pack_id, 0) + 1

            if turn is None:
                turn_action = self._put(
                    {
                        "PK": pk,
                        "SK": turn_sk,
                        "grantBinding": grant_binding,
                        "callCount": 1,
                        "packCounts": pack_counts,
                        "version": 1,
                    },
                    owner_binding=owner_binding,
                    ttl=retention_ttl,
                )
            else:
                turn_ttl = max(turn["ttl"], retention_ttl)
                turn_action = self._update(
                    pk=pk,
                    sk=turn_sk,
                    expression=(
                        "SET #call_count = :call_count, #pack_counts = :pack_counts, "
                        "#ttl = :ttl, #version = :next"
                    ),
                    condition="#version = :expected",
                    names={
                        "#call_count": "callCount",
                        "#pack_counts": "packCounts",
                        "#ttl": "ttl",
                        "#version": "version",
                    },
                    values={
                        ":call_count": call_count + 1,
                        ":pack_counts": pack_counts,
                        ":ttl": turn_ttl,
                        ":next": turn["version"] + 1,
                        ":expected": turn["version"],
                    },
                )
            actions = [
                turn_action,
                self._put(
                    {
                        "PK": pk,
                        "SK": call_sk,
                        "tenantBinding": tenant,
                        "grantBinding": grant_binding,
                        "logicalKey": logical_key,
                        "callJson": call_json,
                        "attempts": 1,
                        "state": "IN_FLIGHT",
                        "version": 1,
                    },
                    owner_binding=owner_binding,
                    ttl=retention_ttl,
                ),
                self._put(
                    {
                        "PK": pk,
                        "SK": tool_sk,
                        "operationId": call.operation_id,
                        "argsHash": call.args_hash,
                        "callId": call.call_id,
                        "version": 1,
                    },
                    owner_binding=owner_binding,
                    ttl=retention_ttl,
                ),
                logical_action
                or self._put(
                    {
                        "PK": pk,
                        "SK": logical_sk,
                        "callId": call.call_id,
                        "version": 1,
                    },
                    owner_binding=owner_binding,
                    ttl=retention_ttl,
                ),
            ]
            if self._transact(
                actions,
                user_id=grant.sub,
                owner_binding=owner_binding,
            ):
                return LedgerClaim(LedgerDisposition.NEW)
        raise LedgerDenied("CAPABILITY_LEDGER_CONTENTION")

    def complete(
        self,
        *,
        call: CapabilityCallV1,
        grant: TurnCapabilityGrantV1,
        result: CapabilityResultV1,
    ) -> None:
        if (
            not isinstance(call, CapabilityCallV1)
            or not isinstance(grant, TurnCapabilityGrantV1)
            or not isinstance(result, CapabilityResultV1)
        ):
            raise TypeError("ledger completion requires validated contracts")
        result.validate_against_call(call)
        tenant = derive_tenant_binding(grant)
        owner_binding = derive_deletion_subject_binding(grant.sub)
        grant_binding = canonical_sha256(grant.to_mapping())
        call_json = canonical_json_bytes(call.to_mapping()).decode("utf-8")
        result_json = canonical_json_bytes(result.to_mapping()).decode("utf-8")
        pk, sk = self._pk(grant.sub), self._call_sk(call.call_id)
        for _ in range(4):
            entry = self._get(pk, sk, owner_binding=owner_binding)
            if entry is None or entry.get("callJson") != call_json:
                raise LedgerDenied("CAPABILITY_CALL_NOT_CLAIMED")
            if (
                entry.get("tenantBinding") != tenant
                or entry.get("grantBinding") != grant_binding
            ):
                raise LedgerDenied("CAPABILITY_GRANT_BINDING_MISMATCH")
            if entry.get("state") != "IN_FLIGHT":
                if entry.get("resultJson") == result_json:
                    return
                raise LedgerDenied("CAPABILITY_RESULT_CONFLICT")
            action = self._update(
                pk=pk,
                sk=sk,
                expression="SET #state = :state, #result = :result, #version = :next",
                condition="#version = :expected",
                names={
                    "#state": "state",
                    "#result": "resultJson",
                    "#version": "version",
                },
                values={
                    ":state": "COMPLETE",
                    ":result": result_json,
                    ":next": entry["version"] + 1,
                    ":expected": entry["version"],
                },
            )
            if self._transact(
                [action],
                user_id=grant.sub,
                owner_binding=owner_binding,
            ):
                return
        raise LedgerDenied("CAPABILITY_LEDGER_CONTENTION")


__all__ = [
    "DynamoAdmissionRepository",
    "DynamoCapabilityLedger",
    "DynamoTurnAuthorityRepository",
]
