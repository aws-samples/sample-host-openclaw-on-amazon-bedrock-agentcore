"use strict";

const UNSAFE_OBJECT_KEYS = new Set(["__proto__", "constructor", "prototype"]);

class InvocationActionError extends Error {
  constructor(message) {
    super(message);
    this.name = "InvocationActionError";
    this.code = "UNSUPPORTED_INVOCATION_ACTION";
  }
}

function cloneAndFreeze(value, depth = 0, seen = new Set()) {
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean" ||
    (typeof value === "number" && Number.isFinite(value))
  ) {
    return value;
  }
  if (depth > 32 || typeof value !== "object" || seen.has(value)) {
    throw new TypeError("Invocation request must be bounded acyclic JSON data");
  }
  const isArray = Array.isArray(value);
  if (!isArray && Object.getPrototypeOf(value) !== Object.prototype) {
    throw new TypeError("Invocation request contains an unsupported value");
  }
  seen.add(value);
  let clone;
  if (isArray) {
    if (value.length > 1_000) {
      throw new TypeError("Invocation request array is too large");
    }
    clone = value.map((entry) => cloneAndFreeze(entry, depth + 1, seen));
  } else {
    const keys = Object.keys(value);
    if (keys.length > 1_000) {
      throw new TypeError("Invocation request object is too large");
    }
    clone = {};
    for (const key of keys) {
      if (UNSAFE_OBJECT_KEYS.has(key)) {
        throw new TypeError("Invocation request contains an unsafe object key");
      }
      clone[key] = cloneAndFreeze(value[key], depth + 1, seen);
    }
  }
  seen.delete(value);
  return Object.freeze(clone);
}

function snapshotOptionalString(value, label) {
  if (value === undefined) return undefined;
  if (
    typeof value !== "string" ||
    !value.isWellFormed() ||
    value.length > 256
  ) {
    throw new TypeError(`${label} must be bounded text metadata`);
  }
  return value;
}

function snapshotRequest(payload) {
  const request = {};
  for (const key of ["message", "invocationId"]) {
    if (payload[key] !== undefined) {
      request[key] = cloneAndFreeze(payload[key]);
    }
  }
  return Object.freeze(request);
}

function createInvocationHandler({ sessionBinding, handlers = {} } = {}) {
  if (!sessionBinding || typeof sessionBinding.bindOrAssert !== "function") {
    throw new TypeError("sessionBinding with bindOrAssert is required");
  }

  return Object.freeze({
    handle(payload = {}) {
      // This call is deliberately the first stateful operation. It is a normal
      // synchronous call, so a warm runtime cannot enter any action handler for
      // a different user even while the original user's init promise is pending.
      const identity = sessionBinding.bindOrAssert({
        internalUserId: payload.internalUserId,
        namespace: payload.namespace,
      });

      const action = payload.action === undefined ? "status" : payload.action;
      const actionHandler = handlers[action];
      if (
        typeof action !== "string" ||
        !Object.hasOwn(handlers, action) ||
        typeof actionHandler !== "function"
      ) {
        throw new InvocationActionError(`Unsupported invocation action: ${action}`);
      }

      const actorId = snapshotOptionalString(payload.actorId, "actorId");
      const channel = snapshotOptionalString(payload.channel, "channel");
      const delivery = Object.freeze({
        ...(actorId === undefined ? {} : { actorId }),
        ...(channel === undefined ? {} : { channel }),
      });
      const request = snapshotRequest(payload);

      return actionHandler(Object.freeze({
        identity,
        delivery,
        request,
      }));
    },
  });
}

module.exports = {
  createInvocationHandler,
  InvocationActionError,
  cloneAndFreeze,
};
