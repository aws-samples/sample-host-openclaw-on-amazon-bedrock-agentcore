/**
 * AgentCore Gateway MCP server wiring for the OpenClaw config (prototype).
 *
 * When AGENTCORE_GATEWAY_URL is set, the contract server adds
 *
 *   mcp.servers.agentcore = {
 *     url: <gateway url>, transport: "streamable-http",
 *     headers: { Authorization: "Bearer <per-user Cognito ACCESS token>" }, ...
 *   }
 *
 * to the generated openclaw.json (OpenClaw v2026.9.5 docs/gateway/config-extensions.md).
 * With the env var unset nothing is added and the config is byte-identical to
 * the pre-gateway build.
 *
 * Token refresh is handled by the contract readiness check before dispatch.
 */
"use strict";

const SERVER_NAME = "agentcore";
function isEnabled(env = process.env) {
  return typeof env.AGENTCORE_GATEWAY_URL === "string" && env.AGENTCORE_GATEWAY_URL.trim() !== "";
}

/** Build the mcp.servers.<agentcore> entry. Pure. */
function buildServerEntry(gatewayUrl, token) {
  if (!gatewayUrl) throw new Error("gatewayUrl is required");
  if (!token) throw new Error("token is required");
  return {
    url: gatewayUrl,
    transport: "streamable-http",
    headers: { Authorization: `Bearer ${token}` },
    requestTimeoutMs: 30000,
    connectionTimeoutMs: 10000,
    supportsParallelToolCalls: true,
  };
}

/**
 * Return a copy of `config` with mcp.servers.agentcore set (when gatewayUrl is
 * set) or untouched (when it is not). Never mutates the input.
 */
function applyGatewayMcp(config, { gatewayUrl, token } = {}) {
  if (!gatewayUrl) return config;
  const next = { ...config };
  const mcp = { ...(config.mcp || {}) };
  mcp.servers = { ...(mcp.servers || {}), [SERVER_NAME]: buildServerEntry(gatewayUrl, token) };
  next.mcp = mcp;
  return next;
}

module.exports = {
  SERVER_NAME,
  isEnabled,
  buildServerEntry,
  applyGatewayMcp,
};
