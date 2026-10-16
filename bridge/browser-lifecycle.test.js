/**
 * Tests for the contract's Browser lifecycle with a mocked AWS SDK and filesystem.
 * Run: cd bridge && node --test browser-lifecycle.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf8");
const start = source.indexOf("function browserOwner()");
const end = source.indexOf("async function setupGatewayBearer(", start);
assert.ok(start >= 0 && end > start);

describe("OS entropy after snapshot restore", () => {
  const lifecycleSource = fs.readFileSync(path.join(__dirname, "runtime-lifecycle.js"), "utf8");
  function load(io) {
    const box = vm.createContext({
      Buffer, module: { exports: {} },
      require(name) {
        assert.equal(name, "node:fs"); // OpenSSL RNG must not be used.
        return io;
      },
    });
    vm.runInContext(lifecycleSource, box);
    return box.module.exports.osRandomUUID;
  }

  it("opens OS entropy per call, handles partial reads and never reads during import", () => {
    let opened = 0, closed = 0, reads = 0;
    const io = {
      openSync(file, mode) {
        assert.equal(file, "/dev/urandom");
        assert.equal(mode, "r");
        return ++opened;
      },
      readSync(fd, bytes, offset, length, position) {
        assert.equal(position, null);
        reads++;
        const count = Math.min(length, 5);
        bytes.fill(fd, offset, offset + count);
        return count;
      },
      closeSync() { closed++; },
    };
    const instances = [load(io), load(io)];
    assert.equal(opened, 0);
    assert.equal(reads, 0);
    const ids = instances.flatMap((generate) => [generate(), generate()]);
    assert.equal(new Set(ids).size, 4);
    ids.forEach((id) => assert.match(id,
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/));
    assert.equal(opened, 4);
    assert.equal(closed, 4);
    assert.equal(reads, 16);
  });

  it("fails closed on OS errors or EOF and closes every opened descriptor", () => {
    let closed = 0;
    for (const readSync of [() => 0, () => { throw new Error("read failure"); }]) {
      const generate = load({ openSync: () => 7, readSync, closeSync: () => { closed++; } });
      assert.throws(generate, /incomplete|read failure/);
    }
    assert.equal(closed, 2);
    assert.throws(load({
      openSync() { throw new Error("open failure"); },
      closeSync() { assert.fail("No descriptor was opened"); },
    }), /open failure/);
  });
});

function createBrowserFunctions({
  enabled = true, sessionId = null, sessions = [], response, startThrows, stopThrows, aborted = false,
  runtimeSessionId = "runtime-session-for-browser-tests-0001",
  service, replicatedRandomUUID,
} = {}) {
  const calls = [];
  const files = new Map();
  const intervals = new Set();
  class Start { constructor(input) { this.input = input; } }
  class Stop { constructor(input) { this.input = input; } }
  class Get { constructor(input) { this.input = input; } }
  const binding = { userId: "user1", actorId: "telegram:1", runtimeSessionId, routingGeneration: 1 };
  const box = vm.createContext({
    process: { env: { ...(enabled ? { BROWSER_IDENTIFIER: "test-browser-id" } : {}), AWS_REGION: "us-east-1",
      USER_ID: binding.actorId, INTERNAL_USER_ID: binding.userId } },
    currentBrowserSessionId: sessionId,
    currentBrowserEndpoint: sessionId ? "wss://existing" : null,
    browserGeneration: 0, browserExpiresAt: 0, browserHeaderRefreshTimer: null,
    browserStartOwner: null,
    browserResourceOrigins: new Map([...sessions, ...(sessionId ? [sessionId] : [])]
      .map((id) => [id, Object.freeze({ browserIdentifier: "test-browser-id", region: "us-east-1" })])),
    browserSessions: new Set([...sessions, ...(sessionId ? [sessionId] : [])]), browserStops: new Map(),
    BROWSER_SESSION_FILE: "/test/browser-session.json", BROWSER_SESSION_TIMEOUT_SECONDS: 3600,
    activationScope: { generation: "activation", signal: new AbortController().signal,
      checkpoint() { if (aborted) throw new Error("Activation aborted"); } },
    v2Lifecycle: { snapshot: () => ({ binding, generation: "activation",
      resources: ["credentials", "restore", "skills"] }) },
    currentUserId: binding.userId, currentNamespace: "telegram_1", shuttingDown: false,
    workspaceSync: { assertRestoreReady() {} }, TURN_TIMEOUT_MS: 620000,
    turnValidityMs: (deadline) => Math.max(620000, deadline - Date.now()) + 60000,
    createHash: require("node:crypto").createHash,
    osRandomUUID: require("./runtime-lifecycle").osRandomUUID,
    randomUUID: replicatedRandomUUID,
    signBrowserEndpoint: async () => ({ Authorization: "signed" }),
    fs: { writeFileSync: (name, value) => files.set(name, value) },
    AbortSignal, console: { log() {}, error() {} },
    setInterval(callback) { intervals.add(callback); return callback; },
    clearInterval(callback) { intervals.delete(callback); },
    require(name) {
      assert.equal(name, "@aws-sdk/client-bedrock-agentcore");
      return {
        StartBrowserSessionCommand: Start, StopBrowserSessionCommand: Stop, GetBrowserSessionCommand: Get,
        BedrockAgentCoreClient: class {
          async send(command) {
            calls.push(command);
            if (command instanceof Start) {
              if (startThrows) throw startThrows;
              if (service) return service.start(command.input);
              return response || {
                sessionId: "mock-session", streams: { automationStream: { streamEndpoint: "wss://mock" } },
              };
            }
            if (command instanceof Get) return service.get(command.input);
            assert.ok(command instanceof Stop);
            if (stopThrows) throw stopThrows;
            if (service) return service.stop(command.input);
            return {};
          }
          destroy() {}
        },
      };
    },
  });
  vm.runInContext(source.slice(start, end), box);
  return {
    initBrowserSession: box.initBrowserSession, stopBrowserSessions: box.stopBrowserSessions,
    browserStartClientToken: box.browserStartClientToken,
    box, calls, files, intervals,
    stops: () => calls.filter((command) => command instanceof Stop).map((command) => command.input.sessionId),
  };
}

describe("initBrowserSession", () => {
  it("skips when BROWSER_IDENTIFIER is not configured", async () => {
    const app = createBrowserFunctions({ enabled: false });
    await app.initBrowserSession("user1");
    assert.equal(app.box.currentBrowserSessionId, null);
    assert.equal(app.calls.length, 0);
  });

  it("keeps the session already owned by this AgentCore Runtime", async () => {
    const app = createBrowserFunctions({ sessionId: "existing-session" });
    await app.initBrowserSession("user1");
    assert.equal(app.box.currentBrowserSessionId, "existing-session");
    assert.equal(app.calls.length, 0);
  });

  it("rejects cancelled activation before starting a session", async () => {
    const app = createBrowserFunctions({ aborted: true });
    await assert.rejects(app.initBrowserSession("user1"), /Activation aborted/);
    assert.equal(app.calls.length, 0);
  });

  it("rejects SDK errors and leaves ownership unset", async () => {
    const app = createBrowserFunctions({ startThrows: new Error("AccessDenied") });
    await assert.rejects(app.initBrowserSession("user1"), /AccessDenied/);
    assert.equal(app.box.currentBrowserSessionId, null);
    assert.equal(app.intervals.size, 0);
  });

  it("records the acquired session and its endpoint", async () => {
    const app = createBrowserFunctions();
    await app.initBrowserSession("user1");
    assert.equal(app.box.currentBrowserSessionId, "mock-session");
    assert.equal(app.box.currentBrowserEndpoint, "wss://mock");
    assert.ok(app.box.browserSessions.has("mock-session"));
    assert.equal(app.intervals.size, 1);
  });

  it("writes signed connection headers for skill processes", async () => {
    const app = createBrowserFunctions();
    await app.initBrowserSession("user1");
    assert.deepEqual(JSON.parse(app.files.get("/test/browser-session.json")), {
      sessionId: "mock-session", endpoint: "wss://mock", headers: { Authorization: "signed" },
    });
  });

  it("stops an acquired session and rejects a missing automation endpoint", async () => {
    const app = createBrowserFunctions({ response: { sessionId: "no-endpoint", streams: {} } });
    await assert.rejects(app.initBrowserSession("user1"), /No automation stream endpoint/);
    assert.deepEqual(app.stops(), ["no-endpoint"]);
    assert.equal(app.box.currentBrowserSessionId, null);
    assert.equal(app.box.browserSessions.size, 0);
    assert.equal(app.files.size, 0);
  });
});

describe("Browser start clientToken", () => {
  it("uses fresh entropy for every acquisition even when binding and generation repeat", () => {
    const { browserStartClientToken: token } = createBrowserFunctions();
    const seed = ["user_1", "runtime-session-1", "browser-1", 1];
    const first = token(...seed);
    assert.match(first, /^[0-9a-f]{64}$/);
    assert.notEqual(token(...seed), first);
    for (const [index, value] of [[0, "user-1"], [1, "runtime-session-2"], [2, "browser-2"], [3, 2]]) {
      const changed = [...seed];
      changed[index] = value;
      assert.notEqual(token(...changed), first);
    }
    assert.throws(() => token("user", undefined, "browser", 1), /runtime session ID/);
    assert.throws(() => token("user", "session", "browser", Number.MAX_SAFE_INTEGER + 1), /generation/);
  });

  it("passes distinct explicit tokens to SDK commands in independently restored bindings", async () => {
    const a = createBrowserFunctions({ runtimeSessionId: "session-a" });
    const b = createBrowserFunctions({ runtimeSessionId: "session-b" });
    await a.initBrowserSession("same-user");
    await b.initBrowserSession("same-user");
    const input = a.calls[0].input;
    assert.match(input.clientToken, /^[0-9a-f]{64}$/);
    assert.notEqual(input.clientToken, b.calls[0].input.clientToken);
    assert.notEqual(a.browserStartClientToken("same-user", "session-a", "test-browser-id", 1), input.clientToken);
    assert.equal(input.sessionTimeoutSeconds, 3600);
    assert.equal(input.name, "same-user");
    assert.equal(input.viewPort.width, 1280);
    assert.equal(input.viewPort.height, 720);
  });

  it("does not replay retained Browser sessions across instances with replicated crypto state", async () => {
    for (const overlap of [false, true]) {
      for (const previousStatus of ["TERMINATED", "READY"]) {
        const tokens = new Map();
        const sessions = new Map();
        let cryptoCalls = 0;
        const replicatedRandomUUID = () => {
          cryptoCalls++;
          return "11111111-1111-4111-8111-111111111111";
        };
        const service = {
          start(input) {
            if (tokens.has(input.clientToken)) {
              const previous = sessions.get(tokens.get(input.clientToken));
              if (previous.status === "TERMINATED") throw new Error("Idempotent session is TERMINATED");
              return previous;
            }
            const sessionId = `session-${sessions.size + 1}`;
            const result = { sessionId, browserIdentifier: input.browserIdentifier, status: "READY",
              expiresAt: Date.now() + 3600000,
              streams: { automationStream: { streamEndpoint: `wss://${sessionId}` } } };
            tokens.set(input.clientToken, sessionId);
            sessions.set(sessionId, result);
            return result;
          },
          get(input) { return sessions.get(input.sessionId); },
          stop(input) { sessions.get(input.sessionId).status = "TERMINATED"; return {}; },
        };
        for (let instance = 0; instance < 3; instance++) {
          const app = createBrowserFunctions({ service, replicatedRandomUUID });
          assert.equal(app.box.browserGeneration, 0);
          if (overlap) {
            const owner = app.box.startBrowserOverlap("user1");
            assert.equal(app.box.startBrowserOverlap("user1"), owner);
            owner.gatewayActivationPassed = true;
            await app.initBrowserSession("user1", owner);
          } else {
            await app.initBrowserSession("user1");
          }
          assert.equal(app.box.currentBrowserSessionId, `session-${instance + 1}`);
          await app.box.ensureV2BrowserSession("user1", Date.now() + 10000);
          assert.equal(tokens.size, instance + 1);
          assert.equal(sessions.size, instance + 1);
          const retained = sessions.get(app.box.currentBrowserSessionId);
          retained.status = previousStatus;
          retained.expiresAt = Date.now() + 1000;
        }
        assert.equal(cryptoCalls, 0);
      }
    }
  });

  it("uses another token for replacement in the same AgentCore Runtime session", async () => {
    const app = createBrowserFunctions();
    await app.initBrowserSession("user1");
    const first = app.calls[0].input.clientToken;
    await app.stopBrowserSessions();
    await app.initBrowserSession("user1");
    assert.notEqual(app.calls.at(-1).input.clientToken, first);
  });

  it("keeps the explicit token when the installed SDK retries one request", async () => {
    const { BedrockAgentCoreClient, StartBrowserSessionCommand } =
      require("@aws-sdk/client-bedrock-agentcore");
    const app = createBrowserFunctions();
    await app.initBrowserSession("user1");
    const input = app.calls[0].input;
    const bodies = [];
    const client = new BedrockAgentCoreClient({
      region: "us-west-2", credentials: { accessKeyId: "test", secretAccessKey: "test" },
      maxAttempts: 2,
      requestHandler: {
        async handle(request) {
          assert.equal(request.method, "PUT");
          bodies.push(JSON.parse(request.body));
          return { response: { statusCode: bodies.length === 1 ? 503 : 200, headers: {},
            body: Buffer.from(JSON.stringify(bodies.length === 1
              ? { message: "fixture retry" }
              : { browserIdentifier: "test-browser-id", sessionId: "mock-session" })) } };
        },
      },
    });
    try {
      const result = await client.send(new StartBrowserSessionCommand(input));
      assert.equal(result.$metadata.attempts, 2);
      assert.equal(bodies.length, 2);
      assert.deepEqual(bodies[0], bodies[1]);
      assert.equal(bodies[0].clientToken, input.clientToken);
    } finally {
      client.destroy();
    }
  });

  it("rejects missing or malformed session binding before the Browser API", async () => {
    for (const runtimeSessionId of [null, "", " ", 42, "s".repeat(257)]) {
      const app = createBrowserFunctions({ runtimeSessionId });
      await assert.rejects(app.initBrowserSession("user1"), /runtime session ID/);
      assert.equal(app.calls.length, 0);
    }
    const disabled = createBrowserFunctions({ enabled: false, runtimeSessionId: null });
    await disabled.initBrowserSession("user1");
    assert.equal(disabled.calls.length, 0);
  });
});

describe("stopBrowserSessions", () => {
  it("handles an empty set of owned sessions", async () => {
    const app = createBrowserFunctions();
    await app.stopBrowserSessions();
    assert.equal(app.calls.length, 0);
  });

  it("skips when BROWSER_IDENTIFIER is not configured", async () => {
    const app = createBrowserFunctions({ enabled: false, sessions: ["session-1"] });
    await app.stopBrowserSessions();
    assert.equal(app.calls.length, 0);
  });

  it("stops all acquired sessions and retires header refresh", async () => {
    const app = createBrowserFunctions({ sessions: ["old-session"] });
    await app.initBrowserSession("user1");
    await app.stopBrowserSessions();
    assert.deepEqual(app.stops().sort(), ["mock-session", "old-session"]);
    assert.equal(app.box.currentBrowserSessionId, null);
    assert.equal(app.box.browserSessions.size, 0);
    assert.equal(app.intervals.size, 0);
  });

  it("reports stop failures and retains the session for cleanup retry", async () => {
    const app = createBrowserFunctions({ sessionId: "session-1", stopThrows: new Error("NetworkError") });
    await assert.rejects(app.stopBrowserSessions(), /NetworkError/);
    assert.equal(app.box.currentBrowserSessionId, null);
    assert.ok(app.box.browserSessions.has("session-1"));
    assert.equal(app.box.browserStops.size, 0);
  });
});
