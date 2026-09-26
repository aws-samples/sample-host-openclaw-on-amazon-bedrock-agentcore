/**
 * Guards the Bedrock Guardrails env hand-off between the contract and the proxy.
 *
 * agentcore-contract.js spawns agentcore-proxy.js with an explicit env
 * allowlist (no process.env spread). agentcore-proxy.js decides at load time
 * whether to attach guardrailConfig to Converse calls from
 * BEDROCK_GUARDRAIL_ID / BEDROCK_GUARDRAIL_VERSION. If the allowlist drops
 * either name, guardrails are silently disabled even though the runtime
 * environment carries them (this happened: the proxy logged no
 * "Bedrock Guardrails enabled" line and a test card number passed through).
 *
 * The contract is a server entry point and cannot be required in a test, so
 * this checks the source text of the proxyEnv block directly.
 * Run: cd bridge && node --test proxy-guardrail-env.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

const contractSrc = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf-8");
const proxySrc = fs.readFileSync(path.join(__dirname, "agentcore-proxy.js"), "utf-8");

function proxyEnvBlock() {
  const start = contractSrc.indexOf("const proxyEnv = {");
  assert.notEqual(start, -1, "agentcore-contract.js must build a proxyEnv allowlist");
  const end = contractSrc.indexOf("};", start);
  assert.notEqual(end, -1);
  return contractSrc.slice(start, end);
}

describe("proxy guardrail env hand-off", () => {
  it("contract passes BEDROCK_GUARDRAIL_ID and BEDROCK_GUARDRAIL_VERSION to the proxy child", () => {
    const block = proxyEnvBlock();
    assert.match(block, /BEDROCK_GUARDRAIL_ID:\s*process\.env\.BEDROCK_GUARDRAIL_ID/);
    assert.match(block, /BEDROCK_GUARDRAIL_VERSION:\s*process\.env\.BEDROCK_GUARDRAIL_VERSION/);
  });

  it("proxy reads the same names the contract forwards", () => {
    assert.match(proxySrc, /process\.env\.BEDROCK_GUARDRAIL_ID/);
    assert.match(proxySrc, /process\.env\.BEDROCK_GUARDRAIL_VERSION/);
  });

  it("proxy env stays an explicit allowlist (no process.env spread)", () => {
    assert.doesNotMatch(proxyEnvBlock(), /\.\.\.process\.env/);
  });
});

describe("workspace AGENTS.md injection keeps the contract's Browser section", () => {
  // The proxy injects the contract-written AGENTS.md (read from S3) into the system
  // prompt, truncated to WORKSPACE_PER_FILE_MAX_CHARS. The Browser and Sub-agents
  // sections sit past 5 KB, so a 4096 cap dropped them and the model denied having
  // a browser skill while the browser session was up. Guard the cap here.
  it("per-file cap is at least 8192 chars", () => {
    const m = proxySrc.match(/const WORKSPACE_PER_FILE_MAX_CHARS = (\d+);/);
    assert.ok(m, "proxy must define WORKSPACE_PER_FILE_MAX_CHARS");
    assert.ok(Number(m[1]) >= 8192, `per-file cap ${m[1]} is too small for the contract AGENTS.md`);
  });
  it("contract still writes the Browser section the cap must cover", () => {
    assert.match(contractSrc, /"## Browser \(AgentCore Browser\)"/);
  });
});
