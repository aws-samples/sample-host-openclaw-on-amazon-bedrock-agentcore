"use strict";

const nodeFs = require("node:fs");
const path = require("node:path");
const { randomBytes, randomUUID } = require("node:crypto");
const { canonicalNamespace } = require("./session-binding");

const READY_MARKER_BASENAME = ".personal-operator-ready.json";
const READY_MARKER_FORMAT = "personal-operator.workspace-ready.v1";
const GENERATION_PATTERN =
  /^g-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;

function decodeMountInfoPath(value) {
  return value.replace(/\\(040|011|012|134)/g, (match, code) => {
    if (code === "040") return " ";
    if (code === "011") return "\t";
    if (code === "012") return "\n";
    if (code === "134") return "\\";
    return match;
  });
}

function assertExactWritableMount({
  mountedDir,
  mountInfoPath = "/proc/self/mountinfo",
  fs = nodeFs,
} = {}) {
  if (typeof mountedDir !== "string" || !path.isAbsolute(mountedDir)) {
    throw new Error("Workspace mount path must be absolute");
  }
  const expected = path.normalize(mountedDir);
  let mountInfo;
  try {
    mountInfo = fs.readFileSync(mountInfoPath, "utf8");
  } catch (error) {
    throw new Error(`Cannot inspect workspace mount: ${error.message}`);
  }
  const matches = [];
  for (const line of mountInfo.split("\n")) {
    if (!line) continue;
    const separator = line.indexOf(" - ");
    if (separator === -1) continue;
    const fields = line.slice(0, separator).split(" ");
    const trailing = line.slice(separator + 3).split(" ");
    if (fields.length < 6 || trailing.length < 3) continue;
    const mountPoint = path.normalize(decodeMountInfoPath(fields[4]));
    if (mountPoint !== expected) continue;
    matches.push({
      mountOptions: new Set(fields[5].split(",")),
      superOptions: new Set(trailing.slice(2).join(" ").split(",")),
    });
  }
  if (matches.length !== 1) {
    throw new Error("Configured workspace path is not one exact mount");
  }
  if (!matches[0].mountOptions.has("rw") || !matches[0].superOptions.has("rw")) {
    throw new Error("Configured workspace mount is not writable");
  }
  const stat = fs.lstatSync(expected);
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw new Error("Configured workspace mount must be a real directory");
  }
  return expected;
}

function fsyncDirectory(fs, directory) {
  const fd = fs.openSync(directory, fs.constants.O_RDONLY);
  try {
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
}

function probeWritableMount({ mountedDir, fs = nodeFs } = {}) {
  const probePath = path.join(mountedDir, ".personal-operator-mount-probe");
  const token = randomBytes(32);
  let fd;
  try {
    fd = fs.openSync(
      probePath,
      fs.constants.O_CREAT |
        fs.constants.O_EXCL |
        fs.constants.O_RDWR |
        (fs.constants.O_NOFOLLOW || 0),
      0o600,
    );
    fs.writeFileSync(fd, token);
    fs.fsyncSync(fd);
  } catch (error) {
    try {
      fs.unlinkSync(probePath);
    } catch {}
    throw new Error(`Workspace mount write probe failed: ${error.message}`);
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }

  try {
    const retained = fs.readFileSync(probePath);
    if (!Buffer.isBuffer(retained) || !retained.equals(token)) {
      throw new Error("probe bytes changed after fsync");
    }
    fs.unlinkSync(probePath);
    fsyncDirectory(fs, mountedDir);
  } catch (error) {
    try {
      fs.unlinkSync(probePath);
    } catch {}
    throw new Error(`Workspace mount read/unlink probe failed: ${error.message}`);
  }
}

function lstatOrNull(fs, candidate) {
  try {
    return fs.lstatSync(candidate);
  } catch (error) {
    if (error && error.code === "ENOENT") return null;
    throw error;
  }
}

function validateHomeLink({ fs, homeLinkPath, liveDir }) {
  if (typeof homeLinkPath !== "string" || !path.isAbsolute(homeLinkPath)) {
    throw new Error("OpenClaw home state link path must be absolute");
  }
  const stat = lstatOrNull(fs, homeLinkPath);
  if (!stat) return false;
  if (!stat.isSymbolicLink()) {
    throw new Error("Existing OpenClaw home state path is not a managed symlink");
  }
  const target = fs.readlinkSync(homeLinkPath);
  const resolvedTarget = path.resolve(path.dirname(homeLinkPath), target);
  if (resolvedTarget !== liveDir) {
    throw new Error("Existing OpenClaw home state symlink has an unexpected target");
  }
  return true;
}

function encodeReadyMarker({ generation, manifestSha256, namespace }) {
  const canonical = canonicalNamespace(namespace);
  if (!GENERATION_PATTERN.test(generation || "")) {
    throw new Error("Workspace ready marker requires a canonical generation");
  }
  if (!SHA256_PATTERN.test(manifestSha256 || "")) {
    throw new Error("Workspace ready marker requires a canonical manifest hash");
  }
  return Buffer.from(
    JSON.stringify({
      format: READY_MARKER_FORMAT,
      generation,
      manifestSha256,
      namespace: canonical,
    }),
    "utf8",
  );
}

async function prepareWorkspace({
  seedDir,
  mountedDir,
  namespace,
  homeLinkPath,
  snapshotStore,
  assertWritable = async () => {},
  mountInfoPath = "/proc/self/mountinfo",
  fs = nodeFs,
  uuid = randomUUID,
} = {}) {
  const canonical = canonicalNamespace(namespace);
  if (!snapshotStore || typeof snapshotStore.restore !== "function") {
    throw new Error("Workspace snapshot store is required");
  }
  if (typeof seedDir !== "string" || !path.isAbsolute(seedDir)) {
    throw new Error("Immutable workspace seed path must be absolute");
  }

  const mount = assertExactWritableMount({ mountedDir, mountInfoPath, fs });
  probeWritableMount({ mountedDir: mount, fs });

  const liveDir = path.join(mount, "live");
  const existingHomeLink = validateHomeLink({ fs, homeLinkPath, liveDir });
  const identifier = uuid();
  if (
    typeof identifier !== "string" ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
      identifier,
    )
  ) {
    throw new Error("Workspace activation requires a canonical UUIDv4");
  }
  const stagingDir = path.join(mount, `.staging-${identifier}`);
  const rollbackDir = path.join(mount, `.rollback-${identifier}`);

  let activated = false;
  let previousMoved = false;
  let createdHomeLink = false;
  try {
    let head = await snapshotStore.restore({
      targetDir: stagingDir,
      seedDir,
    });
    const stagingStat = fs.lstatSync(stagingDir);
    if (!stagingStat.isDirectory() || stagingStat.isSymbolicLink()) {
      throw new Error("Workspace restore did not produce a private directory");
    }
    fs.chmodSync(stagingDir, 0o700);
    if (!head || typeof head !== "object") {
      throw new Error("Workspace restore returned no verified head");
    }
    if (head.newUser === true) {
      if (typeof snapshotStore.commit !== "function") {
        throw new Error("New workspace cannot be committed durably");
      }
      head = await snapshotStore.commit({
        liveDir: stagingDir,
        assertWritable,
      });
    }

    const marker = encodeReadyMarker({
      generation: head.generation,
      manifestSha256: head.manifestSha256,
      namespace: canonical,
    });
    const markerPath = path.join(stagingDir, READY_MARKER_BASENAME);
    const markerFd = fs.openSync(
      markerPath,
      fs.constants.O_CREAT |
        fs.constants.O_EXCL |
        fs.constants.O_WRONLY |
        (fs.constants.O_NOFOLLOW || 0),
      0o600,
    );
    try {
      fs.writeFileSync(markerFd, marker);
      fs.fsyncSync(markerFd);
    } finally {
      fs.closeSync(markerFd);
    }
    fsyncDirectory(fs, stagingDir);

    if (lstatOrNull(fs, liveDir)) {
      fs.renameSync(liveDir, rollbackDir);
      previousMoved = true;
    }
    try {
      fs.renameSync(stagingDir, liveDir);
      activated = true;
      fsyncDirectory(fs, mount);
    } catch (error) {
      if (previousMoved && !lstatOrNull(fs, liveDir)) {
        fs.renameSync(rollbackDir, liveDir);
        fsyncDirectory(fs, mount);
      }
      previousMoved = false;
      throw error;
    }

    if (!existingHomeLink) {
      fs.symlinkSync(liveDir, homeLinkPath);
      createdHomeLink = true;
      fsyncDirectory(fs, path.dirname(homeLinkPath));
    }
    const retainedMarker = fs.readFileSync(
      path.join(liveDir, READY_MARKER_BASENAME),
    );
    if (!retainedMarker.equals(marker)) {
      throw new Error("Activated workspace ready marker changed");
    }

    if (previousMoved) {
      fs.rmSync(rollbackDir, { recursive: true, force: false });
      previousMoved = false;
      fsyncDirectory(fs, mount);
    }
    return Object.freeze({ liveDir, head: Object.freeze({ ...head }) });
  } catch (error) {
    if (activated) {
      try {
        if (createdHomeLink) {
          fs.unlinkSync(homeLinkPath);
          createdHomeLink = false;
        }
        fs.rmSync(liveDir, { recursive: true, force: true });
        if (previousMoved) {
          fs.renameSync(rollbackDir, liveDir);
          previousMoved = false;
        }
        fsyncDirectory(fs, mount);
      } catch (rollbackError) {
        const combined = new Error(
          `Workspace activation failed and rollback failed: ${rollbackError.message}`,
        );
        combined.cause = error;
        throw combined;
      }
    }
    try {
      fs.rmSync(stagingDir, { recursive: true, force: true });
    } catch {}
    throw error;
  }
}

function lifecycleError(code, message, { retryable = false, cause } = {}) {
  const error = new Error(message);
  error.code = code;
  error.retryable = retryable;
  if (cause) error.cause = cause;
  return error;
}

class WorkspaceLifecycle {
  constructor({
    snapshotStore,
    namespace,
    seedDir,
    mountedDir,
    homeLinkPath,
    prepareWorkspace: prepare = prepareWorkspace,
    stopOpenClaw = async () => {},
    stopSupportProcesses = async () => {},
    shutdownTimeoutMs = 10_000,
    ...prepareOptions
  } = {}) {
    if (!snapshotStore || typeof snapshotStore.commit !== "function") {
      throw new Error("WorkspaceLifecycle requires a snapshot store");
    }
    this.snapshotStore = snapshotStore;
    this.namespace = canonicalNamespace(namespace);
    this.seedDir = seedDir;
    this.mountedDir = mountedDir;
    this.homeLinkPath = homeLinkPath;
    this.prepare = prepare;
    this.prepareOptions = prepareOptions;
    this.stopOpenClaw = stopOpenClaw;
    this.stopSupportProcesses = stopSupportProcesses;
    this.shutdownTimeoutMs = shutdownTimeoutMs;

    this.state = "NEW";
    this.liveDir = null;
    this.head = null;
    this.quarantine = null;
    this.activeTurns = new Set();
    this.nextTurnId = 1;
    this.activeDrained = null;
    this.commitTail = Promise.resolve();
    this.periodicCommit = null;
    this.initializePromise = null;
    this.shutdownPromise = null;
    this.periodicTimer = null;
  }

  status() {
    return Object.freeze({
      state: this.state,
      namespace: this.namespace,
      activeTurns: this.activeTurns.size,
      generation: this.head?.generation || null,
      manifestSha256: this.head?.manifestSha256 || null,
      quarantine: this.quarantine ? { ...this.quarantine } : null,
    });
  }

  async assertWritable() {
    if (this.state !== "READY" && this.state !== "DRAINING") {
      throw lifecycleError(
        "WORKSPACE_NOT_WRITABLE",
        `Workspace is not writable while ${this.state.toLowerCase()}`,
      );
    }
    return Object.freeze({ namespace: this.namespace });
  }

  initialize() {
    if (this.state === "READY") return Promise.resolve(this.status());
    if (this.initializePromise) return this.initializePromise;
    if (this.state !== "NEW") {
      return Promise.reject(
        lifecycleError(
          "WORKSPACE_INITIALIZATION_INVALID",
          `Workspace cannot initialize while ${this.state.toLowerCase()}`,
        ),
      );
    }
    this.state = "INITIALIZING";
    this.initializePromise = (async () => {
      try {
        const prepared = await this.prepare({
          snapshotStore: this.snapshotStore,
          namespace: this.namespace,
          seedDir: this.seedDir,
          mountedDir: this.mountedDir,
          homeLinkPath: this.homeLinkPath,
          assertWritable: async () => {
            if (this.state !== "INITIALIZING") {
              throw new Error("Workspace initialization lost write authority");
            }
            return Object.freeze({ namespace: this.namespace });
          },
          ...this.prepareOptions,
        });
        if (!prepared || typeof prepared.liveDir !== "string" || !prepared.head) {
          throw new Error("Workspace preparation returned an invalid result");
        }
        this.liveDir = prepared.liveDir;
        this.head = prepared.head;
        this.state = "READY";
        return this.status();
      } catch (error) {
        this.state = "FAILED";
        throw error;
      }
    })();
    return this.initializePromise;
  }

  beginTurn() {
    if (this.state !== "READY") {
      const label = this.state === "QUARANTINED" ? "quarantined" : "not ready";
      throw lifecycleError(
        "WORKSPACE_TURN_REJECTED",
        `Workspace is ${label} and cannot admit a turn`,
        { retryable: this.state !== "QUARANTINED" },
      );
    }
    if (this.activeTurns.size === 0) this.activeDrained = deferredPromise();
    const token = Object.freeze({ id: this.nextTurnId++ });
    this.activeTurns.add(token);
    return token;
  }

  _finishTurn(token) {
    if (!this.activeTurns.delete(token)) {
      throw lifecycleError(
        "WORKSPACE_TURN_INVALID",
        "Workspace turn token is not active",
      );
    }
    if (this.activeTurns.size === 0 && this.activeDrained) {
      this.activeDrained.resolve();
      this.activeDrained = null;
    }
  }

  _enqueueCommit(reason) {
    const operation = this.commitTail.then(async () => {
      const head = await this.snapshotStore.commit({
        liveDir: this.liveDir,
        assertWritable: () => this.assertWritable(),
        reason,
      });
      if (!head || !GENERATION_PATTERN.test(head.generation || "")) {
        throw new Error("Workspace commit returned an invalid generation");
      }
      if (!SHA256_PATTERN.test(head.manifestSha256 || "")) {
        throw new Error("Workspace commit returned an invalid manifest hash");
      }
      this.head = Object.freeze({ ...head });
      return this.head;
    });
    this.commitTail = operation.catch(() => {});
    return operation;
  }

  async commitAfterTurn(token) {
    if (this.state !== "READY" && this.state !== "DRAINING") {
      throw lifecycleError(
        "WORKSPACE_TURN_REJECTED",
        `Workspace is ${this.state.toLowerCase()} and cannot persist a turn`,
      );
    }
    this._finishTurn(token);
    try {
      return await this._enqueueCommit("post-turn");
    } catch (cause) {
      this.quarantine = Object.freeze({
        code: "WORKSPACE_PERSISTENCE_FAILED",
        message: "A completed turn could not be durably persisted",
      });
      if (this.state === "READY") this.state = "QUARANTINED";
      throw lifecycleError(
        "WORKSPACE_PERSISTENCE_FAILED",
        "Completed work was not acknowledged because persistence failed",
        { retryable: true, cause },
      );
    }
  }

  async requestPeriodicCommit() {
    if (this.state !== "READY") {
      throw lifecycleError(
        "WORKSPACE_PERIODIC_REJECTED",
        `Workspace is ${this.state.toLowerCase()} and cannot run a periodic commit`,
      );
    }
    if (this.periodicCommit) return this.periodicCommit;
    const operation = this._enqueueCommit("periodic");
    this.periodicCommit = operation;
    try {
      return await operation;
    } catch (cause) {
      this.quarantine = Object.freeze({
        code: "WORKSPACE_PERSISTENCE_FAILED",
        message: "Periodic persistence failed",
      });
      this.state = "QUARANTINED";
      throw lifecycleError(
        "WORKSPACE_PERSISTENCE_FAILED",
        "Periodic workspace persistence failed",
        { retryable: true, cause },
      );
    } finally {
      if (this.periodicCommit === operation) this.periodicCommit = null;
    }
  }

  startPeriodic(intervalMs) {
    if (!Number.isSafeInteger(intervalMs) || intervalMs <= 0) {
      throw new Error("Periodic workspace interval must be a positive integer");
    }
    if (this.state !== "READY") {
      throw new Error("Workspace must be ready before periodic persistence starts");
    }
    if (this.periodicTimer) clearInterval(this.periodicTimer);
    this.periodicTimer = setInterval(() => {
      void this.requestPeriodicCommit().catch(() => {});
    }, intervalMs);
    this.periodicTimer.unref?.();
  }

  shutdown() {
    if (this.shutdownPromise) return this.shutdownPromise;
    if (this.state === "STOPPED") return Promise.resolve(this.status());
    this.shutdownPromise = (async () => {
      this.state = "DRAINING";
      if (this.periodicTimer) {
        clearInterval(this.periodicTimer);
        this.periodicTimer = null;
      }
      let failure = null;
      try {
        if (this.activeTurns.size > 0 && this.activeDrained) {
          await withTimeout(
            this.activeDrained.promise,
            this.shutdownTimeoutMs,
            "Timed out draining active workspace turns",
          );
        }
        await this.commitTail;
        await this.stopOpenClaw();
        await this._enqueueCommit("shutdown");
      } catch (error) {
        failure = error;
      }
      try {
        await this.stopSupportProcesses();
      } catch (error) {
        if (!failure) failure = error;
      }
      if (failure) {
        this.state = "FAILED";
        throw failure;
      }
      this.state = "STOPPED";
      return this.status();
    })();
    return this.shutdownPromise;
  }
}

function deferredPromise() {
  let resolve;
  const promise = new Promise((onResolve) => {
    resolve = onResolve;
  });
  return { promise, resolve };
}

function withTimeout(promise, timeoutMs, message) {
  let timer;
  const timeout = new Promise((resolve, reject) => {
    timer = setTimeout(() => reject(new Error(message)), timeoutMs);
    timer.unref?.();
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

module.exports = {
  READY_MARKER_BASENAME,
  READY_MARKER_FORMAT,
  assertExactWritableMount,
  probeWritableMount,
  encodeReadyMarker,
  prepareWorkspace,
  WorkspaceLifecycle,
};
