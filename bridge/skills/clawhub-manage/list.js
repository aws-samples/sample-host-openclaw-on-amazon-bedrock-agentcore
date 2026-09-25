#!/usr/bin/env node
/**
 * List installed ClawHub skills.
 * Usage: node list.js
 *
 * Shows what is on disk in /skills now, marks the skills the user installed
 * at runtime (recorded in ~/.openclaw/runtime-skills.json and reinstalled
 * automatically in new sessions), and lists manifest entries that are not on
 * disk yet (reinstall pending or failed after this session's cold start).
 */
const fs = require("fs");
const path = require("path");
const { SKILLS_DIR, runtimeSkills } = require("./common");

try {
  const manifest = runtimeSkills.readManifest(runtimeSkills.manifestPath(), {
    log: { log() {}, warn() {}, error() {} },
  });
  const persisted = manifest.skills;

  let onDisk = [];
  if (fs.existsSync(SKILLS_DIR)) {
    onDisk = fs
      .readdirSync(SKILLS_DIR, { withFileTypes: true })
      .filter((e) => e.isDirectory())
      .filter((e) => fs.existsSync(path.join(SKILLS_DIR, e.name, "SKILL.md")))
      .map((e) => e.name)
      .sort();
  }
  const pending = Object.keys(persisted)
    .filter((slug) => !onDisk.includes(slug))
    .sort();

  if (onDisk.length === 0 && pending.length === 0) {
    console.log("No ClawHub skills installed.");
    process.exit(0);
  }

  if (onDisk.length > 0) {
    console.log(`Installed ClawHub skills (${onDisk.length}):\n`);
    for (const skill of onDisk) {
      const entry = persisted[skill];
      const tag = entry
        ? ` (installed by you${entry.version ? `, v${entry.version}` : ""}; reinstalled automatically in new sessions)`
        : "";
      console.log(`  - ${skill}${tag}`);
    }
  }
  if (pending.length > 0) {
    console.log(
      `\nRecorded for automatic reinstall but not on disk yet (${pending.length}) — the reinstall runs a few seconds after a session starts; if a skill stays here, its last reinstall failed:\n`,
    );
    for (const skill of pending) {
      const v = persisted[skill].version;
      console.log(`  - ${skill}${v ? ` (v${v})` : ""}`);
    }
  }
  if (manifest.corrupt) {
    console.log("\nWarning: the reinstall manifest could not be read; skills you installed earlier may not come back. Reinstall them to fix it.");
  }
} catch (err) {
  console.error(`Failed to list skills: ${err.message}`);
  process.exit(1);
}
