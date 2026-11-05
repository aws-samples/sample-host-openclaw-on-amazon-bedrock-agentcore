"use strict";

/**
 * Bedrock Converse `requestMetadata` for token attribution.
 *
 * Bedrock copies requestMetadata into the model invocation log record, where
 * lambda/token_metrics reads it to attribute tokens and cost to a user,
 * channel, session and agent type. Without it every call lands on
 * default-user / unknown / default-session.
 *
 * The user is the internal user ID (`user_<16 hex>`), never the channel actor:
 * a WhatsApp actor is the user's phone number, and the invocation log group is
 * shared by every Bedrock caller in the account.
 *
 * Values are identifiers only, never message content or secrets. Bedrock
 * allows at most 16 entries, keys and values up to 256 characters, and the
 * characters [a-zA-Z0-9\s:_@$#=/+,-.]. Every value is reduced to that set
 * (whitespace excluded); a value that is too long or had characters replaced
 * is sent as a hash so distinct inputs stay distinct.
 */

const crypto = require("crypto");

const MAX_VALUE_LENGTH = 128;
const DISALLOWED = /[^A-Za-z0-9:_@$#=/+,.-]/g;
// Channel names are short lowercase words (telegram, slack, feishu, ...).
const CHANNEL = /^[a-z][a-z0-9_-]{0,31}$/;
const TRIGGERS = new Set(["chat", "cron"]);
const MODEL_ROUTES = new Set(["primary", "fallback"]);
// The internal user ID (lambda/shared/python/runtime_enrollment.py).
const INTERNAL_USER_ID = /^user_[0-9a-f]{16}$/;

/** Reduce a value to Bedrock's allowed set; hash it if anything was lost. */
function sanitizeValue(value, fallback) {
  if (typeof value !== "string" || !value) return fallback;
  const cleaned = value.replace(DISALLOWED, "_");
  if (cleaned === value && value.length <= MAX_VALUE_LENGTH) return value;
  return "sha256:" + crypto.createHash("sha256").update(value).digest("hex").slice(0, 32);
}

function oneOf(value, allowed, fallback) {
  return typeof value === "string" && allowed.has(value) ? value : fallback;
}

/**
 * Build the requestMetadata map for one Converse/ConverseStream request.
 * @param {object} ctx
 * @param {string} [ctx.userId]   bound internal user ID; omitted from the map when absent or malformed
 * @param {string} ctx.channel    delivery channel
 * @param {string} ctx.sessionId  AgentCore runtime session (or the proxy's own)
 * @param {boolean} ctx.subagent  request came from an OpenClaw sub-agent
 * @param {string} ctx.trigger    "chat" or "cron": the kind of turn that caused the call
 * @param {string} ctx.idSource   how the proxy resolved the turn identity
 * @param {string} [ctx.modelRoute] "primary" or "fallback" (model fallback route)
 */
function buildRequestMetadata(ctx = {}) {
  const trigger = oneOf(ctx.trigger, TRIGGERS, "chat");
  const userId = typeof ctx.userId === "string" && INTERNAL_USER_ID.test(ctx.userId) ? ctx.userId : null;
  return {
    // No user ID, no field: the call is pooled on default-user. Never the actor instead.
    ...(userId ? { "openclaw.user_id": userId } : {}),
    "openclaw.channel": typeof ctx.channel === "string" && CHANNEL.test(ctx.channel)
      ? ctx.channel : "unknown",
    "openclaw.session_id": sanitizeValue(ctx.sessionId, "default-session"),
    // A sub-agent is a sub-agent whatever started it; otherwise the turn kind decides.
    "openclaw.agent_type": ctx.subagent ? "subagent" : trigger === "cron" ? "cron" : "main",
    "openclaw.trigger": trigger,
    // Unbound calls stay explicit: they are pooled on default-user and say so.
    "openclaw.identity": userId ? "bound" : "unbound",
    "openclaw.id_source": sanitizeValue(ctx.idSource, "none"),
    "openclaw.model_route": oneOf(ctx.modelRoute, MODEL_ROUTES, "primary"),
  };
}

module.exports = { buildRequestMetadata, sanitizeValue, MAX_VALUE_LENGTH, INTERNAL_USER_ID };
