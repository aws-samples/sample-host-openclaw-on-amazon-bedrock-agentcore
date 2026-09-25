/**
 * Tests for runtime-skills.js — the per-user manifest of ClawHub skills a
 * user installs at runtime through the clawhub-manage skill, and the
 * background reinstall agentcore-contract.js runs from it after a cold start.
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
    const before = fs.readFileSync(manifestFile, "utf8");
    const { run, calls } = fakeRunner({ failing: ["flagged"] });
    const log = collectingLog();
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log });
    assert.equal(r.total, 3);
    assert.equal(r.reinstalled, 2);
    assert.equal(r.failed, 1);
    assert.deepEqual(calls.map((a) => a[1]), ["alpha", "flagged", "gamma"]);
    const failed = r.results.find((x) => x.status === "failed");
    assert.equal(failed.slug, "flagged");
    assert.match(failed.error, /security review|suspicious/);
    assert.equal(log.lines.filter((l) => /warn:.*Reinstall of flagged@1\.0\.0 failed/.test(l)).length, 1);
    assert.equal(rs.isSkillPresent("flagged", skillsDir), false);
    assert.equal(fs.readFileSync(manifestFile, "utf8"), before, "manifest untouched by reinstall");
  });

  it("counts a zero exit that left no SKILL.md behind as a failure", async () => {
    rs.recordInstall(manifestFile, "ghost", "1.0.0", { log: quietLog });
    const { run } = fakeRunner({ noFiles: ["ghost"] });
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.equal(r.failed, 1);
    assert.match(r.results[0].error, /SKILL\.md is missing/);
  });

  it("a corrupt manifest reinstalls nothing, calls nothing, reports corrupt and does not throw", async () => {
    fs.mkdirSync(path.dirname(manifestFile), { recursive: true });
    fs.writeFileSync(manifestFile, "{{{ definitely not json");
    const { run, calls } = fakeRunner();
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.deepEqual({ total: r.total, reinstalled: r.reinstalled, failed: r.failed, corrupt: r.corrupt }, { total: 0, reinstalled: 0, failed: 0, corrupt: true });
    assert.deepEqual(calls, []);
  });

  it("a missing manifest is simply nothing to do", async () => {
    const { run, calls } = fakeRunner();
    const r = await rs.reinstallFromManifest({ manifestFile, skillsDir, run, log: quietLog });
    assert.equal(r.total, 0);
    assert.equal(r.corrupt, false);
    assert.deepEqual(calls, []);
  });

  it("invalid manifest entries never reach the runner and are counted", async () => {
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
});

describe("runtime-skills.watchManifest", () => {
  let dir;
  let watcher;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  beforeEach(() => { dir = mkTmp("rs-watch-"); });
  afterEach(() => { if (watcher) watcher.close(); watcher = null; fs.rmSync(dir, { recursive: true, force: true }); });

  it("fires once (debounced) after recordInstall/recordUninstall rewrite the manifest, not for tmp files", async () => {
    const file = path.join(dir, "runtime-skills.json");
    const calls = [];
    watcher = rs.watchManifest(file, (f) => calls.push(f), { debounceMs: 150, log: quietLog });
    assert.ok(watcher, "fs.watch should be available on the test runtime");
    rs.recordInstall(file, "baidu-search", "1.2.3", { log: quietLog });
    rs.recordInstall(file, "hackernews", "1.0.0", { log: quietLog }); // burst → one callback
    await sleep(500);
    assert.deepEqual(calls, [file]);
    fs.writeFileSync(path.join(dir, `runtime-skills.json.tmp-${Date.now()}1`), "partial"); // our own staging file
    fs.writeFileSync(path.join(dir, "other.json"), "{}");
    await sleep(400);
    assert.equal(calls.length, 1, "unrelated / tmp files must not trigger a backup");
    rs.recordUninstall(file, "hackernews", { log: quietLog });
    await sleep(500);
    assert.equal(calls.length, 2);
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

  it("starts the reinstall only after the gateway is ready, guarded to run once", () => {
    const readyIdx = source.indexOf("openclawReady = true;");
    const startIdx = source.indexOf("startRuntimeSkillReinstall();");
    assert.ok(readyIdx > 0 && startIdx > readyIdx, "startRuntimeSkillReinstall() must follow openclawReady = true in pollOpenClawReadiness");
    assert.match(source, /if \(runtimeSkillReinstallStarted \|\| shuttingDown\) return;/);
    assert.match(source, /runtimeSkills\.manifestPath\(OPENCLAW_DIR\)/);
    assert.match(source, /reinstallFromManifest\(\{ manifestFile, log: console \}\)/);
  });

  it("backs the manifest up to S3 on change via watchManifest + workspaceSync.saveFile, started with the periodic save", () => {
    assert.match(source, /runtimeSkills\.watchManifest\(/);
    assert.match(source, /workspaceSync\.saveFile\(namespace, runtimeSkills\.MANIFEST_NAME\)/);
    const periodic = source.indexOf("workspaceSync.startPeriodicSave(namespace);");
    const backup = source.indexOf("startRuntimeSkillManifestBackup(namespace);");
    assert.ok(periodic > 0 && backup > periodic && backup - periodic < 200, "manifest backup must start right after the periodic save (same namespace, only once OpenClaw is ready)");
    const sync = fs.readFileSync(path.join(__dirname, "workspace-sync.js"), "utf-8");
    assert.match(sync, /async function saveFile\(namespace, relativePath\)/);
    assert.match(sync, /^\s*saveFile,$/m);
  });

  it("does not await the reinstall on the boot path (init/ping stay unblocked)", () => {
    assert.equal(/await\s+(runtimeSkills\.)?reinstallFromManifest|await\s+startRuntimeSkillReinstall/.test(source), false);
  });

  it("the Dockerfile ships runtime-skills.js to /app and next to the skill scripts", () => {
    const dockerfile = fs.readFileSync(path.join(__dirname, "Dockerfile"), "utf-8");
    assert.match(dockerfile, /^COPY runtime-skills\.js \/app\/runtime-skills\.js$/m);
    assert.match(dockerfile, /^COPY runtime-skills\.js \/skills\/clawhub-manage\/runtime-skills\.js$/m);
    assert.ok(dockerfile.indexOf("COPY skills/clawhub-manage /skills/clawhub-manage") < dockerfile.indexOf("COPY runtime-skills.js /skills/clawhub-manage/runtime-skills.js"), "real module must be copied after the skill dir so it replaces the shim");
  });

  it("the system prompt and SKILL.md no longer promise plain 'available on next session start'", () => {
    const skillMd = fs.readFileSync(path.join(__dirname, "skills", "clawhub-manage", "SKILL.md"), "utf-8");
    assert.match(skillMd, /reinstall/i);
    assert.match(source, /reinstalled automatically a few seconds after each new session starts/);
    assert.equal(/After install\/uninstall, the skill will be available on the next session start/.test(source), false);
  });
});
