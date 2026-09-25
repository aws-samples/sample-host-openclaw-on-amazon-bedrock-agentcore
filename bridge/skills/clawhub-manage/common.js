/**
 * Shared utilities for clawhub-manage skill.
 *
 * Slug validation, the skills directory and the persisted-install manifest all
 * live in ./runtime-skills.js so agentcore-contract.js can reuse them at cold
 * start; this file only re-exports what the scripts here need.
 */
const runtimeSkills = require("./runtime-skills");

module.exports = {
  validateSkillName: runtimeSkills.validateSkillName,
  SKILLS_DIR: runtimeSkills.SKILLS_DIR,
  runtimeSkills,
};
