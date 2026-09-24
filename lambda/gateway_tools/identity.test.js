/**
 * Unit tests for lib/identity.js and the REQUEST interceptor.
 * Run: node --test lambda/gateway_tools/
 */
"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const h = require("./test-helpers");
const { createVerifier, resolveCaller, RESERVED_ARG } = require("./lib/identity");
const interceptor = require("./interceptor/index");

function verifier(extra = {}) {
  return createVerifier({ issuer: h.ISSUER, clientId: h.CLIENT_ID, fetchJwks: h.fakeFetchJwks, ...extra });
}

describe("identity.createVerifier", () => {
  it("accepts a valid Cognito ID token and derives the namespace from cognito:username", async () => {
    const id = await verifier().verify(h.idToken("telegram:123456"));
    assert.equal(id.actorId, "telegram:123456");
    assert.equal(id.namespace, "telegram_123456");
    assert.equal(id.tokenUse, "id");
  });

  it("accepts a Cognito access token (client_id + username claims)", async () => {
    const id = await verifier().verify(h.accessToken("slack:U0AGD41CBGS"));
    assert.equal(id.namespace, "slack_U0AGD41CBGS");
    assert.equal(id.tokenUse, "access");
  });

  it("rejects a token signed by another key", async () => {
    const forged = h.signToken(
      {
        iss: h.ISSUER, aud: h.CLIENT_ID, token_use: "id",
        "cognito:username": "telegram:999", exp: Math.floor(Date.now() / 1000) + 600,
      },
      { key: h.otherPrivateKey },
    );
    await assert.rejects(verifier().verify(forged), /bad signature/);
  });

  it("rejects alg=none / non-RS256 headers", async () => {
    const [, payload] = h.idToken("telegram:1").split(".");
    const header = Buffer.from(JSON.stringify({ alg: "none", kid: h.KID })).toString("base64url");
    await assert.rejects(verifier().verify(`${header}.${payload}.`), /unsupported alg/);
  });

  it("rejects wrong issuer, wrong audience, wrong client, expiry, unknown token_use", async () => {
    const v = verifier();
    await assert.rejects(v.verify(h.idToken("telegram:1", { iss: "https://evil.example" })), /wrong issuer/);
    await assert.rejects(v.verify(h.idToken("telegram:1", { aud: "other-client" })), /wrong audience/);
    await assert.rejects(v.verify(h.accessToken("telegram:1", { client_id: "other" })), /wrong client/);
    await assert.rejects(v.verify(h.idToken("telegram:1", { exp: Math.floor(Date.now() / 1000) - 5 })), /expired/);
    await assert.rejects(v.verify(h.idToken("telegram:1", { token_use: "refresh" })), /unknown token_use/);
  });

  it("rejects usernames that are not channel identities (no namespace escape)", async () => {
    const v = verifier();
    await assert.rejects(v.verify(h.idToken("default-user")), /not a channel identity/);
    await assert.rejects(v.verify(h.idToken("telegram:../../other")), /not a channel identity/);
    await assert.rejects(v.verify(h.idToken("admin")), /not a channel identity/);
  });

  it("refetches JWKS once for an unknown kid, then fails", async () => {
    let calls = 0;
    const v = verifier({ fetchJwks: () => { calls++; return Promise.resolve(h.JWKS); } });
    await assert.rejects(v.verify(h.idToken("telegram:1", {}) .replace(/^[^.]+/, () =>
      Buffer.from(JSON.stringify({ alg: "RS256", kid: "rotated" })).toString("base64url"))), /unknown signing key|bad signature/);
    assert.equal(calls, 2);
  });
});

describe("identity.resolveCaller", () => {
  it("strips the reserved argument and returns the verified identity", async () => {
    const event = { filename: "a.txt", [RESERVED_ARG]: h.idToken("telegram:42") };
    const { identity, args } = await resolveCaller(event, verifier());
    assert.equal(identity.namespace, "telegram_42");
    assert.deepEqual(args, { filename: "a.txt" });
  });

  it("fails closed when the reserved argument is missing (interceptor not wired)", async () => {
    await assert.rejects(resolveCaller({ filename: "a.txt" }, verifier()), /interceptor did not run/);
  });

  it("ignores any user_id / namespace argument the model supplies", async () => {
    const event = {
      user_id: "telegram_victim", namespace: "telegram_victim",
      [RESERVED_ARG]: h.idToken("telegram:attacker"),
    };
    const { identity } = await resolveCaller(event, verifier());
    assert.equal(identity.namespace, "telegram_attacker");
  });
});

describe("interceptor", () => {
  const headers = { Authorization: "Bearer eyJ.fake.token", Accept: "application/json" };

  it("injects the bearer as __caller_token on tools/call, overwriting a spoofed value", async () => {
    const body = {
      jsonrpc: "2.0", id: 7, method: "tools/call",
      params: { name: "user-files___read_file", arguments: { filename: "x", [RESERVED_ARG]: "spoofed" } },
    };
    const out = await interceptor.handler({ interceptorInputVersion: "1.0", mcp: { gatewayRequest: { headers, body } } });
    assert.equal(out.interceptorOutputVersion, "1.0");
    const args = out.mcp.transformedGatewayRequest.body.params.arguments;
    assert.equal(args[RESERVED_ARG], "eyJ.fake.token");
    assert.equal(args.filename, "x");
    assert.equal(out.mcp.transformedGatewayResponse, undefined);
  });

  it("is case-insensitive on the header name", async () => {
    const body = { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "t", arguments: {} } };
    const out = await interceptor.handler({ mcp: { gatewayRequest: { headers: { authorization: "bearer abc" }, body } } });
    assert.equal(out.mcp.transformedGatewayRequest.body.params.arguments[RESERVED_ARG], "abc");
  });

  it("passes tools/list and initialize through unchanged", async () => {
    for (const method of ["tools/list", "initialize", "ping"]) {
      const body = { jsonrpc: "2.0", id: 2, method };
      const out = await interceptor.handler({ mcp: { gatewayRequest: { headers, body } } });
      assert.deepEqual(out.mcp.transformedGatewayRequest.body, body);
    }
  });

  it("short-circuits tools/call with a JSON-RPC error when no bearer is present", async () => {
    const body = { jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "t", arguments: {} } };
    const out = await interceptor.handler({ mcp: { gatewayRequest: { body } } });
    assert.equal(out.mcp.transformedGatewayResponse.statusCode, 401);
    assert.equal(out.mcp.transformedGatewayResponse.body.id, 3);
    assert.match(out.mcp.transformedGatewayResponse.body.error.message, /missing bearer/);
  });

  it("passes a RESPONSE payload through unchanged if ever invoked as one", async () => {
    const out = await interceptor.handler({
      mcp: { gatewayRequest: { body: {} }, gatewayResponse: { statusCode: 200, body: { jsonrpc: "2.0", id: 1, result: {} } } },
    });
    assert.deepEqual(out.mcp.transformedGatewayResponse, { statusCode: 200, body: { jsonrpc: "2.0", id: 1, result: {} } });
  });
});
