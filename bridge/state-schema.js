/**
 * Schema versions of the restored OpenClaw SQLite stores.
 *
 * OpenClaw 2026.11.2 does not open an agent database written by 2026.9.5
 * (schema 21): the gateway refuses to start ("Sessions remain unavailable")
 * until `openclaw doctor --fix` upgrades it. Doctor upgrades the agent
 * databases (21 -> 27) and the shared state database (17 -> 22) in place and
 * keeps the transcript history. The contract runs doctor before activation when
 * a restored store is older than the pinned release expects.
 *
 * OpenClaw keeps its schema version in SQLite's user_version, which is the
 * big-endian 32-bit integer at offset 60 of the database header. Reading the
 * header avoids opening the database (and creating -shm/-wal files as root).
 * Restored stores are online-backup snapshots, so the header is current; a
 * stale header could only make doctor run once more than needed.
 */
const fs = require("fs");
const path = require("path");

// OpenClaw 2026.11.2 schema versions (agent: openclaw-agent.sqlite, state: state/openclaw.sqlite).
const REQUIRED_SCHEMA_VERSIONS = Object.freeze({ agent: 27, state: 22 });
const SQLITE_HEADER = Buffer.from("SQLite format 3\0", "latin1");
const HEADER_BYTES = 100;

/** user_version from the database header, or null for a missing, short or non-SQLite file. */
function readSqliteUserVersion(file) {
  let fd;
  try {
    fd = fs.openSync(file, "r");
    const header = Buffer.alloc(HEADER_BYTES);
    if (fs.readSync(fd, header, 0, HEADER_BYTES, 0) !== HEADER_BYTES) return null;
    if (!header.subarray(0, SQLITE_HEADER.length).equals(SQLITE_HEADER)) return null;
    return header.readUInt32BE(60);
  } catch {
    return null;
  } finally {
    if (fd !== undefined) {
      try { fs.closeSync(fd); } catch { /* ignore */ }
    }
  }
}

/** The agent and state stores under an OpenClaw state dir that exist on disk. */
function listStores(openclawDir) {
  const stores = [];
  const stateStore = path.join(openclawDir, "state", "openclaw.sqlite");
  if (fs.existsSync(stateStore)) stores.push({ kind: "state", file: stateStore });
  let agents = [];
  try {
    agents = fs.readdirSync(path.join(openclawDir, "agents"), { withFileTypes: true });
  } catch {
    agents = [];
  }
  for (const entry of agents) {
    if (!entry.isDirectory()) continue;
    const agentStore = path.join(openclawDir, "agents", entry.name, "agent", "openclaw-agent.sqlite");
    if (fs.existsSync(agentStore)) stores.push({ kind: "agent", file: agentStore });
  }
  return stores;
}

/**
 * Stores whose schema is older than the pinned OpenClaw release opens.
 * A file without a readable SQLite header is left to the gateway, as before.
 * @returns {{kind: string, file: string, version: number, required: number}[]}
 */
function findOutdatedStores(openclawDir) {
  const outdated = [];
  for (const store of listStores(openclawDir)) {
    const required = REQUIRED_SCHEMA_VERSIONS[store.kind];
    const version = readSqliteUserVersion(store.file);
    if (version !== null && version < required) outdated.push({ ...store, version, required });
  }
  return outdated;
}

module.exports = { REQUIRED_SCHEMA_VERSIONS, readSqliteUserVersion, listStores, findOutdatedStores };
