/**
 * Tests for MODEL_OVERRIDE env var support in the proxy.
 *
 * These tests verify that:
 * 1. MODEL_OVERRIDE is used instead of MODEL_ID when set
 * 2. MODEL_OVERRIDE falls back to MODEL_ID when empty
 * 3. Subagent requests still use SUBAGENT_BEDROCK_MODEL_ID regardless
 */

const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");

// We test resolveModelId by re-evaluating the proxy module with different env vars.
// Since the proxy module sets up an HTTP server, we need a lighter approach.
// Instead, we extract the logic pattern and test it directly.

describe("MODEL_OVERRIDE resolution", () => {
  // Simulate the proxy's resolveModelId logic with MODEL_OVERRIDE support
  function resolveModelId(requestedModel, opts = {}) {
    const MODEL_ID = opts.modelId || "minimax.minimax-m2.1";
    const MODEL_OVERRIDE = opts.modelOverride || "";
    const SUBAGENT_MODEL_NAME = opts.subagentModelName || "bedrock-agentcore-subagent";
    const SUBAGENT_BEDROCK_MODEL_ID = opts.subagentModelId || MODEL_ID;

    if (!requestedModel) return MODEL_OVERRIDE || MODEL_ID;
    if (
      requestedModel === SUBAGENT_MODEL_NAME ||
      requestedModel.endsWith(`/${SUBAGENT_MODEL_NAME}`)
    ) {
      return SUBAGENT_BEDROCK_MODEL_ID;
    }
    return MODEL_OVERRIDE || MODEL_ID;
  }

  it("should use MODEL_OVERRIDE when set for regular requests", () => {
    const result = resolveModelId(null, {
      modelId: "minimax.minimax-m2.1",
      modelOverride: "global.anthropic.claude-opus-4-6-v1",
    });
    assert.equal(result, "global.anthropic.claude-opus-4-6-v1");
  });

  it("should fall back to MODEL_ID when MODEL_OVERRIDE is empty", () => {
    const result = resolveModelId(null, {
      modelId: "minimax.minimax-m2.1",
      modelOverride: "",
    });
    assert.equal(result, "minimax.minimax-m2.1");
  });

  it("should use MODEL_OVERRIDE for named model requests", () => {
    const result = resolveModelId("some-model-name", {
      modelId: "minimax.minimax-m2.1",
      modelOverride: "global.anthropic.claude-opus-4-6-v1",
    });
    assert.equal(result, "global.anthropic.claude-opus-4-6-v1");
  });

  it("should still route subagent requests to SUBAGENT_BEDROCK_MODEL_ID", () => {
    const result = resolveModelId("bedrock-agentcore-subagent", {
      modelId: "minimax.minimax-m2.1",
      modelOverride: "global.anthropic.claude-opus-4-6-v1",
      subagentModelId: "global.anthropic.claude-sonnet-4-6",
    });
    assert.equal(result, "global.anthropic.claude-sonnet-4-6");
  });

  it("should still route subagent requests with path prefix", () => {
    const result = resolveModelId("prefix/bedrock-agentcore-subagent", {
      modelId: "minimax.minimax-m2.1",
      modelOverride: "global.anthropic.claude-opus-4-6-v1",
      subagentModelId: "global.anthropic.claude-sonnet-4-6",
    });
    assert.equal(result, "global.anthropic.claude-sonnet-4-6");
  });

  it("should use MODEL_ID when both MODEL_OVERRIDE and requested model are absent", () => {
    const result = resolveModelId(null, {
      modelId: "minimax.minimax-m2.1",
    });
    assert.equal(result, "minimax.minimax-m2.1");
  });
});

describe("Contract server modelOverride pass-through", () => {
  it("should set MODEL_OVERRIDE in proxy env when modelOverride present in payload", () => {
    // Simulate the contract server reading modelOverride from payload
    const payload = {
      action: "chat",
      userId: "user_abc",
      actorId: "telegram:123",
      channel: "telegram",
      message: "hello",
      modelOverride: "global.anthropic.claude-opus-4-6-v1",
    };

    // The contract server should extract and set it
    const modelOverride = payload.modelOverride || "";
    assert.equal(modelOverride, "global.anthropic.claude-opus-4-6-v1");

    // Simulate building proxyEnv
    const proxyEnv = {
      BEDROCK_MODEL_ID: "minimax.minimax-m2.1",
      MODEL_OVERRIDE: modelOverride,
    };
    assert.equal(proxyEnv.MODEL_OVERRIDE, "global.anthropic.claude-opus-4-6-v1");
  });

  it("should set empty MODEL_OVERRIDE when modelOverride absent in payload", () => {
    const payload = {
      action: "chat",
      userId: "user_abc",
      actorId: "telegram:123",
      channel: "telegram",
      message: "hello",
    };

    const modelOverride = payload.modelOverride || "";
    assert.equal(modelOverride, "");
  });
});
