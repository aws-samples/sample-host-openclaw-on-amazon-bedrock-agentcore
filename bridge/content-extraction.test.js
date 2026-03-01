/**
 * Tests for extractTextFromContent from agentcore-contract.js.
 * Run: node --test content-extraction.test.js
 *
 * Since extractTextFromContent is not exported (inline in the contract server),
 * we mirror the logic here — same pattern as subagent-routing.test.js.
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

// --- Mirror of extractTextFromContent from agentcore-contract.js ---

function extractTextFromContent(content) {
  if (!content) return "";
  if (Array.isArray(content)) {
    const text = content
      .filter((b) => b.type === "text")
      .map((b) => b.text)
      .join("");
    return extractTextFromContent(text);
  }
  if (typeof content === "string") {
    const trimmed = content.trim();
    if (trimmed.startsWith("[{") && trimmed.endsWith("]")) {
      try {
        const parsed = JSON.parse(trimmed);
        if (
          Array.isArray(parsed) &&
          parsed.length > 0 &&
          parsed[0].type === "text"
        ) {
          const text = parsed
            .filter((b) => b.type === "text")
            .map((b) => b.text)
            .join("");
          return extractTextFromContent(text);
        }
      } catch {}
    }
    return content;
  }
  if (typeof content === "object" && content !== null) {
    if (typeof content.text === "string")
      return extractTextFromContent(content.text);
    if (typeof content.content === "string")
      return extractTextFromContent(content.content);
    if (Array.isArray(content.content)) {
      const text = content.content
        .filter((b) => b.type === "text")
        .map((b) => b.text)
        .join("");
      return extractTextFromContent(text);
    }
  }
  return "";
}

// --- Tests ---

describe("extractTextFromContent", () => {
  it("returns empty string for falsy input", () => {
    assert.equal(extractTextFromContent(null), "");
    assert.equal(extractTextFromContent(undefined), "");
    assert.equal(extractTextFromContent(""), "");
    assert.equal(extractTextFromContent(0), "");
  });

  it("returns plain text string as-is", () => {
    assert.equal(extractTextFromContent("Hello world"), "Hello world");
  });

  it("extracts text from a parsed content blocks array", () => {
    const blocks = [{ type: "text", text: "Hello " }, { type: "text", text: "world" }];
    assert.equal(extractTextFromContent(blocks), "Hello world");
  });

  it("extracts text from a JSON-serialized content blocks string", () => {
    const json = JSON.stringify([{ type: "text", text: "Hello world" }]);
    assert.equal(extractTextFromContent(json), "Hello world");
  });

  it("extracts text from object with text property", () => {
    assert.equal(extractTextFromContent({ text: "Hello" }), "Hello");
  });

  it("extracts text from object with content string property", () => {
    assert.equal(extractTextFromContent({ content: "Hello" }), "Hello");
  });

  it("extracts text from object with content array property", () => {
    const obj = { content: [{ type: "text", text: "Hello" }] };
    assert.equal(extractTextFromContent(obj), "Hello");
  });

  // --- Nested content blocks (subagent scenarios) ---

  it("unwraps double-nested content blocks (subagent response)", () => {
    const inner = JSON.stringify([{ type: "text", text: "Found several skills." }]);
    const outer = JSON.stringify([{ type: "text", text: inner }]);
    assert.equal(extractTextFromContent(outer), "Found several skills.");
  });

  it("unwraps triple-nested content blocks (deep subagent chain)", () => {
    const level1 = "Found several. Here are the most relevant.";
    const level2 = JSON.stringify([{ type: "text", text: level1 }]);
    const level3 = JSON.stringify([{ type: "text", text: level2 }]);
    const level4 = JSON.stringify([{ type: "text", text: level3 }]);
    assert.equal(extractTextFromContent(level4), level1);
  });

  it("unwraps nested content blocks from parsed array", () => {
    const inner = JSON.stringify([{ type: "text", text: "Actual response" }]);
    const blocks = [{ type: "text", text: inner }];
    assert.equal(extractTextFromContent(blocks), "Actual response");
  });

  it("unwraps nested content blocks from object with content property", () => {
    const inner = JSON.stringify([{ type: "text", text: "Deep text" }]);
    const obj = { content: [{ type: "text", text: inner }] };
    assert.equal(extractTextFromContent(obj), "Deep text");
  });

  it("handles text that looks like JSON but is not content blocks", () => {
    const text = '[{"key": "value"}]';
    assert.equal(extractTextFromContent(text), text);
  });

  it("handles malformed JSON gracefully", () => {
    const text = '[{"type":"text","text":"broken';
    assert.equal(extractTextFromContent(text), text);
  });

  it("preserves newlines and markdown in unwrapped text", () => {
    const inner = "# Title\n\n- Item 1\n- Item 2\n\n**Bold** text";
    const wrapped = JSON.stringify([{ type: "text", text: inner }]);
    assert.equal(extractTextFromContent(wrapped), inner);
  });

  it("concatenates multiple text blocks before unwrapping", () => {
    const blocks = [
      { type: "text", text: "Part 1. " },
      { type: "text", text: "Part 2." },
    ];
    assert.equal(extractTextFromContent(blocks), "Part 1. Part 2.");
  });

  it("filters out non-text blocks", () => {
    const blocks = [
      { type: "text", text: "Hello" },
      { type: "image", data: "..." },
      { type: "text", text: " world" },
    ];
    assert.equal(extractTextFromContent(blocks), "Hello world");
  });
});
