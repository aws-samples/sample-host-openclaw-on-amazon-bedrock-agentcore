// The cron Lambda's brief-audio marker must survive OpenClaw's own secret redaction.
//
// OpenClaw redacts the value after labels that look like secret names, so the
// first marker (AUDIO_S3_KEY) reached the Lambda as "AUDIO_S3_KEY: ***". This
// test runs the pinned OpenClaw dist's redaction functions on the marker line
// the briefs emit. It needs the dist: set OPENCLAW_DIST to an installed
// openclaw package directory (or install openclaw where Node can resolve it);
// otherwise it is skipped and tests/test_cron_audio.py checks the copied patterns.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const repo = path.resolve(__dirname, "..");
const pinned = fs.readFileSync(path.join(repo, "bridge", "Dockerfile"), "utf8").match(/\bopenclaw@(\d[\w.-]*)/)?.[1];
const label = fs.readFileSync(path.join(repo, "lambda", "cron", "index.py"), "utf8")
  .match(/^AUDIO_MARKER_LABEL = "([A-Z0-9_]+)"$/m)?.[1];

function findOpenClaw() {
  if (process.env.OPENCLAW_DIST) return path.resolve(process.env.OPENCLAW_DIST);
  try {
    return path.dirname(require.resolve("openclaw/package.json"));
  } catch {
    return null;
  }
}

// The redaction module is a hashed chunk; find it by its export of redactSensitiveText.
async function loadRedaction(root) {
  const dist = path.join(root, "dist");
  for (const name of fs.readdirSync(dist).filter((file) => /^redact-[\w-]+\.mjs$/.test(file))) {
    const source = fs.readFileSync(path.join(dist, name), "utf8");
    const exports = source.match(/^export \{([^}]*)\};?\s*$/m)?.[1];
    if (!exports) continue;
    const alias = (local) => exports.match(new RegExp(`\\b${local} as (\\w+)`))?.[1];
    if (!alias("redactSensitiveText")) continue;
    const mod = await import(pathToFileURL(path.join(dist, name)).href);
    return Object.fromEntries(["redactSensitiveText", "redactToolPayloadText", "redactSecrets"]
      .filter((local) => alias(local)).map((local) => [local, mod[alias(local)]]));
  }
  throw new Error("OpenClaw redaction module not found in dist");
}

const root = findOpenClaw();
const skip = root ? false : "OpenClaw dist not installed (set OPENCLAW_DIST)";

test("the brief-audio marker label is read from the cron Lambda", () => {
  assert.equal(label, "BRIEF_AUDIO");
  assert.ok(pinned, "openclaw version pin not found in bridge/Dockerfile");
});

test("OpenClaw's redaction leaves the brief-audio marker line intact", { skip }, async () => {
  const version = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8")).version;
  assert.equal(version, pinned, `installed OpenClaw ${version} is not the pinned ${pinned}`);
  const redaction = await loadRedaction(root);
  assert.ok(redaction.redactSensitiveText, "redactSensitiveText not exported");
  const key = "telegram_1234567890/morning-brief-2026-01-01-en.mp3";
  // Control: the old label is redacted, so this is the function that broke it.
  assert.notEqual(redaction.redactSensitiveText(`AUDIO_S3_KEY: ${key}`), `AUDIO_S3_KEY: ${key}`);
  for (const line of [
    `${label}: ${key}`,
    `**${label}:** \`${key}\``,
    `Good morning.\n\nHere is your brief.\n${label}: ${key}\nHave a good day.`,
  ]) {
    for (const [name, redact] of Object.entries(redaction)) {
      assert.equal(redact(line), line, `${name} changed: ${JSON.stringify(line)}`);
    }
  }
});
