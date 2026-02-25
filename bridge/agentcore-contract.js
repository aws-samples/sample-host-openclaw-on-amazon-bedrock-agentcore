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
 * Optimized for fast startup: no bundled skills, basic tool profile, no control UI.
 */
function writeOpenClawConfig() {
  const fs = require("fs");
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
      },
    },
    tools: {
      profile: "basic",
      deny: ["write", "edit", "apply_patch", "browser", "canvas", "automation"],
    },
    skills: {
      allowBundled: [],
      load: { extraDirs: ["/skills"] },
    },
    gateway: {
      mode: "local",
      port: OPENCLAW_PORT,
      bind: "lan",
      trustedProxies: ["0.0.0.0/0"],
      auth: { mode: "token", token: GATEWAY_TOKEN },
      controlUi: { enabled: false },
    },
    skipBootstrap: true,
    channels: {}, // No channels — messages bridged via WebSocket
  };

  const homeDir = process.env.HOME || "/root";
  fs.mkdirSync(`${homeDir}/.openclaw`, { recursive: true });
  fs.writeFileSync(
    `${homeDir}/.openclaw/openclaw.json`,
    JSON.stringify(config, null, 2),
  );
  console.log("[contract] OpenClaw headless config written (optimized)");

  // Write AGENTS.md — OpenClaw loads this as workspace bootstrap instructions.
  // Only write if not already present (workspace restore from S3 may have a user-customized version).
  const agentsMdPath = `${homeDir}/.openclaw/AGENTS.md`;
  if (!fs.existsSync(agentsMdPath)) {
    fs.writeFileSync(
      agentsMdPath,
      [
        "# Agent Instructions",
        "",
        "You are a helpful AI assistant running in a per-user container on AWS.",
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

    // 1. Start proxy, OpenClaw, and workspace restore in parallel
    console.log("[contract] Starting parallel initialization...");

    // 1b. Clean up stale lock files from S3 restore (prevents "session file locked" errors)
    try {
      const lockDir = `${process.env.HOME || "/root"}/.openclaw`;
      const { readdirSync, unlinkSync, statSync } = require("fs");
      const { join } = require("path");
      const removeLocks = (dir) => {
        try {
          for (const entry of readdirSync(dir)) {
            const full = join(dir, entry);
            const st = statSync(full, { throwIfNoEntry: false });
            if (!st) continue;
            if (st.isDirectory()) removeLocks(full);
            else if (entry.endsWith(".lock")) unlinkSync(full);
          }
        } catch (_) {
          /* ignore */
        }
      };
      removeLocks(lockDir);
      console.log("[contract] Cleaned up stale lock files");
    } catch (err) {
      console.warn(`[contract] Lock cleanup failed: ${err.message}`);
    }

    // Start the Bedrock proxy with user identity env vars
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

    // Write OpenClaw config and start gateway (non-blocking)
    writeOpenClawConfig();
    console.log(
      "[contract] Starting OpenClaw gateway (headless, background)...",
    );
    // Set OPENCLAW_SKIP_CRON in parent env so OpenClaw gateway inherits it
    process.env.OPENCLAW_SKIP_CRON = "1";
    openclawProcess = spawn(
      "openclaw",
      [
        "gateway",
        "run",
        "--port",
        String(OPENCLAW_PORT),
        "--bind",
        "lan",
        "--verbose",
      ],
      { stdio: "inherit" },
    );
    openclawProcess.on("exit", (code) => {
      console.log(`[contract] OpenClaw exited with code ${code}`);
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
 * blocks, or JSON-serialized array of content blocks.
 */
function extractTextFromContent(content) {
  if (!content) return "";
  // Already a parsed array of content blocks
  if (Array.isArray(content)) {
    return content
      .filter((b) => b.type === "text")
      .map((b) => b.text)
      .join("");
  }
  if (typeof content === "string") {
    // Check if the string is a JSON-serialized array of content blocks
    if (content.startsWith("[{")) {
      try {
        const parsed = JSON.parse(content);
        if (Array.isArray(parsed)) {
          return parsed
            .filter((b) => b.type === "text")
            .map((b) => b.text)
            .join("");
        }
      } catch {}
    }
    // Plain text string
    return content;
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
      const response = await bridgeMessage(message, 120000);
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
          if (text) responseText = text; // Delta replaces (accumulates progressively)
          return;
        }

        if (payload.state === "final") {
          // Final message may include the complete text
          const text =
            extractTextFromContent(msgContent) ||
            extractTextFromContent(payload.message);
          if (text) responseText = text;
          console.log(`[contract] Chat final (${responseText.length} chars)`);
          done(responseText || "Message processed.");
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
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(
            JSON.stringify({
              status: "running",
              uptime_seconds: Math.floor((Date.now() - startTime) / 1000),
              currentUserId,
              openclawReady,
              proxyReady,
              secretsReady,
            }),
          );
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

        // Chat action — init and route
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
          let responseText;
          if (openclawReady) {
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
          } else if (proxyReady) {
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
          } else {
            // Proxy not ready yet (should be rare — init awaits proxy)
            responseText = "I'm starting up — please try again in a moment.";
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
