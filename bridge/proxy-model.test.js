const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const policy = require("./model-policy");
const source = fs.readFileSync(require.resolve("./agentcore-proxy"), "utf8");

test("each route obtains its ID from the OS entropy helper, including restored module state", () => {
  const policySource = fs.readFileSync(require.resolve("./model-policy"), "utf8");
  let calls = 0;
  const records = [];
  for (let restore = 0; restore < 2; restore++) {
    const box = { module: { exports: {} }, AbortController, process,
      require(name) {
        assert.equal(name, "./runtime-lifecycle");
        return { osRandomUUID: () => `00000000-0000-4000-8000-${String(++calls).padStart(12, "0")}` };
      },
    };
    vm.runInNewContext(policySource, box);
    assert.equal(calls, restore * 2, "Entropy must be read at route creation, not module import");
    for (let request = 0; request < 2; request++) {
      box.module.exports.createStreamingRoute({
        primaryModelId: policy.PRIMARY_MODEL_ID, response: new EventEmitter(),
        emit: (record) => records.push(record),
      }).finish();
    }
  }
  assert.equal(calls, 4);
  assert.equal(new Set(records.map((record) => record.route_id)).size, 4);
});

for (const bound of [true, false]) {
  test(`request session IDs use fresh OS entropy and retain per-user reuse: bound=${bound}`, () => {
    let calls = 0;
    const ids = [];
    const start = source.indexOf("function extractSessionMetadata(");
    const end = source.indexOf("// Per-user Cognito", start);
    const helperImport = 'const { osRandomUUID } = require("./runtime-lifecycle");';
    assert.ok(source.includes(helperImport));
    assert.ok(start > 0 && end > start);
    for (let restore = 0; restore < 2; restore++) {
      const box = {
        require(name) {
          assert.equal(name, "./runtime-lifecycle");
          return { osRandomUUID: () => `00000000-0000-4000-8000-${String(++calls).padStart(12, "0")}` };
        },
        crypto: { createHash: require("node:crypto").createHash,
          randomBytes() { assert.fail("Request IDs must not use the snapshot-restored DRBG"); } },
        Date: { now: () => 1234567890 },
        fs: { readFileSync: () => "{}" }, BOUND_ACTOR_ID: bound ? "slack:owner" : "",
        process: { env: bound ? { USER_ID: "slack:owner", CHANNEL: "slack" } : {} },
        sessionMap: new Map(), console: { log() {} },
      };
      vm.createContext(box);
      vm.runInContext(helperImport + source.slice(start, end), box);
      const headers = { "x-openclaw-actor-id": "slack:owner", "x-openclaw-channel": "slack" };
      const first = box.extractSessionMetadata({}, headers).sessionId;
      assert.equal(box.extractSessionMetadata({}, headers).sessionId, first);
      assert.equal(calls, restore + 1);
      assert.ok(first.includes(`00000000-0000-4000-8000-${String(calls).padStart(12, "0")}`));
      assert.ok(first.length >= 33);
      ids.push(first);
    }
    assert.notEqual(ids[0], ids[1]);
  });
}

function streaming(send, { subagent = false, subagentModel = "custom-subagent" } = {}) {
  const requests = [], clients = [], frames = [], logs = [];
  let handler, bodyReads = 0;
  const response = new EventEmitter();
  response.writeHead = (status) => { response.statusCode = status; response.headersSent = true; };
  response.write = (value) => { frames.push(value); return true; };
  response.end = (value) => { if (value) frames.push(value); response.writableEnded = true; };
  const box = {
    modelPolicy: { ...policy, createStreamingRoute: (options) =>
      policy.createStreamingRoute({ ...options, emit: (record) => logs.push(record) }) },
    preparedSdk: () => ({
      ConverseStreamCommand: class { constructor(input) { this.input = input; } },
      BedrockRuntimeClient: class {
        constructor(options) { clients.push(options); }
        async send(command, options) {
          requests.push({ input: command.input, options });
          return send(command.input, requests.length, response);
        }
      },
    }),
    MODEL_ID: policy.PRIMARY_MODEL_ID, FALLBACK_MODEL_ID: policy.FALLBACK_MODEL_ID,
    AWS_REGION: "us-west-2", MAX_RETRIES: 5, BASE_DELAY_MS: 0,
    sleep: async () => {}, bedrockClientOptions: () => ({}), structuredClone,
    convertMessages: () => ({
      bedrockMessages: [{ role: "user", content: [{ text: "benign request" }] }],
      systemText: "system",
    }),
    scopeGuardrailToLatestUserTurn: require("./guardrail-scope").scopeGuardrailToLatestUserTurn,
    guardrailConfig: { guardrailIdentifier: "test-guard", guardrailVersion: "1" },
    buildInferenceConfig: require("./inference-config").buildInferenceConfig,
    temperatureOverride: undefined,
    systemPrefixCache: { apply: (_prefix, system) => system },
    addToolPrefixCachePoint: require("./system-prefix-cache").addToolPrefixCachePoint,
    console: { log() {}, error() {}, warn() {}, debug() {} },
    modelOutcomeUnknown: false,
    fs: { readdirSync: () => [], existsSync: () => false },
    SUBAGENT_BEDROCK_MODEL_ID: subagentModel, SUBAGENT_MODEL_NAME: "bedrock-agentcore-subagent", COGNITO_USER_POOL_ID: "",
    chatRequestCount: 0, subagentRequestCount: 0,
    http: { createServer(callback) { handler = callback; return {}; } },
    readBody() { bodyReads++; throw new Error("Fenced requests must not enter body or SDK processing"); },
  };
  vm.createContext(box);
  const start = source.indexOf("async function invokeBedrockStreaming(");
  const end = source.indexOf("\n/**\n * Format a response", start);
  assert.ok(start > 0 && end > start);
  const routingStart = source.indexOf("function isSubagentRequest(");
  const routingEnd = source.indexOf("\n/**\n * Call Bedrock Converse API", routingStart);
  assert.ok(routingStart > 0 && routingEnd > routingStart);
  vm.runInContext(source.slice(routingStart, routingEnd), box);
  vm.runInContext(source.slice(start, end), box, { filename: "agentcore-proxy.js" });
  vm.runInContext(source.slice(source.indexOf("const server = http.createServer("),
    source.indexOf("\nserver.listen(")), box);
  const healthStart = source.indexOf('  if (req.method === "GET" && req.url === "/health")');
  const healthEnd = source.indexOf("  // Chat completions endpoint", healthStart);
  assert.ok(healthStart > 0 && healthEnd > healthStart);
  vm.runInContext(`function health(req, res) { ${source.slice(healthStart, healthEnd)} }`, box);
  return {
    requests, clients, frames, logs, response,
    get unknownOutcome() { return box.modelOutcomeUnknown; },
    get bodyReads() { return bodyReads; },
    request: async (body) => {
      let status, result;
      await handler({ method: "POST", url: "/v1/chat/completions", body }, {
        writeHead(value) { status = value; },
        end(value) { result = JSON.parse(value); },
      });
      return { status, body: result };
    },
    health: () => {
      let body;
      box.health({ method: "GET", url: "/health" }, { writeHead() {}, end: (value) => { body = value; } });
      return JSON.parse(body);
    },
    invoke: (nextResponse = response) => box.invokeBedrockStreaming([], nextResponse, subagent ? "bedrock-agentcore-subagent" : "bedrock-agentcore",
      "system", { tools: [{ toolSpec: { name: "test", inputSchema: { json: { type: "object" } } } }] }),
  };
}

function reply(events = [
  { contentBlockDelta: { delta: { text: "answer" } } },
  { messageStop: { stopReason: "end_turn" } },
]) {
  return { $metadata: { requestId: "sdk-request-1", attempts: 1 },
    stream: (async function* () { yield* events; })() };
}

test("the actual proxy sends Haiku with disabled thinking, guardrails and one SDK attempt", async () => {
  const app = streaming(() => reply());
  assert.equal(await app.invoke(), "answer");
  assert.equal(app.requests.length, 1);
  const params = app.requests[0].input;
  assert.equal(params.modelId, policy.PRIMARY_MODEL_ID);
  assert.equal(params.additionalModelRequestFields.thinking.type, "disabled");
  assert.equal(Object.hasOwn(params.inferenceConfig, "temperature"), false);
  assert.equal(params.guardrailConfig.guardrailIdentifier, "test-guard");
  assert.ok(params.messages[0].content.some((block) => block.guardContent));
  assert.equal(params.toolConfig.tools.at(-1).cachePoint.type, "default");
  assert.equal(app.clients[0].maxAttempts, 1);
  assert.equal(app.logs[0].attempts[0].sdk_request_id, "sdk-request-1");
  assert.equal(app.logs[0].attempts[0].sdk_attempts, 1);
  assert.equal(app.logs[0].attempts[0].status, "completed");
  assert.match(app.frames[0], /answer/);
});

test("a temporary pre-output failure selects Sonnet once without changing guardrail inputs", async () => {
  const app = streaming((_input, ordinal) => {
    if (ordinal === 1) throw Object.assign(new Error("service unavailable"), {
      name: "ServiceUnavailableException", $metadata: { httpStatusCode: 503, attempts: 1, requestId: "failed-1" },
    });
    return reply();
  });
  assert.equal(await app.invoke(), "answer");
  assert.deepEqual(app.requests.map((entry) => entry.input.modelId),
    [policy.PRIMARY_MODEL_ID, policy.FALLBACK_MODEL_ID]);
  const [first, second] = app.requests.map((entry) => entry.input);
  for (const key of ["messages", "system", "toolConfig", "guardrailConfig"]) {
    assert.deepEqual(second[key], first[key]);
  }
  assert.equal(Object.hasOwn(second, "additionalModelRequestFields"), false);
  assert.equal(app.logs[0].fallback_selected, true);
  assert.equal(app.logs[0].attempts[0].sdk_request_id, "failed-1");
});

for (const firstEvent of [
  { contentBlockDelta: { delta: { text: "" } } },
  { contentBlockDelta: { delta: { reasoningContent: { text: "private" } } } },
  { contentBlockStart: { start: { toolUse: { toolUseId: "id", name: "test" } } } },
  { messageStop: { stopReason: "guardrail_intervened" } },
]) {
  test(`no fallback after output or a terminal event: ${Object.keys(firstEvent)[0]}`, async () => {
    const app = streaming(() => reply([
      firstEvent, { serviceUnavailableException: { message: "not logged" } },
    ]));
    await app.invoke();
    assert.equal(app.requests.length, 1);
    assert.equal(app.logs[0].fallback_selected, false);
    assert.equal(app.logs[0].attempts[0].status, "failed");
    assert.equal(JSON.stringify(app.logs).includes("private"), false);
  });
}

for (const [name, status, attempts] of [
  ["AccessDeniedException", 403, 1], ["ValidationException", 400, 1],
  ["ServiceUnavailableException", 503, 2], ["OtherError", 503, 1],
]) {
  test(`the actual route retains an ineligible failure: ${name}/${attempts}`, async () => {
    const app = streaming(() => { throw Object.assign(new Error("private error body"), {
      name, $metadata: { httpStatusCode: status, attempts },
    }); });
    await app.invoke();
    assert.equal(app.requests.length, 1);
    assert.equal(app.logs[0].fallback_selected, false);
    assert.equal(app.logs[0].attempts[0].sdk_request_id, null);
    assert.equal(JSON.stringify(app.logs).includes("private"), false);
  });
}

test("a disconnected response cannot authorize another send", async () => {
  const app = streaming((_input, _ordinal, response) => {
    response.destroyed = true;
    response.emit("close");
    throw Object.assign(new Error("closed"), { name: "ServiceUnavailableException",
      $metadata: { httpStatusCode: 503 } });
  });
  await app.invoke();
  assert.equal(app.requests.length, 1);
  assert.equal(app.requests[0].options.abortSignal.aborted, true);
});

test("subagent override keeps its configured model with one SDK attempt and no fallback", async () => {
  const app = streaming(() => reply(), { subagent: true });
  assert.equal(await app.invoke(), "answer");
  assert.equal(app.requests[0].input.modelId, "custom-subagent");
  assert.equal(Object.hasOwn(app.requests[0].input, "additionalModelRequestFields"), false);
  assert.equal(app.clients[0].maxAttempts, 1);
  assert.equal(app.requests.length, 1);
  assert.equal(app.logs[0].fallback_selected, false);
});

test("the default subagent model also uses one SDK attempt", async () => {
  const app = streaming(() => reply(), { subagent: true, subagentModel: policy.PRIMARY_MODEL_ID });
  await app.invoke();
  assert.equal(app.requests[0].input.modelId, policy.PRIMARY_MODEL_ID);
  assert.equal(app.clients[0].maxAttempts, 1);
  assert.equal(app.requests.length, 1);
});

test("subagent ambiguous transport failure is terminal, latches unknown and never retries", async () => {
  const app = streaming(() => { throw new Error("timeout"); }, { subagent: true });
  await app.invoke();
  assert.equal(app.requests.length, 1);
  assert.equal(app.clients[0].maxAttempts, 1);
  assert.equal(app.logs[0].fallback_selected, false);
  assert.equal(app.response.statusCode, 400);
  assert.equal(JSON.parse(app.frames[0]).error.code, "PROVIDER_POST_DISPATCH_AMBIGUITY");
  assert.equal(app.health().model_outcome_unknown, true);
});

test("subagent explicit pre-content rejection cannot select Sonnet or retry locally", async () => {
  const app = streaming(() => { throw Object.assign(new Error("unavailable"), {
    name: "ServiceUnavailableException", $metadata: { httpStatusCode: 503, attempts: 1 },
  }); }, { subagent: true });
  await app.invoke();
  assert.equal(app.requests.length, 1);
  assert.equal(app.requests[0].input.modelId, "custom-subagent");
  assert.equal(app.clients[0].maxAttempts, 1);
  assert.equal(app.logs[0].fallback_selected, false);
  assert.equal(app.health().model_outcome_unknown, false);
});

for (const name of ["TimeoutError", "Error", "AbortError"]) {
  test(`ambiguous ${name} is terminal and not exposed as a retryable HTTP failure`, async () => {
    const app = streaming(() => { throw Object.assign(new Error("socket timeout 502 private"), { name }); });
    await app.invoke();
    assert.equal(app.requests.length, 1);
    assert.equal(app.logs[0].fallback_selected, false);
    assert.equal(app.response.statusCode, 400);
    const { error } = JSON.parse(app.frames[0]);
    assert.equal(error.code, "PROVIDER_POST_DISPATCH_AMBIGUITY");
    assert.doesNotMatch(error.message, /timeout|502|private/);
    assert.equal(app.unknownOutcome, true);
  });
}

test("a closed response still reports an unknown outcome to the contract", async () => {
  const app = streaming((_input, _ordinal, response) => {
    response.destroyed = true;
    response.emit("close");
    throw new Error("connection lost");
  });
  await app.invoke();
  assert.equal(app.requests.length, 1);
  assert.equal(app.frames.length, 0);
  assert.equal(app.unknownOutcome, true);
  assert.equal(app.health().model_outcome_unknown, true);
});

test("unknown outcome fences fresh streaming requests before another SDK client or route", async () => {
  const app = streaming((_input, ordinal) => {
    if (ordinal === 1) throw new Error("connection lost");
    return reply();
  });
  assert.equal(app.health().model_outcome_unknown, false);
  await app.invoke();
  assert.equal(app.health().model_outcome_unknown, true);
  const fresh = new EventEmitter();
  let status, body;
  fresh.writeHead = (value) => { status = value; };
  fresh.end = (value) => { body = value; fresh.writableEnded = true; };
  await app.invoke(fresh);
  assert.equal(status, 400);
  assert.equal(JSON.parse(body).error.code, "PROVIDER_POST_DISPATCH_AMBIGUITY");
  assert.equal(app.requests.length, 1);
  assert.equal(app.clients.length, 1);
  assert.equal(app.logs.length, 1);
  assert.equal(app.logs[0].fallback_selected, false);
  assert.equal(app.health().model_outcome_unknown, true);
});

for (const subagent of [false, true]) for (const events of [
  [{ contentBlockDelta: { delta: { text: "partial answer" } } }],
  [
    { contentBlockStart: { contentBlockIndex: 0, start: { toolUse: { toolUseId: "id", name: "test" } } } },
    { contentBlockDelta: { delta: { toolUse: { input: "{}" } } } },
    { contentBlockStop: { contentBlockIndex: 0 } },
  ],
]) {
  test(`partial ${events[0].contentBlockStart ? "tool" : "text"} output ends with terminal SSE error: subagent=${subagent}`, async () => {
    const app = streaming(() => reply([...events, { serviceUnavailableException: {} }]), { subagent });
    await app.invoke();
    assert.equal(app.requests.length, 1);
    assert.equal(app.clients[0].maxAttempts, 1);
    assert.equal(app.logs[0].fallback_selected, false);
    assert.equal(app.response.statusCode, 200);
    const frame = app.frames.at(-1);
    assert.equal(JSON.parse(frame.slice(6)).error.code, "PROVIDER_FAILURE_WITH_OUTPUT");
    assert.doesNotMatch(app.frames.join(""), /\[DONE\]|"finish_reason":"tool_calls"/);
    assert.equal(app.unknownOutcome, true);
  });
}

for (const subagent of [false, true]) for (const [label, events] of [
  ["empty", []],
  ["text", [{ contentBlockDelta: { delta: { text: "partial" } } }]],
  ["tool", [
    { contentBlockStart: { contentBlockIndex: 0, start: { toolUse: { toolUseId: "id", name: "test" } } } },
    { contentBlockDelta: { delta: { toolUse: { input: "{}" } } } },
    { contentBlockStop: { contentBlockIndex: 0 } },
  ]],
]) {
  test(`EOF requires messageStop: ${label}, subagent=${subagent}`, async () => {
    const app = streaming(() => reply(events), { subagent });
    await app.invoke();
    assert.equal(app.requests.length, 1);
    assert.equal(app.clients[0].maxAttempts, 1);
    assert.equal(app.logs[0].fallback_selected, false);
    assert.equal(app.logs[0].attempts[0].status, "failed");
    assert.equal(app.health().model_outcome_unknown, true);
    assert.equal(JSON.parse(app.frames.at(-1).slice(6)).error.code,
      events.length ? "PROVIDER_FAILURE_WITH_OUTPUT" : "PROVIDER_POST_DISPATCH_AMBIGUITY");
    assert.doesNotMatch(app.frames.join(""), /\[DONE\]|"finish_reason":"(?:stop|tool_calls)"/);

    const completed = streaming(() => reply([...events, { messageStop: { stopReason: "end_turn" } }]), { subagent });
    await completed.invoke();
    assert.equal(completed.requests.length, 1);
    assert.equal(completed.health().model_outcome_unknown, false);
    assert.equal(completed.frames.at(-1), "data: [DONE]\n\n");
  });
}

test("fallback EOF without messageStop also latches unknown", async () => {
  const app = streaming((_input, ordinal) => {
    if (ordinal === 1) throw Object.assign(new Error("unavailable"), {
      name: "ServiceUnavailableException", $metadata: { httpStatusCode: 503, attempts: 1 },
    });
    return reply([]);
  });
  await app.invoke();
  assert.equal(app.requests.length, 2);
  assert.equal(app.requests[1].input.modelId, policy.FALLBACK_MODEL_ID);
  assert.equal(app.logs[0].fallback_selected, true);
  assert.equal(app.health().model_outcome_unknown, true);
  assert.doesNotMatch(app.frames.join(""), /\[DONE\]|"finish_reason":"stop"/);
});

for (const subagent of [false, true]) for (const stream of [true, false, undefined]) {
  test(`common admission fences latched requests before body/SDK work: stream=${stream}, subagent=${subagent}`, async () => {
    const app = streaming(() => { throw new Error("timeout"); }, { subagent });
    await app.invoke();
    const result = await app.request({
      model: subagent ? "bedrock-agentcore-subagent" : "bedrock-agentcore",
      messages: [{ role: "user", content: "hello" }], ...(stream === undefined ? {} : { stream }),
    });
    assert.equal(result.status, 400);
    assert.equal(result.body.error.code, "PROVIDER_POST_DISPATCH_AMBIGUITY");
    assert.equal(app.bodyReads, 0);
    assert.equal(app.requests.length, 1);
    assert.equal(app.clients.length, 1);
    assert.equal(app.logs.length, 1);
  });
}

test("pinned OpenClaw projects HTTP/SSE failures into terminal assistant errors without replay", {
  skip: !process.env.OPENCLAW_SOURCE_ROOT && "Set OPENCLAW_SOURCE_ROOT to the pinned 2026.9.5 source fixture",
}, async () => {
  const path = require("node:path");
  const root = process.env.OPENCLAW_SOURCE_ROOT;
  assert.equal(JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8")).version, "2026.9.5");
  const worker = fs.readFileSync(path.join(root, "dist/worker/worker.mjs"), "utf8");
  const retry = fs.readFileSync(path.join(root, "dist/retry-C-xDi4CZ.mjs"), "utf8");
  const between = (text, start, end) => {
    const a = text.indexOf(start), b = text.indexOf(end, a + start.length);
    assert.ok(a >= 0 && b > a, `Pinned source boundaries: ${start}`);
    return text.slice(a, b);
  };
  const box = {
    OpenAIError: Error,
    asOptionalRecord: (value) => value && typeof value === "object" ? value : undefined,
    normalizeStringifiedOptionalString: (value) => String(value).trim() || undefined,
    truncateUtf16Safe: (value, length) => value.slice(0, length),
    redactDiagnosticText: (value) => value,
    stableStringify: JSON.stringify,
    MAX_ERROR_BODY_LENGTH: 4000,
    PROVIDER_FAILURE_WITH_OUTPUT_ERROR_CODE: "PROVIDER_FAILURE_WITH_OUTPUT",
    PROVIDER_POST_DISPATCH_AMBIGUITY_ERROR_CODE: "PROVIDER_POST_DISPATCH_AMBIGUITY",
    WEBSOCKET_NON_RETRYABLE_CLOSE_ERROR_CODE: "ERR_WEBSOCKET_NON_RETRYABLE_CLOSE",
    console,
    _iterSSEMessages$1: async function* (response) {
      for (const frame of response.frames) yield { event: null, data: frame.slice(6).trim() };
    },
    Stream$11: class {
      constructor(iterator) { this.iterator = iterator; }
      [Symbol.asyncIterator]() { return this.iterator(); }
    },
    isTransportAbortError: () => false,
  };
  vm.createContext(box);
  vm.runInContext(
    `const APIError = ${between(worker, "class APIError$2 extends OpenAIError", ",APIUserAbortError$2=")}; const APIError$2 = APIError;\n` +
    between(worker, "function readProviderErrorField(", "function projectProviderError(") + "\n" +
    between(retry, "function isProviderRefusalAssistantError(", "//#endregion") + "\n" +
    between(retry, "const RETRYABLE_HTTP_STATUS_CODES", "const RATE_LIMIT_RETRY_CONTEXT_RE") + "\n" +
    between(retry, "function resolveRetrySignalStatus(", "function hasRateLimitRetryContext(") + "\n" +
    between(retry, "const TERMINAL_ASSISTANT_ERROR_CODES", "//#endregion") + "\n" +
    "this.decode = (status, body) => buildProjection(new APIError(status, body.error));" +
    "this.terminal = isTerminalAssistantError; this.retryable = isRetryableAssistantError;" +
    "this.transient = hasTransientRetryEvidence;" +
    `const wi = new Set(); let ${between(worker, "enqueueTools=Ot=>{", "let _n=wi.size>0;")}return Zt;}; this.pendingTools = enqueueTools;` +
    `this.sse = class { ${between(worker, "static fromSSEResponse(", "static fromReadableStream(")} };`,
    box);
  assert.ok(worker.includes("if(_n&&_n.error)throw new APIError$2(void 0,_n.error,void 0,Ot.headers)"));
  assert.ok(worker.includes("maxRetries:0"));
  assert.equal(box.terminal(box.decode(502, { error: { type: "proxy_error", message: "timeout" } })), false);
  assert.equal(box.transient({ status: 502 }), true);
  const tool = { type: "toolCall", id: "id", name: "test", arguments: {} };
  assert.equal(box.pendingTools({ stopReason: "toolUse", content: [tool] }).length, 1);

  for (const subagent of [false, true]) for (const eof of [false, true]) for (const events of [null, [], [
    { contentBlockDelta: { delta: { text: "partial" } } },
  ], [
    { contentBlockStart: { contentBlockIndex: 0, start: { toolUse: { toolUseId: "id", name: "test" } } } },
    { contentBlockDelta: { delta: { toolUse: { input: "{}" } } } },
    { contentBlockStop: { contentBlockIndex: 0 } },
  ]]) {
    let sends = 0, outerRequests = 0, fallback = 0;
    for (;;) {
      const app = streaming(() => {
        sends++;
        if (events === null) throw new Error("timeout");
        return reply(eof ? events : [...events, { modelStreamErrorException: {} }]);
      }, { subagent });
      await app.invoke();
      outerRequests++;
      fallback += Number(app.logs[0].fallback_selected);
      const frame = app.frames.at(-1);
      let assistant;
      if (app.response.statusCode === 400) assistant = box.decode(400, JSON.parse(frame));
      else {
        let streamError;
        try {
          for await (const _chunk of box.sse.fromSSEResponse(
            { frames: app.frames }, new AbortController())) {}
        } catch (error) { streamError = error; }
        assert.ok(streamError, "The actual SDK SSE decoder must throw before normal completion");
        box.streamError = streamError;
        assistant = vm.runInContext("buildProjection(streamError)", box);
      }
      assert.equal(box.terminal(assistant), true);
      assert.equal(box.pendingTools({ ...assistant, content: [tool] }), undefined);
      if (!box.retryable(assistant)) break;
      assert.ok(outerRequests < 3, "Outer retry replayed the model request");
    }
    assert.equal(sends, 1);
    assert.equal(outerRequests, 1);
    assert.equal(fallback, 0);
  }
});
