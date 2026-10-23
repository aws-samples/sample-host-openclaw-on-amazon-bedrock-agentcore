/**
 * Requests whose history carries toolUse/toolResult blocks but whose current
 * request has no tools must still be valid Converse requests.
 * Run: node --test tool-history.test.js
 *
 * The integration tests run the real convertMessages, invokeBedrock and
 * invokeBedrockStreaming bodies from agentcore-proxy.js against a fake SDK
 * that applies Bedrock's rule: toolUse/toolResult blocks require toolConfig.
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const policy = require("./model-policy");

const source = fs.readFileSync(path.join(__dirname, "agentcore-proxy.js"), "utf8");

// Lazy so the integration tests below still run (and fail on their own
// assertion) against a proxy that predates the module.
function toolHistory() {
  return require("./tool-history");
}

// An OpenClaw history from a previous run: a tool call, its result, and the
// model's answer, followed by the user's new prompt.
const HISTORY = [
  { role: "system", content: "system" },
  { role: "user", content: "run the morning brief" },
  { role: "assistant", content: "", tool_calls: [
    { id: "call-1", type: "function", function: { name: "exec", arguments: "{\"command\":\"python3 brief.py\"}" } },
  ] },
  { role: "tool", tool_call_id: "call-1", content: "brief written" },
  { role: "assistant", content: "Done, the brief is ready." },
  { role: "user", content: "now the evening brief" },
];

function bedrockRule(params) {
  const blocks = (params.messages || []).flatMap((m) => m.content || []);
  if (!params.toolConfig && blocks.some((b) => b.toolUse || b.toolResult)) {
    throw Object.assign(new Error(
      "The toolConfig field must be defined when using toolUse and toolResult content blocks."),
    { name: "ValidationException", $metadata: { httpStatusCode: 400 } });
  }
}

function slice(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start > 0 && end > start, `proxy source marker missing: ${startMarker}`);
  return source.slice(start, end);
}

function proxy({ guardrail = false } = {}) {
  const requests = [];
  const sdk = {
    ConverseCommand: class { constructor(input) { this.input = input; } },
    ConverseStreamCommand: class { constructor(input) { this.input = input; } },
    BedrockRuntimeClient: class {
      async send(command) {
        requests.push(command.input);
        bedrockRule(command.input);
        if (command instanceof sdk.ConverseStreamCommand) {
          return { $metadata: { requestId: "r", attempts: 1 }, stream: (async function* () {
            yield { contentBlockDelta: { delta: { text: "answer" } } };
            yield { messageStop: { stopReason: "end_turn" } };
          })() };
        }
        return { output: { message: { content: [{ text: "answer" }] } }, usage: {} };
      }
    },
  };
  let reconcile;
  try { reconcile = toolHistory().reconcileToolHistory; } catch { reconcile = undefined; }
  const box = {
    reconcileToolHistory: reconcile,
    modelPolicy: { ...policy, createStreamingRoute: (options) =>
      policy.createStreamingRoute({ ...options, emit: () => {} }) },
    preparedSdk: () => sdk,
    MODEL_ID: policy.PRIMARY_MODEL_ID, FALLBACK_MODEL_ID: policy.FALLBACK_MODEL_ID,
    AWS_REGION: "us-west-2", MAX_RETRIES: 2, BASE_DELAY_MS: 0, SYSTEM_PROMPT: "default",
    sleep: async () => {}, bedrockClientOptions: () => ({}), bedrockStreamClientOptions: () => ({}), structuredClone,
    extractSystemText: require("./system-messages").extractSystemText,
    scopeGuardrailToLatestUserTurn: require("./guardrail-scope").scopeGuardrailToLatestUserTurn,
    blockedGuardrailInput: require("./guardrail-scope").createBlockedInputRegistry(),
    guardrailConfig: guardrail ? { guardrailIdentifier: "test-guard", guardrailVersion: "1" } : undefined,
    buildInferenceConfig: require("./inference-config").buildInferenceConfig,
    temperatureOverride: undefined,
    systemPrefixCache: { apply: (_prefix, system) => system },
    addToolPrefixCachePoint: require("./system-prefix-cache").addToolPrefixCachePoint,
    console: { log() {}, error() {}, warn() {}, debug() {} },
    modelOutcomeUnknown: false,
    SUBAGENT_BEDROCK_MODEL_ID: "subagent-model", SUBAGENT_MODEL_NAME: "bedrock-agentcore-subagent",
  };
  vm.createContext(box);
  vm.runInContext(slice("function convertMessages(", "\n/**\n * Determine if the incoming request"), box);
  vm.runInContext(slice("function isSubagentRequest(", "\n/**\n * Call Bedrock Converse API"), box);
  vm.runInContext(slice("async function invokeBedrock(messages", "\n/**\n * Call Bedrock ConverseStream API"), box);
  vm.runInContext(slice("async function invokeBedrockStreaming(", "\n/**\n * Format a response"), box);
  const response = () => {
    const res = new EventEmitter();
    res.writeHead = () => { res.headersSent = true; };
    res.write = () => true;
    res.end = () => { res.writableEnded = true; };
    return res;
  };
  return {
    requests,
    stream: (model, toolConfig) => box.invokeBedrockStreaming(HISTORY, response(), model, "system", toolConfig),
    converse: (model, toolConfig) => box.invokeBedrock(HISTORY, "system", toolConfig, model),
  };
}

const TOOLS = { tools: [{ toolSpec: { name: "exec", inputSchema: { json: { type: "object" } } } }] };
const blocksOf = (params) => params.messages.flatMap((m) => m.content);

describe("tool history without tools on the current request", () => {
  for (const [label, model] of [["main model", "bedrock-agentcore"], ["sub-agent", "bedrock-agentcore-subagent"]]) {
    it(`streaming ${label}: request is valid and tool blocks become text`, async () => {
      const app = proxy();
      assert.equal(await app.stream(model, undefined), "answer");
      assert.equal(app.requests.length, 1);
      const params = app.requests[0];
      assert.equal(params.toolConfig, undefined);
      assert.ok(!blocksOf(params).some((b) => b.toolUse || b.toolResult));
      const text = blocksOf(params).map((b) => b.text).join("\n");
      assert.match(text, /\[Tool call exec \(id call-1\)\] \{"command":"python3 brief\.py"\}/);
      assert.match(text, /\[Tool result \(id call-1\)\]\nbrief written/);
      assert.deepEqual(params.messages.map((m) => m.role), ["user", "assistant", "user", "assistant", "user"]);
    });

    it(`non-streaming ${label}: request is valid and tool blocks become text`, async () => {
      const app = proxy();
      const result = await app.converse(model, undefined);
      assert.equal(result.text, "answer");
      assert.equal(app.requests.length, 1);
      assert.ok(!blocksOf(app.requests[0]).some((b) => b.toolUse || b.toolResult));
    });
  }

  it("streaming with a guardrail still tags only the latest user prompt", async () => {
    const app = proxy({ guardrail: true });
    assert.equal(await app.stream("bedrock-agentcore", undefined), "answer");
    const params = app.requests[0];
    assert.ok(!blocksOf(params).some((b) => b.toolUse || b.toolResult));
    const guarded = blocksOf(params).filter((b) => b.guardContent).map((b) => b.guardContent.text.text);
    assert.deepEqual(guarded, ["now the evening brief"]);
  });

  it("requests that carry tools keep structured toolUse/toolResult blocks", async () => {
    const app = proxy();
    assert.equal(await app.stream("bedrock-agentcore", TOOLS), "answer");
    const blocks = blocksOf(app.requests[0]);
    assert.ok(blocks.some((b) => b.toolUse && b.toolUse.name === "exec"));
    assert.ok(blocks.some((b) => b.toolResult && b.toolResult.toolUseId === "call-1"));
    assert.ok(app.requests[0].toolConfig);
  });
});

describe("reconcileToolHistory", () => {
  const messages = [
    { role: "assistant", content: [{ text: "checking" }, { toolUse: { toolUseId: "t1", name: "read", input: { path: "a" } } }] },
    { role: "user", content: [
      { toolResult: { toolUseId: "t1", status: "error", content: [{ text: "boom" }, { json: { code: 1 } }, { image: {} }] } },
    ] },
  ];

  it("returns the same array when a toolConfig is present", () => {
    assert.equal(toolHistory().reconcileToolHistory(messages, TOOLS), messages);
  });

  it("returns the same array when history has no tool blocks", () => {
    const plain = [{ role: "user", content: [{ text: "hi" }] }];
    assert.equal(toolHistory().reconcileToolHistory(plain, undefined), plain);
  });

  it("rewrites tool blocks as non-empty text without mutating the input", () => {
    const before = structuredClone(messages);
    const out = toolHistory().reconcileToolHistory(messages, undefined);
    assert.deepEqual(messages, before);
    assert.deepEqual(out[0].content[0], { text: "checking" });
    assert.equal(out[0].content[1].text, "[Tool call read (id t1)] {\"path\":\"a\"}");
    assert.equal(out[1].content[0].text,
      "[Tool result error (id t1)]\nboom\n{\"code\":1}\n[non-text tool output omitted]");
    assert.ok(!toolHistory().hasToolBlocks(out));
  });

  it("clips very large tool inputs and labels empty results", () => {
    const big = [{ role: "assistant", content: [{ toolUse: { toolUseId: "t", name: "w", input: { s: "x".repeat(10000) } } }] },
      { role: "user", content: [{ toolResult: { toolUseId: "t", content: [] } }] }];
    const out = toolHistory().reconcileToolHistory(big, undefined);
    assert.ok(out[0].content[0].text.length < 4100);
    assert.match(out[0].content[0].text, /…\[truncated\]$/);
    assert.equal(out[1].content[0].text, "[Tool result (id t)]\n(empty)");
  });

  it("is copied into the image", () => {
    const docker = fs.readFileSync(path.join(__dirname, "Dockerfile"), "utf8");
    assert.match(docker, /^COPY tool-history\.js \/app\/tool-history\.js$/m);
  });
});
