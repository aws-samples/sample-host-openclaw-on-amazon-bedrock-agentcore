"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

let policyModule = null;
try {
  policyModule = require("./runtime-policy");
} catch {
  // RED: the production module is introduced only after these contracts fail.
}

const APPROVED_TOOLS = [
  "session_status",
  "web_fetch",
  "po_file_list",
  "po_file_read",
  "po_file_write",
  "po_file_delete",
];
const PROFILE_ADDITIONS = APPROVED_TOOLS.filter(
  (tool) => tool !== "session_status",
);

const FORBIDDEN_TOOL_FAMILIES = [
  "exec",
  "process",
  "read",
  "write",
  "edit",
  "apply_patch",
  "browser",
  "cron",
  "gateway",
  "sessions",
  "subagents",
  "mcp",
  "canvas",
];

const GATEWAY_CLIENT_SCOPES = ["operator.read", "operator.write"];

describe("frozen runtime policy", () => {
  it("exports the runtime policy module", () => {
    assert.ok(policyModule, "runtime-policy.js must exist");
  });

  it("allows exactly the reviewed OpenClaw tools", () => {
    const policy = policyModule.buildRuntimePolicy();

    assert.equal(policy.tools.profile, "minimal");
    assert.deepEqual(policy.tools.alsoAllow, PROFILE_ADDITIONS);
    assert.deepEqual(policyModule.APPROVED_TOOLS, APPROVED_TOOLS);
    assert.deepEqual(policyModule.PROFILE_ADDITIONS, PROFILE_ADDITIONS);
    assert.deepEqual(["session_status", ...policy.tools.alsoAllow], APPROVED_TOOLS);
    assert.equal(new Set(APPROVED_TOOLS).size, APPROVED_TOOLS.length);
  });

  it("loads only the repository-owned plugin from its immutable image path", () => {
    const policy = policyModule.buildRuntimePolicy();

    assert.deepEqual(policy.plugins, {
      enabled: true,
      allow: ["personal-operator"],
      load: { paths: ["/app/plugins/personal-operator"] },
      entries: {
        "personal-operator": { enabled: true },
      },
      slots: { memory: "none" },
    });
    assert.equal("web" in policy.tools, false);
  });

  it("does not expose any forbidden tool family", () => {
    const { tools } = policyModule.buildRuntimePolicy();
    for (const forbidden of FORBIDDEN_TOOL_FAMILIES) {
      assert.equal(
        APPROVED_TOOLS.some(
          (tool) =>
            tool === forbidden ||
            tool.startsWith(`${forbidden}_`) ||
            tool.startsWith(`${forbidden}.`),
        ),
        false,
        `${forbidden} must remain unavailable`,
      );
    }
  });
});

describe("generated OpenClaw configuration", () => {
  it("uses the frozen policy without subagents, skills, or control UI bypasses", () => {
    const config = policyModule.buildOpenClawConfig({
      gatewayToken: "a".repeat(43),
      proxyPort: 18790,
      gatewayPort: 18789,
    });

    assert.equal(config.tools.profile, "minimal");
    assert.deepEqual(config.tools.alsoAllow, PROFILE_ADDITIONS);
    assert.equal("allow" in config.tools, false);
    assert.deepEqual(config.plugins.allow, ["personal-operator"]);
    assert.deepEqual(config.plugins.load.paths, [
      "/app/plugins/personal-operator",
    ]);
    assert.equal(config.gateway.auth.token, "a".repeat(43));
    assert.deepEqual(config.gateway.controlUi, { enabled: false });
    assert.deepEqual(config.agents.defaults.skills, []);
    assert.deepEqual(config.skills, { allowBundled: [] });
    assert.deepEqual(config.commands, { text: false });
    assert.equal("subagents" in config.agents.defaults, false);
    assert.equal("exec" in config.tools, false);

    const serialized = JSON.stringify(config);
    assert.doesNotMatch(serialized, /web_search|duckduckgo/i);
    assert.doesNotMatch(serialized, /dangerously/i);
    assert.doesNotMatch(serialized, /allowInsecureAuth/i);
    assert.doesNotMatch(serialized, /allowedOrigins/i);
    assert.doesNotMatch(serialized, /\/skills/);
  });

  it("uses only read/write gateway scopes and gives the client no config authority", () => {
    assert.deepEqual(policyModule.GATEWAY_CLIENT_SCOPES, GATEWAY_CLIENT_SCOPES);
    assert.equal(policyModule.GATEWAY_CLIENT_SCOPES.includes("operator.admin"), false);

    const contractSource = fs.readFileSync(
      path.join(__dirname, "agentcore-contract.js"),
      "utf8",
    );
    const gatewaySource = fs.readFileSync(
      path.join(__dirname, "gateway-invocation.js"),
      "utf8",
    );
    assert.match(gatewaySource, /scopes:\s*GATEWAY_CLIENT_SCOPES/);
    assert.match(contractSource, /buildGatewayConnectRequest\(/);
    assert.match(contractSource, /assertGrantedGatewayScopes\(msg\.payload\)/);
    assert.doesNotMatch(contractSource, /operator\.admin/);
    assert.doesNotMatch(gatewaySource, /operator\.admin/);
    assert.match(gatewaySource, /minProtocol:\s*4/);
    assert.match(gatewaySource, /maxProtocol:\s*4/);
    assert.match(gatewaySource, /id:\s*"cli"/);
    assert.match(gatewaySource, /mode:\s*"cli"/);
    assert.doesNotMatch(gatewaySource, /mode:\s*"backend"/);
    assert.doesNotMatch(gatewaySource, /id:\s*"gateway-client"/);
    assert.doesNotMatch(gatewaySource, /id:\s*"openclaw-control-ui"/);
  });

  it("rejects a missing local gateway token", () => {
    assert.throws(
      () => policyModule.buildOpenClawConfig({ gatewayToken: "" }),
      /gateway token/i,
    );
  });
});

describe("OpenClaw child environment", () => {
  it("forwards only runtime data and never product or provider authority", () => {
    const childEnv = policyModule.buildOpenClawChildEnv({
      scopedEnv: {
        PATH: "/usr/bin",
        HOME: "/root",
        NODE_PATH: "/app/node_modules",
        NODE_OPTIONS: "--dns-result-order=ipv4first",
        AWS_REGION: "eu-west-1",
        AWS_CONFIG_FILE: "/tmp/scoped/config",
        AWS_SDK_LOAD_CONFIG: "1",
        S3_USER_FILES_BUCKET: "user-files",
        TELEGRAM_BOT_TOKEN: "telegram-secret",
        TELEGRAM_CHANNEL_SECRET_ID: "telegram-secret-id",
        GMAIL_REFRESH_TOKEN: "gmail-secret",
        GOOGLE_CLIENT_SECRET: "google-secret",
        COGNITO_PASSWORD_SECRET: "cognito-secret",
        IDENTITY_TABLE_NAME: "identity-table",
        APPROVAL_SIGNING_KEY: "signing-secret",
        INTERNAL_USER_ID: "caller-controlled",
        USER_ID: "caller-controlled",
        EVENTBRIDGE_ROLE_ARN: "scheduler-role",
      },
      workspacePrefix: "user_abcd1234",
    });

    assert.deepEqual(childEnv, {
      PATH: "/usr/bin",
      HOME: "/root",
      NODE_PATH: "/app/node_modules",
      NODE_OPTIONS: "--dns-result-order=ipv4first",
      AWS_REGION: "eu-west-1",
      AWS_CONFIG_FILE: "/tmp/scoped/config",
      AWS_SDK_LOAD_CONFIG: "1",
      S3_USER_FILES_BUCKET: "user-files",
      PERSONAL_OPERATOR_WORKSPACE_PREFIX: "user_abcd1234",
      OPENCLAW_SKIP_CRON: "1",
    });
  });

  it("generates independent high-entropy local gateway tokens", () => {
    const first = policyModule.createLocalGatewayToken();
    const second = policyModule.createLocalGatewayToken();

    assert.match(first, /^[A-Za-z0-9_-]{43}$/);
    assert.match(second, /^[A-Za-z0-9_-]{43}$/);
    assert.notEqual(first, second);
  });
});
