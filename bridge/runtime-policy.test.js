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
  "po_file_list",
  "po_file_read",
  "po_file_write",
  "po_file_delete",
];
const PROFILE_ADDITIONS = APPROVED_TOOLS;
const APPROVED_MODEL = "agentcore/bedrock-agentcore";

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
    assert.deepEqual(policy.tools.deny, ["session_status"]);
    assert.deepEqual(policyModule.APPROVED_TOOLS, APPROVED_TOOLS);
    assert.deepEqual(policyModule.PROFILE_ADDITIONS, PROFILE_ADDITIONS);
    const effectiveTools = ["session_status", ...policy.tools.alsoAllow].filter(
      (tool) => !policy.tools.deny.includes(tool),
    );
    assert.deepEqual(effectiveTools, APPROVED_TOOLS);
    assert.equal(effectiveTools.includes("session_status"), false);
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

  it("exposes no model-callable network egress tool", () => {
    const serialized = JSON.stringify(policyModule.buildOpenClawConfig({
      gatewayToken: "a".repeat(43),
    }));

    assert.doesNotMatch(serialized, /web_fetch|web_search|browser|http_request/i);
    assert.equal(
      policyModule.APPROVED_TOOLS.some((tool) => /^web(?:_|\.)/.test(tool)),
      false,
    );
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
    assert.deepEqual(config.tools.deny, ["session_status"]);
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

  it("freezes model visibility and selection to the loopback AgentCore route", () => {
    const config = policyModule.buildOpenClawConfig({
      gatewayToken: "a".repeat(43),
      proxyPort: 18790,
    });

    assert.equal(config.models.mode, "replace");
    assert.deepEqual(Object.keys(config.models.providers), ["agentcore"]);
    assert.deepEqual(config.models.providers.agentcore.models, [
      { id: "bedrock-agentcore", name: "Bedrock AgentCore" },
    ]);
    assert.equal(
      config.models.providers.agentcore.baseUrl,
      "http://127.0.0.1:18790/v1",
    );
    assert.deepEqual(config.agents.defaults.model, { primary: APPROVED_MODEL });
    assert.deepEqual(config.agents.defaults.models, { [APPROVED_MODEL]: {} });
    assert.equal("fallbacks" in config.agents.defaults.model, false);

    const serialized = JSON.stringify(config);
    assert.doesNotMatch(serialized, /https?:\/\/(?!127\.0\.0\.1:18790\/v1)/i);
    assert.equal("openai" in config.models.providers, false);
    assert.equal("anthropic" in config.models.providers, false);
    assert.equal("google" in config.models.providers, false);
    assert.equal("openrouter" in config.models.providers, false);
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
    assert.match(contractSource, /invokeGatewayAgent\(\{/);
    assert.match(gatewaySource, /buildGatewayConnectRequest\(/);
    assert.match(gatewaySource, /assertGrantedGatewayScopes\(frame\.payload\)/);
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
        PATH: "/tmp/attacker-bin",
        HOME: "/tmp/attacker-home",
        NODE_PATH: "/tmp/attacker-modules",
        NODE_OPTIONS: "--require /tmp/evil.js",
        AWS_REGION: "eu-west-1",
        AWS_DEFAULT_REGION: "eu-west-1",
        AWS_CONFIG_FILE: "/tmp/scoped/config",
        AWS_SDK_LOAD_CONFIG: "1",
        AWS_EC2_METADATA_DISABLED: "true",
        AWS_SHARED_CREDENTIALS_FILE: "/dev/null",
        PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE:
          "/tmp/scoped/scoped-creds.json",
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
      PATH: "/usr/local/bin:/usr/bin:/bin",
      HOME: "/root",
      NODE_PATH: "/app/node_modules",
      NODE_OPTIONS:
        "--dns-result-order=ipv4first --no-network-family-autoselection -r /app/force-ipv4.js",
      AWS_REGION: "eu-west-1",
      AWS_DEFAULT_REGION: "eu-west-1",
      AWS_CONFIG_FILE: "/tmp/scoped/config",
      AWS_SDK_LOAD_CONFIG: "1",
      AWS_EC2_METADATA_DISABLED: "true",
      AWS_SHARED_CREDENTIALS_FILE: "/dev/null",
      S3_USER_FILES_BUCKET: "user-files",
      PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE:
        "/tmp/scoped/scoped-creds.json",
      PERSONAL_OPERATOR_WORKSPACE_PREFIX: "user_abcd1234",
      OPENCLAW_CONFIG_PATH: "/run/personal-operator/openclaw.json",
      OPENCLAW_STATE_DIR: "/mnt/workspace/live",
      OPENCLAW_WORKSPACE_DIR: "/mnt/workspace/live/workspace",
      OPENCLAW_SKIP_CRON: "1",
    });
  });

  it("rejects incomplete scoped configuration and explicit region poison", () => {
    const safe = {
      AWS_REGION: "eu-west-1",
      AWS_DEFAULT_REGION: "eu-west-1",
      AWS_CONFIG_FILE: "/tmp/scoped/config",
      AWS_SDK_LOAD_CONFIG: "1",
      AWS_EC2_METADATA_DISABLED: "true",
      AWS_SHARED_CREDENTIALS_FILE: "/dev/null",
      S3_USER_FILES_BUCKET: "user-files",
      PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE:
        "/tmp/scoped/scoped-creds.json",
    };
    for (const required of [
      "AWS_CONFIG_FILE",
      "PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE",
      "S3_USER_FILES_BUCKET",
    ]) {
      const incomplete = { ...safe };
      delete incomplete[required];
      assert.throws(
        () =>
          policyModule.buildOpenClawChildEnv({
            scopedEnv: incomplete,
            workspacePrefix: "user_A",
          }),
        /scoped|bucket|credential/i,
      );
    }
    assert.throws(
      () =>
        policyModule.buildOpenClawChildEnv({
          scopedEnv: { ...safe, AWS_REGION: "us-west-2" },
          workspacePrefix: "user_A",
        }),
      /eu-west-1|region/i,
    );
  });

  it("overwrites inherited executable and module search configuration", () => {
    const source = fs.readFileSync(
      path.join(__dirname, "entrypoint.sh"),
      "utf8",
    );
    assert.match(
      source,
      /NODE_OPTIONS="--dns-result-order=ipv4first --no-network-family-autoselection -r \/app\/force-ipv4\.js"/,
    );
    assert.doesNotMatch(source, /NODE_OPTIONS:-/);
  });

  it("pins config outside durable state and exposes only the activated live tree", () => {
    const childEnv = policyModule.buildOpenClawChildEnv({
      scopedEnv: {
        AWS_REGION: "eu-west-1",
        AWS_DEFAULT_REGION: "eu-west-1",
        AWS_CONFIG_FILE: "/tmp/scoped/config",
        AWS_SDK_LOAD_CONFIG: "1",
        AWS_EC2_METADATA_DISABLED: "true",
        AWS_SHARED_CREDENTIALS_FILE: "/dev/null",
        PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE:
          "/tmp/scoped/scoped-creds.json",
        S3_USER_FILES_BUCKET: "user-files",
        OPENCLAW_CONFIG_PATH: "/tmp/caller-config.json",
        OPENCLAW_STATE_DIR: "/tmp/caller-state",
        OPENCLAW_WORKSPACE_DIR: "/tmp/caller-workspace",
      },
      workspacePrefix: "user_A",
    });

    assert.equal(
      childEnv.OPENCLAW_CONFIG_PATH,
      "/run/personal-operator/openclaw.json",
    );
    assert.equal(childEnv.OPENCLAW_STATE_DIR, "/mnt/workspace/live");
    assert.equal(
      childEnv.OPENCLAW_WORKSPACE_DIR,
      "/mnt/workspace/live/workspace",
    );
    assert.equal(childEnv.OPENCLAW_CONFIG_PATH.startsWith(childEnv.OPENCLAW_STATE_DIR), false);
  });

  it("generates independent high-entropy local gateway tokens", () => {
    const first = policyModule.createLocalGatewayToken();
    const second = policyModule.createLocalGatewayToken();

    assert.match(first, /^[A-Za-z0-9_-]{43}$/);
    assert.match(second, /^[A-Za-z0-9_-]{43}$/);
    assert.notEqual(first, second);
  });
});

describe("Bedrock proxy child environment", () => {
  it("keeps execution-role sources only in proxy while S3 receives the scoped file", () => {
    const result = policyModule.buildProxyChildEnv({
      baseEnv: {
        AWS_REGION: "eu-west-1",
        AWS_DEFAULT_REGION: "eu-west-1",
        BEDROCK_MODEL_ID: "global.anthropic.test",
        S3_USER_FILES_BUCKET: "user-files",
        AWS_ACCESS_KEY_ID: "execution-key",
        AWS_SECRET_ACCESS_KEY: "execution-secret",
        AWS_SESSION_TOKEN: "execution-token",
        AWS_CONTAINER_CREDENTIALS_RELATIVE_URI: "/v2/credentials/execution",
        AWS_CONTAINER_AUTHORIZATION_TOKEN: "container-auth",
        AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE: "/var/run/container-auth",
        AWS_WEB_IDENTITY_TOKEN_FILE: "/var/run/token",
        AWS_ROLE_ARN: "arn:aws:iam::123456789012:role/execution",
        AWS_CONFIG_FILE: "/tmp/scoped/config-must-not-reach-proxy",
        AWS_PROFILE: "admin",
        NODE_OPTIONS: "--require /tmp/evil.js",
      },
      internalUserId: "user_A",
      namespace: "user_A",
      scopedCredentialsFile: "/tmp/scoped/scoped-creds.json",
    });

    assert.deepEqual(result, {
      PATH: "/usr/local/bin:/usr/bin:/bin",
      HOME: "/root",
      NODE_PATH: "/app/node_modules",
      NODE_OPTIONS:
        "--dns-result-order=ipv4first --no-network-family-autoselection -r /app/force-ipv4.js",
      AWS_REGION: "eu-west-1",
      AWS_DEFAULT_REGION: "eu-west-1",
      AWS_EC2_METADATA_DISABLED: "true",
      AWS_SHARED_CREDENTIALS_FILE: "/dev/null",
      AWS_CONFIG_FILE: "/dev/null",
      BEDROCK_MODEL_ID: "global.anthropic.test",
      S3_USER_FILES_BUCKET: "user-files",
      INTERNAL_USER_ID: "user_A",
      PERSONAL_OPERATOR_WORKSPACE_PREFIX: "user_A",
      PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE:
        "/tmp/scoped/scoped-creds.json",
      AWS_ACCESS_KEY_ID: "execution-key",
      AWS_SECRET_ACCESS_KEY: "execution-secret",
      AWS_SESSION_TOKEN: "execution-token",
      AWS_CONTAINER_CREDENTIALS_RELATIVE_URI: "/v2/credentials/execution",
      AWS_CONTAINER_AUTHORIZATION_TOKEN: "container-auth",
      AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE: "/var/run/container-auth",
      AWS_WEB_IDENTITY_TOKEN_FILE: "/var/run/token",
      AWS_ROLE_ARN: "arn:aws:iam::123456789012:role/execution",
    });
    assert.equal(result.AWS_CONFIG_FILE, "/dev/null");
    assert.equal("AWS_PROFILE" in result, false);
  });

  it("rejects identity mismatch, missing scoped file, and region poison", () => {
    const base = {
      AWS_REGION: "eu-west-1",
      S3_USER_FILES_BUCKET: "user-files",
    };
    for (const options of [
      {
        baseEnv: base,
        internalUserId: "user_A",
        namespace: "user_B",
        scopedCredentialsFile: "/tmp/scoped/creds.json",
      },
      {
        baseEnv: base,
        internalUserId: "user_A",
        namespace: "user_A",
        scopedCredentialsFile: "",
      },
      {
        baseEnv: { ...base, AWS_REGION: "us-west-2" },
        internalUserId: "user_A",
        namespace: "user_A",
        scopedCredentialsFile: "/tmp/scoped/creds.json",
      },
    ]) {
      assert.throws(
        () => policyModule.buildProxyChildEnv(options),
        /identity|namespace|scoped|eu-west-1|region/i,
      );
    }
  });
});
