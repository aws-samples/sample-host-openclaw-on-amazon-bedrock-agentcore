"use strict";
const fs = require("fs");
const net = require("net");
const dns = require("dns").promises;
const urlPolicy = require("./url-policy");
const { installRequestGuard, assertPageAllowed, UrlPolicyError, isBlockedIp } = urlPolicy;

const BROWSER_SESSION_FILE = "/tmp/agentcore-browser-session.json";
const CONTENT_TRUNCATE_CHARS = 8000;
const NAV_TIMEOUT_MS = 30000;
const INTERACT_TIMEOUT_MS = 10000;
const WAIT_TIMEOUT_MS = 15000;

// Accepting invalid TLS certificates is a security-posture change: it disables
// certificate validation for ALL browsing in this context. Keep it OFF by
// default and require an explicit opt-in via BROWSER_IGNORE_HTTPS_ERRORS=true,
// intended only for VPC environments that terminate TLS with a private CA.
const IGNORE_HTTPS_ERRORS = process.env.BROWSER_IGNORE_HTTPS_ERRORS === "true";

// --- Operator restrictions on top of url-policy.js ---
//
// Optional, set only through the container environment (never tool args):
//   BROWSER_HOST_ALLOWLIST  comma-separated exact hostnames; when set, every
//                           other host is refused (subresources included).
//   BROWSER_BLOCKED_CIDRS   comma-separated extra IPv4/IPv6 CIDRs to refuse,
//                           e.g. a VPC range that uses public addresses.
// An unparseable CIDR fails closed: every URL is refused until it is fixed.

function envList(value) {
  return (value || "").split(",").map((s) => s.trim()).filter(Boolean);
}

function hostAllowlist(env = process.env) {
  const items = envList(env.BROWSER_HOST_ALLOWLIST).map((h) => h.toLowerCase().replace(/\.$/, ""));
  return items.length ? new Set(items) : null;
}

function extraBlockList(env = process.env) {
  const cidrs = envList(env.BROWSER_BLOCKED_CIDRS);
  if (!cidrs.length) return null;
  const list = new net.BlockList();
  for (const cidr of cidrs) {
    const [addr, bitsStr] = cidr.split("/");
    const family = net.isIPv4(addr) ? "ipv4" : net.isIPv6(addr) ? "ipv6" : null;
    const bits = Number(bitsStr);
    const max = family === "ipv4" ? 32 : 128;
    if (!family || !/^\d+$/.test(bitsStr || "") || bits > max) {
      return { invalid: cidr };
    }
    list.addSubnet(addr, bits, family);
  }
  return { list };
}

/** url-policy precheck plus the operator host allowlist (no DNS). */
function precheckUrl(input, env = process.env) {
  const result = urlPolicy.precheckUrl(input);
  const allow = hostAllowlist(env);
  if (allow && !allow.has(result.host)) {
    throw new UrlPolicyError(`destination not allowed: ${result.host} is not on the configured host allowlist`);
  }
  return result;
}

/**
 * Wrap a url-policy policy with the operator restrictions above. Returns the
 * base policy unchanged when neither variable is set.
 */
function withOperatorRestrictions(base, { env = process.env, lookup = dns.lookup } = {}) {
  const allow = hostAllowlist(env);
  const extra = extraBlockList(env);
  if (!allow && !extra) return base;
  const cache = new Map();
  const addressesOf = (host) => {
    if (!cache.has(host)) {
      cache.set(host, (async () => {
        try {
          return (await lookup(host, { all: true, verbatim: true })).map((r) => r.address);
        } catch (err) {
          throw new UrlPolicyError(`could not resolve ${host} (${err.code || "lookup failed"})`);
        }
      })());
    }
    return cache.get(host);
  };
  return {
    async checkUrl(input) {
      const href = await base.checkUrl(input);
      const { host } = precheckUrl(input, env);
      if (extra) {
        if (extra.invalid) {
          throw new UrlPolicyError(`BROWSER_BLOCKED_CIDRS has an invalid entry: ${extra.invalid}`);
        }
        const addrs = net.isIP(host) ? [host] : await addressesOf(host);
        for (const addr of addrs) {
          const ip = addr.split("%")[0];
          if (extra.list.check(ip, net.isIPv6(ip) ? "ipv6" : "ipv4")) {
            throw new UrlPolicyError(`destination not allowed: ${host} is in a blocked range`);
          }
        }
      }
      return href;
    },
  };
}

const defaultPolicy = withOperatorRestrictions(urlPolicy.defaultPolicy);

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

/**
 * Connect to the session's browser over CDP. Every request the context makes
 * while this process is connected goes through the destination policy in
 * url-policy.js; callers check the page with checkPage() before returning
 * anything read from it.
 */
async function connectBrowser({ policy = defaultPolicy } = {}) {
  const session = getBrowserSession();
  const { chromium } = require("playwright-core");
  const browser = await chromium.connectOverCDP(session.endpoint, {
    timeout: 15000,
    headers: session.headers || {},
  });
  const disconnect = async () => {
    try { await browser.close(); } catch {}
  };
  try {
    const contexts = browser.contexts();
    const context = contexts.length > 0 ? contexts[0] : await browser.newContext({ ignoreHTTPSErrors: IGNORE_HTTPS_ERRORS });
    await installRequestGuard(context, policy);
    const pages = context.pages();
    const page = pages.length > 0 ? pages[0] : await context.newPage();
    return {
      browser,
      page,
      checkPage: () => assertPageAllowed(page, policy),
      disconnect,
    };
  } catch (err) {
    await disconnect();
    throw err;
  }
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
  defaultPolicy,
  precheckUrl,
  withOperatorRestrictions,
  isBlockedIp,
  UrlPolicyError,
  CONTENT_TRUNCATE_CHARS,
  NAV_TIMEOUT_MS,
  INTERACT_TIMEOUT_MS,
  WAIT_TIMEOUT_MS,
};
