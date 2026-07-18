"""Deterministic compiler for the release-owned Personal Operator catalog."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

try:  # package import in Lambda and repository consumers
    from .contracts import (
        CapabilityCatalogV1,
        CapabilityPackV1,
        ContractValidationError,
        canonical_json_bytes,
    )
except ImportError:  # focused tests load this Lambda asset directory directly
    from contracts import (  # type: ignore[no-redef]
        CapabilityCatalogV1,
        CapabilityPackV1,
        ContractValidationError,
        canonical_json_bytes,
    )


CATALOG_SOURCE_SCHEMA = "personal-operator.capability-catalog-source.v1"
MAX_SCHEMA_ARTIFACT_BYTES = 64 * 1024
MAX_CATALOG_SOURCE_BYTES = 256 * 1024
_RELEASE_COMMIT = re.compile(r"[0-9a-f]{40}")
_SCHEMA_FILENAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.json")
_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def _fail(message: str) -> None:
    raise ContractValidationError(message)


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("catalog artifact contains a duplicate JSON key")
        result[key] = value
    return result


def _load_canonical_artifact(path: Path, maximum: int) -> tuple[bytes, Any]:
    if path.is_symlink() or not path.is_file():
        _fail(f"catalog artifact is absent or symlinked: {path.name}")
    raw = path.read_bytes()
    if not raw or len(raw) > maximum or not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        _fail(f"catalog artifact has an invalid byte envelope: {path.name}")
    try:
        parsed = json.loads(
            raw[:-1].decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=lambda value: _fail(
                f"catalog artifact contains non-finite number {value}"
            ),
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        if isinstance(error, ContractValidationError):
            raise
        raise ContractValidationError(
            f"catalog artifact is not strict UTF-8 JSON: {path.name}"
        ) from error
    if canonical_json_bytes(parsed) + b"\n" != raw:
        _fail(f"catalog artifact is not canonical JSON plus LF: {path.name}")
    return raw, parsed


def _exact(value: Any, label: str, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        _fail(f"{label} must contain its exact source fields")
    return dict(value)


def _validate_schema_document(value: Any, filename: str) -> None:
    if not isinstance(value, Mapping):
        _fail(f"tool schema must be an object: {filename}")
    required_top_level = {
        "$schema",
        "$id",
        "title",
        "type",
        "additionalProperties",
        "properties",
        "required",
    }
    if set(value) != required_top_level:
        _fail(f"tool schema has an unexpected top-level shape: {filename}")
    if value["$schema"] != _SCHEMA_DIALECT:
        _fail(f"tool schema dialect is not frozen: {filename}")
    if (
        not isinstance(value["$id"], str)
        or value["$id"] != f"urn:personal-operator:tool-schema:{filename[:-5]}:v1"
    ):
        _fail(f"tool schema ID is not bound to its filename: {filename}")
    if not isinstance(value["title"], str) or not value["title"]:
        _fail(f"tool schema title is invalid: {filename}")
    if value["type"] != "object" or value["additionalProperties"] is not False:
        _fail(f"tool schema must be an exact object: {filename}")
    if not isinstance(value["properties"], Mapping):
        _fail(f"tool schema properties are invalid: {filename}")
    if (
        not isinstance(value["required"], list)
        or value["required"] != sorted(value["required"])
        or len(set(value["required"])) != len(value["required"])
        or not set(value["required"]).issubset(value["properties"])
    ):
        _fail(f"tool schema required fields are invalid: {filename}")

    def reject_dynamic_or_remote_schema(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if key in {"$ref", "$dynamicRef", "$dynamicAnchor"}:
                    _fail(f"tool schema cannot load dynamic or remote schema: {filename}")
                reject_dynamic_or_remote_schema(nested)
        elif isinstance(item, list):
            for nested in item:
                reject_dynamic_or_remote_schema(nested)

    reject_dynamic_or_remote_schema(value)


def _source_pack(
    raw_pack: Any, schema_dir: Path, seen_schema_files: set[str]
) -> dict[str, Any]:
    pack = _exact(
        raw_pack,
        "catalog source pack",
        {
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
        },
    )
    if not isinstance(pack["operations"], list) or len(pack["operations"]) != 1:
        _fail("each v1 catalog source pack must expose exactly one model tool")
    compiled_operations = []
    for raw_operation in pack["operations"]:
        operation = _exact(
            raw_operation,
            "catalog source operation",
            {"operationId", "toolName", "inputSchema", "outputSchema"},
        )
        compiled: dict[str, Any] = {
            "operationId": operation["operationId"],
            "toolName": operation["toolName"],
        }
        for source_field, digest_field in (
            ("inputSchema", "inputSchemaDigest"),
            ("outputSchema", "outputSchemaDigest"),
        ):
            filename = operation[source_field]
            if not isinstance(filename, str) or _SCHEMA_FILENAME.fullmatch(filename) is None:
                _fail("catalog schema filename is unsafe")
            schema_path = schema_dir / filename
            if schema_path.parent != schema_dir:
                _fail("catalog schema escaped the configured schema directory")
            raw_schema, parsed_schema = _load_canonical_artifact(
                schema_path, MAX_SCHEMA_ARTIFACT_BYTES
            )
            _validate_schema_document(parsed_schema, filename)
            seen_schema_files.add(filename)
            compiled[digest_field] = hashlib.sha256(raw_schema).hexdigest()
        compiled_operations.append(compiled)
    pack["operations"] = compiled_operations
    return CapabilityPackV1.from_mapping(pack).to_mapping()


def compile_catalog(
    release_commit: str, schema_dir: str | Path
) -> tuple[bytes, CapabilityCatalogV1]:
    """Compile source plus exact schema bytes into one digest-bound catalog.

    ``schema_dir`` must be ``.../capabilities/schemas`` and its parent must
    contain ``catalog-v1.json``. ``catalogDigest`` is deliberately
    non-self-referential: SHA-256 over canonical compiled catalog bytes with
    only ``catalogDigest`` omitted. Input/output schema digests cover the exact
    canonical repository artifact bytes, including their single trailing LF.
    """

    if (
        not isinstance(release_commit, str)
        or _RELEASE_COMMIT.fullmatch(release_commit) is None
    ):
        _fail("release commit must be an exact lowercase 40-character Git SHA")
    if isinstance(schema_dir, bool) or not isinstance(schema_dir, (str, Path)):
        _fail("schema_dir must identify the frozen schema directory")
    configured_dir = Path(schema_dir)
    if configured_dir.is_symlink() or not configured_dir.is_dir():
        _fail("schema_dir is absent or symlinked")
    configured_dir = configured_dir.resolve()
    source_path = configured_dir.parent / "catalog-v1.json"
    _, source = _load_canonical_artifact(source_path, MAX_CATALOG_SOURCE_BYTES)
    source = _exact(source, "catalog source", {"schema", "packs"})
    if source["schema"] != CATALOG_SOURCE_SCHEMA:
        _fail("catalog source schema is invalid")
    if not isinstance(source["packs"], list) or len(source["packs"]) != 10:
        _fail("v1 catalog source must contain exactly ten tool packs")

    seen_schema_files: set[str] = set()
    packs = [
        _source_pack(pack, configured_dir, seen_schema_files)
        for pack in source["packs"]
    ]
    expected_files = {path.name for path in configured_dir.glob("*.json") if path.is_file()}
    if seen_schema_files != expected_files:
        _fail("schema directory contains an unreferenced or missing catalog schema")

    digest_input = {
        "schema": CapabilityCatalogV1.SCHEMA,
        "releaseCommit": release_commit,
        "packs": packs,
    }
    catalog_digest = hashlib.sha256(canonical_json_bytes(digest_input)).hexdigest()
    catalog = CapabilityCatalogV1.from_mapping(
        {**digest_input, "catalogDigest": catalog_digest}
    )
    return catalog.to_bytes(), catalog


__all__ = [
    "CATALOG_SOURCE_SCHEMA",
    "MAX_CATALOG_SOURCE_BYTES",
    "MAX_SCHEMA_ARTIFACT_BYTES",
    "compile_catalog",
]
