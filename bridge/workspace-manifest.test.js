"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  MANIFEST_FORMAT,
  POINTER_FORMAT,
  WorkspaceManifestError,
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
  validateManifest,
  validatePointer,
  workspaceRootKey,
} = require("./workspace-manifest");

const G1 = "g-11111111-1111-4111-8111-111111111111";
const G2 = "g-22222222-2222-4222-8222-222222222222";
const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);

function entry(overrides = {}) {
  return {
    kind: "file",
    path: "workspace/a.txt",
    sha256: SHA_A,
    size: 3,
    ...overrides,
  };
}

function manifest(overrides = {}) {
  return {
    entries: [entry()],
    format: MANIFEST_FORMAT,
    generation: G1,
    parent: null,
    ...overrides,
  };
}

function pointer(overrides = {}) {
  return {
    committedAt: "2026-07-18T01:02:03.004Z",
    format: POINTER_FORMAT,
    generation: G1,
    manifestSha256: SHA_B,
    parent: null,
    ...overrides,
  };
}

function expectManifestError(fn, code) {
  assert.throws(fn, (error) => {
    assert.ok(error instanceof WorkspaceManifestError);
    assert.equal(error.code, code);
    return true;
  });
}

describe("generation and S3 key helpers", () => {
  it("builds only lowercase UUIDv4 generation identifiers", () => {
    assert.equal(
      createGeneration(() => "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
      "g-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    );
    expectManifestError(
      () => createGeneration(() => "11111111-1111-1111-8111-111111111111"),
      "WORKSPACE_GENERATION_INVALID",
    );
  });

  it("builds the frozen workspace v1 key layout", () => {
    assert.equal(workspaceRootKey("user_A"), "user_A/.system/workspace/v1");
    assert.equal(
      currentPointerKey("user_A"),
      "user_A/.system/workspace/v1/current.json",
    );
    assert.equal(
      generationRootKey("user_A", G1),
      `user_A/.system/workspace/v1/generations/${G1}`,
    );
    assert.equal(
      manifestKey("user_A", G1),
      `user_A/.system/workspace/v1/generations/${G1}/manifest.json`,
    );
    assert.equal(
      payloadKey("user_A", G1, SHA_A),
      `user_A/.system/workspace/v1/generations/${G1}/payload/${SHA_A}`,
    );
  });

  it("rejects unsafe namespaces, generations, and digests before building keys", () => {
    for (const build of [
      () => workspaceRootKey("../user"),
      () => generationRootKey("user_A", "g-NOT-A-UUID"),
      () => payloadKey("user_A", G1, "A".repeat(64)),
    ]) {
      assert.throws(build);
    }
  });
});

describe("canonical workspace manifest", () => {
  it("encodes exact field order, sorted entries, and no newline", () => {
    const value = manifest({
      entries: [
        entry({ path: "workspace/z.txt", sha256: SHA_B }),
        entry({ kind: "sqlite", path: "state/openclaw.sqlite", size: 9 }),
      ],
    });
    const bytes = encodeManifest(value);
    assert.equal(
      bytes.toString("utf8"),
      `{"entries":[{"kind":"sqlite","path":"state/openclaw.sqlite","sha256":"${SHA_A}","size":9},{"kind":"file","path":"workspace/z.txt","sha256":"${SHA_B}","size":3}],"format":"personal-operator.workspace-manifest.v1","generation":"${G1}","parent":null}`,
    );
    assert.equal(bytes.at(-1), "}".charCodeAt(0));
    assert.deepEqual(parseManifest(bytes), JSON.parse(bytes.toString("utf8")));
  });

  it("rejects noncanonical JSON bytes, duplicate keys, and alternate entry order", () => {
    const canonical = encodeManifest(manifest());
    for (const bytes of [
      Buffer.from(`${canonical.toString("utf8")}\n`),
      Buffer.from(JSON.stringify(manifest(), null, 2)),
      Buffer.from(
        `{"format":"${MANIFEST_FORMAT}","entries":[],"generation":"${G1}","parent":null}`,
      ),
      Buffer.from(
        `{"entries":[],"entries":[],"format":"${MANIFEST_FORMAT}","generation":"${G1}","parent":null}`,
      ),
      Buffer.from(
        JSON.stringify(
          manifest({
            entries: [
              entry({ path: "workspace/z.txt" }),
              entry({ path: "workspace/a.txt" }),
            ],
          }),
        ),
      ),
    ]) {
      expectManifestError(() => parseManifest(bytes), "WORKSPACE_JSON_NONCANONICAL");
    }
  });

  it("rejects duplicate or unsafe paths and unsupported entry fields", () => {
    for (const value of [
      manifest({ entries: [entry(), entry()] }),
      manifest({
        entries: [
          entry({ path: "workspace/a" }),
          entry({ path: "workspace/a-foo" }),
          entry({ path: "workspace/a/b" }),
        ],
      }),
      manifest({ entries: [entry({ path: "workspace/../secret" })] }),
      manifest({ entries: [entry({ kind: "directory" })] }),
      manifest({ entries: [entry({ sha256: SHA_A.toUpperCase() })] }),
      manifest({ entries: [{ ...entry(), mode: 0o600 }] }),
    ]) {
      expectManifestError(() => validateManifest(value), "WORKSPACE_MANIFEST_INVALID");
    }
  });

  it("binds each manifest kind to the exact durable path allowlist", () => {
    for (const invalidEntry of [
      entry({ kind: "sqlite", path: "workspace/a.txt" }),
      entry({ kind: "file", path: "state/openclaw.sqlite" }),
      entry({ path: "unknown/a.txt" }),
      entry({ path: "workspace/cache.tmp" }),
      entry({ path: "workspace/cache.tmp/child.txt" }),
      entry({ path: "workspace/AGENTS.md" }),
      entry({ path: "workspace/.personal-operator-ready.json" }),
      entry({ path: "workspace/.ssh/config" }),
      entry({ path: "workspace/cache.sqlite-wal" }),
    ]) {
      expectManifestError(
        () => validateManifest(manifest({ entries: [invalidEntry] })),
        "WORKSPACE_MANIFEST_INVALID",
      );
    }
    assert.doesNotThrow(() =>
      validateManifest(
        manifest({
          entries: [
            entry({
              kind: "sqlite",
              path: "agents/main/agent/openclaw-agent.sqlite",
            }),
          ],
        }),
      ),
    );
  });

  it("enforces entry count, file, generation, and encoded manifest limits", () => {
    expectManifestError(
      () => validateManifest(manifest({ entries: [entry(), entry({ path: "workspace/b" })] }), { maxEntries: 1 }),
      "WORKSPACE_MANIFEST_LIMIT",
    );
    expectManifestError(
      () => validateManifest(manifest({ entries: [entry({ size: 11 })] }), { maxFileBytes: 10 }),
      "WORKSPACE_MANIFEST_LIMIT",
    );
    expectManifestError(
      () => validateManifest(manifest({ entries: [entry({ size: 6 }), entry({ path: "workspace/b", size: 6 })] }), { maxGenerationBytes: 10 }),
      "WORKSPACE_MANIFEST_LIMIT",
    );
    expectManifestError(
      () => parseManifest(encodeManifest(manifest()), { maxManifestBytes: 8 }),
      "WORKSPACE_MANIFEST_LIMIT",
    );
  });

  it("rejects wrong format, self-parent, extra fields, and unsafe integer sizes", () => {
    for (const value of [
      manifest({ format: "workspace.v0" }),
      manifest({ parent: G1 }),
      { ...manifest(), extra: true },
      manifest({ entries: [entry({ size: -1 })] }),
      manifest({ entries: [entry({ size: 1.5 })] }),
    ]) {
      expectManifestError(() => validateManifest(value), "WORKSPACE_MANIFEST_INVALID");
    }
  });
});

describe("canonical current pointer", () => {
  it("encodes and parses the exact pointer shape without a newline", () => {
    const bytes = encodePointer(pointer({ parent: G2 }));
    assert.equal(
      bytes.toString("utf8"),
      `{"committedAt":"2026-07-18T01:02:03.004Z","format":"personal-operator.workspace-current.v1","generation":"${G1}","manifestSha256":"${SHA_B}","parent":"${G2}"}`,
    );
    assert.deepEqual(parsePointer(bytes), pointer({ parent: G2 }));
  });

  it("rejects noncanonical bytes and non-millisecond UTC timestamps", () => {
    expectManifestError(
      () => parsePointer(Buffer.from(`${encodePointer(pointer()).toString("utf8")}\n`)),
      "WORKSPACE_JSON_NONCANONICAL",
    );
    for (const committedAt of [
      "2026-07-18T01:02:03Z",
      "2026-07-18T01:02:03.004+00:00",
      "not-a-date",
    ]) {
      expectManifestError(
        () => validatePointer(pointer({ committedAt })),
        "WORKSPACE_POINTER_INVALID",
      );
    }
  });

  it("rejects wrong format, self-parent, invalid digest, and extra fields", () => {
    for (const value of [
      pointer({ format: "workspace-current.v0" }),
      pointer({ parent: G1 }),
      pointer({ manifestSha256: SHA_B.toUpperCase() }),
      { ...pointer(), etag: "rival" },
    ]) {
      expectManifestError(() => validatePointer(value), "WORKSPACE_POINTER_INVALID");
    }
  });
});

describe("sha256Hex", () => {
  it("hashes exact bytes as lowercase hex", () => {
    assert.equal(
      sha256Hex(Buffer.from("abc")),
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
  });
});
