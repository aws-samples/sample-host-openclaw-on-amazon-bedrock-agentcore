/**
 * Workspace Sync — .openclaw/ directory persistence to/from S3.
 *
 * Restores a user's .openclaw/ directory from S3 on session start, and saves
 * it back: a full periodic save, a change-driven save of just the files that
 * changed (debounced, see startChangeBackup()), and a best-effort final save
 * on SIGTERM. Uses the same S3 bucket and client pattern as the proxy's
 * workspace files (readUserFileFromS3/writeUserFileToS3).
 *
 * Namespace format: {actorId.replace(/:/g, "_")} (e.g., "telegram_123456789")
 * S3 prefix: {namespace}/.openclaw/
 * Local path: $HOME/.openclaw/ (defaults to /root/.openclaw/). On AgentCore this
 * is a local directory whose `workspace/` entry is a symlink onto the session
 * storage mount (state-storage.js); the walk follows it so workspace files are
 * still backed up.
 */

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const stateStorage = require("./state-storage");

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
];
const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10MB

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

function getS3Client() {
  if (!_s3Client) {
    const { S3Client } = getS3Sdk();
    const opts = { region: process.env.AWS_REGION };
    if (_scopedCredentials) {
      opts.credentials = {
        accessKeyId: _scopedCredentials.accessKeyId,
        secretAccessKey: _scopedCredentials.secretAccessKey,
        sessionToken: _scopedCredentials.sessionToken,
      };
    }
    _s3Client = new S3Client(opts);
  }
  return _s3Client;
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
  _scopedCredentials = {
    accessKeyId: credentials.accessKeyId,
    secretAccessKey: credentials.secretAccessKey,
    sessionToken: credentials.sessionToken,
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
  const sqlite = getNodeSqlite();
  if (!sqlite || typeof sqlite.backup !== "function") {
    throw new Error("node:sqlite backup API unavailable on this Node runtime");
  }
  const stagePath = path.join(
    path.dirname(dbPath),
    `.${path.basename(dbPath)}.${process.pid}-${Date.now()}.sqlite-snapshot`,
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
  } finally {
    try { source.close(); } catch { /* already closed */ }
  }
  try {
    return fs.readFileSync(stagePath);
  } finally {
    try { fs.unlinkSync(stagePath); } catch { /* best effort */ }
    // backup() may leave a -journal next to the staged copy on some builds
    try { fs.unlinkSync(`${stagePath}-journal`); } catch { /* none */ }
  }
}

/**
 * Restore the .openclaw/ directory from S3 for a user namespace.
 * Downloads all objects under {namespace}/.openclaw/ to $HOME/.openclaw/.
 * Skips silently if no objects exist (new user).
 */
async function restoreWorkspace(namespace) {
  if (!BUCKET || !namespace) {
    console.log("[workspace-sync] No bucket or namespace — skipping restore");
    return;
  }

  const prefix = `${namespace}/${WORKSPACE_PREFIX}/`;
  const s3 = getS3Client();

  console.log(
    `[workspace-sync] Restoring workspace from s3://${BUCKET}/${prefix}`,
  );

  let totalFiles = 0;
  let continuationToken;

  do {
    const params = {
      Bucket: BUCKET,
      Prefix: prefix,
      MaxKeys: 1000,
    };
    if (continuationToken) params.ContinuationToken = continuationToken;

    const response = await s3.send(new (getS3Sdk().ListObjectsV2Command)(params));
    const objects = response.Contents || [];

    for (const obj of objects) {
      const relativePath = obj.Key.slice(prefix.length);
      if (!relativePath || shouldSkip(relativePath)) continue;

      // Validate object size before downloading (uses ListObjectsV2 Size field)
      if (obj.Size > MAX_FILE_SIZE) {
        console.warn(
          `[workspace-sync] Skipping oversized file: ${obj.Key} (${obj.Size} bytes)`,
        );
        continue;
      }

      const localFile = path.join(LOCAL_PATH, relativePath);
      const localDir = path.dirname(localFile);

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
        continue;
      }

      try {
        fs.mkdirSync(localDir, { recursive: true });
        const getResp = await s3.send(
          new (getS3Sdk().GetObjectCommand)({ Bucket: BUCKET, Key: obj.Key }),
        );
        const chunks = [];
        for await (const chunk of getResp.Body) {
          chunks.push(chunk);
        }
        const content = Buffer.concat(chunks);
        fs.writeFileSync(localFile, content);
        // What is on disk now IS what S3 holds, so the first save after a
        // restore must not re-upload it. Without this seed every restored file
        // was PUT again by the first full save (SIGTERM / periodic) of the
        // session even when nothing had touched it.
        _uploadedHashes.set(relativePath, contentHash(content));
        totalFiles++;
      } catch (err) {
        console.warn(
          `[workspace-sync] Failed to restore ${relativePath}: ${err.message}`,
        );
      }
    }

    continuationToken = response.IsTruncated
      ? response.NextContinuationToken
      : undefined;
  } while (continuationToken);

  console.log(
    `[workspace-sync] Restored ${totalFiles} file(s) to ${LOCAL_PATH}`,
  );
}

/**
 * Recursively walk a directory and return all file paths (relative to root).
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
        results.push(path.relative(root, fullPath));
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

function contentHash(content) {
  return crypto.createHash("sha256").update(content).digest("hex");
}

/**
 * Upload ONE file under $HOME/.openclaw/ to {namespace}/.openclaw/{relativePath}.
 * Shared by saveWorkspace(), saveFile() and the change-driven backup.
 *
 * `relativePath` must already be normalised and inside the state dir; skip
 * rules are the caller's job. Never throws on a per-file problem it can report
 * (missing, oversized, snapshot failure) — those come back as an outcome — but
 * lets S3 errors propagate so the caller can count and log them.
 *
 * @returns {Promise<"uploaded"|"unchanged"|"skipped"|"missing">}
 */
async function uploadStateFile(namespace, relativePath, { force = false } = {}) {
  const localFile = path.join(LOCAL_PATH, relativePath);
  let stat;
  try {
    stat = fs.statSync(localFile);
  } catch {
    _uploadedHashes.delete(relativePath);
    return "missing"; // deleted between change and upload — nothing to do
  }
  if (!stat.isFile()) return "missing";
  if (stat.size > MAX_FILE_SIZE) {
    console.warn(
      `[workspace-sync] Skipping ${relativePath} (${stat.size} bytes > ${MAX_FILE_SIZE})`,
    );
    return "skipped";
  }

  let content;
  if (relativePath.endsWith(SQLITE_EXT)) {
    // Live database: upload a consistent snapshot, never the raw file.
    try {
      content = await snapshotSqlite(localFile);
    } catch (err) {
      console.warn(
        `[workspace-sync] Skipping ${relativePath}: SQLite snapshot failed (${err.message})`,
      );
      return "skipped";
    }
    if (content.length > MAX_FILE_SIZE) {
      console.warn(
        `[workspace-sync] Skipping ${relativePath} snapshot (${content.length} bytes > ${MAX_FILE_SIZE})`,
      );
      return "skipped";
    }
  } else {
    content = fs.readFileSync(localFile);
  }

  const hash = contentHash(content);
  if (!force && _uploadedHashes.get(relativePath) === hash) return "unchanged";

  // Credential detection: warn (but don't block) when secrets are found.
  // Exempt only the root-level native API key store — user made a conscious choice.
  // Match exact relative path (not just basename) to prevent bypass via subdirectories.
  if (relativePath !== CREDENTIAL_SCAN_EXEMPT) {
    const detected = detectCredentials(content);
    if (detected) {
      console.warn(
        `[workspace-sync] WARNING: Potential credential detected in ${relativePath} ` +
        `(pattern: ${detected}). File will still be uploaded to S3.`,
      );
    }
  }

  await getS3Client().send(
    new (getS3Sdk().PutObjectCommand)({
      Bucket: BUCKET,
      Key: `${namespace}/${WORKSPACE_PREFIX}/${relativePath}`,
      Body: content,
    }),
  );
  _uploadedHashes.set(relativePath, hash);
  return "uploaded";
}

/**
 * Save the .openclaw/ directory to S3 for a user namespace.
 * Uploads all files under $HOME/.openclaw/ to {namespace}/.openclaw/.
 * Skips files matching SKIP_PATTERNS, files > MAX_FILE_SIZE, and files whose
 * content this process already uploaded (see _uploadedHashes).
 */
async function saveWorkspace(namespace) {
  if (!BUCKET || !namespace) return;

  const files = walkDir(LOCAL_PATH);

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
  if (!BUCKET || !namespace || !relativePath) return false;
  const normalized = normalizeRelativePath(relativePath);
  if (!normalized) {
    console.warn(`[workspace-sync] Refusing to save path outside the state dir: ${relativePath}`);
    return false;
  }
  if (shouldSkip(normalized)) return false;
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

/**
 * Map a change event path (relative to the state dir) to the file that should
 * be uploaded for it, or null when the change is not worth a backup: outside
 * the state dir, a transient/staging file, or a SKIP_PATTERNS match. SQLite
 * sidecar events map onto their main database.
 */
function changeTarget(relativePath) {
  let rel = normalizeRelativePath(relativePath);
  if (!rel) return null;
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
    // A steady stream of changes must not postpone the upload forever.
    _changeDeadline = setTimeout(() => {
      flushPendingSaves("deadline").catch(() => {});
    }, maxWaitMs);
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

async function runChangeFlush(reason) {
  const { namespace, maxFilesPerFlush } = _changeOpts;
  if (_sweepPending) {
    _sweepPending = false;
    for (const rel of walkDir(LOCAL_PATH)) {
      const target = changeTarget(rel);
      if (target) _dirty.add(target);
    }
  }
  const batch = [..._dirty].slice(0, maxFilesPerFlush);
  for (const rel of batch) _dirty.delete(rel);
  const result = { uploaded: 0, unchanged: 0, skipped: 0, missing: 0, failed: 0, deferred: _dirty.size };
  if (batch.length === 0) return result;

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
  _lastFlushHadFailures = result.failed > 0;
  if (result.uploaded || result.failed || result.deferred) {
    console.log(
      `[workspace-sync] Change backup (${reason}): ${result.uploaded} uploaded, ${result.unchanged} unchanged, ` +
        `${result.skipped} skipped, ${result.failed} failed, ${result.deferred} deferred`,
    );
  }
  return result;
}

/**
 * Upload everything marked dirty now (coalescing onto an in-flight flush).
 * Resolves with the flush result, or null when the change backup is not
 * running. Never rejects.
 */
function flushPendingSaves(reason = "flush") {
  clearChangeTimers();
  if (!_changeOpts) return Promise.resolve(null);
  if (_flushInFlight) return _flushInFlight;
  _flushInFlight = runChangeFlush(reason)
    .catch((err) => {
      console.warn(`[workspace-sync] Change backup (${reason}) failed: ${err.message}`);
      return null;
    })
    .finally(() => {
      _flushInFlight = null;
      if (_changeOpts && (_dirty.size > 0 || _sweepPending)) {
        // Leftovers: deferred by maxFilesPerFlush, failed, or changed mid-flush.
        // Failed ones wait the longer maxWaitMs so a persistent S3 error does
        // not turn into a hot retry loop.
        scheduleChangeFlush(_lastFlushHadFailures ? _changeOpts.maxWaitMs : undefined);
      }
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
  closeChangeWatcher();
  if (!BUCKET || !namespace) {
    console.log("[workspace-sync] No bucket or namespace — change backup disabled");
    return { mode: "none" };
  }
  const debounceMs = opts.debounceMs ||
    parseInt(process.env.WORKSPACE_SYNC_CHANGE_DEBOUNCE_MS || String(DEFAULT_CHANGE_DEBOUNCE_MS), 10);
  const maxWaitMs = opts.maxWaitMs ||
    parseInt(process.env.WORKSPACE_SYNC_CHANGE_MAX_WAIT_MS || String(DEFAULT_CHANGE_MAX_WAIT_MS), 10);
  _changeOpts = {
    namespace,
    debounceMs,
    maxWaitMs: Math.max(maxWaitMs, debounceMs),
    maxFilesPerFlush: opts.maxFilesPerFlush || DEFAULT_CHANGE_MAX_FILES_PER_FLUSH,
  };
  const fallbackIntervalMs = opts.fallbackIntervalMs || DEFAULT_CHANGE_POLL_INTERVAL_MS;
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
      `[workspace-sync] Change backup started on ${LOCAL_PATH} (debounce ${debounceMs}ms, max wait ${_changeOpts.maxWaitMs}ms)`,
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
  let result = await flushPendingSaves(reason);
  closeChangeWatcher();
  if (_dirty.size > 0 || _sweepPending) {
    result = await flushPendingSaves(`${reason}-2`);
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
  restoreWorkspace,
  saveWorkspace,
  saveFile,
  startPeriodicSave,
  startChangeBackup,
  stopChangeBackup,
  flushPendingSaves,
  cleanup,
  configureCredentials,
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
};
