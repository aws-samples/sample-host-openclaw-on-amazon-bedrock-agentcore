"use strict";

const crypto = require("node:crypto");
const http = require("node:http");
const {
  CAPABILITY_TOOL_NAMES,
  GATEWAY_OPERATION_REGISTRY,
} = require("./capability-catalog");

const CALL_SCHEMA = "personal-operator.capability-call.v1";
const GRANT_SCHEMA = "personal-operator.turn-capability-grant.v1";
const RESULT_SCHEMA = "personal-operator.capability-result.v1";
const RELAY_SCHEMA = "personal-operator.capability-relay-envelope.v1";
const RELAY_PATH = "/capabilities/call";
const MAX_RELAY_BODY_BYTES = 1024 * 1024;
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "::1"]);
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const RELEASE_PATTERN = /^[0-9a-f]{40}$/;
const OPAQUE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$/;
const USER_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$/;
const PACK_ID_PATTERN = /^[a-z0-9]+(?:[.-][a-z0-9-]+){1,7}$/;
const OPERATION_ID_PATTERN = /^[a-z0-9]+(?:[.-][a-z0-9-]+){1,7}$/;
const RUNTIME_ARN_PATTERN =
  /^arn:aws:bedrock-agentcore:eu-west-1:[0-9]{12}:runtime\/[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const RESULT_FIELDS = Object.freeze([
  "schema",
  "callId",
  "invocationId",
  "toolUseId",
  "catalogDigest",
  "operationId",
  "toolName",
  "argsHash",
  "status",
  "data",
  "provenanceRefs",
  "proposalRef",
  "receiptRef",
  "errorCode",
  "retryPolicy",
]);
const GRANT_FIELDS = Object.freeze([
  "schema",
  "sub",
  "sessionId",
  "runtimeArn",
  "runtimeQualifier",
  "invocationId",
  "releaseCommit",
  "catalogDigest",
  "allowedPackIds",
  "allowedOperationIds",
  "targetGrantHashes",
  "iat",
  "exp",
  "maxCalls",
  "nonce",
]);

class CapabilityRelayError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "CapabilityRelayError";
    this.code = code;
  }
}

function fail(code, message) {
  throw new CapabilityRelayError(code, message);
}

function isPlainObject(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype
  );
}

function assertWellFormed(value) {
  if (typeof value === "string" && !value.isWellFormed()) {
    throw new TypeError("canonical JSON contains malformed Unicode");
  }
}

function canonicalJson(value, active = new Set(), depth = 0) {
  if (depth > 16) throw new TypeError("canonical JSON exceeds the depth limit");
  if (value === null || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "string") {
    assertWellFormed(value);
    return JSON.stringify(value);
  }
  if (typeof value === "number" && Number.isSafeInteger(value)) {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    if (value.length > 4096 || active.has(value)) {
      throw new TypeError("canonical JSON array is cyclic or oversized");
    }
    active.add(value);
    try {
      return `[${value
        .map((entry) => canonicalJson(entry, active, depth + 1))
        .join(",")}]`;
    } finally {
      active.delete(value);
    }
  }
  if (isPlainObject(value)) {
    const keys = Object.keys(value);
    if (keys.length > 4096 || active.has(value)) {
      throw new TypeError("canonical JSON object is cyclic or oversized");
    }
    active.add(value);
    try {
      return `{${keys
        .sort()
        .map((key) => {
          assertWellFormed(key);
          return `${JSON.stringify(key)}:${canonicalJson(value[key], active, depth + 1)}`;
        })
        .join(",")}}`;
    } finally {
      active.delete(value);
    }
  }
  throw new TypeError("value is not canonical JSON data");
}

function canonicalJsonBytes(value, maximum = MAX_RELAY_BODY_BYTES) {
  const bytes = Buffer.from(canonicalJson(value), "utf8");
  if (bytes.length > maximum) {
    throw new TypeError("canonical JSON exceeds its byte limit");
  }
  return bytes;
}

function cloneCanonical(value) {
  return JSON.parse(canonicalJson(value));
}

function deepFreeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    for (const nested of Object.values(value)) deepFreeze(nested);
    Object.freeze(value);
  }
  return value;
}

function sameCanonical(left, right) {
  try {
    return canonicalJson(left) === canonicalJson(right);
  } catch {
    return false;
  }
}

function exactFields(value, fields) {
  return (
    isPlainObject(value) &&
    Object.keys(value).length === fields.length &&
    fields.every((field) => Object.hasOwn(value, field))
  );
}

function validString(value, pattern, maximum = 1024) {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= maximum &&
    value.isWellFormed() &&
    !value.includes("\0") &&
    (!pattern || pattern.test(value))
  );
}

function sortedUniqueStrings(value, pattern) {
  return (
    Array.isArray(value) &&
    value.every((entry) => validString(entry, pattern)) &&
    value.every((entry, index) => index === 0 || value[index - 1] < entry)
  );
}

function validateGrant(input) {
  let value;
  try {
    value = cloneCanonical(input);
  } catch {
    fail("CAPABILITY_GRANT_INVALID", "Turn capability grant is not canonical JSON");
  }
  const valid =
    exactFields(value, GRANT_FIELDS) &&
    value.schema === GRANT_SCHEMA &&
    validString(value.sub, USER_ID_PATTERN, 64) &&
    validString(value.sessionId, OPAQUE_ID_PATTERN, 128) &&
    validString(value.runtimeArn, RUNTIME_ARN_PATTERN, 256) &&
    validString(value.invocationId, OPAQUE_ID_PATTERN, 128) &&
    validString(value.nonce, OPAQUE_ID_PATTERN, 128) &&
    validString(value.releaseCommit, RELEASE_PATTERN, 40) &&
    value.runtimeQualifier === `release_${value.releaseCommit}` &&
    validString(value.catalogDigest, SHA256_PATTERN, 64) &&
    sortedUniqueStrings(value.allowedPackIds, PACK_ID_PATTERN) &&
    value.allowedPackIds.length > 0 &&
    sortedUniqueStrings(value.allowedOperationIds, OPERATION_ID_PATTERN) &&
    value.allowedOperationIds.length > 0 &&
    sortedUniqueStrings(value.targetGrantHashes, SHA256_PATTERN) &&
    Number.isSafeInteger(value.iat) &&
    value.iat >= 0 &&
    Number.isSafeInteger(value.exp) &&
    value.exp > value.iat &&
    value.exp - value.iat <= 900 &&
    Number.isSafeInteger(value.maxCalls) &&
    value.maxCalls >= 1 &&
    value.maxCalls <= 64;
  if (!valid) {
    fail("CAPABILITY_GRANT_INVALID", "Turn capability grant is invalid");
  }
  return deepFreeze(value);
}

function schemaAccepts(schema, value) {
  if (schema.oneOf) {
    let matches = 0;
    for (const branch of schema.oneOf) {
      try {
        validateSchema(branch, value);
        matches += 1;
      } catch {
        // A union is valid only when exactly one branch accepts the value.
      }
    }
    if (matches !== 1) throw new TypeError("value does not match one exact branch");
    return;
  }
  if (Object.hasOwn(schema, "const") && !sameCanonical(schema.const, value)) {
    throw new TypeError("value differs from schema const");
  }
  if (
    Object.hasOwn(schema, "enum") &&
    !schema.enum.some((entry) => sameCanonical(entry, value))
  ) {
    throw new TypeError("value is outside schema enum");
  }
  if (schema.type) {
    const typeMatches = {
      null: value === null,
      boolean: typeof value === "boolean",
      integer: Number.isSafeInteger(value),
      string: typeof value === "string" && value.isWellFormed(),
      array: Array.isArray(value),
      object: isPlainObject(value),
    }[schema.type];
    if (!typeMatches) throw new TypeError("value has the wrong schema type");
  }
}

function validateSchema(schema, value) {
  schemaAccepts(schema, value);
  if (schema.oneOf || Object.hasOwn(schema, "const") || schema.enum) return;
  if (schema.type === "object") {
    const properties = schema.properties || {};
    const required = schema.required || [];
    if (required.some((name) => !Object.hasOwn(value, name))) {
      throw new TypeError("object lacks a required property");
    }
    if (
      schema.additionalProperties === false &&
      Object.keys(value).some((name) => !Object.hasOwn(properties, name))
    ) {
      throw new TypeError("object contains an additional property");
    }
    for (const [name, nested] of Object.entries(value)) {
      if (properties[name]) validateSchema(properties[name], nested);
    }
  } else if (schema.type === "array") {
    if (schema.minItems !== undefined && value.length < schema.minItems) {
      throw new TypeError("array is too short");
    }
    if (schema.maxItems !== undefined && value.length > schema.maxItems) {
      throw new TypeError("array is too long");
    }
    if (
      schema.uniqueItems === true &&
      new Set(value.map((entry) => canonicalJson(entry))).size !== value.length
    ) {
      throw new TypeError("array items are not unique");
    }
    for (const nested of value) validateSchema(schema.items, nested);
  } else if (schema.type === "string") {
    const length = Array.from(value).length;
    if (schema.minLength !== undefined && length < schema.minLength) {
      throw new TypeError("string is too short");
    }
    if (schema.maxLength !== undefined && length > schema.maxLength) {
      throw new TypeError("string is too long");
    }
    if (schema.pattern && !new RegExp(schema.pattern, "u").test(value)) {
      throw new TypeError("string does not match the schema pattern");
    }
  } else if (schema.type === "integer") {
    if (schema.minimum !== undefined && value < schema.minimum) {
      throw new TypeError("integer is below the minimum");
    }
    if (schema.maximum !== undefined && value > schema.maximum) {
      throw new TypeError("integer is above the maximum");
    }
  }
}

function sha256(bytes) {
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

function deriveCallId(call) {
  const digest = crypto
    .createHash("sha256")
    .update("personal-operator.capability-call.v1\0", "utf8");
  for (const value of [
    call.invocationId,
    call.toolUseId,
    call.catalogDigest,
    call.operationId,
    call.toolName,
    call.argsHash,
  ]) {
    digest.update(value, "utf8");
    digest.update("\0", "utf8");
  }
  return `call_${digest.digest("hex")}`;
}

function operationForTool(toolName) {
  return Object.values(GATEWAY_OPERATION_REGISTRY).find(
    (operation) => operation.toolName === toolName,
  );
}

function buildCall(grant, toolUseId, toolName, args) {
  if (!validString(toolUseId, OPAQUE_ID_PATTERN, 128)) {
    fail("CAPABILITY_TOOL_USE_INVALID", "Tool-use identity is invalid");
  }
  const operation = operationForTool(toolName);
  if (!operation) {
    fail("CAPABILITY_TOOL_UNKNOWN", "Tool is absent from the frozen catalog");
  }
  if (
    !grant.allowedPackIds.includes(operation.packId) ||
    !grant.allowedOperationIds.includes(operation.operationId)
  ) {
    fail("CAPABILITY_NOT_GRANTED", "Tool is absent from the current turn grant");
  }
  let argumentsCopy;
  try {
    argumentsCopy = cloneCanonical(args);
    validateSchema(operation.inputSchema, argumentsCopy);
    canonicalJsonBytes(argumentsCopy, operation.quotaPolicy.maxInputBytes);
  } catch {
    fail("CAPABILITY_ARGUMENTS_INVALID", "Tool arguments fail the frozen schema");
  }
  const argsHash = sha256(canonicalJsonBytes(argumentsCopy));
  const call = {
    schema: CALL_SCHEMA,
    callId: "",
    invocationId: grant.invocationId,
    toolUseId,
    catalogDigest: grant.catalogDigest,
    operationId: operation.operationId,
    toolName: operation.toolName,
    arguments: argumentsCopy,
    argsHash,
  };
  call.callId = deriveCallId(call);
  return { call: deepFreeze(call), operation };
}

function boundedOptionalString(value, maximum = 1024) {
  return value === null || validString(value, null, maximum);
}

function validateStringRefs(value) {
  return (
    Array.isArray(value) &&
    value.length <= 32 &&
    value.every((entry) => validString(entry, null, 1024)) &&
    value.every((entry, index) => index === 0 || value[index - 1] < entry)
  );
}

function validateResult(input, call, operation, sensitiveValues) {
  let result;
  try {
    result = cloneCanonical(input);
  } catch {
    fail("CAPABILITY_RESULT_INVALID", "Gateway result is not canonical JSON");
  }
  const identityFields = [
    "callId",
    "invocationId",
    "toolUseId",
    "catalogDigest",
    "operationId",
    "toolName",
    "argsHash",
  ];
  const statuses = new Set([
    "SUCCEEDED",
    "PENDING_APPROVAL",
    "DENIED",
    "FAILED_RETRYABLE",
    "UNCERTAIN",
  ]);
  const validBase =
    exactFields(result, RESULT_FIELDS) &&
    result.schema === RESULT_SCHEMA &&
    identityFields.every((field) => result[field] === call[field]) &&
    statuses.has(result.status) &&
    isPlainObject(result.data) &&
    validateStringRefs(result.provenanceRefs) &&
    boundedOptionalString(result.proposalRef) &&
    boundedOptionalString(result.receiptRef) &&
    boundedOptionalString(result.errorCode, 128) &&
    ["NONE", "SAFE_RETRY", "RECONCILE_ONLY"].includes(result.retryPolicy);
  if (!validBase) {
    fail("CAPABILITY_RESULT_INVALID", "Gateway result violates its exact contract");
  }
  const requiredRetry = {
    FAILED_RETRYABLE: "SAFE_RETRY",
    UNCERTAIN: "RECONCILE_ONLY",
  }[result.status] || "NONE";
  if (result.retryPolicy !== requiredRetry) {
    fail("CAPABILITY_RESULT_INVALID", "Gateway result retry policy is inconsistent");
  }
  const proposalOperation =
    operation.approvalPolicy.mode === "EXACT_ONE_TIME_PROPOSAL";
  try {
    if (result.status === "SUCCEEDED") {
      if (
        proposalOperation ||
        result.proposalRef !== null ||
        result.errorCode !== null
      ) {
        throw new TypeError("successful result fields are inconsistent");
      }
      validateSchema(operation.outputSchema, result.data);
    } else if (result.status === "PENDING_APPROVAL") {
      validateSchema(operation.outputSchema, result.data);
      if (
        !proposalOperation ||
        result.proposalRef === null ||
        result.proposalRef !== result.data.proposalRef ||
        result.data.argsHash !== result.argsHash ||
        result.receiptRef !== null ||
        result.errorCode !== null ||
        result.provenanceRefs.length !== 0
      ) {
        throw new TypeError("pending result fields are inconsistent");
      }
    } else if (
      Object.keys(result.data).length !== 0 ||
      result.provenanceRefs.length !== 0 ||
      result.proposalRef !== null ||
      result.receiptRef !== null ||
      result.errorCode === null ||
      (result.status === "UNCERTAIN" && operation.retryPolicy.mode === "READ_ONLY")
    ) {
      throw new TypeError("failure result fields are inconsistent");
    }
    canonicalJsonBytes(result.data, operation.quotaPolicy.maxOutputBytes);
  } catch {
    fail("CAPABILITY_RESULT_INVALID", "Gateway result fails the frozen output contract");
  }
  const serialized = canonicalJson(result);
  if (sensitiveValues.some((secret) => serialized.includes(secret))) {
    fail("CAPABILITY_RESULT_SENSITIVE", "Gateway result contains relay authority");
  }
  return deepFreeze(result);
}

function emitSafe(logger, event) {
  try {
    logger(Object.freeze(event));
  } catch {
    // Diagnostics are non-authoritative and cannot affect admission or dispatch.
  }
}

class CapabilityRelay {
  #grant = null;
  #calls = new Map();
  #gatewayTransport;
  #inflight = 0;
  #logger;
  #maxInflight;
  #now;
  #sensitiveValues = [];

  constructor({
    gatewayTransport,
    now = () => Math.floor(Date.now() / 1000),
    logger = () => {},
    maxInflight = 4,
  } = {}) {
    if (typeof gatewayTransport !== "function") {
      throw new TypeError("Capability relay requires a gateway transport");
    }
    if (
      typeof now !== "function" ||
      typeof logger !== "function" ||
      !Number.isSafeInteger(maxInflight) ||
      maxInflight < 1 ||
      maxInflight > 16
    ) {
      throw new TypeError("Capability relay configuration is invalid");
    }
    this.#gatewayTransport = gatewayTransport;
    this.#now = now;
    this.#logger = logger;
    this.#maxInflight = maxInflight;
  }

  bind_turn(grant) {
    if (this.#inflight !== 0) {
      fail("CAPABILITY_TURN_BUSY", "Cannot replace an active capability turn");
    }
    this.#grant = validateGrant(grant);
    this.#calls = new Map();
    this.#sensitiveValues = [
      this.#grant.nonce,
      ...this.#grant.targetGrantHashes,
    ];
  }

  clear_turn() {
    if (this.#inflight !== 0) {
      fail("CAPABILITY_TURN_BUSY", "Cannot clear an active capability turn");
    }
    this.#grant = null;
    this.#calls = new Map();
    this.#sensitiveValues = [];
  }

  async call(toolUseId, toolName, args) {
    const grant = this.#grant;
    if (!grant) fail("CAPABILITY_GRANT_REQUIRED", "No capability turn is bound");
    const now = this.#now();
    if (!Number.isSafeInteger(now) || now < grant.iat) {
      fail("CAPABILITY_GRANT_NOT_YET_VALID", "Capability grant is not yet valid");
    }
    if (now >= grant.exp) {
      fail("CAPABILITY_GRANT_EXPIRED", "Capability grant has expired");
    }
    const { call, operation } = buildCall(grant, toolUseId, toolName, args);
    const existing = this.#calls.get(toolUseId);
    if (existing) {
      if (existing.call.argsHash !== call.argsHash || existing.call.toolName !== toolName) {
        fail(
          "CAPABILITY_ARGUMENT_MUTATION",
          "One tool-use identity cannot be rebound to different arguments",
        );
      }
      return existing.promise;
    }
    if (this.#calls.size >= grant.maxCalls) {
      fail(
        "CAPABILITY_CALL_BUDGET_EXCEEDED",
        "Turn capability call budget is exhausted",
      );
    }
    if (this.#inflight >= this.#maxInflight) {
      fail("CAPABILITY_RELAY_BUSY", "Capability relay concurrency is exhausted");
    }
    const envelope = deepFreeze({
      schema: RELAY_SCHEMA,
      grant,
      call,
    });
    const entry = { call, promise: null };
    const promise = (async () => {
      this.#inflight += 1;
      emitSafe(this.#logger, {
        event: "capability_call_started",
        callId: call.callId,
        operationId: call.operationId,
      });
      try {
        let rawResult;
        try {
          rawResult = await this.#gatewayTransport(envelope);
        } catch (error) {
          if (error instanceof CapabilityRelayError) throw error;
          fail(
            "CAPABILITY_GATEWAY_UNAVAILABLE",
            "Capability gateway transport is unavailable",
          );
        }
        const result = validateResult(
          rawResult,
          call,
          operation,
          this.#sensitiveValues,
        );
        emitSafe(this.#logger, {
          event: "capability_call_finished",
          callId: call.callId,
          status: result.status,
        });
        return result;
      } finally {
        this.#inflight -= 1;
      }
    })();
    entry.promise = promise;
    this.#calls.set(toolUseId, entry);
    return promise;
  }
}

function createCapabilityAdapters({ relay, client } = {}) {
  const target = relay || client;
  if (!target || typeof target.call !== "function") {
    throw new TypeError("Capability adapters require an exact relay client");
  }
  const adapters = {};
  for (const toolName of CAPABILITY_TOOL_NAMES) {
    adapters[toolName] = (toolUseId, args) =>
      target.call(toolUseId, toolName, args);
  }
  return deepFreeze(adapters);
}

function assertLoopback(host) {
  if (!LOOPBACK_HOSTS.has(host)) {
    throw new TypeError("Capability relay must use an exact loopback address");
  }
}

function parsePort(port) {
  if (!Number.isSafeInteger(port) || port < 0 || port > 65535) {
    throw new TypeError("Capability relay port is invalid");
  }
  return port;
}

function createCapabilityRelayServer({
  relay,
  host = "127.0.0.1",
  port = 18791,
  maxBodyBytes = MAX_RELAY_BODY_BYTES,
} = {}) {
  assertLoopback(host);
  parsePort(port);
  if (!relay || typeof relay.call !== "function") {
    throw new TypeError("Capability relay server requires a relay");
  }
  if (!Number.isSafeInteger(maxBodyBytes) || maxBodyBytes < 1) {
    throw new TypeError("Capability relay body limit is invalid");
  }
  let listening = false;
  const server = http.createServer((request, response) => {
    if (request.method !== "POST" || request.url !== RELAY_PATH) {
      response.writeHead(404, { "Content-Type": "application/json" });
      response.end('{"errorCode":"NOT_FOUND"}');
      return;
    }
    let size = 0;
    const chunks = [];
    let rejected = false;
    request.on("data", (chunk) => {
      size += chunk.length;
      if (size > maxBodyBytes) {
        rejected = true;
        response.writeHead(413, { "Content-Type": "application/json" });
        response.end('{"errorCode":"CAPABILITY_REQUEST_TOO_LARGE"}');
        request.destroy();
        return;
      }
      chunks.push(chunk);
    });
    request.on("end", async () => {
      if (rejected) return;
      try {
        const payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        if (
          !exactFields(payload, ["toolUseId", "toolName", "arguments"])
        ) {
          fail("CAPABILITY_REQUEST_INVALID", "Relay request fields are invalid");
        }
        const result = await relay.call(
          payload.toolUseId,
          payload.toolName,
          payload.arguments,
        );
        const body = canonicalJsonBytes(result, maxBodyBytes);
        response.writeHead(200, {
          "Content-Type": "application/json",
          "Content-Length": body.length,
        });
        response.end(body);
      } catch (error) {
        const body = canonicalJsonBytes({
          errorCode:
            typeof error?.code === "string"
              ? error.code.slice(0, 128)
              : "CAPABILITY_RELAY_FAILED",
        });
        response.writeHead(400, {
          "Content-Type": "application/json",
          "Content-Length": body.length,
        });
        response.end(body);
      }
    });
  });
  return Object.freeze({
    listen() {
      if (listening) return Promise.resolve();
      return new Promise((resolve, reject) => {
        server.once("error", reject);
        server.listen(port, host, () => {
          listening = true;
          server.off("error", reject);
          resolve();
        });
      });
    },
    address() {
      return server.address();
    },
    close() {
      if (!listening) return Promise.resolve();
      return new Promise((resolve, reject) => {
        server.close((error) => {
          if (error) reject(error);
          else {
            listening = false;
            resolve();
          }
        });
      });
    },
  });
}

function createLoopbackRelayClient({
  host = "127.0.0.1",
  port = 18791,
  timeoutMs = 30_000,
  maxBodyBytes = MAX_RELAY_BODY_BYTES,
} = {}) {
  assertLoopback(host);
  if (parsePort(port) === 0) throw new TypeError("Relay client requires a bound port");
  if (
    !Number.isSafeInteger(maxBodyBytes) ||
    maxBodyBytes < 1 ||
    maxBodyBytes > 16 * 1024 * 1024
  ) {
    throw new TypeError("Relay client body limit is invalid");
  }
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 120_000) {
    throw new TypeError("Relay client timeout is invalid");
  }
  return Object.freeze({
    call(toolUseId, toolName, args) {
      let body;
      try {
        body = canonicalJsonBytes(
          { toolUseId, toolName, arguments: args },
          maxBodyBytes,
        );
      } catch {
        return Promise.reject(
          new CapabilityRelayError(
            "CAPABILITY_REQUEST_INVALID",
            "Relay request cannot be encoded",
          ),
        );
      }
      return new Promise((resolve, reject) => {
        const request = http.request(
          {
            host,
            port,
            path: RELAY_PATH,
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Content-Length": body.length,
            },
          },
          (response) => {
            let size = 0;
            const chunks = [];
            response.on("data", (chunk) => {
              size += chunk.length;
              if (size > maxBodyBytes) {
                request.destroy();
                reject(
                  new CapabilityRelayError(
                    "CAPABILITY_RESPONSE_TOO_LARGE",
                    "Relay response exceeds its byte limit",
                  ),
                );
                return;
              }
              chunks.push(chunk);
            });
            response.on("end", () => {
              try {
                const parsed = JSON.parse(Buffer.concat(chunks).toString("utf8"));
                if (response.statusCode !== 200) {
                  throw new CapabilityRelayError(
                    validString(parsed?.errorCode, null, 128)
                      ? parsed.errorCode
                      : "CAPABILITY_RELAY_FAILED",
                    "Capability relay rejected the call",
                  );
                }
                resolve(parsed);
              } catch (error) {
                reject(
                  error instanceof CapabilityRelayError
                    ? error
                    : new CapabilityRelayError(
                        "CAPABILITY_RESPONSE_INVALID",
                        "Relay response is invalid",
                      ),
                );
              }
            });
          },
        );
        request.setTimeout(timeoutMs, () => {
          request.destroy(
            new CapabilityRelayError(
              "CAPABILITY_RELAY_TIMEOUT",
              "Capability relay timed out",
            ),
          );
        });
        request.on("error", (error) => {
          reject(
            error instanceof CapabilityRelayError
              ? error
              : new CapabilityRelayError(
                  "CAPABILITY_RELAY_UNAVAILABLE",
                  "Capability relay is unavailable",
                ),
          );
        });
        request.end(body);
      });
    },
  });
}

function createLambdaGatewayTransport({ functionArn, lambdaClient } = {}) {
  if (
    !validString(
      functionArn,
      /^arn:aws:lambda:eu-west-1:[0-9]{12}:function:[A-Za-z0-9-_]+$/,
      256,
    )
  ) {
    throw new TypeError("Capability gateway requires an exact eu-west-1 function ARN");
  }
  let client = lambdaClient;
  return async (envelope) => {
    if (!client) {
      const { LambdaClient } = require("@aws-sdk/client-lambda");
      client = new LambdaClient({ region: "eu-west-1" });
    }
    const { InvokeCommand } = require("@aws-sdk/client-lambda");
    const response = await client.send(
      new InvokeCommand({
        FunctionName: functionArn,
        InvocationType: "RequestResponse",
        Payload: canonicalJsonBytes(envelope),
      }),
    );
    if (response.FunctionError || !response.Payload) {
      fail("CAPABILITY_GATEWAY_FAILED", "Capability gateway invocation failed");
    }
    try {
      return JSON.parse(Buffer.from(response.Payload).toString("utf8"));
    } catch {
      fail("CAPABILITY_GATEWAY_FAILED", "Capability gateway response is invalid");
    }
  };
}

module.exports = {
  CapabilityRelay,
  CapabilityRelayError,
  createCapabilityAdapters,
  createCapabilityRelayServer,
  createLambdaGatewayTransport,
  createLoopbackRelayClient,
};
