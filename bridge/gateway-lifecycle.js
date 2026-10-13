/**
 * OpenClaw Gateway readiness and chat transport for the AgentCore Runtime bridge.
 *
 * Readiness requires both /startupz and an authenticated protocol-v4 RPC;
 * an open HTTP port alone does not prove that the gateway can handle a turn.
 * Callers can also require readback of the applied gateway configuration.
 *
 * Each chat connection tracks its own run and any sessions_yield handoff.
 * A disconnect after chat.send leaves an unknown outcome: a missing ACK
 * does not prove the turn failed, so this module never replays the request.
 */
const http = require("http");
const { osRandomUUID: randomUUID } = require("./runtime-lifecycle");
const WebSocket = require("ws");
const { createChatRunTracker } = require("./chat-run-filter");

function readStartup(url, { signal, timeoutMs = 2000 } = {}) {
  return new Promise((resolve, reject) => {
    const request = http.get(url, { signal, timeout: timeoutMs }, (response) => {
      response.resume();
      response.on("end", () => resolve(response.statusCode === 200));
    });
    request.on("timeout", () => request.destroy(new Error("Gateway startup probe timed out")));
    request.on("error", reject);
  });
}

async function waitForGateway({
  port, token, signal, deadline = Date.now() + 300000, intervalMs = 1000, configProbe = false,
}) {
  while (Date.now() < deadline) {
    signal?.throwIfAborted();
    try {
      if (await readStartup(`http://127.0.0.1:${port}/startupz`, {
        signal, timeoutMs: Math.max(1, Math.min(2000, deadline - Date.now())),
      })) {
        const result = await gatewayTurn({
          url: `ws://127.0.0.1:${port}`, token, signal,
          deadline: Math.min(deadline, Date.now() + 10000), probeOnly: true, configProbe,
        });
        if (result.phase === "verified" && (!configProbe || appliedConfigReady(result.configSnapshot, port))) return;
      }
    } catch (error) {
      signal?.throwIfAborted();
    }
    await new Promise((resolve) => {
      const finish = () => {
        clearTimeout(timer);
        signal?.removeEventListener("abort", finish);
        resolve();
      };
      const timer = setTimeout(finish, Math.max(1, Math.min(intervalMs, deadline - Date.now())));
      signal?.addEventListener("abort", finish, { once: true });
    });
  }
  throw new Error("Gateway readiness deadline exceeded");
}

function appliedConfigReady(snapshot, port) {
  const config = snapshot?.config;
  return snapshot?.valid === true && snapshot.exists === true && !snapshot.writeError &&
    typeof snapshot.configRevisionHash === "string" && !!snapshot.configRevisionHash &&
    snapshot.appliedConfigHash === snapshot.configRevisionHash &&
    config?.gateway?.port === port && config.gateway.auth?.mode === "token" &&
    config.models?.providers?.agentcore?.baseUrl === "http://127.0.0.1:18790/v1" &&
    config.models.providers.agentcore.api === "openai-completions" &&
    config.models.providers.agentcore.models?.some((model) => model.id === "bedrock-agentcore") &&
    config.agents?.defaults?.model?.primary === "agentcore/bedrock-agentcore";
}

function gatewayTurn({
  url, token, message, onDelta, extractText = (value) => typeof value === "string" ? value : "",
  deadline = Date.now() + 620000, signal, probeOnly = false, configProbe = false,
}) {
  return new Promise((resolve) => {
    let socket;
    let settled = false;
    let sent = false;
    let accepted = false;
    let response = "";
    let connectId;
    let probeId;
    let configId;
    let configSnapshot;
    let runId;
    let tracker;
    let stage = "challenge";
    let capabilities = [];
    let timer;
    let handshakeTimer;
    const finish = (phase, error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      clearTimeout(handshakeTimer);
      signal?.removeEventListener("abort", abort);
      socket?.terminate();
      resolve({
        phase, response, runId: runId || null,
        ...(configSnapshot ? { configSnapshot } : {}),
        accepted: accepted ? true : sent && phase !== "rejected" ? null : false,
        ...(error ? { error } : {}),
      });
    };
    const uncertain = (error) => finish(sent ? "unknown_outcome" : "not_sent", error);
    const abort = () => uncertain("Gateway request aborted");
    if (signal?.aborted || Date.now() >= deadline) return uncertain("Turn deadline exceeded");
    signal?.addEventListener("abort", abort, { once: true });
    timer = setTimeout(() => uncertain("Turn deadline exceeded"), deadline - Date.now());
    handshakeTimer = setTimeout(() => uncertain("Gateway handshake deadline exceeded"),
      Math.min(10000, deadline - Date.now()));
    socket = new WebSocket(url); // Loopback token clients must not send an Origin.
    const send = (id, method, params) => {
      try {
        socket.send(JSON.stringify({ type: "req", id, method, params }), (error) => {
          if (error) uncertain("Gateway send failed");
        });
      } catch {
        uncertain("Gateway send failed");
      }
    };
    socket.on("message", (data) => {
      if (settled) return;
      let frame;
      try { frame = JSON.parse(data.toString()); } catch { return; }
      if (stage === "challenge" && frame.type === "event" && frame.event === "connect.challenge") {
        stage = "connect";
        connectId = randomUUID();
        send(connectId, "connect", {
          minProtocol: 4, maxProtocol: 4,
          client: { id: "gateway-client", mode: "backend", version: "dev", platform: "linux" },
          caps: [], auth: { token }, role: "operator",
          scopes: ["operator.admin", "operator.read", "operator.write"],
        });
      } else if (stage === "connect" && frame.type === "res" && frame.id === connectId) {
        if (frame.ok !== true || frame.payload?.type !== "hello-ok" || frame.payload.protocol !== 4) {
          return finish("not_sent", "Gateway authentication rejected");
        }
        const methods = frame.payload.features?.methods;
        if (!Array.isArray(methods) ||
            !methods.includes("chat.send") || !methods.includes("last-heartbeat")) {
          return finish("not_sent", "Gateway capabilities unavailable");
        }
        capabilities = methods;
        stage = "probe";
        probeId = randomUUID();
        // The pinned gateway reads only its cached event; null is a valid RPC response.
        send(probeId, "last-heartbeat", {});
      } else if (stage === "probe" && frame.type === "res" && frame.id === probeId) {
        if (frame.ok !== true) return finish("not_sent", "Gateway readiness probe rejected");
        clearTimeout(handshakeTimer);
        if (probeOnly && configProbe) {
          if (!capabilities.includes("config.get")) return finish("not_sent", "Gateway config capability unavailable");
          stage = "config";
          configId = randomUUID();
          send(configId, "config.get", {});
          return;
        }
        if (probeOnly) return finish("verified");
        if (signal?.aborted || Date.now() >= deadline) return abort();
        stage = "chat";
        runId = randomUUID();
        tracker = createChatRunTracker(runId);
        sent = true; // A lost ACK is not proof that the turn did not run.
        send(runId, "chat.send", { sessionKey: "global", message, idempotencyKey: runId });
      } else if (stage === "config" && frame.type === "res" && frame.id === configId) {
        if (frame.ok !== true) return finish("not_sent", "Gateway config read rejected");
        configSnapshot = frame.payload;
        finish("verified");
      } else if (stage === "chat" && frame.type === "res" && frame.id === runId) {
        if (frame.ok !== true) return finish("rejected", "Gateway rejected the turn");
        accepted = true;
        tracker.adoptAck(frame.payload);
        if (["final", "done"].includes(frame.payload?.status)) finish("completed");
      } else if (stage === "chat" && frame.type === "event" && frame.event === "chat") {
        const payload = frame.payload || {};
        const verdict = tracker.classify(payload);
        if (verdict.action === "ignore" || verdict.action === "yield") return;
        const text = extractText(payload.message?.content) || extractText(payload.message) ||
          extractText(payload.text) || extractText(payload.content);
        if (text) response = text;
        accepted = true;
        if (payload.state === "delta") {
          try { if (text) onDelta?.(text); } catch {}
        } else if (payload.state === "final") {
          finish("completed");
        } else if (["error", "aborted"].includes(payload.state)) {
          finish(payload.state, "Gateway turn ended without completion");
        }
      }
    });
    socket.on("error", () => uncertain("Gateway connection failed"));
    socket.on("close", () => uncertain("Gateway connection closed"));
  });
}

module.exports = { readStartup, waitForGateway, gatewayTurn, appliedConfigReady };
