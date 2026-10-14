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

/**
 * Open one authenticated operator connection and expose a request helper.
 * Rejects when the handshake fails or the deadline passes; `close()` is idempotent.
 */
function openOperatorSession({ url, token, signal, deadline }) {
  return new Promise((resolve, reject) => {
    const pending = new Map();
    let socket;
    let settled = false;
    let closed = false;
    let connectId;
    let timer;
    const close = (reason = "Gateway connection closed") => {
      if (closed) return;
      closed = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      for (const { reject: fail } of pending.values()) fail(new Error(reason));
      pending.clear();
      try { socket?.terminate(); } catch {}
    };
    const fail = (message) => {
      close(message);
      if (!settled) {
        settled = true;
        reject(new Error(message));
      }
    };
    const abort = () => fail("Gateway request aborted");
    if (signal?.aborted || Date.now() >= deadline) return fail("Deadline exceeded");
    signal?.addEventListener("abort", abort, { once: true });
    timer = setTimeout(() => fail("Deadline exceeded"), deadline - Date.now());
    const send = (id, method, params) => {
      socket.send(JSON.stringify({ type: "req", id, method, params }), (error) => {
        if (error) fail("Gateway send failed");
      });
    };
    const call = (method, params) => new Promise((done, failCall) => {
      if (closed) return failCall(new Error("Gateway connection closed"));
      const id = randomUUID();
      pending.set(id, { resolve: done, reject: failCall });
      try { send(id, method, params); } catch { fail("Gateway send failed"); }
    });
    socket = new WebSocket(url); // Loopback token clients must not send an Origin.
    socket.on("message", (data) => {
      if (closed) return;
      let frame;
      try { frame = JSON.parse(data.toString()); } catch { return; }
      if (!connectId && frame.type === "event" && frame.event === "connect.challenge") {
        connectId = randomUUID();
        send(connectId, "connect", {
          minProtocol: 4, maxProtocol: 4,
          client: { id: "gateway-client", mode: "backend", version: "dev", platform: "linux" },
          caps: [], auth: { token }, role: "operator",
          scopes: ["operator.admin", "operator.read", "operator.write"],
        });
      } else if (connectId && !settled && frame.type === "res" && frame.id === connectId) {
        if (frame.ok !== true || frame.payload?.type !== "hello-ok" || frame.payload.protocol !== 4) {
          return fail("Gateway authentication rejected");
        }
        const methods = frame.payload.features?.methods;
        settled = true;
        resolve({ methods: Array.isArray(methods) ? methods : [], call, close });
      } else if (frame.type === "res" && pending.has(frame.id)) {
        const entry = pending.get(frame.id);
        pending.delete(frame.id);
        if (frame.ok === true) entry.resolve(frame.payload);
        else entry.reject(new Error(typeof frame.error?.message === "string"
          ? frame.error.message.slice(0, 200) : "Gateway request rejected"));
      }
    });
    socket.on("error", () => fail("Gateway connection failed"));
    socket.on("close", () => fail("Gateway connection closed"));
  });
}

const STALE_DELIVERY_MS = 2 * 60 * 60 * 1000;
const TASKS_LIST_LIMIT = 500; // openclaw 2026.9.5 TasksListParamsSchema maximum.
const TASKS_DISMISS_BATCH = 10; // openclaw 2026.9.5 TasksRecoveryParamsSchema maxItems.

function taskTimestamp(value) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value) {
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

/**
 * True for a tasks.list entry whose sub-agent completion delivery is stuck.
 *
 * openclaw 2026.9.5 does not put the sub-agent's `delivery.status` in the
 * public task summary. Suspension (blockSubagentCompletionDelivery) is the
 * only path that writes task deliveryStatus "failed" together with
 * terminalOutcome "blocked" on a succeeded sub-agent task; intentional
 * non-delivery leaves no "blocked" outcome. tasks.dismiss itself refuses any
 * task whose delivery is not suspended, so a misclassified row is a no-op.
 */
function isStaleSuspendedDelivery(task, now = Date.now(), staleMs = STALE_DELIVERY_MS) {
  if (!task || typeof task !== "object") return false;
  if (task.runtime !== "subagent" || task.status !== "completed") return false;
  if (task.deliveryStatus !== "failed" || task.terminalOutcome !== "blocked") return false;
  if (typeof (task.taskId || task.id) !== "string" || !(task.taskId || task.id)) return false;
  const times = [taskTimestamp(task.updatedAt), taskTimestamp(task.endedAt)].filter((t) => t !== undefined);
  if (times.length === 0) return false;
  return now - Math.max(...times) >= staleMs;
}

/**
 * Dismiss sub-agent result deliveries that openclaw suspended long ago.
 *
 * A suspended delivery is rendered into every main-session turn as
 * "Child results awaiting delivery" for 7 days (openclaw 2026.9.5), and the
 * model narrates it into replies. Run once after the gateway is ready and
 * before the first chat turn. Never rejects: startup must not depend on it.
 */
async function dismissStaleSuspendedDeliveries({
  url, token, signal, deadline = Date.now() + 15000, now = Date.now(),
  staleMs = STALE_DELIVERY_MS, log = console, maxPages = 10,
}) {
  const result = { phase: "done", listed: 0, candidates: [], dismissed: [], notDismissed: [] };
  let session;
  try {
    session = await openOperatorSession({ url, token, signal, deadline });
    if (!session.methods.includes("tasks.list") || !session.methods.includes("tasks.dismiss")) {
      result.phase = "skipped";
      log?.warn?.("[gateway] Stale delivery sweep skipped: %s", "task methods unavailable");
      return result;
    }
    const stale = [];
    let cursor;
    for (let page = 0; page < maxPages; page++) {
      const params = { status: "completed", limit: TASKS_LIST_LIMIT, ...(cursor ? { cursor } : {}) };
      const payload = await session.call("tasks.list", params);
      const tasks = Array.isArray(payload?.tasks) ? payload.tasks : [];
      result.listed += tasks.length;
      for (const task of tasks) if (isStaleSuspendedDelivery(task, now, staleMs)) stale.push(task);
      cursor = typeof payload?.nextCursor === "string" && payload.nextCursor ? payload.nextCursor : undefined;
      if (!cursor) break;
    }
    result.candidates = stale.map((task) => task.taskId || task.id);
    const runIds = new Map(stale.map((task) => [task.taskId || task.id, task.runId || "unknown"]));
    for (let i = 0; i < result.candidates.length; i += TASKS_DISMISS_BATCH) {
      const taskIds = result.candidates.slice(i, i + TASKS_DISMISS_BATCH);
      let items;
      try {
        const payload = await session.call("tasks.dismiss", { taskIds });
        items = Array.isArray(payload?.results) ? payload.results : [];
      } catch (error) {
        for (const taskId of taskIds) result.notDismissed.push({ taskId, reason: error.message });
        log?.warn?.("[gateway] tasks.dismiss failed for %s: %s", taskIds.join(","), error.message);
        continue;
      }
      for (const taskId of taskIds) {
        const item = items.find((entry) => entry?.taskId === taskId);
        if (item?.ok === true) {
          result.dismissed.push(taskId);
          log?.log?.("[gateway] Dismissed stale sub-agent delivery task=%s run=%s", taskId, runIds.get(taskId));
        } else {
          const reason = typeof item?.reason === "string" ? item.reason : "no result";
          result.notDismissed.push({ taskId, reason });
          log?.warn?.("[gateway] Sub-agent delivery not dismissed task=%s reason=%s", taskId, reason);
        }
      }
    }
    log?.log?.("[gateway] Stale delivery sweep: listed=%s stale=%s dismissed=%s not_dismissed=%s",
      result.listed, result.candidates.length, result.dismissed.length, result.notDismissed.length);
  } catch (error) {
    result.phase = "failed";
    result.error = error?.message || "unknown error";
    log?.warn?.("[gateway] Stale delivery sweep failed: %s", result.error);
  } finally {
    session?.close();
  }
  return result;
}

module.exports = {
  readStartup, waitForGateway, gatewayTurn, appliedConfigReady,
  dismissStaleSuspendedDeliveries, isStaleSuspendedDelivery, STALE_DELIVERY_MS,
};
