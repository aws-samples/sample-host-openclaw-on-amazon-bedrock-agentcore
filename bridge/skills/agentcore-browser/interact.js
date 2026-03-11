"use strict";
const { connectBrowser, applyStealthHeaders, INTERACT_TIMEOUT_MS, WAIT_TIMEOUT_MS } = require("./common");

const VALID_ACTIONS = new Set(["click", "type", "wait", "scroll"]);

async function browserInteract(args) {
  const { action, selector, text } = args || {};

  if (!action) return JSON.stringify({ error: "action is required" });
  if (!VALID_ACTIONS.has(action)) {
    return JSON.stringify({ error: `Invalid action. Must be one of: ${[...VALID_ACTIONS].join(", ")}` });
  }

  // Validate required args before connecting to browser
  if ((action === "click" || action === "wait") && !selector) {
    return JSON.stringify({ error: `selector is required for ${action}` });
  }
  if (action === "type") {
    if (!selector) return JSON.stringify({ error: "selector is required for type" });
    if (!text) return JSON.stringify({ error: "text is required for type" });
  }

  const { page, disconnect } = await connectBrowser();
  try {
    await applyStealthHeaders(page);

    switch (action) {
      case "click":
        await page.click(selector, { timeout: INTERACT_TIMEOUT_MS });
        return JSON.stringify({ success: true, message: `Clicked: ${selector}` });

      case "type":
        await page.fill(selector, text);
        return JSON.stringify({ success: true, message: `Typed into: ${selector}` });

      case "wait":
        await page.waitForSelector(selector, { timeout: WAIT_TIMEOUT_MS });
        return JSON.stringify({ success: true, message: `Element appeared: ${selector}` });

      case "scroll":
        await page.evaluate(() => window.scrollBy(0, 500));
        return JSON.stringify({ success: true, message: "Scrolled down 500px" });

      default:
        return JSON.stringify({ error: `Unknown action: ${action}` });
    }
  } catch (err) {
    if (err.message.includes("Browser session not available")) {
      return JSON.stringify({ error: "Browser is not available. The enable_browser feature must be enabled in CDK configuration." });
    }
    return JSON.stringify({ error: `Interaction failed: ${err.message}` });
  } finally {
    await disconnect();
  }
}

const args = JSON.parse(process.argv[2] || "{}");
browserInteract(args).then(console.log).catch(err => console.log(JSON.stringify({ error: err.message })));
