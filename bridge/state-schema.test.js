/**
 * Schema versions of restored OpenClaw SQLite stores (state-schema.js).
 * OpenClaw 2026.11.2 opens agent schema 27 and state schema 22; 2026.9.5 wrote 21 and 17.
 * Run: cd bridge && node --test state-schema.test.js
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { DatabaseSync } = require("node:sqlite");
const { REQUIRED_SCHEMA_VERSIONS, readSqliteUserVersion, findOutdatedStores } = require("./state-schema");

function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "state-schema-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}
function database(file, version) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const db = new DatabaseSync(file);
  db.exec("PRAGMA journal_mode = WAL; CREATE TABLE t (x); INSERT INTO t VALUES (1);");
  db.exec(`PRAGMA user_version = ${version}`);
  db.exec("PRAGMA wal_checkpoint(TRUNCATE)");
  db.close();
  return file;
}

test("pins the OpenClaw 2026.11.2 schema versions", () => {
  assert.deepEqual(REQUIRED_SCHEMA_VERSIONS, { agent: 27, state: 22 });
});

test("reads user_version from the database header without opening the database", (t) => {
  const dir = tempDir(t);
  const file = database(path.join(dir, "a.sqlite"), 21);
  assert.equal(readSqliteUserVersion(file), 21);
  assert.ok(!fs.existsSync(`${file}-shm`), "reading the header creates no sidecar files");
  fs.writeFileSync(path.join(dir, "short.sqlite"), "SQLite format 3\0");
  fs.writeFileSync(path.join(dir, "text.sqlite"), "x".repeat(200));
  fs.writeFileSync(path.join(dir, "empty.sqlite"), "");
  for (const name of ["short.sqlite", "text.sqlite", "empty.sqlite", "missing.sqlite"]) {
    assert.equal(readSqliteUserVersion(path.join(dir, name)), null, name);
  }
});

test("finds 2026.9.5 agent and state stores and nothing else", (t) => {
  const dir = tempDir(t);
  database(path.join(dir, "agents/main/agent/openclaw-agent.sqlite"), 21);
  database(path.join(dir, "agents/helper/agent/openclaw-agent.sqlite"), 27);
  database(path.join(dir, "state/openclaw.sqlite"), 17);
  // Lock files, sidecars and other databases are not schema-versioned stores.
  database(path.join(dir, "tmp/openclaw-1/gateway.state.lock.sqlite"), 0);
  database(path.join(dir, "state/openclaw-quarantine.sqlite"), 0);
  fs.writeFileSync(path.join(dir, "agents/README"), "");
  const outdated = findOutdatedStores(dir).map(({ kind, file, version, required }) =>
    ({ kind, file: path.relative(dir, file), version, required }));
  assert.deepEqual(outdated, [
    { kind: "state", file: "state/openclaw.sqlite", version: 17, required: 22 },
    { kind: "agent", file: "agents/main/agent/openclaw-agent.sqlite", version: 21, required: 27 },
  ]);
});

test("current, newer, unreadable and missing stores are not outdated", (t) => {
  const dir = tempDir(t);
  assert.deepEqual(findOutdatedStores(dir), []);
  database(path.join(dir, "agents/main/agent/openclaw-agent.sqlite"), 28);
  database(path.join(dir, "state/openclaw.sqlite"), 22);
  fs.mkdirSync(path.join(dir, "agents/empty/agent"), { recursive: true });
  fs.writeFileSync(path.join(dir, "agents/empty/agent/openclaw-agent.sqlite"), "");
  assert.deepEqual(findOutdatedStores(dir), []);
});

const packageDir = process.env.OPENCLAW_PACKAGE_DIR;
test("the pinned versions are the installed OpenClaw's own schema versions", {
  skip: !packageDir && "Set OPENCLAW_PACKAGE_DIR to the pinned OpenClaw 2026.11.2 package",
}, () => {
  const dist = path.join(packageDir, "dist");
  const found = { agent: new Set(), state: new Set() };
  for (const name of fs.readdirSync(dist).filter((file) => file.endsWith(".mjs"))) {
    const source = fs.readFileSync(path.join(dist, name), "utf8");
    for (const [kind, constant] of [["agent", "OPENCLAW_AGENT_SCHEMA_VERSION"], ["state", "OPENCLAW_STATE_SCHEMA_VERSION"]]) {
      for (const match of source.matchAll(new RegExp(`\\bconst ${constant} = (\\d+);`, "g"))) found[kind].add(Number(match[1]));
    }
  }
  assert.deepEqual({ agent: [...found.agent], state: [...found.state] },
    { agent: [REQUIRED_SCHEMA_VERSIONS.agent], state: [REQUIRED_SCHEMA_VERSIONS.state] });
});
