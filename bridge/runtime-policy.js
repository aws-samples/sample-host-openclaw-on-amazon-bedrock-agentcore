"use strict";

const { randomBytes } = require("node:crypto");

const APPROVED_TOOLS = Object.freeze([
  "session_status",
  "web_search",
  "web_fetch",
  "po_file_list",
  "po_file_read",
  "po_file_write",
  "po_file_delete",
]);

// OpenClaw's minimal profile contains only session_status. This version rejects
// allow and alsoAllow in the same scope, so the exact effective boundary is the
// minimal profile plus these reviewed additions.
const PROFILE_ADDITIONS = Object.freeze(
  APPROVED_TOOLS.filter((tool) => tool !== "session_status"),
);

const PLUGIN_ID = "personal-operator";
const PLUGIN_PATH = "/app/plugins/personal-operator";

const CHILD_ENV_ALLOWLIST = Object.freeze([
  "PATH",
  "HOME",
  "NODE_PATH",
  "NODE_OPTIONS",
  "AWS_REGION",
  "AWS_CONFIG_FILE",
  "AWS_SDK_LOAD_CONFIG",
  "S3_USER_FILES_BUCKET",
]);

function buildRuntimePolicy() {
  return {
    tools: {
      profile: "minimal",
      alsoAllow: [...PROFILE_ADDITIONS],
    },
    plugins: {
      enabled: true,
      allow: [PLUGIN_ID],
      load: { paths: [PLUGIN_PATH] },
      entries: { [PLUGIN_ID]: { enabled: true } },
      slots: { memory: "none" },
    },
  };
}

function buildOpenClawConfig({
  gatewayToken,
  proxyPort = 18790,
  gatewayPort = 18789,
} = {}) {
  if (typeof gatewayToken !== "string" || gatewayToken.length < 32) {
    throw new Error("A high-entropy local gateway token is required");
  }

  const policy = buildRuntimePolicy();
  return {
    models: {
      providers: {
        agentcore: {
          baseUrl: `http://127.0.0.1:${proxyPort}/v1`,
          apiKey: "local",
          api: "openai-completions",
          models: [{ id: "bedrock-agentcore", name: "Bedrock AgentCore" }],
        },
      },
    },
    agents: {
      defaults: {
        model: { primary: "agentcore/bedrock-agentcore" },
      },
    },
    tools: policy.tools,
    plugins: policy.plugins,
    gateway: {
      mode: "local",
      port: gatewayPort,
      trustedProxies: ["127.0.0.1"],
      auth: { mode: "token", token: gatewayToken },
      controlUi: { enabled: false },
    },
    channels: {},
  };
}

function buildOpenClawChildEnv({ scopedEnv = {}, workspacePrefix } = {}) {
  if (typeof workspacePrefix !== "string" || workspacePrefix.length === 0) {
    throw new Error("A server-derived workspace prefix is required");
  }

  const env = {};
  for (const key of CHILD_ENV_ALLOWLIST) {
    if (scopedEnv[key] !== undefined && scopedEnv[key] !== "") {
      env[key] = scopedEnv[key];
    }
  }
  env.PERSONAL_OPERATOR_WORKSPACE_PREFIX = workspacePrefix;
  env.OPENCLAW_SKIP_CRON = "1";
  return env;
}

function createLocalGatewayToken() {
  return randomBytes(32).toString("base64url");
}

module.exports = {
  APPROVED_TOOLS,
  PROFILE_ADDITIONS,
  PLUGIN_ID,
  PLUGIN_PATH,
  CHILD_ENV_ALLOWLIST,
  buildRuntimePolicy,
  buildOpenClawConfig,
  buildOpenClawChildEnv,
  createLocalGatewayToken,
};
