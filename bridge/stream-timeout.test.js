// A streaming Bedrock request must wait long enough for the model's first event.
// Staging, 17-18 Oct: three scheduled tasks ended when Opus 5.5 took over 90 s to
// start a stream on a 135k-165k token prompt ("no activity for 90000 ms").
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");

const source = fs.readFileSync(require.resolve("./agentcore-proxy"), "utf8");

function slice(start, end) {
  const a = source.indexOf(start);
  const b = source.indexOf(end, a);
  assert.ok(a > 0 && b > a, `source boundaries: ${start}`);
  return source.slice(a, b);
}

function proxy() {
  const clients = [];
  const sdk = {
    ConverseStreamCommand: class { constructor(input) { this.input = input; } },
    BedrockRuntimeClient: class {
      constructor(options) { clients.push(options); }
      async send() {
        return { stream: (async function* () { yield { messageStop: { stopReason: "end_turn" } }; })() };
      }
    },
  };
  const box = {
    require: () => sdk,
    console: { log() {}, error() {}, warn() {}, debug() {} },
    AWS_REGION: "us-west-2", BASE_DELAY_MS: 0, Date, JSON, Math,
    sleep: async () => {},
    resolveModelId: () => "global.anthropic.claude-opus-5-5",
    convertMessages: () => ({ bedrockMessages: [{ role: "user", content: [{ text: "hi" }] }], systemText: "s" }),
    scopeGuardrailToLatestUserTurn: (messages) => messages,
    reconcileToolHistory: (messages) => messages,
    buildInferenceConfig: require("./inference-config").buildInferenceConfig,
    streamUsage: require("./inference-config").streamUsage,
    temperatureOverride: undefined, guardrailConfig: undefined,
  };
  vm.createContext(box);
  vm.runInContext(slice("const MAX_RETRIES =", "// Session tracking"), box);
  vm.runInContext(slice("async function invokeBedrockStreaming(", "\n/**\n * Format a response"), box);
  vm.runInContext("globalThis.out = { MAX_RETRIES, bedrockClientOptions };", box);
  return { box, clients };
}

test("a streaming request waits longer than 90 s for Bedrock's first event", async () => {
  const { box, clients } = proxy();
  const res = new EventEmitter();
  res.writeHead = () => { res.headersSent = true; };
  res.write = () => true;
  res.end = () => { res.writableEnded = true; };
  await box.invokeBedrockStreaming([], res, "bedrock-agentcore", "s", undefined);
  const timeout = clients[0].requestHandler.requestTimeout;
  assert.ok(timeout >= 120_000, `stream timeout ${timeout} ms must cover a 120 s first token`);
  assert.ok(box.out.MAX_RETRIES * timeout < 620_000, "every attempt together must fit inside a 620 s turn");
  // Non-streaming Converse keeps its own 90 s limit (lightweight agent's 120 s timeout).
  assert.equal(box.out.bedrockClientOptions("global.anthropic.claude-opus-5-5").requestTimeout, 90_000);
});
