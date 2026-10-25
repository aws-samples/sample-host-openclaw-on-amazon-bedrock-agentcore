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

# --- Privilege separation preflight (runs as root, before the contract server) ---
# OpenClaw and its tool shells run as an unprivileged uid (see runtime-privsep.js).
# Two defence-in-depth layers are set up here while we still have root; both are
# best-effort and the container must come up regardless of whether they succeed.
# The uid split itself is applied per-child by the contract server and already
# closes the cross-process /proc/<pid>/environ read on its own.
export OPENCLAW_RUNTIME_UID="${OPENCLAW_RUNTIME_UID:-10001}"
export OPENCLAW_RUNTIME_GID="${OPENCLAW_RUNTIME_GID:-10001}"

# Layer 1: hide other processes' /proc entries from the agent uid (hidepid).
# Needs CAP_SYS_ADMIN to remount /proc; skipped cleanly when unavailable.
if command -v mount >/dev/null 2>&1 && mount -o remount,hidepid=2 /proc 2>/dev/null; then
    export OPENCLAW_PROC_HIDDEN="active"
    echo "[openclaw-agentcore] /proc remounted with hidepid=2 (other processes hidden from the agent uid)"
else
    export OPENCLAW_PROC_HIDDEN="inactive"
    echo "[openclaw-agentcore] /proc hidepid INACTIVE (remount refused; needs CAP_SYS_ADMIN); uid split still blocks cross-process environ reads"
fi

# Layer 2: block the agent uid from the metadata / container-credential endpoints.
if [ -x /app/mmds-firewall.sh ]; then
    mmds_output="$(/app/mmds-firewall.sh 2>&1)"
    echo "$mmds_output"
    OPENCLAW_MMDS_BLOCK="$(printf '%s\n' "$mmds_output" | sed -n 's/^MMDS_BLOCK_STATUS=//p' | tail -1)"
    export OPENCLAW_MMDS_BLOCK="${OPENCLAW_MMDS_BLOCK:-unknown}"
else
    export OPENCLAW_MMDS_BLOCK="unknown"
    echo "[openclaw-agentcore] WARNING: /app/mmds-firewall.sh missing — metadata endpoint NOT blocked for the agent uid"
fi

# --- Start the AgentCore contract server (port 8080) ---
echo "[openclaw-agentcore] Starting AgentCore contract server on port 8080..."
echo "[openclaw-agentcore] Common bootstrap -> user activation -> OpenClaw"
exec node /app/agentcore-contract.js
