/**
 * Patch OpenClaw's self-presence reset for preinit snapshots in AgentCore Runtime.
 *
 * The added reset refreshes the instance ID and clock origins after user
 * binding, once per process and only while self-presence is the sole entry.
 * Package version, source hashes, and replacement anchors are pinned; all
 * required inputs and resulting hashes are checked before vendor files change.
 */
const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");

const OPENCLAW_VERSION = "2026.9.5";
const LAUNCHER_FILE = "openclaw.mjs";
const PRESENCE_FILE = "dist/system-presence-B9mMmTN5.mjs";
const PRESENCE_EXPORT = "export { upsertPresence as i, touchPresence as n, updateSystemPresence as r, listSystemPresence as t };";
const PRESENCE_RESET = `// AgentCore calls this only after binding, before the original CLI starts.
let agentCorePresenceReset = false;
function resetAgentCoreSnapshotPresence() {
\tif (process.env.OPENCLAW_AGENTCORE_PREINIT !== "1" ||
\t\t!process.env.USER_ID || !process.env.INTERNAL_USER_ID ||
\t\tagentCorePresenceReset || entries.size !== 1 || !entries.has(SELF_KEY)) {
\t\tthrow new Error("AgentCore presence reset requires a single bound preinit activation.");
\t}
\tSELF_INSTANCE_ID = randomUUID();
\tuptimeOrigin = os.uptime() * 1e3 - performance.now();
\tfreshnessTime = Date.now();
\tfreshnessSample = continuousTimeNow();
\tentries.delete(SELF_KEY);
\tinitSelfPresence();
\tagentCorePresenceReset = true;
}
export { resetAgentCoreSnapshotPresence, upsertPresence as i, touchPresence as n, updateSystemPresence as r, listSystemPresence as t };`;

const PATCHES = Object.freeze([
  Object.freeze({
    file: PRESENCE_FILE,
    originalSha256: "36f48de3d1ea40ab551a0bef2c55da17bd71c6f73ead689622b5bb4d9a457671",
    patchedSha256: "c58957886f965656d3fa751bec6ebcf49f8dbfe5a809560e2ad6f13da3bae52b",
  }),
]);
const PINNED_RUNTIME_FILES = Object.freeze({
  [LAUNCHER_FILE]: "538e8ee2b65a0b24bb8a5ed3421bfe66621b1e0b5f726a167758c004f566fb36",
  "dist/server-start-gE7ggn-j.mjs": "ca2f0adbe8273b170462bfe9a0a25f53a77c9828fb22cbb7160cae186f9af7d8",
  "dist/get-reply-from-config.runtime-uSvr2ey5.mjs": "a58da16a2a15c2457e90853fdeeb3fe6c52764a25a89d3e71a67d80d98a84b62",
  "dist/get-reply-DN4OcavF.mjs": "ddacdea4bc3ea5e0fc10418e7c2688f76f97fe74358b03ba6d6c29ded4c51081",
});

function sha256(source) {
  return createHash("sha256").update(source).digest("hex");
}

function replaceExactlyOnce(source, anchor, replacement) {
  if (source.split(anchor).length !== 2) {
    throw new Error("Expected exactly one pinned preinit patch anchor.");
  }
  return source.replace(anchor, replacement);
}

function transformSource(file, source) {
  if (file === PRESENCE_FILE) {
    let result = replaceExactlyOnce(source,
      "const SELF_INSTANCE_ID = randomUUID();", "let SELF_INSTANCE_ID = randomUUID();");
    result = replaceExactlyOnce(result,
      "const uptimeOrigin = os.uptime() * 1e3 - performance.now();",
      "let uptimeOrigin = os.uptime() * 1e3 - performance.now();");
    return replaceExactlyOnce(result, PRESENCE_EXPORT, PRESENCE_RESET);
  }
  throw new Error("Unsupported OpenClaw preinit patch target.");
}

function patchSource(file, source) {
  const pin = PATCHES.find((item) => item.file === file);
  if (!pin) throw new Error("Unsupported OpenClaw preinit patch target.");
  const hash = sha256(source);
  if (hash === pin.patchedSha256) return { changed: false, source };
  if (hash !== pin.originalSha256) {
    throw new Error(`Unsupported OpenClaw preinit source SHA-256: ${file}.`);
  }
  const patched = Buffer.from(transformSource(file, source.toString("utf8")), "utf8");
  if (sha256(patched) !== pin.patchedSha256) {
    throw new Error(`Patched OpenClaw preinit source SHA-256 does not match: ${file}.`);
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
  const results = PATCHES.map((pin) => ({
    ...pin, ...patchSource(pin.file, fs.readFileSync(path.join(packageDir, pin.file))),
  }));
  for (const [file, hash] of Object.entries(PINNED_RUNTIME_FILES)) {
    if (sha256(fs.readFileSync(path.join(packageDir, file))) !== hash) {
      throw new Error(`Unsupported OpenClaw preinit runtime SHA-256: ${file}.`);
    }
  }
  // Validate every pinned input before writing.
  for (const result of results) {
    if (result.changed) fs.writeFileSync(path.join(packageDir, result.file), result.source);
  }
  return {
    changed: results.some((result) => result.changed),
    version: OPENCLAW_VERSION,
    files: results.map(({ file, changed, patchedSha256 }) => ({ file, changed, sha256: patchedSha256 })),
  };
}

if (require.main === module) {
  if (process.argv.length !== 3) {
    console.error("Usage: node patch-openclaw-preinit.js <openclaw-package-dir>");
    process.exitCode = 1;
  } else {
    try {
      const result = patchPackage(process.argv[2]);
      console.log(`[patch-openclaw-preinit] OpenClaw ${result.version}: preinit patches ${result.changed ? "applied" : "already applied"}.`);
    } catch (error) {
      console.error(`[patch-openclaw-preinit] ${error.message}`);
      process.exitCode = 1;
    }
  }
}

module.exports = {
  OPENCLAW_VERSION, LAUNCHER_FILE, PRESENCE_FILE,
  PRESENCE_EXPORT, PRESENCE_RESET, PATCHES, PINNED_RUNTIME_FILES,
  sha256, replaceExactlyOnce, transformSource, patchSource, patchPackage,
};
