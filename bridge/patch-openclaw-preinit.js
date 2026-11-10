/**
 * Patch OpenClaw's self-presence reset for preinit snapshots in AgentCore Runtime.
 *
 * The added reset refreshes the instance ID and clock origins after user
 * binding, once per process and only while self-presence is the sole entry.
 * The gateway is spawned after the snapshot restore, so this now guards the
 * preparation window rather than a snapshotted process.
 * Package version, source hashes, and replacement anchors are pinned; all
 * required inputs and resulting hashes are checked before vendor files change.
 */
const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");

const OPENCLAW_VERSION = "2026.11.2";
const LAUNCHER_FILE = "openclaw.mjs";
const PRESENCE_FILE = "dist/system-presence-Di2OrVHO.mjs";
const PRESENCE_EXPORT = "export { upsertPresence as a, updateSystemPresence as i, listSystemPresence as n, touchPresence as r, commitPresence as t };";
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
export { resetAgentCoreSnapshotPresence, upsertPresence as a, updateSystemPresence as i, listSystemPresence as n, touchPresence as r, commitPresence as t };`;

const PATCHES = Object.freeze([
  Object.freeze({
    file: PRESENCE_FILE,
    originalSha256: "0671b94acc271233e3a03c065536cf1f0434b8e1817d8e885177f69473ed68e3",
    patchedSha256: "63b624abc8629e1e236801c2b6758fc0f7d2a2e35f0a9409ee83c0315b2cf645",
  }),
]);
const PINNED_RUNTIME_FILES = Object.freeze({
  [LAUNCHER_FILE]: "469e60c9f60b615ff71c985a54238ee532a8f949866d76aebe7d6e4bc7219902",
  // The gateway's lazily loaded server implementation (startGatewayServerCore).
  "dist/gateway-prewarm.js": "78681bb339bea4253b22860dd450fbce59663b411ccddf21bae661c87681b3cb",
  "dist/get-reply-from-config.runtime-COs94SyF.mjs": "2456193106b30c00117cd8743a09e8f0a7aab35176c59659cfcbb70635ddf4a3",
  "dist/get-reply-CvPWdKII.mjs": "7ef72b03167d556c1aee49c50b46e01e8b64fbeb1e82c670ee012f52f037454d",
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
