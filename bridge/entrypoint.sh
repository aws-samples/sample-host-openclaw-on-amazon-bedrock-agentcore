#!/bin/bash
set -euo pipefail

echo "[openclaw-agentcore] Starting OpenClaw on AgentCore Runtime..."

# --- Force IPv4 for Node.js 22 VPC compatibility ---
export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--dns-result-order=ipv4first --no-network-family-autoselection -r /app/force-ipv4.js"

# Disable IPv6 at the OS level if writable
if [ -w /proc/sys/net/ipv6/conf/all/disable_ipv6 ]; then
    echo 1 > /proc/sys/net/ipv6/conf/all/disable_ipv6
    echo "[openclaw-agentcore] IPv6 disabled at OS level"
else
    echo "[openclaw-agentcore] WARNING: Cannot disable IPv6 (no write access to /proc/sys)"
fi

# --- 1. Start the AgentCore contract server (port 8080) IMMEDIATELY ---
# This MUST be first! AgentCore health check hits /ping within seconds of container start.
echo "[openclaw-agentcore] Starting AgentCore contract server on port 8080..."
node /app/agentcore-contract.js &
CONTRACT_PID=$!

# Wait briefly for the contract server to bind
sleep 1
echo "[openclaw-agentcore] Contract server started (PID ${CONTRACT_PID})"

# --- 2. Fetch secrets from Secrets Manager ---
echo "[openclaw-agentcore] Fetching secrets from Secrets Manager..."

# Gateway token
if [ -n "${GATEWAY_TOKEN_SECRET_ID:-}" ]; then
    SM_ERR=$(mktemp)
    GATEWAY_TOKEN=$(aws secretsmanager get-secret-value \
        --secret-id "${GATEWAY_TOKEN_SECRET_ID}" \
        --region "${AWS_REGION:?AWS_REGION must be set}" \
        --query 'SecretString' \
        --output text 2>"${SM_ERR}" || echo "")
    if [ -s "${SM_ERR}" ]; then
        echo "[openclaw-agentcore] Secrets Manager error: $(cat "${SM_ERR}")"
    fi
    rm -f "${SM_ERR}"

    if [ -z "${GATEWAY_TOKEN}" ]; then
        echo "[openclaw-agentcore] WARNING: Could not fetch gateway token, using fallback"
        GATEWAY_TOKEN="changeme"
    fi
else
    echo "[openclaw-agentcore] WARNING: No GATEWAY_TOKEN_SECRET_ID set"
    GATEWAY_TOKEN="${GATEWAY_TOKEN:-changeme}"
fi

# Cognito password derivation secret
COGNITO_PASSWORD_SECRET=""
if [ -n "${COGNITO_PASSWORD_SECRET_ID:-}" ]; then
    COGNITO_PASSWORD_SECRET=$(aws secretsmanager get-secret-value \
        --secret-id "${COGNITO_PASSWORD_SECRET_ID}" \
        --region "${AWS_REGION:?AWS_REGION must be set}" \
        --query 'SecretString' --output text 2>/dev/null || echo "")
    if [ -n "${COGNITO_PASSWORD_SECRET}" ]; then
        echo "[openclaw-agentcore] Cognito password secret loaded"
    else
        echo "[openclaw-agentcore] WARNING: Could not fetch Cognito password secret"
    fi
fi
export COGNITO_PASSWORD_SECRET

# Channel bot tokens
read_channel_secret() {
    local channel="$1"
    local secret_id="openclaw/channels/${channel}"
    local value
    value=$(aws secretsmanager get-secret-value \
        --secret-id "${secret_id}" \
        --region "${AWS_REGION:?AWS_REGION must be set}" \
        --query 'SecretString' \
        --output text 2>/dev/null || echo "")
    echo "${value}"
}

TELEGRAM_TOKEN=$(read_channel_secret "telegram")
DISCORD_TOKEN=$(read_channel_secret "discord")
SLACK_TOKEN=$(read_channel_secret "slack")

# Validate tokens — skip channels with placeholder/empty tokens
is_valid_token() {
    local token="$1"
    [ -n "${token}" ] && [ "${token}" != "changeme" ] && [ "${token}" != "placeholder" ] && [ ${#token} -gt 20 ]
}
if ! is_valid_token "${DISCORD_TOKEN}"; then
    echo "[openclaw-agentcore] Discord token missing or placeholder, skipping"
    DISCORD_TOKEN=""
fi
if ! is_valid_token "${SLACK_TOKEN}"; then
    echo "[openclaw-agentcore] Slack token missing or placeholder, skipping"
    SLACK_TOKEN=""
fi

echo "[openclaw-agentcore] Secrets loaded"

# --- 3. Start the Bedrock proxy adapter (port 18790) ---
echo "[openclaw-agentcore] Starting Bedrock proxy adapter on port 18790..."
node /app/agentcore-proxy.js &
PROXY_PID=$!
sleep 2
echo "[openclaw-agentcore] Memory ID: ${AGENTCORE_MEMORY_ID:-not configured}"

# --- 4. Write OpenClaw config ---
echo "[openclaw-agentcore] Writing OpenClaw configuration..."

CHANNELS_JSON="{}"
if [ -n "${TELEGRAM_TOKEN}" ]; then
    CHANNELS_JSON=$(echo "${CHANNELS_JSON}" | jq --arg t "${TELEGRAM_TOKEN}" '. + {"telegram": {"enabled": true, "botToken": $t, "dmPolicy": "open", "allowFrom": ["*"]}}')
fi
if [ -n "${DISCORD_TOKEN}" ]; then
    CHANNELS_JSON=$(echo "${CHANNELS_JSON}" | jq --arg t "${DISCORD_TOKEN}" '. + {"discord": {"enabled": true, "token": $t}}')
fi
if [ -n "${SLACK_TOKEN}" ]; then
    # Slack secret can be JSON {"botToken":"xoxb-...","appToken":"xapp-..."} or plain bot token string.
    # Socket Mode requires appToken; without it OpenClaw cannot connect to Slack.
    SLACK_BOT_TOKEN=""
    SLACK_APP_TOKEN=""
    if echo "${SLACK_TOKEN}" | jq -e '.botToken' >/dev/null 2>&1; then
        SLACK_BOT_TOKEN=$(echo "${SLACK_TOKEN}" | jq -r '.botToken')
        SLACK_APP_TOKEN=$(echo "${SLACK_TOKEN}" | jq -r '.appToken // empty')
    else
        SLACK_BOT_TOKEN="${SLACK_TOKEN}"
    fi
    if [ -n "${SLACK_APP_TOKEN}" ]; then
        CHANNELS_JSON=$(echo "${CHANNELS_JSON}" | jq \
            --arg bt "${SLACK_BOT_TOKEN}" \
            --arg at "${SLACK_APP_TOKEN}" \
            '. + {"slack": {"enabled": true, "botToken": $bt, "appToken": $at, "dmPolicy": "open", "allowFrom": ["*"]}}')
        echo "[openclaw-agentcore] Slack configured with botToken + appToken (Socket Mode)"
    elif [ -n "${SLACK_BOT_TOKEN}" ]; then
        CHANNELS_JSON=$(echo "${CHANNELS_JSON}" | jq --arg t "${SLACK_BOT_TOKEN}" '. + {"slack": {"enabled": true, "botToken": $t}}')
        echo "[openclaw-agentcore] WARNING: Slack configured with botToken only — Socket Mode requires appToken"
    fi
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
  "tools": {
    "profile": "full",
    "deny": ["write", "edit", "apply_patch"]
  },
  "skills": {
    "allowBundled": ["*"],
    "load": {
      "extraDirs": ["/skills"]
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
      "enabled": false
    }
  },
  "channels": ${CHANNELS_JSON}
}
CONF

echo "[openclaw-agentcore] Configuration written. Starting OpenClaw gateway..."

# --- 5. Start OpenClaw gateway (port 18789) ---
# This runs in the foreground. If it exits, the container exits.
exec openclaw gateway run --port 18789 --bind lan --allow-unconfigured --verbose
