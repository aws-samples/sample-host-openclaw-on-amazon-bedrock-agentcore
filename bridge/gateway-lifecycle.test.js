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
  probe = true, chat, config,
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
