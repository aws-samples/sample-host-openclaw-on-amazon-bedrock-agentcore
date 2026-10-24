/**
 * Workspace Sync — .openclaw/ directory persistence to/from S3.
 *
 * Restores a user's .openclaw/ directory from S3 on session start, and saves
 * it back: a full periodic save, a change-driven save of just the files that
 * changed (debounced, see startChangeBackup()), and a best-effort final save
 * on SIGTERM. Uses the same S3 bucket and client pattern as the proxy's
 * workspace files (readUserFileFromS3/writeUserFileToS3).
 *
 * Nothing is uploaded until restoreWorkspace() has finished (or confirmed that
 * S3 holds no state for the namespace), and nothing is uploaded from a
 * container whose restore failed: a partially restored state dir must never
 * overwrite the good copy in S3 (see the upload gate below).
 *
 * Namespace format: {actorId.replace(/:/g, "_")} (e.g., "telegram_123456789")
 * S3 prefix: {namespace}/.openclaw/
 * Local path: $HOME/.openclaw/ (defaults to /root/.openclaw/). On AgentCore Runtime,
 * this and `workspace/` are local directories. state-storage.js mirrors them
 * to the session storage mount when available; S3 backups include both.
 */

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const zlib = require("zlib");
const { pipeline } = require("stream/promises");
const stateStorage = require("./state-storage");
const { validateNamespace, assertCredentialsFresh } = require("./scoped-credentials");

// Lazy-require AWS SDK (only available inside Docker image, not in local dev/test)
let _s3Sdk = null;
function getS3Sdk() {
  if (!_s3Sdk) {
    _s3Sdk = require("@aws-sdk/client-s3");
  }
  return _s3Sdk;
}

const BUCKET = process.env.S3_USER_FILES_BUCKET;
const LOCAL_PATH = process.env.HOME
  ? `${process.env.HOME}/.openclaw`
  : "/root/.openclaw";
const WORKSPACE_PREFIX = ".openclaw";

// Skip patterns — files/dirs that should not be synced to S3
const SKIP_PATTERNS = [
  "node_modules/",
  ".cache/",
  "*.log",
  "*.lock",
  ".npm/",
  "package-lock.json",
  "openclaw.json",
  "AGENTS.md",
  "workspace/AGENTS.md",
  // Security: exclude files that commonly contain secrets
  ".env",
  ".secrets/",
  "*.pem",
  "*.key",
  // SQLite sidecars (OpenClaw 2.0 keeps session/auth state in per-agent SQLite
  // databases, WAL mode). The -wal/-shm/-journal files are only meaningful
  // together with the exact main-db bytes they were written against; copying
  // them file-by-file yields a corrupt or rolled-back restore. The main *.sqlite
  // file is NOT skipped — saveWorkspace() uploads a consistent snapshot of it
  // instead of the raw live file (see snapshotSqlite()).
  "*.sqlite-wal",
  "*.sqlite-shm",
  "*.sqlite-journal",
  "*.sqlite-snapshot", // in-flight snapshotSqlite() staging files
  // OpenClaw gateway lock files. SQLite-named but hold no state; snapshotting
  // them fails ("not an error") on every save and they must never be restored.
  "*.lock.sqlite",
  // Rotated config backups written by `openclaw doctor` — regenerated, never restored.
  "openclaw.json.bak",
  "*.json.bak.1",
  "*.json.bak.2",
  "*.json.bak.3",
  "*.json.bak.4",
  // `openclaw doctor --fix` moves the pre-2.0 sessions.json + .jsonl transcripts
  // here once it has imported them into the agent SQLite store. S3 still holds
  // the originals (deletes are never propagated), so backing the archive up
  // would only double the small-file restore time of every cold start.
  "session-sqlite-import-archive/",
  // The pre-2.0 index the bridge moves aside once its import receipt matches
  // (legacy-session-import.js). S3 keeps the original sessions.json.
  "sessions.json.pre-2.0-imported",
];

// Per-file size limits.
//
// Non-SQLite files are read into memory whole and PUT as-is, so they keep the
// 10 MB cap. SQLite databases are handled differently: their snapshot is
// streamed (snapshot file -> gzip -> S3 multipart) and may be far larger. The
// live user's imported 1.x history is one ~299 MB openclaw-agent.sqlite, which
// the 10 MB cap skipped on EVERY save — so all post-upgrade 2.0 history was
// lost at each cold start and each cold start re-ran the 1.x import (F1,
// us-west-2 rehearsal). A snapshot up to `maxFileSize` still goes up buffered
// and uncompressed, exactly as before; above it the gzip path is used; above
// `maxSqliteFileSize` (uncompressed) the database is skipped with a clear log.
const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10MB
const DEFAULT_MAX_SQLITE_FILE_SIZE = 1024 * 1024 * 1024; // 1 GiB
// One multipart part per this many gzip'd bytes (S3 minimum is 5 MiB); at most
// ~2 parts are ever held in memory, whatever the database size.
const DEFAULT_UPLOAD_PART_SIZE = 8 * 1024 * 1024;
// Whole-restore limits. The per-file caps above bound one object; these bound
// one restore: the bytes written to local disk (after gzip decompression) and
// the number of objects under the namespace prefix. The uploader has no such
// cap, so the defaults sit far above anything a cold start can restore inside
// its wait (the live user's state dir is ~1,350 files / a few hundred MB, and
// deploy.sh's default restore wait is 180 s): 16 GiB and 100,000 objects.
// Override with WORKSPACE_RESTORE_MAX_BYTES / WORKSPACE_RESTORE_MAX_FILES.
const DEFAULT_MAX_RESTORE_BYTES = 16 * 1024 * 1024 * 1024; // 16 GiB
const DEFAULT_MAX_RESTORE_FILES = 100000;
// Free disk space a restore must leave on the local filesystem. A download
// that would eat into it fails the restore with a clear reason before it is
// written, instead of running the microVM's disk out (ENOSPC) and leaving the
// restore failed with uploads disabled. Override with
// WORKSPACE_RESTORE_FREE_DISK_RESERVE_BYTES.
const DEFAULT_RESTORE_FREE_DISK_RESERVE_BYTES = 256 * 1024 * 1024; // 256 MiB

function parseRestoreLimit(name, fallback) {
  const raw = process.env[name];
  if (raw === undefined || raw === "") return fallback;
  const n = Number(raw);
  if (!Number.isSafeInteger(n) || n < 1) {
    console.warn(
      "[workspace-sync] Ignoring invalid %s=%s — using %d",
      name, String(raw).slice(0, 32), fallback,
    );
    return fallback;
  }
  return n;
}

const _limits = {
  maxFileSize: MAX_FILE_SIZE,
  maxSqliteFileSize: parseInt(
    process.env.WORKSPACE_SYNC_MAX_SQLITE_BYTES || String(DEFAULT_MAX_SQLITE_FILE_SIZE),
    10,
  ),
  uploadPartSize: DEFAULT_UPLOAD_PART_SIZE,
  maxRestoreBytes: parseRestoreLimit("WORKSPACE_RESTORE_MAX_BYTES", DEFAULT_MAX_RESTORE_BYTES),
  maxRestoreFiles: parseRestoreLimit("WORKSPACE_RESTORE_MAX_FILES", DEFAULT_MAX_RESTORE_FILES),
  restoreFreeDiskReserveBytes: parseRestoreLimit(
    "WORKSPACE_RESTORE_FREE_DISK_RESERVE_BYTES",
    DEFAULT_RESTORE_FREE_DISK_RESERVE_BYTES,
  ),
};

// Files with this extension are SQLite databases and are uploaded via
// snapshotSqlite() rather than fs.readFileSync().
const SQLITE_EXT = ".sqlite";

// Lazy-require node:sqlite (Node >= 22.13 / 24 in the container image; absent on
// older local Node, in which case SQLite files are skipped with a warning
// rather than uploaded live).
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

// Credential patterns — detect potential secrets before S3 upload.
// Files matching these are still uploaded (user's choice) but a warning is logged.
// The designated native key store (user-api-keys.json) is exempt.
const CREDENTIAL_PATTERNS = [
  /AKIA[0-9A-Z]{16}/, // AWS access key IDs
  /-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----/, // Private keys
  /sk-[a-zA-Z0-9]{20,}/, // OpenAI / Anthropic keys
  /xox[bpas]-[a-zA-Z0-9-]{10,}/, // Slack tokens
  /\d{8,10}:[a-zA-Z0-9_-]{35}/, // Telegram bot tokens
  /ghp_[a-zA-Z0-9]{36}/, // GitHub personal access tokens
  /glpat-[a-zA-Z0-9_-]{20,}/, // GitLab personal access tokens
];
// File exempt from credential scanning — the designated native API key store.
// Users who choose "native" storage consciously store keys here.
const CREDENTIAL_SCAN_EXEMPT = "user-api-keys.json";

// S3 client singleton (same pattern as agentcore-proxy.js)
let _s3Client = null;
let _scopedCredentials = null;
let _credentialProvider = null;

function getS3Client() {
  if (!_credentialProvider && _scopedCredentials?.expiration !== undefined) assertCredentialsFresh(_scopedCredentials);
  if (!_s3Client) {
    const { S3Client } = getS3Sdk();
    const opts = { region: process.env.AWS_REGION };
    if (_credentialProvider) {
      const provider = _credentialProvider;
      opts.credentials = async () => {
        const credentials = await provider();
        if (provider !== _credentialProvider) throw new Error("Workspace credential owner changed");
        return { ...assertCredentialsFresh(credentials) };
      };
    } else if (_scopedCredentials) {
      opts.credentials = async () => {
        if (_scopedCredentials.expiration !== undefined) assertCredentialsFresh(_scopedCredentials);
        return { ..._scopedCredentials };
      };
    }
    _s3Client = new S3Client(opts);
    if (_credentialProvider) {
      const send = _s3Client.send.bind(_s3Client);
      _s3Client.send = async (...args) => {
        // Check every operation, including cached clients and multipart parts.
        await opts.credentials();
        return send(...args);
      };
    }
  }
  return _s3Client;
}

function configureCredentialProvider(provider) {
  if (typeof provider !== "function") throw new TypeError("Workspace credential provider is required");
  if (_credentialProvider && _credentialProvider !== provider) {
    throw new Error("Workspace credential owner is already bound");
  }
  _credentialProvider = provider;
  _s3Client = null;
}

/**
 * Configure the S3 client with explicit credentials (scoped STS session).
 * Replaces the default client that uses the container's execution role.
 *
 * @param {object} credentials
 * @param {string} credentials.accessKeyId
 * @param {string} credentials.secretAccessKey
 * @param {string} [credentials.sessionToken]
 */
function configureCredentials(credentials) {
  if (!credentials || !credentials.accessKeyId) {
    throw new Error("configureCredentials: accessKeyId is required");
  }
  if (!credentials.secretAccessKey) {
    throw new Error("configureCredentials: secretAccessKey is required");
  }
  if (credentials.expiration !== undefined) assertCredentialsFresh(credentials);
  _scopedCredentials = {
    accessKeyId: credentials.accessKeyId,
    secretAccessKey: credentials.secretAccessKey,
    sessionToken: credentials.sessionToken,
    ...(credentials.expiration !== undefined ? { expiration: new Date(credentials.expiration) } : {}),
  };
  // Reset client so next getS3Client() picks up new credentials
  _s3Client = null;
}

/**
 * Scan file content for potential credentials/secrets.
 * Returns the name of the first matching pattern, or null if clean.
 *
 * @param {Buffer|string} content - File content to scan
 * @returns {string|null} - Pattern description if detected, null if clean
 */
function detectCredentials(content) {
  const text = typeof content === "string" ? content : content.toString("utf-8", 0, Math.min(content.length, 1024 * 64));
  for (const pattern of CREDENTIAL_PATTERNS) {
    if (pattern.test(text)) {
      return pattern.source.slice(0, 40);
    }
  }
  return null;
}

/**
 * Check if a relative path matches any skip pattern.
 */
function shouldSkip(relativePath) {
  for (const pattern of SKIP_PATTERNS) {
    if (pattern.endsWith("/")) {
      // Directory pattern
      if (
        relativePath.startsWith(pattern) ||
        relativePath.includes("/" + pattern)
      ) {
        return true;
      }
    } else if (pattern.startsWith("*")) {
      // Wildcard extension
      const ext = pattern.slice(1);
      if (relativePath.endsWith(ext)) return true;
    } else {
      if (relativePath === pattern || relativePath.endsWith("/" + pattern)) {
        return true;
      }
    }
  }
  return false;
}

/**
 * OpenClaw regenerates these state-root links from installed plugins.
 * Exclude them from backup while retaining validation for nested user
 * directories and restore paths.
 */
function isGeneratedPluginSkillPath(relativePath) {
  return relativePath === "plugin-skills" || relativePath.startsWith("plugin-skills/");
}

/**
 * Take a consistent point-in-time snapshot of a SQLite database that another
 * process (the OpenClaw gateway) may be writing to, and return its bytes.
 *
 * Uses node:sqlite's online backup API from a read-only connection inside a
 * read transaction — the same primitive OpenClaw's own `backup create` uses.
 * In WAL mode a reader sees every committed transaction, including frames that
 * are still in the -wal file and not yet checkpointed into the main db, so the
 * snapshot is complete as of the moment it starts and never torn by a
 * concurrent checkpoint. The result is a standalone db file (no sidecars).
 *
 * The snapshot is staged next to the source (same filesystem, so it works on
 * the session-storage mount) under a dot-prefixed name and removed afterwards.
 *
 * @param {string} dbPath - Absolute path to the *.sqlite file
 * @returns {Promise<Buffer>} snapshot bytes
 * @throws when node:sqlite is unavailable or the backup fails (caller skips the file)
 */
async function snapshotSqlite(dbPath) {
  const stagePath = await snapshotSqliteToFile(dbPath);
  try {
    return fs.readFileSync(stagePath);
  } finally {
    removeSnapshotFile(stagePath);
  }
}

/**
 * Same snapshot as snapshotSqlite(), left on disk: returns the path of the
 * staged copy, which the caller must remove with removeSnapshotFile(). This is
 * the form the large-database path uses so a multi-hundred-MB snapshot is
 * hashed and compressed as a stream and never held in memory whole.
 */
async function snapshotSqliteToFile(dbPath) {
  const sqlite = getNodeSqlite();
  if (!sqlite || typeof sqlite.backup !== "function") {
    throw new Error("node:sqlite backup API unavailable on this Node runtime");
  }
  // Unique per call: two saves of the same database can start in the same
  // millisecond, and must never back up into (or unlink) each other's file.
  const stagePath = path.join(
    path.dirname(dbPath),
    `.${path.basename(dbPath)}.${process.pid}-${Date.now()}-${crypto.randomUUID()}.sqlite-snapshot`,
  );
  const source = new sqlite.DatabaseSync(dbPath, { readOnly: true });
  try {
    // Bounded wait if the writer is mid-commit rather than failing fast.
    source.exec("PRAGMA busy_timeout = 5000");
    source.exec("BEGIN");
    try {
      await sqlite.backup(source, stagePath);
    } finally {
      try { source.exec("ROLLBACK"); } catch { /* read-only txn */ }
    }
  } catch (err) {
    removeSnapshotFile(stagePath);
    throw err;
  } finally {
    try { source.close(); } catch { /* already closed */ }
  }
  return stagePath;
}

function removeSnapshotFile(stagePath) {
  try { fs.unlinkSync(stagePath); } catch { /* best effort */ }
  // Backup and read-only validation can create sidecars on the staged copy.
  try { fs.unlinkSync(`${stagePath}-journal`); } catch { /* none */ }
  try { fs.unlinkSync(`${stagePath}-wal`); } catch { /* none */ }
  try { fs.unlinkSync(`${stagePath}-shm`); } catch { /* none */ }
}

/** sha256 (hex) of a file's bytes, streamed. */
async function hashFile(filePath) {
  const hash = crypto.createHash("sha256");
  await pipeline(fs.createReadStream(filePath), hash);
  return hash.digest("hex");
}

/** First `bytes` of a file (for the credential scan of a large snapshot). */
function readFileHead(filePath, bytes) {
  const fd = fs.openSync(filePath, "r");
  try {
    const buf = Buffer.alloc(bytes);
    const n = fs.readSync(fd, buf, 0, bytes, 0);
    return buf.subarray(0, n);
  } finally {
    fs.closeSync(fd);
  }
}

// --- upload gate ----------------------------------------------------------------
//
// State of the S3 restore for this process. Every upload path (change backup,
// periodic save, single-file save, final save) is a no-op unless it is "ready":
//   "unknown" — restoreWorkspace() has not been called yet
//   "pending" — restore in flight; gateway activation waits for completion
//   "ready"   — every S3 object was restored, or S3 holds nothing for this
//               namespace (new user)
//   "failed"  — the listing failed or at least one object could not be
//               restored: local state may be partial, so this container never
//               uploads (seen on staging: a same-session restart with no state
//               DBs uploaded a fresh empty database over the good S3 copy
//               within 20 s).
let _restoreState = "unknown";
let _restoreNamespace = null;
let _restoreAttempt = null;
let _fatalRestoreError = null;

function assertRestoreReady() {
  if (_restoreState !== "ready" || _fatalRestoreError) {
    throw _fatalRestoreError || new Error(`Workspace restore is ${_restoreState}`);
  }
}

function assertNamespaceBound(namespace) {
  validateNamespace(namespace);
  if (_restoreNamespace && namespace !== _restoreNamespace) {
    throw new Error("Workspace namespace does not match the restored owner");
  }
}

function assertRestoreActive(attempt) {
  if (attempt.error) throw attempt.error;
  if (_fatalRestoreError) throw _fatalRestoreError;
}

function uploadsAllowed() {
  return _restoreState === "ready";
}

let _gateWarned = false;
function warnGated(what) {
  if (_gateWarned) return;
  _gateWarned = true;
  console.warn(
    `[workspace-sync] ${what} skipped: S3 restore is ${_restoreState} — uploads are disabled until it completes` +
      (_restoreState === "failed" ? " (it failed, so they stay disabled in this container)" : ""),
  );
}

function setRestoreState(state) {
  if (state === "ready" && _fatalRestoreError) return;
  _restoreState = state;
  if (state === "ready") {
    _gateWarned = false;
    // Changes that arrived while the gate was closed are still dirty — upload them now.
    if (_changeOpts && (_dirty.size > 0 || _sweepPending)) scheduleChangeFlush(0);
  }
}

/**
 * Restore the .openclaw/ directory from S3 for a user namespace.
 * Downloads all objects under {namespace}/.openclaw/ to $HOME/.openclaw/.
 * Skips silently if no objects exist (new user).
 *
 * With `overwrite: false`, only files that are MISSING locally are downloaded
 * and files that already exist are left untouched. This is the mode for a
 * container whose session-storage mount already held some state: the mount's
 * mirror may be partial (on staging a mid-turn restart found only the
 * workspace files there — the 2 s workspace mirror had run, the 5 min state
 * mirror had not), and the state DBs, runtime-skills manifest etc. must then
 * come from S3 rather than be recreated empty by the gateway.
 *
 * Kept files are compared against S3 without downloading them: every upload
 * stores the sha256 of its bytes as object metadata (UPLOAD_HASH_METADATA_KEY),
 * a HeadObject per kept file reads it back, and a kept file whose local bytes
 * hash to the same value is seeded into the upload dedupe so the first full
 * save does not PUT it again (on staging every same-session restart re-uploaded
 * 9 unchanged files). A kept file with no stored hash, a different hash, or a
 * failed HEAD is NOT seeded and is uploaded by the first save — the local copy
 * is never assumed to match S3 without evidence.
 *
 * Opens the upload gate on success (see above); on any failure the gate stays
 * closed and the error is rethrown after being recorded.
 *
 * @param {string} namespace
 * @param {{ overwrite?: boolean }} [opts]
 * @returns {Promise<{ restored: number, kept: number, keptUnchanged: number, failed: number, hadState: boolean }>}
 *   `keptUnchanged` counts the kept files verified identical to S3 (seeded).
 */
async function restoreAbsentSeed(namespace, { lifecycle, scope, bindSeed } = {}) {
  assertNamespaceBound(namespace);
  if (_fatalRestoreError) throw _fatalRestoreError;
  if (_restoreState !== "unknown") throw new Error("Live seed restore was already attempted");
  if (!BUCKET || !_credentialProvider || typeof bindSeed !== "function") {
    throw new Error("Live seed restore requires a bound persistence owner");
  }
  const binding = lifecycle.snapshot().binding;
  const provider = _credentialProvider;
  const attempt = { controller: new AbortController(), error: null };
  _restoreNamespace = namespace;
  _restoreAttempt = attempt;
  setRestoreState("pending");
  let bound = false;
  const abort = () => {
    const error = scope.signal.reason || new Error("Live seed restore aborted");
    attempt.error = error;
    _fatalRestoreError ||= error;
    attempt.controller.abort(error);
    setRestoreState("failed");
  };
  const check = () => {
    assertRestoreActive(attempt);
    scope.checkpoint();
    lifecycle.checkpoint();
    const current = lifecycle.snapshot();
    if (_restoreAttempt !== attempt || _restoreState !== "pending" ||
        current.state !== "BINDING" || current.binding !== binding ||
        current.generation !== scope.generation || !current.resources.includes("credentials") ||
        provider !== _credentialProvider || !binding ||
        binding.actorId.replace(/:/g, "_") !== namespace ||
        process.env.USER_ID !== binding.actorId || process.env.INTERNAL_USER_ID !== binding.userId) {
      throw new Error("Live seed restore owner changed");
    }
    lifecycle.assertBinding(binding);
  };
  const ineligible = (reason) => Object.assign(new Error("Live seed restore is ineligible"), {
    code: "LIVE_SEED_INELIGIBLE", reason,
  });
  async function inspectNamespace() {
    const prefix = `${namespace}/`;
    const s3 = getS3Client();
    const tokens = new Set();
    let continuationToken;
    do {
      check();
      const params = { Bucket: BUCKET, Prefix: prefix, MaxKeys: 1000 };
      if (continuationToken !== undefined) params.ContinuationToken = continuationToken;
      const page = await s3.send(new (getS3Sdk().ListObjectsV2Command)(params),
        { abortSignal: attempt.controller.signal });
      check();
      if (!page || typeof page !== "object" ||
          typeof page.IsTruncated !== "boolean" ||
          (page.Contents !== undefined && !Array.isArray(page.Contents)) ||
          (page.CommonPrefixes !== undefined &&
            (!Array.isArray(page.CommonPrefixes) || page.CommonPrefixes.length)) ||
          (page.Prefix !== undefined && page.Prefix !== prefix)) {
        throw ineligible("listing_invalid");
      }
      const contents = page.Contents || [];
      if (page.KeyCount !== undefined &&
          (!Number.isSafeInteger(page.KeyCount) || page.KeyCount !== contents.length)) {
        throw ineligible("listing_invalid");
      }
      for (const item of contents) {
        if (!item || typeof item.Key !== "string" || !item.Key.startsWith(prefix)) {
          throw ineligible("listing_invalid");
        }
      }
      if (contents.length) throw ineligible("namespace_content");
      if (page.IsTruncated) {
        const next = page.NextContinuationToken;
        if (typeof next !== "string" || !next || tokens.has(next)) {
          throw ineligible("listing_incomplete");
        }
        tokens.add(next);
        continuationToken = next;
      } else {
        if (page.NextContinuationToken !== undefined) throw ineligible("listing_invalid");
        continuationToken = undefined;
      }
    } while (continuationToken !== undefined);
  }
  scope.signal.addEventListener("abort", abort, { once: true });
  try {
    if (scope.signal.aborted) abort();
    check();
    if ((process.env.AGENTCORE_GATEWAY_URL || "").trim()) throw ineligible("mcp_enabled");
    const ticket = stateStorage.inspectEmptySessionStorage();
    await inspectNamespace();
    check();
    stateStorage.assertEmptySessionStorage(ticket);
    // Recheck remote absence immediately before admission; this is not a distributed lock.
    await inspectNamespace();
    check();
    stateStorage.assertEmptySessionStorage(ticket);
    const proof = Object.freeze({
      generation: scope.generation, actorId: binding.actorId, userId: binding.userId,
      verified: true, restoreGateVerified: true, mcpDisabled: true,
    });
    const result = await bindSeed(proof);
    bound = result?.status === "bound" && result.bound === true;
    check();
    if (!bound) throw ineligible("owner_declined");
    if (result.generation !== scope.generation || result.userReady !== false ||
        !Number.isSafeInteger(result.pid) || result.pid <= 0) {
      throw new Error("Live seed owner acknowledgement mismatch");
    }
    // A changed mirror after bind is fatal, never a deletion of bound user state.
    stateStorage.assertEmptySessionStorage(ticket);
    const storage = stateStorage.attachEmptySessionStorage(ticket);
    check();
    setRestoreState("ready");
    return { status: "adopted", owner: result, storage, restored: 0, failed: 0, hadState: false };
  } catch (error) {
    if (error.code === "LIVE_SEED_INELIGIBLE" && !bound && !attempt.error && !_fatalRestoreError) {
      setRestoreState("ineligible");
      return { status: "ineligible", reason: error.reason };
    }
    attempt.error ||= error;
    _fatalRestoreError ||= error;
    attempt.controller.abort(error);
    setRestoreState("failed");
    throw error;
  } finally {
    scope.signal.removeEventListener("abort", abort);
  }
}

async function restoreWorkspace(namespace, { overwrite = true } = {}) {
  assertNamespaceBound(namespace);
  if (_fatalRestoreError) throw _fatalRestoreError;
  if (_restoreState === "pending") throw new Error("Workspace restore already pending");
  if (!BUCKET) throw new Error("Workspace restore requires S3_USER_FILES_BUCKET");
  _restoreNamespace = namespace;
  const attempt = { controller: new AbortController(), error: null };
  _restoreAttempt = attempt;
  setRestoreState("pending");
  try {
    const result = await runRestore(namespace, overwrite, attempt);
    assertRestoreActive(attempt);
    if (result.failed > 0) {
      setRestoreState("failed");
      console.warn(
        `[workspace-sync] Restore incomplete (${result.failed} file(s) failed) — S3 uploads disabled for this container`,
      );
      throw new Error(`Workspace restore incomplete: ${result.failed} file(s) failed`);
    } else {
      setRestoreState("ready");
    }
    return result;
  } catch (err) {
    setRestoreState("failed");
    console.warn(`[workspace-sync] Restore failed (${err.message}) — S3 uploads disabled for this container`);
    throw err;
  }
}

// --- restore downloads: bounded parallelism + retry -------------------------
//
// The restore used to GET one object at a time: ~8-10 files/s, so a
// 1,000-1,350 file state dir (the live user) spent 100-160 s in the restore on
// every cold start, on V1 and V2 alike (round-9 perf test). The listing is
// still read in full first (and validated exactly as before); then the
// downloads run through a pool of at most `concurrency` GetObjects in flight.
//
// What does NOT change:
//   - Each file is still streamed to a unique temp name next to its target,
//     sha256-verified against the object metadata, SQLite-validated, and only
//     then renamed into place (writeRestoredObject). Gzip/multipart SQLite
//     objects and legacy uncompressed objects are decoded exactly as before.
//   - A file that cannot be restored still counts as failed and fails the
//     whole restore; the upload gate opens ("ready") only after EVERY worker
//     has settled with zero failures, and the contract does not start the
//     gateway before that (awaitRestore + assertRestoreReady). The order in
//     which files land on disk is therefore invisible to OpenClaw: nothing
//     reads the state dir until the restore as a whole has succeeded. SQLite
//     -wal/-shm/-journal sidecars are never restored (SKIP_PATTERNS), so there
//     is no db/sidecar pairing to order.
//   - Missing-only mode (`overwrite: false`) decides keep-vs-download in the
//     listing pass, before any download starts.
//   - An abort (restore deadline, owner change) stops every worker: no new
//     GET starts, in-flight streams are torn down by the abort signal, and a
//     download that finishes anyway is not renamed into place
//     (assertRestoreActive() runs after the stream completes; from there to
//     the rename everything is synchronous).
//
// Retries: the SDK already retries a failed GetObject *request*; a stream that
// breaks mid-body (connection reset, premature close) or an SDK give-up on
// throttling/5xx is retried here, per file, with capped exponential backoff and
// full jitter. Anything else (403, sha256 mismatch, invalid SQLite, size cap)
// fails the file at once.
const DEFAULT_RESTORE_CONCURRENCY = 16;
const MAX_RESTORE_CONCURRENCY = 64;

function parseRestoreConcurrency(raw) {
  if (raw === undefined || raw === "") return DEFAULT_RESTORE_CONCURRENCY;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < 1) {
    console.warn(
      "[workspace-sync] Ignoring invalid WORKSPACE_RESTORE_CONCURRENCY=%s — using %d",
      String(raw).slice(0, 32), DEFAULT_RESTORE_CONCURRENCY,
    );
    return DEFAULT_RESTORE_CONCURRENCY;
  }
  return Math.min(n, MAX_RESTORE_CONCURRENCY);
}

const _restoreTuning = {
  concurrency: parseRestoreConcurrency(process.env.WORKSPACE_RESTORE_CONCURRENCY),
  retries: 3, // extra attempts per file after the first
  baseDelayMs: 250,
  maxDelayMs: 4000,
};

const RETRYABLE_ERROR_NAMES = new Set([
  "SlowDown", "Throttling", "ThrottlingException", "TooManyRequestsException",
  "RequestLimitExceeded", "RequestTimeout", "RequestTimeoutException", "TimeoutError",
  "InternalError", "ServiceUnavailable",
]);
const RETRYABLE_ERROR_CODES = new Set([
  "ECONNRESET", "ETIMEDOUT", "EPIPE", "ECONNREFUSED", "EAI_AGAIN",
  "ERR_STREAM_PREMATURE_CLOSE", "UND_ERR_SOCKET",
]);

/** Throttling, 5xx and transient network errors; never an abort. */
function isRetryableRestoreError(err) {
  if (!err || err.name === "AbortError") return false;
  if (err.$retryable) return true;
  const status = err.$metadata?.httpStatusCode;
  if (status === 429 || (Number.isInteger(status) && status >= 500 && status <= 599)) return true;
  return RETRYABLE_ERROR_NAMES.has(err.name) || RETRYABLE_ERROR_NAMES.has(err.Code) ||
    RETRYABLE_ERROR_CODES.has(err.code);
}

function restoreBackoffMs(retry) {
  const cap = Math.min(_restoreTuning.maxDelayMs, _restoreTuning.baseDelayMs * 2 ** retry);
  return Math.floor(Math.random() * cap); // full jitter
}

function abortableSleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(signal.reason); return; }
    const onAbort = () => { clearTimeout(timer); reject(signal.reason); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", onAbort); resolve(); }, ms);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

function restoreAborted(attempt) {
  return Boolean(attempt.error || _fatalRestoreError || attempt.controller.signal.aborted);
}

/** GET one object into place, retrying transient failures. */
async function downloadRestoreObject(s3, item, attempt, stats, budget) {
  for (let retry = 0; ; retry++) {
    assertRestoreActive(attempt);
    try {
      fs.mkdirSync(path.dirname(item.localFile), { recursive: true });
      const getResp = await s3.send(
        new (getS3Sdk().GetObjectCommand)({ Bucket: BUCKET, Key: item.key }),
        { abortSignal: attempt.controller.signal },
      );
      assertRestoreActive(attempt);
      return await writeRestoredObject(getResp, item.localFile, attempt, budget);
    } catch (err) {
      if (restoreAborted(attempt) || retry >= _restoreTuning.retries || !isRetryableRestoreError(err)) throw err;
      stats.retries++;
      const delay = restoreBackoffMs(retry);
      console.warn(
        "[workspace-sync] Retrying restore of %s in %d ms (attempt %d of %d): %s",
        item.relativePath, delay, retry + 2, _restoreTuning.retries + 1, err.name || err.code || "error",
      );
      await abortableSleep(delay, attempt.controller.signal);
    }
  }
}

/**
 * Run `fn` over `items` with at most `limit` in flight. Resolves only once
 * every started call has settled; rejects with the first error `fn` threw
 * (fn reports per-item failures itself and throws only to stop the pool).
 */
async function runBounded(items, limit, fn) {
  let next = 0;
  let stopError = null;
  const worker = async () => {
    while (!stopError && next < items.length) {
      const item = items[next++];
      try {
        await fn(item);
      } catch (err) {
        stopError ||= err;
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  if (stopError) throw stopError;
}

async function runRestore(namespace, overwrite, attempt) {
  const prefix = `${namespace}/${WORKSPACE_PREFIX}/`;
  const s3 = getS3Client();
  const startedAt = Date.now();
  const concurrency = _restoreTuning.concurrency;

  console.log(
    `[workspace-sync] Restoring workspace from s3://${BUCKET}/${prefix}` +
      (overwrite ? "" : " (missing files only — existing local files are kept)"),
  );

  let failed = 0;
  let hadState = false;
  let continuationToken;
  let candidates = 0;
  const keptFiles = []; // { key, relativePath } — compared against S3 after the listing
  const downloads = []; // { key, relativePath, localFile } — fetched after the listing

  // Pass 1: the complete listing. Every validation and every keep-vs-download
  // decision happens here, before any object is fetched.
  do {
    const params = {
      Bucket: BUCKET,
      Prefix: prefix,
      MaxKeys: 1000,
    };
    if (continuationToken) params.ContinuationToken = continuationToken;

    const response = await s3.send(new (getS3Sdk().ListObjectsV2Command)(params), { abortSignal: attempt.controller.signal });
    assertRestoreActive(attempt);
    const objects = response.Contents || [];

    for (const obj of objects) {
      assertRestoreActive(attempt);
      if (typeof obj.Key !== "string" || !obj.Key.startsWith(prefix)) {
        throw new Error("S3 restore returned an object outside the namespace");
      }
      const relativePath = obj.Key.slice(prefix.length);
      if (!relativePath || shouldSkip(relativePath)) continue;
      if (normalizeRelativePath(relativePath) !== relativePath) {
        throw new Error("Invalid S3 restore path");
      }
      hadState = true;
      if (++candidates > _limits.maxRestoreFiles) {
        throw new Error(
          `Workspace restore has more than ${_limits.maxRestoreFiles} objects under ${prefix} ` +
            "(WORKSPACE_RESTORE_MAX_FILES)",
        );
      }

      // Validate object size before downloading (uses ListObjectsV2 Size field).
      // obj.Size is the STORED (compressed) size. A large SQLite database is
      // uploaded as a gzip snapshot that can itself be well above the
      // non-SQLite cap, so `.sqlite` keys are listed against the SQLite cap.
      // The key suffix only widens this pre-download filter: the bytes
      // written to disk are bounded in writeRestoredObject by the object's
      // metadata (declared uncompressed size for gzip, the non-SQLite cap
      // otherwise) and the whole-restore budget.
      const listedCap = relativePath.endsWith(SQLITE_EXT) ? _limits.maxSqliteFileSize : _limits.maxFileSize;
      if (obj.Size > listedCap) {
        console.warn(
          `[workspace-sync] Skipping oversized file: ${obj.Key} (${obj.Size} stored bytes > ${listedCap})`,
        );
        failed++;
        continue;
      }

      const localFile = path.join(LOCAL_PATH, relativePath);

      // Path traversal protection: ensure resolved path stays within LOCAL_PATH
      const resolvedFile = path.resolve(localFile);
      const resolvedBase = path.resolve(LOCAL_PATH);
      if (
        !resolvedFile.startsWith(resolvedBase + path.sep) &&
        resolvedFile !== resolvedBase
      ) {
        console.warn(
          `[workspace-sync] Path traversal blocked: ${relativePath}`,
        );
        failed++;
        continue;
      }

      try {
        stateStorage.assertSafePath(LOCAL_PATH, localFile);
      } catch (err) {
        failed++;
        console.warn(`[workspace-sync] Failed to restore ${relativePath}: ${err.message}`);
        continue;
      }

      const emptySqlite = relativePath.endsWith(SQLITE_EXT) &&
        fs.existsSync(localFile) && fs.statSync(localFile).size === 0;
      if (!overwrite && fs.existsSync(localFile) && !emptySqlite) {
        // Local copy (restored from the session-storage mirror) wins. Whether
        // it matches S3 is checked below (seedKeptFiles); until proven
        // identical its hash is NOT seeded and the first save uploads it.
        if (relativePath.endsWith(SQLITE_EXT)) {
          try { stateStorage.assertSqliteFile(localFile); } catch (err) {
            failed++;
            console.warn(`[workspace-sync] Failed to validate ${relativePath}: ${err.message}`);
            continue;
          }
        }
        keptFiles.push({ key: obj.Key, relativePath });
        continue;
      }

      downloads.push({ key: obj.Key, relativePath, localFile });
    }

    if (response.IsTruncated && (!response.NextContinuationToken || response.NextContinuationToken === continuationToken)) {
      throw new Error("Incomplete S3 restore listing");
    }
    continuationToken = response.IsTruncated
      ? response.NextContinuationToken
      : undefined;
  } while (continuationToken);

  // Pass 2: the downloads, at most `concurrency` in flight.
  let totalFiles = 0;
  let totalBytes = 0;
  const stats = { retries: 0 };
  const budget = createRestoreBudget(_limits.maxRestoreBytes, _limits.restoreFreeDiskReserveBytes);
  await runBounded(downloads, concurrency, async (item) => {
    assertRestoreActive(attempt);
    try {
      const { digest, size } = await downloadRestoreObject(s3, item, attempt, stats, budget);
      // What is on disk now IS what S3 holds, so the first save after a
      // restore must not re-upload it. Without this seed every restored file
      // was PUT again by the first full save (SIGTERM / periodic) of the
      // session even when nothing had touched it.
      _uploadedHashes.set(item.relativePath, digest);
      totalFiles++;
      totalBytes += size;
    } catch (err) {
      // An abort stops the pool (and the restore); any other error fails
      // just this file, which still fails the restore as a whole.
      if (restoreAborted(attempt)) throw attempt.error || _fatalRestoreError || err;
      // Over the whole-restore budget: every remaining file would be refused
      // too, so stop the pool and fail the restore with that one reason.
      if (budget.exceeded) throw budget.exceeded;
      failed++;
      console.warn("[workspace-sync] Failed to restore %s: %s", item.relativePath, err.message);
    }
  });
  assertRestoreActive(attempt);

  const kept = keptFiles.length;
  const keptUnchanged = kept ? await seedKeptFiles(s3, keptFiles) : 0;
  assertRestoreActive(attempt);

  const elapsedS = ((Date.now() - startedAt) / 1000).toFixed(1);
  console.log(
    `[workspace-sync] Restored ${totalFiles} file(s) to ${LOCAL_PATH}` +
      (kept ? `, kept ${kept} existing local file(s) (${keptUnchanged} verified identical to S3)` : "") +
      (failed ? `, ${failed} failed` : "") +
      (hadState ? "" : " (no saved state in S3 — new user)") +
      ` — ${totalBytes} bytes, concurrency ${concurrency}, ${elapsedS} s, ${stats.retries} retries`,
  );
  return { restored: totalFiles, kept, keptUnchanged, failed, hadState };
}

// Distinguishes the temp files of concurrent downloads (and of retries).
let _restoreTmpSeq = 0;

/**
 * Bytes one restore may write to local disk, shared by the download pool.
 * Bytes of a download that fails (and is retried or dropped) are released, so
 * only what lands on disk counts. `freeDiskReserveBytes` is the free space
 * assertRestoreDiskHeadroom() keeps on the local filesystem.
 */
function createRestoreBudget(maxBytes, freeDiskReserveBytes = 0) {
  return {
    used: 0,
    exceeded: null,
    freeDiskReserveBytes,
    reserve(n) {
      if (this.exceeded) throw this.exceeded;
      if (this.used + n > maxBytes) {
        this.exceeded = new Error(
          `Workspace restore exceeds ${maxBytes} bytes on disk (WORKSPACE_RESTORE_MAX_BYTES)`,
        );
        throw this.exceeded;
      }
      this.used += n;
    },
    release(n) {
      this.used -= n;
    },
  };
}

/**
 * Fail the restore before a download is written when the local filesystem does
 * not have `needed` bytes plus the budget's free-disk reserve available. A
 * full disk would otherwise end in ENOSPC, which fails the restore and keeps
 * uploads disabled; failing here names the reason. Sets budget.exceeded so the
 * download pool stops (every remaining file would hit the same wall).
 *
 * Skipped when fs.statfsSync is unavailable or the directory cannot be read;
 * the byte budget still bounds what the restore writes.
 */
function assertRestoreDiskHeadroom(dir, needed, budget) {
  if (!budget || typeof fs.statfsSync !== "function") return;
  let stat;
  try {
    stat = fs.statfsSync(dir);
  } catch {
    return;
  }
  const avail = Number(stat.bavail) * Number(stat.bsize);
  if (!Number.isFinite(avail) || avail <= 0) return;
  const reserve = Math.max(0, budget.freeDiskReserveBytes || 0);
  if (avail < needed + reserve) {
    budget.exceeded ||= new Error(
      "Workspace restore aborted: not enough free disk space " +
        `(need ${needed} B + ${reserve} B reserve, ${avail} B available) ` +
        "(WORKSPACE_RESTORE_FREE_DISK_RESERVE_BYTES)",
    );
    throw budget.exceeded;
  }
}

/**
 * The uncompressed size a gzip object declares (UPLOAD_SIZE_METADATA_KEY,
 * written by uploadCompressedFile on every gzip object since gzip uploads
 * existed). A gzip object without a valid one is not ours and is refused.
 */
function declaredUncompressedSize(meta) {
  const raw = meta[UPLOAD_SIZE_METADATA_KEY];
  const n = typeof raw === "string" && /^\d{1,15}$/.test(raw) ? Number(raw) : NaN;
  if (!Number.isSafeInteger(n)) {
    throw new Error("gzip object has no valid uncompressed-size metadata");
  }
  return n;
}

/**
 * Stream one GetObject response to `localFile` and return the sha256 and size
 * of the bytes written. A gzip snapshot (UPLOAD_ENCODING_METADATA_KEY / the
 * ContentEncoding header — see uploadCompressedFile) is decompressed on the
 * way; objects written before compression existed have neither marker and are
 * copied as-is, so they keep restoring. Nothing is buffered whole: a 299 MB
 * database passes through in stream chunks. The bytes land in a temp file that
 * is renamed into place only once complete, and when the object carries a
 * sha256 (every upload since #115) it must match the bytes written or the
 * restore of this file fails — a torn or mis-decoded download must never be
 * handed to the gateway as its state.
 *
 * @returns {Promise<{ digest: string, size: number }>}
 */
async function writeRestoredObject(getResp, localFile, attempt, budget) {
  const meta = getResp.Metadata || {};
  const gzipped =
    getResp.ContentEncoding === GZIP_ENCODING || meta[UPLOAD_ENCODING_METADATA_KEY] === GZIP_ENCODING;
  const expected = meta[UPLOAD_HASH_METADATA_KEY];
  // A gzip body may decompress to no more than the size its uploader recorded
  // (and to no more than the per-file cap), and must decompress to exactly it.
  const declared = gzipped ? declaredUncompressedSize(meta) : null;
  const fileCap = localFile.endsWith(SQLITE_EXT) ? _limits.maxSqliteFileSize : _limits.maxFileSize;
  if (declared !== null && declared > fileCap) throw new Error("Restored object exceeds size limit");
  // A body without gzip is written byte for byte, and the uploader only ever
  // stores one that way when it is at most maxFileSize (a larger SQLite
  // database always goes up as a gzip snapshot). So a raw body is held to
  // maxFileSize whatever its key suffix: the key name is tenant-chosen and
  // must not widen the cap to the SQLite one.
  const cap = declared === null ? _limits.maxFileSize : declared;
  // Free-disk reserve, checked against what this object will write.
  const length = Number(getResp.ContentLength);
  const expectedBytes = declared !== null
    ? declared
    : (Number.isSafeInteger(length) && length >= 0 ? Math.min(length, cap) : cap);
  if (budget) assertRestoreDiskHeadroom(path.dirname(localFile), expectedBytes, budget);
  const tmpFile = `${localFile}.tmp-${process.pid}-${++_restoreTmpSeq}`;
  const hash = crypto.createHash("sha256");
  let size = 0;
  let reserved = 0;
  let written = false;
  const stages = [getResp.Body];
  if (gzipped) stages.push(zlib.createGunzip());
  stages.push(async function* (source) {
    for await (const chunk of source) {
      assertRestoreActive(attempt);
      const n = Buffer.byteLength(chunk);
      size += n;
      if (size > cap) {
        throw new Error(
          `Restored object exceeds size limit (${size} bytes so far exceeds declared size or cap of ${cap})`,
        );
      }
      if (budget) {
        budget.reserve(n);
        reserved += n;
      }
      hash.update(chunk);
      yield chunk;
    }
  });
  stages.push(fs.createWriteStream(tmpFile, { fd: fs.openSync(tmpFile, "wx", 0o600) }));
  try {
    await pipeline(...stages, { signal: attempt.controller.signal });
    assertRestoreActive(attempt);
    const digest = hash.digest("hex");
    if (declared !== null && size !== declared) {
      throw new Error(`Restored object is ${size} bytes, its metadata declares ${declared}`);
    }
    if (expected && expected !== digest) {
      throw new Error(`sha256 mismatch after download (S3 says ${expected.slice(0, 12)}…, got ${digest.slice(0, 12)}…)`);
    }
    if (localFile.endsWith(SQLITE_EXT)) stateStorage.assertSqliteFile(tmpFile);
    stateStorage.assertSafePath(LOCAL_PATH, localFile);
    fs.renameSync(tmpFile, localFile);
    written = true;
    return { digest, size };
  } finally {
    if (budget && !written) budget.release(reserved);
    removeSnapshotFile(tmpFile);
  }
}

// HeadObject calls in flight at once while verifying kept files.
const KEPT_VERIFY_CONCURRENCY = 4;

/**
 * Seed the upload dedupe for the kept files that are provably identical to S3.
 *
 * Evidence is the sha256 every upload stores as object metadata
 * (UPLOAD_HASH_METADATA_KEY), read back with one HeadObject per kept file (no
 * body transfer; ListObjectsV2 returns neither user metadata nor checksums,
 * and the bucket is SSE-KMS so the ETag is not a content MD5). The local side
 * is hashed exactly as uploadStateFile() would upload it — a SQLite database as
 * a snapshot — so a match means the next save would PUT the same bytes.
 *
 * Never fails the restore: an object without the metadata (uploaded before
 * this was stored), a mismatch, an unreadable local file or a HEAD error just
 * leaves the file unseeded, and the first save uploads it (which also writes
 * the metadata, so the next restart can verify it).
 *
 * @returns {Promise<number>} files seeded
 */
async function seedKeptFiles(s3, keptFiles) {
  let seeded = 0;
  const queue = keptFiles.slice();
  const worker = async () => {
    for (let item = queue.shift(); item; item = queue.shift()) {
      const { key, relativePath } = item;
      let remoteHash;
      try {
        const head = await s3.send(new (getS3Sdk().HeadObjectCommand)({ Bucket: BUCKET, Key: key }));
        remoteHash = head && head.Metadata ? head.Metadata[UPLOAD_HASH_METADATA_KEY] : undefined;
      } catch (err) {
        console.warn(
          `[workspace-sync] Could not compare kept ${relativePath} with S3 (${err.message}) — it will be uploaded by the next save`,
        );
        continue;
      }
      if (!remoteHash) continue; // no stored hash: no evidence, upload later
      const localHash = await localUploadHash(relativePath);
      if (localHash && localHash === remoteHash) {
        _uploadedHashes.set(relativePath, localHash);
        seeded++;
      }
    }
  };
  await Promise.all(
    Array.from({ length: Math.min(KEPT_VERIFY_CONCURRENCY, keptFiles.length) }, worker),
  );
  return seeded;
}

/**
 * sha256 of the bytes uploadStateFile() would upload for `relativePath` right
 * now (a SQLite database hashes as its snapshot), or null when that cannot be
 * determined (missing, not a file, snapshot failure).
 */
async function localUploadHash(relativePath) {
  const localFile = path.join(LOCAL_PATH, relativePath);
  try {
    if (!fs.statSync(localFile).isFile()) return null;
    if (!relativePath.endsWith(SQLITE_EXT)) return contentHash(fs.readFileSync(localFile));
    // Hash the snapshot from disk: the database may be hundreds of MB.
    const stagePath = await snapshotSqliteToFile(localFile);
    try {
      return await hashFile(stagePath);
    } finally {
      removeSnapshotFile(stagePath);
    }
  } catch {
    return null;
  }
}

/**
 * Await a workspace restore for at most `waitMs`.
 *
 * Rejects failure and timeout, aborts outstanding restore IO and keeps
 * uploads disabled after timeout.
 *
 * Injectable `timers` lets tests use fake timers instead of sleeping.
 */
async function awaitRestore(restorePromise, waitMs, { log = console, timers = globalThis } = {}) {
  if (!Number.isFinite(waitMs) || waitMs < 0) throw new Error("Invalid restore timeout");
  let timer = null;
  const attempt = _restoreAttempt;
  const fail = (err, fatal = false) => {
    if (fatal) _fatalRestoreError = err;
    if (attempt) {
      attempt.error = err;
      attempt.controller.abort(err);
    }
    setRestoreState("failed");
    return err;
  };
  const settled = Promise.resolve(restorePromise)
    .then((result) => {
      if (result?.failed > 0) throw new Error(`Workspace restore incomplete: ${result.failed} file(s) failed`);
      if (attempt) assertRestoreActive(attempt);
    })
    .catch((err) => {
      log.warn(`[contract] Workspace restore failed: ${err.message}`);
      throw fail(err);
    })
    .then(() => "restored");
  const timedOut = new Promise((_, reject) => {
    timer = timers.setTimeout(() => {
      reject(fail(new Error(`Workspace restore timed out after ${waitMs}ms`), true));
    }, waitMs);
    if (typeof timer.unref === "function") timer.unref();
  });
  try {
    return await Promise.race([settled, timedOut]);
  } finally {
    timers.clearTimeout(timer);
  }
}

/**
 * Recursively walk backup files (relative to root), excluding generated plugin skills.
 *
 * Follows directory symlinks (with a realpath cycle guard): on AgentCore the
 * state dir is local disk but `~/.openclaw/workspace` is a symlink onto the
 * session storage mount (see state-storage.js), and the workspace files behind
 * it must keep being backed up to S3.
 */
function walkDir(dir, root = dir, seen = new Set()) {
  const results = [];
  try {
    let real;
    try {
      real = fs.realpathSync(dir);
    } catch {
      real = dir;
    }
    if (seen.has(real)) return results; // symlink cycle
    seen.add(real);
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      const relativePath = path.relative(root, fullPath);
      if (isGeneratedPluginSkillPath(relativePath)) continue;
      let isDir = entry.isDirectory();
      let isFile = entry.isFile();
      if (entry.isSymbolicLink()) {
        try {
          const st = fs.statSync(fullPath);
          isDir = st.isDirectory();
          isFile = st.isFile();
        } catch {
          continue; // dangling link
        }
      }
      if (isDir) {
        results.push(...walkDir(fullPath, root, seen));
      } else if (isFile) {
        results.push(relativePath);
      }
    }
  } catch (err) {
    // Directory may not exist yet
  }
  return results;
}

// Content hash (sha256) of the bytes last uploaded per relative path, for this
// process. Lets the change-driven backup and the periodic save skip files that
// were touched but not changed — a SQLite snapshot with no new commits is
// byte-identical to the previous one, so it dedupes those too.
const _uploadedHashes = new Map();

// Object metadata key under which every upload stores the sha256 (hex) of its
// body. HeadObject returns it as Metadata[UPLOAD_HASH_METADATA_KEY]; the
// fill-missing restore uses it to tell kept files that match S3 from those
// that must be uploaded (see seedKeptFiles).
const UPLOAD_HASH_METADATA_KEY = "sha256";
// Set to GZIP_ENCODING on objects whose body is a gzip stream of the file (large
// SQLite snapshots, see uploadCompressedFile). UPLOAD_HASH_METADATA_KEY is the
// sha256 of the UNCOMPRESSED bytes in every case, so seedKeptFiles() compares
// like with like and the restore can verify what it wrote. The S3
// ContentEncoding header is set too; the restore honours either.
const UPLOAD_ENCODING_METADATA_KEY = "encoding";
const UPLOAD_SIZE_METADATA_KEY = "uncompressed-size";
const GZIP_ENCODING = "gzip";

function contentHash(content) {
  return crypto.createHash("sha256").update(content).digest("hex");
}

/**
 * Upload ONE file under $HOME/.openclaw/ to {namespace}/.openclaw/{relativePath}.
 * Shared by saveWorkspace(), saveFile() and the change-driven backup.
 *
 * `relativePath` must already be normalised and inside the state dir; skip
 * rules other than the generated plugin skills root are the caller's job.
 * Never throws on a per-file problem it can report
 * (missing, oversized, snapshot failure) — those come back as an outcome — but
 * lets S3 errors propagate so the caller can count and log them.
 *
 * Uploads of the same file never overlap: the periodic save, the change flush,
 * saveFile() and the final save all come through here, and a call that arrives
 * while that file is uploading waits for it and then runs its own pass (callers
 * that arrive meanwhile share that one pass). Two snapshots of one database in
 * flight at once could otherwise finish out of order and leave the older one
 * in S3.
 *
 * @returns {Promise<"uploaded"|"unchanged"|"skipped"|"missing">}
 */
function uploadStateFile(namespace, relativePath, { force = false } = {}) {
  let lane = _uploadLanes.get(relativePath);
  if (!lane) {
    lane = { running: null, queued: null };
    _uploadLanes.set(relativePath, lane);
  }
  const start = (opts) => {
    const run = uploadStateFileNow(namespace, relativePath, opts);
    lane.running = run;
    const settle = () => {
      if (lane.running === run) lane.running = null;
      if (!lane.running && !lane.queued && _uploadLanes.get(relativePath) === lane) {
        _uploadLanes.delete(relativePath);
      }
    };
    run.then(settle, settle);
    return run;
  };
  if (!lane.running) return start({ force });
  if (lane.queued) {
    lane.queued.force ||= force;
    return lane.queued.promise;
  }
  const queued = { force, promise: null };
  lane.queued = queued;
  queued.promise = lane.running.then(() => {}, () => {}).then(() => {
    lane.queued = null;
    return start({ force: queued.force });
  });
  return queued.promise;
}

// relativePath -> { running: Promise|null, queued: { force, promise }|null }
const _uploadLanes = new Map();

async function uploadStateFileNow(namespace, relativePath, { force = false } = {}) {
  assertNamespaceBound(namespace);
  if (!uploadsAllowed()) { warnGated("File upload"); return "skipped"; }
  if (normalizeRelativePath(relativePath) !== relativePath) throw new Error("Invalid state file path");
  if (isGeneratedPluginSkillPath(relativePath)) return "skipped";
  const localFile = path.join(LOCAL_PATH, relativePath);
  stateStorage.assertSafePath(LOCAL_PATH, localFile);
  let stat;
  try {
    stat = fs.statSync(localFile);
  } catch {
    _uploadedHashes.delete(relativePath);
    return "missing"; // deleted between change and upload — nothing to do
  }
  if (!stat.isFile()) return "missing";
  const key = `${namespace}/${WORKSPACE_PREFIX}/${relativePath}`;
  if (relativePath.endsWith(SQLITE_EXT)) {
    return uploadSqliteFile(key, localFile, relativePath, stat, force);
  }
  if (stat.size > _limits.maxFileSize) {
    console.warn(
      `[workspace-sync] Skipping ${relativePath} (${stat.size} bytes > ${_limits.maxFileSize})`,
    );
    return "skipped";
  }
  const content = fs.readFileSync(localFile);
  const hash = contentHash(content);
  if (!force && _uploadedHashes.get(relativePath) === hash) return "unchanged";
  scanForCredentials(relativePath, content);
  await putObject(key, content, hash);
  _uploadedHashes.set(relativePath, hash);
  return "uploaded";
}

/**
 * Credential detection: warn (but don't block) when secrets are found.
 * Exempt only the root-level native API key store — user made a conscious choice.
 * Match exact relative path (not just basename) to prevent bypass via subdirectories.
 */
function scanForCredentials(relativePath, content) {
  if (relativePath === CREDENTIAL_SCAN_EXEMPT) return;
  const detected = detectCredentials(content);
  if (detected) {
    console.warn(
      `[workspace-sync] WARNING: Potential credential detected in ${relativePath} ` +
      `(pattern: ${detected}). File will still be uploaded to S3.`,
    );
  }
}

/** Single PUT of an in-memory body, with its sha256 as object metadata. */
async function putObject(key, content, hash) {
  assertRestoreReady();
  await getS3Client().send(
    new (getS3Sdk().PutObjectCommand)({
      Bucket: BUCKET,
      Key: key,
      Body: content,
      Metadata: { [UPLOAD_HASH_METADATA_KEY]: hash },
    }),
  );
}

/**
 * Upload a live SQLite database as a consistent snapshot, never the raw file.
 *
 * A snapshot up to `maxFileSize` is read into memory and PUT uncompressed —
 * the pre-existing path, unchanged. A larger one (the imported 1.x history is
 * ~299 MB) is hashed from disk, then streamed gzip-compressed through
 * uploadCompressedFile(); the uncompressed sha256 is stored as metadata either
 * way, so the hash-skip and the kept-file check (#115) work across both. Above
 * `maxSqliteFileSize` (WORKSPACE_SYNC_MAX_SQLITE_BYTES) the database is skipped
 * and the log says so at every attempt: its changes are not being backed up.
 */
async function uploadSqliteFile(key, localFile, relativePath, stat, force) {
  const cap = _limits.maxSqliteFileSize;
  const warnCap = (bytes) => console.warn(
    `[workspace-sync] Skipping ${relativePath}: SQLite database is ${bytes} bytes, above the ` +
      `${cap}-byte backup cap (WORKSPACE_SYNC_MAX_SQLITE_BYTES) — its changes are NOT backed up to S3`,
  );
  if (stat.size > cap) {
    warnCap(stat.size);
    return "skipped";
  }
  let stagePath;
  try {
    stagePath = await snapshotSqliteToFile(localFile);
  } catch (err) {
    console.warn(
      `[workspace-sync] Skipping ${relativePath}: SQLite snapshot failed (${err.message})`,
    );
    return "skipped";
  }
  try {
    const size = fs.statSync(stagePath).size;
    if (size > cap) {
      warnCap(size);
      return "skipped";
    }
    if (size <= _limits.maxFileSize) {
      const content = fs.readFileSync(stagePath);
      const hash = contentHash(content);
      if (!force && _uploadedHashes.get(relativePath) === hash) return "unchanged";
      scanForCredentials(relativePath, content);
      await putObject(key, content, hash);
      _uploadedHashes.set(relativePath, hash);
      return "uploaded";
    }
    // Large database: never in memory whole.
    const statAtHash = fs.statSync(stagePath);
    const hash = await hashFile(stagePath);
    if (!force && _uploadedHashes.get(relativePath) === hash) return "unchanged";
    scanForCredentials(relativePath, readFileHead(stagePath, 64 * 1024));
    const started = Date.now();
    // Pass the size/mtime captured at hash time so uploadCompressedFile can
    // confirm the staged file still matches right before CompleteMultipartUpload.
    // That guarantees the body uploaded to S3 is the exact bytes we hashed,
    // closing the hash-then-reread window (if some other code path mutates or
    // unlinks this staging file the upload is aborted rather than publishing
    // an object whose body disagrees with its sha256 metadata).
    const { bytes, parts } = await uploadCompressedFile(key, stagePath, {
      hash,
      size,
      verify: { size: statAtHash.size, mtimeMs: statAtHash.mtimeMs },
    });
    _uploadedHashes.set(relativePath, hash);
    _lastLargeUploadAt.set(relativePath, Date.now());
    console.log(
      `[workspace-sync] Uploaded ${relativePath} as a gzip snapshot: ${size} -> ${bytes} bytes ` +
        `(${parts} part(s), ${Date.now() - started}ms)`,
    );
    return "uploaded";
  } finally {
    removeSnapshotFile(stagePath);
  }
}

/**
 * Stream `filePath` gzip-compressed to S3 as a multipart upload (a single PUT
 * when the compressed body fits in one part). Memory is bounded by ~2 parts
 * whatever the file size, and gzip runs in the libuv threadpool, so the
 * contract server keeps answering while a large snapshot goes up. Object
 * metadata records the uncompressed sha256 and size and the gzip encoding
 * (plus the ContentEncoding header) so the restore knows to decompress.
 *
 * @returns {Promise<{ bytes: number, parts: number }>} compressed bytes sent
 */
async function uploadCompressedFile(key, filePath, { hash, size, verify } = {}) {
  assertRestoreReady();
  const s3 = getS3Client();
  const sdk = getS3Sdk();
  // Confirm the staged bytes we hashed are still on disk unchanged. If another
  // concurrent save path mutated or unlinked the staging file between hashFile()
  // and here, abort rather than publishing an S3 object whose body contradicts
  // the sha256 we store in its metadata (which would make the next restore
  // reject the object with "sha256 mismatch after download").
  const assertStageUnchanged = () => {
    if (!verify) return;
    let cur;
    try {
      cur = fs.statSync(filePath);
    } catch (err) {
      throw new Error(
        `Staged snapshot ${path.basename(filePath)} disappeared before upload completed: ${err.message}`,
      );
    }
    if (cur.size !== verify.size || cur.mtimeMs !== verify.mtimeMs) {
      throw new Error(
        `Staged snapshot ${path.basename(filePath)} changed under us before upload completed ` +
          `(size ${verify.size}->${cur.size}, mtimeMs ${verify.mtimeMs}->${cur.mtimeMs}); aborting`,
      );
    }
  };
  const base = {
    Bucket: BUCKET,
    Key: key,
    ContentEncoding: GZIP_ENCODING,
    Metadata: {
      [UPLOAD_HASH_METADATA_KEY]: hash,
      [UPLOAD_ENCODING_METADATA_KEY]: GZIP_ENCODING,
      [UPLOAD_SIZE_METADATA_KEY]: String(size),
    },
  };
  const gzip = zlib.createGzip({ level: 6 });
  const source = fs.createReadStream(filePath);
  source.on("error", (err) => gzip.destroy(err));
  source.pipe(gzip);

  let uploadId = null;
  const completed = [];
  let pending = [];
  let pendingBytes = 0;
  let total = 0;
  const sendPart = async (body) => {
    assertRestoreReady();
    getS3Client();
    if (!uploadId) {
      const created = await s3.send(new sdk.CreateMultipartUploadCommand(base));
      uploadId = created.UploadId;
    }
    const partNumber = completed.length + 1;
    const res = await s3.send(new sdk.UploadPartCommand({
      Bucket: BUCKET,
      Key: key,
      UploadId: uploadId,
      PartNumber: partNumber,
      Body: body,
      ContentLength: body.length,
    }));
    completed.push({ ETag: res.ETag, PartNumber: partNumber });
  };
  try {
    for await (const chunk of gzip) {
      pending.push(chunk);
      pendingBytes += chunk.length;
      total += chunk.length;
      if (pendingBytes >= _limits.uploadPartSize) {
        const body = Buffer.concat(pending);
        pending = [];
        pendingBytes = 0;
        await sendPart(body); // the gzip stream is paused meanwhile (backpressure)
      }
    }
    const tail = Buffer.concat(pending);
    // The staged bytes we hashed must still be the exact bytes we just streamed.
    assertStageUnchanged();
    assertRestoreReady();
    getS3Client();
    if (!uploadId) {
      await s3.send(new sdk.PutObjectCommand({ ...base, Body: tail }));
      return { bytes: total, parts: 1 };
    }
    if (tail.length > 0) await sendPart(tail);
    assertStageUnchanged();
    assertRestoreReady();
    getS3Client();
    await s3.send(new sdk.CompleteMultipartUploadCommand({
      Bucket: BUCKET,
      Key: key,
      UploadId: uploadId,
      MultipartUpload: { Parts: completed },
    }));
    return { bytes: total, parts: completed.length };
  } catch (err) {
    if (uploadId) {
      // Leave no incomplete upload behind (they are billed until aborted).
      try {
        await s3.send(new sdk.AbortMultipartUploadCommand({ Bucket: BUCKET, Key: key, UploadId: uploadId }));
      } catch (abortErr) {
        console.warn(`[workspace-sync] Could not abort multipart upload of ${key}: ${abortErr.message}`);
      }
    }
    throw err;
  }
}

/**
 * Save the .openclaw/ directory to S3 for a user namespace.
 * Uploads all files under $HOME/.openclaw/ to {namespace}/.openclaw/.
 * Skips files matching SKIP_PATTERNS, files over their size cap (see
 * _limits), and files whose content this process already uploaded (see
 * _uploadedHashes).
 */
async function saveWorkspace(namespace) {
  if (namespace) assertNamespaceBound(namespace);
  if (!BUCKET || !namespace) return;
  if (!uploadsAllowed()) {
    warnGated("Full save");
    return;
  }

  const files = walkDir(LOCAL_PATH);
  // A full save uploads every changed SQLite snapshot, so the change backup's
  // SQLite throttle window restarts here.
  if (files.some(isSqlitePath)) _lastSqliteFlushAt = Date.now();

  let uploaded = 0;
  let unchanged = 0;
  let skipped = 0;

  for (const relativePath of files) {
    if (shouldSkip(relativePath)) {
      skipped++;
      continue;
    }
    try {
      const outcome = await uploadStateFile(namespace, relativePath);
      if (outcome === "uploaded") uploaded++;
      else if (outcome === "unchanged") unchanged++;
      else skipped++;
    } catch (err) {
      console.warn(
        `[workspace-sync] Failed to save ${relativePath}: ${err.message}`,
      );
    }
  }

  console.log(
    `[workspace-sync] Saved ${uploaded} file(s), ${unchanged} unchanged, skipped ${skipped}`,
  );
}

/**
 * Normalise a path relative to the state dir for S3 use, or return null when it
 * escapes the state dir.
 */
function normalizeRelativePath(relativePath) {
  if (typeof relativePath !== "string" || relativePath.includes("\0")) return null;
  const normalized = path.posix.normalize(String(relativePath).split(path.sep).join("/"));
  if (
    !normalized ||
    normalized === "." ||
    normalized === ".." ||
    normalized.startsWith("../") ||
    path.posix.isAbsolute(normalized)
  ) {
    return null;
  }
  return normalized;
}

/**
 * Back up ONE file under $HOME/.openclaw/ to S3 right away, outside the
 * periodic save. For small state that must survive a cold start that happens
 * before the next periodic save (the runtime-skills manifest). Same key layout
 * and skip rules as saveWorkspace(); resolves false when nothing was uploaded
 * (missing, skipped, or content unchanged since the last upload).
 */
async function saveFile(namespace, relativePath) {
  if (namespace) assertNamespaceBound(namespace);
  if (!BUCKET || !namespace || !relativePath) return false;
  const normalized = normalizeRelativePath(relativePath);
  if (!normalized) {
    console.warn(`[workspace-sync] Refusing to save path outside the state dir: ${relativePath}`);
    return false;
  }
  if (shouldSkip(normalized)) return false;
  if (!uploadsAllowed()) {
    warnGated(`Save of ${normalized}`);
    return false;
  }
  const outcome = await uploadStateFile(namespace, normalized);
  if (outcome !== "uploaded") return false;
  console.log(`[workspace-sync] Saved ${normalized}`);
  return true;
}

// Periodic save state
let _saveInterval = null;
// Backup mode: when session storage is primary, S3 sync becomes a cold backup
let _backupMode = false;
// Backup interval: 30 minutes (vs 5 minutes for primary sync)
const BACKUP_INTERVAL_MS = 30 * 60 * 1000;

/**
 * Enable or disable backup mode.
 * In backup mode, periodic saves use a longer interval (30 min)
 * since session storage handles primary persistence.
 */
function setBackupMode(enabled) {
  _backupMode = enabled;
  console.log(`[workspace-sync] Backup mode ${enabled ? "enabled" : "disabled"} (session storage is ${enabled ? "primary" : "unavailable"})`);
}

/**
 * Start periodic workspace saves.
 */
function startPeriodicSave(namespace, intervalMs) {
  assertNamespaceBound(namespace);
  const defaultInterval = parseInt(process.env.WORKSPACE_SYNC_INTERVAL_MS || "300000", 10);
  const interval = intervalMs || (_backupMode ? BACKUP_INTERVAL_MS : defaultInterval);
  if (_saveInterval) clearInterval(_saveInterval);

  _saveInterval = setInterval(() => {
    saveWorkspace(namespace).catch((err) => {
      console.warn(`[workspace-sync] Periodic save failed: ${err.message}`);
    });
  }, interval);

  console.log(
    `[workspace-sync] Periodic save started (every ${interval / 1000}s, mode=${_backupMode ? "backup" : "primary"})`,
  );
}


// --- change-driven backup -----------------------------------------------------
//
// AgentCore stops a session's container (idle timeout, max lifetime,
// StopRuntimeSession) without a usable grace period: on staging the process
// was gone well under 5 s after SIGTERM, and idle terminations showed no
// SIGTERM at all. Whatever is not in S3 by then is lost at the next cold start.
// So instead of relying on the 30-minute periodic save or the SIGTERM save,
// watch the state dir and upload each changed file a few seconds after it
// changes — debounced, bounded, through the same skip rules and SQLite snapshot
// path as saveWorkspace().

const DEFAULT_CHANGE_DEBOUNCE_MS = 5000;
const DEFAULT_CHANGE_MAX_WAIT_MS = 30000;
const DEFAULT_CHANGE_MAX_FILES_PER_FLUSH = 100;
const DEFAULT_CHANGE_POLL_INTERVAL_MS = 60000;
const CHANGE_UPLOAD_CONCURRENCY = 4;
// SQLite-only throttle. The gateway writes a lease heartbeat into
// state/openclaw.sqlite every 30 s, so an IDLE session would otherwise upload a
// ~4 MB snapshot ~116 times an hour (staging, round 1). When the only dirty
// files are SQLite databases, their snapshots go up at most once per this
// interval. A change to any non-SQLite file flushes the pending snapshots with
// it, and the SIGTERM flush / stopChangeBackup() / the periodic full save
// always include them. Cost: on a stop with no SIGTERM (idle timeout), up to
// this much SQLite-only history (session rows that no memory/workspace write
// accompanied) can be lost.
const DEFAULT_SQLITE_MIN_INTERVAL_MS = 5 * 60 * 1000;
// Large SQLite databases (snapshot above _limits.maxFileSize, i.e. the ones
// that go up gzip'd) have their own, longer cadence, measured per file from
// its last upload. The imported 1.x history is ~299 MB (~45-75 MB gzip'd):
// at the 5 min cadence an active session would push ~0.55-0.9 GB/h into a
// versioned bucket with no noncurrent-version expiry; 10 min halves that.
// Cost: up to this much 2.0 history lost on a stop with no SIGTERM. The idle
// timeout is 15 min, so a held snapshot is still uploaded before an idle
// stop; only an abrupt kill (max lifetime, host loss) can lose the window.
// Non-SQLite changes do NOT pull a held large snapshot along with them (they
// do for small ones); the SIGTERM flush and the 30-min full save always do.
const DEFAULT_LARGE_SQLITE_MIN_INTERVAL_MS = 10 * 60 * 1000;
// SQLite writes land in the sidecars first (WAL mode); a change to any of them
// means the main database has new commits to snapshot.
const SQLITE_SIDECAR_RE = /^(.*\.sqlite)-(wal|shm|journal)$/;
// Our own / state-storage's atomic-copy temp files.
const TMP_COPY_RE = /\.tmp-\d+$/;
// OpenClaw's scratch dir at the state-dir root (gateway locks, staging files).
const STATE_TMP_DIR = "tmp";

let _changeOpts = null; // { namespace, debounceMs, maxWaitMs, maxFilesPerFlush }
let _changeWatcher = null;
let _changePollTimer = null;
let _changeDebounce = null;
let _changeDeadline = null;
let _dirty = new Set();
let _sweepPending = false;
let _flushInFlight = null;
let _lastFlushHadFailures = false;
// When SQLite snapshots were last included in an upload pass (change flush or
// full save). 0 = never, so the first SQLite change of a session is not held.
let _lastSqliteFlushAt = 0;
// Per large database: when its gzip snapshot was last uploaded (any path).
const _lastLargeUploadAt = new Map();

function isSqlitePath(rel) {
  return rel.endsWith(SQLITE_EXT);
}

/** A SQLite database whose upload would take the gzip path (see uploadSqliteFile). */
function isLargeSqlite(rel) {
  try {
    return fs.statSync(path.join(LOCAL_PATH, rel)).size > _limits.maxFileSize;
  } catch {
    return false;
  }
}

/**
 * Map a change event path (relative to the state dir) to the file that should
 * be uploaded for it, or null when the change is not worth a backup: outside
 * the state dir, a transient/staging file, or a SKIP_PATTERNS match. SQLite
 * sidecar events map onto their main database.
 */
function changeTarget(relativePath) {
  let rel = normalizeRelativePath(relativePath);
  if (!rel || isGeneratedPluginSkillPath(rel)) return null;
  const sidecar = rel.match(SQLITE_SIDECAR_RE);
  if (sidecar) rel = sidecar[1];
  const parts = rel.split("/");
  if (parts[0] === STATE_TMP_DIR) return null;
  if (parts.slice(0, -1).some((dir) => stateStorage.isTransientDir(dir))) return null;
  const name = parts[parts.length - 1];
  if (TMP_COPY_RE.test(name) || name.endsWith(".sqlite-snapshot")) return null;
  if (shouldSkip(rel)) return null;
  return rel;
}

function clearChangeTimers() {
  if (_changeDebounce) { clearTimeout(_changeDebounce); _changeDebounce = null; }
  if (_changeDeadline) { clearTimeout(_changeDeadline); _changeDeadline = null; }
}

function scheduleChangeFlush(delayMs) {
  if (!_changeOpts) return;
  const { debounceMs, maxWaitMs } = _changeOpts;
  if (_changeDebounce) clearTimeout(_changeDebounce);
  _changeDebounce = setTimeout(() => {
    flushPendingSaves("change").catch(() => {});
  }, delayMs === undefined ? debounceMs : delayMs);
  if (typeof _changeDebounce.unref === "function") _changeDebounce.unref();
  if (!_changeDeadline) {
    // A steady stream of changes must not postpone the upload forever. (A
    // deliberate SQLite hold is longer than maxWaitMs; it is its own deadline.)
    _changeDeadline = setTimeout(() => {
      flushPendingSaves("deadline").catch(() => {});
    }, Math.max(maxWaitMs, delayMs || 0));
    if (typeof _changeDeadline.unref === "function") _changeDeadline.unref();
  }
}

/**
 * Record that `filename` (relative to the state dir, as fs.watch reports it)
 * changed. A null filename (the platform could not say which) requests a full
 * sweep of the state dir at the next flush.
 */
function markChanged(filename) {
  if (!_changeOpts) return;
  if (filename === null || filename === undefined || filename === "") {
    _sweepPending = true;
  } else {
    const target = changeTarget(filename.toString());
    if (!target) return;
    _dirty.add(target);
  }
  scheduleChangeFlush();
}

async function uploadBatch(namespace, batch, result) {
  let next = 0;
  const worker = async () => {
    while (next < batch.length) {
      const rel = batch[next++];
      try {
        result[await uploadStateFile(namespace, rel)]++;
      } catch (err) {
        result.failed++;
        _dirty.add(rel); // retried by the next flush (with a longer delay) or the periodic save
        console.warn(`[workspace-sync] Change backup failed for ${rel}: ${err.message}`);
      }
    }
  };
  await Promise.all(
    Array.from({ length: Math.min(CHANGE_UPLOAD_CONCURRENCY, batch.length) }, worker),
  );
}

/**
 * One upload pass over the dirty set. Non-SQLite files go first; SQLite
 * snapshots follow only when `force` is set, the SQLite throttle interval has
 * elapsed, or this pass actually changed something in S3 (a non-SQLite upload
 * or a failed attempt at one) — otherwise they stay dirty and `held`/`holdMs`
 * say for how long. `deferred` counts files left over by maxFilesPerFlush.
 */
async function runChangeFlush(reason, { force = false } = {}) {
  const { namespace, maxFilesPerFlush, sqliteMinIntervalMs, largeSqliteMinIntervalMs } = _changeOpts;
  const result = { uploaded: 0, unchanged: 0, skipped: 0, missing: 0, failed: 0, deferred: 0, held: 0, holdMs: 0 };
  if (!uploadsAllowed()) {
    // Keep the dirty set (and the sweep request) for when the restore
    // completes; setRestoreState("ready") schedules the flush.
    warnGated(`Change backup (${reason})`);
    result.deferred = _dirty.size;
    return result;
  }
  if (_sweepPending) {
    _sweepPending = false;
    for (const rel of walkDir(LOCAL_PATH)) {
      const target = changeTarget(rel);
      if (target) _dirty.add(target);
    }
  }
  const plain = [];
  const sqlite = [];
  for (const rel of _dirty) (isSqlitePath(rel) ? sqlite : plain).push(rel);

  const plainBatch = plain.slice(0, maxFilesPerFlush);
  for (const rel of plainBatch) _dirty.delete(rel);
  if (plainBatch.length) await uploadBatch(namespace, plainBatch, result);

  const now = Date.now();
  const dueAt = _lastSqliteFlushAt + sqliteMinIntervalMs;
  const includeSqlite = force || now >= dueAt || result.uploaded > 0 || result.failed > 0;
  const ready = [];
  let holdUntil = Infinity;
  for (const rel of sqlite) {
    if (!force && isLargeSqlite(rel)) {
      // Large databases follow their own cadence (see DEFAULT_LARGE_SQLITE_MIN_INTERVAL_MS).
      const largeDueAt = (_lastLargeUploadAt.get(rel) || 0) + largeSqliteMinIntervalMs;
      if (now >= largeDueAt) ready.push(rel);
      else holdUntil = Math.min(holdUntil, largeDueAt);
    } else if (includeSqlite) {
      ready.push(rel);
    } else {
      holdUntil = Math.min(holdUntil, dueAt);
    }
  }
  if (ready.length) {
    const sqliteBatch = ready.slice(0, Math.max(0, maxFilesPerFlush - plainBatch.length));
    if (sqliteBatch.length) {
      _lastSqliteFlushAt = now;
      for (const rel of sqliteBatch) _dirty.delete(rel);
      await uploadBatch(namespace, sqliteBatch, result);
    }
  }
  const held = sqlite.length - ready.length;
  if (held) {
    result.held = held;
    result.holdMs = Math.max(1, holdUntil - now);
  }
  result.deferred = _dirty.size - result.held;

  _lastFlushHadFailures = result.failed > 0;
  if (result.uploaded || result.failed || result.deferred) {
    console.log(
      `[workspace-sync] Change backup (${reason}): ${result.uploaded} uploaded, ${result.unchanged} unchanged, ` +
        `${result.skipped} skipped, ${result.failed} failed, ${result.deferred} deferred` +
        (result.held ? `, ${result.held} SQLite snapshot(s) held for ${Math.round(result.holdMs / 1000)}s` : ""),
    );
  }
  return result;
}

/**
 * Upload everything marked dirty now (coalescing onto an in-flight flush).
 * `force` includes SQLite snapshots regardless of the throttle (SIGTERM, stop).
 * Resolves with the flush result, or null when the change backup is not
 * running. Never rejects.
 */
function flushPendingSaves(reason = "flush", { force = false } = {}) {
  clearChangeTimers();
  if (!_changeOpts) return Promise.resolve(null);
  if (_flushInFlight) return _flushInFlight;
  _flushInFlight = runChangeFlush(reason, { force })
    .catch((err) => {
      console.warn(`[workspace-sync] Change backup (${reason}) failed: ${err.message}`);
      return null;
    })
    .then((result) => {
      _flushInFlight = null;
      if (_changeOpts && uploadsAllowed() && (_dirty.size > 0 || _sweepPending)) {
        // Leftovers: deferred by maxFilesPerFlush, failed, held by the SQLite
        // throttle, or changed mid-flush. Failed ones wait the longer
        // maxWaitMs so a persistent S3 error does not turn into a hot retry
        // loop; held snapshots wait until the throttle interval has elapsed.
        let delay;
        if (_lastFlushHadFailures) delay = _changeOpts.maxWaitMs;
        else if (result && result.held > 0 && result.deferred === 0 && !_sweepPending) delay = result.holdMs;
        scheduleChangeFlush(delay);
      }
      return result;
    });
  return _flushInFlight;
}

function startChangePollFallback(intervalMs) {
  if (_changePollTimer) clearInterval(_changePollTimer);
  _changePollTimer = setInterval(() => {
    _sweepPending = true;
    scheduleChangeFlush(0);
  }, intervalMs);
  if (typeof _changePollTimer.unref === "function") _changePollTimer.unref();
}

/**
 * Watch $HOME/.openclaw/ and upload changed files to S3 shortly after they
 * change: `debounceMs` after the last change, at most `maxWaitMs` after the
 * first change of a burst, at most `maxFilesPerFlush` files per flush. Uses
 * fs.watch({ recursive: true }); when that is unavailable, falls back to a
 * full sweep every `fallbackIntervalMs`. Idempotent — calling again replaces
 * the previous watcher but keeps the pending dirty set.
 *
 * @returns {{ mode: "watch" | "poll" | "none" }}
 */
function startChangeBackup(namespace, opts = {}) {
  if (namespace) assertNamespaceBound(namespace);
  closeChangeWatcher();
  if (!BUCKET || !namespace) {
    console.log("[workspace-sync] No bucket or namespace — change backup disabled");
    return { mode: "none" };
  }
  const debounceMs = opts.debounceMs ||
    parseInt(process.env.WORKSPACE_SYNC_CHANGE_DEBOUNCE_MS || String(DEFAULT_CHANGE_DEBOUNCE_MS), 10);
  const maxWaitMs = opts.maxWaitMs ||
    parseInt(process.env.WORKSPACE_SYNC_CHANGE_MAX_WAIT_MS || String(DEFAULT_CHANGE_MAX_WAIT_MS), 10);
  const sqliteMinIntervalMs = opts.sqliteMinIntervalMs !== undefined
    ? opts.sqliteMinIntervalMs
    : parseInt(process.env.WORKSPACE_SYNC_SQLITE_MIN_INTERVAL_MS || String(DEFAULT_SQLITE_MIN_INTERVAL_MS), 10);
  const largeSqliteMinIntervalMs = opts.largeSqliteMinIntervalMs !== undefined
    ? opts.largeSqliteMinIntervalMs
    : parseInt(process.env.WORKSPACE_SYNC_LARGE_SQLITE_MIN_INTERVAL_MS || String(DEFAULT_LARGE_SQLITE_MIN_INTERVAL_MS), 10);
  _changeOpts = {
    namespace,
    debounceMs,
    maxWaitMs: Math.max(maxWaitMs, debounceMs),
    maxFilesPerFlush: opts.maxFilesPerFlush || DEFAULT_CHANGE_MAX_FILES_PER_FLUSH,
    sqliteMinIntervalMs: Math.max(0, sqliteMinIntervalMs),
    // Never shorter than the small-database cadence.
    largeSqliteMinIntervalMs: Math.max(0, sqliteMinIntervalMs, largeSqliteMinIntervalMs),
  };
  const fallbackIntervalMs = opts.fallbackIntervalMs || DEFAULT_CHANGE_POLL_INTERVAL_MS;
  if (opts.initialSweep === true) markChanged(null);
  try {
    fs.mkdirSync(LOCAL_PATH, { recursive: true });
    _changeWatcher = fs.watch(LOCAL_PATH, { recursive: true, persistent: false }, (_event, filename) => {
      markChanged(filename);
    });
    _changeWatcher.on("error", (err) => {
      console.warn(`[workspace-sync] Change watcher error: ${err.message} — falling back to polling`);
      try { _changeWatcher.close(); } catch { /* already closed */ }
      _changeWatcher = null;
      startChangePollFallback(fallbackIntervalMs);
    });
    console.log(
      `[workspace-sync] Change backup started on ${LOCAL_PATH} (debounce ${debounceMs}ms, max wait ${_changeOpts.maxWaitMs}ms, ` +
        `SQLite-only changes at most every ${_changeOpts.sqliteMinIntervalMs / 1000}s, ` +
        `databases over ${_limits.maxFileSize} bytes at most every ${_changeOpts.largeSqliteMinIntervalMs / 1000}s, gzip'd)`,
    );
    return { mode: "watch" };
  } catch (err) {
    console.warn(
      `[workspace-sync] fs.watch unavailable (${err.message}) — sweeping ${LOCAL_PATH} every ${fallbackIntervalMs / 1000}s`,
    );
    startChangePollFallback(fallbackIntervalMs);
    return { mode: "poll" };
  }
}

function closeChangeWatcher() {
  if (_changeWatcher) {
    try { _changeWatcher.close(); } catch { /* already closed */ }
    _changeWatcher = null;
  }
  if (_changePollTimer) {
    clearInterval(_changePollTimer);
    _changePollTimer = null;
  }
  clearChangeTimers();
}

/**
 * Stop watching and upload whatever is still pending. Runs a full (hash-deduped)
 * sweep of the state dir rather than trusting the dirty set alone, so writes
 * whose watch events have not been delivered yet — the gateway's last writes
 * before it exits — are caught too. Changes that arrive while that flush runs
 * get one more flush. Bounded by the caller's shutdown budget, never rejects.
 */
async function stopChangeBackup(reason = "stop") {
  if (!_changeOpts) {
    closeChangeWatcher();
    return null;
  }
  _sweepPending = true;
  let result = await flushPendingSaves(reason, { force: true });
  closeChangeWatcher();
  if (uploadsAllowed() && (_dirty.size > 0 || _sweepPending)) {
    result = await flushPendingSaves(`${reason}-2`, { force: true });
  }
  clearChangeTimers();
  _changeOpts = null;
  return result;
}

/**
 * Stop periodic saves and the change backup, then do a final full save.
 */
async function cleanup(namespace) {
  if (_saveInterval) {
    clearInterval(_saveInterval);
    _saveInterval = null;
  }
  await stopChangeBackup("cleanup");
  if (namespace) {
    console.log("[workspace-sync] Final save before shutdown...");
    await saveWorkspace(namespace);
  }
}

module.exports = {
  restoreAbsentSeed,
  assertRestoreReady,
  restoreWorkspace,
  awaitRestore,
  saveWorkspace,
  saveFile,
  startPeriodicSave,
  startChangeBackup,
  stopChangeBackup,
  flushPendingSaves,
  cleanup,
  configureCredentials,
  configureCredentialProvider,
  setBackupMode,
  getS3Client,
  // Exported for testing
  shouldSkip,
  walkDir,
  detectCredentials,
  snapshotSqlite,
  changeTarget,
  markChanged,
  uploadStateFile,
  CREDENTIAL_SCAN_EXEMPT,
  DEFAULT_SQLITE_MIN_INTERVAL_MS,
  DEFAULT_LARGE_SQLITE_MIN_INTERVAL_MS,
  UPLOAD_HASH_METADATA_KEY,
  UPLOAD_ENCODING_METADATA_KEY,
  UPLOAD_SIZE_METADATA_KEY,
  getRestoreState: () => _restoreState,
  // Tests exercise the save paths without a restore; production code must
  // only ever open the gate through restoreWorkspace().
  _setRestoreStateForTests: setRestoreState,
  // Tests shrink the size thresholds so the large-file paths run on small
  // fixtures; production values come from the constants above / the env.
  _setLimitsForTests: (overrides) => Object.assign(_limits, overrides),
  _getLimitsForTests: () => ({ ..._limits }),
  // Tests pin the restore concurrency / retry backoff; production values come
  // from WORKSPACE_RESTORE_CONCURRENCY and the constants above.
  _setRestoreTuningForTests: (overrides) => Object.assign(_restoreTuning, overrides),
  getRestoreTuning: () => ({ ..._restoreTuning }),
  isRetryableRestoreError,
  DEFAULT_RESTORE_CONCURRENCY,
  MAX_RESTORE_CONCURRENCY,
};
