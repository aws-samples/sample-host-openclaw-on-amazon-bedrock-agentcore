/**
 * Local test of the contract's gatewayTurn() against a fake
 * OpenClaw gateway speaking protocol v4 over WebSocket.
 * Run: node --test chat-run-filter.bridge.test.js
 *
 * Text extraction comes from agentcore-contract.js. The transport is the
 * shipped gateway-lifecycle module. Skips when `ws` is not installed.
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

function loadGatewayTurn(port) {
  const src = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf-8");
  const extractText = new Function(
    sliceFunction(src, "function extractTextFromContent(") +
    "\nreturn extractTextFromContent;",
  )();
  const { gatewayTurn } = require("./gateway-lifecycle");
  return (message, timeoutMs, onDelta) => gatewayTurn({
    url: `ws://127.0.0.1:${port}`, token: "test-token", message,
    deadline: Date.now() + timeoutMs, onDelta, extractText,
  });
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
        sock.send(JSON.stringify({ type: "res", id: req.id, ok: true, payload: {
          type: "hello-ok", protocol: 4, features: { methods: ["chat.send", "last-heartbeat"] },
        } }));
        return;
      }
      if (req.method === "last-heartbeat") {
        sock.send(JSON.stringify({ type: "res", id: req.id, ok: true, payload: null }));
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
  return new Promise((resolve, reject) => {
    wss.once("error", reject);
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

describe("gatewayTurn against a fake gateway", { skip: !WebSocket && "ws not installed" }, () => {
  let gw;
  before(async () => {
    gw = await startGateway();
  });
  after(async () => {
    if (gw) await gw.close();
  });

  it("does not resolve with a sub-agent final that arrives before the main final", async () => {
    const gatewayTurn = loadGatewayTurn(gw.port);
    gw.setScript((runId) => [
      { runId, sessionKey: MAIN_KEY, seq: 1, state: "delta", message: textMsg("Working on the brief") },
      { runId: "sub-run", sessionKey: SUB_KEY, spawnedBy: MAIN_KEY, seq: 1, state: "delta", message: textMsg("sub partial") },
      { runId: "sub-run", sessionKey: SUB_KEY, spawnedBy: MAIN_KEY, seq: 2, state: "final", message: textMsg("SUB-AGENT REPORT") },
      null,
      { runId, sessionKey: MAIN_KEY, seq: 2, state: "final", message: textMsg("MAIN BRIEF") },
    ]);
    const deltas = [];
    const result = await gatewayTurn("hello", 5000, (t) => deltas.push(t));
    assert.equal(result.response, "MAIN BRIEF");
    assert.equal(result.phase, "completed");
    assert.ok(!deltas.includes("sub partial"), "sub-agent delta must not stream");
  });

  it("sub-agent deltas do not replace responseText when the main run times out", async () => {
    const gatewayTurn = loadGatewayTurn(gw.port);
    gw.setScript((runId) => [
      { runId, sessionKey: MAIN_KEY, seq: 1, state: "delta", message: textMsg("main partial") },
      { runId: "sub-run", sessionKey: SUB_KEY, seq: 1, state: "delta", message: textMsg("sub partial") },
    ]);
    const result = await gatewayTurn("hello", 300);
    assert.equal(result.response, "main partial");
    assert.equal(result.phase, "unknown_outcome");
  });

  it("an empty final from the main run still resolves with an empty string", async () => {
    const gatewayTurn = loadGatewayTurn(gw.port);
    gw.setScript((runId) => [{ runId, sessionKey: MAIN_KEY, seq: 1, state: "final" }]);
    const result = await gatewayTurn("hello", 5000);
    assert.equal(result.response, "");
    assert.equal(result.phase, "completed");
  });

  it("a sub-agent error does not end the request; the main final does", async () => {
    const gatewayTurn = loadGatewayTurn(gw.port);
    gw.setScript((runId) => [
      { runId: "sub-run", sessionKey: SUB_KEY, seq: 1, state: "error", errorMessage: "sub failed" },
      { runId: "sub-run", sessionKey: SUB_KEY, seq: 2, state: "aborted" },
      null,
      { runId, sessionKey: MAIN_KEY, seq: 1, state: "final", message: textMsg("MAIN OK") },
    ]);
    const result = await gatewayTurn("hello", 5000);
    assert.equal(result.response, "MAIN OK");
    assert.equal(result.phase, "completed");
  });

  it("follows a sessions_yield handoff to the successor run", async () => {
    const gatewayTurn = loadGatewayTurn(gw.port);
    gw.setScript((runId) => [
      { runId, sessionKey: MAIN_KEY, seq: 1, state: "delta", message: textMsg("spawning helpers") },
      { runId, sessionKey: MAIN_KEY, seq: 2, state: "final", yielded: true, message: textMsg("spawning helpers") },
      { runId: "sub-run", sessionKey: SUB_KEY, seq: 1, state: "final", message: textMsg("SUB-AGENT REPORT") },
      null,
      { runId: "successor-run", sessionKey: MAIN_KEY, seq: 1, state: "final", message: textMsg("FINAL BRIEF") },
    ]);
    const result = await gatewayTurn("hello", 5000);
    assert.equal(result.response, "FINAL BRIEF");
    assert.equal(result.phase, "completed");
  });
});
