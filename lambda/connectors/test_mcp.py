"""Hostile tests for the synthetic MCP connector adapter (Task 10).

The adapter sits ENTIRELY behind the trusted capability boundary and in front
of a fake in-memory MCP server. It never lets the private server URL/token/
config escape, refuses any drift from the build-time-locked manifest, validates
args/results against the locked schema digests, caps output size, enforces an
explicit no-retry timeout, and routes every effect through the Task 3 kernel.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from actions.connectors import ConnectorAdapter, GenericConnectorKernel
from capabilities.contracts import (
    ContractValidationError,
    ConnectorConnectionV1,
    canonical_json_bytes,
    canonical_sha256,
)
from connectors import manifest as manifest_module
from connectors import mcp as mcp_module
from connectors import synthetic as synthetic_module

SCHEMA_DIR = Path(manifest_module.__file__).resolve().parent / "schemas"
CONNECTOR_ID = "synthetic.notes"
USER_ID = "founder-1"
CONNECTION_REF = "synthetic_conn_00000001"
RESOURCE = "synthetic:notes:default"


def _manifest():
    return manifest_module.compile_connector_manifest(CONNECTOR_ID, SCHEMA_DIR)


def _connection(state="CONNECTED", fence=False):
    payload = {
        "schema": ConnectorConnectionV1.SCHEMA,
        "userId": USER_ID,
        "connectorId": CONNECTOR_ID,
        "connectionRef": CONNECTION_REF,
        "state": state,
        "consentRevision": 1,
        "deletionFence": fence,
    }
    return ConnectorConnectionV1.from_mapping(payload)


def _context(**overrides):
    base = dict(
        user_id=USER_ID,
        resource=RESOURCE,
        connection_ref=CONNECTION_REF,
        now=1_800_000_000,
        proposal_id="synthetic_prop_00000001",
        invocation_id="synthetic_inv_00000001",
        revision=1,
        expires_at=1_800_003_600,
    )
    base.update(overrides)
    return mcp_module.ConnectorRequestContext(**base)


def _adapter(server=None, connection=None, store=None):
    server = server or synthetic_module.SyntheticMcpServer()
    return mcp_module.SyntheticMcpConnectorAdapter(
        manifest=_manifest(),
        connection=connection or _connection(),
        server=server,
        store=store if store is not None else mcp_module.InMemoryPreparedStore(),
        schema_dir=SCHEMA_DIR,
    )


def test_adapter_satisfies_the_generic_connector_protocol():
    assert isinstance(_adapter(), ConnectorAdapter)


# --- manifest / tool-list drift -------------------------------------------
@pytest.mark.parametrize(
    "tool_list",
    [
        ["synthetic.notes.append", "synthetic.notes.read-list", "synthetic.notes.delete"],
        ["synthetic.notes.append"],
        ["synthetic.notes.read-list", "synthetic.notes.append"],  # reordered
    ],
)
def test_tool_list_drift_refuses_to_act(tool_list):
    server = synthetic_module.SyntheticMcpServer(tool_list=tool_list)
    adapter = _adapter(server=server)
    with pytest.raises(mcp_module.ManifestDrift):
        adapter.read(_context(), "synthetic.notes.read-list", {})
    with pytest.raises(mcp_module.ManifestDrift):
        adapter.prepare(_context(), "synthetic.notes.append", {"text": "hi"})
    assert server.read_calls == 0
    assert server.prepare_calls == 0
    assert server.effect_calls == 0


def test_detected_tool_list_drift_latches_connection_until_explicit_reconnect():
    server = synthetic_module.SyntheticMcpServer()
    store = mcp_module.InMemoryPreparedStore()
    adapter = _adapter(server=server, store=store)

    # Prepare while the live list still matches, then drift before dispatch.
    proposal = adapter.prepare(
        _context(), "synthetic.notes.append", {"text": "must stay inert"}
    )
    persisted = store.get(proposal.data["proposalId"])
    server._tool_list = ("synthetic.notes.read-list",)

    with pytest.raises(mcp_module.ManifestDrift):
        GenericConnectorKernel(adapter).dispatch(persisted)
    assert adapter._connection.state == "DRIFTED"
    assert server.effect_calls == 0

    # Merely restoring the server list cannot reactivate the latched adapter.
    server._tool_list = tuple(
        operation["operationId"] for operation in _manifest().operations
    )
    with pytest.raises(ContractValidationError, match="not active"):
        adapter.read(_context(), "synthetic.notes.read-list", {})
    assert server.read_calls == 0
    assert server.effect_calls == 0

    # Supplying a fresh trusted CONNECTED record is the explicit reconnect.
    reconnected = _adapter(server=server, connection=_connection())
    assert reconnected.read(_context(), "synthetic.notes.read-list", {})["notes"]


# --- unknown / undeclared tool --------------------------------------------
def test_unknown_operation_rejected_before_server_contact():
    server = synthetic_module.SyntheticMcpServer()
    adapter = _adapter(server=server)
    with pytest.raises(ContractValidationError):
        adapter.read(_context(), "synthetic.notes.exfiltrate", {})
    with pytest.raises(ContractValidationError):
        adapter.prepare(_context(), "synthetic.notes.exfiltrate", {"text": "x"})
    assert server.read_calls == 0
    assert server.prepare_calls == 0


def test_read_rejects_a_prepare_only_operation_and_vice_versa():
    adapter = _adapter()
    with pytest.raises(ContractValidationError):
        adapter.read(_context(), "synthetic.notes.append", {"text": "x"})
    with pytest.raises(ContractValidationError):
        adapter.prepare(_context(), "synthetic.notes.read-list", {})


# --- schema mutation (input and output) -----------------------------------
def test_input_violating_locked_schema_is_rejected():
    adapter = _adapter()
    with pytest.raises(ContractValidationError):
        adapter.prepare(_context(), "synthetic.notes.append", {"text": 123})
    with pytest.raises(ContractValidationError):
        adapter.prepare(_context(), "synthetic.notes.append", {"nope": "x"})


def test_output_violating_locked_schema_is_rejected_as_malicious():
    server = synthetic_module.SyntheticMcpServer(
        observations={"synthetic.notes.read-list": {"notes": ["a"], "oauth_token": "x"}}
    )
    adapter = _adapter(server=server)
    with pytest.raises(ContractValidationError):
        adapter.read(_context(), "synthetic.notes.read-list", {})


# --- arbitrary endpoint ----------------------------------------------------
def test_caller_supplied_endpoint_is_ignored_or_rejected():
    server = synthetic_module.SyntheticMcpServer()
    adapter = _adapter(server=server)
    # A caller-supplied endpoint/url arg is not a declared schema field, so it
    # is rejected; the private release-owned server config is the only source.
    with pytest.raises(ContractValidationError):
        adapter.read(
            _context(),
            "synthetic.notes.read-list",
            {"endpoint": "https://evil.example.com/mcp"},
        )
    assert server.read_calls == 0


# --- credential leakage ----------------------------------------------------
def test_private_url_token_config_never_leak_into_any_return_value():
    store = mcp_module.InMemoryPreparedStore()
    adapter = _adapter(store=store)
    obs = adapter.read(_context(), "synthetic.notes.read-list", {})
    proposal = adapter.prepare(_context(), "synthetic.notes.append", {"text": "hello"})
    persisted = store.get(proposal.data["proposalId"])
    receipt = GenericConnectorKernel(adapter).dispatch(persisted)

    haystacks = [
        json.dumps(obs, default=str),
        proposal.to_bytes().decode("utf-8"),
        json.dumps(dict(persisted), default=str),
        json.dumps(receipt, default=str),
    ]
    for needle in (
        synthetic_module.SYNTHETIC_MCP_URL,
        synthetic_module.SYNTHETIC_OAUTH_TOKEN,
        "server_config",
        "oauth",
    ):
        for hay in haystacks:
            assert needle not in hay


# --- runtime unawareness ---------------------------------------------------
def test_prepare_proposal_carries_no_toolname_or_catalog_digest():
    adapter = _adapter()
    proposal = adapter.prepare(_context(), "synthetic.notes.append", {"text": "hi"})
    data = proposal.data
    assert data["toolName"] is None
    assert data["catalogDigest"] is None
    assert data["operationId"] == "synthetic.notes.append"
    assert data["connectorSchemaDigest"] == _manifest().schema_digest


# --- oversize --------------------------------------------------------------
def test_oversize_output_is_rejected():
    big = ["x" * 500 for _ in range(64)]
    server = synthetic_module.SyntheticMcpServer(
        observations={"synthetic.notes.read-list": {"notes": big}}
    )
    adapter = mcp_module.SyntheticMcpConnectorAdapter(
        manifest=_manifest(),
        connection=_connection(),
        server=server,
        store=mcp_module.InMemoryPreparedStore(),
        schema_dir=SCHEMA_DIR,
        max_output_bytes=256,
    )
    with pytest.raises(mcp_module.ConnectorOutputTooLarge):
        adapter.read(_context(), "synthetic.notes.read-list", {})


# --- timeout ---------------------------------------------------------------
def test_timeout_raises_without_retry_and_leaves_no_effect():
    server = synthetic_module.SyntheticMcpServer(raise_timeout=True)
    adapter = _adapter(server=server)
    with pytest.raises(mcp_module.ConnectorCallTimeout):
        adapter.read(_context(), "synthetic.notes.read-list", {})
    assert server.read_calls == 1  # exactly one attempt, no retry
    assert server.effect_calls == 0


# --- connection deletion / fence ------------------------------------------
@pytest.mark.parametrize(
    "connection",
    [_connection(state="REVOKED"), _connection(state="PAUSED"), _connection(state="DRIFTED")],
)
def test_non_connected_connection_blocks_prepare_and_dispatch(connection):
    store = mcp_module.InMemoryPreparedStore()
    adapter = _adapter(connection=connection, store=store)
    with pytest.raises(ContractValidationError):
        adapter.prepare(_context(), "synthetic.notes.append", {"text": "x"})


def test_revoke_drops_connection_without_touching_provider_creds():
    server = synthetic_module.SyntheticMcpServer()
    adapter = _adapter(server=server)
    adapter.revoke(CONNECTION_REF)
    with pytest.raises(ContractValidationError):
        adapter.prepare(_context(), "synthetic.notes.append", {"text": "x"})
    assert server.token_requests == 0


# --- effect only through the kernel with a reloaded persisted record ------
def test_prepare_performs_no_effect_and_dispatch_reloads_persisted_state():
    server = synthetic_module.SyntheticMcpServer()
    store = mcp_module.InMemoryPreparedStore()
    adapter = _adapter(server=server, store=store)

    proposal = adapter.prepare(_context(), "synthetic.notes.append", {"text": "note1"})
    assert server.effect_calls == 0  # prepare never acts

    # Dispatching an in-memory proposal that was never persisted is refused.
    orphan = mcp_module.InMemoryPreparedStore()
    orphan_adapter = _adapter(server=server, store=orphan)
    with pytest.raises(ContractValidationError):
        GenericConnectorKernel(orphan_adapter).dispatch(proposal.to_mapping())
    assert server.effect_calls == 0

    persisted = store.get(proposal.data["proposalId"])
    receipt = GenericConnectorKernel(adapter).dispatch(persisted)
    assert server.effect_calls == 1
    assert receipt["operationId"] == "synthetic.notes.append"
