/**
 * Tests for createTelegramStreamer in agentcore-contract.js.
 * Run: node --test telegram-streamer.test.js
 *
 * The contract must NOT send the final Telegram message itself. It used to
 * send `{chat_id, text}` with no parse_mode, and when that send succeeded the
 * router skipped its own send, so short replies arrived as raw markdown
 * (literal **bold**, ### headers). The router converts markdown to Telegram
 * HTML and splits to the 4096 UTF-16 limit, so finalize() must leave the send
 * to it: no sendMessage call and messageId null (=> `streamed` stays false).
 *
 * agentcore-contract.js is a server entry point with no exports, so the
 * shipped function is sliced out of the source and evaluated with a fake
 * Telegram API. No network calls are made.
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

function sliceFunction(src, signature) {
  const start = src.indexOf(signature);
  assert.notEqual(start, -1, `missing ${signature}`);
  const end = src.indexOf("\n}\n", start);
  assert.notEqual(end, -1, `unterminated ${signature}`);
  return src.slice(start, end + 2);
}

function loadStreamer(calls) {
  const src = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf-8");
  const body = [
    sliceFunction(src, "function createTelegramStreamer("),
    "return createTelegramStreamer;",
  ].join("\n");
  const fakeApi = async (method, payload) => {
    calls.push({ method, payload });
    if (method === "sendMessage") return { ok: true, result: { message_id: 42 } };
    return { ok: true, result: true };
  };
  const quiet = { log: () => {}, warn: () => {}, error: () => {} };
  const factory = new Function("telegramApiCall", "console", body);
  return factory(fakeApi, quiet);
}

const CHAT_ID = "100000001"; // placeholder, not a real Telegram ID
const MARKDOWN_REPLY = "### Summary\n\n**bold** point\n\n| a | b |\n|---|---|\n| 1 | 2 |";

describe("createTelegramStreamer", () => {
  it("finalize does not send the final message (router sends formatted HTML)", async () => {
    const calls = [];
    const streamer = loadStreamer(calls)(CHAT_ID);
    const result = await streamer.finalize(MARKDOWN_REPLY);
    const sends = calls.filter((c) => c.method === "sendMessage");
    assert.equal(sends.length, 0, "contract must not call sendMessage");
    assert.equal(result.messageId, null, "messageId must be null so streamed stays false");
  });

  it("finalize after typing started still does not send and stops the typing loop", async () => {
    const calls = [];
    const streamer = loadStreamer(calls)(CHAT_ID);
    streamer.onDelta("x".repeat(80));
    const result = await streamer.finalize(MARKDOWN_REPLY);
    assert.equal(result.messageId, null);
    assert.equal(calls.filter((c) => c.method === "sendMessage").length, 0);
    const typing = calls.filter((c) => c.method === "sendChatAction");
    assert.equal(typing.length, 1, "typing indicator sent once on start");
    assert.deepEqual(typing[0].payload, { chat_id: CHAT_ID, action: "typing" });
    // The 5s interval must be cleared; if it were not, node --test would hang.
  });

  it("onDelta ignores short text and does not start typing", async () => {
    const calls = [];
    const streamer = loadStreamer(calls)(CHAT_ID);
    streamer.onDelta("short");
    await streamer.finalize("");
    assert.equal(calls.length, 0);
  });

  it("finalize with empty text returns messageId null", async () => {
    const calls = [];
    const streamer = loadStreamer(calls)(CHAT_ID);
    assert.deepEqual(await streamer.finalize(""), { messageId: null });
    assert.equal(calls.length, 0);
  });
});
