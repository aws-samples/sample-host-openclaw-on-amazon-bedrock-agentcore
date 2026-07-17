/**
 * AgentCore Runtime Contract Server — Per-User Sessions
 *
 * Implements the required HTTP protocol contract for AgentCore Runtime:
 *   - GET  /ping         -> Health check (Healthy — allows idle termination)
 *   - POST /invocations  -> Chat handler with hybrid init
 *
 * Each AgentCore session is dedicated to a single user. On first invocation:
 *   1. Generate an ephemeral local gateway token and pre-fetch only the
 *      proxy's infrastructure identity secret
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
const runtimePolicy = require("./runtime-policy");

const PORT = 8080;
const PROXY_PORT = 18790;
const OPENCLAW_PORT = 18789;

// Session storage mount path (set via filesystemConfigurations on Runtime)
const SESSION_STORAGE_MOUNT = "/mnt/workspace";
const OPENCLAW_DIR = process.env.HOME ? `${process.env.HOME}/.openclaw` : "/root/.openclaw";

// Ephemeral, microVM-local authentication for the loopback gateway only.
const GATEWAY_TOKEN = runtimePolicy.createLocalGatewayToken();

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
const SCOPED_CREDS_DIR = "/tmp/scoped-creds";
const IDENTITY_FILE = "/tmp/current-identity.json";
const BUILD_VERSION = "v40"; // Bump in cdk.json to force container redeploy

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
// Initialized to startup time; updated on each chat/warmup invocation.
let lastActivityTime = Math.floor(Date.now() / 1000);

// Message queue for serializing concurrent requests (OpenClaw WebSocket path)
let messageQueue = [];
let processingMessage = false;

/**
 * Write current actorId and channel to a shared file so the proxy process
 * can pick up cross-channel identity changes (the proxy's env vars are
 * fixed at spawn time and cannot be updated for a running child process).
 */
/**
 * Set up symlink from ~/.openclaw to session storage mount.
 * Returns true if session storage is available and symlink was created.
 */
function setupSessionStorageSymlink() {
  try {
    // Check if session storage mount exists (only available during invocation)
    if (!fs.existsSync(SESSION_STORAGE_MOUNT)) {
      console.log("[contract] Session storage not available at", SESSION_STORAGE_MOUNT);
      return false;
    }

    const mountedDir = `${SESSION_STORAGE_MOUNT}/.openclaw`;
    fs.mkdirSync(mountedDir, { recursive: true });

    // Check existing .openclaw — may be a symlink, directory, or missing
    let existingType = null;
    try {
      const stat = fs.lstatSync(OPENCLAW_DIR);
      if (stat.isSymbolicLink()) {
        const target = fs.readlinkSync(OPENCLAW_DIR);
        if (target === mountedDir) {
          console.log("[contract] Session storage symlink already in place");
          return true;
        }
        existingType = "symlink";
        fs.unlinkSync(OPENCLAW_DIR);
      } else if (stat.isDirectory()) {
        existingType = "directory";
        // Copy contents to session storage (cross-device, can't use rename)
        const { execSync } = require("child_process");
        execSync(`cp -a ${OPENCLAW_DIR}/. ${mountedDir}/ 2>/dev/null || true`);
        fs.rmSync(OPENCLAW_DIR, { recursive: true, force: true });
      } else {
        existingType = "file";
        fs.unlinkSync(OPENCLAW_DIR);
      }
    } catch {
      // OPENCLAW_DIR doesn't exist yet — that's fine
    }

    fs.symlinkSync(mountedDir, OPENCLAW_DIR);
    console.log(`[contract] Session storage symlink: ${OPENCLAW_DIR} -> ${mountedDir} (was: ${existingType || "missing"})`);
    return true;
  } catch (err) {
    console.warn(`[contract] Session storage setup failed: ${err.message}`);
    return false;
  }
}

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
  const region = process.env.AWS_REGION || "eu-west-1";
  const smClient = new SecretsManagerClient({ region });

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

/**
 * Write the frozen headless OpenClaw configuration and user-facing capability
 * instructions. Policy construction is pure and covered independently.
 */
function writeOpenClawConfig() {
  const fs = require("fs");
  const config = runtimePolicy.buildOpenClawConfig({
    gatewayToken: GATEWAY_TOKEN,
    proxyPort: PROXY_PORT,
    gatewayPort: OPENCLAW_PORT,
  });

  const homeDir = process.env.HOME || "/root";
  fs.mkdirSync(`${homeDir}/.openclaw`, { recursive: true });
  fs.writeFileSync(
    `${homeDir}/.openclaw/openclaw.json`,
    JSON.stringify(config, null, 2),
  );
  console.log("[contract] OpenClaw headless config written");

  // Always overwrite restored instructions so stale capability claims cannot
  // widen the model-visible surface.
  const agentsMdPath = `${homeDir}/.openclaw/AGENTS.md`;
  {
    fs.writeFileSync(
      agentsMdPath,
      [
        "# Agent Instructions",
        "",
        "You are a helpful AI assistant running in a per-user container on AWS.",
        "You have web page retrieval and four bounded persistent workspace tools.",
        "",
        "## Response Formatting",
        "",
        "Format responses for a chat interface:",
        "- **No markdown tables** — use bullet lists or plain text paragraphs instead",
        "- Tables do not render in most chat apps; bullets always work",
        "- Keep responses concise and chat-appropriate",
        "",
        "## Built-in Web Tool",
        "",
        "You have the built-in **web_fetch** tool:",
        "- **web_fetch**: Fetch and read a public web page as markdown",
        "",
        "Use it when a request includes or identifies a page URL to read.",
        "",
        "## File Storage",
        "",
        "Use only `po_file_list`, `po_file_read`, `po_file_write`, and `po_file_delete` for persistent files.",
        "Paths are relative to this user's server-owned workspace. Never ask for or invent a user ID or namespace.",
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

    // The in-process warm-up store and the OpenClaw plugin read only this
    // server-derived prefix; model/tool arguments cannot replace it.
    process.env.PERSONAL_OPERATOR_WORKSPACE_PREFIX = namespace;

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

    // 1b. Create scoped S3 credentials (per-user IAM isolation)
    // Restricts S3 access to the user's namespace prefix.
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
      AWS_REGION: process.env.AWS_REGION || "eu-west-1",
      BEDROCK_MODEL_ID: process.env.BEDROCK_MODEL_ID || "",
      COGNITO_USER_POOL_ID: process.env.COGNITO_USER_POOL_ID || "",
      COGNITO_CLIENT_ID: process.env.COGNITO_CLIENT_ID || "",
      COGNITO_PASSWORD_SECRET: COGNITO_PASSWORD_SECRET || "",
      S3_USER_FILES_BUCKET: process.env.S3_USER_FILES_BUCKET || "",
      USER_ID: actorId,
      INTERNAL_USER_ID: userId,
      CHANNEL: channel,
    };
    proxyProcess = spawn("node", ["/app/agentcore-proxy.js"], {
      env: proxyEnv,
      stdio: ["inherit", "pipe", "pipe"],
    });
    proxyProcess.stdout.on("data", (d) => {
      d.toString().split("\n").filter(Boolean).forEach(line => console.log(`[proxy:out] ${line}`));
    });
    proxyProcess.stderr.on("data", (d) => {
      d.toString().split("\n").filter(Boolean).forEach(line => console.error(`[proxy:err] ${line}`));
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
    let scopedEnvironment;
    if (scopedCredsAvailable) {
      scopedEnvironment = scopedCreds.buildOpenClawEnv({
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
      scopedEnvironment = scopedCreds.buildOpenClawEnv({
        credDir: null,
        baseEnv: process.env,
      });
    }
    const openclawEnv = runtimePolicy.buildOpenClawChildEnv({
      scopedEnv: scopedEnvironment,
      workspacePrefix: namespace,
    });
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

    // Session storage: symlink .openclaw → /mnt/workspace/.openclaw if available
    const sessionStorageAvailable = setupSessionStorageSymlink();

    // Restore workspace from S3 if session storage is empty or unavailable
    if (sessionStorageAvailable) {
      // Check if session storage .openclaw dir has content (non-empty = resumed session)
      const mountedOpenclawDir = `${SESSION_STORAGE_MOUNT}/.openclaw`;
      let hasContent = false;
      try {
        const entries = fs.readdirSync(mountedOpenclawDir);
        hasContent = entries.length > 0;
      } catch { /* dir doesn't exist yet */ }

      if (hasContent) {
        console.log("[contract] Session storage has existing data — skipping S3 restore");
      } else {
        console.log("[contract] Session storage is empty — restoring from S3 backup");
        workspaceSync.restoreWorkspace(namespace).catch((err) => {
          console.warn(`[contract] Workspace restore failed: ${err.message}`);
        });
      }
    } else {
      // No session storage — use S3 sync as primary (existing behavior)
      workspaceSync.restoreWorkspace(namespace).catch((err) => {
        console.warn(`[contract] Workspace restore failed: ${err.message}`);
      });
    }

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
 * Recursively unwraps nested content blocks.
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
    const { message, onDelta, resolve, reject } = messageQueue.shift();
    console.log(
      `[contract] Processing queued message (${messageQueue.length} remaining)`,
    );

    try {
      const response = await bridgeMessage(message, 620000, onDelta);
      resolve(response);
    } catch (err) {
      reject(err);
    }
  }

  processingMessage = false;
}

/**
 * Enqueue a message and wait for its response (serialized processing).
 * @param {string} message - The message to send
 * @param {function} [onDelta] - Optional callback invoked with cumulative text on each delta
 */
function enqueueMessage(message, onDelta) {
  return new Promise((resolve, reject) => {
    messageQueue.push({ message, onDelta, resolve, reject });
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
 * @param {string} message - The message to send
 * @param {number} timeoutMs - Timeout in milliseconds
 * @param {function} [onDelta] - Optional callback invoked with cumulative text on each delta
 */
async function bridgeMessage(message, timeoutMs = 620000, onDelta) {
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
              minProtocol: 4,
              maxProtocol: 4,
              client: {
                id: "gateway-client",
                mode: "backend",
                version: "dev",
                platform: "linux",
              },
              caps: [],
              auth: { token: GATEWAY_TOKEN },
              role: "operator",
              scopes: runtimePolicy.GATEWAY_CLIENT_SCOPES,
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
          if (text) {
            responseText = text; // Delta replaces (accumulates progressively)
            if (onDelta) onDelta(text);
          }
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
              // A tool-call-only response can produce an empty bridge response
              // that is not necessarily a failure.
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
                responseText = await agent.chat(bridgeText, actorId, Date.now() + 620000);
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
    "[contract] Endpoints: GET /ping, POST /invocations {action: chat|status|warmup}",
  );

  // Pre-fetch secrets in background (saves ~2-3s from first-message critical path)
  secretsPrefetchPromise = prefetchSecrets().catch((err) => {
    console.warn(`[contract] Secret prefetch failed: ${err.message}`);
  });
});
