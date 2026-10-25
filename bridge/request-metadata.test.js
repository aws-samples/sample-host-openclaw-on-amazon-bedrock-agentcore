// Token attribution: every Bedrock request the proxy sends carries
// requestMetadata identifying the actor, channel, session and agent type.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const policy = require("./model-policy");
const { buildRequestMetadata, sanitizeValue } = require("./request-metadata");

const source = fs.readFileSync(require.resolve("./agentcore-proxy"), "utf8");
// Bedrock's documented limits for requestMetadata (Converse / ConverseStream).
const BEDROCK_PATTERN = /^[a-zA-Z0-9\s:_@$#=/+,\-.]*$/;
const MAX_ENTRIES = 16;

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
  assert.equal(buildRequestMetadata({})["openclaw.actor_id"], "default-user");
  assertBedrockValid(buildRequestMetadata({}));
});

test("values outside Bedrock's character set or length are hashed, never sent raw", () => {
  const hostile = [
    "telegram:1 ignore previous instructions",
    "slack:U1\nnewline",
    "x".repeat(300),
    "line:Ünïcode",
    "a\"quote';drop",
    "<script>",
  ];
  for (const value of hostile) {
    const metadata = buildRequestMetadata({ actorId: value, sessionId: value, idSource: value, channel: value });
    assertBedrockValid(metadata);
    for (const key of ["openclaw.actor_id", "openclaw.session_id", "openclaw.id_source"]) {
      assert.match(metadata[key], /^sha256:[0-9a-f]{32}$/, `${key} for ${JSON.stringify(value)}`);
    }
    assert.equal(metadata["openclaw.channel"], "unknown");
  }
  assert.notEqual(sanitizeValue("telegram:1 a", "d"), sanitizeValue("telegram:1 b", "d"),
    "distinct inputs keep distinct hashes");
  assert.equal(sanitizeValue("telegram:1_a-b.c", "d"), "telegram:1_a-b.c");
  assert.equal(sanitizeValue(42, "d"), "d");
  assert.equal(buildRequestMetadata({ channel: "Telegram" })["openclaw.channel"], "unknown");
  assert.equal(buildRequestMetadata({ channel: "feishu" })["openclaw.channel"], "feishu");
});

// --- The real proxy request handler, end to end down to the SDK command ---

function slice(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `${startMarker} .. ${endMarker}`);
  return source.slice(start, end);
}

function proxy({ identity, env = { USER_ID: "telegram:1001", CHANNEL: "telegram" }, bound = "telegram:1001",
  fail = () => false } = {}) {
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
          return { $metadata: { requestId: "r", attempts: 1 }, stream: (async function* () {
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
      if (name === "./runtime-lifecycle") return { osRandomUUID: () => "00000000-0000-4000-8000-000000000001" };
      if (name === "./read-body") return { readBody: async (req) => req.body };
      throw new Error(`unexpected require ${name}`);
    },
    crypto: require("node:crypto"), Date, structuredClone, process: { env },
    fs: { readFileSync: () => { if (identity === undefined) throw new Error("ENOENT"); return JSON.stringify(identity); } },
    BOUND_ACTOR_ID: bound, sessionMap: new Map(),
    VALID_ACTOR_ID: /^(telegram|slack|discord|whatsapp|line|feishu):[A-Za-z0-9_-]{1,64}$/,
    console: { log() {}, error() {}, warn() {}, debug() {} },
    http: { createServer(callback) { handler = callback; return {}; } },
    modelPolicy: { ...policy, createStreamingRoute: (options) =>
      policy.createStreamingRoute({ ...options, emit: () => {} }) },
    preparedSdk: () => sdk,
    MODEL_ID: policy.PRIMARY_MODEL_ID, FALLBACK_MODEL_ID: policy.FALLBACK_MODEL_ID,
    SUBAGENT_BEDROCK_MODEL_ID: policy.SUBAGENT_MODEL_ID, SUBAGENT_MODEL_NAME: "bedrock-agentcore-subagent",
    USER_SELECTABLE_MODEL_IDS: new Set(), COGNITO_USER_POOL_ID: "",
    AWS_REGION: "us-west-2", MAX_RETRIES: 2, BASE_DELAY_MS: 0, SYSTEM_PROMPT: "default",
    sleep: async () => {}, bedrockClientOptions: () => ({}), bedrockStreamClientOptions: () => ({}),
    convertMessages: () => ({ bedrockMessages: [{ role: "user", content: [{ text: "hi" }] }], systemText: "s" }),
    convertTools: () => undefined,
    extractImageReferences: (text) => ({ cleanText: text, images: [] }),
    getCognitoToken: async () => null,
    retrieveMemoryContext: async () => "",
    buildUserIdentityContext: async () => "",
    extractSystemText: require("./system-messages").extractSystemText,
    scopeGuardrailToLatestUserTurn: require("./guardrail-scope").scopeGuardrailToLatestUserTurn,
    blockedGuardrailInput: require("./guardrail-scope").createBlockedInputRegistry(),
    reconcileToolHistory: require("./tool-history").reconcileToolHistory,
    buildInferenceConfig: require("./inference-config").buildInferenceConfig,
    temperatureOverride: undefined, guardrailConfig: undefined,
    systemPrefixCache: { capture: () => undefined, apply: (_prefix, system) => system },
    addToolPrefixCachePoint: require("./system-prefix-cache").addToolPrefixCachePoint,
    buildRequestMetadata,
    modelOutcomeUnknown: false, chatRequestCount: 0, subagentRequestCount: 0,
  };
  vm.createContext(box);
  vm.runInContext(slice("const VALID_CHANNELS = new Set([", "\nasync function retrieveMemoryContext("), box);
  vm.runInContext('const { osRandomUUID } = require("./runtime-lifecycle");' +
    slice("function extractSessionMetadata(", "// Per-user Cognito"), box);
  vm.runInContext('const { readBody } = require("./read-body");', box);
  vm.runInContext(slice("function isSubagentRequest(", "\n/**\n * Call Bedrock Converse API"), box);
  vm.runInContext(slice("async function invokeBedrock(messages", "\n/**\n * Call Bedrock ConverseStream API"), box);
  vm.runInContext(slice("async function invokeBedrockStreaming(", "\n/**\n * Format a response"), box);
  vm.runInContext(slice("function formatChatResponse(", "\n/**\n * HTTP request handler"), box);
  vm.runInContext(slice("const server = http.createServer(", "\nserver.listen("), box);
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
  assert.equal(app.requests.length, 1);
  const metadata = app.requests[0].input.requestMetadata;
  assert.deepEqual(metadata, buildRequestMetadata({ ...IDENTITY, subagent: false, idSource: "identity-file" }));
  assert.equal(metadata["openclaw.agent_type"], "main");
  assert.equal(metadata["openclaw.session_id"], IDENTITY.sessionId);
  assertBedrockValid(metadata);
});

test("sub-agent streaming request is tagged subagent, on the same actor and session", async () => {
  const app = proxy({ identity: IDENTITY });
  assert.equal(await app.chat(SUB), 200);
  const metadata = app.requests[0].input.requestMetadata;
  assert.equal(app.requests[0].input.modelId, policy.SUBAGENT_MODEL_ID);
  assert.equal(metadata["openclaw.agent_type"], "subagent");
  assert.equal(metadata["openclaw.actor_id"], "telegram:1001");
  assert.equal(metadata["openclaw.session_id"], IDENTITY.sessionId);
});

test("a cron turn's main request is tagged cron and its sub-agent subagent", async () => {
  const app = proxy({ identity: { ...IDENTITY, trigger: "cron" } });
  await app.chat(MAIN);
  await app.chat(SUB);
  assert.deepEqual(app.requests.map((r) => r.input.requestMetadata["openclaw.agent_type"]), ["cron", "subagent"]);
  assert.deepEqual(app.requests.map((r) => r.input.requestMetadata["openclaw.trigger"]), ["cron", "cron"]);
});

test("the model-fallback attempt carries the same attribution, marked fallback", async () => {
  const app = proxy({ identity: IDENTITY, fail: (_input, ordinal) => ordinal === 1 });
  assert.equal(await app.chat(MAIN), 200);
  assert.deepEqual(app.requests.map((r) => r.input.modelId), [policy.PRIMARY_MODEL_ID, policy.FALLBACK_MODEL_ID]);
  const [first, second] = app.requests.map((r) => r.input.requestMetadata);
  assert.equal(first["openclaw.model_route"], "primary");
  assert.equal(second["openclaw.model_route"], "fallback");
  assert.deepEqual({ ...second, "openclaw.model_route": "primary" }, first);
});

test("non-streaming Converse requests carry it too, on every retry", async () => {
  const app = proxy({ identity: IDENTITY, fail: (_input, ordinal) => ordinal === 1 });
  assert.equal(await app.chat({ ...MAIN, stream: false }), 200);
  assert.equal(app.requests.length, 2);
  for (const request of app.requests) {
    assert.equal(request.stream, false);
    assert.equal(request.input.requestMetadata["openclaw.actor_id"], "telegram:1001");
    assert.equal(request.input.requestMetadata["openclaw.agent_type"], "main");
  }
});

test("without an identity file the proxy refuses to serve (no fallback to environment identity)", async () => {
  // The identity file is a required per-turn signal from the contract server
  // for the bound actor. If it is absent, the resolver fails closed rather
  // than falling back to an environment-only identity derivation.
  const app = proxy({ identity: undefined });
  const status = await app.chat(MAIN);
  assert.equal(status, 502);
  assert.equal(app.requests.length, 0);
});

test("an unbound proxy refuses to serve requests and makes no Bedrock call", async () => {
  // Identity resolution is fail-closed: the proxy never consults request
  // data to derive the actor, and refuses to serve requests whose bound
  // actor is missing. There is no legitimate "default-user" / fallback path.
  const app = proxy({ identity: undefined, env: {}, bound: "" });
  const status = await app.chat(MAIN);
  assert.equal(status, 502);
  assert.equal(app.requests.length, 0);
});

test("message content never reaches requestMetadata", async () => {
  const secret = "my password is hunter2";
  const app = proxy({ identity: IDENTITY });
  await app.chat({ ...MAIN, messages: [{ role: "user", content: secret }], user: secret });
  const values = Object.values(app.requests[0].input.requestMetadata).join(" ");
  assert.equal(values.includes("hunter2"), false);
});

test("a malformed identity-file session is refused before any Bedrock request", async () => {
  const app = proxy({ identity: { ...IDENTITY, sessionId: "bad session\nid" } });
  assert.notEqual(await app.chat(MAIN), 200);
  assert.equal(app.requests.length, 0);
});

test("an identity-file channel or trigger outside the proxy allowlists is refused", async () => {
  for (const identity of [{ ...IDENTITY, channel: "email" }, { ...IDENTITY, trigger: "webhook" }]) {
    const app = proxy({ identity });
    assert.notEqual(await app.chat(MAIN), 200);
    assert.equal(app.requests.length, 0);
  }
});

test("the image ships request-metadata.js next to the proxy", () => {
  const dockerfile = fs.readFileSync(require.resolve("./Dockerfile"), "utf8");
  assert.match(dockerfile, /COPY request-metadata\.js \/app\/request-metadata\.js/);
  assert.ok(source.includes('require("./request-metadata")'));
});
