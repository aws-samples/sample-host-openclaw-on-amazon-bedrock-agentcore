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
      let parsed = null;
      try {
        parsed = JSON.parse(trimmed);
      } catch {
        // Retry with literal control characters escaped (JS JSON.parse is strict)
        try {
          const sanitized = trimmed.replace(/[\x00-\x1f\x7f]/g, c => {
            const e = {"\b":"\\b","\t":"\\t","\n":"\\n","\f":"\\f","\r":"\\r"};
            return e[c] || ("\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
          });
          parsed = JSON.parse(sanitized);
        } catch {
          // Both failed — try regex extraction below
        }
      }
      if (!parsed) {
        // Regex fallback for malformed JSON (e.g., "text","value" instead of "text":"value")
        const textMatch = trimmed.match(/[,{]\s*"text"\s*[,:]\s*"((?:[^"\\]|\\.)*)"/);
        if (textMatch) {
          try {
            const extracted = JSON.parse('"' + textMatch[1] + '"');
            if (extracted) return extractTextFromContent(extracted);
          } catch {
            return extractTextFromContent(textMatch[1]);
          }
        }
      }
      if (parsed && Array.isArray(parsed) && parsed.length > 0 && parsed[0].type === "text") {
        const text = parsed
          .filter((b) => b.type === "text")
          .map((b) => b.text)
          .join("");
        if (text) return extractTextFromContent(text);
      }
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

  // --- Literal control character handling (JSON.parse strict mode) ---

  it("extracts text from JSON string with literal newlines", () => {
    // Simulate JSON with actual newline bytes (not escaped \\n)
    const json = '[{"type":"text","text":"Hello' + String.fromCharCode(10) + 'world"}]';
    assert.equal(extractTextFromContent(json), "Hello\nworld");
  });

  it("extracts text from malformed JSON with comma instead of colon", () => {
    // Real-world case: "text","value" instead of "text":"value"
    const malformed = '[{"type":"text","text","\\n\\n## ✅ Hello World"}]';
    const result = extractTextFromContent(malformed);
    assert.ok(result.includes("Hello World"), `Expected 'Hello World' in: ${result}`);
  });

  it("extracts text from JSON string with literal tab characters", () => {
    const json = '[{"type":"text","text":"col1' + String.fromCharCode(9) + 'col2"}]';
    assert.equal(extractTextFromContent(json), "col1\tcol2");
  });
});
