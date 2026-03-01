# ============================================================
# Stage 1: Builder — install all dependencies and tools
# ============================================================
FROM --platform=linux/arm64 public.ecr.aws/docker/library/node:22-slim AS builder

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl jq git python3 python3-pip && \
    rm -rf /var/lib/apt/lists/*

# Install AWS CLI (for Secrets Manager in entrypoint)
RUN pip3 install --no-cache-dir --break-system-packages awscli

# Install OpenClaw and ClawHub CLI globally
RUN npm install -g openclaw@latest clawhub@latest

# Install ClawHub community skills (5 high-value marketplace skills)
# Each in a separate RUN so Docker layer cache survives partial ClawHub rate-limit failures.
# --no-input --force: non-interactive, skip VirusTotal warnings
RUN clawhub install jina-reader --no-input --force
RUN clawhub install deep-research-pro --no-input --force
RUN clawhub install telegram-compose --no-input --force
RUN clawhub install transcript --no-input --force
RUN clawhub install task-decomposer --no-input --force

# Install Claude Code CLI globally (required by @anthropic-ai/claude-agent-sdk's
# query() which spawns `claude` as a subprocess for agentic loops).
# Installed AFTER ClawHub skills to preserve their Docker layer cache.
RUN npm install -g @anthropic-ai/claude-code@latest

# Create app directory
RUN mkdir -p /app

WORKDIR /app

# Install AWS SDK, WebSocket library, Claude Agent SDK, and Zod (production only)
RUN cd /app && npm init -y > /dev/null 2>&1 && \
    npm install --omit=dev \
                @aws-sdk/client-bedrock-runtime \
                @aws-sdk/client-cognito-identity-provider \
                @aws-sdk/client-s3 \
                @aws-sdk/client-secrets-manager \
                @aws-sdk/client-scheduler \
                @aws-sdk/client-dynamodb \
                @aws-sdk/lib-dynamodb \
                @anthropic-ai/claude-agent-sdk \
                zod \
                ws

# Install Python MCP server library and boto3 for cost analyzer AWS MCP server
RUN pip3 install --no-cache-dir --break-system-packages mcp boto3

# Pre-warm V8 compile cache for heavy AWS SDK modules.
# This populates the bytecode cache at build time so runtime module loading is faster.
RUN mkdir -p /app/.compile-cache && \
    NODE_COMPILE_CACHE=/app/.compile-cache node -e " \
      require('@aws-sdk/client-bedrock-runtime'); \
      require('@aws-sdk/client-cognito-identity-provider'); \
      require('@aws-sdk/client-s3'); \
      require('@aws-sdk/client-secrets-manager'); \
      require('@aws-sdk/client-scheduler'); \
      require('@aws-sdk/client-dynamodb'); \
      require('@aws-sdk/lib-dynamodb'); \
      require('ws'); \
      console.log('[build] V8 compile cache pre-warmed'); \
    "

# ============================================================
# Stage 2: Runtime — minimal image with only needed artifacts
# ============================================================
FROM --platform=linux/arm64 public.ecr.aws/docker/library/node:22-slim

# Install runtime system dependencies (git needed by Claude Agent SDK for cost analyzer agent)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl jq python3 git && \
    rm -rf /var/lib/apt/lists/*

# Copy AWS CLI from builder
COPY --from=builder /usr/local/bin/aws /usr/local/bin/aws
COPY --from=builder /usr/local/lib/python3.*/dist-packages /usr/local/lib/python3.11/dist-packages
COPY --from=builder /usr/lib/python3/dist-packages /usr/lib/python3/dist-packages

# Copy OpenClaw and ClawHub from builder (global npm modules + binaries)
COPY --from=builder /usr/local/lib/node_modules /usr/local/lib/node_modules
# Re-create symlinks instead of COPY (which resolves symlinks and breaks
# relative imports like ./dist/entry.js in the openclaw ESM binary)
RUN ln -s /usr/local/lib/node_modules/openclaw/openclaw.mjs /usr/local/bin/openclaw && \
    ln -s /usr/local/lib/node_modules/clawhub/bin/clawdhub.js /usr/local/bin/clawhub && \
    ln -s /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js /usr/local/bin/claude

# Copy ClawHub installed skills (clawhub installs to /skills/ in builder)
COPY --from=builder /skills/ /skills/

# Copy app directory (node_modules, compile cache)
COPY --from=builder /app/node_modules /app/node_modules
COPY --from=builder /app/package.json /app/package.json
COPY --from=builder /app/.compile-cache /app/.compile-cache

# Ensure skills can resolve npm packages from /app/node_modules
ENV NODE_PATH=/app/node_modules

WORKDIR /app

# Pre-create OpenClaw workspace directory (avoids mkdir at runtime)
RUN mkdir -p /root/.openclaw

# Copy application files
COPY agentcore-contract.js /app/agentcore-contract.js
COPY agentcore-proxy.js /app/agentcore-proxy.js
COPY lightweight-agent.js /app/lightweight-agent.js
COPY workspace-sync.js /app/workspace-sync.js
COPY force-ipv4.js /app/force-ipv4.js

# Copy custom per-user file storage skill
# /skills/ — scanned via extraDirs config (skills also copied to home dir during user setup below)
COPY skills/s3-user-files /skills/s3-user-files
RUN chmod +x /skills/s3-user-files/*.js

# Copy EventBridge cron scheduling skill
COPY skills/eventbridge-cron /skills/eventbridge-cron
RUN chmod +x /skills/eventbridge-cron/*.js

# Copy cost analyzer skill (only manifest + code — no extra .md files or subdirectories
# that could interfere with OpenClaw's skill scanner)
COPY skills/cost-analyzer/SKILL.md /skills/cost-analyzer/SKILL.md
COPY skills/cost-analyzer/run.js /skills/cost-analyzer/run.js
COPY skills/cost-analyzer/token-usage-server.js /skills/cost-analyzer/token-usage-server.js
COPY skills/cost-analyzer/package.json /skills/cost-analyzer/package.json
RUN chmod +x /skills/cost-analyzer/*.js

# Cost analyzer supporting files (outside /skills/ to avoid skill scanner interference)
COPY skills/cost-analyzer/system-prompt.md /app/cost-analyzer-deps/system-prompt.md
COPY skills/cost-analyzer/aws-cost-server/server.py /app/cost-analyzer-deps/aws-cost-server/server.py
RUN chmod +x /app/cost-analyzer-deps/aws-cost-server/server.py

# Copy project instructions (CLAUDE.md) so the agent knows about its skills
COPY CLAUDE.md /app/CLAUDE.md

# Copy entrypoint
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# AgentCore Runtime requires port 8080
EXPOSE 8080

# AgentCore Runtime uses /ping for health checks (not Docker HEALTHCHECK)
# No HEALTHCHECK directive needed — AgentCore manages this via /ping endpoint
# Note: container runs as root inside a per-user AgentCore microVM (already isolated).

ENTRYPOINT ["/app/entrypoint.sh"]
