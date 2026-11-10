/**
 * The proxy logs once per container whether OpenClaw added its workspace files
 * (its "# Project Context" section) to the system prompt, so an operator can
 * check a route's configuration without reading prompts.
 *
 * The whole proxy module runs in a VM with fake S3, Cognito and Bedrock
 * clients; no network. Run: node --test proxy-project-context-log.test.js
 */
"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const util = require("node:util");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");

const LOG_PREFIX = "[proxy] OpenClaw workspace files in system prompt:";
const user = { role: "user", content: "hello" };
const WITH_FILES = [{ role: "system", content: "You are a personal assistant.\n\n# Project Context\n\n## USER.md\n- Prefers short replies.\n" }, user];
const WITHOUT_FILES = [{ role: "system", content: "You are a personal assistant.\n\n## Tooling\n- exec\n" }, user];
const NO_SYSTEM = [user];

function proxy() {
  let handler;
  const logs = [];
  class Command { constructor(input) { this.input = input; } }
  const sdk = {
    GetObjectCommand: Command, PutObjectCommand: Command,
    S3Client: class {
      async send() {
        return { Body: (async function* () { yield Buffer.from("fixture workspace"); })() };
      }
    },
    ConverseCommand: Command,
    BedrockRuntimeClient: class {
      async send() {
        return { output: { message: { content: [{ text: "answer" }] } }, usage: {} };
      }
    },
  };
  const process = new EventEmitter();
  process.env = { AWS_REGION: "us-west-2", S3_USER_FILES_BUCKET: "fixture-bucket", USER_ID: "slack:owner", CHANNEL: "slack" };
  process.connected = false;
  process.exit = () => assert.fail("Proxy must not exit");
  const tokens = {
    _cache: new Map(),
    async getIdToken() { return { token: "fixture-token", expiresAt: Date.now() + 60000 }; },
  };
  const mocks = {
    http: { createServer(callback) {
      handler = callback;
      return { listen(_port, _host, ready) { ready(); } };
    } },
    fs: { readFileSync(file) {
      assert.equal(file, "/tmp/current-identity.json");
      return JSON.stringify({ actorId: "slack:owner", channel: "slack" });
    } },
    "./cognito-token": { createCognitoTokenProvider: () => tokens },
    "./proxy-sdk-snapshot": { preparedSdk: () => null },
    "./read-body": { readBody: async (req) => JSON.stringify(req.body) },
    "@aws-sdk/client-s3": sdk,
    "@aws-sdk/client-bedrock-runtime": sdk,
  };
  const log = (...args) => logs.push(util.format(...args));
  vm.runInNewContext(fs.readFileSync(require.resolve("./agentcore-proxy"), "utf8"), {
    require: (name) => mocks[name] || require(name), process, Buffer, URL,
    console: { log, info: log, warn() {}, error() {}, debug() {} }, setTimeout,
  }, { filename: "agentcore-proxy.js" });
  return {
    lines: () => logs.filter((line) => line.startsWith(LOG_PREFIX)),
    request(messages) {
      const req = { method: "POST", url: "/v1/chat/completions", headers: {},
        body: { model: "bedrock-agentcore", messages } };
      return new Promise((resolve) => {
        const res = { writeHead(status) { res.status = status; res.headersSent = true; }, end() { resolve(res.status); } };
        handler(req, res);
      });
    },
  };
}

test("logs 'no' once, at the first request that carries OpenClaw's system prompt", async () => {
  const app = proxy();
  assert.equal(await app.request(NO_SYSTEM), 200);
  assert.deepEqual(app.lines(), [], "a request without a system message does not decide the line");
  assert.equal(await app.request(WITHOUT_FILES), 200);
  assert.deepEqual(app.lines(), [`${LOG_PREFIX} no`]);
  assert.equal(await app.request(WITH_FILES), 200);
  assert.deepEqual(app.lines(), [`${LOG_PREFIX} no`], "logged once per container");
});

test("logs 'yes' when OpenClaw's system prompt has its Project Context section", async () => {
  const app = proxy();
  assert.equal(await app.request(WITH_FILES), 200);
  assert.deepEqual(app.lines(), [`${LOG_PREFIX} yes`]);
});
