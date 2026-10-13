/**
 * Prepare the bound actor's proxy token through inherited parent/child IPC.
 *
 * Preparation carries an activation generation, never an actor supplied by
 * the message. The child accepts one generation and uses its existing actor
 * binding for token lookup. Concurrent callers join the preparation, then
 * re-enter the original expiry-aware getter even if warmup failed.
 *
 * Stop and disconnect close the binding and evict its cached token, including
 * tokens returned by a lookup that finishes after closure. Status messages
 * report preparation progress; they do not carry credentials.
 */
"use strict";

const PREPARE = "openclaw.proxy-auth-prepare.v1";
const STOP = "openclaw.proxy-auth-stop.v1";
const STATUS = "openclaw.proxy-auth-status.v1";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const STATUSES = new Set(["started", "warmed", "unavailable", "failed", "joined", "stopped"]);

function exactKeys(value, keys) {
  return value && typeof value === "object" && !Array.isArray(value) &&
    Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}

function validGeneration(value) {
  return typeof value === "string" && value.length === 36 && UUID.test(value);
}

function send(channel, message) {
  if (!channel.connected || typeof channel.send !== "function") return;
  try { channel.send(message, () => {}); } catch {}
}

function createProxyAuthParent({ child, generation, signal, isCurrent, onStatus = () => {} }) {
  let closed = false;
  const current = () => !closed && !signal.aborted && isCurrent();
  const receive = (message) => {
    if (!current() || !exactKeys(message, ["type", "generation", "status"]) ||
        message.type !== STATUS || message.generation !== generation || !STATUSES.has(message.status)) return;
    try { onStatus(message.status); } catch {}
  };
  const stop = () => {
    if (closed) return;
    closed = true;
    send(child, { type: STOP, generation });
    child.removeListener("message", receive);
    child.removeListener("exit", stop);
    child.removeListener("disconnect", stop);
    signal.removeEventListener("abort", stop);
  };
  if (!validGeneration(generation) || !current() || !child.connected || typeof child.send !== "function") {
    closed = true;
    return { stop };
  }
  child.on("message", receive);
  child.once("exit", stop);
  child.once("disconnect", stop);
  signal.addEventListener("abort", stop, { once: true });
  // Only this inherited parent/child descriptor authorizes preparation. No actor is sent.
  send(child, { type: PREPARE, generation });
  return { stop };
}

function createProxyAuthChild({ channel, tokenProvider, boundActor, emit = () => {},
                                now = Date.now, monotonic = () => process.hrtime.bigint() }) {
  let generation = null;
  let closed = false;
  let flight = null;
  const validOwner = typeof boundActor === "string" && boundActor.length > 0;
  const publish = (status, started = null) => {
    if (!generation || !STATUSES.has(status)) return;
    const stamp = monotonic();
    try {
      emit({ schema: PREPARE, event: "PROXY_AUTH_PREPARE", status,
        activation_generation: generation, wall_unix_ms: now(), monotonic_ns: stamp.toString(),
        elapsed_ms: started === null ? null : Number(stamp - started) / 1e6 });
    } catch {}
    send(channel, { type: STATUS, generation, status });
  };
  const stale = () => new Error("Proxy auth preparation binding closed");
  const lookup = () => {
    if (closed) return Promise.reject(stale());
    if (flight) return flight;
    const work = Promise.resolve().then(() => {
      if (closed) throw stale();
      return tokenProvider.getIdToken(boundActor);
    }).then((entry) => {
      if (closed) {
        tokenProvider._cache.delete(boundActor);
        throw stale();
      }
      return entry;
    });
    flight = work;
    const settled = () => {
      if (closed) tokenProvider._cache.delete(boundActor);
      if (flight === work) flight = null;
    };
    work.then(settled, settled);
    return work;
  };
  const stop = () => {
    if (closed) return;
    closed = true;
    if (validOwner) tokenProvider._cache.delete(boundActor);
    publish("stopped");
    channel.removeListener("message", receive);
    channel.removeListener("disconnect", stop);
    channel.removeListener("exit", stop);
  };
  const receive = (message) => {
    if (closed || !validOwner || !channel.connected ||
        !exactKeys(message, ["type", "generation"]) || !validGeneration(message.generation)) return;
    if (message.type === STOP && message.generation === generation) {
      stop();
      return;
    }
    if (message.type !== PREPARE || generation !== null) return;
    generation = message.generation; // A child accepts exactly one generation for its lifetime.
    const started = monotonic();
    publish("started");
    void lookup().then((entry) => {
      if (closed) return;
      publish(entry && typeof entry.token === "string" && entry.token &&
        Number.isFinite(entry.expiresAt) && entry.expiresAt > now() ? "warmed" : "unavailable", started);
    }, () => {
      if (!closed) publish("failed", started);
    });
  };
  const getIdToken = async (actor) => {
    if (closed) throw stale();
    if (!validOwner || actor !== boundActor) throw new Error("Proxy auth canonical actor mismatch");
    if (flight) {
      publish("joined");
      try { await flight; } catch (error) { if (closed) throw error; }
    }
    // Always re-enter the original expiry-aware getter, including after a failed warmup.
    return lookup();
  };
  if (channel.connected && typeof channel.send === "function" && validOwner) {
    channel.on("message", receive);
    channel.once("disconnect", stop);
    channel.once("exit", stop);
    channel.channel?.unref();
  }
  return { getIdToken, stop };
}

module.exports = { createProxyAuthParent, createProxyAuthChild };
