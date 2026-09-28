/**
 * Chat-event ownership filter for the contract's WebSocket bridge.
 *
 * The gateway broadcasts `chat` events for EVERY run to every operator
 * connection, including runs in sub-agent sessions the main run spawned via
 * `sessions_spawn`. Without a filter, a sub-agent's `final` resolves the
 * bridge request with the sub-agent's text and closes the socket while the
 * main run is still working (seen in production with a cron brief).
 *
 * Gateway protocol v4 (openclaw 2026.9.5, packages/gateway-protocol
 * ChatEventBaseSchema): every chat event carries `runId` and `sessionKey`
 * (both required), plus optional `spawnedBy` for sub-agent sessions.
 * `chat.send` uses the request's `idempotencyKey` as the run id
 * (`clientRunId = p.idempotencyKey`) and echoes it as `runId` in the ack.
 * `sessionKey: "global"` resolves to the agent's main session
 * (`agent:<agentId>:<mainKey>`, i.e. `agent:main:main`).
 *
 * A run that calls `sessions_yield` ends with `final` + `yielded: true`; the
 * subagent registry then starts a successor run (new runId) in the SAME
 * session to deliver the real answer. The tracker follows that handoff.
 */

/** Mirrors upstream isSubagentSessionKey (session-key.ts). */
function isSubagentSessionKey(sessionKey) {
  if (typeof sessionKey !== "string") return false;
  const raw = sessionKey.trim().toLowerCase();
  if (!raw) return false;
  if (raw.startsWith("subagent:")) return true;
  const parts = raw.split(":");
  return parts[0] === "agent" && parts.length >= 3 && parts[2] === "subagent";
}

/**
 * Track which gateway run a single bridge request owns.
 * @param {string} runId - the idempotencyKey sent with chat.send
 */
function createChatRunTracker(runId) {
  const state = {
    runId: runId || null,
    sessionKey: null,
    awaitingSuccessor: false,
  };

  /** Adopt runId/sessionKey from the chat.send ack payload when present. */
  const adoptAck = (payload) => {
    if (!payload || typeof payload !== "object") return;
    if (typeof payload.runId === "string" && payload.runId) state.runId = payload.runId;
    if (typeof payload.sessionKey === "string" && payload.sessionKey && !state.sessionKey) {
      state.sessionKey = payload.sessionKey;
    }
  };

  /**
   * Classify one chat event payload.
   * @returns {{action: "accept"|"ignore"|"yield", reason: string}}
   *   accept — belongs to our run, handle normally
   *   yield  — our run yielded (final + yielded:true); keep waiting for its successor
   *   ignore — another run or a sub-agent session
   */
  const classify = (payload) => {
    const pl = payload || {};
    const evRun = typeof pl.runId === "string" && pl.runId ? pl.runId : null;
    const evKey = typeof pl.sessionKey === "string" && pl.sessionKey ? pl.sessionKey : null;

    if (isSubagentSessionKey(evKey) || pl.spawnedBy) {
      return { action: "ignore", reason: "subagent" };
    }

    if (evRun && state.runId && evRun === state.runId) {
      if (evKey && !state.sessionKey) state.sessionKey = evKey;
      if (pl.state === "final" && pl.yielded === true) {
        state.awaitingSuccessor = true;
        return { action: "yield", reason: "yielded" };
      }
      return { action: "accept", reason: "own-run" };
    }

    if (evRun) {
      if (state.awaitingSuccessor && evKey && state.sessionKey && evKey === state.sessionKey) {
        state.runId = evRun;
        state.awaitingSuccessor = false;
        if (pl.state === "final" && pl.yielded === true) {
          state.awaitingSuccessor = true;
          return { action: "yield", reason: "yielded" };
        }
        return { action: "accept", reason: "successor-run" };
      }
      return { action: "ignore", reason: "other-run" };
    }

    // Event without runId (not valid under protocol v4). Keep the old
    // behaviour unless the session key shows it belongs elsewhere.
    if (evKey && state.sessionKey && evKey !== state.sessionKey) {
      return { action: "ignore", reason: "other-session" };
    }
    return { action: "accept", reason: "no-run-id" };
  };

  return { adoptAck, classify, state };
}

module.exports = { createChatRunTracker, isSubagentSessionKey };
