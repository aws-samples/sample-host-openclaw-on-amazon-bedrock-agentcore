"""Build-time schema lock for the curated connector manifest (Task 10).

This mirrors :mod:`capabilities.catalog` byte-for-byte: it loads a closed,
non-symlink schema inventory, computes each operation's input/output schema
digest over the exact canonical repository artifact bytes (including the single
trailing LF), hard-pins ``credentialBoundary`` to ``TRUSTED_ADAPTER``, and sorts
operations. The curated connector set is a FROZEN release-owned constant so the
plane is never expanded by dynamic (ClawHub-style) discovery.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
from typing import Any

try:  # package import in Lambda and repository consumers
    from capabilities.catalog import (
        MAX_SCHEMA_ARTIFACT_BYTES,
        _closed_schema_inventory,
        _load_canonical_artifact,
        _validate_schema_document,
        _assert_no_symlink_components,
        _absolute_lexical,
    )
    from capabilities.contracts import (
        ConnectorManifestV1,
        ContractValidationError,
        canonical_sha256,
    )
except ImportError:  # pragma: no cover - bare-module load path (connector_manifest)
    from catalog import (  # type: ignore[no-redef]
        MAX_SCHEMA_ARTIFACT_BYTES,
        _closed_schema_inventory,
        _load_canonical_artifact,
        _validate_schema_document,
        _assert_no_symlink_components,
        _absolute_lexical,
    )
    from contracts import (  # type: ignore[no-redef]
        ConnectorManifestV1,
        ContractValidationError,
        canonical_sha256,
    )

CONNECTOR_MANIFEST_VERSION = "1.0.0"

# The FROZEN curated registry: connector_id -> reviewed operation source rows.
# Each operation names the exact schema artifact stems (``-input``/``-output``)
# resolved against the release-owned schema directory. There is no dynamic
# discovery: this dict is the entire set of connectors the release admits.
_CURATED_SOURCE: dict[str, tuple[dict[str, str], ...]] = {
    "synthetic.notes": (
        {
            "operationId": "synthetic.notes.append",
            "mode": "PREPARE",
            "inputStem": "synthetic-notes-append-input",
            "outputStem": "synthetic-notes-append-output",
        },
        {
            "operationId": "synthetic.notes.read-list",
            "mode": "READ",
            "inputStem": "synthetic-notes-read-list-input",
            "outputStem": "synthetic-notes-read-list-output",
        },
    ),
}

CURATED_CONNECTOR_IDS = tuple(sorted(_CURATED_SOURCE))


def _fail(message: str) -> None:
    raise ContractValidationError(message)


def _default_schema_dir() -> Path:
    return Path(__file__).resolve().parent / "schemas"


def schema_index_from_source(
    source_operations: Any,
) -> dict[str, tuple[str, str]]:
    """Map each operationId to its exact (input, output) schema filenames."""

    return {
        operation["operationId"]: (
            f"{operation['inputStem']}.json",
            f"{operation['outputStem']}.json",
        )
        for operation in source_operations
    }


def schema_index(connector_id: str) -> dict[str, tuple[str, str]]:
    """Schema-file index for a curated connector (reused by adapters)."""

    if connector_id not in _CURATED_SOURCE:
        _fail("connector id is not in the release-owned curated registry")
    return schema_index_from_source(_CURATED_SOURCE[connector_id])


def compile_manifest_from_source(
    connector_id: str,
    version: str,
    source_operations: Any,
    schema_dir: str | Path,
) -> ConnectorManifestV1:
    """Compile any reviewed connector source into a digest-bound manifest.

    ``schemaDigest`` binds the canonical manifest (with ``schemaDigest`` omitted)
    exactly as :class:`ConnectorManifestV1` requires. Every operation's
    input/output digest covers the exact canonical schema artifact bytes.
    """

    configured_dir = _absolute_lexical(Path(schema_dir).expanduser())
    _assert_no_symlink_components(configured_dir)
    try:
        directory_metadata = os.lstat(configured_dir)
    except OSError as error:
        raise ContractValidationError("connector schema_dir is absent") from error
    if not stat.S_ISDIR(directory_metadata.st_mode):
        _fail("connector schema_dir must be a non-symlink directory")

    expected_files = _closed_schema_inventory(configured_dir)
    seen_schema_files: set[str] = set()

    operations: list[dict[str, Any]] = []
    for source in source_operations:
        compiled: dict[str, Any] = {
            "operationId": source["operationId"],
            "mode": source["mode"],
        }
        for stem_field, digest_field in (
            ("inputStem", "inputSchemaDigest"),
            ("outputStem", "outputSchemaDigest"),
        ):
            filename = f"{source[stem_field]}.json"
            schema_path = configured_dir / filename
            if schema_path.parent != configured_dir:
                _fail("connector schema escaped the configured schema directory")
            raw_schema, parsed_schema = _load_canonical_artifact(
                schema_path, MAX_SCHEMA_ARTIFACT_BYTES
            )
            _validate_schema_document(parsed_schema, filename)
            seen_schema_files.add(filename)
            compiled[digest_field] = hashlib.sha256(raw_schema).hexdigest()
        operations.append(compiled)

    if seen_schema_files != expected_files:
        _fail("connector schema directory has an unreferenced or missing artifact")

    operations.sort(key=lambda item: item["operationId"])
    digest_input = {
        "schema": ConnectorManifestV1.SCHEMA,
        "connectorId": connector_id,
        "version": version,
        "operations": operations,
        "credentialBoundary": "TRUSTED_ADAPTER",
    }
    schema_digest = canonical_sha256(digest_input)
    return ConnectorManifestV1.from_mapping(
        {**digest_input, "schemaDigest": schema_digest}
    )


def compile_connector_manifest(
    connector_id: str, schema_dir: str | Path
) -> ConnectorManifestV1:
    """Compile a curated connector's reviewed source into a digest-bound manifest."""

    if connector_id not in _CURATED_SOURCE:
        _fail("connector id is not in the release-owned curated registry")
    return compile_manifest_from_source(
        connector_id,
        CONNECTOR_MANIFEST_VERSION,
        _CURATED_SOURCE[connector_id],
        schema_dir,
    )


def build_curated_registry(
    schema_dir: str | Path | None = None,
) -> dict[str, ConnectorManifestV1]:
    """Compile every curated connector into its frozen, reviewed manifest."""

    directory = Path(schema_dir) if schema_dir is not None else _default_schema_dir()
    return {
        connector_id: compile_connector_manifest(connector_id, directory)
        for connector_id in CURATED_CONNECTOR_IDS
    }


def manifest_digest(
    connector_id: str, schema_dir: str | Path | None = None
) -> str:
    """Return the single equality anchor that detects any manifest drift."""

    directory = Path(schema_dir) if schema_dir is not None else _default_schema_dir()
    return compile_connector_manifest(connector_id, directory).schema_digest


__all__ = [
    "CONNECTOR_MANIFEST_VERSION",
    "CURATED_CONNECTOR_IDS",
    "build_curated_registry",
    "compile_connector_manifest",
    "compile_manifest_from_source",
    "manifest_digest",
    "schema_index",
    "schema_index_from_source",
]
