/**
 * Tests for state-storage.js — the OpenClaw 2.0 state layout on AgentCore
 * session storage: local state dir INCLUDING the workspace (the NFS mount has
 * neither SQLite locks nor hard links), mirror onto the mount (plain copy +
 * consistent SQLite snapshots + debounced workspace watcher) and restore.
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
  it("resolvePaths derives local state dir, mounted state dir and both workspace dirs", () => {
    const p = storage.resolvePaths({ homeDir: "/root", mountDir: "/mnt/workspace" });
    assert.equal(p.stateDir, "/root/.openclaw");
    assert.equal(p.mountStateDir, "/mnt/workspace/.openclaw");
    assert.equal(p.workspaceDir, "/root/.openclaw/workspace");
    assert.equal(p.mountWorkspaceDir, "/mnt/workspace/.openclaw/workspace");
  });

  it("isTransientDir matches caches and OpenClaw's bootstrap staging dirs", () => {
    assert.equal(storage.isTransientDir("node_modules"), true);
    assert.equal(storage.isTransientDir("openclaw-bootstrap-GGFCIv"), true);
    assert.equal(storage.isTransientDir("memory"), false);
    assert.equal(storage.isTransientDir("agents"), false);
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

  it("creates a real local state dir AND a real local workspace dir (no symlinks)", () => {
    const r = storage.setupSessionStorage({ homeDir: home, mountDir: mount, log: quietLog });
    assert.equal(r.available, true);
    const stateDir = path.join(home, ".openclaw");
    assert.equal(fs.lstatSync(stateDir).isDirectory(), true);
    assert.equal(fs.lstatSync(stateDir).isSymbolicLink(), false);
    const ws = path.join(stateDir, "workspace");
    assert.equal(fs.lstatSync(ws).isDirectory(), true);
    assert.equal(fs.lstatSync(ws).isSymbolicLink(), false);
    assert.equal(fs.statSync(path.join(mount, ".openclaw", "workspace")).isDirectory(), true);
    // OpenClaw 2.0 publishes bootstrap files with a hard link from a staging
    // file in the workspace dir — this must work on the local workspace.
    write(path.join(ws, "openclaw-bootstrap-abc", "AGENTS.md"), "seed");
    fs.linkSync(path.join(ws, "openclaw-bootstrap-abc", "AGENTS.md"), path.join(ws, "AGENTS.md"));
    assert.equal(fs.readFileSync(path.join(ws, "AGENTS.md"), "utf8"), "seed");
    // Workspace writes do NOT land on the mount until mirrored
    assert.equal(fs.existsSync(path.join(mount, ".openclaw", "workspace", "AGENTS.md")), false);
  });

  it("is idempotent", () => {
    storage.setupSessionStorage({ homeDir: home, mountDir: mount, log: quietLog });
    const r = storage.setupSessionStorage({ homeDir: home, mountDir: mount, log: quietLog });
    assert.equal(r.available, true);
    assert.equal(r.workspaceWas, "directory");
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

  it("replaces the intermediate layout's workspace symlink with a real dir and restores its files (local wins on conflict)", () => {
    const target = path.join(mount, ".openclaw", "workspace");
    write(path.join(target, "mount-only.md"), "mount");
    write(path.join(target, "both.md"), "mount");
    fs.mkdirSync(path.join(home, ".openclaw"), { recursive: true });
    fs.symlinkSync(target, path.join(home, ".openclaw", "workspace"));
    const r = storage.setupSessionStorage({ homeDir: home, mountDir: mount, log: quietLog });
    assert.match(r.workspaceWas, /^symlink/);
    const ws = path.join(home, ".openclaw", "workspace");
    assert.equal(fs.lstatSync(ws).isSymbolicLink(), false);
    assert.equal(fs.readFileSync(path.join(ws, "mount-only.md"), "utf8"), "mount");
    assert.equal(r.restored.workspaceFiles, 2);
    // A second boot with a locally modified file keeps the local copy
    write(path.join(ws, "both.md"), "local");
    const r2 = storage.setupSessionStorage({ homeDir: home, mountDir: mount, log: quietLog });
    assert.equal(fs.readFileSync(path.join(ws, "both.md"), "utf8"), "local");
    assert.equal(r2.restored.workspaceFiles, 0);
    assert.equal(r2.restored.skipped, 2);
  });

  it("restores a 1.x mount layout (legacy sessions.json + transcripts + workspace) to local disk", () => {
    const mountState = path.join(mount, ".openclaw");
    write(path.join(mountState, "agents/main/sessions/sessions.json"), '{"legacy":true}');
    write(path.join(mountState, "agents/main/sessions/abc.jsonl"), "{}\n");
    write(path.join(mountState, "workspace/AGENTS.md"), "ws");
    write(path.join(mountState, "agents/main/sessions/x.lock"), "");
    const r = storage.setupSessionStorage({ homeDir: home, mountDir: mount, log: quietLog });
    assert.equal(r.restored.files, 2);
    assert.equal(r.restored.workspaceFiles, 1);
    const local = path.join(home, ".openclaw");
    assert.equal(fs.readFileSync(path.join(local, "agents/main/sessions/sessions.json"), "utf8"), '{"legacy":true}');
    assert.equal(fs.existsSync(path.join(local, "agents/main/sessions/abc.jsonl")), true);
    assert.equal(fs.existsSync(path.join(local, "agents/main/sessions/x.lock")), false);
    // workspace is a local copy, not a link
    assert.equal(fs.lstatSync(path.join(local, "workspace")).isSymbolicLink(), false);
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

  it("copies plain files incl. the workspace, skips sidecars/locks/staging dirs, and skips unchanged files on rerun", async () => {
    write(path.join(stateDir, "openclaw.json"), "{}");
    write(path.join(stateDir, "agents/main/sessions/abc.jsonl"), "{}\n");
    write(path.join(stateDir, "agents/main/sessions/abc.lock"), "");
    write(path.join(stateDir, "state/openclaw.sqlite-wal"), "wal");
    write(path.join(stateDir, "workspace/memory.md"), "m");
    write(path.join(stateDir, "workspace/openclaw-bootstrap-x/AGENTS.md"), "staging");
    const r = await storage.mirrorStateDir({ stateDir, mountStateDir: mountState, log: quietLog, sqlite: null });
    assert.equal(r.files, 3);
    assert.equal(r.sqlite, 0);
    assert.equal(fs.existsSync(path.join(mountState, "workspace/openclaw-bootstrap-x")), false);
    const r2 = await storage.mirrorStateDir({ stateDir, mountStateDir: mountState, log: quietLog, sqlite: null });
    assert.equal(r2.files, 0);
    assert.equal(r2.unchanged, 3);
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
describe("mirrorWorkspaceDir / workspace watcher", () => {
  let home, mount, stateDir, mountState, ws;
  beforeEach(() => {
    home = mkTmp("ss-ws-home-");
    mount = mkTmp("ss-ws-mount-");
    storage.setupSessionStorage({ homeDir: home, mountDir: mount, log: quietLog });
    stateDir = path.join(home, ".openclaw");
    mountState = path.join(mount, ".openclaw");
    ws = path.join(stateDir, "workspace");
  });
  afterEach(() => {
    storage.stopWorkspaceWatcher();
    fs.rmSync(home, { recursive: true, force: true });
    fs.rmSync(mount, { recursive: true, force: true });
  });

  it("copies changed workspace files, prunes deleted ones, ignores the rest of the state dir", () => {
    write(path.join(ws, "AGENTS.md"), "a");
    write(path.join(ws, "memory/2026-09-24.md"), "m");
    write(path.join(stateDir, "state/openclaw.sqlite"), "not-mirrored-here");
    write(path.join(mountState, "workspace/stale.md"), "old");
    const r = storage.mirrorWorkspaceDir({ stateDir, mountStateDir: mountState, log: quietLog });
    assert.equal(r.files, 2);
    assert.equal(r.pruned, 1);
    assert.equal(fs.readFileSync(path.join(mountState, "workspace/AGENTS.md"), "utf8"), "a");
    assert.equal(fs.existsSync(path.join(mountState, "workspace/stale.md")), false);
    assert.equal(fs.existsSync(path.join(mountState, "state/openclaw.sqlite")), false);
    const r2 = storage.mirrorWorkspaceDir({ stateDir, mountStateDir: mountState, log: quietLog });
    assert.equal(r2.files, 0);
    assert.equal(r2.unchanged, 2);
    // A content change with a new mtime is picked up
    const f = path.join(ws, "AGENTS.md");
    fs.writeFileSync(f, "bb");
    const t = new Date(Date.now() + 5000);
    fs.utimesSync(f, t, t);
    const r3 = storage.mirrorWorkspaceDir({ stateDir, mountStateDir: mountState, log: quietLog });
    assert.equal(r3.files, 1);
    assert.equal(fs.readFileSync(path.join(mountState, "workspace/AGENTS.md"), "utf8"), "bb");
  });

  it("the restored workspace round-trips: mirror -> fresh container -> setup restores it", () => {
    write(path.join(ws, "AGENTS.md"), "a");
    write(path.join(ws, "memory/note.md"), "n");
    storage.mirrorWorkspaceDir({ stateDir, mountStateDir: mountState, log: quietLog });
    const home2 = mkTmp("ss-ws-home2-");
    try {
      const r = storage.setupSessionStorage({ homeDir: home2, mountDir: mount, log: quietLog });
      assert.equal(r.restored.workspaceFiles, 2);
      assert.equal(fs.readFileSync(path.join(home2, ".openclaw/workspace/memory/note.md"), "utf8"), "n");
    } finally {
      fs.rmSync(home2, { recursive: true, force: true });
    }
  });

  it("watcher mirrors a burst of changes once after the debounce, and stop() flushes pending work", async () => {
    const started = storage.startWorkspaceWatcher(
      { stateDir, mountStateDir: mountState, log: quietLog },
      { debounceMs: 150, maxWaitMs: 2000 },
    );
    assert.ok(["watch", "poll"].includes(started.mode));
    write(path.join(ws, "a.md"), "1");
    write(path.join(ws, "sub/b.md"), "2");
    await new Promise((r) => setTimeout(r, 900));
    if (started.mode === "watch") {
      assert.equal(fs.readFileSync(path.join(mountState, "workspace/a.md"), "utf8"), "1");
      assert.equal(fs.readFileSync(path.join(mountState, "workspace/sub/b.md"), "utf8"), "2");
    }
    write(path.join(ws, "c.md"), "3");
    await new Promise((r) => setTimeout(r, 20)); // event delivered, debounce still pending
    storage.stopWorkspaceWatcher();
    assert.equal(fs.readFileSync(path.join(mountState, "workspace/c.md"), "utf8"), "3");
  });

  it("flushWorkspaceMirror is a no-op after stop", () => {
    storage.stopWorkspaceWatcher();
    assert.equal(storage.flushWorkspaceMirror("test"), null);
  });
});

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
    const migrate = idx("if (await migrateLegacySessionStore(openclawEnv, { forceDoctor: resurrected.changed.length > 0 }))");
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

  it("starts the workspace watcher after the legacy import and before the gateway spawns", () => {
    const migrate = idx("if (await migrateLegacySessionStore(openclawEnv, { forceDoctor: resurrected.changed.length > 0 }))");
    const watcher = source.indexOf("stateStorage.startWorkspaceWatcher(", migrate);
    const spawnGw = source.indexOf('["gateway", "run", "--port", String(OPENCLAW_PORT), "--verbose"]', migrate);
    assert.ok(watcher > migrate && watcher < spawnGw);
  });

  it("stops the workspace watcher (flushing pending changes) on SIGTERM before the shutdown snapshot", () => {
    const sigterm = idx('process.on("SIGTERM", async () => {');
    const stopW = source.indexOf("stateStorage.stopWorkspaceWatcher()", sigterm);
    const mirror = source.indexOf('await mirrorStateToSessionStorage("shutdown")', sigterm);
    assert.ok(stopW > sigterm && stopW < mirror);
  });

  it("never symlinks the workspace onto the mount", () => {
    assert.equal(source.includes("symlinkSync"), false);
  });
});
