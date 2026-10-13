#!/bin/bash
# Container entrypoint for OpenClaw on AgentCore Runtime V2.
#
# Records launch lineage for snapshot diagnostics and prepares Node.js caching
# and IPv4 networking before handing off to the contract server. Requires Bash
# and the bridge files installed under /app by the container image.

if [[ "${AGENTCORE_RUNTIME_PLATFORM+x}" && "$AGENTCORE_RUNTIME_PLATFORM" != "V2" ]]; then
    echo "[openclaw-agentcore] AGENTCORE_RUNTIME_PLATFORM must be V2 when set" >&2
    exit 1
fi

# Capture shell execution before pre-flight work; snapshots inherit this lineage.
entrypoint_epoch_realtime=${EPOCHREALTIME:-}
unset OPENCLAW_ENTRYPOINT_WALL_MS OPENCLAW_ENTRYPOINT_EXECUTION_ID
if [[ "$entrypoint_epoch_realtime" =~ ^[0-9]{10}\.[0-9]{6}$ ]]; then
    entrypoint_seconds=${entrypoint_epoch_realtime%.*}
    entrypoint_fraction=${entrypoint_epoch_realtime#*.}
    export OPENCLAW_ENTRYPOINT_WALL_MS=$((10#$entrypoint_seconds * 1000 + 10#${entrypoint_fraction:0:3}))
    export OPENCLAW_ENTRYPOINT_EXECUTION_ID="entrypoint-${OPENCLAW_ENTRYPOINT_WALL_MS}-${RANDOM}-${RANDOM}"
    printf '{"schema_version":1,"event":"ENTRYPOINT_EXEC","wall_unix_ms":%s,"entrypoint_execution_id":"%s","delivery":"producer"}\n' \
        "$OPENCLAW_ENTRYPOINT_WALL_MS" "$OPENCLAW_ENTRYPOINT_EXECUTION_ID"
fi
unset entrypoint_epoch_realtime entrypoint_seconds entrypoint_fraction

# Start the contract server; V2 health waits for state-free common bootstrap.
# The common gateway token is fetched at bootstrap; user resources wait for activation.
# Do NOT use set -e — the contract server must start regardless of any pre-flight issues.

echo "[openclaw-agentcore] Starting OpenClaw on AgentCore Runtime (per-user session mode)..."
echo "[openclaw-agentcore] Node: $(node --version 2>&1 || echo 'not found')"
echo "[openclaw-agentcore] AWS_REGION=${AWS_REGION:-not set}"

# --- V8 Compile Cache (Node.js 22+) ---
# Caches compiled bytecode so modules load faster on subsequent runs.
# Pre-warmed at Docker build time with AWS SDK modules.
if [ -d /app/.compile-cache ]; then
    export NODE_COMPILE_CACHE=/app/.compile-cache
    echo "[openclaw-agentcore] V8 compile cache enabled: /app/.compile-cache"
fi

# --- Force IPv4 for Node.js 22 VPC compatibility ---
export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--dns-result-order=ipv4first --no-network-family-autoselection -r /app/force-ipv4.js"

# Disable IPv6 at the OS level if writable (best-effort)
if [ -w /proc/sys/net/ipv6/conf/all/disable_ipv6 ]; then
    echo 1 > /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null || true
    echo "[openclaw-agentcore] IPv6 disabled at OS level"
else
    echo "[openclaw-agentcore] WARNING: Cannot disable IPv6 (no write access to /proc/sys)"
fi

# --- Start the AgentCore contract server (port 8080) ---
echo "[openclaw-agentcore] Starting AgentCore contract server on port 8080..."
echo "[openclaw-agentcore] Common bootstrap -> user activation -> OpenClaw"
exec node /app/agentcore-contract.js
