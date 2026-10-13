/**
 * Correlate invocation arrival times with entrypoint and startup-attempt IDs.
 *
 * A bounded buffer replays early markers when scoped logging becomes available,
 * preserving their original wall and monotonic timestamps. Delivery labels
 * distinguish replay from producer records, and telemetry errors are ignored.
 */
const { osRandomUUID } = require("./runtime-lifecycle");

const MAX_PENDING_REQUESTS = 8;
const ATTEMPT_ID = /^startup-[0-9a-f]{32}$/;
const EXECUTION_ID = /^entrypoint-([0-9]{13})-[0-9]{1,5}-[0-9]{1,5}$/;

function createStartupTiming({
  env = process.env,
  wallNow = Date.now,
  monotonicNow = () => process.hrtime.bigint(),
  emit = (record) => console.log(JSON.stringify(record)),
} = {}) {
  const lineage = typeof env.OPENCLAW_ENTRYPOINT_EXECUTION_ID === "string"
    ? EXECUTION_ID.exec(env.OPENCLAW_ENTRYPOINT_EXECUTION_ID) : null;
  const entrypoint = lineage && lineage[0] === env.OPENCLAW_ENTRYPOINT_EXECUTION_ID &&
    lineage[1] === env.OPENCLAW_ENTRYPOINT_WALL_MS
    ? Object.freeze({
      schema_version: 1,
      event: "ENTRYPOINT_EXEC",
      wall_unix_ms: Number(lineage[1]),
      entrypoint_execution_id: lineage[0],
    }) : null;
  let pending = entrypoint ? [entrypoint] : [];
  let scopedLogging = false;

  function publish(record, delivery) {
    try {
      emit(Object.freeze({ ...record, delivery }));
    } catch {
      // Telemetry must not interrupt request processing.
    }
  }

  function captureHttpHandler(method, url) {
    if (method !== "POST" || url !== "/invocations") return null;
    try {
      const wall = wallNow();
      const monotonic = monotonicNow();
      if (!Number.isSafeInteger(wall) || wall < 0 ||
          typeof monotonic !== "bigint" || monotonic < 0n) return null;
      const arrival = Object.freeze({
        wall_unix_ms: wall,
        monotonic_ns: monotonic.toString(),
        // Correlate this session-scoped ID with runtimeSessionId.
        request_id: osRandomUUID(),
      });
      publish({
        schema_version: 1,
        event: "PRE_ENTRYPOINT",
        marker: "[PRE-ENTRYPOINT]",
        ...arrival,
        entrypoint_execution_id: entrypoint?.entrypoint_execution_id || null,
        route_kind: "invocations",
      }, "producer");
      return arrival;
    } catch {
      return null;
    }
  }

  function recordHttpHandler(arrival, attemptId) {
    if (!arrival || typeof attemptId !== "string" || attemptId.length !== 40 ||
        !ATTEMPT_ID.test(attemptId)) return;
    const record = Object.freeze({
      schema_version: 1,
      event: "HTTP_HANDLER_ENTER",
      wall_unix_ms: arrival.wall_unix_ms,
      monotonic_ns: arrival.monotonic_ns,
      request_id: arrival.request_id,
      entrypoint_execution_id: entrypoint?.entrypoint_execution_id || null,
      route_kind: "invocations",
      measurement_attempt_id: attemptId,
    });
    if (!scopedLogging) {
      const limit = MAX_PENDING_REQUESTS + (entrypoint ? 1 : 0);
      if (pending.length === limit) pending.splice(entrypoint ? 1 : 0, 1);
      pending.push(record);
    }
    publish(record, "producer");
  }

  function enableScopedLogging() {
    if (scopedLogging) return;
    scopedLogging = true;
    const replay = pending;
    pending = [];
    for (const record of replay) publish(record, "replay");
  }

  return { captureHttpHandler, recordHttpHandler, enableScopedLogging };
}

module.exports = { createStartupTiming, MAX_PENDING_REQUESTS };
