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

# Create app directory and config directory
RUN mkdir -p /app /root/.openclaw/skills

# Pre-install Tier 1+2 skills from ClawHub
# --force needed because some skills trigger VirusTotal flags for external API calls
RUN clawhub install duckduckgo-search --no-input --force && \
    clawhub install jina-reader --no-input --force && \
    clawhub install openclaw-mem --no-input --force && \
    clawhub install telegram-compose --no-input --force && \
    clawhub install transcript --no-input --force && \
    clawhub install deep-research-pro --no-input --force && \
    clawhub install hackernews --no-input --force && \
    clawhub install news-feed --no-input --force && \
    clawhub install task-decomposer --no-input --force && \
    clawhub install cron-mastery --no-input --force

WORKDIR /app

# Install AWS SDK for Bedrock Runtime and Cognito (used by the proxy adapter)
RUN cd /app && npm init -y > /dev/null 2>&1 && \
    npm install @aws-sdk/client-bedrock-runtime \
                @aws-sdk/client-cognito-identity-provider \
                @aws-sdk/client-bedrock-agentcore

# Copy application files
COPY agentcore-contract.js /app/agentcore-contract.js
COPY agentcore-proxy.js /app/agentcore-proxy.js
COPY force-ipv4.js /app/force-ipv4.js

# Copy entrypoint
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# AgentCore Runtime requires port 8080
EXPOSE 8080

# AgentCore Runtime uses /ping for health checks (not Docker HEALTHCHECK)
# No HEALTHCHECK directive needed — AgentCore manages this via /ping endpoint

ENTRYPOINT ["/app/entrypoint.sh"]
