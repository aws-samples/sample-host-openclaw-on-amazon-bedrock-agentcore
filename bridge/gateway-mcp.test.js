/**
 * Tests for gateway-mcp.js (mcp.servers.agentcore config) and
 * cognito-token.js (shared per-user token provider).
 * Run: node --test gateway-mcp.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const gw = require("./gateway-mcp");
const { createCognitoTokenProvider, defaultSecretId, generateRandomPassword } = require("./cognito-token");

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
  for (const n of [
    "CognitoIdentityProviderClient",
    "AdminGetUserCommand",
    "AdminCreateUserCommand",
    "AdminSetUserPasswordCommand",
    "AdminInitiateAuthCommand",
  ]) {
    commands[n] = class {
      constructor(input) { this.input = input; }
    };
    Object.defineProperty(commands[n], "name", { value: n });
  }
  const secretsCommands = {};
  for (const n of ["SecretsManagerClient", "GetSecretValueCommand", "CreateSecretCommand", "PutSecretValueCommand"]) {
    secretsCommands[n] = class {
      constructor(input) { this.input = input; }
    };
    Object.defineProperty(secretsCommands[n], "name", { value: n });
  }

  const POOL = "us-west-2_POOL";
  const secretName = (ns) => `openclaw/cognito-user/${POOL}/${ns}`;

  function makeSecretsStore(initial = {}) {
    const store = new Map(Object.entries(initial));
    const sent = [];
    const client = {
      async send(cmd) {
        sent.push(cmd);
        const name = cmd.constructor.name;
        if (name === "GetSecretValueCommand") {
          const value = store.get(cmd.input.SecretId);
          if (value === undefined) {
            throw Object.assign(new Error("not found"), { name: "ResourceNotFoundException" });
          }
          return { SecretString: value };
        }
        if (name === "CreateSecretCommand") {
          if (store.has(cmd.input.Name)) {
            throw Object.assign(new Error("exists"), { name: "ResourceExistsException" });
          }
          store.set(cmd.input.Name, cmd.input.SecretString);
          return {};
        }
        if (name === "PutSecretValueCommand") {
          store.set(cmd.input.SecretId, cmd.input.SecretString);
          return {};
        }
        return {};
      },
    };
    return { client, store, sent };
  }

  // A Cognito fake that tracks each user's current password and only
  // authenticates with it, like the real pool.
  function makeCognito({ users = {} } = {}) {
    const passwords = new Map(Object.entries(users));
    const sent = [];
    let n = 0;
    const client = {
      async send(cmd) {
        sent.push(cmd);
        const name = cmd.constructor.name;
        const user = cmd.input.Username || cmd.input.AuthParameters?.USERNAME;
        if (name === "AdminGetUserCommand") {
          if (!passwords.has(user)) throw Object.assign(new Error("nf"), { name: "UserNotFoundException" });
          return {};
        }
        if (name === "AdminCreateUserCommand") {
          if (passwords.has(user)) throw Object.assign(new Error("dup"), { name: "UsernameExistsException" });
          passwords.set(user, cmd.input.TemporaryPassword);
          return {};
        }
        if (name === "AdminSetUserPasswordCommand") {
          passwords.set(user, cmd.input.Password);
          return {};
        }
        if (name === "AdminInitiateAuthCommand") {
          if (passwords.get(user) !== cmd.input.AuthParameters.PASSWORD) {
            throw Object.assign(new Error("Incorrect username or password."), { name: "NotAuthorizedException" });
          }
          n += 1;
          return { AuthenticationResult: { IdToken: `tok${n}`, AccessToken: `acc${n}`, ExpiresIn: 3600 } };
        }
        return {};
      },
    };
    const names = () => sent.map((c) => c.constructor.name);
    return { client, passwords, sent, names };
  }

  const cfg = { userPoolId: POOL, clientId: "client", region: "us-west-2", commands, secretsCommands };
  const provider = (cognito, sm, extra = {}) =>
    createCognitoTokenProvider({ ...cfg, client: cognito.client, secretsClient: sm.client, log: () => {}, ...extra });

  it("returns null when not configured and caches tokens until 60 s before expiry", async () => {
    assert.equal(await createCognitoTokenProvider({ ...cfg, clientId: "" }).getIdToken("a"), null);

    const cognito = makeCognito();
    const sm = makeSecretsStore();
    const p = provider(cognito, sm);
    const first = await p.getIdToken("telegram:1");
    assert.match(first.token, /^tok/);
    assert.ok(first.expiresAt > Date.now() + 3500 * 1000);
    const second = await p.getIdToken("telegram:1");
    assert.equal(second, first, "cached");
    // Both token types come from the same authentication and share the cache.
    const access = await p.getAccessToken("telegram:1");
    assert.match(access.token, /^acc/);
    assert.equal(access.expiresAt, first.expiresAt);
    const auths = () => cognito.names().filter((x) => x === "AdminInitiateAuthCommand").length;
    assert.equal(auths(), 1, "no extra auth for the access token");
    const forced = await p.getIdToken("telegram:1", { force: true });
    assert.notEqual(forced.token, first.token, "force bypasses the cache");
    assert.equal(auths(), 2);
  });

  it("getAccessToken refuses to hand out an empty bearer", async () => {
    const sm = makeSecretsStore({ [secretName("telegram_1")]: "stored-pw" });
    const client = {
      send: async (cmd) => {
        if (cmd.constructor.name === "AdminInitiateAuthCommand") {
          return { AuthenticationResult: { IdToken: "t", ExpiresIn: 3600 } };
        }
        return {};
      },
    };
    const p = createCognitoTokenProvider({ ...cfg, client, secretsClient: sm.client, log: () => {} });
    await assert.rejects(p.getAccessToken("telegram:1"), /no AccessToken/);
    assert.equal(await createCognitoTokenProvider({ ...cfg, clientId: "" }).getAccessToken("a"), null);
  });

  it("provisions a missing user with a random password stored in its own secret", async () => {
    const cognito = makeCognito();
    const sm = makeSecretsStore();
    await provider(cognito, sm).getIdToken("slack:U1");
    assert.deepEqual(cognito.names(), [
      "AdminGetUserCommand",
      "AdminCreateUserCommand",
      "AdminSetUserPasswordCommand",
      "AdminInitiateAuthCommand",
    ]);
    const stored = sm.store.get(secretName("slack_U1"));
    assert.equal(typeof stored, "string");
    assert.equal(cognito.sent[2].input.Permanent, true);
    assert.equal(cognito.sent[2].input.Password, stored, "stored password is the one set on Cognito");
    assert.equal(cognito.sent[3].input.AuthFlow, "ADMIN_USER_PASSWORD_AUTH");
    assert.equal(cognito.sent[3].input.AuthParameters.PASSWORD, stored);
    // The secret is claimed before the user is created.
    assert.equal(sm.sent.find((c) => c.constructor.name === "CreateSecretCommand").input.Name, secretName("slack_U1"));
  });

  it("generates independent passwords that satisfy the pool policy", () => {
    const seen = new Set();
    for (let i = 0; i < 200; i += 1) {
      const pw = generateRandomPassword();
      assert.ok(pw.length >= 16, "pool min_length is 16");
      assert.match(pw, /[a-z]/);
      assert.match(pw, /[A-Z]/);
      assert.match(pw, /[0-9]/);
      seen.add(pw);
    }
    assert.equal(seen.size, 200);
  });

  it("two users get different passwords", async () => {
    const cognito = makeCognito();
    const sm = makeSecretsStore();
    const p = provider(cognito, sm);
    await p.getIdToken("telegram:a");
    await p.getIdToken("telegram:b");
    assert.notEqual(sm.store.get(secretName("telegram_a")), sm.store.get(secretName("telegram_b")));
  });

  it("keeps the secret out of the agent's openclaw/user/<namespace>/ prefix", () => {
    assert.equal(defaultSecretId("telegram:42", POOL), secretName("telegram_42"));
    assert.ok(!defaultSecretId("telegram:42", POOL).startsWith("openclaw/user/"));
  });

  it("migrates an existing user that has no secret without re-enrolment", async () => {
    // Created by the previous scheme: the user exists with a password nobody
    // stored.
    const cognito = makeCognito({ users: { "telegram:legacy": "previous-derived-password" } });
    const sm = makeSecretsStore();
    const p = provider(cognito, sm);
    const entry = await p.getIdToken("telegram:legacy");
    assert.match(entry.token, /^tok/);
    assert.deepEqual(cognito.names(), [
      "AdminGetUserCommand",
      "AdminSetUserPasswordCommand",
      "AdminInitiateAuthCommand",
    ]);
    const stored = sm.store.get(secretName("telegram_legacy"));
    assert.equal(cognito.passwords.get("telegram:legacy"), stored);
    // A later process (fresh cache) authenticates with the stored value and
    // does not rotate again.
    const before = cognito.sent.length;
    await provider(cognito, sm).getIdToken("telegram:legacy");
    assert.deepEqual(cognito.names().slice(before), ["AdminGetUserCommand", "AdminInitiateAuthCommand"]);
  });

  it("two processes migrating the same user at once converge on one password", async () => {
    const cognito = makeCognito({ users: { "telegram:race": "previous-derived-password" } });
    const sm = makeSecretsStore();
    const a = provider(cognito, sm);
    const b = provider(cognito, sm);
    const [ta, tb] = await Promise.all([a.getIdToken("telegram:race"), b.getIdToken("telegram:race")]);
    assert.match(ta.token, /^tok/);
    assert.match(tb.token, /^tok/);
    assert.equal(cognito.passwords.get("telegram:race"), sm.store.get(secretName("telegram_race")));
    assert.equal(sm.sent.filter((c) => c.constructor.name === "PutSecretValueCommand").length, 0,
      "a lost claim adopts the existing secret instead of overwriting it");
  });

  it("two processes provisioning a new user at once converge on one password", async () => {
    const cognito = makeCognito();
    const sm = makeSecretsStore();
    const [ta, tb] = await Promise.all([
      provider(cognito, sm).getIdToken("slack:U2"),
      provider(cognito, sm).getIdToken("slack:U2"),
    ]);
    assert.match(ta.token, /^tok/);
    assert.match(tb.token, /^tok/);
    assert.equal(cognito.passwords.get("slack:U2"), sm.store.get(secretName("slack_U2")));
  });

  it("re-sets the stored password on Cognito when Cognito refuses it, then retries once", async () => {
    // e.g. a previous claim stored the secret but stopped before setting it.
    const cognito = makeCognito({ users: { "telegram:7": "something-else" } });
    const sm = makeSecretsStore({ [secretName("telegram_7")]: "stored-pwAa1" });
    const entry = await provider(cognito, sm).getIdToken("telegram:7");
    assert.match(entry.token, /^tok/);
    assert.equal(cognito.passwords.get("telegram:7"), "stored-pwAa1");
    assert.equal(sm.store.get(secretName("telegram_7")), "stored-pwAa1", "the secret is authoritative");
    assert.deepEqual(cognito.names(), [
      "AdminGetUserCommand",
      "AdminInitiateAuthCommand",
      "AdminSetUserPasswordCommand",
      "AdminInitiateAuthCommand",
    ]);
  });

  it("does not loop when Cognito keeps refusing (e.g. a disabled user)", async () => {
    const sm = makeSecretsStore({ [secretName("telegram_8")]: "stored-pwAa1" });
    let auths = 0;
    const client = {
      send: async (cmd) => {
        if (cmd.constructor.name === "AdminInitiateAuthCommand") {
          auths += 1;
          throw Object.assign(new Error("User is disabled."), { name: "NotAuthorizedException" });
        }
        return {};
      },
    };
    const p = createCognitoTokenProvider({ ...cfg, client, secretsClient: sm.client, log: () => {} });
    await assert.rejects(p.getIdToken("telegram:8"), /disabled/);
    assert.equal(auths, 2);
  });

  it("shares one in-flight authentication between concurrent callers in a process", async () => {
    const cognito = makeCognito();
    const sm = makeSecretsStore();
    const p = provider(cognito, sm);
    const [x, y] = await Promise.all([p.getIdToken("telegram:9"), p.getAccessToken("telegram:9")]);
    assert.equal(x.expiresAt, y.expiresAt);
    assert.equal(cognito.names().filter((n) => n === "AdminInitiateAuthCommand").length, 1);
  });
});
