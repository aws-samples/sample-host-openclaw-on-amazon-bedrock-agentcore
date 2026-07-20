/**
 * Tests for image support in the proxy adapter.
 *
 * Tests extractImageReferences, convertMessages with multimodal content,
 * and fetchImageFromS3 key validation.
 */

const { describe, it, beforeEach, mock } = require("node:test");
const assert = require("node:assert/strict");
process.env.AWS_REGION = "eu-west-1";
process.env.AWS_DEFAULT_REGION = "eu-west-1";
process.env.INTERNAL_USER_ID = "user_A";
process.env.PERSONAL_OPERATOR_WORKSPACE_PREFIX = "user_A";
process.env.PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE = "/tmp/not-used";
process.env.S3_USER_FILES_BUCKET = "personal-operator-workspace";
const productionProxy = require("./agentcore-proxy");

// We need to extract the functions from the proxy module.
// Since the proxy starts a server on import, we'll extract the functions
// by reading the source and evaluating just the relevant parts.
// Instead, we'll test the logic directly by reimplementing the pure functions
// here (they're simple enough to be tested in isolation).

// --- extractImageReferences ---

const ALLOWED_IMAGE_TYPES = new Set([
  "image/jpeg",
  "image/png",
  "image/gif",
  "image/webp",
]);
const IMAGE_MARKER_REGEX = /\n?\n?\[OPENCLAW_IMAGES:(\[.*?\])\]\s*$/;

const { extractImageReferences } = productionProxy;

describe("extractImageReferences", () => {
  it("returns original text when no marker present", () => {
    const result = extractImageReferences("Hello, how are you?");
    assert.equal(result.cleanText, "Hello, how are you?");
    assert.equal(result.images.length, 0);
  });

  it("extracts single image reference", () => {
    const text =
      'What is this?\n\n[OPENCLAW_IMAGES:[{"s3Key":"ns/_uploads/img_123.jpeg","contentType":"image/jpeg"}]]';
    const result = extractImageReferences(text);
    assert.equal(result.cleanText, "What is this?");
    assert.equal(result.images.length, 1);
    assert.equal(result.images[0].s3Key, "ns/_uploads/img_123.jpeg");
    assert.equal(result.images[0].contentType, "image/jpeg");
  });

  it("handles empty text with image only", () => {
    const text =
      '\n\n[OPENCLAW_IMAGES:[{"s3Key":"ns/_uploads/img.png","contentType":"image/png"}]]';
    const result = extractImageReferences(text);
    assert.equal(result.cleanText, "");
    assert.equal(result.images.length, 1);
  });

  it("handles invalid JSON gracefully", () => {
    const text = "Hello\n\n[OPENCLAW_IMAGES:[not valid json]]";
    const result = extractImageReferences(text);
    assert.equal(result.cleanText, "Hello");
    assert.equal(result.images.length, 0);
  });

  it("rejects disallowed content types", () => {
    const text =
      'Check this\n\n[OPENCLAW_IMAGES:[{"s3Key":"ns/_uploads/file.pdf","contentType":"application/pdf"}]]';
    const result = extractImageReferences(text);
    assert.equal(result.cleanText, "Check this");
    assert.equal(result.images.length, 0);
  });

  it("handles non-string input", () => {
    const result = extractImageReferences(42);
    assert.equal(result.cleanText, 42);
    assert.equal(result.images.length, 0);
  });

  it("handles trailing whitespace after marker", () => {
    const text =
      'Look\n\n[OPENCLAW_IMAGES:[{"s3Key":"ns/_uploads/img.jpeg","contentType":"image/jpeg"}]]  \n';
    const result = extractImageReferences(text);
    assert.equal(result.cleanText, "Look");
    assert.equal(result.images.length, 1);
  });

  it("filters out entries missing s3Key", () => {
    const text =
      'Hi\n\n[OPENCLAW_IMAGES:[{"contentType":"image/jpeg"},{"s3Key":"ns/_uploads/img.jpeg","contentType":"image/jpeg"}]]';
    const result = extractImageReferences(text);
    assert.equal(result.images.length, 1);
    assert.equal(result.images[0].s3Key, "ns/_uploads/img.jpeg");
  });
});

// --- convertMessages with multimodal content ---

const SYSTEM_PROMPT = "Test system prompt";

function convertMessages(messages) {
  const bedrockMessages = [];
  for (const msg of messages) {
    if (msg.role === "system") continue;

    if (msg.role === "user") {
      if (Array.isArray(msg.content)) {
        const bedrockContent = [];
        for (const part of msg.content) {
          if (part.type === "text" && part.text) {
            bedrockContent.push({ text: part.text });
          } else if (part.type === "image_bedrock" && part.image) {
            bedrockContent.push({ image: part.image });
          }
        }
        if (bedrockContent.length > 0) {
          bedrockMessages.push({ role: "user", content: bedrockContent });
        }
      } else {
        bedrockMessages.push({
          role: "user",
          content: [
            {
              text:
                typeof msg.content === "string"
                  ? msg.content
                  : JSON.stringify(msg.content),
            },
          ],
        });
      }
    } else if (msg.role === "assistant") {
      if (msg.content) {
        bedrockMessages.push({
          role: "assistant",
          content: [
            {
              text:
                typeof msg.content === "string"
                  ? msg.content
                  : JSON.stringify(msg.content),
            },
          ],
        });
      }
    }
  }

  const systemMessages = messages.filter((m) => m.role === "system");
  const systemText =
    systemMessages.length > 0
      ? systemMessages.map((m) => m.content).join("\n")
      : SYSTEM_PROMPT;

  return { bedrockMessages, systemText };
}

describe("convertMessages with multimodal content", () => {
  it("converts string content as before", () => {
    const messages = [{ role: "user", content: "Hello" }];
    const { bedrockMessages } = convertMessages(messages);
    assert.equal(bedrockMessages.length, 1);
    assert.equal(bedrockMessages[0].content[0].text, "Hello");
  });

  it("converts array content with text and image parts", () => {
    const messages = [
      {
        role: "user",
        content: [
          { type: "text", text: "What is this?" },
          {
            type: "image_bedrock",
            image: {
              format: "jpeg",
              source: { bytes: Buffer.from("fake-image") },
            },
          },
        ],
      },
    ];
    const { bedrockMessages } = convertMessages(messages);
    assert.equal(bedrockMessages.length, 1);
    assert.equal(bedrockMessages[0].content.length, 2);
    assert.equal(bedrockMessages[0].content[0].text, "What is this?");
    assert.ok(bedrockMessages[0].content[1].image);
    assert.equal(bedrockMessages[0].content[1].image.format, "jpeg");
  });

  it("handles image-only message (no text)", () => {
    const messages = [
      {
        role: "user",
        content: [
          {
            type: "image_bedrock",
            image: {
              format: "png",
              source: { bytes: Buffer.from("fake") },
            },
          },
        ],
      },
    ];
    const { bedrockMessages } = convertMessages(messages);
    assert.equal(bedrockMessages.length, 1);
    assert.equal(bedrockMessages[0].content.length, 1);
    assert.ok(bedrockMessages[0].content[0].image);
  });

  it("filters out unknown content types", () => {
    const messages = [
      {
        role: "user",
        content: [
          { type: "text", text: "Hello" },
          { type: "audio", data: "..." },
        ],
      },
    ];
    const { bedrockMessages } = convertMessages(messages);
    assert.equal(bedrockMessages[0].content.length, 1);
    assert.equal(bedrockMessages[0].content[0].text, "Hello");
  });
});

// --- fetchImageFromS3 key validation (namespace-aware) ---

describe("fetchImageFromS3 key validation", () => {
  const namespace = "user_A";

  it("accepts keys in the correct user namespace", () => {
    const key = "user_A/_uploads/img_1234_abcd.jpeg";
    assert.equal(productionProxy.isValidImageKey(key, namespace), true);
  });

  it("rejects keys from a different user namespace", () => {
    const key = "user_B/_uploads/img_1234_abcd.jpeg";
    assert.equal(productionProxy.isValidImageKey(key, namespace), false);
  });

  it("rejects keys with path traversal", () => {
    const key = "user_A/_uploads/../../user_B/_uploads/img.jpeg";
    assert.equal(productionProxy.isValidImageKey(key, namespace), false);
  });

  it("rejects keys without /_uploads/ segment", () => {
    const key = "user_A/regular-file.txt";
    assert.equal(productionProxy.isValidImageKey(key, namespace), false);
  });

  it("rejects crafted keys that contain /_uploads/ but wrong namespace", () => {
    // Attacker sends: other_ns/_uploads/stolen.jpeg
    const key = "other_ns/_uploads/stolen.jpeg";
    assert.equal(productionProxy.isValidImageKey(key, namespace), false);
  });
});
