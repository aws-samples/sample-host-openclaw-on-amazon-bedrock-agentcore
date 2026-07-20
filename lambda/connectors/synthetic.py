"""The synthetic local MCP "server" fake (Task 10).

Public/synthetic, in-memory, offline. It exposes a fixed tool list and canned
observations / prepare-outputs. It holds a fake URL + fake OAuth token + fake
server config INTERNALLY only, so the adapter tests can prove these strings
never escape into any observation, proposal, receipt, or capability result.

Nothing here makes a real network/MCP/AWS/browser call.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence


# Private, release-owned server configuration. These synthetic secrets exist
# purely so tests can substring-scan every adapter return value and prove they
# are never surfaced. The runtime must remain UNAWARE of all of them.
SYNTHETIC_MCP_URL = "https://synthetic-mcp.internal.invalid/v1/tools"
SYNTHETIC_OAUTH_TOKEN = "oauth-synthetic-DO-NOT-LEAK-000000000000"
SYNTHETIC_SERVER_CONFIG = {
    "mcpUrl": SYNTHETIC_MCP_URL,
    "oauthToken": SYNTHETIC_OAUTH_TOKEN,
    "headers": {"authorization": f"Bearer {SYNTHETIC_OAUTH_TOKEN}"},
}

_DEFAULT_TOOL_LIST = ("synthetic.notes.append", "synthetic.notes.read-list")


class SyntheticMcpTimeout(TimeoutError):
    """The synthetic server crossed its explicit no-retry time boundary."""


class SyntheticMcpServer:
    """An offline fake MCP server with a fixed tool list and canned outputs."""

    def __init__(
        self,
        *,
        tool_list: Optional[Sequence[str]] = None,
        observations: Optional[Mapping[str, Mapping[str, object]]] = None,
        raise_timeout: bool = False,
    ) -> None:
        self._tool_list = tuple(tool_list) if tool_list is not None else _DEFAULT_TOOL_LIST
        self._observations = dict(observations or {})
        self._raise_timeout = raise_timeout
        self._notes: list[str] = ["synthetic reminder: renew library card"]
        # Private config held only inside the fake, never returned.
        self._mcp_url = SYNTHETIC_MCP_URL
        self._oauth_token = SYNTHETIC_OAUTH_TOKEN
        self._server_config = dict(SYNTHETIC_SERVER_CONFIG)
        # Counters for the hostile tests.
        self.read_calls = 0
        self.prepare_calls = 0
        self.effect_calls = 0
        self.token_requests = 0

    # The adapter fetches this and asserts it equals the locked manifest ops.
    def list_tools(self) -> tuple[str, ...]:
        return self._tool_list

    def read(self, operation: str, args: Mapping[str, object]) -> Mapping[str, object]:
        self.read_calls += 1
        if self._raise_timeout:
            raise SyntheticMcpTimeout("synthetic MCP read timed out")
        if operation in self._observations:
            return dict(self._observations[operation])
        if operation == "synthetic.notes.read-list":
            return {"notes": list(self._notes)}
        raise KeyError(operation)

    def prepare(
        self, operation: str, args: Mapping[str, object]
    ) -> Mapping[str, object]:
        self.prepare_calls += 1
        if self._raise_timeout:
            raise SyntheticMcpTimeout("synthetic MCP prepare timed out")
        if operation == "synthetic.notes.append":
            # Canned normalized prepare output (mirrors the input schema).
            return {"text": args["text"]}
        raise KeyError(operation)

    def apply_effect(
        self, operation: str, args: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Perform the synthetic (in-memory only) effect. Called by dispatch."""
        self.effect_calls += 1
        self.token_requests += 1  # a real effect resolves creds lazily here
        if self._raise_timeout:
            raise SyntheticMcpTimeout("synthetic MCP effect timed out")
        if operation == "synthetic.notes.append":
            self._notes.append(str(args["text"]))
            return {"appended": True, "count": len(self._notes)}
        raise KeyError(operation)


__all__ = [
    "SYNTHETIC_MCP_URL",
    "SYNTHETIC_OAUTH_TOKEN",
    "SYNTHETIC_SERVER_CONFIG",
    "SyntheticMcpServer",
    "SyntheticMcpTimeout",
]
