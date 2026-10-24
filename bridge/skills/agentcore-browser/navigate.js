"use strict";
const {
  connectBrowser,
  applyStealthHeaders,
  truncateContent,
  defaultPolicy,
  precheckUrl,
  UrlPolicyError,
  CONTENT_TRUNCATE_CHARS,
  NAV_TIMEOUT_MS,
} = require("./common");

async function browserNavigate(args) {
  const { url } = args;
  if (!url) return JSON.stringify({ error: "url is required" });

  // Refuse non-http(s) schemes and blocked IP literals before opening the
  // browser; the DNS check follows once connected (see url-policy.js).
  try {
    precheckUrl(url);
  } catch (err) {
    return JSON.stringify({ error: `url rejected: ${err.message}` });
  }

  const { page, checkPage, disconnect } = await connectBrowser();
  try {
    const target = await defaultPolicy.checkUrl(url);
    await applyStealthHeaders(page);
    await page.goto(target, { waitUntil: "domcontentloaded", timeout: NAV_TIMEOUT_MS });
    // A redirect can land somewhere the first check never saw.
    await checkPage();
    const title = await page.title();

    // Extract readable text — remove script/style noise
    let content = "";
    try {
      content = await page.evaluate(() => {
        const clone = document.cloneNode(true);
        clone.querySelectorAll("script, style, noscript, iframe").forEach(el => el.remove());
        return clone.body?.innerText || clone.body?.textContent || "";
      });
    } catch (_) {
      // Execution context may be destroyed during navigation — title is enough as fallback
    }

    return JSON.stringify({
      // Mark the returned payload as untrusted tool output so callers
      // (prompt builders) do not treat it as model instructions.
      source: "untrusted-web-content",
      url: page.url(),
      title,
      content: truncateContent(content.replace(/\s+/g, " ").trim(), CONTENT_TRUNCATE_CHARS),
    });
  } catch (err) {
    if (err instanceof UrlPolicyError) {
      return JSON.stringify({ error: `url rejected: ${err.message}` });
    }
    if (err.message.includes("ERR_BLOCKED_BY_CLIENT")) {
      return JSON.stringify({ error: "url rejected: destination not allowed" });
    }
    if (err.message.includes("Browser session not available")) {
      return JSON.stringify({ error: "Browser is not available. The enable_browser feature must be enabled in CDK configuration." });
    }
    return JSON.stringify({ error: `Navigation failed: ${err.message}` });
  } finally {
    await disconnect();
  }
}

// CLI entrypoint (called by OpenClaw)
const args = JSON.parse(process.argv[2] || "{}");
browserNavigate(args).then(console.log).catch(err => console.log(JSON.stringify({ error: err.message })));
