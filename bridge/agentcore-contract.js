/**
 * AgentCore Runtime Contract Server
 *
 * Implements the required HTTP protocol contract for AgentCore Runtime:
 *   - GET  /ping         -> Health check (HealthyBusy while OpenClaw is running)
 *   - POST /invocations  -> Handles keepalive pings and status queries
 *
 * Runs on port 8080 (required by AgentCore Runtime).
 * OpenClaw gateway (18789) and Bedrock proxy (18790) run as sibling processes.
 */

const http = require("http");
const { spawn, execSync } = require("child_process");

const PORT = 8080;
const PROXY_PORT = 18790;
const OPENCLAW_PORT = 18789;

// Process state
let openclawProcess = null;
let proxyProcess = null;
let openclawReady = false;
let proxyReady = false;
let startTime = Date.now();
let lastInvocationTime = Date.now();
let telegramConnected = false;

/**
 * Check if the proxy health endpoint responds.
 */
async function checkProxyHealth() {
  return new Promise((resolve) => {
    const req = http.get(`http://127.0.0.1:${PROXY_PORT}/health`, (res) => {
      let body = "";
      res.on("data", (chunk) => (body += chunk));
      res.on("end", () => {
        try {
          const data = JSON.parse(body);
          resolve(data);
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
 * Periodically check readiness of child processes.
 */
async function pollReadiness() {
  const proxyHealth = await checkProxyHealth();
  if (proxyHealth) {
    proxyReady = true;
  }
  // OpenClaw readiness: check if port 18789 is listening
  try {
    await new Promise((resolve, reject) => {
      const req = http.get(`http://127.0.0.1:${OPENCLAW_PORT}`, (res) => {
        openclawReady = true;
        res.resume();
        resolve();
      });
      req.on("error", reject);
      req.setTimeout(1000, () => {
        req.destroy();
        reject(new Error("timeout"));
      });
    });
  } catch {
    // OpenClaw not ready yet (expected during startup)
  }
}

setInterval(pollReadiness, 10000);

/**
 * AgentCore contract HTTP server.
 */
const server = http.createServer(async (req, res) => {
  // GET /ping — AgentCore health check
  if (req.method === "GET" && req.url === "/ping") {
    // Return HealthyBusy to keep the session alive (prevents idle termination).
    // The container is always "busy" because OpenClaw maintains persistent
    // connections to messaging channels (Telegram, Discord, etc.).
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        status: "HealthyBusy",
        time_of_last_update: Math.floor(Date.now() / 1000),
      })
    );
    return;
  }

  // POST /invocations — AgentCore invocation endpoint
  if (req.method === "POST" && req.url === "/invocations") {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", async () => {
      try {
        const payload = body ? JSON.parse(body) : {};
        const action = payload.action || "status";
        lastInvocationTime = Date.now();

        if (action === "keepalive" || action === "status") {
          const proxyHealth = await checkProxyHealth();
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(
            JSON.stringify({
              status: "running",
              uptime_seconds: Math.floor((Date.now() - startTime) / 1000),
              openclaw_ready: openclawReady,
              proxy_ready: proxyReady,
              proxy_mode: proxyHealth?.mode || "unknown",
              telegram_connected: telegramConnected,
              last_invocation: new Date(lastInvocationTime).toISOString(),
            })
          );
          return;
        }

        // For any other action, return a helpful message
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            response:
              "OpenClaw is running on AgentCore Runtime. " +
              "Send messages via Telegram to interact with the bot.",
            status: "running",
          })
        );
      } catch (err) {
        console.error("[agentcore-contract] Invocation error:", err.message);
        res.writeHead(500, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: err.message }));
      }
    });
    return;
  }

  res.writeHead(404, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "Not found" }));
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(
    `[agentcore-contract] AgentCore contract server listening on http://0.0.0.0:${PORT}`
  );
  console.log(
    `[agentcore-contract] Endpoints: GET /ping, POST /invocations`
  );
});
