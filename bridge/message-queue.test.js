/**
 * Tests for message queue serialization logic from agentcore-contract.js.
 * Run: cd bridge && node --test message-queue.test.js
 *
 * Since enqueueMessage/processQueue are not exported (inline in contract module),
 * we mirror the core queue logic here. Changes to the contract must be mirrored.
 */
const { describe, it, beforeEach } = require("node:test");
const assert = require("node:assert/strict");

// --- Mirror of queue logic from agentcore-contract.js ---

const MAX_QUEUE_DEPTH = 10;
const QUEUE_WAIT_TIMEOUT_MS = 90000;

let messageQueue;
let bridgeInProgress;
let shuttingDown;
let mockBridgeMessage;

function resetState() {
  messageQueue = [];
  bridgeInProgress = false;
  shuttingDown = false;
  mockBridgeMessage = null;
}

function enqueueMessage(message, timeoutMs = 120000) {
  if (shuttingDown) {
    return Promise.resolve(
      "The system is restarting. Please resend your message in a moment.",
    );
  }
  if (messageQueue.length >= MAX_QUEUE_DEPTH) {
    return Promise.resolve(
      "Too many messages queued. Please wait for a response before sending more.",
    );
  }
  return new Promise((resolve) => {
    const entry = { message, timeoutMs, resolve, enqueuedAt: Date.now() };
    messageQueue.push(entry);
    if (!bridgeInProgress) {
      processQueue();
    }
  });
}

async function processQueue() {
  if (bridgeInProgress) return;
  bridgeInProgress = true;
  try {
    while (messageQueue.length > 0) {
      const now = Date.now();
      const batch = [];
      while (messageQueue.length > 0) {
        const entry = messageQueue.shift();
        if (now - entry.enqueuedAt > QUEUE_WAIT_TIMEOUT_MS) {
          entry.resolve(
            "Your message timed out while waiting in the queue. Please try again.",
          );
          continue;
        }
        batch.push(entry);
      }

      if (batch.length === 0) continue;

      let combinedMessage;
      let bridgeTimeout;
      if (batch.length === 1) {
        combinedMessage = batch[0].message;
        bridgeTimeout = batch[0].timeoutMs;
      } else {
        const parts = batch.map(
          (e, i) => `[Message ${i + 1}/${batch.length}]: ${e.message}`,
        );
        combinedMessage = parts.join("\n\n---\n\n");
        bridgeTimeout = Math.max(...batch.map((e) => e.timeoutMs));
      }

      let responseText;
      try {
        responseText = await mockBridgeMessage(combinedMessage, bridgeTimeout);
      } catch (err) {
        responseText = `Bridge error: ${err.message}`;
      }

      batch[0].resolve(responseText);
      for (let i = 1; i < batch.length; i++) {
        batch[i].resolve("");
      }
    }
  } finally {
    bridgeInProgress = false;
    if (messageQueue.length > 0) {
      processQueue();
    }
  }
}

// --- Tests ---

describe("Message Queue Serialization", () => {
  beforeEach(() => {
    resetState();
  });

  it("single message passes through immediately", async () => {
    mockBridgeMessage = async (msg) => `Response to: ${msg}`;
    const result = await enqueueMessage("Hello");
    assert.equal(result, "Response to: Hello");
    assert.equal(bridgeInProgress, false);
    assert.equal(messageQueue.length, 0);
  });

  it("second message waits for first to complete", async () => {
    const order = [];
    let resolveFirst;
    const firstBlocks = new Promise((r) => {
      resolveFirst = r;
    });

    mockBridgeMessage = async (msg) => {
      if (msg === "first") {
        order.push("bridge-first-start");
        await firstBlocks;
        order.push("bridge-first-end");
        return "Response 1";
      }
      order.push("bridge-second");
      return "Response 2";
    };

    const p1 = enqueueMessage("first");
    // Allow processQueue to start (it's async, kicks off on enqueue)
    await new Promise((r) => setTimeout(r, 10));

    const p2 = enqueueMessage("second");

    // First is still processing — second should be queued
    assert.equal(bridgeInProgress, true);

    // Release the first
    resolveFirst();
    const r1 = await p1;
    const r2 = await p2;

    assert.equal(r1, "Response 1");
    assert.equal(r2, "Response 2");
    assert.deepEqual(order, [
      "bridge-first-start",
      "bridge-first-end",
      "bridge-second",
    ]);
  });

  it("three rapid messages: 2+3 batched with [Message N/M] format", async () => {
    let resolveFirst;
    const firstBlocks = new Promise((r) => {
      resolveFirst = r;
    });
    const bridgeCalls = [];

    mockBridgeMessage = async (msg) => {
      bridgeCalls.push(msg);
      if (bridgeCalls.length === 1) {
        await firstBlocks;
        return "Response 1";
      }
      return "Batched response";
    };

    const p1 = enqueueMessage("msg1");
    await new Promise((r) => setTimeout(r, 10));

    const p2 = enqueueMessage("msg2");
    const p3 = enqueueMessage("msg3");

    resolveFirst();
    const [r1, r2, r3] = await Promise.all([p1, p2, p3]);

    assert.equal(r1, "Response 1");
    assert.equal(r2, "Batched response"); // First in batch gets full response
    assert.equal(r3, ""); // Remaining get empty (suppressed)

    assert.equal(bridgeCalls.length, 2);
    assert.equal(bridgeCalls[0], "msg1");
    assert.ok(bridgeCalls[1].includes("[Message 1/2]: msg2"));
    assert.ok(bridgeCalls[1].includes("[Message 2/2]: msg3"));
    assert.ok(bridgeCalls[1].includes("---"));
  });

  it("queue depth limit rejects 11th message", async () => {
    let resolveBlock;
    const block = new Promise((r) => {
      resolveBlock = r;
    });

    mockBridgeMessage = async () => {
      await block;
      return "done";
    };

    // Enqueue first message (starts processing, blocks)
    const p1 = enqueueMessage("msg0");
    await new Promise((r) => setTimeout(r, 10));

    // Fill the queue to MAX_QUEUE_DEPTH
    const promises = [];
    for (let i = 1; i <= MAX_QUEUE_DEPTH; i++) {
      promises.push(enqueueMessage(`msg${i}`));
    }

    // 11th should be rejected immediately
    const overflow = await enqueueMessage("overflow");
    assert.ok(overflow.includes("Too many messages queued"));

    // Clean up — resolve the block
    resolveBlock();
    await p1;
    await Promise.all(promises);
  });

  it("queue wait timeout drops stale messages", async () => {
    let resolveBlock;
    const block = new Promise((r) => {
      resolveBlock = r;
    });

    mockBridgeMessage = async (msg) => {
      if (msg === "first") {
        await block;
        return "Response 1";
      }
      return "Response 2";
    };

    const p1 = enqueueMessage("first");
    await new Promise((r) => setTimeout(r, 10));

    // Manually enqueue an entry with an old enqueuedAt to simulate timeout
    const staleResult = new Promise((resolve) => {
      messageQueue.push({
        message: "stale",
        timeoutMs: 120000,
        resolve,
        enqueuedAt: Date.now() - QUEUE_WAIT_TIMEOUT_MS - 1000,
      });
    });

    resolveBlock();
    const r1 = await p1;
    const rStale = await staleResult;

    assert.equal(r1, "Response 1");
    assert.ok(rStale.includes("timed out"));
  });

  it("shutdown resolves immediately with restart message", async () => {
    mockBridgeMessage = async () => "should not be called";

    shuttingDown = true;
    const result = await enqueueMessage("hello");
    assert.ok(result.includes("restarting"));
  });

  it("shutdown drains queue with restart messages", async () => {
    let resolveBlock;
    const block = new Promise((r) => {
      resolveBlock = r;
    });

    mockBridgeMessage = async () => {
      await block;
      return "done";
    };

    const p1 = enqueueMessage("msg1");
    await new Promise((r) => setTimeout(r, 10));
    const p2 = enqueueMessage("msg2");

    // Simulate SIGTERM drain
    while (messageQueue.length > 0) {
      const entry = messageQueue.shift();
      entry.resolve(
        "The system is restarting. Please resend your message in a moment.",
      );
    }

    const r2 = await p2;
    assert.ok(r2.includes("restarting"));

    // Clean up the blocked first message
    resolveBlock();
    await p1;
  });

  it("bridgeMessage error resolves all batch entries", async () => {
    let resolveBlock;
    const block = new Promise((r) => {
      resolveBlock = r;
    });
    let callCount = 0;

    mockBridgeMessage = async () => {
      callCount++;
      if (callCount === 1) {
        await block;
        return "Response 1";
      }
      throw new Error("WebSocket failed");
    };

    const p1 = enqueueMessage("msg1");
    await new Promise((r) => setTimeout(r, 10));

    const p2 = enqueueMessage("msg2");
    const p3 = enqueueMessage("msg3");

    resolveBlock();
    const [r1, r2, r3] = await Promise.all([p1, p2, p3]);

    assert.equal(r1, "Response 1");
    assert.ok(r2.includes("Bridge error: WebSocket failed"));
    assert.equal(r3, ""); // Suppressed (batched)
  });
});
