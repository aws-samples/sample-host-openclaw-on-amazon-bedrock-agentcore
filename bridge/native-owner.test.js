const { test, before } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const { createHash, randomUUID } = require("node:crypto");
const { DatabaseSync } = require("node:sqlite");
const { renderConfig, renderAgents } = require("./gateway-config");
let common;
before(async () => { common = await import("./live-gateway-seed-common.mjs"); });

function harness(t, { holdSpawn = false, holdBind = false, onKill, onPrepare } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "native-owner-"));
  const home = path.join(root, "home"), seedRoot = path.join(root, "control"), stateRoot = path.join(home, ".openclaw");
  fs.mkdirSync(home, { mode: 0o700 });
  const removed = [];
  const child = Object.assign(new EventEmitter(), { pid: 101, exitCode: null, signalCode: null,
    stdout: { resume() {} }, stderr: { resume() {} } });
  child.kill = () => { onKill?.(stateRoot); child.exitCode = 0; child.emit("exit", 0); };
  let binding;
  const processStub = { execPath: "/usr/local/bin/node", version: "v24.21.0", arch: "arm64",
    platform: "linux", getuid: () => process.getuid(), kill: () => child.kill() };
  const fsStub = {
    ...fs,
    readdirSync: (file, ...args) => file === "/proc" ? child.exitCode === null ? ["101"] : [] : fs.readdirSync(file, ...args),
    rmSync: (file, options) => { removed.push(file); fs.rmSync(file, options); },
  };
  const layout = seedId => ({ home, stateDir: stateRoot,
    configPath: path.join(stateRoot, "openclaw.json"), workspaceDir: path.join(stateRoot, "workspace"),
    cwd: root, controlDir: seedRoot, port: 18789, pid: 101, seedId });
  const respond = message => child.emit("message", {
    type: "result", version: common.version, seedId: message.seedId, requestId: message.requestId,
    pid: 101, status: message.type === "prepare" ? "prepared" : "bound",
    generation: message.payload.generation ?? null, reason: null,
    proof: message.type === "prepare" ? { heartbeatQuiescent: true, layout: layout(message.seedId),
      liveEagerCompileExecArgv: true } : { heartbeatResponsive: true, userReady: false, layout: layout(message.seedId) },
  });
  child.send = (message, callback) => {
    if (message.type === "bind") binding = message;
    queueMicrotask(() => {
      callback(null);
      if (message.type === "prepare") onPrepare?.(stateRoot);
      if (message.type !== "bind" || !holdBind) respond(message);
    });
  };
  const spawn = () => {
    if (!holdSpawn) queueMicrotask(() => child.emit("spawn"));
    return child;
  };
  const source = fs.readFileSync(path.join(__dirname, "live-gateway-seed-owner.mjs"), "utf8")
    .split("export function createLiveGatewaySeedOwner")[1];
  const dependencies = { ...common, fs: fsStub, path, spawn, randomUUID, createHash, DatabaseSync, process: processStub,
    listeners: () => [],
    processWitness: pid => {
      if (child.exitCode !== null) throw new Error("gone");
      return { pid, start: "123", group: 101, state: "S" };
    } };
  const create = vm.compileFunction(`return function createLiveGatewaySeedOwner${source}`, Object.keys(dependencies))(
    ...Object.values(dependencies));
  const owner = create({ seedRoot, home, cwd: root, port: 18789 });
  t.after(async () => { await owner.stop(); fs.rmSync(root, { recursive: true, force: true }); });
  const gatewayToken = "neutral-private-gateway-token";
  const input = { gatewayToken, renderedConfig: renderConfig({ gatewayToken }),
    agentsText: renderAgents(), neutralEnv: {} };
  const bindInput = () => {
    const generation = randomUUID();
    return { ...input, generation, actorId: "actor", userId: "owner", channel: "web",
      boundEnv: { USER_ID: "actor", INTERNAL_USER_ID: "owner", CHANNEL: "web",
        AWS_CONFIG_FILE: "/tmp/scoped-creds/scoped-aws-config", AWS_SDK_LOAD_CONFIG: "1" },
      absenceProof: { generation, actorId: "actor", userId: "owner", verified: true,
        restoreGateVerified: true, mcpDisabled: true } };
  };
  return { owner, child, root, home, seedRoot, stateRoot, removed, input, bindInput: () => {
    const result = bindInput(); delete result.agentsText; delete result.neutralEnv; return result;
  }, lateBind: () => respond(binding) };
}
test("never-used prepared seed cleanup removes only owned inventory", async t => {
  const h = harness(t);
  assert.equal((await h.owner.prepare(h.input)).status, "prepared");
  const cleanup = await h.owner.stop("discard");
  assert.equal(cleanup.cleanupConfirmed, true);
  assert.equal(cleanup.seedInventoryRemoved, true);
  assert.equal(cleanup.adoptionStatus, "not_attempted");
  assert.equal(fs.existsSync(h.stateRoot), false);
  assert.deepEqual(h.removed, [h.seedRoot]);
  assert.deepEqual(await h.owner.stop(), cleanup);
});
test("bound seed shutdown preserves user inventory", async t => {
  const h = harness(t);
  await h.owner.prepare(h.input);
  assert.equal((await h.owner.bind(h.bindInput())).status, "bound");
  const cleanup = await h.owner.stop();
  assert.equal(cleanup.seedInventoryRemoved, false);
  assert.equal(cleanup.adoptionStatus, "bound");
  assert.deepEqual(h.removed, [h.seedRoot]);
  assert.equal(fs.existsSync(h.stateRoot), true);
});
test("lost bind ACK remains uncertain and forbids cold fallback", async t => {
  const h = harness(t, { holdBind: true });
  await h.owner.prepare(h.input);
  const pending = h.owner.bind(h.bindInput());
  const cleanup = await h.owner.stop("abort");
  h.lateBind();
  const result = await pending;
  assert.equal(result.status, "failed");
  assert.equal(result.coldFallbackAllowed, false);
  assert.equal(cleanup.adoptionStatus, "unknown");
  assert.equal(cleanup.seedInventoryRemoved, false);
  assert.deepEqual(h.removed, [h.seedRoot]);
});
test("stop during spawn waits for the child and cannot resurrect preparation", async t => {
  const h = harness(t, { holdSpawn: true });
  const preparation = h.owner.prepare(h.input), stopped = h.owner.stop("abort");
  h.child.emit("spawn");
  const cleanup = await stopped, result = await preparation;
  assert.equal(cleanup.cleanupConfirmed, true);
  assert.equal(cleanup.seedInventoryRemoved, false);
  assert.equal(result.status, "fallback");
  assert.equal(result.coldFallbackAllowed, false);
  assert.equal(h.child.exitCode, 0);
  assert.equal((await h.owner.prepare(h.input)).status, "fallback");
  assert.deepEqual(h.removed, [h.seedRoot]);
});

const mutations = {
  addition(h) {
    const file = path.join(h.stateRoot, "workspace", "history.json");
    fs.writeFileSync(file, "restored history");
    return file;
  },
  modification(h) {
    const file = path.join(h.stateRoot, "AGENTS.md");
    fs.writeFileSync(file, "restored instructions");
    return file;
  },
  replacement(h) {
    const file = path.join(h.stateRoot, "AGENTS.md");
    fs.renameSync(file, path.join(h.root, "old-agents"));
    fs.writeFileSync(file, "restored replacement");
    return file;
  },
  symlink(h) {
    const file = path.join(h.stateRoot, "AGENTS.md"), outside = path.join(h.root, "user-data");
    fs.writeFileSync(outside, "outside user data");
    fs.unlinkSync(file); fs.symlinkSync(outside, file);
    return file;
  },
};
for (const action of ["discard", "invalid-bind"]) for (const [name, mutate] of Object.entries(mutations)) {
  test(`${action} preserves ${name} within the same state directory inode`, async t => {
    const h = harness(t);
    assert.equal((await h.owner.prepare(h.input)).status, "prepared");
    const inode = fs.lstatSync(h.stateRoot).ino, file = mutate(h);
    const bytes = fs.readFileSync(file), identity = fs.lstatSync(file);
    let cleanup;
    if (action === "discard") cleanup = await h.owner.stop("discard");
    else {
      const result = await h.owner.bind({ ...h.bindInput(), extra: true });
      assert.equal(result.coldFallbackAllowed, false);
      cleanup = result.cleanup;
    }
    assert.equal(cleanup.cleanupConfirmed, true);
    assert.equal(cleanup.seedInventoryRemoved, false);
    assert.equal(fs.lstatSync(h.stateRoot).ino, inode);
    assert.equal(fs.lstatSync(file).ino, identity.ino);
    assert.equal(fs.lstatSync(file).isSymbolicLink(), identity.isSymbolicLink());
    assert.deepEqual(fs.readFileSync(file), bytes);
    assert.equal((await h.owner.stop()).seedInventoryRemoved, false);
  });
}
test("restoration protection is irreversible even while the inventory is still unchanged", async t => {
  const h = harness(t);
  await h.owner.prepare(h.input);
  h.owner.protectInventory(); h.owner.protectInventory();
  assert.equal((await h.owner.stop("discard")).seedInventoryRemoved, false);
  assert.equal(fs.existsSync(path.join(h.stateRoot, "openclaw.json")), true);
});
test("unexpected data arriving during preparation is never admitted to the owned inventory", async t => {
  const h = harness(t, { onPrepare(directory) { fs.writeFileSync(path.join(directory, "workspace", "restored"), "user"); } });
  const result = await h.owner.prepare(h.input);
  assert.equal(result.coldFallbackAllowed, false);
  assert.equal(fs.readFileSync(path.join(h.stateRoot, "workspace", "restored"), "utf8"), "user");
});
test("a modification during graceful stop is preserved", async t => {
  const h = harness(t, { onKill(directory) { fs.writeFileSync(path.join(directory, "AGENTS.md"), "late restore"); } });
  await h.owner.prepare(h.input);
  assert.equal((await h.owner.stop("discard")).seedInventoryRemoved, false);
  assert.equal(fs.readFileSync(path.join(h.stateRoot, "AGENTS.md"), "utf8"), "late restore");
});
test("clean unused discard restores preexisting empty workspace directories", async t => {
  const h = harness(t);
  fs.mkdirSync(h.stateRoot, { mode: 0o700 });
  fs.mkdirSync(path.join(h.stateRoot, "workspace"), { mode: 0o700 });
  await h.owner.prepare(h.input);
  assert.equal((await h.owner.stop("discard")).seedInventoryRemoved, true);
  assert.deepEqual(fs.readdirSync(h.stateRoot), ["workspace"]);
  assert.deepEqual(fs.readdirSync(path.join(h.stateRoot, "workspace")), []);
});

function withDatabase(directory, operation) {
  const state = path.join(directory, "state");
  fs.mkdirSync(state, { recursive: true, mode: 0o700 });
  const db = new DatabaseSync(path.join(state, "openclaw.sqlite"));
  try { operation(db); } finally { db.close(); }
}
function prepareDatabase(directory) {
  withDatabase(directory, db => {
    db.exec(`CREATE TABLE gateway_boot_lifecycle (
      boot_id TEXT, pid INTEGER, started_at_ms INTEGER, completed_at_ms INTEGER, outcome TEXT, startup_reason TEXT, reason TEXT);
      CREATE TABLE state_leases (scope TEXT, lease_key TEXT, payload_json TEXT);
      CREATE TABLE user_notes (note TEXT);`);
    db.prepare("INSERT INTO gateway_boot_lifecycle VALUES (?, 101, ?, NULL, NULL, NULL, NULL)").run(randomUUID(), Date.now());
    db.prepare("INSERT INTO state_leases VALUES ('gateway-owner', 'global', ?)").run(JSON.stringify({ owner: { pid: 101 } }));
  });
}
function closeDatabase(directory, userWrite = false) {
  withDatabase(directory, db => {
    db.exec("DELETE FROM state_leases");
    db.prepare("UPDATE gateway_boot_lifecycle SET completed_at_ms=?, outcome='clean_stop', reason='stop (SIGTERM)'").run(Date.now());
    if (userWrite) db.exec("INSERT INTO user_notes VALUES ('restored user note')");
  });
}
test("unused SQLite cleanup admits only the owned boot completion and lease removal", async t => {
  const h = harness(t, { onPrepare: prepareDatabase, onKill: closeDatabase });
  assert.equal((await h.owner.prepare(h.input)).status, "prepared");
  assert.equal((await h.owner.stop("discard")).seedInventoryRemoved, true);
  assert.equal(fs.existsSync(h.stateRoot), false);
});
test("SQLite data written during shutdown is preserved despite valid boot and lease cleanup", async t => {
  const h = harness(t, { onPrepare: prepareDatabase, onKill: directory => closeDatabase(directory, true) });
  assert.equal((await h.owner.prepare(h.input)).status, "prepared");
  assert.equal((await h.owner.stop("discard")).seedInventoryRemoved, false);
  withDatabase(h.stateRoot, db => assert.equal(db.prepare("SELECT note FROM user_notes").get().note, "restored user note"));
});
