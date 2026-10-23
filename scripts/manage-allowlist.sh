#!/bin/bash
# Manage the user allowlist in DynamoDB.
#
# Usage:
#   ./scripts/manage-allowlist.sh add telegram:123456
#   ./scripts/manage-allowlist.sh add slack:U789ABC
#   ./scripts/manage-allowlist.sh add telegram_group:-1001234567890   (a Telegram group; see docs/channels.md)
#   ./scripts/manage-allowlist.sh remove telegram:123456
#   ./scripts/manage-allowlist.sh disable telegram:123456
#   ./scripts/manage-allowlist.sh enable telegram:123456
#   ./scripts/manage-allowlist.sh list
#
# Uses the openclaw-identity DynamoDB table.
# Requires: aws cli, appropriate IAM permissions.
# Set AWS_PROFILE and AWS_REGION as needed.

set -euo pipefail

TABLE_NAME="${IDENTITY_TABLE_NAME:-openclaw-identity}"
REGION="${AWS_REGION:-us-west-2}"
PROFILE_ARG=""
if [ -n "${AWS_PROFILE:-}" ]; then
    PROFILE_ARG="--profile $AWS_PROFILE"
fi

usage() {
    echo "Usage: $0 {add|remove|disable|enable|list} [channel:user_id]"
    echo ""
    echo "Commands:"
    echo "  add     <channel:user_id>  — Allow a user to register (e.g. telegram:123456)"
    echo "  remove  <channel:user_id>  — Stop a user who has not registered yet from registering"
    echo "  disable <channel:user_id>  — Block a registered user on all linked channels and cron"
    echo "  enable  <channel:user_id>  — Undo disable"
    echo "  list                       — List all allowed users"
    echo ""
    echo "  A Telegram group is allowed with telegram_group:<chat_id> (e.g. telegram_group:-1001234567890)."
    echo ""
    echo "Environment:"
    echo "  AWS_REGION   — AWS region (default: us-west-2)"
    echo "  AWS_PROFILE  — AWS CLI profile (optional)"
    echo "  IDENTITY_TABLE_NAME — DynamoDB table (default: openclaw-identity)"
    exit 1
}

cmd_add() {
    local channel_key="$1"
    validate_channel_key "$channel_key"
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
    validate_channel_key "$channel_key"

    echo "Removing $channel_key from allowlist..."
    aws dynamodb delete-item \
        --table-name "$TABLE_NAME" \
        --region "$REGION" \
        $PROFILE_ARG \
        --key "{
            \"PK\": {\"S\": \"ALLOW#${channel_key}\"},
            \"SK\": {\"S\": \"ALLOW\"}
        }"
    echo "Done. $channel_key is no longer allowed to register."
    echo "Note: an already registered user keeps access. Run '$0 disable $channel_key' to block them."
}

# Resolve channel:user_id to the internal user ID it is bound to.
resolve_user_id() {
    local channel_key="$1"
    local user_id
    user_id=$(aws dynamodb get-item \
        --table-name "$TABLE_NAME" \
        --region "$REGION" \
        $PROFILE_ARG \
        --consistent-read \
        --key "{
            \"PK\": {\"S\": \"CHANNEL#${channel_key}\"},
            \"SK\": {\"S\": \"PROFILE\"}
        }" \
        --query 'Item.userId.S' \
        --output text)
    if [[ ! "$user_id" =~ ^[A-Za-z0-9_-]+$ ]] || [ "$user_id" = "None" ]; then
        echo "Error: $channel_key is not registered." >&2
        exit 1
    fi
    echo "$user_id"
}

# Router and cron reject a user whose PROFILE has accessStatus = DISABLED.
cmd_disable() {
    local channel_key="$1"
    validate_channel_key "$channel_key"
    local user_id now_iso
    user_id=$(resolve_user_id "$channel_key")
    now_iso=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    echo "Disabling $user_id (bound to $channel_key)..."
    aws dynamodb update-item \
        --table-name "$TABLE_NAME" \
        --region "$REGION" \
        $PROFILE_ARG \
        --key "{
            \"PK\": {\"S\": \"USER#${user_id}\"},
            \"SK\": {\"S\": \"PROFILE\"}
        }" \
        --condition-expression "attribute_exists(PK)" \
        --update-expression "SET accessStatus = :disabled, disabledAt = :now" \
        --expression-attribute-values "{
            \":disabled\": {\"S\": \"DISABLED\"},
            \":now\": {\"S\": \"${now_iso}\"}
        }"
    echo "Done. Messages on every channel linked to $user_id and its scheduled tasks are rejected from the next turn."
}

cmd_enable() {
    local channel_key="$1"
    validate_channel_key "$channel_key"
    local user_id
    user_id=$(resolve_user_id "$channel_key")

    echo "Enabling $user_id (bound to $channel_key)..."
    aws dynamodb update-item \
        --table-name "$TABLE_NAME" \
        --region "$REGION" \
        $PROFILE_ARG \
        --key "{
            \"PK\": {\"S\": \"USER#${user_id}\"},
            \"SK\": {\"S\": \"PROFILE\"}
        }" \
        --condition-expression "attribute_exists(PK)" \
        --update-expression "REMOVE accessStatus, disabledAt"
    echo "Done. $user_id is enabled."
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

# channel_key is written into the DynamoDB item/key JSON, so it must not
# contain quotes, backslashes, braces or whitespace. The ID part covers every
# channel's user IDs (numeric, Slack/LINE IDs, Feishu ou_..., phone numbers).
validate_channel_key() {
    if ! [[ "$1" =~ ^[a-z][a-z0-9_]{0,31}:[A-Za-z0-9_.@+-]{1,128}$ ]]; then
        echo "ERROR: expected <channel>:<user_id> (e.g. telegram:123456); the user ID may" >&2
        echo "       only contain letters, digits and _ . @ + -" >&2
        exit 1
    fi
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
    disable)
        [ $# -lt 2 ] && usage
        cmd_disable "$2"
        ;;
    enable)
        [ $# -lt 2 ] && usage
        cmd_enable "$2"
        ;;
    list)
        cmd_list
        ;;
    *)
        usage
        ;;
esac
