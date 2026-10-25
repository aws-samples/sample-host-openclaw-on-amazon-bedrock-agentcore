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
    // "....." (5 dots) -> first pass removes 2 pairs -> "." -> rejected (leading dot)
    assert.throws(() => sanitize("....."), /leading\/trailing dots not allowed/);
  });

  it("rejects leading dots in filenames", () => {
    assert.throws(() => sanitize(".hidden"), /leading\/trailing dots not allowed/);
    assert.throws(() => sanitize(".env"), /leading\/trailing dots not allowed/);
  });

  it("rejects trailing dots in filenames", () => {
    assert.throws(() => sanitize("file."), /leading\/trailing dots not allowed/);
  });
});

describe("buildKey", () => {
  it("builds key with userId and filename", () => {
    assert.equal(
      buildKey("telegram_12345", "notes.md"),
      "telegram_12345/notes.md",
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

  it("rejects reserved system-prompt filenames", () => {
    for (const name of [
      "AGENTS.md",
      "SOUL.md",
      "USER.md",
      "IDENTITY.md",
      "TOOLS.md",
      "MEMORY.md",
    ]) {
      assert.throws(
        () => buildKey("telegram_12345", name),
        /reserved system-prompt source/,
        `expected ${name} to be rejected`,
      );
    }
  });

  it("rejects filenames inside the reserved _system/ sub-prefix", () => {
    assert.throws(
      () => buildKey("telegram_12345", "_system/SOUL.md"),
      /_system\/.*reserved/,
    );
    assert.throws(
      () => buildKey("telegram_12345", "_system"),
      /_system\/.*reserved/,
    );
  });
});

// validateUserId also requires the user_id to be this session's own user,
// read from /tmp/current-identity.json or USER_ID ("channel:id").
function asUser(actorId, fn) {
  const saved = process.env.USER_ID;
  if (actorId === undefined) delete process.env.USER_ID;
  else process.env.USER_ID = actorId;
  try {
    return fn();
  } finally {
    if (saved === undefined) delete process.env.USER_ID;
    else process.env.USER_ID = saved;
  }
}

function exitCodeOf(fn) {
  const originalExit = process.exit;
  let exitCode = null;
  process.exit = (code) => {
    exitCode = code;
    throw new Error("process.exit called");
  };
  try {
    assert.throws(fn, /process\.exit/);
  } finally {
    process.exit = originalExit;
  }
  return exitCode;
}

describe("validateUserId", () => {
  it("accepts valid telegram namespace", () => {
    asUser("telegram:123456789", () => assert.doesNotThrow(() => validateUserId("telegram_123456789")));
  });

  it("accepts valid slack namespace", () => {
    asUser("slack:sen-outlook", () => assert.doesNotThrow(() => validateUserId("slack_sen-outlook")));
  });

  it("accepts valid discord namespace", () => {
    asUser("discord:123456789012345678", () => assert.doesNotThrow(() => validateUserId("discord_123456789012345678")));
  });

  it("accepts valid line namespace", () => {
    asUser("line:U0000000000000000000000000000001", () => assert.doesNotThrow(() => validateUserId("line_U0000000000000000000000000000001")));
  });

  it("accepts valid slack namespace with uppercase ID", () => {
    asUser("slack:U0XXXXXXXX1", () => assert.doesNotThrow(() => validateUserId("slack_U0XXXXXXXX1")));
  });

  it("rejects empty userId", () => {
    asUser("telegram:123456789", () => assert.equal(exitCodeOf(() => validateUserId("")), 1));
  });

  it("rejects default-user", () => {
    asUser("telegram:123456789", () => assert.equal(exitCodeOf(() => validateUserId("default-user")), 1));
  });

  it("rejects arbitrary namespace without channel prefix", () => {
    asUser("telegram:123456789", () => assert.equal(exitCodeOf(() => validateUserId("my-custom-id")), 1));
  });

  it("rejects path traversal attempts", () => {
    asUser("telegram:123456789", () => assert.equal(exitCodeOf(() => validateUserId("../other_user")), 1));
  });

  it("rejects a valid namespace that is not this session's user", () => {
    asUser("telegram:123456789", () => assert.equal(exitCodeOf(() => validateUserId("telegram_987654321")), 1));
  });

  it("rejects a longer namespace that shares the session user's prefix", () => {
    asUser("telegram:1", () => assert.equal(exitCodeOf(() => validateUserId("telegram_12")), 1));
  });

  it("rejects every user_id when the session's user is unknown", () => {
    asUser(undefined, () => assert.equal(exitCodeOf(() => validateUserId("telegram_123456789")), 1));
  });

  it("does not treat INTERNAL_USER_ID as the session's namespace", () => {
    // INTERNAL_USER_ID carries the internal user id, not the channel_id
    // namespace, so it must never authorize a user_id on its own.
    const saved = process.env.INTERNAL_USER_ID;
    process.env.INTERNAL_USER_ID = "telegram:123456789";
    try {
      asUser(undefined, () => assert.equal(exitCodeOf(() => validateUserId("telegram_123456789")), 1));
    } finally {
      if (saved === undefined) delete process.env.INTERNAL_USER_ID;
      else process.env.INTERNAL_USER_ID = saved;
    }
  });
});
