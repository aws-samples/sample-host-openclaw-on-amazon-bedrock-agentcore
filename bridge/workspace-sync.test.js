/**
 * Tests for workspace-sync.js — credential configuration, skip patterns,
 * and credential detection guard for S3 isolation.
 *
 * Covers: configureCredentials(), shouldSkip(), detectCredentials(),
 *         credential validation, client replacement.
 * Note: S3Client creation is tested implicitly (SDK only in Docker image).
 * Run: cd bridge && node --test workspace-sync.test.js
 */
const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");

describe("workspace-sync credentials", () => {
  let workspaceSync;

  beforeEach(() => {
    // Fresh module on each test
    delete require.cache[require.resolve("./workspace-sync")];
    process.env.AWS_REGION = "us-west-2";
    process.env.S3_USER_FILES_BUCKET = "test-bucket";
    workspaceSync = require("./workspace-sync");
  });

  afterEach(() => {
    delete process.env.S3_USER_FILES_BUCKET;
  });

  it("exports configureCredentials function", () => {
    assert.equal(typeof workspaceSync.configureCredentials, "function");
  });

  it("exports getS3Client function", () => {
    assert.equal(typeof workspaceSync.getS3Client, "function");
  });

  it("configureCredentials accepts valid credentials without throwing", () => {
    // Should not throw (S3Client created lazily, not at configureCredentials time)
    workspaceSync.configureCredentials({
      accessKeyId: "AKIAIOSFODNN7EXAMPLE",
      secretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
      sessionToken: "FwoGZXIvYXdzEBYaDH...",
    });
  });

  it("rejects configureCredentials with missing accessKeyId", () => {
    assert.throws(
      () =>
        workspaceSync.configureCredentials({
          secretAccessKey: "secret",
          sessionToken: "token",
        }),
      /accessKeyId/i,
    );
  });

  it("rejects configureCredentials with missing secretAccessKey", () => {
    assert.throws(
      () =>
        workspaceSync.configureCredentials({
          accessKeyId: "AKIAEXAMPLE",
          sessionToken: "token",
        }),
      /secretAccessKey/i,
    );
  });

  it("rejects configureCredentials with null credentials", () => {
    assert.throws(
      () => workspaceSync.configureCredentials(null),
      /accessKeyId/i,
    );
  });

  it("rejects configureCredentials with empty object", () => {
    assert.throws(
      () => workspaceSync.configureCredentials({}),
      /accessKeyId/i,
    );
  });
});

// --- shouldSkip ---

describe("shouldSkip", () => {
  let shouldSkip;

  beforeEach(() => {
    delete require.cache[require.resolve("./workspace-sync")];
    process.env.AWS_REGION = "us-west-2";
    process.env.S3_USER_FILES_BUCKET = "test-bucket";
    shouldSkip = require("./workspace-sync").shouldSkip;
  });

  afterEach(() => {
    delete process.env.S3_USER_FILES_BUCKET;
  });

  // Original patterns
  it("skips node_modules/ directory", () => {
    assert.ok(shouldSkip("node_modules/some-package/index.js"));
  });

  it("skips .cache/ directory", () => {
    assert.ok(shouldSkip(".cache/data"));
  });

  it("skips *.log files", () => {
    assert.ok(shouldSkip("debug.log"));
    assert.ok(shouldSkip("subdir/error.log"));
  });

  it("skips *.lock files", () => {
    assert.ok(shouldSkip("yarn.lock"));
  });

  it("skips openclaw.json", () => {
    assert.ok(shouldSkip("openclaw.json"));
  });

  // New security patterns
  it("skips .env files", () => {
    assert.ok(shouldSkip(".env"));
  });

  it("skips .secrets/ directory", () => {
    assert.ok(shouldSkip(".secrets/api-key.txt"));
  });

  it("skips *.pem files", () => {
    assert.ok(shouldSkip("cert.pem"));
    assert.ok(shouldSkip("subdir/private.pem"));
  });

  it("skips *.key files", () => {
    assert.ok(shouldSkip("server.key"));
    assert.ok(shouldSkip("tls/private.key"));
  });

  // Should NOT skip
  it("does not skip user-api-keys.json", () => {
    assert.ok(!shouldSkip("user-api-keys.json"));
  });

  it("does not skip regular files", () => {
    assert.ok(!shouldSkip("notes.md"));
    assert.ok(!shouldSkip("data/config.yaml"));
  });

  it("skips AGENTS.md (regenerated on init, not synced from S3)", () => {
    assert.ok(shouldSkip("AGENTS.md"));
    assert.ok(shouldSkip("workspace/AGENTS.md"));
  });

  // OpenClaw 2.0 SQLite state (agents/<id>/agent/openclaw-agent.sqlite, WAL mode)
  it("skips SQLite WAL/SHM/journal sidecars (only valid with the exact main-db bytes)", () => {
    assert.ok(shouldSkip("agents/main/agent/openclaw-agent.sqlite-wal"));
    assert.ok(shouldSkip("agents/main/agent/openclaw-agent.sqlite-shm"));
    assert.ok(shouldSkip("agents/main/agent/openclaw-agent.sqlite-journal"));
    assert.ok(shouldSkip("openclaw.sqlite-wal"));
  });

  it("skips in-flight SQLite snapshot staging files", () => {
    assert.ok(shouldSkip("agents/main/agent/.openclaw-agent.sqlite.123-456.sqlite-snapshot"));
  });

  it("does not skip the main SQLite database (uploaded as a snapshot)", () => {
    assert.ok(!shouldSkip("agents/main/agent/openclaw-agent.sqlite"));
    assert.ok(!shouldSkip("openclaw.sqlite"));
  });

  it("skips doctor-rotated openclaw.json backups", () => {
    assert.ok(shouldSkip("openclaw.json.bak"));
    assert.ok(shouldSkip("openclaw.json.bak.1"));
    assert.ok(shouldSkip("openclaw.json.bak.4"));
  });
});

// --- snapshotSqlite ---

const hasNodeSqlite = (() => {
  try {
    return typeof require("node:sqlite").backup === "function";
  } catch {
    return false;
  }
})();

describe("snapshotSqlite", () => {
  const os = require("node:os");
  const fs = require("node:fs");
  const path = require("node:path");
  let workspaceSync;
  let tmpDir;

  beforeEach(() => {
    delete require.cache[require.resolve("./workspace-sync")];
    process.env.AWS_REGION = "us-west-2";
    process.env.S3_USER_FILES_BUCKET = "test-bucket";
    workspaceSync = require("./workspace-sync");
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "oc-sqlite-snap-"));
  });

  afterEach(() => {
    delete process.env.S3_USER_FILES_BUCKET;
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it("exports snapshotSqlite function", () => {
    assert.equal(typeof workspaceSync.snapshotSqlite, "function");
  });

  it(
    "rejects (instead of copying a live file) when node:sqlite is unavailable",
    { skip: hasNodeSqlite && "node:sqlite present on this runtime" },
    async () => {
      const dbPath = path.join(tmpDir, "openclaw-agent.sqlite");
      fs.writeFileSync(dbPath, "not a real db");
      await assert.rejects(
        () => workspaceSync.snapshotSqlite(dbPath),
        /node:sqlite/,
      );
    },
  );

  it(
    "returns a consistent standalone snapshot of a live WAL-mode database",
    { skip: !hasNodeSqlite && "node:sqlite not available on this Node" },
    async () => {
      const { DatabaseSync } = require("node:sqlite");
      const dbPath = path.join(tmpDir, "openclaw-agent.sqlite");

      // Writer stays OPEN for the whole test — simulates the running gateway.
      const writer = new DatabaseSync(dbPath);
      writer.exec("PRAGMA journal_mode = WAL");
      // Large autocheckpoint so the rows below stay in the -wal file only:
      // a raw copy of the main db would NOT contain them.
      writer.exec("PRAGMA wal_autocheckpoint = 100000");
      writer.exec("CREATE TABLE sessions (id TEXT PRIMARY KEY, body TEXT)");
      const insert = writer.prepare("INSERT INTO sessions VALUES (?, ?)");
      for (let i = 0; i < 50; i++) insert.run(`s${i}`, "x".repeat(200));
      assert.ok(fs.existsSync(`${dbPath}-wal`), "test precondition: WAL file exists");
      assert.ok(fs.statSync(`${dbPath}-wal`).size > 0, "test precondition: WAL holds frames");

      const bytes = await workspaceSync.snapshotSqlite(dbPath);

      // Snapshot is a real SQLite file containing the committed-but-not-yet-checkpointed rows.
      assert.equal(bytes.subarray(0, 15).toString(), "SQLite format 3");
      const snapPath = path.join(tmpDir, "restored.sqlite");
      fs.writeFileSync(snapPath, bytes);
      const restored = new DatabaseSync(snapPath, { readOnly: true });
      const { n } = restored.prepare("SELECT COUNT(*) AS n FROM sessions").get();
      assert.equal(n, 50);
      assert.equal(restored.prepare("PRAGMA integrity_check").get().integrity_check, "ok");
      restored.close();

      // Staging file cleaned up, source untouched.
      const leftovers = fs.readdirSync(tmpDir).filter((f) => f.includes("sqlite-snapshot"));
      assert.deepEqual(leftovers, []);
      assert.equal(writer.prepare("SELECT COUNT(*) AS n FROM sessions").get().n, 50);
      writer.close();
    },
  );

  it(
    "saveWorkspace uploads the snapshot, not the raw db, and skips the sidecars",
    { skip: !hasNodeSqlite && "node:sqlite not available on this Node" },
    async () => {
      // Point LOCAL_PATH ($HOME/.openclaw) at a temp state dir.
      const savedHome = process.env.HOME;
      process.env.HOME = tmpDir;
      delete require.cache[require.resolve("./workspace-sync")];
      workspaceSync = require("./workspace-sync");
      try {
        const { DatabaseSync } = require("node:sqlite");
        const agentDir = path.join(tmpDir, ".openclaw", "agents", "main", "agent");
        fs.mkdirSync(agentDir, { recursive: true });
        const dbPath = path.join(agentDir, "openclaw-agent.sqlite");
        const writer = new DatabaseSync(dbPath);
        writer.exec("PRAGMA journal_mode = WAL");
        writer.exec("PRAGMA wal_autocheckpoint = 100000");
        writer.exec("CREATE TABLE t (v TEXT)");
        writer.exec("INSERT INTO t VALUES ('only-in-wal')");
        fs.writeFileSync(path.join(tmpDir, ".openclaw", "notes.md"), "hello");

        // Stub the S3 client: capture PutObject keys and bodies.
        const puts = [];
        workspaceSync.configureCredentials({
          accessKeyId: "AKIAIOSFODNN7EXAMPLE",
          secretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        });
        const fakeS3 = { send: async (cmd) => { puts.push(cmd); return {}; } };
        // getS3Client() lazily requires @aws-sdk/client-s3 (absent locally) —
        // intercept that require and hand back the stub.
        const Module = require("node:module");
        const realLoad = Module._load;
        Module._load = function (request, ...rest) {
          if (request === "@aws-sdk/client-s3") {
            return {
              S3Client: function () { return fakeS3; },
              PutObjectCommand: function (input) { this.input = input; },
              GetObjectCommand: function (input) { this.input = input; },
              ListObjectsV2Command: function (input) { this.input = input; },
            };
          }
          return realLoad.call(this, request, ...rest);
        };
        let rawCopy;
        try {
          await workspaceSync.saveWorkspace("telegram_1");
          // Copy the raw main-db file while the writer is still open (closing the
          // last connection would checkpoint the WAL into it).
          rawCopy = path.join(tmpDir, "raw.sqlite");
          fs.copyFileSync(dbPath, rawCopy);
        } finally {
          Module._load = realLoad;
          writer.close();
        }

        const keys = puts.map((c) => c.input.Key).sort();
        assert.deepEqual(keys, [
          "telegram_1/.openclaw/agents/main/agent/openclaw-agent.sqlite",
          "telegram_1/.openclaw/notes.md",
        ]);
        const dbPut = puts.find((c) => c.input.Key.endsWith(".sqlite"));
        // Uploaded body is a self-contained snapshot that includes the WAL-only row.
        const snapPath = path.join(tmpDir, "uploaded.sqlite");
        fs.writeFileSync(snapPath, dbPut.input.Body);
        const check = new DatabaseSync(snapPath, { readOnly: true });
        assert.equal(check.prepare("SELECT v FROM t").get().v, "only-in-wal");
        check.close();
        // The raw main-db file on disk did NOT contain that row (still in WAL),
        // which is exactly why a plain readFileSync upload would be wrong.
        const raw = new DatabaseSync(rawCopy, { readOnly: true });
        let rawRows;
        try {
          rawRows = raw.prepare("SELECT COUNT(*) AS n FROM t").get().n;
        } catch (err) {
          assert.match(err.message, /no such table/); // schema itself still only in WAL
          rawRows = 0;
        }
        assert.equal(rawRows, 0);
        raw.close();
      } finally {
        process.env.HOME = savedHome;
      }
    },
  );
});

// --- detectCredentials ---

describe("detectCredentials", () => {
  let detectCredentials;

  beforeEach(() => {
    delete require.cache[require.resolve("./workspace-sync")];
    process.env.AWS_REGION = "us-west-2";
    process.env.S3_USER_FILES_BUCKET = "test-bucket";
    detectCredentials = require("./workspace-sync").detectCredentials;
  });

  afterEach(() => {
    delete process.env.S3_USER_FILES_BUCKET;
  });

  it("detects AWS access key IDs", () => {
    const content = 'aws_access_key_id = AKIAIOSFODNN7EXAMPLE';
    assert.ok(detectCredentials(content));
  });

  it("detects OpenAI API keys", () => {
    const content = 'OPENAI_API_KEY=sk-proj1234567890abcdefghij';
    assert.ok(detectCredentials(content));
  });

  it("detects Slack tokens", () => {
    const content = 'token: xoxb-123456789012-abcdefgh';
    assert.ok(detectCredentials(content));
  });

  it("detects Telegram bot tokens", () => {
    // Telegram tokens: 8-10 digit bot ID + colon + exactly 35 alphanumeric/dash/underscore chars
    const content = '123456789:ABCdefGHI_jklMNOpqrSTUvwxYZ01234567';
    assert.ok(detectCredentials(content));
  });

  it("detects private key headers", () => {
    const content = '-----BEGIN RSA PRIVATE KEY-----\nMIIEpAI...';
    assert.ok(detectCredentials(content));
  });

  it("detects GitHub personal access tokens", () => {
    const content = 'GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij';
    assert.ok(detectCredentials(content));
  });

  it("detects GitLab personal access tokens", () => {
    const content = 'GL_TOKEN=glpat-abcdef1234567890abcdef';
    assert.ok(detectCredentials(content));
  });

  it("returns null for safe content", () => {
    assert.equal(detectCredentials("Hello, this is a normal document."), null);
  });

  it("returns null for empty content", () => {
    assert.equal(detectCredentials(""), null);
  });

  it("returns null for JSON without secrets", () => {
    const content = JSON.stringify({ name: "test", value: 42 });
    assert.equal(detectCredentials(content), null);
  });

  it("works with Buffer input", () => {
    const content = Buffer.from('OPENAI_API_KEY=sk-proj1234567890abcdefghij');
    assert.ok(detectCredentials(content));
  });

  it("works with large Buffer (scans first 64KB only)", () => {
    // Create a buffer larger than 64KB with a secret near the start
    const secret = 'AKIAIOSFODNN7EXAMPLE';
    const padding = "x".repeat(100 * 1024);
    const content = Buffer.from(secret + padding);
    assert.ok(detectCredentials(content));
  });
});

// --- CREDENTIAL_SCAN_EXEMPT ---

describe("CREDENTIAL_SCAN_EXEMPT", () => {
  let CREDENTIAL_SCAN_EXEMPT;

  beforeEach(() => {
    delete require.cache[require.resolve("./workspace-sync")];
    process.env.AWS_REGION = "us-west-2";
    process.env.S3_USER_FILES_BUCKET = "test-bucket";
    CREDENTIAL_SCAN_EXEMPT = require("./workspace-sync").CREDENTIAL_SCAN_EXEMPT;
  });

  afterEach(() => {
    delete process.env.S3_USER_FILES_BUCKET;
  });

  it("is user-api-keys.json", () => {
    assert.equal(CREDENTIAL_SCAN_EXEMPT, "user-api-keys.json");
  });
});

// --- setBackupMode / periodic-save interval selection ---

describe("setBackupMode", () => {
  let workspaceSync;
  let realSetInterval;
  let realClearInterval;
  let capturedIntervalMs;

  beforeEach(() => {
    delete require.cache[require.resolve("./workspace-sync")];
    process.env.AWS_REGION = "us-west-2";
    process.env.S3_USER_FILES_BUCKET = "test-bucket";
    delete process.env.WORKSPACE_SYNC_INTERVAL_MS;
    workspaceSync = require("./workspace-sync");

    // Capture the interval startPeriodicSave picks without actually scheduling.
    capturedIntervalMs = null;
    realSetInterval = global.setInterval;
    realClearInterval = global.clearInterval;
    global.setInterval = (_fn, ms) => {
      capturedIntervalMs = ms;
      return { unref() {} }; // fake handle; never fires
    };
    global.clearInterval = () => {};
  });

  afterEach(() => {
    global.setInterval = realSetInterval;
    global.clearInterval = realClearInterval;
    delete process.env.S3_USER_FILES_BUCKET;
  });

  it("exports setBackupMode function", () => {
    assert.equal(typeof workspaceSync.setBackupMode, "function");
  });

  it("uses the 5 min primary interval when backup mode is off (default)", () => {
    workspaceSync.startPeriodicSave("test-namespace");
    assert.equal(capturedIntervalMs, 5 * 60 * 1000);
  });

  it("uses the 30 min backup interval after setBackupMode(true)", () => {
    workspaceSync.setBackupMode(true);
    workspaceSync.startPeriodicSave("test-namespace");
    assert.equal(capturedIntervalMs, 30 * 60 * 1000);
  });

  it("reverts to the 5 min primary interval after setBackupMode(false)", () => {
    workspaceSync.setBackupMode(true);
    workspaceSync.setBackupMode(false);
    workspaceSync.startPeriodicSave("test-namespace");
    assert.equal(capturedIntervalMs, 5 * 60 * 1000);
  });

  it("honors an explicit intervalMs override regardless of backup mode", () => {
    workspaceSync.setBackupMode(true);
    workspaceSync.startPeriodicSave("test-namespace", 1234);
    assert.equal(capturedIntervalMs, 1234);
  });
});

// --- AGENTS.md template validation ---

describe("AGENTS.md template in agentcore-contract.js", () => {
  const fs = require("fs");
  const path = require("path");
  const source = fs.readFileSync(
    path.join(__dirname, "agentcore-contract.js"),
    "utf-8",
  );

  it("does not tell the LLM to 'Read' skill files (read tool is denied)", () => {
    // The read tool is denied in OpenClaw's tool profile, so AGENTS.md must not
    // instruct the LLM to read SKILL.md files — it will fail and confuse the LLM.
    assert.ok(
      !source.includes("Read the eventbridge-cron SKILL.md"),
      "AGENTS.md should not tell the LLM to 'Read the eventbridge-cron SKILL.md' — read tool is denied",
    );
  });

  it("includes explicit eventbridge-cron exec commands", () => {
    // The LLM needs explicit node commands for the eventbridge-cron skill
    assert.ok(
      source.includes("node /skills/eventbridge-cron/create.js"),
      "AGENTS.md should include explicit create schedule command",
    );
    assert.ok(
      source.includes("node /skills/eventbridge-cron/list.js"),
      "AGENTS.md should include explicit list schedules command",
    );
  });
});

// --- walkDir ---
describe("walkDir", () => {
  const fs = require("fs");
  const os = require("os");
  const path = require("path");
  let workspaceSync;

  beforeEach(() => {
    delete require.cache[require.resolve("./workspace-sync")];
    workspaceSync = require("./workspace-sync");
  });

  it("follows the workspace symlink onto session storage and guards against cycles", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "ws-walk-"));
    const mount = fs.mkdtempSync(path.join(os.tmpdir(), "ws-walk-mount-"));
    fs.mkdirSync(path.join(mount, "memory"), { recursive: true });
    fs.writeFileSync(path.join(mount, "memory", "note.md"), "m");
    fs.writeFileSync(path.join(root, "openclaw.json"), "{}");
    fs.symlinkSync(mount, path.join(root, "workspace"));
    fs.symlinkSync(root, path.join(mount, "loop")); // cycle back to root
    fs.symlinkSync(path.join(root, "missing"), path.join(root, "dangling"));
    const files = workspaceSync.walkDir(root).sort();
    assert.deepEqual(files, ["openclaw.json", "workspace/memory/note.md"]);
  });
});
