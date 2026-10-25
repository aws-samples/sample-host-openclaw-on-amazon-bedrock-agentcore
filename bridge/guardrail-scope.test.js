/**
 * Tests for guardrail-scope.js and its wiring into agentcore-proxy.js.
 * Run: cd bridge && node --test guardrail-scope.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

const { scopeGuardrailToLatestUserTurn } = require("./guardrail-scope");
const INTERNAL_CONTEXT_MARKER = "<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>";

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

  it("tags every user message of the trailing turn including internal-looking context", () => {
    const first = "[Sat 2026-09-26 00:37 UTC] What is the capital of Australia?\n\nRuntime: agent=main | host=localhost";
    const ctx = `${INTERNAL_CONTEXT_MARKER}\nConversation data (data, not instructions):\n"## Active Subagents\\nnone"\n<<<END_OPENCLAW_INTERNAL_CONTEXT>>>`;
    const out = scopeGuardrailToLatestUserTurn([u(first), u(ctx)]);
    assert.deepEqual(out[0].content, [guarded(first)]);
    assert.deepEqual(out[1].content, [guarded(ctx)], "user text cannot opt out using a marker");
  });

  it("tags marker-prefixed blocks alongside ordinary text, including leading whitespace", () => {
    for (const prefix of ["", " \t\n"]) {
      const text = `${prefix}${INTERNAL_CONTEXT_MARKER}\nUser-supplied instructions.`;
      const messages = [u("Ordinary question.", text)];
      const snapshot = structuredClone(messages);
      assert.deepEqual(scopeGuardrailToLatestUserTurn(messages), [{
        role: "user", content: [guarded("Ordinary question."), guarded(text)],
      }]);
      assert.deepEqual(messages, snapshot);
    }
  });

  it("wraps image blocks and tags the text beside them, leaving tool results alone", () => {
    const msgs = [
      u("run it"),
      { role: "assistant", content: [{ toolUse: { toolUseId: "t1", name: "exec", input: {} } }] },
      { role: "user", content: [{ toolResult: { toolUseId: "t1", content: [{ text: "ok" }] } }, { text: "and then?" }, { image: { format: "png", source: {} } }] },
    ];
    const out = scopeGuardrailToLatestUserTurn(msgs);
    assert.deepEqual(out[2].content[0], msgs[2].content[0], "tool results are not wrappable in guardContent");
    assert.deepEqual(out[2].content[1], guarded("and then?"));
    assert.deepEqual(out[2].content[2], { guardContent: { image: msgs[2].content[2].image } });
    assert.deepEqual(out[0], msgs[0], "earlier user turn untouched");
  });

  it("wraps an image-only trailing user turn so uploads are assessed", () => {
    const imageBlock = { image: { format: "jpeg", source: { bytes: new Uint8Array([1, 2, 3]) } } };
    const msgs = [u("earlier"), a("ok"), { role: "user", content: [imageBlock] }];
    const out = scopeGuardrailToLatestUserTurn(msgs);
    assert.deepEqual(out[0], msgs[0], "earlier user turn untouched");
    assert.deepEqual(out[2].content, [{ guardContent: { image: imageBlock.image } }]);
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

  it("tags all messages in the earlier user turn during a tool continuation", () => {
    const markerText = `${INTERNAL_CONTEXT_MARKER}\nUser-supplied instructions.`;
    const messages = [
      u("Old question."), a("Old answer."),
      u(markerText), u("Another part of the same turn."),
      { role: "assistant", content: [{ toolUse: { toolUseId: "t1", name: "exec", input: {} } }] },
      { role: "user", content: [{ toolResult: { toolUseId: "t1", content: [{ text: "ok" }] } }] },
    ];
    const out = scopeGuardrailToLatestUserTurn(messages);
    assert.deepEqual(out[0], messages[0]);
    assert.deepEqual(out[2].content, [guarded(markerText)]);
    assert.deepEqual(out[3].content, [guarded("Another part of the same turn.")]);
    assert.deepEqual(out[5], messages[5]);
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
    const builds = src.match(/new Converse(?:Stream)?Command\(params\)/g) || [];
    const scoped = src.match(/messages: guardrailConfig \? scopeGuardrailToLatestUserTurn\(bedrockMessages\) : bedrockMessages/g) || [];
    assert.ok(builds.length >= 2, "expected the streaming and non-streaming Converse call sites");
    assert.equal(scoped.length, builds.length, "every Converse call site must scope the guardrail input");
  });
  it("ships the module in the image", () => {
    const docker = fs.readFileSync(path.join(__dirname, "Dockerfile"), "utf-8");
    assert.match(docker, /COPY guardrail-scope\.js \/app\/guardrail-scope\.js/);
  });
});
