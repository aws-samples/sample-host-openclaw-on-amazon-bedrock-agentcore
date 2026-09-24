/**
 * Small helpers shared by the Gateway Lambda tool targets.
 */
"use strict";

const TOOL_NAME_DELIMITER = "___";

/**
 * The Gateway exposes tools as `${target_name}___${tool_name}` and passes the
 * full name in clientContext.Custom.bedrockAgentCoreToolName. Return the bare
 * tool name.
 *   https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-tool-naming.html
 */
function toolNameFromContext(context) {
  const custom =
    (context && context.clientContext && context.clientContext.Custom) ||
    (context && context.client_context && context.client_context.custom) ||
    {};
  const full = custom.bedrockAgentCoreToolName || "";
  const idx = full.indexOf(TOOL_NAME_DELIMITER);
  return idx === -1 ? full : full.slice(idx + TOOL_NAME_DELIMITER.length);
}

/**
 * Sanitize a file name for use as an S3 key component. Mirrors
 * bridge/skills/s3-user-files/common.js so both tool surfaces address the
 * same objects.
 */
function sanitizeFilename(str) {
  if (typeof str !== "string" || !str) throw new Error("filename is required");
  let result = str;
  while (result.includes("..")) result = result.replace(/\.\./g, "");
  result = result.replace(/[^a-zA-Z0-9_\-.]/g, "_").slice(0, 256);
  if (!result || result.startsWith(".") || result.endsWith(".")) {
    throw new Error(`Invalid filename "${result}": leading/trailing dots not allowed`);
  }
  return result;
}

/** Wrap a handler so thrown errors become a JSON error object, not a Lambda fault. */
function withErrorEnvelope(fn) {
  return async (event, context) => {
    try {
      return await fn(event, context);
    } catch (err) {
      const code = err && err.name === "IdentityError" ? "unauthorized" : "error";
      return { error: code, message: err && err.message ? err.message : String(err) };
    }
  };
}

module.exports = { TOOL_NAME_DELIMITER, toolNameFromContext, sanitizeFilename, withErrorEnvelope };
