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
    .filter((slug) => !onDisk.includes(slug) && !persisted[slug].disabled)
    .sort();
  const disabled = Object.keys(persisted)
    .filter((slug) => persisted[slug].disabled && !onDisk.includes(slug))
    .sort();

  if (onDisk.length === 0 && pending.length === 0 && disabled.length === 0) {
    console.log("No ClawHub skills installed.");
    process.exit(0);
  }

  if (onDisk.length > 0) {
    console.log(`Installed ClawHub skills (${onDisk.length}):\n`);
    for (const skill of onDisk) {
      const entry = persisted[skill];
      if (!entry) {
        console.log(`  - ${skill}`);
        continue;
      }
      const parts = [];
      if (entry.version) parts.push(`v${entry.version}`);
      if (entry.installedAt) parts.push(`at ${entry.installedAt}`);
      if (entry.installedBy) parts.push(`by user ${entry.installedBy}`);
      if (entry.installedSession) parts.push(`in session ${entry.installedSession}`);
      const provenance = parts.length ? ` (installed ${parts.join(", ")}` : " (installed by this environment";
      const verified = entry.integrity ? "" : ", not checksum-verified";
      const warning = !entry.installedBy && !entry.installedSession
        ? "; WARNING: no install provenance recorded — this entry may have been added directly to the manifest without going through `node /skills/clawhub-manage/install.js`"
        : "";
      console.log(`  - ${skill}${provenance}; reinstalled automatically in new sessions${verified}${warning})`);
    }
  }
  if (pending.length > 0) {
    console.log(
      `\nRecorded for automatic reinstall but not on disk yet (${pending.length}) — the reinstall runs a few seconds after a session starts; if a skill stays here, its last reinstall failed or did not fit in the session's reinstall time budget and is retried in the next session (install it again to restore it now):\n`,
    );
    for (const skill of pending) {
      const entry = persisted[skill];
      const parts = [];
      if (entry.version) parts.push(`v${entry.version}`);
      if (entry.installedAt) parts.push(`recorded ${entry.installedAt}`);
      if (entry.installedBy) parts.push(`by user ${entry.installedBy}`);
      if (entry.installedSession) parts.push(`in session ${entry.installedSession}`);
      const suffix = parts.length ? ` (${parts.join(", ")})` : "";
      const warning = !entry.installedBy && !entry.installedSession
        ? "  [WARNING: no install provenance recorded]"
        : "";
      console.log(`  - ${skill}${suffix}${warning}`);
    }
  }
  if (disabled.length > 0) {
    console.log(
      `\nQuarantined (${disabled.length}) — these entries are skipped on cold start so one unresolvable or changed skill cannot block activation. Reinstall the skill with 'install <slug>' to try again:\n`,
    );
    for (const skill of disabled) {
      const { version: v, disabledReason } = persisted[skill];
      const why = disabledReason === "integrity"
        ? "reinstalled files did not match the ones originally installed"
        : "repeated reinstall failures";
      console.log(`  - ${skill}${v ? ` (v${v})` : ""}: ${why}`);
    }
  }
  if (manifest.corrupt) {
    console.log("\nWarning: the reinstall manifest could not be read; skills you installed earlier may not come back. Reinstall them to fix it.");
  }
} catch (err) {
  console.error(`Failed to list skills: ${err.message}`);
  process.exit(1);
}
