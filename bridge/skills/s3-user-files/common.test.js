/**
 * Tests for s3-user-files common utilities.
 * Run: node --test common.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { sanitize, buildKey, validateUserId } = require("./common");

describe("sanitize", () => {
  it("passes through simple alphanumeric strings", () => {
    assert.equal(sanitize("telegram_12345"), "telegram_12345");
  });

  it("replaces colons with underscores", () => {
    assert.equal(sanitize("telegram:12345"), "telegram_12345");
  });

  it("removes path traversal sequences", () => {
    // ".." removed, remaining "/" become "_"
    assert.equal(sanitize("../../../etc/passwd"), "___etc_passwd");
  });

  it("replaces slashes with underscores", () => {
    assert.equal(sanitize("foo/bar/baz"), "foo_bar_baz");
  });

  it("allows hyphens and dots", () => {
    assert.equal(sanitize("my-file.md"), "my-file.md");
  });

  it("truncates to 256 characters", () => {
    const long = "a".repeat(300);
    assert.equal(sanitize(long).length, 256);
  });

  it("handles empty string", () => {
    assert.equal(sanitize(""), "");
  });

  it("replaces spaces with underscores", () => {
    assert.equal(sanitize("John Doe"), "John_Doe");
  });

  it("handles Slack user IDs", () => {
    assert.equal(sanitize("slack:U0123456789"), "slack_U0123456789");
  });

  it("iteratively removes nested dot-dot sequences", () => {
    // "......" (6 dots) -> first pass removes 3 pairs -> ""
    assert.equal(sanitize("......"), "");
    // "....." (5 dots) -> first pass removes 2 pairs -> "." -> no more ".."
    assert.equal(sanitize("....."), ".");
  });
});

describe("buildKey", () => {
  it("builds key with userId and filename", () => {
    assert.equal(
      buildKey("telegram_12345", "IDENTITY.md"),
      "telegram_12345/IDENTITY.md",
    );
  });

  it("sanitizes userId in key", () => {
    assert.equal(
      buildKey("telegram:12345", "notes.md"),
      "telegram_12345/notes.md",
    );
  });

  it("returns prefix with trailing slash when no filename", () => {
    assert.equal(buildKey("telegram_12345"), "telegram_12345/");
  });

  it("sanitizes both userId and filename", () => {
    assert.equal(
      buildKey("../admin", "../../etc/passwd"),
      "_admin/__etc_passwd",
    );
  });
});

describe("validateUserId", () => {
  it("accepts valid telegram namespace", () => {
    assert.doesNotThrow(() => validateUserId("telegram_123456789"));
  });

  it("accepts valid slack namespace", () => {
    assert.doesNotThrow(() => validateUserId("slack_sen-outlook"));
  });

  it("accepts valid discord namespace", () => {
    assert.doesNotThrow(() => validateUserId("discord_123456789012345678"));
  });

  it("accepts valid slack namespace with uppercase ID", () => {
    assert.doesNotThrow(() => validateUserId("slack_U0AGD41CBGS"));
  });

  it("rejects empty userId", () => {
    // validateUserId calls process.exit, so we mock it
    const originalExit = process.exit;
    let exitCode = null;
    process.exit = (code) => {
      exitCode = code;
      throw new Error("process.exit called");
    };
    try {
      assert.throws(() => validateUserId(""), /process\.exit/);
      assert.equal(exitCode, 1);
    } finally {
      process.exit = originalExit;
    }
  });

  it("rejects default-user", () => {
    const originalExit = process.exit;
    let exitCode = null;
    process.exit = (code) => {
      exitCode = code;
      throw new Error("process.exit called");
    };
    try {
      assert.throws(() => validateUserId("default-user"), /process\.exit/);
      assert.equal(exitCode, 1);
    } finally {
      process.exit = originalExit;
    }
  });

  it("rejects arbitrary namespace without channel prefix", () => {
    const originalExit = process.exit;
    let exitCode = null;
    process.exit = (code) => {
      exitCode = code;
      throw new Error("process.exit called");
    };
    try {
      assert.throws(() => validateUserId("my-custom-id"), /process\.exit/);
      assert.equal(exitCode, 1);
    } finally {
      process.exit = originalExit;
    }
  });

  it("rejects path traversal attempts", () => {
    const originalExit = process.exit;
    let exitCode = null;
    process.exit = (code) => {
      exitCode = code;
      throw new Error("process.exit called");
    };
    try {
      assert.throws(() => validateUserId("../other_user"), /process\.exit/);
      assert.equal(exitCode, 1);
    } finally {
      process.exit = originalExit;
    }
  });
});
