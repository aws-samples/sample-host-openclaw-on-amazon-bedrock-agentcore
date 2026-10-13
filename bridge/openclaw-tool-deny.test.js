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
 * Test the shared renderer without starting the contract server.
 * Run: cd bridge && node --test openclaw-tool-deny.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { renderConfig, renderAgents } = require("./gateway-config");

function denyList() {
  return renderConfig({ gatewayToken: "test-token" }).tools.deny;
}

describe("OpenClaw tool deny list", () => {
  it("config stays headless (no OpenClaw channels)", () => {
    assert.deepEqual(renderConfig({ gatewayToken: "test-token" }).channels, {});
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
      renderAgents({ browserEnabled: true }),
      /final reply text is delivered to the user automatically/i,
    );
  });
});
