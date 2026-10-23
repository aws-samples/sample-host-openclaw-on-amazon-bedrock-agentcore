/**
 * Scope Bedrock Guardrails input assessment to the user's latest message.
 *
 * Without guardContent tags, a Converse guardrail assesses every user-role
 * text block in the conversation. Once a blocked string (a card number)
 * is in the transcript, every later turn in that session can be blocked
 * too, including "hello".
 *
 * Wrapping text in guardContent makes the guardrail assess only the wrapped
 * blocks (model output is still assessed in full). This tags the text blocks
 * of the trailing user turn — the messages after the last assistant turn —
 * without excluding text based on markers the user can supply. When the
 * trailing turn carries only tool results (the model is mid tool-call), the
 * most recent earlier user turn is tagged instead: tool results cannot carry
 * guardContent, and leaving the request untagged would fall back to assessing
 * the whole conversation — exactly the behaviour this module exists to avoid.
 */

const crypto = require("crypto");

/**
 * Return a copy of `bedrockMessages` where the text blocks of the trailing
 * user turn are wrapped in guardContent. Tool results and images are left
 * as they are. If the trailing
 * turn has no taggable text (e.g. it is only tool results), the nearest
 * earlier user turn is tagged instead. Only when the conversation has
 * no user text at all is the input returned unchanged (default scope).
 */
function scopeGuardrailToLatestUserTurn(bedrockMessages) {
  if (!Array.isArray(bedrockMessages) || bedrockMessages.length === 0) return bedrockMessages;

  // Index of the first message of the trailing user turn.
  let start = bedrockMessages.length;
  while (start > 0 && bedrockMessages[start - 1].role === "user") start--;
  if (start === bedrockMessages.length) return bedrockMessages;

  const taggable = (block) => typeof block.text === "string";
  const tagMessage = (msg) => ({
    ...msg,
    content: (msg.content || []).map((block) =>
      taggable(block) ? { guardContent: { text: { text: block.text } } } : block,
    ),
  });

  const out = bedrockMessages.slice();
  let tagged = 0;
  for (let i = start; i < out.length; i++) {
    if ((out[i].content || []).some(taggable)) {
      out[i] = tagMessage(out[i]);
      tagged++;
    }
  }
  if (tagged > 0) return out;

  // Trailing turn is tool results only: tag the latest earlier user turn.
  for (let i = start - 1; i >= 0; i--) {
    if (out[i].role === "user" && (out[i].content || []).some(taggable)) {
      let first = i;
      while (first > 0 && out[first - 1].role === "user") first--;
      for (let j = first; j <= i; j++) out[j] = tagMessage(out[j]);
      return out;
    }
  }
  return bedrockMessages;
}

// Replaces earlier text that a guardrail already stopped (see createBlockedInputRegistry).
const BLOCKED_INPUT_PLACEHOLDER = "[An earlier message was blocked by the content policy and is not shown.]";
const MAX_BLOCKED_INPUTS = 256;

const textKey = (text) =>
  crypto.createHash("sha256").update(text.trim().replace(/\s+/g, " "), "utf8").digest("hex");

/**
 * Keep text the guardrail stopped out of later requests.
 *
 * Only the tagged turn is assessed, so a message the guardrail blocked would
 * otherwise reach the model untagged as history on the next request. The proxy
 * records the tagged text of every request that ends in guardrail_intervened
 * and, on later requests, replaces matching untagged text with a placeholder.
 * Tagged text is left alone: the guardrail assesses it again. Only SHA-256
 * digests are kept, in process memory, bounded to MAX_BLOCKED_INPUTS (oldest
 * evicted). The proxy serves one bound user, so the registry is per user.
 *
 * Bedrock reports input and output interventions with the same stop reason,
 * so text whose model reply was blocked is also kept out of later history.
 */
function createBlockedInputRegistry(limit = MAX_BLOCKED_INPUTS) {
  const keys = new Set();

  /** Record the guardContent-tagged text of a request the guardrail stopped. */
  function record(messages) {
    if (!Array.isArray(messages)) return;
    for (const msg of messages) {
      for (const block of msg?.content || []) {
        const text = block?.guardContent?.text?.text;
        if (typeof text !== "string" || !text.trim()) continue;
        const key = textKey(text);
        keys.delete(key);
        keys.add(key);
        if (keys.size > limit) keys.delete(keys.values().next().value);
      }
    }
  }

  /** Return `messages` with recorded untagged text blocks replaced; input is not mutated. */
  function redact(messages) {
    if (keys.size === 0 || !Array.isArray(messages)) return messages;
    let changed = false;
    const out = messages.map((msg) => {
      if (!Array.isArray(msg?.content)) return msg;
      let hit = false;
      const content = msg.content.map((block) => {
        if (typeof block?.text !== "string" || !keys.has(textKey(block.text))) return block;
        hit = true;
        return { ...block, text: BLOCKED_INPUT_PLACEHOLDER };
      });
      if (!hit) return msg;
      changed = true;
      return { ...msg, content };
    });
    return changed ? out : messages;
  }

  return { record, redact, size: () => keys.size };
}

module.exports = {
  scopeGuardrailToLatestUserTurn, createBlockedInputRegistry, BLOCKED_INPUT_PLACEHOLDER, MAX_BLOCKED_INPUTS,
};
