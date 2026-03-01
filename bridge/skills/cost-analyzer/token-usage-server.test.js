/**
 * Tests for token-usage-server.js — MCP server for DynamoDB token usage queries.
 *
 * Tests cover:
 *   1. Pure utility functions (mirrored from implementation)
 *   2. MCP protocol compliance (initialize, tools/list, error handling)
 *
 * Run: node --test token-usage-server.test.js
 */

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { spawn } = require("child_process");
const path = require("path");

const SERVER_PATH = path.join(__dirname, "token-usage-server.js");

// ── Pure function tests (mirrored from token-usage-server.js) ────────────

describe("namespaceToActorId", () => {
  // Mirror of the function in token-usage-server.js
  function namespaceToActorId(namespace) {
    const idx = namespace.indexOf("_");
    if (idx === -1) return namespace;
    return namespace.substring(0, idx) + ":" + namespace.substring(idx + 1);
  }

  it("converts telegram namespace to actor ID", () => {
    assert.equal(namespaceToActorId("telegram_12345"), "telegram:12345");
  });

  it("converts slack namespace to actor ID", () => {
    assert.equal(namespaceToActorId("slack_U12345ABC"), "slack:U12345ABC");
  });

  it("handles namespace with multiple underscores (only first split)", () => {
    assert.equal(
      namespaceToActorId("telegram_user_name_123"),
      "telegram:user_name_123",
    );
  });

  it("returns unchanged if no underscore", () => {
    assert.equal(namespaceToActorId("nounderscore"), "nounderscore");
  });

  it("handles discord namespace", () => {
    assert.equal(
      namespaceToActorId("discord_123456789012345"),
      "discord:123456789012345",
    );
  });

  it("handles whatsapp namespace", () => {
    assert.equal(
      namespaceToActorId("whatsapp_15551234567"),
      "whatsapp:15551234567",
    );
  });
});

describe("getDateRange", () => {
  // Mirror of the function in token-usage-server.js
  function getDateRange(days) {
    const dates = [];
    const now = new Date();
    for (let i = 0; i < days; i++) {
      const d = new Date(now);
      d.setDate(d.getDate() - i);
      dates.push(d.toISOString().slice(0, 10));
    }
    return dates;
  }

  it("returns correct number of dates for 7 days", () => {
    assert.equal(getDateRange(7).length, 7);
  });

  it("returns correct number of dates for 1 day", () => {
    assert.equal(getDateRange(1).length, 1);
  });

  it("returns correct number of dates for 30 days", () => {
    assert.equal(getDateRange(30).length, 30);
  });

  it("starts with today", () => {
    const dates = getDateRange(1);
    const today = new Date().toISOString().slice(0, 10);
    assert.equal(dates[0], today);
  });

  it("dates are in yyyy-mm-dd format", () => {
    const dates = getDateRange(5);
    for (const d of dates) {
      assert.ok(/^\d{4}-\d{2}-\d{2}$/.test(d), `${d} is not yyyy-mm-dd`);
    }
  });

  it("dates are in descending order (newest first)", () => {
    const dates = getDateRange(10);
    for (let i = 0; i < dates.length - 1; i++) {
      assert.ok(
        dates[i] >= dates[i + 1],
        `${dates[i]} should be >= ${dates[i + 1]}`,
      );
    }
  });

  it("returns empty array for 0 days", () => {
    assert.equal(getDateRange(0).length, 0);
  });
});

// ── MCP Protocol tests (spawn server, test JSON-RPC messages) ────────────

/**
 * Spawn the token-usage-server as a child process with test env vars.
 */
function createServerProcess() {
  return spawn("node", [SERVER_PATH], {
    env: {
      ...process.env,
      AWS_REGION: "us-east-1",
      TOKEN_USAGE_TABLE_NAME: "test-token-usage-table",
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
}

/**
 * Send a JSON-RPC message to the server and wait for a response with matching id.
 */
function sendAndReceive(proc, message, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    let buf = "";
    const timer = setTimeout(() => {
      proc.stdout.removeListener("data", onData);
      reject(new Error(`Timeout after ${timeoutMs}ms waiting for response`));
    }, timeoutMs);

    const onData = (chunk) => {
      buf += chunk.toString();
      const lines = buf.split("\n");
      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const parsed = JSON.parse(line);
          if (parsed.id === message.id) {
            clearTimeout(timer);
            proc.stdout.removeListener("data", onData);
            resolve(parsed);
            return;
          }
        } catch {
          // Not valid JSON yet, keep reading
        }
      }
    };

    proc.stdout.on("data", onData);
    proc.stdin.write(JSON.stringify(message) + "\n");
  });
}

describe("MCP Protocol — initialize", () => {
  it("responds to initialize with server info", async () => {
    const proc = createServerProcess();
    try {
      const resp = await sendAndReceive(proc, {
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: {
          protocolVersion: "2024-11-05",
          capabilities: {},
          clientInfo: { name: "test-client", version: "1.0.0" },
        },
      });

      assert.equal(resp.jsonrpc, "2.0");
      assert.equal(resp.id, 1);
      assert.ok(resp.result, "Expected result in response");
      assert.equal(resp.result.serverInfo.name, "token-usage");
      assert.equal(resp.result.serverInfo.version, "1.0.0");
      assert.ok(resp.result.capabilities.tools);
      assert.equal(resp.result.protocolVersion, "2024-11-05");
    } finally {
      proc.kill();
    }
  });
});

describe("MCP Protocol — tools/list", () => {
  it("lists exactly 3 tools", async () => {
    const proc = createServerProcess();
    try {
      // Initialize first
      await sendAndReceive(proc, {
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: {
          protocolVersion: "2024-11-05",
          capabilities: {},
          clientInfo: { name: "test", version: "1.0.0" },
        },
      });

      const resp = await sendAndReceive(proc, {
        jsonrpc: "2.0",
        id: 2,
        method: "tools/list",
        params: {},
      });

      assert.equal(resp.id, 2);
      assert.ok(resp.result, "Expected result in response");
      assert.equal(resp.result.tools.length, 3);
    } finally {
      proc.kill();
    }
  });

  it("lists the correct tool names", async () => {
    const proc = createServerProcess();
    try {
      await sendAndReceive(proc, {
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: {
          protocolVersion: "2024-11-05",
          capabilities: {},
          clientInfo: { name: "test", version: "1.0.0" },
        },
      });

      const resp = await sendAndReceive(proc, {
        jsonrpc: "2.0",
        id: 2,
        method: "tools/list",
        params: {},
      });

      const names = resp.result.tools.map((t) => t.name).sort();
      assert.deepEqual(names, [
        "query_daily_totals",
        "query_top_users",
        "query_user_usage",
      ]);
    } finally {
      proc.kill();
    }
  });

  it("each tool has description and inputSchema", async () => {
    const proc = createServerProcess();
    try {
      await sendAndReceive(proc, {
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: {
          protocolVersion: "2024-11-05",
          capabilities: {},
          clientInfo: { name: "test", version: "1.0.0" },
        },
      });

      const resp = await sendAndReceive(proc, {
        jsonrpc: "2.0",
        id: 2,
        method: "tools/list",
        params: {},
      });

      for (const tool of resp.result.tools) {
        assert.ok(tool.description, `${tool.name} missing description`);
        assert.ok(tool.inputSchema, `${tool.name} missing inputSchema`);
        assert.equal(
          tool.inputSchema.type,
          "object",
          `${tool.name} schema type should be object`,
        );
      }
    } finally {
      proc.kill();
    }
  });

  it("query_user_usage requires user_id parameter", async () => {
    const proc = createServerProcess();
    try {
      await sendAndReceive(proc, {
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: {
          protocolVersion: "2024-11-05",
          capabilities: {},
          clientInfo: { name: "test", version: "1.0.0" },
        },
      });

      const resp = await sendAndReceive(proc, {
        jsonrpc: "2.0",
        id: 2,
        method: "tools/list",
        params: {},
      });

      const tool = resp.result.tools.find((t) => t.name === "query_user_usage");
      assert.ok(tool);
      assert.deepEqual(tool.inputSchema.required, ["user_id"]);
      assert.ok(tool.inputSchema.properties.user_id);
      assert.ok(tool.inputSchema.properties.days);
    } finally {
      proc.kill();
    }
  });

  it("query_top_users requires date parameter", async () => {
    const proc = createServerProcess();
    try {
      await sendAndReceive(proc, {
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: {
          protocolVersion: "2024-11-05",
          capabilities: {},
          clientInfo: { name: "test", version: "1.0.0" },
        },
      });

      const resp = await sendAndReceive(proc, {
        jsonrpc: "2.0",
        id: 2,
        method: "tools/list",
        params: {},
      });

      const tool = resp.result.tools.find((t) => t.name === "query_top_users");
      assert.ok(tool);
      assert.deepEqual(tool.inputSchema.required, ["date"]);
      assert.ok(tool.inputSchema.properties.date);
    } finally {
      proc.kill();
    }
  });

  it("query_daily_totals has optional days parameter", async () => {
    const proc = createServerProcess();
    try {
      await sendAndReceive(proc, {
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: {
          protocolVersion: "2024-11-05",
          capabilities: {},
          clientInfo: { name: "test", version: "1.0.0" },
        },
      });

      const resp = await sendAndReceive(proc, {
        jsonrpc: "2.0",
        id: 2,
        method: "tools/list",
        params: {},
      });

      const tool = resp.result.tools.find(
        (t) => t.name === "query_daily_totals",
      );
      assert.ok(tool);
      assert.ok(tool.inputSchema.properties.days);
      // days is not required
      assert.ok(
        !tool.inputSchema.required || !tool.inputSchema.required.includes("days"),
      );
    } finally {
      proc.kill();
    }
  });
});

describe("MCP Protocol — error handling", () => {
  it("returns error for unknown tool via tools/call", async () => {
    const proc = createServerProcess();
    try {
      await sendAndReceive(proc, {
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: {
          protocolVersion: "2024-11-05",
          capabilities: {},
          clientInfo: { name: "test", version: "1.0.0" },
        },
      });

      const resp = await sendAndReceive(proc, {
        jsonrpc: "2.0",
        id: 2,
        method: "tools/call",
        params: { name: "nonexistent_tool", arguments: {} },
      });

      assert.equal(resp.id, 2);
      assert.ok(resp.error);
      assert.ok(resp.error.message.includes("Unknown tool"));
    } finally {
      proc.kill();
    }
  });

  it("returns error for unknown JSON-RPC method", async () => {
    const proc = createServerProcess();
    try {
      const resp = await sendAndReceive(proc, {
        jsonrpc: "2.0",
        id: 1,
        method: "nonexistent/method",
        params: {},
      });

      assert.equal(resp.id, 1);
      assert.ok(resp.error);
      assert.ok(resp.error.message.includes("Method not found"));
    } finally {
      proc.kill();
    }
  });

  it("silently ignores notifications (no id)", async () => {
    const proc = createServerProcess();
    try {
      // Send a notification (no id) — should not crash or produce a response
      proc.stdin.write(
        JSON.stringify({
          jsonrpc: "2.0",
          method: "notifications/initialized",
        }) + "\n",
      );

      // Then send a real request — server should still be alive
      const resp = await sendAndReceive(proc, {
        jsonrpc: "2.0",
        id: 99,
        method: "initialize",
        params: {
          protocolVersion: "2024-11-05",
          capabilities: {},
          clientInfo: { name: "test", version: "1.0.0" },
        },
      });

      assert.equal(resp.id, 99);
      assert.ok(resp.result);
    } finally {
      proc.kill();
    }
  });
});
