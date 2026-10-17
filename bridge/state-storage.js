/**
 * State storage layout for OpenClaw 2.0 on AgentCore session storage.
 *
 * Why this exists: AgentCore session storage (/mnt/workspace) is a loopback
 * NFSv4 export mounted with `local_lock=none`, and it refuses hard links
 * (link(2) fails with -524 / ENOTSUPP). OpenClaw 2.0 needs both in its state
 * dir: the gateway keeps sessions, auth and shared state in SQLite (which
 * cannot take even a RESERVED lock there, so it dies at startup with
 * "database is locked"), and it publishes workspace bootstrap files
 * (AGENTS.md, SOUL.md, IDENTITY.md, USER.md, BOOTSTRAP.md) and other
 * workspace artifacts atomically with `fs.linkSync` from a staging file in
 * the same directory (which fails every chat turn with "Unknown system error
 * -524"). The 1.x line only kept plain files there and wrote them in place.
 *
 * Layout
 *
 *   ~/.openclaw                        real directory on the container's local
 *                                      (overlay) disk = OPENCLAW_STATE_DIR.
 *                                      Everything the gateway touches lives
 *                                      here: every SQLite file (state/
 *                                      openclaw.sqlite, agents/<id>/agent/
 *                                      openclaw-agent.sqlite, ...) and the
 *                                      agent workspace ~/.openclaw/workspace
 *                                      (memory, bootstrap files, user files).
 *                                      Locks and hard links both work.
 *
 *   /mnt/workspace/.openclaw/<rest>    MIRROR of the local state dir on the
 *                                      persistent mount: plain files copied
 *                                      as-is (mtime preserved), each *.sqlite
 *                                      replaced by a consistent snapshot
 *                                      (SQLite online backup API), -wal/-shm/
 *                                      -journal never copied. Written by
 *                                      mirrorStateDir() every few minutes and
 *                                      at shutdown; the workspace/ subtree is
 *                                      additionally mirrored within seconds of
 *                                      any change by a debounced fs.watch
 *                                      (mirrorWorkspaceDir()). Restored to
 *                                      local disk by setupSessionStorage()
 *                                      BEFORE the gateway spawns.
 *
 * The mount therefore still looks like a complete OpenClaw state dir. That
 * keeps it backward compatible with whatever previous deployments left there
 * (a 1.x `agents/<id>/sessions/sessions.json` is restored to local disk and
 * picked up by the legacy import; a 1.x or 2.0 workspace/ is restored as the
 * agent workspace; a 2.0 snapshot is restored and opened).
 *
 * Only the mount/mirror mechanics live here so they can be unit-tested with
 * temp directories; agentcore-contract.js decides when to call them.
 */

const fs = require("fs");
const path = require("path");
const os = require("os");

const DEFAULT_MOUNT = "/mnt/workspace";
const STATE_DIRNAME = ".openclaw";
const WORKSPACE_SUBDIR = "workspace";
const SQLITE_EXT = ".sqlite";

// AgentCore session storage is capped at 1 GiB per session and statfs on the
// mount always reports it empty, so the mirror keeps its own count of the bytes
// it has on the mount and never writes past this budget. A write past the cap
// fails with ENOSPC, and on 16 Oct a session whose mirror reached it started
// failing with EREMOTEIO and then could not be started again at all. Leaves
// room for the workspace watcher's writes and for the temp copy of a file
// being replaced.
const DEFAULT_MIRROR_BUDGET_BYTES = 850 * 1024 * 1024;
// Errors that mean the mount itself refused the write (full, quota, broken).
const MOUNT_WRITE_ERRORS = new Set(["ENOSPC", "EDQUOT", "EREMOTEIO", "EIO", "EROFS"]);

function mirrorBudgetBytes() {
  const v = Number(process.env.STATE_MIRROR_BUDGET_BYTES);
  return Number.isFinite(v) && v > 0 ? v : DEFAULT_MIRROR_BUDGET_BYTES;
}

// Directories never mirrored or restored (regenerated, large, or caches).
const SKIP_DIR_NAMES = new Set(["node_modules", ".cache", ".npm"]);
// Short-lived staging directories OpenClaw creates next to the file it is
// about to publish (tempFile({ rootDir, prefix: "openclaw-bootstrap" })).
const TRANSIENT_DIR_RE = /^openclaw-(bootstrap|publish|tmp)-/;

// Lazy node:sqlite (Node >= 22.13 / 24 in the image; may be absent locally).
let _nodeSqlite;
function getNodeSqlite() {
  if (_nodeSqlite === undefined) {
    try {
      _nodeSqlite = require("node:sqlite");
    } catch {
      _nodeSqlite = null;
    }
  }
  return _nodeSqlite;
}

/**
 * Files that are only meaningful next to the exact live database bytes they
 * were written against (SQLite sidecars), in-flight snapshot staging files,
 * our own atomic-copy temp files, lock files and logs. Never mirrored, never
 * restored.
 */
function isTransientFile(relativePath) {
  const name = path.basename(relativePath);
  return (
    /\.sqlite-(wal|shm|journal|snapshot)$/.test(name) ||
    /\.tmp-\d+$/.test(name) ||
    name.endsWith(".lock") ||
    name.endsWith(".lock.sqlite") ||
    name.endsWith(".log")
  );
}

function isTransientDir(name) {
  return SKIP_DIR_NAMES.has(name) || TRANSIENT_DIR_RE.test(name);
}

function isWorkspacePath(relativePath) {
  return (
    relativePath === WORKSPACE_SUBDIR ||
    relativePath.startsWith(WORKSPACE_SUBDIR + "/")
  );
}

/**
 * Resolve the local and mounted paths for a given HOME and mount point.
 */
function resolvePaths({ homeDir = process.env.HOME || "/root", mountDir = DEFAULT_MOUNT } = {}) {
  const stateDir = path.join(homeDir, STATE_DIRNAME);
  const mountStateDir = path.join(mountDir, STATE_DIRNAME);
  return {
    mountDir,
    stateDir,
    mountStateDir,
    workspaceDir: path.join(stateDir, WORKSPACE_SUBDIR),
    mountWorkspaceDir: path.join(mountStateDir, WORKSPACE_SUBDIR),
  };
}

/**
 * List regular files under `root` as relative paths. Does NOT follow symlinks
 * and skips caches and OpenClaw's transient staging directories.
 */
function walkFiles(root, dir = root, { strict = false } = {}) {
  const out = [];
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch (err) {
    if (strict && !(err.code === "ENOENT" && dir === root)) throw err;
    return out;
  }
  for (const entry of entries) {
    if (entry.isDirectory()) {
      if (isTransientDir(entry.name)) continue;
      out.push(...walkFiles(root, path.join(dir, entry.name), { strict }));
    } else if (entry.isFile()) {
      out.push(path.relative(root, path.join(dir, entry.name)));
    }
  }
  return out;
}

/**
 * True when the mounted state dir holds any persisted content (a mirrored
 * state file or a workspace file) — i.e. this is a returning user and the S3
 * restore should fill only missing files. Empty directories (which setupSessionStorage
 * itself creates) do not count.
 */
function sessionStorageHasContent(mountStateDir) {
  return walkFiles(mountStateDir).some((rel) => !isTransientFile(rel));
}

function insideBase(resolvedBase, candidate) {
  const resolved = path.resolve(candidate);
  return resolved === resolvedBase || resolved.startsWith(resolvedBase + path.sep);
}

function assertSafePath(root, candidate) {
  const base = path.resolve(root);
  const resolved = path.resolve(candidate);
  if (!insideBase(base, resolved)) throw new Error("Path outside state directory");
  let current = base;
  const parts = path.relative(base, resolved).split(path.sep).filter(Boolean);
  for (let i = 0; i <= parts.length; i++) {
    try {
      if (fs.lstatSync(current).isSymbolicLink()) throw new Error(`State path contains symlink: ${current}`);
    } catch (err) {
      if (err.code !== "ENOENT") throw err;
    }
    if (i < parts.length) current = path.join(current, parts[i]);
  }
}

function assertSqliteFile(file) {
  if (fs.statSync(file).size === 0) throw new Error("Empty SQLite state");
  const sqlite = getNodeSqlite();
  if (!sqlite) throw new Error("node:sqlite unavailable for restore validation");
  const db = new sqlite.DatabaseSync(file, { readOnly: true });
  try {
    const rows = db.prepare("PRAGMA quick_check").all();
    if (rows.length !== 1 || rows[0].quick_check !== "ok") throw new Error("Invalid SQLite state");
  } finally {
    db.close();
  }
}

/**
 * Take a consistent point-in-time snapshot of a SQLite database (which the
 * gateway may still have open) and write it to `destPath` as a standalone db
 * file with no sidecars.
 *
 * Uses node:sqlite's online backup API from a read-only connection inside a
 * read transaction — the same primitive `openclaw backup create` uses. In WAL
 * mode the reader sees every committed transaction including frames still in
 * the -wal, so the copy is complete as of when it starts and never torn.
 *
 * The backup is staged on LOCAL disk (`stagingDir`, default os.tmpdir()) —
 * the backup API needs locks on its destination too, which the session
 * storage mount cannot provide — then copied to `destPath` via a temp file and
 * rename so a reader never sees a partial file.
 */
async function snapshotSqliteToFile(dbPath, destPath, { sqlite = getNodeSqlite(), stagingDir = os.tmpdir(), assertReady,
  beforePublish } = {}) {
  if (!sqlite || typeof sqlite.backup !== "function") {
    throw new Error("node:sqlite backup API unavailable on this Node runtime");
  }
  const stagePath = path.join(
    stagingDir,
    `.${path.basename(dbPath)}.${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}.sqlite-snapshot`,
  );
  const tmpDest = `${destPath}.tmp-${process.pid}`;
  try {
    const source = new sqlite.DatabaseSync(dbPath, { readOnly: true });
    try {
      source.exec("PRAGMA busy_timeout = 5000"); // bounded wait if the writer is mid-commit
      source.exec("BEGIN");
      try {
        await sqlite.backup(source, stagePath);
      } finally {
        try { source.exec("ROLLBACK"); } catch { /* read-only txn */ }
      }
    } finally {
      try { source.close(); } catch { /* already closed */ }
    }
    if (assertReady) assertReady();
    // The caller may refuse (e.g. no room on the mount); nothing is written then.
    if (beforePublish && !beforePublish(fs.statSync(stagePath).size)) return null;
    fs.mkdirSync(path.dirname(destPath), { recursive: true });
    fs.copyFileSync(stagePath, tmpDest);
    fs.renameSync(tmpDest, destPath);
    return fs.statSync(destPath).size;
  } finally {
    try { fs.unlinkSync(stagePath); } catch { /* best effort */ }
    try { fs.unlinkSync(`${stagePath}-journal`); } catch { /* none */ }
    try { fs.unlinkSync(tmpDest); } catch { /* not created */ }
  }
}

/**
 * Copy a plain file atomically (temp file in the destination dir + rename),
 * preserving the source mtime so unchanged files can be detected later.
 */
function copyFileAtomic(src, dest, { validate } = {}) {
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  const tmp = `${dest}.tmp-${process.pid}`;
  try {
    fs.copyFileSync(src, tmp);
    if (validate) validate(tmp);
    try {
      const st = fs.statSync(src);
      fs.utimesSync(tmp, st.atime, st.mtime);
    } catch { /* best effort */ }
    fs.renameSync(tmp, dest);
  } finally {
    try { fs.unlinkSync(tmp); } catch { /* not created */ }
    // Read-only SQLite validation may create sidecars on this staged copy.
    try { fs.unlinkSync(`${tmp}-wal`); } catch { /* none */ }
    try { fs.unlinkSync(`${tmp}-shm`); } catch { /* none */ }
    try { fs.unlinkSync(`${tmp}-journal`); } catch { /* none */ }
  }
}

/**
 * True when `dest` already holds a copy of `src` (same size and mtime, to the
 * second — NFS mtime granularity). Used to make the workspace mirror cheap.
 */
function fileSize(file) {
  try {
    return fs.statSync(file).size;
  } catch {
    return 0;
  }
}

/**
 * Bytes held under `dir` on the mount: every regular file, including caches
 * and temp files the mirror would skip (they count against the quota too).
 */
function diskUsageBytes(dir) {
  let total = 0;
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return 0;
  }
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) total += diskUsageBytes(full);
    else if (entry.isFile()) total += fileSize(full);
  }
  return total;
}

/**
 * Size + mtime of what a mirror copy of `src` was made from. For SQLite the
 * -wal is included: a commit that is still only in the WAL changes the
 * snapshot without touching the main file.
 */
function sourceSignature(src, withWal) {
  try {
    const st = fs.statSync(src);
    let sig = `${st.size}:${st.mtimeMs}`;
    if (withWal) {
      try {
        const wal = fs.statSync(`${src}-wal`);
        sig += `|${wal.size}:${wal.mtimeMs}`;
      } catch { /* no WAL */ }
    }
    return sig;
  } catch {
    return null;
  }
}

// dest path -> source signature of the copy the mirror last wrote there, for
// this process. The mount does not keep the mtime the mirror sets (NFS
// rewrites it on rename), so size+mtime of the mount copy can never match and
// every file -- including a several-hundred-MB SQLite snapshot -- used to be
// rewritten every 5 minutes.
const _mirrored = new Map();

function unchangedSinceMirror(src, dest, withWal) {
  const sig = sourceSignature(src, withWal);
  return sig !== null && _mirrored.get(dest) === sig && fs.existsSync(dest);
}

function isMountWriteError(err) {
  return Boolean(err && MOUNT_WRITE_ERRORS.has(err.code));
}

/**
 * Make room for writing `needed` bytes to `dest` (a temp copy next to the old
 * one, then rename). If the budget does not allow it, the old mirror copy is
 * removed first: a file missing from the mount is restored from the S3
 * backup on the next start, while a mount past its quota breaks. Returns
 * false (and leaves the file off the mount) if it still does not fit.
 */
function makeRoom(acct, dest, needed, rel, log) {
  if (acct.used + needed <= acct.budget) return true;
  const old = fileSize(dest);
  if (old > 0) {
    try {
      fs.unlinkSync(dest);
      acct.used -= old;
      _mirrored.delete(dest);
    } catch { /* counted as still there */ }
  }
  if (acct.used + needed <= acct.budget) {
    log.warn(`[state-storage] MOUNT_BUDGET replaced ${rel} without a second copy: ${needed} B more would pass ` +
      `${acct.budget} B (${acct.used} B in use)`);
    return true;
  }
  log.warn(`[state-storage] MOUNT_BUDGET skipped ${rel} (${needed} B): ${acct.used} B of ${acct.budget} B in use; ` +
    "the S3 backup keeps this file");
  acct.skippedForBudget++;
  return false;
}

function sameSizeAndMtime(src, dest) {
  try {
    const a = fs.statSync(src);
    const b = fs.statSync(dest);
    return a.size === b.size && Math.floor(a.mtimeMs / 1000) === Math.floor(b.mtimeMs / 1000);
  } catch {
    return false;
  }
}

/**
 * Restore the mirrored state dir from the mount to local disk.
 *
 * Fills missing files and validates SQLite without clobbering local state.
 * Workspace files are restored without overwriting anything already present locally (never destroys
 * local work); transient files and 0-byte *.sqlite files (a database that was
 * created but never written — e.g. left behind by a gateway that crashed on
 * the mount — which would otherwise shadow a real snapshot) are skipped.
 *
 * @returns {{ files: number, workspaceFiles: number, skipped: number }}
 */
function restoreStateDir({ stateDir, mountStateDir, log = console } = {}) {
  const resolvedBase = path.resolve(stateDir);
  let files = 0;
  let workspaceFiles = 0;
  let skipped = 0;
  for (const rel of walkFiles(mountStateDir, mountStateDir, { strict: true })) {
    if (isTransientFile(rel)) continue;
    const src = path.join(mountStateDir, rel);
    const dest = path.join(stateDir, rel);
    if (!insideBase(resolvedBase, dest)) {
      log.warn(`[state-storage] Path traversal blocked on restore: ${rel}`);
      skipped++;
      continue;
    }
    assertSafePath(stateDir, dest);
    assertSafePath(mountStateDir, src);
    if (fs.existsSync(dest) && !(rel.endsWith(SQLITE_EXT) && fs.statSync(dest).size === 0)) {
      if (rel.endsWith(SQLITE_EXT)) assertSqliteFile(dest);
      skipped++;
      continue;
    }
    if (rel.endsWith(SQLITE_EXT) && fs.statSync(src).size === 0) {
      log.warn(`[state-storage] Skipping 0-byte SQLite file on restore: ${rel}`);
      skipped++;
      continue;
    }
    // Validation opens only the staged local copy; SQLite never opens the mount.
    const validate = rel.endsWith(SQLITE_EXT) ? assertSqliteFile : undefined;
    if (isWorkspacePath(rel)) {
      copyFileAtomic(src, dest, { validate });
      workspaceFiles++;
      continue;
    }
    copyFileAtomic(src, dest, { validate });
    files++;
  }
  return { files, workspaceFiles, skipped };
}

/**
 * Mirror the local state dir to the mount: plain files copied (unchanged
 * files, by size+mtime, are skipped), each *.sqlite written as a consistent
 * snapshot, transient files skipped, and mirror files that no longer exist
 * locally removed (so e.g. a legacy sessions.json that the import consumed
 * does not come back on the next boot and re-trigger it). Includes the
 * workspace/ subtree.
 *
 * Safe to call while the gateway is running (snapshots are consistent), but
 * the caller should stop the gateway first on shutdown so the copy is fully
 * quiesced (all WAL frames checkpointed).
 *
 * @returns {Promise<{ files: number, unchanged: number, sqlite: number, skipped: number, pruned: number }>}
 */
async function mirrorStateDir({ stateDir, mountStateDir, log = console, sqlite = getNodeSqlite(), stagingDir, assertReady,
  budgetBytes = mirrorBudgetBytes() } = {}) {
  if (assertReady) assertReady();
  const result = { files: 0, unchanged: 0, sqlite: 0, skipped: 0, pruned: 0, budgetSkipped: 0, mountErrors: 0 };
  const localFiles = walkFiles(stateDir, stateDir, { strict: true }).filter((rel) => !isTransientFile(rel));
  const localSet = new Set(localFiles);
  const acct = { used: diskUsageBytes(path.dirname(mountStateDir)), budget: budgetBytes, skippedForBudget: 0 };
  const staging = stagingDir || fs.mkdtempSync(path.join(os.tmpdir(), "openclaw-state-mirror-"));
  try {
    for (const rel of localFiles) {
      if (assertReady) assertReady();
      const src = path.join(stateDir, rel);
      const dest = path.join(mountStateDir, rel);
      const isDb = rel.endsWith(SQLITE_EXT);
      try {
        if (unchangedSinceMirror(src, dest, isDb)) {
          result.unchanged++;
          continue;
        }
        const sig = sourceSignature(src, isDb);
        if (isDb) {
          let staged = 0;
          let replaced = 0;
          const size = await snapshotSqliteToFile(src, dest, {
            sqlite, stagingDir: staging, assertReady,
            beforePublish: (n) => {
              if (!makeRoom(acct, dest, n, rel, log)) return false;
              staged = n;
              replaced = fileSize(dest);
              return true;
            },
          });
          if (size === null) continue;
          acct.used += staged - replaced;
          result.sqlite++;
        } else if (sameSizeAndMtime(src, dest)) {
          result.unchanged++;
        } else {
          const n = fileSize(src);
          if (!makeRoom(acct, dest, n, rel, log)) continue;
          const left = fileSize(dest);
          copyFileAtomic(src, dest);
          acct.used += n - left;
          result.files++;
        }
        if (sig !== null) _mirrored.set(dest, sig);
      } catch (err) {
        _mirrored.delete(dest);
        result.skipped++;
        if (isMountWriteError(err)) {
          result.mountErrors++;
          log.warn(`[state-storage] MOUNT_WRITE_FAILED code=${err.code} while mirroring ${rel}; ` +
            "stopping this mirror pass (the S3 backup is unaffected)");
          break;
        }
        log.warn(`[state-storage] Failed to mirror ${rel}: ${err.message}`);
      }
    }
    result.budgetSkipped = acct.skippedForBudget;
    // Prune mirror files that were deleted locally. Only when the local walk
    // produced something — an empty local set means we could not read it.
    if (assertReady) assertReady();
    if (localFiles.length > 0 && result.skipped === 0) {
      for (const rel of walkFiles(mountStateDir, mountStateDir, { strict: true })) {
        if (localSet.has(rel)) continue;
        try {
          fs.unlinkSync(path.join(mountStateDir, rel));
          result.pruned++;
        } catch { /* best effort */ }
      }
    }
  } finally {
    if (!stagingDir) {
      try { fs.rmSync(staging, { recursive: true, force: true }); } catch { /* best effort */ }
    }
  }
  return result;
}

/**
 * Mirror only the workspace/ subtree (plain files, no SQLite) — the cheap
 * path the change watcher runs: copies files whose size or mtime differ from
 * the mirror and prunes workspace files deleted locally.
 *
 * @returns {{ files: number, unchanged: number, skipped: number, pruned: number }}
 */
function mirrorWorkspaceDir({ stateDir, mountStateDir, log = console, assertReady, budgetBytes = mirrorBudgetBytes() } = {}) {
  if (assertReady) assertReady();
  const result = { files: 0, unchanged: 0, skipped: 0, pruned: 0 };
  const workspaceDir = path.join(stateDir, WORKSPACE_SUBDIR);
  const mountWorkspaceDir = path.join(mountStateDir, WORKSPACE_SUBDIR);
  if (!fs.existsSync(workspaceDir)) return result;
  const localFiles = walkFiles(workspaceDir, workspaceDir, { strict: true }).filter((rel) => !isTransientFile(rel));
  const localSet = new Set(localFiles);
  let acct = null;
  for (const rel of localFiles) {
    const src = path.join(workspaceDir, rel);
    const dest = path.join(mountWorkspaceDir, rel);
    if (rel.endsWith(SQLITE_EXT)) {
      result.skipped++;
      continue; // The full state mirror snapshots databases, including workspace ones.
    }
    try {
      if (unchangedSinceMirror(src, dest, false) || sameSizeAndMtime(src, dest)) {
        result.unchanged++;
      } else {
        // Count the mount only when something has to be written.
        acct ||= { used: diskUsageBytes(path.dirname(mountStateDir)), budget: budgetBytes, skippedForBudget: 0 };
        const sig = sourceSignature(src, false);
        const n = fileSize(src);
        if (!makeRoom(acct, dest, n, `workspace/${rel}`, log)) {
          result.skipped++;
          continue;
        }
        const left = fileSize(dest);
        copyFileAtomic(src, dest);
        acct.used += n - left;
        if (sig !== null) _mirrored.set(dest, sig);
        result.files++;
      }
    } catch (err) {
      _mirrored.delete(dest);
      result.skipped++;
      if (isMountWriteError(err)) {
        log.warn(`[state-storage] MOUNT_WRITE_FAILED code=${err.code} while mirroring workspace/${rel}; ` +
          "stopping this mirror pass (the S3 backup is unaffected)");
        break;
      }
      log.warn(`[state-storage] Failed to mirror workspace/${rel}: ${err.message}`);
    }
  }
  if (localFiles.length === 0 || result.skipped > 0) return result;
  for (const rel of walkFiles(mountWorkspaceDir, mountWorkspaceDir, { strict: true })) {
    if (localSet.has(rel)) continue;
    try {
      fs.unlinkSync(path.join(mountWorkspaceDir, rel));
      result.pruned++;
    } catch { /* best effort */ }
  }
  return result;
}

/**
 * Prepare the state storage layout for a container boot. Idempotent.
 *
 *  1. `stateDir` (~/.openclaw) is a real local directory — a symlink onto the
 *     mount left by the previous layout is replaced.
 *  2. `stateDir/workspace` is a real local directory too — the symlink onto
 *     the mount used by an intermediate layout is replaced (its target on the
 *     mount is the mirror and is restored in step 3).
 *  3. The mirror on the mount is restored to local disk (see restoreStateDir).
 *
 * Returns `{ available: false }` when the mount is absent (local-only mode:
 * S3 sync remains the only persistence, as before).
 */
function setupSessionStorage({ homeDir, mountDir, log = console } = {}) {
  const p = resolvePaths({ homeDir, mountDir });
  try {
    if (!fs.statSync(p.mountDir).isDirectory()) throw new Error("Session storage mount is not a directory");
  } catch (err) {
    if (err.code === "ENOENT") return { available: false, ...p };
    throw err;
  }
  fs.mkdirSync(p.mountWorkspaceDir, { recursive: true });

  // 1. Real local state dir.
  let stateDirWas = "missing";
  try {
    const st = fs.lstatSync(p.stateDir);
    if (st.isSymbolicLink()) {
      stateDirWas = `symlink -> ${fs.readlinkSync(p.stateDir)}`;
      fs.unlinkSync(p.stateDir);
    } else if (st.isDirectory()) {
      stateDirWas = "directory";
    } else {
      throw new Error("Local state path is not a directory");
    }
  } catch (err) { if (err.code !== "ENOENT") throw err; }
  fs.mkdirSync(p.stateDir, { recursive: true });

  // 2. Real local workspace dir.
  let workspaceWas = "missing";
  try {
    const st = fs.lstatSync(p.workspaceDir);
    if (st.isSymbolicLink()) {
      workspaceWas = `symlink -> ${fs.readlinkSync(p.workspaceDir)}`;
      fs.unlinkSync(p.workspaceDir);
    } else if (st.isDirectory()) {
      workspaceWas = "directory";
    } else {
      throw new Error("Local workspace path is not a directory");
    }
  } catch (err) { if (err.code !== "ENOENT") throw err; }
  fs.mkdirSync(p.workspaceDir, { recursive: true });

  // 3. Restore the mirror (state files + workspace) to local disk.
  const restored = restoreStateDir({ stateDir: p.stateDir, mountStateDir: p.mountStateDir, log });

  log.log(
    `[state-storage] Local state dir ${p.stateDir} (was: ${stateDirWas}); ` +
      `local workspace ${p.workspaceDir} (was: ${workspaceWas}); ` +
      `restored ${restored.files} state file(s) + ${restored.workspaceFiles} workspace file(s) from ${p.mountStateDir}` +
      (restored.skipped ? `, skipped ${restored.skipped}` : ""),
  );
  return { available: true, ...p, stateDirWas, workspaceWas, restored };
}

/**
 * Environment the gateway (and `openclaw doctor`) must run with so every
 * SQLite database and the default agent workspace (`<stateDir>/workspace`)
 * resolve under the local state dir.
 */
function gatewayStateEnv(stateDir) {
  return { OPENCLAW_STATE_DIR: stateDir };
}

// --- periodic mirror -------------------------------------------------------

let _mirrorTimer = null;
let _mirrorInFlight = null;

/**
 * Mirror now, coalescing concurrent callers onto one in-flight run.
 */
function mirrorNow(opts) {
  if (!_mirrorInFlight) {
    _mirrorInFlight = mirrorStateDir(opts).finally(() => {
      _mirrorInFlight = null;
    });
  }
  return _mirrorInFlight;
}

/**
 * Start mirroring the local state dir to the mount every `intervalMs`
 * (default STATE_MIRROR_INTERVAL_MS or 5 minutes). The mount is local
 * loopback NFS, so this is cheap; it bounds the history lost if the
 * container is stopped without a graceful SIGTERM.
 */
function startPeriodicMirror(opts, intervalMs) {
  if (opts.assertReady) opts.assertReady();
  const interval = intervalMs || parseInt(process.env.STATE_MIRROR_INTERVAL_MS || "300000", 10);
  if (_mirrorTimer) clearInterval(_mirrorTimer);
  _mirrorTimer = setInterval(() => {
    mirrorNow(opts).then(
      (r) => (opts.log || console).log(
        `[state-storage] Periodic mirror: ${r.files} file(s) copied, ${r.unchanged} unchanged, ${r.sqlite} sqlite snapshot(s), ${r.skipped} skipped, ${r.pruned} pruned` +
        (r.budgetSkipped ? `, ${r.budgetSkipped} left to S3 (MOUNT_BUDGET)` : "") +
        (r.mountErrors ? ", mount refused a write (MOUNT_WRITE_FAILED)" : ""),
      ),
      (err) => (opts.log || console).warn(`[state-storage] Periodic mirror failed: ${err.message}`),
    );
  }, interval);
  if (typeof _mirrorTimer.unref === "function") _mirrorTimer.unref();
  (opts.log || console).log(`[state-storage] Periodic mirror started (every ${interval / 1000}s)`);
  return _mirrorTimer;
}

function stopPeriodicMirror() {
  if (_mirrorTimer) {
    clearInterval(_mirrorTimer);
    _mirrorTimer = null;
  }
}

// --- workspace change watcher ----------------------------------------------

let _watcher = null;
let _watchDebounce = null;
let _watchDeadline = null;
let _watchFallbackTimer = null;
let _watchOpts = null;

function flushWorkspaceMirror(reason = "flush") {
  if (_watchDebounce) { clearTimeout(_watchDebounce); _watchDebounce = null; }
  if (_watchDeadline) { clearTimeout(_watchDeadline); _watchDeadline = null; }
  if (!_watchOpts) return null;
  const log = _watchOpts.log || console;
  try {
    const r = mirrorWorkspaceDir(_watchOpts);
    if (r.files || r.pruned || r.skipped) {
      log.log(
        `[state-storage] Workspace mirror (${reason}): ${r.files} file(s) copied, ${r.unchanged} unchanged, ${r.pruned} pruned` +
          (r.skipped ? `, ${r.skipped} skipped` : ""),
      );
    }
    return r;
  } catch (err) {
    log.warn(`[state-storage] Workspace mirror (${reason}) failed: ${err.message}`);
    return null;
  }
}

function scheduleWorkspaceMirror(debounceMs, maxWaitMs) {
  if (_watchDebounce) clearTimeout(_watchDebounce);
  _watchDebounce = setTimeout(() => flushWorkspaceMirror("change"), debounceMs);
  if (typeof _watchDebounce.unref === "function") _watchDebounce.unref();
  if (!_watchDeadline) {
    // A stream of changes must not postpone the mirror forever.
    _watchDeadline = setTimeout(() => flushWorkspaceMirror("deadline"), maxWaitMs);
    if (typeof _watchDeadline.unref === "function") _watchDeadline.unref();
  }
}

/**
 * Watch the local workspace and mirror it onto the mount shortly after any
 * change (debounced `debounceMs`, at most `maxWaitMs` after the first change
 * of a burst). Uses fs.watch({ recursive: true }); if that is unavailable it
 * falls back to a polling mirror every `fallbackIntervalMs`. Idempotent —
 * calling again replaces the previous watcher.
 *
 * @returns {{ mode: "watch" | "poll" | "none" }}
 */
function startWorkspaceWatcher(opts, { debounceMs = 2000, maxWaitMs = 15000, fallbackIntervalMs = 30000 } = {}) {
  if (opts.assertReady) opts.assertReady();
  stopWorkspaceWatcher();
  _watchOpts = opts;
  const log = opts.log || console;
  const workspaceDir = path.join(opts.stateDir, WORKSPACE_SUBDIR);
  try {
    fs.mkdirSync(workspaceDir, { recursive: true });
    _watcher = fs.watch(workspaceDir, { recursive: true, persistent: false }, () => {
      scheduleWorkspaceMirror(debounceMs, maxWaitMs);
    });
    _watcher.on("error", (err) => {
      log.warn(`[state-storage] Workspace watcher error: ${err.message} — falling back to polling`);
      try { _watcher.close(); } catch { /* already closed */ }
      _watcher = null;
      startWorkspacePollFallback(fallbackIntervalMs);
    });
    log.log(`[state-storage] Workspace watcher started on ${workspaceDir} (debounce ${debounceMs}ms)`);
    return { mode: "watch" };
  } catch (err) {
    log.warn(`[state-storage] fs.watch unavailable (${err.message}) — polling workspace every ${fallbackIntervalMs / 1000}s`);
    startWorkspacePollFallback(fallbackIntervalMs);
    return { mode: "poll" };
  }
}

function startWorkspacePollFallback(intervalMs) {
  if (_watchFallbackTimer) clearInterval(_watchFallbackTimer);
  _watchFallbackTimer = setInterval(() => flushWorkspaceMirror("poll"), intervalMs);
  if (typeof _watchFallbackTimer.unref === "function") _watchFallbackTimer.unref();
}

/**
 * Stop the watcher and mirror any pending change synchronously.
 */
function stopWorkspaceWatcher() {
  const pending = Boolean(_watchDebounce || _watchDeadline);
  if (_watcher) {
    try { _watcher.close(); } catch { /* already closed */ }
    _watcher = null;
  }
  if (_watchFallbackTimer) {
    clearInterval(_watchFallbackTimer);
    _watchFallbackTimer = null;
  }
  if (pending) flushWorkspaceMirror("stop");
  if (_watchDebounce) { clearTimeout(_watchDebounce); _watchDebounce = null; }
  if (_watchDeadline) { clearTimeout(_watchDeadline); _watchDeadline = null; }
  _watchOpts = null;
}

// Tickets are local capabilities, never caller-provided absence booleans.
const emptyMountTickets = new WeakMap();
function attachmentNumber(value) {
  return typeof value === "string" && /^[0-9]{1,10}$/.test(value) &&
    Number(value) <= 0xffffffff ? Number(value) : null;
}
function attachmentDeviceMatches(dev, mountDevice) {
  try {
    if (process.platform !== "linux" || typeof dev !== "string" || !/^[0-9]{1,20}$/.test(dev)) return null;
    const parts = /^([0-9]{1,10}):([0-9]{1,10})$/.exec(mountDevice || "");
    if (!parts || parts[0] !== mountDevice) return null;
    const value = BigInt(dev), major = BigInt(parts[1]), minor = BigInt(parts[2]);
    if (value > 0xffffffffffffffffn || major > 0xffffffffn || minor > 0xffffffffn) return null;
    return ((value >> 8n & 0xfffn) | (value >> 32n & 0xfffff000n)) === major &&
      ((value & 0xffn) | (value >> 12n & 0xffffff00n)) === minor;
  } catch { return null; }
}
function selectVisibleAttachment(p, attached, expectedIdentity) {
  if (process.platform !== "linux" ||
      !["openSync", "closeSync", "fstatSync"].every((key) => typeof fs[key] === "function") ||
      !["O_RDONLY", "O_DIRECTORY", "O_NOFOLLOW"].every((key) => Number.isInteger(fs.constants?.[key]))) {
    return null;
  }
  const rows = attached.map((row) => {
    const fields = row.line.split(" - ")[0].split(" ");
    const id = attachmentNumber(fields[0]), parent = attachmentNumber(fields[1]);
    const device = /^([0-9]{1,10}):([0-9]{1,10})$/.exec(fields[2] || "");
    return id !== null && id > 0 && parent !== null && fields.length >= 6 &&
      device && attachmentNumber(device[1]) !== null && attachmentNumber(device[2]) !== null &&
      /^[a-zA-Z0-9._+-]{1,64}$/.test(row.type)
      ? { row, id, device: fields[2] } : null;
  });
  if (rows.some((row) => row === null)) return null;
  const identity = (stat) => {
    if (!stat.isDirectory() || stat.isSymbolicLink()) return null;
    const values = [stat.dev, stat.ino, stat.mode, stat.mtimeNs, stat.ctimeNs];
    return values.every((value) => typeof value === "bigint" && value >= 0n) ? values.map(String) : null;
  };
  const sameIdentity = (stat) => JSON.stringify(identity(stat)) === JSON.stringify(expectedIdentity);
  let fd, selected = null;
  try {
    fd = fs.openSync(p.mountDir, fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW);
    const opened = fs.fstatSync(fd, { bigint: true });
    if (!sameIdentity(opened)) return null;
    const lines = fs.readFileSync(`/proc/self/fdinfo/${fd}`, "utf8").split("\n")
      .filter((line) => /^mnt_id(?:[ \t:]|$)/.test(line));
    const match = lines.length === 1 ? /^mnt_id:[ \t]+([0-9]{1,10})[ \t]*$/.exec(lines[0]) : null;
    const id = match ? attachmentNumber(match[1]) : null;
    if (id === null || id === 0) return null;
    const matching = rows.filter((row) => row.id === id);
    if (matching.length !== 1) return null;
    const active = matching[0];
    if (!["nfs", "nfs4"].includes(active.row.type) ||
        attachmentDeviceMatches(String(opened.dev), active.device) !== true) return null;
    if (fs.realpathSync(p.mountDir) !== path.resolve(p.mountDir) ||
        !sameIdentity(fs.fstatSync(fd, { bigint: true })) ||
        !sameIdentity(fs.lstatSync(p.mountDir, { bigint: true }))) return null;
    selected = active;
  } catch {
    selected = null;
  } finally {
    if (fd !== undefined) {
      try { fs.closeSync(fd); }
      catch { selected = null; }
    }
  }
  if (!selected) return null;
  return { ...selected.row, visibility: { mountId: selected.id, identity: expectedIdentity } };
}
function liveSeedIneligible(reason) {
  return Object.assign(new Error("Live seed storage is ineligible"), {
    code: "LIVE_SEED_INELIGIBLE", reason,
  });
}

function inspectEmptySessionStorage({ homeDir, mountDir } = {}) {
  const p = resolvePaths({ homeDir, mountDir });
  let mountIdentity;
  const rejected = (reason) => { throw liveSeedIneligible(reason); };
  const directory = (file) => {
    const stat = fs.lstatSync(file, { bigint: true });
    if (!stat.isDirectory() || stat.isSymbolicLink()) rejected("layout_not_directory");
    const description = [stat.dev, stat.ino, stat.mode, stat.mtimeNs, stat.ctimeNs].map(String);
    return description;
  };
  try {
    for (const file of [path.dirname(p.stateDir), p.stateDir, p.workspaceDir, p.mountDir]) {
      if (fs.realpathSync(file) !== path.resolve(file)) rejected("layout_symlink");
      const description = directory(file);
      if (file === p.mountDir) mountIdentity = description;
    }
  } catch (error) {
    if (error.code === "ENOENT") rejected("mount_or_layout_missing");
    throw error;
  }
  if (insideBase(path.resolve(p.mountDir), p.stateDir) ||
      insideBase(path.resolve(p.stateDir), p.mountDir)) rejected("layout_overlap");

  let mountInfo;
  try { mountInfo = fs.readFileSync("/proc/self/mountinfo", "utf8"); }
  catch (error) {
    if (error.code === "ENOENT") {
      rejected("attachment_unconfirmed");
    }
    throw error;
  }
  const decode = (value) => value.replace(/\\([0-7]{3})/g,
    (_, octal) => String.fromCharCode(parseInt(octal, 8)));
  const attached = mountInfo.trim().split("\n").filter(Boolean).flatMap((line) => {
    const parts = line.split(" - ");
    if (parts.length !== 2) rejected("attachment_unconfirmed");
    const fields = parts[0].split(" ");
    const type = parts[1].split(" ")[0];
    const point = decode(fields[4] || "");
    return point === path.resolve(p.mountDir) ? [{ line, type }] : [];
  });
  const selected = attached.length > 1
    ? selectVisibleAttachment(p, attached, mountIdentity)
    : attached.length === 1 && ["nfs", "nfs4"].includes(attached[0].type) ? attached[0] : null;
  if (!selected) {
    rejected("attachment_unconfirmed");
  }
  const allowed = new Set(["", STATE_DIRNAME, `${STATE_DIRNAME}/${WORKSPACE_SUBDIR}`]);
  const inventory = [];
  function walk(dir, relative = "") {
    if (!allowed.has(relative)) rejected("mount_content");
    const before = directory(dir);
    const names = fs.readdirSync(dir).sort();
    for (const name of names) {
      if (typeof name !== "string" || !name || name === "." || name === ".." ||
          name.includes("/") || name.includes("\0")) rejected("mount_inventory_invalid");
      const rel = relative ? `${relative}/${name}` : name;
      if (!allowed.has(rel)) rejected("mount_content");
      walk(path.join(dir, name), rel);
    }
    const after = directory(dir);
    if (JSON.stringify(before) !== JSON.stringify(after) ||
        JSON.stringify(names) !== JSON.stringify(fs.readdirSync(dir).sort())) {
      rejected("mount_inventory_changed");
    }
    inventory.push([relative, before]);
  }
  walk(p.mountDir);
  const ticket = Object.freeze({});
  emptyMountTickets.set(ticket, {
    paths: p, mount: selected.line, inventory,
    ...(selected.visibility ? { visibility: selected.visibility } : {}),
    local: [p.stateDir, p.workspaceDir].map((file) => directory(file).slice(0, 3)),
  });
  return ticket;
}

function assertEmptySessionStorage(ticket) {
  const previous = emptyMountTickets.get(ticket);
  if (!previous) throw liveSeedIneligible("mount_ticket_invalid");
  const currentTicket = inspectEmptySessionStorage({
    homeDir: path.dirname(previous.paths.stateDir), mountDir: previous.paths.mountDir,
  });
  const current = emptyMountTickets.get(currentTicket);
  emptyMountTickets.delete(currentTicket);
  if (JSON.stringify(previous) !== JSON.stringify(current)) {
    throw liveSeedIneligible("mount_inventory_changed");
  }
}

function attachEmptySessionStorage(ticket) {
  assertEmptySessionStorage(ticket);
  const { paths: p } = emptyMountTickets.get(ticket);
  emptyMountTickets.delete(ticket);
  // Only the empty mirror is created. The live local DB and workspace stay put.
  fs.mkdirSync(p.mountWorkspaceDir, { recursive: true });
  return { available: true, ...p };
}

module.exports = {
  inspectEmptySessionStorage,
  assertEmptySessionStorage,
  attachEmptySessionStorage,
  assertSafePath,
  assertSqliteFile,
  DEFAULT_MOUNT,
  STATE_DIRNAME,
  WORKSPACE_SUBDIR,
  resolvePaths,
  setupSessionStorage,
  sessionStorageHasContent,
  restoreStateDir,
  mirrorStateDir,
  mirrorWorkspaceDir,
  mirrorNow,
  snapshotSqliteToFile,
  gatewayStateEnv,
  startPeriodicMirror,
  stopPeriodicMirror,
  startWorkspaceWatcher,
  stopWorkspaceWatcher,
  flushWorkspaceMirror,
  // Exported for testing
  DEFAULT_MIRROR_BUDGET_BYTES,
  diskUsageBytes,
  _resetMirrorMemo: () => _mirrored.clear(),
  walkFiles,
  isTransientFile,
  isTransientDir,
  isWorkspacePath,
  getNodeSqlite,
};
