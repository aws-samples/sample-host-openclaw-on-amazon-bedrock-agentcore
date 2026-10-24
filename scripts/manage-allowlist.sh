#!/bin/bash
# Manage the user allowlist and the operator disable switch in DynamoDB.
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
# Requires: aws cli, jq, appropriate IAM permissions.
# Set AWS_PROFILE and AWS_REGION as needed.
#
#   * `remove <channel:user_id>` deletes the ALLOW# record.
#   * `disable <channel:user_id>` resolves the internal user the channel
#     identity is bound to and sets USER#<id>/PROFILE.accessStatus = DISABLED.
#     The router and the cron Lambda check it before resolving a route, so
#     every linked channel and every scheduled task is refused from the next
#     turn.
#   * `enable <channel:user_id>` clears it.

set -euo pipefail

TABLE_NAME="${IDENTITY_TABLE_NAME:-openclaw-identity}"
REGION="${AWS_REGION:-us-west-2}"
# An array keeps a profile name with unusual characters a single argument.
profile_args=()
if [ -n "${AWS_PROFILE:-}" ]; then
    profile_args=(--profile "$AWS_PROFILE")
fi

# Run the AWS CLI with the shared table/region/profile arguments. The
# ${arr[@]+...} form keeps `set -u` happy on bash < 4.4 when no profile is set.
aws_ddb() {
    local op="$1"
    shift
    aws dynamodb "$op" \
        --table-name "$TABLE_NAME" \
        --region "$REGION" \
        ${profile_args[@]+"${profile_args[@]}"} \
        "$@"
}

require_jq() {
    if ! command -v jq >/dev/null 2>&1; then
        echo "ERROR: 'jq' is required but was not found on PATH. Install jq and retry." >&2
        exit 1
    fi
}

# DynamoDB key {PK, SK} built with jq, so no value is spliced into JSON text.
ddb_key() {
    jq -cn --arg pk "$1" --arg sk "$2" '{PK:{S:$pk},SK:{S:$sk}}'
}

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
    require_jq
    local now_iso item
    now_iso=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    item=$(jq -cn --arg k "$channel_key" --arg t "$now_iso" \
        '{PK:{S:("ALLOW#"+$k)},SK:{S:"ALLOW"},channelKey:{S:$k},addedAt:{S:$t}}')

    echo "Adding $channel_key to allowlist..."
    aws_ddb put-item --item "$item"
    echo "Done. $channel_key is now allowed."
}

cmd_remove() {
    local channel_key="$1"
    validate_channel_key "$channel_key"
    require_jq

    echo "Removing $channel_key from allowlist..."
    aws_ddb delete-item --key "$(ddb_key "ALLOW#${channel_key}" ALLOW)"
    echo "Done. $channel_key is no longer allowed to register."
    echo "Note: if $channel_key has already registered, run '$0 disable $channel_key'"
    echo "to block it on every linked channel and its scheduled tasks."
}

# Resolve channel:user_id to the internal user ID it is bound to.
resolve_user_id() {
    local channel_key="$1"
    local user_id
    user_id=$(aws_ddb get-item \
        --consistent-read \
        --key "$(ddb_key "CHANNEL#${channel_key}" PROFILE)" \
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
    require_jq
    local user_id now_iso values
    user_id=$(resolve_user_id "$channel_key")
    now_iso=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    values=$(jq -cn --arg t "$now_iso" '{":disabled":{S:"DISABLED"},":now":{S:$t}}')

    echo "Disabling $user_id (bound to $channel_key)..."
    aws_ddb update-item \
        --key "$(ddb_key "USER#${user_id}" PROFILE)" \
        --condition-expression "attribute_exists(PK)" \
        --update-expression "SET accessStatus = :disabled, disabledAt = :now" \
        --expression-attribute-values "$values"
    echo "Done. Messages on every channel linked to $user_id and its scheduled tasks are rejected from the next turn."
}

cmd_enable() {
    local channel_key="$1"
    validate_channel_key "$channel_key"
    require_jq
    local user_id
    user_id=$(resolve_user_id "$channel_key")

    echo "Enabling $user_id (bound to $channel_key)..."
    aws_ddb update-item \
        --key "$(ddb_key "USER#${user_id}" PROFILE)" \
        --condition-expression "attribute_exists(PK)" \
        --update-expression "REMOVE accessStatus, disabledAt"
    echo "Done. $user_id is enabled."
}

cmd_list() {
    echo "Allowed users in $TABLE_NAME ($REGION):"
    echo ""
    aws_ddb scan \
        --filter-expression "begins_with(PK, :prefix)" \
        --expression-attribute-values '{":prefix": {"S": "ALLOW#"}}' \
        --query 'Items[].{User: channelKey.S, AddedAt: addedAt.S}' \
        --output table
}

# channel_key is written into DynamoDB keys, so it must not contain quotes,
# backslashes, braces or whitespace (jq also escapes it, as a second layer).
# The ID part covers every channel's user IDs (numeric, Slack/LINE IDs,
# Feishu ou_..., phone numbers, negative Telegram group chat IDs).
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
