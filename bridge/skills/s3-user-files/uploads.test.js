/**
 * Tests for reaching router-stored uploads ({user_id}/_uploads/<name>) and for
 * download_user_file's local path handling.
 * Run: node --test uploads.test.js
 */
process.env.AWS_REGION = process.env.AWS_REGION || "us-west-2";
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const common = require("./common");

const USER = "telegram_123456";

describe("buildKey with the _uploads/ prefix", () => {
  it("keeps a single _uploads/ folder for router-stored files", () => {
    assert.equal(
      common.buildKey(USER, "_uploads/doc_1700000000_ab12cd34.md"),
      `${USER}/_uploads/doc_1700000000_ab12cd34.md`,
    );
  });

  it("matches the names list.js prints for uploads", () => {
    const key = `${USER}/_uploads/doc_1_ab.zip`;
    const listed = key.replace(common.buildKey(USER), "");
    assert.equal(common.buildKey(USER, listed), key);
  });

  it("still flattens deeper paths and traversal inside _uploads/", () => {
    assert.equal(
      common.buildKey(USER, "_uploads/../../other_user/secret.md"),
      `${USER}/_uploads/__other_user_secret.md`,
    );
    assert.equal(common.buildKey(USER, "_uploads/a/b.md"), `${USER}/_uploads/a_b.md`);
  });

  it("rejects an empty name after _uploads/", () => {
    assert.throws(() => common.buildKey(USER, "_uploads/"), /filename/);
  });

  it("leaves other filenames unchanged", () => {
    assert.equal(common.buildKey(USER, "notes/today.md"), `${USER}/notes_today.md`);
    assert.equal(common.buildKey(USER, "IDENTITY.md"), `${USER}/IDENTITY.md`);
  });
});

describe("downloadPath", () => {
  it("defaults to a per-user directory under /tmp/openclaw-uploads", () => {
    const p = common.downloadPath(USER, `${USER}/_uploads/doc_1_ab.zip`);
    assert.equal(p, path.join("/tmp/openclaw-uploads", USER, "doc_1_ab.zip"));
  });

  it("uses only the key's base name inside a given directory", () => {
    const p = common.downloadPath(USER, `${USER}/_uploads/doc_1_ab.zip`, "/tmp/work");
    assert.equal(p, "/tmp/work/doc_1_ab.zip");
  });

  it("rejects a relative output directory", () => {
    assert.throws(() => common.downloadPath(USER, `${USER}/x.md`, "work"), /absolute/);
  });

  it("caps downloads at 25 MB", () => {
    assert.equal(common.MAX_DOWNLOAD_BYTES, 25 * 1024 * 1024);
  });
});
