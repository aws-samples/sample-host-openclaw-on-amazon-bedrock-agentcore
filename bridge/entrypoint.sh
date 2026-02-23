#!/bin/bash
# Start the contract server immediately — AgentCore requires a fast /ping response.
# Secrets are fetched by the contract server itself via the AWS SDK.
# Do NOT use set -e — the contract server must start regardless of any pre-flight issues.

echo "[openclaw-agentcore] Starting OpenClaw on AgentCore Runtime (per-user session mode)..."
echo "[openclaw-agentcore] Node: $(node --version 2>&1 || echo 'not found')"
echo "[openclaw-agentcore] AWS_REGION=${AWS_REGION:-not set}"

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
# Must be the first thing to start — AgentCore health-checks /ping very quickly.
# The contract server fetches secrets from Secrets Manager on first chat invocation.
echo "[openclaw-agentcore] Starting AgentCore contract server on port 8080..."
exec node /app/agentcore-contract.js
