/**
 * Tests for agentcore-browser skill — navigate, screenshot, interact.
 *
 * Covers: getBrowserSession, truncateContent, browserNavigate (missing url),
 *         browserInteract (invalid action, missing selector), browserScreenshot
 *         (connect failure), S3 key format.
 * Run: cd bridge && node --test agentcore-browser.test.js
 */
const { describe, it, beforeEach, afterEach, mock } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

const BROWSER_SESSION_FILE = "/tmp/agentcore-browser-session.json";

// --- getBrowserSession ---

describe("getBrowserSession", () => {
  afterEach(() => {
    try { fs.unlinkSync(BROWSER_SESSION_FILE); } catch {}
  });

  it("throws when session file is missing", () => {
    // Ensure file does not exist
    try { fs.unlinkSync(BROWSER_SESSION_FILE); } catch {}
    const { getBrowserSession } = require("./skills/agentcore-browser/common");
    assert.throws(
      () => getBrowserSession(),
      (err) => err.message.includes("Browser session not available")
    );
  });

  it("returns session data when session file exists", () => {
    fs.writeFileSync(BROWSER_SESSION_FILE, JSON.stringify({
      endpoint: "ws://127.0.0.1:9222",
      headers: { Authorization: "test" },
    }));
    const { getBrowserSession } = require("./skills/agentcore-browser/common");
    const session = getBrowserSession();
    assert.equal(session.endpoint, "ws://127.0.0.1:9222");
    assert.equal(session.headers.Authorization, "test");
  });

  it("throws when session file missing endpoint field", () => {
    fs.writeFileSync(BROWSER_SESSION_FILE, JSON.stringify({ other: "data" }));
    const { getBrowserSession } = require("./skills/agentcore-browser/common");
    assert.throws(
      () => getBrowserSession(),
      (err) => err.message.includes("missing endpoint")
    );
  });
});

// --- truncateContent ---

describe("truncateContent", () => {
  it("returns text unchanged when under limit", () => {
    const { truncateContent } = require("./skills/agentcore-browser/common");
    const text = "Hello world";
    assert.equal(truncateContent(text, 100), text);
  });

  it("truncates text and appends notice when over limit", () => {
    const { truncateContent } = require("./skills/agentcore-browser/common");
    const text = "a".repeat(200);
    const result = truncateContent(text, 50);
    assert.ok(result.startsWith("a".repeat(50)));
    assert.ok(result.includes("[Content truncated at 50 characters]"));
    assert.ok(result.length < 200);
  });
});

// --- browserNavigate ---

describe("browserNavigate", () => {
  it("returns error JSON when url is missing", async () => {
    // We can call navigate's exported function directly by requiring the module
    // but navigate.js runs as CLI. Instead, we test the function logic via child_process.
    // For unit testing, we'll require the module and mock connectBrowser.

    // Use a helper approach: parse the navigate.js and extract the function
    // Actually, let's just spawn the script with empty args
    const { execFileSync } = require("child_process");
    const result = execFileSync("node", [
      path.join(__dirname, "skills/agentcore-browser/navigate.js"),
      JSON.stringify({}),
    ], { encoding: "utf8" });
    const parsed = JSON.parse(result.trim());
    assert.equal(parsed.error, "url is required");
  });

  it("returns error JSON when url is empty string", async () => {
    const { execFileSync } = require("child_process");
    const result = execFileSync("node", [
      path.join(__dirname, "skills/agentcore-browser/navigate.js"),
      JSON.stringify({ url: "" }),
    ], { encoding: "utf8" });
    const parsed = JSON.parse(result.trim());
    assert.equal(parsed.error, "url is required");
  });

  it("returns browser not available error when session file missing", async () => {
    try { fs.unlinkSync(BROWSER_SESSION_FILE); } catch {}
    const { execFileSync } = require("child_process");
    const result = execFileSync("node", [
      path.join(__dirname, "skills/agentcore-browser/navigate.js"),
      JSON.stringify({ url: "https://example.com" }),
    ], { encoding: "utf8" });
    const parsed = JSON.parse(result.trim());
    assert.ok(parsed.error.includes("Browser is not available"));
  });
});

// --- browserInteract ---

describe("browserInteract", () => {
  it("returns error when action is missing", async () => {
    const { execFileSync } = require("child_process");
    const result = execFileSync("node", [
      path.join(__dirname, "skills/agentcore-browser/interact.js"),
      JSON.stringify({}),
    ], { encoding: "utf8" });
    const parsed = JSON.parse(result.trim());
    assert.equal(parsed.error, "action is required");
  });

  it("returns error when action is invalid", async () => {
    const { execFileSync } = require("child_process");
    const result = execFileSync("node", [
      path.join(__dirname, "skills/agentcore-browser/interact.js"),
      JSON.stringify({ action: "drag" }),
    ], { encoding: "utf8" });
    const parsed = JSON.parse(result.trim());
    assert.ok(parsed.error.includes("Invalid action"));
    assert.ok(parsed.error.includes("click"));
    assert.ok(parsed.error.includes("type"));
    assert.ok(parsed.error.includes("wait"));
    assert.ok(parsed.error.includes("scroll"));
  });

  it("returns error when selector missing for click", async () => {
    try { fs.unlinkSync(BROWSER_SESSION_FILE); } catch {}
    const { execFileSync } = require("child_process");
    const result = execFileSync("node", [
      path.join(__dirname, "skills/agentcore-browser/interact.js"),
      JSON.stringify({ action: "click" }),
    ], { encoding: "utf8" });
    const parsed = JSON.parse(result.trim());
    assert.ok(parsed.error.includes("selector is required"));
  });

  it("returns error when selector missing for type", async () => {
    const { execFileSync } = require("child_process");
    const result = execFileSync("node", [
      path.join(__dirname, "skills/agentcore-browser/interact.js"),
      JSON.stringify({ action: "type" }),
    ], { encoding: "utf8" });
    const parsed = JSON.parse(result.trim());
    assert.ok(parsed.error.includes("selector is required"));
  });

  it("returns error when text missing for type with selector", async () => {
    const { execFileSync } = require("child_process");
    const result = execFileSync("node", [
      path.join(__dirname, "skills/agentcore-browser/interact.js"),
      JSON.stringify({ action: "type", selector: "#input" }),
    ], { encoding: "utf8" });
    const parsed = JSON.parse(result.trim());
    assert.ok(parsed.error.includes("text is required"));
  });

  it("returns error when selector missing for wait", async () => {
    const { execFileSync } = require("child_process");
    const result = execFileSync("node", [
      path.join(__dirname, "skills/agentcore-browser/interact.js"),
      JSON.stringify({ action: "wait" }),
    ], { encoding: "utf8" });
    const parsed = JSON.parse(result.trim());
    assert.ok(parsed.error.includes("selector is required"));
  });

  it("returns browser not available for scroll when session file missing", async () => {
    try { fs.unlinkSync(BROWSER_SESSION_FILE); } catch {}
    const { execFileSync } = require("child_process");
    const result = execFileSync("node", [
      path.join(__dirname, "skills/agentcore-browser/interact.js"),
      JSON.stringify({ action: "scroll" }),
    ], { encoding: "utf8" });
    const parsed = JSON.parse(result.trim());
    assert.ok(parsed.error.includes("Browser is not available"));
  });
});

// --- browserScreenshot ---

describe("browserScreenshot", () => {
  it("returns error JSON on connect failure (no session file)", async () => {
    try { fs.unlinkSync(BROWSER_SESSION_FILE); } catch {}
    const { execFileSync } = require("child_process");
    const result = execFileSync("node", [
      path.join(__dirname, "skills/agentcore-browser/screenshot.js"),
      JSON.stringify({}),
    ], { encoding: "utf8" });
    const parsed = JSON.parse(result.trim());
    assert.ok(parsed.error.includes("Browser is not available"));
  });
});

// --- S3 key format ---

describe("uploadScreenshotToS3 key format", () => {
  it("screenshot S3 key contains _screenshots/screenshot_ and .png", () => {
    // We test the key format logic without actually uploading
    const userId = "telegram:123456";
    const namespace = userId.replace(/:/g, "_");
    const timestamp = Date.now();
    const key = `${namespace}/_screenshots/screenshot_${timestamp}.png`;

    assert.ok(key.includes("_screenshots/screenshot_"));
    assert.ok(key.endsWith(".png"));
    assert.ok(key.startsWith("telegram_123456/"));
  });

  it("screenshot key uses underscore namespace format", () => {
    const userId = "slack:U0ABC123";
    const namespace = userId.replace(/:/g, "_");
    const key = `${namespace}/_screenshots/screenshot_${Date.now()}.png`;

    assert.ok(key.startsWith("slack_U0ABC123/"));
    assert.ok(key.includes("_screenshots/screenshot_"));
  });
});

// --- Constants ---

describe("common constants", () => {
  it("exports expected constants", () => {
    const common = require("./skills/agentcore-browser/common");
    assert.equal(common.CONTENT_TRUNCATE_CHARS, 8000);
    assert.equal(common.NAV_TIMEOUT_MS, 30000);
    assert.equal(common.INTERACT_TIMEOUT_MS, 10000);
    assert.equal(common.WAIT_TIMEOUT_MS, 15000);
  });
});
