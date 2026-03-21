/**
 * AgentCore Runtime Contract Server — Per-User Sessions
 *
 * Implements the required HTTP protocol contract for AgentCore Runtime:
 *   - GET  /ping         -> Health check (Healthy — allows idle termination)
 *   - POST /invocations  -> Chat handler with hybrid init
 *
 * Each AgentCore session is dedicated to a single user. On first invocation:
 *   1. Use pre-fetched secrets (fetched eagerly at boot)
 *   2. Start proxy + OpenClaw + workspace restore in parallel
 *   3. Once proxy is ready (~5s), route via lightweight agent shim
 *   4. Once OpenClaw is ready (~1-2 min), route via WebSocket bridge
 *
 * The lightweight agent handles messages immediately while OpenClaw starts.
 * Once OpenClaw is ready, all subsequent messages route through it seamlessly.
 *
 * Runs on port 8080 (required by AgentCore Runtime).
 */

const http = require("http");
const fs = require("fs");
const { spawn } = require("child_process");
const WebSocket = require("ws");
const {
  SecretsManagerClient,
  GetSecretValueCommand,
} = require("@aws-sdk/client-secrets-manager");
const workspaceSync = require("./workspace-sync");
const cwLogger = require("./cloudwatch-logger");
const agent = require("./lightweight-agent");
const scopedCreds = require("./scoped-credentials");

const PORT = 8080;
const PROXY_PORT = 18790;
const OPENCLAW_PORT = 18789;

// Gateway token — fetched from Secrets Manager eagerly at boot.
// No fallback — container will fail to authenticate WebSocket if not set.
let GATEWAY_TOKEN = null;

// Cognito password secret — fetched from Secrets Manager eagerly at boot.
// Stored in-process only, never written to process.env.
let COGNITO_PASSWORD_SECRET = null;

// Maximum request body size (1MB) to prevent memory exhaustion
const MAX_BODY_SIZE = 1 * 1024 * 1024;

// Ping diagnostics — track call count and log periodically
let pingCount = 0;
let lastPingLogTime = 0;
const PING_LOG_INTERVAL_MS = 60000; // Log ping stats every 60s

// State tracking
let currentUserId = null;
let currentNamespace = null;
let openclawProcess = null;
let proxyProcess = null;
let openclawReady = false;
let proxyReady = false;
let secretsReady = false;
let initInProgress = false;
let initPromise = null;
let secretsPrefetchPromise = null;
let startTime = Date.now();
let shuttingDown = false;
let credentialRefreshTimer = null;
let browserHeaderRefreshTimer = null;
let currentBrowserSessionId = null;
let currentBrowserEndpoint = null;
const SCOPED_CREDS_DIR = "/tmp/scoped-creds";
const IDENTITY_FILE = "/tmp/current-identity.json";
const BROWSER_SESSION_FILE = "/tmp/agentcore-browser-session.json";
const BROWSER_SESSION_TIMEOUT_SECONDS = 3600;
const BUILD_VERSION = "v40"; // Bump in cdk.json to force container redeploy

// Derive EVENTBRIDGE_ROLE_ARN from EXECUTION_ROLE_ARN + AWS_REGION if not already set.
// The agentcore toolkit doesn't support injecting arbitrary env vars into the container,
// so we derive it: arn:aws:iam::{account}:role/openclaw-cron-scheduler-role-{region}
if (!process.env.EVENTBRIDGE_ROLE_ARN && process.env.EXECUTION_ROLE_ARN) {
  const match = process.env.EXECUTION_ROLE_ARN.match(/arn:aws:iam::(\d+):role\//);
  if (match) {
    const account = match[1];
    const region = process.env.AWS_REGION || "us-west-2";
    process.env.EVENTBRIDGE_ROLE_ARN = `arn:aws:iam::${account}:role/openclaw-cron-scheduler-role-${region}`;
    console.log(`[contract] Derived EVENTBRIDGE_ROLE_ARN from execution role (account=${account}, region=${region})`);
  }
}

// OpenClaw process diagnostics (last N lines of stdout/stderr)
const OPENCLAW_LOG_LIMIT = 50;
let openclawLogs = [];
let openclawExitCode = null;
let lastOpenClawEnv = null;

// OpenClaw auto-restart on crash
let openclawRestartCount = 0;
const OPENCLAW_MAX_RESTARTS = 3;
const OPENCLAW_RESTART_DELAY_MS = 5000;

// Active task tracking — HealthyBusy prevents AgentCore from terminating during long tasks
let activeTaskCount = 0;
// Last activity timestamp (epoch seconds) — reported in /ping so AgentCore can track idle time.
// Initialized to startup time; updated on each chat/cron/warmup invocation.
let lastActivityTime = Math.floor(Date.now() / 1000);

// Message queue for serializing concurrent requests (OpenClaw WebSocket path)
let messageQueue = [];
let processingMessage = false;

/**
 * Write current actorId and channel to a shared file so the proxy process
 * can pick up cross-channel identity changes (the proxy's env vars are
 * fixed at spawn time and cannot be updated for a running child process).
 */
function updateIdentityFile(actorId, channel) {
  try {
    fs.writeFileSync(
      IDENTITY_FILE,
      JSON.stringify({ actorId, channel }),
      "utf-8",
    );
  } catch (err) {
    console.warn(`[contract] Failed to write identity file: ${err.message}`);
  }
}

/**
 * Pre-fetch secrets from Secrets Manager at container boot.
 * Runs in the background — does not block /ping health checks.
 */
async function prefetchSecrets() {
  const region = process.env.AWS_REGION || "us-west-2";
  const smClient = new SecretsManagerClient({ region });

  const gatewaySecretId = process.env.GATEWAY_TOKEN_SECRET_ID;
  if (gatewaySecretId) {
    const resp = await smClient.send(
      new GetSecretValueCommand({ SecretId: gatewaySecretId }),
    );
    if (resp.SecretString) {
      GATEWAY_TOKEN = resp.SecretString;
      console.log("[contract] Gateway token pre-fetched from Secrets Manager");
    }
  }

  const cognitoSecretId = process.env.COGNITO_PASSWORD_SECRET_ID;
  if (cognitoSecretId) {
    const resp = await smClient.send(
      new GetSecretValueCommand({ SecretId: cognitoSecretId }),
    );
    if (resp.SecretString) {
      COGNITO_PASSWORD_SECRET = resp.SecretString;
      console.log("[contract] Cognito password secret pre-fetched");
    }
  }

  secretsReady = true;
  console.log("[contract] Secrets pre-fetch complete");
}

/**
 * Clean up stale .lock files in the .openclaw directory (async, non-blocking).
 * Prevents "session file locked" errors after workspace restore from S3.
 */
async function cleanupLockFiles() {
  const fs = require("fs");
  const path = require("path");
  const homeDir = process.env.HOME || "/root";
  const openclawDir = path.join(homeDir, ".openclaw");

  try {
    await fs.promises.access(openclawDir);
  } catch {
    return; // Directory doesn't exist yet — nothing to clean
  }

  async function walkAndClean(dir) {
    let entries;
    try {
      entries = await fs.promises.readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    const tasks = [];
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        tasks.push(walkAndClean(fullPath));
      } else if (entry.name.endsWith(".lock")) {
        tasks.push(
          fs.promises.unlink(fullPath).catch(() => {}),
        );
      }
    }
    await Promise.all(tasks);
  }

  await walkAndClean(openclawDir);
  console.log("[contract] Lock file cleanup complete (async)");
}

/**
 * Check if the proxy health endpoint responds.
 */
function checkProxyHealth() {
  return new Promise((resolve) => {
    const req = http.get(`http://127.0.0.1:${PROXY_PORT}/health`, (res) => {
      let body = "";
      res.on("data", (chunk) => (body += chunk));
      res.on("end", () => {
        try {
          resolve(JSON.parse(body));
        } catch {
          resolve(null);
        }
      });
    });
    req.on("error", () => resolve(null));
    req.setTimeout(2000, () => {
      req.destroy();
      resolve(null);
    });
  });
}

/**
 * Send a lightweight request to the proxy to trigger JIT compilation
 * of the request handling path. Makes the first real user message faster.
 */
function warmProxyJit() {
  return new Promise((resolve) => {
    const payload = JSON.stringify({
      model: "bedrock-agentcore",
      messages: [{ role: "user", content: "warmup" }],
      max_tokens: 1,
      stream: false,
    });
    const req = http.request(
      {
        hostname: "127.0.0.1",
        port: PROXY_PORT,
        path: "/v1/chat/completions",
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(payload),
        },
        timeout: 10000,
      },
      (res) => {
        res.resume();
        res.on("end", () => {
          console.log("[contract] Proxy JIT warm-up complete");
          resolve();
        });
      },
    );
    req.on("error", () => resolve());
    req.on("timeout", () => {
      req.destroy();
      resolve();
    });
    req.write(payload);
    req.end();
  });
}

/**
 * Check if OpenClaw gateway port is listening.
 */
function checkOpenClawReady() {
  return new Promise((resolve) => {
    const req = http.get(`http://127.0.0.1:${OPENCLAW_PORT}`, (res) => {
      res.resume();
      resolve(true);
    });
    req.on("error", () => resolve(false));
    req.setTimeout(2000, () => {
      req.destroy();
      resolve(false);
    });
  });
}

/**
 * Wait for a port to become available, with timeout.
 */
async function waitForPort(port, label, timeoutMs = 300000, intervalMs = 3000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const ready = await new Promise((resolve) => {
      const req = http.get(`http://127.0.0.1:${port}`, (res) => {
        res.resume();
        resolve(true);
      });
      req.on("error", () => resolve(false));
      req.setTimeout(2000, () => {
        req.destroy();
        resolve(false);
      });
    });
    if (ready) {
      console.log(`[contract] ${label} is ready on port ${port}`);
      return true;
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  console.error(
    `[contract] ${label} did not become ready within ${timeoutMs / 1000}s`,
  );
  return false;
}

// Distinct subagent model name — proxy uses this to detect and route subagent requests.
// Must match the SUBAGENT_MODEL_NAME env var passed to the proxy.
const SUBAGENT_MODEL_NAME = "bedrock-agentcore-subagent";

/**
 * Write a headless OpenClaw config (no channels — messages bridged via WebSocket).
 * Full tool profile with deny list for unsafe/irrelevant tools.
 * Sub-agents enabled for deep-research-pro and task-decomposer skills.
 * Sandbox disabled — AgentCore microVMs provide per-user isolation.
 */
function writeOpenClawConfig() {
  const fs = require("fs");

  // Sub-agent model uses a distinct name so the proxy can identify subagent requests.
  // The proxy maps this name → SUBAGENT_BEDROCK_MODEL_ID (or MODEL_ID fallback).
  const subagentModel = `agentcore/${SUBAGENT_MODEL_NAME}`;

  const config = {
    models: {
      providers: {
        agentcore: {
          baseUrl: `http://127.0.0.1:${PROXY_PORT}/v1`,
          apiKey: "local",
          api: "openai-completions",
          models: [
            { id: "bedrock-agentcore", name: "Bedrock AgentCore" },
            { id: SUBAGENT_MODEL_NAME, name: "Bedrock AgentCore Subagent" },
          ],
        },
      },
    },
    agents: {
      defaults: {
        model: { primary: "agentcore/bedrock-agentcore" },
        subagents: {
          model: subagentModel,
          maxConcurrent: 2,
          runTimeoutSeconds: 900,
          archiveAfterMinutes: 60,
        },
        sandbox: {
          mode: "off", // No Docker in AgentCore container; microVMs provide isolation
        },
      },
    },
    tools: {
      profile: "full",
      exec: {
        host: "gateway",  // Run on container host — microVM provides isolation, no Docker sandbox
        security: "full", // Full shell access; container is already isolated
        ask: "off",       // Headless container — no approval UI
      },
      deny: [
        "write", // Local writes don't persist — use S3 skill instead
        "edit", // Local edits are ephemeral — use S3 skill instead
        "apply_patch", // Code patching not needed for chat assistant
        "read", // Blocks local file reads — prevents reading sibling process environ; use s3-user-files
        "browser", // Deny built-in browser tool — use agentcore-browser skill instead (via exec)
        "canvas", // No UI rendering in headless chat context
        "cron", // EventBridge handles scheduling, not OpenClaw's built-in cron
        "gateway", // Admin tool — not needed for end users
      ],
      // Note: `exec` is intentionally NOT denied — skills like clawhub-manage
      // need Bash(node:*) to run scripts. Scoped STS credentials ensure
      // OpenClaw only has access to the user's S3 namespace prefix.
    },
    skills: {
      allowBundled: [],
      load: { extraDirs: ["/skills"] },
    },
    gateway: {
      mode: "local",
      port: OPENCLAW_PORT,
      trustedProxies: ["127.0.0.1"],
      auth: { mode: "token", token: GATEWAY_TOKEN },
      controlUi: {
        enabled: false,
        allowInsecureAuth: true,
        dangerouslyDisableDeviceAuth: true,
        dangerouslyAllowHostHeaderOriginFallback: true,
        allowedOrigins: ["*"],
      },
    },
    channels: {}, // No channels — messages bridged via WebSocket
  };

  const homeDir = process.env.HOME || "/root";
  fs.mkdirSync(`${homeDir}/.openclaw`, { recursive: true });
  fs.writeFileSync(
    `${homeDir}/.openclaw/openclaw.json`,
    JSON.stringify(config, null, 2),
  );
  console.log("[contract] OpenClaw headless config written");

  // Write AGENTS.md — OpenClaw loads this as workspace bootstrap instructions.
  // Always overwrite to ensure instructions match the current container version
  // (workspace restore from S3 may carry stale AGENTS.md without new features like browser).
  const agentsMdPath = `${homeDir}/.openclaw/AGENTS.md`;
  {
    fs.writeFileSync(
      agentsMdPath,
      [
        "# Agent Instructions",
        "",
        "You are a helpful AI assistant running in a per-user container on AWS.",
        "You have built-in web tools, file storage, scheduling, and many community skills.",
        "",
        "## Response Formatting",
        "",
        "Format responses for chat messaging apps (Telegram, Slack):",
        "- **No markdown tables** — use bullet lists or plain text paragraphs instead",
        "- Tables do not render in most chat apps; bullets always work",
        "- Keep responses concise and chat-appropriate",
        "",
        "## Built-in Web Tools",
        "",
        "You have built-in **web_search** and **web_fetch** tools:",
        "- **web_search**: Search the web for current information",
        "- **web_fetch**: Fetch and read web page content as markdown",
        "",
        "Use these for real-time information, news, research, and reading web pages.",
        "",
        "## Scheduling & Cron Jobs",
        "",
        "You have the **eventbridge-cron** skill for scheduling tasks. When users ask to set up reminders,",
        "recurring tasks, or cron jobs, use these commands via Bash. Do NOT say cron is disabled.",
        "The built-in cron is replaced by Amazon EventBridge Scheduler (more reliable, persists across sessions).",
        "",
        "Always ask the user for their **timezone** if you don't know it (e.g., Asia/Shanghai, America/New_York).",
        "",
        "**Commands** (run via Bash):",
        "- Create: `node /skills/eventbridge-cron/create.js <user_id> <cron_expression> <timezone> <message> [channel] [channel_target] [schedule_name]`",
        "- List: `node /skills/eventbridge-cron/list.js <user_id>`",
        "- Update: `node /skills/eventbridge-cron/update.js <user_id> <schedule_id> [--expression \"cron(...)\"] [--timezone \"TZ\"] [--message \"msg\"] [--enable] [--disable]`",
        "- Delete: `node /skills/eventbridge-cron/delete.js <user_id> <schedule_id>`",
        "",
        "Cron format: `cron(min hour day-of-month month day-of-week year)` — e.g., `cron(0 9 * * ? *)` for daily at 9 AM.",
        "Rate format: `rate(1 hour)`, `rate(5 minutes)`.",
        "",
        "## File Storage",
        "",
        "You have the **s3-user-files** skill for persistent file storage. Files survive across sessions.",
        "",
        "## Community Skills (ClawHub)",
        "",
        "The following community skills are pre-installed:",
        "- **jina-reader**: Extract web content as clean markdown (higher quality than built-in web_fetch)",
        "- **deep-research-pro**: In-depth multi-step research on complex topics (uses sub-agents)",
        "- **telegram-compose**: Rich HTML formatting for Telegram messages",
        "- **transcript**: YouTube video transcript extraction",
        "- **task-decomposer**: Break complex requests into manageable subtasks (uses sub-agents)",
        "",
        "### Installing More Skills",
        "",
        "You have the **clawhub-manage** skill to install/uninstall additional community skills from the ClawHub marketplace.",
        "When a user asks to install or add a skill, use this skill — do NOT say it's not possible or that exec is blocked.",
        "**Use Bash to run the skill scripts** (Bash is available, only exec is denied):",
        "- Install: `node /skills/clawhub-manage/install.js <skill-name>`",
        "- Uninstall: `node /skills/clawhub-manage/uninstall.js <skill-name>`",
        "- List: `node /skills/clawhub-manage/list.js`",
        "",
        "After install/uninstall, the skill will be available on the next session start (after idle timeout or new conversation).",
        "",
        "## API Key Storage",
        "",
        "You have the **api-keys** skill for secure API key storage.",
        "",
        "### Proactive Detection",
        "",
        "If a user message contains what looks like an API key or secret token — even without explicitly asking to save it — you MUST proactively offer to store it securely. Common patterns:",
        "- `sk-...`, `sk-proj-...` (OpenAI)",
        "- `key-...`, `pk-...` (generic)",
        "- `ghp_...`, `gho_...` (GitHub)",
        "- `xoxb-...`, `xoxp-...` (Slack)",
        "- `AKIA...` (AWS access key)",
        "- Any long alphanumeric string (20+ chars) that the user labels as a key, token, or secret",
        "",
        "When detected, say something like: *\"That looks like an API key. Let me store it securely so you don't lose it. I'll use Secrets Manager (recommended) — OK?\"* Then store it immediately using Secrets Manager unless the user prefers native storage. Infer the key name from context (e.g., `openai_api_key`, `github_token`).",
        "",
        "### Storage Options",
        "",
        "When a user explicitly asks to save an API key, present both options:",
        "",
        "**Option 1 — Native (file-based)**:",
        "- `node /skills/api-keys/native.js <user_id> set <key_name> <key_value>`",
        "- Stored in your workspace file, persists across sessions via S3 sync",
        "- KMS-encrypted at rest in S3, isolated to your user namespace",
        "",
        "**Option 2 — Secure (AWS Secrets Manager)** (recommended):",
        "- `node /skills/api-keys/secret.js <user_id> set <key_name> <key_value>`",
        "- Stored in AWS Secrets Manager, KMS-encrypted, auditable via CloudTrail",
        "- NOT stored in workspace files — stronger isolation",
        "",
        "**Unified retrieval** (checks SM first, falls back to native):",
        "- `node /skills/api-keys/retrieve.js <user_id> <key_name>`",
        "",
        "**Migration** between backends:",
        "- `node /skills/api-keys/migrate.js <user_id> <key_name> native-to-secure`",
        "- `node /skills/api-keys/migrate.js <user_id> <key_name> secure-to-native`",
        "",
        "Actions for both native.js and secret.js: `set`, `get`, `list`, `delete`",
        "",
        "**Important**: The `<user_id>` is your namespace (e.g. `telegram_12345`). Never write API keys to regular user files (s3-user-files). Always use the api-keys skill.",
        "",
        ...(process.env.BROWSER_IDENTIFIER
          ? [
              "## Browser (AgentCore Browser)",
              "",
              "You have the **agentcore-browser** skill for headless Chromium browsing. Use it when users ask to:",
              "- Visit or navigate to a web page",
              "- Take a screenshot of a website",
              "- Interact with page elements (click buttons, fill forms, scroll)",
              "",
              "**Commands** (run via Bash):",
              '- Navigate: `node /skills/agentcore-browser/navigate.js \'{"url": "https://example.com"}\'`',
              '- Screenshot: `node /skills/agentcore-browser/screenshot.js \'{"description": "Page screenshot"}\'`',
              '- Click: `node /skills/agentcore-browser/interact.js \'{"action": "click", "selector": "#btn"}\'`',
              '- Type: `node /skills/agentcore-browser/interact.js \'{"action": "type", "selector": "#input", "text": "hello"}\'`',
              '- Scroll: `node /skills/agentcore-browser/interact.js \'{"action": "scroll"}\'`',
              '- Wait: `node /skills/agentcore-browser/interact.js \'{"action": "wait", "selector": ".results"}\'`',
              "",
              "Screenshots are uploaded to S3 and delivered as images to the user's chat.",
              "The browser session is pre-created at startup — no setup needed.",
              "",
            ]
          : []),
        "## Sub-agents",
        "",
        "Skills like deep-research-pro and task-decomposer can spawn sub-agents for parallel work.",
        "Sub-agents share the same model and capabilities. Sandbox is disabled (the container is already isolated).",
        "",
      ].join("\n"),
    );
    console.log("[contract] AGENTS.md written");
  }
}

/**
 * Poll for OpenClaw readiness in the background.
 * Sets openclawReady=true and starts workspace saves when ready.
 */
async function pollOpenClawReadiness(namespace) {
  const ready = await waitForPort(OPENCLAW_PORT, "OpenClaw", 300000, 5000);
  if (ready) {
    openclawReady = true;
    workspaceSync.startPeriodicSave(namespace);
    console.log(
      "[contract] OpenClaw ready — switching from lightweight agent to full OpenClaw",
    );
  } else {
    console.error(
      "[contract] OpenClaw failed to start — lightweight agent will continue handling messages",
    );
  }
}

/**
 * Generate SigV4-signed HTTP headers for a WebSocket CDP endpoint.
 * The browser automation stream requires IAM authentication — Playwright's
 * connectOverCDP sends these as HTTP upgrade request headers.
 *
 * @param {string} wsEndpoint - WebSocket URL (wss://...)
 * @returns {object} Signed headers (Authorization, X-Amz-Date, X-Amz-Security-Token, Host)
 */
async function signBrowserEndpoint(wsEndpoint) {
  const { SignatureV4 } = require("@smithy/signature-v4");
  const { Sha256 } = require("@aws-crypto/sha256-js");
  const { defaultProvider } = require("@aws-sdk/credential-provider-node");

  const url = new URL(wsEndpoint.replace(/^wss:/, "https:"));
  const region = process.env.AWS_REGION || "us-east-1";

  const signer = new SignatureV4({
    service: "bedrock-agentcore",
    region,
    credentials: defaultProvider(),
    sha256: Sha256,
  });

  const signed = await signer.sign({
    method: "GET",
    protocol: url.protocol,
    hostname: url.hostname,
    port: url.port ? Number(url.port) : undefined,
    path: url.pathname + url.search,
    headers: {
      host: url.host,
    },
  });

  // Return only the auth-relevant headers
  const result = {};
  for (const [k, v] of Object.entries(signed.headers)) {
    if (/^(authorization|x-amz-|host)$/i.test(k) || k.startsWith("x-amz-")) {
      result[k] = v;
    }
  }
  return result;
}

/**
 * Start an AgentCore Browser session for the given user.
 * Non-fatal — logs and continues if browser feature is not enabled or SDK fails.
 */
async function initBrowserSession(userId) {
  const browserIdentifier = process.env.BROWSER_IDENTIFIER;
  if (!browserIdentifier) return; // Feature not enabled

  // Per-user sessions use a single userId; check state vars directly
  if (currentBrowserSessionId) return; // Already initialized

  try {
    const { BedrockAgentCoreClient, StartBrowserSessionCommand } =
      await import("@aws-sdk/client-bedrock-agentcore");
    const client = new BedrockAgentCoreClient({ region: process.env.AWS_REGION || "us-east-1" });

    const response = await client.send(new StartBrowserSessionCommand({
      browserIdentifier,
      name: userId.replace(/[^a-zA-Z0-9-]/g, "-").slice(0, 64),
      sessionTimeoutSeconds: BROWSER_SESSION_TIMEOUT_SECONDS,
      viewportConfiguration: {
        width: 1280,
        height: 720,
      },
    }));

    const endpoint = response.streams?.automationStream?.streamEndpoint;
    if (!endpoint) throw new Error("No automation stream endpoint returned");

    currentBrowserSessionId = response.sessionId;
    currentBrowserEndpoint = endpoint;

    // Generate SigV4 auth headers for the WebSocket CDP connection
    const headers = await signBrowserEndpoint(endpoint);

    // Write endpoint + headers to file for skill processes to read
    fs.writeFileSync(BROWSER_SESSION_FILE, JSON.stringify({
      endpoint, sessionId: response.sessionId, headers,
    }));

    // Refresh SigV4 headers every 4 min (signatures expire after ~5 min)
    browserHeaderRefreshTimer = setInterval(async () => {
      try {
        const refreshed = await signBrowserEndpoint(currentBrowserEndpoint);
        const data = JSON.parse(fs.readFileSync(BROWSER_SESSION_FILE, "utf8"));
        data.headers = refreshed;
        fs.writeFileSync(BROWSER_SESSION_FILE, JSON.stringify(data));
      } catch (e) {
        console.error("[browser] Failed to refresh SigV4 headers:", e.message);
      }
    }, 4 * 60 * 1000);

    console.log("[browser] Session started for", userId, "-", response.sessionId);
  } catch (err) {
    console.error("[browser] Failed to start session for", userId, "-", err.message);
    // Non-fatal — continue without browser
  }
}

/**
 * Stop all active browser sessions. Called during SIGTERM shutdown.
 */
async function stopBrowserSessions() {
  const browserIdentifier = process.env.BROWSER_IDENTIFIER;
  if (!browserIdentifier) return;
  if (!currentBrowserSessionId) return;

  try {
    const { BedrockAgentCoreClient, StopBrowserSessionCommand } =
      await import("@aws-sdk/client-bedrock-agentcore");
    const client = new BedrockAgentCoreClient({ region: process.env.AWS_REGION || "us-east-1" });

    await client.send(new StopBrowserSessionCommand({
      browserIdentifier,
      sessionId: currentBrowserSessionId,
    }));
    console.log("[browser] Stopped session for user (sessionId:", currentBrowserSessionId + ")");
  } catch (err) {
    console.error("[browser] Stop failed (sessionId:", currentBrowserSessionId + ") -", err.message);
  }
}

/**
 * Auto-restart OpenClaw if it crashes mid-session.
 * Uses linear backoff (5s, 10s, 15s) with a maximum of 3 retries.
 * Does not restart during shutdown or if OpenClaw recovered on its own.
 */
function scheduleOpenClawRestart(namespace) {
  if (shuttingDown) return;
  if (openclawRestartCount >= OPENCLAW_MAX_RESTARTS) {
    console.error(
      `[contract] OpenClaw crashed ${openclawRestartCount} times — giving up, lightweight agent will handle messages`,
    );
    return;
  }
  openclawRestartCount++;
  const delay = OPENCLAW_RESTART_DELAY_MS * openclawRestartCount;
  console.log(
    `[contract] Scheduling OpenClaw restart #${openclawRestartCount} in ${delay}ms...`,
  );
  setTimeout(() => {
    if (shuttingDown || openclawReady) return;
    console.log(
      `[contract] Restarting OpenClaw (attempt #${openclawRestartCount})...`,
    );
    openclawProcess = spawn(
      "openclaw",
      ["gateway", "run", "--port", String(OPENCLAW_PORT), "--verbose"],
      { stdio: ["ignore", "pipe", "pipe"], env: lastOpenClawEnv },
    );
    const captureLog2 = (stream, label) => {
      let buf = "";
      stream.on("data", (chunk) => {
        buf += chunk.toString();
        const lines = buf.split("\n");
        buf = lines.pop();
        for (const line of lines) {
          if (line.trim()) {
            console.log(`[openclaw:${label}] ${line}`);
            openclawLogs.push(`[${label}] ${line}`);
            if (openclawLogs.length > OPENCLAW_LOG_LIMIT) openclawLogs.shift();
          }
        }
      });
    };
    captureLog2(openclawProcess.stdout, "out");
    captureLog2(openclawProcess.stderr, "err");
    openclawProcess.on("exit", (code2) => {
      console.log(
        `[contract] OpenClaw (restart #${openclawRestartCount}) exited with code ${code2}`,
      );
      openclawExitCode = code2;
      openclawReady = false;
      scheduleOpenClawRestart(namespace);
    });
    // Poll for readiness after restart
    pollOpenClawReadiness(namespace).catch((err) => {
      console.error(
        `[contract] OpenClaw restart readiness poll failed: ${err.message}`,
      );
    });
  }, delay);
}

/**
 * Initialization — called on first /invocations request.
 *
 * Uses pre-fetched secrets. Starts proxy, OpenClaw, and workspace restore
 * in parallel. Only waits for proxy readiness (~5s), then returns.
 * OpenClaw readiness is polled in the background.
 */
async function init(userId, actorId, channel) {
  if (proxyReady) return; // Already initialized
  if (initInProgress) return initPromise;
  initInProgress = true;

  initPromise = (async () => {
    const namespace = actorId.replace(/:/g, "_");
    currentUserId = userId;
    currentNamespace = namespace;
    await cwLogger.init(`${namespace}-${Date.now()}`);

    // Expose USER_ID so child processes (OpenClaw skill scripts) inherit it
    process.env.USER_ID = actorId;
    // Expose INTERNAL_USER_ID for lightweight agent tool env (eventbridge-cron authorization)
    process.env.INTERNAL_USER_ID = userId;
    agent.TOOL_ENV.INTERNAL_USER_ID = userId;

    // Write initial identity file for the proxy to read
    updateIdentityFile(actorId, channel);

    console.log(
      `[contract] Init for user=${userId} actor=${actorId} namespace=${namespace}`,
    );

    // 0. Wait for pre-fetched secrets (should already be done by now)
    if (!secretsReady && secretsPrefetchPromise) {
      console.log("[contract] Waiting for secrets pre-fetch to complete...");
      await secretsPrefetchPromise;
    }

    // Retry secrets fetch inline if pre-fetch failed (transient error recovery)
    if (!GATEWAY_TOKEN) {
      console.log(
        "[contract] Gateway token missing — retrying secrets fetch...",
      );
      await prefetchSecrets();
    }
    if (!GATEWAY_TOKEN) {
      throw new Error(
        "Gateway token not available — cannot authenticate WebSocket connections",
      );
    }

    // 1b. Create scoped S3 credentials (per-user IAM isolation)
    // Restricts S3 access to the user's namespace prefix, preventing cross-user
    // data access even through OpenClaw's bash/code execution tools.
    let scopedCredsAvailable = false;
    if (process.env.EXECUTION_ROLE_ARN) {
      try {
        console.log("[contract] Creating scoped S3 credentials for namespace=", namespace);
        const creds = await scopedCreds.createScopedCredentials(namespace, { internalUserId: userId });
        scopedCreds.writeCredentialFiles(creds, SCOPED_CREDS_DIR);
        workspaceSync.configureCredentials(creds);
        scopedCredsAvailable = true;
        console.log("[contract] Scoped S3 credentials created and applied");

        // Refresh credentials before expiry (45 min timer, max 1 hour session)
        if (credentialRefreshTimer) clearInterval(credentialRefreshTimer);
        credentialRefreshTimer = setInterval(async () => {
          try {
            console.log("[contract] Refreshing scoped S3 credentials...");
            const refreshed = await scopedCreds.createScopedCredentials(namespace, { internalUserId: userId });
            scopedCreds.writeCredentialFiles(refreshed, SCOPED_CREDS_DIR);
            workspaceSync.configureCredentials(refreshed);
            console.log("[contract] Scoped S3 credentials refreshed");
          } catch (err) {
            console.error(`[contract] Credential refresh failed: ${err.message}`);
          }
        }, 45 * 60 * 1000); // 45 minutes
      } catch (err) {
        console.warn(`[contract] Scoped credentials failed (falling back to full role): ${err.message}`);
        // Non-fatal — fall back to full execution role credentials
      }
    } else {
      console.log("[contract] EXECUTION_ROLE_ARN not set — skipping credential scoping");
    }

    // 1c. Clean up stale lock files restored from S3 (non-blocking)
    // Runs in parallel with proxy startup — does not block init.
    const lockCleanupPromise = cleanupLockFiles().catch((err) => {
      console.warn(`[contract] Lock cleanup failed: ${err.message}`);
    });

    // 2. Start the Bedrock proxy with user identity env vars
    // Only pass required env vars — avoid leaking secrets via process.env spread
    console.log("[contract] Starting Bedrock proxy...");
    const proxyEnv = {
      PATH: process.env.PATH,
      HOME: process.env.HOME || "/root",
      NODE_PATH: process.env.NODE_PATH || "/app/node_modules",
      NODE_OPTIONS: process.env.NODE_OPTIONS || "",
      AWS_REGION: process.env.AWS_REGION || "us-west-2",
      BEDROCK_MODEL_ID: process.env.BEDROCK_MODEL_ID || "",
      COGNITO_USER_POOL_ID: process.env.COGNITO_USER_POOL_ID || "",
      COGNITO_CLIENT_ID: process.env.COGNITO_CLIENT_ID || "",
      COGNITO_PASSWORD_SECRET: COGNITO_PASSWORD_SECRET || "",
      S3_USER_FILES_BUCKET: process.env.S3_USER_FILES_BUCKET || "",
      SUBAGENT_MODEL_NAME: SUBAGENT_MODEL_NAME,
      SUBAGENT_BEDROCK_MODEL_ID: process.env.SUBAGENT_BEDROCK_MODEL_ID || "",
      USER_ID: actorId,
      INTERNAL_USER_ID: userId,  // container internal userId for skill authorization
      CHANNEL: channel,
      OPENCLAW_SKIP_CRON: "1", // Disable internal cron — EventBridge handles scheduling
    };
    proxyProcess = spawn("node", ["/app/agentcore-proxy.js"], {
      env: proxyEnv,
      stdio: "inherit",
    });
    proxyProcess.on("exit", (code) => {
      console.log(`[contract] Proxy exited with code ${code}`);
      proxyReady = false;
    });

    // Wait for lock cleanup to complete before starting OpenClaw
    await lockCleanupPromise;

    // Write OpenClaw config and start gateway (non-blocking)
    writeOpenClawConfig();
    console.log("[contract] Starting OpenClaw gateway (headless)...");
    // Build scoped env for OpenClaw — excludes container credentials,
    // uses credential_process for scoped S3 access only.
    // Falls back to full process.env if scoped credentials failed.
    let openclawEnv;
    if (scopedCredsAvailable) {
      openclawEnv = scopedCreds.buildOpenClawEnv({
        credDir: SCOPED_CREDS_DIR,
        baseEnv: process.env,
      });
    } else {
      // SECURITY: Never start OpenClaw with full execution role credentials.
      // Build a safe env that strips ALL AWS credential sources.
      // OpenClaw will have zero AWS access — tools fail gracefully.
      console.error(
        "[contract] WARNING: Scoped credentials failed — starting OpenClaw with zero AWS access",
      );
      openclawEnv = scopedCreds.buildOpenClawEnv({
        credDir: null,
        baseEnv: process.env,
      });
      openclawEnv.OPENCLAW_NO_AWS = "1";
    }
    // Propagate INTERNAL_USER_ID so OpenClaw skills (e.g., eventbridge-cron)
    // can resolve the container's authorized userId for DynamoDB writes.
    openclawEnv.INTERNAL_USER_ID = userId;
    openclawProcess = spawn(
      "openclaw",
      ["gateway", "run", "--port", String(OPENCLAW_PORT), "--verbose"],
      { stdio: ["ignore", "pipe", "pipe"], env: openclawEnv },
    );
    lastOpenClawEnv = openclawEnv;
    openclawRestartCount = 0;
    // Capture OpenClaw stdout/stderr for diagnostics
    const captureLog = (stream, label) => {
      let buf = "";
      stream.on("data", (chunk) => {
        buf += chunk.toString();
        const lines = buf.split("\n");
        buf = lines.pop(); // keep incomplete line in buffer
        for (const line of lines) {
          if (line.trim()) {
            console.log(`[openclaw:${label}] ${line}`);
            openclawLogs.push(`[${label}] ${line}`);
            if (openclawLogs.length > OPENCLAW_LOG_LIMIT) openclawLogs.shift();
          }
        }
      });
    };
    captureLog(openclawProcess.stdout, "out");
    captureLog(openclawProcess.stderr, "err");
    openclawProcess.on("exit", (code) => {
      console.log(`[contract] OpenClaw exited with code ${code}`);
      openclawExitCode = code;
      openclawReady = false;
      scheduleOpenClawRestart(currentNamespace);
    });

    // Restore workspace from S3 (non-blocking, needed for OpenClaw)
    workspaceSync.restoreWorkspace(namespace).catch((err) => {
      console.warn(`[contract] Workspace restore failed: ${err.message}`);
    });

    // 2. Wait only for proxy readiness (~5s)
    proxyReady = await waitForPort(PROXY_PORT, "Proxy", 30000, 1000);
    if (!proxyReady) {
      throw new Error("Proxy failed to start within 30s");
    }

    // 2b. Warm proxy JIT — send a lightweight request to trigger V8 compilation
    // of the request handling path, so the first real user message is faster.
    warmProxyJit().catch(() => {}); // non-blocking, fire-and-forget

    // 3. Poll for OpenClaw readiness in the background (don't block)
    pollOpenClawReadiness(namespace).catch((err) => {
      console.error(
        `[contract] OpenClaw readiness polling failed: ${err.message}`,
      );
    });

    // Start browser session in background (non-blocking, fire-and-forget)
    initBrowserSession(userId).catch((err) => {
      console.error(`[browser] Init error (non-fatal): ${err.message}`);
    });

    console.log(
      "[contract] Init complete — proxy ready, lightweight agent active",
    );
  })();

  try {
    await initPromise;
  } catch (err) {
    // Reset initPromise on failure so concurrent requests don't await a stale rejected promise
    initPromise = null;
    throw err;
  } finally {
    initInProgress = false;
  }
}

/**
 * Extract plain text from message content — handles string, array of content
 * blocks, JSON-serialized array of content blocks, or object with text/content.
 *
 * Recursively unwraps nested content blocks (common with subagent responses
 * where each layer wraps the previous one in content block JSON).
 */
function extractTextFromContent(content) {
  if (!content) return "";
  // Already a parsed array of content blocks
  if (Array.isArray(content)) {
    const text = content
      .filter((b) => b.type === "text")
      .map((b) => b.text)
      .join("");
    // Recurse in case the inner text is itself a JSON content block array
    return extractTextFromContent(text);
  }
  if (typeof content === "string") {
    // Check if the string is a JSON-serialized array of content blocks
    const trimmed = content.trim();
    if (trimmed.startsWith("[{") && trimmed.endsWith("]")) {
      let parsed = null;
      try {
        parsed = JSON.parse(trimmed);
      } catch {
        // Retry with literal control characters escaped (JS JSON.parse is strict)
        try {
          const sanitized = trimmed.replace(/[\x00-\x1f\x7f]/g, c => {
            const e = {"\b":"\\b","\t":"\\t","\n":"\\n","\f":"\\f","\r":"\\r"};
            return e[c] || ("\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
          });
          parsed = JSON.parse(sanitized);
        } catch {
          // Both failed — try regex extraction below
        }
      }
      if (!parsed) {
        // Regex fallback for malformed JSON (e.g., "text","value" instead of "text":"value")
        const textMatch = trimmed.match(/[,{]\s*"text"\s*[,:]\s*"((?:[^"\\]|\\.)*)"/);
        if (textMatch) {
          try {
            const extracted = JSON.parse('"' + textMatch[1] + '"');
            if (extracted) return extractTextFromContent(extracted);
          } catch {
            return extractTextFromContent(textMatch[1]);
          }
        }
      }
      if (
        parsed &&
        Array.isArray(parsed) &&
        parsed.length > 0 &&
        parsed.every((b) => typeof b === "object" && b !== null) &&
        parsed.some((b) => typeof b.type === "string")
      ) {
        const text = parsed
          .filter((b) => b.type === "text")
          .map((b) => b.text)
          .join("");
        // Preserve leading whitespace from original string, recurse to unwrap further nesting
        const leading = content.match(/^(\s*)/)[0];
        return extractTextFromContent(leading + text);
      }
    }
    // Detect truncated content block JSON (e.g., "\n\n[{" or "\n\n[{"type":"text"...")
    // These are partial content blocks from streaming that shouldn't leak as response text
    if (trimmed.startsWith("[{") && !trimmed.endsWith("]")) {
      if (/^\[\{\s*"type"\s*:/.test(trimmed) || trimmed === "[{") {
        return "";
      }
    }
    // Plain text string
    return content;
  }
  // Object with text or content property (e.g., {role: "assistant", content: "..."})
  if (typeof content === "object" && content !== null) {
    if (typeof content.text === "string")
      return extractTextFromContent(content.text);
    if (typeof content.content === "string")
      return extractTextFromContent(content.content);
    if (Array.isArray(content.content)) {
      const text = content.content
        .filter((b) => b.type === "text")
        .map((b) => b.text)
        .join("");
      return extractTextFromContent(text);
    }
  }
  return "";
}

/**
 * Process the message queue serially to prevent concurrent WebSocket race conditions.
 */
async function processMessageQueue() {
  if (processingMessage || messageQueue.length === 0) return;
  processingMessage = true;

  while (messageQueue.length > 0) {
    const { message, resolve, reject } = messageQueue.shift();
    console.log(
      `[contract] Processing queued message (${messageQueue.length} remaining)`,
    );

    try {
      const response = await bridgeMessage(message, 560000);
      resolve(response);
    } catch (err) {
      reject(err);
    }
  }

  processingMessage = false;
}

/**
 * Enqueue a message and wait for its response (serialized processing).
 */
function enqueueMessage(message) {
  return new Promise((resolve, reject) => {
    messageQueue.push({ message, resolve, reject });
    console.log(
      `[contract] Message enqueued (queue length: ${messageQueue.length})`,
    );
    processMessageQueue().catch((err) => {
      console.error(`[contract] Queue processing error: ${err.message}`);
    });
  });
}

/**
 * Bridge a chat message to OpenClaw via WebSocket and collect the response.
 */
async function bridgeMessage(message, timeoutMs = 560000) {
  const { randomUUID } = require("crypto");
  return new Promise((resolve) => {
    const wsUrl = `ws://127.0.0.1:${OPENCLAW_PORT}`;
    console.log(`[contract] Connecting to WebSocket: ${wsUrl}`);
    const ws = new WebSocket(wsUrl, {
      origin: `http://127.0.0.1:${OPENCLAW_PORT}`,
    });
    let responseText = "";
    let authenticated = false;
    let chatSent = false;
    let resolved = false;
    let connectReqId = null;
    let chatReqId = null;
    let unhandledMsgs = [];

    const done = (text) => {
      if (resolved) return;
      resolved = true;
      clearTimeout(timer);
      try {
        ws.close();
      } catch {}
      resolve(text);
    };

    const timer = setTimeout(() => {
      const debugInfo =
        unhandledMsgs.length > 0
          ? ` unhandled=[${unhandledMsgs.slice(0, 5).join(" | ")}]`
          : "";
      console.warn(
        `[contract] WebSocket timeout after ${timeoutMs}ms (auth=${authenticated}, chatSent=${chatSent}, responseLen=${responseText.length})${debugInfo}`,
      );
      // Return "" on timeout so caller can fall back to lightweight agent
      done(responseText || "");
    }, timeoutMs);

    ws.on("open", () => {
      console.log("[contract] WebSocket connected, waiting for challenge...");
    });

    ws.on("message", (data) => {
      const raw = data.toString();
      console.log(`[contract] WS rx: ${raw.slice(0, 500)}`);
      let msg;
      try {
        msg = JSON.parse(raw);
      } catch (e) {
        console.log(`[contract] WS parse error: ${e.message}`);
        return;
      }

      // Step 1: Server sends connect.challenge event -> client sends connect request
      if (msg.type === "event" && msg.event === "connect.challenge") {
        console.log(
          "[contract] Received challenge, sending connect request...",
        );
        connectReqId = randomUUID();
        ws.send(
          JSON.stringify({
            type: "req",
            id: connectReqId,
            method: "connect",
            params: {
              minProtocol: 3,
              maxProtocol: 3,
              client: {
                id: "openclaw-control-ui",
                mode: "backend",
                version: "dev",
                platform: "linux",
              },
              caps: [],
              auth: { token: GATEWAY_TOKEN },
              role: "operator",
              scopes: ["operator.admin", "operator.read", "operator.write"],
            },
          }),
        );
        return;
      }

      // Step 2: Server responds to connect request -> send chat.send
      if (msg.type === "res" && msg.id === connectReqId) {
        if (!msg.ok) {
          console.error(
            `[contract] Connect rejected: ${JSON.stringify(msg.error || msg.payload)}`,
          );
          done(
            `Auth failed: ${msg.error?.message || JSON.stringify(msg.payload)}`,
          );
          return;
        }
        authenticated = true;
        console.log(
          "[contract] Authenticated successfully, sending chat.send...",
        );
        chatReqId = randomUUID();
        ws.send(
          JSON.stringify({
            type: "req",
            id: chatReqId,
            method: "chat.send",
            params: {
              sessionKey: "global",
              message: message,
              idempotencyKey: chatReqId,
            },
          }),
        );
        chatSent = true;
        return;
      }

      // Helper: try all known content locations in a payload
      const extractFromPayload = (pl) => {
        return (
          extractTextFromContent(pl.message?.content) ||
          extractTextFromContent(pl.message) ||
          extractTextFromContent(pl.text) ||
          extractTextFromContent(pl.content)
        );
      };

      // Step 3: Chat events — state: "delta" (streaming) or "final" (complete)
      // OpenClaw puts content in payload.message.content (usual) or
      // directly in payload.message (string or content-blocks array).
      if (msg.type === "event" && msg.event === "chat") {
        const payload = msg.payload || {};

        if (payload.state === "delta") {
          const text = extractFromPayload(payload);
          if (text) responseText = text; // Delta replaces (accumulates progressively)
          return;
        }

        if (payload.state === "final") {
          // Final message may include the complete text
          const text = extractFromPayload(payload);
          if (text) responseText = text;
          console.log(`[contract] Chat final (${responseText.length} chars)`);
          if (responseText) {
            done(responseText);
          } else {
            // Empty final — log full payload for diagnostics and return ""
            // to signal caller that the bridge got no content.
            console.warn(
              `[contract] Empty final event — payload: ${JSON.stringify(payload).slice(0, 1000)}`,
            );
            done("");
          }
          return;
        }

        if (payload.state === "error") {
          console.error(
            `[contract] Chat error event: ${payload.errorMessage || "unknown"}`,
          );
          done(
            responseText || `Chat error: ${payload.errorMessage || "unknown"}`,
          );
          return;
        }

        if (payload.state === "aborted") {
          done(responseText || "Chat aborted.");
          return;
        }
        return;
      }

      // Step 4: Response to chat.send request (accepted/final)
      if (msg.type === "res" && msg.id === chatReqId) {
        if (!msg.ok) {
          console.error(
            `[contract] Chat error: ${JSON.stringify(msg.error || msg.payload)}`,
          );
          done(
            responseText || `Chat error: ${msg.error?.message || "unknown"}`,
          );
          return;
        }
        // Log full payload for debugging
        const status = msg.payload?.status;
        console.log(
          `[contract] Chat res status=${status} payload=${JSON.stringify(msg.payload).slice(0, 500)}`,
        );
        // "started" or "accepted" = in progress, wait for streaming events
        if (status === "started" || status === "accepted") return;
        // "final" or "done" = completed — return "" if no content (bridge empty)
        if (responseText) {
          done(responseText);
        } else {
          console.warn(
            `[contract] Chat response completed with no streaming content — payload: ${JSON.stringify(msg.payload).slice(0, 500)}`,
          );
          done("");
        }
        return;
      }

      // Unhandled message — log for debugging
      unhandledMsgs.push(raw.slice(0, 300));
    });

    ws.on("error", (err) => {
      console.error(`[contract] WebSocket error: ${err.message}`);
      // Return "" on error so caller can fall back to lightweight agent
      done(responseText || "");
    });

    ws.on("close", (code, reason) => {
      const reasonStr = reason ? reason.toString() : "";
      const debugInfo =
        unhandledMsgs.length > 0
          ? ` unhandled=[${unhandledMsgs.slice(0, 3).join(" | ")}]`
          : "";
      console.warn(
        `[contract] WebSocket closed: code=${code} reason=${reasonStr} auth=${authenticated} chatSent=${chatSent} responseLen=${responseText.length}${debugInfo}`,
      );
      // Return "" on unexpected close so caller can fall back to lightweight agent
      done(responseText || "");
    });
  });
}

/**
 * Build bridge text from message payload.
 * Handles structured messages with images and plain text.
 */
function buildBridgeText(message) {
  if (
    typeof message === "object" &&
    message !== null &&
    Array.isArray(message.images)
  ) {
    return (
      (message.text || "") +
      "\n\n[OPENCLAW_IMAGES:" +
      JSON.stringify(message.images) +
      "]"
    );
  }
  if (typeof message === "string") {
    return message;
  }
  return String(message);
}

/**
 * AgentCore contract HTTP server.
 */
const server = http.createServer(async (req, res) => {
  // GET /ping — AgentCore health check
  if (req.method === "GET" && req.url === "/ping") {
    pingCount++;
    const now = Date.now();
    const uptimeSec = Math.floor((now - startTime) / 1000);
    // HealthyBusy prevents AgentCore from terminating during active tasks.
    // Healthy allows natural idle termination when no tasks are running.
    const status = activeTaskCount > 0 ? "HealthyBusy" : "Healthy";
    const responseBody = {
      status,
      time_of_last_update: lastActivityTime,
      active_tasks: activeTaskCount,
    };

    // Log every ping for the first 5 minutes, then every 60s
    if (uptimeSec < 300 || now - lastPingLogTime >= PING_LOG_INTERVAL_MS) {
      console.log(
        `[contract] /ping #${pingCount} uptime=${uptimeSec}s status=${responseBody.status} openclawReady=${openclawReady} proxyReady=${proxyReady} activeTasks=${activeTaskCount}`,
      );
      lastPingLogTime = now;
    }

    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify(responseBody));
    return;
  }


  // POST /invocations — Chat handler
  if (req.method === "POST" && req.url === "/invocations") {
    let body = "";
    let bodySize = 0;
    let aborted = false;
    req.on("data", (chunk) => {
      bodySize += chunk.length;
      if (bodySize > MAX_BODY_SIZE) {
        aborted = true;
        res.writeHead(413, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "Request body too large" }));
        req.destroy();
        return;
      }
      body += chunk;
    });
    req.on("end", async () => {
      if (aborted) return;
      try {
        const payload = body ? JSON.parse(body) : {};
        const action = payload.action || "status";

        // Status check (no init needed)
        if (action === "status") {
          // Fetch proxy /health for request counters (non-blocking — null on failure)
          const proxyHealth = await checkProxyHealth();

          const diag = {
            buildVersion: BUILD_VERSION,
            uptime_seconds: Math.floor((Date.now() - startTime) / 1000),
            currentUserId,
            openclawReady,
            proxyReady,
            secretsReady,
            openclawExitCode,
            openclawPid: openclawProcess?.pid || null,
            openclawLogs: openclawLogs.slice(-20),
            totalRequestCount: proxyHealth?.total_requests ?? null,
            subagentRequestCount: proxyHealth?.subagent_requests ?? null,
            activeTaskCount,
            pingStatus: activeTaskCount > 0 ? "HealthyBusy" : "Healthy",
          };
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ response: JSON.stringify(diag) }));
          return;
        }

        // Warmup action — trigger lazy init without blocking for a chat response
        if (action === "warmup") {
          lastActivityTime = Math.floor(Date.now() / 1000);
          const { userId, actorId, channel } = payload;
          if (openclawReady && proxyReady) {
            res.writeHead(200, { "Content-Type": "application/json" });
            res.end(JSON.stringify({ status: "ready" }));
            return;
          }
          // Trigger init in background if not already running
          if (!initInProgress && userId && actorId) {
            init(userId, actorId, channel || "unknown").catch((err) => {
              console.error(`[contract] Warmup init failed: ${err.message}`);
            });
          }
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ status: "initializing" }));
          return;
        }

        // Cron action — blocks until init completes, then bridges the message
        if (action === "cron") {
          const { userId, actorId, channel, message } = payload;
          if (!userId || !actorId || !message) {
            res.writeHead(400, { "Content-Type": "application/json" });
            res.end(
              JSON.stringify({ error: "Missing userId, actorId, or message" }),
            );
            return;
          }

          // Update shared identity file so proxy picks up cross-channel changes
          updateIdentityFile(actorId, channel || "unknown");

          // Block until init completes (unlike chat which returns immediately)
          if (!openclawReady || !proxyReady) {
            try {
              if (!initInProgress) {
                await init(userId, actorId, channel || "unknown");
              } else {
                await initPromise;
              }
            } catch (err) {
              console.error(`[contract] Cron init failed: ${err.message}`);
              res.writeHead(200, { "Content-Type": "application/json" });
              res.end(
                JSON.stringify({
                  response: "Agent initialization failed for scheduled task.",
                  status: "error",
                }),
              );
              return;
            }
          }

          if (!openclawReady || !proxyReady) {
            res.writeHead(200, { "Content-Type": "application/json" });
            res.end(
              JSON.stringify({
                response: "Agent not ready after initialization.",
                status: "error",
              }),
            );
            return;
          }

          // Track active task to prevent idle termination during cron processing
          lastActivityTime = Math.floor(Date.now() / 1000);
          activeTaskCount++;
          let responseText;
          try {
            // Enqueue message (serialized with chat messages to prevent WebSocket races)
            try {
              responseText = await enqueueMessage(message);
            } catch (bridgeErr) {
              responseText = "";
              console.error(
                `[contract] Cron bridge error: ${bridgeErr.message}`,
              );
            }
            // Belt-and-suspenders: strip any remaining content-block JSON wrappers
            if (responseText) responseText = extractTextFromContent(responseText);

            // If bridge returned empty, fall back to lightweight agent
            if (!responseText || !responseText.trim()) {
              console.warn(
                "[contract] Cron bridge returned empty — falling back to lightweight agent",
              );
              try {
                responseText = await agent.chat(message, actorId, Date.now() + 30000);
              } catch (agentErr) {
                responseText =
                  "I couldn't process this scheduled task. Please check the configuration.";
                console.error(
                  `[contract] Cron lightweight agent fallback error: ${agentErr.message}`,
                );
              }
            }
          } finally {
            activeTaskCount = Math.max(0, activeTaskCount - 1);
          }

          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(
            JSON.stringify({
              response: responseText,
              userId: currentUserId,
              sessionId: payload.sessionId || null,
            }),
          );
          return;
        }

        // Chat action — lazy init and bridge
        if (action === "chat") {
          const { userId, actorId, channel, message } = payload;
          if (!userId || !actorId || !message) {
            res.writeHead(400, { "Content-Type": "application/json" });
            res.end(
              JSON.stringify({ error: "Missing userId, actorId, or message" }),
            );
            return;
          }

          // Update shared identity file so proxy picks up cross-channel changes
          updateIdentityFile(actorId, channel || "unknown");

          // Trigger init if not done yet (blocks until proxy is ready)
          if (!proxyReady && !initInProgress) {
            try {
              await init(userId, actorId, channel || "unknown");
            } catch (err) {
              console.error(`[contract] Init failed: ${err.message}`);
              res.writeHead(200, { "Content-Type": "application/json" });
              res.end(
                JSON.stringify({
                  response:
                    "I'm having trouble starting up. Please try again in a moment.",
                  userId,
                  sessionId: payload.sessionId || null,
                  status: "error",
                }),
              );
              return;
            }
          } else if (!proxyReady && initInProgress) {
            // Init already in progress — wait for it
            try {
              await initPromise;
            } catch (err) {
              console.error(
                `[contract] Init (in-progress) failed: ${err.message}`,
              );
              res.writeHead(200, { "Content-Type": "application/json" });
              res.end(
                JSON.stringify({
                  response:
                    "I'm still starting up. Please try again in a moment.",
                  userId,
                  sessionId: payload.sessionId || null,
                  status: "initializing",
                }),
              );
              return;
            }
          }

          const bridgeText = buildBridgeText(message);

          // Track active task to prevent idle termination during chat processing
          lastActivityTime = Math.floor(Date.now() / 1000);
          activeTaskCount++;
          let responseText;
          try {
            // Route based on readiness: OpenClaw (full) > lightweight agent (shim)
            if (openclawReady) {
              // Full OpenClaw path — WebSocket bridge
              try {
                responseText = await enqueueMessage(bridgeText);
              } catch (bridgeErr) {
                console.error(
                  `[contract] Bridge error, falling back to shim: ${bridgeErr.message}`,
                );
                responseText = "";
              }
              // If bridge returned empty (OpenClaw sent no content), check whether
              // OpenClaw is mid-run before falling back to lightweight agent.
              // A tool-call-only response or concurrent subagent task can produce
              // an empty bridge response that is NOT a failure.
              if (!responseText || !responseText.trim()) {
                // Brief retry — transient empty responses resolve quickly
                await new Promise((r) => setTimeout(r, 300));

                // Probe OpenClaw to see if it is still busy
                let openclawBusy = false;
                try {
                  const pingData = await new Promise((resolve, reject) => {
                    const pingReq = http.get(
                      `http://127.0.0.1:${OPENCLAW_PORT}`,
                      (pingRes) => {
                        let data = "";
                        pingRes.on("data", (c) => (data += c));
                        pingRes.on("end", () => resolve(data));
                      },
                    );
                    pingReq.on("error", reject);
                    pingReq.setTimeout(2000, () => {
                      pingReq.destroy();
                      reject(new Error("ping timeout"));
                    });
                  });
                  // OpenClaw may return JSON with activeTasks count
                  try {
                    const parsed = JSON.parse(pingData);
                    if (parsed.activeTasks > 0) openclawBusy = true;
                  } catch {
                    // Non-JSON response — OpenClaw is alive but format unknown
                  }
                } catch {
                  // OpenClaw not responding — not busy, allow fallback
                }

                // Also treat a still-running process (no exit code) as busy
                if (openclawExitCode === null) openclawBusy = true;

                if (openclawBusy) {
                  console.log(
                    "[contract] Bridge returned empty but OpenClaw is mid-run — returning busy message",
                  );
                  responseText =
                    "I'm still working on your previous request — check back in a moment.";
                } else {
                  console.warn(
                    "[contract] Bridge returned empty — falling back to lightweight agent",
                  );
                  try {
                    responseText = await agent.chat(
                      bridgeText,
                      actorId,
                      Date.now() + 30000,
                    );
                  } catch (agentErr) {
                    responseText =
                      "I'm having trouble right now. Please try again in a moment.";
                    console.error(
                      `[contract] Lightweight agent fallback error: ${agentErr.message}`,
                    );
                  }
                }
              }
            } else if (proxyReady) {
              // Warm-up shim path — lightweight agent via proxy
              console.log("[contract] Routing via lightweight agent (warm-up)");
              try {
                responseText = await agent.chat(bridgeText, actorId, Date.now() + 560000);
              } catch (agentErr) {
                responseText = `I'm having trouble right now. Please try again in a moment.`;
                console.error(
                  `[contract] Lightweight agent error: ${agentErr.message}`,
                );
              }
            } else {
              // Proxy not ready yet (should be rare — init awaits proxy)
              responseText = "I'm starting up — please try again in a moment.";
            }
          } finally {
            activeTaskCount = Math.max(0, activeTaskCount - 1);
          }

          // Belt-and-suspenders: strip any remaining content-block JSON wrappers
          if (responseText) responseText = extractTextFromContent(responseText);

          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(
            JSON.stringify({
              response: responseText,
              userId: currentUserId,
              sessionId: payload.sessionId || null,
            }),
          );
          return;
        }

        // Unknown action
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({ response: "Unknown action", status: "running" }),
        );
      } catch (err) {
        console.error("[contract] Invocation error:", err.message, err.stack);
        // Return 200 with generic error — AgentCore treats 500 as infrastructure failure.
        // Never expose stack traces or internal details to callers.
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            response: "An internal error occurred. Please try again.",
          }),
        );
      }
    });
    return;
  }

  res.writeHead(404, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "Not found" }));
});

// --- SIGTERM handler: save workspace and exit gracefully ---
process.on("SIGTERM", async () => {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(
    "[contract] SIGTERM received — saving workspace and shutting down",
  );

  // Stop credential refresh timer
  if (credentialRefreshTimer) {
    clearInterval(credentialRefreshTimer);
    credentialRefreshTimer = null;
  }
  if (browserHeaderRefreshTimer) {
    clearInterval(browserHeaderRefreshTimer);
    browserHeaderRefreshTimer = null;
  }

  // Save workspace to S3 (10s max)
  const saveTimeout = setTimeout(() => {
    console.warn("[contract] Workspace save timeout — exiting");
    process.exit(0);
  }, 10000);

  try {
    await workspaceSync.cleanup(currentNamespace);
  } catch (err) {
    console.warn(`[contract] Workspace cleanup error: ${err.message}`);
  }

  // Stop browser sessions before exit
  try {
    await stopBrowserSessions();
  } catch (err) {
    console.warn(`[contract] Browser session cleanup error: ${err.message}`);
  }

  clearTimeout(saveTimeout);

  // Kill child processes
  if (openclawProcess) {
    try {
      openclawProcess.kill("SIGTERM");
    } catch {}
  }
  if (proxyProcess) {
    try {
      proxyProcess.kill("SIGTERM");
    } catch {}
  }

  await cwLogger.shutdown();
  console.log("[contract] Shutdown complete");
  process.exit(0);
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(
    `[contract] AgentCore contract server listening on http://0.0.0.0:${PORT} (per-user session mode)`,
  );
  console.log(
    "[contract] Endpoints: GET /ping, POST /invocations {action: chat|status|warmup|cron}",
  );

  // Pre-fetch secrets in background (saves ~2-3s from first-message critical path)
  secretsPrefetchPromise = prefetchSecrets().catch((err) => {
    console.warn(`[contract] Secret prefetch failed: ${err.message}`);
  });
});
