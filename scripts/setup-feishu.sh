#!/bin/bash
# Set up Feishu Bot event subscription and add the deployer to the user allowlist.
#
# Usage:
#   ./scripts/setup-feishu.sh
#
# This script:
#   1. Displays the webhook URL for Feishu Event Subscriptions
#   2. Prompts for Feishu app credentials and stores them in Secrets Manager
#   3. Prompts for your Feishu open_id and adds you to the allowlist
#
# Prerequisites:
#   - CDK stacks deployed (OpenClawRouter, OpenClawSecurity)
#   - Feishu app created at https://open.feishu.cn/app with Bot capability
#   - aws cli configured with appropriate permissions
#
# Environment:
#   CDK_DEFAULT_REGION -- AWS region (default: us-west-2)
#   AWS_PROFILE        -- AWS CLI profile (optional)

set -euo pipefail

REGION="${CDK_DEFAULT_REGION:-${AWS_REGION:-us-west-2}}"
TABLE_NAME="${IDENTITY_TABLE_NAME:-openclaw-identity}"
PROFILE_ARG=""
if [ -n "${AWS_PROFILE:-}" ]; then
    PROFILE_ARG="--profile $AWS_PROFILE"
fi

echo "=== OpenClaw Feishu Setup ==="
echo ""

# --- Step 1: Display webhook URL ---
echo "Step 1: Webhook URL"
echo ""

API_URL=$(aws cloudformation describe-stacks \
    --stack-name OpenClawRouter \
    --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
    --output text --region "$REGION" $PROFILE_ARG)

WEBHOOK_URL="${API_URL}webhook/feishu"

echo "Your Feishu webhook URL is:"
echo ""
echo "  $WEBHOOK_URL"
echo ""
echo "Configure in Feishu Developer Console (https://open.feishu.cn/app):"
echo ""
echo "  Step A: Create & configure the app (if not done yet)"
echo "    1. Create a self-built app"
echo "    2. Add Bot capability"
echo "    3. Permissions -> add these scopes:"
echo "       - im:message              (receive messages)"
echo "       - im:message:send_as_bot  (send messages as bot)"
echo "       - im:message.content:readonly (read message content)"
echo "       - im:chat:readonly        (read group info)"
echo "       - im:resource             (download images)"
echo ""
echo "  Step B: Configure event subscriptions"
echo "    1. Event Subscriptions -> Request URL -> paste the URL above"
echo "    2. Set 'Send events to' -> Developer Server"
echo "    3. Add events:"
echo "       - im.message.receive_v1          (required)"
echo "       - im.chat.member.bot.added_v1    (recommended)"
echo "       - im.chat.member.bot.deleted_v1  (recommended)"
echo ""
echo "  Step C: Publish the app"
echo "    - The bot is NOT usable by others until published!"
echo "    - After publishing, users can search and add the bot on Feishu"
echo ""
read -rp "Press Enter once you've completed the above steps..."
echo ""

# --- Step 2: Store credentials ---
echo "Step 2: Store Feishu credentials in Secrets Manager"
echo ""
echo "Find these in your Feishu app's 'Credentials & Basic Info' and 'Event Subscriptions'."
echo ""
read -rp "Enter your Feishu App ID: " APP_ID
read -rp "Enter your Feishu App Secret: " APP_SECRET
read -rp "Enter your Verification Token: " VERIFICATION_TOKEN
read -rp "Enter your Encrypt Key: " ENCRYPT_KEY

echo "Storing credentials in Secrets Manager..."
aws secretsmanager update-secret \
    --secret-id openclaw/channels/feishu \
    --secret-string "{\"appId\":\"${APP_ID}\",\"appSecret\":\"${APP_SECRET}\",\"verificationToken\":\"${VERIFICATION_TOKEN}\",\"encryptKey\":\"${ENCRYPT_KEY}\"}" \
    --region "$REGION" $PROFILE_ARG

echo "Credentials stored."
echo ""

# --- Step 3: Add to allowlist ---
echo "Step 3: Add yourself to the allowlist"
echo ""
echo "To find your Feishu open_id, message the bot -- the rejection reply will show your ID."
echo "The open_id looks like: ou_xxxxxxxxxxxxxxxxxxxx"
echo ""
read -rp "Enter your Feishu open_id (e.g. ou_xxxx): " FEISHU_USER_ID

# Validate: must start with ou_
if ! [[ "$FEISHU_USER_ID" =~ ^ou_ ]]; then
    echo "WARNING: Feishu open_id typically starts with 'ou_'."
    echo "Got: $FEISHU_USER_ID"
    read -rp "Continue anyway? (y/N): " CONFIRM
    if [[ "${CONFIRM:-n}" != "y" && "${CONFIRM:-n}" != "Y" ]]; then
        echo "Aborted."
        exit 1
    fi
fi

CHANNEL_KEY="feishu:${FEISHU_USER_ID}"
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
echo "You can now message your Feishu bot. The first message will take"
echo "~2 minutes (container cold start), subsequent messages are fast."
echo ""
echo "To add more users later:"
echo "  ./scripts/manage-allowlist.sh add feishu:<open_id>"
