# Task 10 report: curated connector SDK and browser authority boundary

Strict RED->GREEN->REFACTOR, single writer, offline, public/synthetic only.
No AWS/network/MCP/browser calls: every provider surface is an injected fake.

## External gates OPEN

The external browser provider gate and the real (non-synthetic) connector
provider gate remain OPEN. This task ships the curated SDK, the synthetic MCP
adapter, and the disabled-by-default browser boundary only. No authenticated
browser and no real connector is enabled anywhere: production composition still
wires `adapters={}` and `stacks/browser_stack.py` synthesizes no browser IAM.

## What was built

- Build-time schema lock (`lambda/connectors/manifest.py`) mirroring
  `capabilities.catalog`: closed non-symlink schema inventory, per-op
  input/output digests over exact canonical artifact bytes (+ single LF),
  `credentialBoundary` hard-pinned `TRUSTED_ADAPTER`, sorted/unique operations,
  a FROZEN release-owned curated registry (`synthetic.notes`), and
  `manifest_digest()` as a single equality drift anchor. No dynamic/ClawHub
  discovery.
- Synthetic offline MCP server fake (`lambda/connectors/synthetic.py`) holding a
  private fake URL + OAuth token + server config so tests can prove they never
  escape.
- `SyntheticMcpConnectorAdapter` (`lambda/connectors/mcp.py`) implementing the
  Task 3 `ConnectorAdapter` surface: fetches the live tool list and refuses any
  drift from the locked manifest; rejects unknown/undeclared tools before server
  contact; validates args/results against the locked closed schemas; caps output
  bytes; enforces an explicit no-retry timeout; READ returns only schema-closed
  observations; PREPARE builds an `ActionProposalV1` (no effect) and persists it;
  dispatch reloads the persisted record and re-binds it through the trusted
  validator so an in-memory proposal can never dispatch. Private URL/token/config
  never appear in any return value.
- Disabled-by-default `BrowserGateway` (`lambda/browser/gateway.py`): refuses to
  enable without an explicit flag + profile ref + exact target allowlist; every
  target is an exact normalized public HTTPS URL bound to the allowlist;
  observations are redacted; credential injection is trusted-side only and
  refuses a user-supplied key; submit/upload/send/delete each return an
  `ActionProposalV1` routed through the Task 3 kernel; NO direct-effect method
  exists. It reuses the trusted MCP adapter for the kernel dispatch discipline.
- `ConnectorPlaneRegistry` seam in `lambda/capabilities/gateway.py`:
  disabled-by-default, keyed by connector operationId, refuses any op colliding
  with the frozen model-facing catalog (runtime-unawareness), and requires the
  Task 3 effect surface. Production composition stays `adapters={}`.
- `stacks/browser_stack.py`: a SEPARATE stack owning all browser IAM in its own
  role, disabled by default (no `CfnBrowserCustom`, no browser actions). The
  browser role is never the runtime execution role. `agentcore_stack.py` keeps
  its runtime-owned-browser forbid and now documents the browser_stack tie.

## Invariants mapped to tests

- manifest/tool-list drift -> `test_mcp.py::test_tool_list_drift_refuses_to_act`
- unknown tool -> `test_mcp.py::test_unknown_operation_rejected_before_server_contact`,
  `test_read_rejects_a_prepare_only_operation_and_vice_versa`
- schema mutation -> `test_mcp.py::test_input_violating_locked_schema_is_rejected`,
  `test_output_violating_locked_schema_is_rejected_as_malicious`
- arbitrary endpoint -> `test_mcp.py::test_caller_supplied_endpoint_is_ignored_or_rejected`
- credential leakage -> `test_mcp.py::test_private_url_token_config_never_leak_into_any_return_value`
- runtime unawareness -> `test_mcp.py::test_prepare_proposal_carries_no_toolname_or_catalog_digest`,
  `test_gateway_seam.py::test_connector_ops_are_not_in_the_frozen_model_facing_catalog`,
  `test_registry_refuses_connector_op_colliding_with_model_catalog`
- malicious result -> `test_mcp.py::test_output_violating_locked_schema_is_rejected_as_malicious`
- oversize -> `test_mcp.py::test_oversize_output_is_rejected`
- timeout -> `test_mcp.py::test_timeout_raises_without_retry_and_leaves_no_effect`
- connection deletion/fence -> `test_mcp.py::test_non_connected_connection_blocks_prepare_and_dispatch`,
  `test_revoke_drops_connection_without_touching_provider_creds`
- effect-through-kernel -> `test_mcp.py::test_prepare_performs_no_effect_and_dispatch_reloads_persisted_state`
- browser disabled-by-default -> `browser/test_gateway.py::test_disabled_gateway_refuses_every_operation`,
  `test_enable_requires_profile_ref_and_nonempty_allowlist`,
  `test_gateway_is_disabled_by_default_constant`
- browser no-direct-effect -> `browser/test_gateway.py::test_no_direct_effect_method_exists_on_the_gateway`,
  `test_action_methods_return_proposal_and_never_act_directly`,
  `test_effect_only_occurs_via_kernel_dispatch_of_persisted_record`
- browser exact-target -> `browser/test_gateway.py::test_target_not_in_allowlist_is_rejected`
- browser observation redaction -> `browser/test_gateway.py::test_observations_strip_credentials_cookies_tokens_pii`,
  `test_credential_injection_is_trusted_side_only_never_returns_key`,
  `test_user_supplied_key_is_refused`
- browser IAM isolation (CDK synth) -> `tests/test_browser_stack.py` (all)
- agentcore browser-forbid preserved -> `tests/test_browser_stack.py::test_agentcore_source_still_forbids_a_runtime_owned_browser`,
  `test_agentcore_execution_role_has_zero_browser_actions`
- composition stays disabled -> `test_gateway_seam.py::test_production_composition_wires_connector_registry_empty`,
  `test_connector_registry_is_empty_by_default`

## New files

- `lambda/connectors/{__init__,manifest,mcp,synthetic}.py`
- `lambda/connectors/schemas/*.json` (4 curated connector schemas)
- `lambda/connectors/test_{manifest,mcp,gateway_seam}.py`
- `lambda/browser/{__init__,gateway}.py`
- `lambda/browser/schemas/*.json` (4 browser action/observe schemas)
- `lambda/browser/test_gateway.py`
- `stacks/browser_stack.py`
- `tests/test_browser_stack.py`

## Modified files

- `lambda/capabilities/gateway.py` (added `ConnectorPlaneRegistry` seam)
- `stacks/agentcore_stack.py` (documentation tie to browser_stack; no browser IAM)

## Deviations

- The connector schema validation is a dependency-free closed-schema walker in
  `mcp.py` (jsonschema is not vendored); it covers exactly the frozen schema
  subset the catalog compiler already validates.
- PREPARE validates caller args against the locked input schema and never
  contacts the untrusted server (prepare performs no effect), so the READ/PREPARE
  separation is exact.

Focused suite `lambda/connectors lambda/browser tests/test_browser_stack.py`:
48 passed. Regression `lambda/capabilities lambda/actions tests/test_capability_stack.py`:
305 passed. `synthetic.notes` manifest digest:
`772b0cb54da9ce10d8d0cbff4123b05f6033fb6ad5dae7e40e04bce426091ab0`.
