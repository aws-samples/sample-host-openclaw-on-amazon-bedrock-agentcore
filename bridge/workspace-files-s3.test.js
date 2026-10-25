/**
 * Tests for bridge/workspace-files-s3.js: system-prompt workspace file
 * location, the one-time copy from the legacy key, seeding, the contract's
 * AGENTS.md upload, and the session policy staying unchanged.
 * Run: node --test workspace-files-s3.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const wf = require("./workspace-files-s3");

class GetObjectCommand { constructor(input) { this.input = input; this.op = "Get"; } }
class PutObjectCommand { constructor(input) { this.input = input; this.op = "Put"; } }
const commands = { GetObjectCommand, PutObjectCommand };

function notFound() {
  const e = new Error("NoSuchKey");
  e.name = "NoSuchKey";
  e.$metadata = { httpStatusCode: 404 };
  return e;
}
function preconditionFailed() {
  const e = new Error("PreconditionFailed");
  e.name = "PreconditionFailed";
  e.$metadata = { httpStatusCode: 412 };
  return e;
}

/** In-memory S3 that honours IfNoneMatch: "*". */
function fakeS3(initial = {}) {
  const objects = new Map(Object.entries(initial));
  const calls = [];
  return {
    objects,
    calls,
    async send(cmd, opts) {
      calls.push({ op: cmd.op, input: cmd.input, opts });
      const { Key, Body, IfNoneMatch } = cmd.input;
      if (cmd.op === "Get") {
        if (!objects.has(Key)) throw notFound();
        return { Body: { transformToString: async () => objects.get(Key) } };
      }
      if (cmd.op === "Put") {
        if (IfNoneMatch === "*" && objects.has(Key)) throw preconditionFailed();
        objects.set(Key, Body);
        return {};
      }
      throw new Error(`unexpected op ${cmd.op}`);
    },
  };
}

const bucket = "test-bucket";
const ns = "telegram_12345";

describe("workspace file keys", () => {
  it("live under _system/<namespace>/ at the bucket root, outside the user prefix", () => {
    const key = wf.workspaceKey(ns, "IDENTITY.md");
    assert.equal(key, "_system/telegram_12345/IDENTITY.md");
    assert.ok(!key.startsWith(`${ns}/`), "must not be inside the user-writable namespace prefix");
    assert.equal(wf.legacyWorkspaceKey(ns, "IDENTITY.md"), "telegram_12345/IDENTITY.md");
  });

  it("reject namespaces or filenames that could escape the prefix", () => {
    for (const bad of ["", "a/b", "..", "x/../y"]) {
      assert.throws(() => wf.workspaceKey(bad, "IDENTITY.md"));
      assert.throws(() => wf.workspaceKey(ns, bad));
    }
  });
});

describe("readWorkspaceFile", () => {
  it("reads the new key and does not touch the legacy key when it exists", async () => {
    const s3 = fakeS3({
      "_system/telegram_12345/SOUL.md": "new soul",
      "telegram_12345/SOUL.md": "old soul",
    });
    const r = await wf.readWorkspaceFile({ s3, commands, bucket, namespace: ns, filename: "SOUL.md" });
    assert.deepEqual(r, { content: "new soul", source: "system" });
    assert.deepEqual(s3.calls.map((c) => c.input.Key), ["_system/telegram_12345/SOUL.md"]);
  });

  it("copies a legacy file once when the new key is empty, and keeps the legacy file", async () => {
    const s3 = fakeS3({ "telegram_12345/IDENTITY.md": "  name: Sam  " });
    const r = await wf.readWorkspaceFile({ s3, commands, bucket, namespace: ns, filename: "IDENTITY.md" });
    assert.deepEqual(r, { content: "name: Sam", source: "migrated" });
    assert.equal(s3.objects.get("_system/telegram_12345/IDENTITY.md"), "name: Sam");
    assert.equal(s3.objects.get("telegram_12345/IDENTITY.md"), "  name: Sam  ", "legacy object must be left in place");
    const put = s3.calls.find((c) => c.op === "Put");
    assert.equal(put.input.IfNoneMatch, "*", "the copy must be a conditional write");
    assert.ok(s3.calls.every((c) => c.op === "Get" || c.op === "Put"), "no delete is ever sent");

    // A second read is served from the new key only.
    s3.calls.length = 0;
    const r2 = await wf.readWorkspaceFile({ s3, commands, bucket, namespace: ns, filename: "IDENTITY.md" });
    assert.deepEqual(r2, { content: "name: Sam", source: "system" });
    assert.equal(s3.calls.length, 1);
  });

  it("does not overwrite a new-key object written by a concurrent request", async () => {
    const s3 = fakeS3({ "telegram_12345/MEMORY.md": "legacy memory" });
    const realSend = s3.send.bind(s3);
    let first = true;
    s3.send = async (cmd, opts) => {
      // Simulate another request winning the race between our miss and our copy.
      if (cmd.op === "Put" && first) {
        first = false;
        s3.objects.set("_system/telegram_12345/MEMORY.md", "written by the other request");
      }
      return realSend(cmd, opts);
    };
    const r = await wf.readWorkspaceFile({ s3, commands, bucket, namespace: ns, filename: "MEMORY.md" });
    assert.deepEqual(r, { content: "written by the other request", source: "system" });
    assert.equal(s3.objects.get("_system/telegram_12345/MEMORY.md"), "written by the other request");
  });

  it("returns empty when neither key exists and writes nothing", async () => {
    const s3 = fakeS3();
    const r = await wf.readWorkspaceFile({ s3, commands, bucket, namespace: ns, filename: "USER.md" });
    assert.deepEqual(r, { content: "", source: "none" });
    assert.ok(!s3.calls.some((c) => c.op === "Put"));
  });

  it("never copies a legacy AGENTS.md (the contract regenerates it)", async () => {
    const s3 = fakeS3({ "telegram_12345/AGENTS.md": "user-written agents" });
    const r = await wf.readWorkspaceFile({ s3, commands, bucket, namespace: ns, filename: "AGENTS.md" });
    assert.deepEqual(r, { content: "", source: "none" });
    assert.ok(!s3.objects.has("_system/telegram_12345/AGENTS.md"));
    assert.deepEqual(s3.calls.map((c) => c.input.Key), ["_system/telegram_12345/AGENTS.md"]);
  });

  it("propagates unexpected S3 errors", async () => {
    const s3 = { async send() { const e = new Error("AccessDenied"); e.name = "AccessDenied"; throw e; } };
    await assert.rejects(
      wf.readWorkspaceFile({ s3, commands, bucket, namespace: ns, filename: "SOUL.md" }),
      /AccessDenied/,
    );
  });
});

describe("seedWorkspaceFile", () => {
  it("writes a default only when the key is absent", async () => {
    const s3 = fakeS3({ "_system/telegram_12345/SOUL.md": "existing" });
    assert.equal(await wf.seedWorkspaceFile({ s3, commands, bucket, namespace: ns, filename: "SOUL.md", content: "default" }), false);
    assert.equal(s3.objects.get("_system/telegram_12345/SOUL.md"), "existing");
    assert.equal(await wf.seedWorkspaceFile({ s3, commands, bucket, namespace: ns, filename: "USER.md", content: "default" }), true);
    assert.equal(s3.objects.get("_system/telegram_12345/USER.md"), "default");
  });
});

describe("contract AGENTS.md upload", () => {
  it("publishAgentsMd overwrites the new key and forwards the abort signal", async () => {
    const s3 = fakeS3({ "_system/telegram_12345/AGENTS.md": "old" });
    const ac = new AbortController();
    const key = await wf.publishAgentsMd({ s3, commands, bucket, namespace: ns, content: "fresh", abortSignal: ac.signal });
    assert.equal(key, "_system/telegram_12345/AGENTS.md");
    assert.equal(s3.objects.get(key), "fresh");
    const put = s3.calls[0];
    assert.equal(put.input.IfNoneMatch, undefined, "AGENTS.md is regenerated, so the upload must overwrite");
    assert.equal(put.opts.abortSignal, ac.signal);
  });

  it("the contract publishes AGENTS.md with the execution-role client, not the scoped client", () => {
    const src = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf8");
    const start = src.indexOf("// Also upload to S3 so the proxy's system prompt injection picks it up.");
    assert.ok(start > 0, "AGENTS.md upload block not found");
    const block = src.slice(start, src.indexOf("configPublishPromise = Promise.all", start));
    assert.match(block, /workspaceFilesS3\.publishAgentsMd\(/);
    assert.match(block, /s3: executionRoleS3Client\(\)/);
    assert.doesNotMatch(block, /workspaceSync\.getS3Client\(\)/,
      "the scoped-credential client cannot write _system/<namespace>/");
    assert.doesNotMatch(block, /Key: `\$\{namespace\}\//, "must not write inside the user prefix");
  });

  it("the Dockerfile ships workspace-files-s3.js next to the proxy and contract", () => {
    const dockerfile = fs.readFileSync(path.join(__dirname, "Dockerfile"), "utf8");
    assert.match(dockerfile, /^COPY workspace-files-s3\.js \/app\/workspace-files-s3\.js$/m);
  });
});

describe("session policy is unchanged by the _system/ location", () => {
  const { buildSessionPolicy } = require("./scoped-credentials");
  const ns46 = "feishu_ou_" + "0123456789abcdef".repeat(2) + "abcd";
  const opts = {
    bucket: "openclaw-user-files-123456789012-ap-southeast-2-production",
    namespace: ns46,
    internalUserId: "user_0123456789abcdef",
    cmkArn: "arn:aws:kms:ap-southeast-2:123456789012:key/abcdef12-3456-7890-abcd-ef1234567890",
    eventbridgeRoleArn: "arn:aws:iam::123456789012:role/openclaw-cron-scheduler-role-ap-southeast-2-production",
    identityTableArn: "arn:aws:dynamodb:ap-southeast-2:123456789012:table/openclaw-identity-production",
    scheduleGroupArn: "arn:aws:scheduler:ap-southeast-2:123456789012:schedule-group/openclaw-cron-production",
    region: "ap-southeast-2",
    account: "123456789012",
  };

  it("adds no statement for _system/ and stays under the 2048-character STS limit (46-char case)", () => {
    const policy = buildSessionPolicy(opts);
    assert.ok(!policy.includes("_system"), "no Deny or other statement for _system/ is needed");
    assert.ok(policy.length <= 2048, `policy length ${policy.length} must stay within the 2048 STS limit`);
  });

  it("no S3 statement in the policy covers the system-prompt keys", () => {
    const parsed = JSON.parse(buildSessionPolicy(opts));
    const key = `arn:aws:s3:::${opts.bucket}/${wf.workspaceKey(ns46, "IDENTITY.md")}`;
    for (const st of parsed.Statement) {
      const actions = [].concat(st.Action);
      if (!actions.some((a) => a.startsWith("s3:"))) continue;
      for (const r of [].concat(st.Resource)) {
        const re = new RegExp("^" + r.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*") + "$");
        if (actions.includes("s3:ListBucket")) continue; // bucket ARN, prefix-conditioned to <ns>/*
        assert.ok(!re.test(key), `statement resource ${r} must not match ${key}`);
      }
    }
    const list = parsed.Statement.find((s) => [].concat(s.Action).includes("s3:ListBucket"));
    assert.deepEqual([].concat(list.Condition.StringLike["s3:prefix"]), [`${ns46}/*`]);
  });
});
