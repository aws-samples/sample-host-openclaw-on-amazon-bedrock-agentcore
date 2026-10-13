const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");

function load(file, overrides, env) {
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, file), "utf8"), {
    module, exports: module.exports, Buffer, AbortController, setTimeout, clearTimeout,
    setInterval, clearInterval, console,
    process: { env, pid: process.pid, platform: "linux" },
    require: (name) => Object.hasOwn(overrides, name) ? overrides[name] : require(name),
  }, { filename: file });
  return module.exports;
}

function fixture(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "live-storage-")));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const home = path.join(root, "home"), mount = path.join(root, "mount");
  fs.mkdirSync(path.join(home, ".openclaw/workspace"), { recursive: true });
  fs.mkdirSync(mount);
  const local = path.join(home, ".openclaw/workspace/retained.txt");
  fs.writeFileSync(local, "local seed contents");
  const env = { HOME: home, USER_ID: "slack:test", INTERNAL_USER_ID: "test",
    AWS_REGION: "us-west-2", S3_USER_FILES_BUCKET: "test-bucket" };
  let mountInfo = `20 1 0:32 / ${mount} rw - nfs4 fixture rw\n`;
  const storage = load("state-storage.js", {
    fs: { ...fs, readFileSync(file, ...args) {
      return file === "/proc/self/mountinfo" ? mountInfo : fs.readFileSync(file, ...args);
    } },
  }, env);
  const calls = [];
  const actions = {
    async list() { return { IsTruncated: false, Contents: [] }; },
    async bind(proof) {
      calls.push("bind");
      assert.equal(proof.restoreGateVerified, true);
      return { status: "bound", bound: true, userReady: false, pid: 123, generation: "generation" };
    },
  };
  class ListObjectsV2Command { constructor(input) { this.input = input; } }
  const sdk = {
    ListObjectsV2Command,
    S3Client: class {
      async send(command, options) {
        assert.equal(command instanceof ListObjectsV2Command, true);
        assert.equal(options.abortSignal.aborted, false);
        calls.push(command.input);
        return actions.list(command.input);
      }
    },
  };
  const sync = load("workspace-sync.js", {
    "@aws-sdk/client-s3": sdk,
    "./state-storage": { ...storage, inspectEmptySessionStorage: () =>
      storage.inspectEmptySessionStorage({ homeDir: home, mountDir: mount }) },
  }, env);
  sync.configureCredentialProvider(async () => ({
    accessKeyId: "test-key", secretAccessKey: "test-secret", sessionToken: "test-session",
    expiration: new Date(Date.now() + 3600000),
  }));
  const controller = new AbortController();
  const binding = Object.freeze({ actorId: env.USER_ID, userId: env.INTERNAL_USER_ID });
  const current = { state: "BINDING", binding, generation: "generation", resources: ["credentials"] };
  const scope = { generation: current.generation, signal: controller.signal,
    checkpoint: () => controller.signal.throwIfAborted() };
  const lifecycle = { snapshot: () => current, checkpoint: scope.checkpoint,
    assertBinding: (value) => assert.equal(value, binding) };
  const restore = () => sync.restoreAbsentSeed("slack_test", { lifecycle, scope,
    bindSeed: (proof) => actions.bind(proof) });
  return { storage, sync, restore, actions, calls, controller, mount, home, local,
    changeMount() { mountInfo = mountInfo.replace("20 1", "21 1"); } };
}

test("empty adoption lists every page twice and creates only the separate mirror after ACK", async (t) => {
  const f = fixture(t);
  f.actions.list = async (input) => input.ContinuationToken
    ? { IsTruncated: false, Contents: [] }
    : { IsTruncated: true, Contents: [], NextContinuationToken: "page-two" };
  assert.throws(() => f.sync.assertRestoreReady());
  const result = await f.restore();
  assert.equal(result.status, "adopted");
  assert.deepEqual(f.calls.map((c) => typeof c === "string" ? c : c.ContinuationToken || "first"),
    ["first", "page-two", "first", "page-two", "bind"]);
  assert.ok(f.calls.slice(0, 4).every((c) => c.Prefix === "slack_test/" && c.MaxKeys === 1000));
  f.sync.assertRestoreReady();
  assert.equal(fs.readFileSync(f.local, "utf8"), "local seed contents");
  assert.deepEqual(fs.readdirSync(path.join(f.mount, ".openclaw/workspace")), []);
});

for (const failure of ["page-two-content", "second-list-content", "repeated-token", "mount-content", "abort"]) {
  test(`${failure} never binds, opens the upload gate, or modifies existing files`, async (t) => {
    const f = fixture(t);
    let lists = 0;
    f.actions.list = async () => {
      lists++;
      if (failure === "abort") f.controller.abort(new Error("aborted"));
      if (failure === "repeated-token" || failure === "page-two-content" && lists === 1) {
        return { IsTruncated: true, Contents: [], NextContinuationToken: "next" };
      }
      return { IsTruncated: false, Contents: lists === 2
        ? [{ Key: "slack_test/user-file.txt" }] : [] };
    };
    if (failure === "mount-content") fs.writeFileSync(path.join(f.mount, "existing.txt"), "retained");
    if (failure === "abort") await assert.rejects(f.restore(), /aborted/);
    else assert.equal((await f.restore()).status, "ineligible");
    assert.throws(() => f.sync.assertRestoreReady());
    assert.ok(!f.calls.includes("bind"));
    assert.equal(fs.readFileSync(f.local, "utf8"), "local seed contents");
    if (failure === "mount-content") assert.equal(fs.readFileSync(path.join(f.mount, "existing.txt"), "utf8"), "retained");
  });
}

test("mount replacement after a bound ACK fails closed without overwriting bound data", async (t) => {
  const f = fixture(t);
  const bind = f.actions.bind;
  f.actions.bind = async (proof) => { const reply = await bind(proof); f.changeMount(); return reply; };
  await assert.rejects(f.restore(), /ineligible/);
  assert.throws(() => f.sync.assertRestoreReady());
  assert.equal(f.calls.filter((c) => c === "bind").length, 1);
  assert.equal(fs.readFileSync(f.local, "utf8"), "local seed contents");
  assert.deepEqual(fs.readdirSync(f.mount), []);
});

test("tickets reject forgery and new mounted content while leaving local state untouched", (t) => {
  const f = fixture(t);
  assert.throws(() => f.storage.attachEmptySessionStorage({}), /ineligible/);
  const ticket = f.storage.inspectEmptySessionStorage({ homeDir: f.home, mountDir: f.mount });
  fs.writeFileSync(path.join(f.mount, "appeared.txt"), "user data");
  assert.throws(() => f.storage.attachEmptySessionStorage(ticket), /ineligible/);
  assert.equal(fs.readFileSync(path.join(f.mount, "appeared.txt"), "utf8"), "user data");
  assert.equal(fs.readFileSync(f.local, "utf8"), "local seed contents");
});
