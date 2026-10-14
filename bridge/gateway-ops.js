/**
 * Operator-side helpers for the V1 contract's OpenClaw gateway connection.
 *
 * - dismissStaleSuspendedDeliveries(): one bounded startup sweep that clears
 *   sub-agent result deliveries openclaw suspended long ago. openclaw 2026.9.5
 *   renders every suspended row into each main-session turn as
 *   "Child results awaiting delivery" for 7 days, and the model narrates it
 *   into replies.
 *
 * Gateway protocol v4, openclaw 2026.9.5 (bridge/Dockerfile pin). The RPC
 * shapes match main's bridge/gateway-lifecycle.js; this V1 copy keeps the
 * same behaviour without the V2 lifecycle.
 *
 * Log calls use a constant first argument and pass values as trailing
 * arguments: cloudwatch-logger.js joins arguments with spaces and does not
 * apply printf placeholders.
 */
const { randomUUID } = require("crypto");
const WebSocket = require("ws");

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
        if (frame.ok !== true || frame.payload?.type !== "hello-ok") {
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
const STALE_SWEEP_TIMEOUT_MS = 15000;
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
 * Run once when the gateway is ready, before the first chat turn is sent.
 * Bounded by `deadline` (15 s by default). Never rejects.
 */
async function dismissStaleSuspendedDeliveries({
  url, token, signal, deadline = Date.now() + STALE_SWEEP_TIMEOUT_MS, now = Date.now(),
  staleMs = STALE_DELIVERY_MS, log = console, maxPages = 10,
}) {
  const result = { phase: "done", listed: 0, candidates: [], dismissed: [], notDismissed: [] };
  let session;
  try {
    session = await openOperatorSession({ url, token, signal, deadline });
    if (!session.methods.includes("tasks.list") || !session.methods.includes("tasks.dismiss")) {
      result.phase = "skipped";
      log?.warn?.("[gateway] Stale delivery sweep skipped: task methods unavailable");
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
        log?.warn?.("[gateway] tasks.dismiss failed; tasks, error:", taskIds.join(","), error.message);
        continue;
      }
      for (const taskId of taskIds) {
        const item = items.find((entry) => entry?.taskId === taskId);
        if (item?.ok === true) {
          result.dismissed.push(taskId);
          log?.log?.("[gateway] Dismissed stale sub-agent delivery task", taskId, "run", runIds.get(taskId));
        } else {
          const reason = typeof item?.reason === "string" ? item.reason : "no result";
          result.notDismissed.push({ taskId, reason });
          log?.warn?.("[gateway] Sub-agent delivery not dismissed task", taskId, "reason", reason);
        }
      }
    }
    log?.log?.("[gateway] Stale delivery sweep: listed", result.listed, "stale", result.candidates.length,
      "dismissed", result.dismissed.length, "not_dismissed", result.notDismissed.length);
  } catch (error) {
    result.phase = "failed";
    result.error = error?.message || "unknown error";
    log?.warn?.("[gateway] Stale delivery sweep failed:", result.error);
  } finally {
    session?.close();
  }
  return result;
}

module.exports = {
  openOperatorSession,
  isStaleSuspendedDelivery,
  dismissStaleSuspendedDeliveries,
  STALE_DELIVERY_MS,
  STALE_SWEEP_TIMEOUT_MS,
};
