/**
 * Reserve one empty SQLite broker slot through restore and first acquisition.
 *
 * Arming records the restored actor, user and generation without binding the
 * anonymous worker. The first claim sends its current environment, verifies
 * the bound acknowledgement and empty slot, then transfers the slot to the
 * ordinary open path.
 *
 * A failed claim can return null only after retiring the unused reservation;
 * cancellation or changed identity still fails. Once consumed, the slot can
 * no longer be retired under empty-reservation ownership.
 */
import { backendPreparationEnabled } from "./sqlite-backend-prepare.mjs";
// Own one empty broker slot. Failure fallback is allowed only before its first real open.
import { randomUUID } from "node:crypto";
import { CONTROL, ordinaryEnvironment, environmentHash, validateEnvironment } from "./sqlite-receiver-control.mjs";

export function brokerEmpty(broker) {
  return !broker.actors.size && !broker.clients.size && !broker.stores.size &&
    !broker.operations.size && !broker.requests && !broker.bytes && !broker.admissionBytes;
}
function emptyProof(reply) {
  const keys = ["actors", "paths", "contexts", "fences", "pendingInput", "pendingResult",
    "sourceLoader", "operationAdmission", "lifecycle", "maintenanceFence"];
  return reply?.empty && Object.keys(reply.empty).length === keys.length &&
    keys.every(key => reply.empty[key] === 0);
}
export class ReceiverReservation {
  #arming = null;
  constructor(broker, environment) {
    validateEnvironment(ordinaryEnvironment(environment), false);
    this.broker = broker;
    this.backendExpected = backendPreparationEnabled(environment);
    this.phase = "preparing";
    this.seedHash = environmentHash(ordinaryEnvironment(environment));
    this.slot = null;
    this.pending = null;
  }
  get generation() { return this.#arming?.generation ?? null; }
  attach(slot) { this.slot = slot; }
  slotAvailable() {
    return !!this.slot && !this.slot.failed && !this.slot.exited &&
      !this.slot.retiring && this.slot.worker.threadId > 0;
  }
  slotEmpty() {
    return !!this.slot && !this.slot.actors.size && !this.slot.current &&
      !this.slot.queue.length && this.slot.pendingOpens === 0;
  }
  currentEnvironment(environment) {
    const current = ordinaryEnvironment(environment);
    validateEnvironment(current, true);
    if (current.USER_ID !== this.#arming?.actor ||
        current.INTERNAL_USER_ID !== this.#arming?.user) {
      throw new Error("SQLite receiver armed identity changed");
    }
    return current;
  }
  wait(type, timeoutMs, signal, send) {
    if (this.pending) throw new Error("SQLite receiver control is already pending");
    return new Promise((resolve, reject) => {
      let settled = false, timer;
      const finish = (error, reply) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
        this.pending = null;
        this.slot?.worker.unref();
        if (error) reject(error); else resolve(reply);
      };
      const abort = () => finish(new Error("SQLite receiver control canceled"));
      this.pending = { type, finish };
      timer = setTimeout(() => finish(new Error("SQLite receiver control timed out")), timeoutMs);
      signal?.addEventListener("abort", abort, { once: true });
      this.slot?.worker.ref();
      if (signal?.aborted) abort();
      else {
        try { send?.(); } catch (error) { finish(error); }
      }
    });
  }
  receive(reply) {
    if (reply?.control !== CONTROL) return false;
    const pending = this.pending;
    if (!pending || pending.type !== reply.type || !emptyProof(reply) ||
        (this.backendExpected ? reply.backendDefinitionsPrepared !== true : reply.backendDefinitionsPrepared !== undefined) ||
        reply.threadId !== this.slot.worker.threadId ||
        (reply.type === "ready" && reply.environmentHash !== this.seedHash) ||
        (reply.type === "bound" && (reply.controlId !== this.controlId ||
          reply.generation !== this.generation || reply.environmentHash !== this.boundHash))) {
      const error = new Error("SQLite receiver bootstrap acknowledgement mismatch");
      this.fail(error);
      this.broker.fail(this.slot, error);
      return true;
    }
    pending.finish(null, reply);
    return true;
  }
  fail(error) { this.pending?.finish(error); }
  async ready(timeoutMs, signal) {
    await this.wait("ready", timeoutMs, signal);
    this.phase = "ready";
    return this.proof();
  }
  async bind(generation, environment, timeoutMs = 1500, signal) {
    if (this.phase !== "ready" || typeof generation !== "string" ||
        !/^[a-zA-Z0-9_-]{1,128}$/.test(generation) ||
        !Number.isSafeInteger(timeoutMs) || timeoutMs <= 0) throw new Error("SQLite receiver binding is not available");
    const current = ordinaryEnvironment(environment);
    validateEnvironment(current, true);
    if (!this.slotAvailable() || !this.slotEmpty() || signal?.aborted) {
      await this.retire("arming_unavailable");
      throw new Error("SQLite receiver arming is not available or was canceled");
    }
    // Arming follows restore. The worker remains anonymous until the first admitted acquire.
    this.#arming = Object.freeze({ generation, actor: current.USER_ID, user: current.INTERNAL_USER_ID,
      timeoutMs, signal });
    this.phase = "armed";
    return this.proof();
  }
  async claim(environment) {
    if (this.phase !== "armed") throw new Error("SQLite receiver is not armed");
    let boundEnvironment;
    try {
      boundEnvironment = this.currentEnvironment(environment);
    } catch (error) {
      await this.retire("armed_identity_or_environment_changed");
      throw error;
    }
    if (!this.slotAvailable() || !this.slotEmpty()) {
      await this.retire("worker_or_empty_state_changed");
      if (this.#arming.signal?.aborted) throw new Error("SQLite receiver control canceled");
      this.currentEnvironment(environment);
      return null;
    }
    const { generation, timeoutMs, signal } = this.#arming;
    this.boundHash = environmentHash(boundEnvironment);
    this.controlId = randomUUID({ disableEntropyCache: true });
    this.phase = "binding";
    try {
      await this.wait("bound", timeoutMs, signal, () => this.slot.worker.postMessage({
        control: CONTROL, type: "bind", controlId: this.controlId, generation,
        environment: boundEnvironment,
      }));
    } catch (error) {
      // No actor or user operation can be sent while this slot is reserved.
      await this.retire("binding_failed");
      if (signal?.aborted) throw error;
      this.currentEnvironment(environment);
      return null;
    }
    let current;
    try {
      current = this.currentEnvironment(environment);
    } catch (error) {
      await this.retire("armed_identity_or_environment_changed");
      throw error;
    }
    const environmentChanged = environmentHash(current) !== this.boundHash;
    if (!this.slotAvailable() || !this.slotEmpty() || environmentChanged) {
      await this.retire("environment_or_empty_state_changed");
      if (signal?.aborted) throw new Error("SQLite receiver control canceled");
      this.currentEnvironment(environment);
      return null;
    }
    this.phase = "bound";
    this.phase = "consumed";
    this.slot.pendingOpens += 1;
    this.broker.agentCoreReceiver = undefined;
    return this.slot;
  }
  async retire(reason = "unused") {
    if (this.phase === "consumed") throw new Error("Cannot retire an adopted receiver as an empty reservation");
    if (this.retirement) return this.retirement;
    this.phase = "retiring";
    this.fail(new Error("SQLite receiver reservation retired"));
    this.retirement = (async () => {
      if (this.slot) {
        if (this.slot.actors.size || this.slot.current || this.slot.queue.length || this.slot.pendingOpens) {
          throw new Error("SQLite receiver lost empty reservation ownership");
        }
        await this.broker.retire(this.slot);
      }
      if (this.broker.agentCoreReceiver === this) this.broker.agentCoreReceiver = undefined;
      this.phase = "retired";
    })();
    return this.retirement;
  }
  proof() {
    const alive = this.slotAvailable();
    return { prepared: alive && ["ready", "armed", "bound"].includes(this.phase),
      armed: alive && this.phase === "armed", binding_deferred: this.phase === "armed",
      bound: alive && this.phase === "bound", thread_id: this.slot?.worker.threadId ?? null,
      phase: this.phase, generation: this.generation, pending_control: !!this.pending,
      empty: this.slotEmpty(),
      ...(this.backendExpected ? { backend_definitions_prepared: alive && ["ready", "armed", "bound"].includes(this.phase) } : {}) };
  }
}
