/**
 * Tests for runtime-skills.js — the per-user manifest of ClawHub skills a
 * user installs at runtime through the clawhub-manage skill, and the
 * required reinstall agentcore-contract.js runs before gateway activation.
 *
 * Run: cd bridge && node --test runtime-skills.test.js
 */
const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const os = require("os");
const path = require("path");

const rs = require("./runtime-skills");

const quietLog = { log() {}, warn() {}, error() {} };

function mkTmp(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function collectingLog() {
  const lines = [];
  return {
    lines,
    log: (m) => lines.push(`log:${m}`),
    warn: (m) => lines.push(`warn:${m}`),
    error: (m) => lines.push(`error:${m}`),
  };
}

/** Make `<skillsDir>/<slug>/SKILL.md` (+ optional clawhub origin.json). */
function placeSkill(skillsDir, slug, { version } = {}) {
  const dir = path.join(skillsDir, slug);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "SKILL.md"), `---\nname: ${slug}\n---\n`);
  if (version !== undefined) {
    fs.mkdirSync(path.join(dir, ".clawhub"), { recursive: true });
    fs.writeFileSync(
      path.join(dir, ".clawhub", "origin.json"),
      JSON.stringify({ version: 1, slug, installedVersion: version, installedAt: 1 }),
    );
  }
}

describe("runtime-skills.validateSkillName / isValidVersion", () => {
  it("accepts ClawHub slugs and lowercases them", () => {
    assert.equal(rs.validateSkillName("baidu-search"), "baidu-search");
    assert.equal(rs.validateSkillName("Reddit-ReadOnly"), "reddit-readonly");
    assert.equal(rs.validateSkillName("a"), "a");
    assert.equal(rs.validateSkillName("x1-2-3"), "x1-2-3");
  });

  it("rejects anything that is not a plain slug (owner prefixes, paths, shell text, flags)", () => {
    for (const bad of [
      undefined,
      null,
      "",
      42,
      "@owner/slug",
      "owner/slug",
      "../etc",
      "skill name",
      "skill;rm -rf /",
      "skill$(id)",
      "-flag",
      "--force",
      "1abc",
      "a".repeat(65),
      "skill_name",
      "skill.js",
    ]) {
      assert.throws(() => rs.validateSkillName(bad), /Skill name is required|Invalid skill name/, `should reject ${JSON.stringify(bad)}`);
    }
  });

  it("accepts semver versions and rejects commit hashes, ranges and shell text", () => {
    for (const ok of ["1.0.0", "0.1.2", "10.20.30", "1.0.0-beta.1", "1.0.0+build.5", "1.2.3-rc.1+meta"]) {
      assert.equal(rs.isValidVersion(ok), true, ok);
    }
    for (const bad of [null, undefined, "", "latest", "1.0", "v1.0.0", "^1.0.0", "1.0.0 --force", "deadbeefcafe", "1.0.0;id", 7, "1.0.0".padEnd(70, "0")]) {
      assert.equal(rs.isValidVersion(bad), false, JSON.stringify(bad));
    }
  });
});

describe("runtime-skills.resolveStateDir / manifestPath", () => {
  it("prefers OPENCLAW_STATE_DIR, then $HOME/.openclaw, then /root/.openclaw", () => {
    assert.equal(rs.resolveStateDir({ OPENCLAW_STATE_DIR: "/state", HOME: "/h" }), "/state");
    assert.equal(rs.resolveStateDir({ HOME: "/h" }), "/h/.openclaw");
    assert.equal(rs.resolveStateDir({}), "/root/.openclaw");
    assert.equal(rs.manifestPath("/h/.openclaw"), "/h/.openclaw/runtime-skills.json");
  });
});

describe("runtime-skills manifest read/write", () => {
  let dir;
  let file;
  beforeEach(() => {
    dir = mkTmp("rs-manifest-");
    file = path.join(dir, "runtime-skills.json");
  });
  afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

  it("recordInstall captures install provenance from the environment", () => {
    rs.recordInstall(file, "baidu-search", "1.0.0", {
      log: quietLog,
      env: { INTERNAL_USER_ID: "user-123", AGENTCORE_RUNTIME_SESSION_ID: "session-abc" },
    });
    const m = rs.readManifest(file, { log: quietLog });
    assert.equal(m.skills["baidu-search"].installedBy, "user-123");
    assert.equal(m.skills["baidu-search"].installedSession, "session-abc");
    assert.match(m.skills["baidu-search"].installedAt, /^\d{4}-\d{2}-\d{2}T/);
  });

  it("recordInstall tolerates missing provenance env vars and does not store empty fields", () => {
    rs.recordInstall(file, "baidu-search", "1.0.0", { log: quietLog, env: {} });
    const raw = JSON.parse(fs.readFileSync(file, "utf8"));
    assert.equal(Object.prototype.hasOwnProperty.call(raw.skills["baidu-search"], "installedBy"), false);
    assert.equal(Object.prototype.hasOwnProperty.call(raw.skills["baidu-search"], "installedSession"), false);
  });

  it("readManifest preserves provenance fields on round-trip and ignores oversized / malformed ones", () => {
    fs.writeFileSync(
      file,
      JSON.stringify({
        version: 1,
        skills: {
          good: { version: "1.0.0", installedBy: "u1", installedSession: "s1", installedAt: "2026-01-01T00:00:00Z" },
          nostr: { version: null, installedBy: 42, installedSession: {} },
          toobig: { version: null, installedBy: "x".repeat(300) },
        },
      }),
    );
    const m = rs.readManifest(file, { log: quietLog });
    assert.equal(m.skills.good.installedBy, "u1");
    assert.equal(m.skills.good.installedSession, "s1");
    assert.equal(m.skills.good.installedAt, "2026-01-01T00:00:00Z");
    assert.equal(m.skills.nostr.installedBy, undefined);
    assert.equal(m.skills.nostr.installedSession, undefined);
    assert.equal(m.skills.toobig.installedBy, undefined);
  });

  it("reads a missing manifest as empty (not corrupt)", () => {
    const m = rs.readManifest(file, { log: quietLog });
    assert.deepEqual(m.skills, {});
    assert.equal(m.corrupt, false);
    assert.equal(m.invalid, 0);
  });

  it("recordInstall writes slug + pinned version and creates the parent dir", () => {
    const nested = path.join(dir, "deep", "state", "runtime-skills.json");
    rs.recordInstall(nested, "Baidu-Search", "1.2.3", { log: quietLog });
    const m = rs.readManifest(nested, { log: quietLog });
    assert.deepEqual(Object.keys(m.skills), ["baidu-search"]);
    assert.equal(m.skills["baidu-search"].version, "1.2.3");
    assert.match(m.skills["baidu-search"].installedAt, /^\d{4}-\d{2}-\d{2}T/);
    const raw = JSON.parse(fs.readFileSync(nested, "utf8"));
    assert.equal(raw.version, 1);
    assert.equal(fs.readdirSync(path.dirname(nested)).filter((n) => n.includes(".tmp-")).length, 0, "no temp file left behind");
  });

  it("recordInstall stores null for an unpinnable version and keeps other entries", () => {
    rs.recordInstall(file, "one", "1.0.0", { log: quietLog });
    rs.recordInstall(file, "two", "abc123def", { log: quietLog }); // git commit — not re-resolvable
    rs.recordInstall(file, "three", null, { log: quietLog });
    const m = rs.readManifest(file, { log: quietLog });
    assert.deepEqual(Object.keys(m.skills), ["one", "three", "two"]); // sorted on write
    assert.equal(m.skills.one.version, "1.0.0");
    assert.equal(m.skills.two.version, null);
    assert.equal(m.skills.three.version, null);
  });

  it("recordInstall of an existing slug replaces the version", () => {
    rs.recordInstall(file, "one", "1.0.0", { log: quietLog });
    rs.recordInstall(file, "one", "1.1.0", { log: quietLog });
    assert.equal(rs.readManifest(file, { log: quietLog }).skills.one.version, "1.1.0");
  });

  it("recordUninstall removes the entry and reports whether it existed", () => {
    rs.recordInstall(file, "one", "1.0.0", { log: quietLog });
    rs.recordInstall(file, "two", "2.0.0", { log: quietLog });
    assert.equal(rs.recordUninstall(file, "one", { log: quietLog }), true);
    assert.equal(rs.recordUninstall(file, "one", { log: quietLog }), false);
    assert.equal(rs.recordUninstall(file, "never", { log: quietLog }), false);
    assert.deepEqual(Object.keys(rs.readManifest(file, { log: quietLog }).skills), ["two"]);
  });

  it("recordUninstall on a missing manifest is a no-op that creates nothing", () => {
    assert.equal(rs.recordUninstall(file, "one", { log: quietLog }), false);
    assert.equal(fs.existsSync(file), false);
  });

  it("treats unparseable JSON as corrupt + empty and logs a warning", () => {
    fs.writeFileSync(file, "{ not json");
    const log = collectingLog();
    const m = rs.readManifest(file, { log });
    assert.equal(m.corrupt, true);
    assert.deepEqual(m.skills, {});
    assert.equal(log.lines.filter((l) => l.startsWith("warn:") && /not valid JSON/.test(l)).length, 1);
  });

  it("treats the wrong shape (array / missing skills / skills as array) as corrupt", () => {
    for (const bad of ["[]", "null", '"str"', '{"version":1}', '{"skills":[]}', '{"skills":"x"}']) {
      fs.writeFileSync(file, bad);
      const m = rs.readManifest(file, { log: quietLog });
      assert.equal(m.corrupt, true, bad);
      assert.deepEqual(m.skills, {}, bad);
    }
  });

  it("drops entries with invalid slugs or versions, keeps the valid ones, counts the drops", () => {
    fs.writeFileSync(
      file,
      JSON.stringify({
        version: 1,
        skills: {
          good: { version: "1.0.0" },
          unpinned: { version: null },
          legacy: {}, // no version key -> unpinned
          "@owner/qualified": { version: "1.0.0" },
          "../traversal": { version: "1.0.0" },
          "bad version": { version: "1.0.0" },
          "bad-version": { version: "1.0.0 --force" },
          "bad-version-type": { version: 1 },
          "not-an-object": "1.0.0",
        },
      }),
    );
    const log = collectingLog();
    const m = rs.readManifest(file, { log });
    assert.equal(m.corrupt, false);
    assert.deepEqual(Object.keys(m.skills).sort(), ["good", "legacy", "not-an-object", "unpinned"]);
    assert.equal(m.skills.good.version, "1.0.0");
    assert.equal(m.skills.unpinned.version, null);
    assert.equal(m.skills.legacy.version, null);
    assert.equal(m.skills["not-an-object"].version, null);
    assert.equal(m.invalid, 5);
    assert.equal(log.lines.filter((l) => l.startsWith("warn:")).length, 5);
  });

  it("a corrupt manifest is overwritten cleanly by the next recordInstall", () => {
    fs.writeFileSync(file, "garbage");
    rs.recordInstall(file, "one", "1.0.0", { log: quietLog });
    const m = rs.readManifest(file, { log: quietLog });
    assert.equal(m.corrupt, false);
    assert.deepEqual(Object.keys(m.skills), ["one"]);
  });

  it("writeManifest never persists an invalid slug or version", () => {
    rs.writeManifest(file, {
      skills: { ok: { version: "1.0.0" }, "@bad/slug": { version: "1.0.0" }, weird: { version: "not-semver" } },
    });
    const raw = JSON.parse(fs.readFileSync(file, "utf8"));
    assert.deepEqual(raw.skills, { ok: { version: "1.0.0" }, weird: { version: null } });
  });
});

describe("runtime-skills.mirrorManifest", () => {
  let dir;
  beforeEach(() => (dir = mkTmp("rs-mirror-")));
  afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

  it("copies the manifest onto an existing mount state dir", () => {
    const file = path.join(dir, "state", "runtime-skills.json");
    const mount = path.join(dir, "mnt", ".openclaw");
    fs.mkdirSync(mount, { recursive: true });
    rs.recordInstall(file, "one", "1.0.0", { log: quietLog });
    assert.equal(rs.mirrorManifest(file, mount, { log: quietLog }), true);
    assert.equal(fs.readFileSync(path.join(mount, "runtime-skills.json"), "utf8"), fs.readFileSync(file, "utf8"));
    assert.equal(fs.readdirSync(mount).filter((n) => n.includes(".tmp-")).length, 0);
  });

  it("is a no-op when there is no mount or no manifest", () => {
    const file = path.join(dir, "state", "runtime-skills.json");
    assert.equal(rs.mirrorManifest(file, path.join(dir, "nope"), { log: quietLog }), false);
    fs.mkdirSync(path.join(dir, "mount"));
    assert.equal(rs.mirrorManifest(file, path.join(dir, "mount"), { log: quietLog }), false);
    assert.deepEqual(fs.readdirSync(path.join(dir, "mount")), []);
  });
});

describe("runtime-skills clawhub argv", () => {
  it("installArgs pins the location, keeps --no-input and never passes --force", () => {
    const args = rs.installArgs("baidu-search", "1.2.3", "/skills");
    assert.deepEqual(args, ["install", "baidu-search", "--version", "1.2.3", "--no-input", "--workdir", "/skills", "--dir", "/skills"]);
    assert.equal(args.includes("--force"), false);
    assert.equal(args.includes("--force-install"), false);
  });

  it("installArgs omits --version when unpinned", () => {
    assert.deepEqual(rs.installArgs("Baidu-Search", null), ["install", "baidu-search", "--no-input", "--workdir", "/skills", "--dir", "/skills"]);
    assert.equal(rs.installArgs("x", undefined).includes("--version"), false);
  });

  it("installArgs refuses invalid slugs and versions before anything reaches the CLI", () => {
    assert.throws(() => rs.installArgs("@owner/slug", "1.0.0"), /Invalid skill name/);
    assert.throws(() => rs.installArgs("ok", "1.0.0 --force"), /Invalid skill version/);
    assert.throws(() => rs.installArgs("ok", "latest"), /Invalid skill version/);
    assert.throws(() => rs.installArgs("ok", "deadbeef"), /Invalid skill version/);
  });

  it("uninstallArgs passes --yes (clawhub refuses to uninstall with prompts off otherwise) and no --force", () => {
    const args = rs.uninstallArgs("One");
    assert.deepEqual(args, ["uninstall", "one", "--yes", "--no-input", "--workdir", "/skills", "--dir", "/skills"]);
    assert.throws(() => rs.uninstallArgs("a b"), /Invalid skill name/);
  });

  it("the shipped skill scripts and Dockerfile runtime path contain no --force", () => {
    const skillDir = path.join(__dirname, "skills", "clawhub-manage");
    for (const name of ["install.js", "uninstall.js", "list.js", "common.js"]) {
      const src = fs.readFileSync(path.join(skillDir, name), "utf8");
      const code = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
      assert.equal(/--force/.test(code), false, `${name} must not pass --force`);
    }
    const mod = fs.readFileSync(path.join(__dirname, "runtime-skills.js"), "utf8");
    const modCode = mod.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
    assert.equal(/"--force"/.test(modCode), false, "runtime-skills.js must not pass --force");
  });

  it("isSecurityRefusal recognises clawhub's review/malware refusals only", () => {
    assert.equal(rs.isSecurityRefusal('Warning: "x" is flagged for ClawHub security review.'), true);
    assert.equal(rs.isSecurityRefusal("Use --force to install suspicious skills in non-interactive mode"), true);
    assert.equal(rs.isSecurityRefusal("Blocked: x is flagged as malicious"), true);
    assert.equal(rs.isSecurityRefusal("This skill has been flagged as malware and cannot be installed."), true);
    assert.equal(rs.isSecurityRefusal("Already installed: /skills/x (use --force)"), false);
    assert.equal(rs.isSecurityRefusal("ETIMEDOUT"), false);
    assert.equal(rs.isSecurityRefusal(undefined), false);
  });

  it("clawhubEnv keeps PATH/HOME and CLAWHUB_* settings and drops AWS credentials and CLAWHUB_WORKDIR", () => {
    const env = rs.clawhubEnv({
      PATH: "/usr/bin",
      HOME: "/root",
      NODE_PATH: "/app/node_modules",
      AWS_ACCESS_KEY_ID: "AKIA",
      AWS_SECRET_ACCESS_KEY: "s",
      AWS_SESSION_TOKEN: "t",
      AWS_CONTAINER_CREDENTIALS_FULL_URI: "http://x",
      AWS_REGION: "us-west-2",
      CLAWHUB_REGISTRY: "https://registry.example",
      CLAWHUB_WORKDIR: "/somewhere",
      CLAWDHUB_SITE: "https://site.example",
      INTERNAL_USER_ID: "u",
    });
    assert.deepEqual(env, {
      PATH: "/usr/bin",
      HOME: "/root",
      NODE_PATH: "/app/node_modules",
      CLAWHUB_REGISTRY: "https://registry.example",
      CLAWDHUB_SITE: "https://site.example",
    });
  });
});

describe("runtime-skills.parseAllowlist / isAllowed", () => {
  it("returns null when the env var is unset or empty (back-compat: no allowlist)", () => {
    assert.equal(rs.parseAllowlist({}, { log: quietLog }), null);
    assert.equal(rs.parseAllowlist({ RUNTIME_SKILLS_ALLOWLIST: "" }, { log: quietLog }), null);
    assert.equal(rs.parseAllowlist({ RUNTIME_SKILLS_ALLOWLIST: "   \t  " }, { log: quietLog }), null);
  });

  it("parses comma / whitespace separated slugs, lowercases, drops invalid tokens with a warning", () => {
    const log = collectingLog();
    const list = rs.parseAllowlist(
      { RUNTIME_SKILLS_ALLOWLIST: "alpha, Beta ,gamma\n@owner/slug, ../bad\tdelta" },
      { log },
    );
    assert.ok(list instanceof Set);
    assert.deepEqual([...list].sort(), ["alpha", "beta", "delta", "gamma"]);
    assert.equal(log.lines.filter((l) => l.startsWith("warn:")).length, 2);
  });

  it("isAllowed returns true when there is no allowlist, enforces membership otherwise", () => {
    assert.equal(rs.isAllowed("anything", null), true);
    const list = rs.parseAllowlist({ RUNTIME_SKILLS_ALLOWLIST: "alpha,beta" }, { log: quietLog });
    assert.equal(rs.isAllowed("alpha", list), true);
    assert.equal(rs.isAllowed("ALPHA", list), true);
    assert.equal(rs.isAllowed("gamma", list), false);
    assert.equal(rs.isAllowed("", list), false);
    assert.equal(rs.isAllowed(undefined, list), false);
  });
});

describe("runtime-skills.readInstalledVersion / isSkillPresent", () => {
  let skillsDir;
  beforeEach(() => (skillsDir = mkTmp("rs-skills-")));
  afterEach(() => fs.rmSync(skillsDir, { recursive: true, force: true }));

  it("reads the semver clawhub recorded in .clawhub/origin.json", () => {
    placeSkill(skillsDir, "one", { version: "2.3.4" });
    assert.equal(rs.readInstalledVersion("one", skillsDir), "2.3.4");
    assert.equal(rs.isSkillPresent("one", skillsDir), true);
  });

  it("returns null for git-commit versions, missing origin, missing skill and invalid slugs", () => {
    placeSkill(skillsDir, "git", { version: "0123456789abcdef0123456789abcdef01234567" });
    placeSkill(skillsDir, "noorigin");
    fs.mkdirSync(path.join(skillsDir, "nomd"));
    assert.equal(rs.readInstalledVersion("git", skillsDir), null);
    assert.equal(rs.readInstalledVersion("noorigin", skillsDir), null);
    assert.equal(rs.readInstalledVersion("absent", skillsDir), null);
    assert.equal(rs.readInstalledVersion("../one", skillsDir), null);
    assert.equal(rs.isSkillPresent("nomd", skillsDir), false, "a dir without SKILL.md is not a skill");
    assert.equal(rs.isSkillPresent("../one", skillsDir), false);
  });
});

describe("runtime-skills.reinstallFromManifest", () => {
  let dir;
  let skillsDir;
  let manifestFile;
  beforeEach(() => {
    dir = mkTmp("rs-reinstall-");
    skillsDir = path.join(dir, "skills");
    fs.mkdirSync(skillsDir);
    manifestFile = path.join(dir, "state", "runtime-skills.json");
  });
  afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

  /** A fake `clawhub` runner that installs by creating SKILL.md, or fails for `failing` slugs. */
  function fakeRunner({ failing = [], noFiles = [] } = {}) {
    const calls = [];
    const run = async (args) => {
      calls.push(args);
      const slug = args[1];
      if (failing.includes(slug)) {
        const err = new Error("Command failed: clawhub install");
        err.stderr = `⚠️  Warning: "${slug}" is flagged for ClawHub security review.\nUse --force to install suspicious skills in non-interactive mode`;
        throw err;
      }
      if (!noFiles.includes(slug)) placeSkill(skillsDir, slug, { version: "9.9.9" });
      return { stdout: `Installed ${slug}`, stderr: "" };
    };
    return { run, calls };
  }

  it("reinstalls every recorded skill that is missing, pinned to the recorded version, and logs a count", async () => {
    rs.recordInstall(manifestFile, "alpha", "1.0.0", { log: quietLog });
    rs.recordInstall(manifestFile, "beta", null, { log: quietLog });
    const { run, calls } = fakeRunner();
    const log = collectingLog();
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log });
    assert.deepEqual(
      { total: r.total, reinstalled: r.reinstalled, present: r.present, failed: r.failed, corrupt: r.corrupt, invalid: r.invalid },
      { total: 2, reinstalled: 2, present: 0, failed: 0, corrupt: false, invalid: 0 },
    );
    assert.deepEqual(calls, [
      ["install", "alpha", "--version", "1.0.0", "--no-input", "--workdir", skillsDir, "--dir", skillsDir],
      ["install", "beta", "--no-input", "--workdir", skillsDir, "--dir", skillsDir],
    ]);
    for (const args of calls) assert.equal(args.includes("--force"), false);
    assert.equal(rs.isSkillPresent("alpha", skillsDir), true);
    assert.equal(rs.isSkillPresent("beta", skillsDir), true);
    assert.equal(log.lines.filter((l) => /Reinstalled alpha@1\.0\.0/.test(l)).length, 1);
    assert.equal(log.lines.filter((l) => /Reinstalled beta /.test(l)).length, 1);
  });

  it("skips skills already on disk without calling clawhub", async () => {
    rs.recordInstall(manifestFile, "alpha", "1.0.0", { log: quietLog });
    rs.recordInstall(manifestFile, "beta", "2.0.0", { log: quietLog });
    placeSkill(skillsDir, "alpha", { version: "1.0.0" });
    const { run, calls } = fakeRunner();
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.equal(r.present, 1);
    assert.equal(r.reinstalled, 1);
    assert.deepEqual(calls.map((a) => a[1]), ["beta"]);
    assert.deepEqual(r.results.find((x) => x.slug === "alpha"), { slug: "alpha", version: "1.0.0", status: "present" });
  });

  it("one failing skill does not stop the others, is logged, and stays in the manifest for next time", async () => {
    rs.recordInstall(manifestFile, "alpha", "1.0.0", { log: quietLog });
    rs.recordInstall(manifestFile, "flagged", "1.0.0", { log: quietLog });
    rs.recordInstall(manifestFile, "gamma", "3.0.0", { log: quietLog });
    const { run, calls } = fakeRunner({ failing: ["flagged"] });
    const log = collectingLog();
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log });
    assert.equal(r.total, 3);
    assert.equal(r.reinstalled, 2);
    assert.equal(r.failed, 1);
    assert.equal(r.quarantined, 0);
    assert.deepEqual(calls.map((a) => a[1]), ["alpha", "flagged", "gamma"]);
    const failed = r.results.find((x) => x.status === "failed");
    assert.equal(failed.slug, "flagged");
    assert.match(failed.error, /security review|suspicious/);
    assert.equal(log.lines.filter((l) => /warn:.*Reinstall of flagged@1\.0\.0 failed/.test(l)).length, 1);
    assert.equal(rs.isSkillPresent("flagged", skillsDir), false);
    // The failing entry stays in the manifest (not pruned on a single failure),
    // only its `failures` counter is bumped so repeated failures can be
    // quarantined — see the next test.
    const after = rs.readManifest(manifestFile, { log: quietLog });
    assert.deepEqual(Object.keys(after.skills).sort(), ["alpha", "flagged", "gamma"]);
    assert.equal(after.skills.flagged.disabled, undefined);
    assert.equal(after.skills.flagged.failures, 1);
    assert.equal(after.skills.alpha.failures, undefined);
  });

  it("quarantines an entry after MAX_CONSECUTIVE_FAILURES consecutive failures and skips it afterwards", async () => {
    rs.recordInstall(manifestFile, "zz-no-such-skill", "1.0.0", { log: quietLog });
    const { run, calls } = fakeRunner({ failing: ["zz-no-such-skill"] });
    const log = collectingLog();
    // Threshold = 2 to keep the test short; production default is
    // MAX_CONSECUTIVE_FAILURES (3).
    const first = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log, maxConsecutiveFailures: 2 });
    assert.equal(first.failed, 1);
    assert.equal(first.quarantined, 0);
    const second = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log, maxConsecutiveFailures: 2 });
    assert.equal(second.failed, 0);
    assert.equal(second.quarantined, 1);
    const third = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log, maxConsecutiveFailures: 2 });
    assert.equal(third.failed, 0);
    assert.equal(third.quarantined, 0);
    assert.equal(third.skipped, 1);
    // After quarantine the slug is never passed to clawhub again.
    assert.deepEqual(calls.map((a) => a[1]), ["zz-no-such-skill", "zz-no-such-skill"]);
    const m = rs.readManifest(manifestFile, { log: quietLog });
    assert.equal(m.skills["zz-no-such-skill"].disabled, true);
  });

  it("a successful reinstall clears the failure streak and the quarantine flag", async () => {
    rs.recordInstall(manifestFile, "flaky", "1.0.0", { log: quietLog });
    // Pre-seed a near-quarantine state (failures=1, not disabled yet).
    const seeded = rs.readManifest(manifestFile, { log: quietLog });
    seeded.skills.flaky.failures = 1;
    rs.writeManifest(manifestFile, { skills: seeded.skills });
    const { run } = fakeRunner();
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.equal(r.reinstalled, 1);
    const m = rs.readManifest(manifestFile, { log: quietLog });
    assert.equal(m.skills.flaky.failures, undefined);
    assert.equal(m.skills.flaky.disabled, undefined);
  });

  it("recordInstall clears the quarantine so re-installing a disabled slug makes it live again", () => {
    rs.recordInstall(manifestFile, "flaky", "1.0.0", { log: quietLog });
    const seeded = rs.readManifest(manifestFile, { log: quietLog });
    seeded.skills.flaky.failures = 5;
    seeded.skills.flaky.disabled = true;
    rs.writeManifest(manifestFile, { skills: seeded.skills });
    rs.recordInstall(manifestFile, "flaky", "1.1.0", { log: quietLog });
    const m = rs.readManifest(manifestFile, { log: quietLog });
    assert.equal(m.skills.flaky.version, "1.1.0");
    assert.equal(m.skills.flaky.failures, undefined);
    assert.equal(m.skills.flaky.disabled, undefined);
  });

  it("counts a zero exit that left no SKILL.md behind as a failure", async () => {
    rs.recordInstall(manifestFile, "ghost", "1.0.0", { log: quietLog });
    const { run } = fakeRunner({ noFiles: ["ghost"] });
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.equal(r.failed, 1);
    assert.match(r.results[0].error, /SKILL\.md is missing/);
  });

  it("a corrupt manifest reinstalls nothing, is rewritten clean, and does not throw", async () => {
    fs.mkdirSync(path.dirname(manifestFile), { recursive: true });
    fs.writeFileSync(manifestFile, "{{{ definitely not json");
    const { run, calls } = fakeRunner();
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.deepEqual({ total: r.total, reinstalled: r.reinstalled, failed: r.failed, corrupt: r.corrupt }, { total: 0, reinstalled: 0, failed: 0, corrupt: true });
    assert.deepEqual(calls, []);
    // Self-heal: the next read sees a clean manifest so the lockout cannot
    // repeat on every cold start.
    const after = rs.readManifest(manifestFile, { log: quietLog });
    assert.equal(after.corrupt, false);
    assert.deepEqual(after.skills, {});
  });

  it("a missing manifest is simply nothing to do", async () => {
    const { run, calls } = fakeRunner();
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.equal(r.total, 0);
    assert.equal(r.corrupt, false);
    assert.deepEqual(calls, []);
  });

  it("invalid manifest entries never reach the runner, are counted and dropped from the persisted manifest", async () => {
    fs.mkdirSync(path.dirname(manifestFile), { recursive: true });
    fs.writeFileSync(
      manifestFile,
      JSON.stringify({ version: 1, skills: { good: { version: "1.0.0" }, "@evil/slug": { version: "1.0.0" }, bad: { version: "1.0.0; rm -rf /" } } }),
    );
    const { run, calls } = fakeRunner();
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.deepEqual(calls.map((a) => a[1]), ["good"]);
    assert.equal(r.invalid, 2);
    assert.equal(r.total, 1);
    // Self-heal: the invalid entries are pruned so the invalid counter does
    // not re-fire on every subsequent cold start.
    const after = rs.readManifest(manifestFile, { log: quietLog });
    assert.equal(after.invalid, 0);
    assert.deepEqual(Object.keys(after.skills), ["good"]);
  });

  it("a runner that throws synchronously-shaped errors without stderr still yields a summary", async () => {
    rs.recordInstall(manifestFile, "alpha", "1.0.0", { log: quietLog });
    const run = async () => {
      throw new Error("spawn clawhub ENOENT");
    };
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.equal(r.failed, 1);
    assert.match(r.results[0].error, /ENOENT/);
  });

  it("operator allowlist (RUNTIME_SKILLS_ALLOWLIST) refuses manifest entries that are not permitted", async () => {
    rs.recordInstall(manifestFile, "alpha", "1.0.0", { log: quietLog });
    rs.recordInstall(manifestFile, "attacker-slug", null, { log: quietLog });
    rs.recordInstall(manifestFile, "gamma", "3.0.0", { log: quietLog });
    const { run, calls } = fakeRunner();
    const log = collectingLog();
    const r = await rs.reinstallFromManifest({
      manifestFile,
      skillsDir,
      run,
      log,
      env: { RUNTIME_SKILLS_ALLOWLIST: "alpha, gamma" },
    });
    assert.equal(r.total, 3);
    assert.equal(r.refused, 1);
    assert.equal(r.reinstalled, 2);
    assert.deepEqual(calls.map((a) => a[1]).sort(), ["alpha", "gamma"]);
    const refused = r.results.find((x) => x.status === "refused");
    assert.equal(refused.slug, "attacker-slug");
    assert.match(refused.error, /not in operator allowlist/);
    assert.ok(
      log.lines.some((l) => /warn:.*Refusing to reinstall attacker-slug.*RUNTIME_SKILLS_ALLOWLIST/.test(l)),
      log.lines.join("\n"),
    );
  });

  it("an unset or empty allowlist is back-compat: every valid entry is reinstalled", async () => {
    rs.recordInstall(manifestFile, "alpha", "1.0.0", { log: quietLog });
    rs.recordInstall(manifestFile, "beta", null, { log: quietLog });
    const { run, calls } = fakeRunner();
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog, env: {} });
    assert.equal(r.refused, 0);
    assert.equal(r.reinstalled, 2);
    assert.deepEqual(calls.map((a) => a[1]), ["alpha", "beta"]);
    const r2 = await rs.reinstallFromManifest({
      manifestFile,
      skillsDir: path.join(dir, "skills2"),
      run: fakeRunner().run,
      log: quietLog,
      env: { RUNTIME_SKILLS_ALLOWLIST: "   " },
    });
    assert.equal(r2.refused, 0);
  });

  it("the allowlist log notes a slug's recorded installedBy if present", async () => {
    rs.recordInstall(manifestFile, "attacker-slug", null, { log: quietLog, env: { INTERNAL_USER_ID: "victim-user-id" } });
    const { run } = fakeRunner();
    const log = collectingLog();
    await rs.reinstallFromManifest({
      manifestFile,
      skillsDir,
      run,
      log,
      env: { RUNTIME_SKILLS_ALLOWLIST: "only-permitted-slug" },
    });
    assert.ok(
      log.lines.some((l) => /Refusing to reinstall attacker-slug.*installedBy=victim-user-id/.test(l)),
      log.lines.join("\n"),
    );
  });
});

describe("runtime-skills integrity digest", () => {
  let dir;
  let skillsDir;
  let manifestFile;
  beforeEach(() => {
    dir = mkTmp("rs-integrity-");
    skillsDir = path.join(dir, "skills");
    fs.mkdirSync(skillsDir);
    manifestFile = path.join(dir, "state", "runtime-skills.json");
  });
  afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

  /** Fake clawhub that writes `content` into scripts/run.js and origin.json with `version`. */
  function contentRunner(state) {
    const calls = [];
    const run = async (args) => {
      calls.push(args);
      const slug = args[1];
      placeSkill(skillsDir, slug, { version: state.version || "9.9.9" });
      fs.mkdirSync(path.join(skillsDir, slug, "scripts"), { recursive: true });
      fs.writeFileSync(path.join(skillsDir, slug, "scripts", "run.js"), state.content);
      return { stdout: "", stderr: "" };
    };
    return { run, calls };
  }

  function installed(slug, content, version = "1.0.0") {
    placeSkill(skillsDir, slug, { version });
    fs.mkdirSync(path.join(skillsDir, slug, "scripts"), { recursive: true });
    fs.writeFileSync(path.join(skillsDir, slug, "scripts", "run.js"), content);
    return rs.computeSkillIntegrity(slug, skillsDir);
  }

  it("computeSkillIntegrity is stable, ignores .clawhub metadata and changes with the files", () => {
    const a = installed("alpha", "console.log(1)\n");
    assert.match(a, /^sha256:[0-9a-f]{64}$/);
    assert.equal(rs.isValidIntegrity(a), true);
    fs.writeFileSync(path.join(skillsDir, "alpha", ".clawhub", "origin.json"), "{\"installedAt\":2}");
    assert.equal(rs.computeSkillIntegrity("alpha", skillsDir), a);
    fs.writeFileSync(path.join(skillsDir, "alpha", "scripts", "run.js"), "console.log(2)\n");
    const b = rs.computeSkillIntegrity("alpha", skillsDir);
    assert.notEqual(b, a);
    fs.writeFileSync(path.join(skillsDir, "alpha", "extra.js"), "");
    assert.notEqual(rs.computeSkillIntegrity("alpha", skillsDir), b);
    const c = rs.computeSkillIntegrity("alpha", skillsDir);
    fs.symlinkSync("/etc/hostname", path.join(skillsDir, "alpha", "link"));
    assert.notEqual(rs.computeSkillIntegrity("alpha", skillsDir), c);
    assert.equal(rs.computeSkillIntegrity("missing", skillsDir), null);
    assert.equal(rs.computeSkillIntegrity("../alpha", skillsDir), null);
    assert.equal(rs.isValidIntegrity("sha256:xyz"), false);
  });

  it("the manifest round-trips integrity and disabledReason and drops malformed integrity values", () => {
    const digest = `sha256:${"a".repeat(64)}`;
    fs.mkdirSync(path.dirname(manifestFile), { recursive: true });
    fs.writeFileSync(manifestFile, JSON.stringify({
      version: 1,
      skills: {
        good: { version: "1.0.0", integrity: digest },
        legacy: { version: "1.0.0" },
        bad: { version: "1.0.0", integrity: "md5:abc" },
        off: { version: "1.0.0", disabled: true, disabledReason: "integrity" },
        odd: { version: "1.0.0", disabledReason: "integrity" },
      },
    }));
    const m = rs.readManifest(manifestFile, { log: quietLog });
    assert.equal(m.invalid, 1);
    assert.equal(m.skills.bad, undefined);
    assert.equal(m.skills.good.integrity, digest);
    assert.equal("integrity" in m.skills.legacy, false);
    assert.equal(m.skills.off.disabledReason, "integrity");
    assert.equal(m.skills.odd.disabledReason, undefined);
    const out = rs.writeManifest(manifestFile, m);
    assert.equal(out.skills.good.integrity, digest);
    assert.equal(out.skills.off.disabledReason, "integrity");
  });

  it("recordInstall stores the digest and a fresh install clears an integrity quarantine", () => {
    const digest = `sha256:${"b".repeat(64)}`;
    rs.recordInstall(manifestFile, "alpha", "1.0.0", { log: quietLog, integrity: digest });
    const seeded = rs.readManifest(manifestFile, { log: quietLog });
    assert.equal(seeded.skills.alpha.integrity, digest);
    Object.assign(seeded.skills.alpha, { disabled: true, disabledReason: "integrity", failures: 1 });
    rs.writeManifest(manifestFile, seeded);
    const newer = `sha256:${"c".repeat(64)}`;
    rs.recordInstall(manifestFile, "alpha", "1.0.1", { log: quietLog, integrity: newer });
    const m = rs.readManifest(manifestFile, { log: quietLog });
    assert.deepEqual(
      [m.skills.alpha.version, m.skills.alpha.integrity, m.skills.alpha.disabled, m.skills.alpha.disabledReason],
      ["1.0.1", newer, undefined, undefined],
    );
  });

  it("reinstalls a pinned entry whose files match the recorded digest", async () => {
    const digest = installed("alpha", "ok\n");
    fs.rmSync(path.join(skillsDir, "alpha"), { recursive: true });
    rs.recordInstall(manifestFile, "alpha", "1.0.0", { log: quietLog, integrity: digest });
    const { run, calls } = contentRunner({ content: "ok\n", version: "1.0.0" });
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.equal(r.reinstalled, 1);
    assert.equal(r.quarantined, 0);
    assert.deepEqual(calls[0].slice(0, 4), ["install", "alpha", "--version", "1.0.0"]);
    assert.equal(rs.isSkillPresent("alpha", skillsDir), true);
  });

  it("removes and quarantines a reinstall whose files differ from the recorded digest, without retrying it", async () => {
    const digest = installed("alpha", "ok\n");
    fs.rmSync(path.join(skillsDir, "alpha"), { recursive: true });
    rs.recordInstall(manifestFile, "alpha", "1.0.0", { log: quietLog, integrity: digest });
    rs.recordInstall(manifestFile, "beta", "2.0.0", { log: quietLog });
    const { run, calls } = contentRunner({ content: "changed\n", version: "1.0.0" });
    const log = collectingLog();
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log });
    assert.equal(r.quarantined, 1);
    assert.equal(r.failed, 0);
    assert.equal(r.reinstalled, 1, "the other entry is still restored");
    assert.equal(fs.existsSync(path.join(skillsDir, "alpha")), false, "mismatched files are removed before the gateway starts");
    assert.equal(r.results.find((x) => x.slug === "alpha").status, "quarantined");
    assert.ok(log.lines.some((l) => /warn:.*Quarantining alpha@1\.0\.0: reinstalled files do not match/.test(l)));
    const m = rs.readManifest(manifestFile, { log: quietLog });
    assert.equal(m.skills.alpha.disabled, true);
    assert.equal(m.skills.alpha.disabledReason, "integrity");
    assert.equal(m.skills.alpha.integrity, digest, "the approved digest is kept");
    calls.length = 0;
    fs.rmSync(path.join(skillsDir, "beta"), { recursive: true });
    const again = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.equal(again.skipped, 1);
    assert.deepEqual(calls.map((a) => a[1]), ["beta"]);
  });

  it("records a digest for a pinned entry written before digests existed, then verifies it", async () => {
    rs.recordInstall(manifestFile, "alpha", "1.0.0", { log: quietLog });
    const state = { content: "v1\n", version: "1.0.0" };
    const { run } = contentRunner(state);
    const first = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.deepEqual([first.reinstalled, first.migrated, first.failed, first.quarantined], [1, 1, 0, 0]);
    const m = rs.readManifest(manifestFile, { log: quietLog });
    assert.equal(m.skills.alpha.integrity, rs.computeSkillIntegrity("alpha", skillsDir));
    assert.equal(m.skills.alpha.version, "1.0.0");

    fs.rmSync(path.join(skillsDir, "alpha"), { recursive: true });
    const second = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.deepEqual([second.reinstalled, second.migrated], [1, 0]);

    fs.rmSync(path.join(skillsDir, "alpha"), { recursive: true });
    state.content = "v1 changed\n";
    const third = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.equal(third.quarantined, 1);
  });

  it("pins the resolved version and records a digest for an unpinned entry written before digests existed", async () => {
    rs.recordInstall(manifestFile, "beta", null, { log: quietLog });
    const { run, calls } = contentRunner({ content: "b\n", version: "3.1.4" });
    const first = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.deepEqual([first.reinstalled, first.migrated], [1, 1]);
    assert.equal(calls[0].includes("--version"), false);
    assert.equal(first.results[0].version, "3.1.4");
    const m = rs.readManifest(manifestFile, { log: quietLog });
    assert.equal(m.skills.beta.version, "3.1.4");
    assert.ok(rs.isValidIntegrity(m.skills.beta.integrity));
    fs.rmSync(path.join(skillsDir, "beta"), { recursive: true });
    await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.deepEqual(calls[1].slice(0, 4), ["install", "beta", "--version", "3.1.4"]);
  });

  it("an entry with a digest but no version (no semver from clawhub) still has its files verified", async () => {
    const digest = installed("gitty", "g\n", "abc123");
    fs.rmSync(path.join(skillsDir, "gitty"), { recursive: true });
    rs.recordInstall(manifestFile, "gitty", null, { log: quietLog, integrity: digest });
    const state = { content: "g\n", version: "abc123" };
    const { run, calls } = contentRunner(state);
    const ok = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.equal(ok.reinstalled, 1);
    assert.equal(calls[0].includes("--version"), false);
    fs.rmSync(path.join(skillsDir, "gitty"), { recursive: true });
    state.content = "g2\n";
    const bad = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.equal(bad.quarantined, 1);
  });

  it("install.js records the digest and list.js explains an integrity quarantine", () => {
    const install = fs.readFileSync(path.join(__dirname, "skills", "clawhub-manage", "install.js"), "utf-8");
    assert.match(install, /computeSkillIntegrity\(skillName\)/);
    assert.match(install, /recordInstall\(manifestFile, skillName, installedVersion, \{ integrity: installedIntegrity \}\)/);
    const list = fs.readFileSync(path.join(__dirname, "skills", "clawhub-manage", "list.js"), "utf-8");
    assert.match(list, /disabledReason === "integrity"/);
  });
});

describe("runtime-skills integrity", () => {
  let dir;
  let skillsDir;
  let manifestFile;
  beforeEach(() => {
    dir = mkTmp("rs-integrity-");
    skillsDir = path.join(dir, "skills");
    fs.mkdirSync(skillsDir);
    manifestFile = path.join(dir, "state", "runtime-skills.json");
  });
  afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

  /** A fake clawhub that writes `files` (relpath -> content) for each slug. */
  function serving(files) {
    const calls = [];
    const run = async (args) => {
      calls.push(args);
      const slug = args[1];
      const root = path.join(skillsDir, slug);
      for (const [rel, content] of Object.entries(files[slug] || {})) {
        fs.mkdirSync(path.dirname(path.join(root, rel)), { recursive: true });
        fs.writeFileSync(path.join(root, rel), content);
      }
      fs.mkdirSync(path.join(root, ".clawhub"), { recursive: true });
      fs.writeFileSync(path.join(root, ".clawhub", "origin.json"), JSON.stringify({ installedVersion: "1.0.0", installedAt: Date.now() }));
      return { stdout: "", stderr: "" };
    };
    return { run, calls };
  }

  const v1 = { "SKILL.md": "---\nname: alpha\n---\n", "scripts/run.js": "console.log(1)\n" };

  it("computeSkillIntegrity is stable across installs, ignores clawhub metadata, and changes with content, names or symlinks", () => {
    placeSkill(skillsDir, "alpha", { version: "1.0.0" });
    fs.mkdirSync(path.join(skillsDir, "alpha", "lib"));
    fs.writeFileSync(path.join(skillsDir, "alpha", "lib", "a.js"), "a");
    const first = rs.computeSkillIntegrity("alpha", skillsDir);
    assert.ok(rs.isValidIntegrity(first), first);
    fs.writeFileSync(path.join(skillsDir, "alpha", ".clawhub", "origin.json"), JSON.stringify({ installedAt: 2 }));
    assert.equal(rs.computeSkillIntegrity("alpha", skillsDir), first, ".clawhub metadata is excluded");
    fs.writeFileSync(path.join(skillsDir, "alpha", "lib", "a.js"), "b");
    const changed = rs.computeSkillIntegrity("alpha", skillsDir);
    assert.notEqual(changed, first);
    fs.renameSync(path.join(skillsDir, "alpha", "lib", "a.js"), path.join(skillsDir, "alpha", "lib", "c.js"));
    assert.notEqual(rs.computeSkillIntegrity("alpha", skillsDir), changed);
    const beforeLink = rs.computeSkillIntegrity("alpha", skillsDir);
    fs.symlinkSync("/etc/hostname", path.join(skillsDir, "alpha", "link"));
    assert.notEqual(rs.computeSkillIntegrity("alpha", skillsDir), beforeLink);
    assert.equal(rs.computeSkillIntegrity("absent", skillsDir), null);
    assert.equal(rs.computeSkillIntegrity("../alpha", skillsDir), null);
  });

  it("isValidIntegrity accepts only sha256:<64 hex>", () => {
    assert.equal(rs.isValidIntegrity(`sha256:${"a".repeat(64)}`), true);
    for (const bad of [null, undefined, "", `sha256:${"A".repeat(64)}`, `sha512:${"a".repeat(64)}`, `sha256:${"a".repeat(63)}`, 42]) {
      assert.equal(rs.isValidIntegrity(bad), false, String(bad));
    }
  });

  it("recordInstall stores the integrity; read/write round-trip it; an invalid value drops the entry", () => {
    const integrity = `sha256:${"b".repeat(64)}`;
    rs.recordInstall(manifestFile, "alpha", "1.0.0", { integrity, log: quietLog });
    rs.recordInstall(manifestFile, "beta", "1.0.0", { integrity: "garbage", log: quietLog });
    const m = rs.readManifest(manifestFile, { log: quietLog });
    assert.equal(m.skills.alpha.integrity, integrity);
    assert.equal(Object.hasOwn(m.skills.beta, "integrity"), false, "invalid value is not written");
    fs.writeFileSync(manifestFile, JSON.stringify({ version: 1, skills: { alpha: { version: "1.0.0", integrity: "sha256:zz" } } }));
    const bad = rs.readManifest(manifestFile, { log: quietLog });
    assert.equal(bad.invalid, 1);
    assert.deepEqual(bad.skills, {});
  });

  it("reinstall verifies a recorded integrity and loads matching files", async () => {
    const { run } = serving({ alpha: v1 });
    await run(rs.installArgs("alpha", "1.0.0", skillsDir));
    const integrity = rs.computeSkillIntegrity("alpha", skillsDir);
    rs.recordInstall(manifestFile, "alpha", "1.0.0", { integrity, log: quietLog });
    fs.rmSync(path.join(skillsDir, "alpha"), { recursive: true });
    const log = collectingLog();
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log });
    assert.equal(r.reinstalled, 1);
    assert.equal(r.failed, 0);
    assert.equal(r.unverified, 0);
    assert.equal(rs.isSkillPresent("alpha", skillsDir), true);
    assert.ok(log.lines.some((l) => /Reinstalled alpha@1\.0\.0 \(checksum verified\)/.test(l)), log.lines.join("\n"));
  });

  it("reinstall removes files that do not match the recorded integrity and quarantines the entry", async () => {
    const original = serving({ alpha: v1 });
    await original.run(rs.installArgs("alpha", "1.0.0", skillsDir));
    const integrity = rs.computeSkillIntegrity("alpha", skillsDir);
    rs.recordInstall(manifestFile, "alpha", "1.0.0", { integrity, log: quietLog });
    fs.rmSync(path.join(skillsDir, "alpha"), { recursive: true });
    const swapped = serving({ alpha: { ...v1, "scripts/run.js": "console.log(2)\n" } });
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run: swapped.run, log: quietLog });
    assert.equal(r.reinstalled, 0);
    assert.equal(r.failed, 0);
    assert.equal(r.quarantined, 1);
    assert.match(r.results[0].error, /do not match the checksum/);
    assert.equal(fs.existsSync(path.join(skillsDir, "alpha")), false, "mismatching files must not stay where OpenClaw loads skills");
    const m = rs.readManifest(manifestFile, { log: quietLog });
    assert.equal(m.skills.alpha.integrity, integrity, "the approved digest is kept");
    assert.equal(m.skills.alpha.disabledReason, "integrity");
  });

  it("accepts a digest recorded in the earlier fork format and re-records it in the current format", async () => {
    const { run } = serving({ alpha: v1 });
    await run(rs.installArgs("alpha", "1.0.0", skillsDir));
    const current = rs.computeSkillIntegrity("alpha", skillsDir);
    // The earlier format appended a NUL after each regular file's bytes.
    const crypto = require("crypto");
    const h = crypto.createHash("sha256");
    // readdirSync order is sorted, so "SKILL.md" comes before "scripts".
    h.update(`F\0SKILL.md\0${v1["SKILL.md"].length}\0`); h.update(v1["SKILL.md"]); h.update("\0");
    h.update("D\0scripts\0");
    h.update(`F\0scripts/run.js\0${v1["scripts/run.js"].length}\0`); h.update(v1["scripts/run.js"]); h.update("\0");
    const legacy = `sha256:${h.digest("hex")}`;
    assert.notEqual(legacy, current);
    rs.recordInstall(manifestFile, "alpha", "1.0.0", { integrity: legacy, log: quietLog });
    fs.rmSync(path.join(skillsDir, "alpha"), { recursive: true });
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.deepEqual([r.reinstalled, r.quarantined, r.failed], [1, 0, 0]);
    assert.equal(rs.readManifest(manifestFile, { log: quietLog }).skills.alpha.integrity, current);
  });

  it("entries recorded before integrity existed still reinstall, counted as unverified", async () => {
    fs.mkdirSync(path.dirname(manifestFile), { recursive: true });
    fs.writeFileSync(manifestFile, JSON.stringify({ version: 1, skills: { alpha: { version: "1.0.0", installedAt: "2026-01-01T00:00:00.000Z" }, beta: { version: null } } }));
    const { run, calls } = serving({ alpha: v1, beta: { "SKILL.md": "x" } });
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.equal(r.reinstalled, 2);
    assert.equal(r.unverified, 2);
    assert.equal(r.failed, 0);
    assert.deepEqual(calls.map((a) => a.slice(0, 4)), [["install", "alpha", "--version", "1.0.0"], ["install", "beta", "--no-input", "--workdir"]]);
  });

  it("stops starting installs once the total budget is used and defers the rest", async () => {
    for (const slug of ["alpha", "beta", "gamma"]) rs.recordInstall(manifestFile, slug, "1.0.0", { log: quietLog });
    let clock = 0;
    const calls = [];
    const run = async (args) => {
      calls.push(args[1]);
      clock += 100;
      const err = new Error("ETIMEDOUT");
      throw err;
    };
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog, totalBudgetMs: 150, now: () => clock });
    assert.deepEqual(calls, ["alpha", "beta"]);
    assert.equal(r.failed, 2);
    assert.equal(r.deferred, 1);
    assert.deepEqual(r.results.find((x) => x.slug === "gamma"), { slug: "gamma", version: "1.0.0", status: "deferred" });
  });
});

describe("runtime-skills.watchManifest", () => {
  let dir;
  let watcher;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  beforeEach(() => { dir = mkTmp("rs-watch-"); });
  afterEach(() => { if (watcher) watcher.close(); watcher = null; fs.rmSync(dir, { recursive: true, force: true }); });

  it("fires once (debounced) after recordInstall/recordUninstall rewrite the manifest, not for tmp files", (t) => {
    const file = path.join(dir, "runtime-skills.json");
    const calls = [];
    let changed;
    const handle = new (require("node:events").EventEmitter)();
    handle.close = () => {};
    // OS event delivery can follow a fixed sleep under load. Drive the
    // notification boundary directly while retaining real manifest writes.
    t.mock.method(fs, "watch", (directory, options, callback) => {
      assert.equal(directory, dir);
      assert.equal(options.persistent, false);
      changed = callback;
      return handle;
    });
    t.mock.timers.enable({ apis: ["setTimeout"] });
    watcher = rs.watchManifest(file, (f) => calls.push(f), { debounceMs: 150, log: quietLog });
    assert.ok(watcher, "fs.watch should be available on the test runtime");
    rs.recordInstall(file, "baidu-search", "1.2.3", { log: quietLog });
    changed("rename", "runtime-skills.json");
    t.mock.timers.tick(100);
    rs.recordInstall(file, "hackernews", "1.0.0", { log: quietLog }); // burst → one callback
    changed("rename", "runtime-skills.json");
    t.mock.timers.tick(149);
    assert.deepEqual(calls, [], "a second rewrite resets the debounce deadline");
    t.mock.timers.tick(1);
    assert.deepEqual(calls, [file]);
    const stagingName = `runtime-skills.json.tmp-${Date.now()}1`;
    fs.writeFileSync(path.join(dir, stagingName), "partial"); // our own staging file
    changed("rename", stagingName);
    fs.writeFileSync(path.join(dir, "other.json"), "{}");
    changed("rename", "other.json");
    t.mock.timers.tick(400);
    assert.equal(calls.length, 1, "unrelated / tmp files must not trigger a backup");
    rs.recordUninstall(file, "hackernews", { log: quietLog });
    changed("rename", "runtime-skills.json");
    t.mock.timers.tick(150);
    assert.deepEqual(calls, [file, file]);
  });

  it("a rejecting handler is logged, not thrown, and the watcher keeps working", async () => {
    const file = path.join(dir, "runtime-skills.json");
    const log = collectingLog();
    let n = 0;
    watcher = rs.watchManifest(file, async () => { n++; if (n === 1) throw new Error("s3 down"); }, { debounceMs: 100, log });
    rs.recordInstall(file, "a", "1.0.0", { log: quietLog });
    await sleep(400);
    assert.equal(n, 1);
    assert.ok(log.lines.some((l) => /warn:.*Manifest change handler failed: s3 down/.test(l)), log.lines.join("\n"));
    rs.recordInstall(file, "b", "1.0.0", { log: quietLog });
    await sleep(400);
    assert.equal(n, 2);
  });

  it("creates the state dir when missing and close() stops callbacks", async () => {
    const file = path.join(dir, "nested", "state", "runtime-skills.json");
    const calls = [];
    watcher = rs.watchManifest(file, () => calls.push(1), { debounceMs: 100, log: quietLog });
    assert.ok(fs.existsSync(path.dirname(file)));
    watcher.close();
    watcher = null;
    rs.recordInstall(file, "a", "1.0.0", { log: quietLog });
    await sleep(350);
    assert.equal(calls.length, 0);
  });
});

describe("agentcore-contract.js wiring", () => {
  const source = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf-8");

  it("awaits skill restoration before activating the prepared gateway without making it a readiness gate", () => {
    const reinstall = source.indexOf("await runtimeSkills.reinstallFromManifest(");
    const activate = source.indexOf("await preparedGateway.activate(");
    assert.ok(reinstall > 0 && activate > reinstall);
    assert.match(source, /runtimeSkills\.manifestPath\(OPENCLAW_DIR\)/);
    // The old fail-closed gate is gone: a failed/corrupt/invalid summary must
    // not block USER_READY (persistent-lockout fix).
    assert.equal(/if \(skills\.failed \|\| skills\.corrupt \|\| skills\.invalid\) throw new Error/.test(source), false);
    assert.doesNotMatch(source, /Required runtime skills could not be restored/, "restore outcome must not gate readiness");
    assert.match(source, /Runtime skills partly restored/);
  });

  it("backs the manifest up to S3 on change via watchManifest + workspaceSync.saveFile, started with the periodic save", () => {
    assert.match(source, /runtimeSkills\.watchManifest\(/);
    assert.match(source, /workspaceSync\.saveFile\(namespace, runtimeSkills\.MANIFEST_NAME\)/);
    const periodic = source.indexOf("workspaceSync.startPeriodicSave(namespace);");
    const backup = source.indexOf("startRuntimeSkillManifestBackup(namespace);");
    assert.ok(periodic > 0 && backup > periodic && backup - periodic < 300, "manifest backup must start after the periodic save and required change backup");
    const sync = fs.readFileSync(path.join(__dirname, "workspace-sync.js"), "utf-8");
    assert.match(sync, /async function saveFile\(namespace, relativePath\)/);
    assert.match(sync, /^\s*saveFile,$/m);
  });

  it("the Dockerfile ships runtime-skills.js to /app and next to the skill scripts", () => {
    const dockerfile = fs.readFileSync(path.join(__dirname, "Dockerfile"), "utf-8");
    assert.match(dockerfile, /^COPY runtime-skills\.js \/app\/runtime-skills\.js$/m);
    assert.match(dockerfile, /^COPY runtime-skills\.js \/skills\/clawhub-manage\/runtime-skills\.js$/m);
    assert.ok(dockerfile.indexOf("COPY skills/clawhub-manage /skills/clawhub-manage") < dockerfile.indexOf("COPY runtime-skills.js /skills/clawhub-manage/runtime-skills.js"), "real module must be copied after the skill dir so it replaces the shim");
  });

  it("the system prompt and SKILL.md no longer promise plain 'available on next session start'", () => {
    const skillMd = fs.readFileSync(path.join(__dirname, "skills", "clawhub-manage", "SKILL.md"), "utf-8");
    const agents = require("./gateway-config").renderAgents({ browserEnabled: true });
    assert.match(skillMd, /reinstall/i);
    assert.match(agents, /restored before requests are accepted in each new session/);
    assert.equal(/After install\/uninstall, the skill will be available on the next session start/.test(agents), false);
  });
});
