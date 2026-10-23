/**
 * AgentCore Gateway REQUEST interceptor.
 *
 * Runs after the Gateway's CUSTOM_JWT authorizer has validated the bearer
 * token and before the Gateway invokes the Lambda target. Configured with
 * `inputConfiguration.passRequestHeaders: true`, so the payload contains the
 * caller's `Authorization` header:
 *   https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.html
 *   https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-configuration.html
 *
 * For `tools/call` it rewrites `params.arguments.__caller_token` to the bearer
 * JWT, replacing anything the model sent under that key. That is the only way
 * the caller's verified identity reaches a Lambda target (the Lambda-target
 * contract has no claims field, and `Authorization` can never be allowlisted
 * for header propagation). The tool Lambdas re-verify the token themselves.
 *
 * Every other method (initialize, tools/list, ping, notifications, ...)
 * passes through, with any client-supplied `params.arguments.__caller_token`
 * removed so the reserved key only ever carries the interceptor's value.
 *
 * A JSON-RPC batch array gets the same treatment entry by entry. Any other
 * body shape (a string, number or empty batch) is answered with a JSON-RPC
 * "invalid request" error rather than forwarded unexamined.
 *
 * If no bearer is present and the request contains a `tools/call`, the
 * interceptor answers with a JSON-RPC error instead of forwarding a request
 * the tool could not attribute.
 */
"use strict";

const RESERVED_ARG = "__caller_token";

function getHeader(headers, name) {
  if (!headers) return undefined;
  const want = name.toLowerCase();
  for (const [k, v] of Object.entries(headers)) {
    if (k.toLowerCase() === want) return Array.isArray(v) ? v[0] : v;
  }
  return undefined;
}

function bearerFrom(headers) {
  const auth = getHeader(headers, "authorization");
  if (typeof auth !== "string") return null;
  const m = auth.match(/^Bearer\s+(\S+)$/i);
  return m ? m[1] : null;
}

function isPlainObject(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

function passThrough(body) {
  return { interceptorOutputVersion: "1.0", mcp: { transformedGatewayRequest: { body } } };
}

function errorResponse(body, statusCode, code, message) {
  const id = isPlainObject(body) && body.id !== undefined ? body.id : null;
  return {
    interceptorOutputVersion: "1.0",
    mcp: {
      transformedGatewayResponse: {
        statusCode,
        body: { jsonrpc: "2.0", id, error: { code, message } },
      },
    },
  };
}

function rpcError(body, message) {
  return errorResponse(body, 401, -32001, message);
}

function invalidRequest(body, message) {
  return errorResponse(body, 400, -32600, message);
}

/**
 * Rewrite one JSON-RPC request object. `tools/call` gets the bearer as
 * `__caller_token`; any other method has a client-supplied `__caller_token`
 * removed. Everything else in the request is left as it was.
 */
function rewriteRequest(req, token) {
  const params = isPlainObject(req.params) ? req.params : {};
  const args = isPlainObject(params.arguments) ? params.arguments : null;

  if (req.method === "tools/call") {
    // Overwrite unconditionally: a model-supplied value is never trusted.
    return { ...req, params: { ...params, arguments: { ...(args || {}), [RESERVED_ARG]: token } } };
  }
  if (args && Object.prototype.hasOwnProperty.call(args, RESERVED_ARG)) {
    const { [RESERVED_ARG]: _dropped, ...rest } = args;
    return { ...req, params: { ...params, arguments: rest } };
  }
  return req;
}

async function handler(event) {
  const mcp = (event && event.mcp) || {};
  // A RESPONSE interceptor payload carries gatewayResponse; we are only wired
  // as REQUEST, but pass the response through unchanged if ever invoked as one.
  if (mcp.gatewayResponse) {
    return {
      interceptorOutputVersion: "1.0",
      mcp: {
        transformedGatewayResponse: {
          body: mcp.gatewayResponse.body,
          statusCode: mcp.gatewayResponse.statusCode || 200,
        },
      },
    };
  }

  const request = mcp.gatewayRequest || {};
  const body = request.body;
  if (body === null || body === undefined) return passThrough(body);

  const isBatch = Array.isArray(body);
  if (!isBatch && !isPlainObject(body)) {
    return invalidRequest(body, "unsupported request body shape");
  }
  const entries = isBatch ? body : [body];
  if (entries.length === 0) return invalidRequest(body, "empty JSON-RPC batch");
  if (!entries.every(isPlainObject)) return invalidRequest(body, "invalid JSON-RPC batch entry");

  let token = null;
  if (entries.some((e) => e.method === "tools/call")) {
    token = bearerFrom(request.headers);
    if (!token) {
      return rpcError(body, "missing bearer token (interceptor passRequestHeaders must be true)");
    }
  }

  const next = entries.map((e) => rewriteRequest(e, token));
  return passThrough(isBatch ? next : next[0]);
}

module.exports = { handler, RESERVED_ARG, bearerFrom };
