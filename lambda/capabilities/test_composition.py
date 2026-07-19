"""Offline production-composition proof for the capability gateway."""

from __future__ import annotations

from copy import deepcopy
import importlib
import json
from pathlib import Path
import shutil
import sys
from types import ModuleType

# Some router tests replace ``sys.modules["boto3"]`` with a bare MagicMock at
# collection time. Restore the installed package before importing DynamoDB's
# serializers so this test remains order-independent in the aggregate suite.
if any(
    (loaded_module := sys.modules.get(package_name)) is not None
    and not isinstance(loaded_module, ModuleType)
    for package_name in ("boto3", "botocore")
):
    for module_name in tuple(sys.modules):
        if module_name in {"boto3", "botocore"} or module_name.startswith(
            ("boto3.", "botocore.")
        ):
            sys.modules.pop(module_name, None)
    importlib.invalidate_caches()

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
    _target_grant,
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


def _seed_installation(client: MemoryDynamoClient, catalog, pack_id: str) -> None:
    installation = CapabilityInstallationV1.from_mapping(
        {
            "schema": CapabilityInstallationV1.SCHEMA,
            "userId": "user_alpha",
            "packId": pack_id,
            "catalogDigest": catalog.catalog_digest,
            "state": "ENABLED",
            "policyRevision": 1,
            "connectionRefs": [],
            "killSwitch": False,
        }
    )
    client.put(
        "USER#user_alpha",
        f"INSTALL#{pack_id}",
        recordJson=json.dumps(
            installation.to_mapping(), sort_keys=True, separators=(",", ":")
        ),
        version=1,
    )


def _seed_authority(client: MemoryDynamoClient, catalog) -> None:
    user_id = "user_alpha"
    session_id = "session_12345678"
    runtime_arn = "arn:aws:bedrock-agentcore:eu-west-1:000000000000:runtime/example"
    runtime_qualifier = f"release_{RELEASE_COMMIT}"
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
    ]
    for pk, sk, payload in records:
        client.put(
            pk,
            sk,
            recordJson=json.dumps(payload, sort_keys=True, separators=(",", ":")),
            version=1,
        )
    _seed_installation(client, catalog, "schedule.list")


class SequencedAdapter:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    def invoke(self, admitted):
        self.calls.append(admitted.call.call_id)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def _durable_web_gateway(client: MemoryDynamoClient, catalog, adapter):
    from capabilities.durable import (
        DynamoAdmissionRepository,
        DynamoCapabilityLedger,
    )
    from capabilities.gateway import CapabilityGateway

    return CapabilityGateway(
        catalog=catalog,
        repository=DynamoAdmissionRepository(
            client=client,
            table_name="capability-state",
        ),
        ledger=DynamoCapabilityLedger(
            client=client,
            table_name="capability-state",
        ),
        adapters={"web.exact.read": adapter},
        allowed_caller_arn=CALLER_ARN,
        clock=lambda: NOW,
    )


def _seed_web_target(client: MemoryDynamoClient, catalog):
    from capabilities.durable import DynamoAdmissionRepository

    target = _target_grant(max_uses=1)
    _seed_installation(client, catalog, "web.exact-read")
    DynamoAdmissionRepository(
        client=client,
        table_name="capability-state",
    ).persist_target_grants(
        tenant_id="user_alpha",
        current_request_id="invocation_12345678",
        grants=[target],
    )
    return target


def _web_success():
    from capabilities.gateway import AdapterOutcome

    return AdapterOutcome(
        status="SUCCEEDED",
        data={
            "canonicalUrl": "https://example.com/exact",
            "contentDigest": "c" * 64,
            "retrievedAt": NOW,
            "sourceRef": "source_12345678",
            "text": "reviewed public text",
        },
        provenance_refs=("source_12345678",),
    )


def _target_record(client, target):
    item = client.items[
        (f"TENANT#{target.tenant_binding}", f"TARGET#{target.target_hash}")
    ]
    return json.loads(item["recordJson"])


def _turn_record(client):
    return next(
        item
        for (pk, sk), item in client.items.items()
        if pk.startswith("TENANT#") and sk == "TURN#invocation_12345678"
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


def test_durable_target_claim_allows_exact_cached_response_loss_recovery_only():
    catalog = _catalog()
    client = MemoryDynamoClient()
    _seed_authority(client, catalog)
    target = _seed_web_target(client, catalog)
    adapter = SequencedAdapter(_web_success())
    gateway = _durable_web_gateway(client, catalog, adapter)
    call = _call(
        catalog,
        "web.exact.read",
        {"url": "https://example.com/exact"},
    )
    grant = _turn_grant(catalog, target_grant=target, max_calls=1)
    iam = {"callerArn": CALLER_ARN, "turnGrant": grant}

    ignored_response = gateway.invoke(call, iam)
    recovered = gateway.invoke(call, iam)
    wrong_grant = gateway.invoke(
        call,
        {
            "callerArn": CALLER_ARN,
            "turnGrant": _turn_grant(
                catalog,
                target_grant=target,
                max_calls=1,
                overrides={"nonce": "nonce_876543210abcdef"},
            ),
        },
    )
    fresh_call = gateway.invoke(
        _call(
            catalog,
            "web.exact.read",
            {"url": "https://example.com/exact"},
            tool_use_id="tooluse_87654321",
        ),
        iam,
    )

    assert ignored_response.status == "SUCCEEDED"
    assert recovered.to_bytes() == ignored_response.to_bytes()
    assert wrong_grant.status == "DENIED"
    assert wrong_grant.error_code == "CAPABILITY_GRANT_BINDING_MISMATCH"
    assert fresh_call.status == "DENIED"
    assert fresh_call.error_code == "TARGET_GRANT_EXHAUSTED"
    assert adapter.calls == [call.call_id]
    assert _target_record(client, target) == {
        "grant": target.to_mapping(),
        "uses": 1,
        "claimedCallIds": [call.call_id],
    }
    assert _turn_record(client)["callCount"] == 1
    assert _turn_record(client)["packCounts"] == {"web.exact-read": 1}


def test_durable_target_rows_are_partitioned_by_the_grants_tenant_binding():
    catalog = _catalog()
    client = MemoryDynamoClient()

    target = _seed_web_target(client, catalog)

    assert (
        f"TENANT#{target.tenant_binding}",
        f"TARGET#{target.target_hash}",
    ) in client.items
    assert (f"TARGET#{target.target_hash}", "GRANT") not in client.items


def test_durable_target_persistence_claim_and_cached_recovery_keep_exact_bindings():
    from capabilities.admission import AdmissionDenied
    from capabilities.contracts import derive_target_tenant_binding
    from capabilities.durable import DynamoAdmissionRepository

    client = MemoryDynamoClient()
    repository = DynamoAdmissionRepository(
        client=client,
        table_name="capability-state",
    )
    target = _target_grant(max_uses=1)

    assert callable(getattr(repository, "persist_target_grants", None))
    repository.persist_target_grants(
        tenant_id="user_alpha",
        current_request_id="invocation_12345678",
        grants=[target],
    )
    repository.persist_target_grants(
        tenant_id="user_alpha",
        current_request_id="invocation_12345678",
        grants=[target],
    )

    tenant_binding = derive_target_tenant_binding("user_alpha")
    recovered = repository.strong_read_target_grant(
        tenant_binding,
        target.target_hash,
    )
    assert recovered is not None
    assert recovered.grant.to_bytes() == target.to_bytes()
    assert recovered.uses == 0

    with pytest.raises(ValueError, match="request"):
        repository.persist_target_grants(
            tenant_id="user_alpha",
            current_request_id="invocation_other_1234",
            grants=[target],
        )
    with pytest.raises(ValueError, match="tenant"):
        repository.persist_target_grants(
            tenant_id="user_beta",
            current_request_id="invocation_12345678",
            grants=[target],
        )

    call_id = "call_" + "a" * 64
    with pytest.raises(AdmissionDenied) as request_mismatch:
        repository.claim_target_use(
            tenant_binding,
            target.target_hash,
            "invocation_other_1234",
            call_id,
        )
    assert request_mismatch.value.code == "TARGET_GRANT_REQUEST_MISMATCH"
    assert repository.claim_target_use(
        derive_target_tenant_binding("user_beta"),
        target.target_hash,
        "invocation_12345678",
        call_id,
    ) is False
    assert repository.claim_target_use(
        tenant_binding,
        target.target_hash,
        "invocation_12345678",
        call_id,
    ) is True
    update_count = len(client.update_calls)
    assert repository.claim_target_use(
        tenant_binding,
        target.target_hash,
        "invocation_12345678",
        call_id,
    ) is True
    assert len(client.update_calls) == update_count

    claimed = repository.strong_read_target_grant(
        tenant_binding,
        target.target_hash,
    )
    assert claimed is not None
    assert claimed.uses == 1
    assert claimed.claimed_call_ids == (call_id,)
    target_key = (
        f"TENANT#{tenant_binding}",
        f"TARGET#{target.target_hash}",
    )
    client.items[target_key]["unexpected"] = "must fail closed"
    with pytest.raises(RuntimeError, match="unexpected fields"):
        repository.claim_target_use(
            tenant_binding,
            target.target_hash,
            "invocation_12345678",
            call_id,
        )
    client.items[target_key].pop("unexpected")
    with pytest.raises(RuntimeError, match="persistence conflict"):
        repository.persist_target_grants(
            tenant_id="user_alpha",
            current_request_id="invocation_12345678",
            grants=[target],
        )


def test_durable_cached_target_row_rejects_a_cross_tenant_partition_copy():
    from capabilities.admission import AdmissionDenied
    from capabilities.contracts import derive_target_tenant_binding
    from capabilities.durable import DynamoAdmissionRepository

    catalog = _catalog()
    client = MemoryDynamoClient()
    target = _seed_web_target(client, catalog)
    repository = DynamoAdmissionRepository(
        client=client,
        table_name="capability-state",
    )
    tenant_a = derive_target_tenant_binding("user_alpha")
    tenant_b = derive_target_tenant_binding("user_beta")
    source_key = (f"TENANT#{tenant_a}", f"TARGET#{target.target_hash}")
    copied = deepcopy(client.items[source_key])
    copied["PK"] = f"TENANT#{tenant_b}"
    client.items[(copied["PK"], copied["SK"])] = copied

    with pytest.raises(AdmissionDenied) as mismatch:
        repository.strong_read_target_grant(tenant_b, target.target_hash)

    assert mismatch.value.code == "TARGET_GRANT_TENANT_MISMATCH"


def test_durable_target_claim_allows_one_same_call_read_retry_without_recharge():
    catalog = _catalog()
    client = MemoryDynamoClient()
    _seed_authority(client, catalog)
    target = _seed_web_target(client, catalog)
    adapter = SequencedAdapter(TimeoutError("response lost"), _web_success())
    gateway = _durable_web_gateway(client, catalog, adapter)
    call = _call(
        catalog,
        "web.exact.read",
        {"url": "https://example.com/exact"},
    )
    iam = {
        "callerArn": CALLER_ARN,
        "turnGrant": _turn_grant(catalog, target_grant=target, max_calls=1),
    }

    retryable = gateway.invoke(call, iam)
    succeeded = gateway.invoke(call, iam)
    cached = gateway.invoke(call, iam)

    assert retryable.status == "FAILED_RETRYABLE"
    assert retryable.retry_policy == "SAFE_RETRY"
    assert succeeded.status == "SUCCEEDED"
    assert cached.to_bytes() == succeeded.to_bytes()
    assert adapter.calls == [call.call_id, call.call_id]
    assert _target_record(client, target)["uses"] == 1
    assert _target_record(client, target)["claimedCallIds"] == [call.call_id]
    assert _turn_record(client)["callCount"] == 1
    assert _turn_record(client)["packCounts"] == {"web.exact-read": 1}


def test_durable_target_claim_preserves_retry_exhaustion_and_fresh_call_denial():
    catalog = _catalog()
    client = MemoryDynamoClient()
    _seed_authority(client, catalog)
    target = _seed_web_target(client, catalog)
    adapter = SequencedAdapter(
        TimeoutError("first response lost"),
        TimeoutError("second response lost"),
    )
    gateway = _durable_web_gateway(client, catalog, adapter)
    call = _call(
        catalog,
        "web.exact.read",
        {"url": "https://example.com/exact"},
    )
    iam = {
        "callerArn": CALLER_ARN,
        "turnGrant": _turn_grant(catalog, target_grant=target, max_calls=1),
    }

    first = gateway.invoke(call, iam)
    second = gateway.invoke(call, iam)
    exhausted = gateway.invoke(call, iam)
    fresh = gateway.invoke(
        _call(
            catalog,
            "web.exact.read",
            {"url": "https://example.com/exact"},
            tool_use_id="tooluse_87654321",
        ),
        iam,
    )

    assert first.status == "FAILED_RETRYABLE"
    assert second.status == "FAILED_RETRYABLE"
    assert exhausted.status == "DENIED"
    assert exhausted.error_code == "CAPABILITY_READ_RETRY_EXHAUSTED"
    assert fresh.status == "DENIED"
    assert fresh.error_code == "TARGET_GRANT_EXHAUSTED"
    assert adapter.calls == [call.call_id, call.call_id]
    assert _target_record(client, target)["uses"] == 1
    assert _target_record(client, target)["claimedCallIds"] == [call.call_id]
    assert _turn_record(client)["callCount"] == 1
    assert _turn_record(client)["packCounts"] == {"web.exact-read": 1}


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
