# ============================================================
# Stage 1: Builder — install all dependencies and tools
# ============================================================
FROM --platform=linux/arm64 public.ecr.aws/docker/library/node:24.21.0-slim@sha256:ba6b7d0ee561d9d49deb99e2b93c98632cc287b6cca1a6b18d66127054efa4dc AS builder

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
COPY patch-openclaw.js patch-openclaw.test.js /tmp/openclaw-patch/
COPY patch-openclaw-preinit.js patch-openclaw-preinit.test.js /tmp/openclaw-patch/
COPY patch-openclaw-snapshot.js patch-openclaw-snapshot.test.js /tmp/openclaw-patch/
COPY patches/runtime-v2.json /tmp/openclaw-patch/patches/runtime-v2.json
COPY openclaw-preinit.mjs gateway-invariant-prewarm.mjs scoped-credentials.js /tmp/openclaw-patch/
# Omit the empty subagent status fact at its source; keep active/completed status.
RUN npm install -g openclaw@2026.9.5 clawhub@0.23.3 --allow-scripts=openclaw && \
    OPENCLAW_PACKAGE_DIR=/usr/local/lib/node_modules/openclaw \
      node --test /tmp/openclaw-patch/patch-openclaw.test.js \
        /tmp/openclaw-patch/patch-openclaw-preinit.test.js \
        /tmp/openclaw-patch/patch-openclaw-snapshot.test.js && \
    node /tmp/openclaw-patch/patch-openclaw.js /usr/local/lib/node_modules/openclaw && \
    node /tmp/openclaw-patch/patch-openclaw-preinit.js /usr/local/lib/node_modules/openclaw && \
    node /tmp/openclaw-patch/patch-openclaw-snapshot.js /usr/local/lib/node_modules/openclaw

# Fail the build if this Node image cannot run the pinned gateway and SQLite.
RUN node --input-type=module -e " \
      import { detectCurrentSqliteCapabilities, nodeRuntimeFailure } \
        from '/usr/local/lib/node_modules/openclaw/node-sqlite.mjs'; \
      const failure = nodeRuntimeFailure(process.versions.node, await detectCurrentSqliteCapabilities()); \
      if (failure) throw new Error(failure); \
    "

# Install ClawHub community skills (5 high-value marketplace skills)
# Removed duckduckgo-search (redundant with OpenClaw's web_search),
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

# Install the locked bridge dependencies without downloading a browser.
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
COPY package.json package-lock.json /app/
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund

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
FROM --platform=linux/arm64 public.ecr.aws/docker/library/node:24.21.0-slim@sha256:ba6b7d0ee561d9d49deb99e2b93c98632cc287b6cca1a6b18d66127054efa4dc

# Install only runtime system dependencies (no git, no pip, no build tools).
# node:24.21.0-slim ships no CA bundle and --no-install-recommends drops the one
# curl would otherwise pull in, so ca-certificates must be listed explicitly or
# every `curl https://` inside the container fails (rc=77, "error setting
# certificate file: /etc/ssl/certs/ca-certificates.crt"). Node's fetch uses its
# own bundled roots and is unaffected, which is why this went unnoticed. The
# final test fails the build if a future edit drops the package again.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl jq python3 && \
    rm -rf /var/lib/apt/lists/* && \
    test -f /etc/ssl/certs/ca-certificates.crt

# Require Bash's builtin clock for the entrypoint producer timestamp.
RUN /bin/bash -c '[[ ${EPOCHREALTIME:-} =~ ^[0-9]{10}\.[0-9]{6}$ ]]'

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
COPY --from=builder /app/package-lock.json /app/package-lock.json
COPY --from=builder /app/.compile-cache /app/.compile-cache

# Ensure skills can resolve npm packages from /app/node_modules.
# Skill scripts live in /skills/<name>/ — a sibling of /app, not a descendant —
# so Node's parent-directory walk (/skills/<name>/node_modules, /skills/node_modules,
# /node_modules) never reaches /app/node_modules on its own. NODE_PATH covers
# processes we spawn ourselves (proxy), but OpenClaw's exec
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
COPY workspace-sync.js /app/workspace-sync.js
COPY state-storage.js /app/state-storage.js
COPY legacy-session-import.js /app/legacy-session-import.js
COPY cloudwatch-logger.js /app/cloudwatch-logger.js
COPY startup-timing.js /app/startup-timing.js
COPY scoped-credentials.js /app/scoped-credentials.js
COPY force-ipv4.js /app/force-ipv4.js
COPY cognito-token.js /app/cognito-token.js
COPY guardrail-scope.js /app/guardrail-scope.js
COPY tool-history.js /app/tool-history.js
COPY system-messages.js /app/system-messages.js
COPY inference-config.js /app/inference-config.js
COPY request-metadata.js /app/request-metadata.js
COPY read-body.js /app/read-body.js
COPY chat-run-filter.js /app/chat-run-filter.js
COPY gateway-mcp.js /app/gateway-mcp.js
COPY runtime-skills.js /app/runtime-skills.js
COPY runtime-lifecycle.js /app/runtime-lifecycle.js
COPY gateway-lifecycle.js /app/gateway-lifecycle.js
COPY gateway-config.js live-seed-bridge.js model-policy.js invocation-stream.js cron-jobs.js /app/
COPY proxy-sdk-snapshot.js proxy-auth-prepare.js system-prefix-cache.js /app/
COPY openclaw-worker-owner.js /app/openclaw-worker-owner.js
COPY openclaw-preinit.mjs /app/openclaw-preinit.mjs
COPY live-gateway-seed-owner.mjs live-gateway-seed-child.mjs live-gateway-seed-common.mjs /app/
COPY session-diff-worker-prewarm.mjs session-diff-worker-bootstrap.mjs /app/
COPY sqlite-receiver-control.mjs sqlite-receiver-reservation.mjs sqlite-backend-prepare.mjs /app/
COPY gateway-invariant-prewarm.mjs /app/gateway-invariant-prewarm.mjs
COPY patches/runtime-v2.json /app/patches/runtime-v2.json

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

# Record the final application bytes after all functional patches and copies.
RUN node <<'NODE'
const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { execFileSync } = require("node:child_process");
const vendor = "/usr/local/lib/node_modules/openclaw/";
const files = new Map();
function pin(file, expected) {
  const stat = fs.lstatSync(file);
  if (!stat.isFile()) throw new Error("AgentCore Runtime source must be a regular file: " + file);
  const sha256 = createHash("sha256").update(fs.readFileSync(file)).digest("hex");
  if (expected && sha256 !== expected) throw new Error("AgentCore Runtime source mismatch: " + file);
  files.set(file, { path: file, sha256, mode: stat.mode & 0o777 });
}
for (const name of fs.readdirSync("/app").sort()) {
  if (!/\.(js|mjs|json|sh|md)$/.test(name)) continue;
  const file = path.join("/app", name);
  pin(file);
  if (/\.(js|mjs)$/.test(name)) execFileSync(process.execPath, ["--check", file]);
}
pin("/app/patches/runtime-v2.json");
const patch = JSON.parse(fs.readFileSync("/app/patches/runtime-v2.json", "utf8"));
for (const [file, expected] of Object.entries(patch.requires)) pin(vendor + file, expected);
for (const entry of patch.files) pin(vendor + entry.file, entry.patchedSha256);
const child = fs.readFileSync("/app/live-gateway-seed-child.mjs", "utf8");
const list = child.match(/export const CHAT_PREPARE_MODULES = Object\.freeze\(([\s\S]*?)\.map\(Object\.freeze\)\);/);
if (!list) throw new Error("Missing chat preparation pins");
const matches = [...list[1].matchAll(/\["([^"\n]+)", "([a-f0-9]{64})"\]/g)];
if (!matches.length) throw new Error("Empty chat preparation pins");
for (const [, file, expected] of matches) pin(vendor + "dist/" + file, expected);
pin("/app/node_modules/@aws-sdk/client-bedrock-runtime/dist-cjs/index.js",
  "2d0c2e259d5ea7b483ee5b9dc301ac85f85f9a612074f1c5a5d1ee9be8887682");
fs.writeFileSync("/app/runtime-source-manifest.json", JSON.stringify({
  node: process.version, platform: process.platform, arch: process.arch,
  openclaw: JSON.parse(fs.readFileSync(vendor + "package.json", "utf8")).version,
  files: [...files.values()].sort((a, b) => a.path.localeCompare(b.path)),
}, null, 2) + "\n", { flag: "wx" });
NODE

# AgentCore Runtime requires port 8080
EXPOSE 8080

# AgentCore Runtime uses /ping for health checks (not Docker HEALTHCHECK)
# No HEALTHCHECK directive needed — AgentCore manages this via /ping endpoint
# Note: container runs as root inside a per-user AgentCore microVM (already isolated).

ENTRYPOINT ["/app/entrypoint.sh"]
