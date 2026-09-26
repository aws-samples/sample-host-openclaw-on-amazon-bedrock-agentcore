/**
 * Tests for workspace-sync.js — credential configuration, skip patterns,
 * credential detection guard for S3 isolation, SQLite snapshots, single-file
 * saves and the change-driven backup.
 *
 * Covers: configureCredentials(), shouldSkip(), detectCredentials(),
 *         credential validation, client replacement, snapshotSqlite(),
 *         saveFile(), startChangeBackup()/flushPendingSaves()/stopChangeBackup()
 *         (debounce, max wait, per-flush cap, hash dedupe, SQLite sidecar
 *         mapping, SQLite-only throttle, failure handling), the upload gate
 *         (nothing uploaded before/without a completed restore), the
 *         fill-missing restore mode (incl. kept files verified against the
 *         sha256 stored as S3 object metadata), large SQLite databases (gzip
 *         multipart upload, streaming restore of gzip and legacy objects,
 *         hash-skip, hard cap, longer cadence) and the SIGTERM wiring in the
 *         contract.
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
    workspaceSync._setRestoreStateForTests("ready"); // save paths under test, no restore here
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
      workspaceSync._setRestoreStateForTests("ready"); // fresh module: reopen the upload gate
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

// --- awaitRestore (bounded wait before the gateway spawns) ---

describe("awaitRestore", () => {
  const { mock } = require("node:test");
  let workspaceSync;
  let warnings;
  let log;

  beforeEach(() => {
    delete require.cache[require.resolve("./workspace-sync")];
    process.env.AWS_REGION = "us-west-2";
    process.env.S3_USER_FILES_BUCKET = "test-bucket";
    workspaceSync = require("./workspace-sync");
    warnings = [];
    log = { log() {}, warn: (m) => warnings.push(m), error() {} };
    mock.timers.enable({ apis: ["setTimeout"] });
  });

  afterEach(() => {
    mock.timers.reset();
    delete process.env.S3_USER_FILES_BUCKET;
  });

  it("exports awaitRestore function", () => {
    assert.equal(typeof workspaceSync.awaitRestore, "function");
  });

  it("fast restore: resolves without a warning, even after the wait elapses", async () => {
    const result = await workspaceSync.awaitRestore(Promise.resolve(), 45000, { log });
    assert.equal(result, "restored");
    assert.deepEqual(warnings, []);

    // The bug: the wait timer used to keep running after the restore won the
    // race and logged "still running after 45000ms" on every boot.
    mock.timers.tick(45000);
    await Promise.resolve();
    assert.deepEqual(warnings, [], "no timeout warning once the restore has settled");
  });

  it("failed restore: logs the failure, resolves, and never warns about a timeout", async () => {
    const result = await workspaceSync.awaitRestore(Promise.reject(new Error("boom")), 45000, { log });
    assert.equal(result, "restored");
    assert.deepEqual(warnings, ["[contract] Workspace restore failed: boom"]);

    mock.timers.tick(45000);
    await Promise.resolve();
    assert.equal(warnings.length, 1, "no timeout warning after a settled (failed) restore");
  });

  it("slow restore: warns after the wait and resolves so the gateway can start", async () => {
    let finishRestore;
    const slow = new Promise((resolve) => { finishRestore = resolve; });
    const pending = workspaceSync.awaitRestore(slow, 45000, { log });

    mock.timers.tick(44999);
    await Promise.resolve();
    assert.deepEqual(warnings, [], "no warning before the wait elapses");

    mock.timers.tick(1);
    const result = await pending;
    assert.equal(result, "timeout");
    assert.deepEqual(warnings, [
      "[contract] Workspace restore still running after 45000ms — starting gateway anyway",
    ]);

    // A late-finishing restore must not produce any further output.
    finishRestore();
    await Promise.resolve();
    assert.equal(warnings.length, 1);
  });

  it("does not keep the process alive while waiting (timer is unref'd)", async () => {
    let unrefCalled = false;
    let cleared = null;
    const handle = { unref() { unrefCalled = true; } };
    const timers = {
      setTimeout() { return handle; },
      clearTimeout(t) { cleared = t; },
    };
    await workspaceSync.awaitRestore(Promise.resolve(), 45000, { log, timers });
    assert.equal(unrefCalled, true);
    assert.equal(cleared, handle, "wait timer is cleared once the restore settles");
  });
});

describe("restore wait wiring in agentcore-contract.js", () => {
  const fs = require("fs");
  const path = require("path");
  const source = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf-8");

  it("uses awaitRestore for the bounded pre-spawn restore wait", () => {
    assert.ok(
      source.includes("await workspaceSync.awaitRestore(restorePromise, RESTORE_WAIT_MS);"),
      "contract should await the restore via workspaceSync.awaitRestore",
    );
  });

  it("no longer races an uncleared setTimeout against the restore", () => {
    assert.equal(source.includes("Workspace restore still running after ${RESTORE_WAIT_MS}ms"), false);
  });

  it("always restores from S3: fills missing files when the mount already has data instead of skipping", () => {
    assert.equal(source.includes("skipping S3 restore"), false, "the non-empty mount no longer skips the restore");
    assert.ok(source.includes("workspaceSync.restoreWorkspace(namespace, { overwrite: false })"));
    // Every branch assigns a restore promise and the wait is unconditional.
    assert.equal(source.includes("let restorePromise = null;"), false);
    assert.equal(source.includes("if (restorePromise) {"), false);
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

// --- saveFile (single-file immediate backup, used for the runtime-skills manifest) ---

describe("saveFile", () => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const Module = require("node:module");
  let workspaceSync;
  let tmpDir;
  let savedHome;
  let realLoad;
  let puts;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "ws-savefile-"));
    savedHome = process.env.HOME;
    process.env.HOME = tmpDir;
    fs.mkdirSync(path.join(tmpDir, ".openclaw"), { recursive: true });
    delete require.cache[require.resolve("./workspace-sync")];
    process.env.AWS_REGION = "us-west-2";
    process.env.S3_USER_FILES_BUCKET = "test-bucket";
    workspaceSync = require("./workspace-sync");
    workspaceSync.configureCredentials({
      accessKeyId: "AKIAIOSFODNN7EXAMPLE",
      secretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    });
    puts = [];
    workspaceSync._setRestoreStateForTests("ready"); // save paths under test, no restore here
    const fakeS3 = { send: async (cmd) => { puts.push(cmd); return {}; } };
    realLoad = Module._load;
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
  });

  afterEach(() => {
    Module._load = realLoad;
    process.env.HOME = savedHome;
    delete process.env.S3_USER_FILES_BUCKET;
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it("uploads one file under the same {namespace}/.openclaw/ key as saveWorkspace", async () => {
    fs.writeFileSync(path.join(tmpDir, ".openclaw", "runtime-skills.json"), '{"version":1,"skills":{}}\n');
    assert.equal(await workspaceSync.saveFile("telegram_1", "runtime-skills.json"), true);
    assert.equal(puts.length, 1);
    assert.equal(puts[0].input.Bucket, "test-bucket");
    assert.equal(puts[0].input.Key, "telegram_1/.openclaw/runtime-skills.json");
    assert.equal(puts[0].input.Body.toString(), '{"version":1,"skills":{}}\n');
  });

  it("is a no-op for a missing file, a skipped pattern, or a path outside the state dir", async () => {
    assert.equal(await workspaceSync.saveFile("telegram_1", "runtime-skills.json"), false);
    fs.writeFileSync(path.join(tmpDir, ".openclaw", "x.log"), "log");
    assert.equal(await workspaceSync.saveFile("telegram_1", "x.log"), false);
    fs.writeFileSync(path.join(tmpDir, "outside.json"), "{}");
    assert.equal(await workspaceSync.saveFile("telegram_1", "../outside.json"), false);
    assert.equal(await workspaceSync.saveFile("telegram_1", "/etc/passwd"), false);
    assert.equal(await workspaceSync.saveFile("", "runtime-skills.json"), false);
    assert.equal(puts.length, 0);
  });
});

// --- restore seeds the upload dedupe -----------------------------------------
//
// Staging (PR #114 test): the SIGTERM flush re-uploaded every file the cold
// start had just restored (workspace/*.md, plugin-skills, runtime-skills.json)
// although nothing had touched them, because the hash cache only learned about
// files this process had uploaded. Restore must seed it with what it wrote.

describe("restoreWorkspace seeds the upload hash cache", () => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const Module = require("node:module");
  let workspaceSync;
  let tmpDir;
  let savedHome;
  let realLoad;
  let puts;
  let heads;
  let objects;
  let metadata; // Key -> { sha256 } as HeadObject would return it (absent = legacy object)
  let failGets;
  let failHeads;
  const sha256 = (body) => require("node:crypto").createHash("sha256").update(body).digest("hex");

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "ws-restore-"));
    savedHome = process.env.HOME;
    process.env.HOME = tmpDir;
    fs.mkdirSync(path.join(tmpDir, ".openclaw"), { recursive: true });
    delete require.cache[require.resolve("./workspace-sync")];
    process.env.AWS_REGION = "us-west-2";
    process.env.S3_USER_FILES_BUCKET = "test-bucket";
    workspaceSync = require("./workspace-sync");
    workspaceSync.configureCredentials({
      accessKeyId: "AKIAIOSFODNN7EXAMPLE",
      secretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    });
    puts = [];
    heads = [];
    failGets = new Set();
    failHeads = new Set();
    objects = {
      "telegram_1/.openclaw/workspace/SOUL.md": "soul v1\n",
      "telegram_1/.openclaw/runtime-skills.json": '{"version":1,"skills":{}}\n',
    };
    // Every object was uploaded by this code, so it carries the sha256 metadata,
    // unless a test deletes the entry to model an object from before the fix.
    metadata = Object.fromEntries(
      Object.entries(objects).map(([Key, body]) => [Key, { sha256: sha256(body) }]),
    );
    const fakeS3 = {
      send: async (cmd) => {
        if (cmd.kind === "list") {
          return {
            Contents: Object.entries(objects).map(([Key, body]) => ({ Key, Size: Buffer.byteLength(body) })),
            IsTruncated: false,
          };
        }
        if (cmd.kind === "get") {
          if (failGets.has(cmd.input.Key)) throw new Error("boom");
          const body = Buffer.from(objects[cmd.input.Key]);
          return { Body: (async function* () { yield body; })() };
        }
        if (cmd.kind === "head") {
          heads.push(cmd);
          if (failHeads.has(cmd.input.Key)) throw new Error("head boom");
          if (!(cmd.input.Key in objects)) throw new Error("NotFound");
          const meta = metadata[cmd.input.Key];
          return { ContentLength: Buffer.byteLength(objects[cmd.input.Key]), ...(meta ? { Metadata: meta } : {}) };
        }
        puts.push(cmd);
        // Uploads store the hash, exactly as S3 would keep it for the next HEAD.
        objects[cmd.input.Key] = Buffer.from(cmd.input.Body);
        metadata[cmd.input.Key] = cmd.input.Metadata;
        return {};
      },
    };
    realLoad = Module._load;
    Module._load = function (request, ...rest) {
      if (request === "@aws-sdk/client-s3") {
        return {
          S3Client: function () { return fakeS3; },
          PutObjectCommand: function (input) { this.kind = "put"; this.input = input; },
          GetObjectCommand: function (input) { this.kind = "get"; this.input = input; },
          HeadObjectCommand: function (input) { this.kind = "head"; this.input = input; },
          ListObjectsV2Command: function (input) { this.kind = "list"; this.input = input; },
        };
      }
      return realLoad.call(this, request, ...rest);
    };
  });

  afterEach(() => {
    Module._load = realLoad;
    process.env.HOME = savedHome;
    delete process.env.S3_USER_FILES_BUCKET;
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it("does not re-upload restored files that were not modified", async () => {
    await workspaceSync.restoreWorkspace("telegram_1");
    assert.equal(fs.readFileSync(path.join(tmpDir, ".openclaw", "workspace", "SOUL.md"), "utf8"), "soul v1\n");
    assert.equal(puts.length, 0, "restore itself must not PUT");
    assert.equal(heads.length, 0, "a full restore downloads everything — nothing to compare");

    await workspaceSync.saveWorkspace("telegram_1");
    assert.equal(puts.length, 0, "a full save right after restore has nothing new to upload");

    // A real change is still uploaded, and only that file, with its sha256 as
    // object metadata so a later fill-missing restore can verify a kept copy.
    fs.writeFileSync(path.join(tmpDir, ".openclaw", "workspace", "SOUL.md"), "soul v2\n");
    await workspaceSync.saveWorkspace("telegram_1");
    assert.deepEqual(puts.map((p) => p.input.Key), ["telegram_1/.openclaw/workspace/SOUL.md"]);
    assert.equal(puts[0].input.Body.toString(), "soul v2\n");
    assert.deepEqual(puts[0].input.Metadata, { [workspaceSync.UPLOAD_HASH_METADATA_KEY]: sha256("soul v2\n") });
  });

  it("does not seed the cache for a file whose download failed", async () => {
    objects["telegram_1/.openclaw/workspace/BROKEN.md"] = "x";
    failGets.add("telegram_1/.openclaw/workspace/BROKEN.md");
    const r = await workspaceSync.restoreWorkspace("telegram_1");
    assert.equal(r.failed, 1);
    assert.equal(fs.existsSync(path.join(tmpDir, ".openclaw", "workspace", "BROKEN.md")), false);
    assert.equal(fs.existsSync(path.join(tmpDir, ".openclaw", "workspace", "SOUL.md")), true);

    // A partial restore closes the upload gate (tested in "upload gate" below);
    // reopen it here to check the hash cache on its own.
    assert.equal(workspaceSync.getRestoreState(), "failed");
    workspaceSync._setRestoreStateForTests("ready");

    // Something later creates that file locally with the same bytes: it must be uploaded.
    fs.writeFileSync(path.join(tmpDir, ".openclaw", "workspace", "BROKEN.md"), "x");
    await workspaceSync.saveWorkspace("telegram_1");
    assert.deepEqual(puts.map((p) => p.input.Key), ["telegram_1/.openclaw/workspace/BROKEN.md"]);
  });

  // --- fill-missing restore (session-storage mount already had SOME state) ---

  it("overwrite:false restores only files missing locally and keeps existing ones untouched", async () => {
    const soul = path.join(tmpDir, ".openclaw", "workspace", "SOUL.md");
    fs.mkdirSync(path.dirname(soul), { recursive: true });
    fs.writeFileSync(soul, "soul LOCAL (fresher, from the mount mirror)\n");
    objects["telegram_1/.openclaw/state/openclaw.sqlite"] = "SQLite format 3\0fake-db-bytes";

    const r = await workspaceSync.restoreWorkspace("telegram_1", { overwrite: false });
    assert.deepEqual(r, { restored: 2, kept: 1, keptUnchanged: 0, failed: 0, hadState: true });
    assert.equal(fs.readFileSync(soul, "utf8"), "soul LOCAL (fresher, from the mount mirror)\n", "existing file kept");
    assert.equal(fs.readFileSync(path.join(tmpDir, ".openclaw", "state", "openclaw.sqlite"), "latin1"), "SQLite format 3\0fake-db-bytes");
    assert.equal(fs.readFileSync(path.join(tmpDir, ".openclaw", "runtime-skills.json"), "utf8"), '{"version":1,"skills":{}}\n');
    assert.equal(puts.length, 0, "restore never PUTs");
    assert.equal(workspaceSync.getRestoreState(), "ready");

    // The kept file differs from S3 (its stored sha256 is that of "soul v1"),
    // so it was NOT seeded: the local (fresher) copy is uploaded by the first
    // save; the restored files are not.
    await workspaceSync.saveWorkspace("telegram_1");
    assert.deepEqual(puts.map((p) => p.input.Key), ["telegram_1/.openclaw/workspace/SOUL.md"]);
    assert.equal(puts[0].input.Body.toString(), "soul LOCAL (fresher, from the mount mirror)\n");
  });

  // --- kept files: seed the dedupe only with evidence that they match S3 -----
  //
  // Staging (PR #114 round 2, O1): a same-session restart found all 11 files on
  // the mount ("0 restored, 11 kept") and the first 30-min save re-uploaded 9
  // of them although they were identical to S3 — once per restart.

  it("does not re-upload kept files whose bytes match the sha256 S3 holds for them", async () => {
    // The mount mirror holds exact copies of both S3 objects.
    const soul = path.join(tmpDir, ".openclaw", "workspace", "SOUL.md");
    fs.mkdirSync(path.dirname(soul), { recursive: true });
    fs.writeFileSync(soul, "soul v1\n");
    fs.writeFileSync(path.join(tmpDir, ".openclaw", "runtime-skills.json"), '{"version":1,"skills":{}}\n');

    const r = await workspaceSync.restoreWorkspace("telegram_1", { overwrite: false });
    assert.deepEqual(r, { restored: 0, kept: 2, keptUnchanged: 2, failed: 0, hadState: true });
    assert.equal(workspaceSync.getRestoreState(), "ready");
    assert.deepEqual(
      heads.map((h) => h.input.Key).sort(),
      ["telegram_1/.openclaw/runtime-skills.json", "telegram_1/.openclaw/workspace/SOUL.md"],
      "one HeadObject per kept file, no GetObject",
    );
    assert.equal(puts.length, 0, "restore never PUTs");

    // The first full save has nothing to upload; neither does the change backup
    // when the watcher merely touches an identical file.
    await workspaceSync.saveWorkspace("telegram_1");
    assert.equal(puts.length, 0, "kept-and-identical files are not re-uploaded");
    workspaceSync.startChangeBackup("telegram_1", { debounceMs: 10, maxWaitMs: 100 });
    workspaceSync.markChanged("workspace/SOUL.md");
    await workspaceSync.flushPendingSaves("test", { force: true });
    await workspaceSync.stopChangeBackup("test");
    assert.equal(puts.length, 0);

    // A later real change to one of them is still uploaded, and only that one.
    fs.writeFileSync(soul, "soul v2\n");
    await workspaceSync.saveWorkspace("telegram_1");
    assert.deepEqual(puts.map((p) => p.input.Key), ["telegram_1/.openclaw/workspace/SOUL.md"]);
  });

  it("uploads kept files that differ from S3, and seeds only the identical ones", async () => {
    const soul = path.join(tmpDir, ".openclaw", "workspace", "SOUL.md");
    fs.mkdirSync(path.dirname(soul), { recursive: true });
    fs.writeFileSync(soul, "soul v1 plus a line the mount has and S3 does not\n"); // differs
    fs.writeFileSync(path.join(tmpDir, ".openclaw", "runtime-skills.json"), '{"version":1,"skills":{}}\n'); // identical

    const r = await workspaceSync.restoreWorkspace("telegram_1", { overwrite: false });
    assert.deepEqual(r, { restored: 0, kept: 2, keptUnchanged: 1, failed: 0, hadState: true });
    assert.equal(heads.length, 2);

    await workspaceSync.saveWorkspace("telegram_1");
    assert.deepEqual(puts.map((p) => p.input.Key), ["telegram_1/.openclaw/workspace/SOUL.md"]);
    assert.equal(puts[0].input.Body.toString(), "soul v1 plus a line the mount has and S3 does not\n");
    assert.equal(objects["telegram_1/.openclaw/workspace/SOUL.md"].toString(), puts[0].input.Body.toString());

    // Next same-session restart: S3 now holds the uploaded bytes and their
    // hash, so both kept files verify and nothing is re-uploaded.
    delete require.cache[require.resolve("./workspace-sync")];
    const fresh = require("./workspace-sync");
    fresh.configureCredentials({ accessKeyId: "AKIAIOSFODNN7EXAMPLE", secretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" });
    const r2 = await fresh.restoreWorkspace("telegram_1", { overwrite: false });
    assert.deepEqual(r2, { restored: 0, kept: 2, keptUnchanged: 2, failed: 0, hadState: true });
    await fresh.saveWorkspace("telegram_1");
    assert.equal(puts.length, 1, "no further uploads after the second restart");
  });

  it("uploads a kept file when S3 holds no hash for it (object from before the metadata was stored) — identical bytes are not assumed", async () => {
    const soul = path.join(tmpDir, ".openclaw", "workspace", "SOUL.md");
    fs.mkdirSync(path.dirname(soul), { recursive: true });
    fs.writeFileSync(soul, "soul v1\n"); // byte-identical to S3, but S3 cannot prove it
    delete metadata["telegram_1/.openclaw/workspace/SOUL.md"];

    const r = await workspaceSync.restoreWorkspace("telegram_1", { overwrite: false });
    assert.deepEqual(r, { restored: 1, kept: 1, keptUnchanged: 0, failed: 0, hadState: true });
    assert.equal(workspaceSync.getRestoreState(), "ready");

    await workspaceSync.saveWorkspace("telegram_1");
    assert.deepEqual(puts.map((p) => p.input.Key), ["telegram_1/.openclaw/workspace/SOUL.md"]);
    // ...and that upload wrote the hash, so the next restart can verify it.
    assert.deepEqual(metadata["telegram_1/.openclaw/workspace/SOUL.md"], { sha256: sha256("soul v1\n") });
  });

  it("a failed HeadObject leaves the kept file unseeded (uploaded later) without failing the restore", async () => {
    const soul = path.join(tmpDir, ".openclaw", "workspace", "SOUL.md");
    fs.mkdirSync(path.dirname(soul), { recursive: true });
    fs.writeFileSync(soul, "soul v1\n");
    failHeads.add("telegram_1/.openclaw/workspace/SOUL.md");

    const r = await workspaceSync.restoreWorkspace("telegram_1", { overwrite: false });
    assert.deepEqual(r, { restored: 1, kept: 1, keptUnchanged: 0, failed: 0, hadState: true });
    assert.equal(workspaceSync.getRestoreState(), "ready", "a HEAD failure is not a restore failure — S3 is intact");

    await workspaceSync.saveWorkspace("telegram_1");
    assert.deepEqual(puts.map((p) => p.input.Key), ["telegram_1/.openclaw/workspace/SOUL.md"]);
  });

  it(
    "verifies a kept SQLite database by its snapshot hash (what the save would upload), not the raw file",
    { skip: !hasNodeSqlite && "node:sqlite not available on this Node" },
    async () => {
      const { DatabaseSync } = require("node:sqlite");
      const dbRel = "state/openclaw.sqlite";
      const dbPath = path.join(tmpDir, ".openclaw", dbRel);
      fs.mkdirSync(path.dirname(dbPath), { recursive: true });
      const db = new DatabaseSync(dbPath);
      db.exec("PRAGMA journal_mode=WAL; CREATE TABLE t(v TEXT); INSERT INTO t VALUES ('a')");
      // S3 holds the snapshot this code uploaded earlier in the session.
      const snap = await workspaceSync.snapshotSqlite(dbPath);
      objects["telegram_1/.openclaw/" + dbRel] = snap;
      metadata["telegram_1/.openclaw/" + dbRel] = { sha256: sha256(snap) };

      let r = await workspaceSync.restoreWorkspace("telegram_1", { overwrite: false });
      assert.equal(r.kept, 1);
      assert.equal(r.keptUnchanged, 1, "live DB whose snapshot matches S3 is seeded");
      await workspaceSync.saveWorkspace("telegram_1");
      assert.deepEqual(puts.map((p) => p.input.Key), [], "no snapshot re-uploaded");

      // New commits since the snapshot: the kept DB now differs and must upload.
      db.exec("INSERT INTO t VALUES ('b')");
      delete require.cache[require.resolve("./workspace-sync")];
      const fresh = require("./workspace-sync");
      fresh.configureCredentials({ accessKeyId: "AKIAIOSFODNN7EXAMPLE", secretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" });
      r = await fresh.restoreWorkspace("telegram_1", { overwrite: false });
      assert.equal(r.kept, 3, "the two files restored earlier are now kept too");
      assert.equal(r.keptUnchanged, 2, "those two still match S3; the changed DB does not");
      await fresh.saveWorkspace("telegram_1");
      assert.equal(puts.filter((p) => p.input.Key.endsWith(".sqlite")).length, 1, "changed DB snapshot uploaded");
      db.close();
    },
  );

  it("overwrite:true (default) still replaces existing local files", async () => {
    const soul = path.join(tmpDir, ".openclaw", "workspace", "SOUL.md");
    fs.mkdirSync(path.dirname(soul), { recursive: true });
    fs.writeFileSync(soul, "stale local\n");
    const r = await workspaceSync.restoreWorkspace("telegram_1");
    assert.deepEqual(r, { restored: 2, kept: 0, keptUnchanged: 0, failed: 0, hadState: true });
    assert.equal(fs.readFileSync(soul, "utf8"), "soul v1\n");
  });

  // --- upload gate ---------------------------------------------------------------

  describe("upload gate", () => {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

    it("starts closed: no upload path PUTs before restoreWorkspace() has run", async () => {
      fs.writeFileSync(path.join(tmpDir, ".openclaw", "runtime-skills.json"), "local");
      assert.equal(workspaceSync.getRestoreState(), "unknown");
      await workspaceSync.saveWorkspace("telegram_1");
      assert.equal(await workspaceSync.saveFile("telegram_1", "runtime-skills.json"), false);
      workspaceSync.startChangeBackup("telegram_1", { debounceMs: 10, maxWaitMs: 100 });
      workspaceSync.markChanged("runtime-skills.json");
      const r = await workspaceSync.flushPendingSaves("test", { force: true });
      assert.equal(r.uploaded, 0);
      assert.equal(r.deferred, 1, "the change stays dirty for after the restore");
      await workspaceSync.stopChangeBackup("test");
      assert.equal(puts.length, 0);
    });

    it("stays closed while the restore is in flight, then opens and flushes the changes that queued up", async () => {
      // Hold the S3 listing until the test releases it.
      let release;
      const gate = new Promise((r) => { release = r; });
      const s3 = workspaceSync.getS3Client();
      const realSend = s3.send;
      s3.send = async (cmd) => {
        if (cmd.kind === "list") await gate;
        return realSend(cmd);
      };

      const restore = workspaceSync.restoreWorkspace("telegram_1");
      assert.equal(workspaceSync.getRestoreState(), "pending");
      workspaceSync.startChangeBackup("telegram_1", { debounceMs: 10, maxWaitMs: 100 });
      fs.writeFileSync(path.join(tmpDir, ".openclaw", "user-api-keys.json"), "{}");
      workspaceSync.markChanged("user-api-keys.json");
      await sleep(150);
      assert.equal(puts.length, 0, "nothing uploaded while the restore is pending");
      await workspaceSync.saveWorkspace("telegram_1");
      assert.equal(puts.length, 0);

      release();
      await restore;
      assert.equal(workspaceSync.getRestoreState(), "ready");
      await sleep(50); // the gate opening schedules the held flush
      assert.deepEqual(puts.map((p) => p.input.Key), ["telegram_1/.openclaw/user-api-keys.json"]);
      await workspaceSync.stopChangeBackup("test");
    });

    it("opens when S3 holds no state for the namespace (new user)", async () => {
      for (const k of Object.keys(objects)) delete objects[k];
      const r = await workspaceSync.restoreWorkspace("telegram_1");
      assert.deepEqual(r, { restored: 0, kept: 0, keptUnchanged: 0, failed: 0, hadState: false });
      assert.equal(workspaceSync.getRestoreState(), "ready");
      fs.writeFileSync(path.join(tmpDir, ".openclaw", "user-api-keys.json"), "{}");
      await workspaceSync.saveWorkspace("telegram_1");
      assert.deepEqual(puts.map((p) => p.input.Key), ["telegram_1/.openclaw/user-api-keys.json"]);
    });

    it("stays closed for the rest of the process when the listing fails — even the SIGTERM paths upload nothing", async () => {
      const s3 = workspaceSync.getS3Client();
      s3.send = async (cmd) => {
        if (cmd.kind === "list") throw new Error("AccessDenied (test)");
        puts.push(cmd);
        return {};
      };
      await assert.rejects(workspaceSync.restoreWorkspace("telegram_1"), /AccessDenied/);
      assert.equal(workspaceSync.getRestoreState(), "failed");

      fs.writeFileSync(path.join(tmpDir, ".openclaw", "user-api-keys.json"), "{}");
      workspaceSync.startChangeBackup("telegram_1", { debounceMs: 10, maxWaitMs: 100 });
      workspaceSync.markChanged("user-api-keys.json");
      await workspaceSync.flushPendingSaves("sigterm", { force: true });
      await workspaceSync.stopChangeBackup("sigterm");
      await workspaceSync.cleanup("telegram_1");
      assert.equal(await workspaceSync.saveFile("telegram_1", "user-api-keys.json"), false);
      assert.equal(puts.length, 0);
    });

    it("stays closed when one object could not be restored (a partially restored state dir must not reach S3)", async () => {
      failGets.add("telegram_1/.openclaw/runtime-skills.json");
      const r = await workspaceSync.restoreWorkspace("telegram_1", { overwrite: false });
      assert.equal(r.failed, 1);
      assert.equal(workspaceSync.getRestoreState(), "failed");
      // The gateway would now create these fresh; none of it may be uploaded.
      fs.mkdirSync(path.join(tmpDir, ".openclaw", "state"), { recursive: true });
      fs.writeFileSync(path.join(tmpDir, ".openclaw", "runtime-skills.json"), '{"version":1,"skills":{}}\n');
      await workspaceSync.saveWorkspace("telegram_1");
      assert.equal(puts.length, 0);
    });
  });
});

// --- change-driven backup (save changed state to S3 soon after it changes) ---
//
// AgentCore stops the container without a usable grace period (staging: gone
// under 5 s after SIGTERM; idle stops show no SIGTERM at all), so state must be
// in S3 before the stop. These tests drive the watcher's bookkeeping directly
// (markChanged) where timing must be deterministic, and the real fs.watch once.

describe("change-driven backup", () => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const Module = require("node:module");
  let workspaceSync;
  let tmpDir;
  let stateDir;
  let savedHome;
  let realLoad;
  let puts;
  let failKeys;
  let warnings;
  let realWarn;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const write = (rel, content) => {
    const p = path.join(stateDir, rel);
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, content);
  };
  const keys = () => puts.map((c) => c.input.Key);

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "ws-change-"));
    stateDir = path.join(tmpDir, ".openclaw");
    fs.mkdirSync(stateDir, { recursive: true });
    savedHome = process.env.HOME;
    process.env.HOME = tmpDir;
    delete require.cache[require.resolve("./workspace-sync")];
    process.env.AWS_REGION = "us-west-2";
    process.env.S3_USER_FILES_BUCKET = "test-bucket";
    workspaceSync = require("./workspace-sync");
    workspaceSync.configureCredentials({
      accessKeyId: "AKIAIOSFODNN7EXAMPLE",
      secretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    });
    puts = [];
    failKeys = new Set();
    workspaceSync._setRestoreStateForTests("ready"); // save paths under test, no restore here
    const fakeS3 = {
      send: async (cmd) => {
        if (failKeys.has(cmd.input.Key)) throw new Error("S3 unavailable (test)");
        puts.push(cmd);
        return {};
      },
    };
    realLoad = Module._load;
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
    warnings = [];
    realWarn = console.warn;
    console.warn = (...args) => { warnings.push(args.join(" ")); };
  });

  afterEach(async () => {
    await workspaceSync.stopChangeBackup("test-teardown");
    console.warn = realWarn;
    Module._load = realLoad;
    process.env.HOME = savedHome;
    delete process.env.S3_USER_FILES_BUCKET;
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  describe("changeTarget (which change events are worth an upload)", () => {
    it("keeps plain state and workspace files", () => {
      assert.equal(workspaceSync.changeTarget("workspace/notes.md"), "workspace/notes.md");
      assert.equal(workspaceSync.changeTarget("runtime-skills.json"), "runtime-skills.json");
      assert.equal(workspaceSync.changeTarget("agents/main/agent/openclaw-agent.sqlite"), "agents/main/agent/openclaw-agent.sqlite");
    });

    it("maps SQLite sidecar events onto the main database (commits land in the -wal first)", () => {
      assert.equal(workspaceSync.changeTarget("state/openclaw.sqlite-wal"), "state/openclaw.sqlite");
      assert.equal(workspaceSync.changeTarget("state/openclaw.sqlite-shm"), "state/openclaw.sqlite");
      assert.equal(workspaceSync.changeTarget("state/openclaw.sqlite-journal"), "state/openclaw.sqlite");
    });

    it("applies the existing skip rules", () => {
      for (const rel of [
        "node_modules/x/index.js",
        "workspace/.cache/a",
        "gateway.log",
        "openclaw.json",
        "AGENTS.md",
        "workspace/AGENTS.md",
        ".env",
        "certs/x.pem",
        "openclaw.json.bak",
        "tmp/openclaw-0/gateway.a504a3cd.lock.sqlite",
        "state/gateway.state.lock.sqlite",
      ]) {
        assert.equal(workspaceSync.changeTarget(rel), null, rel);
      }
    });

    it("ignores transient and staging paths", () => {
      for (const rel of [
        "tmp/openclaw-0/anything.json",
        "workspace/openclaw-bootstrap-abc123/AGENTS.md",
        "workspace/openclaw-publish-x/file.md",
        "workspace/notes.md.tmp-4242",
        "state/.openclaw.sqlite.99-1.sqlite-snapshot",
      ]) {
        assert.equal(workspaceSync.changeTarget(rel), null, rel);
      }
    });

    it("refuses paths that escape the state dir", () => {
      assert.equal(workspaceSync.changeTarget("../outside.json"), null);
      assert.equal(workspaceSync.changeTarget("/etc/passwd"), null);
      assert.equal(workspaceSync.changeTarget(""), null);
      assert.equal(workspaceSync.changeTarget("."), null);
    });
  });

  it("does nothing when the change backup is not running", async () => {
    workspaceSync.markChanged("workspace/notes.md");
    assert.equal(await workspaceSync.flushPendingSaves("test"), null);
    assert.equal(await workspaceSync.stopChangeBackup("test"), null);
    assert.equal(puts.length, 0);
  });

  it("is disabled without a namespace", () => {
    assert.deepEqual(workspaceSync.startChangeBackup(""), { mode: "none" });
  });

  it("uploads a burst of changes once, after the debounce, under the saveWorkspace key layout", async () => {
    workspaceSync.startChangeBackup("telegram_1", { debounceMs: 60, maxWaitMs: 2000 });
    write("workspace/notes.md", "hello");
    write("agents/main/memory.json", "{}");
    workspaceSync.markChanged("workspace/notes.md");
    workspaceSync.markChanged("workspace/notes.md"); // duplicate event, same file
    workspaceSync.markChanged("agents/main/memory.json");
    assert.equal(puts.length, 0, "nothing uploaded before the debounce elapses");
    await sleep(250);
    assert.deepEqual(keys().sort(), [
      "telegram_1/.openclaw/agents/main/memory.json",
      "telegram_1/.openclaw/workspace/notes.md",
    ]);
    assert.equal(puts.find((c) => c.input.Key.endsWith("notes.md")).input.Body.toString(), "hello");
  });

  it("does not re-upload a file whose content did not change, and does upload a real change", async () => {
    workspaceSync.startChangeBackup("telegram_1", { debounceMs: 30, maxWaitMs: 2000 });
    write("workspace/notes.md", "v1");
    workspaceSync.markChanged("workspace/notes.md");
    let r = await workspaceSync.flushPendingSaves("test");
    assert.equal(r.uploaded, 1);
    write("workspace/notes.md", "v1"); // rewritten, identical bytes (mtime moved)
    workspaceSync.markChanged("workspace/notes.md");
    r = await workspaceSync.flushPendingSaves("test");
    assert.equal(r.uploaded, 0);
    assert.equal(r.unchanged, 1);
    write("workspace/notes.md", "v2");
    workspaceSync.markChanged("workspace/notes.md");
    r = await workspaceSync.flushPendingSaves("test");
    assert.equal(r.uploaded, 1);
    assert.equal(puts.length, 2);
    assert.equal(puts[1].input.Body.toString(), "v2");
  });

  it("flushes by the max-wait deadline while changes keep streaming in", async () => {
    workspaceSync.startChangeBackup("telegram_1", { debounceMs: 200, maxWaitMs: 400 });
    write("workspace/notes.md", "x");
    const started = Date.now();
    // Touch every 50 ms for ~700 ms: the debounce alone would never fire.
    while (Date.now() - started < 700) {
      workspaceSync.markChanged("workspace/notes.md");
      await sleep(50);
      if (puts.length > 0) break;
    }
    assert.ok(puts.length >= 1, "deadline flush happened");
    assert.ok(Date.now() - started < 700, `flushed after ${Date.now() - started}ms`);
  });

  it("caps uploads per flush and drains the rest in a follow-up flush", async () => {
    workspaceSync.startChangeBackup("telegram_1", { debounceMs: 30, maxWaitMs: 2000, maxFilesPerFlush: 2 });
    for (const n of ["a", "b", "c"]) {
      write(`workspace/${n}.md`, n);
      workspaceSync.markChanged(`workspace/${n}.md`);
    }
    const r = await workspaceSync.flushPendingSaves("test");
    assert.equal(r.uploaded, 2);
    assert.equal(r.deferred, 1);
    assert.equal(puts.length, 2);
    await sleep(200); // the leftover is rescheduled automatically
    assert.equal(puts.length, 3);
    assert.deepEqual(keys().sort(), ["a", "b", "c"].map((n) => `telegram_1/.openclaw/workspace/${n}.md`));
  });

  it("drops files deleted before the flush and dirs reported by the watcher", async () => {
    workspaceSync.startChangeBackup("telegram_1", { debounceMs: 30, maxWaitMs: 2000 });
    fs.mkdirSync(path.join(stateDir, "workspace/sub"), { recursive: true });
    workspaceSync.markChanged("workspace/gone.md");
    workspaceSync.markChanged("workspace/sub");
    const r = await workspaceSync.flushPendingSaves("test");
    assert.equal(r.missing, 2);
    assert.equal(r.uploaded, 0);
    assert.equal(puts.length, 0);
  });

  it(
    "uploads a consistent snapshot of a live WAL database when its sidecar changes, and only when it has new commits",
    { skip: !hasNodeSqlite && "node:sqlite not available on this Node" },
    async () => {
      const { DatabaseSync } = require("node:sqlite");
      fs.mkdirSync(path.join(stateDir, "state"), { recursive: true });
      const dbPath = path.join(stateDir, "state/openclaw.sqlite");
      const writer = new DatabaseSync(dbPath);
      writer.exec("PRAGMA journal_mode = WAL");
      writer.exec("PRAGMA wal_autocheckpoint = 100000");
      writer.exec("CREATE TABLE sessions (id TEXT PRIMARY KEY, body TEXT)");
      writer.prepare("INSERT INTO sessions VALUES (?, ?)").run("s1", "one");

      // Throttle off: this test is about snapshot consistency and hash dedupe
      // (the SQLite-only throttle has its own tests below).
      workspaceSync.startChangeBackup("telegram_1", { debounceMs: 30, maxWaitMs: 2000, sqliteMinIntervalMs: 0 });
      workspaceSync.markChanged("state/openclaw.sqlite-wal");
      let r = await workspaceSync.flushPendingSaves("test");
      assert.equal(r.uploaded, 1);
      assert.equal(puts[0].input.Key, "telegram_1/.openclaw/state/openclaw.sqlite");
      assert.equal(puts[0].input.Body.subarray(0, 15).toString(), "SQLite format 3");
      // The snapshot holds the committed-but-uncheckpointed row (a raw copy would not).
      const snapPath = path.join(tmpDir, "restored.sqlite");
      fs.writeFileSync(snapPath, puts[0].input.Body);
      const restored = new DatabaseSync(snapPath, { readOnly: true });
      assert.equal(restored.prepare("SELECT COUNT(*) AS n FROM sessions").get().n, 1);
      restored.close();

      // Sidecar touched (e.g. a checkpoint) but no new commits: byte-identical snapshot, no PUT.
      writer.exec("PRAGMA wal_checkpoint(PASSIVE)");
      workspaceSync.markChanged("state/openclaw.sqlite-shm");
      r = await workspaceSync.flushPendingSaves("test");
      assert.equal(r.uploaded, 0);
      assert.equal(r.unchanged, 1);

      writer.prepare("INSERT INTO sessions VALUES (?, ?)").run("s2", "two");
      workspaceSync.markChanged("state/openclaw.sqlite-wal");
      r = await workspaceSync.flushPendingSaves("test");
      assert.equal(r.uploaded, 1);
      assert.equal(puts.length, 2);
      writer.close();
      // No -wal/-shm/-snapshot ever uploaded; no staging file left behind.
      assert.ok(keys().every((k) => k.endsWith("/openclaw.sqlite")));
      assert.deepEqual(fs.readdirSync(path.join(stateDir, "state")).filter((f) => f.includes("snapshot")), []);
    },
  );

  describe("SQLite-only throttle (idle gateway heartbeats must not upload a 4 MB snapshot every 30 s)", () => {
    let writer;
    let dbPath;
    const insert = (id) => writer.prepare("INSERT INTO sessions VALUES (?, ?)").run(id, id);
    const sqliteKeys = () => keys().filter((k) => k.endsWith(".sqlite"));

    beforeEach(() => {
      if (!hasNodeSqlite) return;
      const { DatabaseSync } = require("node:sqlite");
      fs.mkdirSync(path.join(stateDir, "state"), { recursive: true });
      dbPath = path.join(stateDir, "state/openclaw.sqlite");
      writer = new DatabaseSync(dbPath);
      writer.exec("PRAGMA journal_mode = WAL");
      writer.exec("CREATE TABLE sessions (id TEXT PRIMARY KEY, body TEXT)");
      insert("s0");
    });

    afterEach(() => {
      if (writer) { try { writer.close(); } catch { /* closed */ } writer = null; }
    });

    it("defaults to 5 minutes", () => {
      assert.equal(workspaceSync.DEFAULT_SQLITE_MIN_INTERVAL_MS, 5 * 60 * 1000);
    });

    it(
      "uploads the first SQLite change at once, then holds SQLite-only changes until the interval has elapsed",
      { skip: !hasNodeSqlite && "node:sqlite not available on this Node" },
      async () => {
        workspaceSync.startChangeBackup("telegram_1", { debounceMs: 20, maxWaitMs: 2000, sqliteMinIntervalMs: 400 });
        workspaceSync.markChanged("state/openclaw.sqlite-wal");
        let r = await workspaceSync.flushPendingSaves("test");
        assert.equal(r.uploaded, 1, "first change of the session is not held");
        assert.equal(r.held, 0);

        // Heartbeat-style commits: dirty again, but inside the interval → held, no snapshot, no PUT.
        insert("s1");
        workspaceSync.markChanged("state/openclaw.sqlite-wal");
        r = await workspaceSync.flushPendingSaves("test");
        assert.equal(r.uploaded, 0);
        assert.equal(r.held, 1);
        assert.ok(r.holdMs > 0 && r.holdMs <= 400, `holdMs=${r.holdMs}`);
        assert.equal(r.deferred, 0, "held snapshots are not 'deferred' leftovers");
        assert.equal(sqliteKeys().length, 1);
        assert.deepEqual(fs.readdirSync(path.join(stateDir, "state")).filter((f) => f.includes("snapshot")), [], "no snapshot taken while held");

        // Once the interval has elapsed, the held snapshot goes up on its own (no new event needed).
        await sleep(600);
        assert.equal(sqliteKeys().length, 2, "held snapshot uploaded by the rescheduled flush");
        // And it carries the commits made during the hold.
        const { DatabaseSync } = require("node:sqlite");
        const snapPath = path.join(tmpDir, "held.sqlite");
        fs.writeFileSync(snapPath, puts[puts.length - 1].input.Body);
        const restored = new DatabaseSync(snapPath, { readOnly: true });
        assert.equal(restored.prepare("SELECT COUNT(*) AS n FROM sessions").get().n, 2);
        restored.close();
      },
    );

    it(
      "a change to a non-SQLite file flushes the held SQLite snapshots with it",
      { skip: !hasNodeSqlite && "node:sqlite not available on this Node" },
      async () => {
        workspaceSync.startChangeBackup("telegram_1", { debounceMs: 20, maxWaitMs: 2000, sqliteMinIntervalMs: 60 * 60 * 1000 });
        workspaceSync.markChanged("state/openclaw.sqlite-wal");
        await workspaceSync.flushPendingSaves("test");
        insert("s1");
        workspaceSync.markChanged("state/openclaw.sqlite-wal");
        let r = await workspaceSync.flushPendingSaves("test");
        assert.equal(r.held, 1);

        write("workspace/memory.md", "the user likes tests");
        workspaceSync.markChanged("workspace/memory.md");
        r = await workspaceSync.flushPendingSaves("test");
        assert.equal(r.uploaded, 2, "memory file + the held snapshot");
        assert.equal(r.held, 0);
        assert.deepEqual(keys().slice(-2).sort(), [
          "telegram_1/.openclaw/state/openclaw.sqlite",
          "telegram_1/.openclaw/workspace/memory.md",
        ]);

        // A non-SQLite file that is dirty but byte-identical to its last upload
        // is not a change, so it does not release the hold.
        insert("s2");
        workspaceSync.markChanged("state/openclaw.sqlite-wal");
        workspaceSync.markChanged("workspace/memory.md");
        r = await workspaceSync.flushPendingSaves("test");
        assert.equal(r.unchanged, 1);
        assert.equal(r.held, 1);
      },
    );

    it(
      "the forced flush (SIGTERM), stop and the full save always include held snapshots",
      { skip: !hasNodeSqlite && "node:sqlite not available on this Node" },
      async () => {
        workspaceSync.startChangeBackup("telegram_1", { debounceMs: 20, maxWaitMs: 2000, sqliteMinIntervalMs: 60 * 60 * 1000 });
        workspaceSync.markChanged("state/openclaw.sqlite-wal");
        await workspaceSync.flushPendingSaves("test");
        assert.equal(sqliteKeys().length, 1);

        insert("s1");
        workspaceSync.markChanged("state/openclaw.sqlite-wal");
        assert.equal((await workspaceSync.flushPendingSaves("test")).held, 1);
        let r = await workspaceSync.flushPendingSaves("sigterm", { force: true });
        assert.equal(r.uploaded, 1);
        assert.equal(sqliteKeys().length, 2);

        insert("s2");
        workspaceSync.markChanged("state/openclaw.sqlite-wal");
        assert.equal((await workspaceSync.flushPendingSaves("test")).held, 1);
        await workspaceSync.saveWorkspace("telegram_1"); // the 30-min periodic save
        assert.equal(sqliteKeys().length, 3);

        workspaceSync.startChangeBackup("telegram_1", { debounceMs: 20, maxWaitMs: 2000, sqliteMinIntervalMs: 60 * 60 * 1000 });
        insert("s3");
        workspaceSync.markChanged("state/openclaw.sqlite-wal");
        assert.equal((await workspaceSync.flushPendingSaves("test")).held, 1);
        r = await workspaceSync.stopChangeBackup("sigterm");
        assert.equal(sqliteKeys().length, 4);
      },
    );
  });

  it("logs an upload failure, keeps going, and retries the file on the next flush", async () => {
    workspaceSync.startChangeBackup("telegram_1", { debounceMs: 30, maxWaitMs: 5000 });
    write("workspace/ok.md", "ok");
    write("workspace/bad.md", "bad");
    failKeys.add("telegram_1/.openclaw/workspace/bad.md");
    workspaceSync.markChanged("workspace/ok.md");
    workspaceSync.markChanged("workspace/bad.md");
    let r = await workspaceSync.flushPendingSaves("test");
    assert.equal(r.uploaded, 1);
    assert.equal(r.failed, 1);
    assert.ok(warnings.some((w) => w.includes("Change backup failed for workspace/bad.md") && w.includes("S3 unavailable")));
    assert.deepEqual(keys(), ["telegram_1/.openclaw/workspace/ok.md"]);

    failKeys.clear();
    r = await workspaceSync.flushPendingSaves("test"); // the failed file is still dirty
    assert.equal(r.uploaded, 1);
    assert.deepEqual(keys().sort(), [
      "telegram_1/.openclaw/workspace/bad.md",
      "telegram_1/.openclaw/workspace/ok.md",
    ]);
  });

  it("stop sweeps the whole state dir (hash-deduped), catching writes whose events never arrived", async () => {
    write("workspace/seen.md", "seen");
    write("workspace/unseen.md", "unseen");
    write("gateway.log", "never uploaded");
    workspaceSync.startChangeBackup("telegram_1", { debounceMs: 30, maxWaitMs: 2000 });
    workspaceSync.markChanged("workspace/seen.md");
    await workspaceSync.flushPendingSaves("test");
    assert.deepEqual(keys(), ["telegram_1/.openclaw/workspace/seen.md"]);

    const r = await workspaceSync.stopChangeBackup("sigterm");
    assert.equal(r.uploaded, 1, "only the file not uploaded yet");
    assert.equal(r.unchanged, 1);
    assert.deepEqual(keys().sort(), [
      "telegram_1/.openclaw/workspace/seen.md",
      "telegram_1/.openclaw/workspace/unseen.md",
    ]);
    // Stopped: later changes are ignored.
    workspaceSync.markChanged("workspace/seen.md");
    assert.equal(await workspaceSync.flushPendingSaves("test"), null);
  });

  it("watches the real state dir with fs.watch and uploads a changed file", async () => {
    const started = workspaceSync.startChangeBackup("telegram_1", { debounceMs: 100, maxWaitMs: 2000 });
    assert.ok(["watch", "poll"].includes(started.mode));
    write("workspace/live.md", "live");
    write("tmp/openclaw-0/gateway.state.lock.sqlite", "");
    await sleep(600);
    if (started.mode === "watch") {
      assert.deepEqual(keys(), ["telegram_1/.openclaw/workspace/live.md"]);
    }
  });

  it("saveWorkspace and saveFile share the content index (periodic save re-uploads only changes)", async () => {
    write("workspace/a.md", "a");
    write("workspace/b.md", "b");
    await workspaceSync.saveWorkspace("telegram_1");
    assert.equal(puts.length, 2);
    await workspaceSync.saveWorkspace("telegram_1");
    assert.equal(puts.length, 2, "second full save uploads nothing");
    assert.equal(await workspaceSync.saveFile("telegram_1", "workspace/a.md"), false);
    write("workspace/a.md", "a2");
    assert.equal(await workspaceSync.saveFile("telegram_1", "workspace/a.md"), true);
    await workspaceSync.saveWorkspace("telegram_1");
    assert.equal(puts.length, 3);
  });
});

describe("change backup wiring in agentcore-contract.js", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const source = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf-8");
  const idx = (needle, from = 0) => {
    const i = source.indexOf(needle, from);
    assert.ok(i >= 0, `contract source should contain: ${needle}`);
    return i;
  };

  it("starts the change backup once the gateway is ready, next to the periodic save", () => {
    const periodic = idx("workspaceSync.startPeriodicSave(namespace);");
    const change = idx("workspaceSync.startChangeBackup(namespace);");
    assert.ok(change > periodic && change - periodic < 200);
  });

  it("on SIGTERM flushes dirty files first, then stops the gateway, then stops the change backup before the mount snapshot and the full S3 save", () => {
    const sigterm = idx('process.on("SIGTERM", async () => {');
    const urgent = idx('const urgentSave = workspaceSync.flushPendingSaves("sigterm", { force: true });', sigterm);
    const stop = idx("await stopGateway(GATEWAY_STOP_WAIT_MS)", sigterm);
    const awaitUrgent = idx("await urgentSave;", sigterm);
    const stopChange = idx('await workspaceSync.stopChangeBackup("sigterm");', sigterm);
    const mirror = idx('await mirrorStateToSessionStorage("shutdown")', sigterm);
    const s3 = idx("await workspaceSync.cleanup(currentNamespace)", sigterm);
    assert.ok(urgent < stop, "dirty flush starts before waiting for the gateway");
    assert.ok(stop < awaitUrgent && awaitUrgent < stopChange, "gateway stopped, then pending changes uploaded");
    assert.ok(stopChange < mirror && mirror < s3);
  });

  it("stops the gateway on SIGTERM even without session storage", () => {
    const sigterm = idx('process.on("SIGTERM", async () => {');
    const stop = idx("const exited = await stopGateway(GATEWAY_STOP_WAIT_MS);", sigterm);
    const guard = source.lastIndexOf("if (sessionStorageActive) {", stop);
    // The nearest preceding session-storage guard must be closed before the stop call.
    const guardClose = source.indexOf("\n  }\n", guard);
    assert.ok(guard < 0 || guardClose < stop);
  });

  it("waits at most 2 s for the gateway by default (the container has no longer to live)", () => {
    assert.ok(source.includes('process.env.GATEWAY_STOP_WAIT_MS || "2000"'));
  });
});

// --- large SQLite databases: gzip multipart backup, streaming restore, cap (F1) ---
//
// us-west-2 rehearsal, big-state boot: `openclaw doctor --fix` imported the live
// user's 990 pre-2.0 sessions into one 299,360,256-byte openclaw-agent.sqlite,
// which the 10 MB cap then skipped on EVERY save ("Skipping … (299360256 bytes
// > 10485760)" at each flush). Everything the user said to 2.0 was lost at the
// next cold start. These tests shrink the thresholds (_setLimitsForTests) so
// the large-file paths run on a ~300 KB fixture.

describe("large SQLite databases (gzip snapshot backup)", () => {
  const Module = require("module");
  const os = require("os");
  const fs = require("fs");
  const path = require("path");
  const zlib = require("zlib");
  const hasNodeSqlite = (() => {
    try { return typeof require("node:sqlite").backup === "function"; } catch { return false; }
  })();
  const sha256 = (body) => require("node:crypto").createHash("sha256").update(body).digest("hex");
  const NS = "telegram_1";
  const REL = "agents/main/agent/openclaw-agent.sqlite";
  const KEY = `${NS}/.openclaw/${REL}`;
  // Fixture thresholds: "large" = snapshot > 64 KB, multipart parts of 32 KB.
  const LIMITS = { maxFileSize: 64 * 1024, uploadPartSize: 32 * 1024 };

  let tmpDir, savedHome, realLoad, workspaceSync;
  let s3; // fake S3 state
  let warnings, logs, realWarn, realLog;

  function freshModule() {
    delete require.cache[require.resolve("./workspace-sync")];
    const m = require("./workspace-sync");
    m.configureCredentials({ accessKeyId: "AKIAIOSFODNN7EXAMPLE", secretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" });
    m._setLimitsForTests({ ...LIMITS });
    return m;
  }

  function makeFakeS3() {
    const state = {
      objects: {}, metadata: {}, encodings: {}, uploads: {}, aborted: [], calls: [],
      failPart: null, // PartNumber whose UploadPart should throw
    };
    let nextUpload = 0;
    state.client = {
      send: async (cmd) => {
        state.calls.push(cmd.kind);
        const inp = cmd.input;
        switch (cmd.kind) {
          case "list":
            return {
              Contents: Object.entries(state.objects).map(([Key, body]) => ({ Key, Size: body.length })),
              IsTruncated: false,
            };
          case "get": {
            const body = state.objects[inp.Key];
            if (!body) throw new Error("NoSuchKey");
            const half = Math.ceil(body.length / 2);
            return {
              Body: (async function* () { yield body.subarray(0, half); yield body.subarray(half); })(),
              ...(state.metadata[inp.Key] ? { Metadata: state.metadata[inp.Key] } : {}),
              ...(state.encodings[inp.Key] ? { ContentEncoding: state.encodings[inp.Key] } : {}),
            };
          }
          case "head":
            if (!(inp.Key in state.objects)) throw new Error("NotFound");
            return { ContentLength: state.objects[inp.Key].length, ...(state.metadata[inp.Key] ? { Metadata: state.metadata[inp.Key] } : {}) };
          case "put":
            state.objects[inp.Key] = Buffer.from(inp.Body);
            state.metadata[inp.Key] = inp.Metadata;
            if (inp.ContentEncoding) state.encodings[inp.Key] = inp.ContentEncoding; else delete state.encodings[inp.Key];
            return {};
          case "createMultipart": {
            const id = `upload-${++nextUpload}`;
            state.uploads[id] = { input: inp, parts: new Map() };
            return { UploadId: id };
          }
          case "uploadPart": {
            if (state.failPart === inp.PartNumber) throw new Error("part boom");
            const up = state.uploads[inp.UploadId];
            assert.ok(up, "part for an unknown upload");
            assert.equal(inp.Body.length, inp.ContentLength);
            up.parts.set(inp.PartNumber, Buffer.from(inp.Body));
            return { ETag: `"etag-${inp.PartNumber}"` };
          }
          case "complete": {
            const up = state.uploads[inp.UploadId];
            assert.ok(up, "complete for an unknown upload");
            const parts = inp.MultipartUpload.Parts;
            assert.deepEqual(parts.map((p) => p.PartNumber), [...up.parts.keys()].sort((a, b) => a - b), "every uploaded part is listed, in order");
            for (const p of parts) assert.equal(p.ETag, `"etag-${p.PartNumber}"`);
            state.objects[inp.Key] = Buffer.concat(parts.map((p) => up.parts.get(p.PartNumber)));
            state.metadata[inp.Key] = up.input.Metadata;
            state.encodings[inp.Key] = up.input.ContentEncoding;
            delete state.uploads[inp.UploadId];
            return {};
          }
          case "abort":
            state.aborted.push(inp.UploadId);
            delete state.uploads[inp.UploadId];
            return {};
          default:
            throw new Error(`unexpected command ${cmd.kind}`);
        }
      },
    };
    return state;
  }

  /** A WAL-mode SQLite db of roughly `rows` compressible rows (~300 KB for 1500). */
  function makeDb(dbPath, rows = 1500, tag = "v1") {
    const { DatabaseSync } = require("node:sqlite");
    fs.mkdirSync(path.dirname(dbPath), { recursive: true });
    const db = new DatabaseSync(dbPath);
    db.exec("PRAGMA journal_mode = WAL");
    db.exec("CREATE TABLE IF NOT EXISTS transcript_events (id INTEGER PRIMARY KEY, session_id TEXT, event_json TEXT)");
    const ins = db.prepare("INSERT INTO transcript_events (session_id, event_json) VALUES (?, ?)");
    db.exec("BEGIN");
    for (let i = 0; i < rows; i++) {
      // A random token per row keeps the gzip body large enough to need
      // several parts at the test part size (real transcripts compress ~4-6x).
      ins.run(`ses_${i % 17}`, JSON.stringify({ type: "message", role: i % 2 ? "assistant" : "user", tag, seq: i, id: require("node:crypto").randomBytes(48).toString("hex"), text: `row ${i} ${"lorem ipsum dolor sit amet ".repeat(4)}` }));
    }
    db.exec("COMMIT");
    db.close();
  }

  function appendRows(dbPath, n, tag) {
    const { DatabaseSync } = require("node:sqlite");
    const db = new DatabaseSync(dbPath);
    const ins = db.prepare("INSERT INTO transcript_events (session_id, event_json) VALUES (?, ?)");
    for (let i = 0; i < n; i++) ins.run("ses_new", JSON.stringify({ tag, i, text: "something the user said to 2.0" }));
    db.close();
  }

  function rowCount(dbPath) {
    const { DatabaseSync } = require("node:sqlite");
    const db = new DatabaseSync(dbPath, { readOnly: true });
    try { return db.prepare("SELECT count(*) AS n FROM transcript_events").get().n; } finally { db.close(); }
  }

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "ws-large-"));
    savedHome = process.env.HOME;
    process.env.HOME = tmpDir;
    fs.mkdirSync(path.join(tmpDir, ".openclaw"), { recursive: true });
    process.env.AWS_REGION = "us-west-2";
    process.env.S3_USER_FILES_BUCKET = "test-bucket";
    s3 = makeFakeS3();
    realLoad = Module._load;
    Module._load = function (request, ...rest) {
      if (request === "@aws-sdk/client-s3") {
        const cmd = (kind) => function (input) { this.kind = kind; this.input = input; };
        return {
          S3Client: function () { return s3.client; },
          PutObjectCommand: cmd("put"),
          GetObjectCommand: cmd("get"),
          HeadObjectCommand: cmd("head"),
          ListObjectsV2Command: cmd("list"),
          CreateMultipartUploadCommand: cmd("createMultipart"),
          UploadPartCommand: cmd("uploadPart"),
          CompleteMultipartUploadCommand: cmd("complete"),
          AbortMultipartUploadCommand: cmd("abort"),
        };
      }
      return realLoad.call(this, request, ...rest);
    };
    warnings = []; logs = [];
    realWarn = console.warn; realLog = console.log;
    console.warn = (...a) => { warnings.push(a.join(" ")); };
    console.log = (...a) => { logs.push(a.join(" ")); };
    workspaceSync = freshModule();
    workspaceSync._setRestoreStateForTests("ready");
  });

  afterEach(async () => {
    try { await workspaceSync.stopChangeBackup("test"); } catch { /* not started */ }
    console.warn = realWarn; console.log = realLog;
    Module._load = realLoad;
    process.env.HOME = savedHome;
    delete process.env.S3_USER_FILES_BUCKET;
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it(
    "uploads a database above the buffered cap as a gzip multipart object keyed by its uncompressed sha256, never in memory whole",
    { skip: !hasNodeSqlite && "node:sqlite not available on this Node" },
    async () => {
      const dbPath = path.join(tmpDir, ".openclaw", REL);
      makeDb(dbPath);
      const raw = fs.statSync(dbPath).size;
      assert.ok(raw > LIMITS.maxFileSize, `fixture must exceed the buffered cap (${raw} bytes)`);

      await workspaceSync.saveWorkspace(NS);

      // Multipart, not a single PUT: create + >= 2 parts + complete, no abort.
      assert.deepEqual(s3.calls.filter((c) => c !== "uploadPart"), ["createMultipart", "complete"]);
      assert.ok(s3.calls.filter((c) => c === "uploadPart").length >= 2, `expected several parts, got ${s3.calls}`);
      assert.deepEqual(s3.aborted, []);
      assert.ok(KEY in s3.objects, "the database is in S3");

      // The body is gzip; decompressed it is the exact snapshot the old path
      // would have uploaded, and the metadata records THAT hash (so #115's
      // kept-file HeadObject comparison keeps working), the encoding and the
      // uncompressed size.
      const gz = s3.objects[KEY];
      assert.ok(gz.length < raw / 2, `gzip should shrink the fixture (${gz.length} vs ${raw})`);
      const plain = zlib.gunzipSync(gz);
      const snapshot = await workspaceSync.snapshotSqlite(dbPath);
      assert.equal(sha256(plain), sha256(snapshot), "decompressed body == consistent snapshot");
      assert.equal(s3.encodings[KEY], "gzip");
      assert.deepEqual(s3.metadata[KEY], {
        [workspaceSync.UPLOAD_HASH_METADATA_KEY]: sha256(snapshot),
        [workspaceSync.UPLOAD_ENCODING_METADATA_KEY]: "gzip",
        [workspaceSync.UPLOAD_SIZE_METADATA_KEY]: String(snapshot.length),
      });
      assert.ok(logs.some((l) => /Uploaded agents\/main\/agent\/openclaw-agent\.sqlite as a gzip snapshot: \d+ -> \d+ bytes \(\d+ part\(s\)/.test(l)), logs.join("\n"));
      assert.equal(warnings.filter((w) => /Skipping/.test(w)).length, 0, "nothing skipped");

      // No snapshot / temp file left next to the database.
      const leftovers = fs.readdirSync(path.dirname(dbPath)).filter((f) => f.includes("snapshot") || f.includes(".tmp-"));
      assert.deepEqual(leftovers, []);
    },
  );

  it(
    "a small database still goes up as one uncompressed PUT (unchanged path)",
    { skip: !hasNodeSqlite && "node:sqlite not available on this Node" },
    async () => {
      const dbPath = path.join(tmpDir, ".openclaw", REL);
      makeDb(dbPath, 20); // a few KB
      assert.ok(fs.statSync(dbPath).size <= LIMITS.maxFileSize);
      await workspaceSync.saveWorkspace(NS);
      assert.deepEqual(s3.calls, ["put"]);
      assert.equal(s3.encodings[KEY], undefined);
      assert.equal(Buffer.from(s3.objects[KEY]).subarray(0, 15).toString(), "SQLite format 3");
      assert.deepEqual(Object.keys(s3.metadata[KEY]), [workspaceSync.UPLOAD_HASH_METADATA_KEY]);
    },
  );

  it(
    "restores a gzip snapshot object decompressed and a legacy uncompressed object as-is, seeding both hashes",
    { skip: !hasNodeSqlite && "node:sqlite not available on this Node" },
    async () => {
      // Build the two objects: one uploaded by this code (gzip), one by the
      // pre-compression code (raw bytes, sha256 metadata from #115).
      const src = path.join(tmpDir, "src.sqlite");
      makeDb(src, 1500, "big");
      const bigSnapshot = await workspaceSync.snapshotSqlite(src);
      s3.objects[KEY] = zlib.gzipSync(bigSnapshot);
      s3.metadata[KEY] = { sha256: sha256(bigSnapshot), encoding: "gzip", "uncompressed-size": String(bigSnapshot.length) };
      s3.encodings[KEY] = "gzip";
      const legacyKey = `${NS}/.openclaw/state/openclaw.sqlite`;
      const legacySrc = path.join(tmpDir, "legacy.sqlite");
      makeDb(legacySrc, 20, "small");
      const legacyBytes = await workspaceSync.snapshotSqlite(legacySrc);
      s3.objects[legacyKey] = Buffer.from(legacyBytes);
      s3.metadata[legacyKey] = { sha256: sha256(legacyBytes) };
      // And one from before any metadata existed at all.
      s3.objects[`${NS}/.openclaw/workspace/MEMORY.md`] = Buffer.from("# memory\n");

      const fresh = freshModule();
      const r = await fresh.restoreWorkspace(NS);
      assert.deepEqual(r, { restored: 3, kept: 0, keptUnchanged: 0, failed: 0, hadState: true });
      assert.equal(fresh.getRestoreState(), "ready");

      const restoredBig = path.join(tmpDir, ".openclaw", REL);
      assert.equal(sha256(fs.readFileSync(restoredBig)), sha256(bigSnapshot), "gzip object restored decompressed, byte-exact");
      assert.equal(rowCount(restoredBig), 1500, "restored database opens and has every row");
      assert.equal(sha256(fs.readFileSync(path.join(tmpDir, ".openclaw", "state", "openclaw.sqlite"))), sha256(legacyBytes), "legacy object restored as-is");
      assert.equal(fs.readFileSync(path.join(tmpDir, ".openclaw", "workspace", "MEMORY.md"), "utf8"), "# memory\n");
      assert.deepEqual(fs.readdirSync(path.dirname(restoredBig)).filter((f) => f.includes(".tmp-")), [], "no temp file left");

      // Every restored file is seeded: the first full save uploads nothing.
      const before = s3.calls.length;
      await fresh.saveWorkspace(NS);
      assert.equal(s3.calls.length, before, `a full save right after restore must not upload (${s3.calls.slice(before)})`);
    },
  );

  it(
    "a downloaded object whose bytes do not match its recorded sha256 fails that file (and closes the upload gate)",
    { skip: !hasNodeSqlite && "node:sqlite not available on this Node" },
    async () => {
      const src = path.join(tmpDir, "src.sqlite");
      makeDb(src);
      const snapshot = await workspaceSync.snapshotSqlite(src);
      s3.objects[KEY] = zlib.gzipSync(snapshot);
      s3.metadata[KEY] = { sha256: "0".repeat(64), encoding: "gzip" };
      s3.encodings[KEY] = "gzip";
      const fresh = freshModule();
      const r = await fresh.restoreWorkspace(NS);
      assert.equal(r.failed, 1);
      assert.equal(fresh.getRestoreState(), "failed");
      assert.equal(fs.existsSync(path.join(tmpDir, ".openclaw", REL)), false, "nothing handed to the gateway");
      assert.ok(warnings.some((w) => /sha256 mismatch/.test(w)), warnings.join("\n"));
    },
  );

  it(
    "does not re-upload an unchanged large database (in-process hash, and HeadObject on a same-session restart), but uploads new commits",
    { skip: !hasNodeSqlite && "node:sqlite not available on this Node" },
    async () => {
      const dbPath = path.join(tmpDir, ".openclaw", REL);
      makeDb(dbPath);
      await workspaceSync.saveWorkspace(NS);
      assert.ok(s3.calls.includes("complete"));
      const afterFirst = s3.calls.length;

      // Same bytes: the second full save neither snapshots to S3 nor talks to it.
      await workspaceSync.saveWorkspace(NS);
      assert.equal(s3.calls.length, afterFirst, "unchanged database: no S3 traffic");

      // Same-session restart with the database on the mount mirror (kept file):
      // one HeadObject, hash from metadata == local snapshot hash, seeded.
      const restarted = freshModule();
      const r = await restarted.restoreWorkspace(NS, { overwrite: false });
      assert.deepEqual(r, { restored: 0, kept: 1, keptUnchanged: 1, failed: 0, hadState: true });
      assert.deepEqual(s3.calls.slice(afterFirst), ["list", "head"]);
      await restarted.saveWorkspace(NS);
      assert.equal(s3.calls.length, afterFirst + 2, "kept-and-identical: not re-uploaded");

      // New commits -> a new gzip object with the new hash.
      appendRows(dbPath, 50, "post-upgrade");
      await restarted.saveWorkspace(NS);
      assert.ok(s3.calls.slice(afterFirst + 2).includes("complete"), `expected a multipart upload, got ${s3.calls.slice(afterFirst + 2)}`);
      const plain = zlib.gunzipSync(s3.objects[KEY]);
      const check = path.join(tmpDir, "check.sqlite");
      fs.writeFileSync(check, plain);
      assert.equal(rowCount(check), 1550);
      assert.equal(s3.metadata[KEY].sha256, sha256(plain));
    },
  );

  it(
    "skips a database above the hard cap with a clear warning and no S3 traffic",
    { skip: !hasNodeSqlite && "node:sqlite not available on this Node" },
    async () => {
      const dbPath = path.join(tmpDir, ".openclaw", REL);
      makeDb(dbPath);
      workspaceSync._setLimitsForTests({ maxSqliteFileSize: 100 * 1024 }); // fixture is ~300 KB
      const outcome = await workspaceSync.uploadStateFile(NS, REL);
      assert.equal(outcome, "skipped");
      assert.deepEqual(s3.calls, []);
      const w = warnings.find((x) => /Skipping agents\/main\/agent\/openclaw-agent\.sqlite: SQLite database is \d+ bytes, above the 102400-byte backup cap \(WORKSPACE_SYNC_MAX_SQLITE_BYTES\) — its changes are NOT backed up to S3/.test(x));
      assert.ok(w, warnings.join("\n"));
      // The cap is also applied to the listing on restore.
      s3.objects[KEY] = Buffer.alloc(101 * 1024);
      const fresh = freshModule();
      fresh._setLimitsForTests({ maxSqliteFileSize: 100 * 1024 });
      const r = await fresh.restoreWorkspace(NS);
      assert.equal(r.restored, 0);
      assert.ok(warnings.some((x) => /Skipping oversized file/.test(x)));
    },
  );

  it(
    "aborts the multipart upload when a part fails, leaves no object, and the save reports the failure",
    { skip: !hasNodeSqlite && "node:sqlite not available on this Node" },
    async () => {
      const dbPath = path.join(tmpDir, ".openclaw", REL);
      makeDb(dbPath);
      s3.failPart = 2;
      await workspaceSync.saveWorkspace(NS);
      assert.deepEqual(s3.aborted, ["upload-1"]);
      assert.equal(KEY in s3.objects, false);
      assert.deepEqual(Object.keys(s3.uploads), [], "no incomplete upload left");
      assert.ok(warnings.some((w) => /Failed to save agents\/main\/agent\/openclaw-agent\.sqlite: part boom/.test(w)), warnings.join("\n"));
      // The next save retries (hash was not recorded).
      s3.failPart = null;
      await workspaceSync.saveWorkspace(NS);
      assert.ok(KEY in s3.objects);
    },
  );

  it(
    "change backup: a large database follows its own longer cadence, is held (not pulled along by other files) and force-flushed on stop",
    { skip: !hasNodeSqlite && "node:sqlite not available on this Node" },
    async () => {
      const dbPath = path.join(tmpDir, ".openclaw", REL);
      makeDb(dbPath);
      // Small-database throttle off; large databases at most every 60 s.
      workspaceSync.startChangeBackup(NS, { debounceMs: 10, maxWaitMs: 100, sqliteMinIntervalMs: 0, largeSqliteMinIntervalMs: 60_000 });

      // First change of the session: never uploaded before -> goes up now.
      workspaceSync.markChanged(REL);
      let r = await workspaceSync.flushPendingSaves("test");
      assert.equal(r.uploaded, 1);
      assert.equal(r.held, 0);
      const v1 = s3.objects[KEY];

      // New commits a moment later: held for the rest of the 60 s window, even
      // though a non-SQLite file is uploaded in the same pass.
      appendRows(dbPath, 10, "t2");
      fs.mkdirSync(path.join(tmpDir, ".openclaw", "workspace"), { recursive: true });
      fs.writeFileSync(path.join(tmpDir, ".openclaw", "workspace", "MEMORY.md"), "note\n");
      workspaceSync.markChanged(REL);
      workspaceSync.markChanged("workspace/MEMORY.md");
      r = await workspaceSync.flushPendingSaves("test");
      assert.equal(r.uploaded, 1, "MEMORY.md went up");
      assert.equal(r.held, 1, "the large database is held");
      assert.ok(r.holdMs > 50_000 && r.holdMs <= 60_000, `holdMs ${r.holdMs}`);
      assert.equal(s3.objects[KEY], v1, "database not re-uploaded yet");
      assert.ok(logs.some((l) => /1 SQLite snapshot\(s\) held for \d+s/.test(l)), logs.join("\n"));

      // Stop (SIGTERM path) forces it out.
      r = await workspaceSync.stopChangeBackup("test");
      assert.equal(r.held, 0);
      assert.notEqual(s3.objects[KEY], v1, "held snapshot uploaded on stop");
      assert.equal(rowCount((() => { const p = path.join(tmpDir, "chk.sqlite"); fs.writeFileSync(p, zlib.gunzipSync(s3.objects[KEY])); return p; })()), 1510);
    },
  );

  it("large cadence is never shorter than the SQLite throttle and defaults to 10 min", () => {
    assert.equal(workspaceSync.DEFAULT_LARGE_SQLITE_MIN_INTERVAL_MS, 10 * 60 * 1000);
    workspaceSync.startChangeBackup(NS, { debounceMs: 10, maxWaitMs: 100, sqliteMinIntervalMs: 90_000, largeSqliteMinIntervalMs: 1_000 });
    assert.ok(logs.some((l) => /databases over 65536 bytes at most every 90s, gzip'd/.test(l)), logs.join("\n"));
  });
});
