"use strict";
const fs = require("fs");

const BROWSER_SESSION_FILE = "/tmp/agentcore-browser-session.json";
const CONTENT_TRUNCATE_CHARS = 8000;
const NAV_TIMEOUT_MS = 30000;
const INTERACT_TIMEOUT_MS = 10000;
const WAIT_TIMEOUT_MS = 15000;

// Module-level CDP connection cache
let _browser = null;
let _page = null;

function getBrowserEndpoint() {
  if (!fs.existsSync(BROWSER_SESSION_FILE)) {
    throw new Error(
      "Browser session not available. Ensure enable_browser=true in CDK config and that the session has been initialized."
    );
  }
  const data = JSON.parse(fs.readFileSync(BROWSER_SESSION_FILE, "utf8"));
  if (!data.endpoint) throw new Error("Browser session file missing endpoint");
  return data.endpoint;
}

async function connectBrowser() {
  if (_browser && _page) {
    // Check connection is still alive
    try {
      await _page.title(); // lightweight liveness check
      return { browser: _browser, page: _page };
    } catch {
      _browser = null;
      _page = null;
    }
  }
  const endpoint = getBrowserEndpoint();
  const { chromium } = require("playwright-core");
  _browser = await chromium.connectOverCDP(endpoint, { timeout: 15000 });
  const contexts = _browser.contexts();
  const context = contexts.length > 0 ? contexts[0] : await _browser.newContext();
  const pages = context.pages();
  _page = pages.length > 0 ? pages[0] : await context.newPage();
  return { browser: _browser, page: _page };
}

async function uploadScreenshotToS3(imageBuffer) {
  const bucket = process.env.S3_USER_FILES_BUCKET;
  if (!bucket) {
    throw new Error("S3_USER_FILES_BUCKET environment variable is not set — cannot upload screenshot");
  }
  const userId = process.env.USER_ID || "default-user";
  const namespace = userId.replace(/:/g, "_");
  const timestamp = Date.now();
  const key = `${namespace}/_screenshots/screenshot_${timestamp}.png`;

  const { S3Client, PutObjectCommand } = require("@aws-sdk/client-s3");
  const client = new S3Client({ region: process.env.AWS_REGION || "us-east-1" });
  await client.send(new PutObjectCommand({
    Bucket: bucket,
    Key: key,
    Body: imageBuffer,
    ContentType: "image/png",
  }));
  return key;
}

function truncateContent(text, maxChars) {
  if (text.length <= maxChars) return text;
  return text.slice(0, maxChars) + `\n\n[Content truncated at ${maxChars} characters]`;
}

module.exports = {
  getBrowserEndpoint,
  connectBrowser,
  uploadScreenshotToS3,
  truncateContent,
  CONTENT_TRUNCATE_CHARS,
  NAV_TIMEOUT_MS,
  INTERACT_TIMEOUT_MS,
  WAIT_TIMEOUT_MS,
};
