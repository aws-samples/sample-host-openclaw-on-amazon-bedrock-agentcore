"use strict";

const { GATEWAY_CLIENT_SCOPES } = require("./runtime-policy");

/**
 * Open a loopback CLI connection. Deliberately omit Origin: pinned
 * OpenClaw treats an Origin-bearing, device-less shared-token connection as a
 * browser-like client and clears its self-declared scopes.
 */
function createGatewaySocket(WebSocketConstructor, url) {
  return new WebSocketConstructor(url);
}

function buildGatewayConnectRequest({ id, token, version = "dev" }) {
  return {
    type: "req",
    id,
    method: "connect",
    params: {
      minProtocol: 4,
      maxProtocol: 4,
      client: {
        id: "cli",
        mode: "cli",
        version,
        platform: "linux",
      },
      caps: [],
      auth: { token },
      role: "operator",
      scopes: GATEWAY_CLIENT_SCOPES,
    },
  };
}

/**
 * Trust the gateway's hello, not the scopes the client asked for. Exact scope
 * equality fails closed on both silent scope loss and unexpected expansion.
 */
function assertGrantedGatewayScopes(helloPayload) {
  const granted = helloPayload?.auth?.scopes;
  const expected = GATEWAY_CLIENT_SCOPES;
  const grantedSet = Array.isArray(granted) ? new Set(granted) : null;
  const exact =
    grantedSet !== null &&
    granted.length === expected.length &&
    grantedSet.size === expected.length &&
    expected.every((scope) => grantedSet.has(scope));

  if (!exact) {
    throw new Error(
      `Unexpected granted gateway scopes: ${JSON.stringify(granted ?? null)}; ` +
        `expected ${JSON.stringify(expected)}`,
    );
  }
  return granted;
}

function buildAgentRequest({ id, message }) {
  return {
    type: "req",
    id,
    method: "agent",
    params: {
      sessionKey: "global",
      message,
      deliver: false,
      idempotencyKey: id,
    },
  };
}

function extractAgentResponseText(payload) {
  const responsePayloads = payload?.result?.payloads;
  if (!Array.isArray(responsePayloads)) return "";
  return responsePayloads
    .map((entry) => (typeof entry?.text === "string" ? entry.text : ""))
    .filter(Boolean)
    .join("\n");
}

function classifyAgentResponse(message, requestId) {
  if (message?.type !== "res" || message.id !== requestId) return "ignore";
  if (
    message.ok === true &&
    ["accepted", "in_flight"].includes(message.payload?.status)
  ) {
    return "pending";
  }
  return "terminal";
}

module.exports = {
  createGatewaySocket,
  buildGatewayConnectRequest,
  assertGrantedGatewayScopes,
  buildAgentRequest,
  extractAgentResponseText,
  classifyAgentResponse,
};
