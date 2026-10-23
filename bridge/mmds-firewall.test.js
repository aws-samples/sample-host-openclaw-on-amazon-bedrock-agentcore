const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const SCRIPT = path.join(__dirname, "mmds-firewall.sh");

// Build a throwaway bin dir holding mock iptables/ip6tables that log their argv
// to $RULE_LOG and exit with $IPT_EXIT. Only the named tools are provided, so a
// test can simulate a tool being absent by simply not creating it.
function sandbox(t, { tools = ["iptables", "ip6tables"], exitCode = 0 } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "mmds-fw-"));
  const ruleLog = path.join(dir, "rules.log");
  for (const tool of tools) {
    const file = path.join(dir, tool);
    fs.writeFileSync(file,
      `#!/bin/bash\necho "${tool} $*" >> "${ruleLog}"\nexit \${IPT_EXIT:-0}\n`, { mode: 0o755 });
  }
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  // A minimal PATH that contains ONLY our mock bin plus the real bash/coreutils,
  // so `command -v iptables` resolves to the mock (or fails when absent).
  const run = (env = {}) => spawnSync("/bin/bash", [SCRIPT], {
    encoding: "utf8",
    env: {
      PATH: `${dir}:/usr/bin:/bin`,
      OPENCLAW_RUNTIME_UID: "10001",
      IPT_EXIT: String(exitCode),
      ...env,
    },
  });
  const rules = () => (fs.existsSync(ruleLog) ? fs.readFileSync(ruleLog, "utf8") : "");
  return { run, rules };
}

function status(result) {
  const line = result.stdout.trim().split("\n").at(-1);
  const match = /^MMDS_BLOCK_STATUS=(\w+)$/.exec(line);
  assert.ok(match, `last line should be the status, got: ${JSON.stringify(line)}`);
  return match[1];
}

test("installs owner-match drops for both metadata addresses and reports active", (t) => {
  const box = sandbox(t, { exitCode: 0 });
  const result = box.run();
  assert.equal(result.status, 0);
  assert.equal(status(result), "active");
  const rules = box.rules();
  // IPv4 link-local metadata blocked from the agent uid, not uid 0.
  assert.match(rules, /iptables -A OUTPUT -m owner --uid-owner 10001 -d 169\.254\.0\.0\/16 -j DROP/);
  // IPv6 metadata blocked too.
  assert.match(rules, /ip6tables -A OUTPUT -m owner --uid-owner 10001 -d fd00:ec2::254 -j DROP/);
  // Idempotent: it deletes a prior copy before adding.
  assert.match(rules, /iptables -D OUTPUT -m owner --uid-owner 10001 -d 169\.254\.0\.0\/16 -j DROP/);
});

test("honours an overridden agent uid", (t) => {
  const box = sandbox(t, { exitCode: 0 });
  const result = box.run({ OPENCLAW_RUNTIME_UID: "20002" });
  assert.equal(status(result), "active");
  assert.match(box.rules(), /--uid-owner 20002 -d 169\.254\.0\.0\/16/);
});

test("fails safe (inactive, exit 0) when the firewall tool is absent", (t) => {
  const box = sandbox(t, { tools: [] }); // no iptables/ip6tables on PATH
  const result = box.run();
  assert.equal(result.status, 0, "must not fail the container");
  assert.equal(status(result), "inactive");
  assert.match(result.stdout, /not present/);
});

test("fails safe (inactive, exit 0) when the rule is refused (no CAP_NET_ADMIN)", (t) => {
  const box = sandbox(t, { exitCode: 1 });
  const result = box.run();
  assert.equal(result.status, 0, "must not fail the container");
  assert.equal(status(result), "inactive");
  assert.match(result.stdout, /refused the rule|NOT blocked/);
});

test("reports inactive if only IPv6 cannot be blocked", (t) => {
  // iptables present and working, ip6tables absent.
  const box = sandbox(t, { tools: ["iptables"], exitCode: 0 });
  const result = box.run();
  assert.equal(status(result), "inactive");
  assert.match(box.rules(), /iptables -A OUTPUT .* -d 169\.254\.0\.0\/16 -j DROP/);
});
