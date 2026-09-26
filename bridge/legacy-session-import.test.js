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
    const nothing = idx("if (legacy.length === 0 && !forceDoctor) return false; // nothing to migrate");
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

describe("retired-files manifest: files doctor removed must not come back from S3 (F5)", () => {
  // Background (us-west-2 F1 staging test, live user's 224 MB 1.x state): the
  // first 2.0 boot ran doctor, which imported sessions.json and ALSO removed /
  // archived workspace-state.json, auth-profiles.json, exec-approvals.json and
  // 990 .jsonl transcripts. A same-session restart's fill-missing restore
  // brought all 1010 back from S3; the receipt skipped the sessions.json
  // import, doctor did not run, and the gateway exited 78 three times
  // ("Legacy workspace setup state requires migration") -> shim forever.
  let dir, warnings;
  const log = { warn: (m) => warnings.push(m), log: () => {} };
  const write = (rel, body) => {
    const full = path.join(dir, rel);
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(full, body);
    return full;
  };
  const exists = (rel) => fs.existsSync(path.join(dir, rel));

  // What the state dir looked like before doctor on the upgrade boot.
  const LEGACY = {
    "workspace/.openclaw/workspace-state.json": '{"setup":"legacy"}',
    "agents/main/agent/auth-profiles.json": '{"profiles":{}}',
    "agents/main/exec-approvals.json": "{}",
    "agents/main/sessions/sessions.json": '{"agent:main:telegram:direct:1":{"sessionId":"ses_1"}}',
    "agents/main/sessions/ses_1.jsonl": '{"type":"message"}\n',
  };
  const KEPT = {
    "workspace/notes.md": "# keep me",
    "state/openclaw.sqlite": "sqlite-bytes",
  };

  beforeEach(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "legacy-retired-"));
    warnings = [];
    for (const [rel, body] of Object.entries({ ...LEGACY, ...KEPT })) write(rel, body);
  });
  afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

  // Simulates `openclaw doctor --fix`: import, archive the transcripts, retire the rest.
  const runDoctor = () => {
    write("agents/main/agent/openclaw-agent.sqlite", "imported-store");
    write("agents/main/session-sqlite-import-archive/ses_1.jsonl", LEGACY["agents/main/sessions/ses_1.jsonl"]);
    for (const rel of Object.keys(LEGACY)) fs.unlinkSync(path.join(dir, rel));
  };

  it("records exactly the files doctor removed (path + sha256 of the bytes it had), not the kept ones", () => {
    const before = mod.fingerprintStateDir(dir);
    assert.equal(before.size, Object.keys(LEGACY).length + Object.keys(KEPT).length);
    runDoctor();
    const retired = mod.diffRetiredFiles(before, dir);
    assert.deepEqual(
      retired.map((f) => f.path),
      Object.keys(LEGACY).sort(),
    );
    const wsState = retired.find((f) => f.path === "workspace/.openclaw/workspace-state.json");
    assert.equal(wsState.sha256, crypto.createHash("sha256").update(LEGACY[wsState.path]).digest("hex"));

    const written = mod.writeRetiredManifest(dir, retired, log);
    assert.equal(written.path, path.join(dir, mod.RETIRED_MANIFEST_NAME));
    const manifest = mod.readRetiredManifest(dir);
    assert.equal(manifest.version, 1);
    assert.deepEqual(manifest.files, retired);
    assert.deepEqual(warnings, []);
  });

  it("does not fingerprint files above the size cap and never lists the receipt or the manifest itself", () => {
    write("workspace/big.bin", Buffer.alloc(2048));
    const before = mod.fingerprintStateDir(dir, { maxBytes: 1024 });
    assert.ok(!before.has("workspace/big.bin"));
    write("agents/main/agent/" + mod.RECEIPT_NAME, "{}");
    write(mod.RETIRED_MANIFEST_NAME, "{}");
    const before2 = mod.fingerprintStateDir(dir);
    fs.unlinkSync(path.join(dir, "agents/main/agent/" + mod.RECEIPT_NAME));
    fs.unlinkSync(path.join(dir, mod.RETIRED_MANIFEST_NAME));
    const retired = mod.diffRetiredFiles(before2, dir).map((f) => f.path);
    assert.ok(!retired.includes(mod.RETIRED_MANIFEST_NAME));
    assert.ok(!retired.some((p) => p.endsWith(mod.RECEIPT_NAME)));
  });

  it("the resurrection: after a restore brings the retired files back byte-identical, prune removes them and leaves everything else", () => {
    const before = mod.fingerprintStateDir(dir);
    runDoctor();
    mod.writeRetiredManifest(dir, mod.diffRetiredFiles(before, dir), log);

    // Restore from S3 (which never saw the deletes): every legacy file is back.
    for (const [rel, body] of Object.entries(LEGACY)) write(rel, body);
    for (const rel of Object.keys(LEGACY)) assert.ok(exists(rel), `${rel} restored`);

    const result = mod.pruneRetiredFiles(dir, log);
    assert.deepEqual(result.pruned.sort(), Object.keys(LEGACY).sort());
    assert.deepEqual(result.changed, []);
    for (const rel of Object.keys(LEGACY)) assert.ok(!exists(rel), `${rel} pruned again`);
    for (const rel of Object.keys(KEPT)) assert.ok(exists(rel), `${rel} kept`);
    assert.ok(exists("agents/main/agent/openclaw-agent.sqlite"), "2.0 store kept");
    assert.ok(exists("agents/main/session-sqlite-import-archive/ses_1.jsonl"), "doctor's archive kept");
    assert.ok(exists(mod.RETIRED_MANIFEST_NAME), "manifest kept for the next restore");
    // With sessions.json pruned there is nothing left for doctor to import.
    assert.deepEqual(mod.planLegacyImport(dir), { toImport: [], alreadyImported: [] });

    // Second restore, second prune: idempotent, absent files just counted.
    const again = mod.pruneRetiredFiles(dir, log);
    assert.deepEqual(again, { pruned: [], changed: [], absent: Object.keys(LEGACY).length });
    assert.deepEqual(warnings, []);
  });

  it("a recorded file that came back with DIFFERENT bytes (1.x wrote it during a rollback) is kept and reported", () => {
    const before = mod.fingerprintStateDir(dir);
    runDoctor();
    mod.writeRetiredManifest(dir, mod.diffRetiredFiles(before, dir), log);

    for (const [rel, body] of Object.entries(LEGACY)) write(rel, body);
    write("agents/main/sessions/sessions.json", '{"agent:main:telegram:direct:1":{"sessionId":"ses_1"},"new":{"sessionId":"ses_2"}}');

    const result = mod.pruneRetiredFiles(dir, log);
    assert.deepEqual(result.changed, ["agents/main/sessions/sessions.json"]);
    assert.ok(exists("agents/main/sessions/sessions.json"), "changed index left for doctor");
    assert.equal(result.pruned.length, Object.keys(LEGACY).length - 1);
    // ...and it is now due for import (no receipt for these bytes).
    assert.deepEqual(mod.planLegacyImport(dir).toImport, [path.join(dir, "agents/main/sessions/sessions.json")]);
  });

  it("a second import merges into the existing manifest instead of forgetting the first one's files", () => {
    const before = mod.fingerprintStateDir(dir);
    runDoctor();
    mod.writeRetiredManifest(dir, mod.diffRetiredFiles(before, dir), log);
    // Rollback wrote a new index; the upgrade boot imports it again.
    write("agents/main/sessions/sessions.json", '{"v2":true}');
    const before2 = mod.fingerprintStateDir(dir);
    fs.unlinkSync(path.join(dir, "agents/main/sessions/sessions.json"));
    const written = mod.writeRetiredManifest(dir, mod.diffRetiredFiles(before2, dir), log);
    assert.equal(written.files.length, Object.keys(LEGACY).length);
    const idx = written.files.find((f) => f.path === "agents/main/sessions/sessions.json");
    assert.equal(idx.sha256, crypto.createHash("sha256").update('{"v2":true}').digest("hex"), "newest hash wins");
  });

  it("ignores manifest entries that point outside the state dir, and tolerates a missing or corrupt manifest", () => {
    assert.deepEqual(mod.pruneRetiredFiles(dir, log), { pruned: [], changed: [], absent: 0 });
    write(mod.RETIRED_MANIFEST_NAME, "not json");
    assert.deepEqual(mod.pruneRetiredFiles(dir, log), { pruned: [], changed: [], absent: 0 });
    const outside = path.join(os.tmpdir(), `legacy-retired-outside-${process.pid}`);
    fs.writeFileSync(outside, "x");
    try {
      write(
        mod.RETIRED_MANIFEST_NAME,
        JSON.stringify({ version: 1, files: [{ path: path.relative(dir, outside), sha256: crypto.createHash("sha256").update("x").digest("hex") }] }),
      );
      assert.deepEqual(mod.pruneRetiredFiles(dir, log), { pruned: [], changed: [], absent: 0 });
      assert.ok(fs.existsSync(outside), "file outside the state dir untouched");
    } finally {
      fs.rmSync(outside, { force: true });
    }
  });
});

describe("retired-files wiring in agentcore-contract.js (F5)", () => {
  const source = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf-8");
  const idx = (needle) => {
    const i = source.indexOf(needle);
    assert.ok(i >= 0, `contract source should contain: ${needle}`);
    return i;
  };

  it("fingerprints the state dir BEFORE doctor and records the retired files only on a successful import", () => {
    const fp = idx("const stateBeforeDoctor = legacySessionImport.fingerprintStateDir(OPENCLAW_DIR);");
    const spawnDoctor = idx('["doctor", "--fix", "--non-interactive"]');
    const success = idx("if (result.code === 0 && remaining.length === 0) {");
    const diff = idx("legacySessionImport.diffRetiredFiles(stateBeforeDoctor, OPENCLAW_DIR)");
    const manifest = idx("legacySessionImport.writeRetiredManifest(OPENCLAW_DIR, retired)");
    const quarantine = idx("Import did not complete. Quarantine what is left");
    assert.ok(fp < spawnDoctor, "fingerprint before doctor runs");
    assert.ok(success < diff && diff < manifest && manifest < quarantine, "manifest written inside the success branch only");
  });

  it("prunes resurrected files after EVERY restore, before the config write and before deciding on doctor", () => {
    const restored = idx("await workspaceSync.awaitRestore(restorePromise, RESTORE_WAIT_MS);");
    const prune = idx("const resurrected = pruneResurrectedLegacyFiles();");
    const config = source.indexOf("writeOpenClawConfig();", restored);
    const doctor = idx("migrateLegacySessionStore(openclawEnv, { forceDoctor: resurrected.changed.length > 0 })");
    assert.ok(restored < prune && prune < config && config < doctor);
    assert.ok(source.includes("legacySessionImport.pruneRetiredFiles(OPENCLAW_DIR)"));
  });

  it("a changed retired file forces doctor even when no legacy index is present", () => {
    idx("async function migrateLegacySessionStore(env, { forceDoctor = false } = {})");
    idx("if (legacy.length === 0 && !forceDoctor) return false; // nothing to migrate");
  });
});

describe("import records reach S3 immediately (F6)", () => {
  // us-west-2 F1 staging test: two full upgrade boots each wrote the receipt and
  // the retired-files manifest, and neither object ever appeared in S3. They are
  // written before the gateway spawns, i.e. before the change watcher starts;
  // the periodic save is 30 min away in backup mode; an idle stop sends no
  // SIGTERM. So every later cold start re-ran doctor (39-52 s) and the F5 prune
  // had no manifest — both fixes were inert on the cold-start path.
  const source = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf-8");
  const idx = (needle) => {
    const i = source.indexOf(needle);
    assert.ok(i >= 0, `contract source should contain: ${needle}`);
    return i;
  };

  it("uploads the receipt(s) and the manifest through workspaceSync.saveFile right after writing them, inside the success branch", () => {
    const success = idx("if (result.code === 0 && remaining.length === 0) {");
    const receipts = idx("const receipts = legacySessionImport.writeReceipts(OPENCLAW_DIR, fingerprints);");
    const manifest = idx("const manifest = legacySessionImport.writeRetiredManifest(OPENCLAW_DIR, retired);");
    const backup = idx("await backupImportRecords([...receipts, ...(manifest ? [manifest.path] : [])]);");
    const quarantine = idx("Import did not complete. Quarantine what is left");
    assert.ok(success < receipts && receipts < manifest && manifest < backup && backup < quarantine);
    const helper = idx("async function backupImportRecords(absolutePaths)");
    assert.ok(source.slice(helper, helper + 1200).includes("workspaceSync.saveFile(namespace, rel)"));
  });

  it("the records are not on the skip list and are small enough for saveFile's single PUT", () => {
    process.env.S3_USER_FILES_BUCKET = "test-bucket";
    const { shouldSkip } = require("./workspace-sync");
    delete process.env.S3_USER_FILES_BUCKET;
    assert.equal(shouldSkip(".pre-2.0-retired-files.json"), false);
    assert.equal(shouldSkip("agents/main/agent/.pre-2.0-import.json"), false);
  });
});
