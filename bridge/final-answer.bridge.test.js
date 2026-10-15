/**
 * Final-answer delivery: the contract's real bridgeMessage() against a fake
 * openclaw 2026.9.5 gateway that advertises chat.history.
 * Run: node --test final-answer.bridge.test.js
 *
 * The WS chat final carries the run's whole text buffer (every assistant item
 * joined with blank lines), so the status lines a model writes before tool
 * calls ("That 10/8 result is stale...", "switching to gTTS") reached users.
 * On our run's final the contract reads the run back with chat.history and
 * delivers the last non-commentary assistant message; any history problem
 * falls back to the buffer. Live deltas are unchanged.
 *
 * bridgeMessage is sliced out of agentcore-contract.js (a server entry point)
 * and evaluated with injected dependencies, so this runs the shipped code.
 * Transcript shapes follow the 2026.9.5 agent transcript: user
 * `idempotencyKey`, assistant `__openclaw.runId`, commentary `textSignature`
 * JSON strings, toolCall blocks. Ids are placeholders.
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
const skip = !WebSocket && "ws not installed";

const MAIN_KEY = "agent:main:main";
const SUB_KEY = "agent:main:subagent:0000aaaa-1111-2222-3333-444455556666";
const HISTORY_METHODS = ["chat.send", "chat.history", "tasks.list", "tasks.dismiss"];
const SRC = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf8");

function sliceFunction(src, signature) {
  const start = src.indexOf(signature);
  assert.notEqual(start, -1, `missing ${signature}`);
  const end = src.indexOf("\n}\n", start);
  assert.notEqual(end, -1, `unterminated ${signature}`);
  return src.slice(start, end + 2);
}

function loadBridgeMessage(port, logs) {
  const body = [
    sliceFunction(SRC, "function extractTextFromContent("),
    sliceFunction(SRC, "async function bridgeMessage("),
    "return bridgeMessage;",
  ].join("\n");
  const { createChatRunTracker } = require("./chat-run-filter");
  const record = (level) => (...args) => logs.push([level, ...args]);
  const quiet = { log: record("log"), warn: record("warn"), error: record("error") };
  const factory = new Function(
    "WebSocket", "OPENCLAW_PORT", "GATEWAY_TOKEN", "createChatRunTracker", "require", "console", body,
  );
  return factory(WebSocket, port, "test-token", createChatRunTracker, require, quiet);
}

const extractTextFromContent = new Function(
  `${sliceFunction(SRC, "function extractTextFromContent(")}\nreturn extractTextFromContent;`,
)();

/**
 * Fake gateway. `chatEvents(runId)` lists chat event payloads sent after the
 * chat.send ack; `history({frame, reply, socket, runId})` answers chat.history.
 */
async function historyGateway(t, { chatEvents, history = () => {}, methods = HISTORY_METHODS }) {
  const frames = [];
  const historyCalls = [];
  let runId;
  const wss = new WebSocket.Server({ host: "127.0.0.1", port: 0 });
  wss.on("connection", (socket) => {
    socket.send(JSON.stringify({ type: "event", event: "connect.challenge", payload: { nonce: "n" } }));
    socket.on("message", (raw) => {
      const frame = JSON.parse(raw);
      frames.push(frame);
      const reply = (ok, payload, id = frame.id) => socket.send(JSON.stringify({ type: "res", id, ok, payload }));
      if (frame.method === "connect") {
        return reply(true, { type: "hello-ok", protocol: 4, features: { methods } });
      }
      if (frame.method === "chat.send") {
        runId = frame.params.idempotencyKey;
        reply(true, { runId, status: "started" });
        for (const payload of chatEvents(runId)) {
          socket.send(JSON.stringify({ type: "event", event: "chat", payload }));
        }
        return;
      }
      if (frame.method === "chat.history") {
        historyCalls.push(frame.params);
        return history({ frame, reply, socket, runId });
      }
    });
  });
  await new Promise((resolve) => wss.on("listening", resolve));
  t.after(async () => {
    for (const socket of wss.clients) socket.terminate();
    await new Promise((resolve) => wss.close(resolve));
  });
  return { frames, historyCalls, port: wss.address().port, chatSends: () => frames.filter((f) => f.method === "chat.send").length };
}

const commentarySignature = (id) => JSON.stringify({ v: 1, id: `commentary-0-${id}`, phase: "commentary" });
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
const textMsg = (text) => ({ role: "assistant", content: [{ type: "text", text }] });

const STATUS = "That old result is stale; checking prices now.";
const BRIEF = `Morning brief\n\n${"- market item with enough detail to be a real answer\n".repeat(5)}`;
const BUFFER = `${STATUS}\n\n${BRIEF}`;

// Two text items and a tool call, as the gateway streams them.
const twoItemsAndToolCall = (run) => [
  { runId: run, sessionKey: MAIN_KEY, seq: 1, state: "delta", message: textMsg(STATUS) },
  { runId: run, sessionKey: MAIN_KEY, seq: 2, state: "delta", message: textMsg(BUFFER) },
  { runId: run, sessionKey: MAIN_KEY, seq: 3, state: "final", message: textMsg(BUFFER) },
];
const runAnswer = (run) => ({ messages: [userMessage(run), commentaryMessage(run, STATUS, "a"),
  toolResult(run, "a"), answerMessage(run, BRIEF)] });

// The lines this change adds. (The contract's existing `WS rx` frame log
// embeds frame text by design and is not checked here.)
const NEW_LINES = /^\[contract\] (Delivering|chat\.history|Empty final:)/;
function assertConstantFormats(logs, fragments) {
  const added = logs.filter(([, format]) => typeof format === "string" && NEW_LINES.test(format));
  assert.ok(added.length > 0);
  for (const [, format] of added) {
    assert.doesNotMatch(format, /%[sdifjoOc]/);
    for (const fragment of fragments) assert.ok(!format.includes(fragment), "log format must not embed reply text");
  }
}
const historyLine = (logs) => logs.find(([, format]) => typeof format === "string" &&
  format.startsWith("[contract] Delivering the run's final answer from chat.history"));
const bufferLine = (logs) => logs.find(([level, format]) => level === "warn" && typeof format === "string" &&
  format.startsWith("[contract] Delivering the run text buffer"));

test("delivers the run's last assistant message, not the status lines before tool calls", { skip }, async (t) => {
  const gateway = await historyGateway(t, {
    chatEvents: twoItemsAndToolCall,
    history: ({ frame, reply, runId }) => reply(true, { sessionKey: frame.params.sessionKey, messages: [
      userMessage("earlier-run"), answerMessage("earlier-run", "yesterday's answer"),
      userMessage(runId),
      commentaryMessage(runId, STATUS, "a"), toolResult(runId, "a"),
      toolOnlyMessage(runId, "b"), toolResult(runId, "b"),
      answerMessage(runId, BRIEF),
    ] }),
  });
  const logs = [];
  const deltas = [];
  const text = await loadBridgeMessage(gateway.port, logs)("brief please", 5000, (d) => deltas.push(d));
  assert.equal(text, BRIEF.trim());
  assert.ok(!text.includes("stale"));
  assert.deepEqual(gateway.historyCalls, [{ sessionKey: MAIN_KEY, limit: 100, maxChars: 500000 }]);
  // Live streaming is unchanged: the deltas still carry the run buffer.
  assert.deepEqual(deltas, [STATUS, BUFFER]);
  const line = historyLine(logs);
  assert.ok(line, "logs the history delivery");
  assert.deepEqual(line.slice(2), [BRIEF.trim().length, 1, BUFFER.length]);
  assertConstantFormats(logs, [BRIEF.slice(0, 20), STATUS.slice(0, 20)]);
  assert.equal(gateway.chatSends(), 1);
});

test("a short final answer is delivered together with the run's earlier non-commentary text", { skip }, async (t) => {
  const gateway = await historyGateway(t, {
    chatEvents: twoItemsAndToolCall,
    history: ({ reply, runId }) => reply(true, { messages: [
      userMessage(runId),
      commentaryMessage(runId, "Checking prices now.", "a"), toolResult(runId, "a"),
      // Unphased text next to a tool call is content, not commentary.
      { ...toolOnlyMessage(runId, "b"), content: [{ type: "text", text: BRIEF },
        { type: "toolCall", id: "call-b", name: "exec", arguments: {} }] }, toolResult(runId, "b"),
      answerMessage(runId, "Audio is in the brief folder."),
    ] }),
  });
  const text = await loadBridgeMessage(gateway.port, [])("brief please", 5000);
  assert.equal(text, `${BRIEF.trim()}\n\nAudio is in the brief folder.`);
  assert.ok(!text.includes("Checking prices"));
});

test("a short final answer with only commentary before it is delivered alone", { skip }, async (t) => {
  const gateway = await historyGateway(t, {
    chatEvents: (run) => [{ runId: run, sessionKey: MAIN_KEY, seq: 1, state: "final", message: textMsg("Working.\n\nDone.") }],
    history: ({ reply, runId }) => reply(true, { messages: [userMessage(runId),
      commentaryMessage(runId, "Working.", "a"), toolResult(runId, "a"), answerMessage(runId, "Done.")] }),
  });
  assert.equal(await loadBridgeMessage(gateway.port, [])("hi", 5000), "Done.");
});

for (const failure of ["rejected", "empty", "no-final", "truncated", "closed"]) {
  test(`chat.history ${failure}: falls back to the run buffer with a warning`, { skip }, async (t) => {
    const gateway = await historyGateway(t, {
      chatEvents: twoItemsAndToolCall,
      history: ({ reply, socket, runId }) => {
        if (failure === "rejected") return reply(false, undefined);
        if (failure === "empty") return reply(true, { messages: [] });
        if (failure === "no-final") return reply(true, { messages: [userMessage(runId), commentaryMessage(runId, "x", "a")] });
        if (failure === "truncated") return reply(true, { messages: [userMessage(runId),
          { ...answerMessage(runId, BRIEF), __openclaw: { runId, truncated: true, reason: "oversized" } }] });
        if (failure === "closed") return socket.terminate();
      },
    });
    const logs = [];
    const text = await loadBridgeMessage(gateway.port, logs)("brief please", 5000);
    assert.equal(text, BUFFER);
    assert.ok(bufferLine(logs), "fallback must warn");
    assert.equal(bufferLine(logs)[2], BUFFER.length);
    assertConstantFormats(logs, [BRIEF.slice(0, 20)]);
    assert.equal(gateway.chatSends(), 1);
  });
}

test("a gateway without chat.history keeps the old buffer delivery and sends no history request", { skip }, async (t) => {
  const gateway = await historyGateway(t, { methods: ["chat.send"], chatEvents: twoItemsAndToolCall });
  const text = await loadBridgeMessage(gateway.port, [])("brief please", 5000);
  assert.equal(text, BUFFER);
  assert.deepEqual(gateway.historyCalls, []);
});

test("a chat.history reply slower than 5 s still delivers the final answer (snapshot thaw)", { skip }, async (t) => {
  const gateway = await historyGateway(t, {
    chatEvents: twoItemsAndToolCall,
    history: ({ reply, runId }) => {
      const timer = setTimeout(() => reply(true, runAnswer(runId)), 5600);
      t.after(() => clearTimeout(timer));
    },
  });
  const logs = [];
  const text = await loadBridgeMessage(gateway.port, logs)("brief please", 20000);
  assert.equal(text, BRIEF.trim());
  assert.equal(gateway.historyCalls.length, 1);
  assert.ok(!logs.some(([level]) => level === "warn"));
});

test("a chat.history request that gets no reply is retried once, and the answer delivered", { skip }, async (t) => {
  const gateway = await historyGateway(t, {
    chatEvents: twoItemsAndToolCall,
    history: ({ reply, runId }) => {
      if (gateway.historyCalls.length === 2) reply(true, runAnswer(runId));
    },
  });
  const logs = [];
  const text = await loadBridgeMessage(gateway.port, logs)("brief please", 20000, undefined, { historyTimeoutMs: 200 });
  assert.equal(text, BRIEF.trim());
  assert.equal(gateway.historyCalls.length, 2);
  assert.deepEqual(gateway.historyCalls[1], gateway.historyCalls[0]);
  assert.ok(logs.some(([level, format]) => level === "warn" && /retrying once/.test(format)));
});

test("a late reply to the first chat.history request is still used after the retry", { skip }, async (t) => {
  let first;
  const gateway = await historyGateway(t, {
    chatEvents: twoItemsAndToolCall,
    history: ({ frame, reply, runId }) => {
      if (gateway.historyCalls.length === 1) { first = frame.id; return; }
      reply(true, runAnswer(runId), first);
    },
  });
  const text = await loadBridgeMessage(gateway.port, [])("brief please", 20000, undefined, { historyTimeoutMs: 200 });
  assert.equal(text, BRIEF.trim());
});

test("a chat.history that never answers falls back after two bounded attempts", { skip }, async (t) => {
  const gateway = await historyGateway(t, { chatEvents: twoItemsAndToolCall });
  const logs = [];
  const started = Date.now();
  const text = await loadBridgeMessage(gateway.port, logs)("brief please", 20000, undefined, { historyTimeoutMs: 200 });
  const elapsed = Date.now() - started;
  assert.equal(text, BUFFER);
  assert.equal(gateway.historyCalls.length, 2);
  assert.ok(elapsed >= 400 && elapsed < 2000, `fallback after two 200 ms attempts, took ${elapsed} ms`);
  assert.equal(bufferLine(logs)[3], "chat.history timed out");
});

test("the chat.history wait never runs past the turn deadline", { skip }, async (t) => {
  const gateway = await historyGateway(t, { chatEvents: twoItemsAndToolCall });
  const started = Date.now();
  const text = await loadBridgeMessage(gateway.port, [])("brief please", 1500);
  assert.equal(text, BUFFER);
  assert.ok(Date.now() - started <= 1600, "the default 15 s attempt is cut to the turn deadline");
  assert.equal(gateway.historyCalls.length, 1);
});

test("history from other runs, later queued inputs and sub-agents is never delivered (#135)", { skip }, async (t) => {
  const gateway = await historyGateway(t, {
    chatEvents: (run) => [
      { runId: "sub-run", sessionKey: SUB_KEY, spawnedBy: MAIN_KEY, seq: 1, state: "final", message: textMsg("SUB-AGENT REPORT") },
      ...twoItemsAndToolCall(run),
    ],
    history: ({ reply, runId }) => reply(true, { messages: [
      userMessage(runId), answerMessage(runId, BRIEF),
      userMessage("later-run"), answerMessage("later-run", "later answer"),
      answerMessage("sub-run", "SUB-AGENT REPORT"),
    ] }),
  });
  const text = await loadBridgeMessage(gateway.port, [])("brief please", 5000);
  assert.equal(text, BRIEF.trim());
  assert.equal(gateway.historyCalls.length, 1, "a sub-agent final does not trigger chat.history");
});

test("a sessions_yield run delivers its successor's final answer", { skip }, async (t) => {
  const gateway = await historyGateway(t, {
    chatEvents: (run) => [
      { runId: run, sessionKey: MAIN_KEY, seq: 1, state: "final", yielded: true, message: textMsg("waiting") },
      { runId: "successor-run", sessionKey: MAIN_KEY, seq: 1, state: "delta", message: textMsg("Status\n\nReal answer") },
      { runId: "successor-run", sessionKey: MAIN_KEY, seq: 2, state: "final", message: textMsg("Status\n\nReal answer") },
    ],
    history: ({ reply, runId }) => reply(true, { messages: [
      userMessage(runId), commentaryMessage(runId, "Status", "a"), toolResult(runId, "a"),
      answerMessage("successor-run", BRIEF),
    ] }),
  });
  assert.equal(await loadBridgeMessage(gateway.port, [])("brief please", 5000), BRIEF.trim());
});

test("the raw content-block guard (#137) applies to the history answer", { skip }, async (t) => {
  const raw = JSON.stringify([{ type: "text", text: BRIEF }]);
  const gateway = await historyGateway(t, {
    chatEvents: (run) => [{ runId: run, sessionKey: MAIN_KEY, seq: 1, state: "final", message: { content: raw } }],
    history: ({ reply, runId }) => reply(true, { messages: [userMessage(runId), answerMessage(runId, raw)] }),
  });
  const text = await loadBridgeMessage(gateway.port, [])("brief please", 5000);
  assert.equal(text, extractTextFromContent(raw).trim());
  assert.ok(!text.includes('"type"'));
});

test("an empty WS final still reads the answer from chat.history", { skip }, async (t) => {
  const gateway = await historyGateway(t, {
    chatEvents: (run) => [{ runId: run, sessionKey: MAIN_KEY, seq: 1, state: "final" }],
    history: ({ reply, runId }) => reply(true, runAnswer(runId)),
  });
  assert.equal(await loadBridgeMessage(gateway.port, [])("brief please", 5000), BRIEF.trim());
});
