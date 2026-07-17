"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  EXACT_DATABASES,
  WorkspacePathPolicy,
  WorkspacePathPolicyError,
  DEFAULT_WORKSPACE_LIMITS,
  validateRelativePath,
} = require("./workspace-path-policy");

function regularStat({ size = 0, nlink = 1 } = {}) {
  return {
    size,
    nlink,
    isFile: () => true,
    isDirectory: () => false,
    isSymbolicLink: () => false,
    isSocket: () => false,
    isFIFO: () => false,
    isBlockDevice: () => false,
    isCharacterDevice: () => false,
  };
}

function directoryStat() {
  return {
    size: 0,
    nlink: 2,
    isFile: () => false,
    isDirectory: () => true,
    isSymbolicLink: () => false,
    isSocket: () => false,
    isFIFO: () => false,
    isBlockDevice: () => false,
    isCharacterDevice: () => false,
  };
}

function specialStat(kind) {
  return {
    size: 0,
    nlink: 1,
    isFile: () => false,
    isDirectory: () => false,
    isSymbolicLink: () => kind === "symlink",
    isSocket: () => kind === "socket",
    isFIFO: () => kind === "fifo",
    isBlockDevice: () => false,
    isCharacterDevice: () => false,
  };
}

function expectPolicyError(fn, code) {
  assert.throws(fn, (error) => {
    assert.ok(error instanceof WorkspacePathPolicyError);
    assert.equal(error.code, code);
    return true;
  });
}

describe("workspace relative path validation", () => {
  it("accepts one canonical UTF-8 relative path", () => {
    assert.equal(validateRelativePath("workspace/notes/ä.md"), "workspace/notes/ä.md");
  });

  it("rejects absolute, traversal, empty-segment, backslash, and control paths", () => {
    for (const value of [
      "",
      "/workspace/a",
      "../workspace/a",
      "workspace/../a",
      "workspace//a",
      "workspace\\a",
      "workspace/a\u0000b",
      "workspace/a\u0085b",
      "workspace/a\u202eb",
      "C:\\workspace\\a",
    ]) {
      expectPolicyError(() => validateRelativePath(value), "WORKSPACE_PATH_INVALID");
    }
  });

  it("enforces UTF-8 total and segment byte limits", () => {
    expectPolicyError(
      () => validateRelativePath(`workspace/${"a".repeat(256)}`),
      "WORKSPACE_PATH_LIMIT",
    );
    expectPolicyError(
      () => validateRelativePath(`workspace/${"é".repeat(128)}`),
      "WORKSPACE_PATH_LIMIT",
    );
    const segments = Array.from({ length: 6 }, () => "a".repeat(200));
    expectPolicyError(
      () => validateRelativePath(`workspace/${segments.join("/")}`),
      "WORKSPACE_PATH_LIMIT",
    );
  });
});

describe("WorkspacePathPolicy", () => {
  const policy = new WorkspacePathPolicy();

  it("freezes the exact production ceilings and permits only stricter injected limits", () => {
    assert.deepEqual(DEFAULT_WORKSPACE_LIMITS, {
      maxEntries: 10_000,
      maxFileBytes: 64 * 1024 * 1024,
      maxGenerationBytes: 512 * 1024 * 1024,
      maxManifestBytes: 8 * 1024 * 1024,
      maxPathBytes: 1024,
      maxSegmentBytes: 255,
    });
    assert.doesNotThrow(
      () => new WorkspacePathPolicy({ limits: { maxFileBytes: 10 } }),
    );
    expectPolicyError(
      () =>
        new WorkspacePathPolicy({
          limits: { maxFileBytes: DEFAULT_WORKSPACE_LIMITS.maxFileBytes + 1 },
        }),
      "WORKSPACE_LIMITS_INVALID",
    );
  });

  it("exposes the two database roles as immutable data", () => {
    assert.deepEqual(Object.keys(EXACT_DATABASES).sort(), [
      "agents/main/agent/openclaw-agent.sqlite",
      "state/openclaw.sqlite",
    ]);
    assert.deepEqual(EXACT_DATABASES["state/openclaw.sqlite"], {
      kind: "sqlite",
      role: "global",
    });
    assert.throws(() => {
      EXACT_DATABASES["workspace/evil.sqlite"] = {
        kind: "sqlite",
        role: "global",
      };
    }, TypeError);
  });

  it("admits only workspace files and the two exact SQLite databases", () => {
    assert.deepEqual(
      policy.classify("workspace/notes.md", {
        stat: regularStat({ size: 4 }),
        content: Buffer.from("safe"),
      }),
      { action: "persist", kind: "file" },
    );
    assert.deepEqual(
      policy.classify("state/openclaw.sqlite", { stat: regularStat() }),
      { action: "persist", kind: "sqlite", role: "global" },
    );
    assert.deepEqual(
      policy.classify("agents/main/agent/openclaw-agent.sqlite", {
        stat: regularStat(),
      }),
      { action: "persist", kind: "sqlite", role: "agent", agentId: "main" },
    );

    for (const unknown of [
      "openclaw.json",
      "state/other.json",
      "agents/other/agent/openclaw-agent.sqlite",
      "agents/main/agent/other.sqlite",
    ]) {
      expectPolicyError(
        () => policy.classify(unknown, { stat: regularStat() }),
        "WORKSPACE_PATH_UNSUPPORTED",
      );
    }
  });

  it("allows traversal only through structural durable directories", () => {
    for (const directory of [
      "workspace",
      "workspace/nested",
      "state",
      "agents",
      "agents/main",
      "agents/main/agent",
    ]) {
      assert.deepEqual(policy.classify(directory, { stat: directoryStat() }), {
        action: "traverse",
      });
    }
    expectPolicyError(
      () => policy.classify("unknown", { stat: directoryStat() }),
      "WORKSPACE_PATH_UNSUPPORTED",
    );
  });

  it("excludes exact transient trees, database sidecars, suffixes, and managed files", () => {
    for (const transient of [
      "agents/main/sessions",
      "agents/main/sessions/one.jsonl",
      "sessions/legacy.json",
      "logs/runtime.jsonl",
      "delivery-queue/pending.json",
      "session-delivery-queue/pending.json",
      "state/openclaw.sqlite-wal",
      "state/openclaw.sqlite-shm",
      "state/openclaw.sqlite-journal",
      "agents/main/agent/openclaw-agent.sqlite-wal",
      "workspace/cache.tmp",
      "workspace/cache.tmp/child.txt",
      "workspace/process.pid",
      "workspace/runtime.sock",
      "workspace/write.lock",
      "workspace/AGENTS.md",
      ".personal-operator-ready.json",
    ]) {
      const stat = transient.endsWith("sessions") ? directoryStat() : regularStat();
      assert.equal(policy.classify(transient, { stat }).action, "exclude", transient);
    }
    expectPolicyError(
      () =>
        policy.classify("workspace/.personal-operator-ready.json", {
          stat: regularStat(),
          content: Buffer.alloc(0),
        }),
      "WORKSPACE_PATH_UNSUPPORTED",
    );
    expectPolicyError(
      () =>
        policy.classify("delivery-queues/pending.json", {
          stat: regularStat(),
        }),
      "WORKSPACE_PATH_UNSUPPORTED",
    );
  });

  it("requires exact managed exclusions to remain regular files", () => {
    for (const managed of ["workspace/AGENTS.md", ".personal-operator-ready.json"]) {
      expectPolicyError(
        () => policy.classify(managed, { stat: directoryStat() }),
        "WORKSPACE_FILE_TYPE_INVALID",
      );
    }
  });

  it("rejects every symlink, special file, and hardlinked regular file even in excluded trees", () => {
    for (const [relativePath, stat] of [
      ["workspace/link", specialStat("symlink")],
      ["logs/socket", specialStat("socket")],
      ["workspace/fifo", specialStat("fifo")],
      ["workspace/hardlink", regularStat({ nlink: 2 })],
    ]) {
      expectPolicyError(
        () => policy.classify(relativePath, { stat, content: Buffer.alloc(0) }),
        "WORKSPACE_FILE_TYPE_INVALID",
      );
    }
  });

  it("rejects sensitive workspace segments, basenames, key material, and databases", () => {
    for (const relativePath of [
      "workspace/.env",
      "workspace/.env.local",
      "workspace/.ssh/config",
      "workspace/secrets/note.md",
      "workspace/credentials.json",
      "workspace/user-api-keys.json",
      "workspace/private.pem",
      "workspace/private.key",
      "workspace/cache.sqlite",
      "workspace/cache.sqlite3",
      "workspace/cache.db",
      "workspace/cache.sqlite-wal",
      "workspace/cache.db-journal",
    ]) {
      expectPolicyError(
        () =>
          policy.classify(relativePath, {
            stat: regularStat(),
            content: Buffer.from("safe"),
          }),
        "WORKSPACE_SENSITIVE_PATH",
      );
    }
  });

  it("scans the full file and rejects secret content", () => {
    const content = Buffer.from(
      `${"x".repeat(80 * 1024)}\naws_access_key_id=AKIAIOSFODNN7EXAMPLE`,
    );
    expectPolicyError(
      () =>
        policy.classify("workspace/long-notes.txt", {
          stat: regularStat({ size: content.length }),
          content,
        }),
      "WORKSPACE_SECRET_DETECTED",
    );
  });

  it("rejects common secret assignments and bearer credentials", () => {
    for (const text of [
      "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
      "refresh_token=0123456789abcdef0123456789abcdef",
      "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signaturevalue",
      "npm_token=npm_abcdefghijklmnopqrstuvwxyz012345",
    ]) {
      const content = Buffer.from(text);
      expectPolicyError(
        () =>
          policy.classify("workspace/notes.txt", {
            stat: regularStat({ size: content.length }),
            content,
          }),
        "WORKSPACE_SECRET_DETECTED",
      );
    }
  });

  it("requires full content for ordinary workspace files and enforces file size", () => {
    expectPolicyError(
      () => policy.classify("workspace/a.txt", { stat: regularStat() }),
      "WORKSPACE_CONTENT_REQUIRED",
    );
    expectPolicyError(
      () =>
        policy.classify("workspace/huge.txt", {
          stat: regularStat({ size: DEFAULT_WORKSPACE_LIMITS.maxFileBytes + 1 }),
          content: Buffer.alloc(0),
        }),
      "WORKSPACE_FILE_LIMIT",
    );
    expectPolicyError(
      () =>
        policy.classify("workspace/raced.txt", {
          stat: regularStat({ size: 1 }),
          content: Buffer.from("two"),
        }),
      "WORKSPACE_CONTENT_SIZE_MISMATCH",
    );
  });
});
