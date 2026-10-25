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
        // The sub-agent emits an event during the main turn; this is what
        // ties its sessionKey to the originating turn's route so a late
        // announce for it is delivered to this conversation, not a later one.
        { runId: "sub-run-1", sessionKey: SUB_KEY, seq: 1, state: "delta",
          message: assistant("sub-run-1", "spawning sub-agent"), spawnedBy: MAIN_KEY },
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
        // Sub-agent event during the main turn captures the originating route.
        { runId: "sub-run-1", sessionKey: SUB_KEY, seq: 1, state: "delta",
          message: assistant("sub-run-1", "spawning sub-agent"), spawnedBy: MAIN_KEY },
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
      const turn = relay.beginTurn(ROUTE);
      // Observe a sub-agent event under this active turn so the relay captures
      // its originating route; the announce arriving later maps back to it.
      relay.onChatEvent({ runId: "sub-run-reconnect", sessionKey: SUB_KEY, seq: 1,
        state: "delta", spawnedBy: MAIN_KEY,
        message: { role: "assistant", content: [{ type: "text", text: "spawning" }] } });
      turn.end();
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
  const has = (needle, message) => assert.ok(src.includes(needle), message || `missing ${needle}`);
  const functionBody = (signature) => {
    const start = src.indexOf(signature);
    assert.notEqual(start, -1, `missing ${signature}`);
    return src.slice(start, src.indexOf("\n}\n", start));
  };

  it("each turn claims its own runs so the relay never re-sends them", () => {
    const handler = functionBody("async function handleV2Invocation(");
    assert.ok(handler.includes("lateAnnounces?.beginTurn("), "turn is not registered with the relay");
    assert.ok(handler.includes("onOwnRun: (runId) => lateTurn?.claim(runId)"), "turn does not claim its runs");
    assert.ok(handler.includes("lateTurn?.end()"), "turn never ends");
  });

  it("starts the watcher only after the gateway is ready, scoped to the activation", () => {
    const ready = src.indexOf("await dismissStaleSuspendedDeliveries(");
    assert.notEqual(ready, -1);
    assert.notEqual(src.indexOf("startLateAnnounceRelay();", ready), -1, "relay not started after readiness");
    const start = functionBody("function startLateAnnounceRelay(");
    assert.ok(start.includes("signal: activationScope.signal"), "watcher is not bound to the activation");
    assert.ok(start.includes("ws://127.0.0.1:"), "watcher must use the loopback gateway");
    has("lateAnnounces?.close();", "relay is not closed on cleanup");
  });

  it("delivers only to a Telegram route of the bound owner, through the streamer's send path", () => {
    const fn = functionBody("async function deliverLateAnnounce(");
    assert.ok(fn.includes('route.channel !== "telegram"'), "non-Telegram routes are not refused");
    assert.ok(fn.includes("knownDeliveryRoutes.has(tripleKey)"),
      "delivery triple is not checked against routes owned by the current binding");
    assert.ok(fn.includes("binding.actorId !== route.actorId"), "route is not checked against the binding");
    assert.ok(fn.includes("createTelegramStreamer(route.channelTarget).finalize("), "not the streamer send path");
  });

  it("an async cron turn holds late deliveries until its result has been collected", () => {
    const cron = functionBody("async function handleCronAsync(");
    assert.ok(cron.includes("deferTurnEnd: (handle) => holdCronTurn(jobId, handle)"), "cron turn is not held");
    assert.ok(cron.includes('if (status.status === "done") releaseCronTurn(payload.jobId);'), "hold never released");
  });

  it("the image ships late-announce.js", () => {
    const dockerfile = fs.readFileSync(path.join(__dirname, "Dockerfile"), "utf-8");
    assert.ok(dockerfile.includes("COPY late-announce.js /app/late-announce.js"));
  });
});

describe("async cron hold", () => {
  const vm = require("vm");
  const jobs = require("./cron-jobs");
  const src = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf-8");
  const start = src.indexOf("function cronJobStore() {");
  const end = src.indexOf("\n// Shape check for the Router", start);

  function cronBox() {
    const timers = [];
    const ended = [];
    const box = {
      fs: { existsSync: () => false }, stateStorage: { DEFAULT_MOUNT: "/mnt/workspace" },
      cronJobs: jobs, osRandomUUID: () => "boot-1", CRON_JOB_BUDGET_MS: 45 * 60 * 1000,
      cronJobStoreInstance: null, console: quiet, Date, Object, Number,
      v2Lifecycle: { assertBinding() {}, checkpoint() {} },
      setTimeout: (callback, delay) => {
        const timer = { callback, delay, cleared: false, unref() {} };
        timers.push(timer);
        return timer;
      },
      clearTimeout: (timer) => { if (timer) timer.cleared = true; },
      handleV2Invocation(payload, req, res, callerGone, arrival, options) {
        options.deferTurnEnd({ end: () => ended.push(payload.jobId ?? "turn"), claim() {} });
        res.writeHead(200);
        res.end(JSON.stringify({ response: "BRIEF", execution: { phase: "completed" } }));
        return Promise.resolve();
      },
    };
    vm.createContext(box);
    vm.runInContext(src.slice(start, end), box);
    const call = async (payload) => {
      let reply;
      await box.handleCronAsync(payload, { headers: {} }, {
        writeHead(code) { this.code = code; },
        end(body) { reply = JSON.parse(body); },
      });
      return reply;
    };
    const fire = (delay) => {
      for (const timer of timers.filter((t) => t.delay === delay && !t.cleared)) {
        timer.cleared = true;
        timer.callback();
      }
    };
    return { call, ended, timers, fire };
  }

  const owner = { userId: "u1", actorId: "telegram:<telegram_id>", channel: "telegram", channelTarget: "<telegram_id>" };

  it("keeps the turn active until cron_status hands the brief to the Lambda, then a grace period", async () => {
    const box = cronBox();
    await box.call({ ...owner, action: "cron_start", jobId: "job-1", message: "brief" });
    await sleep(0);
    assert.deepEqual(box.ended, [], "turn released before the Lambda collected the brief");
    const status = await box.call({ ...owner, action: "cron_status", jobId: "job-1" });
    assert.equal(status.status, "done");
    assert.deepEqual(box.ended, [], "released without a grace period for the Lambda's send");
    box.fire(15000);
    assert.equal(box.ended.length, 1);
    await box.call({ ...owner, action: "cron_status", jobId: "job-1" });
    box.fire(15000);
    assert.equal(box.ended.length, 1, "a repeated status poll must not end the turn twice");
  });

  it("releases the hold after its maximum if the Lambda never collects the brief", async () => {
    const box = cronBox();
    await box.call({ ...owner, action: "cron_start", jobId: "job-2", message: "brief" });
    await sleep(0);
    assert.deepEqual(box.ended, []);
    box.fire(5 * 60 * 1000);
    assert.equal(box.ended.length, 1);
  });
});
