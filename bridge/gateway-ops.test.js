/**
 * Stale suspended sub-agent delivery sweep (V1 contract).
 * Run: node --test gateway-ops.test.js
 *
 * openclaw 2026.9.5 renders every sub-agent run whose completion delivery is
 * "suspended" into each main-session turn ("Child results awaiting delivery")
 * for 7 days, and the model narrates it into replies (prod, 10-10..10-15).
 * Once the gateway is ready the contract lists tasks and dismisses
 * suspensions older than 2 h, bounded, without delaying readiness, and before
 * the first queued turn reaches the gateway.
 *
 * The fake gateway speaks protocol v4. Fixtures follow the 2026.9.5 closed
 * TaskSummary schema. All ids are placeholders.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

let WebSocket;
try {
  WebSocket = require("ws");
} catch {
  WebSocket = null;
}
let ops;
try {
  ops = require("./gateway-ops");
} catch {
  ops = null; // b0c427f: module absent, every test below fails on this
}
const skip = !WebSocket && "ws not installed";

const HOUR = 60 * 60 * 1000;
const NOW = Date.parse("2026-10-14T00:00:00Z");
const TASK_METHODS = ["chat.send", "chat.history", "tasks.list", "tasks.dismiss"];

// openclaw 2026.9.5 packages/gateway-protocol closed schemas.
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

function requireOps() {
  assert.ok(ops, "bridge/gateway-ops.js must exist");
  return ops;
}

/** A tasks.list entry for a sub-agent whose completion delivery was suspended on expiry. */
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
  return {
    lines,
    log: (...args) => lines.push(["log", ...args]),
    warn: (...args) => lines.push(["warn", ...args]),
    error: (...args) => lines.push(["error", ...args]),
  };
}

function assertConstantFormats(lines, ids) {
  for (const [, format] of lines) {
    // Constant first argument with values as trailing arguments and no printf
    // placeholders: cloudwatch-logger.js joins arguments as they are.
    assert.equal(typeof format, "string");
    assert.doesNotMatch(format, /%[sdifjoOc]/);
    for (const id of ids) assert.ok(!format.includes(id), `format string must not embed ${id}`);
  }
}

/** Fake protocol-v4 gateway with tasks.list / tasks.dismiss handlers. */
async function taskGateway(t, { pages = [[]], dismiss, list, methods = TASK_METHODS, auth = true } = {}) {
  const calls = [];
  const dismissCalls = [];
  const listCalls = [];
  const wss = new WebSocket.Server({ host: "127.0.0.1", port: 0 });
  wss.on("connection", (socket, request) => {
    assert.equal(request.headers.origin, undefined);
    socket.send(JSON.stringify({ type: "event", event: "connect.challenge", payload: { nonce: "n" } }));
    socket.on("message", (raw) => {
      const frame = JSON.parse(raw);
      calls.push({ method: frame.method, at: Date.now() });
      const reply = (ok, payload, id = frame.id) => socket.send(JSON.stringify({ type: "res", id, ok, payload }));
      if (frame.method === "connect") {
        assert.equal(frame.params.auth.token, "secret");
        return reply(auth, { type: "hello-ok", protocol: 4, features: { methods } });
      }
      if (frame.method === "tasks.list") {
        listCalls.push(frame.params);
        if (list) return list({ frame, reply, socket });
        for (const key of Object.keys(frame.params)) {
          if (!TASKS_LIST_PARAM_KEYS.has(key)) return reply(false, undefined);
        }
        if (!Number.isInteger(frame.params.limit) || frame.params.limit < 1 || frame.params.limit > 500) {
          return reply(false, undefined);
        }
        const index = frame.params.cursor ? Number(frame.params.cursor.split(".")[1]) : 0;
        return reply(true, { tasks: pages[index], ...(index + 1 < pages.length ? { nextCursor: `1.${index + 1}` } : {}) });
      }
      if (frame.method === "tasks.dismiss") {
        assert.deepEqual(Object.keys(frame.params), ["taskIds"]);
        assert.ok(frame.params.taskIds.length >= 1 && frame.params.taskIds.length <= 10);
        dismissCalls.push(frame.params.taskIds);
        if (dismiss) return dismiss({ frame, reply, socket });
        return reply(true, { results: frame.params.taskIds.map((taskId) => ({
          taskId, ok: true, task: suspendedTask({ id: taskId, taskId, deliveryStatus: "dismissed" }),
        })) });
      }
    });
  });
  await new Promise((resolve) => wss.on("listening", resolve));
  t.after(async () => {
    for (const socket of wss.clients) socket.terminate();
    await new Promise((resolve) => wss.close(resolve));
  });
  const port = wss.address().port;
  return { calls, dismissCalls, listCalls, port, url: `ws://127.0.0.1:${port}` };
}

test("fixtures match the openclaw 2026.9.5 task summary schema; threshold is 2 h", () => {
  const { STALE_DELIVERY_MS, STALE_SWEEP_TIMEOUT_MS } = requireOps();
  for (const task of [suspendedTask(), suspendedTask({ deliveryStatus: "dismissed" })]) {
    for (const key of Object.keys(task)) assert.ok(TASK_SUMMARY_KEYS.has(key), `unknown TaskSummary key ${key}`);
    assert.ok(TASK_LEDGER_STATUSES.includes(task.status));
    assert.ok(TASK_DELIVERY_STATUSES.includes(task.deliveryStatus));
    assert.ok(TASK_TERMINAL_OUTCOMES.includes(task.terminalOutcome));
  }
  assert.equal(STALE_DELIVERY_MS, 2 * HOUR);
  assert.equal(STALE_SWEEP_TIMEOUT_MS, 15000);
});

test("an old suspended sub-agent delivery is dismissed exactly once, with counts logged", { skip }, async (t) => {
  const { dismissStaleSuspendedDeliveries } = requireOps();
  const gateway = await taskGateway(t, { pages: [[suspendedTask()]] });
  const out = recordingLog();
  const result = await dismissStaleSuspendedDeliveries({
    url: gateway.url, token: "secret", now: NOW, log: out, deadline: Date.now() + 2000,
  });
  assert.deepEqual(gateway.dismissCalls, [["task-old"]]);
  assert.deepEqual(gateway.listCalls, [{ status: "completed", limit: 500 }]);
  assert.equal(result.phase, "done");
  assert.deepEqual(result.dismissed, ["task-old"]);
  assert.ok(out.lines.some(([level, format, ...args]) => level === "log" && /Dismissed/.test(format) &&
    args.includes("task-old") && args.includes("run-old")));
  assert.ok(out.lines.some(([level, format, ...args]) => level === "log" &&
    [format, ...args].join(" ") === "[gateway] Stale delivery sweep: listed 1 stale 1 dismissed 1 not_dismissed 0"));
  assertConstantFormats(out.lines, ["task-old", "run-old"]);
  assert.ok(!gateway.calls.some((c) => c.method === "chat.send"));
});

test("fresh, delivered, dismissed, intentional non-delivery, non-subagent and undated tasks are left alone", { skip }, async (t) => {
  const { dismissStaleSuspendedDeliveries } = requireOps();
  const gateway = await taskGateway(t, { pages: [[
    suspendedTask({ id: "fresh", taskId: "fresh", updatedAt: NOW - 30 * 60 * 1000, endedAt: NOW - 40 * 60 * 1000 }),
    suspendedTask({ id: "late-update", taskId: "late-update", updatedAt: NOW - HOUR }),
    suspendedTask({ id: "delivered", taskId: "delivered", deliveryStatus: "delivered", terminalOutcome: "succeeded" }),
    suspendedTask({ id: "dismissed", taskId: "dismissed", deliveryStatus: "dismissed" }),
    suspendedTask({ id: "intentional", taskId: "intentional", terminalOutcome: undefined, terminalSummary: undefined }),
    suspendedTask({ id: "cron", taskId: "cron", runtime: "cron", kind: "cron" }),
    suspendedTask({ id: "undated", taskId: "undated", updatedAt: undefined, endedAt: undefined }),
  ]] });
  const out = recordingLog();
  const result = await dismissStaleSuspendedDeliveries({
    url: gateway.url, token: "secret", now: NOW, log: out, deadline: Date.now() + 2000,
  });
  assert.deepEqual(gateway.dismissCalls, []);
  assert.equal(result.phase, "done");
  assert.equal(result.listed, 7);
  assert.deepEqual(result.candidates, []);
  assert.ok(out.lines.some(([, format, ...args]) =>
    [format, ...args].join(" ") === "[gateway] Stale delivery sweep: listed 7 stale 0 dismissed 0 not_dismissed 0"));
});

test("ISO timestamps, pagination and batches of ten follow the 2026.9.5 limits", { skip }, async (t) => {
  const { dismissStaleSuspendedDeliveries } = requireOps();
  const old = (i) => suspendedTask({ id: `task-${i}`, taskId: `task-${i}`, runId: `run-${i}`,
    updatedAt: new Date(NOW - 3 * HOUR).toISOString(), endedAt: new Date(NOW - 4 * HOUR).toISOString() });
  const gateway = await taskGateway(t, { pages: [
    Array.from({ length: 7 }, (_, i) => old(i)),
    Array.from({ length: 5 }, (_, i) => old(i + 7)),
  ] });
  const result = await dismissStaleSuspendedDeliveries({
    url: gateway.url, token: "secret", now: NOW, log: recordingLog(), deadline: Date.now() + 2000,
  });
  assert.equal(gateway.listCalls.length, 2);
  assert.equal(gateway.listCalls[1].cursor, "1.1");
  assert.deepEqual(gateway.dismissCalls.map((ids) => ids.length), [10, 2]);
  assert.equal(result.dismissed.length, 12);
});

test("a tasks.dismiss error or a per-task refusal is logged, not counted, and the sweep resolves", { skip }, async (t) => {
  const { dismissStaleSuspendedDeliveries } = requireOps();
  const failing = await taskGateway(t, {
    pages: [[suspendedTask()]],
    dismiss: ({ frame, socket }) => socket.send(JSON.stringify({ type: "res", id: frame.id, ok: false,
      error: { code: "UNAVAILABLE", message: "dismiss failed" } })),
  });
  let out = recordingLog();
  let result = await dismissStaleSuspendedDeliveries({
    url: failing.url, token: "secret", now: NOW, log: out, deadline: Date.now() + 2000,
  });
  assert.equal(result.phase, "done");
  assert.deepEqual(result.dismissed, []);
  assert.deepEqual(result.notDismissed, [{ taskId: "task-old", reason: "dismiss failed" }]);
  assert.ok(out.lines.some(([level, , ...args]) => level === "warn" && args.includes("dismiss failed")));
  assertConstantFormats(out.lines, ["task-old"]);

  const refusing = await taskGateway(t, {
    pages: [[suspendedTask()]],
    dismiss: ({ frame, reply }) => reply(true, { results: [
      { taskId: frame.params.taskIds[0], ok: false, reason: "completion delivery is not blocked" },
    ] }),
  });
  out = recordingLog();
  result = await dismissStaleSuspendedDeliveries({
    url: refusing.url, token: "secret", now: NOW, log: out, deadline: Date.now() + 2000,
  });
  assert.deepEqual(result.dismissed, []);
  assert.deepEqual(result.notDismissed, [{ taskId: "task-old", reason: "completion delivery is not blocked" }]);
  assertConstantFormats(out.lines, ["task-old"]);
});

test("list failure, missing task methods, a hang, a dead port and an auth rejection never reject", { skip }, async (t) => {
  const { dismissStaleSuspendedDeliveries } = requireOps();
  const failing = await taskGateway(t, { list: ({ reply }) => reply(false, undefined) });
  const out = recordingLog();
  const failed = await dismissStaleSuspendedDeliveries({
    url: failing.url, token: "secret", now: NOW, log: out, deadline: Date.now() + 2000,
  });
  assert.equal(failed.phase, "failed");
  assert.ok(out.lines.some(([level]) => level === "warn"));
  assertConstantFormats(out.lines, []);

  const old = await taskGateway(t, { pages: [[suspendedTask()]], methods: ["chat.send"] });
  const skipped = await dismissStaleSuspendedDeliveries({
    url: old.url, token: "secret", now: NOW, log: recordingLog(), deadline: Date.now() + 2000,
  });
  assert.equal(skipped.phase, "skipped");
  assert.deepEqual(old.listCalls, []);

  const hanging = await taskGateway(t, { list: () => {} });
  const started = Date.now();
  const hung = await dismissStaleSuspendedDeliveries({
    url: hanging.url, token: "secret", now: NOW, log: recordingLog(), deadline: Date.now() + 150,
  });
  assert.equal(hung.phase, "failed");
  assert.ok(Date.now() - started < 1500);

  const dead = await dismissStaleSuspendedDeliveries({
    url: "ws://127.0.0.1:1", token: "secret", now: NOW, log: recordingLog(), deadline: Date.now() + 500,
  });
  assert.equal(dead.phase, "failed");

  const rejected = await taskGateway(t, { auth: false });
  const unauth = await dismissStaleSuspendedDeliveries({
    url: rejected.url, token: "secret", now: NOW, log: recordingLog(), deadline: Date.now() + 500,
  });
  assert.equal(unauth.phase, "failed");
});

// ── Contract wiring: the real pollOpenClawReadiness + message queue ──────────

const CONTRACT_SRC = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf8");

function sliceFunction(src, signature) {
  const start = src.indexOf(signature);
  assert.notEqual(start, -1, `missing ${signature}`);
  const end = src.indexOf("\n}\n", start);
  assert.notEqual(end, -1, `unterminated ${signature}`);
  return src.slice(start, end + 2);
}

/**
 * Evaluate the shipped readiness poller and queue with stubbed neighbours.
 * `bridgeMessage` records when each turn reaches the gateway.
 */
function loadReadinessAndQueue({ port, gatewayOps, logs }) {
  const body = [
    "let openclawReady = false;",
    "let messageQueue = [];",
    "let processingMessage = false;",
    "let staleDeliverySweep = null;",
    "const sessionStorageActive = false;",
    sliceFunction(CONTRACT_SRC, "function startStaleDeliverySweep("),
    sliceFunction(CONTRACT_SRC, "async function pollOpenClawReadiness("),
    sliceFunction(CONTRACT_SRC, "async function processMessageQueue("),
    sliceFunction(CONTRACT_SRC, "function enqueueMessage("),
    "return { pollOpenClawReadiness, enqueueMessage, isReady: () => openclawReady };",
  ].join("\n");
  const turns = [];
  const quiet = {
    log: (...a) => logs.push(a.join(" ")),
    warn: (...a) => logs.push(a.join(" ")),
    error: (...a) => logs.push(a.join(" ")),
  };
  const noop = () => {};
  const factory = new Function(
    "OPENCLAW_PORT", "GATEWAY_TOKEN", "gatewayOps", "waitForPort", "workspaceSync", "stateStorage",
    "startRuntimeSkillManifestBackup", "startRuntimeSkillReinstall", "OPENCLAW_DIR", "MOUNTED_OPENCLAW_DIR",
    "bridgeMessage", "console", body,
  );
  const api = factory(
    port, "secret", gatewayOps, async () => true,
    { startPeriodicSave: noop, startChangeBackup: noop }, { startPeriodicMirror: noop },
    noop, noop, "/tmp/none", "/tmp/none",
    async (message) => {
      turns.push({ message, at: Date.now() });
      return `reply to ${message}`;
    },
    quiet,
  );
  return { ...api, turns };
}

test("contract: once ready, the sweep dismisses before the first queued turn reaches the gateway", { skip }, async (t) => {
  const real = requireOps();
  const old = suspendedTask({ updatedAt: Date.now() - 3 * HOUR, endedAt: Date.now() - 4 * HOUR });
  const gateway = await taskGateway(t, {
    pages: [[old]],
    // Slow list: a turn sent without waiting for the sweep would win the race.
    list: ({ reply }) => setTimeout(() => reply(true, { tasks: [old] }), 150),
  });
  const logs = [];
  const contract = loadReadinessAndQueue({ port: gateway.port, gatewayOps: real, logs });
  await contract.pollOpenClawReadiness("placeholder-namespace");
  assert.equal(contract.isReady(), true, "readiness is not held back by the sweep");
  const reply = await contract.enqueueMessage("first turn");
  assert.equal(reply, "reply to first turn");
  assert.deepEqual(gateway.dismissCalls, [["task-old"]]);
  const dismissAt = gateway.calls.find((c) => c.method === "tasks.dismiss").at;
  assert.ok(contract.turns[0].at >= dismissAt, "the first turn waits for the sweep");
  assert.ok(logs.some((l) => l === "[gateway] Stale delivery sweep: listed 1 stale 1 dismissed 1 not_dismissed 0"));
});

test("contract: a hung sweep delays the first turn only up to its bound and never blocks readiness", { skip }, async (t) => {
  const real = requireOps();
  const gateway = await taskGateway(t, { list: () => {} });
  const logs = [];
  const bounded = { ...real, STALE_SWEEP_TIMEOUT_MS: 200 };
  const contract = loadReadinessAndQueue({ port: gateway.port, gatewayOps: bounded, logs });
  const started = Date.now();
  await contract.pollOpenClawReadiness("placeholder-namespace");
  assert.equal(contract.isReady(), true);
  assert.ok(Date.now() - started < 100, "readiness does not wait for the sweep");
  const reply = await contract.enqueueMessage("first turn");
  assert.equal(reply, "reply to first turn");
  const waited = contract.turns[0].at - started;
  assert.ok(waited >= 150 && waited < 1500, `first turn waited ${waited} ms`);
  assert.ok(logs.some((l) => l.startsWith("[gateway] Stale delivery sweep failed:")));
});

test("contract: a sweep against a dead gateway still lets turns run", { skip }, async () => {
  const real = requireOps();
  const logs = [];
  const contract = loadReadinessAndQueue({ port: 1, gatewayOps: real, logs });
  await contract.pollOpenClawReadiness("placeholder-namespace");
  assert.equal(await contract.enqueueMessage("a"), "reply to a");
  assert.equal(await contract.enqueueMessage("b"), "reply to b");
  assert.deepEqual(contract.turns.map((turn) => turn.message), ["a", "b"]);
});

test("both Dockerfiles copy gateway-ops.js into /app", () => {
  for (const file of [path.join(__dirname, "Dockerfile"),
    path.join(__dirname, "..", ".bedrock_agentcore", "openclaw_agent", "Dockerfile")]) {
    assert.match(fs.readFileSync(file, "utf8"), /^COPY gateway-ops\.js \/app\/gateway-ops\.js$/m, file);
  }
});

test("gateway-ops.js log lines have constant first arguments without printf placeholders", () => {
  const src = fs.readFileSync(path.join(__dirname, "gateway-ops.js"), "utf8");
  const calls = [...src.matchAll(/log\?\.(?:log|warn)\?\.\(\s*("(?:[^"\\]|\\.)*")/g)];
  assert.ok(calls.length >= 5);
  for (const [, literal] of calls) assert.doesNotMatch(literal, /%[sdifjoOc]/, literal);
});
