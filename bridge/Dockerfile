# ============================================================
# Stage 1: Builder — install all dependencies and tools
# ============================================================
FROM --platform=linux/arm64 public.ecr.aws/docker/library/node:24.15.0-slim@sha256:4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d AS builder

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends git python3 && \
    rm -rf /var/lib/apt/lists/*

# The approved 2026.7.2 runtime is an unreleased source snapshot. Fetch the
# audited commit directly, verify its declared version, and build with its
# integrity-pinned package manager and frozen lockfile.
ARG OPENCLAW_VERSION=2026.7.2
ARG OPENCLAW_SOURCE_COMMIT=4bfaccafd62ac2ff2e70ca1decc40fb1297ab438
RUN git init /opt/openclaw && \
    cd /opt/openclaw && \
    git remote add origin https://github.com/openclaw/openclaw.git && \
    git fetch --depth 1 origin "$OPENCLAW_SOURCE_COMMIT" && \
    git checkout --detach FETCH_HEAD && \
    test "$(git rev-parse HEAD)" = "$OPENCLAW_SOURCE_COMMIT" && \
    test "$(node -p 'require("./package.json").version')" = "$OPENCLAW_VERSION" && \
    corepack enable && \
    corepack prepare "$(node -p 'require("./package.json").packageManager')" --activate && \
    pnpm install --frozen-lockfile && \
    pnpm build && \
    pnpm ui:build && \
    test -f /opt/openclaw/dist/backup-DE9-5vmG.js && \
    rm -rf /opt/openclaw/.git

# Create app directory
RUN mkdir -p /app

WORKDIR /app

# Install the bridge's exact locked production dependency graph.
COPY package.json package-lock.json /app/
RUN npm ci --omit=dev

# Pre-warm V8 compile cache for heavy AWS SDK modules.
# This populates the bytecode cache at build time so runtime module loading is faster.
RUN mkdir -p /app/.compile-cache && \
    NODE_COMPILE_CACHE=/app/.compile-cache node -e " \
      require('@aws-sdk/client-bedrock-runtime'); \
      require('@aws-sdk/client-s3'); \
      require('@aws-sdk/client-lambda'); \
      require('typebox'); \
      require('ws'); \
      console.log('[build] V8 compile cache pre-warmed'); \
    "

# ============================================================
# Stage 2: Runtime — minimal image with only needed artifacts
# ============================================================
FROM --platform=linux/arm64 public.ecr.aws/docker/library/node:24.15.0-slim@sha256:4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d

# Install only runtime system dependencies (no git, no pip, no build tools)
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 && \
    rm -rf /var/lib/apt/lists/*

# Copy the verified source build and expose its CLI without a mutable registry
# install. Relative imports continue to resolve inside /opt/openclaw.
COPY --from=builder /opt/openclaw /opt/openclaw
RUN ln -s /opt/openclaw/openclaw.mjs /usr/local/bin/openclaw

# Copy app directory (node_modules, compile cache)
COPY --from=builder /app/node_modules /app/node_modules
COPY --from=builder /app/package.json /app/package.json
COPY --from=builder /app/.compile-cache /app/.compile-cache
RUN ln -s /opt/openclaw /app/node_modules/openclaw

# Ensure skills can resolve npm packages from /app/node_modules
ENV NODE_PATH=/app/node_modules
ENV AWS_REGION=eu-west-1 AWS_DEFAULT_REGION=eu-west-1

WORKDIR /app

# The mounted workspace is restored before either child starts. The first-user
# seed is image-owned and immutable; generated config remains outside it.
RUN mkdir -p /opt/personal-operator/seed/workspace /run/personal-operator && \
    chmod 0700 /run/personal-operator && \
    chmod -R a-w /opt/personal-operator/seed

# Copy application files
COPY agentcore-contract.js /app/agentcore-contract.js
COPY agentcore-proxy.js /app/agentcore-proxy.js
COPY lightweight-agent.js /app/lightweight-agent.js
COPY runtime-policy.js /app/runtime-policy.js
COPY capability-catalog.js /app/capability-catalog.js
COPY capability-relay.js /app/capability-relay.js
COPY capabilities/catalog-v1.json /app/capabilities/catalog-v1.json
COPY capabilities/schemas /app/capabilities/schemas
COPY gateway-invocation.js /app/gateway-invocation.js
COPY session-binding.js /app/session-binding.js
COPY invocation-handler.js /app/invocation-handler.js
COPY workspace-path-policy.js /app/workspace-path-policy.js
COPY workspace-manifest.js /app/workspace-manifest.js
COPY sqlite-snapshot.js /app/sqlite-snapshot.js
COPY workspace-sync.js /app/workspace-sync.js
COPY workspace-lifecycle.js /app/workspace-lifecycle.js
COPY workspace-s3-client.js /app/workspace-s3-client.js
COPY cloudwatch-logger.js /app/cloudwatch-logger.js
COPY scoped-credentials.js /app/scoped-credentials.js
COPY force-ipv4.js /app/force-ipv4.js

# Copy the only reviewed OpenClaw plugin. No executable skill tree or mutable
# marketplace content enters the image.
COPY plugins/personal-operator /app/plugins/personal-operator

# Copy entrypoint
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# AgentCore Runtime requires port 8080
EXPOSE 8080

# AgentCore Runtime uses /ping for health checks (not Docker HEALTHCHECK)
# No HEALTHCHECK directive needed — AgentCore manages this via /ping endpoint
# Note: container runs as root inside a per-user AgentCore microVM (already isolated).

ENTRYPOINT ["/app/entrypoint.sh"]
