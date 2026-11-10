const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { PATCHES, patchSource, patchPackage, sha256 } = require("./patch-openclaw-snapshot");
const base = require("./patch-openclaw-delivery-expiry");
const preinit = require("./patch-openclaw-preinit");
const manifest = require("./patches/runtime-v2.json");

test("snapshot delta contains only pinned functional changes", () => {
  assert.equal(manifest.version, "2026.11.2");
  assert.equal(new Set(PATCHES.map(pin => pin.file)).size, PATCHES.length);
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
    // Vendor files may import only the bridge's SQLite preparation modules.
    for (const [, specifier] of delta.matchAll(/from "(\/app\/[^"]+)"/g)) {
      assert.match(specifier, /^\/app\/sqlite-(?:backend-prepare|receiver-control|receiver-reservation)\.mjs$/);
    }
  }
  // Only the SQLite receiver preparation used on every activation remains. 2026.11.2 has no
  // module-level runtime instance id in the backend graph, so execution-identity-admission is not patched.
  assert.deepEqual(PATCHES.map(pin => pin.file).sort(), [
    "dist/infra/sqlite-store.worker.js", "dist/sqlite-worker-store-CpfrwHqQ.mjs", "node_modules/tslog/esm/index.node.js",
  ]);
  assert.deepEqual(manifest.requires, {
    [base.TARGET_FILE]: base.PATCHED_SHA256,
    [preinit.PRESENCE_FILE]: preinit.PATCHES.find(pin => pin.file === preinit.PRESENCE_FILE).patchedSha256,
  });
  assert.throws(() => patchSource("dist/unknown.js", Buffer.alloc(0)), /Unsupported/);
});

test("the store worker imports the preparation module before any OpenClaw chunk", () => {
  const carrier = PATCHES.find(pin => pin.file === "dist/infra/sqlite-store.worker.js");
  const first = carrier.replacements[0];
  assert.ok(first.after.startsWith('import { BACKEND_MODULE_URL, backendPreparationEnabled, '));
  assert.ok(first.after.indexOf('"/app/sqlite-backend-prepare.mjs"') < first.after.indexOf('"../'));
  assert.ok(first.before.startsWith('import { a as scheduleOpenClawCompileCacheFlush } from "../node-compile-cache-'));
});

const packageDir = process.env.OPENCLAW_PACKAGE_DIR;
test("offline npm source applies base, preinit and snapshot patches idempotently", {
  skip: !packageDir && "Set OPENCLAW_PACKAGE_DIR to a read-only OpenClaw 2026.11.2 source fixture",
}, t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "openclaw-snapshot-patch-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const files = [...new Set(["package.json", base.TARGET_FILE, ...preinit.PATCHES.map(pin => pin.file),
    ...Object.keys(preinit.PINNED_RUNTIME_FILES), ...PATCHES.map(pin => pin.file)])];
  for (const file of files) {
    fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    fs.copyFileSync(path.join(packageDir, file), path.join(root, file));
  }
  // The store worker must begin with the anchored import so the preparation module loads first.
  const worker = fs.readFileSync(path.join(root, "dist/infra/sqlite-store.worker.js"), "utf8");
  assert.ok(worker.startsWith(PATCHES.find(pin => pin.file === "dist/infra/sqlite-store.worker.js").replacements[0].before));
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
