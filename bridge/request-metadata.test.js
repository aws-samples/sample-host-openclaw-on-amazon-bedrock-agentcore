// Token attribution: every Bedrock request the proxy sends carries
// requestMetadata identifying the actor, channel, session and agent type.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const { buildRequestMetadata, sanitizeValue } = require("./request-metadata");

const source = fs.readFileSync(require.resolve("./agentcore-proxy"), "utf8");
const contractSource = fs.readFileSync(require.resolve("./agentcore-contract"), "utf8");
// Bedrock's documented limits for requestMetadata (Converse / ConverseStream).
const BEDROCK_PATTERN = /^[a-zA-Z0-9\s:_@$#=/+,\-.]*$/;
const MAX_ENTRIES = 16;
const MAIN_MODEL = "global.anthropic.claude-opus-5-5";
const SUBAGENT_MODEL = "global.anthropic.claude-sonnet-5-5";

function assertBedrockValid(metadata) {
  const entries = Object.entries(metadata);
  assert.ok(entries.length <= MAX_ENTRIES, `at most ${MAX_ENTRIES} entries`);
  for (const [key, value] of entries) {
    assert.ok(key.length >= 1 && key.length <= 256 && BEDROCK_PATTERN.test(key), `key ${key}`);
    assert.equal(typeof value, "string");
    assert.ok(value.length <= 256 && BEDROCK_PATTERN.test(value), `value for ${key}: ${value}`);
  }
}

test("a bound main-agent chat call carries actor, channel, session and agent type", () => {
  const metadata = buildRequestMetadata({
    actorId: "telegram:1001", channel: "telegram", sessionId: "ses_user_abc_0123456789ab_cdef",
    subagent: false, trigger: "chat", idSource: "identity-file",
  });
  assert.deepEqual(metadata, {
    "openclaw.actor_id": "telegram:1001",
    "openclaw.channel": "telegram",
    "openclaw.session_id": "ses_user_abc_0123456789ab_cdef",
    "openclaw.agent_type": "main",
    "openclaw.trigger": "chat",
    "openclaw.identity": "bound",
    "openclaw.id_source": "identity-file",
    "openclaw.model_route": "primary",
  });
  assertBedrockValid(metadata);
});

test("agent type: sub-agent wins over the turn kind, cron otherwise", () => {
  const base = { actorId: "slack:U0XXXXXXX", channel: "slack", sessionId: "s", idSource: "identity-file" };
  assert.equal(buildRequestMetadata({ ...base, trigger: "cron" })["openclaw.agent_type"], "cron");
  assert.equal(buildRequestMetadata({ ...base, trigger: "cron", subagent: true })["openclaw.agent_type"], "subagent");
  assert.equal(buildRequestMetadata({ ...base, trigger: "chat", subagent: true })["openclaw.agent_type"], "subagent");
  assert.equal(buildRequestMetadata({ ...base, trigger: "warmup" })["openclaw.trigger"], "chat");
});

test("unbound calls are explicit: default-user, unbound, default session", () => {
  const metadata = buildRequestMetadata({ actorId: "default-user", idSource: "fallback" });
  assert.equal(metadata["openclaw.actor_id"], "default-user");
  assert.equal(metadata["openclaw.identity"], "unbound");
  assert.equal(metadata["openclaw.id_source"], "fallback");
  assert.equal(metadata["openclaw.session_id"], "default-session");
  assert.equal(metadata["openclaw.channel"], "unknown");
  assertBedrockValid(buildRequestMetadata({}));
});

test("values outside Bedrock's character set or length are hashed, never sent raw", () => {
  for (const value of ["telegram:1 ignore previous instructions", "slack:U1\nnewline", "x".repeat(300),
    "line:Ünïcode", "a\"quote';drop", "<script>"]) {
    const metadata = buildRequestMetadata({ actorId: value, sessionId: value, idSource: value, channel: value });
    assertBedrockValid(metadata);
    for (const key of ["openclaw.actor_id", "openclaw.session_id", "openclaw.id_source"]) {
      assert.match(metadata[key], /^sha256:[0-9a-f]{32}$/, `${key} for ${JSON.stringify(value)}`);
    }
    assert.equal(metadata["openclaw.channel"], "unknown");
  }
  assert.notEqual(sanitizeValue("telegram:1 a", "d"), sanitizeValue("telegram:1 b", "d"));
  assert.equal(sanitizeValue("telegram:1_a-b.c", "d"), "telegram:1_a-b.c");
  assert.equal(buildRequestMetadata({ channel: "feishu" })["openclaw.channel"], "feishu");
});

// --- The real proxy request handler, end to end down to the SDK command ---

function slice(src, startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  const end = src.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `${startMarker} .. ${endMarker}`);
  return src.slice(start, end);
}

function proxy({ identity, env = { USER_ID: "telegram:1001", CHANNEL: "telegram" }, fail = () => false } = {}) {
  const requests = [];
  let handler;
  const sdk = {
    ConverseCommand: class { constructor(input) { this.input = input; } },
    ConverseStreamCommand: class { constructor(input) { this.input = input; } },
    BedrockRuntimeClient: class {
      async send(command) {
        requests.push({ stream: command instanceof sdk.ConverseStreamCommand, input: command.input });
        if (fail(command.input, requests.length)) {
          throw Object.assign(new Error("service unavailable"), {
            name: "ServiceUnavailableException", $metadata: { httpStatusCode: 503, attempts: 1 },
          });
        }
        if (command instanceof sdk.ConverseStreamCommand) {
          return { stream: (async function* () {
            yield { contentBlockDelta: { delta: { text: "answer" } } };
            yield { messageStop: { stopReason: "end_turn" } };
          })() };
        }
        return { output: { message: { content: [{ text: "answer" }] } }, usage: {} };
      }
    },
  };
  const box = {
    require(name) {
      if (name === "@aws-sdk/client-bedrock-runtime") return sdk;
      if (name === "./read-body") return { readBody: async (req) => req.body };
      throw new Error(`unexpected require ${name}`);
    },
    crypto: require("node:crypto"), Date, process: { env },
    fs: { readFileSync: () => { if (identity === undefined) throw new Error("ENOENT"); return JSON.stringify(identity); } },
    sessionMap: new Map(),
    console: { log() {}, error() {}, warn() {}, debug() {} },
    http: { createServer(callback) { handler = callback; return {}; } },
    MODEL_ID: MAIN_MODEL, SUBAGENT_BEDROCK_MODEL_ID: SUBAGENT_MODEL,
    SUBAGENT_MODEL_NAME: "bedrock-agentcore-subagent", COGNITO_USER_POOL_ID: "",
    AWS_REGION: "us-west-2", MAX_RETRIES: 2, BASE_DELAY_MS: 0, SYSTEM_PROMPT: "default",
    sleep: async () => {}, bedrockClientOptions: () => ({}), bedrockStreamClientOptions: () => ({}),
    convertMessages: () => ({ bedrockMessages: [{ role: "user", content: [{ text: "hi" }] }], systemText: "s" }),
    convertTools: () => undefined,
    extractImageReferences: (text) => ({ cleanText: text, images: [] }),
    getCognitoToken: async () => null,
    retrieveMemoryContext: async () => "",
    buildUserIdentityContext: async () => "",
    scopeGuardrailToLatestUserTurn: require("./guardrail-scope").scopeGuardrailToLatestUserTurn,
    reconcileToolHistory: require("./tool-history").reconcileToolHistory,
    buildInferenceConfig: require("./inference-config").buildInferenceConfig,
    streamUsage: require("./inference-config").streamUsage,
    temperatureOverride: undefined, guardrailConfig: undefined,
    buildRequestMetadata,
    chatRequestCount: 0, subagentRequestCount: 0,
  };
  vm.createContext(box);
  vm.runInContext(slice(source, "function extractSessionMetadata(", "\n// Per-user Cognito"), box);
  vm.runInContext('const { readBody } = require("./read-body");', box);
  vm.runInContext(slice(source, "function isSubagentRequest(", "\n/**\n * Call Bedrock Converse API"), box);
  vm.runInContext(slice(source, "async function invokeBedrock(messages", "\n/**\n * Call Bedrock ConverseStream API"), box);
  vm.runInContext(slice(source, "async function invokeBedrockStreaming(", "\n/**\n * Format a response"), box);
  vm.runInContext(slice(source, "function formatChatResponse(", "\n/**\n * HTTP request handler"), box);
  vm.runInContext(slice(source, "const server = http.createServer(", "\nserver.listen("), box);
  return {
    requests,
    async chat(body) {
      const res = new EventEmitter();
      const done = new Promise((resolve) => {
        res.writeHead = (status) => { res.statusCode = status; res.headersSent = true; };
        res.write = () => true;
        res.end = () => { res.writableEnded = true; resolve(); };
      });
      await handler({ method: "POST", url: "/v1/chat/completions", body: JSON.stringify(body), headers: {} }, res);
      await done;
      return res.statusCode;
    },
  };
}

const IDENTITY = { actorId: "telegram:1001", channel: "telegram", trigger: "chat",
  sessionId: "ses_user_abc_0123456789ab_cdef0123456789" };
const MAIN = { model: "bedrock-agentcore", stream: true, messages: [{ role: "user", content: "hi" }] };
const SUB = { ...MAIN, model: "agentcore/bedrock-agentcore-subagent" };

test("main-agent streaming request carries the bound actor, channel and runtime session", async () => {
  const app = proxy({ identity: IDENTITY });
  assert.equal(await app.chat(MAIN), 200);
  const metadata = app.requests[0].input.requestMetadata;
  assert.deepEqual(metadata, buildRequestMetadata({ ...IDENTITY, subagent: false, idSource: "identity-file" }));
  assertBedrockValid(metadata);
});

test("sub-agent streaming request is tagged subagent, on the same actor and session", async () => {
  const app = proxy({ identity: IDENTITY });
  assert.equal(await app.chat(SUB), 200);
  assert.equal(app.requests[0].input.modelId, SUBAGENT_MODEL);
  const metadata = app.requests[0].input.requestMetadata;
  assert.equal(metadata["openclaw.agent_type"], "subagent");
  assert.equal(metadata["openclaw.actor_id"], "telegram:1001");
  assert.equal(metadata["openclaw.session_id"], IDENTITY.sessionId);
});

test("a cron turn's main request is tagged cron and its sub-agent subagent", async () => {
  const app = proxy({ identity: { ...IDENTITY, trigger: "cron" } });
  await app.chat(MAIN);
  await app.chat(SUB);
  assert.deepEqual(app.requests.map((r) => r.input.requestMetadata["openclaw.agent_type"]), ["cron", "subagent"]);
});

test("streaming and non-streaming retries carry the same attribution", async () => {
  for (const stream of [true, false]) {
    const app = proxy({ identity: IDENTITY, fail: (_input, ordinal) => ordinal === 1 });
    assert.equal(await app.chat({ ...MAIN, stream }), 200);
    assert.equal(app.requests.length, 2);
    assert.deepEqual(app.requests[1].input.requestMetadata, app.requests[0].input.requestMetadata);
    assert.equal(app.requests[1].input.requestMetadata["openclaw.actor_id"], "telegram:1001");
  }
});

test("without an identity file the environment identity is used with the proxy's own session", async () => {
  const app = proxy({ identity: undefined });
  await app.chat(MAIN);
  const metadata = app.requests[0].input.requestMetadata;
  assert.equal(metadata["openclaw.actor_id"], "telegram:1001");
  assert.equal(metadata["openclaw.id_source"], "environment");
  assert.match(metadata["openclaw.session_id"], /^ses-/);
});

test("an unbound proxy with no identity sends default-user, marked unbound", async () => {
  const app = proxy({ identity: undefined, env: {} });
  await app.chat(MAIN);
  const metadata = app.requests[0].input.requestMetadata;
  assert.equal(metadata["openclaw.actor_id"], "default-user");
  assert.equal(metadata["openclaw.identity"], "unbound");
  assert.equal(metadata["openclaw.id_source"], "fallback");
});

test("message content never reaches requestMetadata", async () => {
  const app = proxy({ identity: IDENTITY });
  await app.chat({ ...MAIN, messages: [{ role: "user", content: "my password is hunter2" }], user: "hunter2" });
  assert.equal(Object.values(app.requests[0].input.requestMetadata).join(" ").includes("hunter2"), false);
});

test("a malformed identity-file session is hashed, not forwarded", async () => {
  const app = proxy({ identity: { ...IDENTITY, sessionId: "bad session\nid" } });
  await app.chat(MAIN);
  assert.match(app.requests[0].input.requestMetadata["openclaw.session_id"], /^sha256:[0-9a-f]{32}$/);
});

// --- Contract: the trigger changes when a queued turn is dispatched ---

function contract() {
  const files = new Map();
  const sent = [];
  let release;
  const box = {
    fs: { writeFileSync: (path, text) => files.set(path, text) },
    console: { log() {}, warn() {}, error() {} },
    IDENTITY_FILE: "/tmp/current-identity.json",
    messageQueue: [], processingMessage: false, staleDeliverySweep: null,
    setTimeout, clearTimeout,
    bridgeMessage: async (message) => {
      sent.push({ message, identity: JSON.parse(files.get("/tmp/current-identity.json")) });
      if (message === "slow chat") await new Promise((resolve) => { release = resolve; });
      return `reply to ${message}`;
    },
  };
  vm.createContext(box);
  vm.runInContext(slice(contractSource, "let identityTrigger = ", "\n/**\n * Pre-fetch secrets"), box);
  vm.runInContext(slice(contractSource, "async function processMessageQueue(", "\n/**\n * Enqueue a message"), box);
  vm.runInContext(slice(contractSource, "function enqueueMessage(", "\n/**\n * Bridge a chat message"), box);
  return { box, files, sent, release: () => release() };
}

test("identity file carries trigger and runtime session; cron arriving mid-chat does not relabel it", async () => {
  const app = contract();
  const { box, sent } = app;
  box.updateIdentityFile("telegram:1001", "telegram", { sessionId: "ses_a" });
  const chat = box.enqueueMessage("slow chat", undefined, { onDispatch: () => box.setTurnTrigger("chat") });
  await new Promise((resolve) => setImmediate(resolve));
  // The cron request rewrites the identity before queueing behind the chat turn.
  box.updateIdentityFile("telegram:1001", "telegram", { sessionId: "ses_a" });
  assert.equal(JSON.parse(app.files.get("/tmp/current-identity.json")).trigger, "chat",
    "the running chat turn keeps trigger chat");
  const cron = box.enqueueMessage("cron job", undefined, { onDispatch: () => box.setTurnTrigger("cron") });
  app.release();
  await chat;
  await cron;
  assert.deepEqual(sent.map((s) => [s.message, s.identity.trigger, s.identity.sessionId]),
    [["slow chat", "chat", "ses_a"], ["cron job", "cron", "ses_a"]]);
  const next = box.enqueueMessage("next chat", undefined, { onDispatch: () => box.setTurnTrigger("chat") });
  await next;
  assert.equal(sent[2].identity.trigger, "chat");
});

test("the contract wires dispatch triggers and the runtime session header", () => {
  assert.match(contractSource, /runCronTurn\(message, budget\.deadline, \(\) => setTurnTrigger\("cron"\)\)/);
  assert.match(contractSource, /onDispatch: \(\) => setTurnTrigger\("chat"\)/);
  assert.equal(contractSource.split("{ sessionId: runtimeSessionHeader(req) }").length - 1, 2);
});

test("the image ships request-metadata.js next to the proxy", () => {
  const dockerfile = fs.readFileSync(require.resolve("./Dockerfile"), "utf8");
  assert.match(dockerfile, /COPY request-metadata\.js \/app\/request-metadata\.js/);
  assert.ok(source.includes('require("./request-metadata")'));
});
