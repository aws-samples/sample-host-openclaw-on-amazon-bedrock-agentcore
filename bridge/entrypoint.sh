#!/bin/bash
# Start the contract server immediately — AgentCore requires a fast /ping response.
# The contract server mints scoped workspace credentials during trusted init.
set -euo pipefail
umask 077

export PATH="/usr/local/bin:/usr/bin:/bin"
export HOME="/root"
export NODE_PATH="/app/node_modules"
export OPENCLAW_CONFIG_PATH="/run/personal-operator/openclaw.json"
export OPENCLAW_STATE_DIR="/mnt/workspace/live"
export OPENCLAW_WORKSPACE_DIR="/mnt/workspace/live/workspace"

echo '{"version":1,"event":"RUNTIME_ENTRYPOINT","level":"INFO","status":"INITIALIZING"}'

# --- V8 Compile Cache (Node.js 22+) ---
# Caches compiled bytecode so modules load faster on subsequent runs.
# Pre-warmed at Docker build time with AWS SDK modules.
if [ -d /app/.compile-cache ]; then
    export NODE_COMPILE_CACHE=/app/.compile-cache
    echo '{"version":1,"event":"COMPILE_CACHE","level":"INFO","status":"READY"}'
fi

# --- Force IPv4 for Node.js 22 VPC compatibility ---
export NODE_OPTIONS="--dns-result-order=ipv4first --no-network-family-autoselection -r /app/force-ipv4.js"

# Disable IPv6 at the OS level if writable (best-effort)
if [ -w /proc/sys/net/ipv6/conf/all/disable_ipv6 ]; then
    echo 1 > /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null || true
    echo '{"version":1,"event":"IPV6_POLICY","level":"INFO","status":"READY"}'
else
    echo '{"version":1,"event":"IPV6_POLICY","level":"WARN","status":"DENIED"}'
fi

# --- Start the AgentCore contract server (port 8080) ---
# Must be the first thing to start — AgentCore health-checks /ping very quickly.
# Lightweight agent handles messages after scoped init while OpenClaw starts.
echo '{"version":1,"event":"CONTRACT_SERVER","level":"INFO","status":"INITIALIZING"}'
exec node /app/agentcore-contract.js
