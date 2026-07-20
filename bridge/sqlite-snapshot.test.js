"use strict";

const { afterEach, describe, it, mock } = require("node:test");
const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const fsp = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { DatabaseSync } = require("node:sqlite");

const { PINNED_HELPER_PATH, SqliteSnapshot } = require("./sqlite-snapshot");

const PINNED_OPENCLAW_ROOT =
  "/tmp/pan-oss-audit-2026-07-17/repos/openclaw_openclaw";
const PINNED_OPENCLAW_COMMIT =
  "4bfaccafd62ac2ff2e70ca1decc40fb1297ab438";
const PINNED_HELPER = path.join(
  PINNED_OPENCLAW_ROOT,
  "dist/backup-DE9-5vmG.js",
);

const GLOBAL_AUTHORITY_TABLES = [
  "auth_profile_stores",
  "auth_profile_state",
  "mcp_oauth_stores",
  "device_pairing_pending",
  "device_pairing_paired",
  "device_bootstrap_tokens",
  "device_identities",
  "device_auth_tokens",
];
const AGENT_AUTHORITY_TABLES = ["auth_profile_store", "auth_profile_state"];

const tempDirectories = [];

async function makePrivateDirectory(parent, name) {
  const directory = path.join(parent, name);
  await fsp.mkdir(directory, { mode: 0o700 });
  await fsp.chmod(directory, 0o700);
  return directory;
}

async function createGlobalFixture() {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), "po-sqlite-snapshot-"));
  tempDirectories.push(root);
  await fsp.chmod(root, 0o700);
  const stateDirectory = await makePrivateDirectory(root, "state");
  const targetDirectory = await makePrivateDirectory(root, "snapshots");
  const sourcePath = path.join(stateDirectory, "openclaw.sqlite");
  const targetPath = path.join(targetDirectory, "global.sqlite");
  const database = new DatabaseSync(sourcePath);
  try {
    for (const table of GLOBAL_AUTHORITY_TABLES) {
      database.exec(`CREATE TABLE ${table} (value TEXT)`);
    }
    database.exec("CREATE TABLE proof_rows (value TEXT NOT NULL)");
    database.prepare("INSERT INTO proof_rows VALUES (?)").run("survives");
  } finally {
    database.close();
  }
  await fsp.chmod(sourcePath, 0o600);
  return { sourcePath, targetPath };
}

async function createAgentFixture() {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), "po-sqlite-snapshot-"));
  tempDirectories.push(root);
  await fsp.chmod(root, 0o700);
  const agentsDirectory = await makePrivateDirectory(root, "agents");
  const mainDirectory = await makePrivateDirectory(agentsDirectory, "main");
  const agentDirectory = await makePrivateDirectory(mainDirectory, "agent");
  const targetDirectory = await makePrivateDirectory(root, "snapshots");
  const sourcePath = path.join(agentDirectory, "openclaw-agent.sqlite");
  const targetPath = path.join(targetDirectory, "agent.sqlite");
  const database = new DatabaseSync(sourcePath);
  try {
    for (const table of AGENT_AUTHORITY_TABLES) {
      database.exec(`CREATE TABLE ${table} (value TEXT)`);
    }
    database.exec("CREATE TABLE agent_proof_rows (value TEXT NOT NULL)");
    database.prepare("INSERT INTO agent_proof_rows VALUES (?)").run("main");
  } finally {
    database.close();
  }
  await fsp.chmod(sourcePath, 0o600);
  return { sourcePath, targetPath };
}

function executeDatabase(sourcePath, sql) {
  const database = new DatabaseSync(sourcePath);
  try {
    database.exec(sql);
  } finally {
    database.close();
  }
}

function createFakeHelperLoader() {
  const snapshot = mock.fn(async ({ sourcePath, targetPath, validate }) => {
    const source = new DatabaseSync(sourcePath, { readOnly: true });
    try {
      validate?.(source, sourcePath);
      source.prepare("VACUUM INTO ?").run(targetPath);
    } finally {
      source.close();
    }
    await fsp.chmod(targetPath, 0o600);
    const target = new DatabaseSync(targetPath, { readOnly: true });
    try {
      validate?.(target, targetPath);
    } finally {
      target.close();
    }
  });
  return {
    loadHelper: mock.fn(async () => ({ o: snapshot })),
    snapshot,
  };
}

afterEach(async () => {
  await Promise.all(
    tempDirectories.splice(0).map((directory) =>
      fsp.rm(directory, { recursive: true, force: true }),
    ),
  );
});

describe("SqliteSnapshot", () => {
  it("pins the production loader to the audited image helper", () => {
    assert.equal(
      PINNED_HELPER_PATH,
      "/opt/openclaw/dist/backup-DE9-5vmG.js",
    );
    const source = fs.readFileSync(require.resolve("./sqlite-snapshot"), "utf8");
    assert.match(
      source,
      /import\(pathToFileURL\(PINNED_HELPER_PATH\)\.href\)/,
    );
    assert.doesNotMatch(
      source,
      /node:child_process|\bspawn\b|\bexecFile\b|\bsqlite3\b|\bcopyFile(?:Sync)?\b/,
    );
  });

  it("uses helper export o and returns independently verified private bytes", async () => {
    const { sourcePath, targetPath } = await createGlobalFixture();
    const helper = createFakeHelperLoader();
    const snapshots = new SqliteSnapshot({ loadHelper: helper.loadHelper });

    const result = await snapshots.snapshot({
      sourcePath,
      targetPath,
      role: "global",
    });

    const bytes = await fsp.readFile(targetPath);
    assert.deepEqual(result, {
      size: bytes.length,
      sha256: createHash("sha256").update(bytes).digest("hex"),
    });
    assert.equal(helper.loadHelper.mock.calls.length, 1);
    assert.equal(helper.snapshot.mock.calls.length, 1);
    assert.equal(helper.snapshot.mock.calls[0].arguments[0].sourcePath, sourcePath);
    assert.equal(helper.snapshot.mock.calls[0].arguments[0].targetPath, targetPath);
    assert.equal(fs.statSync(targetPath).mode & 0o777, 0o600);

    const verified = new DatabaseSync(targetPath, { readOnly: true });
    try {
      assert.deepEqual(
        verified
          .prepare("SELECT value FROM proof_rows")
          .all()
          .map((row) => row.value),
        ["survives"],
      );
    } finally {
      verified.close();
    }
  });

  it("accepts only the canonical main-agent database role", async () => {
    const { sourcePath, targetPath } = await createAgentFixture();
    const helper = createFakeHelperLoader();
    const snapshots = new SqliteSnapshot({ loadHelper: helper.loadHelper });

    const result = await snapshots.snapshot({
      sourcePath,
      targetPath,
      role: "agent",
      agentId: "main",
    });

    assert.ok(result.size > 0);
    assert.match(result.sha256, /^[0-9a-f]{64}$/);
    assert.equal(helper.snapshot.mock.calls.length, 1);

    const wrongAgentTarget = path.join(path.dirname(targetPath), "wrong.sqlite");
    await assert.rejects(
      () =>
        snapshots.snapshot({
          sourcePath,
          targetPath: wrongAgentTarget,
          role: "agent",
          agentId: "secondary",
        }),
      /agentId|main/i,
    );
  });

  it("rejects every non-empty authority table before publishing", async () => {
    for (const table of GLOBAL_AUTHORITY_TABLES) {
      const { sourcePath, targetPath } = await createGlobalFixture();
      executeDatabase(sourcePath, `INSERT INTO ${table} VALUES ('secret')`);
      const helper = createFakeHelperLoader();
      const snapshots = new SqliteSnapshot({ loadHelper: helper.loadHelper });

      await assert.rejects(
        () => snapshots.snapshot({ sourcePath, targetPath, role: "global" }),
        new RegExp(`${table}.*authority|authority.*${table}`, "i"),
      );
      await assert.rejects(() => fsp.access(targetPath), { code: "ENOENT" });
    }

    for (const table of AGENT_AUTHORITY_TABLES) {
      const { sourcePath, targetPath } = await createAgentFixture();
      executeDatabase(sourcePath, `INSERT INTO ${table} VALUES ('secret')`);
      const helper = createFakeHelperLoader();
      const snapshots = new SqliteSnapshot({ loadHelper: helper.loadHelper });

      await assert.rejects(
        () =>
          snapshots.snapshot({
            sourcePath,
            targetPath,
            role: "agent",
            agentId: "main",
          }),
        new RegExp(`${table}.*authority|authority.*${table}`, "i"),
      );
      await assert.rejects(() => fsp.access(targetPath), { code: "ENOENT" });
    }
  });

  it("rejects missing authority schema and the wrong database role", async () => {
    const missing = await createGlobalFixture();
    executeDatabase(missing.sourcePath, "DROP TABLE device_auth_tokens");
    const missingHelper = createFakeHelperLoader();
    await assert.rejects(
      () =>
        new SqliteSnapshot({ loadHelper: missingHelper.loadHelper }).snapshot({
          ...missing,
          role: "global",
        }),
      /schema|device_auth_tokens|missing/i,
    );
    await assert.rejects(() => fsp.access(missing.targetPath), { code: "ENOENT" });

    const wrongRole = await createAgentFixture();
    executeDatabase(
      wrongRole.sourcePath,
      `
        DROP TABLE auth_profile_store;
        ${GLOBAL_AUTHORITY_TABLES.filter((table) => table !== "auth_profile_state")
          .map((table) => `CREATE TABLE ${table} (value TEXT);`)
          .join("\n")}
      `,
    );
    const wrongRoleHelper = createFakeHelperLoader();
    await assert.rejects(
      () =>
        new SqliteSnapshot({ loadHelper: wrongRoleHelper.loadHelper }).snapshot({
          ...wrongRole,
          role: "agent",
          agentId: "main",
        }),
      /schema|auth_profile_store|role/i,
    );
    await assert.rejects(() => fsp.access(wrongRole.targetPath), {
      code: "ENOENT",
    });
  });

  it("rejects noncanonical, symlinked, special, hard-linked, or public sources", async () => {
    const noncanonical = await createGlobalFixture();
    const movedSource = path.join(path.dirname(noncanonical.sourcePath), "other.sqlite");
    await fsp.rename(noncanonical.sourcePath, movedSource);
    await assert.rejects(
      () =>
        new SqliteSnapshot({
          loadHelper: createFakeHelperLoader().loadHelper,
        }).snapshot({
          sourcePath: movedSource,
          targetPath: noncanonical.targetPath,
          role: "global",
        }),
      /state\/openclaw\.sqlite|global/i,
    );

    const symlinked = await createGlobalFixture();
    const realSource = path.join(path.dirname(symlinked.sourcePath), "real.sqlite");
    await fsp.rename(symlinked.sourcePath, realSource);
    await fsp.symlink(realSource, symlinked.sourcePath);
    await assert.rejects(
      () =>
        new SqliteSnapshot({
          loadHelper: createFakeHelperLoader().loadHelper,
        }).snapshot({ ...symlinked, role: "global" }),
      /regular|symlink|source/i,
    );

    const special = await createGlobalFixture();
    await fsp.unlink(special.sourcePath);
    await fsp.mkdir(special.sourcePath, { mode: 0o700 });
    await assert.rejects(
      () =>
        new SqliteSnapshot({
          loadHelper: createFakeHelperLoader().loadHelper,
        }).snapshot({ ...special, role: "global" }),
      /regular|source/i,
    );

    const hardLinked = await createGlobalFixture();
    await fsp.link(
      hardLinked.sourcePath,
      path.join(path.dirname(hardLinked.sourcePath), "second-link.sqlite"),
    );
    await assert.rejects(
      () =>
        new SqliteSnapshot({
          loadHelper: createFakeHelperLoader().loadHelper,
        }).snapshot({ ...hardLinked, role: "global" }),
      /regular|link|source/i,
    );

    const publicSource = await createGlobalFixture();
    await fsp.chmod(publicSource.sourcePath, 0o640);
    await assert.rejects(
      () =>
        new SqliteSnapshot({
          loadHelper: createFakeHelperLoader().loadHelper,
        }).snapshot({ ...publicSource, role: "global" }),
      /0600|mode|private/i,
    );

    const publicParent = await createGlobalFixture();
    await fsp.chmod(path.dirname(publicParent.sourcePath), 0o750);
    await assert.rejects(
      () =>
        new SqliteSnapshot({
          loadHelper: createFakeHelperLoader().loadHelper,
        }).snapshot({ ...publicParent, role: "global" }),
      /0700|mode|private/i,
    );

    const corrupt = await createGlobalFixture();
    await fsp.writeFile(corrupt.sourcePath, "not sqlite", { mode: 0o600 });
    await assert.rejects(
      () =>
        new SqliteSnapshot({
          loadHelper: createFakeHelperLoader().loadHelper,
        }).snapshot({ ...corrupt, role: "global" }),
      /sqlite|database|file/i,
    );
    await assert.rejects(() => fsp.access(corrupt.targetPath), {
      code: "ENOENT",
    });
  });

  it("rejects source, sidecar, existing, symlinked, and public targets", async () => {
    const sourceTarget = await createGlobalFixture();
    await assert.rejects(
      () =>
        new SqliteSnapshot({
          loadHelper: createFakeHelperLoader().loadHelper,
        }).snapshot({
          sourcePath: sourceTarget.sourcePath,
          targetPath: sourceTarget.sourcePath,
          role: "global",
        }),
      /target|source|differ/i,
    );

    const sidecar = await createGlobalFixture();
    await assert.rejects(
      () =>
        new SqliteSnapshot({
          loadHelper: createFakeHelperLoader().loadHelper,
        }).snapshot({
          sourcePath: sidecar.sourcePath,
          targetPath: `${sidecar.sourcePath}-wal`,
          role: "global",
        }),
      /sidecar|wal|target/i,
    );
    await assert.rejects(() => fsp.access(`${sidecar.sourcePath}-wal`), {
      code: "ENOENT",
    });

    const existing = await createGlobalFixture();
    await fsp.writeFile(existing.targetPath, "keep", { mode: 0o600 });
    await assert.rejects(
      () =>
        new SqliteSnapshot({
          loadHelper: createFakeHelperLoader().loadHelper,
        }).snapshot({ ...existing, role: "global" }),
      /exists|target/i,
    );
    assert.equal(await fsp.readFile(existing.targetPath, "utf8"), "keep");

    const symlinkedParent = await createGlobalFixture();
    const privateTarget = await makePrivateDirectory(
      path.dirname(path.dirname(symlinkedParent.targetPath)),
      "real-target",
    );
    const linkedParent = path.join(
      path.dirname(path.dirname(symlinkedParent.targetPath)),
      "linked-target",
    );
    await fsp.symlink(privateTarget, linkedParent);
    await assert.rejects(
      () =>
        new SqliteSnapshot({
          loadHelper: createFakeHelperLoader().loadHelper,
        }).snapshot({
          sourcePath: symlinkedParent.sourcePath,
          targetPath: path.join(linkedParent, "snapshot.sqlite"),
          role: "global",
        }),
      /parent|directory|symlink/i,
    );

    const publicParent = await createGlobalFixture();
    await fsp.chmod(path.dirname(publicParent.targetPath), 0o750);
    await assert.rejects(
      () =>
        new SqliteSnapshot({
          loadHelper: createFakeHelperLoader().loadHelper,
        }).snapshot({ ...publicParent, role: "global" }),
      /0700|mode|private/i,
    );
  });

  it("rejects helper export mismatch and removes invalid helper output", async () => {
    const mismatch = await createGlobalFixture();
    await assert.rejects(
      () =>
        new SqliteSnapshot({ loadHelper: async () => ({ snapshot() {} }) }).snapshot({
          ...mismatch,
          role: "global",
        }),
      /export o|helper/i,
    );
    await assert.rejects(() => fsp.access(mismatch.targetPath), {
      code: "ENOENT",
    });

    const corrupt = await createGlobalFixture();
    const corruptHelper = async () => ({
      o: async ({ targetPath }) => {
        await fsp.writeFile(targetPath, "not sqlite", { mode: 0o600 });
      },
    });
    await assert.rejects(
      () =>
        new SqliteSnapshot({ loadHelper: corruptHelper }).snapshot({
          ...corrupt,
          role: "global",
        }),
      /sqlite|database|integrity|file/i,
    );
    await assert.rejects(() => fsp.access(corrupt.targetPath), {
      code: "ENOENT",
    });

    const publicOutput = await createGlobalFixture();
    const publicHelper = async () => ({
      o: async ({ sourcePath, targetPath }) => {
        const source = new DatabaseSync(sourcePath, { readOnly: true });
        try {
          source.prepare("VACUUM INTO ?").run(targetPath);
        } finally {
          source.close();
        }
        await fsp.chmod(targetPath, 0o644);
      },
    });
    await assert.rejects(
      () =>
        new SqliteSnapshot({ loadHelper: publicHelper }).snapshot({
          ...publicOutput,
          role: "global",
        }),
      /private|0600|target/i,
    );
    await assert.rejects(() => fsp.access(publicOutput.targetPath), {
      code: "ENOENT",
    });
  });

  it("uses the exact pinned helper to preserve a WAL-only committed row", async () => {
    assert.equal(
      execFileSync("git", ["-C", PINNED_OPENCLAW_ROOT, "rev-parse", "HEAD"], {
        encoding: "utf8",
      }).trim(),
      PINNED_OPENCLAW_COMMIT,
    );

    const { sourcePath, targetPath } = await createGlobalFixture();
    const source = new DatabaseSync(sourcePath);
    try {
      source.exec(`
        PRAGMA journal_mode = WAL;
        PRAGMA wal_autocheckpoint = 0;
        PRAGMA wal_checkpoint(TRUNCATE);
      `);
      source
        .prepare("INSERT INTO proof_rows (value) VALUES (?)")
        .run("wal-only-commit");

      const walStat = await fsp.stat(`${sourcePath}-wal`);
      assert.ok(walStat.size > 0);

      const mainFileOnly = path.join(path.dirname(targetPath), "main-file-only.sqlite");
      await fsp.copyFile(sourcePath, mainFileOnly, fs.constants.COPYFILE_EXCL);
      const rawMain = new DatabaseSync(mainFileOnly, { readOnly: true });
      try {
        assert.equal(
          rawMain
            .prepare(
              "SELECT count(*) AS count FROM proof_rows WHERE value = ?",
            )
            .get("wal-only-commit").count,
          0,
        );
      } finally {
        rawMain.close();
      }

      const snapshots = new SqliteSnapshot({
        loadHelper: async () => import(pathToFileURL(PINNED_HELPER).href),
      });
      const result = await snapshots.snapshot({
        sourcePath,
        targetPath,
        role: "global",
      });
      assert.ok(result.size > 0);
      assert.match(result.sha256, /^[0-9a-f]{64}$/);

      const restored = new DatabaseSync(targetPath, { readOnly: true });
      try {
        assert.equal(
          restored
            .prepare(
              "SELECT count(*) AS count FROM proof_rows WHERE value = ?",
            )
            .get("wal-only-commit").count,
          1,
        );
      } finally {
        restored.close();
      }
      for (const suffix of ["-wal", "-shm", "-journal"]) {
        await assert.rejects(() => fsp.access(`${targetPath}${suffix}`), {
          code: "ENOENT",
        });
      }
    } finally {
      source.close();
    }
  });
});
