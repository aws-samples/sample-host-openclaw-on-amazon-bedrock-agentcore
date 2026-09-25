# ============================================================
# Stage 1: Builder — install all dependencies and tools
# ============================================================
FROM --platform=linux/arm64 public.ecr.aws/docker/library/node:24-slim AS builder

# Install system dependencies. ca-certificates is listed explicitly: with
# --no-install-recommends only python3-pip pulls it in (git and libcurl4 merely
# Recommend it), so it would silently vanish if the pip install ever went away.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl jq git python3 python3-pip && \
    rm -rf /var/lib/apt/lists/*

# Install AWS CLI (for Secrets Manager in entrypoint)
RUN pip3 install --no-cache-dir --break-system-packages awscli

# Install OpenClaw and ClawHub CLI globally
# Pinned to tested versions — update intentionally to avoid unexpected breaking changes.
# To upgrade: test the new version locally first, then bump both pins here.
#   openclaw release notes: https://github.com/openclaw/openclaw/releases
RUN npm install -g openclaw@2026.9.5 clawhub@0.23.3 --allow-scripts=openclaw

# Install ClawHub community skills (5 high-value marketplace skills)
# Removed duckduckgo-search (redundant — lightweight agent has built-in web_search),
# hackernews (niche), news-feed (niche) to reduce startup time.
# --no-input --force: non-interactive, skip VirusTotal warnings
#
# deep-research-pro, transcript and task-decomposer are installed as qualified
# @owner/slug specs: ClawHub now hosts duplicate slugs for all three and
# clawhub >= 0.23 refuses an ambiguous bare slug (the old retry loop then
# silently produced an image with 2/5 skills). Owners and pinned versions are
# the ones baked into the previously shipped image (/skills/<slug>/_meta.json
# and .clawhub/origin.json). clawhub installs a qualified spec into
# /skills/@owner/<slug>; flatten to /skills/<slug> because the runtime config
# (extraDirs), the clawhub-manage skill and the system prompt all address
# skills as /skills/<slug>. A missing skill fails the build.
RUN set -e; \
    install_skill() { spec="$1"; slug="$2"; ver="$3"; \
      if [ -n "$ver" ]; then set -- "$spec" --version "$ver"; else set -- "$spec"; fi; \
      for attempt in 1 2 3 4 5; do \
        clawhub install "$@" --no-input --force && break; \
        [ "$attempt" = 5 ] && { echo "FAILED: could not install $spec"; exit 1; }; \
        echo "Retry $attempt for $spec (waiting 15s)..."; \
        sleep 15; \
      done; \
      case "$spec" in @*/*) owner_dir="/skills/${spec%/*}"; \
        mv "$owner_dir/$slug" "/skills/$slug" && rmdir "$owner_dir" ;; esac; \
      test -d "/skills/$slug" && test -f "/skills/$slug/SKILL.md" \
        || { echo "FAILED: /skills/$slug missing after install of $spec"; exit 1; }; \
    }; \
    install_skill jina-reader                 jina-reader       ""; \
    install_skill @parags/deep-research-pro   deep-research-pro 1.0.2; \
    install_skill telegram-compose            telegram-compose  ""; \
    install_skill @therohitdas/transcript     transcript        1.4.1; \
    install_skill @10e9928a/task-decomposer   task-decomposer   1.0.0; \
    echo "Installed ClawHub skills:"; ls -1 /skills

# Create app directory
RUN mkdir -p /app

WORKDIR /app

# Install AWS SDK and WebSocket library (production only)
# Skip browser binary download — we only need playwright-core for CDP client (connectOverCDP)
#
# @smithy/signature-v4, @aws-sdk/credential-provider-node and
# @aws-crypto/sha256-js are require()d directly by agentcore-contract.js
# (signBrowserEndpoint: SigV4 for the AgentCore Browser CDP WebSocket). They
# used to arrive as transitive dependencies of the SDK clients; recent SDK
# releases dropped @aws-crypto/sha256-js, so the browser skill failed with
# "Cannot find module '@aws-crypto/sha256-js'". Declare all three explicitly.
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
RUN cd /app && npm init -y > /dev/null 2>&1 && \
    npm install --omit=dev \
                @aws-sdk/client-bedrock-agentcore \
                @aws-sdk/client-bedrock-runtime \
                @aws-sdk/client-cognito-identity-provider \
                @aws-sdk/client-s3 \
                @aws-sdk/client-secrets-manager \
                @aws-sdk/client-scheduler \
                @aws-sdk/client-dynamodb \
                @aws-sdk/client-sts \
                @aws-sdk/lib-dynamodb \
                playwright-core \
                @aws-sdk/s3-request-presigner \
                @aws-sdk/client-cloudwatch-logs \
                @aws-sdk/credential-provider-node \
                @smithy/signature-v4 \
                @aws-crypto/sha256-js@5.2.0 \
                ws

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
      require('@aws-sdk/client-sts'); \
      require('@aws-sdk/lib-dynamodb'); \
      require('@aws-sdk/credential-provider-node'); \
      require('@smithy/signature-v4'); \
      require('@aws-crypto/sha256-js'); \
      require('ws'); \
      console.log('[build] V8 compile cache pre-warmed'); \
    "

# ============================================================
# Stage 2: Runtime — minimal image with only needed artifacts
# ============================================================
FROM --platform=linux/arm64 public.ecr.aws/docker/library/node:24-slim

# Install only runtime system dependencies (no git, no pip, no build tools).
# node:24-slim ships no CA bundle and --no-install-recommends drops the one
# curl would otherwise pull in, so ca-certificates must be listed explicitly or
# every `curl https://` inside the container fails (rc=77, "error setting
# certificate file: /etc/ssl/certs/ca-certificates.crt"). Node's fetch uses its
# own bundled roots and is unaffected, which is why this went unnoticed. The
# final test fails the build if a future edit drops the package again.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl jq python3 && \
    rm -rf /var/lib/apt/lists/* && \
    test -f /etc/ssl/certs/ca-certificates.crt

# Copy AWS CLI from builder
COPY --from=builder /usr/local/bin/aws /usr/local/bin/aws
COPY --from=builder /usr/local/lib/python3.*/dist-packages /usr/local/lib/python3.11/dist-packages
COPY --from=builder /usr/lib/python3/dist-packages /usr/lib/python3/dist-packages

# Copy OpenClaw and ClawHub from builder (global npm modules + binaries)
COPY --from=builder /usr/local/lib/node_modules /usr/local/lib/node_modules
# Re-create symlinks instead of COPY (which resolves symlinks and breaks
# relative imports like ./dist/entry.js in the openclaw ESM binary)
RUN ln -s /usr/local/lib/node_modules/openclaw/openclaw.mjs /usr/local/bin/openclaw && \
    ln -s /usr/local/lib/node_modules/clawhub/bin/clawdhub.js /usr/local/bin/clawhub

# Copy ClawHub installed skills (clawhub installs to /skills/ in builder)
COPY --from=builder /skills/ /skills/

# Copy app directory (node_modules, compile cache)
COPY --from=builder /app/node_modules /app/node_modules
COPY --from=builder /app/package.json /app/package.json
COPY --from=builder /app/.compile-cache /app/.compile-cache

# Ensure skills can resolve npm packages from /app/node_modules.
# Skill scripts live in /skills/<name>/ — a sibling of /app, not a descendant —
# so Node's parent-directory walk (/skills/<name>/node_modules, /skills/node_modules,
# /node_modules) never reaches /app/node_modules on its own. NODE_PATH covers
# processes we spawn ourselves (lightweight-agent, proxy), but OpenClaw's exec
# tool (host=gateway) strips NODE_PATH and NODE_OPTIONS from the child env as a
# hardening measure (openclaw src/infra/host-env-security-policy.json), so
# `node /skills/<name>/<script>.js` run by the agent would otherwise fail with
# MODULE_NOT_FOUND. The symlink puts /app/node_modules on the standard walk so
# resolution works with no env var at all. OpenClaw's skill scanner skips
# entries named node_modules, so this is invisible to skill discovery.
ENV NODE_PATH=/app/node_modules
RUN mkdir -p /skills && ln -s /app/node_modules /skills/node_modules

WORKDIR /app

# Pre-create OpenClaw workspace directory (avoids mkdir at runtime)
RUN mkdir -p /root/.openclaw

# Copy application files
COPY agentcore-contract.js /app/agentcore-contract.js
COPY agentcore-proxy.js /app/agentcore-proxy.js
COPY lightweight-agent.js /app/lightweight-agent.js
COPY workspace-sync.js /app/workspace-sync.js
COPY state-storage.js /app/state-storage.js
COPY cloudwatch-logger.js /app/cloudwatch-logger.js
COPY scoped-credentials.js /app/scoped-credentials.js
COPY force-ipv4.js /app/force-ipv4.js
COPY cognito-token.js /app/cognito-token.js
COPY gateway-mcp.js /app/gateway-mcp.js
COPY runtime-skills.js /app/runtime-skills.js

# Copy custom per-user file storage skill
# /skills/ — scanned via extraDirs config (skills also copied to home dir during user setup below)
COPY skills/s3-user-files /skills/s3-user-files
RUN chmod +x /skills/s3-user-files/*.js

# Copy EventBridge cron scheduling skill
COPY skills/eventbridge-cron /skills/eventbridge-cron
RUN chmod +x /skills/eventbridge-cron/*.js

# Copy ClawHub skill management skill. runtime-skills.js (manifest of
# user-installed skills + cold-start reinstall) is shared with
# agentcore-contract.js; the repo keeps a require() shim at
# skills/clawhub-manage/runtime-skills.js, replaced here by the real module so
# the skill scripts are self-contained under /skills.
COPY skills/clawhub-manage /skills/clawhub-manage
COPY runtime-skills.js /skills/clawhub-manage/runtime-skills.js
RUN chmod +x /skills/clawhub-manage/*.js

# Copy API key management skill (dual-mode: native file + Secrets Manager)
COPY skills/api-keys /skills/api-keys
RUN chmod +x /skills/api-keys/*.js

# Copy AgentCore browser skill (headless Chromium browsing)
COPY skills/agentcore-browser /skills/agentcore-browser
RUN chmod +x /skills/agentcore-browser/*.js

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
