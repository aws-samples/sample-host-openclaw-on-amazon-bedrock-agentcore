const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { Writable } = require("node:stream");
const { createInvocationStream } = require("./invocation-stream");

const resources = ["restore", "credentials", "proxy", "gateway", "browser", "skills", "persistence"];
const lifecycle = { state: "USER_READY", resources, generation: "generation",
  binding: { runtimeSessionId: "session" } };

function channel(t, timeoutMs = 1000) {
  const req = new EventEmitter(), res = new EventEmitter(), frames = [], order = [];
  res.writeHead = (code, headers) => { res.headersSent = true; res.code = code; res.headers = headers; };
  res.flushHeaders = () => order.push("headers");
  res.write = (value) => { frames.push(value); order.push("write"); return true; };
  res.end = () => { res.writableEnded = true; };
  const stream = createInvocationStream(req, res, {
    request_id: "request", monotonic_ns: process.hrtime.bigint().toString(),
  }, timeoutMs);
  t.after(stream.close);
  const records = () => frames.map((frame) => ({
    event: /^event: (.+)$/m.exec(frame)[1], ...JSON.parse(/^data: (.+)$/m.exec(frame)[1]),
  }));
  return { req, res, stream, order, records };
}

test("ready requires the full resource barrier, including Browser", (t) => {
  const { stream, records } = channel(t);
  for (const name of resources) assert.throws(() => stream.ready({
    ...lifecycle, resources: resources.filter((entry) => entry !== name),
  }), /Full user readiness/);
  assert.throws(() => stream.ready({ ...lifecycle, state: "BINDING" }), /Full user readiness/);
  assert.deepEqual(records(), []);
  stream.ready(lifecycle);
  assert.equal(records()[0].runtime_session_id, "session");
  assert.equal(records()[0].source_phase, "USER_READY");
});

test("cumulative text becomes incremental SSE; blank content never establishes TTFT", (t) => {
  const { stream, order, records, res } = channel(t);
  const timings = [];
  t.mock.method(console, "log", (record) => {
    assert.equal(order.at(-1), "write", "First text must be written before logging.");
    timings.push(JSON.parse(record.slice("[TTFT] ".length)));
  });
  stream.ready(lifecycle);
  stream.delta("");
  stream.delta(" ");
  assert.equal(timings.length, 0);
  stream.delta(" Hello");
  stream.delta(" Hello");
  stream.delta(" Hello world");
  stream.done({ response: " Hello world", execution: { phase: "completed", run_id: "run" } });
  assert.deepEqual(records().filter((r) => r.event === "delta").map((r) => r.text), [" ", "Hello", " world"]);
  assert.equal(timings.length, 1);
  assert.equal(timings[0].request_id, "request");
  assert.ok(timings[0].runtime_ttft_ms >= 0);
  assert.equal(records().at(-1).streamed_text_matches_final, true);
  assert.equal(res.writableEnded, true);
  assert.equal(JSON.stringify(timings).includes("Hello"), false);
});

test("logging failure cannot suppress the first text or final response", (t) => {
  const { stream, records } = channel(t);
  t.mock.method(console, "log", () => { throw new Error("sink failure"); });
  stream.ready(lifecycle);
  stream.delta("answer");
  stream.done({ response: "answer" });
  assert.deepEqual(records().map((r) => r.event), ["ready", "delta", "done"]);
});

test("yielded successor text waits for the actual final response without canceling the turn", (t) => {
  t.mock.method(console, "log", () => {});
  const { stream, records } = channel(t);
  stream.ready(lifecycle);
  stream.delta("first run");
  stream.delta("successor run");
  stream.delta("successor run completed");
  stream.delta("first run is also not appended");
  assert.equal(stream.signal.aborted, false);
  assert.deepEqual(records().filter((r) => r.event === "delta").map((r) => r.text), ["first run"]);
  stream.done({ response: "successor final", execution: { phase: "completed", run_id: "successor" } });
  assert.equal(records().at(-1).response, "successor final");
  assert.equal(records().at(-1).execution.run_id, "successor");
  assert.equal(records().at(-1).streamed_text_matches_final, false);
  assert.equal(stream.signal.aborted, false);
});

test("false writes are accepted once and queued frames drain in order through terminal flush", (t) => {
  const { stream, records, res } = channel(t);
  const timings = [], write = res.write;
  let count = 0;
  res.write = (frame) => {
    write(frame);
    count++;
    return count === 3;
  };
  t.mock.method(console, "log", (record) => timings.push(JSON.parse(record.slice("[TTFT] ".length))));
  stream.ready(lifecycle);
  stream.delta("Hello");
  stream.delta("Hello world");
  stream.done({ response: "Hello world" });
  stream.close();
  assert.deepEqual(records().map((r) => r.event), ["ready"]);
  assert.equal(timings.length, 0);
  assert.equal(stream.signal.aborted, false);
  assert.equal(res.writableEnded, undefined);
  const beforeWrite = process.hrtime.bigint();
  res.emit("drain");
  assert.deepEqual(records().map((r) => r.event), ["ready", "delta"]);
  assert.equal(timings.length, 1);
  assert.ok(BigInt(timings[0].monotonic_ns) >= beforeWrite);
  res.emit("drain");
  assert.deepEqual(records().map((r) => r.event), ["ready", "delta", "delta", "done"]);
  assert.deepEqual(records().filter((r) => r.event === "delta").map((r) => r.text), ["Hello", " world"]);
  assert.equal(records().at(-1).streamed_text_matches_final, true);
  assert.equal(res.writableEnded, undefined);
  res.emit("drain");
  assert.equal(res.writableEnded, true);
  assert.equal(count, 4);
  assert.equal(stream.signal.aborted, false);
  assert.equal(res.listenerCount("drain"), 0);
});

test("an actual slow Node Writable flushes each accepted frame once before finish", async (t) => {
  t.mock.method(console, "log", () => {});
  const frames = [], req = new EventEmitter();
  let release;
  const res = new Writable({ highWaterMark: 1, write(chunk, _encoding, callback) {
    frames.push(chunk.toString());
    release = callback;
  } });
  res.writeHead = () => { res.headersSent = true; };
  res.flushHeaders = () => {};
  const stream = createInvocationStream(req, res, null, 1000);
  t.after(() => { stream.close(); res.destroy(); });
  const finished = new Promise((resolve) => res.once("finish", resolve));
  stream.ready(lifecycle);
  stream.delta("a");
  stream.delta("ab");
  stream.done({ response: "ab" });
  for (let count = 1; count <= 4; count++) {
    assert.equal(frames.length, count);
    assert.equal(res.writableFinished, false);
    release();
    await new Promise(setImmediate);
  }
  await finished;
  assert.deepEqual(frames.map((frame) => /^event: (.+)$/m.exec(frame)[1]), ["ready", "delta", "delta", "done"]);
  assert.equal(stream.signal.aborted, false);
});

for (const event of ["close", "error"]) {
  test(`${event} discards queued frames without replay`, (t) => {
    const { stream, records, res } = channel(t);
    const write = res.write;
    res.write = (frame) => { write(frame); return false; };
    stream.ready(lifecycle);
    stream.delta("queued");
    stream.done({ response: "queued" });
    if (event === "close") res.destroyed = true;
    res.emit(event, new Error("fixture transport failure"));
    assert.equal(stream.signal.aborted, true);
    res.emit("drain");
    assert.deepEqual(records().map((r) => r.event), ["ready"]);
    assert.equal(res.listenerCount("drain"), 0);
  });
}

test("queued output remains bounded and a stalled drain retains the request timeout", async (t) => {
  const capped = channel(t), write = capped.res.write;
  capped.res.write = (frame) => { write(frame); return false; };
  capped.stream.ready(lifecycle);
  capped.stream.delta("x".repeat(1024 * 1024));
  assert.equal(capped.stream.signal.aborted, true);
  assert.deepEqual(capped.records().map((r) => r.event), ["ready"]);
  const timed = channel(t, 10), timedWrite = timed.res.write;
  timed.res.write = (frame) => { timedWrite(frame); return false; };
  timed.stream.ready(lifecycle);
  timed.stream.done({ response: "" });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(timed.stream.signal.aborted, true);
  timed.res.emit("drain");
  assert.deepEqual(timed.records().map((r) => r.event), ["ready"]);
});

test("error and empty completion do not invent first useful text", (t) => {
  const records = [];
  t.mock.method(console, "log", (...args) => records.push(args));
  const empty = channel(t);
  empty.stream.ready(lifecycle);
  empty.stream.done({ response: "" });
  const failed = channel(t);
  failed.stream.error("INVOCATION_FAILED");
  assert.equal(failed.records()[0].code, "INVOCATION_FAILED");
  assert.equal(records.length, 0);
});
