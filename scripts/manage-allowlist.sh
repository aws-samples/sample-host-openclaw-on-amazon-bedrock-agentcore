#!/bin/bash
# Manage the user allowlist in DynamoDB.
#
# Usage:
#   ./scripts/manage-allowlist.sh add telegram:123456
#   ./scripts/manage-allowlist.sh add slack:U789ABC
#   ./scripts/manage-allowlist.sh remove telegram:123456
#   ./scripts/manage-allowlist.sh list
#
# Uses the openclaw-identity DynamoDB table.
# Requires: aws cli, appropriate IAM permissions.
# AWS region is fixed to eu-west-1. Set AWS_PROFILE as needed.

set -euo pipefail

TABLE_NAME="${IDENTITY_TABLE_NAME:-openclaw-identity}"
REQUIRED_REGION="eu-west-1"
for region_variable in CDK_DEFAULT_REGION AWS_REGION AWS_DEFAULT_REGION; do
    configured_region="${!region_variable:-}"
    if [ -n "$configured_region" ] && [ "$configured_region" != "$REQUIRED_REGION" ]; then
        echo "ERROR: $region_variable must be exactly $REQUIRED_REGION; got $configured_region." >&2
        exit 1
    fi
done
REGION="$REQUIRED_REGION"
PROFILE_ARG=""
if [ -n "${AWS_PROFILE:-}" ]; then
    PROFILE_ARG="--profile $AWS_PROFILE"
fi

usage() {
    echo "Usage: $0 {add|remove|list} [channel:user_id]"
    echo ""
    echo "Commands:"
    echo "  add    <channel:user_id>  — Allow a user (e.g. telegram:123456)"
    echo "  remove <channel:user_id>  — Revoke a user's access"
    echo "  list                      — List all allowed users"
    echo ""
    echo "Environment:"
    echo "  CDK_DEFAULT_REGION / AWS_REGION / AWS_DEFAULT_REGION"
    echo "               — if set, must be eu-west-1"
    echo "  AWS_PROFILE  — AWS CLI profile (optional)"
    echo "  IDENTITY_TABLE_NAME — DynamoDB table (default: openclaw-identity)"
    exit 1
}

cmd_add() {
    local channel_key="$1"
    local now_iso
    now_iso=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    echo "Adding $channel_key to allowlist..."
    aws dynamodb put-item \
        --table-name "$TABLE_NAME" \
        --region "$REGION" \
        $PROFILE_ARG \
        --item "{
            \"PK\": {\"S\": \"ALLOW#${channel_key}\"},
            \"SK\": {\"S\": \"ALLOW\"},
            \"channelKey\": {\"S\": \"${channel_key}\"},
            \"addedAt\": {\"S\": \"${now_iso}\"}
        }"
    echo "Done. $channel_key is now allowed."
}

cmd_remove() {
    local channel_key="$1"

    echo "Removing $channel_key from allowlist..."
    aws dynamodb delete-item \
        --table-name "$TABLE_NAME" \
        --region "$REGION" \
        $PROFILE_ARG \
        --key "{
            \"PK\": {\"S\": \"ALLOW#${channel_key}\"},
            \"SK\": {\"S\": \"ALLOW\"}
        }"
    echo "Done. $channel_key is no longer allowed."
    echo "Note: If the user already has a session, they can still use it until it expires."
}

cmd_list() {
    echo "Allowed users in $TABLE_NAME ($REGION):"
    echo ""
    aws dynamodb scan \
        --table-name "$TABLE_NAME" \
        --region "$REGION" \
        $PROFILE_ARG \
        --filter-expression "begins_with(PK, :prefix)" \
        --expression-attribute-values '{":prefix": {"S": "ALLOW#"}}' \
        --query 'Items[].{User: channelKey.S, AddedAt: addedAt.S}' \
        --output table
}

if [ $# -lt 1 ]; then
    usage
fi

case "$1" in
    add)
        [ $# -lt 2 ] && usage
        cmd_add "$2"
        ;;
    remove)
        [ $# -lt 2 ] && usage
        cmd_remove "$2"
        ;;
    list)
        cmd_list
        ;;
    *)
        usage
        ;;
esac
