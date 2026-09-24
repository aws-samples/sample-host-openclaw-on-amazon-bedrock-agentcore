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
 * Token refresh: the Cognito access token lives 1 h. Shortly before expiry the
 * contract server mints a fresh one and rewrites openclaw.json with the new
 * header. OpenClaw watches the file and hot-applies `mcp` changes without a
 * gateway restart; per the upstream hot-reload doc "MCP config changes retire
 * only changed or removed server connections ... active runs can continue
 * calling their tools" (docs/gateway/configuration/hot-reload.md,
 * docs/gateway/config-extensions.md). Verified live against the Gateway: its
 * Streamable-HTTP endpoint is stateless per request (no Mcp-Session-Id), each
 * call is authorised on its own bearer, and the previous token stays valid
 * until its own exp, so an in-flight call is unaffected — see
 * docs/gateway-mcp-tools.md.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const SERVER_NAME = "agentcore";
// Refresh this long before the token's expiry. Cognito tokens last 3600 s; the
// provider already caches to expiresAt = issued + (expiresIn - 60) s.
const DEFAULT_REFRESH_LEAD_MS = 5 * 60 * 1000;

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

/**
 * Rewrite only the bearer header of mcp.servers.agentcore in an existing
 * openclaw.json. Writes atomically (tmp + rename) so the OpenClaw watcher sees
 * a complete file. Returns true when the file changed.
 */
function rewriteBearer(configPath, token) {
  const raw = fs.readFileSync(configPath, "utf8");
  const config = JSON.parse(raw);
  const server = config.mcp && config.mcp.servers && config.mcp.servers[SERVER_NAME];
  if (!server) return false;
  const nextAuth = `Bearer ${token}`;
  if (server.headers && server.headers.Authorization === nextAuth) return false;
  server.headers = { ...(server.headers || {}), Authorization: nextAuth };
  const tmp = path.join(path.dirname(configPath), `.openclaw.json.${process.pid}.tmp`);
  fs.writeFileSync(tmp, JSON.stringify(config, null, 2));
  fs.renameSync(tmp, configPath);
  return true;
}

/**
 * Keep the bearer fresh. `getToken()` resolves to `{ token, expiresAt }` (the
 * cognito-token provider shape). Schedules the next refresh at
 * expiresAt - leadMs (min 30 s) and returns a handle with stop().
 * `setTimer`/`clearTimer` are injectable for tests.
 */
function scheduleBearerRefresh({
  configPath,
  getToken,
  leadMs = DEFAULT_REFRESH_LEAD_MS,
  now = Date.now,
  setTimer = setTimeout,
  clearTimer = clearTimeout,
  log = console.log,
  onRefresh = () => {},
}) {
  let timer = null;
  let stopped = false;

  function planNext(expiresAt) {
    if (stopped) return;
    const delay = Math.max(30 * 1000, expiresAt - leadMs - now());
    timer = setTimer(tick, delay);
    if (timer && typeof timer.unref === "function") timer.unref();
  }

  async function tick() {
    if (stopped) return;
    try {
      // Force a fresh token: the provider caches until 60 s before expiry, so
      // a refresh 5 min ahead needs the cache bypassed.
      const entry = await getToken({ force: true });
      if (!entry) throw new Error("token provider returned null");
      const changed = rewriteBearer(configPath, entry.token);
      log(`[gateway-mcp] bearer ${changed ? "refreshed" : "unchanged"}; next refresh before ${new Date(entry.expiresAt).toISOString()}`);
      onRefresh(entry, changed);
      planNext(entry.expiresAt);
    } catch (err) {
      log(`[gateway-mcp] bearer refresh failed: ${err.message}; retrying in 60s`);
      timer = setTimer(tick, 60 * 1000);
      if (timer && typeof timer.unref === "function") timer.unref();
    }
  }

  return {
    start(expiresAt) {
      planNext(expiresAt);
    },
    stop() {
      stopped = true;
      if (timer) clearTimer(timer);
    },
    _tick: tick,
  };
}

module.exports = {
  SERVER_NAME,
  DEFAULT_REFRESH_LEAD_MS,
  isEnabled,
  buildServerEntry,
  applyGatewayMcp,
  rewriteBearer,
  scheduleBearerRefresh,
};
