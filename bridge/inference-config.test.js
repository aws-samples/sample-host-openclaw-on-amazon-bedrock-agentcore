/**
 * Converse inferenceConfig: temperature per model + BEDROCK_TEMPERATURE override.
 *
 * Claude Opus 5 rejects temperature on Converse/ConverseStream
 * ("ValidationException: temperature is deprecated for this model"), which
 * broke every turn when BEDROCK_MODEL_ID was switched to it. The proxy must
 * leave the key out for such models and keep 0.7 for the rest.
 * Run: cd bridge && node --test inference-config.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

const {
  modelRejectsTemperature,
  parseTemperatureOverride,
  buildInferenceConfig,
} = require("./inference-config");

const OPUS_5 = "global.anthropic.claude-opus-5-5";
const SONNET_46 = "global.anthropic.claude-sonnet-4-6";

describe("modelRejectsTemperature", () => {
  it("is true for Opus 5 with any region/global prefix", () => {
    for (const id of [
      OPUS_5,
      "anthropic.claude-opus-5-5",
      "us.anthropic.claude-opus-5-5",
      "apac.anthropic.claude-opus-5-5-v1:0",
      "anthropic.claude-opus-5",
      "arn:aws:bedrock:us-west-2:123456789012:inference-profile/global.anthropic.claude-opus-5-5",
    ]) {
      assert.equal(modelRejectsTemperature(id), true, id);
    }
  });

  it("is true for later Opus majors", () => {
    assert.equal(modelRejectsTemperature("global.anthropic.claude-opus-6"), true);
    assert.equal(modelRejectsTemperature("anthropic.claude-opus-10-1"), true);
  });

  it("is false for current models", () => {
    for (const id of [
      SONNET_46,
      "global.anthropic.claude-sonnet-4-6-v1",
      "us.anthropic.claude-opus-4-6-v1",
      "anthropic.claude-opus-4-5-20251101-v1:0",
      "anthropic.claude-3-opus-20240229-v1:0",
      "minimax.minimax-m2.1",
      "amazon.nova-pro-v1:0",
      "",
      undefined,
    ]) {
      assert.equal(modelRejectsTemperature(id), false, String(id));
    }
  });
});

describe("buildInferenceConfig without override", () => {
  it("omits the temperature key for Opus 5 (key absent, not undefined/null)", () => {
    const cfg = buildInferenceConfig(OPUS_5, parseTemperatureOverride(undefined));
    assert.deepEqual(cfg, { maxTokens: 16384 });
    assert.equal(Object.prototype.hasOwnProperty.call(cfg, "temperature"), false);
    assert.equal(JSON.stringify(cfg), '{"maxTokens":16384}');
  });

  it("keeps Sonnet 4.6 unchanged: maxTokens 16384, temperature 0.7", () => {
    assert.deepEqual(buildInferenceConfig(SONNET_46, parseTemperatureOverride("")), {
      maxTokens: 16384,
      temperature: 0.7,
    });
  });

  it("keeps the default model (minimax) at 0.7", () => {
    assert.deepEqual(buildInferenceConfig("minimax.minimax-m2.1"), {
      maxTokens: 16384,
      temperature: 0.7,
    });
  });
});

describe("BEDROCK_TEMPERATURE override", () => {
  it("parses unset/empty as auto", () => {
    assert.deepEqual(parseTemperatureOverride(undefined), { mode: "auto" });
    assert.deepEqual(parseTemperatureOverride(""), { mode: "auto" });
    assert.deepEqual(parseTemperatureOverride("  "), { mode: "auto" });
  });

  it("'none' omits temperature for every model", () => {
    for (const raw of ["none", "NONE", " None "]) {
      const o = parseTemperatureOverride(raw);
      assert.deepEqual(o, { mode: "omit" });
      assert.deepEqual(buildInferenceConfig(SONNET_46, o), { maxTokens: 16384 });
      assert.deepEqual(buildInferenceConfig(OPUS_5, o), { maxTokens: 16384 });
    }
  });

  it("a number sets temperature for every model, including Opus 5", () => {
    const o = parseTemperatureOverride("0.2");
    assert.deepEqual(o, { mode: "set", value: 0.2 });
    assert.deepEqual(buildInferenceConfig(SONNET_46, o), { maxTokens: 16384, temperature: 0.2 });
    assert.deepEqual(buildInferenceConfig(OPUS_5, o), { maxTokens: 16384, temperature: 0.2 });
    assert.deepEqual(buildInferenceConfig(SONNET_46, parseTemperatureOverride("0")), {
      maxTokens: 16384,
      temperature: 0,
    });
  });

  it("an invalid value falls back to the per-model default and is reported", () => {
    for (const raw of ["abc", "-1", "NaN", "Infinity"]) {
      const o = parseTemperatureOverride(raw);
      assert.equal(o.mode, "auto", raw);
      assert.equal(o.invalid, raw);
      assert.deepEqual(buildInferenceConfig(SONNET_46, o), { maxTokens: 16384, temperature: 0.7 });
      assert.deepEqual(buildInferenceConfig(OPUS_5, o), { maxTokens: 16384 });
    }
  });
});

describe("proxy and contract wiring", () => {
  const bridgeDir = __dirname;
  const proxySrc = fs.readFileSync(path.join(bridgeDir, "agentcore-proxy.js"), "utf-8");
  const contractSrc = fs.readFileSync(path.join(bridgeDir, "agentcore-contract.js"), "utf-8");
  const dockerfile = fs.readFileSync(path.join(bridgeDir, "Dockerfile"), "utf-8");

  it("proxy no longer hard-codes a temperature in inferenceConfig", () => {
    assert.doesNotMatch(proxySrc, /inferenceConfig:\s*\{[^}]*temperature/);
  });

  it("both Converse and ConverseStream calls build inferenceConfig per model", () => {
    const uses = proxySrc.match(/inferenceConfig:\s*buildInferenceConfig\(modelId, temperatureOverride\)/g) || [];
    assert.equal(uses.length, 2);
    assert.match(proxySrc, /parseTemperatureOverride\(process\.env\.BEDROCK_TEMPERATURE\)/);
  });

  it("contract forwards BEDROCK_TEMPERATURE to the proxy child", () => {
    const start = contractSrc.indexOf("const proxyEnv = {");
    assert.notEqual(start, -1);
    const block = contractSrc.slice(start, contractSrc.indexOf("};", start));
    assert.match(block, /BEDROCK_TEMPERATURE:\s*process\.env\.BEDROCK_TEMPERATURE/);
  });

  it("image ships inference-config.js next to the proxy", () => {
    assert.match(dockerfile, /COPY inference-config\.js \/app\/inference-config\.js/);
  });
});
