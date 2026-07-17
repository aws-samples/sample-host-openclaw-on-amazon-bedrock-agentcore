"use strict";

/**
 * Fail-closed IP classifier adapted from OpenClaw 2026.7.2's reviewed
 * packages/net-policy/src/ip.ts at commit 4bfaccafd62ac2ff2e70ca1decc40fb1297ab438.
 * Keep this local because the warm-up process starts independently of the
 * OpenClaw package export graph.
 */

const ipaddr = require("ipaddr.js");

const BLOCKED_IPV4_RANGES = new Set([
  "unspecified",
  "broadcast",
  "multicast",
  "linkLocal",
  "loopback",
  "carrierGradeNat",
  "private",
  "reserved",
]);
const BLOCKED_IPV6_RANGES = new Set([
  "unspecified",
  "loopback",
  "linkLocal",
  "uniqueLocal",
  "multicast",
  "reserved",
  "benchmarking",
  "discard",
  "orchid2",
]);
const RFC2544_BENCHMARK = [ipaddr.IPv4.parse("198.18.0.0"), 15];

function stripIpv6Brackets(value) {
  return value.startsWith("[") && value.endsWith("]")
    ? value.slice(1, -1)
    : value;
}

function parseCanonicalIpAddress(raw) {
  if (typeof raw !== "string" || !raw.trim()) return undefined;
  const value = stripIpv6Brackets(raw.trim());
  if (
    !ipaddr.IPv4.isValidFourPartDecimal(value) &&
    !ipaddr.IPv6.isValid(value)
  ) {
    return undefined;
  }
  return ipaddr.parse(value);
}

function decodeIpv4(high, low) {
  return ipaddr.IPv4.parse(
    [high >>> 8, high & 0xff, low >>> 8, low & 0xff].join("."),
  );
}

function extractEmbeddedIpv4(address) {
  const parts = address.parts;
  if (!Array.isArray(parts) || parts.length !== 8) {
    throw new Error("Expected IPv6 address to contain eight hextets");
  }
  switch (address.range()) {
    case "ipv4Mapped":
      return address.toIPv4Address();
    case "rfc6145":
    case "rfc6052":
      return decodeIpv4(parts[6], parts[7]);
    case "6to4":
      return decodeIpv4(parts[1], parts[2]);
    case "teredo":
      return decodeIpv4(parts[6] ^ 0xffff, parts[7] ^ 0xffff);
    default:
      break;
  }

  const compatible = parts.slice(0, 6).every((part) => part === 0);
  const isatap = (parts[4] & 0xfcff) === 0 && parts[5] === 0x5efe;
  return compatible || isatap ? decodeIpv4(parts[6], parts[7]) : undefined;
}

function isBlockedIpv4(address) {
  return (
    BLOCKED_IPV4_RANGES.has(address.range()) ||
    address.match(RFC2544_BENCHMARK)
  );
}

function isBlockedIpv6(address) {
  if (BLOCKED_IPV6_RANGES.has(address.range())) return true;
  // ipaddr.js does not classify deprecated site-local fec0::/10 as private.
  if ((address.parts[0] & 0xffc0) === 0xfec0) return true;
  const embedded = extractEmbeddedIpv4(address);
  return embedded ? isBlockedIpv4(embedded) : false;
}

function isBlockedIp(raw) {
  const parsed = parseCanonicalIpAddress(raw);
  if (!parsed) {
    // IP-shaped parse failures fail closed. Ordinary DNS hostnames remain
    // subject to hostname and post-DNS checks.
    const value = typeof raw === "string" ? raw.trim() : "";
    return value.includes(":") || /^[0-9x.]+$/i.test(value);
  }
  if (parsed.kind() === "ipv4") return isBlockedIpv4(parsed);
  return isBlockedIpv6(parsed);
}

module.exports = {
  isBlockedIp,
  parseCanonicalIpAddress,
  extractEmbeddedIpv4,
};
