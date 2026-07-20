"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { describe, it } = require("node:test");

const SHA_A = "a".repeat(64);
const NONCE = "nonce_secret_12345678";
const EXPECTED_ARGS_HASH =
  "0c35fc687acce074cbecca9153dbbe2e396d17ba22a36048662d536070117011";
const EXPECTED_CALL_ID =
  "call_f0c1e75e4f7064a6b8940078e613318fc279363bedafb0279b2b1b06c735fde9";

function loadRelay() {
  try {
    return require("./capability-relay");
  } catch {
    return null;
  }
}

function grant(overrides = {}) {
  return {
    schema: "personal-operator.turn-capability-grant.v1",
    sub: "user_alpha",
    sessionId: "session_12345678",
    runtimeArn:
      "arn:aws:bedrock-agentcore:eu-west-1:000000000000:runtime/example",
    runtimeQualifier: "release_0123456789abcdef0123456789abcdef01234567",
    invocationId: "invocation_12345678",
    releaseCommit: "0123456789abcdef0123456789abcdef01234567",
    catalogDigest: SHA_A,
    allowedPackIds: ["web.exact-read"],
    allowedOperationIds: ["web.exact.read"],
    targetGrantHashes: ["b".repeat(64)],
    iat: 1_800_000_000,
    exp: 1_800_000_300,
    maxCalls: 2,
    nonce: NONCE,
    ...overrides,
  };
}

function succeededResult(call, overrides = {}) {
  return {
    schema: "personal-operator.capability-result.v1",
    callId: call.callId,
    invocationId: call.invocationId,
    toolUseId: call.toolUseId,
    catalogDigest: call.catalogDigest,
    operationId: call.operationId,
    toolName: call.toolName,
    argsHash: call.argsHash,
    status: "SUCCEEDED",
    data: {
      canonicalUrl: call.arguments.url,
      contentDigest: "c".repeat(64),
      retrievedAt: 1_800_000_100,
      sourceRef: "source_12345678",
      text: "reviewed public text",
    },
    provenanceRefs: ["source_12345678"],
    proposalRef: null,
    receiptRef: null,
    errorCode: null,
    retryPolicy: "NONE",
    ...overrides,
  };
}

function failedResult(
  call,
  {
    status = "FAILED_RETRYABLE",
    errorCode = "SYNTHETIC_READ_UNAVAILABLE",
    retryPolicy = "SAFE_RETRY",
  } = {},
) {
  return {
    schema: "personal-operator.capability-result.v1",
    callId: call.callId,
    invocationId: call.invocationId,
    toolUseId: call.toolUseId,
    catalogDigest: call.catalogDigest,
    operationId: call.operationId,
    toolName: call.toolName,
    argsHash: call.argsHash,
    status,
    data: {},
    provenanceRefs: [],
    proposalRef: null,
    receiptRef: null,
    errorCode,
    retryPolicy,
  };
}

async function assertTypedLambdaAmbiguity(relayModule, send) {
  const cases = [
    {
      args: { url: "https://example.com/exact" },
      expectedRetry: "SAFE_RETRY",
      expectedStatus: "FAILED_RETRYABLE",
      grant: grant({ maxCalls: 1 }),
      toolName: "po_web_read",
      toolUseId: "tooluse_12345678",
    },
    {
      args: { path: "notes/a.md", content: "hello" },
      expectedRetry: "RECONCILE_ONLY",
      expectedStatus: "UNCERTAIN",
      grant: grant({
        allowedPackIds: ["workspace.file-write"],
        allowedOperationIds: ["workspace.file.write"],
        targetGrantHashes: [],
        maxCalls: 1,
      }),
      toolName: "po_file_write",
      toolUseId: "tooluse_11111111",
    },
  ];
  for (const testCase of cases) {
    let sends = 0;
    const transport = relayModule.createLambdaGatewayTransport({
      functionArn:
        "arn:aws:lambda:eu-west-1:123456789012:function:capability-gateway",
      lambdaClient: {
        async send(command) {
          sends += 1;
          return send(command);
        },
      },
    });
    const relay = new relayModule.CapabilityRelay({
      now: () => 1_800_000_100,
      gatewayTransport: transport,
    });
    relay.bind_turn(testCase.grant);

    const result = await relay.call(
      testCase.toolUseId,
      testCase.toolName,
      testCase.args,
    );

    assert.equal(result.status, testCase.expectedStatus);
    assert.equal(result.errorCode, "CAPABILITY_GATEWAY_RESPONSE_UNAVAILABLE");
    assert.equal(result.retryPolicy, testCase.expectedRetry);
    assert.equal(result.toolUseId, testCase.toolUseId);
    assert.equal(sends, 1);
  }
}

describe("trusted capability relay", () => {
  it("exists with the frozen public interface", () => {
    const relayModule = loadRelay();
    assert.ok(relayModule, "capability-relay.js must exist");
    assert.equal(typeof relayModule.CapabilityRelay, "function");
    assert.equal(typeof relayModule.createCapabilityAdapters, "function");
    assert.equal(typeof relayModule.createCapabilityRelayServer, "function");
    assert.equal(typeof relayModule.createLoopbackRelayClient, "function");
  });

  it("binds scheduled turns only to the catalog-derived read/propose surface", () => {
    const relayModule = loadRelay();
    const relay = new relayModule.CapabilityRelay({
      now: () => 1_800_000_100,
      gatewayTransport: async () => assert.fail("must not dispatch"),
    });
    for (const [allowedPackIds, allowedOperationIds] of [
      [["workspace.file-list", "workspace.file-read"], ["workspace.file.list", "workspace.file.read"]],
      [["schedule.list", "schedule.propose"], ["schedule.list", "schedule.propose"]],
      [["schedule.cancel-propose"], ["schedule.cancel.propose"]],
    ]) {
      relay.bind_scheduled_turn(grant({
        allowedPackIds,
        allowedOperationIds,
        targetGrantHashes: [],
      }));
      relay.clear_turn();
    }
  });

  it("rejects scheduled target grants and exact-target reads before transport", async () => {
    const relayModule = loadRelay();
    let transportCalls = 0;
    const relay = new relayModule.CapabilityRelay({
      now: () => 1_800_000_100,
      gatewayTransport: async () => {
        transportCalls += 1;
        assert.fail("scheduled target authority must not dispatch");
      },
    });

    for (const scheduledGrant of [
      grant({
        allowedPackIds: ["web.exact-read"],
        allowedOperationIds: ["web.exact.read"],
        targetGrantHashes: [],
      }),
      grant({
        allowedPackIds: ["schedule.list"],
        allowedOperationIds: ["schedule.list"],
      }),
    ]) {
      assert.throws(
        () => relay.bind_scheduled_turn(scheduledGrant),
        (error) => error.code === "CAPABILITY_SCHEDULED_EFFECT_DENIED",
      );
    }

    await assert.rejects(
      relay.call("tooluse_12345678", "po_web_read", {
        url: "https://example.com/exact",
      }),
      /No capability turn is bound/,
    );
    assert.equal(transportCalls, 0);
  });

  it("fails closed before binding any scheduled mutation, compute, or effect grant", async () => {
    const relayModule = loadRelay();
    const relay = new relayModule.CapabilityRelay({
      now: () => 1_800_000_100,
      gatewayTransport: async () => assert.fail("must not dispatch"),
    });
    for (const [allowedPackIds, allowedOperationIds] of [
      [["workspace.file-write"], ["workspace.file.write"]],
      [["workspace.file-delete"], ["workspace.file.delete"]],
      [["compute.run"], ["compute.run"]],
      [["compute.status"], ["compute.status"]],
    ]) {
      assert.throws(
        () => relay.bind_scheduled_turn(grant({
          allowedPackIds,
          allowedOperationIds,
          targetGrantHashes: [],
        })),
        (error) => {
          assert.equal(error.code, "CAPABILITY_SCHEDULED_EFFECT_DENIED");
          return true;
        },
      );
      await assert.rejects(
        relay.call("tooluse_12345678", "po_web_read", { url: "https://example.com/exact" }),
        /No capability turn is bound/,
      );
    }
  });

  it("derives the Python-compatible deterministic call and injects only server authority", async () => {
    const relayModule = loadRelay();
    const envelopes = [];
    const relay = new relayModule.CapabilityRelay({
      now: () => 1_800_000_100,
      gatewayTransport: async (envelope) => {
        envelopes.push(envelope);
        return succeededResult(envelope.call);
      },
    });
    const mutableGrant = grant();
    relay.bind_turn(mutableGrant);
    mutableGrant.nonce = "attacker_replaced_nonce";
    mutableGrant.allowedOperationIds.push("compute.run");

    const result = await relay.call(
      "tooluse_12345678",
      "po_web_read",
      { url: "https://example.com/exact" },
    );

    assert.equal(envelopes.length, 1);
    assert.deepEqual(Object.keys(envelopes[0]), ["schema", "grant", "call"]);
    assert.equal(
      envelopes[0].schema,
      "personal-operator.capability-relay-envelope.v1",
    );
    assert.equal(envelopes[0].grant.nonce, NONCE);
    assert.deepEqual(envelopes[0].grant.allowedOperationIds, ["web.exact.read"]);
    assert.equal(envelopes[0].call.argsHash, EXPECTED_ARGS_HASH);
    assert.equal(envelopes[0].call.callId, EXPECTED_CALL_ID);
    assert.deepEqual(result, succeededResult(envelopes[0].call));
    assert.equal(Object.isFrozen(envelopes[0]), true);
    assert.equal(Object.isFrozen(envelopes[0].grant), true);
    assert.equal("grant" in relay, false);
    assert.doesNotMatch(JSON.stringify(relay), /nonce_secret|targetGrantHashes/);
  });

  it("deduplicates exact retries and rejects mutation, quota, unknown, and expired calls before transport", async () => {
    const relayModule = loadRelay();
    let transportCalls = 0;
    const relay = new relayModule.CapabilityRelay({
      now: () => 1_800_000_100,
      gatewayTransport: async ({ call }) => {
        transportCalls += 1;
        return succeededResult(call);
      },
    });
    relay.bind_turn(grant({ maxCalls: 1 }));
    const args = { url: "https://example.com/exact" };

    const first = await relay.call("tooluse_12345678", "po_web_read", args);
    const retry = await relay.call("tooluse_12345678", "po_web_read", args);
    assert.deepEqual(retry, first);
    assert.equal(transportCalls, 1);
    await assert.rejects(
      () => relay.call("tooluse_12345678", "po_web_read", {
        url: "https://example.com/changed",
      }),
      (error) => error.code === "CAPABILITY_ARGUMENT_MUTATION",
    );
    await assert.rejects(
      () => relay.call("tooluse_87654321", "po_web_read", args),
      (error) => error.code === "CAPABILITY_CALL_BUDGET_EXCEEDED",
    );
    await assert.rejects(
      () => relay.call("tooluse_87654321", "po_not_real", {}),
      (error) => error.code === "CAPABILITY_TOOL_UNKNOWN",
    );
    assert.equal(transportCalls, 1);

    const expired = new relayModule.CapabilityRelay({
      now: () => 1_800_000_301,
      gatewayTransport: async () => {
        throw new Error("must not dispatch");
      },
    });
    expired.bind_turn(grant());
    await assert.rejects(
      () => expired.call("tooluse_12345678", "po_web_read", args),
      (error) => error.code === "CAPABILITY_GRANT_EXPIRED",
    );
  });

  it("maps lost mutation delivery to typed uncertainty and fences a fresh tool-use ID", async () => {
    const relayModule = loadRelay();
    let transportCalls = 0;
    const relay = new relayModule.CapabilityRelay({
      now: () => 1_800_000_100,
      gatewayTransport: async () => {
        transportCalls += 1;
        throw new Error("synthetic post-send response loss");
      },
    });
    relay.bind_turn(
      grant({
        allowedPackIds: ["workspace.file-write"],
        allowedOperationIds: ["workspace.file.write"],
        targetGrantHashes: [],
      }),
    );
    const args = { path: "notes/a.md", content: "hello" };

    const ambiguous = await relay.call(
      "tooluse_11111111",
      "po_file_write",
      args,
    );
    const fenced = await relay.call(
      "tooluse_22222222",
      "po_file_write",
      args,
    );

    assert.equal(ambiguous.status, "UNCERTAIN");
    assert.equal(ambiguous.errorCode, "CAPABILITY_GATEWAY_RESPONSE_UNAVAILABLE");
    assert.equal(ambiguous.retryPolicy, "RECONCILE_ONLY");
    assert.equal(fenced.status, "UNCERTAIN");
    assert.equal(fenced.errorCode, "CAPABILITY_LOGICAL_EFFECT_UNCERTAIN");
    assert.equal(fenced.toolUseId, "tooluse_22222222");
    assert.equal(fenced.retryPolicy, "RECONCILE_ONLY");
    assert.equal(transportCalls, 1);
  });

  it("retries only the same validated read envelope once without recharging the call budget", async () => {
    const relayModule = loadRelay();
    const envelopes = [];
    const relay = new relayModule.CapabilityRelay({
      now: () => 1_800_000_100,
      gatewayTransport: async (envelope) => {
        envelopes.push(envelope);
        if (envelopes.length === 1) return failedResult(envelope.call);
        return succeededResult(envelope.call);
      },
    });
    relay.bind_turn(grant({ maxCalls: 1 }));
    const args = { url: "https://example.com/exact" };

    const retryable = await relay.call(
      "tooluse_12345678",
      "po_web_read",
      args,
    );
    const succeeded = await relay.call(
      "tooluse_12345678",
      "po_web_read",
      args,
    );
    const replay = await relay.call(
      "tooluse_12345678",
      "po_web_read",
      args,
    );

    assert.equal(retryable.status, "FAILED_RETRYABLE");
    assert.equal(succeeded.status, "SUCCEEDED");
    assert.deepEqual(replay, succeeded);
    assert.equal(envelopes.length, 2);
    assert.strictEqual(envelopes[1], envelopes[0]);
  });

  it("turns a lost read response into a validated retryable result before exact retry", async () => {
    const relayModule = loadRelay();
    let transportCalls = 0;
    const relay = new relayModule.CapabilityRelay({
      now: () => 1_800_000_100,
      gatewayTransport: async ({ call }) => {
        transportCalls += 1;
        if (transportCalls === 1) {
          throw new Error("synthetic read response loss");
        }
        return succeededResult(call);
      },
    });
    relay.bind_turn(grant({ maxCalls: 1 }));
    const args = { url: "https://example.com/exact" };

    const lost = await relay.call(
      "tooluse_12345678",
      "po_web_read",
      args,
    );
    const retried = await relay.call(
      "tooluse_12345678",
      "po_web_read",
      args,
    );

    assert.equal(lost.status, "FAILED_RETRYABLE");
    assert.equal(lost.errorCode, "CAPABILITY_GATEWAY_RESPONSE_UNAVAILABLE");
    assert.equal(lost.retryPolicy, "SAFE_RETRY");
    assert.equal(retried.status, "SUCCEEDED");
    assert.equal(transportCalls, 2);
  });

  it("types Lambda FunctionError responses after dispatch", async () => {
    const relayModule = loadRelay();
    await assertTypedLambdaAmbiguity(relayModule, async () => ({
      FunctionError: "Unhandled",
      Payload: Buffer.from('{"errorMessage":"hidden"}'),
    }));
  });

  it("types absent Lambda payloads after dispatch", async () => {
    const relayModule = loadRelay();
    await assertTypedLambdaAmbiguity(relayModule, async () => ({}));
  });

  it("types malformed Lambda payloads after dispatch", async () => {
    const relayModule = loadRelay();
    await assertTypedLambdaAmbiguity(relayModule, async () => ({
      Payload: Buffer.from("{"),
    }));
  });

  it("types schema-invalid Lambda payloads after dispatch", async () => {
    const relayModule = loadRelay();
    await assertTypedLambdaAmbiguity(relayModule, async () => ({
      Payload: Buffer.from("{}"),
    }));
  });

  it("types ambiguous Lambda SDK rejections after dispatch", async () => {
    const relayModule = loadRelay();
    await assertTypedLambdaAmbiguity(relayModule, async () => {
      throw new Error("synthetic connection reset after request write");
    });
  });

  it("caps read dispatch at two attempts and blocks fresh-tool retry bypass", async () => {
    const relayModule = loadRelay();
    let transportCalls = 0;
    const relay = new relayModule.CapabilityRelay({
      now: () => 1_800_000_100,
      gatewayTransport: async ({ call }) => {
        transportCalls += 1;
        return failedResult(call);
      },
    });
    relay.bind_turn(grant());
    const args = { url: "https://example.com/exact" };

    const first = await relay.call(
      "tooluse_12345678",
      "po_web_read",
      args,
    );
    await assert.rejects(
      () => relay.call("tooluse_87654321", "po_web_read", args),
      (error) => error.code === "CAPABILITY_READ_RETRY_REQUIRES_SAME_CALL",
    );
    const second = await relay.call(
      "tooluse_12345678",
      "po_web_read",
      args,
    );
    const exhausted = await relay.call(
      "tooluse_12345678",
      "po_web_read",
      args,
    );

    assert.equal(first.status, "FAILED_RETRYABLE");
    assert.equal(second.status, "FAILED_RETRYABLE");
    assert.equal(exhausted.status, "DENIED");
    assert.equal(exhausted.errorCode, "CAPABILITY_READ_RETRY_EXHAUSTED");
    assert.equal(transportCalls, 2);
  });

  it("rejects local invalid input and types post-dispatch invalid results", async () => {
    const relayModule = loadRelay();
    const relay = new relayModule.CapabilityRelay({
      now: () => 1_800_000_100,
      gatewayTransport: async ({ call }) => succeededResult(call, {
        toolUseId: "tooluse_attacker",
      }),
    });
    assert.throws(
      () => relay.bind_turn({ ...grant(), unexpected: true }),
      (error) => error.code === "CAPABILITY_GRANT_INVALID",
    );
    relay.bind_turn(grant());
    await assert.rejects(
      () => relay.call("tooluse_12345678", "po_web_read", {
        url: "https://example.com/exact",
        unexpected: true,
      }),
      (error) => error.code === "CAPABILITY_ARGUMENTS_INVALID",
    );
    const mismatched = await relay.call(
      "tooluse_12345678",
      "po_web_read",
      {
        url: "https://example.com/exact",
      },
    );
    assert.equal(mismatched.status, "FAILED_RETRYABLE");
    assert.equal(
      mismatched.errorCode,
      "CAPABILITY_GATEWAY_RESPONSE_UNAVAILABLE",
    );
    assert.equal(mismatched.retryPolicy, "SAFE_RETRY");

    const leaking = new relayModule.CapabilityRelay({
      now: () => 1_800_000_100,
      gatewayTransport: async ({ call }) => succeededResult(call, {
        data: {
          ...succeededResult(call).data,
          text: `do not expose ${NONCE}`,
        },
      }),
    });
    leaking.bind_turn(grant());
    const redacted = await leaking.call(
      "tooluse_12345678",
      "po_web_read",
      {
        url: "https://example.com/exact",
      },
    );
    assert.equal(redacted.status, "FAILED_RETRYABLE");
    assert.equal(redacted.errorCode, "CAPABILITY_GATEWAY_RESPONSE_UNAVAILABLE");
    assert.doesNotMatch(JSON.stringify(redacted), new RegExp(NONCE));
  });

  it("keeps grants and relay tokens out of child env, model data, workspace, results, and logs", async () => {
    const relayModule = loadRelay();
    const runtimePolicy = require("./runtime-policy");
    const logs = [];
    const relay = new relayModule.CapabilityRelay({
      now: () => 1_800_000_100,
      logger: (event) => logs.push(event),
      gatewayTransport: async ({ call }) => succeededResult(call),
    });
    relay.bind_turn(grant());

    const childEnv = runtimePolicy.buildOpenClawChildEnv({
      workspacePrefix: "user_alpha",
      scopedEnv: {
        AWS_REGION: "eu-west-1",
        AWS_DEFAULT_REGION: "eu-west-1",
        AWS_EC2_METADATA_DISABLED: "true",
        AWS_SHARED_CREDENTIALS_FILE: "/dev/null",
        AWS_CONFIG_FILE: "/tmp/scoped/config",
        AWS_SDK_LOAD_CONFIG: "1",
        S3_USER_FILES_BUCKET: "files",
        PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE: "/tmp/scoped/credentials",
        TURN_CAPABILITY_GRANT: NONCE,
        CAPABILITY_RELAY_TOKEN: NONCE,
      },
    });
    const modelMessage = { role: "user", content: "read the exact URL" };
    const toolArguments = { url: "https://example.com/exact" };
    const result = await relay.call(
      "tooluse_12345678",
      "po_web_read",
      toolArguments,
    );
    const workspace = fs.mkdtempSync(path.join(os.tmpdir(), "po-relay-workspace-"));
    try {
      fs.writeFileSync(path.join(workspace, "note.txt"), "ordinary user content");
      for (const visible of [
        childEnv,
        modelMessage,
        toolArguments,
        result,
        logs,
        fs.readFileSync(path.join(workspace, "note.txt"), "utf8"),
      ]) {
        assert.doesNotMatch(JSON.stringify(visible), new RegExp(NONCE));
        assert.doesNotMatch(JSON.stringify(visible), /CAPABILITY_RELAY_TOKEN/);
      }
    } finally {
      fs.rmSync(workspace, { recursive: true, force: true });
    }
  });
});

describe("private relay boundary", () => {
  it("accepts only loopback and sends no authority from the child request", async () => {
    const relayModule = loadRelay();
    assert.throws(
      () => relayModule.createCapabilityRelayServer({
        relay: { call: async () => ({}) },
        host: "0.0.0.0",
      }),
      /loopback/i,
    );
    assert.throws(
      () => relayModule.createLoopbackRelayClient({ host: "localhost" }),
      /loopback/i,
    );

    const received = [];
    const expected = Object.freeze({ status: "DENIED", errorCode: "PACK_DISABLED" });
    const server = relayModule.createCapabilityRelayServer({
      host: "127.0.0.1",
      port: 0,
      relay: {
        async call(toolUseId, toolName, args) {
          received.push({ toolUseId, toolName, args });
          return expected;
        },
      },
    });
    await server.listen();
    try {
      const client = relayModule.createLoopbackRelayClient({
        host: "127.0.0.1",
        port: server.address().port,
      });
      assert.deepEqual(
        await client.call("tooluse_12345678", "po_web_read", {
          url: "https://example.com/exact",
        }),
        expected,
      );
      assert.deepEqual(received, [{
        toolUseId: "tooluse_12345678",
        toolName: "po_web_read",
        args: { url: "https://example.com/exact" },
      }]);
      assert.doesNotMatch(JSON.stringify(received), /grant|nonce|token/i);
    } finally {
      await server.close();
    }
  });
});
