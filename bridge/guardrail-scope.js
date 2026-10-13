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

module.exports = { scopeGuardrailToLatestUserTurn };
