const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { PATCHES, patchSource, patchPackage, sha256 } = require("./patch-openclaw-snapshot");
const base = require("./patch-openclaw");
const preinit = require("./patch-openclaw-preinit");
const manifest = require("./patches/runtime-v2.json");

test("snapshot delta contains only pinned functional changes", () => {
  assert.equal(new Set(PATCHES.map(pin => pin.file)).size, PATCHES.length);
  assert.equal(PATCHES.some(pin => pin.file === "dist/server-methods-C72cO74x.mjs"), false);
  for (const pin of PATCHES) {
    assert.match(pin.originalSha256, /^[a-f0-9]{64}$/);
    assert.match(pin.patchedSha256, /^[a-f0-9]{64}$/);
    assert.notEqual(pin.originalSha256, pin.patchedSha256);
    assert.ok(pin.replacements.length);
    assert.throws(() => patchSource(pin.file, Buffer.from(pin.replacements[0].before)), /SHA-256/);
    const delta = pin.replacements.map(change => change.after).join("\n");
    assert.doesNotMatch(delta, /\/app\/(?:.*diagnostic|.*probe|.*timing|chat-preparation-detail|jiti)/);
    assert.doesNotMatch(delta, /OPENCLAW_DIAGNOSTICS_/);
    assert.doesNotMatch(delta, /liveSeedChatHandlerWitness|observeLiveSeedReplyRuntime/);
    // The shared live seed was removed: no replacement may keep one of its hooks.
    assert.doesNotMatch(delta, /liveGatewaySeed|liveSeedReplyRuntimeWitness|session-diff-worker|seedQuiesce|seedHeartbeat/);
  }
  // Only the SQLite receiver preparation used on every activation remains.
  assert.deepEqual(PATCHES.map(pin => pin.file).sort(), [
    "dist/execution-identity-admission-Bf_lkljE.mjs", "dist/infra/sqlite-store.worker.js",
    "dist/sqlite-worker-store-CpUHa3pA.mjs", "node_modules/tslog/esm/index.node.js",
  ]);
  assert.throws(() => patchSource("dist/unknown.js", Buffer.alloc(0)), /Unsupported/);
});

const packageDir = process.env.OPENCLAW_PACKAGE_DIR;
test("offline npm source applies base, preinit and snapshot patches idempotently", {
  skip: !packageDir && "Set OPENCLAW_PACKAGE_DIR to a read-only OpenClaw 2026.9.5 source fixture",
}, t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "openclaw-snapshot-patch-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const files = [...new Set(["package.json", base.TARGET_FILE, ...preinit.PATCHES.map(pin => pin.file),
    ...Object.keys(preinit.PINNED_RUNTIME_FILES), ...PATCHES.map(pin => pin.file)])];
  for (const file of files) {
    fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    fs.copyFileSync(path.join(packageDir, file), path.join(root, file));
  }
  assert.throws(() => patchPackage(root), /base and preinit/);
  base.patchPackage(root);
  preinit.patchPackage(root);
  const last = PATCHES.at(-1);
  fs.appendFileSync(path.join(root, last.file), "\n");
  const write = t.mock.method(fs, "writeFileSync", () => assert.fail("No partial patch writes"));
  assert.throws(() => patchPackage(root), /SHA-256/);
  write.mock.restore();
  fs.copyFileSync(path.join(packageDir, last.file), path.join(root, last.file));
  const result = patchPackage(root);
  assert.equal(result.changed, true);
  for (const pin of PATCHES) assert.equal(sha256(fs.readFileSync(path.join(root, pin.file))), pin.patchedSha256);
  const again = t.mock.method(fs, "writeFileSync", () => assert.fail("Idempotent patch must not rewrite"));
  assert.equal(patchPackage(root).changed, false);
  again.mock.restore();
  for (const [file, digest] of Object.entries(manifest.requires)) {
    assert.equal(sha256(fs.readFileSync(path.join(root, file))), digest);
  }
});
