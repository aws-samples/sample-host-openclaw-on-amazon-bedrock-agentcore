// Build-time gate for the undici response-decompression DoS (GHSA-xjj6-f33q-929c):
// an attacker-controlled server can crash a fetch() by returning more than five
// content-codings. Fixed in undici 6.29.1 / 7.30.1 / 8.12.2.
//
// Run this after the base image, OpenClaw and ClawHub are in place. It fails the
// build unless every undici the image can load is at or above the fixed version:
//   - Node's bundled global-fetch undici  >= 7.30.1 (Node >= 24.22.1)
//   - openclaw's undici                    >= 8.12.2 (openclaw 2026.9.5 pins the
//                                            vulnerable 8.10.2; the Dockerfile
//                                            overrides it, and openclaw 2026.10.10
//                                            ships 8.12.2 natively)
//   - clawhub's undici                     >= 7.30.1 (clawhub >= 0.25.1)
//
// Usage: node undici-floor-check.js [global node_modules dir]
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");

const root = process.argv[2] || "/usr/local/lib/node_modules";

const atLeast = (version, min) => {
  const a = version.split(".").map(Number), b = min.split(".").map(Number);
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i];
  return true;
};

// Resolve undici the way each package's own require() does, then read its manifest.
const resolvedUndici = (pkg) => {
  let dir = path.dirname(createRequire(path.join(root, pkg, "package.json")).resolve("undici"));
  while (!fs.existsSync(path.join(dir, "package.json"))) dir = path.dirname(dir);
  return JSON.parse(fs.readFileSync(path.join(dir, "package.json"), "utf8")).version;
};

const checks = [
  ["node bundled undici (global fetch)", process.versions.undici, "7.30.1"],
  ["openclaw undici", resolvedUndici("openclaw"), "8.12.2"],
  ["clawhub undici", resolvedUndici("clawhub"), "7.30.1"],
];

for (const [name, version, min] of checks) console.log(`${name}: ${version} (min ${min})`);

const bad = checks.filter(([, version, min]) => !atLeast(version, min));
if (bad.length) {
  throw new Error(
    "undici below the GHSA-xjj6-f33q-929c fix: " +
    bad.map(([name, version]) => `${name} ${version}`).join(", "),
  );
}
console.log("[build] undici floor check passed (GHSA-xjj6-f33q-929c)");
