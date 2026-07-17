import {
  DeleteObjectCommand,
  GetObjectCommand,
  ListObjectsV2Command,
  PutObjectCommand,
  S3Client,
} from "@aws-sdk/client-s3";
import { Type } from "typebox";

export const MAX_PATH_BYTES = 512;
export const MAX_FILE_BYTES = 256 * 1024;
export const MAX_LIST_ITEMS = 1000;
export const MAX_LIST_PAGES = 20;

const WORKSPACE_PREFIX_PATTERN = /^[A-Za-z][A-Za-z0-9_-]{1,64}$/;
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/u;
const WINDOWS_DRIVE_PATH = /^[A-Za-z]:[\\/]/;
const SYMLINK_LIKE_SEGMENT = /(?:^|\.)symlink$/i;

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
  return {
    bucket,
    workspacePrefix,
    objectPrefix: `${workspacePrefix}/`,
    region: runtimeEnv.AWS_REGION || "eu-west-1",
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

export function createWorkspaceStore({ s3Client, env = process.env } = {}) {
  const configuration = resolveConfiguration(env);
  const client = s3Client || new S3Client({ region: configuration.region });

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

const EmptyParameters = Type.Object({}, { additionalProperties: false });
const PathParameters = Type.Object(
  { path: Type.String({ minLength: 1, maxLength: MAX_PATH_BYTES }) },
  { additionalProperties: false },
);
const WriteParameters = Type.Object(
  {
    path: Type.String({ minLength: 1, maxLength: MAX_PATH_BYTES }),
    content: Type.String({ maxLength: MAX_FILE_BYTES }),
  },
  { additionalProperties: false },
);

function textResult(text, details) {
  return { content: [{ type: "text", text }], details };
}

export function registerPersonalOperatorPlugin(api, options = {}) {
  const store = createWorkspaceStore(options);
  api.registerTool({
    name: "po_file_list",
    description: "List UTF-8 files in this user's persistent workspace.",
    parameters: EmptyParameters,
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
    description: "Read one bounded UTF-8 file from this user's workspace.",
    parameters: PathParameters,
    async execute(_id, params) {
      const content = await store.read(params.path);
      return textResult(content, { path: params.path, content });
    },
  });
  api.registerTool({
    name: "po_file_write",
    description: "Create or replace one bounded UTF-8 workspace file.",
    parameters: WriteParameters,
    async execute(_id, params) {
      const result = await store.write(params.path, params.content);
      return textResult(`Wrote ${result.path} (${result.bytes} bytes).`, result);
    },
  });
  api.registerTool({
    name: "po_file_delete",
    description: "Delete one exact file from this user's workspace.",
    parameters: PathParameters,
    async execute(_id, params) {
      const result = await store.delete(params.path);
      return textResult(`Deleted ${result.path}.`, result);
    },
  });
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
