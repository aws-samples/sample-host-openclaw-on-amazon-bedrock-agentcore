import { Readable } from "node:stream";
import { fileURLToPath, pathToFileURL } from "node:url";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, it } from "node:test";
import assert from "node:assert/strict";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const PLUGIN_DIR = __dirname;
const PLUGIN_PATH = path.join(PLUGIN_DIR, "index.js");
const EXPECTED_TOOLS = [
  "po_file_list",
  "po_file_read",
  "po_file_write",
  "po_file_delete",
];

async function loadPlugin() {
  try {
    return await import(`${pathToFileURL(PLUGIN_PATH).href}?test=${Date.now()}`);
  } catch {
    return null;
  }
}

class FakeS3Client {
  constructor(handler) {
    this.handler = handler;
    this.calls = [];
  }

  async send(command) {
    this.calls.push(command);
    return this.handler(command, this.calls.length);
  }
}

function env(overrides = {}) {
  return {
    AWS_REGION: "eu-west-1",
    S3_USER_FILES_BUCKET: "personal-operator-files",
    PERSONAL_OPERATOR_WORKSPACE_PREFIX: "user_alpha123",
    ...overrides,
  };
}

describe("personal-operator plugin package", () => {
  it("is a native ESM plugin package with an exact manifest contract", async () => {
    const manifest = JSON.parse(
      fs.readFileSync(path.join(PLUGIN_DIR, "openclaw.plugin.json"), "utf8"),
    );
    const packageJson = JSON.parse(
      fs.readFileSync(path.join(PLUGIN_DIR, "package.json"), "utf8"),
    );
    const module = await loadPlugin();

    assert.deepEqual(manifest.contracts.tools, EXPECTED_TOOLS);
    assert.equal(manifest.id, "personal-operator");
    assert.equal(manifest.activation.onStartup, true);
    assert.deepEqual(manifest.configSchema, {
      type: "object",
      additionalProperties: false,
      properties: {},
    });
    assert.equal(packageJson.type, "module");
    assert.deepEqual(packageJson.openclaw.extensions, ["./index.js"]);
    assert.ok(module, "the actual ESM entry must load under Node 24");
    assert.equal(module.default.id, "personal-operator");
    assert.equal(typeof module.default.register, "function");
  });

  it("registers exactly four strict workspace tools", async () => {
    const module = await loadPlugin();
    const registered = [];
    module.registerPersonalOperatorPlugin(
      { registerTool: (tool) => registered.push(tool) },
      { s3Client: new FakeS3Client(() => ({})), env: env() },
    );

    assert.deepEqual(
      registered.map((tool) => tool.name),
      EXPECTED_TOOLS,
    );
    for (const tool of registered) {
      assert.equal(tool.parameters.type, "object");
      assert.equal(tool.parameters.additionalProperties, false);
      assert.equal(typeof tool.execute, "function");
    }
    assert.deepEqual(Object.keys(registered[0].parameters.properties), []);
    assert.deepEqual(Object.keys(registered[1].parameters.properties), ["path"]);
    assert.deepEqual(Object.keys(registered[2].parameters.properties), [
      "path",
      "content",
    ]);
    assert.deepEqual(Object.keys(registered[3].parameters.properties), ["path"]);
    for (const tool of registered) {
      assert.equal("userId" in tool.parameters.properties, false);
      assert.equal("namespace" in tool.parameters.properties, false);
      assert.equal("prefix" in tool.parameters.properties, false);
    }
  });
});

describe("workspace namespace boundary", () => {
  it("derives every object key only from the server environment", async () => {
    const module = await loadPlugin();
    const s3 = new FakeS3Client((command) => {
      assert.equal(command.constructor.name, "GetObjectCommand");
      return {
        ContentLength: 4,
        Body: Readable.from([Buffer.from("safe")]),
      };
    });
    const store = module.createWorkspaceStore({ s3Client: s3, env: env() });

    const result = await store.read("notes/today.md", {
      userId: "victim",
      namespace: "victim",
      prefix: "victim",
    });

    assert.equal(result, "safe");
    assert.deepEqual(s3.calls[0].input, {
      Bucket: "personal-operator-files",
      Key: "user_alpha123/files/notes/today.md",
    });
  });

  it("ignores caller-controlled identity fields at the tool boundary", async () => {
    const module = await loadPlugin();
    const s3 = new FakeS3Client(() => ({
      ContentLength: 4,
      Body: Readable.from([Buffer.from("safe")]),
    }));
    const registered = [];
    module.registerPersonalOperatorPlugin(
      { registerTool: (tool) => registered.push(tool) },
      { s3Client: s3, env: env() },
    );
    const readTool = registered.find((tool) => tool.name === "po_file_read");

    await readTool.execute("call-1", {
      path: "notes.md",
      userId: "victim",
      namespace: "victim",
      prefix: "victim",
    });

    assert.equal(s3.calls[0].input.Key, "user_alpha123/files/notes.md");
  });

  it("fails closed when required server namespace configuration is missing", async () => {
    const module = await loadPlugin();
    assert.throws(
      () =>
        module.createWorkspaceStore({
          s3Client: new FakeS3Client(() => ({})),
          env: env({ PERSONAL_OPERATOR_WORKSPACE_PREFIX: "" }),
        }),
      /workspace prefix/i,
    );
  });

  it("uses the exact 2-64 character canonical internal ID grammar", async () => {
    const module = await loadPlugin();
    const fake = new FakeS3Client(() => ({ Contents: [] }));
    for (const workspacePrefix of ["7f9a-legacy", "a".repeat(64)]) {
      const store = module.createWorkspaceStore({
        s3Client: fake,
        env: env({ PERSONAL_OPERATOR_WORKSPACE_PREFIX: workspacePrefix }),
      });
      assert.deepEqual(await store.list(), []);
    }
    for (const workspacePrefix of ["a", "a".repeat(65), "telegram:123"] ) {
      assert.throws(
        () =>
          module.createWorkspaceStore({
            s3Client: fake,
            env: env({ PERSONAL_OPERATOR_WORKSPACE_PREFIX: workspacePrefix }),
          }),
        /workspace prefix/i,
      );
    }
  });

  it("rejects an explicit wrong region before constructing or calling S3", async () => {
    const module = await loadPlugin();
    const s3 = new FakeS3Client(() => assert.fail("S3 must not be called"));
    assert.throws(
      () =>
        module.createWorkspaceStore({
          s3Client: s3,
          env: env({ AWS_REGION: "us-west-2" }),
        }),
      /eu-west-1|region/i,
    );
    assert.equal(s3.calls.length, 0);
  });

  it("requires an explicit scoped credential file for its production S3 client", async () => {
    const module = await loadPlugin();
    assert.throws(
      () => module.createWorkspaceStore({ env: env() }),
      /scoped credential/i,
    );
  });

  it("constructs production S3 with a refreshing explicit file provider", async () => {
    const module = await loadPlugin();
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "plugin-creds-"));
    const credentialsPath = path.join(tmpDir, "scoped-creds.json");
    const writeCredentials = (accessKeyId) =>
      fs.writeFileSync(
        credentialsPath,
        JSON.stringify({
          Version: 1,
          AccessKeyId: accessKeyId,
          SecretAccessKey: "secret",
          SessionToken: "token",
          Expiration: new Date(Date.now() + 60_000).toISOString(),
        }),
      );
    writeCredentials("ASIA_FIRST");
    const constructorOptions = [];
    class FakeConstructor {
      constructor(options) {
        constructorOptions.push(options);
      }
      async send() {
        return { Contents: [] };
      }
    }

    try {
      const store = module.createWorkspaceStore({
        S3ClientConstructor: FakeConstructor,
        env: env({
          PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE: credentialsPath,
        }),
      });
      assert.equal(constructorOptions.length, 1);
      assert.equal(constructorOptions[0].region, "eu-west-1");
      assert.equal(typeof constructorOptions[0].credentials, "function");
      assert.equal(
        (await constructorOptions[0].credentials()).accessKeyId,
        "ASIA_FIRST",
      );
      writeCredentials("ASIA_SECOND");
      assert.equal(
        (await constructorOptions[0].credentials()).accessKeyId,
        "ASIA_SECOND",
      );
      assert.deepEqual(await store.list(), []);
    } finally {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });
});

describe("strict path and UTF-8 validation", () => {
  it("rejects traversal, absolute, control, directory, and symlink-like paths", async () => {
    const module = await loadPlugin();
    const store = module.createWorkspaceStore({
      s3Client: new FakeS3Client(() => {
        throw new Error("S3 must not be called for invalid paths");
      }),
      env: env(),
    });
    const invalidPaths = [
      "",
      "/etc/passwd",
      "../other-user/file",
      "notes/../secret",
      "notes/./today",
      "notes//today",
      "notes\\today",
      "C:/Windows/system.ini",
      "folder/",
      "notes/evil\u0000name",
      "notes/current.symlink",
      "x".repeat(513),
      "bad\ud800name",
    ];

    for (const filePath of invalidPaths) {
      await assert.rejects(() => store.read(filePath), /path/i, filePath);
    }
  });

  it("rejects reserved internal top-level namespaces before S3 access", async () => {
    const module = await loadPlugin();
    const s3 = new FakeS3Client(() => ({}));
    const store = module.createWorkspaceStore({
      s3Client: s3,
      env: env(),
    });

    for (const filePath of [
      ".openclaw/openclaw.json",
      "_uploads/image.png",
      "_internal/state.json",
      "internal/state.json",
    ]) {
      await assert.rejects(() => store.read(filePath), /reserved/i, filePath);
    }
    assert.equal(s3.calls.length, 0);
  });

  it("accepts nested Unicode relative paths", async () => {
    const module = await loadPlugin();
    const s3 = new FakeS3Client((command) => {
      assert.equal(command.input.Key, "user_alpha123/files/märkmed/tere-世界.txt");
      return { ContentLength: 5, Body: Readable.from([Buffer.from("hello")]) };
    });
    const store = module.createWorkspaceStore({ s3Client: s3, env: env() });

    assert.equal(await store.read("märkmed/tere-世界.txt"), "hello");
  });

  it("rejects oversized or malformed UTF-8 writes before S3", async () => {
    const module = await loadPlugin();
    const store = module.createWorkspaceStore({
      s3Client: new FakeS3Client(() => {
        throw new Error("S3 must not be called for invalid content");
      }),
      env: env(),
    });

    await assert.rejects(
      () => store.write("large.txt", "x".repeat(256 * 1024 + 1)),
      /size|large/i,
    );
    await assert.rejects(
      () => store.write("multibyte.txt", "🌍".repeat(65_537)),
      /size|large/i,
      "the limit must be measured in encoded UTF-8 bytes, not JS characters",
    );
    await assert.rejects(
      () => store.write("malformed.txt", "bad\ud800text"),
      /utf-8/i,
    );
  });

  it("rejects oversized and malformed UTF-8 reads", async () => {
    const module = await loadPlugin();
    const oversized = module.createWorkspaceStore({
      s3Client: new FakeS3Client(() => ({
        ContentLength: 256 * 1024 + 1,
        Body: Readable.from([]),
      })),
      env: env(),
    });
    await assert.rejects(() => oversized.read("large.txt"), /size|large/i);

    const malformed = module.createWorkspaceStore({
      s3Client: new FakeS3Client(() => ({
        ContentLength: 2,
        Body: Readable.from([Buffer.from([0xc3, 0x28])]),
      })),
      env: env(),
    });
    await assert.rejects(() => malformed.read("bad.txt"), /utf-8/i);
  });

  it("rejects objects marked as non-file or symlink-like", async () => {
    const module = await loadPlugin();
    const store = module.createWorkspaceStore({
      s3Client: new FakeS3Client(() => ({
        ContentLength: 4,
        Metadata: { "personal-operator-entry-type": "symlink" },
        Body: Readable.from([Buffer.from("safe")]),
      })),
      env: env(),
    });

    await assert.rejects(() => store.read("current.txt"), /regular file|symlink/i);
  });

  it("caps streamed bytes even when ContentLength is missing or deceptive", async () => {
    const module = await loadPlugin();
    for (const contentLength of [undefined, 0, false]) {
      const store = module.createWorkspaceStore({
        s3Client: new FakeS3Client(() => ({
          ...(contentLength === undefined ? {} : { ContentLength: contentLength }),
          Body: Readable.from([
            Buffer.alloc(128 * 1024),
            Buffer.alloc(128 * 1024),
            Buffer.from("x"),
          ]),
        })),
        env: env(),
      });

      await assert.rejects(
        () => store.read("deceptive.txt"),
        /size|large/i,
        `ContentLength=${String(contentLength)}`,
      );
    }
  });
});

describe("bounded S3 workspace operations", () => {
  it("lists files deterministically across pages and ignores directory markers", async () => {
    const module = await loadPlugin();
    const s3 = new FakeS3Client((command, callNumber) => {
      assert.equal(command.constructor.name, "ListObjectsV2Command");
      assert.equal(command.input.Bucket, "personal-operator-files");
      assert.equal(command.input.Prefix, "user_alpha123/files/");
      if (callNumber === 1) {
        assert.equal(command.input.ContinuationToken, undefined);
        return {
          Contents: [
            { Key: "user_alpha123/files/z.txt", Size: 3 },
            { Key: "user_alpha123/files/folder/", Size: 0 },
            { Key: "user_alpha123/files/é.txt", Size: 2 },
          ],
          NextContinuationToken: "next-page",
        };
      }
      assert.equal(command.input.ContinuationToken, "next-page");
      return {
        Contents: [{ Key: "user_alpha123/files/a.txt", Size: 1 }],
      };
    });
    const store = module.createWorkspaceStore({ s3Client: s3, env: env() });

    assert.deepEqual(await store.list(), [
      { path: "a.txt", size: 1 },
      { path: "z.txt", size: 3 },
      { path: "é.txt", size: 2 },
    ]);
  });

  it("rejects any returned object outside the exact server prefix", async () => {
    const module = await loadPlugin();
    const store = module.createWorkspaceStore({
      s3Client: new FakeS3Client(() => ({
        Contents: [
          { Key: "user_alpha123/files/ok.txt", Size: 1 },
          { Key: "user_alpha1234/collision.txt", Size: 2 },
        ],
      })),
      env: env(),
    });

    await assert.rejects(() => store.list(), /prefix|namespace/i);
  });

  it("caps list items before returning an attacker-sized response", async () => {
    const module = await loadPlugin();
    const store = module.createWorkspaceStore({
      s3Client: new FakeS3Client(() => ({
        Contents: Array.from({ length: 1001 }, (_, index) => ({
          Key: `user_alpha123/files/file-${String(index).padStart(4, "0")}.txt`,
          Size: 1,
        })),
      })),
      env: env(),
    });

    await assert.rejects(() => store.list(), /too many|limit/i);
  });

  it("caps list pagination even when S3 never terminates it", async () => {
    const module = await loadPlugin();
    const s3 = new FakeS3Client((_command, callNumber) => ({
      Contents: [],
      NextContinuationToken: `page-${callNumber}`,
    }));
    const store = module.createWorkspaceStore({ s3Client: s3, env: env() });

    await assert.rejects(() => store.list(), /pages|pagination|limit/i);
    assert.ok(s3.calls.length <= 20, "pagination must stop at a fixed bound");
  });

  it("writes UTF-8 bytes with the fixed object key and content type", async () => {
    const module = await loadPlugin();
    const s3 = new FakeS3Client((command) => {
      assert.equal(command.constructor.name, "PutObjectCommand");
      assert.equal(command.input.Bucket, "personal-operator-files");
      assert.equal(command.input.Key, "user_alpha123/files/notes.txt");
      assert.deepEqual(command.input.Body, Buffer.from("Tere 🌍", "utf8"));
      assert.equal(command.input.ContentType, "text/plain; charset=utf-8");
      assert.deepEqual(command.input.Metadata, {
        "personal-operator-entry-type": "file",
      });
      return {};
    });
    const store = module.createWorkspaceStore({ s3Client: s3, env: env() });

    assert.deepEqual(await store.write("notes.txt", "Tere 🌍"), {
      path: "notes.txt",
      bytes: 9,
    });
  });

  it("deletes only the exact server-prefixed object", async () => {
    const module = await loadPlugin();
    const s3 = new FakeS3Client((command) => {
      assert.equal(command.constructor.name, "DeleteObjectCommand");
      assert.deepEqual(command.input, {
        Bucket: "personal-operator-files",
        Key: "user_alpha123/files/old.txt",
      });
      return {};
    });
    const store = module.createWorkspaceStore({ s3Client: s3, env: env() });

    assert.deepEqual(await store.delete("old.txt"), {
      path: "old.txt",
      deleted: true,
    });
  });
});
