/**
 * Repo-side shim. The real module is bridge/runtime-skills.js (shared with
 * agentcore-contract.js, which reinstalls from the manifest at cold start).
 * The Dockerfile copies that file over this one so the skill is
 * self-contained under /skills/clawhub-manage in the image.
 */
module.exports = require("../../runtime-skills");
