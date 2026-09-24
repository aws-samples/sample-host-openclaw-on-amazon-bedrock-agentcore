/**
 * Tests for state-storage.js — the OpenClaw 2.0 state layout on AgentCore
 * session storage: local SQLite state dir, live workspace symlink onto the
 * mount, cold mirror (plain copy + consistent SQLite snapshots) and restore.
 *
 * Run: cd bridge && node --test state-storage.test.js
 */
const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const os = require("os");
const path = require("path");

const storage = require("./state-storage");

let hasNodeSqlite = false;
try {
  require("node:sqlite");
  hasNodeSqlite = true;
} catch {
  hasNodeSqlite = false;
}
const sqliteOnly = { skip: !hasNodeSqlite && "node:sqlite not available on this Node" };

const quietLog = { log() {}, warn() {}, error() {} };

function mkTmp(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}
function write(file, content = "x") {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content);
}
function makeDb(file, rows) {
  const { DatabaseSync } = require("node:sqlite");
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const db = new DatabaseSync(file);
  db.exec("PRAGMA journal_mode = WAL");
  db.exec("CREATE TABLE IF NOT EXISTS t (v TEXT)");
  const ins = db.prepare("INSERT INTO t (v) VALUES (?)");
  for (const r of rows) ins.run(r);
  return db;
}
function readRows(file) {
  const { DatabaseSync } = require("node:sqlite");
  const db = new DatabaseSync(file, { readOnly: true });
  try {
    return db.prepare("SELECT v FROM t ORDER BY rowid").all().map((r) => r.v);
  } finally {
    db.close();
  }
}

describe("state-storage path helpers", () => {
  it("resolvePaths derives local state dir, mounted state dir and workspace link", () => {
    const p = storage.resolvePaths({ homeDir: "/root", mountDir: "/mnt/workspace" });
    assert.equal(p.stateDir, "/root/.openclaw");
    assert.equal(p.mountStateDir, "/mnt/workspace/.openclaw");
    assert.equal(p.workspaceLink, "/root/.openclaw/workspace");
    assert.equal(p.workspaceTarget, "/mnt/workspace/.openclaw/workspace");
  });

  it("gatewayStateEnv points OPENCLAW_STATE_DIR at the local state dir", () => {
    assert.deepEqual(storage.gatewayStateEnv("/root/.openclaw"), { OPENCLAW_STATE_DIR: "/root/.openclaw" });
  });

  it("isTransientFile matches SQLite sidecars, snapshot staging, locks and logs", () => {
    for (const f of [
      "agents/main/agent/openclaw-agent.sqlite-wal",
      "agents/main/agent/openclaw-agent.sqlite-shm",
      "state/openclaw.sqlite-journal",
      "state/.openclaw.sqlite.123-456.sqlite-snapshot",
      "state/openclaw.sqlite.tmp-42",
      "agents/main/sessions/x.lock",
      "logs/gateway.log",
    ]) {
      assert.equal(storage.isTransientFile(f), true, f);
    }
    for (const f of ["state/openclaw.sqlite", "agents/main/agent/openclaw-agent.sqlite", "openclaw.json", "agents/main/sessions/abc.jsonl"]) {
      assert.equal(storage.isTransientFile(f), false, f);
    }
  });

  it("isWorkspacePath matches only the workspace subtree", () => {
    assert.equal(storage.isWorkspacePath("workspace"), true);
    assert.equal(storage.isWorkspacePath("workspace/memory/2026.md"), true);
    assert.equal(storage.isWorkspacePath("workspaces/x"), false);
    assert.equal(storage.isWorkspacePath("agents/main/workspace.json"), false);
  });

  it("walkFiles lists regular files, skips caches and does not follow symlinks", () => {
    const root = mkTmp("ss-walk-");
    const other = mkTmp("ss-walk-other-");
    write(path.join(root, "a.txt"));
    write(path.join(root, "sub/b.txt"));
    write(path.join(root, "node_modules/pkg/index.js"));
    write(path.join(root, ".cache/c"));
    write(path.join(other, "linked.txt"));
    fs.symlinkSync(other, path.join(root, "link"));
    assert.deepEqual(storage.walkFiles(root).sort(), ["a.txt", "sub/b.txt"]);
  });
});

describe("setupSessionStorage", () => {
  let home, mount;
  beforeEach(() => {
    home = mkTmp("ss-home-");
    mount = mkTmp("ss-mount-");
  });
  afterEach(() => {
    fs.rmSync(home, { recursive: true, force: true });
    fs.rmSync(mount, { recursive: true, force: true });
  });

  it("reports unavailable and touches nothing when the mount is absent", () => {
    const r = storage.setupSessionStorage({ homeDir: home, mountDir: path.join(mount, "nope"), log: quietLog });
    assert.equal(r.available, false);
    assert.equal(fs.existsSync(path.join(home, ".openclaw")), false);
  });

  it("creates a real local state dir with workspace symlinked onto the mount", () => {
    const r = storage.setupSessionStorage({ homeDir: home, mountDir: mount, log: quietLog });
    assert.equal(r.available, true);
    const stateDir = path.join(home, ".openclaw");
    assert.equal(fs.lstatSync(stateDir).isDirectory(), true);
    assert.equal(fs.lstatSync(stateDir).isSymbolicLink(), false);
    const ws = path.join(stateDir, "workspace");
    assert.equal(fs.lstatSync(ws).isSymbolicLink(), true);
    assert.equal(fs.readlinkSync(ws), path.join(mount, ".openclaw", "workspace"));
    assert.equal(fs.statSync(path.join(mount, ".openclaw", "workspace")).isDirectory(), true);
    // Writes through the link land on the mount
    write(path.join(ws, "memory", "note.md"), "hi");
    assert.equal(fs.readFileSync(path.join(mount, ".openclaw", "workspace", "memory", "note.md"), "utf8"), "hi");
  });

  it("is idempotent", () => {
    storage.setupSessionStorage({ homeDir: home, mountDir: mount, log: quietLog });
    const r = storage.setupSessionStorage({ homeDir: home, mountDir: mount, log: quietLog });
    assert.equal(r.available, true);
    assert.equal(r.workspaceWas, "linked");
    assert.equal(r.stateDirWas, "directory");
  });

  it("replaces a previous-layout symlink (~/.openclaw -> mount) with a real local dir", () => {
    const mountState = path.join(mount, ".openclaw");
    write(path.join(mountState, "openclaw.json"), "{}");
    fs.symlinkSync(mountState, path.join(home, ".openclaw"));
    const r = storage.setupSessionStorage({ homeDir: home, mountDir: mount, log: quietLog });
    assert.match(r.stateDirWas, /^symlink/);
    assert.equal(fs.lstatSync(path.join(home, ".openclaw")).isSymbolicLink(), false);
    // The mount's plain files were restored into the local dir
    assert.equal(fs.readFileSync(path.join(home, ".openclaw", "openclaw.json"), "utf8"), "{}");
    // And the mount itself still has them (it is the mirror)
    assert.equal(fs.existsSync(path.join(mountState, "openclaw.json")), true);
  });

  it("merges a pre-existing local workspace dir onto the mount (mount wins on conflict)", () => {
    write(path.join(home, ".openclaw", "workspace", "local-only.md"), "local");
    write(path.join(home, ".openclaw", "workspace", "both.md"), "local");
    write(path.join(mount, ".openclaw", "workspace", "both.md"), "mount");
    const r = storage.setupSessionStorage({ homeDir: home, mountDir: mount, log: quietLog });
    assert.equal(r.workspaceWas, "directory");
    const target = path.join(mount, ".openclaw", "workspace");
    assert.equal(fs.readFileSync(path.join(target, "local-only.md"), "utf8"), "local");
    assert.equal(fs.readFileSync(path.join(target, "both.md"), "utf8"), "mount");
    assert.equal(fs.lstatSync(path.join(home, ".openclaw", "workspace")).isSymbolicLink(), true);
  });

  it("restores a 1.x mount layout (legacy sessions.json + transcripts) to local disk, leaving workspace live", () => {
    const mountState = path.join(mount, ".openclaw");
    write(path.join(mountState, "agents/main/sessions/sessions.json"), '{"legacy":true}');
    write(path.join(mountState, "agents/main/sessions/abc.jsonl"), "{}\n");
    write(path.join(mountState, "workspace/AGENTS.md"), "ws");
    write(path.join(mountState, "agents/main/sessions/x.lock"), "");
    const r = storage.setupSessionStorage({ homeDir: home, mountDir: mount, log: quietLog });
    assert.equal(r.restored.files, 2);
    const local = path.join(home, ".openclaw");
    assert.equal(fs.readFileSync(path.join(local, "agents/main/sessions/sessions.json"), "utf8"), '{"legacy":true}');
    assert.equal(fs.existsSync(path.join(local, "agents/main/sessions/abc.jsonl")), true);
    assert.equal(fs.existsSync(path.join(local, "agents/main/sessions/x.lock")), false);
    // workspace is the live link, not a copy
    assert.equal(fs.lstatSync(path.join(local, "workspace")).isSymbolicLink(), true);
    assert.equal(fs.readFileSync(path.join(local, "workspace/AGENTS.md"), "utf8"), "ws");
  });

  it("skips a 0-byte SQLite file left on the mount by a crashed gateway", () => {
    const mountState = path.join(mount, ".openclaw");
    write(path.join(mountState, "state/openclaw.sqlite"), "");
    write(path.join(mountState, "openclaw.json"), "{}");
    const r = storage.setupSessionStorage({ homeDir: home, mountDir: mount, log: quietLog });
    assert.equal(r.restored.files, 1);
    assert.equal(r.restored.skipped, 1);
    assert.equal(fs.existsSync(path.join(home, ".openclaw", "state", "openclaw.sqlite")), false);
  });
});

describe("sessionStorageHasContent", () => {
  it("is false for the empty directories setup creates, true once a file is persisted", () => {
    const mount = mkTmp("ss-content-");
    const mountState = path.join(mount, ".openclaw");
    fs.mkdirSync(path.join(mountState, "workspace"), { recursive: true });
    assert.equal(storage.sessionStorageHasContent(mountState), false);
    write(path.join(mountState, "workspace", "x.lock"), "");
    assert.equal(storage.sessionStorageHasContent(mountState), false, "transient files do not count");
    write(path.join(mountState, "workspace", "memory.md"), "m");
    assert.equal(storage.sessionStorageHasContent(mountState), true);
  });
});

describe("mirrorStateDir / restoreStateDir", () => {
  let home, mount, stateDir, mountState;
  beforeEach(() => {
    home = mkTmp("ss-mirror-home-");
    mount = mkTmp("ss-mirror-mount-");
    storage.setupSessionStorage({ homeDir: home, mountDir: mount, log: quietLog });
    stateDir = path.join(home, ".openclaw");
    mountState = path.join(mount, ".openclaw");
  });
  afterEach(() => {
    fs.rmSync(home, { recursive: true, force: true });
    fs.rmSync(mount, { recursive: true, force: true });
  });

  it("copies plain files, skips sidecars/locks and never touches the live workspace", async () => {
    write(path.join(stateDir, "openclaw.json"), "{}");
    write(path.join(stateDir, "agents/main/sessions/abc.jsonl"), "{}\n");
    write(path.join(stateDir, "agents/main/sessions/abc.lock"), "");
    write(path.join(stateDir, "state/openclaw.sqlite-wal"), "wal");
    write(path.join(stateDir, "workspace/memory.md"), "m"); // through the link
    const r = await storage.mirrorStateDir({ stateDir, mountStateDir: mountState, log: quietLog, sqlite: null });
    assert.equal(r.files, 2);
    assert.equal(r.sqlite, 0);
    assert.equal(fs.readFileSync(path.join(mountState, "openclaw.json"), "utf8"), "{}");
    assert.equal(fs.existsSync(path.join(mountState, "agents/main/sessions/abc.jsonl")), true);
    assert.equal(fs.existsSync(path.join(mountState, "agents/main/sessions/abc.lock")), false);
    assert.equal(fs.existsSync(path.join(mountState, "state/openclaw.sqlite-wal")), false);
    assert.equal(fs.readFileSync(path.join(mountState, "workspace/memory.md"), "utf8"), "m");
    assert.equal(r.pruned, 0);
  });

  it("prunes mirror files deleted locally (consumed legacy sessions.json does not come back)", async () => {
    write(path.join(mountState, "agents/main/sessions/sessions.json"), "{}");
    write(path.join(stateDir, "openclaw.json"), "{}");
    const r = await storage.mirrorStateDir({ stateDir, mountStateDir: mountState, log: quietLog, sqlite: null });
    assert.equal(r.pruned, 1);
    assert.equal(fs.existsSync(path.join(mountState, "agents/main/sessions/sessions.json")), false);
  });

  it("does not prune when the local state dir is empty (unreadable local walk guard)", async () => {
    write(path.join(mountState, "openclaw.json"), "{}");
    const r = await storage.mirrorStateDir({ stateDir: path.join(home, "does-not-exist"), mountStateDir: mountState, log: quietLog, sqlite: null });
    assert.equal(r.pruned, 0);
    assert.equal(fs.existsSync(path.join(mountState, "openclaw.json")), true);
  });

  it("logs and counts a SQLite file it cannot snapshot without node:sqlite", async () => {
    write(path.join(stateDir, "state/openclaw.sqlite"), "not really a db");
    const warnings = [];
    const r = await storage.mirrorStateDir({
      stateDir, mountStateDir: mountState, sqlite: null,
      log: { log() {}, warn: (m) => warnings.push(m) },
    });
    assert.equal(r.skipped, 1);
    assert.equal(r.sqlite, 0);
    assert.match(warnings.join("\n"), /backup API unavailable/);
    assert.equal(fs.existsSync(path.join(mountState, "state/openclaw.sqlite")), false);
  });

  it("restore is a no-op once the local state dir already holds SQLite state", () => {
    write(path.join(mountState, "openclaw.json"), "{}");
    write(path.join(stateDir, "state/openclaw.sqlite"), "x");
    const r = storage.restoreStateDir({ stateDir, mountStateDir: mountState, log: quietLog });
    assert.equal(r.files, 0);
    assert.match(r.reason, /already holds SQLite/);
    assert.equal(fs.existsSync(path.join(stateDir, "openclaw.json")), false);
  });

  it(
    "snapshots a live WAL-mode database consistently and restores it on the next boot",
    sqliteOnly,
    async () => {
      const dbPath = path.join(stateDir, "agents/main/agent/openclaw-agent.sqlite");
      const writer = makeDb(dbPath, ["one", "two"]);
      // Rows still in the -wal (no checkpoint) must be in the snapshot.
      assert.equal(fs.existsSync(`${dbPath}-wal`), true);

      const r = await storage.mirrorStateDir({ stateDir, mountStateDir: mountState, log: quietLog });
      assert.equal(r.sqlite, 1);
      const mirrored = path.join(mountState, "agents/main/agent/openclaw-agent.sqlite");
      assert.equal(fs.existsSync(mirrored), true);
      assert.equal(fs.existsSync(`${mirrored}-wal`), false, "no sidecars on the mount");
      assert.equal(fs.existsSync(`${mirrored}-shm`), false);
      assert.deepEqual(readRows(mirrored), ["one", "two"]);
      // The writer is untouched and keeps working.
      writer.prepare("INSERT INTO t (v) VALUES (?)").run("three");
      writer.close();

      // Next boot: fresh container -> empty local state dir; restore the mirror.
      const home2 = mkTmp("ss-mirror-home2-");
      try {
        const r2 = storage.setupSessionStorage({ homeDir: home2, mountDir: mount, log: quietLog });
        assert.equal(r2.restored.files, 1);
        const restored = path.join(home2, ".openclaw/agents/main/agent/openclaw-agent.sqlite");
        assert.deepEqual(readRows(restored), ["one", "two"]);
        // A second setup on the same container must not clobber the local DB.
        const r3 = storage.setupSessionStorage({ homeDir: home2, mountDir: mount, log: quietLog });
        assert.equal(r3.restored.files, 0);
      } finally {
        fs.rmSync(home2, { recursive: true, force: true });
      }
    },
  );

  it("snapshotSqliteToFile stages on local disk and writes the destination atomically", sqliteOnly, async () => {
    const dbPath = path.join(stateDir, "state/openclaw.sqlite");
    makeDb(dbPath, ["a"]).close();
    const staging = mkTmp("ss-staging-");
    const dest = path.join(mountState, "state/openclaw.sqlite");
    const size = await storage.snapshotSqliteToFile(dbPath, dest, { stagingDir: staging });
    assert.ok(size > 0);
    assert.deepEqual(readRows(dest), ["a"]);
    assert.deepEqual(fs.readdirSync(staging), [], "staging file removed");
    assert.deepEqual(
      fs.readdirSync(path.dirname(dest)).filter((f) => f.includes(".tmp-")),
      [],
      "no temp file left next to the destination",
    );
  });

  it("mirrorNow coalesces concurrent callers", async () => {
    write(path.join(stateDir, "openclaw.json"), "{}");
    const opts = { stateDir, mountStateDir: mountState, log: quietLog, sqlite: null };
    const a = storage.mirrorNow(opts);
    const b = storage.mirrorNow(opts);
    assert.equal(a, b);
    await a;
  });
});

// --- wiring in agentcore-contract.js (source-level ordering guarantees) ---
describe("state storage wiring in agentcore-contract.js", () => {
  const source = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf-8");
  const idx = (needle) => {
    const i = source.indexOf(needle);
    assert.ok(i >= 0, `contract source should contain: ${needle}`);
    return i;
  };

  it("no longer symlinks the whole state dir onto the mount", () => {
    assert.equal(source.includes("setupSessionStorageSymlink"), false);
    assert.equal(source.includes("fs.symlinkSync(mountedDir, OPENCLAW_DIR)"), false);
  });

  it("pins OPENCLAW_STATE_DIR to the local state dir in the gateway/doctor env", () => {
    assert.ok(source.includes("stateStorage.gatewayStateEnv(OPENCLAW_DIR)"));
  });

  it("restores the mirror before config write, legacy import and gateway spawn (in that order)", () => {
    const setup = idx("const sessionStorageAvailable = setupSessionStorage();");
    const config = idx("    writeOpenClawConfig();\n\n    // 1g. OpenClaw 2.0 moved session state");
    const migrate = idx("if (await migrateLegacySessionStore(openclawEnv))");
    // The restart path also spawns the gateway; take the spawn that follows the import.
    const spawnGw = source.indexOf('["gateway", "run", "--port", String(OPENCLAW_PORT), "--verbose"]', migrate);
    assert.ok(setup < config, "setup before config write");
    assert.ok(config < migrate, "config write before legacy import");
    assert.ok(spawnGw > migrate, "gateway spawn after legacy import");
  });

  it("on SIGTERM stops the gateway, then snapshots state to the mount, then saves to S3", () => {
    const sigterm = idx('process.on("SIGTERM", async () => {');
    const stop = source.indexOf("await stopGateway(GATEWAY_STOP_WAIT_MS)", sigterm);
    const mirror = source.indexOf('await mirrorStateToSessionStorage("shutdown")', sigterm);
    const s3 = source.indexOf("await workspaceSync.cleanup(currentNamespace)", sigterm);
    assert.ok(stop > sigterm && mirror > stop && s3 > mirror);
  });

  it("starts the periodic mirror once the gateway is ready", () => {
    const ready = idx("openclawReady = true;\n    workspaceSync.startPeriodicSave(namespace);");
    assert.ok(source.indexOf("stateStorage.startPeriodicMirror(", ready) > ready);
  });
});
