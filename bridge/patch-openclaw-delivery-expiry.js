/**
 * Build-time patch: discard a suspended sub-agent result delivery after 2 hours.
 *
 * OpenClaw renders every suspended delivery into each main-session turn as
 * "Child results awaiting delivery", and the model narrates it into replies.
 * Its sub-agent registry sweeper (about once a minute while runs exist)
 * discards a suspended delivery 7 days after `suspendedAt`. This bridge has
 * no channel that could ever receive such a delivery, so the threshold is
 * lowered to the 2 hours the bridge's earlier tasks.dismiss sweep used
 * (openclaw 2026.11.2 has no tasks.* gateway methods). Nothing else changes:
 * the sweeper still discards through OpenClaw's own expiry path and logs
 * "subagent suspended delivery discarded".
 *
 * Only the pinned package version and verified source hashes are accepted.
 */
const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");

const OPENCLAW_VERSION = "2026.11.2";
const TARGET_FILE = "dist/subagent-registry-Cl4t1jcw.mjs";
const ORIGINAL_SHA256 = "84395b2ef53e13030c99015fd0f13e9015e7f302d80ac5319dde59d7f618628b";
const PATCHED_SHA256 = "5aabe293afc81c2589337b81e07ee549db689e3b0876fd16a653262804717593";
const EXPIRY_MS = 2 * 60 * 60 * 1000;
const OLD_ANCHOR = "if (now - (entry.delivery?.suspendedAt ?? now) >= 6048e5) await discardSuspendedPendingFinalDelivery({";
const NEW_ANCHOR = "if (now - (entry.delivery?.suspendedAt ?? now) >= 72e5) await discardSuspendedPendingFinalDelivery({";

function sha256(source) {
  return createHash("sha256").update(source).digest("hex");
}

function replaceExpiry(source) {
  if (source.split(OLD_ANCHOR).length !== 2) {
    throw new Error("Expected exactly one suspended delivery expiry anchor.");
  }
  return source.replace(OLD_ANCHOR, NEW_ANCHOR);
}

/** Validate both byte hashes before returning any replacement content. */
function patchSource(source) {
  const hash = sha256(source);
  if (hash === PATCHED_SHA256) return { changed: false, source };
  if (hash !== ORIGINAL_SHA256) {
    throw new Error("Unsupported OpenClaw sub-agent registry source SHA-256.");
  }
  const patched = Buffer.from(replaceExpiry(source.toString("utf8")), "utf8");
  if (sha256(patched) !== PATCHED_SHA256) {
    throw new Error("Patched OpenClaw sub-agent registry source SHA-256 does not match.");
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
  if (manifest?.name !== "openclaw" || manifest.version !== OPENCLAW_VERSION) {
    throw new Error(`Expected OpenClaw package version ${OPENCLAW_VERSION}.`);
  }
  const target = path.join(packageDir, TARGET_FILE);
  const result = patchSource(fs.readFileSync(target));
  if (result.changed) fs.writeFileSync(target, result.source);
  return { changed: result.changed, version: OPENCLAW_VERSION, file: TARGET_FILE, sha256: PATCHED_SHA256 };
}

if (require.main === module) {
  if (process.argv.length !== 3) {
    console.error("Usage: node patch-openclaw-delivery-expiry.js <openclaw-package-dir>");
    process.exitCode = 1;
  } else {
    try {
      const result = patchPackage(process.argv[2]);
      console.log(
        `[patch-openclaw-delivery-expiry] OpenClaw ${result.version}: suspended delivery expiry patch ${result.changed ? "applied" : "already applied"}.`,
      );
    } catch (err) {
      console.error(`[patch-openclaw-delivery-expiry] ${err.message}`);
      process.exitCode = 1;
    }
  }
}

module.exports = {
  OPENCLAW_VERSION,
  TARGET_FILE,
  ORIGINAL_SHA256,
  PATCHED_SHA256,
  EXPIRY_MS,
  OLD_ANCHOR,
  NEW_ANCHOR,
  sha256,
  replaceExpiry,
  patchSource,
  patchPackage,
};
