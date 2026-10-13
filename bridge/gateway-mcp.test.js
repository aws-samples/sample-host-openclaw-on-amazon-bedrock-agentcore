/**
 * Tests for gateway-mcp.js (mcp.servers.agentcore config) and
 * cognito-token.js (shared per-user token provider).
 * Run: node --test gateway-mcp.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("crypto");

const gw = require("./gateway-mcp");
const { createCognitoTokenProvider } = require("./cognito-token");

const BASE_CONFIG = {
  models: { providers: { agentcore: { baseUrl: "http://127.0.0.1:18790/v1" } } },
  tools: { profile: "full" },
  gateway: { mode: "local", port: 18789 },
};

describe("gateway-mcp.applyGatewayMcp", () => {
  it("leaves the config untouched (same object) when no gateway url is set", () => {
    assert.equal(gw.applyGatewayMcp(BASE_CONFIG, { gatewayUrl: "", token: "t" }), BASE_CONFIG);
    assert.equal(gw.applyGatewayMcp(BASE_CONFIG, {}), BASE_CONFIG);
    assert.equal(JSON.stringify(gw.applyGatewayMcp(BASE_CONFIG, { gatewayUrl: undefined })), JSON.stringify(BASE_CONFIG));
    assert.equal("mcp" in BASE_CONFIG, false);
  });

  it("adds mcp.servers.agentcore as a streamable-http server with a bearer header", () => {
    const url = "https://abc123.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp";
    const out = gw.applyGatewayMcp(BASE_CONFIG, { gatewayUrl: url, token: "eyJ.id.token" });
    assert.notEqual(out, BASE_CONFIG);
    assert.equal("mcp" in BASE_CONFIG, false, "input not mutated");
    assert.deepEqual(out.mcp.servers.agentcore, {
      url,
      transport: "streamable-http",
      headers: { Authorization: "Bearer eyJ.id.token" },
      requestTimeoutMs: 30000,
      connectionTimeoutMs: 10000,
      supportsParallelToolCalls: true,
    });
    // Everything else is preserved.
    assert.deepEqual(out.models, BASE_CONFIG.models);
    assert.deepEqual(out.tools, BASE_CONFIG.tools);
  });

  it("keeps other mcp servers and requires a token", () => {
    const cfg = { ...BASE_CONFIG, mcp: { servers: { docs: { command: "uvx" } } } };
    const out = gw.applyGatewayMcp(cfg, { gatewayUrl: "https://x/mcp", token: "t" });
    assert.deepEqual(Object.keys(out.mcp.servers).sort(), ["agentcore", "docs"]);
    assert.throws(() => gw.applyGatewayMcp(cfg, { gatewayUrl: "https://x/mcp", token: null }), /token is required/);
  });

  it("isEnabled reflects a non-blank AGENTCORE_GATEWAY_URL only", () => {
    assert.equal(gw.isEnabled({}), false);
    assert.equal(gw.isEnabled({ AGENTCORE_GATEWAY_URL: "  " }), false);
    assert.equal(gw.isEnabled({ AGENTCORE_GATEWAY_URL: "https://x/mcp" }), true);
  });
});

describe("cognito-token.createCognitoTokenProvider", () => {
  // Fake command classes so the test runs without @aws-sdk installed.
  const commands = {};
  for (const n of ["CognitoIdentityProviderClient", "AdminGetUserCommand", "AdminCreateUserCommand", "AdminSetUserPasswordCommand", "AdminInitiateAuthCommand"]) {
    commands[n] = class {
      constructor(input) {
        this.input = input;
      }
    };
    Object.defineProperty(commands[n], "name", { value: n });
  }
  const cfg = { userPoolId: "us-west-2_POOL", clientId: "client", passwordSecret: "s3cret", region: "us-west-2", commands };

  it("derives the same HMAC password the proxy used to derive inline", () => {
    const provider = createCognitoTokenProvider(cfg);
    const expected = crypto.createHmac("sha256", "s3cret").update("telegram:123").digest("base64url").slice(0, 32);
    assert.equal(provider.derivePassword("telegram:123"), expected);
    assert.equal(provider.derivePassword("telegram:123").length, 32);
  });

  it("returns null when not configured and caches tokens until 60 s before expiry", async () => {
    assert.equal(await createCognitoTokenProvider({ ...cfg, clientId: "" }).getIdToken("a"), null);

    const sent = [];
    const client = {
      send: async (cmd) => {
        sent.push(cmd.constructor.name);
        if (cmd.constructor.name === "AdminGetUserCommand") return {};
        if (cmd.constructor.name === "AdminInitiateAuthCommand") {
          return {
            AuthenticationResult: { IdToken: `tok${sent.length}`, AccessToken: `acc${sent.length}`, ExpiresIn: 3600 },
          };
        }
        return {};
      },
    };
    const provider = createCognitoTokenProvider({ ...cfg, client, log: () => {} });
    const first = await provider.getIdToken("telegram:1");
    assert.match(first.token, /^tok/);
    assert.ok(first.expiresAt > Date.now() + 3500 * 1000);
    const second = await provider.getIdToken("telegram:1");
    assert.equal(second, first, "cached");
    // Both token types come from the same authentication and share the cache.
    const access = await provider.getAccessToken("telegram:1");
    assert.match(access.token, /^acc/);
    assert.equal(access.expiresAt, first.expiresAt);
    assert.equal(sent.filter((n) => n === "AdminInitiateAuthCommand").length, 1, "no extra auth for the access token");
    const forced = await provider.getIdToken("telegram:1", { force: true });
    assert.notEqual(forced.token, first.token, "force bypasses the cache");
    assert.deepEqual(
      sent.filter((n) => n === "AdminInitiateAuthCommand").length,
      2,
    );
  });

  it("getAccessToken refuses to hand out an empty bearer", async () => {
    const client = {
      send: async (cmd) => {
        if (cmd.constructor.name === "AdminInitiateAuthCommand") return { AuthenticationResult: { IdToken: "t", ExpiresIn: 3600 } };
        return {};
      },
    };
    const provider = createCognitoTokenProvider({ ...cfg, client, log: () => {} });
    await assert.rejects(provider.getAccessToken("telegram:1"), /no AccessToken/);
    assert.equal(await createCognitoTokenProvider({ ...cfg, clientId: "" }).getAccessToken("a"), null);
  });

  it("provisions a missing user with a permanent password before authenticating", async () => {
    const sent = [];
    const client = {
      send: async (cmd) => {
        sent.push(cmd);
        const n = cmd.constructor.name;
        if (n === "AdminGetUserCommand") throw Object.assign(new Error("nf"), { name: "UserNotFoundException" });
        if (n === "AdminInitiateAuthCommand") return { AuthenticationResult: { IdToken: "t", ExpiresIn: 3600 } };
        return {};
      },
    };
    const provider = createCognitoTokenProvider({ ...cfg, client, log: () => {} });
    await provider.getIdToken("slack:U1");
    const names = sent.map((c) => c.constructor.name);
    assert.deepEqual(names, ["AdminGetUserCommand", "AdminCreateUserCommand", "AdminSetUserPasswordCommand", "AdminInitiateAuthCommand"]);
    const setPw = sent[2].input;
    assert.equal(setPw.Permanent, true);
    assert.equal(setPw.Password, provider.derivePassword("slack:U1"));
    assert.equal(sent[3].input.AuthFlow, "ADMIN_USER_PASSWORD_AUTH");
    assert.equal(sent[3].input.AuthParameters.PASSWORD, provider.derivePassword("slack:U1"));
  });
});
