const { test } = require("node:test");
const assert = require("node:assert/strict");
const privsep = require("./runtime-privsep");

const rootProc = { getuid: () => 0 };
const nonRootProc = { getuid: () => 10001 };
const noUidProc = {}; // e.g. a platform without getuid

test("drops to the default uid/gid only when running as root", () => {
  assert.equal(privsep.canDropPrivileges(rootProc, {}), true);
  assert.equal(privsep.canDropPrivileges(nonRootProc, {}), false);
  assert.equal(privsep.canDropPrivileges(noUidProc, {}), false);
});

test("childPrivilegeOptions returns uid/gid as root and {} otherwise", () => {
  assert.deepEqual(privsep.childPrivilegeOptions(rootProc, {}),
    { uid: privsep.DEFAULT_UID, gid: privsep.DEFAULT_GID });
  assert.deepEqual(privsep.childPrivilegeOptions(nonRootProc, {}), {});
  assert.deepEqual(privsep.childPrivilegeOptions(noUidProc, {}), {});
});

test("uid/gid are overridable but never resolve to root", () => {
  assert.deepEqual(privsep.childPrivilegeOptions(rootProc,
    { OPENCLAW_RUNTIME_UID: "20002", OPENCLAW_RUNTIME_GID: "20002" }),
    { uid: 20002, gid: 20002 });
  // A 0/garbage override falls back to the safe default rather than root.
  assert.deepEqual(privsep.childPrivilegeOptions(rootProc, { OPENCLAW_RUNTIME_UID: "0" }),
    { uid: privsep.DEFAULT_UID, gid: privsep.DEFAULT_GID });
  assert.deepEqual(privsep.childPrivilegeOptions(rootProc, { OPENCLAW_RUNTIME_UID: "nonsense" }),
    { uid: privsep.DEFAULT_UID, gid: privsep.DEFAULT_GID });
});

test("chownToOpenClaw is a no-op (false) when not root and never throws", () => {
  let called = 0;
  const fs = { chownSync: () => { called += 1; } };
  assert.equal(privsep.chownToOpenClaw("/tmp/x", { proc: nonRootProc, env: {}, fs }), false);
  assert.equal(called, 0);
});

test("chownToOpenClaw chowns a single path as root", () => {
  const calls = [];
  const fs = { chownSync: (p, uid, gid) => calls.push([p, uid, gid]) };
  assert.equal(privsep.chownToOpenClaw("/tmp/scoped-creds/scoped-creds.json",
    { proc: rootProc, env: {}, fs }), true);
  assert.deepEqual(calls, [["/tmp/scoped-creds/scoped-creds.json", 10001, 10001]]);
});

test("chownToOpenClaw recurses into directories, following the layout but not symlinks", () => {
  const tree = {
    "/s": { dir: true },
    "/s/a.json": { dir: false },
    "/s/sub": { dir: true },
    "/s/sub/b.sqlite": { dir: false },
    "/s/link": { dir: true, symlink: true },
  };
  const chowned = [];
  const fs = {
    lstatSync: (p) => ({
      isDirectory: () => tree[p].dir,
      isSymbolicLink: () => !!tree[p].symlink,
    }),
    lchownSync: (p, uid, gid) => chowned.push([p, uid, gid]),
    readdirSync: (p) => Object.keys(tree)
      .filter((k) => k !== p && k.startsWith(p + "/") && !k.slice(p.length + 1).includes("/"))
      .map((k) => ({ name: k.slice(p.length + 1) })),
  };
  assert.equal(privsep.chownToOpenClaw("/s", { recursive: true, proc: rootProc, env: {}, fs }), true);
  const paths = chowned.map(([p]) => p);
  assert.deepEqual(paths.sort(), ["/s", "/s/a.json", "/s/link", "/s/sub", "/s/sub/b.sqlite"].sort());
  // The symlink itself is chowned (lchown) but not descended into.
  assert.ok(!paths.includes("/s/link/anything"));
});

test("chownToOpenClaw swallows a missing recursive root", () => {
  const fs = {
    lstatSync: () => { const e = new Error("nope"); e.code = "ENOENT"; throw e; },
  };
  assert.equal(privsep.chownToOpenClaw("/gone", { recursive: true, proc: rootProc, env: {}, fs }), false);
});

test("describeProtections and startup lines report the uid split as active under root", () => {
  const state = privsep.describeProtections({ proc: rootProc, env: {}, networkBlock: "active", procHidden: "active" });
  assert.equal(state.uidSplit, "active");
  assert.equal(state.uid, 10001);
  assert.equal(state.procEnvironIsolated, "active");
  const lines = privsep.formatStartupLines(state).join("\n");
  assert.match(lines, /run as uid=10001 gid=10001/);
  assert.match(lines, /environ of the supervisor and proxy is unreadable/);
  assert.match(lines, /MMDS \/ metadata network block for the agent uid: active/);
  assert.doesNotMatch(lines, /WARNING: privilege separation INACTIVE/);
});

test("startup lines warn loudly when privilege separation is inactive", () => {
  const state = privsep.describeProtections({ proc: nonRootProc, env: {}, networkBlock: "inactive", procHidden: "inactive" });
  assert.equal(state.uidSplit, "INACTIVE");
  const lines = privsep.formatStartupLines(state).join("\n");
  assert.match(lines, /WARNING: privilege separation INACTIVE/);
  // And when the network block is down it must say the execution role is still reachable.
  assert.match(lines, /can still reach the metadata endpoint/);
});

test("startup summary line names every layer and normalises inactive to INACTIVE", () => {
  const state = privsep.describeProtections({ proc: rootProc, env: {}, networkBlock: "inactive", procHidden: "inactive" });
  assert.equal(state.networkBlock, "INACTIVE");
  assert.equal(state.procHidden, "INACTIVE");
  const lines = privsep.formatStartupLines(state).join("\n");
  assert.match(lines, /\[privsep\] protections: uid_split=active proc_environ_isolation=active proc_hidepid=INACTIVE metadata_block=INACTIVE/);
  const unknown = privsep.describeProtections({ proc: rootProc, env: {}, networkBlock: "", procHidden: "weird" });
  assert.equal(unknown.networkBlock, "unknown");
  assert.equal(unknown.procHidden, "unknown");
});

test("credential endpoint shape: relative URI only is covered and logged without values", () => {
  const env = { AWS_CONTAINER_CREDENTIALS_RELATIVE_URI: "/v2/credentials/secret-path" };
  const shape = privsep.credentialEndpointShape(env);
  assert.deepEqual(shape, { relativeUri: true, fullUri: "none", tokenFile: "none" });
  const lines = privsep.formatStartupLines(privsep.describeProtections({ proc: rootProc, env, networkBlock: "active", procHidden: "active" })).join("\n");
  assert.match(lines, /container credential endpoint: relative_uri=set full_uri=none token_file=none/);
  assert.doesNotMatch(lines, /secret-path/);
  assert.doesNotMatch(lines, /may be able to reach the container credential endpoint/);
});

test("credential endpoint shape: a full URI on the firewalled credential address is covered", () => {
  const shape = privsep.credentialEndpointShape({ AWS_CONTAINER_CREDENTIALS_FULL_URI: "http://169.254.170.2/v1/creds" });
  assert.equal(shape.fullUri, "covered by the firewall");
  const v6 = privsep.credentialEndpointShape({ AWS_CONTAINER_CREDENTIALS_FULL_URI: "http://[fd00:ec2::254]/creds" });
  assert.equal(v6.fullUri, "covered by the firewall");
});

test("credential endpoint shape: a loopback full URI and a world-readable token file are flagged", () => {
  const fs = { statSync: () => ({ mode: 0o100644 }) };
  const env = {
    AWS_CONTAINER_CREDENTIALS_FULL_URI: "http://127.0.0.1:1338/creds",
    AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE: "/var/run/token",
  };
  const shape = privsep.credentialEndpointShape(env, fs);
  assert.equal(shape.fullUri, "NOT covered by the firewall (host 127.0.0.1)");
  assert.equal(shape.tokenFile, "readable by other uids");
  const lines = privsep.formatStartupLines(privsep.describeProtections({ proc: rootProc, env, networkBlock: "active", procHidden: "active", fs })).join("\n");
  assert.match(lines, /NOTE: the agent uid may be able to reach the container credential endpoint/);
});

test("credential endpoint shape: owner-only, missing and unparseable inputs never throw", () => {
  assert.equal(privsep.credentialEndpointShape({ AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE: "/t" }, { statSync: () => ({ mode: 0o100600 }) }).tokenFile, "owner-only");
  assert.equal(privsep.credentialEndpointShape({ AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE: "/t" }, { statSync: () => { throw new Error("ENOENT"); } }).tokenFile, "set but not readable");
  assert.equal(privsep.credentialEndpointShape({ AWS_CONTAINER_CREDENTIALS_FULL_URI: "not a url" }).fullUri, "unparseable (NOT covered by the firewall)");
});

// --- agent uid's own cache/config/temp directories ---------------------------

const realFs = require("node:fs");
const os = require("node:os");
const nodePath = require("node:path");

test("agentDirEnv points XDG dirs, TMPDIR and the npm cache at the agent dir as root, and is empty otherwise", () => {
  assert.deepEqual(privsep.agentDirEnv(rootProc, {}), {
    XDG_CACHE_HOME: "/home/openclaw/.cache",
    XDG_CONFIG_HOME: "/home/openclaw/.config",
    XDG_DATA_HOME: "/home/openclaw/.local/share",
    XDG_STATE_HOME: "/home/openclaw/.local/state",
    npm_config_cache: "/home/openclaw/.npm",
    TMPDIR: "/home/openclaw/tmp",
  });
  // Nothing under HOME (/root): the agent uid cannot create entries there.
  for (const value of Object.values(privsep.agentDirEnv(rootProc, { HOME: "/root" }))) {
    assert.equal(value.startsWith("/root"), false, value);
  }
  assert.deepEqual(privsep.agentDirEnv(nonRootProc, {}), {});
  assert.deepEqual(privsep.agentDirEnv(noUidProc, {}), {});
});

test("agentDir accepts only a clean absolute override", () => {
  assert.equal(privsep.agentDir({}), privsep.DEFAULT_AGENT_DIR);
  assert.equal(privsep.agentDir({ OPENCLAW_RUNTIME_AGENT_DIR: "/var/lib/agent/" }), "/var/lib/agent");
  for (const bad of ["relative/dir", "/", "/tmp/../root", ""]) {
    assert.equal(privsep.agentDir({ OPENCLAW_RUNTIME_AGENT_DIR: bad }), privsep.DEFAULT_AGENT_DIR, bad);
  }
});

test("ensureAgentDirs creates each directory parent-first, 0700, owned by the agent uid", () => {
  const ops = [];
  const fs = {
    mkdirSync: (p, o) => ops.push(["mkdir", p, o.mode]),
    lstatSync: () => ({ isDirectory: () => true, isSymbolicLink: () => false }),
    lchownSync: (p, u, g) => ops.push(["lchown", p, u, g]),
    chmodSync: (p, m) => ops.push(["chmod", p, m]),
  };
  assert.equal(privsep.ensureAgentDirs({ proc: rootProc, env: {}, fs }), true);
  const made = ops.filter((op) => op[0] === "mkdir").map((op) => op[1]);
  assert.deepEqual(made, privsep.agentDirLayout({}).map((entry) => entry.path));
  assert.equal(made[0], "/home/openclaw");
  assert.ok(ops.filter((op) => op[0] === "mkdir").every((op) => op[2] === 0o700));
  assert.ok(ops.filter((op) => op[0] === "lchown").every((op) => op[2] === 10001 && op[3] === 10001));
  assert.equal(ops.filter((op) => op[0] === "lchown").length, made.length);
});

test("ensureAgentDirs is a no-op when not root", () => {
  let touched = 0;
  const fs = { mkdirSync: () => { touched += 1; } };
  assert.equal(privsep.ensureAgentDirs({ proc: nonRootProc, env: {}, fs }), false);
  assert.equal(touched, 0);
});

test("ensureAgentDirs never chowns through a symlink and never throws", () => {
  const warnings = [];
  const ops = [];
  const fs = {
    mkdirSync: () => { throw Object.assign(new Error("exists"), { code: "EEXIST" }); },
    lstatSync: () => ({ isDirectory: () => false, isSymbolicLink: () => true }),
    lchownSync: () => ops.push("lchown"),
    chownSync: () => ops.push("chown"),
    chmodSync: () => ops.push("chmod"),
  };
  assert.equal(privsep.ensureAgentDirs({ proc: rootProc, env: {}, fs, log: { warn: (m) => warnings.push(m) } }), false);
  assert.deepEqual(ops, []);
  assert.match(warnings[0], /not a directory/);
  const failing = { mkdirSync: () => { throw Object.assign(new Error("read-only"), { code: "EROFS" }); } };
  assert.equal(privsep.ensureAgentDirs({ proc: rootProc, env: {}, fs: failing, log: { warn: (m) => warnings.push(m) } }), false);
  assert.match(warnings[1], /could not prepare \/home\/openclaw/);
});

test("ensureAgentDirs on a real filesystem: creates 0700 dirs and leaves a planted symlink alone", (t) => {
  if (typeof process.getuid !== "function" || process.getuid() === 0) return t.skip("needs a non-root uid");
  const base = realFs.mkdtempSync(nodePath.join(os.tmpdir(), "privsep-agentdir-"));
  t.after(() => realFs.rmSync(base, { recursive: true, force: true }));
  // Pretend to be root but "drop" to our own uid, so lchown succeeds without privileges.
  const env = {
    OPENCLAW_RUNTIME_UID: String(process.getuid()), OPENCLAW_RUNTIME_GID: String(process.getgid()),
    OPENCLAW_RUNTIME_AGENT_DIR: nodePath.join(base, "agent"),
  };
  assert.equal(privsep.ensureAgentDirs({ proc: rootProc, env }), true);
  for (const value of Object.values(privsep.agentDirEnv(rootProc, env))) {
    const stat = realFs.lstatSync(value);
    assert.ok(stat.isDirectory(), value);
    assert.equal(stat.mode & 0o777, 0o700, value);
  }
  // A symlink in place of a directory is reported, not followed.
  const outside = nodePath.join(base, "outside");
  realFs.mkdirSync(outside, { mode: 0o755 });
  const planted = { ...env, OPENCLAW_RUNTIME_AGENT_DIR: nodePath.join(base, "agent2") };
  realFs.mkdirSync(planted.OPENCLAW_RUNTIME_AGENT_DIR, { mode: 0o700 });
  realFs.symlinkSync(outside, nodePath.join(planted.OPENCLAW_RUNTIME_AGENT_DIR, ".cache"));
  assert.equal(privsep.ensureAgentDirs({ proc: rootProc, env: planted, log: { warn() {} } }), false);
  assert.equal(realFs.statSync(outside).mode & 0o777, 0o755);
});

test("Dockerfile creates every agent dir owned by the agent uid and keeps /root traverse-only", () => {
  const dockerfile = realFs.readFileSync(nodePath.join(__dirname, "Dockerfile"), "utf8").replace(/\\\n/g, " ");
  const runs = dockerfile.split("\n").filter((line) => line.startsWith("RUN "));
  const block = runs.find((line) => line.includes("mkdir") && line.includes("/home/openclaw"));
  assert.ok(block, "a RUN step prepares /home/openclaw");
  for (const { path: dir } of privsep.agentDirLayout({})) {
    assert.ok(block.split(/\s+/).includes(dir) || dir === "/home/openclaw" || dir === "/home/openclaw/.local",
      `${dir} is created`);
  }
  assert.match(block, /chown -R 10001:10001 \/home\/openclaw(\s|$)/);
  assert.match(block, /chmod -R 0700 \/home\/openclaw(\s|$)/);
  assert.match(block, /chmod 0711 \/root(\s|$)/);
  // /root is only ever made traverse-only, never writable or handed to the agent uid.
  const rootModes = [...dockerfile.matchAll(/chmod (?:-R )?(\S+) \/root(?=\s|$)/g)].map((m) => m[1]);
  assert.deepEqual(rootModes, ["0711"]);
  assert.equal(/chown (?:-R )?\S+ \/root(?=\s|$)/.test(dockerfile), false);
  assert.match(dockerfile, /useradd --uid 10001 --gid 10001 --home-dir \/home\/openclaw /);
});
