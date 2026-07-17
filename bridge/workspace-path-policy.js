"use strict";

const DEFAULT_WORKSPACE_LIMITS = Object.freeze({
  maxEntries: 10_000,
  maxFileBytes: 64 * 1024 * 1024,
  maxGenerationBytes: 512 * 1024 * 1024,
  maxManifestBytes: 8 * 1024 * 1024,
  maxPathBytes: 1024,
  maxSegmentBytes: 255,
});

const EXACT_DATABASES = Object.freeze({
  "state/openclaw.sqlite": Object.freeze({ kind: "sqlite", role: "global" }),
  "agents/main/agent/openclaw-agent.sqlite": Object.freeze({
    kind: "sqlite",
    role: "agent",
    agentId: "main",
  }),
});

const DATABASE_SIDECAR_SUFFIXES = ["-wal", "-shm", "-journal"];
const TRANSIENT_SUFFIXES = [".sock", ".pid", ".tmp", ".lock"];
const TRANSIENT_TREES = [
  "agents/main/sessions",
  "sessions",
  "logs",
  "delivery-queue",
  "session-delivery-queue",
];
const STRUCTURAL_DIRECTORIES = new Set([
  "state",
  "agents",
  "agents/main",
  "agents/main/agent",
]);
const MANAGED_READY_MARKER = ".personal-operator-ready.json";
const MANAGED_AGENTS_FILE = "workspace/AGENTS.md";

const SENSITIVE_SEGMENTS = new Set([
  ".aws",
  ".docker",
  ".gnupg",
  ".kube",
  ".secrets",
  ".ssh",
  "api-keys",
  "api_keys",
  "credential",
  "credentials",
  "secret",
  "secrets",
  "token",
  "tokens",
]);
const SENSITIVE_BASENAMES = new Set([
  ".netrc",
  ".npmrc",
  ".pypirc",
  "credentials.json",
  "id_dsa",
  "id_ed25519",
  "id_ecdsa",
  "id_rsa",
  "user-api-keys.json",
]);
const SENSITIVE_EXTENSIONS = [
  ".jks",
  ".key",
  ".keystore",
  ".p12",
  ".pem",
  ".pfx",
];
const DATABASE_BASENAME_PATTERN =
  /\.(?:sqlite3?|db3?)(?:-(?:wal|shm|journal))?$/iu;

const SECRET_PATTERNS = [
  /(?:AKIA|ASIA)[0-9A-Z]{16}/,
  /-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----/,
  /(?:^|[^A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9_-]{20,}/m,
  /xox[baprs]-[A-Za-z0-9-]{10,}/,
  /gh[pousr]_[A-Za-z0-9]{20,}/,
  /glpat-[A-Za-z0-9_-]{20,}/,
  /AIza[0-9A-Za-z_-]{35}/,
  /(?:^|[^A-Za-z0-9])sk_live_[0-9A-Za-z]{16,}/m,
  /\bnpm_[A-Za-z0-9]{20,}\b/,
  /\b\d{8,10}:[A-Za-z0-9_-]{35}\b/,
  /\bauthorization\s*:\s*bearer\s+[A-Za-z0-9_+./=-]{20,}/i,
  /\b(?:api[_-]?key|aws[_-]?secret[_-]?access[_-]?key|client[_-]?secret|password|private[_-]?key|secret[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|token)\s*[:=]\s*["']?[A-Za-z0-9_+./=-]{16,}/i,
];

class WorkspacePathPolicyError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "WorkspacePathPolicyError";
    this.code = code;
    this.details = Object.freeze({ ...details });
  }
}

function resolveWorkspaceLimits(overrides = {}) {
  if (
    overrides === null ||
    typeof overrides !== "object" ||
    Array.isArray(overrides)
  ) {
    throw new WorkspacePathPolicyError(
      "WORKSPACE_LIMITS_INVALID",
      "workspace limits must be a plain object",
    );
  }
  const unknown = Object.keys(overrides).filter(
    (key) => !Object.hasOwn(DEFAULT_WORKSPACE_LIMITS, key),
  );
  if (unknown.length > 0) {
    throw new WorkspacePathPolicyError(
      "WORKSPACE_LIMITS_INVALID",
      "workspace limits contain an unknown ceiling",
      { keys: unknown },
    );
  }
  const resolved = { ...DEFAULT_WORKSPACE_LIMITS };
  for (const [key, value] of Object.entries(overrides)) {
    if (
      !Number.isSafeInteger(value) ||
      value < 0 ||
      value > DEFAULT_WORKSPACE_LIMITS[key]
    ) {
      throw new WorkspacePathPolicyError(
        "WORKSPACE_LIMITS_INVALID",
        "workspace limits may only tighten frozen production ceilings",
        { key },
      );
    }
    resolved[key] = value;
  }
  return Object.freeze(resolved);
}

function fail(code, message, relativePath) {
  throw new WorkspacePathPolicyError(code, message, { path: relativePath });
}

function validateRelativePath(relativePath, limits = DEFAULT_WORKSPACE_LIMITS) {
  const effectiveLimits = resolveWorkspaceLimits(limits);
  if (
    typeof relativePath !== "string" ||
    !relativePath.isWellFormed() ||
    relativePath.length === 0 ||
    relativePath.startsWith("/") ||
    relativePath.includes("\\") ||
    /[\p{Cc}\p{Cf}]/u.test(relativePath)
  ) {
    fail("WORKSPACE_PATH_INVALID", "workspace path must be canonical relative UTF-8", relativePath);
  }

  const segments = relativePath.split("/");
  if (
    segments.some((segment) => segment === "" || segment === "." || segment === "..") ||
    /^[A-Za-z]:/u.test(segments[0])
  ) {
    fail("WORKSPACE_PATH_INVALID", "workspace path contains an unsafe segment", relativePath);
  }

  if (
    Buffer.byteLength(relativePath, "utf8") > effectiveLimits.maxPathBytes ||
    segments.some(
      (segment) =>
        Buffer.byteLength(segment, "utf8") > effectiveLimits.maxSegmentBytes,
    )
  ) {
    fail("WORKSPACE_PATH_LIMIT", "workspace path exceeds its UTF-8 byte limit", relativePath);
  }
  return relativePath;
}

function isBelow(relativePath, root) {
  return relativePath === root || relativePath.startsWith(`${root}/`);
}

function isDatabaseSidecar(relativePath) {
  for (const databasePath of Object.keys(EXACT_DATABASES)) {
    if (DATABASE_SIDECAR_SUFFIXES.some((suffix) => relativePath === `${databasePath}${suffix}`)) {
      return true;
    }
  }
  return false;
}

function hasTransientSuffixSegment(relativePath) {
  return relativePath
    .split("/")
    .some((segment) =>
      TRANSIENT_SUFFIXES.some((suffix) => segment.endsWith(suffix)),
    );
}

function isSpecial(stat) {
  return [
    "isSymbolicLink",
    "isSocket",
    "isFIFO",
    "isBlockDevice",
    "isCharacterDevice",
  ].some((method) => typeof stat?.[method] === "function" && stat[method]());
}

function assertSafeFileType(relativePath, stat) {
  if (!stat || isSpecial(stat)) {
    fail("WORKSPACE_FILE_TYPE_INVALID", "symlinks and special files are forbidden", relativePath);
  }
  const isFile = typeof stat.isFile === "function" && stat.isFile();
  const isDirectory = typeof stat.isDirectory === "function" && stat.isDirectory();
  if (!isFile && !isDirectory) {
    fail("WORKSPACE_FILE_TYPE_INVALID", "workspace entries must be regular files or directories", relativePath);
  }
  if (isFile && (!Number.isSafeInteger(stat.nlink) || stat.nlink !== 1)) {
    fail("WORKSPACE_FILE_TYPE_INVALID", "hardlinked workspace files are forbidden", relativePath);
  }
  return { isFile, isDirectory };
}

function isSensitiveWorkspacePath(relativePath) {
  const segments = relativePath.split("/").slice(1);
  const lower = segments.map((segment) => segment.toLowerCase());
  const basename = lower.at(-1);
  return (
    lower.some(
      (segment) =>
        SENSITIVE_SEGMENTS.has(segment) ||
        segment === ".env" ||
        segment.startsWith(".env."),
    ) ||
    SENSITIVE_BASENAMES.has(basename) ||
    (basename !== undefined &&
      (DATABASE_BASENAME_PATTERN.test(basename) ||
        SENSITIVE_EXTENSIONS.some((extension) => basename.endsWith(extension))))
  );
}

function detectSecret(content) {
  const text = content.toString("utf8");
  return SECRET_PATTERNS.find((pattern) => pattern.test(text))?.source || null;
}

function validateDurableEntryPath(kind, relativePath, limits = DEFAULT_WORKSPACE_LIMITS) {
  validateRelativePath(relativePath, limits);
  if (kind === "sqlite") {
    if (!Object.hasOwn(EXACT_DATABASES, relativePath)) {
      fail(
        "WORKSPACE_PATH_UNSUPPORTED",
        "SQLite manifest entry is not one of the frozen database roles",
        relativePath,
      );
    }
    return relativePath;
  }
  if (kind !== "file" || !relativePath.startsWith("workspace/")) {
    fail(
      "WORKSPACE_PATH_UNSUPPORTED",
      "manifest entry is outside the durable allowlist",
      relativePath,
    );
  }
  if (
    relativePath === MANAGED_AGENTS_FILE ||
    relativePath.endsWith(`/${MANAGED_READY_MARKER}`) ||
    hasTransientSuffixSegment(relativePath) ||
    isSensitiveWorkspacePath(relativePath)
  ) {
    fail(
      "WORKSPACE_SENSITIVE_PATH",
      "manifest entry is transient or sensitive",
      relativePath,
    );
  }
  return relativePath;
}

class WorkspacePathPolicy {
  constructor({ limits = DEFAULT_WORKSPACE_LIMITS } = {}) {
    this.limits = resolveWorkspaceLimits(limits);
  }

  classify(relativePath, { stat, content } = {}) {
    validateRelativePath(relativePath, this.limits);
    const { isFile, isDirectory } = assertSafeFileType(relativePath, stat);

    if (relativePath === MANAGED_READY_MARKER || relativePath === MANAGED_AGENTS_FILE) {
      if (!isFile) {
        fail(
          "WORKSPACE_FILE_TYPE_INVALID",
          "managed workspace exclusions must be regular files",
          relativePath,
        );
      }
      return Object.freeze({ action: "exclude" });
    }
    if (relativePath.endsWith(`/${MANAGED_READY_MARKER}`)) {
      fail("WORKSPACE_PATH_UNSUPPORTED", "managed ready marker is valid only at the root", relativePath);
    }

    if (
      TRANSIENT_TREES.some((tree) => isBelow(relativePath, tree)) ||
      isDatabaseSidecar(relativePath) ||
      hasTransientSuffixSegment(relativePath)
    ) {
      return Object.freeze({ action: "exclude" });
    }

    if (isDirectory) {
      if (relativePath === "workspace" || relativePath.startsWith("workspace/")) {
        if (isSensitiveWorkspacePath(relativePath)) {
          fail("WORKSPACE_SENSITIVE_PATH", "sensitive workspace path is forbidden", relativePath);
        }
        return Object.freeze({ action: "traverse" });
      }
      if (STRUCTURAL_DIRECTORIES.has(relativePath)) {
        return Object.freeze({ action: "traverse" });
      }
      fail("WORKSPACE_PATH_UNSUPPORTED", "directory is outside the durable allowlist", relativePath);
    }

    const database = EXACT_DATABASES[relativePath];
    if (database) {
      if (stat.size > this.limits.maxFileBytes) {
        fail("WORKSPACE_FILE_LIMIT", "database exceeds the per-file limit", relativePath);
      }
      return Object.freeze({ action: "persist", ...database });
    }

    if (!relativePath.startsWith("workspace/")) {
      fail("WORKSPACE_PATH_UNSUPPORTED", "file is outside the durable allowlist", relativePath);
    }
    if (isSensitiveWorkspacePath(relativePath)) {
      fail("WORKSPACE_SENSITIVE_PATH", "sensitive workspace path is forbidden", relativePath);
    }
    if (stat.size > this.limits.maxFileBytes) {
      fail("WORKSPACE_FILE_LIMIT", "workspace file exceeds the per-file limit", relativePath);
    }
    if (!Buffer.isBuffer(content)) {
      fail("WORKSPACE_CONTENT_REQUIRED", "full workspace file content is required", relativePath);
    }
    if (content.length !== stat.size) {
      fail(
        "WORKSPACE_CONTENT_SIZE_MISMATCH",
        "workspace file content does not match its captured size",
        relativePath,
      );
    }
    const secretPattern = detectSecret(content);
    if (secretPattern !== null) {
      throw new WorkspacePathPolicyError(
        "WORKSPACE_SECRET_DETECTED",
        "workspace file contains secret material",
        { path: relativePath, pattern: secretPattern },
      );
    }
    return Object.freeze({ action: "persist", kind: "file" });
  }
}

module.exports = {
  DEFAULT_WORKSPACE_LIMITS,
  EXACT_DATABASES,
  WorkspacePathPolicy,
  WorkspacePathPolicyError,
  detectSecret,
  resolveWorkspaceLimits,
  validateDurableEntryPath,
  validateRelativePath,
};
