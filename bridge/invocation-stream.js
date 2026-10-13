/**
 * SSE response handling for one bridge invocation.
 *
 * Readiness is emitted only after all user resources are marked ready.
 * Cumulative gateway text becomes append-only deltas; if a successor replaces
 * the visible prefix, further deltas stop and the final response reports the
 * mismatch. First-useful-text timing is captured when its frame is written.
 *
 * Frames wait for drain under backpressure, with a total output limit and a
 * deadline. Disconnects and write failures abort the exposed signal; normal
 * completion waits for queued frames before ending the response.
 */
function createInvocationStream(req, res, arrival, timeoutMs) {
  const controller = new AbortController();
  let finished = false, text = "", firstText = null, correlation = null, bytes = 0;
  let ending = false, blocked = false, suppressDeltas = false;
  const queue = [];
  const abort = () => {
    if (finished) return;
    controller.abort();
    queue.length = 0;
    finish();
  };
  req.once("aborted", abort);
  res.once("close", abort);
  res.once("error", abort);
  res.on("drain", drain);
  const timer = setTimeout(abort, timeoutMs);
  if (req.aborted || res.destroyed) abort();
  function send(event, body, useful = false) {
    controller.signal.throwIfAborted();
    if (finished || ending) throw new Error("Stream is closed.");
    if (!res.headersSent) {
      res.writeHead(200, {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
      });
      res.flushHeaders();
    }
    const frame = `event: ${event}\ndata: ${JSON.stringify(body)}\n\n`;
    bytes += Buffer.byteLength(frame);
    if (bytes > 1024 * 1024) { abort(); throw new Error("Stream output limit exceeded."); }
    queue.push({ frame, useful });
    flush();
  }
  function flush() {
    try {
      while (!finished && !blocked && queue.length) {
        const { frame, useful } = queue.shift();
        const stamp = firstText === null && useful ? process.hrtime.bigint() : null;
        // A false return accepts this frame; only subsequent frames wait for drain.
        blocked = res.write(frame) === false;
        if (stamp !== null) {
          firstText = stamp;
          try {
            console.log("[TTFT] " + JSON.stringify({
              schema: "openclaw.streaming-ttft.v1", type: "first_useful_text", ...correlation,
              monotonic_ns: stamp.toString(),
              runtime_ttft_ms: arrival?.monotonic_ns
                ? Number(stamp - BigInt(arrival.monotonic_ns)) / 1e6 : null,
            }));
          } catch {}
        }
      }
      if (ending && !blocked && !queue.length) finish();
    } catch { abort(); }
  }
  function drain() {
    blocked = false;
    flush();
  }
  function finish() {
    if (finished) return;
    finished = true;
    clearTimeout(timer);
    req.removeListener("aborted", abort);
    res.removeListener("close", abort);
    res.removeListener("error", abort);
    res.removeListener("drain", drain);
    if (!res.destroyed) res.end();
  }
  function close() {
    ending = true;
    flush();
  }
  return {
    signal: controller.signal,
    ready(lifecycle) {
      if (lifecycle.state !== "USER_READY" ||
          !["restore", "credentials", "proxy", "gateway", "browser", "skills", "persistence"]
            .every((name) => lifecycle.resources.includes(name))) {
        throw new Error("Full user readiness has not been established.");
      }
      correlation = { request_id: arrival?.request_id ?? null,
        runtime_session_id: lifecycle.binding.runtimeSessionId, generation: lifecycle.generation };
      send("ready", { ...correlation, user_ready: true, source_phase: "USER_READY",
        monotonic_ns: process.hrtime.bigint().toString(), resources: lifecycle.resources });
    },
    delta(snapshot) {
      if (typeof snapshot !== "string" || suppressDeltas || snapshot === text || controller.signal.aborted) return;
      // A yielded successor may replace its cumulative text; retain the visible prefix until done.
      if (!snapshot.startsWith(text)) { suppressDeltas = true; return; }
      const delta = snapshot.slice(text.length);
      text = snapshot;
      if (!delta) return;
      try {
        send("delta", { text: delta }, !!delta.trim());
      } catch { abort(); }
    },
    done(result) {
      try {
        send("done", { ...result, ...correlation,
          streamed_text_matches_final: !suppressDeltas && text === result.response });
      }
      finally { close(); }
    },
    error(code) {
      try { if (!controller.signal.aborted && !res.destroyed) send("error", { code, ...correlation }); }
      finally { close(); }
    },
    close,
  };
}

module.exports = { createInvocationStream };
