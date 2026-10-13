/**
 * Worker-side bootstrap handshake for an empty SQLite receiver.
 *
 * Readiness reports empty-state counters and an environment hash. One bind
 * control copies the restored parent environment into the worker and binds
 * any prepared backend definitions to that generation. Ordinary requests
 * and the first backend import are rejected until binding, keeping bootstrap
 * control separate from the receiver's job protocol.
 */
import { assertBackendDefinitionBindReady, bindBackendDefinitions, backendDefinitionPreparationProof } from "./sqlite-backend-prepare.mjs";
// Separate bootstrap control protocol. No job IDs, databases, backend imports or SHARE_ENV.
import { createHash } from "node:crypto";
import { threadId } from "node:worker_threads";

export const CONTROL = "openclaw:sqlite-receiver:v1";
export const RECEIVER_ENV = "OPENCLAW_AGENTCORE_EMPTY_SQLITE_RECEIVER";
const forbidden = new Set(["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
  "AWS_SECURITY_TOKEN", "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
  "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
  "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "AWS_ROLE_SESSION_NAME",
  "AWS_SHARED_CREDENTIALS_FILE", "AWS_CREDENTIAL_FILE", "AWS_BEARER_TOKEN_BEDROCK"]);
export function environmentHash(environment) {
  return createHash("sha256").update(JSON.stringify(
    Object.keys(environment).sort().map(key => [key, environment[key]]))).digest("hex");
}
export function ordinaryEnvironment(environment) {
  const result = { ...environment };
  delete result[RECEIVER_ENV];
  delete result.OPENCLAW_AGENTCORE_PREINIT;
  return result;
}
export function validateEnvironment(environment, bound) {
  if (!environment || Object.getPrototypeOf(environment) !== Object.prototype) {
    throw new Error("Invalid SQLite receiver environment");
  }
  for (const key of Object.keys(environment)) {
    if (!key || key.includes("=") || key.includes("\0") || forbidden.has(key) ||
        typeof environment[key] !== "string" || environment[key].includes("\0")) {
      throw new Error("Invalid SQLite receiver environment entry");
    }
  }
  if (bound) {
    for (const key of ["USER_ID", "INTERNAL_USER_ID"]) {
      if (typeof environment[key] !== "string" || !environment[key].trim() ||
          /[\u0000-\u001f\u007f]/u.test(environment[key])) throw new Error("Missing SQLite receiver binding");
    }
  } else if (Object.hasOwn(environment, "USER_ID") || Object.hasOwn(environment, "INTERNAL_USER_ID")) {
    throw new Error("SQLite receiver seed is already bound");
  }
}

export function createReceiverControl(port, inspectEmpty) {
  const enabled = process.env[RECEIVER_ENV] === "1";
  let phase = enabled ? "unbound" : "ordinary";
  let generation = null, firstBackend = true;
  const assertEmpty = () => {
    const empty = inspectEmpty();
    if (!empty || Object.values(empty).some(value => value !== 0)) {
      throw new Error("SQLite receiver is not empty");
    }
    return empty;
  };
  return {
    start() {
      if (!enabled) return;
      assertBackendDefinitionBindReady();
      validateEnvironment(ordinaryEnvironment(process.env), false);
      const empty = assertEmpty();
      port.postMessage({ control: CONTROL, type: "ready", threadId, empty,
        environmentHash: environmentHash(ordinaryEnvironment(process.env)),
        ...(backendDefinitionPreparationProof().prepared ? { backendDefinitionsPrepared: true } : {}) });
    },
    receive(request) {
      if (!enabled) return false;
      if (request?.control !== CONTROL) {
        if (phase !== "bound") throw new Error("SQLite receiver has not admitted a user binding");
        return false;
      }
      if (phase !== "unbound" || request.type !== "bind" ||
          typeof request.controlId !== "string" || !/^[a-f0-9-]{36}$/.test(request.controlId) ||
          typeof request.generation !== "string" || !/^[a-zA-Z0-9_-]{1,128}$/.test(request.generation) ||
          Object.keys(request).some(key => !["control", "type", "controlId", "generation", "environment"].includes(key))) {
        throw new Error("Invalid SQLite receiver binding control");
      }
      const empty = assertEmpty();
      validateEnvironment(request.environment, true);
      assertBackendDefinitionBindReady(request.environment);
      // Copy the current parent environment once, after restore. Never share its mutable object.
      for (const key of Object.keys(process.env)) {
        if (!Object.hasOwn(request.environment, key)) delete process.env[key];
      }
      for (const [key, value] of Object.entries(request.environment)) process.env[key] = value;
      generation = request.generation;
      phase = "bound";
      bindBackendDefinitions(generation);
      port.postMessage({ control: CONTROL, type: "bound", controlId: request.controlId,
        generation, threadId, empty, environmentHash: environmentHash(process.env),
        ...(backendDefinitionPreparationProof().prepared ? { backendDefinitionsPrepared: true } : {}) });
      return true;
    },
    backendImport() {
      if (!enabled || !firstBackend) return;
      if (phase !== "bound") throw new Error("SQLite backend import preceded binding");
      firstBackend = false;
    },
  };
}
