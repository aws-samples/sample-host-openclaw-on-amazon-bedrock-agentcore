/**
 * Build-time patch for OpenClaw's empty Active Subagents runtime fact.
 * Only the pinned package version and verified source hashes are accepted.
 */
const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");

const OPENCLAW_VERSION = "2026.9.5";
const TARGET_FILE = "dist/system-prompt-report-2ZKyKbCp.mjs";
const ORIGINAL_SHA256 = "923dcae542ebabaf7b5dc08f9530436d8778aedf57e397fde4e52a78423e041e";
const PATCHED_SHA256 = "46a40964a7c5c9505594c8728b39597291af960c29a2c1f37a593d6816c2bcaf";
const OLD_ANCHOR = 'if (subagentContext || canSpawn) sections.push(subagentContext ?? "## Active Subagents\\nnone");';
const NEW_ANCHOR = "if (subagentContext) sections.push(subagentContext);";

function sha256(source) {
  return createHash("sha256").update(source).digest("hex");
}

function replaceEmptySubagentFact(source) {
  if (source.split(OLD_ANCHOR).length !== 2) {
    throw new Error("Expected exactly one empty subagent fact anchor.");
  }
  return source.replace(OLD_ANCHOR, NEW_ANCHOR);
}

/** Validate both byte hashes before returning any replacement content. */
function patchSource(source) {
  const hash = sha256(source);
  if (hash === PATCHED_SHA256) return { changed: false, source };
  if (hash !== ORIGINAL_SHA256) {
    throw new Error("Unsupported OpenClaw runtime facts source SHA-256.");
  }
  const patched = Buffer.from(replaceEmptySubagentFact(source.toString("utf8")), "utf8");
  if (sha256(patched) !== PATCHED_SHA256) {
    throw new Error("Patched OpenClaw runtime facts source SHA-256 does not match.");
  }
  return { changed: true, source: patched };
}

function patchPackage(packageDir) {
  let manifest;
  try {
    manifest = JSON.parse(fs.readFileSync(path.join(packageDir, "package.json"), "utf8"));
  } catch {
    throw new Error("Cannot read a valid OpenClaw package.json.");
  }
  if (manifest?.version !== OPENCLAW_VERSION) {
    throw new Error(`Expected OpenClaw package version ${OPENCLAW_VERSION}.`);
  }
  const target = path.join(packageDir, TARGET_FILE);
  const result = patchSource(fs.readFileSync(target));
  if (result.changed) fs.writeFileSync(target, result.source);
  return { changed: result.changed, version: OPENCLAW_VERSION, file: TARGET_FILE, sha256: PATCHED_SHA256 };
}

if (require.main === module) {
  if (process.argv.length !== 3) {
    console.error("Usage: node patch-openclaw.js <openclaw-package-dir>");
    process.exitCode = 1;
  } else {
    try {
      const result = patchPackage(process.argv[2]);
      console.log(
        `[patch-openclaw] OpenClaw ${result.version}: empty subagent fact patch ${result.changed ? "applied" : "already applied"}.`,
      );
    } catch (err) {
      console.error(`[patch-openclaw] ${err.message}`);
      process.exitCode = 1;
    }
  }
}

module.exports = {
  OPENCLAW_VERSION,
  TARGET_FILE,
  ORIGINAL_SHA256,
  PATCHED_SHA256,
  OLD_ANCHOR,
  NEW_ANCHOR,
  sha256,
  replaceEmptySubagentFact,
  patchSource,
  patchPackage,
};
