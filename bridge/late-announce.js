/**
 * Forward late sub-agent announce finals to the user (issue #148, option B).
 *
 * When a sub-agent finishes while its requester session is idle, openclaw
 * 2026.9.5 starts an "announce" run in that session through the gateway's
 * `agent` method. Its run id is the announce idempotency key,
 * `announce:v1:<child session key>:<child run id>` (upstream
 * src/agents/announce-idempotency.ts, isAnnounceRunId). The container has no
 * channels, so that run's final only lands in the transcript and the user
 * sees it on their next turn.
 *
 * The relay watches chat finals, keeps only announce runs in the main
 * session that no bridge turn returned, and hands their answer to `deliver`,
 * which uses the bridge's existing Telegram send path. `message` stays
 * denied (#122); nothing here lets the agent pick a destination.
 *
 * Ordering and dedupe:
 * - A final that arrives while a turn is active waits until every active turn
 *   has ended plus `settleMs`, so it never goes out ahead of the main reply.
 *   If the turn returned that run itself (a sessions_yield successor), it is
 *   dropped.
 * - Each announce run id is handled at most once per container.
 * - The destination is the route of the latest turn, which the contract has
 *   already checked against the runtime's user binding.
 * - The announce run does not say which turn spawned the sub-agent, so the
 *   latest route is only known to be the spawning conversation while this
 *   activation has served a single conversation (channel + chat). Once a
 *   second one appears (a group chat, another linked channel), announces are
 *   not delivered: the answer stays in the transcript for the next turn
 *   rather than going to a chat that may not have asked for it.
 */
const { isSubagentSessionKey } = require("./chat-run-filter");

const ANNOUNCE_RUN_PREFIX = "announce:";
const SILENT_REPLIES = new Set(["NO_REPLY", "HEARTBEAT_OK"]);
const DEFAULT_SETTLE_MS = 5000;
const DEFAULT_MAX_REMEMBERED = 500;

/** Mirrors upstream isAnnounceRunId (announce-idempotency.ts). */
function isAnnounceRunId(runId) {
  return typeof runId === "string" && runId.startsWith(ANNOUNCE_RUN_PREFIX);
}

/** openclaw's silent reply tokens mean the run chose not to say anything. */
function isSilentReply(text) {
  const trimmed = typeof text === "string" ? text.trim() : "";
  return !trimmed || SILENT_REPLIES.has(trimmed);
}

/** A Set that forgets its oldest entries past `limit`. */
function boundedSet(limit) {
  const items = new Set();
  return {
    has: (value) => items.has(value),
    add: (value) => {
      items.delete(value);
      items.add(value);
      while (items.size > limit) items.delete(items.values().next().value);
    },
  };
}

/** Where a reply to this route goes: the channel and its chat. */
function conversationKey(route) {
  return JSON.stringify([route.channel ?? null, route.channelTarget ?? null]);
}

function bufferText(payload) {
  const content = payload?.message?.content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.filter((block) => block?.type === "text" && typeof block.text === "string")
    .map((block) => block.text).join("");
}

/**
 * @param {object} options
 * @param {(entry: {runId: string, sessionKey: string, buffer: string}) => Promise<string|null>} options.fetchAnswer
 * @param {(text: string, route: object, meta: {runId: string}) => Promise<unknown>} options.deliver
 */
function createLateAnnounceRelay({
  fetchAnswer, deliver, log = console, settleMs = DEFAULT_SETTLE_MS, maxRemembered = DEFAULT_MAX_REMEMBERED,
}) {
  const seen = boundedSet(maxRemembered); // announce runs already queued or handled
  const claimed = boundedSet(maxRemembered); // runs some bridge turn returned
  const pending = [];
  const activeTurns = new Set();
  let route = null;
  let firstConversation = null; // channel + chat of the first routed turn
  let multipleConversations = false;
  let closed = false;
  let timer = null;
  let draining = null;
  let idleWaiters = [];

  const settleIdle = () => {
    if (pending.length || timer || draining) return;
    const waiters = idleWaiters;
    idleWaiters = [];
    for (const resolve of waiters) resolve();
  };

  const drain = async () => {
    while (pending.length && !closed && activeTurns.size === 0) {
      const entry = pending.shift();
      if (claimed.has(entry.runId)) continue;
      const target = route;
      if (!target) {
        log?.warn?.("[late-announce] No delivery route yet; announce run not delivered");
        continue;
      }
      if (multipleConversations) {
        log?.warn?.("[late-announce] Runtime served more than one conversation; announce run not delivered");
        continue;
      }
      try {
        const text = await fetchAnswer(entry);
        if (closed || claimed.has(entry.runId)) continue;
        if (isSilentReply(text)) {
          log?.log?.("[late-announce] Announce run ended without text to deliver");
          continue;
        }
        await deliver(text.trim(), target, { runId: entry.runId });
        log?.log?.("[late-announce] Delivered a late sub-agent announce; chars:", text.trim().length);
      } catch (error) {
        log?.warn?.("[late-announce] Late announce delivery failed:", error?.message || "unknown error");
      }
    }
  };

  const schedule = () => {
    if (closed || timer || draining || activeTurns.size || !pending.length) return settleIdle();
    timer = setTimeout(() => {
      timer = null;
      if (closed || activeTurns.size) return settleIdle();
      draining = drain().finally(() => {
        draining = null;
        schedule();
      });
    }, settleMs);
    timer.unref?.();
  };

  /**
   * Mark a bridge turn active. `claim(runId)` records a run the turn returns;
   * `end()` lets held finals go out. `route` is the turn's validated channel.
   */
  const beginTurn = (turnRoute) => {
    const token = {};
    activeTurns.add(token);
    if (turnRoute && typeof turnRoute === "object") {
      route = Object.freeze({ ...turnRoute });
      const conversation = conversationKey(route);
      if (firstConversation === null) firstConversation = conversation;
      else if (conversation !== firstConversation) multipleConversations = true;
    }
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    return {
      claim: (runId) => { if (typeof runId === "string" && runId) claimed.add(runId); },
      end: () => {
        if (!activeTurns.delete(token)) return;
        schedule();
      },
    };
  };

  const onChatEvent = (payload) => {
    if (closed || !payload || typeof payload !== "object") return;
    if (payload.state !== "final" || payload.yielded === true) return;
    const { runId, sessionKey } = payload;
    if (!isAnnounceRunId(runId) || typeof sessionKey !== "string" || !sessionKey) return;
    if (isSubagentSessionKey(sessionKey) || payload.spawnedBy) return;
    if (seen.has(runId) || claimed.has(runId)) return;
    seen.add(runId);
    pending.push({ runId, sessionKey, buffer: bufferText(payload) });
    schedule();
  };

  /** Resolves when nothing is queued or being sent (for tests and shutdown). */
  const idle = () => new Promise((resolve) => {
    idleWaiters.push(resolve);
    settleIdle();
  });

  const close = () => {
    closed = true;
    pending.length = 0;
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    settleIdle();
  };

  return { beginTurn, onChatEvent, idle, close };
}

module.exports = { createLateAnnounceRelay, isAnnounceRunId, isSilentReply, ANNOUNCE_RUN_PREFIX };
