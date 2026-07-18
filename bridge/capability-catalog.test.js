"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { describe, it } = require("node:test");
const { pathToFileURL } = require("node:url");

const RELEASE_COMMIT = "0123456789abcdef0123456789abcdef01234567";
const CATALOG_DIGEST =
  "521be0cb78ea1f30d81391e3e1752a5569c7d919fc93c376feb0b1d949ec6b33";
const CAPABILITY_DIR = path.resolve(__dirname, "../specs/capabilities");
const PLUGIN_DIR = path.join(__dirname, "plugins/personal-operator");
const EXPECTED_TOOLS = Object.freeze([
  "po_file_list",
  "po_file_read",
  "po_file_write",
  "po_file_delete",
  "po_web_read",
  "po_schedule_list",
  "po_schedule_propose",
  "po_schedule_cancel_propose",
  "po_compute_run",
  "po_compute_status",
]);
const NEW_TOOLS = EXPECTED_TOOLS.slice(4);

function loadCatalogModule() {
  try {
    delete require.cache[require.resolve("./capability-catalog")];
    return require("./capability-catalog");
  } catch {
    return null;
  }
}

async function loadPlugin() {
  try {
    return await import(
      `${pathToFileURL(path.join(PLUGIN_DIR, "index.js")).href}?catalog=${Date.now()}`
    );
  } catch {
    return null;
  }
}

function copyArtifacts() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "po-catalog-"));
  const target = path.join(root, "capabilities");
  fs.cpSync(CAPABILITY_DIR, target, { recursive: true });
  return { root, target };
}

describe("immutable capability catalog", () => {
  it("loads the exact source catalog and all reviewed schemas", () => {
    const catalogModule = loadCatalogModule();
    assert.ok(catalogModule, "capability-catalog.js must exist");

    const loaded = catalogModule.loadCapabilityCatalog({
      releaseCommit: RELEASE_COMMIT,
      expectedCatalogDigest: CATALOG_DIGEST,
    });
    assert.equal(loaded.catalog.releaseCommit, RELEASE_COMMIT);
    assert.equal(loaded.catalog.catalogDigest, CATALOG_DIGEST);
    assert.deepEqual(loaded.toolNames, EXPECTED_TOOLS);
    assert.deepEqual(Object.keys(loaded.operationRegistry), [
      "workspace.file.list",
      "workspace.file.read",
      "workspace.file.write",
      "workspace.file.delete",
      "web.exact.read",
      "schedule.list",
      "schedule.propose",
      "schedule.cancel.propose",
      "compute.run",
      "compute.status",
    ]);
    assert.equal(Object.isFrozen(loaded), true);
    assert.equal(Object.isFrozen(loaded.catalog.packs), true);
    assert.equal(Object.isFrozen(loaded.toolDefinitions.po_web_read.parameters), true);

    for (const operation of Object.values(loaded.operationRegistry)) {
      const sourcePack = JSON.parse(
        fs.readFileSync(path.join(CAPABILITY_DIR, "catalog-v1.json"), "utf8"),
      ).packs.find((pack) => pack.packId === operation.packId);
      const sourceOperation = sourcePack.operations[0];
      const inputBytes = fs.readFileSync(
        path.join(CAPABILITY_DIR, "schemas", sourceOperation.inputSchema),
      );
      const outputBytes = fs.readFileSync(
        path.join(CAPABILITY_DIR, "schemas", sourceOperation.outputSchema),
      );
      assert.equal(
        operation.inputSchemaDigest,
        crypto.createHash("sha256").update(inputBytes).digest("hex"),
      );
      assert.equal(
        operation.outputSchemaDigest,
        crypto.createHash("sha256").update(outputBytes).digest("hex"),
      );
      assert.deepEqual(
        loaded.toolDefinitions[operation.toolName].parameters,
        JSON.parse(inputBytes),
      );
    }
  });

  it("fails closed on release, catalog, source, schema, and inventory drift", () => {
    const catalogModule = loadCatalogModule();
    assert.throws(
      () =>
        catalogModule.loadCapabilityCatalog({
          releaseCommit: "main",
          expectedCatalogDigest: CATALOG_DIGEST,
        }),
      /release/i,
    );
    assert.throws(
      () =>
        catalogModule.loadCapabilityCatalog({
          releaseCommit: RELEASE_COMMIT,
          expectedCatalogDigest: "a".repeat(64),
        }),
      /catalog.*digest/i,
    );

    for (const mutation of ["source", "schema", "extra", "symlink"]) {
      const { root, target } = copyArtifacts();
      try {
        if (mutation === "source") {
          fs.appendFileSync(path.join(target, "catalog-v1.json"), " ");
        } else if (mutation === "schema") {
          fs.appendFileSync(
            path.join(target, "schemas", "po-web-read-input.json"),
            " ",
          );
        } else if (mutation === "extra") {
          fs.writeFileSync(path.join(target, "schemas", "unreviewed.json"), "{}\n");
        } else {
          const schemaPath = path.join(target, "schemas", "po-web-read-input.json");
          const outsidePath = path.join(root, "outside.json");
          fs.copyFileSync(schemaPath, outsidePath);
          fs.unlinkSync(schemaPath);
          fs.symlinkSync(outsidePath, schemaPath);
        }
        assert.throws(
          () =>
            catalogModule.loadCapabilityCatalog({
              capabilityDir: target,
              releaseCommit: RELEASE_COMMIT,
              expectedCatalogDigest: CATALOG_DIGEST,
            }),
          /source|schema|inventory|digest|artifact/i,
        );
      } finally {
        fs.rmSync(root, { recursive: true, force: true });
      }
    }
  });

  it("keeps catalog, runtime policy, warm-up, plugin, and gateway registry in exact parity", async () => {
    const catalogModule = loadCatalogModule();
    const policy = require("./runtime-policy");
    const warmup = require("./lightweight-agent");
    const plugin = await loadPlugin();
    assert.ok(plugin, "the real plugin must load");

    const registered = [];
    plugin.registerPersonalOperatorPlugin(
      { registerTool: (tool) => registered.push(tool) },
      {
        s3Client: { send: async () => ({ Contents: [] }) },
        env: {
          AWS_REGION: "eu-west-1",
          S3_USER_FILES_BUCKET: "files",
          PERSONAL_OPERATOR_WORKSPACE_PREFIX: "user_alpha",
        },
      },
    );
    const manifest = JSON.parse(
      fs.readFileSync(path.join(PLUGIN_DIR, "openclaw.plugin.json"), "utf8"),
    );

    assert.deepEqual(catalogModule.TOOL_NAMES, EXPECTED_TOOLS);
    assert.deepEqual(policy.APPROVED_TOOLS, EXPECTED_TOOLS);
    assert.deepEqual(policy.PROFILE_ADDITIONS, EXPECTED_TOOLS);
    assert.deepEqual(warmup.LIGHTWEIGHT_TOOL_NAMES, EXPECTED_TOOLS);
    assert.deepEqual(
      warmup.TOOLS.map((tool) => tool.function.name),
      EXPECTED_TOOLS,
    );
    assert.deepEqual(
      registered.map((tool) => tool.name),
      EXPECTED_TOOLS,
    );
    assert.deepEqual(manifest.contracts.tools, EXPECTED_TOOLS);
    assert.deepEqual(
      Object.values(catalogModule.GATEWAY_OPERATION_REGISTRY).map(
        (operation) => operation.toolName,
      ),
      EXPECTED_TOOLS,
    );
    for (const tool of warmup.TOOLS) {
      assert.deepEqual(
        tool.function.parameters,
        catalogModule.TOOL_DEFINITIONS[tool.function.name].parameters,
      );
    }
    for (const tool of registered) {
      assert.deepEqual(
        tool.parameters,
        catalogModule.TOOL_DEFINITIONS[tool.name].parameters,
      );
    }
    assert.equal(EXPECTED_TOOLS.includes("po_capability_call"), false);
    assert.equal(
      EXPECTED_TOOLS.some((name) => /mcp|clawhub|plugin|payment/.test(name)),
      false,
    );
  });

  it("registers every new tool disabled until its exact adapter exists", async () => {
    const plugin = await loadPlugin();
    const registered = [];
    const calls = [];
    plugin.registerPersonalOperatorPlugin(
      { registerTool: (tool) => registered.push(tool) },
      {
        s3Client: { send: async () => ({ Contents: [] }) },
        env: {
          AWS_REGION: "eu-west-1",
          S3_USER_FILES_BUCKET: "files",
          PERSONAL_OPERATOR_WORKSPACE_PREFIX: "user_alpha",
        },
        capabilityAdapters: {
          po_compute_status: async (toolUseId, args) => {
            calls.push([toolUseId, args]);
            return { status: "SUCCEEDED", data: { jobId: args.jobId } };
          },
        },
      },
    );

    for (const name of NEW_TOOLS.filter((tool) => tool !== "po_compute_status")) {
      const tool = registered.find((candidate) => candidate.name === name);
      await assert.rejects(() => tool.execute("tooluse_00000001", {}), (error) => {
        assert.equal(error.code, "CAPABILITY_ADAPTER_DISABLED");
        return true;
      });
    }
    const statusTool = registered.find((tool) => tool.name === "po_compute_status");
    assert.deepEqual(
      await statusTool.execute("tooluse_00000002", { jobId: "job_00000001" }),
      {
        content: [{
          type: "text",
          text: '{"status":"SUCCEEDED","data":{"jobId":"job_00000001"}}',
        }],
        details: {
          status: "SUCCEEDED",
          data: { jobId: "job_00000001" },
        },
      },
    );
    assert.deepEqual(calls, [["tooluse_00000002", { jobId: "job_00000001" }]]);
  });
});
