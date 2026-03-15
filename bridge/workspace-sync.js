/**
 * Workspace Sync — .openclaw/ directory persistence to/from S3.
 *
 * Restores a user's .openclaw/ directory from S3 on session start, and
 * periodically saves it back. Uses the same S3 bucket and client pattern
 * as the proxy's workspace files (readUserFileFromS3/writeUserFileToS3).
 *
 * Namespace format: {actorId.replace(/:/g, "_")} (e.g., "telegram_123456789")
 * S3 prefix: {namespace}/.openclaw/
 * Local path: $HOME/.openclaw/ (defaults to /root/.openclaw/)
 */

const fs = require("fs");
const path = require("path");

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
];
const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10MB

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
        fs.writeFileSync(localFile, Buffer.concat(chunks));
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
 */
function walkDir(dir, root = dir) {
  const results = [];
  try {
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        results.push(...walkDir(fullPath, root));
      } else if (entry.isFile()) {
        results.push(path.relative(root, fullPath));
      }
    }
  } catch (err) {
    // Directory may not exist yet
  }
  return results;
}

/**
 * Save the .openclaw/ directory to S3 for a user namespace.
 * Uploads all files under $HOME/.openclaw/ to {namespace}/.openclaw/.
 * Skips files matching SKIP_PATTERNS and files > MAX_FILE_SIZE.
 */
async function saveWorkspace(namespace) {
  if (!BUCKET || !namespace) return;

  const prefix = `${namespace}/${WORKSPACE_PREFIX}/`;
  const s3 = getS3Client();
  const files = walkDir(LOCAL_PATH);

  let uploaded = 0;
  let skipped = 0;

  for (const relativePath of files) {
    if (shouldSkip(relativePath)) {
      skipped++;
      continue;
    }

    const localFile = path.join(LOCAL_PATH, relativePath);
    try {
      const stat = fs.statSync(localFile);
      if (stat.size > MAX_FILE_SIZE) {
        console.warn(
          `[workspace-sync] Skipping ${relativePath} (${stat.size} bytes > ${MAX_FILE_SIZE})`,
        );
        skipped++;
        continue;
      }

      const content = fs.readFileSync(localFile);

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

      await s3.send(
        new (getS3Sdk().PutObjectCommand)({
          Bucket: BUCKET,
          Key: `${prefix}${relativePath}`,
          Body: content,
        }),
      );
      uploaded++;
    } catch (err) {
      console.warn(
        `[workspace-sync] Failed to save ${relativePath}: ${err.message}`,
      );
    }
  }

  console.log(`[workspace-sync] Saved ${uploaded} file(s), skipped ${skipped}`);
}

// Periodic save state
let _saveInterval = null;

/**
 * Start periodic workspace saves.
 */
function startPeriodicSave(namespace, intervalMs) {
  const interval =
    intervalMs ||
    parseInt(process.env.WORKSPACE_SYNC_INTERVAL_MS || "300000", 10);
  if (_saveInterval) clearInterval(_saveInterval);

  _saveInterval = setInterval(() => {
    saveWorkspace(namespace).catch((err) => {
      console.warn(`[workspace-sync] Periodic save failed: ${err.message}`);
    });
  }, interval);

  console.log(
    `[workspace-sync] Periodic save started (every ${interval / 1000}s)`,
  );
}

/**
 * Stop periodic saves and do a final save.
 */
async function cleanup(namespace) {
  if (_saveInterval) {
    clearInterval(_saveInterval);
    _saveInterval = null;
  }
  if (namespace) {
    console.log("[workspace-sync] Final save before shutdown...");
    await saveWorkspace(namespace);
  }
}

module.exports = {
  restoreWorkspace,
  saveWorkspace,
  startPeriodicSave,
  cleanup,
  configureCredentials,
  getS3Client,
  // Exported for testing
  shouldSkip,
  detectCredentials,
  CREDENTIAL_SCAN_EXEMPT,
};
