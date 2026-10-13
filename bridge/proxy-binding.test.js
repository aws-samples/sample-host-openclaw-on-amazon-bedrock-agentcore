const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const { createProxyAuthChild } = require("./proxy-auth-prepare");

function proxy(actor = "slack:owner") {
  let handler, identity = actor ? { actorId: actor, channel: "slack" } : null;
  const cognito = [], s3 = [], model = [];
  const tokens = {
    _cache: new Map(),
    async getIdToken(value) {
      cognito.push(value);
      return { token: "fixture-token", expiresAt: Date.now() + 60000 };
    },
  };
  class Command { constructor(input) { this.input = input; } }
  const sdk = {
    GetObjectCommand: Command, PutObjectCommand: Command,
    S3Client: class {
      async send(command) {
        s3.push(command.input);
        return { Body: (async function* () { yield Buffer.from("fixture workspace"); })() };
      }
    },
    ConverseCommand: Command,
    BedrockRuntimeClient: class {
      async send(command) {
        model.push(command.input);
        return { output: { message: { content: [{ text: "answer" }] } }, usage: {} };
      }
    },
  };
  const process = new EventEmitter();
  process.env = {
    AWS_REGION: "us-west-2", S3_USER_FILES_BUCKET: "fixture-bucket",
    ...(actor ? { USER_ID: actor, CHANNEL: "slack" } : {}),
  };
  process.connected = false;
  process.exit = () => assert.fail("Proxy must not exit");
  const mocks = {
    http: { createServer(callback) {
      handler = callback;
      return { listen(_port, _host, callback) { callback(); } };
    } },
    fs: { readFileSync(file) {
      assert.equal(file, "/tmp/current-identity.json");
      if (!identity) throw Object.assign(new Error("absent"), { code: "ENOENT" });
      return JSON.stringify(identity);
    } },
    "./cognito-token": { createCognitoTokenProvider: () => tokens },
    "./proxy-sdk-snapshot": { preparedSdk: () => null },
    "./read-body": { readBody: async (req) => JSON.stringify(req.body) },
    "@aws-sdk/client-s3": sdk,
    "@aws-sdk/client-bedrock-runtime": sdk,
  };
  vm.runInNewContext(fs.readFileSync(require.resolve("./agentcore-proxy"), "utf8"), {
    require: (name) => mocks[name] || require(name), process, Buffer, URL,
    console: { log() {}, warn() {}, error() {} }, setTimeout,
  }, { filename: "agentcore-proxy.js" });
  return {
    cognito, s3, model, process,
    identity(value) { identity = value; },
    request(headers = {}) {
      const req = { method: "POST", url: "/v1/chat/completions", headers,
        body: { model: "bedrock-agentcore", messages: [{ role: "user", content: "hello" }] } };
      return new Promise((resolve) => {
        const res = { writeHead(status) { res.status = status; res.headersSent = true; },
          end(body) { resolve({ status: res.status, body: JSON.parse(body) }); } };
        handler(req, res);
      });
    },
  };
}

test("mutating the identity file within a bound runtime cannot start Cognito, S3 or model work", async () => {
  const app = proxy();
  assert.equal((await app.request()).status, 200);
  app.cognito.length = app.s3.length = app.model.length = 0;
  app.identity({ actorId: "telegram:foreign", channel: "telegram" });
  const response = await app.request();
  assert.equal(response.status, 502);
  assert.match(response.body.error.message, /canonical actor changed/);
  assert.deepEqual(app.cognito, []);
  assert.deepEqual(app.s3, []);
  assert.deepEqual(app.model, []);
});

test("linked-channel delivery changes retain the canonical actor and namespace", async () => {
  const app = proxy();
  app.identity({ actorId: "slack:owner", channel: "telegram",
    channelActorId: "telegram:linked", channelTarget: "delivery-target" });
  assert.equal((await app.request({ "x-openclaw-actor-id": "discord:foreign" })).status, 200);
  assert.deepEqual(app.cognito, ["slack:owner"]);
  assert.ok(app.s3.length > 0);
  assert.ok(app.s3.every((request) => request.Key.startsWith("slack_owner/")));
  assert.equal(app.model.length, 1);
});

test("missing identity metadata uses the fixed actor; a later environment rewrite cannot rebind it", async () => {
  const app = proxy();
  app.identity(null);
  assert.equal((await app.request()).status, 200);
  app.cognito.length = app.s3.length = app.model.length = 0;
  app.process.env.USER_ID = "slack:foreign";
  app.identity({ actorId: "slack:foreign", channel: "slack" });
  assert.equal((await app.request()).status, 502);
  assert.deepEqual(app.cognito, []);
  assert.deepEqual(app.s3, []);
  assert.deepEqual(app.model, []);
});

test("an unbound legacy proxy retains the original identity and Cognito path", async () => {
  const app = proxy(null);
  assert.equal((await app.request({ "x-openclaw-actor-id": "telegram:legacy" })).status, 200);
  assert.deepEqual(app.cognito, ["telegram:legacy"]);
  assert.ok(app.s3.every((request) => request.Key.startsWith("telegram_legacy/")));
});

test("a neutral auth factory cannot adopt its first caller, and a bound factory rejects foreign actors", async () => {
  const channel = new EventEmitter(), calls = [];
  const tokenProvider = { _cache: new Map(), getIdToken: async (actor) => { calls.push(actor); return null; } };
  const neutral = createProxyAuthChild({ channel, tokenProvider, boundActor: undefined });
  await assert.rejects(neutral.getIdToken("slack:owner"), /canonical actor mismatch/);
  const bound = createProxyAuthChild({ channel, tokenProvider, boundActor: "slack:owner" });
  await assert.rejects(bound.getIdToken("slack:foreign"), /canonical actor mismatch/);
  assert.deepEqual(calls, []);
  assert.equal(await bound.getIdToken("slack:owner"), null);
  assert.deepEqual(calls, ["slack:owner"]);
  neutral.stop();
  bound.stop();
});

test("foreign callers cannot join or replace a bound auth warmup", async () => {
  const channel = new EventEmitter(), calls = [];
  let release;
  channel.connected = true;
  channel.send = (_message, callback) => callback();
  const tokenProvider = { _cache: new Map(), getIdToken(actor) {
    calls.push(actor);
    return new Promise((resolve) => { release = resolve; });
  } };
  const auth = createProxyAuthChild({ channel, tokenProvider, boundActor: "slack:owner" });
  channel.emit("message", { type: "openclaw.proxy-auth-prepare.v1",
    generation: "efab2af8-49c0-4d10-8b2a-01a8e15ae53e" });
  await new Promise(setImmediate);
  await assert.rejects(auth.getIdToken("slack:foreign"), /canonical actor mismatch/);
  assert.deepEqual(calls, ["slack:owner"]);
  release({ token: "fixture-token", expiresAt: Date.now() + 60000 });
  await new Promise(setImmediate);
  auth.stop();
});
