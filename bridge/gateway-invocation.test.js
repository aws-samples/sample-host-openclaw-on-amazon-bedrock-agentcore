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
    assert.match(source, /createGatewaySocket\(WebSocket,\s*wsUrl\)/);
    assert.match(source, /buildGatewayConnectRequest\(/);
    assert.match(source, /assertGrantedGatewayScopes\(msg\.payload\)/);
    assert.match(source, /buildAgentRequest\(/);
    assert.match(source, /extractAgentResponseText\(msg\.payload\)/);
    assert.match(source, /classifyAgentResponse\(msg,\s*agentReqId\)/);
    assert.doesNotMatch(source, /chat\.send/);
    assert.doesNotMatch(source, /new WebSocket\(wsUrl,\s*\{/);
    assert.doesNotMatch(source, /origin\s*:/i);
    assert.doesNotMatch(source, /extraSystemPrompt|promptMode|internalRuntimeHandoff/);
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
