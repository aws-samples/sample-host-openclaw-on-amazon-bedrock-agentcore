/**
 * Extract OpenAI system text, including text blocks with cache metadata.
 * Preserve empty content and use the fallback only when no system message exists.
 * Reject malformed content without including prompt values in errors.
 */
function extractSystemText(messages, fallback) {
  const systemTexts = [];
  for (const [messageIndex, message] of messages.entries()) {
    if (message.role !== "system") continue;

    if (typeof message.content === "string") {
      systemTexts.push(message.content);
      continue;
    }
    if (!Array.isArray(message.content)) {
      throw new TypeError(
        `Invalid system content at message ${messageIndex}: expected a string or an array of text blocks.`,
      );
    }

    const blockTexts = [];
    for (const [blockIndex, block] of message.content.entries()) {
      if (
        !block ||
        typeof block !== "object" ||
        Array.isArray(block) ||
        block.type !== "text" ||
        typeof block.text !== "string"
      ) {
        throw new TypeError(
          `Invalid system block at message ${messageIndex}, block ${blockIndex}: expected an object with type "text" and string text.`,
        );
      }
      blockTexts.push(block.text);
    }
    systemTexts.push(blockTexts.join("\n"));
  }
  return systemTexts.length > 0 ? systemTexts.join("\n") : fallback;
}

module.exports = { extractSystemText };
