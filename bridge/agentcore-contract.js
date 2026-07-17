/**
 * AgentCore Runtime Contract Server — Per-User Sessions
 *
 * Implements the required HTTP protocol contract for AgentCore Runtime:
 *   - GET  /ping         -> Health check (Healthy — allows idle termination)
 *   - POST /invocations  -> Chat handler with hybrid init
 *
 * Each AgentCore session is dedicated to a single user. On first invocation:
 *   1. Bind the exact internal identity and mint scoped workspace credentials
 *   2. Start the execution-role proxy and scoped OpenClaw/workspace paths
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
const workspaceSync = require("./workspace-sync");
const cwLogger = require("./cloudwatch-logger");
const agent = require("./lightweight-agent");
const scopedCreds = require("./scoped-credentials");
const runtimePolicy = require("./runtime-policy");
const gatewayInvocation = require("./gateway-invocation");
const { SessionBinding } = require("./session-binding");
const { createInvocationHandler } = require("./invocation-handler");

const RUNTIME_REGION = scopedCreds.requireExactRegion(process.env);

const PORT = 8080;
const PROXY_PORT = 18790;
const OPENCLAW_PORT = 18789;

// Session storage mount path (set via filesystemConfigurations on Runtime)
const SESSION_STORAGE_MOUNT = "/mnt/workspace";
const OPENCLAW_DIR = process.env.HOME ? `${process.env.HOME}/.openclaw` : "/root/.openclaw";

// Ephemeral, microVM-local authentication for the loopback gateway only.
const GATEWAY_TOKEN = runtimePolicy.createLocalGatewayToken();

// Maximum request body size (1MB) to prevent memory exhaustion
const MAX_BODY_SIZE = 1 * 1024 * 1024;

// Ping diagnostics — track call count and log periodically
let pingCount = 0;
let lastPingLogTime = 0;
const PING_LOG_INTERVAL_MS = 60000; // Log ping stats every 60s

// State tracking
let currentInternalUserId = null;
let currentNamespace = null;
let openclawProcess = null;
let proxyProcess = null;
let openclawReady = false;
let gatewayQuarantined = null;
let proxyReady = false;
let initInProgress = false;
let initPromise = null;
let startTime = Date.now();
let shuttingDown = false;
let credentialRefreshTimer = null;
const SCOPED_CREDS_DIR = "/tmp/scoped-creds";
const BUILD_VERSION = "v40"; // Bump in cdk.json to force container redeploy

// OpenClaw process diagnostics (last N lines of stdout/stderr)
const OPENCLAW_LOG_LIMIT = 50;
let openclawLogs = [];
let openclawExitCode = null;
let lastOpenClawEnv = null;

const gatewayRuntimeBoundary = gatewayInvocation.createGatewayRuntimeBoundary({
  getGatewayProcess: () => openclawProcess,
  terminateGraceMs: 2_000,
});
const trustedInvocationRegistry =
  gatewayInvocation.createTrustedInvocationRegistry({
    ttlMs: 60 * 60 * 1_000,
    maxSettledEntries: 64,
    maxInFlightEntries: 8,
  });
const gatewayMessageExecutor =
  gatewayInvocation.createBoundedSerialExecutor({ maxPending: 7 });

// OpenClaw auto-restart on crash
let openclawRestartCount = 0;
const OPENCLAW_MAX_RESTARTS = 3;
const OPENCLAW_RESTART_DELAY_MS = 5000;

// Active task tracking — HealthyBusy prevents AgentCore from terminating during long tasks
let activeTaskCount = 0;
// Last activity timestamp (epoch seconds) — reported in /ping so AgentCore can track idle time.
// Initialized to startup time; updated on each chat/warmup invocation.
let lastActivityTime = Math.floor(Date.now() / 1000);

function createRuntimeInvocationAdmission({
  sessionBinding = new SessionBinding(),
  handlers = {
    status: (context) => context,
    warmup: (context) => context,
    chat: (context) => context,
  },
} = {}) {
  return createInvocationHandler({ sessionBinding, handlers });
}

function hashBoundInvocation({ identity, delivery, request } = {}) {
  const gatewayRunId = gatewayInvocation.deriveGatewayRunId({
    invocationId: request?.invocationId,
  });
  const requestHash = gatewayInvocation.hashTrustedInvocationRequest({
    // RH1's frozen canonical hash schema names this slot `userId`. Its value
    // is exclusively the internal identity retained by SessionBinding.
    userId: identity?.internalUserId,
    actorId: delivery?.actorId || null,
    channel: delivery?.channel || null,
    message: request?.message,
  });
  return Object.freeze({ gatewayRunId, requestHash });
}

const runtimeInvocationHandler = createRuntimeInvocationAdmission();

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
  if (ready && !gatewayQuarantined) {
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
  if (shuttingDown || gatewayQuarantined) return;
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
    if (shuttingDown || openclawReady || gatewayQuarantined) return;
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
 * Mints scoped workspace credentials, then starts proxy, OpenClaw, and
 * workspace restore. Only waits for proxy readiness (~5s), then returns.
 * OpenClaw readiness is polled in the background.
 */
function quarantineForCredentialFailure(error) {
  gatewayQuarantined = Object.freeze({
    code: "SCOPED_CREDENTIAL_FAILURE",
    message: "Scoped workspace credentials could not be maintained",
  });
  openclawReady = false;
  proxyReady = false;
  if (credentialRefreshTimer) {
    clearInterval(credentialRefreshTimer);
    credentialRefreshTimer = null;
  }
  void workspaceSync.cleanup(null).catch(() => {});
  for (const child of [openclawProcess, proxyProcess]) {
    try {
      child?.kill("SIGTERM");
    } catch {}
  }
  console.error(`[contract] Fatal scoped credential failure: ${error.message}`);
  const fatalExitTimer = setTimeout(() => process.exit(1), 2_000);
  fatalExitTimer.unref();
}

async function init(internalUserId, namespace) {
  if (proxyReady) return; // Already initialized
  if (initInProgress) return initPromise;
  initInProgress = true;

  initPromise = (async () => {
    console.log("[contract] Creating scoped S3 credentials");
    const credentials = await scopedCreds.createScopedCredentials(namespace);
    const credentialFiles = scopedCreds.writeCredentialFiles(
      credentials,
      SCOPED_CREDS_DIR,
    );
    workspaceSync.configureCredentials(credentials);

    const scopedEnvironment = scopedCreds.buildOpenClawEnv({
      credDir: SCOPED_CREDS_DIR,
      baseEnv: process.env,
    });
    const workspaceEnvironment = {
      AWS_REGION: RUNTIME_REGION,
      AWS_DEFAULT_REGION: RUNTIME_REGION,
      S3_USER_FILES_BUCKET: process.env.S3_USER_FILES_BUCKET,
      PERSONAL_OPERATOR_WORKSPACE_PREFIX: namespace,
      PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE:
        credentialFiles.credentialsPath,
    };
    await agent.configureWorkspaceRuntime({ env: workspaceEnvironment });

    currentInternalUserId = internalUserId;
    currentNamespace = namespace;
    await cwLogger.init(`${namespace}-${Date.now()}`);
    console.log(`[contract] Init for internalUserId=${internalUserId}`);

    if (credentialRefreshTimer) clearInterval(credentialRefreshTimer);
    credentialRefreshTimer = setInterval(async () => {
      try {
        const refreshed = await scopedCreds.createScopedCredentials(namespace);
        scopedCreds.writeCredentialFiles(refreshed, SCOPED_CREDS_DIR);
        workspaceSync.configureCredentials(refreshed);
        console.log("[contract] Scoped S3 credentials refreshed");
      } catch (error) {
        quarantineForCredentialFailure(error);
      }
    }, 45 * 60 * 1000);

    // 1c. Clean up stale lock files restored from S3 (non-blocking)
    // Runs in parallel with proxy startup — does not block init.
    const lockCleanupPromise = cleanupLockFiles().catch((err) => {
      console.warn(`[contract] Lock cleanup failed: ${err.message}`);
    });

    console.log("[contract] Starting Bedrock proxy...");
    const proxyEnv = runtimePolicy.buildProxyChildEnv({
      baseEnv: process.env,
      internalUserId,
      namespace,
      scopedCredentialsFile: credentialFiles.credentialsPath,
    });
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
    quarantineForCredentialFailure(err);
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
 * Submit bounded serialized gateway work.
 * @param {string} message - The message to send
 * @param {function} [onDelta] - Optional callback invoked with cumulative text on each delta
 */
function enqueueMessage(message, runId, onDelta) {
  return gatewayMessageExecutor.submit(() =>
    bridgeMessage(message, 620000, onDelta, runId),
  );
}

/**
 * Bridge a chat message to OpenClaw via WebSocket and collect the response.
 * @param {string} message - The message to send
 * @param {number} timeoutMs - Timeout in milliseconds
 * @param {function} [onDelta] - Optional callback invoked with cumulative text on each delta
 */
async function bridgeMessage(message, timeoutMs = 620000, onDelta, runId) {
  const wsUrl = `ws://127.0.0.1:${OPENCLAW_PORT}`;
  console.log(`[contract] Invoking committed agent state machine: ${wsUrl}`);
  return gatewayInvocation.invokeGatewayAgent({
    WebSocketConstructor: WebSocket,
    url: wsUrl,
    token: GATEWAY_TOKEN,
    message,
    runId,
    timeoutMs,
    onDelta,
    logger: console,
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
        const action = payload.action === undefined ? "status" : payload.action;
        let boundInvocation;
        try {
          boundInvocation = runtimeInvocationHandler.handle(payload);
        } catch (identityError) {
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(
            JSON.stringify({
              response: "This invocation does not match the bound runtime identity.",
              status: "failed",
              errorCode:
                identityError.code || "INVALID_SESSION_IDENTITY",
            }),
          );
          return;
        }
        const { identity, delivery, request } = boundInvocation;

        if (action === "status") {
          const proxyHealth = await checkProxyHealth();

          const diag = {
            buildVersion: BUILD_VERSION,
            uptime_seconds: Math.floor((Date.now() - startTime) / 1000),
            currentInternalUserId,
            boundInternalUserId: identity.internalUserId,
            openclawReady,
            gatewayQuarantined,
            proxyReady,
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
          if (gatewayQuarantined) {
            res.writeHead(200, { "Content-Type": "application/json" });
            res.end(
              JSON.stringify({
                status: "quarantined",
                errorCode: "AGENT_RUNTIME_QUARANTINED",
              }),
            );
            return;
          }

          lastActivityTime = Math.floor(Date.now() / 1000);
          if (openclawReady && proxyReady) {
            res.writeHead(200, { "Content-Type": "application/json" });
            res.end(JSON.stringify({ status: "ready" }));
            return;
          }
          // Trigger init in background if not already running
          if (!initInProgress) {
            init(identity.internalUserId, identity.namespace).catch((err) => {
              console.error(`[contract] Warmup init failed: ${err.message}`);
            });
          }
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ status: "initializing" }));
          return;
        }

        // Chat action — lazy init and bridge
        if (action === "chat") {
          const { message, invocationId } = request;
          if (message === undefined) {
            res.writeHead(400, { "Content-Type": "application/json" });
            res.end(
              JSON.stringify({ error: "Missing message" }),
            );
            return;
          }

          let gatewayRunId;
          let requestHash;
          try {
            ({ gatewayRunId, requestHash } = hashBoundInvocation(
              boundInvocation,
            ));
          } catch {
            res.writeHead(200, { "Content-Type": "application/json" });
            res.end(
              JSON.stringify({
                response:
                  "This request has no valid trusted invocation identity and was not executed.",
                internalUserId: identity.internalUserId,
                status: "failed",
                errorCode: "INVALID_INVOCATION_IDENTITY",
              }),
            );
            return;
          }

          let responseText;
          let responseStatus;
          let responseErrorCode;
          try {
            const outcome = await trustedInvocationRegistry.invoke({
              invocationId,
              requestHash,
              execute: async () => {
                if (gatewayQuarantined) {
                  return {
                    responseText:
                      "This runtime is quarantined because an earlier request could not be reconciled. Start a fresh runtime before sending more work.",
                    status: "quarantined",
                    errorCode: "AGENT_RUNTIME_QUARANTINED",
                  };
                }

                if (!proxyReady && !initInProgress) {
                  try {
                    await init(identity.internalUserId, identity.namespace);
                  } catch (err) {
                    console.error(`[contract] Init failed: ${err.message}`);
                    return {
                      responseText:
                        "I'm having trouble starting up. Please try again in a moment.",
                      status: "failed",
                      errorCode: "RUNTIME_INIT_FAILED",
                    };
                  }
                } else if (!proxyReady && initInProgress) {
                  try {
                    await initPromise;
                  } catch (err) {
                    console.error(
                      `[contract] Init (in-progress) failed: ${err.message}`,
                    );
                    return {
                      responseText:
                        "I'm still starting up. Please try again in a moment.",
                      status: "failed",
                      errorCode: "RUNTIME_INIT_FAILED",
                    };
                  }
                }

                const bridgeText = buildBridgeText(message);
                lastActivityTime = Math.floor(Date.now() / 1000);
                activeTaskCount++;
                let executionText;
                let executionStatus;
                let executionErrorCode;
                try {
                  if (openclawReady) {
                    try {
                      const gatewayOutcome =
                        await gatewayRuntimeBoundary.invoke({
                          invokePrimary: () =>
                            enqueueMessage(bridgeText, gatewayRunId),
                          invokeFallback: () =>
                            agent.chat(
                              bridgeText,
                              Date.now() + 30000,
                            ),
                        });
                      executionText = gatewayOutcome.text;
                    } catch (bridgeErr) {
                      gatewayQuarantined =
                        gatewayRuntimeBoundary.getQuarantine();
                      if (gatewayQuarantined) openclawReady = false;
                      console.error(
                        `[contract] Committed gateway invocation failed: ${bridgeErr.code || "UNKNOWN"} ${bridgeErr.message}`,
                      );
                      executionErrorCode =
                        bridgeErr.code || "GATEWAY_INVOCATION_FAILED";
                      if (bridgeErr.code === "UNCERTAIN_AGENT_RUN") {
                        executionStatus = "uncertain";
                        executionText =
                          "I couldn't confirm whether that request stopped, so I won't run it again. Check its status before retrying.";
                      } else {
                        executionStatus = "failed";
                        executionText =
                          "The request failed without a committed response and was not run again.";
                      }
                    }
                  } else if (proxyReady) {
                    console.log(
                      "[contract] Routing via lightweight agent (warm-up)",
                    );
                    try {
                      executionText = await agent.chat(
                        bridgeText,
                        Date.now() + 620000,
                      );
                    } catch (agentErr) {
                      executionText =
                        "I'm having trouble right now. Please try again in a moment.";
                      executionStatus = "failed";
                      executionErrorCode = "LIGHTWEIGHT_AGENT_FAILED";
                      console.error(
                        `[contract] Lightweight agent error: ${agentErr.message}`,
                      );
                    }
                  } else {
                    executionText =
                      "I'm starting up — please try again in a moment.";
                    executionStatus = "failed";
                    executionErrorCode = "RUNTIME_NOT_READY";
                  }
                } finally {
                  activeTaskCount = Math.max(0, activeTaskCount - 1);
                }

                if (executionText) {
                  executionText = extractTextFromContent(executionText);
                }
                return {
                  responseText: executionText,
                  ...(executionStatus ? { status: executionStatus } : {}),
                  ...(executionErrorCode
                    ? { errorCode: executionErrorCode }
                    : {}),
                };
              },
            });
            responseText = outcome.responseText;
            responseStatus = outcome.status;
            responseErrorCode = outcome.errorCode;
          } catch (invocationErr) {
            console.error(
              `[contract] Trusted invocation rejected: ${invocationErr.code || "UNKNOWN"} ${invocationErr.message}`,
            );
            responseText =
              invocationErr.code === "INVOCATION_ID_CONFLICT"
                ? "This request identity was already used for different work and was not executed."
                : "This request could not be executed safely.";
            responseStatus = "failed";
            responseErrorCode =
              invocationErr.code || "TRUSTED_INVOCATION_FAILED";
          }

          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(
            JSON.stringify({
              response: responseText,
              internalUserId: identity.internalUserId,
              ...(responseStatus ? { status: responseStatus } : {}),
              ...(responseErrorCode ? { errorCode: responseErrorCode } : {}),
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
async function shutdownRuntime() {
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
}

function startContractServer() {
  process.once("SIGTERM", shutdownRuntime);
  return server.listen(PORT, "0.0.0.0", () => {
    console.log(
      `[contract] AgentCore contract server listening on http://0.0.0.0:${PORT} (per-user session mode)`,
    );
    console.log(
      "[contract] Endpoints: GET /ping, POST /invocations {action: chat|status|warmup}",
    );
  });
}

if (require.main === module) {
  startContractServer();
}

module.exports = {
  createRuntimeInvocationAdmission,
  hashBoundInvocation,
  startContractServer,
};
