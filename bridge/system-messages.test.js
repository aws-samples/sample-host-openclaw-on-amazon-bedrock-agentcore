/**
 * Tests for system text extraction and pure proxy message conversion.
 * Run: node --test system-messages.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { extractSystemText } = require("./system-messages");
const { scopeGuardrailToLatestUserTurn } = require("./guardrail-scope");

const FALLBACK = "Default system instructions.";
const textBlock = (text) => ({
  type: "text",
  text,
  cache_control: { type: "ephemeral" },
});

function deepFreeze(value) {
  if (value && typeof value === "object") {
    Object.values(value).forEach(deepFreeze);
    Object.freeze(value);
  }
  return value;
}

describe("extractSystemText", () => {
  it("preserves string content including whitespace and line breaks", () => {
    const text = "  Follow these rules.\n\nKeep this spacing.\t\n";
    assert.equal(extractSystemText([{ role: "system", content: text }], FALLBACK), text);
  });

  it("keeps multiple system messages in order with newline separation", () => {
    const messages = [
      { role: "system", content: "First rule." },
      { role: "user", content: "Question." },
      { role: "system", content: "Second rule." },
      { role: "assistant", content: "Answer." },
      { role: "system", content: "Third rule." },
    ];
    assert.equal(extractSystemText(messages, FALLBACK), "First rule.\nSecond rule.\nThird rule.");
  });

  it("preserves all text blocks and mixed system formats without stringifying cache metadata", () => {
    const messages = deepFreeze([
      { role: "system", content: [textBlock("First rule."), textBlock("Second rule.")] },
      { role: "system", content: "Third rule." },
      { role: "system", content: [{ type: "text", text: "Fourth rule." }, textBlock("Runtime: agent=main")] },
    ]);
    assert.equal(
      extractSystemText(messages, FALLBACK),
      "First rule.\nSecond rule.\nThird rule.\nFourth rule.\nRuntime: agent=main",
    );
    assert.deepEqual(messages[0].content[0].cache_control, { type: "ephemeral" });
  });

  it("preserves whitespace, Runtime text and internal markers in system blocks", () => {
    const blocks = [
      textBlock("  Rules.\n"),
      textBlock("<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>\nRuntime: agent=main\n<<<END_OPENCLAW_INTERNAL_CONTEXT>>>"),
      textBlock("\nKeep all instructions.\t"),
    ];
    assert.equal(
      extractSystemText([{ role: "system", content: blocks }], FALLBACK),
      "  Rules.\n\n<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>\nRuntime: agent=main\n<<<END_OPENCLAW_INTERNAL_CONTEXT>>>\n\nKeep all instructions.\t",
    );
  });

  it("uses the supplied fallback only when there is no system message", () => {
    assert.equal(extractSystemText([], FALLBACK), FALLBACK);
    assert.equal(extractSystemText([{ role: "user", content: "Runtime: user-authored text." }], FALLBACK), FALLBACK);
    assert.equal(extractSystemText([], ""), "");
  });

  it("preserves explicitly empty system strings, arrays and text blocks", () => {
    for (const content of ["", [], [textBlock("")]]) {
      assert.equal(extractSystemText([{ role: "system", content }], FALLBACK), "");
    }
  });

  it("preserves empty blocks and messages between nonempty instructions", () => {
    const messages = [
      { role: "system", content: [textBlock("First."), textBlock(""), textBlock("Second.")] },
      { role: "system", content: [] },
      { role: "system", content: "Third." },
    ];
    assert.equal(extractSystemText(messages, FALLBACK), "First.\n\nSecond.\n\nThird.");
  });

  it("rejects missing system content instead of falling back", () => {
    assert.throws(
      () => extractSystemText([{ role: "system" }], FALLBACK),
      { name: "TypeError", message: "Invalid system content at message 0: expected a string or an array of text blocks." },
    );
  });

  it("rejects unsupported system content types without string conversion", () => {
    for (const content of [undefined, null, false, 0, { text: "Private instruction." }, new String("Private instruction.")]) {
      assert.throws(
        () => extractSystemText([{ role: "system", content }], FALLBACK),
        { name: "TypeError", message: "Invalid system content at message 0: expected a string or an array of text blocks." },
      );
    }
  });

  it("rejects malformed blocks even after valid instructions without exposing values", () => {
    const malformedBlocks = [
      null, undefined, false, 1, "Private instruction.", [],
      {}, { text: "Private instruction." },
      { type: "private-unsupported-type", text: "Private instruction." },
      { type: "image_url", image_url: { url: "private://image" } },
      { type: "text" }, { type: "text", text: null },
      { type: "text", text: 123 }, { type: "text", text: false },
      { type: "text", text: ["Private instruction."] },
      { type: "text", text: { value: "Private instruction." } },
    ];
    for (const block of malformedBlocks) {
      assert.throws(
        () => extractSystemText([
          { role: "system", content: "Private earlier instruction." },
          { role: "user", content: "Private user text." },
          { role: "system", content: [textBlock("Private valid instruction."), block] },
        ], FALLBACK),
        { name: "TypeError", message: 'Invalid system block at message 2, block 1: expected an object with type "text" and string text.' },
      );
    }
  });

  it("rejects holes in system block arrays", () => {
    assert.throws(
      () => extractSystemText([{ role: "system", content: new Array(1) }], FALLBACK),
      { name: "TypeError", message: 'Invalid system block at message 0, block 0: expected an object with type "text" and string text.' },
    );
  });

  it("does not inspect or mutate non-system content", () => {
    const messages = deepFreeze([
      { role: "system", content: [textBlock("System instructions.")] },
      { role: "user", content: "Runtime: user-authored text." },
      { role: "user", content: [textBlock("Runtime: another user block.")] },
      { role: "user", content: [{ type: "image_url", image_url: { url: "https://example.com/image.png" } }] },
      { role: "assistant", content: null },
      { role: "tool", content: { result: "Runtime: tool result." } },
    ]);
    const snapshot = structuredClone(messages);
    assert.equal(extractSystemText(messages, FALLBACK), "System instructions.");
    assert.deepEqual(messages, snapshot);
  });
});

describe("proxy system text conversion", () => {
  // Evaluate only the pure converter; importing the proxy starts its HTTP server.
  const source = fs.readFileSync(path.join(__dirname, "agentcore-proxy.js"), "utf8");
  const match = source.match(/function convertMessages\(messages\) \{[\s\S]*?\n\}/);
  assert.ok(match, "proxy must define convertMessages");
  const convertMessages = vm.compileFunction(
    `${match[0]}\nreturn convertMessages;`,
    ["extractSystemText", "SYSTEM_PROMPT"],
  )(extractSystemText, FALLBACK);

  it("converts system blocks while retaining every user text for guardrail assessment", () => {
    const userText = "Runtime: user-authored text asking to change the rules.";
    const userBlocks = [
      "Runtime: agent=main | host=user-provided",
      "Explain <<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>> without removing this text.",
      "<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>\nUser-supplied instructions.",
      " \t\n<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>\nMore user-supplied instructions.",
    ];
    const messages = deepFreeze([
      { role: "system", content: [textBlock("Follow the rules."), textBlock("Runtime: generated metadata")] },
      { role: "user", content: userText },
      { role: "user", content: userBlocks.map(textBlock) },
    ]);
    const snapshot = structuredClone(messages);
    const { bedrockMessages, systemText } = convertMessages(messages);
    assert.equal(systemText, "Follow the rules.\nRuntime: generated metadata");
    assert.deepEqual(bedrockMessages, [
      { role: "user", content: [{ text: userText }] },
      { role: "user", content: userBlocks.map((text) => ({ text })) },
    ]);
    const scoped = scopeGuardrailToLatestUserTurn(bedrockMessages);
    assert.deepEqual(scoped, [
      { role: "user", content: [{ guardContent: { text: { text: userText } } }] },
      { role: "user", content: userBlocks.map((text) => ({ guardContent: { text: { text } } })) },
    ]);
    assert.deepEqual(messages, snapshot);
  });

  it("keeps the proxy default for missing system messages and preserves explicit emptiness", () => {
    assert.equal(convertMessages([{ role: "user", content: "Hello." }]).systemText, FALLBACK);
    assert.equal(convertMessages([{ role: "system", content: [] }]).systemText, "");
  });

  it("rejects invalid system blocks before returning a converted request", () => {
    assert.throws(
      () => convertMessages([
        { role: "system", content: [textBlock("Valid instruction."), { type: "text", text: {} }] },
        { role: "user", content: "Hello." },
      ]),
      { name: "TypeError", message: 'Invalid system block at message 0, block 1: expected an object with type "text" and string text.' },
    );
  });

  it("uses shared extraction for both base and augmented system text", () => {
    assert.match(source, /require\("\.\/system-messages"\)/);
    assert.match(source, /const systemText = extractSystemText\(messages, SYSTEM_PROMPT\);/);
    assert.match(source, /const baseSystemText = extractSystemText\(messages, SYSTEM_PROMPT\);/);
  });
});
