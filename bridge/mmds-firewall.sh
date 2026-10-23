#!/bin/bash
# Block the unprivileged OpenClaw agent uid from the microVM metadata service
# (MMDS) and the container-credential endpoint, so a tool shell cannot lift the
# full execution role, while the root supervisor (contract server + Bedrock
# proxy) keeps its metadata access.
#
# This is an owner-match packet filter: it drops traffic from the agent uid to
# the IMDS-style link-local range (169.254.0.0/16, which covers both the
# 169.254.169.254 metadata address and the 169.254.170.2 container-credential
# address) and the IPv6 metadata address. It needs CAP_NET_ADMIN and
# iptables/ip6tables in the image. If either is missing the rule cannot be
# installed; this script then reports the block as INACTIVE and exits 0 — it
# never fails the container, because the uid split (which closes the
# /proc/<pid>/environ path on its own) must still come up. The caller logs the
# residual risk.
#
# Contract with the caller: the LAST line of stdout is `MMDS_BLOCK_STATUS=active`
# or `MMDS_BLOCK_STATUS=inactive`. Everything else is human-readable detail.
#
# Idempotent: an existing identical rule is removed and re-added, so repeated
# runs (e.g. container restarts) do not stack duplicate rules.

set -u

UID_TO_BLOCK="${OPENCLAW_RUNTIME_UID:-10001}"

# IMDS-style metadata / credential endpoints an AgentCore microVM serves on:
# the IPv4 link-local range and the IPv6 metadata address.
V4_META="169.254.0.0/16"
V6_META="fd00:ec2::254"

status="active"

have() { command -v "$1" >/dev/null 2>&1; }

# Install one owner-match DROP rule. Returns non-zero when the tool is missing
# (127) or the insert was refused (anything else, typically no CAP_NET_ADMIN).
install_rule() {
    local tool="$1" addr="$2"
    if ! have "$tool"; then
        echo "[mmds-firewall] $tool not present — cannot block $addr for uid $UID_TO_BLOCK"
        return 127
    fi
    # Remove any prior copy first so the script is idempotent.
    "$tool" -D OUTPUT -m owner --uid-owner "$UID_TO_BLOCK" -d "$addr" -j DROP >/dev/null 2>&1
    if "$tool" -A OUTPUT -m owner --uid-owner "$UID_TO_BLOCK" -d "$addr" -j DROP 2>/dev/null; then
        echo "[mmds-firewall] $tool OUTPUT drop installed: uid $UID_TO_BLOCK -> $addr"
        return 0
    fi
    echo "[mmds-firewall] $tool refused the rule (no CAP_NET_ADMIN?) — $addr NOT blocked for uid $UID_TO_BLOCK"
    return 1
}

install_rule iptables "$V4_META" || status="inactive"
install_rule ip6tables "$V6_META" || status="inactive"

if [ "$status" = "active" ]; then
    echo "[mmds-firewall] metadata/credential endpoints blocked for uid $UID_TO_BLOCK (supervisor uid 0 unaffected)"
else
    echo "[mmds-firewall] metadata endpoint block could NOT be fully installed; relying on uid isolation + least-privilege execution role"
fi

echo "MMDS_BLOCK_STATUS=${status}"
exit 0
