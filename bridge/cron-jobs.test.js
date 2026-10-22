// Async scheduled turns: cron_start / cron_status and the job store behind them.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const jobs = require("./cron-jobs");

const source = fs.readFileSync(require.resolve("./agentcore-contract"), "utf8");
const HOUR = 3600 * 1000;

function slice(start, end) {
  const a = source.indexOf(start);
  const b = source.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `source boundaries: ${start}`);
  return source.slice(a, b);
}

function tmpdir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cron-jobs-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

// --- cron_start validation ---------------------------------------------------

test("cron_start takes a jobId and an optional job budget, capped under an hour", () => {
  assert.deepEqual(jobs.parseCronStart({ jobId: "a723d463-20261019T204700Z" }),
    { ok: true, jobId: "a723d463-20261019T204700Z", budgetMs: 45 * 60 * 1000 });
  assert.equal(jobs.parseCronStart({ jobId: "j1", jobBudgetMs: 55 * 60 * 1000 }).budgetMs, 55 * 60 * 1000);
  for (const bad of [{}, { jobId: "" }, { jobId: "a:b" }, { jobId: "x".repeat(81) }, { jobId: 7 },
    { jobId: "j", jobBudgetMs: 59999 }, { jobId: "j", jobBudgetMs: 55 * 60 * 1000 + 1 },
    { jobId: "j", jobBudgetMs: "600000" }, { jobId: "j", callerBudgetMs: 900000 }]) {
    assert.equal(jobs.parseCronStart(bad).ok, false, JSON.stringify(bad));
  }
  // The cap stays far below the session's 8 h maxLifetime and inside 1 h credentials.
  assert.ok(jobs.MAX_JOB_BUDGET_MS + 60000 < HOUR);
});

test("the default job budget comes from the environment and is clamped", () => {
  assert.equal(jobs.envBudgetMs(undefined), 45 * 60 * 1000);
  assert.equal(jobs.envBudgetMs("1200000"), 1200000);
  assert.equal(jobs.envBudgetMs("99999999"), jobs.MAX_JOB_BUDGET_MS);
  assert.equal(jobs.envBudgetMs("5"), jobs.MIN_JOB_BUDGET_MS);
  assert.equal(jobs.envBudgetMs("soon"), 45 * 60 * 1000);
});

// --- job states --------------------------------------------------------------

test("a job is running, then done with its reply and outcome", () => {
  const store = jobs.createCronJobStore({ bootId: "boot-1" });
  assert.equal(store.start("j1", { userId: "u1", deadline: 5 }).duplicate, false);
  assert.deepEqual(store.status("j1", "u1"), { status: "running", jobId: "j1", deadline: 5 });
  store.finish("j1", { response: "the brief", execution: { phase: "completed" } });
  assert.deepEqual(store.status("j1", "u1"),
    { status: "done", jobId: "j1", outcome: "completed", response: "the brief", execution: { phase: "completed" } });
});

for (const [name, result, outcome] of [
  ["error", { response: "The agent could not complete this request.", status: "error", execution: { phase: "error" } }, "error"],
  ["unknown outcome", { response: "The request outcome is unknown.", status: "unknown_outcome" }, "unknown_outcome"],
  ["readiness failure", { response: "Agent initialization or readiness failed.", status: "error" }, "error"],
  ["validation error", { error: "Missing userId, actorId, or message" }, "error"],
]) {
  test(`a ${name} is a failed job and its text is never kept`, () => {
    const store = jobs.createCronJobStore({ bootId: "b" });
    store.start("j", { userId: "u", deadline: 1 });
    store.finish("j", result);
    assert.deepEqual(store.status("j", "u"), { status: "done", jobId: "j", outcome, response: "", execution: { phase: outcome } });
  });
}

test("an unknown job, or another user's job, answers unknown", () => {
  const store = jobs.createCronJobStore({ bootId: "b" });
  store.start("j", { userId: "u1", deadline: 1 });
  assert.deepEqual(store.status("nope", "u1"), { status: "unknown", jobId: "nope" });
  assert.deepEqual(store.status("j", "u2"), { status: "unknown", jobId: "j" });
  assert.deepEqual(store.status(undefined, "u1"), { status: "unknown", jobId: undefined });
});

test("a duplicate jobId does not start a second job, even after it finished", () => {
  const store = jobs.createCronJobStore({ bootId: "b" });
  store.start("j", { userId: "u", deadline: 1 });
  assert.equal(store.start("j", { userId: "u", deadline: 2 }).duplicate, true);
  store.finish("j", { response: "ok", execution: { phase: "completed" } });
  const again = store.start("j", { userId: "u", deadline: 3 });
  assert.equal(again.duplicate, true);
  assert.equal(again.job.deadline, 1);
  // A second finish (the turn's fallback) does not overwrite the result.
  store.finish("j", { status: "error" });
  assert.equal(store.status("j", "u").outcome, "completed");
});

test("a long reply is capped under the Step Functions state limit", () => {
  const store = jobs.createCronJobStore({ bootId: "b" });
  store.start("j", { userId: "u", deadline: 1 });
  store.finish("j", { response: "x".repeat(jobs.MAX_RESPONSE_CHARS + 5), execution: { phase: "completed" } });
  const status = store.status("j", "u");
  assert.equal(status.response.length, jobs.MAX_RESPONSE_CHARS);
  assert.equal(status.truncated, true);
});

test("session storage keeps done jobs across a restart; a running job becomes lost", (t) => {
  const dir = tmpdir(t);
  const first = jobs.createCronJobStore({ dir, bootId: "boot-1" });
  first.load();
  first.start("done", { userId: "u", deadline: 1 });
  first.finish("done", { response: "kept", execution: { phase: "completed" } });
  first.start("cut", { userId: "u", deadline: 2 });
  assert.ok(fs.existsSync(path.join(dir, jobs.STORE_FILE)));
  // Same boot reloading its own file: the running job is still its own.
  const same = jobs.createCronJobStore({ dir, bootId: "boot-1" });
  same.load();
  assert.equal(same.status("cut", "u").status, "running");
  const next = jobs.createCronJobStore({ dir, bootId: "boot-2" });
  next.load();
  assert.equal(next.status("done", "u").response, "kept");
  assert.deepEqual(next.status("cut", "u"), { status: "unknown", jobId: "cut", reason: "lost" });
  // A lost job is still a duplicate: the poller reports it, nothing re-runs it.
  assert.equal(next.start("cut", { userId: "u", deadline: 3 }).duplicate, true);
});

test("an unreadable or missing store file starts empty", (t) => {
  const dir = tmpdir(t);
  const log = { warnings: 0, warn() { this.warnings++; } };
  const empty = jobs.createCronJobStore({ dir, bootId: "b", log });
  empty.load();
  assert.equal(empty.size(), 0);
  fs.writeFileSync(path.join(dir, jobs.STORE_FILE), "{not json");
  const broken = jobs.createCronJobStore({ dir, bootId: "b", log });
  broken.load();
  assert.equal(broken.size(), 0);
  assert.equal(log.warnings, 1);
});

test("old finished jobs are pruned; running jobs are kept", () => {
  let clock = 0;
  const store = jobs.createCronJobStore({ bootId: "b", now: () => clock });
  store.start("old", { userId: "u", deadline: 1 });
  store.finish("old", { response: "x", execution: { phase: "completed" } });
  store.start("long", { userId: "u", deadline: 1 });
  clock = 25 * HOUR;
  store.start("new", { userId: "u", deadline: 1 });
  assert.equal(store.status("old", "u").status, "unknown");
  assert.equal(store.status("long", "u").status, "running");
  for (let i = 0; i < jobs.MAX_JOBS + 5; i++) {
    store.start(`n${i}`, { userId: "u", deadline: 1 });
    store.finish(`n${i}`, { status: "error" });
  }
  assert.ok(store.size() <= jobs.MAX_JOBS);
  assert.equal(store.status("long", "u").status, "running");
});

// --- the contract's cron_start / cron_status handler -------------------------

function cronHandler({ turn = async () => {} } = {}) {
  const turns = [];
  const box = {
    fs: { existsSync: () => false }, stateStorage: { DEFAULT_MOUNT: "/mnt/workspace" },
    cronJobs: jobs, osRandomUUID: () => "boot-1", CRON_JOB_BUDGET_MS: 45 * 60 * 1000,
    cronJobStoreInstance: null, console: { log() {}, warn() {}, error() {} }, Date,
    handleV2Invocation(payload, req, res, callerGone, arrival, options) {
      turns.push({ payload, options, callerGone: callerGone() });
      return turn(res, payload, options);
    },
  };
  vm.createContext(box);
  vm.runInContext(slice("function cronJobStore() {", "\n// Shape check for the Router"), box);
  const call = async (payload) => {
    let reply;
    await box.handleCronAsync(payload, { headers: {} }, {
      writeHead(code) { this.code = code; },
      end(body) { reply = { code: this.code, body: JSON.parse(body) }; },
    });
    return reply;
  };
  return { turns, call, store: () => box.cronJobStore() };
}

const owner = { userId: "u1", actorId: "telegram:1", channel: "telegram", channelTarget: "1" };

test("cron_start replies accepted at once and runs a cron turn to the job deadline", async () => {
  let finishTurn;
  const app = cronHandler({ turn: (res) => new Promise((resolve) => { finishTurn = () => {
    res.writeHead(200);
    res.end(JSON.stringify({ response: "brief text", execution: { phase: "completed" } }));
    resolve();
  }; }) });
  const before = Date.now();
  const reply = await app.call({ ...owner, action: "cron_start", jobId: "j1", message: "run the brief" });
  assert.equal(reply.code, 200);
  assert.equal(reply.body.status, "accepted");
  assert.equal(reply.body.jobId, "j1");
  assert.equal(app.turns.length, 1);
  const { payload, options } = app.turns[0];
  // trigger=cron: the turn is a normal cron turn, so token tags stay the same.
  assert.equal(payload.action, "cron");
  assert.equal(payload.message, "run the brief");
  assert.equal(payload.callerBudgetMs, undefined);
  assert.equal(payload.jobId, undefined);
  // Not the 620 s chat limit or a 900 s caller budget: the 45 min job deadline.
  assert.ok(options.jobDeadline >= before + 45 * 60 * 1000);
  assert.equal(reply.body.deadline, options.jobDeadline);
  assert.equal((await app.call({ ...owner, action: "cron_status", jobId: "j1" })).body.status, "running");
  finishTurn();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual((await app.call({ ...owner, action: "cron_status", jobId: "j1" })).body,
    { status: "done", jobId: "j1", outcome: "completed", response: "brief text", execution: { phase: "completed" } });
});

test("cron_status never starts or waits on a turn", async () => {
  const app = cronHandler({ turn: () => new Promise(() => {}) });
  await app.call({ ...owner, action: "cron_start", jobId: "j1", message: "m" });
  for (let i = 0; i < 3; i++) await app.call({ ...owner, action: "cron_status", jobId: "j1" });
  assert.equal(app.turns.length, 1);
  assert.equal((await app.call({ ...owner, action: "cron_status", jobId: "other" })).body.status, "unknown");
});

test("a duplicate cron_start is acknowledged without a second turn", async () => {
  const app = cronHandler({ turn: () => new Promise(() => {}) });
  const first = await app.call({ ...owner, action: "cron_start", jobId: "j1", message: "m" });
  const second = await app.call({ ...owner, action: "cron_start", jobId: "j1", message: "m" });
  assert.equal(second.body.status, "accepted");
  assert.equal(second.body.duplicate, true);
  assert.equal(second.body.deadline, first.body.deadline);
  assert.equal(app.turns.length, 1);
});

for (const [name, turn] of [
  ["a non-200 reply", async (res) => { res.writeHead(400); res.end(JSON.stringify({ error: "bad binding" })); }],
  ["a thrown turn", async () => { throw new Error("boom"); }],
  ["a turn that never replies", async () => {}],
  ["an unknown outcome", async (res) => { res.writeHead(200); res.end(JSON.stringify({ status: "unknown_outcome", response: "x" })); }],
]) {
  test(`${name} finishes the job as failed`, async () => {
    const app = cronHandler({ turn });
    await app.call({ ...owner, action: "cron_start", jobId: "j1", message: "m" });
    await new Promise((resolve) => setImmediate(resolve));
    const status = (await app.call({ ...owner, action: "cron_status", jobId: "j1" })).body;
    assert.equal(status.status, "done");
    assert.notEqual(status.outcome, "completed");
    assert.equal(status.response, "");
  });
}

test("cron_start rejects a bad jobId, a caller budget or a missing message without a turn", async () => {
  const app = cronHandler();
  for (const payload of [{ jobId: "a b", message: "m" }, { jobId: "j", message: "m", callerBudgetMs: 1000 },
    { jobId: "j" }, { jobId: "j", message: "m", jobBudgetMs: 9 * HOUR }]) {
    const reply = await app.call({ ...owner, action: "cron_start", ...payload });
    assert.equal(reply.code, 400, JSON.stringify(payload));
  }
  assert.equal((await app.call({ action: "cron_status", jobId: "j" })).code, 400);
  assert.equal(app.turns.length, 0);
});

// --- wiring ------------------------------------------------------------------

test("the HTTP server routes cron_start and cron_status to the async handler", () => {
  const server = slice("const server = http.createServer(", "\n// --- SIGTERM handler");
  assert.match(server, /action === "cron_start" \|\| action === "cron_status"\)\s*\{\s*await handleCronAsync\(/);
  assert.ok(server.indexOf("handleCronAsync(") < server.indexOf('["warmup", "chat", "cron"].includes(action)'));
});

test("handleV2Invocation bounds an async job by its own deadline, not a caller budget", async () => {
  const seen = {};
  const binding = { userId: "u1", actorId: "telegram:1", runtimeSessionId: "s", routingGeneration: 1 };
  const box = {
    console: { log() {}, warn() {}, error() {} }, Date, Object, JSON, Math, Number, AbortSignal,
    MAX_CALLER_BUDGET_MS: 900000, CALLER_REPLY_MARGIN_MS: 20000, TURN_TIMEOUT_MS: 620000,
    proxyModelOutcomeUnknown: false, activeTaskCount: 0, lastActivityTime: 0, RUNTIME_PLATFORM: "V2",
    OPENCLAW_PORT: 1, GATEWAY_TOKEN: "t", TELEGRAM_BOT_TOKEN: "", activationScope: { signal: undefined },
    lateAnnounces: null, payloadModelOverride: () => null, buildBridgeText: (m) => m, extractTextFromContent: (x) => x,
    checkProxyHealth: async () => ({ model_outcome_unknown: false }),
    verifyV2Resources: async () => {},
    updateIdentityFile: (...args) => { seen.trigger = args[5].trigger; },
    gatewayTurn: async ({ deadline }) => { seen.deadline = deadline; return { phase: "completed", response: "ok", runId: "r" }; },
    v2Lifecycle: {
      assertBinding() {}, checkpoint() {}, fail: async () => {},
      snapshot: () => ({ state: "USER_READY", binding, generation: 1 }),
      recover: async (fn) => fn(),
      run: async (context, fn) => { seen.queueDeadline = context.deadline; return fn({ ...context, engine: "openclaw" }); },
    },
  };
  vm.createContext(box);
  vm.runInContext(slice("async function handleV2Invocation(", "\nfunction cronJobStore() {"), box);
  const jobDeadline = Date.now() + 45 * 60 * 1000;
  let reply;
  await box.handleV2Invocation({ ...binding, action: "cron", message: "m", channel: "telegram" }, { headers: {} },
    { writeHead() {}, end(body) { reply = JSON.parse(body); } }, () => false, null, { jobDeadline });
  assert.equal(reply.execution.phase, "completed");
  assert.equal(seen.trigger, "cron");
  assert.equal(seen.deadline, jobDeadline);
  assert.equal(seen.queueDeadline, jobDeadline);
});

test("a release/v1 reply (no execution block) is completed only with text and no error status", () => {
  assert.equal(jobs.summarize({ response: "brief" }).outcome, "completed");
  assert.equal(jobs.summarize({ response: "x", status: "error" }).outcome, "error");
  assert.equal(jobs.summarize({ status: "error" }).outcome, "error");
  assert.equal(jobs.summarize({ response: "x", execution: { phase: "error" } }).outcome, "error");
  assert.equal(jobs.summarize(null).outcome, "error");
});
