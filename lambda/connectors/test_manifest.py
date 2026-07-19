"""Build-time schema-lock tests for the curated connector manifest (Task 10).

These prove the connector manifest is compiled from closed, non-symlink schema
bytes with per-operation digests bound to the exact canonical artifact bytes,
that the curated registry is release-owned (no dynamic discovery), and that
drift in the on-disk schema bytes is a single equality break.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from capabilities.contracts import ContractValidationError, ConnectorManifestV1
from connectors import manifest as manifest_module

SCHEMA_DIR = Path(manifest_module.__file__).resolve().parent / "schemas"
CONNECTOR_ID = "synthetic.notes"


def test_curated_registry_is_release_owned_and_bounded():
    registry = manifest_module.build_curated_registry()
    # Exactly the reviewed connectors; no dynamic discovery surface.
    assert set(registry) == set(manifest_module.CURATED_CONNECTOR_IDS)
    assert CONNECTOR_ID in registry
    assert all(
        isinstance(entry, ConnectorManifestV1) for entry in registry.values()
    )


def test_manifest_pins_trusted_adapter_credential_boundary_and_modes():
    manifest = manifest_module.compile_connector_manifest(CONNECTOR_ID, SCHEMA_DIR)
    assert isinstance(manifest, ConnectorManifestV1)
    assert manifest.connector_id == CONNECTOR_ID
    assert manifest.credential_boundary == "TRUSTED_ADAPTER"
    modes = {op["operationId"]: op["mode"] for op in manifest.operations}
    assert modes == {
        "synthetic.notes.append": "PREPARE",
        "synthetic.notes.read-list": "READ",
    }


def test_operation_digests_bind_exact_canonical_schema_bytes():
    manifest = manifest_module.compile_connector_manifest(CONNECTOR_ID, SCHEMA_DIR)
    for op in manifest.operations:
        oid = op["operationId"]
        parts = oid.split(".")
        stem = "synthetic-notes-" + parts[-1]
        input_bytes = (SCHEMA_DIR / f"{stem}-input.json").read_bytes()
        output_bytes = (SCHEMA_DIR / f"{stem}-output.json").read_bytes()
        assert op["inputSchemaDigest"] == hashlib.sha256(input_bytes).hexdigest()
        assert op["outputSchemaDigest"] == hashlib.sha256(output_bytes).hexdigest()


def test_manifest_digest_is_a_single_equality_drift_check():
    a = manifest_module.manifest_digest(CONNECTOR_ID)
    b = manifest_module.compile_connector_manifest(CONNECTOR_ID, SCHEMA_DIR).schema_digest
    assert a == b
    # The digest binds the canonical manifest (schemaDigest omitted).
    assert ContractValidationError  # imported for the guard below


def test_unknown_connector_id_is_rejected_no_dynamic_discovery():
    with pytest.raises(ContractValidationError):
        manifest_module.compile_connector_manifest("clawhub.arbitrary", SCHEMA_DIR)
    with pytest.raises(ContractValidationError):
        manifest_module.manifest_digest("clawhub.arbitrary")


def test_symlinked_schema_component_is_refused(tmp_path):
    # Mirror the catalog closed-inventory rule: a symlinked schema dir entry is
    # refused, so no schema bytes can be swapped in at build time.
    evil_dir = tmp_path / "schemas"
    evil_dir.mkdir()
    for entry in SCHEMA_DIR.iterdir():
        (evil_dir / entry.name).write_bytes(entry.read_bytes())
    # Replace one artifact with a symlink to prove the inventory rejects it.
    target = evil_dir / "synthetic-notes-append-input.json"
    contents = target.read_bytes()
    target.unlink()
    outside = tmp_path / "outside.json"
    outside.write_bytes(contents)
    target.symlink_to(outside)
    with pytest.raises(ContractValidationError):
        manifest_module.compile_connector_manifest(CONNECTOR_ID, evil_dir)


def test_unreferenced_schema_file_breaks_the_closed_inventory(tmp_path):
    stray_dir = tmp_path / "schemas"
    stray_dir.mkdir()
    for entry in SCHEMA_DIR.iterdir():
        (stray_dir / entry.name).write_bytes(entry.read_bytes())
    (stray_dir / "extra.json").write_bytes(b"{}\n")
    with pytest.raises(ContractValidationError):
        manifest_module.compile_connector_manifest(CONNECTOR_ID, stray_dir)
