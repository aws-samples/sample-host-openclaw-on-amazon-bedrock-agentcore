/**
 * Retain backend definitions in the existing anonymous SQLite receiver.
 *
 * Preparation captures the module namespace and factory references while
 * deferring tslog's default logger initialization. Import-time selectors must
 * match at binding, and deferred effects must be activated before callers
 * can consume the retained namespace. Loading definitions alone does not
 * authorize mutable backend effects.
 */
export const BACKEND_PREPARE_FLAG = "OPENCLAW_AGENTCORE_SQLITE_BACKEND_PREPARE";
export const BACKEND_MODULE_URL = "file:///usr/local/lib/node_modules/openclaw/dist/state/openclaw-state.worker.js";
let phase = "inactive";
let namespace;
let factoryReferences;
let generation;
let consumptions = 0;
let activated = false;
let selectorSeed;
// Pinned import audit: these selectors can become module constants before binding.
const SELECTOR_KEYS = Object.freeze(["HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH",
  "OPENCLAW_WORKSPACE_DIR", "OPENCLAW_NIX_MODE", "OPENCLAW_BUNDLED_VERSION", "NODE_DEBUG", "VITEST"]);
const deferred = new Map();

export function backendPreparationEnabled(environment = process.env) {
  const value = environment[BACKEND_PREPARE_FLAG] ?? "0";
  if (value !== "0" && value !== "1") throw new Error(`Invalid ${BACKEND_PREPARE_FLAG}`);
  if (value === "1" && environment.OPENCLAW_AGENTCORE_SQLITE_RECEIVER_PREPARE !== "1") {
    throw new Error("SQLite backend preparation requires the prepared receiver");
  }
  return value === "1";
}

export function beginBackendDefinitionPreparation() {
  if (!backendPreparationEnabled()) return false;
  if (process.env.OPENCLAW_AGENTCORE_EMPTY_SQLITE_RECEIVER !== "1" || phase !== "inactive") {
    throw new Error("SQLite backend preparation requires a fresh anonymous receiver");
  }
  selectorSeed = Object.fromEntries(SELECTOR_KEYS.map(key =>
    [key, { present: Object.hasOwn(process.env, key), value: process.env[key] }]));
  phase = "preparing";
  return true;
}

export function backendDefinitionsPreparing() {
  return phase === "preparing";
}

export function deferBackendBindingEffect(name, initialize) {
  if (phase !== "preparing" || name !== "tslog.defaultLogger" ||
      typeof initialize !== "function" || deferred.has(name)) {
    throw new Error("Unexpected SQLite backend binding effect");
  }
  deferred.set(name, initialize);
}

export function assertBackendEffectsBound() {
  if (phase !== "inactive" && phase !== "bound") {
    throw new Error("SQLite backend mutable effect preceded binding");
  }
}

export function completeBackendDefinitionPreparation(module) {
  if (phase !== "preparing" || namespace !== undefined || !module ||
      typeof module.createSqliteWorkerBackend !== "function" ||
      typeof module.openExistingSqliteWorkerBackend !== "function") {
    throw new Error("SQLite backend definitions did not complete preparation");
  }
  namespace = module;
  factoryReferences = Object.freeze([module.createSqliteWorkerBackend, module.openExistingSqliteWorkerBackend]);
  phase = "prepared";
}

export function assertBackendDefinitionBindReady(environment) {
  if (phase !== "inactive" && phase !== "prepared") {
    throw new Error("SQLite backend definitions are not ready for binding");
  }
  if (phase === "prepared" && environment) {
    for (const key of SELECTOR_KEYS) {
      const original = selectorSeed[key];
      if (Object.hasOwn(environment, key) !== original.present || environment[key] !== original.value) {
        throw new Error(`SQLite backend preparation selector changed: ${key}`);
      }
    }
  }
}

export function bindBackendDefinitions(boundGeneration) {
  if (phase === "inactive") return;
  assertBackendDefinitionBindReady();
  if (typeof boundGeneration !== "string" || !/^[a-zA-Z0-9_-]{1,128}$/.test(boundGeneration)) {
    throw new Error("Invalid SQLite backend binding generation");
  }
  if (!backendPreparationEnabled()) throw new Error("SQLite backend preparation flag changed at binding");
  generation = boundGeneration;
  phase = "bound";
}

export function activateBoundBackendEffects() {
  if (phase === "inactive" || activated) return;
  assertBackendEffectsBound();
  for (const initialize of deferred.values()) initialize();
  deferred.clear();
  activated = true;
}

export function consumePreparedBackend(moduleUrl) {
  if (phase === "inactive") return;
  assertBackendEffectsBound();
  if (moduleUrl !== BACKEND_MODULE_URL) return;
  if (!namespace || !activated) throw new Error("SQLite backend definitions have not been activated");
  if (namespace.createSqliteWorkerBackend !== factoryReferences[0] ||
      namespace.openExistingSqliteWorkerBackend !== factoryReferences[1]) {
    throw new Error("SQLite backend factory references changed after preparation");
  }
  consumptions++;

  return namespace;
}

export function backendDefinitionPreparationProof() {
  return {
    phase, prepared: namespace !== undefined, bound: phase === "bound",
    generation_bound: generation !== undefined, deferred_effects: deferred.size,
    activated, consumptions,
  };
}
