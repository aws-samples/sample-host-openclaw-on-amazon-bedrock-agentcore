/**
 * Tests for createTelegramStreamer in agentcore-contract.js.
 * Run: node --test telegram-streamer.test.js
 *
 * The contract must NOT send the final Telegram message itself while the
 * router is still connected. It used to send `{chat_id, text}` with no
 * parse_mode, and when that send succeeded the router skipped its own send,
 * so short replies arrived as raw markdown (literal **bold**, ### headers).
 * The router converts markdown to Telegram HTML and splits to the 4096 UTF-16
 * limit, so finalize() leaves the send to it: no sendMessage call and
 * messageId null (=> `streamed` stays false).
 *
 * Exception: if the router has already disconnected (its invoke read timeout
 * expired on a long turn), nobody else will deliver the reply, so finalize()
 * sends it as plain text split under 4096 UTF-16 units.
 *
 * agentcore-contract.js is a server entry point with no exports, so the
 * shipped functions are sliced out of the source and evaluated with a fake
 * Telegram API. No network calls are made (the disconnect test uses a
 * loopback HTTP server only).
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const http = require("http");
const path = require("path");

const SRC = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf-8");

function sliceFunction(src, signature) {
  const start = src.indexOf(signature);
  assert.notEqual(start, -1, `missing ${signature}`);
  const end = src.indexOf("\n}\n", start);
  assert.notEqual(end, -1, `unterminated ${signature}`);
  return src.slice(start, end + 2);
}

// Optional helpers: absent on older heads, where finalize ignores callerGone.
function sliceOptional(src, signature) {
  return src.includes(signature) ? sliceFunction(src, signature) : "";
}

function loadStreamer(calls) {
  const limit = SRC.match(/^const TELEGRAM_MAX_UTF16_UNITS = \d+;$/m);
  const body = [
    limit ? limit[0] : "",
    sliceOptional(SRC, "function splitTelegramText("),
    sliceFunction(SRC, "function createTelegramStreamer("),
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

function loadSplitter() {
  const limit = SRC.match(/^const TELEGRAM_MAX_UTF16_UNITS = \d+;$/m);
  assert.ok(limit, "missing TELEGRAM_MAX_UTF16_UNITS");
  const body = [limit[0], sliceFunction(SRC, "function splitTelegramText("),
    "return splitTelegramText;"].join("\n");
  return new Function(body)();
}

function loadDisconnectTracker() {
  const body = [sliceFunction(SRC, "function trackCallerDisconnect("),
    "return trackCallerDisconnect;"].join("\n");
  return new Function(body)();
}

// Contains paragraph, line and word breaks plus astral characters, so the
// split has to respect both break points and surrogate pairs.
function longReply() {
  const para = "**Point** " + "word ".repeat(120) + "\u{1F600}\n";
  return ("### Heading\n\n" + para.repeat(8) + "\n").repeat(3) + "x".repeat(5000);
}

function isLoneSurrogateAt(s, i) {
  const c = s.charCodeAt(i);
  return c >= 0xd800 && c <= 0xdfff;
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

describe("createTelegramStreamer when the router has disconnected", () => {
  it("finalize sends the reply itself as plain text, split under 4096 UTF-16 units", async () => {
    const calls = [];
    const streamer = loadStreamer(calls)(CHAT_ID);
    const reply = longReply();
    assert.ok(reply.length > 3 * 4096, "fixture must need several chunks");
    const result = await streamer.finalize(reply, { callerGone: true });
    const sends = calls.filter((c) => c.method === "sendMessage");
    assert.ok(sends.length >= 4, `expected >=4 chunks, got ${sends.length}`);
    for (const s of sends) {
      assert.equal(s.payload.chat_id, CHAT_ID);
      assert.equal(s.payload.parse_mode, undefined, "plain text, no parse_mode");
      assert.ok(s.payload.text.length <= 4096, `chunk of ${s.payload.text.length} units`);
      assert.ok(s.payload.text.length > 0);
      assert.ok(!isLoneSurrogateAt(s.payload.text, s.payload.text.length - 1) ||
        s.payload.text.codePointAt(s.payload.text.length - 2) > 0xffff,
        "chunk must not end inside a surrogate pair");
    }
    assert.equal(sends.map((s) => s.payload.text).join(""), reply, "no text lost");
    assert.equal(result.messageId, 42);
  });

  it("finalize with callerGone and empty text sends nothing", async () => {
    const calls = [];
    const streamer = loadStreamer(calls)(CHAT_ID);
    const result = await streamer.finalize("   ", { callerGone: true });
    assert.equal(calls.length, 0);
    assert.equal(result.messageId, null);
  });

  it("finalize with callerGone false still leaves the send to the router", async () => {
    const calls = [];
    const streamer = loadStreamer(calls)(CHAT_ID);
    const result = await streamer.finalize(MARKDOWN_REPLY, { callerGone: false });
    assert.equal(calls.length, 0);
    assert.equal(result.messageId, null);
  });
});

describe("splitTelegramText", () => {
  it("returns one chunk for a short reply and none for empty text", () => {
    const split = loadSplitter();
    assert.deepEqual(split("hello"), ["hello"]);
    assert.deepEqual(split(""), []);
    assert.deepEqual(split("a".repeat(4096)), ["a".repeat(4096)]);
  });

  it("never splits a surrogate pair at the limit", () => {
    const split = loadSplitter();
    const text = "a".repeat(4095) + "\u{1F600}" + "b".repeat(10); // emoji straddles 4096
    const chunks = split(text);
    assert.equal(chunks.join(""), text);
    assert.equal(chunks[0], "a".repeat(4095));
    assert.ok(chunks.every((c) => c.length <= 4096));
  });
});

describe("trackCallerDisconnect", () => {
  // Runs the real tracker in a loopback HTTP server: the handler waits, then
  // records what the tracker reports just before it would write the response.
  function serve(onCheck) {
    const track = loadDisconnectTracker();
    const server = http.createServer((req, res) => {
      const gone = track(res);
      req.resume();
      setTimeout(() => {
        onCheck(gone());
        res.writeHead(200);
        res.end("ok");
      }, 150);
    });
    return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
  }

  it("reports false while the caller is still waiting", async () => {
    let seen;
    const server = await serve((v) => { seen = v; });
    const { port } = server.address();
    await new Promise((resolve, reject) => {
      http.get({ host: "127.0.0.1", port, path: "/" }, (r) => { r.resume(); r.on("end", resolve); })
        .on("error", reject);
    });
    server.close();
    assert.equal(seen, false);
  });

  it("reports true when the caller dropped the connection before the reply", async () => {
    let resolveSeen;
    const seenP = new Promise((r) => { resolveSeen = r; });
    const server = await serve((v) => resolveSeen(v));
    const { port } = server.address();
    const req = http.request({ host: "127.0.0.1", port, path: "/", method: "POST" });
    req.on("error", () => {});
    req.end("{}");
    setTimeout(() => req.destroy(), 30); // router read timeout: client gives up
    const seen = await seenP;
    server.close();
    assert.equal(seen, true);
  });
});
