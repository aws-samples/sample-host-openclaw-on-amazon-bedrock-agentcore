#!/bin/bash
# Set up Telegram webhook and add the deployer to the user allowlist.
#
# Usage:
#   ./scripts/setup-telegram.sh
#
# This script:
#   1. Registers the Telegram webhook with API Gateway
#   2. Prompts for your Telegram user ID
#   3. Adds you to the allowlist so you can use the bot immediately
#
# Prerequisites:
#   - CDK stacks deployed (OpenClawRouter, OpenClawSecurity)
#   - Telegram bot token stored in Secrets Manager (openclaw/channels/telegram)
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

echo "=== OpenClaw Telegram Setup ==="
echo ""

# --- Step 1: Webhook registration ---
echo "Step 1: Registering Telegram webhook..."

API_URL=$(aws cloudformation describe-stacks \
    --stack-name OpenClawRouter \
    --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
    --output text --region "$REGION" $PROFILE_ARG)

WEBHOOK_SECRET=$(aws secretsmanager get-secret-value \
    --secret-id openclaw/webhook-secret \
    --region "$REGION" $PROFILE_ARG --query SecretString --output text)

TELEGRAM_TOKEN=$(aws secretsmanager get-secret-value \
    --secret-id openclaw/channels/telegram \
    --region "$REGION" $PROFILE_ARG --query SecretString --output text)

WEBHOOK_RESULT=$(curl -s "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook?url=${API_URL}webhook/telegram&secret_token=${WEBHOOK_SECRET}")
echo "Webhook result: $WEBHOOK_RESULT"

if ! echo "$WEBHOOK_RESULT" | grep -q '"ok":true'; then
    echo "ERROR: Webhook registration failed. Check your Telegram bot token."
    exit 1
fi
echo "Webhook registered successfully."
echo ""

# --- Step 2: Get deployer's Telegram user ID ---
echo "Step 2: Add yourself to the allowlist"
echo ""
echo "To find your Telegram user ID, message @userinfobot on Telegram"
echo "or send any message to your bot — the rejection reply will show your ID."
echo ""
read -rp "Enter your Telegram user ID (numeric, e.g. 123456789): " TELEGRAM_USER_ID

# Validate: must be numeric
if ! [[ "$TELEGRAM_USER_ID" =~ ^[0-9]+$ ]]; then
    echo "ERROR: Telegram user ID must be numeric. Got: $TELEGRAM_USER_ID"
    exit 1
fi

# --- Step 3: Add to allowlist ---
CHANNEL_KEY="telegram:${TELEGRAM_USER_ID}"
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
echo "  Webhook URL: ${API_URL}webhook/telegram"
echo "  Allowlisted: $CHANNEL_KEY"
echo ""
echo "You can now message your Telegram bot. The first message will take"
echo "~4 minutes (container cold start), subsequent messages are fast."
echo ""
echo "To add more users later:"
echo "  ./scripts/manage-allowlist.sh add telegram:<user_id>"
