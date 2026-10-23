/**
 * Text the guardrail stopped must not reach the model as untagged history.
 * Run: cd bridge && node --test guardrail-blocked-input.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");

const {
  scopeGuardrailToLatestUserTurn, createBlockedInputRegistry, BLOCKED_INPUT_PLACEHOLDER,
} = require("./guardrail-scope");

const u = (...texts) => ({ role: "user", content: texts.map((t) => ({ text: t })) });
const a = (text) => ({ role: "assistant", content: [{ text }] });
const guarded = (t) => ({ guardContent: { text: { text: t } } });
const BLOCKED = "My card is 4539 1488 0343 6467";

describe("createBlockedInputRegistry", () => {
  it("replaces recorded text in untagged history and leaves the tagged turn for the guardrail", () => {
    const registry = createBlockedInputRegistry();
    registry.record(scopeGuardrailToLatestUserTurn([u(BLOCKED)]));
    const next = scopeGuardrailToLatestUserTurn([u(BLOCKED), a("I can't process that request."), u("hello")]);
    const out = registry.redact(next);
    assert.deepEqual(out[0].content, [{ text: BLOCKED_INPUT_PLACEHOLDER }]);
    assert.deepEqual(out[1], next[1]);
    assert.deepEqual(out[2].content, [guarded("hello")]);
    // Sending the same text again: it is tagged, so the guardrail assesses it again.
    const retry = scopeGuardrailToLatestUserTurn([u(BLOCKED), a("I can't process that request."), u(BLOCKED)]);
    assert.deepEqual(registry.redact(retry)[2].content, [guarded(BLOCKED)]);
  });

  it("records what was assessed, including the earlier turn tagged during a tool continuation", () => {
    const registry = createBlockedInputRegistry();
    const request = scopeGuardrailToLatestUserTurn([u(BLOCKED),
      { role: "assistant", content: [{ toolUse: { toolUseId: "t1", name: "exec", input: {} } }] },
      { role: "user", content: [{ toolResult: { toolUseId: "t1", content: [{ text: "ok" }] } }] }]);
    registry.record(request);
    assert.equal(registry.size(), 1);
    assert.deepEqual(registry.redact([u(BLOCKED), a("x"), u("next")])[0].content,
      [{ text: BLOCKED_INPUT_PLACEHOLDER }]);
  });

  it("ignores untagged text when recording and whitespace differences when matching", () => {
    const registry = createBlockedInputRegistry();
    registry.record([u("history that was not assessed"), { role: "user", content: [guarded(BLOCKED)] }]);
    assert.equal(registry.size(), 1);
    const out = registry.redact([u(`  ${BLOCKED.replace(/ /g, "  ")}\n`), u("history that was not assessed")]);
    assert.deepEqual(out[0].content, [{ text: BLOCKED_INPUT_PLACEHOLDER }]);
    assert.deepEqual(out[1].content, [{ text: "history that was not assessed" }]);
  });

  it("leaves tool, image and unrelated blocks alone and does not mutate its input", () => {
    const registry = createBlockedInputRegistry();
    registry.record([{ role: "user", content: [guarded(BLOCKED)] }]);
    const messages = [{ role: "user", content: [
      { toolResult: { toolUseId: "t1", content: [{ text: BLOCKED }] } },
      { image: { format: "png", source: { bytes: new Uint8Array(1) } } },
      { text: BLOCKED }, { text: "fine" },
    ] }];
    const snapshot = structuredClone(messages);
    const out = registry.redact(messages);
    assert.deepEqual(messages, snapshot);
    assert.deepEqual(out[0].content[0], messages[0].content[0]);
    assert.deepEqual(out[0].content[1], messages[0].content[1]);
    assert.deepEqual(out[0].content[2], { text: BLOCKED_INPUT_PLACEHOLDER });
    assert.deepEqual(out[0].content[3], { text: "fine" });
    assert.equal(registry.redact([u("fine")])[0].content[0].text, "fine");
  });

  it("returns the same array when nothing matches, and is bounded", () => {
    const registry = createBlockedInputRegistry(4);
    const messages = [u("hello")];
    assert.equal(registry.redact(messages), messages);
    for (let i = 0; i < 10; i++) registry.record([{ role: "user", content: [guarded(`blocked ${i}`)] }]);
    assert.equal(registry.size(), 4);
    assert.equal(registry.redact([u("blocked 0")])[0].content[0].text, "blocked 0", "oldest evicted");
    assert.equal(registry.redact([u("blocked 9")])[0].content[0].text, BLOCKED_INPUT_PLACEHOLDER);
  });
});

// Full proxy module with mocked SDKs: a request the guardrail stops, then the next turn.
function proxy({ guardrail = true, stream = false } = {}) {
  let handler;
  const model = [];
  const replies = [];
  class Command { constructor(input) { this.input = input; } }
  const sdk = {
    GetObjectCommand: Command, PutObjectCommand: Command,
    S3Client: class { async send() { const e = new Error("absent"); e.name = "NoSuchKey"; throw e; } },
    ConverseCommand: Command, ConverseStreamCommand: Command,
    BedrockRuntimeClient: class {
      async send(command) {
        model.push(structuredClone(command.input));
        const reply = replies.shift() || { text: "answer", stopReason: "end_turn" };
        if (!stream) {
          return { output: { message: { content: [{ text: reply.text }] } }, stopReason: reply.stopReason, usage: {} };
        }
        return { stream: (async function* () {
          yield { contentBlockDelta: { contentBlockIndex: 0, delta: { text: reply.text } } };
          yield { messageStop: { stopReason: reply.stopReason } };
          yield { metadata: { usage: { inputTokens: 1, outputTokens: 1 } } };
        })() };
      }
    },
  };
  const proc = new EventEmitter();
  proc.env = {
    AWS_REGION: "us-west-2", USER_ID: "slack:owner", CHANNEL: "slack",
    ...(guardrail ? { BEDROCK_GUARDRAIL_ID: "test-guard", BEDROCK_GUARDRAIL_VERSION: "1" } : {}),
  };
  proc.connected = false;
  proc.exit = () => assert.fail("Proxy must not exit");
  const mocks = {
    http: { createServer(callback) { handler = callback; return { listen(_p, _h, cb) { cb(); } }; } },
    fs: { readFileSync() { return JSON.stringify({ actorId: "slack:owner", channel: "slack" }); },
      readdirSync: () => [], existsSync: () => false },
    "./cognito-token": { createCognitoTokenProvider: () => ({ _cache: new Map(), getIdToken: async () => null }) },
    "./proxy-sdk-snapshot": { preparedSdk: () => null },
    "./read-body": { readBody: async (req) => JSON.stringify(req.body) },
    "@aws-sdk/client-s3": sdk,
    "@aws-sdk/client-bedrock-runtime": sdk,
  };
  vm.runInNewContext(fs.readFileSync(require.resolve("./agentcore-proxy"), "utf8"), {
    require: (name) => mocks[name] || require(name), process: proc, Buffer, URL, structuredClone,
    console: { log() {}, warn() {}, error() {}, debug() {} }, setTimeout, clearTimeout,
  }, { filename: "agentcore-proxy.js" });
  return {
    model, replies,
    chat(messages) {
      const req = { method: "POST", url: "/v1/chat/completions", headers: {},
        body: { model: "bedrock-agentcore", stream, messages } };
      return new Promise((resolve) => {
        const res = new EventEmitter();
        res.writeHead = (status) => { res.status = status; res.headersSent = true; };
        res.write = () => true;
        res.end = () => { res.writableEnded = true; resolve(res.status); };
        handler(req, res);
      });
    },
  };
}

for (const stream of [false, true]) {
  describe(`proxy (stream=${stream})`, () => {
    it("does not send text the guardrail stopped as untagged history on the next turn", async () => {
      const app = proxy({ stream });
      app.replies.push({ text: "I can't process that request.", stopReason: "guardrail_intervened" });
      await app.chat([{ role: "user", content: BLOCKED }]);
      assert.deepEqual(app.model[0].messages.at(-1).content, [guarded(BLOCKED)]);
      await app.chat([{ role: "user", content: BLOCKED },
        { role: "assistant", content: "I can't process that request." }, { role: "user", content: "hello" }]);
      const next = app.model[1].messages;
      assert.deepEqual(next[0].content, [{ text: BLOCKED_INPUT_PLACEHOLDER }]);
      assert.deepEqual(next.at(-1).content, [guarded("hello")]);
      assert.ok(!JSON.stringify(next).includes("4539"), "stopped text must not reach the model");
    });

    it("leaves history unchanged when no request was stopped", async () => {
      const app = proxy({ stream });
      await app.chat([{ role: "user", content: BLOCKED }]);
      await app.chat([{ role: "user", content: BLOCKED },
        { role: "assistant", content: "ok" }, { role: "user", content: "hello" }]);
      assert.deepEqual(app.model[1].messages[0].content, [{ text: BLOCKED }]);
    });

    it("does nothing without a guardrail", async () => {
      const app = proxy({ stream, guardrail: false });
      app.replies.push({ text: "no", stopReason: "guardrail_intervened" });
      await app.chat([{ role: "user", content: BLOCKED }]);
      await app.chat([{ role: "user", content: BLOCKED },
        { role: "assistant", content: "no" }, { role: "user", content: "hello" }]);
      assert.deepEqual(app.model[1].messages[0].content, [{ text: BLOCKED }]);
    });
  });
}
