#!/bin/bash
# Set up Slack Event Subscriptions and add the deployer to the user allowlist.
#
# Usage:
#   ./scripts/setup-slack.sh
#
# This script:
#   1. Displays the webhook URL for Slack Event Subscriptions configuration
#   2. Prompts for your Slack user ID
#   3. Adds you to the allowlist so you can use the bot immediately
#
# Prerequisites:
#   - CDK stacks deployed (OpenClawRouter)
#   - Slack app created at https://api.slack.com/apps with required scopes
#   - Slack credentials stored in Secrets Manager (openclaw/channels/slack)
#   - aws cli configured with appropriate permissions
#
# Environment:
#   CDK_DEFAULT_REGION — AWS region (default: us-west-2)
#   AWS_PROFILE        — AWS CLI profile (optional)

set -euo pipefail

REGION="${CDK_DEFAULT_REGION:-${AWS_REGION:-us-west-2}}"
TABLE_NAME="${IDENTITY_TABLE_NAME:-openclaw-identity}"
PROFILE_ARG=""
if [ -n "${AWS_PROFILE:-}" ]; then
    PROFILE_ARG="--profile $AWS_PROFILE"
fi

echo "=== OpenClaw Slack Setup ==="
echo ""

# --- Step 1: Display webhook URL ---
echo "Step 1: Configure Slack Event Subscriptions"
echo ""

API_URL=$(aws cloudformation describe-stacks \
    --stack-name OpenClawRouter \
    --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
    --output text --region "$REGION" $PROFILE_ARG)

WEBHOOK_URL="${API_URL}webhook/slack"

echo "Your Slack webhook URL is:"
echo ""
echo "  $WEBHOOK_URL"
echo ""
echo "Paste this URL into your Slack app's Event Subscriptions:"
echo "  1. Go to https://api.slack.com/apps → select your app"
echo "  2. Features → Event Subscriptions → Enable Events"
echo "  3. Set Request URL to the URL above"
echo "  4. Subscribe to bot events: message.im (and optionally message.channels)"
echo "  5. Save Changes"
echo ""
read -rp "Press Enter once you've configured the Event Subscriptions URL..."
echo ""

# --- Step 2: Get deployer's Slack user ID ---
echo "Step 2: Add yourself to the allowlist"
echo ""
echo "To find your Slack user ID:"
echo "  1. Open Slack → click your profile picture (bottom-left)"
echo "  2. Click 'Profile'"
echo "  3. Click the '...' (more) button → 'Copy member ID'"
echo "  The ID looks like: U0AFVC4GEAE"
echo ""
read -rp "Enter your Slack member ID (e.g. U0AFVC4GEAE): " SLACK_USER_ID

# Validate: must start with U and be alphanumeric
if ! [[ "$SLACK_USER_ID" =~ ^U[A-Z0-9]+$ ]]; then
    echo "WARNING: Slack member IDs typically start with 'U' followed by uppercase alphanumeric characters."
    echo "Got: $SLACK_USER_ID"
    read -rp "Continue anyway? (y/N): " CONFIRM
    if [[ "${CONFIRM:-n}" != "y" && "${CONFIRM:-n}" != "Y" ]]; then
        echo "Aborted."
        exit 1
    fi
fi

# --- Step 3: Add to allowlist ---
CHANNEL_KEY="slack:${SLACK_USER_ID}"
NOW_ISO=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

echo "Adding $CHANNEL_KEY to allowlist..."
aws dynamodb put-item \
    --table-name "$TABLE_NAME" \
    --region "$REGION" \
    $PROFILE_ARG \
    --item "{
        \"PK\": {\"S\": \"ALLOW#${CHANNEL_KEY}\"},
        \"SK\": {\"S\": \"ALLOW\"},
        \"channelKey\": {\"S\": \"${CHANNEL_KEY}\"},
        \"addedAt\": {\"S\": \"${NOW_ISO}\"}
    }"

echo ""
echo "=== Setup complete ==="
echo ""
echo "  Webhook URL: $WEBHOOK_URL"
echo "  Allowlisted: $CHANNEL_KEY"
echo ""
echo "You can now DM your Slack bot. The first message will take"
echo "~4 minutes (container cold start), subsequent messages are fast."
echo ""
echo "To add more users later:"
echo "  ./scripts/manage-allowlist.sh add slack:<member_id>"
