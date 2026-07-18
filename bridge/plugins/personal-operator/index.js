import {
  DeleteObjectCommand,
  GetObjectCommand,
  ListObjectsV2Command,
  PutObjectCommand,
  S3Client,
} from "@aws-sdk/client-s3";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const {
  CAPABILITY_TOOL_NAMES,
  TOOL_DEFINITIONS,
} = require("../../capability-catalog.js");
const {
  createCapabilityAdapters,
  createLoopbackRelayClient,
} = require("../../capability-relay.js");

export const MAX_PATH_BYTES = 512;
export const MAX_FILE_BYTES = 256 * 1024;
export const MAX_LIST_ITEMS = 1000;
export const MAX_LIST_PAGES = 20;

const WORKSPACE_PREFIX_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$/;
const RUNTIME_REGION = "eu-west-1";
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/u;
const WINDOWS_DRIVE_PATH = /^[A-Za-z]:[\\/]/;
const SYMLINK_LIKE_SEGMENT = /(?:^|\.)symlink$/i;
const RESERVED_TOP_LEVEL_SEGMENTS = new Set([
  ".openclaw",
  "_uploads",
  "_internal",
  "internal",
]);

function assertWellFormed(value, label) {
  if (typeof value !== "string" || !value.isWellFormed()) {
    throw new Error(`${label} must be well-formed UTF-8 text`);
  }
}

export function validateRelativePath(value) {
  assertWellFormed(value, "File path");
  if (value.length === 0 || Buffer.byteLength(value, "utf8") > MAX_PATH_BYTES) {
    throw new Error("File path is empty or exceeds the path size limit");
  }
  if (
    value.startsWith("/") ||
    value.startsWith("\\") ||
    WINDOWS_DRIVE_PATH.test(value) ||
    value.includes("\\") ||
    value.endsWith("/") ||
    CONTROL_CHARACTERS.test(value)
  ) {
    throw new Error("File path must be a safe relative file path");
  }

  const segments = value.split("/");
  if (
    segments.some(
      (segment) =>
        segment.length === 0 ||
        segment === "." ||
        segment === ".." ||
        SYMLINK_LIKE_SEGMENT.test(segment),
    )
  ) {
    throw new Error("File path contains a directory, traversal, or symlink-like segment");
  }
  if (RESERVED_TOP_LEVEL_SEGMENTS.has(segments[0].toLowerCase())) {
    throw new Error("File path uses a reserved internal top-level namespace");
  }
  return value;
}

function resolveConfiguration(runtimeEnv) {
  const bucket = runtimeEnv.S3_USER_FILES_BUCKET;
  const workspacePrefix = runtimeEnv.PERSONAL_OPERATOR_WORKSPACE_PREFIX;
  if (typeof bucket !== "string" || bucket.length === 0) {
    throw new Error("S3 user-files bucket is required");
  }
  if (
    typeof workspacePrefix !== "string" ||
    !WORKSPACE_PREFIX_PATTERN.test(workspacePrefix)
  ) {
    throw new Error("A valid server-derived workspace prefix is required");
  }
  for (const key of ["AWS_REGION", "AWS_DEFAULT_REGION"]) {
    if (
      runtimeEnv[key] !== undefined &&
      runtimeEnv[key] !== "" &&
      runtimeEnv[key] !== RUNTIME_REGION
    ) {
      throw new Error(`${key} must be exactly ${RUNTIME_REGION}`);
    }
  }
  return {
    bucket,
    workspacePrefix,
    objectPrefix: `${workspacePrefix}/files/`,
    region: RUNTIME_REGION,
    credentialsFile: runtimeEnv.PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE,
  };
}

export function createCredentialFileProvider(
  credentialsFile,
  { readFile = readFileSync, now = () => Date.now() } = {},
) {
  if (typeof credentialsFile !== "string" || credentialsFile.length === 0) {
    throw new Error("An explicit scoped credential file is required");
  }
  return async () => {
    let document;
    try {
      document = JSON.parse(readFile(credentialsFile, "utf8"));
    } catch (error) {
      throw new Error(`Scoped credential file cannot be read: ${error.message}`);
    }
    const expiration = new Date(document?.Expiration);
    if (
      document?.Version !== 1 ||
      typeof document.AccessKeyId !== "string" ||
      document.AccessKeyId.length === 0 ||
      typeof document.SecretAccessKey !== "string" ||
      document.SecretAccessKey.length === 0 ||
      typeof document.SessionToken !== "string" ||
      document.SessionToken.length === 0 ||
      !Number.isFinite(expiration.getTime()) ||
      expiration.getTime() <= now()
    ) {
      throw new Error("Scoped credential file is incomplete or expired");
    }
    return {
      accessKeyId: document.AccessKeyId,
      secretAccessKey: document.SecretAccessKey,
      sessionToken: document.SessionToken,
      expiration,
    };
  };
}

function assertRegularFileMetadata(metadata) {
  if (!metadata) return;
  const entryType = metadata["personal-operator-entry-type"];
  const symlinkTarget = metadata["symlink-target"];
  if ((entryType && entryType !== "file") || symlinkTarget) {
    throw new Error("Workspace object is not a regular file (symlink-like object rejected)");
  }
}

async function readBodyBounded(body) {
  if (body === undefined || body === null) {
    throw new Error("Workspace file body is unavailable");
  }

  const chunks = [];
  let totalBytes = 0;
  const append = (chunk) => {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    totalBytes += bytes.length;
    if (totalBytes > MAX_FILE_BYTES) {
      throw new Error("Workspace file exceeds the read size limit");
    }
    chunks.push(bytes);
  };

  if (typeof body === "string" || Buffer.isBuffer(body) || body instanceof Uint8Array) {
    append(body);
  } else if (body[Symbol.asyncIterator]) {
    for await (const chunk of body) append(chunk);
  } else if (typeof body.transformToByteArray === "function") {
    append(await body.transformToByteArray());
  } else {
    throw new Error("Workspace file body cannot be read safely");
  }

  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
  } catch {
    throw new Error("Workspace file is not valid UTF-8");
  }
}

export function createWorkspaceStore({
  s3Client,
  S3ClientConstructor = S3Client,
  env = process.env,
} = {}) {
  const configuration = resolveConfiguration(env);
  const client =
    s3Client ||
    new S3ClientConstructor({
      region: configuration.region,
      credentials: createCredentialFileProvider(configuration.credentialsFile),
    });

  const objectKey = (filePath) =>
    `${configuration.objectPrefix}${validateRelativePath(filePath)}`;

  return Object.freeze({
    async list() {
      const files = [];
      let continuationToken;

      for (let page = 0; page < MAX_LIST_PAGES; page += 1) {
        const response = await client.send(
          new ListObjectsV2Command({
            Bucket: configuration.bucket,
            Prefix: configuration.objectPrefix,
            ...(continuationToken
              ? { ContinuationToken: continuationToken }
              : {}),
          }),
        );

        for (const object of response.Contents || []) {
          if (
            typeof object.Key !== "string" ||
            !object.Key.startsWith(configuration.objectPrefix)
          ) {
            throw new Error("S3 returned an object outside the workspace namespace prefix");
          }
          const relativePath = object.Key.slice(configuration.objectPrefix.length);
          if (relativePath.length === 0 || relativePath.endsWith("/")) continue;
          validateRelativePath(relativePath);
          files.push({
            path: relativePath,
            size:
              Number.isSafeInteger(object.Size) && object.Size >= 0
                ? object.Size
                : 0,
          });
          if (files.length > MAX_LIST_ITEMS) {
            throw new Error("Workspace contains too many files for one bounded listing");
          }
        }

        continuationToken = response.NextContinuationToken;
        if (!continuationToken) {
          return files.sort((left, right) =>
            left.path < right.path ? -1 : left.path > right.path ? 1 : 0,
          );
        }
      }
      throw new Error("Workspace list pagination exceeded the page limit");
    },

    async read(filePath) {
      const response = await client.send(
        new GetObjectCommand({
          Bucket: configuration.bucket,
          Key: objectKey(filePath),
        }),
      );
      if (
        typeof response.ContentLength === "number" &&
        response.ContentLength > MAX_FILE_BYTES
      ) {
        throw new Error("Workspace file exceeds the read size limit");
      }
      assertRegularFileMetadata(response.Metadata);
      return readBodyBounded(response.Body);
    },

    async write(filePath, content) {
      const safePath = validateRelativePath(filePath);
      assertWellFormed(content, "File content");
      const body = Buffer.from(content, "utf8");
      if (body.length > MAX_FILE_BYTES) {
        throw new Error("Workspace file exceeds the write size limit");
      }
      await client.send(
        new PutObjectCommand({
          Bucket: configuration.bucket,
          Key: `${configuration.objectPrefix}${safePath}`,
          Body: body,
          ContentType: "text/plain; charset=utf-8",
          Metadata: { "personal-operator-entry-type": "file" },
        }),
      );
      return { path: safePath, bytes: body.length };
    },

    async delete(filePath) {
      const safePath = validateRelativePath(filePath);
      await client.send(
        new DeleteObjectCommand({
          Bucket: configuration.bucket,
          Key: `${configuration.objectPrefix}${safePath}`,
        }),
      );
      return { path: safePath, deleted: true };
    },
  });
}

function textResult(text, details) {
  return { content: [{ type: "text", text }], details };
}

function capabilityAdapter(adapters, toolName) {
  if (adapters instanceof Map) return adapters.get(toolName);
  if (
    adapters &&
    typeof adapters === "object" &&
    Object.hasOwn(adapters, toolName)
  ) {
    return adapters[toolName];
  }
  return undefined;
}

function disabledCapabilityError(toolName) {
  const error = new Error(`Capability tool '${toolName}' is disabled`);
  error.code = "CAPABILITY_ADAPTER_DISABLED";
  return error;
}

export function registerPersonalOperatorPlugin(api, options = {}) {
  const store = createWorkspaceStore(options);
  const configuredCapabilityAdapters =
    options.capabilityAdapters === undefined
      ? createCapabilityAdapters({
          client:
            options.loopbackRelayClient || createLoopbackRelayClient(),
        })
      : options.capabilityAdapters;
  api.registerTool({
    name: "po_file_list",
    description: TOOL_DEFINITIONS.po_file_list.description,
    parameters: TOOL_DEFINITIONS.po_file_list.parameters,
    async execute() {
      const files = await store.list();
      const text =
        files.length === 0
          ? "No workspace files."
          : files.map((file) => `${file.path} (${file.size} bytes)`).join("\n");
      return textResult(text, { files });
    },
  });
  api.registerTool({
    name: "po_file_read",
    description: TOOL_DEFINITIONS.po_file_read.description,
    parameters: TOOL_DEFINITIONS.po_file_read.parameters,
    async execute(_id, params) {
      const content = await store.read(params.path);
      return textResult(content, { path: params.path, content });
    },
  });
  api.registerTool({
    name: "po_file_write",
    description: TOOL_DEFINITIONS.po_file_write.description,
    parameters: TOOL_DEFINITIONS.po_file_write.parameters,
    async execute(_id, params) {
      const result = await store.write(params.path, params.content);
      return textResult(`Wrote ${result.path} (${result.bytes} bytes).`, result);
    },
  });
  api.registerTool({
    name: "po_file_delete",
    description: TOOL_DEFINITIONS.po_file_delete.description,
    parameters: TOOL_DEFINITIONS.po_file_delete.parameters,
    async execute(_id, params) {
      const result = await store.delete(params.path);
      return textResult(`Deleted ${result.path}.`, result);
    },
  });
  for (const toolName of CAPABILITY_TOOL_NAMES) {
    const definition = TOOL_DEFINITIONS[toolName];
    api.registerTool({
      name: toolName,
      description: definition.description,
      parameters: definition.parameters,
      async execute(toolUseId, params) {
        const adapter = capabilityAdapter(configuredCapabilityAdapters, toolName);
        if (typeof adapter !== "function") {
          throw disabledCapabilityError(toolName);
        }
        const result = await adapter(toolUseId, params);
        if (!result || typeof result !== "object" || Array.isArray(result)) {
          throw new Error("Capability relay returned an invalid result");
        }
        return textResult(JSON.stringify(result), result);
      },
    });
  }
}

const plugin = {
  id: "personal-operator",
  name: "Personal Operator Workspace",
  description: "Bounded user workspace tools",
  register(api) {
    registerPersonalOperatorPlugin(api);
  },
};

export default plugin;
