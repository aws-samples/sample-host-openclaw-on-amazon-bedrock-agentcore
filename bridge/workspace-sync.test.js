"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  MANIFEST_FORMAT,
  POINTER_FORMAT,
  currentPointerKey,
  encodeManifest,
  encodePointer,
  manifestKey,
  parseManifest,
  parsePointer,
  payloadKey,
  sha256Hex,
} = require("./workspace-manifest");
const { WorkspacePathPolicy } = require("./workspace-path-policy");
const {
  WorkspaceIntegrityError,
  WorkspaceQuarantinedError,
  WorkspaceSnapshotStore,
} = require("./workspace-sync");

const G1_UUID = "11111111-1111-4111-8111-111111111111";
const G2_UUID = "22222222-2222-4222-8222-222222222222";
const G3_UUID = "33333333-3333-4333-8333-333333333333";
const G4_UUID = "44444444-4444-4444-8444-444444444444";
const G1 = `g-${G1_UUID}`;
const G2 = `g-${G2_UUID}`;
const G3 = `g-${G3_UUID}`;
const NOW = "2026-07-18T01:02:03.004Z";

function awsError(name, statusCode, message = name) {
  const error = new Error(message);
  error.name = name;
  error.$metadata = { httpStatusCode: statusCode };
  return error;
}

class FakeS3 {
  constructor() {
    this.objects = new Map();
    this.etags = new Map();
    this.calls = [];
    this.before = null;
    this.sequence = 0;
  }

  putDirect(key, bytes, etag = `"etag-${++this.sequence}"`) {
    this.objects.set(key, Buffer.from(bytes));
    this.etags.set(key, etag);
    return etag;
  }

  async send(command) {
    const name = command.constructor.name;
    const input = command.input;
    this.calls.push({ name, input });
    if (this.before) await this.before({ name, input, s3: this });

    if (name === "GetObjectCommand") {
      if (!this.objects.has(input.Key)) throw awsError("NoSuchKey", 404);
      const bytes = this.objects.get(input.Key);
      return {
        Body: (async function* body() {
          const midpoint = Math.floor(bytes.length / 2);
          yield bytes.subarray(0, midpoint);
          yield bytes.subarray(midpoint);
        })(),
        ContentLength: bytes.length,
        ETag: this.etags.get(input.Key),
      };
    }

    if (name === "PutObjectCommand") {
      const existingEtag = this.etags.get(input.Key);
      if (input.IfNoneMatch === "*" && existingEtag !== undefined) {
        throw awsError("PreconditionFailed", 412);
      }
      if (input.IfMatch !== undefined && input.IfMatch !== existingEtag) {
        throw awsError("PreconditionFailed", 412);
      }
      const etag = this.putDirect(input.Key, input.Body);
      return { ETag: etag };
    }

    if (name === "DeleteObjectCommand") {
      this.objects.delete(input.Key);
      this.etags.delete(input.Key);
      return {};
    }

    if (name === "ListObjectsV2Command") {
      const contents = [...this.objects.entries()]
        .filter(([key]) => key.startsWith(input.Prefix))
        .sort(([left], [right]) => left.localeCompare(right))
        .slice(0, input.MaxKeys || 1000)
        .map(([Key, bytes]) => ({ Key, Size: bytes.length }));
      return { Contents: contents, IsTruncated: false };
    }

    throw new Error(`unsupported fake S3 command: ${name}`);
  }
}

function temporaryDirectory(t, prefix = "workspace-store-") {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

function write(root, relativePath, content) {
  const destination = path.join(root, ...relativePath.split("/"));
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.writeFileSync(destination, content);
  return destination;
}

function sqliteFixture() {
  const calls = [];
  return {
    calls,
    async snapshot({ sourcePath, targetPath, role, agentId }) {
      calls.push({ sourcePath, targetPath, role, agentId });
      const bytes = Buffer.from(`verified-snapshot:${role}:${agentId || "global"}`);
      fs.writeFileSync(targetPath, bytes);
      return { size: bytes.length, sha256: sha256Hex(bytes) };
    },
  };
}

function storeFixture({
  s3 = new FakeS3(),
  uuids = [G1_UUID, G2_UUID, G3_UUID, G4_UUID],
  sqliteSnapshot = sqliteFixture(),
  limits,
  fileSystem = fs,
} = {}) {
  const queue = [...uuids];
  const store = new WorkspaceSnapshotStore({
    s3,
    bucket: "workspace-bucket",
    namespace: "user_A",
    pathPolicy: new WorkspacePathPolicy({ limits }),
    sqliteSnapshot,
    fs: fileSystem,
    clock: () => new Date(NOW),
    uuid: () => {
      assert.ok(queue.length > 0, "test UUID queue exhausted");
      return queue.shift();
    },
    limits,
  });
  return { store, s3, sqliteSnapshot };
}

function makeLiveTree(t, files = { "workspace/a.txt": "alpha" }) {
  const liveDir = temporaryDirectory(t, "workspace-live-");
  for (const [relativePath, content] of Object.entries(files)) {
    write(liveDir, relativePath, content);
  }
  return liveDir;
}

function currentBytes(s3) {
  return s3.objects.get(currentPointerKey("user_A"));
}

function assertCode(error, Class, code) {
  assert.ok(error instanceof Class, `${error?.constructor?.name} is not ${Class.name}`);
  assert.equal(error.code, code);
  return true;
}

describe("WorkspaceSnapshotStore construction", () => {
  it("requires every authority-bearing dependency and never constructs ambient S3", () => {
    assert.throws(
      () =>
        new WorkspaceSnapshotStore({
          bucket: "workspace-bucket",
          namespace: "user_A",
          pathPolicy: new WorkspacePathPolicy(),
          sqliteSnapshot: sqliteFixture(),
          fs,
          clock: () => new Date(NOW),
          uuid: () => G1_UUID,
        }),
      /injected s3/i,
    );
  });
});

describe("immutable generation commit", () => {
  it("captures first, writes payload then manifest, rechecks lease, and creates current with IfNoneMatch", async (t) => {
    const liveDir = makeLiveTree(t);
    const events = [];
    const { store, s3 } = storeFixture();
    s3.before = ({ name, input }) => {
      if (name === "PutObjectCommand") events.push(`put:${input.Key}`);
    };

    const result = await store.commit({
      liveDir,
      assertWritable: async () => events.push("lease"),
    });

    const digest = sha256Hex(Buffer.from("alpha"));
    assert.deepEqual(result, {
      generation: G1,
      manifestSha256: result.manifestSha256,
      parent: null,
      etag: '"etag-3"',
    });
    assert.deepEqual(events, [
      `put:${payloadKey("user_A", G1, digest)}`,
      `put:${manifestKey("user_A", G1)}`,
      "lease",
      `put:${currentPointerKey("user_A")}`,
    ]);
    const puts = s3.calls.filter(({ name }) => name === "PutObjectCommand");
    assert.equal(puts[0].input.IfNoneMatch, "*");
    assert.equal(puts[1].input.IfNoneMatch, "*");
    assert.equal(puts[2].input.IfNoneMatch, "*");
    assert.equal(puts[2].input.IfMatch, undefined);
    const manifest = parseManifest(s3.objects.get(manifestKey("user_A", G1)));
    assert.deepEqual(manifest.entries, [
      { kind: "file", path: "workspace/a.txt", sha256: digest, size: 5 },
    ]);
    const pointer = parsePointer(currentBytes(s3));
    assert.deepEqual(pointer, {
      committedAt: NOW,
      format: POINTER_FORMAT,
      generation: G1,
      manifestSha256: result.manifestSha256,
      parent: null,
    });
  });

  it("preserves the exact opaque quoted ETag in the next pointer IfMatch", async (t) => {
    const liveDir = makeLiveTree(t);
    const { store, s3 } = storeFixture();
    let injected = false;
    s3.before = ({ name, input, s3: fake }) => {
      if (
        !injected &&
        name === "PutObjectCommand" &&
        input.Key === currentPointerKey("user_A")
      ) {
        injected = true;
        fake.putDirect(input.Key, input.Body, '"opaque/quoted:etag"');
        throw awsError("PreconditionFailed", 412);
      }
    };
    const first = await store.commit({ liveDir, assertWritable: async () => {} });
    write(liveDir, "workspace/a.txt", "second");

    const second = await store.commit({ liveDir, assertWritable: async () => {} });

    const pointerPuts = s3.calls.filter(
      ({ name, input }) => name === "PutObjectCommand" && input.Key === currentPointerKey("user_A"),
    );
    assert.equal(first.etag, '"opaque/quoted:etag"');
    assert.equal(pointerPuts[1].input.IfMatch, '"opaque/quoted:etag"');
    assert.equal(pointerPuts[1].input.IfNoneMatch, undefined);
    assert.equal(second.parent, G1);
  });

  it("deduplicates identical payload bytes inside one generation", async (t) => {
    const liveDir = makeLiveTree(t, {
      "workspace/a.txt": "same",
      "workspace/b.txt": "same",
    });
    const { store, s3 } = storeFixture();

    await store.commit({ liveDir, assertWritable: async () => {} });

    const payloadPuts = s3.calls.filter(
      ({ name, input }) => name === "PutObjectCommand" && input.Key.includes("/payload/"),
    );
    assert.equal(payloadPuts.length, 1);
    assert.equal(parseManifest(s3.objects.get(manifestKey("user_A", G1))).entries.length, 2);
  });

  it("uses the injected verified SQLite snapshot and never uploads live database bytes", async (t) => {
    const liveDir = makeLiveTree(t, { "workspace/a.txt": "safe" });
    const liveDatabase = write(liveDir, "state/openclaw.sqlite", "LIVE-BYTES-MUST-NOT-UPLOAD");
    const { store, s3, sqliteSnapshot } = storeFixture();

    await store.commit({ liveDir, assertWritable: async () => {} });

    assert.equal(sqliteSnapshot.calls.length, 1);
    assert.equal(sqliteSnapshot.calls[0].sourcePath, liveDatabase);
    assert.equal(sqliteSnapshot.calls[0].role, "global");
    const manifest = parseManifest(s3.objects.get(manifestKey("user_A", G1)));
    const database = manifest.entries.find(({ kind }) => kind === "sqlite");
    assert.ok(database);
    assert.equal(
      s3.objects.get(payloadKey("user_A", G1, database.sha256)).toString(),
      "verified-snapshot:global:global",
    );
    assert.ok(
      [...s3.objects.values()].every((bytes) => !bytes.includes("LIVE-BYTES-MUST-NOT-UPLOAD")),
    );
  });
});

describe("CAS reconciliation and failure boundaries", () => {
  it("reconciles an ambiguous successful pointer PUT only when exact intended bytes are visible", async (t) => {
    const liveDir = makeLiveTree(t);
    const { store, s3 } = storeFixture();
    let injected = false;
    s3.before = ({ name, input, s3: fake }) => {
      if (!injected && name === "PutObjectCommand" && input.Key === currentPointerKey("user_A")) {
        injected = true;
        fake.putDirect(input.Key, input.Body, '"ambiguous-success"');
        const error = new Error("socket timed out after send");
        error.name = "TimeoutError";
        throw error;
      }
    };

    const result = await store.commit({ liveDir, assertWritable: async () => {} });

    assert.equal(result.etag, '"ambiguous-success"');
    assert.equal(result.generation, G1);
  });

  it("quarantines an ambiguous pointer failure when a rival pointer is visible", async (t) => {
    const liveDir = makeLiveTree(t);
    const { store, s3 } = storeFixture();
    const rival = encodePointer({
      committedAt: NOW,
      format: POINTER_FORMAT,
      generation: G2,
      manifestSha256: "b".repeat(64),
      parent: null,
    });
    let injected = false;
    s3.before = ({ name, input, s3: fake }) => {
      if (!injected && name === "PutObjectCommand" && input.Key === currentPointerKey("user_A")) {
        injected = true;
        fake.putDirect(input.Key, rival, '"rival-etag"');
        const error = new Error("request timeout");
        error.name = "TimeoutError";
        throw error;
      }
    };

    await assert.rejects(
      store.commit({ liveDir, assertWritable: async () => {} }),
      (error) => assertCode(error, WorkspaceQuarantinedError, "WORKSPACE_QUARANTINED"),
    );
    const callsAfterFailure = s3.calls.length;
    await assert.rejects(
      store.commit({ liveDir, assertWritable: async () => {} }),
      (error) => assertCode(error, WorkspaceQuarantinedError, "WORKSPACE_QUARANTINED"),
    );
    assert.equal(s3.calls.length, callsAfterFailure, "quarantine must reject without using rival ETag");
  });

  it("allows exactly one concurrent writer and leaves the loser orphan without deleting", async (t) => {
    const liveA = makeLiveTree(t, { "workspace/a": "writer-a" });
    const liveB = makeLiveTree(t, { "workspace/a": "writer-b" });
    const s3 = new FakeS3();
    const first = storeFixture({ s3, uuids: [G1_UUID] }).store;
    const second = storeFixture({ s3, uuids: [G2_UUID] }).store;
    let waiting = 0;
    let release;
    const barrier = new Promise((resolve) => {
      release = resolve;
    });
    s3.before = async ({ name, input }) => {
      if (name === "PutObjectCommand" && input.Key === currentPointerKey("user_A")) {
        waiting += 1;
        if (waiting === 2) release();
        await barrier;
      }
    };

    const settled = await Promise.allSettled([
      first.commit({ liveDir: liveA, assertWritable: async () => {} }),
      second.commit({ liveDir: liveB, assertWritable: async () => {} }),
    ]);

    assert.equal(settled.filter(({ status }) => status === "fulfilled").length, 1);
    const rejected = settled.find(({ status }) => status === "rejected").reason;
    assertCode(rejected, WorkspaceQuarantinedError, "WORKSPACE_QUARANTINED");
    assert.equal(s3.calls.filter(({ name }) => name === "DeleteObjectCommand").length, 0);
    assert.ok(s3.objects.has(manifestKey("user_A", G1)));
    assert.ok(s3.objects.has(manifestKey("user_A", G2)));
  });

  it("rejects a stale restored writer instead of rebasing its tree onto a rival commit", async (t) => {
    const s3 = new FakeS3();
    const initialTree = makeLiveTree(t, { "workspace/state.txt": "G" });
    const initial = storeFixture({ s3, uuids: [G1_UUID] }).store;
    await initial.commit({ liveDir: initialTree, assertWritable: async () => {} });

    const writerA = storeFixture({ s3, uuids: [G2_UUID] }).store;
    const writerB = storeFixture({ s3, uuids: [G3_UUID] }).store;
    const seed = makeLiveTree(t, {});
    const treeA = path.join(temporaryDirectory(t, "workspace-writer-a-"), "live");
    const treeB = path.join(temporaryDirectory(t, "workspace-writer-b-"), "live");
    await writerA.restore({ targetDir: treeA, seedDir: seed });
    await writerB.restore({ targetDir: treeB, seedDir: seed });

    write(treeB, "workspace/state.txt", "B1");
    const committedB = await writerB.commit({
      liveDir: treeB,
      assertWritable: async () => {},
    });
    write(treeA, "workspace/state.txt", "A1");

    await assert.rejects(
      writerA.commit({ liveDir: treeA, assertWritable: async () => {} }),
      (error) =>
        assertCode(
          error,
          WorkspaceQuarantinedError,
          "WORKSPACE_QUARANTINED",
        ),
    );

    const callsAfterConflict = s3.calls.length;
    await assert.rejects(
      writerA.commit({ liveDir: treeA, assertWritable: async () => {} }),
      (error) =>
        assertCode(
          error,
          WorkspaceQuarantinedError,
          "WORKSPACE_QUARANTINED",
        ),
    );
    assert.equal(s3.calls.length, callsAfterConflict);

    const current = parsePointer(currentBytes(s3));
    assert.equal(current.generation, committedB.generation);
    assert.equal(current.parent, G1);
    const pointerPuts = s3.calls.filter(
      ({ name, input }) =>
        name === "PutObjectCommand" &&
        input.Key === currentPointerKey("user_A"),
    );
    assert.equal(pointerPuts.at(-1).input.IfMatch, '"etag-3"');
    assert.notEqual(current.generation, G2);
  });

  it("never writes current when payload, manifest, or writable-lease validation fails", async (t) => {
    for (const boundary of ["payload", "manifest", "lease"]) {
      const liveDir = makeLiveTree(t, { "workspace/a": boundary });
      const { store, s3 } = storeFixture({ uuids: [G1_UUID] });
      s3.before = ({ name, input }) => {
        const isPayload = input.Key?.includes("/payload/");
        const isManifest = input.Key?.endsWith("/manifest.json");
        if (
          name === "PutObjectCommand" &&
          ((boundary === "payload" && isPayload) || (boundary === "manifest" && isManifest))
        ) {
          throw new Error(`${boundary} unavailable`);
        }
      };
      const assertWritable = async () => {
        if (boundary === "lease") throw new Error("lease lost");
      };

      await assert.rejects(store.commit({ liveDir, assertWritable }));
      assert.equal(s3.objects.has(currentPointerKey("user_A")), false, boundary);
      assert.equal(s3.calls.some(({ name }) => name === "DeleteObjectCommand"), false, boundary);
    }
  });

  it("reconciles exact immutable objects after precondition or timeout and rejects different bytes", async (t) => {
    const liveDir = makeLiveTree(t);
    const digest = sha256Hex(Buffer.from("alpha"));
    const payload = payloadKey("user_A", G1, digest);

    const exact = storeFixture({ uuids: [G1_UUID] });
    exact.s3.putDirect(payload, Buffer.from("alpha"), '"already-there"');
    await exact.store.commit({ liveDir, assertWritable: async () => {} });

    const different = storeFixture({ uuids: [G1_UUID] });
    different.s3.putDirect(payload, Buffer.from("rival"), '"rival"');
    await assert.rejects(
      different.store.commit({ liveDir, assertWritable: async () => {} }),
      (error) => assertCode(error, WorkspaceIntegrityError, "WORKSPACE_IMMUTABLE_MISMATCH"),
    );

    const ambiguous = storeFixture({ uuids: [G1_UUID] });
    let injected = false;
    ambiguous.s3.before = ({ name, input, s3: fake }) => {
      if (!injected && name === "PutObjectCommand" && input.Key === payload) {
        injected = true;
        fake.putDirect(input.Key, input.Body, '"payload-ambiguous"');
        const error = new Error("timeout");
        error.name = "TimeoutError";
        throw error;
      }
    };
    await ambiguous.store.commit({ liveDir, assertWritable: async () => {} });
  });

  it("reconciles exact manifest and pointer bytes at their independent ambiguous boundaries", async (t) => {
    const liveDir = makeLiveTree(t);
    const { store, s3 } = storeFixture();
    let manifestInjected = false;
    let pointerInjected = false;
    s3.before = ({ name, input, s3: fake }) => {
      if (
        !manifestInjected &&
        name === "PutObjectCommand" &&
        input.Key === manifestKey("user_A", G1)
      ) {
        manifestInjected = true;
        fake.putDirect(input.Key, input.Body, '"manifest-ambiguous"');
        const error = new Error("manifest timed out after send");
        error.name = "TimeoutError";
        throw error;
      }
      if (
        !pointerInjected &&
        name === "PutObjectCommand" &&
        input.Key === currentPointerKey("user_A")
      ) {
        pointerInjected = true;
        fake.putDirect(input.Key, input.Body, '"pointer-precondition-reconcile"');
        throw awsError("PreconditionFailed", 412);
      }
    };

    const result = await store.commit({ liveDir, assertWritable: async () => {} });

    assert.equal(result.etag, '"pointer-precondition-reconcile"');
    assert.equal(result.generation, G1);
  });

  it("strongly reconciles pointer writes accepted before connection reset or S3 5xx", async (t) => {
    for (const boundary of ["reset", "5xx"]) {
      const liveDir = makeLiveTree(t, { "workspace/a": boundary });
      const { store, s3 } = storeFixture({ uuids: [G1_UUID] });
      let injected = false;
      s3.before = ({ name, input, s3: fake }) => {
        if (!injected && name === "PutObjectCommand" && input.Key === currentPointerKey("user_A")) {
          injected = true;
          fake.putDirect(input.Key, input.Body, `"${boundary}-accepted"`);
          const error = new Error(boundary === "reset" ? "socket hang up" : "service unavailable");
          if (boundary === "reset") error.code = "ECONNRESET";
          if (boundary === "5xx") error.$metadata = { httpStatusCode: 503 };
          throw error;
        }
      };

      const result = await store.commit({ liveDir, assertWritable: async () => {} });

      assert.equal(result.etag, `"${boundary}-accepted"`);
      assert.equal(result.generation, G1);
      assert.equal(store.quarantined, null);
    }
  });

  it("fails closed when head GET or missing-head legacy reconciliation is unavailable", async (t) => {
    const liveDir = makeLiveTree(t);
    for (const boundary of ["get", "list"]) {
      const { store, s3 } = storeFixture({ uuids: [G1_UUID] });
      s3.before = ({ name }) => {
        if (
          (boundary === "get" && name === "GetObjectCommand") ||
          (boundary === "list" && name === "ListObjectsV2Command")
        ) {
          throw new Error(`${boundary} unavailable`);
        }
      };
      await assert.rejects(store.commit({ liveDir, assertWritable: async () => {} }));
      assert.equal(
        s3.calls.some(({ name }) => name === "PutObjectCommand"),
        false,
        boundary,
      );
    }
  });

  it("accepts new-user seeding only after an authoritative empty legacy listing", async (t) => {
    const liveDir = makeLiveTree(t);
    const malformedListings = [
      { Contents: [], IsTruncated: true, KeyCount: 1 },
      { Contents: [{}], IsTruncated: false, KeyCount: 1 },
      {
        Contents: [{ Key: "another-user/.openclaw/state.json" }],
        IsTruncated: false,
        KeyCount: 1,
      },
      { Contents: [], IsTruncated: false, KeyCount: 1 },
    ];

    for (const listing of malformedListings) {
      const calls = [];
      const s3 = {
        async send(command) {
          calls.push(command.constructor.name);
          if (command.constructor.name === "GetObjectCommand") {
            throw awsError("NoSuchKey", 404);
          }
          if (command.constructor.name === "ListObjectsV2Command") {
            return listing;
          }
          throw new Error("unexpected S3 mutation");
        },
      };
      const { store } = storeFixture({ s3, uuids: [G1_UUID] });

      await assert.rejects(
        store.commit({ liveDir, assertWritable: async () => {} }),
        (error) =>
          assertCode(
            error,
            WorkspaceIntegrityError,
            "WORKSPACE_LEGACY_CHECK_INVALID",
          ),
      );
      assert.equal(calls.includes("PutObjectCommand"), false);
    }
  });

  it("quarantines a pointer timeout when no exact pointer becomes visible", async (t) => {
    const liveDir = makeLiveTree(t);
    const { store, s3 } = storeFixture();
    s3.before = ({ name, input }) => {
      if (name === "PutObjectCommand" && input.Key === currentPointerKey("user_A")) {
        const error = new Error("pointer timeout before visibility");
        error.name = "TimeoutError";
        throw error;
      }
    };
    await assert.rejects(
      store.commit({ liveDir, assertWritable: async () => {} }),
      (error) => assertCode(error, WorkspaceQuarantinedError, "WORKSPACE_QUARANTINED"),
    );
    assert.equal(s3.objects.has(currentPointerKey("user_A")), false);
    assert.equal(s3.calls.some(({ name }) => name === "DeleteObjectCommand"), false);
  });
});

describe("restore and logical deletion", () => {
  it("round-trips a committed tree across restart with streamed hash and size checks", async (t) => {
    const liveDir = makeLiveTree(t, { "workspace/a.txt": "alpha" });
    write(liveDir, "agents/main/agent/openclaw-agent.sqlite", "live-agent-db");
    const fixture = storeFixture();
    const committed = await fixture.store.commit({ liveDir, assertWritable: async () => {} });
    const restarted = storeFixture({ s3: fixture.s3, uuids: [G2_UUID] }).store;
    const targetDir = path.join(temporaryDirectory(t, "workspace-target-parent-"), "restored");
    const seedDir = makeLiveTree(t, { "workspace/seed.txt": "seed" });

    const restored = await restarted.restore({ targetDir, seedDir });

    assert.deepEqual(restored, {
      generation: committed.generation,
      manifestSha256: committed.manifestSha256,
      parent: null,
      newUser: false,
    });
    assert.equal(fs.readFileSync(path.join(targetDir, "workspace/a.txt"), "utf8"), "alpha");
    assert.equal(
      fs.readFileSync(path.join(targetDir, "agents/main/agent/openclaw-agent.sqlite"), "utf8"),
      "verified-snapshot:agent:main",
    );
    assert.equal(fs.existsSync(path.join(targetDir, "workspace/seed.txt")), false);
  });

  it("does not replace an empty target created concurrently during activation", async (t) => {
    const sourceDir = makeLiveTree(t, { "workspace/a.txt": "alpha" });
    const source = storeFixture();
    await source.store.commit({ liveDir: sourceDir, assertWritable: async () => {} });
    const parent = temporaryDirectory(t, "workspace-restore-race-");
    const targetDir = path.join(parent, "live");
    const seedDir = makeLiveTree(t, {});
    let raced = false;
    let competingInode;
    const racingFs = Object.create(fs);
    racingFs.lstatSync = (target, options) => {
      try {
        return fs.lstatSync(target, options);
      } catch (error) {
        if (!raced && target === targetDir && error?.code === "ENOENT") {
          fs.mkdirSync(targetDir, { mode: 0o700 });
          competingInode = fs.lstatSync(targetDir, { bigint: true }).ino;
          raced = true;
        }
        throw error;
      }
    };
    racingFs.mkdirSync = (target, options) => {
      if (!raced && target === targetDir) {
        fs.mkdirSync(targetDir, { mode: 0o700 });
        competingInode = fs.lstatSync(targetDir, { bigint: true }).ino;
        raced = true;
      }
      return fs.mkdirSync(target, options);
    };
    const { store } = storeFixture({ s3: source.s3, fileSystem: racingFs });

    await assert.rejects(
      store.restore({ targetDir, seedDir }),
      (error) =>
        assertCode(error, WorkspaceIntegrityError, "WORKSPACE_TARGET_EXISTS"),
    );
    assert.equal(raced, true);
    assert.equal(fs.lstatSync(targetDir, { bigint: true }).ino, competingInode);
    assert.deepEqual(fs.readdirSync(targetDir), []);
  });

  it("represents deletion only by absence from the new manifest", async (t) => {
    const liveDir = makeLiveTree(t, {
      "workspace/keep.txt": "keep",
      "workspace/delete.txt": "delete",
    });
    const fixture = storeFixture();
    await fixture.store.commit({ liveDir, assertWritable: async () => {} });
    fs.unlinkSync(path.join(liveDir, "workspace/delete.txt"));
    await fixture.store.commit({ liveDir, assertWritable: async () => {} });
    const targetDir = path.join(temporaryDirectory(t, "workspace-delete-parent-"), "restored");

    await storeFixture({ s3: fixture.s3, uuids: [G3_UUID] }).store.restore({
      targetDir,
      seedDir: makeLiveTree(t),
    });

    assert.equal(fs.existsSync(path.join(targetDir, "workspace/delete.txt")), false);
    assert.equal(fs.readFileSync(path.join(targetDir, "workspace/keep.txt"), "utf8"), "keep");
    assert.equal(
      fixture.s3.calls.some(({ name }) => name === "DeleteObjectCommand"),
      false,
      "second commit must not physically delete the parent generation",
    );
  });

  it("treats a truly missing pointer as a new user and copies the immutable seed", async (t) => {
    const seedDir = makeLiveTree(t, { "workspace/welcome.md": "welcome" });
    const targetDir = path.join(temporaryDirectory(t, "workspace-seed-parent-"), "restored");
    const { store } = storeFixture();

    const result = await store.restore({ targetDir, seedDir });

    assert.deepEqual(result, {
      generation: null,
      manifestSha256: null,
      parent: null,
      newUser: true,
    });
    assert.equal(fs.readFileSync(path.join(targetDir, "workspace/welcome.md"), "utf8"), "welcome");
  });

  it("keeps a restored new user bound to absence and retries current only with IfNoneMatch", async (t) => {
    const seedDir = makeLiveTree(t, { "workspace/welcome.md": "welcome" });
    const targetDir = path.join(temporaryDirectory(t, "workspace-new-user-parent-"), "live");
    const s3 = new FakeS3();
    const store = storeFixture({ s3, uuids: [G1_UUID] }).store;
    await store.restore({ targetDir, seedDir });

    const rivalTree = makeLiveTree(t, { "workspace/rival.md": "rival" });
    const rival = storeFixture({ s3, uuids: [G2_UUID] }).store;
    await rival.commit({ liveDir: rivalTree, assertWritable: async () => {} });
    write(targetDir, "workspace/welcome.md", "personalized");
    await assert.rejects(
      store.commit({ liveDir: targetDir, assertWritable: async () => {} }),
      (error) =>
        assertCode(
          error,
          WorkspaceQuarantinedError,
          "WORKSPACE_QUARANTINED",
        ),
    );

    const pointerPut = s3.calls.filter(
      ({ name, input }) =>
        name === "PutObjectCommand" &&
        input.Key === currentPointerKey("user_A"),
    ).at(-1);
    assert.equal(pointerPut.input.IfNoneMatch, "*");
    assert.equal(pointerPut.input.IfMatch, undefined);
    const current = parsePointer(currentBytes(s3));
    assert.equal(current.generation, G2);
    assert.equal(current.parent, null);
  });

  it("fails closed on legacy flat state, malformed current, missing payload, and corrupt payload", async (t) => {
    const seedDir = makeLiveTree(t);
    const targetParent = temporaryDirectory(t, "workspace-corrupt-parent-");

    const legacy = storeFixture();
    legacy.s3.putDirect("user_A/.openclaw/workspace/old.txt", Buffer.from("legacy"));
    await assert.rejects(
      legacy.store.restore({ targetDir: path.join(targetParent, "legacy"), seedDir }),
      (error) => assertCode(error, WorkspaceIntegrityError, "WORKSPACE_LEGACY_LAYOUT"),
    );

    const malformed = storeFixture();
    malformed.s3.putDirect(currentPointerKey("user_A"), Buffer.from("{}\n"), '"bad"');
    await assert.rejects(
      malformed.store.restore({ targetDir: path.join(targetParent, "malformed"), seedDir }),
    );

    const source = storeFixture();
    const liveDir = makeLiveTree(t, { "workspace/a": "original" });
    await source.store.commit({ liveDir, assertWritable: async () => {} });
    const current = parsePointer(currentBytes(source.s3));
    const sourceManifest = parseManifest(source.s3.objects.get(manifestKey("user_A", current.generation)));
    const objectKey = payloadKey("user_A", current.generation, sourceManifest.entries[0].sha256);
    source.s3.objects.delete(objectKey);
    source.s3.etags.delete(objectKey);
    await assert.rejects(
      storeFixture({ s3: source.s3 }).store.restore({
        targetDir: path.join(targetParent, "missing"),
        seedDir,
      }),
      (error) => assertCode(error, WorkspaceIntegrityError, "WORKSPACE_PAYLOAD_MISSING"),
    );

    source.s3.putDirect(objectKey, Buffer.from("bad"), '"tampered"');
    const existingTarget = path.join(targetParent, "last-good");
    fs.mkdirSync(existingTarget);
    fs.writeFileSync(path.join(existingTarget, "sentinel"), "last-good");
    await assert.rejects(
      storeFixture({ s3: source.s3 }).store.restore({ targetDir: existingTarget, seedDir }),
      (error) => assertCode(error, WorkspaceIntegrityError, "WORKSPACE_PAYLOAD_SIZE_MISMATCH"),
    );
    assert.equal(fs.readFileSync(path.join(existingTarget, "sentinel"), "utf8"), "last-good");
  });

  it("fails closed on an unquoted head ETag, missing manifest, payload outage, and same-size hash mismatch", async (t) => {
    const seedDir = makeLiveTree(t);
    const targetParent = temporaryDirectory(t, "workspace-head-failure-");
    const emptyManifestBytes = encodeManifest({
      entries: [],
      format: MANIFEST_FORMAT,
      generation: G1,
      parent: null,
    });
    const pointerBytes = encodePointer({
      committedAt: NOW,
      format: POINTER_FORMAT,
      generation: G1,
      manifestSha256: sha256Hex(emptyManifestBytes),
      parent: null,
    });

    const unquoted = storeFixture();
    unquoted.s3.putDirect(currentPointerKey("user_A"), pointerBytes, "not-quoted");
    await assert.rejects(
      unquoted.store.restore({ targetDir: path.join(targetParent, "unquoted"), seedDir }),
      (error) => assertCode(error, WorkspaceIntegrityError, "WORKSPACE_ETAG_INVALID"),
    );

    const missingManifest = storeFixture();
    missingManifest.s3.putDirect(currentPointerKey("user_A"), pointerBytes, '"current"');
    await assert.rejects(
      missingManifest.store.restore({ targetDir: path.join(targetParent, "manifest"), seedDir }),
      (error) => assertCode(error, WorkspaceIntegrityError, "WORKSPACE_MANIFEST_MISSING"),
    );

    const committed = storeFixture();
    const liveDir = makeLiveTree(t, { "workspace/a": "abcd" });
    await committed.store.commit({ liveDir, assertWritable: async () => {} });
    const current = parsePointer(currentBytes(committed.s3));
    const storedManifest = parseManifest(
      committed.s3.objects.get(manifestKey("user_A", current.generation)),
    );
    const key = payloadKey("user_A", current.generation, storedManifest.entries[0].sha256);

    let outageInjected = false;
    committed.s3.before = ({ name, input }) => {
      if (!outageInjected && name === "GetObjectCommand" && input.Key === key) {
        outageInjected = true;
        throw new Error("payload unavailable");
      }
    };
    await assert.rejects(
      storeFixture({ s3: committed.s3 }).store.restore({
        targetDir: path.join(targetParent, "outage"),
        seedDir,
      }),
    );
    committed.s3.before = null;
    committed.s3.putDirect(key, Buffer.from("wxyz"), '"same-size-corrupt"');
    await assert.rejects(
      storeFixture({ s3: committed.s3 }).store.restore({
        targetDir: path.join(targetParent, "hash"),
        seedDir,
      }),
      (error) => assertCode(error, WorkspaceIntegrityError, "WORKSPACE_PAYLOAD_HASH_MISMATCH"),
    );
  });
});

describe("capture rejection and bounded GC", () => {
  it("rejects a torn generation when an earlier file changes before a later capture", async (t) => {
    const liveDir = makeLiveTree(t, {
      "workspace/a.txt": "A0",
      "workspace/b.txt": "B0",
    });
    const aPath = path.join(liveDir, "workspace/a.txt");
    const bPath = path.join(liveDir, "workspace/b.txt");
    let mutated = false;
    const racingFs = Object.create(fs);
    racingFs.lstatSync = (target, options) => {
      if (
        !mutated &&
        options?.bigint &&
        String(target).endsWith(`${path.sep}b.txt`)
      ) {
        mutated = true;
        fs.writeFileSync(aPath, "A1");
        fs.writeFileSync(bPath, "B1");
      }
      return fs.lstatSync(target, options);
    };
    const { store, s3 } = storeFixture({ fileSystem: racingFs });

    await assert.rejects(
      store.commit({ liveDir, assertWritable: async () => {} }),
      (error) =>
        assertCode(error, WorkspaceIntegrityError, "WORKSPACE_FILE_RACED"),
    );
    assert.equal(mutated, true);
    assert.equal(s3.calls.some(({ name }) => name === "PutObjectCommand"), false);
  });

  it("detects a durable parent-directory symlink swap before any S3 mutation", async (t) => {
    const liveDir = makeLiveTree(t, { "workspace/original.txt": "original" });
    const outside = temporaryDirectory(t, "workspace-race-outside-");
    write(outside, "escaped.txt", "must-not-upload");
    const workspaceDirectory = path.join(liveDir, "workspace");
    const originalDirectory = path.join(liveDir, "workspace-original");
    let swapped = false;
    let directoryReads = 0;
    const racingFs = Object.create(fs);
    racingFs.readdirSync = (target, ...args) => {
      directoryReads += 1;
      if (!swapped && directoryReads === 2) {
        swapped = true;
        fs.renameSync(workspaceDirectory, originalDirectory);
        fs.symlinkSync(outside, workspaceDirectory, "dir");
      }
      return fs.readdirSync(target, ...args);
    };
    const { store, s3 } = storeFixture({ fileSystem: racingFs });

    await assert.rejects(
      store.commit({ liveDir, assertWritable: async () => {} }),
      (error) =>
        assertCode(
          error,
          WorkspaceIntegrityError,
          "WORKSPACE_DIRECTORY_RACED",
        ),
    );
    assert.equal(swapped, true);
    assert.equal(s3.calls.some(({ name }) => name === "PutObjectCommand"), false);
    assert.ok(
      [...s3.objects.values()].every((bytes) => !bytes.includes("must-not-upload")),
    );
  });

  it("detects same-inode content mutation even when mtime and size are restored", async (t) => {
    const liveDir = makeLiveTree(t, { "workspace/raced.txt": "SAFE" });
    const racedPath = path.join(liveDir, "workspace/raced.txt");
    const wholeSecond = 1_700_000_000;
    fs.utimesSync(racedPath, wholeSecond, wholeSecond);
    const original = fs.statSync(racedPath);
    let mutated = false;
    const racingFs = Object.create(fs);
    racingFs.readFileSync = (target, ...args) => {
      if (!mutated && typeof target === "number") {
        mutated = true;
        fs.writeFileSync(racedPath, "EVIL");
        fs.utimesSync(racedPath, original.atime, original.mtime);
      }
      return fs.readFileSync(target, ...args);
    };
    const { store, s3 } = storeFixture({ fileSystem: racingFs });

    await assert.rejects(
      store.commit({ liveDir, assertWritable: async () => {} }),
      (error) =>
        assertCode(error, WorkspaceIntegrityError, "WORKSPACE_FILE_RACED"),
    );
    assert.equal(mutated, true);
    assert.equal(s3.calls.some(({ name }) => name === "PutObjectCommand"), false);
  });

  it("detects same-inode mutation between the named stat and descriptor open", async (t) => {
    const liveDir = makeLiveTree(t, { "workspace/raced.txt": "SAFE" });
    const racedPath = path.join(liveDir, "workspace/raced.txt");
    const wholeSecond = 1_700_000_000;
    fs.utimesSync(racedPath, wholeSecond, wholeSecond);
    const original = fs.statSync(racedPath);
    let mutated = false;
    const racingFs = Object.create(fs);
    racingFs.lstatSync = (target, options) => {
      const result = fs.lstatSync(target, options);
      if (
        !mutated &&
        !options?.bigint &&
        String(target).endsWith(`${path.sep}raced.txt`)
      ) {
        mutated = true;
        fs.writeFileSync(racedPath, "EVIL");
        fs.utimesSync(racedPath, original.atime, original.mtime);
      }
      return result;
    };
    const { store, s3 } = storeFixture({ fileSystem: racingFs });

    await assert.rejects(
      store.commit({ liveDir, assertWritable: async () => {} }),
      (error) =>
        assertCode(error, WorkspaceIntegrityError, "WORKSPACE_FILE_RACED"),
    );
    assert.equal(mutated, true);
    assert.equal(s3.calls.some(({ name }) => name === "PutObjectCommand"), false);
  });

  it("rejects an oversized completed SQLite snapshot before reading its bytes", async (t) => {
    const liveDir = makeLiveTree(t, {});
    write(liveDir, "state/openclaw.sqlite", Buffer.alloc(0));
    let snapshotRead = false;
    const trackingFs = Object.create(fs);
    trackingFs.readFileSync = (...args) => {
      snapshotRead = true;
      return fs.readFileSync(...args);
    };
    const sqliteSnapshot = {
      async snapshot({ targetPath }) {
        const bytes = Buffer.from("xx");
        fs.writeFileSync(targetPath, bytes);
        return { size: bytes.length, sha256: sha256Hex(bytes) };
      },
    };
    const { store, s3 } = storeFixture({
      fileSystem: trackingFs,
      sqliteSnapshot,
      limits: { maxFileBytes: 1 },
    });

    await assert.rejects(
      store.commit({ liveDir, assertWritable: async () => {} }),
      (error) =>
        assertCode(error, WorkspaceIntegrityError, "WORKSPACE_FILE_LIMIT"),
    );
    assert.equal(snapshotRead, false);
    assert.equal(s3.calls.some(({ name }) => name === "PutObjectCommand"), false);
  });

  it("rejects secrets, symlinks, hardlinks, special files, and limit overflow before any S3 mutation", async (t) => {
    const cases = [];

    const secret = makeLiveTree(t, { "workspace/a": "token=notsecret", "workspace/b": "AKIAIOSFODNN7EXAMPLE" });
    cases.push(secret);

    const symlink = makeLiveTree(t);
    fs.symlinkSync("a.txt", path.join(symlink, "workspace/link"));
    cases.push(symlink);

    const hardlink = makeLiveTree(t);
    fs.linkSync(path.join(hardlink, "workspace/a.txt"), path.join(hardlink, "workspace/hard"));
    cases.push(hardlink);

    if (process.platform !== "win32") {
      const fifo = makeLiveTree(t);
      const fifoPath = path.join(fifo, "workspace/fifo");
      require("node:child_process").execFileSync("mkfifo", [fifoPath]);
      cases.push(fifo);
    }

    const overflow = makeLiveTree(t, { "workspace/a": "aa", "workspace/b": "bb" });
    cases.push(overflow);

    for (const [index, liveDir] of cases.entries()) {
      const limits = index === cases.length - 1 ? { maxGenerationBytes: 3 } : undefined;
      const { store, s3 } = storeFixture({ uuids: [G1_UUID], limits });
      await assert.rejects(store.commit({ liveDir, assertWritable: async () => {} }));
      assert.equal(
        s3.calls.some(({ name }) => name === "PutObjectCommand"),
        false,
        `case ${index} wrote S3 before capture succeeded`,
      );
    }
  });

  it("retains current and parent, then deletes only a validated grandparent declaration", async (t) => {
    const liveDir = makeLiveTree(t, { "workspace/a": "one" });
    const { store, s3 } = storeFixture();
    await store.commit({ liveDir, assertWritable: async () => {} });
    const firstManifest = parseManifest(s3.objects.get(manifestKey("user_A", G1)));
    const firstPayload = payloadKey("user_A", G1, firstManifest.entries[0].sha256);
    const unknown = `user_A/.system/workspace/v1/generations/${G1}/unknown-object`;
    const incomplete = "user_A/.system/workspace/v1/generations/g-incomplete/orphan";
    s3.putDirect(unknown, Buffer.from("unknown"));
    s3.putDirect(incomplete, Buffer.from("incomplete"));

    write(liveDir, "workspace/a", "two");
    await store.commit({ liveDir, assertWritable: async () => {} });
    assert.ok(s3.objects.has(firstPayload));
    assert.ok(s3.objects.has(manifestKey("user_A", G1)));

    write(liveDir, "workspace/a", "three");
    await store.commit({ liveDir, assertWritable: async () => {} });

    const deleted = s3.calls
      .filter(({ name }) => name === "DeleteObjectCommand")
      .map(({ input }) => input.Key);
    assert.deepEqual(deleted.sort(), [firstPayload, manifestKey("user_A", G1)].sort());
    assert.equal(s3.objects.has(firstPayload), false);
    assert.equal(s3.objects.has(manifestKey("user_A", G1)), false);
    assert.ok(s3.objects.has(manifestKey("user_A", G2)));
    assert.ok(s3.objects.has(manifestKey("user_A", G3)));
    assert.ok(s3.objects.has(unknown));
    assert.ok(s3.objects.has(incomplete));
  });

  it("keeps a durable pointer successful when post-commit GC fails", async (t) => {
    const liveDir = makeLiveTree(t, { "workspace/a": "one" });
    const { store, s3 } = storeFixture();
    await store.commit({ liveDir, assertWritable: async () => {} });
    write(liveDir, "workspace/a", "two");
    await store.commit({ liveDir, assertWritable: async () => {} });
    write(liveDir, "workspace/a", "three");
    s3.before = ({ name }) => {
      if (name === "DeleteObjectCommand") throw new Error("GC unavailable");
    };

    const result = await store.commit({ liveDir, assertWritable: async () => {} });

    assert.equal(result.generation, G3);
    assert.equal(parsePointer(currentBytes(s3)).generation, G3);
    assert.match(store.lastGcError.message, /GC unavailable/);
    assert.ok(s3.objects.has(manifestKey("user_A", G1)));
  });
});
