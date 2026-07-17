"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const contract = fs.readFileSync(
  path.join(__dirname, "agentcore-contract.js"),
  "utf8",
);
const dockerfile = fs.readFileSync(path.join(__dirname, "Dockerfile"), "utf8");
const entrypoint = fs.readFileSync(
  path.join(__dirname, "entrypoint.sh"),
  "utf8",
);

function functionBody(source, name, nextMarker) {
  const start = source.indexOf(`function ${name}`);
  assert.notEqual(start, -1, `${name} must exist`);
  const end = source.indexOf(nextMarker, start);
  assert.notEqual(end, -1, `${name} end marker must exist`);
  return source.slice(start, end);
}

describe("AgentCore workspace lifecycle production coupling", () => {
  it("uses the manifest store, SQLite snapshot, and lifecycle state machine", () => {
    assert.match(contract, /require\("\.\/workspace-sync"\)/);
    assert.match(contract, /WorkspaceSnapshotStore/);
    assert.match(contract, /require\("\.\/sqlite-snapshot"\)/);
    assert.match(contract, /SqliteSnapshot/);
    assert.match(contract, /require\("\.\/workspace-lifecycle"\)/);
    assert.match(contract, /WorkspaceLifecycle/);
    assert.match(contract, /require\("\.\/workspace-s3-client"\)/);
    assert.match(contract, /new RefreshingScopedS3\(\)/);
    assert.match(contract, /refreshingS3\.setCredentials\(credentials\)/);
    assert.doesNotMatch(contract, /workspaceSync\.(configureCredentials|getS3Client)/);
  });

  it("restores and verifies the mount before config or either child starts", () => {
    const init = functionBody(contract, "init", "/**\n * Extract plain text");
    const initialize = init.indexOf("await workspaceLifecycle.initialize()");
    const config = init.indexOf("writeOpenClawConfig()");
    const proxy = init.indexOf('spawn("node", ["/app/agentcore-proxy.js"]');
    const openclaw = init.indexOf('spawn(\n      "openclaw"');

    assert.ok(initialize >= 0, "trusted lifecycle initialization must be awaited");
    assert.ok(config > initialize, "config must follow verified restore");
    assert.ok(proxy > initialize, "proxy must follow verified restore");
    assert.ok(openclaw > initialize, "OpenClaw must follow verified restore");
    assert.doesNotMatch(init, /restoreWorkspace|setupSessionStorageSymlink|hasContent/);
    assert.doesNotMatch(init, /skipping S3 restore|session storage is empty/i);
  });

  it("keeps generated config outside state and writes only managed workspace instructions", () => {
    assert.match(
      contract,
      /OPENCLAW_CONFIG_PATH\s*=\s*runtimePolicy\.OPENCLAW_CONFIG_PATH/,
    );
    assert.match(contract, /OPENCLAW_WORKSPACE_DIR/);
    assert.match(contract, /workspace\/AGENTS\.md|OPENCLAW_WORKSPACE_DIR[^;]*AGENTS\.md/s);
    assert.doesNotMatch(contract, /\.openclaw\/openclaw\.json/);
    assert.doesNotMatch(contract, /process\.env\.HOME[^\n]*\.openclaw/);
  });

  it("does not retain the warning-only flat-file sync or copy fallback", () => {
    assert.doesNotMatch(contract, /workspaceSync\.(restoreWorkspace|saveWorkspace|cleanup|startPeriodicSave|setBackupMode)/);
    assert.doesNotMatch(contract, /setupSessionStorageSymlink|cleanupLockFiles/);
    assert.doesNotMatch(contract, /cp -a|execSync/);
    assert.doesNotMatch(contract, /Workspace restore failed.*warn|No session storage/i);
  });

  it("holds every successful chat response until post-turn persistence commits", () => {
    assert.match(contract, /workspaceLifecycle\.beginTurn\(\)/);
    assert.match(contract, /await workspaceLifecycle\.commitAfterTurn\(/);
    assert.match(contract, /WORKSPACE_PERSISTENCE_FAILED/);
    const begin = contract.indexOf("workspaceLifecycle.beginTurn()");
    const execute = contract.indexOf("gatewayRuntimeBoundary.invoke", begin);
    const commit = contract.indexOf(
      "await workspaceLifecycle.commitAfterTurn(",
      execute,
    );
    const response = contract.indexOf("res.end(JSON.stringify", commit);
    assert.ok(begin >= 0 && execute > begin && commit > execute && response > commit);
  });

  it("delegates ordered draining to the lifecycle and exits nonzero on failure", () => {
    const shutdown = functionBody(
      contract,
      "shutdownRuntime",
      "function startContractServer",
    );
    assert.match(shutdown, /await workspaceLifecycle\.shutdown\(\)/);
    assert.match(shutdown, /process\.exit\(0\)/);
    assert.match(shutdown, /process\.exit\(1\)/);
    assert.doesNotMatch(shutdown, /workspaceSync\.cleanup/);
    assert.doesNotMatch(shutdown, /Workspace save timeout.*exit\(0\)/s);
  });
});

describe("runtime image lifecycle contract", () => {
  it("copies every trusted persistence module and a read-only seed", () => {
    for (const file of [
      "workspace-path-policy.js",
      "workspace-manifest.js",
      "sqlite-snapshot.js",
      "workspace-sync.js",
      "workspace-lifecycle.js",
      "workspace-s3-client.js",
    ]) {
      assert.match(dockerfile, new RegExp(`COPY ${file.replaceAll(".", "\\.")} /app/${file.replaceAll(".", "\\.")}`));
    }
    assert.match(dockerfile, /\/opt\/personal-operator\/seed/);
    assert.match(dockerfile, /chmod -R a-w \/opt\/personal-operator\/seed/);
    assert.match(dockerfile, /\/run\/personal-operator/);
  });

  it("starts with strict shell failure handling and fixed runtime paths", () => {
    assert.match(entrypoint, /^set -euo pipefail$/m);
    assert.match(entrypoint, /^umask 077$/m);
    assert.match(
      entrypoint,
      /OPENCLAW_CONFIG_PATH="\/run\/personal-operator\/openclaw\.json"/,
    );
    assert.match(entrypoint, /OPENCLAW_STATE_DIR="\/mnt\/workspace\/live"/);
    assert.match(
      entrypoint,
      /OPENCLAW_WORKSPACE_DIR="\/mnt\/workspace\/live\/workspace"/,
    );
    assert.doesNotMatch(entrypoint, /Do NOT use set -e/i);
  });
});
