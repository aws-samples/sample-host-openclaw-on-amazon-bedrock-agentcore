/**
 * Caller identity for AgentCore Gateway Lambda tool targets.
 *
 * The Gateway's CUSTOM_JWT authorizer validates the bearer token on the way in,
 * but the documented Lambda-target contract does not forward any claim to the
 * function: `context.clientContext.Custom` carries only the
 * bedrockAgentCore{MessageVersion,AwsRequestId,McpMessageId,GatewayId,TargetId,
 * ToolName} fields, and `event` is the tool's own arguments.
 *   https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-add-target-lambda.html
 *
 * The REQUEST interceptor (../interceptor) therefore copies the bearer JWT from
 * the incoming `Authorization` header into the reserved tool argument
 * `__caller_token`, overwriting whatever the model supplied. This module then
 * verifies that token *again*, independently of the interceptor, so the tool
 * never trusts an unsigned name or namespace argument:
 *   - RS256 signature against the user pool's JWKS (Node's crypto, no deps)
 *   - iss === COGNITO_ISSUER_URL
 *   - aud (ID token) or client_id (access token) === COGNITO_CLIENT_ID
 *   - exp in the future
 * and derives the OpenClaw namespace from the token's `cognito:username`
 * (ID token) / `username` (access token), which the proxy sets to the actorId
 * ("telegram:123"), so namespace = username with ":" -> "_" — identical to
 * `actorId.replace(/:/g, "_")` in bridge/agentcore-proxy.js.
 *
 * Which token type the Gateway accepts (ID vs access) is unverified — see
 * docs/gateway-mcp-tools.md. Both are handled here so the Lambda side is not
 * the blocker either way.
 */
"use strict";

const crypto = require("node:crypto");
const https = require("node:https");

const RESERVED_ARG = "__caller_token";

// Same namespace pattern the exec skills enforce (bridge/skills/*/common.js).
const VALID_NAMESPACE = /^(telegram|slack|discord|whatsapp|feishu)_[a-zA-Z0-9_-]{1,64}$/;

class IdentityError extends Error {
  constructor(message) {
    super(message);
    this.name = "IdentityError";
  }
}

function b64urlDecode(str) {
  return Buffer.from(str.replace(/-/g, "+").replace(/_/g, "/"), "base64");
}

function decodeJwt(token) {
  if (typeof token !== "string") throw new IdentityError("caller token missing");
  const parts = token.split(".");
  if (parts.length !== 3) throw new IdentityError("caller token malformed");
  let header, payload;
  try {
    header = JSON.parse(b64urlDecode(parts[0]).toString("utf8"));
    payload = JSON.parse(b64urlDecode(parts[1]).toString("utf8"));
  } catch {
    throw new IdentityError("caller token malformed");
  }
  return { header, payload, signingInput: `${parts[0]}.${parts[1]}`, signature: b64urlDecode(parts[2]) };
}

function httpsGetJson(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { timeout: 5000 }, (res) => {
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => {
        if (res.statusCode !== 200) {
          reject(new Error(`JWKS fetch failed: HTTP ${res.statusCode}`));
          return;
        }
        try {
          resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
        } catch (err) {
          reject(err);
        }
      });
    });
    req.on("timeout", () => req.destroy(new Error("JWKS fetch timed out")));
    req.on("error", reject);
  });
}

/**
 * Build a verifier. `deps.fetchJwks(url)` is injectable for tests.
 * Keys are cached per process; an unknown `kid` triggers one refetch
 * (Cognito rotates signing keys).
 */
function createVerifier(options = {}) {
  const issuer = options.issuer || process.env.COGNITO_ISSUER_URL || "";
  const clientId = options.clientId || process.env.COGNITO_CLIENT_ID || "";
  const fetchJwks = options.fetchJwks || httpsGetJson;
  const now = options.now || (() => Math.floor(Date.now() / 1000));

  if (!issuer || !clientId) {
    throw new Error("COGNITO_ISSUER_URL and COGNITO_CLIENT_ID must be set");
  }

  let keyCache = null; // Map<kid, KeyObject>

  async function loadKeys(force) {
    if (keyCache && !force) return keyCache;
    const jwks = await fetchJwks(`${issuer}/.well-known/jwks.json`);
    const map = new Map();
    for (const jwk of jwks.keys || []) {
      if (jwk.kty !== "RSA" || !jwk.kid) continue;
      map.set(jwk.kid, crypto.createPublicKey({ key: jwk, format: "jwk" }));
    }
    keyCache = map;
    return map;
  }

  async function verify(token) {
    const { header, payload, signingInput, signature } = decodeJwt(token);
    if (header.alg !== "RS256") throw new IdentityError("caller token: unsupported alg");
    if (!header.kid) throw new IdentityError("caller token: missing kid");

    let keys = await loadKeys(false);
    let key = keys.get(header.kid);
    if (!key) {
      keys = await loadKeys(true);
      key = keys.get(header.kid);
    }
    if (!key) throw new IdentityError("caller token: unknown signing key");

    const ok = crypto.verify("RSA-SHA256", Buffer.from(signingInput, "utf8"), key, signature);
    if (!ok) throw new IdentityError("caller token: bad signature");

    if (payload.iss !== issuer) throw new IdentityError("caller token: wrong issuer");
    if (typeof payload.exp !== "number" || payload.exp <= now()) {
      throw new IdentityError("caller token: expired");
    }
    if (payload.token_use === "id") {
      if (payload.aud !== clientId) throw new IdentityError("caller token: wrong audience");
    } else if (payload.token_use === "access") {
      if (payload.client_id !== clientId) throw new IdentityError("caller token: wrong client");
    } else {
      throw new IdentityError("caller token: unknown token_use");
    }

    const username = payload["cognito:username"] || payload.username;
    if (!username || typeof username !== "string") {
      throw new IdentityError("caller token: no username claim");
    }
    const namespace = username.replace(/:/g, "_");
    if (!VALID_NAMESPACE.test(namespace)) {
      throw new IdentityError("caller token: username is not a channel identity");
    }
    return { actorId: username, namespace, sub: payload.sub, tokenUse: payload.token_use };
  }

  return { verify };
}

/**
 * Pull the reserved token out of the tool arguments, verify it, and return
 * `{ identity, args }` where `args` no longer contains the reserved key.
 * Every other argument is treated as untrusted model input.
 */
async function resolveCaller(event, verifier) {
  const args = { ...(event || {}) };
  const token = args[RESERVED_ARG];
  delete args[RESERVED_ARG];
  if (!token) {
    throw new IdentityError(
      `${RESERVED_ARG} missing — the gateway REQUEST interceptor did not run`,
    );
  }
  const identity = await verifier.verify(token);
  return { identity, args };
}

module.exports = {
  RESERVED_ARG,
  VALID_NAMESPACE,
  IdentityError,
  decodeJwt,
  createVerifier,
  resolveCaller,
};
