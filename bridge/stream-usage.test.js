// The proxy's final SSE chunk carries Bedrock's token usage. OpenClaw records it
// on the turn and its preflight compaction sizes the session from it; without
// it OpenClaw falls back to a character estimate that undercounts tool-heavy
// sessions, and the session is never compacted.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const { streamUsage } = require("./inference-config");

const source = fs.readFileSync(require.resolve("./agentcore-proxy"), "utf8");

function slice(start, end) {
  const a = source.indexOf(start);
  const b = source.indexOf(end, a);
  assert.ok(a > 0 && b > a, `source boundaries: ${start}`);
  return source.slice(a, b);
}

function stream(events) {
  const sdk = {
    ConverseStreamCommand: class { constructor(input) { this.input = input; } },
    BedrockRuntimeClient: class {
      async send() { return { stream: (async function* () { yield* events; })() }; }
    },
  };
  const box = {
    require(name) {
      assert.equal(name, "@aws-sdk/client-bedrock-runtime");
      return sdk;
    },
    console: { log() {}, error() {}, warn() {}, debug() {} },
    AWS_REGION: "us-west-2", MAX_RETRIES: 1, BASE_DELAY_MS: 0, Date, JSON, Math,
    sleep: async () => {}, bedrockClientOptions: () => ({}), bedrockStreamClientOptions: () => ({}),
    resolveModelId: () => "global.anthropic.claude-opus-5-5",
    convertMessages: () => ({ bedrockMessages: [{ role: "user", content: [{ text: "hi" }] }], systemText: "s" }),
    scopeGuardrailToLatestUserTurn: (messages) => messages,
    reconcileToolHistory: (messages) => messages,
    buildInferenceConfig: require("./inference-config").buildInferenceConfig,
    streamUsage, temperatureOverride: undefined, guardrailConfig: undefined,
  };
  vm.createContext(box);
  vm.runInContext(slice("async function invokeBedrockStreaming(", "\n/**\n * Format a response"), box);
  const res = new EventEmitter();
  res.frames = [];
  res.writeHead = () => { res.headersSent = true; };
  res.write = (frame) => { res.frames.push(frame); return true; };
  res.end = () => { res.writableEnded = true; };
  return box.invokeBedrockStreaming([], res, "bedrock-agentcore", "s", undefined).then(() =>
    res.frames.filter((frame) => frame !== "data: [DONE]\n\n").map((frame) => JSON.parse(frame.slice(6))));
}

test("the final stream chunk reports Bedrock usage with cache tokens in prompt_tokens", async () => {
  const chunks = await stream([
    { contentBlockDelta: { delta: { text: "answer" } } },
    { messageStop: { stopReason: "end_turn" } },
    { metadata: { usage: { inputTokens: 1200, outputTokens: 80, cacheReadInputTokens: 190000,
      cacheWriteInputTokens: 16000 } } },
  ]);
  const final = chunks.at(-1);
  assert.equal(final.choices[0].finish_reason, "stop");
  assert.deepEqual(final.usage, {
    prompt_tokens: 207200, completion_tokens: 80, total_tokens: 207280,
    prompt_tokens_details: { cached_tokens: 190000, cache_write_tokens: 16000 },
  });
  assert.ok(chunks.slice(0, -1).every((chunk) => chunk.usage === undefined));
});

test("a stream without Bedrock metadata reports no usage", async () => {
  const chunks = await stream([
    { contentBlockDelta: { delta: { text: "answer" } } },
    { messageStop: { stopReason: "end_turn" } },
  ]);
  assert.equal(Object.hasOwn(chunks.at(-1), "usage"), false);
});

test("streamUsage ignores missing or invalid counts", () => {
  assert.deepEqual(streamUsage({ inputTokens: 10, outputTokens: -1, cacheReadInputTokens: "5" }), {
    prompt_tokens: 10, completion_tokens: 0, total_tokens: 10,
    prompt_tokens_details: { cached_tokens: 0, cache_write_tokens: 0 },
  });
});

test("pinned OpenClaw reads the proxy's final-chunk usage as the turn's prompt size", {
  skip: !process.env.OPENCLAW_SOURCE_ROOT && "Set OPENCLAW_SOURCE_ROOT to the pinned 2026.9.5 source fixture",
}, () => {
  const path = require("node:path");
  const root = process.env.OPENCLAW_SOURCE_ROOT;
  assert.equal(JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8")).version, "2026.9.5");
  const worker = fs.readFileSync(path.join(root, "dist/worker/worker.mjs"), "utf8");
  const start = worker.indexOf("function parseOpenAICompletionsUsage(");
  const end = worker.indexOf("function createOpenAIResponseHook(", start);
  assert.ok(start >= 0 && end > start, "Pinned source boundaries: parseOpenAICompletionsUsage");
  assert.match(worker, /\.usage&&\([A-Za-z_$][\w$]*\.usage=parseOpenAICompletionsUsage\(/);
  const box = { calculateCost() {}, applyProviderReportedUsageCost() {} };
  vm.createContext(box);
  vm.runInContext(worker.slice(start, end), box);
  const usage = box.parseOpenAICompletionsUsage(streamUsage({ inputTokens: 1200, outputTokens: 80,
    cacheReadInputTokens: 190000, cacheWriteInputTokens: 16000 }), {}, {});
  assert.equal(usage.input, 1200);
  assert.equal(usage.cacheRead, 190000);
  assert.equal(usage.cacheWrite, 16000);
  assert.equal(usage.totalTokens, 207280);
});
