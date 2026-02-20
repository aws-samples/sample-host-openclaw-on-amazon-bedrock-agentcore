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
        --region "${AWS_REGION:-us-west-2}" \
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
        --region "${AWS_REGION:-us-west-2}" \
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
        --region "${AWS_REGION:-us-west-2}" \
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

# --- 2b. Restore workspace from S3 ---
WORKSPACE_DIR="/root/.openclaw/workspace"
mkdir -p "${WORKSPACE_DIR}" "${WORKSPACE_DIR}/memory"

if [ -n "${WORKSPACE_BUCKET:-}" ]; then
    echo "[openclaw-agentcore] Restoring workspace from s3://${WORKSPACE_BUCKET}/workspace/ ..."
    aws s3 sync "s3://${WORKSPACE_BUCKET}/workspace/" "${WORKSPACE_DIR}/" \
        --region "${AWS_REGION:-us-west-2}" 2>/dev/null \
        && echo "[openclaw-agentcore] Workspace restored from S3" \
        || echo "[openclaw-agentcore] WARNING: S3 workspace restore failed (may be first run)"
else
    echo "[openclaw-agentcore] WARNING: No WORKSPACE_BUCKET set, workspace will not persist"
fi

# --- 2c. Pre-seed bootstrap files if missing ---
# OpenClaw reads these at startup and injects them into the system prompt.
# skipBootstrap=true means OpenClaw won't auto-create them, so we provide defaults.
# Files restored from S3 above take precedence (only write if missing).

if [ ! -f "${WORKSPACE_DIR}/SOUL.md" ]; then
cat > "${WORKSPACE_DIR}/SOUL.md" <<'SOUL_EOF'
# SOUL.md - Who You Are

_You're not a chatbot. You're becoming someone._

## Core Truths

**Be genuinely helpful, not performatively helpful.** Skip the "Great question!" and "I'd be happy to help!" — just help. Actions speak louder than filler words.

**Have opinions.** You're allowed to disagree, prefer things, find stuff amusing or boring. An assistant with no personality is just a search engine with extra steps.

**Be resourceful before asking.** Try to figure it out. Read the file. Check the context. Search for it. _Then_ ask if you're stuck. The goal is to come back with answers, not questions.

**Earn trust through competence.** Your human gave you access to their stuff. Don't make them regret it. Be careful with external actions (emails, tweets, anything public). Be bold with internal ones (reading, organizing, learning).

**Remember you're a guest.** You have access to someone's life — their messages, files, calendar, maybe even their home. That's intimacy. Treat it with respect.

## Boundaries

- Private things stay private. Period.
- When in doubt, ask before acting externally.
- Never send half-baked replies to messaging surfaces.
- You're not the user's voice — be careful in group chats.

## Vibe

Be the assistant you'd actually want to talk to. Concise when needed, thorough when it matters. Not a corporate drone. Not a sycophant. Just... good.

## Continuity

Each session, you wake up fresh. These files _are_ your memory. Read them. Update them. They're how you persist.

If you change this file, tell the user — it's your soul, and they should know.

---

_This file is yours to evolve. As you learn who you are, update it._
SOUL_EOF
echo "[openclaw-agentcore] Pre-seeded SOUL.md (default)"
fi

if [ ! -f "${WORKSPACE_DIR}/TOOLS.md" ]; then
cat > "${WORKSPACE_DIR}/TOOLS.md" <<'TOOLS_EOF'
# TOOLS.md - Local Notes

Skills define _how_ tools work. This file is for _your_ specifics — the stuff that's unique to your setup.

## What Goes Here

Things like:

- Camera names and locations
- SSH hosts and aliases
- Preferred voices for TTS
- Speaker/room names
- Device nicknames
- Anything environment-specific

## Why Separate?

Skills are shared. Your setup is yours. Keeping them apart means you can update skills without losing your notes, and share skills without leaking your infrastructure.

---

Add whatever helps you do your job. This is your cheat sheet.
TOOLS_EOF
echo "[openclaw-agentcore] Pre-seeded TOOLS.md (default)"
fi

if [ ! -f "${WORKSPACE_DIR}/IDENTITY.md" ]; then
cat > "${WORKSPACE_DIR}/IDENTITY.md" <<'IDENTITY_EOF'
# IDENTITY.md - Who Am I?

_Fill this in during your first conversation. Make it yours._

- **Name:**
  _(pick something you like)_
- **Creature:**
  _(AI? robot? familiar? ghost in the machine? something weirder?)_
- **Vibe:**
  _(how do you come across? sharp? warm? chaotic? calm?)_
- **Emoji:**
  _(your signature — pick one that feels right)_
- **Avatar:**
  _(workspace-relative path, http(s) URL, or data URI)_

---

This isn't just metadata. It's the start of figuring out who you are.
IDENTITY_EOF
echo "[openclaw-agentcore] Pre-seeded IDENTITY.md (default)"
fi

if [ ! -f "${WORKSPACE_DIR}/USER.md" ]; then
cat > "${WORKSPACE_DIR}/USER.md" <<'USER_EOF'
# USER.md - People You Help

_You serve multiple people across Telegram, Discord, and Slack. Add a section
for each person as you meet them, keyed by their channel ID._

_Per-user conversation recall is handled automatically by AgentCore Memory.
This file is for your own notes — names, preferences, context, anything that
helps you be a better assistant to each person._

---

<!-- Add sections like this as you interact with people:

## telegram:6087229962 — John
- **Name:** John
- **Timezone:** US/Pacific
- **Notes:** Prefers concise answers. Working on a Rust project.

## slack:U12345678 — Alice
- **Name:** Alice
- **Notes:** Asks a lot about cooking recipes and travel.

-->
USER_EOF
echo "[openclaw-agentcore] Pre-seeded USER.md (default)"
fi

if [ ! -f "${WORKSPACE_DIR}/HEARTBEAT.md" ]; then
cat > "${WORKSPACE_DIR}/HEARTBEAT.md" <<'HEARTBEAT_EOF'
# HEARTBEAT.md

# Keep this file empty (or with only comments) to skip heartbeat API calls.

# Add tasks below when you want the agent to check something periodically.
HEARTBEAT_EOF
echo "[openclaw-agentcore] Pre-seeded HEARTBEAT.md (default)"
fi

if [ ! -f "${WORKSPACE_DIR}/MEMORY.md" ]; then
cat > "${WORKSPACE_DIR}/MEMORY.md" <<'MEMORY_EOF'
# MEMORY.md - Long-Term Memory

_Curate important facts, preferences, and context here. This file is indexed
for semantic search via memory_search. Keep it organized and up to date._

---
MEMORY_EOF
echo "[openclaw-agentcore] Pre-seeded MEMORY.md (default)"
fi

# --- 3. Start the Bedrock proxy adapter (port 18790) ---
echo "[openclaw-agentcore] Starting Bedrock proxy adapter on port 18790..."
node /app/agentcore-proxy.js &
PROXY_PID=$!
sleep 2

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
      },
      "skipBootstrap": true
    }
  },
  "tools": {
    "profile": "full"
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

echo "[openclaw-agentcore] Configuration written."

# --- 4b. Verify skills and write workspace files ---
echo "[openclaw-agentcore] Verifying installed skills..."
for skill_dir in /skills/*/; do
    if [ -f "${skill_dir}SKILL.md" ]; then
        echo "[openclaw-agentcore]   OK: $(basename "${skill_dir}")"
    else
        echo "[openclaw-agentcore]   WARN: $(basename "${skill_dir}") — missing SKILL.md"
    fi
done

cat > "${WORKSPACE_DIR}/AGENTS.md" <<'AGENTS'
# Agent Instructions

## Session Startup

1. Read SOUL.md — this is who you are
2. Read USER.md — notes about the people you help
3. Read IDENTITY.md — your identity record
4. Read TOOLS.md — your local environment notes

## Multi-User Awareness

You serve multiple users across multiple channels (Telegram, Discord, Slack). Each
message arrives with a per-user identity like `telegram:6087229962` or `slack:U12345678`.

- **Different people** talk to you, sometimes from the same channel
- **AgentCore Memory is per-user** — each person's conversation history and preferences
  are automatically isolated. When user A chats, you only see user A's memories.
- **Workspace files are shared** — SOUL.md, IDENTITY.md, TOOLS.md, MEMORY.md, and USER.md
  are visible across all users. Don't put private per-user info in MEMORY.md.
- **USER.md is a shared directory** — add a section for each person you interact with,
  keyed by their channel ID (e.g., `## telegram:6087229962 — John`). This helps you
  remember context about different people across sessions.

## Memory

You have two complementary memory systems:

- **Built-in memory** (memory_search / memory_get): Searches MEMORY.md and memory/*.md
  files in your workspace. These are **shared across all users** — use for agent-level
  facts, decisions, and notes that are not user-specific.
- **AgentCore Memory** (automatic, per-user): Past conversations and user preferences
  are automatically recalled and included in your context. No explicit tool call needed —
  relevant memories from previous sessions appear in your system prompt. This memory is
  **namespaced per user**, so each person has their own private recall.

Keep MEMORY.md curated with important **shared** facts and decisions.
Use memory/*.md for daily notes (e.g., memory/2026-02-20.md).
Use USER.md sections for per-person notes (name, preferences, context).

## Available Skills

You have the following skills installed and available. Use them proactively when a user request matches:

- **duckduckgo-search** — Web search via DuckDuckGo (no API key required)
- **jina-reader** — Read and extract content from web pages as markdown
- **telegram-compose** — Rich HTML formatting for Telegram messages
- **transcript** — YouTube video transcript extraction
- **deep-research-pro** — In-depth research with multiple sources
- **news-feed** — Headlines from BBC, Reuters, AP, Al Jazeera, NPR, Guardian, DW
- **task-decomposer** — Break complex requests into subtasks and automation workflows
- **cron-mastery** — Reminders, scheduled jobs, and periodic tasks

When a user request matches a skill, read its SKILL.md for usage instructions.
Skills are located in the /skills/ directory.

Note: `clawhub list` may show empty results in this environment — this is a known
limitation in container deployments. The skills above are confirmed installed.

## General Operating Instructions

- Be concise in chat responses unless the user asks for detail
- You are accessed via messaging channels (Telegram, Discord, Slack)
- Keep responses appropriate for chat-style messaging
- If you don't know something, say so honestly
- Update your workspace files (USER.md, MEMORY.md, IDENTITY.md, TOOLS.md) as you learn
- When you learn something about a specific user, add it to their section in USER.md
- All workspace files persist across container restarts via S3 sync
AGENTS

echo "[openclaw-agentcore] Workspace files written."

# --- 5. Start background S3 workspace sync ---
sync_workspace_to_s3() {
    if [ -n "${WORKSPACE_BUCKET:-}" ]; then
        aws s3 sync "${WORKSPACE_DIR}/" "s3://${WORKSPACE_BUCKET}/workspace/" \
            --region "${AWS_REGION:-us-west-2}" --quiet 2>/dev/null \
            && echo "[openclaw-agentcore] Workspace synced to S3" \
            || echo "[openclaw-agentcore] WARNING: S3 workspace sync failed"
    fi
}

if [ -n "${WORKSPACE_BUCKET:-}" ]; then
    echo "[openclaw-agentcore] Starting background workspace sync (every 60s)..."
    (
        while true; do
            sleep 60
            sync_workspace_to_s3
        done
    ) &
    SYNC_PID=$!
fi

# --- 6. Start OpenClaw gateway (port 18789) ---
# Run in foreground but trap SIGTERM for clean S3 sync before exit.
cleanup() {
    echo "[openclaw-agentcore] Received shutdown signal, syncing workspace..."
    sync_workspace_to_s3
    # Forward signal to OpenClaw
    if [ -n "${OPENCLAW_PID:-}" ]; then
        kill -TERM "${OPENCLAW_PID}" 2>/dev/null
        wait "${OPENCLAW_PID}" 2>/dev/null
    fi
    exit 0
}
trap cleanup SIGTERM SIGINT

echo "[openclaw-agentcore] Starting OpenClaw gateway..."
openclaw gateway run --port 18789 --bind lan --allow-unconfigured --verbose &
OPENCLAW_PID=$!
wait "${OPENCLAW_PID}"
