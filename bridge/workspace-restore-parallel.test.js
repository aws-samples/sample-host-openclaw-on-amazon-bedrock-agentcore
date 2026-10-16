/**
 * Tests for the parallel S3 restore in workspace-sync.js (runRestore).
 *
 * Covers: the concurrency bound (and its env parsing), a failure in the middle
 * of a parallel restore failing the whole restore with no "ready" signal and no
 * stray temp files, an abort stopping the pool, retry with backoff on
 * throttling / 5xx / a broken stream (and no retry on a permanent error),
 * gzip + legacy SQLite objects restored byte-identically to a serial restore,
 * the missing-only (overwrite:false) skip, and the summary log line.
 * Run: cd bridge && node --test workspace-restore-parallel.test.js
 */
const { describe, it, beforeEach, afterEach, mock } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const zlib = require("node:zlib");
const crypto = require("node:crypto");
const Module = require("node:module");

const hasNodeSqlite = (() => {
  try { return typeof require("node:sqlite").backup === "function"; } catch { return false; }
})();
const sha256 = (body) => crypto.createHash("sha256").update(body).digest("hex");
const NS = "telegram_1";
const PREFIX = `${NS}/.openclaw/`;
const CREDS = { accessKeyId: "AKIAIOSFODNN7EXAMPLE", secretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" };
const tick = () => new Promise(setImmediate);

/** Every file under `dir` (relative path -> sha256), plus the names of any temp files. */
function treeHashes(dir) {
  const out = {};
  const walk = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const full = path.join(d, e.name);
      if (e.isDirectory()) walk(full);
      else out[path.relative(dir, full)] = sha256(fs.readFileSync(full));
    }
  };
  if (fs.existsSync(dir)) walk(dir);
  return out;
}

describe("parallel workspace restore", () => {
  let root, savedEnv, realLoad, sync, s3, logs, warnings, realLog, realWarn;

  function makeS3() {
    const state = {
      objects: {}, metadata: {}, encodings: {},
      calls: [], gets: [], inFlight: 0, maxInFlight: 0,
      getHook: null, // async (key, n) => optional override; n = 1-based GET count for that key
      bodyDelayTicks: 3,
    };
    const perKey = new Map();
    state.client = {
      send: async (cmd, opts) => {
        state.calls.push(cmd.kind);
        const inp = cmd.input;
        if (cmd.kind === "list") {
          return {
            Contents: Object.keys(state.objects).sort().map((Key) => ({ Key, Size: state.objects[Key].length })),
            IsTruncated: false,
          };
        }
        if (cmd.kind === "head") {
          return { Metadata: state.metadata[inp.Key] || {} };
        }
        if (cmd.kind === "get") {
          const n = (perKey.get(inp.Key) || 0) + 1;
          perKey.set(inp.Key, n);
          state.gets.push(inp.Key);
          state.inFlight++;
          state.maxInFlight = Math.max(state.maxInFlight, state.inFlight);
          let done = false;
          const release = () => { if (!done) { done = true; state.inFlight--; } };
          try {
            if (state.getHook) {
              const r = await state.getHook(inp.Key, n, opts);
              if (r) { release(); return r; }
            }
          } catch (err) { release(); throw err; }
          const body = state.objects[inp.Key];
          const ticks = state.bodyDelayTicks;
          return {
            Body: (async function* () {
              try {
                for (let i = 0; i < ticks; i++) await tick();
                const half = Math.ceil(body.length / 2);
                yield body.subarray(0, half);
                await tick();
                yield body.subarray(half);
              } finally { release(); }
            })(),
            ...(state.metadata[inp.Key] ? { Metadata: state.metadata[inp.Key] } : {}),
            ...(state.encodings[inp.Key] ? { ContentEncoding: state.encodings[inp.Key] } : {}),
          };
        }
        return {};
      },
    };
    return state;
  }

  function put(rel, body, { gzip = false, meta = true } = {}) {
    const raw = Buffer.from(body);
    s3.objects[PREFIX + rel] = gzip ? zlib.gzipSync(raw) : raw;
    if (meta) {
      s3.metadata[PREFIX + rel] = gzip
        ? { sha256: sha256(raw), encoding: "gzip", "uncompressed-size": String(raw.length) }
        : { sha256: sha256(raw) };
    }
    if (gzip) s3.encodings[PREFIX + rel] = "gzip";
  }

  function freshModule(home, tuning) {
    process.env.HOME = home;
    fs.mkdirSync(path.join(home, ".openclaw"), { recursive: true });
    delete require.cache[require.resolve("./workspace-sync")];
    const m = require("./workspace-sync");
    m.configureCredentials(CREDS);
    m._setRestoreTuningForTests({ baseDelayMs: 1, maxDelayMs: 2, ...tuning });
    return m;
  }

  beforeEach(() => {
    root = fs.mkdtempSync(path.join(os.tmpdir(), "ws-par-"));
    savedEnv = {
      HOME: process.env.HOME, S3_USER_FILES_BUCKET: process.env.S3_USER_FILES_BUCKET,
      AWS_REGION: process.env.AWS_REGION, WORKSPACE_RESTORE_CONCURRENCY: process.env.WORKSPACE_RESTORE_CONCURRENCY,
    };
    process.env.AWS_REGION = "us-west-2";
    process.env.S3_USER_FILES_BUCKET = "test-bucket";
    delete process.env.WORKSPACE_RESTORE_CONCURRENCY;
    s3 = makeS3();
    realLoad = Module._load;
    Module._load = function (request, ...rest) {
      if (request !== "@aws-sdk/client-s3") return realLoad.call(this, request, ...rest);
      const sdk = { S3Client: function () { return s3.client; } };
      for (const [name, kind] of Object.entries({
        ListObjectsV2Command: "list", GetObjectCommand: "get", HeadObjectCommand: "head", PutObjectCommand: "put",
      })) sdk[name] = function (input) { this.kind = kind; this.input = input; };
      return sdk;
    };
    logs = []; warnings = [];
    realLog = console.log; realWarn = console.warn;
    const fmt = (args) => require("node:util").format(...args);
    console.log = (...a) => logs.push(fmt(a));
    console.warn = (...a) => warnings.push(fmt(a));
    sync = freshModule(path.join(root, "home"));
  });

  afterEach(async () => {
    console.log = realLog; console.warn = realWarn;
    mock.timers.reset();
    try { await sync?.cleanup(); } catch { /* gate closed */ }
    Module._load = realLoad;
    for (const [k, v] of Object.entries(savedEnv)) {
      if (v === undefined) delete process.env[k]; else process.env[k] = v;
    }
    delete require.cache[require.resolve("./workspace-sync")];
    fs.rmSync(root, { recursive: true, force: true });
  });

  const state = (home = path.join(root, "home")) => path.join(home, ".openclaw");
  const tmpLeftovers = (dir) => Object.keys(treeHashes(dir)).filter((f) => f.includes(".tmp-"));

  // --- concurrency bound ------------------------------------------------------

  it("defaults to 16 in flight and reads WORKSPACE_RESTORE_CONCURRENCY (clamped, invalid -> default)", () => {
    assert.equal(sync.getRestoreTuning().concurrency, 16);
    assert.equal(sync.DEFAULT_RESTORE_CONCURRENCY, 16);
    const load = (v) => {
      process.env.WORKSPACE_RESTORE_CONCURRENCY = v;
      delete require.cache[require.resolve("./workspace-sync")];
      return require("./workspace-sync").getRestoreTuning().concurrency;
    };
    assert.equal(load("7"), 7);
    assert.equal(load("1"), 1);
    assert.equal(load("1000"), sync.MAX_RESTORE_CONCURRENCY);
    assert.equal(load("0"), 16);
    assert.equal(load("-3"), 16);
    assert.equal(load("abc"), 16);
    assert.equal(load("2.5"), 16);
    assert.ok(warnings.some((w) => /Ignoring invalid WORKSPACE_RESTORE_CONCURRENCY/.test(w)));
  });

  it("never has more GetObjects in flight than the configured concurrency, and does use it", async () => {
    for (let i = 0; i < 40; i++) put(`workspace/f${String(i).padStart(2, "0")}.md`, `file ${i}\n`);
    sync._setRestoreTuningForTests({ concurrency: 4 });
    const r = await sync.restoreWorkspace(NS);
    assert.deepEqual(r, { restored: 40, kept: 0, keptUnchanged: 0, failed: 0, hadState: true });
    assert.equal(s3.maxInFlight, 4, "bounded at 4 and actually parallel");
    assert.equal(s3.gets.length, 40, "each object fetched once");
    assert.equal(sync.getRestoreState(), "ready");
    for (let i = 0; i < 40; i++) {
      assert.equal(fs.readFileSync(path.join(state(), `workspace/f${String(i).padStart(2, "0")}.md`), "utf8"), `file ${i}\n`);
    }
  });

  it("concurrency 1 is strictly serial", async () => {
    for (let i = 0; i < 6; i++) put(`f${i}.md`, `x${i}`);
    sync._setRestoreTuningForTests({ concurrency: 1 });
    await sync.restoreWorkspace(NS);
    assert.equal(s3.maxInFlight, 1);
  });

  it("logs one summary line with file count, bytes, concurrency, elapsed and retries", async () => {
    put("a.md", "aaaa"); put("b.md", "bb");
    sync._setRestoreTuningForTests({ concurrency: 3 });
    await sync.restoreWorkspace(NS);
    const line = logs.find((l) => /Restored \d+ file/.test(l));
    assert.match(line, /Restored 2 file\(s\) to .* — 6 bytes, concurrency 3, \d+\.\d s, 0 retries$/);
  });

  // --- failure mid-restore ----------------------------------------------------

  it("a failure in the middle fails the whole restore: no ready signal, uploads stay off, no temp files", async () => {
    for (let i = 0; i < 30; i++) put(`workspace/f${String(i).padStart(2, "0")}.md`, `file ${i}\n`);
    const bad = `${PREFIX}workspace/f12.md`;
    s3.getHook = async (key) => {
      if (key === bad) throw Object.assign(new Error("Access Denied"), { name: "AccessDenied", $metadata: { httpStatusCode: 403 } });
    };
    sync._setRestoreTuningForTests({ concurrency: 8 });
    const states = new Set();
    const poll = setInterval(() => states.add(sync.getRestoreState()), 0);
    try {
      const restore = sync.restoreWorkspace(NS);
      await assert.rejects(sync.awaitRestore(restore, 10_000, { log: { warn() {} } }), /incomplete: 1 file/);
      await assert.rejects(restore, /incomplete: 1 file/);
    } finally {
      clearInterval(poll);
    }
    states.add(sync.getRestoreState());
    assert.ok(!states.has("ready"), `never reported ready (saw ${[...states]})`);
    assert.equal(sync.getRestoreState(), "failed");
    assert.throws(() => sync.assertRestoreReady(), /failed/);
    assert.equal(s3.gets.filter((k) => k === bad).length, 1, "a 403 is not retried");
    assert.equal(fs.existsSync(path.join(state(), "workspace/f12.md")), false);
    assert.deepEqual(tmpLeftovers(state()), []);
    // Every other worker ran to completion before the restore settled.
    assert.equal(s3.inFlight, 0);
    // Uploads stay disabled for the container.
    fs.writeFileSync(path.join(state(), "workspace/new.md"), "n");
    assert.equal(await sync.saveFile(NS, "workspace/new.md"), false);
    assert.ok(!s3.calls.includes("put"));
  });

  it("a timeout aborts the pool: no new GETs start and nothing more is renamed into place", async () => {
    for (let i = 0; i < 20; i++) put(`f${String(i).padStart(2, "0")}.md`, `v${i}`);
    const { Readable } = require("node:stream");
    const hanging = [];
    s3.getHook = async () => {
      const body = new Readable({ read() {} });
      hanging.push(body);
      return { Body: body };
    };
    sync._setRestoreTuningForTests({ concurrency: 3 });
    mock.timers.enable({ apis: ["setTimeout"] });
    const restore = sync.restoreWorkspace(NS);
    const wait = sync.awaitRestore(restore, 100, { log: { warn() {} } });
    const rejected = assert.rejects(wait, /timed out/);
    for (let i = 0; i < 5; i++) await tick();
    assert.equal(s3.gets.length, 3, "three downloads in flight");
    hanging[0].push("partial");
    mock.timers.tick(100);
    await rejected;
    // A body that completes after the abort must not land.
    for (const b of hanging) { b.push("rest"); b.push(null); }
    await assert.rejects(restore);
    assert.equal(s3.gets.length, 3, "no GET started after the abort");
    assert.deepEqual(Object.keys(treeHashes(state())), [], "nothing restored, no temp files");
    assert.equal(sync.getRestoreState(), "failed");
  });

  // --- retry ------------------------------------------------------------------

  it("retries a throttled GetObject (SlowDown / 503) with backoff and then succeeds", async () => {
    put("a.md", "alpha"); put("b.md", "beta");
    s3.getHook = async (key, n) => {
      if (key === `${PREFIX}a.md` && n <= 2) {
        throw Object.assign(new Error("Please reduce your request rate."), { name: "SlowDown", $metadata: { httpStatusCode: 503 } });
      }
    };
    const r = await sync.restoreWorkspace(NS);
    assert.equal(r.restored, 2);
    assert.equal(r.failed, 0);
    assert.equal(s3.gets.filter((k) => k === `${PREFIX}a.md`).length, 3);
    assert.equal(fs.readFileSync(path.join(state(), "a.md"), "utf8"), "alpha");
    assert.equal(sync.getRestoreState(), "ready");
    assert.equal(warnings.filter((w) => /Retrying restore of a\.md in \d+ ms \(attempt [23] of 4\): SlowDown/.test(w)).length, 2);
    assert.match(logs.find((l) => /Restored 2 file/.test(l)), /2 retries$/);
  });

  it("retries a body stream that breaks mid-download (ECONNRESET), from a clean temp file", async () => {
    put("big.json", "x".repeat(5000));
    s3.getHook = async (key, n) => {
      if (n === 1) {
        return {
          Metadata: s3.metadata[key],
          Body: (async function* () {
            yield Buffer.from("x".repeat(100));
            throw Object.assign(new Error("socket hang up"), { code: "ECONNRESET" });
          })(),
        };
      }
    };
    const r = await sync.restoreWorkspace(NS);
    assert.equal(r.restored, 1);
    assert.equal(fs.readFileSync(path.join(state(), "big.json"), "utf8"), "x".repeat(5000));
    assert.deepEqual(tmpLeftovers(state()), []);
  });

  it("retries 5xx / 429 / $retryable, never a 4xx, a sha256 mismatch or an abort", () => {
    const r = sync.isRetryableRestoreError;
    assert.ok(r({ name: "InternalError", $metadata: { httpStatusCode: 500 } }));
    assert.ok(r({ name: "Whatever", $metadata: { httpStatusCode: 502 } }));
    assert.ok(r({ name: "TooManyRequestsException", $metadata: { httpStatusCode: 429 } }));
    assert.ok(r({ name: "X", $retryable: { throttling: true } }));
    assert.ok(r({ code: "ETIMEDOUT" }));
    assert.ok(r({ code: "ERR_STREAM_PREMATURE_CLOSE" }));
    assert.ok(!r({ name: "AccessDenied", $metadata: { httpStatusCode: 403 } }));
    assert.ok(!r({ name: "NoSuchKey", $metadata: { httpStatusCode: 404 } }));
    assert.ok(!r(new Error("sha256 mismatch after download")));
    assert.ok(!r(Object.assign(new Error("aborted"), { name: "AbortError", code: "ABORT_ERR" })));
    assert.ok(!r(null));
  });

  it("gives up after the retry budget and fails the restore", async () => {
    put("a.md", "alpha");
    s3.getHook = async () => {
      throw Object.assign(new Error("We encountered an internal error."), { name: "InternalError", $metadata: { httpStatusCode: 500 } });
    };
    sync._setRestoreTuningForTests({ retries: 2 });
    await assert.rejects(sync.restoreWorkspace(NS), /incomplete: 1 file/);
    assert.equal(s3.gets.length, 3, "1 attempt + 2 retries");
    assert.equal(sync.getRestoreState(), "failed");
  });

  // --- SQLite / gzip multipart objects ----------------------------------------

  it(
    "restores gzip (multipart) and legacy uncompressed SQLite objects exactly as a serial restore does",
    { skip: !hasNodeSqlite && "node:sqlite not available on this Node" },
    async () => {
      const { DatabaseSync } = require("node:sqlite");
      const mkdb = (file, rows) => {
        const db = new DatabaseSync(file);
        db.exec("CREATE TABLE t(v TEXT); BEGIN");
        const ins = db.prepare("INSERT INTO t VALUES (?)");
        for (let i = 0; i < rows; i++) ins.run(`row ${i} ${"y".repeat(50)}`);
        db.exec("COMMIT");
        db.close();
        return fs.readFileSync(file);
      };
      const big = mkdb(path.join(root, "big.sqlite"), 3000);
      const small = mkdb(path.join(root, "small.sqlite"), 5);
      put("agents/main/agent/openclaw-agent.sqlite", big, { gzip: true });
      put("state/openclaw.sqlite", small);
      put("memory/main.sqlite", small, { meta: false }); // legacy: no sha256, no encoding
      for (let i = 0; i < 25; i++) put(`agents/main/sessions/s${i}.jsonl`, `{"turn":${i}}\n`);
      // Sidecars in S3 are never restored (unchanged skip rules).
      put("state/openclaw.sqlite-wal", "wal bytes");
      put("state/openclaw.sqlite-shm", "shm bytes");

      const serialHome = path.join(root, "serial");
      const serial = freshModule(serialHome, { concurrency: 1 });
      const rs = await serial.restoreWorkspace(NS);
      const parallelHome = path.join(root, "parallel");
      const parallel = freshModule(parallelHome, { concurrency: 16 });
      const rp = await parallel.restoreWorkspace(NS);

      assert.deepEqual(rp, rs);
      assert.equal(rp.restored, 28);
      const hs = treeHashes(state(serialHome));
      const hp = treeHashes(state(parallelHome));
      assert.deepEqual(hp, hs, "byte-identical trees");
      assert.equal(hp["agents/main/agent/openclaw-agent.sqlite"], sha256(big), "gzip object decompressed");
      assert.equal(hp["memory/main.sqlite"], sha256(small), "legacy object copied as-is");
      assert.ok(!("state/openclaw.sqlite-wal" in hp) && !("state/openclaw.sqlite-shm" in hp));
      assert.ok(s3.maxInFlight > 1);
      assert.deepEqual(tmpLeftovers(state(parallelHome)), []);
    },
  );

  it("a sha256 mismatch on one object fails the restore and leaves no file", async () => {
    put("a.md", "alpha"); put("b.md", "beta");
    s3.metadata[`${PREFIX}b.md`] = { sha256: sha256("something else") };
    await assert.rejects(sync.restoreWorkspace(NS), /incomplete: 1 file/);
    assert.equal(fs.existsSync(path.join(state(), "b.md")), false);
    assert.equal(s3.gets.filter((k) => k.endsWith("b.md")).length, 1, "not retried");
  });

  // --- missing-only -----------------------------------------------------------

  it("overwrite:false downloads only the missing files and leaves existing ones untouched", async () => {
    for (let i = 0; i < 10; i++) put(`workspace/f${i}.md`, `remote ${i}`);
    for (const i of [1, 4, 7]) {
      fs.mkdirSync(path.join(state(), "workspace"), { recursive: true });
      fs.writeFileSync(path.join(state(), `workspace/f${i}.md`), `local ${i}`);
    }
    sync._setRestoreTuningForTests({ concurrency: 4 });
    const r = await sync.restoreWorkspace(NS, { overwrite: false });
    assert.equal(r.restored, 7);
    assert.equal(r.kept, 3);
    assert.equal(r.failed, 0);
    assert.deepEqual(
      s3.gets.map((k) => k.slice(PREFIX.length)).sort(),
      [0, 2, 3, 5, 6, 8, 9].map((i) => `workspace/f${i}.md`),
      "no GET for a kept file",
    );
    for (const i of [1, 4, 7]) assert.equal(fs.readFileSync(path.join(state(), `workspace/f${i}.md`), "utf8"), `local ${i}`);
    for (const i of [0, 2, 9]) assert.equal(fs.readFileSync(path.join(state(), `workspace/f${i}.md`), "utf8"), `remote ${i}`);
    assert.equal(sync.getRestoreState(), "ready");
  });
});
