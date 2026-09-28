/**
 * Tests for chat-run-filter.js and its wiring in agentcore-contract.js.
 * Run: node --test chat-run-filter.test.js
 *
 * Regression: a cron brief's main run (agent:main:main) spawned a sub-agent.
 * The sub-agent's `chat` final resolved the bridge request with the
 * sub-agent's report, and the main run's final had no client left.
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

const { createChatRunTracker, isSubagentSessionKey } = require("./chat-run-filter");

const MAIN_KEY = "agent:main:main";
const SUB_KEY = "agent:main:subagent:0000aaaa-1111-2222-3333-444455556666";
const OUR_RUN = "run-ours-0001";
const SUB_RUN = "run-sub-0002";

const textMsg = (text) => ({ role: "assistant", content: [{ type: "text", text }] });

/**
 * Minimal replica of the contract's chat-event handling (bridgeMessage step 3)
 * using the tracker the same way. Returns the resolved text, or undefined if
 * the request is still open after all events.
 */
function runSequence(events, { ack } = {}) {
  const tracker = createChatRunTracker(OUR_RUN);
  if (ack) tracker.adoptAck(ack);
  let responseText = "";
  let resolved;
  const deltas = [];
  const ignored = [];
  const done = (t) => {
    if (resolved === undefined) resolved = t;
  };
  for (const payload of events) {
    if (resolved !== undefined) break;
    const verdict = tracker.classify(payload);
    if (verdict.action === "ignore") {
      ignored.push(verdict.reason);
      continue;
    }
    if (verdict.action === "yield") continue;
    const text = payload.message?.content?.[0]?.text || "";
    if (payload.state === "delta") {
      if (text) {
        responseText = text;
        deltas.push(text);
      }
    } else if (payload.state === "final") {
      if (text) responseText = text;
      done(responseText || "");
    } else if (payload.state === "error") {
      done(responseText || `Chat error: ${payload.errorMessage || "unknown"}`);
    } else if (payload.state === "aborted") {
      done(responseText || "Chat aborted.");
    }
  }
  return { resolved, responseText, deltas, ignored };
}

describe("isSubagentSessionKey", () => {
  it("matches agent-scoped and bare sub-agent keys", () => {
    assert.equal(isSubagentSessionKey(SUB_KEY), true);
    assert.equal(isSubagentSessionKey("subagent:abc"), true);
    assert.equal(isSubagentSessionKey("AGENT:Main:SubAgent:x"), true);
  });
  it("does not match main, cron or missing keys", () => {
    assert.equal(isSubagentSessionKey(MAIN_KEY), false);
    assert.equal(isSubagentSessionKey("agent:main:cron:job-1"), false);
    assert.equal(isSubagentSessionKey("global"), false);
    assert.equal(isSubagentSessionKey(undefined), false);
  });
});

describe("chat run tracker", () => {
  it("sub-agent final arriving before the main final does not resolve the request", () => {
    const r = runSequence([
      { runId: OUR_RUN, sessionKey: MAIN_KEY, seq: 1, state: "delta", message: textMsg("Working on the brief") },
      { runId: SUB_RUN, sessionKey: SUB_KEY, spawnedBy: MAIN_KEY, seq: 1, state: "final", message: textMsg("SUB-AGENT REPORT") },
    ]);
    assert.equal(r.resolved, undefined);
    assert.deepEqual(r.ignored, ["subagent"]);
  });

  it("the main final resolves with the main text after a sub-agent final", () => {
    const r = runSequence([
      { runId: SUB_RUN, sessionKey: SUB_KEY, seq: 1, state: "final", message: textMsg("SUB-AGENT REPORT") },
      { runId: OUR_RUN, sessionKey: MAIN_KEY, seq: 2, state: "final", message: textMsg("MAIN BRIEF") },
    ]);
    assert.equal(r.resolved, "MAIN BRIEF");
  });

  it("sub-agent deltas do not overwrite responseText or reach onDelta", () => {
    const r = runSequence([
      { runId: OUR_RUN, sessionKey: MAIN_KEY, seq: 1, state: "delta", message: textMsg("main partial") },
      { runId: SUB_RUN, sessionKey: SUB_KEY, seq: 1, state: "delta", message: textMsg("sub partial") },
    ]);
    assert.equal(r.responseText, "main partial");
    assert.deepEqual(r.deltas, ["main partial"]);
  });

  it("ignores a sub-agent event identified only by spawnedBy", () => {
    const r = runSequence([
      { runId: SUB_RUN, sessionKey: "agent:main:dashboard:x", spawnedBy: MAIN_KEY, seq: 1, state: "final", message: textMsg("SUB") },
    ]);
    assert.equal(r.resolved, undefined);
  });

  it("ignores another run's final in the main session", () => {
    const r = runSequence([
      { runId: "run-someone-else", sessionKey: MAIN_KEY, seq: 1, state: "final", message: textMsg("OTHER") },
      { runId: OUR_RUN, sessionKey: MAIN_KEY, seq: 2, state: "final", message: textMsg("OURS") },
    ]);
    assert.equal(r.resolved, "OURS");
  });

  it("filters error and aborted states the same way", () => {
    const r = runSequence([
      { runId: SUB_RUN, sessionKey: SUB_KEY, seq: 1, state: "error", errorMessage: "sub failed" },
      { runId: SUB_RUN, sessionKey: SUB_KEY, seq: 2, state: "aborted" },
      { runId: "run-other", sessionKey: MAIN_KEY, seq: 1, state: "error", errorMessage: "other failed" },
    ]);
    assert.equal(r.resolved, undefined);
    const own = runSequence([{ runId: OUR_RUN, sessionKey: MAIN_KEY, seq: 1, state: "error", errorMessage: "boom" }]);
    assert.equal(own.resolved, "Chat error: boom");
    const aborted = runSequence([{ runId: OUR_RUN, sessionKey: MAIN_KEY, seq: 1, state: "aborted" }]);
    assert.equal(aborted.resolved, "Chat aborted.");
  });

  it("an empty final from our run still resolves with an empty string", () => {
    const r = runSequence([{ runId: OUR_RUN, sessionKey: MAIN_KEY, seq: 1, state: "final" }]);
    assert.equal(r.resolved, "");
  });

  it("adopts the runId echoed by the chat.send ack", () => {
    const r = runSequence(
      [{ runId: "run-from-ack", sessionKey: MAIN_KEY, seq: 1, state: "final", message: textMsg("ACKED") }],
      { ack: { runId: "run-from-ack", status: "started" } },
    );
    assert.equal(r.resolved, "ACKED");
  });

  it("follows a sessions_yield handoff to the successor run in the same session", () => {
    const r = runSequence([
      { runId: OUR_RUN, sessionKey: MAIN_KEY, seq: 1, state: "delta", message: textMsg("spawning helpers") },
      { runId: OUR_RUN, sessionKey: MAIN_KEY, seq: 2, state: "final", yielded: true, message: textMsg("spawning helpers") },
      { runId: SUB_RUN, sessionKey: SUB_KEY, seq: 1, state: "final", message: textMsg("SUB-AGENT REPORT") },
      { runId: "run-successor", sessionKey: MAIN_KEY, seq: 1, state: "final", message: textMsg("FINAL BRIEF") },
    ]);
    assert.equal(r.resolved, "FINAL BRIEF");
  });

  it("does not adopt a successor from a different session after a yield", () => {
    const r = runSequence([
      { runId: OUR_RUN, sessionKey: MAIN_KEY, seq: 1, state: "final", yielded: true },
      { runId: "run-cron", sessionKey: "agent:main:cron:job-1", seq: 1, state: "final", message: textMsg("CRON") },
    ]);
    assert.equal(r.resolved, undefined);
  });

  it("keeps legacy behaviour for events without a runId", () => {
    const r = runSequence([{ state: "final", message: textMsg("LEGACY") }]);
    assert.equal(r.resolved, "LEGACY");
  });
});

describe("contract wiring", () => {
  const src = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf-8");
  const dockerfile = fs.readFileSync(path.join(__dirname, "Dockerfile"), "utf-8");

  it("contract requires the filter and creates a tracker per chat.send", () => {
    assert.match(src, /require\("\.\/chat-run-filter"\)/);
    assert.match(src, /createChatRunTracker\(chatReqId\)/);
  });

  it("chat events are classified before delta/final/error/aborted handling", () => {
    const start = src.indexOf('msg.event === "chat"');
    assert.notEqual(start, -1);
    const block = src.slice(start, src.indexOf("// Step 4", start));
    const classifyAt = block.indexOf("chatRun.classify(");
    assert.notEqual(classifyAt, -1, "chat handler must classify events");
    for (const st of ['"delta"', '"final"', '"error"', '"aborted"']) {
      const at = block.indexOf(`payload.state === ${st}`);
      assert.ok(at > classifyAt, `${st} handling must come after classify`);
    }
  });

  it("ignored-event log uses a constant format string", () => {
    assert.match(src, /console\.log\(\s*"\[contract\] Ignoring chat %s for runId=%s sessionKey=%s \(%s\)"/);
  });

  it("chat.send ack runId is adopted", () => {
    assert.match(src, /chatRun\.adoptAck\(msg\.payload\)/);
  });

  it("Dockerfile copies chat-run-filter.js into /app", () => {
    assert.match(dockerfile, /COPY chat-run-filter\.js \/app\/chat-run-filter\.js/);
  });
});
