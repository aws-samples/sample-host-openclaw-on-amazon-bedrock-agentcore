#!/bin/bash
# Manage the user allowlist and the operator disable switch in DynamoDB.
#
# Usage:
#   ./scripts/manage-allowlist.sh add telegram:123456
#   ./scripts/manage-allowlist.sh add slack:U789ABC
#   ./scripts/manage-allowlist.sh add telegram_group:-1001234567890   (a Telegram group; see docs/channels.md)
#   ./scripts/manage-allowlist.sh add slack_channel:C0123ABCDEF       (a Slack channel; see docs/channels.md)
#   ./scripts/manage-allowlist.sh add feishu_group:oc_0123abcd        (a Feishu group chat; see docs/channels.md)
#   ./scripts/manage-allowlist.sh remove telegram:123456
#   ./scripts/manage-allowlist.sh disable telegram:123456
#   ./scripts/manage-allowlist.sh enable telegram:123456
#   ./scripts/manage-allowlist.sh list
#   ./scripts/manage-allowlist.sh whois user_0123456789abcdef
#   ./scripts/manage-allowlist.sh set-cap telegram:123456 5           (daily cost limit in USD; see README)
#   ./scripts/manage-allowlist.sh clear-cap telegram:123456
#   ./scripts/manage-allowlist.sh usage telegram:123456
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
#   * `set-cap <channel:user_id> <usd>` sets the daily cost limit of the internal
#     user the channel identity is bound to (USAGE#<id>/CAP), replacing
#     per_user_daily_cost_usd for that user; 0 exempts the user. A linked user has
#     one limit across channels; a Telegram group, Slack channel or Feishu group
#     chat identity has its own. It applies only while per_user_daily_cost_usd in
#     cdk.json is above 0.
#   * `clear-cap <channel:user_id>` removes it (back to per_user_daily_cost_usd).
#   * `usage <channel:user_id>` prints today's (UTC) estimated usage and the override.

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
    echo "Usage: $0 {add|remove|disable|enable|list|whois|set-cap|clear-cap|usage} [channel:user_id] [usd]"
    echo ""
    echo "Commands:"
    echo "  add     <channel:user_id>  — Allow a user to register (e.g. telegram:123456)"
    echo "  remove  <channel:user_id>  — Stop a user who has not registered yet from registering"
    echo "  disable <channel:user_id>  — Block a registered user on all linked channels and cron"
    echo "  enable  <channel:user_id>  — Undo disable"
    echo "  list                       — List all allowed users"
    echo "  whois   <user_id>          — List the channel actors linked to an internal user ID"
    echo "  set-cap <channel:user_id> <usd>"
    echo "                             — Daily cost limit for a registered user (0 = no limit); applies"
    echo "                               while per_user_daily_cost_usd in cdk.json is above 0"
    echo "  clear-cap <channel:user_id> — Back to the per_user_daily_cost_usd default"
    echo "  usage   <channel:user_id>  — Today's (UTC) estimated usage and the user's limit override"
    echo ""
    echo "  A Telegram group is allowed with telegram_group:<chat_id> (e.g. telegram_group:-1001234567890)."
    echo "  With channel_identity_mode \"channel\", a Slack channel is allowed with slack_channel:<channel_id>"
    echo "  (e.g. slack_channel:C0123ABCDEF) and a Feishu group chat with feishu_group:<chat_id>"
    echo "  (e.g. feishu_group:oc_0123abcd)."
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
    case "$channel_key" in
        slack_channel:*|feishu_group:*)
            echo "Note: the conversation is answered as its own identity only while channel_identity_mode"
            echo "is \"channel\" in cdk.json (see docs/channels.md)."
            ;;
    esac
}

cmd_remove() {
    local channel_key="$1"
    validate_channel_key "$channel_key"
    require_jq

    echo "Removing $channel_key from allowlist..."
    aws_ddb delete-item --key "$(ddb_key "ALLOW#${channel_key}" ALLOW)"
    case "$channel_key" in
        slack_channel:*|feishu_group:*)
            # The router reads the record on every message from the conversation.
            echo "Done. The next message from $channel_key is ignored."
            return
            ;;
    esac
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

# Map an internal user ID back to the channel actors linked to it, so an
# operator can still identify a person from a user ID that now stands in for
# the raw actor in the logs. Reads the USER#<id> partition: the CHANNEL#<actor>
# back-reference of every linked channel, plus the primary enrollmentActorId.
cmd_whois() {
    local user_id="$1"
    if ! [[ "$user_id" =~ ^user_[A-Za-z0-9_-]{1,128}$ ]]; then
        echo "ERROR: expected an internal user ID, e.g. user_0123456789abcdef" >&2
        exit 1
    fi
    require_jq
    local values items actors
    values=$(jq -cn --arg pk "USER#${user_id}" '{":pk":{S:$pk}}')
    items=$(aws_ddb query \
        --key-condition-expression "PK = :pk" \
        --expression-attribute-values "$values" \
        --output json)
    actors=$(printf '%s' "$items" | jq -r '
        [ (.Items[] | select(.SK.S | startswith("CHANNEL#")) | .SK.S[8:]),
          (.Items[] | select(.SK.S == "PROFILE") | .enrollmentActorId.S // empty) ]
        | unique | .[]')
    if [ -z "$actors" ]; then
        echo "No channels found for ${user_id} (unknown or not registered)."
        exit 0
    fi
    echo "Channel actors linked to ${user_id} in $TABLE_NAME ($REGION):"
    printf '%s\n' "$actors"
}

# A USD amount from 0 to 10000, the range per_user_daily_cost_usd accepts. It is
# written as a DynamoDB number with jq, so it must be a plain decimal number.
validate_usd() {
    if ! [[ "$1" =~ ^([0-9]{1,4}(\.[0-9]{1,6})?|10000(\.0{1,6})?)$ ]]; then
        echo "ERROR: expected an amount in USD from 0 to 10000 (e.g. 5 or 2.50)" >&2
        exit 1
    fi
}

# Per-user daily cost limit (per_user_daily_cost_usd). The override lives on
# USAGE#<id>/CAP, outside the USER#<id> partition the agent can write; the
# Router and the cron Lambda read it before every turn while the limit is on.
cmd_set_cap() {
    local channel_key="$1" usd="$2"
    validate_channel_key "$channel_key"
    validate_usd "$usd"
    require_jq
    local user_id now_iso item
    user_id=$(resolve_user_id "$channel_key")
    now_iso=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    item=$(jq -cn --arg pk "USAGE#${user_id}" --arg usd "$usd" --arg t "$now_iso" \
        '{PK:{S:$pk},SK:{S:"CAP"},dailyCostUsd:{N:$usd},setAt:{S:$t}}')

    echo "Setting the daily cost limit of $user_id (bound to $channel_key) to \$$usd..."
    aws_ddb put-item --item "$item"
    if [[ "$usd" =~ ^0+(\.0+)?$ ]]; then
        echo "Done. $user_id has no daily cost limit."
    else
        echo "Done. $user_id is limited to \$$usd of estimated cost per UTC day."
    fi
    echo "Note: the override applies only while per_user_daily_cost_usd in cdk.json is above 0."
}

cmd_clear_cap() {
    local channel_key="$1"
    validate_channel_key "$channel_key"
    require_jq
    local user_id
    user_id=$(resolve_user_id "$channel_key")

    echo "Removing the daily cost limit override of $user_id (bound to $channel_key)..."
    aws_ddb delete-item --key "$(ddb_key "USAGE#${user_id}" CAP)"
    echo "Done. $user_id has the per_user_daily_cost_usd default from cdk.json."
}

cmd_usage() {
    local channel_key="$1"
    validate_channel_key "$channel_key"
    require_jq
    local user_id today values items
    user_id=$(resolve_user_id "$channel_key")
    today=$(date -u +"%Y-%m-%d")
    values=$(jq -cn --arg pk "USAGE#${user_id}" --arg day "DAY#${today}" \
        '{":pk":{S:$pk},":cap":{S:"CAP"},":day":{S:$day}}')
    items=$(aws_ddb query \
        --key-condition-expression "PK = :pk AND SK BETWEEN :cap AND :day" \
        --expression-attribute-values "$values" \
        --output json)
    [ -n "$items" ] || items='{"Items":[]}'
    printf '%s' "$items" | jq -r --arg user "$user_id" --arg today "$today" '
        (.Items // []) as $items
        | (first($items[] | select(.SK.S == ("DAY#" + $today))) // {}) as $day
        | (first($items[] | select(.SK.S == "CAP")) // null) as $cap
        | "Usage of \($user) on \($today) (UTC), estimated:",
          "  cost:        $\($day.costUsd.N // "0") (scheduled tasks: $\($day.cronCostUsd.N // "0"))",
          "  model calls: \($day.calls.N // "0") (scheduled tasks: \($day.cronCalls.N // "0"))",
          "  tokens:      \($day.inputTokens.N // "0") in, \($day.outputTokens.N // "0") out",
          "  limit:       " + (if $cap == null then "per_user_daily_cost_usd from cdk.json (no override)"
                             elif ($cap.dailyCostUsd.N | tonumber) == 0 then "none (override 0, set \($cap.setAt.S // "?"))"
                             else "$\($cap.dailyCostUsd.N) (override, set \($cap.setAt.S // "?"))" end)'
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
    # A conversation identity takes the platform's conversation ID, the same
    # shape the router accepts (lambda/router/channel_identity.py).
    case "$1" in
        slack_channel:*)
            if ! [[ "$1" =~ ^slack_channel:[CG][A-Z0-9]{2,31}$ ]]; then
                echo "ERROR: expected slack_channel:<channel ID>, e.g. slack_channel:C0123ABCDEF" >&2
                echo "       (the channel ID from the channel details, not its #name or a member ID)" >&2
                exit 1
            fi
            ;;
        feishu_group:*)
            if ! [[ "$1" =~ ^feishu_group:oc_[A-Za-z0-9]{1,49}$ ]]; then
                echo "ERROR: expected feishu_group:<chat_id>, e.g. feishu_group:oc_0123abcd" >&2
                exit 1
            fi
            ;;
    esac
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
    whois)
        [ $# -lt 2 ] && usage
        cmd_whois "$2"
        ;;
    set-cap)
        [ $# -lt 3 ] && usage
        cmd_set_cap "$2" "$3"
        ;;
    clear-cap)
        [ $# -lt 2 ] && usage
        cmd_clear_cap "$2"
        ;;
    usage)
        [ $# -lt 2 ] && usage
        cmd_usage "$2"
        ;;
    *)
        usage
        ;;
esac
