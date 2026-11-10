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

const TOOL_BLOCK_TYPES = new Set(["toolCall", "tool_call", "toolUse", "tool_use", "functionCall", "function_call"]);
const SHORT_FINAL_CHARS = 200;
// The first chat.history after a snapshot restore can take several seconds
// (5559 ms on staging after a ~15 min freeze), so each attempt gets 15 s and a
// timed-out request is sent once more, always inside the turn deadline.
const HISTORY_TIMEOUT_MS = 15000;
const HISTORY_ATTEMPTS = 2;
const HISTORY_RETRY_MIN_MS = 100;
const HISTORY_LIMIT = 100;
const HISTORY_MAX_CHARS = 500000; // openclaw 2026.11.2 ChatHistoryParamsSchema maximum; default 8000 truncates.
// The bridge runs one turn at a time, so any run already active on the main
// session when a turn is sent belongs to nobody waiting on it (for example
// openclaw's restart recovery of a turn interrupted before a container stop).
// openclaw 2026.11.2 defaults to "steer": it folds the new message into that run
// and answers chat.send with an empty final, so the user gets no reply.
// "interrupt" stops that run and answers this turn as its own run.
const CHAT_QUEUE_MODE = "interrupt";

function textBlockPhase(block) {
  let signature = block?.textSignature;
  if (typeof signature === "string") {
    try { signature = JSON.parse(signature); } catch { signature = undefined; }
  }
  return signature && typeof signature === "object" ? signature.phase : undefined;
}

function historyMeta(message) {
  const meta = message?.__openclaw;
  return meta && typeof meta === "object" ? meta : {};
}

function historyUserKey(message) {
  return message.idempotencyKey ?? historyMeta(message).idempotencyKey;
}

/**
 * Pick the text to deliver for one run from openclaw 2026.11.2 chat.history.
 *
 * The WS chat final carries the run's whole text buffer, so status lines the
 * model wrote before tool calls reach the user. The answer is the last
 * assistant message of the run that has no tool call, without commentary-phase
 * text blocks. When that message is short and the run wrote other
 * non-commentary text, all of it is delivered so content split over several
 * messages is not lost. Returns null when the run's answer cannot be found
 * or was truncated by the gateway, so the caller can fall back.
 */
function selectFinalAnswer(messages, { userKey, runIds, extractText = (value) => typeof value === "string" ? value : "" }) {
  if (!Array.isArray(messages)) return null;
  const list = messages.filter((message) => message && typeof message === "object");
  let start = -1;
  for (let i = list.length - 1; i >= 0 && userKey; i--) {
    if (list[i].role === "user" && historyUserKey(list[i]) === userKey) { start = i; break; }
  }
  const items = [];
  for (const message of start === -1 ? list : list.slice(start + 1)) {
    const otherInput = message.role === "user" && historyUserKey(message) && historyUserKey(message) !== userKey;
    if (start !== -1 && otherInput) break;
    if (message.role !== "assistant") continue;
    const messageRun = historyMeta(message).runId;
    if (messageRun ? !runIds.has(messageRun) : start === -1) continue;
    if (message.phase === "commentary" || message.openclawStreamFallback) continue;
    const content = Array.isArray(message.content) ? message.content
      : typeof message.content === "string" ? [{ type: "text", text: message.content }] : [];
    const blocks = content.filter((block) => block?.type === "text" && typeof block.text === "string" &&
      textBlockPhase(block) !== "commentary");
    items.push({
      text: blocks.length ? String(extractText(blocks.map((block) => block.text).join("")) || "").trim() : "",
      hasTool: content.some((block) => TOOL_BLOCK_TYPES.has(block?.type)),
      truncated: historyMeta(message).truncated === true,
    });
  }
  let last = -1;
  for (let i = items.length - 1; i >= 0; i--) {
    if (!items[i].hasTool && items[i].text) { last = i; break; }
  }
  if (last === -1) return null;
  const earlier = items.slice(0, last).filter((item) => item.text);
  const chosen = items[last].text.length < SHORT_FINAL_CHARS && earlier.length ? [...earlier, items[last]] : [items[last]];
  if (chosen.some((item) => item.truncated)) return null;
  return { text: chosen.map((item) => item.text).join("\n\n"), items: chosen.length };
}

function gatewayTurn({
  url, token, message, onDelta, extractText = (value) => typeof value === "string" ? value : "",
  deadline = Date.now() + 620000, signal, probeOnly = false, configProbe = false, log = globalThis.console,
  historyTimeoutMs = HISTORY_TIMEOUT_MS, onOwnRun,
}) {
  return new Promise((resolve) => {
    let socket;
    let settled = false;
    let sent = false;
    let accepted = false;
    let response = "";
    let delivery = "buffer";
    let connectId;
    let probeId;
    let configId;
    const historyIds = new Set();
    let configSnapshot;
    let runId;
    let tracker;
    const ownRuns = new Set();
    let stage = "challenge";
    let capabilities = [];
    let timer;
    let handshakeTimer;
    let historyTimer;
    const finish = (phase, error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      clearTimeout(handshakeTimer);
      clearTimeout(historyTimer);
      signal?.removeEventListener("abort", abort);
      socket?.terminate();
      resolve({
        phase, response, runId: runId || null,
        ...(configSnapshot ? { configSnapshot } : {}),
        ...(phase === "completed" ? { delivery } : {}),
        accepted: accepted ? true : sent && phase !== "rejected" ? null : false,
        ...(error ? { error } : {}),
      });
    };
    // The run has finished: any history problem delivers today's run buffer.
    const deliverBuffer = (reason) => {
      if (settled) return;
      try { log?.warn?.("[gateway] Delivering the run text buffer; chars, reason:", response.length, reason); } catch {}
      finish("completed");
    };
    const uncertain = (error) => stage === "history" ? deliverBuffer(error)
      : finish(sent ? "unknown_outcome" : "not_sent", error);
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
        send(runId, "chat.send", { sessionKey: "global", message, idempotencyKey: runId, queueMode: CHAT_QUEUE_MODE });
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
        if (verdict.action !== "ignore" && tracker.state.runId && !ownRuns.has(tracker.state.runId)) {
          ownRuns.add(tracker.state.runId);
          // Lets the late announce relay skip a run this turn returns (#148).
          try { onOwnRun?.(tracker.state.runId); } catch {}
        }
        if (verdict.action === "ignore" || verdict.action === "yield") return;
        const text = extractText(payload.message?.content) || extractText(payload.message) ||
          extractText(payload.text) || extractText(payload.content);
        if (text) response = text;
        accepted = true;
        if (payload.state === "delta") {
          try { if (text) onDelta?.(text); } catch {}
        } else if (payload.state === "final") {
          // The final carries every assistant item of the run joined together;
          // deliver only the run's answer, read back from the transcript.
          if (!capabilities.includes("chat.history")) {
            return deliverBuffer("chat.history capability unavailable");
          }
          stage = "history";
          clearTimeout(timer);
          const historyParams = {
            sessionKey: tracker.state.sessionKey || payload.sessionKey || "global",
            limit: HISTORY_LIMIT, maxChars: HISTORY_MAX_CHARS,
          };
          // Each attempt ends by the turn deadline. A late reply to an earlier
          // attempt is still accepted after the retry is sent.
          const requestHistory = (attempt) => {
            const wait = Math.max(1, Math.min(historyTimeoutMs, deadline - Date.now()));
            historyTimer = setTimeout(() => {
              if (settled) return;
              if (attempt >= HISTORY_ATTEMPTS || deadline - Date.now() < HISTORY_RETRY_MIN_MS) {
                return deliverBuffer("chat.history timed out");
              }
              try { log?.warn?.("[gateway] chat.history got no reply, retrying once; waited ms:", wait); } catch {}
              requestHistory(attempt + 1);
            }, wait);
            const id = randomUUID();
            historyIds.add(id);
            send(id, "chat.history", historyParams);
          };
          requestHistory(1);
        } else if (["error", "aborted"].includes(payload.state)) {
          finish(payload.state, "Gateway turn ended without completion");
        }
      } else if (stage === "history" && frame.type === "res" && historyIds.has(frame.id)) {
        if (frame.ok !== true) return deliverBuffer("chat.history rejected");
        const answer = selectFinalAnswer(frame.payload?.messages, { userKey: runId, runIds: ownRuns, extractText });
        if (!answer) return deliverBuffer("no final assistant message in chat.history");
        try {
          log?.log?.("[gateway] Delivering the run's final answer from chat.history; chars, items, buffer chars:",
            answer.text.length, answer.items, response.length);
        } catch {}
        response = answer.text;
        delivery = "history";
        finish("completed");
      }
    });
    socket.on("error", () => uncertain("Gateway connection failed"));
    socket.on("close", () => uncertain("Gateway connection closed"));
  });
}

/**
 * Open one authenticated operator connection and expose a request helper.
 * Rejects when the handshake fails or the deadline passes; `close()` is idempotent.
 * With `persistent`, the deadline bounds only the handshake and the connection
 * stays open until it drops, `signal` aborts or `close()` is called. `onEvent`
 * receives every event frame after the handshake; `onClose` runs once.
 */
function openOperatorSession({ url, token, signal, deadline, persistent = false, onEvent, onClose }) {
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
      for (const { reject: fail, timer: callTimer } of pending.values()) {
        clearTimeout(callTimer);
        fail(new Error(reason));
      }
      pending.clear();
      try { socket?.terminate(); } catch {}
      try { onClose?.(); } catch {}
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
    const call = (method, params, { timeoutMs } = {}) => new Promise((done, failCall) => {
      if (closed) return failCall(new Error("Gateway connection closed"));
      const id = randomUUID();
      const entry = { resolve: done, reject: failCall };
      if (timeoutMs) {
        entry.timer = setTimeout(() => {
          if (!pending.delete(id)) return;
          failCall(new Error("Gateway request timed out"));
        }, timeoutMs);
        entry.timer.unref?.();
      }
      pending.set(id, entry);
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
        if (persistent) clearTimeout(timer);
        resolve({ methods: Array.isArray(methods) ? methods : [], call, close });
      } else if (frame.type === "res" && pending.has(frame.id)) {
        const entry = pending.get(frame.id);
        pending.delete(frame.id);
        clearTimeout(entry.timer);
        if (frame.ok === true) entry.resolve(frame.payload);
        else entry.reject(new Error(typeof frame.error?.message === "string"
          ? frame.error.message.slice(0, 200) : "Gateway request rejected"));
      } else if (settled && frame.type === "event") {
        try { onEvent?.(frame); } catch {}
      }
    });
    socket.on("error", () => fail("Gateway connection failed"));
    socket.on("close", () => fail("Gateway connection closed"));
  });
}

const WATCH_HANDSHAKE_MS = 10000;
const WATCH_RETRY_MS = 5000;

function pause(ms, signal) {
  return new Promise((resolve) => {
    const finish = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", finish);
      resolve();
    };
    const timer = setTimeout(finish, ms);
    signal?.addEventListener("abort", finish, { once: true });
  });
}

/**
 * Keep one operator connection open and pass every `chat` event payload to
 * `onEvent`, reconnecting after `retryMs` when the connection drops. Stops
 * when `signal` aborts. The gateway broadcasts chat events for every run to
 * every operator connection, so this sees runs that no turn is waiting on,
 * such as a sub-agent announce that finishes after the main reply (#148).
 *
 * `readRunAnswer` reads one run's answer from chat.history on the current
 * connection with the same selection as gatewayTurn, falling back to the
 * run's final text buffer. `ready` resolves on the first connection;
 * `stopped` resolves when the watcher has stopped.
 */
function watchChatEvents({
  url, token, signal, onEvent, log = globalThis.console, retryMs = WATCH_RETRY_MS,
  historyTimeoutMs = HISTORY_TIMEOUT_MS,
}) {
  let session = null;
  let markReady;
  const ready = new Promise((resolve) => { markReady = resolve; });
  const forward = (frame) => {
    if (frame.event !== "chat" || !frame.payload || typeof frame.payload !== "object") return;
    try { onEvent?.(frame.payload); } catch {}
  };
  const loop = async () => {
    while (!signal?.aborted) {
      let dropped;
      const closedOnce = new Promise((resolve) => { dropped = resolve; });
      try {
        session = await openOperatorSession({
          url, token, signal, deadline: Date.now() + WATCH_HANDSHAKE_MS, persistent: true,
          onEvent: forward, onClose: () => dropped(),
        });
        markReady();
        await closedOnce;
        if (!signal?.aborted) log?.warn?.("[gateway] Chat event watcher disconnected; reconnecting");
      } catch (error) {
        if (signal?.aborted) break;
        log?.warn?.("[gateway] Chat event watcher could not connect:", error?.message || "unknown error");
      } finally {
        session?.close();
        session = null;
      }
      if (signal?.aborted) break;
      await pause(retryMs, signal);
    }
    markReady();
  };
  const stopped = loop();
  const readRunAnswer = async ({ runId, sessionKey, buffer = "", extractText }) => {
    const current = session;
    if (current && current.methods.includes("chat.history")) {
      try {
        const payload = await current.call("chat.history",
          { sessionKey, limit: HISTORY_LIMIT, maxChars: HISTORY_MAX_CHARS }, { timeoutMs: historyTimeoutMs });
        const answer = selectFinalAnswer(payload?.messages, { userKey: null, runIds: new Set([runId]), extractText });
        if (answer) return answer.text;
        log?.warn?.("[gateway] No answer for the run in chat.history; using its text buffer");
      } catch (error) {
        log?.warn?.("[gateway] chat.history for the run failed; using its text buffer:", error?.message || "unknown error");
      }
    }
    return buffer;
  };
  return { ready, stopped, readRunAnswer };
}

module.exports = {
  readStartup, waitForGateway, gatewayTurn, appliedConfigReady, watchChatEvents, selectFinalAnswer,
};
