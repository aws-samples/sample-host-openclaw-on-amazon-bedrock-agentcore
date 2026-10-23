"use strict";
const { connectBrowser, uploadScreenshotToS3, UrlPolicyError } = require("./common");

async function browserScreenshot(args) {
  const { description } = args || {};

  const { page, checkPage, disconnect } = await connectBrowser();
  try {
    // Never capture a page that sits on a destination the policy blocks.
    await checkPage();
    const imageBuffer = await page.screenshot({ type: "png", fullPage: false });
    const s3Key = await uploadScreenshotToS3(imageBuffer);

    const caption = description ? ` — ${description}` : "";
    return `Screenshot taken${caption}: [SCREENSHOT:${s3Key}]`;
  } catch (err) {
    if (err instanceof UrlPolicyError) {
      return JSON.stringify({ error: `Screenshot refused: ${err.message}` });
    }
    if (err.message.includes("Browser session not available")) {
      return JSON.stringify({ error: "Browser is not available. The enable_browser feature must be enabled in CDK configuration." });
    }
    return JSON.stringify({ error: `Screenshot failed: ${err.message}` });
  } finally {
    await disconnect();
  }
}

const args = JSON.parse(process.argv[2] || "{}");
browserScreenshot(args).then(console.log).catch(err => console.log(JSON.stringify({ error: err.message })));
