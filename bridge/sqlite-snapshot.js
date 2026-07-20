"use strict";

const { createHash } = require("node:crypto");
const fsp = require("node:fs/promises");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { DatabaseSync } = require("node:sqlite");

const PINNED_HELPER_PATH = "/opt/openclaw/dist/backup-DE9-5vmG.js";
const GLOBAL_AUTHORITY_TABLES = Object.freeze([
  "auth_profile_stores",
  "auth_profile_state",
  "mcp_oauth_stores",
  "device_pairing_pending",
  "device_pairing_paired",
  "device_bootstrap_tokens",
  "device_identities",
  "device_auth_tokens",
]);
const AGENT_AUTHORITY_TABLES = Object.freeze([
  "auth_profile_store",
  "auth_profile_state",
]);

async function loadPinnedHelper() {
  return import(pathToFileURL(PINNED_HELPER_PATH).href);
}

function fileMode(stat) {
  return stat.mode & 0o777;
}

async function assertPrivateDirectory(directoryPath, label) {
  const stat = await fsp.lstat(directoryPath);
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw new Error(`${label} must be a real directory`);
  }
  if (fileMode(stat) !== 0o700) {
    throw new Error(`${label} must have mode 0700`);
  }
}

async function assertValidSource(sourcePath, role, agentId) {
  const resolved = path.resolve(sourcePath);
  if (role === "global") {
    if (
      agentId !== undefined ||
      path.basename(resolved) !== "openclaw.sqlite" ||
      path.basename(path.dirname(resolved)) !== "state"
    ) {
      throw new Error("Global SQLite source must end with state/openclaw.sqlite");
    }
  } else if (role === "agent") {
    const segments = resolved.split(path.sep);
    const suffix = segments.slice(-4);
    if (
      agentId !== "main" ||
      suffix[0] !== "agents" ||
      suffix[1] !== "main" ||
      suffix[2] !== "agent" ||
      suffix[3] !== "openclaw-agent.sqlite"
    ) {
      throw new Error(
        "Agent SQLite source requires agentId main and canonical agents/main/agent/openclaw-agent.sqlite",
      );
    }
  } else {
    throw new Error("SQLite snapshot role must be global or agent");
  }
  const stat = await fsp.lstat(resolved);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1) {
    throw new Error("SQLite snapshot source must be one regular unlinked file");
  }
  if (fileMode(stat) !== 0o600) {
    throw new Error("SQLite snapshot source must have mode 0600");
  }
  await assertPrivateDirectory(path.dirname(resolved), "SQLite source parent");
  return resolved;
}

async function assertNewTarget(sourcePath, targetPath) {
  const resolved = path.resolve(targetPath);
  const sourceAndSidecars = new Set([
    sourcePath,
    `${sourcePath}-wal`,
    `${sourcePath}-shm`,
    `${sourcePath}-journal`,
  ]);
  if (
    sourceAndSidecars.has(resolved) ||
    /\.sqlite-(?:wal|shm|journal)$/u.test(path.basename(resolved))
  ) {
    throw new Error("SQLite snapshot target cannot be the source or a sidecar");
  }
  await assertPrivateDirectory(path.dirname(resolved), "SQLite target parent");
  try {
    await fsp.lstat(resolved);
  } catch (error) {
    if (error?.code === "ENOENT") return resolved;
    throw error;
  }
  throw new Error("SQLite snapshot target already exists");
}

function sameFileFingerprint(left, right) {
  return (
    left.dev === right.dev &&
    left.ino === right.ino &&
    left.size === right.size &&
    left.mtimeMs === right.mtimeMs &&
    left.ctimeMs === right.ctimeMs &&
    left.birthtimeMs === right.birthtimeMs
  );
}

async function removeTargetIfOwned(targetPath, expected) {
  try {
    const current = await fsp.lstat(targetPath);
    if (sameFileFingerprint(current, expected)) {
      await fsp.unlink(targetPath);
    }
  } catch {}
}

function assertPrivateTargetStat(stat) {
  if (
    !stat.isFile() ||
    stat.isSymbolicLink() ||
    stat.nlink !== 1 ||
    fileMode(stat) !== 0o600
  ) {
    throw new Error("SQLite snapshot target is not one private regular file");
  }
}

function tableExists(database, table) {
  return Boolean(
    database
      .prepare(
        "SELECT 1 AS present FROM sqlite_schema WHERE type = 'table' AND name = ?",
      )
      .get(table),
  );
}

function assertRoleDatabase(database, role, label) {
  database.exec("PRAGMA trusted_schema = OFF;");
  const integrity = database.prepare("PRAGMA integrity_check").all();
  if (
    integrity.length !== 1 ||
    Object.values(integrity[0] || {})[0] !== "ok"
  ) {
    throw new Error(`SQLite integrity check failed for ${label}`);
  }
  if (database.prepare("PRAGMA foreign_key_check").get()) {
    throw new Error(`SQLite foreign-key check failed for ${label}`);
  }

  const required =
    role === "global" ? GLOBAL_AUTHORITY_TABLES : AGENT_AUTHORITY_TABLES;
  const forbidden =
    role === "global"
      ? ["auth_profile_store"]
      : GLOBAL_AUTHORITY_TABLES.filter(
          (table) => !AGENT_AUTHORITY_TABLES.includes(table),
        );
  for (const table of required) {
    if (!tableExists(database, table)) {
      throw new Error(
        `SQLite ${role} schema is missing pinned authority table ${table}`,
      );
    }
    if (database.prepare(`SELECT 1 AS present FROM "${table}" LIMIT 1`).get()) {
      throw new Error(`SQLite authority table ${table} must be empty`);
    }
  }
  for (const table of forbidden) {
    if (tableExists(database, table)) {
      throw new Error(
        `SQLite ${role} role contains authority table ${table} from another schema`,
      );
    }
  }
}

function assertTargetDatabase(targetPath, role) {
  const database = new DatabaseSync(targetPath, { readOnly: true });
  try {
    database.exec("PRAGMA query_only = ON;");
    assertRoleDatabase(database, role, targetPath);
  } finally {
    database.close();
  }
}

async function hashPrivateTarget(targetPath) {
  const before = await fsp.lstat(targetPath);
  assertPrivateTargetStat(before);
  const handle = await fsp.open(targetPath, "r");
  try {
    const opened = await handle.stat();
    if (opened.dev !== before.dev || opened.ino !== before.ino) {
      throw new Error("SQLite snapshot target changed before verification");
    }
    const hash = createHash("sha256");
    const buffer = Buffer.allocUnsafe(1024 * 1024);
    let offset = 0;
    while (true) {
      const { bytesRead } = await handle.read(
        buffer,
        0,
        buffer.length,
        offset,
      );
      if (bytesRead === 0) break;
      hash.update(buffer.subarray(0, bytesRead));
      offset += bytesRead;
    }
    const after = await handle.stat();
    const current = await fsp.lstat(targetPath);
    if (
      after.dev !== opened.dev ||
      after.ino !== opened.ino ||
      after.size !== opened.size ||
      after.mtimeMs !== opened.mtimeMs ||
      current.dev !== opened.dev ||
      current.ino !== opened.ino
    ) {
      throw new Error("SQLite snapshot target changed during verification");
    }
    if (offset <= 0 || offset !== after.size) {
      throw new Error("SQLite snapshot size verification failed");
    }
    return { size: offset, sha256: hash.digest("hex") };
  } finally {
    await handle.close();
  }
}

class SqliteSnapshot {
  constructor({ loadHelper = loadPinnedHelper } = {}) {
    if (typeof loadHelper !== "function") {
      throw new TypeError("SqliteSnapshot requires a helper loader");
    }
    this.loadHelper = loadHelper;
  }

  async snapshot({ sourcePath, targetPath, role, agentId } = {}) {
    const source = await assertValidSource(sourcePath, role, agentId);
    const target = await assertNewTarget(source, targetPath);
    const helperModule = await this.loadHelper();
    if (!helperModule || typeof helperModule.o !== "function") {
      throw new Error("Pinned OpenClaw SQLite helper export o is unavailable");
    }
    const validate = (database, label) =>
      assertRoleDatabase(database, role, label);
    let publishedIdentity;
    try {
      await helperModule.o({
        sourcePath: source,
        targetPath: target,
        validate,
      });
      publishedIdentity = await fsp.lstat(target);
      assertPrivateTargetStat(publishedIdentity);
      assertTargetDatabase(target, role);
      return await hashPrivateTarget(target);
    } catch (error) {
      if (publishedIdentity) {
        await removeTargetIfOwned(target, publishedIdentity);
      }
      throw error;
    }
  }
}

module.exports = {
  PINNED_HELPER_PATH,
  SqliteSnapshot,
};
