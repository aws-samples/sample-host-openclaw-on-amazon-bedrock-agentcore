"""Hostile tests for the disabled-by-default trusted Browser Gateway (Task 10).

The gateway is OFF unless explicitly enabled with a profile ref AND an exact
release-owned target allowlist. It never performs a browser effect directly:
submit/upload/send/delete return an ActionProposalV1 that MUST be dispatched
through the Task 3 kernel. Observations are redacted; credential injection is
trusted-side only and never returns or accepts a user-supplied key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from actions.connectors import ConnectorAdapter, GenericConnectorKernel
from capabilities.contracts import ContractValidationError, ConnectorConnectionV1
from browser import gateway as browser_module
from connectors import manifest as manifest_module
from connectors import mcp as mcp_module

BROWSER_SCHEMA_DIR = Path(browser_module.__file__).resolve().parent / "schemas"
CONNECTOR_ID = "browser.gateway"
USER_ID = "founder-1"
CONNECTION_REF = "browser_conn_00000001"
PROFILE_REF = "browser_profile_00000001"
ALLOWED_TARGET = "https://tasks.example.com/inbox"
RESOURCE = "browser:tasks.example.com:inbox"


def _connection(state="CONNECTED", fence=False):
    return ConnectorConnectionV1.from_mapping(
        {
            "schema": ConnectorConnectionV1.SCHEMA,
            "userId": USER_ID,
            "connectorId": CONNECTOR_ID,
            "connectionRef": CONNECTION_REF,
            "state": state,
            "consentRevision": 1,
            "deletionFence": fence,
        }
    )


def _profiles(creds=None):
    return browser_module.TrustedProfileVault(
        {PROFILE_REF: creds or {"cookie": "SECRET_COOKIE", "token": "SECRET_TOKEN"}}
    )


def _enabled_gateway(session=None, store=None, connection=None):
    return browser_module.BrowserGateway(
        enabled=True,
        profile_ref=PROFILE_REF,
        target_allowlist=(ALLOWED_TARGET,),
        session=session or browser_module.FakeBrowserSession(),
        profiles=_profiles(),
        connection=connection or _connection(),
        store=store if store is not None else mcp_module.InMemoryPreparedStore(),
        schema_dir=BROWSER_SCHEMA_DIR,
    )


def _context(**overrides):
    base = dict(
        user_id=USER_ID,
        resource=RESOURCE,
        connection_ref=CONNECTION_REF,
        now=1_800_000_000,
        proposal_id="browser_prop_00000001",
        invocation_id="browser_inv_00000001",
        revision=1,
        expires_at=1_800_003_600,
    )
    base.update(overrides)
    return mcp_module.ConnectorRequestContext(**base)


# --- disabled by default ---------------------------------------------------
def test_gateway_is_disabled_by_default_constant():
    assert browser_module.BROWSER_ENABLED_BY_DEFAULT is False


def test_disabled_gateway_refuses_every_operation():
    gw = browser_module.BrowserGateway(schema_dir=BROWSER_SCHEMA_DIR)
    assert gw.enabled is False
    with pytest.raises(browser_module.BrowserDisabled):
        gw.observe(_context(), ALLOWED_TARGET)
    for action in ("submit", "upload", "send", "delete"):
        with pytest.raises(browser_module.BrowserDisabled):
            getattr(gw, action)(_context(), ALLOWED_TARGET, ["field"])


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(enabled=True, profile_ref=None, target_allowlist=(ALLOWED_TARGET,)),
        dict(enabled=True, profile_ref=PROFILE_REF, target_allowlist=()),
    ],
)
def test_enable_requires_profile_ref_and_nonempty_allowlist(kwargs):
    with pytest.raises(ValueError):
        browser_module.BrowserGateway(
            session=browser_module.FakeBrowserSession(),
            profiles=_profiles(),
            connection=_connection(),
            store=mcp_module.InMemoryPreparedStore(),
            schema_dir=BROWSER_SCHEMA_DIR,
            **kwargs,
        )


# --- no direct effect ------------------------------------------------------
def test_no_direct_effect_method_exists_on_the_gateway():
    gw = _enabled_gateway()
    for forbidden in ("click", "type", "execute", "perform", "commit", "act"):
        assert not hasattr(gw, forbidden)


def test_action_methods_return_proposal_and_never_act_directly():
    session = browser_module.FakeBrowserSession()
    gw = _enabled_gateway(session=session)
    for action in ("submit", "upload", "send", "delete"):
        proposal = getattr(gw, action)(_context(), ALLOWED_TARGET, ["field-a"])
        assert proposal.data["toolName"] is None
        assert proposal.data["catalogDigest"] is None
    assert session.effect_calls == 0  # nothing acted at proposal time


def test_effect_only_occurs_via_kernel_dispatch_of_persisted_record():
    session = browser_module.FakeBrowserSession()
    store = mcp_module.InMemoryPreparedStore()
    gw = _enabled_gateway(session=session, store=store)

    proposal = gw.submit(_context(), ALLOWED_TARGET, ["subject", "body"])
    assert session.effect_calls == 0

    adapter = gw.action_adapter()
    assert isinstance(adapter, ConnectorAdapter)

    # In-memory proposal that was never persisted cannot dispatch: an adapter
    # backed by an empty store has no reloaded record to act on.
    orphan_store = mcp_module.InMemoryPreparedStore()
    orphan_gw = _enabled_gateway(session=session, store=orphan_store)
    with pytest.raises(ContractValidationError):
        GenericConnectorKernel(orphan_gw.action_adapter()).dispatch(
            proposal.to_mapping()
        )
    assert session.effect_calls == 0

    persisted = store.get(proposal.data["proposalId"])
    receipt = GenericConnectorKernel(adapter).dispatch(persisted)
    assert session.effect_calls == 1
    assert receipt["operationId"].startswith("browser.")


# --- exact target ----------------------------------------------------------
@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example.com/inbox",  # host substitution
        "https://tasks.example.com/inbox/../admin",  # path alias
        "http://tasks.example.com/inbox",  # scheme downgrade
        "https://tasks.example.com/other",  # not in allowlist
    ],
)
def test_target_not_in_allowlist_is_rejected(target):
    gw = _enabled_gateway()
    with pytest.raises((ContractValidationError, browser_module.BrowserTargetDenied)):
        gw.observe(_context(), target)
    with pytest.raises((ContractValidationError, browser_module.BrowserTargetDenied)):
        gw.submit(_context(), target, ["field"])


# --- observation redaction + trusted-side creds ---------------------------
def test_observations_strip_credentials_cookies_tokens_pii():
    session = browser_module.FakeBrowserSession(
        observations=[
            "task: renew passport",
            "cookie: SECRET_COOKIE",
            "token=SECRET_TOKEN",
            "ssn: 123-45-6789",
        ]
    )
    gw = _enabled_gateway(session=session)
    obs = gw.observe(_context(), ALLOWED_TARGET)
    blob = json.dumps(obs, default=str)
    for secret in ("SECRET_COOKIE", "SECRET_TOKEN", "123-45-6789"):
        assert secret not in blob


def test_credential_injection_is_trusted_side_only_never_returns_key():
    gw = _enabled_gateway()
    result = gw.inject_profile(PROFILE_REF)
    blob = json.dumps(result, default=str)
    assert "SECRET_COOKIE" not in blob
    assert "SECRET_TOKEN" not in blob


def test_user_supplied_key_is_refused():
    gw = _enabled_gateway()
    with pytest.raises((ValueError, ContractValidationError, TypeError)):
        gw.inject_profile(PROFILE_REF, credentials={"cookie": "attacker"})
