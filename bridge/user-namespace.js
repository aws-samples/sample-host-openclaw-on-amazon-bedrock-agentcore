/**
 * The user's storage namespace: the S3 prefix of the user's files and the
 * stem of the user's secret, schedule, STS session and Cognito names.
 *
 * Two forms exist. The legacy namespace is the runtime actor with every ":"
 * replaced by "_" ("telegram:123" -> "telegram_123"). A stored namespace is
 * kept on the user's SESSION record and sent by the Router and the Cron Lambda
 * in every invocation payload; they use it only when its NS#<namespace> owner
 * record names the same user (lambda/shared/python/runtime_routing.py). It is
 * either the legacy value or "ns_" + 26 base32 characters.
 *
 * The contract pins the namespace of the first request at activation, next to
 * the user binding (runtime-lifecycle.js). Callers from before stored
 * namespaces send none; the legacy namespace is used then.
 */
"use strict";

const OPAQUE_NAMESPACE = /^ns_[a-z2-7]{26}$/;

function legacyNamespace(actorId) {
  return String(actorId).replace(/:/g, "_");
}

function isOpaqueNamespace(namespace) {
  return typeof namespace === "string" && OPAQUE_NAMESPACE.test(namespace);
}

/** True when namespace is a namespace the actor's user can have. */
function isNamespaceOf(namespace, actorId) {
  return typeof actorId === "string" && actorId.length > 0 && typeof namespace === "string" &&
    (namespace === legacyNamespace(actorId) || isOpaqueNamespace(namespace));
}

/**
 * The namespace of a request: the one it names, or the legacy namespace when
 * it names none. null when the named value is not a namespace of the actor.
 */
function requestNamespace(actorId, namespace) {
  if (typeof actorId !== "string" || !actorId) return null;
  if (namespace === undefined || namespace === null) return legacyNamespace(actorId);
  return isNamespaceOf(namespace, actorId) ? namespace : null;
}

/**
 * Cognito username of the user: the actor while the namespace is the legacy
 * one (so nothing changes for those users), the namespace once it is opaque.
 * The gateway tools resolve an opaque username through its NS# owner record.
 */
function cognitoUsername(actorId, namespace) {
  return isOpaqueNamespace(namespace) ? namespace : actorId;
}

module.exports = {
  OPAQUE_NAMESPACE, legacyNamespace, isOpaqueNamespace, isNamespaceOf, requestNamespace, cognitoUsername,
};
