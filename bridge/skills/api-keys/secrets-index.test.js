/**
 * Tests for api-keys/secrets-index.js: the per-user S3 key-name index that
 * replaces secretsmanager:ListSecrets. Uses an in-memory fake S3 client that
 * enforces the conditional-write semantics (If-None-Match / If-Match), so no
 * AWS calls are made. Run: node --test skills/api-keys/secrets-index.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  indexKey, readIndex, writeIndex, addKey, removeKey, listKeys,
} = require("./secrets-index");

const BUCKET = "openclaw-user-files-123-us-west-2";
const NS = "telegram_12345";

function preconditionError() {
  const e = new Error("precondition failed");
  e.name = "PreconditionFailed";
  e.$metadata = { httpStatusCode: 412 };
  return e;
}

function notFoundError() {
  const e = new Error("no such key");
  e.name = "NoSuchKey";
  e.$metadata = { httpStatusCode: 404 };
  return e;
}

// Minimal S3 fake: one object per key, monotonic ETags, conditional writes.
function makeFakeS3() {
  const store = new Map(); // key -> { body, etag }
  let etagCounter = 0;
  const calls = { get: 0, put: 0 };
  const api = {
    store,
    calls,
    async send(command) {
      const name = command.constructor.name;
      const input = command.input;
      if (name === "GetObjectCommand") {
        calls.get += 1;
        const obj = store.get(input.Key);
        if (!obj) throw notFoundError();
        return { ETag: obj.etag, Body: { transformToString: async () => obj.body } };
      }
      if (name === "PutObjectCommand") {
        calls.put += 1;
        const existing = store.get(input.Key);
        if (input.IfNoneMatch === "*" && existing) throw preconditionError();
        if (input.IfMatch && (!existing || existing.etag !== input.IfMatch)) throw preconditionError();
        etagCounter += 1;
        const etag = `"etag-${etagCounter}"`;
        store.set(input.Key, { body: input.Body, etag });
        return { ETag: etag };
      }
      throw new Error(`unexpected command ${name}`);
    },
  };
  return api;
}

describe("secrets-index", () => {
  it("puts the index under the user's own prefix in a leading-dot path", () => {
    assert.equal(indexKey(NS), "telegram_12345/.openclaw/api-keys-index.json");
  });

  it("reads a missing object as an empty index", async () => {
    const s3 = makeFakeS3();
    assert.deepEqual(await readIndex(s3, BUCKET, NS), { keys: [], etag: null });
    assert.deepEqual(await listKeys(s3, BUCKET, NS), []);
  });

  it("creates the index on first add (If-None-Match) and lists it", async () => {
    const s3 = makeFakeS3();
    assert.deepEqual(await addKey(s3, BUCKET, NS, "openai"), ["openai"]);
    assert.deepEqual(await listKeys(s3, BUCKET, NS), ["openai"]);
    // The object was created, and its body is the versioned shape.
    const stored = JSON.parse(s3.store.get(indexKey(NS)).body);
    assert.equal(stored.version, 1);
    assert.deepEqual(stored.keys, ["openai"]);
  });

  it("keeps names sorted and deduped, and does not write when unchanged", async () => {
    const s3 = makeFakeS3();
    await addKey(s3, BUCKET, NS, "zeta");
    await addKey(s3, BUCKET, NS, "alpha");
    assert.deepEqual(await listKeys(s3, BUCKET, NS), ["alpha", "zeta"]);
    const putsBefore = s3.calls.put;
    // Adding an existing name is a no-op (no write).
    assert.deepEqual(await addKey(s3, BUCKET, NS, "alpha"), ["alpha", "zeta"]);
    assert.equal(s3.calls.put, putsBefore);
  });

  it("removes a name, and removing an absent name is a no-op", async () => {
    const s3 = makeFakeS3();
    await addKey(s3, BUCKET, NS, "a");
    await addKey(s3, BUCKET, NS, "b");
    assert.deepEqual(await removeKey(s3, BUCKET, NS, "a"), ["b"]);
    const putsBefore = s3.calls.put;
    assert.deepEqual(await removeKey(s3, BUCKET, NS, "missing"), ["b"]);
    assert.equal(s3.calls.put, putsBefore);
  });

  it("treats a corrupt index body as empty and overwrites it on the next write", async () => {
    const s3 = makeFakeS3();
    s3.store.set(indexKey(NS), { body: "}{ not json", etag: '"etag-x"' });
    assert.deepEqual(await listKeys(s3, BUCKET, NS), []);
    await addKey(s3, BUCKET, NS, "fresh");
    assert.deepEqual(await listKeys(s3, BUCKET, NS), ["fresh"]);
  });

  it("isolates each namespace's index", async () => {
    const s3 = makeFakeS3();
    await addKey(s3, BUCKET, "telegram_1", "k1");
    await addKey(s3, BUCKET, "telegram_2", "k2");
    assert.deepEqual(await listKeys(s3, BUCKET, "telegram_1"), ["k1"]);
    assert.deepEqual(await listKeys(s3, BUCKET, "telegram_2"), ["k2"]);
  });

  it("retries and converges when a concurrent writer wins a round", async () => {
    const s3 = makeFakeS3();
    await addKey(s3, BUCKET, NS, "a");
    let injected = false;
    const realSend = s3.send.bind(s3);
    s3.send = async (command) => {
      if (command.constructor.name === "PutObjectCommand" && !injected) {
        injected = true;
        // Another session commits "b" first, invalidating our read's ETag.
        s3.store.set(indexKey(NS), {
          body: JSON.stringify({ version: 1, keys: ["a", "b"] }),
          etag: '"etag-concurrent"',
        });
      }
      return realSend(command);
    };
    // Our add of "c" hits a 412 on its first Put, re-reads, and converges.
    assert.deepEqual(await addKey(s3, BUCKET, NS, "c"), ["a", "b", "c"]);
  });

  it("surfaces a persistent conflict instead of looping forever", async () => {
    const s3 = makeFakeS3();
    await addKey(s3, BUCKET, NS, "a");
    // Every Put sees a fresh concurrent commit, so the ETag is always stale.
    const realSend = s3.send.bind(s3);
    let n = 0;
    s3.send = async (command) => {
      if (command.constructor.name === "PutObjectCommand") {
        n += 1;
        s3.store.set(indexKey(NS), {
          body: JSON.stringify({ version: 1, keys: ["a"] }),
          etag: `"etag-moving-${n}"`,
        });
      }
      return realSend(command);
    };
    await assert.rejects(() => addKey(s3, BUCKET, NS, "b"), /PreconditionFailed|retries exhausted/);
  });

  it("writeIndex creates with If-None-Match and replaces with If-Match", async () => {
    const s3 = makeFakeS3();
    const captured = [];
    const realSend = s3.send.bind(s3);
    s3.send = async (command) => {
      if (command.constructor.name === "PutObjectCommand") captured.push(command.input);
      return realSend(command);
    };
    await writeIndex(s3, BUCKET, NS, ["a"], null);
    assert.equal(captured[0].IfNoneMatch, "*");
    assert.equal(captured[0].IfMatch, undefined);
    const { etag } = await readIndex(s3, BUCKET, NS);
    await writeIndex(s3, BUCKET, NS, ["a", "b"], etag);
    assert.equal(captured[1].IfMatch, etag);
    assert.equal(captured[1].IfNoneMatch, undefined);
  });
});
