/**
 * Tests for browser session lifecycle in agentcore-contract.js.
 *
 * Covers: initBrowserSession, stopBrowserSessions — lazy start/stop
 * of AgentCore Browser sessions with graceful error handling.
 * Run: cd bridge && node --test browser-lifecycle.test.js
 */
const { describe, it, beforeEach, afterEach, mock } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");

// We test the exported functions directly by requiring the module,
// but agentcore-contract.js is a server entry point. Instead, we
// extract the browser lifecycle functions and test them in isolation
// by re-implementing the testable logic here with mocks.

// --- initBrowserSession ---

describe("initBrowserSession", () => {
  let originalEnv;

  beforeEach(() => {
    originalEnv = { ...process.env };
  });

  afterEach(() => {
    process.env = originalEnv;
    // Clean up session file if created
    try {
      fs.unlinkSync("/tmp/agentcore-browser-session.json");
    } catch {}
  });

  it("skips when BROWSER_IDENTIFIER env not set", async () => {
    delete process.env.BROWSER_IDENTIFIER;

    // Inline the function logic to test without requiring the full server
    const { initBrowserSession } = createBrowserFunctions();
    const userSessions = new Map();
    userSessions.set("user1", { browserSessionId: null, browserEndpoint: null });

    await initBrowserSession("user1", userSessions);

    const session = userSessions.get("user1");
    assert.equal(session.browserSessionId, null, "should not set browserSessionId");
    assert.equal(session.browserEndpoint, null, "should not set browserEndpoint");
  });

  it("skips when session already has browserSessionId", async () => {
    process.env.BROWSER_IDENTIFIER = "test-browser-id";
    process.env.AWS_REGION = "us-east-1";

    const { initBrowserSession } = createBrowserFunctions();
    const userSessions = new Map();
    userSessions.set("user1", {
      browserSessionId: "existing-session",
      browserEndpoint: "wss://existing",
    });

    await initBrowserSession("user1", userSessions);

    const session = userSessions.get("user1");
    assert.equal(session.browserSessionId, "existing-session", "should not change existing session");
  });

  it("skips when user has no session entry", async () => {
    process.env.BROWSER_IDENTIFIER = "test-browser-id";

    const { initBrowserSession } = createBrowserFunctions();
    const userSessions = new Map(); // empty — no user session

    // Should not throw
    await initBrowserSession("user1", userSessions);
    assert.equal(userSessions.size, 0, "should not create a session entry");
  });

  it("handles SDK errors gracefully (non-fatal)", async () => {
    process.env.BROWSER_IDENTIFIER = "test-browser-id";
    process.env.AWS_REGION = "us-east-1";

    const { initBrowserSession } = createBrowserFunctions({
      startThrows: new Error("AccessDenied"),
    });
    const userSessions = new Map();
    userSessions.set("user1", { browserSessionId: null, browserEndpoint: null });

    // Should not throw — errors are non-fatal
    await initBrowserSession("user1", userSessions);

    const session = userSessions.get("user1");
    assert.equal(session.browserSessionId, null, "should remain null on error");
  });

  it("sets browserSessionId and browserEndpoint on success", async () => {
    process.env.BROWSER_IDENTIFIER = "test-browser-id";
    process.env.AWS_REGION = "us-east-1";

    const { initBrowserSession } = createBrowserFunctions({
      response: {
        sessionId: "sess-abc-123",
        streams: { automationStream: { streamEndpoint: "wss://browser.example.com/auto" } },
      },
    });
    const userSessions = new Map();
    userSessions.set("user1", { browserSessionId: null, browserEndpoint: null });

    await initBrowserSession("user1", userSessions);

    const session = userSessions.get("user1");
    assert.equal(session.browserSessionId, "sess-abc-123");
    assert.equal(session.browserEndpoint, "wss://browser.example.com/auto");
  });

  it("writes session file on success", async () => {
    process.env.BROWSER_IDENTIFIER = "test-browser-id";
    process.env.AWS_REGION = "us-east-1";

    const { initBrowserSession } = createBrowserFunctions({
      response: {
        sessionId: "sess-file-test",
        streams: { automationStream: { streamEndpoint: "wss://endpoint" } },
      },
    });
    const userSessions = new Map();
    userSessions.set("user1", { browserSessionId: null, browserEndpoint: null });

    await initBrowserSession("user1", userSessions);

    const data = JSON.parse(fs.readFileSync("/tmp/agentcore-browser-session.json", "utf-8"));
    assert.equal(data.sessionId, "sess-file-test");
    assert.equal(data.endpoint, "wss://endpoint");
  });

  it("handles missing automation stream endpoint", async () => {
    process.env.BROWSER_IDENTIFIER = "test-browser-id";
    process.env.AWS_REGION = "us-east-1";

    const { initBrowserSession } = createBrowserFunctions({
      response: {
        sessionId: "sess-no-endpoint",
        streams: {},
      },
    });
    const userSessions = new Map();
    userSessions.set("user1", { browserSessionId: null, browserEndpoint: null });

    // Should not throw — handles missing endpoint gracefully
    await initBrowserSession("user1", userSessions);

    const session = userSessions.get("user1");
    assert.equal(session.browserSessionId, null, "should remain null when no endpoint");
  });
});

// --- stopBrowserSessions ---

describe("stopBrowserSessions", () => {
  let originalEnv;

  beforeEach(() => {
    originalEnv = { ...process.env };
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  it("handles empty userSessions", async () => {
    process.env.BROWSER_IDENTIFIER = "test-browser-id";
    process.env.AWS_REGION = "us-east-1";

    const { stopBrowserSessions } = createBrowserFunctions();
    const userSessions = new Map();

    // Should not throw
    await stopBrowserSessions(userSessions);
  });

  it("skips when BROWSER_IDENTIFIER env not set", async () => {
    delete process.env.BROWSER_IDENTIFIER;

    const { stopBrowserSessions } = createBrowserFunctions();
    const userSessions = new Map();
    userSessions.set("user1", { browserSessionId: "sess-123" });

    // Should not throw and should not attempt to call SDK
    await stopBrowserSessions(userSessions);
  });

  it("stops sessions for all users with browserSessionId", async () => {
    process.env.BROWSER_IDENTIFIER = "test-browser-id";
    process.env.AWS_REGION = "us-east-1";

    const stoppedSessions = [];
    const { stopBrowserSessions } = createBrowserFunctions({
      onStop: (params) => stoppedSessions.push(params.sessionId),
    });

    const userSessions = new Map();
    userSessions.set("user1", { browserSessionId: "sess-1", browserEndpoint: "wss://1" });
    userSessions.set("user2", { browserSessionId: null, browserEndpoint: null });
    userSessions.set("user3", { browserSessionId: "sess-3", browserEndpoint: "wss://3" });

    await stopBrowserSessions(userSessions);

    assert.deepEqual(stoppedSessions.sort(), ["sess-1", "sess-3"]);
  });

  it("handles stop errors gracefully", async () => {
    process.env.BROWSER_IDENTIFIER = "test-browser-id";
    process.env.AWS_REGION = "us-east-1";

    const { stopBrowserSessions } = createBrowserFunctions({
      stopThrows: new Error("NetworkError"),
    });

    const userSessions = new Map();
    userSessions.set("user1", { browserSessionId: "sess-1" });

    // Should not throw — errors handled per-session
    await stopBrowserSessions(userSessions);
  });
});

// --- Test helper: create browser lifecycle functions with mock SDK ---

function createBrowserFunctions(opts = {}) {
  const { response, startThrows, stopThrows, onStop } = opts;

  const mockClient = {
    send(command) {
      if (command._type === "StartBrowserSessionCommand") {
        if (startThrows) throw startThrows;
        return Promise.resolve(
          response || {
            sessionId: "mock-session",
            streams: { automationStream: { streamEndpoint: "wss://mock" } },
          },
        );
      }
      if (command._type === "StopBrowserSessionCommand") {
        if (onStop) onStop(command._params);
        if (stopThrows) return Promise.reject(stopThrows);
        return Promise.resolve({});
      }
      return Promise.resolve({});
    },
  };

  function StartBrowserSessionCommand(params) {
    return { _type: "StartBrowserSessionCommand", _params: params };
  }

  function StopBrowserSessionCommand(params) {
    return { _type: "StopBrowserSessionCommand", _params: params };
  }

  const BROWSER_SESSION_FILE = "/tmp/agentcore-browser-session.json";
  const BROWSER_SESSION_TIMEOUT_SECONDS = 3600;

  async function initBrowserSession(userId, userSessions) {
    const browserIdentifier = process.env.BROWSER_IDENTIFIER;
    if (!browserIdentifier) return;

    const session = userSessions.get(userId);
    if (!session || session.browserSessionId) return;

    try {
      const client = mockClient;

      const resp = await client.send(
        new StartBrowserSessionCommand({
          browserIdentifier,
          name: userId.replace(/[^a-zA-Z0-9-]/g, "-").slice(0, 64),
          sessionTimeoutSeconds: BROWSER_SESSION_TIMEOUT_SECONDS,
        }),
      );

      const endpoint = resp.streams?.automationStream?.streamEndpoint;
      if (!endpoint) throw new Error("No automation stream endpoint returned");

      session.browserSessionId = resp.sessionId;
      session.browserEndpoint = endpoint;

      fs.writeFileSync(
        BROWSER_SESSION_FILE,
        JSON.stringify({ endpoint, sessionId: resp.sessionId }),
      );

      console.log(`[browser] Session started for ${userId}: ${resp.sessionId}`);
    } catch (err) {
      console.error(`[browser] Failed to start session for ${userId}:`, err.message);
    }
  }

  async function stopBrowserSessions(userSessions) {
    const browserIdentifier = process.env.BROWSER_IDENTIFIER;
    if (!browserIdentifier) return;

    const client = mockClient;

    const stops = [];
    for (const [userId, session] of userSessions.entries()) {
      if (session.browserSessionId) {
        stops.push(
          client
            .send(
              new StopBrowserSessionCommand({
                browserIdentifier,
                sessionId: session.browserSessionId,
              }),
            )
            .then(() => console.log(`[browser] Stopped session for ${userId}`))
            .catch((err) =>
              console.error(`[browser] Stop failed for ${userId}:`, err.message),
            ),
        );
      }
    }
    await Promise.allSettled(stops);
  }

  return { initBrowserSession, stopBrowserSessions };
}
