"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { SessionBinding } = require("./session-binding");
const { createInvocationHandler } = require("./invocation-handler");

function payload(action, overrides = {}) {
  return {
    action,
    internalUserId: "user_A",
    namespace: "user_A",
    actorId: "telegram:111",
    channel: "telegram",
    ...overrides,
  };
}

describe("trusted invocation admission", () => {
  it("binds synchronously before status, warmup, chat, snapshot, or cron dispatch", () => {
    for (const action of ["status", "warmup", "chat", "snapshot", "cron"]) {
      const trace = [];
      const handler = createInvocationHandler({
        sessionBinding: new SessionBinding(),
        handlers: {
          [action]: () => {
            trace.push("dispatch");
          },
        },
      });

      handler.handle(payload(action));
      assert.deepEqual(trace, ["dispatch"]);

      trace.length = 0;
      assert.throws(
        () =>
          handler.handle(
            payload(action, {
              internalUserId: "user_B",
              namespace: "user_B",
            }),
          ),
        (error) => {
          assert.equal(error.code, "SESSION_IDENTITY_MISMATCH");
          return true;
        },
      );
      assert.deepEqual(trace, [], `${action} must not dispatch after mismatch`);
    }
  });

  it("rejects a second user immediately while the first user's init is awaiting", async () => {
    let finishInit;
    const trace = [];
    const handler = createInvocationHandler({
      sessionBinding: new SessionBinding(),
      handlers: {
        warmup: () =>
          new Promise((resolve) => {
            trace.push("A-start");
            finishInit = resolve;
          }),
        chat: () => trace.push("B-chat"),
      },
    });

    const first = handler.handle(payload("warmup"));
    assert.deepEqual(trace, ["A-start"]);
    assert.throws(
      () =>
        handler.handle(
          payload("chat", {
            internalUserId: "user_B",
            namespace: "user_B",
          }),
        ),
      (error) => {
        assert.equal(error.code, "SESSION_IDENTITY_MISMATCH");
        return true;
      },
    );
    assert.deepEqual(trace, ["A-start"]);
    finishInit("ready");
    assert.equal(await first, "ready");
  });

  it("allows linked delivery actors to change without changing authority", () => {
    const deliveries = [];
    const binding = new SessionBinding();
    const handler = createInvocationHandler({
      sessionBinding: binding,
      handlers: {
        chat: ({ identity, delivery }) => deliveries.push({ identity, delivery }),
      },
    });

    handler.handle(payload("chat"));
    handler.handle(
      payload("chat", { actorId: "slack:U222", channel: "slack" }),
    );

    assert.deepEqual(binding.current(), {
      internalUserId: "user_A",
      namespace: "user_A",
    });
    assert.deepEqual(
      deliveries.map(({ identity }) => identity),
      [
        { internalUserId: "user_A", namespace: "user_A" },
        { internalUserId: "user_A", namespace: "user_A" },
      ],
    );
    assert.deepEqual(deliveries.map(({ delivery }) => delivery), [
      { actorId: "telegram:111", channel: "telegram" },
      { actorId: "slack:U222", channel: "slack" },
    ]);
  });

  it("snapshots and freezes only allowlisted request data before awaiting", async () => {
    let release;
    let observed;
    const gate = new Promise((resolve) => {
      release = resolve;
    });
    const handler = createInvocationHandler({
      sessionBinding: new SessionBinding(),
      handlers: {
        chat: async (context) => {
          await gate;
          observed = context;
        },
      },
    });
    const raw = payload("chat", {
      message: {
        text: "original",
        images: [{ s3Key: "user_A/_uploads/a.png", contentType: "image/png" }],
      },
      invocationId: "invocation-1",
      sessionId: "caller-session-must-be-dropped",
      userId: "legacy-authority",
      unexpected: "drop-me",
    });

    const pending = handler.handle(raw);
    raw.action = "status";
    raw.internalUserId = "user_B";
    raw.namespace = "user_B";
    raw.actorId = "slack:attacker";
    raw.message.text = "mutated";
    raw.message.images[0].s3Key = "user_B/_uploads/stolen.png";
    release();
    await pending;

    assert.deepEqual(observed.identity, {
      internalUserId: "user_A",
      namespace: "user_A",
    });
    assert.deepEqual(observed.delivery, {
      actorId: "telegram:111",
      channel: "telegram",
    });
    assert.deepEqual(observed.request, {
      message: {
        text: "original",
        images: [
          {
            s3Key: "user_A/_uploads/a.png",
            contentType: "image/png",
          },
        ],
      },
      invocationId: "invocation-1",
    });
    assert.equal(Object.isFrozen(observed.request), true);
    assert.equal(Object.isFrozen(observed.request.message), true);
    assert.equal(Object.isFrozen(observed.request.message.images), true);
    assert.equal("userId" in observed.request, false);
    assert.equal("internalUserId" in observed.request, false);
    assert.equal("namespace" in observed.request, false);
    assert.equal("actorId" in observed.request, false);
    assert.equal("channel" in observed.request, false);
    assert.equal("sessionId" in observed.request, false);
    assert.equal("action" in observed.request, false);
    assert.equal("unexpected" in observed.request, false);
    assert.equal("payload" in observed, false);
  });

  it("does not treat legacy userId or delivery metadata as identity", () => {
    const handler = createInvocationHandler({
      sessionBinding: new SessionBinding(),
      handlers: { chat: () => assert.fail("must not dispatch") },
    });

    for (const candidate of [
      {
        action: "chat",
        userId: "user_A",
        actorId: "user_A",
        namespace: "user_A",
      },
      {
        action: "chat",
        internalUserId: "user_A",
        actorId: "user_A",
      },
    ]) {
      assert.throws(() => handler.handle(candidate), (error) => {
        assert.equal(error.code, "INVALID_SESSION_IDENTITY");
        return true;
      });
    }
  });

  it("rejects unsupported actions only after binding the trusted identity", () => {
    const binding = new SessionBinding();
    const handler = createInvocationHandler({
      sessionBinding: binding,
      handlers: {},
    });

    for (const action of ["erase-everything", "toString", "constructor", 42]) {
      assert.throws(() => handler.handle(payload(action)), (error) => {
        assert.equal(error.code, "UNSUPPORTED_INVOCATION_ACTION");
        return true;
      });
    }
    assert.deepEqual(binding.current(), {
      internalUserId: "user_A",
      namespace: "user_A",
    });
  });

  it("does not reinterpret falsey caller actions as status", () => {
    for (const action of ["", 0, false, null]) {
      const trace = [];
      const handler = createInvocationHandler({
        sessionBinding: new SessionBinding(),
        handlers: { status: () => trace.push("status") },
      });

      assert.throws(() => handler.handle(payload(action)), (error) => {
        assert.equal(error.code, "UNSUPPORTED_INVOCATION_ACTION");
        return true;
      });
      assert.deepEqual(trace, []);
    }
  });

  it("keeps omitted action as the explicit status compatibility default", () => {
    const handler = createInvocationHandler({
      sessionBinding: new SessionBinding(),
      handlers: { status: (context) => context.identity },
    });
    const candidate = payload(undefined);
    delete candidate.action;

    assert.deepEqual(handler.handle(candidate), {
      internalUserId: "user_A",
      namespace: "user_A",
    });
  });

  it("rejects prototype-bearing request keys instead of cloning them", () => {
    const handler = createInvocationHandler({
      sessionBinding: new SessionBinding(),
      handlers: { chat: () => assert.fail("must not dispatch") },
    });
    const pollutedMessage = JSON.parse(
      '{"text":"safe","__proto__":{"polluted":true}}',
    );

    assert.throws(
      () => handler.handle(payload("chat", { message: pollutedMessage })),
      /request|key|prototype/i,
    );
    assert.equal({}.polluted, undefined);
  });

  it("rejects non-JSON numeric values in request snapshots", () => {
    const handler = createInvocationHandler({
      sessionBinding: new SessionBinding(),
      handlers: { chat: () => assert.fail("must not dispatch") },
    });

    for (const message of [Number.NaN, Infinity, -Infinity, 1n]) {
      assert.throws(
        () => handler.handle(payload("chat", { message })),
        /request|unsupported|JSON/i,
      );
    }
  });

  it("rejects oversized or malformed delivery metadata", () => {
    const handler = createInvocationHandler({
      sessionBinding: new SessionBinding(),
      handlers: { chat: () => assert.fail("must not dispatch") },
    });

    for (const actorId of ["a".repeat(257), "actor-\ud800"]) {
      assert.throws(
        () => handler.handle(payload("chat", { actorId })),
        /actorId.*bounded text metadata/i,
      );
    }
  });
});

describe("production contract admission boundary", () => {
  it("uses the same synchronous binding boundary for every production action", async () => {
    process.env.AWS_REGION = "eu-west-1";
    process.env.AWS_DEFAULT_REGION = "eu-west-1";
    const { createRuntimeInvocationAdmission } = require("./agentcore-contract");

    let finishWarmup;
    const trace = [];
    const admission = createRuntimeInvocationAdmission({
      handlers: {
        status: () => trace.push("status"),
        warmup: () =>
          new Promise((resolve) => {
            trace.push("warmup-start");
            finishWarmup = resolve;
          }),
        chat: () => trace.push("chat"),
        snapshot: () => trace.push("snapshot"),
      },
    });

    const pending = admission.handle(payload("warmup"));
    assert.deepEqual(trace, ["warmup-start"]);
    for (const action of ["status", "warmup", "chat", "snapshot"]) {
      assert.throws(
        () =>
          admission.handle(
            payload(action, {
              internalUserId: "user_B",
              namespace: "user_B",
            }),
          ),
        (error) => {
          assert.equal(error.code, "SESSION_IDENTITY_MISMATCH");
          return true;
        },
      );
    }
    assert.deepEqual(trace, ["warmup-start"]);
    finishWarmup("ready");
    assert.equal(await pending, "ready");
  });

  it("hashes only a bound chat snapshot before registry execution", async () => {
    process.env.AWS_REGION = "eu-west-1";
    process.env.AWS_DEFAULT_REGION = "eu-west-1";
    const {
      createRuntimeInvocationAdmission,
      hashBoundInvocation,
    } = require("./agentcore-contract");
    const { createTrustedInvocationRegistry } = require("./gateway-invocation");
    const invocationId = `po1_${"a".repeat(64)}`;
    const admission = createRuntimeInvocationAdmission();
    const bound = admission.handle(
      payload("chat", { invocationId, message: "check my workspace" }),
    );
    const trusted = hashBoundInvocation(bound);
    const registry = createTrustedInvocationRegistry();
    let executions = 0;

    const result = await registry.invoke({
      invocationId: bound.request.invocationId,
      requestHash: trusted.requestHash,
      execute: async () => {
        executions += 1;
        return "ok";
      },
    });

    assert.equal(result, "ok");
    assert.equal(executions, 1);
    assert.match(trusted.gatewayRunId, /^[0-9a-f-]{36}$/);
    assert.match(trusted.requestHash, /^[0-9a-f]{64}$/);
    assert.throws(
      () =>
        admission.handle(
          payload("chat", {
            internalUserId: "user_B",
            namespace: "user_B",
            invocationId: `po1_${"b".repeat(64)}`,
            message: "steal workspace",
          }),
        ),
      (error) => {
        assert.equal(error.code, "SESSION_IDENTITY_MISMATCH");
        return true;
      },
    );
    assert.equal(executions, 1);
  });

  it("admits the parsed payload before any production action mutation", () => {
    const source = fs.readFileSync(
      path.join(__dirname, "agentcore-contract.js"),
      "utf8",
    );
    const invocationStart = source.indexOf(
      'if (req.method === "POST" && req.url === "/invocations")',
    );
    const invocationSource = source.slice(invocationStart);
    const parseIndex = invocationSource.indexOf("JSON.parse(body)");
    const admissionIndex = invocationSource.indexOf(
      "runtimeInvocationHandler.handle(payload)",
      parseIndex,
    );

    assert.ok(parseIndex >= 0);
    assert.ok(admissionIndex > parseIndex);
    for (const mutation of [
      "await checkProxyHealth()",
      "lastActivityTime =",
      "trustedInvocationRegistry.invoke(",
      "init(identity.internalUserId",
    ]) {
      assert.ok(
        invocationSource.indexOf(mutation, parseIndex) > admissionIndex,
        `${mutation} must occur only after production admission`,
      );
    }
    assert.doesNotMatch(invocationSource, /payload\.(?:userId|sessionId)/);
    assert.doesNotMatch(source, /current-identity|identity\.json/i);
  });

  it("ships the admission boundary in the frozen runtime image", () => {
    const dockerfile = fs.readFileSync(path.join(__dirname, "Dockerfile"), "utf8");
    const packageJson = JSON.parse(
      fs.readFileSync(path.join(__dirname, "package.json"), "utf8"),
    );
    const packageLock = JSON.parse(
      fs.readFileSync(path.join(__dirname, "package-lock.json"), "utf8"),
    );

    assert.match(
      dockerfile,
      /^COPY session-binding\.js \/app\/session-binding\.js$/m,
    );
    assert.match(
      dockerfile,
      /^COPY invocation-handler\.js \/app\/invocation-handler\.js$/m,
    );
    assert.match(
      dockerfile,
      /^ENV AWS_REGION=eu-west-1 AWS_DEFAULT_REGION=eu-west-1$/m,
    );
    assert.doesNotMatch(
      dockerfile,
      /require\('@aws-sdk\/(?:client-cognito-identity-provider|client-secrets-manager)'\)/,
    );
    for (const dependency of [
      "@aws-sdk/client-cognito-identity-provider",
      "@aws-sdk/client-secrets-manager",
    ]) {
      assert.equal(dependency in packageJson.dependencies, false);
      assert.equal(dependency in packageLock.packages[""].dependencies, false);
      assert.equal(`node_modules/${dependency}` in packageLock.packages, false);
    }
  });
});
