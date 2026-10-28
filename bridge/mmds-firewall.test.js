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

// Destination addresses of every rule appended (-A), in order.
function appendedDestinations(rules) {
  return rules.split("\n").filter((line) => / -A /.test(line)).map((line) => /-d (\S+)/.exec(line)[1]);
}

const METADATA_V4 = "169.254.169.254/32";
const CREDENTIALS_V4 = "169.254.170.2/32";
const METADATA_V6 = "fd00:ec2::254";

test("installs owner-match drops for the metadata and credential addresses and reports active", (t) => {
  const box = sandbox(t, { exitCode: 0 });
  const result = box.run();
  assert.equal(result.status, 0);
  assert.equal(status(result), "active");
  const rules = box.rules();
  assert.deepEqual(appendedDestinations(rules), [METADATA_V4, CREDENTIALS_V4, METADATA_V6]);
  assert.ok(rules.includes(`iptables -A OUTPUT -m owner --uid-owner 10001 -d ${METADATA_V4} -j DROP`));
  assert.ok(rules.includes(`iptables -A OUTPUT -m owner --uid-owner 10001 -d ${CREDENTIALS_V4} -j DROP`));
  assert.ok(rules.includes(`ip6tables -A OUTPUT -m owner --uid-owner 10001 -d ${METADATA_V6} -j DROP`));
  // Idempotent: it deletes a prior copy before adding.
  assert.ok(rules.includes(`iptables -D OUTPUT -m owner --uid-owner 10001 -d ${METADATA_V4} -j DROP`));
  assert.ok(rules.includes(`iptables -D OUTPUT -m owner --uid-owner 10001 -d ${CREDENTIALS_V4} -j DROP`));
});

test("does not block the whole link-local range, so DNS and time sync keep working", (t) => {
  const box = sandbox(t, { exitCode: 0 });
  box.run();
  const rules = box.rules();
  // No /16 (or any wider prefix) over link-local: the VPC resolver (.253) and
  // the time sync service (.123) must stay reachable for the agent uid.
  assert.ok(!rules.includes("169.254.0.0/16"), rules);
  for (const dest of appendedDestinations(rules)) {
    if (dest.includes(".")) assert.ok(dest.endsWith("/32"), `IPv4 rule must be a single host, got ${dest}`);
  }
  assert.ok(!rules.includes("169.254.169.253"));
  assert.ok(!rules.includes("169.254.169.123"));
});

test("honours an overridden agent uid", (t) => {
  const box = sandbox(t, { exitCode: 0 });
  const result = box.run({ OPENCLAW_RUNTIME_UID: "20002" });
  assert.equal(status(result), "active");
  assert.ok(box.rules().includes(`--uid-owner 20002 -d ${METADATA_V4}`));
  assert.ok(box.rules().includes(`--uid-owner 20002 -d ${CREDENTIALS_V4}`));
});

test("fails safe (inactive, exit 0) when the firewall tool is absent", (t) => {
  const box = sandbox(t, { tools: [] }); // no iptables/ip6tables on PATH
  const result = box.run();
  assert.equal(result.status, 0, "must not fail the container");
  assert.equal(status(result), "inactive");
  assert.match(result.stdout, /not present/);
  assert.match(result.stdout, /INACTIVE/);
});

test("fails safe (inactive, exit 0) when the rule is refused (no CAP_NET_ADMIN)", (t) => {
  const box = sandbox(t, { exitCode: 1 });
  const result = box.run();
  assert.equal(result.status, 0, "must not fail the container");
  assert.equal(status(result), "inactive");
  assert.match(result.stdout, /refused the rule|NOT blocked/);
  assert.match(result.stdout, /INACTIVE/);
});

test("reports inactive if only IPv6 cannot be blocked", (t) => {
  // iptables present and working, ip6tables absent.
  const box = sandbox(t, { tools: ["iptables"], exitCode: 0 });
  const result = box.run();
  assert.equal(status(result), "inactive");
  assert.deepEqual(appendedDestinations(box.rules()), [METADATA_V4, CREDENTIALS_V4]);
});
