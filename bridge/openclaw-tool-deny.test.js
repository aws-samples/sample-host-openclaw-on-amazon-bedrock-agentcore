/**
 * Guards the OpenClaw tool deny list written by writeOpenClawConfig().
 *
 * The container runs OpenClaw headless (`channels: {}`) and has no Telegram /
 * Slack credentials. Replies reach the user only through the bridge: the
 * contract returns the model's final reply text and the router / cron Lambda
 * delivers it. OpenClaw 2.0 exposes channel-delivery tools (`message`,
 * `conversations_send`, `conversations_turn`); if the model calls one of them
 * (e.g. to "send" a scheduled-brief summary to Telegram) the send fails with
 * `missing_token` and the summary never appears in the reply text, so the
 * user gets nothing. These tools must stay denied.
 *
 * The contract is a server entry point and cannot be required in a test, so
 * this checks the source text of the tools.deny block directly (same pattern
 * as proxy-guardrail-env.test.js).
 * Run: cd bridge && node --test openclaw-tool-deny.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

const contractSrc = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf-8");

function writeOpenClawConfigSrc() {
  const start = contractSrc.indexOf("function writeOpenClawConfig()");
  assert.notEqual(start, -1, "agentcore-contract.js must define writeOpenClawConfig()");
  // Next top-level function declaration ends the body.
  const end = contractSrc.indexOf("\nfunction ", start + 1);
  return contractSrc.slice(start, end === -1 ? undefined : end);
}

function denyList() {
  const body = writeOpenClawConfigSrc();
  const m = body.match(/\bdeny:\s*\[([\s\S]*?)\]/);
  assert.ok(m, "writeOpenClawConfig must set tools.deny");
  // Strip line comments, then collect the quoted tool names.
  const entries = m[1].replace(/\/\/.*$/gm, "");
  return [...entries.matchAll(/"([^"]+)"/g)].map((x) => x[1]);
}

describe("OpenClaw tool deny list", () => {
  it("config stays headless (no OpenClaw channels)", () => {
    assert.match(writeOpenClawConfigSrc(), /channels:\s*\{\s*\}/);
  });

  for (const tool of ["message", "conversations_send", "conversations_turn"]) {
    it(`denies channel-delivery tool "${tool}" so replies return via the bridge`, () => {
      assert.ok(denyList().includes(tool), `tools.deny must include "${tool}"`);
    });
  }

  it("keeps the pre-existing denials", () => {
    const deny = denyList();
    for (const tool of ["write", "edit", "read", "browser", "cron", "gateway", "heartbeat_respond"]) {
      assert.ok(deny.includes(tool), `tools.deny must still include "${tool}"`);
    }
  });

  it("does not deny exec (skills run through it)", () => {
    assert.ok(!denyList().includes("exec"));
  });

  it("AGENTS.md tells the model its reply text is delivered automatically", () => {
    assert.match(
      writeOpenClawConfigSrc(),
      /final reply text is delivered to the user automatically/i,
    );
  });
});
