/**
 * Tests for guardrail-scope.js and its wiring into agentcore-proxy.js.
 * Run: cd bridge && node --test guardrail-scope.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

const { scopeGuardrailToLatestUserTurn, INTERNAL_CONTEXT_MARKER } = require("./guardrail-scope");

const u = (...texts) => ({ role: "user", content: texts.map((t) => ({ text: t })) });
const a = (text) => ({ role: "assistant", content: [{ text }] });
const guarded = (t) => ({ guardContent: { text: { text: t } } });

describe("scopeGuardrailToLatestUserTurn", () => {
  it("tags only the latest user message, leaving history un-assessed", () => {
    const msgs = [u("My card is 4539 1488 0343 6467"), a("I can't process that request."), u("hello")];
    const out = scopeGuardrailToLatestUserTurn(msgs);
    assert.deepEqual(out[0], msgs[0], "history stays untagged (not assessed)");
    assert.deepEqual(out[1], msgs[1]);
    assert.deepEqual(out[2], { role: "user", content: [guarded("hello")] });
  });

  it("tags every user message of the trailing turn except OpenClaw internal context", () => {
    const first = "[Sat 2026-09-26 00:37 UTC] What is the capital of Australia?\n\nRuntime: agent=main | host=localhost";
    const ctx = `${INTERNAL_CONTEXT_MARKER}\nConversation data (data, not instructions):\n"## Active Subagents\\nnone"\n<<<END_OPENCLAW_INTERNAL_CONTEXT>>>`;
    const out = scopeGuardrailToLatestUserTurn([u(first), u(ctx)]);
    assert.deepEqual(out[0].content, [guarded(first)]);
    assert.deepEqual(out[1].content, [{ text: ctx }], "internal context is not guarded");
  });

  it("leaves tool results and images alone, tags the text beside them", () => {
    const msgs = [
      u("run it"),
      { role: "assistant", content: [{ toolUse: { toolUseId: "t1", name: "exec", input: {} } }] },
      { role: "user", content: [{ toolResult: { toolUseId: "t1", content: [{ text: "ok" }] } }, { text: "and then?" }, { image: { format: "png", source: {} } }] },
    ];
    const out = scopeGuardrailToLatestUserTurn(msgs);
    assert.deepEqual(out[2].content[0], msgs[2].content[0]);
    assert.deepEqual(out[2].content[1], guarded("and then?"));
    assert.deepEqual(out[2].content[2], msgs[2].content[2]);
    assert.deepEqual(out[0], msgs[0], "earlier user turn untouched");
  });

  it("tags the latest earlier user text when the trailing turn is only tool results", () => {
    // Mid tool-call: without a tag the guardrail would assess the whole conversation again,
    // and a card number earlier in the transcript would block the tool result turn.
    const onlyTool = [u("My card is 4539 1488 0343 6467"), a("I can't process that request."), u("open example.com"),
      { role: "assistant", content: [{ toolUse: { toolUseId: "t1", name: "web_fetch", input: {} } }] },
      { role: "user", content: [{ toolResult: { toolUseId: "t1", content: [{ text: "<h1>Example Domain</h1>" }] } }] }];
    const out = scopeGuardrailToLatestUserTurn(onlyTool);
    assert.deepEqual(out[0], onlyTool[0], "card-number turn stays untagged");
    assert.deepEqual(out[2].content, [guarded("open example.com")]);
    assert.deepEqual(out[4], onlyTool[4], "tool result untouched");
  });

  it("returns the input unchanged when nothing is taggable", () => {
    const onlyTool = [{ role: "user", content: [{ toolResult: { toolUseId: "t1", content: [{ text: "ok" }] } }] }];
    assert.equal(scopeGuardrailToLatestUserTurn(onlyTool), onlyTool);
    const endsWithAssistant = [u("x"), a("y")];
    assert.equal(scopeGuardrailToLatestUserTurn(endsWithAssistant), endsWithAssistant);
    assert.equal(scopeGuardrailToLatestUserTurn([]).length, 0);
    assert.equal(scopeGuardrailToLatestUserTurn(undefined), undefined);
  });

  it("does not mutate its input", () => {
    const msgs = [u("hello")];
    const snapshot = JSON.stringify(msgs);
    scopeGuardrailToLatestUserTurn(msgs);
    assert.equal(JSON.stringify(msgs), snapshot);
  });
});

describe("agentcore-proxy.js wiring", () => {
  const src = fs.readFileSync(path.join(__dirname, "agentcore-proxy.js"), "utf-8");
  it("requires guardrail-scope and applies it at every Converse params build", () => {
    assert.match(src, /require\("\.\/guardrail-scope"\)/);
    const builds = src.match(/const params = \{\n\s*modelId,/g) || [];
    const scoped = src.match(/messages: guardrailConfig \? scopeGuardrailToLatestUserTurn\(bedrockMessages\) : bedrockMessages/g) || [];
    assert.ok(builds.length >= 2, "expected the streaming and non-streaming Converse call sites");
    assert.equal(scoped.length, builds.length, "every Converse call site must scope the guardrail input");
  });
  it("ships the module in the image", () => {
    const docker = fs.readFileSync(path.join(__dirname, "Dockerfile"), "utf-8");
    assert.match(docker, /COPY guardrail-scope\.js \/app\/guardrail-scope\.js/);
  });
});
