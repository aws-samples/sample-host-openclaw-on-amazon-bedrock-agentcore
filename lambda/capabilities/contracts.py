"""Dependency-free, fail-closed canonical contracts for Personal Operator v1.

The wire format is UTF-8 canonical JSON with sorted object keys, no whitespace,
and no trailing newline. Repository JSON artifacts use the same encoding plus
one LF; that enclosing artifact rule is enforced by :mod:`catalog`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import re
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, Sequence
from urllib.parse import urlsplit


MAX_SAFE_INTEGER = 2**53 - 1
RISK_CLASSES = frozenset(
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
RESULT_STATUSES = frozenset(
    {"SUCCEEDED", "PENDING_APPROVAL", "DENIED", "FAILED_RETRYABLE", "UNCERTAIN"}
)
APPROVAL_POLICIES = frozenset(
    {
        "NONE",
        "CURRENT_REQUEST_TARGET_GRANT",
        "EXACT_ONE_TIME",
        "EXACT_ONE_TIME_PROPOSAL",
    }
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_RELEASE_COMMIT = re.compile(r"[0-9a-f]{40}")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_PACK_ID = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9-]+){1,7}")
_OPERATION_ID = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9-]+){1,7}")
_TOOL_NAME = re.compile(r"po_[a-z0-9]+(?:_[a-z0-9]+){1,7}")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_TIMEZONE = re.compile(
    r"(?:UTC|[A-Za-z]+(?:[_-][A-Za-z]+)*/[A-Za-z]+(?:[_-][A-Za-z]+)*)"
)
_PATH_SEGMENT = re.compile(r"[^/\\\x00-\x1f\x7f]+")
_RUNTIME_ARN = re.compile(
    r"arn:aws:bedrock-agentcore:eu-west-1:[0-9]{12}:runtime/"
    r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
)
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_LEGACY_IPV4_COMPONENT = re.compile(r"(?:[0-9]+|0x[0-9a-f]+)")
_HEX_PAIR = re.compile(r"[0-9A-F]{2}")


class ContractValidationError(ValueError):
    """A value cannot cross a canonical Personal Operator boundary."""


@dataclass(frozen=True, slots=True)
class ContractLimits:
    """Parser resource limits, configurable only by the trusted caller."""

    max_bytes: int = 256 * 1024
    max_depth: int = 16
    max_collection_items: int = 4096
    max_string_chars: int = 32 * 1024
    max_total_nodes: int = 32_768

    def __post_init__(self) -> None:
        for name in (
            "max_bytes",
            "max_depth",
            "max_collection_items",
            "max_string_chars",
            "max_total_nodes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ContractValidationError(f"{name} must be a positive integer")


DEFAULT_LIMITS = ContractLimits()


def _fail(message: str) -> None:
    raise ContractValidationError(message)


def _validate_json_tree(value: Any, limits: ContractLimits) -> None:
    nodes = 0
    active_containers: set[int] = set()

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > limits.max_total_nodes:
            _fail("canonical JSON exceeds the total node limit")
        if depth > limits.max_depth:
            _fail("canonical JSON exceeds the depth limit")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, str):
            if len(item) > limits.max_string_chars:
                _fail("canonical JSON string exceeds the character limit")
            return
        if isinstance(item, int):
            if abs(item) > MAX_SAFE_INTEGER:
                _fail("canonical JSON integer exceeds the interoperable range")
            return
        if isinstance(item, float):
            _fail("canonical JSON floats are unsupported")
        if isinstance(item, Mapping):
            if len(item) > limits.max_collection_items:
                _fail("canonical JSON object exceeds the item limit")
            identity = id(item)
            if identity in active_containers:
                _fail("canonical JSON contains a container cycle")
            active_containers.add(identity)
            try:
                for key, nested in item.items():
                    if not isinstance(key, str) or not key:
                        _fail("canonical JSON object keys must be non-empty strings")
                    if len(key) > limits.max_string_chars:
                        _fail("canonical JSON object key exceeds the character limit")
                    visit(nested, depth + 1)
            finally:
                active_containers.remove(identity)
            return
        if isinstance(item, (list, tuple)):
            if len(item) > limits.max_collection_items:
                _fail("canonical JSON array exceeds the item limit")
            identity = id(item)
            if identity in active_containers:
                _fail("canonical JSON contains a container cycle")
            active_containers.add(identity)
            try:
                for nested in item:
                    visit(nested, depth + 1)
            finally:
                active_containers.remove(identity)
            return
        _fail("canonical JSON contains an unsupported value")

    try:
        visit(value, 0)
    except RecursionError as error:
        raise ContractValidationError(
            "canonical JSON exceeds the depth limit"
        ) from error


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(nested) for key, nested in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(nested) for nested in value]
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(nested) for key, nested in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(nested) for nested in value)
    return value


def _catalog_source_pack(
    pack_id: str,
    operation_id: str,
    tool_name: str,
    input_schema: str,
    output_schema: str,
    risk_class: str,
    credential_boundary: str,
    approval_mode: str,
    target_mode: str,
    retry_mode: str,
    max_calls: int,
    max_input_bytes: int,
    max_output_bytes: int,
    retention_class: str,
    max_days: int,
    deletion_behavior: str,
) -> dict[str, Any]:
    """Build one release-owned row; every authority field is explicit."""

    return {
        "packId": pack_id,
        "version": "1.0.0",
        "riskClass": risk_class,
        "credentialBoundary": credential_boundary,
        "operations": [
            {
                "operationId": operation_id,
                "toolName": tool_name,
                "inputSchema": input_schema,
                "outputSchema": output_schema,
            }
        ],
        "approvalPolicy": {"mode": approval_mode, "standingAllowed": False},
        "targetPolicy": {"mode": target_mode},
        "retryPolicy": {
            "mode": retry_mode,
            "onUncertain": "STOP_AND_RECONCILE",
        },
        "quotaPolicy": {
            "maxCallsPerTurn": max_calls,
            "maxInputBytes": max_input_bytes,
            "maxOutputBytes": max_output_bytes,
        },
        "retentionPolicy": {"class": retention_class, "maxDays": max_days},
        "deletionPolicy": {
            "authorityFence": "REQUIRED",
            "behavior": deletion_behavior,
        },
    }


FROZEN_CATALOG_PACKS_V1 = _freeze(
    (
        _catalog_source_pack(
            "workspace.file-list",
            "workspace.file.list",
            "po_file_list",
            "po-file-list-input.json",
            "po-file-list-output.json",
            "LOCAL_READ",
            "WORKSPACE_SCOPED_SESSION",
            "NONE",
            "SESSION_WORKSPACE",
            "READ_ONLY",
            8,
            262144,
            1048576,
            "WORKSPACE_LIFECYCLE",
            30,
            "PURGE_WITH_WORKSPACE",
        ),
        _catalog_source_pack(
            "workspace.file-read",
            "workspace.file.read",
            "po_file_read",
            "po-file-read-input.json",
            "po-file-read-output.json",
            "LOCAL_READ",
            "WORKSPACE_SCOPED_SESSION",
            "NONE",
            "SESSION_WORKSPACE",
            "READ_ONLY",
            8,
            262144,
            262144,
            "WORKSPACE_LIFECYCLE",
            30,
            "PURGE_WITH_WORKSPACE",
        ),
        _catalog_source_pack(
            "workspace.file-write",
            "workspace.file.write",
            "po_file_write",
            "po-file-write-input.json",
            "po-file-write-output.json",
            "LOCAL_MUTATION",
            "WORKSPACE_SCOPED_SESSION",
            "NONE",
            "SESSION_WORKSPACE",
            "IDEMPOTENT",
            8,
            262144,
            262144,
            "WORKSPACE_LIFECYCLE",
            30,
            "PURGE_WITH_WORKSPACE",
        ),
        _catalog_source_pack(
            "workspace.file-delete",
            "workspace.file.delete",
            "po_file_delete",
            "po-file-delete-input.json",
            "po-file-delete-output.json",
            "LOCAL_MUTATION",
            "WORKSPACE_SCOPED_SESSION",
            "NONE",
            "SESSION_WORKSPACE",
            "IDEMPOTENT",
            8,
            262144,
            262144,
            "WORKSPACE_LIFECYCLE",
            30,
            "PURGE_WITH_WORKSPACE",
        ),
        _catalog_source_pack(
            "web.exact-read",
            "web.exact.read",
            "po_web_read",
            "po-web-read-input.json",
            "po-web-read-output.json",
            "PUBLIC_READ",
            "NONE",
            "CURRENT_REQUEST_TARGET_GRANT",
            "EXACT_PUBLIC_URL",
            "READ_ONLY",
            4,
            4096,
            65536,
            "NONE",
            0,
            "REVOKE_AND_PURGE",
        ),
        _catalog_source_pack(
            "schedule.list",
            "schedule.list",
            "po_schedule_list",
            "po-schedule-list-input.json",
            "po-schedule-list-output.json",
            "LOCAL_READ",
            "TRUSTED_CONTROL_PLANE",
            "NONE",
            "CONTROL_PLANE_RECORD",
            "READ_ONLY",
            8,
            262144,
            1048576,
            "CONTROL_RECORD",
            90,
            "CANCEL_AND_PURGE",
        ),
        _catalog_source_pack(
            "schedule.propose",
            "schedule.propose",
            "po_schedule_propose",
            "po-schedule-propose-input.json",
            "po-schedule-propose-output.json",
            "DURABLE_MUTATION",
            "TRUSTED_CONTROL_PLANE",
            "EXACT_ONE_TIME_PROPOSAL",
            "CONTROL_PLANE_RECORD",
            "DEDUPE_KEY_REQUIRED",
            8,
            262144,
            262144,
            "CONTROL_RECORD",
            90,
            "CANCEL_AND_PURGE",
        ),
        _catalog_source_pack(
            "schedule.cancel-propose",
            "schedule.cancel.propose",
            "po_schedule_cancel_propose",
            "po-schedule-cancel-propose-input.json",
            "po-schedule-cancel-propose-output.json",
            "DURABLE_MUTATION",
            "TRUSTED_CONTROL_PLANE",
            "EXACT_ONE_TIME_PROPOSAL",
            "CONTROL_PLANE_RECORD",
            "DEDUPE_KEY_REQUIRED",
            8,
            262144,
            262144,
            "CONTROL_RECORD",
            90,
            "CANCEL_AND_PURGE",
        ),
        _catalog_source_pack(
            "compute.run",
            "compute.run",
            "po_compute_run",
            "po-compute-run-input.json",
            "po-compute-run-output.json",
            "LOCAL_MUTATION",
            "NETWORKLESS_COMPUTE",
            "NONE",
            "FRESH_JOB_NAMESPACE",
            "DEDUPE_KEY_REQUIRED",
            2,
            1048576,
            1048576,
            "JOB_RECEIPT",
            90,
            "FENCE_CANCEL_PURGE",
        ),
        _catalog_source_pack(
            "compute.status",
            "compute.status",
            "po_compute_status",
            "po-compute-status-input.json",
            "po-compute-status-output.json",
            "LOCAL_READ",
            "NETWORKLESS_COMPUTE",
            "NONE",
            "FRESH_JOB_NAMESPACE",
            "READ_ONLY",
            8,
            262144,
            1048576,
            "JOB_RECEIPT",
            90,
            "FENCE_CANCEL_PURGE",
        ),
    )
)

# Reviewed exact-schema boundary for the v1 authority matrix.  These values bind
# each operation to the repository-owned canonical input/output schema bytes;
# callers cannot move a valid digest to another operation or substitute a
# different, internally self-consistent schema.
FROZEN_OPERATION_SCHEMA_DIGESTS_V1 = MappingProxyType(
    {
        "workspace.file.list": MappingProxyType(
            {
                "inputSchemaDigest": "35e141ebe098d3cbc73ef1655e93ed743520f76cab08a533574c1262330d344a",
                "outputSchemaDigest": "6a6e9fe40d241f055d5509e7c337a1c279ba4e175fbddfed5bd9cc76ad09663f",
            }
        ),
        "workspace.file.read": MappingProxyType(
            {
                "inputSchemaDigest": "adf79b35ad5da0e5ccfba630254e1b8cb6b0ee41db07cfe34d7d5004851bfb5f",
                "outputSchemaDigest": "f6d5ec6a082465d3b66d4af47bdc41cc0fdcded0adfed706e14d18019188b34a",
            }
        ),
        "workspace.file.write": MappingProxyType(
            {
                "inputSchemaDigest": "3c83b398c5709887c626fd70d36e251cce8a0c96c7b28080c530ff2c587d652d",
                "outputSchemaDigest": "efacdca9b890b9ba2cb239a25488f487d00c4a6f070f532b2910d6cbd88e0052",
            }
        ),
        "workspace.file.delete": MappingProxyType(
            {
                "inputSchemaDigest": "b731089b54f1c87185741f0045d04555a1e77fb05ded2c7a7e3ce4389759b224",
                "outputSchemaDigest": "657d345a47d261692729a01ca96a6cd0d9b9b65c7bb862989503ee111cfd2d19",
            }
        ),
        "web.exact.read": MappingProxyType(
            {
                "inputSchemaDigest": "3ce72f18e68a9c53329670e403dbf176c8fb5203d25d23438f29f5285defd80f",
                "outputSchemaDigest": "c241a4bd2a4c986821f0aa71b4c6c883319c43598e3904e18e52ece8ad6e99e5",
            }
        ),
        "schedule.list": MappingProxyType(
            {
                "inputSchemaDigest": "1ea525d11a67b6581efb9f064818e5da4782914ed8e7ef93c295ab3c93e3a710",
                "outputSchemaDigest": "605a0da1316c22151b3e64d8be0a6fc5168353e7e9a99097ec35758f6c4986fa",
            }
        ),
        "schedule.propose": MappingProxyType(
            {
                "inputSchemaDigest": "e3586bade20d0d184b65d5c79b41c22703c4feccd9fdbdb137fafc1006b3f12b",
                "outputSchemaDigest": "8a82e3987b4f193e6c0edf8ca98245c950183883bf688f1d3690f7a05410198d",
            }
        ),
        "schedule.cancel.propose": MappingProxyType(
            {
                "inputSchemaDigest": "fdee4f975779c5e83d216d20b1aabd68623c257903bd1d86ac3b776116383f15",
                "outputSchemaDigest": "311792ffe90921e24eca03e043c21b5b790e32be1046a0dd533b148d0c98fa78",
            }
        ),
        "compute.run": MappingProxyType(
            {
                "inputSchemaDigest": "01cf09ff29529611b51bbee73b86f32b99a6814f7319ba27b18bef5e579b2a1d",
                "outputSchemaDigest": "ea26fc131f78e0377d5fbda50e8b1b9cf688d1c43f5a3a61dd3bd66953c08eb4",
            }
        ),
        "compute.status": MappingProxyType(
            {
                "inputSchemaDigest": "54c5277b5f0b1da875e5c4321161cff110ab3ef9048409c9f1e7d7a70ab938d8",
                "outputSchemaDigest": "144dd0d56b7897d0dfe225fdb20780e30225cddf7da19b7acbdbbd4ab1618c08",
            }
        ),
    }
)

_OPERATION_METADATA_V1 = MappingProxyType(
    {
        pack["operations"][0]["operationId"]: MappingProxyType(
            {
                "packId": pack["packId"],
                "toolName": pack["operations"][0]["toolName"],
                "approvalMode": pack["approvalPolicy"]["mode"],
                "retryMode": pack["retryPolicy"]["mode"],
            }
        )
        for pack in FROZEN_CATALOG_PACKS_V1
    }
)


def canonical_json_bytes(
    value: Any, *, limits: ContractLimits = DEFAULT_LIMITS
) -> bytes:
    """Return the one accepted JSON byte representation for ``value``."""

    _validate_json_tree(value, limits)
    try:
        plain = _thaw(value)
        encoded = json.dumps(
            plain,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ContractValidationError(
            "value cannot be encoded as canonical JSON"
        ) from error
    if len(encoded) > limits.max_bytes:
        _fail("canonical JSON exceeds the byte limit")
    return encoded


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _domain_hash(domain: bytes, *values: object) -> str:
    digest = hashlib.sha256(domain + b"\0")
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            _fail("hash identity components must be strings or integers")
        digest.update(str(value).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def derive_call_id(
    invocation_id: str,
    tool_use_id: str,
    catalog_digest: str,
    operation_id: str,
    tool_name: str,
    args_hash: str,
) -> str:
    _string(invocation_id, "invocationId", pattern=_OPAQUE_ID)
    _string(tool_use_id, "toolUseId", pattern=_OPAQUE_ID)
    _sha256(catalog_digest, "catalogDigest")
    _validate_tool_identity(operation_id, tool_name)
    _sha256(args_hash, "argsHash")
    return f"call_{_domain_hash(b'personal-operator.capability-call.v1', invocation_id, tool_use_id, catalog_digest, operation_id, tool_name, args_hash)}"


def derive_target_hash(
    normalized_target: str,
    method: str,
    redirect_policy: str,
    expires_at: int,
    max_uses: int,
    request_id: str,
) -> str:
    _public_https_url(normalized_target)
    _enum(method, "method", {"GET"})
    _enum(redirect_policy, "redirectPolicy", {"NO_REDIRECT", "SAME_HOST"})
    _integer(expires_at, "expiresAt")
    _integer(max_uses, "maxUses", minimum=1, maximum=3)
    _string(request_id, "currentRequestId", pattern=_OPAQUE_ID)
    return hashlib.sha256(
        b"personal-operator.target-grant.v1\0"
        + canonical_json_bytes(
            {
                "schema": "personal-operator.target-grant.v1",
                "normalizedTarget": normalized_target,
                "method": method,
                "redirectPolicy": redirect_policy,
                "expiresAt": expires_at,
                "maxUses": max_uses,
                "currentRequestId": request_id,
            }
        )
    ).hexdigest()


def derive_occurrence_id(
    schedule_id: str, generation: int, occurrence_time: int
) -> str:
    _string(schedule_id, "scheduleId", pattern=_OPAQUE_ID)
    _integer(generation, "generation", minimum=1)
    _integer(occurrence_time, "occurrenceTime", minimum=0)
    return f"occ_{_domain_hash(b'personal-operator.schedule-occurrence.v1', schedule_id, generation, occurrence_time)}"


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be an object")
    _validate_json_tree(value, DEFAULT_LIMITS)
    try:
        return {key: _thaw(nested) for key, nested in value.items()}
    except RecursionError as error:
        raise ContractValidationError(f"{label} exceeds the depth limit") from error


def _exact(value: Any, label: str, fields: Sequence[str]) -> dict[str, Any]:
    result = _mapping(value, label)
    if set(result) != set(fields):
        _fail(f"{label} must contain its exact canonical fields")
    return result


def _string(
    value: Any,
    label: str,
    *,
    pattern: re.Pattern[str] | None = None,
    maximum: int = 1024,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\x00" in value
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        _fail(f"{label} is invalid")
    return value


def _optional_string(value: Any, label: str, *, maximum: int = 1024) -> str | None:
    return None if value is None else _string(value, label, maximum=maximum)


def _enum(value: Any, label: str, allowed: set[str] | frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail(f"{label} is unsupported")
    return value


def _integer(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_SAFE_INTEGER,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        _fail(f"{label} must be an integer in range")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{label} must be a boolean")
    return value


def _sha256(value: Any, label: str) -> str:
    return _string(value, label, pattern=_SHA256, maximum=64)


def _release_commit(value: Any) -> str:
    return _string(value, "releaseCommit", pattern=_RELEASE_COMMIT, maximum=40)


def _safe_path(value: Any, label: str = "path") -> str:
    path = _string(value, label, maximum=512)
    if path.startswith(("/", "\\")) or "\\" in path:
        _fail(f"{label} must be a safe relative path")
    segments = path.split("/")
    if any(
        segment in {"", ".", ".."} or _PATH_SEGMENT.fullmatch(segment) is None
        for segment in segments
    ):
        _fail(f"{label} must be a safe relative path")
    return path


def _public_https_url(value: Any) -> str:
    target = _string(value, "normalizedTarget", maximum=2048)
    try:
        target.encode("ascii", errors="strict")
    except UnicodeError as error:
        raise ContractValidationError(
            "normalizedTarget must use an ASCII URI form"
        ) from error
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in target):
        _fail("normalizedTarget contains control characters")
    if "\\" in target:
        _fail("normalizedTarget contains an ambiguous path separator")
    try:
        parsed = urlsplit(target)
        _ = parsed.port
    except ValueError as error:
        raise ContractValidationError("normalizedTarget is invalid") from error
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.netloc == ""
        or parsed.path == ""
    ):
        _fail("normalizedTarget must be an exact public HTTPS URL")
    if parsed.geturl() != target or host != host.lower() or "%" in parsed.netloc:
        _fail("normalizedTarget is not normalized")

    canonical_authority: str
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            host.encode("ascii", errors="strict")
        except UnicodeError as error:
            raise ContractValidationError(
                "normalizedTarget hostname must be ASCII"
            ) from error
        labels = host.split(".")
        if (
            not labels
            or len(labels) < 2
            or any(_DNS_LABEL.fullmatch(label) is None for label in labels)
            or len(host) > 253
            or host in {"localhost", "localhost.localdomain"}
            or host.endswith(
                (".localhost", ".local", ".internal", ".lan", ".home", ".localdomain")
            )
            or labels[-1]
            in {
                "invalid",
                "localhost",
                "local",
                "internal",
                "lan",
                "home",
                "onion",
                "test",
            }
            or all(_LEGACY_IPV4_COMPONENT.fullmatch(label) for label in labels)
            or host.startswith("0x")
        ):
            _fail("normalizedTarget hostname is not a canonical public name")
        canonical_authority = host
    else:
        if (
            not address.is_global
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
            or address.is_loopback
            or address.is_link_local
            or address.is_private
            or getattr(address, "is_site_local", False)
            or (
                isinstance(address, ipaddress.IPv6Address)
                and address.ipv4_mapped is not None
            )
        ):
            _fail("normalizedTarget IP literal is not globally routable")
        if isinstance(address, ipaddress.IPv6Address):
            canonical_authority = f"[{address.compressed}]"
        else:
            canonical_authority = str(address)

    if parsed.netloc != canonical_authority:
        _fail("normalizedTarget authority is not canonical")
    if any(segment in {".", ".."} for segment in parsed.path.split("/")):
        _fail("normalizedTarget contains a path-normalization alias")

    for component in (parsed.path, parsed.query):
        offset = 0
        while True:
            percent = component.find("%", offset)
            if percent < 0:
                break
            encoded = component[percent + 1 : percent + 3]
            if len(encoded) != 2 or _HEX_PAIR.fullmatch(encoded) is None:
                _fail("normalizedTarget contains a non-canonical percent escape")
            decoded = chr(int(encoded, 16))
            if decoded.isalnum() or decoded in "-._~/%\\":
                _fail("normalizedTarget contains an ambiguous percent escape")
            offset = percent + 3
    return target


def _string_list(
    value: Any,
    label: str,
    *,
    pattern: re.Pattern[str] | None = None,
    maximum_items: int = 64,
    sorted_unique: bool = False,
) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum_items:
        _fail(f"{label} must be a bounded array")
    result = [_string(item, label, pattern=pattern) for item in value]
    if len(set(result)) != len(result):
        _fail(f"{label} must not contain duplicates")
    if sorted_unique and result != sorted(result):
        _fail(f"{label} must be sorted")
    return result


def _file_records(
    value: Any, label: str, *, maximum_items: int = 128
) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum_items:
        _fail(f"{label} must be a bounded array")
    records: list[dict[str, Any]] = []
    paths: list[str] = []
    for item in value:
        record = _exact(item, label, ("path", "sha256", "size"))
        record["path"] = _safe_path(record["path"])
        record["sha256"] = _sha256(record["sha256"], "sha256")
        record["size"] = _integer(record["size"], "size", maximum=64 * 1024 * 1024)
        paths.append(record["path"])
        records.append(record)
    if paths != sorted(paths) or len(set(paths)) != len(paths):
        _fail(f"{label} paths must be sorted and unique")
    return records


def _camel_to_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


@dataclass(frozen=True, slots=True)
class ContractValue:
    """Immutable validated value retaining only deeply frozen data."""

    _data: Mapping[str, Any]
    _wire: bytes = field(init=False, repr=False, compare=True)

    SCHEMA: ClassVar[str]
    FIELDS: ClassVar[tuple[str, ...]]

    def __post_init__(self) -> None:
        validated = self._validate_mapping(self._data)
        frozen = _freeze(validated)
        object.__setattr__(self, "_data", frozen)
        object.__setattr__(self, "_wire", canonical_json_bytes(validated))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ContractValue":
        return cls(value)

    @classmethod
    def _base(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result = _exact(value, cls.__name__, cls.FIELDS)
        if result.get("schema") != cls.SCHEMA:
            _fail(f"{cls.__name__} schema discriminator is invalid")
        return result

    @classmethod
    def _validate_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        return cls._base(value)

    @property
    def data(self) -> Mapping[str, Any]:
        return self._data

    @property
    def schema(self) -> str:
        return self.SCHEMA

    def __getattr__(self, name: str) -> Any:
        for wire_name in self.FIELDS:
            if _camel_to_snake(wire_name) == name:
                return self._data[wire_name]
        raise AttributeError(name)

    def to_mapping(self) -> dict[str, Any]:
        return _thaw(self._data)

    def to_bytes(self) -> bytes:
        return self._wire


@dataclass(frozen=True, slots=True)
class CapabilityPackV1:
    _data: Mapping[str, Any]

    FIELDS: ClassVar[tuple[str, ...]] = (
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
    )

    def __post_init__(self) -> None:
        value = _exact(self._data, "CapabilityPackV1", self.FIELDS)
        value["packId"] = _string(value["packId"], "packId", pattern=_PACK_ID)
        value["version"] = _string(
            value["version"], "version", pattern=_VERSION, maximum=32
        )
        value["riskClass"] = _enum(value["riskClass"], "riskClass", RISK_CLASSES)
        value["credentialBoundary"] = _enum(
            value["credentialBoundary"],
            "credentialBoundary",
            {
                "NONE",
                "WORKSPACE_SCOPED_SESSION",
                "TRUSTED_CONTROL_PLANE",
                "NETWORKLESS_COMPUTE",
            },
        )
        operations = value["operations"]
        if not isinstance(operations, (list, tuple)) or not 1 <= len(operations) <= 16:
            _fail("operations must be a non-empty bounded array")
        normalized_operations = []
        operation_ids: list[str] = []
        tool_names: list[str] = []
        for operation in operations:
            item = _exact(
                operation,
                "capability operation",
                ("operationId", "toolName", "inputSchemaDigest", "outputSchemaDigest"),
            )
            item["operationId"] = _string(
                item["operationId"], "operationId", pattern=_OPERATION_ID
            )
            item["toolName"] = _string(item["toolName"], "toolName", pattern=_TOOL_NAME)
            item["inputSchemaDigest"] = _sha256(
                item["inputSchemaDigest"], "inputSchemaDigest"
            )
            item["outputSchemaDigest"] = _sha256(
                item["outputSchemaDigest"], "outputSchemaDigest"
            )
            operation_ids.append(item["operationId"])
            tool_names.append(item["toolName"])
            normalized_operations.append(item)
        if len(set(operation_ids)) != len(operation_ids) or len(set(tool_names)) != len(
            tool_names
        ):
            _fail("capability operations must have unique identities")
        value["operations"] = normalized_operations

        approval = _exact(
            value["approvalPolicy"], "approvalPolicy", ("mode", "standingAllowed")
        )
        approval["mode"] = _enum(
            approval["mode"],
            "approvalPolicy.mode",
            {"NONE", "CURRENT_REQUEST_TARGET_GRANT", "EXACT_ONE_TIME_PROPOSAL"},
        )
        approval["standingAllowed"] = _boolean(
            approval["standingAllowed"], "approvalPolicy.standingAllowed"
        )
        if approval["standingAllowed"]:
            _fail("standing approval is reserved and rejected in v1")
        value["approvalPolicy"] = approval

        target = _exact(value["targetPolicy"], "targetPolicy", ("mode",))
        target["mode"] = _enum(
            target["mode"],
            "targetPolicy.mode",
            {
                "SESSION_WORKSPACE",
                "EXACT_PUBLIC_URL",
                "CONTROL_PLANE_RECORD",
                "FRESH_JOB_NAMESPACE",
            },
        )
        value["targetPolicy"] = target

        retry = _exact(value["retryPolicy"], "retryPolicy", ("mode", "onUncertain"))
        retry["mode"] = _enum(
            retry["mode"],
            "retryPolicy.mode",
            {"READ_ONLY", "IDEMPOTENT", "DEDUPE_KEY_REQUIRED"},
        )
        retry["onUncertain"] = _enum(
            retry["onUncertain"], "retryPolicy.onUncertain", {"STOP_AND_RECONCILE"}
        )
        value["retryPolicy"] = retry

        quota = _exact(
            value["quotaPolicy"],
            "quotaPolicy",
            ("maxCallsPerTurn", "maxInputBytes", "maxOutputBytes"),
        )
        quota["maxCallsPerTurn"] = _integer(
            quota["maxCallsPerTurn"],
            "quotaPolicy.maxCallsPerTurn",
            minimum=1,
            maximum=64,
        )
        quota["maxInputBytes"] = _integer(
            quota["maxInputBytes"],
            "quotaPolicy.maxInputBytes",
            maximum=64 * 1024 * 1024,
        )
        quota["maxOutputBytes"] = _integer(
            quota["maxOutputBytes"],
            "quotaPolicy.maxOutputBytes",
            maximum=64 * 1024 * 1024,
        )
        value["quotaPolicy"] = quota

        retention = _exact(
            value["retentionPolicy"], "retentionPolicy", ("class", "maxDays")
        )
        retention["class"] = _enum(
            retention["class"],
            "retentionPolicy.class",
            {"NONE", "WORKSPACE_LIFECYCLE", "CONTROL_RECORD", "JOB_RECEIPT"},
        )
        retention["maxDays"] = _integer(
            retention["maxDays"], "retentionPolicy.maxDays", maximum=365
        )
        value["retentionPolicy"] = retention

        deletion = _exact(
            value["deletionPolicy"], "deletionPolicy", ("authorityFence", "behavior")
        )
        deletion["authorityFence"] = _enum(
            deletion["authorityFence"], "deletionPolicy.authorityFence", {"REQUIRED"}
        )
        deletion["behavior"] = _enum(
            deletion["behavior"],
            "deletionPolicy.behavior",
            {
                "PURGE_WITH_WORKSPACE",
                "REVOKE_AND_PURGE",
                "CANCEL_AND_PURGE",
                "FENCE_CANCEL_PURGE",
            },
        )
        value["deletionPolicy"] = deletion
        object.__setattr__(self, "_data", _freeze(value))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CapabilityPackV1":
        return cls(value)

    @property
    def data(self) -> Mapping[str, Any]:
        return self._data

    def __getattr__(self, name: str) -> Any:
        for wire_name in self.FIELDS:
            if _camel_to_snake(wire_name) == name:
                return self._data[wire_name]
        raise AttributeError(name)

    def to_mapping(self) -> dict[str, Any]:
        return _thaw(self._data)


@dataclass(frozen=True, slots=True)
class CapabilityCatalogV1(ContractValue):
    SCHEMA = "personal-operator.capability-catalog.v1"
    FIELDS = ("schema", "releaseCommit", "catalogDigest", "packs")

    @classmethod
    def _validate_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result = cls._base(value)
        result["releaseCommit"] = _release_commit(result["releaseCommit"])
        result["catalogDigest"] = _sha256(result["catalogDigest"], "catalogDigest")
        packs = result["packs"]
        if not isinstance(packs, (list, tuple)) or not 1 <= len(packs) <= 64:
            _fail("catalog packs must be a non-empty bounded array")
        normalized = [
            CapabilityPackV1.from_mapping(pack).to_mapping() for pack in packs
        ]
        pack_ids = [pack["packId"] for pack in normalized]
        operation_ids = [
            operation["operationId"]
            for pack in normalized
            for operation in pack["operations"]
        ]
        tool_names = [
            operation["toolName"]
            for pack in normalized
            for operation in pack["operations"]
        ]
        if len(set(pack_ids)) != len(pack_ids):
            _fail("catalog pack IDs must be unique")
        if len(set(operation_ids)) != len(operation_ids):
            _fail("catalog operation IDs must be unique")
        if len(set(tool_names)) != len(tool_names):
            _fail("catalog tool names must be unique")
        if len(normalized) != len(FROZEN_CATALOG_PACKS_V1):
            _fail("catalog must contain the exact frozen v1 pack rows")
        for actual, expected in zip(normalized, FROZEN_CATALOG_PACKS_V1, strict=True):
            actual_metadata = {
                key: nested for key, nested in actual.items() if key != "operations"
            }
            expected_metadata = {
                key: nested for key, nested in expected.items() if key != "operations"
            }
            actual_operations = actual["operations"]
            expected_operation = expected["operations"][0]
            if (
                canonical_json_bytes(actual_metadata)
                != canonical_json_bytes(expected_metadata)
                or len(actual_operations) != 1
                or actual_operations[0]["operationId"]
                != expected_operation["operationId"]
                or actual_operations[0]["toolName"] != expected_operation["toolName"]
            ):
                _fail("catalog authority metadata differs from the frozen v1 matrix")
            operation_id = actual_operations[0]["operationId"]
            reviewed_digests = FROZEN_OPERATION_SCHEMA_DIGESTS_V1.get(operation_id)
            if reviewed_digests is None or any(
                actual_operations[0][field] != reviewed_digests[field]
                for field in ("inputSchemaDigest", "outputSchemaDigest")
            ):
                _fail("catalog schema digests differ from the reviewed v1 matrix")
        result["packs"] = normalized
        digest_input = {
            key: nested for key, nested in result.items() if key != "catalogDigest"
        }
        expected = hashlib.sha256(canonical_json_bytes(digest_input)).hexdigest()
        if result["catalogDigest"] != expected:
            _fail(
                "catalogDigest does not bind the canonical catalog with that field omitted"
            )
        return result


@dataclass(frozen=True, slots=True)
class CapabilityInstallationV1(ContractValue):
    SCHEMA = "personal-operator.capability-installation.v1"
    FIELDS = (
        "schema",
        "userId",
        "packId",
        "catalogDigest",
        "state",
        "policyRevision",
        "connectionRefs",
        "killSwitch",
    )

    @classmethod
    def _validate_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result = cls._base(value)
        result["userId"] = _string(result["userId"], "userId", pattern=_USER_ID)
        result["packId"] = _string(result["packId"], "packId", pattern=_PACK_ID)
        result["catalogDigest"] = _sha256(result["catalogDigest"], "catalogDigest")
        result["state"] = _enum(
            result["state"], "state", {"ENABLED", "PAUSED", "REVOKED"}
        )
        result["policyRevision"] = _integer(
            result["policyRevision"], "policyRevision", minimum=1
        )
        result["connectionRefs"] = _string_list(
            result["connectionRefs"],
            "connectionRefs",
            pattern=_OPAQUE_ID,
            maximum_items=16,
            sorted_unique=True,
        )
        result["killSwitch"] = _boolean(result["killSwitch"], "killSwitch")
        if result["state"] == "ENABLED" and result["killSwitch"]:
            _fail("an enabled installation cannot have its kill switch set")
        return result


@dataclass(frozen=True, slots=True)
class TurnCapabilityGrantV1(ContractValue):
    SCHEMA = "personal-operator.turn-capability-grant.v1"
    FIELDS = (
        "schema",
        "sub",
        "sessionId",
        "runtimeArn",
        "runtimeQualifier",
        "invocationId",
        "releaseCommit",
        "catalogDigest",
        "allowedPackIds",
        "allowedOperationIds",
        "targetGrantHashes",
        "iat",
        "exp",
        "maxCalls",
        "nonce",
    )

    @classmethod
    def _validate_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result = cls._base(value)
        result["sub"] = _string(result["sub"], "sub", pattern=_USER_ID)
        for name in ("sessionId", "invocationId", "nonce"):
            result[name] = _string(result[name], name, pattern=_OPAQUE_ID)
        result["runtimeArn"] = _string(
            result["runtimeArn"], "runtimeArn", pattern=_RUNTIME_ARN, maximum=256
        )
        if _RUNTIME_ARN.fullmatch(result["runtimeArn"]) is None:
            _fail("runtimeArn must identify the frozen eu-west-1 runtime")
        result["releaseCommit"] = _release_commit(result["releaseCommit"])
        expected_qualifier = f"release_{result['releaseCommit']}"
        if result["runtimeQualifier"] != expected_qualifier:
            _fail("runtimeQualifier must bind the exact release commit")
        result["catalogDigest"] = _sha256(result["catalogDigest"], "catalogDigest")
        result["allowedPackIds"] = _string_list(
            result["allowedPackIds"],
            "allowedPackIds",
            pattern=_PACK_ID,
            sorted_unique=True,
        )
        result["allowedOperationIds"] = _string_list(
            result["allowedOperationIds"],
            "allowedOperationIds",
            pattern=_OPERATION_ID,
            sorted_unique=True,
        )
        if not result["allowedPackIds"] or not result["allowedOperationIds"]:
            _fail("turn grant must allow at least one pack and operation")
        result["targetGrantHashes"] = _string_list(
            result["targetGrantHashes"],
            "targetGrantHashes",
            pattern=_SHA256,
            sorted_unique=True,
        )
        result["iat"] = _integer(result["iat"], "iat")
        result["exp"] = _integer(result["exp"], "exp")
        if result["exp"] <= result["iat"] or result["exp"] - result["iat"] > 900:
            _fail("turn grant lifetime must be positive and at most 15 minutes")
        result["maxCalls"] = _integer(
            result["maxCalls"], "maxCalls", minimum=1, maximum=64
        )
        return result


@dataclass(frozen=True, slots=True)
class CapabilityCallV1(ContractValue):
    SCHEMA = "personal-operator.capability-call.v1"
    FIELDS = (
        "schema",
        "callId",
        "invocationId",
        "toolUseId",
        "catalogDigest",
        "operationId",
        "toolName",
        "arguments",
        "argsHash",
    )

    @classmethod
    def _validate_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result = cls._base(value)
        result["invocationId"] = _string(
            result["invocationId"], "invocationId", pattern=_OPAQUE_ID
        )
        result["toolUseId"] = _string(
            result["toolUseId"], "toolUseId", pattern=_OPAQUE_ID
        )
        result["catalogDigest"] = _sha256(result["catalogDigest"], "catalogDigest")
        result["operationId"], result["toolName"] = _validate_tool_identity(
            result["operationId"], result["toolName"]
        )
        result["arguments"] = _validate_tool_input(
            result["operationId"], result["arguments"]
        )
        result["argsHash"] = _sha256(result["argsHash"], "argsHash")
        if canonical_sha256(result["arguments"]) != result["argsHash"]:
            _fail("argsHash does not bind canonical arguments")
        expected_call_id = derive_call_id(
            result["invocationId"],
            result["toolUseId"],
            result["catalogDigest"],
            result["operationId"],
            result["toolName"],
            result["argsHash"],
        )
        if result["callId"] != expected_call_id:
            _fail(
                "callId does not bind catalog, operation, invocation, tool use, and arguments"
            )
        return result


@dataclass(frozen=True, slots=True)
class CapabilityResultV1(ContractValue):
    SCHEMA = "personal-operator.capability-result.v1"
    FIELDS = (
        "schema",
        "callId",
        "invocationId",
        "toolUseId",
        "catalogDigest",
        "operationId",
        "toolName",
        "argsHash",
        "status",
        "data",
        "provenanceRefs",
        "proposalRef",
        "receiptRef",
        "errorCode",
        "retryPolicy",
    )

    @classmethod
    def _validate_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result = cls._base(value)
        result["callId"] = _string(
            result["callId"],
            "callId",
            pattern=re.compile(r"call_[0-9a-f]{64}"),
            maximum=69,
        )
        result["invocationId"] = _string(
            result["invocationId"], "invocationId", pattern=_OPAQUE_ID
        )
        result["toolUseId"] = _string(
            result["toolUseId"], "toolUseId", pattern=_OPAQUE_ID
        )
        result["catalogDigest"] = _sha256(result["catalogDigest"], "catalogDigest")
        result["operationId"], result["toolName"] = _validate_tool_identity(
            result["operationId"], result["toolName"]
        )
        result["argsHash"] = _sha256(result["argsHash"], "argsHash")
        expected_call_id = derive_call_id(
            result["invocationId"],
            result["toolUseId"],
            result["catalogDigest"],
            result["operationId"],
            result["toolName"],
            result["argsHash"],
        )
        if result["callId"] != expected_call_id:
            _fail("result identity does not bind the referenced capability call")
        result["status"] = _enum(result["status"], "status", RESULT_STATUSES)
        result["data"] = _mapping(result["data"], "data")
        result["provenanceRefs"] = _string_list(
            result["provenanceRefs"],
            "provenanceRefs",
            maximum_items=32,
            sorted_unique=True,
        )
        result["proposalRef"] = _optional_string(result["proposalRef"], "proposalRef")
        result["receiptRef"] = _optional_string(result["receiptRef"], "receiptRef")
        result["errorCode"] = _optional_string(
            result["errorCode"], "errorCode", maximum=128
        )
        result["retryPolicy"] = _enum(
            result["retryPolicy"],
            "retryPolicy",
            {"NONE", "SAFE_RETRY", "RECONCILE_ONLY"},
        )
        required_retry = {
            "FAILED_RETRYABLE": "SAFE_RETRY",
            "UNCERTAIN": "RECONCILE_ONLY",
        }.get(result["status"], "NONE")
        if result["retryPolicy"] != required_retry:
            _fail("result retry policy is inconsistent with status")
        status = result["status"]
        proposal_operation = (
            _operation_approval_mode(result["operationId"]) == "EXACT_ONE_TIME_PROPOSAL"
        )
        if status == "SUCCEEDED":
            if proposal_operation:
                _fail("proposal operations cannot succeed before approval")
            result["data"] = _validate_tool_output(
                result["operationId"], result["data"]
            )
            if result["proposalRef"] is not None or result["errorCode"] is not None:
                _fail("successful result contains a forbidden proposal or error")
            read_operations = {
                "workspace.file.list",
                "workspace.file.read",
                "web.exact.read",
                "schedule.list",
                "compute.status",
            }
            mutation_operations = {
                "workspace.file.write",
                "workspace.file.delete",
                "compute.run",
            }
            if (
                result["operationId"] in read_operations
                and result["receiptRef"] is not None
            ):
                _fail("read, list, and status results cannot claim effect receipts")
            if (
                result["operationId"] in mutation_operations
                and result["provenanceRefs"]
            ):
                _fail("mutation results cannot claim read provenance")
        elif status == "PENDING_APPROVAL":
            if not proposal_operation:
                _fail("only proposal operations can await approval")
            result["data"] = _validate_tool_output(
                result["operationId"], result["data"]
            )
            if (
                result["proposalRef"] is None
                or result["proposalRef"] != result["data"]["proposalRef"]
                or result["argsHash"] != result["data"]["argsHash"]
                or result["receiptRef"] is not None
                or result["errorCode"] is not None
                or result["provenanceRefs"]
            ):
                _fail("pending approval result fields are inconsistent")
        else:
            if (
                result["data"]
                or result["provenanceRefs"]
                or result["proposalRef"] is not None
                or result["receiptRef"] is not None
                or result["errorCode"] is None
            ):
                _fail("non-success result fields are inconsistent")
            if (
                status == "UNCERTAIN"
                and _operation_metadata(result["operationId"])["retryMode"]
                == "READ_ONLY"
            ):
                _fail("read-only operations cannot return an uncertain effect outcome")
        return result

    def validate_against_call(self, call: CapabilityCallV1) -> "CapabilityResultV1":
        """Bind this result to one validated originating capability call."""

        if not isinstance(call, CapabilityCallV1):
            _fail("originating call must be a validated CapabilityCallV1")
        for identity_field in (
            "callId",
            "invocationId",
            "toolUseId",
            "catalogDigest",
            "operationId",
            "toolName",
            "argsHash",
        ):
            if self._data[identity_field] != call.data[identity_field]:
                _fail(f"result {identity_field} does not match the originating call")

        if self._data["status"] != "SUCCEEDED":
            return self

        operation_id = self._data["operationId"]
        arguments = call.data["arguments"]
        data = self._data["data"]
        if (
            operation_id
            in {
                "workspace.file.read",
                "workspace.file.write",
                "workspace.file.delete",
            }
            and data["path"] != arguments["path"]
        ):
            _fail("file result path does not match the originating call")
        if operation_id == "workspace.file.write" and data["bytes"] != len(
            arguments["content"].encode("utf-8")
        ):
            _fail("file write byte count does not match the originating content")
        if operation_id == "web.exact.read":
            requested_authority = urlsplit(arguments["url"]).netloc
            result_authority = urlsplit(data["canonicalUrl"]).netloc
            if result_authority != requested_authority:
                _fail("web result authority does not match the originating call")
        if operation_id == "compute.status" and data["jobId"] != arguments["jobId"]:
            _fail("compute status job does not match the originating call")
        return self


@dataclass(frozen=True, slots=True)
class TargetGrantV1(ContractValue):
    SCHEMA = "personal-operator.target-grant.v1"
    FIELDS = (
        "schema",
        "targetHash",
        "normalizedTarget",
        "method",
        "redirectPolicy",
        "expiresAt",
        "maxUses",
        "currentRequestId",
    )

    @classmethod
    def _validate_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result = cls._base(value)
        result["normalizedTarget"] = _public_https_url(result["normalizedTarget"])
        result["method"] = _enum(result["method"], "method", {"GET"})
        result["redirectPolicy"] = _enum(
            result["redirectPolicy"], "redirectPolicy", {"NO_REDIRECT", "SAME_HOST"}
        )
        result["expiresAt"] = _integer(result["expiresAt"], "expiresAt")
        result["maxUses"] = _integer(result["maxUses"], "maxUses", minimum=1, maximum=3)
        result["currentRequestId"] = _string(
            result["currentRequestId"], "currentRequestId", pattern=_OPAQUE_ID
        )
        expected = derive_target_hash(
            result["normalizedTarget"],
            result["method"],
            result["redirectPolicy"],
            result["expiresAt"],
            result["maxUses"],
            result["currentRequestId"],
        )
        if result["targetHash"] != expected:
            _fail("targetHash does not bind the exact current-request target")
        return result


@dataclass(frozen=True, slots=True)
class ActionProposalV1(ContractValue):
    SCHEMA = "personal-operator.action-proposal.v1"
    FIELDS = (
        "schema",
        "proposalId",
        "userId",
        "catalogDigest",
        "connectorSchemaDigest",
        "operationId",
        "toolName",
        "capabilityId",
        "resource",
        "connectionRef",
        "arguments",
        "argsHash",
        "revision",
        "originatingInvocationId",
        "approvalPolicy",
        "expiresAt",
    )

    @classmethod
    def _validate_common(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result = cls._base(value)
        result["proposalId"] = _string(
            result["proposalId"], "proposalId", pattern=_OPAQUE_ID
        )
        result["userId"] = _string(result["userId"], "userId", pattern=_USER_ID)
        result["capabilityId"] = _string(
            result["capabilityId"], "capabilityId", pattern=_OPERATION_ID
        )
        result["resource"] = _string(result["resource"], "resource", maximum=1024)
        result["connectionRef"] = (
            None
            if result["connectionRef"] is None
            else _string(result["connectionRef"], "connectionRef", pattern=_OPAQUE_ID)
        )
        result["arguments"] = _mapping(result["arguments"], "arguments")
        result["argsHash"] = _sha256(result["argsHash"], "argsHash")
        result["revision"] = _integer(result["revision"], "revision", minimum=1)
        result["originatingInvocationId"] = _string(
            result["originatingInvocationId"],
            "originatingInvocationId",
            pattern=_OPAQUE_ID,
        )
        result["approvalPolicy"] = _enum(
            result["approvalPolicy"], "approvalPolicy", {"EXACT_ONE_TIME"}
        )
        result["expiresAt"] = _integer(result["expiresAt"], "expiresAt")
        return result

    @classmethod
    def _validate_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result = cls._validate_common(value)
        result["catalogDigest"] = _sha256(result["catalogDigest"], "catalogDigest")
        if result["connectorSchemaDigest"] is not None:
            _fail("schedule proposals cannot carry a connector schema digest")
        result["operationId"], result["toolName"] = _validate_tool_identity(
            result["operationId"], result["toolName"]
        )
        if _operation_approval_mode(result["operationId"]) != "EXACT_ONE_TIME_PROPOSAL":
            _fail("action proposal must name an approval-gated operation")
        if result["capabilityId"] != result["operationId"]:
            _fail("capabilityId must equal operationId")
        result["arguments"] = _validate_tool_input(
            result["operationId"], result["arguments"]
        )
        if result["connectionRef"] is not None:
            _fail("schedule proposals cannot carry a connector reference")
        expected_resource = (
            "schedule:new"
            if result["operationId"] == "schedule.propose"
            else f"schedule:{result['arguments']['scheduleId']}"
        )
        if result["resource"] != expected_resource:
            _fail(
                "schedule proposal resource does not match its exact operation target"
            )
        if canonical_sha256(result["arguments"]) != result["argsHash"]:
            _fail("argsHash does not bind proposal arguments")
        return result

    @classmethod
    def from_connector_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        manifest: "ConnectorManifestV1",
        connection: "ConnectorConnectionV1",
        expected_resource: str,
        normalized_arguments: Mapping[str, Any],
    ) -> "ActionProposalV1":
        """Validate a proposal against trusted connector and WYSIWYIS context."""

        if not isinstance(manifest, ConnectorManifestV1):
            _fail("manifest must be a validated ConnectorManifestV1")
        if not isinstance(connection, ConnectorConnectionV1):
            _fail("connection must be a validated ConnectorConnectionV1")
        result = cls._validate_common(value)
        if result["catalogDigest"] is not None:
            _fail("connector proposals cannot carry a capability catalog digest")
        result["connectorSchemaDigest"] = _sha256(
            result["connectorSchemaDigest"], "connectorSchemaDigest"
        )
        if result["connectorSchemaDigest"] != manifest.schema_digest:
            _fail("connector proposal does not bind the trusted manifest")
        result["operationId"] = _string(
            result["operationId"], "operationId", pattern=_OPERATION_ID
        )
        if result["toolName"] is not None:
            _fail("connector proposals cannot name a local model tool")
        if result["capabilityId"] != result["operationId"]:
            _fail("capabilityId must equal operationId")
        operations = [
            operation
            for operation in manifest.operations
            if operation["operationId"] == result["operationId"]
        ]
        if len(operations) != 1 or operations[0]["mode"] != "PREPARE":
            _fail("connector proposal must name one PREPARE manifest operation")
        if (
            connection.user_id != result["userId"]
            or connection.connector_id != manifest.connector_id
            or connection.connection_ref != result["connectionRef"]
            or connection.state != "CONNECTED"
            or connection.deletion_fence
        ):
            _fail("connector proposal does not bind an active trusted connection")
        normalized_resource = _string(
            expected_resource, "expected_resource", maximum=1024
        )
        if result["resource"] != normalized_resource:
            _fail("connector proposal resource differs from trusted display context")
        trusted_arguments = _mapping(normalized_arguments, "normalized_arguments")
        if canonical_json_bytes(result["arguments"]) != canonical_json_bytes(
            trusted_arguments
        ):
            _fail("connector proposal arguments differ from trusted display context")
        if canonical_sha256(result["arguments"]) != result["argsHash"]:
            _fail("argsHash does not bind proposal arguments")

        instance = object.__new__(cls)
        object.__setattr__(instance, "_data", _freeze(result))
        object.__setattr__(instance, "_wire", canonical_json_bytes(result))
        return instance


@dataclass(frozen=True, slots=True)
class EffectReceiptV1(ContractValue):
    SCHEMA = "personal-operator.effect-receipt.v1"
    FIELDS = (
        "schema",
        "receiptId",
        "capabilityId",
        "resource",
        "argumentsHash",
        "providerEvidenceId",
        "providerEvidenceHash",
        "executedAt",
        "reconciledAt",
    )

    @classmethod
    def _validate_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result = cls._base(value)
        result["receiptId"] = _string(
            result["receiptId"], "receiptId", pattern=_OPAQUE_ID
        )
        result["capabilityId"] = _string(
            result["capabilityId"], "capabilityId", pattern=_OPERATION_ID
        )
        result["resource"] = _string(result["resource"], "resource", maximum=1024)
        result["argumentsHash"] = _sha256(result["argumentsHash"], "argumentsHash")
        result["providerEvidenceId"] = _string(
            result["providerEvidenceId"], "providerEvidenceId", pattern=_OPAQUE_ID
        )
        result["providerEvidenceHash"] = _sha256(
            result["providerEvidenceHash"], "providerEvidenceHash"
        )
        result["executedAt"] = _integer(result["executedAt"], "executedAt")
        result["reconciledAt"] = (
            None
            if result["reconciledAt"] is None
            else _integer(result["reconciledAt"], "reconciledAt")
        )
        if (
            result["reconciledAt"] is not None
            and result["reconciledAt"] < result["executedAt"]
        ):
            _fail("reconciledAt cannot precede executedAt")
        return result


@dataclass(frozen=True, slots=True)
class ScheduleSpecV1(ContractValue):
    SCHEMA = "personal-operator.schedule-spec.v1"
    FIELDS = (
        "schema",
        "scheduleId",
        "userId",
        "taskType",
        "definition",
        "definitionHash",
        "revision",
        "state",
        "timezone",
        "nextRunAt",
    )

    @classmethod
    def _validate_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result = cls._base(value)
        result["scheduleId"] = _string(
            result["scheduleId"], "scheduleId", pattern=_OPAQUE_ID
        )
        result["userId"] = _string(result["userId"], "userId", pattern=_USER_ID)
        result["taskType"] = _enum(
            result["taskType"], "taskType", {"REMINDER", "READ_ONLY_AGENT_TURN"}
        )
        result["definition"] = _validate_schedule_definition(
            result["taskType"], result["definition"]
        )
        result["definitionHash"] = _sha256(result["definitionHash"], "definitionHash")
        if canonical_sha256(result["definition"]) != result["definitionHash"]:
            _fail("definitionHash does not bind the schedule definition")
        result["revision"] = _integer(result["revision"], "revision", minimum=1)
        result["state"] = _enum(
            result["state"], "state", {"ENABLED", "PAUSED", "CANCELLED"}
        )
        result["timezone"] = _string(
            result["timezone"], "timezone", pattern=_TIMEZONE, maximum=64
        )
        if result["timezone"] != result["definition"]["timezone"]:
            _fail("schedule timezone must equal the exact definition timezone")
        result["nextRunAt"] = (
            None
            if result["nextRunAt"] is None
            else _integer(result["nextRunAt"], "nextRunAt")
        )
        if result["state"] == "ENABLED":
            if result["nextRunAt"] != result["definition"]["runAt"]:
                _fail("enabled schedule next run must equal its hashed definition")
        elif result["nextRunAt"] is not None:
            _fail("a disabled schedule cannot have a next run")
        return result


@dataclass(frozen=True, slots=True)
class ScheduleOccurrenceV1(ContractValue):
    SCHEMA = "personal-operator.schedule-occurrence.v1"
    FIELDS = (
        "schema",
        "occurrenceId",
        "scheduleId",
        "generation",
        "occurrenceTime",
        "status",
    )

    @classmethod
    def _validate_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result = cls._base(value)
        result["scheduleId"] = _string(
            result["scheduleId"], "scheduleId", pattern=_OPAQUE_ID
        )
        result["generation"] = _integer(result["generation"], "generation", minimum=1)
        result["occurrenceTime"] = _integer(result["occurrenceTime"], "occurrenceTime")
        expected = derive_occurrence_id(
            result["scheduleId"], result["generation"], result["occurrenceTime"]
        )
        if result["occurrenceId"] != expected:
            _fail("occurrenceId does not bind schedule generation and time")
        result["status"] = _enum(
            result["status"],
            "status",
            {"QUEUED", "CLAIMED", "COMPLETED", "STALE", "FAILED"},
        )
        return result


def _command(value: Any) -> dict[str, Any]:
    result = _exact(value, "command", ("mode", "value"))
    result["mode"] = _enum(result["mode"], "command.mode", {"SCRIPT", "ARGV"})
    if result["mode"] == "SCRIPT":
        result["value"] = _string(result["value"], "command.value", maximum=32 * 1024)
    else:
        result["value"] = _string_list(
            result["value"], "command.value", maximum_items=64
        )
        if not result["value"]:
            _fail("ARGV command cannot be empty")
    return result


def _operation_metadata(operation_id: Any) -> Mapping[str, Any]:
    normalized = _string(operation_id, "operationId", pattern=_OPERATION_ID)
    metadata = _OPERATION_METADATA_V1.get(normalized)
    if metadata is None:
        _fail("operationId is not in the frozen v1 catalog")
    return metadata


def _operation_approval_mode(operation_id: Any) -> str:
    return str(_operation_metadata(operation_id)["approvalMode"])


def _validate_tool_identity(operation_id: Any, tool_name: Any) -> tuple[str, str]:
    normalized_operation = _string(operation_id, "operationId", pattern=_OPERATION_ID)
    normalized_tool = _string(tool_name, "toolName", pattern=_TOOL_NAME)
    metadata = _operation_metadata(normalized_operation)
    if normalized_tool != metadata["toolName"]:
        _fail("toolName does not match the frozen operation identity")
    return normalized_operation, normalized_tool


def _bounded_text(value: Any, label: str, *, maximum: int, minimum: int = 0) -> str:
    if (
        not isinstance(value, str)
        or len(value) < minimum
        or len(value) > maximum
        or "\x00" in value
    ):
        _fail(f"{label} is invalid")
    return value


def _validate_schedule_definition(task_type: Any, value: Any) -> dict[str, Any]:
    normalized_type = _enum(task_type, "taskType", {"REMINDER", "READ_ONLY_AGENT_TURN"})
    content_field = "message" if normalized_type == "REMINDER" else "prompt"
    result = _exact(value, "schedule definition", (content_field, "runAt", "timezone"))
    result[content_field] = _bounded_text(
        result[content_field], f"definition.{content_field}", minimum=1, maximum=4096
    )
    result["runAt"] = _integer(result["runAt"], "definition.runAt")
    result["timezone"] = _string(
        result["timezone"], "definition.timezone", pattern=_TIMEZONE, maximum=64
    )
    return result


def _validate_tool_input(operation_id: str, value: Any) -> dict[str, Any]:
    _operation_metadata(operation_id)
    if operation_id in {"workspace.file.list", "schedule.list"}:
        return _exact(value, "arguments", ())
    if operation_id in {"workspace.file.read", "workspace.file.delete"}:
        result = _exact(value, "arguments", ("path",))
        result["path"] = _safe_path(result["path"])
        return result
    if operation_id == "workspace.file.write":
        result = _exact(value, "arguments", ("path", "content"))
        result["path"] = _safe_path(result["path"])
        result["content"] = _bounded_text(
            result["content"], "arguments.content", maximum=262144
        )
        return result
    if operation_id == "web.exact.read":
        result = _exact(value, "arguments", ("url",))
        result["url"] = _public_https_url(result["url"])
        return result
    if operation_id == "schedule.propose":
        result = _exact(value, "arguments", ("taskType", "definition"))
        result["taskType"] = _enum(
            result["taskType"],
            "arguments.taskType",
            {"REMINDER", "READ_ONLY_AGENT_TURN"},
        )
        result["definition"] = _validate_schedule_definition(
            result["taskType"], result["definition"]
        )
        return result
    if operation_id == "schedule.cancel.propose":
        result = _exact(value, "arguments", ("scheduleId",))
        result["scheduleId"] = _string(
            result["scheduleId"], "arguments.scheduleId", pattern=_OPAQUE_ID
        )
        return result
    if operation_id == "compute.run":
        result = _exact(
            value, "arguments", ("command", "inputPaths", "network", "resourceProfile")
        )
        result["command"] = _command(result["command"])
        result["inputPaths"] = _string_list(
            result["inputPaths"],
            "arguments.inputPaths",
            maximum_items=128,
            sorted_unique=True,
        )
        result["inputPaths"] = [
            _safe_path(path, "arguments.inputPaths") for path in result["inputPaths"]
        ]
        result["network"] = _enum(result["network"], "arguments.network", {"NONE"})
        result["resourceProfile"] = _enum(
            result["resourceProfile"], "arguments.resourceProfile", {"SMALL"}
        )
        return result
    if operation_id == "compute.status":
        result = _exact(value, "arguments", ("jobId",))
        result["jobId"] = _string(
            result["jobId"], "arguments.jobId", pattern=_OPAQUE_ID
        )
        return result
    _fail("operation input validator is unavailable")


def _file_listing_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)) or len(value) > 1000:
        _fail("files must be a bounded array")
    result: list[dict[str, Any]] = []
    paths: list[str] = []
    for raw in value:
        item = _exact(raw, "file listing", ("path", "size"))
        item["path"] = _safe_path(item["path"])
        item["size"] = _integer(item["size"], "file size", maximum=262144)
        result.append(item)
        paths.append(item["path"])
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        _fail("file listing paths must be sorted and unique")
    return result


def _proposal_output(value: Any) -> dict[str, Any]:
    result = _exact(value, "proposal output", ("proposalRef", "argsHash", "expiresAt"))
    result["proposalRef"] = _string(
        result["proposalRef"], "proposalRef", pattern=_OPAQUE_ID
    )
    result["argsHash"] = _sha256(result["argsHash"], "argsHash")
    result["expiresAt"] = _integer(result["expiresAt"], "expiresAt")
    return result


def _validate_tool_output(operation_id: str, value: Any) -> dict[str, Any]:
    _operation_metadata(operation_id)
    if operation_id == "workspace.file.list":
        result = _exact(value, "result data", ("files",))
        result["files"] = _file_listing_records(result["files"])
        return result
    if operation_id == "workspace.file.read":
        result = _exact(value, "result data", ("path", "content"))
        result["path"] = _safe_path(result["path"])
        result["content"] = _bounded_text(
            result["content"], "result content", maximum=262144
        )
        return result
    if operation_id == "workspace.file.write":
        result = _exact(value, "result data", ("path", "bytes"))
        result["path"] = _safe_path(result["path"])
        result["bytes"] = _integer(result["bytes"], "bytes", maximum=262144)
        return result
    if operation_id == "workspace.file.delete":
        result = _exact(value, "result data", ("path", "deleted"))
        result["path"] = _safe_path(result["path"])
        if result["deleted"] is not True:
            _fail("delete output must confirm deletion")
        return result
    if operation_id == "web.exact.read":
        result = _exact(
            value,
            "result data",
            ("canonicalUrl", "contentDigest", "retrievedAt", "sourceRef", "text"),
        )
        result["canonicalUrl"] = _public_https_url(result["canonicalUrl"])
        result["contentDigest"] = _sha256(result["contentDigest"], "contentDigest")
        result["retrievedAt"] = _integer(result["retrievedAt"], "retrievedAt")
        result["sourceRef"] = _bounded_text(
            result["sourceRef"], "sourceRef", maximum=512
        )
        result["text"] = _bounded_text(result["text"], "text", maximum=32768)
        return result
    if operation_id == "schedule.list":
        result = _exact(value, "result data", ("schedules",))
        schedules = result["schedules"]
        if not isinstance(schedules, (list, tuple)) or len(schedules) > 256:
            _fail("schedules must be a bounded array")
        normalized = []
        identities = []
        for raw in schedules:
            item = _exact(
                raw,
                "schedule listing",
                ("scheduleId", "taskType", "state", "nextRunAt"),
            )
            item["scheduleId"] = _string(
                item["scheduleId"], "scheduleId", pattern=_OPAQUE_ID
            )
            item["taskType"] = _enum(
                item["taskType"], "taskType", {"REMINDER", "READ_ONLY_AGENT_TURN"}
            )
            item["state"] = _enum(
                item["state"], "state", {"ENABLED", "PAUSED", "CANCELLED"}
            )
            item["nextRunAt"] = (
                None
                if item["nextRunAt"] is None
                else _integer(item["nextRunAt"], "nextRunAt")
            )
            if item["state"] != "ENABLED" and item["nextRunAt"] is not None:
                _fail("disabled schedule listing cannot have a next run")
            identities.append(item["scheduleId"])
            normalized.append(item)
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            _fail("schedule listings must be sorted and unique")
        result["schedules"] = normalized
        return result
    if operation_id in {"schedule.propose", "schedule.cancel.propose"}:
        return _proposal_output(value)
    if operation_id == "compute.run":
        result = _exact(value, "result data", ("jobId", "status"))
        result["jobId"] = _string(result["jobId"], "jobId", pattern=_OPAQUE_ID)
        result["status"] = _enum(result["status"], "status", {"QUEUED"})
        return result
    if operation_id == "compute.status":
        result = _exact(value, "result data", ("jobId", "outputs", "status"))
        result["jobId"] = _string(result["jobId"], "jobId", pattern=_OPAQUE_ID)
        result["outputs"] = _file_records(result["outputs"], "outputs")
        result["status"] = _enum(
            result["status"],
            "status",
            {"QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "DENIED", "TIMED_OUT"},
        )
        if result["status"] != "SUCCEEDED" and result["outputs"]:
            _fail("unfinished or failed compute output must be empty")
        return result
    _fail("operation output validator is unavailable")


@dataclass(frozen=True, slots=True)
class ComputeJobSpecV1(ContractValue):
    SCHEMA = "personal-operator.compute-job-spec.v1"
    FIELDS = (
        "schema",
        "jobId",
        "userId",
        "imageDigest",
        "command",
        "inputFiles",
        "resourceProfile",
        "deadline",
        "network",
    )

    @classmethod
    def _validate_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result = cls._base(value)
        result["jobId"] = _string(result["jobId"], "jobId", pattern=_OPAQUE_ID)
        result["userId"] = _string(result["userId"], "userId", pattern=_USER_ID)
        result["imageDigest"] = _string(
            result["imageDigest"], "imageDigest", pattern=_IMAGE_DIGEST, maximum=71
        )
        result["command"] = _command(result["command"])
        result["inputFiles"] = _file_records(result["inputFiles"], "inputFiles")
        result["resourceProfile"] = _enum(
            result["resourceProfile"], "resourceProfile", {"SMALL"}
        )
        result["deadline"] = _integer(result["deadline"], "deadline")
        result["network"] = _enum(result["network"], "network", {"NONE"})
        return result


@dataclass(frozen=True, slots=True)
class ComputeReceiptV1(ContractValue):
    SCHEMA = "personal-operator.compute-receipt.v1"
    FIELDS = (
        "schema",
        "jobId",
        "status",
        "imageDigest",
        "inputDigest",
        "outputFiles",
        "startedAt",
        "completedAt",
        "errorCode",
    )

    @classmethod
    def _validate_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result = cls._base(value)
        result["jobId"] = _string(result["jobId"], "jobId", pattern=_OPAQUE_ID)
        result["status"] = _enum(
            result["status"], "status", {"SUCCEEDED", "FAILED", "DENIED", "TIMED_OUT"}
        )
        result["imageDigest"] = _string(
            result["imageDigest"], "imageDigest", pattern=_IMAGE_DIGEST, maximum=71
        )
        result["inputDigest"] = _sha256(result["inputDigest"], "inputDigest")
        result["outputFiles"] = _file_records(result["outputFiles"], "outputFiles")
        result["startedAt"] = _integer(result["startedAt"], "startedAt")
        result["completedAt"] = _integer(result["completedAt"], "completedAt")
        if result["completedAt"] < result["startedAt"]:
            _fail("completedAt cannot precede startedAt")
        result["errorCode"] = _optional_string(
            result["errorCode"], "errorCode", maximum=128
        )
        if (result["status"] == "SUCCEEDED") != (result["errorCode"] is None):
            _fail("compute status and errorCode are inconsistent")
        if result["status"] != "SUCCEEDED" and result["outputFiles"]:
            _fail("non-success compute receipts cannot publish output files")
        return result


@dataclass(frozen=True, slots=True)
class ConnectorManifestV1(ContractValue):
    SCHEMA = "personal-operator.connector-manifest.v1"
    FIELDS = (
        "schema",
        "connectorId",
        "version",
        "schemaDigest",
        "operations",
        "credentialBoundary",
    )

    @classmethod
    def _validate_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result = cls._base(value)
        result["connectorId"] = _string(
            result["connectorId"], "connectorId", pattern=_PACK_ID
        )
        result["version"] = _string(
            result["version"], "version", pattern=_VERSION, maximum=32
        )
        result["schemaDigest"] = _sha256(result["schemaDigest"], "schemaDigest")
        operations = result["operations"]
        if not isinstance(operations, (list, tuple)) or not 1 <= len(operations) <= 32:
            _fail("connector operations must be a non-empty bounded array")
        normalized = []
        ids = []
        for operation in operations:
            item = _exact(
                operation,
                "connector operation",
                ("operationId", "mode", "inputSchemaDigest", "outputSchemaDigest"),
            )
            item["operationId"] = _string(
                item["operationId"], "operationId", pattern=_OPERATION_ID
            )
            item["mode"] = _enum(item["mode"], "mode", {"READ", "PREPARE"})
            item["inputSchemaDigest"] = _sha256(
                item["inputSchemaDigest"], "inputSchemaDigest"
            )
            item["outputSchemaDigest"] = _sha256(
                item["outputSchemaDigest"], "outputSchemaDigest"
            )
            ids.append(item["operationId"])
            normalized.append(item)
        if ids != sorted(ids) or len(set(ids)) != len(ids):
            _fail("connector operations must be sorted and unique")
        result["operations"] = normalized
        result["credentialBoundary"] = _enum(
            result["credentialBoundary"], "credentialBoundary", {"TRUSTED_ADAPTER"}
        )
        digest_input = {
            key: nested for key, nested in result.items() if key != "schemaDigest"
        }
        if result["schemaDigest"] != canonical_sha256(digest_input):
            _fail("schemaDigest does not bind the canonical connector manifest")
        return result


@dataclass(frozen=True, slots=True)
class ConnectorConnectionV1(ContractValue):
    SCHEMA = "personal-operator.connector-connection.v1"
    FIELDS = (
        "schema",
        "userId",
        "connectorId",
        "connectionRef",
        "state",
        "consentRevision",
        "deletionFence",
    )

    @classmethod
    def _validate_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result = cls._base(value)
        result["userId"] = _string(result["userId"], "userId", pattern=_USER_ID)
        result["connectorId"] = _string(
            result["connectorId"], "connectorId", pattern=_PACK_ID
        )
        result["connectionRef"] = _string(
            result["connectionRef"], "connectionRef", pattern=_OPAQUE_ID
        )
        result["state"] = _enum(
            result["state"], "state", {"CONNECTED", "PAUSED", "REVOKED", "DRIFTED"}
        )
        result["consentRevision"] = _integer(
            result["consentRevision"], "consentRevision", minimum=1
        )
        result["deletionFence"] = _boolean(result["deletionFence"], "deletionFence")
        if result["state"] == "CONNECTED" and result["deletionFence"]:
            _fail("a connected connector cannot have a deletion fence")
        return result


@dataclass(frozen=True, slots=True)
class PortableStateManifestV2(ContractValue):
    SCHEMA = "personal-operator.portable-state-manifest.v2"
    FIELDS = (
        "schema",
        "generation",
        "bundleHash",
        "objects",
        "excludedClasses",
        "createdAt",
    )

    @classmethod
    def _validate_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result = cls._base(value)
        result["generation"] = _string(
            result["generation"], "generation", pattern=_OPAQUE_ID
        )
        result["bundleHash"] = _sha256(result["bundleHash"], "bundleHash")
        objects = result["objects"]
        if not isinstance(objects, (list, tuple)) or len(objects) > 2048:
            _fail("portable objects must be a bounded array")
        normalized = []
        paths = []
        for entry in objects:
            item = _exact(entry, "portable object", ("path", "type", "size", "sha256"))
            item["path"] = _safe_path(item["path"])
            item["type"] = _enum(
                item["type"],
                "type",
                {
                    "FILE",
                    "MEMORY",
                    "SCHEDULE",
                    "INSTALLATION",
                    "CONNECTOR",
                    "COMPUTE_RECEIPT",
                    "EFFECT_RECEIPT",
                },
            )
            item["size"] = _integer(item["size"], "size", maximum=64 * 1024 * 1024)
            item["sha256"] = _sha256(item["sha256"], "sha256")
            paths.append(item["path"])
            normalized.append(item)
        if paths != sorted(paths) or len(set(paths)) != len(paths):
            _fail("portable object paths must be sorted and unique")
        result["objects"] = normalized
        result["excludedClasses"] = _string_list(
            result["excludedClasses"],
            "excludedClasses",
            maximum_items=32,
            sorted_unique=True,
        )
        required_exclusions = {"CREDENTIALS", "GRANTS", "PENDING_EFFECTS"}
        if not required_exclusions.issubset(result["excludedClasses"]):
            _fail("portable state must explicitly exclude authority-bearing classes")
        result["createdAt"] = _integer(result["createdAt"], "createdAt")
        return result


@dataclass(frozen=True, slots=True)
class ImportPlanV1(ContractValue):
    SCHEMA = "personal-operator.import-plan.v1"
    FIELDS = (
        "schema",
        "planId",
        "userId",
        "bundleHash",
        "baseGeneration",
        "objectCount",
        "totalBytes",
        "schedulesDisabled",
        "connectorsDisconnected",
        "effectsReplayable",
    )

    @classmethod
    def _validate_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result = cls._base(value)
        result["planId"] = _string(result["planId"], "planId", pattern=_OPAQUE_ID)
        result["userId"] = _string(result["userId"], "userId", pattern=_USER_ID)
        result["bundleHash"] = _sha256(result["bundleHash"], "bundleHash")
        result["baseGeneration"] = _string(
            result["baseGeneration"], "baseGeneration", pattern=_OPAQUE_ID
        )
        result["objectCount"] = _integer(
            result["objectCount"], "objectCount", maximum=2048
        )
        result["totalBytes"] = _integer(
            result["totalBytes"], "totalBytes", maximum=256 * 1024 * 1024
        )
        for name in (
            "schedulesDisabled",
            "connectorsDisconnected",
            "effectsReplayable",
        ):
            result[name] = _boolean(result[name], name)
        if not result["schedulesDisabled"] or not result["connectorsDisconnected"]:
            _fail("import must disable schedules and disconnect connectors")
        if result["effectsReplayable"]:
            _fail("imported past effects must never be replayable")
        return result


@dataclass(frozen=True, slots=True)
class ImportReceiptV1(ContractValue):
    SCHEMA = "personal-operator.import-receipt.v1"
    FIELDS = (
        "schema",
        "planId",
        "userId",
        "bundleHash",
        "state",
        "activatedGeneration",
        "importedAt",
    )

    @classmethod
    def _validate_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result = cls._base(value)
        result["planId"] = _string(result["planId"], "planId", pattern=_OPAQUE_ID)
        result["userId"] = _string(result["userId"], "userId", pattern=_USER_ID)
        result["bundleHash"] = _sha256(result["bundleHash"], "bundleHash")
        result["state"] = _enum(result["state"], "state", {"ACTIVATED", "FAILED"})
        result["activatedGeneration"] = (
            None
            if result["activatedGeneration"] is None
            else _string(
                result["activatedGeneration"], "activatedGeneration", pattern=_OPAQUE_ID
            )
        )
        if (result["state"] == "ACTIVATED") != (
            result["activatedGeneration"] is not None
        ):
            _fail("import state and activated generation are inconsistent")
        result["importedAt"] = _integer(result["importedAt"], "importedAt")
        return result


CONTRACT_TYPES = MappingProxyType(
    {
        contract_type.SCHEMA: contract_type
        for contract_type in (
            CapabilityCatalogV1,
            CapabilityInstallationV1,
            TurnCapabilityGrantV1,
            CapabilityCallV1,
            CapabilityResultV1,
            TargetGrantV1,
            ActionProposalV1,
            EffectReceiptV1,
            ScheduleSpecV1,
            ScheduleOccurrenceV1,
            ComputeJobSpecV1,
            ComputeReceiptV1,
            ConnectorManifestV1,
            ConnectorConnectionV1,
            PortableStateManifestV2,
            ImportPlanV1,
            ImportReceiptV1,
        )
    }
)


def parse_canonical_json(
    raw: bytes,
    expected_schema: str | type[ContractValue],
    *,
    limits: ContractLimits = DEFAULT_LIMITS,
) -> ContractValue:
    """Parse one exact contract, rejecting alternate JSON representations."""

    if not isinstance(limits, ContractLimits):
        _fail("limits must be ContractLimits")
    if not isinstance(raw, bytes) or not raw or len(raw) > limits.max_bytes:
        _fail("canonical contract bytes are absent, untrusted, or oversized")
    if isinstance(expected_schema, str):
        contract_type = CONTRACT_TYPES.get(expected_schema)
    elif isinstance(expected_schema, type) and issubclass(
        expected_schema, ContractValue
    ):
        contract_type = expected_schema
        expected_schema = contract_type.SCHEMA
    else:
        contract_type = None
    if contract_type is None:
        _fail("expected schema is not a frozen contract type")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _fail("canonical JSON contains a duplicate object key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        _fail(f"canonical JSON contains non-finite number {value}")

    def reject_float(value: str) -> None:
        _fail(f"canonical JSON contains unsupported float {value}")

    try:
        text = raw.decode("utf-8", errors="strict")
        parsed = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
            parse_float=reject_float,
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
        raise ContractValidationError("contract is not strict UTF-8 JSON") from error
    _validate_json_tree(parsed, limits)
    if not isinstance(parsed, Mapping) or parsed.get("schema") != expected_schema:
        _fail("contract schema does not match the expected schema")
    if canonical_json_bytes(parsed, limits=limits) != raw:
        _fail("contract bytes are not canonical JSON")
    return contract_type.from_mapping(parsed)


__all__ = [
    "APPROVAL_POLICIES",
    "ActionProposalV1",
    "CapabilityCallV1",
    "CapabilityCatalogV1",
    "CapabilityInstallationV1",
    "CapabilityPackV1",
    "CapabilityResultV1",
    "ComputeJobSpecV1",
    "ComputeReceiptV1",
    "ConnectorConnectionV1",
    "ConnectorManifestV1",
    "CONTRACT_TYPES",
    "ContractLimits",
    "ContractValidationError",
    "EffectReceiptV1",
    "FROZEN_CATALOG_PACKS_V1",
    "FROZEN_OPERATION_SCHEMA_DIGESTS_V1",
    "ImportPlanV1",
    "ImportReceiptV1",
    "PortableStateManifestV2",
    "RESULT_STATUSES",
    "RISK_CLASSES",
    "ScheduleOccurrenceV1",
    "ScheduleSpecV1",
    "TargetGrantV1",
    "TurnCapabilityGrantV1",
    "canonical_json_bytes",
    "canonical_sha256",
    "derive_call_id",
    "derive_occurrence_id",
    "derive_target_hash",
    "parse_canonical_json",
]
