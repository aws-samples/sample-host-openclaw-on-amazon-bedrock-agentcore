#!/usr/bin/env node
/**
 * Uninstall a ClawHub community skill.
 * Usage: node uninstall.js <skill_name>
 *
 * Removes the skill from /skills and from the per-user manifest
 * (~/.openclaw/runtime-skills.json), so it is NOT reinstalled when a new
 * session starts. Skills baked into the container image cannot be removed
 * persistently — they come back with the image on every cold start.
 */
const { execFileSync } = require("child_process");
const { validateSkillName, runtimeSkills } = require("./common");

let skillName;
try {
  skillName = validateSkillName(process.argv[2]);
} catch (err) {
  console.error(err.message);
  console.error("Usage: node uninstall.js <skill_name>");
  process.exit(2);
}
const manifestFile = runtimeSkills.manifestPath();

// Forget it first: even if the on-disk removal fails, the skill must not be
// brought back by the cold-start reinstall.
let forgotten = false;
try {
  forgotten = runtimeSkills.recordUninstall(manifestFile, skillName);
  if (forgotten) runtimeSkills.mirrorManifest(manifestFile);
} catch (err) {
  console.error(`Warning: could not update the reinstall manifest: ${err.message}`);
}

const wasPresent = runtimeSkills.isSkillPresent(skillName);
if (!wasPresent) {
  if (forgotten) {
    console.log(`Skill "${skillName}" was not on disk in this session; it will no longer be reinstalled in new sessions.`);
    process.exit(0);
  }
  console.error(`Skill "${skillName}" is not installed.`);
  process.exit(1);
}

try {
  const output = execFileSync("clawhub", runtimeSkills.uninstallArgs(skillName), {
    encoding: "utf-8",
    timeout: 30_000,
    stdio: ["pipe", "pipe", "pipe"],
    env: runtimeSkills.clawhubEnv(),
  });
  console.log(`Successfully uninstalled skill: ${skillName}`);
  if (output.trim()) console.log(output.trim());
  console.log(
    forgotten
      ? "\nIt will not be reinstalled in new sessions. OpenClaw drops it when it next refreshes its skill list (at the latest at the next session start)."
      : "\nOpenClaw drops it when it next refreshes its skill list (at the latest at the next session start).",
  );
} catch (err) {
  const detail = (err.stderr || "").trim() || (err.stdout || "").trim() || err.message;
  if (/Not installed/i.test(detail) && !forgotten) {
    // On disk but unknown to clawhub's lockfile: a skill baked into the image.
    console.error(
      `Cannot uninstall "${skillName}": it ships with this deployment's container image and would return on the next cold start. ` +
        "Ask the operator of this deployment to remove it from the image.",
    );
  } else {
    console.error(`Failed to uninstall skill "${skillName}": ${detail}`);
    if (forgotten) console.error("It will still not be reinstalled in new sessions.");
  }
  process.exit(1);
}
