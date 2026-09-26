/**
 * Legacy (pre-2.0) session store import bookkeeping.
 *
 * OpenClaw <= 2026.7.x kept `~/.openclaw/agents/<id>/sessions/sessions.json`
 * (+ per-session .jsonl transcripts); 2.0 keeps rows in
 * `~/.openclaw/agents/<id>/agent/openclaw-agent.sqlite`. The 2.0 gateway does
 * not migrate on its own — when startup finds a `sessions.json` it REFUSES
 * READINESS (src/config/sessions/startup-migration.ts, "Legacy session store
 * requires migration") — so the contract runs `openclaw doctor --fix` before
 * spawning it (agentcore-contract.js migrateLegacySessionStore()). Doctor
 * imports the index into the SQLite store and archives the source files.
 *
 * The bridge never deletes from S3, so the 1.x `sessions.json` and transcripts
 * stay there (that is what keeps a rollback to 1.x possible) and every cold
 * start restores them again, next to the 2.0 SQLite store that now holds the
 * imported history. Without a record of the import, every cold start would
 * re-run doctor (~53 s for the live user's 990 sessions) and doctor would
 * re-write the imported session entries over what 2.0 has since done with them.
 *
 * So the contract records a RECEIPT next to the SQLite store once doctor has
 * imported an index — the sha256 of the index bytes it imported — and on later
 * boots an index whose bytes match the receipt, with the store present, is
 * moved aside locally (`sessions.json.pre-2.0-imported`, never uploaded) instead
 * of imported again. This mirrors OpenClaw's own deferred-import receipts,
 * which also bind the source sha256. If the index differs (1.x wrote new
 * sessions during a rollback) or the store is missing (its backup never
 * landed), the import runs as before: the failure mode is today's behaviour,
 * never lost history.
 *
 * The receipt is a small JSON file in the state dir, so workspace-sync backs
 * it up and restores it with everything else.
 */

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const LEGACY_INDEX_NAME = "sessions.json";
// Name the moved-aside index gets. Listed in workspace-sync SKIP_PATTERNS so it
// is never uploaded (S3 has the original), and not `sessions.json`, so the
// gateway's legacy-store check ignores it.
const IMPORTED_INDEX_NAME = "sessions.json.pre-2.0-imported";
const SQLITE_STORE_NAME = "openclaw-agent.sqlite";
const RECEIPT_NAME = ".pre-2.0-import.json";
const RECEIPT_VERSION = 1;

/**
 * Paths for one agent's legacy index and 2.0 store.
 * @param {string} openclawDir state dir
 * @param {string} agentId
 */
function agentPaths(openclawDir, agentId) {
  const agentRoot = path.join(openclawDir, "agents", agentId);
  return {
    agentId,
    legacyIndex: path.join(agentRoot, "sessions", LEGACY_INDEX_NAME),
    importedIndex: path.join(agentRoot, "sessions", IMPORTED_INDEX_NAME),
    sqliteStore: path.join(agentRoot, "agent", SQLITE_STORE_NAME),
    receipt: path.join(agentRoot, "agent", RECEIPT_NAME),
  };
}

/**
 * Find pre-2.0 session stores in the state dir. Returns the legacy index paths
 * found (empty when already migrated / fresh install), like the contract's
 * original findLegacySessionStores().
 */
function findLegacySessionStores(openclawDir) {
  const found = [];
  let agents = [];
  try {
    agents = fs.readdirSync(path.join(openclawDir, "agents"), { withFileTypes: true });
  } catch {
    return found; // No agents dir — fresh install
  }
  for (const entry of agents) {
    if (!entry.isDirectory()) continue;
    const { legacyIndex } = agentPaths(openclawDir, entry.name);
    if (fs.existsSync(legacyIndex)) found.push(legacyIndex);
  }
  return found;
}

function sha256File(filePath) {
  return crypto.createHash("sha256").update(fs.readFileSync(filePath)).digest("hex");
}

function readReceipt(receiptPath) {
  try {
    const receipt = JSON.parse(fs.readFileSync(receiptPath, "utf8"));
    if (!receipt || receipt.version !== RECEIPT_VERSION || typeof receipt.indexSha256 !== "string") {
      return null;
    }
    return receipt;
  } catch {
    return null;
  }
}

/**
 * Decide, per legacy index present, whether doctor must import it or whether
 * it was already imported into the 2.0 store that is on disk.
 *
 * @param {string} openclawDir
 * @returns {{ toImport: string[], alreadyImported: Array<{ legacyIndex: string, importedIndex: string, sqliteStore: string }> }}
 *   `toImport` are legacy index paths doctor must handle; `alreadyImported`
 *   ones match their receipt and have their SQLite store present.
 */
function planLegacyImport(openclawDir) {
  const plan = { toImport: [], alreadyImported: [] };
  for (const legacyIndex of findLegacySessionStores(openclawDir)) {
    const agentId = path.basename(path.dirname(path.dirname(legacyIndex)));
    const p = agentPaths(openclawDir, agentId);
    const receipt = readReceipt(p.receipt);
    let matches = false;
    if (receipt && fs.existsSync(p.sqliteStore)) {
      try {
        matches = sha256File(legacyIndex) === receipt.indexSha256;
      } catch {
        matches = false;
      }
    }
    if (matches) plan.alreadyImported.push({ legacyIndex, importedIndex: p.importedIndex, sqliteStore: p.sqliteStore });
    else plan.toImport.push(legacyIndex);
  }
  return plan;
}

/**
 * Move an already-imported index aside so the gateway starts (it only looks
 * for the exact `sessions.json` name). Overwrites a previous moved-aside copy.
 * Returns false (and logs) when the rename fails — the caller then treats the
 * index as needing import, which is safe.
 */
function moveImportedIndexAside({ legacyIndex, importedIndex }, log = console) {
  try {
    fs.renameSync(legacyIndex, importedIndex);
    return true;
  } catch (err) {
    log.warn(`[legacy-import] Could not move aside ${legacyIndex}: ${err.message} — importing it again instead`);
    return false;
  }
}

/**
 * Hash the legacy indexes doctor is about to import. Must be called BEFORE
 * doctor runs (it archives the files away); the result feeds writeReceipts().
 * An unreadable index yields no entry (no receipt will be written for it).
 * @returns {Map<string, string>} legacyIndex -> sha256
 */
function fingerprintIndexes(legacyIndexes) {
  const out = new Map();
  for (const legacyIndex of legacyIndexes) {
    try {
      out.set(legacyIndex, sha256File(legacyIndex));
    } catch {
      /* unreadable: doctor will report it; no receipt */
    }
  }
  return out;
}

/**
 * After a SUCCESSFUL doctor run (exit 0, no legacy index left), record one
 * receipt per imported index next to that agent's SQLite store. A receipt is
 * written only when the store exists — a receipt without a store would make a
 * later boot skip an import that never happened.
 *
 * @param {string} openclawDir
 * @param {Map<string, string>} fingerprints from fingerprintIndexes()
 * @returns {string[]} receipt paths written
 */
function writeReceipts(openclawDir, fingerprints, log = console) {
  const written = [];
  for (const [legacyIndex, indexSha256] of fingerprints) {
    const agentId = path.basename(path.dirname(path.dirname(legacyIndex)));
    const p = agentPaths(openclawDir, agentId);
    if (!fs.existsSync(p.sqliteStore)) {
      log.warn(`[legacy-import] No ${SQLITE_STORE_NAME} for agent ${agentId} after import — not recording a receipt`);
      continue;
    }
    const receipt = {
      version: RECEIPT_VERSION,
      agentId,
      indexSha256,
      importedAt: new Date().toISOString(),
      store: SQLITE_STORE_NAME,
    };
    try {
      fs.mkdirSync(path.dirname(p.receipt), { recursive: true });
      const tmp = `${p.receipt}.tmp-${Date.now()}`;
      fs.writeFileSync(tmp, JSON.stringify(receipt, null, 2) + "\n");
      fs.renameSync(tmp, p.receipt);
      written.push(p.receipt);
    } catch (err) {
      log.warn(`[legacy-import] Could not write receipt ${p.receipt}: ${err.message}`);
    }
  }
  return written;
}

// ---------------------------------------------------------------------------
// Files doctor RETIRED during the import (F5, us-west-2 F1 staging test).
//
// `openclaw doctor --fix` does more than import sessions.json: it also
// migrates and then removes/archives the other pre-2.0 state files —
// `workspace/.openclaw/workspace-state.json`, `agents/<id>/agent/auth-profiles.json`,
// `update-check.json`, `exec-approvals.json`, the imported `.jsonl`
// transcripts, … — and the 2.0 gateway REFUSES to run while some of them exist
// (`StartupMaintenanceRequiredError: Legacy workspace setup state requires
// migration`, exit 78; `Auth profile store … requires legacy credential
// migration`). The receipt above only covers `sessions.json`. Because the
// bridge never deletes from S3, every later restore (cold start, and the
// fill-missing restore of a same-session restart) brings the retired files
// back next to the migrated 2.0 store, and doctor is only re-run when a
// sessions.json without a receipt is present — so after the first successful
// upgrade the gateway crash-loops on exit 78 and the shim answers forever.
//
// So, on a successful import, the contract also records WHICH files doctor
// retired (state-dir-relative path + sha256 of the bytes it had) in a manifest
// at the state-dir root, and after every restore deletes a restored file again
// when its bytes still match that record — before the config write and the
// gateway spawn. S3 keeps the originals (the rollback to 1.x is unchanged),
// nothing about OpenClaw's file layout is hardcoded (the list is a before/after
// diff of the state dir), and a file whose bytes differ from the record (1.x
// rewrote it during a rollback, or the user recreated it) is left alone and
// reported so the caller can run doctor again.
// ---------------------------------------------------------------------------

const RETIRED_MANIFEST_NAME = ".pre-2.0-retired-files.json";
const RETIRED_MANIFEST_VERSION = 1;
// Files above this size are not fingerprinted before doctor runs (doctor
// retires small JSON/JSONL files; the SQLite stores it writes are far larger
// and are never retired). Keeps the pre-doctor walk to a hash of ~200 MB of
// small files for the live user rather than every large upload.
const RETIRED_FINGERPRINT_MAX_BYTES = 64 * 1024 * 1024;

function retiredManifestPath(openclawDir) {
  return path.join(openclawDir, RETIRED_MANIFEST_NAME);
}

function toRelative(openclawDir, filePath) {
  return path.relative(openclawDir, filePath).split(path.sep).join("/");
}

/**
 * Walk the state dir and return relative path -> sha256 for every regular file
 * up to RETIRED_FINGERPRINT_MAX_BYTES. Follows directory symlinks (the
 * workspace dir is a symlink onto the session-storage mount on AgentCore) with
 * a realpath cycle guard. Unreadable files are skipped (they cannot be
 * fingerprinted, so they can never be pruned either — the safe side).
 * @returns {Map<string, string>}
 */
function fingerprintStateDir(openclawDir, { maxBytes = RETIRED_FINGERPRINT_MAX_BYTES } = {}) {
  const out = new Map();
  const seen = new Set();
  const walk = (dir) => {
    let real;
    try {
      real = fs.realpathSync(dir);
    } catch {
      return;
    }
    if (seen.has(real)) return;
    seen.add(real);
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      let st;
      try {
        st = fs.statSync(full); // follows symlinks
      } catch {
        continue; // dangling link
      }
      if (st.isDirectory()) {
        walk(full);
      } else if (st.isFile() && st.size <= maxBytes) {
        try {
          out.set(toRelative(openclawDir, full), sha256File(full));
        } catch {
          /* unreadable: never recorded, never pruned */
        }
      }
    }
  };
  walk(openclawDir);
  return out;
}

/**
 * Files that were present before doctor and are gone (moved or deleted)
 * afterwards. The manifest itself and the receipt are never listed.
 * @param {Map<string, string>} before fingerprintStateDir() taken before doctor
 * @param {string} openclawDir
 * @returns {Array<{ path: string, sha256: string }>}
 */
function diffRetiredFiles(before, openclawDir) {
  const retired = [];
  for (const [rel, sha256] of before) {
    if (rel === RETIRED_MANIFEST_NAME || path.basename(rel) === RECEIPT_NAME) continue;
    if (!fs.existsSync(path.join(openclawDir, rel))) retired.push({ path: rel, sha256 });
  }
  retired.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  return retired;
}

function readRetiredManifest(openclawDir) {
  try {
    const manifest = JSON.parse(fs.readFileSync(retiredManifestPath(openclawDir), "utf8"));
    if (!manifest || manifest.version !== RETIRED_MANIFEST_VERSION || !Array.isArray(manifest.files)) {
      return null;
    }
    return manifest;
  } catch {
    return null;
  }
}

/**
 * After a SUCCESSFUL doctor run, record the files it retired. Entries from an
 * earlier manifest are kept (merged by path, newest hash wins) so a second
 * import — 1.x wrote new sessions during a rollback and doctor ran again —
 * extends the record instead of forgetting the files retired the first time.
 * Written atomically; a write failure is logged and leaves any old manifest.
 *
 * @returns {{ path: string, files: Array<{ path: string, sha256: string }> } | null}
 *   what was written, or null when nothing was retired and no manifest existed.
 */
function writeRetiredManifest(openclawDir, retired, log = console) {
  const previous = readRetiredManifest(openclawDir);
  if (retired.length === 0 && !previous) return null;
  const byPath = new Map((previous ? previous.files : []).map((f) => [f.path, f.sha256]));
  for (const f of retired) byPath.set(f.path, f.sha256);
  const files = [...byPath].map(([p, sha256]) => ({ path: p, sha256 })).sort((a, b) => (a.path < b.path ? -1 : 1));
  const manifest = {
    version: RETIRED_MANIFEST_VERSION,
    recordedAt: new Date().toISOString(),
    files,
  };
  const target = retiredManifestPath(openclawDir);
  try {
    const tmp = `${target}.tmp-${Date.now()}`;
    fs.writeFileSync(tmp, JSON.stringify(manifest, null, 2) + "\n");
    fs.renameSync(tmp, target);
    return { path: target, files };
  } catch (err) {
    log.warn(`[legacy-import] Could not write retired-files manifest ${target}: ${err.message}`);
    return null;
  }
}

/**
 * After a restore, remove again every file the manifest lists whose bytes
 * still match the record (the restore resurrected doctor's input). A listed
 * file with different bytes is reported in `changed` and left in place: 1.x
 * wrote to it during a rollback (or the user recreated it), so it needs doctor
 * again, not deletion. Paths that escape the state dir are ignored.
 *
 * @returns {{ pruned: string[], changed: string[], absent: number }}
 */
function pruneRetiredFiles(openclawDir, log = console) {
  const result = { pruned: [], changed: [], absent: 0 };
  const manifest = readRetiredManifest(openclawDir);
  if (!manifest) return result;
  const root = path.resolve(openclawDir);
  for (const entry of manifest.files) {
    if (!entry || typeof entry.path !== "string" || typeof entry.sha256 !== "string") continue;
    const full = path.resolve(root, entry.path);
    if (full !== root && !full.startsWith(root + path.sep)) continue; // outside the state dir
    let st;
    try {
      st = fs.lstatSync(full);
    } catch {
      result.absent += 1;
      continue;
    }
    if (!st.isFile()) continue;
    let sha256;
    try {
      sha256 = sha256File(full);
    } catch (err) {
      log.warn(`[legacy-import] Could not read ${entry.path} to compare with the retired-files record: ${err.message}`);
      continue;
    }
    if (sha256 !== entry.sha256) {
      result.changed.push(entry.path);
      continue;
    }
    try {
      fs.unlinkSync(full);
      result.pruned.push(entry.path);
    } catch (err) {
      log.warn(`[legacy-import] Could not remove resurrected legacy file ${entry.path}: ${err.message}`);
    }
  }
  return result;
}

module.exports = {
  findLegacySessionStores,
  planLegacyImport,
  moveImportedIndexAside,
  fingerprintIndexes,
  writeReceipts,
  fingerprintStateDir,
  diffRetiredFiles,
  writeRetiredManifest,
  readRetiredManifest,
  pruneRetiredFiles,
  retiredManifestPath,
  agentPaths,
  IMPORTED_INDEX_NAME,
  RECEIPT_NAME,
  RETIRED_MANIFEST_NAME,
  SQLITE_STORE_NAME,
};
