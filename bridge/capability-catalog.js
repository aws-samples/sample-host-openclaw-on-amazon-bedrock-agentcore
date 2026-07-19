"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

// The reviewed catalog is copied beside this module in both the source tree and
// the runtime image. Keeping the runtime lookup image-local avoids an implicit
// dependency on the repository layout or any mutable mounted workspace.
const DEFAULT_CAPABILITY_DIR = path.resolve(__dirname, "capabilities");
const DEFAULT_RELEASE_PATH = path.join(
  DEFAULT_CAPABILITY_DIR,
  "release-v1.json",
);
const SOURCE_CATALOG_SHA256 =
  "b4385b54dfa5aaa7ecf2e916111e44248b647b15208432bb9d31883c26e87a26";
const SOURCE_SCHEMA = "personal-operator.capability-catalog-source.v1";
const CATALOG_SCHEMA = "personal-operator.capability-catalog.v1";
const RELEASE_METADATA_SCHEMA = "personal-operator.capability-release.v1";
const RELEASE_COMMIT_PATTERN = /^[0-9a-f]{40}$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;

const SCHEMA_SHA256 = Object.freeze({
  "po-compute-run-input.json": "01cf09ff29529611b51bbee73b86f32b99a6814f7319ba27b18bef5e579b2a1d",
  "po-compute-run-output.json": "ea26fc131f78e0377d5fbda50e8b1b9cf688d1c43f5a3a61dd3bd66953c08eb4",
  "po-compute-status-input.json": "54c5277b5f0b1da875e5c4321161cff110ab3ef9048409c9f1e7d7a70ab938d8",
  "po-compute-status-output.json": "144dd0d56b7897d0dfe225fdb20780e30225cddf7da19b7acbdbbd4ab1618c08",
  "po-file-delete-input.json": "b731089b54f1c87185741f0045d04555a1e77fb05ded2c7a7e3ce4389759b224",
  "po-file-delete-output.json": "657d345a47d261692729a01ca96a6cd0d9b9b65c7bb862989503ee111cfd2d19",
  "po-file-list-input.json": "35e141ebe098d3cbc73ef1655e93ed743520f76cab08a533574c1262330d344a",
  "po-file-list-output.json": "6a6e9fe40d241f055d5509e7c337a1c279ba4e175fbddfed5bd9cc76ad09663f",
  "po-file-read-input.json": "adf79b35ad5da0e5ccfba630254e1b8cb6b0ee41db07cfe34d7d5004851bfb5f",
  "po-file-read-output.json": "f6d5ec6a082465d3b66d4af47bdc41cc0fdcded0adfed706e14d18019188b34a",
  "po-file-write-input.json": "3c83b398c5709887c626fd70d36e251cce8a0c96c7b28080c530ff2c587d652d",
  "po-file-write-output.json": "efacdca9b890b9ba2cb239a25488f487d00c4a6f070f532b2910d6cbd88e0052",
  "po-schedule-cancel-propose-input.json": "fdee4f975779c5e83d216d20b1aabd68623c257903bd1d86ac3b776116383f15",
  "po-schedule-cancel-propose-output.json": "311792ffe90921e24eca03e043c21b5b790e32be1046a0dd533b148d0c98fa78",
  "po-schedule-list-input.json": "1ea525d11a67b6581efb9f064818e5da4782914ed8e7ef93c295ab3c93e3a710",
  "po-schedule-list-output.json": "605a0da1316c22151b3e64d8be0a6fc5168353e7e9a99097ec35758f6c4986fa",
  "po-schedule-propose-input.json": "e3586bade20d0d184b65d5c79b41c22703c4feccd9fdbdb137fafc1006b3f12b",
  "po-schedule-propose-output.json": "8a82e3987b4f193e6c0edf8ca98245c950183883bf688f1d3690f7a05410198d",
  "po-web-read-input.json": "3ce72f18e68a9c53329670e403dbf176c8fb5203d25d23438f29f5285defd80f",
  "po-web-read-output.json": "c241a4bd2a4c986821f0aa71b4c6c883319c43598e3904e18e52ece8ad6e99e5",
});

const TOOL_DESCRIPTIONS = Object.freeze({
  po_file_list: "List UTF-8 files in this user's persistent workspace.",
  po_file_read: "Read one bounded UTF-8 file from this user's workspace.",
  po_file_write: "Create or replace one bounded UTF-8 workspace file.",
  po_file_delete: "Delete one exact file from this user's workspace.",
  po_web_read: "Read one exact currently authorized public HTTPS URL.",
  po_schedule_list: "List this user's governed schedules.",
  po_schedule_propose: "Prepare an exact schedule proposal for approval.",
  po_schedule_cancel_propose: "Prepare an exact schedule cancellation proposal for approval.",
  po_compute_run: "Start one bounded networkless compute job.",
  po_compute_status: "Read the status of one bounded compute job.",
});

function sha256(bytes) {
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

function canonicalJson(value) {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number" && Number.isSafeInteger(value)) {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((entry) => canonicalJson(entry)).join(",")}]`;
  }
  if (value && typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype) {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(",")}}`;
  }
  throw new TypeError("Catalog value is not canonical JSON data");
}

function deepFreeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    for (const nested of Object.values(value)) deepFreeze(nested);
    Object.freeze(value);
  }
  return value;
}

function readPinnedJson(filePath, expectedDigest, label) {
  let raw;
  try {
    const metadata = fs.lstatSync(filePath);
    if (!metadata.isFile() || metadata.isSymbolicLink()) {
      throw new Error("not a regular file");
    }
    raw = fs.readFileSync(filePath);
  } catch {
    throw new Error(`${label} artifact is unavailable or not a regular file`);
  }
  if (sha256(raw) !== expectedDigest) {
    throw new Error(`${label} digest differs from the reviewed release`);
  }
  try {
    return { raw, value: JSON.parse(raw.toString("utf8")) };
  } catch {
    throw new Error(`${label} artifact is not valid JSON`);
  }
}

function loadPinnedArtifacts(capabilityDir = DEFAULT_CAPABILITY_DIR) {
  if (typeof capabilityDir !== "string" || capabilityDir.length === 0) {
    throw new TypeError("capabilityDir must identify the reviewed artifacts");
  }
  const root = path.resolve(capabilityDir);
  const source = readPinnedJson(
    path.join(root, "catalog-v1.json"),
    SOURCE_CATALOG_SHA256,
    "Catalog source",
  ).value;
  if (source.schema !== SOURCE_SCHEMA || !Array.isArray(source.packs)) {
    throw new Error("Catalog source schema is not the frozen v1 source");
  }

  const expectedInventory = Object.keys(SCHEMA_SHA256).sort();
  let actualInventory;
  try {
    actualInventory = fs.readdirSync(path.join(root, "schemas")).sort();
  } catch {
    throw new Error("Schema inventory is unavailable");
  }
  if (canonicalJson(actualInventory) !== canonicalJson(expectedInventory)) {
    throw new Error("Schema inventory differs from the reviewed release");
  }

  const schemas = {};
  for (const filename of expectedInventory) {
    schemas[filename] = readPinnedJson(
      path.join(root, "schemas", filename),
      SCHEMA_SHA256[filename],
      `Schema ${filename}`,
    ).value;
  }

  const packs = [];
  const toolDefinitions = {};
  const operationRegistry = {};
  const referencedSchemas = new Set();
  for (const sourcePack of source.packs) {
    if (!Array.isArray(sourcePack.operations) || sourcePack.operations.length !== 1) {
      throw new Error("Catalog source pack must contain one exact operation");
    }
    const sourceOperation = sourcePack.operations[0];
    const inputSchemaDigest = SCHEMA_SHA256[sourceOperation.inputSchema];
    const outputSchemaDigest = SCHEMA_SHA256[sourceOperation.outputSchema];
    if (!inputSchemaDigest || !outputSchemaDigest) {
      throw new Error("Catalog source references an unreviewed schema");
    }
    referencedSchemas.add(sourceOperation.inputSchema);
    referencedSchemas.add(sourceOperation.outputSchema);
    const operation = {
      operationId: sourceOperation.operationId,
      toolName: sourceOperation.toolName,
      inputSchemaDigest,
      outputSchemaDigest,
    };
    const compiledPack = { ...sourcePack, operations: [operation] };
    packs.push(compiledPack);
    operationRegistry[operation.operationId] = {
      packId: sourcePack.packId,
      version: sourcePack.version,
      operationId: operation.operationId,
      toolName: operation.toolName,
      inputSchemaDigest,
      outputSchemaDigest,
      inputSchema: schemas[sourceOperation.inputSchema],
      outputSchema: schemas[sourceOperation.outputSchema],
      riskClass: sourcePack.riskClass,
      approvalPolicy: sourcePack.approvalPolicy,
      credentialBoundary: sourcePack.credentialBoundary,
      retryPolicy: sourcePack.retryPolicy,
      quotaPolicy: sourcePack.quotaPolicy,
      adapterEnabled: false,
    };
    toolDefinitions[operation.toolName] = {
      name: operation.toolName,
      description: TOOL_DESCRIPTIONS[operation.toolName],
      parameters: schemas[sourceOperation.inputSchema],
      outputSchema: schemas[sourceOperation.outputSchema],
      operationId: operation.operationId,
      packId: sourcePack.packId,
    };
  }
  if (
    referencedSchemas.size !== expectedInventory.length ||
    expectedInventory.some((filename) => !referencedSchemas.has(filename))
  ) {
    throw new Error("Catalog source does not bind the exact schema inventory");
  }
  if (
    Object.keys(toolDefinitions).length !== 10 ||
    Object.values(toolDefinitions).some((tool) => !tool.description)
  ) {
    throw new Error("Catalog source does not contain the exact ten reviewed tools");
  }
  return deepFreeze({ packs, toolDefinitions, operationRegistry });
}

function loadCapabilityCatalog({
  capabilityDir = DEFAULT_CAPABILITY_DIR,
  releaseCommit,
  expectedCatalogDigest,
} = {}) {
  if (typeof releaseCommit !== "string" || !RELEASE_COMMIT_PATTERN.test(releaseCommit)) {
    throw new Error("Release commit must be an exact lowercase 40-character Git SHA");
  }
  if (
    typeof expectedCatalogDigest !== "string" ||
    !SHA256_PATTERN.test(expectedCatalogDigest)
  ) {
    throw new Error("Expected catalog digest must be an exact SHA-256");
  }
  const artifacts = loadPinnedArtifacts(capabilityDir);
  const digestInput = {
    schema: CATALOG_SCHEMA,
    releaseCommit,
    packs: artifacts.packs,
  };
  const catalogDigest = sha256(Buffer.from(canonicalJson(digestInput), "utf8"));
  if (catalogDigest !== expectedCatalogDigest) {
    throw new Error("Catalog digest does not bind the exact release and schemas");
  }
  const catalog = { ...digestInput, catalogDigest };
  return deepFreeze({
    catalog,
    toolNames: Object.keys(artifacts.toolDefinitions),
    toolDefinitions: artifacts.toolDefinitions,
    operationRegistry: artifacts.operationRegistry,
  });
}

function loadRuntimeCapabilityRelease({
  capabilityDir = DEFAULT_CAPABILITY_DIR,
  releasePath = DEFAULT_RELEASE_PATH,
} = {}) {
  let raw;
  try {
    const metadata = fs.lstatSync(releasePath);
    if (!metadata.isFile() || metadata.isSymbolicLink()) {
      throw new Error("not a regular file");
    }
    raw = fs.readFileSync(releasePath);
  } catch {
    throw new Error("Capability release metadata is unavailable or unsafe");
  }
  let release;
  try {
    release = JSON.parse(raw.toString("utf8"));
  } catch {
    throw new Error("Capability release metadata is not valid JSON");
  }
  const expectedFields = ["schema", "releaseCommit", "catalogDigest"];
  if (
    !release ||
    typeof release !== "object" ||
    Array.isArray(release) ||
    Object.getPrototypeOf(release) !== Object.prototype ||
    Object.keys(release).length !== expectedFields.length ||
    expectedFields.some((field) => !Object.hasOwn(release, field)) ||
    release.schema !== RELEASE_METADATA_SCHEMA ||
    typeof release.releaseCommit !== "string" ||
    !RELEASE_COMMIT_PATTERN.test(release.releaseCommit) ||
    typeof release.catalogDigest !== "string" ||
    !SHA256_PATTERN.test(release.catalogDigest)
  ) {
    throw new Error("Capability release metadata violates its exact contract");
  }
  const loaded = loadCapabilityCatalog({
    capabilityDir,
    releaseCommit: release.releaseCommit,
    expectedCatalogDigest: release.catalogDigest,
  });
  return deepFreeze({
    release: { ...release },
    ...loaded,
  });
}

const PINNED_ARTIFACTS = loadPinnedArtifacts();
const TOOL_NAMES = deepFreeze(Object.keys(PINNED_ARTIFACTS.toolDefinitions));
const WORKSPACE_TOOL_NAMES = deepFreeze(TOOL_NAMES.slice(0, 4));
const CAPABILITY_TOOL_NAMES = deepFreeze(TOOL_NAMES.slice(4));
const TOOL_DEFINITIONS = PINNED_ARTIFACTS.toolDefinitions;
const GATEWAY_OPERATION_REGISTRY = PINNED_ARTIFACTS.operationRegistry;

module.exports = {
  CAPABILITY_TOOL_NAMES,
  DEFAULT_CAPABILITY_DIR,
  DEFAULT_RELEASE_PATH,
  GATEWAY_OPERATION_REGISTRY,
  SCHEMA_SHA256,
  SOURCE_CATALOG_SHA256,
  TOOL_DEFINITIONS,
  TOOL_NAMES,
  WORKSPACE_TOOL_NAMES,
  loadCapabilityCatalog,
  loadRuntimeCapabilityRelease,
};
