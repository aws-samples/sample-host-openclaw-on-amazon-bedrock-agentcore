#!/bin/bash
# Block the unprivileged OpenClaw agent uid from the microVM metadata service
# (MMDS) and the container-credential endpoint, so a tool shell cannot lift the
# full execution role, while the root supervisor (contract server + Bedrock
# proxy) keeps its metadata access.
#
# This is an owner-match packet filter: it drops traffic from the agent uid to
# the metadata and credential endpoints only:
#   169.254.169.254/32  instance metadata service (MMDS / IMDS)
#   169.254.170.2/32    container credential endpoint
#   fd00:ec2::254       IPv6 metadata address
# It deliberately does NOT block the rest of 169.254.0.0/16: the Amazon VPC
# DNS resolver (169.254.169.253) and the Amazon Time Sync Service
# (169.254.169.123) are in that range and the agent uid still needs both.
#
# A credential endpoint given as AWS_CONTAINER_CREDENTIALS_FULL_URI on any
# other address is not covered by these rules; the contract server's startup
# log reports the endpoint shape so this can be checked.
#
# It needs CAP_NET_ADMIN and iptables/ip6tables in the image. If either is
# missing a rule cannot be installed; this script then reports the block as
# INACTIVE and exits 0. It never fails the container, because the uid split
# (which closes the /proc/<pid>/environ path on its own) must still come up.
# The caller logs the residual risk.
#
# Contract with the caller: the LAST line of stdout is `MMDS_BLOCK_STATUS=active`
# or `MMDS_BLOCK_STATUS=inactive`. Everything else is human-readable detail.
#
# Idempotent: an existing identical rule is removed and re-added, so repeated
# runs (e.g. container restarts) do not stack duplicate rules.

set -u

UID_TO_BLOCK="${OPENCLAW_RUNTIME_UID:-10001}"

# Metadata / credential endpoints to block (see header). Space-separated.
V4_META="169.254.169.254/32 169.254.170.2/32"
V6_META="fd00:ec2::254"

status="active"

have() { command -v "$1" >/dev/null 2>&1; }

# Install one owner-match DROP rule. Returns non-zero when the tool is missing
# (127) or the insert was refused (anything else, typically no CAP_NET_ADMIN).
install_rule() {
    local tool="$1" addr="$2"
    if ! have "$tool"; then
        echo "[mmds-firewall] $tool not present: cannot block $addr for uid $UID_TO_BLOCK"
        return 127
    fi
    # Remove any prior copy first so the script is idempotent.
    "$tool" -D OUTPUT -m owner --uid-owner "$UID_TO_BLOCK" -d "$addr" -j DROP >/dev/null 2>&1
    if "$tool" -A OUTPUT -m owner --uid-owner "$UID_TO_BLOCK" -d "$addr" -j DROP 2>/dev/null; then
        echo "[mmds-firewall] $tool OUTPUT drop installed: uid $UID_TO_BLOCK -> $addr"
        return 0
    fi
    echo "[mmds-firewall] $tool refused the rule (no CAP_NET_ADMIN?): $addr NOT blocked for uid $UID_TO_BLOCK"
    return 1
}

for addr in $V4_META; do
    install_rule iptables "$addr" || status="inactive"
done
for addr in $V6_META; do
    install_rule ip6tables "$addr" || status="inactive"
done

if [ "$status" = "active" ]; then
    echo "[mmds-firewall] metadata/credential endpoints blocked for uid $UID_TO_BLOCK (supervisor uid 0, DNS resolver and time sync unaffected)"
else
    echo "[mmds-firewall] metadata endpoint block INACTIVE (could not be fully installed); relying on uid isolation + least-privilege execution role"
fi

echo "MMDS_BLOCK_STATUS=${status}"
exit 0
