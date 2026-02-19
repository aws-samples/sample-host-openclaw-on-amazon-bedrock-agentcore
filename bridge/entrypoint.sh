#!/bin/bash
set -euo pipefail

echo "[openclaw-bridge] Starting OpenClaw messaging bridge..."

# --- Read gateway token from Secrets Manager ---
if [ -n "${GATEWAY_TOKEN_SECRET_ID:-}" ]; then
    echo "[openclaw-bridge] Fetching gateway token from Secrets Manager..."
    SM_ERR=$(mktemp)
    GATEWAY_TOKEN=$(aws secretsmanager get-secret-value \
        --secret-id "${GATEWAY_TOKEN_SECRET_ID}" \
        --region "${AWS_REGION:?AWS_REGION env var required}" \
        --query 'SecretString' \
        --output text 2>"${SM_ERR}" || echo "")
    if [ -s "${SM_ERR}" ]; then
        echo "[openclaw-bridge] Secrets Manager error: $(cat "${SM_ERR}")"
    fi
    rm -f "${SM_ERR}"

    if [ -z "${GATEWAY_TOKEN}" ]; then
        echo "[openclaw-bridge] WARNING: Could not fetch gateway token, using fallback"
        GATEWAY_TOKEN="changeme"
    fi
else
    echo "[openclaw-bridge] WARNING: No GATEWAY_TOKEN_SECRET_ID set"
    GATEWAY_TOKEN="${GATEWAY_TOKEN:-changeme}"
fi

# --- Read Cognito password derivation secret ---
COGNITO_PASSWORD_SECRET=""
if [ -n "${COGNITO_PASSWORD_SECRET_ID:-}" ]; then
    echo "[openclaw-bridge] Fetching Cognito password secret from Secrets Manager..."
    COGNITO_PASSWORD_SECRET=$(aws secretsmanager get-secret-value \
        --secret-id "${COGNITO_PASSWORD_SECRET_ID}" \
        --region "${AWS_REGION:?AWS_REGION env var required}" \
        --query 'SecretString' --output text 2>/dev/null || echo "")
    if [ -n "${COGNITO_PASSWORD_SECRET}" ]; then
        echo "[openclaw-bridge] Cognito password secret loaded"
    else
        echo "[openclaw-bridge] WARNING: Could not fetch Cognito password secret"
    fi
fi
export COGNITO_PASSWORD_SECRET

# --- Read channel bot tokens if available ---
read_channel_secret() {
    local channel="$1"
    local secret_id="openclaw/channels/${channel}"
    local value
    value=$(aws secretsmanager get-secret-value \
        --secret-id "${secret_id}" \
        --region "${AWS_REGION:?AWS_REGION env var required}" \
        --query 'SecretString' \
        --output text 2>/dev/null || echo "")
    echo "${value}"
}

TELEGRAM_TOKEN=$(read_channel_secret "telegram")
DISCORD_TOKEN=$(read_channel_secret "discord")
SLACK_TOKEN=$(read_channel_secret "slack")

# Validate tokens — skip channels with placeholder/empty tokens to avoid
# constant retry loops that may interfere with other channels.
is_valid_token() {
    local token="$1"
    # Must be non-empty and not a known placeholder
    [ -n "${token}" ] && [ "${token}" != "changeme" ] && [ "${token}" != "placeholder" ] && [ ${#token} -gt 20 ]
}
if ! is_valid_token "${DISCORD_TOKEN}"; then
    echo "[openclaw-bridge] Discord token missing or placeholder, skipping"
    DISCORD_TOKEN=""
fi
if ! is_valid_token "${SLACK_TOKEN}"; then
    echo "[openclaw-bridge] Slack token missing or placeholder, skipping"
    SLACK_TOKEN=""
fi

# --- Start the AgentCore proxy adapter ---
echo "[openclaw-bridge] Starting AgentCore proxy adapter on port 18790..."
AGENTCORE_ENDPOINT="${AGENTCORE_ENDPOINT:-}" \
    node /app/agentcore-proxy.js &
PROXY_PID=$!

# Wait for proxy to be ready
sleep 2

# --- Write OpenClaw config ---
echo "[openclaw-bridge] Writing OpenClaw configuration..."

# Build channels object dynamically using correct config key names per provider
CHANNELS_JSON="{}"
if [ -n "${TELEGRAM_TOKEN}" ]; then
    CHANNELS_JSON=$(echo "${CHANNELS_JSON}" | jq --arg t "${TELEGRAM_TOKEN}" '. + {"telegram": {"enabled": true, "botToken": $t, "dmPolicy": "open", "allowFrom": ["*"]}}')
fi
if [ -n "${DISCORD_TOKEN}" ]; then
    CHANNELS_JSON=$(echo "${CHANNELS_JSON}" | jq --arg t "${DISCORD_TOKEN}" '. + {"discord": {"enabled": true, "token": $t}}')
fi
if [ -n "${SLACK_TOKEN}" ]; then
    CHANNELS_JSON=$(echo "${CHANNELS_JSON}" | jq --arg t "${SLACK_TOKEN}" '. + {"slack": {"enabled": true, "botToken": $t}}')
fi

cat > /root/.openclaw/openclaw.json <<CONF
{
  "models": {
    "providers": {
      "agentcore": {
        "baseUrl": "http://127.0.0.1:18790/v1",
        "apiKey": "local",
        "api": "openai-completions",
        "models": [
          {
            "id": "bedrock-agentcore",
            "name": "Bedrock AgentCore"
          }
        ]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": {
        "primary": "agentcore/bedrock-agentcore"
      }
    }
  },
  "gateway": {
    "mode": "local",
    "port": 18789,
    "bind": "lan",
    "trustedProxies": ["0.0.0.0/0"],
    "auth": {
      "mode": "token",
      "token": "${GATEWAY_TOKEN}"
    },
    "controlUi": {
      "enabled": true,
      "allowInsecureAuth": true,
      "allowedOrigins": ["https://${CLOUDFRONT_DOMAIN:-localhost}"]
    }
  },
  "channels": ${CHANNELS_JSON}
}
CONF

echo "[openclaw-bridge] Configuration written. Starting OpenClaw daemon..."

# --- Start OpenClaw ---
# Force IPv4 for all Node.js connections — the VPC has no IPv6 and Node 22's
# Happy Eyeballs (autoSelectFamily) fails when IPv6 is unreachable.
export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--dns-result-order=ipv4first --no-network-family-autoselection -r /app/force-ipv4.js"

# Disable IPv6 at the OS level so DNS never returns AAAA records to Node.js.
# This is the most reliable fix for the Node 22 + NAT Gateway + IPv6 issue.
if [ -w /proc/sys/net/ipv6/conf/all/disable_ipv6 ]; then
    echo 1 > /proc/sys/net/ipv6/conf/all/disable_ipv6
    echo "[openclaw-bridge] IPv6 disabled at OS level"
else
    echo "[openclaw-bridge] WARNING: Cannot disable IPv6 (no write access to /proc/sys)"
fi

exec openclaw gateway run --port 18789 --bind lan --allow-unconfigured --verbose
