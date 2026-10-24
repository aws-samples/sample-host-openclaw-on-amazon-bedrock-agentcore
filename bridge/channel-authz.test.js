const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createChannelOwnershipVerifier } = require("./channel-authz");

const binding = Object.freeze({ userId: "u1", actorId: "telegram:123" });

test("missing per-turn delivery identity is allowed", async () => {
  const { verify } = createChannelOwnershipVerifier();
  assert.deepEqual(await verify(binding, {}), { decision: "allow", reason: "no_channel_identity" });
  assert.deepEqual(
    await verify(binding, { channel: null, channelActorId: null, channelTarget: null }),
    { decision: "allow", reason: "no_channel_identity" },
  );
});

test("self-channel is allowed without any identity table lookup", async () => {
  let called = 0;
  const { verify } = createChannelOwnershipVerifier({
    identityTable: "identity", lookup: async () => { called++; return null; },
  });
  const result = await verify(binding, {
    channel: "telegram", channelActorId: "telegram:123", channelTarget: "123",
  });
  assert.deepEqual(result, { decision: "allow", reason: "self_channel" });
  assert.equal(called, 0);
});

test("cross-channel is allowed when the identity table maps to the bound user", async () => {
  const records = { "slack:U456": { userId: "u1" } };
  const { verify } = createChannelOwnershipVerifier({
    identityTable: "identity",
    lookup: async ({ channelActorId }) => records[channelActorId] || null,
  });
  const result = await verify(binding, {
    channel: "slack", channelActorId: "slack:U456", channelTarget: "C789",
  });
  assert.deepEqual(result, { decision: "allow", reason: "verified_channel" });
});

test("cross-channel is denied when the identity table maps to another user", async () => {
  const records = { "telegram:999": { userId: "u-attacker-victim" } };
  const { verify } = createChannelOwnershipVerifier({
    identityTable: "identity",
    lookup: async ({ channelActorId }) => records[channelActorId] || null,
  });
  const result = await verify(binding, {
    channel: "telegram", channelActorId: "telegram:999", channelTarget: "999",
  });
  assert.equal(result.decision, "deny");
  assert.equal(result.reason, "foreign_channel");
});

test("cross-channel is denied when no profile exists", async () => {
  const { verify } = createChannelOwnershipVerifier({
    identityTable: "identity", lookup: async () => null,
  });
  const result = await verify(binding, {
    channel: "telegram", channelActorId: "telegram:12345", channelTarget: "12345",
  });
  assert.equal(result.decision, "deny");
  assert.equal(result.reason, "unknown_channel");
});

test("cross-channel is denied when the lookup throws", async () => {
  const { verify } = createChannelOwnershipVerifier({
    identityTable: "identity",
    lookup: async () => { throw new Error("ddb down"); },
  });
  const result = await verify(binding, {
    channel: "slack", channelActorId: "slack:U456", channelTarget: "C789",
  });
  assert.equal(result.decision, "deny");
  assert.equal(result.reason, "lookup_failed");
});

test("malformed channel actor id (missing channel prefix) is denied", async () => {
  const { verify } = createChannelOwnershipVerifier({
    identityTable: "identity", lookup: async () => ({ userId: "u1" }),
  });
  for (const channelActorId of ["not-a-channel", "slack:", ""]) {
    const result = await verify(binding, {
      channel: "slack", channelActorId, channelTarget: "C789",
    });
    assert.equal(result.decision, "deny", channelActorId);
  }
});

test("without an identity table, cross-channel is fail-closed by default", async () => {
  const { verify } = createChannelOwnershipVerifier();
  const result = await verify(binding, {
    channel: "slack", channelActorId: "slack:U456", channelTarget: "C789",
  });
  assert.equal(result.decision, "deny");
  assert.equal(result.reason, "identity_table_unavailable");
});

test("without an identity table, cross-channel can opt into fail-open", async () => {
  const { verify } = createChannelOwnershipVerifier({ allowWithoutTable: true });
  const result = await verify(binding, {
    channel: "slack", channelActorId: "slack:U456", channelTarget: "C789",
  });
  assert.equal(result.decision, "allow");
  assert.equal(result.reason, "identity_table_unavailable");
});

test("lookup results are cached across calls to avoid DynamoDB round trips", async () => {
  let calls = 0;
  const { verify } = createChannelOwnershipVerifier({
    identityTable: "identity",
    lookup: async () => { calls++; return { userId: "u1" }; },
  });
  for (let i = 0; i < 5; i++) {
    await verify(binding, {
      channel: "slack", channelActorId: "slack:U456", channelTarget: "C789",
    });
  }
  assert.equal(calls, 1);
});

test("missing binding fails closed", async () => {
  const { verify } = createChannelOwnershipVerifier({ identityTable: "identity" });
  const result = await verify(null, {
    channel: "slack", channelActorId: "slack:U456", channelTarget: "C789",
  });
  assert.equal(result.decision, "deny");
  assert.equal(result.reason, "missing_binding");
});

test("channelTarget without channelActorId fails closed on identity shape", async () => {
  const { verify } = createChannelOwnershipVerifier({ identityTable: "identity" });
  const result = await verify(binding, {
    channel: "slack", channelActorId: null, channelTarget: "C789",
  });
  assert.equal(result.decision, "deny");
  assert.equal(result.reason, "missing_channel_actor");
});
