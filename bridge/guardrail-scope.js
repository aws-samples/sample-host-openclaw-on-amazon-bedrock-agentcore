/**
 * Scope Bedrock Guardrails input assessment to the user's latest message.
 *
 * Without guardContent tags, a Converse guardrail assesses every user-role
 * text block in the conversation. Two things go wrong for an OpenClaw
 * gateway sitting behind the proxy:
 *
 *   1. OpenClaw injects its own user-role blocks alongside the person's
 *      message — a "Runtime: agent=main | session=... | host=..." trailer on
 *      the first turn and "<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>" data blocks.
 *      Together they read as an injection attempt to the PROMPT_ATTACK
 *      filter, so an ordinary first question ("What is the capital of
 *      Australia?") is blocked.
 *   2. History is re-assessed on every turn. Once a blocked string (a card
 *      number) is in the transcript, every later turn in that session is
 *      blocked too, including "hello".
 *
 * Wrapping text in guardContent makes the guardrail assess only the wrapped
 * blocks (model output is still assessed in full). This tags the text blocks
 * of the trailing user turn — the messages after the last assistant turn —
 * and skips OpenClaw's internal-context blocks and tool results.
 */

const INTERNAL_CONTEXT_MARKER = "<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>";

function isInternalContext(text) {
  return typeof text === "string" && text.trimStart().startsWith(INTERNAL_CONTEXT_MARKER);
}

/**
 * Return a copy of `bedrockMessages` where the text blocks of the trailing
 * user turn are wrapped in guardContent. Blocks that are tool results,
 * images, or OpenClaw internal context are left as they are. If nothing is
 * taggable (e.g. the turn is only tool results), the input is returned
 * unchanged so the guardrail keeps its default (assess everything) scope
 * rather than silently assessing nothing.
 */
function scopeGuardrailToLatestUserTurn(bedrockMessages) {
  if (!Array.isArray(bedrockMessages) || bedrockMessages.length === 0) return bedrockMessages;

  // Index of the first message of the trailing user turn.
  let start = bedrockMessages.length;
  while (start > 0 && bedrockMessages[start - 1].role === "user") start--;
  if (start === bedrockMessages.length) return bedrockMessages;

  let tagged = 0;
  const out = bedrockMessages.slice(0, start);
  for (let i = start; i < bedrockMessages.length; i++) {
    const msg = bedrockMessages[i];
    const content = (msg.content || []).map((block) => {
      if (typeof block.text !== "string" || isInternalContext(block.text)) return block;
      tagged++;
      return { guardContent: { text: { text: block.text } } };
    });
    out.push({ ...msg, content });
  }
  return tagged > 0 ? out : bedrockMessages;
}

module.exports = { scopeGuardrailToLatestUserTurn, isInternalContext, INTERNAL_CONTEXT_MARKER };
