/**
 * Regression: container log lines never carry the raw channel actor, the
 * platform user ID (a WhatsApp ID is the user's phone number) or the
 * namespace derived from it. The bridge ships this output to the
 * /openclaw/container log group (cloudwatch-logger.js).
 *
 * The proxy runs end to end in a VM with fake S3, Cognito, Secrets Manager and
 * Bedrock clients, and everything it writes to the console is captured. The
 * real cognito-token.js and workspace-files-s3.js run inside it. Contract-side
 * lines are covered in runtime-contract.test.js, workspace restore lines in
 * workspace-restore-parallel.test.js.
 *
 * All IDs are synthetic. Run: node --test container-log-privacy.test.js
 */
"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const util = require("node:util");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const { createCognitoTokenProvider } = require("./cognito-token");
const policy = require("./model-policy");

const USER = "user_0123456789abcdef";
const POOL = "us-west-2_EXAMPLE";
const OTHER_NS = "telegram_445566001";
const ACTORS = [
  { label: "WhatsApp", actorId: "whatsapp:15551234567", platformId: "15551234567" },
  { label: "Telegram", actorId: "telegram:778899001", platformId: "778899001" },
];
const namespaceOf = (actorId) => actorId.replace(/:/g, "_");

function assertClean(output, actor, extra = []) {
  for (const value of [actor.actorId, actor.platformId, namespaceOf(actor.actorId), ...extra]) {
    assert.equal(output.includes(value), false, `container log contains ${value}:\n${output}`);
  }
}

function commandClass(name) {
  const C = class { constructor(input) { this.input = input; } };
  Object.defineProperty(C, "name", { value: name });
  return C;
}
const cognitoCommands = Object.fromEntries(["CognitoIdentityProviderClient", "AdminGetUserCommand",
  "AdminCreateUserCommand", "AdminSetUserPasswordCommand", "AdminInitiateAuthCommand"]
  .map((name) => [name, commandClass(name)]));
const secretsCommands = Object.fromEntries(["SecretsManagerClient", "GetSecretValueCommand", "CreateSecretCommand"]
  .map((name) => [name, commandClass(name)]));

const awsError = (name, message = name) => Object.assign(new Error(message), { name });

/** Secrets Manager and Cognito fakes that behave like the services for these calls. */
function fakeIdentityServices({ users = {}, secrets = {} } = {}) {
  const passwords = new Map(Object.entries(users));
  const store = new Map(Object.entries(secrets));
  let tokens = 0;
  const cognito = {
    async send(cmd) {
      const name = cmd.constructor.name;
      const user = cmd.input.Username || cmd.input.AuthParameters?.USERNAME;
      if (name === "AdminGetUserCommand") {
        if (!passwords.has(user)) throw awsError("UserNotFoundException", "User does not exist.");
        return {};
      }
      if (name === "AdminCreateUserCommand") {
        passwords.set(user, cmd.input.TemporaryPassword);
        return {};
      }
      if (name === "AdminSetUserPasswordCommand") {
        passwords.set(user, cmd.input.Password);
        return {};
      }
      if (name === "AdminInitiateAuthCommand") {
        if (passwords.get(user) !== cmd.input.AuthParameters.PASSWORD) {
          throw awsError("NotAuthorizedException", "Incorrect username or password.");
        }
        tokens += 1;
        return { AuthenticationResult: { IdToken: `id-${tokens}`, AccessToken: `access-${tokens}`, ExpiresIn: 3600 } };
      }
      throw new Error(`unexpected Cognito call ${name}`);
    },
  };
  const secretsManager = {
    async send(cmd) {
      const name = cmd.constructor.name;
      if (name === "GetSecretValueCommand") {
        if (!store.has(cmd.input.SecretId)) throw awsError("ResourceNotFoundException");
        return { SecretString: store.get(cmd.input.SecretId) };
      }
      if (name === "CreateSecretCommand") {
        if (store.has(cmd.input.Name)) throw awsError("ResourceExistsException");
        store.set(cmd.input.Name, cmd.input.SecretString);
        return {};
      }
      throw new Error(`unexpected Secrets Manager call ${name}`);
    },
  };
  return { cognito, secretsManager };
}

/** The whole proxy module in a VM, with every AWS client faked and console captured. */
function loadProxy(actor, { onExit, services = fakeIdentityServices(), output = [] } = {}) {
  const namespace = namespaceOf(actor.actorId);
  const channel = actor.actorId.split(":")[0];
  const capture = (level) => (...args) => output.push(`[${level}] ${util.format(...args)}`);
  const objects = new Map([
    [`_system/${namespace}/AGENTS.md`, "# Operating Instructions\n- Be concise.\n"],
    // Only at the legacy key, so the first read copies it to _system/.
    [`${namespace}/SOUL.md`, "# Agent Persona\nFriendly.\n"],
    [`${namespace}/_uploads/img_1.png`, "png-bytes"],
  ]);
  const model = [];
  let handler, identity = { actorId: actor.actorId, userId: USER, channel, trigger: "chat",
    sessionId: "ses_0123456789abcdef0123456789abcdef" };
  const S3 = { GetObjectCommand: commandClass("GetObjectCommand"), PutObjectCommand: commandClass("PutObjectCommand") };
  S3.S3Client = class {
    async send(cmd) {
      const key = cmd.input.Key;
      if (cmd instanceof S3.GetObjectCommand) {
        if (!objects.has(key)) throw awsError("NoSuchKey", "The specified key does not exist.");
        const body = objects.get(key);
        return { ContentType: key.endsWith(".png") ? "image/png" : "text/markdown",
          Body: (async function* () { yield Buffer.from(body); })() };
      }
      if (cmd.input.IfNoneMatch === "*" && objects.has(key)) throw awsError("PreconditionFailed");
      objects.set(key, String(cmd.input.Body));
      return {};
    }
  };
  const Bedrock = { ConverseCommand: commandClass("ConverseCommand"),
    ConverseStreamCommand: commandClass("ConverseStreamCommand") };
  Bedrock.BedrockRuntimeClient = class {
    async send(cmd) {
      model.push(cmd.input);
      if (cmd instanceof Bedrock.ConverseStreamCommand) {
        return { $metadata: { requestId: "r", attempts: 1 }, stream: (async function* () {
          yield { contentBlockDelta: { delta: { text: "answer" } } };
          yield { messageStop: { stopReason: "end_turn" } };
          yield { metadata: { usage: { inputTokens: 10, outputTokens: 2 } } };
        })() };
      }
      return { output: { message: { content: [{ text: "answer" }] } },
        usage: { inputTokens: 10, outputTokens: 2 } };
    }
  };
  const proc = new EventEmitter();
  proc.env = {
    AWS_REGION: "us-west-2", S3_USER_FILES_BUCKET: "fixture-bucket",
    COGNITO_USER_POOL_ID: POOL, COGNITO_CLIENT_ID: "fixture-client",
    USER_ID: actor.actorId, INTERNAL_USER_ID: USER, CHANNEL: channel,
  };
  proc.connected = false;
  proc.exit = onExit || ((code) => assert.fail(`proxy exited with ${code}`));
  proc.hrtime = process.hrtime;
  const mocks = {
    http: { createServer(callback) {
      handler = callback;
      return { listen(_port, _host, ready) { ready(); } };
    } },
    fs: { ...fs, readFileSync(file, ...rest) {
      if (file === "/tmp/current-identity.json") return JSON.stringify(identity);
      return fs.readFileSync(file, ...rest);
    } },
    "./cognito-token": { createCognitoTokenProvider: (config) => createCognitoTokenProvider({
      ...config, client: services.cognito, commands: cognitoCommands,
      secretsClient: services.secretsManager, secretsCommands }) },
    "./proxy-sdk-snapshot": { preparedSdk: () => null },
    "./read-body": { readBody: async (req) => JSON.stringify(req.body) },
    // Route records (model-policy.js) are proxy output too.
    "./model-policy": { ...policy, createStreamingRoute: (options) => policy.createStreamingRoute({
      ...options, emit: (record) => output.push(`[INFO] ${JSON.stringify(record)}`) }) },
    "@aws-sdk/client-s3": S3,
    "@aws-sdk/client-bedrock-runtime": Bedrock,
  };
  vm.runInNewContext(fs.readFileSync(require.resolve("./agentcore-proxy"), "utf8"), {
    require: (name) => mocks[name] || require(name), process: proc, Buffer, URL,
    console: { log: capture("INFO"), info: capture("INFO"), warn: capture("WARN"), error: capture("ERROR"),
      debug: capture("DEBUG") },
    setTimeout, clearTimeout, setInterval, clearInterval, setImmediate, AbortController, AbortSignal,
    structuredClone, TextEncoder, TextDecoder,
  }, { filename: "agentcore-proxy.js" });
  return {
    output, model, objects,
    setIdentity(value) { identity = value; },
    async chat(body) {
      const req = { method: "POST", url: "/v1/chat/completions", headers: {}, body };
      const res = new EventEmitter();
      const done = new Promise((resolve) => {
        res.writeHead = (status) => { res.status = status; res.headersSent = true; };
        res.flushHeaders = () => {};
        res.write = () => true;
        res.end = () => { res.writableEnded = true; resolve(res.status); };
      });
      handler(req, res);
      const status = await done;
      // Let the fire-and-forget workspace seeding finish.
      for (let i = 0; i < 20; i++) await new Promise(setImmediate);
      return status;
    },
  };
}

function imageTurn(namespace) {
  const images = [
    { s3Key: `${namespace}/_uploads/img_1.png`, contentType: "image/png" },
    { s3Key: `${namespace}/_uploads/missing.png`, contentType: "image/png" },
    { s3Key: `${OTHER_NS}/_uploads/img_9.png`, contentType: "image/png" },
    { s3Key: `${namespace}/_uploads/../../${OTHER_NS}/_uploads/img_9.png`, contentType: "image/png" },
  ];
  return `What is in these pictures?\n\n[OPENCLAW_IMAGES:${JSON.stringify(images)}]`;
}

for (const actor of ACTORS) {
  test(`${actor.label}: a proxy turn logs no actor, platform ID or namespace`, async () => {
    const namespace = namespaceOf(actor.actorId);
    const app = loadProxy(actor);
    for (const stream of [false, true]) {
      const status = await app.chat({ model: "bedrock-agentcore", stream,
        messages: [{ role: "user", content: imageTurn(namespace) }] });
      assert.equal(status, 200, app.output.join("\n"));
    }
    const output = app.output.join("\n");
    assertClean(output, actor, [OTHER_NS]);

    // The lines that used to carry the actor or namespace ran, so the check is not vacuous.
    for (const expected of [
      `[INFO] [proxy][identity] user=${USER}, channel=${actor.actorId.split(":")[0]}, source=identity-file`,
      "[INFO] [proxy] Cognito user provisioned",
      "[INFO] [proxy] Cognito token acquired (expires in 3600s)",
      "[INFO] [proxy] Read AGENTS.md: ",
      "[INFO] [proxy] Copied legacy SOUL.md to _system/",
      "[INFO] [proxy] No USER.md yet (not created)",
      "[INFO] [proxy] Seeding 4 workspace file(s): USER.md, IDENTITY.md, TOOLS.md, MEMORY.md",
      "[INFO] [proxy] Seeded USER.md: ",
      "[INFO] [proxy] Fetched image from S3: _uploads/img_1.png (9 bytes, format=png)",
      "[ERROR] [proxy] Failed to fetch image from S3: _uploads/missing.png",
      "[WARN] [proxy] Rejected S3 image key outside the user's uploads prefix",
      "[WARN] [proxy] Rejected S3 image key with path traversal",
      "[WARN] [proxy] Skipping unfetchable image",
      '[INFO] {"schema":"openclaw.model-route.v1"',
    ]) {
      assert.ok(output.includes(expected), `missing log line ${JSON.stringify(expected)}:\n${output}`);
    }

    // Bedrock request metadata names the internal user ID, never the actor.
    assert.equal(app.model.length, 2);
    for (const request of app.model) {
      assert.equal(request.requestMetadata["openclaw.user_id"], USER);
      assert.equal(Object.hasOwn(request.requestMetadata, "openclaw.actor_id"), false);
      assert.equal(JSON.stringify(request.requestMetadata).includes(actor.platformId), false);
      // The system prompt drops the raw actor; the namespace stays for the skills.
      const system = request.system.map((block) => block.text || "").join("");
      assert.equal(system.includes(actor.actorId), false, "raw actor in the system prompt");
      assert.ok(system.includes(`You are chatting with a user (namespace: ${namespace}) on channel: `));
    }
  });
}

test("a Cognito failure is logged by error name, not by an AWS message that can quote the secret name", async () => {
  const actor = ACTORS[0];
  const namespace = namespaceOf(actor.actorId);
  const secret = `arn:aws:secretsmanager:us-west-2:123456789012:secret:openclaw/cognito-user/${POOL}/${namespace}`;
  const services = fakeIdentityServices();
  services.secretsManager = { async send() {
    throw awsError("AccessDeniedException", "User: arn:aws:sts::123456789012:assumed-role/example/x is not " +
      `authorized to perform: secretsmanager:GetSecretValue on resource: ${secret}`);
  } };
  const app = loadProxy(actor, { services });
  assert.equal(await app.chat({ model: "bedrock-agentcore", messages: [{ role: "user", content: "hello" }] }), 200);
  const output = app.output.join("\n");
  assertClean(output, actor);
  assert.ok(output.includes("[WARN] [proxy] Cognito token acquisition failed: AccessDeniedException"), output);
});

test("a malformed bound actor stops the proxy without echoing it", () => {
  const actor = { label: "WhatsApp", actorId: "whatsapp:+15551234567", platformId: "15551234567" };
  const output = [];
  let exitCode = null;
  assert.throws(() => loadProxy(actor, { output, onExit: (code) => {
    exitCode = code;
    throw new Error(`process.exit(${code})`);
  } }), /process\.exit\(1\)/);
  assert.equal(exitCode, 1);
  assert.ok(output.some((line) => line.startsWith("[ERROR] [proxy] FATAL: bound USER_ID does not match")), output);
  assertClean(output.join("\n"), actor);
});

for (const actor of ACTORS) {
  test(`${actor.label}: Cognito provisioning, migration, resync and token lines name no actor`, async () => {
    const namespace = namespaceOf(actor.actorId);
    const secretId = `openclaw/cognito-user/${POOL}/${namespace}`;
    const lines = [];
    const provider = (services) => createCognitoTokenProvider({
      userPoolId: POOL, clientId: "fixture-client", region: "us-west-2",
      client: services.cognito, commands: cognitoCommands,
      secretsClient: services.secretsManager, secretsCommands,
      log: (message) => lines.push(message),
    });
    // A new user is provisioned; an existing user without a secret is migrated;
    // a user whose Cognito password drifted from its secret is re-synchronised.
    await provider(fakeIdentityServices()).getIdToken(actor.actorId);
    await provider(fakeIdentityServices({ users: { [actor.actorId]: "earlier-password" } }))
      .getIdToken(actor.actorId);
    await provider(fakeIdentityServices({ users: { [actor.actorId]: "drifted" }, secrets: { [secretId]: "stored" } }))
      .getAccessToken(actor.actorId);
    assert.deepEqual(lines, [
      "[cognito] user provisioned",
      "[cognito] token acquired (expires in 3600s)",
      "[cognito] user password migrated to a per-user secret",
      "[cognito] token acquired (expires in 3600s)",
      "[cognito] user password re-synchronised from its secret",
      "[cognito] token acquired (expires in 3600s)",
    ]);
  });
}

test("the scoped container log stream must be named <bound user ID>-<epoch ms>", () => {
  const load = () => {
    const context = vm.createContext({
      module: { exports: {} }, process: { env: {} }, console: { log() {}, warn() {}, error() {} },
      setInterval, clearInterval, setTimeout, clearTimeout, AbortController,
      require: () => ({ CloudWatchLogsClient: class {}, CreateLogGroupCommand: class {},
        CreateLogStreamCommand: class {}, PutLogEventsCommand: class {}, PutRetentionPolicyCommand: class {} }),
    });
    vm.runInContext(fs.readFileSync(require.resolve("./cloudwatch-logger"), "utf8"), context);
    return context.module.exports;
  };
  const options = { binding: { userId: USER, actorId: ACTORS[0].actorId }, generation: "generation-1",
    signal: new AbortController().signal };
  for (const name of [`${namespaceOf(ACTORS[0].actorId)}-1730000000000`, USER, `${USER}-`, `${USER}-abc`,
    "user_fedcba9876543210-1730000000000", `x${USER}-1730000000000`, `${USER}-1730000000000-1`]) {
    assert.throws(() => load().startOwned(name, options), /Missing scoped logger binding/, name);
  }
  const owner = load().startOwned(`${USER}-1730000000000`, options);
  assert.equal(owner.streamName, `${USER}-1730000000000`);
  owner.retire();
});
