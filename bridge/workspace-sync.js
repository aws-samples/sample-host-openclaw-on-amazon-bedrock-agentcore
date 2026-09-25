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

// --- upload gate ----------------------------------------------------------------
//
// State of the S3 restore for this process. Every upload path (change backup,
// periodic save, single-file save, final save) is a no-op unless it is "ready":
//   "unknown" — restoreWorkspace() has not been called yet
//   "pending" — restore in flight (the gateway may already be running if the
//               bounded pre-spawn wait timed out)
//   "ready"   — every S3 object was restored, or S3 holds nothing for this
//               namespace (new user), or there is no bucket/namespace at all
//   "failed"  — the listing failed or at least one object could not be
//               restored: local state may be partial, so this container never
//               uploads (seen on staging: a same-session restart with no state
//               DBs uploaded a fresh empty database over the good S3 copy
//               within 20 s).
let _restoreState = "unknown";

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
async function restoreWorkspace(namespace, { overwrite = true } = {}) {
  if (!BUCKET || !namespace) {
    console.log("[workspace-sync] No bucket or namespace — skipping restore");
    setRestoreState("ready"); // nothing to protect: uploads are no-ops without a bucket
    return { restored: 0, kept: 0, keptUnchanged: 0, failed: 0, hadState: false };
  }
  setRestoreState("pending");
  try {
    const result = await runRestore(namespace, overwrite);
    if (result.failed > 0) {
      setRestoreState("failed");
      console.warn(
        `[workspace-sync] Restore incomplete (${result.failed} file(s) failed) — S3 uploads disabled for this container`,
      );
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

async function runRestore(namespace, overwrite) {
  const prefix = `${namespace}/${WORKSPACE_PREFIX}/`;
  const s3 = getS3Client();

  console.log(
    `[workspace-sync] Restoring workspace from s3://${BUCKET}/${prefix}` +
      (overwrite ? "" : " (missing files only — existing local files are kept)"),
  );

  let totalFiles = 0;
  let failed = 0;
  let hadState = false;
  let continuationToken;
  const keptFiles = []; // { key, relativePath } — compared against S3 after the listing

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
      hadState = true;

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

      if (!overwrite && fs.existsSync(localFile)) {
        // Local copy (restored from the session-storage mirror) wins. Whether
        // it matches S3 is checked below (seedKeptFiles); until proven
        // identical its hash is NOT seeded and the first save uploads it.
        keptFiles.push({ key: obj.Key, relativePath });
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
        failed++;
        console.warn(
          `[workspace-sync] Failed to restore ${relativePath}: ${err.message}`,
        );
      }
    }

    continuationToken = response.IsTruncated
      ? response.NextContinuationToken
      : undefined;
  } while (continuationToken);

  const kept = keptFiles.length;
  const keptUnchanged = kept ? await seedKeptFiles(s3, keptFiles) : 0;

  console.log(
    `[workspace-sync] Restored ${totalFiles} file(s) to ${LOCAL_PATH}` +
      (kept ? `, kept ${kept} existing local file(s) (${keptUnchanged} verified identical to S3)` : "") +
      (failed ? `, ${failed} failed` : "") +
      (hadState ? "" : " (no saved state in S3 — new user)"),
  );
  return { restored: totalFiles, kept, keptUnchanged, failed, hadState };
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
    const content = relativePath.endsWith(SQLITE_EXT)
      ? await snapshotSqlite(localFile)
      : fs.readFileSync(localFile);
    return contentHash(content);
  } catch {
    return null;
  }
}

/**
 * Await a workspace restore for at most `waitMs`.
 *
 * Resolves as soon as the restore settles (a failure is logged, not thrown).
 * If the restore is still running after `waitMs`, logs a warning and resolves
 * anyway so the caller can start the gateway. The wait timer is cleared once
 * the restore settles, so the warning only fires on a genuine timeout (it
 * used to fire on every boot, ~45s after a ~1s restore).
 *
 * Injectable `timers` lets tests use fake timers instead of sleeping.
 */
async function awaitRestore(restorePromise, waitMs, { log = console, timers = globalThis } = {}) {
  let timer = null;
  const settled = Promise.resolve(restorePromise)
    .catch((err) => {
      log.warn(`[contract] Workspace restore failed: ${err.message}`);
    })
    .then(() => "restored");
  const timedOut = new Promise((resolve) => {
    timer = timers.setTimeout(() => {
      log.warn(
        `[contract] Workspace restore still running after ${waitMs}ms — starting gateway anyway`,
      );
      resolve("timeout");
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

// Object metadata key under which every upload stores the sha256 (hex) of its
// body. HeadObject returns it as Metadata[UPLOAD_HASH_METADATA_KEY]; the
// fill-missing restore uses it to tell kept files that match S3 from those
// that must be uploaded (see seedKeptFiles).
const UPLOAD_HASH_METADATA_KEY = "sha256";

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
      Metadata: { [UPLOAD_HASH_METADATA_KEY]: hash },
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

function isSqlitePath(rel) {
  return rel.endsWith(SQLITE_EXT);
}

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
  const { namespace, maxFilesPerFlush, sqliteMinIntervalMs } = _changeOpts;
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
  if (sqlite.length) {
    if (includeSqlite) {
      const sqliteBatch = sqlite.slice(0, Math.max(0, maxFilesPerFlush - plainBatch.length));
      if (sqliteBatch.length) {
        _lastSqliteFlushAt = now;
        for (const rel of sqliteBatch) _dirty.delete(rel);
        await uploadBatch(namespace, sqliteBatch, result);
      }
    } else {
      result.held = sqlite.length;
      result.holdMs = Math.max(1, dueAt - now);
    }
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
  _changeOpts = {
    namespace,
    debounceMs,
    maxWaitMs: Math.max(maxWaitMs, debounceMs),
    maxFilesPerFlush: opts.maxFilesPerFlush || DEFAULT_CHANGE_MAX_FILES_PER_FLUSH,
    sqliteMinIntervalMs: Math.max(0, sqliteMinIntervalMs),
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
      `[workspace-sync] Change backup started on ${LOCAL_PATH} (debounce ${debounceMs}ms, max wait ${_changeOpts.maxWaitMs}ms, ` +
        `SQLite-only changes at most every ${_changeOpts.sqliteMinIntervalMs / 1000}s)`,
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
  UPLOAD_HASH_METADATA_KEY,
  getRestoreState: () => _restoreState,
  // Tests exercise the save paths without a restore; production code must
  // only ever open the gate through restoreWorkspace().
  _setRestoreStateForTests: setRestoreState,
};
