/**
 * Cron turn outcomes and queue deadlines on the V1 contract.
 * Run: node --test cron-turn.bridge.test.js
 *
 * Prod 10-13: the 620 s WebSocket timeout returned 393 chars of streamed
 * status text as the evening EN brief, and the queued CN turn got an empty
 * final, so the contract fell back to the warm-up shim, which answered
 * "I ran into a limit processing your request". A cron turn now returns
 * status "error" with a fixed text unless the run finished with text, and a
 * cron turn still queued at its deadline is dropped, never sent, while the
 * caller is still waiting.
 *
 * The shipped functions are sliced out of agentcore-contract.js and run
 * against a fake protocol-v4 gateway. Ids are placeholders.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

let WebSocket;
try {
  WebSocket = require("ws");
} catch {
  WebSocket = null;
}
const skip = !WebSocket && "ws not installed";

const MAIN_KEY = "agent:main:main";
const SRC = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf8");
const FAILED = "Your scheduled task did not return a result this time. It will run again at the next scheduled time.";

function sliceFunction(src, signature) {
  const start = src.indexOf(signature);
  assert.notEqual(start, -1, `missing ${signature}`);
  const end = src.indexOf("\n}\n", start);
  assert.notEqual(end, -1, `unterminated ${signature}`);
  return src.slice(start, end + 2);
}

function sliceConst(src, name) {
  const match = new RegExp(`\\nconst ${name} =[\\s\\S]*?;[^\\n]*\\n`).exec(src);
  assert.ok(match, `missing const ${name}`);
  return match[0].slice(1);
}

/** Load the real queue + bridge + cron turn with a spy for the lightweight agent. */
function loadCron(port, logs = []) {
  const body = [
    "let messageQueue = [];",
    "let processingMessage = false;",
    "let staleDeliverySweep = null;",
    ...["CRON_DEFAULT_WAIT_MS", "CALLER_REPLY_MARGIN_MS", "MAX_CALLER_BUDGET_MS", "CRON_TURN_FAILED_TEXT"]
      .map((name) => sliceConst(SRC, name)),
    sliceFunction(SRC, "function extractTextFromContent("),
    sliceFunction(SRC, "async function bridgeMessage("),
    sliceFunction(SRC, "async function processMessageQueue("),
    sliceFunction(SRC, "function enqueueMessage("),
    sliceFunction(SRC, "function cronCallerDeadline("),
    sliceFunction(SRC, "function cronFailureText("),
    sliceFunction(SRC, "async function runCronTurn("),
    "return { runCronTurn, enqueueMessage, cronCallerDeadline, cronFailureText, CRON_DEFAULT_WAIT_MS };",
  ].join("\n");
  const { createChatRunTracker } = require("./chat-run-filter");
  const record = (level) => (...args) => logs.push([level, ...args]);
  const quiet = { log: record("log"), warn: record("warn"), error: record("error") };
  const shimCalls = [];
  const agent = { chat: async (...args) => { shimCalls.push(args); return "I ran into a limit processing your request."; } };
  const factory = new Function(
    "WebSocket", "OPENCLAW_PORT", "GATEWAY_TOKEN", "createChatRunTracker", "require", "console", "agent", body,
  );
  return { ...factory(WebSocket, port, "test-token", createChatRunTracker, require, quiet, agent), shimCalls, logs };
}

const textMsg = (text) => ({ role: "assistant", content: [{ type: "text", text }] });

/**
 * Fake gateway. `onChat({runId, socket, index})` runs for each chat.send
 * after its ack and sends that run's chat events.
 */
async function gateway(t, { onChat, methods = ["chat.send"] }) {
  const sends = [];
  const wss = new WebSocket.Server({ host: "127.0.0.1", port: 0 });
  wss.on("connection", (socket) => {
    socket.send(JSON.stringify({ type: "event", event: "connect.challenge", payload: { nonce: "n" } }));
    socket.on("message", (raw) => {
      const frame = JSON.parse(raw);
      const reply = (ok, payload) => socket.send(JSON.stringify({ type: "res", id: frame.id, ok, payload }));
      if (frame.method === "connect") return reply(true, { type: "hello-ok", protocol: 4, features: { methods } });
      if (frame.method === "chat.send") {
        const runId = frame.params.idempotencyKey;
        sends.push({ message: frame.params.message, at: Date.now() });
        reply(true, { runId, status: "started" });
        const emit = (payload) => {
          if (socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({ type: "event", event: "chat", payload: { runId, sessionKey: MAIN_KEY, ...payload } }));
          }
        };
        onChat({ runId, socket, emit, index: sends.length - 1, message: frame.params.message });
      }
    });
  });
  await new Promise((resolve) => wss.on("listening", resolve));
  t.after(async () => {
    for (const socket of wss.clients) socket.terminate();
    await new Promise((resolve) => wss.close(resolve));
  });
  return { port: wss.address().port, sends };
}

const EN = "[Scheduled task: evening-briefing-en] write the brief";
const CN = "[Scheduled task: evening-briefing-cn] write the brief";
const STATUS = "That old result is stale. edge-tts is returning 403, switching to gTTS.";

test("a cron turn that times out returns status error and the fixed text, never the partial deltas", { skip }, async (t) => {
  const gw = await gateway(t, { onChat: ({ emit }) => emit({ seq: 1, state: "delta", message: textMsg(STATUS) }) });
  const cron = loadCron(gw.port);
  const started = Date.now();
  const reply = await cron.runCronTurn(EN, Date.now() + 300);
  assert.deepEqual(reply, { response: `[Scheduled: evening-briefing-en] ${FAILED}`, status: "error" });
  assert.ok(Date.now() - started < 1500, "the turn ends at its caller deadline");
  assert.deepEqual(cron.shimCalls, [], "the warm-up shim never answers a cron turn");
  assert.ok(cron.logs.some(([level, format, kind, chars]) => level === "warn" &&
    format === "[contract] Cron turn returned no result; outcome, partial chars:" && kind === "timeout" && chars === STATUS.length));
});

for (const [name, events] of [
  ["an empty final", [{ seq: 1, state: "final" }]],
  ["an empty final after deltas were cleared", [{ seq: 1, state: "delta", message: textMsg("") }, { seq: 2, state: "final", message: textMsg("") }]],
  ["an error event after partial text", [{ seq: 1, state: "delta", message: textMsg(STATUS) }, { seq: 2, state: "error", errorMessage: "ValidationException" }]],
  ["an aborted run", [{ seq: 1, state: "aborted" }]],
]) {
  test(`${name} on a cron turn returns status error, not shim text`, { skip }, async (t) => {
    const gw = await gateway(t, { onChat: ({ emit }) => events.forEach(emit) });
    const cron = loadCron(gw.port);
    const reply = await cron.runCronTurn(CN, Date.now() + 5000);
    assert.deepEqual(reply, { response: `[Scheduled: evening-briefing-cn] ${FAILED}`, status: "error" });
    assert.deepEqual(cron.shimCalls, []);
    assert.ok(!reply.response.includes("gTTS") && !reply.response.includes("Chat aborted"));
  });
}

test("a dropped gateway socket mid-run on a cron turn returns status error", { skip }, async (t) => {
  const gw = await gateway(t, { onChat: ({ emit, socket }) => {
    emit({ seq: 1, state: "delta", message: textMsg(STATUS) });
    setTimeout(() => socket.terminate(), 20);
  } });
  const reply = await loadCron(gw.port).runCronTurn(EN, Date.now() + 5000);
  assert.equal(reply.status, "error");
  assert.ok(!reply.response.includes("gTTS"));
});

test("a finished cron run returns its text with no error status", { skip }, async (t) => {
  const gw = await gateway(t, { onChat: ({ emit }) => {
    emit({ seq: 1, state: "delta", message: textMsg("Evening brief") });
    emit({ seq: 2, state: "final", message: textMsg("Evening brief\n\n- item") });
  } });
  const reply = await loadCron(gw.port).runCronTurn(EN, Date.now() + 5000);
  assert.deepEqual(reply, { response: "Evening brief\n\n- item" });
});

test("a queued cron turn is dropped at its deadline while the earlier turn still runs, and is never sent", { skip }, async (t) => {
  let releaseEn;
  const gw = await gateway(t, { onChat: ({ emit, index }) => {
    if (index === 0) {
      emit({ seq: 1, state: "delta", message: textMsg("working") });
      releaseEn = () => emit({ seq: 2, state: "final", message: textMsg("EN BRIEF") });
    } else {
      emit({ seq: 1, state: "final", message: textMsg("CN BRIEF") });
    }
  } });
  const cron = loadCron(gw.port);
  const en = cron.runCronTurn(EN, Date.now() + 5000);
  const started = Date.now();
  const cn = await cron.runCronTurn(CN, Date.now() + 300);
  const waited = Date.now() - started;
  assert.deepEqual(cn, { response: `[Scheduled: evening-briefing-cn] ${FAILED}`, status: "error" });
  assert.ok(waited >= 250 && waited < 1000, `CN replied at its deadline, after ${waited} ms`);
  assert.equal(gw.sends.length, 1, "EN still running; CN never reached the gateway");
  releaseEn();
  assert.deepEqual(await en, { response: "EN BRIEF" });
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(gw.sends.length, 1, "the dropped CN turn is not sent after EN finishes");
  assert.ok(cron.logs.some(([, format]) => format === "[contract] Cron turn not run:"));
});

test("a queued cron turn sent after the earlier turn gets only the budget left, from dispatch", { skip }, async (t) => {
  const gw = await gateway(t, { onChat: ({ emit, index }) => {
    if (index === 0) setTimeout(() => emit({ seq: 1, state: "final", message: textMsg("EN BRIEF") }), 200);
    else emit({ seq: 1, state: "delta", message: textMsg(STATUS) }); // CN never finishes
  } });
  const cron = loadCron(gw.port);
  const started = Date.now();
  const en = cron.runCronTurn(EN, Date.now() + 5000);
  const cn = await cron.runCronTurn(CN, Date.now() + 700);
  const elapsed = Date.now() - started;
  assert.deepEqual(await en, { response: "EN BRIEF" });
  assert.equal(cn.status, "error");
  assert.ok(!cn.response.includes("gTTS"));
  assert.equal(gw.sends.length, 2);
  assert.ok(gw.sends[1].at - started >= 180, "CN was sent after EN finished");
  assert.ok(elapsed >= 650 && elapsed < 1300, `CN ended at its caller deadline, after ${elapsed} ms`);
});

test("chat turns queued without a deadline still wait for the turn ahead", { skip }, async (t) => {
  const gw = await gateway(t, { onChat: ({ emit, index }) => {
    setTimeout(() => emit({ seq: 1, state: "final", message: textMsg(`reply ${index}`) }), index === 0 ? 400 : 0);
  } });
  const cron = loadCron(gw.port);
  const first = cron.runCronTurn(EN, Date.now() + 5000);
  const chat = await cron.enqueueMessage("chat message");
  assert.equal(chat, "reply 1");
  assert.deepEqual(await first, { response: "reply 0" });
  assert.match(SRC, /let timeoutMs = 620000;/, "a turn without a deadline keeps the 620 s WebSocket limit");
});

test("cron caller deadline: 780 s default from arrival, or callerBudgetMs less a 20 s margin", () => {
  const cron = loadCron(1);
  assert.equal(cron.CRON_DEFAULT_WAIT_MS, 780000);
  assert.deepEqual(cron.cronCallerDeadline(1000, undefined), { ok: true, deadline: 781000 });
  assert.deepEqual(cron.cronCallerDeadline(1000, 600000), { ok: true, deadline: 581000 });
  assert.deepEqual(cron.cronCallerDeadline(1000, 900000), { ok: true, deadline: 881000 });
  for (const bad of [0, -1, 1.5, "600000", 900001, null, Number.NaN]) {
    assert.deepEqual(cron.cronCallerDeadline(1000, bad), { ok: false }, String(bad));
  }
  // The default fits the V1 cron Lambda (cdk.json 900 s): read timeout 870 s.
  const cdk = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "cdk.json"), "utf8"));
  const lambdaTimeoutMs = cdk.context.cron_lambda_timeout_seconds * 1000;
  assert.ok(cron.CRON_DEFAULT_WAIT_MS <= lambdaTimeoutMs - 30000 - 60000, "replies >= 60 s inside the read timeout");
});

test("the fixed failure text names the schedule only from the cron prefix", () => {
  const cron = loadCron(1);
  assert.equal(cron.cronFailureText(EN), `[Scheduled: evening-briefing-en] ${FAILED}`);
  assert.equal(cron.cronFailureText("no prefix"), FAILED);
  assert.equal(cron.cronFailureText(`[Scheduled task: ${"x".repeat(101)}] y`), FAILED);
  assert.equal(cron.cronFailureText(undefined), FAILED);
});

test("the cron handler validates callerBudgetMs, runs runCronTurn and has no lightweight-agent fallback", () => {
  const start = SRC.indexOf('if (action === "cron") {');
  const end = SRC.indexOf('if (action === "chat") {', start);
  assert.ok(start !== -1 && end > start);
  const branch = SRC.slice(start, end);
  assert.doesNotMatch(branch, /agent\.chat\(/, "no shim fallback on cron turns");
  assert.match(branch, /cronCallerDeadline\(requestArrival, payload\.callerBudgetMs\)/);
  assert.match(branch, /writeHead\(400[\s\S]*Invalid callerBudgetMs/);
  assert.match(branch, /runCronTurn\(message, budget\.deadline\)/);
  assert.ok(branch.indexOf("cronCallerDeadline(") < branch.indexOf("await init("), "budget is checked before init");
});
