"use strict";

const crypto = require("node:crypto");

const { canonicalNamespace } = require("./session-binding");
const {
  resolveWorkspaceLimits,
  validateDurableEntryPath,
} = require("./workspace-path-policy");

const MANIFEST_FORMAT = "personal-operator.workspace-manifest.v1";
const POINTER_FORMAT = "personal-operator.workspace-current.v1";
const GENERATION_PATTERN =
  /^g-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const SHA256_PATTERN = /^[0-9a-f]{64}$/u;
const ISO_UTC_MILLISECONDS_PATTERN =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/u;

class WorkspaceManifestError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "WorkspaceManifestError";
    this.code = code;
    this.details = Object.freeze({ ...details });
  }
}

function invalid(code, message, details) {
  throw new WorkspaceManifestError(code, message, details);
}

function limitsWithDefaults(limits = {}) {
  return resolveWorkspaceLimits(limits);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function hasExactKeys(value, expected) {
  return (
    isPlainObject(value) &&
    Object.keys(value).sort().join("\u0000") === [...expected].sort().join("\u0000")
  );
}

function validateGeneration(generation) {
  if (typeof generation !== "string" || !GENERATION_PATTERN.test(generation)) {
    invalid(
      "WORKSPACE_GENERATION_INVALID",
      "generation must be g- followed by a lowercase UUIDv4",
      { generation },
    );
  }
  return generation;
}

function createGeneration(uuid) {
  if (typeof uuid !== "function") {
    invalid("WORKSPACE_GENERATION_INVALID", "an injected UUID generator is required");
  }
  const candidate = uuid();
  if (typeof candidate !== "string" || !candidate.isWellFormed()) {
    invalid("WORKSPACE_GENERATION_INVALID", "UUID generator returned an invalid value");
  }
  return validateGeneration(`g-${candidate.toLowerCase()}`);
}

function validateSha256(sha256) {
  if (typeof sha256 !== "string" || !SHA256_PATTERN.test(sha256)) {
    invalid("WORKSPACE_DIGEST_INVALID", "SHA-256 must be lowercase hexadecimal", {
      sha256,
    });
  }
  return sha256;
}

function comparePaths(left, right) {
  return Buffer.compare(Buffer.from(left, "utf8"), Buffer.from(right, "utf8"));
}

function validateEntry(rawEntry, limits) {
  if (!hasExactKeys(rawEntry, ["kind", "path", "sha256", "size"])) {
    invalid("WORKSPACE_MANIFEST_INVALID", "manifest entry has an invalid shape");
  }
  if (rawEntry.kind !== "file" && rawEntry.kind !== "sqlite") {
    invalid("WORKSPACE_MANIFEST_INVALID", "manifest entry kind is unsupported");
  }
  try {
    validateDurableEntryPath(rawEntry.kind, rawEntry.path, limits);
    validateSha256(rawEntry.sha256);
  } catch (error) {
    if (error instanceof WorkspaceManifestError) {
      invalid("WORKSPACE_MANIFEST_INVALID", error.message);
    }
    invalid("WORKSPACE_MANIFEST_INVALID", "manifest entry path is unsafe");
  }
  if (!Number.isSafeInteger(rawEntry.size) || rawEntry.size < 0) {
    invalid("WORKSPACE_MANIFEST_INVALID", "manifest entry size is invalid", {
      path: rawEntry.path,
    });
  }
  if (rawEntry.size > limits.maxFileBytes) {
    invalid("WORKSPACE_MANIFEST_LIMIT", "manifest entry is too large", {
      path: rawEntry.path,
    });
  }
  return Object.freeze({
    kind: rawEntry.kind,
    path: rawEntry.path,
    sha256: rawEntry.sha256,
    size: rawEntry.size,
  });
}

function validateManifest(value, suppliedLimits = {}) {
  const limits = limitsWithDefaults(suppliedLimits);
  if (!hasExactKeys(value, ["entries", "format", "generation", "parent"])) {
    invalid("WORKSPACE_MANIFEST_INVALID", "manifest has an invalid shape");
  }
  if (value.format !== MANIFEST_FORMAT || !Array.isArray(value.entries)) {
    invalid("WORKSPACE_MANIFEST_INVALID", "manifest format or entries are invalid");
  }
  let generation;
  let parent;
  try {
    generation = validateGeneration(value.generation);
    parent = value.parent === null ? null : validateGeneration(value.parent);
  } catch (error) {
    invalid("WORKSPACE_MANIFEST_INVALID", error.message);
  }
  if (parent === generation) {
    invalid("WORKSPACE_MANIFEST_INVALID", "manifest cannot parent itself");
  }
  if (value.entries.length > limits.maxEntries) {
    invalid("WORKSPACE_MANIFEST_LIMIT", "manifest has too many entries");
  }

  const entries = value.entries.map((rawEntry) => validateEntry(rawEntry, limits));
  entries.sort((left, right) => comparePaths(left.path, right.path));
  const paths = new Set();
  for (const entry of entries) {
    if (paths.has(entry.path)) {
      invalid("WORKSPACE_MANIFEST_INVALID", "manifest contains colliding paths", {
        path: entry.path,
      });
    }
    paths.add(entry.path);
  }
  for (const entry of entries) {
    let boundary = entry.path.lastIndexOf("/");
    while (boundary > 0) {
      const ancestor = entry.path.slice(0, boundary);
      if (paths.has(ancestor)) {
        invalid("WORKSPACE_MANIFEST_INVALID", "manifest contains colliding paths", {
          path: entry.path,
        });
      }
      boundary = ancestor.lastIndexOf("/");
    }
  }
  const totalSize = entries.reduce((total, current) => total + current.size, 0);
  if (!Number.isSafeInteger(totalSize) || totalSize > limits.maxGenerationBytes) {
    invalid("WORKSPACE_MANIFEST_LIMIT", "manifest generation is too large");
  }

  return Object.freeze({
    entries: Object.freeze(entries),
    format: MANIFEST_FORMAT,
    generation,
    parent,
  });
}

function validatePointer(value) {
  if (
    !hasExactKeys(value, [
      "committedAt",
      "format",
      "generation",
      "manifestSha256",
      "parent",
    ])
  ) {
    invalid("WORKSPACE_POINTER_INVALID", "current pointer has an invalid shape");
  }
  if (value.format !== POINTER_FORMAT) {
    invalid("WORKSPACE_POINTER_INVALID", "current pointer format is invalid");
  }
  let generation;
  let parent;
  let manifestSha256;
  try {
    generation = validateGeneration(value.generation);
    parent = value.parent === null ? null : validateGeneration(value.parent);
    manifestSha256 = validateSha256(value.manifestSha256);
  } catch (error) {
    invalid("WORKSPACE_POINTER_INVALID", error.message);
  }
  if (parent === generation) {
    invalid("WORKSPACE_POINTER_INVALID", "current pointer cannot parent itself");
  }
  if (
    typeof value.committedAt !== "string" ||
    !ISO_UTC_MILLISECONDS_PATTERN.test(value.committedAt) ||
    new Date(value.committedAt).toISOString() !== value.committedAt
  ) {
    invalid("WORKSPACE_POINTER_INVALID", "commit timestamp must be canonical UTC milliseconds");
  }
  return Object.freeze({
    committedAt: value.committedAt,
    format: POINTER_FORMAT,
    generation,
    manifestSha256,
    parent,
  });
}

function encodeCanonical(value) {
  return Buffer.from(JSON.stringify(value), "utf8");
}

function encodeManifest(value, limits) {
  const canonical = validateManifest(value, limits);
  const bytes = encodeCanonical(canonical);
  const effectiveLimits = limitsWithDefaults(limits);
  if (bytes.length > effectiveLimits.maxManifestBytes) {
    invalid("WORKSPACE_MANIFEST_LIMIT", "encoded manifest exceeds its byte limit");
  }
  return bytes;
}

function encodePointer(value) {
  return encodeCanonical(validatePointer(value));
}

function parseCanonical(bytes, { kind, validate, encode, maxBytes }) {
  const input = Buffer.isBuffer(bytes) ? bytes : Buffer.from(bytes || []);
  if (input.length > maxBytes) {
    invalid("WORKSPACE_MANIFEST_LIMIT", `${kind} exceeds its byte limit`);
  }
  let parsed;
  try {
    parsed = JSON.parse(input.toString("utf8"));
  } catch {
    invalid(
      kind === "manifest" ? "WORKSPACE_MANIFEST_INVALID" : "WORKSPACE_POINTER_INVALID",
      `${kind} is not valid JSON`,
    );
  }
  const canonical = validate(parsed);
  const canonicalBytes = encode(canonical);
  if (!input.equals(canonicalBytes)) {
    invalid("WORKSPACE_JSON_NONCANONICAL", `${kind} JSON is not canonical`);
  }
  return canonical;
}

function parseManifest(bytes, suppliedLimits = {}) {
  const limits = limitsWithDefaults(suppliedLimits);
  return parseCanonical(bytes, {
    kind: "manifest",
    validate: (value) => validateManifest(value, limits),
    encode: (value) => encodeManifest(value, limits),
    maxBytes: limits.maxManifestBytes,
  });
}

function parsePointer(bytes, suppliedLimits = {}) {
  const limits = limitsWithDefaults(suppliedLimits);
  return parseCanonical(bytes, {
    kind: "pointer",
    validate: validatePointer,
    encode: encodePointer,
    maxBytes: limits.maxManifestBytes,
  });
}

function sha256Hex(bytes) {
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

function workspaceRootKey(namespace) {
  return `${canonicalNamespace(namespace)}/.system/workspace/v1`;
}

function currentPointerKey(namespace) {
  return `${workspaceRootKey(namespace)}/current.json`;
}

function generationRootKey(namespace, generation) {
  return `${workspaceRootKey(namespace)}/generations/${validateGeneration(generation)}`;
}

function manifestKey(namespace, generation) {
  return `${generationRootKey(namespace, generation)}/manifest.json`;
}

function payloadKey(namespace, generation, sha256) {
  return `${generationRootKey(namespace, generation)}/payload/${validateSha256(sha256)}`;
}

module.exports = {
  MANIFEST_FORMAT,
  POINTER_FORMAT,
  WorkspaceManifestError,
  comparePaths,
  createGeneration,
  currentPointerKey,
  encodeManifest,
  encodePointer,
  generationRootKey,
  manifestKey,
  parseManifest,
  parsePointer,
  payloadKey,
  sha256Hex,
  validateGeneration,
  validateManifest,
  validatePointer,
  validateSha256,
  workspaceRootKey,
};
