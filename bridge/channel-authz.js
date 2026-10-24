/**
 * Per-turn channel delivery authorization.
 *
 * The contract server's /invocations handler binds (userId, actorId,
 * routingGeneration, runtimeSessionId) at activation time, but earlier
 * revisions only shape-checked the per-turn delivery identity (channel,
 * channelActorId, channelTarget). A process inside the microVM could read
 * the active binding from the unauthenticated status action and replay a
 * turn with another user's chat id, redirecting Telegram delivery to the
 * victim's chat and poisoning /tmp/current-identity.json so later cron
 * schedules kept delivering there (CWE-1220).
 *
 * This module validates the delivery identity against the activated user:
 *   - Self-channel (channelActorId === binding.actorId): accept without I/O.
 *   - Cross-channel: look up CHANNEL#<channelActorId> in the identity table
 *     and require the PROFILE record's userId to equal binding.userId. The
 *     enrollment contract in lambda/shared/python/runtime_enrollment.py
 *     writes { PK: "CHANNEL#<actorId>", SK: "PROFILE", userId } for every
 *     channel the user has linked, so a chat id that does not belong to
 *     the activated user never resolves.
 *
 * When IDENTITY_TABLE_NAME is unset (local dev harness, unit tests), the
 * verifier is deliberately NOT a silent no-op: the caller decides the
 * fail-open vs fail-closed stance by passing allowWithoutTable.
 */

const SELF_CHANNEL = Object.freeze({ decision: "allow", reason: "self_channel" });
const MISSING_BINDING = Object.freeze({ decision: "deny", reason: "missing_binding" });
const MISSING_IDENTITY = Object.freeze({ decision: "deny", reason: "missing_channel_actor" });
const TABLE_UNAVAILABLE_ALLOW = Object.freeze({ decision: "allow", reason: "identity_table_unavailable" });
const TABLE_UNAVAILABLE_DENY = Object.freeze({ decision: "deny", reason: "identity_table_unavailable" });

function allowForeignActor(reason) {
  return Object.freeze({ decision: "allow", reason });
}
function denyForeignActor(reason) {
  return Object.freeze({ decision: "deny", reason });
}

function createChannelOwnershipVerifier({
  identityTable, region, lookup, allowWithoutTable = false,
} = {}) {
  const cache = new Map();
  const unavailable = allowWithoutTable ? TABLE_UNAVAILABLE_ALLOW : TABLE_UNAVAILABLE_DENY;

  async function fetchProfile(channelActorId) {
    if (cache.has(channelActorId)) return cache.get(channelActorId);
    if (lookup) {
      const value = await lookup({ channelActorId });
      cache.set(channelActorId, value || null);
      return value || null;
    }
    // Lazy-load the AWS SDK so dev harnesses that never set IDENTITY_TABLE_NAME
    // never pull @aws-sdk/client-dynamodb into memory.
    const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
    const { DynamoDBDocumentClient, GetCommand } = require("@aws-sdk/lib-dynamodb");
    const client = DynamoDBDocumentClient.from(new DynamoDBClient({ region }));
    try {
      const resp = await client.send(new GetCommand({
        TableName: identityTable,
        Key: { PK: `CHANNEL#${channelActorId}`, SK: "PROFILE" },
        ConsistentRead: true,
      }));
      const item = resp.Item || null;
      cache.set(channelActorId, item);
      return item;
    } finally {
      if (typeof client.destroy === "function") client.destroy();
    }
  }

  async function verify(binding, params) {
    const { channel, channelActorId, channelTarget } = params || {};
    if (channelActorId == null && channelTarget == null) {
      return allowForeignActor("no_channel_identity");
    }
    if (!binding || !binding.userId || !binding.actorId) return MISSING_BINDING;
    if (typeof channelActorId !== "string" || !channelActorId) return MISSING_IDENTITY;
    if (channelActorId === binding.actorId) return SELF_CHANNEL;
    if (typeof channel !== "string" || !channel ||
        !channelActorId.startsWith(`${channel}:`) ||
        !channelActorId.slice(channel.length + 1)) {
      return denyForeignActor("malformed_channel_actor");
    }
    if (!identityTable && !lookup) return unavailable;
    let profile;
    try {
      profile = await fetchProfile(channelActorId);
    } catch (error) {
      return Object.freeze({ decision: "deny", reason: "lookup_failed", error });
    }
    if (!profile || typeof profile.userId !== "string" || !profile.userId) {
      return denyForeignActor("unknown_channel");
    }
    if (profile.userId !== binding.userId) return denyForeignActor("foreign_channel");
    return allowForeignActor("verified_channel");
  }

  return { verify };
}

module.exports = { createChannelOwnershipVerifier };
