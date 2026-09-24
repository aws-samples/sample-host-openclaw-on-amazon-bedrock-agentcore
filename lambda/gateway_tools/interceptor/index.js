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
 * Any other MCP method passes through unchanged. If no bearer is present the
 * interceptor answers the call with a JSON-RPC error instead of forwarding a
 * request the tool could not attribute.
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

function passThrough(body) {
  return { interceptorOutputVersion: "1.0", mcp: { transformedGatewayRequest: { body } } };
}

function rpcError(body, message) {
  const id = body && body.id !== undefined ? body.id : null;
  return {
    interceptorOutputVersion: "1.0",
    mcp: {
      transformedGatewayResponse: {
        statusCode: 401,
        body: { jsonrpc: "2.0", id, error: { code: -32001, message } },
      },
    },
  };
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
  if (!body || body.method !== "tools/call") return passThrough(body);

  const token = bearerFrom(request.headers);
  if (!token) {
    return rpcError(body, "missing bearer token (interceptor passRequestHeaders must be true)");
  }

  const params = { ...(body.params || {}) };
  const args = { ...(params.arguments || {}) };
  // Overwrite unconditionally: a model-supplied value is never trusted.
  args[RESERVED_ARG] = token;
  params.arguments = args;
  return passThrough({ ...body, params });
}

module.exports = { handler, RESERVED_ARG, bearerFrom };
