"use strict";

const VALID_INTERNAL_USER_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$/;

class SessionIdentityError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "SessionIdentityError";
    this.code = code;
  }
}

function canonicalNamespace(internalUserId) {
  if (
    typeof internalUserId !== "string" ||
    !internalUserId.isWellFormed() ||
    !VALID_INTERNAL_USER_ID.test(internalUserId)
  ) {
    throw new SessionIdentityError(
      "INVALID_SESSION_IDENTITY",
      "internalUserId must be a canonical internal identifier",
    );
  }
  return internalUserId;
}

class SessionBinding {
  #bound = null;

  bindOrAssert(candidate = {}) {
    const internalUserId = candidate?.internalUserId;
    const namespace = candidate?.namespace;

    if (this.#bound !== null) {
      if (
        this.#bound.internalUserId === internalUserId &&
        this.#bound.namespace === namespace
      ) {
        return this.#bound;
      }
      throw new SessionIdentityError(
        "SESSION_IDENTITY_MISMATCH",
        "runtime session is already bound to a different identity",
      );
    }

    const canonical = canonicalNamespace(internalUserId);
    canonicalNamespace(namespace);
    if (namespace !== canonical) {
      throw new SessionIdentityError(
        "SESSION_IDENTITY_MISMATCH",
        "namespace must exactly equal internalUserId",
      );
    }

    this.#bound = Object.freeze({
      internalUserId: canonical,
      namespace: canonical,
    });
    return this.#bound;
  }

  current() {
    return this.#bound;
  }
}

module.exports = {
  VALID_INTERNAL_USER_ID,
  SessionBinding,
  SessionIdentityError,
  canonicalNamespace,
};
