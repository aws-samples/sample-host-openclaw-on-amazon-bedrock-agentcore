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
  // The Node runtime hands the decoded X-Amz-Client-Context through as-is, so
  // the Gateway metadata lives under the lowercase `custom` key (the same key
  // Python exposes as client_context.custom). Accept `Custom` too for callers
  // and tests that use the capitalised spelling.
  const cc = (context && (context.clientContext || context.client_context)) || {};
  const custom = cc.custom || cc.Custom || {};
  const full = custom.bedrockAgentCoreToolName || "";
  const idx = full.indexOf(TOOL_NAME_DELIMITER);
  return idx === -1 ? full : full.slice(idx + TOOL_NAME_DELIMITER.length);
}

/**
 * Reserved "_system/" filename prefix; see
 * bridge/skills/s3-user-files/common.js SYSTEM_PREFIX. The proxy's sources
 * live at "_system/<namespace>/" at the bucket root, outside the user prefix;
 * this mirror keeps both tool surfaces refusing the same names.
 */
const SYSTEM_PREFIX = "_system/";

/**
 * Filenames the proxy injects verbatim into the Converse `system` block.
 * Blocking them here closes the second write surface identified in the
 * "User-writable S3 workspace files are injected into the Bedrock system
 * block" finding (CWE-501). Keep in sync with
 * bridge/skills/s3-user-files/common.js RESERVED_SYSTEM_FILENAMES.
 */
const RESERVED_SYSTEM_FILENAMES = new Set([
  "AGENTS.md",
  "SOUL.md",
  "USER.md",
  "IDENTITY.md",
  "TOOLS.md",
  "MEMORY.md",
]);

/**
 * Sanitize a file name for use as an S3 key component. Mirrors
 * bridge/skills/s3-user-files/common.js so both tool surfaces address the
 * same objects, and refuses the reserved _system/ sub-prefix and the six
 * reserved system-prompt filenames so this surface cannot plant persistent
 * instructions in the Converse `system` block.
 */
function sanitizeFilename(str) {
  if (typeof str !== "string" || !str) throw new Error("filename is required");
  if (str.startsWith(SYSTEM_PREFIX) || str === "_system") {
    throw new Error(
      `Invalid filename "${str}": "${SYSTEM_PREFIX}" is reserved for system-prompt sources and is not user-writable.`,
    );
  }
  let result = str;
  while (result.includes("..")) result = result.replace(/\.\./g, "");
  result = result.replace(/[^a-zA-Z0-9_\-.]/g, "_").slice(0, 256);
  if (!result || result.startsWith(".") || result.endsWith(".")) {
    throw new Error(`Invalid filename "${result}": leading/trailing dots not allowed`);
  }
  if (RESERVED_SYSTEM_FILENAMES.has(result)) {
    throw new Error(
      `Invalid filename "${result}": reserved system-prompt source, not writable through the gateway user-files tool.`,
    );
  }
  return result;
}

/** Wrap a handler so thrown errors become a JSON error object, not a Lambda fault. */
function withErrorEnvelope(fn, log = console.log) {
  return async (event, context) => {
    try {
      return await fn(event, context);
    } catch (err) {
      const code = err && err.name === "IdentityError" ? "unauthorized" : "error";
      const message = err && err.message ? err.message : String(err);
      // One structured line so a failing tool is visible in CloudWatch, not
      // only in the model's paraphrase of the result. No arguments are logged.
      log(
        JSON.stringify({
          event: "gateway_tool_error",
          tool: toolNameFromContext(context),
          code,
          name: err && err.name,
          message,
        }),
      );
      return { error: code, message };
    }
  };
}

/**
 * One structured line per tool call so CloudWatch shows which tool ran for
 * which verified namespace and with which Cognito token type. The namespace is
 * the channel identity already present in every other log of this project;
 * no argument values are logged.
 */
function logToolCall(tool, identity, log = console.log) {
  log(
    JSON.stringify({
      event: "gateway_tool_call",
      tool,
      namespace: identity && identity.namespace,
      tokenUse: identity && identity.tokenUse,
    }),
  );
}

module.exports = { TOOL_NAME_DELIMITER, toolNameFromContext, sanitizeFilename, withErrorEnvelope, logToolCall, SYSTEM_PREFIX, RESERVED_SYSTEM_FILENAMES };
