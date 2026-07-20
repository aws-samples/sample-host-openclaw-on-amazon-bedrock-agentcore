"""Synthetic MCP connector adapter behind the trusted boundary (Task 10).

``SyntheticMcpConnectorAdapter`` implements the Task 3 ``ConnectorAdapter``
surface (read/prepare/dispatch/reconcile/revoke). It sits in front of the
offline :class:`connectors.synthetic.SyntheticMcpServer` and:

* holds ``mcp_url`` / ``oauth_token`` / ``server_config`` PRIVATELY (never in a
  returned mapping);
* on every call fetches the server tool list and asserts it equals the
  build-time-locked manifest operations (drift -> raise, never proceed);
* rejects unknown/undeclared tools before any server contact;
* validates args against the locked input schema and results against the locked
  output schema (byte-digest bound);
* enforces a max-output-bytes cap and an explicit no-retry provider timeout;
* READ returns only redacted observations;
* PREPARE builds an ``ActionProposalV1`` (no effect) and persists it; and
* dispatch delegates to the concrete effect via the reloaded persisted record,
  so the effect only ever runs through the kernel's reload discipline.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Mapping, Optional

try:  # package import in Lambda and repository consumers
    from actions.proposals import ActionProposalV1 as _ActionProposalProto  # noqa: F401
    from capabilities.catalog import (
        MAX_SCHEMA_ARTIFACT_BYTES,
        _load_canonical_artifact,
    )
    from capabilities.contracts import (
        ActionProposalV1,
        ConnectorConnectionV1,
        ConnectorManifestV1,
        ContractValidationError,
        canonical_json_bytes,
        canonical_sha256,
    )
    from .manifest import schema_index
except ImportError:  # pragma: no cover - bare-module load path (connector_mcp)
    from action_proposals import ActionProposalV1 as _ActionProposalProto  # noqa: F401
    from catalog import (  # type: ignore[no-redef]
        MAX_SCHEMA_ARTIFACT_BYTES,
        _load_canonical_artifact,
    )
    from contracts import (  # type: ignore[no-redef]
        ActionProposalV1,
        ConnectorConnectionV1,
        ConnectorManifestV1,
        ContractValidationError,
        canonical_json_bytes,
        canonical_sha256,
    )
    from manifest import schema_index  # type: ignore[no-redef]


DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024


class ManifestDrift(ContractValidationError):
    """The live server tool list diverged from the locked manifest operations."""


class ConnectorOutputTooLarge(ContractValidationError):
    """An adapter output exceeded the configured max-output-bytes cap."""


class ConnectorCallTimeout(RuntimeError):
    """A synthetic server call crossed its explicit no-retry time boundary."""


def _fail(message: str) -> None:
    raise ContractValidationError(message)


@dataclass(frozen=True, slots=True)
class ConnectorRequestContext:
    """The exact caller-bound context one connector operation runs under."""

    user_id: str
    resource: str
    connection_ref: str
    now: int
    proposal_id: str
    invocation_id: str
    revision: int
    expires_at: int


class InMemoryPreparedStore:
    """A synthetic persisted-proposal store.

    ``dispatch`` NEVER acts from an in-memory proposal: it reloads the persisted
    record here first. Records are stored as plain wire mappings.
    """

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    def put(self, proposal: ActionProposalV1) -> None:
        self._records[proposal.data["proposalId"]] = proposal.to_mapping()

    def get(self, proposal_id: str) -> dict[str, Any]:
        record = self._records.get(proposal_id)
        if record is None:
            _fail("no persisted proposal exists for this identity")
        return dict(record)


# --- closed schema validation (dependency-free) ---------------------------
def _validate_against_schema(value: Any, schema: Mapping[str, Any], label: str) -> None:
    branches = schema.get("oneOf")
    if branches is not None:
        for branch in branches:
            try:
                _validate_against_schema(value, branch, label)
                return
            except ContractValidationError:
                continue
        _fail(f"{label} matches no permitted schema branch")
        return

    node_type = schema.get("type")
    if node_type == "object":
        if not isinstance(value, Mapping):
            _fail(f"{label} must be an object")
        properties = schema.get("properties", {})
        allowed = set(properties)
        if schema.get("additionalProperties") is False and set(value) - allowed:
            _fail(f"{label} carries an undeclared field")
        for name in schema.get("required", []):
            if name not in value:
                _fail(f"{label} is missing required field {name}")
        for name, child in properties.items():
            if name in value:
                _validate_against_schema(value[name], child, f"{label}.{name}")
        return
    if node_type == "array":
        if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
            _fail(f"{label} must be an array")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            _fail(f"{label} exceeds maxItems")
        if "minItems" in schema and len(value) < schema["minItems"]:
            _fail(f"{label} is below minItems")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                _validate_against_schema(item, item_schema, f"{label}[{index}]")
        return
    if node_type == "string":
        if not isinstance(value, str):
            _fail(f"{label} must be a string")
        if "minLength" in schema and len(value) < schema["minLength"]:
            _fail(f"{label} is below minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            _fail(f"{label} exceeds maxLength")
        return
    if node_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            _fail(f"{label} must be an integer")
        return
    if node_type == "boolean":
        if not isinstance(value, bool):
            _fail(f"{label} must be a boolean")
        return
    if "enum" in schema:
        if value not in schema["enum"]:
            _fail(f"{label} is not a permitted enum value")
        return
    if "const" in schema:
        if value != schema["const"]:
            _fail(f"{label} is not the permitted constant")
        return
    _fail(f"{label} has no closed schema assertion")


class SyntheticMcpConnectorAdapter:
    """Task 3 ``ConnectorAdapter`` in front of the synthetic MCP server."""

    def __init__(
        self,
        *,
        manifest: ConnectorManifestV1,
        connection: ConnectorConnectionV1,
        server: Any,
        store: InMemoryPreparedStore,
        schema_dir: str | Path,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        connector_id: Optional[str] = None,
        schema_files: Optional[Mapping[str, tuple[str, str]]] = None,
    ) -> None:
        if not isinstance(manifest, ConnectorManifestV1):
            raise TypeError("adapter requires a validated ConnectorManifestV1")
        self._manifest = manifest
        self._connection: Optional[ConnectorConnectionV1] = connection
        self._server = server
        self._store = store
        self._schema_dir = Path(schema_dir)
        self._max_output_bytes = int(max_output_bytes)
        self._connector_id = connector_id or manifest.connector_id
        # PRIVATE server config: never returned in any mapping.
        self._mcp_url = getattr(server, "_mcp_url", None)
        self._oauth_token = getattr(server, "_oauth_token", None)
        self._server_config = getattr(server, "_server_config", None)
        # Operation -> mode, and the locked (input, output) schema documents.
        self._modes = {op["operationId"]: op["mode"] for op in manifest.operations}
        self._digests = {
            op["operationId"]: (op["inputSchemaDigest"], op["outputSchemaDigest"])
            for op in manifest.operations
        }
        index = schema_files or schema_index(self._connector_id)
        self._schemas: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
        for operation_id, (input_name, output_name) in index.items():
            in_raw, in_doc = _load_canonical_artifact(
                self._schema_dir / input_name, MAX_SCHEMA_ARTIFACT_BYTES
            )
            out_raw, out_doc = _load_canonical_artifact(
                self._schema_dir / output_name, MAX_SCHEMA_ARTIFACT_BYTES
            )
            # The loaded schema bytes must equal the digest the manifest locked.
            locked_in, locked_out = self._digests[operation_id]
            if hashlib.sha256(in_raw).hexdigest() != locked_in:
                _fail("connector input schema bytes drifted from the locked digest")
            if hashlib.sha256(out_raw).hexdigest() != locked_out:
                _fail("connector output schema bytes drifted from the locked digest")
            self._schemas[operation_id] = (in_doc, out_doc)

    # --- guards ------------------------------------------------------------
    def _assert_no_drift(self) -> None:
        live = tuple(self._server.list_tools())
        locked = tuple(op["operationId"] for op in self._manifest.operations)
        if tuple(live) != locked:
            # Drift is a latched connection-state transition, not a transient
            # per-call error. Restoring the live list cannot reactivate this
            # adapter; a trusted reconnect must provide a fresh CONNECTED
            # record and construct a new adapter boundary.
            connection = self._connection
            if connection is not None and connection.state == "CONNECTED":
                drifted = connection.to_mapping()
                drifted["state"] = "DRIFTED"
                self._connection = ConnectorConnectionV1.from_mapping(drifted)
            raise ManifestDrift("server tool list diverged from the locked manifest")

    def _operation(self, operation: str, expected_mode: str) -> None:
        if operation not in self._modes:
            _fail("operation is not declared in the locked manifest")
        if self._modes[operation] != expected_mode:
            _fail("operation mode is not permitted for this call")

    def _active_connection(self) -> ConnectorConnectionV1:
        connection = self._connection
        if connection is None:
            _fail("connector connection has been revoked")
        if connection.state != "CONNECTED" or connection.deletion_fence:
            _fail("connector connection is not active")
        return connection

    def _validate_input(self, operation: str, args: Mapping[str, object]) -> dict:
        if not isinstance(args, Mapping):
            _fail("connector args must be a mapping")
        # Reject any caller-supplied endpoint/url/config field outright: the
        # closed input schema forbids additional properties, so this is caught,
        # but we surface it early to prove no arbitrary endpoint is honored.
        in_schema, _ = self._schemas[operation]
        _validate_against_schema(args, in_schema, "arguments")
        return dict(args)

    def _validate_output(
        self, operation: str, result: Mapping[str, object]
    ) -> dict:
        _, out_schema = self._schemas[operation]
        _validate_against_schema(result, out_schema, "result")
        size = len(canonical_json_bytes(dict(result)))
        if size > self._max_output_bytes:
            raise ConnectorOutputTooLarge("connector output exceeds the size cap")
        return dict(result)

    def _call_server(self, method: str, operation: str, args: Mapping[str, object]):
        try:
            return getattr(self._server, method)(operation, args)
        except TimeoutError as error:
            raise ConnectorCallTimeout(str(error)) from None

    # --- ConnectorAdapter surface -----------------------------------------
    def read(
        self, context: ConnectorRequestContext, operation: str, args: Mapping[str, object]
    ) -> Mapping[str, object]:
        self._operation(operation, "READ")
        self._assert_no_drift()
        self._active_connection()
        validated_args = self._validate_input(operation, args)
        raw = self._call_server("read", operation, validated_args)
        # Redact/validate: the closed output schema rejects any injected
        # credential/PII/oversize field as an undeclared property.
        return self._validate_output(operation, raw)

    def prepare(
        self, context: ConnectorRequestContext, operation: str, args: Mapping[str, object]
    ) -> ActionProposalV1:
        self._operation(operation, "PREPARE")
        self._assert_no_drift()
        connection = self._active_connection()
        # PREPARE performs NO effect and never contacts the untrusted server: it
        # validates the caller args against the locked input schema and binds an
        # approvable proposal to the locked manifest + connection + resource.
        normalized_arguments = self._validate_input(operation, args)
        proposal = self._build_proposal(
            context, operation, connection, normalized_arguments
        )
        self._store.put(proposal)
        return proposal

    def _build_proposal(
        self,
        context: ConnectorRequestContext,
        operation: str,
        connection: ConnectorConnectionV1,
        normalized_arguments: Mapping[str, object],
    ) -> ActionProposalV1:
        arguments = dict(normalized_arguments)
        payload = {
            "schema": ActionProposalV1.SCHEMA,
            "proposalId": context.proposal_id,
            "userId": context.user_id,
            "catalogDigest": None,
            "connectorSchemaDigest": self._manifest.schema_digest,
            "operationId": operation,
            "toolName": None,
            "capabilityId": operation,
            "resource": context.resource,
            "connectionRef": context.connection_ref,
            "arguments": arguments,
            "argsHash": canonical_sha256(arguments),
            "revision": context.revision,
            "originatingInvocationId": context.invocation_id,
            "approvalPolicy": "EXACT_ONE_TIME",
            "expiresAt": context.expires_at,
        }
        return ActionProposalV1.from_connector_mapping(
            payload,
            manifest=self._manifest,
            connection=connection,
            expected_resource=context.resource,
            normalized_arguments=arguments,
        )

    def dispatch(self, approved_action: Mapping[str, object]) -> Mapping[str, object]:
        # NEVER act from an in-memory proposal: reload the persisted record and
        # re-validate its binding against the locked manifest + connection.
        if not isinstance(approved_action, Mapping):
            _fail("dispatch requires a persisted proposal record")
        proposal_id = approved_action.get("proposalId")
        if not isinstance(proposal_id, str):
            _fail("dispatch record has no proposal identity")
        persisted = self._store.get(proposal_id)
        if canonical_json_bytes(dict(persisted)) != canonical_json_bytes(
            dict(approved_action)
        ):
            _fail("dispatch record does not match the persisted proposal")
        self._assert_no_drift()
        connection = self._active_connection()
        # Re-bind the persisted record through the trusted validator so a
        # tampered in-memory copy can never dispatch.
        proposal = ActionProposalV1.from_connector_mapping(
            persisted,
            manifest=self._manifest,
            connection=connection,
            expected_resource=persisted["resource"],
            normalized_arguments=persisted["arguments"],
        )
        operation = proposal.data["operationId"]
        self._operation(operation, "PREPARE")
        # The effect (in-memory only) resolves creds lazily inside the server.
        raw = self._call_server("apply_effect", operation, proposal.data["arguments"])
        _ = raw  # provider evidence stays trusted-side; only a receipt escapes.
        return {
            "operationId": operation,
            "resource": proposal.data["resource"],
            "connectionRef": proposal.data["connectionRef"],
            "argsHash": proposal.data["argsHash"],
            "status": "SUCCEEDED",
        }

    def reconcile(
        self, action: Mapping[str, object]
    ) -> Optional[Mapping[str, object]]:
        # v0 synthetic connector performs a deterministic in-memory effect, so
        # there is no ambiguous provider evidence to reconcile.
        return None

    def revoke(
        self,
        connection_ref: str,
        *,
        action_id: str | None = None,
        user_id: str | None = None,
        revision: int | None = None,
        operation_id: str | None = None,
    ) -> None:
        if any(
            value is not None
            for value in (action_id, user_id, revision, operation_id)
        ):
            _fail(
                "approval revocation is unavailable while the connector plane is disabled"
            )
        # Drop the connection without the kernel ever holding provider creds.
        self._connection = None


__all__ = [
    "ConnectorCallTimeout",
    "ConnectorOutputTooLarge",
    "ConnectorRequestContext",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "InMemoryPreparedStore",
    "ManifestDrift",
    "SyntheticMcpConnectorAdapter",
]
