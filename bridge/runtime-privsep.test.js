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
