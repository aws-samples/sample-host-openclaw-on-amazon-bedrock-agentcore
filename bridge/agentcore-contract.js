/**
 * AgentCore Runtime Contract Server — Per-User Sessions
 *
 * Implements the required HTTP protocol contract for AgentCore Runtime:
 *   - GET  /ping         -> Health check (Healthy — allows idle termination)
 *   - POST /invocations  -> Chat handler with lazy init per user
 *
 * Each AgentCore session is dedicated to a single user. On first invocation:
 *   1. Restore .openclaw/ workspace from S3
 *   2. Start the Bedrock proxy (port 18790) with USER_ID/CHANNEL env vars
 *   3. Start OpenClaw gateway (port 18789) in headless mode (no channels)
 *   4. Wait for OpenClaw to become ready (~4 min)
 *   5. Start periodic workspace saves
 *
 * Subsequent invocations bridge messages to OpenClaw via WebSocket.
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

const PORT = 8080;
const PROXY_PORT = 18790;
const OPENCLAW_PORT = 18789;

// Gateway token — fetched from Secrets Manager during lazy init.
// No fallback — container will fail to authenticate WebSocket if not set.
let GATEWAY_TOKEN = null;

// Cognito password secret — fetched from Secrets Manager during lazy init.
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
let initInProgress = false;
let initPromise = null;
let startTime = Date.now();
let shuttingDown = false;

// --- Message queue for serializing concurrent requests ---
let messageQueue = [];
let bridgeInProgress = false;
const MAX_QUEUE_DEPTH = 10;
const QUEUE_WAIT_TIMEOUT_MS = 90000;

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
      profile: "full",
      deny: ["write", "edit", "apply_patch"],
    },
    skills: {
      allowBundled: ["*"],
      load: { extraDirs: ["/skills"] },
    },
    gateway: {
      mode: "local",
      port: OPENCLAW_PORT,
      bind: "lan",
      trustedProxies: ["0.0.0.0/0"],
      auth: { mode: "token", token: GATEWAY_TOKEN },
      controlUi: {
        enabled: true,
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
}

/**
 * Lazy initialization — called on first /invocations request.
 * Restores workspace, starts proxy and OpenClaw, waits for readiness.
 */
async function lazyInit(userId, actorId, channel) {
  if (initInProgress) return initPromise;
  initInProgress = true;

  initPromise = (async () => {
    const namespace = actorId.replace(/:/g, "_");
    currentUserId = userId;
    currentNamespace = namespace;

    console.log(
      `[contract] Lazy init for user=${userId} actor=${actorId} namespace=${namespace}`,
    );

    // 0. Fetch secrets from Secrets Manager
    try {
      const region = process.env.AWS_REGION || "us-west-2";
      const smClient = new SecretsManagerClient({ region });

      const gatewaySecretId = process.env.GATEWAY_TOKEN_SECRET_ID;
      if (gatewaySecretId) {
        const resp = await smClient.send(
          new GetSecretValueCommand({ SecretId: gatewaySecretId }),
        );
        if (resp.SecretString) {
          GATEWAY_TOKEN = resp.SecretString;
          console.log("[contract] Gateway token loaded from Secrets Manager");
        }
      }
      if (!GATEWAY_TOKEN) {
        throw new Error(
          "Gateway token not available — cannot authenticate WebSocket connections",
        );
      }

      const cognitoSecretId = process.env.COGNITO_PASSWORD_SECRET_ID;
      if (cognitoSecretId) {
        const resp = await smClient.send(
          new GetSecretValueCommand({ SecretId: cognitoSecretId }),
        );
        if (resp.SecretString) {
          COGNITO_PASSWORD_SECRET = resp.SecretString;
          console.log("[contract] Cognito password secret loaded");
        }
      }
    } catch (err) {
      console.error(`[contract] Secrets fetch failed: ${err.message}`);
      throw err; // Abort init — secrets are required for operation
    }

    // 1. Restore .openclaw/ from S3
    try {
      await workspaceSync.restoreWorkspace(namespace);
    } catch (err) {
      console.warn(`[contract] Workspace restore failed: ${err.message}`);
    }

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
    };
    proxyProcess = spawn("node", ["/app/agentcore-proxy.js"], {
      env: proxyEnv,
      stdio: "inherit",
    });
    proxyProcess.on("exit", (code) => {
      console.log(`[contract] Proxy exited with code ${code}`);
      proxyReady = false;
    });

    // Wait for proxy to be ready
    proxyReady = await waitForPort(PROXY_PORT, "Proxy", 30000, 1000);

    // 3. Write headless OpenClaw config and start gateway
    writeOpenClawConfig();
    console.log("[contract] Starting OpenClaw gateway (headless)...");
    openclawProcess = spawn(
      "openclaw",
      [
        "gateway",
        "run",
        "--port",
        String(OPENCLAW_PORT),
        "--bind",
        "lan",
        "--allow-unconfigured",
        "--verbose",
      ],
      { stdio: "inherit" },
    );
    openclawProcess.on("exit", (code) => {
      console.log(`[contract] OpenClaw exited with code ${code}`);
      openclawReady = false;
    });

    // 4. Wait for OpenClaw to be ready
    openclawReady = await waitForPort(OPENCLAW_PORT, "OpenClaw", 300000, 5000);

    // 5. Start periodic workspace saves
    workspaceSync.startPeriodicSave(namespace);

    console.log("[contract] Lazy init complete");
  })();

  try {
    await initPromise;
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
 * Enqueue a chat message for serialized processing.
 * Returns a Promise that resolves with response text (never rejects).
 * If the system is shutting down or the queue is full, resolves immediately.
 */
function enqueueMessage(message, timeoutMs = 120000) {
  if (shuttingDown) {
    return Promise.resolve(
      "The system is restarting. Please resend your message in a moment.",
    );
  }
  if (messageQueue.length >= MAX_QUEUE_DEPTH) {
    console.warn(
      `[contract] Queue full (${messageQueue.length}/${MAX_QUEUE_DEPTH}) — rejecting message`,
    );
    return Promise.resolve(
      "Too many messages queued. Please wait for a response before sending more.",
    );
  }
  return new Promise((resolve) => {
    const entry = { message, timeoutMs, resolve, enqueuedAt: Date.now() };
    messageQueue.push(entry);
    console.log(
      `[contract] Message enqueued (queue depth: ${messageQueue.length})`,
    );
    if (!bridgeInProgress) {
      processQueue();
    }
  });
}

/**
 * Process queued messages serially. Batches multiple waiting messages into
 * a single bridgeMessage call when the previous call completes.
 */
async function processQueue() {
  if (bridgeInProgress) return; // re-entry guard
  bridgeInProgress = true;
  try {
    while (messageQueue.length > 0) {
      // Drain all currently queued entries into a batch (fresh timestamp per batch)
      const now = Date.now();
      const batch = [];
      while (messageQueue.length > 0) {
        const entry = messageQueue.shift();
        // Drop timed-out entries
        if (now - entry.enqueuedAt > QUEUE_WAIT_TIMEOUT_MS) {
          console.warn("[contract] Dropping timed-out queued message");
          entry.resolve(
            "Your message timed out while waiting in the queue. Please try again.",
          );
          continue;
        }
        batch.push(entry);
      }

      if (batch.length === 0) continue;

      // Build the message to send
      let combinedMessage;
      let bridgeTimeout;
      if (batch.length === 1) {
        combinedMessage = batch[0].message;
        bridgeTimeout = batch[0].timeoutMs;
      } else {
        const parts = batch.map(
          (e, i) => `[Message ${i + 1}/${batch.length}]: ${e.message}`,
        );
        combinedMessage = parts.join("\n\n---\n\n");
        bridgeTimeout = Math.max(...batch.map((e) => e.timeoutMs));
        console.log(
          `[contract] Batched ${batch.length} messages into single request`,
        );
      }

      // Call bridgeMessage and resolve batch entries — wrapped in try/catch to
      // guarantee all shifted entries get resolved (prevents hanging promises)
      let responseText;
      try {
        responseText = await bridgeMessage(combinedMessage, bridgeTimeout);
      } catch (err) {
        responseText = `Bridge error: ${err.message}`;
      }

      // First entry gets the full response, rest get empty (suppressed at router)
      batch[0].resolve(responseText);
      for (let i = 1; i < batch.length; i++) {
        batch[i].resolve("");
      }
    }
  } finally {
    bridgeInProgress = false;
    // If new items arrived during the final await, re-trigger
    if (messageQueue.length > 0) {
      processQueue();
    }
  }
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

        // Status check (no lazy init needed)
        if (action === "status") {
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(
            JSON.stringify({
              status: "running",
              uptime_seconds: Math.floor((Date.now() - startTime) / 1000),
              currentUserId,
              openclawReady,
              proxyReady,
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

          // Kick off lazy init in background (non-blocking) if not ready
          if (!openclawReady || !proxyReady) {
            if (!initInProgress) {
              // Start init in background — don't await
              lazyInit(userId, actorId, channel || "unknown").catch((err) => {
                console.error(
                  `[contract] Background lazy init failed: ${err.message}`,
                );
              });
            }
            // Return immediately so AgentCore doesn't timeout
            res.writeHead(200, { "Content-Type": "application/json" });
            res.end(
              JSON.stringify({
                response:
                  "I'm starting up — this takes a few minutes for the first message. Please try again shortly.",
                userId,
                sessionId: payload.sessionId || null,
                status: "initializing",
              }),
            );
            return;
          }

          // Enqueue message for serialized processing (prevents concurrent bridge conflicts)
          const responseText = await enqueueMessage(message, 120000);

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

  // Drain the message queue — resolve all pending entries with restart message
  while (messageQueue.length > 0) {
    const entry = messageQueue.shift();
    entry.resolve(
      "The system is restarting. Please resend your message in a moment.",
    );
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

  console.log("[contract] Shutdown complete");
  process.exit(0);
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(
    `[contract] AgentCore contract server listening on http://0.0.0.0:${PORT} (per-user session mode)`,
  );
  console.log(
    "[contract] Endpoints: GET /ping, POST /invocations {action: chat|status}",
  );
});
