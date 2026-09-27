/**
 * Tests for read-body.js — UTF-8 request bodies must survive a multi-byte
 * character split across two TCP/data chunks.
 *
 * Run: cd bridge && node --test read-body.test.js
 */
const { describe, it, before, after } = require("node:test");
const assert = require("node:assert");
const http = require("http");
const fs = require("fs");
const path = require("path");

const { readBody } = require("./read-body");

let server;
let port;

before(async () => {
  server = http.createServer((req, res) => {
    const max = Number(req.headers["x-max-bytes"]) || undefined;
    readBody(req, max).then(
      (body) => {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ body }));
      },
      (err) => {
        res.writeHead(err.code === "BODY_TOO_LARGE" ? 413 : 400);
        res.end();
      },
    );
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  port = server.address().port;
});

after(() => server.close());

/** POST `parts` (Buffers) as separate writes, pausing so each is its own chunk. */
function postInParts(parts, headers = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        host: "127.0.0.1",
        port,
        method: "POST",
        path: "/",
        headers: {
          "Content-Length": parts.reduce((n, p) => n + p.length, 0),
          ...headers,
        },
      },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () =>
          resolve({
            status: res.statusCode,
            text: Buffer.concat(chunks).toString("utf8"),
          }),
        );
      },
    );
    req.on("error", reject);
    (async () => {
      for (const p of parts) {
        req.write(p);
        await new Promise((r) => setTimeout(r, 20));
      }
      req.end();
    })();
  });
}

describe("readBody", () => {
  it("round-trips a CJK body split in the middle of a character", async () => {
    const text = JSON.stringify({ message: "你好世界" });
    const buf = Buffer.from(text, "utf8");
    // '{"message":"' is 12 bytes; byte 13 is inside the 3-byte '你'.
    const split = 13;
    const res = await postInParts([buf.subarray(0, split), buf.subarray(split)]);
    assert.strictEqual(res.status, 200);
    assert.strictEqual(JSON.parse(res.text).body, text);
  });

  it("round-trips a 4-byte emoji split across three chunks", async () => {
    const text = "a😀b";
    const buf = Buffer.from(text, "utf8");
    const res = await postInParts([
      buf.subarray(0, 2),
      buf.subarray(2, 4),
      buf.subarray(4),
    ]);
    assert.strictEqual(JSON.parse(res.text).body, text);
  });

  it("round-trips a large multi-byte body (> one socket read)", async () => {
    const text = JSON.stringify({ message: "你好世界".repeat(20000) });
    const buf = Buffer.from(text, "utf8");
    const res = await postInParts([buf.subarray(0, 100001), buf.subarray(100001)]);
    const body = JSON.parse(res.text).body;
    assert.ok(!body.includes("\uFFFD"), "body contains U+FFFD");
    assert.strictEqual(body, text);
  });

  it("counts the cap in bytes and rejects with BODY_TOO_LARGE", async () => {
    // 4 CJK chars = 12 bytes but only 4 UTF-16 units.
    const buf = Buffer.from("你好世界", "utf8");
    const over = await postInParts([buf], { "x-max-bytes": "11" });
    assert.strictEqual(over.status, 413);
    const exact = await postInParts([buf], { "x-max-bytes": "12" });
    assert.strictEqual(exact.status, 200);
    assert.strictEqual(JSON.parse(exact.text).body, "你好世界");
  });

  it("resolves an empty string for an empty body", async () => {
    const res = await postInParts([]);
    assert.strictEqual(JSON.parse(res.text).body, "");
  });
});

describe("request handlers use readBody", () => {
  for (const file of ["agentcore-contract.js", "agentcore-proxy.js"]) {
    it(`${file} reads its request body with readBody`, () => {
      const src = fs.readFileSync(path.join(__dirname, file), "utf8");
      assert.match(src, /require\("\.\/read-body"\)/);
      assert.match(src, /readBody\(req\b/);
      assert.doesNotMatch(
        src,
        /req\.on\("data"/,
        "per-chunk string concatenation of req corrupts split UTF-8",
      );
    });
  }

  it("Dockerfile copies read-body.js into the image", () => {
    const df = fs.readFileSync(path.join(__dirname, "Dockerfile"), "utf8");
    assert.match(df, /^COPY read-body\.js \/app\/read-body\.js$/m);
  });
});
