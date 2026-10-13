/**
 * Unit tests for the pinned OpenClaw runtime facts patch.
 * OPENCLAW_PACKAGE_DIR enables source-backed tests; the package is read-only.
 * Run: node --test patch-openclaw.test.js
 */
const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const {
  OPENCLAW_VERSION, TARGET_FILE, ORIGINAL_SHA256, PATCHED_SHA256,
  OLD_ANCHOR, NEW_ANCHOR, sha256, replaceEmptySubagentFact, patchSource, patchPackage,
} = require("./patch-openclaw");

function makePackage(t, source, manifest = { version: OPENCLAW_VERSION }) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "openclaw-empty-subagent-unit-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, "dist"));
  fs.writeFileSync(path.join(root, "package.json"), JSON.stringify(manifest));
  fs.writeFileSync(path.join(root, TARGET_FILE), source);
  return root;
}

function assertRefusedWithoutWrites(t, root, expected) {
  const target = path.join(root, TARGET_FILE);
  const before = fs.readFileSync(target);
  const manifest = fs.readFileSync(path.join(root, "package.json"));
  const write = t.mock.method(fs, "writeFileSync", () => assert.fail("Rejected packages must not be written."));
  assert.throws(() => patchPackage(root), expected);
  assert.equal(write.mock.callCount(), 0);
  assert.deepEqual(fs.readFileSync(target), before);
  assert.deepEqual(fs.readFileSync(path.join(root, "package.json")), manifest);
  write.mock.restore();
}

describe("pinned patch validation", () => {
  it("replaces exactly one literal anchor without changing surrounding source", () => {
    const source = `before\n\t${OLD_ANCHOR}\nafter\n`;
    assert.equal(replaceEmptySubagentFact(source), `before\n\t${NEW_ANCHOR}\nafter\n`);
    assert.equal(source, `before\n\t${OLD_ANCHOR}\nafter\n`);
  });

  it("rejects absent or repeated anchors", () => {
    for (const source of ["", NEW_ANCHOR, `${OLD_ANCHOR}\n${OLD_ANCHOR}`]) {
      assert.throws(() => replaceEmptySubagentFact(source), /Expected exactly one/);
    }
  });

  it("rejects an unknown hash even when the replacement anchor matches", (t) => {
    const source = Buffer.from(`${OLD_ANCHOR}\n`);
    const original = Buffer.from(source);
    const root = makePackage(t, source);
    assertRefusedWithoutWrites(t, root, /Unsupported OpenClaw runtime facts source SHA-256/);
    assert.throws(() => patchSource(source), /Unsupported OpenClaw/);
    assert.deepEqual(source, original);
  });

  it("rejects unknown, missing and non-string package versions before writing", (t) => {
    for (const manifest of [{ version: "2026.9.6" }, {}, { version: 2026.95 }, null]) {
      assertRefusedWithoutWrites(
        t, makePackage(t, OLD_ANCHOR, manifest), /Expected OpenClaw package version 2026\.9\.5/,
      );
    }
  });

  it("rejects malformed package metadata without echoing its contents", (t) => {
    const root = makePackage(t, OLD_ANCHOR);
    fs.writeFileSync(path.join(root, "package.json"), "private-invalid-manifest");
    assertRefusedWithoutWrites(t, root, {
      message: "Cannot read a valid OpenClaw package.json.",
    });
  });

  it("requires the exact pinned file instead of searching for similar names", (t) => {
    const root = makePackage(t, OLD_ANCHOR);
    const alternate = path.join(root, "dist/system-prompt-report-other.mjs");
    fs.renameSync(path.join(root, TARGET_FILE), alternate);
    assert.throws(() => patchPackage(root), { code: "ENOENT" });
    assert.equal(fs.existsSync(path.join(root, TARGET_FILE)), false);
    assert.equal(fs.readFileSync(alternate, "utf8"), OLD_ANCHOR);
  });
});

function deepFreeze(value) {
  if (value && typeof value === "object") {
    Object.values(value).forEach(deepFreeze);
    Object.freeze(value);
  }
  return value;
}

function compileRuntimeFacts(source, overrides = {}) {
  const match = source.match(/async function buildRuntimeFactsContext\(params\) \{[\s\S]*?\n\}/);
  assert.ok(match, "pinned source must define buildRuntimeFactsContext");
  const calls = { subagent: [], media: [], scope: [], sessions: [], approved: [] };
  const dependencies = {
    process: { platform: "linux" },
    buildApprovedExecutablesRuntimeContext: (agentId) => {
      calls.approved.push(agentId);
      return "## Approved executables\nallowed";
    },
    resolveProcessToolScopeKey: (params) => {
      calls.scope.push(params);
      return "process-scope";
    },
    listActiveProcessSessionReferences: (params) => {
      calls.sessions.push(params);
      return [];
    },
    sanitizeForPromptLiteral: (text) => text,
    truncateUtf16Safe: (text, limit) => text.slice(0, limit),
    buildActiveSubagentRuntimeContext: (params) => {
      calls.subagent.push(params);
      return undefined;
    },
    buildMediaTaskRuntimeContext: async (params) => {
      calls.media.push(params);
      return undefined;
    },
    ...overrides,
  };
  const build = vm.compileFunction(
    `return (${match[0]});`, Object.keys(dependencies),
  )(...Object.values(dependencies));
  return { build, calls };
}

// Compile the real subagent formatter; registry and other external helpers are stubs.
function compileSubagentContext(source, entries, list) {
  const start = source.indexOf("function quotePromptData(value) {");
  assert.notEqual(start, -1);
  const end = source.indexOf("//#endregion", start);
  assert.notEqual(end, -1);
  const snapshot = new Map(entries.map((entry) => [entry.runId, entry]));
  const dependencies = {
    sanitizeForPromptLiteral: (text) => text,
    truncateUtf16Safe: (text, limit) => text.slice(0, limit),
    resolveSubagentCompletionResultText: (entry) => entry.result,
    resolveMainSessionAlias: () => ({ mainKey: "agent:main:main", alias: "main" }),
    resolveInternalSessionKey: ({ key }) => key,
    parseAgentSessionKey: () => ({ agentId: "main" }),
    subagentRuns: snapshot,
    getSubagentRunsSnapshotForSession: () => new Map(snapshot),
    getSubagentSessionListRunsSnapshotForRead: () => new Map(snapshot),
    buildSubagentRunReadIndexFromRuns: () => ({
      latestRunsByChildSessionKey: new Map(entries.map((entry) => [entry.childSessionKey, entry])),
    }),
    isSubagentRunVisibleToSession: () => true,
    sortSubagentRuns: (runs) => runs,
    buildSubagentList: () => list,
  };
  return vm.compileFunction(
    `${source.slice(start, end)}\nreturn buildActiveSubagentRuntimeContext;`,
    Object.keys(dependencies),
  )(...Object.values(dependencies));
}

function paramsWithTools(...tools) {
  return deepFreeze({
    cfg: { test: true },
    sessionKey: "agent:main:main",
    agentId: "main",
    capabilityToolNames: new Set(tools),
  });
}

const packageDir = process.env.OPENCLAW_PACKAGE_DIR;
describe("pinned OpenClaw source", {
  skip: packageDir ? false : "Set OPENCLAW_PACKAGE_DIR to the unmodified pinned OpenClaw package.",
}, () => {
  let original;
  let originalManifest;
  let patched;

  before(() => {
    originalManifest = fs.readFileSync(path.join(packageDir, "package.json"));
    assert.equal(JSON.parse(originalManifest).version, OPENCLAW_VERSION);
    original = fs.readFileSync(path.join(packageDir, TARGET_FILE));
    assert.equal(sha256(original), ORIGINAL_SHA256);
    patched = patchSource(original).source;
    assert.equal(sha256(patched), PATCHED_SHA256);
  });

  after(() => {
    if (original) assert.deepEqual(fs.readFileSync(path.join(packageDir, TARGET_FILE)), original);
    if (originalManifest) assert.deepEqual(fs.readFileSync(path.join(packageDir, "package.json")), originalManifest);
  });

  it("patches a package copy to the exact expected hash with no other changes", (t) => {
    const root = makePackage(t, original, JSON.parse(originalManifest));
    const manifest = fs.readFileSync(path.join(root, "package.json"));
    const untouched = path.join(root, "dist/unrelated.mjs");
    fs.writeFileSync(untouched, `// ${OLD_ANCHOR}\n`);
    const result = patchPackage(root);
    assert.deepEqual(result, {
      changed: true, version: OPENCLAW_VERSION, file: TARGET_FILE, sha256: PATCHED_SHA256,
    });
    const actual = fs.readFileSync(path.join(root, TARGET_FILE));
    assert.deepEqual(actual, patched);
    assert.equal(actual.toString("utf8"), original.toString("utf8").replace(OLD_ANCHOR, NEW_ANCHOR));
    assert.deepEqual(fs.readFileSync(path.join(root, "package.json")), manifest);
    assert.equal(fs.readFileSync(untouched, "utf8"), `// ${OLD_ANCHOR}\n`);
    assert.equal(sha256(original), ORIGINAL_SHA256);
  });

  it("accepts an already patched file at the same path without writing", (t) => {
    const root = makePackage(t, original);
    patchPackage(root);
    const before = fs.statSync(path.join(root, TARGET_FILE), { bigint: true });
    const write = t.mock.method(fs, "writeFileSync", () => assert.fail("Already patched packages must not be written."));
    assert.equal(patchPackage(root).changed, false);
    assert.equal(write.mock.callCount(), 0);
    const after = fs.statSync(path.join(root, TARGET_FILE), { bigint: true });
    for (const field of ["ino", "mtimeNs", "ctimeNs", "size", "mode"]) {
      assert.equal(after[field], before[field], field);
    }
    assert.deepEqual(fs.readFileSync(path.join(root, TARGET_FILE)), patched);
    assert.equal(patchSource(patched).source, patched);
  });

  it("still rejects an unknown version with either known source hash", (t) => {
    for (const source of [original, patched]) {
      assertRefusedWithoutWrites(
        t, makePackage(t, source, { version: "2026.10.0" }), /Expected OpenClaw package version/,
      );
    }
  });

  it("rejects modified pinned source before writing", (t) => {
    assertRefusedWithoutWrites(
      t, makePackage(t, Buffer.concat([original, Buffer.from("\n")])), /Unsupported OpenClaw/,
    );
    assertRefusedWithoutWrites(
      t, makePackage(t, Buffer.concat([patched, Buffer.from("\n")])), /Unsupported OpenClaw/,
    );
  });

  it("omits only the empty subagent fact when spawning is available", async () => {
    const params = paramsWithTools("sessions_spawn");
    const empty = compileSubagentContext(patched.toString("utf8"), [], { active: [], recent: [] });
    const dependencies = { buildActiveSubagentRuntimeContext: empty };
    assert.deepEqual(await compileRuntimeFacts(original.toString("utf8"), dependencies).build(params), [
      { kind: "conversation-data", text: "## Active Subagents\nnone" },
    ]);
    assert.deepEqual(await compileRuntimeFacts(patched.toString("utf8"), dependencies).build(params), []);
  });

  it("keeps agentcore replay non-append-only and removes the previous turn's active carrier", async () => {
    const policySource = fs.readFileSync(path.join(packageDir, "dist/helpers-Dc7h-q-4.mjs"), "utf8");
    const policyStart = policySource.indexOf("const DEFAULT_TRANSCRIPT_POLICY = {");
    const policyEnd = policySource.indexOf("//#endregion", policyStart);
    assert.ok(policyStart >= 0 && policyEnd > policyStart);
    const dependencies = {
      normalizeLowercaseStringOrEmpty: (value) => typeof value === "string" ? value.trim().toLowerCase() : "",
      normalizeProviderId: (provider) => {
        assert.equal(provider, "agentcore");
        return provider;
      },
      resolveProviderRuntimePlugin: ({ provider }) => {
        assert.equal(provider, "agentcore");
        return undefined;
      },
      isGoogleModelApi: (api) => {
        assert.equal(api, "openai-completions");
        return false;
      },
      bindsClaudeThinkingPrefix: () => assert.fail("The target transport must not use Anthropic replay ownership."),
      shouldDropClaudeThinkingBlocks: () => assert.fail("The target transport must not use Anthropic thinking rules."),
    };
    const { DEFAULT_TRANSCRIPT_POLICY, resolveTranscriptPolicy } = vm.compileFunction(
      `${policySource.slice(policyStart, policyEnd)}\nreturn { DEFAULT_TRANSCRIPT_POLICY, resolveTranscriptPolicy };`,
      Object.keys(dependencies),
    )(...Object.values(dependencies));
    assert.equal(DEFAULT_TRANSCRIPT_POLICY.appendOnlyRuntimeContext, false);
    for (const modelId of ["bedrock-agentcore", "bedrock-agentcore-subagent"]) {
      const policy = resolveTranscriptPolicy({ provider: "agentcore", modelApi: "openai-completions", modelId });
      assert.equal(policy.appendOnlyRuntimeContext, false);
      assert.equal(policy.sanitizeToolCallIds, true);
      assert.equal(policy.toolCallIdMode, "strict");
    }

    const contextSource = fs.readFileSync(
      path.join(packageDir, "dist/internal-runtime-context-BH-o1oq1.mjs"), "utf8",
    );
    const contextStart = contextSource.indexOf("function isOpenClawRuntimeContextCustomMessage(message) {");
    const stripStart = contextSource.indexOf("function stripHistoricalRuntimeContextCustomMessages(messages) {");
    const contextEnd = contextSource.indexOf("\n/**", stripStart);
    assert.ok(contextStart >= 0 && stripStart > contextStart && contextEnd > stripStart);
    const { retainRuntimeContextMessageForPrompt, stripHistoricalRuntimeContextCustomMessages } = vm.compileFunction(
      `${contextSource.slice(contextStart, contextEnd)}\nreturn { retainRuntimeContextMessageForPrompt, stripHistoricalRuntimeContextCustomMessages };`,
    )();

    const params = paramsWithTools("sessions_spawn");
    const activeFacts = await compileRuntimeFacts(patched.toString("utf8"), {
      buildActiveSubagentRuntimeContext: () => "## Active Subagents\n- run=previous-run; status=running",
    }).build(params);
    assert.equal(activeFacts.length, 1);
    const emptyFacts = await compileRuntimeFacts(patched.toString("utf8")).build(params);
    assert.deepEqual(emptyFacts, []);
    const previousUser = { role: "user", content: "Start the task." };
    const oldCarrier = {
      role: "custom", customType: "openclaw.runtime-context", content: activeFacts[0].text,
      details: { source: "openclaw-runtime-context", runtimeContextCarrier: true, fragments: activeFacts },
    };
    const assistant = { role: "assistant", content: "The task has finished." };
    const currentUser = {
      role: "user",
      content: "Keep <<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>> Runtime: and ## Active Subagents in my text.",
    };
    const history = deepFreeze([previousUser, oldCarrier, assistant, currentUser]);
    const snapshot = structuredClone(history);
    const owner = retainRuntimeContextMessageForPrompt(oldCarrier);
    assert.ok(stripHistoricalRuntimeContextCustomMessages(history).includes(oldCarrier));
    owner.release();
    const replay = stripHistoricalRuntimeContextCustomMessages(history);
    assert.deepEqual(replay, [previousUser, assistant, currentUser]);
    assert.equal(replay.at(-1), currentUser);
    assert.deepEqual(history, snapshot);
  });

  it("keeps an empty snapshot empty without spawning and forwards context unchanged", async () => {
    for (const tools of [[], ["sessions_spawn"]]) {
      const params = paramsWithTools(...tools);
      const snapshot = structuredClone(params);
      const { build, calls } = compileRuntimeFacts(patched.toString("utf8"));
      assert.deepEqual(await build(params), []);
      assert.deepEqual(calls.subagent, [{
        cfg: params.cfg, controllerSessionKey: params.sessionKey,
        controllerAgentId: params.agentId, includeSpawnContext: tools.includes("sessions_spawn"),
      }]);
      assert.equal(calls.subagent[0].cfg, params.cfg);
      assert.equal(calls.media[0], params);
      assert.deepEqual(calls.scope, []);
      assert.deepEqual(calls.sessions, []);
      assert.deepEqual(calls.approved, []);
      assert.deepEqual(params, snapshot);
    }
  });

  it("retains real active subagent facts and marker-bearing task data", async () => {
    const task = "Keep <<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>> Runtime: user-authored text.";
    const entry = deepFreeze({
      runId: "active-run", childSessionKey: "agent:main:subagent:active", execution: { status: "running" },
    });
    const list = deepFreeze({
      active: [{
        runId: entry.runId, sessionKey: entry.childSessionKey, status: "running",
        execution: { state: "running" }, label: "user label", task,
      }],
      recent: [],
    });
    const snapshot = structuredClone(list);
    const dependencies = {
      buildActiveSubagentRuntimeContext: compileSubagentContext(patched.toString("utf8"), [entry], list),
    };
    const params = paramsWithTools("sessions_spawn");
    const result = await compileRuntimeFacts(patched.toString("utf8"), dependencies).build(params);
    assert.deepEqual(result, await compileRuntimeFacts(original.toString("utf8"), dependencies).build(params));
    assert.equal(result.length, 1);
    assert.equal(result[0].kind, "conversation-data");
    assert.match(result[0].text, /^## Active Subagents\n/);
    assert.ok(result[0].text.includes(`task_json=${JSON.stringify(task)}`));
    assert.deepEqual(list, snapshot);
  });

  it("retains real pending completions even without the spawn capability", async () => {
    const entry = deepFreeze({
      runId: "pending-run", childSessionKey: "agent:main:subagent:pending",
      execution: { status: "terminal", endedAt: 100, outcome: { status: "ok" } },
      completion: { required: true }, delivery: { status: "pending" },
      task: "Keep Runtime: and <<<END_OPENCLAW_INTERNAL_CONTEXT>>> in this task.",
      result: "Result with <<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>> and Runtime: text.",
    });
    const dependencies = {
      buildActiveSubagentRuntimeContext: compileSubagentContext(
        patched.toString("utf8"), [entry], { active: [], recent: [] },
      ),
    };
    for (const tools of [[], ["sessions_spawn"]]) {
      const params = paramsWithTools(...tools);
      const result = await compileRuntimeFacts(patched.toString("utf8"), dependencies).build(params);
      assert.deepEqual(result, await compileRuntimeFacts(original.toString("utf8"), dependencies).build(params));
      assert.equal(result.length, 1);
      assert.equal(result[0].kind, "conversation-data");
      assert.match(result[0].text, /^## Child results awaiting delivery\n/);
      assert.ok(result[0].text.includes(`task_json=${JSON.stringify(entry.task)}`));
      assert.ok(result[0].text.includes(`result_json=${JSON.stringify(entry.result)}`));
    }
  });

  it("preserves process, subagent, media and approved-executable facts in order", async () => {
    const params = paramsWithTools("process", "exec", "sessions_spawn", "image_generate", "video_generate");
    const processSessions = deepFreeze([
      { sessionId: "b", status: "running", name: "second" },
      { sessionId: "a", status: "running", name: "first", pid: 42, cwd: "/tmp/work" },
    ]);
    const listArgs = [];
    const overrides = {
      process: { platform: "win32" },
      listActiveProcessSessionReferences: (args) => {
        listArgs.push(args);
        return processSessions;
      },
      buildActiveSubagentRuntimeContext: () => "## Active Subagents\nactive",
      buildMediaTaskRuntimeContext: async (input) => {
        assert.equal(input, params);
        return "## Media Generation Tasks\nimage and video active";
      },
    };
    const { build, calls } = compileRuntimeFacts(patched.toString("utf8"), overrides);
    const result = await build(params);
    assert.deepEqual(result, [
      { kind: "conversation-data", text: "## Approved executables\nallowed" },
      { kind: "conversation-data", text: "Active exec sessions:\n- a running pid=42 cwd=/tmp/work :: first\n- b running :: second" },
      { kind: "conversation-data", text: "## Active Subagents\nactive" },
      { kind: "conversation-data", text: "## Media Generation Tasks\nimage and video active" },
    ]);
    assert.deepEqual(result, await compileRuntimeFacts(original.toString("utf8"), overrides).build(params));
    assert.deepEqual(calls.approved, [params.agentId]);
    assert.equal(calls.scope[0], params);
    assert.deepEqual(listArgs, [{ scopeKey: "process-scope" }, { scopeKey: "process-scope" }]);
    assert.equal(processSessions[0].sessionId, "b");
  });

  it("retains other empty fact families when the empty subagent fact is omitted", async () => {
    const params = paramsWithTools("process", "sessions_spawn", "image_generate");
    const media = "## Media Generation Tasks\n- tool=image_generate; none";
    const { build } = compileRuntimeFacts(patched.toString("utf8"), {
      buildMediaTaskRuntimeContext: async () => media,
    });
    assert.deepEqual(await build(params), [
      { kind: "conversation-data", text: "Active exec sessions:\nnone" },
      { kind: "conversation-data", text: media },
    ]);
  });

  it("does not inspect or mutate user messages containing runtime markers", async () => {
    const user = deepFreeze({
      role: "user",
      content: "<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>\n## Active Subagents\nnone\nRuntime: user task\n<<<END_OPENCLAW_INTERNAL_CONTEXT>>>",
    });
    const params = deepFreeze({ ...paramsWithTools("sessions_spawn"), messages: [user] });
    const snapshot = structuredClone(params);
    const { build } = compileRuntimeFacts(patched.toString("utf8"));
    assert.deepEqual(await build(params), []);
    assert.deepEqual(params, snapshot);
    assert.equal(params.messages[0], user);
  });
});
