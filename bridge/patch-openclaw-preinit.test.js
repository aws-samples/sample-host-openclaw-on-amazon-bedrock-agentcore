// OPENCLAW_PACKAGE_DIR is read-only; patch tests use temporary copies.
const { before, after, describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const {
  OPENCLAW_VERSION, LAUNCHER_FILE, PRESENCE_FILE,
  PRESENCE_EXPORT, PRESENCE_RESET, PATCHES, PINNED_RUNTIME_FILES,
  sha256, replaceExactlyOnce, transformSource, patchSource, patchPackage,
} = require("./patch-openclaw-preinit");

function packageCopy(t, files, manifest = { name: "openclaw", version: OPENCLAW_VERSION }) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "openclaw-preinit-patch-unit-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, "dist"));
  fs.writeFileSync(path.join(root, "package.json"), JSON.stringify(manifest));
  for (const [file, contents] of Object.entries(files)) fs.writeFileSync(path.join(root, file), contents);
  return root;
}

function refusesWithoutWriting(t, root, pattern) {
  const write = t.mock.method(fs, "writeFileSync", () => assert.fail("Rejected package must not be written."));
  try {
    assert.throws(() => patchPackage(root), pattern);
    assert.equal(write.mock.callCount(), 0);
  } finally {
    write.mock.restore();
  }
}

describe("pinned build patch guards", () => {
  it("requires exact, unique anchors and preserves every surrounding byte", () => {
    const variables = "const SELF_INSTANCE_ID = randomUUID();\n" +
      "const uptimeOrigin = os.uptime() * 1e3 - performance.now();\n";
    const source = `before\n${variables}${PRESENCE_EXPORT}\nafter\n`;
    const expected = `before\n${variables.replaceAll("const ", "let ")}${PRESENCE_RESET}\nafter\n`;
    assert.equal(transformSource(PRESENCE_FILE, source), expected);
    for (const text of ["", PRESENCE_RESET, `${source}\n${source}`]) {
      assert.throws(() => transformSource(PRESENCE_FILE, text), /exactly one/);
    }
    assert.throws(() => replaceExactlyOnce("x x", "x", "y"), /exactly one/);
    assert.throws(() => transformSource("other.mjs", source), /Unsupported/);
  });

  it("rejects anchor-only source and unknown hashes", () => {
    assert.throws(() => patchSource(LAUNCHER_FILE, Buffer.from("launcher")), /Unsupported/);
    assert.throws(() => patchSource(PRESENCE_FILE, Buffer.from(PRESENCE_EXPORT)), /SHA-256/);
    assert.throws(() => patchSource("other.mjs", Buffer.from("other")), /Unsupported/);
  });

  it("rejects unknown versions and malformed metadata before touching source", (t) => {
    for (const manifest of [
      { name: "openclaw", version: "2026.9.6" }, { name: "other", version: OPENCLAW_VERSION },
      { name: "openclaw", version: 2026.95 }, {}, null,
    ]) {
      refusesWithoutWriting(t, packageCopy(t, {}, manifest), /Expected OpenClaw package version/);
    }
    const root = packageCopy(t, {});
    fs.writeFileSync(path.join(root, "package.json"), "DO_NOT_ECHO_PRIVATE_CONTENT");
    refusesWithoutWriting(t, root, { message: "Cannot read a valid OpenClaw package.json." });
  });
});

const packageDir = process.env.OPENCLAW_PACKAGE_DIR;
describe("actual pinned OpenClaw source (offline)", {
  skip: packageDir ? false : "Set OPENCLAW_PACKAGE_DIR to the unmodified OpenClaw 2026.9.5 package.",
}, () => {
  let originals, manifest;
  before(() => {
    manifest = fs.readFileSync(path.join(packageDir, "package.json"));
    assert.equal(JSON.parse(manifest).version, OPENCLAW_VERSION);
    originals = Object.fromEntries([...PATCHES.map((pin) => pin.file), ...Object.keys(PINNED_RUNTIME_FILES)]
      .map((file) => [file, fs.readFileSync(path.join(packageDir, file))]));
    for (const pin of PATCHES) assert.equal(sha256(originals[pin.file]), pin.originalSha256);
    for (const [file, hash] of Object.entries(PINNED_RUNTIME_FILES)) assert.equal(sha256(originals[file]), hash);
  });

  after(() => {
    if (manifest) assert.deepEqual(fs.readFileSync(path.join(packageDir, "package.json")), manifest);
    for (const [file, contents] of Object.entries(originals || {})) {
      assert.deepEqual(fs.readFileSync(path.join(packageDir, file)), contents, "Evidence is read-only.");
    }
  });

  it("patches only presence and preserves the launcher and runtime bytes", (t) => {
    const root = packageCopy(t, originals, JSON.parse(manifest));
    const untouched = path.join(root, "dist/unrelated.mjs");
    fs.writeFileSync(untouched, "// untouched\n");
    const beforeManifest = fs.readFileSync(path.join(root, "package.json"));
    const result = patchPackage(root);
    assert.equal(result.changed, true);
    assert.equal(result.version, OPENCLAW_VERSION);
    for (const pin of PATCHES) {
      const actual = fs.readFileSync(path.join(root, pin.file));
      assert.equal(sha256(actual), pin.patchedSha256);
      assert.deepEqual(actual, patchSource(pin.file, originals[pin.file]).source);
      assert.equal(actual.toString("utf8"), transformSource(pin.file, originals[pin.file].toString("utf8")));
    }
    for (const file of Object.keys(PINNED_RUNTIME_FILES)) {
      assert.deepEqual(fs.readFileSync(path.join(root, file)), originals[file]);
    }
    assert.deepEqual(fs.readFileSync(path.join(root, "package.json")), beforeManifest);
    assert.equal(fs.readFileSync(untouched, "utf8"), "// untouched\n");
  });

  it("is idempotent without rewriting files or changing inode/mtime", (t) => {
    const root = packageCopy(t, originals);
    patchPackage(root);
    const stats = PATCHES.map((pin) => fs.statSync(path.join(root, pin.file), { bigint: true }));
    const write = t.mock.method(fs, "writeFileSync", () => assert.fail("Idempotent patch must not write."));
    try {
      const result = patchPackage(root);
      assert.equal(result.changed, false);
      assert.ok(result.files.every((file) => file.changed === false));
    } finally {
      write.mock.restore();
    }
    PATCHES.forEach((pin, index) => {
      const current = fs.statSync(path.join(root, pin.file), { bigint: true });
      for (const key of ["ino", "mtimeNs", "ctimeNs", "size", "mode"]) assert.equal(current[key], stats[index][key]);
    });
  });

  it("validates the launcher even when presence is already patched", (t) => {
    const files = { ...originals, [PRESENCE_FILE]: patchSource(PRESENCE_FILE, originals[PRESENCE_FILE]).source };
    const root = packageCopy(t, files);
    const result = patchPackage(root);
    assert.deepEqual(result.files.map((file) => file.changed), [false]);
    fs.appendFileSync(path.join(root, LAUNCHER_FILE), "\n");
    refusesWithoutWriting(t, root, /SHA-256/);
  });

  it("rejects modified, missing and misnamed targets and runtimes before any writes", (t) => {
    for (const file of Object.keys(originals)) {
      const root = packageCopy(t, { ...originals, [file]: Buffer.concat([originals[file], Buffer.from("\n")]) });
      refusesWithoutWriting(t, root, /SHA-256/);
      const missing = packageCopy(t, originals);
      fs.renameSync(path.join(missing, file), path.join(missing, `${file}.other`));
      refusesWithoutWriting(t, missing, { code: "ENOENT" });
    }
    const root = packageCopy(t, originals);
    patchPackage(root);
    fs.appendFileSync(path.join(root, PRESENCE_FILE), "\n");
    refusesWithoutWriting(t, root, /SHA-256/);
  });

  it("rejects changed versions even for fully patched source", (t) => {
    const root = packageCopy(t, originals);
    patchPackage(root);
    fs.writeFileSync(path.join(root, "package.json"), JSON.stringify({ name: "openclaw", version: "2026.10.0" }));
    refusesWithoutWriting(t, root, /Expected OpenClaw package version/);
  });

  function launcherHarness({ optIn = "1", env, prepare = async () => {}, entry = async () => true } = {}) {
    const calls = [];
    const source = originals[LAUNCHER_FILE].toString("utf8");
    const start = source.lastIndexOf("if (!waitingForCompileCacheRespawn) {");
    assert.ok(start > 0);
    // Evaluate the preload entry and original launcher tail with imports stubbed.
    const preloader = fs.readFileSync(path.join(__dirname, "openclaw-preinit.mjs"), "utf8");
    const preloadStart = preloader.lastIndexOf('\nif (process.env.OPENCLAW_AGENTCORE_PREINIT === "1") {');
    assert.ok(preloadStart > 0);
    const body = preloader.slice(preloadStart) + source.slice(start);
    const dependencies = {
      waitingForCompileCacheRespawn: false,
      isHelpFastPathDisabled: () => true,
      installProcessWarningFilter: async () => { calls.push("warning-filter"); },
      process: { env: env ?? { OPENCLAW_AGENTCORE_PREINIT: optIn } },
      LAUNCHER_URL: "file:///usr/local/lib/node_modules/openclaw/openclaw.mjs",
      prepareGateway: async (url) => { calls.push(["prepare", url]); await prepare(); calls.push("activated"); },
      tryImport: async (specifier) => { calls.push(specifier); return entry(specifier); },
      buildMissingEntryErrorMessage: async () => "missing entry",
    };
    return {
      calls,
      run: () => vm.compileFunction(`return (async () => { ${body} })();`, Object.keys(dependencies))(
        ...Object.values(dependencies)),
    };
  }

  it("awaits activation before the unmodified launcher starts", async () => {
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    const h = launcherHarness({ prepare: () => gate });
    const done = h.run();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(h.calls, [
      ["prepare", "file:///usr/local/lib/node_modules/openclaw/openclaw.mjs"],
    ]);
    release();
    await done;
    assert.deepEqual(h.calls.slice(-3), ["activated", "warning-filter", "./dist/entry.js"]);
  });

  it("preserves normal CLI behavior without exact opt-in and preserves the entry fallback", async () => {
    for (const optIn of [undefined, "", "0", "true"]) {
      const h = launcherHarness({ optIn: optIn ?? "" });
      await h.run();
      assert.deepEqual(h.calls, ["warning-filter", "./dist/entry.js"]);
    }
    const h = launcherHarness({ entry: (specifier) => specifier.endsWith("entry.mjs") });
    await h.run();
    assert.deepEqual(h.calls.slice(-4), ["activated", "warning-filter", "./dist/entry.js", "./dist/entry.mjs"]);
  });

  it("cannot continue the original CLI when preparation or activation rejects", async () => {
    const h = launcherHarness({ prepare: async () => { throw new Error("barrier failed"); } });
    await assert.rejects(h.run(), /barrier failed/);
    assert.equal(h.calls.some((call) => typeof call === "string" && call.includes("entry.")), false);
  });

  it("a normal descendant CLI inheriting the activated environment does not reenter preinit", async (t) => {
    const { EventEmitter } = require("node:events");
    const { createGatewayPreparer, LAUNCHER_URL, PACKAGE_ROOT, RUNTIME_FILES } =
      await import("./openclaw-preinit.mjs");
    const child = new EventEmitter();
    child.connected = true;
    child.argv = ["node", `${PACKAGE_ROOT}/openclaw.mjs`, "gateway", "run"];
    child.env = {
      HOME: "/root", PATH: "/usr/bin:/bin", OPENCLAW_STATE_DIR: "/root/.openclaw",
      OPENCLAW_CONFIG_PATH: "/root/.openclaw/openclaw.json",
      OPENCLAW_WORKSPACE_DIR: "/root/.openclaw/workspace",
      AWS_EC2_METADATA_DISABLED: "true", OPENCLAW_AGENTCORE_PREINIT: "1",
      OPENCLAW_NO_RESPAWN: "1", NODE_DISABLE_COMPILE_CACHE: "1",
    };
    const activationEnv = { ...child.env, USER_ID: "actor-one", INTERNAL_USER_ID: "user-one" };
    child.send = (message, callback) => {
      queueMicrotask(() => {
        if (message.type === "openclaw:prepared") child.emit("message", {
          type: "openclaw:activate", generation: "generation-one",
          binding: { userId: "user-one", actorId: "actor-one", routingGeneration: 0, runtimeSessionId: null },
          env: activationEnv,
        });
        callback(null);
      });
      return true;
    };
    t.after(() => child.emit("exit", 0));
    const prepare = createGatewayPreparer({
      child,
      readFile(file) {
        assert.equal(file, `${PACKAGE_ROOT}/package.json`);
        return manifest.toString("utf8");
      },
      realpath: (file) => file,
      importModule: async (url) => {
        if (url.endsWith(RUNTIME_FILES.server)) return {};
        if (url.endsWith(RUNTIME_FILES.reply)) return { prewarmConfigDrivenReplyRuntime() {} };
        assert.ok(url.endsWith(RUNTIME_FILES.presence));
        return { resetAgentCoreSnapshotPresence() {
          assert.equal(child.env.OPENCLAW_AGENTCORE_PREINIT, "1");
        } };
      },
    });
    await prepare(LAUNCHER_URL);
    const descendantEnv = { ...child.env };
    assert.equal(Object.hasOwn(descendantEnv, "OPENCLAW_AGENTCORE_PREINIT"), false);
    assert.equal(descendantEnv.OPENCLAW_NO_RESPAWN, "1");
    assert.equal(descendantEnv.NODE_DISABLE_COMPILE_CACHE, "1");
    // A descendant may inherit Node's --import option, but no activation channel.
    const descendant = launcherHarness({
      env: descendantEnv,
      prepare: () => assert.fail("A normal descendant must not require activation IPC."),
    });
    await descendant.run();
    assert.deepEqual(descendant.calls, ["warning-filter", "./dist/entry.js"]);
  });

  function pinnedFunction(file, name) {
    const source = fs.readFileSync(path.join(packageDir, file), "utf8");
    const match = source.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^\\}`, "m"));
    assert.ok(match, `Pinned source must define ${name}.`);
    return match[0];
  }

  it("preserves scoped selectors and identity during the original entry environment normalization", () => {
    const { buildOpenClawEnv } = require("./scoped-credentials");
    const env = {
      ...buildOpenClawEnv({
        credDir: "/tmp/scoped-creds",
        baseEnv: { HOME: "/root", PATH: "/usr/bin:/bin", Z_AI_API_KEY: "not-forwarded" },
      }),
      OPENCLAW_STATE_DIR: "/root/.openclaw", OPENCLAW_CONFIG_PATH: "/root/.openclaw/openclaw.json",
      OPENCLAW_WORKSPACE_DIR: "/root/.openclaw/workspace",
      NODE_DISABLE_COMPILE_CACHE: "1", OPENCLAW_NO_RESPAWN: "1",
      USER_ID: "actor-one", INTERNAL_USER_ID: "user-one",
    };
    const before = structuredClone(env);
    const functions = [
      pinnedFunction("dist/openclaw-exec-env-Cc7ECPuD.mjs", "ensureOpenClawExecMarkerOnProcess"),
      pinnedFunction("dist/env-CZ7wtIBj.mjs", "normalizeZaiEnv"),
      pinnedFunction("dist/env-CZ7wtIBj.mjs", "normalizeEnv"),
    ].join("\n");
    vm.compileFunction(`${functions}
      ensureOpenClawExecMarkerOnProcess();
      normalizeEnv();`, ["process", "OPENCLAW_CLI_ENV_VAR", "CLI_ENV_VALUE"])(
      { env }, "OPENCLAW_CLI", "1");
    // Normal CLI startup intentionally adds a marker after the preinit barrier.
    assert.deepEqual(env, { ...before, OPENCLAW_CLI: "1" });
    assert.equal(Object.hasOwn(env, "ZAI_API_KEY"), false);
  });

  it("the pinned entry cannot plan an environment or compile-cache respawn for the prepared package", () => {
    const packageRoot = "/usr/local/lib/node_modules/openclaw";
    const child = {
      argv: ["node", `${packageRoot}/openclaw.mjs`, "gateway", "run", "--port", "18789", "--verbose"],
      env: { NODE_DISABLE_COMPILE_CACHE: "1", OPENCLAW_NO_RESPAWN: "1" },
      execArgv: [], execPath: "/usr/local/bin/node", platform: "linux",
    };
    const original = structuredClone(child);
    const general = vm.compileFunction(
      `return (${pinnedFunction("dist/entry.respawn-czfKIVQC.mjs", "buildCliRespawnPlan")});`,
      ["process", "shouldSkipStartupEnvironmentRespawnForArgv", "isTruthyEnvValue", "resolveNodeStartupTlsEnvironment"],
    )(child, () => false, (value) => value === "1",
      () => assert.fail("NO_RESPAWN must return before TLS/Node environment mutation."));
    assert.equal(general({ env: child.env }), null);

    const entryFunctions = [
      "isSourceCheckoutInstallRoot", "isNodeCompileCacheDisabled", "isNodeCompileCacheRequested",
      "buildOpenClawCompileCacheRespawnPlan",
    ].map((name) => pinnedFunction("dist/entry.js", name)).join("\n");
    const compileCache = vm.compileFunction(
      `${entryFunctions}\nreturn buildOpenClawCompileCacheRespawnPlan;`,
      ["process", "path", "existsSync", "isForegroundGmailRunArgv", "shouldKeepNativeHookRelayInProcess"],
    )(child, path, (file) => {
      assert.ok(file.startsWith(packageRoot + "/"));
      // Inspect only immutable package layout; never run the entry or gateway.
      return fs.existsSync(path.join(packageDir, path.relative(packageRoot, file)));
    }, () => false, () => false);
    assert.equal(compileCache({
      installRoot: packageRoot, currentFile: `${packageRoot}/dist/entry.js`,
      env: child.env, compileCacheDir: undefined,
    }), undefined);
    assert.deepEqual(child, original);
  });

  function presenceHarness(patched, optIn = "1") {
    const clock = { now: 5000, monotonic: 1000, uptime: 10, host: "snapshot-host", ip: "192.0.2.1" };
    let uuidCount = 0;
    const env = { OPENCLAW_AGENTCORE_PREINIT: optIn };
    const original = originals[PRESENCE_FILE];
    const source = patched ? patchSource(PRESENCE_FILE, original).source.toString("utf8") : original.toString("utf8");
    const body = source.replace(/^import .*;\n/gmu, "").replace(/^export \{.*\};$/mu, `
      return {
        listSystemPresence, upsertPresence,
        reset: typeof resetAgentCoreSnapshotPresence === "function" ? resetAgentCoreSnapshotPresence : null,
        state: () => ({ instanceId: SELF_INSTANCE_ID, uptimeOrigin, freshnessTime, freshnessSample, count: entries.size })
      };`);
    const dependencies = {
      normalizeOptionalLowercaseString: (value) => typeof value === "string" ? value.trim().toLowerCase() : undefined,
      normalizeOptionalString: (value) => typeof value === "string" ? value.trim() : undefined,
      normalizeLowercaseStringOrEmpty: (value) => value?.toLowerCase() || "",
      truncateUtf16Safe: (value, length) => value.slice(0, length),
      resolveRuntimeServiceVersion: () => OPENCLAW_VERSION,
      pickBestEffortPrimaryLanIPv4: () => clock.ip,
      resolveDarwinProductVersion: () => "test",
      resolveMachineModelIdentifier: () => `model-${clock.host}`,
      os: { hostname: () => clock.host, uptime: () => clock.uptime, platform: () => "linux", release: () => "test" },
      performance: { now: () => clock.monotonic },
      Date: { now: () => clock.now },
      randomUUID: () => `uuid-${++uuidCount}`,
      process: { env },
    };
    const runtime = vm.compileFunction(body, Object.keys(dependencies))(...Object.values(dependencies));
    return { runtime, clock, env, uuidCount: () => uuidCount };
  }

  it("keeps upstream self-presence behavior identical for normal CLI startup", () => {
    const original = presenceHarness(false, "0"), patched = presenceHarness(true, "0");
    assert.deepEqual(patched.runtime.listSystemPresence(), original.runtime.listSystemPresence());
    assert.deepEqual(patched.runtime.state(), original.runtime.state());
    assert.equal(patched.uuidCount(), 1);
    assert.throws(() => patched.runtime.reset(), /requires a single bound preinit activation/);
  });

  it("refreshes cloned self UUID, host/IP and clock baselines exactly once after binding", () => {
    const h = presenceHarness(true);
    const initial = h.runtime.listSystemPresence()[0];
    assert.equal(initial.instanceId, "uuid-1");
    assert.throws(() => h.runtime.reset(), /requires a single bound/);
    h.env.USER_ID = "actor-one";
    h.env.INTERNAL_USER_ID = "user-one";
    // Restore may have a different wall clock and OS uptime; discard old baselines.
    Object.assign(h.clock, { now: 250, monotonic: 20, uptime: 1, host: "restored-host", ip: "192.0.2.2" });
    h.runtime.reset();
    const restored = h.runtime.listSystemPresence()[0];
    assert.equal(restored.instanceId, "uuid-2");
    assert.equal(restored.host, "restored-host");
    assert.equal(restored.ip, "192.0.2.2");
    assert.equal(restored.ts, 250);
    assert.equal(restored.modelIdentifier, "model-restored-host");
    assert.deepEqual(h.runtime.state(), {
      instanceId: "uuid-2", uptimeOrigin: 980, freshnessTime: 250, freshnessSample: 20, count: 1,
    });
    assert.throws(() => h.runtime.reset(), /requires a single bound/);
    assert.equal(h.uuidCount(), 2);
  });

  it("refuses to reset a stateful presence registry or remove other connection entries", () => {
    const h = presenceHarness(true);
    h.env.USER_ID = "actor-one";
    h.env.INTERNAL_USER_ID = "user-one";
    h.runtime.upsertPresence("connected-client", { host: "client", mode: "client" });
    const before = h.runtime.listSystemPresence();
    assert.throws(() => h.runtime.reset(), /requires a single bound/);
    assert.deepEqual(h.runtime.listSystemPresence(), before);
    assert.equal(h.uuidCount(), 1);
  });
});
