"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

process.env.AWS_REGION = "eu-west-1";
process.env.AWS_DEFAULT_REGION = "eu-west-1";

const {
  createActiveTaskTracker,
  createRuntimeInitializationGuard,
  createUnexpectedChildExitHandler,
} = require("./agentcore-contract");

function deferred() {
  let resolve;
  const promise = new Promise((onResolve) => {
    resolve = onResolve;
  });
  return { promise, resolve };
}

describe("one-shot runtime initialization", () => {
  it("cannot be claimed again after child and workspace startup began", () => {
    const guard = createRuntimeInitializationGuard();
    guard.claim();
    assert.equal(guard.attempted, true);
    assert.throws(
      () => guard.claim(),
      (error) => error.code === "RUNTIME_REINITIALIZATION_FORBIDDEN",
    );
  });
});

describe("unexpected trusted child exit", () => {
  it("marks the proxy unavailable and quarantines instead of allowing reinit", () => {
    const events = [];
    const handler = createUnexpectedChildExitHandler({
      label: "Bedrock proxy",
      code: "BEDROCK_PROXY_EXITED",
      isExpected: () => false,
      markUnavailable: () => events.push("unavailable"),
      quarantine: (error, code) =>
        events.push({ code, message: error.message }),
    });

    handler(17, "SIGABRT");

    assert.deepEqual(events, [
      "unavailable",
      {
        code: "BEDROCK_PROXY_EXITED",
        message: "Bedrock proxy exited unexpectedly (code=17, signal=SIGABRT)",
      },
    ]);
  });

  it("does not quarantine an exit caused by ordered shutdown", () => {
    const events = [];
    const handler = createUnexpectedChildExitHandler({
      label: "Bedrock proxy",
      code: "BEDROCK_PROXY_EXITED",
      isExpected: () => true,
      markUnavailable: () => events.push("unavailable"),
      quarantine: () => events.push("quarantine"),
    });

    handler(0, "SIGTERM");
    assert.deepEqual(events, ["unavailable"]);
  });
});

describe("active task health accounting", () => {
  it("remains busy until execution and its durable persistence both settle", async () => {
    const tracker = createActiveTaskTracker();
    const execution = deferred();
    const persistence = deferred();
    const task = tracker.run(async () => {
      await execution.promise;
      await persistence.promise;
      return "durable";
    });

    assert.equal(tracker.count, 1);
    execution.resolve();
    await Promise.resolve();
    assert.equal(tracker.count, 1, "persistence is still pending");
    persistence.resolve();
    assert.equal(await task, "durable");
    assert.equal(tracker.count, 0);
  });

  it("decrements exactly once when persistence rejects", async () => {
    const tracker = createActiveTaskTracker();
    await assert.rejects(
      tracker.run(async () => {
        throw new Error("CAS failed");
      }),
      /CAS failed/,
    );
    assert.equal(tracker.count, 0);
  });
});
