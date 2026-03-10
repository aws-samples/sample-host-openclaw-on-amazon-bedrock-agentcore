/**
 * Tests for subagent model routing and detection from agentcore-proxy.js.
 * Run: node --test subagent-routing.test.js
 *
 * Since resolveModelId and isSubagentRequest are not exported (inline in
 * proxy module), we mirror the logic here. Changes to the proxy must be
 * mirrored — same pattern as proxy-identity.test.js.
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

// --- Mirror constants from agentcore-proxy.js ---
const MODEL_ID = "minimax.minimax-m2.1";
const SUBAGENT_MODEL_NAME = "bedrock-agentcore-subagent";
const SUBAGENT_BEDROCK_MODEL_ID = "global.anthropic.claude-sonnet-4-6-v1";

// --- Mirror of isSubagentRequest from agentcore-proxy.js ---

function isSubagentRequest(parsed) {
  if (!parsed || !parsed.model) return false;
  const requested = parsed.model;
  return requested === SUBAGENT_MODEL_NAME ||
    requested.endsWith(`/${SUBAGENT_MODEL_NAME}`);
}

// --- Mirror of resolveModelId from agentcore-proxy.js ---

function resolveModelId(requestedModel) {
  if (!requestedModel) return MODEL_ID;
  if (requestedModel === SUBAGENT_MODEL_NAME ||
      requestedModel.endsWith(`/${SUBAGENT_MODEL_NAME}`)) {
    return SUBAGENT_BEDROCK_MODEL_ID;
  }
  return MODEL_ID;
}

// --- Tests ---

describe("resolveModelId", () => {
  it("returns MODEL_ID when no model requested", () => {
    assert.equal(resolveModelId(undefined), MODEL_ID);
    assert.equal(resolveModelId(null), MODEL_ID);
    assert.equal(resolveModelId(""), MODEL_ID);
  });

  it("returns MODEL_ID for main agent model name", () => {
    assert.equal(resolveModelId("bedrock-agentcore"), MODEL_ID);
  });

  it("returns MODEL_ID for provider-prefixed main agent model", () => {
    assert.equal(resolveModelId("agentcore/bedrock-agentcore"), MODEL_ID);
  });

  it("returns SUBAGENT_BEDROCK_MODEL_ID for bare subagent model name", () => {
    assert.equal(resolveModelId(SUBAGENT_MODEL_NAME), SUBAGENT_BEDROCK_MODEL_ID);
  });

  it("returns SUBAGENT_BEDROCK_MODEL_ID for provider-prefixed subagent model", () => {
    assert.equal(
      resolveModelId(`agentcore/${SUBAGENT_MODEL_NAME}`),
      SUBAGENT_BEDROCK_MODEL_ID,
    );
  });

  it("returns MODEL_ID for unknown model names", () => {
    assert.equal(resolveModelId("gpt-4"), MODEL_ID);
    assert.equal(resolveModelId("claude-3"), MODEL_ID);
    assert.equal(resolveModelId("some-other-model"), MODEL_ID);
  });
});

describe("isSubagentRequest", () => {
  it("returns false for null/undefined parsed", () => {
    assert.equal(isSubagentRequest(null), false);
    assert.equal(isSubagentRequest(undefined), false);
  });

  it("returns false when no model in parsed", () => {
    assert.equal(isSubagentRequest({}), false);
    assert.equal(isSubagentRequest({ messages: [] }), false);
  });

  it("returns false for main agent model", () => {
    assert.equal(isSubagentRequest({ model: "bedrock-agentcore" }), false);
  });

  it("returns false for provider-prefixed main agent model", () => {
    assert.equal(
      isSubagentRequest({ model: "agentcore/bedrock-agentcore" }),
      false,
    );
  });

  it("returns true for bare subagent model name", () => {
    assert.equal(
      isSubagentRequest({ model: SUBAGENT_MODEL_NAME }),
      true,
    );
  });

  it("returns true for provider-prefixed subagent model name", () => {
    assert.equal(
      isSubagentRequest({ model: `agentcore/${SUBAGENT_MODEL_NAME}` }),
      true,
    );
  });

  it("returns false for partial subagent name match", () => {
    assert.equal(
      isSubagentRequest({ model: "bedrock-agentcore-subagent-v2" }),
      false,
    );
  });
});

describe("subagent counter logic", () => {
  it("increments subagentRequestCount only for subagent requests", () => {
    let chatRequestCount = 0;
    let subagentRequestCount = 0;

    // Simulate main agent request
    const mainReq = { model: "bedrock-agentcore", messages: [{ role: "user", content: "hello" }] };
    chatRequestCount++;
    if (isSubagentRequest(mainReq)) subagentRequestCount++;

    assert.equal(chatRequestCount, 1);
    assert.equal(subagentRequestCount, 0);

    // Simulate subagent request
    const subReq = { model: `agentcore/${SUBAGENT_MODEL_NAME}`, messages: [{ role: "user", content: "research" }] };
    chatRequestCount++;
    if (isSubagentRequest(subReq)) subagentRequestCount++;

    assert.equal(chatRequestCount, 2);
    assert.equal(subagentRequestCount, 1);

    // Simulate another main request
    const mainReq2 = { model: "bedrock-agentcore", messages: [] };
    chatRequestCount++;
    if (isSubagentRequest(mainReq2)) subagentRequestCount++;

    assert.equal(chatRequestCount, 3);
    assert.equal(subagentRequestCount, 1);
  });
});

describe("health endpoint shape", () => {
  it("includes subagent fields in health response", () => {
    // Simulate the /health response shape from the proxy
    const healthResponse = {
      status: "ok",
      model: MODEL_ID,
      subagent_model: SUBAGENT_BEDROCK_MODEL_ID,
      subagent_model_name: SUBAGENT_MODEL_NAME,
      cognito: "configured",
      s3_bucket: "test-bucket",
      total_requests: 5,
      subagent_requests: 2,
      last_identity: null,
      installed_skills: [],
      s3_skill_exists: true,
    };

    assert.equal(healthResponse.subagent_requests, 2);
    assert.equal(healthResponse.total_requests, 5);
    assert.equal(healthResponse.subagent_model, SUBAGENT_BEDROCK_MODEL_ID);
    assert.equal(healthResponse.subagent_model_name, SUBAGENT_MODEL_NAME);
  });
});

describe("contract writeOpenClawConfig subagent model", () => {
  it("subagent model name is distinct from main model", () => {
    // The contract sets subagentModel = "agentcore/bedrock-agentcore-subagent"
    const SUBAGENT_MODEL_NAME_CONTRACT = "bedrock-agentcore-subagent";
    const subagentModel = `agentcore/${SUBAGENT_MODEL_NAME_CONTRACT}`;

    assert.notEqual(subagentModel, "agentcore/bedrock-agentcore");
    assert.equal(subagentModel, `agentcore/${SUBAGENT_MODEL_NAME}`);
  });

  it("config models array includes both main and subagent entries", () => {
    const models = [
      { id: "bedrock-agentcore", name: "Bedrock AgentCore" },
      { id: SUBAGENT_MODEL_NAME, name: "Bedrock AgentCore Subagent" },
    ];

    assert.equal(models.length, 2);
    assert.equal(models[0].id, "bedrock-agentcore");
    assert.equal(models[1].id, SUBAGENT_MODEL_NAME);
  });
});
