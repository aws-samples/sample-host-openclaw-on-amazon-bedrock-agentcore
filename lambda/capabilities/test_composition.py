"""Offline production-composition proof for the capability gateway."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError
import pytest

from capabilities.catalog import compile_catalog
from capabilities.contracts import CapabilityInstallationV1
from capabilities.contracts import TurnCapabilityGrantV1
from capabilities.test_gateway import (
    CALLER_ARN,
    NOW,
    RELEASE_COMMIT,
    _call,
    _catalog,
    _turn_grant,
)

ROOT = Path(__file__).resolve().parents[2]
SERIALIZER = TypeSerializer()
DESERIALIZER = TypeDeserializer()


def _serialize(value):
    return {key: SERIALIZER.serialize(item) for key, item in value.items()}


def _deserialize(value):
    return {key: DESERIALIZER.deserialize(item) for key, item in value.items()}


class MemoryDynamoClient:
    """Small low-level Dynamo model used only for offline composition proof."""

    def __init__(self):
        self.items = {}
        self.get_calls = []
        self.transact_calls = []
        self.update_calls = []

    @staticmethod
    def _key(raw):
        decoded = _deserialize(raw)
        return decoded["PK"], decoded["SK"]

    def put(self, pk, sk, **values):
        self.items[(pk, sk)] = {"PK": pk, "SK": sk, **deepcopy(values)}

    def get_item(self, **kwargs):
        self.get_calls.append(deepcopy(kwargs))
        item = self.items.get(self._key(kwargs["Key"]))
        return {} if item is None else {"Item": _serialize(deepcopy(item))}

    @staticmethod
    def _condition_matches(item, action):
        condition = action.get("ConditionExpression")
        if condition is None:
            return True
        if condition == "attribute_not_exists(PK)":
            return item is None
        names = action.get("ExpressionAttributeNames", {})
        values = _deserialize(action.get("ExpressionAttributeValues", {}))
        clauses = [clause.strip() for clause in condition.split(" AND ")]
        if item is None:
            return False
        for clause in clauses:
            left, right = [part.strip() for part in clause.split("=", 1)]
            if item.get(names[left]) != values[right]:
                return False
        return True

    @staticmethod
    def _apply_update(item, action):
        updated = deepcopy(item)
        names = action.get("ExpressionAttributeNames", {})
        values = _deserialize(action.get("ExpressionAttributeValues", {}))
        expression = action["UpdateExpression"]
        set_part = expression.removeprefix("SET ").split(" REMOVE ", 1)[0]
        for assignment in set_part.split(","):
            left, right = [part.strip() for part in assignment.split("=", 1)]
            updated[names[left]] = deepcopy(values[right])
        if " REMOVE " in expression:
            for token in expression.split(" REMOVE ", 1)[1].split(","):
                updated.pop(names[token.strip()], None)
        return updated

    def transact_write_items(self, **kwargs):
        self.transact_calls.append(deepcopy(kwargs))
        candidate = deepcopy(self.items)
        for wrapped in kwargs["TransactItems"]:
            kind, action = next(iter(wrapped.items()))
            if kind == "Put":
                item = _deserialize(action["Item"])
                key = item["PK"], item["SK"]
                if not self._condition_matches(candidate.get(key), action):
                    raise ClientError(
                        {"Error": {"Code": "TransactionCanceledException"}},
                        "TransactWriteItems",
                    )
                candidate[key] = item
            elif kind == "Update":
                key = self._key(action["Key"])
                current = candidate.get(key)
                if not self._condition_matches(current, action):
                    raise ClientError(
                        {"Error": {"Code": "TransactionCanceledException"}},
                        "TransactWriteItems",
                    )
                candidate[key] = self._apply_update(current, action)
            else:  # pragma: no cover - production emits only reviewed actions
                raise AssertionError(kind)
        self.items = candidate
        return {}

    def update_item(self, **kwargs):
        self.update_calls.append(deepcopy(kwargs))
        key = self._key(kwargs["Key"])
        current = self.items.get(key)
        if not self._condition_matches(current, kwargs):
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}},
                "UpdateItem",
            )
        self.items[key] = self._apply_update(current, kwargs)
        return {}


def _artifact_copy(tmp_path: Path) -> Path:
    target = tmp_path / "artifacts"
    shutil.copytree(ROOT / "specs" / "capabilities", target)
    return target


def _environment(catalog_digest: str) -> dict[str, str]:
    return {
        "AWS_REGION": "eu-west-1",
        "CAPABILITY_STATE_TABLE_NAME": "capability-state",
        "CAPABILITY_RELEASE_COMMIT": RELEASE_COMMIT,
        "CAPABILITY_CATALOG_DIGEST": catalog_digest,
        "CAPABILITY_ALLOWED_CALLER_ARN": CALLER_ARN,
    }


def _seed_authority(client: MemoryDynamoClient, catalog) -> None:
    user_id = "user_alpha"
    session_id = "session_12345678"
    runtime_arn = "arn:aws:bedrock-agentcore:eu-west-1:000000000000:runtime/example"
    runtime_qualifier = f"release_{RELEASE_COMMIT}"
    installation = CapabilityInstallationV1.from_mapping(
        {
            "schema": CapabilityInstallationV1.SCHEMA,
            "userId": user_id,
            "packId": "schedule.list",
            "catalogDigest": catalog.catalog_digest,
            "state": "ENABLED",
            "policyRevision": 1,
            "connectionRefs": [],
            "killSwitch": False,
        }
    )
    records = [
        ("CONTROL", "GLOBAL", {"enabled": False}),
        (f"USER#{user_id}", "DELETION", {"enabled": False}),
        (
            f"USER#{user_id}",
            "PROFILE",
            {"userId": user_id, "state": "ACTIVE", "deletionFence": False},
        ),
        (
            f"SESSION#{session_id}",
            "PROFILE",
            {
                "sessionId": session_id,
                "userId": user_id,
                "runtimeArn": runtime_arn,
                "runtimeQualifier": runtime_qualifier,
                "state": "ACTIVE",
            },
        ),
        (
            f"RUNTIME#{runtime_arn}",
            runtime_qualifier,
            {
                "runtimeArn": runtime_arn,
                "runtimeQualifier": runtime_qualifier,
                "sessionId": session_id,
                "userId": user_id,
                "releaseCommit": RELEASE_COMMIT,
                "catalogDigest": catalog.catalog_digest,
                "state": "READY",
            },
        ),
        (
            f"USER#{user_id}",
            "INSTALL#schedule.list",
            installation.to_mapping(),
        ),
    ]
    for pk, sk, payload in records:
        client.put(
            pk,
            sk,
            recordJson=json.dumps(payload, sort_keys=True, separators=(",", ":")),
            version=1,
        )


def test_cold_start_recompiles_packaged_catalog_and_rejects_one_byte_drift(
    tmp_path,
):
    from capabilities.composition import load_packaged_catalog

    artifacts = _artifact_copy(tmp_path)
    _, catalog = compile_catalog(RELEASE_COMMIT, artifacts / "schemas")
    env = _environment(catalog.catalog_digest)

    assert load_packaged_catalog(env, artifact_root=artifacts) == catalog

    schema = artifacts / "schemas" / "po-schedule-list-input.json"
    schema.write_bytes(schema.read_bytes() + b" ")
    with pytest.raises(Exception, match="catalog|artifact|envelope|canonical"):
        load_packaged_catalog(env, artifact_root=artifacts)


def test_offline_handler_composes_strong_dynamo_state_and_keeps_adapters_disabled(
    tmp_path, monkeypatch
):
    from capabilities import composition
    from capabilities.durable import (
        DynamoAdmissionRepository,
        DynamoCapabilityLedger,
    )
    from capabilities.gateway import lambda_handler

    artifacts = _artifact_copy(tmp_path)
    catalog = _catalog()
    client = MemoryDynamoClient()
    _seed_authority(client, catalog)
    production = composition.build_production_composition(
        env=_environment(catalog.catalog_digest),
        artifact_root=artifacts,
        dynamodb_client=client,
        clock=lambda: NOW,
    )
    monkeypatch.setattr(
        composition,
        "get_production_composition",
        lambda: production,
    )
    call = _call(catalog, "schedule.list", {})
    event = {
        "schema": "personal-operator.capability-relay-envelope.v1",
        "grant": _turn_grant(catalog),
        "call": call.to_mapping(),
    }

    result = lambda_handler(event, None)

    assert isinstance(production.repository, DynamoAdmissionRepository)
    assert isinstance(production.ledger, DynamoCapabilityLedger)
    assert result["status"] == "DENIED"
    assert result["errorCode"] == "ADAPTER_DISABLED"
    assert client.get_calls
    assert all(call["ConsistentRead"] is True for call in client.get_calls)
    assert client.transact_calls
    assert "callerArn" not in event


def test_composition_rejects_wrong_region_release_digest_and_event_shape(tmp_path):
    from capabilities.composition import build_production_composition

    artifacts = _artifact_copy(tmp_path)
    catalog = _catalog()
    client = MemoryDynamoClient()
    valid = _environment(catalog.catalog_digest)
    for changes in (
        {"AWS_REGION": "us-east-1"},
        {"CAPABILITY_RELEASE_COMMIT": "f" * 40},
        {"CAPABILITY_CATALOG_DIGEST": "f" * 64},
    ):
        with pytest.raises(Exception):
            build_production_composition(
                env={**valid, **changes},
                artifact_root=artifacts,
                dynamodb_client=client,
                clock=lambda: NOW,
            )


def test_durable_ledger_isolates_tenants_fences_mutations_and_caps_read_retry():
    from capabilities.durable import DynamoCapabilityLedger
    from capabilities.gateway import _ambiguous
    from capabilities.ledger import LedgerDenied, LedgerDisposition

    catalog = _catalog()
    client = MemoryDynamoClient()
    ledger = DynamoCapabilityLedger(client=client, table_name="capability-state")
    grant_alpha = TurnCapabilityGrantV1.from_mapping(_turn_grant(catalog))
    grant_beta = TurnCapabilityGrantV1.from_mapping(
        _turn_grant(
            catalog,
            overrides={
                "sub": "user_beta",
                "sessionId": "session_87654321",
                "nonce": "nonce_876543210abcdef",
            },
        )
    )
    same_call = _call(catalog, "schedule.list", {})

    assert (
        ledger.begin(
            call=same_call,
            grant=grant_alpha,
            pack_id="schedule.list",
            pack_max_calls=8,
            retry_mode="READ_ONLY",
        ).disposition
        is LedgerDisposition.NEW
    )
    assert (
        ledger.begin(
            call=same_call,
            grant=grant_beta,
            pack_id="schedule.list",
            pack_max_calls=8,
            retry_mode="READ_ONLY",
        ).disposition
        is LedgerDisposition.NEW
    )

    mutation = _call(
        catalog,
        "workspace.file.write",
        {"path": "notes/a.md", "content": "hello"},
        tool_use_id="tooluse_11111111",
    )
    fresh_mutation = _call(
        catalog,
        "workspace.file.write",
        {"path": "notes/a.md", "content": "hello"},
        tool_use_id="tooluse_22222222",
    )
    assert (
        ledger.begin(
            call=mutation,
            grant=grant_alpha,
            pack_id="workspace.file-write",
            pack_max_calls=8,
            retry_mode="IDEMPOTENT",
        ).disposition
        is LedgerDisposition.NEW
    )
    assert (
        ledger.begin(
            call=fresh_mutation,
            grant=grant_alpha,
            pack_id="workspace.file-write",
            pack_max_calls=8,
            retry_mode="IDEMPOTENT",
        ).disposition
        is LedgerDisposition.LOGICAL_FENCE
    )

    read = _call(
        catalog,
        "workspace.file.read",
        {"path": "notes/a.md"},
        tool_use_id="tooluse_33333333",
    )
    fresh_read = _call(
        catalog,
        "workspace.file.read",
        {"path": "notes/a.md"},
        tool_use_id="tooluse_44444444",
    )
    begin_args = {
        "call": read,
        "grant": grant_alpha,
        "pack_id": "workspace.file-read",
        "pack_max_calls": 8,
        "retry_mode": "READ_ONLY",
    }
    assert ledger.begin(**begin_args).disposition is LedgerDisposition.NEW
    retryable = _ambiguous(read, "READ_ONLY", "SYNTHETIC")
    ledger.complete(call=read, grant=grant_alpha, result=retryable)
    with pytest.raises(
        LedgerDenied,
        match="CAPABILITY_READ_RETRY_REQUIRES_SAME_CALL",
    ):
        ledger.begin(
            **{
                **begin_args,
                "call": fresh_read,
            }
        )
    assert ledger.begin(**begin_args).disposition is LedgerDisposition.RETRY
    ledger.complete(call=read, grant=grant_alpha, result=retryable)
    assert ledger.begin(**begin_args).disposition is LedgerDisposition.RETRY_EXHAUSTED
    assert all(call["ConsistentRead"] is True for call in client.get_calls)
