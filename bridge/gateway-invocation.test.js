"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { GATEWAY_CLIENT_SCOPES } = require("./runtime-policy");

let gatewayInvocation = null;
try {
  gatewayInvocation = require("./gateway-invocation");
} catch {
  // RED: this production boundary is introduced after the contract fails.
}

describe("gateway invocation boundary", () => {
  it("exports the production gateway invocation helpers", () => {
    assert.ok(gatewayInvocation, "gateway-invocation.js must exist");
  });

  it("opens the CLI WebSocket without an Origin header", () => {
    const constructorCalls = [];
    class CapturingWebSocket {
      constructor(...args) {
        constructorCalls.push(args);
      }
    }

    const socket = gatewayInvocation.createGatewaySocket(
      CapturingWebSocket,
      "ws://127.0.0.1:18789",
    );

    assert.ok(socket instanceof CapturingWebSocket);
    assert.deepEqual(constructorCalls, [["ws://127.0.0.1:18789"]]);
  });

  it("requests only the frozen operator scopes as a CLI client", () => {
    assert.deepEqual(
      gatewayInvocation.buildGatewayConnectRequest({
        id: "connect-1",
        token: "local-token",
        version: "test-proof",
      }),
      {
        type: "req",
        id: "connect-1",
        method: "connect",
        params: {
          minProtocol: 4,
          maxProtocol: 4,
          client: {
            id: "cli",
            mode: "cli",
            version: "test-proof",
            platform: "linux",
          },
          caps: [],
          auth: { token: "local-token" },
          role: "operator",
          scopes: GATEWAY_CLIENT_SCOPES,
        },
      },
    );
  });

  it("derives granted scopes from hello and rejects missing or expanded authority", () => {
    const helloPayload = {
      auth: { scopes: ["operator.write", "operator.read"] },
    };
    assert.deepEqual(
      gatewayInvocation.assertGrantedGatewayScopes(helloPayload),
      helloPayload.auth.scopes,
    );

    assert.throws(
      () => gatewayInvocation.assertGrantedGatewayScopes({ auth: { scopes: [] } }),
      /granted gateway scopes/i,
    );
    assert.throws(
      () =>
        gatewayInvocation.assertGrantedGatewayScopes({
          auth: {
            scopes: ["operator.read", "operator.write", "operator.admin"],
          },
        }),
      /granted gateway scopes/i,
    );
    assert.throws(
      () => gatewayInvocation.assertGrantedGatewayScopes({}),
      /granted gateway scopes/i,
    );
  });

  it("builds an idempotent agent request with stable session continuity", () => {
    assert.deepEqual(
      gatewayInvocation.buildAgentRequest({
        id: "agent-1",
        message: "/status",
        sessionKey: "attacker-controlled",
      }),
      {
        type: "req",
        id: "agent-1",
        method: "agent",
        params: {
          sessionKey: "global",
          message: "/status",
          deliver: false,
          idempotencyKey: "agent-1",
        },
      },
    );
  });

  it("derives a stable server run identity from the trusted channel invocation", () => {
    const input = {
      invocationId: `po1_${"a".repeat(64)}`,
    };
    const first = gatewayInvocation.deriveGatewayRunId(input);
    const retry = gatewayInvocation.deriveGatewayRunId(input);
    const next = gatewayInvocation.deriveGatewayRunId({
      invocationId: `po1_${"b".repeat(64)}`,
    });

    assert.equal(first, retry);
    assert.notEqual(first, next);
    assert.match(first, /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
    assert.throws(
      () => gatewayInvocation.deriveGatewayRunId({ ...input, invocationId: "" }),
      /invocation identity/i,
    );
  });

  it("single-flights one canonical trusted invocation across executor changes", async () => {
    const registry = gatewayInvocation.createTrustedInvocationRegistry({
      ttlMs: 60_000,
      maxSettledEntries: 10,
    });
    const invocationId = `po1_${"c".repeat(64)}`;
    const requestHash = gatewayInvocation.hashTrustedInvocationRequest({
      userId: "user-1",
      actorId: "actor-1",
      channel: "slack",
      message: "do the work",
    });
    let releaseFallback;
    let fallbackCalls = 0;
    let gatewayCalls = 0;

    const first = registry.invoke({
      invocationId,
      requestHash,
      execute: async () => {
        fallbackCalls += 1;
        return new Promise((resolve) => {
          releaseFallback = () =>
            resolve({ status: "ok", response: "fallback result" });
        });
      },
    });
    const retryAfterReadinessChange = registry.invoke({
      invocationId,
      requestHash,
      execute: async () => {
        gatewayCalls += 1;
        return { status: "ok", response: "gateway result" };
      },
    });

    assert.equal(fallbackCalls, 1);
    assert.equal(gatewayCalls, 0);
    releaseFallback();
    assert.deepEqual(await first, {
      status: "ok",
      response: "fallback result",
    });
    assert.deepEqual(await retryAfterReadinessChange, {
      status: "ok",
      response: "fallback result",
    });
    assert.equal(fallbackCalls, 1);
    assert.equal(gatewayCalls, 0);
  });

  it("caps new in-flight IDs while allowing an existing duplicate to coalesce", async () => {
    const registry = gatewayInvocation.createTrustedInvocationRegistry({
      ttlMs: 60_000,
      maxSettledEntries: 10,
      maxInFlightEntries: 2,
    });
    const releases = [];
    let executions = 0;
    const start = (digit) =>
      registry.invoke({
        invocationId: `po1_${digit.repeat(64)}`,
        requestHash: digit.repeat(64),
        execute: async () => {
          executions += 1;
          return new Promise((resolve) => releases.push(resolve));
        },
      });

    const first = start("1");
    const second = start("2");
    const firstDuplicate = registry.invoke({
      invocationId: `po1_${"1".repeat(64)}`,
      requestHash: "1".repeat(64),
      execute: async () => {
        executions += 1;
        return { status: "wrong-executor" };
      },
    });

    await assert.rejects(
      registry.invoke({
        invocationId: `po1_${"3".repeat(64)}`,
        requestHash: "3".repeat(64),
        execute: async () => {
          executions += 1;
          return { status: "over-admitted" };
        },
      }),
      (error) =>
        error instanceof gatewayInvocation.GatewayInvocationError &&
        error.code === "RUNTIME_OVERLOADED" &&
        error.safeToFallback === false,
    );
    assert.equal(executions, 2);
    assert.deepEqual(registry.getStats(), {
      total: 2,
      inFlight: 2,
      pinned: 0,
      settledEvictable: 0,
    });

    releases[0]({ status: "ok", response: "first" });
    releases[1]({ status: "ok", response: "second" });
    assert.deepEqual(await first, { status: "ok", response: "first" });
    assert.deepEqual(await firstDuplicate, {
      status: "ok",
      response: "first",
    });
    assert.deepEqual(await second, { status: "ok", response: "second" });
  });

  it("bounds pending serialized gateway work and fails excess work closed", async () => {
    const executor = gatewayInvocation.createBoundedSerialExecutor({
      maxPending: 2,
    });
    const order = [];
    let releaseFirst;
    const first = executor.submit(
      () =>
        new Promise((resolve) => {
          order.push("first:start");
          releaseFirst = () => {
            order.push("first:end");
            resolve("first");
          };
        }),
    );
    const second = executor.submit(async () => {
      order.push("second");
      return "second";
    });
    const third = executor.submit(async () => {
      order.push("third");
      return "third";
    });

    await assert.rejects(
      executor.submit(async () => {
        order.push("excess");
      }),
      (error) =>
        error instanceof gatewayInvocation.GatewayInvocationError &&
        error.code === "RUNTIME_OVERLOADED" &&
        error.safeToFallback === false,
    );
    assert.deepEqual(executor.getStats(), { running: true, pending: 2 });
    assert.deepEqual(order, ["first:start"]);

    releaseFirst();
    assert.equal(await first, "first");
    assert.equal(await second, "second");
    assert.equal(await third, "third");
    assert.deepEqual(order, ["first:start", "first:end", "second", "third"]);
    assert.deepEqual(executor.getStats(), { running: false, pending: 0 });
  });

  it("binds a trusted invocation ID to the canonical request hash", async () => {
    const registry = gatewayInvocation.createTrustedInvocationRegistry();
    const invocationId = `po1_${"d".repeat(64)}`;
    let executorCalls = 0;

    await registry.invoke({
      invocationId,
      requestHash: "1".repeat(64),
      execute: async () => {
        executorCalls += 1;
        return { status: "failed", errorCode: "TEST_FAILURE" };
      },
    });

    await assert.rejects(
      registry.invoke({
        invocationId,
        requestHash: "2".repeat(64),
        execute: async () => {
          executorCalls += 1;
          return { status: "ok" };
        },
      }),
      (error) =>
        error instanceof gatewayInvocation.GatewayInvocationError &&
        error.code === "INVOCATION_ID_CONFLICT" &&
        error.safeToFallback === false,
    );
    assert.equal(executorCalls, 1);
  });

  it("canonicalizes structured image work and rejects a changed image under the same ID", async () => {
    const registry = gatewayInvocation.createTrustedInvocationRegistry();
    const invocationId = `po1_${"8".repeat(64)}`;
    const firstMessage = {
      text: "inspect this",
      images: [
        { s3Key: "users/u-1/image-a.png", contentType: "image/png" },
      ],
    };
    const sameMessage = {
      images: [
        { contentType: "image/png", s3Key: "users/u-1/image-a.png" },
      ],
      text: "inspect this",
    };
    const changedMessage = {
      text: "inspect this",
      images: [
        { s3Key: "users/u-1/image-b.png", contentType: "image/png" },
      ],
    };
    const hash = (message) =>
      gatewayInvocation.hashTrustedInvocationRequest({
        userId: "user-1",
        actorId: "actor-1",
        channel: "telegram",
        message,
      });
    let calls = 0;

    assert.equal(hash(firstMessage), hash(sameMessage));
    await registry.invoke({
      invocationId,
      requestHash: hash(firstMessage),
      execute: async () => {
        calls += 1;
        return { status: "ok", response: "image result" };
      },
    });
    assert.deepEqual(
      await registry.invoke({
        invocationId,
        requestHash: hash(sameMessage),
        execute: async () => {
          calls += 1;
          return { status: "ok", response: "wrong" };
        },
      }),
      { status: "ok", response: "image result" },
    );
    await assert.rejects(
      registry.invoke({
        invocationId,
        requestHash: hash(changedMessage),
        execute: async () => {
          calls += 1;
          return { status: "ok" };
        },
      }),
      (error) => error.code === "INVOCATION_ID_CONFLICT",
    );
    assert.equal(calls, 1);
  });

  it("never evicts in-flight or originating uncertain outcomes from the runtime registry", async () => {
    let now = 1_000;
    const registry = gatewayInvocation.createTrustedInvocationRegistry({
      ttlMs: 10,
      maxSettledEntries: 1,
      now: () => now,
    });
    const uncertainId = `po1_${"e".repeat(64)}`;
    const inFlightId = `po1_${"f".repeat(64)}`;
    let uncertainCalls = 0;
    let inFlightCalls = 0;
    let releaseInFlight;

    const uncertain = await registry.invoke({
      invocationId: uncertainId,
      requestHash: "3".repeat(64),
      execute: async () => {
        uncertainCalls += 1;
        return {
          status: "uncertain",
          errorCode: "UNCERTAIN_AGENT_RUN",
        };
      },
    });
    const inFlight = registry.invoke({
      invocationId: inFlightId,
      requestHash: "4".repeat(64),
      execute: async () => {
        inFlightCalls += 1;
        return new Promise((resolve) => {
          releaseInFlight = resolve;
        });
      },
    });

    now += 100;
    await registry.invoke({
      invocationId: `po1_${"9".repeat(64)}`,
      requestHash: "5".repeat(64),
      execute: async () => ({ status: "ok" }),
    });

    assert.deepEqual(
      await registry.invoke({
        invocationId: uncertainId,
        requestHash: "3".repeat(64),
        execute: async () => {
          uncertainCalls += 1;
          return { status: "ok" };
        },
      }),
      uncertain,
    );
    const inFlightRetry = registry.invoke({
      invocationId: inFlightId,
      requestHash: "4".repeat(64),
      execute: async () => {
        inFlightCalls += 1;
        return { status: "wrong-executor" };
      },
    });
    assert.equal(uncertainCalls, 1);
    assert.equal(inFlightCalls, 1);
    releaseInFlight({ status: "ok", response: "one execution" });
    assert.deepEqual(await inFlight, {
      status: "ok",
      response: "one execution",
    });
    assert.deepEqual(await inFlightRetry, {
      status: "ok",
      response: "one execution",
    });
  });

  it("bounds a flood of settled post-quarantine rejections", async () => {
    const registry = gatewayInvocation.createTrustedInvocationRegistry({
      ttlMs: 60_000,
      maxSettledEntries: 2,
    });
    await registry.invoke({
      invocationId: `po1_${"a".repeat(64)}`,
      requestHash: "a".repeat(64),
      execute: async () => ({
        status: "uncertain",
        errorCode: "UNCERTAIN_AGENT_RUN",
      }),
    });

    for (const digit of ["1", "2", "3", "4", "5"]) {
      await registry.invoke({
        invocationId: `po1_${digit.repeat(64)}`,
        requestHash: digit.repeat(64),
        execute: async () => ({
          status: "quarantined",
          errorCode: "AGENT_RUNTIME_QUARANTINED",
        }),
      });
    }

    assert.deepEqual(registry.getStats(), {
      total: 3,
      inFlight: 0,
      pinned: 1,
      settledEvictable: 2,
    });
  });

  it("extracts the correlated terminal agent response", () => {
    assert.equal(
      gatewayInvocation.extractAgentResponseText({
        status: "ok",
        result: {
          payloads: [
            { text: "first" },
            { mediaUrl: "ignored" },
            { text: "second" },
          ],
        },
      }),
      "first\nsecond",
    );
    assert.equal(gatewayInvocation.extractAgentResponseText({ status: "ok" }), "");
  });

  it("waits through accepted responses and resolves only same-id terminal responses", () => {
    assert.equal(
      gatewayInvocation.classifyAgentResponse(
        {
          type: "res",
          id: "agent-1",
          ok: true,
          payload: { status: "accepted" },
        },
        "agent-1",
      ),
      "pending",
    );
    assert.equal(
      gatewayInvocation.classifyAgentResponse(
        {
          type: "res",
          id: "agent-1",
          ok: true,
          payload: { status: "in_flight" },
        },
        "agent-1",
      ),
      "pending",
    );
    assert.equal(
      gatewayInvocation.classifyAgentResponse(
        {
          type: "res",
          id: "other-run",
          ok: true,
          payload: { status: "ok" },
        },
        "agent-1",
      ),
      "ignore",
    );
    assert.equal(
      gatewayInvocation.classifyAgentResponse(
        {
          type: "res",
          id: "agent-1",
          ok: true,
          payload: { status: "ok" },
        },
        "agent-1",
      ),
      "terminal",
    );
    assert.equal(
      gatewayInvocation.classifyAgentResponse(
        {
          type: "res",
          id: "agent-1",
          ok: false,
          error: { message: "denied" },
        },
        "agent-1",
      ),
      "terminal",
    );
  });
});

describe("production gateway coupling", () => {
  it("routes the runtime bridge through the reviewed helper boundary", () => {
    const source = fs.readFileSync(
      path.join(__dirname, "agentcore-contract.js"),
      "utf8",
    );

    assert.match(source, /require\("\.\/gateway-invocation"\)/);
    assert.match(source, /createGatewayRuntimeBoundary\(\{/);
    assert.match(source, /invokeGatewayAgent\(\{/);
    assert.match(source, /gatewayRuntimeBoundary\.invoke\(\{/);
    assert.match(source, /deriveGatewayRunId\(\{/);
    assert.match(source, /createTrustedInvocationRegistry\(\{/);
    assert.match(source, /maxSettledEntries:\s*64/);
    assert.match(source, /maxInFlightEntries:\s*8/);
    assert.match(source, /trustedInvocationRegistry\.invoke\(\{/);
    assert.match(source, /hashTrustedInvocationRequest\(\{/);
    assert.match(source, /createBoundedSerialExecutor\(\{/);
    assert.doesNotMatch(source, /messageQueue\.push/);
    assert.match(source, /gatewayQuarantined/);
    assert.match(source, /UNCERTAIN_AGENT_RUN/);
    assert.match(
      source,
      /if \(shuttingDown \|\| openclawReady \|\| gatewayQuarantined\) return;/,
    );
    assert.match(
      source,
      /gatewayQuarantined\s*=\s*gatewayRuntimeBoundary\.getQuarantine\(\);[\s\S]{0,80}if \(gatewayQuarantined\) openclawReady = false;/,
    );
    assert.doesNotMatch(source, /chat\.send/);
    assert.doesNotMatch(source, /new WebSocket\(wsUrl,\s*\{/);
    assert.doesNotMatch(source, /origin\s*:/i);
    assert.doesNotMatch(source, /extraSystemPrompt|promptMode|internalRuntimeHandoff/);
    assert.doesNotMatch(source, /still working on your previous request/i);
  });

  it("makes the pinned proof consume server-reported scopes and production requests", () => {
    const source = fs.readFileSync(
      path.join(__dirname, "..", "scripts", "verify-gateway-scope-boundary.js"),
      "utf8",
    );

    assert.match(source, /require\("\.\.\/bridge\/gateway-invocation"\)/);
    assert.match(source, /createGatewaySocket\(WebSocket,\s*url\)/);
    assert.match(source, /assertGrantedGatewayScopes\(message\.payload\)/);
    assert.match(source, /buildAgentRequest\(/);
    assert.match(source, /extractAgentResponseText\(message\.payload\)/);
    assert.doesNotMatch(source, /chat\.send/);
    assert.doesNotMatch(source, /connectedScopes:\s*\[/);
    assert.doesNotMatch(source, /new WebSocket\(url,\s*\{/);
  });
});
