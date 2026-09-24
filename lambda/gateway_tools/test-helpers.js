/**
 * Test helpers: a throwaway RSA key pair, a fake JWKS, and signed Cognito-shaped tokens.
 * Not shipped in the Lambda asset (excluded by Code.from_asset in gateway_stack.py).
 */
"use strict";

const crypto = require("node:crypto");

const ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_TESTPOOL";
const CLIENT_ID = "test-client-id";

const { publicKey, privateKey } = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
const otherPair = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });

const KID = "kid-1";
const jwk = { ...publicKey.export({ format: "jwk" }), kid: KID, alg: "RS256", use: "sig" };
const JWKS = { keys: [jwk] };

function b64url(buf) {
  return Buffer.from(buf).toString("base64url");
}

function signToken(payload, { key = privateKey, kid = KID, alg = "RS256" } = {}) {
  const header = { alg, kid, typ: "JWT" };
  const input = `${b64url(JSON.stringify(header))}.${b64url(JSON.stringify(payload))}`;
  const sig = crypto.sign("RSA-SHA256", Buffer.from(input), key);
  return `${input}.${b64url(sig)}`;
}

function idToken(username, overrides = {}) {
  const now = Math.floor(Date.now() / 1000);
  return signToken({
    sub: "11111111-2222-3333-4444-555555555555",
    iss: ISSUER,
    aud: CLIENT_ID,
    token_use: "id",
    "cognito:username": username,
    exp: now + 3600,
    iat: now,
    ...overrides,
  });
}

function accessToken(username, overrides = {}) {
  const now = Math.floor(Date.now() / 1000);
  return signToken({
    sub: "11111111-2222-3333-4444-555555555555",
    iss: ISSUER,
    client_id: CLIENT_ID,
    token_use: "access",
    username,
    exp: now + 3600,
    iat: now,
    ...overrides,
  });
}

function fakeFetchJwks() {
  fakeFetchJwks.calls = (fakeFetchJwks.calls || 0) + 1;
  return Promise.resolve(JWKS);
}

/** Lambda context as the Node runtime exposes it (clientContext.custom). */
function gatewayContext(targetName, toolName) {
  return {
    clientContext: {
      custom: {
        bedrockAgentCoreMessageVersion: "1.0",
        bedrockAgentCoreAwsRequestId: "req-1",
        bedrockAgentCoreMcpMessageId: "1",
        bedrockAgentCoreGatewayId: "gw-1",
        bedrockAgentCoreTargetId: "tgt-1",
        bedrockAgentCoreToolName: `${targetName}___${toolName}`,
      },
    },
  };
}

/** Records every command sent; answers from a queue or a function. */
function fakeClient(answer) {
  const sent = [];
  return {
    sent,
    send(cmd) {
      sent.push(cmd);
      return Promise.resolve(typeof answer === "function" ? answer(cmd, sent.length) : answer);
    },
  };
}

/** Command "classes" that just record their input and constructor name. */
function fakeCommands(...names) {
  const out = {};
  for (const n of names) {
    out[n] = class {
      constructor(input) {
        this.input = input;
        this.kind = n;
      }
    };
  }
  return out;
}

module.exports = {
  ISSUER,
  CLIENT_ID,
  KID,
  JWKS,
  privateKey,
  otherPrivateKey: otherPair.privateKey,
  signToken,
  idToken,
  accessToken,
  fakeFetchJwks,
  gatewayContext,
  fakeClient,
  fakeCommands,
};
