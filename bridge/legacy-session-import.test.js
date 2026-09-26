/**
 * Tests for legacy-session-import.js — the receipt that stops the pre-2.0
 * sessions.json import from re-running on every cold start once the 2.0
 * SQLite store (which now gets backed up, see workspace-sync large-database
 * tests) is restored next to the restored 1.x index — and for its wiring in
 * agentcore-contract.js.
 *
 * Background (us-west-2 rehearsal, F1): S3 keeps the 1.x files forever (no
 * deletes are propagated; they are the rollback path), so every cold start
 * restores `agents/main/sessions/sessions.json` again. The 2.0 gateway refuses
 * readiness while that file exists, so the contract runs `openclaw doctor
 * --fix` (53 s for 990 sessions) — which, with the SQLite store now restored
 * too, would re-import over 2.0's own session entries at every boot.
 *
 * Run: cd bridge && node --test legacy-session-import.test.js
 */
const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");

const mod = require("./legacy-session-import");

describe("legacy-session-import receipts", () => {
  let dir, p, warnings;
  const log = { warn: (m) => warnings.push(m), log: () => {} };
  const sha = (file) => crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");

  beforeEach(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "legacy-import-"));
    p = mod.agentPaths(dir, "main");
    warnings = [];
    fs.mkdirSync(path.dirname(p.legacyIndex), { recursive: true });
    fs.mkdirSync(path.dirname(p.sqliteStore), { recursive: true });
    fs.writeFileSync(p.legacyIndex, JSON.stringify({ "agent:main:telegram:direct:1": { sessionId: "ses_1" } }));
  });

  afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

  it("finds legacy indexes per agent like the contract did (and nothing on a fresh install)", () => {
    assert.deepEqual(mod.findLegacySessionStores(dir), [p.legacyIndex]);
    assert.deepEqual(mod.findLegacySessionStores(path.join(dir, "nope")), []);
    fs.writeFileSync(path.join(dir, "agents", "not-a-dir"), "x");
    assert.deepEqual(mod.findLegacySessionStores(dir), [p.legacyIndex]);
  });

  it("first boot: no receipt -> the index must be imported", () => {
    assert.deepEqual(mod.planLegacyImport(dir), { toImport: [p.legacyIndex], alreadyImported: [] });
  });

  it("records a receipt (sha256 of the imported index) next to the store, only when the store exists", () => {
    const fingerprints = mod.fingerprintIndexes([p.legacyIndex]);
    assert.deepEqual([...fingerprints], [[p.legacyIndex, sha(p.legacyIndex)]]);
    // Doctor archives the index and creates the store...
    fs.unlinkSync(p.legacyIndex);

    // ...but if the store is NOT there, no receipt: a receipt without a store
    // would make the next boot skip an import that never happened.
    assert.deepEqual(mod.writeReceipts(dir, fingerprints, log), []);
    assert.equal(fs.existsSync(p.receipt), false);
    assert.match(warnings[0], /No openclaw-agent\.sqlite for agent main/);

    fs.writeFileSync(p.sqliteStore, "SQLite format 3\0...");
    assert.deepEqual(mod.writeReceipts(dir, fingerprints, log), [p.receipt]);
    const receipt = JSON.parse(fs.readFileSync(p.receipt, "utf8"));
    assert.equal(receipt.version, 1);
    assert.equal(receipt.agentId, "main");
    assert.equal(receipt.indexSha256, fingerprints.get(p.legacyIndex));
    assert.equal(receipt.store, "openclaw-agent.sqlite");
    assert.ok(Date.parse(receipt.importedAt) > 0);
    assert.deepEqual(fs.readdirSync(path.dirname(p.receipt)).filter((f) => f.includes(".tmp-")), [], "written atomically");
  });

  it("next cold start: the restored index matches the receipt and the store is present -> moved aside, not imported", () => {
    const indexBytes = fs.readFileSync(p.legacyIndex);
    const fingerprints = mod.fingerprintIndexes([p.legacyIndex]);
    fs.unlinkSync(p.legacyIndex);
    fs.writeFileSync(p.sqliteStore, "SQLite format 3\0...");
    mod.writeReceipts(dir, fingerprints, log);

    // S3 restore brings the same sessions.json back next to the store + receipt.
    fs.writeFileSync(p.legacyIndex, indexBytes);
    const plan = mod.planLegacyImport(dir);
    assert.deepEqual(plan, {
      toImport: [],
      alreadyImported: [{ legacyIndex: p.legacyIndex, importedIndex: p.importedIndex, sqliteStore: p.sqliteStore }],
    });
    assert.equal(mod.moveImportedIndexAside(plan.alreadyImported[0], log), true);
    assert.equal(fs.existsSync(p.legacyIndex), false, "gateway no longer sees a legacy store");
    assert.equal(fs.readFileSync(p.importedIndex).toString(), indexBytes.toString(), "kept locally under the moved-aside name");
    assert.equal(mod.IMPORTED_INDEX_NAME, "sessions.json.pre-2.0-imported");

    // A second restart with the moved-aside copy already there: overwritten, no error.
    fs.writeFileSync(p.legacyIndex, indexBytes);
    assert.equal(mod.moveImportedIndexAside(mod.planLegacyImport(dir).alreadyImported[0], log), true);
    assert.deepEqual(mod.findLegacySessionStores(dir), []);
  });

  it("an index that changed since the import (1.x wrote sessions during a rollback) is imported again", () => {
    const fingerprints = mod.fingerprintIndexes([p.legacyIndex]);
    fs.unlinkSync(p.legacyIndex);
    fs.writeFileSync(p.sqliteStore, "SQLite format 3\0...");
    mod.writeReceipts(dir, fingerprints, log);

    fs.writeFileSync(p.legacyIndex, JSON.stringify({ "agent:main:telegram:direct:1": { sessionId: "ses_1" }, "agent:main:telegram:direct:2": { sessionId: "ses_2" } }));
    assert.deepEqual(mod.planLegacyImport(dir), { toImport: [p.legacyIndex], alreadyImported: [] });
  });

  it("a receipt whose store is missing (its backup never landed) does not skip the import", () => {
    const fingerprints = mod.fingerprintIndexes([p.legacyIndex]);
    fs.writeFileSync(p.sqliteStore, "SQLite format 3\0...");
    mod.writeReceipts(dir, fingerprints, log);
    fs.unlinkSync(p.sqliteStore); // restored without the store
    assert.deepEqual(mod.planLegacyImport(dir), { toImport: [p.legacyIndex], alreadyImported: [] });
  });

  it("an unreadable or foreign receipt is ignored", () => {
    fs.writeFileSync(p.sqliteStore, "SQLite format 3\0...");
    fs.writeFileSync(p.receipt, "{not json");
    assert.deepEqual(mod.planLegacyImport(dir).toImport, [p.legacyIndex]);
    fs.writeFileSync(p.receipt, JSON.stringify({ version: 99, indexSha256: sha(p.legacyIndex) }));
    assert.deepEqual(mod.planLegacyImport(dir).toImport, [p.legacyIndex]);
  });

  it("a failed move-aside falls back to importing", () => {
    const fingerprints = mod.fingerprintIndexes([p.legacyIndex]);
    fs.writeFileSync(p.sqliteStore, "SQLite format 3\0...");
    mod.writeReceipts(dir, fingerprints, log);
    const item = mod.planLegacyImport(dir).alreadyImported[0];
    assert.equal(mod.moveImportedIndexAside({ ...item, importedIndex: path.join(dir, "no", "such", "dir", "x") }, log), false);
    assert.match(warnings.at(-1), /Could not move aside .* importing it again instead/);
    assert.equal(fs.existsSync(p.legacyIndex), true);
  });

  it("the moved-aside index and doctor's import archive are never backed up to S3", () => {
    delete require.cache[require.resolve("./workspace-sync")];
    process.env.S3_USER_FILES_BUCKET = "test-bucket";
    const { shouldSkip } = require("./workspace-sync");
    delete process.env.S3_USER_FILES_BUCKET;
    assert.ok(shouldSkip("agents/main/sessions/sessions.json.pre-2.0-imported"));
    assert.ok(shouldSkip("agents/main/session-sqlite-import-archive/sessions.json"));
    assert.ok(shouldSkip("agents/main/session-sqlite-import-archive/ses_1.jsonl"));
    assert.equal(shouldSkip("agents/main/sessions/sessions.json"), false, "the original index is still backed up (rollback path)");
    assert.equal(shouldSkip("agents/main/agent/.pre-2.0-import.json"), false, "the receipt IS backed up — it must travel with the store");
    assert.equal(shouldSkip("agents/main/agent/openclaw-agent.sqlite"), false);
  });
});

describe("legacy import wiring in agentcore-contract.js", () => {
  const source = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf-8");
  const idx = (needle) => {
    const i = source.indexOf(needle);
    assert.ok(i >= 0, `contract source should contain: ${needle}`);
    return i;
  };

  it("plans the import through the receipt module and moves already-imported indexes aside before deciding to run doctor", () => {
    const plan = idx("const plan = legacySessionImport.planLegacyImport(OPENCLAW_DIR);");
    const aside = idx("legacySessionImport.moveImportedIndexAside(item)");
    const nothing = idx("if (legacy.length === 0) return false; // nothing to migrate");
    const spawnDoctor = idx('["doctor", "--fix", "--non-interactive"]');
    assert.ok(plan < aside && aside < nothing && nothing < spawnDoctor);
  });

  it("fingerprints the indexes BEFORE doctor archives them and records receipts only on a successful import", () => {
    const fp = idx("const fingerprints = legacySessionImport.fingerprintIndexes(legacy);");
    const spawnDoctor = idx('["doctor", "--fix", "--non-interactive"]');
    const success = idx("if (result.code === 0 && remaining.length === 0) {");
    const write = idx("legacySessionImport.writeReceipts(OPENCLAW_DIR, fingerprints)");
    const quarantine = idx("Import did not complete. Quarantine what is left");
    assert.ok(fp < spawnDoctor, "hash before doctor runs");
    assert.ok(success < write && write < quarantine, "receipts written inside the success branch only");
  });

  it("ships the module in the image", () => {
    const dockerfile = fs.readFileSync(path.join(__dirname, "Dockerfile"), "utf-8");
    assert.ok(dockerfile.includes("COPY legacy-session-import.js /app/legacy-session-import.js"));
    assert.ok(source.includes('require("./legacy-session-import")'));
  });
});
