const { test } = require("node:test");
const assert = require("node:assert/strict");
const ns = require("./user-namespace");

const OPAQUE = "ns_" + "abcdefghijklmnopqrstuvwxyz";

test("the legacy namespace replaces every ':' of the actor", () => {
  assert.equal(ns.legacyNamespace("telegram:123"), "telegram_123");
  assert.equal(ns.legacyNamespace("telegram_group:-100123"), "telegram_group_-100123");
  assert.equal(ns.legacyNamespace("a:b:c"), "a_b_c");
});

test("a request namespace is the named one when it belongs to the actor, else null", () => {
  assert.equal(ns.requestNamespace("telegram:123", undefined), "telegram_123");
  assert.equal(ns.requestNamespace("telegram:123", null), "telegram_123");
  assert.equal(ns.requestNamespace("telegram:123", "telegram_123"), "telegram_123");
  assert.equal(ns.requestNamespace("telegram:123", OPAQUE), OPAQUE);
  for (const value of ["telegram_1234", "slack_123", "", "ns_" + "a".repeat(25), "ns_" + "a".repeat(27),
    "ns_" + "1".repeat(26), "NS_" + "A".repeat(26), 7, {}, ["telegram_123"]]) {
    assert.equal(ns.requestNamespace("telegram:123", value), null, JSON.stringify(value));
  }
  assert.equal(ns.requestNamespace("", "telegram_123"), null);
  assert.equal(ns.requestNamespace(undefined, undefined), null);
});

test("the Cognito username is the actor for a legacy namespace and the namespace once it is opaque", () => {
  assert.equal(ns.cognitoUsername("telegram:123", "telegram_123"), "telegram:123");
  assert.equal(ns.cognitoUsername("telegram:123", OPAQUE), OPAQUE);
  assert.ok(ns.isOpaqueNamespace(OPAQUE));
  assert.equal(ns.isOpaqueNamespace("telegram_123"), false);
});

test("Slack channel and Feishu group identities have both namespace forms too", () => {
  for (const actor of ["slack_channel:C0123CHAN", "feishu_group:oc_0123abcdef"]) {
    const legacy = actor.replace(/:/g, "_");
    assert.equal(ns.requestNamespace(actor, undefined), legacy);
    assert.equal(ns.requestNamespace(actor, legacy), legacy);
    assert.equal(ns.requestNamespace(actor, OPAQUE), OPAQUE);
    assert.equal(ns.requestNamespace(actor, actor.split(":")[0].split("_")[0] + "_" + actor.split(":")[1]), null,
      "a member-shaped namespace is not the conversation's");
    assert.equal(ns.cognitoUsername(actor, legacy), actor);
    assert.equal(ns.cognitoUsername(actor, OPAQUE), OPAQUE);
  }
});
