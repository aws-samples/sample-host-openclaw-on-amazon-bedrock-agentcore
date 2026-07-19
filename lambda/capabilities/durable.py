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

_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()
_CONDITIONAL_ERRORS = frozenset(
    {"ConditionalCheckFailedException", "TransactionCanceledException"}
)
_RETRY_MODES = frozenset({"READ_ONLY", "IDEMPOTENT", "DEDUPE_KEY_REQUIRED"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
_CALL_ID = re.compile(r"call_[0-9a-f]{64}")
_MAX_TARGET_GRANTS = 64


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

    def _record(self, pk: str, sk: str) -> dict[str, Any] | None:
        item = self._get(pk, sk)
        if item is None:
            return None
        self._validate_item(item, pk, sk)
        return _json_mapping(item["recordJson"], "live authority")

    @staticmethod
    def _validate_item(item: Mapping[str, Any], pk: str, sk: str) -> None:
        if set(item) != {"PK", "SK", "recordJson", "version"}:
            raise RuntimeError("live authority item has unexpected fields")
        if item["PK"] != pk or item["SK"] != sk:
            raise RuntimeError("live authority item key is inconsistent")
        if (
            isinstance(item["version"], bool)
            or not isinstance(item["version"], int)
            or item["version"] < 1
        ):
            raise RuntimeError("live authority item version is invalid")

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
        return self._required_flag(f"USER#{user_id}", "DELETION")

    def strong_read_user(self, user_id: str) -> Mapping[str, Any] | None:
        return self._record(f"USER#{user_id}", "PROFILE")

    def strong_read_session(self, session_id: str) -> Mapping[str, Any] | None:
        return self._record(f"SESSION#{session_id}", "PROFILE")

    def strong_read_runtime(
        self, runtime_arn: str, runtime_qualifier: str
    ) -> Mapping[str, Any] | None:
        return self._record(f"RUNTIME#{runtime_arn}", runtime_qualifier)

    def strong_read_installation(
        self, user_id: str, pack_id: str
    ) -> CapabilityInstallationV1 | Mapping[str, Any] | None:
        return self._record(f"USER#{user_id}", f"INSTALL#{pack_id}")

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
            pk, sk = self._target_key(tenant_binding, grant.target_hash)
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
                                "version": 1,
                            }
                        ),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                }
            )
        if not actions:
            return
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
                tenant_binding,
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
    def _target_key(tenant_binding: str, target_hash: str) -> tuple[str, str]:
        if (
            not isinstance(tenant_binding, str)
            or _SHA256.fullmatch(tenant_binding) is None
            or not isinstance(target_hash, str)
            or _SHA256.fullmatch(target_hash) is None
        ):
            raise TypeError("target state requires exact tenant and target bindings")
        return f"TENANT#{tenant_binding}", f"TARGET#{target_hash}"

    def strong_read_target_grant(
        self, tenant_binding: str, target_hash: str
    ) -> LiveTargetGrant | None:
        pk, sk = self._target_key(tenant_binding, target_hash)
        record = self._record(pk, sk)
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
        tenant_binding: str,
        target_hash: str,
        current_request_id: str,
        call_id: str,
    ) -> bool:
        pk, sk = self._target_key(tenant_binding, target_hash)
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
            self._validate_item(item, pk, sk)
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
    def _pk(tenant_binding: str) -> str:
        return f"TENANT#{tenant_binding}"

    @staticmethod
    def _turn_sk(call: CapabilityCallV1) -> str:
        return f"TURN#{call.invocation_id}"

    @staticmethod
    def _call_sk(call_id: str) -> str:
        return f"CALL#{call_id}"

    @staticmethod
    def _tool_sk(call: CapabilityCallV1) -> str:
        return f"TOOL#{call.invocation_id}#{call.tool_use_id}"

    @staticmethod
    def _logical_sk(call: CapabilityCallV1, logical_key: str) -> str:
        return f"LOGICAL#{call.invocation_id}#{logical_key}"

    def _get(self, pk: str, sk: str) -> dict[str, Any] | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=_serialize_item({"PK": pk, "SK": sk}),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        return None if raw is None else _deserialize_item(raw)

    def _put(self, item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "Put": {
                "TableName": self._table_name,
                "Item": _serialize_item(item),
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

    def _transact(self, actions: list[dict[str, Any]]) -> bool:
        try:
            self._client.transact_write_items(TransactItems=actions)
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

    def begin(
        self,
        *,
        call: CapabilityCallV1,
        grant: TurnCapabilityGrantV1,
        pack_id: str,
        pack_max_calls: int,
        retry_mode: str,
    ) -> LedgerClaim:
        self._validate_begin(call, grant, pack_id, pack_max_calls, retry_mode)
        tenant = derive_tenant_binding(grant)
        grant_binding = canonical_sha256(grant.to_mapping())
        logical_key = derive_logical_call_key(call, tenant)
        pk = self._pk(tenant)
        call_json = canonical_json_bytes(call.to_mapping()).decode("utf-8")
        turn_sk = self._turn_sk(call)
        call_sk = self._call_sk(call.call_id)
        tool_sk = self._tool_sk(call)
        logical_sk = self._logical_sk(call, logical_key)

        for _ in range(4):
            turn = self._get(pk, turn_sk)
            if turn is not None and turn.get("grantBinding") != grant_binding:
                raise LedgerDenied("CAPABILITY_GRANT_BINDING_MISMATCH")

            tool = self._get(pk, tool_sk)
            if tool is not None:
                expected_tool = (call.operation_id, call.args_hash, call.call_id)
                actual_tool = (
                    tool.get("operationId"),
                    tool.get("argsHash"),
                    tool.get("callId"),
                )
                if actual_tool != expected_tool:
                    raise LedgerDenied("CAPABILITY_ARGUMENT_MUTATION")

            entry = self._get(pk, call_sk)
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
                    if self._transact([retry]):
                        return LedgerClaim(LedgerDisposition.RETRY)
                    continue
                return LedgerClaim(LedgerDisposition.CACHED, result)

            if tool is not None:
                raise LedgerDenied("CAPABILITY_LEDGER_CORRUPT")

            logical = self._get(pk, logical_sk)
            logical_action = None
            if logical is not None:
                prior_call_id = logical.get("callId")
                if prior_call_id != call.call_id:
                    if retry_mode != "READ_ONLY":
                        return LedgerClaim(LedgerDisposition.LOGICAL_FENCE)
                    prior = self._get(pk, self._call_sk(prior_call_id))
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
                        expression="SET #call_id = :call_id, #version = :next",
                        condition="#call_id = :prior AND #version = :expected",
                        names={"#call_id": "callId", "#version": "version"},
                        values={
                            ":call_id": call.call_id,
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
                    }
                )
            else:
                turn_action = self._update(
                    pk=pk,
                    sk=turn_sk,
                    expression=(
                        "SET #call_count = :call_count, #pack_counts = :pack_counts, "
                        "#version = :next"
                    ),
                    condition="#version = :expected",
                    names={
                        "#call_count": "callCount",
                        "#pack_counts": "packCounts",
                        "#version": "version",
                    },
                    values={
                        ":call_count": call_count + 1,
                        ":pack_counts": pack_counts,
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
                    }
                ),
                self._put(
                    {
                        "PK": pk,
                        "SK": tool_sk,
                        "operationId": call.operation_id,
                        "argsHash": call.args_hash,
                        "callId": call.call_id,
                        "version": 1,
                    }
                ),
                logical_action
                or self._put(
                    {
                        "PK": pk,
                        "SK": logical_sk,
                        "callId": call.call_id,
                        "version": 1,
                    }
                ),
            ]
            if self._transact(actions):
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
        grant_binding = canonical_sha256(grant.to_mapping())
        call_json = canonical_json_bytes(call.to_mapping()).decode("utf-8")
        result_json = canonical_json_bytes(result.to_mapping()).decode("utf-8")
        pk, sk = self._pk(tenant), self._call_sk(call.call_id)
        for _ in range(4):
            entry = self._get(pk, sk)
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
            if self._transact([action]):
                return
        raise LedgerDenied("CAPABILITY_LEDGER_CONTENTION")


__all__ = ["DynamoAdmissionRepository", "DynamoCapabilityLedger"]
