"""Deterministic compiler for the release-owned Personal Operator catalog."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

try:  # package import in Lambda and repository consumers
    from .contracts import (
        CapabilityCatalogV1,
        CapabilityPackV1,
        ContractValidationError,
        FROZEN_CATALOG_PACKS_V1,
        canonical_json_bytes,
    )
except ImportError:  # focused tests load this Lambda asset directory directly
    from contracts import (  # type: ignore[no-redef]
        CapabilityCatalogV1,
        CapabilityPackV1,
        ContractValidationError,
        FROZEN_CATALOG_PACKS_V1,
        canonical_json_bytes,
    )


CATALOG_SOURCE_SCHEMA = "personal-operator.capability-catalog-source.v1"
MAX_SCHEMA_ARTIFACT_BYTES = 64 * 1024
MAX_CATALOG_SOURCE_BYTES = 256 * 1024
_RELEASE_COMMIT = re.compile(r"[0-9a-f]{40}")
_SCHEMA_FILENAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.json")
_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_SCHEMA_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "title",
        "type",
        "additionalProperties",
        "properties",
        "required",
        "oneOf",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "enum",
        "const",
    }
)
_SCHEMA_TYPES = frozenset({"object", "array", "string", "integer", "boolean", "null"})


def _fail(message: str) -> None:
    raise ContractValidationError(message)


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("catalog artifact contains a duplicate JSON key")
        result[key] = value
    return result


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _assert_no_symlink_components(path: Path) -> None:
    absolute = _absolute_lexical(path)
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except OSError as error:
            raise ContractValidationError(
                f"catalog path component is absent: {component}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            _fail(f"catalog path component is symlinked: {component}")


def _require_regular_file(path: Path) -> None:
    _assert_no_symlink_components(path)
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise ContractValidationError(
            f"catalog artifact is absent: {path.name}"
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        _fail(f"catalog artifact is not a regular file: {path.name}")


def _load_canonical_artifact(path: Path, maximum: int) -> tuple[bytes, Any]:
    _require_regular_file(path)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ContractValidationError(
            f"catalog artifact cannot be read: {path.name}"
        ) from error
    if (
        not raw
        or len(raw) > maximum
        or not raw.endswith(b"\n")
        or raw.endswith(b"\n\n")
    ):
        _fail(f"catalog artifact has an invalid byte envelope: {path.name}")
    try:
        parsed = json.loads(
            raw[:-1].decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=lambda value: _fail(
                f"catalog artifact contains non-finite number {value}"
            ),
            parse_float=lambda value: _fail(
                f"catalog artifact contains unsupported float {value}"
            ),
        )
    except (
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RecursionError,
    ) as error:
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
    if not {"$schema", "$id", "title"}.issubset(value):
        _fail(f"tool schema root metadata is incomplete: {filename}")
    if value["$schema"] != _SCHEMA_DIALECT:
        _fail(f"tool schema dialect is not frozen: {filename}")
    if (
        not isinstance(value["$id"], str)
        or value["$id"] != f"urn:personal-operator:tool-schema:{filename[:-5]}:v1"
    ):
        _fail(f"tool schema ID is not bound to its filename: {filename}")
    if not isinstance(value["title"], str) or not value["title"]:
        _fail(f"tool schema title is invalid: {filename}")
    if value.get("type") == "object":
        pass
    elif (
        "oneOf" in value
        and isinstance(value["oneOf"], list)
        and all(
            isinstance(branch, Mapping) and branch.get("type") == "object"
            for branch in value["oneOf"]
        )
    ):
        pass
    else:
        _fail(f"tool schema root must be an exact object or object union: {filename}")

    def bounded_integer(item: Any, label: str) -> None:
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            _fail(f"tool schema {label} is invalid: {filename}")

    def validate_node(item: Any, *, root: bool = False) -> None:
        if not isinstance(item, Mapping):
            _fail(f"tool schema node must be an object: {filename}")
        unknown = set(item) - _SCHEMA_KEYWORDS
        if unknown:
            _fail(f"tool schema uses an unapproved keyword: {filename}")
        if not root and ({"$schema", "$id", "title"} & set(item)):
            _fail(f"tool schema metadata is only valid at the root: {filename}")
        if "type" in item and (
            not isinstance(item["type"], str) or item["type"] not in _SCHEMA_TYPES
        ):
            _fail(f"tool schema type is unsupported: {filename}")
        assertion_keys = set(item) - ({"$schema", "$id", "title"} if root else set())
        node_type = item.get("type")
        allowed_by_type = {
            "object": {"type", "additionalProperties", "properties", "required"},
            "array": {"type", "items", "minItems", "maxItems", "uniqueItems"},
            "string": {"type", "minLength", "maxLength", "pattern"},
            "integer": {"type", "minimum", "maximum"},
            "boolean": {"type"},
            "null": {"type"},
        }
        if node_type is not None:
            if not assertion_keys.issubset(allowed_by_type[node_type]):
                _fail(f"tool schema combines incompatible keywords: {filename}")
        elif assertion_keys not in ({"enum"}, {"const"}, {"oneOf"}):
            _fail(f"tool schema node has no closed assertion: {filename}")
        if "additionalProperties" in item and item["additionalProperties"] is not False:
            _fail(f"tool schema must close additional properties: {filename}")
        if "properties" in item:
            if (
                item.get("type") != "object"
                or item.get("additionalProperties") is not False
            ):
                _fail(f"tool schema object must be exact: {filename}")
            properties = item["properties"]
            if not isinstance(properties, Mapping):
                _fail(f"tool schema properties are invalid: {filename}")
            for property_name, child in properties.items():
                if not isinstance(property_name, str) or not property_name:
                    _fail(f"tool schema property name is invalid: {filename}")
                validate_node(child)
            required = item.get("required")
            if not isinstance(required, list) or any(
                not isinstance(name, str) for name in required
            ):
                _fail(f"tool schema required fields are invalid: {filename}")
            if (
                required != sorted(required)
                or len(set(required)) != len(required)
                or set(required) != set(properties)
            ):
                _fail(f"tool schema must require every exact property: {filename}")
        elif "required" in item or "additionalProperties" in item:
            _fail(f"tool schema object keywords are incomplete: {filename}")
        elif item.get("type") == "object":
            _fail(f"tool schema object shape is incomplete: {filename}")
        if "oneOf" in item:
            branches = item["oneOf"]
            if not isinstance(branches, list) or not 2 <= len(branches) <= 4:
                _fail(f"tool schema oneOf is not a bounded union: {filename}")
            if len({canonical_json_bytes(branch) for branch in branches}) != len(
                branches
            ):
                _fail(
                    f"tool schema oneOf branches must be canonically unique: {filename}"
                )
            for branch in branches:
                validate_node(branch)
        if "items" in item:
            if item.get("type") != "array":
                _fail(f"tool schema items require an array: {filename}")
            validate_node(item["items"])
        elif item.get("type") == "array":
            _fail(f"tool schema array shape is incomplete: {filename}")
        for keyword in (
            "minItems",
            "maxItems",
            "minLength",
            "maxLength",
            "minimum",
            "maximum",
        ):
            if keyword in item:
                bounded_integer(item[keyword], keyword)
        for minimum, maximum in (
            ("minItems", "maxItems"),
            ("minLength", "maxLength"),
            ("minimum", "maximum"),
        ):
            if minimum in item and maximum in item and item[minimum] > item[maximum]:
                _fail(f"tool schema bounds are inverted: {filename}")
        if "uniqueItems" in item and item["uniqueItems"] is not True:
            _fail(f"tool schema uniqueItems must be true: {filename}")
        if "enum" in item:
            choices = item["enum"]
            if (
                not isinstance(choices, list)
                or not choices
                or any(not isinstance(choice, str) or not choice for choice in choices)
                or len(set(choices)) != len(choices)
            ):
                _fail(f"tool schema enum is invalid: {filename}")
        if "const" in item and not (
            isinstance(item["const"], (str, bool, int))
            and not isinstance(item["const"], float)
        ):
            _fail(f"tool schema const is invalid: {filename}")
        if "pattern" in item:
            pattern = item["pattern"]
            if not isinstance(pattern, str) or len(pattern) > 256:
                _fail(f"tool schema pattern is invalid: {filename}")
            try:
                re.compile(pattern)
            except re.error as error:
                raise ContractValidationError(
                    f"tool schema pattern cannot compile: {filename}"
                ) from error

    validate_node(value, root=True)


def _closed_schema_inventory(schema_dir: Path) -> set[str]:
    try:
        entries = list(os.scandir(schema_dir))
    except OSError as error:
        raise ContractValidationError("schema_dir cannot be enumerated") from error
    names: set[str] = set()
    for entry in entries:
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as error:
            raise ContractValidationError(
                "schema directory entry cannot be inspected"
            ) from error
        if entry.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            _fail("schema directory must contain only non-symlink regular files")
        if _SCHEMA_FILENAME.fullmatch(entry.name) is None:
            _fail("schema directory contains an unapproved filename")
        names.add(entry.name)
    return names


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
            if (
                not isinstance(filename, str)
                or _SCHEMA_FILENAME.fullmatch(filename) is None
            ):
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
    configured_dir = _absolute_lexical(Path(schema_dir).expanduser())
    _assert_no_symlink_components(configured_dir)
    try:
        directory_metadata = os.lstat(configured_dir)
    except OSError as error:
        raise ContractValidationError("schema_dir is absent") from error
    if not stat.S_ISDIR(directory_metadata.st_mode):
        _fail("schema_dir must be a non-symlink directory")
    expected_files = _closed_schema_inventory(configured_dir)
    source_path = configured_dir.parent / "catalog-v1.json"
    _, source = _load_canonical_artifact(source_path, MAX_CATALOG_SOURCE_BYTES)
    source = _exact(source, "catalog source", {"schema", "packs"})
    if source["schema"] != CATALOG_SOURCE_SCHEMA:
        _fail("catalog source schema is invalid")
    if not isinstance(source["packs"], list) or len(source["packs"]) != 10:
        _fail("v1 catalog source must contain exactly ten tool packs")
    if canonical_json_bytes(source["packs"]) != canonical_json_bytes(
        FROZEN_CATALOG_PACKS_V1
    ):
        _fail("v1 catalog source does not equal the frozen ten-row authority matrix")

    seen_schema_files: set[str] = set()
    packs = [
        _source_pack(pack, configured_dir, seen_schema_files)
        for pack in source["packs"]
    ]
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
