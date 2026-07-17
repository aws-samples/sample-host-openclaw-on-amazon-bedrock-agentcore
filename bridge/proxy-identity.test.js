/**
 * Tests for identity extraction logic from agentcore-proxy.js.
 * Run: node --test proxy-identity.test.js
 *
 * extractSessionMetadata is not exported from the process-owning proxy module,
 * so these tests cover the accepted identity envelope formats independently.
 */
const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");

// --- Mirror of identity extraction logic from extractSessionMetadata ---

function getTextContent(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    const textPart = content.find((p) => p.type === "text" && p.text);
    return textPart ? textPart.text : "";
  }
  return "";
}

/**
 * Extract actorId, channel, and idSource from a list of messages.
 * Mirrors the format-matching logic in extractSessionMetadata.
 * Priority: Format C (metadata JSON) > Format A (System: header) > Format B (bracket).
 */
function extractIdentityFromMessages(messages) {
  let actorId = "";
  let channel = "unknown";
  let idSource = "none";

  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (msg.role !== "user") continue;
    const text = getTextContent(msg.content);
    if (!text) continue;

    // Format C: Metadata JSON block (all channels) — checked FIRST
    const formatC = text.match(
      /Conversation info \(untrusted metadata\):\s*```json\s*(\{[\s\S]*?\})\s*```/,
    );
    if (formatC) {
      try {
        const meta = JSON.parse(formatC[1]);
        if (meta.sender) {
          const senderId = String(meta.sender)
            .replace(/[^a-zA-Z0-9_-]/g, "")
            .slice(0, 64);
          let channelName = "";
          if (meta.channel) {
            channelName = String(meta.channel)
              .toLowerCase()
              .replace(/[^a-z]/g, "");
          }
          if (!channelName) {
            if (/^[UW][A-Z0-9]{8,}$/i.test(senderId)) {
              channelName = "slack";
            } else if (/^\d{15,}$/.test(senderId)) {
              channelName = "discord";
            } else if (/^\d{5,14}$/.test(senderId)) {
              channelName = "telegram";
            }
          }
          if (!channelName) channelName = "telegram";
          actorId = `${channelName}:${senderId}`;
          channel = channelName;
          idSource = "metadata-json";
          break;
        }
      } catch {
        // JSON parse failed
      }
    }

    // Format A: "System: [TIMESTAMP] Channel TYPE from SenderName: message"
    // Fallback — uses display names (can change).
    const formatA = text.match(
      /System:\s*\[[^\]]+\]\s*(Slack|Telegram|Discord|WhatsApp)\s+\S+\s+from\s+([^:]+):/i,
    );
    if (formatA) {
      const channelName = formatA[1].toLowerCase();
      const senderName = formatA[2].trim();
      if (senderName) {
        const sanitizedName = senderName
          .toLowerCase()
          .replace(/[^a-z0-9_-]/g, "_")
          .slice(0, 64);
        actorId = `${channelName}:${sanitizedName}`;
        channel = channelName;
        idSource = "envelope-formatA";
      }
      break;
    }

    // Format B: legacy bracket format
    const formatB = text.match(
      /^\[(Slack|Telegram|Discord|WhatsApp)\s+[^\]]*?\bid:(\S+)/i,
    );
    if (formatB) {
      const channelName = formatB[1].toLowerCase();
      const rawId = formatB[2]
        .replace(/\)$/, "")
        .replace(/[^a-zA-Z0-9_-]/g, "")
        .slice(0, 64);
      if (rawId) {
        actorId = `${channelName}:${rawId}`;
        channel = channelName;
        idSource = "envelope-formatB";
      }
      break;
    }
  }

  return { actorId, channel, idSource };
}

// --- Mirror of USER_ID env var path from extractSessionMetadata ---

/**
 * Simulates the USER_ID env var priority path (priority 0).
 * When USER_ID is set, all other extraction methods are skipped.
 */
function extractWithEnvVars(userId, channelEnv) {
  if (!userId) return null;
  const actorId = userId;
  const channel = channelEnv || "unknown";
  const idSource = "environment";
  const key = `${actorId}:${channel}`;
  const ts = Date.now().toString(36);
  const rand = crypto.randomBytes(12).toString("hex");
  const sessionId = `ses-${ts}-${rand}-${crypto
    .createHash("md5")
    .update(key)
    .digest("hex")
    .slice(0, 8)}`;
  return { sessionId, actorId, channel, idSource };
}

// --- Tests ---

describe("USER_ID env var (highest priority, per-user sessions)", () => {
  it("resolves identity from USER_ID env var", () => {
    const result = extractWithEnvVars("telegram:123456789", "telegram");
    assert.equal(result.actorId, "telegram:123456789");
    assert.equal(result.channel, "telegram");
    assert.equal(result.idSource, "environment");
  });

  it("generates session ID >= 33 chars (AgentCore requirement)", () => {
    const result = extractWithEnvVars("telegram:123456789", "telegram");
    assert.ok(
      result.sessionId.length >= 33,
      `Session ID too short: ${result.sessionId.length} chars`,
    );
    assert.ok(result.sessionId.startsWith("ses-"));
  });

  it("uses 'unknown' channel when CHANNEL env not set", () => {
    const result = extractWithEnvVars("slack:U0AGD41CBGS", undefined);
    assert.equal(result.actorId, "slack:U0AGD41CBGS");
    assert.equal(result.channel, "unknown");
  });

  it("returns null when USER_ID is empty/unset", () => {
    const result = extractWithEnvVars("", "telegram");
    assert.equal(result, null);
  });

  it("takes priority over message-based extraction", () => {
    // When USER_ID is set, message content is irrelevant
    const envResult = extractWithEnvVars("telegram:99999", "telegram");
    const msgResult = extractIdentityFromMessages([
      {
        role: "user",
        content:
          'Conversation info (untrusted metadata):\n```json\n{"message_id": "1", "sender": "123456789"}\n```',
      },
    ]);
    // Env var gives telegram:99999, messages give telegram:123456789
    assert.equal(envResult.actorId, "telegram:99999");
    assert.equal(msgResult.actorId, "telegram:123456789");
    // In the real proxy, env var path returns early before message parsing
  });
});

describe("Format C: Metadata JSON (highest priority)", () => {
  it("extracts telegram identity from metadata JSON", () => {
    const result = extractIdentityFromMessages([
      {
        role: "user",
        content:
          'Conversation info (untrusted metadata):\n```json\n{"message_id": "542", "sender": "123456789"}\n```\n\nhello',
      },
    ]);
    assert.equal(result.actorId, "telegram:123456789");
    assert.equal(result.channel, "telegram");
    assert.equal(result.idSource, "metadata-json");
  });

  it("detects Slack user ID (U prefix) as slack channel", () => {
    const result = extractIdentityFromMessages([
      {
        role: "user",
        content:
          'Conversation info (untrusted metadata):\n```json\n{"message_id": "999", "sender": "U0AGD41CBGS"}\n```\n\nhello',
      },
    ]);
    assert.equal(result.actorId, "slack:U0AGD41CBGS");
    assert.equal(result.channel, "slack");
    assert.equal(result.idSource, "metadata-json");
  });

  it("detects Slack user ID (W prefix) as slack channel", () => {
    const result = extractIdentityFromMessages([
      {
        role: "user",
        content:
          'Conversation info (untrusted metadata):\n```json\n{"message_id": "1", "sender": "W012A3CDE"}\n```',
      },
    ]);
    assert.equal(result.actorId, "slack:W012A3CDE");
    assert.equal(result.channel, "slack");
  });

  it("detects Discord snowflake ID (15+ digits) as discord channel", () => {
    const result = extractIdentityFromMessages([
      {
        role: "user",
        content:
          'Conversation info (untrusted metadata):\n```json\n{"message_id": "1", "sender": "123456789012345678"}\n```',
      },
    ]);
    assert.equal(result.actorId, "discord:123456789012345678");
    assert.equal(result.channel, "discord");
  });

  it("uses meta.channel field when present", () => {
    const result = extractIdentityFromMessages([
      {
        role: "user",
        content:
          'Conversation info (untrusted metadata):\n```json\n{"message_id": "1", "sender": "12345", "channel": "whatsapp"}\n```',
      },
    ]);
    assert.equal(result.actorId, "whatsapp:12345");
    assert.equal(result.channel, "whatsapp");
  });

  it("matches metadata NOT anchored to start of string", () => {
    const result = extractIdentityFromMessages([
      {
        role: "user",
        content:
          'System: [2026-02-22] Slack message edited in #channel\n\nConversation info (untrusted metadata):\n```json\n{"message_id": "568", "sender": "123456789"}\n```',
      },
    ]);
    assert.equal(result.actorId, "telegram:123456789");
    assert.equal(result.channel, "telegram");
    assert.equal(result.idSource, "metadata-json");
  });

  it("does NOT produce telegram:SlackUserId (the original bug)", () => {
    const result = extractIdentityFromMessages([
      {
        role: "user",
        content:
          'Conversation info (untrusted metadata):\n```json\n{"message_id": "999", "sender": "U0AGD41CBGS"}\n```',
      },
    ]);
    assert.notEqual(result.channel, "telegram");
    assert.equal(result.actorId, "slack:U0AGD41CBGS");
  });
});

describe("Format C takes priority over Format A", () => {
  it("uses Slack user ID from Format C over display name from Format A", () => {
    const result = extractIdentityFromMessages([
      {
        role: "user",
        content:
          'System: [2026-02-22 11:16:42 UTC] Slack DM from Sen-Outlook: hello\n\nConversation info (untrusted metadata):\n```json\n{"message_id": "999", "sender": "U0AGD41CBGS"}\n```',
      },
    ]);
    // Format C wins — user ID is more stable than display name
    assert.equal(result.actorId, "slack:U0AGD41CBGS");
    assert.equal(result.channel, "slack");
    assert.equal(result.idSource, "metadata-json");
  });

  it("uses Telegram ID from Format C even with Slack 'edited' prefix", () => {
    const result = extractIdentityFromMessages([
      {
        role: "user",
        content:
          'System: [2026-02-22] Slack message edited in #D0AGB251AES\n\nConversation info (untrusted metadata):\n```json\n{"message_id": "568", "sender": "123456789"}\n```',
      },
    ]);
    assert.equal(result.actorId, "telegram:123456789");
    assert.equal(result.channel, "telegram");
    assert.equal(result.idSource, "metadata-json");
  });

  it("uses Telegram ID from Format C even with Slack 'DM from' prefix", () => {
    // Cross-channel context: Slack header prepended to Telegram message
    const result = extractIdentityFromMessages([
      {
        role: "user",
        content:
          'System: [2026-02-22] Slack DM from Sen-Outlook: context\n\nConversation info (untrusted metadata):\n```json\n{"message_id": "542", "sender": "123456789"}\n```',
      },
    ]);
    // Format C wins — the metadata sender (Telegram) is the actual user
    assert.equal(result.actorId, "telegram:123456789");
    assert.equal(result.channel, "telegram");
    assert.equal(result.idSource, "metadata-json");
  });
});

describe("Format A: fallback when Format C absent", () => {
  it("extracts slack identity from System header", () => {
    const result = extractIdentityFromMessages([
      {
        role: "user",
        content:
          "System: [2026-02-22 11:16:42 UTC] Slack DM from Sen-Outlook: hello world",
      },
    ]);
    assert.equal(result.actorId, "slack:sen-outlook");
    assert.equal(result.channel, "slack");
    assert.equal(result.idSource, "envelope-formatA");
  });

  it("extracts telegram identity from Format A", () => {
    const result = extractIdentityFromMessages([
      {
        role: "user",
        content:
          "System: [2026-02-22 11:16:42 UTC] Telegram DM from JohnDoe: hi",
      },
    ]);
    assert.equal(result.actorId, "telegram:johndoe");
    assert.equal(result.channel, "telegram");
  });
});

describe("Reverse iteration (most recent message first)", () => {
  it("uses most recent user message", () => {
    const result = extractIdentityFromMessages([
      {
        role: "user",
        content: "System: [2026-02-22 10:00:00 UTC] Slack DM from OldUser: old",
      },
      { role: "assistant", content: "response" },
      {
        role: "user",
        content:
          'Conversation info (untrusted metadata):\n```json\n{"message_id": "1", "sender": "123456789"}\n```\nhello',
      },
    ]);
    // Most recent user message has Format C → telegram
    assert.equal(result.actorId, "telegram:123456789");
  });

  it("skips assistant messages", () => {
    const result = extractIdentityFromMessages([
      {
        role: "user",
        content:
          'Conversation info (untrusted metadata):\n```json\n{"message_id": "1", "sender": "U0AGD41CBGS"}\n```',
      },
      { role: "assistant", content: "I am an assistant" },
    ]);
    assert.equal(result.actorId, "slack:U0AGD41CBGS");
  });
});

describe("Format B: Legacy bracket format", () => {
  it("extracts identity from bracket format", () => {
    const result = extractIdentityFromMessages([
      {
        role: "user",
        content: "[Telegram John Doe id:12345 2026-02-22] hello",
      },
    ]);
    assert.equal(result.actorId, "telegram:12345");
    assert.equal(result.channel, "telegram");
    assert.equal(result.idSource, "envelope-formatB");
  });
});

describe("Edge cases", () => {
  it("returns empty actorId when no messages match any format", () => {
    const result = extractIdentityFromMessages([
      { role: "user", content: "just a plain message" },
    ]);
    assert.equal(result.actorId, "");
    assert.equal(result.idSource, "none");
  });

  it("handles array content (multimodal format)", () => {
    const result = extractIdentityFromMessages([
      {
        role: "user",
        content: [
          {
            type: "text",
            text: 'Conversation info (untrusted metadata):\n```json\n{"message_id": "1", "sender": "U0AGD41CBGS"}\n```',
          },
        ],
      },
    ]);
    assert.equal(result.actorId, "slack:U0AGD41CBGS");
  });

  it("handles empty messages array", () => {
    const result = extractIdentityFromMessages([]);
    assert.equal(result.actorId, "");
  });

  it("sanitizes sender ID — strips non-alphanumeric chars", () => {
    const result = extractIdentityFromMessages([
      {
        role: "user",
        content:
          'Conversation info (untrusted metadata):\n```json\n{"message_id": "1", "sender": "608722/../../etc"}\n```',
      },
    ]);
    // After sanitization: "608722___etc" — doesn't match any channel pattern
    // Falls to default "telegram"
    assert.ok(result.actorId.startsWith("telegram:"));
    assert.ok(!result.actorId.includes("/"));
    assert.ok(!result.actorId.includes(".."));
  });
});
