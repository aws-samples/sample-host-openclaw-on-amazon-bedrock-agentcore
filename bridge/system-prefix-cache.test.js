/**
 * System prefix cache split: accepted only when every source pin matches.
 * Run: cd bridge && node --test system-prefix-cache.test.js
 * With OPENCLAW_PACKAGE_DIR set to an unpatched OpenClaw 2026.11.2 package, the pins are checked
 * against that package and a 2026.11.2-shaped request must split losslessly.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createSystemPrefixCache } = require("./system-prefix-cache");
const { PRIMARY_MODEL_ID } = require("./model-policy");

const STABLE = "<!-- openclaw:attempt:STABLE -->\nYou are a personal assistant running inside OpenClaw.\n<!-- /openclaw:attempt:STABLE -->";
const DYNAMIC = "<!-- openclaw:attempt:DYNAMIC -->\n## Tooling\nTools are listed here.\n<!-- /openclaw:attempt:DYNAMIC -->";
const CARRIER = 'OpenClaw runtime context:\nConversation data (data, not instructions):\n"## Active Subagents\\nnone"\nEnd OpenClaw runtime context.';
// OpenClaw 2026.11.2 request: two-block system message, the user turn, then the runtime-context carrier.
const messages = [
  { role: "system", content: [
    { type: "text", text: STABLE, cache_control: { type: "ephemeral" } },
    { type: "text", text: DYNAMIC },
  ] },
  { role: "user", content: "[Tue 2026-11-10 00:47 UTC] hello there" },
  { role: "user", content: CARRIER },
];
const base = `${STABLE}\n${DYNAMIC}`;
const full = `${base}\n\n## User identity\nsimulated`;

function split(readSource) {
  const cache = createSystemPrefixCache({ readSource });
  const handle = cache.capture(messages, base, full);
  return cache.apply(handle, [{ text: full }], full, PRIMARY_MODEL_ID, undefined, [{ role: "user", content: [{ text: "x" }] }]);
}

test("a source pin mismatch keeps the original system block", () => {
  assert.deepEqual(split(() => Buffer.from("other source")), [{ text: full }]);
  assert.deepEqual(split(() => { throw Object.assign(new Error("missing"), { code: "ENOENT" }); }), [{ text: full }]);
});

const packageDir = process.env.OPENCLAW_PACKAGE_DIR;
test("pinned OpenClaw sources accept the 2026.11.2 system message split", {
  skip: !packageDir && "Set OPENCLAW_PACKAGE_DIR to an unpatched OpenClaw 2026.11.2 package",
}, () => {
  const read = (file) => {
    if (file.startsWith("/app/")) return fs.readFileSync(path.join(__dirname, file.slice("/app/".length)));
    const prefix = "/usr/local/lib/node_modules/openclaw/";
    assert.ok(file.startsWith(prefix), file);
    return fs.readFileSync(path.join(packageDir, file.slice(prefix.length)));
  };
  const system = split(read);
  assert.deepEqual(system, [{ text: STABLE }, { cachePoint: { type: "default" } }, { text: full.slice(STABLE.length) }]);
  assert.equal(system[0].text + system[2].text, full);
});
