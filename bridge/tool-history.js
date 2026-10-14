/**
 * Keep Converse requests valid when the history holds tool blocks but the
 * current request carries no tools.
 *
 * Bedrock rejects such a request with "ValidationException: The toolConfig
 * field must be defined when using toolUse and toolResult content blocks."
 * OpenClaw sends tool-less requests over a history that still contains an
 * earlier run's tool turn (for example the first turn of a new run, or a
 * non-streaming side request), so the proxy must not forward those blocks
 * as structured toolUse/toolResult.
 *
 * The blocks are rewritten as plain text instead of synthesising a
 * toolConfig from the tool names in history: a synthetic toolConfig would
 * let the model emit tool calls the client never offered on this request.
 * Text keeps what the model did and saw, and changes nothing when the
 * request does carry tools.
 */

const MAX_INPUT_CHARS = 4000;

function clip(text) {
  return text.length > MAX_INPUT_CHARS ? `${text.slice(0, MAX_INPUT_CHARS)}…[truncated]` : text;
}

function stringify(value) {
  try {
    return JSON.stringify(value === undefined ? {} : value);
  } catch {
    return "{}";
  }
}

function toolUseText(toolUse) {
  const name = typeof toolUse.name === "string" && toolUse.name ? toolUse.name : "unknown";
  return `[Tool call ${name} (id ${toolUse.toolUseId || "unknown"})] ${clip(stringify(toolUse.input))}`;
}

function toolResultText(toolResult) {
  const parts = [];
  for (const item of Array.isArray(toolResult.content) ? toolResult.content : []) {
    if (typeof item?.text === "string") parts.push(item.text);
    else if (item && Object.hasOwn(item, "json")) parts.push(stringify(item.json));
    else parts.push("[non-text tool output omitted]");
  }
  const status = toolResult.status === "error" ? " error" : "";
  const body = parts.join("\n").trim() || "(empty)";
  return `[Tool result${status} (id ${toolResult.toolUseId || "unknown"})]\n${body}`;
}

function hasToolBlocks(bedrockMessages) {
  return Array.isArray(bedrockMessages) && bedrockMessages.some((msg) =>
    Array.isArray(msg?.content) && msg.content.some((block) => block && (block.toolUse || block.toolResult)));
}

/**
 * Return messages Bedrock accepts for the given toolConfig. With tools, the
 * input is returned unchanged. Without tools, toolUse/toolResult blocks are
 * replaced by text blocks; other blocks and message order are untouched.
 */
function reconcileToolHistory(bedrockMessages, toolConfig) {
  if (toolConfig || !hasToolBlocks(bedrockMessages)) return bedrockMessages;
  return bedrockMessages.map((msg) => {
    if (!Array.isArray(msg?.content) || !msg.content.some((b) => b && (b.toolUse || b.toolResult))) return msg;
    return {
      ...msg,
      content: msg.content.map((block) => {
        if (block?.toolUse) return { text: toolUseText(block.toolUse) };
        if (block?.toolResult) return { text: toolResultText(block.toolResult) };
        return block;
      }),
    };
  });
}

module.exports = { reconcileToolHistory, hasToolBlocks };
