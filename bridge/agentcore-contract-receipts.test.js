"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

process.env.AWS_REGION = "eu-west-1";
process.env.AWS_DEFAULT_REGION = "eu-west-1";

const contractModule = require("./agentcore-contract");
const { createTrustedInvocationRegistry } = require("./gateway-invocation");

const contractSource = fs.readFileSync(
  path.join(__dirname, "agentcore-contract.js"),
  "utf8",
);

function committedHead(overrides = {}) {
  return {
    generation: "g-123e4567-e89b-42d3-a456-426614174000",
    manifestSha256: "a".repeat(64),
    parent: null,
    ...overrides,
  };
}

describe("immutable workspace receipts", () => {
  it("returns only the committed generation and manifest digest as frozen data", () => {
    const receipt = contractModule.createWorkspaceReceipt(committedHead());

    assert.deepEqual(receipt, {
      generation: "g-123e4567-e89b-42d3-a456-426614174000",
      manifestSha256: "a".repeat(64),
    });
    assert.equal(Object.isFrozen(receipt), true);
    assert.throws(() => {
      receipt.generation = "g-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    }, TypeError);
    assert.throws(
      () => contractModule.createWorkspaceReceipt(committedHead({ generation: "bad" })),
      /generation/i,
    );
    assert.throws(
      () =>
        contractModule.createWorkspaceReceipt(
          committedHead({ manifestSha256: "bad" }),
        ),
      /manifest/i,
    );
  });

  it("attaches a receipt after persistence for successful, failed, and uncertain work", async () => {
    for (const status of [undefined, "failed", "uncertain"]) {
      let commitCalls = 0;
      const outcome = await contractModule.persistWorkspaceOutcome({
        workspaceLifecycle: {
          commitAfterTurn: async (turn) => {
            commitCalls += 1;
            assert.equal(turn, "turn-1");
            return committedHead();
          },
        },
        workspaceTurn: "turn-1",
        outcome: {
          responseText: "bounded response",
          ...(status ? { status } : {}),
        },
      });

      assert.equal(commitCalls, 1);
      assert.equal(outcome.status, status);
      assert.deepEqual(outcome.workspaceReceipt, {
        generation: committedHead().generation,
        manifestSha256: committedHead().manifestSha256,
      });
      assert.equal(Object.isFrozen(outcome), true);
      assert.equal(Object.isFrozen(outcome.workspaceReceipt), true);
    }
  });

  it("returns no outcome or receipt when persistence fails", async () => {
    const original = Object.freeze({
      responseText: "unpersisted response",
      status: "failed",
    });

    await assert.rejects(
      contractModule.persistWorkspaceOutcome({
        workspaceLifecycle: {
          commitAfterTurn: async () => {
            const error = new Error("commit rejected");
            error.code = "WORKSPACE_PERSISTENCE_FAILED";
            throw error;
          },
        },
        workspaceTurn: "turn-1",
        outcome: original,
      }),
      /commit rejected/,
    );
    assert.equal("workspaceReceipt" in original, false);
  });

  it("replays the exact settled outcome and its receipt without re-executing", async () => {
    const registry = createTrustedInvocationRegistry();
    const invocationId = `po1_${"6".repeat(64)}`;
    const requestHash = "7".repeat(64);
    let executions = 0;
    const execute = async () => {
      executions += 1;
      return contractModule.persistWorkspaceOutcome({
        workspaceLifecycle: {
          commitAfterTurn: async () => committedHead(),
        },
        workspaceTurn: "turn-1",
        outcome: { responseText: "once", status: "uncertain" },
      });
    };

    const first = await registry.invoke({ invocationId, requestHash, execute });
    const replay = await registry.invoke({
      invocationId,
      requestHash,
      execute: async () => assert.fail("a replay must not execute again"),
    });

    assert.equal(executions, 1);
    assert.strictEqual(replay, first);
    assert.strictEqual(replay.workspaceReceipt, first.workspaceReceipt);
  });

  it("uses the persisted outcome in the production chat response", () => {
    const chatStart = contractSource.indexOf('if (action === "chat")');
    const unknownStart = contractSource.indexOf("// Unknown action", chatStart);
    const chatBranch = contractSource.slice(chatStart, unknownStart);
    const execute = chatBranch.indexOf("gatewayRuntimeBoundary.invoke");
    const persist = chatBranch.indexOf("persistWorkspaceOutcome", execute);
    const respond = chatBranch.lastIndexOf("res.end(");

    assert.ok(chatStart >= 0 && unknownStart > chatStart);
    assert.ok(execute >= 0 && persist > execute && respond > persist);
    assert.match(
      chatBranch,
      /persistWorkspaceOutcome\(\{\s*workspaceLifecycle,\s*workspaceTurn,/s,
    );
    assert.match(chatBranch, /workspaceReceipt/);
  });
});

describe("trusted snapshot invocation", () => {
  it("is admitted by the same bound identity action allowlist", () => {
    const admission = contractModule.createRuntimeInvocationAdmission();
    const bound = admission.handle({
      action: "snapshot",
      internalUserId: "user_A",
      namespace: "user_A",
    });

    assert.deepEqual(bound.identity, {
      internalUserId: "user_A",
      namespace: "user_A",
    });
    assert.throws(
      () =>
        admission.handle({
          action: "snapshot",
          internalUserId: "user_B",
          namespace: "user_B",
        }),
      (error) => error.code === "SESSION_IDENTITY_MISMATCH",
    );
  });

  it("initializes if necessary and returns a receipt only after a manual commit", async () => {
    const order = [];
    const result = await contractModule.executeSnapshotAction({
      initialize: async () => order.push("initialize"),
      getWorkspaceLifecycle: () => ({
        requestManualSnapshot: async () => {
          order.push("manual-commit");
          return committedHead();
        },
      }),
    });

    assert.deepEqual(order, ["initialize", "manual-commit"]);
    assert.deepEqual(result, {
      status: "snapshotted",
      workspaceReceipt: {
        generation: committedHead().generation,
        manifestSha256: committedHead().manifestSha256,
      },
    });
    assert.equal(Object.isFrozen(result), true);
    assert.equal(Object.isFrozen(result.workspaceReceipt), true);
  });

  it("ships snapshot in the documented endpoint without model execution", () => {
    const snapshotStart = contractSource.indexOf('if (action === "snapshot")');
    const chatStart = contractSource.indexOf('if (action === "chat")');
    assert.ok(snapshotStart >= 0, "snapshot endpoint branch must exist");
    assert.ok(chatStart > snapshotStart, "snapshot must dispatch before chat");
    const snapshotBranch = contractSource.slice(snapshotStart, chatStart);

    assert.match(snapshotBranch, /executeSnapshotAction/);
    assert.match(snapshotBranch, /identity\.internalUserId/);
    assert.match(snapshotBranch, /identity\.namespace/);
    assert.match(snapshotBranch, /warmModel:\s*false/);
    assert.doesNotMatch(snapshotBranch, /agent\.chat|enqueueMessage|gatewayRuntimeBoundary\.invoke/);
    assert.match(
      contractSource,
      /POST \/invocations \{action: chat\|status\|warmup\|snapshot\}/,
    );
  });
});
