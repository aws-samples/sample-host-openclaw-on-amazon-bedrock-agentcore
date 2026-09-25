#!/usr/bin/env node
/**
 * Install a ClawHub community skill.
 * Usage: node install.js <skill_name>
 *
 * The skill is downloaded into /skills (where OpenClaw scans for skills and
 * where it can resolve /app/node_modules) and recorded, with its pinned
 * version, in the per-user manifest ~/.openclaw/runtime-skills.json. That
 * manifest is part of the state that survives a microVM cold start, and the
 * bridge reinstalls every skill in it in the background shortly after a new
 * session starts — /skills itself is not persisted.
 *
 * No `--force`: clawhub uses that flag to bypass its "flagged for security
 * review" check in non-interactive mode. A flagged skill therefore fails here
 * with a clear message instead of being installed silently.
 */
const { execFileSync } = require("child_process");
const { validateSkillName, runtimeSkills } = require("./common");

let skillName;
try {
  skillName = validateSkillName(process.argv[2]);
} catch (err) {
  console.error(err.message);
  console.error("Usage: node install.js <skill_name>");
  process.exit(2);
}
const manifestFile = runtimeSkills.manifestPath();

if (runtimeSkills.isSkillPresent(skillName)) {
  const manifest = runtimeSkills.readManifest(manifestFile);
  if (manifest.skills[skillName]) {
    console.log(`Skill "${skillName}" is already installed and will be reinstalled automatically in new sessions.`);
  } else {
    console.log(`Skill "${skillName}" is already installed (it ships with this deployment).`);
  }
  process.exit(0);
}

let installedVersion = null;
try {
  const output = execFileSync("clawhub", runtimeSkills.installArgs(skillName), {
    encoding: "utf-8",
    timeout: 90_000,
    stdio: ["pipe", "pipe", "pipe"],
    env: runtimeSkills.clawhubEnv(),
  });
  if (!runtimeSkills.isSkillPresent(skillName)) {
    throw new Error(`clawhub reported success but ${runtimeSkills.SKILLS_DIR}/${skillName}/SKILL.md is missing`);
  }
  installedVersion = runtimeSkills.readInstalledVersion(skillName);
  console.log(`Successfully installed skill: ${skillName}${installedVersion ? ` (v${installedVersion})` : ""}`);
  if (output.trim()) console.log(output.trim());
} catch (err) {
  const detail = (err.stderr || "").trim() || (err.stdout || "").trim() || err.message;
  if (runtimeSkills.isSecurityRefusal(detail) || runtimeSkills.isSecurityRefusal(err.stdout)) {
    console.error(
      `Refused to install skill "${skillName}": ClawHub has flagged it for security review ` +
        "(it may contain risky patterns such as credential access, external API calls or eval). " +
        "Installs from this environment never bypass that check. Review the skill on clawhub.ai; " +
        "if you still want it, ask the operator of this deployment to bake it into the image.",
    );
  } else {
    console.error(`Failed to install skill "${skillName}": ${detail}`);
  }
  process.exit(1);
}

// Record the install so it comes back after a cold start. A manifest write
// failure must not be reported as an install failure — the skill IS on disk —
// but the user needs to know it will not persist.
try {
  runtimeSkills.recordInstall(manifestFile, skillName, installedVersion);
  runtimeSkills.mirrorManifest(manifestFile);
  console.log(
    "\nOpenClaw loads the skill when it next refreshes its skill list (on file change, or at the latest " +
      "at the next session start). It is reinstalled automatically a few seconds after each new session starts.",
  );
} catch (err) {
  console.error(
    `Warning: installed, but could not record "${skillName}" for reinstall on new sessions (${err.message}). ` +
      "It will be gone after the next cold start unless you install it again.",
  );
}
