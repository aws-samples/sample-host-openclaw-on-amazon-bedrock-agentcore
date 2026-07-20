"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const source = fs.readFileSync(
  path.join(__dirname, "agentcore-proxy.js"),
  "utf8",
);
const identityTestSource = fs.readFileSync(
  path.join(__dirname, "proxy-identity.test.js"),
  "utf8",
);

describe("Bedrock proxy runtime boundary", () => {
  it("uses only the trusted base system text without workspace augmentation", () => {
    assert.doesNotMatch(source, /WORKSPACE_FILES|WORKSPACE_DEFAULTS/);
    assert.doesNotMatch(
      source,
      /buildUserIdentityContext|retrieveMemoryContext|ensureWorkspaceFiles/,
    );
    assert.doesNotMatch(source, /AGENTS\.md|SOUL\.md|TOOLS\.md|MEMORY\.md/);
    assert.match(source, /const systemTextOverride = baseSystemText;/);
  });

  it("does not inspect legacy skill directories or advertise forbidden tools", () => {
    assert.doesNotMatch(source, /\/skills(?:\/|\")/);
    assert.doesNotMatch(source, /installed_skills|s3_skill_exists/);
    assert.doesNotMatch(
      source,
      /eventbridge-cron|clawhub-manage|s3-user-files|openclaw-mem/,
    );
  });

  it("keeps identity tests coupled to production behavior instead of mirrored prompt code", () => {
    assert.doesNotMatch(identityTestSource, /WORKSPACE_FILES|WORKSPACE_DEFAULTS/);
    assert.doesNotMatch(identityTestSource, /buildUserIdentityContext/);
    assert.match(identityTestSource, /require\("\.\/agentcore-proxy"\)/);
    assert.match(identityTestSource, /resolveRuntimeIdentity/);
  });

  it("contains no caller-derived identity, mutable identity file, or Cognito actor path", () => {
    assert.doesNotMatch(source, /current-identity|default-user/);
    assert.doesNotMatch(source, /x-openclaw-actor-id|x-openclaw-session-id/);
    assert.doesNotMatch(source, /extractSessionMetadata|metadata-json|message-name/);
    assert.doesNotMatch(source, /\bUSER_ID\b/);
    assert.doesNotMatch(source, /Cognito|cognito|client-cognito-identity-provider/);
    assert.match(source, /resolveRuntimeIdentity\(process\.env\)/);
    assert.match(source, /RUNTIME_IDENTITY\.namespace/);
  });
});
