/**
 * End-to-end test of the contract's real bridgeMessage() against a fake
 * OpenClaw gateway speaking protocol v4 over WebSocket.
 * Run: node --test chat-run-filter.bridge.test.js
 *
 * bridgeMessage is not exported (agentcore-contract.js is a server entry
 * point), so we slice its source and extractTextFromContent's out of the file
 * and evaluate them with injected dependencies. This runs the shipped code,
 * not a mirror. Skips when the `ws` package is not installed.
 */
const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

let WebSocket;
try {
  WebSocket = require("ws");
} catch {
  WebSocket = null;
}

const MAIN_KEY = "agent:main:main";
const SUB_KEY = "agent:main:subagent:0000aaaa-1111-2222-3333-444455556666";

function sliceFunction(src, signature) {
  const start = src.indexOf(signature);
  assert.notEqual(start, -1, `missing ${signature}`);
  const end = src.indexOf("\n}\n", start);
  assert.notEqual(end, -1, `unterminated ${signature}`);
  return src.slice(start, end + 2);
}

function loadBridgeMessage(port, logs) {
  const src = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf-8");
  const body = [
    sliceFunction(src, "function extractTextFromContent("),
    sliceFunction(src, "async function bridgeMessage("),
    "return bridgeMessage;",
  ].join("\n");
  let createChatRunTracker;
  try {
    ({ createChatRunTracker } = require("./chat-run-filter"));
  } catch {
    createChatRunTracker = undefined; // main: module absent, contract does not use it
  }
  const quiet = {
    log: (...a) => logs.push(a.join(" ")),
    warn: (...a) => logs.push(a.join(" ")),
    error: (...a) => logs.push(a.join(" ")),
  };
  const factory = new Function(
    "WebSocket", "OPENCLAW_PORT", "GATEWAY_TOKEN", "createChatRunTracker", "require", "console",
    body,
  );
  return factory(WebSocket, port, "test-token", createChatRunTracker, require, quiet);
}

/**
 * Fake gateway. `script(runId)` returns the chat event payloads to broadcast
 * after the chat.send ack; `null` entries insert a short pause.
 */
function startGateway() {
  const wss = new WebSocket.Server({ host: "127.0.0.1", port: 0 });
  let script = () => [];
  wss.on("connection", (sock) => {
    sock.send(JSON.stringify({ type: "event", event: "connect.challenge", payload: { nonce: "n" } }));
    sock.on("message", async (data) => {
      const req = JSON.parse(data.toString());
      if (req.method === "connect") {
        sock.send(JSON.stringify({ type: "res", id: req.id, ok: true, payload: { type: "hello-ok" } }));
        return;
      }
      if (req.method === "chat.send") {
        const runId = req.params.idempotencyKey;
        sock.send(JSON.stringify({ type: "res", id: req.id, ok: true, payload: { runId, status: "started" } }));
        for (const payload of script(runId)) {
          if (payload === null) {
            await new Promise((r) => setTimeout(r, 20));
            continue;
          }
          if (sock.readyState !== WebSocket.OPEN) return;
          sock.send(JSON.stringify({ type: "event", event: "chat", payload }));
        }
      }
    });
  });
  return new Promise((resolve) => {
    wss.on("listening", () =>
      resolve({
        port: wss.address().port,
        setScript: (fn) => {
          script = fn;
        },
        close: () => new Promise((r) => wss.close(r)),
      }),
    );
  });
}

const textMsg = (text) => ({ role: "assistant", content: [{ type: "text", text }] });

describe("bridgeMessage against a fake gateway", { skip: !WebSocket && "ws not installed" }, () => {
  let gw;
  before(async () => {
    gw = await startGateway();
  });
  after(async () => {
    if (gw) await gw.close();
  });

  it("does not resolve with a sub-agent final that arrives before the main final", async () => {
    const logs = [];
    const bridgeMessage = loadBridgeMessage(gw.port, logs);
    gw.setScript((runId) => [
      { runId, sessionKey: MAIN_KEY, seq: 1, state: "delta", message: textMsg("Working on the brief") },
      { runId: "sub-run", sessionKey: SUB_KEY, spawnedBy: MAIN_KEY, seq: 1, state: "delta", message: textMsg("sub partial") },
      { runId: "sub-run", sessionKey: SUB_KEY, spawnedBy: MAIN_KEY, seq: 2, state: "final", message: textMsg("SUB-AGENT REPORT") },
      null,
      { runId, sessionKey: MAIN_KEY, seq: 2, state: "final", message: textMsg("MAIN BRIEF") },
    ]);
    const deltas = [];
    const text = await bridgeMessage("hello", 5000, (t) => deltas.push(t));
    assert.equal(text, "MAIN BRIEF");
    assert.ok(!deltas.includes("sub partial"), "sub-agent delta must not stream");
    assert.ok(logs.some((l) => l.includes("Ignoring chat")), "ignored events are logged");
  });

  it("sub-agent deltas do not replace responseText when the main run times out", async () => {
    const logs = [];
    const bridgeMessage = loadBridgeMessage(gw.port, logs);
    gw.setScript((runId) => [
      { runId, sessionKey: MAIN_KEY, seq: 1, state: "delta", message: textMsg("main partial") },
      { runId: "sub-run", sessionKey: SUB_KEY, seq: 1, state: "delta", message: textMsg("sub partial") },
    ]);
    const text = await bridgeMessage("hello", 300);
    assert.equal(text, "main partial");
  });

  it("an empty final from the main run still resolves with an empty string", async () => {
    const logs = [];
    const bridgeMessage = loadBridgeMessage(gw.port, logs);
    gw.setScript((runId) => [{ runId, sessionKey: MAIN_KEY, seq: 1, state: "final" }]);
    const text = await bridgeMessage("hello", 5000);
    assert.equal(text, "");
  });

  it("a sub-agent error does not end the request; the main final does", async () => {
    const logs = [];
    const bridgeMessage = loadBridgeMessage(gw.port, logs);
    gw.setScript((runId) => [
      { runId: "sub-run", sessionKey: SUB_KEY, seq: 1, state: "error", errorMessage: "sub failed" },
      { runId: "sub-run", sessionKey: SUB_KEY, seq: 2, state: "aborted" },
      null,
      { runId, sessionKey: MAIN_KEY, seq: 1, state: "final", message: textMsg("MAIN OK") },
    ]);
    const text = await bridgeMessage("hello", 5000);
    assert.equal(text, "MAIN OK");
  });

  it("follows a sessions_yield handoff to the successor run", async () => {
    const logs = [];
    const bridgeMessage = loadBridgeMessage(gw.port, logs);
    gw.setScript((runId) => [
      { runId, sessionKey: MAIN_KEY, seq: 1, state: "delta", message: textMsg("spawning helpers") },
      { runId, sessionKey: MAIN_KEY, seq: 2, state: "final", yielded: true, message: textMsg("spawning helpers") },
      { runId: "sub-run", sessionKey: SUB_KEY, seq: 1, state: "final", message: textMsg("SUB-AGENT REPORT") },
      null,
      { runId: "successor-run", sessionKey: MAIN_KEY, seq: 1, state: "final", message: textMsg("FINAL BRIEF") },
    ]);
    const text = await bridgeMessage("hello", 5000);
    assert.equal(text, "FINAL BRIEF");
  });
});
