"use strict";

const path = require("node:path");
const crypto = require("node:crypto");
const {
  DeleteObjectCommand,
  GetObjectCommand,
  ListObjectsV2Command,
  PutObjectCommand,
} = require("@aws-sdk/client-s3");

const {
  EXACT_DATABASES,
  resolveWorkspaceLimits,
} = require("./workspace-path-policy");
const {
  MANIFEST_FORMAT,
  POINTER_FORMAT,
  createGeneration,
  currentPointerKey,
  encodeManifest,
  encodePointer,
  manifestKey,
  parseManifest,
  parsePointer,
  payloadKey,
  sha256Hex,
} = require("./workspace-manifest");

class WorkspaceStoreError extends Error {
  constructor(code, message, details = {}, cause) {
    super(message, cause ? { cause } : undefined);
    this.name = this.constructor.name;
    this.code = code;
    this.details = Object.freeze({ ...details });
  }
}

class WorkspaceConflictError extends WorkspaceStoreError {}
class WorkspaceIntegrityError extends WorkspaceStoreError {}
class WorkspaceQuarantinedError extends WorkspaceStoreError {}

function isMissing(error) {
  return (
    error?.name === "NoSuchKey" ||
    error?.name === "NotFound" ||
    error?.$metadata?.httpStatusCode === 404
  );
}

function isConflict(error) {
  return (
    error?.name === "PreconditionFailed" ||
    error?.name === "ConditionalRequestConflict" ||
    error?.$metadata?.httpStatusCode === 409 ||
    error?.$metadata?.httpStatusCode === 412
  );
}

function isTimeout(error) {
  return (
    error?.name === "TimeoutError" ||
    error?.name === "RequestTimeout" ||
    error?.code === "ETIMEDOUT" ||
    /timed?\s*out|timeout/iu.test(error?.message || "")
  );
}

function isAmbiguousWriteFailure(error) {
  const statusCode = error?.$metadata?.httpStatusCode;
  return (
    isTimeout(error) ||
    (Number.isInteger(statusCode) && statusCode >= 500) ||
    [
      "ECONNABORTED",
      "ECONNRESET",
      "EHOSTUNREACH",
      "EPIPE",
      "ENETDOWN",
      "ENETRESET",
      "ENETUNREACH",
    ].includes(error?.code) ||
    ["AbortError", "NetworkingError"].includes(error?.name) ||
    /connection\s+reset|socket\s+hang\s+up|network\s+error/iu.test(
      error?.message || "",
    )
  );
}

function isQuotedEtag(etag) {
  return (
    typeof etag === "string" &&
    etag.length >= 2 &&
    etag.startsWith('"') &&
    etag.endsWith('"') &&
    !/[\r\n]/u.test(etag)
  );
}

function bufferChunk(chunk) {
  if (Buffer.isBuffer(chunk)) return chunk;
  if (chunk instanceof Uint8Array) {
    return Buffer.from(chunk.buffer, chunk.byteOffset, chunk.byteLength);
  }
  if (typeof chunk === "string") return Buffer.from(chunk, "utf8");
  throw new WorkspaceIntegrityError(
    "WORKSPACE_BODY_INVALID",
    "S3 returned a non-byte body chunk",
  );
}

async function* bodyChunks(body) {
  if (Buffer.isBuffer(body) || body instanceof Uint8Array || typeof body === "string") {
    yield bufferChunk(body);
    return;
  }
  if (body && typeof body[Symbol.asyncIterator] === "function") {
    for await (const chunk of body) yield bufferChunk(chunk);
    return;
  }
  if (body && typeof body.transformToByteArray === "function") {
    yield bufferChunk(await body.transformToByteArray());
    return;
  }
  throw new WorkspaceIntegrityError(
    "WORKSPACE_BODY_INVALID",
    "S3 response body is not byte-readable",
  );
}

async function collectBody(response, maxBytes, code) {
  if (
    Number.isSafeInteger(response?.ContentLength) &&
    response.ContentLength > maxBytes
  ) {
    throw new WorkspaceIntegrityError(code, "S3 object exceeds its byte limit");
  }
  const chunks = [];
  let size = 0;
  for await (const chunk of bodyChunks(response?.Body)) {
    size += chunk.length;
    if (!Number.isSafeInteger(size) || size > maxBytes) {
      throw new WorkspaceIntegrityError(code, "S3 object exceeds its byte limit");
    }
    chunks.push(chunk);
  }
  return Buffer.concat(chunks, size);
}

function samePreciseRegularIdentity(before, after) {
  return (
    before.dev === after.dev &&
    before.ino === after.ino &&
    before.size === after.size &&
    before.mtimeNs === after.mtimeNs &&
    before.ctimeNs === after.ctimeNs &&
    before.nlink === after.nlink &&
    after.isFile() &&
    !after.isSymbolicLink() &&
    after.nlink === 1n
  );
}

function samePreciseDirectoryIdentity(before, after) {
  return (
    before.dev === after.dev &&
    before.ino === after.ino &&
    before.size === after.size &&
    before.mtimeNs === after.mtimeNs &&
    before.ctimeNs === after.ctimeNs &&
    before.nlink === after.nlink &&
    after.isDirectory() &&
    !after.isSymbolicLink()
  );
}

function sameDirectoryFingerprint(before, after) {
  return (
    before.dev === after.dev &&
    before.ino === after.ino &&
    before.size === after.size &&
    before.mtimeMs === after.mtimeMs &&
    before.ctimeMs === after.ctimeMs &&
    after.isDirectory() &&
    !after.isSymbolicLink()
  );
}

function isContainedPath(root, candidate) {
  return candidate === root || candidate.startsWith(`${root}${path.sep}`);
}

class WorkspaceSnapshotStore {
  constructor({
    s3,
    bucket,
    namespace,
    pathPolicy,
    sqliteSnapshot,
    fs,
    clock,
    uuid,
    limits = {},
  } = {}) {
    if (!s3 || typeof s3.send !== "function") {
      throw new TypeError("WorkspaceSnapshotStore requires an injected S3 client");
    }
    if (typeof bucket !== "string" || bucket.length === 0) {
      throw new TypeError("WorkspaceSnapshotStore requires an injected bucket");
    }
    if (!pathPolicy || typeof pathPolicy.classify !== "function") {
      throw new TypeError("WorkspaceSnapshotStore requires an injected path policy");
    }
    if (!sqliteSnapshot || typeof sqliteSnapshot.snapshot !== "function") {
      throw new TypeError("WorkspaceSnapshotStore requires an injected SQLite snapshot helper");
    }
    if (!fs || typeof fs.lstatSync !== "function") {
      throw new TypeError("WorkspaceSnapshotStore requires an injected filesystem");
    }
    if (typeof clock !== "function" || typeof uuid !== "function") {
      throw new TypeError("WorkspaceSnapshotStore requires injected clock and UUID functions");
    }

    // Validate the namespace before retaining any authority-bearing dependency.
    currentPointerKey(namespace);
    this.s3 = s3;
    this.bucket = bucket;
    this.namespace = namespace;
    this.pathPolicy = pathPolicy;
    this.sqliteSnapshot = sqliteSnapshot;
    this.fs = fs;
    this.clock = clock;
    this.uuid = uuid;
    this.limits = resolveWorkspaceLimits(limits);
    this.quarantined = null;
    this.lastGcError = null;
  }

  _assertUsable() {
    if (this.quarantined) {
      throw new WorkspaceQuarantinedError(
        "WORKSPACE_QUARANTINED",
        "workspace store is quarantined after an ambiguous write",
        {},
        this.quarantined,
      );
    }
  }

  _quarantine(message, cause, details = {}) {
    const error = new WorkspaceQuarantinedError(
      "WORKSPACE_QUARANTINED",
      message,
      details,
      cause,
    );
    this.quarantined = error;
    throw error;
  }

  async _getResponse(key, { allowMissing = false } = {}) {
    try {
      return await this.s3.send(
        new GetObjectCommand({ Bucket: this.bucket, Key: key }),
      );
    } catch (error) {
      if (allowMissing && isMissing(error)) return null;
      throw error;
    }
  }

  async _getBytes(key, maxBytes, { allowMissing = false, code = "WORKSPACE_OBJECT_INVALID" } = {}) {
    const response = await this._getResponse(key, { allowMissing });
    if (response === null) return null;
    return {
      bytes: await collectBody(response, maxBytes, code),
      etag: response.ETag,
    };
  }

  async _assertNoLegacyFlatLayout() {
    const prefix = `${this.namespace}/.openclaw/`;
    const response = await this.s3.send(
      new ListObjectsV2Command({
        Bucket: this.bucket,
        Prefix: prefix,
        MaxKeys: 1,
      }),
    );
    const invalidListing = () =>
      new WorkspaceIntegrityError(
        "WORKSPACE_LEGACY_CHECK_INVALID",
        "legacy workspace listing was not an authoritative empty result",
      );
    if (
      response === null ||
      typeof response !== "object" ||
      Array.isArray(response) ||
      response.IsTruncated !== false ||
      (response.Contents !== undefined && !Array.isArray(response.Contents))
    ) {
      throw invalidListing();
    }
    const contents = response.Contents || [];
    if (
      (response.KeyCount !== undefined &&
        (!Number.isSafeInteger(response.KeyCount) ||
          response.KeyCount !== contents.length)) ||
      contents.some(
        (entry) =>
          entry === null ||
          typeof entry !== "object" ||
          typeof entry.Key !== "string" ||
          !entry.Key.startsWith(prefix),
      )
    ) {
      throw invalidListing();
    }
    if (contents.length > 0) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_LEGACY_LAYOUT",
        "legacy flat workspace objects require explicit migration",
      );
    }
  }

  async loadHead() {
    this._assertUsable();
    const pointerKey = currentPointerKey(this.namespace);
    const current = await this._getBytes(pointerKey, this.limits.maxManifestBytes, {
      allowMissing: true,
      code: "WORKSPACE_POINTER_LIMIT",
    });
    if (current === null) {
      await this._assertNoLegacyFlatLayout();
      return null;
    }
    if (!isQuotedEtag(current.etag)) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_ETAG_INVALID",
        "current pointer requires an exact opaque quoted ETag",
      );
    }

    let pointer;
    try {
      pointer = parsePointer(current.bytes, this.limits);
    } catch (error) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_POINTER_INVALID",
        "current pointer is malformed or noncanonical",
        {},
        error,
      );
    }
    const immutableManifestKey = manifestKey(this.namespace, pointer.generation);
    const storedManifest = await this._getBytes(
      immutableManifestKey,
      this.limits.maxManifestBytes,
      { allowMissing: true, code: "WORKSPACE_MANIFEST_LIMIT" },
    );
    if (storedManifest === null) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_MANIFEST_MISSING",
        "current pointer references a missing manifest",
      );
    }
    if (sha256Hex(storedManifest.bytes) !== pointer.manifestSha256) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_MANIFEST_HASH_MISMATCH",
        "current pointer manifest digest does not match",
      );
    }

    let manifest;
    try {
      manifest = parseManifest(storedManifest.bytes, this.limits);
    } catch (error) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_MANIFEST_INVALID",
        "current manifest is malformed or noncanonical",
        {},
        error,
      );
    }
    if (
      manifest.generation !== pointer.generation ||
      manifest.parent !== pointer.parent
    ) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_HEAD_MISMATCH",
        "current pointer and immutable manifest disagree",
      );
    }
    return Object.freeze({
      generation: pointer.generation,
      manifestSha256: pointer.manifestSha256,
      parent: pointer.parent,
      etag: current.etag,
      pointer,
      pointerBytes: current.bytes,
      manifest,
      manifestBytes: storedManifest.bytes,
    });
  }

  _readStableRegularFile(absolutePath, preciseBefore, relativePath) {
    const constants = this.fs.constants;
    if (!constants || constants.O_NOFOLLOW === undefined) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_NOFOLLOW_UNAVAILABLE",
        "filesystem lacks O_NOFOLLOW support",
        { path: relativePath },
      );
    }
    let descriptor;
    try {
      if (
        !preciseBefore ||
        typeof preciseBefore.size !== "bigint" ||
        !preciseBefore.isFile() ||
        preciseBefore.isSymbolicLink() ||
        preciseBefore.nlink !== 1n
      ) {
        throw new WorkspaceIntegrityError(
          "WORKSPACE_FILE_RACED",
          "workspace file changed before capture",
          { path: relativePath },
        );
      }
      descriptor = this.fs.openSync(
        absolutePath,
        constants.O_RDONLY | constants.O_NOFOLLOW,
      );
      const opened = this.fs.fstatSync(descriptor, { bigint: true });
      if (!samePreciseRegularIdentity(preciseBefore, opened)) {
        throw new WorkspaceIntegrityError(
          "WORKSPACE_FILE_RACED",
          "workspace file changed before capture",
          { path: relativePath },
        );
      }
      const bytes = this.fs.readFileSync(descriptor);
      const after = this.fs.fstatSync(descriptor, { bigint: true });
      const namedAfter = this.fs.lstatSync(absolutePath, { bigint: true });
      if (
        !samePreciseRegularIdentity(preciseBefore, after) ||
        !samePreciseRegularIdentity(preciseBefore, namedAfter) ||
        BigInt(bytes.length) !== preciseBefore.size
      ) {
        throw new WorkspaceIntegrityError(
          "WORKSPACE_FILE_RACED",
          "workspace file changed during capture",
          { path: relativePath },
        );
      }
      return bytes;
    } finally {
      if (descriptor !== undefined) this.fs.closeSync(descriptor);
    }
  }

  _directoryRace(relativePath, cause) {
    return new WorkspaceIntegrityError(
      "WORKSPACE_DIRECTORY_RACED",
      "workspace directory changed during deterministic capture",
      { path: relativePath || "." },
      cause,
    );
  }

  _openPinnedDirectory(absolutePath, expected, rootRealPath, relativePath) {
    const constants = this.fs.constants;
    if (
      !constants ||
      constants.O_NOFOLLOW === undefined ||
      constants.O_DIRECTORY === undefined
    ) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_DIRECTORY_PIN_UNAVAILABLE",
        "filesystem lacks pinned no-follow directory support",
        { path: relativePath || "." },
      );
    }
    let named;
    let realPath;
    let descriptor;
    try {
      named = this.fs.lstatSync(absolutePath);
      realPath = this.fs.realpathSync(absolutePath);
      if (
        !sameDirectoryFingerprint(expected, named) ||
        !isContainedPath(rootRealPath, realPath)
      ) {
        throw this._directoryRace(relativePath);
      }
      descriptor = this.fs.openSync(
        absolutePath,
        constants.O_RDONLY |
          constants.O_DIRECTORY |
          constants.O_NOFOLLOW |
          (constants.O_CLOEXEC || 0),
      );
      const opened = this.fs.fstatSync(descriptor);
      if (!sameDirectoryFingerprint(named, opened)) {
        throw this._directoryRace(relativePath);
      }
      const preciseOpened = this.fs.fstatSync(descriptor, { bigint: true });
      if (!preciseOpened.isDirectory() || preciseOpened.isSymbolicLink()) {
        throw this._directoryRace(relativePath);
      }

      // AgentCore runs on Linux. Traversing through this retained directory FD
      // means a rename or symlink replacement of any named ancestor cannot
      // redirect child reads. Other development platforms retain the FD and
      // enforce the before/after identity checks below, failing before S3.
      let traversalPath = absolutePath;
      if (process.platform === "linux") {
        traversalPath = `/proc/self/fd/${descriptor}`;
        const pinned = this.fs.statSync(traversalPath);
        if (!sameDirectoryFingerprint(opened, pinned)) {
          throw this._directoryRace(relativePath);
        }
      }
      return {
        descriptor,
        named,
        opened,
        preciseOpened,
        realPath,
        traversalPath,
      };
    } catch (error) {
      if (descriptor !== undefined) this.fs.closeSync(descriptor);
      if (error instanceof WorkspaceIntegrityError) throw error;
      throw this._directoryRace(relativePath, error);
    }
  }

  _assertPinnedDirectoryUnchanged(
    absolutePath,
    pinned,
    rootRealPath,
    relativePath,
  ) {
    try {
      const openedAfter = this.fs.fstatSync(pinned.descriptor);
      const namedAfter = this.fs.lstatSync(absolutePath);
      const realAfter = this.fs.realpathSync(absolutePath);
      if (
        !sameDirectoryFingerprint(pinned.opened, openedAfter) ||
        !sameDirectoryFingerprint(pinned.named, namedAfter) ||
        realAfter !== pinned.realPath ||
        !isContainedPath(rootRealPath, realAfter)
      ) {
        throw this._directoryRace(relativePath);
      }
    } catch (error) {
      if (error instanceof WorkspaceIntegrityError) throw error;
      throw this._directoryRace(relativePath, error);
    }
  }

  _recordCaptured(capture, relativePath, kind, bytes) {
    if (capture.entries.length >= this.limits.maxEntries) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_ENTRY_LIMIT",
        "workspace has too many durable entries",
      );
    }
    if (bytes.length > this.limits.maxFileBytes) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_FILE_LIMIT",
        "workspace file exceeds its byte limit",
        { path: relativePath },
      );
    }
    capture.totalSize += bytes.length;
    if (
      !Number.isSafeInteger(capture.totalSize) ||
      capture.totalSize > this.limits.maxGenerationBytes
    ) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_GENERATION_LIMIT",
        "workspace generation exceeds its byte limit",
      );
    }
    const sha256 = sha256Hex(bytes);
    capture.entries.push(
      Object.freeze({ kind, path: relativePath, sha256, size: bytes.length }),
    );
    if (!capture.payloads.has(sha256)) capture.payloads.set(sha256, bytes);
  }

  async _captureSqlite({ absolutePath, relativePath, stat, policy, tempDir, index }) {
    const targetPath = path.join(tempDir, `${index}.sqlite`);
    const beforeSource = this.fs.lstatSync(absolutePath);
    if (
      beforeSource.dev !== stat.dev ||
      beforeSource.ino !== stat.ino ||
      !beforeSource.isFile() ||
      beforeSource.isSymbolicLink() ||
      beforeSource.nlink !== 1
    ) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_FILE_RACED",
        "live SQLite path changed before snapshot",
        { path: relativePath },
      );
    }
    const metadata = await this.sqliteSnapshot.snapshot({
      sourcePath: absolutePath,
      targetPath,
      role: policy.role,
      ...(policy.agentId ? { agentId: policy.agentId } : {}),
    });
    const afterSource = this.fs.lstatSync(absolutePath);
    if (
      afterSource.dev !== stat.dev ||
      afterSource.ino !== stat.ino ||
      !afterSource.isFile() ||
      afterSource.isSymbolicLink() ||
      afterSource.nlink !== 1
    ) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_FILE_RACED",
        "live SQLite path changed during snapshot",
        { path: relativePath },
      );
    }
    const snapshotStat = this.fs.lstatSync(targetPath, { bigint: true });
    if (snapshotStat.size > BigInt(this.limits.maxFileBytes)) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_FILE_LIMIT",
        "completed SQLite snapshot exceeds its byte limit",
        { path: relativePath },
      );
    }
    const bytes = this._readStableRegularFile(targetPath, snapshotStat, relativePath);
    const digest = sha256Hex(bytes);
    if (
      !metadata ||
      metadata.size !== bytes.length ||
      metadata.sha256 !== digest
    ) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_SQLITE_SNAPSHOT_INVALID",
        "SQLite helper metadata does not match the completed snapshot",
        { path: relativePath },
      );
    }
    return bytes;
  }

  async _captureTree(root) {
    if (typeof root !== "string" || !path.isAbsolute(root)) {
      throw new TypeError("workspace root must be an absolute path");
    }
    const rootStat = this.fs.lstatSync(root);
    if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_ROOT_INVALID",
        "workspace root must be a real directory",
      );
    }
    const rootRealPath = this.fs.realpathSync(root);
    const capture = { entries: [], payloads: new Map(), totalSize: 0 };
    const retainedIdentities = [];
    const tempDir = this.fs.mkdtempSync(
      path.join(path.dirname(root), ".personal-operator-snapshot-"),
    );
    let sqliteIndex = 0;

    const walk = async (
      absoluteDirectory,
      relativeDirectory = "",
      expectedDirectory = rootStat,
    ) => {
      const pinned = this._openPinnedDirectory(
        absoluteDirectory,
        expectedDirectory,
        rootRealPath,
        relativeDirectory,
      );
      const logicalDirectory = relativeDirectory
        ? path.join(root, ...relativeDirectory.split("/"))
        : root;
      retainedIdentities.push({
        kind: "directory",
        path: relativeDirectory,
        absolutePath: logicalDirectory,
        identity: pinned.preciseOpened,
        realPath: pinned.realPath,
      });
      try {
        const names = this.fs.readdirSync(pinned.traversalPath);
        names.sort((left, right) =>
          Buffer.compare(Buffer.from(left, "utf8"), Buffer.from(right, "utf8")),
        );
        for (const name of names) {
          const relativePath = relativeDirectory ? `${relativeDirectory}/${name}` : name;
          const absolutePath = path.join(pinned.traversalPath, name);
          const logicalAbsolutePath = path.join(
            root,
            ...relativePath.split("/"),
          );
          // Capture nanosecond identity before the policy-facing stat. If a
          // writer changes an inode between classification and descriptor open,
          // the retained identity below makes that change observable.
          const preciseStat = this.fs.lstatSync(absolutePath, { bigint: true });
          const stat = this.fs.lstatSync(absolutePath);

          if (stat.isDirectory() && !stat.isSymbolicLink()) {
            const classification = this.pathPolicy.classify(relativePath, { stat });
            if (classification.action === "traverse" || classification.action === "exclude") {
              // Excluded trees are still inspected so hidden symlinks, hardlinks, and
              // special files cannot evade the global file-type invariant.
              await walk(absolutePath, relativePath, stat);
              continue;
            }
            throw new WorkspaceIntegrityError(
              "WORKSPACE_POLICY_INVALID",
              "path policy returned an invalid directory action",
              { path: relativePath },
            );
          }

          if (Object.hasOwn(EXACT_DATABASES, relativePath)) {
            const classification = this.pathPolicy.classify(relativePath, { stat });
            if (classification.action !== "persist" || classification.kind !== "sqlite") {
              throw new WorkspaceIntegrityError(
                "WORKSPACE_POLICY_INVALID",
                "path policy rejected a frozen SQLite role",
                { path: relativePath },
              );
            }
            const bytes = await this._captureSqlite({
              absolutePath: logicalAbsolutePath,
              relativePath,
              stat,
              policy: classification,
              tempDir,
              index: sqliteIndex,
            });
            sqliteIndex += 1;
            this._recordCaptured(capture, relativePath, "sqlite", bytes);
            continue;
          }

          // Classify excluded paths without ever reading their bytes. Ordinary
          // workspace files require their complete content for the secret scan.
          let classification;
          try {
            classification = this.pathPolicy.classify(relativePath, { stat });
          } catch (error) {
            if (error?.code !== "WORKSPACE_CONTENT_REQUIRED") throw error;
          }
          if (classification?.action === "exclude") continue;

          const bytes = this._readStableRegularFile(
            absolutePath,
            preciseStat,
            relativePath,
          );
          classification = this.pathPolicy.classify(relativePath, { stat, content: bytes });
          if (classification.action !== "persist" || classification.kind !== "file") {
            throw new WorkspaceIntegrityError(
              "WORKSPACE_POLICY_INVALID",
              "path policy returned an invalid file action",
              { path: relativePath },
            );
          }
          retainedIdentities.push({
            kind: "file",
            path: relativePath,
            absolutePath: logicalAbsolutePath,
            identity: preciseStat,
          });
          this._recordCaptured(capture, relativePath, "file", bytes);
        }
        this._assertPinnedDirectoryUnchanged(
          absoluteDirectory,
          pinned,
          rootRealPath,
          relativeDirectory,
        );
      } catch (error) {
        if (
          error instanceof WorkspaceIntegrityError ||
          error?.code?.startsWith("WORKSPACE_")
        ) {
          throw error;
        }
        throw this._directoryRace(relativeDirectory, error);
      } finally {
        this.fs.closeSync(pinned.descriptor);
      }
    };

    try {
      await walk(root);
      for (const retained of retainedIdentities) {
        let current;
        try {
          current = this.fs.lstatSync(retained.absolutePath, { bigint: true });
          if (retained.kind === "file") {
            if (!samePreciseRegularIdentity(retained.identity, current)) {
              throw new WorkspaceIntegrityError(
                "WORKSPACE_FILE_RACED",
                "workspace file changed across generation capture",
                { path: retained.path },
              );
            }
          } else {
            const currentRealPath = this.fs.realpathSync(retained.absolutePath);
            if (
              !samePreciseDirectoryIdentity(retained.identity, current) ||
              currentRealPath !== retained.realPath ||
              !isContainedPath(rootRealPath, currentRealPath)
            ) {
              throw this._directoryRace(retained.path);
            }
          }
        } catch (error) {
          if (error instanceof WorkspaceIntegrityError) throw error;
          if (retained.kind === "file") {
            throw new WorkspaceIntegrityError(
              "WORKSPACE_FILE_RACED",
              "workspace file changed across generation capture",
              { path: retained.path },
              error,
            );
          }
          throw this._directoryRace(retained.path, error);
        }
      }
      capture.entries.sort((left, right) =>
        Buffer.compare(Buffer.from(left.path, "utf8"), Buffer.from(right.path, "utf8")),
      );
      return capture;
    } finally {
      this.fs.rmSync(tempDir, { recursive: true, force: true });
    }
  }

  async _putImmutable(key, bytes) {
    try {
      await this.s3.send(
        new PutObjectCommand({
          Bucket: this.bucket,
          Key: key,
          Body: bytes,
          IfNoneMatch: "*",
        }),
      );
      return;
    } catch (error) {
      const ambiguous = isAmbiguousWriteFailure(error);
      if (!isConflict(error) && !ambiguous) throw error;
      let stored;
      try {
        stored = await this._getBytes(key, bytes.length, {
          allowMissing: true,
          code: "WORKSPACE_IMMUTABLE_LIMIT",
        });
      } catch (reconcileError) {
        if (ambiguous) {
          this._quarantine(
            "immutable write timed out and could not be reconciled",
            reconcileError,
            { key },
          );
        }
        throw new WorkspaceConflictError(
          "WORKSPACE_CONFLICT",
          "immutable object conflict could not be reconciled",
          { key },
          reconcileError,
        );
      }
      if (stored !== null && stored.bytes.equals(bytes)) return;
      if (stored !== null) {
        if (ambiguous) {
          this._quarantine("immutable timeout exposed different bytes", error, { key });
        }
        throw new WorkspaceIntegrityError(
          "WORKSPACE_IMMUTABLE_MISMATCH",
          "immutable object key already contains different bytes",
          { key },
          error,
        );
      }
      if (ambiguous) {
        this._quarantine("immutable write outcome is unknown", error, { key });
      }
      throw new WorkspaceConflictError(
        "WORKSPACE_CONFLICT",
        "immutable conditional write lost a race",
        { key },
        error,
      );
    }
  }

  async _putCurrent(bytes, head) {
    const key = currentPointerKey(this.namespace);
    const input = {
      Bucket: this.bucket,
      Key: key,
      Body: bytes,
      ...(head === null ? { IfNoneMatch: "*" } : { IfMatch: head.etag }),
    };
    try {
      const response = await this.s3.send(new PutObjectCommand(input));
      if (isQuotedEtag(response?.ETag)) return response.ETag;
      // A successful response without its exact opaque ETag is ambiguous. Re-read
      // rather than inventing, normalizing, or borrowing an ETag.
      return await this._reconcileCurrent(bytes, new Error("pointer PUT omitted ETag"), true);
    } catch (error) {
      if (isConflict(error)) {
        return await this._reconcileCurrent(bytes, error, false);
      }
      if (isAmbiguousWriteFailure(error)) {
        return await this._reconcileCurrent(bytes, error, true);
      }
      throw error;
    }
  }

  async _reconcileCurrent(intendedBytes, originalError, ambiguous) {
    const key = currentPointerKey(this.namespace);
    let current;
    try {
      current = await this._getBytes(key, this.limits.maxManifestBytes, {
        allowMissing: true,
        code: "WORKSPACE_POINTER_LIMIT",
      });
    } catch (error) {
      if (ambiguous) {
        this._quarantine("pointer write could not be strongly reconciled", error);
      }
      throw new WorkspaceConflictError(
        "WORKSPACE_CONFLICT",
        "pointer conflict could not be reconciled",
        {},
        error,
      );
    }
    if (
      current !== null &&
      current.bytes.equals(intendedBytes) &&
      isQuotedEtag(current.etag)
    ) {
      return current.etag;
    }
    if (ambiguous) {
      this._quarantine("ambiguous pointer write exposed a different state", originalError);
    }
    throw new WorkspaceConflictError(
      "WORKSPACE_CONFLICT",
      "current pointer compare-and-swap lost a race",
      {},
      originalError,
    );
  }

  _commitTimestamp() {
    const value = this.clock();
    if (!(value instanceof Date) || !Number.isFinite(value.getTime())) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_CLOCK_INVALID",
        "injected clock must return a valid Date",
      );
    }
    return value.toISOString();
  }

  async commit({ liveDir, assertWritable } = {}) {
    this._assertUsable();
    if (typeof assertWritable !== "function") {
      throw new TypeError("commit requires an injected assertWritable function");
    }
    const head = await this.loadHead();
    const capture = await this._captureTree(liveDir);
    const generation = createGeneration(this.uuid);
    const parent = head?.generation || null;
    const manifest = {
      entries: capture.entries,
      format: MANIFEST_FORMAT,
      generation,
      parent,
    };
    const manifestBytes = encodeManifest(manifest, this.limits);
    const manifestSha256 = sha256Hex(manifestBytes);

    for (const sha256 of [...capture.payloads.keys()].sort()) {
      await this._putImmutable(
        payloadKey(this.namespace, generation, sha256),
        capture.payloads.get(sha256),
      );
    }
    await this._putImmutable(
      manifestKey(this.namespace, generation),
      manifestBytes,
    );
    await assertWritable();

    const pointerBytes = encodePointer({
      committedAt: this._commitTimestamp(),
      format: POINTER_FORMAT,
      generation,
      manifestSha256,
      parent,
    });
    const etag = await this._putCurrent(pointerBytes, head);

    // Pointer durability is the commit point. GC cannot invalidate the new
    // generation, so cleanup failures are retained for diagnostics and bucket
    // lifecycle retry rather than turning a durable commit into a false failure.
    try {
      await this._garbageCollectGrandparent(head);
      this.lastGcError = null;
    } catch (error) {
      this.lastGcError = error;
    }

    return Object.freeze({ generation, manifestSha256, parent, etag });
  }

  _openExclusiveFile(destination) {
    const constants = this.fs.constants;
    if (!constants || constants.O_NOFOLLOW === undefined) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_NOFOLLOW_UNAVAILABLE",
        "filesystem lacks O_NOFOLLOW support",
      );
    }
    return this.fs.openSync(
      destination,
      constants.O_WRONLY |
        constants.O_CREAT |
        constants.O_EXCL |
        constants.O_NOFOLLOW,
      0o600,
    );
  }

  _writeChunk(descriptor, chunk) {
    let offset = 0;
    while (offset < chunk.length) {
      const written = this.fs.writeSync(
        descriptor,
        chunk,
        offset,
        chunk.length - offset,
      );
      if (!Number.isSafeInteger(written) || written <= 0) {
        throw new WorkspaceIntegrityError(
          "WORKSPACE_WRITE_FAILED",
          "filesystem made no progress while restoring",
        );
      }
      offset += written;
    }
  }

  _destinationFor(stageDir, relativePath) {
    const destination = path.join(stageDir, ...relativePath.split("/"));
    const resolvedStage = path.resolve(stageDir);
    const resolvedDestination = path.resolve(destination);
    if (!resolvedDestination.startsWith(`${resolvedStage}${path.sep}`)) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_PATH_INVALID",
        "restore path escaped private staging",
        { path: relativePath },
      );
    }
    return destination;
  }

  _writeBufferToStage(stageDir, entry, bytes) {
    const destination = this._destinationFor(stageDir, entry.path);
    this.fs.mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 });
    let descriptor;
    try {
      descriptor = this._openExclusiveFile(destination);
      this._writeChunk(descriptor, bytes);
      this.fs.fsyncSync(descriptor);
    } finally {
      if (descriptor !== undefined) this.fs.closeSync(descriptor);
    }
  }

  async _restorePayloadToStage(stageDir, entry) {
    const key = payloadKey(
      this.namespace,
      entry.generation,
      entry.sha256,
    );
    // This method receives a generation-decorated private entry; keeping the
    // persisted manifest shape exact avoids leaking storage-only fields.
    const response = await this._getResponse(key, { allowMissing: true });
    if (response === null) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_PAYLOAD_MISSING",
        "manifest references a missing payload",
        { path: entry.path },
      );
    }
    if (
      Number.isSafeInteger(response.ContentLength) &&
      response.ContentLength !== entry.size
    ) {
      throw new WorkspaceIntegrityError(
        "WORKSPACE_PAYLOAD_SIZE_MISMATCH",
        "payload size differs from manifest",
        { path: entry.path },
      );
    }

    const destination = this._destinationFor(stageDir, entry.path);
    this.fs.mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 });
    let descriptor;
    let size = 0;
    const hash = crypto.createHash("sha256");
    try {
      descriptor = this._openExclusiveFile(destination);
      for await (const chunk of bodyChunks(response.Body)) {
        size += chunk.length;
        if (size > entry.size || size > this.limits.maxFileBytes) {
          throw new WorkspaceIntegrityError(
            "WORKSPACE_PAYLOAD_SIZE_MISMATCH",
            "payload stream exceeds manifest size",
            { path: entry.path },
          );
        }
        hash.update(chunk);
        this._writeChunk(descriptor, chunk);
      }
      if (size !== entry.size) {
        throw new WorkspaceIntegrityError(
          "WORKSPACE_PAYLOAD_SIZE_MISMATCH",
          "payload stream is shorter than manifest size",
          { path: entry.path },
        );
      }
      if (hash.digest("hex") !== entry.sha256) {
        throw new WorkspaceIntegrityError(
          "WORKSPACE_PAYLOAD_HASH_MISMATCH",
          "payload digest differs from manifest",
          { path: entry.path },
        );
      }
      this.fs.fsyncSync(descriptor);
    } finally {
      if (descriptor !== undefined) this.fs.closeSync(descriptor);
    }
  }

  async _restoreManifest(stageDir, head) {
    for (const entry of head.manifest.entries) {
      await this._restorePayloadToStage(stageDir, {
        ...entry,
        generation: head.generation,
      });
    }
  }

  async _restoreSeed(stageDir, seedDir) {
    const capture = await this._captureTree(seedDir);
    for (const entry of capture.entries) {
      this._writeBufferToStage(stageDir, entry, capture.payloads.get(entry.sha256));
    }
  }

  _claimRestoreTarget(target) {
    try {
      this.fs.mkdirSync(target, { mode: 0o700 });
    } catch (error) {
      if (error?.code === "EEXIST") {
        throw new WorkspaceIntegrityError(
          "WORKSPACE_TARGET_EXISTS",
          "restore target already exists; refusing to disturb it",
        );
      }
      throw error;
    }
    try {
      const claim = this.fs.lstatSync(target, { bigint: true });
      if (!claim.isDirectory() || claim.isSymbolicLink()) {
        throw new WorkspaceIntegrityError(
          "WORKSPACE_TARGET_CLAIM_INVALID",
          "restore target claim is not a private directory",
        );
      }
      return claim;
    } catch (error) {
      try {
        this.fs.rmdirSync(target);
      } catch {
        // Preserve the original claim failure. A non-empty or replaced target
        // is never recursively removed here.
      }
      throw error;
    }
  }

  _releaseRestoreClaim(target, claim) {
    let current;
    try {
      current = this.fs.lstatSync(target, { bigint: true });
    } catch (error) {
      if (error?.code === "ENOENT") return;
      throw error;
    }
    if (
      current.dev !== claim.dev ||
      current.ino !== claim.ino ||
      !current.isDirectory() ||
      current.isSymbolicLink()
    ) {
      return;
    }
    try {
      this.fs.rmdirSync(target);
    } catch (error) {
      if (error?.code === "ENOENT" || error?.code === "ENOTEMPTY") return;
      throw error;
    }
  }

  async restore({ targetDir, seedDir } = {}) {
    this._assertUsable();
    if (
      typeof targetDir !== "string" ||
      typeof seedDir !== "string" ||
      !path.isAbsolute(targetDir) ||
      !path.isAbsolute(seedDir)
    ) {
      throw new TypeError("restore requires absolute targetDir and seedDir paths");
    }
    const head = await this.loadHead();
    const parentDirectory = path.dirname(targetDir);
    this.fs.mkdirSync(parentDirectory, { recursive: true, mode: 0o700 });
    const stageDir = this.fs.mkdtempSync(
      path.join(parentDirectory, ".workspace-restore-"),
    );
    let activated = false;
    let targetClaim = null;
    try {
      if (head === null) {
        await this._restoreSeed(stageDir, seedDir);
      } else {
        await this._restoreManifest(stageDir, head);
      }
      targetClaim = this._claimRestoreTarget(targetDir);
      this.fs.renameSync(stageDir, targetDir);
      activated = true;
      targetClaim = null;
      if (head === null) {
        return Object.freeze({
          generation: null,
          manifestSha256: null,
          parent: null,
          newUser: true,
        });
      }
      return Object.freeze({
        generation: head.generation,
        manifestSha256: head.manifestSha256,
        parent: head.parent,
        newUser: false,
      });
    } finally {
      if (!activated) this.fs.rmSync(stageDir, { recursive: true, force: true });
      if (targetClaim !== null) {
        this._releaseRestoreClaim(targetDir, targetClaim);
      }
    }
  }

  async _garbageCollectGrandparent(head) {
    const grandparentGeneration = head?.manifest?.parent;
    if (!grandparentGeneration) return;
    const grandparentKey = manifestKey(this.namespace, grandparentGeneration);
    const stored = await this._getBytes(
      grandparentKey,
      this.limits.maxManifestBytes,
      { allowMissing: true, code: "WORKSPACE_MANIFEST_LIMIT" },
    );
    if (stored === null) return;

    let grandparent;
    try {
      grandparent = parseManifest(stored.bytes, this.limits);
    } catch {
      return;
    }
    if (grandparent.generation !== grandparentGeneration) return;

    const exactKeys = [
      ...new Set(
        grandparent.entries.map((entry) =>
          payloadKey(this.namespace, grandparentGeneration, entry.sha256),
        ),
      ),
      grandparentKey,
    ];
    for (const key of exactKeys) {
      await this.s3.send(
        new DeleteObjectCommand({ Bucket: this.bucket, Key: key }),
      );
    }
  }
}

module.exports = {
  WorkspaceConflictError,
  WorkspaceIntegrityError,
  WorkspaceQuarantinedError,
  WorkspaceSnapshotStore,
  WorkspaceStoreError,
  bodyChunks,
  collectBody,
};
