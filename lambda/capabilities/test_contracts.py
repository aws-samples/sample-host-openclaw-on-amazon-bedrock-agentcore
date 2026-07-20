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
    call_id = contracts.derive_call_id(
        "invocation_12345678",
        "tooluse_12345678",
        SHA_A,
        "workspace.file.read",
        "po_file_read",
        args_hash,
    )
    definition = {
        "message": "Review the plan",
        "runAt": 1_800_000_000,
        "timezone": "Europe/Tallinn",
    }
    definition_hash = contracts.canonical_sha256(definition)
    proposal_arguments = {"taskType": "REMINDER", "definition": definition}
    proposal_args_hash = contracts.canonical_sha256(proposal_arguments)
    connector_manifest_body = {
        "schema": "personal-operator.connector-manifest.v1",
        "connectorId": "google.gmail",
        "version": "1.0.0",
        "operations": [
            {
                "operationId": "gmail.read",
                "mode": "READ",
                "inputSchemaDigest": SHA_A,
                "outputSchemaDigest": SHA_B,
            }
        ],
        "credentialBoundary": "TRUSTED_ADAPTER",
    }
    connector_manifest_digest = contracts.canonical_sha256(connector_manifest_body)
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
            "catalogDigest": SHA_A,
            "operationId": "workspace.file.read",
            "toolName": "po_file_read",
            "arguments": args,
            "argsHash": args_hash,
        },
        "personal-operator.capability-result.v1": {
            "schema": "personal-operator.capability-result.v1",
            "callId": call_id,
            "invocationId": "invocation_12345678",
            "toolUseId": "tooluse_12345678",
            "catalogDigest": SHA_A,
            "operationId": "workspace.file.read",
            "toolName": "po_file_read",
            "argsHash": args_hash,
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
                "https://example.com/exact",
                "GET",
                "SAME_HOST",
                1_800_000_300,
                1,
                "invocation_12345678",
                contracts.derive_target_tenant_binding("user_alpha"),
            ),
            "normalizedTarget": "https://example.com/exact",
            "method": "GET",
            "redirectPolicy": "SAME_HOST",
            "expiresAt": 1_800_000_300,
            "maxUses": 1,
            "currentRequestId": "invocation_12345678",
            "tenantBinding": contracts.derive_target_tenant_binding("user_alpha"),
        },
        "personal-operator.action-proposal.v1": {
            "schema": "personal-operator.action-proposal.v1",
            "proposalId": "proposal_12345678",
            "userId": "user_alpha",
            "catalogDigest": SHA_A,
            "connectorSchemaDigest": None,
            "operationId": "schedule.propose",
            "toolName": "po_schedule_propose",
            "capabilityId": "schedule.propose",
            "resource": "schedule:new",
            "connectionRef": None,
            "arguments": proposal_arguments,
            "argsHash": proposal_args_hash,
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
            **connector_manifest_body,
            "schemaDigest": connector_manifest_digest,
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
    without_digest = {
        key: value for key, value in mapping.items() if key != "catalogDigest"
    }
    assert (
        mapping["catalogDigest"]
        == hashlib.sha256(_canonical(without_digest)).hexdigest()
    )
    assert (
        contracts.parse_canonical_json(
            first_bytes, "personal-operator.capability-catalog.v1"
        )
        == first
    )

    operations = [
        operation for pack in mapping["packs"] for operation in pack["operations"]
    ]
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


def test_direct_catalog_parser_rejects_rehashed_frozen_matrix_mutation():
    contracts, catalog_module = _load_modules()
    _, catalog = catalog_module.compile_catalog(RELEASE_COMMIT, SCHEMA_DIR)
    document = catalog.to_mapping()
    document["packs"][0]["riskClass"] = "PUBLIC_READ"
    without_digest = {
        key: value for key, value in document.items() if key != "catalogDigest"
    }
    document["catalogDigest"] = hashlib.sha256(_canonical(without_digest)).hexdigest()
    with pytest.raises(contracts.ContractValidationError):
        contracts.parse_canonical_json(_canonical(document), document["schema"])


@pytest.mark.parametrize("mutation", ["change", "swap"])
def test_direct_catalog_parser_binds_exact_schema_digest_placement(mutation):
    contracts, catalog_module = _load_modules()
    _, catalog = catalog_module.compile_catalog(RELEASE_COMMIT, SCHEMA_DIR)
    document = catalog.to_mapping()
    operations = [pack["operations"][0] for pack in document["packs"]]
    if mutation == "change":
        operations[0]["inputSchemaDigest"] = SHA_A
    else:
        operations[0]["inputSchemaDigest"], operations[1]["inputSchemaDigest"] = (
            operations[1]["inputSchemaDigest"],
            operations[0]["inputSchemaDigest"],
        )
    without_digest = {
        key: value for key, value in document.items() if key != "catalogDigest"
    }
    document["catalogDigest"] = hashlib.sha256(_canonical(without_digest)).hexdigest()
    with pytest.raises(contracts.ContractValidationError):
        contracts.parse_canonical_json(_canonical(document), document["schema"])


def test_schema_byte_change_is_rejected_by_reviewed_digest_lock(tmp_path):
    contracts, catalog_module = _load_modules()
    copied_root = tmp_path / "capabilities"
    shutil.copytree(SCHEMA_DIR.parent, copied_root)
    copied_schema_dir = copied_root / "schemas"
    catalog_module.compile_catalog(RELEASE_COMMIT, copied_schema_dir)
    schema_path = copied_schema_dir / "po-web-read-output.json"
    raw = schema_path.read_bytes()
    changed = raw.replace(b'"maxLength":32768', b'"maxLength":32767', 1)
    assert changed != raw
    assert hashlib.sha256(changed).digest() != hashlib.sha256(raw).digest()
    schema_path.write_bytes(changed)
    with pytest.raises(contracts.ContractValidationError):
        catalog_module.compile_catalog(RELEASE_COMMIT, copied_schema_dir)


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


# Review-fix adversarial coverage. These tests were added against commit
# 0a85110 before the corresponding production changes.


def _legacy_target_hash(target):
    digest = hashlib.sha256(b"personal-operator.target-grant.v1\0")
    for value in (target, "GET", "SAME_HOST", "request_12345678"):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _write_canonical(path, value):
    path.write_bytes(_canonical(value) + b"\n")


def _copied_capability_tree(tmp_path):
    target = tmp_path / "capabilities"
    shutil.copytree(SCHEMA_DIR.parent, target)
    return target, target / "schemas"


def _operation_call(contracts, operation_id, tool_name, arguments, index=1):
    document = dict(_documents(contracts)["personal-operator.capability-call.v1"])
    document.update(
        {
            "toolUseId": f"tooluse_{index:08d}",
            "operationId": operation_id,
            "toolName": tool_name,
            "arguments": arguments,
            "argsHash": contracts.canonical_sha256(arguments),
        }
    )
    document["callId"] = contracts.derive_call_id(
        document["invocationId"],
        document["toolUseId"],
        document["catalogDigest"],
        document["operationId"],
        document["toolName"],
        document["argsHash"],
    )
    return document


def _successful_result_for_call(call, data, *, provenance_refs=None, receipt_ref=None):
    return {
        "schema": "personal-operator.capability-result.v1",
        "callId": call["callId"],
        "invocationId": call["invocationId"],
        "toolUseId": call["toolUseId"],
        "catalogDigest": call["catalogDigest"],
        "operationId": call["operationId"],
        "toolName": call["toolName"],
        "argsHash": call["argsHash"],
        "status": "SUCCEEDED",
        "data": data,
        "provenanceRefs": [] if provenance_refs is None else provenance_refs,
        "proposalRef": None,
        "receiptRef": receipt_ref,
        "errorCode": None,
        "retryPolicy": "NONE",
    }


def _connector_manifest_document(
    contracts, *, connector_id="google.gmail", mode="PREPARE"
):
    body = {
        "schema": "personal-operator.connector-manifest.v1",
        "connectorId": connector_id,
        "version": "1.0.0",
        "operations": [
            {
                "operationId": "gmail.send",
                "mode": mode,
                "inputSchemaDigest": SHA_A,
                "outputSchemaDigest": SHA_B,
            }
        ],
        "credentialBoundary": "TRUSTED_ADAPTER",
    }
    return {**body, "schemaDigest": contracts.canonical_sha256(body)}


def _connector_proposal_document(contracts, manifest, arguments):
    return {
        "schema": "personal-operator.action-proposal.v1",
        "proposalId": "proposal_12345678",
        "userId": "user_alpha",
        "catalogDigest": None,
        "connectorSchemaDigest": manifest.schema_digest,
        "operationId": "gmail.send",
        "toolName": None,
        "capabilityId": "gmail.send",
        "resource": "gmail:draft:draft_12345678",
        "connectionRef": "connection_12345678",
        "arguments": arguments,
        "argsHash": contracts.canonical_sha256(arguments),
        "revision": 1,
        "originatingInvocationId": "invocation_12345678",
        "approvalPolicy": "EXACT_ONE_TIME",
        "expiresAt": 1_800_000_300,
    }


@pytest.mark.parametrize(
    "target",
    [
        "https://172.16.0.1/admin",
        "https://192.0.2.1/",
        "https://169.254.1.1/",
        "https://224.0.0.1/",
        "https://239.255.255.250/",
        "https://[::1]/",
        "https://[fc00::1]/",
        "https://[fe80::1]/",
        "https://[fec0::1]/",
        "https://[ff02::1]/",
        "https://[::ffff:127.0.0.1]/",
        "https://2130706433/",
        "https://0177.0.0.1/",
        "https://0x7f000001/",
        "https://127.0.0.0x1/",
        "https://127.0x0.0.1/",
        "https://0x7f.0.0.1/",
        "https://0177.0x0.0.01/",
        "https://example.com.:443/",
        "https://user@example.com/",
        "https://example.com/#fragment",
        "https://example.com:0443/",
        "https://example.com/%2e%2e/admin",
        "https://example.com/a/../admin",
        "https://example.com/./admin",
        "https://example.com/é",
        "https://example.com/a\x01b",
        "https://example.com",
    ],
)
def test_target_grant_rejects_non_global_literals_and_url_aliases(target):
    contracts, _ = _load_modules()
    document = _documents(contracts)["personal-operator.target-grant.v1"]
    with pytest.raises(contracts.ContractValidationError):
        contracts.derive_target_hash(
            target,
            document["method"],
            document["redirectPolicy"],
            document["expiresAt"],
            document["maxUses"],
            document["currentRequestId"],
            document["tenantBinding"],
        )
    document["normalizedTarget"] = target
    document["targetHash"] = _legacy_target_hash(target)
    with pytest.raises(contracts.ContractValidationError):
        contracts.parse_canonical_json(_canonical(document), document["schema"])


def test_target_hash_binds_expiry_use_request_and_tenant_authority():
    contracts, _ = _load_modules()
    document = _documents(contracts)["personal-operator.target-grant.v1"]
    for field, value in (
        ("expiresAt", document["expiresAt"] + 1),
        ("maxUses", 2),
        ("currentRequestId", "invocation_other_1234"),
        ("tenantBinding", contracts.derive_target_tenant_binding("user_beta")),
    ):
        mutated = {**document, field: value}
        with pytest.raises(contracts.ContractValidationError):
            contracts.parse_canonical_json(_canonical(mutated), document["schema"])


def test_target_grant_contract_requires_a_tenant_binding():
    contracts, _ = _load_modules()

    assert "tenantBinding" in contracts.TargetGrantV1.FIELDS


@pytest.mark.parametrize(
    "target",
    [
        "https://8.8.8.8/",
        "https://[2606:4700:4700::1111]/",
        "https://example.com/a%20b",
    ],
)
def test_target_grant_accepts_only_canonical_global_https_examples(target):
    contracts, _ = _load_modules()
    document = _documents(contracts)["personal-operator.target-grant.v1"]
    document["normalizedTarget"] = target
    document["targetHash"] = contracts.derive_target_hash(
        target,
        document["method"],
        document["redirectPolicy"],
        document["expiresAt"],
        document["maxUses"],
        document["currentRequestId"],
        document["tenantBinding"],
    )
    assert contracts.parse_canonical_json(_canonical(document), document["schema"])


def test_call_id_binds_catalog_operation_and_tool_identity():
    contracts, _ = _load_modules()
    document = _documents(contracts)["personal-operator.capability-call.v1"]
    mutations = [
        {**document, "catalogDigest": SHA_B},
        {
            **document,
            "operationId": "workspace.file.delete",
            "toolName": "po_file_delete",
        },
        {**document, "toolName": "po_file_delete"},
    ]
    for mutation in mutations:
        with pytest.raises(contracts.ContractValidationError):
            contracts.parse_canonical_json(_canonical(mutation), document["schema"])


@pytest.mark.parametrize(
    "arguments,tool_name",
    [
        ({"path": "notes/today.md", "extra": "authority-smuggling"}, "po_file_read"),
        ({"path": "notes/today.md"}, "po_file_delete"),
    ],
)
def test_call_rejects_rehashed_wrong_schema_or_tool_substitution(arguments, tool_name):
    contracts, _ = _load_modules()
    document = _documents(contracts)["personal-operator.capability-call.v1"]
    document["arguments"] = arguments
    document["toolName"] = tool_name
    document["argsHash"] = contracts.canonical_sha256(arguments)
    if tool_name == document["toolName"] == "po_file_read":
        document["callId"] = contracts.derive_call_id(
            document["invocationId"],
            document["toolUseId"],
            document["catalogDigest"],
            document["operationId"],
            document["toolName"],
            document["argsHash"],
        )
    with pytest.raises(contracts.ContractValidationError):
        contracts.parse_canonical_json(_canonical(document), document["schema"])


@pytest.mark.parametrize(
    "mutation",
    [
        {
            "status": "DENIED",
            "errorCode": None,
            "data": {"secret": "still-present"},
            "provenanceRefs": ["workspace:notes/today.md"],
        },
        {
            "status": "FAILED_RETRYABLE",
            "errorCode": None,
            "retryPolicy": "SAFE_RETRY",
            "data": {"partial": True},
        },
        {
            "status": "UNCERTAIN",
            "errorCode": None,
            "retryPolicy": "RECONCILE_ONLY",
            "receiptRef": "receipt_12345678",
        },
        {
            "status": "PENDING_APPROVAL",
            "proposalRef": "proposal_12345678",
            "data": {"path": "notes/today.md", "content": "hello"},
        },
        {
            "status": "SUCCEEDED",
            "proposalRef": "proposal_12345678",
        },
    ],
)
def test_result_status_has_exact_required_and_forbidden_fields(mutation):
    contracts, _ = _load_modules()
    document = _documents(contracts)["personal-operator.capability-result.v1"]
    document.update(mutation)
    with pytest.raises(contracts.ContractValidationError):
        contracts.parse_canonical_json(_canonical(document), document["schema"])


def test_proposal_rejects_rehashed_operation_specific_extra_arguments():
    contracts, _ = _load_modules()
    document = _documents(contracts)["personal-operator.action-proposal.v1"]
    document["arguments"] = {**document["arguments"], "sendEmail": True}
    document["argsHash"] = contracts.canonical_sha256(document["arguments"])
    with pytest.raises(contracts.ContractValidationError):
        contracts.parse_canonical_json(_canonical(document), document["schema"])


@pytest.mark.parametrize("mutation", ["resource", "connection"])
def test_cancel_proposal_binds_exact_schedule_resource_without_connection(mutation):
    contracts, _ = _load_modules()
    document = _documents(contracts)["personal-operator.action-proposal.v1"]
    arguments = {"scheduleId": "schedule_00000001"}
    document.update(
        {
            "operationId": "schedule.cancel.propose",
            "toolName": "po_schedule_cancel_propose",
            "capabilityId": "schedule.cancel.propose",
            "resource": "schedule:schedule_00000001",
            "arguments": arguments,
            "argsHash": contracts.canonical_sha256(arguments),
        }
    )
    if mutation == "resource":
        document["resource"] = "schedule:schedule_00000002"
    else:
        document["connectionRef"] = "connection_00000001"
    with pytest.raises(contracts.ContractValidationError):
        contracts.parse_canonical_json(_canonical(document), document["schema"])


def test_connector_proposal_requires_manifest_and_connection_bound_context():
    contracts, _ = _load_modules()
    manifest = contracts.ConnectorManifestV1.from_mapping(
        _connector_manifest_document(contracts)
    )
    connection = contracts.ConnectorConnectionV1.from_mapping(
        _documents(contracts)["personal-operator.connector-connection.v1"]
    )
    normalized_arguments = {
        "recipient": "synthetic@example.com",
        "subject": "Synthetic draft",
    }
    proposal_document = _connector_proposal_document(
        contracts, manifest, normalized_arguments
    )
    proposal = contracts.ActionProposalV1.from_connector_mapping(
        proposal_document,
        manifest=manifest,
        connection=connection,
        expected_resource="gmail:draft:draft_12345678",
        normalized_arguments=normalized_arguments,
    )
    assert proposal.to_mapping() == proposal_document
    with pytest.raises(contracts.ContractValidationError):
        contracts.ActionProposalV1.from_mapping(proposal_document)


@pytest.mark.parametrize(
    "mutation",
    ["manifest", "schema", "connection", "resource", "arguments", "read-operation"],
)
def test_connector_proposal_rejects_context_and_wysiwyis_substitution(mutation):
    contracts, _ = _load_modules()
    manifest_document = _connector_manifest_document(contracts)
    if mutation == "read-operation":
        manifest_document = _connector_manifest_document(contracts, mode="READ")
    manifest = contracts.ConnectorManifestV1.from_mapping(manifest_document)
    connection_document = _documents(contracts)[
        "personal-operator.connector-connection.v1"
    ]
    connection = contracts.ConnectorConnectionV1.from_mapping(connection_document)
    normalized_arguments = {
        "recipient": "synthetic@example.com",
        "subject": "Synthetic draft",
    }
    proposal_document = _connector_proposal_document(
        contracts, manifest, normalized_arguments
    )
    expected_resource = "gmail:draft:draft_12345678"
    if mutation == "manifest":
        manifest = contracts.ConnectorManifestV1.from_mapping(
            _connector_manifest_document(contracts, connector_id="other.gmail")
        )
    elif mutation == "schema":
        proposal_document["connectorSchemaDigest"] = SHA_A
        assert proposal_document["connectorSchemaDigest"] != manifest.schema_digest
    elif mutation == "connection":
        proposal_document["connectionRef"] = "connection_00000000"
    elif mutation == "resource":
        proposal_document["resource"] = "gmail:draft:draft_00000000"
    elif mutation == "arguments":
        proposal_document["arguments"] = {
            **proposal_document["arguments"],
            "sendImmediately": True,
        }
        proposal_document["argsHash"] = contracts.canonical_sha256(
            proposal_document["arguments"]
        )
    with pytest.raises(contracts.ContractValidationError):
        contracts.ActionProposalV1.from_connector_mapping(
            proposal_document,
            manifest=manifest,
            connection=connection,
            expected_resource=expected_resource,
            normalized_arguments=normalized_arguments,
        )


def test_connector_manifest_digest_binds_operation_schema_metadata():
    contracts, _ = _load_modules()
    document = _connector_manifest_document(contracts)
    document["operations"][0]["inputSchemaDigest"] = SHA_B
    with pytest.raises(contracts.ContractValidationError):
        contracts.ConnectorManifestV1.from_mapping(document)


@pytest.mark.parametrize(
    "task_type,definition",
    [
        (
            "REMINDER",
            {
                "message": "Review the plan",
                "runAt": 1_800_000_000,
                "timezone": "Europe/Tallinn",
                "sendEmail": True,
            },
        ),
        (
            "READ_ONLY_AGENT_TURN",
            {
                "prompt": "Read current workspace",
                "runAt": 1_800_000_000,
                "timezone": "Europe/Tallinn",
                "externalEffect": True,
            },
        ),
    ],
)
def test_schedule_definition_is_exact_for_each_task_type(task_type, definition):
    contracts, _ = _load_modules()
    document = _documents(contracts)["personal-operator.schedule-spec.v1"]
    document["taskType"] = task_type
    document["definition"] = definition
    document["definitionHash"] = contracts.canonical_sha256(definition)
    with pytest.raises(contracts.ContractValidationError):
        contracts.parse_canonical_json(_canonical(document), document["schema"])


def test_catalog_source_must_equal_the_frozen_ten_row_matrix():
    contracts, _ = _load_modules()
    source = json.loads(SOURCE_CATALOG.read_bytes())
    assert contracts.canonical_json_bytes(
        source["packs"]
    ) == contracts.canonical_json_bytes(contracts.FROZEN_CATALOG_PACKS_V1)
    with pytest.raises(TypeError):
        contracts.FROZEN_CATALOG_PACKS_V1[0]["riskClass"] = "LOCAL_MUTATION"
    with pytest.raises((AttributeError, TypeError)):
        contracts.FROZEN_CATALOG_PACKS_V1[0]["operations"].append({})


def test_frozen_catalog_sequences_reject_base_list_mutator_bypass():
    contracts, _ = _load_modules()
    operations = contracts.FROZEN_CATALOG_PACKS_V1[0]["operations"]
    original = operations[0]
    try:
        with pytest.raises(TypeError):
            list.__setitem__(operations, 0, original)
    finally:
        if isinstance(operations, list):
            list.__setitem__(operations, 0, original)


@pytest.mark.parametrize("mutation", ["schema-swap", "policy-drift"])
def test_catalog_rejects_schema_swaps_and_policy_incoherence(tmp_path, mutation):
    contracts, catalog_module = _load_modules()
    root, schema_dir = _copied_capability_tree(tmp_path)
    source_path = root / "catalog-v1.json"
    source = json.loads(source_path.read_bytes())
    if mutation == "schema-swap":
        first = source["packs"][0]["operations"][0]
        second = source["packs"][1]["operations"][0]
        first["inputSchema"], second["inputSchema"] = (
            second["inputSchema"],
            first["inputSchema"],
        )
    else:
        source["packs"][6]["riskClass"] = "LOCAL_READ"
        source["packs"][6]["approvalPolicy"] = {
            "mode": "NONE",
            "standingAllowed": False,
        }
    _write_canonical(source_path, source)
    with pytest.raises(contracts.ContractValidationError):
        catalog_module.compile_catalog(RELEASE_COMMIT, schema_dir)


@pytest.mark.parametrize("entry_kind", ["broken-symlink", "file", "directory"])
def test_schema_directory_is_a_closed_regular_file_set(tmp_path, entry_kind):
    contracts, catalog_module = _load_modules()
    _, schema_dir = _copied_capability_tree(tmp_path)
    extra = schema_dir / "unreviewed"
    if entry_kind == "broken-symlink":
        extra = extra.with_suffix(".json")
        extra.symlink_to(schema_dir / "absent.json")
    elif entry_kind == "file":
        extra.write_text("unreviewed", encoding="utf-8")
    else:
        extra.mkdir()
    with pytest.raises(contracts.ContractValidationError):
        catalog_module.compile_catalog(RELEASE_COMMIT, schema_dir)


def test_schema_directory_rejects_referenced_and_ancestor_symlinks(tmp_path):
    contracts, catalog_module = _load_modules()
    root, schema_dir = _copied_capability_tree(tmp_path / "real")
    referenced = schema_dir / "po-file-list-input.json"
    outside = tmp_path / "outside.json"
    outside.write_bytes(referenced.read_bytes())
    referenced.unlink()
    referenced.symlink_to(outside)
    with pytest.raises(contracts.ContractValidationError):
        catalog_module.compile_catalog(RELEASE_COMMIT, schema_dir)

    referenced.unlink()
    referenced.write_bytes(outside.read_bytes())
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(contracts.ContractValidationError):
        catalog_module.compile_catalog(RELEASE_COMMIT, alias / "schemas")


def test_schema_rejects_every_unapproved_reference_keyword(tmp_path):
    contracts, catalog_module = _load_modules()
    _, schema_dir = _copied_capability_tree(tmp_path)
    schema_path = schema_dir / "po-web-read-input.json"
    schema = json.loads(schema_path.read_bytes())
    schema["properties"]["url"]["$recursiveRef"] = "https://attacker.invalid/schema"
    _write_canonical(schema_path, schema)
    with pytest.raises(contracts.ContractValidationError):
        catalog_module.compile_catalog(RELEASE_COMMIT, schema_dir)


@pytest.mark.parametrize(
    "keyword",
    ["$ref", "$dynamicRef", "$recursiveRef", "$defs", "allOf", "if", "description"],
)
def test_schema_rejects_every_keyword_outside_the_explicit_subset(tmp_path, keyword):
    contracts, catalog_module = _load_modules()
    _, schema_dir = _copied_capability_tree(tmp_path)
    schema_path = schema_dir / "po-web-read-input.json"
    schema = json.loads(schema_path.read_bytes())
    schema["properties"]["url"][keyword] = "unreviewed"
    _write_canonical(schema_path, schema)
    with pytest.raises(contracts.ContractValidationError):
        catalog_module.compile_catalog(RELEASE_COMMIT, schema_dir)


def test_schema_rejects_allowed_keywords_in_incompatible_positions(tmp_path):
    contracts, catalog_module = _load_modules()
    _, schema_dir = _copied_capability_tree(tmp_path)
    schema_path = schema_dir / "po-web-read-input.json"
    schema = json.loads(schema_path.read_bytes())
    schema["properties"]["url"]["minimum"] = 0
    _write_canonical(schema_path, schema)
    with pytest.raises(contracts.ContractValidationError):
        catalog_module.compile_catalog(RELEASE_COMMIT, schema_dir)


def test_schema_rejects_a_top_level_scalar_tool_contract(tmp_path):
    contracts, catalog_module = _load_modules()
    _, schema_dir = _copied_capability_tree(tmp_path)
    schema_path = schema_dir / "po-web-read-input.json"
    schema = {
        "$id": "urn:personal-operator:tool-schema:po-web-read-input:v1",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Scalar bypass",
        "type": "string",
    }
    _write_canonical(schema_path, schema)
    with pytest.raises(contracts.ContractValidationError):
        catalog_module.compile_catalog(RELEASE_COMMIT, schema_dir)


def test_schema_rejects_duplicate_union_branches(tmp_path):
    contracts, catalog_module = _load_modules()
    _, schema_dir = _copied_capability_tree(tmp_path)
    schema_path = schema_dir / "po-compute-run-input.json"
    schema = json.loads(schema_path.read_bytes())
    schema["properties"]["command"]["oneOf"][1] = schema["properties"]["command"][
        "oneOf"
    ][0]
    _write_canonical(schema_path, schema)
    with pytest.raises(contracts.ContractValidationError):
        catalog_module.compile_catalog(RELEASE_COMMIT, schema_dir)


@pytest.mark.parametrize("invalid_type", [[], {}])
def test_schema_malformed_type_normalizes_to_contract_error(tmp_path, invalid_type):
    contracts, catalog_module = _load_modules()
    _, schema_dir = _copied_capability_tree(tmp_path)
    schema_path = schema_dir / "po-web-read-input.json"
    schema = json.loads(schema_path.read_bytes())
    schema["properties"]["url"]["type"] = invalid_type
    _write_canonical(schema_path, schema)
    with pytest.raises(contracts.ContractValidationError):
        catalog_module.compile_catalog(RELEASE_COMMIT, schema_dir)


def test_schema_mixed_required_members_normalize_to_contract_error(tmp_path):
    contracts, catalog_module = _load_modules()
    _, schema_dir = _copied_capability_tree(tmp_path)
    schema_path = schema_dir / "po-web-read-input.json"
    schema = json.loads(schema_path.read_bytes())
    schema["required"] = ["url", 1]
    _write_canonical(schema_path, schema)
    with pytest.raises(contracts.ContractValidationError):
        catalog_module.compile_catalog(RELEASE_COMMIT, schema_dir)


@pytest.mark.parametrize(
    "mutation", ["schema-id", "path-escape", "duplicate-operation"]
)
def test_catalog_rejects_schema_identity_path_escape_and_duplicate_ids(
    tmp_path, mutation
):
    contracts, catalog_module = _load_modules()
    root, schema_dir = _copied_capability_tree(tmp_path)
    if mutation == "schema-id":
        schema_path = schema_dir / "po-file-read-input.json"
        schema = json.loads(schema_path.read_bytes())
        schema["$id"] = "urn:personal-operator:tool-schema:other:v1"
        _write_canonical(schema_path, schema)
    else:
        source_path = root / "catalog-v1.json"
        source = json.loads(source_path.read_bytes())
        if mutation == "path-escape":
            source["packs"][0]["operations"][0]["inputSchema"] = "../outside.json"
        else:
            source["packs"][1]["operations"][0]["operationId"] = source["packs"][0][
                "operations"
            ][0]["operationId"]
        _write_canonical(source_path, source)
    with pytest.raises(contracts.ContractValidationError):
        catalog_module.compile_catalog(RELEASE_COMMIT, schema_dir)


def test_compute_schema_and_contract_form_a_closed_discriminated_union():
    contracts, _ = _load_modules()
    schema = json.loads((SCHEMA_DIR / "po-compute-run-input.json").read_bytes())
    assert schema["properties"]["network"] == {"const": "NONE"}
    assert "oneOf" in schema["properties"]["command"]

    base = _documents(contracts)["personal-operator.capability-call.v1"]
    for command in (
        {"mode": "SCRIPT", "value": ["python", "script.py"]},
        {"mode": "ARGV", "value": "python script.py"},
    ):
        arguments = {
            "command": command,
            "inputPaths": [],
            "network": "NONE",
            "resourceProfile": "SMALL",
        }
        document = {
            **base,
            "operationId": "compute.run",
            "toolName": "po_compute_run",
            "arguments": arguments,
        }
        document["argsHash"] = contracts.canonical_sha256(arguments)
        document["callId"] = contracts.derive_call_id(
            document["invocationId"],
            document["toolUseId"],
            document["catalogDigest"],
            document["operationId"],
            document["toolName"],
            document["argsHash"],
        )
        with pytest.raises(contracts.ContractValidationError):
            contracts.parse_canonical_json(_canonical(document), document["schema"])


@pytest.mark.parametrize("status", ["FAILED", "DENIED", "TIMED_OUT"])
def test_non_success_compute_receipt_cannot_publish_output_files(status):
    contracts, _ = _load_modules()
    document = _documents(contracts)["personal-operator.compute-receipt.v1"]
    document.update({"status": status, "errorCode": "EXECUTION_FAILURE"})
    with pytest.raises(contracts.ContractValidationError):
        contracts.parse_canonical_json(_canonical(document), document["schema"])


def test_result_rejects_cross_operation_substitution_for_a_real_call_id():
    contracts, _ = _load_modules()
    document = _documents(contracts)["personal-operator.capability-result.v1"]
    document.update(
        {
            "operationId": "workspace.file.delete",
            "toolName": "po_file_delete",
            "data": {"path": "notes/today.md", "deleted": True},
            "provenanceRefs": [],
        }
    )
    with pytest.raises(contracts.ContractValidationError):
        contracts.parse_canonical_json(_canonical(document), document["schema"])


def test_result_has_a_strict_validation_path_against_originating_call():
    contracts, _ = _load_modules()
    call_document = _operation_call(
        contracts, "workspace.file.read", "po_file_read", {"path": "notes/a.md"}
    )
    result_document = _successful_result_for_call(
        call_document,
        {"path": "notes/a.md", "content": "text"},
        provenance_refs=["workspace:notes/a.md"],
    )
    call = contracts.CapabilityCallV1.from_mapping(call_document)
    result = contracts.CapabilityResultV1.from_mapping(result_document)
    assert result.validate_against_call(call) is result


@pytest.mark.parametrize(
    "operation_id,tool_name,arguments,data",
    [
        (
            "workspace.file.read",
            "po_file_read",
            {"path": "notes/a.md"},
            {"path": "notes/b.md", "content": "text"},
        ),
        (
            "workspace.file.write",
            "po_file_write",
            {"path": "notes/a.md", "content": "text"},
            {"path": "notes/b.md", "bytes": 4},
        ),
        (
            "workspace.file.write",
            "po_file_write",
            {"path": "notes/a.md", "content": "text"},
            {"path": "notes/a.md", "bytes": 3},
        ),
        (
            "workspace.file.delete",
            "po_file_delete",
            {"path": "notes/a.md"},
            {"path": "notes/b.md", "deleted": True},
        ),
        (
            "web.exact.read",
            "po_web_read",
            {"url": "https://example.com/a"},
            {
                "canonicalUrl": "https://other.example.com/b",
                "contentDigest": SHA_A,
                "retrievedAt": 1_800_000_000,
                "sourceRef": "public:https://other.example.com/b",
                "text": "text",
            },
        ),
        (
            "compute.status",
            "po_compute_status",
            {"jobId": "job_00000001"},
            {"jobId": "job_00000002", "outputs": [], "status": "RUNNING"},
        ),
    ],
)
def test_result_strict_path_rejects_unrelated_output_identity(
    operation_id, tool_name, arguments, data
):
    contracts, _ = _load_modules()
    call = contracts.CapabilityCallV1.from_mapping(
        _operation_call(contracts, operation_id, tool_name, arguments)
    )
    result = contracts.CapabilityResultV1.from_mapping(
        _successful_result_for_call(call.to_mapping(), data)
    )
    with pytest.raises(contracts.ContractValidationError):
        result.validate_against_call(call)


@pytest.mark.parametrize(
    "operation_id,tool_name,arguments,data",
    [
        ("workspace.file.list", "po_file_list", {}, {"files": []}),
        (
            "workspace.file.read",
            "po_file_read",
            {"path": "notes/a.md"},
            {"path": "notes/a.md", "content": "text"},
        ),
        (
            "web.exact.read",
            "po_web_read",
            {"url": "https://example.com/a"},
            {
                "canonicalUrl": "https://example.com/a",
                "contentDigest": SHA_A,
                "retrievedAt": 1_800_000_000,
                "sourceRef": "public:https://example.com/a",
                "text": "text",
            },
        ),
        ("schedule.list", "po_schedule_list", {}, {"schedules": []}),
        (
            "compute.status",
            "po_compute_status",
            {"jobId": "job_00000001"},
            {"jobId": "job_00000001", "outputs": [], "status": "RUNNING"},
        ),
    ],
)
def test_read_list_and_status_results_cannot_invent_effect_receipts(
    operation_id, tool_name, arguments, data
):
    contracts, _ = _load_modules()
    call = _operation_call(contracts, operation_id, tool_name, arguments)
    result = _successful_result_for_call(call, data, receipt_ref="receipt_00000001")
    with pytest.raises(contracts.ContractValidationError):
        contracts.parse_canonical_json(_canonical(result), result["schema"])


@pytest.mark.parametrize(
    "operation_id,tool_name,arguments,data",
    [
        (
            "workspace.file.write",
            "po_file_write",
            {"path": "notes/a.md", "content": "text"},
            {"path": "notes/a.md", "bytes": 4},
        ),
        (
            "workspace.file.delete",
            "po_file_delete",
            {"path": "notes/a.md"},
            {"path": "notes/a.md", "deleted": True},
        ),
        (
            "compute.run",
            "po_compute_run",
            {
                "command": {"mode": "SCRIPT", "value": "print('ok')"},
                "inputPaths": [],
                "network": "NONE",
                "resourceProfile": "SMALL",
            },
            {"jobId": "job_00000001", "status": "QUEUED"},
        ),
    ],
)
def test_mutation_results_cannot_invent_read_provenance(
    operation_id, tool_name, arguments, data
):
    contracts, _ = _load_modules()
    call = _operation_call(contracts, operation_id, tool_name, arguments)
    result = _successful_result_for_call(
        call, data, provenance_refs=["workspace:notes/a.md"]
    )
    with pytest.raises(contracts.ContractValidationError):
        contracts.parse_canonical_json(_canonical(result), result["schema"])


def test_pending_result_proposal_hash_must_equal_the_call_arguments_hash():
    contracts, _ = _load_modules()
    arguments = {
        "taskType": "REMINDER",
        "definition": {
            "message": "Review",
            "runAt": 1_800_000_000,
            "timezone": "Europe/Tallinn",
        },
    }
    call = _operation_call(
        contracts, "schedule.propose", "po_schedule_propose", arguments
    )
    result = {
        "schema": "personal-operator.capability-result.v1",
        "callId": call["callId"],
        "invocationId": call["invocationId"],
        "toolUseId": call["toolUseId"],
        "catalogDigest": call["catalogDigest"],
        "operationId": call["operationId"],
        "toolName": call["toolName"],
        "argsHash": call["argsHash"],
        "status": "PENDING_APPROVAL",
        "data": {
            "proposalRef": "proposal_00000001",
            "argsHash": SHA_B,
            "expiresAt": 1_800_000_300,
        },
        "provenanceRefs": [],
        "proposalRef": "proposal_00000001",
        "receiptRef": None,
        "errorCode": None,
        "retryPolicy": "NONE",
    }
    assert result["data"]["argsHash"] != result["argsHash"]
    with pytest.raises(contracts.ContractValidationError):
        contracts.parse_canonical_json(_canonical(result), result["schema"])


@pytest.mark.parametrize(
    "status,retry_policy",
    [
        ("DENIED", "NONE"),
        ("FAILED_RETRYABLE", "SAFE_RETRY"),
        ("UNCERTAIN", "RECONCILE_ONLY"),
    ],
)
def test_each_non_success_result_status_has_one_valid_closed_shape(
    status, retry_policy
):
    contracts, _ = _load_modules()
    call = _operation_call(
        contracts,
        "workspace.file.write",
        "po_file_write",
        {"path": "notes/a.md", "content": "text"},
    )
    result = {
        "schema": "personal-operator.capability-result.v1",
        "callId": call["callId"],
        "invocationId": call["invocationId"],
        "toolUseId": call["toolUseId"],
        "catalogDigest": call["catalogDigest"],
        "operationId": call["operationId"],
        "toolName": call["toolName"],
        "argsHash": call["argsHash"],
        "status": status,
        "data": {},
        "provenanceRefs": [],
        "proposalRef": None,
        "receiptRef": None,
        "errorCode": "POLICY_DENIED" if status == "DENIED" else "EXECUTION_FAILURE",
        "retryPolicy": retry_policy,
    }
    assert contracts.parse_canonical_json(_canonical(result), result["schema"])


def test_read_only_result_cannot_claim_uncertain_effect_outcome():
    contracts, _ = _load_modules()
    result = _documents(contracts)["personal-operator.capability-result.v1"]
    result.update(
        {
            "status": "UNCERTAIN",
            "data": {},
            "provenanceRefs": [],
            "errorCode": "UNKNOWN_OUTCOME",
            "retryPolicy": "RECONCILE_ONLY",
        }
    )
    with pytest.raises(contracts.ContractValidationError):
        contracts.parse_canonical_json(_canonical(result), result["schema"])


def test_read_only_agent_schedule_definition_has_one_exact_valid_shape():
    contracts, _ = _load_modules()
    document = _documents(contracts)["personal-operator.schedule-spec.v1"]
    definition = {
        "prompt": "Read current workspace",
        "runAt": 1_800_000_000,
        "timezone": "Europe/Tallinn",
    }
    document.update(
        {
            "taskType": "READ_ONLY_AGENT_TURN",
            "definition": definition,
            "definitionHash": contracts.canonical_sha256(definition),
        }
    )
    assert contracts.parse_canonical_json(_canonical(document), document["schema"])


@pytest.mark.parametrize("next_run_at", [None, 1_800_000_001])
def test_enabled_schedule_next_run_matches_the_hashed_definition(next_run_at):
    contracts, _ = _load_modules()
    document = _documents(contracts)["personal-operator.schedule-spec.v1"]
    document["nextRunAt"] = next_run_at
    with pytest.raises(contracts.ContractValidationError):
        contracts.parse_canonical_json(_canonical(document), document["schema"])


def test_every_frozen_operation_has_exact_input_and_output_validation():
    contracts, _ = _load_modules()
    cases = [
        ("workspace.file.list", "po_file_list", {}, {"files": []}),
        (
            "workspace.file.read",
            "po_file_read",
            {"path": "notes/a.md"},
            {"path": "notes/a.md", "content": "text"},
        ),
        (
            "workspace.file.write",
            "po_file_write",
            {"path": "notes/a.md", "content": "text"},
            {"path": "notes/a.md", "bytes": 4},
        ),
        (
            "workspace.file.delete",
            "po_file_delete",
            {"path": "notes/a.md"},
            {"path": "notes/a.md", "deleted": True},
        ),
        (
            "web.exact.read",
            "po_web_read",
            {"url": "https://example.com/exact"},
            {
                "canonicalUrl": "https://example.com/exact",
                "contentDigest": SHA_A,
                "retrievedAt": 1_800_000_000,
                "sourceRef": "public:https://example.com/exact",
                "text": "public text",
            },
        ),
        ("schedule.list", "po_schedule_list", {}, {"schedules": []}),
        (
            "schedule.propose",
            "po_schedule_propose",
            {
                "taskType": "REMINDER",
                "definition": {
                    "message": "Review",
                    "runAt": 1_800_000_000,
                    "timezone": "Europe/Tallinn",
                },
            },
            {
                "proposalRef": "proposal_00000001",
                "argsHash": SHA_A,
                "expiresAt": 1_800_000_300,
            },
        ),
        (
            "schedule.cancel.propose",
            "po_schedule_cancel_propose",
            {"scheduleId": "schedule_00000001"},
            {
                "proposalRef": "proposal_00000002",
                "argsHash": SHA_B,
                "expiresAt": 1_800_000_300,
            },
        ),
        (
            "compute.run",
            "po_compute_run",
            {
                "command": {"mode": "SCRIPT", "value": "print('ok')"},
                "inputPaths": [],
                "network": "NONE",
                "resourceProfile": "SMALL",
            },
            {"jobId": "job_00000001", "status": "QUEUED"},
        ),
        (
            "compute.status",
            "po_compute_status",
            {"jobId": "job_00000001"},
            {"jobId": "job_00000001", "outputs": [], "status": "RUNNING"},
        ),
    ]
    for index, (operation_id, tool_name, arguments, output) in enumerate(
        cases, start=1
    ):
        call = _operation_call(contracts, operation_id, tool_name, arguments, index)
        assert contracts.parse_canonical_json(_canonical(call), call["schema"])
        proposal = operation_id in {"schedule.propose", "schedule.cancel.propose"}
        if proposal:
            output = {**output, "argsHash": call["argsHash"]}
        result = {
            "schema": "personal-operator.capability-result.v1",
            "callId": call["callId"],
            "invocationId": call["invocationId"],
            "toolUseId": call["toolUseId"],
            "catalogDigest": call["catalogDigest"],
            "operationId": operation_id,
            "toolName": tool_name,
            "argsHash": call["argsHash"],
            "status": "PENDING_APPROVAL" if proposal else "SUCCEEDED",
            "data": output,
            "provenanceRefs": [],
            "proposalRef": output["proposalRef"] if proposal else None,
            "receiptRef": None,
            "errorCode": None,
            "retryPolicy": "NONE",
        }
        assert contracts.parse_canonical_json(_canonical(result), result["schema"])

        bad_arguments = {**arguments, "unreviewed": True}
        bad_call = _operation_call(
            contracts, operation_id, tool_name, bad_arguments, index + 100
        )
        with pytest.raises(contracts.ContractValidationError):
            contracts.parse_canonical_json(_canonical(bad_call), bad_call["schema"])

        bad_result = {**result, "data": {**output, "unreviewed": True}}
        with pytest.raises(contracts.ContractValidationError):
            contracts.parse_canonical_json(_canonical(bad_result), bad_result["schema"])


@pytest.mark.parametrize("number", [1.0, -0.0])
def test_v1_rejects_all_floats_in_programmatic_and_wire_values(number):
    contracts, _ = _load_modules()
    with pytest.raises(contracts.ContractValidationError):
        contracts.canonical_json_bytes({"number": number})

    base = _documents(contracts)["personal-operator.capability-call.v1"]
    arguments = {"path": "notes/today.md", "number": number}
    with pytest.raises(contracts.ContractValidationError):
        args_hash = contracts.canonical_sha256(arguments)
        document = {**base, "arguments": arguments, "argsHash": args_hash}
        document["callId"] = contracts.derive_call_id(
            document["invocationId"],
            document["toolUseId"],
            document["catalogDigest"],
            document["operationId"],
            document["toolName"],
            args_hash,
        )
        contracts.parse_canonical_json(_canonical(document), document["schema"])


def test_cycles_and_extreme_depth_normalize_to_contract_errors():
    contracts, _ = _load_modules()
    cycle = {}
    cycle["cycle"] = cycle
    with pytest.raises(contracts.ContractValidationError):
        contracts.canonical_json_bytes(cycle)

    nested = None
    for _ in range(2_000):
        nested = [nested]
    with pytest.raises(contracts.ContractValidationError):
        contracts.canonical_json_bytes(nested)

    raw = b"[" * 2_000 + b"null" + b"]" * 2_000
    with pytest.raises(contracts.ContractValidationError):
        contracts.parse_canonical_json(
            raw, "personal-operator.capability-installation.v1"
        )


@pytest.mark.parametrize(
    "runtime_arn",
    [
        "arn:aws:bedrock-agentcore:eu-west-1:not-account:runtime/example",
        "arn:aws:bedrock-agentcore:eu-west-1:123:runtime/example",
        "arn:aws:bedrock-agentcore:eu-west-1:123456789012:other/example",
        "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/example/child",
        "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/example\nforged",
        "arn:aws-cn:bedrock-agentcore:eu-west-1:123456789012:runtime/example",
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/example",
    ],
)
def test_turn_grant_requires_exact_agentcore_runtime_arn(runtime_arn):
    contracts, _ = _load_modules()
    document = _documents(contracts)["personal-operator.turn-capability-grant.v1"]
    document["runtimeArn"] = runtime_arn
    with pytest.raises(contracts.ContractValidationError):
        contracts.parse_canonical_json(_canonical(document), document["schema"])


def _mutate_nested_inputs(value):
    if isinstance(value, dict):
        for nested in list(value.values()):
            _mutate_nested_inputs(nested)
        value["__callerMutation"] = True
    elif isinstance(value, list):
        for nested in list(value):
            _mutate_nested_inputs(nested)
        value.append("__callerMutation")


def test_every_named_contract_and_catalog_pack_deeply_detach_caller_inputs():
    contracts, catalog_module = _load_modules()
    for schema, original in _documents(contracts).items():
        source = json.loads(json.dumps(original))
        frozen = contracts.parse_canonical_json(_canonical(source), schema)
        before = frozen.to_bytes()
        _mutate_nested_inputs(source)
        assert frozen.to_bytes() == before

    _, catalog = catalog_module.compile_catalog(RELEASE_COMMIT, SCHEMA_DIR)
    source_pack = catalog.to_mapping()["packs"][0]
    frozen_pack = contracts.CapabilityPackV1.from_mapping(source_pack)
    before = frozen_pack.to_mapping()
    _mutate_nested_inputs(source_pack)
    assert frozen_pack.to_mapping() == before


def test_catalog_compiler_binds_exact_reviewed_schema_digests():
    contracts, catalog_module = _load_modules()
    _, catalog = catalog_module.compile_catalog(RELEASE_COMMIT, SCHEMA_DIR)
    source = json.loads(SOURCE_CATALOG.read_bytes())
    compiled = catalog.to_mapping()["packs"]
    assert len(source["packs"]) == len(compiled) == 10
    for source_pack, compiled_pack in zip(source["packs"], compiled, strict=True):
        source_operation = source_pack["operations"][0]
        compiled_operation = compiled_pack["operations"][0]
        assert (
            compiled_operation["inputSchemaDigest"]
            == hashlib.sha256(
                (SCHEMA_DIR / source_operation["inputSchema"]).read_bytes()
            ).hexdigest()
        )
        assert (
            compiled_operation["outputSchemaDigest"]
            == hashlib.sha256(
                (SCHEMA_DIR / source_operation["outputSchema"]).read_bytes()
            ).hexdigest()
        )
        without_operations = {
            key: value for key, value in source_pack.items() if key != "operations"
        }
        assert {
            key: value for key, value in compiled_pack.items() if key != "operations"
        } == without_operations
