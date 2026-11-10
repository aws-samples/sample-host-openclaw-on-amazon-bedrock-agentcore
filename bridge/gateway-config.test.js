/**
 * Tests for the agent defaults rendered by gateway-config.js, and (with
 * OPENCLAW_PACKAGE_DIR set to an installed OpenClaw package) that the rendered
 * config passes OpenClaw's own `openclaw config validate`.
 *
 * Run: cd bridge && node --test gateway-config.test.js
 *      OPENCLAW_PACKAGE_DIR=/usr/local/lib/node_modules/openclaw node --test gateway-config.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { renderConfig } = require("./gateway-config");

describe("agent defaults", () => {
  const defaults = renderConfig({ gatewayToken: "test-token" }).agents.defaults;

  it("keeps OpenClaw's workspace files out of the system prompt", () => {
    assert.equal(defaults.contextInjection, "never");
  });

  it("does not let OpenClaw seed its onboarding templates into the workspace", () => {
    assert.equal(defaults.skipBootstrap, true);
  });
});

const packageDir = process.env.OPENCLAW_PACKAGE_DIR;
describe("rendered config against the installed OpenClaw", {
  skip: packageDir ? false : "Set OPENCLAW_PACKAGE_DIR to an installed OpenClaw package.",
}, () => {
  // `openclaw config validate --json` on a throwaway state dir: it reads the
  // config file and checks it against the schema without starting the gateway.
  function validate(t, config) {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "gateway-config-"));
    t.after(() => fs.rmSync(home, { recursive: true, force: true }));
    const stateDir = path.join(home, ".openclaw");
    const configPath = path.join(stateDir, "openclaw.json");
    fs.mkdirSync(stateDir);
    fs.writeFileSync(configPath, JSON.stringify(config, null, 2));
    const run = spawnSync(process.execPath, [path.join(packageDir, "openclaw.mjs"), "config", "validate", "--json"], {
      env: { HOME: home, OPENCLAW_STATE_DIR: stateDir, OPENCLAW_CONFIG_PATH: configPath, NO_COLOR: "1" },
      encoding: "utf8",
      timeout: 60000,
    });
    assert.equal(run.error, undefined, run.error && run.error.message);
    const result = JSON.parse(run.stdout.slice(run.stdout.indexOf("{")));
    return { status: run.status, result, output: `${run.stdout}\n${run.stderr}` };
  }

  it("passes OpenClaw's config validation with no warnings", (t) => {
    const { status, result, output } = validate(t, renderConfig({ gatewayToken: "test-token" }));
    assert.equal(status, 0, output);
    assert.equal(result.valid, true, output);
    assert.deepEqual(result.warnings, [], output);
  });

  it("rejects the same settings in the wrong place or with an unknown value", (t) => {
    const topLevel = renderConfig({ gatewayToken: "test-token" });
    topLevel.skipBootstrap = true;
    const unknownValue = renderConfig({ gatewayToken: "test-token" });
    unknownValue.agents.defaults.contextInjection = "sometimes";
    for (const [config, key] of [[topLevel, "skipBootstrap"], [unknownValue, "contextInjection"]]) {
      const { status, result, output } = validate(t, config);
      assert.equal(status, 1, output);
      assert.equal(result.valid, false, output);
      assert.ok(JSON.stringify(result.issues).includes(key), output);
    }
  });
});
