# AgentCore Runtime requires ARM64
FROM --platform=linux/arm64 node:22-slim

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl jq git python3 python3-pip && \
    rm -rf /var/lib/apt/lists/*

# Install AWS CLI (for Secrets Manager in entrypoint)
RUN pip3 install --no-cache-dir --break-system-packages awscli

# Install OpenClaw and ClawHub CLI globally
RUN npm install -g openclaw@latest clawhub@latest

# Create app directory
RUN mkdir -p /app

# Pre-install Tier 1+2 skills from ClawHub
# DISABLED: clawhub fetch fails under QEMU ARM64 cross-platform emulation on macOS.
# Re-enable when building on native ARM64 (e.g. EC2 Graviton or CI).
# --force needed because some skills trigger VirusTotal flags for external API calls
# RUN clawhub install duckduckgo-search --no-input --force && \
#     clawhub install jina-reader --no-input --force && \
#     clawhub install telegram-compose --no-input --force && \
#     clawhub install transcript --no-input --force && \
#     clawhub install deep-research-pro --no-input --force && \
#     clawhub install hackernews --no-input --force && \
#     clawhub install news-feed --no-input --force && \
#     clawhub install task-decomposer --no-input --force && \
#     clawhub install cron-mastery --no-input --force

WORKDIR /app

# Install AWS SDK and WebSocket library (used by proxy adapter and contract server)
RUN cd /app && npm init -y > /dev/null 2>&1 && \
    npm install @aws-sdk/client-bedrock-runtime \
                @aws-sdk/client-cognito-identity-provider \
                @aws-sdk/client-s3 \
                @aws-sdk/client-secrets-manager \
                @aws-sdk/client-scheduler \
                @aws-sdk/client-dynamodb \
                @aws-sdk/lib-dynamodb \
                ws

# Ensure skills can resolve npm packages from /app/node_modules
ENV NODE_PATH=/app/node_modules

# Copy application files
COPY agentcore-contract.js /app/agentcore-contract.js
COPY agentcore-proxy.js /app/agentcore-proxy.js
COPY workspace-sync.js /app/workspace-sync.js
COPY force-ipv4.js /app/force-ipv4.js

# Copy custom per-user file storage skill
# /skills/ — scanned via extraDirs config (skills also copied to home dir during user setup below)
COPY skills/s3-user-files /skills/s3-user-files
RUN chmod +x /skills/s3-user-files/*.js

# Copy EventBridge cron scheduling skill
COPY skills/eventbridge-cron /skills/eventbridge-cron
RUN chmod +x /skills/eventbridge-cron/*.js

# Copy project instructions (CLAUDE.md) so the agent knows about its skills
COPY CLAUDE.md /app/CLAUDE.md

# Copy entrypoint
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Create non-root user for running the application
# AgentCore Runtime requires port 8080
EXPOSE 8080

# AgentCore Runtime uses /ping for health checks (not Docker HEALTHCHECK)
# No HEALTHCHECK directive needed — AgentCore manages this via /ping endpoint
# Note: container runs as root inside a per-user AgentCore microVM (already isolated).

ENTRYPOINT ["/app/entrypoint.sh"]
