const { test } = require("node:test");
const assert = require("node:assert/strict");
const http = require("http");
const WebSocket = require("ws");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { gatewayTurn, waitForGateway } = require("./gateway-lifecycle");

async function fakeGateway(t, {
  startup = 200, auth = true, protocol = 4, methods = ["chat.send", "last-heartbeat"],
  probe = true, chat, config, handlers = {},
} = {}) {
  const calls = [];
  const frames = [];
  const server = http.createServer((_req, res) => {
    calls.push("startupz");
    res.writeHead(startup);
    res.end("{}");
  });
  const wss = new WebSocket.Server({ server });
  wss.on("connection", (socket, request) => {
    assert.equal(request.headers.origin, undefined);
    socket.send(JSON.stringify({ type: "event", event: "connect.challenge" }));
    socket.on("message", (raw) => {
      const frame = JSON.parse(raw);
      frames.push(frame);
      calls.push(frame.method);
      const reply = (ok, payload, id = frame.id) => socket.send(JSON.stringify({ type: "res", id, ok, payload }));
      if (frame.method === "connect") {
        assert.equal(frame.params.auth.token, "secret");
        assert.equal(frame.params.minProtocol, 4);
        reply(auth, { type: "hello-ok", protocol, features: { methods } });
      } else if (frame.method === "last-heartbeat") {
        assert.deepEqual(frame.params, {});
        if (probe === "wrong-id") reply(true, {}, "another-request");
        else reply(probe, null);
      } else if (frame.method === "chat.send") {
        chat?.({ socket, frame, reply });
      } else if (frame.method === "config.get") {
        assert.deepEqual(frame.params, {});
        config?.({ frame, reply, port: server.address().port });
      } else if (handlers[frame.method]) {
        handlers[frame.method]({ socket, frame, reply });
      }
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(async () => {
    for (const socket of wss.clients) socket.terminate();
    await new Promise((resolve) => wss.close(resolve));
    await new Promise((resolve) => server.close(resolve));
  });
  return { calls, frames, port: server.address().port, url: `ws://127.0.0.1:${server.address().port}` };
}

test("startup + token handshake + cached heartbeat RPC verifies with a null payload and no chat", async (t) => {
  const gateway = await fakeGateway(t);
  await waitForGateway({ port: gateway.port, token: "secret", deadline: Date.now() + 1000 });
  assert.deepEqual(gateway.calls, ["startupz", "connect", "last-heartbeat"]);
});

function appliedSnapshot(port) {
  return { exists: true, valid: true, configRevisionHash: "config-revision",
    appliedConfigHash: "config-revision", config: {
      gateway: { port, auth: { mode: "token" } },
      models: { providers: { agentcore: { baseUrl: "http://127.0.0.1:18790/v1",
        api: "openai-completions", models: [{ id: "bedrock-agentcore" }] } } },
      agents: { defaults: { model: { primary: "agentcore/bedrock-agentcore" } } },
    } };
}

test("full readiness requires an authenticated applied-config reply without sending chat", async (t) => {
  const gateway = await fakeGateway(t, {
    methods: ["chat.send", "last-heartbeat", "config.get"],
    config: ({ reply, port }) => reply(true, appliedSnapshot(port)),
  });
  await waitForGateway({ port: gateway.port, token: "secret", configProbe: true,
    deadline: Date.now() + 1000 });
  assert.deepEqual(gateway.calls, ["startupz", "connect", "last-heartbeat", "config.get"]);
});

for (const failure of ["capability", "revision", "invalid", "error", "wrong-id", "timeout"]) {
  test(`full readiness rejects ${failure} while retaining the deadline`, async (t) => {
    const gateway = await fakeGateway(t, {
      methods: failure === "capability" ? ["chat.send", "last-heartbeat"]
        : ["chat.send", "last-heartbeat", "config.get"],
      config: ({ reply, port }) => {
        const snapshot = appliedSnapshot(port);
        if (failure === "timeout") return;
        if (failure === "revision") snapshot.appliedConfigHash = "previous";
        if (failure === "invalid") snapshot.valid = false;
        reply(failure !== "error", snapshot, failure === "wrong-id" ? "unrelated" : undefined);
      },
    });
    await assert.rejects(waitForGateway({ port: gateway.port, token: "secret", configProbe: true,
      deadline: Date.now() + 120, intervalMs: 5 }), /deadline/);
    assert.ok(!gateway.calls.includes("chat.send"));
  });
}

test("HTTP 503 never becomes gateway readiness", async (t) => {
  const gateway = await fakeGateway(t, { startup: 503 });
  await assert.rejects(waitForGateway({
    port: gateway.port, token: "secret", deadline: Date.now() + 50, intervalMs: 5,
  }), /deadline/);
  assert.ok(gateway.calls.every((method) => method === "startupz"));
});

for (const scenario of [
  { auth: false }, { probe: false }, { probe: "wrong-id" },
  { protocol: 3 }, { methods: null }, { methods: ["chat.send"] },
]) {
  test(`failed handshake/probe never sends a turn: ${JSON.stringify(scenario)}`, async (t) => {
    const gateway = await fakeGateway(t, scenario);
    const outcome = await gatewayTurn({
      url: gateway.url, token: "secret", message: "hello", deadline: Date.now() + 80,
    });
    assert.equal(outcome.phase, "not_sent");
    assert.equal(outcome.accepted, false);
    assert.ok(!gateway.calls.includes("chat.send"));
  });
}

test("lost ACK is unknown and never resends; its socket is closed", async (t) => {
  const gateway = await fakeGateway(t, { chat: ({ socket }) => socket.terminate() });
  const result = await gatewayTurn({ url: gateway.url, token: "secret", message: "hello" });
  assert.equal(result.phase, "unknown_outcome");
  assert.equal(result.accepted, null);
  assert.ok(result.runId);
  assert.equal(gateway.calls.filter((method) => method === "chat.send").length, 1);
});

test("accepted empty final completes once with no alternate engine", async (t) => {
  const gateway = await fakeGateway(t, {
    chat: ({ frame, reply, socket }) => {
      reply(true, { status: "accepted", runId: frame.id });
      socket.send(JSON.stringify({ type: "event", event: "chat", payload: {
        runId: frame.id, sessionKey: "agent:main:main", state: "final",
      } }));
    },
  });
  const result = await gatewayTurn({ url: gateway.url, token: "secret", message: "hello" });
  assert.equal(result.phase, "completed");
  assert.equal(result.response, "");
  assert.equal(result.accepted, true);
  assert.equal(gateway.calls.filter((method) => method === "chat.send").length, 1);
});

test("image marker is preserved and subagent events cannot complete the main turn", async (t) => {
  const message = 'look\n\n[OPENCLAW_IMAGES:[{"s3Key":"telegram_1/img.png"}]]';
  const gateway = await fakeGateway(t, {
    chat: ({ frame, reply, socket }) => {
      assert.equal(frame.params.message, message);
      assert.equal(frame.params.sessionKey, "global");
      reply(true, { status: "accepted", runId: frame.id });
      for (const payload of [
        { runId: "sub", sessionKey: "agent:main:subagent:1", state: "final", text: "subagent" },
        { runId: frame.id, sessionKey: "agent:main:main", state: "final", text: "main" },
      ]) socket.send(JSON.stringify({ type: "event", event: "chat", payload }));
    },
  });
  const result = await gatewayTurn({ url: gateway.url, token: "secret", message });
  assert.equal(result.response, "main");
});

test("abort after acceptance keeps unknown outcome and does not resend", async (t) => {
  const abort = new AbortController();
  const gateway = await fakeGateway(t, {
    chat: ({ frame, reply }) => {
      reply(true, { status: "accepted", runId: frame.id });
      setTimeout(() => abort.abort(), 20);
    },
  });
  const result = await gatewayTurn({ url: gateway.url, token: "secret", message: "hello", signal: abort.signal });
  assert.equal(result.phase, "unknown_outcome");
  assert.equal(result.accepted, true);
  assert.equal(gateway.calls.filter((method) => method === "chat.send").length, 1);
});

test("restored instances do not reuse external idempotency keys or RPC IDs with replicated crypto state", async (t) => {
  const retained = new Map();
  let cryptoCalls = 0;
  const gateway = await fakeGateway(t, {
    methods: ["chat.send", "last-heartbeat", "config.get"],
    config: ({ reply, port }) => reply(true, appliedSnapshot(port)),
    chat: ({ frame, reply, socket }) => {
      assert.equal(frame.params.sessionKey, "global");
      assert.equal(frame.params.idempotencyKey, frame.id);
      if (!retained.has(frame.id)) retained.set(frame.id, `effect-${retained.size + 1}`);
      reply(true, { status: "accepted", runId: frame.id });
      socket.send(JSON.stringify({ type: "event", event: "chat", payload: {
        runId: frame.id, sessionKey: "agent:main:main", state: "final", text: retained.get(frame.id),
      } }));
    },
  });
  const source = fs.readFileSync(path.join(__dirname, "gateway-lifecycle.js"), "utf8");
  for (let instance = 0; instance < 3; instance++) {
    const box = vm.createContext({
      module: { exports: {} }, setTimeout, clearTimeout,
      require(name) {
        if (name === "crypto" || name === "node:crypto") {
          return { randomUUID() {
            cryptoCalls++;
            return "11111111-1111-4111-8111-111111111111";
          } };
        }
        return require(name);
      },
    });
    vm.runInContext(source, box);
    await box.module.exports.waitForGateway({ port: gateway.port, token: "secret",
      configProbe: true, deadline: Date.now() + 1000 });
    const result = await box.module.exports.gatewayTurn({
      url: gateway.url, token: "secret", message: "same request", deadline: Date.now() + 1000,
    });
    assert.equal(result.phase, "completed");
    assert.equal(result.response, `effect-${instance + 1}`);
    assert.equal(retained.size, instance + 1);
  }
  assert.equal(cryptoCalls, 0);
  assert.equal(gateway.frames.length, 18);
  assert.equal(new Set(gateway.frames.map((frame) => frame.id)).size, 18);
  assert.equal(gateway.calls.filter((method) => method === "chat.send").length, 3);
});

// ── Startup sweep of stale suspended sub-agent deliveries ───────────────────
// openclaw 2026.9.5 keeps a sub-agent result whose delivery was suspended in
// every main-session turn ("Child results awaiting delivery") for 7 days.
// The bridge dismisses suspended deliveries older than 2 h at startup.

const lifecycle = require("./gateway-lifecycle");

const HOUR = 60 * 60 * 1000;
const NOW = Date.parse("2026-10-14T00:00:00Z");
const TASK_METHODS = ["chat.send", "last-heartbeat", "config.get", "tasks.list", "tasks.dismiss"];

// openclaw 2026.9.5 packages/gateway-protocol (dist/src-*.mjs) closed schemas.
const TASK_SUMMARY_KEYS = new Set([
  "id", "kind", "runtime", "status", "title", "agentId", "sessionKey", "childSessionKey",
  "hasTranscript", "ownerKey", "runId", "taskId", "flowId", "parentTaskId", "sourceId",
  "createdAt", "updatedAt", "startedAt", "endedAt", "toolUseCount", "lastToolName", "execution",
  "lastActivity", "diffStat", "progressSummary", "terminalSummary", "error", "deliveryStatus",
  "terminalOutcome", "result", "prompt",
]);
const TASK_LEDGER_STATUSES = ["queued", "running", "completed", "failed", "cancelled", "timed_out"];
const TASK_DELIVERY_STATUSES = ["pending", "delivered", "session_queued", "failed", "dismissed",
  "parent_missing", "not_applicable"];
const TASK_TERMINAL_OUTCOMES = ["succeeded", "blocked"];
const TASKS_LIST_PARAM_KEYS = new Set(["status", "agentId", "sessionKey", "limit", "cursor", "sortBy"]);

/**
 * One tasks.list entry as mapTaskSummary (dist/task-summary-*.mjs) returns it
 * for a sub-agent whose completion delivery was suspended on expiry:
 * blockSubagentCompletionDelivery writes deliveryStatus "failed" and
 * resolveRequiredCompletionDeliveryFailureTerminalResult writes
 * terminalOutcome "blocked". Ids are placeholders.
 */
function suspendedTask(overrides = {}) {
  return {
    id: "task-old", taskId: "task-old", kind: "subagent", runtime: "subagent", status: "completed",
    execution: { state: "finished" }, title: "placeholder sub-agent label",
    sessionKey: "agent:main:main", childSessionKey: "agent:main:subagent:placeholder",
    hasTranscript: true, ownerKey: "agent:main:main", runId: "run-old",
    createdAt: NOW - 6 * HOUR, updatedAt: NOW - 3 * HOUR, startedAt: NOW - 6 * HOUR, endedAt: NOW - 5 * HOUR,
    terminalSummary: "Required completion delivery failed before reaching the requester: delivery path none did not complete.",
    deliveryStatus: "failed", terminalOutcome: "blocked",
    ...overrides,
  };
}

function recordingLog() {
  const lines = [];
  return { lines, log: (...args) => lines.push(["log", ...args]), warn: (...args) => lines.push(["warn", ...args]) };
}

function assertConstantFormats(lines, ids) {
  for (const [, format] of lines) {
    assert.equal(typeof format, "string");
    assert.match(format, /%s/);
    for (const id of ids) assert.ok(!format.includes(id), `format string must not embed ${id}`);
  }
}

function taskGateway(t, { pages, dismiss, methods = TASK_METHODS } = {}) {
  const dismissCalls = [];
  const listCalls = [];
  return fakeGateway(t, {
    methods,
    handlers: {
      "tasks.list": ({ frame, reply }) => {
        listCalls.push(frame.params);
        for (const key of Object.keys(frame.params)) {
          if (!TASKS_LIST_PARAM_KEYS.has(key)) return reply(false, undefined);
        }
        if (!Number.isInteger(frame.params.limit) || frame.params.limit < 1 || frame.params.limit > 500) {
          return reply(false, undefined);
        }
        const index = frame.params.cursor ? Number(frame.params.cursor.split(".")[1]) : 0;
        const page = pages[index];
        reply(true, { tasks: page, ...(index + 1 < pages.length ? { nextCursor: `1.${index + 1}` } : {}) });
      },
      "tasks.dismiss": ({ frame, reply, socket }) => {
        assert.deepEqual(Object.keys(frame.params), ["taskIds"]);
        assert.ok(frame.params.taskIds.length >= 1 && frame.params.taskIds.length <= 10);
        dismissCalls.push(frame.params.taskIds);
        if (dismiss) return dismiss({ frame, reply, socket });
        reply(true, { results: frame.params.taskIds.map((taskId) => ({
          taskId, ok: true, task: suspendedTask({ id: taskId, taskId, deliveryStatus: "dismissed" }),
        })) });
      },
    },
  }).then((gateway) => ({ ...gateway, dismissCalls, listCalls }));
}

test("fixtures match the openclaw 2026.9.5 task summary schema", () => {
  for (const task of [suspendedTask(), suspendedTask({ deliveryStatus: "dismissed" })]) {
    for (const key of Object.keys(task)) assert.ok(TASK_SUMMARY_KEYS.has(key), `unknown TaskSummary key ${key}`);
    assert.ok(TASK_LEDGER_STATUSES.includes(task.status));
    assert.ok(TASK_DELIVERY_STATUSES.includes(task.deliveryStatus));
    assert.ok(TASK_TERMINAL_OUTCOMES.includes(task.terminalOutcome));
  }
  assert.equal(lifecycle.STALE_DELIVERY_MS, 2 * HOUR);
});

test("an old suspended sub-agent delivery is dismissed exactly once", async (t) => {
  const gateway = await taskGateway(t, { pages: [[suspendedTask()]] });
  const out = recordingLog();
  const result = await lifecycle.dismissStaleSuspendedDeliveries({
    url: gateway.url, token: "secret", now: NOW, log: out, deadline: Date.now() + 2000,
  });
  assert.deepEqual(gateway.dismissCalls, [["task-old"]]);
  assert.deepEqual(gateway.listCalls, [{ status: "completed", limit: 500 }]);
  assert.equal(result.phase, "done");
  assert.deepEqual(result.dismissed, ["task-old"]);
  assert.ok(out.lines.some(([level, format, ...args]) => level === "log" && /Dismissed/.test(format) &&
    args.includes("task-old") && args.includes("run-old")));
  assert.ok(out.lines.some(([level, format, ...args]) => level === "log" && /sweep/.test(format) &&
    args.join(" ") === "1 1 1 0"));
  assertConstantFormats(out.lines, ["task-old", "run-old"]);
  assert.ok(!gateway.calls.includes("chat.send"));
});

test("fresh, delivered, dismissed, intentional non-delivery and non-subagent tasks are left alone", async (t) => {
  const gateway = await taskGateway(t, { pages: [[
    suspendedTask({ id: "fresh", taskId: "fresh", updatedAt: NOW - 30 * 60 * 1000, endedAt: NOW - 40 * 60 * 1000 }),
    suspendedTask({ id: "late-update", taskId: "late-update", updatedAt: NOW - HOUR }),
    suspendedTask({ id: "delivered", taskId: "delivered", deliveryStatus: "delivered", terminalOutcome: "succeeded" }),
    suspendedTask({ id: "dismissed", taskId: "dismissed", deliveryStatus: "dismissed" }),
    // Intentional non-delivery: deliveryStatus "failed" with no blocked outcome.
    suspendedTask({ id: "intentional", taskId: "intentional", terminalOutcome: undefined,
      terminalSummary: undefined }),
    suspendedTask({ id: "cron", taskId: "cron", runtime: "cron", kind: "cron" }),
    suspendedTask({ id: "undated", taskId: "undated", updatedAt: undefined, endedAt: undefined }),
  ]] });
  const result = await lifecycle.dismissStaleSuspendedDeliveries({
    url: gateway.url, token: "secret", now: NOW, log: recordingLog(), deadline: Date.now() + 2000,
  });
  assert.deepEqual(gateway.dismissCalls, []);
  assert.equal(result.phase, "done");
  assert.equal(result.listed, 7);
  assert.deepEqual(result.candidates, []);
});

test("ISO timestamps, pagination and batches of ten follow the 2026.9.5 limits", async (t) => {
  const old = (i) => suspendedTask({ id: `task-${i}`, taskId: `task-${i}`, runId: `run-${i}`,
    updatedAt: new Date(NOW - 3 * HOUR).toISOString(), endedAt: new Date(NOW - 4 * HOUR).toISOString() });
  const gateway = await taskGateway(t, { pages: [
    Array.from({ length: 7 }, (_, i) => old(i)),
    Array.from({ length: 5 }, (_, i) => old(i + 7)),
  ] });
  const result = await lifecycle.dismissStaleSuspendedDeliveries({
    url: gateway.url, token: "secret", now: NOW, log: recordingLog(), deadline: Date.now() + 2000,
  });
  assert.equal(gateway.listCalls.length, 2);
  assert.equal(gateway.listCalls[1].cursor, "1.1");
  assert.deepEqual(gateway.dismissCalls.map((ids) => ids.length), [10, 2]);
  assert.equal(result.dismissed.length, 12);
});

test("a tasks.dismiss error is logged and the sweep still resolves", async (t) => {
  const gateway = await taskGateway(t, {
    pages: [[suspendedTask()]],
    dismiss: ({ frame, socket }) => socket.send(JSON.stringify({ type: "res", id: frame.id, ok: false,
      error: { code: "UNAVAILABLE", message: "dismiss failed" } })),
  });
  const out = recordingLog();
  const result = await lifecycle.dismissStaleSuspendedDeliveries({
    url: gateway.url, token: "secret", now: NOW, log: out, deadline: Date.now() + 2000,
  });
  assert.equal(result.phase, "done");
  assert.deepEqual(result.dismissed, []);
  assert.deepEqual(result.notDismissed, [{ taskId: "task-old", reason: "dismiss failed" }]);
  assert.ok(out.lines.some(([level, , ...args]) => level === "warn" && args.includes("dismiss failed")));
  assertConstantFormats(out.lines, ["task-old"]);
});

test("a per-task refusal (delivery no longer suspended) is logged, not counted", async (t) => {
  const gateway = await taskGateway(t, {
    pages: [[suspendedTask()]],
    dismiss: ({ frame, reply }) => reply(true, { results: [
      { taskId: frame.params.taskIds[0], ok: false, reason: "completion delivery is not blocked" },
    ] }),
  });
  const out = recordingLog();
  const result = await lifecycle.dismissStaleSuspendedDeliveries({
    url: gateway.url, token: "secret", now: NOW, log: out, deadline: Date.now() + 2000,
  });
  assert.deepEqual(result.dismissed, []);
  assert.deepEqual(result.notDismissed, [{ taskId: "task-old", reason: "completion delivery is not blocked" }]);
});

test("list failure, missing task methods, hangs and dead gateways never reject", async (t) => {
  const failing = await fakeGateway(t, { methods: TASK_METHODS, handlers: {
    "tasks.list": ({ reply }) => reply(false, undefined),
  } });
  const out = recordingLog();
  const failed = await lifecycle.dismissStaleSuspendedDeliveries({
    url: failing.url, token: "secret", now: NOW, log: out, deadline: Date.now() + 2000,
  });
  assert.equal(failed.phase, "failed");
  assert.ok(out.lines.some(([level]) => level === "warn"));
  assertConstantFormats(out.lines, []);

  const old = await taskGateway(t, { pages: [[suspendedTask()]], methods: ["chat.send", "last-heartbeat"] });
  const skipped = await lifecycle.dismissStaleSuspendedDeliveries({
    url: old.url, token: "secret", now: NOW, log: recordingLog(), deadline: Date.now() + 2000,
  });
  assert.equal(skipped.phase, "skipped");
  assert.deepEqual(old.listCalls, []);

  const hanging = await fakeGateway(t, { methods: TASK_METHODS, handlers: { "tasks.list": () => {} } });
  const started = Date.now();
  const hung = await lifecycle.dismissStaleSuspendedDeliveries({
    url: hanging.url, token: "secret", now: NOW, log: recordingLog(), deadline: Date.now() + 150,
  });
  assert.equal(hung.phase, "failed");
  assert.ok(Date.now() - started < 1500);

  const dead = await lifecycle.dismissStaleSuspendedDeliveries({
    url: "ws://127.0.0.1:1", token: "secret", now: NOW, log: recordingLog(), deadline: Date.now() + 500,
  });
  assert.equal(dead.phase, "failed");

  const rejected = await fakeGateway(t, { auth: false, methods: TASK_METHODS });
  const unauth = await lifecycle.dismissStaleSuspendedDeliveries({
    url: rejected.url, token: "secret", now: NOW, log: recordingLog(), deadline: Date.now() + 500,
  });
  assert.equal(unauth.phase, "failed");
});

// ── Final delivery: only the run's answer, not the whole run buffer ─────────
// openclaw 2026.9.5 server-chat emitChatDelta/emitChatTerminal join every
// assistant text item of a run into one buffer, so the WS final carries the
// status lines the model wrote before its tool calls. The bridge reads the
// run back with chat.history and delivers the last assistant message.

const HISTORY_METHODS = ["chat.send", "last-heartbeat", "chat.history"];
const commentarySignature = (id) => JSON.stringify({ v: 1, id: `commentary-0-${id}`, phase: "commentary" });

// Transcript shapes as openclaw 2026.9.5 stores them (agent transcript events).
const userMessage = (key, text = "brief please") => ({
  role: "user", content: [{ type: "text", text }], idempotencyKey: key, timestamp: 1,
  __openclaw: { senderIsOwner: true, transport: "gateway" },
});
const commentaryMessage = (run, text, id) => ({
  role: "assistant", stopReason: "toolUse", __openclaw: { runId: run },
  content: [{ type: "text", text, textSignature: commentarySignature(id) },
    { type: "toolCall", id: `call-${id}`, name: "exec", arguments: { command: "true" } }],
});
const toolOnlyMessage = (run, id) => ({
  role: "assistant", stopReason: "toolUse", __openclaw: { runId: run },
  content: [{ type: "toolCall", id: `call-${id}`, name: "exec", arguments: { command: "true" } }],
});
const toolResult = (run, id) => ({
  role: "toolResult", toolCallId: `call-${id}`, toolName: "exec", isError: false,
  __openclaw: { runId: run }, content: [{ type: "text", text: "ok" }],
});
const answerMessage = (run, text) => ({
  role: "assistant", stopReason: "stop", __openclaw: { runId: run }, content: [{ type: "text", text }],
});

const BRIEF = `Morning brief\n\n${"- market item with enough detail to be a real answer\n".repeat(5)}`;

function historyGateway(t, { history, chatEvents, methods = HISTORY_METHODS }) {
  const historyCalls = [];
  return fakeGateway(t, {
    methods,
    chat: ({ frame, reply, socket }) => {
      reply(true, { status: "accepted", runId: frame.id });
      for (const payload of chatEvents(frame.id)) {
        socket.send(JSON.stringify({ type: "event", event: "chat", payload }));
      }
    },
    handlers: {
      "chat.history": ({ frame, reply, socket }) => {
        historyCalls.push(frame.params);
        history({ frame, reply, socket });
      },
    },
  }).then((gateway) => ({ ...gateway, historyCalls }));
}

// Two text items and a tool call, as the gateway streams them: the buffer
// grows "Status A" -> "Status A\n\n<brief>".
const twoItemsAndToolCall = (run) => [
  { runId: run, sessionKey: "agent:main:main", state: "delta", text: "That old result is stale; checking prices now." },
  { runId: run, sessionKey: "agent:main:main", state: "delta", text: `That old result is stale; checking prices now.\n\n${BRIEF}` },
  { runId: run, sessionKey: "agent:main:main", state: "final", text: `That old result is stale; checking prices now.\n\n${BRIEF}` },
];

test("final delivery is the run's last assistant message, not the status lines before tool calls", async (t) => {
  const deltas = [];
  const gateway = await historyGateway(t, {
    chatEvents: twoItemsAndToolCall,
    history: ({ frame, reply }) => {
      const run = gateway.frames.find((f) => f.method === "chat.send").id;
      reply(true, { sessionKey: frame.params.sessionKey, messages: [
        userMessage("earlier-run"), answerMessage("earlier-run", "yesterday's answer"),
        userMessage(run),
        commentaryMessage(run, "That old result is stale; checking prices now.", "a"), toolResult(run, "a"),
        toolOnlyMessage(run, "b"), toolResult(run, "b"),
        answerMessage(run, BRIEF),
      ] });
    },
  });
  const result = await gatewayTurn({ url: gateway.url, token: "secret", message: "brief please",
    onDelta: (text) => deltas.push(text), log: recordingLog() });
  assert.equal(result.phase, "completed");
  assert.equal(result.response, BRIEF.trim());
  assert.equal(result.delivery, "history");
  assert.ok(!result.response.includes("stale"));
  assert.deepEqual(gateway.historyCalls, [{ sessionKey: "agent:main:main", limit: 100, maxChars: 500000 }]);
  // Live streaming is unchanged: the deltas still carry the run buffer.
  assert.equal(deltas.length, 2);
  assert.ok(deltas[1].startsWith("That old result is stale"));
});

test("a short final answer is delivered together with the run's earlier non-commentary text", async (t) => {
  const gateway = await historyGateway(t, {
    chatEvents: twoItemsAndToolCall,
    history: ({ reply }) => {
      const run = gateway.frames.find((f) => f.method === "chat.send").id;
      reply(true, { messages: [
        userMessage(run),
        commentaryMessage(run, "Checking prices now.", "a"), toolResult(run, "a"),
        // Unphased text next to a tool call is content, not commentary.
        { ...toolOnlyMessage(run, "b"), content: [{ type: "text", text: BRIEF },
          { type: "toolCall", id: "call-b", name: "exec", arguments: {} }] }, toolResult(run, "b"),
        answerMessage(run, "Audio is attached."),
      ] });
    },
  });
  const result = await gatewayTurn({ url: gateway.url, token: "secret", message: "brief please", log: recordingLog() });
  assert.equal(result.response, `${BRIEF.trim()}\n\nAudio is attached.`);
  assert.ok(!result.response.includes("Checking prices"));
  assert.equal(result.delivery, "history");
});

test("a short final answer with no other content is delivered alone", async (t) => {
  const gateway = await historyGateway(t, {
    chatEvents: (run) => [{ runId: run, sessionKey: "agent:main:main", state: "final", text: "Working.\n\nDone." }],
    history: ({ reply }) => {
      const run = gateway.frames.find((f) => f.method === "chat.send").id;
      reply(true, { messages: [userMessage(run), commentaryMessage(run, "Working.", "a"), toolResult(run, "a"),
        answerMessage(run, "Done.")] });
    },
  });
  const result = await gatewayTurn({ url: gateway.url, token: "secret", message: "hi", log: recordingLog() });
  assert.equal(result.response, "Done.");
});

for (const failure of ["rejected", "empty", "no-final", "truncated", "timeout", "closed", "capability"]) {
  test(`chat.history ${failure} falls back to the run buffer with a warning`, async (t) => {
    const buffer = `That old result is stale; checking prices now.\n\n${BRIEF}`;
    const gateway = await historyGateway(t, {
      methods: failure === "capability" ? ["chat.send", "last-heartbeat"] : HISTORY_METHODS,
      chatEvents: twoItemsAndToolCall,
      history: ({ reply, socket }) => {
        const run = gateway.frames.find((f) => f.method === "chat.send").id;
        if (failure === "rejected") return reply(false, undefined);
        if (failure === "empty") return reply(true, { messages: [] });
        if (failure === "no-final") return reply(true, { messages: [userMessage(run), commentaryMessage(run, "x", "a")] });
        if (failure === "truncated") return reply(true, { messages: [userMessage(run),
          { ...answerMessage(run, BRIEF), __openclaw: { runId: run, truncated: true, reason: "oversized" } }] });
        if (failure === "closed") return socket.terminate();
      },
    });
    const out = recordingLog();
    const result = await gatewayTurn({ url: gateway.url, token: "secret", message: "brief please", log: out,
      deadline: Date.now() + (failure === "timeout" ? 300 : 20000) });
    assert.equal(result.phase, "completed");
    assert.equal(result.accepted, true);
    assert.equal(result.response, buffer);
    assert.equal(result.delivery, "buffer");
    assert.ok(out.lines.some(([level]) => level === "warn"), "fallback must warn");
    assertConstantFormats(out.lines, [buffer.slice(0, 20)]);
    assert.equal(gateway.calls.filter((method) => method === "chat.send").length, 1);
  });
}

test("history from other runs and sub-agents is never delivered", async (t) => {
  const gateway = await historyGateway(t, {
    chatEvents: (run) => [
      { runId: "sub", sessionKey: "agent:main:subagent:1", state: "final", text: "subagent report" },
      ...twoItemsAndToolCall(run),
    ],
    history: ({ reply }) => {
      const run = gateway.frames.find((f) => f.method === "chat.send").id;
      reply(true, { messages: [
        userMessage(run), answerMessage(run, BRIEF),
        // A later queued input and its run must not replace our answer.
        userMessage("later-run"), answerMessage("later-run", "later answer"),
        answerMessage("sub", "subagent report"),
      ] });
    },
  });
  const result = await gatewayTurn({ url: gateway.url, token: "secret", message: "brief please", log: recordingLog() });
  assert.equal(result.response, BRIEF.trim());
});

test("a yielded run delivers its successor's final answer", async (t) => {
  const gateway = await historyGateway(t, {
    chatEvents: (run) => [
      { runId: run, sessionKey: "agent:main:main", state: "final", yielded: true, text: "waiting" },
      { runId: "successor", sessionKey: "agent:main:main", state: "delta", text: "Status\n\nReal answer" },
      { runId: "successor", sessionKey: "agent:main:main", state: "final", text: "Status\n\nReal answer" },
    ],
    history: ({ reply }) => {
      const run = gateway.frames.find((f) => f.method === "chat.send").id;
      reply(true, { messages: [
        userMessage(run), commentaryMessage(run, "Status", "a"), toolResult(run, "a"),
        answerMessage("successor", BRIEF),
      ] });
    },
  });
  const result = await gatewayTurn({ url: gateway.url, token: "secret", message: "brief please", log: recordingLog() });
  assert.equal(result.response, BRIEF.trim());
});

test("the raw content-block guard (#137) still applies to the history answer", async (t) => {
  const src = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf8");
  const start = src.indexOf("function extractTextFromContent(");
  const end = src.indexOf("\n}\n", start);
  assert.ok(start !== -1 && end !== -1);
  const extractText = new Function(`${src.slice(start, end + 2)}\nreturn extractTextFromContent;`)();
  const raw = JSON.stringify([{ type: "text", text: BRIEF }]);
  const gateway = await historyGateway(t, {
    chatEvents: (run) => [{ runId: run, sessionKey: "agent:main:main", state: "final", message: { content: raw } }],
    history: ({ reply }) => {
      const run = gateway.frames.find((f) => f.method === "chat.send").id;
      reply(true, { messages: [userMessage(run), answerMessage(run, raw)] });
    },
  });
  const result = await gatewayTurn({ url: gateway.url, token: "secret", message: "brief please",
    extractText, log: recordingLog() });
  assert.equal(result.response, BRIEF.trim());
  assert.equal(result.delivery, "history");
});

test("the contract's turn keeps handing the gateway result to delivery unchanged", () => {
  const contract = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf8");
  assert.match(contract, /extractText: extractTextFromContent,/);
  assert.match(contract, /outcome\.response \|\| "The request completed without a text response\."/);
});

test("the contract runs the sweep after gateway readiness and before the first turn", () => {
  const contract = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf8");
  assert.match(contract, /dismissStaleSuspendedDeliveries[^;]*\} = require\("\.\/gateway-lifecycle"\)/s);
  const branch = contract.indexOf("const gatewayBranch = async () => {");
  assert.notEqual(branch, -1);
  const body = contract.slice(branch, contract.indexOf("const browserBranch", branch));
  const ready = body.indexOf("await waitForGateway(");
  const sweep = body.indexOf("await dismissStaleSuspendedDeliveries(");
  const flag = body.indexOf("openclawReady = true;");
  assert.ok(ready !== -1 && sweep > ready && flag > sweep, "sweep must sit between readiness and openclawReady");
  const v2 = contract.indexOf("async function handleV2Invocation(");
  const v2Body = contract.slice(v2, contract.indexOf("\n}\n", v2));
  assert.ok(!v2Body.includes("dismissStaleSuspendedDeliveries"), "handleV2Invocation stays untouched");
});
