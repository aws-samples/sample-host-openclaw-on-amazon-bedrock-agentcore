"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  SessionBinding,
  canonicalNamespace,
} = require("./session-binding");

describe("canonical session identity", () => {
  it("returns the exact validated internal identifier without transformation", () => {
    assert.equal(canonicalNamespace("user_A-123"), "user_A-123");
    assert.equal(canonicalNamespace("7f9a-legacy"), "7f9a-legacy");
  });

  it("rejects missing, actor-derived, transformed, and unsafe identifiers", () => {
    for (const value of [
      undefined,
      null,
      "",
      "a",
      "telegram:123",
      "../user_A",
      "user/A",
      " user_A",
      "user_A ",
      "user\u0000A",
      "a".repeat(65),
    ]) {
      assert.throws(() => canonicalNamespace(value), (error) => {
        assert.equal(error.code, "INVALID_SESSION_IDENTITY");
        return true;
      });
    }
  });

  it("accepts the exact two and sixty-four character boundaries", () => {
    assert.equal(canonicalNamespace("a1"), "a1");
    assert.equal(canonicalNamespace(`a${"1".repeat(63)}`).length, 64);
  });

  it("rejects Unicode confusables and malformed UTF-16", () => {
    for (const value of ["u\u0455er_A", "user_\ud800", "éclair"]) {
      assert.throws(() => canonicalNamespace(value), (error) => {
        assert.equal(error.code, "INVALID_SESSION_IDENTITY");
        return true;
      });
    }
  });
});

describe("SessionBinding", () => {
  it("binds once and accepts only the same exact pair", () => {
    const binding = new SessionBinding();
    assert.deepEqual(
      binding.bindOrAssert({
        internalUserId: "user_A",
        namespace: "user_A",
      }),
      { internalUserId: "user_A", namespace: "user_A" },
    );
    assert.deepEqual(
      binding.bindOrAssert({
        internalUserId: "user_A",
        namespace: "user_A",
      }),
      { internalUserId: "user_A", namespace: "user_A" },
    );
  });

  it("does not let callers rewrite the retained binding", () => {
    const binding = new SessionBinding();
    const retained = binding.bindOrAssert({
      internalUserId: "user_A",
      namespace: "user_A",
    });

    assert.ok(Object.isFrozen(retained));
    assert.throws(() => {
      retained.internalUserId = "user_B";
    }, TypeError);
    assert.deepEqual(binding.current(), {
      internalUserId: "user_A",
      namespace: "user_A",
    });
  });

  it("copies the caller pair before the caller can mutate its input", () => {
    const binding = new SessionBinding();
    const candidate = { internalUserId: "user_A", namespace: "user_A" };
    binding.bindOrAssert(candidate);
    candidate.internalUserId = "user_B";
    candidate.namespace = "user_B";

    assert.deepEqual(binding.current(), {
      internalUserId: "user_A",
      namespace: "user_A",
    });
  });

  it("throws synchronously on every mismatch and preserves the first binding", () => {
    const binding = new SessionBinding();
    binding.bindOrAssert({ internalUserId: "user_A", namespace: "user_A" });

    for (const candidate of [
      { internalUserId: "user_B", namespace: "user_B" },
      { internalUserId: "user_A", namespace: "user_B" },
      { internalUserId: "user_B", namespace: "user_A" },
      { internalUserId: "user_A", namespace: "telegram_123" },
    ]) {
      assert.throws(() => binding.bindOrAssert(candidate), (error) => {
        assert.equal(error.code, "SESSION_IDENTITY_MISMATCH");
        return true;
      });
    }

    assert.deepEqual(binding.current(), {
      internalUserId: "user_A",
      namespace: "user_A",
    });
  });

  it("normalizes every malformed later pair to SESSION_IDENTITY_MISMATCH", () => {
    const binding = new SessionBinding();
    binding.bindOrAssert({ internalUserId: "user_A", namespace: "user_A" });

    for (const candidate of [
      undefined,
      {},
      { internalUserId: "user_A" },
      { namespace: "user_A" },
      { internalUserId: "../user_A", namespace: "../user_A" },
      { internalUserId: "user_A", namespace: "telegram:123" },
      { internalUserId: "\ud800x", namespace: "\ud800x" },
    ]) {
      assert.throws(() => binding.bindOrAssert(candidate), (error) => {
        assert.equal(error.code, "SESSION_IDENTITY_MISMATCH");
        return true;
      });
    }
    assert.deepEqual(binding.current(), {
      internalUserId: "user_A",
      namespace: "user_A",
    });
  });

  it("does not bind invalid first input", () => {
    const binding = new SessionBinding();
    assert.throws(
      () =>
        binding.bindOrAssert({
          internalUserId: "user_A",
          namespace: "telegram_123",
        }),
      (error) => {
        assert.equal(error.code, "SESSION_IDENTITY_MISMATCH");
        return true;
      },
    );
    assert.equal(binding.current(), null);
  });
});
