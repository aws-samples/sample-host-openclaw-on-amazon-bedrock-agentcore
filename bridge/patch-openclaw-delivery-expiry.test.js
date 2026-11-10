/**
 * Unit tests for the suspended sub-agent delivery expiry patch.
 * OPENCLAW_PACKAGE_DIR enables source-backed tests; the package is read-only.
 * Run: node --test patch-openclaw-delivery-expiry.test.js
 */
const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const {
  OPENCLAW_VERSION, TARGET_FILE, ORIGINAL_SHA256, PATCHED_SHA256, EXPIRY_MS,
  OLD_ANCHOR, NEW_ANCHOR, sha256, replaceExpiry, patchSource, patchPackage,
} = require("./patch-openclaw-delivery-expiry");

function makePackage(t, source, manifest = { name: "openclaw", version: OPENCLAW_VERSION }) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "openclaw-delivery-expiry-unit-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, "dist"));
  fs.writeFileSync(path.join(root, "package.json"), JSON.stringify(manifest));
  fs.writeFileSync(path.join(root, TARGET_FILE), source);
  return root;
}

function assertRefusedWithoutWrites(t, root, expected) {
  const target = path.join(root, TARGET_FILE);
  const before = fs.readFileSync(target);
  const write = t.mock.method(fs, "writeFileSync", () => assert.fail("Rejected packages must not be written."));
  assert.throws(() => patchPackage(root), expected);
  assert.equal(write.mock.callCount(), 0);
  write.mock.restore();
  assert.deepEqual(fs.readFileSync(target), before);
}

describe("pinned patch validation", () => {
  it("lowers only the 7-day threshold to 2 hours", () => {
    assert.equal(EXPIRY_MS, 72e5);
    assert.equal(OLD_ANCHOR.replace("6048e5", "72e5"), NEW_ANCHOR);
    const source = `before\n\t${OLD_ANCHOR}\nafter\n`;
    assert.equal(replaceExpiry(source), `before\n\t${NEW_ANCHOR}\nafter\n`);
  });

  it("rejects absent or repeated anchors", () => {
    for (const source of ["", NEW_ANCHOR, `${OLD_ANCHOR}\n${OLD_ANCHOR}`]) {
      assert.throws(() => replaceExpiry(source), /Expected exactly one/);
    }
  });

  it("rejects an unknown hash even when the anchor matches", (t) => {
    const root = makePackage(t, `${OLD_ANCHOR}\n`);
    assertRefusedWithoutWrites(t, root, /Unsupported OpenClaw sub-agent registry source SHA-256/);
  });

  it("rejects unknown, missing and non-string package versions and other packages", (t) => {
    for (const manifest of [{ name: "openclaw", version: "2026.9.5" }, { name: "openclaw" },
      { name: "openclaw", version: 2026.112 }, { name: "other", version: OPENCLAW_VERSION }, null]) {
      assertRefusedWithoutWrites(t, makePackage(t, OLD_ANCHOR, manifest), /Expected OpenClaw package version 2026\.11\.2/);
    }
    const root = makePackage(t, OLD_ANCHOR);
    fs.writeFileSync(path.join(root, "package.json"), "private-invalid-manifest");
    assertRefusedWithoutWrites(t, root, { message: "Cannot read a valid OpenClaw package.json." });
  });

  it("requires the exact pinned file", (t) => {
    const root = makePackage(t, OLD_ANCHOR);
    fs.renameSync(path.join(root, TARGET_FILE), path.join(root, "dist/subagent-registry-other.mjs"));
    assert.throws(() => patchPackage(root), { code: "ENOENT" });
  });
});

// Compile the sweeper's suspended-delivery branch with its helpers stubbed.
function compileSuspendedBranch(source) {
  const start = source.indexOf("\t\t\t\tif (isSuspendedPendingFinalDelivery(entry)) {");
  assert.ok(start > 0, "pinned source must have the suspended delivery branch");
  const end = source.indexOf("\t\t\t\t\tcontinue;\n\t\t\t\t}\n", start);
  assert.ok(end > start);
  const block = source.slice(start, end + "\t\t\t\t\tcontinue;\n\t\t\t\t}\n".length);
  return vm.compileFunction(`return async (entry, now, runId, params) => {
    for (const _ of [0]) {
${block}
      return "kept-going";
    }
    return "handled";
  };`, ["isSuspendedPendingFinalDelivery", "discardSuspendedPendingFinalDelivery"]);
}

const packageDir = process.env.OPENCLAW_PACKAGE_DIR;
describe("pinned OpenClaw source", {
  skip: packageDir ? false : "Set OPENCLAW_PACKAGE_DIR to the unmodified pinned OpenClaw package.",
}, () => {
  let original, patched, manifest;
  before(() => {
    manifest = fs.readFileSync(path.join(packageDir, "package.json"));
    assert.equal(JSON.parse(manifest).version, OPENCLAW_VERSION);
    original = fs.readFileSync(path.join(packageDir, TARGET_FILE));
    assert.equal(sha256(original), ORIGINAL_SHA256);
    patched = patchSource(original).source;
    assert.equal(sha256(patched), PATCHED_SHA256);
  });
  after(() => {
    if (original) assert.deepEqual(fs.readFileSync(path.join(packageDir, TARGET_FILE)), original);
    if (manifest) assert.deepEqual(fs.readFileSync(path.join(packageDir, "package.json")), manifest);
  });

  it("changes nothing but the threshold and is idempotent", (t) => {
    assert.equal(patched.toString("utf8"), original.toString("utf8").replace(OLD_ANCHOR, NEW_ANCHOR));
    const root = makePackage(t, original, JSON.parse(manifest));
    assert.equal(patchPackage(root).changed, true);
    assert.deepEqual(fs.readFileSync(path.join(root, TARGET_FILE)), patched);
    const write = t.mock.method(fs, "writeFileSync", () => assert.fail("Already patched packages must not be written."));
    assert.equal(patchPackage(root).changed, false);
    write.mock.restore();
    assertRefusedWithoutWrites(t, makePackage(t, Buffer.concat([patched, Buffer.from("\n")])), /Unsupported/);
  });

  it("discards a suspended delivery after 2 hours instead of 7 days", async () => {
    const now = Date.UTC(2026, 10, 10);
    for (const [label, buffer, cutoff] of [["original", original, 6048e5], ["patched", patched, EXPIRY_MS]]) {
      const discarded = [];
      const branch = compileSuspendedBranch(buffer.toString("utf8"))(() => true, async (input) => {
        discarded.push(input.runId);
      });
      const params = { isCleanupOwnerCurrent: () => true };
      for (const age of [EXPIRY_MS - 1, EXPIRY_MS, 6048e5 - 1, 6048e5]) {
        const entry = { delivery: { status: "suspended", suspendedAt: now - age } };
        assert.equal(await branch(entry, now, `run-${age}`, params), "handled", label);
      }
      assert.deepEqual(discarded, [EXPIRY_MS, 6048e5 - 1, 6048e5].filter((age) => age >= cutoff)
        .map((age) => `run-${age}`), label);
    }
  });
});
