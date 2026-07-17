"use strict";

const { EventEmitter } = require("node:events");
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const gatewayInvocation = require("./gateway-invocation");

class FakeWebSocket extends EventEmitter {
  static OPEN = 1;
  static CLOSED = 3;
  static instances = [];
  static onSend = null;

  constructor(url) {
    super();
    this.url = url;
    this.readyState = FakeWebSocket.OPEN;
    this.sent = [];
    FakeWebSocket.instances.push(this);
  }

  send(raw) {
    if (this.readyState !== FakeWebSocket.OPEN) {
      throw new Error("socket is not open");
    }
    const frame = JSON.parse(raw);
    this.sent.push(frame);
    FakeWebSocket.onSend?.(this, frame);
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED;
  }

  serverFrame(frame) {
    this.emit("message", Buffer.from(JSON.stringify(frame)));
  }

  serverError(message = "transport failed") {
    this.emit("error", new Error(message));
  }

  serverClose(code = 1006, reason = "transport lost") {
    this.readyState = FakeWebSocket.CLOSED;
    this.emit("close", code, Buffer.from(reason));
  }
}

const silentLogger = {
  log() {},
  warn() {},
  error() {},
};

function resetFakeWebSocket() {
  FakeWebSocket.instances = [];
  FakeWebSocket.onSend = null;
}

function startRun(overrides = {}) {
  resetFakeWebSocket();
  const runPromise = gatewayInvocation.invokeGatewayAgent({
    WebSocketConstructor: FakeWebSocket,
    url: "ws://127.0.0.1:18789",
    token: "local-token",
    message: "do the work",
    runId: "11111111-1111-1111-1111-111111111111",
    timeoutMs: 1_000,
    abortTimeoutMs: 25,
    logger: silentLogger,
    quarantine: gatewayInvocation.createGatewayRunQuarantine(),
    ...overrides,
  });
  const socket = FakeWebSocket.instances[0];
  assert.ok(socket, "state machine must open a socket synchronously");
  return { runPromise, socket };
}

function authenticateAndDispatch(socket) {
  socket.serverFrame({
    type: "event",
    event: "connect.challenge",
    payload: { nonce: "proof-nonce" },
  });
  const connect = socket.sent.find((frame) => frame.method === "connect");
  assert.ok(connect, "connect request missing");
  socket.serverFrame({
    type: "res",
    id: connect.id,
    ok: true,
    payload: { auth: { scopes: ["operator.read", "operator.write"] } },
  });
  const agent = socket.sent.find((frame) => frame.method === "agent");
  assert.ok(agent, "agent request missing");
  return agent;
}

function authenticateAndAccept(socket) {
  const agent = authenticateAndDispatch(socket);
  socket.serverFrame({
    type: "res",
    id: agent.id,
    ok: true,
    payload: { runId: agent.id, status: "accepted" },
  });
  return agent;
}

async function captureError(promise) {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  assert.fail("expected invocation to fail");
}

describe("gateway agent production state machine", () => {
  it("persists uncertain-run quarantine and rejects later turns before opening a socket", async () => {
    const quarantine = gatewayInvocation.createGatewayRunQuarantine();
    const { runPromise, socket } = startRun({ quarantine });
    authenticateAndAccept(socket);
    socket.serverClose();
    const firstError = await captureError(runPromise);
    assert.equal(firstError.code, "UNCERTAIN_AGENT_RUN");
    const socketCount = FakeWebSocket.instances.length;

    const secondError = await captureError(
      gatewayInvocation.invokeGatewayAgent({
        WebSocketConstructor: FakeWebSocket,
        url: "ws://127.0.0.1:18789",
        token: "local-token",
        message: "must not start",
        runId: "22222222-2222-2222-2222-222222222222",
        quarantine,
        logger: silentLogger,
      }),
    );

    assert.equal(secondError.code, "AGENT_RUNTIME_QUARANTINED");
    assert.equal(secondError.runId, firstError.runId);
    assert.equal(secondError.safeToFallback, false);
    assert.equal(FakeWebSocket.instances.length, socketCount);
  });

  it("terminates the gateway and rejects the next turn after accepted-close uncertainty", async () => {
    const kills = [];
    const scheduled = [];
    const child = {
      exitCode: null,
      signalCode: null,
      kill(signal) {
        kills.push(signal);
        return true;
      },
    };
    const boundary = gatewayInvocation.createGatewayRuntimeBoundary({
      getGatewayProcess: () => child,
      terminateGraceMs: 10,
      schedule(fn) {
        scheduled.push(fn);
        return { unref() {} };
      },
    });
    const { runPromise, socket } = startRun();
    authenticateAndAccept(socket);
    const fallbackCalls = [];
    const firstTurn = boundary.invoke({
      invokePrimary: () => runPromise,
      invokeFallback: async () => {
        fallbackCalls.push("first");
        return "must not run";
      },
    });
    socket.serverClose();

    const firstError = await captureError(firstTurn);
    assert.equal(firstError.code, "UNCERTAIN_AGENT_RUN");
    assert.deepEqual(kills, ["SIGTERM"]);
    assert.deepEqual(fallbackCalls, []);
    assert.equal(boundary.getQuarantine().runId, firstError.runId);

    const primaryCalls = [];
    const secondError = await captureError(
      boundary.invoke({
        invokePrimary: async () => {
          primaryCalls.push("second");
          return { runId: "new", status: "ok", text: "must not run" };
        },
        invokeFallback: async () => {
          fallbackCalls.push("second");
          return "must not run";
        },
      }),
    );
    assert.equal(secondError.code, "AGENT_RUNTIME_QUARANTINED");
    assert.deepEqual(primaryCalls, []);
    assert.deepEqual(fallbackCalls, []);

    assert.equal(scheduled.length, 1);
    scheduled[0]();
    assert.deepEqual(kills, ["SIGTERM", "SIGKILL"]);
  });

  it("quarantines a disconnect after dispatch even when acceptance was not observed", async () => {
    const { runPromise, socket } = startRun();
    const agent = authenticateAndDispatch(socket);
    socket.serverClose();

    const error = await captureError(runPromise);
    assert.equal(error.code, "UNCERTAIN_AGENT_RUN");
    assert.equal(error.runId, agent.id);
    assert.equal(error.accepted, true);
    assert.equal(error.safeToFallback, false);
  });

  it("quarantines accepted delta then close without returning partial output", async () => {
    const deltas = [];
    const { runPromise, socket } = startRun({
      onDelta: (text) => deltas.push(text),
    });
    const agent = authenticateAndAccept(socket);
    socket.serverFrame({
      type: "event",
      event: "chat",
      payload: {
        runId: agent.id,
        state: "delta",
        message: { content: "uncommitted partial" },
      },
    });
    socket.serverClose();

    const error = await captureError(runPromise);
    assert.equal(error.code, "UNCERTAIN_AGENT_RUN");
    assert.equal(error.runId, agent.id);
    assert.equal(error.accepted, true);
    assert.equal(error.safeToFallback, false);
    assert.deepEqual(deltas, ["uncommitted partial"]);
    assert.doesNotMatch(error.message, /uncommitted partial/);
  });

  it("quarantines accepted delta then transport error when abort cannot be confirmed", async () => {
    const { runPromise, socket } = startRun({ abortTimeoutMs: 5 });
    const agent = authenticateAndAccept(socket);
    socket.serverFrame({
      type: "event",
      event: "chat",
      payload: {
        runId: agent.id,
        state: "delta",
        message: { content: "never committed" },
      },
    });
    socket.serverError();

    const error = await captureError(runPromise);
    const abort = socket.sent.find((frame) => frame.method === "chat.abort");
    assert.deepEqual(abort?.params, {
      sessionKey: "global",
      runId: agent.id,
    });
    assert.equal(error.code, "UNCERTAIN_AGENT_RUN");
    assert.equal(error.safeToFallback, false);
  });

  it("aborts the exact accepted run on timeout and never promotes its delta", async () => {
    const events = [];
    FakeWebSocket.onSend = null;
    const { runPromise, socket } = startRun({
      timeoutMs: 5,
      abortTimeoutMs: 50,
      onDelta: (text) => events.push(`delta:${text}`),
    });
    const agent = authenticateAndAccept(socket);
    FakeWebSocket.onSend = (activeSocket, frame) => {
      if (frame.method === "chat.abort") {
        events.push(`abort:${frame.params.runId}`);
        queueMicrotask(() => {
          activeSocket.serverFrame({
            type: "res",
            id: frame.id,
            ok: true,
            payload: { ok: true, aborted: true, runIds: [agent.id] },
          });
        });
      }
      if (frame.method === "agent.wait") {
        events.push(`wait:${frame.params.runId}`);
        queueMicrotask(() => {
          activeSocket.serverFrame({
            type: "res",
            id: frame.id,
            ok: true,
            payload: {
              runId: agent.id,
              status: "error",
              stopReason: "rpc",
              endedAt: 200,
            },
          });
        });
      }
    };
    socket.serverFrame({
      type: "event",
      event: "chat",
      payload: {
        runId: agent.id,
        state: "delta",
        message: { content: "uncommitted timeout delta" },
      },
    });

    const error = await captureError(runPromise);
    assert.equal(error.code, "AGENT_RUN_ABORTED");
    assert.equal(error.runId, agent.id);
    assert.equal(error.accepted, true);
    assert.equal(error.safeToFallback, false);
    assert.deepEqual(events, [
      "delta:uncommitted timeout delta",
      `abort:${agent.id}`,
      `wait:${agent.id}`,
    ]);
    assert.doesNotMatch(error.message, /uncommitted timeout delta/);
  });

  it("quarantines when abort is acknowledged but terminal settlement is not proven", async () => {
    const quarantine = gatewayInvocation.createGatewayRunQuarantine();
    const { runPromise, socket } = startRun({
      timeoutMs: 5,
      abortTimeoutMs: 5,
      quarantine,
    });
    const agent = authenticateAndAccept(socket);
    FakeWebSocket.onSend = (activeSocket, frame) => {
      if (frame.method !== "chat.abort") return;
      queueMicrotask(() => {
        activeSocket.serverFrame({
          type: "res",
          id: frame.id,
          ok: true,
          payload: { ok: true, aborted: true, runIds: [agent.id] },
        });
      });
    };

    const error = await captureError(runPromise);
    assert.equal(error.code, "UNCERTAIN_AGENT_RUN");
    assert.equal(error.safeToFallback, false);
    assert.equal(quarantine.runId, agent.id);
    assert.ok(socket.sent.some((frame) => frame.method === "agent.wait"));
  });

  it("turns terminal error and timeout into distinct typed failures", async () => {
    for (const [status, code] of [
      ["error", "AGENT_TERMINAL_ERROR"],
      ["timeout", "AGENT_TERMINAL_TIMEOUT"],
    ]) {
      const { runPromise, socket } = startRun();
      const agent = authenticateAndAccept(socket);
      socket.serverFrame({
        type: "res",
        id: agent.id,
        ok: true,
        payload: { runId: agent.id, status, error: `${status} detail` },
      });

      const error = await captureError(runPromise);
      assert.equal(error.code, code);
      assert.equal(error.terminalStatus, status);
      assert.equal(error.accepted, true);
      assert.equal(error.safeToFallback, false);
    }
  });

  it("ignores unrelated frames and returns only same-id terminal ok output", async () => {
    const deltas = [];
    const { runPromise, socket } = startRun({
      onDelta: (text) => deltas.push(text),
    });
    const agent = authenticateAndAccept(socket);
    socket.serverFrame({
      type: "event",
      event: "chat",
      payload: {
        runId: "other-run",
        state: "delta",
        message: { content: "other partial" },
      },
    });
    socket.serverFrame({
      type: "res",
      id: "other-run",
      ok: true,
      payload: { status: "ok", result: { payloads: [{ text: "other" }] } },
    });
    socket.serverFrame({
      type: "event",
      event: "chat",
      payload: {
        runId: agent.id,
        state: "delta",
        message: { content: "visible but uncommitted" },
      },
    });
    socket.serverFrame({
      type: "res",
      id: agent.id,
      ok: true,
      payload: {
        runId: agent.id,
        status: "ok",
        result: { payloads: [{ text: "committed answer" }] },
      },
    });

    assert.deepEqual(await runPromise, {
      runId: agent.id,
      status: "ok",
      text: "committed answer",
    });
    assert.deepEqual(deltas, ["visible but uncommitted"]);
  });

  it("keeps request, run, and idempotency identity stable for duplicate calls", async () => {
    const runId = "33333333-3333-3333-3333-333333333333";
    const invokeOnce = async (text) => {
      const { runPromise, socket } = startRun({ runId });
      const agent = authenticateAndAccept(socket);
      assert.equal(agent.id, runId);
      assert.equal(agent.params.idempotencyKey, runId);
      socket.serverFrame({
        type: "res",
        id: runId,
        ok: true,
        payload: {
          runId,
          status: "ok",
          result: { payloads: [{ text }] },
        },
      });
      return runPromise;
    };

    assert.equal((await invokeOnce("first")).text, "first");
    assert.equal((await invokeOnce("cached")).text, "cached");
  });

  it("checks typed safety only after exact-run abort/reconciliation before fallback", async () => {
    const order = [];
    const { runPromise, socket } = startRun({
      timeoutMs: 5,
      abortTimeoutMs: 50,
    });
    const agent = authenticateAndAccept(socket);
    FakeWebSocket.onSend = (activeSocket, frame) => {
      if (frame.method === "chat.abort") {
        order.push(`abort:${frame.params.runId}`);
        queueMicrotask(() => {
          activeSocket.serverFrame({
            type: "res",
            id: frame.id,
            ok: true,
            payload: { ok: true, aborted: true, runIds: [agent.id] },
          });
        });
      }
      if (frame.method === "agent.wait") {
        order.push(`wait:${frame.params.runId}`);
        queueMicrotask(() => {
          activeSocket.serverFrame({
            type: "res",
            id: frame.id,
            ok: true,
            payload: {
              runId: agent.id,
              status: "error",
              stopReason: "rpc",
              endedAt: 200,
            },
          });
        });
      }
    };

    const resultPromise = gatewayInvocation.invokeWithSafeFallback({
      invokePrimary: () => runPromise,
      invokeFallback: async () => {
        order.push("fallback");
        return "fallback answer";
      },
    });
    const error = await captureError(resultPromise);

    assert.equal(error.code, "AGENT_RUN_ABORTED");
    assert.deepEqual(order, [`abort:${agent.id}`, `wait:${agent.id}`]);
  });

  it("hard-caps reconciliation below OpenClaw's synthetic 15-second snapshot window", async () => {
    const { runPromise, socket } = startRun({
      timeoutMs: 5,
      abortTimeoutMs: 20_000,
    });
    const agent = authenticateAndAccept(socket);
    let waitTimeoutMs = null;
    FakeWebSocket.onSend = (activeSocket, frame) => {
      if (frame.method === "chat.abort") {
        queueMicrotask(() => {
          activeSocket.serverFrame({
            type: "res",
            id: frame.id,
            ok: true,
            payload: { ok: true, aborted: true, runIds: [agent.id] },
          });
        });
      }
      if (frame.method === "agent.wait") {
        waitTimeoutMs = frame.params.timeoutMs;
        queueMicrotask(() => {
          activeSocket.serverFrame({
            type: "res",
            id: frame.id,
            ok: true,
            payload: {
              runId: agent.id,
              status: "error",
              stopReason: "rpc",
              endedAt: 200,
            },
          });
        });
      }
    };

    const error = await captureError(runPromise);
    assert.equal(error.code, "AGENT_RUN_ABORTED");
    assert.ok(waitTimeoutMs > 0);
    assert.ok(waitTimeoutMs < 15_000);
  });

  it("allows fallback only when transport failed before any run was accepted", async () => {
    const order = [];
    const { runPromise, socket } = startRun();
    socket.serverClose();

    const result = await gatewayInvocation.invokeWithSafeFallback({
      invokePrimary: () => runPromise,
      invokeFallback: async () => {
        order.push("fallback");
        return "fallback answer";
      },
    });

    assert.deepEqual(result, {
      runId: null,
      status: "ok",
      text: "fallback answer",
      source: "fallback",
    });
    assert.deepEqual(order, ["fallback"]);
  });
});
