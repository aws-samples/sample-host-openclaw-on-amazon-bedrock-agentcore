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

  // OpenClaw 2026.11.2 added `theme` and `presence`; denying them keeps the agent's
  // tool surface the same as on 2026.9.5. Any other change to this list must be deliberate.
  it("denies exactly the reviewed tool list", () => {
    assert.deepEqual(denyList(), [
      "write", "edit", "apply_patch", "read", "browser", "canvas", "cron", "gateway",
      "terminal", "process", "plugins", "ask_user", "secrets", "screen", "progress_card", "nodes",
      "heartbeat_respond", "image_generate", "music_generate", "video_generate", "tts",
      "message", "conversations_send", "conversations_turn",
      "theme", "presence",
    ]);
  });

  it("keeps tool search off so every request carries the full tool list", () => {
    const { tools } = renderConfig({ gatewayToken: "test-token" });
    assert.equal(tools.toolSearch, false);
    assert.equal(tools.profile, "full");
    assert.deepEqual(Object.keys(tools).sort(), ["deny", "exec", "profile", "toolSearch"]);
  });

  it("AGENTS.md tells the model its reply text is delivered automatically", () => {
    assert.match(
      renderAgents({ browserEnabled: true }),
      /final reply text is delivered to the user automatically/i,
    );
  });
});
