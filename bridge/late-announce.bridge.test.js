/**
 * Issue #148: a sub-agent announce that finishes after the main run's final
 * reached nobody. gatewayTurn() resolves on the main final and closes its
 * socket, so the announce run's final only landed in the transcript.
 *
 * These tests run the shipped gatewayTurn(), watchChatEvents() and the late
 * announce relay against a fake protocol-v4 gateway that broadcasts every chat
 * event to every operator connection (as openclaw 2026.9.5 does) and serves
 * chat.history. Run: node --test late-announce.bridge.test.js
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
const ANNOUNCE_RUN = `announce:v1:${SUB_KEY}:child-run-1`;
const ROUTE = Object.freeze({
  actorId: "telegram:<telegram_id>", channel: "telegram",
  channelActorId: "telegram:<telegram_id>", channelTarget: "<telegram_id>",
});
const quiet = { log: () => {}, warn: () => {}, error: () => {} };
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const extractText = (content) => {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((block) => block?.text || "").join("");
  if (content && typeof content === "object" && Array.isArray(content.content)) return extractText(content.content);
  return "";
};
const assistant = (runId, text, extra = {}) => ({
  role: "assistant", content: [{ type: "text", text }], __openclaw: { runId }, ...extra,
});

/**
 * `script(runId)` returns the chat events to broadcast after the chat.send
 * ack; a number entry pauses for that many ms. `history` is the transcript
 * served by chat.history.
 */
function startGateway() {
  const wss = new WebSocket.Server({ host: "127.0.0.1", port: 0 });
  const sockets = new Set();
  let script = () => [];
  let history = [];
  const broadcast = (payload) => {
    for (const sock of sockets) {
      if (sock.readyState === WebSocket.OPEN) sock.send(JSON.stringify({ type: "event", event: "chat", payload }));
    }
  };
  wss.on("connection", (sock) => {
    sock.send(JSON.stringify({ type: "event", event: "connect.challenge", payload: { nonce: "n" } }));
    sock.on("close", () => sockets.delete(sock));
    sock.on("message", async (data) => {
      const req = JSON.parse(data.toString());
      const ok = (payload) => sock.send(JSON.stringify({ type: "res", id: req.id, ok: true, payload }));
      if (req.method === "connect") {
        sockets.add(sock);
        return ok({ type: "hello-ok", protocol: 4,
          features: { methods: ["chat.send", "chat.history", "last-heartbeat"] } });
      }
      if (req.method === "last-heartbeat") return ok(null);
      if (req.method === "chat.history") return ok({ sessionKey: req.params.sessionKey, messages: history });
      if (req.method === "chat.send") {
        const runId = req.params.idempotencyKey;
        history.push({ role: "user", content: "hello", idempotencyKey: runId });
        ok({ runId, status: "started" });
        for (const step of script(runId)) {
          if (typeof step === "number") {
            await sleep(step);
            continue;
          }
          broadcast(step);
        }
      }
    });
  });
  return new Promise((resolve, reject) => {
    wss.once("error", reject);
    wss.on("listening", () => resolve({
      url: `ws://127.0.0.1:${wss.address().port}`,
      setScript: (fn) => { script = fn; },
      setHistory: (messages) => { history = messages; },
      broadcast,
      connections: () => sockets.size,
      dropAll: () => { for (const sock of sockets) sock.terminate(); },
      close: () => new Promise((r) => {
        for (const sock of sockets) sock.terminate();
        wss.close(r);
      }),
    }));
  });
}

async function startRelay(gw, { settleMs = 20, retryMs = 20 } = {}) {
  const { watchChatEvents } = require("./gateway-lifecycle");
  const { createLateAnnounceRelay } = require("./late-announce");
  const controller = new AbortController();
  const delivered = [];
  let watcher;
  const relay = createLateAnnounceRelay({
    settleMs, log: quiet,
    fetchAnswer: (entry) => watcher.readRunAnswer({ ...entry, extractText }),
    deliver: async (text, route, meta) => { delivered.push({ text, route, runId: meta.runId }); },
  });
  watcher = watchChatEvents({
    url: gw.url, token: "test-token", signal: controller.signal, log: quiet, retryMs,
    onEvent: (payload) => relay.onChatEvent(payload),
  });
  await watcher.ready;
  return {
    relay, delivered,
    stop: async () => {
      relay.close();
      controller.abort();
      await watcher.stopped;
    },
  };
}

function runTurn(gw, relay, timeoutMs = 5000) {
  const { gatewayTurn } = require("./gateway-lifecycle");
  const turn = relay.beginTurn(ROUTE);
  return gatewayTurn({
    url: gw.url, token: "test-token", message: "hello", deadline: Date.now() + timeoutMs,
    extractText, log: quiet, onOwnRun: turn.claim,
  }).then((result) => ({ result, turn }));
}

async function waitFor(check, timeoutMs = 2000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    if (check()) return;
    await sleep(10);
  }
}

describe("late sub-agent announce delivery", { skip: !WebSocket && "ws not installed" }, () => {
  let gw;
  before(async () => { gw = await startGateway(); });
  after(async () => { if (gw) await gw.close(); });

  it("forwards an announce final that arrives after the main reply, once", async () => {
    const { relay, delivered, stop } = await startRelay(gw);
    try {
      gw.setHistory([]);
      gw.setScript((runId) => [
        { runId, sessionKey: MAIN_KEY, seq: 1, state: "delta", message: assistant(runId, "Working") },
        { runId, sessionKey: MAIN_KEY, seq: 2, state: "final", message: assistant(runId, "MAIN BRIEF") },
      ]);
      const { result, turn } = await runTurn(gw, relay);
      assert.equal(result.phase, "completed");
      assert.equal(result.response, "MAIN BRIEF");
      turn.end();

      // The sub-agent finishes later; openclaw runs the announce in the main session.
      gw.setHistory([
        { role: "user", content: "hello", idempotencyKey: result.runId },
        assistant(result.runId, "MAIN BRIEF"),
        assistant(ANNOUNCE_RUN, "Let me check the child results.", { phase: "commentary" }),
        assistant(ANNOUNCE_RUN, "SUB-AGENT SUMMARY"),
      ]);
      const buffer = { role: "assistant", content: [{ type: "text", text: "Let me check... SUB-AGENT SUMMARY" }] };
      gw.broadcast({ runId: ANNOUNCE_RUN, sessionKey: MAIN_KEY, seq: 1, state: "delta", message: buffer });
      gw.broadcast({ runId: ANNOUNCE_RUN, sessionKey: MAIN_KEY, seq: 2, state: "final", message: buffer });
      gw.broadcast({ runId: ANNOUNCE_RUN, sessionKey: MAIN_KEY, seq: 2, state: "final", message: buffer });
      await waitFor(() => delivered.length > 0);
      await relay.idle();
      await sleep(50);
      assert.deepEqual(delivered, [{ text: "SUB-AGENT SUMMARY", route: ROUTE, runId: ANNOUNCE_RUN }]);
    } finally {
      await stop();
    }
  });

  it("holds an announce final from while the main run was active until after the main reply", async () => {
    const { relay, delivered, stop } = await startRelay(gw);
    try {
      gw.setHistory([assistant(ANNOUNCE_RUN, "EARLY SUB-AGENT SUMMARY")]);
      gw.setScript((runId) => [
        { runId, sessionKey: MAIN_KEY, seq: 1, state: "delta", message: assistant(runId, "Working") },
        { runId: ANNOUNCE_RUN, sessionKey: MAIN_KEY, seq: 1, state: "final",
          message: assistant(ANNOUNCE_RUN, "EARLY SUB-AGENT SUMMARY") },
        60,
        { runId, sessionKey: MAIN_KEY, seq: 2, state: "final", message: assistant(runId, "MAIN BRIEF") },
      ]);
      const { result, turn } = await runTurn(gw, relay);
      assert.equal(result.response, "MAIN BRIEF", "the announce must not replace the main reply");
      assert.equal(delivered.length, 0, "the announce must not go out before the main reply");
      turn.end();
      await waitFor(() => delivered.length > 0);
      await relay.idle();
      assert.deepEqual(delivered.map((d) => d.text), ["EARLY SUB-AGENT SUMMARY"]);
    } finally {
      await stop();
    }
  });

  it("does not deliver an announce run the turn returned as its sessions_yield successor", async () => {
    const { relay, delivered, stop } = await startRelay(gw);
    try {
      gw.setHistory([assistant(ANNOUNCE_RUN, "FINAL BRIEF")]);
      gw.setScript((runId) => [
        { runId, sessionKey: MAIN_KEY, seq: 1, state: "final", yielded: true, message: assistant(runId, "spawning") },
        20,
        { runId: ANNOUNCE_RUN, sessionKey: MAIN_KEY, seq: 1, state: "final", message: assistant(ANNOUNCE_RUN, "FINAL BRIEF") },
      ]);
      const { result, turn } = await runTurn(gw, relay);
      assert.equal(result.response, "FINAL BRIEF");
      turn.end();
      await sleep(80);
      await relay.idle();
      assert.deepEqual(delivered, []);
    } finally {
      await stop();
    }
  });

  it("the watcher reconnects after the gateway drops its connection", async () => {
    const { relay, delivered, stop } = await startRelay(gw);
    try {
      relay.beginTurn(ROUTE).end();
      await waitFor(() => gw.connections() === 1);
      gw.dropAll();
      await waitFor(() => gw.connections() === 0);
      await waitFor(() => gw.connections() === 1);
      assert.equal(gw.connections(), 1, "watcher did not reconnect");
      gw.setHistory([assistant(ANNOUNCE_RUN, "AFTER RECONNECT")]);
      gw.broadcast({ runId: ANNOUNCE_RUN, sessionKey: MAIN_KEY, seq: 1, state: "final" });
      await waitFor(() => delivered.length > 0);
      await relay.idle();
      assert.deepEqual(delivered.map((d) => d.text), ["AFTER RECONNECT"]);
    } finally {
      await stop();
    }
  });
});

describe("contract wiring", () => {
  const src = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf-8");

  it("each turn claims its own runs so the relay never re-sends them", () => {
    assert.match(src, /lateAnnounces\?\.beginTurn\(/);
    assert.match(src, /onOwnRun:/);
  });

  it("starts the watcher only after the gateway is ready, scoped to the activation", () => {
    const ready = src.indexOf("await dismissStaleSuspendedDeliveries(");
    assert.notEqual(ready, -1);
    assert.notEqual(src.indexOf("startLateAnnounceRelay(", ready), -1);
    assert.match(src, /watchChatEvents\(\{[^}]*signal: activationScope\.signal/s);
  });

  it("delivers only to a Telegram route of the bound owner, through the streamer's send path", () => {
    const body = src.slice(src.indexOf("function deliverLateAnnounce("));
    const fn = body.slice(0, body.indexOf("\n}\n"));
    assert.match(fn, /route\.channel !== "telegram"/);
    assert.match(fn, /binding\.actorId !== route\.actorId/);
    assert.match(fn, /createTelegramStreamer\(route\.channelTarget\)\.finalize\(/);
  });

  it("an async cron turn holds late deliveries until its result has been collected", () => {
    assert.match(src, /cronTurnHolds/);
  });
});
