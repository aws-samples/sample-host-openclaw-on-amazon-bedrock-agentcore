"use strict";
const fs = require("fs");

// Ensure /app/node_modules is in the module search path — OpenClaw's exec tool
// may not forward NODE_PATH to child processes.
if (!process.env.NODE_PATH?.includes("/app/node_modules")) {
  require("module").Module._initPaths &&
    (process.env.NODE_PATH = [process.env.NODE_PATH, "/app/node_modules"].filter(Boolean).join(":"));
  require("module").Module._initPaths();
}

const BROWSER_SESSION_FILE = "/tmp/agentcore-browser-session.json";
const CONTENT_TRUNCATE_CHARS = 8000;
const NAV_TIMEOUT_MS = 30000;
const INTERACT_TIMEOUT_MS = 10000;
const WAIT_TIMEOUT_MS = 15000;

function getBrowserSession() {
  if (!fs.existsSync(BROWSER_SESSION_FILE)) {
    throw new Error(
      "Browser session not available. Ensure enable_browser=true in CDK config and that the session has been initialized."
    );
  }
  const data = JSON.parse(fs.readFileSync(BROWSER_SESSION_FILE, "utf8"));
  if (!data.endpoint) throw new Error("Browser session file missing endpoint");
  return data;
}

async function connectBrowser() {
  const session = getBrowserSession();
  const { chromium } = require("playwright-core");
  const browser = await chromium.connectOverCDP(session.endpoint, {
    timeout: 15000,
    headers: session.headers || {},
  });
  const contexts = browser.contexts();
  const context = contexts.length > 0 ? contexts[0] : await browser.newContext({ ignoreHTTPSErrors: true });
  const pages = context.pages();
  const page = pages.length > 0 ? pages[0] : await context.newPage();
  return {
    browser,
    page,
    disconnect: async () => {
      try { await browser.close(); } catch {}
    },
  };
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

const STEALTH_USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36";

async function applyStealthHeaders(page) {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });
    Object.defineProperty(navigator, "plugins", { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, "languages", { get: () => ["en-US", "en"] });
    window.chrome = { runtime: {} };
  });
  await page.setExtraHTTPHeaders({
    "User-Agent": STEALTH_USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
  });
}

module.exports = {
  getBrowserSession,
  connectBrowser,
  applyStealthHeaders,
  uploadScreenshotToS3,
  truncateContent,
  CONTENT_TRUNCATE_CHARS,
  NAV_TIMEOUT_MS,
  INTERACT_TIMEOUT_MS,
  WAIT_TIMEOUT_MS,
};
