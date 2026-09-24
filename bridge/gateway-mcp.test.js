/**
 * Tests for gateway-mcp.js (mcp.servers.agentcore config + bearer refresh) and
 * cognito-token.js (shared per-user token provider).
 * Run: node --test gateway-mcp.test.js
 */
const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const os = require("os");
const path = require("path");
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

describe("gateway-mcp.rewriteBearer", () => {
  let dir;
  beforeEach(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "gwmcp-"));
  });
  afterEach(() => {
    fs.rmSync(dir, { recursive: true, force: true });
  });

  it("rewrites only the Authorization header, atomically, and reports change", () => {
    const p = path.join(dir, "openclaw.json");
    const cfg = gw.applyGatewayMcp(BASE_CONFIG, { gatewayUrl: "https://x/mcp", token: "old" });
    fs.writeFileSync(p, JSON.stringify(cfg, null, 2));

    assert.equal(gw.rewriteBearer(p, "new"), true);
    const after = JSON.parse(fs.readFileSync(p, "utf8"));
    assert.equal(after.mcp.servers.agentcore.headers.Authorization, "Bearer new");
    assert.equal(after.mcp.servers.agentcore.url, "https://x/mcp");
    assert.deepEqual(after.models, BASE_CONFIG.models);
    assert.deepEqual(fs.readdirSync(dir), ["openclaw.json"], "no tmp file left behind");

    assert.equal(gw.rewriteBearer(p, "new"), false, "idempotent");
  });

  it("is a no-op on a config without the agentcore server", () => {
    const p = path.join(dir, "openclaw.json");
    fs.writeFileSync(p, JSON.stringify(BASE_CONFIG));
    assert.equal(gw.rewriteBearer(p, "x"), false);
    assert.deepEqual(JSON.parse(fs.readFileSync(p, "utf8")), BASE_CONFIG);
  });
});

describe("gateway-mcp.scheduleBearerRefresh", () => {
  let dir, p;
  beforeEach(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "gwmcp-"));
    p = path.join(dir, "openclaw.json");
    fs.writeFileSync(p, JSON.stringify(gw.applyGatewayMcp(BASE_CONFIG, { gatewayUrl: "https://x/mcp", token: "t0" })));
  });
  afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

  function fakeTimers() {
    const timers = [];
    return {
      timers,
      setTimer: (fn, ms) => {
        const h = { fn, ms, cleared: false, unref() {} };
        timers.push(h);
        return h;
      },
      clearTimer: (h) => {
        h.cleared = true;
      },
    };
  }

  it("schedules the first refresh leadMs before expiry and forces a fresh token", async () => {
    const t = fakeTimers();
    let now = 1_000_000;
    const calls = [];
    const expires1 = now + 3_540_000; // 59 min out, like Cognito's 3600 - 60
    const handle = gw.scheduleBearerRefresh({
      configPath: p,
      getToken: async (opts) => {
        calls.push(opts);
        return { token: `t${calls.length}`, expiresAt: now + 3_540_000 };
      },
      leadMs: 5 * 60 * 1000,
      now: () => now,
      setTimer: t.setTimer,
      clearTimer: t.clearTimer,
      log: () => {},
    });
    handle.start(expires1);
    assert.equal(t.timers.length, 1);
    assert.equal(t.timers[0].ms, expires1 - 5 * 60 * 1000 - now);

    now = expires1 - 5 * 60 * 1000;
    await t.timers[0].fn();
    assert.deepEqual(calls, [{ force: true }]);
    assert.equal(JSON.parse(fs.readFileSync(p, "utf8")).mcp.servers.agentcore.headers.Authorization, "Bearer t1");
    assert.equal(t.timers.length, 2, "next refresh scheduled");
    assert.equal(t.timers[1].ms, 3_540_000 - 5 * 60 * 1000);

    handle.stop();
    assert.equal(t.timers[1].cleared, true);
  });

  it("retries in 60 s when minting fails and never goes below a 30 s delay", async () => {
    const t = fakeTimers();
    const now = 5_000_000;
    let fail = true;
    const handle = gw.scheduleBearerRefresh({
      configPath: p,
      getToken: async () => {
        if (fail) throw new Error("cognito down");
        return { token: "ok", expiresAt: now + 1000 };
      },
      now: () => now,
      setTimer: t.setTimer,
      clearTimer: t.clearTimer,
      log: () => {},
    });
    handle.start(now + 10); // already inside the lead window
    assert.equal(t.timers[0].ms, 30 * 1000);
    await t.timers[0].fn();
    assert.equal(t.timers[1].ms, 60 * 1000, "retry delay");
    assert.equal(JSON.parse(fs.readFileSync(p, "utf8")).mcp.servers.agentcore.headers.Authorization, "Bearer t0");
    fail = false;
    await t.timers[1].fn();
    assert.equal(JSON.parse(fs.readFileSync(p, "utf8")).mcp.servers.agentcore.headers.Authorization, "Bearer ok");
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
