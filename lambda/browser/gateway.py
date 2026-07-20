"""Disabled-by-default trusted Browser Gateway contract (Task 10).

The gateway is OFF unless an explicit trusted ``enabled`` flag, a profile ref,
AND a non-empty exact target allowlist are supplied. It exposes:

* ``observe(context, target)`` -> REDACTED observations (credentials/cookies/
  tokens/PII stripped) validated against a build-time-locked output schema;
* ``inject_profile(profile_ref)`` -> resolves creds lazily trusted-side and
  never returns them; a user-supplied key is refused;
* ``submit`` / ``upload`` / ``send`` / ``delete`` -> each returns an
  ``ActionProposalV1`` that MUST be dispatched through the Task 3 kernel; no
  direct-effect method exists on the gateway.

All browser IAM lives in ``stacks/browser_stack.py``; this module performs no
real browser/network call (the session is an injected fake).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

try:  # package import in Lambda and repository consumers
    from capabilities.contracts import (
        ActionProposalV1,
        ConnectorConnectionV1,
        ContractValidationError,
        _public_https_url,
    )
    from connectors.manifest import compile_manifest_from_source, schema_index_from_source
    from connectors.mcp import (
        ConnectorRequestContext,
        InMemoryPreparedStore,
        SyntheticMcpConnectorAdapter,
    )
except ImportError:  # pragma: no cover - bare-module load path (flat zip layout)
    from contracts import (  # type: ignore[no-redef]
        ActionProposalV1,
        ConnectorConnectionV1,
        ContractValidationError,
        _public_https_url,
    )
    from manifest import (  # type: ignore[no-redef]
        compile_manifest_from_source,
        schema_index_from_source,
    )
    from mcp import (  # type: ignore[no-redef]
        ConnectorRequestContext,
        InMemoryPreparedStore,
        SyntheticMcpConnectorAdapter,
    )


# The runtime never owns a browser; curated browsing is trusted-side only and
# never enabled without an explicit flag + profile + exact target allowlist.
BROWSER_ENABLED_BY_DEFAULT = False
BROWSER_CONNECTOR_ID = "browser.gateway"
BROWSER_MANIFEST_VERSION = "1.0.0"

# The reviewed, release-owned browser operation source. observe is READ; every
# mutating verb is PREPARE (proposal only). Digests bind the exact schema bytes.
_BROWSER_SOURCE: tuple[dict[str, str], ...] = (
    {
        "operationId": "browser.delete",
        "mode": "PREPARE",
        "inputStem": "browser-action-input",
        "outputStem": "browser-action-output",
    },
    {
        "operationId": "browser.observe",
        "mode": "READ",
        "inputStem": "browser-observe-input",
        "outputStem": "browser-observe-output",
    },
    {
        "operationId": "browser.send",
        "mode": "PREPARE",
        "inputStem": "browser-action-input",
        "outputStem": "browser-action-output",
    },
    {
        "operationId": "browser.submit",
        "mode": "PREPARE",
        "inputStem": "browser-action-input",
        "outputStem": "browser-action-output",
    },
    {
        "operationId": "browser.upload",
        "mode": "PREPARE",
        "inputStem": "browser-action-input",
        "outputStem": "browser-action-output",
    },
)

_ACTION_TO_OP = {
    "submit": "browser.submit",
    "upload": "browser.upload",
    "send": "browser.send",
    "delete": "browser.delete",
}

# Lines matching any of these are dropped from observations (defense in depth on
# top of the closed output schema which already forbids undeclared fields).
_REDACT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"cookie",
        r"token",
        r"authorization",
        r"password",
        r"secret",
        r"\bssn\b",
        r"\d{3}-\d{2}-\d{4}",  # US SSN-shaped
        r"bearer\s",
    )
)


class BrowserDisabled(RuntimeError):
    """The gateway is disabled; no browser operation may run."""


class BrowserTargetDenied(ContractValidationError):
    """A target is not in the exact release-owned allowlist."""


def _redact_observation(line: str) -> Optional[str]:
    if any(pattern.search(line) for pattern in _REDACT_PATTERNS):
        return None
    return line


class TrustedProfileVault:
    """Resolves browser profile credentials lazily, trusted-side only."""

    def __init__(self, profiles: Mapping[str, Mapping[str, str]]) -> None:
        self._profiles = {key: dict(value) for key, value in profiles.items()}

    def resolve(self, profile_ref: str) -> Mapping[str, str]:
        creds = self._profiles.get(profile_ref)
        if creds is None:
            raise ContractValidationError("unknown browser profile reference")
        return dict(creds)


class FakeBrowserSession:
    """An offline fake browser session. Never makes a real browser call."""

    def __init__(self, *, observations: Optional[Sequence[str]] = None) -> None:
        self._observations = list(
            observations
            if observations is not None
            else ["task: renew library card", "status: open"]
        )
        self.effect_calls = 0
        self.acted: list[tuple[str, str, tuple[str, ...]]] = []

    def observe(self, target: str) -> list[str]:
        return list(self._observations)

    def act(self, action: str, target: str, fields: Sequence[str]) -> None:
        self.effect_calls += 1
        self.acted.append((action, target, tuple(fields)))


class _BrowserSessionServer:
    """Server shim so the trusted MCP adapter drives the browser session.

    Presents the locked browser tool list, redacts observations, and applies the
    (fake) browser effect. Holds no URL/token; the profile vault stays private.
    """

    def __init__(
        self,
        *,
        session: FakeBrowserSession,
        profiles: TrustedProfileVault,
        profile_ref: str,
        tool_list: Sequence[str],
    ) -> None:
        self._session = session
        self._profiles = profiles  # PRIVATE; never returned in any mapping
        self._profile_ref = profile_ref
        self._tool_list = tuple(tool_list)

    def list_tools(self) -> tuple[str, ...]:
        return self._tool_list

    def read(self, operation: str, args: Mapping[str, object]) -> Mapping[str, object]:
        raw = self._session.observe(str(args["target"]))
        redacted = [
            line for line in (_redact_observation(str(item)) for item in raw)
            if line is not None
        ]
        return {"observations": redacted}

    def apply_effect(
        self, operation: str, args: Mapping[str, object]
    ) -> Mapping[str, object]:
        # Credentials are resolved lazily trusted-side, never returned.
        self._profiles.resolve(self._profile_ref)
        self._session.act(
            str(args["action"]), str(args["target"]), list(args.get("fields", []))
        )
        return {"accepted": True}


class BrowserGateway:
    """Disabled-by-default trusted browser authority boundary."""

    def __init__(
        self,
        *,
        enabled: bool = BROWSER_ENABLED_BY_DEFAULT,
        profile_ref: Optional[str] = None,
        target_allowlist: Sequence[str] = (),
        session: Optional[FakeBrowserSession] = None,
        profiles: Optional[TrustedProfileVault] = None,
        connection: Optional[ConnectorConnectionV1] = None,
        store: Optional[InMemoryPreparedStore] = None,
        schema_dir: str | Path,
    ) -> None:
        self.enabled = bool(enabled)
        self._schema_dir = Path(schema_dir)
        if not self.enabled:
            # A disabled gateway holds no profile, target, or effect authority.
            self._adapter = None
            self._profile_ref = None
            self._allowlist: tuple[str, ...] = ()
            return

        if not profile_ref:
            raise ValueError("an enabled Browser Gateway requires a profile ref")
        normalized_allowlist = tuple(target_allowlist)
        if not normalized_allowlist:
            raise ValueError("an enabled Browser Gateway requires a target allowlist")
        if session is None or profiles is None or connection is None:
            raise ValueError("an enabled Browser Gateway requires its trusted context")
        # Each allowlisted target must be an exact normalized public HTTPS URL.
        self._allowlist = tuple(_public_https_url(target) for target in normalized_allowlist)
        self._profile_ref = profile_ref
        self._profiles = profiles

        manifest = compile_manifest_from_source(
            BROWSER_CONNECTOR_ID,
            BROWSER_MANIFEST_VERSION,
            _BROWSER_SOURCE,
            self._schema_dir,
        )
        server = _BrowserSessionServer(
            session=session,
            profiles=profiles,
            profile_ref=profile_ref,
            tool_list=tuple(op["operationId"] for op in manifest.operations),
        )
        self._adapter = SyntheticMcpConnectorAdapter(
            manifest=manifest,
            connection=connection,
            server=server,
            store=store if store is not None else InMemoryPreparedStore(),
            schema_dir=self._schema_dir,
            connector_id=BROWSER_CONNECTOR_ID,
            schema_files=schema_index_from_source(_BROWSER_SOURCE),
        )

    # --- helpers -----------------------------------------------------------
    def _require_enabled(self) -> None:
        if not self.enabled or self._adapter is None:
            raise BrowserDisabled("the Browser Gateway is disabled")

    def _bind_target(self, target: str) -> str:
        normalized = _public_https_url(target)
        if normalized not in self._allowlist:
            raise BrowserTargetDenied("target is not in the release-owned allowlist")
        return normalized

    def action_adapter(self):
        """Return the trusted ConnectorAdapter that dispatches browser effects."""
        self._require_enabled()
        return self._adapter

    # --- read (redacted observation) --------------------------------------
    def observe(self, context: ConnectorRequestContext, target: str) -> Mapping[str, object]:
        self._require_enabled()
        bound = self._bind_target(target)
        return self._adapter.read(context, "browser.observe", {"target": bound})

    # --- credential injection (trusted-side only) -------------------------
    def inject_profile(
        self, profile_ref: str, credentials: Any = None
    ) -> Mapping[str, object]:
        self._require_enabled()
        if credentials is not None:
            raise ValueError("browser credential injection refuses a user-supplied key")
        if profile_ref != self._profile_ref:
            raise ContractValidationError("profile ref is not the bound trusted profile")
        # Resolve lazily trusted-side; the resolved creds never escape.
        self._profiles.resolve(profile_ref)
        return {"injected": True, "profileRef": profile_ref}

    # --- action proposals (NO direct effect) ------------------------------
    def _propose(
        self,
        action: str,
        context: ConnectorRequestContext,
        target: str,
        fields: Sequence[str],
    ) -> ActionProposalV1:
        self._require_enabled()
        bound = self._bind_target(target)
        operation = _ACTION_TO_OP[action]
        args = {"action": action, "target": bound, "fields": list(fields)}
        return self._adapter.prepare(context, operation, args)

    def submit(self, context, target, fields) -> ActionProposalV1:
        return self._propose("submit", context, target, fields)

    def upload(self, context, target, fields) -> ActionProposalV1:
        return self._propose("upload", context, target, fields)

    def send(self, context, target, fields) -> ActionProposalV1:
        return self._propose("send", context, target, fields)

    def delete(self, context, target, fields) -> ActionProposalV1:
        return self._propose("delete", context, target, fields)


__all__ = [
    "BROWSER_CONNECTOR_ID",
    "BROWSER_ENABLED_BY_DEFAULT",
    "BrowserDisabled",
    "BrowserGateway",
    "BrowserTargetDenied",
    "FakeBrowserSession",
    "TrustedProfileVault",
]
