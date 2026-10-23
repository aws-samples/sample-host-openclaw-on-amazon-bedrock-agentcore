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
