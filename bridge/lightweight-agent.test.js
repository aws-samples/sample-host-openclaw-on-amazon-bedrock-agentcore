"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { PROFILE_ADDITIONS } = require("./runtime-policy");

let agentModule = null;
try {
  agentModule = require("./lightweight-agent");
} catch {
  // The rewritten safe shim is introduced after these contracts fail.
}

const EXPECTED_LIGHTWEIGHT_TOOLS = [
  "web_search",
  "web_fetch",
  "po_file_list",
  "po_file_read",
  "po_file_write",
  "po_file_delete",
];

describe("lightweight tool boundary", () => {
  it("exports the safe lightweight runtime", () => {
    assert.ok(agentModule);
  });

  it("exposes only safe web and repository workspace capabilities", () => {
    assert.deepEqual(agentModule.LIGHTWEIGHT_TOOL_NAMES, PROFILE_ADDITIONS);
    assert.deepEqual(
      agentModule.TOOLS.map((tool) => tool.function.name),
      EXPECTED_LIGHTWEIGHT_TOOLS,
    );
    for (const tool of agentModule.TOOLS) {
      assert.equal(tool.type, "function");
      assert.equal(tool.function.parameters.additionalProperties, false);
      const properties = tool.function.parameters.properties;
      assert.equal("userId" in properties, false);
      assert.equal("namespace" in properties, false);
      assert.equal("prefix" in properties, false);
    }
  });

  it("contains no executable skill or child-process path", () => {
    const source = fs.readFileSync(
      path.join(__dirname, "lightweight-agent.js"),
      "utf8",
    );

    assert.doesNotMatch(source, /child_process/);
    assert.doesNotMatch(source, /execFile|spawn\s*\(/);
    assert.doesNotMatch(source, /\/skills\//);
    assert.doesNotMatch(source, /clawhub|api.?key|secretsmanager|eventbridge/i);
  });

  it("does not promise forbidden capabilities in its system prompt", () => {
    assert.doesNotMatch(
      agentModule.SYSTEM_PROMPT,
      /scheduling|cron|clawhub|skill management|api.?key|sub-?agents?|browser|shell|exec/i,
    );
    assert.match(agentModule.SYSTEM_PROMPT, /web search/i);
    assert.match(agentModule.SYSTEM_PROMPT, /persistent workspace/i);
  });
});

describe("lightweight workspace execution", () => {
  it("dispatches file calls to an injected store without caller identity", async () => {
    const calls = [];
    const workspaceStore = {
      list: async () => {
        calls.push(["list"]);
        return [{ path: "notes.md", size: 2 }];
      },
      read: async (filePath) => {
        calls.push(["read", filePath]);
        return "hi";
      },
      write: async (filePath, content) => {
        calls.push(["write", filePath, content]);
        return { path: filePath, bytes: Buffer.byteLength(content) };
      },
      delete: async (filePath) => {
        calls.push(["delete", filePath]);
        return { path: filePath, deleted: true };
      },
    };
    const executeTool = agentModule.createToolExecutor({
      workspaceStore,
      webFetch: async () => "web-fetch",
      webSearch: async () => "web-search",
    });

    assert.equal(await executeTool("po_file_read", { path: "notes.md" }), "hi");
    assert.match(
      await executeTool("po_file_list", { userId: "victim" }),
      /notes\.md/,
    );
    assert.match(
      await executeTool("po_file_write", {
        path: "todo.md",
        content: "one",
        namespace: "victim",
      }),
      /todo\.md/,
    );
    assert.match(
      await executeTool("po_file_delete", {
        path: "old.md",
        prefix: "victim",
      }),
      /old\.md/,
    );
    assert.deepEqual(calls, [
      ["read", "notes.md"],
      ["list"],
      ["write", "todo.md", "one"],
      ["delete", "old.md"],
    ]);
  });

  it("dispatches only the two in-process web operations", async () => {
    const executeTool = agentModule.createToolExecutor({
      workspaceStore: {},
      webFetch: async (url) => `FETCH:${url}`,
      webSearch: async (query) => `SEARCH:${query}`,
    });

    assert.equal(
      await executeTool("web_fetch", { url: "https://example.com" }),
      "FETCH:https://example.com",
    );
    assert.equal(
      await executeTool("web_search", { query: "Tallinn" }),
      "SEARCH:Tallinn",
    );
    assert.equal(
      await executeTool("exec", { command: "id" }),
      "Error: Unknown tool 'exec'",
    );
  });
});

describe("web content helpers", () => {
  it("strips scripts, tags, and common entities", () => {
    assert.equal(
      agentModule.stripHtml(
        "<script>alert(1)</script><p>Hello &amp; <b>Tallinn</b></p>",
      ),
      "Hello & Tallinn",
    );
  });

  it("parses deterministic DuckDuckGo HTML results", () => {
    const html = [
      '<a class="result__a" href="https://example.com/a">A result</a>',
      '<a class="result__snippet">First</a>',
      '<a class="result__a" href="https://example.com/b">B result</a>',
      '<a class="result__snippet">Second</a>',
    ].join("");

    assert.equal(
      agentModule.parseSearchResults(html),
      "1. A result\n   https://example.com/a\n   First\n\n" +
        "2. B result\n   https://example.com/b\n   Second",
    );
  });

  it("blocks non-http, local, metadata, and private network targets", () => {
    const blocked = [
      "file:///etc/passwd",
      "ftp://example.com/file",
      "http://localhost/admin",
      "http://127.0.0.1/",
      "http://10.0.0.1/",
      "http://172.16.0.1/",
      "http://192.168.1.1/",
      "http://169.254.169.254/latest/meta-data/",
      "http://[::1]/",
      "http://[::ffff:127.0.0.1]/",
      "http://metadata.google.internal/",
    ];

    for (const url of blocked) {
      assert.match(agentModule.validateUrlSafety(url), /blocked|unsupported/i);
    }
    assert.equal(agentModule.validateUrlSafety("https://example.com/a"), null);
  });

  it("rejects empty web search queries without network access", async () => {
    assert.equal(
      await agentModule.executeWebSearch(""),
      "Error: Search query is required",
    );
  });

  it("rejects unsafe web fetch URLs without network access", async () => {
    assert.match(
      await agentModule.executeWebFetch("http://127.0.0.1/secret"),
      /^Error: Blocked/i,
    );
  });
});
