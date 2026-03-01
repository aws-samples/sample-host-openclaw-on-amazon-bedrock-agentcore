/**
 * Tests for run.js — Cost analysis agent runner.
 *
 * Tests cover:
 *   1. Argument validation (missing user_id, default-user rejection)
 *   2. System prompt file existence and content
 *   3. SKILL.md frontmatter correctness
 *
 * Run: node --test run.test.js
 */

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { execFileSync } = require("child_process");
const { existsSync, readFileSync } = require("fs");
const path = require("path");

const RUN_JS = path.join(__dirname, "run.js");
const SYSTEM_PROMPT = path.join(__dirname, "system-prompt.md");
const SKILL_MD = path.join(__dirname, "SKILL.md");
const PACKAGE_JSON = path.join(__dirname, "package.json");
const TOKEN_SERVER = path.join(__dirname, "token-usage-server.js");
const AWS_COST_SERVER = path.join(__dirname, "aws-cost-server", "server.py");

// ── Argument validation ──────────────────────────────────────────────────

describe("run.js argument validation", () => {
  it("exits with code 1 when no user_id provided", () => {
    try {
      execFileSync("node", [RUN_JS], {
        encoding: "utf8",
        timeout: 10000,
      });
      assert.fail("Should have exited with error");
    } catch (err) {
      assert.equal(err.status, 1);
      assert.ok(
        err.stderr.includes("Usage: node run.js"),
        "Should print usage message",
      );
    }
  });

  it("exits with code 1 for default-user (hyphen)", () => {
    try {
      execFileSync("node", [RUN_JS, "default-user"], {
        encoding: "utf8",
        timeout: 10000,
      });
      assert.fail("Should have exited with error");
    } catch (err) {
      assert.equal(err.status, 1);
      assert.ok(
        err.stderr.includes("Cannot analyze costs for default-user"),
        "Should reject default-user",
      );
    }
  });

  it("exits with code 1 for default_user (underscore)", () => {
    try {
      execFileSync("node", [RUN_JS, "default_user"], {
        encoding: "utf8",
        timeout: 10000,
      });
      assert.fail("Should have exited with error");
    } catch (err) {
      assert.equal(err.status, 1);
      assert.ok(
        err.stderr.includes("Cannot analyze costs for default-user"),
        "Should reject default_user",
      );
    }
  });

  it("prints usage with user_id and days parameters", () => {
    try {
      execFileSync("node", [RUN_JS], {
        encoding: "utf8",
        timeout: 10000,
      });
    } catch (err) {
      assert.ok(err.stderr.includes("user_id"), "Usage should mention user_id");
      assert.ok(err.stderr.includes("days"), "Usage should mention days");
    }
  });
});

// ── File existence and content ───────────────────────────────────────────

describe("system-prompt.md", () => {
  it("exists", () => {
    assert.ok(existsSync(SYSTEM_PROMPT), "system-prompt.md should exist");
  });

  it("contains cost analysis specialist header", () => {
    const content = readFileSync(SYSTEM_PROMPT, "utf8");
    assert.ok(content.includes("Cost Analysis Specialist"));
  });

  it("references all MCP tools", () => {
    const content = readFileSync(SYSTEM_PROMPT, "utf8");
    assert.ok(
      content.includes("get_detailed_breakdown_by_day"),
      "Should reference Cost Explorer tool",
    );
    assert.ok(
      content.includes("get_bedrock_daily_usage_stats"),
      "Should reference Bedrock stats tool",
    );
    assert.ok(
      content.includes("query_user_usage"),
      "Should reference user usage tool",
    );
    assert.ok(
      content.includes("query_daily_totals"),
      "Should reference daily totals tool",
    );
    assert.ok(
      content.includes("query_top_users"),
      "Should reference top users tool",
    );
  });

  it("includes report format structure", () => {
    const content = readFileSync(SYSTEM_PROMPT, "utf8");
    assert.ok(
      content.includes("Executive Summary"),
      "Should include Executive Summary section",
    );
    assert.ok(
      content.includes("Infrastructure Costs"),
      "Should include Infrastructure Costs section",
    );
    assert.ok(
      content.includes("Recommendations"),
      "Should include Recommendations section",
    );
  });

  it("includes analysis workflow", () => {
    const content = readFileSync(SYSTEM_PROMPT, "utf8");
    assert.ok(
      content.includes("Analysis Workflow"),
      "Should include Analysis Workflow section",
    );
    assert.ok(
      content.includes("Cross-Reference"),
      "Should include cross-referencing instructions",
    );
    assert.ok(
      content.includes("Anomalies"),
      "Should include anomaly detection",
    );
  });
});

describe("SKILL.md", () => {
  it("exists", () => {
    assert.ok(existsSync(SKILL_MD), "SKILL.md should exist");
  });

  it("has correct skill name in frontmatter", () => {
    const content = readFileSync(SKILL_MD, "utf8");
    assert.ok(content.includes("name: cost-analyzer"));
  });

  it("has allowed-tools for Bash node execution", () => {
    const content = readFileSync(SKILL_MD, "utf8");
    assert.ok(content.includes("allowed-tools: Bash(node:*)"));
  });

  it("references run.js as the entry point", () => {
    const content = readFileSync(SKILL_MD, "utf8");
    assert.ok(content.includes("run.js"));
  });

  it("documents user_id and days parameters", () => {
    const content = readFileSync(SKILL_MD, "utf8");
    assert.ok(content.includes("user_id"));
    assert.ok(content.includes("days"));
  });
});

describe("package.json", () => {
  it("exists", () => {
    assert.ok(existsSync(PACKAGE_JSON), "package.json should exist");
  });

  it("has correct name", () => {
    const pkg = JSON.parse(readFileSync(PACKAGE_JSON, "utf8"));
    assert.equal(pkg.name, "cost-analyzer");
  });

  it("is marked private", () => {
    const pkg = JSON.parse(readFileSync(PACKAGE_JSON, "utf8"));
    assert.equal(pkg.private, true);
  });
});

describe("supporting files exist", () => {
  it("token-usage-server.js exists", () => {
    assert.ok(existsSync(TOKEN_SERVER));
  });

  it("aws-cost-server/server.py exists", () => {
    assert.ok(existsSync(AWS_COST_SERVER));
  });

  it("run.js exists", () => {
    assert.ok(existsSync(RUN_JS));
  });
});
