"use strict";

const { randomBytes } = require("node:crypto");
const { canonicalNamespace } = require("./session-binding");
const {
  requireExactRegion,
  FIXED_CHILD_ENV,
} = require("./scoped-credentials");

const APPROVED_TOOLS = Object.freeze([
  "session_status",
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
const GATEWAY_CLIENT_SCOPES = Object.freeze([
  "operator.read",
  "operator.write",
]);

const CHILD_ENV_ALLOWLIST = Object.freeze([
  "PATH",
  "HOME",
  "NODE_PATH",
  "NODE_OPTIONS",
  "AWS_REGION",
  "AWS_DEFAULT_REGION",
  "AWS_CONFIG_FILE",
  "AWS_SDK_LOAD_CONFIG",
  "AWS_EC2_METADATA_DISABLED",
  "AWS_SHARED_CREDENTIALS_FILE",
  "S3_USER_FILES_BUCKET",
  "PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE",
]);

const REQUIRED_SCOPED_ENV = Object.freeze([
  "AWS_CONFIG_FILE",
  "PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE",
  "S3_USER_FILES_BUCKET",
]);
const OPENCLAW_CONFIG_PATH = "/run/personal-operator/openclaw.json";
const OPENCLAW_STATE_DIR = "/mnt/workspace/live";
const OPENCLAW_WORKSPACE_DIR = "/mnt/workspace/live/workspace";
const PROXY_AMBIENT_CREDENTIAL_ENV_KEYS = Object.freeze([
  "AWS_ACCESS_KEY_ID",
  "AWS_SECRET_ACCESS_KEY",
  "AWS_SESSION_TOKEN",
  "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
  "AWS_CONTAINER_CREDENTIALS_FULL_URI",
  "AWS_CONTAINER_AUTHORIZATION_TOKEN",
  "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
  "AWS_WEB_IDENTITY_TOKEN_FILE",
  "AWS_ROLE_ARN",
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
      entries: {
        [PLUGIN_ID]: { enabled: true },
      },
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
        skills: [],
      },
    },
    skills: { allowBundled: [] },
    commands: { text: false },
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
  const canonicalPrefix = canonicalNamespace(workspacePrefix);
  requireExactRegion(scopedEnv);
  for (const key of REQUIRED_SCOPED_ENV) {
    if (typeof scopedEnv[key] !== "string" || scopedEnv[key].length === 0) {
      throw new Error(`Scoped child environment requires ${key}`);
    }
  }
  if (
    scopedEnv.AWS_EC2_METADATA_DISABLED !== "true" ||
    scopedEnv.AWS_SHARED_CREDENTIALS_FILE !== "/dev/null" ||
    scopedEnv.AWS_SDK_LOAD_CONFIG !== "1"
  ) {
    throw new Error("Scoped child credential providers are not fail-closed");
  }

  const env = {};
  for (const key of CHILD_ENV_ALLOWLIST) {
    if (scopedEnv[key] !== undefined && scopedEnv[key] !== "") {
      env[key] = scopedEnv[key];
    }
  }
  Object.assign(env, FIXED_CHILD_ENV);
  env.PERSONAL_OPERATOR_WORKSPACE_PREFIX = canonicalPrefix;
  env.OPENCLAW_CONFIG_PATH = OPENCLAW_CONFIG_PATH;
  env.OPENCLAW_STATE_DIR = OPENCLAW_STATE_DIR;
  env.OPENCLAW_WORKSPACE_DIR = OPENCLAW_WORKSPACE_DIR;
  env.OPENCLAW_SKIP_CRON = "1";
  return env;
}

function buildProxyChildEnv({
  baseEnv = {},
  internalUserId,
  namespace,
  scopedCredentialsFile,
} = {}) {
  const region = requireExactRegion(baseEnv);
  const canonical = canonicalNamespace(internalUserId);
  if (canonicalNamespace(namespace) !== canonical) {
    throw new Error("Proxy namespace must exactly equal internal identity");
  }
  if (
    typeof scopedCredentialsFile !== "string" ||
    scopedCredentialsFile.length === 0
  ) {
    throw new Error("Proxy requires an explicit scoped credential file for S3");
  }
  if (
    typeof baseEnv.S3_USER_FILES_BUCKET !== "string" ||
    baseEnv.S3_USER_FILES_BUCKET.length === 0
  ) {
    throw new Error("Proxy requires the S3 user-files bucket");
  }

  const env = {
    ...FIXED_CHILD_ENV,
    AWS_REGION: region,
    AWS_DEFAULT_REGION: region,
    AWS_EC2_METADATA_DISABLED: "true",
    AWS_SHARED_CREDENTIALS_FILE: "/dev/null",
    AWS_CONFIG_FILE: "/dev/null",
    S3_USER_FILES_BUCKET: baseEnv.S3_USER_FILES_BUCKET,
    INTERNAL_USER_ID: canonical,
    PERSONAL_OPERATOR_WORKSPACE_PREFIX: canonical,
    PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE: scopedCredentialsFile,
  };
  for (const key of [
    "BEDROCK_MODEL_ID",
    "BEDROCK_GUARDRAIL_ID",
    "BEDROCK_GUARDRAIL_VERSION",
  ]) {
    if (typeof baseEnv[key] === "string" && baseEnv[key].length > 0) {
      env[key] = baseEnv[key];
    }
  }
  for (const key of PROXY_AMBIENT_CREDENTIAL_ENV_KEYS) {
    if (typeof baseEnv[key] === "string" && baseEnv[key].length > 0) {
      env[key] = baseEnv[key];
    }
  }
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
  GATEWAY_CLIENT_SCOPES,
  CHILD_ENV_ALLOWLIST,
  PROXY_AMBIENT_CREDENTIAL_ENV_KEYS,
  OPENCLAW_CONFIG_PATH,
  OPENCLAW_STATE_DIR,
  OPENCLAW_WORKSPACE_DIR,
  buildRuntimePolicy,
  buildOpenClawConfig,
  buildOpenClawChildEnv,
  buildProxyChildEnv,
  createLocalGatewayToken,
};
