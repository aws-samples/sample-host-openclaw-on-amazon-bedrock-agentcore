/**
 * Start the user's OpenClaw gateway after the Runtime V2 snapshot restore.
 *
 * Bootstrap starts no OpenClaw process. A process that is running when the
 * snapshot is taken resumes with the same random-number state in every
 * restored microVM (Node's randomUUID cache, OpenSSL's DRBG), so a gateway
 * started before the snapshot would give every user the same device key and
 * the same boot, session and event IDs. See docs/runtime-v2.md.
 *
 * At binding the prepared gateway is spawned (openclaw-worker-owner.js) while
 * the binding is rechecked before and after the spawn; activation happens
 * only after the caller has restored the user's state. Stop is terminal, also
 * while a spawn is in progress.
 */

function createGatewayActivation({ createCold, env = process.env }) {
  let phase = "new", cold, stopped = false;
  const checkRunning = () => {
    if (stopped) {
      throw Object.assign(new Error("Gateway activation was stopped"), { code: "GATEWAY_ACTIVATION_STOPPED" });
    }
  };
  // Phase telemetry must never interrupt activation.
  const report = (onPhase, name) => { try { onPhase?.(name); } catch {} };

  async function stop() {
    // Terminal even while a spawn is still pending.
    stopped = true;
    phase = "stopped";
    if (cold) return cold.stop();
  }

  async function prepare() {
    checkRunning();
    if (phase !== "new") throw new Error("Gateway preparation was already attempted");
    // Start nothing before the snapshot: the gateway is spawned per user after restore.
    phase = "deferred";
    return { status: "deferred" };
  }

  async function restoreForBinding({ lifecycle, scope, namespace, onPhase }) {
    scope.checkpoint();
    checkRunning();
    if (cold) return { status: "cold" };
    if (phase !== "deferred") throw new Error("Gateway is not prepared for binding");
    const current = lifecycle.snapshot();
    const binding = current.binding;
    if (!binding || current.generation !== scope.generation) throw new Error("Gateway binding mismatch");
    const check = () => {
      scope.checkpoint();
      checkRunning();
      const now = lifecycle.snapshot();
      if (now.state !== "BINDING" || now.binding !== binding ||
          now.generation !== scope.generation || !now.resources.includes("credentials") ||
          namespace !== now.namespace ||
          env.USER_ID !== binding.actorId || env.INTERNAL_USER_ID !== binding.userId) {
        throw new Error("Gateway binding changed");
      }
      lifecycle.assertBinding(binding);
    };
    try {
      check();
      report(onPhase, "cold_prepare_start");
      cold = createCold();
      await cold.prepare();
      checkRunning();
      report(onPhase, "cold_prepare_ack");
      check();
      phase = "cold";
      return { status: "cold" };
    } catch (error) {
      try { await stop(); } catch {}
      throw error;
    }
  }

  async function activate(input) {
    checkRunning();
    if (!cold || phase !== "cold") throw new Error("Gateway must be prepared before activation");
    return cold.activate(input);
  }

  return {
    prepare, restoreForBinding, activate, stop,
    get child() { return cold?.child; },
  };
}

module.exports = { createGatewayActivation };
