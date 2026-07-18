"""Contract and release-catalog tests for Personal Operator v1.

These tests intentionally load the dependency-free modules from their Lambda
asset directory so they exercise the same import shape used by focused Lambda
tests and by later gateway work.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import sys

import pytest


CAPABILITIES_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = CAPABILITIES_DIR.parents[1]
SCHEMA_DIR = REPOSITORY_ROOT / "specs" / "capabilities" / "schemas"
SOURCE_CATALOG = SCHEMA_DIR.parent / "catalog-v1.json"
RELEASE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
SHA_A = "a" * 64
SHA_B = "b" * 64


def _load_modules():
    sys.path.insert(0, str(CAPABILITIES_DIR))
    try:
        contracts = importlib.import_module("contracts")
        catalog = importlib.import_module("catalog")
    finally:
        sys.path.pop(0)
    return contracts, catalog


def test_contract_module_exists_before_behavior_is_exercised():
    """Provides a clean test-first failure instead of a collection error."""

    assert (CAPABILITIES_DIR / "contracts.py").is_file(), (
        "contracts.py is absent: implement the frozen v1 contract boundary"
    )


def _documents(contracts):
    args = {"path": "notes/today.md"}
    args_hash = contracts.canonical_sha256(args)
    call_id = contracts.derive_call_id("invocation_12345678", "tooluse_12345678", args_hash)
    definition = {"message": "Review the plan", "runAt": 1_800_000_000}
    definition_hash = contracts.canonical_sha256(definition)
    return {
        "personal-operator.capability-installation.v1": {
            "schema": "personal-operator.capability-installation.v1",
            "userId": "user_alpha",
            "packId": "workspace.file-read",
            "catalogDigest": SHA_A,
            "state": "ENABLED",
            "policyRevision": 1,
            "connectionRefs": [],
            "killSwitch": False,
        },
        "personal-operator.turn-capability-grant.v1": {
            "schema": "personal-operator.turn-capability-grant.v1",
            "sub": "user_alpha",
            "sessionId": "session_12345678",
            "runtimeArn": "arn:aws:bedrock-agentcore:eu-west-1:000000000000:runtime/example",
            "runtimeQualifier": "release_0123456789abcdef0123456789abcdef01234567",
            "invocationId": "invocation_12345678",
            "releaseCommit": RELEASE_COMMIT,
            "catalogDigest": SHA_A,
            "allowedPackIds": ["workspace.file-read"],
            "allowedOperationIds": ["workspace.file.read"],
            "targetGrantHashes": [],
            "iat": 1_800_000_000,
            "exp": 1_800_000_300,
            "maxCalls": 4,
            "nonce": "nonce_1234567890abcdef",
        },
        "personal-operator.capability-call.v1": {
            "schema": "personal-operator.capability-call.v1",
            "callId": call_id,
            "invocationId": "invocation_12345678",
            "toolUseId": "tooluse_12345678",
            "toolName": "po_file_read",
            "arguments": args,
            "argsHash": args_hash,
        },
        "personal-operator.capability-result.v1": {
            "schema": "personal-operator.capability-result.v1",
            "callId": call_id,
            "status": "SUCCEEDED",
            "data": {"path": "notes/today.md", "content": "hello"},
            "provenanceRefs": ["workspace:notes/today.md"],
            "proposalRef": None,
            "receiptRef": None,
            "errorCode": None,
            "retryPolicy": "NONE",
        },
        "personal-operator.target-grant.v1": {
            "schema": "personal-operator.target-grant.v1",
            "targetHash": contracts.derive_target_hash(
                "https://example.com/exact", "GET", "SAME_HOST", "request_12345678"
            ),
            "normalizedTarget": "https://example.com/exact",
            "method": "GET",
            "redirectPolicy": "SAME_HOST",
            "expiresAt": 1_800_000_300,
            "maxUses": 1,
            "currentRequestId": "request_12345678",
        },
        "personal-operator.action-proposal.v1": {
            "schema": "personal-operator.action-proposal.v1",
            "proposalId": "proposal_12345678",
            "userId": "user_alpha",
            "capabilityId": "schedule.create",
            "resource": "schedule:schedule_12345678",
            "connectionRef": None,
            "arguments": definition,
            "argsHash": definition_hash,
            "revision": 1,
            "originatingInvocationId": "invocation_12345678",
            "approvalPolicy": "EXACT_ONE_TIME",
            "expiresAt": 1_800_000_300,
        },
        "personal-operator.effect-receipt.v1": {
            "schema": "personal-operator.effect-receipt.v1",
            "receiptId": "receipt_12345678",
            "capabilityId": "connector.gmail.send",
            "resource": "google:gmail:connection:connection_12345678",
            "argumentsHash": SHA_A,
            "providerEvidenceId": "provider_12345678",
            "providerEvidenceHash": SHA_B,
            "executedAt": 1_800_000_000,
            "reconciledAt": None,
        },
        "personal-operator.schedule-spec.v1": {
            "schema": "personal-operator.schedule-spec.v1",
            "scheduleId": "schedule_12345678",
            "userId": "user_alpha",
            "taskType": "REMINDER",
            "definition": definition,
            "definitionHash": definition_hash,
            "revision": 1,
            "state": "ENABLED",
            "timezone": "Europe/Tallinn",
            "nextRunAt": 1_800_000_000,
        },
        "personal-operator.schedule-occurrence.v1": {
            "schema": "personal-operator.schedule-occurrence.v1",
            "occurrenceId": contracts.derive_occurrence_id(
                "schedule_12345678", 1, 1_800_000_000
            ),
            "scheduleId": "schedule_12345678",
            "generation": 1,
            "occurrenceTime": 1_800_000_000,
            "status": "QUEUED",
        },
        "personal-operator.compute-job-spec.v1": {
            "schema": "personal-operator.compute-job-spec.v1",
            "jobId": "job_12345678",
            "userId": "user_alpha",
            "imageDigest": f"sha256:{SHA_A}",
            "command": {"mode": "ARGV", "value": ["python", "script.py"]},
            "inputFiles": [{"path": "script.py", "sha256": SHA_B, "size": 12}],
            "resourceProfile": "SMALL",
            "deadline": 1_800_000_300,
            "network": "NONE",
        },
        "personal-operator.compute-receipt.v1": {
            "schema": "personal-operator.compute-receipt.v1",
            "jobId": "job_12345678",
            "status": "SUCCEEDED",
            "imageDigest": f"sha256:{SHA_A}",
            "inputDigest": SHA_B,
            "outputFiles": [{"path": "result.txt", "sha256": SHA_A, "size": 5}],
            "startedAt": 1_800_000_000,
            "completedAt": 1_800_000_010,
            "errorCode": None,
        },
        "personal-operator.connector-manifest.v1": {
            "schema": "personal-operator.connector-manifest.v1",
            "connectorId": "google.gmail",
            "version": "1.0.0",
            "schemaDigest": SHA_A,
            "operations": [
                {
                    "operationId": "gmail.read",
                    "mode": "READ",
                    "inputSchemaDigest": SHA_A,
                    "outputSchemaDigest": SHA_B,
                }
            ],
            "credentialBoundary": "TRUSTED_ADAPTER",
        },
        "personal-operator.connector-connection.v1": {
            "schema": "personal-operator.connector-connection.v1",
            "userId": "user_alpha",
            "connectorId": "google.gmail",
            "connectionRef": "connection_12345678",
            "state": "CONNECTED",
            "consentRevision": 1,
            "deletionFence": False,
        },
        "personal-operator.portable-state-manifest.v2": {
            "schema": "personal-operator.portable-state-manifest.v2",
            "generation": "generation_12345678",
            "bundleHash": SHA_A,
            "objects": [
                {"path": "files/notes.md", "type": "FILE", "size": 5, "sha256": SHA_B}
            ],
            "excludedClasses": ["CREDENTIALS", "GRANTS", "PENDING_EFFECTS"],
            "createdAt": 1_800_000_000,
        },
        "personal-operator.import-plan.v1": {
            "schema": "personal-operator.import-plan.v1",
            "planId": "importplan_12345678",
            "userId": "user_alpha",
            "bundleHash": SHA_A,
            "baseGeneration": "generation_12345678",
            "objectCount": 1,
            "totalBytes": 5,
            "schedulesDisabled": True,
            "connectorsDisconnected": True,
            "effectsReplayable": False,
        },
        "personal-operator.import-receipt.v1": {
            "schema": "personal-operator.import-receipt.v1",
            "planId": "importplan_12345678",
            "userId": "user_alpha",
            "bundleHash": SHA_A,
            "state": "ACTIVATED",
            "activatedGeneration": "generation_abcdefgh",
            "importedAt": 1_800_000_100,
        },
    }


def _canonical(document):
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def test_every_named_contract_round_trips_as_its_frozen_type():
    contracts, _ = _load_modules()
    documents = _documents(contracts)
    assert set(documents) == set(contracts.CONTRACT_TYPES) - {
        "personal-operator.capability-catalog.v1"
    }

    for schema, document in documents.items():
        value = contracts.parse_canonical_json(_canonical(document), schema)
        assert isinstance(value, contracts.CONTRACT_TYPES[schema])
        assert value.to_mapping() == document
        assert value.to_bytes() == _canonical(document)
        with pytest.raises((FrozenInstanceError, AttributeError)):
            value._data = {}  # type: ignore[misc]
        with pytest.raises(TypeError):
            value.data["schema"] = "mutated"  # type: ignore[index]


def test_contract_construction_seals_nested_mutable_inputs_and_returns_copies():
    contracts, _ = _load_modules()
    document = _documents(contracts)["personal-operator.capability-call.v1"]
    value = contracts.CapabilityCallV1.from_mapping(document)
    document["arguments"]["path"] = "attacker.md"
    assert value.to_mapping()["arguments"] == {"path": "notes/today.md"}
    thawed = value.to_mapping()
    thawed["arguments"]["path"] = "also-attacker.md"
    assert value.to_mapping()["arguments"] == {"path": "notes/today.md"}


@pytest.mark.parametrize(
    "mutator",
    [
        lambda raw: raw.replace(b'"schema":', b'"schemaX":', 1),
        lambda raw: raw[:-1] + b',"extra":true}',
        lambda raw: b" " + raw,
        lambda raw: raw + b"\n",
        lambda raw: raw.replace(b'"arguments":', b'"arguments" :', 1),
    ],
)
def test_parser_rejects_alias_extra_and_noncanonical_bytes(mutator):
    contracts, _ = _load_modules()
    document = _documents(contracts)["personal-operator.capability-call.v1"]
    with pytest.raises(contracts.ContractValidationError):
        contracts.parse_canonical_json(
            mutator(_canonical(document)), "personal-operator.capability-call.v1"
        )


def test_parser_rejects_duplicate_keys_nonfinite_numbers_and_invalid_utf8():
    contracts, _ = _load_modules()
    schema = "personal-operator.capability-call.v1"
    samples = [
        b'{"schema":"personal-operator.capability-call.v1","schema":"personal-operator.capability-call.v1"}',
        b'{"schema":"personal-operator.capability-call.v1","x":NaN}',
        b'{"schema":"personal-operator.capability-call.v1","x":Infinity}',
        b"\xff",
    ]
    for raw in samples:
        with pytest.raises(contracts.ContractValidationError):
            contracts.parse_canonical_json(raw, schema)


def test_configured_canonical_limits_reject_each_overflow_dimension():
    contracts, _ = _load_modules()
    limits = contracts.ContractLimits(
        max_bytes=64,
        max_depth=2,
        max_collection_items=2,
        max_string_chars=8,
        max_total_nodes=4,
    )
    schema = "personal-operator.capability-call.v1"
    cases = [
        b"{}" * 33,
        _canonical({"x": {"y": {"z": None}}}),
        _canonical({"x": [1, 2, 3]}),
        _canonical({"x": "123456789"}),
        _canonical({"x": [1, 2], "y": [3, 4]}),
    ]
    for raw in cases:
        with pytest.raises(contracts.ContractValidationError):
            contracts.parse_canonical_json(raw, schema, limits=limits)


def test_integer_fields_reject_booleans_unsafe_ranges_and_grammar_mutations():
    contracts, _ = _load_modules()
    documents = _documents(contracts)
    mutations = [
        ("personal-operator.capability-installation.v1", "policyRevision", True),
        ("personal-operator.turn-capability-grant.v1", "maxCalls", False),
        ("personal-operator.turn-capability-grant.v1", "iat", 2**53),
        ("personal-operator.turn-capability-grant.v1", "releaseCommit", "A" * 40),
        ("personal-operator.capability-call.v1", "toolName", "file_read"),
        ("personal-operator.target-grant.v1", "normalizedTarget", "http://127.0.0.1/"),
        ("personal-operator.compute-job-spec.v1", "network", "HOST"),
        ("personal-operator.import-plan.v1", "effectsReplayable", True),
    ]
    for schema, key, value in mutations:
        mutated = {**documents[schema], key: value}
        with pytest.raises(contracts.ContractValidationError):
            contracts.parse_canonical_json(_canonical(mutated), schema)


def test_hash_bound_contracts_reject_argument_and_identity_mutation():
    contracts, _ = _load_modules()
    documents = _documents(contracts)
    call = documents["personal-operator.capability-call.v1"]
    for mutation in [
        {**call, "arguments": {"path": "changed.md"}},
        {**call, "argsHash": SHA_B},
        {**call, "toolUseId": "tooluse_changed123"},
    ]:
        with pytest.raises(contracts.ContractValidationError):
            contracts.parse_canonical_json(
                _canonical(mutation), "personal-operator.capability-call.v1"
            )


def test_enums_are_exact_and_standing_or_irreversible_authority_is_absent():
    contracts, _ = _load_modules()
    assert contracts.RISK_CLASSES == frozenset(
        {
            "LOCAL_READ",
            "LOCAL_MUTATION",
            "PUBLIC_READ",
            "PRIVATE_READ",
            "DURABLE_MUTATION",
            "EXTERNAL_EFFECT",
            "IRREVERSIBLE_EFFECT",
        }
    )
    assert contracts.RESULT_STATUSES == frozenset(
        {"SUCCEEDED", "PENDING_APPROVAL", "DENIED", "FAILED_RETRYABLE", "UNCERTAIN"}
    )
    assert "STANDING" not in contracts.APPROVAL_POLICIES


def test_catalog_compile_is_deterministic_digest_bound_and_exact():
    contracts, catalog_module = _load_modules()
    first_bytes, first = catalog_module.compile_catalog(RELEASE_COMMIT, SCHEMA_DIR)
    second_bytes, second = catalog_module.compile_catalog(RELEASE_COMMIT, SCHEMA_DIR)
    assert first_bytes == second_bytes
    assert first == second
    assert first.to_bytes() == first_bytes
    mapping = first.to_mapping()
    with pytest.raises((FrozenInstanceError, AttributeError)):
        first._data = {}  # type: ignore[misc]
    with pytest.raises(TypeError):
        first.data["packs"][0]["packId"] = "mutated"  # type: ignore[index]
    pack_source = dict(mapping["packs"][0])
    frozen_pack = contracts.CapabilityPackV1.from_mapping(pack_source)
    pack_source["packId"] = "mutated.pack"
    assert frozen_pack.pack_id == "workspace.file-list"
    assert mapping["releaseCommit"] == RELEASE_COMMIT
    without_digest = {key: value for key, value in mapping.items() if key != "catalogDigest"}
    assert mapping["catalogDigest"] == hashlib.sha256(_canonical(without_digest)).hexdigest()
    assert contracts.parse_canonical_json(
        first_bytes, "personal-operator.capability-catalog.v1"
    ) == first

    operations = [operation for pack in mapping["packs"] for operation in pack["operations"]]
    assert [operation["toolName"] for operation in operations] == [
        "po_file_list",
        "po_file_read",
        "po_file_write",
        "po_file_delete",
        "po_web_read",
        "po_schedule_list",
        "po_schedule_propose",
        "po_schedule_cancel_propose",
        "po_compute_run",
        "po_compute_status",
    ]
    for pack in mapping["packs"]:
        assert set(pack) == {
            "packId",
            "version",
            "riskClass",
            "credentialBoundary",
            "operations",
            "approvalPolicy",
            "targetPolicy",
            "retryPolicy",
            "quotaPolicy",
            "retentionPolicy",
            "deletionPolicy",
        }
        operation = pack["operations"][0]
        assert set(operation) == {
            "operationId",
            "toolName",
            "inputSchemaDigest",
            "outputSchemaDigest",
        }
        assert len(operation["inputSchemaDigest"]) == 64
        assert len(operation["outputSchemaDigest"]) == 64


def test_schema_byte_change_changes_catalog_digest(tmp_path):
    _, catalog_module = _load_modules()
    copied_root = tmp_path / "capabilities"
    shutil.copytree(SCHEMA_DIR.parent, copied_root)
    copied_schema_dir = copied_root / "schemas"
    original_bytes, original = catalog_module.compile_catalog(RELEASE_COMMIT, copied_schema_dir)
    schema_path = copied_schema_dir / "po-web-read-output.json"
    raw = schema_path.read_bytes()
    schema_path.write_bytes(raw.replace(b'"maxLength":32768', b'"maxLength":32767', 1))
    changed_bytes, changed = catalog_module.compile_catalog(RELEASE_COMMIT, copied_schema_dir)
    assert original_bytes != changed_bytes
    assert original.catalog_digest != changed.catalog_digest


@pytest.mark.parametrize(
    "release_commit",
    ["a" * 39, "a" * 41, "A" * 40, "g" * 40, "main", True],
)
def test_catalog_rejects_any_release_identity_other_than_exact_lowercase_sha(
    release_commit,
):
    contracts, catalog_module = _load_modules()
    with pytest.raises(contracts.ContractValidationError):
        catalog_module.compile_catalog(release_commit, SCHEMA_DIR)


def test_catalog_sources_are_canonical_bounded_json_and_dependency_free():
    contracts, catalog_module = _load_modules()
    assert SOURCE_CATALOG.read_bytes().endswith(b"\n")
    for path in [SOURCE_CATALOG, *sorted(SCHEMA_DIR.glob("*.json"))]:
        raw = path.read_bytes()
        parsed = json.loads(raw)
        assert raw == _canonical(parsed) + b"\n"
        assert len(raw) <= catalog_module.MAX_SCHEMA_ARTIFACT_BYTES

    production = (CAPABILITIES_DIR / "contracts.py").read_text("utf-8") + (
        CAPABILITIES_DIR / "catalog.py"
    ).read_text("utf-8")
    lowered = production.lower()
    for forbidden in ["import boto", "from boto", "import aws", "from aws", "botocore"]:
        assert forbidden not in lowered
