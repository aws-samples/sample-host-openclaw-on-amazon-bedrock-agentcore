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
 *   4. Once OpenClaw is ready (~2-4 min), route via WebSocket bridge
 *
 * The lightweight agent handles messages immediately while OpenClaw starts.
 * Once OpenClaw is ready, all subsequent messages route through it seamlessly.
 *
 * Runs on port 8080 (required by AgentCore Runtime).
 */

const http = require("http");
const { spawn } = require("child_process");
const WebSocket = require("ws");
const {
  SecretsManagerClient,
  GetSecretValueCommand,
} = require("@aws-sdk/client-secrets-manager");
const workspaceSync = require("./workspace-sync");
const agent = require("./lightweight-agent");

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
const BUILD_VERSION = "v46"; // Bump in cdk.json to force container redeploy

// OpenClaw process diagnostics (last N lines of stdout/stderr)
const OPENCLAW_LOG_LIMIT = 50;
let openclawLogs = [];
let openclawExitCode = null;

// Message queue for serializing concurrent requests (OpenClaw WebSocket path)
let messageQueue = [];
let processingMessage = false;

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

/**
 * Write a headless OpenClaw config (no channels — messages bridged via WebSocket).
 * Full tool profile with deny list for unsafe/irrelevant tools.
 * Sub-agents enabled for deep-research-pro and task-decomposer skills.
 * Sandbox disabled — AgentCore microVMs provide per-user isolation.
 */
function writeOpenClawConfig() {
  const fs = require("fs");

  // Sub-agent model: defaults to main model, configurable via SUBAGENT_MODEL env var
  const subagentModel =
    process.env.SUBAGENT_MODEL || "agentcore/bedrock-agentcore";

  const config = {
    models: {
      providers: {
        agentcore: {
          baseUrl: `http://127.0.0.1:${PROXY_PORT}/v1`,
          apiKey: "local",
          api: "openai-completions",
          models: [{ id: "bedrock-agentcore", name: "Bedrock AgentCore" }],
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
      deny: [
        "write", // Local writes don't persist — use S3 skill instead
        "edit", // Local edits are ephemeral — use S3 skill instead
        "apply_patch", // Code patching not needed for chat assistant
        "browser", // No headless browser in ARM64 container
        "canvas", // No UI rendering in headless chat context
        "cron", // EventBridge handles scheduling, not OpenClaw's built-in cron
        "gateway", // Admin tool — not needed for end users
      ],
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
  // Always overwrite: this is system-managed content that must match the current image version.
  // Skills/instructions may change between image versions, and stale AGENTS.md from S3 workspace
  // restore would cause the bot to not know about newly added skills.
  const agentsMdPath = `${homeDir}/.openclaw/AGENTS.md`;
  fs.writeFileSync(
    agentsMdPath,
    [
      "# Agent Instructions",
      "",
      "You are a helpful AI assistant running in a per-user container on AWS.",
      "You have built-in web tools, file storage, scheduling, and many community skills.",
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
      "You have the **eventbridge-cron** skill for scheduling tasks. When users ask to:",
      "- Set up reminders, alarms, or scheduled messages",
      "- Create recurring tasks or cron jobs",
      "- Schedule daily, weekly, or periodic actions",
      "",
      "**Read the eventbridge-cron SKILL.md and use it.** Do NOT say cron is disabled.",
      "The built-in cron is replaced by Amazon EventBridge Scheduler (more reliable, persists across sessions).",
      "",
      "Always ask the user for their **timezone** if you don't know it (e.g., Asia/Shanghai, America/New_York).",
      "",
      "## File Storage",
      "",
      "You have the **s3-user-files** skill for persistent file storage. Files survive across sessions.",
      "",
      "## Cost Analysis",
      "",
      "You have the **cost-analyzer** skill for AWS cost analysis.",
      "**Always use this skill when users ask about costs, spending, usage, or billing.**",
      "Do NOT use `aws ce` CLI commands directly — the skill provides much richer data:",
      "- Cross-references Cost Explorer, CloudWatch Bedrock logs, and DynamoDB token usage",
      "- Provides per-user token breakdown and cost ranking",
      "- Generates a structured report with actionable recommendations",
      "",
      "**Read the cost-analyzer SKILL.md and use it.** Do NOT attempt manual AWS CLI cost queries.",
      "**IMPORTANT**: After running cost-analyzer, include the COMPLETE output in your response.",
      "The user can only see your messages — tool outputs are NOT visible to them.",
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
      "## Sub-agents",
      "",
      "Skills like deep-research-pro and task-decomposer can spawn sub-agents for parallel work.",
      "Sub-agents share the same model and capabilities. Sandbox is disabled (the container is already isolated).",
      "",
    ].join("\n"),
  );
  console.log("[contract] AGENTS.md written");
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

    // 1b. Clean up stale lock files restored from S3 (non-blocking)
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
      USER_ID: actorId,
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
    // Set OPENCLAW_SKIP_CRON in parent env so OpenClaw gateway inherits it
    process.env.OPENCLAW_SKIP_CRON = "1";
    openclawProcess = spawn(
      "openclaw",
      ["gateway", "run", "--port", String(OPENCLAW_PORT), "--verbose"],
      { stdio: ["ignore", "pipe", "pipe"] },
    );
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
 * Check if a string looks like (possibly truncated) content block JSON.
 */
function looksLikeContentBlockJson(str) {
  if (typeof str !== "string") return false;
  const t = str.trim();
  return t.startsWith("[{") || t.startsWith("{\"type\"");
}

/**
 * Extract text from a truncated/partial content block JSON string.
 * When streaming, OpenClaw may send progressively built content blocks like:
 *   '[{"type":"text","text":"partial report text here...'
 * This extracts the text value even when the JSON is incomplete.
 * Returns empty string if no text content is found.
 */
function extractFromPartialContentBlock(str) {
  if (typeof str !== "string") return "";
  // Match the text value in a content block — captures everything after "text":"
  // up to end of string (for truncated) or closing quote (for partial)
  const match = str.match(/"text"\s*:\s*"((?:[^"\\]|\\.)*)(?:"|$)/g);
  if (!match || match.length < 2) {
    // Need at least 2 matches: "type":"text" and "text":"<content>"
    // If only one match, it might be just the type field
    if (match && match.length === 1) {
      // Check if this single match is the content text (not the type value)
      const single = match[0];
      const val = single.replace(/^"text"\s*:\s*"/, "").replace(/"$/, "");
      // If the value is "text" itself, it's the type field, not content
      if (val === "text") return "";
      return val;
    }
    return "";
  }
  // The second "text":"..." match is the content (first is type:"text")
  const contentMatch = match[match.length - 1];
  const val = contentMatch.replace(/^"text"\s*:\s*"/, "").replace(/"$/, "");
  // Unescape JSON string escapes
  try {
    return JSON.parse('"' + val + '"');
  } catch {
    return val.replace(/\\n/g, "\n").replace(/\\"/g, '"').replace(/\\\\/g, "\\");
  }
}

/**
 * Extract plain text from message content. Handles all formats OpenClaw may send:
 *   - Parsed array of content blocks: [{type:"text", text:"..."}, ...]
 *   - JSON-serialized content blocks (string): '[{"type":"text","text":"..."}]'
 *   - Single content block object: {type:"text", text:"..."}
 *   - Plain text string
 *   - Truncated content block JSON (during streaming)
 * Recursively unwraps nested content blocks (e.g., content block containing
 * a JSON string of more content blocks).
 */
function extractTextFromContent(content) {
  if (!content) return "";
  // Already a parsed array of content blocks
  if (Array.isArray(content)) {
    const texts = content
      .filter((b) => b && b.type === "text" && typeof b.text === "string")
      .map((b) => b.text);
    if (texts.length > 0) {
      const joined = texts.join("");
      // Recursively unwrap if the text itself is a JSON content block array
      const unwrapped = unwrapContentBlocks(joined);
      if (unwrapped) return unwrapped;
      // Try to extract partial text from truncated content block JSON
      if (looksLikeContentBlockJson(joined)) return extractFromPartialContentBlock(joined);
      return joined;
    }
    return "";
  }
  // Single content block object
  if (typeof content === "object" && content.type === "text" && typeof content.text === "string") {
    const unwrapped = unwrapContentBlocks(content.text);
    if (unwrapped) return unwrapped;
    // Try to extract partial text from truncated content block JSON
    if (looksLikeContentBlockJson(content.text)) return extractFromPartialContentBlock(content.text);
    return content.text;
  }
  if (typeof content === "string") {
    // Try to parse as JSON content blocks
    const unwrapped = unwrapContentBlocks(content);
    if (unwrapped) return unwrapped;
    // Try to extract partial text from truncated content block JSON
    if (looksLikeContentBlockJson(content)) return extractFromPartialContentBlock(content);
    // Plain text string
    return content;
  }
  return "";
}

/**
 * Detect OpenClaw's NO_REPLY marker — sent as the final assistant message
 * when the response was already delivered in a previous conversation turn
 * (e.g., after multi-turn tool use).
 *
 * Handles multiple forms:
 *   - Plain text: "NO_REPLY", "NO_", "NO"
 *   - Content block wrapped: '[{"type":"text","text":"NO_REPLY"}]'
 *   - Truncated content block: '[{"type":"text","text":"NO' (streaming partial)
 */
function isNoReplyMarker(text) {
  if (typeof text !== "string") return false;
  const t = text.trim();
  if (!t) return false;
  // Plain text match — full "NO_REPLY" or any streaming prefix of it
  // During progressive streaming, deltas accumulate: "N" → "NO" → "NO_" → ... → "NO_REPLY"
  if ("NO_REPLY".startsWith(t) || t === "NO_REPLY") {
    return true;
  }
  // Content block wrapped — try to parse and check inner text
  if (t.startsWith("[")) {
    try {
      const parsed = JSON.parse(t);
      if (Array.isArray(parsed) && parsed.length === 1 && parsed[0]?.type === "text") {
        const inner = (parsed[0].text || "").trim();
        return inner.length > 0 && ("NO_REPLY".startsWith(inner) || inner === "NO_REPLY");
      }
    } catch {
      // Truncated content block — check if it's a partial NO_REPLY
      const match = t.match(/"text"\s*:\s*"(NO[_A-Z]*)/);
      if (match && "NO_REPLY".startsWith(match[1])) return true;
    }
  }
  return false;
}

/**
 * If a string looks like JSON content blocks, parse and extract text.
 * Returns the extracted text, or empty string if not content blocks.
 */
function unwrapContentBlocks(str) {
  if (typeof str !== "string") return "";
  const trimmed = str.trim();
  if (!trimmed.startsWith("[")) return "";
  try {
    const parsed = JSON.parse(trimmed);
    if (Array.isArray(parsed) && parsed.length > 0 && parsed[0] && typeof parsed[0].type === "string") {
      const texts = parsed
        .filter((b) => b.type === "text" && typeof b.text === "string")
        .map((b) => b.text);
      return texts.length > 0 ? texts.join("") : "";
    }
  } catch {}
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
      const response = await bridgeMessage(message, 480000);
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
async function bridgeMessage(message, timeoutMs = 240000) {
  const { randomUUID } = require("crypto");
  return new Promise((resolve) => {
    const wsUrl = `ws://127.0.0.1:${OPENCLAW_PORT}`;
    console.log(`[contract] Connecting to WebSocket: ${wsUrl}`);
    const ws = new WebSocket(wsUrl, {
      headers: { Origin: `http://127.0.0.1:${OPENCLAW_PORT}` },
    });
    let responseText = "";
    let lastSubstantiveText = ""; // Track last non-marker response for NO_REPLY fallback
    let deltaCount = 0;
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
      console.log(
        `[contract] WebSocket timeout after ${timeoutMs}ms (auth=${authenticated}, chatSent=${chatSent})`,
      );
      const debugInfo =
        unhandledMsgs.length > 0
          ? ` unhandled=[${unhandledMsgs.slice(0, 5).join(" | ")}]`
          : "";
      done(
        responseText ||
          `Timeout (auth=${authenticated}, chat=${chatSent})${debugInfo}`,
      );
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

      // Step 3: Chat events — state: "delta" (streaming) or "final" (complete)
      // OpenClaw puts content in payload.message.content (usual) or
      // directly in payload.message (string or content-blocks array).
      if (msg.type === "event" && msg.event === "chat") {
        const payload = msg.payload || {};
        const msgContent = payload.message?.content;

        if (payload.state === "delta") {
          const text =
            extractTextFromContent(msgContent) ||
            extractTextFromContent(payload.message);
          if (text) {
            const marker = isNoReplyMarker(text);
            responseText = text; // Delta replaces (accumulates progressively)
            // Track last substantive text (skip NO_REPLY marker used by OpenClaw
            // to signal "response was already sent in a previous turn")
            if (!marker) {
              lastSubstantiveText = text;
              deltaCount++;
              // Log periodically (first delta, and every 20th update)
              if (deltaCount === 1 || deltaCount % 20 === 0) {
                console.log(`[contract] Delta #${deltaCount}: ${text.length} chars, substantive=${lastSubstantiveText.length} chars`);
              }
            }
          }
          return;
        }

        if (payload.state === "final") {
          // Final message may include the complete text
          const text =
            extractTextFromContent(msgContent) ||
            extractTextFromContent(payload.message);
          if (text) {
            responseText = text;
            if (!isNoReplyMarker(text)) lastSubstantiveText = text;
          }
          // If final response is a NO_REPLY marker, use the last substantive response
          const isMarker = isNoReplyMarker(responseText);
          const effectiveText = isMarker ? lastSubstantiveText : responseText;
          console.log(`[contract] Chat final: response=${responseText.length}ch marker=${isMarker} substantive=${lastSubstantiveText.length}ch effective=${effectiveText.length}ch deltas=${deltaCount}`);
          if (effectiveText.length < 50) {
            console.log(`[contract] Chat final raw: ${JSON.stringify(effectiveText)}`);
          }
          done(effectiveText || "Message processed.");
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
        // "final" or "done" = completed
        done(responseText || "Message processed (no streaming content).");
        return;
      }

      // Unhandled message — log for debugging
      unhandledMsgs.push(raw.slice(0, 300));
    });

    ws.on("error", (err) => {
      console.error(`[contract] WebSocket error: ${err.message}`);
      done(responseText || `Connection error: ${err.message}`);
    });

    ws.on("close", (code, reason) => {
      const reasonStr = reason ? reason.toString() : "";
      console.log(
        `[contract] WebSocket closed: code=${code} reason=${reasonStr} auth=${authenticated} chatSent=${chatSent}`,
      );
      const debugInfo =
        unhandledMsgs.length > 0
          ? ` unhandled=[${unhandledMsgs.slice(0, 3).join(" | ")}]`
          : "";
      done(
        responseText ||
          `WS closed (code=${code}, reason=${reasonStr})${debugInfo}`,
      );
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
 * Detect if a message is a cost analysis request.
 * Returns true for messages asking about costs, spending, billing, or usage reports.
 */
function isCostAnalysisRequest(text) {
  if (typeof text !== "string") return false;
  // Strip image marker if present (from buildBridgeText)
  const idx = text.indexOf("[OPENCLAW_IMAGES:");
  const cleanText = idx >= 0 ? text.slice(0, idx) : text;
  const lower = cleanText.toLowerCase().trim();
  if (!lower) return false;

  const patterns = [
    /\bcost\s*(report|analysis|breakdown|summary|overview)\b/,
    /\b(analyze|show|check|get|run|give)\b.*\bcost/,
    /\bhow\s+much\b.*\b(spent|cost|spend|spending)\b/,
    /\bspend(ing)?\s*(report|analysis|breakdown|summary|overview)\b/,
    /\b(my|our|the)\s+(costs?|spending|billing)\b/,
    /\bbilling\s*(report|analysis|breakdown|summary|overview)\b/,
    /\busage\s*(report|analysis|breakdown|summary|overview)\b/,
    /\bcost.?analyzer\b/,
  ];

  return patterns.some((p) => p.test(lower));
}

/**
 * Extract the number of days to analyze from a message.
 */
function extractDaysFromMessage(text) {
  if (typeof text !== "string") return 7;
  const lower = text.toLowerCase();

  const dayMatch = lower.match(/(\d+)\s*days?/);
  if (dayMatch) return Math.min(parseInt(dayMatch[1], 10), 90);

  if (lower.includes("yesterday") || lower.includes("today")) return 1;
  if (lower.includes("this week") || lower.includes("last week")) return 7;
  if (lower.includes("this month") || lower.includes("last month")) return 30;

  return 7;
}

/**
 * Run the cost analyzer directly as a child process (bypasses OpenClaw).
 *
 * OpenClaw only streams the final assistant turn via WebSocket, which is
 * "NO_REPLY" for multi-turn tool use. The actual cost report is generated in
 * intermediate turns that never reach the bridge. Running the cost-analyzer
 * directly avoids this limitation entirely.
 */
function runCostAnalyzerDirectly(namespace, days, timeoutMs = 300000) {
  return new Promise((resolve) => {
    const fs = require("fs");
    const scriptPath = "/skills/cost-analyzer/run.js";

    if (!fs.existsSync(scriptPath)) {
      console.warn("[contract] Cost analyzer script not found at " + scriptPath);
      resolve(null); // null = not available, fall back to normal routing
      return;
    }

    console.log(
      `[contract] Running cost analyzer directly: user=${namespace} days=${days} timeout=${timeoutMs}ms`,
    );

    let stdout = "";
    let stderr = "";

    const child = spawn("node", [scriptPath, namespace, String(days)], {
      env: { ...process.env, CLAUDE_CODE_USE_BEDROCK: "1" },
    });

    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });

    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });

    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };

    child.on("error", (err) => {
      console.error(`[contract] Cost analyzer spawn error: ${err.message}`);
      finish(null); // null = fall back to normal routing
    });

    child.on("close", (code) => {
      // Log stderr summary (last few lines for debugging)
      const stderrLines = stderr.split("\n").filter((l) => l.trim());
      if (stderrLines.length > 0) {
        const last = stderrLines.slice(-5);
        for (const line of last) {
          console.log(`[cost-analyzer:err] ${line}`);
        }
      }
      console.log(
        `[contract] Cost analyzer exited: code=${code} stdout=${stdout.length}ch stderr=${stderr.length}ch`,
      );

      if (stdout.trim()) {
        finish(stdout.trim());
      } else {
        finish(null); // No output — fall back to normal routing
      }
    });

    const timer = setTimeout(() => {
      console.warn(`[contract] Cost analyzer timeout after ${timeoutMs}ms`);
      try {
        child.kill("SIGTERM");
      } catch {}
      // Give 5s for graceful shutdown, then SIGKILL
      setTimeout(() => {
        try {
          child.kill("SIGKILL");
        } catch {}
      }, 5000);
      if (stdout.trim()) {
        finish(stdout.trim()); // Return partial output
      } else {
        finish("Cost analysis timed out. Please try again later.");
      }
    }, timeoutMs);
  });
}

/**
 * AgentCore contract HTTP server.
 */
const server = http.createServer(async (req, res) => {
  // GET /ping — AgentCore health check
  if (req.method === "GET" && req.url === "/ping") {
    // Return Healthy (not HealthyBusy) — allows natural idle termination.
    // Per-user sessions should terminate when idle.
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        status: "Healthy",
        time_of_last_update: Math.floor(Date.now() / 1000),
      }),
    );
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
          };
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ response: JSON.stringify(diag) }));
          return;
        }

        // Warmup action — trigger lazy init without blocking for a chat response
        if (action === "warmup") {
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

          // Enqueue message (serialized with chat messages to prevent WebSocket races)
          let responseText;
          try {
            responseText = await enqueueMessage(message);
          } catch (bridgeErr) {
            responseText = `Bridge error: ${bridgeErr.message}`;
          }

          // Unwrap content blocks (same safety net as chat action)
          if (responseText) {
            const cronUnwrapped = unwrapContentBlocks(responseText);
            if (cronUnwrapped) responseText = cronUnwrapped;
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

          // Route based on readiness: OpenClaw (full) > lightweight agent (shim)
          // Cost analysis requests bypass OpenClaw entirely (direct invocation).
          let responseText;

          if (isCostAnalysisRequest(bridgeText)) {
            // Direct cost analysis — bypasses OpenClaw WebSocket bridge.
            // OpenClaw only streams the final assistant turn via WebSocket, which
            // is "NO_REPLY" for multi-turn tool use. The actual cost report is
            // generated in intermediate turns that never reach the bridge.
            console.log(
              "[contract] Cost analysis detected — running directly (bypassing OpenClaw)",
            );
            const costDays = extractDaysFromMessage(bridgeText);
            const namespace = actorId.replace(/:/g, "_");
            responseText = await runCostAnalyzerDirectly(
              namespace,
              costDays,
              300000,
            );
            // null = script not available, fall through to normal routing
          }

          if (!responseText && openclawReady) {
            // Full OpenClaw path — WebSocket bridge
            try {
              responseText = await enqueueMessage(bridgeText);
            } catch (bridgeErr) {
              console.error(
                `[contract] Bridge error, falling back to shim: ${bridgeErr.message}`,
              );
              // Fall back to lightweight agent on bridge failure
              responseText = await agent.chat(bridgeText, actorId);
            }
          } else if (!responseText && proxyReady) {
            // Warm-up shim path — lightweight agent via proxy
            console.log("[contract] Routing via lightweight agent (warm-up)");
            try {
              responseText = await agent.chat(bridgeText, actorId);
            } catch (agentErr) {
              responseText = `I'm having trouble right now. Please try again in a moment.`;
              console.error(
                `[contract] Lightweight agent error: ${agentErr.message}`,
              );
            }
          } else if (!responseText) {
            // Proxy not ready yet (should be rare — init awaits proxy)
            responseText = "I'm starting up — please try again in a moment.";
          }

          // Final safety net: unwrap any remaining content block JSON in the response.
          // OpenClaw sometimes returns responses wrapped in content blocks even after
          // extractTextFromContent() processing in the WebSocket bridge.
          if (responseText) {
            const finalUnwrapped = unwrapContentBlocks(responseText);
            if (finalUnwrapped) responseText = finalUnwrapped;
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
