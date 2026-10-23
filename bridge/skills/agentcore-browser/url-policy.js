"use strict";
/**
 * Destination policy for the agentcore-browser skill.
 *
 * The AgentCore Browser runs in the VPC private subnets with the agent's
 * security group, so it can reach addresses the user should not be able to
 * read through chat: loopback inside the browser sandbox, link-local
 * metadata/DNS endpoints, and private addresses in the VPC. This module
 * decides whether a URL may be loaded:
 *
 *   - only http: and https: URLs are navigable;
 *   - the host, or every address it resolves to, must be a public unicast
 *     address (see BLOCKED_V4 / BLOCKED_V6 below);
 *   - IPv6 forms that embed an IPv4 address (IPv4-mapped, NAT64, 6to4) are
 *     judged by the embedded IPv4 address.
 *
 * Resolution happens in this container, which shares the VPC resolver with
 * the browser. The browser resolves the name again when it connects, so a
 * DNS answer that changes between the two lookups is not caught here; the
 * post-load check in assertPageAllowed() is the second line, and the network
 * layer is the place to close that gap completely.
 */
const dns = require("dns").promises;
const net = require("net");

const NAVIGABLE_SCHEMES = new Set(["http:", "https:"]);
// Frame URLs that carry no fetched network content of their own.
const INERT_SCHEMES = new Set(["about:", "data:", "blob:", "chrome-error:"]);

const BLOCKED_V4 = [
  ["0.0.0.0", 8], // "this network"
  ["10.0.0.0", 8], // RFC 1918
  ["100.64.0.0", 10], // carrier-grade NAT
  ["127.0.0.0", 8], // loopback
  ["169.254.0.0", 16], // link-local, including instance metadata and DNS
  ["172.16.0.0", 12], // RFC 1918
  ["192.0.0.0", 24], // IETF protocol assignments
  ["192.0.2.0", 24], // documentation
  ["192.88.99.0", 24], // 6to4 relay anycast
  ["192.168.0.0", 16], // RFC 1918
  ["198.18.0.0", 15], // benchmarking
  ["198.51.100.0", 24], // documentation
  ["203.0.113.0", 24], // documentation
  ["224.0.0.0", 4], // multicast
  ["240.0.0.0", 4], // reserved, including broadcast
];

const BLOCKED_V6 = [
  ["::", 96], // unspecified, loopback, IPv4-compatible
  ["64:ff9b:1::", 48], // local-use NAT64
  ["100::", 64], // discard
  ["2001::", 32], // Teredo
  ["2001:db8::", 32], // documentation
  ["fc00::", 7], // unique local, including the EC2 IPv6 metadata endpoint
  ["fe80::", 10], // link-local
  ["fec0::", 10], // site-local (deprecated)
  ["ff00::", 8], // multicast
];

function buildBlockList() {
  const list = new net.BlockList();
  for (const [addr, bits] of BLOCKED_V4) list.addSubnet(addr, bits, "ipv4");
  for (const [addr, bits] of BLOCKED_V6) list.addSubnet(addr, bits, "ipv6");
  return list;
}

const BLOCK_LIST = buildBlockList();

/** Expand an IPv6 literal to eight 16-bit groups, or null if it is not one. */
function ipv6Groups(ip) {
  if (!net.isIPv6(ip)) return null;
  let addr = ip.toLowerCase();
  const zone = addr.indexOf("%");
  if (zone !== -1) addr = addr.slice(0, zone);
  let tail = [];
  const lastColon = addr.lastIndexOf(":");
  const last = addr.slice(lastColon + 1);
  if (net.isIPv4(last)) {
    const o = last.split(".").map(Number);
    tail = [(o[0] << 8) | o[1], (o[2] << 8) | o[3]];
    addr = addr.slice(0, lastColon + 1) + "0";
  }
  const [head, rest] = addr.includes("::") ? addr.split("::") : [addr, null];
  const left = head ? head.split(":") : [];
  const right = rest === null ? [] : rest ? rest.split(":") : [];
  const want = 8 - (tail.length ? 1 : 0);
  const fill = rest === null ? 0 : want - left.length - right.length;
  const groups = [...left, ...Array(Math.max(fill, 0)).fill("0"), ...right].map((g) => parseInt(g, 16));
  if (tail.length) groups.splice(groups.length - 1, 1, ...tail);
  return groups.length === 8 ? groups : null;
}

function v4FromGroups(hi, lo) {
  return [hi >> 8, hi & 0xff, lo >> 8, lo & 0xff].join(".");
}

/** The IPv4 address an IPv6 address stands for, if it embeds one. */
function embeddedIpv4(ip) {
  const g = ipv6Groups(ip);
  if (!g) return null;
  const zeros = (from, to) => g.slice(from, to).every((x) => x === 0);
  if (zeros(0, 5) && g[5] === 0xffff) return v4FromGroups(g[6], g[7]); // ::ffff:a.b.c.d
  if (g[0] === 0x64 && g[1] === 0xff9b && zeros(2, 6)) return v4FromGroups(g[6], g[7]); // 64:ff9b::/96
  if (g[0] === 0x2002) return v4FromGroups(g[1], g[2]); // 6to4
  return null;
}

/** True when the browser must not connect to this IP address. */
function isBlockedIp(ip) {
  if (net.isIPv4(ip)) return BLOCK_LIST.check(ip, "ipv4");
  if (net.isIPv6(ip)) {
    const v4 = embeddedIpv4(ip);
    if (v4) return BLOCK_LIST.check(v4, "ipv4");
    return BLOCK_LIST.check(ip.split("%")[0], "ipv6");
  }
  return true; // not an IP address: refuse
}

class UrlPolicyError extends Error {}

/**
 * Checks that need no DNS: the URL parses, the scheme is http(s), and the
 * host is not a loopback name or a blocked IP literal. Returns the
 * normalised URL and host.
 */
function precheckUrl(input) {
  let parsed;
  try {
    parsed = new URL(input);
  } catch {
    throw new UrlPolicyError("url is not a valid absolute URL");
  }
  if (!NAVIGABLE_SCHEMES.has(parsed.protocol)) {
    throw new UrlPolicyError(`url scheme not allowed: ${parsed.protocol.replace(/:$/, "")} (only http and https)`);
  }
  let host = parsed.hostname.toLowerCase();
  if (host.startsWith("[") && host.endsWith("]")) host = host.slice(1, -1);
  if (host.endsWith(".")) host = host.slice(0, -1);
  if (!host) throw new UrlPolicyError("url is missing a hostname");
  if (host === "localhost" || host.endsWith(".localhost")) {
    throw new UrlPolicyError(`destination not allowed: ${host}`);
  }
  if (net.isIP(host) && isBlockedIp(host)) {
    throw new UrlPolicyError(`destination not allowed: ${host}`);
  }
  return { href: parsed.href, host };
}

/**
 * Build a policy. `lookup` defaults to dns.lookup and is injectable for
 * tests. Lookups are cached for the life of the process (one skill call).
 */
function createUrlPolicy({ lookup = dns.lookup } = {}) {
  const cache = new Map();

  function checkResolved(host) {
    if (!cache.has(host)) {
      cache.set(host, (async () => {
        let records;
        try {
          records = await lookup(host, { all: true, verbatim: true });
        } catch (err) {
          throw new UrlPolicyError(`could not resolve ${host} (${err.code || "lookup failed"})`);
        }
        if (!records || records.length === 0) throw new UrlPolicyError(`could not resolve ${host}`);
        if (records.some((r) => isBlockedIp(r.address))) {
          throw new UrlPolicyError(`destination not allowed: ${host} resolves to a non-public address`);
        }
      })());
    }
    return cache.get(host);
  }

  /** Throw UrlPolicyError unless `input` is an http(s) URL to a public host. */
  async function checkUrl(input) {
    const { href, host } = precheckUrl(input);
    if (!net.isIP(host)) await checkResolved(host);
    return href;
  }

  return { checkUrl };
}

function isInertUrl(url) {
  if (!url) return true;
  try {
    return INERT_SCHEMES.has(new URL(url).protocol);
  } catch {
    return false;
  }
}

/**
 * Apply the policy to every request the browser context issues (documents,
 * frames, subresources, popups). Playwright calls the handler for the first
 * URL of a redirect chain only, so redirect targets are checked afterwards by
 * assertPageAllowed().
 */
async function installRequestGuard(context, policy) {
  await context.route("**/*", async (route) => {
    const url = route.request().url();
    let allowed = isInertUrl(url);
    if (!allowed) {
      try {
        await policy.checkUrl(url);
        allowed = true;
      } catch {
        allowed = false;
      }
    }
    try {
      if (allowed) await route.continue();
      else await route.abort("blockedbyclient");
    } catch {
      // The page navigated or closed while the request was paused.
    }
  });
}

/**
 * Check the URL every frame of the page ended up on (after redirects, and
 * after anything the page did while no skill was connected). If one is not
 * allowed, blank the page and throw so nothing from it is returned.
 */
async function assertPageAllowed(page, policy) {
  for (const frame of page.frames()) {
    const url = frame.url();
    if (isInertUrl(url)) continue;
    try {
      await policy.checkUrl(url);
    } catch (err) {
      try { await page.goto("about:blank"); } catch {}
      throw new UrlPolicyError(`page reached a destination that is not allowed (${err.message})`);
    }
  }
}

module.exports = {
  UrlPolicyError,
  createUrlPolicy,
  precheckUrl,
  installRequestGuard,
  assertPageAllowed,
  isBlockedIp,
  embeddedIpv4,
  defaultPolicy: createUrlPolicy(),
};
