/**
 * Default model routing in agentcore-proxy.js: the main agent defaults to
 * Claude Opus 5.5 and sub-agents to Claude Sonnet 5.5, and neither sends
 * temperature (both reject it on Converse/ConverseStream).
 *
 * The proxy is a server entry point, so this evaluates its own model
 * constants and routing functions from the source text in a sandbox.
 * Run: cd bridge && node --test model-defaults.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { buildInferenceConfig, parseTemperatureOverride } = require("./inference-config");

const source = fs.readFileSync(path.join(__dirname, "agentcore-proxy.js"), "utf-8");
const OPUS_55 = "global.anthropic.claude-opus-5-5";
const SONNET_55 = "global.anthropic.claude-sonnet-5-5";

function slice(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start > 0 && end > start, `${startMarker} block not found`);
  return source.slice(start, end);
}

function proxyRouting(env) {
  const box = { process: { env }, AWS_REGION: "us-west-2", console: { log() {} } };
  vm.createContext(box);
  vm.runInContext(
    slice("// Defaults: Opus 5.5", "// Bedrock Guardrails") +
      slice("function isSubagentRequest(", "\n/**\n * Call Bedrock Converse API") +
      "\n;globalThis.out = { MODEL_ID, SUBAGENT_BEDROCK_MODEL_ID, resolveModelId };",
    box,
  );
  return box.out;
}

describe("proxy model defaults", () => {
  for (const [label, env] of [
    ["unset env", {}],
    ["empty env (as deploy.sh passes an empty subagent_model_id)", { BEDROCK_MODEL_ID: "", SUBAGENT_BEDROCK_MODEL_ID: "" }],
  ]) {
    it(`${label}: main turn routes to Opus 5.5, sub-agent turn to Sonnet 5.5`, () => {
      const proxy = proxyRouting(env);
      assert.equal(proxy.MODEL_ID, OPUS_55);
      assert.equal(proxy.SUBAGENT_BEDROCK_MODEL_ID, SONNET_55);
      assert.equal(proxy.resolveModelId("bedrock-agentcore"), OPUS_55);
      assert.equal(proxy.resolveModelId("agentcore/bedrock-agentcore"), OPUS_55);
      assert.equal(proxy.resolveModelId("bedrock-agentcore-subagent"), SONNET_55);
      assert.equal(proxy.resolveModelId("agentcore/bedrock-agentcore-subagent"), SONNET_55);
    });
  }

  it("a main-model override does not change the sub-agent default", () => {
    const proxy = proxyRouting({ BEDROCK_MODEL_ID: "custom-main" });
    assert.equal(proxy.resolveModelId("bedrock-agentcore"), "custom-main");
    assert.equal(proxy.resolveModelId("agentcore/bedrock-agentcore-subagent"), SONNET_55);
  });

  it("explicit env values win", () => {
    const proxy = proxyRouting({ BEDROCK_MODEL_ID: "custom-main", SUBAGENT_BEDROCK_MODEL_ID: "custom-sub" });
    assert.equal(proxy.resolveModelId("agentcore/bedrock-agentcore-subagent"), "custom-sub");
  });

  it("neither default model is sent a temperature", () => {
    for (const modelId of [OPUS_55, SONNET_55]) {
      assert.deepEqual(buildInferenceConfig(modelId, parseTemperatureOverride(undefined)), { maxTokens: 16384 });
    }
  });

  it("cdk.json defaults match the proxy defaults", () => {
    const context = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "cdk.json"), "utf-8")).context;
    assert.equal(context.default_model_id, OPUS_55);
    assert.equal(context.subagent_model_id, SONNET_55);
  });
});
