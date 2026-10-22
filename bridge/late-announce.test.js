/**
 * Unit tests for the late sub-agent announce relay (issue #148, option B).
 * Run: node --test late-announce.test.js
 *
 * On OpenClaw 2.0 a sub-agent that finishes after the main run has replied
 * reports back through an "announce" run in the main session. Its run id is
 * the announce idempotency key ("announce:v1:<child session>:<child run>").
 * Nothing waits on that run, so its final only lands in the transcript.
 * The relay forwards it through the bridge's own delivery path, once, to the
 * chat of the session owner's latest turn, and never ahead of the main reply.
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { createLateAnnounceRelay, isAnnounceRunId, isSilentReply } = require("./late-announce");

const MAIN_KEY = "agent:main:main";
const SUB_KEY = "agent:main:subagent:0000aaaa-1111-2222-3333-444455556666";
const ANNOUNCE_RUN = `announce:v1:${SUB_KEY}:child-run-1`;
const ROUTE = Object.freeze({
  actorId: "telegram:<telegram_id>", channel: "telegram",
  channelActorId: "telegram:<telegram_id>", channelTarget: "<telegram_id>",
});
const quiet = { log: () => {}, warn: () => {}, error: () => {} };
const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

function harness({ answers = {}, settleMs = 10 } = {}) {
  const delivered = [];
  const fetched = [];
  const relay = createLateAnnounceRelay({
    settleMs, log: quiet,
    fetchAnswer: async (entry) => {
      fetched.push(entry.runId);
      return Object.hasOwn(answers, entry.runId) ? answers[entry.runId] : entry.buffer;
    },
    deliver: async (text, route, meta) => { delivered.push({ text, route, runId: meta.runId }); },
  });
  return { relay, delivered, fetched };
}

const finalEvent = (runId, extra = {}) => ({
  runId, sessionKey: MAIN_KEY, seq: 3, state: "final",
  message: { role: "assistant", content: [{ type: "text", text: "buffer text" }] }, ...extra,
});

describe("isAnnounceRunId", () => {
  it("matches openclaw's announce idempotency key prefix only", () => {
    assert.equal(isAnnounceRunId(ANNOUNCE_RUN), true);
    assert.equal(isAnnounceRunId(`${ANNOUNCE_RUN}:wake`), true);
    assert.equal(isAnnounceRunId("0f6c1a52-run"), false);
    assert.equal(isAnnounceRunId("xannounce:v1"), false);
    assert.equal(isAnnounceRunId(undefined), false);
  });
});

describe("isSilentReply", () => {
  it("treats openclaw's silent tokens and blank text as nothing to deliver", () => {
    assert.equal(isSilentReply("NO_REPLY"), true);
    assert.equal(isSilentReply("  HEARTBEAT_OK \n"), true);
    assert.equal(isSilentReply("   "), true);
    assert.equal(isSilentReply("NO_REPLY needed? Here is the summary"), false);
  });
});

describe("late announce relay", () => {
  it("delivers an announce final that arrives after the main turn ended", async () => {
    const { relay, delivered } = harness({ answers: { [ANNOUNCE_RUN]: "SUB-AGENT SUMMARY" } });
    const turn = relay.beginTurn(ROUTE);
    turn.claim("main-run");
    turn.end();
    relay.onChatEvent(finalEvent(ANNOUNCE_RUN));
    await relay.idle();
    assert.deepEqual(delivered, [{ text: "SUB-AGENT SUMMARY", route: ROUTE, runId: ANNOUNCE_RUN }]);
    relay.close();
  });

  it("holds an announce final that arrives while the main run is active until the turn ends", async () => {
    const { relay, delivered } = harness({ answers: { [ANNOUNCE_RUN]: "SUB-AGENT SUMMARY" } });
    const turn = relay.beginTurn(ROUTE);
    turn.claim("main-run");
    relay.onChatEvent(finalEvent(ANNOUNCE_RUN));
    await tick(40);
    assert.equal(delivered.length, 0, "must not be sent ahead of the main reply");
    turn.end();
    await relay.idle();
    assert.equal(delivered.length, 1);
    assert.equal(delivered[0].text, "SUB-AGENT SUMMARY");
    relay.close();
  });

  it("waits the settle time after the turn ends so the main reply goes out first", async () => {
    const { relay, delivered } = harness({ answers: { [ANNOUNCE_RUN]: "S" }, settleMs: 80 });
    const turn = relay.beginTurn(ROUTE);
    relay.onChatEvent(finalEvent(ANNOUNCE_RUN));
    turn.end();
    await tick(30);
    assert.equal(delivered.length, 0);
    await relay.idle();
    assert.equal(delivered.length, 1);
    relay.close();
  });

  it("never delivers an announce run the active turn owns (sessions_yield successor)", async () => {
    const { relay, delivered, fetched } = harness({ answers: { [ANNOUNCE_RUN]: "FINAL BRIEF" } });
    const turn = relay.beginTurn(ROUTE);
    turn.claim("main-run");
    relay.onChatEvent(finalEvent(ANNOUNCE_RUN));
    turn.claim(ANNOUNCE_RUN); // the turn adopted it after the watcher saw it
    turn.end();
    await relay.idle();
    assert.deepEqual(delivered, []);
    assert.deepEqual(fetched, []);
    relay.close();
  });

  it("does not deliver a run an earlier turn already returned", async () => {
    const { relay, delivered } = harness();
    const turn = relay.beginTurn(ROUTE);
    turn.claim(ANNOUNCE_RUN);
    turn.end();
    relay.onChatEvent(finalEvent(ANNOUNCE_RUN));
    await relay.idle();
    assert.deepEqual(delivered, []);
    relay.close();
  });

  it("delivers each announce run at most once", async () => {
    const { relay, delivered } = harness({ answers: { [ANNOUNCE_RUN]: "ONCE" } });
    relay.beginTurn(ROUTE).end();
    relay.onChatEvent(finalEvent(ANNOUNCE_RUN));
    relay.onChatEvent(finalEvent(ANNOUNCE_RUN, { seq: 4 }));
    await relay.idle();
    relay.onChatEvent(finalEvent(ANNOUNCE_RUN, { seq: 5 }));
    await relay.idle();
    assert.equal(delivered.length, 1);
    relay.close();
  });

  it("ignores ordinary runs, sub-agent sessions, deltas, yields and errors", async () => {
    const { relay, delivered } = harness({ answers: { [ANNOUNCE_RUN]: "X" } });
    relay.beginTurn(ROUTE).end();
    relay.onChatEvent(finalEvent("plain-run"));
    relay.onChatEvent(finalEvent(ANNOUNCE_RUN, { sessionKey: SUB_KEY }));
    relay.onChatEvent(finalEvent(ANNOUNCE_RUN, { spawnedBy: MAIN_KEY }));
    relay.onChatEvent(finalEvent(ANNOUNCE_RUN, { state: "delta" }));
    relay.onChatEvent(finalEvent(ANNOUNCE_RUN, { yielded: true }));
    relay.onChatEvent(finalEvent(ANNOUNCE_RUN, { state: "error" }));
    await relay.idle();
    assert.deepEqual(delivered, []);
    // The run was not consumed by any ignored event, so its real final still goes out.
    relay.onChatEvent(finalEvent(ANNOUNCE_RUN));
    await relay.idle();
    assert.equal(delivered.length, 1);
    relay.close();
  });

  it("skips a silent announce reply", async () => {
    const { relay, delivered } = harness({ answers: { [ANNOUNCE_RUN]: "NO_REPLY" } });
    relay.beginTurn(ROUTE).end();
    relay.onChatEvent(finalEvent(ANNOUNCE_RUN));
    await relay.idle();
    assert.deepEqual(delivered, []);
    relay.close();
  });

  it("delivers nothing before any turn has supplied the owner's route", async () => {
    const { relay, delivered } = harness({ answers: { [ANNOUNCE_RUN]: "X" } });
    relay.onChatEvent(finalEvent(ANNOUNCE_RUN));
    await relay.idle();
    assert.deepEqual(delivered, []);
    relay.close();
  });

  it("sends to the latest turn's route", async () => {
    const { relay, delivered } = harness({ answers: { [ANNOUNCE_RUN]: "X" } });
    relay.beginTurn(ROUTE).end();
    const group = { ...ROUTE, channelTarget: "-100<telegram_id>" };
    relay.beginTurn(group).end();
    relay.onChatEvent(finalEvent(ANNOUNCE_RUN));
    await relay.idle();
    assert.equal(delivered[0].route.channelTarget, "-100<telegram_id>");
    relay.close();
  });

  it("keeps working after a fetch or delivery failure", async () => {
    const delivered = [];
    let failNext = true;
    const relay = createLateAnnounceRelay({
      settleMs: 1, log: quiet,
      fetchAnswer: async (entry) => entry.runId,
      deliver: async (text) => {
        if (failNext) { failNext = false; throw new Error("Telegram API timeout"); }
        delivered.push(text);
      },
    });
    relay.beginTurn(ROUTE).end();
    relay.onChatEvent(finalEvent(`${ANNOUNCE_RUN}-a`));
    await relay.idle();
    relay.onChatEvent(finalEvent(`${ANNOUNCE_RUN}-b`));
    await relay.idle();
    assert.deepEqual(delivered, [`${ANNOUNCE_RUN}-b`]);
    relay.close();
  });

  it("drops pending deliveries on close", async () => {
    const { relay, delivered } = harness({ answers: { [ANNOUNCE_RUN]: "X" } });
    const turn = relay.beginTurn(ROUTE);
    relay.onChatEvent(finalEvent(ANNOUNCE_RUN));
    relay.close();
    turn.end();
    await relay.idle();
    assert.deepEqual(delivered, []);
  });

  it("an extra end() call does not release a later turn early", async () => {
    const { relay, delivered } = harness({ answers: { [ANNOUNCE_RUN]: "X" } });
    const first = relay.beginTurn(ROUTE);
    first.end();
    const second = relay.beginTurn(ROUTE);
    first.end();
    relay.onChatEvent(finalEvent(ANNOUNCE_RUN));
    await tick(40);
    assert.equal(delivered.length, 0);
    second.end();
    await relay.idle();
    assert.equal(delivered.length, 1);
    relay.close();
  });
});
