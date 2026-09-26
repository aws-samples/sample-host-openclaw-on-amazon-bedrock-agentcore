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

module.exports = {
  findLegacySessionStores,
  planLegacyImport,
  moveImportedIndexAside,
  fingerprintIndexes,
  writeReceipts,
  agentPaths,
  IMPORTED_INDEX_NAME,
  RECEIPT_NAME,
  SQLITE_STORE_NAME,
};
